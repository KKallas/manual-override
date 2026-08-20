"""
Auto Pick and Place game — gamemaster master controller.

Two players (green + purple), each independent. Player controllers ask for a
name before registering. The operator can calibrate robot poses against
ArUco markers rendered by Playfield Areas, then click the webcam view to map a
detected screen location into a robot cartesian move. Every finished run is
appended to ``auto-pickup-log.csv``.

Player controllers (green/purple sandboxes) drive their own arm via
joint-angles-test and forward name/start here; only the gamemaster can mark a
run finished.
"""

import csv
import datetime as _dt
import io
import json
import math
import os
import sys
import threading
import time
import zipfile

from flask import Blueprint, Response, jsonify, request, send_from_directory

import live

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY_DIR = os.path.join(os.path.dirname(HERE), "dobot-mg400-relay")
if RELAY_DIR not in sys.path:
    sys.path.insert(0, RELAY_DIR)
from relay_arm import DobotMG400, DobotError  # noqa: E402

LOG_PATH = os.path.join(HERE, "auto-pickup-log.csv")
CALIBRATION_PATH = os.path.join(HERE, "auto-calibration.json")
CALIBRATION2_PATH = os.path.join(HERE, "auto-calibration-2.json")
FIXED_CAL2_PLAYFIELD_PATH = os.path.join(
    HERE, "fixed-auto-pp-cal-2-playfield.json")
PLAYFIELD_SNAPSHOT_PATH = os.path.join(HERE, "auto-playfield.json")
HIGHSCORE_RESET_PATH = os.path.join(HERE, "auto-highscore-reset.json")
LOG_FIELDS = [
    "logged_at", "player", "team", "elapsed_seconds",
    "started_at", "completed_at",
]
TEAMS = ("green", "purple")
CALIBRATION_POINTS = (
    ("top_left", "Top left", 0.25, 0.25, 20),
    ("top_right", "Top right", 0.75, 0.25, 21),
    ("bottom_right", "Bottom right", 0.75, 0.75, 22),
    ("bottom_left", "Bottom left", 0.25, 0.75, 23),
)
PLAYFIELD_MARKERS = CALIBRATION_POINTS + (
    ("center", "Center", 0.5, 0.5, 24),
)
CORNER_KEYS = tuple(key for key, _label, _u, _v, _marker in CALIBRATION_POINTS)
CORNER_MARKERS = tuple(marker for _key, _label, _u, _v, marker in CALIBRATION_POINTS)
CORNERS_BY_MARKER = {
    20: (0.0, 0.0), 21: (1.0, 0.0),
    22: (1.0, 1.0), 23: (0.0, 1.0),
}
CENTER_MARKER = 24
CALIBRATION_TAG_SIZE = 2.8
CALIBRATION_TAG_SPAN_X = 7.5
CALIBRATION_TAG_SPAN_Z = 4.0
ROBOT_IP = "192.168.1.6"
DEFAULT_LINKS = {
    "purple": {"local_ip": "192.168.1.50"},
    "green": {"local_ip": "192.168.1.51"},
}
SUCK_DO_INDEX = 2
BLOW_DO_INDEX = 1
DIRECT_MAX_LIN_VEL = 20.0
DIRECT_MAX_ANG_VEL = 9.0
DIRECT_RAMP_SECS = 0.50
WORKSPACE = {
    "x": [-450.0, 450.0],
    "y": [-450.0, 450.0],
    "z": [-150.0, 230.0],
    "r": [-160.0, 160.0],
}
RADIUS_MIN = 150.0
RADIUS_MAX = 440.0
# Auto PP applies a stricter 200..400 mm motion ring than the robot's raw
# 150..440 mm workspace. Keep Cal 2 targets another 20 mm inside that ring.
CAL2_RADIUS_MIN = 220.0
CAL2_RADIUS_MAX = 380.0
CAL2_MARKERS = tuple(range(30, 36))
CAL2_PARALLAX_MARKERS = (31, 35, 33, 34)

MANIFEST = {
    "name": "Auto Pick and Place",
    "description": "Auto pick-and-place game: each player enters a name, "
                   "the gamemaster marks finishes, all logged to CSV.",
    "default_page": "game",
    "pages": [{"path": "game", "label": "Auto Pick and Place"}],
}
bp = Blueprint("auto_pickup_game", __name__)

_state_lock = threading.Lock()
_calibration_lock = threading.Lock()
_live = live.LiveState()
_hub_ctx = None
_direct_robots = {side: None for side in TEAMS}
_direct_locks = {side: threading.Lock() for side in TEAMS}
_calibration_access = {
    side: {"enabled": False, "updated_at": None}
    for side in TEAMS
}


def _team_state():
    return {
        "player": "",
        "phase": "idle",            # idle | ready | running | done
        "started_at": None,         # epoch seconds
        "completed_at": None,
        "elapsed_seconds": None,
    }


_state = {
    "teams": {t: _team_state() for t in TEAMS},
    "best_seconds": None,
    "best_player": "",
    "best_team": "",
    "highscore_reset_at": 0.0,
    "updated_at": time.time(),
}


def _empty_pose():
    return {"set": False, "x": None, "y": None, "z": None, "r": None}


def _empty_cal2_parallax_points():
    return {
        str(marker): {"marker": marker, "ground": None, "raised": None}
        for marker in CAL2_PARALLAX_MARKERS
    }


def _clean_cal2_parallax_sample(marker, value):
    try:
        marker = int(marker)
        if marker not in CAL2_PARALLAX_MARKERS or not isinstance(value, dict):
            raise ValueError
        ground, raised = value["ground"], value["raised"]
        sample = {
            "marker": marker,
            "ground": {
                "u": round(float(ground["u"]), 8),
                "v": round(float(ground["v"]), 8),
            },
            "raised": {
                "u": round(float(raised["u"]), 8),
                "v": round(float(raised["v"]), 8),
            },
        }
        if not all(
            0 <= sample[level][axis] <= 1
            for level in ("ground", "raised") for axis in ("u", "v")
        ):
            raise ValueError
        return sample
    except (KeyError, TypeError, ValueError):
        raise ValueError("valid marker, ground and raised camera coordinates required")


def _default_calibration():
    return {
        "playfield": {
            "active_area": {"sMin": 0.0, "sMax": 1.0, "tMin": 0.0, "tMax": 1.0},
            "marker_cache": None,
            "areas": {
                key: {
                    "key": key,
                    "label": label,
                    "u": u,
                    "v": v,
                    "marker": marker,
                    "area_id": None,
                }
                for key, label, u, v, marker in PLAYFIELD_MARKERS
            },
            "corner_map": {
                key: marker for key, _label, _u, _v, marker in CALIBRATION_POINTS
            },
            "updated_at": None,
        },
        "arms": {
            side: {
                "points": {
                    key: {
                        "key": key,
                        "label": label,
                        "u": u,
                        "v": v,
                        "marker": marker,
                        "pose": _empty_pose(),
                    }
                    for key, label, u, v, marker in CALIBRATION_POINTS
                },
                "center": _empty_pose(),
                "corner_map": {
                    key: marker for key, _label, _u, _v, marker in CALIBRATION_POINTS
                },
                "marker_cache": None,
                "pickup_height": {"set": False, "z": None},
                "transport_height": {"set": False, "z": None},
                "updated_at": None,
            }
            for side in TEAMS
        },
    }


_calibration = _default_calibration()


def _default_calibration2():
    targets = {
        f"p{i + 1}": {
            "key": f"p{i + 1}", "label": f"Point {i + 1}",
            "marker": marker, "s": None, "t": None, "area_id": None,
        }
        for i, marker in enumerate(CAL2_MARKERS)
    }
    return {
        "format": "auto-pp-calibration-2-v1",
        "shared_z": {"transport_height": None},
        "playfield": {
            "active_area": {"sMin": 0.0, "sMax": 1.0, "tMin": 0.0, "tMax": 1.0},
            "targets": targets,
            "saved_areas": None,
            "saved_settings": None,
            "saved_camera_view": "normal",
            "updated_at": None,
        },
        "arms": {
            side: {
                "camera_view": "normal",
                "parallax_points": _empty_cal2_parallax_points(),
                "pickup_height": None,
                # Raise a drop by one physical tag when its destination is
                # another 100-series tag. Each arm can tune this separately.
                "stack_drop_offset": 31.5,
                "points": {
                    key: {**target, "camera": None, "pose": _empty_pose()}
                    for key, target in targets.items()
                },
                "updated_at": None,
            }
            for side in TEAMS
        },
    }


_calibration2 = _default_calibration2()


def _roles():
    return request.environ.get("hhh.roles") or set()


@bp.route("/api/laser-intent", methods=["POST"])
def api_laser_intent():
    """Forward Green Auto PP X intentions to the active Laser Tag run."""
    if "green" not in _roles() and "gamemaster" not in _roles():
        return jsonify({"ok": False, "error": "green role required"}), 403
    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind") or "")[:80]
    laser = _hub_ctx.get_prototype("laser-tag") if _hub_ctx is not None else None
    if laser is None or not hasattr(laser, "append_external_event"):
        return jsonify({"ok": False, "error": "Laser Tag logging unavailable"}), 503
    event, error = laser.append_external_event(
        "green_auto_pp_x", kind, data.get("detail"), category=data.get("category")
    )
    if error:
        status = 409 if error == "no active Laser Tag run" else 400
        return jsonify({"ok": False, "error": error}), status
    return jsonify({"ok": True, "event": event})


@bp.route("/api/laser-tag-x-intent", methods=["POST"])
def api_laser_tag_x_intent():
    """Forward the authenticated player's LTX intentions to Laser Tag X."""
    if not ({"green", "purple", "gamemaster"} & _roles()):
        return jsonify({"ok": False, "error": "player or gamemaster required"}), 403
    data = request.get_json(silent=True) or {}
    side = _requested_team(data)
    if side not in TEAMS:
        return jsonify({"ok": False, "error": "green or purple team required"}), 400
    kind = str(data.get("kind") or "")[:80]
    detail = dict(data.get("detail")) if isinstance(data.get("detail"), dict) else {}
    detail["team"] = side
    laser = _hub_ctx.get_prototype("laser-tag-x") if _hub_ctx is not None else None
    if laser is None or not hasattr(laser, "append_external_event"):
        return jsonify({"ok": False, "error": "Laser Tag X logging unavailable"}), 503
    event, error = laser.append_external_event(
        f"{side}_ltx", kind, detail, category=data.get("category")
    )
    if error:
        status = 409 if error == "no active Laser Tag X run" else 400
        return jsonify({"ok": False, "error": error}), status
    return jsonify({"ok": True, "event": event})


