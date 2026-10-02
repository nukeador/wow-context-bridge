from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from reader.png import Image, read_png, write_png
from reader.protocol import DecodeResult, decode_image
from reader.synthetic import make_synthetic_image


class ImageDecodeTests(unittest.TestCase):
    def test_synthetic_offset_noise_gamma_and_scaled_cells(self) -> None:
        cases = ((1, 38, 3.28125, 0.88, 0.95), (1, 38, 3.25, 0.88, 0.95), (13, 9, 3.0, 0.72, 0.92), (13, 9, 4.0, 0.72, 0.92), (17, 11, 4.25, 1.32, 0.78), (8, 21, 5.5, 1.1, 0.84), (1, 38, 4.375, 0.88, 0.95))
        expected = {"player": "Aíra", "zone": "Elwynn Forest", "subzone": "Goldshire", "target": ""}
        for ox, oy, size, gamma, gain in cases:
            with self.subTest(offset=(ox, oy), cell_size=size):
                synthetic = make_synthetic_image(
                    expected,
                    offset_x=ox,
                    offset_y=oy,
                    cell_size=size,
                    gamma=gamma,
                    gain=gain,
                    noise=12,
                    seed=19,
                )
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / "sample.png"
                    write_png(path, synthetic)
                    decoded_image = read_png(path)
                result = decode_image(decoded_image, search_px=max(24, oy + 4))
                self.assertEqual(result.status, "valid", result.reason)
                self.assertEqual(result.frame.context, expected)

    def test_bottom_left_strip(self) -> None:
        expected = {"player": "Aíra", "zone": "Zone", "target": "NPC", "subzone": "", "nearby": ["Мария"], "nearby_total": 1}
        image = make_synthetic_image(expected, width=1280, height=800,
                                     offset_x=1, offset_y=770, cell_size=4.375,
                                     noise=12, gamma=0.88, gain=0.95)
        result = decode_image(image)
        self.assertEqual(result.status, "valid", result.reason)
        self.assertEqual(result.frame.context, expected)

    def test_blank_screenshot_is_not_found(self) -> None:
        blank = Image(200, 100, bytearray((32, 34, 38)) * (200 * 100))
        self.assertEqual(decode_image(blank, search_px=10).status, "not_found")

    def test_incomplete_screenshot_is_reported(self) -> None:
        complete = make_synthetic_image(width=960, height=540, offset_y=0, noise=0)
        cropped = Image(complete.width, 5, complete.data[: complete.width * 5 * 3])
        result = decode_image(cropped, search_px=20)
        self.assertEqual(result.status, "incomplete", result.reason)

    def test_corrupted_screenshot_fails_checksum(self) -> None:
        image = make_synthetic_image(noise=0)
        # Change the color of one data cell while retaining a valid magic/header.
        col = 30
        x = round(13 + (col + 0.5) * 4)
        y = round(9 + 2)
        cell_color = image.rgb(x, y)
        replacement = bytes((0, 0, 0)) if cell_color[0] > 127 else bytes((255, 255, 255))
        x0, x1 = 13 + col * 4, 13 + (col + 1) * 4
        y0, y1 = 9, 13
        for py in range(y0, y1):
            for px in range(x0, x1):
                offset = (py * image.width + px) * 3
                image.data[offset : offset + 3] = replacement
        result = decode_image(image, search_px=20)
        self.assertEqual(result.status, "malformed")
        self.assertIn("checksum", result.reason)


if __name__ == "__main__":
    unittest.main()
