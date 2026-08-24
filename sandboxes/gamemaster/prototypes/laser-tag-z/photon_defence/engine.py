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
PARTICLE_SOLVER_ITERATIONS = 8
PARTICLE_MAX_SPEED_SCALE = 1.35
PATH_WAYPOINT_RADIUS = 8.0
CORNER_RADIUS = 30.0
CORNER_SAMPLES = 6
CORE_BASIN_HALF_SIZE = 112.0
CORE_KEEP_OUT_HALF_SIZE = 74.0
CORE_BASIN_SPEED = 40.0
CORE_ENTRY_SEARCH_COUNT = 64
CORE_HOLDING_ADMISSION_RADIUS = 18.0
ATOM_OWNERS = {100: "green", 101: "green", 102: "purple", 103: "purple"}
TOWER_TYPES = {"machine_gun", "flamethrower", "mortar"}
DEFAULT_LOADOUT = {100: "machine_gun", 101: "flamethrower", 102: "machine_gun", 103: "mortar"}
ENEMY_STATS = {
    "grunt": {"hp": 70.0, "speed": 62.0, "core_dps": 6.0, "collision_radius": 2.2},
    "runner": {"hp": 46.0, "speed": 104.0, "core_dps": 4.0, "collision_radius": 2.2},
    "breaker": {"hp": 130.0, "speed": 50.0, "core_dps": 10.0, "collision_radius": 2.2},
    "brute": {"hp": 240.0, "speed": 34.0, "core_dps": 16.0, "collision_radius": 2.8},
}
TOWER_STATS = {
    "machine_gun": {"range": 285.0, "rate": 6.0, "damage": 13.0},
    "flamethrower": {"range": 185.0, "rate": 4.0, "damage": 10.0},
    "mortar": {"range": 500.0, "min_range": 110.0, "rate": 0.8, "damage": 62.0, "splash": 105.0},
}
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


