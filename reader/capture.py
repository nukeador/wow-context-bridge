"""Capture backends. Decoding is kept independent in protocol.py."""

from __future__ import annotations

import ctypes
import ctypes.util
from dataclasses import dataclass, field

from .png import Image


class CaptureError(RuntimeError):
    pass


class XImageFunctions(ctypes.Structure):
    _fields_ = [
        ("create_image", ctypes.c_void_p),
        ("destroy_image", ctypes.c_void_p),
        ("get_pixel", ctypes.c_void_p),
        ("put_pixel", ctypes.c_void_p),
        ("sub_image", ctypes.c_void_p),
        ("add_pixel", ctypes.c_void_p),
    ]


class XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.POINTER(ctypes.c_char)),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("obdata", ctypes.c_void_p),
        # Xlib embeds the function table at the end of XImage; it is not a
        # pointer to a separate table.
        ("functions", XImageFunctions),
    ]


class XClassHint(ctypes.Structure):
    _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]


@dataclass
class X11Image:
    width: int
    height: int
    lib: ctypes.CDLL
    image: ctypes.POINTER(XImage)
    _get_pixel: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        functions = self.image.contents.functions
        pixel_fn = functions.get_pixel
        if not pixel_fn:
            raise CaptureError("X11 image has no pixel accessor")
        self._get_pixel = ctypes.CFUNCTYPE(
            ctypes.c_ulong, ctypes.POINTER(XImage), ctypes.c_int, ctypes.c_int
        )(pixel_fn)

    def sample_cell(self, x: int, y: int) -> int:
        if x < 0 or y < 0 or x >= self.width or y >= self.height:
            raise IndexError("pixel is outside captured X11 image")
        value = int(self._get_pixel(self.image, x, y))
        raw = self.image.contents
        return (
            (4 if self._channel(value, raw.red_mask) >= 128 else 0)
            | (2 if self._channel(value, raw.green_mask) >= 128 else 0)
            | (1 if self._channel(value, raw.blue_mask) >= 128 else 0)
        )

    @staticmethod
    def _channel(value: int, mask: int) -> int:
        if not mask:
            return 0
        shift = (mask & -mask).bit_length() - 1
        maximum = mask >> shift
        return ((value & mask) >> shift) * 255 // maximum

    def to_rgb_image(self) -> Image:
        out = bytearray(self.width * self.height * 3)
        raw = self.image.contents
        offset = 0
        for y in range(self.height):
            for x in range(self.width):
                pixel = int(self._get_pixel(self.image, x, y))
                out[offset] = self._channel(pixel, raw.red_mask)
                out[offset + 1] = self._channel(pixel, raw.green_mask)
                out[offset + 2] = self._channel(pixel, raw.blue_mask)
                offset += 3
        return Image(self.width, self.height, out)

    def close(self) -> None:
        if not self.image:
            return
        functions = self.image.contents.functions
        destroy_image = functions.destroy_image
        if not destroy_image:
            self.image = ctypes.POINTER(XImage)()
            return
        destroy = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(XImage))(destroy_image)
        destroy(self.image)
        self.image = ctypes.POINTER(XImage)()


