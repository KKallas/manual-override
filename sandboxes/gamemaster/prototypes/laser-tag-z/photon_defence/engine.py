"""Small authoritative tower-defence simulation for the Laser Tag Z vertical slice."""

from __future__ import annotations

import heapq
import json
import math
import random
import threading
import time
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

from .level_layout import layout_revision


MAX_ACTIVE_ENEMIES = 1000
COLLISION_PADDING = 0.05
COLLISION_CELL_SIZE = 12.0
FLOW_SPAWN_OFFSETS = (
    0.0, -9.0, 9.0, -18.0, 18.0, -27.0, 27.0,
    -4.5, 4.5, -13.5, 13.5, -22.5, 22.5,
)
FLOW_CORRIDOR_RADIUS = 28.0
FLOW_NEIGHBOR_MARGIN = 5.0
PARTICLE_STEERING_RESPONSE = 7.5
PARTICLE_PRESSURE_WEIGHT = 0.9
PARTICLE_WANDER_WEIGHT = 0.32
PARTICLE_SOLVER_ITERATIONS = 18
PARTICLE_MAX_SPEED_SCALE = 1.35
PATH_WAYPOINT_RADIUS = 8.0
CORNER_RADIUS = 30.0
CORNER_SAMPLES = 6
CORE_BASIN_HALF_SIZE = 112.0
# Eight outward half-planes measured from the opaque alpha silhouette of the
# 144 px central_core_square_base Tiled object. Diagonal normals preserve its
# 45-degree corners; limits are relative to the authored core node.
CORE_OCTAGON_PLANES = (
    (1.0, 0.0, 43.5),     # right
    (-1.0, 0.0, 52.0),    # left
    (0.0, 1.0, 40.5),     # bottom
    (0.0, -1.0, 52.0),    # top
    (1.0, 1.0, 69.0),     # bottom-right
    (-1.0, -1.0, 89.25),  # top-left
    (1.0, -1.0, 80.25),   # top-right
    (-1.0, 1.0, 77.5),    # bottom-left
)
CORE_OCTAGON_MAX_AXIS_EXTENT = 52.0
CORE_BASIN_SPEED = 40.0
CORE_ENTRY_DISTANCE = 9.0
ATOM_OWNERS = {100: "green", 101: "green", 102: "purple", 103: "purple"}
TOWER_TYPES = {"machine_gun", "flamethrower", "mortar", "tesla_coil"}
DEFAULT_LOADOUT = {
    100: "machine_gun",
    101: "flamethrower",
    102: "mortar",
    103: "tesla_coil",
}
ENEMY_STATS = {
    "grunt": {"hp": 70.0, "speed": 62.0, "core_dps": 6.0, "collision_radius": 2.2},
    "runner": {"hp": 46.0, "speed": 104.0, "core_dps": 4.0, "collision_radius": 2.2},
    "breaker": {"hp": 130.0, "speed": 50.0, "core_dps": 10.0, "collision_radius": 2.2},
    "brute": {"hp": 240.0, "speed": 34.0, "core_dps": 16.0, "collision_radius": 2.8},
}
TOWER_STATS = {
    "machine_gun": {"near_range": 190.0, "far_range": 360.0, "wide_half_angle": 55.0, "narrow_half_angle": 12.0, "rate": 6.0},
    "flamethrower": {"near_range": 130.0, "far_range": 235.0, "wide_half_angle": 65.0, "narrow_half_angle": 18.0, "rate": 4.0},
    "mortar": {"min_range": 110.0, "max_range": 500.0, "near_splash": 72.0, "far_splash": 155.0, "rate": 0.8},
    "tesla_coil": {"min_range": 120.0, "range": 265.0, "rate": 1.0},
}
FLAMETHROWER_SWEEP_PERIOD_S = 1.6
FLAMETHROWER_PATH_SEGMENTS = 18
FLAMETHROWER_TRAIL_LAG_S = 0.48
FLAMETHROWER_MUZZLE_OFFSET = 35.0
FLAMETHROWER_HIT_RADIUS = 12.0
MORTAR_FLIGHT_DURATION_S = 0.9
MORTAR_IMPACT_VISIBLE_S = 0.9
TESLA_DAMAGE_FALLOFF = 0.82
TESLA_EFFECT_DURATION_S = 0.48
TESLA_CLOSE_DAMAGE_MULTIPLIER = 1.75
TESLA_MAX_RANGE_VISUAL_INTENSITY = 0.58
FIELD_CONTACT_DISTANCE = 24.0
FIELD_CONTACT_REARM_S = 0.45
FORCE_FIELD_ZAP_DURATION_S = 0.5
FORCE_FIELD_IMPACT_RETENTION_S = 0.65
# Orcs attack the physical 112 px defense pod, not only the smaller 88 px
# rotating weapon sprite. Melee reach is intentionally explicit so render
# scale changes cannot silently alter authoritative combat geometry.
TOWER_POD_RADIUS = 56.0
ORC_TOWER_MELEE_REACH = 38.0
TOWER_ATTACK_RADIUS = TOWER_POD_RADIUS + ORC_TOWER_MELEE_REACH
TOWER_LINK_START_MULTIPLIER = 0.9
TOWER_LINK_MULTIPLIER_STEP = 0.1
RING_MIN_TURRETS = 8
RING_MAX_TURRETS = 16
CORE_MARKER_ID = 38
CORE_TAG_STABLE_S = 0.55
CORE_TAG_DISTANCE = 0.05
CORE_DETONATION_DURATION_S = 2.4
DEFAULT_SETTINGS = {
    "wave_count": 12,
    "wave_interval_s": 45.0,
    "enemy_health_multiplier": 1.0,
    "enemy_speed_multiplier": 1.0,
    "enemy_core_damage_multiplier": 1.0,
    "enemy_tower_damage_multiplier": 1.0,
    "enemy_count_multiplier": 1.0,
    "release_rate_multiplier": 1.0,
    "force_field_damage_per_s": 8.0,
    "force_field_slow": 0.55,
    "force_field_hit_capacity": 50,
    "ring_field_immunity_s": 100.0,
    "machine_gun_damage": 13.0,
    "flamethrower_damage": 10.0,
    "flamethrower_burn_damage_per_s": 4.0,
    "flamethrower_burn_duration_s": 3.0,
    "mortar_damage": 62.0,
    "mortar_far_damage_multiplier": 0.55,
    "tesla_damage": 36.0,
    "tesla_link_distance": 90.0,
    "tesla_max_links": 10,
    "defense_unit_health_percent": 15.0,
    "tower_link_start_multiplier": TOWER_LINK_START_MULTIPLIER,
    "tower_link_step": TOWER_LINK_MULTIPLIER_STEP,
    "core_hp": 10000.0,
    "max_active_enemies": MAX_ACTIVE_ENEMIES,
}


def _properties(item: dict[str, Any]) -> dict[str, Any]:
    return {entry["name"]: entry.get("value") for entry in item.get("properties", [])}


def _distance_point_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _segment_circle_overlap_fraction(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    center_x: float,
    center_y: float,
    radius: float,
) -> float:
    """Return the exact fraction of a movement segment inside a circle."""
    dx, dy = bx - ax, by - ay
    start_x, start_y = ax - center_x, ay - center_y
    length_sq = dx * dx + dy * dy
    radius_sq = max(0.0, radius) ** 2
    if length_sq <= 1e-12:
        return 1.0 if start_x * start_x + start_y * start_y <= radius_sq else 0.0

    linear = 2.0 * (start_x * dx + start_y * dy)
    constant = start_x * start_x + start_y * start_y - radius_sq
    discriminant = linear * linear - 4.0 * length_sq * constant
    if discriminant < 0.0:
        return 1.0 if constant <= 0.0 else 0.0

    root = math.sqrt(max(0.0, discriminant))
    enter = (-linear - root) / (2.0 * length_sq)
    leave = (-linear + root) / (2.0 * length_sq)
    return max(0.0, min(1.0, leave) - max(0.0, enter))


def _angle_delta(angle: float, reference: float) -> float:
    return (angle - reference + math.pi) % (math.pi * 2.0) - math.pi


def _segments_intersect(
    ax: float, ay: float, bx: float, by: float,
    cx: float, cy: float, dx: float, dy: float,
) -> bool:
    if max(ax, bx) + 1e-6 < min(cx, dx) or max(cx, dx) + 1e-6 < min(ax, bx):
        return False
    if max(ay, by) + 1e-6 < min(cy, dy) or max(cy, dy) + 1e-6 < min(ay, by):
        return False

    def orientation(px: float, py: float, qx: float, qy: float, rx: float, ry: float) -> float:
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)

    first = orientation(ax, ay, bx, by, cx, cy)
    second = orientation(ax, ay, bx, by, dx, dy)
    third = orientation(cx, cy, dx, dy, ax, ay)
    fourth = orientation(cx, cy, dx, dy, bx, by)
    return first * second <= 1e-6 and third * fourth <= 1e-6


def _point_in_polygon(
    point_x: float, point_y: float, points: list[tuple[float, float]]
) -> bool:
    """Return whether a point is strictly inside a simple polygon."""
    inside = False
    previous_x, previous_y = points[-1]
    for current_x, current_y in points:
        if (current_y > point_y) != (previous_y > point_y):
            crossing_x = (
                (previous_x - current_x) * (point_y - current_y)
                / (previous_y - current_y) + current_x
            )
            if point_x < crossing_x:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def _is_simple_polygon(points: list[tuple[float, float]]) -> bool:
    if len(points) < 3 or len(set(points)) != len(points):
        return False
    count = len(points)
    for first_index in range(count):
        first_end = (first_index + 1) % count
        ax, ay = points[first_index]
        bx, by = points[first_end]
        for second_index in range(first_index + 1, count):
            second_end = (second_index + 1) % count
            if first_index in {second_index, second_end} or first_end in {second_index, second_end}:
                continue
            cx, cy = points[second_index]
            dx, dy = points[second_end]
            if _segments_intersect(ax, ay, bx, by, cx, cy, dx, dy):
                return False
    return True


def _is_valid_core_ring(
    points: list[tuple[float, float]], core_x: float, core_y: float
) -> bool:
    return (
        RING_MIN_TURRETS <= len(points) <= RING_MAX_TURRETS
        and _is_simple_polygon(points)
        and _point_in_polygon(core_x, core_y, points)
    )


def _segment_intersects_square(
    ax: float, ay: float, bx: float, by: float,
    center_x: float, center_y: float, size: float,
) -> bool:
    half = size / 2.0
    left, right = center_x - half, center_x + half
    top, bottom = center_y - half, center_y + half
    if left <= ax <= right and top <= ay <= bottom:
        return True
    if left <= bx <= right and top <= by <= bottom:
        return True
    return any((
        _segments_intersect(ax, ay, bx, by, left, top, right, top),
        _segments_intersect(ax, ay, bx, by, right, top, right, bottom),
        _segments_intersect(ax, ay, bx, by, right, bottom, left, bottom),
        _segments_intersect(ax, ay, bx, by, left, bottom, left, top),
    ))


def _segment_intersects_polygon(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    points: tuple[tuple[float, float], ...],
) -> bool:
    """Return whether a closed authored blocker intersects a connection."""
    if len(points) < 3:
        return False
    polygon = list(points)
    if _point_in_polygon(ax, ay, polygon) or _point_in_polygon(bx, by, polygon):
        return True
    return any(
        _segments_intersect(ax, ay, bx, by, cx, cy, dx, dy)
        for (cx, cy), (dx, dy) in zip(points, (*points[1:], points[0]))
    )


def _closest_point_on_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> tuple[float, float, float]:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return ax, ay, 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return ax + dx * t, ay + dy * t, t


def _core_octagon_face(
    dx: float, dy: float, radius: float = 0.0
) -> tuple[int, float, float, float]:
    """Return the nearest outward core face and body-to-face clearance."""
    best = (0, 1.0, 0.0, -math.inf)
    for index, (normal_x, normal_y, limit) in enumerate(CORE_OCTAGON_PLANES):
        length = math.hypot(normal_x, normal_y)
        unit_x, unit_y = normal_x / length, normal_y / length
        clearance = (
            normal_x * dx + normal_y * dy - limit
        ) / length - radius
        if clearance > best[3]:
            best = index, unit_x, unit_y, clearance
    return best


def _tile_draw_offset(alignment: str, width: float, height: float) -> tuple[float, float]:
    value = (alignment or "bottomleft").lower()
    dx = 0.0 if "left" in value else -width if "right" in value else -width / 2.0
    dy = 0.0 if value.startswith("top") else -height if value.startswith("bottom") else -height / 2.0
    return dx, dy


def _offset_polyline(
    points: list[tuple[float, float]], offset: float
) -> list[tuple[float, float]]:
    """Return a parallel polyline, using mitered corners for orthogonal paths."""
    if not points or abs(offset) <= 1e-9:
        return list(points)
    if len(points) == 1:
        return list(points)

    normals: list[tuple[float, float]] = []
    for start, end in zip(points, points[1:]):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        normals.append((-dy / length, dx / length) if length > 1e-9 else (0.0, 0.0))

    shifted: list[tuple[float, float]] = []
    for index, point in enumerate(points):
        if index == 0:
            normal = normals[0]
            scale = offset
        elif index == len(points) - 1:
            normal = normals[-1]
            scale = offset
        else:
            previous_normal, next_normal = normals[index - 1], normals[index]
            sum_x = previous_normal[0] + next_normal[0]
            sum_y = previous_normal[1] + next_normal[1]
            denominator = 1.0 + (
                previous_normal[0] * next_normal[0] + previous_normal[1] * next_normal[1]
            )
            if abs(denominator) <= 1e-9:
                normal = next_normal
                scale = offset
            else:
                normal = (sum_x, sum_y)
                scale = offset / denominator
        shifted.append((point[0] + normal[0] * scale, point[1] + normal[1] * scale))
    return shifted


def _rounded_polyline(
    points: list[tuple[float, float]], radius: float = CORNER_RADIUS
) -> list[tuple[float, float]]:
    """Round polyline corners with quadratic arcs so motion keeps its inertia."""
    clean = [point for index, point in enumerate(points) if index == 0 or point != points[index - 1]]
    if len(clean) < 3:
        return clean

    rounded = [clean[0]]
    for previous, corner, following in zip(clean, clean[1:-1], clean[2:]):
        in_dx, in_dy = corner[0] - previous[0], corner[1] - previous[1]
        out_dx, out_dy = following[0] - corner[0], following[1] - corner[1]
        in_length, out_length = math.hypot(in_dx, in_dy), math.hypot(out_dx, out_dy)
        if in_length <= 1e-9 or out_length <= 1e-9:
            continue
        in_x, in_y = in_dx / in_length, in_dy / in_length
        out_x, out_y = out_dx / out_length, out_dy / out_length
        if abs(in_x * out_y - in_y * out_x) <= 1e-6:
            rounded.append(corner)
            continue
        trim = min(radius, in_length * 0.45, out_length * 0.45)
        entry = (corner[0] - in_x * trim, corner[1] - in_y * trim)
        exit_point = (corner[0] + out_x * trim, corner[1] + out_y * trim)
        rounded.append(entry)
        for sample in range(1, CORNER_SAMPLES + 1):
            t = sample / CORNER_SAMPLES
            inverse = 1.0 - t
            rounded.append((
                inverse * inverse * entry[0] + 2.0 * inverse * t * corner[0] + t * t * exit_point[0],
                inverse * inverse * entry[1] + 2.0 * inverse * t * corner[1] + t * t * exit_point[1],
            ))
    rounded.append(clean[-1])
    return rounded


class _CollisionGrid:
    """Small spatial hash used for collision checks at the 1000-orc cap."""

    def __init__(self, enemies: list[dict[str, Any]]) -> None:
        self.cells: dict[tuple[int, int], set[int]] = defaultdict(set)
        self.entries: dict[int, tuple[float, float, float]] = {}
        self.entry_cells: dict[int, tuple[int, int]] = {}
        for enemy in enemies:
            self.add(enemy["id"], enemy["x"], enemy["y"], enemy["collision_radius"])

    @staticmethod
    def _cell(x: float, y: float) -> tuple[int, int]:
        return math.floor(x / COLLISION_CELL_SIZE), math.floor(y / COLLISION_CELL_SIZE)

    def add(
        self, enemy_id: int, x: float, y: float, radius: float
    ) -> None:
        cell = self._cell(x, y)
        self.entries[enemy_id] = (x, y, radius)
        self.entry_cells[enemy_id] = cell
        self.cells[cell].add(enemy_id)

    def remove(self, enemy_id: int) -> None:
        cell = self.entry_cells.pop(enemy_id, None)
        self.entries.pop(enemy_id, None)
        if cell is not None:
            self.cells[cell].discard(enemy_id)

    def collides(self, x: float, y: float, radius: float) -> bool:
        cell_x, cell_y = self._cell(x, y)
        for offset_y in (-1, 0, 1):
            for offset_x in (-1, 0, 1):
                for enemy_id in self.cells.get((cell_x + offset_x, cell_y + offset_y), ()):
                    other_x, other_y, other_radius = self.entries[enemy_id]
                    minimum = radius + other_radius + COLLISION_PADDING
                    if (x - other_x) ** 2 + (y - other_y) ** 2 < minimum ** 2 - 1e-6:
                        return True
        return False

    def pressure(
        self, x: float, y: float, radius: float, enemy_id: int
    ) -> tuple[float, float]:
        """Return a smooth separation force before hard-disc contact occurs."""
        force_x = force_y = 0.0
        cell_x, cell_y = self._cell(x, y)
        for offset_y in (-1, 0, 1):
            for offset_x in (-1, 0, 1):
                for other_id in self.cells.get((cell_x + offset_x, cell_y + offset_y), ()):
                    if other_id == enemy_id:
                        continue
                    other_x, other_y, other_radius = self.entries[other_id]
                    dx, dy = x - other_x, y - other_y
                    preferred = radius + other_radius + FLOW_NEIGHBOR_MARGIN
                    distance_sq = dx * dx + dy * dy
                    if distance_sq >= preferred * preferred:
                        continue
                    if distance_sq <= 1e-18:
                        angle = (enemy_id * 2.399963229728653 + other_id) % (math.pi * 2.0)
                        direction_x, direction_y = math.cos(angle), math.sin(angle)
                        distance = 0.0
                    else:
                        distance = math.sqrt(distance_sq)
                        direction_x, direction_y = dx / distance, dy / distance
                    strength = (preferred - distance) / preferred
                    force_x += direction_x * strength
                    force_y += direction_y * strength
        return force_x, force_y


