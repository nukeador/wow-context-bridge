"""Generate a synthetic screenshot containing a protocol-v1 pixel strip."""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from .png import Image, write_png
from .protocol import COLUMNS, bytes_to_cells, encode_context


def make_synthetic_image(
    context: dict[str, str] | None = None,
    *,
    sequence: int = 7,
    width: int = 960,
    height: int = 540,
    offset_x: int = 13,
    offset_y: int = 9,
    cell_size: float = 4.0,
    gamma: float = 1.15,
    gain: float = 0.86,
    noise: int = 10,
    seed: int = 41,
) -> Image:
    if context is None:
        context = {
            "player": "Aíra",
            "zone": "Elwynn Forest",
            "subzone": "Goldshire",
            "target": "",
        }
    image = Image(width, height, bytearray((24, 29, 34)) * (width * height), 3)
    cells = bytes_to_cells(encode_context(context, sequence))
    randomizer = random.Random(seed)
    for index, value in enumerate(cells):
        col, row = index % COLUMNS, index // COLUMNS
        x0 = round(offset_x + col * cell_size)
        x1 = round(offset_x + (col + 1) * cell_size)
        y0 = round(offset_y + row * cell_size)
        y1 = round(offset_y + (row + 1) * cell_size)
        color = (255 if value & 4 else 0, 255 if value & 2 else 0, 255 if value & 1 else 0)
        color = tuple(min(255, round(((channel / 255) ** gamma) * gain * 255)) for channel in color)
        for y in range(y0, min(y1, height)):
            for x in range(x0, min(x1, width)):
                offset = (y * width + x) * 3
                for channel in range(3):
                    delta = randomizer.randint(-noise, noise) if noise else 0
                    image.data[offset + channel] = max(0, min(255, color[channel] + delta))
    return image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--player", default="Aíra")
    parser.add_argument("--zone", default="Elwynn Forest")
    parser.add_argument("--subzone", default="Goldshire")
    parser.add_argument("--target", default="")
    parser.add_argument("--sequence", type=int, default=7)
    parser.add_argument("--offset-x", type=int, default=13)
    parser.add_argument("--offset-y", type=int, default=9)
    parser.add_argument("--cell-size", type=float, default=4.0)
    parser.add_argument("--gamma", type=float, default=1.15)
    parser.add_argument("--gain", type=float, default=0.86)
    parser.add_argument("--noise", type=int, default=10)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    args = parser.parse_args()
    context = {"player": args.player, "zone": args.zone, "subzone": args.subzone, "target": args.target}
    image = make_synthetic_image(
        context,
        sequence=args.sequence,
        width=args.width,
        height=args.height,
        offset_x=args.offset_x,
        offset_y=args.offset_y,
        cell_size=args.cell_size,
        gamma=args.gamma,
        gain=args.gain,
        noise=args.noise,
        seed=args.seed,
    )
    write_png(args.output, image)
    print(f"Wrote synthetic screenshot to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
