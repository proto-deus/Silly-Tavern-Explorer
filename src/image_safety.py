"""Guards against decompression bombs in untrusted images.

Pillow's own ``MAX_IMAGE_PIXELS`` default is 89 478 485 pixels.  It only
*warns* below twice that limit and raises above it, so a 10 000 x 10 000 PNG
(380 KiB on disk) sails through the check and then decodes to 381 MiB of RAM -
a ~1000x amplification.  One crafted gallery image or card is enough to
exhaust the process.

Every image the app decodes comes from a file the user (or a SillyTavern
import) supplied, so the limit is enforced explicitly *before* ``load()``
rather than relying on Pillow's warning.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Maximum number of pixels an image may have before it is refused.
#: 40 MP is far above any real character card (typically under 4 MP) while
#: capping the decode at roughly 160 MiB of RGBA pixels.
DEFAULT_MAX_IMAGE_PIXELS = 40_000_000

#: Pillow's own ceiling.  Kept in step with ours so Pillow raises its
#: ``DecompressionBombError`` at the same point we raise ours, and so a
#: malformed header cannot make Pillow allocate before we get to check.
PILLOW_MAX_IMAGE_PIXELS = DEFAULT_MAX_IMAGE_PIXELS


class ImageTooLargeError(Exception):
    """The image is too large to decode safely."""


def max_image_pixels() -> int:
    """The configured pixel ceiling.

    ``ST_EXPLORER_MAX_IMAGE_PIXELS`` overrides it (useful for a user with
    genuinely huge source art, and for the test suite).
    """
    raw = os.environ.get('ST_EXPLORER_MAX_IMAGE_PIXELS', '').strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            logger.warning("Ignoring an invalid ST_EXPLORER_MAX_IMAGE_PIXELS=%r", raw)
        else:
            if value > 0:
                return value
    return DEFAULT_MAX_IMAGE_PIXELS


def apply_limits() -> None:
    """Apply the pixel ceiling to Pillow itself."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return
    Image.MAX_IMAGE_PIXELS = max_image_pixels()


def check_size(width: int, height: int, limit: Optional[int] = None) -> None:
    """Raise :class:`ImageTooLargeError` when the image exceeds the ceiling."""
    ceiling = limit if limit is not None else max_image_pixels()
    pixels = int(width) * int(height)
    if pixels > ceiling:
        raise ImageTooLargeError(
            f'The image is {width}x{height} pixels ({pixels:,}), which is over '
            f'the {ceiling:,}-pixel limit.'
        )


def open_checked(source, limit: Optional[int] = None):
    """Open an image and check its size *before* the pixels are decoded.

    ``PIL.Image.open`` only reads the header, so the size check is free; the
    expensive ``load()`` happens later and is where the memory is used.

    Pillow's own :class:`PIL.Image.DecompressionBombError` is translated into
    :class:`ImageTooLargeError` so callers only have to handle one exception.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(source)
    except Image.DecompressionBombError as exc:
        raise ImageTooLargeError(str(exc)) from exc
    except UnidentifiedImageError:
        raise
    try:
        check_size(img.width, img.height, limit)
    except ImageTooLargeError:
        img.close()
        raise
    return img


def load_checked(img, limit: Optional[int] = None):
    """Check the size of an already-open image and then decode it."""
    check_size(img.width, img.height, limit)
    img.load()
    return img


def open_image_file(path: str | Path, limit: Optional[int] = None):
    """Open an image from disk with the size check applied."""
    return open_checked(str(path), limit)


# Pillow is configured as soon as this module is imported so that no code
# path can decode an oversized image before the check is in place.
apply_limits()
