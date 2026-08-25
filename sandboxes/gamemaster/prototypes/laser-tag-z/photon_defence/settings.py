"""Validated, Laser Tag Z-only draft settings and built-in presets."""

from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from typing import Any

from .engine import DEFAULT_SETTINGS


RULES = {
    "wave_count": (int, 1, 12),
    "wave_interval_s": (float, 2.0, 600.0),
    "release_rate_multiplier": (float, 0.05, 20.0),
    "enemy_count_multiplier": (float, 0.1, 5.0),
    "enemy_health_multiplier": (float, 0.1, 10.0),
    "enemy_speed_multiplier": (float, 0.1, 5.0),
    "enemy_core_damage_multiplier": (float, 0.0, 10.0),
    "force_field_damage_per_s": (float, 0.0, 500.0),
    "force_field_slow": (float, 0.05, 1.0),
    "force_field_hit_capacity": (int, 1, 10000),
    "machine_gun_damage": (float, 0.0, 1000.0),
    "flamethrower_damage": (float, 0.0, 1000.0),
    "flamethrower_burn_damage_per_s": (float, 0.0, 500.0),
    "flamethrower_burn_duration_s": (float, 0.0, 30.0),
    "mortar_damage": (float, 0.0, 5000.0),
    "mortar_far_damage_multiplier": (float, 0.05, 1.0),
    "defense_unit_health_percent": (float, 1.0, 100.0),
    "core_hp": (float, 100.0, 100000.0),
    "max_active_enemies": (int, 1, 1000),
}

DEFAULTS = {key: DEFAULT_SETTINGS[key] for key in RULES}
PRESETS = {
    "balanced": dict(DEFAULTS),
    "training": {
        **DEFAULTS,
        "wave_count": 6,
        "wave_interval_s": 60.0,
        "release_rate_multiplier": 0.65,
        "enemy_count_multiplier": 0.5,
        "enemy_health_multiplier": 0.75,
        "enemy_speed_multiplier": 0.75,
        "enemy_core_damage_multiplier": 0.5,
        "core_hp": 15000.0,
    },
    "onslaught": {
        **DEFAULTS,
        "wave_interval_s": 28.0,
        "release_rate_multiplier": 1.8,
        "enemy_count_multiplier": 1.35,
        "enemy_health_multiplier": 1.25,
        "enemy_speed_multiplier": 1.15,
        "enemy_core_damage_multiplier": 1.4,
    },
}


def validate_settings(incoming: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    incoming = incoming if isinstance(incoming, dict) else {}
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, (kind, lower, upper) in RULES.items():
        raw = incoming.get(key, DEFAULTS[key])
        try:
            if kind is int:
                numeric = float(raw)
                if not numeric.is_integer():
                    raise ValueError
                value: int | float = int(numeric)
            else:
                value = float(raw)
            if not math.isfinite(float(value)) or value < lower or value > upper:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            errors[key] = f"must be between {lower:g} and {upper:g}"
            continue
        clean[key] = value
    return clean, errors


class SettingsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        self.revision = 1
        self.draft = dict(DEFAULTS)
        self.preset = "balanced"
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            clean, errors = validate_settings(data.get("settings") or {})
            if errors:
                return
            self.draft = clean
            self.preset = str(data.get("preset") or "custom")[:40]
            self.revision = max(1, int(data.get("revision", 1)))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "schema_version": 1,
            "revision": self.revision,
            "preset": self.preset,
            "settings": self.draft,
        }
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.draft)

    def response(self) -> dict[str, Any]:
        with self.lock:
            return {
                "ok": True,
                "revision": self.revision,
                "preset": self.preset,
                "settings": dict(self.draft),
                "defaults": dict(DEFAULTS),
                "presets": {key: dict(value) for key, value in PRESETS.items()},
                "limits": {
                    key: {"min": lower, "max": upper, "integer": kind is int}
                    for key, (kind, lower, upper) in RULES.items()
                },
            }

    def update(self, incoming: dict[str, Any], preset: str | None = None) -> tuple[dict[str, Any] | None, dict[str, str]]:
        candidate = PRESETS.get(preset) if preset in PRESETS else incoming
        clean, errors = validate_settings(candidate or {})
        if errors:
            return None, errors
        with self.lock:
            self.draft = clean
            self.preset = str(preset or "custom")[:40]
            self.revision += 1
            self._save_locked()
            return self.response(), {}

    def reset_defaults(self) -> dict[str, Any]:
        response, errors = self.update(dict(DEFAULTS), "balanced")
        if errors or response is None:
            raise RuntimeError("default settings failed validation")
        return response
