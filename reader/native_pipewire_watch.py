"""Decode cropped RGB frames from the standalone native PipeWire stream."""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .capture import CaptureError
from .native_pipewire_probe import NativeProbePortalSession, _portal_size
from .png import Image
from .protocol import DecodeResult, decode_image
from .state import ContextTracker

FRAME_HEADER = struct.Struct("!4sHHQ")
FRAME_MAGIC = b"DCPF"
MAX_FRAME_WIDTH = 1088
MAX_FRAME_HEIGHT = 128


def parse_frame_header(data: bytes) -> tuple[int, int, int]:
    if len(data) != FRAME_HEADER.size:
        raise ValueError("invalid native frame header size")
    magic, width, height, timestamp_ns = FRAME_HEADER.unpack(data)
    if magic != FRAME_MAGIC:
        raise ValueError("invalid native frame magic")
    if not (0 < width <= MAX_FRAME_WIDTH and 0 < height <= MAX_FRAME_HEIGHT):
        raise ValueError("native frame dimensions exceed crop bounds")
    return width, height, timestamp_ns


def _read_exact(fd: int, size: int, timeout: float = 2.0) -> bytes | None:
    result = bytearray()
    deadline = time.monotonic() + timeout
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise ValueError("timed out reading incomplete native frame")
        chunk = os.read(fd, size - len(result))
        if not chunk:
            return None if not result else bytes(result)
        result.extend(chunk)
    return bytes(result)


def _read_frame(fd: int, timeout: float) -> tuple[int, int, int, bytes] | None | bool:
    readable, _, _ = select.select([fd], [], [], timeout)
    if not readable:
        return None
    header = _read_exact(fd, FRAME_HEADER.size)
    if header is None:
        return False
    if len(header) != FRAME_HEADER.size:
        raise ValueError("truncated native frame header")
    width, height, timestamp_ns = parse_frame_header(header)
    payload = _read_exact(fd, width * height * 3)
    if payload is None or len(payload) != width * height * 3:
        raise ValueError("truncated native RGB frame")
    return width, height, timestamp_ns, payload


