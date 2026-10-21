"""Whole-library backup and restore (zip archive).

A backup is a zip containing:
- ``manifest.json``  — app/version/timestamp + item counts
- ``library.db``     — the SQLite database (WAL checkpointed first)
- ``cards/``         — the character card files
- ``thumbnails/``    — cached thumbnails (regenerable, but cheap to keep)
- ``sessions/``      — saved chat sessions

Restore validates the manifest, stages everything in a temp directory
*inside* the data dir, then swaps it in with atomic replaces.  The database
is replaced last so a failure never leaves a DB that doesn't match the
files.  The staging directory deliberately lives inside the vault root:
the live database is plaintext while the app runs, so a staging area
outside the data dir would leave that plaintext copy in the user's home
directory, where no sealing pass ever reaches it.

Encryption: with the vault enabled, files under the data dir are already
sealed on disk and are archived verbatim (their magic travels with them);
only ``library.db`` - plaintext while the app runs - is sealed explicitly as
it is written into the zip.  Restore decrypts a sealed database in staging
(swap-in state must match the live runtime state) and re-seals any
unencrypted files when a vault is active, so old plaintext archives restore
cleanly into an encrypted library.

On top of the plain archive:

* :func:`create_encrypted_backup` / :func:`read_encrypted_backup` wrap the
  whole archive in a password-derived key (Scrypt + AES-256-GCM) for
  backups that have to live outside the vault.
* :func:`run_scheduled_backup` writes timestamped automatic backups into
  ``<data dir>/backups`` and prunes the old ones.
* :func:`snapshot_database` takes a consistent, vault-aware copy of
  ``library.db`` before a destructive operation.

Pure filesystem logic: no Qt imports, so it is unit-testable and safe to
run on worker threads.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from src.fs_utils import (
    atomic_replace,
    atomic_write_bytes,
    safe_filename_component,
    unique_temp_path,
)
from src import safe_json

logger = logging.getLogger(__name__)

MANIFEST_NAME = 'manifest.json'
BACKUP_FORMAT_VERSION = 1

# Upper bound on the total uncompressed size accepted from a restore archive.
# Guards against a malicious/corrupt zip exhausting memory or disk.
_MAX_RESTORE_BYTES = 8 * 1024 * 1024 * 1024   # 8 GiB

# A member that expands this many times beyond its compressed size is a zip
# bomb: zlib reaches ~1029:1 on zero-filled data, so an 8 MB archive would
# otherwise expand to 8 GiB.  Real library content (PNG cards, JSON
# sessions, SQLite pages) compresses far below this ratio.
_MAX_COMPRESSION_RATIO = 200
# Below this size the deflate framing overhead dominates the ratio, so the
# per-member check is skipped; the archive-wide ratio check still applies.
_MIN_RATIO_CHECK_BYTES = 64 * 1024
# Upper bound on the number of members in a restore archive: a bomb can also
# reach its budget with a huge number of tiny members.
_MAX_RESTORE_MEMBERS = 100_000
# The manifest is a small JSON document.  ``ZipFile.read`` allocates the
# member's declared size, so an oversized manifest must be refused before
# it is read - otherwise a crafted archive causes a MemoryError.
_MAX_MANIFEST_BYTES = 1024 * 1024
# The staged database is loaded into memory to swap it in; refuse absurd
# sizes instead of letting the process die with a MemoryError.
_MAX_DB_BYTES = 512 * 1024 * 1024
# Chunk size used by the bounded copy out of a zip member.
_COPY_CHUNK = 1024 * 1024

_DB_SUBDIR = ''          # library.db sits at the zip root
_CARD_SUBDIR = 'cards'
_THUMB_SUBDIR = 'thumbnails'
_SESSION_SUBDIR = 'sessions'
_LORE_SUBDIR = 'lorebooks'
_GALLERY_SUBDIR = 'gallery'

_SQLITE_HEADER = b'SQLite format 3\x00'

# Directory (inside the data dir) that holds the automatic backups.
_BACKUP_DIR_NAME = 'backups'
# Directory (inside the data dir) that holds the database snapshots.
_SNAPSHOT_DIR_NAME = 'snapshots'
# Name of the automatic-backup files.  Only files matching
# ``_AUTO_BACKUP_RE`` are ever pruned: a user's own backups that happen to
# live in the same folder must survive.
_AUTO_BACKUP_PREFIX = 'st-explorer-'
_AUTO_BACKUP_RE = re.compile(r'^st-explorer-\d{8}-\d{6}(?:-\d+)?\.zip$')
# Name of the directory that holds the pre-restore state.
_PRE_RESTORE_NAME = 'st-explorer-pre-restore'

# Characters that can never appear in a Windows file name.  A member that
# contains one of them cannot be extracted safely, so it is rejected
# instead of producing a raw OS error later on.
_ILLEGAL_NAME_CHARS = frozenset('<>"|?*')


class BackupError(Exception):
    """Raised when a backup cannot be created or restored."""


class BackupCancelled(Exception):
    """Raised when the progress callback requests cancellation."""


def notify_backup_created(path: str) -> None:
    """Announce a created backup archive to the plugin/event bus.

    The event module is imported lazily - it is an optional cross-cutting
    module and must not be pulled in at start-up - and every error is
    swallowed: a failing user plugin must never break the backup.

    Args:
        path: The path of the written archive.
    """
    try:
        from src import app_events
        app_events.backup_created(path)
    except Exception:
        logger.exception("Could not announce the created backup")


def _default_data_dir() -> Path:
    from src.database import _get_data_dir
    return _get_data_dir()


def _checkpoint_wal(db_path: Path, required: bool = False) -> None:
    """Checkpoint the WAL so the main DB file contains every commit.

    ``PRAGMA wal_checkpoint`` reports ``(busy, log, checkpointed)`` instead of
    raising when it cannot finish, so the result has to be inspected: a busy
    checkpoint leaves committed rows in the ``-wal`` file.

    The checkpoint is best-effort: the archive is built from a snapshot
    taken through SQLite's backup API, which sees the committed rows in the
    WAL whether or not the checkpoint succeeded.  Aborting the backup
    because the database was busy (which is the normal state while the app
    is running) would make the feature useless.
    """
    if not db_path.exists():
        raise BackupError(f"Database not found: {db_path}")
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            row = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        if required:
            raise BackupError(f"Could not checkpoint the database: {exc}") from exc
        logger.warning("WAL checkpoint failed: %s", exc)
        return
    busy = row[0] if row else 1
    if busy:
        msg = (
            "The database is busy (another connection is writing); the "
            "checkpoint was skipped. The backup itself is unaffected - it is "
            "taken through SQLite's backup API."
        )
        if required:
            raise BackupError(msg)
        logger.warning(msg)


# -- archive layout --------------------------------------------------------

def _collect_library_files(data: Path) -> list[tuple[Path, str]]:
    """Return the library files to archive as ``(path, zip subdir)`` pairs."""
    file_groups: list[tuple[Path, str]] = [
        (data / 'library', _CARD_SUBDIR),
        (data / 'thumbnails', _THUMB_SUBDIR),
        (data / 'sessions', _SESSION_SUBDIR),
        (data / 'lorebooks', _LORE_SUBDIR),
        (data / 'gallery', _GALLERY_SUBDIR),
    ]
    grouped: list[tuple[Path, str]] = []
    for src_dir, prefix in file_groups:
        if src_dir.is_dir():
            for f in sorted(src_dir.rglob('*')):
                if f.is_file():
                    grouped.append((f, prefix))
    return grouped


def _build_manifest(grouped: list[tuple[Path, str]], encrypted: bool) -> dict:
    """Build the manifest dict for an archive holding *grouped*."""
    return {
        'app': 'ST Explorer',
        'format_version': BACKUP_FORMAT_VERSION,
        'created': datetime.now(timezone.utc).isoformat(),
        'encryption': encrypted,
        'counts': {
            'cards': sum(1 for _, p in grouped if p == _CARD_SUBDIR),
            'thumbnails': sum(1 for _, p in grouped if p == _THUMB_SUBDIR),
            'sessions': sum(1 for _, p in grouped if p == _SESSION_SUBDIR),
            'lorebooks': sum(1 for _, p in grouped if p == _LORE_SUBDIR),
            'gallery': sum(1 for _, p in grouped if p == _GALLERY_SUBDIR),
        },
    }


def _snapshot_db_bytes(db_path: Path) -> bytes:
    """Return a consistent snapshot of the library database as bytes.

    The app's own connections keep committing while the archive is written
    (those commits land in ``-wal``, which is not archived), and a plain file
    copy can tear mid-write.  ``Connection.backup`` copies under a read lock,
    so the snapshot is both consistent and complete.

    The copy is taken into an **in-memory** database and serialised from
    there: a snapshot written to a temp file would be a plaintext copy of the
    whole library sitting next to the destination (a Desktop folder, a USB
    stick, a cloud-synced directory) - outside the vault root, where no
    sealing pass ever reaches it.  A hard kill during the backup would leave
    that file behind forever.
    """
    src_conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        snap_conn = sqlite3.connect(':memory:', timeout=5.0)
        try:
            src_conn.backup(snap_conn)
            try:
                return snap_conn.serialize()
            except AttributeError:
                # SQLite built without SQLITE_ENABLE_DESERIALIZE: fall back to
                # a temp file *inside the vault root*, which the seal pass
                # covers, and delete it as soon as it has been read.
                return _snapshot_db_bytes_via_file(src_conn, db_path)
        finally:
            snap_conn.close()
    finally:
        src_conn.close()


def _snapshot_db_bytes_via_file(src_conn, db_path: Path) -> bytes:
    """Fallback snapshot that goes through a temp file inside the data dir."""
    data = db_path.parent
    tmp = unique_temp_path(data, 'library.db.', '.db-snapshot.tmp')
    try:
        snap_conn = sqlite3.connect(str(tmp), timeout=5.0)
        try:
            src_conn.backup(snap_conn)
        finally:
            snap_conn.close()
        from src import vault
        return vault.read_bytes(tmp)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove the database snapshot %s", tmp)


def _write_archive(
    dest: Path,
    data: Path,
    manifest: dict,
    grouped: list[tuple[Path, str]],
    report,
) -> None:
    """Write the archive (manifest, database, library files) to *dest*."""
    from src import vault

    db_path = data / 'library.db'
    encrypted = vault.vault_active()
    total_files = len(grouped) + 1  # + DB

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp names: two concurrent backups must not interleave writes to
    # the same temp file (the old fixed '<dest>.tmp' / '<dest>.db-snapshot.tmp'
    # collided).
    tmp_zip = unique_temp_path(dest.parent, f'{dest.name}.', '.tmp')
    try:
        db_bytes = _snapshot_db_bytes(db_path)
        with zipfile.ZipFile(tmp_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
            done = 0
            report(0.05, 'Writing database...')
            if encrypted:
                # The live DB is plaintext while the app runs; seal it so the
                # archive carries no readable database. Card/session/lorebook
                # files are already sealed on disk and are copied verbatim.
                zf.writestr('library.db', vault.get_vault().encrypt(db_bytes))
            else:
                zf.writestr('library.db', db_bytes)
            done += 1
            src_dirs = {
                _CARD_SUBDIR: data / 'library',
                _THUMB_SUBDIR: data / 'thumbnails',
                _SESSION_SUBDIR: data / 'sessions',
                _LORE_SUBDIR: data / 'lorebooks',
                _GALLERY_SUBDIR: data / 'gallery',
            }
            for src_file, prefix in grouped:
                rel = src_file.relative_to(src_dirs[prefix])
                zf.write(str(src_file), f'{prefix}/{rel.as_posix()}')
                done += 1
                if done % 25 == 0 or done == total_files:
                    report(done / max(1, total_files), f'Backing up... {done}/{total_files}')
        atomic_replace(tmp_zip, dest)
    finally:
        tmp_zip.unlink(missing_ok=True)


def create_backup(
    zip_path: str | Path,
    data_dir: str | Path | None = None,
    progress_cb=None,
) -> dict:
    """Zip the whole library into *zip_path*.

    ``progress_cb(fraction, message)`` is called with fraction in [0, 1];
    returning False from it cancels (raises :class:`BackupCancelled`).
    Returns the manifest dict that was written.
    """
    data = Path(data_dir) if data_dir else _default_data_dir()
    dest = Path(zip_path)

    def report(fraction: float, message: str) -> None:
        if progress_cb is not None and not progress_cb(min(1.0, max(0.0, fraction)), message):
            raise BackupCancelled('Backup cancelled')

    report(0.0, 'Preparing backup...')
    _checkpoint_wal(data / 'library.db')

    grouped = _collect_library_files(data)
    from src import vault
    manifest = _build_manifest(grouped, vault.vault_active())
    _write_archive(dest, data, manifest, grouped, report)

    logger.info("Library backup written to %s (%d files)", dest, len(grouped) + 1)
    notify_backup_created(str(dest))
    return manifest


# -- member name validation ------------------------------------------------

def _member_parts(member: str) -> list[str]:
    """Split a zip member name into the components to extract.

    ``.`` and ``..`` components are dropped, so a traversal attempt simply
    lands inside the staging directory.  Everything that is not a plain
    file name is rejected with a :class:`BackupError`:

    * absolute paths (``/etc/passwd``) and UNC paths (``//server/share``),
    * drive letters (``C:/x``),
    * empty components (``a//b``, or an empty member name),
    * components with a trailing dot or space (``'.. '``, ``'x.'``) - Win32
      strips those, so ``Path.resolve`` does not see them as traversal,
    * colons (alternate data streams), NUL bytes and other characters
      that cannot appear in a file name.
    """
    normalized = member.replace('\\', '/')
    raw = normalized.split('/')
    if normalized.startswith('/'):
        raise BackupError(f'Unsafe path in backup: {member!r}')
    if raw and raw[0].endswith(':'):
        raise BackupError(f'Unsafe path in backup: {member!r}')
    parts: list[str] = []
    for comp in raw:
        if comp in ('.', '..'):
            continue
        if not comp:
            raise BackupError(f'Unsafe path in backup: {member!r}')
        if comp != comp.rstrip(' .'):
            raise BackupError(
                f'Unsafe path in backup: {member!r} (trailing dot or space)'
            )
        if ':' in comp or '\x00' in comp:
            raise BackupError(f'Unsafe path in backup: {member!r}')
        if any(ch in _ILLEGAL_NAME_CHARS or ord(ch) < 32 for ch in comp):
            raise BackupError(f'Unsafe path in backup: {member!r}')
        parts.append(comp)
    return parts


def _resolve_within(base: Path, parts: list[str]) -> Path | None:
    """Join *parts* under *base*, or return None if the result escapes it."""
    target = base.joinpath(*parts)
    try:
        resolved = target.resolve()
        root = base.resolve()
    except OSError:
        return None
    # A trailing separator stops a sibling like '<root>-evil' from passing.
    if resolved != root and not str(resolved).startswith(str(root) + os.sep):
        return None
    return target


def _copy_limited(src, dst, limit: int) -> int:
    """Copy at most *limit* bytes from *src* to *dst*; returns the count."""
    remaining = max(0, limit)
    copied = 0
    while remaining > 0:
        chunk = src.read(min(_COPY_CHUNK, remaining))
        if not chunk:
            break
        dst.write(chunk)
        copied += len(chunk)
        remaining -= len(chunk)
    return copied


def _zip_entry_count(path: Path) -> int | None:
    """The number of entries in the zip's central directory (None if unknown).

    ``zipfile`` parses the whole central directory into memory when the
    archive is opened, so a crafted archive with millions of entries
    allocates gigabytes before any check can run.  The end-of-central-
    directory record carries the count, and it can be read from the tail of
    the file without parsing anything.
    """
    try:
        size = path.stat().st_size
        with open(path, 'rb') as handle:
            # The EOCD record is at the end, followed by at most 64 KiB of
            # comment.
            handle.seek(max(0, size - 66_000))
            tail = handle.read()
    except OSError:
        return None
    index = tail.rfind(b'PK\x05\x06')
    if index < 0 or len(tail) - index < 22:
        return None
    try:
        return struct.unpack('<H', tail[index + 10:index + 12])[0]
    except (struct.error, IndexError):
        return None


def read_manifest(zip_path: str | Path) -> dict:
    """Read and validate the manifest of a backup zip."""
    count = _zip_entry_count(Path(zip_path))
    if count is not None and count > _MAX_RESTORE_MEMBERS:
        raise BackupError(
            'This backup contains too many files to be valid; refusing to '
            'read it.'
        )
    try:
        with zipfile.ZipFile(str(zip_path), 'r') as zf:
            # The member count is checked as soon as the archive is opened:
            # ``ZipFile`` builds the whole central directory in memory, so a
            # crafted archive with millions of entries would allocate
            # gigabytes before the cap below was ever consulted.
            if len(zf.namelist()) > _MAX_RESTORE_MEMBERS:
                raise BackupError(
                    'This backup contains too many files to be valid; refusing '
                    'to read it.'
                )
            try:
                info = zf.getinfo(MANIFEST_NAME)
            except KeyError:
                raise BackupError('Not an ST Explorer backup (missing manifest).') from None
            # The manifest is untrusted input: refuse an oversized one
            # *before* reading it, otherwise ``zf.read`` would allocate the
            # member's declared size (gigabytes) and die with a MemoryError.
            if info.file_size > _MAX_MANIFEST_BYTES:
                raise BackupError(
                    'The backup manifest is too large to be valid; refusing to read it.'
                )
            with zf.open(info) as fh:
                raw = fh.read(_MAX_MANIFEST_BYTES + 1)
            if len(raw) > _MAX_MANIFEST_BYTES:
                raise BackupError(
                    'The backup manifest is too large to be valid; refusing to read it.'
                )
    except BackupError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        # A non-zip file, an encrypted zip or a directory: report it like
        # every other bad backup instead of leaking a raw error to the UI.
        raise BackupError(f'Cannot read backup: {exc}') from exc
    try:
        manifest = safe_json.loads_safe(raw.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise BackupError(f'Corrupted backup manifest: {exc}') from exc
    if not isinstance(manifest, dict) or manifest.get('app') != 'ST Explorer':
        raise BackupError('Not an ST Explorer backup.')
    version = manifest.get('format_version', 0)
    # The manifest is untrusted input: a string/None here would raise TypeError
    # on comparison and surface as a raw error instead of the friendly message.
    if not isinstance(version, int) or isinstance(version, bool):
        if isinstance(version, float) and version.is_integer():
            version = int(version)
        else:
            raise BackupError('Corrupted backup manifest: bad format_version.')
    if version > BACKUP_FORMAT_VERSION:
        raise BackupError(
            'This backup was created by a newer version of ST Explorer.'
        )
    manifest['format_version'] = version
    return manifest


def restore_backup(
    zip_path: str | Path,
    data_dir: str | Path | None = None,
    progress_cb=None,
) -> dict:
    """Restore a backup zip into *data_dir* (default: the live data dir).

    Returns the manifest dict on success.  The caller must ensure the app
    restarts afterwards — open connections keep pointing at the old DB.

    Everything is validated and staged first; only then is anything live
    touched, and each live directory is moved aside (not deleted) so a failure
    part-way through can be rolled back.  Directories the archive does not
    contain are left untouched rather than being wiped.
    """
    src = Path(zip_path)
    data = Path(data_dir) if data_dir else _default_data_dir()

    def report(fraction: float, message: str) -> None:
        if progress_cb is not None and not progress_cb(min(1.0, max(0.0, fraction)), message):
            raise BackupCancelled('Restore cancelled')

    report(0.0, 'Validating backup...')
    manifest = read_manifest(src)

    # The staging area lives *inside* the data dir.  The live database is
    # plaintext while the app runs: staging it next to the data dir (i.e. in
    # the user's home directory) left that plaintext copy outside the vault
    # root, where no sealing pass ever reaches it.
    data.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(
        prefix='.st-explorer-restore-', dir=str(data)))
    staging_db = staging_root / 'data'
    staging_db.mkdir(parents=True, exist_ok=True)
    swapped: list[tuple[Path, Path]] = []   # (live, rollback copy)
    db_backup: Path | None = None
    # When a rollback itself fails the original data must survive: the
    # staging directory is then left in place (and its path logged) instead
    # of being deleted along with the last copy of the user's library.
    preserve_staging = False
    # The entry count is read from the tail of the file: opening the
    # archive parses the whole central directory into memory, and a crafted
    # archive with millions of entries would allocate gigabytes first.
    count = _zip_entry_count(src)
    if count is not None and count > _MAX_RESTORE_MEMBERS:
        raise BackupError(
            'The backup contains too many files; refusing to extract it.'
        )
    try:
        with zipfile.ZipFile(str(src), 'r') as zf:
            members = zf.namelist()
            total = len(members)
            if total > _MAX_RESTORE_MEMBERS:
                raise BackupError(
                    'The backup contains too many files; refusing to extract it.'
                )
            # Absolute ceiling so a zip bomb can't exhaust memory/disk.
            budget = _MAX_RESTORE_BYTES
            spent = 0
            spent_compressed = 0
            for i, member in enumerate(members):
                normalized = member.replace('\\', '/')
                if normalized.endswith('/'):
                    continue    # directory entry
                # Normalise and guard against path traversal.
                parts = _member_parts(member)
                if not parts:
                    continue
                target = _resolve_within(staging_db, parts)
                if target is None:
                    raise BackupError(f'Unsafe path in backup: {member}')
                try:
                    info = zf.getinfo(member)
                except KeyError:
                    continue
                file_size = max(0, info.file_size)
                compress_size = max(0, info.compress_size)
                if (file_size >= _MIN_RATIO_CHECK_BYTES
                        and file_size > compress_size * _MAX_COMPRESSION_RATIO):
                    raise BackupError(
                        'This backup looks like a zip bomb (a file expands far '
                        'beyond its compressed size); refusing to extract it.'
                    )
                spent += file_size
                spent_compressed += compress_size
                if spent > budget:
                    raise BackupError('Backup is larger than the restore limit; refusing to extract.')
                if (spent >= _MIN_RATIO_CHECK_BYTES
                        and spent > spent_compressed * _MAX_COMPRESSION_RATIO):
                    raise BackupError(
                        'This backup looks like a zip bomb (it expands far '
                        'beyond its compressed size); refusing to extract it.'
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with zf.open(info) as fh, open(target, 'wb') as out:
                        # Bounded copy: never write more than the member
                        # claims, whatever the compressed stream holds.
                        _copy_limited(fh, out, file_size)
                except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                    raise BackupError(
                        f'Cannot extract {member!r} from the backup: {exc}'
                    ) from exc
                if i % 25 == 0 or i == total - 1:
                    report(0.1 + 0.65 * (i + 1) / max(1, total), f'Restoring... {i + 1}/{total}')

        db_copy = staging_db / 'library.db'
        if not db_copy.exists():
            raise BackupError('Backup does not contain a database.')
        from src import vault
        try:
            db_size = db_copy.stat().st_size
        except OSError as exc:
            raise BackupError(f'Cannot read the backed-up database: {exc}') from exc
        if db_size > _MAX_DB_BYTES:
            raise BackupError(
                'The backed-up database is too large to restore; refusing to load it.'
            )
        db_bytes = db_copy.read_bytes()
        if vault.is_encrypted_blob(db_bytes):
            if not vault.vault_active():
                raise BackupError(
                    'This backup is encrypted. Enable data encryption and unlock '
                    'the library before restoring it.'
                )
            try:
                db_bytes = vault.get_vault().decrypt(db_bytes)
            except vault.VaultError as exc:
                raise BackupError(
                    'The encrypted backup could not be decrypted with the '
                    'current password.'
                ) from exc
            # Swap in plaintext: the live library database is decrypted while
            # the app runs and is sealed again on exit.  The write is atomic:
            # a plain ``write_bytes`` leaves a half-written database behind
            # when it fails.
            atomic_write_bytes(db_copy, db_bytes)
        if db_bytes[:len(_SQLITE_HEADER)] != _SQLITE_HEADER:
            raise BackupError('The backed-up database is corrupted.')

        # Refuse to start the destructive phase on cancellation - everything
        # above is reversible.
        report(0.78, 'Swapping in restored files...')
        data.mkdir(parents=True, exist_ok=True)

        # Zip subdir -> live data-dir name ('cards' holds the contents of
        # the live 'library' directory).
        dir_map = {
            _CARD_SUBDIR: 'library',
            _THUMB_SUBDIR: 'thumbnails',
            _SESSION_SUBDIR: 'sessions',
            _LORE_SUBDIR: 'lorebooks',
            _GALLERY_SUBDIR: 'gallery',
        }
        # Only directories the archive actually provides are replaced. Wiping
        # a live directory that the backup omits would destroy data the
        # archive never held.
        plan = [
            (staging_db / staged_name, data / live_name)
            for staged_name, live_name in dir_map.items()
            if (staging_db / staged_name).exists()
        ]

        # ``db_backup`` is a *success flag*, not a path: it is only set once
        # the live database has actually been moved aside.  It is declared
        # before the try so the rollback below can always read it, even when
        # the failure happened long before the database was touched.
        db_backup: Path | None = None

        try:
            for i, (staged, live) in enumerate(plan):
                rollback = staging_root / f'rollback-{live.name}'
                if live.exists():
                    os.replace(str(live), str(rollback))
                    swapped.append((live, rollback))
                os.replace(str(staged), str(live))
                report(0.78 + 0.14 * ((i + 1) / max(1, len(plan))),
                       f'Swapping in {live.name}...')

            # Database last: a failure above leaves the old DB with the old
            # files rather than a new DB pointing at deleted ones.
            live_db = data / 'library.db'
            if live_db.exists():
                candidate = staging_root / 'rollback-library.db'
                # atomic_replace retries the Windows sharing violation that
                # a live SQLite connection causes; a raw os.replace failed
                # whenever a query was in flight.
                atomic_replace(live_db, candidate)
                db_backup = candidate
            # Move (not delete) any stale WAL/SHM: if the swap below fails the
            # old database is restored, and its un-checkpointed WAL frames have
            # to survive with it - SQLite would otherwise see a truncated DB.
            for suffix in ('-wal', '-shm'):
                sidecar = data / ('library.db' + suffix)
                if sidecar.exists():
                    try:
                        os.replace(str(sidecar), str(staging_root / ('rollback-library.db' + suffix)))
                    except OSError:
                        # A foreign WAL left next to the new database would be
                        # replayed over it by SQLite and silently undo the
                        # restore.  Removing it is safe (the frames belong to
                        # the database that is being replaced); if that fails
                        # too, abort and roll back rather than install a
                        # database next to a WAL that does not match it.
                        try:
                            sidecar.unlink()
                        except OSError:
                            preserve_staging = True
                            raise BackupError(
                                f'Could not remove the stale {sidecar.name}; the '
                                f'restore was aborted so the database cannot be '
                                f'left inconsistent.'
                            ) from None
                        logger.warning("Removed the stale %s", sidecar.name)
            atomic_replace(db_copy, live_db)
        except Exception:
            # Roll back everything we moved so a failure is non-destructive.
            for live, rollback in reversed(swapped):
                try:
                    if live.exists():
                        shutil.rmtree(live) if live.is_dir() else live.unlink()
                    os.replace(str(rollback), str(live))
                except OSError:
                    preserve_staging = True
                    logger.exception("Rollback of %s failed; it is preserved at %s",
                                     live, rollback)
            if db_backup is not None:
                try:
                    if (data / 'library.db').exists():
                        (data / 'library.db').unlink()
                    os.replace(str(db_backup), str(data / 'library.db'))
                    for suffix in ('-wal', '-shm'):
                        saved = staging_root / ('rollback-library.db' + suffix)
                        if saved.exists():
                            os.replace(str(saved), str(data / ('library.db' + suffix)))
                except OSError:
                    preserve_staging = True
                    logger.exception("Rollback of library.db failed; it is preserved at %s",
                                     db_backup)
            raise

        logger.info("Library restored from %s", src)
        if vault.vault_active():
            # Plaintext archives (or partial restores) leave unencrypted files
            # in the library; seal them so the vault invariant holds. Already
            # sealed files are skipped, so this is cheap for encrypted backups.
            changed, failed = vault.seal_all_files(root=data)
            if changed or failed:
                logger.info("Re-sealed %d restored file(s) (%d failures)", changed, failed)
        # Keep the pre-restore state: restoring the wrong archive has to be
        # recoverable.  Only the most recent snapshot is kept.  It stays
        # inside the vault root, and the rolled-back database is sealed
        # first so no plaintext database survives the restore.
        try:
            _seal_rollback_database(staging_root / 'rollback-library.db')
            snapshot = data / _PRE_RESTORE_NAME
            if snapshot.exists():
                shutil.rmtree(snapshot, ignore_errors=True)
            shutil.move(str(staging_root), str(snapshot))
            _seal_snapshot_tree(snapshot)
            logger.warning(
                "Pre-restore state saved to %s - delete it once the restore is confirmed",
                snapshot,
            )
        except OSError:
            # The staging directory holds the rolled-back library: it is the
            # safety net the user needs if the restore turned out to be the
            # wrong archive.  Deleting it because the *move* to the snapshot
            # location failed would destroy that safety net, so it is kept.
            preserve_staging = True
            logger.exception("Could not keep a pre-restore snapshot")
        return manifest
    finally:
        if preserve_staging:
            logger.error(
                "Restore rollback failed; the previous library data is preserved at %s",
                staging_root,
            )
        else:
            shutil.rmtree(staging_root, ignore_errors=True)


def _seal_snapshot_tree(root: Path) -> None:
    """Seal every plaintext file of the pre-restore snapshot.

    The rolled-back library files were already sealed on disk, but a file
    that never got sealed (a failed migration, a hand-copied file) would
    survive the restore as readable data.  ``seal_file`` skips files that
    are already sealed, so this is a no-op for a healthy library.
    """
    from src import vault

    if not vault.vault_active():
        return
    try:
        files = [p for p in root.rglob('*') if p.is_file()]
    except OSError as exc:
        logger.warning("Could not list the pre-restore snapshot: %s", exc)
        return
    for path in files:
        try:
            vault.seal_file(path)
        except (OSError, vault.VaultError) as exc:
            logger.error("Could not seal the pre-restore file %s: %s", path, exc)


def _seal_rollback_database(path: Path) -> bool:
    """Encrypt the rolled-back database copy in place.

    The live database is plaintext while the app runs, so the copy that is
    kept for a rollback would stay readable on disk.  With the vault active
    the copy is sealed (a WAL checkpoint folds the sidecars into it first);
    if that fails the plaintext copy is deleted rather than left behind.

    Returns True when no plaintext database is left at *path*.
    """
    from src import vault

    if not path.exists():
        return True
    if not vault.vault_active():
        # No vault: the whole data dir is plaintext anyway.
        return True

    sealed = False
    try:
        sealed = vault.seal_database(path)
    except (OSError, vault.VaultError) as exc:
        logger.error("Could not seal the pre-restore database: %s", exc)
    if not sealed and vault.is_file_encrypted(path):
        # Already sealed (seal_database reports "nothing to do").
        sealed = True

    # A leftover WAL/SHM sidecar holds plaintext database pages too.
    for suffix in ('-wal', '-shm'):
        sidecar = Path(str(path) + suffix)
        if not sidecar.exists():
            continue
        try:
            vault.seal_file(sidecar)
        except (OSError, vault.VaultError) as exc:
            logger.error("Could not seal the pre-restore sidecar %s: %s", sidecar, exc)
            try:
                sidecar.unlink()
            except OSError:
                sealed = False

    if not sealed:
        # Sealing failed: delete the plaintext copy instead of leaving it.
        try:
            path.unlink()
            logger.warning("Deleted the unsealed pre-restore database %s", path)
        except OSError:
            logger.error("Could not remove the unsealed pre-restore database %s", path)
    return sealed


# -- password encrypted backups -------------------------------------------

def create_encrypted_backup(
    zip_path: str | Path,
    password: str,
    data_dir: str | Path | None = None,
    progress_cb=None,
) -> dict:
    """Write a backup whose whole archive is encrypted with *password*.

    The normal archive is built first and then encrypted as one blob
    (Scrypt-derived key + AES-256-GCM), so the result is *not* a readable
    zip any more - it is safe to copy to an unencrypted location.

    Returns the manifest dict; its ``encryption`` field is ``'password'``.
    """
    from src import vault

    data = Path(data_dir) if data_dir else _default_data_dir()
    dest = Path(zip_path)
    if not password:
        raise BackupError('A password is required to encrypt the backup.')

    def report(fraction: float, message: str) -> None:
        if progress_cb is not None and not progress_cb(min(1.0, max(0.0, fraction)), message):
            raise BackupCancelled('Backup cancelled')

    report(0.0, 'Preparing backup...')
    _checkpoint_wal(data / 'library.db')

    grouped = _collect_library_files(data)
    vault_on = vault.vault_active()
    manifest = _build_manifest(grouped, vault_on)
    manifest['encryption'] = 'password'
    manifest['vault_encrypted'] = vault_on

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = unique_temp_path(dest.parent, f'{dest.name}.', '.tmp')
    try:
        _write_archive(tmp_zip, data, manifest, grouped, report)
        blob = vault.encrypt_with_password(tmp_zip.read_bytes(), password)
        # The blob is written verbatim: it is already encrypted, and the
        # destination is usually outside the vault root.
        vault.write_plain_bytes(dest, blob)
    finally:
        tmp_zip.unlink(missing_ok=True)

    logger.info("Encrypted library backup written to %s", dest)
    return manifest


def read_encrypted_backup(zip_path: str | Path, password: str) -> dict:
    """Decrypt a password-encrypted backup and return its manifest.

    Raises :class:`BackupError` when the password is wrong or the file is
    corrupt.
    """
    from src import vault

    src = Path(zip_path)
    try:
        size = src.stat().st_size
    except OSError as exc:
        raise BackupError(f'Cannot read backup: {exc}') from exc
    if size > _MAX_RESTORE_BYTES:
        raise BackupError('Backup is larger than the restore limit; refusing to read it.')
    try:
        blob = src.read_bytes()
    except OSError as exc:
        raise BackupError(f'Cannot read backup: {exc}') from exc
    if len(blob) > _MAX_RESTORE_BYTES:
        raise BackupError('Backup is larger than the restore limit; refusing to read it.')
    if not vault.is_password_blob(blob):
        raise BackupError('This is not a password-encrypted backup.')
    try:
        plain = vault.decrypt_with_password(blob, password)
    except vault.VaultError as exc:
        raise BackupError(
            'The backup could not be decrypted: the password is wrong or the '
            'file is corrupt.'
        ) from exc

    # The decrypted archive is read straight from memory: writing it to the
    # system temp dir put the whole library in plaintext on disk (readable
    # by other accounts on many systems), and a killed process skipped the
    # cleanup, leaving it there forever.  Only the manifest is needed.
    try:
        with zipfile.ZipFile(io.BytesIO(plain)) as zf:
            if MANIFEST_NAME not in zf.namelist():
                raise BackupError('The decrypted backup has no manifest.')
            raw = zf.read(MANIFEST_NAME)
    except zipfile.BadZipFile as exc:
        raise BackupError('The decrypted backup is not a valid archive.') from exc
    except OSError as exc:
        raise BackupError(f'Cannot read backup: {exc}') from exc
    try:
        manifest = safe_json.loads_safe(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackupError('The decrypted backup has a corrupt manifest.') from exc
    if not isinstance(manifest, dict):
        raise BackupError('The decrypted backup has a corrupt manifest.')
    return manifest


# -- automatic (scheduled) backups ----------------------------------------

def backup_destination(data_dir: str | Path | None = None) -> Path:
    """Return the default path of an automatic backup.

    ``<data dir>/backups/st-explorer-<YYYYmmdd-HHMMSS>.zip``.  The
    ``backups`` directory is created if needed.  Two backups within the same
    second must not collide, so a counter is appended when the timestamped
    name is already taken.
    """
    data = Path(data_dir) if data_dir else _default_data_dir()
    backups = data / _BACKUP_DIR_NAME
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    candidate = backups / f'{_AUTO_BACKUP_PREFIX}{stamp}.zip'
    counter = 1
    while candidate.exists():
        candidate = backups / f'{_AUTO_BACKUP_PREFIX}{stamp}-{counter}.zip'
        counter += 1
    return candidate


def _prune_automatic_backups(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest *keep* automatic backups in *directory*.

    Only files matching :data:`_AUTO_BACKUP_RE` are considered: a user's own
    backups that live in the same folder must survive.
    """
    removed: list[Path] = []
    try:
        entries = [p for p in directory.iterdir() if p.is_file() and _AUTO_BACKUP_RE.match(p.name)]
    except OSError as exc:
        logger.warning("Cannot list %s for pruning: %s", directory, exc)
        return removed
    entries.sort(key=lambda p: (_mtime(p), p.name), reverse=True)
    for path in entries[max(0, keep):]:
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:
            logger.warning("Could not remove old backup %s: %s", path, exc)
    return removed


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def run_scheduled_backup(
    data_dir: str | Path | None = None,
    keep: int = 5,
    progress_cb=None,
) -> dict | None:
    """Create an automatic backup and prune the older ones.

    At most *keep* automatic backups are kept in ``<data dir>/backups``;
    files that do not follow the automatic naming pattern are never
    touched.  This function never raises: a scheduled run must not be able
    to crash the app, so failures are logged and reported as ``None``.
    """
    try:
        dest = backup_destination(data_dir)
        manifest = create_backup(dest, data_dir=data_dir, progress_cb=progress_cb)
        removed = _prune_automatic_backups(dest.parent, keep)
        if removed:
            logger.info("Pruned %d old automatic backup(s)", len(removed))
        return manifest
    except Exception as exc:  # a scheduled run must never break the app
        logger.error("Scheduled backup failed: %s", exc, exc_info=True)
        return None


# -- database snapshots ----------------------------------------------------

def _copy_database(src: Path, dst: Path) -> None:
    """Copy the SQLite database *src* into *dst* through the backup API.

    The backup API copies under a read lock, so the result is consistent
    even while the app keeps writing.
    """
    conn = sqlite3.connect(str(src), timeout=5.0)
    try:
        snap = sqlite3.connect(str(dst), timeout=5.0)
        try:
            conn.backup(snap)
        finally:
            snap.close()
    finally:
        conn.close()


def snapshot_database(
    data_dir: str | Path | None = None,
    label: str = 'manual',
) -> Path | None:
    """Make a safe, consistent copy of ``library.db``.

    The snapshot is written to ``<data dir>/snapshots/<label>-<timestamp>.db``
    and uses the vault-aware write, so it is encrypted when the vault is
    active.  Returns the snapshot path, or ``None`` when the snapshot could
    not be created.
    """
    from src import vault

    data = Path(data_dir) if data_dir else _default_data_dir()
    db_path = data / 'library.db'
    if not db_path.is_file():
        logger.warning("Cannot snapshot the database: %s does not exist", db_path)
        return None
    safe_label = safe_filename_component(label) or 'manual'
    try:
        snapshots = data / _SNAPSHOT_DIR_NAME
        snapshots.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        target = snapshots / f'{safe_label}-{stamp}.db'
        counter = 1
        while target.exists():
            target = snapshots / f'{safe_label}-{stamp}-{counter}.db'
            counter += 1
        tmp_db = unique_temp_path(snapshots, f'{target.name}.', '.tmp')
        try:
            _copy_database(db_path, tmp_db)
            vault.write_sealed_if_active(target, tmp_db.read_bytes())
        finally:
            tmp_db.unlink(missing_ok=True)
    except (OSError, sqlite3.Error, vault.VaultError) as exc:
        logger.error("Could not create a database snapshot: %s", exc)
        return None
    logger.info("Database snapshot written to %s", target)
    return target


__all__ = [
    'BACKUP_FORMAT_VERSION',
    'MANIFEST_NAME',
    'BackupCancelled',
    'BackupError',
    'backup_destination',
    'create_backup',
    'create_encrypted_backup',
    'read_encrypted_backup',
    'read_manifest',
    'restore_backup',
    'run_scheduled_backup',
    'snapshot_database',
]
