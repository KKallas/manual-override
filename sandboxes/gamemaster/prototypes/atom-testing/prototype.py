"""Automated Atom lift/drop data collection for the gamemaster."""

import csv
import json
import math
import os
import re
import struct
import threading
import time

from flask import Blueprint, jsonify, request, send_from_directory

try:
    import requests
except Exception:
    requests = None

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURE_RECORD = struct.Struct("<I7fB")
CAPTURE_SAMPLES = 1600
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
SIDES = ("purple", "green")

MANIFEST = {
    "name": "Atom Testing",
    "description": "Run repeatable robot lift/drop sequences and collect full-rate Atom IMU datasets.",
    "default_page": "",
    "pages": [{"path": "", "label": "Atom Testing"}],
}
bp = Blueprint("atom_testing", __name__)

_hub_ctx = None
_lock = threading.Lock()
_stop = threading.Event()
_worker = None
_state = {
    "running": False, "phase": "idle", "message": "Ready",
    "run": 0, "runs": 0, "saved": [], "error": None,
    "started_at": None, "finished_at": None,
}


def hub_init(ctx):
    global _hub_ctx
    _hub_ctx = ctx


def _module(slug):
    return _hub_ctx.get_prototype(slug) if _hub_ctx is not None else None


def _public_state():
    with _lock:
        return json.loads(json.dumps(_state))


def _state_payload(since=0):
    """Keep high-rate samples out of every status poll; return each capture once."""
    with _lock:
        saved = _state["saved"]
        summary = {
            key: json.loads(json.dumps(value))
            for key, value in _state.items() if key != "saved"
        }
        summary["saved_count"] = len(saved)
        summary["recordings"] = json.loads(json.dumps(saved[max(0, since):]))
        return summary


def _set_state(**changes):
    with _lock:
        _state.update(changes)


def _safe_part(value, fallback):
    return SAFE_NAME.sub("-", str(value or "")).strip("-.") or fallback


def _units():
    atom = _module("atom-manager")
    if atom is None:
        return []
    lock = getattr(atom, "_units_lock", None)
    source = getattr(atom, "_units", [])
    if lock is None:
        return []
    with lock:
        return [{
            "id": i, "name": u.get("name") or f"unit-{i + 1}",
            "ip": u.get("ip") or "", "online": bool(u.get("online")),
            "battery": u.get("bat"),
            "accel_available": bool((u.get("accel") or {}).get("available")),
        } for i, u in enumerate(source)]


def _unit(unit_id):
    return next((u for u in _units() if u["id"] == unit_id), None)


def _capture(unit_id):
    """Return the same full telemetry captured by Atom Manager."""
    atom = _module("atom-manager")
    unit = _unit(unit_id)
    if atom is None or unit is None:
        raise RuntimeError("selected tag is not configured")
    if not unit["online"]:
        raise RuntimeError("selected tag is offline")
    if requests is None:
        raise RuntimeError("requests is not installed")
    io_locks = getattr(atom, "_tag_io_locks", [])
    if not 0 <= unit_id < len(io_locks):
        raise RuntimeError("selected tag is unavailable")
    with io_locks[unit_id]:
        response = requests.get(
            f"http://{unit['ip']}/accelerometer/capture", timeout=5
        )
    if not response.ok:
        raise RuntimeError(f"tag returned HTTP {response.status_code}")
    raw = response.content
    if len(raw) % CAPTURE_RECORD.size:
        raise RuntimeError("tag returned a malformed capture")
    if len(raw) // CAPTURE_RECORD.size != CAPTURE_SAMPLES:
        raise RuntimeError(
            f"tag returned {len(raw) // CAPTURE_RECORD.size}/{CAPTURE_SAMPLES} samples"
        )
    records = []
    for values in CAPTURE_RECORD.iter_unpack(raw):
        timestamp_us, magnitude, ax, ay, az, gx, gy, gz, detector_hit = values
        records.append({
            "timestamp_us": timestamp_us,
            "accel_magnitude": round(magnitude, 6),
            "accel_x": round(ax, 6), "accel_y": round(ay, 6),
            "accel_z": round(az, 6), "gyro_x": round(gx, 6),
            "gyro_y": round(gy, 6), "gyro_z": round(gz, 6),
            "manual_drop_label": 0,
            "detector_hit": int(bool(detector_hit)),
        })
    return unit, records