def _emit_frame(result: DecodeResult, image: Image, received_ns: int,
                native_timestamp_ns: int) -> None:
    record: dict[str, Any] = {
        "type": "frame",
        "valid": result.status == "valid",
        "capture_to_decode_ms": round((time.monotonic_ns() - native_timestamp_ns) / 1e6, 2),
        "received_at_monotonic_ns": received_ns,
    }
    if result.frame:
        record["sequence"] = result.frame.sequence
        record["origin"] = list(result.origin or (0, 0))
        record["cell_size"] = result.cell_size
    else:
        record["status"] = result.status
        record["reason"] = result.reason
    print(json.dumps(record, separators=(",", ":")), flush=True)


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Decode live Gamescope frames through a native PipeWire stream; no screenshots are saved."
    )
    parser.add_argument("--native-binary", type=Path,
                        default=project_root / "reader" / "native_pipewire_probe.candidate")
    parser.add_argument("--duration-seconds", type=float, default=120.0)
    parser.add_argument("--frame-interval-ms", type=int, default=5000)
    parser.add_argument("--stale-after", type=float, default=15.0)
    parser.add_argument("--search-px", type=int, default=64)
    parser.add_argument("--request-timeout", type=int, default=120)
    args = parser.parse_args()
    if not 0 < args.duration_seconds <= 600:
        parser.error("--duration-seconds must be in (0, 600]")
    if not 50 <= args.frame_interval_ms <= 600000:
        parser.error("--frame-interval-ms must be between 50 and 600000")
    if args.stale_after <= 0 or args.search_px < 0:
        parser.error("--stale-after must be positive and --search-px nonnegative")

    binary = args.native_binary.expanduser().resolve()
    if not binary.is_file():
        print(json.dumps({"type": "error", "message": "native executable missing",
                          "expected": str(binary)}), file=sys.stderr, flush=True)
        return 2

    capture: NativeProbePortalSession | None = None
    process: subprocess.Popen[bytes] | None = None
    read_fd = write_fd = -1
    valid_frames = malformed_frames = 0
    tracker = ContextTracker(stale_after=args.stale_after)
    hint: tuple[int, int, float] | None = None
    try:
        capture = NativeProbePortalSession(request_timeout=args.request_timeout,
                                           diagnostic=True, subprocess_probe=True)
        fd = capture._pipewire_fd
        node_id = capture._stream_node_id
        if fd is None or node_id is None:
            raise CaptureError("portal session did not provide both FD and node ID")
        size = _portal_size(capture._stream_properties.get("size")) or (1280, 800)
        width, height = size
        print(json.dumps({"type": "native_watch_start", "portal_version": capture._portal_version,
                          "node_id": node_id, "width": width, "height": height,
                          "frame_interval_ms": args.frame_interval_ms}, separators=(",", ":")),
              flush=True)

        read_fd, write_fd = os.pipe()
        command = [str(binary), "--fd", str(fd), "--node-id", str(node_id),
                   "--width", str(width), "--height", str(height),
                   "--timeout-ms", str(round(args.duration_seconds * 1000)),
                   "--stream-output-fd", str(write_fd),
                   "--stream-interval-ms", str(args.frame_interval_ms)]
        process = subprocess.Popen(command, pass_fds=(fd, write_fd), close_fds=True)
        os.close(write_fd)
        write_fd = -1
        started = time.monotonic()
        frame_count = 0
        while process.poll() is None:
            packet = _read_frame(read_fd, timeout=0.5)
            now = time.monotonic()
            if packet is None:
                stale_age = tracker.stale_event(now)
                if stale_age is not None:
                    print(json.dumps({"type": "context", "status": "stale",
                                      "age_seconds": round(stale_age, 2)}, separators=(",", ":")),
                          file=sys.stderr, flush=True)
                continue
            if packet is False:
                break
            frame_width, frame_height, timestamp_ns, pixels = packet
            frame = Image(frame_width, frame_height, bytearray(pixels), 3)
            result = decode_image(frame, search_px=args.search_px, hint=hint)
            frame_count += 1
            received_ns = time.monotonic_ns()
            _emit_frame(result, frame, received_ns, timestamp_ns)
            if result.status == "valid" and result.frame:
                valid_frames += 1
                hint = (*result.origin, result.cell_size) if result.origin and result.cell_size else hint
                context = tracker.ingest(result.frame, now)
                if context is not None:
                    print(json.dumps({"type": "context", "sequence": result.frame.sequence,
                                      **context}, ensure_ascii=False, separators=(",", ":")), flush=True)
            else:
                malformed_frames += 1
            # A frozen but continuously delivered frame must also expire.
            stale_age = tracker.stale_event(now)
            if stale_age is not None:
                print(json.dumps({"type": "context", "status": "stale",
                                  "age_seconds": round(stale_age, 2)}, separators=(",", ":")),
                      file=sys.stderr, flush=True)

        return_code = process.wait(timeout=10)
        print(json.dumps({"type": "native_watch_summary", "frames": frame_count,
                          "valid_frames": valid_frames, "invalid_frames": malformed_frames,
                          "duration_seconds": round(time.monotonic() - started, 2),
                          "native_return_code": return_code}, separators=(",", ":")), flush=True)
        return 0 if return_code == 0 and valid_frames > 0 else 2
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
        print(json.dumps({"type": "native_watch_stopped"}), file=sys.stderr, flush=True)
        return 130
    except (CaptureError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"type": "native_watch_error", "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr, flush=True)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        return 2
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if read_fd >= 0:
            os.close(read_fd)
        if capture is not None:
            capture.close()


if __name__ == "__main__":
    raise SystemExit(main())