class X11Capture:
    ALL_PLANES = ctypes.c_ulong(-1).value
    ZPIXMAP = 2
    MAX_WIDTH = 1200
    MAX_HEIGHT = 160

    def __init__(self, window_class: str | None = "Wow.exe", window_name: str | None = None):
        library = ctypes.util.find_library("X11")
        if not library:
            raise CaptureError("libX11 was not found")
        self.lib = ctypes.CDLL(library)
        self._configure()
        self._error_count = 0
        self._error_callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self._error_callback = self._error_callback_type(self._on_x_error)
        self._previous_error_handler = self.lib.XSetErrorHandler(ctypes.cast(self._error_callback, ctypes.c_void_p))
        self.display = self.lib.XOpenDisplay(None)
        if not self.display:
            self.lib.XSetErrorHandler(self._previous_error_handler)
            raise CaptureError("cannot open the current X display; DISPLAY may be unavailable")
        self.root = self.lib.XDefaultRootWindow(self.display)
        self.window_class = window_class.casefold() if window_class else None
        self.window_name = window_name.casefold() if window_name else None

    def _configure(self) -> None:
        lib = self.lib
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XDefaultRootWindow.restype = ctypes.c_ulong
        lib.XQueryTree.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.XQueryTree.restype = ctypes.c_int
        lib.XGetClassHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XClassHint)]
        lib.XGetClassHint.restype = ctypes.c_int
        lib.XFetchName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        lib.XFetchName.restype = ctypes.c_int
        lib.XFree.argtypes = [ctypes.c_void_p]
        lib.XFree.restype = ctypes.c_int
        lib.XGetGeometry.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.XGetGeometry.restype = ctypes.c_int
        lib.XGetImage.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_ulong,
            ctypes.c_int,
        ]
        lib.XGetImage.restype = ctypes.POINTER(XImage)
        lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XSync.restype = ctypes.c_int
        lib.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        lib.XSetErrorHandler.restype = ctypes.c_void_p
        lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        lib.XCloseDisplay.restype = ctypes.c_int

    def _on_x_error(self, _display: int, _event: int) -> int:
        self._error_count += 1
        return 0

    def _matches(self, window: int) -> bool:
        found = False
        if self.window_class:
            hint = XClassHint()
            if self.lib.XGetClassHint(self.display, window, ctypes.byref(hint)):
                for ptr in (hint.res_name, hint.res_class):
                    if ptr:
                        value = ctypes.string_at(ptr).decode("utf-8", "replace").casefold()
                        found = found or value == self.window_class
                        self.lib.XFree(ptr)
        if found:
            return True
        if self.window_name:
            name = ctypes.c_void_p()
            if self.lib.XFetchName(self.display, window, ctypes.byref(name)) and name.value:
                title = ctypes.string_at(name.value).decode("utf-8", "replace").casefold()
                self.lib.XFree(name.value)
                return self.window_name in title
        return False

    def _find_window(self) -> tuple[int, int, int] | None:
        root_return = ctypes.c_ulong()
        parent_return = ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        before = self._error_count
        ok = self.lib.XQueryTree(
            self.display,
            self.root,
            ctypes.byref(root_return),
            ctypes.byref(parent_return),
            ctypes.byref(children),
            ctypes.byref(count),
        )
        self.lib.XSync(self.display, 0)
        if not ok or self._error_count != before:
            if children:
                self.lib.XFree(children)
            return None
        best: tuple[int, int, int] | None = None
        best_area = 0
        try:
            for i in range(count.value):
                window = int(children[i])
                if not self._matches(window):
                    continue
                root = ctypes.c_ulong()
                x = ctypes.c_int()
                y = ctypes.c_int()
                width = ctypes.c_uint()
                height = ctypes.c_uint()
                border = ctypes.c_uint()
                depth = ctypes.c_uint()
                if not self.lib.XGetGeometry(
                    self.display,
                    window,
                    ctypes.byref(root),
                    ctypes.byref(x),
                    ctypes.byref(y),
                    ctypes.byref(width),
                    ctypes.byref(height),
                    ctypes.byref(border),
                    ctypes.byref(depth),
                ):
                    continue
                area = width.value * height.value
                if area > best_area:
                    best, best_area = (window, width.value, height.value), area
        finally:
            if children:
                self.lib.XFree(children)
        return best

    def capture(self) -> X11Image:
        target = self._find_window()
        if target is None:
            raise CaptureError("matching game window is not available")
        window, window_width, window_height = target
        width = min(window_width, self.MAX_WIDTH)
        height = min(window_height, self.MAX_HEIGHT)
        if width <= 0 or height <= 0:
            raise CaptureError("game window has no drawable area")
        before = self._error_count
        image = self.lib.XGetImage(self.display, window, 0, 0, width, height, self.ALL_PLANES, self.ZPIXMAP)
        self.lib.XSync(self.display, 0)
        if not image or self._error_count != before:
            raise CaptureError("X11 could not read the game window pixels")
        return X11Image(width, height, self.lib, image)

    def close(self) -> None:
        if self.display:
            self.lib.XCloseDisplay(self.display)
            self.display = None
        if self._previous_error_handler is not None:
            self.lib.XSetErrorHandler(self._previous_error_handler)
            self._previous_error_handler = None

    def __enter__(self) -> "X11Capture":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
