"""Per-character gallery images, global VN backdrops and ST expression images.

Three distinct stores live here:

* **Gallery** — extra images kept with the library under
  ``~/.st-explorer/gallery/<char_id>/``. Files go through the vault
  transparent I/O like every other library file, and their metadata
  (caption, keywords, order) lives in the ``gallery_images`` table.
* **Backdrops** — shared visual-novel backdrops under
  ``~/.st-explorer/backdrops/``, usable by every character. Same storage
  rules as the gallery; metadata lives in the ``backdrop_images`` table.
* **Expressions** — SillyTavern expression images read and written
  directly in the ST user-images directory
  (``data/<user>/user/images/<charName>/<expression>.<ext>``). These are
  plain files outside the vault root on purpose: SillyTavern must be able
  to read them without our key. Copies out of the library therefore
  decrypt on read and write plain.

Pure filesystem/dict logic: no Qt imports.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Optional

from src import vault

logger = logging.getLogger(__name__)

# SillyTavern's default expression set (the emotion classes the built-in
# expression picker offers first). Anything else found in a character's
# expression folder is a custom expression and is shown after these.
DEFAULT_EXPRESSIONS: tuple[str, ...] = (
    'anger', 'annoyed', 'care', 'confused', 'crying', 'cute', 'daily_clothes',
    'dejected', 'doubt', 'excited', 'happy', 'hat_glasses', 'laughing', 'like',
    'love', 'mid_delta', 'misconduct', 'netorare', 'naughty', 'normal', 'pride',
    'rage', 'sad', 'shame', 'shock', 'sleep', 'surprise', 'tsundere',
    'turn_away', 'wink',
)

IMAGE_EXTENSIONS: tuple[str, ...] = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')

_EXPR_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def is_image_file(path: str | Path) -> bool:
    """True when *path* has a supported image extension (case-insensitive)."""
    return Path(str(path)).suffix.lower() in IMAGE_EXTENSIONS


def _base_data_dir() -> Path:
    from src.app_paths import data_dir
    return data_dir()


def get_gallery_dir(char_id: int) -> Path:
    """Return (creating) the on-disk gallery directory for *char_id*."""
    d = _base_data_dir() / 'gallery' / str(char_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def gallery_image_path(char_id: int, file_name: str) -> Path:
    """Full path of a stored gallery image.

    ``Path(...).name`` strips any directory part a caller might pass, so a
    crafted ``file_name`` can never escape the character's gallery folder.
    ``safe_filename_component`` additionally rejects ``'.'`` / ``'..'``,
    which survive ``Path(...).name`` and would resolve to the folder itself
    or its parent.  An unusable name maps to the gallery directory (never a
    file), which every caller already treats as "missing".
    """
    from src.fs_utils import safe_filename_component
    return get_gallery_dir(char_id) / safe_filename_component(file_name)


def content_hash(data: bytes) -> str:
    """Hex SHA-256 of raw file bytes, used for import deduplication."""
    return hashlib.sha256(data).hexdigest()


def sanitize_image_filename(name: str) -> str:
    """Sanitize a source filename for storage in a gallery folder.

    The trailing dots/spaces are stripped *after* the truncation: Windows
    silently drops them when creating the file, so a name truncated to end in
    ``.`` would be stored in the database under a name that does not exist on
    disk (and a second import would overwrite the first image).
    """
    cleaned = re.sub(r'[<>:"/\\|?*]', '_', name or '')
    cleaned = cleaned.strip('. ')
    cleaned = cleaned[:100]
    # Strip again: the truncation may have exposed a trailing dot or space.
    cleaned = cleaned.strip('. ')
    return cleaned or 'image.png'


def unique_gallery_filename(directory: Path, filename: str) -> str:
    """Return a non-colliding filename inside *directory*.

    Mirrors the lorebook naming convention: ``name_1``, ``name_2``, ...
    until free.  Comparison is case-folded because the default macOS and
    Windows volumes treat ``A.png`` and ``a.png`` as the same file.
    """
    base = sanitize_image_filename(filename)
    candidate = base
    stem = Path(base).stem
    suffix = Path(base).suffix
    existing = {p.name.casefold() for p in directory.iterdir()} if directory.is_dir() else set()
    n = 1
    while candidate.casefold() in existing:
        candidate = f'{stem}_{n}{suffix}'
        n += 1
    return candidate


# ---- Gallery (library-local) -------------------------------------------


def import_gallery_image(char_id: int, source_path: str | Path) -> dict:
    """Copy one image into the character's gallery.

    Returns a metadata dict ``{file_name, content_hash}`` describing the
    stored file.  Raises ``ValueError`` for non-image sources and the
    usual OSError/vault errors on failure.
    """
    src = Path(source_path)
    if not is_image_file(src):
        raise ValueError(f'Not a supported image file: {src.name}')
    data = vault.read_bytes(src)
    return import_gallery_bytes(char_id, data, src.name)


def sniff_image_extension(data: bytes) -> Optional[str]:
    """Return the file extension matching *data*'s image format, or None.

    Pillow is imported lazily so this module stays importable without it
    (it is only needed for images that arrive without a usable extension).
    """
    try:
        from io import BytesIO

        from PIL import Image
        with Image.open(BytesIO(data)) as img:
            fmt = (img.format or '').lower()
    except Exception:
        return None
    return {
        'jpeg': '.jpg', 'png': '.png', 'gif': '.gif',
        'webp': '.webp', 'bmp': '.bmp',
    }.get(fmt)


def import_gallery_bytes(char_id: int, data: bytes, name: str = 'image.png') -> dict:
    """Store raw image *data* in the character's gallery.

    The in-memory counterpart of :func:`import_gallery_image`, for images
    that never existed as a file (chat attachments, downloaded URL images).
    Returns the same ``{file_name, content_hash}`` metadata dict.  When
    *name* carries no supported image extension the right one is derived
    from the data itself; unrecognised data raises ``ValueError``.
    """
    if not data:
        raise ValueError('No image data')
    if Path(name or '').suffix.lower() not in IMAGE_EXTENSIONS:
        suffix = sniff_image_extension(data)
        if suffix is None:
            raise ValueError('Not a supported image')
        name = f'{name or "image"}{suffix}'
    digest = content_hash(data)
    directory = get_gallery_dir(char_id)
    file_name = unique_gallery_filename(directory, name)
    vault.write_bytes(directory / file_name, data)
    return {'file_name': file_name, 'content_hash': digest}


def delete_gallery_file(char_id: int, file_name: str) -> bool:
    """Delete one stored gallery image; True when a file was removed."""
    path = gallery_image_path(char_id, file_name)
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError as e:
        logger.warning("Could not delete gallery image %s: %s", path, e)
    return False


def delete_gallery_dir(char_id: int) -> None:
    """Remove a deleted card's whole gallery folder."""
    import shutil
    directory = _base_data_dir() / 'gallery' / str(char_id)
    if not directory.is_dir():
        return
    try:
        shutil.rmtree(directory)
    except OSError as e:
        logger.warning("Could not delete gallery folder %s: %s", directory, e)


