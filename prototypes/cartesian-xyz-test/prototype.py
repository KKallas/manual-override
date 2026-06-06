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

import math
import os
import sys
import threading

from flask import Blueprint, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so the sibling dobot.py driver imports cleanly
from dobot import DobotMG400, DobotError  # noqa: E402

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

# Air pump box (two-line suck/blow model — see joint prototype).
SUCK_DO_INDEX = 2
BLOW_DO_INDEX = 1

_robot = None
_robot_lock = threading.Lock()


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
    return jsonify(
        {
            "workspace": WORKSPACE,
            "radius_min": RADIUS_MIN,
            "radius_max": RADIUS_MAX,
            "default_ip": DEFAULT_IP,
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


@bp.route("/api/status")
def status():
    robot = _current()
    st = DobotMG400._blank_state() if robot is None else robot.get_state()
    st["pump_mode"] = _pump_mode(st.get("digital_out", 0))
    # The commanded target lets other open windows sync their sliders to it.
    st["target"] = None if robot is None else robot.get_target()
    return jsonify(st)


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
    return _ok(ip=ip)


@bp.route("/api/disconnect", methods=["POST"])
def disconnect():
    global _robot
    with _robot_lock:
        if _robot is not None:
            _robot.close()
            _robot = None
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
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    ratio = _clamp(int((request.json or {}).get("ratio", 30)), 1, 100)
    frac = ratio / 100.0
    robot.set_max_velocity(frac * MAX_LIN_VEL, frac * MAX_ANG_VEL)
    return _command(lambda r: r.speed_factor(ratio))


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
