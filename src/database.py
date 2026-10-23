from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from src.card_models import CharacterCard
from src.card_parser import read_card_data, read_chara_card, read_card_from_json, save_thumbnail
from src import safe_json
from src import vault
from src.fs_utils import unique_temp_path
from src.tag_ops import (
    count_tags,
    merge_tag,
    normalize_tag,
    remove_tag,
    rename_tag,
)

logger = logging.getLogger(__name__)

# Bumped when a migration adds work that only needs to run once. Stored in the
# database's PRAGMA user_version so startup can skip migrations that have
# already been applied instead of scanning the whole table every launch.
_SCHEMA_VERSION = 3

# How many cards the one-off body-text backfill may read per launch, and
# how long it may take. Keeps the first start after the update from
# freezing on a large library.
_BODY_BACKFILL_BATCH = 2000
_BODY_BACKFILL_SECONDS = 2.0


def _escape_like(text: str) -> str:
    """Escape SQL LIKE wildcards so user input matches literally.

    Only the LIKE metacharacters are escaped.  A double quote has no special
    meaning in a LIKE pattern, and escaping it made a query for ``say "hi"``
    look for a literal backslash and never match - not even the card's own
    name.  (The tag blob is no longer matched with LIKE at all: it stores
    JSON-escaped values, so it is compared through the ``st_tags_contain``
    helper instead.)
    """
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _st_norm(value) -> str:
    """Unicode-aware, case-insensitive normalisation for SQL comparison.

    Mirrors :func:`src.tag_ops.normalize_tag` so the SQL tag filter and the
    Python tag listing agree on what "the same tag" means. Not SQL-standard
    folding (that would need ICU), just Unicode lower-casing like the rest of
    the app uses.
    """
    if value is None:
        return ''
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return ''
    return value.strip().lower()


def _has_tag(tags_json, tag) -> int:
    """SQL helper: does *tags_json* contain *tag*?

    Compares decoded tag values rather than substring-matching the raw JSON
    text, so tags containing quotes, backslashes or non-ASCII characters match
    exactly. Case-insensitive and whitespace-trimmed on both sides, matching
    :func:`src.tag_ops.normalize_tag`.
    """
    wanted = normalize_tag(tag)
    if not wanted:
        return 0
    for t in _decode_tag_blob(tags_json):
        if normalize_tag(t) == wanted:
            return 1
    return 0


#: The columns a library-wide listing needs.  ``body_text`` is deliberately
#: absent: it holds the whole card, and a listing that only needs the
#: metadata would pull megabytes of text through memory.
_CARD_LIST_COLUMNS = (
    'id, name, source_path, thumbnail_path, tags, creator, '
    'description_preview, creator_notes, token_count, spec_version, '
    'is_favorite, rating, create_date, date_modified'
)


def _unique_gallery_name(name: str, taken: set) -> str:
    """A free ``<stem>__<n><suffix>`` gallery file name."""
    p = Path(name)
    stem, suffix = p.stem, p.suffix
    for n in range(1, 10_000):
        candidate = f'{stem}__{n}{suffix}'
        if candidate not in taken:
            return candidate
    return f'{stem}__{uuid.uuid4().hex[:8]}{suffix}'


def _tag_set(tags) -> set:
    """The normalised set of a tag list (used to compare two lists)."""
    from src.tag_ops import normalize_tag

    return {normalize_tag(t) for t in (tags or []) if normalize_tag(t)}