def migrate_gallery_files(old_id: int, new_id: int) -> dict[str, str]:
    """Move ``gallery/<old_id>/`` to ``gallery/<new_id>/``.

    Mirrors ``migrate_chat_sessions``: when a card is superseded during
    sync and re-imported under a new row id, its gallery belongs to the
    character and must follow it.

    Returns the renames as ``{old_name: new_name}``: a file whose name
    collided with one already in the target folder is renamed on disk, and
    the gallery row that points at it has to be updated as well - otherwise
    the row shows the *other* card's image.
    """
    if old_id == new_id:
        return {}
    base = _base_data_dir() / 'gallery'
    old_dir = base / str(old_id)
    new_dir = base / str(new_id)
    if not old_dir.is_dir():
        return {}
    try:
        if not new_dir.exists():
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            old_dir.rename(new_dir)
            return {}
        from src.fs_utils import move_contents_into
        renames = move_contents_into(old_dir, new_dir)
        import shutil
        shutil.rmtree(old_dir, ignore_errors=True)
        mapping = {old_name: new_name for old_name, new_name in renames}
        for old_name, new_name in mapping.items():
            logger.warning("Gallery file '%s' was merged as '%s' during the move %s -> %s",
                           old_name, new_name, old_dir, new_dir)
        return mapping
    except OSError as e:
        logger.warning("Could not migrate gallery files from %s to %s: %s", old_dir, new_dir, e)
        return {}


