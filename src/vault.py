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
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from src.app_paths import data_dir
from src.fs_utils import atomic_write_bytes
from src import safe_json

logger = logging.getLogger(__name__)

MAGIC = b'STEV\x01'
#: The current file magic.  A payload with this magic carries a random file
#: id that is authenticated as additional data, so a sealed file cannot be
#: swapped for another one.  ``MAGIC`` is the older format, without the id.
MAGIC_V2 = b'STEV\x02'
# Magic prefix of a password-encrypted backup blob (Scrypt KEK + AES-256-GCM).
# Different from MAGIC on purpose: a blob is not a file of the library, and
# the two are never interchangeable.
PASSWORD_BLOB_MAGIC = b'STEBKP01'
#: The current password-blob magic.  It carries the key-derivation
#: parameters in its header, so the cost can be raised later without
#: invalidating the backups that were already written.
PASSWORD_BLOB_MAGIC_V2 = b'STEBKP02'
NONCE_SIZE = 12
KEY_SIZE = 32
#: The random per-file id that binds a sealed payload to itself.
FILE_ID_SIZE = 16

VAULT_FILENAME = 'vault.json'

# Scrypt is memory-hard, which is what a password-derived key needs.  The
# parameters are stored per-slot in vault.json so they can be raised later.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_SALT_SIZE = 16

#: The vault envelope is a few hundred bytes; anything larger is not one.
_MAX_META_BYTES = 64 * 1024

# Recovery keys are 160 bits of entropy shown as base32 groups: 20 bytes
# encode to 32 base32 characters, displayed as eight groups of four.
_RECOVERY_BYTES = 20

# Bounds for KDF parameters read back from vault.json (untrusted on-disk
# input): a corrupt/hostile ``n`` must not be allowed to allocate gigabytes
# or hang the UI thread during unlock.  Memory is 128 * n * r bytes, so the
# individual caps must be paired with a product cap: n = 2**20, r = 32 on
# their own would still allocate ~4 GiB.
_SCRYPT_N_MIN = 2
_SCRYPT_N_MAX = 2 ** 20
_SCRYPT_R_MAX = 32
_SCRYPT_P_MAX = 16
# 128 * n * r <= 32 MiB - comfortably above the parameters this vault writes
# (n = 2**15, r = 8) while keeping a hostile metadata file bounded.
_SCRYPT_MEM_MAX = 2 ** 25

_MIGRATION_DIRS = (
    'library', 'thumbnails', 'sessions', 'lorebooks', 'generated', 'gallery',
    # The scheduled backups and the pre-restore snapshot hold the whole
    # library, so they are part of the "everything under the data dir is
    # encrypted" invariant too.  Leaving them out meant a plaintext copy of
    # the library sat in the data dir.
    'backups', 'snapshots', 'st-explorer-pre-restore',
)
_MIGRATION_FILES = ('lorebook_sync_state.json',)

#: Directory name prefixes that are covered as well (the restore staging
#: area, which lives inside the data dir while a restore runs).
_MIGRATION_DIR_PREFIXES = ('.st-explorer-restore-',)


class VaultError(Exception):
    """Base error for vault operations."""


class VaultMetaMissing(VaultError):
    """The vault envelope file is absent (vault never enabled, or removed).

    Distinct from corruption: "there is nothing to unlock" is a normal
    outcome (``unlock`` returns False), while a corrupt envelope is an
    error the user must be told about.
    """


class VaultLocked(VaultError):
    """Encrypted data was touched while no key is in memory."""


class VaultAuthError(VaultError):
    """The supplied password or recovery key does not unlock the vault."""


def is_encrypted_blob(blob: bytes) -> bool:
    """True when *blob* carries a vault file magic."""
    return blob.startswith(MAGIC) or blob.startswith(MAGIC_V2)


def is_file_encrypted(path: str | Path) -> bool:
    """True when the file's header carries a vault magic (reads 5 bytes)."""
    try:
        with open(path, 'rb') as f:
            head = f.read(len(MAGIC_V2))
        return head.startswith(MAGIC) or head.startswith(MAGIC_V2)
    except OSError:
        return False


def is_password_blob(blob: bytes) -> bool:
    """True when *blob* carries the password-encrypted backup magic."""
    return blob.startswith(PASSWORD_BLOB_MAGIC) or blob.startswith(PASSWORD_BLOB_MAGIC_V2)


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode('ascii')


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode('ascii'))


