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


def _roles():
    return request.environ.get("hhh.roles") or set()


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
    try:
        with open(CALIBRATION_PATH, "w", encoding="utf-8") as fh:
            json.dump(_calibration, fh, indent=2)
    except OSError:
        pass


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
    _ensure_playfield_calibration_areas()


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
    return jsonify({"ok": True, "calibration": out})


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
_state["highscore_reset_at"] = _load_highscore_reset_at()
_seed_best_from_log()