@bp.route("/api/ltx-config")
def api_ltx_config():
    """Expose the gamemaster's Laser Tag X game mode to both players."""
    if not ({"green", "purple", "gamemaster"} & _roles()):
        return jsonify({"ok": False, "error": "player or gamemaster required"}), 403
    laser = _hub_ctx.get_prototype("laser-tag-x") if _hub_ctx is not None else None
    if laser is None or not hasattr(laser, "ltx_config_snapshot"):
        return jsonify({"ok": False, "error": "Laser Tag X configuration unavailable"}), 503
    return jsonify({"ok": True, **laser.ltx_config_snapshot()})


@bp.route("/api/ltx-state")
def api_ltx_state():
    """Expose read-only Laser Tag X timing/game state to both players."""
    if not ({"green", "purple", "gamemaster"} & _roles()):
        return jsonify({"ok": False, "error": "player or gamemaster required"}), 403
    laser = _hub_ctx.get_prototype("laser-tag-x") if _hub_ctx is not None else None
    if laser is None or not hasattr(laser, "ltx_player_state_snapshot"):
        return jsonify({"ok": False, "error": "Laser Tag X state unavailable"}), 503
    return jsonify({"ok": True, "state": laser.ltx_player_state_snapshot()})


@bp.route("/api/ltx-arms")
def api_ltx_arms():
    """Expose both compact live LTX arm poses to authenticated players."""
    if not ({"green", "purple", "gamemaster"} & _roles()):
        return jsonify({"ok": False, "error": "player or gamemaster required"}), 403
    laser = _hub_ctx.get_prototype("laser-tag-x") if _hub_ctx is not None else None
    if laser is None or not hasattr(laser, "ltx_player_arms_snapshot"):
        return jsonify({"ok": False, "error": "Laser Tag X arm tracking unavailable"}), 503
    return jsonify({"ok": True, **laser.ltx_player_arms_snapshot()})


def _is_operator():
    return "gamemaster" in _roles()


def _player_side():
    roles = _roles()
    if "green" in roles:
        return "green"
    if "purple" in roles:
        return "purple"
    return ""


def _referrer_side():
    ref = request.headers.get("Referer") or request.headers.get("Referrer") or ""
    for side in TEAMS:
        if f"/s/{side}/" in ref:
            return side
    return ""


def _requested_team(data):
    """The team the caller is allowed to act on: operator may name any team,
    a player only their own."""
    requested = data.get("team")
    if requested in TEAMS and (_is_operator() or requested in _roles()):
        return requested
    return _player_side()


def _read_log_rows():
    if not os.path.exists(LOG_PATH):
        return []
    rows = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    row["elapsed_seconds"] = float(row.get("elapsed_seconds"))
                except (TypeError, ValueError):
                    continue
                rows.append(row)
    except OSError:
        return []
    return rows


