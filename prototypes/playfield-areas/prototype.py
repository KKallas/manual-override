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

from flask import Blueprint, jsonify, request, send_from_directory

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

DEFAULTS = {
    "name": "Area",
    "x": 0.0, "y": 0.0, "z": 0.0,
    "size": 1.0,
    "color": "#4f9dff",
    "glow": 1.0,
}
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
    raise KeyError(field)


def _apply(area, data):
    for field in ("name", "x", "y", "z", "size", "color", "glow"):
        if field in data:
            area[field] = _coerce(field, data[field])


def _touch():
    global _rev
    _rev += 1
    _dirty.set()


# ---- programmatic API (for other prototypes via the hub) -------------------
# These let another prototype drive the playfield in-process (same store the
# REST routes use), so e.g. an animation can spawn and move areas. They mirror
# the route handlers but take/return plain dicts and never touch Flask.
def create_area(**fields):
    """Create an area; `fields` may set any of name/x/y/z/size/color/glow."""
    global _next_id
    with _lock:
        area = dict(DEFAULTS)
        area["id"] = f"a{_next_id}"
        area["name"] = f"Area {_next_id}"
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


def remove_area(area_id):
    """Delete an area; returns True if it existed."""
    with _lock:
        existed = _store.pop(area_id, None) is not None
        if existed:
            _touch()
        return existed


def list_areas():
    with _lock:
        return list(_store.values())


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


# ---- areas + settings API --------------------------------------------------
@bp.route("/api/state")
def state():
    with _lock:
        return jsonify({
            "rev": _rev, "areas": list(_store.values()),
            "srev": _srev, "settings": _settings,
        })


@bp.route("/api/areas", methods=["GET"])
def route_list_areas():
    with _lock:
        return jsonify({"rev": _rev, "areas": list(_store.values())})


@bp.route("/api/areas", methods=["POST"])
def route_create_area():
    global _next_id
    data = request.json or {}
    with _lock:
        area = dict(DEFAULTS)
        area["id"] = f"a{_next_id}"
        area["name"] = f"Area {_next_id}"
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
        area = dict(DEFAULTS)
        area["id"] = f"a{_next_id}"
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
            area = dict(DEFAULTS)
            area["id"] = str(raw["id"])
            _apply(area, raw)            # coerce/clamp known fields
            _store[area["id"]] = area
    _next_id = int(data.get("next_id", _next_id))
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
