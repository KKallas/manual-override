"""
Cartesian (TCP / XYZ) test prototype — a Flask blueprint mounted by the hub.

Sliders drive the tool pose in workspace coordinates (X, Y, Z in mm, R in deg)
rather than joint angles. The server streams ServoP setpoints toward the target
with velocity limiting, so live dragging is smooth.

This module is loaded by hub.py and registered under /p/cartesian-xyz-test. It
has no app.run() of its own — it only runs inside the hub server. It needs the
robot reachable on the network to do anything; without it, the GUI still loads
and the API simply reports "Not connected".

Put the robot in API mode first — see ../../docs/operations/dobot-api-mode.md.
Safety: keep the hardware E-stop within reach; start with a low speed.
"""

import json
import math
import os
import sys
import threading
import time

from flask import Blueprint, jsonify, request, send_from_directory

import live   # shared push helper (prototypes/live.py)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so the sibling driver imports cleanly
from dobot_cartesian import DobotMG400, DobotError  # noqa: E402  (unique name; modules share one namespace)
from relay_client import RelayClient, RelayError  # noqa: E402  (relay-link backend, stdlib only)

MANIFEST = {
    "name": "Cartesian XYZ Test",
    "description": "Drive the MG400 tool pose in workspace coordinates (X/Y/Z mm, "
                   "R deg) via streamed ServoP. Requires the robot on the network; "
                   "the GUI loads regardless.",
    "default_page": "",   # index.html lives at the prototype root
    "pages": [{"path": "", "label": "Controller"}],
}
bp = Blueprint("cartesian_xyz_test", __name__)

# ---- configuration --------------------------------------------------------
DEFAULT_IP = "192.168.1.6"

# Approximate MG400 workspace. The arm's reachable area is an ANNULUS (a ring),
# not a box, so X/Y are additionally clamped to a min/max radius from the base
# axis. These are conservative starting values — the controller enforces the true
# workspace and rejects anything unreachable (surfaced as a ServoP error). Verify
# and tighten on your hardware.
WORKSPACE = {
    "x": [-450.0, 450.0],
    "y": [-450.0, 450.0],
    "z": [-150.0, 230.0],
    "r": [-160.0, 160.0],
}
RADIUS_MIN = 150.0   # mm — inside this the arm can't reach (too folded)
RADIUS_MAX = 440.0   # mm — max horizontal reach

# Following speed at 100% on the speed slider.
MAX_LIN_VEL = 200.0  # mm/s
MAX_ANG_VEL = 90.0   # deg/s
# Follower easing: time to ramp from rest to the speed cap (and to brake to a
# stop). Acceleration = speed-cap / ramp-time. Larger ramp = gentler start/stop
# and a longer braking distance → removes the overshoot a hard velocity step
# causes. Exposed live as the "Smoothness" control.
RAMP_SECS = 0.35

# Air pump box (two-line suck/blow model — see joint prototype).
SUCK_DO_INDEX = 2
BLOW_DO_INDEX = 1

_robot = None
_robot_lock = threading.Lock()
# How the active _robot is wired: "direct" (DobotMG400 over TCP) or "relay"
# (RelayClient forwarding to a remote game-master relay). Tracked so _status_dict
# can surface the link + side/holder/lease to the UI; control_mode for the relay
# is fixed for this controller ("cartesian").
_link = "direct"
_relay_side = None
CONTROL_MODE = "cartesian"
# The hub hands us a HubContext via the optional hub_init hook below; we keep it
# so we can reach the OPTIONAL game-link machine for relay defaults (host + team).
# None until hub_init runs, and stays None in a plain hub without that hook.
_hub_ctx = None
# Sampled state (live pose/feedback from the arm), so the stream re-snapshots on
# a short interval rather than on a bump. See prototypes/live.py.
_live = live.LiveState()

# Motion shaping: last speed % and ramp time, pushed to the follower together as
# velocity + acceleration caps.
_speed_ratio = 30
_ramp_secs = RAMP_SECS


