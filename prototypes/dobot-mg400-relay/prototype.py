"""
Dobot MG400 Relay — game-master arbitration server (a Flask blueprint).

This machine OWNS the single MG400 connection and acts as a relay between two
remote "sides" (purple / green). At most one side holds the floor at a time; the
holder streams move/pump commands and the relay applies a safety filter (joint
limits / reachable-workspace clamp mirrored from the joint and cartesian
prototypes) before forwarding them to the arm. A watchdog frees the floor if the
holder stops heartbeating.

Loaded by hub.py; registered under /p/dobot-mg400-relay. No app.run() of its own —
it only runs inside the hub server. It needs the robot reachable on the network to
move anything; without it the operator GUI + every endpoint still work and
move/pump fail cleanly with "Not connected".

Put the robot in API mode first — see ../../docs/operations/dobot-api-mode.md.
Safety: keep the hardware E-stop within reach; start with a low speed.
"""

import math
import os
import sys
import threading
import time

from flask import Blueprint, jsonify, request, send_from_directory

import live   # shared push helper (prototypes/live.py)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so the sibling driver imports cleanly
from relay_arm import DobotMG400, DobotError  # noqa: E402

MANIFEST = {
    "name": "Dobot MG400 Relay",
    "description": "Game-master relay: owns the MG400 and arbitrates control "
                   "between purple/green sides with a safety filter. Remote "
                   "controllers connect via its HTTP API.",
    "default_page": "",
    "pages": [{"path": "", "label": "Relay (operator)"}],
}
bp = Blueprint("dobot_mg400_relay", __name__)

# ---- configuration --------------------------------------------------------
DEFAULT_IP = "192.168.1.6"

SIDES = ("purple", "green")
MODES = ("joint", "cartesian")
LEASE_SECS = 2.0          # holder must heartbeat within this or it's dropped
WATCHDOG_HZ = 5.0         # how often the watchdog checks the lease

# Per-joint angle limits (degrees) — mirrored from joint-angles-test/prototype.py.
JOINT_LIMITS = {
    "j1": [-160.0, 160.0],   # base rotation
    "j2": [-25.0, 85.0],     # arm joint 1
    "j3": [-25.0, 105.0],    # arm joint 2
    "j4": [-160.0, 160.0],   # end rotation (R)
}

# Approximate MG400 workspace — mirrored EXACTLY from cartesian-xyz-test/prototype.py.
# The reachable area is an ANNULUS, so X/Y are clamped to a min/max radius too.
WORKSPACE = {
    "x": [-450.0, 450.0],
    "y": [-450.0, 450.0],
    "z": [-150.0, 230.0],
    "r": [-160.0, 160.0],
}
RADIUS_MIN = 150.0   # mm — inside this the arm can't reach (too folded)
RADIUS_MAX = 440.0   # mm — max horizontal reach

# Conservative following speed at 100% on the speed slider (per follower).
MAX_JOINT_VEL = 120.0   # deg/s
MAX_LIN_VEL = 200.0     # mm/s
MAX_ANG_VEL = 90.0      # deg/s
RAMP_SECS = 0.35        # follower ramp/brake time → accel = vel-cap / ramp-time

# Air pump box (two-line suck/blow model — see joint prototype).
SUCK_DO_INDEX = 2
BLOW_DO_INDEX = 1

_robot = None
_robot_lock = threading.Lock()   # guards (re)connecting the arm
# Sampled state (live feedback + lease seconds tick down), so the SSE stream
# re-snapshots on a short interval rather than on a bump. See prototypes/live.py.
_live = live.LiveState()


# ---- arbitration state -----------------------------------------------------
# Guarded by _arb_lock. NEVER do blocking arm socket I/O while holding this lock;
# read what's needed under it, release, then talk to the arm.
_arb_lock = threading.Lock()
_estop = False                 # latched; cleared only by /api/clear
_holder = None                 # "purple" | "green" | None
_holder_mode = "joint"         # the mode the holder acquired with
_token_counter = 0             # bumps on every acquire → opaque per-side tokens
_sides = {
    s: {"present": False, "last_seen": 0.0, "token": None}
    for s in SIDES
}


def _current():
    return _robot


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _clamp_joints(j1, j2, j3, j4):
    """Clamp a joint target to each joint's limit (mirror joint-angles-test)."""
    return (
        _clamp(j1, *JOINT_LIMITS["j1"]),
        _clamp(j2, *JOINT_LIMITS["j2"]),
        _clamp(j3, *JOINT_LIMITS["j3"]),
        _clamp(j4, *JOINT_LIMITS["j4"]),
    )


def _clamp_pose(x, y, z, r):
    """Clamp a target pose into the (approximate) reachable workspace: Z and R to
    their ranges, X/Y to the reachable annulus, then to the X/Y box. Mirrored
    EXACTLY from cartesian-xyz-test/prototype.py."""
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


