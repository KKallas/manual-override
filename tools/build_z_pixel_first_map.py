#!/usr/bin/env python3
"""Build the first editable Photon Yard map from the Z pixel v2 modular pack."""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TILED = ROOT / "assets" / "tiled"
LEVELS = TILED / "levels"
TILESETS = TILED / "tilesets"
MAP_PATH = LEVELS / "z-pixel-first-map.tmj"
WAVE_PATH = LEVELS / "z-pixel-first-map.waves.json"
REPORT_PATH = TILED / "z-pixel-first-map-validation.json"

MAP_W = 1696
MAP_H = 960
TILE = 32
ROAD_STEP = 160
ROAD_DRAW = 160
SOCKET_SIZE = 208


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def prop(name: str, value: Any, type_name: str | None = None) -> dict[str, Any]:
    if type_name is None:
        if isinstance(value, bool):
            type_name = "bool"
        elif isinstance(value, int):
            type_name = "int"
        elif isinstance(value, float):
            type_name = "float"
        else:
            type_name = "string"
    return {"name": name, "type": type_name, "value": value}


def props(*values: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(values, key=lambda item: item["name"])


class Factory:
    def __init__(self) -> None:
        self.next_id = 1

    def base(self, name: str, class_name: str, x: float, y: float) -> dict[str, Any]:
        result = {
            "height": 0,
            "id": self.next_id,
            "name": name,
            "rotation": 0,
            "type": class_name,
            "visible": True,
            "width": 0,
            "x": x,
            "y": y,
        }
        self.next_id += 1
        return result

    def point(self, name: str, class_name: str, x: float, y: float, properties: list[dict[str, Any]]) -> dict[str, Any]:
        result = self.base(name, class_name, x, y)
        result.update({"point": True, "properties": properties})
        return result

    def rectangle(self, name: str, class_name: str, x: float, y: float, width: float, height: float, properties: list[dict[str, Any]]) -> dict[str, Any]:
        result = self.base(name, class_name, x, y)
        result.update({"width": width, "height": height, "properties": properties})
        return result

    def polygon(self, name: str, class_name: str, points: list[tuple[float, float]], properties: list[dict[str, Any]]) -> dict[str, Any]:
        x0, y0 = points[0]
        result = self.base(name, class_name, x0, y0)
        result.update({"polygon": [{"x": x - x0, "y": y - y0} for x, y in points], "properties": properties})
        return result

    def polyline(self, name: str, class_name: str, points: list[tuple[float, float]], properties: list[dict[str, Any]]) -> dict[str, Any]:
        x0, y0 = points[0]
        result = self.base(name, class_name, x0, y0)
        result.update({"polyline": [{"x": x - x0, "y": y - y0} for x, y in points], "properties": properties})
        return result

    def tile_object(
        self,
        name: str,
        class_name: str,
        x: float,
        y: float,
        width: float,
        height: float,
        gid: int,
        properties: list[dict[str, Any]],
        rotation: float = 0,
        visible: bool = True,
    ) -> dict[str, Any]:
        result = self.base(name, class_name, x, y)
        result.update(
            {
                "gid": gid,
                "width": width,
                "height": height,
                "properties": properties,
                "rotation": rotation,
                "visible": visible,
            }
        )
        return result


def object_layer(
    layer_id: int,
    name: str,
    objects: list[dict[str, Any]],
    *,
    visible: bool = True,
    locked: bool = False,
    color: str | None = None,
    draworder: str = "topdown",
) -> dict[str, Any]:
    layer: dict[str, Any] = {
        "draworder": draworder,
        "id": layer_id,
        "name": name,
        "objects": objects,
        "opacity": 1,
        "type": "objectgroup",
        "visible": visible,
        "x": 0,
        "y": 0,
    }
    if locked:
        layer["locked"] = True
    if color:
        layer["color"] = color
    return layer


def load_tilesets() -> tuple[list[dict[str, Any]], dict[str, int]]:
    names = ["ground", "seam-safe-roads", "core-plaza", "targets", "towers", "core", "force-fields", "seam-caps"]
    references = []
    gids: dict[str, int] = {}
    first_gid = 1
    for name in names:
        path = TILESETS / f"z-pixel-v2-{name}.tsj"
        tileset = json.loads(path.read_text(encoding="utf-8"))
        references.append({"firstgid": first_gid, "source": f"../tilesets/{path.name}"})
        for tile in tileset["tiles"]:
            asset_id = next(item["value"] for item in tile["properties"] if item["name"] == "asset_id")
            gids[asset_id] = first_gid + tile["id"]
        first_gid += tileset["tilecount"]
    return references, gids


def path_length(points: list[tuple[float, float]]) -> float:
    return round(sum(math.dist(a, b) for a, b in zip(points, points[1:])), 2)


def point_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    px, py = point
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.dist(point, start)
    amount = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + amount * dx), py - (ay + amount * dy))


