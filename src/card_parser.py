from __future__ import annotations

import base64
import copy
import io
import json
import logging
import struct
import zlib
from pathlib import Path
from typing import Optional

from PIL import Image

from src.fs_utils import atomic_write_bytes

logger = logging.getLogger(__name__)

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'

# Upper bound on decompressed text-chunk payloads. A crafted card could
# otherwise act as a decompression bomb (a few MB of compressed data
# expanding to gigabytes of RAM).
_MAX_DECOMPRESSED_TEXT = 32 * 1024 * 1024


class PNGParseError(Exception):
    """Raised when a PNG file is malformed or cannot be parsed."""


def _read_chunk(data: bytes, offset: int) -> tuple[str, bytes, int]:
    if offset + 8 > len(data):
        raise PNGParseError(f"Truncated chunk header at offset {offset}")
    length = struct.unpack('>I', data[offset:offset + 4])[0]
    offset += 4
    chunk_type = data[offset:offset + 4].decode('ascii', errors='replace')
    offset += 4
    chunk_end = offset + length
    if chunk_end + 4 > len(data):
        raise PNGParseError(f"Truncated chunk data for '{chunk_type}' (length {length})")
    chunk_data = data[offset:chunk_end]
    offset = chunk_end
    offset += 4  # CRC
    return chunk_type, chunk_data, offset


def _build_chunk(chunk_type: str, chunk_data: bytes) -> bytes:
    type_bytes = chunk_type.encode('ascii')
    length = struct.pack('>I', len(chunk_data))
    crc = struct.pack('>I', zlib.crc32(type_bytes + chunk_data) & 0xFFFFFFFF)
    return length + type_bytes + chunk_data + crc


def extract_chunks(png_data: bytes) -> list[dict]:
    if png_data[:8] != PNG_SIGNATURE:
        raise PNGParseError("Not a valid PNG file")
    chunks = []
    offset = 8
    while offset < len(png_data):
        try:
            chunk_type, chunk_data, offset = _read_chunk(png_data, offset)
        except PNGParseError:
            logger.warning("Stopped reading chunks at offset %d due to truncation", offset)
            break
        chunks.append({'name': chunk_type, 'data': chunk_data})
        if chunk_type == 'IEND':
            break
    return chunks


def encode_chunks(chunks: list[dict]) -> bytes:
    output = PNG_SIGNATURE
    for chunk in chunks:
        output += _build_chunk(chunk['name'], chunk['data'])
    return output


def _parse_text_chunk(data: bytes) -> tuple[str, str]:
    null_idx = data.find(b'\x00')
    if null_idx == -1:
        # No null separator — treat the whole chunk as the keyword
        return data.decode('latin-1', errors='replace'), ''
    keyword = data[:null_idx].decode('latin-1', errors='replace')
    text_value = data[null_idx + 1:].decode('latin-1', errors='replace')
    return keyword, text_value


def _parse_ztxt_chunk(data: bytes) -> tuple[str, str]:
    """Parse a zTXt chunk: keyword\\x00 compression_method compressed_data."""
    null_idx = data.find(b'\x00')
    if null_idx == -1:
        return data.decode('latin-1', errors='replace'), ''
    keyword = data[:null_idx].decode('latin-1', errors='replace')
    rest = data[null_idx + 1:]
    if len(rest) < 2:
        return keyword, ''
    # compression_method is a single byte (0 = zlib/deflate)
    compression_method = rest[0]
    compressed = rest[1:]
    if compression_method != 0:
        logger.warning("Unsupported zTXt compression method: %d", compression_method)
        return keyword, ''
    try:
        decompressed = _safe_decompress(compressed)
        if decompressed is None:
            logger.warning("zTXt chunk exceeds %d byte decompression limit", _MAX_DECOMPRESSED_TEXT)
            return keyword, ''
        return keyword, decompressed.decode('latin-1', errors='replace')
    except zlib.error as e:
        logger.warning("Failed to decompress zTXt chunk: %s", e)
        return keyword, ''


