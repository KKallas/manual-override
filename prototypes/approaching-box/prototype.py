"""
Approaching Box prototype — drives the Playfield Areas module.

A background loop spawns a blue box and tweens ONE of its properties from a
start value to an end value, holds, deletes it, waits ~1 s, and repeats. Which
property is tweened is chosen from a dropdown in the controller (depth, x,
height, size, or glow) and is saved to config.json so it survives a restart.

It owns no rendering of its own: it creates/moves/deletes *areas* in the
Playfield Areas prototype, so you watch it on the Playfield tab. This is a demo
of one prototype depending on another.

Behaviour the loop guarantees:
  * If THIS prototype is disabled in the hub  -> the loop idles and removes its
    box, so nothing happens.
  * If the Playfield module is missing/disabled -> status error; the controller
    explains and offers an Enable button.
  * Otherwise the box appears and the chosen value changes live, visible in the
    Playfield controller and screen.

Loaded by hub.py; registered under /p/approaching-box. The hub calls hub_init()
to hand us a context for checking enable state and reaching the playfield module.
"""

import json
import os
import threading

from flask import Blueprint, jsonify, request, send_from_directory

import live   # shared push helper (prototypes/live.py)

HERE = os.path.dirname(os.path.abspath(__file__))

MANIFEST = {
    "name": "Approaching Box",
    "description": "Spawns a blue box and tweens one of its properties (depth, x, "
                   "height, size or glow) over and over, by driving the Playfield "
                   "Areas module. Pick the property in the dropdown; watch it on "
                   "the Playfield tab.",
    "default_page": "controller",
    "pages": [{"path": "controller", "label": "Controller"}],
}
bp = Blueprint("approaching_box", __name__)

# The prototype we depend on (its folder name / hub slug).
PLAYFIELD = "playfield-areas"

# ---- tweenable parameters --------------------------------------------------
# key -> how to animate it: a human label and the start/end values. The box is
# created from DEFAULT_FIELDS with the chosen key overridden to its `from`.
DEFAULT_FIELDS = {"name": "Approaching", "color": "#3b82f6",
                  "x": 0.0, "y": 0.0, "z": 0.0, "size": 1.2, "glow": 2.0}
PARAMS = {
    "z":    {"label": "depth (z) — toward camera", "from": -10.0, "to": 0.0},
    "x":    {"label": "x (left ↔ right)",          "from": -10.0, "to": 10.0},
    "y":    {"label": "height (y)",                "from": 0.0,   "to": 5.0},
    "size": {"label": "size (grow)",               "from": 0.4,   "to": 3.0},
    "glow": {"label": "glow (brighten)",           "from": 0.0,   "to": 4.0},
}

# ---- animation timing ------------------------------------------------------
APPROACH_SECS = 4.0  # time to tween from -> to
WAIT_SECS = 2.0      # hold at `to` before vanishing
GAP_SECS = 1.0       # pause after it disappears before the next one
FPS = 30             # update rate while tweening

# ---- persisted config ------------------------------------------------------
CONFIG_PATH = os.path.join(HERE, "config.json")
_config = {"param": "z"}


def _load_config():
    try:
        with open(CONFIG_PATH) as f:
            param = json.load(f).get("param")
        if param in PARAMS:
            _config["param"] = param
    except (OSError, ValueError, TypeError):
        pass


def _save_config():
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(_config, f, indent=2)
    except OSError:
        pass


_load_config()

# ---- runtime state ---------------------------------------------------------
_ctx = None
_stop = threading.Event()
_restart = threading.Event()   # set when the tween param changes -> restart the cycle now
_thread = None
_lock = threading.Lock()
_status = {"running": False, "phase": "idle", "box_id": None, "value": None}
_stale_cleaned = False

# Live push: the tween updates _status ~30x/s; bump on each so the controller's
# readout follows in real time. A short keep-alive interval also lets clients
# notice hub enable/disable changes (which don't go through _update).
_live = live.LiveState()


def _update(**kw):
    with _lock:
        _status.update(kw)
    _live.bump()


def _can_drive():
    if _ctx is None or not _ctx.is_enabled():
        return False, "disabled"
    if _ctx.get_prototype(PLAYFIELD) is None:
        return False, "not_installed"
    if not _ctx.is_prototype_enabled(PLAYFIELD):
        return False, "playfield_disabled"
    return True, None


def _playfield():
    return _ctx.get_prototype(PLAYFIELD) if _ctx else None