def _apply_motion(robot, mode):
    """Push conservative velocity + acceleration caps to the follower for `mode`
    (accel = velocity-cap / ramp-time)."""
    secs = max(0.05, RAMP_SECS)
    if mode == "cartesian":
        robot.set_max_velocity_cartesian(MAX_LIN_VEL, MAX_ANG_VEL)
        robot.set_max_accel_cartesian(MAX_LIN_VEL / secs, MAX_ANG_VEL / secs)
    else:
        robot.set_max_velocity_joint(MAX_JOINT_VEL)
        robot.set_max_accel_joint(MAX_JOINT_VEL / secs)


def _ensure_follower(robot, mode):
    """Make sure the arm is running the follower for `mode` (switching followers if
    a different one is active). Keeps the connection + enabled state intact. No
    arbitration lock is held here — this does blocking arm I/O."""
    if robot is None or not robot.is_connected():
        return
    if not robot.get_state().get("enabled"):
        return
    if robot.control_mode() != mode:
        robot.start_servo(mode)   # start_servo stops any current follower first
        _apply_motion(robot, mode)


# ---- lease / arbitration helpers (call with _arb_lock held) ----------------
def _lease_secs_left_locked():
    if _holder is None:
        return 0.0
    left = LEASE_SECS - (time.time() - _sides[_holder]["last_seen"])
    return max(0.0, left)


def _touch_locked(side):
    """Refresh a side's presence + lease (call with _arb_lock held)."""
    _sides[side]["present"] = True
    _sides[side]["last_seen"] = time.time()


# ---- programmatic API (for other machines via the hub) ---------------------
def arm_state():
    """The relay's full state object (same shape as GET /api/state)."""
    return _state_dict()


def current_holder():
    """Which side holds the floor right now: "purple" | "green" | None."""
    with _arb_lock:
        return _holder


# ---- watchdog --------------------------------------------------------------
def _watchdog_loop():
    """~5 Hz: if the holder's lease expired, smooth-stop the arm and free the
    floor (and mark that side not present). Arm I/O happens outside the lock."""
    period = 1.0 / WATCHDOG_HZ
    while True:
        time.sleep(period)
        expired = None
        with _arb_lock:
            global _holder
            if _holder is not None and _lease_secs_left_locked() <= 0.0:
                expired = _holder
                _sides[_holder]["present"] = False
                _sides[_holder]["token"] = None
                _holder = None
        if expired is not None:
            robot = _current()
            if robot is not None and robot.is_connected():
                try:
                    robot.hold()      # smooth stop; keep servos enabled
                except Exception:     # pragma: no cover - defensive
                    pass
            _live.bump()


_watchdog_thread = threading.Thread(
    target=_watchdog_loop, name="relay-watchdog", daemon=True
)
_watchdog_thread.start()


# ---- response helpers ------------------------------------------------------
def _ok(**kw):
    return jsonify({"ok": True, **kw})


def _fail(error, **kw):
    return jsonify({"ok": False, "error": error, **kw})


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


def _arm_dict():
    """The arm sub-object of the state (works with no robot connected)."""
    robot = _current()
    raw = DobotMG400._blank_state() if robot is None else robot.get_state()
    return {
        "connected": raw["connected"],
        "enabled": raw["enabled"],
        "mode_name": raw["mode_name"],
        "joints": raw["joints"],
        "pose": raw["pose"],
        "servo_active": raw["servo_active"],
        "servo_error": raw["servo_error"],
        "pump_mode": _pump_mode(raw.get("digital_out", 0)),
        "control_mode": raw.get("control_mode"),
        "ip": None if robot is None else robot.ip,
    }


def _state_dict():
    """The full relay state (GET /api/state and the SSE stream)."""
    robot = _current()
    with _arb_lock:
        sides = {s: {"present": _sides[s]["present"],
                     "last_seen": round(_sides[s]["last_seen"], 3)} for s in SIDES}
        state = {
            "arm": _arm_dict(),
            "estop": _estop,
            "holder": _holder,
            "lease_secs": round(_lease_secs_left_locked(), 3),
            "sides": sides,
            "target": None if robot is None else robot.get_target(),
        }
    return state


# ---- pages ----------------------------------------------------------------
@bp.route("/")
def index():
    return send_from_directory(HERE, "controller.html")


@bp.route("/api/config")
def config():
    return jsonify({
        "joint_limits": JOINT_LIMITS,
        "workspace": WORKSPACE,
        "radius_min": RADIUS_MIN,
        "radius_max": RADIUS_MAX,
        "default_ip": DEFAULT_IP,
        "sides": list(SIDES),
        "modes": list(MODES),
        "lease_secs": LEASE_SECS,
    })