def _row_logged_at(row):
    raw = row.get("logged_at")
    if not raw:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _load_highscore_reset_at():
    try:
        with open(HIGHSCORE_RESET_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return float(data.get("reset_at", 0.0))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 0.0


def _save_highscore_reset_at(reset_at):
    with open(HIGHSCORE_RESET_PATH, "w", encoding="utf-8") as fh:
        json.dump({"reset_at": reset_at}, fh, indent=2)


def _highscore_rows(rows=None):
    reset_at = float(_state.get("highscore_reset_at") or 0.0)
    source = _read_log_rows() if rows is None else rows
    if reset_at <= 0:
        return list(source)
    return [row for row in source if _row_logged_at(row) > reset_at]


def _seed_best_from_log():
    best = None
    best_player = ""
    best_team = ""
    for row in _highscore_rows():
        secs = row.get("elapsed_seconds")
        if best is None or secs < best:
            best = secs
            best_player = row.get("player") or ""
            best_team = row.get("team") or ""
    _state["best_seconds"] = best
    _state["best_player"] = best_player
    _state["best_team"] = best_team


def _top_score_rows(rows, limit=10):
    return sorted(
        rows,
        key=lambda row: (
            float(row.get("elapsed_seconds", float("inf"))),
            str(row.get("logged_at") or ""),
        ),
    )[:limit]


def _public_state_locked():
    out = dict(_state)
    out["teams"] = {t: dict(_state["teams"][t]) for t in TEAMS}
    out["server_time"] = time.time()
    return out


def _public_state():
    with _state_lock:
        return _public_state_locked()


def player_names_snapshot():
    """Return the registered player name for each shared player side."""
    with _state_lock:
        return {
            team: str(_state["teams"][team].get("player") or "")
            for team in TEAMS
        }


def _clean_pose(value):
    if not isinstance(value, dict):
        raise ValueError("pose required")
    try:
        return {
            "set": True,
            "x": round(float(value["x"]), 3),
            "y": round(float(value["y"]), 3),
            "z": round(float(value["z"]), 3),
            "r": round(float(value.get("r", 0.0)), 3),
        }
    except (KeyError, TypeError, ValueError):
        raise ValueError("pose must include numeric x, y, z, r")


def _clean_corner_map(value):
    if not isinstance(value, dict):
        raise ValueError("corner map required")
    out = {}
    used = set()
    for key in CORNER_KEYS:
        try:
            marker = int(value[key])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{key.replace('_', ' ')} marker required")
        if marker not in CORNER_MARKERS:
            raise ValueError("corner markers must use the four displayed corner tag numbers")
        if marker in used:
            raise ValueError("each corner must use a different tag number")
        out[key] = marker
        used.add(marker)
    return out


def _clean_active_area(value):
    if not isinstance(value, dict):
        raise ValueError("active area required")
    try:
        s_min = float(value["sMin"])
        s_max = float(value["sMax"])
        t_min = float(value["tMin"])
        t_max = float(value["tMax"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("active area requires numeric sMin, sMax, tMin, tMax")
    # Match the player calibration editor: permit one full calibrated-field
    # width of extrapolation while the UI marks unreachable cells separately.
    lo = -1.0
    hi = 2.0
    s_min = _clamp(s_min, lo, hi)
    s_max = _clamp(s_max, lo, hi)
    t_min = _clamp(t_min, lo, hi)
    t_max = _clamp(t_max, lo, hi)
    if s_max - s_min < 0.05 or t_max - t_min < 0.05:
        raise ValueError("active area is too small")
    return {
        "sMin": round(s_min, 4),
        "sMax": round(s_max, 4),
        "tMin": round(t_min, 4),
        "tMax": round(t_max, 4),
    }


def _clean_marker_cache(value):
    if not isinstance(value, dict):
        raise ValueError("marker cache required")
    tags = value.get("tags")
    if not isinstance(tags, dict):
        raise ValueError("marker cache requires tags")
    out_tags = {}
    for key in ("top_left", "top_right", "bottom_right", "bottom_left", "center"):
        tag = tags.get(key)
        if not isinstance(tag, dict):
            raise ValueError(f"{key.replace('_', ' ')} marker cache required")
        try:
            nx = _clamp(float(tag["nx"]), 0.0, 1.0)
            ny = _clamp(float(tag["ny"]), 0.0, 1.0)
            marker_id = int(tag.get("id", tag.get("marker", 0)))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{key.replace('_', ' ')} marker cache requires numeric nx and ny")
        out_tags[key] = {
            "id": marker_id,
            "nx": round(nx, 6),
            "ny": round(ny, 6),
            "missing": 0.0,
        }
    captured_at = value.get("capturedAt", value.get("captured_at", value.get("captured_at_ms")))
    try:
        captured_at = int(float(captured_at))
    except (TypeError, ValueError):
        captured_at = int(time.time() * 1000)
    camera_view = str(value.get("cameraView") or value.get("camera_view") or "normal")
    if camera_view not in ("normal", "rot180"):
        camera_view = "normal"
    return {"tags": out_tags, "capturedAt": captured_at, "cameraView": camera_view}


def _calibration_side(data):
    data = data if isinstance(data, dict) else {}
    side = (
        data.get("side")
        or data.get("team")
        or request.values.get("side")
        or request.values.get("team")
        or request.headers.get("X-HHH-Team")
        or request.headers.get("X-Team")
    )
    side = str(side or "").strip().lower()
    player = _player_side()
    if side in TEAMS:
        if _is_operator() or player == side:
            return side
    if player in TEAMS and (not side or side == player):
        return player
    if _is_operator():
        hint = _referrer_side()
        if hint in TEAMS:
            return hint
    active = [team for team in TEAMS if _calibration_access.get(team, {}).get("enabled")]
    if len(active) == 1:
        return active[0]
    return None


def _ok(**kw):
    return jsonify({"ok": True, **kw})


def _fail(error, **kw):
    return jsonify({"ok": False, "error": error, **kw})


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _clamp_pose(x, y, z, r):
    z = _clamp(z, *WORKSPACE["z"])
    r = _clamp(r, *WORKSPACE["r"])
    radius = math.hypot(x, y)
    if radius == 0.0:
        x, y = RADIUS_MIN, 0.0
    elif radius > RADIUS_MAX:
        scale = RADIUS_MAX / radius
        x, y = x * scale, y * scale
    elif radius < RADIUS_MIN:
        scale = RADIUS_MIN / radius
        x, y = x * scale, y * scale
    return _clamp(x, *WORKSPACE["x"]), _clamp(y, *WORKSPACE["y"]), z, r


def _pump_mode(do_bits):
    suck = bool(do_bits & (1 << (SUCK_DO_INDEX - 1)))
    blow = bool(do_bits & (1 << (BLOW_DO_INDEX - 1)))
    if suck and blow:
        return "conflict"
    if suck:
        return "suck"
    if blow:
        return "blow"
    return "off"


def _direct_arm_state(side):
    robot = _direct_robots.get(side)
    raw = DobotMG400._blank_state() if robot is None else robot.get_state()
    return {
        "connected": raw["connected"],
        "enabled": raw["enabled"],
        "mode_name": raw.get("mode_name", "DISCONNECTED"),
        "error": raw.get("error", False),
        "joints": raw.get("joints", [0.0, 0.0, 0.0, 0.0]),
        "pose": raw.get("pose", [0.0, 0.0, 0.0, 0.0]),
        "target": None if robot is None else robot.get_target(),
        "pump_mode": _pump_mode(raw.get("digital_out", 0)),
        "feedback_ok": raw.get("feedback_ok", False),
        "servo_active": raw.get("servo_active", False),
        "servo_error": raw.get("servo_error"),
        "control_mode": raw.get("control_mode"),
    }


def _direct_state_dict():
    return {"ok": True, "link": "direct", "arms": {side: _direct_arm_state(side) for side in TEAMS}}


def _direct_apply_motion(robot):
    robot.set_max_velocity_cartesian(DIRECT_MAX_LIN_VEL, DIRECT_MAX_ANG_VEL)
    robot.set_max_accel_cartesian(
        DIRECT_MAX_LIN_VEL / DIRECT_RAMP_SECS,
        DIRECT_MAX_ANG_VEL / DIRECT_RAMP_SECS,
    )


def _direct_robot(side):
    return _direct_robots.get(side)


def _direct_side(data):
    side = data.get("side")
    if side not in TEAMS:
        raise ValueError("side must be 'purple' or 'green'")
    return side


def _direct_require_operator():
    if not _is_operator():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    return None


def _calibration_from_saved(saved):
    base = _default_calibration()
    if not isinstance(saved, dict):
        return base
    saved_playfield = saved.get("playfield") if isinstance(saved.get("playfield"), dict) else {}
    saved_areas = saved_playfield.get("areas") if isinstance(saved_playfield.get("areas"), dict) else {}
    try:
        base["playfield"]["active_area"] = _clean_active_area(saved_playfield.get("active_area"))
    except ValueError:
        active_area = saved.get("active_area")
        if isinstance(active_area, dict):
            try:
                base["playfield"]["active_area"] = _clean_active_area(active_area)
            except ValueError:
                pass
    try:
        base["playfield"]["marker_cache"] = _clean_marker_cache(saved_playfield.get("marker_cache"))
    except ValueError:
        pass
    for key in base["playfield"]["areas"]:
        area = saved_areas.get(key)
        if isinstance(area, dict):
            base["playfield"]["areas"][key]["area_id"] = area.get("area_id")
    try:
        base["playfield"]["corner_map"] = _clean_corner_map(saved_playfield.get("corner_map"))
    except ValueError:
        pass
    global_corner_map = base["playfield"]["corner_map"]
    base["playfield"]["updated_at"] = saved_playfield.get("updated_at")
    saved_arms = saved.get("arms") if isinstance(saved.get("arms"), dict) else saved
    for side in TEAMS:
        src = saved_arms.get(side) if isinstance(saved_arms, dict) else None
        if not isinstance(src, dict):
            continue
        try:
            base["arms"][side]["corner_map"] = _clean_corner_map(src.get("corner_map"))
        except ValueError:
            base["arms"][side]["corner_map"] = dict(global_corner_map)
        try:
            base["arms"][side]["marker_cache"] = _clean_marker_cache(src.get("marker_cache"))
        except ValueError:
            pass
        for key in base["arms"][side]["points"]:
            pose = (((src.get("points") or {}).get(key) or {}).get("pose"))
            if isinstance(pose, dict) and pose.get("set"):
                try:
                    base["arms"][side]["points"][key]["pose"] = _clean_pose(pose)
                except ValueError:
                    pass
        for target in ("center",):
            pose = src.get(target)
            if isinstance(pose, dict) and pose.get("set"):
                try:
                    base["arms"][side][target] = _clean_pose(pose)
                except ValueError:
                    pass
        for target in ("pickup_height", "transport_height"):
            h = src.get(target)
            if isinstance(h, dict) and h.get("set"):
                try:
                    base["arms"][side][target] = {"set": True, "z": round(float(h["z"]), 3)}
                except (KeyError, TypeError, ValueError):
                    pass
        base["arms"][side]["updated_at"] = src.get("updated_at")
    return base


def _load_calibration():
    global _calibration
    try:
        with open(CALIBRATION_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError, TypeError):
        return
    base = _calibration_from_saved(saved)
    _calibration = base


def _save_calibration():
    with open(CALIBRATION_PATH, "w", encoding="utf-8") as fh:
        json.dump(_calibration, fh, indent=2)


def _calibration2_from_saved(saved):
    """Validate a saved Cal 2 document without changing live state."""
    base = _default_calibration2()
    if not isinstance(saved, dict):
        return base
    for key, target in ((saved.get("playfield") or {}).get("targets") or {}).items():
        if key in base["playfield"]["targets"] and isinstance(target, dict):
            base["playfield"]["targets"][key].update({
                field: target.get(field)
                for field in ("s", "t", "area_id") if target.get(field) is not None
            })
    fixed_snapshot = _fixed_cal2_playfield()
    if fixed_snapshot:
        canonical = _cal2_snapshot_coordinates(fixed_snapshot)
        for target in base["playfield"]["targets"].values():
            if target["marker"] in canonical:
                target.update(canonical[target["marker"]])
    saved_areas = (saved.get("playfield") or {}).get("saved_areas")
    if isinstance(saved_areas, list):
        base["playfield"]["saved_areas"] = json.loads(json.dumps(saved_areas))
    saved_settings = (saved.get("playfield") or {}).get("saved_settings")
    if isinstance(saved_settings, dict):
        base["playfield"]["saved_settings"] = json.loads(json.dumps(saved_settings))
    saved_view = (saved.get("playfield") or {}).get("saved_camera_view")
    if saved_view in ("normal", "rot180"):
        base["playfield"]["saved_camera_view"] = saved_view
    try:
        base["playfield"]["active_area"] = _clean_active_area(
            (saved.get("playfield") or {}).get("active_area"))
    except ValueError:
        pass
    for side in TEAMS:
        arm = ((saved.get("arms") or {}).get(side) or {})
        if arm.get("camera_view") in ("normal", "rot180"):
            base["arms"][side]["camera_view"] = arm["camera_view"]
        for key, point in (arm.get("points") or {}).items():
            if key not in base["arms"][side]["points"] or not isinstance(point, dict):
                continue
            camera = point.get("camera")
            if isinstance(camera, dict):
                try:
                    base["arms"][side]["points"][key]["camera"] = {
                        "u": float(camera["u"]), "v": float(camera["v"])
                    }
                except (KeyError, TypeError, ValueError):
                    pass
            pose = point.get("pose")
            if isinstance(pose, dict) and pose.get("set"):
                try:
                    base["arms"][side]["points"][key]["pose"] = _clean_pose(pose)
                except ValueError:
                    pass
        for marker, sample in (arm.get("parallax_points") or {}).items():
            marker_key = str(marker)
            try:
                base["arms"][side]["parallax_points"][marker_key] = \
                    _clean_cal2_parallax_sample(marker, sample)
            except (KeyError, ValueError):
                pass
        base["arms"][side]["updated_at"] = arm.get("updated_at")
        try:
            stack_drop_offset = float(arm["stack_drop_offset"])
            if math.isfinite(stack_drop_offset) and 0 <= stack_drop_offset <= 100:
                base["arms"][side]["stack_drop_offset"] = stack_drop_offset
        except (KeyError, TypeError, ValueError):
            pass
    shared_z = saved.get("shared_z") or {}
    try:
        base["shared_z"]["transport_height"] = float(shared_z["transport_height"])
    except (KeyError, TypeError, ValueError):
        pass
    # Older Cal 2 files stored one pickup Z for both robots. Copy that Cal 2
    # value to each arm on migration; the removed four-corner calibration is
    # deliberately not consulted.
    for side in TEAMS:
        arm = ((saved.get("arms") or {}).get(side) or {})
        candidates = [arm.get("pickup_height"), shared_z.get("pickup_height")]
        for candidate in candidates:
            try:
                value = float(candidate)
                if math.isfinite(value):
                    base["arms"][side]["pickup_height"] = value
                    break
            except (TypeError, ValueError):
                pass
    base["playfield"]["updated_at"] = (saved.get("playfield") or {}).get("updated_at")
    return base


def _load_calibration2():
    global _calibration2
    try:
        with open(CALIBRATION2_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(saved, dict):
        return
    base = _calibration2_from_saved(saved)
    _calibration2 = base


def _save_calibration2():
    with open(CALIBRATION2_PATH, "w", encoding="utf-8") as fh:
        json.dump(_calibration2, fh, indent=2)


def _calibration2_public():
    with _calibration_lock:
        return json.loads(json.dumps(_calibration2))


def _fixed_cal2_playfield():
    try:
        with open(FIXED_CAL2_PLAYFIELD_PATH, "r", encoding="utf-8") as fh:
            snapshot = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    areas = snapshot.get("areas") if isinstance(snapshot, dict) else None
    if not isinstance(areas, list) or len(areas) != len(CAL2_MARKERS):
        return None
    try:
        if {int(area["marker"]) for area in areas} != set(CAL2_MARKERS):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return snapshot


def _cal2_snapshot_coordinates(snapshot):
    """Return immutable logical target coordinates from a saved field."""
    coordinates = {}
    for area in snapshot.get("areas") or ():
        try:
            marker = int(area["marker"])
            x, z = float(area["x"]), float(area["z"])
        except (KeyError, TypeError, ValueError):
            continue
        coordinates[marker] = {
            "s": round((1.0-x/CALIBRATION_TAG_SPAN_X)/2.0, 6),
            "t": round((1.0-z/CALIBRATION_TAG_SPAN_Z)/2.0, 6),
        }
    return coordinates


def _solve_linear_system(matrix, values):
    """Solve a small dense linear system with pivoted elimination."""
    size = len(values)
    rows = [list(map(float, matrix[i])) + [float(values[i])]
            for i in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-10:
            return None
        rows[column], rows[pivot] = rows[pivot], rows[column]
        divisor = rows[column][column]
        rows[column] = [value/divisor for value in rows[column]]
        for row in range(size):
            if row == column:
                continue
            factor = rows[row][column]
            rows[row] = [
                value-factor*rows[column][index]
                for index, value in enumerate(rows[row])
            ]
    return [rows[index][-1] for index in range(size)]


def _cal2_robot_model(side):
    """Fit this arm's six Cal 2 poses in canonical field coordinates."""
    arm = _calibration2.get("arms", {}).get(side, {})
    points = arm.get("points") or {}
    targets = _calibration2.get("playfield", {}).get("targets") or {}
    samples = []
    for key in targets:
        point, target = points.get(key) or {}, targets[key]
        pose = point.get("pose") or {}
        try:
            if not pose.get("set"):
                return None
            s, t = float(target["s"]), float(target["t"])
            x, y = float(pose["x"]), float(pose["y"])
        except (KeyError, TypeError, ValueError):
            return None
        samples.append(([1.0, s, t, s*t], x, y))
    if len(samples) != len(CAL2_MARKERS):
        return None
    normal = [[0.0]*4 for _ in range(4)]
    rhs_x, rhs_y = [0.0]*4, [0.0]*4
    for features, x, y in samples:
        for row in range(4):
            rhs_x[row] += features[row]*x
            rhs_y[row] += features[row]*y
            for column in range(4):
                normal[row][column] += features[row]*features[column]
    coefficients_x = _solve_linear_system(normal, rhs_x)
    coefficients_y = _solve_linear_system(normal, rhs_y)
    if coefficients_x is None or coefficients_y is None:
        return None
    return coefficients_x, coefficients_y


def _cal2_robot_xy(side, s, t, model=None):
    """Map a canonical field point through this arm's own Cal 2 reach model."""
    model = model if model is not None else _cal2_robot_model(side)
    if model is None:
        return None
    features = (1.0, float(s), float(t), float(s)*float(t))
    return tuple(sum(value*coefficient for value, coefficient in zip(features, axis))
                 for axis in model)


def _cal2_axis_positions():
    area = _calibration2["playfield"].get("active_area") or {}
    s0, s1 = float(area.get("sMin", 0)), float(area.get("sMax", 1))
    t0, t1 = float(area.get("tMin", 0)), float(area.get("tMax", 1))
    # Convert the requested 20 mm robot-space inset to normalized field units.
    spans = []
    for side in TEAMS:
        model = _cal2_robot_model(side)
        a = _cal2_robot_xy(side, s0, (t0+t1)/2, model)
        b = _cal2_robot_xy(side, s1, (t0+t1)/2, model)
        c = _cal2_robot_xy(side, (s0+s1)/2, t0, model)
        d = _cal2_robot_xy(side, (s0+s1)/2, t1, model)
        if a and b:
            spans.append((abs(s1-s0) * 20.0 / max(1.0, math.dist(a, b)), "s"))
        if c and d:
            spans.append((abs(t1-t0) * 20.0 / max(1.0, math.dist(c, d)), "t"))
    inset_s = max([v for v, axis in spans if axis == "s"] or [abs(s1-s0)*0.05])
    inset_t = max([v for v, axis in spans if axis == "t"] or [abs(t1-t0)*0.05])
    inset_s = min(inset_s, abs(s1-s0)*0.22)
    inset_t = min(inset_t, abs(t1-t0)*0.22)
    return (
        [s0 + inset_s, (s0+s1)/2, s1 - inset_s],
        [t0 + inset_t, (t0+t1)/2, t1 - inset_t],
    )


def _cal2_safe_at(s, t, selected_side=None, models=None):
    sides = (selected_side,) if selected_side in TEAMS else TEAMS
    for side in sides:
        model = (models or {}).get(side) if models is not None else None
        xy = _cal2_robot_xy(side, s, t, model)
        if xy is None:
            continue
        radius = math.hypot(*xy)
        if radius < CAL2_RADIUS_MIN or radius > CAL2_RADIUS_MAX:
            return False
    return True


def _cal2_safe_at_view(s, t, selected_side=None, rotate_layout=False, models=None):
    """Test canonical reach; camera rotation affects only rendered coordinates."""
    return _cal2_safe_at(s, t, selected_side, models)


def _cal2_reachable_contour(side, rotate_layout=False):
    """Return three left/centre/right rows following this arm's usable contour.

    This samples the same per-arm six-point model used by the player's red
    reach overlay. Coordinates remain canonical; camera rotation is applied
    only when the physical marker areas are created.
    """
    ss, ts = _cal2_axis_positions()
    s_lo, s_hi = min(ss[0], ss[2]), max(ss[0], ss[2])
    t_lo, t_hi = min(ts[0], ts[2]), max(ts[0], ts[2])
    rows = []
    models = {side: _cal2_robot_model(side)}
    s_steps, t_steps = 320, 220
    for row_index in range(t_steps + 1):
        t = t_lo + (t_hi-t_lo) * row_index / t_steps
        runs, run = [], []
        for col_index in range(s_steps + 1):
            s = s_lo + (s_hi-s_lo) * col_index / s_steps
            if _cal2_safe_at_view(s, t, side, rotate_layout, models):
                run.append(s)
            elif run:
                runs.append(run)
                run = []
        if run:
            runs.append(run)
        if not runs:
            continue
        widest = max(runs, key=lambda values: values[-1]-values[0])
        rows.append({
            "t": t, "lo": widest[0], "hi": widest[-1],
            "width": widest[-1]-widest[0],
        })
    if not rows:
        return None
    max_width = max(row["width"] for row in rows)
    useful = [row for row in rows if row["width"] >= max_width * 0.30]
    if len(useful) < 3:
        useful = rows
    selected = [useful[0], useful[len(useful)//2], useful[-1]]
    result = []
    for row in selected:
        result.extend([
            (row["lo"], row["t"]),
            ((row["lo"]+row["hi"])/2.0, row["t"]),
            (row["hi"], row["t"]),
        ])
    return result


def _set_cal2_arm_view_locked(side, view):
    arm = _calibration2["arms"][side]
    if arm.get("camera_view") != view:
        # Raised/ground camera coordinates belong to the orientation in which
        # they were captured, so changing the authoritative view invalidates
        # only this player's parallax samples.
        arm["parallax_points"] = _empty_cal2_parallax_points()
    arm["camera_view"] = view
    arm["updated_at"] = time.time()


def _ensure_playfield_calibration2_areas(side=None, camera_view=None):
    playfield = _playfield_module()
    if playfield is None or not hasattr(playfield, "remove_area"):
        return False, "Playfield Areas prototype is not loaded"
    for area in list(playfield.list_areas()):
        area_id = area.get("id")
        if area_id:
            playfield.remove_area(area_id)
    fixed_snapshot = _fixed_cal2_playfield()
    saved_areas = (fixed_snapshot.get("areas") if fixed_snapshot else
                   _calibration2["playfield"].get("saved_areas"))
    # Laser Tag also uses the Cal 2 marker range. Older controller code accidentally
    # adopted whatever happened to be on the shared playfield on page load,
    # so reject that foreign snapshot and reconstruct the original Cal 2 field
    # from its already-captured robot calibration points.
    if isinstance(saved_areas, list) and any(
            str(area.get("name", "")).startswith("Laser Tag")
            for area in saved_areas if isinstance(area, dict)):
        with _calibration_lock:
            _calibration2["playfield"]["saved_areas"] = None
            _calibration2["playfield"]["saved_settings"] = None
            _save_calibration2()
        saved_areas = None
    if isinstance(saved_areas, list) and len(saved_areas) == len(CAL2_MARKERS):
        requested_view = camera_view if camera_view in ("normal", "rot180") else \
            (_calibration2["arms"][side].get("camera_view", "normal")
             if side in TEAMS else _calibration2["playfield"].get("saved_camera_view", "normal"))
        saved_view = (fixed_snapshot.get("camera_view", "normal")
                      if fixed_snapshot else
                      _calibration2["playfield"].get("saved_camera_view", "normal"))
        rotate_saved = requested_view != saved_view
        created = {}
        canonical = _cal2_snapshot_coordinates({"areas": saved_areas})
        for saved in saved_areas:
            fields = {
                key: saved[key]
                for key in (
                    "name", "x", "y", "z", "size", "color", "glow", "marker",
                    "show_area", "show_aruco", "show_links",
                )
                if key in saved
            }
            if rotate_saved:
                fields["x"] = -float(fields.get("x", 0.0))
                fields["z"] = -float(fields.get("z", 0.0))
            area = playfield.create_area(**fields)
            if area:
                created[int(area["marker"])] = area
        with _calibration_lock:
            if side in TEAMS:
                _set_cal2_arm_view_locked(side, requested_view)
            for target in _calibration2["playfield"]["targets"].values():
                area = created.get(target["marker"])
                logical = canonical.get(target["marker"])
                if not area or not logical:
                    continue
                target.update({
                    **logical,
                    "area_id": area.get("id"),
                })
            _calibration2["playfield"]["updated_at"] = time.time()
            _save_calibration2()
        saved_settings = (fixed_snapshot.get("settings") if fixed_snapshot else
                          _calibration2["playfield"].get("saved_settings"))
        if isinstance(saved_settings, dict) and hasattr(playfield, "set_view_settings"):
            playfield.set_view_settings(**saved_settings)
        else:
            _reset_playfield_camera(playfield)
        if hasattr(playfield, "save_areas_now"):
            playfield.save_areas_now()
        return True, None
    ss, ts = _cal2_axis_positions()
    view = camera_view if camera_view in ("normal", "rot180") else \
        (_calibration2["arms"][side].get("camera_view", "normal")
         if side in TEAMS else "normal")
    rotate_layout = side in TEAMS and view == "rot180"
    candidates = None
    if side in TEAMS:
        candidates = _cal2_reachable_contour(side, rotate_layout)
    if candidates is None:
        candidates = [(s, t) for t in ts for s in ss]
    # Pull an unsafe candidate toward the field centre until it is 20 mm
    # inside both robot reach rings.
    center_s, center_t = ss[1], ts[1]
    safe = []
    models = {side: _cal2_robot_model(side)} if side in TEAMS else None
    for s, t in candidates:
        for _ in range(20):
            if _cal2_safe_at_view(s, t, side, rotate_layout, models):
                break
            s = center_s + (s-center_s)*0.9
            t = center_t + (t-center_t)*0.9
        safe.append((s, t))
    with _calibration_lock:
        target_items = list(_calibration2["playfield"]["targets"].items())
        if side in TEAMS:
            _set_cal2_arm_view_locked(side, view)
        for (key, target), (s, t) in zip(target_items, safe):
            # Keep logical coordinates canonical. Only physical marker
            # placement follows the selected camera orientation.
            display_s, display_t = ((1.0-s, 1.0-t)
                                    if rotate_layout else (s, t))
            fields = {
                "name": f"Auto PP Cal 2 - {target['label']}",
                "x": CALIBRATION_TAG_SPAN_X * (1.0 - 2.0*display_s),
                "y": 0.02,
                "z": CALIBRATION_TAG_SPAN_Z * (1.0 - 2.0*display_t),
                "size": CALIBRATION_TAG_SIZE,
                "color": "#ffffff", "glow": 0.0,
                "marker": target["marker"],
                "show_area": False, "show_aruco": True, "show_links": False,
            }
            area = playfield.create_area(**fields)
            target.update({"s": round(s, 6), "t": round(t, 6),
                           "area_id": area.get("id") if area else None})
        _calibration2["playfield"]["updated_at"] = time.time()
        _save_calibration2()
    _reset_playfield_camera(playfield)
    if hasattr(playfield, "save_areas_now"):
        playfield.save_areas_now()
    return True, None


def _adopt_current_playfield_calibration2(side=None, camera_view=None):
    """Use the currently rendered marker objects without moving them."""
    playfield = _playfield_module()
    if playfield is None or not hasattr(playfield, "list_areas"):
        return False, "Playfield Areas prototype is not loaded"
    by_marker = {}
    for area in playfield.list_areas():
        try:
            marker = int(area.get("marker"))
        except (TypeError, ValueError):
            continue
        if marker in CAL2_MARKERS and area.get("show_aruco", True):
            by_marker[marker] = area
    missing = [marker for marker in CAL2_MARKERS if marker not in by_marker]
    if missing:
        return False, "current playfield is missing Cal 2 markers: " + \
            ", ".join(str(marker) for marker in missing)
    with _calibration_lock:
        saved_areas = []
        for target in _calibration2["playfield"]["targets"].values():
            area = by_marker[target["marker"]]
            try:
                x, z = float(area["x"]), float(area["z"])
            except (KeyError, TypeError, ValueError):
                return False, f"marker {target['marker']} has invalid playfield coordinates"
            target.update({
                "s": round((1.0-x/CALIBRATION_TAG_SPAN_X)/2.0, 6),
                "t": round((1.0-z/CALIBRATION_TAG_SPAN_Z)/2.0, 6),
                "area_id": area.get("id"),
            })
            saved_areas.append({
                key: area[key]
                for key in (
                    "name", "x", "y", "z", "size", "color", "glow", "marker",
                    "show_area", "show_aruco", "show_links",
                )
                if key in area
            })
        _calibration2["playfield"]["saved_areas"] = saved_areas
        if hasattr(playfield, "get_view_settings"):
            settings = playfield.get_view_settings()
            if isinstance(settings, dict):
                _calibration2["playfield"]["saved_settings"] = settings
        view = camera_view if camera_view in ("normal", "rot180") else \
            (_calibration2["arms"][side].get("camera_view", "normal")
             if side in TEAMS else "normal")
        if side in TEAMS:
            _set_cal2_arm_view_locked(side, view)
        _calibration2["playfield"]["saved_camera_view"] = view
        _calibration2["playfield"]["updated_at"] = time.time()
        _save_calibration2()
    return True, None


def _calibration_public():
    with _calibration_lock:
        return json.loads(json.dumps(_calibration))


def _calibration_access_public():
    return {
        "active_side": next((side for side in TEAMS if _calibration_access[side]["enabled"]), ""),
        "teams": {side: dict(_calibration_access[side]) for side in TEAMS},
    }


def _calibration_write_allowed(data=None):
    if _is_operator():
        return True, None
    side = _player_side()
    if side in TEAMS and _calibration_access[side]["enabled"]:
        return True, None
    return False, f"{side or 'player'} calibration is not enabled by the gamemaster"


def hub_init(ctx):
    global _hub_ctx
    _hub_ctx = ctx
    # Loading the prototype must be read-only. Calibration fields are shared
    # with other games, so only the explicit playfield endpoints below may
    # replace the currently active areas or camera settings.


def _playfield_module():
    return _hub_ctx.get_prototype("playfield-areas") if _hub_ctx is not None else None


def _relay_module():
    return _hub_ctx.get_prototype("dobot-mg400-relay") if _hub_ctx is not None else None


def _kick_relay_side(side):
    relay = _relay_module()
    if relay is None or not hasattr(relay, "kick_side"):
        return False
    return bool(relay.kick_side(side))


def _playfield_pose_for(key):
    # Calibration markers are mirrored around the center tag.
    return {
        "top_left": {"x": CALIBRATION_TAG_SPAN_X, "z": CALIBRATION_TAG_SPAN_Z},
        "top_right": {"x": -CALIBRATION_TAG_SPAN_X, "z": CALIBRATION_TAG_SPAN_Z},
        "bottom_right": {"x": -CALIBRATION_TAG_SPAN_X, "z": -CALIBRATION_TAG_SPAN_Z},
        "bottom_left": {"x": CALIBRATION_TAG_SPAN_X, "z": -CALIBRATION_TAG_SPAN_Z},
        "center": {"x": 0.0, "z": 0.0},
    }[key]


def _reset_playfield_camera(playfield):
    if not hasattr(playfield, "set_view_settings"):
        return
    playfield.set_view_settings(
        bloom=0.0,
        dof=0.0,
        fov=24.0,
        cam={"x": 0.0, "y": 18.0, "z": 0.0},
        rot={"x": -90.0, "y": 0.0, "z": 0.0},
    )


def _ensure_playfield_calibration_areas():
    playfield = _playfield_module()
    if playfield is None:
        return False, "Playfield Areas prototype is not loaded"
    if not hasattr(playfield, "remove_area"):
        return False, "Playfield Areas controller cannot delete areas"
    for area in list(playfield.list_areas()):
        area_id = area.get("id")
        if area_id:
            playfield.remove_area(area_id)
    with _calibration_lock:
        for key, area_ref in _calibration["playfield"]["areas"].items():
            label = area_ref["label"]
            name = f"Auto Pick Calibration - {label}"
            pos = _playfield_pose_for(key)
            fields = {
                "name": name,
                "x": pos["x"],
                "y": 0.02,
                "z": pos["z"],
                "size": CALIBRATION_TAG_SIZE,
                "color": "#ffffff",
                "glow": 0.0,
                "marker": area_ref["marker"],
                "show_area": False,
                "show_aruco": True,
                "show_links": False,
            }
            area = playfield.create_area(**fields)
            if area:
                area_ref["area_id"] = area["id"]
        _calibration["playfield"]["marker_cache"] = None
        _calibration["playfield"]["updated_at"] = time.time()
        _save_calibration()
    _reset_playfield_camera(playfield)
    if hasattr(playfield, "save_areas_now"):
        playfield.save_areas_now()
    return True, None


def _saved_playfield_payload(areas):
    return {
        "format": "auto-pick-and-place-playfield-v1",
        "saved_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "areas": json.loads(json.dumps(areas)),
    }


def _sync_calibration_area_refs(areas):
    by_name = {area.get("name"): area.get("id") for area in areas if isinstance(area, dict)}
    with _calibration_lock:
        changed = False
        for key, area_ref in _calibration["playfield"]["areas"].items():
            name = f"Auto Pick Calibration - {area_ref['label']}"
            area_id = by_name.get(name)
            if area_id and area_ref.get("area_id") != area_id:
                area_ref["area_id"] = area_id
                changed = True
        if changed:
            _calibration["playfield"]["updated_at"] = time.time()
            _save_calibration()
        return json.loads(json.dumps(_calibration))


@bp.route("/")
@bp.route("/game")
def game():
    resp = send_from_directory(HERE, "game.html")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@bp.route("/api/state")
def api_state():
    return jsonify(_public_state())


@bp.route("/api/events")
def api_events():
    return _live.stream(_public_state, interval=0.2)


@bp.route("/api/calibration")
def api_calibration():
    return jsonify({
        "ok": True,
        "calibration": _calibration_public(),
        "access": _calibration_access_public(),
    })


@bp.route("/api/calibration/access", methods=["GET", "POST"])
def api_calibration_access():
    if request.method == "GET":
        return jsonify({"ok": True, "access": _calibration_access_public()})
    if not _is_operator():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    data = request.get_json(silent=True) or {}
    side = data.get("side") or data.get("team")
    if side not in TEAMS:
        return jsonify({"ok": False, "error": "team required"}), 400
    enabled = bool(data.get("enabled", True))
    now = time.time()
    kicked = []
    if enabled:
        for other in TEAMS:
            _calibration_access[other]["enabled"] = other == side
            _calibration_access[other]["updated_at"] = now
            if other != side and _kick_relay_side(other):
                kicked.append(other)
    else:
        _calibration_access[side]["enabled"] = False
        _calibration_access[side]["updated_at"] = now
    _live.bump()
    return jsonify({"ok": True, "access": _calibration_access_public(), "kicked": kicked})


@bp.route("/api/calibration/playfield", methods=["POST"])
def api_calibration_playfield():
    ok, err = _calibration_write_allowed()
    if not ok:
        return jsonify({"ok": False, "error": err}), 403
    ok, err = _ensure_playfield_calibration_areas()
    if not ok:
        return jsonify({"ok": False, "error": err}), 409
    return jsonify({"ok": True, "calibration": _calibration_public()})


@bp.route("/api/calibration2")
def api_calibration2():
    return jsonify({"ok": True, "calibration": _calibration2_public(),
                    "access": _calibration_access_public()})


@bp.route("/api/calibration2/export.zip", methods=["POST"])
def api_calibration2_export_zip():
    """Download Cal 2 state without activating or modifying it."""
    if not (_is_operator() or _player_side() in TEAMS):
        return jsonify({"ok": False, "error": "team or gamemaster required"}), 403
    side = _player_side() or "all"
    calibration = _calibration2_public()
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "auto-calibration-2.json",
            json.dumps(calibration, indent=2, sort_keys=True),
        )
        zf.writestr(
            "calibration-info.json",
            json.dumps({
                "format": "auto-pp-calibration-2-export-v1",
                "side": side,
                "exported_at": _dt.datetime.now(_dt.UTC).isoformat(),
                "active_area": calibration["playfield"]["active_area"],
            }, indent=2, sort_keys=True),
        )
    payload.seek(0)
    return Response(
        payload.getvalue(),
        mimetype="application/zip",
        headers={
            "Content-Disposition":
                f"attachment; filename=auto-pp-cal-2-{side}.zip"
        },
    )


@bp.route("/api/calibration2/import.zip", methods=["POST"])
def api_calibration2_import_zip():
    """Restore saved Cal 2 state only after an explicit, authorized upload."""
    global _calibration2
    player_side = _player_side()
    ok, err = _calibration_write_allowed()
    if not ok:
        return jsonify({"ok": False, "error": err}), 403
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"ok": False, "error": "zip file required"}), 400
    try:
        raw = upload.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("calibration zip is larger than 2 MB")
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            if "auto-calibration-2.json" not in zf.namelist():
                raise ValueError("auto-calibration-2.json missing from zip")
            info = zf.getinfo("auto-calibration-2.json")
            if info.file_size > 2 * 1024 * 1024:
                raise ValueError("calibration document is larger than 2 MB")
            saved = json.loads(
                zf.read("auto-calibration-2.json").decode("utf-8"))
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile,
            json.JSONDecodeError, ValueError) as exc:
        return jsonify({
            "ok": False, "error": f"invalid Cal 2 calibration zip: {exc}"
        }), 400
    if not isinstance(saved, dict) or \
            saved.get("format") != "auto-pp-calibration-2-v1":
        return jsonify({
            "ok": False,
            "error": "uploaded file is not an Auto PP Cal 2 calibration",
        }), 400
    imported = _calibration2_from_saved(saved)
    with _calibration_lock:
        if _is_operator():
            _calibration2 = imported
        else:
            # A player restores only their arm plus the two intentionally shared
            # values. Do not replace the live playfield, target object IDs, or
            # the other player's calibration.
            _calibration2["arms"][player_side] = imported["arms"][player_side]
            _calibration2["shared_z"] = imported["shared_z"]
            _calibration2["playfield"]["active_area"] = \
                imported["playfield"]["active_area"]
            _calibration2["playfield"]["updated_at"] = time.time()
        _save_calibration2()
        out = json.loads(json.dumps(_calibration2))
    _live.bump()
    return jsonify({
        "ok": True,
        "calibration": out,
        "active_area": out["playfield"]["active_area"],
    })


@bp.route("/api/calibration2/playfield", methods=["POST"])
def api_calibration2_playfield():
    if not _is_operator():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    data = request.get_json(silent=True) or {}
    side = data.get("side") or data.get("team")
    view = data.get("camera_view")
    if side not in TEAMS:
        side = None
    ok, err = _ensure_playfield_calibration2_areas(side, view)
    if not ok:
        return jsonify({"ok": False, "error": err}), 409
    return jsonify({"ok": True, "calibration": _calibration2_public(),
                    "access": _calibration_access_public()})


@bp.route("/api/calibration2/orientation", methods=["POST"])
def api_calibration2_orientation():
    if not _is_operator():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    data = request.get_json(silent=True) or {}
    side = data.get("side") or data.get("team")
    view = data.get("camera_view")
    if side not in TEAMS:
        return jsonify({"ok": False, "error": "team required"}), 400
    if view not in ("normal", "rot180"):
        return jsonify({"ok": False, "error": "camera_view must be normal or rot180"}), 400
    ok, err = _ensure_playfield_calibration2_areas(side, view)
    if not ok:
        return jsonify({"ok": False, "error": err}), 409
    return jsonify({"ok": True, "side": side, "camera_view": view,
                    "calibration": _calibration2_public(),
                    "access": _calibration_access_public()})


@bp.route("/api/calibration2/use-current-playfield", methods=["POST"])
def api_calibration2_use_current_playfield():
    if not _is_operator():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    data = request.get_json(silent=True) or {}
    side = data.get("side") or data.get("team")
    if side not in TEAMS:
        side = None
    ok, err = _adopt_current_playfield_calibration2(
        side, data.get("camera_view"))
    if not ok:
        return jsonify({"ok": False, "error": err}), 409
    return jsonify({
        "ok": True,
        "calibration": _calibration2_public(),
        "access": _calibration_access_public(),
    })


@bp.route("/api/calibration2/capture", methods=["POST"])
def api_calibration2_capture():
    data = request.get_json(silent=True) or {}
    side = _calibration_side(data)
    if side not in TEAMS:
        return jsonify({"ok": False, "error": "team required"}), 403
    if not _is_operator() and not _calibration_access[side]["enabled"]:
        return jsonify({"ok": False, "error": f"{side} calibration is not enabled by the gamemaster"}), 403
    key = data.get("target")
    if key not in _calibration2["arms"][side]["points"]:
        return jsonify({"ok": False, "error": "unknown calibration target"}), 400
    try:
        pose = _clean_pose(data.get("pose"))
        camera = data.get("camera") or {}
        camera = {"u": round(float(camera["u"]), 8),
                  "v": round(float(camera["v"]), 8)}
    except (ValueError, KeyError, TypeError):
        return jsonify({"ok": False, "error": "numeric pose and camera coordinates required"}), 400
    with _calibration_lock:
        arm = _calibration2["arms"][side]
        arm["points"][key]["pose"] = pose
        arm["points"][key]["camera"] = camera
        arm["updated_at"] = time.time()
        if data.get("pickup_height") is not None:
            arm["pickup_height"] = round(float(data["pickup_height"]), 3)
        if data.get("transport_height") is not None:
            _calibration2["shared_z"]["transport_height"] = \
                round(float(data["transport_height"]), 3)
        _save_calibration2()
    return jsonify({"ok": True, "calibration": _calibration2_public()})


@bp.route("/api/calibration2/parallax", methods=["POST"])
def api_calibration2_parallax():
    data = request.get_json(silent=True) or {}
    side = _calibration_side(data)
    if side not in TEAMS:
        return jsonify({"ok": False, "error": "team required"}), 403
    if not _is_operator() and not _calibration_access[side]["enabled"]:
        return jsonify({"ok": False, "error": f"{side} calibration is not enabled by the gamemaster"}), 403
    try:
        marker = int(data.get("marker"))
        sample = _clean_cal2_parallax_sample(marker, data)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "valid marker, ground and raised camera coordinates required"}), 400
    with _calibration_lock:
        _calibration2["arms"][side]["parallax_points"][str(marker)] = sample
        _calibration2["arms"][side]["updated_at"] = time.time()
        _save_calibration2()
    return jsonify({"ok": True, "calibration": _calibration2_public()})


@bp.route("/api/calibration2/parallax/reset", methods=["POST"])
def api_calibration2_parallax_reset():
    data = request.get_json(silent=True) or {}
    side = _calibration_side(data)
    if side not in TEAMS:
        return jsonify({"ok": False, "error": "team required"}), 403
    if not _is_operator() and not _calibration_access[side]["enabled"]:
        return jsonify({"ok": False, "error": f"{side} calibration is not enabled by the gamemaster"}), 403
    with _calibration_lock:
        _calibration2["arms"][side]["parallax_points"] = _empty_cal2_parallax_points()
        _calibration2["arms"][side]["updated_at"] = time.time()
        _save_calibration2()
    return jsonify({"ok": True, "calibration": _calibration2_public()})


@bp.route("/api/calibration2/z", methods=["POST"])
@bp.route("/api/calibration2/shared-z", methods=["POST"])
def api_calibration2_z():
    data = request.get_json(silent=True) or {}
    side = _calibration_side(data)
    if side not in TEAMS:
        return jsonify({"ok": False, "error": "team required"}), 403
    if not _is_operator() and not _calibration_access[side]["enabled"]:
        return jsonify({"ok": False, "error": f"{side} calibration is not enabled by the gamemaster"}), 403
    heights = {}
    accepted_heights = (
        "pickup_height", "transport_height", "stack_drop_offset")
    if any(key in data for key in accepted_heights):
        raw_heights = {
            key: data[key]
            for key in accepted_heights if key in data
        }
    else:
        height = str(data.get("height") or "")
        if height not in accepted_heights:
            return jsonify({
                "ok": False,
                "error": "height must be pickup_height, transport_height, or stack_drop_offset",
            }), 400
        raw_heights = {height: data.get("z")}
    try:
        for height, raw_value in raw_heights.items():
            value = round(float(raw_value), 3)
            if not math.isfinite(value):
                raise ValueError
            if height == "stack_drop_offset":
                if not 0 <= value <= 100:
                    raise ValueError
            elif not WORKSPACE["z"][0] <= value <= WORKSPACE["z"][1]:
                raise ValueError
            heights[height] = value
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "Z values must be inside the robot workspace and the 100s stack drop offset must be 0-100 mm",
        }), 400
    with _calibration_lock:
        if "pickup_height" in heights:
            _calibration2["arms"][side]["pickup_height"] = \
                heights["pickup_height"]
            _calibration2["arms"][side]["updated_at"] = time.time()
        if "transport_height" in heights:
            _calibration2["shared_z"]["transport_height"] = \
                heights["transport_height"]
        if "stack_drop_offset" in heights:
            _calibration2["arms"][side]["stack_drop_offset"] = \
                heights["stack_drop_offset"]
            _calibration2["arms"][side]["updated_at"] = time.time()
        _save_calibration2()
    return jsonify({"ok": True, "calibration": _calibration2_public()})