def export_gallery_file(char_id: int, file_name: str, dest_path: str | Path) -> bool:
    """Copy one gallery image out as plaintext to *dest_path*."""
    src = gallery_image_path(char_id, file_name)
    if not src.is_file():
        return False
    vault.copy_out(src, dest_path)
    return True


# ---- Global backdrops (library-local, shared) ---------------------------


def get_backdrops_dir() -> Path:
    """Return (creating) the on-disk directory for global VN backdrops."""
    d = _base_data_dir() / 'backdrops'
    d.mkdir(parents=True, exist_ok=True)
    return d


def backdrop_image_path(file_name: str) -> Path:
    """Full path of a stored global backdrop.

    Same traversal guards as :func:`gallery_image_path`: the name is reduced
    to its final component and checked against ``'.'`` / ``'..'`` so a
    crafted name can never escape the backdrops folder.
    """
    from src.fs_utils import safe_filename_component
    return get_backdrops_dir() / safe_filename_component(file_name)


def import_backdrop_image(source_path: str | Path) -> dict:
    """Copy one image into the global backdrop library.

    Returns a metadata dict ``{file_name, content_hash}`` describing the
    stored file.  Raises ``ValueError`` for non-image sources and the
    usual OSError/vault errors on failure.
    """
    src = Path(source_path)
    if not is_image_file(src):
        raise ValueError(f'Not a supported image file: {src.name}')
    data = vault.read_bytes(src)
    return import_backdrop_bytes(data, src.name)


def import_backdrop_bytes(data: bytes, name: str = 'image.png') -> dict:
    """Store raw image *data* in the global backdrop library.

    The in-memory counterpart of :func:`import_backdrop_image`, for images
    that never existed as a file.  Same extension sniffing and naming rules
    as the per-character gallery import.
    """
    if not data:
        raise ValueError('No image data')
    if Path(name or '').suffix.lower() not in IMAGE_EXTENSIONS:
        suffix = sniff_image_extension(data)
        if suffix is None:
            raise ValueError('Not a supported image')
        name = f'{name or "image"}{suffix}'
    digest = content_hash(data)
    directory = get_backdrops_dir()
    file_name = unique_gallery_filename(directory, name)
    vault.write_bytes(directory / file_name, data)
    return {'file_name': file_name, 'content_hash': digest}


def delete_backdrop_file(file_name: str) -> bool:
    """Delete one stored global backdrop; True when a file was removed."""
    path = backdrop_image_path(file_name)
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError as e:
        logger.warning("Could not delete backdrop image %s: %s", path, e)
    return False


def export_backdrop_file(file_name: str, dest_path: str | Path) -> bool:
    """Copy one global backdrop out as plaintext to *dest_path*."""
    src = backdrop_image_path(file_name)
    if not src.is_file():
        return False
    vault.copy_out(src, dest_path)
    return True


# ---- Expressions (SillyTavern user images) -----------------------------


def resolve_user_images_dir(characters_dir: str | Path) -> Path:
    """Derive ST's user-images directory from the configured characters dir.

    ST lays these out as siblings under ``data/<user>/``:
    ``.../characters`` <-> ``.../user/images``.
    """
    return Path(characters_dir).parent / 'user' / 'images'