def build_map() -> tuple[dict[str, Any], dict[str, Any]]:
    tilesets, gids = load_tilesets()
    factory = Factory()

    ground_names = ["gunmetal-clean", "concrete-cracked", "service-panels", "energy-conduits"]
    ground = []
    for row, y in enumerate((160, 480, 800)):
        for column, x in enumerate((160, 480, 800, 1120, 1440, 1760)):
            asset_name = ground_names[(row + column) % len(ground_names)]
            asset_id = f"ground/{asset_name}"
            ground.append(
                factory.tile_object(
                    f"ground_{row}_{column}",
                    "GroundModule",
                    x,
                    y,
                    320,
                    320,
                    gids[asset_id],
                    props(
                        prop("asset_id", asset_id),
                        prop("grid_column", column),
                        prop("grid_row", row),
                        prop("module_family", "ground"),
                        prop("normalized_size", 320),
                    ),
                )
            )

    # Six horizontal lane rows: four long approaches plus two return/direct rows.
    placements: dict[tuple[int, int], tuple[str, float, str]] = {}
    for row in (0, 1, 4, 5):
        for column in range(10):
            placements[(column, row)] = ("roads/straight-horizontal", 0, "E,W")
    for row in (2, 3):
        for column in range(10):
            placements[(column, row)] = ("roads/straight-horizontal", 0, "E,W")

    # At the left edge, each inner approach branches into a short direct line
    # toward the central mega tower. The roads remain modular and continuous.
    placements[(0, 1)] = ("roads/junction-t-esw", 0, "E,S,W")
    # Cross junctions keep the upper and lower direct lines connected through
    # the previously open middle-left gap as a continuous vertical passthrough.
    placements[(0, 2)] = ("roads/junction-cross", 0, "N,E,S,W")
    placements[(0, 3)] = ("roads/junction-cross", 0, "N,E,S,W")
    placements[(0, 4)] = ("roads/junction-t-esw", 180, "N,E,W")

    # Two crossover columns let gates force a longer zig-zag route.
    for column in (3, 6):
        for row in (0, 1, 4, 5):
            placements[(column, row)] = ("roads/junction-cross", 0, "N,E,S,W")

    # Continue the first crossover column through the two direct lanes. This
    # creates a second full-height vertical passthrough left of the core.
    placements[(3, 2)] = ("roads/junction-cross", 0, "N,E,S,W")
    placements[(3, 3)] = ("roads/junction-cross", 0, "N,E,S,W")

    # Far-right pairwise merges and 180-degree returns.
    # The generated corner-es art has an off-center south port. Rotating the
    # visually aligned corner-wn counterclockwise preserves W/S connectivity
    # while centering the vertical seam over the T junction below.
    placements[(9, 0)] = ("roads/corner-wn", -90, "S,W")
    placements[(9, 1)] = ("roads/junction-t-esw", 90, "N,S,W")
    placements[(9, 2)] = ("roads/corner-wn", 0, "N,W")
    placements[(9, 5)] = ("roads/corner-wn", 0, "N,W")
    placements[(9, 4)] = ("roads/junction-t-esw", 90, "N,S,W")
    placements[(9, 3)] = ("roads/corner-wn", -90, "S,W")

    # Remove the two platform-bearing modules immediately above and below the
    # core. A separate 256px open road-bed plaza replaces them after the
    # standard lanes are assembled, letting enemies surround the square core.
    placements.pop((5, 2))
    placements.pop((5, 3))

    # The 320px pixel-art modules render at an exact 1:2 scale on the 160px
    # road grid, avoiding the uneven curb pixels introduced by the former 164px
    # scale. The source corner is not actually symmetric: its two port centers
    # differ. These integer half-scale corrections align each mouth with its
    # adjoining straight or T junction without resampling the pixel art.
    road_visual_offsets = {
        (9, 0): (0, -2),
        (9, 1): (0, -2),
        (9, 2): (3, -6),
        (9, 3): (0, -2),
        (9, 4): (0, -2),
        (9, 5): (3, -6),
    }

    roads = []
    right_t_seam_caps = []
    for index, ((column, row), (asset_id, rotation, ports)) in enumerate(sorted(placements.items(), key=lambda item: (item[0][1], item[0][0])), start=1):
        visual_dx, visual_dy = road_visual_offsets.get((column, row), (0, 0))
        road = factory.tile_object(
                f"road_{index:02d}_{asset_id.split('/')[-1]}",
                "RoadModule",
                80 + column * ROAD_STEP + visual_dx,
                80 + row * ROAD_STEP + visual_dy,
                ROAD_DRAW,
                ROAD_DRAW,
                gids[asset_id],
                props(
                    prop("asset_id", asset_id),
                    prop("display_footprint", ROAD_DRAW),
                    prop("grid_column", column),
                    prop("grid_row", row),
                    prop("module_family", "roads"),
                    prop("normalized_size", 320),
                    prop("ports", ports),
                    prop("snap_step", ROAD_STEP),
                    prop("visual_offset_x", visual_dx),
                    prop("visual_offset_y", visual_dy),
                    prop("visual_depth", "sunken"),
                ),
                rotation,
            )
        # The upward corner's first few north-edge pixels taper inward. Keep
        # the aligned right-side T junctions last in index draw order so their
        # straight south lips cover that four-pixel overlap cleanly.
        if (column, row) in {(9, 1), (9, 4)}:
            right_t_seam_caps.append(road)
        else:
            roads.append(road)
    roads.extend(right_t_seam_caps)

    # A six-pixel half-scale interior slice from the branch-free normalized
    # straight-horizontal art bridges the only two remaining T-to-corner curb
    # discontinuities. It rotates into a uniform vertical strip without the
    # T's internal branch widening the patch. No generated bitmap is modified.
    for name, seam_y in (("top", 317), ("bottom", 797)):
        roads.append(
            factory.tile_object(
                f"road_seam_cap_{name}_right",
                "RoadSeamCap",
                1520,
                seam_y,
                6,
                66,
                gids["roads/seam-cap-vertical"],
                props(
                    prop("asset_id", "roads/seam-cap-vertical"),
                    prop("display_footprint", "6x66@90deg"),
                    prop("module_family", "roads"),
                    prop("ports", "N,S"),
                    prop("seam_role", "t_to_upward_corner"),
                    prop("visual_depth", "sunken"),
                ),
                90,
            )
        )

    # This opaque road-bed derivative visually removes the platform tiles in
    # the protected 256px arrival clearance. It is ordered after the standard
    # road modules so no curbs or raised floor fragments remain beside the core.
    roads.append(
        factory.tile_object(
            "road_core_access_plaza",
            "CoreAccessPlaza",
            880,
            480,
            256,
            256,
            gids["roads/core-access-plaza"],
            props(
                prop("asset_id", "roads/core-access-plaza"),
                prop("allows_multi_attacker_access", True),
                prop("corner_vertical_stubs", False),
                prop("curb_end_profile", "32px-stepped-taper"),
                prop("display_footprint", 256),
                prop("edge_style", "recessed_industrial_curb"),
                prop("lane_openings", "W:top,bottom;E:top,bottom"),
                prop("keep_clear", True),
                prop("module_family", "roads"),
                prop("ports", "N,E,S,W"),
                prop("removed_center_grid_cells", "5,2;5,3"),
                prop("visual_depth", "sunken"),
            ),
        )
    )

    node_specs = [
        ("spawn_top_outer", "spawn", 0, 80, "top_outer"),
        ("spawn_top_inner", "spawn", 0, 240, "top_inner"),
        ("spawn_bottom_inner", "spawn", 0, 720, "bottom_inner"),
        ("spawn_bottom_outer", "spawn", 0, 880, "bottom_outer"),
        ("top_outer_switch_a", "junction", 560, 80, ""),
        ("top_inner_switch_a", "junction", 560, 240, ""),
        ("top_outer_switch_b", "junction", 1040, 80, ""),
        ("top_inner_switch_b", "junction", 1040, 240, ""),
        ("bottom_inner_switch_a", "junction", 560, 720, ""),
        ("bottom_outer_switch_a", "junction", 560, 880, ""),
        ("bottom_inner_switch_b", "junction", 1040, 720, ""),
        ("bottom_outer_switch_b", "junction", 1040, 880, ""),
        ("top_merge_right", "turnaround", 1520, 240, ""),
        ("top_return", "junction", 1520, 400, ""),
        ("bottom_merge_right", "turnaround", 1520, 720, ""),
        ("bottom_return", "junction", 1520, 560, ""),
        ("direct_top_left", "junction", 80, 400, ""),
        ("direct_bottom_left", "junction", 80, 560, ""),
        ("direct_top_switch_a", "junction", 560, 400, ""),
        ("direct_bottom_switch_a", "junction", 560, 560, ""),
        ("core_arrival_top", "arrival", 880, 400, ""),
        ("core_arrival_bottom", "arrival", 880, 560, ""),
        ("mega_tower_entry", "core", 880, 480, ""),
    ]
    nodes = []
    node_by_name: dict[str, dict[str, Any]] = {}
    for name, kind, x, y, spawn_group in node_specs:
        node = factory.point(
            name,
            "PathNode",
            x,
            y,
            props(prop("node_id", name), prop("node_kind", kind), prop("spawn_group", spawn_group)),
        )
        nodes.append(node)
        node_by_name[name] = node

    edge_specs = [
        ("edge_top_outer_start", "spawn_top_outer", "top_outer_switch_a", "approach", [(0, 80), (560, 80)]),
        ("edge_top_outer_mid", "top_outer_switch_a", "top_outer_switch_b", "route_segment", [(560, 80), (1040, 80)]),
        ("edge_top_outer_tail", "top_outer_switch_b", "top_merge_right", "route_segment", [(1040, 80), (1520, 80), (1520, 240)]),
        ("edge_top_inner_start", "spawn_top_inner", "top_inner_switch_a", "approach", [(0, 240), (560, 240)]),
        ("edge_top_inner_mid", "top_inner_switch_a", "top_inner_switch_b", "route_segment", [(560, 240), (1040, 240)]),
        ("edge_top_inner_tail", "top_inner_switch_b", "top_merge_right", "route_segment", [(1040, 240), (1520, 240)]),
        ("edge_switch_top_a_down", "top_outer_switch_a", "top_inner_switch_a", "detour", [(560, 80), (560, 240)]),
        ("edge_switch_top_a_up", "top_inner_switch_a", "top_outer_switch_a", "detour", [(560, 240), (560, 80)]),
        ("edge_switch_top_b_down", "top_outer_switch_b", "top_inner_switch_b", "detour", [(1040, 80), (1040, 240)]),
        ("edge_switch_top_b_up", "top_inner_switch_b", "top_outer_switch_b", "detour", [(1040, 240), (1040, 80)]),
        ("edge_bottom_inner_start", "spawn_bottom_inner", "bottom_inner_switch_a", "approach", [(0, 720), (560, 720)]),
        ("edge_bottom_inner_mid", "bottom_inner_switch_a", "bottom_inner_switch_b", "route_segment", [(560, 720), (1040, 720)]),
        ("edge_bottom_inner_tail", "bottom_inner_switch_b", "bottom_merge_right", "route_segment", [(1040, 720), (1520, 720)]),
        ("edge_bottom_outer_start", "spawn_bottom_outer", "bottom_outer_switch_a", "approach", [(0, 880), (560, 880)]),
        ("edge_bottom_outer_mid", "bottom_outer_switch_a", "bottom_outer_switch_b", "route_segment", [(560, 880), (1040, 880)]),
        ("edge_bottom_outer_tail", "bottom_outer_switch_b", "bottom_merge_right", "route_segment", [(1040, 880), (1520, 880), (1520, 720)]),
        ("edge_switch_bottom_a_down", "bottom_inner_switch_a", "bottom_outer_switch_a", "detour", [(560, 720), (560, 880)]),
        ("edge_switch_bottom_a_up", "bottom_outer_switch_a", "bottom_inner_switch_a", "detour", [(560, 880), (560, 720)]),
        ("edge_switch_bottom_b_down", "bottom_inner_switch_b", "bottom_outer_switch_b", "detour", [(1040, 720), (1040, 880)]),
        ("edge_switch_bottom_b_up", "bottom_outer_switch_b", "bottom_inner_switch_b", "detour", [(1040, 880), (1040, 720)]),
        ("edge_top_turnaround", "top_merge_right", "top_return", "turnaround", [(1520, 240), (1520, 400)]),
        ("edge_top_return_to_arrival", "top_return", "core_arrival_top", "return", [(1520, 400), (880, 400)]),
        ("edge_bottom_turnaround", "bottom_merge_right", "bottom_return", "turnaround", [(1520, 720), (1520, 560)]),
        ("edge_bottom_return_to_arrival", "bottom_return", "core_arrival_bottom", "return", [(1520, 560), (880, 560)]),
        ("edge_top_direct_access", "spawn_top_inner", "direct_top_left", "direct_access", [(0, 240), (80, 240), (80, 400)]),
        ("edge_top_direct_mid", "direct_top_left", "direct_top_switch_a", "direct", [(80, 400), (560, 400)]),
        ("edge_top_direct_to_arrival", "direct_top_switch_a", "core_arrival_top", "direct", [(560, 400), (880, 400)]),
        ("edge_bottom_direct_access", "spawn_bottom_inner", "direct_bottom_left", "direct_access", [(0, 720), (80, 720), (80, 560)]),
        ("edge_bottom_direct_mid", "direct_bottom_left", "direct_bottom_switch_a", "direct", [(80, 560), (560, 560)]),
        ("edge_bottom_direct_to_arrival", "direct_bottom_switch_a", "core_arrival_bottom", "direct", [(560, 560), (880, 560)]),
        ("edge_core_approach_top", "core_arrival_top", "mega_tower_entry", "core_approach", [(880, 400), (880, 480)]),
        ("edge_core_approach_bottom", "core_arrival_bottom", "mega_tower_entry", "core_approach", [(880, 560), (880, 480)]),
        ("edge_direct_left_down", "direct_top_left", "direct_bottom_left", "direct_crossover", [(80, 400), (80, 560)]),
        ("edge_direct_left_up", "direct_bottom_left", "direct_top_left", "direct_crossover", [(80, 560), (80, 400)]),
        ("edge_mid_passthrough_top_down", "top_inner_switch_a", "direct_top_switch_a", "mid_passthrough", [(560, 240), (560, 400)]),
        ("edge_mid_passthrough_top_up", "direct_top_switch_a", "top_inner_switch_a", "mid_passthrough", [(560, 400), (560, 240)]),
        ("edge_mid_passthrough_center_down", "direct_top_switch_a", "direct_bottom_switch_a", "mid_passthrough", [(560, 400), (560, 560)]),
        ("edge_mid_passthrough_center_up", "direct_bottom_switch_a", "direct_top_switch_a", "mid_passthrough", [(560, 560), (560, 400)]),
        ("edge_mid_passthrough_bottom_down", "direct_bottom_switch_a", "bottom_inner_switch_a", "mid_passthrough", [(560, 560), (560, 720)]),
        ("edge_mid_passthrough_bottom_up", "bottom_inner_switch_a", "direct_bottom_switch_a", "mid_passthrough", [(560, 720), (560, 560)]),
    ]
    edges = []
    for edge_id, from_name, to_name, phase, points in edge_specs:
        edges.append(
            factory.polyline(
                edge_id,
                "PathEdge",
                points,
                props(
                    prop("base_cost", path_length(points), "float"),
                    prop("edge_id", edge_id),
                    prop("enabled", True),
                    prop("from_node", node_by_name[from_name]["id"], "object"),
                    prop("road_width", 72.0, "float"),
                    prop("route_phase", phase),
                    prop("to_node", node_by_name[to_name]["id"], "object"),
                ),
            )
        )

    reach_zones = [
        factory.polygon("purple_reach", "ReachZone", [(0, 0), (1696, 0), (1696, 360), (0, 360)], props(prop("owner", "purple"), prop("priority", 2))),
        factory.polygon("shared_reach", "ReachZone", [(480, 280), (1360, 280), (1360, 680), (480, 680)], props(prop("owner", "shared"), prop("priority", 1))),
        factory.polygon("green_reach", "ReachZone", [(0, 600), (1696, 600), (1696, 960), (0, 960)], props(prop("owner", "green"), prop("priority", 2))),
    ]

    pair_specs = [
        ("top_inner_a", 640, 240, "purple", True, "edge_top_inner_mid", "edge_switch_top_a_up", 88),
        ("top_outer_b", 880, 80, "purple", False, "edge_top_outer_mid", "edge_switch_top_a_down", 72),
        ("top_outer_c", 1120, 80, "purple", True, "edge_top_outer_tail", "edge_switch_top_b_down", 72),
        ("top_inner_d", 1360, 240, "shared", False, "edge_top_inner_tail", "edge_switch_top_b_up", 88),
        ("bottom_inner_a", 640, 720, "green", True, "edge_bottom_inner_mid", "edge_switch_bottom_a_down", 88),
        ("bottom_outer_b", 880, 880, "green", False, "edge_bottom_outer_mid", "edge_switch_bottom_a_up", 72),
        ("bottom_outer_c", 1120, 880, "green", True, "edge_bottom_outer_tail", "edge_switch_bottom_b_up", 72),
        ("bottom_inner_d", 1360, 720, "shared", False, "edge_bottom_inner_tail", "edge_switch_bottom_b_down", 88),
    ]
    sockets = []
    socket_by_name: dict[str, dict[str, Any]] = {}
    gate_specs: list[dict[str, Any]] = []
    socket_number = 1
    for pair_id, x, lane_y, owner, active_preview, blocked_edge, detour_edge, socket_offset in pair_specs:
        pair_sockets = []
        for side, y in (("north", lane_y - socket_offset), ("south", lane_y + socket_offset)):
            socket_id = f"socket_{socket_number:02d}"
            aruco_id = 39 + socket_number
            socket_number += 1
            state = "active" if active_preview else "inactive"
            asset_id = f"objectives/target-{owner}-{state}"
            socket = factory.tile_object(
                socket_id,
                "TowerSocket",
                x,
                y,
                SOCKET_SIZE,
                SOCKET_SIZE,
                gids[asset_id],
                props(
                    prop("active_preview", active_preview),
                    prop("aruco_id", aruco_id),
                    prop("asset_id", asset_id),
                    prop("gate_pair_id", pair_id),
                    prop("owner", owner),
                    prop("pair_side", side),
                    prop("resource_favored", owner != "shared"),
                    prop("socket_id", socket_id),
                    prop("strategic_value", "standard" if owner != "shared" else "support"),
                ),
            )
            sockets.append(socket)
            socket_by_name[socket_id] = socket
            pair_sockets.append(socket)
        gate_specs.append(
            {
                "pair_id": pair_id,
                "x": x,
                "y": lane_y,
                "owner": owner,
                "active_preview": active_preview,
                "blocked_edge": blocked_edge,
                "detour_edge": detour_edge,
                "socket_a": pair_sockets[0],
                "socket_b": pair_sockets[1],
            }
        )

    gate_visuals = []
    gate_logic = []
    for index, gate in enumerate(gate_specs, start=1):
        effect_name = "force-wall-coop" if gate["owner"] == "shared" else "force-wall-stable"
        asset_id = f"effects/{effect_name}"
        gate_visuals.append(
            factory.tile_object(
                f"gate_visual_{index:02d}_{gate['pair_id']}",
                "ForceFieldWall",
                gate["x"],
                gate["y"],
                128,
                190,
                gids[asset_id],
                props(
                    prop("active_preview", gate["active_preview"]),
                    prop("asset_id", asset_id),
                    prop("blocked_edge_id", gate["blocked_edge"]),
                    prop("detour_edge_id", gate["detour_edge"]),
                    prop("gate_pair_id", gate["pair_id"]),
                    prop("requires_active_towers", True),
                    prop("socket_a", gate["socket_a"]["id"], "object"),
                    prop("socket_b", gate["socket_b"]["id"], "object"),
                    prop("wear_seconds", 18.0, "float"),
                ),
                visible=False,
            )
        )
        gate_logic.append(
            factory.polyline(
                f"gate_logic_{index:02d}_{gate['pair_id']}",
                "GateHint",
                [(gate["socket_a"]["x"], gate["socket_a"]["y"]), (gate["socket_b"]["x"], gate["socket_b"]["y"])],
                props(
                    prop("blocked_edge_id", gate["blocked_edge"]),
                    prop("detour_edge_id", gate["detour_edge"]),
                    prop("gate_pair_id", gate["pair_id"]),
                    prop("requires_active_towers", True),
                    prop("socket_a", gate["socket_a"]["id"], "object"),
                    prop("socket_b", gate["socket_b"]["id"], "object"),
                ),
            )
        )

    # The core retains its compact 144px footprint while the editable physical-
    # tag targets use a larger 208px footprint to fill their road-corner pads.
    mega_tower = [
        factory.tile_object(
            "central_core_square_base",
            "CoreVisual",
            880,
            480,
            144,
            144,
            gids["objectives/target-shared-active"],
            props(
                prop("asset_id", "objectives/target-shared-active"),
                prop("footprint_px", 144),
                prop("gameplay_node_ref", node_by_name["mega_tower_entry"]["id"], "object"),
                prop("objective_role", "central_core"),
                prop("state_active_asset", "objectives/target-shared-active"),
                prop("state_inactive_asset", "objectives/target-shared-inactive"),
                prop("state_stressed_asset", "objectives/target-shared-stressed"),
            ),
        ),
        factory.tile_object(
            "central_core_photon_crown",
            "MegaTowerCrown",
            880,
            480,
            96,
            96,
            gids["structures/photon-detonator-upgraded-l5"],
            props(
                prop("asset_id", "structures/photon-detonator-upgraded-l5"),
                prop("objective_structure", True),
                prop("visual_only", True),
            ),
        ),
    ]

    zones = [
        factory.rectangle("camera_bounds", "CameraBounds", 0, 0, MAP_W, MAP_H, props(prop("clamp_camera", True))),
        factory.rectangle(
            "central_core_arrival_clearance",
            "CoreArrivalClearance",
            752,
            352,
            256,
            256,
            props(
                prop("allow_enemy_traversal", True),
                prop("arrival_lane_count", 2),
                prop("blocks_tower_placement", True),
                prop("keep_clear", True),
                prop("purpose", "preserve_open_enemy_arrival_space_after_maze"),
            ),
        ),
        factory.rectangle(
            "mega_tower_damage_zone",
            "CoreZone",
            808,
            408,
            144,
            144,
            props(
                prop("allow_multiple_attackers", True),
                prop("core_hp", 10000),
                prop("damage_mode", "continuous_contact_drain"),
                prop("loss_on_breach", True),
            ),
        ),
    ]

    camera_registration = [
        factory.rectangle(
            "camera_playfield_bounds",
            "CameraRegistrationBounds",
            0,
            0,
            MAP_W,
            MAP_H,
            props(
                prop("coordinate_space", "corrected_camera"),
                prop("preserve_live_camera", True),
                prop("requires_calibration", True),
            ),
        ),
        factory.point("camera_anchor_nw", "CameraAnchor", 0, 0, props(prop("normalized_u", 0.0, "float"), prop("normalized_v", 0.0, "float"))),
        factory.point("camera_anchor_ne", "CameraAnchor", MAP_W, 0, props(prop("normalized_u", 1.0, "float"), prop("normalized_v", 0.0, "float"))),
        factory.point("camera_anchor_sw", "CameraAnchor", 0, MAP_H, props(prop("normalized_u", 0.0, "float"), prop("normalized_v", 1.0, "float"))),
        factory.point("camera_anchor_se", "CameraAnchor", MAP_W, MAP_H, props(prop("normalized_u", 1.0, "float"), prop("normalized_v", 1.0, "float"))),
    ]

    staging_zone = factory.rectangle(
        "activation_staging_right_grey",
        "ActivationStagingZone",
        1520,
        320,
        176,
        320,
        props(
            prop("allowed_atom_tags", "100,101,102,103"),
            prop("purpose", "physical_and_virtual_activation_unit_start"),
            prop("surface", "right_grey_area"),
        ),
    )
    activator_starts = [staging_zone]
    for atom_tag_id, owner, x, y in (
        (100, "green", 1560, 400),
        (101, "green", 1656, 400),
        (102, "purple", 1560, 560),
        (103, "purple", 1656, 560),
    ):
        activator_starts.append(
            factory.point(
                f"activator_{atom_tag_id}_start",
                "ActivatorStart",
                x,
                y,
                props(
                    prop("allowed_tower_types", "machine_gun,flamethrower,mortar"),
                    prop("atom_tag_id", atom_tag_id),
                    prop("owner", owner),
                    prop("staging_zone", "activation_staging_right_grey"),
                ),
            )
        )

    layers = [
        {
            "id": 1,
            "image": "../../concept-art/z-pixel-variants/gameplay-z-pixel-simplified-tag-targets-v1.png",
            "locked": True,
            "name": "01 Style Reference (hidden)",
            "opacity": 0.2,
            "type": "imagelayer",
            "visible": False,
            "x": 0,
            "y": 0,
        },
        object_layer(2, "02 Ground Modules", ground, locked=True),
        object_layer(3, "03 Road Modules", roads, draworder="index"),
        object_layer(4, "04 Industrial Props (intentionally sparse)", []),
        object_layer(5, "05 Player Reach Zones (hidden)", reach_zones, visible=False, color="#73e6a6"),
        object_layer(6, "06 Enemy Path Graph (hidden)", edges, visible=False, color="#29c5ff"),
        object_layer(7, "07 Path Nodes (hidden)", nodes, visible=False, color="#46ddff"),
        object_layer(8, "08 Gameplay Zones (hidden)", zones, visible=False, color="#e05d62"),
        object_layer(9, "09 Square Placement Spots (16)", sockets),
        object_layer(10, "10 Force Field Walls", gate_visuals, visible=False),
        object_layer(11, "11 Force Field Gate Logic (hidden)", gate_logic, visible=False, color="#66e9ff"),
        object_layer(12, "12 Central Square Core", mega_tower),
        object_layer(13, "13 Camera Registration (hidden)", camera_registration, visible=False, color="#f2e85c"),
        object_layer(14, "14 Activation Unit Staging", activator_starts, color="#ff9f43"),
    ]

    map_data = {
        "backgroundcolor": "#111418",
        "class": "PhotonLevel",
        "compressionlevel": -1,
        "height": MAP_H // TILE,
        "infinite": False,
        "layers": layers,
        "nextlayerid": 15,
        "nextobjectid": factory.next_id,
        "orientation": "orthogonal",
        "properties": props(
            prop("active_gate_preview_count", 4),
            prop("activation_unit_count", 4),
            prop("activation_unit_tag_ids", "100,101,102,103"),
            prop("camera_overlay_preserves_video", True),
            prop("core_access_open", True),
            prop("core_access_plaza_px", 256),
            prop("core_arrival_clearance_px", 256),
            prop("core_arrival_lane_count", 2),
            prop("core_damage_zone_px", 144),
            prop("core_visual_footprint_px", 144),
            prop("direct_route_count", 2),
            prop("force_field_candidate_count", 8),
            prop("fixed_aruco_max", 55),
            prop("fixed_aruco_min", 40),
            prop("level_id", "z_pixel_first_map_01"),
            prop("layout_revision", 1),
            prop("left_mid_passthrough", True),
            prop("max_active_enemies", 1000),
            prop("max_structures", 8),
            prop("route_direction", "four_long_left_to_right_180_returns_plus_two_direct_left_to_center"),
            prop("seam_safe_roads", True),
            prop("socket_count", 16),
            prop("vertical_passthrough_count", 2),
            prop("wave_file", WAVE_PATH.name, "file"),
        ),
        "renderorder": "right-down",
        "tiledversion": "1.12.2",
        "tileheight": TILE,
        "tilesets": tilesets,
        "tilewidth": TILE,
        "type": "map",
        "version": "1.11",
        "width": MAP_W // TILE,
    }

    diagnostics = validate_map(map_data, nodes, edges, sockets, gate_specs, roads, ground)
    return map_data, diagnostics


