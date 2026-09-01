"""Regression checks for the Laser Tag Z vertical slice."""

from __future__ import annotations

import hashlib
import json
import math
import copy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from itertools import combinations, permutations
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "sandboxes/gamemaster/prototypes/laser-tag-z"
sys.path.insert(0, str(PROTOTYPE))

from photon_defence import DefenseEngine, SettingsStore  # noqa: E402
from photon_defence.engine import (  # noqa: E402
    COLLISION_PADDING,
    CORE_BASIN_HALF_SIZE,
    CORE_OCTAGON_PLANES,
    ENEMY_STATS,
    FLAMETHROWER_MUZZLE_OFFSET,
    FLOW_SPAWN_OFFSETS,
    PARTICLE_MAX_SPEED_SCALE,
    ORC_TOWER_MELEE_REACH,
    RING_MAX_TURRETS,
    RING_MIN_TURRETS,
    TOWER_ATTACK_RADIUS,
    TOWER_POD_RADIUS,
    _core_octagon_face,
    _is_valid_core_ring,
    _segment_circle_overlap_fraction,
)
from photon_defence.level_layout import (  # noqa: E402
    SocketLayoutError,
    apply_socket_layout,
    layout_revision,
    socket_records,
)


MAP_PATH = ROOT / "assets/tiled/levels/z-pixel-first-map.tmj"
WAVES_PATH = ROOT / "assets/tiled/levels/z-pixel-first-map.waves.json"


class LaserTagZEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = DefenseEngine(MAP_PATH, WAVES_PATH)

    @staticmethod
    def _field_marker_pairs(engine):
        return {
            tuple(sorted((
                engine.level.sockets[field["from_socket"]]["aruco_id"],
                engine.level.sockets[field["to_socket"]]["aruco_id"],
            )))
            for field in engine.force_fields.values()
        }

    def test_level_has_exact_fixed_marker_range(self):
        self.assertEqual(sorted(self.engine.level.socket_by_marker), list(range(40, 56)))
        self.assertEqual(len(self.engine.level.sockets), 16)
        self.assertEqual(self.engine.level.aruco_code_footprint_px, 77.0)
        self.assertEqual(self.engine.level.core_aruco_code_footprint_px, 116.0)
        self.assertEqual(self.engine.level.force_field_marker_clearance_px, 20.0)

    def test_all_socket_visuals_use_the_large_default_footprint(self):
        level = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        sockets = socket_records(level)
        self.assertEqual({item["size"] for item in sockets}, {208.0})
        self.assertGreaterEqual(layout_revision(level), 8)

    def test_socket_visual_size_never_changes_force_field_line_of_sight(self):
        marker = self.engine.level.socket_by_marker
        clear_first = self.engine.level.sockets[marker[42]]
        clear_second = self.engine.level.sockets[marker[44]]
        blocked_pairs = ((48, 53, 49), (53, 54, 51))
        original_sizes = {
            socket_id: socket["size"]
            for socket_id, socket in self.engine.level.sockets.items()
        }
        try:
            for size in (1.0, 96.0, 208.0, 320.0, 4096.0):
                for socket in self.engine.level.sockets.values():
                    socket["size"] = size
                self.assertEqual(
                    self.engine._line_blockers(clear_first, clear_second), []
                )
                for first_marker, second_marker, blocker_marker in blocked_pairs:
                    obstruction = self.engine._line_obstructions(
                        self.engine.level.sockets[marker[first_marker]],
                        self.engine.level.sockets[marker[second_marker]],
                    )
                    self.assertEqual(
                        obstruction["blocker_markers"], [blocker_marker]
                    )
        finally:
            for socket_id, size in original_sizes.items():
                self.engine.level.sockets[socket_id]["size"] = size

    def test_current_run_rejects_both_fields_over_empty_aruco_positions(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        current_run = (41, 53, 54, 52, 45, 42, 43, 48)
        original_evaluate = self.engine._evaluate_ring_topology
        self.engine._evaluate_ring_topology = lambda: None
        try:
            for marker_id in current_run:
                self.engine.place(100, marker[marker_id], source="virtual")
        finally:
            self.engine._evaluate_ring_topology = original_evaluate

        self.engine._evaluate_ring_topology()
        state = self.engine.snapshot()
        by_markers = {
            tuple(sorted((edge["from_marker"], edge["to_marker"]))): edge
            for edge in state["connections"]
        }
        placement = by_markers[(53, 54)]
        self.assertFalse(placement["exists"])
        self.assertEqual(placement["attempt_state"], "pending_empty_socket")
        self.assertEqual(placement["blocker_markers"], [51])
        self.assertFalse(placement["visible"])
        self.assertFalse(placement["collidable"])

        ring_edge = by_markers[(48, 53)]
        self.assertFalse(ring_edge["exists"])
        self.assertEqual(ring_edge["blocker_markers"], [49])
        self.assertFalse(ring_edge["visible"])
        self.assertFalse(state["ring_status"]["completed"])
        evaluation = state["ring_status"]["last_evaluation"]
        self.assertIn("blocked_line_of_sight", evaluation["rejection_reasons"])
        self.assertEqual(
            {
                marker_id
                for edge in evaluation["blocked_edges"]
                for marker_id in edge["blocker_markers"]
            },
            {49},
        )

    def test_current_run_rejects_43_to_52_over_protected_core_marker(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        current_run = (41, 53, 43, 52, 54, 46, 45, 42)
        original_evaluate = self.engine._evaluate_ring_topology
        self.engine._evaluate_ring_topology = lambda: None
        try:
            for marker_id in current_run:
                self.engine.place(100, marker[marker_id], source="virtual")
        finally:
            self.engine._evaluate_ring_topology = original_evaluate

        self.engine._evaluate_ring_topology()
        state = self.engine.snapshot()
        connection = next(
            edge for edge in state["connections"]
            if {edge["from_marker"], edge["to_marker"]} == {43, 52}
        )
        self.assertFalse(connection["exists"])
        self.assertEqual(connection["attempt_state"], "pending_core_marker")
        self.assertEqual(connection["blocker_ids"], ["protected_marker:38"])
        self.assertEqual(connection["blocker_markers"], [38])
        self.assertFalse(connection["visible"])
        self.assertFalse(connection["collidable"])
        self.assertIsNone(self.engine._field_between(marker[43], marker[52]))
        self.assertTrue(state["ring_status"]["completed"])

    def test_reconciliation_retires_legacy_field_crossing_core_marker(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        self.engine.place(100, marker[43], source="virtual")
        self.engine.place(101, marker[52], source="virtual")
        legacy = self.engine._create_force_field(
            marker[43], marker[52], check_line_of_sight=False
        )
        self.assertIsNotNone(legacy)

        self.engine._reconcile_connections(reason="test_legacy_core_crossing")

        self.assertIsNone(self.engine._field_between(marker[43], marker[52]))
        attempt = self.engine.placement_link_attempts[0]
        self.assertEqual(attempt["status"], "pending_core_marker")
        self.assertEqual(attempt["blocker_markers"], [38])
        rejection = next(
            event for event in reversed(self.engine.events)
            if event["kind"] == "force_field_rejected"
        )
        self.assertEqual(rejection["field_id"], legacy["field_id"])
        self.assertEqual(rejection["blocker_markers"], [38])

    def test_existing_field_suspends_and_resumes_with_dynamic_socket_blocker(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        for marker_id in (53, 51, 54):
            self.engine.place(100, marker[marker_id], source="virtual")
        attempt = self.engine._attempt_placement_link(
            marker[53], marker[54], source="virtual"
        )
        field = self.engine._field_between(marker[53], marker[54])
        self.assertIsNotNone(field)
        self.assertEqual(attempt["status"], "established")
        field["hits"] = 7

        self.engine.placements[marker[51]]["destroyed"] = True
        self.engine._reconcile_connections(reason="test_socket_emptied")
        blocked_state = self.engine.snapshot()
        blocked = next(
            edge for edge in blocked_state["connections"]
            if {edge["from_marker"], edge["to_marker"]} == {53, 54}
        )
        self.assertTrue(blocked["exists"])
        self.assertTrue(blocked["occluded"])
        self.assertEqual(blocked["state"], "occluded")
        self.assertEqual(blocked["blocker_markers"], [51])
        self.assertFalse(blocked["visible"])
        self.assertFalse(blocked["collidable"])
        self.assertEqual(field["hits"], 7)

        self.engine.place(100, marker[51], source="virtual")
        resumed_state = self.engine.snapshot()
        resumed = next(
            edge for edge in resumed_state["connections"]
            if {edge["from_marker"], edge["to_marker"]} == {53, 54}
        )
        self.assertIs(self.engine._field_between(marker[53], marker[54]), field)
        self.assertFalse(resumed["occluded"])
        self.assertEqual(resumed["state"], "active")
        self.assertEqual(resumed["blocker_markers"], [])
        self.assertTrue(resumed["visible"])
        self.assertTrue(resumed["collidable"])
        self.assertEqual(field["hits"], 7)

    def test_all_ring_activation_permutations_apply_identical_line_safety(self):
        marker = self.engine.level.socket_by_marker
        ring_markers = (41, 40, 43, 42, 44, 45, 54, 50)
        self.assertEqual(self.engine.level.force_field_blockers, ())
        self.engine._evaluate_ring_topology = lambda: None
        for order in permutations(ring_markers):
            self.engine.reset()
            self.engine.virtual_play = True
            for marker_id in order:
                self.engine.place(100, marker[marker_id], source="virtual")
            self.assertEqual(len(self.engine.placement_link_attempts), 7)
            for attempt in self.engine.placement_link_attempts:
                first = self.engine.placements[attempt["from_socket"]]
                second = self.engine.placements[attempt["to_socket"]]
                obstruction = self.engine._line_obstructions(first, second)
                expected_status = (
                    "pending_core_marker"
                    if 38 in obstruction["blocker_markers"]
                    else "pending_empty_socket"
                    if obstruction["blocker_socket_ids"]
                    else "established"
                )
                self.assertEqual(attempt["status"], expected_status)
                self.assertEqual(
                    attempt["blocker_markers"], obstruction["blocker_markers"]
                )
                field = self.engine._field_between(
                    attempt["from_socket"], attempt["to_socket"]
                )
                self.assertEqual(field is not None, expected_status == "established")

    def test_partial_ring_preview_is_spatial_and_order_independent(self):
        marker_sets = (
            (41, 40, 43, 42, 44, 45),
            (45, 44, 42, 43, 40, 41),
            (42, 45, 40, 44, 41, 43),
        )
        previews = []
        for sequence in marker_sets:
            engine = DefenseEngine(MAP_PATH, WAVES_PATH)
            engine.set_virtual_play(True)
            marker = engine.level.socket_by_marker
            for marker_id in sequence:
                engine.place(100, marker[marker_id], source="virtual")
            state = engine.snapshot()
            preview = state["ring_preview"]
            self.assertFalse(preview["complete"])
            self.assertEqual(len(preview["edges"]), len(sequence) - 1)
            self.assertTrue(all(
                not edge["blocker_ids"] and edge["visible"]
                for edge in preview["edges"]
            ))
            preview_ids = {edge["field_id"] for edge in preview["edges"]}
            self.assertTrue(all(
                not connection["collidable"]
                for connection in state["connections"]
                if connection["field_id"] in preview_ids
                and connection["provisional"]
            ))
            previews.append((
                tuple(preview["markers"]),
                tuple(preview["closing_markers"]),
                frozenset(
                    tuple(sorted((edge["from_marker"], edge["to_marker"])))
                    for edge in preview["edges"]
                ),
            ))
        self.assertEqual(previews, [previews[0]] * len(previews))

    def test_physical_and_virtual_sources_reconcile_the_same_links(self):
        sequence = (41, 48, 49, 53, 50)
        observed = []
        for source in ("virtual", "physical"):
            engine = DefenseEngine(MAP_PATH, WAVES_PATH)
            if source == "virtual":
                engine.set_virtual_play(True)
            marker = engine.level.socket_by_marker
            for marker_id in sequence:
                engine.place(100, marker[marker_id], source=source)
            state = engine.snapshot()
            self.assertEqual(
                [link["status"] for link in state["placement_links"]],
                ["established"] * (len(sequence) - 1),
            )
            observed.append({
                link["field_id"] for link in state["placement_links"]
            })
        self.assertEqual(observed[0], observed[1])

    def test_ring_adjacency_is_authored_and_symmetric(self):
        level = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        socket_layer = next(
            layer for layer in level["layers"]
            if layer["name"] == "09 Square Placement Spots (16)"
        )
        adjacency = {}
        for obj in socket_layer["objects"]:
            props = {
                item["name"]: item.get("value")
                for item in obj.get("properties", [])
            }
            marker = int(props["aruco_id"])
            adjacency[marker] = {
                int(value) for value in str(props["ring_neighbors"]).split(",")
                if value
            }
        self.assertEqual(adjacency[51], set())
        self.assertEqual(adjacency[52], set())
        for marker, neighbors in adjacency.items():
            for neighbor in neighbors:
                self.assertIn(marker, adjacency[neighbor])
        cycles = [
            {self.engine.level.sockets[socket_id]["aruco_id"] for socket_id in cycle}
            for cycle in self.engine.level.ring_cycles
        ]
        self.assertIn({41, 40, 43, 42, 44, 45, 54, 50}, cycles)
        self.assertIn({53, 49, 48, 50, 41, 40, 44, 45, 47, 55, 46}, cycles)
        self.assertEqual(len(cycles), 3)

    def test_authored_ring_edges_are_canonical_and_unique(self):
        marker_pairs = [
            tuple(sorted((
                self.engine.level.sockets[first]["aruco_id"],
                self.engine.level.sockets[second]["aruco_id"],
            )))
            for first, second in self.engine.level.ring_edges
        ]
        self.assertEqual(len(marker_pairs), 16)
        self.assertEqual(len(set(marker_pairs)), len(marker_pairs))
        self.assertEqual(
            set(marker_pairs),
            {
                (40, 41), (40, 43), (40, 44), (41, 50),
                (42, 43), (42, 44), (44, 45), (45, 47),
                (45, 54), (46, 53), (46, 55), (47, 55),
                (48, 49), (48, 50), (49, 53), (50, 54),
            },
        )
        self.assertTrue(
            all(first < second for first, second in self.engine.level.ring_edges)
        )

    def test_layout_edit_preserves_identity_and_updates_linked_gate(self):
        level = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        submitted = socket_records(level)
        submitted[0].update({"x": 672, "y": 184, "size": 240})
        updated = apply_socket_layout(level, submitted)
        records = socket_records(updated)
        self.assertEqual(records[0]["aruco_id"], 40)
        self.assertEqual(records[0]["owner"], "purple")
        self.assertEqual((records[0]["x"], records[0]["y"], records[0]["size"]), (672.0, 184.0, 240.0))
        self.assertEqual(layout_revision(updated), layout_revision(level) + 1)
        gate_layer = next(layer for layer in updated["layers"] if layer["name"] == "10 Force Field Walls")
        first_gate = gate_layer["objects"][0]
        second = records[1]
        self.assertEqual(
            (first_gate["x"], first_gate["y"]),
            ((records[0]["x"] + second["x"]) / 2, (records[0]["y"] + second["y"]) / 2),
        )
        self.assertGreater(first_gate["height"], 150)

    def test_layout_edit_rejects_incomplete_or_out_of_range_geometry(self):
        level = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        submitted = socket_records(level)
        with self.assertRaises(SocketLayoutError):
            apply_socket_layout(level, submitted[:-1])
        submitted[0]["size"] = 400
        with self.assertRaises(SocketLayoutError):
            apply_socket_layout(level, submitted)

    def test_tiled_center_alignment_is_used_by_map_and_gameplay(self):
        level = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        expected_anchors = {
            "objectives/target-purple-active": (152.5 / 320, 137.0 / 320),
            "objectives/target-purple-inactive": (160.0 / 320, 146.0 / 320),
            "objectives/target-green-active": (156.5 / 320, 135.0 / 320),
            "objectives/target-green-inactive": (156.0 / 320, 131.5 / 320),
            "objectives/target-shared-inactive": (157.5 / 320, 128.5 / 320),
            "objectives/target-shared-active": (149.0 / 320, 140.0 / 320),
        }
        alignments = []
        for reference in level["tilesets"]:
            tileset_path = (MAP_PATH.parent / reference["source"]).resolve()
            alignments.append(json.loads(tileset_path.read_text(encoding="utf-8"))["objectalignment"])
        self.assertEqual(set(alignments), {"center"})

        socket_layer = next(layer for layer in level["layers"] if layer["name"] == "09 Square Placement Spots (16)")
        for obj in socket_layer["objects"]:
            properties = {
                item["name"]: item["value"] for item in obj["properties"]
            }
            marker = properties["aruco_id"]
            socket = self.engine.level.sockets[self.engine.level.socket_by_marker[marker]]
            self.assertEqual((socket["x"], socket["y"]), (float(obj["x"]), float(obj["y"])))
            self.assertEqual(
                (properties["aruco_anchor_u"], properties["aruco_anchor_v"]),
                expected_anchors[properties["asset_id"]],
            )

        core_layer = next(
            layer for layer in level["layers"]
            if layer["name"] == "12 Central Square Core"
        )
        core = next(
            obj for obj in core_layer["objects"]
            if obj["name"] == "central_core_square_base"
        )
        core_properties = {
            item["name"]: item["value"] for item in core["properties"]
        }
        self.assertEqual(
            (core_properties["aruco_anchor_u"], core_properties["aruco_anchor_v"]),
            expected_anchors[core_properties["asset_id"]],
        )

        game_html = (PROTOTYPE / "game.html").read_text(encoding="utf-8")
        renderer_js = (PROTOTYPE / "tower-defence-view.js").read_text(encoding="utf-8")
        self.assertIn('src="tower-defence-view.js"', game_html)
        self.assertIn("selected.source.objectalignment", renderer_js)
        self.assertIn("function arucoMarkerCenter", renderer_js)
        self.assertIn("const center = arucoMarkerCenter(socket)", renderer_js)
        self.assertIn("marker_x: markerCenter.x", renderer_js)
        self.assertIn("marker_y: markerCenter.y", renderer_js)
        self.assertIn("marker_size: socketMarkerVisualSize()", renderer_js)
        self.assertNotIn("socket.x+socket.width/2", renderer_js)

    def test_completed_core_ring_repaints_marker_above_runtime_effects(self):
        renderer_js = (PROTOTYPE / "tower-defence-view.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("function renderCoreMarkerOverlay", renderer_js)
        self.assertIn('["first_tag", "ring_ready"].includes(stage)', renderer_js)
        foreground = renderer_js.index(
            "drawCoreSequence(context, state, visualTime, true)"
        )
        marker = renderer_js.index("renderCoreMarkerOverlay(context, state)")
        self.assertLess(foreground, marker)

    def test_all_orc_walk_frames_are_distinct_and_animation_is_rendered(self):
        for enemy_type, group in (
            ("grunt", "enemies-light-orcs-v2"),
            ("runner", "enemies-light-orcs-v2"),
            ("breaker", "enemies-light-orcs-v2"),
            ("brute", "enemies-heavy-orcs-v2"),
        ):
            hashes = {
                hashlib.sha256(
                    (ROOT / "assets/game-art/sprites" / group / f"{enemy_type}-walk-{frame:02}.png").read_bytes()
                ).hexdigest()
                for frame in range(1, 5)
            }
            self.assertEqual(len(hashes), 4)

        renderer_js = (PROTOTYPE / "tower-defence-view.js").read_text(encoding="utf-8")
        self.assertIn("`enemy:${type}:${frame}`", renderer_js)
        self.assertIn("Math.floor(visualTime * 8", renderer_js)
        self.assertIn("Math.atan2(facingY, facingX) - Math.PI / 2", renderer_js)
        self.assertIn('enemy.enemy_type === "brute" ? 56 : 44', renderer_js)
        self.assertIn("requestAnimationFrame(gameRenderLoop)", renderer_js)
        self.assertIn("function renderCoreHealth", renderer_js)
        enemy_renderer = renderer_js.split("function drawEnemy", 1)[1].split(
            "function renderGame", 1
        )[0]
        self.assertNotIn("enemy.hp", enemy_renderer)
        self.assertNotIn("barWidth", enemy_renderer)

    def test_particle_orcs_spawn_across_a_lane_and_face_their_motion(self):
        weights = {"top_inner": 1.0}
        self.assertTrue(all(self.engine._spawn_enemy("brute", weights) for _ in range(3)))
        enemies = list(self.engine.enemies.values())
        self.assertEqual(
            {enemy["track"] for enemy in enemies}, set(FLOW_SPAWN_OFFSETS[:3])
        )
        self.assertEqual(len({round(enemy["y"], 3) for enemy in enemies}), 3)
        self.assertEqual(
            max(enemy["y"] for enemy in enemies) - min(enemy["y"] for enemy in enemies),
            max(FLOW_SPAWN_OFFSETS[:3]) - min(FLOW_SPAWN_OFFSETS[:3]),
        )
        self.assertAlmostEqual(ENEMY_STATS["brute"]["collision_radius"], 2.8)
        core_x = float(self.engine.level.core["x"])
        for spawned in enemies:
            self.assertAlmostEqual(
                spawned["path"][-1][0],
                core_x - CORE_BASIN_HALF_SIZE + spawned["collision_radius"] + 0.5,
            )
            self.assertAlmostEqual(spawned["path"][-1][1], 400.0 + spawned["track"])

        enemy = enemies[0]
        self.engine.enemies = {enemy["id"]: enemy}
        enemy["speed"] = 240.0
        enemy["vx"] = 240.0
        enemy["vy"] = 0.0
        self.assertAlmostEqual(enemy["facing_x"], 1.0)
        self.assertAlmostEqual(enemy["facing_y"], 0.0)
        diagonal_heading_seen = False
        for _ in range(80):
            self.engine.sim_time += 0.05
            self.engine._integrate_particles([enemy], 0.05, {enemy["id"]: 1.0})
            diagonal_heading_seen |= abs(enemy["facing_x"]) > 0.1 and abs(enemy["facing_y"]) > 0.1
            if enemy["facing_y"] > 0.9:
                break
        self.assertTrue(diagonal_heading_seen)
        self.assertGreater(enemy["facing_y"], 0.9)

        upward_engine = DefenseEngine(MAP_PATH, WAVES_PATH)
        self.assertTrue(upward_engine._spawn_enemy("grunt", {"bottom_inner": 1.0}))
        upward_enemy = upward_engine.enemies[max(upward_engine.enemies)]
        upward_enemy["speed"] = 240.0
        upward_enemy["vx"] = 240.0
        upward_enemy["vy"] = 0.0
        for _ in range(80):
            upward_engine.sim_time += 0.05
            upward_engine._integrate_particles(
                [upward_enemy], 0.05, {upward_enemy["id"]: 1.0}
            )
            if upward_enemy["facing_y"] < -0.9:
                break
        self.assertLess(upward_enemy["facing_y"], -0.9)

    def test_virtual_placements_use_fixed_atom_roles_and_fields_start_with_run(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        self.engine.place(100, marker[48], source="virtual", team="green")
        self.engine.place(101, marker[49], source="virtual", team="green")
        self.engine.place(102, marker[53], source="virtual", team="purple")
        self.engine.place(103, marker[50], source="virtual", team="purple")
        before = self.engine.snapshot()
        self.assertEqual(
            [(tower["atom_tag_id"], tower["tower_type"]) for tower in before["towers"]],
            [
                (100, "machine_gun"),
                (101, "flamethrower"),
                (102, "mortar"),
                (103, "tesla_coil"),
            ],
        )
        self.assertEqual(before["gates"], [])
        self.assertGreaterEqual(len(self.engine.force_fields), 2)
        self.assertEqual(
            len(before["force_field_visuals"]), len(self.engine.force_fields)
        )
        self.assertEqual(
            {field["visual_state"] for field in before["force_field_visuals"]},
            {"preview"},
        )
        self.assertTrue(all(field["visible"] for field in before["force_field_visuals"]))
        self.engine.start()
        self.engine.step(0.05)
        after = self.engine.snapshot()
        self.assertTrue(after["virtual_play"])
        self.assertEqual(len(after["towers"]), 4)
        self.assertGreaterEqual(len(after["gates"]), 2)
        self.assertEqual(len(after["force_field_visuals"]), len(after["gates"]))
        self.assertEqual(
            {field["visual_state"] for field in after["force_field_visuals"]},
            {"active"},
        )
        self.assertEqual(after["gates"][0]["capacity"], 50)
        self.assertGreaterEqual(after["active_enemies"], 1)

    def test_setup_previews_each_partial_ring_link_before_atomic_close(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        ring_markers = (41, 43, 42, 45, 54, 46, 52, 53)

        for index, marker_id in enumerate(ring_markers):
            self.engine.place(
                100 if index % 2 == 0 else 102,
                marker[marker_id],
                source="virtual",
            )
            state = self.engine.snapshot()
            self.assertEqual(state["connection_contract_version"], 2)
            self.assertEqual(state["gates"], [])
            self.assertFalse(state["ring_status"]["completed"])
            actual_connections = [
                connection for connection in state["connections"]
                if connection["exists"]
            ]
            provisional_connections = [
                connection for connection in state["connections"]
                if connection["provisional"]
            ]
            self.assertEqual(len(actual_connections), index)
            self.assertEqual(len(state["force_field_visuals"]), index)
            self.assertTrue(all(
                connection["exists"]
                and connection["attempt_state"] == "established"
                and connection["endpoint_state"] == "live"
                and connection["durability_state"] == "intact"
                and connection["phase_state"] == "preview"
                and connection["visible"]
                and not connection["collidable"]
                and connection["visual_state"] == "preview"
                for connection in actual_connections
            ))
            self.assertTrue(all(
                connection["state"] == "preview"
                and connection["visible"]
                and not connection["collidable"]
                and "ring_preview" in connection["roles"]
                for connection in provisional_connections
            ))
            self.assertEqual(
                {connection["field_id"] for connection in actual_connections},
                {field["field_id"] for field in state["force_field_visuals"]},
            )
            self.assertEqual(len(state["ring_preview"]["edges"]), index)

        self.engine.start()
        state = self.engine.snapshot()

        self.assertTrue(state["ring_status"]["completed"])
        self.assertEqual(len(state["connections"]), len(ring_markers))
        self.assertEqual(len(state["gates"]), len(ring_markers))
        self.assertEqual(len(state["force_field_visuals"]), len(ring_markers))
        self.assertEqual(
            {connection["visual_state"] for connection in state["connections"]},
            {"active"},
        )
        self.assertTrue(all(
            connection["exists"]
            and connection["visible"]
            and connection["collidable"]
            and connection["ring_boundary"]
            for connection in state["connections"]
        ))
        connection_ids = {
            connection["field_id"] for connection in state["connections"]
        }
        self.assertEqual(
            connection_ids,
            {gate["field_id"] for gate in state["gates"]},
        )
        self.assertEqual(
            connection_ids,
            {edge["field_id"] for edge in state["force_field_topology"]},
        )

    def test_live_41_48_49_sequence_creates_two_ordinary_fields(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker

        self.engine.place(100, marker[41], source="virtual")
        self.assertEqual(self._field_marker_pairs(self.engine), set())

        self.engine.place(101, marker[48], source="virtual")
        self.assertEqual(self._field_marker_pairs(self.engine), {(41, 48)})

        self.engine.place(102, marker[49], source="virtual")
        self.assertEqual(
            self._field_marker_pairs(self.engine),
            {(41, 48), (48, 49)},
        )
        self.assertFalse(self.engine.snapshot()["ring_status"]["completed"])
        self.assertEqual(len(self.engine.level.ring_cycles), 3)

    def test_link_groups_scale_power_and_health_and_split_on_break(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        sequence = (41, 48, 49)
        base_max_hp = self.engine._tower_max_hp()

        for count, marker_id in enumerate(sequence, start=1):
            self.engine.place(100, marker[marker_id], source="virtual")
            state = self.engine.snapshot()
            expected_multiplier = 0.9 + (count - 1) * 0.1
            self.assertTrue(all(
                tower["linked_turret_count"] == count
                and math.isclose(
                    tower["link_multiplier"], expected_multiplier
                )
                and math.isclose(
                    tower["max_hp"], base_max_hp * expected_multiplier
                )
                for tower in state["towers"]
            ))

        for tower in self.engine.placements.values():
            tower["hp"] = tower["max_hp"] / 2.0
        split_field = self.engine._field_between(marker[48], marker[49])
        self.assertIsNotNone(split_field)
        split_field["broken"] = True
        split_field["broken_at"] = self.engine.sim_time

        split = {
            tower["aruco_id"]: tower
            for tower in self.engine.snapshot()["towers"]
        }
        for marker_id in (41, 48):
            self.assertEqual(split[marker_id]["linked_turret_count"], 2)
            self.assertEqual(split[marker_id]["link_multiplier"], 1.0)
            self.assertEqual(split[marker_id]["max_hp"], base_max_hp)
            self.assertEqual(split[marker_id]["hp"], base_max_hp / 2.0)
        self.assertEqual(split[49]["linked_turret_count"], 1)
        self.assertEqual(split[49]["link_multiplier"], 0.9)
        self.assertEqual(split[49]["max_hp"], base_max_hp * 0.9)
        self.assertEqual(split[49]["hp"], base_max_hp * 0.45)

        self.engine.place(100, marker[49], source="virtual")
        restored = {
            tower["aruco_id"]: tower
            for tower in self.engine.snapshot()["towers"]
        }
        self.assertTrue(all(
            tower["linked_turret_count"] == 3
            and math.isclose(tower["link_multiplier"], 1.1)
            and math.isclose(tower["max_hp"], base_max_hp * 1.1)
            for tower in restored.values()
        ))
        self.assertEqual(restored[41]["hp"], base_max_hp * 0.55)
        self.assertEqual(restored[48]["hp"], base_max_hp * 0.55)
        self.assertEqual(restored[49]["hp"], base_max_hp * 1.1)

    def test_machine_gun_damage_uses_exact_link_group_multiplier(self):
        for count, expected_multiplier in ((1, 0.9), (2, 1.0), (3, 1.1)):
            engine = DefenseEngine(MAP_PATH, WAVES_PATH)
            engine.set_virtual_play(True)
            marker = engine.level.socket_by_marker
            sockets = [marker[marker_id] for marker_id in (41, 48, 49)]
            for socket_id in sockets[:count]:
                engine.place(100, socket_id, source="virtual")
            engine.settings["machine_gun_damage"] = 100.0
            firing_tower = engine.placements[sockets[0]]
            targeting = engine._tower_targeting(firing_tower)
            enemy = {
                "id": count,
                "x": firing_tower["x"]
                + math.cos(targeting["angle"]) * 80.0,
                "y": firing_tower["y"]
                + math.sin(targeting["angle"]) * 80.0,
                "hp": 1000.0,
                "progress": 0.5,
                "burn_until": 0.0,
                "burn_damage_per_s": 0.0,
            }
            engine.enemies = {enemy["id"]: enemy}
            for tower in list(engine.placements.values())[1:]:
                tower["cooldown"] = 100.0

            engine._fire_towers(0.1, set())

            self.assertAlmostEqual(
                enemy["hp"], 1000.0 - 100.0 * expected_multiplier
            )

    def test_custom_link_settings_control_run_health_and_damage(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        sockets = [marker[marker_id] for marker_id in (41, 48, 49)]
        for socket_id in sockets:
            self.engine.place(100, socket_id, source="virtual")
        self.engine.start({
            "core_hp": 10000.0,
            "defense_unit_health_percent": 10.0,
            "machine_gun_damage": 100.0,
            "tower_link_start_multiplier": 0.75,
            "tower_link_step": 0.2,
        })

        state = self.engine.snapshot()
        self.assertEqual(state["settings"]["tower_link_start_multiplier"], 0.75)
        self.assertEqual(state["settings"]["tower_link_step"], 0.2)
        self.assertTrue(all(
            tower["linked_turret_count"] == 3
            and tower["link_multiplier"] == 1.15
            and tower["max_hp"] == 1150.0
            for tower in state["towers"]
        ))

        firing_tower = self.engine.placements[sockets[0]]
        targeting = self.engine._tower_targeting(firing_tower)
        enemy = {
            "id": 7003,
            "x": firing_tower["x"] + math.cos(targeting["angle"]) * 80.0,
            "y": firing_tower["y"] + math.sin(targeting["angle"]) * 80.0,
            "hp": 1000.0,
            "progress": 0.5,
            "burn_until": 0.0,
            "burn_damage_per_s": 0.0,
        }
        self.engine.enemies = {enemy["id"]: enemy}
        for tower in list(self.engine.placements.values())[1:]:
            tower["cooldown"] = 100.0

        self.engine._fire_towers(0.1, set())

        self.assertEqual(enemy["hp"], 885.0)

    def test_any_atom_can_use_any_unoccupied_turret_socket(self):
        self.engine.set_virtual_play(True)
        purple_socket = self.engine.level.socket_by_marker[41]
        self.engine.place(100, purple_socket, source="virtual", team="green")
        tower = self.engine.snapshot()["towers"][0]
        self.assertEqual(tower["aruco_id"], 41)
        self.assertEqual(tower["tower_type"], "machine_gun")

    def test_one_atom_can_seed_multiple_independently_aimed_defenses(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        first_socket = marker[48]
        second_socket = marker[49]
        self.engine.place(100, first_socket, source="virtual", team="green")
        self.engine.place(100, second_socket, source="virtual", team="green")

        state = self.engine.snapshot()
        self.assertEqual(len(state["towers"]), 2)
        self.assertEqual(
            [(tower["atom_tag_id"], tower["tower_type"]) for tower in state["towers"]],
            [(100, "machine_gun"), (100, "machine_gun")],
        )
        self.assertEqual(
            {tower["placement_id"] for tower in state["towers"]},
            {first_socket, second_socket},
        )
        self.assertEqual(self.engine.activation_order, [first_socket, second_socket])

        first_before = self.engine.placements[first_socket]["aim_angle"]
        self.engine.set_tower_aim(100, 123, 0.25, socket_id=second_socket)
        self.assertEqual(self.engine.placements[first_socket]["aim_angle"], first_before)
        self.assertAlmostEqual(self.engine.placements[second_socket]["aim_angle"], math.radians(123))
        with self.assertRaises(ValueError):
            self.engine.set_tower_aim(100, 45, 0.5)

    def test_dense_simulation_and_stream_snapshot_are_bounded(self):
        self.engine._spawn_enemy("grunt", {})
        template = next(iter(self.engine.enemies.values()))
        core_x = float(self.engine.level.core["x"])
        core_y = float(self.engine.level.core["y"])
        coordinates = []
        x = -107.0
        while x <= 107.0 and len(coordinates) < 1000:
            y = -107.0
            while y <= 107.0 and len(coordinates) < 1000:
                if _core_octagon_face(x, y, 2.2)[3] >= 0:
                    coordinates.append((core_x + x, core_y + y))
                y += 5.5
            x += 5.5

        self.engine.enemies = {}
        for enemy_id, (x, y) in enumerate(coordinates, 1):
            enemy = copy.deepcopy(template)
            enemy.update({
                "id": enemy_id, "x": x, "y": y, "attacking": True,
                "progress": 1.0, "vx": 0.0, "vy": 40.0,
                "basin_direction": -1.0 if enemy_id % 7 == 0 else 1.0,
            })
            self.engine.enemies[enemy_id] = enemy
        self.engine.next_enemy_id = 1001
        self.engine.phase = "running"
        self.engine.current_wave = len(self.engine.wave_source)
        self.engine.core_hp = self.engine.core_max_hp = 1e12
        self.engine.launched_waves = []

        started = time.perf_counter()
        for _ in range(5):
            self.engine.step(0.05)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.9)

        full = self.engine.snapshot()
        compact = self.engine.snapshot(compact_enemies=True)
        self.assertEqual(len(compact["enemies"]), 1000)
        self.assertIn("vx", compact["enemies"][0])
        self.assertNotIn("hp", compact["enemies"][0])
        self.assertNotIn("lane", compact["enemies"][0])
        self.assertLess(len(json.dumps(compact)), len(json.dumps(full)) * 0.55)

    def test_force_field_counts_unique_impacts_breaks_and_reroutes(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        self.engine.place(100, marker[48], source="virtual", team="green")
        self.engine.place(101, marker[49], source="virtual", team="green")
        self.engine.start({"force_field_hit_capacity": 2})
        field = next(iter(self.engine.force_fields.values()))
        enemies = []
        for enemy_id, proportion in ((9001, 0.25), (9002, 0.75)):
            enemies.append({
                "id": enemy_id,
                "enemy_type": "brute" if enemy_id == 9002 else "grunt",
                "x": field["ax"] + (field["bx"] - field["ax"]) * proportion,
                "y": field["ay"] + (field["by"] - field["ay"]) * proportion,
                "vx": 60.0, "vy": 0.0, "collision_radius": 2.2,
                "hp": 100.0, "core_dps": 1.0,
            })
        self.engine._handle_force_fields([enemies[0]])
        self.engine._handle_force_fields([enemies[0]])
        self.assertEqual(field["hits"], 1)
        self.assertEqual(len(self.engine.force_field_impacts), 1)
        first_impact = self.engine.force_field_impacts[0]
        self.assertAlmostEqual(first_impact["contact_x"], enemies[0]["x"])
        self.assertAlmostEqual(first_impact["contact_y"], enemies[0]["y"])
        self.assertAlmostEqual(first_impact["expires_at"] - first_impact["at"], 0.5)
        midpoint = ((field["ax"] + field["bx"]) / 2, (field["ay"] + field["by"]) / 2)
        self.assertNotEqual(
            (first_impact["contact_x"], first_impact["contact_y"]), midpoint
        )
        self.assertLess(enemies[0]["vx"], 0)
        self.engine._handle_force_fields([enemies[1]])
        self.assertTrue(field["broken"])
        broken_state = self.engine.snapshot()
        self.assertEqual(len(broken_state["force_field_impacts"]), 2)
        self.assertNotEqual(
            broken_state["force_field_impacts"][0]["impact_id"],
            broken_state["force_field_impacts"][1]["impact_id"],
        )
        self.assertEqual(
            broken_state["force_field_impacts"][1]["enemy_type"], "brute"
        )
        self.assertTrue(broken_state["gates"][0]["broken"])
        connection = broken_state["connections"][0]
        self.assertEqual(connection["durability_state"], "broken")
        self.assertTrue(connection["visible"])
        self.assertFalse(connection["collidable"])
        self.engine.sim_time += 0.7
        self.engine.step(0.01)
        expired_state = self.engine.snapshot()
        self.assertEqual(expired_state["force_field_impacts"], [])
        self.assertEqual(expired_state["gates"], [])
        self.assertEqual(
            expired_state["force_field_visuals"][0]["visual_state"], "broken"
        )
        self.assertFalse(expired_state["force_field_visuals"][0]["visible"])
        self.assertFalse(expired_state["connections"][0]["visible"])

    def test_force_field_reroute_reverses_on_the_current_authored_edge(self):
        self.engine._spawn_enemy("grunt", {"top_inner": 1.0})
        enemy = next(iter(self.engine.enemies.values()))
        enemy.update({
            "x": 800.0,
            "y": 240.0,
            "vx": enemy["speed"],
            "vy": 0.0,
            "attacking": False,
            "route_steps": [
                {"edge_id": "edge_top_inner_mid", "reverse": False}
            ],
            "current_route_step": 0,
            "current_edge_id": "edge_top_inner_mid",
            "current_edge_progress": 0.0,
        })

        self.assertTrue(self.engine._reroute_enemy(enemy))

        self.assertEqual(enemy["current_edge_id"], "edge_top_inner_mid")
        self.assertAlmostEqual(enemy["current_edge_progress"], 0.5)
        self.assertEqual(
            enemy["path"][:3],
            [(800.0, 240.0), (560.0, 240.0), (560.0, 400.0)],
        )
        event = self.engine.events[-1]
        self.assertEqual(event["kind"], "orc_rerouted")
        self.assertEqual(event["edge_id"], "edge_top_inner_mid")
        self.assertEqual(event["node_id"], 85)

    def test_destroyed_endpoint_suspends_links_without_bridging_neighbors(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        first_socket, middle_socket, last_socket = (
            marker[53], marker[49], marker[48]
        )
        self.engine.place(100, first_socket, source="virtual")
        self.engine.place(101, middle_socket, source="virtual")
        self.engine.place(102, last_socket, source="virtual")
        self.engine.start()

        established = {
            field_id: (field["from_socket"], field["to_socket"])
            for field_id, field in self.engine.force_fields.items()
        }
        self.assertEqual(len(established), 2)
        self.assertIsNone(self.engine._field_between(first_socket, last_socket))

        middle = self.engine.placements[middle_socket]
        middle["hp"] = 1.0
        self.engine._damage_towers([{
            "x": middle["x"], "y": middle["y"], "core_dps": 20.0,
        }], 0.1)

        self.assertTrue(middle["destroyed"])
        self.assertEqual(
            {
                field_id: (field["from_socket"], field["to_socket"])
                for field_id, field in self.engine.force_fields.items()
            },
            established,
        )
        self.assertIsNone(self.engine._field_between(first_socket, last_socket))
        damaged_state = self.engine.snapshot()
        self.assertEqual(damaged_state["gates"], [])
        self.assertEqual(
            {field["visual_state"] for field in damaged_state["force_field_visuals"]},
            {"suspended"},
        )
        self.assertTrue(all(
            not field["visible"]
            for field in damaged_state["force_field_visuals"]
        ))
        self.assertEqual(
            {
                edge["state"]
                for edge in damaged_state["force_field_topology"]
                if middle_socket in {edge["from_socket"], edge["to_socket"]}
                and edge["exists"]
            },
            {"suspended"},
        )

        self.engine.place(103, middle_socket, source="virtual")
        replacement = self.engine.placements[middle_socket]
        self.assertIsNot(replacement, middle)
        self.assertTrue(middle["destroyed"])
        self.assertFalse(replacement["destroyed"])
        self.assertEqual(replacement["tower_type"], "tesla_coil")
        self.assertEqual(
            {
                field_id: (field["from_socket"], field["to_socket"])
                for field_id, field in self.engine.force_fields.items()
            },
            established,
        )
        self.assertEqual(len(self.engine.snapshot()["gates"]), 2)

    def test_spatial_ring_atomically_creates_all_missing_boundary_edges(self):
        self.engine.set_virtual_play(True)
        self.engine.start({"ring_field_immunity_s": 100.0})
        marker = self.engine.level.socket_by_marker
        sequence = (53, 49, 48, 50, 41, 52, 51, 40, 44, 45, 47, 55, 42, 46)
        original_evaluate = self.engine._evaluate_ring_topology
        self.engine._evaluate_ring_topology = lambda: None
        try:
            for marker_id in sequence:
                self.engine.place(100, marker[marker_id], source="virtual")
        finally:
            self.engine._evaluate_ring_topology = original_evaluate

        self.assertIsNotNone(
            self.engine._field_between(marker[51], marker[40])
        )
        first_field = self.engine._field_between(marker[53], marker[49])
        self.assertIsNotNone(first_field)
        first_field.update({"hits": 50, "broken": True, "broken_at": 10.0})
        event_index = len(self.engine.events)

        self.engine._evaluate_ring_topology()

        established = [
            event for event in self.engine.events[event_index:]
            if event["kind"] == "force_field_established"
        ]
        self.assertEqual(
            {
                tuple(sorted((
                    self.engine.level.sockets[event["from_socket"]]["aruco_id"],
                    self.engine.level.sockets[event["to_socket"]]["aruco_id"],
                )))
                for event in established
            },
            {
                (40, 41), (40, 42), (41, 48), (42, 44),
                (46, 52), (46, 55), (49, 50), (51, 53),
            },
        )
        self.assertEqual(
            {event["link_kind"] for event in established}, {"ring_boundary"}
        )
        core = self.engine.snapshot()["core_sequence"]
        self.assertTrue(core["ring_completed"])
        self.assertEqual(core["ring_candidate_count"], 14)
        self.assertEqual(len(core["ring_socket_ids"]), 14)
        self.assertEqual(
            {
                self.engine.level.sockets[socket_id]["aruco_id"]
                for socket_id in core["ring_socket_ids"]
            },
            set(sequence),
        )
        self.assertEqual(core["field_immunity_remaining_s"], 100.0)
        self.assertFalse(first_field["broken"])
        self.assertEqual(first_field["hits"], 0)

    def test_spatial_solver_selects_all_sixteen_living_full_board_turrets(self):
        self.engine.set_virtual_play(True)
        self.engine.start({"ring_field_immunity_s": 100.0})
        marker = self.engine.level.socket_by_marker
        sequence = (53, 49, 41, 48, 40, 42, 43, 54, 55, 46, 51, 52, 47, 45, 44)
        original_evaluate = self.engine._evaluate_ring_topology
        self.engine._evaluate_ring_topology = lambda: None
        try:
            for marker_id in sequence:
                self.engine.place(100, marker[marker_id], source="virtual")
            self.engine.place(100, marker[50], source="virtual")
        finally:
            self.engine._evaluate_ring_topology = original_evaluate
        event_index = len(self.engine.events)

        self.engine._evaluate_ring_topology()

        established = [
            event for event in self.engine.events[event_index:]
            if event["kind"] == "force_field_established"
        ]
        self.assertTrue(established)
        self.assertEqual(
            {event["link_kind"] for event in established}, {"ring_boundary"}
        )
        core = self.engine.snapshot()["core_sequence"]
        self.assertTrue(core["ring_completed"])
        self.assertEqual(core["ring_source"], "spatial_geometry")
        self.assertEqual(core["ring_candidate_count"], 16)
        self.assertEqual(
            {
                self.engine.level.sockets[socket_id]["aruco_id"]
                for socket_id in core["ring_socket_ids"]
            },
            set(range(40, 56)),
        )
        self.assertEqual(
            sum(field["ring_boundary"] for field in self.engine.force_fields.values()),
            16,
        )

    def test_ring_preflight_is_atomic_and_retries_on_replenishment(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        sequence = (40, 48, 50, 41, 43, 49, 53, 52, 55, 54, 45, 42, 46)
        original_evaluate = self.engine._evaluate_ring_topology
        self.engine._evaluate_ring_topology = lambda: None
        try:
            for marker_id in sequence:
                self.engine.place(100, marker[marker_id], source="virtual")
        finally:
            self.engine._evaluate_ring_topology = original_evaluate
        original_line_obstructions = self.engine._line_obstructions
        before = set(self.engine.force_fields)
        self.engine._line_obstructions = lambda _first, _second: {
            "blocker_ids": ["test_blocker"],
            "blocker_socket_ids": [marker[51]],
            "blocker_markers": [51],
        }
        try:
            self.engine._evaluate_ring_topology()
            blocked_state = self.engine.snapshot()
        finally:
            self.engine._line_obstructions = original_line_obstructions

        self.assertEqual(set(self.engine.force_fields), before)
        self.assertEqual(
            sum(
                field["ring_boundary"]
                for field in self.engine.force_fields.values()
            ),
            0,
        )
        self.assertFalse(blocked_state["core_sequence"]["ring_completed"])
        self.assertEqual(
            blocked_state["ring_status"]["last_evaluation"]["source"],
            "spatial_geometry",
        )
        self.assertIn(
            "blocked_line_of_sight",
            blocked_state["ring_status"]["last_evaluation"]["rejection_reasons"],
        )

        self.engine.place(100, marker[46], source="virtual")
        core = self.engine.snapshot()["core_sequence"]
        self.assertTrue(core["ring_completed"])
        self.assertEqual(core["ring_source"], "spatial_geometry")
        self.assertEqual(core["ring_candidate_count"], 13)

    def test_previous_run_closes_the_first_valid_spatial_ring(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        sequence = (41, 49, 53, 51, 52, 54, 46, 55, 45, 43, 40, 50)
        completed_at_marker = None
        for marker_id in sequence:
            self.engine.place(100, marker[marker_id], source="virtual")
            if self.engine.ring_completed_at is not None and completed_at_marker is None:
                completed_at_marker = marker_id

        state = self.engine.snapshot()
        self.assertEqual(completed_at_marker, 43)
        self.assertTrue(state["ring_status"]["completed"])
        self.assertEqual(state["ring_status"]["candidate_source"], "spatial_geometry")
        self.assertEqual(state["ring_status"]["closing_markers"], [43, 45])
        self.assertEqual(
            state["ring_status"]["selected_markers"],
            [41, 43, 45, 54, 55, 46, 52, 51, 53, 49],
        )
        boundary = self.engine._field_between(marker[43], marker[45])
        self.assertIsNotNone(boundary)
        self.assertTrue(boundary["ring_boundary"])
        self.assertEqual(
            state["ring_status"]["last_evaluation"]["rejection_reasons"], []
        )

    def test_latest_run_spatial_solver_ignores_activation_order(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        sequence = (40, 48, 50, 41, 43, 49, 53, 52, 55, 54, 45, 42, 46)
        original_evaluate = self.engine._evaluate_ring_topology
        self.engine._evaluate_ring_topology = lambda: None
        try:
            for marker_id in sequence:
                self.engine.place(100, marker[marker_id], source="virtual")
        finally:
            self.engine._evaluate_ring_topology = original_evaluate

        candidate = self.engine._solve_spatial_ring()

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["source"], "spatial_geometry")
        self.assertEqual(
            [
                self.engine.level.sockets[socket_id]["aruco_id"]
                for socket_id in candidate["order"]
            ],
            [41, 40, 43, 42, 45, 54, 55, 46, 52, 53, 49, 50, 48],
        )
        self.assertEqual(candidate["excluded_markers"], [])
        self.assertTrue(candidate["closable"])

        selected = self.engine._select_ring_candidate()
        diagnostics = self.engine.snapshot()["ring_status"]
        self.assertIsNotNone(selected)
        self.assertEqual(diagnostics["search_evaluated_count"], 1)
        self.assertEqual(
            diagnostics["last_evaluation"]["excluded_markers"], []
        )
        self.assertEqual(
            len(diagnostics["last_evaluation"]["boundary_edges"]), 13
        )
        self.assertEqual(diagnostics["alternative_candidates"], [])
        self.assertEqual(diagnostics["rejected_candidates"], [])

    def test_spatial_solver_is_deterministic_across_activation_permutations(self):
        active_markers = (40, 41, 42, 43, 45, 46, 48, 49, 50, 52, 53, 54, 55)
        sequences = (
            (40, 48, 50, 41, 43, 49, 53, 52, 55, 54, 45, 42, 46),
            tuple(reversed(active_markers)),
            active_markers,
        )
        observed = []
        for sequence in sequences:
            engine = DefenseEngine(MAP_PATH, WAVES_PATH)
            engine.set_virtual_play(True)
            engine.start()
            marker = engine.level.socket_by_marker
            original_evaluate = engine._evaluate_ring_topology
            engine._evaluate_ring_topology = lambda: None
            try:
                for marker_id in sequence:
                    engine.place(100, marker[marker_id], source="virtual")
            finally:
                engine._evaluate_ring_topology = original_evaluate
            candidate = engine._solve_spatial_ring()
            self.assertIsNotNone(candidate)
            observed.append(tuple(
                engine.level.sockets[socket_id]["aruco_id"]
                for socket_id in candidate["order"]
            ))

        self.assertEqual(
            observed,
            [
                (41, 40, 43, 42, 45, 54, 55, 46, 52, 53, 49, 50, 48)
            ] * len(sequences),
        )

    def test_spatial_solver_matches_independent_brute_force_oracle(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        sequence = (40, 48, 50, 41, 43, 49, 53, 52, 55, 54, 45, 42, 46)
        original_evaluate = self.engine._evaluate_ring_topology
        self.engine._evaluate_ring_topology = lambda: None
        try:
            for marker_id in sequence:
                self.engine.place(100, marker[marker_id], source="virtual")
        finally:
            self.engine._evaluate_ring_topology = original_evaluate

        core_x = float(self.engine.level.core["x"])
        core_y = float(self.engine.level.core["y"])
        active_socket_ids = sorted(self.engine._active_living_socket_ids())
        oracle = None
        for turret_count in range(len(active_socket_ids), 7, -1):
            candidates = []
            for subset in combinations(active_socket_ids, turret_count):
                order = sorted(
                    subset,
                    key=lambda socket_id: (
                        math.atan2(
                            self.engine.level.sockets[socket_id]["y"] - core_y,
                            self.engine.level.sockets[socket_id]["x"] - core_x,
                        ),
                        self.engine.level.sockets[socket_id]["aruco_id"],
                    ),
                )
                points = [
                    (
                        self.engine.level.sockets[socket_id]["x"],
                        self.engine.level.sockets[socket_id]["y"],
                    )
                    for socket_id in order
                ]
                if not _is_valid_core_ring(points, core_x, core_y):
                    continue
                pairs = [*zip(order, order[1:]), (order[-1], order[0])]
                if any(
                    self.engine._field_between(first, second) is None
                    and self.engine._line_blockers(
                        self.engine.placements[first],
                        self.engine.placements[second],
                    )
                    for first, second in pairs
                ):
                    continue
                lengths = [
                    math.hypot(
                        self.engine.level.sockets[second]["x"]
                        - self.engine.level.sockets[first]["x"],
                        self.engine.level.sockets[second]["y"]
                        - self.engine.level.sockets[first]["y"],
                    )
                    for first, second in pairs
                ]
                candidates.append((
                    max(lengths),
                    sum(lengths),
                    tuple(
                        self.engine.level.sockets[socket_id]["aruco_id"]
                        for socket_id in order
                    ),
                ))
            if candidates:
                oracle = min(candidates)
                break

        selected = self.engine._solve_spatial_ring()
        self.assertIsNotNone(selected)
        self.assertIsNotNone(oracle)
        self.assertEqual(
            tuple(
                self.engine.level.sockets[socket_id]["aruco_id"]
                for socket_id in selected["order"]
            ),
            oracle[2],
        )

    def test_all_fixed_socket_subsets_have_stable_angular_geometry(self):
        marker = self.engine.level.socket_by_marker
        core_x = float(self.engine.level.core["x"])
        core_y = float(self.engine.level.core["y"])
        valid_counts = {}
        for turret_count in range(8, 17):
            valid_count = 0
            for marker_subset in combinations(range(40, 56), turret_count):
                ordered_markers = sorted(
                    marker_subset,
                    key=lambda marker_id: (
                        math.atan2(
                            self.engine.level.sockets[marker[marker_id]]["y"] - core_y,
                            self.engine.level.sockets[marker[marker_id]]["x"] - core_x,
                        ),
                        marker_id,
                    ),
                )
                points = [
                    (
                        self.engine.level.sockets[marker[marker_id]]["x"],
                        self.engine.level.sockets[marker[marker_id]]["y"],
                    )
                    for marker_id in ordered_markers
                ]
                valid_count += int(_is_valid_core_ring(points, core_x, core_y))
            valid_counts[turret_count] = valid_count

        self.assertEqual(
            valid_counts,
            {8: 12771, 9: 11424, 10: 8007, 11: 4368, 12: 1820,
             13: 560, 14: 120, 15: 16, 16: 1},
        )

    def test_spatial_search_prunes_expensive_analysis_when_all_edges_are_blocked(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        sequence = (40, 48, 50, 41, 43, 49, 53, 52, 55, 54, 45, 42, 46)
        original_evaluate = self.engine._evaluate_ring_topology
        self.engine._evaluate_ring_topology = lambda: None
        try:
            for marker_id in sequence:
                self.engine.place(100, marker[marker_id], source="virtual")
        finally:
            self.engine._evaluate_ring_topology = original_evaluate
        original_line_obstructions = self.engine._line_obstructions
        original_analyze = self.engine._analyze_ring_candidate
        analyze_calls = 0

        def count_analysis(*args, **kwargs):
            nonlocal analyze_calls
            analyze_calls += 1
            return original_analyze(*args, **kwargs)

        self.engine._line_obstructions = lambda _first, _second: {
            "blocker_ids": ["test_blocker"],
            "blocker_socket_ids": [marker[51]],
            "blocker_markers": [51],
        }
        self.engine._analyze_ring_candidate = count_analysis
        try:
            search = self.engine._spatial_ring_search()
        finally:
            self.engine._line_obstructions = original_line_obstructions
            self.engine._analyze_ring_candidate = original_analyze

        self.assertIsNone(search["selected"])
        self.assertEqual(search["evaluated_count"], 2380)
        self.assertEqual(analyze_calls, 14)

    def test_spatial_ring_creation_rolls_back_if_one_boundary_cannot_be_created(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        sequence = (40, 48, 50, 41, 43, 49, 53, 52, 55, 54, 45, 42, 46)
        original_evaluate = self.engine._evaluate_ring_topology
        self.engine._evaluate_ring_topology = lambda: None
        try:
            for marker_id in sequence:
                self.engine.place(100, marker[marker_id], source="virtual")
        finally:
            self.engine._evaluate_ring_topology = original_evaluate
        original_create = self.engine._create_force_field
        field_ids_before = set(self.engine.force_fields)
        event_count_before = len(self.engine.events)
        ring_creations = 0

        def fail_second_boundary(first, second, **kwargs):
            nonlocal ring_creations
            if kwargs.get("link_kind") == "ring_boundary":
                ring_creations += 1
                if ring_creations == 2:
                    return None
            return original_create(first, second, **kwargs)

        self.engine._create_force_field = fail_second_boundary
        try:
            with self.assertRaisesRegex(RuntimeError, "boundary could not be created"):
                self.engine._evaluate_ring_topology()
        finally:
            self.engine._create_force_field = original_create

        self.assertEqual(set(self.engine.force_fields), field_ids_before)
        self.assertEqual(len(self.engine.events), event_count_before)
        self.assertFalse(self.engine.snapshot()["ring_status"]["completed"])
        self.assertFalse(any(
            field["ring_boundary"] for field in self.engine.force_fields.values()
        ))

        self.engine._evaluate_ring_topology()

        state = self.engine.snapshot()
        self.assertTrue(state["ring_status"]["completed"])
        self.assertEqual(state["ring_status"]["candidate_source"], "spatial_geometry")

    def test_spatial_candidate_that_does_not_enclose_core_is_rejected(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        sequence = (40, 41, 42, 43, 44, 45, 47, 54)
        for marker_id in sequence:
            self.engine.place(100, marker[marker_id], source="virtual")

        state = self.engine.snapshot()
        self.assertFalse(state["ring_status"]["completed"])
        evaluation = state["ring_status"]["last_evaluation"]
        self.assertEqual(evaluation["source"], "spatial_geometry")
        self.assertFalse(evaluation["contains_core"])
        self.assertIn("core_outside", evaluation["rejection_reasons"])

    def test_ordinary_fields_follow_placement_order_without_fallback(self):
        sequences = (
            (49, 50, 41, 53, 48, 51, 52, 43, 46, 54, 40),
            (40, 54, 46, 43, 52, 51, 48, 53, 41, 50, 49),
            (40, 41, 43, 46, 48, 49, 50, 51, 52, 53, 54),
        )
        observed = []
        for sequence in sequences:
            engine = DefenseEngine(MAP_PATH, WAVES_PATH)
            engine.set_virtual_play(True)
            engine.start()
            marker = engine.level.socket_by_marker
            original_evaluate = engine._evaluate_ring_topology
            engine._evaluate_ring_topology = lambda: None
            try:
                for marker_id in sequence:
                    engine.place(100, marker[marker_id], source="virtual")
            finally:
                engine._evaluate_ring_topology = original_evaluate
            state = engine.snapshot()
            self.assertEqual(
                [
                    (link["from_marker"], link["to_marker"])
                    for link in state["placement_links"]
                ],
                list(zip(sequence, sequence[1:])),
            )
            expected_fields = {
                tuple(sorted((link["from_marker"], link["to_marker"])))
                for link in state["placement_links"]
                if link["status"] == "established"
            }
            observed.append(self._field_marker_pairs(engine))
            self.assertEqual(observed[-1], expected_fields)
            self.assertFalse(state["ring_status"]["completed"])
        # The first two sequences are reversals and therefore describe the
        # same undirected chain; the third deliberately describes another.
        self.assertEqual(len({frozenset(fields) for fields in observed}), 2)

    def test_ring_boundary_is_order_independent_across_placement_permutations(self):
        ring_markers = {41, 42, 43, 45, 46, 52, 53, 54}
        sequences = (
            (41, 43, 42, 45, 54, 46, 52, 53),
            (53, 52, 46, 54, 45, 42, 43, 41),
            (43, 54, 41, 52, 42, 53, 45, 46),
        )
        for sequence in sequences:
            engine = DefenseEngine(MAP_PATH, WAVES_PATH)
            engine.set_virtual_play(True)
            engine.start()
            marker = engine.level.socket_by_marker
            for marker_id in sequence:
                engine.place(100, marker[marker_id], source="virtual")

            state = engine.snapshot()
            self.assertTrue(state["core_sequence"]["ring_completed"])
            self.assertEqual(
                state["ring_status"]["candidate_source"], "spatial_geometry"
            )
            self.assertEqual(
                {
                    engine.level.sockets[socket_id]["aruco_id"]
                    for socket_id in state["core_sequence"]["ring_socket_ids"]
                },
                ring_markers,
            )
            self.assertEqual(
                sum(
                    field["ring_boundary"]
                    for field in engine.force_fields.values()
                ),
                len(ring_markers),
            )
            self.assertEqual(
                [
                    (link["from_marker"], link["to_marker"])
                    for link in state["placement_links"]
                ],
                list(zip(sequence, sequence[1:])),
            )

    def test_ring_evaluation_is_idempotent_and_does_not_redirect_fields(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        for marker_id in (49, 50, 41, 53, 48, 43, 46, 54, 40):
            self.engine.place(100, marker[marker_id], source="virtual")
        field_ids = set(self.engine.force_fields)
        object_ids = {
            field_id: id(field)
            for field_id, field in self.engine.force_fields.items()
        }
        established_count = sum(
            event["kind"] == "force_field_established"
            for event in self.engine.events
        )

        self.engine._evaluate_ring_topology()
        self.engine._evaluate_ring_topology()

        self.assertEqual(set(self.engine.force_fields), field_ids)
        self.assertEqual(
            {field_id: id(field) for field_id, field in self.engine.force_fields.items()},
            object_ids,
        )
        self.assertEqual(
            sum(
                event["kind"] == "force_field_established"
                for event in self.engine.events
            ),
            established_count,
        )

    def test_broken_field_persists_until_an_endpoint_is_replenished(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        self.engine.place(100, marker[48], source="virtual")
        self.engine.place(100, marker[49], source="virtual")
        field = self.engine._field_between(marker[48], marker[49])
        self.assertIsNotNone(field)
        field.update({"hits": 50, "broken": True, "broken_at": 2.0})

        self.engine._evaluate_ring_topology()

        self.assertIs(self.engine._field_between(marker[48], marker[49]), field)
        self.assertTrue(field["broken"])
        self.assertEqual(field["hits"], 50)

        self.engine.place(100, marker[48], source="virtual")

        self.assertIs(self.engine._field_between(marker[48], marker[49]), field)
        self.assertFalse(field["broken"])
        self.assertEqual(field["hits"], 0)

    def test_last_run_sequence_links_every_clear_consecutive_pair(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        sequence = (40, 50, 48, 49, 41, 53, 51, 52)
        for marker_id in sequence:
            self.engine.place(100, marker[marker_id], source="virtual")

        state = self.engine.snapshot()
        self.assertEqual(
            self._field_marker_pairs(self.engine),
            {
                (40, 50), (41, 49), (41, 53), (48, 49),
                (48, 50), (51, 52), (51, 53),
            },
        )
        self.assertEqual(
            [
                (link["from_marker"], link["to_marker"], link["status"])
                for link in state["placement_links"]
            ],
            [(*pair, "established") for pair in zip(sequence, sequence[1:])],
        )
        self.assertFalse(state["ring_status"]["completed"])
        cycles = {
            cycle["turret_count"]: set(cycle["missing_markers"])
            for cycle in state["ring_status"]["cycles"]
        }
        self.assertEqual(cycles[8], {42, 43, 44, 45, 54})
        self.assertEqual(cycles[11], {44, 45, 46, 47, 55})
        self.assertEqual(cycles[13], {42, 43, 44, 45, 46, 47, 55})

    def test_topology_diagnostics_remain_available_after_gates_are_hidden(self):
        self.engine.set_virtual_play(True)
        self.engine.start()
        marker = self.engine.level.socket_by_marker
        self.engine.place(100, marker[48], source="virtual")
        self.engine.place(100, marker[49], source="virtual")
        field = self.engine._field_between(marker[48], marker[49])
        self.assertIsNotNone(field)
        field.update({"hits": 50, "broken": True, "broken_at": 3.0})
        self.engine.phase = "won"

        state = self.engine.snapshot()

        self.assertEqual(state["gates"], [])
        self.assertTrue(all(
            not field["visible"] for field in state["force_field_visuals"]
        ))
        topology = next(
            edge for edge in state["force_field_topology"]
            if {edge["from_marker"], edge["to_marker"]} == {48, 49}
        )
        self.assertEqual(topology["state"], "broken")
        self.assertEqual(topology["hits"], 50)

    def test_authored_blocker_is_recorded_and_link_retries_when_removed(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        self.engine.place(100, marker[42], source="virtual")

        first = self.engine.level.sockets[marker[42]]
        second = self.engine.level.sockets[marker[44]]
        midpoint_x = (first["x"] + second["x"]) / 2
        midpoint_y = (first["y"] + second["y"]) / 2
        self.engine.level.force_field_blockers = ({
            "blocker_id": "test_wall",
            "points": (
                (midpoint_x - 8, midpoint_y - 8),
                (midpoint_x + 8, midpoint_y - 8),
                (midpoint_x + 8, midpoint_y + 8),
                (midpoint_x - 8, midpoint_y + 8),
            ),
        },)

        self.engine.place(100, marker[44], source="virtual")

        self.assertIsNone(self.engine._field_between(marker[42], marker[44]))
        blocked_state = self.engine.snapshot()
        connection = next(
            edge for edge in blocked_state["connections"]
            if {edge["from_marker"], edge["to_marker"]} == {42, 44}
        )
        self.assertFalse(connection["exists"])
        self.assertEqual(connection["attempt_state"], "blocked")
        self.assertEqual(connection["state"], "blocked")
        self.assertEqual(connection["blocker_ids"], ["test_wall"])
        self.assertEqual(connection["blocker_markers"], [])
        self.assertFalse(connection["visible"])
        self.assertFalse(connection["collidable"])
        self.assertEqual(blocked_state["force_field_visuals"], [])
        blocked = next(
            edge for edge in blocked_state["force_field_topology"]
            if {edge["from_marker"], edge["to_marker"]} == {42, 44}
        )
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["blocker_ids"], ["test_wall"])

        self.engine.placements[marker[42]]["destroyed"] = True
        self.engine.level.force_field_blockers = ()
        self.engine._reconcile_connections(reason="test_blocker_removed")
        self.assertEqual(
            self.engine.placement_link_attempts[0]["status"],
            "pending_endpoint",
        )
        self.engine.place(100, marker[42], source="virtual")
        self.assertEqual(
            self.engine.placement_link_attempts[0]["status"],
            "established",
        )
        self.engine.place(100, marker[45], source="virtual")

        self.assertIsNotNone(self.engine._field_between(marker[42], marker[44]))
        reconciled = next(
            edge for edge in self.engine.snapshot()["force_field_topology"]
            if {edge["from_marker"], edge["to_marker"]} == {42, 44}
        )
        self.assertEqual(reconciled["state"], "active")
        self.assertIsNotNone(self.engine._field_between(marker[44], marker[45]))
        self.assertEqual(
            [link["status"] for link in self.engine.snapshot()["placement_links"]],
            ["established", "established"],
        )

    def test_ring_requires_8_to_16_turrets_and_encloses_aruco_38(self):
        self.assertEqual((RING_MIN_TURRETS, RING_MAX_TURRETS), (8, 16))
        core_x, core_y = self.engine.level.core["x"], self.engine.level.core["y"]
        octagon = [
            (
                core_x + math.cos(index * math.tau / 8) * 300,
                core_y + math.sin(index * math.tau / 8) * 300,
            )
            for index in range(8)
        ]
        self.assertTrue(_is_valid_core_ring(octagon, core_x, core_y))
        self.assertFalse(_is_valid_core_ring(octagon[:7], core_x, core_y))
        seventeen = [
            (
                core_x + math.cos(index * math.tau / 17) * 300,
                core_y + math.sin(index * math.tau / 17) * 300,
            )
            for index in range(17)
        ]
        self.assertFalse(_is_valid_core_ring(seventeen, core_x, core_y))
        self.assertFalse(
            _is_valid_core_ring([(x + 900, y) for x, y in octagon], core_x, core_y)
        )

        self.engine.set_virtual_play(True)
        # This clockwise authored subset demonstrates that the minimum of
        # eight is usable with the deterministic spatial boundary.
        for index, marker_id in enumerate((41, 43, 42, 45, 54, 46, 52, 53)):
            self.engine.place(
                100 if index % 2 == 0 else 102,
                self.engine.level.socket_by_marker[marker_id],
                source="virtual",
            )
        self.engine.start({"ring_field_immunity_s": 100.0})
        sequence = self.engine.snapshot()["core_sequence"]
        self.assertTrue(sequence["ring_completed"])
        self.assertEqual(len(sequence["ring_socket_ids"]), 8)
        self.assertEqual(sequence["marker_id"], 38)
        self.assertEqual(sequence["stage"], "ring_ready")
        self.assertEqual(sequence["field_immunity_remaining_s"], 100.0)
        self.assertEqual(len(self.engine.force_fields), 8)
        self.assertEqual(
            sum(field["ring_boundary"] for field in self.engine.force_fields.values()),
            8,
        )

    def test_completed_ring_field_stays_fixed_when_ninth_turret_is_added(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        ring_markers = (41, 43, 42, 45, 54, 46, 52, 53)
        for index, marker_id in enumerate(ring_markers):
            self.engine.place(
                100 if index % 2 == 0 else 102,
                marker[marker_id],
                source="virtual",
            )
        self.engine.start()

        boundary_id = self.engine._canonical_field_id(marker[53], marker[41])
        boundary_before = dict(self.engine.force_fields[boundary_id])
        self.assertEqual(boundary_before["link_kind"], "ring_boundary")
        self.assertTrue(boundary_before["ring_boundary"])

        self.engine.place(101, marker[47], source="virtual")
        self.assertEqual(
            (self.engine.force_fields[boundary_id]["from_socket"],
             self.engine.force_fields[boundary_id]["to_socket"]),
            (boundary_before["from_socket"], boundary_before["to_socket"]),
        )
        added = self.engine._field_between(marker[53], marker[47])
        self.assertIsNotNone(added)
        self.assertEqual(added["link_kind"], "placement")
        self.assertIsNone(self.engine._field_between(marker[47], marker[41]))

        self.assertEqual(
            set(self.engine.snapshot()["core_sequence"]["ring_socket_ids"]),
            set(marker[marker_id] for marker_id in ring_markers),
        )
        self.assertIn(boundary_id, self.engine.force_fields)

    def test_completed_ring_fields_are_immune_then_resume_unique_damage(self):
        self.engine.set_virtual_play(True)
        for index, marker_id in enumerate((41, 43, 42, 45, 54, 46, 52, 53)):
            self.engine.place(
                100 if index % 2 == 0 else 102,
                self.engine.level.socket_by_marker[marker_id],
                source="virtual",
            )
        self.engine.start({"ring_field_immunity_s": 100.0})
        field = next(iter(self.engine.force_fields.values()))
        midpoint = ((field["ax"] + field["bx"]) / 2, (field["ay"] + field["by"]) / 2)
        first = {"id": 7001, "x": midpoint[0], "y": midpoint[1], "vx": 40.0, "vy": 0.0, "collision_radius": 2.2, "hp": 100.0}
        self.engine._handle_force_fields([first])
        self.assertEqual(field["hits"], 0)
        self.assertTrue(self.engine.snapshot()["gates"][0]["invulnerable"])
        self.engine.sim_time = 100.1
        second = {"id": 7002, "x": midpoint[0], "y": midpoint[1], "vx": 40.0, "vy": 0.0, "collision_radius": 2.2, "hp": 100.0}
        self.engine._handle_force_fields([second])
        self.assertEqual(field["hits"], 1)
        self.assertFalse(self.engine.snapshot()["gates"][0]["invulnerable"])

    def test_opposing_core_tags_trigger_radial_fire_purge(self):
        self.engine.set_virtual_play(True)
        for index, marker_id in enumerate((41, 43, 42, 45, 54, 46, 52, 53)):
            self.engine.place(
                100 if index % 2 == 0 else 102,
                self.engine.level.socket_by_marker[marker_id],
                source="virtual",
            )
        self.engine.start({"ring_field_immunity_s": 100.0})
        self.assertTrue(self.engine._spawn_enemy("brute", {"top_inner": 1.0}))
        self.assertTrue(self.engine._spawn_enemy("grunt", {"bottom_inner": 1.0}))
        self.engine.activate_core_tag(100, source="virtual", team="green")
        self.assertEqual(self.engine.core_stage, "first_tag")
        with self.assertRaisesRegex(ValueError, "opposing team"):
            self.engine.activate_core_tag(101, source="virtual", team="green")
        self.engine.activate_core_tag(102, source="virtual", team="purple")
        self.assertEqual(self.engine.core_stage, "detonating")
        self.assertEqual(len(self.engine.core_purge_target_ids), 2)
        for _ in range(35):
            self.engine.step(0.1)
        state = self.engine.snapshot()
        self.assertEqual(state["phase"], "won")
        self.assertEqual(state["core_sequence"]["stage"], "complete")
        self.assertTrue(state["core_sequence"]["core_force_field_active"])
        self.assertEqual(state["active_enemies"], 0)
        self.assertTrue(any(event["kind"] == "core_detonation_complete" for event in state["events"]))

    def test_physical_core_stack_uses_stable_camera_overlap_and_arm_safety(self):
        self.engine.set_virtual_play(False)
        for index, marker_id in enumerate((41, 43, 42, 45, 54, 46, 52, 53)):
            self.engine.place(
                100 if index % 2 == 0 else 102,
                self.engine.level.socket_by_marker[marker_id],
                source="physical",
            )
        self.engine.start()
        green_arm = {"green": {"connected": True, "enabled": True, "pump_mode": "off"}}
        first_tags = [
            {"id": 38, "nx": 0.5, "ny": 0.5, "missing": 0},
            {"id": 100, "nx": 0.5, "ny": 0.5, "missing": 0},
        ]
        self.engine.ingest_physical(first_tags, green_arm, now=1.0)
        self.engine.ingest_physical(first_tags, green_arm, now=1.6)
        self.assertEqual(self.engine.core_stage, "first_tag")
        purple_arm = {"purple": {"connected": True, "enabled": True, "pump_mode": "off"}}
        second_tags = [
            {"id": 38, "nx": 0.5, "ny": 0.5, "missing": 0},
            {"id": 102, "nx": 0.5, "ny": 0.5, "missing": 0},
        ]
        self.engine.ingest_physical(second_tags, purple_arm, now=2.0)
        self.engine.ingest_physical(second_tags, purple_arm, now=2.6)
        self.assertEqual(self.engine.core_stage, "detonating")

    def test_isolated_defense_health_is_scaled_and_destroyed_tower_is_replaced(self):
        self.engine.set_virtual_play(True)
        socket_id = self.engine.level.socket_by_marker[48]
        self.engine.place(100, socket_id, source="virtual", team="green")
        self.engine.start({"core_hp": 20000.0, "defense_unit_health_percent": 15.0})
        tower = self.engine.placements[socket_id]
        self.assertEqual((tower["hp"], tower["max_hp"]), (2700.0, 2700.0))
        self.assertEqual(tower["linked_turret_count"], 1)
        self.assertEqual(tower["link_multiplier"], 0.9)
        tower["hp"] = 1.0
        self.engine._damage_towers([{
            "x": tower["x"], "y": tower["y"], "core_dps": 20.0,
        }], 0.1)
        self.assertTrue(tower["destroyed"])
        self.assertEqual(tower["hp"], 0.0)
        self.engine.place(103, socket_id, source="virtual", team="purple")
        replacement = self.engine.placements[socket_id]
        self.assertIsNot(replacement, tower)
        self.assertTrue(tower["destroyed"])
        self.assertFalse(replacement["destroyed"])
        self.assertEqual(replacement["tower_type"], "tesla_coil")
        self.assertEqual(replacement["atom_tag_id"], 103)
        self.assertEqual(replacement["hp"], 2700.0)
        self.assertEqual(list(self.engine.placements), [socket_id])

    def test_tower_melee_contact_uses_pod_reach_and_enemy_collision_radius(self):
        self.assertEqual(TOWER_POD_RADIUS, 56.0)
        self.assertEqual(ORC_TOWER_MELEE_REACH, 38.0)
        self.assertEqual(TOWER_ATTACK_RADIUS, 94.0)
        self.engine.set_virtual_play(True)
        socket_id = self.engine.level.socket_by_marker[48]
        self.engine.place(100, socket_id, source="virtual", team="green")
        tower = self.engine.placements[socket_id]
        tower["hp"] = tower["max_hp"] = 100.0

        enemy = {
            "id": 7,
            "x": tower["x"] + 95.0,
            "y": tower["y"],
            "collision_radius": 2.2,
            "tower_dps": 10.0,
        }
        self.engine._damage_towers([enemy], 1.0)
        self.assertEqual(tower["hp"], 90.0)
        self.assertEqual(tower["last_damage_amount"], 10.0)
        self.assertEqual(tower["last_damage_at"], self.engine.sim_time)
        public = self.engine.snapshot()["towers"][0]
        self.assertEqual(public["last_damage_amount"], 10.0)
        self.assertEqual(public["last_damage_at"], self.engine.sim_time)

        tower["hp"] = 100.0
        enemy["x"] = tower["x"] + 97.0
        self.engine._damage_towers([enemy], 1.0)
        self.assertEqual(tower["hp"], 100.0)

    def test_swept_tower_contact_applies_only_the_fraction_of_tick_in_range(self):
        self.engine.set_virtual_play(True)
        socket_id = self.engine.level.socket_by_marker[48]
        self.engine.place(100, socket_id, source="virtual", team="green")
        tower = self.engine.placements[socket_id]
        tower["hp"] = tower["max_hp"] = 100.0
        enemy = {
            "id": 9,
            "x": tower["x"] + 120.0,
            "y": tower["y"],
            "collision_radius": 0.0,
            "tower_dps": 12.0,
        }
        overlap = _segment_circle_overlap_fraction(
            tower["x"] - 120.0,
            tower["y"],
            enemy["x"],
            enemy["y"],
            tower["x"],
            tower["y"],
            TOWER_ATTACK_RADIUS,
        )
        self.assertAlmostEqual(overlap, 188.0 / 240.0)
        self.engine._damage_towers(
            [enemy],
            1.0,
            {enemy["id"]: (tower["x"] - 120.0, tower["y"])},
        )
        self.assertAlmostEqual(tower["hp"], 100.0 - 12.0 * overlap)

    def test_tower_damage_multiplier_is_independent_from_core_damage(self):
        self.engine.settings.update({
            "enemy_core_damage_multiplier": 0.25,
            "enemy_tower_damage_multiplier": 1.5,
        })
        lane = next(iter(self.engine.level.paths))
        self.assertTrue(self.engine._spawn_enemy("grunt", {lane: 1.0}))
        enemy = next(iter(self.engine.enemies.values()))
        self.assertEqual(enemy["core_dps"], ENEMY_STATS["grunt"]["core_dps"] * 0.25)
        self.assertEqual(enemy["tower_dps"], ENEMY_STATS["grunt"]["core_dps"] * 1.5)

    def test_tower_health_stages_use_exact_smoke_burn_and_destroyed_thresholds(self):
        self.engine.set_virtual_play(True)
        socket_id = self.engine.level.socket_by_marker[48]
        self.engine.place(100, socket_id, source="virtual", team="green")
        tower = self.engine.placements[socket_id]
        tower["max_hp"] = 100.0
        for hp, expected in (
            (50.0, "normal"),
            (49.99, "stressed"),
            (30.0, "stressed"),
            (29.99, "smoking"),
            (10.0, "smoking"),
            (9.99, "burning"),
        ):
            tower["hp"] = hp
            tower["destroyed"] = False
            self.assertEqual(self.engine._tower_health_stage(tower), expected)
        tower["hp"] = 0.0
        tower["destroyed"] = True
        self.assertEqual(self.engine._tower_health_stage(tower), "destroyed")

    def test_reapplying_atom_replenishes_defense_and_connected_field(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        first_socket = marker[48]
        second_socket = marker[49]
        self.engine.place(100, first_socket, source="virtual", team="green")
        self.engine.place(101, second_socket, source="virtual", team="green")
        self.engine.start({"force_field_hit_capacity": 50})
        tower = self.engine.placements[first_socket]
        field = next(iter(self.engine.force_fields.values()))
        tower["hp"] = tower["max_hp"] / 3
        field.update({"hits": 49, "broken": True, "last_hit_at": 4.0, "broken_at": 4.0})
        field["impacted_enemy_ids"].update({1, 2, 3})

        self.engine.place(102, first_socket, source="virtual", team="purple")

        self.assertEqual(len(self.engine.placements), 2)
        self.assertEqual(self.engine.activation_order, [first_socket, second_socket])
        self.assertEqual(tower["atom_tag_id"], 100)
        self.assertEqual(tower["tower_type"], "machine_gun")
        self.assertEqual(tower["hp"], tower["max_hp"])
        replenished = next(iter(self.engine.force_fields.values()))
        self.assertEqual(replenished["hits"], 0)
        self.assertFalse(replenished["broken"])
        self.assertIsNone(replenished["last_hit_at"])
        self.assertIsNone(replenished["broken_at"])
        self.assertEqual(replenished["impacted_enemy_ids"], set())

    def test_weapon_aiming_burn_duration_and_mortar_falloff(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        self.engine.place(101, marker[48], source="virtual", team="green")
        self.engine.start({"flamethrower_burn_duration_s": 3.0})
        tower = self.engine.placements[marker[48]]
        targeting = self.engine._tower_targeting(tower)
        enemy = {
            "id": 8001,
            "x": tower["x"] + math.cos(targeting["sweep_angle"]) * 80,
            "y": tower["y"] + math.sin(targeting["sweep_angle"]) * 80,
            "hp": 1000.0, "progress": 0.5, "burn_until": 0.0,
            "burn_damage_per_s": 0.0,
        }
        self.engine.enemies = {enemy["id"]: enemy}
        self.engine._fire_towers(0.1, set())
        self.assertAlmostEqual(enemy["burn_until"], self.engine.sim_time + 3.0)
        self.assertEqual(enemy["hp"], 991.0)
        self.assertEqual(enemy["burn_damage_per_s"], 3.6)

        mortar = DefenseEngine(MAP_PATH, WAVES_PATH)
        mortar.set_virtual_play(True)
        mortar.place(102, marker[41], source="virtual", team="purple")
        mortar.start()
        mortar.set_tower_aim(102, 90, 0.0)
        near = mortar._tower_targeting(mortar.placements[marker[41]])
        mortar.set_tower_aim(102, 90, 1.0)
        far = mortar._tower_targeting(mortar.placements[marker[41]])
        self.assertGreater(far["blast_radius"], near["blast_radius"])
        self.assertLess(far["damage_multiplier"], near["damage_multiplier"])

    def test_machine_gun_snapshot_tracks_one_target_for_dual_barrel_visuals(self):
        self.engine.set_virtual_play(True)
        socket_id = self.engine.level.socket_by_marker[48]
        self.engine.place(100, socket_id, source="virtual", team="green")
        self.engine.start({"machine_gun_damage": 31.0})
        tower = self.engine.placements[socket_id]
        targeting = self.engine._tower_targeting(tower)
        enemy = {
            "id": 8100,
            "x": tower["x"] + math.cos(targeting["angle"]) * 80.0,
            "y": tower["y"] + math.sin(targeting["angle"]) * 80.0,
            "hp": 1000.0,
            "progress": 0.5,
            "burn_until": 0.0,
            "burn_damage_per_s": 0.0,
        }
        self.engine.enemies = {enemy["id"]: enemy}

        self.engine._fire_towers(0.1, set())

        self.assertEqual(enemy["hp"], 972.1)
        target = self.engine._public_tower(tower)["last_fire_target"]
        self.assertEqual(target["kind"], "machine_gun")
        self.assertEqual(target["enemy_id"], enemy["id"])
        self.assertEqual((target["x"], target["y"]), (enemy["x"], enemy["y"]))

    def test_mortar_damage_waits_for_shell_impact(self):
        self.engine.set_virtual_play(True)
        socket_id = self.engine.level.socket_by_marker[41]
        self.engine.place(102, socket_id, source="virtual", team="purple")
        self.engine.start()
        tower = self.engine.placements[socket_id]
        targeting = self.engine._tower_targeting(tower)
        enemy = {
            "id": 9101,
            "x": targeting["target_x"],
            "y": targeting["target_y"],
            "hp": 1000.0,
            "progress": 0.5,
            "burn_until": 0.0,
            "burn_damage_per_s": 0.0,
        }
        self.engine.enemies = {enemy["id"]: enemy}
        self.engine._fire_towers(0.1, set())
        self.assertEqual(enemy["hp"], 1000.0)
        self.assertEqual(len(self.engine.pending_mortar_rounds), 1)
        self.assertAlmostEqual(
            self.engine.pending_mortar_rounds[0]["damage"],
            float(self.engine.settings["mortar_damage"])
            * float(targeting["damage_multiplier"])
            * 0.9,
        )
        self.engine.sim_time += 0.89
        self.engine._resolve_mortar_rounds(set())
        self.assertEqual(enemy["hp"], 1000.0)
        self.engine.sim_time += 0.02
        self.engine._resolve_mortar_rounds(set())
        self.assertLess(enemy["hp"], 1000.0)
        self.assertEqual(len(self.engine.mortar_impacts), 1)

    def test_tesla_chain_respects_gap_depth_and_progressive_falloff(self):
        self.engine.set_virtual_play(True)
        socket_id = self.engine.level.socket_by_marker[50]
        self.engine.place(103, socket_id, source="virtual", team="purple")
        self.engine.start({
            "tesla_damage": 100.0,
            "tesla_link_distance": 45.0,
            "tesla_max_links": 3,
        })
        tower = self.engine.placements[socket_id]
        enemies = {}
        for index, offset in enumerate((40.0, 80.0, 120.0, 190.0), start=1):
            enemies[index] = {
                "id": index,
                "x": tower["x"] + offset,
                "y": tower["y"],
                "hp": 1000.0,
                "progress": 1.0 - index * 0.01,
                "burn_until": 0.0,
                "burn_damage_per_s": 0.0,
            }
        self.engine.enemies = enemies
        self.engine._fire_towers(0.1, set())
        chain = tower["last_fire_chain"]
        self.assertEqual([link["enemy_id"] for link in chain], [1, 2, 3])
        self.assertEqual(enemies[1]["hp"], 910.0)
        self.assertGreater(enemies[2]["hp"], enemies[1]["hp"])
        self.assertGreater(enemies[3]["hp"], enemies[2]["hp"])
        self.assertEqual(enemies[4]["hp"], 1000.0)

    def test_tesla_reach_trades_range_for_damage_and_visual_strength(self):
        self.engine.set_virtual_play(True)
        socket_id = self.engine.level.socket_by_marker[50]
        self.engine.place(103, socket_id, source="virtual", team="purple")
        tower = self.engine.placements[socket_id]

        maximum = self.engine._tower_targeting(tower)
        self.assertEqual(maximum["range"], 265.0)
        self.assertEqual(maximum["damage_multiplier"], 1.0)
        self.assertAlmostEqual(maximum["visual_intensity"], 0.58)

        revision = tower["aim_revision"]
        self.engine.set_tower_aim(103, 270.0, 0.0, socket_id=socket_id)
        minimum = self.engine._tower_targeting(tower)
        self.assertEqual(minimum["range"], 120.0)
        self.assertEqual(minimum["damage_multiplier"], 1.75)
        self.assertEqual(minimum["visual_intensity"], 1.0)
        self.assertEqual(tower["aim_revision"], revision + 1)

        self.engine.settings["tesla_damage"] = 100.0
        self.engine.settings["tesla_max_links"] = 1
        enemy = {
            "id": 1,
            "x": tower["x"] + 60.0,
            "y": tower["y"],
            "hp": 1000.0,
            "progress": 0.5,
            "burn_until": 0.0,
            "burn_damage_per_s": 0.0,
        }
        self.engine.enemies = {enemy["id"]: enemy}
        self.engine._fire_towers(0.1, set())
        self.assertEqual(enemy["hp"], 842.5)
        self.assertEqual(tower["last_fire_chain"][0]["intensity"], 1.0)

    def test_tesla_charge_builds_deterministically_between_shots(self):
        self.engine.set_virtual_play(True)
        socket_id = self.engine.level.socket_by_marker[50]
        self.engine.place(103, socket_id, source="virtual", team="purple")
        tower = self.engine.placements[socket_id]

        ready = self.engine._public_tower(tower)
        self.assertEqual(ready["charge_duration_s"], 1.0)
        self.assertEqual(ready["weapon_charge"], 1.0)

        tower["cooldown"] = 1.0
        self.assertEqual(self.engine._public_tower(tower)["weapon_charge"], 0.0)
        tower["cooldown"] = 0.62
        self.assertEqual(self.engine._public_tower(tower)["weapon_charge"], 0.38)
        tower["cooldown"] = 0.0
        self.assertEqual(self.engine._public_tower(tower)["weapon_charge"], 1.0)

    def test_flamethrower_path_rebuilds_immediately_from_current_aim(self):
        self.engine.set_virtual_play(True)
        socket_id = self.engine.level.socket_by_marker[48]
        self.engine.place(101, socket_id, source="virtual", team="green")
        tower = self.engine.placements[socket_id]
        self.engine.sim_time = 3.125

        self.engine.set_tower_aim(101, 0.0, 0.75, socket_id=socket_id)
        forward = self.engine._flamethrower_path(
            tower, self.engine._tower_targeting(tower)
        )
        self.engine.set_tower_aim(101, 180.0, 0.75, socket_id=socket_id)
        reversed_path = self.engine._flamethrower_path(
            tower, self.engine._tower_targeting(tower)
        )

        self.assertEqual(len(forward), 19)
        self.assertEqual(len(reversed_path), 19)
        self.assertEqual(FLAMETHROWER_MUZZLE_OFFSET, 35.0)
        self.assertAlmostEqual(
            math.hypot(
                forward[0][0] - tower["x"],
                forward[0][1] - tower["y"],
            ),
            FLAMETHROWER_MUZZLE_OFFSET,
        )
        forward_arc_length = math.hypot(
            forward[0][0] - tower["x"],
            forward[0][1] - tower["y"],
        ) + sum(
            math.hypot(second[0] - first[0], second[1] - first[1])
            for first, second in zip(forward, forward[1:])
        )
        self.assertAlmostEqual(
            forward_arc_length,
            self.engine._tower_targeting(tower)["range"],
        )
        for first, second in zip(forward, reversed_path):
            self.assertAlmostEqual(first[0] - tower["x"], -(second[0] - tower["x"]), places=6)
            self.assertAlmostEqual(first[1] - tower["y"], -(second[1] - tower["y"]), places=6)

    def test_orcs_advance_attack_center_and_never_exceed_cap(self):
        self.engine.start({
            "wave_count": 1,
            "enemy_speed_multiplier": 50.0,
            "enemy_core_damage_multiplier": 20.0,
            "release_rate_multiplier": 0.05,
        })
        self.engine.step(0.05)
        origin = tuple(self.engine.snapshot()["enemies"][0][key] for key in ("x", "y"))
        for _ in range(80):
            self.engine.step(0.1)
        state = self.engine.snapshot()
        self.assertTrue(any(tuple(enemy[key] for key in ("x", "y")) != origin for enemy in state["enemies"]))
        self.assertLess(state["core_hp"], state["core_max_hp"])

        capped = DefenseEngine(MAP_PATH, WAVES_PATH)
        capped.start({"wave_count": 1, "max_active_enemies": 10, "release_rate_multiplier": 1000.0})
        capped.step(0.1)
        capped_state = capped.snapshot()
        self.assertLessEqual(capped_state["active_enemies"], 10)
        self.assertGreater(capped_state["pressure_bank"], 0)
        pressure_before = capped_state["pressure_bank"]
        capped.enemies.clear()
        capped.step(0.1)
        drained = capped.snapshot()
        self.assertGreater(drained["active_enemies"], 0)
        self.assertLessEqual(drained["active_enemies"], 10)
        self.assertLess(drained["pressure_bank"], pressure_before)

    def test_orcs_keep_personal_space_and_surround_the_core(self):
        self.engine.start({
            "wave_count": 1,
            "enemy_speed_multiplier": 4.0,
            "release_rate_multiplier": 1000.0,
            "max_active_enemies": 40,
            "core_hp": 100000.0,
        })
        closest_body_clearance = math.inf
        for _ in range(200):
            self.engine.step(0.1)
            enemies = self.engine.snapshot()["enemies"]
            for index, enemy in enumerate(enemies):
                for other in enemies[index + 1:]:
                    distance = math.hypot(enemy["x"] - other["x"], enemy["y"] - other["y"])
                    minimum = enemy["collision_radius"] + other["collision_radius"] + COLLISION_PADDING
                    self.assertGreaterEqual(distance + 0.02, minimum)
                    closest_body_clearance = min(
                        closest_body_clearance,
                        distance - enemy["collision_radius"] - other["collision_radius"],
                    )

        self.assertLess(closest_body_clearance, 0.5)
        self.assertGreaterEqual(
            len({enemy["track"] for enemy in self.engine.enemies.values()}), 5
        )

        attackers = [enemy for enemy in self.engine.snapshot()["enemies"] if enemy["attacking"]]
        self.assertGreaterEqual(len(attackers), 3)
        self.assertEqual(len({(enemy["x"], enemy["y"]) for enemy in attackers}), len(attackers))
        attacker_positions = {enemy["id"]: (enemy["x"], enemy["y"]) for enemy in attackers}
        for _ in range(10):
            self.engine.step(0.1)
        flowing = [
            enemy for enemy in self.engine.snapshot()["enemies"]
            if enemy["id"] in attacker_positions and enemy["attacking"]
            and math.hypot(
                enemy["x"] - attacker_positions[enemy["id"]][0],
                enemy["y"] - attacker_positions[enemy["id"]][1],
            ) > 1.0
        ]
        self.assertTrue(flowing)

        breaches_before = self.engine.breaches
        for enemy in flowing[:3]:
            self.engine.enemies[enemy["id"]]["hp"] = 0.0
        for _ in range(100):
            self.engine.step(0.1)
        self.assertGreater(self.engine.breaches, breaches_before)

    def test_full_wave_drains_routes_into_particle_basin(self):
        dense = DefenseEngine(MAP_PATH, WAVES_PATH)
        dense.start({
            "wave_count": 1,
            "release_rate_multiplier": 1000.0,
            "max_active_enemies": 1000,
            "core_hp": 1_000_000_000.0,
        })
        transition_count = 0
        for _ in range(400):
            before = {
                enemy["id"]: (enemy["x"], enemy["y"], enemy["attacking"])
                for enemy in dense.enemies.values()
            }
            dense.step(0.1)
            for enemy in dense.enemies.values():
                previous = before.get(enemy["id"])
                if previous is None or previous[2] or not enemy["attacking"]:
                    continue
                jump = math.hypot(enemy["x"] - previous[0], enemy["y"] - previous[1])
                maximum_continuous_step = (
                    enemy["speed"] * 0.1 * PARTICLE_MAX_SPEED_SCALE + 1.0
                )
                self.assertLessEqual(jump, maximum_continuous_step)
                transition_count += 1
        state = dense.snapshot()
        self.assertEqual(state["active_enemies"], 118)
        self.assertEqual(state["breaches"], 118)
        self.assertEqual(transition_count, state["breaches"])
        self.assertTrue(all(enemy["attacking"] for enemy in state["enemies"]))
        self.assertLess(max(enemy["blocked_steps"] for enemy in dense.enemies.values()), 10)

    def test_center_particle_basin_holds_four_times_the_old_ring_capacity(self):
        dense = DefenseEngine(MAP_PATH, WAVES_PATH)
        dense.start({
            "wave_count": 2,
            "wave_interval_s": 0.1,
            "enemy_speed_multiplier": 4.0,
            "release_rate_multiplier": 1000.0,
            "max_active_enemies": 1000,
            "core_hp": 1_000_000_000_000.0,
        })
        for _ in range(180):
            dense.step(0.1)

        attackers = [enemy for enemy in dense.enemies.values() if enemy["attacking"]]
        self.assertGreaterEqual(len(attackers), 280)
        self.assertEqual(len(attackers), dense.breaches)
        self.assertFalse([enemy for enemy in dense.enemies.values() if not enemy["attacking"]])

        core_x, core_y = float(dense.level.core["x"]), float(dense.level.core["y"])
        square_radii = set()
        face_names = (
            "right", "left", "bottom", "top",
            "bottom-right", "top-left", "top-right", "bottom-left",
        )
        self.assertEqual(
            sum(
                normal_x != 0.0 and normal_y != 0.0
                for normal_x, normal_y, _ in CORE_OCTAGON_PLANES
            ),
            4,
        )
        surface_gaps = {face: [] for face in face_names}
        for enemy in attackers:
            signed_dx, signed_dy = enemy["x"] - core_x, enemy["y"] - core_y
            dx, dy = abs(signed_dx), abs(signed_dy)
            radius = enemy["collision_radius"]
            self.assertLessEqual(max(dx, dy), CORE_BASIN_HALF_SIZE - radius + 0.02)
            face_clearances = [
                (
                    normal_x * signed_dx + normal_y * signed_dy - limit
                ) / math.hypot(normal_x, normal_y) - radius
                for normal_x, normal_y, limit in CORE_OCTAGON_PLANES
            ]
            nearest_face = max(range(len(face_clearances)), key=face_clearances.__getitem__)
            self.assertGreaterEqual(face_clearances[nearest_face] + 0.02, 0.0)
            surface_gaps[face_names[nearest_face]].append(face_clearances[nearest_face])
            square_radii.add(round(max(dx, dy), 1))
        self.assertGreater(len(square_radii), 80)
        for face, gaps in surface_gaps.items():
            self.assertTrue(gaps, f"no orcs reached the {face} face of the core")
            self.assertLessEqual(min(gaps), 0.75, f"empty floor remains on the {face}")

        for index, enemy in enumerate(attackers):
            for other in attackers[index + 1:]:
                distance = math.hypot(enemy["x"] - other["x"], enemy["y"] - other["y"])
                minimum = (
                    enemy["collision_radius"] + other["collision_radius"]
                    + COLLISION_PADDING
                )
                self.assertGreaterEqual(distance + 0.03, minimum)

        before = {enemy["id"]: (enemy["x"], enemy["y"]) for enemy in attackers}
        for _ in range(5):
            dense.step(0.1)
        moved = sum(
            math.hypot(enemy["x"] - before[enemy["id"]][0], enemy["y"] - before[enemy["id"]][1])
            > 0.5
            for enemy in dense.enemies.values()
            if enemy["id"] in before
        )
        self.assertGreater(moved, len(before) * 0.9)

    def test_physical_placement_requires_matching_enabled_released_arm(self):
        tags = [
            {"id": 48, "nx": 0.2, "ny": 0.3, "missing": 0},
            {"id": 100, "nx": 0.2, "ny": 0.3, "missing": 0},
        ]
        arm = {"green": {"connected": True, "enabled": True, "pump_mode": "suck"}}
        self.engine.ingest_physical(tags, arm, now=1.0)
        self.engine.ingest_physical(tags, arm, now=1.6)
        self.assertEqual(self.engine.snapshot()["towers"], [])
        arm["green"]["pump_mode"] = "off"
        self.engine.ingest_physical(tags, arm, now=2.0)
        self.engine.ingest_physical(tags, arm, now=2.6)
        towers = self.engine.snapshot()["towers"]
        self.assertEqual([(tower["atom_tag_id"], tower["aruco_id"]) for tower in towers], [(100, 48)])

        moved_tags = [
            {"id": 48, "nx": 0.2, "ny": 0.3, "missing": 0},
            {"id": 49, "nx": 0.4, "ny": 0.3, "missing": 0},
            {"id": 100, "nx": 0.4, "ny": 0.3, "missing": 0},
        ]
        self.engine.ingest_physical(moved_tags, arm, now=3.0)
        self.engine.ingest_physical(moved_tags, arm, now=3.6)
        towers = self.engine.snapshot()["towers"]
        self.assertEqual(
            [(tower["atom_tag_id"], tower["aruco_id"]) for tower in towers],
            [(100, 48), (100, 49)],
        )


class LaserTagZSettingsTests(unittest.TestCase):
    def test_settings_are_validated_and_snapshotted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SettingsStore(Path(temp_dir) / "settings.json")
            invalid = store.snapshot()
            invalid["max_active_enemies"] = 1001
            invalid["tower_link_start_multiplier"] = 0.0
            invalid["tower_link_step"] = 1.1
            response, errors = store.update(invalid)
            self.assertIsNone(response)
            self.assertIn("max_active_enemies", errors)
            self.assertIn("tower_link_start_multiplier", errors)
            self.assertIn("tower_link_step", errors)

            first = store.snapshot()
            first["enemy_speed_multiplier"] = 0.5
            first["tower_link_start_multiplier"] = 0.75
            first["tower_link_step"] = 0.2
            response, errors = store.update(first)
            self.assertFalse(errors)
            engine = DefenseEngine(MAP_PATH, WAVES_PATH)
            engine.start(store.snapshot())
            second = store.snapshot()
            second["enemy_speed_multiplier"] = 2.0
            second["tower_link_start_multiplier"] = 1.25
            second["tower_link_step"] = 0.4
            store.update(second)
            self.assertEqual(engine.snapshot()["settings"]["enemy_speed_multiplier"], 0.5)
            self.assertEqual(engine.snapshot()["settings"]["tower_link_start_multiplier"], 0.75)
            self.assertEqual(engine.snapshot()["settings"]["tower_link_step"], 0.2)
            self.assertEqual(store.response()["defaults"]["force_field_hit_capacity"], 50)
            self.assertEqual(store.response()["defaults"]["defense_unit_health_percent"], 15.0)
            self.assertEqual(store.response()["defaults"]["enemy_tower_damage_multiplier"], 1.0)
            self.assertEqual(store.response()["defaults"]["flamethrower_burn_duration_s"], 3.0)
            self.assertEqual(store.response()["defaults"]["ring_field_immunity_s"], 100.0)
            self.assertEqual(store.response()["defaults"]["tesla_damage"], 36.0)
            self.assertEqual(store.response()["defaults"]["tesla_link_distance"], 90.0)
            self.assertEqual(store.response()["defaults"]["tesla_max_links"], 10)
            self.assertEqual(
                store.response()["defaults"]["tower_link_start_multiplier"],
                0.9,
            )
            self.assertEqual(
                store.response()["defaults"]["tower_link_step"], 0.1
            )


class LaserTagZDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game_html = (PROTOTYPE / "game.html").read_text(encoding="utf-8")
        cls.settings_html = (PROTOTYPE / "settings.html").read_text(
            encoding="utf-8"
        )
        cls.screen_html = (PROTOTYPE / "screen.html").read_text(encoding="utf-8")
        cls.renderer_js = (PROTOTYPE / "tower-defence-view.js").read_text(encoding="utf-8")
        cls.arm_overlay_js = (PROTOTYPE / "camera-arm-overlay.js").read_text(encoding="utf-8")
        cls.prototype_py = (PROTOTYPE / "prototype.py").read_text(encoding="utf-8")
        cls.runtime_builder_py = (
            ROOT / "tools/build_runtime_weapon_assets.py"
        ).read_text(encoding="utf-8")
        cls.runtime_weapons = json.loads((
            ROOT / "assets/game-art/z-pixel-v2/normalized/runtime-weapons.json"
        ).read_text(encoding="utf-8"))

    def test_physical_and_virtual_modes_use_mutually_exclusive_feeds(self):
        self.assertIn(".stage>img{object-fit:contain}", self.game_html)
        self.assertIn('id="fieldCanvas" class="field-layer"', self.game_html)
        self.assertIn("fieldCanvas:$('fieldCanvas')", self.game_html)
        self.assertIn(".stage.virtual .field-layer", self.game_html)
        self.assertIn(".stage.layout-editing .field-layer", self.game_html)
        self.assertIn(".stage:not(.virtual) .map-layer", self.game_html)
        self.assertIn(".stage:not(.virtual) .game-layer", self.game_html)
        self.assertIn(".stage.virtual>img", self.game_html)
        self.assertIn(".stage.virtual .tracking-layer", self.game_html)
        self.assertIn(".stage.virtual .arm-overlay-layer", self.game_html)
        self.assertIn("if(enabled)stopCameraView()", self.game_html)
        self.assertIn("else{armOverlay.loadCalibration()", self.game_html)
        self.assertIn("defenceView.applyState(state)", self.game_html)

    def test_external_screen_is_clean_and_uses_the_shared_live_renderer(self):
        self.assertIn("Open game screen ↗", self.game_html)
        self.assertIn("$('screenLink').href=`${SELF}/screen`", self.game_html)
        self.assertIn('@bp.route("/screen")', self.prototype_py)
        self.assertIn("tower-defence-view.js", self.screen_html)
        self.assertIn("TowerDefenceView.create", self.screen_html)
        self.assertIn("/api/defence/events", self.screen_html)
        self.assertNotIn("corrected-stream", self.screen_html)
        self.assertNotIn("<button", self.screen_html)
        self.assertNotIn("function renderGame", self.game_html)
        self.assertNotIn("function renderGame", self.screen_html)
        self.assertIn("function renderGame", self.renderer_js)
        self.assertIn("level_revision", self.screen_html)
        self.assertIn("reloadLevel()", self.screen_html)
        self.assertIn("gameState.connections", self.renderer_js)
        self.assertIn("field.visible === true", self.renderer_js)
        self.assertIn("gameState.force_field_visuals", self.renderer_js)
        self.assertIn('gate.visual_state === "preview"', self.renderer_js)
        self.assertIn("function drawForceFields", self.renderer_js)
        self.assertIn("fieldCanvas?.getContext", self.renderer_js)

    def test_link_multiplier_is_visible_in_shared_and_selected_turret_ui(self):
        self.assertIn("function towerLinkMultiplierLabel", self.renderer_js)
        self.assertIn("context.fillText(linkBonus.label", self.renderer_js)
        self.assertIn("tower.linked_turret_count", self.renderer_js)
        self.assertIn("tower.link_multiplier", self.renderer_js)
        self.assertIn("power/health ×${linkMultiplier.toFixed(2)", self.game_html)
        self.assertIn(
            'data-key="tower_link_start_multiplier"', self.settings_html
        )
        self.assertIn('data-key="tower_link_step"', self.settings_html)
        self.assertIn("Single-turret health and damage", self.settings_html)
        self.assertIn("Gain per extra linked turret", self.settings_html)

        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for multiplier UI verification")
        renderer_path = json.dumps(str(PROTOTYPE / "tower-defence-view.js"))
        script = f"""
require({renderer_path});
const label = globalThis.TowerDefenceView.geometry.towerLinkMultiplierLabel;
const single = label({{linked_turret_count: 1, link_multiplier: 0.9}});
const trio = label({{linked_turret_count: 3, link_multiplier: 1.1}});
const custom = label({{linked_turret_count: 3, link_multiplier: 1.15}});
if (single.label !== '×0.9' || single.linkedTurretCount !== 1) process.exit(1);
if (trio.label !== '×1.1' || trio.linkedTurretCount !== 3) process.exit(2);
if (custom.label !== '×1.15') process.exit(3);
"""
        completed = subprocess.run(
            [node, "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_force_fields_are_segmented_around_every_aruco_keepout(self):
        self.assertIn("const ARUCO_FIELD_CLEARANCE = 20", self.renderer_js)
        self.assertIn("function fieldSegmentsOutsideKeepOuts", self.renderer_js)
        self.assertIn("function arucoFieldKeepOuts", self.renderer_js)
        self.assertIn("circleOverlapsKeepOut", self.renderer_js)
        self.assertIn("setDetectedMarkerKeepOuts", self.renderer_js)
        self.assertIn("defenceView.setDetectedMarkerKeepOuts(keepOuts)", self.game_html)
        self.assertIn("scaleX=1696/width,scaleY=960/height", self.game_html)
        self.assertIn("gameState.force_field_impacts", self.renderer_js)
        self.assertIn("function drawForceFieldSkeletonZaps", self.renderer_js)
        self.assertIn('gameImages.get("effect:force-field-zap-skeleton")', self.renderer_js)
        skeleton = (
            ROOT
            / "assets/game-art/z-pixel-v2/normalized/effects/combat"
            / "force-field-zap-skeleton-v1.png"
        )
        self.assertTrue(skeleton.is_file())
        self.assertGreater(skeleton.stat().st_size, 1000)

        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for renderer geometry verification")
        renderer_path = json.dumps(str(PROTOTYPE / "tower-defence-view.js"))
        script = f"""
require({renderer_path});
const geometry = globalThis.TowerDefenceView.geometry;
const keepOuts = [
  geometry.normalizedKeepOut({{left: 450, right: 550, top: 450, bottom: 550}}),
];
const parts = geometry.fieldSegmentsOutsideKeepOuts(0, 500, 1000, 500, keepOuts);
if (parts.length !== 2) process.exit(1);
if (Math.abs(parts[0].end - 0.45) > 1e-9) process.exit(2);
if (Math.abs(parts[1].start - 0.55) > 1e-9) process.exit(3);
const endpointParts = geometry.fieldSegmentsOutsideKeepOuts(
  0, 500, 1000, 500,
  [geometry.normalizedKeepOut({{left: 0, right: 100, top: 450, bottom: 550}})],
);
if (endpointParts.length !== 1 || Math.abs(endpointParts[0].start - 0.1) > 1e-9) process.exit(4);
const fireLines = geometry.machineGunFireLines(100, 100, 0, 200, 150);
if (fireLines.length !== 2) process.exit(5);
if (fireLines[0].ax === fireLines[1].ax && fireLines[0].ay === fireLines[1].ay) process.exit(6);
if (fireLines.some((line) => line.bx !== 200 || line.by !== 150)) process.exit(7);
if (Math.abs((fireLines[0].ay + fireLines[1].ay) / 2 - 100) > 1e-9) process.exit(8);
"""
        completed = subprocess.run(
            [node, "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_gamemaster_can_drag_and_resize_socket_layout_only_in_setup(self):
        self.assertIn("Edit turret positions", self.game_html)
        self.assertIn("/api/defence/layout", self.game_html)
        self.assertIn("const step=event.shiftKey?8:1", self.game_html)
        self.assertIn("Math.round(dx/step)*step", self.game_html)
        self.assertIn("id=\"socketX\"", self.game_html)
        self.assertIn("id=\"socketY\"", self.game_html)
        self.assertIn("id=\"socketSize\"", self.game_html)
        self.assertIn("runtime.state?.phase!=='setup'", self.game_html)
        self.assertIn('@bp.route("/api/defence/layout", methods=["POST"])', self.prototype_py)
        self.assertIn('if "gamemaster" not in _roles()', self.prototype_py)
        self.assertIn('_defence.phase != "setup"', self.prototype_py)

    def test_virtual_atom_click_activation_and_aim_controls_are_role_fixed(self):
        self.assertIn("const ATOM_ROLES={100:{name:'Machine gun'", self.game_html)
        self.assertIn("102:{name:'Mortar'", self.game_html)
        self.assertIn("103:{name:'Tesla coil'", self.game_html)
        self.assertIn("data-atom", self.game_html)
        self.assertIn("data-marker", self.game_html)
        self.assertIn("handleVirtualCanvasClick", self.game_html)
        self.assertIn("id=\"aimDirection\"", self.game_html)
        self.assertIn("id=\"aimReach\"", self.game_html)
        self.assertIn('id="hudCenter" class="hud-center"', self.game_html)
        self.assertIn('id="aimControls" class="aim-controls hud-aim"', self.game_html)
        self.assertLess(self.game_html.index('id="modeBadge"'), self.game_html.index('id="aimControls"'))
        self.assertIn(".aim-controls.hud-aim", self.game_html)
        self.assertIn("/api/defence/aim", self.game_html)
        self.assertIn("click as many camera markers as needed", self.game_html)
        self.assertIn("function chip(container,dataName,id)", self.game_html)
        self.assertIn("setTimeout(saveAim,90)", self.game_html)
        self.assertIn("runtime.aimDraft", self.game_html)
        self.assertIn("socket_id:draft.placementId", self.game_html)
        self.assertIn("repeat to place more", self.game_html)
        self.assertIn("response.tower", self.game_html)
        self.assertNotIn("team:role.owner", self.game_html)

    def test_directional_weapons_damage_effects_and_destroyed_marker_reveal_are_rendered(self):
        self.assertIn("towerRenderAngles", self.renderer_js)
        self.assertIn("smoothTowerAngle", self.renderer_js)
        self.assertIn("flamethrowerVisualAngle", self.renderer_js)
        self.assertIn("flamethrowerVisualAngleAt", self.renderer_js)
        self.assertNotIn("flamethrowerAngleHistory", self.renderer_js)
        self.assertNotIn("historicalFlamethrowerAngle", self.renderer_js)
        self.assertIn("FLAMETHROWER_PATH_SEGMENTS = 18", self.renderer_js)
        self.assertIn("FLAMETHROWER_TRAIL_LAG_S = 0.48", self.renderer_js)
        self.assertIn("FLAMETHROWER_MUZZLE_OFFSET = 35", self.renderer_js)
        self.assertNotIn("FLAMETHROWER_PILOT_MUZZLE_OFFSET", self.renderer_js)
        self.assertIn("function flamethrowerNozzlePoint", self.renderer_js)
        self.assertIn("FLAMETHROWER_PILOT_LAG_S = 0.08", self.renderer_js)
        self.assertIn("drawFlamethrowerPilotFlame", self.renderer_js)
        self.assertIn("visualTime - FLAMETHROWER_PILOT_LAG_S", self.renderer_js)
        self.assertIn("drawCurvedFlame", self.renderer_js)
        self.assertIn('gameImages.get("effect:flame-gasoline")', self.renderer_js)
        self.assertNotIn('gameImages.get("effect:flame-ribbon")', self.renderer_js)
        self.assertNotIn('gameImages.get("effect:flame-jet")', self.renderer_js)
        self.assertIn('gameImages.get("effect:machine-gun-bullet")', self.renderer_js)
        self.assertIn("machineGunFireLines", self.renderer_js)
        self.assertIn("MACHINE_GUN_MUZZLE_HALF_GAP = 7", self.renderer_js)
        self.assertIn("drawMortarEffects", self.renderer_js)
        self.assertIn("drawLightning", self.renderer_js)
        self.assertIn('gameImages.get("effect:tower-smoke")', self.renderer_js)
        self.assertIn('gameImages.get("effect:tower-fire")', self.renderer_js)
        self.assertIn('gameImages.get("effect:tower-stress-cracks")', self.renderer_js)
        self.assertIn('gameImages.get("effect:tower-destruction-blast")', self.renderer_js)
        self.assertIn('gameImages.get("effect:tower-debris")', self.renderer_js)
        self.assertIn("function drawTowerDestruction", self.renderer_js)
        self.assertIn("const flightDuration = 1.1", self.renderer_js)
        self.assertIn("const settledUntil = flightDuration + 3.0", self.renderer_js)
        self.assertIn("(age - settledUntil) / 0.5", self.renderer_js)
        self.assertIn('if (tower.destroyed) {', self.renderer_js)
        self.assertIn('gameImages.get("tower:socket-cover")', self.renderer_js)
        self.assertIn("function livePodVisualSize", self.renderer_js)
        self.assertIn("LIVE_POD_SIZE = 112", self.renderer_js)
        self.assertIn("TOWER_VISUAL_SIZE = 88", self.renderer_js)
        self.assertNotIn("Math.min(152", self.renderer_js)
        self.assertIn('@bp.route("/api/defence/aim", methods=["POST"])', self.prototype_py)
        self.assertIn('return jsonify({"ok": True, "tower": tower})', self.prototype_py)
        self.assertIn("function drawTargetingOverlay", self.renderer_js)
        self.assertIn("function towerPlacementId", self.renderer_js)
        self.assertIn("towerAimPreview.get(towerPlacementId(tower))", self.renderer_js)
        self.assertIn("tower.destroyed", self.renderer_js)

    def test_flamethrower_sprite_and_paths_share_one_centered_nozzle(self):
        self.assertIn('vertical_alignment="center"', self.runtime_builder_py)
        self.assertIn("flamethrower root centerline drifted", self.runtime_builder_py)
        flame = next(
            asset for asset in self.runtime_weapons["assets"]
            if asset["asset_id"] == "runtime/flame-gasoline"
        )
        self.assertEqual(flame["pivot"], [0.0, 0.5])
        self.assertEqual(flame["centerline"], "alpha_centered")

        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for renderer geometry verification")
        renderer_path = json.dumps(str(PROTOTYPE / "tower-defence-view.js"))
        script = f"""
require({renderer_path});
const nozzle = globalThis.TowerDefenceView.geometry.flamethrowerNozzlePoint;
const epsilon = 1e-9;
const directions = [
  [0, 135, 100],
  [Math.PI / 2, 100, 135],
  [Math.PI, 65, 100],
  [-Math.PI / 2, 100, 65],
];
for (const [angle, expectedX, expectedY] of directions) {{
  const point = nozzle(100, 100, angle);
  if (Math.abs(point.x - expectedX) > epsilon) process.exit(1);
  if (Math.abs(point.y - expectedY) > epsilon) process.exit(2);
}}
"""
        completed = subprocess.run(
            [node, "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_tesla_head_and_idle_charge_use_the_v2_runtime_contract(self):
        tesla = next(
            asset for asset in self.runtime_weapons["assets"]
            if asset["asset_id"] == "runtime/tesla-coil-head"
        )
        self.assertTrue(tesla["file"].endswith("tesla-coil-head-v2.png"))
        head_path = ROOT / tesla["file"]
        png = head_path.read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(png[16:20], "big"), 320)
        self.assertEqual(int.from_bytes(png[20:24], "big"), 320)
        self.assertEqual(png[25], 6)
        self.assertIn("TESLA_HEAD_SOURCE", self.runtime_builder_py)
        self.assertIn("tesla-coil-head-source-v2.png", self.runtime_builder_py)
        self.assertIn('type === "tesla_coil" && layer === "head" ? 2 : 1', self.renderer_js)
        self.assertIn("function drawTeslaIdleCharge", self.renderer_js)
        self.assertIn("tower.weapon_charge", self.renderer_js)
        self.assertIn("TESLA_DISCHARGE_FLASH_S = 0.16", self.renderer_js)

        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for renderer geometry verification")
        renderer_path = json.dumps(str(PROTOTYPE / "tower-defence-view.js"))
        script = f"""
require({renderer_path});
const advance = globalThis.TowerDefenceView.geometry.advancedWeaponCharge;
if (Math.abs(advance(0, 1, 0.25) - 0.25) > 1e-9) process.exit(1);
if (Math.abs(advance(0.38, 1, 0.12) - 0.5) > 1e-9) process.exit(2);
if (advance(0.9, 1, 0.5) !== 1) process.exit(3);
if (advance(0.4, 1, -2) !== 0.4) process.exit(4);
"""
        completed = subprocess.run(
            [node, "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_recent_tower_damage_keeps_exact_health_and_a_visible_notch(self):
        self.assertIn("function towerHealthBarMetrics", self.renderer_js)
        self.assertIn("tower.last_damage_at", self.renderer_js)
        self.assertIn("tower.last_damage_amount", self.renderer_js)
        self.assertIn("health.damageNotchWidth", self.renderer_js)
        self.assertIn("TOWER_DAMAGE_FLASH_S = 0.45", self.renderer_js)

        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for health feedback verification")
        renderer_path = json.dumps(str(PROTOTYPE / "tower-defence-view.js"))
        script = f"""
require({renderer_path});
const metrics = globalThis.TowerDefenceView.geometry.towerHealthBarMetrics;
const recent = metrics({{
  hp: 1494.62,
  max_hp: 1500,
  last_damage_at: 100,
  last_damage_amount: 5.38,
}}, 100.1);
if (Math.abs(recent.healthRatio - 1494.62 / 1500) > 1e-12) process.exit(1);
if (!(recent.fillWidth < 68 && recent.fillWidth > 67)) process.exit(2);
if (recent.damageNotchWidth !== 1) process.exit(3);
if (!(recent.damageAlpha > 0 && recent.damageAlpha < 1)) process.exit(4);
const expired = metrics({{
  hp: 1494.62,
  max_hp: 1500,
  last_damage_at: 100,
  last_damage_amount: 5.38,
}}, 100.5);
if (expired.damageAlpha !== 0 || expired.damageNotchWidth !== 0) process.exit(5);
"""
        completed = subprocess.run(
            [node, "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_dense_renderer_uses_cached_sprites_and_adaptive_frame_rate(self):
        self.assertIn("function cachedEnemySprite", self.renderer_js)
        self.assertIn("enemySpriteCache", self.renderer_js)
        self.assertIn("enemyCount >= 800 ? 18", self.renderer_js)
        self.assertIn("extrapolationAge", self.renderer_js)
        self.assertIn("compact_enemies=True", self.prototype_py)

    def test_renderer_uses_real_dictionary_correct_aruco_images(self):
        self.assertIn("/api/defence/aruco/${markerId}.png", self.renderer_js)
        self.assertIn("markerImages.get", self.renderer_js)
        self.assertNotIn("strokeText(`#${properties.aruco_id}`", self.renderer_js)
        self.assertIn("DICT_4X4_50", self.prototype_py)
        self.assertIn("DICT_4X4_100", self.prototype_py)

    def test_arm_overlay_reuses_calibration_and_fails_closed(self):
        self.assertIn('src="camera-arm-overlay.js"', self.game_html)
        self.assertIn("/api/calibration2", self.game_html)
        self.assertIn("/api/defence/arms", self.game_html)
        self.assertIn("setInterval(pollArmState,200)", self.game_html)
        self.assertIn("staleMs:1500", self.game_html)
        self.assertIn("point.pose.set", self.arm_overlay_js)
        self.assertIn("samples.length < 6", self.arm_overlay_js)
        self.assertIn("state.connected !== true", self.arm_overlay_js)
        self.assertIn("performance.now() - receivedAt > staleMs", self.arm_overlay_js)
        self.assertIn('label.textContent = `${side.toUpperCase()} ARM`', self.arm_overlay_js)
        self.assertIn('layer.style.display = "none"', self.arm_overlay_js)
        self.assertIn('layer.style.display = nodes.length ? "" : "none"', self.arm_overlay_js)

    def test_arm_overlay_has_no_robot_command_path(self):
        self.assertNotIn("fetch(", self.arm_overlay_js)
        self.assertNotIn("XMLHttpRequest", self.arm_overlay_js)
        self.assertNotIn("WebSocket", self.arm_overlay_js)
        self.assertNotIn("pump_mode", self.arm_overlay_js)
        self.assertNotIn("/api/move", self.arm_overlay_js)
        self.assertNotIn('method: "POST"', self.arm_overlay_js)


if __name__ == "__main__":
    unittest.main()
