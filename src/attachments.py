from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

TEXT_EXTENSIONS = {
    '.txt', '.md', '.markdown', '.json', '.csv', '.log',
    '.py', '.js', '.ts', '.html', '.htm', '.css', '.yaml', '.yml',
    '.toml', '.ini', '.cfg',
}
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}

DEFAULT_MAX_TEXT_CHARS = 20000
DEFAULT_MAX_IMAGE_DIM = 1024
DEFAULT_IMAGE_QUALITY = 80


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def is_text(path: str | Path) -> bool:
    return Path(path).suffix.lower() in TEXT_EXTENSIONS


def prepare_text_attachment(
    path: str | Path,
    max_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> dict | None:
    """Read a text file into an attachment dict.

    Returns ``{'kind', 'name', 'data'}`` or ``None`` on failure.  Content is
    decoded as UTF-8 (with replacement) and truncated to *max_chars* with a
    trailing note.  Only enough bytes to cover *max_chars* are read from
    disk — attaching a multi-GB log never loads it fully into memory.
    Pure function so it can be unit-tested without Qt.
    """
    p = Path(path)
    try:
        # UTF-8 worst case is 4 bytes/char; over-read slightly so the
        # truncation note still fits within max_chars after decode.
        with p.open('rb') as f:
            raw = f.read(max_chars * 4 + 4096)
    except OSError as e:
        logger.warning("Could not read attachment %s: %s", p, e)
        return None
    text = raw.decode('utf-8', errors='replace')
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncated to {max_chars} characters]"
    return {'kind': 'text', 'name': p.name, 'data': text}


def prepare_image_attachment(
    path: str | Path,
    max_dim: int = DEFAULT_MAX_IMAGE_DIM,
    quality: int = DEFAULT_IMAGE_QUALITY,
) -> dict | None:
    """Downscale + JPEG-encode an image into a base64 attachment dict.

    Returns ``{'kind', 'name', 'data', 'mime'}`` or ``None`` on failure.  The
    image is EXIF-rotated (phone photos), alpha-composited onto white
    instead of black, resized so its longest side is *max_dim*, and
    re-encoded as JPEG to keep session files small.
    """
    p = Path(path)
    try:
        with Image.open(p) as img:
            # Honour the EXIF orientation tag before any transform.
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
            if img.mode in ('RGBA', 'LA', 'P'):
                # Composite transparency onto white: convert('RGB') alone
                # would flatten transparent areas to black.
                rgba = img.convert('RGBA')
                background = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
                img = Image.alpha_composite(background, rgba).convert('RGB')
            else:
                img = img.convert('RGB')
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=quality)
            data = base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception as e:  # Pillow raises various exceptions for bad files
        logger.warning("Could not prepare image attachment %s: %s", p, e)
        return None
    return {'kind': 'image', 'name': p.name, 'data': data, 'mime': 'image/jpeg'}


def prepare_attachment(path: str | Path) -> dict | None:
    """Dispatch on file extension to the correct attachment preparer.

    Unknown binary types (PDFs, office documents, …) are rejected rather
    than being sent as mojibake "text".
    """
    p = Path(path)
    if is_image(p):
        return prepare_image_attachment(p)
    if is_text(p):
        return prepare_text_attachment(p)
    logger.warning("Unsupported attachment type: %s", p.suffix or p.name)
    return None
