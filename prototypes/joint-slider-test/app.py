"""
Joint-slider test web server for the Dobot MG400.

A tiny Flask app that exposes the MG400 driver over a REST API and serves a
single-page HTML control panel (index.html). This is the FIRST prototype: its
job is to verify the three communication layers (control / motion / feedback)
end-to-end with real hardware, using sliders to rotate J1, J2 and J3.

Run:
    pip install -r requirements.txt
    python app.py            # then open http://localhost:8000

Safety: keep the hardware E-stop within reach. The web E-STOP button cuts servo
power; recovery is Clear Error -> Enable.
"""

import argparse
import threading

from flask import Flask, jsonify, request, send_file

from dobot import DobotMG400, DobotError

app = Flask(__name__)

# ---- configuration --------------------------------------------------------
DEFAULT_IP = "192.168.1.6"  # MG400 factory-default IP
# Conservative joint ranges (degrees). The controller enforces the true limits
# and its working-envelope constraints; out-of-range targets are surfaced as
# errors rather than silently clamped away.
JOINT_LIMITS = {
    "j1": [-160.0, 160.0],
    "j2": [-25.0, 85.0],
    "j3": [-25.0, 105.0],
}
# Per-joint following speed at 100% on the speed slider (deg/s). The live
# follower slews toward the slider target capped at (ratio/100) * this value,
# which is what makes dragging smooth instead of point-to-point.
MAX_JOINT_VEL = 90.0

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


# ---- routes ---------------------------------------------------------------
@app.route("/")
def index():
    return send_file("index.html")


@app.route("/api/config")
def config():
    return jsonify({"joint_limits": JOINT_LIMITS, "default_ip": DEFAULT_IP})


@app.route("/api/status")
def status():
    robot = _current()
    if robot is None:
        return jsonify(DobotMG400._blank_state())
    return jsonify(robot.get_state())


@app.route("/api/connect", methods=["POST"])
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


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    global _robot
    with _robot_lock:
        if _robot is not None:
            _robot.close()
            _robot = None
    return _ok()


@app.route("/api/enable", methods=["POST"])
def enable():
    # Clear any latched error first, then enable, then start smooth streaming.
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
        robot.start_servo()  # begin ServoJ streaming, holding the current pose
    return jsonify({"ok": errid == 0, "errid": errid, "resp": resp})


@app.route("/api/disable", methods=["POST"])
def disable():
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    robot.stop_servo()
    return _command(lambda r: r.disable())


@app.route("/api/clear_error", methods=["POST"])
def clear_error():
    return _command(lambda r: r.clear_error())


@app.route("/api/error_info")
def error_info():
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    try:
        return _ok(error_ids=robot.get_error_id())
    except DobotError as e:
        return _fail(str(e))


@app.route("/api/speed", methods=["POST"])
def speed():
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    ratio = _clamp(int((request.json or {}).get("ratio", 50)), 1, 100)
    # The follower speed is what governs smooth dragging; also set the
    # controller's global SpeedFactor for any non-servo moves.
    robot.set_max_velocity(ratio / 100.0 * MAX_JOINT_VEL)
    return _command(lambda r: r.speed_factor(ratio))


@app.route("/api/stop", methods=["POST"])
def stop():
    """Smooth stop / hold — freeze the follower at its current setpoint."""
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    robot.hold()
    return _ok()


@app.route("/api/estop", methods=["POST"])
def estop():
    """Emergency stop — stop streaming and cut servo power immediately."""
    robot = _current()
    if robot is None or not robot.is_connected():
        return _fail("Not connected")
    robot.stop_servo()
    return _command(lambda r: r.emergency_stop())


@app.route("/api/move", methods=["POST"])
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


def main():
    parser = argparse.ArgumentParser(description="MG400 joint-slider test server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print(f"Serving on http://localhost:{args.port}  (default robot IP {DEFAULT_IP})")
    # threaded=True so status polling and command posts don't block each other.
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
