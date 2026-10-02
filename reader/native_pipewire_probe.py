"""Open a portal ScreenCast session and hand its FD to the native PipeWire probe.

The existing GamescopeCapture helper owns portal setup and OpenPipeWireRemote().
A small subclass overrides its subprocess hook so this diagnostic never creates a
GStreamer pipeline. The portal session and FD remain alive until the native
child exits.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from .capture import CaptureError
from .capture_gamescope import GamescopeCapture


class NativeProbePortalSession(GamescopeCapture):
    """Reuse the established portal session setup without starting GStreamer."""

    def _run_subprocess_probe(self, timeout: float = 10.0) -> dict[str, Any]:
        # GamescopeCapture calls this hook after Start() and OpenPipeWireRemote().
        # Returning here bypasses its legacy gst-launch diagnostic path.
        return {"native_pipewire_probe_handoff": True}


def _portal_size(value: Any) -> tuple[int, int] | None:
    if hasattr(value, "unpack"):
        value = value.unpack()
    if isinstance(value, (tuple, list)) and len(value) == 2:
        try:
            width, height = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
        if 0 < width <= 8192 and 0 < height <= 4096:
            return width, height
    return None


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=(
            "Native libpipewire probe using a portal-returned node ID and "
            "OpenPipeWireRemote FD; no GStreamer stream is created."
        )
    )
    parser.add_argument(
        "--native-binary",
        type=Path,
        default=project_root / "reader" / "native_pipewire_probe",
        help="compiled native probe executable",
    )
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--request-timeout", type=int, default=120)
    parser.add_argument(
        "--output-prefix",
        default="/tmp/wow-context-bridge-frame",
        help="prefix for the first saved CPU-readable buffer (default: /tmp)",
    )
    args = parser.parse_args()

    binary = args.native_binary.expanduser().resolve()
    if not binary.is_file():
        print(
            json.dumps({
                "event": "probe_error",
                "message": "native executable missing; build it with "
                           "./reader/build_native_pipewire_probe.sh",
                "expected": str(binary),
            }),
            file=sys.stderr,
            flush=True,
        )
        return 2
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    capture: NativeProbePortalSession | None = None
    try:
        capture = NativeProbePortalSession(
            request_timeout=args.request_timeout,
            diagnostic=True,
            subprocess_probe=True,
        )
        fd = capture._pipewire_fd
        node_id = capture._stream_node_id
        if fd is None or node_id is None:
            raise CaptureError("portal session did not provide both FD and node ID")

        size = _portal_size(capture._stream_properties.get("size"))
        width, height = size or (1280, 800)
        portal_record = {
            "event": "native_probe_portal_ready",
            "portal_version": capture._portal_version,
            "node_id": node_id,
            "pipewire_serial_present": (
                "pipewire-serial" in capture._stream_properties
            ),
            "requested_width": width,
            "requested_height": height,
            "fd": fd,
            "gstreamer_pipeline_created": False,
        }
        print(json.dumps(portal_record), flush=True)

        command = [
            str(binary),
            "--fd", str(fd),
            "--node-id", str(node_id),
            "--width", str(width),
            "--height", str(height),
            "--timeout-ms", str(round(args.timeout_seconds * 1000)),
            "--output-prefix", args.output_prefix,
        ]
        result = subprocess.run(
            command,
            pass_fds=(fd,),
            check=False,
            timeout=args.timeout_seconds + 10,
        )
        print(json.dumps({
            "event": "native_probe_exit",
            "return_code": result.returncode,
            "frame_prefix": args.output_prefix,
        }), flush=True)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(json.dumps({
            "event": "probe_error",
            "message": "native child exceeded its outer timeout",
        }), file=sys.stderr, flush=True)
        return 124
    except KeyboardInterrupt:
        print(json.dumps({"event": "probe_interrupted"}), flush=True)
        return 130
    except (CaptureError, OSError, RuntimeError) as exc:
        print(json.dumps({
            "event": "probe_error",
            "message": str(exc),
        }, ensure_ascii=False), file=sys.stderr, flush=True)
        return 2
    finally:
        if capture is not None:
            capture.close()


if __name__ == "__main__":
    raise SystemExit(main())
