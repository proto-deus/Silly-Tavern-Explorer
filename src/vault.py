"""Password-based vault: encrypts all user data under the app data dir.

Design
------
* A random 32-byte master key (MEK) encrypts file payloads with AES-256-GCM.
* The MEK is wrapped by a key derived from the user's password (Scrypt) and
  stored in ``vault.json`` along with the KDF parameters and salt.  An optional
  recovery key wraps the MEK a second time, so either secret can unlock it.
* Because the password only wraps the MEK, changing the password re-wraps one
  32-byte blob instead of re-encrypting the whole library.
* Encrypted files carry a ``b'STEV\\x01'`` magic prefix so readers can detect
  them: legacy plaintext files pass through untouched, and crash leftovers or
  partially migrated libraries keep working.

Write policy
------------
A write is encrypted iff the vault is enabled (``vault.json`` exists) *and* the
target path lives under the app data dir.  Exports to user-chosen paths and
SillyTavern sync targets therefore stay plaintext automatically.  If the vault
is enabled but locked, managed writes raise :class:`VaultLocked` instead of
silently dropping plaintext onto disk.

``library.db`` cannot be encrypted while SQLite is using it, so it is decrypted
in place at startup (:func:`unseal_database`) and re-sealed on exit
(:func:`seal_database`) after a WAL checkpoint.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from src.app_paths import data_dir
from src.fs_utils import atomic_write_bytes

logger = logging.getLogger(__name__)

MAGIC = b'STEV\x01'
NONCE_SIZE = 12
KEY_SIZE = 32

VAULT_FILENAME = 'vault.json'

# Scrypt is memory-hard, which is what a password-derived key needs.  The
# parameters are stored per-slot in vault.json so they can be raised later.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_SALT_SIZE = 16

# Recovery keys are 160 bits of entropy shown as base32 groups: 20 bytes
# encode to 32 base32 characters, displayed as eight groups of four.
_RECOVERY_BYTES = 20

# Bounds for KDF parameters read back from vault.json (untrusted on-disk
# input): a corrupt/hostile ``n`` must not be allowed to allocate gigabytes
# or hang the UI thread during unlock.
_SCRYPT_N_MIN = 2
_SCRYPT_N_MAX = 2 ** 20
_SCRYPT_R_MAX = 32
_SCRYPT_P_MAX = 16

_MIGRATION_DIRS = ('library', 'thumbnails', 'sessions', 'lorebooks', 'generated')
_MIGRATION_FILES = ('lorebook_sync_state.json',)


class VaultError(Exception):
    """Base error for vault operations."""


class VaultLocked(VaultError):
    """Encrypted data was touched while no key is in memory."""


class VaultAuthError(VaultError):
    """The supplied password or recovery key does not unlock the vault."""


def is_encrypted_blob(blob: bytes) -> bool:
    """True when *blob* carries the vault file magic."""
    return blob.startswith(MAGIC)


def is_file_encrypted(path: str | Path) -> bool:
    """True when the file's header carries the vault magic (reads 5 bytes)."""
    try:
        with open(path, 'rb') as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode('ascii')


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode('ascii'))


