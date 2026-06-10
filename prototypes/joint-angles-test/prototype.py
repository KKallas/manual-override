"""
Joint Angles test prototype — a Flask blueprint mounted by the hub.

Sliders drive the MG400's joint angles (J1, J2, J3, J4 in degrees) directly. The
server streams ServoJ setpoints toward the target with an acceleration-limited
(trapezoidal) velocity profile, so live dragging eases in and out smoothly without
overshoot.

This module is loaded by hub.py and registered under /p/joint-angles-test. It has
no app.run() of its own — it only runs inside the hub server. It needs the robot
reachable on the network to do anything; without it, the GUI still loads and the
API simply reports "Not connected".

Put the robot in API mode first — see ../../docs/operations/dobot-api-mode.md.
Safety: keep the hardware E-stop within reach; start with a low speed.
"""

import json
import os
import sys
import threading
import time

from flask import Blueprint, jsonify, request, send_from_directory

import live   # shared push helper (prototypes/live.py)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so the sibling driver imports cleanly
from dobot_joint import DobotMG400, DobotError  # noqa: E402  (unique name; modules share one namespace)
from relay_client import RelayClient, RelayError  # noqa: E402  (relay-link backend, stdlib only)

MANIFEST = {
    "name": "Joint Angles Test",
    "description": "Drive the MG400's joint angles (J1–J4, deg) via streamed "
                   "ServoJ with eased start/stop. Requires the robot on the "
                   "network; the GUI loads regardless.",
    "default_page": "",   # index.html lives at the prototype root
    "pages": [{"path": "", "label": "Controller"}],
}
bp = Blueprint("joint_angles_test", __name__)

# ---- configuration --------------------------------------------------------
DEFAULT_IP = "192.168.1.6"

# Per-joint angle limits (degrees). Conservative starting values — the controller
# enforces them and the robot rejects anything still unreachable (surfaced as a
# ServoJ error). Verify and tighten on your hardware.
JOINT_LIMITS = {
    "j1": [-160.0, 160.0],   # base rotation
    "j2": [-25.0, 85.0],     # arm joint 1
    "j3": [-25.0, 105.0],    # arm joint 2
    "j4": [-160.0, 160.0],   # end rotation (R)
}
JOINTS = ["j1", "j2", "j3", "j4"]

# Following speed at 100% on the speed slider.
MAX_VEL = 120.0  # deg/s
# Follower easing: time to ramp from rest to the speed cap (and to brake to a
# stop). Acceleration = speed-cap / ramp-time. Larger ramp = gentler start/stop
# and a longer braking distance → removes the overshoot a hard velocity step
# causes. Exposed live as the "Smoothness" control.
RAMP_SECS = 0.35

# Air pump box (two-line suck/blow model — see the Cartesian prototype).
SUCK_DO_INDEX = 2
BLOW_DO_INDEX = 1

_robot = None
_robot_lock = threading.Lock()
# How the active _robot is wired: "direct" (DobotMG400 over TCP) or "relay"
# (RelayClient forwarding to a remote game-master relay). Tracked so _status_dict
# can surface the link + side/holder/lease to the UI; control_mode for the relay
# is fixed for this controller ("joint").
_link = "direct"
_relay_side = None
CONTROL_MODE = "joint"
# Sampled state (live joint feedback from the arm), so the stream re-snapshots on
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
    robot.set_max_velocity(frac * MAX_VEL)
    robot.set_max_accel(frac * MAX_VEL / secs)


# Saved locations: a FIXED set of NUM_SLOTS numbered slots (1..N), so the recall
# API (/api/recall/<n>) is always a valid call even when a slot is empty. Each
# slot has an editable label + joint pose; persisted so they survive restarts.
NUM_SLOTS = 10
LOCATIONS_PATH = os.path.join(HERE, "locations.json")
_loc_lock = threading.Lock()
_slots = [{"name": "", "j1": 0.0, "j2": 0.0, "j3": 0.0, "j4": 0.0, "set": False}
          for _ in range(NUM_SLOTS)]