def _closest_point_on_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> tuple[float, float, float]:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return ax, ay, 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return ax + dx * t, ay + dy * t, t


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
        for obj in layers["06 Enemy Path Graph (hidden)"]["objects"]:
            props = _properties(obj)
            points = [(float(obj["x"]) + float(point["x"]), float(obj["y"]) + float(point["y"])) for point in obj.get("polyline", [])]
            if not points:
                points = [(self.nodes[props["from_node"]]["x"], self.nodes[props["from_node"]]["y"]), (self.nodes[props["to_node"]]["x"], self.nodes[props["to_node"]]["y"])]
            edge = {"to": int(props["to_node"]), "cost": float(props.get("base_cost", 1.0)), "points": points, "edge_id": props.get("edge_id", obj["name"])}
            self.edges.setdefault(int(props["from_node"]), []).append(edge)
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
            socket = {"socket_id": socket_id, "aruco_id": marker, "owner": props["owner"], "x": x, "y": y}
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

    def _shortest_path(self, start_id: int) -> list[tuple[float, float]]:
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
            self.marker_cache: dict[int, dict[str, Any]] = {}
            self.physical_candidates: dict[int, dict[str, Any]] = {}
            self.events: list[dict[str, Any]] = []

    def set_wake(self, callback: Callable[[], None]) -> None:
        self._wake = callback

    def set_physical_source(self, callback: Callable[[], tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]]) -> None:
        self._physical_source = callback

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
            self.phase = "running"
            self.run_started_at = time.time()
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
        if atom_tag_id not in ATOM_OWNERS or tower_type not in TOWER_TYPES:
            raise ValueError("invalid Atom tag or tower type")
        with self.lock:
            self.loadout[atom_tag_id] = tower_type

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
                if atom_tag_id in self.placements:
                    old = self.placements.pop(atom_tag_id)
                    self.activation_order = [tag for tag in self.activation_order if tag != atom_tag_id]
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
            kind = tower_type or self.loadout[atom_tag_id]
            if kind not in TOWER_TYPES:
                raise ValueError("invalid tower type")
            self.loadout[atom_tag_id] = kind
            self.placements[atom_tag_id] = {"atom_tag_id": atom_tag_id, "owner": owner, "socket_id": socket_id, "aruco_id": socket["aruco_id"], "tower_type": kind, "x": socket["x"], "y": socket["y"], "cooldown": 0.0, "last_fire_at": None, "source": source}
            self.activation_order = [tag for tag in self.activation_order if tag != atom_tag_id] + [atom_tag_id]
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
        path = self._path_to_core_basin(lane)
        initial_dx, initial_dy = path[1][0] - spawn_x, path[1][1] - spawn_y
        initial_length = math.hypot(initial_dx, initial_dy)
        speed = (
            stats["speed"] * float(self.settings["enemy_speed_multiplier"])
            * self._rng.uniform(0.94, 1.06)
        )
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
            "basin_radius": self._rng.uniform(
                CORE_KEEP_OUT_HALF_SIZE + radius + 1.0,
                CORE_BASIN_HALF_SIZE - radius - 1.0,
            ),
            "basin_direction": -1.0 if enemy_id % 7 == 0 else 1.0,
            "blocked_steps": 0,
            "path": path,
            "segment": 0,
            "attacking": False,
            "progress": 0.0,
        }
        return True

    def _path_to_core_basin(self, lane: str) -> list[tuple[float, float]]:
        core_x, core_y = float(self.level.core["x"]), float(self.level.core["y"])
        arrival_y = float(self.level.paths[lane][-2][1])
        road_centerline = [tuple(point) for point in self.level.paths[lane][:-2]]
        road_centerline.append((core_x - CORE_BASIN_HALF_SIZE - 18.0, arrival_y))
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
        inner = CORE_KEEP_OUT_HALF_SIZE + radius
        if abs(dx) >= inner or abs(dy) >= inner:
            return
        choices = (
            (inner - abs(dx), "x"),
            (inner - abs(dy), "y"),
        )
        _, axis = min(choices)
        if axis == "x":
            sign = -1.0 if dx < 0.0 or (dx == 0.0 and enemy["vx"] < 0.0) else 1.0
            enemy["x"] = center_x + sign * inner
            if enemy["vx"] * sign < 0.0:
                enemy["vx"] *= -0.2
        else:
            sign = -1.0 if dy < 0.0 or (dy == 0.0 and enemy["vy"] < 0.0) else 1.0
            enemy["y"] = center_y + sign * inner
            if enemy["vy"] * sign < 0.0:
                enemy["vy"] *= -0.2

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
            if target_distance <= PATH_WAYPOINT_RADIUS or (
                projection >= 0.96 and target_distance <= FLOW_CORRIDOR_RADIUS * 1.25
            ):
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
        if abs(dx) >= abs(dy):
            normal_x, normal_y = (-1.0 if dx < 0.0 else 1.0), 0.0
        else:
            normal_x, normal_y = 0.0, (-1.0 if dy < 0.0 else 1.0)
        tangent_x = -normal_y * enemy["basin_direction"]
        tangent_y = normal_x * enemy["basin_direction"]
        square_radius = max(abs(dx), abs(dy))
        radial = max(-0.8, min(0.8, (enemy["basin_radius"] - square_radius) / 16.0))
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
        response = 1.0 - math.exp(-4.8 * dt)
        enemy["vx"] += (desired_x - enemy["vx"]) * response
        enemy["vy"] += (desired_y - enemy["vy"]) * response

    def _find_basin_entry(
        self, enemy: dict[str, Any], grid: _CollisionGrid
    ) -> tuple[float, float] | None:
        center_x, center_y = float(self.level.core["x"]), float(self.level.core["y"])
        radius = enemy["collision_radius"]
        x = center_x - CORE_BASIN_HALF_SIZE + radius + 0.5
        low = center_y - CORE_BASIN_HALF_SIZE + radius + 0.5
        high = center_y + CORE_BASIN_HALF_SIZE - radius - 0.5
        base = 0.18 if enemy["lane"].startswith("top") else 0.82
        for attempt in range(CORE_ENTRY_SEARCH_COUNT):
            fraction = (base + attempt * 0.618033988749895 + enemy["id"] * 0.037) % 1.0
            y = low + (high - low) * fraction
            if not grid.collides(x, y, radius):
                return x, y
        return None

    def _admit_ready_particles(self, living: list[dict[str, Any]]) -> None:
        grid = _CollisionGrid(living)
        ready = sorted(
            (enemy for enemy in living if not enemy["attacking"]),
            key=lambda item: (item["progress"], item["id"]), reverse=True,
        )
        for enemy in ready:
            self._update_road_progress(enemy)
            path = enemy["path"]
            distance = math.hypot(enemy["x"] - path[-1][0], enemy["y"] - path[-1][1])
            if enemy["segment"] < len(path) - 1 and distance > CORE_HOLDING_ADMISSION_RADIUS:
                continue
            grid.remove(enemy["id"])
            entry = self._find_basin_entry(enemy, grid)
            if entry is None:
                grid.add(enemy["id"], enemy["x"], enemy["y"], enemy["collision_radius"])
                continue
            enemy["attacking"] = True
            enemy["x"], enemy["y"] = entry
            enemy["vx"], enemy["vy"] = 0.0, -CORE_BASIN_SPEED
            enemy["progress"] = 1.0
            enemy["blocked_steps"] = 0
            self.breaches += 1
            grid.add(enemy["id"], enemy["x"], enemy["y"], enemy["collision_radius"])

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

    def _gates(self) -> list[dict[str, Any]]:
        order = [tag for tag in self.activation_order if tag in self.placements]
        if len(order) < 2:
            return []
        pairs = list(zip(order, order[1:]))
        if len(order) == 4:
            pairs.append((order[-1], order[0]))
        return [{"from_tag": a, "to_tag": b, "ax": self.placements[a]["x"], "ay": self.placements[a]["y"], "bx": self.placements[b]["x"], "by": self.placements[b]["y"]} for a, b in pairs]

    def step(self, dt: float) -> None:
        dt = max(0.0, min(float(dt), 0.1))
        with self.lock:
            if self.phase != "running" or self.paused or dt <= 0:
                return
            self.sim_time += dt
            self._spawn_due()
            gates = self._gates()
            dead: set[int] = set()
            slow_by_enemy: dict[int, float] = {}
            for enemy in self.enemies.values():
                slow = 1.0
                for gate in gates:
                    if _distance_point_to_segment(enemy["x"], enemy["y"], gate["ax"], gate["ay"], gate["bx"], gate["by"]) <= 18.0:
                        slow = min(slow, float(self.settings["force_field_slow"]))
                        enemy["hp"] -= float(self.settings["force_field_damage_per_s"]) * dt
                if enemy["hp"] <= 0:
                    dead.add(enemy["id"])
                slow_by_enemy[enemy["id"]] = slow
            living = [
                enemy for enemy in self.enemies.values()
                if enemy["id"] not in dead and enemy["hp"] > 0.0
            ]
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
            stats = TOWER_STATS[tower["tower_type"]]
            tower["cooldown"] = max(0.0, tower["cooldown"] - dt)
            if tower["cooldown"] > 0:
                continue
            candidates = []
            for enemy in living:
                distance = math.hypot(enemy["x"] - tower["x"], enemy["y"] - tower["y"])
                if distance <= stats["range"] and distance >= stats.get("min_range", 0.0):
                    candidates.append((enemy["progress"], -distance, enemy))
            if not candidates:
                continue
            target = max(candidates, key=lambda item: (item[0], item[1]))[2]
            if tower["tower_type"] == "flamethrower":
                hit = [enemy for enemy in living if math.hypot(enemy["x"] - target["x"], enemy["y"] - target["y"]) <= 72.0]
            elif tower["tower_type"] == "mortar":
                hit = [enemy for enemy in living if math.hypot(enemy["x"] - target["x"], enemy["y"] - target["y"]) <= stats["splash"]]
            else:
                hit = [target]
            for enemy in hit:
                enemy["hp"] -= stats["damage"]
                if enemy["hp"] <= 0:
                    dead.add(enemy["id"])
            tower["cooldown"] = 1.0 / stats["rate"]
            tower["last_fire_at"] = self.sim_time

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            enemies = [{key: enemy[key] for key in ("id", "enemy_type", "lane", "track", "orbit_index", "x", "y", "hp", "max_hp", "collision_radius", "facing_x", "facing_y", "attacking", "progress")} for enemy in self.enemies.values()]
            towers = [{key: tower[key] for key in ("atom_tag_id", "owner", "socket_id", "aruco_id", "tower_type", "x", "y", "last_fire_at", "source")} for tower in self.placements.values()]
            return {"phase": self.phase, "paused": self.paused, "virtual_play": self.virtual_play, "sim_time": round(self.sim_time, 3), "wave": self.current_wave, "wave_count": int(self.settings["wave_count"]), "active_enemies": len(enemies), "max_active_enemies": int(self.settings["max_active_enemies"]), "pressure_bank": self.pressure_bank, "core_hp": round(self.core_hp, 2), "core_max_hp": round(self.core_max_hp, 2), "kills": self.kills, "breaches": self.breaches, "enemies": enemies, "towers": towers, "gates": self._gates(), "activation_order": list(self.activation_order), "loadout": {str(key): value for key, value in self.loadout.items()}, "settings": dict(self.settings), "events": list(self.events[-30:]), "server_time": time.time()}

    def _event(self, kind: str, **detail: Any) -> None:
        self.events.append({"kind": kind, "at": round(self.sim_time, 3), **detail})
        if len(self.events) > 200:
            del self.events[:-200]

    def _changed(self) -> None:
        if self._wake:
            self._wake()
