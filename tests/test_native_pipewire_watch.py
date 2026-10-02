from __future__ import annotations

import os
import struct
import unittest

from reader.native_pipewire_watch import FRAME_HEADER, _read_exact, _read_frame, parse_frame_header


class NativeFramePipeTests(unittest.TestCase):
    def test_parse_frame_header(self) -> None:
        header = FRAME_HEADER.pack(b"DCPF", 1088, 128, 123456789)
        self.assertEqual(parse_frame_header(header), (1088, 128, 123456789))

    def test_reject_bad_magic_and_oversize_dimensions(self) -> None:
        with self.assertRaisesRegex(ValueError, "magic"):
            parse_frame_header(FRAME_HEADER.pack(b"NOPE", 10, 10, 1))
        with self.assertRaisesRegex(ValueError, "dimensions"):
            parse_frame_header(FRAME_HEADER.pack(b"DCPF", 1089, 10, 1))

    def test_read_rgb_frame_from_pipe(self) -> None:
        read_fd, write_fd = os.pipe()
        pixels = bytes((255, 0, 0, 0, 255, 0))
        try:
            os.write(write_fd, FRAME_HEADER.pack(b"DCPF", 2, 1, 99) + pixels)
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(_read_frame(read_fd, 0.1), (2, 1, 99, pixels))
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def test_partial_frame_does_not_block_forever(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"x")
            with self.assertRaisesRegex(ValueError, "timed out"):
                _read_exact(read_fd, 4, timeout=0.01)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_report_truncated_frame(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, FRAME_HEADER.pack(b"DCPF", 2, 1, 99) + b"rgb")
            os.close(write_fd)
            write_fd = -1
            with self.assertRaisesRegex(ValueError, "truncated native RGB"):
                _read_frame(read_fd, 0.1)
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
