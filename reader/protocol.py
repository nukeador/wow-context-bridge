"""Versioned pixel packet codec and tolerant screenshot decoder."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

MAGIC = b"\xD3\x71"
VERSION = 1
MAX_PAYLOAD = 256
COLUMNS = 128
HEADER_SIZE = 7
TRAILER_SIZE = 2
MAX_PACKET_SIZE = HEADER_SIZE + MAX_PAYLOAD + TRAILER_SIZE


class ProtocolError(ValueError):
    """A packet is present but fails protocol validation."""


@dataclass(frozen=True)
class Frame:
    sequence: int
    payload: bytes
    context: dict[str, Any]
    packet: bytes


@dataclass(frozen=True)
class DecodeResult:
    status: str
    frame: Frame | None = None
    reason: str | None = None
    origin: tuple[int, int] | None = None
    cell_size: float | None = None


def fletcher16(data: bytes) -> bytes:
    s1 = 0
    s2 = 0
    for value in data:
        s1 = (s1 + value) % 255
        s2 = (s2 + s1) % 255
    return bytes((s1, s2))


def utf8_safe_truncate(value: str, byte_limit: int) -> str:
    """Return a valid UTF-8 prefix no longer than byte_limit bytes."""
    normalized = value.encode("utf-8", errors="replace").decode("utf-8")
    out: list[str] = []
    used = 0
    for char in normalized:
        char_size = len(char.encode("utf-8"))
        if used + char_size > byte_limit:
            break
        out.append(char)
        used += char_size
    return "".join(out)


def normalize_context(context: dict[str, Any], max_payload: int = MAX_PAYLOAD) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key in ("player", "zone", "subzone", "target"):
        value = context.get(key, "")
        if not isinstance(value, str):
            raise ProtocolError(f"context field {key!r} must be a string")
        value = "".join(" " if ord(char) < 32 else char for char in value)
        fields[key] = utf8_safe_truncate(value, max_payload)

    nearby = None
    if "nearby" in context:
        values = context["nearby"]
        if not isinstance(values, list) or len(values) > 40 or any(not isinstance(n, str) for n in values):
            raise ProtocolError("nearby must be a list of at most 40 names")
        nearby = sorted(set(utf8_safe_truncate("".join(" " if ord(c) < 32 else c for c in n), 96) for n in values) - {""})
        total = context.get("nearby_total", len(nearby))
        if type(total) is not int or not len(nearby) <= total <= 40:
            raise ProtocolError("invalid nearby_total")
        fields.update(nearby=[], nearby_total=total)

    def serialize() -> bytes:
        return json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    payload = serialize()
    while len(payload) > max_payload:
        key = max(("player", "zone", "subzone", "target"), key=lambda name: len(fields[name].encode("utf-8")))
        if not fields[key]:
            raise ProtocolError("context cannot fit in maximum payload")
        fields[key] = fields[key][:-1]
        payload = serialize()
    if nearby is not None:
        for name in nearby:
            if len(fields["nearby"]) >= 8:
                break
            fields["nearby"].append(name)
            if len(serialize()) > max_payload:
                fields["nearby"].pop()
    return fields


def encode_context(context: dict[str, Any], sequence: int) -> bytes:
    if not 0 <= sequence <= 0xFFFF:
        raise ProtocolError("sequence must fit in 16 bits")
    fields = normalize_context(context)
    payload = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encode_packet(sequence, payload, version=2 if "nearby" in fields else 1)


def encode_packet(sequence: int, payload: bytes, version: int = VERSION) -> bytes:
    if not 0 <= sequence <= 0xFFFF:
        raise ProtocolError("sequence must fit in 16 bits")
    if not 0 <= version <= 0xFF:
        raise ProtocolError("version must fit in 8 bits")
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError(f"payload exceeds {MAX_PAYLOAD} bytes")
    header = MAGIC + bytes((version,)) + sequence.to_bytes(2, "big") + len(payload).to_bytes(2, "big")
    body = header[2:] + payload
    return header + payload + fletcher16(body)


def decode_packet(packet: bytes) -> Frame:
    if len(packet) < HEADER_SIZE + TRAILER_SIZE:
        raise ProtocolError("incomplete packet header")
    if packet[:2] != MAGIC:
        raise ProtocolError("bad magic")
    version = packet[2]
    if version not in (1, 2):
        raise ProtocolError(f"unsupported protocol version {version}")
    sequence = int.from_bytes(packet[3:5], "big")
    payload_size = int.from_bytes(packet[5:7], "big")
    if payload_size > MAX_PAYLOAD:
        raise ProtocolError(f"payload length {payload_size} exceeds limit {MAX_PAYLOAD}")
    expected_size = HEADER_SIZE + payload_size + TRAILER_SIZE
    if len(packet) < expected_size:
        raise ProtocolError(f"incomplete packet: expected {expected_size} bytes, got {len(packet)}")
    if len(packet) != expected_size:
        raise ProtocolError("packet has trailing bytes")
    payload = packet[HEADER_SIZE : HEADER_SIZE + payload_size]
    actual_checksum = packet[-TRAILER_SIZE:]
    expected_checksum = fletcher16(packet[2 : HEADER_SIZE + payload_size])
    if actual_checksum != expected_checksum:
        raise ProtocolError("checksum mismatch")
    try:
        decoded = json.loads(payload.decode("utf-8", errors="strict"))
    except UnicodeDecodeError as exc:
        raise ProtocolError("payload is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ProtocolError("payload is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("payload JSON must be an object")
    context: dict[str, Any] = {}
    for key in ("player", "zone", "subzone", "target"):
        value = decoded.get(key, "")
        if not isinstance(value, str):
            raise ProtocolError(f"context field {key!r} must be a string")
        context[key] = value
    if version == 2:
        names = decoded.get("nearby")
        total = decoded.get("nearby_total")
        if not isinstance(names, list) or len(names) > 8 or any(not isinstance(n, str) or not n or len(n.encode("utf-8")) > 96 for n in names):
            raise ProtocolError("invalid nearby names")
        if len(set(names)) != len(names) or type(total) is not int or not len(names) <= total <= 40:
            raise ProtocolError("invalid nearby_total or duplicate names")
        context.update(nearby=names, nearby_total=total)
    return Frame(sequence, payload, context, packet)


def bytes_to_cells(data: bytes) -> list[int]:
    cells: list[int] = []
    accumulator = 0
    bit_count = 0
    for byte in data:
        accumulator = (accumulator << 8) | byte
        bit_count += 8
        while bit_count >= 3:
            shift = bit_count - 3
            cells.append((accumulator >> shift) & 0b111)
            bit_count = shift
            accumulator &= (1 << bit_count) - 1 if bit_count else 0
    if bit_count:
        cells.append((accumulator << (3 - bit_count)) & 0b111)
    return cells


def _read_bytes(
    sample: Callable[[int, int], int],
    width: int,
    height: int,
    origin_x: int,
    origin_y: int,
    cell_size: float,
    count: int,
) -> bytes | None:
    cell_count = (count * 8 + 2) // 3
    out = bytearray()
    accumulator = 0
    bit_count = 0
    for index in range(cell_count):
        col = index % COLUMNS
        row = index // COLUMNS
        x = round(origin_x + (col + 0.5) * cell_size)
        y = round(origin_y + (row + 0.5) * cell_size)
        if x < 0 or y < 0 or x >= width or y >= height:
            return None
        accumulator = (accumulator << 3) | sample(x, y)
        bit_count += 3
        while bit_count >= 8 and len(out) < count:
            shift = bit_count - 8
            out.append((accumulator >> shift) & 0xFF)
            bit_count = shift
            accumulator &= (1 << bit_count) - 1 if bit_count else 0
    if len(out) != count:
        return None
    return bytes(out)


def _try_candidate(image: Any, ox: int, oy: int, cell_size: float) -> DecodeResult | None:
    magic_data = _read_bytes(image.sample_cell, image.width, image.height, ox, oy, cell_size, 2)
    if magic_data is None:
        return None
    if magic_data != MAGIC:
        return None
    header = _read_bytes(image.sample_cell, image.width, image.height, ox, oy, cell_size, HEADER_SIZE)
    if header is None:
        return DecodeResult("incomplete", reason="truncated packet header", origin=(ox, oy), cell_size=cell_size)
    version = header[2]
    if version not in (1, 2):
        return DecodeResult("malformed", reason=f"unsupported protocol version {version}", origin=(ox, oy), cell_size=cell_size)
    payload_size = int.from_bytes(header[5:7], "big")
    if payload_size > MAX_PAYLOAD:
        return DecodeResult("malformed", reason=f"payload length {payload_size} exceeds limit {MAX_PAYLOAD}", origin=(ox, oy), cell_size=cell_size)
    packet_size = HEADER_SIZE + payload_size + TRAILER_SIZE
    packet = _read_bytes(image.sample_cell, image.width, image.height, ox, oy, cell_size, packet_size)
    if packet is None:
        return DecodeResult("incomplete", reason="truncated packet body", origin=(ox, oy), cell_size=cell_size)
    try:
        frame = decode_packet(packet)
    except ProtocolError as exc:
        return DecodeResult("malformed", reason=str(exc), origin=(ox, oy), cell_size=cell_size)
    return DecodeResult("valid", frame, origin=(ox, oy), cell_size=cell_size)


def decode_image(
    image: Any,
    *,
    search_px: int = 64,
    hint: tuple[int, int, float] | None = None,
) -> DecodeResult:
    """Find and decode a strip near the image's top-left or bottom-left corner.

    `image` supplies width, height, and sample_cell(x, y) -> 3-bit color value.
    A prior successful geometry hint is tried first for inexpensive repeated reads.
    """
    if hint is not None:
        hinted = _try_candidate(image, hint[0], hint[1], hint[2])
        if hinted is not None and hinted.status == "valid":
            return hinted

    # Search 2.5..8 px cells in eighth-pixel steps. Fractional scaling on
    # Gamescope can produce sizes such as 4.375 px; quarter-pixel steps miss it.
    # Origins are searched near either left corner; bottom margin includes strip height.
    sizes = [v / 8 for v in range(20, 65)]
    sizes.sort(key=lambda value: abs(value - 4.0))
    incomplete: DecodeResult | None = None
    top_rows = list(range(min(search_px, max(0, image.height - 1)) + 1))
    bottom_rows = range(max(0, image.height - search_px - 64), image.height)
    origins_y = top_rows + [y for y in bottom_rows if y not in top_rows]
    for oy in origins_y:
        for ox in range(min(search_px, max(0, image.width - 1)) + 1):
            for size in sizes:
                candidate = _try_candidate(image, ox, oy, size)
                if candidate is None:
                    continue
                if candidate.status == "valid":
                    return candidate
                # Coarse magic can match while small cells drift across a long
                # row. Refine only promising origins rather than every pixel.
                for step in range(-16, 17):
                    refined_size = size + step / 128
                    if step == 0 or not 2.5 <= refined_size <= 8:
                        continue
                    refined = _try_candidate(image, ox, oy, refined_size)
                    if refined is not None and refined.status == "valid":
                        return refined
                if candidate.status in ("malformed", "incomplete") and incomplete is None:
                    incomplete = candidate
    if incomplete is not None:
        return incomplete
    return DecodeResult("not_found", reason="no protocol magic found in search area")