def _seal_raw(key: bytes, plaintext: bytes) -> bytes:
    """Nonce + AES-256-GCM ciphertext (tag included by AESGCM)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(NONCE_SIZE)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def _open_raw(key: bytes, blob: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(blob) < NONCE_SIZE + 16:
        raise VaultError('Encrypted payload is truncated.')
    nonce, ciphertext = blob[:NONCE_SIZE], blob[NONCE_SIZE:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise VaultError('Encrypted payload cannot be decrypted.') from exc


def _derive_kek(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    kdf = Scrypt(salt=salt, length=KEY_SIZE, n=n, r=r, p=p)
    return kdf.derive(secret.encode('utf-8'))


def generate_recovery_key() -> str:
    """Return a fresh recovery key like ``ABCD-EFGH-...`` (eight groups)."""
    raw = os.urandom(_RECOVERY_BYTES)
    text = base64.b32encode(raw).decode('ascii').rstrip('=')
    return '-'.join(text[i:i + 4] for i in range(0, len(text), 4))


def normalize_recovery_key(key: str) -> str:
    """Strip formatting from a typed-in recovery key."""
    return ''.join(ch for ch in (key or '') if ch.isalnum()).upper()


def encrypt_blob(data: bytes, key: bytes) -> bytes:
    """Seal *data* under *key* with the file magic prefix."""
    return MAGIC + _seal_raw(key, data)


def decrypt_blob(blob: bytes, key: bytes) -> bytes:
    """Open a sealed blob; plaintext input passes through unchanged."""
    if not is_encrypted_blob(blob):
        return blob
    return _open_raw(key, blob[len(MAGIC):])


class Vault:
    """Envelope-encryption state for one app data directory."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = Path(root) if root is not None else None
        self._key: Optional[bytes] = None

    # -- paths/state -----------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else data_dir()

    @property
    def meta_path(self) -> Path:
        return self.root / VAULT_FILENAME

    @property
    def enabled(self) -> bool:
        """True when a vault envelope exists on disk."""
        return self.meta_path.is_file()

    @property
    def unlocked(self) -> bool:
        return self._key is not None

    @property
    def has_recovery(self) -> bool:
        meta = self._load_meta(required=False)
        return bool(meta and meta.get('recovery'))

    def lock(self) -> None:
        """Drop the in-memory key."""
        self._key = None

    # -- envelope ---------------------------------------------------------

    def _load_meta(self, required: bool = True) -> Optional[dict]:
        path = self.meta_path
        try:
            raw = path.read_bytes()
        except OSError:
            if required:
                raise VaultError(f'Vault metadata {path} is missing or unreadable.') from None
            return None
        try:
            meta = json.loads(raw.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise VaultError(f'Vault metadata {path} is corrupt: {exc}') from exc
        if not isinstance(meta, dict):
            raise VaultError(f'Vault metadata {path} is corrupt.')
        return meta

    def _save_meta(self, meta: dict) -> None:
        # Deliberately *not* vault.write_bytes: the envelope is the only thing
        # that must stay readable without the key.
        payload = json.dumps(meta, indent=2, sort_keys=True).encode('utf-8')
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.meta_path, payload)

    @staticmethod
    def _wrap_slot(mek: bytes, secret: str) -> dict:
        salt = os.urandom(_SCRYPT_SALT_SIZE)
        kek = _derive_kek(secret, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
        return {
            'kdf': {
                'name': 'scrypt',
                'salt': _b64e(salt),
                'n': _SCRYPT_N,
                'r': _SCRYPT_R,
                'p': _SCRYPT_P,
            },
            'wrapped_mek': _b64e(_seal_raw(kek, mek)),
        }

    @staticmethod
    def _unwrap_slot(slot: dict, secret: str) -> bytes:
        try:
            kdf = slot['kdf']
            salt = _b64d(kdf['salt'])
            n, r, p = int(kdf['n']), int(kdf['r']), int(kdf['p'])
            if not (_SCRYPT_N_MIN <= n <= _SCRYPT_N_MAX) or not (1 <= r <= _SCRYPT_R_MAX) \
                    or not (1 <= p <= _SCRYPT_P_MAX):
                # Treated as corruption, not auth failure: the user's password
                # is fine, the metadata is not.
                raise ValueError('kdf parameters out of range')
            kek = _derive_kek(secret, salt, n, r, p)
            return _open_raw(kek, _b64d(slot['wrapped_mek']))
        except VaultError as exc:
            raise VaultAuthError('Incorrect password or recovery key.') from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise VaultError('Vault metadata is corrupt.') from exc

    @staticmethod
    def _password_slot(meta: dict) -> dict:
        """Return the password slot of *meta*, or raise :class:`VaultError`."""
        slot = meta.get('password')
        if not isinstance(slot, dict):
            # Not a KeyError: unlock/verify must surface this as vault
            # corruption instead of crashing the startup unlock dialog.
            raise VaultError('Vault metadata is corrupt: no password slot.')
        return slot

    def _build_meta(self, mek: bytes, password: str, with_recovery: bool) -> tuple[dict, Optional[str]]:
        meta: dict = {
            'version': 1,
            'created': datetime.now(timezone.utc).isoformat(),
            'password': self._wrap_slot(mek, password),
        }
        recovery_key = None
        if with_recovery:
            recovery_key = generate_recovery_key()
            meta['recovery'] = self._wrap_slot(mek, normalize_recovery_key(recovery_key))
        return meta, recovery_key

    # -- lifecycle --------------------------------------------------------

    def enable(self, password: str, with_recovery: bool = False) -> Optional[str]:
        """Create the vault envelope and unlock it.

        Returns the generated recovery key (once) when *with_recovery* is set.
        Existing data files are *not* touched here; callers run
        :func:`seal_all_files` (with progress) afterwards.
        """
        if self.enabled:
            raise VaultError('Encryption is already enabled.')
        if not password:
            raise VaultError('Password must not be empty.')
        mek = os.urandom(KEY_SIZE)
        meta, recovery_key = self._build_meta(mek, password, with_recovery)
        self._save_meta(meta)
        self._key = mek
        logger.info('Vault enabled (recovery key: %s)', 'yes' if recovery_key else 'no')
        return recovery_key

    def unlock(self, password: str) -> bool:
        """Try to unlock with the password; True on success."""
        def _attempt() -> bytes:
            return self._unwrap_slot(self._password_slot(self._load_meta()), password)

        return self._try_unlock(_attempt)

    def unlock_with_recovery(self, recovery_key: str) -> bool:
        """Try to unlock with the recovery key; True on success."""
        def _attempt() -> bytes:
            meta = self._load_meta()
            slot = meta.get('recovery')
            if not slot:
                raise VaultAuthError('No recovery key is configured for this library.')
            return self._unwrap_slot(slot, normalize_recovery_key(recovery_key))

        return self._try_unlock(_attempt)

    def _try_unlock(self, attempt: Callable[[], bytes]) -> bool:
        try:
            self._key = attempt()
        except VaultAuthError:
            self._key = None
            return False
        except VaultError as exc:
            self._key = None
            logger.error('Vault unlock failed: %s', exc)
            return False
        return True

    def verify_password(self, password: str) -> bool:
        """True when *password* unwraps the MEK (does not change state)."""
        try:
            self._unwrap_slot(self._password_slot(self._load_meta()), password)
        except VaultError:
            return False
        return True

    def change_password(self, old_password: str, new_password: str) -> None:
        """Re-wrap the MEK under a new password (instant; no file re-encryption)."""
        if not new_password:
            raise VaultError('Password must not be empty.')
        meta = self._load_meta()
        try:
            mek = self._unwrap_slot(self._password_slot(meta), old_password)
        except VaultAuthError:
            raise VaultAuthError('The current password is incorrect.') from None
        meta['password'] = self._wrap_slot(mek, new_password)
        self._save_meta(meta)
        self._key = mek
        logger.info('Vault password changed')

    def finish_disable(self) -> None:
        """Delete the envelope after all files have been decrypted."""
        try:
            self.meta_path.unlink(missing_ok=True)
        except OSError as exc:
            raise VaultError(f'Cannot remove {self.meta_path}: {exc}') from exc
        self._key = None
        logger.info('Vault disabled')

    # -- payload crypto ---------------------------------------------------

    def encrypt(self, data: bytes) -> bytes:
        if self._key is None:
            raise VaultLocked('The vault is locked.')
        return encrypt_blob(data, self._key)

    def decrypt(self, blob: bytes) -> bytes:
        if not is_encrypted_blob(blob):
            return blob
        if self._key is None:
            raise VaultLocked('Encountered encrypted data while the vault is locked.')
        try:
            return _open_raw(self._key, blob[len(MAGIC):])
        except VaultError as exc:
            raise VaultError('Encrypted data cannot be decrypted with the current key.') from exc


# -- process-wide active vault -------------------------------------------

_vault: Optional[Vault] = None


def get_vault() -> Vault:
    """The process-wide vault (root resolved against ``data_dir()``)."""
    global _vault
    if _vault is None:
        _vault = Vault()
    return _vault


def set_vault(vault: Optional[Vault]) -> None:
    """Replace the process-wide vault (tests; ``None`` resets it)."""
    global _vault
    _vault = vault


def vault_active() -> bool:
    """True when encryption is enabled *and* the key is in memory."""
    v = get_vault()
    return v.enabled and v.unlocked


# -- transparent I/O -----------------------------------------------------


def _is_managed_path(path: Path) -> bool:
    """True when *path* lives under the vault root (case-safe on Windows)."""
    root = os.path.normcase(str(get_vault().root.resolve()))
    target = os.path.normcase(str(path.resolve()))
    return target == root or target.startswith(root + os.sep)


def read_bytes(path: str | Path) -> bytes:
    """Read a data file, decrypting it when it carries the vault magic."""
    return get_vault().decrypt(Path(path).read_bytes())


def read_text(path: str | Path) -> str:
    return read_bytes(path).decode('utf-8')


def write_bytes(path: str | Path, data: bytes) -> None:
    """Write a data file, sealing it when the vault covers *path*."""
    v = get_vault()
    target = Path(path)
    if v.enabled and _is_managed_path(target):
        # Fail-safe: never write plaintext through the managed path while a
        # vault exists - a locked vault must surface as an error instead.
        data = v.encrypt(data)
    atomic_write_bytes(target, data)


def write_text(path: str | Path, text: str) -> None:
    write_bytes(path, text.encode('utf-8'))


def write_plain_bytes(path: str | Path, data: bytes) -> None:
    """Write *data* verbatim (exports, the vault envelope, the unseal pass)."""
    atomic_write_bytes(Path(path), data)


def write_sealed_bytes(path: str | Path, data: bytes) -> None:
    """Write *data* encrypted regardless of policy (the seal pass)."""
    atomic_write_bytes(Path(path), get_vault().encrypt(data))


def open_image(path: str | Path):
    """Return a fully loaded PIL image for a data file (decrypted as needed)."""
    from PIL import Image

    data = read_bytes(path)
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def import_external(src: str | Path, dst: str | Path) -> None:
    """Copy a file into the library (sealed on write when the vault is on)."""
    write_bytes(dst, read_bytes(src))


def copy_out(src: str | Path, dst: str | Path) -> None:
    """Copy a library file out as plaintext (export, user-chosen target)."""
    write_plain_bytes(dst, read_bytes(src))


# -- library.db seal/unseal ----------------------------------------------


def _resolve_db_path(db_path: str | Path | None) -> Path:
    return Path(db_path) if db_path is not None else data_dir() / 'library.db'


def _drop_db_sidecars(db_path: Path) -> None:
    for sidecar in (Path(str(db_path) + '-wal'), Path(str(db_path) + '-shm')):
        try:
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning('Cannot remove %s: %s', sidecar, exc)


def _checkpoint_wal(db_path: Path) -> None:
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        logger.warning('Cannot open %s for WAL checkpoint: %s', db_path, exc)
        return
    try:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning('WAL checkpoint failed for %s: %s', db_path, exc)
    finally:
        conn.close()


def seal_database(db_path: str | Path | None = None) -> bool:
    """Encrypt ``library.db`` in place (call on exit while the key is held).

    Returns True when the database was sealed.  An already-sealed database is
    left alone so a double call (aboutToQuit plus a defensive path) is safe.
    """
    v = get_vault()
    if not (v.enabled and v.unlocked):
        return False
    path = _resolve_db_path(db_path)
    try:
        if is_file_encrypted(path):
            return False
        _checkpoint_wal(path)
        data = path.read_bytes()
    except OSError as exc:
        logger.error('Cannot read %s for sealing: %s', path, exc)
        return False
    try:
        atomic_write_bytes(path, v.encrypt(data))
    except (OSError, VaultError) as exc:
        logger.error('Cannot seal %s: %s', path, exc)
        return False
    # The WAL was checkpointed (folded into the sealed bytes) before the
    # replace, so dropping the sidecars cannot lose commits; an empty
    # leftover WAL is harmless even if unlink fails.
    _drop_db_sidecars(path)
    logger.info('Library database sealed')
    return True


def unseal_database(db_path: str | Path | None = None) -> bool:
    """Decrypt ``library.db`` in place (call at startup after unlock).

    Returns True when the database was decrypted; a plaintext database
    (fresh install, or one left behind by a crash) passes through untouched.
    """
    v = get_vault()
    if not v.unlocked:
        raise VaultLocked('Cannot unseal the database while the vault is locked.')
    path = _resolve_db_path(db_path)
    if not path.is_file():
        return False
    try:
        data = path.read_bytes()
        if not is_encrypted_blob(data):
            return False
        plain = v.decrypt(data)
    except (OSError, VaultError) as exc:
        logger.error('Cannot unseal %s: %s', path, exc)
        raise
    atomic_write_bytes(path, plain)
    _drop_db_sidecars(path)
    logger.info('Library database unsealed')
    return True


# -- bulk seal/unseal (enable/disable migration) --------------------------


def iter_data_files(root: Optional[Path] = None) -> Iterator[Path]:
    """Yield every data file the vault covers (the DB is handled separately).

    Skips in-flight ``*.tmp`` staging files and, by construction, the
    envelope/keys/log/lock files that live directly in the root.
    """
    base = Path(root) if root is not None else get_vault().root
    for name in _MIGRATION_DIRS:
        directory = base / name
        if directory.is_dir():
            for path in sorted(directory.rglob('*')):
                if path.is_file() and not path.name.endswith('.tmp'):
                    yield path
    for name in _MIGRATION_FILES:
        path = base / name
        if path.is_file():
            yield path
    for path in sorted(base.glob('library.db.bak*')):
        if path.is_file() and not path.name.endswith('.tmp'):
            yield path


def seal_file(path: str | Path) -> bool:
    """Encrypt one file in place; False when it was already sealed."""
    data = Path(path).read_bytes()
    if is_encrypted_blob(data):
        return False
    write_sealed_bytes(path, data)
    return True


def unseal_file(path: str | Path) -> bool:
    """Decrypt one file in place; False when it was not sealed."""
    data = Path(path).read_bytes()
    if not is_encrypted_blob(data):
        return False
    write_plain_bytes(path, get_vault().decrypt(data))
    return True


def seal_all_files(
    root: Optional[Path] = None,
    progress: Optional[Callable[[Path], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> tuple[int, int]:
    """Seal every unencrypted data file; returns ``(changed, failed)``."""
    return _transform_all_files(seal_file, root, progress, cancelled)


def unseal_all_files(
    root: Optional[Path] = None,
    progress: Optional[Callable[[Path], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> tuple[int, int]:
    """Decrypt every sealed data file; returns ``(changed, failed)``."""
    return _transform_all_files(unseal_file, root, progress, cancelled)


def _transform_all_files(
    transform: Callable[[Path], bool],
    root: Optional[Path],
    progress: Optional[Callable[[Path], None]],
    cancelled: Optional[Callable[[], bool]],
) -> tuple[int, int]:
    changed = 0
    failed = 0
    for path in iter_data_files(root):
        if cancelled is not None and cancelled():
            break
        try:
            if transform(path):
                changed += 1
        except (OSError, VaultError) as exc:
            failed += 1
            logger.error('Cannot process %s: %s', path, exc)
        if progress is not None:
            progress(path)
    return changed, failed