def _decode_tag_blob(tags_json) -> list:
    """Decode a card's stored ``tags`` JSON blob into a list.

    Card data is third-party input: the blob may be missing, invalid JSON, a
    non-list, or a list of non-strings. Always returns a list so a single
    malformed card can never break a library-wide aggregation.
    """
    if not tags_json:
        return []
    try:
        parsed = safe_json.loads_safe(tags_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def fuzzy_match(query: str, candidate: str) -> bool:
    """Return True when *query* matches *candidate* leniently.

    A match is either an exact match (case-insensitive) or a match of
    *every* whitespace-separated word of the query inside the candidate,
    in any order and regardless of the word order in the query.  An empty
    query never matches: there is nothing to look for.

    Pure and dependency-free, so it can be used from SQL (through the
    ``st_fuzzy_match`` helper below) and from Python code alike.
    """
    wanted = (query or '').strip().casefold()
    if not wanted:
        return False
    text = (candidate or '').casefold()
    if wanted == text:
        return True
    return all(word in text for word in wanted.split())


def _sql_fuzzy_match(candidate, query) -> int:
    """SQL helper: does *candidate* fuzzy-match *query*? (1 or 0)."""
    try:
        return 1 if fuzzy_match(query, candidate or '') else 0
    except Exception:
        return 0


def _sql_fuzzy_tags(tags_json, query) -> int:
    """SQL helper: does any tag of the blob fuzzy-match *query*? (1 or 0)."""
    for tag in _decode_tag_blob(tags_json):
        if fuzzy_match(query, tag):
            return 1
    return 0


def _sql_tags_contain(tags_json, query) -> int:
    """SQL helper: does any tag of the blob contain *query*? (1 or 0).

    The free-text search used to match the raw JSON blob with LIKE, but the
    blob stores the JSON-*escaped* form of a tag: a card tagged ``say "hi"``
    stores ``say \\"hi\\"``, so a search for ``say "hi"`` could never match
    it.  The tags are decoded here and compared as values.
    """
    wanted = (query or '').strip().casefold()
    if not wanted:
        return 0
    for tag in _decode_tag_blob(tags_json):
        if not isinstance(tag, str):
            continue
        if wanted in tag.casefold():
            return 1
    return 0


def _card_body_text(card: CharacterCard) -> str:
    """Return the searchable body of *card*.

    Everything the full-text search looks at: the long description and the
    other prompt fields, the creator notes and the entries of the embedded
    character book (their name, content and keys).
    """
    parts: list[str] = [
        card.description or '',
        card.personality or '',
        card.scenario or '',
        card.first_mes or '',
        card.mes_example or '',
        card.system_prompt or '',
        card.post_history_instructions or '',
        card.creator_notes or '',
    ]
    book = card.character_book
    if isinstance(book, dict):
        entries = book.get('entries')
        if isinstance(entries, dict):
            # SillyTavern's native shape: entries keyed by uid.
            entries = list(entries.values())
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                parts.append(str(entry.get('name') or ''))
                parts.append(str(entry.get('content') or ''))
                keys = entry.get('keys')
                if isinstance(keys, str):
                    keys = [keys]
                if isinstance(keys, (list, tuple)):
                    parts.extend(str(key) for key in keys)
    return '\n'.join(part for part in parts if part)


def _get_data_dir() -> Path:
    from src.app_paths import data_dir
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_library_dir() -> Path:
    d = _get_data_dir() / 'library'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_thumbnail_dir() -> Path:
    d = _get_data_dir() / 'thumbnails'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_gallery_dir() -> Path:
    d = _get_data_dir() / 'gallery'
    d.mkdir(parents=True, exist_ok=True)
    return d


# Windows reserved device names. Library files are safe because they always
# carry a UUID suffix, but exports build a bare "{name}.png" / "{name}.json".
_WINDOWS_RESERVED = re.compile(
    r'(?i)^(con|prn|aux|nul|clock\$|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$'
)


def _sanitize_filename(name: str) -> str:
    # Normalise to NFC first: on a normalising file system (macOS) two
    # differently-normalised spellings of the same name are the same file,
    # so the second import would overwrite the first card.
    name = unicodedata.normalize('NFC', str(name or ''))
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip('. ')
    # Truncate before the reserved-name check: "CON" + 100 chars is still the
    # device name after truncation on some paths, so check the final value.
    name = name[:80]
    if not name:
        return 'untitled'
    if _WINDOWS_RESERVED.match(name):
        return f'_{name}'
    return name


sanitize_filename = _sanitize_filename  # public alias for reuse


def filename_matches_name(source_path: str | Path, name: str) -> bool:
    """True when a card file name already reflects *name*.

    The library convention is ``{sanitize_filename(name)}_{8-hex}{ext}``;
    a file named exactly ``{sanitize_filename(name)}{ext}`` (no suffix) also
    counts, so bulk fixes don't churn already-correct files.
    """
    p = Path(str(source_path or ''))
    if not p.stem:
        return False
    safe = _sanitize_filename(name)
    stem = p.stem
    if stem == safe:
        return True
    if not stem.startswith(safe + '_'):
        return False
    return bool(re.fullmatch(r'[0-9a-f]{8}', stem[len(safe) + 1:]))


def group_duplicate_rows(rows: list[dict]) -> list[list[dict]]:
    """Group card rows by (name, creator) case-insensitively.

    Pure function so the grouping logic can be unit-tested without a
    database.  Returns only groups with more than one entry, preserving
    the order rows were encountered.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (row.get('name', '').lower(), (row.get('creator') or '').lower())
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    return [groups[key] for key in order if len(groups[key]) > 1]


def perceptual_hash(path: str | Path, hash_size: int = 8) -> str:
    """Compute a simple perceptual hash of an image file using PIL.

    Converts to grayscale, resizes to ``hash_size x hash_size``, then builds a
    bit string where each bit is ``1`` if the pixel is above the mean.  Returns
    a hex string.  Two visually similar images produce the same or very similar
    hashes.  Implemented with basic PIL operations to avoid an extra
    ``imagehash`` dependency.
    """
    with vault.open_image(path) as img:
        gray = img.convert('L').resize((hash_size, hash_size))
        try:
            pixels = list(gray.tobytes())
        finally:
            # ``convert``/``resize`` allocate a new image that is not covered
            # by the ``with`` above; hashing a whole library would otherwise
            # hold one extra image in memory per card.
            gray.close()
    avg = sum(pixels) / len(pixels) if pixels else 0
    bits = ''.join('1' if p >= avg else '0' for p in pixels)
    hex_len = max(1, (hash_size * hash_size) // 4)
    return f'{int(bits, 2):0{hex_len}x}'


def hash_hamming_distance(a: str, b: str) -> int:
    """Return the bit-wise Hamming distance between two perceptual hashes.

    Used to group *near*-identical images: a 1-bit difference in an
    average-hash would defeat exact-string grouping entirely.
    """
    try:
        return bin(int(a, 16) ^ int(b, 16)).count('1')
    except (ValueError, TypeError):
        return 64


# Perceptual hashes are 8x8 grids, i.e. 64 bits.
_HASH_BITS = 64


def _band_width(max_distance: int) -> int:
    """Bits per band for a bucket count that is larger than *max_distance*.

    Two hashes that differ in at most *max_distance* bits can differ in at
    most *max_distance* bands, so with more bands than that they must
    share at least one identical band. Returns 0 when no such split
    exists (a tolerance of 64 bits or more), in which case every group has
    to be compared.
    """
    for width in (32, 16, 8, 4, 2, 1):
        if (_HASH_BITS // width) > max_distance:
            return width
    return 0


def _hash_bands(digest: str, width: int) -> list[tuple[int, str]]:
    """Split *digest* into bands of *width* bits.

    Returns ``(band index, band key)`` pairs. The hash is read as a plain
    integer - exactly like :func:`hash_hamming_distance` does - so a short
    hex string and its zero-padded equivalent land in the same bands.
    """
    try:
        value = int(digest, 16)
    except (TypeError, ValueError):
        # An unparseable hash is 64 bits away from everything, so it
        # gets a band of its own and never matches.
        return [(0, 'invalid')]
    mask = (1 << width) - 1
    return [
        (index, format((value >> (index * width)) & mask, '0%db' % width))
        for index in range(_HASH_BITS // width)
    ]


def group_by_perceptual_hash(
    hashed: list[tuple[str, dict]], max_distance: int = 5,
) -> list[list[dict]]:
    """Group ``(hash, row)`` pairs whose hashes are within *max_distance* bits.

    The grouping semantics are unchanged: a row joins the first group (in
    creation order) whose representative hash is within *max_distance*
    bits, and starts a new group when no group matches.

    What changed is how the candidates are found. Comparing every new
    hash with every group made a library-wide scan quadratic. The hashes
    are bucketed into bands instead: two hashes that differ in at most
    *max_distance* bits must share at least one identical band, so only
    the groups in a shared band can match and the scan stays linear for a
    normal library.
    """
    width = _band_width(max_distance)
    groups: list[tuple[str, list[dict]]] = []
    band_index: dict[tuple[int, str], list[int]] = {}
    for digest, row in hashed:
        bands = _hash_bands(digest, width) if width else None
        candidates: set[int] = set()
        if bands is not None:
            for band in bands:
                candidates.update(band_index.get(band, ()))
        else:
            candidates = set(range(len(groups)))
        for index in sorted(candidates):
            representative, members = groups[index]
            if hash_hamming_distance(digest, representative) <= max_distance:
                members.append(row)
                break
        else:
            groups.append((digest, [row]))
            index = len(groups) - 1
            if bands is not None:
                for band in bands:
                    band_index.setdefault(band, []).append(index)
    return [members for _, members in groups]


# Map dropdown display text -> sort key in LibraryDatabase._SORT_WHITELIST.
# Shared by the Library tab's sort combo and the character sidebar's sort
# combo so the two dropdowns can never drift apart or pass a key that isn't
# whitelisted (which would otherwise fall back to the default ordering).
SORT_LABELS: dict[str, str] = {
    'Name': 'name',
    'Date Added': 'date_added',
    'Token Count': 'token_count',
    'Favorites': 'is_favorite',
    'Rating': 'rating',
    'Random': 'random',
}


class LibraryDatabase:
    _SORT_WHITELIST = frozenset({'name', 'date_added', 'token_count', 'is_favorite', 'rating', 'random'})

    def __init__(self, db_path: Optional[str | Path] = None):
        if db_path is None:
            db_path = _get_data_dir() / 'library.db'
        self.db_path = str(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Create a new connection with the busy timeout already applied."""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute('PRAGMA busy_timeout=5000')
        # SQLite's built-in LIKE/lower() are case-insensitive for ASCII only,
        # so a tag like "École" would be offered by the filter dialog (which
        # lower-cases) yet never match its own stored blob. A Python callback
        # gives Unicode-aware, case-insensitive matching.
        conn.create_function('st_norm', 1, _st_norm, deterministic=True)
        conn.create_function('st_has_tag', 2, _has_tag, deterministic=True)
        # Fuzzy (word-wise) matching for the optional typo-tolerant search.
        conn.create_function('st_fuzzy_match', 2, _sql_fuzzy_match, deterministic=True)
        conn.create_function('st_fuzzy_tags', 2, _sql_fuzzy_tags, deterministic=True)
        conn.create_function('st_tags_contain', 2, _sql_tags_contain, deterministic=True)
        return conn

    @contextmanager
    def _conn(self):
        """Yield a connection that commits on success and ALWAYS closes.

        ``sqlite3.Connection`` as a context manager only wraps a
        transaction — it never closes the connection.  This wrapper
        guarantees deterministic cleanup.
        """
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            # WAL mode improves concurrent read/write performance and is safe
            # for desktop single-process use across threads.
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS characters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    source_path TEXT UNIQUE NOT NULL,
                    thumbnail_path TEXT,
                    tags TEXT,
                    creator TEXT,
                    description_preview TEXT,
                    creator_notes TEXT DEFAULT '',
                    token_count INTEGER DEFAULT 0,
                    spec_version TEXT,
                    is_favorite INTEGER DEFAULT 0,
                    create_date TEXT DEFAULT '',
                    date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    date_modified TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    body_text TEXT DEFAULT ''
                )
            ''')
            # Query-path indexes: sorting/filtering would otherwise
            # full-scan on every grid refresh.
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_characters_name '
                'ON characters (name COLLATE NOCASE)'
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_characters_date_added '
                'ON characters (date_added)'
            )
            conn.commit()
        self._migrate_db()
        self._maybe_backup()

    def _maybe_backup(self) -> None:
        """Create a backup of the database on startup if it has changed."""
        db_file = Path(self.db_path)
        if not db_file.exists():
            return

        bak_path = db_file.parent / (db_file.name + '.bak')
        bak2_path = db_file.parent / (db_file.name + '.bak2')

        # Consider WAL file mtime as well — uncheckpointed changes live there.
        wal_path = db_file.parent / (db_file.name + '-wal')
        latest_mtime = db_file.stat().st_mtime
        if wal_path.exists():
            latest_mtime = max(latest_mtime, wal_path.stat().st_mtime)

        if bak_path.exists():
            if latest_mtime <= bak_path.stat().st_mtime:
                return

        self._do_backup(db_file, bak_path, bak2_path)

    def _do_backup(self, db_file: Path, bak_path: Path, bak2_path: Path) -> bool:
        """Checkpoint WAL, rotate the previous backup, and copy the DB.

        The new backup is written to a temp file first so a crash between
        rotation and copy never leaves the user without any backup.
        Returns True when a new backup now exists at *bak_path*.
        """
        conn = self._connect()
        try:
            row = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if row and row[0]:
                # A busy checkpoint leaves committed rows in the -wal file, so
                # copying only the main DB would back up a truncated database
                # and could then rotate a good backup away.
                raise sqlite3.OperationalError(
                    "database is busy; WAL checkpoint did not complete",
                )
        except sqlite3.Error as exc:
            logger.warning("WAL checkpoint failed during backup: %s", exc)
            return False
        finally:
            conn.close()

        # Unique temp name: two concurrent backups (or two instances before
        # the single-instance lock engages) must not interleave writes to the
        # same .bak.tmp.
        tmp_path = unique_temp_path(bak_path.parent, f'.{bak_path.name}.', '.tmp')
        try:
            # The copy is taken through SQLite's backup API rather than by
            # reading the file: read + encrypt + write held three copies of
            # the database in memory at once.
            snap_path = unique_temp_path(bak_path.parent, f'.{bak_path.name}.', '.snap.tmp')
            try:
                src_conn = self._connect()
                try:
                    snap_conn = sqlite3.connect(str(snap_path), timeout=5.0)
                    try:
                        src_conn.backup(snap_conn)
                    finally:
                        snap_conn.close()
                finally:
                    src_conn.close()
                # Vault-aware copy: the backup is sealed like every other
                # data file when encryption is on, so no plaintext DB copy
                # lingers.
                vault.write_bytes(tmp_path, vault.read_bytes(snap_path))
            finally:
                try:
                    snap_path.unlink(missing_ok=True)
                except OSError:
                    pass
            if bak_path.exists():
                if bak2_path.exists():
                    bak2_path.unlink()
                bak_path.rename(bak2_path)
            tmp_path.replace(bak_path)
            logger.info("Database backed up to %s", bak_path)
            return True
        except (OSError, vault.VaultError):
            logger.exception("Database backup failed")
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def backup(self) -> Path:
        """Force a backup of the database. Returns the backup file path.

        Raises :class:`RuntimeError` when the backup did not happen: the
        caller (the UI's "force backup") must not be shown a stale or
        missing file as the new backup.
        """
        db_file = Path(self.db_path)
        if not db_file.exists():
            raise FileNotFoundError(f"Database file not found: {self.db_path}")
        bak_path = db_file.parent / (db_file.name + '.bak')
        bak2_path = db_file.parent / (db_file.name + '.bak2')
        if not self._do_backup(db_file, bak_path, bak2_path):
            raise RuntimeError(
                f"Database backup to {bak_path} did not complete; "
                "see the log for details"
            )
        return bak_path

    def _migrate_db(self) -> None:
        with self._conn() as conn:
            # One-time migrations are gated on PRAGMA user_version so a large
            # library isn't re-scanned on every launch.
            version_row = conn.execute('PRAGMA user_version').fetchone()
            schema_version = version_row[0] if version_row else 0
            cols = {row[1] for row in conn.execute('PRAGMA table_info(characters)').fetchall()}
            if 'creator_notes' not in cols:
                conn.execute("ALTER TABLE characters ADD COLUMN creator_notes TEXT DEFAULT ''")
            if 'create_date' not in cols:
                conn.execute("ALTER TABLE characters ADD COLUMN create_date TEXT DEFAULT ''")
            if 'st_avatar_url' not in cols:
                conn.execute("ALTER TABLE characters ADD COLUMN st_avatar_url TEXT")
            if 'st_sync_hash' not in cols:
                conn.execute("ALTER TABLE characters ADD COLUMN st_sync_hash TEXT")
            if 'rating' not in cols:
                conn.execute("ALTER TABLE characters ADD COLUMN rating INTEGER DEFAULT 0")
            if 'user_notes' not in cols:
                conn.execute("ALTER TABLE characters ADD COLUMN user_notes TEXT DEFAULT ''")
            if 'body_text' not in cols:
                # The searchable body of the card (description, prompts,
                # creator notes and the character book). Kept in the
                # database so the full-text search is a single SQL
                # clause instead of a file read per card.
                conn.execute("ALTER TABLE characters ADD COLUMN body_text TEXT DEFAULT ''")
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_characters_st_avatar_url '
                'ON characters (st_avatar_url)'
            )
            # Favorites-only, minimum-rating and token-count ordering are
            # first-class grid operations; without these every refresh was a
            # full scan plus a temp B-tree for the sort. Created here rather
            # than in _init_db because 'rating' is itself added by a migration.
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_characters_favorite '
                'ON characters (is_favorite)'
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_characters_rating '
                'ON characters (rating)'
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_characters_token_count '
                'ON characters (token_count)'
            )
            # The default grid order is "favorites first, then name"; a
            # composite index lets SQLite use it instead of sorting the
            # whole table on every refresh.
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_characters_fav_name '
                'ON characters (is_favorite, name COLLATE NOCASE)'
            )
            # The duplicate check filters on the creator, which had no
            # index at all.
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_characters_creator '
                'ON characters (creator)'
            )
            # Bookkeeping for the one-off data migrations below. The
            # PRAGMA user_version cannot be used for them: it is a single
            # number for the whole schema, so a migration introduced
            # after the version was last bumped stays unreachable (the
            # legacy tag encoding was gated on ``user_version < 1`` and
            # therefore never ran for a database that had ever had a
            # version set). Each migration records its own completion
            # here instead, so it runs exactly once per database - for
            # every legacy database, whatever version it carries.
            conn.execute('''
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    value TEXT DEFAULT '',
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            self._run_tag_encoding_migration(conn)
            self._run_body_text_backfill(conn)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS card_collections (
                    character_id INTEGER NOT NULL,
                    collection_id INTEGER NOT NULL,
                    PRIMARY KEY (character_id, collection_id)
                )
            ''')
            # The join is driven by collection_id (list_collections and the
            # collection filter), but the PK leads with character_id - so
            # without this every collection query full-scanned the table.
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_card_collections_collection '
                'ON card_collections (collection_id)'
            )
            # Deletion tombstones: when a card that is linked to a
            # SillyTavern file is deleted in ST Explorer, a row is left
            # here so the sync process can delete the ST copy too — and,
            # crucially, never re-import it as if it were a new card.
            conn.execute('''
                CREATE TABLE IF NOT EXISTS deleted_st_cards (
                    st_avatar_url TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    creator TEXT NOT NULL DEFAULT '',
                    st_sync_hash TEXT,
                    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Per-character gallery images: metadata for the extra images
            # stored under gallery/<character_id>/. Order and captions live
            # here rather than in sidecar JSON so reordering is one UPDATE
            # per row and the Doctor can spot orphan rows directly.
            conn.execute('''
                CREATE TABLE IF NOT EXISTS gallery_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    sort_order INTEGER DEFAULT 0,
                    content_hash TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (character_id, file_name)
                )
            ''')
            # The gallery pane lists per character in sort_order on every
            # load; without this every open full-scanned the table.
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_gallery_images_character '
                'ON gallery_images (character_id, sort_order)'
            )
            if schema_version < _SCHEMA_VERSION:
                # Only ever *raise* the version: a database written by a
                # NEWER build (a user_version above ours) must not be
                # downgraded, or every ``schema_version < N`` migration
                # re-runs on the next launch. Only write when it actually
                # changes: an unconditional write bumps the DB mtime on
                # every launch, which made the startup backup fire every
                # single time.
                conn.execute('PRAGMA user_version = %d' % _SCHEMA_VERSION)
            conn.commit()

    def _migration_applied(self, conn: sqlite3.Connection, name: str) -> bool:
        """True when the one-off migration *name* already ran on this database."""
        row = conn.execute(
            'SELECT 1 FROM schema_migrations WHERE name = ?', (name,),
        ).fetchone()
        return row is not None

    def _migration_value(self, conn: sqlite3.Connection, name: str) -> str:
        """Return the recorded value of a migration marker ('' when unset)."""
        row = conn.execute(
            'SELECT value FROM schema_migrations WHERE name = ?', (name,),
        ).fetchone()
        return str(row[0]) if row and row[0] is not None else ''

    def _mark_migration_applied(
        self, conn: sqlite3.Connection, name: str, value: str = '',
    ) -> None:
        """Record that the one-off migration *name* ran (idempotent)."""
        conn.execute(
            'INSERT OR REPLACE INTO schema_migrations (name, value) VALUES (?, ?)',
            (name, value),
        )

    @staticmethod
    def _migrate_tag_encoding(conn: sqlite3.Connection) -> None:
        """Re-encode legacy tag JSON stored with ``ensure_ascii=True``.

        Older builds stored non-ASCII tags as ``\\uXXXX`` escapes, which the
        LIKE-based tag filter can never match against the user-visible text.
        Rows are rewritten with literal (``ensure_ascii=False``) encoding;
        anything that isn't valid JSON or a list is left untouched.

        The migration is idempotent: re-running it over already-fixed rows
        changes nothing.
        """
        rows = conn.execute('SELECT id, tags FROM characters').fetchall()
        for char_id, tags_json in rows:
            if not tags_json or '\\u' not in tags_json:
                continue
            try:
                parsed = safe_json.loads_safe(tags_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(parsed, list):
                continue
            re_encoded = json.dumps(parsed, ensure_ascii=False)
            if re_encoded != tags_json:
                conn.execute(
                    'UPDATE characters SET tags = ? WHERE id = ?',
                    (re_encoded, char_id),
                )

    def _run_tag_encoding_migration(self, conn: sqlite3.Connection) -> None:
        """Run the legacy tag-encoding migration once per database.

        Gated on its own marker in ``schema_migrations`` instead of the
        schema version: the old ``user_version < 1`` gate was unreachable
        for every database that had ever had a version written to it, so
        a legacy library kept its broken ``\\uXXXX`` tags forever. The
        marker makes the (expensive) re-encode run exactly once, for
        every legacy database, and never again.
        """
        name = 'tag_encoding'
        if self._migration_applied(conn, name):
            return
        self._migrate_tag_encoding(conn)
        self._mark_migration_applied(conn, name)
        logger.info("Applied the legacy tag-encoding migration")

    def _run_body_text_backfill(self, conn: sqlite3.Connection) -> None:
        """Fill the searchable body of cards stored before the column existed.

        The cards that predate the column carry no body, so the full-text
        search could not find anything in them. The work is one-off and
        bounded: it runs for at most :data:`_BODY_BACKFILL_SECONDS` per
        launch (and remembers where it stopped), so a large library is
        filled over a few starts instead of freezing the app on the first
        one. Cards imported or edited later get their body written
        directly.
        """
        if self._migration_applied(conn, 'body_text'):
            return
        if 'body_text' not in self._character_columns(conn):
            return
        cursor = self._migration_value(conn, 'body_text_cursor')
        last_id = int(cursor) if cursor.isdigit() else 0
        rows = conn.execute(
            "SELECT id, source_path FROM characters "
            "WHERE id > ? AND (body_text IS NULL OR body_text = '') ORDER BY id",
            (last_id,),
        ).fetchall()
        deadline = time.monotonic() + _BODY_BACKFILL_SECONDS
        examined = 0
        for char_id, source_path in rows:
            if examined >= _BODY_BACKFILL_BATCH or time.monotonic() > deadline:
                # More to do: come back on the next launch.
                self._mark_migration_applied(conn, 'body_text_cursor', str(last_id))
                if examined:
                    logger.info(
                        "Filled the searchable body of %s card(s); the rest follows",
                        examined,
                    )
                return
            last_id = int(char_id)
            examined += 1
            if not source_path or not Path(source_path).exists():
                continue
            try:
                raw = read_card_data(source_path)
                if raw is None:
                    continue
                card = CharacterCard.from_spec_dict(raw, str(source_path))
            except Exception:
                logger.debug("Could not read card %s for the body backfill", char_id)
                continue
            body = _card_body_text(card)
            if not body:
                continue
            conn.execute(
                'UPDATE characters SET body_text = ? WHERE id = ?',
                (body, char_id),
            )
        # Everything that could be read has been filled.
        self._mark_migration_applied(conn, 'body_text_cursor', str(last_id))
        self._mark_migration_applied(conn, 'body_text', '1')
        if examined:
            logger.info("Filled the searchable body of %s card(s)", examined)

    @staticmethod
    def _character_columns(conn: sqlite3.Connection) -> set[str]:
        """Return the column names of the ``characters`` table."""
        return {
            row[1] for row in conn.execute('PRAGMA table_info(characters)').fetchall()
        }

    def add_card(self, card: CharacterCard) -> int:
        source_path = Path(card.source_path).resolve()
        if not card.source_path or not source_path.exists():
            raise FileNotFoundError(f"Source path not found: {card.source_path}")

        lib_dir = _get_library_dir()
        ext = source_path.suffix
        safe_name = _sanitize_filename(card.name)
        dest_name = f"{safe_name}_{uuid.uuid4().hex[:8]}{ext}"
        dest_path = lib_dir / dest_name
        vault.import_external(source_path, dest_path)

        thumb_dir = _get_thumbnail_dir()
        thumb_name = f"{dest_path.stem}_thumb.png"
        thumb_path = thumb_dir / thumb_name
        # Declared before the try: an exception that is neither OSError nor
        # VaultError used to escape the inner handler and then hit an
        # UnboundLocalError here, which masked the real error and leaked the
        # library copy that vault.import_external already made.
        thumb_path_str = None
        try:
            if save_thumbnail(str(dest_path), str(thumb_path)):
                thumb_path_str = str(thumb_path)
            else:
                logger.warning("Failed to generate thumbnail for '%s'; storing null path", card.name)
                thumb_path_str = None
        except Exception as e:
            # A thumbnail is optional; an unwritable thumbnail directory
            # must not fail the whole import. VaultError matters too: an
            # encrypted-library failure here must not orphan the copy that
            # ``vault.import_external`` already made above.
            logger.warning("Thumbnail write failed for '%s': %s", card.name, e)
            thumb_path_str = None

        try:
            with self._conn() as conn:
                cursor = conn.execute('''
                    INSERT INTO characters
                        (name, source_path, thumbnail_path, tags, creator,
                         description_preview, creator_notes, token_count, spec_version,
                         is_favorite, create_date, body_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    card.name,
                    str(dest_path),
                    thumb_path_str,
                    # ensure_ascii=False so non-ASCII tags are stored as
                    # literal text — the LIKE-based tag filter matches the
                    # user-visible string, not its \uXXXX-escaped form.
                    json.dumps(card.tags, ensure_ascii=False),
                    card.creator,
                    card.description[:500] if card.description else '',
                    card.creator_notes[:1000] if card.creator_notes else '',
                    card.token_count,
                    card.spec_version,
                    int(card.fav),
                    card.create_date,
                    # The full body, for the optional full-text search.
                    _card_body_text(card),
                ))
                char_id = cursor.lastrowid
            logger.info("Added card '%s' (ID: %s)", card.name, char_id)
            return char_id
        except Exception:
            # Don't leak orphan library/thumbnail files when the insert fails.
            try:
                dest_path.unlink(missing_ok=True)
                if thumb_path_str:
                    Path(thumb_path_str).unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _sort_order(self, sort_by: str) -> str:
        """Return a SQL ORDER BY clause for the given sort key.

        Falls back to the default (favorites first, then name) when the key is
        not whitelisted, which guards against SQL injection via sort columns.
        """
        if sort_by not in self._SORT_WHITELIST:
            sort_by = 'name'
        if sort_by == 'name':
            return 'is_favorite DESC, name COLLATE NOCASE'
        if sort_by == 'random':
            # Fresh shuffle on every query.
            return 'RANDOM()'
        return f'{sort_by} DESC, name COLLATE NOCASE'

    def get_all(self, sort_by: str = 'name', favorites_only: bool = False,
                light: bool = False) -> list[dict]:
        """Every card, optionally without the searchable body.

        *light* drops the ``body_text`` column, which holds the whole card
        (description, prompt fields, creator notes and the character book).
        Loading it for every card made a library-wide listing - the shared
        sidebar, the statistics dashboard, the Doctor - pull megabytes of
        text it never looked at.
        """
        order = self._sort_order(sort_by)
        where = 'WHERE is_favorite = 1' if favorites_only else ''
        columns = '*' if not light else _CARD_LIST_COLUMNS
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f'SELECT {columns} FROM characters {where} ORDER BY {order}',
            ).fetchall()
            return [dict(r) for r in rows]

    def get_library_stats(self) -> dict:
        """Return lightweight aggregate counts for the status bar.

        Uses COUNT/SUM queries rather than loading every row so it stays cheap
        even with a large library.
        """
        with self._conn() as conn:
            row = conn.execute(
                'SELECT COUNT(*) AS count, COALESCE(SUM(token_count), 0) AS tokens, '
                'COALESCE(SUM(is_favorite), 0) AS favorites FROM characters',
            ).fetchone()
            return {'count': row[0], 'tokens': row[1], 'favorites': row[2]}

    def get_detailed_stats(self) -> dict:
        """Return comprehensive aggregate statistics for the stats dashboard.

        Uses a single query so it stays cheap even with a large library.
        """
        with self._conn() as conn:
            row = conn.execute(
                'SELECT COUNT(*) AS count, '
                'COALESCE(SUM(token_count), 0) AS total_tokens, '
                'COALESCE(AVG(token_count), 0) AS avg_tokens, '
                'COALESCE(MIN(token_count), 0) AS min_tokens, '
                'COALESCE(MAX(token_count), 0) AS max_tokens, '
                'COALESCE(SUM(is_favorite), 0) AS favorites '
                'FROM characters',
            ).fetchone()
            return {
                'count': row[0],
                'total_tokens': row[1],
                'avg_tokens': round(row[2], 1),
                'min_tokens': row[3],
                'max_tokens': row[4],
                'favorites': row[5],
            }

    def get_creator_counts(self) -> list[tuple[str, int]]:
        """Return ``(creator, count)`` pairs sorted by count descending."""
        with self._conn() as conn:
            # GROUP BY the *projected* expression: grouping by the raw column
            # makes NULL and '' two groups that are both labelled 'Unknown'.
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(creator, ''), 'Unknown') AS creator, "
                'COUNT(*) AS cnt FROM characters '
                "GROUP BY COALESCE(NULLIF(creator, ''), 'Unknown') "
                'ORDER BY cnt DESC, creator ASC',
            ).fetchall()
            return [(row[0], row[1]) for row in rows]

    def get_spec_version_counts(self) -> dict[str, int]:
        """Return a mapping of spec_version -> card count."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(spec_version, ''), 'Unknown') AS ver, "
                'COUNT(*) AS cnt FROM characters '
                "GROUP BY COALESCE(NULLIF(spec_version, ''), 'Unknown') "
                'ORDER BY cnt DESC',
            ).fetchall()
            return {row[0]: row[1] for row in rows}

    def get_cards_per_week(self) -> list[tuple[str, int]]:
        """Return ``(week, count)`` pairs for cards added per week, oldest first.

        Week is formatted as ``YYYY-Www`` (true ISO week: ``%G``/``%V``, so
        a card added on Dec 31 belongs to week 1 of the *next* year rather
        than being mis-binned under the calendar year).
        """
        with self._conn() as conn:
            # COALESCE the label too: strftime returns NULL for an
            # unparseable/NULL date_added, which the stats chart can't render.
            rows = conn.execute(
                "SELECT COALESCE(strftime('%G-W%V', date_added), 'Unknown') AS week, "
                'COUNT(*) AS cnt FROM characters GROUP BY week ORDER BY week ASC',
            ).fetchall()
            return [(row[0], row[1]) for row in rows]

    def search(
        self,
        query: str = '',
        tags: Optional[list[str]] = None,
        sort_by: str = 'name',
        favorites_only: bool = False,
        min_rating: int = 0,
        collection_id: Optional[int] = None,
        full_text: bool = False,
        fuzzy: bool = False,
    ) -> list[dict]:
        """Search the library.

        *query* is matched against the name, the description preview, the
        tags, the creator and the creator notes.

        *full_text* additionally matches the full body of the card (the
        description, the prompt fields, the creator notes and the
        character book) through the ``body_text`` column. It is off by
        default, and the column is only used when the database actually
        has it.

        *fuzzy* additionally keeps the rows whose name, creator or one of
        their tags matches every word of the query (see
        :func:`fuzzy_match`), so the word order does not matter.

        Both extra modes are one extra SQL clause - no query per row.
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            columns = self._character_columns(conn)
            clauses = []
            params = []
            if favorites_only:
                clauses.append('is_favorite = 1')
            if min_rating and min_rating > 0:
                clauses.append('rating >= ?')
                params.append(int(min_rating))
            if collection_id is not None:
                if collection_id < 0:
                    # Sentinel: cards that belong to no collection at all.
                    clauses.append(
                        'id NOT IN (SELECT character_id FROM card_collections)'
                    )
                else:
                    clauses.append(
                        'id IN (SELECT character_id FROM card_collections '
                        'WHERE collection_id = ?)'
                    )
                    params.append(int(collection_id))
            if query:
                # Escape LIKE metacharacters so a query of "%" doesn't match
                # every row.
                q = f"%{_escape_like(query)}%"
                text_clauses = [
                    ("name LIKE ? ESCAPE '\\'", q),
                    ("description_preview LIKE ? ESCAPE '\\'", q),
                    # The tags are compared as decoded values, not as raw
                    # JSON text: a tag containing a quote or a backslash is
                    # stored escaped and could never match itself.
                    ("st_tags_contain(tags, ?) = 1", query),
                    ("creator LIKE ? ESCAPE '\\'", q),
                    ("creator_notes LIKE ? ESCAPE '\\'", q),
                ]
                if full_text and 'body_text' in columns:
                    # The full body of the card, not just its preview.
                    text_clauses.append(("body_text LIKE ? ESCAPE '\\'", q))
                if fuzzy:
                    # Word-wise match: every word of the query has to
                    # appear in the name, the creator or one of the tags.
                    text_clauses.append((
                        '(st_fuzzy_match(name, ?) = 1 '
                        'OR st_fuzzy_match(creator, ?) = 1 '
                        'OR st_fuzzy_tags(tags, ?) = 1)',
                        (query, query, query),
                    ))
                clauses.append('(' + ' OR '.join(c for c, _p in text_clauses) + ')')
                for _c, p in text_clauses:
                    if isinstance(p, tuple):
                        params.extend(p)
                    else:
                        params.append(p)
            if tags:
                for tag in tags:
                    # Exact tag match: decode each card's tag list and compare
                    # values. Substring-matching the raw JSON blob cannot work for
                    # tags containing quotes/backslashes (the JSON escapes them),
                    # and SQLite's LIKE/lower only fold ASCII - so a tag like
                    # "École" would be listed but never match its own card.
                    clauses.append('st_has_tag(tags, ?) = 1')
                    params.append(tag)
            where = ' AND '.join(clauses) if clauses else '1=1'
            order = self._sort_order(sort_by)
            rows = conn.execute(
                f'SELECT * FROM characters WHERE {where} ORDER BY {order}',
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_by_id(self, char_id: int) -> Optional[dict]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT * FROM characters WHERE id = ?', (char_id,)).fetchone()
            return dict(row) if row else None

    def _rename_card_files(
        self, entry: dict, new_name: str,
    ) -> tuple[str, Optional[str]]:
        """Rename a card's library file (and its thumbnail) to match *new_name*.

        The random suffix is preserved when it already follows the 8-hex
        convention (``Aria_abc12345.png`` -> ``Aria Vale_abc12345.png``) and
        regenerated otherwise, keeping filenames unique. Returns the new
        (source_path, thumbnail_path); unchanged values are returned when
        there is nothing to rename (missing file, no thumbnail, ...).
        """
        source = entry.get('source_path') or ''
        src_path = Path(source)
        if not source or not src_path.exists():
            logger.warning(
                "Cannot rename file for card '%s': source missing (%s)",
                new_name, source or '<none>',
            )
            return source, entry.get('thumbnail_path')

        safe_name = _sanitize_filename(new_name)
        match = re.search(r'_([0-9a-f]{8})$', src_path.stem)
        suffix = match.group(1) if match else uuid.uuid4().hex[:8]
        new_path = src_path.parent / f"{safe_name}_{suffix}{src_path.suffix}"
        # A genuinely different existing file forces a fresh suffix; a
        # case-only difference renames in place (same file).
        while new_path.exists() and os.path.normcase(str(new_path)) != os.path.normcase(source):
            suffix = uuid.uuid4().hex[:8]
            new_path = src_path.parent / f"{safe_name}_{suffix}{src_path.suffix}"
        if os.path.normcase(str(new_path)) != os.path.normcase(source):
            src_path.rename(new_path)
            logger.info("Renamed card file %s -> %s", src_path.name, new_path.name)

        new_thumb = None
        old_thumb = entry.get('thumbnail_path')
        if old_thumb and Path(old_thumb).exists():
            thumb_path = Path(old_thumb)
            new_thumb_path = thumb_path.parent / f"{new_path.stem}_thumb.png"
            while (
                new_thumb_path.exists()
                and os.path.normcase(str(new_thumb_path)) != os.path.normcase(old_thumb)
            ):
                new_thumb_path = thumb_path.parent / f"{new_path.stem}_{uuid.uuid4().hex[:4]}_thumb.png"
            if os.path.normcase(str(new_thumb_path)) != os.path.normcase(old_thumb):
                try:
                    thumb_path.rename(new_thumb_path)
                except OSError as e:
                    # The source rename above already happened. Keep the old
                    # (still valid) thumbnail path and return the *actual*
                    # post-rename paths, so the caller's DB update never points
                    # at a file that no longer exists.
                    logger.warning(
                        "Could not rename thumbnail %s -> %s: %s",
                        thumb_path, new_thumb_path, e,
                    )
                    return str(new_path), old_thumb
                new_thumb = str(new_thumb_path)
            else:
                new_thumb = str(new_thumb_path)

        return str(new_path), new_thumb

    def rename_card_files(self, char_id: int) -> bool:
        """Rename a card's file + thumbnail to match its stored name.

        Applies the standard naming convention to cards whose file no
        longer reflects their name (e.g. renamed before the rename-on-save
        feature existed). No-op when the file already conforms or is
        missing. Returns True when a file was renamed.
        """
        entry = self.get_by_id(char_id)
        if entry is None:
            return False
        source = entry.get('source_path') or ''
        name = entry.get('name') or ''
        if not source or filename_matches_name(source, name):
            return False
        new_source, new_thumb = self._rename_card_files(entry, name)
        if new_source == source:
            return False
        with self._conn() as conn:
            conn.execute(
                'UPDATE characters SET source_path = ?, thumbnail_path = ? WHERE id = ?',
                (new_source, new_thumb, char_id),
            )
            conn.commit()
        logger.info("Aligned card file name for ID %s ('%s')", char_id, name)
        return True

    def update_card(
        self,
        char_id: int,
        card: CharacterCard,
        regen_thumbnail: bool = False,
        skip_file_write: bool = False,
        rename_file_on_name_change: bool = False,
    ) -> None:
        """Update a card's metadata and (optionally) its source PNG.

        When *skip_file_write* is True the source PNG is left untouched —
        used by the SillyTavern pull operation which copies the ST PNG
        directly (avoiding a re-serialization that would break the
        content-hash baseline).

        When *rename_file_on_name_change* is True and the card's name
        changed, the library file and thumbnail are renamed to match the
        new name (source_path/thumbnail_path are updated in place). The
        database row keeps its id, so chat sessions, links and sync
        state are unaffected. A failed rename is logged and does not
        abort the save.
        """
        entry = self.get_by_id(char_id)
        if entry is None:
            raise ValueError(f"Character ID {char_id} not found")

        source = entry.get('source_path', '')
        if not skip_file_write:
            if source and Path(source).exists():
                from src.card_parser import write_chara_card_dual
                write_chara_card_dual(source, source, card.to_spec_dict())
            else:
                logger.warning("Source file missing for card ID %s, skipping file write", char_id)

        # The content update is committed *before* the files are renamed.
        # The other order left the row pointing at the pre-rename file name
        # whenever the UPDATE failed (a locked database, a full disk, a
        # crash): the card's file was gone and the Doctor reported it as a
        # ghost.
        self._update_card_row(char_id, card)

        # Rename the backing files when the (editable) name changed.
        new_source = source
        new_thumb = entry.get('thumbnail_path')
        renamed = False
        if rename_file_on_name_change and card.name and card.name != entry.get('name', ''):
            try:
                new_source, new_thumb = self._rename_card_files(entry, card.name)
                renamed = new_source != source
            except OSError as e:
                # The content was already written to the old file — keep
                # the old paths so the save still succeeds.
                logger.error("Failed to rename card file for ID %s: %s", char_id, e)

        if regen_thumbnail:
            thumb_path = new_thumb
            if thumb_path and new_source and Path(new_source).exists():
                try:
                    save_thumbnail(new_source, thumb_path)
                except (OSError, vault.VaultError) as e:
                    # A thumbnail is optional: an unwritable thumbnail
                    # directory must not abort the UPDATE below, or a rename
                    # that already happened would leave the row pointing at a
                    # file name that no longer exists.
                    logger.warning(
                        "Thumbnail regeneration failed for ID %s: %s", char_id, e,
                    )

        if renamed:
            with self._conn() as conn:
                conn.execute(
                    'UPDATE characters SET source_path = ?, thumbnail_path = ? '
                    'WHERE id = ?',
                    (new_source, new_thumb, char_id),
                )
                conn.commit()
        logger.info("Updated card ID %s ('%s')", char_id, card.name)

    def _update_card_row(self, char_id: int, card: CharacterCard) -> None:
        """Write the card's metadata into its database row."""
        with self._conn() as conn:
            sets = '''
                UPDATE characters SET
                    name = ?, tags = ?, creator = ?,
                    description_preview = ?, creator_notes = ?,
                    token_count = ?,
                    spec_version = ?, is_favorite = ?,
                    body_text = ?,
                    date_modified = CURRENT_TIMESTAMP
            '''
            params: list = [
                card.name,
                json.dumps(card.tags, ensure_ascii=False),
                card.creator,
                card.description[:500] if card.description else '',
                card.creator_notes[:1000] if card.creator_notes else '',
                card.token_count,
                card.spec_version,
                int(card.fav),
                # The full body, for the optional full-text search.
                _card_body_text(card),
            ]
            sets += ' WHERE id = ?'
            params.append(char_id)
            conn.execute(sets, params)
            conn.commit()

    def update_card_fields(self, char_id: int, fields: dict) -> bool:
        """Update a subset of a card's fields.

        Used by the batch-edit tool: the card is read from its file, the
        given fields are replaced, and the card is written back.  Reading the
        file first is what makes fields that only exist in the PNG
        (``system_prompt``, ``character_version``, ``description``, ...) work
        the same way as the ones stored in the database row.

        Args:
            char_id: the card to update.
            fields: a mapping of field name to new value.  ``tags`` takes a
                list of strings; every other field takes a string.

        Returns:
            True when the card was updated, False when it could not be read
            or the field is unknown.
        """
        if not fields:
            return False
        entry = self.get_by_id(char_id)
        if entry is None:
            return False
        from src.card_models import CharacterCard
        from src.card_parser import read_card_data

        source = entry.get('source_path', '')
        data = None
        if source:
            try:
                data = read_card_data(source)
            except Exception:
                logger.exception("Could not read card %s for a field update", char_id)
        card = CharacterCard()
        if isinstance(data, dict):
            inner = data.get('data') if isinstance(data.get('data'), dict) else data
            for key, value in inner.items():
                if hasattr(card, key):
                    try:
                        setattr(card, key, value)
                    except Exception:
                        pass
        # Fall back to the database row for the fields it does hold.
        card.name = card.name or entry.get('name', '')
        card.creator = card.creator or entry.get('creator', '')
        card.creator_notes = card.creator_notes or entry.get('creator_notes', '')
        card.description = card.description or entry.get('description_preview', '')

        changed = False
        for key, value in fields.items():
            if key == 'tags':
                if isinstance(value, str):
                    value = [t.strip() for t in value.split(',') if t.strip()]
                if not isinstance(value, list):
                    continue
                card.tags = list(value)
                changed = True
                continue
            if not hasattr(card, key):
                logger.warning("Ignoring an unknown card field %r", key)
                continue
            if isinstance(value, (list, tuple)):
                value = ', '.join(str(v) for v in value)
            setattr(card, key, str(value))
            changed = True
        if not changed:
            return False
        try:
            self.update_card(char_id, card, regen_thumbnail=False)
        except Exception:
            logger.exception("Could not update the fields of card %s", char_id)
            return False
        return True

    def remove_card(
        self,
        char_id: int,
        delete_files: bool = True,
        record_tombstone: bool = True,
        delete_sessions: Optional[bool] = None,
        preserve_related: bool = False,
    ) -> None:
        """Remove a card row (and, by default, its files and chat sessions).

        *preserve_related* keeps the gallery metadata rows and the collection
        memberships.  It exists for the Library Doctor: when a card's PNG has
        disappeared, the row is a ghost, but the gallery images and the chat
        history are independent assets that belong to the user and must not be
        cascaded away with the row.
        """
        entry = self.get_by_id(char_id)
        if entry is None:
            return
        # If the card was linked to a SillyTavern file, remember the
        # deletion (a "tombstone") so the next sync deletes the ST copy
        # instead of treating it as a new card to import. Cards deleted
        # after being unlinked — or never linked — leave no tombstone:
        # unlinking first remains the way to remove a card locally while
        # keeping the ST copy.
        st_url = entry.get('st_avatar_url')
        # One transaction for the tombstone, the join rows and the card row.
        # They were separate connections, so a failure between them left a
        # tombstone for a card still in the library - and the next sync would
        # then delete the live SillyTavern file.
        with self._conn() as conn:
            if record_tombstone and st_url:
                try:
                    self._record_deleted_st_card(
                        conn, st_url,
                        name=entry.get('name', ''),
                        creator=entry.get('creator', ''),
                        sync_hash=entry.get('st_sync_hash'),
                    )
                except Exception:
                    # A tombstone that half-exists alongside a live row would
                    # make the next sync delete the ST file for a card that is
                    # still in the library, so the delete is aborted (and the
                    # transaction rolled back) rather than committed without it.
                    logger.exception(
                        "Could not record ST deletion tombstone for card %s; "
                        "aborting delete to avoid an inconsistent sync state",
                        char_id,
                    )
                    raise
            # Delete the DB row FIRST: if file deletion then fails, we're left
            # with harmless orphan files (cleanable) instead of a ghost row
            # pointing at deleted files.
            if not preserve_related:
                conn.execute('DELETE FROM card_collections WHERE character_id = ?', (char_id,))
                conn.execute('DELETE FROM gallery_images WHERE character_id = ?', (char_id,))
            conn.execute('DELETE FROM characters WHERE id = ?', (char_id,))
        logger.info("Removed card ID %s", char_id)
        # Chat sessions live on disk under sessions/<char_id>/. They are
        # removed together with the card's files (they are unreachable once
        # the card is gone), but a caller that keeps the files — e.g. the sync
        # path that migrates a superseded card to a new id — must keep the
        # sessions too, so deletion is gated separately from ``delete_files``.
        if delete_sessions is None:
            delete_sessions = delete_files
        if delete_sessions:
            self._delete_chat_sessions(char_id)
        if delete_files:
            # Gallery images live on disk under gallery/<char_id>/ (metadata
            # rows are gone already); like the sessions they are unreachable
            # once the card is removed.
            try:
                from src import gallery_store
                gallery_store.delete_gallery_dir(char_id)
            except Exception:
                logger.exception("Could not delete gallery folder for card %s", char_id)
            src_path = entry.get('source_path', '')
            if src_path:
                src = Path(src_path)
                if src.exists() and src.is_file():
                    try:
                        src.unlink()
                    except OSError as e:
                        logger.warning("Could not delete %s: %s", src, e)
            thumb_path = entry.get('thumbnail_path', '')
            if thumb_path:
                thumb = Path(thumb_path)
                if thumb.exists() and thumb.is_file():
                    try:
                        thumb.unlink()
                    except OSError as e:
                        logger.warning("Could not delete %s: %s", thumb, e)

    def _iter_tag_blobs(self):
        """Yield the raw ``tags`` column of every character row."""
        with self._conn() as conn:
            yield from conn.execute('SELECT tags FROM characters').fetchall()

    def get_all_tags(self) -> list[str]:
        tags: set[str] = set()
        for tags_json, in self._iter_tag_blobs():
            for t in _decode_tag_blob(tags_json):
                n = normalize_tag(t)
                if n:
                    tags.add(n)
        return sorted(tags)

    def get_tag_counts(self) -> dict[str, int]:
        """Return a mapping of normalized tag -> number of cards using it.

        Uses the pure ``count_tags`` helper so the aggregation logic is
        unit-tested without a database.
        """
        parsed: list[list] = [_decode_tag_blob(blob) for blob, in self._iter_tag_blobs()]
        return count_tags(parsed)

    def _rewrite_tags(self, transform, conn: sqlite3.Connection) -> int:
        """Apply *transform* (a callable list[str] -> list[str]) to every
        card's tags in a single transaction.

        Returns the number of cards whose tag list actually changed.
        """
        conn.row_factory = sqlite3.Row
        # The read has to be part of the write transaction: with the default
        # isolation level, python's sqlite3 only opens a transaction on DML,
        # so a card saved by another thread between the SELECT and the
        # UPDATE would have its tags silently overwritten.
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute('SELECT id, tags FROM characters').fetchall()
        changed = 0
        for row in rows:
            char_id = row['id']
            tags_json = row['tags']
            current: list[str] = []
            if tags_json:
                try:
                    parsed = safe_json.loads_safe(tags_json)
                    if isinstance(parsed, list):
                        current = [str(t) for t in parsed]
                except (json.JSONDecodeError, TypeError, ValueError, OverflowError):
                    current = []
            new_tags = transform(current)
            # Compare the *normalised* sets: a transform that only changes
            # the spelling (or the order) of the tags has not changed the
            # card, and writing it back would touch every card in the
            # library for nothing.
            if _tag_set(new_tags) == _tag_set(current):
                continue
            conn.execute(
                'UPDATE characters SET tags = ?, date_modified = CURRENT_TIMESTAMP WHERE id = ?',
                (json.dumps(new_tags, ensure_ascii=False), char_id),
            )
            changed += 1
        return changed

    def rename_tag_all(self, old: str, new: str) -> int:
        """Rename *old* tag to *new* across every card. Returns changed count."""
        old_n = normalize_tag(old)
        new_n = normalize_tag(new)
        if not old_n or not new_n:
            return 0
        with self._conn() as conn:
            changed = self._rewrite_tags(lambda t: rename_tag(t, old_n, new_n), conn)
            conn.commit()
        if changed:
            logger.info("Renamed tag '%s' -> '%s' on %d card(s)", old_n, new_n, changed)
        return changed

    def merge_tag_all(self, source: str, target: str) -> int:
        """Merge *source* tag into *target* across every card. Returns changed count."""
        source_n = normalize_tag(source)
        target_n = normalize_tag(target)
        if not source_n or not target_n or source_n == target_n:
            return 0
        with self._conn() as conn:
            changed = self._rewrite_tags(lambda t: merge_tag(t, source_n, target_n), conn)
            conn.commit()
        if changed:
            logger.info("Merged tag '%s' into '%s' on %d card(s)", source_n, target_n, changed)
        return changed

    def delete_tag_all(self, tag: str) -> int:
        """Remove *tag* from every card. Returns changed count."""
        tag_n = normalize_tag(tag)
        if not tag_n:
            return 0
        with self._conn() as conn:
            changed = self._rewrite_tags(lambda t: remove_tag(t, tag_n), conn)
            conn.commit()
        if changed:
            logger.info("Deleted tag '%s' from %d card(s)", tag_n, changed)
        return changed

    def toggle_favorite(self, char_id: int) -> bool:
        """Toggle favorite status. Returns the new favorite state."""
        # Single atomic read-modify-write: doing SELECT-then-UPDATE across
        # two connections could lose a toggle under concurrent access.
        with self._conn() as conn:
            cursor = conn.execute('''
                UPDATE characters
                SET is_favorite = 1 - is_favorite,
                    date_modified = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (char_id,))
            if cursor.rowcount == 0:
                raise ValueError(f"Character ID {char_id} not found")
            row = conn.execute(
                'SELECT is_favorite FROM characters WHERE id = ?', (char_id,),
            ).fetchone()
        new_val = bool(row[0])
        logger.info("Toggled favorite for card ID %s -> %s", char_id, new_val)
        return new_val

    def set_favorite(self, char_id: int, favorite: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                'UPDATE characters SET is_favorite = ?, date_modified = CURRENT_TIMESTAMP WHERE id = ?',
                (int(favorite), char_id),
            )
            conn.commit()

    # ---- Ratings / user notes / collections ----

    def set_rating(self, char_id: int, rating: int) -> None:
        """Store a 0-5 star rating (0 clears the rating)."""
        value = max(0, min(5, int(rating)))
        with self._conn() as conn:
            conn.execute(
                'UPDATE characters SET rating = ?, date_modified = CURRENT_TIMESTAMP WHERE id = ?',
                (value, char_id),
            )
            conn.commit()

    def set_user_notes(self, char_id: int, notes: str) -> None:
        """Store the user's private notes for a card (DB-only, not in PNG)."""
        with self._conn() as conn:
            conn.execute(
                'UPDATE characters SET user_notes = ?, date_modified = CURRENT_TIMESTAMP WHERE id = ?',
                (notes or '', char_id),
            )
            conn.commit()

    def list_collections(self) -> list[dict]:
        """Return all collections ordered by name, with member counts."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT c.id, c.name,
                       COUNT(cc.character_id) AS card_count
                FROM collections c
                LEFT JOIN card_collections cc ON cc.collection_id = c.id
                GROUP BY c.id
                ORDER BY c.name COLLATE NOCASE
            ''').fetchall()
            return [dict(r) for r in rows]

    def create_collection(self, name: str) -> int:
        clean = (name or '').strip()
        if not clean:
            raise ValueError("Collection name cannot be empty")
        with self._conn() as conn:
            cursor = conn.execute(
                'INSERT INTO collections (name) VALUES (?)', (clean,),
            )
            new_id = cursor.lastrowid
        logger.info("Created collection '%s' (ID: %s)", clean, new_id)
        return new_id

    def rename_collection(self, collection_id: int, new_name: str) -> None:
        clean = (new_name or '').strip()
        if not clean:
            raise ValueError("Collection name cannot be empty")
        with self._conn() as conn:
            conn.execute(
                'UPDATE collections SET name = ? WHERE id = ?',
                (clean, collection_id),
            )
            conn.commit()

    def delete_collection(self, collection_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                'DELETE FROM card_collections WHERE collection_id = ?',
                (collection_id,),
            )
            conn.execute('DELETE FROM collections WHERE id = ?', (collection_id,))
            conn.commit()

    def get_card_collection_ids(self, char_id: int) -> list[int]:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT collection_id FROM card_collections WHERE character_id = ?',
                (char_id,),
            ).fetchall()
            return [r[0] for r in rows]

    def get_collection_membership(self) -> dict[int, set[int]]:
        """Return {character_id: {collection_id, ...}} for every card.

        One query for the whole map, so the sidebar (which filters its entire
        listing by collection) does not open a connection per card.
        """
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT character_id, collection_id FROM card_collections',
            ).fetchall()
        membership: dict[int, set[int]] = {}
        for char_id, collection_id in rows:
            membership.setdefault(int(char_id), set()).add(int(collection_id))
        return membership

    def add_to_collection(self, char_id: int, collection_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO card_collections (character_id, collection_id) '
                'VALUES (?, ?)',
                (char_id, collection_id),
            )
            conn.commit()

    def remove_from_collection(self, char_id: int, collection_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                'DELETE FROM card_collections '
                'WHERE character_id = ? AND collection_id = ?',
                (char_id, collection_id),
            )
            conn.commit()

    def set_card_collections(self, char_id: int, collection_ids: list[int]) -> None:
        """Replace a card's collection memberships with *collection_ids*."""
        wanted = {int(c) for c in collection_ids or []}
        with self._conn() as conn:
            conn.execute(
                'DELETE FROM card_collections WHERE character_id = ?', (char_id,),
            )
            conn.executemany(
                'INSERT OR IGNORE INTO card_collections (character_id, collection_id) '
                'VALUES (?, ?)',
                [(char_id, cid) for cid in sorted(wanted)],
            )
            conn.commit()

    # ---- Gallery images ----

    def list_gallery_images(self, char_id: int) -> list[dict]:
        """Return a card's gallery image rows in display order."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT id, character_id, file_name, caption, sort_order, '
                'content_hash, created_at FROM gallery_images '
                'WHERE character_id = ? ORDER BY sort_order, id',
                (char_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_gallery_image(self, image_id: int) -> Optional[dict]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                'SELECT id, character_id, file_name, caption, sort_order, '
                'content_hash, created_at FROM gallery_images WHERE id = ?',
                (image_id,),
            ).fetchone()
            return dict(row) if row else None

    def find_gallery_by_hash(self, char_id: int, content_hash: str) -> Optional[dict]:
        """Return the gallery row already holding *content_hash*, if any.

        Import dedupe: identical bytes are never stored twice for the same
        character.
        """
        if not content_hash:
            return None
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                'SELECT id, character_id, file_name, caption, sort_order, '
                'content_hash, created_at FROM gallery_images '
                'WHERE character_id = ? AND content_hash = ?',
                (char_id, content_hash),
            ).fetchone()
            return dict(row) if row else None

    def add_gallery_image(
        self, char_id: int, file_name: str, content_hash: str = '', caption: str = '',
    ) -> int:
        """Insert a gallery row; new images append at the end of the order."""
        from src.fs_utils import safe_filename_component
        safe_name = safe_filename_component(file_name)
        if not safe_name:
            raise ValueError(f'Invalid gallery file name: {file_name!r}')
        with self._conn() as conn:
            row = conn.execute(
                'SELECT COALESCE(MAX(sort_order), -1) + 1 FROM gallery_images '
                'WHERE character_id = ?',
                (char_id,),
            ).fetchone()
            next_order = int(row[0]) if row else 0
            cursor = conn.execute(
                'INSERT INTO gallery_images '
                '(character_id, file_name, caption, sort_order, content_hash) '
                'VALUES (?, ?, ?, ?, ?)',
                (char_id, safe_name, caption or '', next_order, content_hash or ''),
            )
            new_id = cursor.lastrowid
            conn.commit()
        return new_id

    def set_gallery_caption(self, image_id: int, caption: str) -> None:
        with self._conn() as conn:
            conn.execute(
                'UPDATE gallery_images SET caption = ? WHERE id = ?',
                (caption or '', image_id),
            )
            conn.commit()

    def set_gallery_order(self, char_id: int, ordered_ids: list[int]) -> None:
        """Persist a drag-reordered gallery sequence.

        The order is rewritten wholesale: the ids in *ordered_ids* come
        first, in the given order, and every row the caller did not
        mention follows them, keeping their previous relative order (by
        their current ``sort_order``, then by id). The result is a total
        order, so no two rows of a character can share a ``sort_order``.
        """
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT id FROM gallery_images WHERE character_id = ? '
                'ORDER BY sort_order, id',
                (char_id,),
            ).fetchall()
            own = {int(row[0]) for row in rows}
            ordered: list[int] = []
            seen: set[int] = set()
            for image_id in ordered_ids or []:
                try:
                    image_id = int(image_id)
                except (TypeError, ValueError):
                    continue
                # Ids of other characters (or duplicates) must not
                # consume a position: they would leave a gap in the
                # order and shift every following row.
                if image_id in own and image_id not in seen:
                    seen.add(image_id)
                    ordered.append(image_id)
            # Rows the caller did not mention keep their relative order
            # and are placed after the listed ones.
            ordered.extend(int(row[0]) for row in rows if int(row[0]) not in seen)
            for position, image_id in enumerate(ordered):
                conn.execute(
                    'UPDATE gallery_images SET sort_order = ? '
                    'WHERE id = ? AND character_id = ?',
                    (position, image_id, char_id),
                )
            conn.commit()

    def delete_gallery_image(self, image_id: int) -> Optional[dict]:
        """Remove one gallery row and return it (for the caller's file delete)."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                'SELECT id, character_id, file_name FROM gallery_images WHERE id = ?',
                (image_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute('DELETE FROM gallery_images WHERE id = ?', (image_id,))
            conn.commit()
            return dict(row)

    def delete_gallery_rows(self, char_id: int) -> None:
        with self._conn() as conn:
            conn.execute('DELETE FROM gallery_images WHERE character_id = ?', (char_id,))
            conn.commit()

    def list_all_gallery_images(self) -> list[dict]:
        """Every gallery row across all characters (Doctor scans)."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT id, character_id, file_name, caption, sort_order, '
                'content_hash, created_at FROM gallery_images '
                'ORDER BY character_id, sort_order, id',
            ).fetchall()
            return [dict(r) for r in rows]

    def migrate_gallery_images(self, old_id: int, new_id: int) -> None:
        """Re-point gallery rows from *old_id* to *new_id*.

        Used when a card is superseded during sync and re-imported under a
        new row id; the gallery belongs to the character and must follow it
        (files move separately via gallery_store.migrate_gallery_files).
        """
        if old_id == new_id:
            return
        with self._conn() as conn:
            # A row of the target card may already use the same file name;
            # the UNIQUE(character_id, file_name) constraint would then make
            # the bulk update fail and lose the whole gallery.  The
            # conflicting rows are renamed first.
            rows = conn.execute(
                'SELECT id, file_name FROM gallery_images WHERE character_id = ?',
                (new_id,),
            ).fetchall()
            taken = {r[1] for r in rows}
            for row in conn.execute(
                'SELECT id, file_name FROM gallery_images WHERE character_id = ?',
                (old_id,),
            ).fetchall():
                if row[1] in taken:
                    unique = _unique_gallery_name(row[1], taken)
                    conn.execute(
                        'UPDATE gallery_images SET file_name = ? WHERE id = ?',
                        (unique, row[0]),
                    )
                    taken.add(unique)
            conn.execute(
                'UPDATE gallery_images SET character_id = ? WHERE character_id = ?',
                (new_id, old_id),
            )
            conn.commit()

    def rename_gallery_files(self, char_id: int, renames: dict) -> int:
        """Point the gallery rows at the renamed files.

        The gallery files are renamed on disk when their name collides with
        one already in the target folder; the rows that name them have to
        follow, or the card shows the *other* card's image.
        """
        if not renames:
            return 0
        changed = 0
        with self._conn() as conn:
            for old_name, new_name in renames.items():
                try:
                    cursor = conn.execute(
                        'UPDATE gallery_images SET file_name = ? '
                        'WHERE character_id = ? AND file_name = ?',
                        (new_name, char_id, old_name),
                    )
                    changed += cursor.rowcount or 0
                except sqlite3.IntegrityError:
                    # Another row already uses the new name: leave this one
                    # alone rather than losing the row.
                    logger.warning(
                        "Could not rename the gallery file '%s' to '%s': the "
                        "name is already taken", old_name, new_name,
                    )
            conn.commit()
        return changed

    # ---- SillyTavern linking ----

    def _record_deleted_st_card(
        self, conn: sqlite3.Connection, avatar_url: str,
        name: str = '', creator: str = '', sync_hash: Optional[str] = None,
    ) -> None:
        """INSERT OR REPLACE a tombstone on an existing connection."""
        conn.execute(
            'INSERT OR REPLACE INTO deleted_st_cards '
            '(st_avatar_url, name, creator, st_sync_hash, deleted_at) '
            'VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)',
            (os.path.normcase(avatar_url), name, creator, sync_hash),
        )

    def _clear_deleted_st_card(self, conn: sqlite3.Connection, avatar_url: str) -> None:
        """Delete a tombstone on an existing connection.

        The stored key is already case-normalised (see
        :meth:`_record_deleted_st_card`), and normalising is idempotent,
        so the row can be addressed directly through the primary key
        index instead of scanning the whole table and comparing in
        Python on every link/unlink.
        """
        conn.execute(
            'DELETE FROM deleted_st_cards WHERE st_avatar_url = ?',
            (os.path.normcase(avatar_url),),
        )

    def migrate_chat_sessions(self, old_id: int, new_id: int) -> None:
        """Move ``sessions/<old_id>/`` to ``sessions/<new_id>/``.

        Used when a card is superseded during sync and re-imported under a new
        row id: the chat history belongs to the character, so it must follow
        the card rather than being deleted with the old row.
        """
        if old_id == new_id:
            return
        try:
            from src.app_paths import data_dir
            base = data_dir() / 'sessions'
            old_dir = base / str(old_id)
            new_dir = base / str(new_id)
        except Exception:
            logger.exception("Could not resolve the sessions directory")
            return
        if not old_dir.is_dir():
            return
        try:
            if not new_dir.exists():
                new_dir.parent.mkdir(parents=True, exist_ok=True)
                old_dir.rename(new_dir)
                return
            from src.fs_utils import move_contents_into
            renames = move_contents_into(old_dir, new_dir)
            shutil.rmtree(old_dir, ignore_errors=True)
            for old_name, new_name in renames:
                logger.warning(
                    "Chat session '%s' was merged as '%s' during the move %s -> %s",
                    old_name, new_name, old_dir, new_dir,
                )
        except OSError as e:
            logger.warning(
                "Could not migrate chat sessions from %s to %s: %s",
                old_dir, new_dir, e,
            )

    def _delete_chat_sessions(self, char_id: int) -> None:
        """Remove a deleted card's on-disk chat sessions.

        Sessions live in ``sessions/<char_id>/``; nothing else ever cleaned them
        up, so they grew without bound, were unreachable from the UI, and were
        still archived into every library backup.
        """
        try:
            from src.app_paths import data_dir
            session_dir = data_dir() / 'sessions' / str(char_id)
        except Exception:
            logger.exception("Could not resolve the sessions directory")
            return
        if not session_dir.is_dir():
            return
        try:
            shutil.rmtree(session_dir)
        except OSError as e:
            logger.warning("Could not delete chat sessions in %s: %s", session_dir, e)

    def link_to_st(self, char_id: int, avatar_url: str) -> None:
        """Link a card to a SillyTavern character file by its avatar URL."""
        # Link and tombstone-clear in ONE transaction: separate transactions
        # could leave a card linked *and* a "deleted on purpose" tombstone
        # behind, and the next sync would then delete the ST file the user had
        # just re-linked.
        with self._conn() as conn:
            conn.execute(
                'UPDATE characters SET st_avatar_url = ? WHERE id = ?',
                (avatar_url, char_id),
            )
            try:
                self._clear_deleted_st_card(conn, avatar_url)
            except Exception:
                logger.exception("Could not clear ST deletion tombstone for '%s'", avatar_url)
                raise
        logger.info("Linked card ID %s to ST '%s'", char_id, avatar_url)

    def unlink_from_st(self, char_id: int) -> None:
        """Remove the SillyTavern link and sync baseline from a card."""
        with self._conn() as conn:
            conn.execute(
                'UPDATE characters SET st_avatar_url = NULL, st_sync_hash = NULL WHERE id = ?',
                (char_id,),
            )
            conn.commit()
        logger.info("Unlinked card ID %s from SillyTavern", char_id)

    def mark_st_synced(self, char_id: int, sync_hash: str) -> None:
        """Record the content hash at the time of a successful ST sync.

        This establishes the baseline used by :func:`compare_libraries` to
        detect which side changed since the last sync.
        """
        with self._conn() as conn:
            conn.execute(
                'UPDATE characters SET st_sync_hash = ? WHERE id = ?',
                (sync_hash, char_id),
            )
            conn.commit()

    def get_linked_cards(self) -> list[dict]:
        """Return all cards that have a SillyTavern link."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM characters WHERE st_avatar_url IS NOT NULL '
                'ORDER BY name COLLATE NOCASE',
            ).fetchall()
            return [dict(r) for r in rows]

    def find_by_st_avatar_url(self, avatar_url: str) -> Optional[dict]:
        """Find a card by its linked SillyTavern avatar URL."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                'SELECT * FROM characters WHERE st_avatar_url = ? LIMIT 1',
                (avatar_url,),
            ).fetchone()
            return dict(row) if row else None

    # ---- SillyTavern deletion tombstones ----

    def record_deleted_st_card(
        self,
        avatar_url: str,
        name: str = '',
        creator: str = '',
        sync_hash: Optional[str] = None,
    ) -> None:
        """Record that a card linked to *avatar_url* was deleted on purpose.

        The sync process uses this tombstone to delete the SillyTavern copy
        (and to never re-import it).  Re-recording for the same file simply
        refreshes the tombstone.  The key is normalized with
        ``os.path.normcase`` so case-only filename differences on
        case-insensitive filesystems still resolve to one row.
        """
        key = os.path.normcase(avatar_url)
        with self._conn() as conn:
            self._record_deleted_st_card(
                conn, key, name=name, creator=creator, sync_hash=sync_hash,
            )
            conn.commit()
        logger.info("Recorded ST deletion tombstone for '%s'", avatar_url)

    def clear_deleted_st_card(self, avatar_url: str) -> None:
        """Remove the deletion tombstone for *avatar_url* (if any).

        Called after the ST copy has been deleted, or when a card is
        (re-)linked to the same ST file — either way the deletion intent
        has been fulfilled or revoked.
        """
        wanted = os.path.normcase(avatar_url)
        with self._conn() as conn:
            self._clear_deleted_st_card(conn, wanted)

    def get_deleted_st_cards(self) -> list[dict]:
        """Return all recorded deletion tombstones (oldest first)."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM deleted_st_cards ORDER BY deleted_at, st_avatar_url'
            ).fetchall()
            return [dict(r) for r in rows]

    def find_duplicate(self, card: CharacterCard) -> Optional[dict]:
        """Check if a card with the same name and creator already exists."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            if card.creator:
                row = conn.execute(
                    'SELECT * FROM characters WHERE name = ? COLLATE NOCASE AND creator = ? COLLATE NOCASE LIMIT 1',
                    (card.name, card.creator),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM characters WHERE name = ? COLLATE NOCASE AND (creator = '' OR creator IS NULL) LIMIT 1",
                    (card.name,),
                ).fetchone()
            return dict(row) if row else None

    def find_all_duplicates(self) -> list[list[dict]]:
        """Find groups of cards sharing the same name and creator (case-insensitive).

        Returns a list of groups, each a list of card dicts with more than one
        entry.  Cards within a group are ordered by ``date_added``.
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM characters '
                "ORDER BY lower(name), lower(coalesce(creator, '')), date_added",
            ).fetchall()
        return group_duplicate_rows([dict(r) for r in rows])

    def find_image_duplicates(self, max_distance: int = 5) -> list[list[dict]]:
        """Find groups of cards with near-identical images via perceptual hashing.

        Uses :func:`perceptual_hash` (a simple average-hash over a grayscale
        thumbnail) and groups hashes within *max_distance* differing bits, so
        a re-encoded or slightly scaled copy still matches its original
        despite not being bit-identical.  Cards whose source image is missing
        or unreadable are skipped.  Returns groups with more than one entry.
        """
        rows = self.get_all(sort_by='date_added')
        hashed: list[tuple[str, dict]] = []
        for row in rows:
            source = row.get('source_path', '')
            if not source or not Path(source).exists():
                continue
            try:
                h = perceptual_hash(source)
            except Exception as e:
                logger.debug("Could not hash image for card %s: %s", row.get('id'), e)
                continue
            hashed.append((h, row))
        groups = group_by_perceptual_hash(hashed, max_distance)
        return [members for members in groups if len(members) > 1]

    def import_card(self, png_path: str | Path) -> Optional[int]:
        png_path = Path(png_path).resolve()
        if not png_path.exists():
            raise FileNotFoundError(f"File not found: {png_path}")
        if png_path.suffix.lower() == '.json':
            raw = read_card_from_json(str(png_path))
        else:
            raw = read_chara_card(str(png_path))
        if raw is None:
            return None
        from src.token_counter import count_card_tokens
        card = CharacterCard.from_spec_dict(raw, source_path=str(png_path))
        card.token_count = count_card_tokens(card)
        return self.add_card(card)

    def import_cards(self, png_paths: list[str | Path]) -> list[tuple[str, Optional[int], Optional[str]]]:
        """Import multiple cards, each isolated in its own transaction.

        One bad file must not abort the rest of the batch, so each path goes
        through :meth:`add_card` independently (a per-file commit, not one
        transaction for the whole list).
        Returns a list of (path, char_id_or_None, error_or_None) tuples summarizing
        the outcome for each input file.
        """
        from src.token_counter import count_card_tokens

        results: list[tuple[str, Optional[int], Optional[str]]] = []
        for path in png_paths:
            resolved = Path(path).resolve()
            try:
                if resolved.suffix.lower() == '.json':
                    raw = read_card_from_json(str(resolved))
                else:
                    raw = read_chara_card(str(resolved))
                if raw is None:
                    results.append((str(path), None, 'No character data found'))
                    continue
                card = CharacterCard.from_spec_dict(raw, source_path=str(resolved))
                card.token_count = count_card_tokens(card)
                # The duplicate check the interactive import path uses: a
                # repeated import must not create a second row *and* a
                # second copy of the file.
                dup = self.find_duplicate(card)
                if dup is not None:
                    results.append((
                        str(path), None,
                        f"Duplicate of '{dup.get('name', card.name)}'",
                    ))
                    continue
                char_id = self.add_card(card)
                results.append((str(path), char_id, None))
            except Exception as e:
                logger.exception("Batch import error for %s", path)
                results.append((str(path), None, str(e)))
        return results