class LevelModel:
    def __init__(self, map_path: str | Path) -> None:
        self.map_path = Path(map_path)
        self.data = json.loads(self.map_path.read_text(encoding="utf-8"))
        self.layout_revision = layout_revision(self.data)
        self.width = int(self.data["width"] * self.data["tilewidth"])
        self.height = int(self.data["height"] * self.data["tileheight"])
        map_properties = _properties(self.data)
        try:
            self.aruco_code_footprint_px = float(
                map_properties["aruco_code_footprint_px"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "level must define a finite aruco_code_footprint_px"
            ) from exc
        if (
            not math.isfinite(self.aruco_code_footprint_px)
            or self.aruco_code_footprint_px <= 0.0
        ):
            raise ValueError("aruco_code_footprint_px must be positive and finite")
        try:
            self.core_aruco_code_footprint_px = float(
                map_properties["core_aruco_code_footprint_px"]
            )
            self.force_field_marker_clearance_px = float(
                map_properties["force_field_marker_clearance_px"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "level must define core ArUco footprint and force-field clearance"
            ) from exc
        if (
            not math.isfinite(self.core_aruco_code_footprint_px)
            or self.core_aruco_code_footprint_px <= 0.0
            or not math.isfinite(self.force_field_marker_clearance_px)
            or self.force_field_marker_clearance_px < 0.0
        ):
            raise ValueError(
                "core ArUco footprint and force-field clearance must be finite"
            )
        self.tileset_alignments: list[tuple[int, str]] = []
        for reference in self.data.get("tilesets", []):
            source = reference.get("source")
            if not source:
                continue
            tileset_path = (self.map_path.parent / source).resolve()
            tileset = json.loads(tileset_path.read_text(encoding="utf-8"))
            self.tileset_alignments.append(
                (int(reference["firstgid"]), str(tileset.get("objectalignment") or "bottomleft"))
            )
        self.tileset_alignments.sort()
        layers = {layer["name"]: layer for layer in self.data["layers"]}
        node_objects = layers["07 Path Nodes (hidden)"]["objects"]
        self.nodes = {obj["id"]: {"name": obj["name"], "x": float(obj["x"]), "y": float(obj["y"]), **_properties(obj)} for obj in node_objects}
        self.node_by_name = {node["name"]: node for node in self.nodes.values()}
        self.core = next(node for node in self.nodes.values() if node.get("node_kind") == "core")
        self.spawns = {node["spawn_group"]: node for node in self.nodes.values() if node.get("node_kind") == "spawn"}
        self.edges: dict[int, list[dict[str, Any]]] = {}
        self.edge_by_id: dict[str, dict[str, Any]] = {}
        for obj in layers["06 Enemy Path Graph (hidden)"]["objects"]:
            props = _properties(obj)
            points = [(float(obj["x"]) + float(point["x"]), float(obj["y"]) + float(point["y"])) for point in obj.get("polyline", [])]
            if not points:
                points = [(self.nodes[props["from_node"]]["x"], self.nodes[props["from_node"]]["y"]), (self.nodes[props["to_node"]]["x"], self.nodes[props["to_node"]]["y"])]
            segment_lengths = [
                math.hypot(end[0] - start[0], end[1] - start[1])
                for start, end in zip(points, points[1:])
            ]
            edge = {
                "from": int(props["from_node"]),
                "to": int(props["to_node"]),
                "cost": float(props.get("base_cost", 1.0)),
                "points": points,
                "segment_lengths": segment_lengths,
                "path_length": sum(segment_lengths),
                "edge_id": str(props.get("edge_id", obj["name"])),
            }
            self.edges.setdefault(edge["from"], []).append(edge)
            self.edge_by_id[edge["edge_id"]] = edge
        self.route_edge_ids: dict[str, list[str]] = {}
        self.paths: dict[str, list[tuple[float, float]]] = {}
        for group, node in self.spawns.items():
            route, _ = self._shortest_route_with_cost(self._node_id(node))
            self.route_edge_ids[group] = [edge["edge_id"] for edge in route]
            self.paths[group] = self._points_for_edges(route)
        self.junctions = {
            (float(node["x"]), float(node["y"]))
            for node in self.nodes.values()
            if node.get("node_kind") in {"junction", "arrival", "turnaround"}
        }
        self.sockets: dict[str, dict[str, Any]] = {}
        self.socket_by_marker: dict[int, str] = {}
        ring_neighbor_markers: dict[str, set[int]] = {}
        for obj in layers["09 Square Placement Spots (16)"]["objects"]:
            props = _properties(obj)
            socket_id, marker = str(props["socket_id"]), int(props["aruco_id"])
            x, y = self._tile_object_center(obj)
            socket = {"socket_id": socket_id, "aruco_id": marker, "owner": props["owner"], "x": x, "y": y, "size": float(obj.get("width", 208))}
            self.sockets[socket_id] = socket
            self.socket_by_marker[marker] = socket_id
            try:
                ring_neighbor_markers[socket_id] = {
                    int(value.strip())
                    for value in str(props.get("ring_neighbors") or "").split(",")
                    if value.strip()
                }
            except ValueError as exc:
                raise ValueError(
                    f"socket {marker} has an invalid ring_neighbors property"
                ) from exc
        if sorted(self.socket_by_marker) != list(range(40, 56)):
            raise ValueError("level must map ArUco IDs 40-55 to exactly sixteen sockets")

        # Ingest only immutable authored blockers here. Dynamic empty-socket
        # footprints are evaluated separately from current occupancy so their
        # result never depends on editable placement UI geometry.
        blockers: list[dict[str, Any]] = []
        blocker_ids: set[str] = set()
        pending_layers = list(self.data.get("layers", []))
        while pending_layers:
            layer = pending_layers.pop(0)
            pending_layers[0:0] = list(layer.get("layers", []))
            for obj in layer.get("objects", []):
                if str(obj.get("class") or obj.get("type") or "") != "ForceFieldBlocker":
                    continue
                props = _properties(obj)
                blocker_id = str(
                    props.get("blocker_id")
                    or obj.get("name")
                    or f"force_field_blocker_{obj.get('id', len(blockers) + 1)}"
                )
                if blocker_id in blocker_ids:
                    raise ValueError(
                        f"duplicate ForceFieldBlocker id: {blocker_id}"
                    )
                origin_x = float(obj.get("x", 0.0))
                origin_y = float(obj.get("y", 0.0))
                if obj.get("polygon"):
                    points = tuple(
                        (
                            origin_x + float(point["x"]),
                            origin_y + float(point["y"]),
                        )
                        for point in obj["polygon"]
                    )
                else:
                    width = float(obj.get("width", 0.0))
                    height = float(obj.get("height", 0.0))
                    if width <= 0.0 or height <= 0.0:
                        raise ValueError(
                            f"ForceFieldBlocker {blocker_id} must be a polygon "
                            "or a non-empty rectangle"
                        )
                    points = (
                        (origin_x, origin_y),
                        (origin_x + width, origin_y),
                        (origin_x + width, origin_y + height),
                        (origin_x, origin_y + height),
                    )
                if not _is_simple_polygon(list(points)):
                    raise ValueError(
                        f"ForceFieldBlocker {blocker_id} must be a simple polygon"
                    )
                blocker_ids.add(blocker_id)
                blockers.append({"blocker_id": blocker_id, "points": points})
        self.force_field_blockers = tuple(
            sorted(blockers, key=lambda blocker: blocker["blocker_id"])
        )

        def build_adjacency(
            neighbor_map: dict[str, set[int]], property_name: str
        ) -> dict[str, set[str]]:
            adjacency: dict[str, set[str]] = {}
            for socket_id, neighbor_markers in neighbor_map.items():
                marker = int(self.sockets[socket_id]["aruco_id"])
                unknown = sorted(neighbor_markers - set(self.socket_by_marker))
                if unknown:
                    raise ValueError(
                        f"socket {marker} has unknown {property_name}: {unknown}"
                    )
                if marker in neighbor_markers:
                    raise ValueError(
                        f"socket {marker} cannot be its own {property_name}"
                    )
                for neighbor_marker in neighbor_markers:
                    neighbor_id = self.socket_by_marker[neighbor_marker]
                    if marker not in neighbor_map.get(neighbor_id, set()):
                        raise ValueError(
                            f"{property_name} must be symmetric between "
                            f"{marker} and {neighbor_marker}"
                        )
                adjacency[socket_id] = {
                    self.socket_by_marker[neighbor_marker]
                    for neighbor_marker in neighbor_markers
                }
            return adjacency

        self.ring_adjacency = build_adjacency(
            ring_neighbor_markers, "ring_neighbors"
        )
        self.ring_edges = sorted({
            tuple(sorted((first_socket, second_socket)))
            for first_socket, neighbors in self.ring_adjacency.items()
            for second_socket in neighbors
        })
        self.ring_cycles = self._build_ring_cycles()
        if not self.ring_cycles:
            raise ValueError("level must define at least one valid 8-16 socket ring")

    def _build_ring_cycles(self) -> list[list[str]]:
        found: set[tuple[str, ...]] = set()

        def canonical(path: list[str]) -> tuple[str, ...]:
            variants = []
            for candidate in (path, list(reversed(path))):
                for index in range(len(candidate)):
                    variants.append(tuple(candidate[index:] + candidate[:index]))
            return min(variants)

        def visit(start: str, current: str, path: list[str]) -> None:
            for neighbor in self.ring_adjacency.get(current, set()):
                if neighbor == start:
                    if len(path) >= RING_MIN_TURRETS:
                        found.add(canonical(path))
                    continue
                if neighbor in path or len(path) >= RING_MAX_TURRETS:
                    continue
                visit(start, neighbor, [*path, neighbor])

        for socket_id in self.sockets:
            visit(socket_id, socket_id, [socket_id])

        valid = []
        for cycle in found:
            points = [
                (self.sockets[socket_id]["x"], self.sockets[socket_id]["y"])
                for socket_id in cycle
            ]
            if _is_valid_core_ring(
                points, float(self.core["x"]), float(self.core["y"])
            ):
                valid.append(list(cycle))
        return sorted(valid, key=lambda cycle: (len(cycle), cycle))

    def ring_edge_allowed(self, first_socket: str, second_socket: str) -> bool:
        return second_socket in self.ring_adjacency.get(first_socket, set())

    def _node_id(self, node: dict[str, Any]) -> int:
        return next(node_id for node_id, candidate in self.nodes.items() if candidate is node)

    def _tile_object_center(self, obj: dict[str, Any]) -> tuple[float, float]:
        gid = int(obj["gid"])
        alignment = "bottomleft"
        for first_gid, candidate in self.tileset_alignments:
            if gid < first_gid:
                break
            alignment = candidate
        width, height = float(obj["width"]), float(obj["height"])
        dx, dy = _tile_draw_offset(alignment, width, height)
        local_x, local_y = dx + width / 2.0, dy + height / 2.0
        rotation = math.radians(float(obj.get("rotation", 0.0)))
        cos, sin = math.cos(rotation), math.sin(rotation)
        return (
            float(obj["x"]) + local_x * cos - local_y * sin,
            float(obj["y"]) + local_x * sin + local_y * cos,
        )

    def _shortest_route_with_cost(
        self,
        start_id: int,
        blocked_edges: set[str] | frozenset[str] | None = None,
        edge_penalties: dict[str, float] | None = None,
    ) -> tuple[list[dict[str, Any]], float]:
        blocked_edges = blocked_edges or set()
        edge_penalties = edge_penalties or {}
        core_id = self._node_id(self.core)
        distances = {start_id: 0.0}
        previous: dict[int, tuple[int, dict[str, Any]]] = {}
        queue = [(0.0, start_id)]
        while queue:
            distance, node_id = heapq.heappop(queue)
            if distance != distances.get(node_id):
                continue
            if node_id == core_id:
                break
            for edge in self.edges.get(node_id, []):
                if edge["edge_id"] in blocked_edges:
                    continue
                candidate = distance + edge["cost"] + max(
                    0.0, float(edge_penalties.get(edge["edge_id"], 0.0))
                )
                if candidate < distances.get(edge["to"], math.inf):
                    distances[edge["to"]] = candidate
                    previous[edge["to"]] = (node_id, edge)
                    heapq.heappush(queue, (candidate, edge["to"]))
        if core_id not in distances:
            raise ValueError(f"spawn node {start_id} cannot reach the core")
        ordered = []
        cursor = core_id
        while cursor != start_id:
            parent, edge = previous[cursor]
            ordered.append(edge)
            cursor = parent
        return list(reversed(ordered)), distances[core_id]

    @staticmethod
    def _points_for_edges(
        edges: list[dict[str, Any]],
    ) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        for edge in edges:
            for point in edge["points"]:
                if not points or point != points[-1]:
                    points.append(point)
        return points

    def _shortest_path_with_cost(
        self,
        start_id: int,
        blocked_edges: set[str] | frozenset[str] | None = None,
        edge_penalties: dict[str, float] | None = None,
    ) -> tuple[list[tuple[float, float]], float]:
        route, cost = self._shortest_route_with_cost(
            start_id, blocked_edges, edge_penalties
        )
        return self._points_for_edges(route), cost

    def _shortest_path(
        self, start_id: int, blocked_edges: set[str] | frozenset[str] | None = None
    ) -> list[tuple[float, float]]:
        return self._shortest_path_with_cost(start_id, blocked_edges)[0]

    def path_from_node(
        self, node_id: int, blocked_edges: set[str] | frozenset[str] | None = None
    ) -> list[tuple[float, float]] | None:
        try:
            return self._shortest_path(int(node_id), blocked_edges)
        except ValueError:
            return None

    def weakest_path_from_node(
        self, node_id: int, edge_penalties: dict[str, float]
    ) -> tuple[list[tuple[float, float]], float] | None:
        try:
            return self._shortest_path_with_cost(
                int(node_id), edge_penalties=edge_penalties
            )
        except ValueError:
            return None

    def weakest_route_from_node(
        self, node_id: int, edge_penalties: dict[str, float]
    ) -> tuple[list[dict[str, Any]], float] | None:
        try:
            return self._shortest_route_with_cost(
                int(node_id), edge_penalties=edge_penalties
            )
        except ValueError:
            return None

    def edges_crossed_by_segment(
        self, ax: float, ay: float, bx: float, by: float
    ) -> set[str]:
        crossed: set[str] = set()
        for edge in self.edge_by_id.values():
            for start, end in zip(edge["points"], edge["points"][1:]):
                if _segments_intersect(ax, ay, bx, by, start[0], start[1], end[0], end[1]):
                    crossed.add(edge["edge_id"])
                    break
        return crossed

    def project_onto_edge(
        self, edge_id: str, x: float, y: float
    ) -> tuple[float, float, int, float, float, float]:
        """Project a point onto one authored edge and report edge progress."""
        edge = self.edge_by_id[edge_id]
        points = edge["points"]
        lengths = edge["segment_lengths"]
        total = max(1e-9, float(edge["path_length"]))
        travelled = 0.0
        best = (points[0][0], points[0][1], 0, 0.0, 0.0, math.inf)
        for index, (start, end, length) in enumerate(
            zip(points, points[1:], lengths)
        ):
            point_x, point_y, ratio = _closest_point_on_segment(
                x, y, start[0], start[1], end[0], end[1]
            )
            distance = math.hypot(x - point_x, y - point_y)
            if distance < best[5]:
                progress = (travelled + length * ratio) / total
                best = point_x, point_y, index, ratio, progress, distance
            travelled += length
        return best


class DefenseEngine:
    def __init__(self, map_path: str | Path, wave_path: str | Path) -> None:
        self.level = LevelModel(map_path)
        self.wave_source = json.loads(Path(wave_path).read_text(encoding="utf-8"))["waves"]
        self.lock = threading.RLock()
        self._wake: Callable[[], None] | None = None
        self._last_simulation_wake_at = 0.0
        self._physical_source: Callable[[], tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rng = random.Random(42055)
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.RLock()):
            self.phase = "setup"
            self.paused = False
            self.virtual_play = False
            self.sim_time = 0.0
            self.run_started_at = None
            self.core_hp = float(DEFAULT_SETTINGS["core_hp"])
            self.core_max_hp = float(DEFAULT_SETTINGS["core_hp"])
            self.settings = dict(DEFAULT_SETTINGS)
            self.current_wave = 0
            self.launched_waves: list[dict[str, Any]] = []
            self.next_wave_at = 0.0
            self.enemies: dict[int, dict[str, Any]] = {}
            self.next_enemy_id = 1
            self.track_cursor: dict[str, int] = defaultdict(int)
            self.pressure_bank = 0
            self.pressure_queue: list[dict[str, Any]] = []
            self.pending_mortar_rounds: list[dict[str, Any]] = []
            self.mortar_impacts: list[dict[str, Any]] = []
            self.next_projectile_id = 1
            self.kills = 0
            self.breaches = 0
            self.placements: dict[str, dict[str, Any]] = {}
            self.activation_order: list[str] = []
            self.placement_link_attempts: list[dict[str, Any]] = []
            self.loadout = dict(DEFAULT_LOADOUT)
            self.force_fields: dict[str, dict[str, Any]] = {}
            self.force_field_impacts: list[dict[str, Any]] = []
            self.next_force_field_impact_id = 1
            self.marker_cache: dict[int, dict[str, Any]] = {}
            self.physical_candidates: dict[int, dict[str, Any]] = {}
            self.ring_completed_at: float | None = None
            self.ring_socket_ids: list[str] = []
            self.ring_candidate_socket_ids: list[str] = []
            self.ring_candidate_source: str | None = None
            self.ring_closing_pair: list[str] = []
            self.ring_last_evaluation: dict[str, Any] | None = None
            self.ring_alternative_evaluations: list[dict[str, Any]] = []
            self.ring_rejected_evaluations: list[dict[str, Any]] = []
            self.ring_search_evaluated_count = 0
            self.field_immunity_until = 0.0
            self.core_stage = "locked"
            self.core_first_tag: int | None = None
            self.core_first_team: str | None = None
            self.core_detonation_started_at: float | None = None
            self.core_purge_target_ids: set[int] = set()
            self.core_purge_ignited_ids: set[int] = set()
            self.events: list[dict[str, Any]] = []

    def set_wake(self, callback: Callable[[], None]) -> None:
        self._wake = callback

    def set_physical_source(self, callback: Callable[[], tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]]) -> None:
        self._physical_source = callback

    def reload_level(self) -> None:
        """Reload setup geometry after a validated Gamemaster layout edit."""
        with self.lock:
            if self.phase != "setup":
                raise ValueError("turret positions can only be changed before a run")
            next_level = LevelModel(self.level.map_path)
            virtual_play = self.virtual_play
            loadout = dict(self.loadout)
            self.level = next_level
            self.reset()
            self.virtual_play = virtual_play
            self.loadout = loadout
            self._reconcile_connections(reason="level_reloaded")
            self._changed()

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="laser-tag-z-defence", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(0.05):
            now = time.monotonic()
            dt = min(0.1, now - last)
            last = now
            if self._physical_source and not self.virtual_play:
                try:
                    tags, arms = self._physical_source()
                    self.ingest_physical(tags, arms, now=now)
                except Exception:
                    pass
            self.step(dt)

    def start(self, settings: dict[str, Any] | None = None) -> None:
        with self.lock:
            virtual_play = self.virtual_play
            loadout = dict(self.loadout)
            placements = {
                socket_id: dict(placement)
                for socket_id, placement in self.placements.items()
            }
            activation_order = list(self.activation_order)
            placement_link_attempts = [
                {
                    **attempt,
                    "blocker_ids": list(attempt.get("blocker_ids", [])),
                    "blocker_socket_ids": list(
                        attempt.get("blocker_socket_ids", [])
                    ),
                    "blocker_markers": list(attempt.get("blocker_markers", [])),
                }
                for attempt in self.placement_link_attempts
            ]
            field_topology = [
                {
                    "from_socket": field["from_socket"],
                    "to_socket": field["to_socket"],
                    "link_kind": field.get("link_kind", "placement"),
                    "established_at": field.get("established_at", 0.0),
                }
                for field in self.force_fields.values()
            ]
            self.reset()
            self.virtual_play = virtual_play
            self.loadout = loadout
            self.placements = placements
            self.activation_order = activation_order
            self.placement_link_attempts = placement_link_attempts
            if settings:
                self.settings.update(settings)
            self.settings["max_active_enemies"] = min(MAX_ACTIVE_ENEMIES, int(self.settings["max_active_enemies"]))
            self.core_max_hp = self.core_hp = float(self.settings["core_hp"])
            tower_base_max_hp = self._tower_max_hp()
            tower_max_hp = (
                tower_base_max_hp * self._link_multiplier_for_count(1)
            )
            for tower in self.placements.values():
                tower["base_max_hp"] = tower_base_max_hp
                tower["max_hp"] = tower_max_hp
                tower["hp"] = tower_max_hp
                tower["linked_turret_count"] = 1
                tower["link_multiplier"] = self._link_multiplier_for_count(1)
                tower["destroyed"] = False
                tower["destroyed_at"] = None
                tower["last_fire_chain"] = []
                tower["last_damage_at"] = None
                tower["last_damage_amount"] = 0.0
                tower["facing_angle"] = float(
                    tower.get("aim_angle", tower.get("facing_angle", 0.0))
                )
            self.phase = "running"
            self.run_started_at = time.time()
            for topology in field_topology:
                self._create_force_field(
                    topology["from_socket"],
                    topology["to_socket"],
                    link_kind=topology["link_kind"],
                    check_line_of_sight=False,
                    established_at=float(topology["established_at"]),
                )
            self._reconcile_connections(reason="run_started")
            self._evaluate_ring_topology()
            self._launch_wave(1)
            self._event("run_started", wave=1)
        self._changed()

    def pause(self, paused: bool = True) -> None:
        with self.lock:
            if self.phase == "running":
                self.paused = bool(paused)
                self._event("paused" if paused else "resumed")
        self._changed()

    def set_virtual_play(self, enabled: bool) -> None:
        with self.lock:
            self.virtual_play = bool(enabled)
            self._event("input_mode", virtual_play=self.virtual_play)
        self._changed()

    def set_loadout(self, atom_tag_id: int, tower_type: str) -> None:
        atom_tag_id = int(atom_tag_id)
        expected = DEFAULT_LOADOUT.get(atom_tag_id)
        if expected is None or str(tower_type) != expected:
            raise ValueError(
                "Atom roles are fixed: 100 machine gun, 101 flamethrower, "
                "102 mortar, 103 Tesla coil"
            )
        with self.lock:
            self.loadout[atom_tag_id] = expected

    def _tower_max_hp(self) -> float:
        """Return a turret's unlinked baseline health before link scaling."""
        return max(
            1.0,
            self.core_max_hp
            * float(self.settings["defense_unit_health_percent"])
            / 100.0,
        )

    def _link_multiplier_for_count(self, linked_turret_count: int) -> float:
        count = max(1, int(linked_turret_count))
        return round(
            float(self.settings["tower_link_start_multiplier"])
            + float(self.settings["tower_link_step"]) * (count - 1),
            10,
        )

    def _tower_link_state(self) -> dict[str, dict[str, float | int]]:
        """Return connected-component size and multiplier for every turret.

        Only established, intact, unobstructed fields with two living endpoints
        join components. Provisional ring previews and broken/suspended fields
        therefore never grant combat power.
        """
        living_socket_ids = {
            str(socket_id)
            for socket_id, tower in self.placements.items()
            if not tower.get("destroyed")
        }
        adjacency = {
            socket_id: set() for socket_id in living_socket_ids
        }
        for field in self.force_fields.values():
            if not self._field_operational(field):
                continue
            first_socket = str(field["from_socket"])
            second_socket = str(field["to_socket"])
            if (
                first_socket not in adjacency
                or second_socket not in adjacency
            ):
                continue
            adjacency[first_socket].add(second_socket)
            adjacency[second_socket].add(first_socket)

        state: dict[str, dict[str, float | int]] = {}
        unvisited = set(living_socket_ids)
        while unvisited:
            root = min(unvisited)
            component: set[str] = set()
            pending = [root]
            while pending:
                socket_id = pending.pop()
                if socket_id in component:
                    continue
                component.add(socket_id)
                pending.extend(adjacency[socket_id] - component)
            unvisited.difference_update(component)
            count = len(component)
            multiplier = self._link_multiplier_for_count(count)
            for socket_id in component:
                state[socket_id] = {
                    "linked_turret_count": count,
                    "link_multiplier": multiplier,
                }

        isolated = {
            "linked_turret_count": 1,
            "link_multiplier": self._link_multiplier_for_count(1),
        }
        for socket_id in self.placements:
            state.setdefault(str(socket_id), dict(isolated))
        return state

    def _sync_tower_link_bonuses(self) -> dict[str, dict[str, float | int]]:
        """Apply current link-scaled max health without changing health percent."""
        link_state = self._tower_link_state()
        base_max_hp = self._tower_max_hp()
        for socket_id, tower in self.placements.items():
            bonus = link_state[str(socket_id)]
            new_max_hp = base_max_hp * float(bonus["link_multiplier"])
            old_max_hp = max(1.0, float(tower.get("max_hp", new_max_hp)))
            old_hp = max(0.0, float(tower.get("hp", old_max_hp)))
            tower["base_max_hp"] = base_max_hp
            tower["linked_turret_count"] = int(
                bonus["linked_turret_count"]
            )
            tower["link_multiplier"] = float(bonus["link_multiplier"])
            if not math.isclose(old_max_hp, new_max_hp, rel_tol=1e-12):
                health_ratio = max(0.0, min(1.0, old_hp / old_max_hp))
                tower["max_hp"] = new_max_hp
                tower["hp"] = 0.0 if tower.get("destroyed") else new_max_hp * health_ratio
            else:
                tower["max_hp"] = new_max_hp
                tower["hp"] = 0.0 if tower.get("destroyed") else min(old_hp, new_max_hp)
        return link_state

    def _reset_connected_fields(self, socket_id: str) -> None:
        for field in self.force_fields.values():
            if socket_id not in {field["from_socket"], field["to_socket"]}:
                continue
            field["hits"] = 0
            field["broken"] = False
            field["last_hit_at"] = None
            field["last_hit_x"] = None
            field["last_hit_y"] = None
            field["broken_at"] = None
            field["impacted_enemy_ids"].clear()

    def _new_tower(
        self,
        atom_tag_id: int,
        socket_id: str,
        kind: str,
        owner: str,
        source: str,
    ) -> dict[str, Any]:
        socket = self.level.sockets[socket_id]
        aim_angle = math.atan2(
            float(self.level.core["y"]) - socket["y"],
            float(self.level.core["x"]) - socket["x"],
        )
        base_max_hp = self._tower_max_hp()
        link_multiplier = self._link_multiplier_for_count(1)
        max_hp = base_max_hp * link_multiplier
        return {
            "placement_id": socket_id,
            "atom_tag_id": atom_tag_id,
            "owner": owner,
            "socket_id": socket_id,
            "aruco_id": socket["aruco_id"],
            "tower_type": kind,
            "x": socket["x"],
            "y": socket["y"],
            "cooldown": 0.0,
            "last_fire_at": None,
            "last_fire_target": None,
            "last_fire_chain": [],
            "last_damage_at": None,
            "last_damage_amount": 0.0,
            "source": source,
            "aim_angle": aim_angle,
            "aim_spread": 1.0 if kind == "tesla_coil" else 0.5,
            "aim_revision": 0,
            "facing_angle": aim_angle,
            "hp": max_hp,
            "max_hp": max_hp,
            "base_max_hp": base_max_hp,
            "linked_turret_count": 1,
            "link_multiplier": link_multiplier,
            "destroyed": False,
            "destroyed_at": None,
        }

    def _replenish_tower(self, tower: dict[str, Any], activation_tag: int) -> None:
        if tower.get("destroyed"):
            raise ValueError("destroyed defenses must be replaced")
        tower["cooldown"] = 0.0
        tower["last_damage_at"] = None
        tower["last_damage_amount"] = 0.0
        self._reset_connected_fields(tower["socket_id"])
        self._sync_tower_link_bonuses()
        tower["hp"] = tower["max_hp"]
        self._event(
            "tower_replenished",
            atom_tag_id=tower["atom_tag_id"],
            activation_tag=activation_tag,
            socket_id=tower["socket_id"],
        )

    def _replace_tower(
        self,
        previous: dict[str, Any],
        atom_tag_id: int,
        kind: str,
        owner: str,
        source: str,
    ) -> dict[str, Any]:
        socket_id = str(previous["socket_id"])
        replacement = self._new_tower(
            atom_tag_id, socket_id, kind, owner, source
        )
        self.placements[socket_id] = replacement
        self.loadout[atom_tag_id] = kind
        self._reset_connected_fields(socket_id)
        self._event(
            "tower_replaced",
            socket_id=socket_id,
            old_atom_tag_id=previous["atom_tag_id"],
            old_tower_type=previous["tower_type"],
            atom_tag_id=atom_tag_id,
            tower_type=kind,
            source=source,
        )
        return replacement

    def set_tower_aim(
        self,
        atom_tag_id: int,
        angle_degrees: float,
        spread: float,
        *,
        socket_id: str | None = None,
    ) -> dict[str, Any]:
        atom_tag_id = int(atom_tag_id)
        with self.lock:
            if socket_id not in (None, ""):
                tower = self.placements.get(str(socket_id))
                if tower and int(tower["atom_tag_id"]) != atom_tag_id:
                    raise ValueError("Atom tag does not match the selected defense unit")
            else:
                matches = [
                    tower for tower in self.placements.values()
                    if int(tower["atom_tag_id"]) == atom_tag_id
                ]
                if len(matches) > 1:
                    raise ValueError("socket_id is required when an Atom has multiple defense units")
                tower = matches[0] if matches else None
            if tower is None:
                raise ValueError("defense unit is not placed")
            if tower.get("destroyed"):
                raise ValueError("destroyed defense unit must be reset before aiming")
            angle = float(angle_degrees)
            reach = float(spread)
            if not math.isfinite(angle) or not math.isfinite(reach) or not 0.0 <= reach <= 1.0:
                raise ValueError("aim angle must be finite and reach must be between 0 and 1")
            tower["aim_angle"] = math.radians(angle % 360.0)
            tower["aim_spread"] = reach
            tower["aim_revision"] = int(tower.get("aim_revision", 0)) + 1
            tower["facing_angle"] = tower["aim_angle"]
            self._event("tower_aimed", atom_tag_id=atom_tag_id, socket_id=tower["socket_id"], angle=round(angle % 360.0, 2), reach=round(reach, 3))
            public = self._public_tower(tower)
        self._changed()
        return public

    def place(self, atom_tag_id: int, socket_id: str | None, tower_type: str | None = None, *, source: str, team: str | None = None) -> None:
        atom_tag_id = int(atom_tag_id)
        if atom_tag_id not in ATOM_OWNERS:
            raise ValueError("Atom tag must be 100-103")
        owner = ATOM_OWNERS[atom_tag_id]
        if team and team != owner:
            raise PermissionError(f"tag {atom_tag_id} belongs to {owner}")
        if source == "virtual" and not self.virtual_play:
            raise PermissionError("Virtual play is not enabled")
        if source not in {"virtual", "physical"}:
            raise ValueError("invalid placement source")
        with self.lock:
            if socket_id is None:
                return
            if socket_id not in self.level.sockets:
                raise ValueError("unknown socket")
            kind = DEFAULT_LOADOUT[atom_tag_id]
            if tower_type not in (None, "", kind):
                raise ValueError(
                    f"Atom {atom_tag_id} always activates {kind.replace('_', ' ')}"
                )
            existing = self.placements.get(socket_id)
            if existing is not None:
                if existing.get("destroyed"):
                    self._replace_tower(
                        existing, atom_tag_id, kind, owner, source
                    )
                else:
                    self._replenish_tower(existing, atom_tag_id)
                self._reconcile_connections(reason="tower_reactivated")
                self._evaluate_ring_topology()
                self._changed()
                return
            self.loadout[atom_tag_id] = kind
            previous_socket = next(
                (
                    candidate_socket
                    for candidate_socket in reversed(self.activation_order)
                    if candidate_socket in self.placements
                    and not self.placements[candidate_socket].get("destroyed")
                ),
                None,
            )
            self.placements[socket_id] = self._new_tower(
                atom_tag_id, socket_id, kind, owner, source
            )
            self.activation_order.append(socket_id)
            if previous_socket is not None:
                self._attempt_placement_link(
                    previous_socket, socket_id, source=source
                )
            self._evaluate_ring_topology()
            self._event("tower_placed", atom_tag_id=atom_tag_id, socket_id=socket_id, tower_type=kind, source=source)
        self._changed()

    def activate_core_tag(
        self, atom_tag_id: int, *, source: str, team: str | None = None
    ) -> None:
        atom_tag_id = int(atom_tag_id)
        if atom_tag_id not in ATOM_OWNERS:
            raise ValueError("Core activation requires an Atom tag from 100-103")
        owner = ATOM_OWNERS[atom_tag_id]
        if team and team != owner:
            raise PermissionError(f"tag {atom_tag_id} belongs to {owner}")
        if source == "virtual" and not self.virtual_play:
            raise PermissionError("Virtual play is not enabled")
        if source not in {"virtual", "physical"}:
            raise ValueError("invalid core activation source")
        with self.lock:
            if self.phase != "running":
                raise ValueError("Start the run before activating ArUco 38")
            if self.ring_completed_at is None:
                raise ValueError("Complete a force-field ring with 8-16 turrets first")
            if self.core_stage == "ring_ready":
                self.core_first_tag = atom_tag_id
                self.core_first_team = owner
                self.core_stage = "first_tag"
                self._event(
                    "core_first_tag",
                    atom_tag_id=atom_tag_id,
                    team=owner,
                    marker_id=CORE_MARKER_ID,
                    source=source,
                )
            elif self.core_stage == "first_tag":
                if owner == self.core_first_team:
                    raise ValueError("The second core tag must belong to the opposing team")
                self.core_stage = "detonating"
                self.core_detonation_started_at = self.sim_time
                self.core_purge_target_ids = set(self.enemies)
                self.core_purge_ignited_ids.clear()
                self._event(
                    "core_detonation_started",
                    first_atom_tag_id=self.core_first_tag,
                    second_atom_tag_id=atom_tag_id,
                    first_team=self.core_first_team,
                    second_team=owner,
                    enemy_count=len(self.core_purge_target_ids),
                    source=source,
                )
            else:
                raise ValueError("The ArUco 38 sequence is already complete")
        self._changed()

    def ingest_physical(self, tags: list[dict[str, Any]], arms: dict[str, dict[str, Any]], *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else float(now)
        current = {int(tag["id"]): tag for tag in tags if tag.get("id") is not None and float(tag.get("missing", 0.0)) <= 0.35}
        with self.lock:
            for marker in (CORE_MARKER_ID, *range(40, 56)):
                tag = current.get(marker)
                if tag and all(isinstance(tag.get(axis), (int, float)) for axis in ("nx", "ny")):
                    self.marker_cache[marker] = {"nx": float(tag["nx"]), "ny": float(tag["ny"]), "seen_at": now}
            for atom_tag_id, owner in ATOM_OWNERS.items():
                atom = current.get(atom_tag_id)
                arm = arms.get(owner) or {}
                arm_ready = (
                    bool(arm.get("connected")) and
                    arm.get("enabled", True) is not False and
                    str(arm.get("pump_mode") or "off") == "off"
                )
                if not atom or not arm_ready:
                    self.physical_candidates.pop(atom_tag_id, None)
                    continue
                nx, ny = float(atom.get("nx", -1)), float(atom.get("ny", -1))
                candidates = [
                    (math.hypot(nx - target["nx"], ny - target["ny"]), "socket", marker)
                    for marker, target in self.marker_cache.items()
                    if marker in self.level.socket_by_marker and now - target["seen_at"] <= 120.0
                ]
                core_target = self.marker_cache.get(CORE_MARKER_ID)
                if (
                    self.ring_completed_at is not None
                    and self.core_stage in {"ring_ready", "first_tag"}
                    and core_target
                    and now - core_target["seen_at"] <= 120.0
                ):
                    candidates.append((
                        math.hypot(nx - core_target["nx"], ny - core_target["ny"]),
                        "core",
                        CORE_MARKER_ID,
                    ))
                distance, target_kind, marker = min(
                    candidates, default=(math.inf, None, None)
                )
                if marker is None or distance > CORE_TAG_DISTANCE:
                    self.physical_candidates.pop(atom_tag_id, None)
                    continue
                candidate = self.physical_candidates.get(atom_tag_id)
                if (
                    not candidate
                    or candidate.get("marker") != marker
                    or candidate.get("target_kind") != target_kind
                ):
                    self.physical_candidates[atom_tag_id] = {
                        "marker": marker,
                        "target_kind": target_kind,
                        "since": now,
                        "activated": False,
                    }
                    continue
                if candidate.get("activated") or now - candidate["since"] < CORE_TAG_STABLE_S:
                    continue
                try:
                    if target_kind == "core":
                        self.activate_core_tag(
                            atom_tag_id, source="physical", team=owner
                        )
                    else:
                        self.place(
                            atom_tag_id,
                            self.level.socket_by_marker[marker],
                            source="physical",
                            team=owner,
                        )
                except (PermissionError, ValueError):
                    pass
                candidate["activated"] = True

    def _launch_wave(self, number: int) -> None:
        limit = min(int(self.settings["wave_count"]), len(self.wave_source))
        if number < 1 or number > limit:
            return
        config = self.wave_source[number - 1]
        groups = []
        for source_group in config.get("groups", []):
            count = max(1, int(round(
                int(source_group.get("count", 0)) *
                float(self.settings["enemy_count_multiplier"]))))
            groups.append({**source_group, "count": count, "spawned": 0})
        self.launched_waves.append({"wave": number, "started_at": self.sim_time, "groups": groups})
        self.current_wave = number
        self.next_wave_at = self.sim_time + float(self.settings["wave_interval_s"])
        self._event("wave_started", wave=number)

    def _spawn_due(self) -> None:
        active_limit = int(self.settings["max_active_enemies"])
        spawn_grid = _CollisionGrid(list(self.enemies.values()))
        while self.pressure_queue and len(self.enemies) < active_limit:
            pending = self.pressure_queue[0]
            if not self._spawn_enemy(
                pending["enemy"], pending["lane_weights"], spawn_grid
            ):
                break
            pending["count"] -= 1
            self.pressure_bank -= 1
            if pending["count"] <= 0:
                self.pressure_queue.pop(0)
        for wave in self.launched_waves:
            elapsed = self.sim_time - wave["started_at"]
            for group in wave["groups"]:
                duration = max(0.1, float(group.get("duration_s", 1.0)) / max(0.05, float(self.settings["release_rate_multiplier"])))
                count = int(group["count"])
                target = min(count, max(1 if count else 0, int(count * min(1.0, elapsed / duration))))
                while group["spawned"] < target:
                    if len(self.enemies) >= active_limit:
                        deferred = target - group["spawned"]
                        accepted = min(deferred, 2000 - self.pressure_bank)
                        if accepted > 0:
                            self.pressure_queue.append({
                                "enemy": str(group["enemy"]),
                                "lane_weights": dict(group.get("lane_weights") or {}),
                                "count": accepted,
                            })
                            self.pressure_bank += accepted
                        group["spawned"] = target
                        break
                    if self._spawn_enemy(
                        str(group["enemy"]), group.get("lane_weights") or {}, spawn_grid
                    ):
                        group["spawned"] += 1
                        continue
                    deferred = target - group["spawned"]
                    accepted = min(deferred, 2000 - self.pressure_bank)
                    if accepted > 0:
                        self.pressure_queue.append({
                            "enemy": str(group["enemy"]),
                            "lane_weights": dict(group.get("lane_weights") or {}),
                            "count": accepted,
                        })
                        self.pressure_bank += accepted
                    group["spawned"] = target
                    break
        if self.current_wave < min(int(self.settings["wave_count"]), len(self.wave_source)) and self.sim_time >= self.next_wave_at:
            self._launch_wave(self.current_wave + 1)

    def _spawn_enemy(
        self,
        enemy_type: str,
        weights: dict[str, Any],
        collision_grid: _CollisionGrid | None = None,
    ) -> bool:
        lanes = [lane for lane in self.level.paths if float(weights.get(lane, 0.0)) > 0]
        if not lanes:
            lanes = list(self.level.paths)
        stats = ENEMY_STATS.get(enemy_type, ENEMY_STATS["grunt"])
        radius = float(stats["collision_radius"])
        remaining_lanes = list(lanes)
        lane = None
        flow_offset = 0.0
        spawn_x = spawn_y = 0.0
        while remaining_lanes:
            values = [float(weights.get(candidate, 1.0)) for candidate in remaining_lanes]
            candidate = self._rng.choices(remaining_lanes, weights=values, k=1)[0]
            first_offset = self.track_cursor[candidate] % len(FLOW_SPAWN_OFFSETS)
            for offset_attempt in range(len(FLOW_SPAWN_OFFSETS)):
                candidate_offset = FLOW_SPAWN_OFFSETS[
                    (first_offset + offset_attempt) % len(FLOW_SPAWN_OFFSETS)
                ]
                spawn_x, spawn_y = _offset_polyline(
                    self.level.paths[candidate], candidate_offset
                )[0]
                occupied = (
                    collision_grid.collides(spawn_x, spawn_y, radius)
                    if collision_grid is not None
                    else any(
                        (spawn_x - other["x"]) ** 2 + (spawn_y - other["y"]) ** 2
                        < (radius + other["collision_radius"] + COLLISION_PADDING) ** 2
                        for other in self.enemies.values()
                    )
                )
                if not occupied:
                    lane = candidate
                    flow_offset = candidate_offset
                    break
            if lane is not None:
                break
            remaining_lanes.remove(candidate)
        if lane is None:
            return False
        self.track_cursor[lane] += 1
        hp = stats["hp"] * float(self.settings["enemy_health_multiplier"])
        enemy_id = self.next_enemy_id
        self.next_enemy_id += 1
        path = self._path_to_core_basin(lane, flow_offset, radius)
        initial_dx, initial_dy = path[1][0] - spawn_x, path[1][1] - spawn_y
        initial_length = math.hypot(initial_dx, initial_dy)
        speed = (
            stats["speed"] * float(self.settings["enemy_speed_multiplier"])
            * self._rng.uniform(0.94, 1.06)
        )
        basin_outer_margin = (
            CORE_BASIN_HALF_SIZE - CORE_OCTAGON_MAX_AXIS_EXTENT - radius - 1.0
        )
        basin_margin = 0.25 + (
            basin_outer_margin - 0.25
        ) * self._rng.random() ** 2.6
        self.enemies[enemy_id] = {
            "id": enemy_id,
            "enemy_type": enemy_type,
            "lane": lane,
            "track": flow_offset,
            "orbit_index": enemy_id % 4,
            "x": spawn_x,
            "y": spawn_y,
            "hp": hp,
            "max_hp": hp,
            "speed": speed,
            "core_dps": stats["core_dps"]
            * float(self.settings["enemy_core_damage_multiplier"]),
            "tower_dps": stats["core_dps"]
            * float(self.settings["enemy_tower_damage_multiplier"]),
            "collision_radius": radius,
            "facing_x": initial_dx / initial_length,
            "facing_y": initial_dy / initial_length,
            "vx": initial_dx / initial_length * speed,
            "vy": initial_dy / initial_length * speed,
            "flow_phase": self._rng.uniform(0.0, math.pi * 2.0),
            "flow_rate": self._rng.uniform(0.72, 1.28),
            "basin_margin": basin_margin,
            "basin_direction": -1.0 if enemy_id % 7 == 0 else 1.0,
            "blocked_steps": 0,
            "path": path,
            "segment": 0,
            "route_steps": [
                {"edge_id": edge_id, "reverse": False}
                for edge_id in self.level.route_edge_ids[lane]
            ],
            "current_route_step": 0,
            "current_edge_id": self.level.route_edge_ids[lane][0],
            "current_edge_progress": 0.0,
            "attacking": False,
            "progress": 0.0,
            "burn_until": 0.0,
            "burn_damage_per_s": 0.0,
            "electrocuted_until": 0.0,
            "electrocution_depth": 0,
            "electrocution_intensity": 0.0,
        }
        if collision_grid is not None:
            collision_grid.add(enemy_id, spawn_x, spawn_y, radius)
        return True

    def _path_to_core_basin(
        self, lane: str, flow_offset: float, radius: float
    ) -> list[tuple[float, float]]:
        core_x = float(self.level.core["x"])
        arrival_y = float(self.level.paths[lane][-2][1])
        road_centerline = [tuple(point) for point in self.level.paths[lane][:-2]]
        # Carry each particle's lateral offset through the visible road/plaza
        # seam. The endpoint sits just inside the basin boundary, so admission
        # changes behavior without changing screen position.
        entry_x = core_x - CORE_BASIN_HALF_SIZE + radius + 0.5
        entry_y = arrival_y + flow_offset
        road_centerline.append((entry_x, entry_y))
        return _rounded_polyline(road_centerline)

    def _sync_enemy_road_edge(
        self, enemy: dict[str, Any], *, search_all: bool = False
    ) -> tuple[dict[str, Any], tuple[float, float, int, float, float, float]] | None:
        """Update constant-size edge/progress metadata for one road particle."""
        steps = list(enemy.get("route_steps") or [])
        if steps:
            current = max(
                0, min(int(enemy.get("current_route_step", 0)), len(steps) - 1)
            )
            if search_all:
                indexes = range(len(steps))
            else:
                indexes = range(max(0, current - 1), min(len(steps), current + 3))
            candidates = [
                (index, str(steps[index]["edge_id"])) for index in indexes
            ]
        else:
            current = 0
            candidates = [
                (index, edge_id)
                for index, edge_id in enumerate(self.level.edge_by_id)
            ]

        best: tuple[
            float, int, int, str,
            tuple[float, float, int, float, float, float],
        ] | None = None
        for index, edge_id in candidates:
            projection = self.level.project_onto_edge(
                edge_id, float(enemy["x"]), float(enemy["y"])
            )
            candidate = (
                projection[5], abs(index - current), -index, edge_id, projection
            )
            if best is None or candidate[:3] < best[:3]:
                best = candidate
        if best is None:
            return None

        _, _, negative_index, edge_id, projection = best
        index = -negative_index
        enemy["current_route_step"] = index
        enemy["current_edge_id"] = edge_id
        enemy["current_edge_progress"] = projection[4]
        return self.level.edge_by_id[edge_id], projection

    @staticmethod
    def _nearest_road_point(
        enemy: dict[str, Any], x: float, y: float
    ) -> tuple[float, float, int, float, float]:
        path = enemy["path"]
        segment = int(enemy["segment"])
        best = (path[segment][0], path[segment][1], segment, 0.0, math.inf)
        for index in range(max(0, segment - 3), min(len(path) - 1, segment + 8)):
            point_x, point_y, ratio = _closest_point_on_segment(
                x, y, path[index][0], path[index][1],
                path[index + 1][0], path[index + 1][1],
            )
            distance = math.hypot(x - point_x, y - point_y)
            if distance < best[4]:
                best = point_x, point_y, index, ratio, distance
        return best

    def _constrain_road_particle(self, enemy: dict[str, Any]) -> None:
        point_x, point_y, index, ratio, distance = self._nearest_road_point(
            enemy, enemy["x"], enemy["y"]
        )
        if index > enemy["segment"] and ratio > 0.2:
            enemy["segment"] = index
        if distance <= FLOW_CORRIDOR_RADIUS or distance <= 1e-9:
            return
        normal_x = (enemy["x"] - point_x) / distance
        normal_y = (enemy["y"] - point_y) / distance
        enemy["x"] = point_x + normal_x * FLOW_CORRIDOR_RADIUS
        enemy["y"] = point_y + normal_y * FLOW_CORRIDOR_RADIUS
        outward_speed = enemy["vx"] * normal_x + enemy["vy"] * normal_y
        if outward_speed > 0.0:
            enemy["vx"] -= normal_x * outward_speed * 1.35
            enemy["vy"] -= normal_y * outward_speed * 1.35

    def _constrain_basin_particle(self, enemy: dict[str, Any]) -> None:
        center_x, center_y = float(self.level.core["x"]), float(self.level.core["y"])
        radius = enemy["collision_radius"]
        outer = CORE_BASIN_HALF_SIZE - radius
        enemy["x"] = max(center_x - outer, min(center_x + outer, enemy["x"]))
        enemy["y"] = max(center_y - outer, min(center_y + outer, enemy["y"]))

        dx, dy = enemy["x"] - center_x, enemy["y"] - center_y
        _, normal_x, normal_y, clearance = _core_octagon_face(dx, dy, radius)
        if clearance >= 0.0:
            return
        enemy["x"] -= normal_x * clearance
        enemy["y"] -= normal_y * clearance
        inward_speed = enemy["vx"] * normal_x + enemy["vy"] * normal_y
        if inward_speed < 0.0:
            enemy["vx"] -= normal_x * inward_speed * 1.2
            enemy["vy"] -= normal_y * inward_speed * 1.2

    def _constrain_particle(self, enemy: dict[str, Any]) -> None:
        if enemy["attacking"]:
            self._constrain_basin_particle(enemy)
        else:
            self._constrain_road_particle(enemy)

    def _update_road_progress(self, enemy: dict[str, Any]) -> None:
        path = enemy["path"]
        while enemy["segment"] < len(path) - 1:
            start_x, start_y = path[enemy["segment"]]
            target_x, target_y = path[enemy["segment"] + 1]
            segment_x, segment_y = target_x - start_x, target_y - start_y
            length_sq = segment_x * segment_x + segment_y * segment_y
            target_distance = math.hypot(enemy["x"] - target_x, enemy["y"] - target_y)
            projection = (
                ((enemy["x"] - start_x) * segment_x + (enemy["y"] - start_y) * segment_y)
                / length_sq if length_sq > 1e-9 else 1.0
            )
            final_segment = enemy["segment"] == len(path) - 2
            if final_segment:
                reached = target_distance <= CORE_ENTRY_DISTANCE
            else:
                reached = target_distance <= PATH_WAYPOINT_RADIUS or (
                    projection >= 0.96
                    and target_distance <= FLOW_CORRIDOR_RADIUS * 1.25
                )
            if reached:
                enemy["segment"] += 1
                continue
            break
        enemy["progress"] = enemy["segment"] / max(1, len(path) - 1)

    def _road_particle_velocity(
        self, enemy: dict[str, Any], grid: _CollisionGrid, dt: float, slow: float
    ) -> None:
        self._update_road_progress(enemy)
        path = enemy["path"]
        if enemy["segment"] >= len(path) - 1:
            enemy["vx"] *= 0.7
            enemy["vy"] *= 0.7
            return
        target_x, target_y = path[enemy["segment"] + 1]
        dx, dy = target_x - enemy["x"], target_y - enemy["y"]
        distance = max(1e-9, math.hypot(dx, dy))
        guide_x, guide_y = dx / distance, dy / distance
        point_x, point_y, _, _, center_distance = self._nearest_road_point(
            enemy, enemy["x"], enemy["y"]
        )
        recovery_x = (point_x - enemy["x"]) / max(FLOW_CORRIDOR_RADIUS, center_distance, 1.0)
        recovery_y = (point_y - enemy["y"]) / max(FLOW_CORRIDOR_RADIUS, center_distance, 1.0)
        pressure_x, pressure_y = grid.pressure(
            enemy["x"], enemy["y"], enemy["collision_radius"], enemy["id"]
        )
        wander = math.sin(
            self.sim_time * enemy["flow_rate"] * 1.7 + enemy["flow_phase"]
        )
        desired_x = (
            guide_x - guide_y * wander * PARTICLE_WANDER_WEIGHT
            + recovery_x * 0.75 + pressure_x * PARTICLE_PRESSURE_WEIGHT
        )
        desired_y = (
            guide_y + guide_x * wander * PARTICLE_WANDER_WEIGHT
            + recovery_y * 0.75 + pressure_y * PARTICLE_PRESSURE_WEIGHT
        )
        if desired_x * guide_x + desired_y * guide_y < 0.25:
            desired_x += guide_x
            desired_y += guide_y
        length = max(1e-9, math.hypot(desired_x, desired_y))
        target_speed = enemy["speed"] * slow
        desired_x, desired_y = desired_x / length * target_speed, desired_y / length * target_speed
        response = 1.0 - math.exp(-PARTICLE_STEERING_RESPONSE * dt)
        enemy["vx"] += (desired_x - enemy["vx"]) * response
        enemy["vy"] += (desired_y - enemy["vy"]) * response

    def _basin_particle_velocity(
        self, enemy: dict[str, Any], grid: _CollisionGrid, dt: float
    ) -> None:
        center_x, center_y = float(self.level.core["x"]), float(self.level.core["y"])
        dx, dy = enemy["x"] - center_x, enemy["y"] - center_y
        _, normal_x, normal_y, clearance = _core_octagon_face(
            dx, dy, enemy["collision_radius"]
        )
        tangent_x = -normal_y * enemy["basin_direction"]
        tangent_y = normal_x * enemy["basin_direction"]
        radial = max(
            -1.1, min(1.1, (enemy["basin_margin"] - clearance) / 8.0)
        )
        noise_x = math.sin(self.sim_time * enemy["flow_rate"] * 1.9 + enemy["flow_phase"])
        noise_y = math.cos(self.sim_time * enemy["flow_rate"] * 1.37 - enemy["flow_phase"] * 1.61)
        pressure_x, pressure_y = grid.pressure(
            enemy["x"], enemy["y"], enemy["collision_radius"], enemy["id"]
        )
        desired_x = (
            tangent_x + normal_x * radial * 0.9 + noise_x * 0.42
            + pressure_x * PARTICLE_PRESSURE_WEIGHT
        )
        desired_y = (
            tangent_y + normal_y * radial * 0.9 + noise_y * 0.42
            + pressure_y * PARTICLE_PRESSURE_WEIGHT
        )
        length = max(1e-9, math.hypot(desired_x, desired_y))
        speed_variation = 0.86 + (enemy["id"] * 0.61803398875 % 1.0) * 0.28
        target_speed = CORE_BASIN_SPEED * speed_variation
        desired_x, desired_y = desired_x / length * target_speed, desired_y / length * target_speed
        response = 1.0 - math.exp(-14.0 * dt)
        enemy["vx"] += (desired_x - enemy["vx"]) * response
        enemy["vy"] += (desired_y - enemy["vy"]) * response

    def _admit_ready_particles(self, living: list[dict[str, Any]]) -> None:
        for enemy in living:
            if enemy["attacking"]:
                continue
            self._update_road_progress(enemy)
            path = enemy["path"]
            if enemy["segment"] < len(path) - 1:
                continue
            enemy["attacking"] = True
            enemy["progress"] = 1.0
            enemy["blocked_steps"] = 0
            self.breaches += 1

    def _resolve_particle_contacts(self, living: list[dict[str, Any]]) -> None:
        by_id = {enemy["id"]: enemy for enemy in living}
        population = len(living)
        solver_iterations = (
            2 if population >= 800 else
            4 if population >= 600 else
            8 if population >= 450 else
            PARTICLE_SOLVER_ITERATIONS
        )
        for _ in range(solver_iterations):
            cells: dict[tuple[int, int], list[int]] = defaultdict(list)
            for enemy in living:
                cell = (
                    math.floor(enemy["x"] / COLLISION_CELL_SIZE),
                    math.floor(enemy["y"] / COLLISION_CELL_SIZE),
                )
                cells[cell].append(enemy["id"])
            contacts = 0
            for enemy in living:
                cell_x = math.floor(enemy["x"] / COLLISION_CELL_SIZE)
                cell_y = math.floor(enemy["y"] / COLLISION_CELL_SIZE)
                for offset_y in (-1, 0, 1):
                    for offset_x in (-1, 0, 1):
                        for other_id in cells.get((cell_x + offset_x, cell_y + offset_y), ()):
                            if other_id <= enemy["id"]:
                                continue
                            other = by_id[other_id]
                            dx, dy = other["x"] - enemy["x"], other["y"] - enemy["y"]
                            minimum = (
                                enemy["collision_radius"] + other["collision_radius"]
                                + COLLISION_PADDING
                            )
                            distance_sq = dx * dx + dy * dy
                            if distance_sq >= (minimum - 1e-6) ** 2:
                                continue
                            if distance_sq <= 1e-18:
                                angle = (enemy["id"] * 2.399963229728653 + other_id) % (math.pi * 2.0)
                                normal_x, normal_y = math.cos(angle), math.sin(angle)
                                distance = 0.0
                            else:
                                distance = math.sqrt(distance_sq)
                                normal_x, normal_y = dx / distance, dy / distance
                            # A small deterministic oblique component prevents
                            # wall contacts from crystallizing into one-dimensional
                            # rows; compression then spills into the open basin.
                            jitter = math.sin(
                                enemy["id"] * 12.9898 + other_id * 78.233
                            ) * 0.24
                            cosine, sine = math.cos(jitter), math.sin(jitter)
                            normal_x, normal_y = (
                                normal_x * cosine - normal_y * sine,
                                normal_x * sine + normal_y * cosine,
                            )
                            correction = (minimum - distance) * 0.74
                            enemy["x"] -= normal_x * correction
                            enemy["y"] -= normal_y * correction
                            other["x"] += normal_x * correction
                            other["y"] += normal_y * correction
                            self._constrain_particle(enemy)
                            self._constrain_particle(other)
                            contacts += 1
            if contacts == 0:
                break

    def _integrate_particles(
        self, living: list[dict[str, Any]], dt: float, slow_by_enemy: dict[int, float]
    ) -> None:
        grid = _CollisionGrid(living)
        origins = {enemy["id"]: (enemy["x"], enemy["y"]) for enemy in living}
        for enemy in living:
            if enemy["attacking"]:
                self._basin_particle_velocity(enemy, grid, dt)
                speed_cap = CORE_BASIN_SPEED * PARTICLE_MAX_SPEED_SCALE
            else:
                self._road_particle_velocity(
                    enemy, grid, dt, slow_by_enemy.get(enemy["id"], 1.0)
                )
                speed_cap = enemy["speed"] * PARTICLE_MAX_SPEED_SCALE
            velocity = math.hypot(enemy["vx"], enemy["vy"])
            if velocity > speed_cap:
                enemy["vx"] *= speed_cap / velocity
                enemy["vy"] *= speed_cap / velocity
            enemy["x"] += enemy["vx"] * dt
            enemy["y"] += enemy["vy"] * dt
            self._constrain_particle(enemy)

        self._resolve_particle_contacts(living)
        for enemy in living:
            origin_x, origin_y = origins[enemy["id"]]
            actual_x = (enemy["x"] - origin_x) / dt
            actual_y = (enemy["y"] - origin_y) / dt
            limit = (
                CORE_BASIN_SPEED if enemy["attacking"] else enemy["speed"]
            ) * PARTICLE_MAX_SPEED_SCALE
            actual_speed = math.hypot(actual_x, actual_y)
            if actual_speed > limit:
                actual_x *= limit / actual_speed
                actual_y *= limit / actual_speed
            enemy["vx"] = enemy["vx"] * 0.35 + actual_x * 0.65
            enemy["vy"] = enemy["vy"] * 0.35 + actual_y * 0.65
            movement = math.hypot(enemy["x"] - origin_x, enemy["y"] - origin_y)
            if movement > 0.08:
                enemy["facing_x"] = (enemy["x"] - origin_x) / movement
                enemy["facing_y"] = (enemy["y"] - origin_y) / movement
                enemy["blocked_steps"] = 0
            else:
                enemy["blocked_steps"] += 1
            if not enemy["attacking"]:
                self._update_road_progress(enemy)

    def _tower_targeting(self, tower: dict[str, Any]) -> dict[str, float]:
        spread = max(0.0, min(1.0, float(tower.get("aim_spread", 0.5))))
        angle = float(tower.get("aim_angle", 0.0))
        kind = tower["tower_type"]
        stats = TOWER_STATS[kind]
        if kind == "tesla_coil":
            maximum_range = float(stats["range"])
            minimum_range = float(stats["min_range"])
            selected_range = minimum_range + (maximum_range - minimum_range) * spread
            damage_multiplier = TESLA_CLOSE_DAMAGE_MULTIPLIER - (
                TESLA_CLOSE_DAMAGE_MULTIPLIER - 1.0
            ) * spread
            visual_intensity = 1.0 - (
                1.0 - TESLA_MAX_RANGE_VISUAL_INTENSITY
            ) * spread
            return {
                "angle": angle,
                "angle_degrees": math.degrees(angle) % 360.0,
                "spread": spread,
                "range": selected_range,
                "min_range": minimum_range,
                "max_range": maximum_range,
                "damage_multiplier": damage_multiplier,
                "visual_intensity": visual_intensity,
                "half_angle": 180.0,
            }
        if kind == "mortar":
            distance = stats["min_range"] + (stats["max_range"] - stats["min_range"]) * spread
            radius = stats["near_splash"] + (stats["far_splash"] - stats["near_splash"]) * spread
            multiplier = 1.0 + (float(self.settings["mortar_far_damage_multiplier"]) - 1.0) * spread
            return {
                "angle": angle,
                "angle_degrees": math.degrees(angle) % 360.0,
                "spread": spread,
                "target_x": tower["x"] + math.cos(angle) * distance,
                "target_y": tower["y"] + math.sin(angle) * distance,
                "range": distance,
                "blast_radius": radius,
                "damage_multiplier": multiplier,
            }
        maximum_range = stats["far_range"] + (stats["near_range"] - stats["far_range"]) * spread
        half_angle = stats["narrow_half_angle"] + (stats["wide_half_angle"] - stats["narrow_half_angle"]) * spread
        targeting = {
            "angle": angle,
            "angle_degrees": math.degrees(angle) % 360.0,
            "spread": spread,
            "range": maximum_range,
            "half_angle": half_angle,
        }
        if kind == "flamethrower":
            targeting["sweep_angle"] = self._flamethrower_sweep_angle(
                tower, angle, half_angle
            )
            targeting["sweep_angle_degrees"] = (
                math.degrees(targeting["sweep_angle"]) % 360.0
            )
            targeting["hit_radius"] = FLAMETHROWER_HIT_RADIUS
        return targeting

    def _flamethrower_sweep_angle(
        self, tower: dict[str, Any], center_angle: float, half_angle: float
    ) -> float:
        return self._flamethrower_sweep_angle_at(
            tower, center_angle, half_angle, self.sim_time
        )

    @staticmethod
    def _flamethrower_sweep_angle_at(
        tower: dict[str, Any], center_angle: float, half_angle: float, at: float
    ) -> float:
        phase_offset = (int(tower.get("aruco_id", 0)) % 7) / 7.0
        phase = (at / FLAMETHROWER_SWEEP_PERIOD_S + phase_offset) % 1.0
        oscillation = 4.0 * abs(phase - 0.5) - 1.0
        return center_angle + math.radians(half_angle) * oscillation

    def _flamethrower_path(
        self, tower: dict[str, Any], targeting: dict[str, float]
    ) -> list[tuple[float, float]]:
        current_angle = float(targeting["sweep_angle"])
        reach = max(
            1.0,
            float(targeting["range"]) - FLAMETHROWER_MUZZLE_OFFSET,
        )
        step = reach / FLAMETHROWER_PATH_SEGMENTS
        points = [(
            float(tower["x"]) + math.cos(current_angle) * FLAMETHROWER_MUZZLE_OFFSET,
            float(tower["y"]) + math.sin(current_angle) * FLAMETHROWER_MUZZLE_OFFSET,
        )]
        for index in range(FLAMETHROWER_PATH_SEGMENTS):
            progress = (index + 1) / FLAMETHROWER_PATH_SEGMENTS
            delayed_angle = self._flamethrower_sweep_angle_at(
                tower,
                float(targeting["angle"]),
                float(targeting["half_angle"]),
                self.sim_time - progress * FLAMETHROWER_TRAIL_LAG_S,
            )
            flutter = math.sin(self.sim_time * 18.0 - index * 0.72) * 0.065 * progress
            previous_x, previous_y = points[-1]
            points.append((
                previous_x + math.cos(delayed_angle + flutter) * step,
                previous_y + math.sin(delayed_angle + flutter) * step,
            ))
        return points

    @staticmethod
    def _distance_to_path(
        x: float, y: float, points: list[tuple[float, float]]
    ) -> float:
        return min(
            _distance_point_to_segment(x, y, *start, *end)
            for start, end in zip(points, points[1:])
        )

    @staticmethod
    def _tower_health_stage(tower: dict[str, Any]) -> str:
        if tower.get("destroyed") or float(tower.get("hp", 0.0)) <= 0.0:
            return "destroyed"
        maximum = max(1.0, float(tower.get("max_hp", 1.0)))
        ratio = float(tower.get("hp", 0.0)) / maximum
        if ratio < 0.10:
            return "burning"
        if ratio < 0.30:
            return "smoking"
        if ratio < 0.50:
            return "stressed"
        return "normal"

    def _line_obstructions(
        self, first: dict[str, Any], second: dict[str, Any]
    ) -> dict[str, list[Any]]:
        endpoint_socket_ids = {
            str(first["socket_id"]), str(second["socket_id"])
        }
        active_socket_ids = self._active_living_socket_ids()
        blocker_socket_ids = sorted(
            (
                socket_id
                for socket_id, socket in self.level.sockets.items()
                if socket_id not in endpoint_socket_ids
                and socket_id not in active_socket_ids
                and _segment_intersects_square(
                    float(first["x"]),
                    float(first["y"]),
                    float(second["x"]),
                    float(second["y"]),
                    float(socket["x"]),
                    float(socket["y"]),
                    self.level.aruco_code_footprint_px,
                )
            ),
            key=lambda socket_id: int(
                self.level.sockets[socket_id]["aruco_id"]
            ),
        )
        authored_blocker_ids = [
            str(blocker["blocker_id"])
            for blocker in self.level.force_field_blockers
            if _segment_intersects_polygon(
                float(first["x"]),
                float(first["y"]),
                float(second["x"]),
                float(second["y"]),
                blocker["points"],
            )
        ]
        core_is_blocked = _segment_intersects_square(
            float(first["x"]),
            float(first["y"]),
            float(second["x"]),
            float(second["y"]),
            float(self.level.core["x"]),
            float(self.level.core["y"]),
            self.level.core_aruco_code_footprint_px
            + self.level.force_field_marker_clearance_px * 2.0,
        )
        return {
            "blocker_ids": sorted([
                *authored_blocker_ids,
                *([f"protected_marker:{CORE_MARKER_ID}"] if core_is_blocked else []),
                *(f"empty_socket:{socket_id}" for socket_id in blocker_socket_ids),
            ]),
            "blocker_socket_ids": blocker_socket_ids,
            "blocker_markers": [
                *([CORE_MARKER_ID] if core_is_blocked else []),
                *(
                    int(self.level.sockets[socket_id]["aruco_id"])
                    for socket_id in blocker_socket_ids
                ),
            ],
        }

    def _line_blockers(
        self, first: dict[str, Any], second: dict[str, Any]
    ) -> list[str]:
        return list(self._line_obstructions(first, second)["blocker_ids"])

    def _line_is_clear(self, first: dict[str, Any], second: dict[str, Any]) -> bool:
        return not self._line_blockers(first, second)

    def _field_obstructions(self, field: dict[str, Any]) -> dict[str, list[Any]]:
        first_socket = str(field["from_socket"])
        second_socket = str(field["to_socket"])
        first = self.placements.get(first_socket) or self.level.sockets[first_socket]
        second = self.placements.get(second_socket) or self.level.sockets[second_socket]
        return self._line_obstructions(first, second)

    def _refresh_field_obstructions(self, *, reason: str) -> None:
        retired_fields = []
        for field in list(self.force_fields.values()):
            previous = tuple(field.get("occluded_by_blocker_ids", []))
            obstruction = self._field_obstructions(field)
            if CORE_MARKER_ID in obstruction["blocker_markers"]:
                retired_fields.append((field, obstruction))
                continue
            current = tuple(obstruction["blocker_ids"])
            field["occluded_by_blocker_ids"] = list(obstruction["blocker_ids"])
            field["occluded_by_socket_ids"] = list(
                obstruction["blocker_socket_ids"]
            )
            field["occluded_by_markers"] = list(obstruction["blocker_markers"])
            if current and current != previous:
                self._event(
                    "force_field_occluded",
                    field_id=field["field_id"],
                    blocker_ids=list(current),
                    blocker_socket_ids=list(obstruction["blocker_socket_ids"]),
                    blocker_markers=list(obstruction["blocker_markers"]),
                    reason=reason,
                )
            elif previous and not current:
                self._event(
                    "force_field_resumed",
                    field_id=field["field_id"],
                    reason=reason,
                )
        retired_ring_boundary = False
        for field, obstruction in retired_fields:
            self.force_fields.pop(field["field_id"], None)
            retired_ring_boundary = retired_ring_boundary or bool(
                field.get("ring_boundary")
            )
            self._event(
                "force_field_rejected",
                field_id=field["field_id"],
                blocker_ids=list(obstruction["blocker_ids"]),
                blocker_markers=list(obstruction["blocker_markers"]),
                reason=reason,
            )
        if retired_ring_boundary:
            self.ring_completed_at = None
            self.ring_socket_ids = []
            self.ring_candidate_socket_ids = []
            self.ring_candidate_source = None
            self.ring_closing_pair = []
            self.ring_last_evaluation = None
            self.ring_alternative_evaluations = []
            self.ring_rejected_evaluations = []
            self.ring_search_evaluated_count = 0
            self.field_immunity_until = 0.0
            if self.core_stage == "ring_ready":
                self.core_stage = "locked"
            self._event("ring_topology_invalidated", reason=reason)

    @staticmethod
    def _canonical_field_pair(
        first_socket: str, second_socket: str
    ) -> tuple[str, str]:
        first, second = sorted((str(first_socket), str(second_socket)))
        return first, second

    @classmethod
    def _canonical_field_id(cls, first_socket: str, second_socket: str) -> str:
        first, second = cls._canonical_field_pair(first_socket, second_socket)
        return f"{first}:{second}"

    def _create_force_field(
        self,
        first_socket: str,
        second_socket: str,
        *,
        link_kind: str = "placement",
        check_line_of_sight: bool = True,
        established_at: float | None = None,
    ) -> dict[str, Any] | None:
        """Establish one immutable endpoint pair; existing pairs are untouched."""
        first_socket, second_socket = self._canonical_field_pair(
            first_socket, second_socket
        )
        field_id = self._canonical_field_id(first_socket, second_socket)
        if field_id in self.force_fields:
            return self.force_fields[field_id]
        first = self.placements.get(first_socket)
        second = self.placements.get(second_socket)
        if first is None or second is None:
            return None
        if check_line_of_sight and not self._line_is_clear(first, second):
            return None
        blocked_edges = self.level.edges_crossed_by_segment(
            first["x"], first["y"], second["x"], second["y"]
        )
        field = {
            "field_id": field_id,
            "from_socket": first_socket,
            "to_socket": second_socket,
            "from_tag": first["atom_tag_id"],
            "to_tag": second["atom_tag_id"],
            "ax": first["x"],
            "ay": first["y"],
            "bx": second["x"],
            "by": second["y"],
            "blocked_edges": sorted(blocked_edges),
            "link_kind": link_kind,
            "ring_boundary": False,
            "established_at": self.sim_time if established_at is None else established_at,
            "hits": 0,
            "capacity": int(self.settings["force_field_hit_capacity"]),
            "broken": False,
            "last_hit_at": None,
            "last_hit_x": None,
            "last_hit_y": None,
            "broken_at": None,
            "impacted_enemy_ids": set(),
            "occluded_by_blocker_ids": [],
            "occluded_by_socket_ids": [],
            "occluded_by_markers": [],
        }
        self.force_fields[field_id] = field
        self._event(
            "force_field_established",
            field_id=field_id,
            from_socket=first_socket,
            to_socket=second_socket,
            link_kind=link_kind,
        )
        return field

    def _attempt_placement_link(
        self, first_socket: str, second_socket: str, *, source: str
    ) -> dict[str, Any]:
        """Queue a predecessor link and converge all pending links."""
        field_id = self._canonical_field_id(first_socket, second_socket)
        existing = next(
            (
                attempt for attempt in self.placement_link_attempts
                if str(attempt["field_id"]) == field_id
            ),
            None,
        )
        if existing is not None:
            self._reconcile_connections(reason="placement_reobserved")
            return existing
        attempt = {
            "attempt_id": f"placement_link_{len(self.placement_link_attempts) + 1:02d}",
            "from_socket": first_socket,
            "to_socket": second_socket,
            "field_id": field_id,
            "status": "pending",
            "blocker_ids": [],
            "blocker_socket_ids": [],
            "blocker_markers": [],
            "attempted_at": round(self.sim_time, 3),
            "source": source,
        }
        self.placement_link_attempts.append(attempt)
        self._reconcile_connections(reason="tower_placed")
        return attempt

    def _reconcile_connections(self, *, reason: str) -> None:
        """Idempotently converge every requested placement connection.

        A request is retained when an endpoint is unavailable, authored map
        geometry blocks it, or an empty ArUco socket lies between its endpoints.
        Re-running this method after lifecycle or layout changes therefore
        repairs missed links without duplicating fields or changing the
        durability of an already-established field.
        """
        self._refresh_field_obstructions(reason=reason)
        for attempt in self.placement_link_attempts:
            previous_status = str(attempt.get("status") or "pending")
            first_socket = str(attempt["from_socket"])
            second_socket = str(attempt["to_socket"])
            field_id = self._canonical_field_id(first_socket, second_socket)
            attempt["field_id"] = field_id
            attempt["last_reconciled_at"] = round(self.sim_time, 3)
            attempt["reconcile_reason"] = reason
            field = self.force_fields.get(field_id)
            if field is not None:
                attempt["status"] = "established"
                attempt["blocker_ids"] = list(
                    field.get("occluded_by_blocker_ids", [])
                )
                attempt["blocker_socket_ids"] = list(
                    field.get("occluded_by_socket_ids", [])
                )
                attempt["blocker_markers"] = list(
                    field.get("occluded_by_markers", [])
                )
                continue
            first = self.placements.get(first_socket)
            second = self.placements.get(second_socket)
            if (
                first is None
                or second is None
                or first.get("destroyed")
                or second.get("destroyed")
            ):
                attempt["status"] = "pending_endpoint"
                attempt["blocker_ids"] = []
                attempt["blocker_socket_ids"] = []
                attempt["blocker_markers"] = []
                continue
            obstruction = self._line_obstructions(first, second)
            blocker_ids = list(obstruction["blocker_ids"])
            if blocker_ids:
                attempt["status"] = (
                    "pending_core_marker"
                    if CORE_MARKER_ID in obstruction["blocker_markers"]
                    else "pending_empty_socket"
                    if obstruction["blocker_socket_ids"]
                    else "pending_blocked"
                )
                attempt["blocker_ids"] = blocker_ids
                attempt["blocker_socket_ids"] = list(
                    obstruction["blocker_socket_ids"]
                )
                attempt["blocker_markers"] = list(
                    obstruction["blocker_markers"]
                )
                if previous_status != attempt["status"]:
                    self._event(
                        "placement_link_blocked",
                        attempt_id=attempt["attempt_id"],
                        from_socket=first_socket,
                        to_socket=second_socket,
                        blocker_ids=list(blocker_ids),
                        blocker_socket_ids=list(
                            obstruction["blocker_socket_ids"]
                        ),
                        blocker_markers=list(obstruction["blocker_markers"]),
                    )
                continue
            field = self._create_force_field(
                first_socket,
                second_socket,
                link_kind="placement",
                check_line_of_sight=False,
            )
            if field is None:
                raise RuntimeError(
                    "connection reconciliation passed preflight but creation failed"
                )
            attempt["status"] = "established"
            attempt["blocker_ids"] = []
            attempt["blocker_socket_ids"] = []
            attempt["blocker_markers"] = []
            if previous_status in {
                "pending_blocked", "pending_core_marker",
                "pending_empty_socket", "blocked"
            }:
                self._event(
                    "placement_link_reconciled",
                    attempt_id=attempt["attempt_id"],
                    field_id=field_id,
                    reason=reason,
                )
        self._sync_tower_link_bonuses()

    def _field_endpoints_alive(self, field: dict[str, Any]) -> bool:
        return all(
            socket_id in self.placements
            and not self.placements[socket_id].get("destroyed")
            for socket_id in (field["from_socket"], field["to_socket"])
        )

    @staticmethod
    def _field_is_occluded(field: dict[str, Any]) -> bool:
        return bool(field.get("occluded_by_blocker_ids"))

    def _field_operational(self, field: dict[str, Any]) -> bool:
        return bool(
            not field["broken"]
            and self._field_endpoints_alive(field)
            and not self._field_is_occluded(field)
        )

    def _field_between(
        self, first_socket: str, second_socket: str
    ) -> dict[str, Any] | None:
        return self.force_fields.get(
            self._canonical_field_id(first_socket, second_socket)
        )

    @staticmethod
    def _ring_pairs(order: list[str]) -> list[tuple[str, str]]:
        return [*zip(order, order[1:]), (order[-1], order[0])]

    def _active_living_socket_ids(self) -> set[str]:
        return {
            socket_id for socket_id, tower in self.placements.items()
            if not tower.get("destroyed")
        }

    def _evaluate_ring_topology(self) -> None:
        """Atomically close the best valid ring in the live turret geometry."""
        self._try_establish_ring()
        self._sync_tower_link_bonuses()

    def _analyze_ring_candidate(
        self,
        order: list[str],
        *,
        source: str,
        edge_obstructions: (
            dict[tuple[str, str], dict[str, list[Any]]] | None
        ) = None,
    ) -> dict[str, Any]:
        active_socket_ids = self._active_living_socket_ids()
        missing_socket_ids = [
            socket_id for socket_id in order
            if socket_id not in active_socket_ids
        ]
        points = [
            (
                float(self.level.sockets[socket_id]["x"]),
                float(self.level.sockets[socket_id]["y"]),
            )
            for socket_id in order
        ]
        simple_polygon = _is_simple_polygon(points)
        contains_core = (
            simple_polygon
            and _point_in_polygon(
                float(self.level.core["x"]),
                float(self.level.core["y"]),
                points,
            )
        )
        valid_geometry = (
            RING_MIN_TURRETS <= len(points) <= RING_MAX_TURRETS
            and simple_polygon
            and contains_core
        )
        pairs = self._ring_pairs(order)
        missing_pairs = [
            pair for pair in pairs if self._field_between(*pair) is None
        ]
        blocked_edge_details = []
        if not missing_socket_ids:
            for first, second in pairs:
                obstruction = (
                    edge_obstructions.get(
                        self._canonical_field_pair(first, second),
                        {"blocker_ids": [], "blocker_socket_ids": [], "blocker_markers": []},
                    )
                    if edge_obstructions is not None
                    else self._line_obstructions(
                        self.placements[first], self.placements[second]
                    )
                )
                blocker_ids = list(obstruction["blocker_ids"])
                if blocker_ids:
                    blocked_edge_details.append({
                        "sockets": [first, second],
                        "markers": sorted([
                            self.level.sockets[first]["aruco_id"],
                            self.level.sockets[second]["aruco_id"],
                        ]),
                        "blocker_ids": blocker_ids,
                        "blocker_socket_ids": list(
                            obstruction["blocker_socket_ids"]
                        ),
                        "blocker_markers": list(obstruction["blocker_markers"]),
                    })
        rejection_reasons = []
        if missing_socket_ids:
            rejection_reasons.append("inactive_or_destroyed_turret")
        if not simple_polygon:
            rejection_reasons.append("self_intersection")
        elif not contains_core:
            rejection_reasons.append("core_outside")
        if blocked_edge_details:
            rejection_reasons.append("blocked_line_of_sight")
        boundary_lengths = [
            math.hypot(
                self.level.sockets[second]["x"]
                - self.level.sockets[first]["x"],
                self.level.sockets[second]["y"]
                - self.level.sockets[first]["y"],
            )
            for first, second in pairs
        ]
        perimeter = sum(boundary_lengths)
        return {
            "source": source,
            "order": list(order),
            "pairs": pairs,
            "missing_pairs": missing_pairs,
            "missing_socket_ids": missing_socket_ids,
            "blocked_edges": blocked_edge_details,
            "simple_polygon": simple_polygon,
            "contains_core": contains_core,
            "valid_geometry": valid_geometry,
            "closable": not rejection_reasons and valid_geometry,
            "rejection_reasons": rejection_reasons,
            "perimeter": perimeter,
            "max_edge_length": max(boundary_lengths, default=0.0),
            "boundary_lengths": boundary_lengths,
        }

    def _public_ring_evaluation(
        self, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        def marker_pairs(
            pairs: list[tuple[str, str]],
        ) -> list[list[int]]:
            return [
                sorted([
                    self.level.sockets[first]["aruco_id"],
                    self.level.sockets[second]["aruco_id"],
                ])
                for first, second in pairs
            ]

        closing_pair = list(candidate.get("closing_pair") or [])
        blocked_by_pair = {
            self._canonical_field_pair(*detail["sockets"]): detail
            for detail in candidate["blocked_edges"]
        }
        boundary_edges = []
        for (first, second), length in zip(
            candidate["pairs"], candidate["boundary_lengths"]
        ):
            field = self._field_between(first, second)
            blocker = blocked_by_pair.get(
                self._canonical_field_pair(first, second)
            )
            if field is not None and blocker is not None:
                state = "existing_occluded"
            elif field is not None:
                if not self._field_endpoints_alive(field):
                    state = "existing_suspended"
                elif field["broken"]:
                    state = "existing_broken"
                else:
                    state = "existing_active"
            elif blocker is not None:
                state = "blocked"
            else:
                state = "required"
            boundary_edges.append({
                "sockets": [first, second],
                "markers": [
                    self.level.sockets[first]["aruco_id"],
                    self.level.sockets[second]["aruco_id"],
                ],
                "length": round(float(length), 3),
                "state": state,
                "field_id": self._canonical_field_id(first, second),
                "blocker_ids": (
                    list(blocker["blocker_ids"]) if blocker else []
                ),
                "blocker_socket_ids": (
                    list(blocker["blocker_socket_ids"]) if blocker else []
                ),
                "blocker_markers": (
                    list(blocker["blocker_markers"]) if blocker else []
                ),
            })
        return {
            "source": candidate["source"],
            "socket_ids": list(candidate["order"]),
            "markers": [
                self.level.sockets[socket_id]["aruco_id"]
                for socket_id in candidate["order"]
            ],
            "turret_count": len(candidate["order"]),
            "excluded_socket_ids": list(
                candidate.get("excluded_socket_ids") or []
            ),
            "excluded_markers": list(candidate.get("excluded_markers") or []),
            "closing_sockets": closing_pair,
            "closing_markers": (
                [
                    self.level.sockets[socket_id]["aruco_id"]
                    for socket_id in closing_pair
                ]
                if closing_pair else []
            ),
            "missing_socket_ids": list(candidate["missing_socket_ids"]),
            "missing_markers": [
                self.level.sockets[socket_id]["aruco_id"]
                for socket_id in candidate["missing_socket_ids"]
            ],
            "missing_edges": marker_pairs(candidate["missing_pairs"]),
            "boundary_edges": boundary_edges,
            "blocked_edges": list(candidate["blocked_edges"]),
            "simple_polygon": bool(candidate["simple_polygon"]),
            "contains_core": bool(candidate["contains_core"]),
            "valid_geometry": bool(candidate["valid_geometry"]),
            "closable": bool(candidate["closable"]),
            "rejection_reasons": list(candidate["rejection_reasons"]),
            "max_edge_length": round(float(candidate["max_edge_length"]), 3),
            "perimeter": round(float(candidate["perimeter"]), 3),
            "score": {
                "turret_count": len(candidate["order"]),
                "max_edge_length": round(
                    float(candidate["max_edge_length"]), 3
                ),
                "perimeter": round(float(candidate["perimeter"]), 3),
                "marker_order": [
                    self.level.sockets[socket_id]["aruco_id"]
                    for socket_id in candidate["order"]
                ],
            },
        }

    def _spatial_marker_order(self, socket_ids: tuple[str, ...]) -> list[str]:
        core_x = float(self.level.core["x"])
        core_y = float(self.level.core["y"])
        return sorted(
            socket_ids,
            key=lambda socket_id: (
                math.atan2(
                    float(self.level.sockets[socket_id]["y"]) - core_y,
                    float(self.level.sockets[socket_id]["x"]) - core_x,
                ),
                int(self.level.sockets[socket_id]["aruco_id"]),
            ),
        )

    def _open_spatial_chain(
        self, order: list[str]
    ) -> tuple[list[str], list[str]]:
        """Open a cyclic spatial order at its largest angular gap.

        Marker IDs are the final tie-breaker, so symmetrical layouts produce
        exactly the same partial ring on every machine and placement order.
        """
        if len(order) < 2:
            return list(order), []
        core_x = float(self.level.core["x"])
        core_y = float(self.level.core["y"])
        angles = {
            socket_id: (
                math.atan2(
                    float(self.level.sockets[socket_id]["y"]) - core_y,
                    float(self.level.sockets[socket_id]["x"]) - core_x,
                )
                % math.tau
            )
            for socket_id in order
        }
        gap_candidates = []
        for index, (first, second) in enumerate(self._ring_pairs(order)):
            gap = (angles[second] - angles[first]) % math.tau
            marker_pair = tuple(sorted((
                int(self.level.sockets[first]["aruco_id"]),
                int(self.level.sockets[second]["aruco_id"]),
            )))
            gap_candidates.append((-gap, marker_pair, index))
        _, _, gap_index = min(gap_candidates)
        start_index = (gap_index + 1) % len(order)
        opened = order[start_index:] + order[:start_index]
        return opened, [opened[-1], opened[0]]

    def _ring_preview_snapshot(self) -> dict[str, Any]:
        """Describe the deterministic, non-collidable partial objective ring."""
        active_socket_ids = tuple(sorted(
            self._active_living_socket_ids(),
            key=lambda socket_id: int(
                self.level.sockets[socket_id]["aruco_id"]
            ),
        ))
        if self.ring_completed_at is not None:
            order = list(self.ring_socket_ids)
            return {
                "source": self.ring_candidate_source,
                "socket_ids": order,
                "markers": [
                    self.level.sockets[socket_id]["aruco_id"]
                    for socket_id in order
                ],
                "closing_sockets": list(self.ring_closing_pair),
                "closing_markers": [
                    self.level.sockets[socket_id]["aruco_id"]
                    for socket_id in self.ring_closing_pair
                ],
                "edges": [],
                "complete": True,
            }
        order: list[str]
        source = "spatial_partial"
        if len(active_socket_ids) >= RING_MIN_TURRETS:
            search = self._spatial_ring_search()
            candidate = search["selected"] or (
                search["rejected"][0] if search["rejected"] else None
            )
            if candidate is not None:
                order = list(candidate["order"])
                source = str(candidate["source"])
            else:
                order = self._spatial_marker_order(active_socket_ids)
        else:
            order = self._spatial_marker_order(active_socket_ids)
        opened, closing_pair = self._open_spatial_chain(order)
        edges = []
        for first, second in zip(opened, opened[1:]):
            obstruction = self._line_obstructions(
                self.placements[first], self.placements[second]
            )
            edges.append({
                "field_id": self._canonical_field_id(first, second),
                "from_socket": first,
                "to_socket": second,
                "from_marker": self.level.sockets[first]["aruco_id"],
                "to_marker": self.level.sockets[second]["aruco_id"],
                "blocker_ids": list(obstruction["blocker_ids"]),
                "blocker_socket_ids": list(
                    obstruction["blocker_socket_ids"]
                ),
                "blocker_markers": list(obstruction["blocker_markers"]),
                "visible": not obstruction["blocker_ids"],
            })
        return {
            "source": source,
            "socket_ids": opened,
            "markers": [
                self.level.sockets[socket_id]["aruco_id"]
                for socket_id in opened
            ],
            "closing_sockets": closing_pair,
            "closing_markers": [
                self.level.sockets[socket_id]["aruco_id"]
                for socket_id in closing_pair
            ],
            "edges": edges,
            "complete": False,
        }

    def _spatial_candidate_score(
        self, candidate: dict[str, Any]
    ) -> tuple[float, float, tuple[int, ...]]:
        return (
            float(candidate["max_edge_length"]),
            float(candidate["perimeter"]),
            tuple(
                int(self.level.sockets[socket_id]["aruco_id"])
                for socket_id in candidate["order"]
            ),
        )

    def _spatial_ring_search(self) -> dict[str, Any]:
        """Pure search over living turret geometry, independent of placement order."""
        active_socket_ids = sorted(
            self._active_living_socket_ids(),
            key=lambda socket_id: int(
                self.level.sockets[socket_id]["aruco_id"]
            ),
        )
        result = {
            "selected": None,
            "alternatives": [],
            "rejected": [],
            "evaluated_count": 0,
            "active_socket_ids": list(active_socket_ids),
        }
        if len(active_socket_ids) < RING_MIN_TURRETS:
            return result

        angular_socket_ids = self._spatial_marker_order(
            tuple(active_socket_ids)
        )
        edge_obstructions = {}
        for first, second in combinations(active_socket_ids, 2):
            pair = self._canonical_field_pair(first, second)
            edge_obstructions[pair] = self._line_obstructions(
                self.placements[first], self.placements[second]
            )
        rejected: list[dict[str, Any]] = []
        maximum_count = min(RING_MAX_TURRETS, len(active_socket_ids))
        active_set = set(active_socket_ids)
        for turret_count in range(maximum_count, RING_MIN_TURRETS - 1, -1):
            closable: list[dict[str, Any]] = []
            for subset in combinations(angular_socket_ids, turret_count):
                order = list(subset)
                result["evaluated_count"] += 1
                pairs = self._ring_pairs(order)
                boundary_is_blocked = any(
                    edge_obstructions.get(
                        self._canonical_field_pair(first, second), {}
                    ).get("blocker_ids", [])
                    for first, second in pairs
                )
                rejected_floor_count = (
                    min(len(candidate["order"]) for candidate in rejected)
                    if len(rejected) >= 3 else 0
                )
                if boundary_is_blocked and turret_count < rejected_floor_count:
                    continue
                candidate = self._analyze_ring_candidate(
                    order,
                    source="spatial_geometry",
                    edge_obstructions=edge_obstructions,
                )
                _, candidate["closing_pair"] = self._open_spatial_chain(order)
                excluded_socket_ids = sorted(
                    active_set - set(order),
                    key=lambda socket_id: int(
                        self.level.sockets[socket_id]["aruco_id"]
                    ),
                )
                candidate["excluded_socket_ids"] = excluded_socket_ids
                candidate["excluded_markers"] = [
                    int(self.level.sockets[socket_id]["aruco_id"])
                    for socket_id in excluded_socket_ids
                ]
                if candidate["closable"]:
                    closable.append(candidate)
                else:
                    rejected.append(candidate)
            if closable:
                closable.sort(key=self._spatial_candidate_score)
                result["selected"] = closable[0]
                result["alternatives"] = closable[1:4]
                break

        rejected.sort(
            key=lambda candidate: (
                -len(candidate["order"]),
                len(candidate["rejection_reasons"]),
                *self._spatial_candidate_score(candidate),
            )
        )
        result["rejected"] = rejected[:3]
        return result

    def _solve_spatial_ring(self) -> dict[str, Any] | None:
        return self._spatial_ring_search()["selected"]

    def _select_ring_candidate(self) -> dict[str, Any] | None:
        if self.ring_candidate_socket_ids or self.ring_completed_at is not None:
            return None
        search = self._spatial_ring_search()
        selected = search["selected"]
        self.ring_search_evaluated_count = int(search["evaluated_count"])
        self.ring_alternative_evaluations = [
            self._public_ring_evaluation(candidate)
            for candidate in search["alternatives"]
        ]
        self.ring_rejected_evaluations = [
            self._public_ring_evaluation(candidate)
            for candidate in search["rejected"]
        ]
        if selected is None:
            self.ring_last_evaluation = (
                dict(self.ring_rejected_evaluations[0])
                if self.ring_rejected_evaluations else None
            )
            return None
        self.ring_last_evaluation = self._public_ring_evaluation(selected)
        return selected

    def _try_establish_ring(self) -> None:
        if (
            self.phase != "running"
            or self.ring_candidate_socket_ids
            or self.ring_completed_at is not None
        ):
            return
        candidate = self._select_ring_candidate()
        if candidate is None:
            return
        order = list(candidate["order"])
        pairs = self._ring_pairs(order)
        missing_pairs = [
            pair for pair in pairs if self._field_between(*pair) is None
        ]
        if any(
            self._line_blockers(
                self.placements[first], self.placements[second]
            )
            for first, second in pairs
        ):
            return
        existing_field_ids = set(self.force_fields)
        event_count = len(self.events)
        try:
            for first_socket, second_socket in missing_pairs:
                field = self._create_force_field(
                    first_socket,
                    second_socket,
                    link_kind="ring_boundary",
                    check_line_of_sight=False,
                )
                if field is None:
                    raise RuntimeError(
                        "ring preflight passed but a boundary could not be created"
                    )
        except Exception:
            for field_id in set(self.force_fields) - existing_field_ids:
                self.force_fields.pop(field_id, None)
            del self.events[event_count:]
            raise
        ring_fields = [
            self._field_between(first, second) for first, second in pairs
        ]
        if any(field is None for field in ring_fields):
            raise RuntimeError("ring preflight passed but a boundary field is missing")
        for field in ring_fields:
            assert field is not None
            field["hits"] = 0
            field["capacity"] = int(self.settings["force_field_hit_capacity"])
            field["broken"] = False
            field["last_hit_at"] = None
            field["last_hit_x"] = None
            field["last_hit_y"] = None
            field["broken_at"] = None
            field["impacted_enemy_ids"].clear()
            field["ring_boundary"] = True
        self._refresh_field_obstructions(reason="ring_established")
        if any(
            field is None or not self._field_operational(field)
            for field in ring_fields
        ):
            raise RuntimeError("ring boundary became obstructed after creation")
        self.ring_candidate_socket_ids = list(order)
        self.ring_candidate_source = str(candidate["source"])
        self.ring_closing_pair = list(candidate.get("closing_pair") or [])
        self._event(
            "ring_topology_established",
            turret_count=len(order),
            edge_count=len(ring_fields),
            created_edge_count=len(missing_pairs),
            candidate_source=self.ring_candidate_source,
            closing_pair=list(self.ring_closing_pair),
            excluded_markers=list(candidate.get("excluded_markers") or []),
            boundary_markers=[
                [
                    self.level.sockets[first]["aruco_id"],
                    self.level.sockets[second]["aruco_id"],
                ]
                for first, second in pairs
            ],
        )
        self._maybe_complete_ring()

    def _maybe_complete_ring(self) -> None:
        if self.ring_completed_at is not None or self.phase != "running":
            return
        order = list(self.ring_candidate_socket_ids)
        if not order:
            return
        points = [
            (float(self.placements[socket_id]["x"]), float(self.placements[socket_id]["y"]))
            for socket_id in order
        ]
        if not _is_valid_core_ring(
            points, float(self.level.core["x"]), float(self.level.core["y"])
        ):
            return
        fields = [
            self._field_between(first, second)
            for first, second in self._ring_pairs(order)
        ]
        if any(
            field is None
            or not self._field_operational(field)
            for field in fields
        ):
            return
        self.ring_completed_at = self.sim_time
        self.ring_socket_ids = list(order)
        self.field_immunity_until = self.sim_time + float(
            self.settings["ring_field_immunity_s"]
        )
        self.core_stage = "ring_ready"
        self._event(
            "ring_completed",
            turret_count=len(order),
            marker_id=CORE_MARKER_ID,
            field_immunity_s=float(self.settings["ring_field_immunity_s"]),
            candidate_source=self.ring_candidate_source,
            closing_pair=list(self.ring_closing_pair),
        )

    def _active_blocked_edges(self) -> set[str]:
        return {
            edge_id
            for field in self.force_fields.values()
            if self._field_operational(field)
            for edge_id in field["blocked_edges"]
        }

    def _field_edge_penalties(self) -> dict[str, float]:
        penalties: dict[str, float] = defaultdict(float)
        for field in self.force_fields.values():
            if not self._field_operational(field):
                continue
            remaining = max(
                0.0,
                1.0 - float(field["hits"]) / max(1.0, float(field["capacity"])),
            )
            penalty = 10000.0 + remaining * 1000.0
            for edge_id in field["blocked_edges"]:
                penalties[edge_id] += penalty
        return dict(penalties)

    def _reroute_enemy(self, enemy: dict[str, Any]) -> bool:
        edge_penalties = self._field_edge_penalties()
        tracking = self._sync_enemy_road_edge(enemy, search_all=True)
        if tracking is None:
            return False
        edge, projection = tracking
        point_x, point_y, segment_index, _, edge_progress, distance = projection
        route = self.level.weakest_route_from_node(
            int(edge["from"]), edge_penalties
        )
        if route is None:
            return False
        forward_edges, _ = route

        path: list[tuple[float, float]] = []

        def append_point(point: tuple[float, float]) -> None:
            if not path or point != path[-1]:
                path.append(point)

        if distance > FLOW_CORRIDOR_RADIUS:
            enemy["x"], enemy["y"] = point_x, point_y
        append_point((float(enemy["x"]), float(enemy["y"])))
        append_point((point_x, point_y))
        for point in reversed(edge["points"][:segment_index + 1]):
            append_point(point)
        for point in self.level._points_for_edges(forward_edges):
            append_point(point)
        if len(path) < 2:
            return False

        enemy["path"] = path
        enemy["segment"] = 0
        enemy["progress"] = 0.0
        enemy["attacking"] = False
        enemy["route_steps"] = [
            {"edge_id": edge["edge_id"], "reverse": True},
            *[
                {"edge_id": forward_edge["edge_id"], "reverse": False}
                for forward_edge in forward_edges
            ],
        ]
        enemy["current_route_step"] = 0
        enemy["current_edge_id"] = edge["edge_id"]
        enemy["current_edge_progress"] = edge_progress
        reversal = float(self.settings["force_field_slow"])
        enemy["vx"] *= -reversal
        enemy["vy"] *= -reversal
        self._event(
            "orc_rerouted",
            enemy_id=enemy["id"],
            node_id=int(edge["from"]),
            edge_id=edge["edge_id"],
            edge_progress=round(edge_progress, 4),
        )
        return True

    def _record_force_field_impact(
        self, field: dict[str, Any], enemy: dict[str, Any]
    ) -> dict[str, Any]:
        contact_x, contact_y, _ = _closest_point_on_segment(
            float(enemy["x"]),
            float(enemy["y"]),
            float(field["ax"]),
            float(field["ay"]),
            float(field["bx"]),
            float(field["by"]),
        )
        impact = {
            "impact_id": f"force_field_impact_{self.next_force_field_impact_id}",
            "field_id": field["field_id"],
            "enemy_id": int(enemy["id"]),
            "enemy_type": str(enemy.get("enemy_type", "grunt")),
            "at": self.sim_time,
            "expires_at": self.sim_time + FORCE_FIELD_ZAP_DURATION_S,
            "contact_x": contact_x,
            "contact_y": contact_y,
            "enemy_x": float(enemy["x"]),
            "enemy_y": float(enemy["y"]),
            "facing_x": float(enemy.get("facing_x", 0.0)),
            "facing_y": float(enemy.get("facing_y", 1.0)),
        }
        self.next_force_field_impact_id += 1
        self.force_field_impacts.append(impact)
        field["last_hit_x"] = contact_x
        field["last_hit_y"] = contact_y
        return impact

    def _handle_force_fields(self, enemies: list[dict[str, Any]]) -> None:
        handled: set[int] = set()
        immune = self.sim_time < self.field_immunity_until
        topology_changed = False
        for field in self.force_fields.values():
            if not self._field_operational(field):
                continue
            for enemy in enemies:
                if enemy["id"] in handled:
                    continue
                contacts = enemy.setdefault("field_contact_until", {})
                if self.sim_time < float(contacts.get(field["field_id"], 0.0)):
                    continue
                distance = _distance_point_to_segment(
                    enemy["x"], enemy["y"], field["ax"], field["ay"], field["bx"], field["by"]
                )
                if distance > FIELD_CONTACT_DISTANCE + enemy["collision_radius"]:
                    continue
                contacts[field["field_id"]] = self.sim_time + FIELD_CONTACT_REARM_S
                field["last_hit_at"] = self.sim_time
                impact = self._record_force_field_impact(field, enemy)
                enemy["hp"] -= float(self.settings["force_field_damage_per_s"])
                self._reroute_enemy(enemy)
                handled.add(enemy["id"])
                counted = enemy["id"] not in field["impacted_enemy_ids"] and not immune
                if counted:
                    field["impacted_enemy_ids"].add(enemy["id"])
                    field["hits"] += 1
                self._event(
                    "force_field_hit",
                    field_id=field["field_id"],
                    enemy_id=enemy["id"],
                    hits=field["hits"],
                    immune=immune,
                    impact_id=impact["impact_id"],
                    contact_x=round(float(impact["contact_x"]), 3),
                    contact_y=round(float(impact["contact_y"]), 3),
                )
                if counted and field["hits"] >= field["capacity"]:
                    field["broken"] = True
                    field["broken_at"] = self.sim_time
                    topology_changed = True
                    self._event("force_field_broken", field_id=field["field_id"])
                    break
        if topology_changed:
            self._sync_tower_link_bonuses()

    def _damage_towers(
        self,
        enemies: list[dict[str, Any]],
        dt: float,
        previous_positions: dict[int, tuple[float, float]] | None = None,
    ) -> None:
        topology_changed = False
        for tower in self.placements.values():
            if tower.get("destroyed"):
                continue
            damage = 0.0
            for enemy in enemies:
                current_x, current_y = float(enemy["x"]), float(enemy["y"])
                previous = (
                    previous_positions.get(int(enemy["id"]))
                    if previous_positions is not None and enemy.get("id") is not None
                    else None
                )
                previous_x, previous_y = previous or (current_x, current_y)
                attack_radius = (
                    TOWER_ATTACK_RADIUS
                    + max(0.0, float(enemy.get("collision_radius", 0.0)))
                )
                overlap = _segment_circle_overlap_fraction(
                    previous_x,
                    previous_y,
                    current_x,
                    current_y,
                    float(tower["x"]),
                    float(tower["y"]),
                    attack_radius,
                )
                damage += (
                    float(enemy.get("tower_dps", enemy.get("core_dps", 0.0)))
                    * dt
                    * overlap
                )
            if damage <= 0.0:
                continue
            previous_hp = max(0.0, float(tower["hp"]))
            tower["hp"] = max(0.0, previous_hp - damage)
            tower["last_damage_at"] = self.sim_time
            tower["last_damage_amount"] = previous_hp - tower["hp"]
            if tower["hp"] <= 0.0:
                tower["destroyed"] = True
                tower["destroyed_at"] = self.sim_time
                tower["last_fire_target"] = None
                tower["last_fire_chain"] = []
                topology_changed = True
                self._event("tower_destroyed", atom_tag_id=tower["atom_tag_id"], socket_id=tower["socket_id"])
        if topology_changed:
            self._reconcile_connections(reason="tower_destroyed")
            self._evaluate_ring_topology()

    def _connections_snapshot(
        self, ring_preview: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Build the one authoritative public state for every canonical edge."""
        attempts_by_field: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for attempt in self.placement_link_attempts:
            attempts_by_field[str(attempt["field_id"])].append(attempt)
        ring_preview = ring_preview or self._ring_preview_snapshot()
        preview_by_field = {
            str(edge["field_id"]): edge
            for edge in ring_preview.get("edges", [])
        }
        phase_state = (
            "preview" if self.phase == "setup"
            else "combat" if self.phase == "running"
            else "hidden"
        )
        field_ids = sorted(
            set(self.force_fields) | set(attempts_by_field) | set(preview_by_field)
        )
        connections = []
        for field_id in field_ids:
            field = self.force_fields.get(field_id)
            attempts = attempts_by_field.get(field_id, [])
            preview_edge = preview_by_field.get(field_id)
            source = field or (attempts[0] if attempts else preview_edge)
            if source is None:
                raise RuntimeError("connection snapshot edge has no source")
            first_socket = str(source["from_socket"])
            second_socket = str(source["to_socket"])
            first = self.placements.get(first_socket)
            second = self.placements.get(second_socket)
            endpoints_alive = bool(
                first is not None and second is not None
                and not first.get("destroyed") and not second.get("destroyed")
            )
            exists = field is not None
            provisional = bool(preview_edge is not None and not exists)
            broken = bool(field and field["broken"])
            occluded = bool(field and self._field_is_occluded(field))
            recent_break = bool(
                broken
                and self.phase == "running"
                and self.sim_time - float(field.get("broken_at") or 0.0) <= 0.65
            )
            visible = bool(
                endpoints_alive
                and phase_state in {"preview", "combat"}
                and (
                    (
                        exists
                        and not occluded
                        and (not broken or recent_break)
                    )
                    or (
                        provisional
                        and bool(preview_edge and preview_edge.get("visible"))
                    )
                )
            )
            collidable = bool(
                exists and endpoints_alive
                and phase_state == "combat" and not broken and not occluded
            )
            if provisional and visible:
                state = "preview"
                visual_state = "preview"
                durability_state = "missing"
            elif not exists:
                state = "blocked"
                visual_state = "blocked"
                durability_state = "missing"
            elif not endpoints_alive:
                state = "suspended"
                visual_state = "suspended"
                durability_state = "broken" if broken else "intact"
            elif occluded:
                state = "occluded"
                visual_state = "occluded"
                durability_state = "broken" if broken else "intact"
            elif broken:
                state = "broken"
                visual_state = "broken"
                durability_state = "broken"
            else:
                state = "active"
                visual_state = (
                    "preview" if phase_state == "preview"
                    else "active" if phase_state == "combat"
                    else "hidden"
                )
                durability_state = "intact"
            attempt_statuses = {str(attempt["status"]) for attempt in attempts}
            attempt_state = (
                "established" if "established" in attempt_statuses
                else "pending_core_marker"
                if "pending_core_marker" in attempt_statuses
                else "pending_empty_socket"
                if "pending_empty_socket" in attempt_statuses
                else "blocked" if attempt_statuses & {"blocked", "pending_blocked"}
                else "pending" if attempt_statuses
                else "not_attempted"
            )
            blocker_ids = sorted({
                str(blocker_id)
                for attempt in attempts
                for blocker_id in attempt.get("blocker_ids", [])
            } | {
                str(blocker_id)
                for blocker_id in (
                    preview_edge.get("blocker_ids", []) if preview_edge else []
                )
            } | {
                str(blocker_id)
                for blocker_id in (
                    field.get("occluded_by_blocker_ids", []) if field else []
                )
            })
            blocker_socket_ids = sorted({
                str(socket_id)
                for attempt in attempts
                for socket_id in attempt.get("blocker_socket_ids", [])
            } | {
                str(socket_id)
                for socket_id in (
                    preview_edge.get("blocker_socket_ids", [])
                    if preview_edge else []
                )
            } | {
                str(socket_id)
                for socket_id in (
                    field.get("occluded_by_socket_ids", []) if field else []
                )
            }, key=lambda socket_id: int(
                self.level.sockets[socket_id]["aruco_id"]
            ))
            blocker_markers = sorted({
                int(marker)
                for attempt in attempts
                for marker in attempt.get("blocker_markers", [])
            } | {
                int(marker)
                for marker in (
                    preview_edge.get("blocker_markers", [])
                    if preview_edge else []
                )
            } | {
                int(marker)
                for marker in (
                    field.get("occluded_by_markers", []) if field else []
                )
            })
            first_geometry = first or self.level.sockets[first_socket]
            second_geometry = second or self.level.sockets[second_socket]
            public_field = (
                {
                    key: value for key, value in field.items()
                    if key != "impacted_enemy_ids"
                }
                if field is not None else {
                    "field_id": field_id,
                    "from_socket": first_socket,
                    "to_socket": second_socket,
                    "from_tag": first.get("atom_tag_id") if first else None,
                    "to_tag": second.get("atom_tag_id") if second else None,
                    "ax": float(first_geometry["x"]),
                    "ay": float(first_geometry["y"]),
                    "bx": float(second_geometry["x"]),
                    "by": float(second_geometry["y"]),
                    "blocked_edges": [],
                    "link_kind": (
                        "placement" if attempts else "ring_preview"
                    ),
                    "ring_boundary": False,
                    "established_at": None,
                    "hits": 0,
                    "capacity": int(self.settings["force_field_hit_capacity"]),
                    "broken": False,
                    "last_hit_at": None,
                    "last_hit_x": None,
                    "last_hit_y": None,
                    "broken_at": None,
                    "occluded_by_blocker_ids": [],
                    "occluded_by_socket_ids": [],
                    "occluded_by_markers": [],
                }
            )
            roles = set()
            if field is not None:
                roles.add(str(field.get("link_kind", "placement")))
                if field.get("ring_boundary"):
                    roles.add("ring_boundary")
            if attempts:
                roles.add("placement")
            if preview_edge is not None:
                roles.add("ring_preview")
            connections.append(public_field | {
                "from_marker": self.level.sockets[first_socket]["aruco_id"],
                "to_marker": self.level.sockets[second_socket]["aruco_id"],
                "exists": exists,
                "provisional": provisional,
                "occluded": occluded,
                "roles": sorted(roles),
                "state": state,
                "attempt_state": attempt_state,
                "attempt_ids": [str(attempt["attempt_id"]) for attempt in attempts],
                "endpoint_state": "live" if endpoints_alive else "suspended",
                "durability_state": durability_state,
                "phase_state": phase_state,
                "visual_state": visual_state,
                "visible": visible,
                "collidable": collidable,
                "invulnerable": bool(
                    exists and self.phase == "running"
                    and self.sim_time < self.field_immunity_until
                ),
                "blocker_ids": blocker_ids,
                "blocker_socket_ids": blocker_socket_ids,
                "blocker_markers": blocker_markers,
            })
        return connections

    def _validate_connection_snapshot(
        self, connections: list[dict[str, Any]]
    ) -> None:
        """Fail loudly if a consumer-visible connection invariant is violated."""
        by_id = {connection["field_id"]: connection for connection in connections}
        if len(by_id) != len(connections):
            raise RuntimeError("connection snapshot contains duplicate field IDs")
        existing_ids = {
            field_id for field_id, connection in by_id.items()
            if connection["exists"]
        }
        if existing_ids != set(self.force_fields):
            raise RuntimeError("connection snapshot diverged from authoritative fields")
        for connection in connections:
            expected_id = self._canonical_field_id(
                connection["from_socket"], connection["to_socket"]
            )
            if connection["field_id"] != expected_id:
                raise RuntimeError("connection snapshot contains a non-canonical field ID")
            if connection["collidable"] and not connection["visible"]:
                raise RuntimeError("collidable connection is not visible")
            if connection["visible"] and connection["endpoint_state"] != "live":
                raise RuntimeError("visible connection is missing a living endpoint")
            if connection["occluded"] and not connection["exists"]:
                raise RuntimeError("missing connection cannot be occluded")
            if connection["occluded"] and (
                connection["visible"] or connection["collidable"]
            ):
                raise RuntimeError("occluded connection is visible or collidable")
            if connection["state"] == "occluded" and not connection["occluded"]:
                raise RuntimeError("occluded connection state has no obstruction")
            if (
                connection["attempt_state"] == "pending_empty_socket"
                and not connection["blocker_socket_ids"]
            ):
                raise RuntimeError("empty-socket attempt has no socket blocker")
            if (
                connection["attempt_state"] == "pending_core_marker"
                and CORE_MARKER_ID not in connection["blocker_markers"]
            ):
                raise RuntimeError("core-blocked attempt has no marker 38 blocker")
            if connection["visible"] and not connection["exists"] and not (
                connection["provisional"]
                and connection["state"] == "preview"
                and not connection["collidable"]
            ):
                raise RuntimeError("visible missing connection is not a safe preview")
        for attempt in self.placement_link_attempts:
            if (
                attempt["status"] == "established"
                and attempt["field_id"] not in existing_ids
            ):
                raise RuntimeError("established placement attempt has no field")
        if self.ring_completed_at is not None:
            for first_socket, second_socket in self._ring_pairs(self.ring_socket_ids):
                connection = by_id.get(
                    self._canonical_field_id(first_socket, second_socket)
                )
                if connection is None or not connection["exists"]:
                    raise RuntimeError("completed ring is missing a boundary connection")
                if not connection["ring_boundary"]:
                    raise RuntimeError("completed ring edge is not marked as a boundary")

    @staticmethod
    def _gates(connections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            dict(connection) for connection in connections
            if connection["exists"]
            and connection["phase_state"] == "combat"
            and connection["visible"]
        ]

    @staticmethod
    def _force_field_visuals(
        connections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [dict(connection) for connection in connections if connection["exists"]]

    def _advance_core_detonation(self) -> None:
        if self.core_stage != "detonating" or self.core_detonation_started_at is None:
            return
        progress = min(
            1.0,
            max(
                0.0,
                (self.sim_time - self.core_detonation_started_at)
                / CORE_DETONATION_DURATION_S,
            ),
        )
        core_x, core_y = float(self.level.core["x"]), float(self.level.core["y"])
        maximum_radius = math.hypot(
            max(core_x, self.level.width - core_x),
            max(core_y, self.level.height - core_y),
        )
        radius = maximum_radius * progress
        for enemy_id in self.core_purge_target_ids - self.core_purge_ignited_ids:
            enemy = self.enemies.get(enemy_id)
            if enemy is None:
                self.core_purge_ignited_ids.add(enemy_id)
                continue
            if progress < 1.0 and math.hypot(
                enemy["x"] - core_x, enemy["y"] - core_y
            ) > radius:
                continue
            self.core_purge_ignited_ids.add(enemy_id)
            enemy["burn_until"] = max(
                float(enemy.get("burn_until", 0.0)), self.sim_time + 3.0
            )
            enemy["burn_damage_per_s"] = max(
                float(enemy.get("burn_damage_per_s", 0.0)),
                max(1.0, float(enemy["hp"])) / 0.35,
            )

    def _finish_core_detonation_if_ready(self) -> bool:
        if self.core_stage != "detonating" or self.core_detonation_started_at is None:
            return False
        elapsed = self.sim_time - self.core_detonation_started_at
        if elapsed < CORE_DETONATION_DURATION_S:
            return False
        if any(enemy_id in self.enemies for enemy_id in self.core_purge_target_ids):
            return False
        self.core_stage = "complete"
        self.phase = "won"
        self._event("core_detonation_complete", kills=self.kills)
        self._event("run_won", reason="core_tag_detonation")
        return True

    def step(self, dt: float) -> None:
        dt = max(0.0, min(float(dt), 0.1))
        with self.lock:
            if self.phase != "running" or self.paused or dt <= 0:
                return
            self.sim_time += dt
            self.force_field_impacts = [
                impact for impact in self.force_field_impacts
                if self.sim_time - float(impact["at"])
                <= FORCE_FIELD_IMPACT_RETENTION_S
            ]
            if self.core_stage != "detonating":
                self._spawn_due()
            self._advance_core_detonation()
            dead: set[int] = set()
            for enemy in self.enemies.values():
                if self.sim_time < float(enemy.get("burn_until", 0.0)):
                    enemy["hp"] -= float(enemy.get("burn_damage_per_s", 0.0)) * dt
                if enemy["hp"] <= 0:
                    dead.add(enemy["id"])
            self._resolve_mortar_rounds(dead)
            living = [
                enemy for enemy in self.enemies.values()
                if enemy["id"] not in dead and enemy["hp"] > 0.0
            ]
            self._handle_force_fields(living)
            for enemy in living:
                if enemy["hp"] <= 0.0:
                    dead.add(enemy["id"])
            living = [enemy for enemy in living if enemy["id"] not in dead]
            previous_enemy_positions = {
                int(enemy["id"]): (float(enemy["x"]), float(enemy["y"]))
                for enemy in living
            }
            slow_by_enemy = {enemy["id"]: 1.0 for enemy in living}
            maximum_speed = max(
                (
                    CORE_BASIN_SPEED if enemy["attacking"]
                    else enemy["speed"] * slow_by_enemy.get(enemy["id"], 1.0)
                )
                for enemy in living
            ) if living else 0.0
            substeps = max(1, min(5, math.ceil(maximum_speed * dt / 8.0)))
            sub_dt = dt / substeps
            for _ in range(substeps):
                self._admit_ready_particles(living)
                self._integrate_particles(living, sub_dt, slow_by_enemy)
            self._admit_ready_particles(living)
            for enemy in living:
                if not enemy["attacking"]:
                    self._sync_enemy_road_edge(enemy)
            if self.core_stage != "detonating":
                self.core_hp -= sum(
                    enemy["core_dps"] * dt for enemy in living if enemy["attacking"]
                )
                self._damage_towers(living, dt, previous_enemy_positions)
            self._fire_towers(dt, dead)
            for enemy_id in dead:
                if self.enemies.pop(enemy_id, None):
                    self.kills += 1
            if self._finish_core_detonation_if_ready():
                pass
            elif self.core_hp <= 0:
                self.core_hp = 0.0
                self.phase = "overrun"
                self._event("core_destroyed")
            elif self.core_stage != "detonating" and self.current_wave >= min(int(self.settings["wave_count"]), len(self.wave_source)) and all(group["spawned"] >= int(group["count"]) for wave in self.launched_waves for group in wave["groups"]) and not self.enemies:
                self.phase = "won"
                self._event("run_won")
        self._changed(force=False)

    def _resolve_mortar_rounds(self, dead: set[int]) -> None:
        remaining: list[dict[str, Any]] = []
        for projectile in self.pending_mortar_rounds:
            if self.sim_time + 1e-9 < float(projectile["impact_at"]):
                remaining.append(projectile)
                continue
            for enemy in self.enemies.values():
                if enemy["id"] in dead or enemy["hp"] <= 0.0:
                    continue
                if math.hypot(
                    enemy["x"] - projectile["target_x"],
                    enemy["y"] - projectile["target_y"],
                ) > projectile["blast_radius"]:
                    continue
                enemy["hp"] -= projectile["damage"]
                if enemy["hp"] <= 0.0:
                    dead.add(enemy["id"])
            self.mortar_impacts.append({
                "projectile_id": projectile["projectile_id"],
                "impact_at": self.sim_time,
                "x": projectile["target_x"],
                "y": projectile["target_y"],
                "blast_radius": projectile["blast_radius"],
            })
        self.pending_mortar_rounds = remaining
        self.mortar_impacts = [
            impact for impact in self.mortar_impacts
            if self.sim_time - float(impact["impact_at"]) <= MORTAR_IMPACT_VISIBLE_S
        ]

    def _queue_mortar_round(
        self,
        tower: dict[str, Any],
        targeting: dict[str, float],
        link_multiplier: float,
    ) -> None:
        projectile_id = self.next_projectile_id
        self.next_projectile_id += 1
        self.pending_mortar_rounds.append({
            "projectile_id": projectile_id,
            "tower_id": tower["placement_id"],
            "launch_at": self.sim_time,
            "impact_at": self.sim_time + MORTAR_FLIGHT_DURATION_S,
            "origin_x": tower["x"],
            "origin_y": tower["y"],
            "target_x": targeting["target_x"],
            "target_y": targeting["target_y"],
            "blast_radius": targeting["blast_radius"],
            "damage": float(self.settings["mortar_damage"])
            * targeting["damage_multiplier"]
            * float(link_multiplier),
        })

    def _tesla_chain(
        self,
        tower: dict[str, Any],
        living: list[dict[str, Any]],
        dead: set[int],
        targeting: dict[str, float],
    ) -> list[dict[str, Any]]:
        candidates = [
            enemy for enemy in living
            if enemy["id"] not in dead
            and math.hypot(enemy["x"] - tower["x"], enemy["y"] - tower["y"])
            <= float(targeting["range"])
        ]
        if not candidates:
            return []
        first = max(
            candidates,
            key=lambda enemy: (
                enemy["progress"],
                -math.hypot(enemy["x"] - tower["x"], enemy["y"] - tower["y"]),
                -enemy["id"],
            ),
        )
        chain = [first]
        used = {first["id"]}
        maximum_links = max(1, int(self.settings["tesla_max_links"]))
        maximum_gap = float(self.settings["tesla_link_distance"])
        while len(chain) < maximum_links:
            previous = chain[-1]
            choices = []
            for enemy in living:
                if enemy["id"] in dead or enemy["id"] in used:
                    continue
                distance = math.hypot(
                    enemy["x"] - previous["x"], enemy["y"] - previous["y"]
                )
                if distance <= maximum_gap:
                    choices.append((distance, -enemy["progress"], enemy["id"], enemy))
            if not choices:
                break
            next_enemy = min(choices, key=lambda item: item[:3])[3]
            chain.append(next_enemy)
            used.add(next_enemy["id"])
        return chain

    def _fire_towers(self, dt: float, dead: set[int]) -> None:
        living = [
            enemy for enemy in self.enemies.values()
            if enemy["id"] not in dead and enemy["hp"] > 0
        ]
        link_state = self._tower_link_state()
        for tower in self.placements.values():
            if tower.get("destroyed"):
                continue
            kind = tower["tower_type"]
            stats = TOWER_STATS[kind]
            link_multiplier = float(
                link_state[str(tower["socket_id"])]["link_multiplier"]
            )
            targeting = self._tower_targeting(tower)
            if kind == "flamethrower":
                tower["facing_angle"] = targeting["sweep_angle"]
            elif kind != "machine_gun":
                tower["facing_angle"] = float(targeting.get("angle", 0.0))
            tower["cooldown"] = max(0.0, tower["cooldown"] - dt)
            if tower["cooldown"] > 0:
                continue

            target_x: float
            target_y: float
            target_enemy_id: int | None = None
            if kind == "mortar":
                nearby = any(
                    enemy["id"] not in dead
                    and math.hypot(
                        enemy["x"] - targeting["target_x"],
                        enemy["y"] - targeting["target_y"],
                    ) <= targeting["blast_radius"]
                    for enemy in living
                )
                if not nearby:
                    continue
                self._queue_mortar_round(
                    tower, targeting, link_multiplier
                )
                target_x, target_y = targeting["target_x"], targeting["target_y"]
            elif kind == "tesla_coil":
                chain = self._tesla_chain(tower, living, dead, targeting)
                if not chain:
                    continue
                payload = []
                base_damage = (
                    float(self.settings["tesla_damage"])
                    * float(targeting["damage_multiplier"])
                    * link_multiplier
                )
                for depth, enemy in enumerate(chain):
                    damage_intensity = TESLA_DAMAGE_FALLOFF ** depth
                    visual_intensity = (
                        float(targeting["visual_intensity"])
                        * damage_intensity
                    )
                    enemy["hp"] -= base_damage * damage_intensity
                    enemy["electrocuted_until"] = self.sim_time + TESLA_EFFECT_DURATION_S
                    enemy["electrocution_depth"] = depth
                    enemy["electrocution_intensity"] = visual_intensity
                    payload.append({
                        "enemy_id": enemy["id"],
                        "x": enemy["x"],
                        "y": enemy["y"],
                        "depth": depth,
                        "intensity": round(visual_intensity, 4),
                        "damage_intensity": round(damage_intensity, 4),
                    })
                    if enemy["hp"] <= 0.0:
                        dead.add(enemy["id"])
                tower["last_fire_chain"] = payload
                target_x, target_y = chain[0]["x"], chain[0]["y"]
            else:
                candidates = []
                center_angle = (
                    targeting["sweep_angle"]
                    if kind == "flamethrower"
                    else targeting["angle"]
                )
                flame_path = (
                    self._flamethrower_path(tower, targeting)
                    if kind == "flamethrower"
                    else None
                )
                for enemy in living:
                    if enemy["id"] in dead:
                        continue
                    dx, dy = enemy["x"] - tower["x"], enemy["y"] - tower["y"]
                    distance = math.hypot(dx, dy)
                    angle = math.atan2(dy, dx)
                    in_target = False
                    if kind == "flamethrower" and flame_path is not None:
                        in_target = self._distance_to_path(
                            enemy["x"], enemy["y"], flame_path
                        ) <= float(targeting["hit_radius"])
                    else:
                        in_target = (
                            distance <= targeting["range"]
                            and abs(math.degrees(_angle_delta(angle, center_angle)))
                            <= targeting["half_angle"]
                        )
                    if in_target:
                        candidates.append((enemy["progress"], -distance, enemy))
                if not candidates:
                    continue
                target = max(candidates, key=lambda item: (item[0], item[1]))[2]
                if kind == "flamethrower":
                    target_x, target_y = flame_path[-1]
                    hit = [item[2] for item in candidates]
                    damage = (
                        float(self.settings["flamethrower_damage"])
                        * link_multiplier
                    )
                else:
                    target_x, target_y = target["x"], target["y"]
                    target_enemy_id = int(target["id"])
                    hit = [target]
                    damage = (
                        float(self.settings["machine_gun_damage"])
                        * link_multiplier
                    )
                    tower["facing_angle"] = math.atan2(
                        target_y - tower["y"], target_x - tower["x"]
                    )
                for enemy in hit:
                    enemy["hp"] -= damage
                    if kind == "flamethrower":
                        enemy["burn_until"] = max(
                            float(enemy.get("burn_until", 0.0)),
                            self.sim_time
                            + float(self.settings["flamethrower_burn_duration_s"]),
                        )
                        enemy["burn_damage_per_s"] = max(
                            float(enemy.get("burn_damage_per_s", 0.0)),
                            float(
                                self.settings[
                                    "flamethrower_burn_damage_per_s"
                                ]
                            )
                            * link_multiplier,
                        )
                    if enemy["hp"] <= 0:
                        dead.add(enemy["id"])

            tower["cooldown"] = 1.0 / stats["rate"]
            tower["last_fire_at"] = self.sim_time
            tower["last_fire_target"] = {
                "x": target_x,
                "y": target_y,
                "kind": kind,
                "enemy_id": target_enemy_id,
            }
            if kind != "tesla_coil":
                tower["last_fire_chain"] = []

    def _public_tower(
        self,
        tower: dict[str, Any],
        link_state: dict[str, dict[str, float | int]] | None = None,
    ) -> dict[str, Any]:
        link_state = link_state or self._tower_link_state()
        bonus = link_state[str(tower["socket_id"])]
        public = {
            key: tower.get(key)
            for key in (
                "placement_id", "atom_tag_id", "owner", "socket_id", "aruco_id", "tower_type",
                "x", "y", "last_fire_at", "last_fire_target", "source", "hp",
                "max_hp", "destroyed", "destroyed_at", "facing_angle",
                "last_fire_chain", "aim_revision", "last_damage_at",
                "last_damage_amount",
            )
        }
        fire_interval = 1.0 / float(TOWER_STATS[tower["tower_type"]]["rate"])
        cooldown = max(0.0, float(tower.get("cooldown", 0.0)))
        public["weapon_charge"] = round(
            max(0.0, min(1.0, 1.0 - cooldown / fire_interval)), 4
        )
        public["charge_duration_s"] = round(fire_interval, 4)
        public["linked_turret_count"] = int(
            bonus["linked_turret_count"]
        )
        public["link_multiplier"] = float(bonus["link_multiplier"])
        public["targeting"] = self._tower_targeting(tower)
        public["health_stage"] = self._tower_health_stage(tower)
        return public

    @staticmethod
    def _force_field_topology_snapshot(
        connections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "field_id": connection["field_id"],
                "from_socket": connection["from_socket"],
                "to_socket": connection["to_socket"],
                "from_marker": connection["from_marker"],
                "to_marker": connection["to_marker"],
                "state": connection["state"],
                "exists": connection["exists"],
                "provisional": bool(connection.get("provisional")),
                "link_kind": connection.get("link_kind", "placement"),
                "roles": list(connection.get("roles", [])),
                "ring_boundary": bool(connection.get("ring_boundary")),
                "hits": int(connection["hits"]),
                "capacity": int(connection["capacity"]),
                "occluded": bool(connection.get("occluded")),
                "blocker_ids": list(connection.get("blocker_ids", [])),
                "blocker_socket_ids": list(
                    connection["blocker_socket_ids"]
                ),
                "blocker_markers": list(connection["blocker_markers"]),
            }
            for connection in connections
        ]

    def _placement_links_snapshot(
        self, connections: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        by_id = {
            connection["field_id"]: connection for connection in connections
        }
        links = []
        for attempt in self.placement_link_attempts:
            connection = by_id[attempt["field_id"]]
            links.append({
                **attempt,
                "from_marker": self.level.sockets[attempt["from_socket"]]["aruco_id"],
                "to_marker": self.level.sockets[attempt["to_socket"]]["aruco_id"],
                "current_state": connection["state"],
                "field_exists": connection["exists"],
                "blocker_ids": list(attempt.get("blocker_ids", [])),
                "blocker_socket_ids": list(
                    attempt.get("blocker_socket_ids", [])
                ),
                "blocker_markers": list(
                    attempt.get("blocker_markers", [])
                ),
            })
        return links

    def _ring_status_snapshot(self) -> dict[str, Any]:
        active_socket_ids = self._active_living_socket_ids()
        selected = list(self.ring_socket_ids or self.ring_candidate_socket_ids)
        cycles = []
        for order in self.level.ring_cycles:
            pairs = self._ring_pairs(order)
            fields = [self._field_between(*pair) for pair in pairs]
            missing_socket_ids = [
                socket_id for socket_id in order
                if socket_id not in active_socket_ids
            ]
            missing_edges = [
                pair for pair, field in zip(pairs, fields) if field is None
            ]
            blocked_edge_details = []
            occluded_edges = []
            for (first, second), field in zip(pairs, fields):
                if {first, second} - active_socket_ids:
                    continue
                obstruction = self._line_obstructions(
                    self.placements[first], self.placements[second]
                )
                blocker_ids = list(obstruction["blocker_ids"])
                if blocker_ids:
                    detail = {
                        "sockets": [first, second],
                        "markers": sorted([
                            self.level.sockets[first]["aruco_id"],
                            self.level.sockets[second]["aruco_id"],
                        ]),
                        "blocker_ids": blocker_ids,
                        "blocker_socket_ids": list(
                            obstruction["blocker_socket_ids"]
                        ),
                        "blocker_markers": list(
                            obstruction["blocker_markers"]
                        ),
                        "field_exists": field is not None,
                    }
                    blocked_edge_details.append(detail)
                    if field is not None:
                        occluded_edges.append((first, second))
            broken_edges = [
                pair for pair, field in zip(pairs, fields)
                if field is not None and field["broken"]
            ]
            suspended_edges = [
                pair for pair, field in zip(pairs, fields)
                if field is not None and not self._field_endpoints_alive(field)
            ]

            def marker_pairs(edges: list[tuple[str, str]]) -> list[list[int]]:
                return [
                    sorted([
                        self.level.sockets[first]["aruco_id"],
                        self.level.sockets[second]["aruco_id"],
                    ])
                    for first, second in edges
                ]

            cycles.append({
                "socket_ids": list(order),
                "markers": [
                    self.level.sockets[socket_id]["aruco_id"]
                    for socket_id in order
                ],
                "turret_count": len(order),
                "active_turret_count": len(order) - len(missing_socket_ids),
                "missing_socket_ids": missing_socket_ids,
                "missing_markers": [
                    self.level.sockets[socket_id]["aruco_id"]
                    for socket_id in missing_socket_ids
                ],
                "missing_edges": marker_pairs(missing_edges),
                "blocked_edges": blocked_edge_details,
                "occluded_edges": marker_pairs(occluded_edges),
                "broken_edges": marker_pairs(broken_edges),
                "suspended_edges": marker_pairs(suspended_edges),
                "closable": not missing_socket_ids and not blocked_edge_details,
                "ready": not (
                    missing_socket_ids
                    or missing_edges
                    or blocked_edge_details
                    or broken_edges
                    or suspended_edges
                ),
                "selected": list(order) == selected,
            })
        return {
            "completed": self.ring_completed_at is not None,
            "candidate_source": self.ring_candidate_source,
            "closing_sockets": list(self.ring_closing_pair),
            "closing_markers": [
                self.level.sockets[socket_id]["aruco_id"]
                for socket_id in self.ring_closing_pair
            ],
            "selected_socket_ids": selected,
            "selected_markers": [
                self.level.sockets[socket_id]["aruco_id"]
                for socket_id in selected
            ],
            "last_evaluation": (
                dict(self.ring_last_evaluation)
                if self.ring_last_evaluation is not None else None
            ),
            "search_evaluated_count": self.ring_search_evaluated_count,
            "alternative_candidates": [
                dict(candidate)
                for candidate in self.ring_alternative_evaluations
            ],
            "rejected_candidates": [
                dict(candidate)
                for candidate in self.ring_rejected_evaluations
            ],
            "cycles": cycles,
        }

    def _core_sequence_snapshot(self) -> dict[str, Any]:
        order = [
            socket_id for socket_id in self.activation_order
            if socket_id in self.placements
            and not self.placements[socket_id].get("destroyed")
        ]
        detonation_progress = 0.0
        if self.core_detonation_started_at is not None:
            detonation_progress = min(
                1.0,
                max(
                    0.0,
                    (self.sim_time - self.core_detonation_started_at)
                    / CORE_DETONATION_DURATION_S,
                ),
            )
        core_x, core_y = float(self.level.core["x"]), float(self.level.core["y"])
        maximum_radius = math.hypot(
            max(core_x, self.level.width - core_x),
            max(core_y, self.level.height - core_y),
        )
        return {
            "marker_id": CORE_MARKER_ID,
            "x": core_x,
            "y": core_y,
            "stage": self.core_stage,
            "ring_min_turrets": RING_MIN_TURRETS,
            "ring_max_turrets": RING_MAX_TURRETS,
            "ring_candidate_count": len(
                self.ring_candidate_socket_ids or order
            ),
            "ring_completed": self.ring_completed_at is not None,
            "ring_completed_at": self.ring_completed_at,
            "ring_source": self.ring_candidate_source,
            "ring_socket_ids": list(self.ring_socket_ids),
            "first_atom_tag_id": self.core_first_tag,
            "first_team": self.core_first_team,
            "core_force_field_active": self.core_stage in {"detonating", "complete"},
            "field_immunity_until": round(self.field_immunity_until, 3),
            "field_immunity_remaining_s": round(
                max(0.0, self.field_immunity_until - self.sim_time), 2
            ),
            "detonation_started_at": self.core_detonation_started_at,
            "detonation_duration_s": CORE_DETONATION_DURATION_S,
            "detonation_progress": round(detonation_progress, 4),
            "detonation_radius": round(maximum_radius * detonation_progress, 2),
            "detonation_max_radius": round(maximum_radius, 2),
            "purge_target_count": len(self.core_purge_target_ids),
            "purge_ignited_count": len(self.core_purge_ignited_ids),
        }

    def snapshot(self, *, compact_enemies: bool = False) -> dict[str, Any]:
        with self.lock:
            if compact_enemies:
                enemies = [
                    {
                        "id": enemy["id"],
                        "enemy_type": enemy["enemy_type"],
                        "x": round(enemy["x"], 1),
                        "y": round(enemy["y"], 1),
                        "vx": round(enemy["vx"], 1),
                        "vy": round(enemy["vy"], 1),
                        "facing_x": round(enemy["facing_x"], 3),
                        "facing_y": round(enemy["facing_y"], 3),
                        "burn_until": round(enemy["burn_until"], 2),
                        "electrocuted_until": round(
                            float(enemy.get("electrocuted_until", 0.0)), 2
                        ),
                        "electrocution_depth": int(
                            enemy.get("electrocution_depth", 0)
                        ),
                        "electrocution_intensity": round(
                            float(enemy.get("electrocution_intensity", 0.0)), 4
                        ),
                    }
                    for enemy in self.enemies.values()
                ]
            else:
                enemies = [
                    ({
                        key: enemy[key]
                        for key in (
                            "id", "enemy_type", "lane", "track", "orbit_index", "x", "y",
                            "vx", "vy", "hp", "max_hp", "collision_radius", "facing_x",
                            "facing_y", "attacking", "progress", "burn_until",
                        )
                    } | {
                        "electrocuted_until": float(
                            enemy.get("electrocuted_until", 0.0)
                        ),
                        "electrocution_depth": int(
                            enemy.get("electrocution_depth", 0)
                        ),
                        "electrocution_intensity": float(
                            enemy.get("electrocution_intensity", 0.0)
                        ),
                    })
                    for enemy in self.enemies.values()
                ]
            link_state = self._sync_tower_link_bonuses()
            towers = [
                self._public_tower(tower, link_state)
                for tower in self.placements.values()
            ]
            ring_preview = self._ring_preview_snapshot()
            connections = self._connections_snapshot(ring_preview)
            self._validate_connection_snapshot(connections)
            return {
                "phase": self.phase,
                "paused": self.paused,
                "virtual_play": self.virtual_play,
                "level_revision": self.level.layout_revision,
                "aruco_code_footprint_px": self.level.aruco_code_footprint_px,
                "core_aruco_code_footprint_px": (
                    self.level.core_aruco_code_footprint_px
                ),
                "force_field_marker_clearance_px": (
                    self.level.force_field_marker_clearance_px
                ),
                "sim_time": round(self.sim_time, 3),
                "wave": self.current_wave,
                "wave_count": int(self.settings["wave_count"]),
                "active_enemies": len(enemies),
                "max_active_enemies": int(self.settings["max_active_enemies"]),
                "pressure_bank": self.pressure_bank,
                "core_hp": round(self.core_hp, 2),
                "core_max_hp": round(self.core_max_hp, 2),
                "kills": self.kills,
                "breaches": self.breaches,
                "enemies": enemies,
                "towers": towers,
                "projectiles": [dict(projectile) for projectile in self.pending_mortar_rounds],
                "mortar_impacts": [dict(impact) for impact in self.mortar_impacts],
                "force_field_impacts": [
                    dict(impact) for impact in self.force_field_impacts
                ],
                "connection_contract_version": 2,
                "connections": connections,
                "gates": self._gates(connections),
                "force_field_visuals": self._force_field_visuals(connections),
                "force_field_topology": self._force_field_topology_snapshot(
                    connections
                ),
                "placement_links": self._placement_links_snapshot(connections),
                "ring_preview": ring_preview,
                "force_field_blockers": [
                    {
                        "blocker_id": blocker["blocker_id"],
                        "points": [list(point) for point in blocker["points"]],
                    }
                    for blocker in self.level.force_field_blockers
                ],
                "ring_status": self._ring_status_snapshot(),
                "core_sequence": self._core_sequence_snapshot(),
                "activation_order": list(self.activation_order),
                "loadout": {str(key): value for key, value in self.loadout.items()},
                "settings": dict(self.settings),
                "events": list(self.events[-30:]),
                "server_time": time.time(),
            }

    def _event(self, kind: str, **detail: Any) -> None:
        self.events.append({"kind": kind, "at": round(self.sim_time, 3), **detail})
        if len(self.events) > 200:
            del self.events[:-200]

    def _changed(self, *, force: bool = True) -> None:
        if not self._wake:
            return
        now = time.monotonic()
        if not force:
            minimum_interval = 0.125 if len(self.enemies) >= 600 else 0.08
            if now - self._last_simulation_wake_at < minimum_interval:
                return
        self._last_simulation_wake_at = now
        self._wake()