def _apply_motion(robot):
    """Push the current speed + smoothness to the follower as velocity and
    acceleration caps (acceleration = speed-cap / ramp-time)."""
    frac = _speed_ratio / 100.0
    secs = max(0.05, _ramp_secs)
    robot.set_max_velocity(frac * MAX_LIN_VEL, frac * MAX_ANG_VEL)
    robot.set_max_accel(frac * MAX_LIN_VEL / secs, frac * MAX_ANG_VEL / secs)

# Saved locations: a FIXED set of NUM_SLOTS numbered slots (1..N), so the recall
# API (/api/recall/<n>) is always a valid call even when a slot is empty. Each
# slot has an editable label + pose; persisted so they survive restarts.
NUM_SLOTS = 10
LOCATIONS_PATH = os.path.join(HERE, "locations.json")
_loc_lock = threading.Lock()
_slots = [{"name": "", "x": 0.0, "y": 0.0, "z": 0.0, "r": 0.0, "set": False}
          for _ in range(NUM_SLOTS)]


def _slot_public(i):
    """Slot i (0-based) as sent to clients; `slot` is the 1-based number."""
    s = _slots[i]
    return {"slot": i + 1, "name": s["name"], "set": s["set"],
            "x": s["x"], "y": s["y"], "z": s["z"], "r": s["r"]}


def _load_locations():
    try:
        with open(LOCATIONS_PATH) as f:
            saved = json.load(f).get("slots", [])
    except (OSError, ValueError, TypeError):
        return
    for i in range(min(NUM_SLOTS, len(saved))):
        s = saved[i]
        if not isinstance(s, dict):
            continue
        try:
            _slots[i] = {
                "name": str(s.get("name", ""))[:40],
                "x": float(s.get("x", 0)), "y": float(s.get("y", 0)),
                "z": float(s.get("z", 0)), "r": float(s.get("r", 0)),
                "set": bool(s.get("set", False)),
            }
        except (TypeError, ValueError):
            pass


def _save_locations():
    try:
        with open(LOCATIONS_PATH, "w") as f:
            json.dump({"slots": _slots}, f, indent=2)
    except OSError:
        pass


_load_locations()


def _current():
    return _robot


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _clamp_pose(x, y, z, r):
    """Clamp a target pose into the (approximate) reachable workspace: Z and R to
    their ranges, and X/Y to the reachable annulus, then to the X/Y box."""
    z = _clamp(z, *WORKSPACE["z"])
    r = _clamp(r, *WORKSPACE["r"])
    radius = math.hypot(x, y)
    if radius == 0.0:
        x, y = RADIUS_MIN, 0.0  # base axis is unreachable; nudge outward
    elif radius > RADIUS_MAX:
        s = RADIUS_MAX / radius
        x, y = x * s, y * s
    elif radius < RADIUS_MIN:
        s = RADIUS_MIN / radius
        x, y = x * s, y * s
    x = _clamp(x, *WORKSPACE["x"])
    y = _clamp(y, *WORKSPACE["y"])
    return x, y, z, r


# ---- hub integration (optional game-link pre-fill) -------------------------
def hub_init(ctx):
    """Optional hook the hub calls once after all machines load, handing us a
    HubContext. We only keep it so relay defaults can fall back to the player-side
    game-link machine. Absent in deployments that don't use the hook."""
    global _hub_ctx
    _hub_ctx = ctx


def _link_defaults():
    """The relay host + side to pre-fill from the OPTIONAL game-link machine, as
    {"host", "side"} (each None if game-link isn't installed/enabled). Never
    raises — relay/direct mode work fine without game-link."""
    out = {"host": None, "side": None}
    ctx = _hub_ctx
    if ctx is None:
        return out
    try:
        gl = ctx.get_prototype("game-link")
        if gl is None:
            return out
        sel = gl.link()
        out["host"] = sel.get("host")
        out["side"] = sel.get("team")   # game-link calls the side a "team"
    except Exception:   # pragma: no cover - defensive; game-link is optional
        pass
    return out


