"""Small standard-library PNG reader/writer for RGB/RGBA diagnostic images."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_PIXELS = 40_000_000


@dataclass
class Image:
    width: int
    height: int
    data: bytearray
    channels: int = 3

    def rgb(self, x: int, y: int) -> tuple[int, int, int]:
        offset = (y * self.width + x) * self.channels
        return self.data[offset], self.data[offset + 1], self.data[offset + 2]

    def sample_cell(self, x: int, y: int) -> int:
        red, green, blue = self.rgb(x, y)
        return (4 if red >= 128 else 0) | (2 if green >= 128 else 0) | (1 if blue >= 128 else 0)


def _chunk(kind: bytes, body: bytes) -> bytes:
    checksum = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", checksum)


def write_png(path: str | Path, image: Image) -> None:
    if image.channels != 3 or len(image.data) != image.width * image.height * 3:
        raise ValueError("PNG writer expects tightly packed RGB image data")
    scanlines = bytearray()
    stride = image.width * 3
    for row in range(image.height):
        scanlines.append(0)
        start = row * stride
        scanlines.extend(image.data[start : start + stride])
    header = struct.pack(">IIBBBBB", image.width, image.height, 8, 2, 0, 0, 0)
    encoded = PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(scanlines, 6)) + _chunk(b"IEND", b"")
    Path(path).write_bytes(encoded)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png(path: str | Path) -> Image:
    raw_file = Path(path).read_bytes()
    if not raw_file.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG file")
    pos = len(PNG_SIGNATURE)
    width = height = bit_depth = color_type = None
    interlace = 0
    idat = bytearray()
    palette: bytes | None = None
    transparency: bytes | None = None
    while pos + 12 <= len(raw_file):
        size = struct.unpack_from(">I", raw_file, pos)[0]
        kind = raw_file[pos + 4 : pos + 8]
        start = pos + 8
        end = start + size
        if end + 4 > len(raw_file):
            raise ValueError("truncated PNG chunk")
        body = raw_file[start:end]
        actual_crc = struct.unpack_from(">I", raw_file, end)[0]
        if (zlib.crc32(kind + body) & 0xFFFFFFFF) != actual_crc:
            raise ValueError("PNG chunk checksum mismatch")
        if kind == b"IHDR":
            if size != 13:
                raise ValueError("invalid PNG header")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            if compression != 0 or filtering != 0:
                raise ValueError("unsupported PNG compression or filter method")
        elif kind == b"IDAT":
            idat.extend(body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"IEND":
            break
        pos = end + 4

    if width is None or height is None or bit_depth != 8:
        raise ValueError("only 8-bit PNG images are supported")
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("PNG dimensions are invalid or exceed the image limit")
    if interlace != 0:
        raise ValueError("interlaced PNG images are not supported")
    channel_counts = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if color_type not in channel_counts:
        raise ValueError(f"unsupported PNG color type {color_type}")
    if color_type == 3 and (palette is None or len(palette) % 3):
        raise ValueError("indexed PNG has no valid palette")
    source_channels = channel_counts[color_type]
    stride = width * source_channels
    try:
        decoded = zlib.decompress(idat)
    except zlib.error as exc:
        raise ValueError("PNG image data is corrupt") from exc
    expected_size = height * (stride + 1)
    if len(decoded) != expected_size:
        raise ValueError("PNG scanline data is incomplete")

    rows: list[bytearray] = []
    previous = bytearray(stride)
    cursor = 0
    for _ in range(height):
        filter_type = decoded[cursor]
        cursor += 1
        line = bytearray(decoded[cursor : cursor + stride])
        cursor += stride
        if filter_type > 4:
            raise ValueError("invalid PNG row filter")
        for i in range(stride):
            left = line[i - source_channels] if i >= source_channels else 0
            above = previous[i]
            upper_left = previous[i - source_channels] if i >= source_channels else 0
            if filter_type == 1:
                line[i] = (line[i] + left) & 0xFF
            elif filter_type == 2:
                line[i] = (line[i] + above) & 0xFF
            elif filter_type == 3:
                line[i] = (line[i] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                line[i] = (line[i] + _paeth(left, above, upper_left)) & 0xFF
        rows.append(line)
        previous = line

    rgb = bytearray(width * height * 3)
    out = 0
    for row in rows:
        if color_type == 2:
            rgb[out : out + width * 3] = row
            out += width * 3
        elif color_type == 6:
            for i in range(0, len(row), 4):
                rgb[out : out + 3] = row[i : i + 3]
                out += 3
        elif color_type == 0:
            for gray in row:
                rgb[out : out + 3] = bytes((gray, gray, gray))
                out += 3
        elif color_type == 4:
            for i in range(0, len(row), 2):
                gray = row[i]
                rgb[out : out + 3] = bytes((gray, gray, gray))
                out += 3
        else:  # indexed color
            for index in row:
                entry = index * 3
                if entry + 2 >= len(palette or b""):
                    raise ValueError("PNG pixel index is outside its palette")
                rgb[out : out + 3] = palette[entry : entry + 3]
                out += 3
    return Image(width, height, rgb)