def _parse_itxt_chunk(data: bytes) -> tuple[str, str]:
    """Parse an iTXt chunk (international text), handling optional compression."""
    null_idx = data.find(b'\x00')
    if null_idx == -1:
        return data.decode('utf-8', errors='replace'), ''
    keyword = data[:null_idx].decode('latin-1', errors='replace')
    rest = data[null_idx + 1:]
    # iTXt format: compression_flag (1) compression_method (1) language_tag (cstr) translated_keyword (cstr) text
    if len(rest) < 2:
        return keyword, ''
    compression_flag = rest[0]
    compression_method = rest[1]
    rest = rest[2:]
    # language tag (null-terminated)
    lang_end = rest.find(b'\x00')
    if lang_end == -1:
        return keyword, ''
    rest = rest[lang_end + 1:]
    # translated keyword (null-terminated)
    trans_end = rest.find(b'\x00')
    if trans_end == -1:
        return keyword, ''
    text_bytes = rest[trans_end + 1:]
    if compression_flag == 1:
        if compression_method != 0:
            logger.warning("Unsupported iTXt compression method: %d", compression_method)
            return keyword, ''
        try:
            decompressed = _safe_decompress(text_bytes)
            if decompressed is None:
                logger.warning("iTXt chunk exceeds %d byte decompression limit", _MAX_DECOMPRESSED_TEXT)
                return keyword, ''
            text_bytes = decompressed
        except zlib.error as e:
            logger.warning("Failed to decompress iTXt chunk: %s", e)
            return keyword, ''
    return keyword, text_bytes.decode('utf-8', errors='replace')


def _safe_decompress(data: bytes, max_size: int = _MAX_DECOMPRESSED_TEXT) -> Optional[bytes]:
    """zlib-decompress *data*, refusing payloads that expand beyond *max_size*."""
    decompressor = zlib.decompressobj()
    out = decompressor.decompress(data, max_size + 1)
    if len(out) > max_size or not decompressor.eof and decompressor.unconsumed_tail:
        return None
    return out


def _find_card_text(chunks: list[dict]) -> Optional[tuple[str, str]]:
    chara_entry = None
    for chunk in chunks:
        name = chunk['name']
        if name == 'tEXt':
            keyword, text_value = _parse_text_chunk(chunk['data'])
        elif name == 'zTXt':
            keyword, text_value = _parse_ztxt_chunk(chunk['data'])
        elif name == 'iTXt':
            keyword, text_value = _parse_itxt_chunk(chunk['data'])
        else:
            continue
        if keyword == 'ccv3':
            return 'ccv3', text_value
        if keyword == 'chara' and chara_entry is None:
            chara_entry = ('chara', text_value)
    return chara_entry


