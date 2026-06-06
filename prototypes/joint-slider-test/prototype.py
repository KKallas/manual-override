"""
Joint-slider test prototype — a Flask blueprint mounted by the prototype hub.

Exposes the MG400 driver over a REST API and serves a single-page control panel
(index.html). This is the FIRST hardware prototype: its job is to verify the
three communication layers (control / motion / feedback) end-to-end, using
sliders to rotate J1, J2 and J3, plus vacuum/blow on the air pump.

This module is loaded by hub.py and registered under /p/joint-slider-test. It
has no app.run() of its own — it only runs inside the hub server. It needs the
robot reachable on the network to do anything; without it, the GUI still loads
and the API simply reports "Not connected".

Safety: keep the hardware E-stop within reach. The web E-STOP button cuts servo
power; recovery is Clear Error -> Enable.
"""

import os
import sys
import threading

from flask import Blueprint, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so the sibling dobot.py driver imports cleanly
from dobot import DobotMG400, DobotError  # noqa: E402

MANIFEST = {
    "name": "Joint Slider Test",
    "description": "Drive an MG400's base + first two joints over TCP, with "
                   "vacuum/blow on the air pump. Requires the robot on the "
                   "network; the GUI loads regardless.",
    "default_page": "",   # index.html lives at the prototype root
    "pages": [{"path": "", "label": "Controller"}],
}
bp = Blueprint("joint_slider_test", __name__)

# ---- configuration --------------------------------------------------------
DEFAULT_IP = "192.168.1.6"  # MG400 factory-default IP
JOINT_LIMITS = {
    "j1": [-160.0, 160.0],
    "j2": [-25.0, 85.0],
    "j3": [-25.0, 105.0],
}
MAX_JOINT_VEL = 90.0
SUCK_DO_INDEX = 2        # output that drives suction (vacuum / pull)
BLOW_DO_INDEX = 1        # output that drives blowing (push / release)

# ---- single shared robot instance -----------------------------------------
_robot = None
_robot_lock = threading.Lock()


def _current():
    return _robot


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _ok(**kw):
    return jsonify({"ok": True, **kw})


def _fail(error, **kw):
    return jsonify({"ok": False, "error": error, **kw})


def _command(fn):
    """Run a driver call that returns (errid, resp) and shape it into JSON."""
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    try:
        errid, resp = fn(robot)
        return jsonify({"ok": errid == 0, "errid": errid, "resp": resp})
    except DobotError as e:
        return _fail(str(e), errid=e.errid)
    except Exception as e:  # pragma: no cover - defensive
        return _fail(str(e))


# ---- pages ----------------------------------------------------------------
@bp.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@bp.route("/api/config")
def config():
    return jsonify({"joint_limits": JOINT_LIMITS, "default_ip": DEFAULT_IP})


def _pump_mode(do_bits):
    """Derive the current pump mode from the feedback digital-output bitmask."""
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


@bp.route("/api/error_info")
def error_info():
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    try:
        return _ok(error_ids=robot.get_error_id())
    except DobotError as e:
        return _fail(str(e))


@bp.route("/api/speed", methods=["POST"])
def speed():
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    ratio = _clamp(int((request.json or {}).get("ratio", 50)), 1, 100)
    robot.set_max_velocity(ratio / 100.0 * MAX_JOINT_VEL)
    return _command(lambda r: r.speed_factor(ratio))


@bp.route("/api/stop", methods=["POST"])
def stop():
    """Smooth stop / hold — freeze the follower at its current setpoint."""
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    robot.hold()
    return _ok()


@bp.route("/api/estop", methods=["POST"])
def estop():
    """Emergency stop — stop streaming and cut servo power immediately."""
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    robot.stop_servo()
    return _command(lambda r: r.emergency_stop())


@bp.route("/api/pump", methods=["POST"])
def pump():
    """Set the air pump mode: 'suck' (vacuum/pull), 'blow' (push), or 'off'."""
    mode = (request.json or {}).get("mode", "off")
    if mode not in ("suck", "blow", "off"):
        return _fail("mode must be 'suck', 'blow' or 'off'")
    return _command(lambda r: r.set_pump(mode, SUCK_DO_INDEX, BLOW_DO_INDEX))


@bp.route("/api/move", methods=["POST"])
def move():
    """Update the follower's target. Cheap (no socket round-trip); the servo
    thread streams toward it at the velocity cap, which smooths the motion."""
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    data = request.json or {}
    try:
        j1 = _clamp(float(data["j1"]), *JOINT_LIMITS["j1"])
        j2 = _clamp(float(data["j2"]), *JOINT_LIMITS["j2"])
        j3 = _clamp(float(data["j3"]), *JOINT_LIMITS["j3"])
    except (KeyError, ValueError, TypeError):
        return _fail("Expected numeric j1, j2, j3")
    robot.set_target(j1, j2, j3)
    return _ok()