# ---- programmatic API (for other prototypes via the hub) -------------------
# Lets e.g. the calibration prototype drive this same arm in workspace XYZ. The
# arm must already be Connected + Enabled from this prototype's tab (that starts
# the ServoP follower these calls stream to).
def robot_ready():
    """True if the arm is connected and enabled (follower running)."""
    r = _robot
    if r is None or not r.is_connected():
        return False
    return bool(r.get_state().get("enabled"))


def current_pose():
    """Live [x, y, z, r] from the feedback stream, or None if not connected."""
    r = _robot
    if r is None or not r.is_connected():
        return None
    pose = r.get_state().get("pose")
    return list(pose) if pose else None


def move_to(x, y, z, r=None, wait=True, timeout=12.0, tol=2.0):
    """Move the tool to a workspace pose via the ServoP follower, clamped into the
    reachable workspace. If `wait`, poll the live pose until within `tol` mm on
    XYZ or `timeout`. Returns (ok, reason)."""
    robot = _robot
    if robot is None or not robot.is_connected():
        return False, "robot not connected"
    if r is None:
        cur = current_pose()
        r = cur[3] if cur else 0.0
    x, y, z, r = _clamp_pose(float(x), float(y), float(z), float(r))
    robot.set_target_pose(x, y, z, r)
    if not wait:
        return True, None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cur = current_pose()
        if cur and abs(cur[0] - x) <= tol and abs(cur[1] - y) <= tol and abs(cur[2] - z) <= tol:
            return True, None
        time.sleep(0.03)
    return False, "move timed out"


def pump(mode):
    """Set the air pump: 'suck' | 'blow' | 'off'. Returns (ok, reason)."""
    robot = _robot
    if robot is None or not robot.is_connected():
        return False, "robot not connected"
    if mode not in ("suck", "blow", "off"):
        return False, "bad pump mode"
    try:
        errid, _ = robot.set_pump(mode, SUCK_DO_INDEX, BLOW_DO_INDEX)
        return errid == 0, (None if errid == 0 else f"pump errid {errid}")
    except Exception as e:  # pragma: no cover - defensive
        return False, str(e)


def _ok(**kw):
    return jsonify({"ok": True, **kw})


def _fail(error, **kw):
    return jsonify({"ok": False, "error": error, **kw})


def _command(fn):
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    try:
        errid, resp = fn(robot)
        return jsonify({"ok": errid == 0, "errid": errid, "resp": resp})
    except DobotError as e:
        return _fail(str(e), errid=e.errid)
    except Exception as e:  # pragma: no cover
        return _fail(str(e))


# ---- pages ----------------------------------------------------------------
@bp.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@bp.route("/api/config")
def config():
    # link_defaults pre-fill the relay Host + Side from the player-side game-link
    # machine when it's installed (both null otherwise — the UI keeps its defaults).
    return jsonify(
        {
            "workspace": WORKSPACE,
            "radius_min": RADIUS_MIN,
            "radius_max": RADIUS_MAX,
            "default_ip": DEFAULT_IP,
            "link_defaults": _link_defaults(),
        }
    )


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


def _status_dict():
    robot = _current()
    st = DobotMG400._blank_state() if robot is None else robot.get_state()
    st["pump_mode"] = _pump_mode(st.get("digital_out", 0))
    # The commanded target lets other open windows sync their sliders to it.
    st["target"] = None if robot is None else robot.get_target()
    st["ramp_secs"] = _ramp_secs
    st["speed_ratio"] = _speed_ratio
    # Link mode lets the UI show whether we're talking to the arm directly or via
    # the relay; in relay mode also surface side / holder / lease / estop.
    st["link"] = _link
    if _link == "relay":
        st["side"] = _relay_side
        st["holder"] = st.get("holder")
        st["lease_secs"] = st.get("lease_secs")
        st["estop"] = st.get("estop", False)
    with _loc_lock:
        st["slots"] = [_slot_public(i) for i in range(NUM_SLOTS)]
    return st


