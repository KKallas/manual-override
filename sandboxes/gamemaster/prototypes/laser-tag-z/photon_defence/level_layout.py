"""Validated, atomic edits for the Laser Tag Z tower-socket layout."""

from __future__ import annotations

import copy
import json
import math
import os
import threading
from pathlib import Path
from typing import Any, Callable


SOCKET_LAYER_NAME = "09 Square Placement Spots (16)"
SOCKET_COUNT = 16
SOCKET_MIN_SIZE = 96
SOCKET_MAX_SIZE = 208
SOCKET_MIN_CENTER_DISTANCE = 32


class SocketLayoutError(ValueError):
    """Raised when a submitted socket layout is incomplete or unsafe."""


def _properties(item: dict[str, Any]) -> dict[str, Any]:
    return {
        str(prop.get("name")): prop.get("value")
        for prop in item.get("properties", [])
        if prop.get("name") is not None
    }


def _set_property(item: dict[str, Any], name: str, value: Any, kind: str) -> None:
    for prop in item.setdefault("properties", []):
        if prop.get("name") == name:
            prop.update({"type": kind, "value": value})
            return
    item["properties"].append({"name": name, "type": kind, "value": value})


def _socket_layer(level: dict[str, Any]) -> dict[str, Any]:
    try:
        return next(layer for layer in level.get("layers", []) if layer.get("name") == SOCKET_LAYER_NAME)
    except StopIteration as exc:
        raise SocketLayoutError(f"missing {SOCKET_LAYER_NAME} layer") from exc


