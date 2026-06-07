"""
Playfield-areas prototype — a Flask blueprint mounted by the prototype hub.

Holds a set of "square areas" (the playfield zones / blobs of Manual Override)
in memory and exposes them over a small REST API (create / delete / set / get /
list), plus a persisted set of view/effect settings (glow, depth-of-field,
camera). Two pages talk to it:

  * controller.html — the default GUI: a list of areas (color / position / size)
                      and a View & Effects panel. The hub shows this in its tab.
  * screen.html     — the Three.js 3D view (bloom + DOF); opened "clean" in its
                      own tab from the controller.

This module is loaded by hub.py and registered under /p/playfield-areas. It has
no app.run() of its own — it only runs inside the hub server.
"""

import json
import os
import threading
import time

from flask import Blueprint, Response, jsonify, request, send_from_directory

import live   # shared push helper (prototypes/live.py)

# OpenCV is optional: only the ArUco marker endpoint needs it. If it's missing
# the rest of the playfield works and the markers simply don't render.
try:
    import cv2
    import numpy as np
    _HAS_CV = True
except Exception:
    _HAS_CV = False

HERE = os.path.dirname(os.path.abspath(__file__))

# Every prototype exposes these two names for the hub:
MANIFEST = {
    "name": "Playfield Areas",
    "description": "3D playfield zones with glow + depth-of-field. Edit areas, "
                   "glow/DOF and the camera in the controller; open the clean "
                   "screen to view them.",
    "default_page": "controller",   # what the hub embeds in its tab
    "pages": [
        {"path": "controller", "label": "Controller"},
        {"path": "screen", "label": "Open clean playfield ↗", "newtab": True},
    ],
}
bp = Blueprint("playfield_areas", __name__)

# ---- in-memory area store --------------------------------------------------
# An "area" is a coloured square sitting on the playfield:
#   { id, name, x, y, z, size, color, glow }
# Coordinates are in scene units (1 unit ~= 10 cm); y is height above the
# ground plane. `glow` scales the bloom emissive strength (0 = matte).
_store = {}
_lock = threading.Lock()
_rev = 0          # bumps on every mutation so clients can poll cheaply
_next_id = 1
_dirty = threading.Event()   # set on every mutation; a background thread flushes to disk

# Live push: this is event-driven state, so every mutation bumps and clients are
# woken immediately (see _touch / set_settings). See prototypes/live.py.
_live = live.LiveState()

DEFAULTS = {
    "name": "Area",
    "x": 0.0, "y": 0.0, "z": 0.0,
    "size": 1.0,
    "color": "#4f9dff",
    "glow": 1.0,
    "marker": 0,        # ArUco id (DICT_4X4_50) rendered on the area for tracking
    "links": [],        # ids of other areas this zone draws a glowing line to
    # per-component visibility on the clean screen, configured per area
    "show_area": True,   # the coloured box body + outline
    "show_aruco": True,  # the ArUco marker planes
    "show_links": True,  # the glowing links leaving this zone
}
SHOW_FIELDS = ("show_area", "show_aruco", "show_links")


def _new_area():
    """A fresh area dict (with its own links list, not the shared default)."""
    area = dict(DEFAULTS)
    area["links"] = []
    return area