@bp.route("/api/status")
def status():
    return jsonify(_status_dict())


@bp.route("/api/events")
def events():
    """Push the live arm status (pose, mode, pump, target) ~5x/s while it changes."""
    return _live.stream(_status_dict, interval=0.2)


@bp.route("/api/connect", methods=["POST"])
def connect():
    """Open a control link. Two modes (default "direct" for backward-compat):
      {"mode":"direct","ip":"..."}                         — TCP to the MG400.
      {"mode":"relay","host":"http://HOST:8000","side":..} — forward to the relay.
    Either way the result is stored behind _robot so every other route is
    unchanged (it just calls the same methods on whichever backend is active)."""
    global _robot, _link, _relay_side
    data = request.json or {}
    mode = data.get("mode", "direct")

    if mode == "relay":
        host = data.get("host", "")
        side = data.get("side", "")
        # If host/side weren't supplied, fall back to the player's game-link choice
        # (optional machine); then to the legacy defaults. Keeps direct mode and an
        # explicit relay request unchanged.
        defaults = _link_defaults()
        if not host:
            host = defaults["host"] or ""
        if not side:
            side = defaults["side"] or "purple"
        if side not in ("purple", "green"):
            return _fail("side must be 'purple' or 'green'")
        with _robot_lock:
            if _robot is not None:
                _robot.close()
                _robot = None
            robot = RelayClient(host, side, control_mode=CONTROL_MODE)
            try:
                robot.connect()
            except RelayError as e:
                if e.status == 409:
                    held = e.holder or "the other side"
                    return _fail(f"Relay is held by {held} — release it first.",
                                 holder=e.holder)
                return _fail(f"Could not reach relay at {host}: {e}")
            _robot = robot
            _link = "relay"
            _relay_side = side
        return _ok(link="relay", host=host, side=side)

    # direct (default)
    ip = data.get("ip", DEFAULT_IP)
    with _robot_lock:
        if _robot is not None:
            _robot.close()
            _robot = None
        robot = DobotMG400(ip)
        try:
            robot.connect()
        except DobotError as e:
            return _fail(f"Could not connect to {ip}: {e}")
        _robot = robot
        _link = "direct"
        _relay_side = None
    return _ok(link="direct", ip=ip)


@bp.route("/api/disconnect", methods=["POST"])
def disconnect():
    global _robot, _link, _relay_side
    with _robot_lock:
        if _robot is not None:
            _robot.close()
            _robot = None
        _link = "direct"
        _relay_side = None
    return _ok()


@bp.route("/api/enable", methods=["POST"])
def enable():
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    try:
        robot.clear_error()
    except DobotError:
        pass
    try:
        errid, resp = robot.enable()
    except DobotError as e:
        return _fail(str(e), errid=e.errid)
    if errid == 0:
        robot.start_servo()
        _apply_motion(robot)   # push current speed + smoothness to the follower
    return jsonify({"ok": errid == 0, "errid": errid, "resp": resp})


@bp.route("/api/disable", methods=["POST"])
def disable():
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    robot.stop_servo()
    return _command(lambda r: r.disable())


@bp.route("/api/clear_error", methods=["POST"])
def clear_error():
    return _command(lambda r: r.clear_error())


@bp.route("/api/speed", methods=["POST"])
def speed():
    global _speed_ratio
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    ratio = _clamp(int((request.json or {}).get("ratio", 30)), 1, 100)
    _speed_ratio = ratio
    _apply_motion(robot)
    return _command(lambda r: r.speed_factor(ratio))


@bp.route("/api/smoothness", methods=["POST"])
def smoothness():
    """Set the follower's ramp/brake time in seconds (the 'Smoothness' knob):
    larger = gentler start/stop with no overshoot; smaller = snappier."""
    global _ramp_secs
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    try:
        secs = float((request.json or {}).get("secs", RAMP_SECS))
    except (TypeError, ValueError):
        return _fail("secs must be a number")
    _ramp_secs = max(0.05, min(1.5, secs))
    _apply_motion(robot)
    return _ok(ramp_secs=_ramp_secs)


