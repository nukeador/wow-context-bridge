"""One-shot probe or low-rate context reader for WoWContextBridge."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .capture import CaptureError, X11Capture
from .capture_gamescope import GamescopeCapture
from .png import read_png, write_png
from .protocol import DecodeResult, decode_image
from .state import ContextTracker


def emit(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), file=stream, flush=True)


def _probe_result(result: DecodeResult, image: Any, save_path: Path | None) -> dict[str, Any]:
    if save_path is not None:
        if isinstance(image, Path):
            save_path.write_bytes(image.read_bytes())
        elif hasattr(image, "to_rgb_image"):
            write_png(save_path, image.to_rgb_image())
        else:
            write_png(save_path, image)
    report: dict[str, Any] = {"type": "probe", "valid": result.status == "valid"}
    if result.frame:
        report.update({"sequence": result.frame.sequence, "context": result.frame.context})
    else:
        report.update({"status": result.status, "reason": result.reason})
    if result.origin is not None:
        report["origin"] = list(result.origin)
    if result.cell_size is not None:
        report["cell_size"] = result.cell_size
    if save_path is not None:
        report["saved_image"] = str(save_path)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, help="decode one existing PNG instead of capturing a screen")
    parser.add_argument("--backend", choices=("x11", "gamescope"), default="x11")
    parser.add_argument("--probe", action="store_true", help="capture and decode one frame, then exit")
    parser.add_argument(
        "--gamescope-fd-probe",
        action="store_true",
        help="test one portal PipeWire buffer through gst-launch and fakesink, without saving a frame",
    )
    parser.add_argument(
        "--capture-diagnostics",
        action="store_true",
        help="log Gamescope portal/FD/video metadata to stderr; never logs frame pixels",
    )
    parser.add_argument("--watch", action="store_true", help="poll capture and print changed context")
    parser.add_argument("--save-image", type=Path, help="explicitly save the one-shot capture image")
    parser.add_argument("--window-class", default="Wow.exe", help="X11 WM_CLASS to match (default: Wow.exe)")
    parser.add_argument("--window-name", help="match an X11 window title substring instead of WM_CLASS")
    parser.add_argument("--interval", type=float, default=0.25, help="capture polling interval in seconds")
    parser.add_argument("--stale-after", type=float, default=8.0, help="seconds without a new sequence before stale")
    parser.add_argument("--search-px", type=int, default=64, help="origin search radius near image top-left")
    return parser


def run_image_probe(path: Path, search_px: int) -> int:
    try:
        image = read_png(path)
        result = decode_image(image, search_px=search_px)
        emit(_probe_result(result, image, None))
        return 0 if result.status == "valid" else 1
    except (OSError, ValueError) as exc:
        emit({"type": "probe", "valid": False, "status": "image_error", "reason": str(exc)})
        return 2


def run_probe(args: argparse.Namespace) -> int:
    capture = None
    try:
        capture = open_capture(args)
        image = capture.capture()
        try:
            result = decode_image(image, search_px=args.search_px)
            emit(_probe_result(result, image, args.save_image))
            return 0 if result.status == "valid" else 1
        finally:
            image.close()
    except CaptureError as exc:
        emit({"type": "probe", "valid": False, "status": "capture_error", "reason": str(exc)})
        return 2
    except OSError as exc:
        emit({"type": "probe", "valid": False, "status": "capture_error", "reason": str(exc)})
        return 2
    finally:
        if capture is not None:
            capture.close()


def run_gamescope_fd_probe() -> int:
    capture = None
    try:
        capture = GamescopeCapture(diagnostic=True, subprocess_probe=True)
        result = capture._subprocess_probe_result or {"success": False, "error": "probe did not run"}
        emit({
            "type": "gamescope_fd_probe",
            "portal_version": capture._portal_version,
            "node_id": capture._stream_node_id,
            "actual_unix_fd": capture._pipewire_fd,
            "received_one_buffer": bool(result.get("success")),
            **result,
        })
        return 0 if result.get("success") else 2
    except CaptureError as exc:
        emit({"type": "gamescope_fd_probe", "received_one_buffer": False, "error": str(exc)})
        return 2
    except OSError as exc:
        emit({"type": "gamescope_fd_probe", "received_one_buffer": False, "error": str(exc)})
        return 2
    finally:
        if capture is not None:
            capture.close()


def open_capture(args: argparse.Namespace) -> Any:
    if args.backend == "gamescope":
        return GamescopeCapture(diagnostic=args.capture_diagnostics)
    return X11Capture(args.window_class, args.window_name)


def run_watch(args: argparse.Namespace) -> int:
    tracker = ContextTracker(stale_after=args.stale_after)
    last_warning: dict[str, float] = {}
    hint: tuple[int, int, float] | None = None
    try:
        capture = open_capture(args)
    except CaptureError as exc:
        emit({"type": "reader", "status": "capture_unavailable", "reason": str(exc)}, stream=sys.stderr)
        return 2

    try:
        while True:
            started = time.monotonic()
            try:
                image = capture.capture()
                try:
                    result = decode_image(image, search_px=args.search_px, hint=hint)
                    if result.status == "valid" and result.frame:
                        hint = (*result.origin, result.cell_size) if result.origin is not None and result.cell_size else hint
                        context = tracker.ingest(result.frame, time.monotonic())
                        if context is not None:
                            emit({"type": "context", "sequence": result.frame.sequence, **context})
                    elif result.status in ("malformed", "incomplete"):
                        now = time.monotonic()
                        if now - last_warning.get(result.status, 0) >= 5:
                            emit({"type": "frame_error", "status": result.status, "reason": result.reason}, stream=sys.stderr)
                            last_warning[result.status] = now
                finally:
                    image.close()
            except CaptureError as exc:
                now = time.monotonic()
                if now - last_warning.get("capture", 0) >= 3:
                    emit({"type": "capture", "status": "unavailable", "reason": str(exc)}, stream=sys.stderr)
                    last_warning["capture"] = now

            now = time.monotonic()
            stale_age = tracker.stale_event(now)
            if stale_age is not None:
                emit({"type": "context", "status": "stale", "age_seconds": round(stale_age, 2)}, stream=sys.stderr)
            delay = args.interval - (time.monotonic() - started)
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        emit({"type": "reader", "status": "stopped"}, stream=sys.stderr)
        return 0
    finally:
        capture.close()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.interval <= 0 or args.stale_after <= 0 or args.search_px < 0:
        parser.error("interval/stale-after must be positive and search-px must be nonnegative")
    if args.gamescope_fd_probe:
        if args.backend != "gamescope":
            parser.error("--gamescope-fd-probe requires --backend gamescope")
        if args.image or args.probe or args.watch or args.save_image:
            parser.error("--gamescope-fd-probe cannot be combined with --image, --probe, --watch or --save-image")
        return run_gamescope_fd_probe()
    if args.capture_diagnostics and args.backend != "gamescope":
        parser.error("--capture-diagnostics applies only to --backend gamescope")
    if args.image:
        if args.save_image:
            parser.error("--save-image applies only to live one-shot capture; --image already names a file")
        return run_image_probe(args.image, args.search_px)
    if args.save_image and not args.probe:
        parser.error("--save-image requires --probe")
    if args.probe:
        return run_probe(args)
    return run_watch(args)


if __name__ == "__main__":
    raise SystemExit(main())
