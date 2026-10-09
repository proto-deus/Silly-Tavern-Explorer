"""Whole-library backup and restore (zip archive).

A backup is a zip containing:
- ``manifest.json``  �?" app/version/timestamp + item counts
- ``library.db``     �?" the SQLite database (WAL checkpointed first)
- ``cards/``         �?" the character card files
- ``thumbnails/``    �?" cached thumbnails (regenerable, but cheap to keep)
- ``sessions/``      �?" saved chat sessions

Restore validates the manifest, stages everything in a temp directory next
to the destination, then swaps it in with atomic replaces.  The database is
replaced last so a failure never leaves a DB that doesn't match the files.

Encryption: with the vault enabled, files under the data dir are already
sealed on disk and are archived verbatim (their magic travels with them);
only ``library.db`` - plaintext while the app runs - is sealed explicitly as
it is written into the zip.  Restore decrypts a sealed database in staging
(swap-in state must match the live runtime state) and re-seals any
unencrypted files when a vault is active, so old plaintext archives restore
cleanly into an encrypted library.

Pure filesystem logic: no Qt imports, so it is unit-testable and safe to
run on worker threads.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from src.fs_utils import atomic_replace

logger = logging.getLogger(__name__)

MANIFEST_NAME = 'manifest.json'
BACKUP_FORMAT_VERSION = 1

# Upper bound on the total uncompressed size accepted from a restore archive.
# Guards against a malicious/corrupt zip exhausting memory or disk.
_MAX_RESTORE_BYTES = 8 * 1024 * 1024 * 1024   # 8 GiB

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


def _checkpoint_wal(db_path: Path, required: bool = False) -> None:
    """Checkpoint the WAL so the main DB file contains every commit.

    ``PRAGMA wal_checkpoint`` reports ``(busy, log, checkpointed)`` instead of
    raising when it cannot finish, so the result has to be inspected: a busy
    checkpoint leaves committed rows in the ``-wal`` file, and archiving only
    the main DB would silently drop them.  With *required* set (backup path) a
    failed checkpoint is fatal rather than merely logged.
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
            "The database is busy (another connection is writing); the backup "
            "would be incomplete."
        )
        if required:
            raise BackupError(msg)
        logger.warning(msg)


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
    _checkpoint_wal(db_path, required=True)

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
    from src import vault
    encrypted = vault.vault_active()
    manifest = {
        'app': 'ST Explorer',
        'format_version': BACKUP_FORMAT_VERSION,
        'created': datetime.now(timezone.utc).isoformat(),
        'encryption': encrypted,
        'counts': {
            'cards': sum(1 for _, p in grouped if p == _CARD_SUBDIR),
            'thumbnails': sum(1 for _, p in grouped if p == _THUMB_SUBDIR),
            'sessions': sum(1 for _, p in grouped if p == _SESSION_SUBDIR),
            'lorebooks': sum(1 for _, p in grouped if p == _LORE_SUBDIR),
        },
    }

    dest.parent.mkdir(parents=True, exist_ok=True)
    # NOTE: with_suffix() *replaces* the extension, so build the temp name by
    # concatenation instead - otherwise 'backup.2024.01' becomes
    # 'backup.2024.tmp' and two runs can collide on the same temp file.
    tmp_zip = dest.with_name(f'{dest.name}.tmp')
    # Snapshot the DB through SQLite's backup API instead of copying the file:
    # the app's own connections keep committing while the archive is written
    # (those commits land in ``-wal``, which is not archived), and a plain file
    # copy can tear mid-write.  ``Connection.backup`` copies under a read lock
    # into a temp file, so the snapshot is both consistent and complete.
    db_snapshot = dest.with_name(f'{dest.name}.db-snapshot.tmp')
    try:
        src_conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            snap_conn = sqlite3.connect(str(db_snapshot), timeout=5.0)
            try:
                src_conn.backup(snap_conn)
            finally:
                snap_conn.close()
        finally:
            src_conn.close()
        with zipfile.ZipFile(tmp_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
            done = 0
            report(0.05, 'Writing database...')
            if encrypted:
                # The live DB is plaintext while the app runs; seal it so the
                # archive carries no readable database. Card/session/lorebook
                # files are already sealed on disk and are copied verbatim.
                zf.writestr('library.db', vault.get_vault().encrypt(db_snapshot.read_bytes()))
            else:
                zf.write(str(db_snapshot), 'library.db')
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
        db_snapshot.unlink(missing_ok=True)

    logger.info("Library backup written to %s (%d files)", dest, total_files)
    return manifest


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


def read_manifest(zip_path: str | Path) -> dict:
    """Read and validate the manifest of a backup zip."""
    try:
        with zipfile.ZipFile(str(zip_path), 'r') as zf:
            names = set(zf.namelist())
            if MANIFEST_NAME not in names:
                raise BackupError('Not an ST Explorer backup (missing manifest).')
            raw = zf.read(MANIFEST_NAME)
    except BackupError:
        raise
    except (zipfile.BadZipFile, OSError) as exc:
        # A non-zip file or a directory: report it like every other bad
        # backup instead of leaking a raw zipfile/OSError to the UI.
        raise BackupError(f'Cannot read backup: {exc}') from exc
    try:
        manifest = json.loads(raw.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
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

    staging_root = Path(tempfile.mkdtemp(
        prefix='.st-explorer-restore-', dir=str(data.parent)))
    staging_db = staging_root / 'data'
    staging_db.mkdir(parents=True, exist_ok=True)
    swapped: list[tuple[Path, Path]] = []   # (live, rollback copy)
    db_backup: Path | None = None
    # When a rollback itself fails the original data must survive: the
    # staging directory is then left in place (and its path logged) instead
    # of being deleted along with the last copy of the user's library.
    preserve_staging = False
    try:
        with zipfile.ZipFile(str(src), 'r') as zf:
            members = zf.namelist()
            total = len(members)
            # Absolute ceiling so a zip bomb can't exhaust memory/disk.
            budget = _MAX_RESTORE_BYTES
            spent = 0
            for i, member in enumerate(members):
                if member.endswith('/'):
                    continue
                # Normalise and guard against path traversal.
                parts = [p for p in member.replace('\\', '/').split('/') if p not in ('.', '..')]
                if not parts:
                    continue
                target = _resolve_within(staging_db, parts)
                if target is None:
                    raise BackupError(f'Unsafe path in backup: {member}')
                try:
                    info = zf.getinfo(member)
                except KeyError:
                    continue
                spent += max(0, info.file_size)
                if spent > budget:
                    raise BackupError('Backup is larger than the restore limit; refusing to extract.')
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fh, open(target, 'wb') as out:
                    shutil.copyfileobj(fh, out)
                if i % 25 == 0 or i == total - 1:
                    report(0.1 + 0.65 * (i + 1) / max(1, total), f'Restoring... {i + 1}/{total}')

        db_copy = staging_db / 'library.db'
        if not db_copy.exists():
            raise BackupError('Backup does not contain a database.')
        from src import vault
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
            # the app runs and is sealed again on exit.
            db_copy.write_bytes(db_bytes)
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
        }
        # Only directories the archive actually provides are replaced. Wiping
        # a live directory that the backup omits would destroy data the
        # archive never held.
        plan = [
            (staging_db / staged_name, data / live_name)
            for staged_name, live_name in dir_map.items()
            if (staging_db / staged_name).exists()
        ]

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
            db_backup = staging_root / 'rollback-library.db'
            live_db = data / 'library.db'
            if live_db.exists():
                os.replace(str(live_db), str(db_backup))
            # Move (not delete) any stale WAL/SHM: if the swap below fails the
            # old database is restored, and its un-checkpointed WAL frames have
            # to survive with it - SQLite would otherwise see a truncated DB.
            for suffix in ('-wal', '-shm'):
                sidecar = data / ('library.db' + suffix)
                if sidecar.exists():
                    try:
                        os.replace(str(sidecar), str(staging_root / ('rollback-library.db' + suffix)))
                    except OSError:
                        logger.warning("Could not move aside %s", sidecar)
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
        # recoverable.  Only the most recent snapshot is kept.
        try:
            snapshot = data.parent / 'st-explorer-pre-restore'
            if snapshot.exists():
                shutil.rmtree(snapshot, ignore_errors=True)
            shutil.move(str(staging_root), str(snapshot))
            logger.warning(
                "Pre-restore state saved to %s - delete it once the restore is confirmed",
                snapshot,
            )
        except OSError:
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