ARUCO_COUNT = 50        # DICT_4X4_50
NUM_LIMITS = {
    "x": (-12.0, 12.0),
    "y": (-6.0, 6.0),   # height; negative drops the area below the ground plane
    "z": (-12.0, 12.0),
    "size": (0.2, 6.0),
    "glow": (0.0, 4.0),
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _coerce(field, value):
    """Coerce + clamp one incoming area field; raise on bad input."""
    if field in NUM_LIMITS:
        lo, hi = NUM_LIMITS[field]
        return _clamp(float(value), lo, hi)
    if field == "color":
        s = str(value).strip()
        if not (len(s) == 7 and s[0] == "#"):
            raise ValueError("color must be a #rrggbb hex string")
        int(s[1:], 16)
        return s
    if field == "name":
        return str(value)[:60]
    if field == "marker":
        return int(_clamp(int(float(value)), 0, ARUCO_COUNT - 1))
    if field in SHOW_FIELDS:
        return bool(value)
    raise KeyError(field)


def _apply(area, data):
    for field in ("name", "x", "y", "z", "size", "color", "glow", "marker", *SHOW_FIELDS):
        if field in data:
            area[field] = _coerce(field, data[field])
    if isinstance(data.get("links"), list):
        area["links"] = [str(x) for x in data["links"]][:64]


def _touch():
    global _rev
    _rev += 1
    _dirty.set()
    _live.bump()


# ---- programmatic API (for other prototypes via the hub) -------------------
# These let another prototype drive the playfield in-process (same store the
# REST routes use), so e.g. an animation can spawn and move areas. They mirror
# the route handlers but take/return plain dicts and never touch Flask.
def create_area(**fields):
    """Create an area; `fields` may set any of name/x/y/z/size/color/glow/marker."""
    global _next_id
    with _lock:
        area = _new_area()
        area["id"] = f"a{_next_id}"
        area["name"] = f"Area {_next_id}"
        area["marker"] = (_next_id - 1) % ARUCO_COUNT   # a distinct default id
        _next_id += 1
        _apply(area, fields)
        _store[area["id"]] = area
        _touch()
        return dict(area)


def update_area(area_id, **fields):
    """Set fields on an existing area; returns the area, or None if missing."""
    with _lock:
        area = _store.get(area_id)
        if area is None:
            return None
        _apply(area, fields)
        _touch()
        return dict(area)


def _prune_links_to(area_id):
    """Drop any links pointing at a (now-deleted) area. Call with the lock held."""
    for other in _store.values():
        if area_id in other.get("links", []):
            other["links"].remove(area_id)


def remove_area(area_id):
    """Delete an area and any links pointing to it; returns True if it existed."""
    with _lock:
        existed = _store.pop(area_id, None) is not None
        if existed:
            _prune_links_to(area_id)
            _touch()
        return existed


def list_areas():
    with _lock:
        return list(_store.values())


def add_link(area_id, to):
    """Add a glowing link from `area_id` to area `to`. Returns the area or None."""
    with _lock:
        area = _store.get(area_id)
        if area is None or to not in _store or to == area_id:
            return None
        if to not in area["links"]:
            area["links"].append(to)
            _touch()
        return dict(area)


def remove_link(area_id, to):
    with _lock:
        area = _store.get(area_id)
        if area is None or to not in area.get("links", []):
            return None
        area["links"].remove(to)
        _touch()
        return dict(area)


# ---- persisted view / effect settings -------------------------------------
SETTINGS_PATH = os.path.join(HERE, "settings.json")
DEFAULT_SETTINGS = {
    "bloom": 1.1,
    "dof": 1.2,
    "fov": 50.0,                              # camera field of view (degrees)
    "cam": {"x": 0.0, "y": 9.0, "z": 13.0},   # camera location (translation)
    "rot": {"x": -33.0, "y": 0.0, "z": 0.0},  # camera rotation, Euler degrees (z = roll)
}
SETTINGS_LIMITS = {"bloom": (0.0, 2.5), "dof": (0.0, 3.0), "fov": (10.0, 120.0)}
VEC_LIMIT = (-40.0, 40.0)
ROT_LIMIT = (-180.0, 180.0)
_srev = 0


def _coerce_settings(dst, data):
    for key in ("bloom", "dof", "fov"):
        if key in data:
            lo, hi = SETTINGS_LIMITS[key]
            dst[key] = _clamp(float(data[key]), lo, hi)
    for vec, lim in (("cam", VEC_LIMIT), ("rot", ROT_LIMIT)):
        src = data.get(vec)
        if isinstance(src, dict):
            for axis in ("x", "y", "z"):
                if axis in src:
                    dst[vec][axis] = _clamp(float(src[axis]), *lim)


def _load_settings():
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        with open(SETTINGS_PATH) as f:
            _coerce_settings(settings, json.load(f))
    except (OSError, ValueError, TypeError):
        pass
    return settings


def _save_settings():
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump(_settings, f, indent=2)
    except OSError:
        pass


_settings = _load_settings()


# ---- pages -----------------------------------------------------------------
@bp.route("/")
def index():
    return send_from_directory(HERE, "controller.html")


@bp.route("/controller")
def controller():
    return send_from_directory(HERE, "controller.html")


@bp.route("/screen")
def screen():
    return send_from_directory(HERE, "screen.html")


# ---- ArUco markers ---------------------------------------------------------
# Real OpenCV DICT_4X4_50 markers, so a top-down camera + cv2.aruco can detect
# and identify each area. Generated once per id and cached.
_marker_cache = {}
if _HAS_CV:
    _ARUCO_DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


def _marker_png(mid):
    mid = int(mid) % ARUCO_COUNT
    cached = _marker_cache.get(mid)
    if cached is not None:
        return cached
    side = 240                              # marker pixels (excl. quiet zone)
    img = cv2.aruco.generateImageMarker(_ARUCO_DICT, mid, side)
    border = side // 6                      # white quiet zone (~1 cell) for detection
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
    png = _marker_png(mid)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ---- areas + settings API --------------------------------------------------
def _snapshot():
    """Everything a client needs to render, in one dict (areas + view settings)."""
    with _lock:
        return {
            "rev": _rev, "areas": list(_store.values()),
            "srev": _srev, "settings": _settings,
        }


@bp.route("/api/state")
def state():
    return jsonify(_snapshot())


@bp.route("/api/events")
def events():
    """Server-Sent Events: push the full state whenever any area or setting
    changes (every mutation calls _live.bump). Clients open an EventSource here
    instead of polling. See prototypes/live.py."""
    return _live.stream(_snapshot)


@bp.route("/api/areas/<area_id>/links", methods=["POST"])
def route_add_link(area_id):
    to = (request.json or {}).get("to")
    if not to:
        return jsonify({"ok": False, "error": "need target area id 'to'"}), 400
    area = add_link(area_id, to)
    if area is None:
        return jsonify({"ok": False, "error": "unknown area / target / self-link"}), 400
    return jsonify({"ok": True, "area": area, "rev": _rev})


@bp.route("/api/areas/<area_id>/links/<to>", methods=["DELETE"])
def route_remove_link(area_id, to):
    area = remove_link(area_id, to)
    if area is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "area": area, "rev": _rev})