@bp.route("/api/stop", methods=["POST"])
def stop():
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    robot.hold()
    return _ok()


@bp.route("/api/estop", methods=["POST"])
def estop():
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    robot.stop_servo()
    return _command(lambda r: r.emergency_stop())


@bp.route("/api/pump", methods=["POST"])
def pump():
    mode = (request.json or {}).get("mode", "off")
    if mode not in ("suck", "blow", "off"):
        return _fail("mode must be 'suck', 'blow' or 'off'")
    return _command(lambda r: r.set_pump(mode, SUCK_DO_INDEX, BLOW_DO_INDEX))


@bp.route("/api/move", methods=["POST"])
def move():
    """Update the Cartesian target pose. Clamped to the reachable workspace; the
    follower streams ServoP toward it at the velocity cap."""
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    data = request.json or {}
    try:
        x = float(data["x"])
        y = float(data["y"])
        z = float(data["z"])
        r = float(data.get("r", robot.get_state()["pose"][3]))
    except (KeyError, ValueError, TypeError):
        return _fail("Expected numeric x, y, z (r optional)")
    x, y, z, r = _clamp_pose(x, y, z, r)
    robot.set_target_pose(x, y, z, r)
    return _ok(clamped={"x": round(x, 2), "y": round(y, 2),
                        "z": round(z, 2), "r": round(r, 2)})


# ---- saved locations: NUM_SLOTS fixed slots (edit / set / recall) ----------
@bp.route("/api/locations", methods=["GET"])
def list_locations():
    with _loc_lock:
        return jsonify({"slots": [_slot_public(i) for i in range(NUM_SLOTS)]})


@bp.route("/api/locations/<int:n>", methods=["POST", "PATCH"])
def set_location(n):
    """Edit slot n: set its label (`name`) and/or its pose (`pose`). Passing a
    pose marks the slot filled and clamps it to the reachable workspace; the
    controller sends its current slider pose for the 'Set' button."""
    if not (1 <= n <= NUM_SLOTS):
        return _fail("slot out of range")
    data = request.json or {}
    with _loc_lock:
        s = _slots[n - 1]
        if "name" in data:
            s["name"] = str(data["name"])[:40]
        pose = data.get("pose")
        if isinstance(pose, dict):
            try:
                x, y, z, r = float(pose["x"]), float(pose["y"]), float(pose["z"]), float(pose.get("r", 0))
            except (KeyError, TypeError, ValueError):
                return _fail("bad pose")
            x, y, z, r = _clamp_pose(x, y, z, r)
            s["x"], s["y"], s["z"], s["r"] = round(x, 2), round(y, 2), round(z, 2), round(r, 2)
            s["set"] = True
        _save_locations()
        out = _slot_public(n - 1)
    _live.bump()   # push the change to every open window now
    return _ok(slot=out)


@bp.route("/api/locations/<int:n>/clear", methods=["POST"])
def clear_location(n):
    """Empty slot n (label + pose) but keep the slot itself in place."""
    if not (1 <= n <= NUM_SLOTS):
        return _fail("slot out of range")
    with _loc_lock:
        _slots[n - 1] = {"name": "", "x": 0.0, "y": 0.0, "z": 0.0, "r": 0.0, "set": False}
        _save_locations()
    _live.bump()
    return _ok()


@bp.route("/api/recall/<int:n>", methods=["POST"])
def recall_location(n):
    """Send the arm to slot n (sets the follower target). ok:false if the slot is
    empty or the robot isn't connected."""
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    if not (1 <= n <= NUM_SLOTS):
        return _fail("slot out of range")
    with _loc_lock:
        s = _slots[n - 1]
        if not s["set"]:
            return _fail(f"slot {n} is empty")
        x, y, z, r = _clamp_pose(s["x"], s["y"], s["z"], s["r"])
        out = _slot_public(n - 1)
    robot.set_target_pose(x, y, z, r)
    return _ok(slot=out)
