"""Regression checks for the Laser Tag Z vertical slice."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "sandboxes/gamemaster/prototypes/laser-tag-z"
sys.path.insert(0, str(PROTOTYPE))

from photon_defence import DefenseEngine, SettingsStore  # noqa: E402


MAP_PATH = ROOT / "assets/tiled/levels/z-pixel-first-map.tmj"
WAVES_PATH = ROOT / "assets/tiled/levels/z-pixel-first-map.waves.json"


class LaserTagZEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = DefenseEngine(MAP_PATH, WAVES_PATH)

    def test_level_has_exact_fixed_marker_range(self):
        self.assertEqual(sorted(self.engine.level.socket_by_marker), list(range(40, 56)))
        self.assertEqual(len(self.engine.level.sockets), 16)

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
        self.assertIn("selected.source.objectalignment", game_html)
        self.assertIn("tileObjectCenter(socket)", game_html)
        self.assertNotIn("socket.x+socket.width/2", game_html)

    def test_virtual_placements_make_force_field_and_survive_start(self):
        self.engine.set_virtual_play(True)
        sockets = list(self.engine.level.sockets.values())
        available = {
            owner: [item["socket_id"] for item in sockets if item["owner"] == owner]
            for owner in ("green", "purple", "shared")
        }
        for tag, owner, tower_type in (
            (100, "green", "machine_gun"),
            (101, "green", "flamethrower"),
            (102, "purple", "machine_gun"),
            (103, "purple", "mortar"),
        ):
            socket_id = (available[owner] or available["shared"]).pop()
            self.engine.place(
                tag, socket_id, tower_type, source="virtual", team=owner)
        before = self.engine.snapshot()
        self.assertEqual(len(before["towers"]), 4)
        self.assertEqual(len(before["gates"]), 4)
        self.engine.start()
        self.engine.step(0.05)
        after = self.engine.snapshot()
        self.assertTrue(after["virtual_play"])
        self.assertEqual(len(after["towers"]), 4)
        self.assertGreaterEqual(after["active_enemies"], 1)

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
        self.assertEqual(drained["active_enemies"], 10)
        self.assertLess(drained["pressure_bank"], pressure_before)

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


if __name__ == "__main__":
    unittest.main()
