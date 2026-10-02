"""Read-only PipeWire registry probe using an already-open remote FD.

This diagnostic intentionally enumerates metadata only. It never creates a
stream or requests video buffers. It is launched in a child process because
ctypes calls into PipeWire's C ABI and the parent must survive a library fault.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import resource
import sys
import time
from typing import Any


class SpaDictItem(ctypes.Structure):
    _fields_ = [("key", ctypes.c_char_p), ("value", ctypes.c_char_p)]


class SpaDict(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("n_items", ctypes.c_uint32),
        ("items", ctypes.POINTER(SpaDictItem)),
    ]


GlobalEvent = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.c_uint32,
    ctypes.c_void_p,
)
RemoveEvent = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32)
DoneEvent = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int)


class RegistryEvents(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("global_event", GlobalEvent),
        ("global_remove", RemoveEvent),
    ]


class CoreEvents(ctypes.Structure):
    # pw_core_events callback order from PipeWire core.h. Keep the full table
    # through bound_props so the C library never reads beyond this allocation.
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("_padding", ctypes.c_uint32),
        ("info", ctypes.c_void_p),
        ("done", DoneEvent),
        ("ping", ctypes.c_void_p),
        ("error", ctypes.c_void_p),
        ("remove_id", ctypes.c_void_p),
        ("bound_id", ctypes.c_void_p),
        ("add_mem", ctypes.c_void_p),
        ("remove_mem", ctypes.c_void_p),
        ("bound_props", ctypes.c_void_p),
    ]


def _decode(value: bytes | None) -> str | None:
    return value.decode("utf-8", errors="replace") if value else None


def _properties(dictionary: int | None) -> dict[str, str]:
    if not dictionary:
        return {}
    data = ctypes.cast(dictionary, ctypes.POINTER(SpaDict)).contents
    if data.n_items > 4096:
        raise ValueError(f"PipeWire dictionary has unreasonable item count: {data.n_items}")
    result: dict[str, str] = {}
    for index in range(data.n_items):
        item = data.items[index]
        key, value = _decode(item.key), _decode(item.value)
        if key in (
            "node.name",
            "object.serial",
            "media.class",
            "node.id",
            "client.id",
            "port.id",
            "port.name",
            "port.direction",
            "object.path",
            "media.type",
            "media.role",
        ) and value is not None:
            result[key] = value
    return result


def inspect_remote(fd: int, node_id: int, timeout: float) -> dict[str, Any]:
    library_name = ctypes.util.find_library("pipewire-0.3")
    if not library_name:
        return {"ok": False, "error": "libpipewire-0.3 was not found"}
    lib = ctypes.CDLL(library_name, use_errno=True)

    def api(name: str, argtypes: list[Any], restype: Any) -> Any:
        function = getattr(lib, name)
        function.argtypes = argtypes
        function.restype = restype
        return function

    void_p, size_t = ctypes.c_void_p, ctypes.c_size_t
    api("pw_init", [void_p, void_p], None)(None, None)
    main_loop_new = api("pw_main_loop_new", [void_p], void_p)
    main_loop_get_loop = api("pw_main_loop_get_loop", [void_p], void_p)
    main_loop_destroy = api("pw_main_loop_destroy", [void_p], None)
    loop_iterate = api("pw_loop_iterate", [void_p, ctypes.c_int], ctypes.c_int)
    context_new = api("pw_context_new", [void_p, void_p, size_t], void_p)
    context_destroy = api("pw_context_destroy", [void_p], None)
    connect_fd = api("pw_context_connect_fd", [void_p, ctypes.c_int, void_p, size_t], void_p)
    core_disconnect = api("pw_core_disconnect", [void_p], ctypes.c_int)
    core_add_listener = api(
        "pw_core_add_listener", [void_p, void_p, ctypes.POINTER(CoreEvents), void_p], ctypes.c_int
    )
    core_sync = api("pw_core_sync", [void_p, ctypes.c_uint32, ctypes.c_int], ctypes.c_int)
    core_get_registry = api("pw_core_get_registry", [void_p, ctypes.c_uint32, size_t], void_p)
    registry_add_listener = api(
        "pw_registry_add_listener",
        [void_p, void_p, ctypes.POINTER(RegistryEvents), void_p],
        ctypes.c_int,
    )

    entries_by_id: dict[int, dict[str, Any]] = {}
    seen_ids: list[int] = []
    callback_errors: list[str] = []
    sync_complete = [False]

    @GlobalEvent
    def on_global(_data: int, global_id: int, _permissions: int, interface: bytes,
                  _version: int, props: int) -> None:
        try:
            entry = {
                "global_id": int(global_id),
                "interface": _decode(interface),
                **_properties(props),
            }
            entries_by_id[int(global_id)] = entry
            seen_ids.append(int(global_id))
        except Exception as exc:  # do not let Python exceptions cross the C ABI
            callback_errors.append(f"global {global_id}: {exc}")

    @RemoveEvent
    def on_remove(_data: int, global_id: int) -> None:
        entries_by_id.pop(int(global_id), None)

    @DoneEvent
    def on_done(_data: int, _object_id: int, _sequence: int) -> None:
        sync_complete[0] = True

    registry_events = RegistryEvents(0, on_global, on_remove)
    core_events = CoreEvents()
    core_events.version = 1
    core_events.done = on_done

    # spa_hook is an intrusive C structure; its public size varies by SPA ABI.
    # A generously sized, correctly aligned zeroed buffer is standard for opaque
    # listener storage when the concrete header is not available to ctypes.
    registry_hook = (ctypes.c_longdouble * 256)()
    core_hook = (ctypes.c_longdouble * 256)()

    main_loop = loop = context = core = registry = None
    duplicated_fd: int | None = None
    try:
        main_loop = main_loop_new(None)
        if not main_loop:
            return {"ok": False, "error": "pw_main_loop_new failed"}
        loop = main_loop_get_loop(main_loop)
        context = context_new(loop, None, 0)
        if not context:
            return {"ok": False, "error": "pw_context_new failed"}
        duplicated_fd = os.dup(fd)
        core = connect_fd(context, duplicated_fd, None, 0)
        if not core:
            return {
                "ok": False,
                "error": "pw_context_connect_fd failed",
                "errno": ctypes.get_errno(),
            }
        duplicated_fd = None  # ownership has transferred to PipeWire

        if core_add_listener(core, ctypes.byref(core_hook), ctypes.byref(core_events), None) < 0:
            return {"ok": False, "error": "pw_core_add_listener failed"}
        registry = core_get_registry(core, 3, 0)
        if not registry:
            return {"ok": False, "error": "pw_core_get_registry failed"}
        if registry_add_listener(
            registry, ctypes.byref(registry_hook), ctypes.byref(registry_events), None
        ) < 0:
            return {"ok": False, "error": "pw_registry_add_listener failed"}
        sequence = core_sync(core, 0, 0)
        if sequence < 0:
            return {"ok": False, "error": f"pw_core_sync failed: {sequence}"}

        deadline = time.monotonic() + timeout
        while not sync_complete[0] and time.monotonic() < deadline:
            remaining_ms = max(1, min(200, int((deadline - time.monotonic()) * 1000)))
            result = loop_iterate(loop, remaining_ms)
            if result < 0:
                return {"ok": False, "error": f"pw_loop_iterate failed: {result}"}

        entries = list(entries_by_id.values())
        target = [entry for entry in entries if entry["global_id"] == node_id]
        nodes = [entry for entry in target if entry["interface"] == "PipeWire:Interface:Node"]
        return {
            "ok": True,
            "sync_complete": sync_complete[0],
            "global_count": len(entries),
            "registry_event_count": len(seen_ids),
            "node_id": node_id,
            "node_exists": bool(nodes),
            "matching_globals": target,
            "node": nodes[0] if nodes else None,
            "duplicate_global_ids": sorted({
                global_id for global_id in seen_ids if seen_ids.count(global_id) > 1
            }),
            "globals": entries,
            "callback_errors": callback_errors,
        }
    finally:
        if core:
            core_disconnect(core)
        if context:
            context_destroy(context)
        if main_loop:
            main_loop_destroy(main_loop)
        if duplicated_fd is not None:
            os.close(duplicated_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fd", type=int, required=True, help="already-open PipeWire remote FD")
    parser.add_argument("--node-id", type=int, required=True, help="portal Start() node ID")
    parser.add_argument("--timeout", type=float, default=3.0, help="registry sync timeout")
    args = parser.parse_args()
    # The probe is optional diagnostics; a native-library failure must not
    # leave a core dump containing unrelated process state.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        os.fstat(args.fd)
        if args.timeout <= 0:
            raise ValueError("timeout must be positive")
        report = inspect_remote(args.fd, args.node_id, args.timeout)
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0 if report.get("ok") else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