@bp.route("/api/state")
def state():
    return jsonify(_state_dict())


@bp.route("/api/events")
def events():
    """Push the relay state (arm feedback, holder, lease, sides) ~5x/s while it
    changes; the lease counts down so it changes every sample."""
    return _live.stream(_state_dict, interval=0.2)


# ---- operator endpoints (no token) ----------------------------------------
@bp.route("/api/connect", methods=["POST"])
def connect():
    global _robot
    ip = (request.json or {}).get("ip", DEFAULT_IP)
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
    _live.bump()
    return _ok(ip=ip)


@bp.route("/api/disconnect", methods=["POST"])
def disconnect():
    global _robot
    with _robot_lock:
        if _robot is not None:
            _robot.close()
            _robot = None
    _live.bump()
    return _ok()


@bp.route("/api/enable", methods=["POST"])
def enable():
    """Enable + start the follower in the holder's mode (or default "joint").
    Idempotent. Refused while estop is latched."""
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    with _arb_lock:
        if _estop:
            return _fail("E-STOP latched — Clear first")
        mode = _holder_mode if _holder is not None else "joint"
    try:
        robot.clear_error()
    except DobotError:
        pass
    try:
        errid, resp = robot.enable()
    except DobotError as e:
        return _fail(str(e), errid=e.errid)
    if errid == 0:
        robot.start_servo(mode)
        _apply_motion(robot, mode)
    _live.bump()
    return jsonify({"ok": errid == 0, "errid": errid, "resp": resp})


@bp.route("/api/disable", methods=["POST"])
def disable():
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    robot.stop_servo()
    try:
        errid, resp = robot.disable()
    except DobotError as e:
        return _fail(str(e), errid=e.errid)
    _live.bump()
    return jsonify({"ok": errid == 0, "errid": errid, "resp": resp})


@bp.route("/api/clear", methods=["POST"])
def clear():
    """Clear the robot error AND un-latch the estop so motion is allowed again."""
    global _estop
    with _arb_lock:
        _estop = False
    robot = _current()
    if robot is not None and robot.is_connected():
        try:
            robot.clear_error()
        except DobotError as e:
            _live.bump()
            return _fail(str(e), errid=e.errid)
    _live.bump()
    return _ok()


@bp.route("/api/estop", methods=["POST"])
def estop():
    """Latch the estop, stop the follower, and emergency-stop the arm. Callable by
    operator AND clients (no token)."""
    global _estop
    with _arb_lock:
        _estop = True
    robot = _current()
    if robot is not None and robot.is_connected():
        robot.stop_servo()
        try:
            robot.emergency_stop()
        except DobotError:
            pass
    _live.bump()
    return _ok()


@bp.route("/api/kick", methods=["POST"])
def kick():
    """Force-release a side if it holds the floor (smooth-stop the arm)."""
    global _holder
    side = (request.json or {}).get("side")
    if side not in SIDES:
        return _fail("side must be 'purple' or 'green'")
    released = False
    with _arb_lock:
        _sides[side]["present"] = False
        _sides[side]["token"] = None
        if _holder == side:
            _holder = None
            released = True
    if released:
        robot = _current()
        if robot is not None and robot.is_connected():
            try:
                robot.hold()
            except Exception:   # pragma: no cover - defensive
                pass
    _live.bump()
    return _ok()


# ---- client / side endpoints ----------------------------------------------
def _check_holder(data):
    """Validate (side, token) against the current holder. Returns (side, error)
    where error is None on success. Refreshes the side's lease on success.
    Acquires/releases _arb_lock internally; does no arm I/O."""
    side = data.get("side")
    token = data.get("token")
    if side not in SIDES:
        return None, "side must be 'purple' or 'green'"
    with _arb_lock:
        if _holder != side:
            return None, "you do not hold the floor"
        if token != _sides[side]["token"]:
            return None, "stale token"
        _touch_locked(side)
    return side, None


@bp.route("/api/acquire", methods=["POST"])
def acquire():
    """Grant the floor to `side` if it's free or already held by it. Records the
    mode and mints a fresh token (invalidating any old one for that side). 409 if
    the OTHER side currently holds it."""
    global _holder, _holder_mode, _token_counter
    data = request.json or {}
    side = data.get("side")
    mode = (data.get("mode") or "joint").lower()
    if side not in SIDES:
        return _fail("side must be 'purple' or 'green'"), 400
    if mode not in MODES:
        return _fail("mode must be 'joint' or 'cartesian'"), 400
    with _arb_lock:
        if _holder is not None and _holder != side:
            return jsonify({"ok": False, "error": "floor held by other side",
                            "holder": _holder}), 409
        _holder = side
        _holder_mode = mode
        _token_counter += 1
        token = f"{side}-{_token_counter}"
        _sides[side]["token"] = token
        _touch_locked(side)
    # If the arm is live, make sure the right follower is running (outside lock).
    robot = _current()
    if robot is not None and robot.is_connected():
        try:
            _ensure_follower(robot, mode)
        except Exception:   # pragma: no cover - defensive
            pass
    _live.bump()
    return _ok(token=token, lease_secs=LEASE_SECS)


