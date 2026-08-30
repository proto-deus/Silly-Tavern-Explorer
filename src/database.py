from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from src.card_models import CharacterCard
from src.card_parser import read_chara_card, read_card_from_json, save_thumbnail
from src.tag_ops import (
    count_tags,
    merge_tag,
    normalize_tag,
    remove_tag,
    rename_tag,
)

logger = logging.getLogger(__name__)


def _escape_like(text: str) -> str:
    """Escape SQL LIKE wildcards so user input matches literally."""
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


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


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip('. ')
    return name[:80] if name else 'untitled'


sanitize_filename = _sanitize_filename  # public alias for reuse


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
    from PIL import Image

    with Image.open(path) as img:
        gray = img.convert('L').resize((hash_size, hash_size))
        pixels = list(gray.tobytes())
    avg = sum(pixels) / len(pixels) if pixels else 0
    bits = ''.join('1' if p >= avg else '0' for p in pixels)
    hex_len = max(1, (hash_size * hash_size) // 4)
    return f'{int(bits, 2):0{hex_len}x}'


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
                    date_modified TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

    def _do_backup(self, db_file: Path, bak_path: Path, bak2_path: Path) -> None:
        """Checkpoint WAL, rotate the previous backup, and copy the DB.

        The new backup is written to a temp file first so a crash between
        rotation and copy never leaves the user without any backup.
        """
        conn = self._connect()
        try:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.Error:
            logger.warning("WAL checkpoint failed during backup")
        finally:
            conn.close()

        tmp_path = bak_path.with_suffix('.bak.tmp')
        try:
            shutil.copy2(str(db_file), str(tmp_path))
            if bak_path.exists():
                if bak2_path.exists():
                    bak2_path.unlink()
                bak_path.rename(bak2_path)
            tmp_path.replace(bak_path)
            logger.info("Database backed up to %s", bak_path)
        except OSError:
            logger.exception("Database backup failed")
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def backup(self) -> Path:
        """Force a backup of the database. Returns the backup file path."""
        db_file = Path(self.db_path)
        if not db_file.exists():
            raise FileNotFoundError(f"Database file not found: {self.db_path}")
        bak_path = db_file.parent / (db_file.name + '.bak')
        bak2_path = db_file.parent / (db_file.name + '.bak2')
        self._do_backup(db_file, bak_path, bak2_path)
        return bak_path

    def _migrate_db(self) -> None:
        with self._conn() as conn:
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
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_characters_st_avatar_url '
                'ON characters (st_avatar_url)'
            )
            self._migrate_tag_encoding(conn)
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
            conn.commit()

    @staticmethod
    def _migrate_tag_encoding(conn: sqlite3.Connection) -> None:
        """Re-encode legacy tag JSON stored with ``ensure_ascii=True``.

        Older builds stored non-ASCII tags as ``\\uXXXX`` escapes, which the
        LIKE-based tag filter can never match against the user-visible text.
        Rows are rewritten with literal (``ensure_ascii=False``) encoding;
        anything that isn't valid JSON or a list is left untouched.
        """
        rows = conn.execute('SELECT id, tags FROM characters').fetchall()
        for char_id, tags_json in rows:
            if not tags_json or '\\u' not in tags_json:
                continue
            try:
                parsed = json.loads(tags_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, list):
                continue
            re_encoded = json.dumps(parsed, ensure_ascii=False)
            if re_encoded != tags_json:
                conn.execute(
                    'UPDATE characters SET tags = ? WHERE id = ?',
                    (re_encoded, char_id),
                )

    def add_card(self, card: CharacterCard) -> int:
        source_path = Path(card.source_path).resolve()
        if not card.source_path or not source_path.exists():
            raise FileNotFoundError(f"Source path not found: {card.source_path}")

        lib_dir = _get_library_dir()
        ext = source_path.suffix
        safe_name = _sanitize_filename(card.name)
        dest_name = f"{safe_name}_{uuid.uuid4().hex[:8]}{ext}"
        dest_path = lib_dir / dest_name
        shutil.copy2(str(source_path), str(dest_path))

        thumb_dir = _get_thumbnail_dir()
        thumb_name = f"{dest_path.stem}_thumb.png"
        thumb_path = thumb_dir / thumb_name
        try:
            if save_thumbnail(str(dest_path), str(thumb_path)):
                thumb_path_str = str(thumb_path)
            else:
                logger.warning("Failed to generate thumbnail for '%s'; storing null path", card.name)
                thumb_path_str = None
        except OSError as e:
            # A thumbnail is optional; an unwritable thumbnail directory
            # must not fail the whole import.
            logger.warning("Thumbnail write failed for '%s': %s", card.name, e)
            thumb_path_str = None

        try:
            with self._conn() as conn:
                cursor = conn.execute('''
                    INSERT INTO characters
                        (name, source_path, thumbnail_path, tags, creator,
                         description_preview, creator_notes, token_count, spec_version,
                         is_favorite, create_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def get_all(self, sort_by: str = 'name', favorites_only: bool = False) -> list[dict]:
        order = self._sort_order(sort_by)
        where = 'WHERE is_favorite = 1' if favorites_only else ''
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f'SELECT * FROM characters {where} ORDER BY {order}',
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
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(creator, ''), 'Unknown') AS creator, "
                'COUNT(*) AS cnt FROM characters '
                'GROUP BY creator ORDER BY cnt DESC, creator ASC',
            ).fetchall()
            return [(row[0], row[1]) for row in rows]

    def get_spec_version_counts(self) -> dict[str, int]:
        """Return a mapping of spec_version -> card count."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(spec_version, ''), 'Unknown') AS ver, "
                'COUNT(*) AS cnt FROM characters '
                'GROUP BY spec_version ORDER BY cnt DESC',
            ).fetchall()
            return {row[0]: row[1] for row in rows}

    def get_cards_per_week(self) -> list[tuple[str, int]]:
        """Return ``(week, count)`` pairs for cards added per week, oldest first.

        Week is formatted as ``YYYY-WW`` (ISO-style via SQLite strftime).
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT strftime('%Y-%W', date_added) AS week, COUNT(*) AS cnt "
                'FROM characters GROUP BY week ORDER BY week ASC',
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
    ) -> list[dict]:
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
            clauses.append(
                "(name LIKE ? ESCAPE '\\' OR description_preview LIKE ? ESCAPE '\\' "
                "OR tags LIKE ? ESCAPE '\\' OR creator LIKE ? ESCAPE '\\' "
                "OR creator_notes LIKE ? ESCAPE '\\')"
            )
            params.extend([q, q, q, q, q])
        if tags:
            for tag in tags:
                # Exact tag match: tags are stored as JSON arrays, so match
                # the full quoted element instead of any substring (which
                # would make "test" also match "testing").
                escaped = _escape_like(tag)
                clauses.append("tags LIKE ? ESCAPE '\\'")
                params.append(f'%"{escaped}"%')
        where = ' AND '.join(clauses) if clauses else '1=1'
        order = self._sort_order(sort_by)
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
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

    def update_card(
        self,
        char_id: int,
        card: CharacterCard,
        regen_thumbnail: bool = False,
        skip_file_write: bool = False,
    ) -> None:
        """Update a card's metadata and (optionally) its source PNG.

        When *skip_file_write* is True the source PNG is left untouched —
        used by the SillyTavern pull operation which copies the ST PNG
        directly (avoiding a re-serialization that would break the
        content-hash baseline).
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

        if regen_thumbnail:
            thumb_path = entry.get('thumbnail_path', '')
            if thumb_path and source and Path(source).exists():
                save_thumbnail(source, thumb_path)

        with self._conn() as conn:
            conn.execute('''
                UPDATE characters SET
                    name = ?, tags = ?, creator = ?,
                    description_preview = ?, creator_notes = ?,
                    token_count = ?,
                    spec_version = ?, is_favorite = ?,
                    date_modified = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (
                card.name,
                json.dumps(card.tags, ensure_ascii=False),
                card.creator,
                card.description[:500] if card.description else '',
                card.creator_notes[:1000] if card.creator_notes else '',
                card.token_count,
                card.spec_version,
                int(card.fav),
                char_id,
            ))
            conn.commit()
        logger.info("Updated card ID %s ('%s')", char_id, card.name)

    def remove_card(
        self,
        char_id: int,
        delete_files: bool = True,
        record_tombstone: bool = True,
    ) -> None:
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
        if record_tombstone and st_url:
            try:
                self.record_deleted_st_card(
                    st_url,
                    name=entry.get('name', ''),
                    creator=entry.get('creator', ''),
                    sync_hash=entry.get('st_sync_hash'),
                )
            except Exception:
                logger.exception(
                    "Could not record ST deletion tombstone for card %s", char_id,
                )
        # Delete the DB row FIRST: if file deletion then fails, we're left
        # with harmless orphan files (cleanable) instead of a ghost row
        # pointing at deleted files.
        with self._conn() as conn:
            conn.execute('DELETE FROM card_collections WHERE character_id = ?', (char_id,))
            conn.execute('DELETE FROM characters WHERE id = ?', (char_id,))
        logger.info("Removed card ID %s", char_id)
        if delete_files:
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

    def get_all_tags(self) -> list[str]:
        tags: set[str] = set()
        with self._conn() as conn:
            rows = conn.execute('SELECT tags FROM characters').fetchall()
            for (tags_json,) in rows:
                if tags_json:
                    try:
                        for t in json.loads(tags_json):
                            tags.add(t.lower().strip())
                    except (json.JSONDecodeError, TypeError):
                        pass
        return sorted(tags)

    def get_tag_counts(self) -> dict[str, int]:
        """Return a mapping of normalized tag -> number of cards using it.

        Uses the pure ``count_tags`` helper so the aggregation logic is
        unit-tested without a database.
        """
        with self._conn() as conn:
            rows = conn.execute('SELECT tags FROM characters').fetchall()
        parsed: list[list[str]] = []
        for (tags_json,) in rows:
            if not tags_json:
                parsed.append([])
                continue
            try:
                tags_list = json.loads(tags_json)
                parsed.append(tags_list if isinstance(tags_list, list) else [])
            except (json.JSONDecodeError, TypeError):
                parsed.append([])
        return count_tags(parsed)

    def _rewrite_tags(self, transform, conn: sqlite3.Connection) -> int:
        """Apply *transform* (a callable list[str] -> list[str]) to every
        card's tags in a single transaction.

        Returns the number of cards whose tag list actually changed.
        """
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT id, tags FROM characters').fetchall()
        changed = 0
        for row in rows:
            char_id = row['id']
            tags_json = row['tags']
            current: list[str] = []
            if tags_json:
                try:
                    parsed = json.loads(tags_json)
                    if isinstance(parsed, list):
                        current = [str(t) for t in parsed]
                except (json.JSONDecodeError, TypeError):
                    current = []
            new_tags = transform(current)
            if new_tags != current:
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

    # ---- SillyTavern linking ----

    def link_to_st(self, char_id: int, avatar_url: str) -> None:
        """Link a card to a SillyTavern character file by its avatar URL."""
        with self._conn() as conn:
            conn.execute(
                'UPDATE characters SET st_avatar_url = ? WHERE id = ?',
                (avatar_url, char_id),
            )
            conn.commit()
        # A card linked to this ST file exists again, so any recorded
        # "deleted on purpose" intent for the file is obsolete.
        try:
            self.clear_deleted_st_card(avatar_url)
        except Exception:
            logger.exception("Could not clear ST deletion tombstone for '%s'", avatar_url)
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
            conn.execute(
                'INSERT OR REPLACE INTO deleted_st_cards '
                '(st_avatar_url, name, creator, st_sync_hash, deleted_at) '
                'VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)',
                (key, name, creator, sync_hash),
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
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT st_avatar_url FROM deleted_st_cards'
            ).fetchall()
            for row in rows:
                if os.path.normcase(row['st_avatar_url']) == wanted:
                    conn.execute(
                        'DELETE FROM deleted_st_cards WHERE st_avatar_url = ?',
                        (row['st_avatar_url'],),
                    )
            conn.commit()

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

    def find_image_duplicates(self) -> list[list[dict]]:
        """Find groups of cards with near-identical images via perceptual hashing.

        Uses :func:`perceptual_hash` (a simple average-hash over a grayscale
        thumbnail).  Cards whose source image is missing or unreadable are
        skipped.  Returns groups with more than one entry.
        """
        rows = self.get_all(sort_by='date_added')
        groups: dict[str, list[dict]] = {}
        order: list[str] = []
        for row in rows:
            source = row.get('source_path', '')
            if not source or not Path(source).exists():
                continue
            try:
                h = perceptual_hash(source)
            except Exception as e:
                logger.debug("Could not hash image for card %s: %s", row.get('id'), e)
                continue
            if h not in groups:
                groups[h] = []
                order.append(h)
            groups[h].append(row)
        return [groups[h] for h in order if len(groups[h]) > 1]

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
        """Import multiple cards in a single transaction.

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
                char_id = self.add_card(card)
                results.append((str(path), char_id, None))
            except Exception as e:
                logger.exception("Batch import error for %s", path)
                results.append((str(path), None, str(e)))
        return results