def validate_map(
    map_data: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    sockets: list[dict[str, Any]],
    gates: list[dict[str, Any]],
    roads: list[dict[str, Any]],
    ground: list[dict[str, Any]],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    properties = {item["name"]: item["value"] for item in map_data["properties"]}
    if len(sockets) != 16:
        errors.append(f"expected 16 sockets, got {len(sockets)}")
    if len(gates) != 8:
        errors.append(f"expected 8 gate candidates, got {len(gates)}")
    if sum(bool(gate["active_preview"]) for gate in gates) != 4:
        errors.append("expected four active-preview force walls")
    if sum(2 for gate in gates if gate["active_preview"]) != properties["max_structures"]:
        errors.append("active gate endpoints must consume exactly the eight-structure preview cap")
    owner_counts = defaultdict(int)
    for socket in sockets:
        owner = next(item["value"] for item in socket["properties"] if item["name"] == "owner")
        owner_counts[owner] += 1
    if dict(owner_counts) != {"purple": 6, "shared": 4, "green": 6}:
        errors.append(f"socket ownership split is {dict(owner_counts)}")
    if len(roads) < 48:
        errors.append(f"expected at least 48 road modules, got {len(roads)}")
    if len(ground) != 18:
        errors.append(f"expected 18 ground modules, got {len(ground)}")
    if properties["max_active_enemies"] != 1000 or properties["max_structures"] != 8:
        errors.append("map caps do not match 1000 enemies / 8 structures")
    if not properties.get("seam_safe_roads"):
        errors.append("map must use seam-safe open-port road derivatives")
    if not any(reference["source"].endswith("z-pixel-v2-seam-safe-roads.tsj") for reference in map_data["tilesets"]):
        errors.append("seam-safe road tileset is not attached")

    center_ports: dict[tuple[int, int], set[str]] = {}
    for road in roads:
        road_properties = {item["name"]: item["value"] for item in road["properties"]}
        position = (road_properties.get("grid_column"), road_properties.get("grid_row"))
        if position in {(5, 2), (5, 3)}:
            center_ports[position] = set(road_properties["ports"].split(","))
    if center_ports:
        errors.append(f"platform-bearing center road cells were not removed: {center_ports}")
    center_neighbor_assets = {
        (road_properties.get("grid_column"), road_properties.get("grid_row")): road_properties.get("asset_id")
        for road in roads
        for road_properties in ({item["name"]: item["value"] for item in road["properties"]},)
        if (road_properties.get("grid_column"), road_properties.get("grid_row")) in {(4, 3), (6, 3)}
    }
    expected_center_neighbors = {
        (4, 3): "roads/straight-horizontal",
        (6, 3): "roads/straight-horizontal",
    }
    if center_neighbor_assets != expected_center_neighbors:
        errors.append(f"center plaza neighbors are not standard open lanes: {center_neighbor_assets}")

    core_access_plaza = next(
        (
            road
            for road in roads
            if {item["name"]: item["value"] for item in road["properties"]}.get("asset_id")
            == "roads/core-access-plaza"
        ),
        None,
    )
    core_access_properties = {
        item["name"]: item["value"]
        for item in (core_access_plaza or {}).get("properties", [])
    }
    if (
        core_access_plaza is None
        or core_access_plaza["x"] != 880
        or core_access_plaza["y"] != 480
        or core_access_plaza["width"] != 256
        or core_access_plaza["height"] != 256
        or core_access_properties.get("edge_style") != "recessed_industrial_curb"
        or core_access_properties.get("lane_openings") != "W:top,bottom;E:top,bottom"
        or core_access_properties.get("corner_vertical_stubs") is not False
        or core_access_properties.get("curb_end_profile") != "32px-stepped-taper"
    ):
        errors.append("central core plaza must have four openings and tapered curb ends without vertical caps")

    # Tiled tile objects in this pack use center alignment. The enemy graph is
    # therefore authored against each road object's x/y anchor, not the
    # bottom-left image corner used by a default orthogonal tileset. Sample all
    # path edges against the modular road ports so a future renderer or map edit
    # cannot silently reproduce the half-module drift fixed in Laser Tag Z.
    port_vectors = {"N": (0.0, -1.0), "E": (1.0, 0.0), "S": (0.0, 1.0), "W": (-1.0, 0.0)}
    road_centerlines: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for road in roads:
        road_properties = {item["name"]: item["value"] for item in road.get("properties", [])}
        if road_properties.get("grid_column") is None or road_properties.get("grid_row") is None:
            continue
        center = (float(road["x"]), float(road["y"]))
        for port_name in str(road_properties.get("ports", "")).split(","):
            vector = port_vectors.get(port_name)
            if vector:
                road_centerlines.append(
                    (center, (center[0] + vector[0] * ROAD_STEP / 2, center[1] + vector[1] * ROAD_STEP / 2))
                )
    if core_access_plaza:
        plaza_x, plaza_y = float(core_access_plaza["x"]), float(core_access_plaza["y"])
        plaza_half = float(core_access_plaza["width"]) / 2.0
        road_centerlines.extend(
            [
                ((plaza_x - plaza_half, plaza_y - ROAD_STEP / 2), (plaza_x + plaza_half, plaza_y - ROAD_STEP / 2)),
                ((plaza_x - plaza_half, plaza_y + ROAD_STEP / 2), (plaza_x + plaza_half, plaza_y + ROAD_STEP / 2)),
                ((plaza_x, plaza_y - ROAD_STEP / 2), (plaza_x, plaza_y + ROAD_STEP / 2)),
            ]
        )

    path_deviations: list[tuple[float, tuple[float, float], str]] = []
    for edge in edges:
        absolute = [
            (float(edge["x"]) + float(point["x"]), float(edge["y"]) + float(point["y"]))
            for point in edge.get("polyline", [])
        ]
        for start, end in zip(absolute, absolute[1:]):
            sample_count = max(1, math.ceil(math.dist(start, end) / 16.0))
            for index in range(sample_count + 1):
                amount = index / sample_count
                sample = (start[0] + (end[0] - start[0]) * amount, start[1] + (end[1] - start[1]) * amount)
                deviation = min(
                    (point_segment_distance(sample, line_start, line_end) for line_start, line_end in road_centerlines),
                    default=math.inf,
                )
                path_deviations.append((deviation, sample, edge["name"]))
    max_path_deviation = max(path_deviations, default=(math.inf, (0.0, 0.0), "none"))
    if max_path_deviation[0] > 8.0:
        errors.append(
            "enemy path leaves the modular road centerline by "
            f"{max_path_deviation[0]:.1f}px at {max_path_deviation[1]} on {max_path_deviation[2]}"
        )

    node_properties = {
        node["name"]: {item["name"]: item["value"] for item in node["properties"]}
        for node in nodes
    }
    arrival_names = {
        name
        for name, values in node_properties.items()
        if values.get("node_kind") == "arrival"
    }
    if arrival_names != {"core_arrival_top", "core_arrival_bottom"}:
        errors.append(f"expected two explicit core arrival nodes, got {sorted(arrival_names)}")

    layer_by_name = {layer["name"]: layer for layer in map_data["layers"]}
    socket_markers = sorted(
        next((item["value"] for item in socket.get("properties", []) if item["name"] == "aruco_id"), None)
        for socket in sockets
    )
    if socket_markers != list(range(40, 56)):
        errors.append(f"tower sockets must use consecutive ArUco IDs 40-55, got {socket_markers}")
    if any(socket["width"] != SOCKET_SIZE or socket["height"] != SOCKET_SIZE for socket in sockets):
        errors.append(f"all tower sockets must use the default {SOCKET_SIZE}x{SOCKET_SIZE} footprint")

    staging_objects = layer_by_name["14 Activation Unit Staging"]["objects"]
    activator_ids = sorted(
        next((item["value"] for item in obj.get("properties", []) if item["name"] == "atom_tag_id"), None)
        for obj in staging_objects
        if obj.get("type") == "ActivatorStart"
    )
    if activator_ids != [100, 101, 102, 103]:
        errors.append(f"activation staging must use Atom tags 100-103, got {activator_ids}")
    if any(not (1520 <= obj["x"] <= MAP_W and 320 <= obj["y"] <= 640) for obj in staging_objects if obj.get("type") == "ActivatorStart"):
        errors.append("all activation-unit starts must remain inside the right grey staging area")

    camera_objects = layer_by_name["13 Camera Registration (hidden)"]["objects"]
    if len([obj for obj in camera_objects if obj.get("type") == "CameraAnchor"]) != 4:
        errors.append("camera registration requires four logical corner anchors")
    core_objects = layer_by_name["12 Central Square Core"]["objects"]
    core_base = next((item for item in core_objects if item["name"] == "central_core_square_base"), None)
    if core_base is None or core_base["width"] != 144 or core_base["height"] != 144:
        errors.append("central core must retain its authored 144x144 square footprint")

    gameplay_zones = layer_by_name["08 Gameplay Zones (hidden)"]["objects"]
    arrival_clearance = next((item for item in gameplay_zones if item["name"] == "central_core_arrival_clearance"), None)
    if arrival_clearance is None or arrival_clearance["width"] < 256 or arrival_clearance["height"] < 256:
        errors.append("central core requires a 256x256 enemy-arrival clearance zone")
    elif any(
        socket["x"] - socket["width"] / 2 < arrival_clearance["x"] + arrival_clearance["width"]
        and socket["x"] + socket["width"] / 2 > arrival_clearance["x"]
        and socket["y"] - socket["height"] / 2 < arrival_clearance["y"] + arrival_clearance["height"]
        and socket["y"] + socket["height"] / 2 > arrival_clearance["y"]
        for socket in sockets
    ):
        errors.append("a placement target overlaps the protected central arrival clearance")

    damage_zone = next((item for item in gameplay_zones if item["name"] == "mega_tower_damage_zone"), None)
    damage_properties = {
        item["name"]: item["value"]
        for item in (damage_zone or {}).get("properties", [])
    }
    if (
        damage_zone is None
        or damage_zone["width"] != 144
        or damage_zone["height"] != 144
        or not damage_properties.get("allow_multiple_attackers")
    ):
        errors.append("core damage zone must match the 144x144 square and allow multiple attackers")

    id_to_name = {node["id"]: node["name"] for node in nodes}
    adjacency: dict[str, set[str]] = defaultdict(set)
    edge_ids = set()
    for edge in edges:
        edge_props = {item["name"]: item["value"] for item in edge["properties"]}
        edge_ids.add(edge_props["edge_id"])
        adjacency[id_to_name[edge_props["from_node"]]].add(id_to_name[edge_props["to_node"]])
    for gate in gates:
        if gate["blocked_edge"] not in edge_ids or gate["detour_edge"] not in edge_ids:
            errors.append(f"gate {gate['pair_id']} references a missing edge")

    core = "mega_tower_entry"
    for spawn in ("spawn_top_outer", "spawn_top_inner", "spawn_bottom_inner", "spawn_bottom_outer"):
        queue = deque([spawn])
        seen = {spawn}
        while queue:
            current = queue.popleft()
            for neighbor in adjacency[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        if core not in seen:
            errors.append(f"{spawn} cannot reach the mega tower")

    if not errors:
        warnings.append("Gate previews are illustrative; runtime visibility still requires both endpoint tags to be active")
    return {
        "schema_version": 1,
        "level_id": properties["level_id"],
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "ground_modules": len(ground),
            "road_modules": len(roads),
            "path_nodes": len(nodes),
            "path_edges": len(edges),
            "placement_spots": len(sockets),
            "gate_candidates": len(gates),
            "active_gate_previews": sum(bool(gate["active_preview"]) for gate in gates),
        },
        "max_path_road_deviation_px": round(max_path_deviation[0], 2),
    }


def build_waves() -> dict[str, Any]:
    waves = []
    for wave in range(1, 13):
        waves.append(
            {
                "wave": wave,
                "groups": [
                    {
                        "enemy": ("grunt", "runner", "breaker", "brute")[(wave - 1) % 4],
                        "count": min(760, 70 + wave * 48),
                        "duration_s": max(18, 38 - wave),
                        "lane_weights": {
                            "top_outer": 1,
                            "top_inner": 1,
                            "bottom_inner": 1,
                            "bottom_outer": 1,
                        },
                    }
                ],
            }
        )
    return {
        "schema_version": 1,
        "level_id": "z_pixel_first_map_01",
        "max_active_enemies": 1000,
        "overflow_policy": "pressure_bank",
        "gate_routing": "recalculate_at_crossover_when_edge_blocked",
        "waves": waves,
    }


def main() -> None:
    map_data, diagnostics = build_map()
    if diagnostics["errors"]:
        write_json(REPORT_PATH, diagnostics)
        raise SystemExit(json.dumps(diagnostics, indent=2))
    write_json(MAP_PATH, map_data)
    write_json(WAVE_PATH, build_waves())
    write_json(REPORT_PATH, diagnostics)
    print(json.dumps({"map": os.fspath(MAP_PATH), "waves": os.fspath(WAVE_PATH), "validation": diagnostics}, indent=2))


if __name__ == "__main__":
    main()
