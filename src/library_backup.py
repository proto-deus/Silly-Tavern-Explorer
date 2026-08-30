"""Whole-library backup and restore (zip archive).

A backup is a zip containing:
- ``manifest.json``  — app/version/timestamp + item counts
- ``library.db``     — the SQLite database (WAL checkpointed first)
- ``cards/``         — the character card files
- ``thumbnails/``    — cached thumbnails (regenerable, but cheap to keep)
- ``sessions/``      — saved chat sessions

Restore validates the manifest, stages everything in a temp directory next
to the destination, then swaps it in with atomic replaces.  The database is
replaced last so a failure never leaves a DB that doesn't match the files.

Pure filesystem logic: no Qt imports, so it is unit-testable and safe to
run on worker threads.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from src.fs_utils import atomic_replace

logger = logging.getLogger(__name__)

MANIFEST_NAME = 'manifest.json'
BACKUP_FORMAT_VERSION = 1

_DB_SUBDIR = ''          # library.db sits at the zip root
_CARD_SUBDIR = 'cards'
_THUMB_SUBDIR = 'thumbnails'
_SESSION_SUBDIR = 'sessions'
_LORE_SUBDIR = 'lorebooks'

_SQLITE_HEADER = b'SQLite format 3\x00'


class BackupError(Exception):
    """Raised when a backup cannot be created or restored."""


class BackupCancelled(Exception):
    """Raised when the progress callback requests cancellation."""


def _default_data_dir() -> Path:
    from src.database import _get_data_dir
    return _get_data_dir()


def _checkpoint_wal(db_path: Path) -> None:
    """Checkpoint the WAL so the main DB file contains every commit."""
    if not db_path.exists():
        raise BackupError(f"Database not found: {db_path}")
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("WAL checkpoint failed during backup: %s", exc)


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
    db_path = data / 'library.db'
    dest = Path(zip_path)

    def report(fraction: float, message: str) -> None:
        if progress_cb is not None and not progress_cb(min(1.0, max(0.0, fraction)), message):
            raise BackupCancelled('Backup cancelled')

    report(0.0, 'Preparing backup...')
    _checkpoint_wal(db_path)

    cards_dir = data / 'library'
    thumbs_dir = data / 'thumbnails'
    sessions_dir = data / 'sessions'
    lore_dir = data / 'lorebooks'

    file_groups: list[tuple[Path, str]] = [
        (cards_dir, _CARD_SUBDIR),
        (thumbs_dir, _THUMB_SUBDIR),
        (sessions_dir, _SESSION_SUBDIR),
        (lore_dir, _LORE_SUBDIR),
    ]
    grouped: list[tuple[Path, str]] = []
    for src_dir, prefix in file_groups:
        if src_dir.is_dir():
            for f in sorted(src_dir.rglob('*')):
                if f.is_file():
                    grouped.append((f, prefix))

    total_files = len(grouped) + 1  # + DB
    manifest = {
        'app': 'ST Explorer',
        'format_version': BACKUP_FORMAT_VERSION,
        'created': datetime.now(timezone.utc).isoformat(),
        'counts': {
            'cards': sum(1 for _, p in grouped if p == _CARD_SUBDIR),
            'thumbnails': sum(1 for _, p in grouped if p == _THUMB_SUBDIR),
            'sessions': sum(1 for _, p in grouped if p == _SESSION_SUBDIR),
            'lorebooks': sum(1 for _, p in grouped if p == _LORE_SUBDIR),
        },
    }

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = dest.with_suffix(dest.suffix + '.tmp')
    try:
        with zipfile.ZipFile(tmp_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
            done = 0
            report(0.05, 'Writing database...')
            zf.write(str(db_path), 'library.db')
            done += 1
            src_dirs = {
                _CARD_SUBDIR: cards_dir,
                _THUMB_SUBDIR: thumbs_dir,
                _SESSION_SUBDIR: sessions_dir,
                _LORE_SUBDIR: lore_dir,
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

    logger.info("Library backup written to %s (%d files)", dest, total_files)
    return manifest


def read_manifest(zip_path: str | Path) -> dict:
    """Read and validate the manifest of a backup zip."""
    with zipfile.ZipFile(str(zip_path), 'r') as zf:
        names = set(zf.namelist())
        if MANIFEST_NAME not in names:
            raise BackupError('Not an ST Explorer backup (missing manifest).')
        raw = zf.read(MANIFEST_NAME)
    try:
        manifest = json.loads(raw.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BackupError(f'Corrupted backup manifest: {exc}') from exc
    if not isinstance(manifest, dict) or manifest.get('app') != 'ST Explorer':
        raise BackupError('Not an ST Explorer backup.')
    if manifest.get('format_version', 0) > BACKUP_FORMAT_VERSION:
        raise BackupError(
            'This backup was created by a newer version of ST Explorer.'
        )
    return manifest


def restore_backup(
    zip_path: str | Path,
    data_dir: str | Path | None = None,
    progress_cb=None,
) -> dict:
    """Restore a backup zip into *data_dir* (default: the live data dir).

    Returns the manifest dict on success.  The caller must ensure the app
    restarts afterwards — open connections keep pointing at the old DB.
    """
    src = Path(zip_path)
    data = Path(data_dir) if data_dir else _default_data_dir()

    def report(fraction: float, message: str) -> None:
        if progress_cb is not None and not progress_cb(min(1.0, max(0.0, fraction)), message):
            raise BackupCancelled('Restore cancelled')

    report(0.0, 'Validating backup...')
    manifest = read_manifest(src)

    staging_root = data.parent / f'.st-explorer-restore-{datetime.now().strftime("%Y%m%d%H%M%S")}'
    staging_db = staging_root / 'data'
    staging_root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(str(src), 'r') as zf:
            members = zf.namelist()
            total = len(members)
            for i, member in enumerate(members):
                if member.endswith('/'):
                    continue
                # Normalise and guard against path traversal.
                parts = [p for p in member.replace('\\', '/').split('/') if p not in ('.', '..')]
                if not parts:
                    continue
                target = staging_db.joinpath(*parts)
                if not str(target.resolve()).startswith(str(staging_db.resolve())):
                    raise BackupError(f'Unsafe path in backup: {member}')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member))
                if i % 25 == 0 or i == total - 1:
                    report(0.1 + 0.7 * (i + 1) / max(1, total), f'Restoring... {i + 1}/{total}')

        db_copy = staging_db / 'library.db'
        if not db_copy.exists():
            raise BackupError('Backup does not contain a database.')
        with open(db_copy, 'rb') as f:
            header = f.read(len(_SQLITE_HEADER))
        if header != _SQLITE_HEADER:
            raise BackupError('The backed-up database is corrupted.')

        report(0.85, 'Swapping in restored files...')
        data.mkdir(parents=True, exist_ok=True)

        # Zip subdir -> live data-dir name ('cards' holds the contents of
        # the live 'library' directory).
        dir_map = {
            _CARD_SUBDIR: 'library',
            _THUMB_SUBDIR: 'thumbnails',
            _SESSION_SUBDIR: 'sessions',
            _LORE_SUBDIR: 'lorebooks',
        }
        for staged_name, live_name in dir_map.items():
            staged = staging_db / staged_name
            live = data / live_name
            if live.exists():
                shutil.rmtree(live)
            if staged.exists():
                shutil.move(str(staged), str(live))

        # Remove any stale WAL/SHM so SQLite doesn't mix old pages into the
        # restored database.
        for suffix in ('-wal', '-shm'):
            stale = data / ('library.db' + suffix)
            stale.unlink(missing_ok=True)
        tmp_db = data / '.library.db.restore-tmp'
        shutil.copy2(str(db_copy), str(tmp_db))
        atomic_replace(tmp_db, data / 'library.db')

        report(1.0, 'Restore complete')
        logger.info("Library restored from %s", src)
        return manifest
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
