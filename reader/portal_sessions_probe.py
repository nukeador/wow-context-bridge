"""Inspect Gamescope ScreenCast IDs and PipeWire metadata without capturing frames.

The probe uses only the public XDG ScreenCast portal and read-only PipeWire
registry metadata. It never invokes GStreamer, requests media buffers, decodes
pixels, or saves screenshots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"


def _unwrap(value: Any) -> Any:
    if hasattr(value, "unpack"):
        return _unwrap(value.unpack())
    if isinstance(value, dict):
        return {key: _unwrap(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return type(value)(_unwrap(item) for item in value)
    return value


def _variants(**values: tuple[str, Any]) -> dict[str, Any]:
    return {key: GLib.Variant(signature, value) for key, (signature, value) in values.items()}


def _request(
    connection: Any,
    method: str,
    signature: str,
    arguments: tuple[Any, ...],
    timeout_seconds: int,
) -> dict[str, Any]:
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

    subscription = connection.signal_subscribe(
        PORTAL,
        "org.freedesktop.portal.Request",
        "Response",
        None,
        None,
        Gio.DBusSignalFlags.NONE,
        on_response,
        None,
    )
    try:
        reply = connection.call_sync(
            PORTAL,
            PORTAL_PATH,
            SCREENCAST,
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

            timer = GLib.timeout_add_seconds(timeout_seconds, expire)
            loop.run()
            GLib.source_remove(timer)
            if response is None and timed_out[0]:
                raise TimeoutError(f"{method} response timeout")
        if response is None:
            raise RuntimeError(f"{method} returned no portal response")
        code, results = response
        if code != 0:
            raise RuntimeError(f"{method} portal response code {code}")
        return _unwrap(results)
    finally:
        connection.signal_unsubscribe(subscription)


def _graph_snapshot(target_id: int | None) -> dict[str, Any]:
    env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/1000")
    result = subprocess.run(
        ["pw-dump"], capture_output=True, text=True, timeout=8, env=env, check=False
    )
    if result.returncode:
        return {"ok": False, "error": result.stderr.strip()[-500:]}
    try:
        objects = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid pw-dump JSON: {exc}"}

    nodes: list[dict[str, Any]] = []
    ports: list[dict[str, Any]] = []
    target = None
    for obj in objects:
        object_id = obj.get("id")
        interface = obj.get("type", "")
        props = (obj.get("info") or {}).get("props") or {}
        if interface.endswith(":Node"):
            nodes.append({
                "id": object_id,
                "serial": props.get("object.serial"),
                "node_name": props.get("node.name"),
                "media_class": props.get("media.class"),
                "application_name": props.get("application.name"),
            })
        elif interface.endswith(":Port"):
            ports.append({
                "id": object_id,
                "node_id": props.get("node.id"),
                "port_name": props.get("port.name"),
                "media_class": props.get("media.class"),
                "media_type": props.get("media.type"),
                "direction": props.get("port.direction"),
            })
        if target_id is not None and object_id == target_id:
            allowed = (
                "object.serial", "node.name", "media.class", "application.name",
                "node.id", "port.name", "media.type", "port.direction", "object.path",
            )
            target = {
                "interface": interface,
                **{key: props[key] for key in allowed if key in props},
            }

    canonical = json.dumps([nodes, ports], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    video_sources = [node for node in nodes if node["media_class"] == "Video/Source"]
    gamescope_nodes = [node for node in nodes if "gamescope" in str(node["node_name"]).lower()]
    wow_nodes = [
        node for node in nodes
        if "world of warcraft" in str(node["application_name"]).lower()
        or "world of warcraft" in str(node["node_name"]).lower()
    ]
    return {
        "ok": True,
        "node_count": len(nodes),
        "port_count": len(ports),
        "metadata_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "video_sources": video_sources,
        "gamescope_named_nodes": gamescope_nodes,
        "wow_related_nodes": wow_nodes,
        "target_global": target,
    }


def _open_remote(connection: Any, session_path: str) -> tuple[int, int, int]:
    reply, fd_list = connection.call_with_unix_fd_list_sync(
        PORTAL,
        PORTAL_PATH,
        SCREENCAST,
        "OpenPipeWireRemote",
        GLib.Variant("(oa{sv})", (session_path, {})),
        GLib.VariantType.new("(h)"),
        Gio.DBusCallFlags.NONE,
        10_000,
        None,
        None,
    )
    handle_index = int(reply.unpack()[0])
    if fd_list is None or not 0 <= handle_index < fd_list.get_length():
        raise RuntimeError("portal returned an invalid UnixFDList handle")
    fd = int(fd_list.get(handle_index))
    os.fstat(fd)
    return fd, handle_index, fd_list.get_length()


def _inspect_remote(fd: int, node_id: int, timeout: float) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "reader.pipewire_remote_probe",
        "--fd",
        str(fd),
        "--node-id",
        str(node_id),
        "--timeout",
        str(timeout),
    ]
    result = subprocess.run(
        command,
        pass_fds=(fd,),
        capture_output=True,
        text=True,
        timeout=timeout + 4,
        check=False,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "returncode": result.returncode, "stderr": result.stderr[-500:]}
    report.pop("globals", None)
    report.pop("duplicate_global_ids", None)
    return report


def _close_session(connection: Any, session_path: str) -> str | None:
    try:
        connection.call_sync(
            PORTAL,
            session_path,
            "org.freedesktop.portal.Session",
            "Close",
            GLib.Variant("()", ()),
            None,
            Gio.DBusCallFlags.NONE,
            3_000,
            None,
        )
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _run_session(connection: Any, number: int, portal_timeout: int,
                 pipewire_timeout: float) -> dict[str, Any]:
    token = "wcb_meta_" + secrets.token_hex(6)
    record: dict[str, Any] = {"session": number}
    session_path: str | None = None
    fd: int | None = None
    node_id: int | None = None
    try:
        record["graph_before_create"] = _graph_snapshot(None)
        created = _request(
            connection,
            "CreateSession",
            "(a{sv})",
            (_variants(
                handle_token=("s", token + "_create"),
                session_handle_token=("s", token + "_session"),
            ),),
            portal_timeout,
        )
        session_path = str(created["session_handle"])
        _request(
            connection,
            "SelectSources",
            "(oa{sv})",
            (session_path, _variants(
                handle_token=("s", token + "_select"),
                types=("u", 1),
                multiple=("b", False),
                cursor_mode=("u", 1),
            )),
            portal_timeout,
        )
        started = _request(
            connection,
            "Start",
            "(osa{sv})",
            (session_path, "", _variants(handle_token=("s", token + "_start"))),
            portal_timeout,
        )
        streams = started.get("streams") or []
        if not streams:
            raise RuntimeError("Start returned no streams")
        node_id, properties = streams[0]
        node_id = int(node_id)
        record["portal_node_id"] = node_id
        record["portal_stream_properties"] = properties
        record["graph_after_start"] = _graph_snapshot(node_id)
        fd, handle_index, fd_list_length = _open_remote(connection, session_path)
        record["remote_fd"] = {
            "handle_index": handle_index,
            "fd_list_length": fd_list_length,
            "fstat_ok": True,
        }
        record["graph_while_remote_open"] = _graph_snapshot(node_id)
        record["remote_registry"] = _inspect_remote(fd, node_id, pipewire_timeout)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if session_path is not None:
            record["close_error"] = _close_session(connection, session_path)
            record["graph_after_close"] = _graph_snapshot(node_id)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=3, help="number of sequential sessions (default: 3)")
    parser.add_argument("--portal-timeout", type=int, default=15, help="seconds to wait per portal response")
    parser.add_argument("--pipewire-timeout", type=float, default=4.0, help="seconds for registry sync")
    args = parser.parse_args()
    if not 1 <= args.sessions <= 10 or args.portal_timeout <= 0 or args.pipewire_timeout <= 0:
        parser.error("sessions must be 1..10 and timeout values must be positive")

    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    version_reply = connection.call_sync(
        PORTAL,
        PORTAL_PATH,
        "org.freedesktop.DBus.Properties",
        "Get",
        GLib.Variant("(ss)", (SCREENCAST, "version")),
        GLib.VariantType.new("(v)"),
        Gio.DBusCallFlags.NONE,
        5_000,
        None,
    )
    version = int(_unwrap(version_reply.unpack()[0]))
    print(json.dumps({"type": "probe_start", "portal_version": version, "gstreamer_used": False}, separators=(",", ":")), flush=True)
    failed = False
    for number in range(1, args.sessions + 1):
        record = _run_session(connection, number, args.portal_timeout, args.pipewire_timeout)
        print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)
        failed |= "error" in record or not record.get("remote_registry", {}).get("ok", False)
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
