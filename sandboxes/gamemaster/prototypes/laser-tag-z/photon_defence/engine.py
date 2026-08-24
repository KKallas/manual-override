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
COLLISION_PADDING = 2.0
COLLISION_STEP = 6.0
COLLISION_CELL_SIZE = 64.0
JUNCTION_CLEARANCE = 64.0
ATOM_OWNERS = {100: "green", 101: "green", 102: "purple", 103: "purple"}
TOWER_TYPES = {"machine_gun", "flamethrower", "mortar"}
DEFAULT_LOADOUT = {100: "machine_gun", 101: "flamethrower", 102: "machine_gun", 103: "mortar"}
ENEMY_STATS = {
    "grunt": {"hp": 70.0, "speed": 62.0, "core_dps": 6.0, "collision_radius": 22.0},
    "runner": {"hp": 46.0, "speed": 104.0, "core_dps": 4.0, "collision_radius": 22.0},
    "breaker": {"hp": 130.0, "speed": 50.0, "core_dps": 10.0, "collision_radius": 22.0},
    "brute": {"hp": 240.0, "speed": 34.0, "core_dps": 16.0, "collision_radius": 28.0},
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


def _tile_draw_offset(alignment: str, width: float, height: float) -> tuple[float, float]:
    value = (alignment or "bottomleft").lower()
    dx = 0.0 if "left" in value else -width if "right" in value else -width / 2.0
    dy = 0.0 if value.startswith("top") else -height if value.startswith("bottom") else -height / 2.0
    return dx, dy


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

    def add(self, enemy_id: int, x: float, y: float, radius: float) -> None:
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
            self.attack_slot_cursor: dict[tuple[str, str], int] = defaultdict(int)
            self.junction_reservations: dict[tuple[float, float], int] = {}
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
        while remaining_lanes:
            values = [float(weights.get(candidate, 1.0)) for candidate in remaining_lanes]
            candidate = self._rng.choices(remaining_lanes, weights=values, k=1)[0]
            spawn_x, spawn_y = self.level.paths[candidate][0]
            if all(
                math.hypot(spawn_x - other["x"], spawn_y - other["y"])
                >= radius + other["collision_radius"] + COLLISION_PADDING
                for other in self.enemies.values()
            ):
                lane = candidate
                break
            remaining_lanes.remove(candidate)
        if lane is None:
            return False
        hp = stats["hp"] * float(self.settings["enemy_health_multiplier"])
        path = self._path_to_attack_slot(lane)
        enemy_id = self.next_enemy_id
        self.next_enemy_id += 1
        self.enemies[enemy_id] = {"id": enemy_id, "enemy_type": enemy_type, "lane": lane, "x": path[0][0], "y": path[0][1], "hp": hp, "max_hp": hp, "speed": stats["speed"] * float(self.settings["enemy_speed_multiplier"]), "core_dps": stats["core_dps"] * float(self.settings["enemy_core_damage_multiplier"]), "collision_radius": radius, "path": path, "segment": 0, "attacking": False, "progress": 0.0}
        return True

    def _path_to_attack_slot(self, lane: str) -> list[tuple[float, float]]:
        side = "top" if lane.startswith("top") else "bottom"
        base_path = [tuple(point) for point in self.level.paths[lane][:-1]]
        arrival_x = base_path[-1][0]
        prior_x = base_path[-2][0]
        direction = "right" if arrival_x >= prior_x else "left"
        cursor_key = (side, direction)
        slot = self.attack_slot_cursor[cursor_key] % 2
        self.attack_slot_cursor[cursor_key] += 1
        core_x, core_y = float(self.level.core["x"]), float(self.level.core["y"])
        ring_y = core_y - 104.0 if side == "top" else core_y + 104.0
        side_y = core_y - 30.0 if side == "top" else core_y + 30.0
        if slot == 0:
            offset = 60.0 if direction == "right" else -60.0
            approach = [(core_x + offset, ring_y)]
        else:
            offset = 104.0 if direction == "right" else -104.0
            approach = [(core_x + offset, ring_y), (core_x + offset, side_y)]
        return base_path + approach

    def _release_cleared_junctions(self) -> None:
        for junction, owner_id in list(self.junction_reservations.items()):
            owner = self.enemies.get(owner_id)
            if owner is None or owner["hp"] <= 0:
                self.junction_reservations.pop(junction, None)
                continue
            occurrences = [index for index, point in enumerate(owner["path"]) if point == junction]
            if not occurrences:
                self.junction_reservations.pop(junction, None)
                continue
            passed = owner["segment"] >= max(occurrences)
            if owner["attacking"] or (
                passed and math.hypot(owner["x"] - junction[0], owner["y"] - junction[1]) >= JUNCTION_CLEARANCE
            ):
                self.junction_reservations.pop(junction, None)

    def _junction_blocks(self, enemy: dict[str, Any], candidate_x: float, candidate_y: float) -> bool:
        for junction in self.level.junctions:
            candidate_distance = math.hypot(candidate_x - junction[0], candidate_y - junction[1])
            if candidate_distance >= JUNCTION_CLEARANCE:
                continue
            current_distance = math.hypot(enemy["x"] - junction[0], enemy["y"] - junction[1])
            owner_id = self.junction_reservations.get(junction)
            if owner_id is None and current_distance >= JUNCTION_CLEARANCE:
                self.junction_reservations[junction] = enemy["id"]
                owner_id = enemy["id"]
            if owner_id not in (None, enemy["id"]):
                return True
        return False

    def _advance_enemy(self, enemy: dict[str, Any], remaining: float, grid: _CollisionGrid) -> None:
        path = enemy["path"]
        while remaining > 1e-9 and enemy["segment"] < len(path) - 1:
            target_x, target_y = path[enemy["segment"] + 1]
            dx, dy = target_x - enemy["x"], target_y - enemy["y"]
            distance = math.hypot(dx, dy)
            if distance <= 1e-9:
                enemy["x"], enemy["y"] = target_x, target_y
                enemy["segment"] += 1
                continue
            step = min(remaining, distance, COLLISION_STEP)
            ratio = step / distance
            candidate_x = enemy["x"] + dx * ratio
            candidate_y = enemy["y"] + dy * ratio
            if self._junction_blocks(enemy, candidate_x, candidate_y) or grid.collides(candidate_x, candidate_y, enemy["collision_radius"]):
                low, high = 0.0, step
                for _ in range(10):
                    trial = (low + high) / 2.0
                    trial_ratio = trial / distance
                    trial_x = enemy["x"] + dx * trial_ratio
                    trial_y = enemy["y"] + dy * trial_ratio
                    if self._junction_blocks(enemy, trial_x, trial_y) or grid.collides(trial_x, trial_y, enemy["collision_radius"]):
                        high = trial
                    else:
                        low = trial
                if low > 1e-4:
                    move_ratio = low / distance
                    enemy["x"] += dx * move_ratio
                    enemy["y"] += dy * move_ratio
                break
            enemy["x"], enemy["y"] = candidate_x, candidate_y
            remaining -= step
            if step >= distance - 1e-9:
                enemy["x"], enemy["y"] = target_x, target_y
                enemy["segment"] += 1

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
            self._release_cleared_junctions()
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
            collision_grid = _CollisionGrid(list(self.enemies.values()))
            ordered = sorted(
                self.enemies.values(),
                key=lambda item: (bool(item["attacking"]), float(item["progress"]), item["id"]),
                reverse=True,
            )
            for enemy in ordered:
                if enemy["id"] in dead:
                    continue
                if enemy["attacking"]:
                    self.core_hp -= enemy["core_dps"] * dt
                    continue
                collision_grid.remove(enemy["id"])
                remaining = enemy["speed"] * slow_by_enemy[enemy["id"]] * dt
                path = enemy["path"]
                self._advance_enemy(enemy, remaining, collision_grid)
                collision_grid.add(enemy["id"], enemy["x"], enemy["y"], enemy["collision_radius"])
                enemy["progress"] = enemy["segment"] / max(1, len(path) - 1)
                if enemy["segment"] >= len(path) - 1:
                    enemy["attacking"] = True
                    self.breaches += 1
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
            enemies = [{key: enemy[key] for key in ("id", "enemy_type", "lane", "x", "y", "hp", "max_hp", "collision_radius", "attacking", "progress")} for enemy in self.enemies.values()]
            towers = [{key: tower[key] for key in ("atom_tag_id", "owner", "socket_id", "aruco_id", "tower_type", "x", "y", "last_fire_at", "source")} for tower in self.placements.values()]
            return {"phase": self.phase, "paused": self.paused, "virtual_play": self.virtual_play, "sim_time": round(self.sim_time, 3), "wave": self.current_wave, "wave_count": int(self.settings["wave_count"]), "active_enemies": len(enemies), "max_active_enemies": int(self.settings["max_active_enemies"]), "pressure_bank": self.pressure_bank, "core_hp": round(self.core_hp, 2), "core_max_hp": round(self.core_max_hp, 2), "kills": self.kills, "breaches": self.breaches, "enemies": enemies, "towers": towers, "gates": self._gates(), "activation_order": list(self.activation_order), "loadout": {str(key): value for key, value in self.loadout.items()}, "settings": dict(self.settings), "events": list(self.events[-30:]), "server_time": time.time()}

    def _event(self, kind: str, **detail: Any) -> None:
        self.events.append({"kind": kind, "at": round(self.sim_time, 3), **detail})
        if len(self.events) > 200:
            del self.events[:-200]

    def _changed(self) -> None:
        if self._wake:
            self._wake()