def _cleanup():
    """Remove our current box from the playfield if it still exists."""
    with _lock:
        bid = _status.get("box_id")
    pf = _playfield()
    if bid and pf is not None:
        try:
            pf.remove_area(bid)
        except Exception:
            pass
    _update(box_id=None, value=None)


def _clean_stale(pf):
    """Drop any leftover 'Approaching' boxes from a previous run (once)."""
    global _stale_cleaned
    if _stale_cleaned:
        return
    try:
        for a in pf.list_areas():
            if a.get("name") == "Approaching":
                pf.remove_area(a["id"])
    except Exception:
        pass
    _stale_cleaned = True


def _gated_wait(secs):
    remaining = secs
    while remaining > 0:
        if _stop.is_set() or _restart.is_set():
            return False
        ok, _err = _can_drive()
        if not ok:
            return False
        step = min(0.1, remaining)
        _stop.wait(step)
        remaining -= step
    return True


def _animate(pf, bid, key, v0, v1, secs):
    """Tween area field `key` from v0 to v1 over `secs`. False if interrupted."""
    steps = max(1, int(secs * FPS))
    for i in range(1, steps + 1):
        if _stop.is_set() or _restart.is_set():
            return False  # aborted (param changed / shutting down)
        ok, _err = _can_drive()
        if not ok:
            return False
        v = round(v0 + (v1 - v0) * (i / steps), 3)
        if pf.update_area(bid, **{key: v}) is None:
            return False  # box vanished underneath us
        _update(value=v)
        _stop.wait(secs / steps)
    return True


def _loop():
    while not _stop.is_set():
        ok, err = _can_drive()
        if not ok:
            _cleanup()
            _update(running=False, phase="idle")
            _stop.wait(0.3)
            continue

        pf = _playfield()
        _clean_stale(pf)

        _restart.clear()   # start a fresh cycle with the current param
        key = _config["param"] if _config["param"] in PARAMS else "z"
        p = PARAMS[key]
        fields = dict(DEFAULT_FIELDS)
        fields[key] = p["from"]
        box = pf.create_area(**fields)
        _update(running=True, box_id=box["id"], value=p["from"], phase="approaching")

        if not _animate(pf, box["id"], key, p["from"], p["to"], APPROACH_SECS):
            _cleanup()
            continue

        _update(phase="waiting")
        if not _gated_wait(WAIT_SECS):
            _cleanup()
            continue

        pf.remove_area(box["id"])
        _update(box_id=None, value=None, phase="gap")
        _gated_wait(GAP_SECS)


def hub_init(ctx):
    """Called by the hub once all prototypes are loaded. Starts the loop."""
    global _ctx, _thread
    _ctx = ctx
    if _thread is None:
        _thread = threading.Thread(target=_loop, name="approaching-box", daemon=True)
        _thread.start()


# ---- pages + API -----------------------------------------------------------
@bp.route("/")
def index():
    return send_from_directory(HERE, "controller.html")


@bp.route("/controller")
def controller():
    return send_from_directory(HERE, "controller.html")


def _status_dict():
    """Live status: dependency state, animation phase, and the chosen tween."""
    with _lock:
        st = dict(_status)
    ok, err = _can_drive()
    key = _config["param"] if _config["param"] in PARAMS else "z"
    p = PARAMS[key]
    st["enabled"] = bool(_ctx and _ctx.is_enabled())
    st["playfield_installed"] = bool(_ctx and _ctx.get_prototype(PLAYFIELD) is not None)
    st["playfield_enabled"] = bool(_ctx and _ctx.is_prototype_enabled(PLAYFIELD))
    st["error"] = None if ok else err
    st["playfield_slug"] = PLAYFIELD
    st["param"] = key
    st["from"] = p["from"]
    st["to"] = p["to"]
    st["params"] = [{"key": k, "label": v["label"]} for k, v in PARAMS.items()]
    return st


@bp.route("/api/status")
def status():
    return jsonify(_status_dict())


@bp.route("/api/events")
def events():
    """Push status on every tween step (via _live.bump), and re-check every 0.5s
    so hub enable/disable of this prototype or the playfield is reflected too."""
    return _live.stream(_status_dict, interval=0.5)


@bp.route("/api/param", methods=["POST"])
def set_param():
    """Choose which property the box tweens; persisted to config.json."""
    key = (request.get_json(silent=True) or {}).get("param")
    if key not in PARAMS:
        return jsonify({"ok": False, "error": "unknown param"}), 400
    _config["param"] = key
    _save_config()
    _restart.set()   # abort the current cycle so the new param takes effect now
    return jsonify({"ok": True, "param": key})
