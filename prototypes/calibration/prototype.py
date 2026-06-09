"""
Calibration prototype — rebuilt minimal (work in progress).

For now it just shows the webcam feed + the tracked ArUco tags, and a button that
draws the robot work-area corners (TL / TR / BR / BL) into the Playfield Areas
scene as purple zones linked into a square. The rest of the calibration
functionality is being added back step by step.

Loaded by hub.py; registered under /p/calibration. hub_init() hands us the
context used to reach the webcam (for tags) and the playfield (to draw into).
"""

import json
import os
import time

from flask import Blueprint, Response, jsonify, request, send_from_directory

import live   # shared push helper (prototypes/live.py)

# OpenCV is optional: only the manual-pin marker overlay needs it. Without it the
# rest of the calibration works and the overlay simply has no marker image.
try:
    import cv2
    import numpy as np
    _HAS_CV = True
except Exception:
    _HAS_CV = False

HERE = os.path.dirname(os.path.abspath(__file__))

MANIFEST = {
    "name": "Calibration",
    "description": "Webcam feed + tracked ArUco tags, and a button to draw the "
                   "robot work area into the Playfield. Rebuilding step by step.",
    "default_page": "controller",
    "pages": [{"path": "controller", "label": "Controller"}],
}
bp = Blueprint("calibration", __name__)

WEBCAM = "webcam"
PLAYFIELD = "playfield-areas"
CARTESIAN = "cartesian-xyz-test"

# Game-space work-area corners (X, Y). Drawn flat on the playfield floor as purple
# TL/TR/BR/BL zones: playfield x = game X, z = game Y, y = 0 (ground). X is
# mirrored (negated) because the camera views the field from underneath, so left
# and right are flipped. Order is the square-link loop.
WORK_AREA = {
    "TL": (8.7, -4.7),
    "TR": (-8.7, -4.7),
    "BR": (-8.7, 4.7),
    "BL": (8.7, 4.7),
}
WORK_AREA_ORDER = ["TL", "TR", "BR", "BL"]
WORK_AREA_MARKERS = {"TL": 1, "TR": 2, "BR": 3, "BL": 4}   # ArUco ids
WORK_AREA_COLOR = "#a855f7"   # purple
WORK_AREA_SIZE = 1.5

# Blue center point (ArUco id 0) at the centroid of the four corners.
CENTER_NAME = "Center"
CENTER_MARKER = 0
CENTER_COLOR = "#4f9dff"   # blue
CENTER_SIZE = 2.0

_ctx = None
# Sampled state (live webcam tags change continuously), so the stream re-snapshots
# on a short interval rather than on a bump. See prototypes/live.py.
_live = live.LiveState()

# Robot Cartesian XY entered per corner, persisted so they survive tab switches
# (the iframe reloads) and restarts.
ROBOT_PATH = os.path.join(HERE, "robot-corners.json")
_CORNERS = ["TL", "TR", "BR", "BL", "Center"]
_robot_xy = {c: {"x": None, "y": None} for c in _CORNERS}