def _save_records(folder, project, prefix, run_number, unit, records):
    project_dir = os.path.join(os.path.abspath(os.path.expanduser(folder)), project)
    os.makedirs(project_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = _safe_part(unit["name"], f"unit-{unit['id'] + 1}")
    filename = f"{prefix}-{run_number:04d}-{stamp}-{tag}.csv"
    path = os.path.join(project_dir, filename)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    return path


def _calibration_options():
    auto = _module("auto-pickup-game")
    if auto is None:
        return {"legacy": {}, "cal2": {}}
    legacy, cal2 = {}, {}
    for side in SIDES:
        arm = getattr(auto, "_calibration", {}).get("arms", {}).get(side, {})
        legacy[side] = [{
            "key": key, "label": value.get("label", key.replace("_", " ").title())
        } for key, value in (arm.get("points") or {}).items()
            if (value.get("pose") or {}).get("set")]
        arm2 = getattr(auto, "_calibration2", {}).get("arms", {}).get(side, {})
        cal2[side] = [{
            "key": key, "label": value.get("label", key.upper())
        } for key, value in (arm2.get("points") or {}).items()
            if (value.get("pose") or {}).get("set")]
    return {"legacy": legacy, "cal2": cal2}


def _pose(config, key, level):
    auto = _module("auto-pickup-game")
    side, kind = config["side"], config["calibration"]
    if kind == "legacy":
        arm = auto._calibration["arms"][side]
        point = (arm.get("points") or {}).get(key) or {}
        base = point.get("pose") or {}
        height = arm.get(level) or {}
        z = height.get("z") if height.get("set") else base.get("z")
    else:
        arm = auto._calibration2["arms"][side]
        base = ((arm.get("points") or {}).get(key) or {}).get("pose") or {}
        z = auto._calibration2.get("shared_z", {}).get(level)
        if z is None:
            z = base.get("z")
    if not base.get("set") or any(base.get(k) is None for k in ("x", "y")) or z is None:
        raise RuntimeError(f"{kind} calibration point '{key}' is incomplete")
    return [float(base["x"]), float(base["y"]), float(z), float(base.get("r") or 0)]


def _robot(config):
    auto = _module("auto-pickup-game")
    robot = auto._direct_robot(config["side"]) if auto else None
    if robot is None or not robot.is_connected():
        raise RuntimeError(f"{config['side']} arm is not connected in Auto Pick and Place")
    if not robot.get_state().get("enabled"):
        raise RuntimeError(f"{config['side']} arm is not enabled")
    return robot


def _check_stop(robot=None):
    if _stop.is_set():
        if robot is not None:
            robot.hold()
        raise InterruptedError("Stopped by operator")


def _move(robot, pose, timeout=20.0):
    _check_stop(robot)
    robot.set_target_pose(*pose)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _check_stop(robot)
        current = robot.get_state().get("pose") or []
        if len(current) >= 4 and math.dist(
            [float(v) for v in current[:3]], pose[:3]
        ) <= 3.0 and abs(float(current[3]) - pose[3]) <= 3.0:
            return
        time.sleep(0.05)
    raise RuntimeError("arm movement timed out")


def _pump(robot, mode):
    auto = _module("auto-pickup-game")
    errid, _ = robot.set_pump(mode, auto.SUCK_DO_INDEX, auto.BLOW_DO_INDEX)
    if errid != 0:
        raise RuntimeError(f"pump command failed ({errid})")


def _capture_with_action(config, prefix, run_number, action):
    result, failure = {}, {}

    def collect():
        try:
            result["unit"], result["records"] = _capture(config["unit"])
        except Exception as exc:
            failure["error"] = exc

    capture_thread = threading.Thread(target=collect, daemon=True)
    capture_thread.start()
    time.sleep(0.10)
    _check_stop()
    action()
    capture_thread.join(timeout=6)
    if capture_thread.is_alive():
        raise RuntimeError("tag capture did not finish")
    if failure:
        raise failure["error"]
    path = _save_records(
        config["folder"], config["project"], prefix, run_number,
        result["unit"], result["records"],
    )
    entry = {
        "kind": prefix, "run": run_number, "file": path,
        "samples": len(result["records"]), "records": result["records"],
    }
    with _lock:
        _state["saved"].append(entry)
    return entry


def _run_sequence(config):
    robot = None
    try:
        robot = _robot(config)
        pickup_low = _pose(config, config["pickup_point"], "pickup_height")
        pickup_high = _pose(config, config["pickup_point"], "transport_height")
        drop_low = _pose(config, config["drop_point"], "pickup_height")
        drop_high = _pose(config, config["drop_point"], "transport_height")
        _pump(robot, "off")
        for run_number in range(1, config["runs"] + 1):
            _set_state(run=run_number, phase="positioning",
                       message=f"Run {run_number}: positioning above tag")
            _move(robot, pickup_high)

            _set_state(phase="lift", message=f"Run {run_number}: recording lift")
            def lift():
                _move(robot, pickup_low)
                _pump(robot, "suck")
                time.sleep(0.18)
                _move(robot, pickup_high)
            _capture_with_action(config, "lift", run_number, lift)

            _set_state(phase="transfer", message=f"Run {run_number}: moving to drop point")
            _move(robot, drop_high)
            _move(robot, drop_low)

            _set_state(phase="drop", message=f"Run {run_number}: recording drop")
            def drop():
                _pump(robot, "blow")
                time.sleep(0.16)
                _pump(robot, "off")
            _capture_with_action(config, "drop", run_number, drop)
            _move(robot, drop_high)
        _set_state(phase="complete", message=f"Completed {config['runs']} runs")
    except InterruptedError:
        _set_state(phase="stopped", message="Sequence stopped early")
    except Exception as exc:
        _set_state(phase="error", message=str(exc), error=str(exc))
    finally:
        if robot is not None:
            try:
                if _stop.is_set():
                    robot.hold()
                robot.set_pump("off", 2, 1)
            except Exception:
                pass
        _set_state(running=False, finished_at=time.time())


@bp.route("/")
def index():
    response = send_from_directory(HERE, "controller.html")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@bp.route("/api/setup")
def api_setup():
    return jsonify({
        "ok": True, "units": _units(), "calibrations": _calibration_options(),
        "state": _state_payload(),
    })


@bp.route("/api/state")
def api_state():
    try:
        since = max(0, int(request.args.get("since", 0)))
    except (TypeError, ValueError):
        since = 0
    return jsonify({"ok": True, "state": _state_payload(since)})


@bp.route("/api/start", methods=["POST"])
def api_start():
    global _worker
    data = request.get_json(silent=True) or {}
    with _lock:
        if _state["running"]:
            return jsonify({"ok": False, "error": "a sequence is already running"}), 409
    try:
        unit = int(data.get("unit"))
        runs = int(data.get("runs"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "tag and run count are required"}), 400
    project = _safe_part(data.get("project"), "")
    folder = os.path.abspath(os.path.expanduser(str(data.get("folder") or "").strip()))
    calibration = str(data.get("calibration") or "")
    side = str(data.get("side") or "")
    pickup = str(data.get("pickup_point") or "")
    drop = str(data.get("drop_point") or "")
    if not project:
        return jsonify({"ok": False, "error": "project name is required"}), 400
    if not str(data.get("folder") or "").strip():
        return jsonify({"ok": False, "error": "target folder is required"}), 400
    if not os.path.isdir(folder):
        return jsonify({"ok": False, "error": "target folder does not exist"}), 400
    if calibration not in ("legacy", "cal2") or side not in SIDES:
        return jsonify({"ok": False, "error": "arm and calibration are required"}), 400
    if not pickup or not drop or not 1 <= runs <= 10000:
        return jsonify({"ok": False, "error": "valid points and 1–10000 runs are required"}), 400
    if _unit(unit) is None:
        return jsonify({"ok": False, "error": "selected tag is unavailable"}), 400
    try:
        os.makedirs(os.path.join(folder, project), exist_ok=True)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"cannot create project folder: {exc}"}), 400
    config = {
        "unit": unit, "runs": runs, "project": project, "folder": folder,
        "calibration": calibration, "side": side,
        "pickup_point": pickup, "drop_point": drop,
    }
    _stop.clear()
    _set_state(running=True, phase="starting", message="Starting sequence",
               run=0, runs=runs, saved=[], error=None,
               started_at=time.time(), finished_at=None)
    _worker = threading.Thread(target=_run_sequence, args=(config,), daemon=True)
    _worker.start()
    return jsonify({"ok": True, "state": _state_payload()})


@bp.route("/api/stop", methods=["POST"])
def api_stop():
    _stop.set()
    auto = _module("auto-pickup-game")
    with _lock:
        running = _state["running"]
        _state["message"] = "Stopping arm and sequence…" if running else "Already stopped"
    if auto is not None:
        for side in SIDES:
            robot = auto._direct_robot(side)
            if robot is not None and robot.is_connected():
                try:
                    robot.hold()
                except Exception:
                    pass
    return jsonify({"ok": True, "state": _public_state()})