def _slot_public(i):
    """Slot i (0-based) as sent to clients; `slot` is the 1-based number."""
    s = _slots[i]
    return {"slot": i + 1, "name": s["name"], "set": s["set"],
            "j1": s["j1"], "j2": s["j2"], "j3": s["j3"], "j4": s["j4"]}


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
                "j1": float(s.get("j1", 0)), "j2": float(s.get("j2", 0)),
                "j3": float(s.get("j3", 0)), "j4": float(s.get("j4", 0)),
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


def _clamp_joints(j1, j2, j3, j4):
    """Clamp a joint target to each joint's limit."""
    return (
        _clamp(j1, *JOINT_LIMITS["j1"]),
        _clamp(j2, *JOINT_LIMITS["j2"]),
        _clamp(j3, *JOINT_LIMITS["j3"]),
        _clamp(j4, *JOINT_LIMITS["j4"]),
    )


# ---- programmatic API (for other prototypes via the hub) -------------------
def robot_ready():
    """True if the arm is connected and enabled (follower running)."""
    r = _robot
    if r is None or not r.is_connected():
        return False
    return bool(r.get_state().get("enabled"))


def current_joints():
    """Live [j1, j2, j3, j4] from the feedback stream, or None if not connected."""
    r = _robot
    if r is None or not r.is_connected():
        return None
    joints = r.get_state().get("joints")
    return list(joints) if joints else None


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
    return jsonify({"joint_limits": JOINT_LIMITS, "default_ip": DEFAULT_IP})


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
    """Push the live arm status (joints, mode, pump, target) ~5x/s while it changes."""
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
        side = data.get("side", "purple")
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
def route_pump():
    mode = (request.json or {}).get("mode", "off")
    if mode not in ("suck", "blow", "off"):
        return _fail("mode must be 'suck', 'blow' or 'off'")
    return _command(lambda r: r.set_pump(mode, SUCK_DO_INDEX, BLOW_DO_INDEX))


@bp.route("/api/move", methods=["POST"])
def move():
    """Update the target joint angles. Clamped to the joint limits; the follower
    streams ServoJ toward them with the eased velocity profile."""
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    data = request.json or {}
    try:
        j1 = float(data["j1"])
        j2 = float(data["j2"])
        j3 = float(data["j3"])
        j4 = float(data.get("j4", robot.get_state()["joints"][3]))
    except (KeyError, ValueError, TypeError, IndexError):
        return _fail("Expected numeric j1, j2, j3 (j4 optional)")
    j1, j2, j3, j4 = _clamp_joints(j1, j2, j3, j4)
    robot.set_target(j1, j2, j3, j4)
    return _ok(clamped={"j1": round(j1, 2), "j2": round(j2, 2),
                        "j3": round(j3, 2), "j4": round(j4, 2)})


# ---- saved locations: NUM_SLOTS fixed slots (edit / set / recall) ----------
@bp.route("/api/locations", methods=["GET"])
def list_locations():
    with _loc_lock:
        return jsonify({"slots": [_slot_public(i) for i in range(NUM_SLOTS)]})


@bp.route("/api/locations/<int:n>", methods=["POST", "PATCH"])
def set_location(n):
    """Edit slot n: set its label (`name`) and/or its joint `pose`. Passing a pose
    marks the slot filled and clamps it to the joint limits; the controller sends
    its current slider angles for the 'Set' button."""
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
                j1, j2, j3, j4 = (float(pose["j1"]), float(pose["j2"]),
                                  float(pose["j3"]), float(pose.get("j4", 0)))
            except (KeyError, TypeError, ValueError):
                return _fail("bad pose")
            j1, j2, j3, j4 = _clamp_joints(j1, j2, j3, j4)
            s["j1"], s["j2"], s["j3"], s["j4"] = (round(j1, 2), round(j2, 2),
                                                  round(j3, 2), round(j4, 2))
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
        _slots[n - 1] = {"name": "", "j1": 0.0, "j2": 0.0, "j3": 0.0, "j4": 0.0, "set": False}
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
        j1, j2, j3, j4 = _clamp_joints(s["j1"], s["j2"], s["j3"], s["j4"])
        out = _slot_public(n - 1)
    robot.set_target(j1, j2, j3, j4)
    return _ok(slot=out)