def _load_robot():
    try:
        with open(ROBOT_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return
    for c in _CORNERS:
        v = data.get(c)
        if isinstance(v, dict):
            for k in ("x", "y"):
                try:
                    _robot_xy[c][k] = None if v.get(k) is None else float(v[k])
                except (TypeError, ValueError):
                    pass


def _save_robot():
    try:
        with open(ROBOT_PATH, "w") as f:
            json.dump(_robot_xy, f, indent=2)
    except OSError:
        pass


_load_robot()

# Head/tool ArUco tag, and the approach height used when capturing where the head
# appears at a point (mm) before going straight down.
HEAD_TAG = 10
APPROACH_Z = -60.0
CALIB_POINTS = ["Center", "TL", "TR", "BR", "BL"]   # order used by "calibrate all"

# The pointer tag dropped where you click the video (ArUco id 5, green).
POINTER_NAME = "Pointer"
POINTER_MARKER = 5
POINTER_COLOR = "#36c46b"

# Captured calibration points, persisted (e.g. {"Center": {...}}).
CALIB_PATH = os.path.join(HERE, "calib-points.json")
_calib = {}


def _load_calib():
    global _calib
    try:
        with open(CALIB_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            _calib = data
    except (OSError, ValueError, TypeError):
        pass


def _save_calib():
    try:
        with open(CALIB_PATH, "w") as f:
            json.dump(_calib, f, indent=2)
    except OSError:
        pass


_load_calib()

# Work / move Z heights (mm), persisted. "work" = surface / pick height; "move" =
# safe travel height used by the per-point Move buttons.
LEVELS_PATH = os.path.join(HERE, "levels.json")
_levels = {"work": -125.0, "move": -16.0}
_move_target = "move"   # which level the Move buttons drive to ("work" or "move")


def _load_levels():
    global _move_target
    try:
        with open(LEVELS_PATH) as f:
            data = json.load(f)
        for k in ("work", "move"):
            if k in data:
                _levels[k] = float(data[k])
        if data.get("move_target") in ("work", "move"):
            _move_target = data["move_target"]
    except (OSError, ValueError, TypeError):
        pass


def _save_levels():
    try:
        with open(LEVELS_PATH, "w") as f:
            json.dump({**_levels, "move_target": _move_target}, f, indent=2)
    except OSError:
        pass


_load_levels()


# ---- three-layer corner calibration ----------------------------------------
# For each corner we capture up to three SCREEN positions, all of the same
# physical corner but on different planes (so each plane has its own UV):
#   "screen" : the corner's own ArUco tag (ids 1-4) lying on the work surface
#   "work"   : head tag #10 with the TCP parked over the corner at work-level Z
#   "move"   : head tag #10 with the TCP parked over the corner at move-level Z
# The surface plane is the one a click should map through; the two head planes
# show the head tag's parallax at each working height. Drawn as 3 overlay layers.
CORNERS4 = ["TL", "TR", "BR", "BL"]
CORNER_TAGS = {"TL": 1, "TR": 2, "BR": 3, "BL": 4}   # ArUco ids on the surface
LAYER_KEYS = ["screen", "work", "move"]
# The camera views the field from UNDERNEATH, so the surface corner tags land
# left-right mirrored vs the robot/scene frame (same flip WORK_AREA bakes in). When
# mapping a click we pair each robot/playground corner with the screen tag of its
# left-right mirror, so the same physical corner is matched.
MIRROR_LR = {"TL": "TR", "TR": "TL", "BR": "BL", "BL": "BR"}

LAYERS3_PATH = os.path.join(HERE, "corner-layers.json")
_corner_layers = {c: {k: None for k in LAYER_KEYS} for c in CORNERS4}


def _load_corner_layers():
    try:
        with open(LAYERS3_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    for c in CORNERS4:
        v = data.get(c)
        if not isinstance(v, dict):
            continue
        for k in LAYER_KEYS:
            u = v.get(k)
            if isinstance(u, dict) and u.get("x") is not None:
                _corner_layers[c][k] = {f: u.get(f) for f in ("x", "y", "nx", "ny")}


def _save_corner_layers():
    try:
        with open(LAYERS3_PATH, "w") as f:
            json.dump(_corner_layers, f, indent=2)
    except OSError:
        pass


_load_corner_layers()


# ---- cross-prototype access ------------------------------------------------
def _webcam():
    return _ctx.get_prototype(WEBCAM) if _ctx else None


def _webcam_on():
    return bool(_ctx and _ctx.is_prototype_enabled(WEBCAM))


def _playfield():
    return _ctx.get_prototype(PLAYFIELD) if _ctx else None


def _playfield_on():
    return bool(_ctx and _ctx.is_prototype_enabled(PLAYFIELD))


def _cartesian():
    return _ctx.get_prototype(CARTESIAN) if _ctx else None


def _cartesian_on():
    return bool(_ctx and _ctx.is_prototype_enabled(CARTESIAN))


def _tags():
    cam = _webcam()
    if cam is None or not _webcam_on():
        return []
    try:
        return cam.get_tags()
    except Exception:
        return []


# ---- pages -----------------------------------------------------------------
@bp.route("/")
@bp.route("/controller")
def controller():
    return send_from_directory(HERE, "controller.html")


# ---- ArUco marker images (DICT_4X4_50) for the manual-pin overlay -----------
ARUCO_COUNT = 50
_marker_cache = {}
if _HAS_CV:
    _ARUCO_DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


def _marker_png(mid):
    mid = int(mid) % ARUCO_COUNT
    if mid in _marker_cache:
        return _marker_cache[mid]
    side = 240
    img = cv2.aruco.generateImageMarker(_ARUCO_DICT, mid, side)
    border = side // 6                       # white quiet zone, like the printed tag
    canvas = np.full((side + 2 * border, side + 2 * border), 255, np.uint8)
    canvas[border:border + side, border:border + side] = img
    ok, buf = cv2.imencode(".png", canvas)
    data = buf.tobytes() if ok else b""
    _marker_cache[mid] = data
    return data


@bp.route("/api/marker/<int:mid>")
def marker_png(mid):
    if not _HAS_CV:
        return jsonify({"ok": False, "error": "opencv not installed"}), 503
    resp = Response(_marker_png(mid), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ---- live state ------------------------------------------------------------
def _state_dict():
    return {
        "tags": _tags(),
        "webcam_on": _webcam_on(),
        "playfield_installed": _playfield() is not None,
        "playfield_on": _playfield_on(),
        "calib": _calib,
        "robot_corners": _robot_xy,
        "levels": _levels,
        "move_target": _move_target,
        "corner_layers": _corner_layers,
    }


@bp.route("/api/state")
def api_state():
    return jsonify(_state_dict())


@bp.route("/api/events")
def events():
    """Push the tracked tags ~3x/s while they change."""
    return _live.stream(_state_dict, interval=0.3)


# ---- draw the work area into the playfield ---------------------------------
@bp.route("/api/draw-work-area", methods=["POST"])
def draw_work_area():
    """Draw TL/TR/BR/BL into the playfield as purple zones linked into a square.
    A corner that already exists (by name) is kept as-is, not overwritten."""
    pf = _playfield()
    if pf is None:
        return jsonify({"ok": False, "error": "playfield-areas is not installed"}), 404
    if not _playfield_on():
        return jsonify({"ok": False, "error": "playfield-areas is disabled — enable it first"}), 409

    existing = {a.get("name"): a for a in pf.list_areas()}
    corners, created, kept = {}, [], []
    # Create only the MISSING corners; keep (don't overwrite) ones already placed,
    # so manual moves survive.
    for name in WORK_AREA_ORDER:
        if name in existing:
            corners[name] = existing[name]
            kept.append(name)
        else:
            gx, gy = WORK_AREA[name]
            corners[name] = pf.create_area(name=name, x=gx, y=0.0, z=gy, size=WORK_AREA_SIZE,
                                           color=WORK_AREA_COLOR, glow=1.6,
                                           marker=WORK_AREA_MARKERS[name])
            created.append(name)

    # Re-centre the blue centre point on the ACTUAL corner positions, so if you've
    # moved corners around by hand the centre snaps back to the middle.
    n = len(WORK_AREA_ORDER)
    cx = round(sum(corners[name]["x"] for name in WORK_AREA_ORDER) / n, 3)
    cy = round(sum(corners[name]["y"] for name in WORK_AREA_ORDER) / n, 3)
    cz = round(sum(corners[name]["z"] for name in WORK_AREA_ORDER) / n, 3)
    if CENTER_NAME in existing:
        pf.update_area(existing[CENTER_NAME]["id"], x=cx, y=cy, z=cz, size=CENTER_SIZE)
        center_action = "re-centred"
    else:
        pf.create_area(name=CENTER_NAME, x=cx, y=cy, z=cz, size=CENTER_SIZE,
                       color=CENTER_COLOR, glow=1.6, marker=CENTER_MARKER)
        center_action = "created"

    # Ensure the square links exist (idempotent): TL -> TR -> BR -> BL -> TL.
    for i in range(n):
        try:
            pf.add_link(corners[WORK_AREA_ORDER[i]]["id"],
                        corners[WORK_AREA_ORDER[(i + 1) % n]]["id"])
        except Exception:
            pass

    return jsonify({"ok": True, "created": created, "kept": kept,
                    "center": {"action": center_action, "x": cx, "y": cy, "z": cz}})


# ---- persisted per-corner robot coordinates --------------------------------
@bp.route("/api/robot-corners", methods=["GET"])
def get_robot_corners():
    return jsonify({"corners": _robot_xy})


@bp.route("/api/robot-corners", methods=["PATCH"])
def set_robot_corners():
    """Save one corner's robot X/Y (null clears a field). Persisted to disk."""
    data = request.get_json(silent=True) or {}
    corner = data.get("corner")
    if corner not in _CORNERS:
        return jsonify({"ok": False, "error": "unknown corner"}), 400
    for k in ("x", "y"):
        if k in data:
            v = data[k]
            if v in (None, ""):
                _robot_xy[corner][k] = None
            else:
                try:
                    _robot_xy[corner][k] = float(v)
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "x/y must be numbers"}), 400
    _save_robot()
    _live.bump()   # sync the other windows now
    return jsonify({"ok": True, "corner": corner, "value": _robot_xy[corner]})


# ---- persisted work / move levels ------------------------------------------
@bp.route("/api/levels", methods=["GET"])
def get_levels():
    return jsonify({"levels": _levels, "move_target": _move_target})


@bp.route("/api/levels", methods=["PATCH"])
def set_levels():
    global _move_target
    data = request.get_json(silent=True) or {}
    for k in ("work", "move"):
        if k in data:
            try:
                _levels[k] = float(data[k])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{k} must be a number"}), 400
    if "move_target" in data:
        if data["move_target"] not in ("work", "move"):
            return jsonify({"ok": False, "error": "move_target must be 'work' or 'move'"}), 400
        _move_target = data["move_target"]
    _save_levels()
    _live.bump()   # sync the other windows now
    return jsonify({"ok": True, "levels": _levels, "move_target": _move_target})


# ---- move the head over a point at the move level ---------------------------
def _robot_motion_error():
    cart = _cartesian()
    if cart is None:
        return "cartesian-xyz-test is not installed"
    if not _cartesian_on():
        return "Cartesian prototype is disabled"
    try:
        if not cart.robot_ready():
            return "robot not ready — connect + enable in the Cartesian tab"
    except Exception:
        return "robot not ready — connect + enable in the Cartesian tab"
    return None


@bp.route("/api/move-to/<point>", methods=["POST"])
def move_to_point(point):
    """Move the head over `point` (its robot XY) at the move level Z. Non-blocking
    — the follower eases there."""
    if point not in CALIB_POINTS:
        return jsonify({"ok": False, "error": "unknown point"}), 400
    err = _robot_motion_error()
    if err:
        return jsonify({"ok": False, "error": err}), 409
    p = _robot_xy.get(point)
    if not p or p["x"] is None or p["y"] is None:
        return jsonify({"ok": False, "error": f"{point} robot coordinates are missing"}), 409
    level = _move_target if _move_target in _levels else "move"
    z = _levels[level]
    ok, reason = _cartesian().move_to(p["x"], p["y"], z, wait=False)
    if not ok:
        return jsonify({"ok": False, "error": reason or "move failed"}), 502
    return jsonify({"ok": True, "point": point, "level": level, "z": z})


# ---- read the live robot pose from the Cartesian prototype ------------------
@bp.route("/api/robot-pose")
def robot_pose():
    """Current TCP pose (X/Y/Z/R, mm/deg) from the Cartesian prototype's robot —
    used by the per-corner 'Get' buttons to capture the arm's position."""
    cart = _cartesian()
    if cart is None:
        return jsonify({"ok": False, "error": "cartesian-xyz-test is not installed"}), 404
    if not _cartesian_on():
        return jsonify({"ok": False, "error": "Cartesian prototype is disabled — enable it"}), 409
    try:
        pose = cart.current_pose()
    except Exception:
        pose = None
    if not pose:
        return jsonify({"ok": False,
                        "error": "no live pose — connect + enable the robot in the Cartesian tab"}), 409
    return jsonify({"ok": True, "x": round(pose[0], 2), "y": round(pose[1], 2),
                    "z": round(pose[2], 2), "r": round(pose[3], 2)})


# ---- calibrate a point: auto-drive the head, then record --------------------
def _prereq_error():
    """Shared preconditions for any calibration move. None means OK."""
    cart, cam = _cartesian(), _webcam()
    if cart is None:
        return "cartesian-xyz-test is not installed"
    if not _cartesian_on():
        return "Cartesian prototype is disabled"
    if cam is None or not _webcam_on():
        return "webcam is off"
    try:
        if not cart.robot_ready():
            return "robot not ready — connect + enable in the Cartesian tab"
    except Exception:
        return "robot not ready — connect + enable in the Cartesian tab"
    return None


def _capture_point(point):
    """Move the head to `point` (robot XY at Z=-60), read the head tag (#10), record
    its camera nx/ny + screen pixels and the point's playground position, then park
    at TL out of the way. Returns (record, None) or (None, error)."""
    cart, cam = _cartesian(), _webcam()
    p, tl = _robot_xy.get(point), _robot_xy["TL"]
    if not p or p["x"] is None or p["y"] is None:
        return None, f"{point} robot coordinates are missing"
    if tl["x"] is None or tl["y"] is None:
        return None, "TL robot coordinates are missing"

    ok, reason = cart.move_to(p["x"], p["y"], APPROACH_Z)
    if not ok:
        return None, f"move to {point} failed: {reason}"

    head = None
    for _ in range(30):                       # ~3 s for a fresh sighting
        t = cam.get_tag(HEAD_TAG)
        if t and float(t.get("missing", 99)) <= 0.4:
            head = t
            break
        time.sleep(0.1)
    if not head:
        return None, f"head tag #{HEAD_TAG} not seen at {point}"

    playground = None
    pf = _playfield()
    if pf is not None:
        for a in pf.list_areas():
            if a.get("name") == point:
                playground = {"x": a["x"], "z": a["z"]}
                break

    cart.move_to(tl["x"], tl["y"], APPROACH_Z)   # park out of the way

    rec = {
        "robot": {"x": p["x"], "y": p["y"], "z": APPROACH_Z},
        "head_cam": {"nx": head["nx"], "ny": head["ny"]},
        "head_px": {"x": head["x"], "y": head["y"]},
        "playground": playground,
    }
    _calib[point] = rec
    return rec, None


@bp.route("/api/calibrate/<point>", methods=["POST"])
def calibrate_point(point):
    """Calibrate a single point (Center/TL/TR/BR/BL)."""
    if point not in CALIB_POINTS:
        return jsonify({"ok": False, "error": "unknown point"}), 400
    err = _prereq_error()
    if err:
        return jsonify({"ok": False, "error": err}), 409
    rec, cerr = _capture_point(point)
    if cerr:
        return jsonify({"ok": False, "error": cerr}), 409
    _save_calib()
    _live.bump()
    return jsonify({"ok": True, "point": point, "record": rec})


@bp.route("/api/calibrate-all", methods=["POST"])
def calibrate_all():
    """Calibrate every point that has robot coordinates; skip ones without."""
    err = _prereq_error()
    if err:
        return jsonify({"ok": False, "error": err}), 409
    done, skipped, failed = [], [], {}
    for point in CALIB_POINTS:
        p = _robot_xy.get(point)
        if not p or p["x"] is None or p["y"] is None:
            skipped.append(point)
            continue
        rec, cerr = _capture_point(point)
        if cerr:
            failed[point] = cerr
        else:
            done.append(point)
    if done:
        _save_calib()
        _live.bump()
    return jsonify({"ok": True, "done": done, "skipped": skipped, "failed": failed})


@bp.route("/api/calibrate-manual/<point>", methods=["POST"])
def calibrate_manual(point):
    """Record a manually-pinned tag for `point` (no robot motion). The controller
    sends the pinned tag centre (px + nx/ny) and its four corners (px)."""
    if point not in CALIB_POINTS:
        return jsonify({"ok": False, "error": "unknown point"}), 400
    data = request.get_json(silent=True) or {}
    head_px, head_cam, corners = data.get("head_px"), data.get("head_cam"), data.get("corners")
    if not isinstance(head_px, dict) or not isinstance(head_cam, dict):
        return jsonify({"ok": False, "error": "bad pin data"}), 400
    p = _robot_xy.get(point)
    robot = ({"x": p["x"], "y": p["y"], "z": None}
             if p and p["x"] is not None and p["y"] is not None else None)
    playground = None
    pf = _playfield()
    if pf is not None:
        for a in pf.list_areas():
            if a.get("name") == point:
                playground = {"x": a["x"], "z": a["z"]}
                break
    _calib[point] = {
        "robot": robot,
        "head_cam": {"nx": head_cam.get("nx"), "ny": head_cam.get("ny")},
        "head_px": {"x": head_px.get("x"), "y": head_px.get("y")},
        "corners": corners,
        "playground": playground,
        "manual": True,
    }
    _save_calib()
    _live.bump()
    return jsonify({"ok": True, "point": point, "record": _calib[point]})


# ---- three-layer corner recording ------------------------------------------
def _read_tag_uv(cam, tag_id, fresh=0.4, tries=30):
    """Wait briefly for a fresh sighting of `tag_id`; return its screen UV
    (px + normalised) or None."""
    for _ in range(tries):
        t = cam.get_tag(tag_id)
        if t and float(t.get("missing", 99)) <= fresh:
            return {"x": round(float(t["x"]), 1), "y": round(float(t["y"]), 1),
                    "nx": round(float(t["nx"]), 4), "ny": round(float(t["ny"]), 4)}
        time.sleep(0.1)
    return None


@bp.route("/api/record-corner/<corner>/<layer>", methods=["POST"])
def record_corner(corner, layer):
    """Capture one corner on one plane:
      screen -> read that corner's own tag (id 1-4) where it sits on the surface;
      work/move -> drive head tag #10 over the corner at that level's Z, read it.
    Stores the screen UV into the 3-layer overlay set."""
    if corner not in CORNERS4:
        return jsonify({"ok": False, "error": "unknown corner"}), 400
    if layer not in LAYER_KEYS:
        return jsonify({"ok": False, "error": "unknown layer"}), 400

    # Manual pin: the client sends the pixel it clicked (used when the tag can't be
    # auto-detected at this level). No camera read, no robot motion.
    data = request.get_json(silent=True) or {}
    px = data.get("px")
    if isinstance(px, dict) and px.get("x") is not None and px.get("y") is not None:
        cam_uv = data.get("cam") or {}
        try:
            uv = {"x": round(float(px["x"]), 1), "y": round(float(px["y"]), 1),
                  "nx": None if cam_uv.get("nx") is None else round(float(cam_uv["nx"]), 4),
                  "ny": None if cam_uv.get("ny") is None else round(float(cam_uv["ny"]), 4)}
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad pixel"}), 400
        _corner_layers[corner][layer] = uv
        _save_corner_layers()
        _live.bump()
        return jsonify({"ok": True, "corner": corner, "layer": layer, "uv": uv, "manual": True})

    cam = _webcam()
    if cam is None or not _webcam_on():
        return jsonify({"ok": False, "error": "webcam is off"}), 409

    if layer == "screen":
        tag_id = CORNER_TAGS[corner]
        uv = _read_tag_uv(cam, tag_id)
        if uv is None:
            return jsonify({"ok": False, "error": f"corner tag #{tag_id} not seen"}), 409
    else:
        err = _prereq_error()
        if err:
            return jsonify({"ok": False, "error": err}), 409
        p = _robot_xy.get(corner)
        if not p or p["x"] is None or p["y"] is None:
            return jsonify({"ok": False, "error": f"{corner} robot coordinates are missing"}), 409
        z = _levels.get(layer)
        if z is None:
            return jsonify({"ok": False, "error": f"{layer} level is not set"}), 409
        ok, reason = _cartesian().move_to(p["x"], p["y"], z)
        if not ok:
            return jsonify({"ok": False, "error": f"move to {corner} failed: {reason}"}), 502
        uv = _read_tag_uv(cam, HEAD_TAG)
        if uv is None:
            return jsonify({"ok": False, "error": f"head tag #{HEAD_TAG} not seen at {layer} level"}), 409

    _corner_layers[corner][layer] = uv
    _save_corner_layers()
    _live.bump()
    return jsonify({"ok": True, "corner": corner, "layer": layer, "uv": uv})


# ---- click-to-move: video pixel -> robot move + pointer tag -----------------
@bp.route("/api/click", methods=["POST"])
def click_point():
    """Map a clicked video pixel through the four SCREEN-layer corners (the corner
    tags on the work surface) to robot XY (and move the head there) and to
    playground XZ (drop a green pointer tag, id 5). Source = surface plane, so the
    click, the corners and the tool tip all live on the same plane."""
    if not _HAS_CV:
        return jsonify({"ok": False, "error": "opencv needed for the click mapping"}), 503
    data = request.get_json(silent=True) or {}
    px = data.get("px")
    if not isinstance(px, dict) or px.get("x") is None or px.get("y") is None:
        return jsonify({"ok": False, "error": "bad pixel"}), 400

    pf = _playfield()
    pf_areas = {a.get("name"): a for a in pf.list_areas()} if pf is not None else {}
    src, rob, pg = [], [], []
    have_pg = True
    for n in ("TL", "TR", "BR", "BL"):
        sn = MIRROR_LR[n]                      # camera is from underneath -> mirror the screen tag
        s = _corner_layers[sn].get("screen")
        if not s or s.get("x") is None:
            return jsonify({"ok": False, "error": f"{sn} screen layer is not recorded yet"}), 409
        src.append([float(s["x"]), float(s["y"])])
        r = _robot_xy.get(n)
        if not r or r.get("x") is None or r.get("y") is None:
            return jsonify({"ok": False, "error": f"{n} has no robot coordinates"}), 409
        rob.append([float(r["x"]), float(r["y"])])
        a = pf_areas.get(n)
        if a and a.get("x") is not None:
            pg.append([float(a["x"]), float(a["z"])])
        else:
            have_pg = False

    src_np = np.array(src, np.float32)
    pt = np.array([[[float(px["x"]), float(px["y"])]]], np.float32)

    h_rob = cv2.getPerspectiveTransform(src_np, np.array(rob, np.float32))
    rxy = cv2.perspectiveTransform(pt, h_rob)[0, 0]
    rx, ry = round(float(rxy[0]), 2), round(float(rxy[1]), 2)

    # move the head there (at the selected move level), if the robot is ready
    move_error = _robot_motion_error()
    moved = False
    if move_error is None:
        z = _levels.get(_move_target, _levels["move"])
        ok, reason = _cartesian().move_to(rx, ry, z, wait=False)
        moved = ok
        if not ok:
            move_error = reason or "move failed"

    # place / move the pointer tag (id 5) in the playground
    playground = None
    if have_pg:
        h_pg = cv2.getPerspectiveTransform(src_np, np.array(pg, np.float32))
        pxz = cv2.perspectiveTransform(pt, h_pg)[0, 0]
        playground = {"x": round(float(pxz[0]), 3), "z": round(float(pxz[1]), 3)}
        pf = _playfield()
        if pf is not None and _playfield_on():
            existing = next((a for a in pf.list_areas() if a.get("name") == POINTER_NAME), None)
            if existing:
                pf.update_area(existing["id"], x=playground["x"], z=playground["z"])
            else:
                pf.create_area(name=POINTER_NAME, x=playground["x"], y=0.0, z=playground["z"],
                               size=1.0, color=POINTER_COLOR, glow=1.6, marker=POINTER_MARKER)

    return jsonify({"ok": True, "robot": {"x": rx, "y": ry}, "moved": moved,
                    "move_error": move_error, "playground": playground})


def hub_init(ctx):
    global _ctx
    _ctx = ctx
