import ctypes
import unittest

from reader.capture import X11Image, XImage, XImageFunctions


_PixelCallback = ctypes.CFUNCTYPE(
    ctypes.c_ulong, ctypes.POINTER(XImage), ctypes.c_int, ctypes.c_int
)


@_PixelCallback
def _red_pixel(_image, _x, _y):
    return 0x00FF0000


class X11ImageTests(unittest.TestCase):
    def test_pixel_accessor_is_read_from_inline_ximage_functions(self):
        raw = XImage()
        raw.width = 1
        raw.height = 1
        raw.red_mask = 0x00FF0000
        raw.green_mask = 0x0000FF00
        raw.blue_mask = 0x000000FF
        raw.functions = XImageFunctions(
            None,
            None,
            ctypes.cast(_red_pixel, ctypes.c_void_p).value,
            None,
            None,
            None,
        )

        image = X11Image(1, 1, None, ctypes.pointer(raw))
        self.assertEqual(image.sample_cell(0, 0), 4)


if __name__ == "__main__":
    unittest.main()