def layout_revision(level: dict[str, Any]) -> int:
    value = _properties(level).get("layout_revision", 1)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def socket_records(level: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for obj in _socket_layer(level).get("objects", []):
        props = _properties(obj)
        if obj.get("type") != "TowerSocket":
            continue
        records.append({
            "socket_id": str(props.get("socket_id", obj.get("name", ""))),
            "aruco_id": int(props["aruco_id"]),
            "owner": str(props["owner"]),
            "x": float(obj["x"]),
            "y": float(obj["y"]),
            "size": float(obj["width"]),
            "object_id": int(obj["id"]),
        })
    records.sort(key=lambda item: item["aruco_id"])
    if len(records) != SOCKET_COUNT:
        raise SocketLayoutError(f"expected {SOCKET_COUNT} tower sockets; found {len(records)}")
    if [item["aruco_id"] for item in records] != list(range(40, 56)):
        raise SocketLayoutError("tower sockets must retain ArUco IDs 40 through 55")
    return records


def _finite_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SocketLayoutError(f"{label} must be a finite integer")
    number = float(value)
    if not math.isfinite(number) or abs(number - round(number)) > 1e-6:
        raise SocketLayoutError(f"{label} must be a finite integer")
    return int(round(number))


def _submitted_layout(level: dict[str, Any], submitted: Any) -> dict[str, dict[str, int]]:
    existing = {item["socket_id"]: item for item in socket_records(level)}
    socket_properties = {
        str(_properties(obj).get("socket_id", obj.get("name", ""))): _properties(obj)
        for obj in _socket_layer(level).get("objects", [])
        if obj.get("type") == "TowerSocket"
    }
    if not isinstance(submitted, list) or len(submitted) != SOCKET_COUNT:
        raise SocketLayoutError(f"layout must contain exactly {SOCKET_COUNT} sockets")

    parsed: dict[str, dict[str, int]] = {}
    for index, item in enumerate(submitted):
        if not isinstance(item, dict):
            raise SocketLayoutError(f"socket entry {index + 1} must be an object")
        socket_id = str(item.get("socket_id", ""))
        if socket_id not in existing or socket_id in parsed:
            raise SocketLayoutError(f"unknown or duplicate socket_id: {socket_id or '(blank)'}")
        x = _finite_integer(item.get("x"), f"{socket_id}.x")
        y = _finite_integer(item.get("y"), f"{socket_id}.y")
        size = _finite_integer(item.get("size"), f"{socket_id}.size")
        parsed[socket_id] = {"x": x, "y": y, "size": size}

    if set(parsed) != set(existing):
        raise SocketLayoutError("layout must retain every existing socket_id")

    width = int(level["width"] * level["tilewidth"])
    height = int(level["height"] * level["tileheight"])
    level_properties = _properties(level)
    marker_half = float(level_properties.get("aruco_code_footprint_px", 77)) / 2.0
    marker_gap = float(level_properties.get("active_turret_aruco_gap_px", 0))
    active_turret_size = float(level_properties.get("active_turret_visual_size_px", 112))

    def marker_center(
        item: dict[str, int], properties: dict[str, Any]
    ) -> tuple[float, float]:
        side = int(properties["aruco_side"])
        marker_x = item["x"] + side * (
            active_turret_size / 2 + marker_half + marker_gap
        )
        marker_y = item["y"] + (
            float(properties["aruco_optical_center_v"]) - 0.5
        ) * item["size"]
        return marker_x, marker_y

    def active_turret_rect(
        item: dict[str, int], properties: dict[str, Any]
    ) -> tuple[float, float, float, float]:
        _, center_y = marker_center(item, properties)
        half = active_turret_size / 2
        return (
            item["x"] - half,
            center_y - half,
            item["x"] + half,
            center_y + half,
        )

    for socket_id, item in parsed.items():
        if not (0 <= item["x"] <= width and 0 <= item["y"] <= height):
            raise SocketLayoutError(f"{socket_id} center must remain inside the {width}x{height} playfield")
        if not SOCKET_MIN_SIZE <= item["size"] <= SOCKET_MAX_SIZE:
            raise SocketLayoutError(
                f"{socket_id} size must be between {SOCKET_MIN_SIZE} and {SOCKET_MAX_SIZE}px"
            )
        properties = socket_properties[socket_id]
        marker_x, marker_y = marker_center(item, properties)
        if not (
            marker_half <= marker_x <= width - marker_half
            and marker_half <= marker_y <= height - marker_half
        ):
            raise SocketLayoutError(
                f"{socket_id} side-mounted ArUco marker must remain inside the playfield"
            )
        pad_left, pad_top, pad_right, pad_bottom = active_turret_rect(item, properties)
        if (
            marker_x + marker_half > pad_left
            and marker_x - marker_half < pad_right
            and marker_y + marker_half > pad_top
            and marker_y - marker_half < pad_bottom
        ):
            raise SocketLayoutError(
                f"{socket_id} side-mounted ArUco marker overlaps its active turret position"
            )

    values = list(parsed.items())
    for index, (socket_id, item) in enumerate(values):
        for other_id, other in values[index + 1:]:
            distance = math.hypot(item["x"] - other["x"], item["y"] - other["y"])
            if distance < SOCKET_MIN_CENTER_DISTANCE:
                raise SocketLayoutError(f"{socket_id} and {other_id} are too close together")

    markers = {
        socket_id: marker_center(item, socket_properties[socket_id])
        for socket_id, item in parsed.items()
    }
    for socket_id, (marker_x, marker_y) in markers.items():
        for other_id, other in parsed.items():
            if socket_id == other_id:
                continue
            pad_left, pad_top, pad_right, pad_bottom = active_turret_rect(
                other, socket_properties[other_id]
            )
            if (
                marker_x + marker_half > pad_left
                and marker_x - marker_half < pad_right
                and marker_y + marker_half > pad_top
                and marker_y - marker_half < pad_bottom
            ):
                raise SocketLayoutError(
                    f"{socket_id} ArUco marker overlaps {other_id} active turret position"
                )
    marker_items = list(markers.items())
    for index, (socket_id, (marker_x, marker_y)) in enumerate(marker_items):
        for other_id, (other_x, other_y) in marker_items[index + 1:]:
            if (
                abs(marker_x - other_x) < marker_half * 2
                and abs(marker_y - other_y) < marker_half * 2
            ):
                raise SocketLayoutError(
                    f"{socket_id} and {other_id} ArUco markers overlap"
                )
    return parsed


def _update_gate_geometry(level: dict[str, Any]) -> None:
    sockets = {
        int(obj["id"]): obj
        for obj in _socket_layer(level).get("objects", [])
        if obj.get("type") == "TowerSocket"
    }
    for layer in level.get("layers", []):
        for obj in layer.get("objects", []):
            if obj.get("type") not in {"ForceFieldWall", "GateHint"}:
                continue
            props = _properties(obj)
            socket_a = sockets.get(int(props.get("socket_a", -1)))
            socket_b = sockets.get(int(props.get("socket_b", -1)))
            if socket_a is None or socket_b is None:
                raise SocketLayoutError(f"{obj.get('name', 'gate')} references a missing socket")
            ax, ay = float(socket_a["x"]), float(socket_a["y"])
            bx, by = float(socket_b["x"]), float(socket_b["y"])
            if obj.get("type") == "GateHint":
                obj["x"], obj["y"] = ax, ay
                obj["polyline"] = [{"x": 0, "y": 0}, {"x": bx - ax, "y": by - ay}]
                continue
            dx, dy = bx - ax, by - ay
            obj["x"], obj["y"] = (ax + bx) / 2.0, (ay + by) / 2.0
            obj["height"] = max(32.0, math.hypot(dx, dy) + 14.0)
            obj["rotation"] = math.degrees(math.atan2(dy, dx)) - 90.0


def apply_socket_layout(level: dict[str, Any], submitted: Any) -> dict[str, Any]:
    """Return a validated copy of *level* with socket and gate geometry updated."""
    updated = copy.deepcopy(level)
    parsed = _submitted_layout(updated, submitted)
    for obj in _socket_layer(updated).get("objects", []):
        if obj.get("type") != "TowerSocket":
            continue
        socket_id = str(_properties(obj).get("socket_id", obj.get("name", "")))
        geometry = parsed[socket_id]
        obj.update({
            "x": geometry["x"],
            "y": geometry["y"],
            "width": geometry["size"],
            "height": geometry["size"],
        })
    _update_gate_geometry(updated)
    _set_property(updated, "layout_revision", layout_revision(updated) + 1, "int")
    return updated


def update_socket_layout_file(
    map_path: str | Path,
    submitted: Any,
    *,
    validate_candidate: Callable[[Path], Any] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Validate and atomically persist a socket layout."""
    path = Path(map_path)
    level = json.loads(path.read_text(encoding="utf-8"))
    updated = apply_socket_layout(level, submitted)
    temp_path = path.with_name(
        f".{path.name}.layout-{os.getpid()}-{threading.get_ident()}.tmp"
    )
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(updated, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if validate_candidate is not None:
            validate_candidate(temp_path)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return layout_revision(updated), socket_records(updated)