def _seal_raw(key: bytes, plaintext: bytes, aad: bytes | None = None) -> bytes:
    """Nonce + AES-256-GCM ciphertext (tag included by AESGCM).

    *aad* is authenticated but not encrypted.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(NONCE_SIZE)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _open_raw(key: bytes, blob: bytes, aad: bytes | None = None) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(blob) < NONCE_SIZE + 16:
        raise VaultError('Encrypted payload is truncated.')
    nonce, ciphertext = blob[:NONCE_SIZE], blob[NONCE_SIZE:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        if aad is not None:
            # A payload sealed before the file id was introduced carries no
            # AAD.  Try that form before giving up, so an existing vault
            # still opens.
            try:
                return AESGCM(key).decrypt(nonce, ciphertext, None)
            except InvalidTag:
                pass
        raise VaultError('Encrypted payload cannot be decrypted.') from exc


def _derive_kek(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    kdf = Scrypt(salt=salt, length=KEY_SIZE, n=n, r=r, p=p)
    return kdf.derive(secret.encode('utf-8'))


# -- password-derived keys (exported backups) -----------------------------

def encrypt_with_password(data: bytes, password: str) -> bytes:
    """Seal *data* under a key derived from *password*.

    The blob is self-describing - magic + version + KDF parameters + salt +
    nonce + ciphertext - so the reader needs nothing but the password, and
    the cost of the key derivation can be raised later without invalidating
    the backups that were already written.  This is used for backups that
    have to be stored outside the vault (an encrypted export on a USB stick,
    a copy in a cloud folder): the master key of the vault is not involved.
    """
    if not password:
        raise VaultError('Password must not be empty.')
    salt = os.urandom(_SCRYPT_SALT_SIZE)
    kek = _derive_kek(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    header = (
        PASSWORD_BLOB_MAGIC_V2
        + struct.pack('>III', _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    )
    return header + salt + _seal_raw(kek, data)


def decrypt_with_password(blob: bytes, password: str) -> bytes:
    """Open a blob produced by :func:`encrypt_with_password`."""
    if not is_password_blob(blob):
        raise VaultError('This is not a password-encrypted backup.')
    if not password:
        raise VaultError('Password must not be empty.')
    if blob.startswith(PASSWORD_BLOB_MAGIC_V2):
        head = len(PASSWORD_BLOB_MAGIC_V2)
        if len(blob) < head + 12 + _SCRYPT_SALT_SIZE + NONCE_SIZE + 16:
            raise VaultError('Encrypted payload is truncated.')
        n, r, p = struct.unpack('>III', blob[head:head + 12])
        if not (_SCRYPT_N_MIN <= n <= _SCRYPT_N_MAX) \
                or not (1 <= r <= _SCRYPT_R_MAX) \
                or not (1 <= p <= _SCRYPT_P_MAX) \
                or 128 * n * r > _SCRYPT_MEM_MAX:
            raise VaultError('The backup asks for implausible key-derivation '
                             'parameters and cannot be opened.')
        salt = blob[head + 12:head + 12 + _SCRYPT_SALT_SIZE]
        kek = _derive_kek(password, salt, n, r, p)
        return _open_raw(kek, blob[head + 12 + _SCRYPT_SALT_SIZE:])
    # The older format: the parameters were fixed in the code.
    body = blob[len(PASSWORD_BLOB_MAGIC):]
    if len(body) < _SCRYPT_SALT_SIZE + NONCE_SIZE + 16:
        raise VaultError('Encrypted payload is truncated.')
    salt = body[:_SCRYPT_SALT_SIZE]
    kek = _derive_kek(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return _open_raw(kek, body[_SCRYPT_SALT_SIZE:])


def generate_recovery_key() -> str:
    """Return a fresh recovery key like ``ABCD-EFGH-...`` (eight groups)."""
    raw = os.urandom(_RECOVERY_BYTES)
    text = base64.b32encode(raw).decode('ascii').rstrip('=')
    return '-'.join(text[i:i + 4] for i in range(0, len(text), 4))


def normalize_recovery_key(key: str) -> str:
    """Strip formatting from a typed-in recovery key."""
    return ''.join(ch for ch in (key or '') if ch.isalnum()).upper()


#: The shortest password the vault accepts.  The key is derived from the
#: password with scrypt, but a one-character password is still trivially
#: brute-forced, so the policy sets a floor.
MIN_PASSWORD_LENGTH = 8


def check_password_policy(password: str) -> Optional[str]:
    """Return the reason *password* is unacceptable, or ``None``.

    The vault holds the whole library, and the password is the only thing
    standing between an attacker and it, so a floor on its length is
    enforced rather than merely suggested.
    """
    if not password:
        return 'The password must not be empty.'
    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            f'The password must be at least {MIN_PASSWORD_LENGTH} characters '
            f'long.'
        )
    return None


def encrypt_blob(data: bytes, key: bytes) -> bytes:
    """Seal *data* under *key* with the file magic prefix.

    Each sealed payload carries a random *file id*, which is fed to AES-GCM
    as additional authenticated data.  The id travels with the file, so the
    file can be renamed or moved freely - but a sealed file cannot be
    swapped for a different one: the id in the header would not match the
    content, and the tag check fails.
    """
    file_id = os.urandom(FILE_ID_SIZE)
    return MAGIC_V2 + file_id + _seal_raw(key, data, file_id)


def decrypt_blob(blob: bytes, key: bytes) -> bytes:
    """Open a sealed blob; plaintext input passes through unchanged."""
    if blob.startswith(MAGIC_V2):
        head = len(MAGIC_V2)
        file_id = blob[head:head + FILE_ID_SIZE]
        if len(file_id) != FILE_ID_SIZE:
            raise VaultError('Encrypted payload is truncated.')
        return _open_raw(key, blob[head + FILE_ID_SIZE:], file_id)
    if is_encrypted_blob(blob):
        # A payload sealed before the file id was introduced.
        return _open_raw(key, blob[len(MAGIC):])
    return blob


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
        # A property must never raise: corrupt metadata would escape the
        # unlock dialog's constructor into the generic "failed to start"
        # handler with no useful message.
        try:
            meta = self._load_meta(required=False)
        except VaultError:
            logger.error("Vault metadata is unreadable; treating as no recovery key")
            return False
        return bool(meta and meta.get('recovery'))

    def lock(self) -> None:
        """Drop the in-memory key.

        The key bytes are overwritten before the reference is dropped, so
        they are not left lying around in the heap for the rest of the
        process' life.
        """
        key = self._key
        self._key = None
        if key:
            try:
                mutable = bytearray(key)
                for index in range(len(mutable)):
                    mutable[index] = 0
            except Exception:
                logger.debug('The vault key could not be zeroized', exc_info=True)

    # -- envelope ---------------------------------------------------------

    def _load_meta(self, required: bool = True) -> Optional[dict]:
        path = self.meta_path
        try:
            # The metadata is read in full, so its size is capped: a huge
            # (or hostile) file would be loaded into memory before it could
            # be rejected.
            size = path.stat().st_size
            if size > _MAX_META_BYTES:
                raise VaultError(
                    f'Vault metadata {path} is too large to be valid.'
                )
            raw = path.read_bytes()
        except OSError:
            if required:
                raise VaultMetaMissing(
                    f'Vault metadata {path} is missing or unreadable.'
                ) from None
            return None
        try:
            meta = safe_json.loads_safe(raw.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
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
                    or not (1 <= p <= _SCRYPT_P_MAX) \
                    or 128 * n * r > _SCRYPT_MEM_MAX:
                # Treated as corruption, not auth failure: the user's password
                # is fine, the metadata is not.
                raise ValueError('kdf parameters out of range')
            kek = _derive_kek(secret, salt, n, r, p)
            wrapped = _b64d(slot['wrapped_mek'])
            if len(wrapped) < NONCE_SIZE + 16:
                # A truncated payload is corrupt metadata, not a wrong
                # password: reporting it as the latter tells the user to
                # keep typing a password that is perfectly fine.
                raise ValueError('the wrapped key is truncated')
            return _open_raw(kek, wrapped)
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
        problem = check_password_policy(password)
        if problem:
            raise VaultError(problem)
        mek = os.urandom(KEY_SIZE)
        meta, recovery_key = self._build_meta(mek, password, with_recovery)
        # The envelope is written *before* the files are sealed, so a crash
        # in between would leave the vault enabled with half the library in
        # plaintext and no marker that the migration is unfinished.  The
        # flag is cleared by the seal pass (and re-checked at startup).
        meta['migration_pending'] = True
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
            # A wrong secret is a normal outcome: False means "try again".
            self._key = None
            return False
        except VaultMetaMissing:
            # Nothing to unlock (vault not enabled / envelope removed).
            self._key = None
            return False
        except VaultError as exc:
            # Corruption is NOT a wrong password: the user can never guess
            # their way past it. Surface it so the UI says "the vault
            # metadata is corrupt" instead of "incorrect password".
            self._key = None
            logger.error('Vault unlock failed: %s', exc)
            raise
        return True

    def verify_password(self, password: str) -> bool:
        """True when *password* unwraps the MEK (does not change state).

        Raises :class:`VaultError` when the vault metadata itself is corrupt -
        that is indistinguishable from "wrong password" only by accident of
        both failing to unwrap, and the user deserves the real message.
        """
        try:
            self._unwrap_slot(self._password_slot(self._load_meta()), password)
        except VaultAuthError:
            return False
        return True

    def change_password(self, old_password: str, new_password: str) -> None:
        """Re-wrap the MEK under a new password (instant; no file re-encryption)."""
        problem = check_password_policy(new_password)
        if problem:
            raise VaultError(problem)
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
            return decrypt_blob(blob, self._key)
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


def write_sealed_if_active(path: str | Path, data: bytes) -> bool:
    """Write *data* to *path*, sealing it when the vault is enabled+unlocked.

    Unlike :func:`write_bytes` the decision is based on the vault *state*
    rather than on the path being under the vault root: callers that stage
    files in a directory of their own choosing (restore staging, database
    snapshots) must still get an encrypted file.  Returns True when the data
    was sealed.

    Like :func:`write_bytes` this fails safe: with the vault enabled but
    locked it raises :class:`VaultLocked` instead of dropping a plaintext
    copy onto disk.
    """
    v = get_vault()
    target = Path(path)
    if not v.enabled:
        atomic_write_bytes(target, data)
        return False
    if not v.unlocked:
        raise VaultLocked('The vault is locked.')
    atomic_write_bytes(target, v.encrypt(data))
    return True


def write_sealed_bytes(path: str | Path, data: bytes) -> None:
    """Write *data* encrypted regardless of policy (the seal pass)."""
    atomic_write_bytes(Path(path), get_vault().encrypt(data))


def open_image(path: str | Path):
    """Return a fully loaded PIL image for a data file (decrypted as needed).

    The pixel count is checked *before* the image is decoded: a small file can
    expand into hundreds of megabytes of RAM (a 10 000 x 10 000 PNG is 380 KiB
    on disk and 381 MiB decoded).
    """
    from src import image_safety

    data = read_bytes(path)
    img = image_safety.open_checked(io.BytesIO(data))
    try:
        return image_safety.load_checked(img)
    except Exception:
        img.close()
        raise


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
    directories = [base / name for name in _MIGRATION_DIRS]
    # The restore staging area is created with a random suffix, so it is
    # found by prefix.
    try:
        for entry in base.iterdir():
            if entry.is_dir() and entry.name.startswith(_MIGRATION_DIR_PREFIXES):
                directories.append(entry)
    except OSError:
        pass
    for directory in directories:
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
    _check_seal_root(root)
    changed, failed = _transform_all_files(seal_file, root, progress, cancelled)
    if failed == 0:
        _clear_migration_flag()
    return changed, failed


def unseal_all_files(
    root: Optional[Path] = None,
    progress: Optional[Callable[[Path], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> tuple[int, int]:
    """Decrypt every sealed data file; returns ``(changed, failed)``."""
    _check_seal_root(root)
    return _transform_all_files(unseal_file, root, progress, cancelled)


def _check_seal_root(root: Optional[Path]) -> None:
    """Warn when a directory other than the vault's own root is sealed.

    The vault's key belongs to one data directory.  Sealing (or unsealing)
    a *different* directory with it would write files the other library
    could never open.  The call sites use the vault's own root, so this is
    a guard rail rather than an error: a restore into a directory that is
    not the vault root is a legitimate (if unusual) setup.
    """
    if root is None:
        return
    base = Path(root)
    try:
        expected = get_vault().root.resolve()
        actual = base.resolve()
    except OSError:
        return
    if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
        logger.warning(
            'Sealing a directory that is not the vault root (%s != %s); the '
            'files will only be readable with this vault.',
            actual, expected,
        )


def migration_pending() -> bool:
    """True when a vault was enabled but the sealing pass never finished.

    The envelope is written before the files are sealed, so a crash (or a
    cancelled migration) in between leaves the library half in plaintext.
    The flag makes that state visible instead of silent.
    """
    try:
        meta = get_vault()._load_meta(required=False)
    except VaultError:
        return False
    return bool(meta and meta.get('migration_pending'))


def _clear_migration_flag() -> None:
    """Record that the sealing pass finished."""
    vault = get_vault()
    try:
        meta = vault._load_meta(required=False)
    except VaultError:
        return
    if not meta or not meta.get('migration_pending'):
        return
    meta['migration_pending'] = False
    try:
        vault._save_meta(meta)
    except OSError:
        logger.warning('Could not clear the vault migration flag', exc_info=True)


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