def configured_user_images_dir() -> Optional[Path]:
    """Return the ST user-images dir from app settings (``None`` when unset).

    The explicitly configured ``st/user_images_path`` wins; otherwise the
    directory is derived from ``st/characters_path``.  Shared by the Gallery
    tab and the Test tab's expression sprites so both resolve the folder the
    same way.
    """
    from src.settings_manager import load_st_characters_path, load_st_user_images_path
    configured = load_st_user_images_path().strip()
    if configured:
        return Path(configured)
    characters = load_st_characters_path().strip()
    if characters:
        return resolve_user_images_dir(characters)
    return None


def expression_stem_for(st_avatar_url: Optional[str], card_name: str) -> str:
    """Return the ST folder/file stem that identifies a character's images.

    A linked card's avatar URL is authoritative (it is the name ST itself
    uses, including any ``_1`` de-collision suffix); otherwise fall back to
    the ST-conventional filename stem for the card's name.
    """
    if st_avatar_url:
        stem = Path(str(st_avatar_url).replace('\\', '/')).stem.strip()
        # ``Path('..').stem`` and friends survive as '.' / '..' and would
        # resolve outside the user-images tree once joined.
        if stem and stem not in ('.', '..'):
            return stem
    from src.sillytavern_sync import sanitize_st_filename
    return sanitize_st_filename(card_name or '')


def expression_images_dir(user_images_dir: str | Path, char_stem: str) -> Optional[Path]:
    """Locate ``<user_images>/<char_stem>/`` with a case-insensitive match.

    Returns ``None`` when no matching folder exists; callers that want to
    *create* one should use ``expression_write_dir``.  The *actual* on-disk
    entry is returned, not the requested spelling: on a case-insensitive
    volume ``base / 'aria vale'`` resolves inside ``Aria Vale``, and writing
    through the resolved path would then miss the folder in listings.
    """
    base = Path(user_images_dir)
    if not base.is_dir() or not char_stem:
        return None
    key = char_stem.casefold()
    for entry in base.iterdir():
        if entry.is_dir() and entry.name.casefold() == key:
            return entry
    return None


def expression_write_dir(user_images_dir: str | Path, char_stem: str) -> Path:
    """Return the expression folder for *char_stem*, creating it if needed."""
    from src.fs_utils import safe_filename_component
    safe_stem = safe_filename_component(char_stem)
    if not safe_stem:
        raise ValueError(f'Invalid expression folder name: {char_stem!r}')
    existing = expression_images_dir(user_images_dir, safe_stem)
    if existing is not None:
        return existing
    directory = Path(user_images_dir) / safe_stem
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def sanitize_expression_name(name: str) -> str:
    """Sanitize an expression name for use as a filename stem."""
    cleaned = _EXPR_ILLEGAL_CHARS.sub('', name or '')
    cleaned = cleaned.strip('. ')
    return cleaned[:64]


def find_expression_file(exp_dir: str | Path, name: str) -> Optional[Path]:
    """Return the image file backing expression *name*, or None.

    Matching is case-insensitive on the filename stem; the first hit in
    sorted order wins when several extensions exist for one name.
    """
    directory = Path(exp_dir)
    if not directory.is_dir() or not name:
        return None
    key = sanitize_expression_name(name).casefold()
    if not key:
        return None
    for path in sorted(directory.iterdir()):
        if path.is_file() and is_image_file(path) and path.stem.casefold() == key:
            return path
    return None


def list_expression_images(exp_dir: str | Path) -> dict[str, Path]:
    """Map expression name (lowercased) -> image file for one character.

    Unknown/unreadable entries are skipped rather than breaking the list.
    """
    directory = Path(exp_dir)
    result: dict[str, Path] = {}
    if not directory.is_dir():
        return result
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not is_image_file(path):
            continue
        key = path.stem.casefold()
        if key and key not in result:
            result[key] = path
    return result


