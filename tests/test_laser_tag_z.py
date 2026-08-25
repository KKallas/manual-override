"""Regression checks for the Laser Tag Z vertical slice."""

from __future__ import annotations

import hashlib
import json
import math
import sys
import tempfile
import unittest
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
    FLOW_SPAWN_OFFSETS,
    PARTICLE_MAX_SPEED_SCALE,
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

    def test_level_has_exact_fixed_marker_range(self):
        self.assertEqual(sorted(self.engine.level.socket_by_marker), list(range(40, 56)))
        self.assertEqual(len(self.engine.level.sockets), 16)

    def test_all_socket_visuals_use_the_large_default_footprint(self):
        level = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        sockets = socket_records(level)
        self.assertEqual({item["size"] for item in sockets}, {208.0})
        self.assertGreaterEqual(layout_revision(level), 1)

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
        alignments = []
        for reference in level["tilesets"]:
            tileset_path = (MAP_PATH.parent / reference["source"]).resolve()
            alignments.append(json.loads(tileset_path.read_text(encoding="utf-8"))["objectalignment"])
        self.assertEqual(set(alignments), {"center"})

        socket_layer = next(layer for layer in level["layers"] if layer["name"] == "09 Square Placement Spots (16)")
        for obj in socket_layer["objects"]:
            marker = next(item["value"] for item in obj["properties"] if item["name"] == "aruco_id")
            socket = self.engine.level.sockets[self.engine.level.socket_by_marker[marker]]
            self.assertEqual((socket["x"], socket["y"]), (float(obj["x"]), float(obj["y"])))

        game_html = (PROTOTYPE / "game.html").read_text(encoding="utf-8")
        renderer_js = (PROTOTYPE / "tower-defence-view.js").read_text(encoding="utf-8")
        self.assertIn('src="tower-defence-view.js"', game_html)
        self.assertIn("selected.source.objectalignment", renderer_js)
        self.assertIn("tileObjectCenter(socket)", renderer_js)
        self.assertNotIn("socket.x+socket.width/2", renderer_js)

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
        self.engine.place(102, marker[41], source="virtual", team="purple")
        before = self.engine.snapshot()
        self.assertEqual(
            [(tower["atom_tag_id"], tower["tower_type"]) for tower in before["towers"]],
            [(100, "machine_gun"), (101, "flamethrower"), (102, "mortar")],
        )
        self.assertEqual(before["gates"], [])
        self.assertEqual(len(self.engine.force_fields), 1)
        with self.assertRaises(ValueError):
            self.engine.place(103, marker[50], source="virtual", team="purple")
        self.engine.start()
        self.engine.step(0.05)
        after = self.engine.snapshot()
        self.assertTrue(after["virtual_play"])
        self.assertEqual(len(after["towers"]), 3)
        self.assertEqual(len(after["gates"]), 1)
        self.assertEqual(after["gates"][0]["capacity"], 50)
        self.assertGreaterEqual(after["active_enemies"], 1)

    def test_force_field_counts_unique_impacts_breaks_and_reroutes(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        self.engine.place(100, marker[48], source="virtual", team="green")
        self.engine.place(101, marker[49], source="virtual", team="green")
        self.engine.start({"force_field_hit_capacity": 2})
        field = next(iter(self.engine.force_fields.values()))
        midpoint = ((field["ax"] + field["bx"]) / 2, (field["ay"] + field["by"]) / 2)
        enemies = []
        for enemy_id in (9001, 9002):
            enemies.append({
                "id": enemy_id, "x": midpoint[0], "y": midpoint[1],
                "vx": 60.0, "vy": 0.0, "collision_radius": 2.2,
                "hp": 100.0, "core_dps": 1.0,
            })
        self.engine._handle_force_fields([enemies[0]])
        self.engine._handle_force_fields([enemies[0]])
        self.assertEqual(field["hits"], 1)
        self.assertLess(enemies[0]["vx"], 0)
        self.engine._handle_force_fields([enemies[1]])
        self.assertTrue(field["broken"])
        self.assertTrue(self.engine.snapshot()["gates"][0]["broken"])
        self.engine.sim_time += 0.7
        self.assertEqual(self.engine.snapshot()["gates"], [])

    def test_defense_health_defaults_to_fifteen_percent_and_reserve_repairs(self):
        self.engine.set_virtual_play(True)
        socket_id = self.engine.level.socket_by_marker[48]
        self.engine.place(100, socket_id, source="virtual", team="green")
        self.engine.start({"core_hp": 20000.0, "defense_unit_health_percent": 15.0})
        tower = self.engine.placements[100]
        self.assertEqual((tower["hp"], tower["max_hp"]), (3000.0, 3000.0))
        tower["hp"] = 1.0
        self.engine._damage_towers([{
            "x": tower["x"], "y": tower["y"], "core_dps": 20.0,
        }], 0.1)
        self.assertTrue(tower["destroyed"])
        self.assertEqual(tower["hp"], 0.0)
        self.engine.place(103, socket_id, source="virtual", team="purple")
        self.assertFalse(tower["destroyed"])
        self.assertEqual(tower["hp"], 3000.0)
        self.assertNotIn(103, self.engine.placements)

    def test_weapon_aiming_burn_duration_and_mortar_falloff(self):
        self.engine.set_virtual_play(True)
        marker = self.engine.level.socket_by_marker
        self.engine.place(101, marker[48], source="virtual", team="green")
        self.engine.start({"flamethrower_burn_duration_s": 3.0})
        tower = self.engine.placements[101]
        targeting = self.engine._tower_targeting(tower)
        enemy = {
            "id": 8001,
            "x": tower["x"] + math.cos(targeting["angle"]) * 80,
            "y": tower["y"] + math.sin(targeting["angle"]) * 80,
            "hp": 1000.0, "progress": 0.5, "burn_until": 0.0,
            "burn_damage_per_s": 0.0,
        }
        self.engine.enemies = {enemy["id"]: enemy}
        self.engine._fire_towers(0.1, set())
        self.assertAlmostEqual(enemy["burn_until"], self.engine.sim_time + 3.0)

        mortar = DefenseEngine(MAP_PATH, WAVES_PATH)
        mortar.set_virtual_play(True)
        mortar.place(102, marker[41], source="virtual", team="purple")
        mortar.start()
        mortar.set_tower_aim(102, 90, 0.0)
        near = mortar._tower_targeting(mortar.placements[102])
        mortar.set_tower_aim(102, 90, 1.0)
        far = mortar._tower_targeting(mortar.placements[102])
        self.assertGreater(far["blast_radius"], near["blast_radius"])
        self.assertLess(far["damage_multiplier"], near["damage_multiplier"])

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


class LaserTagZSettingsTests(unittest.TestCase):
    def test_settings_are_validated_and_snapshotted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SettingsStore(Path(temp_dir) / "settings.json")
            invalid = store.snapshot()
            invalid["max_active_enemies"] = 1001
            response, errors = store.update(invalid)
            self.assertIsNone(response)
            self.assertIn("max_active_enemies", errors)

            first = store.snapshot()
            first["enemy_speed_multiplier"] = 0.5
            response, errors = store.update(first)
            self.assertFalse(errors)
            engine = DefenseEngine(MAP_PATH, WAVES_PATH)
            engine.start(store.snapshot())
            second = store.snapshot()
            second["enemy_speed_multiplier"] = 2.0
            store.update(second)
            self.assertEqual(engine.snapshot()["settings"]["enemy_speed_multiplier"], 0.5)
            self.assertEqual(store.response()["defaults"]["force_field_hit_capacity"], 50)
            self.assertEqual(store.response()["defaults"]["defense_unit_health_percent"], 15.0)
            self.assertEqual(store.response()["defaults"]["flamethrower_burn_duration_s"], 3.0)


class LaserTagZDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game_html = (PROTOTYPE / "game.html").read_text(encoding="utf-8")
        cls.screen_html = (PROTOTYPE / "screen.html").read_text(encoding="utf-8")
        cls.renderer_js = (PROTOTYPE / "tower-defence-view.js").read_text(encoding="utf-8")
        cls.arm_overlay_js = (PROTOTYPE / "camera-arm-overlay.js").read_text(encoding="utf-8")
        cls.prototype_py = (PROTOTYPE / "prototype.py").read_text(encoding="utf-8")

    def test_physical_and_virtual_modes_use_mutually_exclusive_feeds(self):
        self.assertIn(".stage>img{object-fit:contain}", self.game_html)
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
        self.assertIn("103:{name:'Reserve / reset'", self.game_html)
        self.assertIn("data-atom", self.game_html)
        self.assertIn("data-marker", self.game_html)
        self.assertIn("handleVirtualCanvasClick", self.game_html)
        self.assertIn("id=\"aimDirection\"", self.game_html)
        self.assertIn("id=\"aimReach\"", self.game_html)
        self.assertIn("/api/defence/aim", self.game_html)
        self.assertIn('@bp.route("/api/defence/aim", methods=["POST"])', self.prototype_py)
        self.assertIn("function drawTargetingOverlay", self.renderer_js)
        self.assertIn("tower.destroyed", self.renderer_js)

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
