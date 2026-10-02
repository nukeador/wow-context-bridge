"""Decode an explicitly saved native BGRx snapshot without capturing."""
import argparse
import json
from pathlib import Path
from .png import Image
from .protocol import decode_image

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path)
    args = parser.parse_args()
    metadata = json.loads(args.prefix.with_suffix(".json").read_text())
    raw = args.prefix.with_suffix(".raw").read_bytes()
    width, height = metadata["width"], metadata["height"]
    if metadata["format_id"] != 8 or len(metadata["planes"]) != 1:
        parser.error("only single-plane BGRx snapshots are supported")
    plane = metadata["planes"][0]
    stride, offset = plane["stride"], plane["chunk_offset"]
    if not 0 < width <= 8192 or not 0 < height <= 8192 or stride < width * 4 or offset < 0 or offset + (height - 1) * stride + width * 4 > len(raw):
        parser.error("invalid snapshot dimensions or bounds")
    rgb = bytearray(width * height * 3)
    for y in range(height):
        row = raw[offset + y * stride:offset + y * stride + width * 4]
        start = y * width * 3
        rgb[start:start + width * 3:3] = row[2::4]
        rgb[start + 1:start + width * 3:3] = row[1::4]
        rgb[start + 2:start + width * 3:3] = row[0::4]
    result = decode_image(Image(width, height, rgb))
    print(json.dumps({"status": result.status, "reason": result.reason,
                      "origin": result.origin, "cell_size": result.cell_size,
                      "context": result.frame.context if result.frame else None}, ensure_ascii=False))
    return 0 if result.status == "valid" else 2

if __name__ == "__main__":
    raise SystemExit(main())