@bp.route("/api/release", methods=["POST"])
def release():
    """Release the floor. Requires a valid (side, token)."""
    global _holder
    data = request.json or {}
    side = data.get("side")
    token = data.get("token")
    if side not in SIDES:
        return _fail("side must be 'purple' or 'green'")
    with _arb_lock:
        if _holder != side:
            return _fail("you do not hold the floor")
        if token != _sides[side]["token"]:
            return _fail("stale token")
        _holder = None
        _sides[side]["token"] = None
        _sides[side]["present"] = False
    robot = _current()
    if robot is not None and robot.is_connected():
        try:
            robot.hold()
        except Exception:   # pragma: no cover - defensive
            pass
    _live.bump()
    return _ok()


@bp.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    """Refresh the holder's lease; returns the full state object."""
    side, err = _check_holder(request.json or {})
    if err:
        return _fail(err)
    return _ok(state=_state_dict())


@bp.route("/api/move", methods=["POST"])
def move():
    """Set the follower target after safety-clamping. Requires the holder + a valid
    token, no estop, and an enabled arm."""
    data = request.json or {}
    side, err = _check_holder(data)
    if err:
        return _fail(err)
    with _arb_lock:
        if _estop:
            return _fail("E-STOP latched")
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    if not robot.get_state().get("enabled"):
        return _fail("Arm not enabled")
    mode = (data.get("mode") or "joint").lower()
    if mode not in MODES:
        return _fail("mode must be 'joint' or 'cartesian'")
    # Make sure the right follower is running for this command.
    try:
        _ensure_follower(robot, mode)
    except DobotError as e:
        return _fail(str(e), errid=e.errid)

    if mode == "joint":
        j = data.get("joints")
        if not (isinstance(j, (list, tuple)) and len(j) >= 3):
            return _fail("Expected joints [j1, j2, j3, j4]")
        try:
            j1, j2, j3 = float(j[0]), float(j[1]), float(j[2])
            j4 = float(j[3]) if len(j) > 3 else robot.get_state()["joints"][3]
        except (ValueError, TypeError, IndexError):
            return _fail("joints must be numeric")
        j1, j2, j3, j4 = _clamp_joints(j1, j2, j3, j4)
        robot.set_target_joints(j1, j2, j3, j4)
        clamped = {"j1": round(j1, 2), "j2": round(j2, 2),
                   "j3": round(j3, 2), "j4": round(j4, 2)}
    else:
        p = data.get("pose")
        if not (isinstance(p, (list, tuple)) and len(p) >= 3):
            return _fail("Expected pose [x, y, z, r]")
        try:
            x, y, z = float(p[0]), float(p[1]), float(p[2])
            r = float(p[3]) if len(p) > 3 else robot.get_state()["pose"][3]
        except (ValueError, TypeError, IndexError):
            return _fail("pose must be numeric")
        x, y, z, r = _clamp_pose(x, y, z, r)
        robot.set_target_pose(x, y, z, r)
        clamped = {"x": round(x, 2), "y": round(y, 2),
                   "z": round(z, 2), "r": round(r, 2)}
    _live.bump()
    return _ok(clamped=clamped)


@bp.route("/api/pump", methods=["POST"])
def pump():
    """Set the air pump (suck|blow|off). Requires the holder + a valid token."""
    data = request.json or {}
    side, err = _check_holder(data)
    if err:
        return _fail(err)
    with _arb_lock:
        if _estop:
            return _fail("E-STOP latched")
    mode = (data.get("mode") or "off").lower()
    if mode not in ("suck", "blow", "off"):
        return _fail("mode must be 'suck', 'blow' or 'off'")
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    try:
        errid, resp = robot.set_pump(mode, SUCK_DO_INDEX, BLOW_DO_INDEX)
    except DobotError as e:
        return _fail(str(e), errid=e.errid)
    _live.bump()
    return jsonify({"ok": errid == 0, "errid": errid, "resp": resp})


@bp.route("/api/hold", methods=["POST"])
def hold():
    """Smooth-stop the arm but keep the floor. Requires the holder + a valid token."""
    side, err = _check_holder(request.json or {})
    if err:
        return _fail(err)
    robot = _current()
    if robot is not None and robot.is_connected():
        try:
            robot.hold()
        except Exception:   # pragma: no cover - defensive
            pass
    _live.bump()
    return _ok()
