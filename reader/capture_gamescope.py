"""Optional Gamescope screen capture through the desktop ScreenCast portal.

This backend uses the user's existing screen-sharing permission and keeps frames
in memory. It does not save screenshots. The SteamOS portal may show a one-time
source/permission prompt when a capture session starts.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import zlib
import json
from typing import Any

from .capture import CaptureError
from .png import Image


class GamescopeCapture:
    """Read video frames from a portal-approved PipeWire stream."""

    PORTAL = "org.freedesktop.portal.Desktop"
    PORTAL_PATH = "/org/freedesktop/portal/desktop"
    SCREENCAST = "org.freedesktop.portal.ScreenCast"

    def __init__(
        self,
        *,
        request_timeout: int = 120,
        diagnostic: bool = False,
        subprocess_probe: bool = False,
    ) -> None:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("Gst", "1.0")
            gi.require_version("GstVideo", "1.0")
            from gi.repository import Gio, GLib, Gst, GstVideo
        except (ImportError, ValueError) as exc:
            raise CaptureError(
                "Gamescope capture needs the system PyGObject, GStreamer, and PipeWire bindings"
            ) from exc

        self.Gio = Gio
        self.GLib = GLib
        self.Gst = Gst
        self.GstVideo = GstVideo
        self._diagnostic = diagnostic
        self._connection: Any = None
        self._session_handle: str | None = None
        self._pipewire_fd: int | None = None
        self._stream_target: str | None = None
        self._stream_path: str | None = None
        self._stream_node_id: int | None = None
        self._stream_properties: dict[str, Any] = {}
        self._portal_version: int | None = None
        self._subprocess_probe_result: dict[str, Any] | None = None
        self._pipeline: Any = None
        self._sink: Any = None
        self._sample_diagnostics_logged = False

        try:
            Gst.init(None)
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._portal_version = self._get_portal_version()
            self._log_diagnostics(
                "runtime",
                portal_version=self._portal_version,
                xdg_runtime_dir=os.environ.get("XDG_RUNTIME_DIR"),
                xdg_session_type=os.environ.get("XDG_SESSION_TYPE"),
                gstreamer_version=Gst.version_string(),
                pipewire_version=(
                    self._command_version(["pw-cli", "--version"])
                    if diagnostic else None
                ),
            )
            token = "wcb_" + secrets.token_hex(8)

            created = self._portal_request(
                "CreateSession",
                "(a{sv})",
                (
                    self._options(
                        handle_token=("s", token + "_request"),
                        session_handle_token=("s", token + "_session"),
                    ),
                ),
                request_timeout,
            )
            self._session_handle = str(created["session_handle"])
            self._log_diagnostics("session", session_handle=self._session_handle)

            # Monitor and window are the only sources requested. The portal
            # asks the user to approve the source; no hidden process access is used.
            selected_source_types = 3
            self._portal_request(
                "SelectSources",
                "(oa{sv})",
                (
                    self._session_handle,
                    self._options(
                        handle_token=("s", token + "_select"),
                        types=("u", selected_source_types),
                        multiple=("b", False),
                        cursor_mode=("u", 1),
                    ),
                ),
                request_timeout,
            )
            self._log_diagnostics("selected_sources", requested_types=selected_source_types)
            started = self._portal_request(
                "Start",
                "(osa{sv})",
                (
                    self._session_handle,
                    "",
                    self._options(handle_token=("s", token + "_start")),
                ),
                request_timeout,
            )
            streams = started.get("streams", ())
            if not streams:
                raise CaptureError("Gamescope ScreenCast returned no video stream")
            node_id, stream_properties = streams[0]
            self._stream_node_id = int(node_id)
            self._stream_properties = dict(stream_properties)
            pipewire_serial = stream_properties.get("pipewire-serial")
            if pipewire_serial is not None:
                # Portal v6 recommends its stable PipeWire serial, which
                # GStreamer accepts through target-object.
                self._stream_target = str(pipewire_serial)
            else:
                # SteamOS currently exposes ScreenCast v5. Its stream tuple
                # provides a numeric PipeWire node ID, which maps to the
                # legacy pipewiresrc `path` property, not `target-object`
                # (which accepts a node name or object serial).
                self._stream_path = str(node_id)
            safe_property_names = ("pipewire-serial", "position", "size", "source_type")
            safe_properties = {
                name: stream_properties[name]
                for name in safe_property_names
                if name in stream_properties
            }
            self._log_diagnostics(
                "portal_stream",
                streams_count=len(streams),
                node_id=self._stream_node_id,
                selected_source_type=safe_properties.get("source_type"),
                stream_properties=safe_properties,
                other_property_names=sorted(set(stream_properties) - set(safe_property_names)),
                target_object=self._stream_target,
                path=self._stream_path,
            )

            self._pipewire_fd, fd_handle_index, fd_list_length = self._open_pipewire_remote_fd()
            self._log_diagnostics(
                "pipewire_fd_resolved",
                dbus_fd_handle_index=fd_handle_index,
                fd_list_length=fd_list_length,
                actual_unix_fd=self._pipewire_fd,
            )
            self._validate_pipewire_fd("after OpenPipeWireRemote")

            if subprocess_probe:
                self._subprocess_probe_result = self._run_subprocess_probe()
                return

            source = Gst.ElementFactory.make("pipewiresrc", "portal-screen")
            converter = Gst.ElementFactory.make("videoconvert", "frame-converter")
            sink = Gst.ElementFactory.make("appsink", "frame-sink")
            if source is None or converter is None or sink is None:
                raise CaptureError("GStreamer PipeWire video elements are unavailable")
            source.set_property("fd", self._pipewire_fd)
            if self._stream_target is not None:
                source.set_property("target-object", self._stream_target)
            elif self._stream_path is not None:
                source.set_property("path", self._stream_path)
            sink.set_property("caps", Gst.Caps.from_string("video/x-raw,format=RGB"))
            sink.set_property("sync", False)
            sink.set_property("max-buffers", 1)
            sink.set_property("drop", True)
            pipeline = Gst.Pipeline.new("companionpoc-screen-capture")
            if pipeline is None:
                raise CaptureError("could not create a GStreamer pipeline")
            pipeline.add(source)
            pipeline.add(converter)
            pipeline.add(sink)
            if not source.link(converter) or not converter.link(sink):
                raise CaptureError("could not link the PipeWire video conversion pipeline")
            self._validate_pipewire_fd("immediately before GStreamer pipeline creation")
            self._pipeline = pipeline
            self._sink = sink
            state = pipeline.set_state(Gst.State.PLAYING)
            if state == Gst.StateChangeReturn.FAILURE:
                message = pipeline.get_bus().timed_pop_filtered(
                    0, Gst.MessageType.ERROR
                )
                if message is not None:
                    error, debug = message.parse_error()
                    detail = error.message
                    if debug:
                        detail += f" ({debug})"
                    if self._stream_target is not None:
                        detail += f"; portal stream target-object={self._stream_target}"
                    if self._stream_path is not None:
                        detail += f"; portal stream path={self._stream_path}"
                    raise CaptureError(f"GStreamer could not start the Gamescope video stream: {detail}")
                raise CaptureError("GStreamer could not start the Gamescope video stream")
        except CaptureError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise CaptureError(f"Gamescope capture setup failed: {exc}") from exc

    def _log_diagnostics(self, event: str, **details: Any) -> None:
        if self._diagnostic:
            record = {"component": "gamescope-capture", "event": event, **details}
            print(json.dumps(record, ensure_ascii=False, default=str), file=sys.stderr, flush=True)

    @staticmethod
    def _command_version(command: list[str]) -> str | None:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        output = (result.stdout or result.stderr).strip()
        return output[:240] if output else None

    def _get_portal_version(self) -> int | None:
        try:
            reply = self._connection.call_sync(
                self.PORTAL,
                self.PORTAL_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                self.GLib.Variant("(ss)", (self.SCREENCAST, "version")),
                self.GLib.VariantType.new("(v)"),
                self.Gio.DBusCallFlags.NONE,
                3_000,
                None,
            )
            return int(self._unpack(reply.unpack()[0]))
        except Exception:
            return None

    def _open_pipewire_remote_fd(self) -> tuple[int, int, int]:
        reply, fd_list = self._connection.call_with_unix_fd_list_sync(
            self.PORTAL,
            self.PORTAL_PATH,
            self.SCREENCAST,
            "OpenPipeWireRemote",
            self.GLib.Variant("(oa{sv})", (self._session_handle, {})),
            self.GLib.VariantType.new("(h)"),
            self.Gio.DBusCallFlags.NONE,
            10_000,
            None,
            None,
        )
        if fd_list is None:
            raise CaptureError("Gamescope portal did not provide a PipeWire connection")
        fd_handle_index = int(reply.unpack()[0])
        fd_list_length = fd_list.get_length()
        if fd_handle_index < 0 or fd_handle_index >= fd_list_length:
            raise CaptureError(
                f"Gamescope portal FD handle index {fd_handle_index} is outside FD list length {fd_list_length}"
            )
        # A D-Bus `h` is an index into the message's UnixFDList. Gio's get()
        # returns an owned duplicate of the actual Unix process descriptor.
        return int(fd_list.get(fd_handle_index)), fd_handle_index, fd_list_length

    def _validate_pipewire_fd(self, stage: str) -> None:
        if self._pipewire_fd is None:
            raise CaptureError(f"PipeWire FD is missing at stage: {stage}")
        try:
            stat_result = os.fstat(self._pipewire_fd)
            inheritable = os.get_inheritable(self._pipewire_fd)
        except OSError as exc:
            self._log_diagnostics(
                "pipewire_fd_invalid",
                stage=stage,
                actual_unix_fd=self._pipewire_fd,
                error=str(exc),
            )
            raise CaptureError(f"PipeWire FD is invalid at stage {stage}: {exc}") from exc
        self._log_diagnostics(
            "pipewire_fd_valid",
            stage=stage,
            actual_unix_fd=self._pipewire_fd,
            fstat={
                "device": stat_result.st_dev,
                "inode": stat_result.st_ino,
                "mode": oct(stat_result.st_mode),
            },
            inheritable=inheritable,
        )

    def _run_subprocess_probe(self, timeout: float = 10.0) -> dict[str, Any]:
        if self._pipewire_fd is None:
            raise CaptureError("PipeWire FD is missing before subprocess probe")
        if self._stream_path is None and self._stream_target is None:
            raise CaptureError("portal did not return a usable PipeWire stream selector")
        self._validate_pipewire_fd("immediately before gst-launch subprocess")
        remote_registry = self._inspect_portal_remote()
        self._log_diagnostics("portal_remote_registry", **remote_registry)
        selector = (
            f"path={self._stream_path}"
            if self._stream_path is not None
            else f"target-object={self._stream_target}"
        )
        command = [
            "gst-launch-1.0", "-q", "pipewiresrc",
            f"fd={self._pipewire_fd}", selector,
            "num-buffers=1", "!", "fakesink", "sync=false",
        ]
        fd_check_code = (
            "import json,os,sys; fd=int(sys.argv[1]); st=os.fstat(fd); "
            "print(json.dumps({'fd':fd,'device':st.st_dev,'inode':st.st_ino,"
            "'inheritable':os.get_inheritable(fd)}))"
        )
        try:
            child_fd_check = subprocess.run(
                [sys.executable, "-c", fd_check_code, str(self._pipewire_fd)],
                pass_fds=(self._pipewire_fd,),
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CaptureError(f"could not verify inherited PipeWire FD in child: {exc}") from exc
        child_fd_report = {
            "valid": child_fd_check.returncode == 0,
            "stdout": child_fd_check.stdout.strip()[-500:],
            "stderr": child_fd_check.stderr.strip()[-500:],
        }
        self._log_diagnostics("child_fd_check", **child_fd_report)
        if child_fd_check.returncode != 0:
            return {
                "success": False,
                "error": "PipeWire FD did not remain valid in a child process",
                "child_fd_check": child_fd_report,
            }
        self._log_diagnostics(
            "subprocess_probe_start",
            command=command,
            passed_fds=[self._pipewire_fd],
            timeout_seconds=timeout,
        )
        try:
            result = subprocess.run(
                command,
                pass_fds=(self._pipewire_fd,),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            report = {
                "success": result.returncode == 0,
                "returncode": result.returncode,
                "stderr": result.stderr[-2000:],
                "child_fd_check": child_fd_report,
                "portal_remote_registry": remote_registry,
            }
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            report = {
                "success": False,
                "timed_out": True,
                "stderr": stderr[-2000:],
                "child_fd_check": child_fd_report,
                "portal_remote_registry": remote_registry,
            }
        except OSError as exc:
            report = {
                "success": False,
                "error": str(exc),
                "child_fd_check": child_fd_report,
                "portal_remote_registry": remote_registry,
            }
        self._log_diagnostics("subprocess_probe_result", **report)
        return report

    def _inspect_portal_remote(self, timeout: float = 6.0) -> dict[str, Any]:
        """List portal-remote globals in an isolated metadata-only child."""
        if self._pipewire_fd is None or self._stream_node_id is None:
            return {"ok": False, "error": "portal PipeWire FD or stream node is unavailable"}
        try:
            probe_fd, fd_handle_index, fd_list_length = self._open_pipewire_remote_fd()
            capture_stat = os.fstat(self._pipewire_fd)
            probe_stat = os.fstat(probe_fd)
            distinct_socket = (capture_stat.st_dev, capture_stat.st_ino) != (
                probe_stat.st_dev,
                probe_stat.st_ino,
            )
        except (CaptureError, OSError) as exc:
            return {"ok": False, "error": f"could not open a separate portal metadata FD: {exc}"}
        if not distinct_socket:
            os.close(probe_fd)
            return {
                "ok": False,
                "error": "portal returned the same PipeWire socket for capture and metadata; inspection skipped to preserve the capture connection",
            }
        command = [
            sys.executable,
            "-m",
            "reader.pipewire_remote_probe",
            "--fd",
            str(probe_fd),
            "--node-id",
            str(self._stream_node_id),
        ]
        try:
            result = subprocess.run(
                command,
                pass_fds=(probe_fd,),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return {
                "ok": False,
                "timed_out": True,
                "fd_handle_index": fd_handle_index,
                "fd_list_length": fd_list_length,
                "distinct_socket": distinct_socket,
                "stderr": stderr[-1000:],
            }
        except OSError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "fd_handle_index": fd_handle_index,
                "fd_list_length": fd_list_length,
                "distinct_socket": distinct_socket,
            }
        finally:
            os.close(probe_fd)

        try:
            report = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            report = {"ok": False, "error": "metadata helper returned no JSON"}
        report["helper_returncode"] = result.returncode
        report["fd_handle_index"] = fd_handle_index
        report["fd_list_length"] = fd_list_length
        report["distinct_socket"] = distinct_socket
        report["metadata_unix_fd"] = probe_fd
        report["metadata_fstat"] = {
            "device": probe_stat.st_dev,
            "inode": probe_stat.st_ino,
            "mode": oct(probe_stat.st_mode),
        }
        if result.stderr.strip():
            report["helper_stderr"] = result.stderr[-1000:]
        return report

    @staticmethod
    def _options(**values: tuple[str, Any]) -> Any:
        # Called only after GI imports succeeded in __init__.
        from gi.repository import GLib

        return {key: GLib.Variant(signature, value) for key, (signature, value) in values.items()}

    def _portal_request(
        self,
        method: str,
        signature: str,
        arguments: tuple[Any, ...],
        timeout: int,
    ) -> dict[str, Any]:
        Gio, GLib = self.Gio, self.GLib
        loop = GLib.MainLoop()
        request_path: str | None = None
        response: tuple[int, dict[str, Any]] | None = None
        early: dict[str, tuple[int, dict[str, Any]]] = {}

        def on_response(_connection: Any, _sender: str, path: str, _interface: str,
                        _signal: str, parameters: Any, _data: Any) -> None:
            nonlocal response
            unpacked = parameters.unpack()
            if request_path is None:
                early[path] = unpacked
            elif path == request_path:
                response = unpacked
                loop.quit()

        subscription = self._connection.signal_subscribe(
            self.PORTAL,
            "org.freedesktop.portal.Request",
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
            None,
        )
        try:
            reply = self._connection.call_sync(
                self.PORTAL,
                self.PORTAL_PATH,
                self.SCREENCAST,
                method,
                GLib.Variant(signature, arguments),
                GLib.VariantType.new("(o)"),
                Gio.DBusCallFlags.NONE,
                10_000,
                None,
            )
            request_path = str(reply.unpack()[0])
            response = early.get(request_path)
            if response is None:
                timed_out = [False]

                def expire() -> bool:
                    timed_out[0] = True
                    loop.quit()
                    return GLib.SOURCE_REMOVE

                timer = GLib.timeout_add_seconds(timeout, expire)
                loop.run()
                GLib.source_remove(timer)
                if response is None and timed_out[0]:
                    raise CaptureError(f"timed out waiting for Gamescope {method} approval")
            if response is None:
                raise CaptureError(f"Gamescope {method} did not return a response")
            code, results = response
            if code != 0:
                detail = "cancelled" if code == 1 else f"portal error {code}"
                raise CaptureError(f"Gamescope screen capture {detail}")
            return self._unpack(results)
        finally:
            self._connection.signal_unsubscribe(subscription)

    @classmethod
    def _unpack(cls, value: Any) -> Any:
        if hasattr(value, "unpack"):
            return cls._unpack(value.unpack())
        if isinstance(value, dict):
            return {key: cls._unpack(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return type(value)(cls._unpack(item) for item in value)
        return value

    def capture(self, *, timeout: float = 8.0) -> Image:
        sample = self._sink.emit("try-pull-sample", int(timeout * self.Gst.SECOND))
        if sample is None:
            bus = self._pipeline.get_bus()
            message = bus.pop_filtered(self.Gst.MessageType.ERROR)
            if message is not None:
                error, _debug = message.parse_error()
                raise CaptureError(f"Gamescope video stream failed: {error.message}")
            raise CaptureError("timed out waiting for a Gamescope video frame")

        caps = sample.get_caps()
        info = self.GstVideo.VideoInfo.new_from_caps(caps)
        if info is None:
            raise CaptureError("PipeWire returned unsupported video frame metadata")
        buffer = sample.get_buffer()
        ok, mapped = buffer.map(self.Gst.MapFlags.READ)
        if not ok:
            raise CaptureError("could not read the PipeWire video frame")
        try:
            width, height = int(info.width), int(info.height)
            stride, offset = int(info.stride[0]), int(info.offset[0])
            if self._diagnostic and not self._sample_diagnostics_logged:
                self._log_diagnostics(
                    "video_sample",
                    caps=caps.to_string(),
                    width=width,
                    height=height,
                    pixel_format=caps.get_structure(0).get_value("format"),
                    framerate=[int(info.fps_n), int(info.fps_d)],
                    buffer_size=int(buffer.get_size()),
                    pts=None if buffer.pts == self.Gst.CLOCK_TIME_NONE else int(buffer.pts),
                    crc32=f"{zlib.crc32(mapped.data) & 0xffffffff:08x}",
                )
                self._sample_diagnostics_logged = True
            row_bytes = width * 3
            pixels = bytearray(row_bytes * height)
            for y in range(height):
                start = offset + y * stride
                pixels[y * row_bytes : (y + 1) * row_bytes] = mapped.data[start : start + row_bytes]
        finally:
            buffer.unmap(mapped)
        return Image(width, height, pixels)

    def close(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(self.Gst.State.NULL)
            self._pipeline = None
            self._sink = None
        if self._pipewire_fd is not None:
            try:
                os.close(self._pipewire_fd)
            except OSError:
                pass
            self._pipewire_fd = None
        if self._session_handle and self._connection is not None:
            try:
                self._connection.call_sync(
                    self.PORTAL,
                    self._session_handle,
                    "org.freedesktop.portal.Session",
                    "Close",
                    self.GLib.Variant("()", ()),
                    None,
                    self.Gio.DBusCallFlags.NONE,
                    2_000,
                    None,
                )
            except Exception:
                pass
            self._session_handle = None

    def __enter__(self) -> "GamescopeCapture":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