@bp.route("/api/calibration2/active-area", methods=["POST"])
def api_calibration2_active_area():
    data = request.get_json(silent=True) or {}
    side = _calibration_side(data)
    if side not in TEAMS:
        return jsonify({"ok": False, "error": "team required"}), 403
    if not _is_operator() and not _calibration_access[side]["enabled"]:
        return jsonify({"ok": False, "error": f"{side} calibration is not enabled by the gamemaster"}), 403
    try:
        active_area = _clean_active_area(data.get("active_area"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    with _calibration_lock:
        _calibration2["playfield"]["active_area"] = active_area
        _calibration2["playfield"]["updated_at"] = time.time()
        _save_calibration2()
    return jsonify({"ok": True, "calibration": _calibration2_public(),
                    "active_area": active_area})


@bp.route("/api/calibration2/reset", methods=["POST"])
def api_calibration2_reset():
    data = request.get_json(silent=True) or {}
    side = _calibration_side(data)
    if side not in TEAMS:
        return jsonify({"ok": False, "error": "team required"}), 403
    if not _is_operator() and not _calibration_access[side]["enabled"]:
        return jsonify({"ok": False, "error": f"{side} calibration is not enabled by the gamemaster"}), 403
    with _calibration_lock:
        view = _calibration2["arms"][side].get("camera_view", "normal")
        pickup_height = _calibration2["arms"][side].get("pickup_height")
        stack_drop_offset = _calibration2["arms"][side].get(
            "stack_drop_offset", 31.5)
        _calibration2["arms"][side] = _default_calibration2()["arms"][side]
        _calibration2["arms"][side]["camera_view"] = view
        _calibration2["arms"][side]["pickup_height"] = pickup_height
        _calibration2["arms"][side]["stack_drop_offset"] = stack_drop_offset
        _save_calibration2()
    return jsonify({"ok": True, "calibration": _calibration2_public()})


@bp.route("/api/playfield/save", methods=["POST"])
def api_playfield_save():
    if not _is_operator():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    playfield = _playfield_module()
    if playfield is None or not hasattr(playfield, "list_areas"):
        return jsonify({"ok": False, "error": "Playfield Areas prototype is not loaded"}), 409
    areas = playfield.list_areas()
    payload = _saved_playfield_payload(areas)
    try:
        with open(PLAYFIELD_SNAPSHOT_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as e:
        return jsonify({"ok": False, "error": f"could not save playfield: {e}"}), 500
    return jsonify({
        "ok": True,
        "count": len(payload["areas"]),
        "saved_at": payload["saved_at"],
    })


@bp.route("/api/playfield/load", methods=["POST"])
def api_playfield_load():
    if not _is_operator():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    playfield = _playfield_module()
    if playfield is None or not hasattr(playfield, "replace_areas"):
        return jsonify({"ok": False, "error": "Playfield Areas controller cannot replace areas"}), 409
    try:
        with open(PLAYFIELD_SNAPSHOT_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "no saved playfield yet"}), 404
    except (OSError, ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": f"could not read saved playfield: {e}"}), 500
    areas = payload.get("areas")
    if not isinstance(areas, list):
        return jsonify({"ok": False, "error": "saved playfield is missing areas"}), 400
    try:
        loaded = playfield.replace_areas(areas)
    except (ValueError, KeyError, TypeError) as e:
        return jsonify({"ok": False, "error": f"could not load playfield: {e}"}), 400
    if hasattr(playfield, "save_areas_now"):
        playfield.save_areas_now()
    calibration = _sync_calibration_area_refs(loaded)
    _live.bump()
    return jsonify({
        "ok": True,
        "count": len(loaded),
        "saved_at": payload.get("saved_at"),
        "calibration": calibration,
    })


@bp.route("/api/calibration/corner-map", methods=["POST"])
def api_calibration_corner_map():
    ok, err = _calibration_write_allowed()
    if not ok:
        return jsonify({"ok": False, "error": err}), 403
    data = request.get_json(silent=True) or {}
    side = _calibration_side(data)
    try:
        corner_map = _clean_corner_map(data.get("corner_map"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    with _calibration_lock:
        if side in TEAMS:
            _calibration["arms"][side]["corner_map"] = corner_map
            _calibration["arms"][side]["marker_cache"] = None
            _calibration["arms"][side]["updated_at"] = time.time()
        else:
            _calibration["playfield"]["corner_map"] = corner_map
            _calibration["playfield"]["marker_cache"] = None
            _calibration["playfield"]["updated_at"] = time.time()
        _save_calibration()
        out = json.loads(json.dumps(_calibration))
    _live.bump()
    return jsonify({"ok": True, "calibration": out})


@bp.route("/api/calibration/active-area", methods=["POST"])
def api_calibration_active_area():
    ok, err = _calibration_write_allowed()
    if not ok:
        return jsonify({"ok": False, "error": err}), 403
    data = request.get_json(silent=True) or {}
    try:
        active_area = _clean_active_area(data.get("active_area"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    with _calibration_lock:
        _calibration["playfield"]["active_area"] = active_area
        _calibration["playfield"]["updated_at"] = time.time()
        _save_calibration()
        out = json.loads(json.dumps(_calibration))
    _live.bump()
    return jsonify({
        "ok": True,
        "calibration": out,
        "active_area": out["playfield"]["active_area"],
    })


@bp.route("/api/calibration/marker-cache", methods=["POST"])
def api_calibration_marker_cache():
    if not (_is_operator() or _player_side() in TEAMS):
        return jsonify({"ok": False, "error": "team or gamemaster required"}), 403
    data = request.get_json(silent=True) or {}
    side = _calibration_side(data)
    if side is None and not _is_operator():
        side = _player_side()
    try:
        marker_cache = _clean_marker_cache(data.get("marker_cache"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    with _calibration_lock:
        if side in TEAMS:
            _calibration["arms"][side]["marker_cache"] = marker_cache
            _calibration["arms"][side]["updated_at"] = time.time()
        else:
            _calibration["playfield"]["marker_cache"] = marker_cache
            _calibration["playfield"]["updated_at"] = time.time()
        _save_calibration()
        out = json.loads(json.dumps(_calibration))
    _live.bump()
    return jsonify({"ok": True, "calibration": out})


@bp.route("/api/calibration/export.zip", methods=["POST"])
def api_calibration_export_zip():
    if not (_is_operator() or _player_side() in TEAMS):
        return jsonify({"ok": False, "error": "team or gamemaster required"}), 403
    data = request.get_json(silent=True) or {}
    active_area = data.get("active_area") if isinstance(data.get("active_area"), dict) else _calibration_public().get("playfield", {}).get("active_area", {})
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "auto-calibration.json",
            json.dumps(_calibration_public(), indent=2, sort_keys=True),
        )
        zf.writestr(
            "active-area.json",
            json.dumps({
                "active_area": active_area,
                "exported_at": _dt.datetime.now(_dt.UTC).isoformat(),
                "format": "auto-pick-and-place-calibration-v1",
            }, indent=2, sort_keys=True),
        )
    payload.seek(0)
    return Response(
        payload.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=auto-pick-calibration.zip"},
    )


@bp.route("/api/calibration/import.zip", methods=["POST"])
def api_calibration_import_zip():
    global _calibration
    player_side = _player_side()
    ok, err = _calibration_write_allowed()
    if not ok:
        return jsonify({"ok": False, "error": err}), 403
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"ok": False, "error": "zip file required"}), 400
    try:
        raw = upload.read()
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            names = set(zf.namelist())
            if "auto-calibration.json" not in names:
                return jsonify({"ok": False, "error": "auto-calibration.json missing from zip"}), 400
            saved = json.loads(zf.read("auto-calibration.json").decode("utf-8"))
            active_area = None
            if "active-area.json" in names:
                active_payload = json.loads(zf.read("active-area.json").decode("utf-8"))
                active_area = active_payload.get("active_area") if isinstance(active_payload, dict) else None
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, json.JSONDecodeError) as e:
        return jsonify({"ok": False, "error": f"invalid calibration zip: {e}"}), 400
    with _calibration_lock:
        imported = _calibration_from_saved(saved)
        if _is_operator():
            _calibration = imported
        else:
            _calibration["playfield"] = imported["playfield"]
            _calibration["arms"][player_side] = imported["arms"][player_side]
        if active_area:
            try:
                _calibration["playfield"]["active_area"] = _clean_active_area(active_area)
            except ValueError:
                pass
        _save_calibration()
        out = json.loads(json.dumps(_calibration))
    _live.bump()
    return jsonify({"ok": True, "calibration": out, "active_area": active_area})


@bp.route("/api/calibration/capture", methods=["POST"])
def api_calibration_capture():
    data = request.get_json(silent=True) or {}
    side = _calibration_side(data)
    target = data.get("target")
    if side not in TEAMS:
        return jsonify({
            "ok": False,
            "error": "team required",
            "received_side": data.get("side") if isinstance(data, dict) else None,
            "received_team": data.get("team") if isinstance(data, dict) else None,
            "role_side": _player_side(),
            "referrer_side": _referrer_side(),
        }), 403
    if not _is_operator() and not _calibration_access[side]["enabled"]:
        return jsonify({"ok": False, "error": f"{side} calibration is not enabled by the gamemaster"}), 403
    if not isinstance(target, str):
        return jsonify({"ok": False, "error": "target required"}), 400
    now = time.time()
    with _calibration_lock:
        side_cal = _calibration["arms"][side]
        if target in side_cal["points"]:
            try:
                side_cal["points"][target]["pose"] = _clean_pose(data.get("pose"))
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
        elif target == "center":
            try:
                side_cal["center"] = _clean_pose(data.get("pose"))
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
        elif target in ("pickup_height", "transport_height"):
            pose = data.get("pose")
            try:
                z = float((pose or {}).get("z", data.get("z")))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "height requires numeric z"}), 400
            side_cal[target] = {"set": True, "z": round(z, 3)}
        else:
            return jsonify({"ok": False, "error": "unknown calibration target"}), 400
        side_cal["updated_at"] = now
        _save_calibration()
        out = json.loads(json.dumps(_calibration))
    _live.bump()
    return jsonify({"ok": True, "calibration": out})


@bp.route("/api/calibration/reset", methods=["POST"])
def api_calibration_reset():
    data = request.get_json(silent=True) or {}
    side = _calibration_side(data)
    if side not in TEAMS:
        return jsonify({"ok": False, "error": "team required"}), 403
    if not _is_operator() and not _calibration_access[side]["enabled"]:
        return jsonify({"ok": False, "error": f"{side} calibration is not enabled by the gamemaster"}), 403
    with _calibration_lock:
        _calibration["arms"][side] = _default_calibration()["arms"][side]
        _save_calibration()
        out = json.loads(json.dumps(_calibration))
    _live.bump()
    return jsonify({"ok": True, "calibration": out})


@bp.route("/api/direct/state")
def api_direct_state():
    return jsonify(_direct_state_dict())


@bp.route("/api/direct/connect", methods=["POST"])
def api_direct_connect():
    denied = _direct_require_operator()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        side = _direct_side(data)
    except ValueError as e:
        return _fail(str(e)), 400
    ip = str(data.get("ip") or ROBOT_IP).strip()
    local_ip = str(data.get("local_ip") or DEFAULT_LINKS[side]["local_ip"]).strip()
    iface = str(data.get("iface") or "").strip() or None
    with _direct_locks[side]:
        old = _direct_robots.get(side)
        if old is not None:
            old.close()
            _direct_robots[side] = None
        robot = DobotMG400(ip, iface=iface, local_ip=local_ip)
        try:
            robot.connect()
        except DobotError as e:
            robot.close()
            return _fail(f"Could not connect to {ip} via {local_ip}: {e}", errid=e.errid)
        _direct_robots[side] = robot
    return _ok(side=side, ip=ip, local_ip=local_ip, iface=robot.iface)


@bp.route("/api/direct/enable", methods=["POST"])
def api_direct_enable():
    denied = _direct_require_operator()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        side = _direct_side(data)
    except ValueError as e:
        return _fail(str(e)), 400
    robot = _direct_robot(side)
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    try:
        try:
            robot.clear_error()
        except DobotError:
            pass
        errid, resp = robot.enable()
        if errid == 0:
            robot.start_servo("cartesian")
            _direct_apply_motion(robot)
        return jsonify({"ok": errid == 0, "errid": errid, "resp": resp, "side": side})
    except DobotError as e:
        return _fail(str(e), errid=e.errid)


@bp.route("/api/direct/clear_error", methods=["POST"])
def api_direct_clear_error():
    denied = _direct_require_operator()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        side = _direct_side(data)
    except ValueError as e:
        return _fail(str(e)), 400
    robot = _direct_robot(side)
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    try:
        errid, resp = robot.clear_error()
        return jsonify({"ok": errid == 0, "errid": errid, "resp": resp, "side": side})
    except DobotError as e:
        return _fail(str(e), errid=e.errid)


@bp.route("/api/direct/move", methods=["POST"])
def api_direct_move():
    denied = _direct_require_operator()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        side = _direct_side(data)
    except ValueError as e:
        return _fail(str(e)), 400
    robot = _direct_robot(side)
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    if not robot.get_state().get("enabled"):
        return _fail("Arm not enabled")
    pose = data.get("pose")
    if not (isinstance(pose, (list, tuple)) and len(pose) >= 3):
        return _fail("Expected pose [x, y, z, r]")
    try:
        x, y, z = float(pose[0]), float(pose[1]), float(pose[2])
        r = float(pose[3]) if len(pose) > 3 else robot.get_state()["pose"][3]
    except (TypeError, ValueError, IndexError):
        return _fail("pose must be numeric")
    try:
        if robot.control_mode() != "cartesian":
            robot.start_servo("cartesian")
            _direct_apply_motion(robot)
        x, y, z, r = _clamp_pose(x, y, z, r)
        robot.set_target_pose(x, y, z, r)
        return _ok(side=side, clamped={
            "x": round(x, 2), "y": round(y, 2),
            "z": round(z, 2), "r": round(r, 2),
        })
    except DobotError as e:
        return _fail(str(e), errid=e.errid)


@bp.route("/api/direct/pump", methods=["POST"])
def api_direct_pump():
    denied = _direct_require_operator()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        side = _direct_side(data)
    except ValueError as e:
        return _fail(str(e)), 400
    mode = str(data.get("mode") or "off").lower()
    if mode not in ("suck", "blow", "off"):
        return _fail("mode must be 'suck', 'blow' or 'off'")
    robot = _direct_robot(side)
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    try:
        errid, resp = robot.set_pump(mode, SUCK_DO_INDEX, BLOW_DO_INDEX)
        return jsonify({"ok": errid == 0, "errid": errid, "resp": resp, "side": side})
    except DobotError as e:
        return _fail(str(e), errid=e.errid)


@bp.route("/api/direct/stop", methods=["POST"])
def api_direct_stop():
    denied = _direct_require_operator()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        side = _direct_side(data)
    except ValueError as e:
        return _fail(str(e)), 400
    robot = _direct_robot(side)
    if robot is not None and robot.is_connected():
        robot.hold()
    return _ok(side=side)


@bp.route("/api/player", methods=["POST"])
def api_player():
    data = request.get_json(silent=True) or {}
    team = _requested_team(data)
    if team not in TEAMS:
        return jsonify({"ok": False, "error": "player team required"}), 403
    name = str(data.get("name") or "").strip()[:32]
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    with _state_lock:
        _state["teams"][team] = _team_state()
        _state["teams"][team]["player"] = name
        _state["teams"][team]["phase"] = "ready"
        _state["updated_at"] = time.time()
        out = _public_state_locked()
    _live.bump()
    return jsonify(out)


@bp.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    team = _requested_team(data)
    if team not in TEAMS:
        return jsonify({"ok": False, "error": "player team required"}), 403
    with _state_lock:
        ts = _state["teams"][team]
        if not ts["player"]:
            return jsonify({"ok": False, "error": "enter a name first"}), 409
        now = time.time()
        ts.update({
            "phase": "running", "started_at": now,
            "completed_at": None, "elapsed_seconds": None,
        })
        _state["updated_at"] = now
        out = _public_state_locked()
    _live.bump()
    return jsonify(out)


@bp.route("/api/finish", methods=["POST"])
def api_finish():
    if not _is_operator():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    data = request.get_json(silent=True) or {}
    team = data.get("team")
    if team not in TEAMS:
        return jsonify({"ok": False, "error": "team required"}), 400
    with _state_lock:
        ts = _state["teams"][team]
        if ts["phase"] != "running" or not ts["started_at"]:
            return jsonify({"ok": False, "error": "no run in progress"}), 409
        now = time.time()
        elapsed = round(now - float(ts["started_at"]), 2)
        player = ts["player"]
        entry = {
            "logged_at": _dt.datetime.now(_dt.UTC).isoformat(),
            "player": player,
            "team": team,
            "elapsed_seconds": elapsed,
            "started_at": _dt.datetime.fromtimestamp(ts["started_at"], _dt.UTC).isoformat(),
            "completed_at": _dt.datetime.fromtimestamp(now, _dt.UTC).isoformat(),
        }
        need_header = not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0
        with open(LOG_PATH, "a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
            if need_header:
                writer.writeheader()
            writer.writerow(entry)
        if _state["best_seconds"] is None or elapsed < _state["best_seconds"]:
            _state["best_seconds"] = elapsed
            _state["best_player"] = player
            _state["best_team"] = team
        ts.update({
            "phase": "done", "completed_at": now, "elapsed_seconds": elapsed,
        })
        _state["updated_at"] = now
        out = _public_state_locked()
    _live.bump()
    return jsonify(out)


@bp.route("/api/reset", methods=["POST"])
def api_reset():
    data = request.get_json(silent=True) or {}
    requested = data.get("team")
    with _state_lock:
        if requested in TEAMS and (_is_operator() or requested in _roles()):
            targets = [requested]
        elif _is_operator() and not requested:
            targets = list(TEAMS)            # operator: reset both
        else:
            side = _player_side()            # player: own team only
            targets = [side] if side in TEAMS else []
        if not targets:
            return jsonify({"ok": False, "error": "team required"}), 403
        for team in targets:
            _state["teams"][team] = _team_state()
        _state["updated_at"] = time.time()
        out = _public_state_locked()
    _live.bump()
    return jsonify(out)


@bp.route("/api/results")
def api_results():
    rows = _read_log_rows()
    top = _top_score_rows(_highscore_rows(rows))
    recent = list(rows)
    recent.reverse()
    with _state_lock:
        best = {
            "seconds": _state["best_seconds"],
            "player": _state["best_player"],
            "team": _state["best_team"],
        }
    return jsonify({
        "results": recent[:50],
        "top": top,
        "best": best,
        "count": len(rows),
        "highscore_reset_at": _state.get("highscore_reset_at") or 0.0,
    })


@bp.route("/api/results/reset-highscores", methods=["POST"])
def api_reset_highscores():
    if not _is_operator():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    now = time.time()
    with _state_lock:
        _state["highscore_reset_at"] = now
        _state["best_seconds"] = None
        _state["best_player"] = ""
        _state["best_team"] = ""
        _state["updated_at"] = now
        _save_highscore_reset_at(now)
        out = _public_state_locked()
    _live.bump()
    return jsonify({"ok": True, "state": out})


@bp.route("/api/log.csv")
def api_log_csv():
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8") as fh:
            body = fh.read()
    else:
        buf = io.StringIO()
        csv.DictWriter(buf, fieldnames=LOG_FIELDS).writeheader()
        body = buf.getvalue()
    return Response(
        body,
        mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=auto-pickup-log.csv"},
    )


_load_calibration()
_load_calibration2()
_state["highscore_reset_at"] = _load_highscore_reset_at()
_seed_best_from_log()