def write_expression_image(exp_dir: str | Path, name: str, source_path: str | Path) -> Path:
    """Write (or replace) one expression image as a plain file.

    The source is read through the vault so an encrypted gallery file can
    be used directly; the destination is always plain because SillyTavern
    reads these files without our key.  The new file is written *before* any
    previous image for the same expression name is removed (``happy.png`` +
    ``happy.jpg`` coexisting for one call is harmless; deleting first and
    then failing the copy would leave the slot empty).
    """
    clean = sanitize_expression_name(name)
    if not clean:
        raise ValueError('Expression name cannot be empty')
    directory = Path(exp_dir)
    directory.mkdir(parents=True, exist_ok=True)
    src = Path(source_path)
    ext = src.suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        raise ValueError(f'Not a supported image file: {src.name}')
    existing = find_expression_file(directory, clean)
    dest = directory / f'{clean}{ext}'
    vault.copy_out(src, dest)
    if existing is not None and existing != dest:
        try:
            existing.unlink()
        except OSError as e:
            logger.warning("Could not remove previous expression image %s: %s", existing, e)
    return dest


def delete_expression_image(exp_dir: str | Path, name: str) -> bool:
    """Delete the image behind expression *name*; True when one was removed."""
    path = find_expression_file(exp_dir, name)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        logger.warning("Could not delete expression image %s: %s", path, e)
        return False


# ---- V3 card assets (read-only display) --------------------------------


def asset_description(entry: dict) -> str:
    """One-line summary of a V3 ``assets`` entry for list display."""
    if not isinstance(entry, dict):
        return ''
    kind = str(entry.get('type') or 'asset')
    name = str(entry.get('name') or '')
    ext = str(entry.get('ext') or '').lstrip('.')
    label = f'{name}.{ext}' if name and ext else (name or str(entry.get('uri') or ''))
    return f'{kind} — {label}' if label else kind


def _within(base: Path, target: Path) -> bool:
    """True when *target* resolves to a path inside *base*.

    Used to keep a card-controlled relative path from escaping the folder it
    is supposed to live in.  ``Path.resolve()`` collapses ``..`` and makes
    absolute paths absolute, so both traversal and an injected drive letter
    are caught.
    """
    try:
        root = base.resolve()
        resolved = target.resolve()
    except OSError:
        return False
    if resolved == root:
        return True
    return str(resolved).startswith(str(root) + os.sep)


def resolve_asset_path(
    entry: dict,
    card_dir: str | Path,
    user_images_dir: str | Path | None = None,
    char_stem: str = '',
) -> Optional[Path]:
    """Best-effort resolution of a V3 asset ``uri`` to a file on disk.

    Tries, in order: the literal ``uri`` (or ``assets/<uri>`` when it has no
    directory part) next to the card, then the named file inside the
    character's ST user-images folder.  Returns None when nothing matches -
    the caller shows a placeholder rather than guessing further.

    The ``uri`` and ``name`` come from the imported card, so they are
    untrusted: every candidate is checked to stay inside the folder it is
    supposed to be in.  Without that check a card could point at
    ``C:/Windows/win.ini`` and the gallery would happily display it.
    """
    if not isinstance(entry, dict):
        return None
    uri = str(entry.get('uri') or '').strip().replace('\\', '/')
    name = str(entry.get('name') or '').strip()
    ext = str(entry.get('ext') or '').lstrip('.')
    base = Path(card_dir)

    candidates: list[tuple[Path, Path]] = []   # (candidate, folder it must stay in)
    if uri and uri != 'ccdefault':
        rel = Path(uri)
        candidates.append((base / rel, base))
        if len(rel.parts) == 1:
            candidates.append((base / 'assets' / rel, base))
    if name:
        file_name = f'{name}.{ext}' if ext else name
        if user_images_dir and char_stem:
            folder = expression_images_dir(user_images_dir, char_stem)
            if folder is not None:
                candidates.append((folder / file_name, folder))
        candidates.append((base / file_name, base))
        candidates.append((base / 'assets' / file_name, base))

    for candidate, allowed_root in candidates:
        # Untrusted input: the candidate must stay inside its folder.
        if not _within(allowed_root, candidate):
            logger.warning(
                "Refusing to resolve a V3 asset outside %s: %s", allowed_root, candidate
            )
            continue
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None
