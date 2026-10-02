# Pixel protocol v1 and v2

All integers in the packet are unsigned big-endian. The stream is packed most-significant-bit first into consecutive 3-bit color values. Each value is shown as one 3×3-pixel square in WoW Context Bridge 0.1.0. Its bits select full-intensity R, G and B channels: `value = R*4 + G*2 + B`. The eight possible colors are black, blue, green, cyan, red, magenta, yellow and white. There is no interpolation or text rendering in the data strip.

## Packet

| Offset | Size | Meaning |
| --- | ---: | --- |
| 0 | 2 | Magic `D3 71` |
| 2 | 1 | Protocol version, `01` for core context, `02` for nameplate context |
| 3 | 2 | Message sequence number, wraps at 65535 |
| 5 | 2 | Payload length in bytes, maximum 256 |
| 7 | N | UTF-8 JSON payload |
| 7+N | 2 | Fletcher-16 bytes `s1, s2` |

Fletcher-16 covers bytes from version through the last payload byte (offset 2 through `6+N`). Initialize `s1=s2=0`; for each byte, set `s1=(s1+byte) mod 255`, then `s2=(s2+s1) mod 255`. The packet is exactly `9+N` bytes. Readers reject unknown versions, over-limit lengths, truncated packets, invalid UTF-8, invalid JSON and checksum mismatches.

Payload fields are JSON strings in this order: `player`, `zone`, `subzone`, `target`. An absent target is the empty string. UTF-8 bytes are not ASCII-folded or transliterated. The addon trims only at a UTF-8 character boundary to keep the JSON payload within 256 bytes.

## Display layout and freshness

- WoW Context Bridge 0.1.0 anchors the frame at the top-left of the WoW screen content; cells run left-to-right, top-to-bottom.
- Cells are laid out row-major, 128 columns wide. The addon reserves textures only for the current packet and reuses them.
- The addon's frame scale aims for three physical pixels per cell when a screen-height API is available. A reader searches near both left corners (with an additional 64 pixels for strip height at the bottom) and cell sizes from 2.5 through 8 pixels in 1/8-pixel steps to tolerate capture offsets and fractional Gamescope scaling. Capture geometry depends on resolution and scaling.
- Sequence increments on a context change and on a three-second heartbeat. Thus a frozen screenshot eventually becomes stale even if the values did not change.
- A reader treats a repeated sequence as a duplicate, and treats the context as stale after eight seconds without a new sequence by default. Identical context with a new heartbeat refreshes liveness without printing another context record.

The checksum detects common capture errors, not adversarial modification. The format is a local optical transport, not encryption or authentication.

## Version 2 nameplate vocabulary

V2 keeps the same framing, checksum, geometry and 256-byte payload maximum. In addition to the four core strings, it requires `nearby` (up to eight nonempty unique UTF-8 strings, each at most 96 bytes) and `nearby_total` (integer from the transmitted count through 40). V1 remains accepted.

The addon scans nameplate1 through nameplate40 using guarded UnitName reads. It omits secret values before string operations, normalizes controls to spaces, trims at UTF-8 boundaries, deduplicates and sorts names. The total is the readable unique count from this scan; it is not a census or distance measurement. The encoder reserves empty-array/count metadata, fits the core fields, then adds whole normalized names while space permits. Overflow names are omitted without batch rotation. Add/remove/name events are coalesced for 250 ms; heartbeat refreshes the snapshot every three seconds. Removed entries disappear on the next refresh.