@bp.route("/api/areas", methods=["GET"])
def route_list_areas():
    with _lock:
        return jsonify({"rev": _rev, "areas": list(_store.values())})


@bp.route("/api/areas", methods=["POST"])
def route_create_area():
    global _next_id
    data = request.json or {}
    with _lock:
        area = _new_area()
        area["id"] = f"a{_next_id}"
        area["name"] = f"Area {_next_id}"
        area["marker"] = (_next_id - 1) % ARUCO_COUNT
        _next_id += 1
        try:
            _apply(area, data)
        except (ValueError, KeyError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        _store[area["id"]] = area
        _touch()
        return jsonify({"ok": True, "area": area, "rev": _rev}), 201


@bp.route("/api/areas/<area_id>", methods=["GET"])
def get_area(area_id):
    with _lock:
        area = _store.get(area_id)
        if area is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "area": area})


@bp.route("/api/areas/<area_id>", methods=["PATCH", "PUT"])
def set_area(area_id):
    data = request.json or {}
    with _lock:
        area = _store.get(area_id)
        if area is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        try:
            _apply(area, data)
        except (ValueError, KeyError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        _touch()
        return jsonify({"ok": True, "area": area, "rev": _rev})


@bp.route("/api/areas/<area_id>", methods=["DELETE"])
def delete_area(area_id):
    with _lock:
        if _store.pop(area_id, None) is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        _prune_links_to(area_id)
        _touch()
        return jsonify({"ok": True, "rev": _rev})


@bp.route("/api/settings", methods=["GET"])
def get_settings():
    with _lock:
        return jsonify({"srev": _srev, "settings": _settings})


@bp.route("/api/settings", methods=["PATCH", "PUT"])
def set_settings():
    global _srev
    data = request.json or {}
    with _lock:
        try:
            _coerce_settings(_settings, data)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "bad settings value"}), 400
        _srev += 1
        _save_settings()
    _live.bump()
    return jsonify({"ok": True, "settings": _settings, "srev": _srev})


def _seed():
    """Starter areas so the playfield shows something on a first-ever run."""
    global _next_id
    presets = [
        {"name": "Player A zone", "x": -4.0, "z": 0.0, "size": 2.0, "color": "#4f9dff", "glow": 1.4},
        {"name": "Contested",     "x":  0.0, "z": 0.0, "size": 1.5, "color": "#f5a623", "glow": 2.0},
        {"name": "Player B zone", "x":  4.0, "z": 0.0, "size": 2.0, "color": "#36c46b", "glow": 1.4},
    ]
    for p in presets:
        area = _new_area()
        area["id"] = f"a{_next_id}"
        area["marker"] = (_next_id - 1) % ARUCO_COUNT
        _next_id += 1
        _apply(area, p)
        _store[area["id"]] = area


# ---- persistence: areas survive restarts -----------------------------------
# Areas live in areas.json next to this file: loaded on startup, written by a
# background thread that batches rapid changes (so a 30 fps animation doesn't
# hammer the disk). Once the file exists it is the source of truth — the seed
# presets only appear on a first-ever run, so deletes stick across restarts.
AREAS_PATH = os.path.join(HERE, "areas.json")


def _save_areas():
    with _lock:
        data = {"next_id": _next_id, "areas": list(_store.values())}
    try:
        with open(AREAS_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def _load_areas():
    """Populate the store from disk. Returns True if a saved file was loaded."""
    global _next_id
    try:
        with open(AREAS_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return False
    areas = data.get("areas")
    if not isinstance(areas, list):
        return False
    for raw in areas:
        if isinstance(raw, dict) and "id" in raw:
            area = _new_area()
            area["id"] = str(raw["id"])
            _apply(area, raw)            # coerce/clamp known fields
            _store[area["id"]] = area
    _next_id = int(data.get("next_id", _next_id))
    # drop links that point at areas which no longer exist
    for area in _store.values():
        area["links"] = [t for t in area.get("links", []) if t in _store]
    return True


def _saver_loop():
    while True:
        _dirty.wait()        # block until something changes
        time.sleep(1.0)      # batch a burst of changes into one write
        _dirty.clear()
        _save_areas()


if not _load_areas():
    _seed()
threading.Thread(target=_saver_loop, name="playfield-saver", daemon=True).start()