def _decode_card_text(text_value: str) -> Optional[dict]:
    """Decode a base64+JSON card payload, returning None on any failure."""
    stripped = text_value.rstrip('=').strip()
    if not stripped or len(stripped) % 4 == 1:
        logger.warning("Character card base64 looks malformed (len=%d)", len(stripped))
    try:
        decoded = base64.b64decode(text_value, validate=False)
        return json.loads(decoded)
    except (base64.binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("Failed to decode character card data: %s", e)
        return None


def read_chara_card(png_path: str | Path) -> Optional[dict]:
    try:
        png_data = Path(png_path).read_bytes()
        chunks = extract_chunks(png_data)
    except (PNGParseError, OSError) as e:
        logger.warning("Failed to read PNG chunks from %s: %s", png_path, e)
        return None
    result = _find_card_text(chunks)
    if result is not None:
        keyword, text_value = result
        card = _decode_card_text(text_value)
        if card is not None:
            return card
        if keyword == 'ccv3':
            # A corrupt ccv3 chunk must not shadow a valid chara chunk:
            # fall back to the V2 data before giving up.
            logger.warning("Corrupt ccv3 chunk in %s; trying chara fallback", png_path)
            for chunk in chunks:
                name = chunk['name']
                if name == 'tEXt':
                    kw, text_value = _parse_text_chunk(chunk['data'])
                elif name == 'zTXt':
                    kw, text_value = _parse_ztxt_chunk(chunk['data'])
                elif name == 'iTXt':
                    kw, text_value = _parse_itxt_chunk(chunk['data'])
                else:
                    continue
                if kw == 'chara':
                    card = _decode_card_text(text_value)
                    if card is not None:
                        return card
        return None
    return None


def read_card_from_json(json_path: str | Path) -> Optional[dict]:
    """Read character card data from a JSON file.

    Validates that the file contains a dict with at least a ``name`` field or
    a ``spec`` field (V2/V3 spec).  Returns ``None`` on any read/parse error
    or if the content doesn't look like a character card.
    """
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning("Failed to read JSON card from %s: %s", json_path, e)
        return None
    if not isinstance(data, dict):
        logger.warning("JSON card data in %s is not a dict", json_path)
        return None
    if not (data.get('name') or data.get('spec') or (data.get('data') and isinstance(data['data'], dict) and data['data'].get('name'))):
        logger.warning("JSON card in %s has no name or spec field", json_path)
        return None
    return data


def read_card_data(path: str | Path) -> Optional[dict]:
    """Read character card data from a PNG or JSON file (dispatch by suffix)."""
    p = Path(path)
    if p.suffix.lower() == '.json':
        return read_card_from_json(p)
    return read_chara_card(p)


def get_card_image(png_path: str | Path) -> Optional[Image.Image]:
    try:
        with Image.open(png_path) as img:
            return img.convert('RGBA')
    except Exception as e:
        # Log (not silently swallow): corrupt images should be diagnosable
        # from the app log.
        logger.warning("Could not load image %s: %s", png_path, e)
        return None


def get_card_thumbnail(png_path: str | Path, size: tuple[int, int] = (200, 200)) -> Optional[Image.Image]:
    img = get_card_image(png_path)
    if img is None:
        return None
    img.thumbnail(size, Image.Resampling.LANCZOS)
    return img


def save_thumbnail(png_path: str | Path, thumb_path: str | Path, size: tuple[int, int] = (200, 200)) -> bool:
    thumb = get_card_thumbnail(png_path, size)
    if thumb is None:
        return False
    Path(thumb_path).parent.mkdir(parents=True, exist_ok=True)
    thumb.save(thumb_path, 'PNG')
    return True


def write_chara_card(
    png_path: str | Path,
    output_path: str | Path,
    card_data: dict,
    keyword: str = 'chara',
) -> None:
    png_data = Path(png_path).read_bytes()
    chunks = extract_chunks(png_data)

    json_bytes = json.dumps(card_data, ensure_ascii=False).encode('utf-8')
    b64_text = base64.b64encode(json_bytes).decode('ascii')

    text_chunk_data = keyword.encode('latin-1') + b'\x00' + b64_text.encode('latin-1')

    # Remove existing chunks with the target keyword in *any* text encoding
    # (tEXt, zTXt, iTXt) so a stale compressed chunk can't shadow the new
    # data on read-back.
    filtered_chunks = []
    for chunk in chunks:
        if chunk['name'] == 'tEXt':
            existing_keyword, _ = _parse_text_chunk(chunk['data'])
        elif chunk['name'] == 'zTXt':
            existing_keyword, _ = _parse_ztxt_chunk(chunk['data'])
        elif chunk['name'] == 'iTXt':
            existing_keyword, _ = _parse_itxt_chunk(chunk['data'])
        else:
            existing_keyword = None
        if existing_keyword == keyword:
            continue
        filtered_chunks.append(chunk)

    iend_idx = _find_iend_index(filtered_chunks)
    if iend_idx is None:
        filtered_chunks.append({'name': 'IEND', 'data': b''})
        iend_idx = len(filtered_chunks) - 1
    filtered_chunks.insert(iend_idx, {'name': 'tEXt', 'data': text_chunk_data})

    _write_png_atomic(output_path, encode_chunks(filtered_chunks))


def replace_card_image(
    source_png: str | Path,
    new_image_path: str | Path,
    output_path: str | Path,
) -> None:
    """Replace the image of a character card while preserving embedded card data.

    All ancillary chunks from the original file (``tEXt``, ``zTXt``,
    ``iTXt``, and other non-pixel chunks such as ICC profiles) are
    carried over into the new image, so card data stored in compressed
    or internationalized text chunks survives the image swap.
    """
    source_data = Path(source_png).read_bytes()
    chunks = extract_chunks(source_data)

    # Preserve everything except pixel-data and image-boundary chunks.
    skip = {'IDAT', 'PLTE', 'tRNS', 'IHDR', 'IEND', 'bKGD', 'hIST', 'sBIT'}
    preserved_chunks = [c for c in chunks if c['name'] not in skip]

    new_img = Image.open(new_image_path).convert('RGBA')
    buf = io.BytesIO()
    new_img.save(buf, format='PNG')
    new_png_data = buf.getvalue()

    new_chunks = extract_chunks(new_png_data)
    final_chunks = []
    for c in new_chunks:
        if c['name'] == 'IEND':
            final_chunks.extend(preserved_chunks)
        final_chunks.append(c)

    _write_png_atomic(output_path, encode_chunks(final_chunks))


def _find_iend_index(chunks: list[dict]) -> int | None:
    for i, c in enumerate(chunks):
        if c['name'] == 'IEND':
            return i
    return None


def write_chara_card_dual(
    png_path: str | Path,
    output_path: str | Path,
    card_data: dict,
) -> None:
    """Write both 'chara' (v2) and 'ccv3' (v3) tEXt chunks for maximum compatibility."""
    png_data = Path(png_path).read_bytes()
    chunks = extract_chunks(png_data)

    # Remove existing chara and ccv3 chunks (tEXt, zTXt, or iTXt)
    filtered_chunks = []
    for chunk in chunks:
        if chunk['name'] in ('tEXt', 'zTXt', 'iTXt'):
            if chunk['name'] == 'tEXt':
                existing_keyword, _ = _parse_text_chunk(chunk['data'])
            elif chunk['name'] == 'zTXt':
                existing_keyword, _ = _parse_ztxt_chunk(chunk['data'])
            else:
                existing_keyword, _ = _parse_itxt_chunk(chunk['data'])
            if existing_keyword in ('chara', 'ccv3'):
                continue
        filtered_chunks.append(chunk)

    # Find IEND (or append one if missing)
    iend_idx = _find_iend_index(filtered_chunks)
    if iend_idx is None:
        filtered_chunks.append({'name': 'IEND', 'data': b''})
        iend_idx = len(filtered_chunks) - 1

    # Build v2 chunk
    v2_data = copy.deepcopy(card_data)
    v2_data['spec'] = 'chara_card_v2'
    v2_data['spec_version'] = '2.0'
    v2_json = json.dumps(v2_data, ensure_ascii=False).encode('utf-8')
    v2_b64 = base64.b64encode(v2_json).decode('ascii')
    v2_chunk_data = b'chara\x00' + v2_b64.encode('latin-1')

    # Build v3 chunk
    v3_data = copy.deepcopy(card_data)
    v3_data['spec'] = 'chara_card_v3'
    v3_data['spec_version'] = '3.0'
    v3_json = json.dumps(v3_data, ensure_ascii=False).encode('utf-8')
    v3_b64 = base64.b64encode(v3_json).decode('ascii')
    v3_chunk_data = b'ccv3\x00' + v3_b64.encode('latin-1')

    # Insert both before IEND
    filtered_chunks.insert(iend_idx, {'name': 'tEXt', 'data': v2_chunk_data})
    filtered_chunks.insert(iend_idx + 1, {'name': 'tEXt', 'data': v3_chunk_data})

    _write_png_atomic(output_path, encode_chunks(filtered_chunks))


def _write_png_atomic(output_path: str | Path, output: bytes) -> None:
    """Validate the rebuilt PNG, then atomically replace *output_path*.

    The validation step guarantees a truncated/corrupt chunk stream is
    never written over a good file; the atomic replace guarantees a
    crash or disk-full error mid-write cannot destroy the original.
    """
    try:
        with Image.open(io.BytesIO(output)) as check_img:
            check_img.verify()
    except Exception as e:
        raise PNGParseError(f"Refusing to write invalid PNG output: {e}") from e
    atomic_write_bytes(output_path, output)
