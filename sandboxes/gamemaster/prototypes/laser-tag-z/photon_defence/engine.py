"""Small authoritative tower-defence simulation for the Laser Tag Z vertical slice."""

from __future__ import annotations

import heapq
import json
import math
import random
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .level_layout import layout_revision


MAX_ACTIVE_ENEMIES = 1000
COLLISION_PADDING = 0.05
COLLISION_CELL_SIZE = 24.0
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
TOWER_TYPES = {"machine_gun", "flamethrower", "mortar"}
DEFAULT_LOADOUT = {100: "machine_gun", 101: "flamethrower", 102: "mortar", 103: "reserve"}
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
}
FIELD_CONTACT_DISTANCE = 24.0
TOWER_ATTACK_RADIUS = 68.0
DEFAULT_SETTINGS = {
    "wave_count": 12,
    "wave_interval_s": 45.0,
    "enemy_health_multiplier": 1.0,
    "enemy_speed_multiplier": 1.0,
    "enemy_core_damage_multiplier": 1.0,
    "enemy_count_multiplier": 1.0,
    "release_rate_multiplier": 1.0,
    "force_field_damage_per_s": 8.0,
    "force_field_slow": 0.55,
    "force_field_hit_capacity": 50,
    "machine_gun_damage": 13.0,
    "flamethrower_damage": 10.0,
    "flamethrower_burn_damage_per_s": 4.0,
    "flamethrower_burn_duration_s": 3.0,
    "mortar_damage": 62.0,
    "mortar_far_damage_multiplier": 0.55,
    "defense_unit_health_percent": 15.0,
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
                    distance = math.hypot(dx, dy)
                    preferred = radius + other_radius + FLOW_NEIGHBOR_MARGIN
                    if distance >= preferred:
                        continue
                    if distance <= 1e-9:
                        angle = (enemy_id * 2.399963229728653 + other_id) % (math.pi * 2.0)
                        direction_x, direction_y = math.cos(angle), math.sin(angle)
                    else:
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
            edge = {"from": int(props["from_node"]), "to": int(props["to_node"]), "cost": float(props.get("base_cost", 1.0)), "points": points, "edge_id": str(props.get("edge_id", obj["name"]))}
            self.edges.setdefault(edge["from"], []).append(edge)
            self.edge_by_id[edge["edge_id"]] = edge
        self.paths = {group: self._shortest_path(self._node_id(node)) for group, node in self.spawns.items()}
        self.junctions = {
            (float(node["x"]), float(node["y"]))
            for node in self.nodes.values()
            if node.get("node_kind") in {"junction", "arrival", "turnaround"}
        }
        self.sockets: dict[str, dict[str, Any]] = {}
        self.socket_by_marker: dict[int, str] = {}
        for obj in layers["09 Square Placement Spots (16)"]["objects"]:
            props = _properties(obj)
            socket_id, marker = str(props["socket_id"]), int(props["aruco_id"])
            x, y = self._tile_object_center(obj)
            socket = {"socket_id": socket_id, "aruco_id": marker, "owner": props["owner"], "x": x, "y": y, "size": float(obj.get("width", 208))}
            self.sockets[socket_id] = socket
            self.socket_by_marker[marker] = socket_id
        if sorted(self.socket_by_marker) != list(range(40, 56)):
            raise ValueError("level must map ArUco IDs 40-55 to exactly sixteen sockets")

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

    def _shortest_path(
        self, start_id: int, blocked_edges: set[str] | frozenset[str] | None = None
    ) -> list[tuple[float, float]]:
        blocked_edges = blocked_edges or set()
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
                candidate = distance + edge["cost"]
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
        points: list[tuple[float, float]] = []
        for edge in reversed(ordered):
            for point in edge["points"]:
                if not points or point != points[-1]:
                    points.append(point)
        return points

    def path_from_node(
        self, node_id: int, blocked_edges: set[str] | frozenset[str] | None = None
    ) -> list[tuple[float, float]] | None:
        try:
            return self._shortest_path(int(node_id), blocked_edges)
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


class DefenseEngine:
    def __init__(self, map_path: str | Path, wave_path: str | Path) -> None:
        self.level = LevelModel(map_path)
        self.wave_source = json.loads(Path(wave_path).read_text(encoding="utf-8"))["waves"]
        self.lock = threading.RLock()
        self._wake: Callable[[], None] | None = None
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
            self.kills = 0
            self.breaches = 0
            self.placements: dict[int, dict[str, Any]] = {}
            self.activation_order: list[int] = []
            self.loadout = dict(DEFAULT_LOADOUT)
            self.force_fields: dict[str, dict[str, Any]] = {}
            self.marker_cache: dict[int, dict[str, Any]] = {}
            self.physical_candidates: dict[int, dict[str, Any]] = {}
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
            placements = {tag: dict(placement) for tag, placement in self.placements.items()}
            activation_order = list(self.activation_order)
            self.reset()
            self.virtual_play = virtual_play
            self.loadout = loadout
            self.placements = placements
            self.activation_order = activation_order
            if settings:
                self.settings.update(settings)
            self.settings["max_active_enemies"] = min(MAX_ACTIVE_ENEMIES, int(self.settings["max_active_enemies"]))
            self.core_max_hp = self.core_hp = float(self.settings["core_hp"])
            tower_max_hp = self._tower_max_hp()
            for tower in self.placements.values():
                tower["max_hp"] = tower_max_hp
                tower["hp"] = tower_max_hp
                tower["destroyed"] = False
            self.phase = "running"
            self.run_started_at = time.time()
            self._rebuild_force_fields(reset=True)
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
            raise ValueError("Atom roles are fixed: 100 machine gun, 101 flamethrower, 102 mortar, 103 reserve")
        with self.lock:
            self.loadout[atom_tag_id] = expected

    def _tower_max_hp(self) -> float:
        return max(1.0, self.core_max_hp * float(self.settings["defense_unit_health_percent"]) / 100.0)

    def _repair_tower(self, tower: dict[str, Any], repair_tag: int) -> None:
        if not tower.get("destroyed"):
            raise ValueError("only a destroyed defense unit can be reset")
        tower["max_hp"] = self._tower_max_hp()
        tower["hp"] = tower["max_hp"]
        tower["destroyed"] = False
        tower["cooldown"] = 0.0
        atom_tag_id = int(tower["atom_tag_id"])
        self.activation_order = [tag for tag in self.activation_order if tag != atom_tag_id] + [atom_tag_id]
        self._event("tower_repaired", atom_tag_id=atom_tag_id, repair_tag=repair_tag, socket_id=tower["socket_id"])
        self._rebuild_force_fields()

    def set_tower_aim(self, atom_tag_id: int, angle_degrees: float, spread: float) -> None:
        atom_tag_id = int(atom_tag_id)
        with self.lock:
            tower = self.placements.get(atom_tag_id)
            if not tower:
                raise ValueError("defense unit is not placed")
            if tower.get("destroyed"):
                raise ValueError("destroyed defense unit must be reset before aiming")
            angle = float(angle_degrees)
            reach = float(spread)
            if not math.isfinite(angle) or not math.isfinite(reach) or not 0.0 <= reach <= 1.0:
                raise ValueError("aim angle must be finite and reach must be between 0 and 1")
            tower["aim_angle"] = math.radians(angle % 360.0)
            tower["aim_spread"] = reach
            self._event("tower_aimed", atom_tag_id=atom_tag_id, angle=round(angle % 360.0, 2), reach=round(reach, 3))
        self._changed()

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
            if atom_tag_id == 103:
                if socket_id is None:
                    return
                target = next(
                    (tower for tower in self.placements.values() if tower["socket_id"] == socket_id),
                    None,
                )
                if target is None:
                    raise ValueError("reserve Atom 103 can only reset a destroyed defense unit")
                self._repair_tower(target, atom_tag_id)
                self._changed()
                return
            if socket_id is None:
                if atom_tag_id in self.placements:
                    old = self.placements.pop(atom_tag_id)
                    self.activation_order = [tag for tag in self.activation_order if tag != atom_tag_id]
                    self._rebuild_force_fields()
                    self._event("tower_removed", atom_tag_id=atom_tag_id, socket_id=old["socket_id"], source=source)
                    self._changed()
                return
            if socket_id not in self.level.sockets:
                raise ValueError("unknown socket")
            socket = self.level.sockets[socket_id]
            if socket["owner"] not in {owner, "shared"}:
                raise PermissionError(f"{owner} cannot use {socket['owner']} socket")
            for placed_tag, placed in self.placements.items():
                if placed_tag != atom_tag_id and placed["socket_id"] == socket_id:
                    raise ValueError("socket is already occupied")
            kind = DEFAULT_LOADOUT[atom_tag_id]
            if tower_type not in (None, "", kind):
                raise ValueError(f"Atom {atom_tag_id} always activates {kind.replace('_', ' ')}")
            existing = self.placements.get(atom_tag_id)
            if existing and existing["socket_id"] == socket_id:
                if existing.get("destroyed"):
                    self._repair_tower(existing, atom_tag_id)
                else:
                    self.activation_order = [tag for tag in self.activation_order if tag != atom_tag_id] + [atom_tag_id]
                    self._rebuild_force_fields()
                    self._event("tower_reactivated", atom_tag_id=atom_tag_id, socket_id=socket_id, source=source)
                self._changed()
                return
            self.loadout[atom_tag_id] = kind
            aim_angle = math.atan2(float(self.level.core["y"]) - socket["y"], float(self.level.core["x"]) - socket["x"])
            max_hp = self._tower_max_hp()
            self.placements[atom_tag_id] = {"atom_tag_id": atom_tag_id, "owner": owner, "socket_id": socket_id, "aruco_id": socket["aruco_id"], "tower_type": kind, "x": socket["x"], "y": socket["y"], "cooldown": 0.0, "last_fire_at": None, "last_fire_target": None, "source": source, "aim_angle": aim_angle, "aim_spread": 0.5, "hp": max_hp, "max_hp": max_hp, "destroyed": False}
            self.activation_order = [tag for tag in self.activation_order if tag != atom_tag_id] + [atom_tag_id]
            self._rebuild_force_fields()
            self._event("tower_placed", atom_tag_id=atom_tag_id, socket_id=socket_id, tower_type=kind, source=source)
        self._changed()

    def ingest_physical(self, tags: list[dict[str, Any]], arms: dict[str, dict[str, Any]], *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else float(now)
        current = {int(tag["id"]): tag for tag in tags if tag.get("id") is not None and float(tag.get("missing", 0.0)) <= 0.35}
        with self.lock:
            for marker in range(40, 56):
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
                candidates = [(math.hypot(nx - target["nx"], ny - target["ny"]), marker) for marker, target in self.marker_cache.items() if now - target["seen_at"] <= 120.0]
                distance, marker = min(candidates, default=(math.inf, None))
                if marker is None or distance > 0.05:
                    candidate = self.physical_candidates.get(atom_tag_id)
                    if atom_tag_id in self.placements and (not candidate or candidate.get("marker") is not None):
                        self.physical_candidates[atom_tag_id] = {"marker": None, "since": now}
                    elif candidate and candidate.get("marker") is None and now - candidate["since"] >= 0.45:
                        self.place(atom_tag_id, None, source="physical", team=owner)
                    continue
                candidate = self.physical_candidates.get(atom_tag_id)
                if not candidate or candidate.get("marker") != marker:
                    self.physical_candidates[atom_tag_id] = {"marker": marker, "since": now}
                    continue
                if now - candidate["since"] < 0.55:
                    continue
                socket_id = self.level.socket_by_marker[marker]
                existing = self.placements.get(atom_tag_id)
                if not existing or existing["socket_id"] != socket_id:
                    try:
                        self.place(atom_tag_id, socket_id, source="physical", team=owner)
                    except (PermissionError, ValueError):
                        pass

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
        while self.pressure_queue and len(self.enemies) < active_limit:
            pending = self.pressure_queue[0]
            if not self._spawn_enemy(pending["enemy"], pending["lane_weights"]):
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
                    if self._spawn_enemy(str(group["enemy"]), group.get("lane_weights") or {}):
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

    def _spawn_enemy(self, enemy_type: str, weights: dict[str, Any]) -> bool:
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
                if all(
                    math.hypot(spawn_x - other["x"], spawn_y - other["y"])
                    >= radius + other["collision_radius"] + COLLISION_PADDING
                    for other in self.enemies.values()
                ):
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
            "attacking": False,
            "progress": 0.0,
            "burn_until": 0.0,
            "burn_damage_per_s": 0.0,
        }
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
        ready = sorted(
            (enemy for enemy in living if not enemy["attacking"]),
            key=lambda item: (item["progress"], item["id"]), reverse=True,
        )
        for enemy in ready:
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
        for _ in range(PARTICLE_SOLVER_ITERATIONS):
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
                            distance = math.hypot(dx, dy)
                            minimum = (
                                enemy["collision_radius"] + other["collision_radius"]
                                + COLLISION_PADDING
                            )
                            if distance >= minimum - 1e-6:
                                continue
                            if distance <= 1e-9:
                                angle = (enemy["id"] * 2.399963229728653 + other_id) % (math.pi * 2.0)
                                normal_x, normal_y = math.cos(angle), math.sin(angle)
                            else:
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
                            correction = (minimum - distance) * 0.68
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
        return {
            "angle": angle,
            "angle_degrees": math.degrees(angle) % 360.0,
            "spread": spread,
            "range": maximum_range,
            "half_angle": half_angle,
        }

    def _line_is_clear(self, first: dict[str, Any], second: dict[str, Any]) -> bool:
        active_socket_ids = {
            tower["socket_id"] for tower in self.placements.values()
            if not tower.get("destroyed")
        }
        for socket in self.level.sockets.values():
            if socket["socket_id"] in {first["socket_id"], second["socket_id"]}:
                continue
            if socket["socket_id"] in active_socket_ids:
                continue
            if _segment_intersects_square(
                first["x"], first["y"], second["x"], second["y"],
                socket["x"], socket["y"], socket["size"],
            ):
                return False
        return True

    def _field_has_route(self, blocked_edges: set[str]) -> bool:
        return all(
            self.level.path_from_node(edge["from"], blocked_edges) is not None
            for edge_id in blocked_edges
            for edge in (self.level.edge_by_id[edge_id],)
        )

    def _rebuild_force_fields(self, reset: bool = False) -> None:
        previous = {} if reset else self.force_fields
        order = [
            tag for tag in self.activation_order
            if tag in self.placements and not self.placements[tag].get("destroyed")
        ]
        rebuilt: dict[str, dict[str, Any]] = {}
        cumulative_blocked: set[str] = set()
        for first_tag, second_tag in zip(order, order[1:]):
            first, second = self.placements[first_tag], self.placements[second_tag]
            field_id = f"{first_tag}:{second_tag}"
            if not self._line_is_clear(first, second):
                continue
            blocked_edges = self.level.edges_crossed_by_segment(
                first["x"], first["y"], second["x"], second["y"]
            )
            if blocked_edges and not self._field_has_route(cumulative_blocked | blocked_edges):
                continue
            existing = previous.get(field_id, {})
            rebuilt[field_id] = {
                "field_id": field_id,
                "from_tag": first_tag,
                "to_tag": second_tag,
                "ax": first["x"],
                "ay": first["y"],
                "bx": second["x"],
                "by": second["y"],
                "blocked_edges": sorted(blocked_edges),
                "hits": int(existing.get("hits", 0)),
                "capacity": int(self.settings["force_field_hit_capacity"]),
                "broken": bool(existing.get("broken", False)),
                "last_hit_at": existing.get("last_hit_at"),
                "broken_at": existing.get("broken_at"),
                "impacted_enemy_ids": set(existing.get("impacted_enemy_ids", set())),
            }
            cumulative_blocked.update(blocked_edges)
        self.force_fields = rebuilt

    def _active_blocked_edges(self) -> set[str]:
        return {
            edge_id
            for field in self.force_fields.values()
            if not field["broken"]
            for edge_id in field["blocked_edges"]
        }

    def _reroute_enemy(self, enemy: dict[str, Any]) -> bool:
        blocked_edges = self._active_blocked_edges()
        velocity_x, velocity_y = float(enemy.get("vx", 0.0)), float(enemy.get("vy", 0.0))
        candidates = []
        for node_id, node in self.level.nodes.items():
            if node is self.level.core:
                continue
            dx, dy = float(node["x"]) - enemy["x"], float(node["y"]) - enemy["y"]
            distance = math.hypot(dx, dy)
            behind = dx * velocity_x + dy * velocity_y <= 0.0
            path = self.level.path_from_node(node_id, blocked_edges)
            if path is not None:
                candidates.append((0 if behind else 1, distance, node_id, path))
        if not candidates:
            return False
        _, _, node_id, path = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        node = self.level.nodes[node_id]
        enemy["path"] = [
            (enemy["x"], enemy["y"]),
            (float(node["x"]), float(node["y"])),
            *path[1:],
        ]
        enemy["segment"] = 0
        enemy["progress"] = 0.0
        enemy["attacking"] = False
        reversal = float(self.settings["force_field_slow"])
        enemy["vx"] *= -reversal
        enemy["vy"] *= -reversal
        self._event("orc_rerouted", enemy_id=enemy["id"], node_id=node_id)
        return True

    def _handle_force_fields(self, enemies: list[dict[str, Any]]) -> None:
        for field in self.force_fields.values():
            if field["broken"]:
                continue
            for enemy in enemies:
                if enemy["id"] in field["impacted_enemy_ids"]:
                    continue
                distance = _distance_point_to_segment(
                    enemy["x"], enemy["y"], field["ax"], field["ay"], field["bx"], field["by"]
                )
                if distance > FIELD_CONTACT_DISTANCE + enemy["collision_radius"]:
                    continue
                field["impacted_enemy_ids"].add(enemy["id"])
                field["hits"] += 1
                field["last_hit_at"] = self.sim_time
                enemy["hp"] -= float(self.settings["force_field_damage_per_s"])
                self._reroute_enemy(enemy)
                self._event("force_field_hit", field_id=field["field_id"], enemy_id=enemy["id"], hits=field["hits"])
                if field["hits"] >= field["capacity"]:
                    field["broken"] = True
                    field["broken_at"] = self.sim_time
                    self._event("force_field_broken", field_id=field["field_id"])
                    break

    def _damage_towers(self, enemies: list[dict[str, Any]], dt: float) -> None:
        destroyed = False
        for tower in self.placements.values():
            if tower.get("destroyed"):
                continue
            damage = sum(
                enemy["core_dps"] * dt
                for enemy in enemies
                if math.hypot(enemy["x"] - tower["x"], enemy["y"] - tower["y"]) <= TOWER_ATTACK_RADIUS
            )
            if damage <= 0.0:
                continue
            tower["hp"] = max(0.0, tower["hp"] - damage)
            if tower["hp"] <= 0.0:
                tower["destroyed"] = True
                destroyed = True
                self._event("tower_destroyed", atom_tag_id=tower["atom_tag_id"], socket_id=tower["socket_id"])
        if destroyed:
            self._rebuild_force_fields()

    def _gates(self) -> list[dict[str, Any]]:
        if self.phase != "running":
            return []
        return [
            {
                key: value for key, value in field.items()
                if key != "impacted_enemy_ids"
            }
            for field in self.force_fields.values()
            if not field["broken"]
            or self.sim_time - float(field.get("broken_at") or 0.0) <= 0.65
        ]

    def step(self, dt: float) -> None:
        dt = max(0.0, min(float(dt), 0.1))
        with self.lock:
            if self.phase != "running" or self.paused or dt <= 0:
                return
            self.sim_time += dt
            self._spawn_due()
            dead: set[int] = set()
            for enemy in self.enemies.values():
                if self.sim_time < float(enemy.get("burn_until", 0.0)):
                    enemy["hp"] -= float(enemy.get("burn_damage_per_s", 0.0)) * dt
                if enemy["hp"] <= 0:
                    dead.add(enemy["id"])
            living = [
                enemy for enemy in self.enemies.values()
                if enemy["id"] not in dead and enemy["hp"] > 0.0
            ]
            self._handle_force_fields(living)
            for enemy in living:
                if enemy["hp"] <= 0.0:
                    dead.add(enemy["id"])
            living = [enemy for enemy in living if enemy["id"] not in dead]
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
            self.core_hp -= sum(
                enemy["core_dps"] * dt for enemy in living if enemy["attacking"]
            )
            self._damage_towers(living, dt)
            self._fire_towers(dt, dead)
            for enemy_id in dead:
                if self.enemies.pop(enemy_id, None):
                    self.kills += 1
            if self.core_hp <= 0:
                self.core_hp = 0.0
                self.phase = "overrun"
                self._event("core_destroyed")
            elif self.current_wave >= min(int(self.settings["wave_count"]), len(self.wave_source)) and all(group["spawned"] >= int(group["count"]) for wave in self.launched_waves for group in wave["groups"]) and not self.enemies:
                self.phase = "won"
                self._event("run_won")
        self._changed()

    def _fire_towers(self, dt: float, dead: set[int]) -> None:
        living = [enemy for enemy in self.enemies.values() if enemy["id"] not in dead and enemy["hp"] > 0]
        for tower in self.placements.values():
            if tower.get("destroyed"):
                continue
            stats = TOWER_STATS[tower["tower_type"]]
            tower["cooldown"] = max(0.0, tower["cooldown"] - dt)
            if tower["cooldown"] > 0:
                continue
            targeting = self._tower_targeting(tower)
            kind = tower["tower_type"]
            if kind == "mortar":
                hit = [
                    enemy for enemy in living
                    if enemy["id"] not in dead
                    and math.hypot(enemy["x"] - targeting["target_x"], enemy["y"] - targeting["target_y"])
                    <= targeting["blast_radius"]
                ]
                if not hit:
                    continue
                target_x, target_y = targeting["target_x"], targeting["target_y"]
                damage = float(self.settings["mortar_damage"]) * targeting["damage_multiplier"]
            else:
                candidates = []
                for enemy in living:
                    if enemy["id"] in dead:
                        continue
                    dx, dy = enemy["x"] - tower["x"], enemy["y"] - tower["y"]
                    distance = math.hypot(dx, dy)
                    angle = math.atan2(dy, dx)
                    if distance <= targeting["range"] and abs(math.degrees(_angle_delta(angle, targeting["angle"]))) <= targeting["half_angle"]:
                        candidates.append((enemy["progress"], -distance, enemy))
                if not candidates:
                    continue
                target = max(candidates, key=lambda item: (item[0], item[1]))[2]
                target_x, target_y = target["x"], target["y"]
                if kind == "flamethrower":
                    hit = [item[2] for item in candidates]
                    damage = float(self.settings["flamethrower_damage"])
                else:
                    hit = [target]
                    damage = float(self.settings["machine_gun_damage"])
            for enemy in hit:
                enemy["hp"] -= damage
                if kind == "flamethrower":
                    enemy["burn_until"] = max(
                        float(enemy.get("burn_until", 0.0)),
                        self.sim_time + float(self.settings["flamethrower_burn_duration_s"]),
                    )
                    enemy["burn_damage_per_s"] = max(
                        float(enemy.get("burn_damage_per_s", 0.0)),
                        float(self.settings["flamethrower_burn_damage_per_s"]),
                    )
                if enemy["hp"] <= 0:
                    dead.add(enemy["id"])
            tower["cooldown"] = 1.0 / stats["rate"]
            tower["last_fire_at"] = self.sim_time
            tower["last_fire_target"] = {"x": target_x, "y": target_y, "kind": kind}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            enemies = [{key: enemy[key] for key in ("id", "enemy_type", "lane", "track", "orbit_index", "x", "y", "hp", "max_hp", "collision_radius", "facing_x", "facing_y", "attacking", "progress", "burn_until")} for enemy in self.enemies.values()]
            towers = []
            for tower in self.placements.values():
                public = {key: tower.get(key) for key in ("atom_tag_id", "owner", "socket_id", "aruco_id", "tower_type", "x", "y", "last_fire_at", "last_fire_target", "source", "hp", "max_hp", "destroyed")}
                public["targeting"] = self._tower_targeting(tower)
                towers.append(public)
            return {"phase": self.phase, "paused": self.paused, "virtual_play": self.virtual_play, "level_revision": self.level.layout_revision, "sim_time": round(self.sim_time, 3), "wave": self.current_wave, "wave_count": int(self.settings["wave_count"]), "active_enemies": len(enemies), "max_active_enemies": int(self.settings["max_active_enemies"]), "pressure_bank": self.pressure_bank, "core_hp": round(self.core_hp, 2), "core_max_hp": round(self.core_max_hp, 2), "kills": self.kills, "breaches": self.breaches, "enemies": enemies, "towers": towers, "gates": self._gates(), "activation_order": list(self.activation_order), "loadout": {str(key): value for key, value in self.loadout.items()}, "settings": dict(self.settings), "events": list(self.events[-30:]), "server_time": time.time()}

    def _event(self, kind: str, **detail: Any) -> None:
        self.events.append({"kind": kind, "at": round(self.sim_time, 3), **detail})
        if len(self.events) > 200:
            del self.events[:-200]

    def _changed(self) -> None:
        if self._wake:
            self._wake()
