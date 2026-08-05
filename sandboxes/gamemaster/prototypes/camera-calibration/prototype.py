"""Gamemaster camera calibration using OpenCV's chessboard lens model."""

import json
import os
import threading
import time

import cv2
import numpy as np
from flask import Blueprint, Response, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_PATH = os.path.join(HERE, "calibration.json")
JPEG_QUALITY = 85
PREVIEW_FPS = 6
DETECT_MAX_WIDTH = 960

MANIFEST = {
    "name": "Camera Calibration",
    "description": "Calibrate and remove wide-angle camera distortion with an "
                   "OpenCV chessboard workflow, a clean grid for the physical "
                   "display, and a live corrected preview.",
    "default_page": "",
    "pages": [
        {"path": "", "label": "Camera Calibration"},
        {"path": "screen", "label": "Open calibration grid ↗", "newtab": True},
    ],
}
bp = Blueprint("camera_calibration", __name__)

_webcam = None
_lock = threading.Lock()
_samples = []
_image_size = None
_board = (15, 9)  # 16x10 squares: exactly fills a 16:10 display
_square_size = 25.0
_calibration = None
_enabled = True
_board_found = False
_board_checked_at = 0.0
_corrected_jpeg = None
_corrected_thread = None
_map_cache = {}
_pattern_revision = 0
_pattern_ack_revision = 0
_target_pattern = {"mode": "alignment", "scale": 1.0, "x": 0.5, "y": 0.5}
CALIBRATION_PATTERNS = [
    {"mode": "capture", "scale": 0.62, "x": x, "y": y}
    for y in (0.0, 0.5, 1.0) for x in (0.0, 0.5, 1.0)
]
MINIMUM_PATTERN_SAMPLES = 6


def _show_pattern(pattern):
    global _pattern_revision, _target_pattern
    with _lock:
        _pattern_revision += 1
        _target_pattern = dict(pattern)
        return _pattern_revision


def _restore_alignment_pattern():
    _show_pattern({"mode": "alignment", "scale": 1.0, "x": 0.5, "y": 0.5})


def _wait_for_pattern(revision, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            if _pattern_ack_revision >= revision:
                return True
        time.sleep(0.025)
    return False


def hub_init(ctx):
    global _webcam
    _webcam = ctx.get_prototype("webcam")
    if _webcam is not None and hasattr(_webcam, "set_tag_transformer"):
        _webcam.set_tag_transformer(correct_tag_sets)
    _load_calibration()


def _load_calibration():
    global _calibration, _board, _square_size
    try:
        with open(CALIBRATION_PATH) as f:
            data = json.load(f)
        data["camera_matrix"] = np.asarray(data["camera_matrix"], dtype=np.float64)
        data["dist_coeffs"] = np.asarray(data["dist_coeffs"], dtype=np.float64)
        _calibration = data
        _board = tuple(data.get("board", _board))
        _square_size = float(data.get("square_size", _square_size))
    except (OSError, ValueError, TypeError, KeyError):
        _calibration = None


def _save_calibration(data):
    serial = {
        "camera_matrix": data["camera_matrix"].tolist(),
        "dist_coeffs": data["dist_coeffs"].reshape(-1).tolist(),
        "image_size": list(data["image_size"]),
        "board": list(_board),
        "square_size": _square_size,
        "rms": data["rms"],
        "samples": data["samples"],
        "aspect_constrained": bool(data.get("aspect_constrained", False)),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with open(CALIBRATION_PATH, "w") as f:
        json.dump(serial, f, indent=2)


def _frame():
    if _webcam is None:
        return None
    manager = getattr(_webcam, "_mgr", None)
    return manager.latest_frame() if manager is not None else None


def _corners(frame, max_width=None):
    """Find board corners, optionally on a smaller image to protect capture FPS."""
    scale = 1.0
    work = frame
    if max_width and frame.shape[1] > max_width:
        scale = max_width / frame.shape[1]
        work = cv2.resize(frame, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    found, corners = False, None
    # The sector-based detector is more tolerant of uneven illumination and
    # small edge obstructions. Fall back for older OpenCV builds and difficult
    # frames where the classic detector still performs better.
    if hasattr(cv2, "findChessboardCornersSB"):
        sb_flags = cv2.CALIB_CB_NORMALIZE_IMAGE
        sb_flags |= getattr(cv2, "CALIB_CB_EXHAUSTIVE", 0)
        sb_flags |= getattr(cv2, "CALIB_CB_ACCURACY", 0)
        found, corners = cv2.findChessboardCornersSB(gray, _board, sb_flags)
    if not found:
        flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
        found, corners = cv2.findChessboardCorners(gray, _board, flags)
    elif scale != 1.0:
        corners /= scale
        return found, corners
    if found:
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
        corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
        if scale != 1.0:
            corners /= scale
    return found, corners


def _cached_board_check(frame):
    """Run at most one lightweight board search per second across all clients."""
    global _board_found, _board_checked_at
    now = time.monotonic()
    with _lock:
        if now - _board_checked_at < 1.0:
            return _board_found
        # Reserve the check before doing the expensive OpenCV call so concurrent
        # status requests do not all start their own detection.
        _board_checked_at = now
    found, _ = _corners(frame, DETECT_MAX_WIDTH)
    with _lock:
        _board_found = bool(found)
    return bool(found)


def _object_points():
    points = np.zeros((_board[0] * _board[1], 3), np.float32)
    points[:, :2] = np.mgrid[0:_board[0], 0:_board[1]].T.reshape(-1, 2)
    points *= _square_size
    return points


def _draw_grid(frame, spacing=80):
    h, w = frame.shape[:2]
    color = (70, 210, 255)
    for x in range(spacing, w, spacing):
        cv2.line(frame, (x, 0), (x, h), color, 1, cv2.LINE_AA)
    for y in range(spacing, h, spacing):
        cv2.line(frame, (0, y), (w, y), color, 1, cv2.LINE_AA)
    cv2.line(frame, (w // 2, 0), (w // 2, h), (80, 255, 120), 2)
    cv2.line(frame, (0, h // 2), (w, h // 2), (80, 255, 120), 2)
    cv2.circle(frame, (w // 2, h // 2), 21, (80, 255, 120), 3, cv2.LINE_AA)


def _camera_model(width, height, calibration):
    source_width, source_height = tuple(calibration["image_size"])
    matrix = calibration["camera_matrix"].copy()
    if (source_width, source_height) != (width, height):
        matrix[0, :] *= width / source_width
        matrix[1, :] *= height / source_height
    output, _ = cv2.getOptimalNewCameraMatrix(
        matrix, calibration["dist_coeffs"], (width, height), 0.2, (width, height))
    # Camera pixels are square. Keep identical horizontal/vertical focal scale
    # so undistortion cannot squeeze a 16:9 source toward a square image.
    focal = min(float(output[0, 0]), float(output[1, 1]))
    output[0, 0] = focal
    output[1, 1] = focal
    # getOptimalNewCameraMatrix may move the principal point while choosing a
    # useful crop, which makes the corrected picture visibly jump sideways or
    # vertically. Anchor the raw frame's geometric centre to the same output
    # pixel while retaining the optimized focal lengths and distortion model.
    image_centre = np.asarray([[[width / 2.0, height / 2.0]]], dtype=np.float64)
    centre_ray = cv2.undistortPoints(
        image_centre, matrix, calibration["dist_coeffs"])[0, 0]
    output[0, 2] = width / 2.0 - (
        output[0, 0] * centre_ray[0] + output[0, 1] * centre_ray[1])
    output[1, 2] = height / 2.0 - (
        output[1, 0] * centre_ray[0] + output[1, 1] * centre_ray[1])
    return matrix, output


def _usable_calibration(calibration):
    if calibration is None:
        return False
    try:
        matrix = np.asarray(calibration["camera_matrix"], dtype=np.float64)
        distortion = np.asarray(calibration["dist_coeffs"], dtype=np.float64)
        base_valid = (
            matrix.shape == (3, 3)
            and np.isfinite(matrix).all()
            and np.isfinite(distortion).all()
            and matrix[0, 0] > 0
            and matrix[1, 1] > 0
            and 0.99 <= matrix[0, 0] / matrix[1, 1] <= 1.01
        )
        if not base_valid:
            return False
        if calibration.get("aspect_constrained"):
            return True
        # Reject legacy single-view solutions whose focal length ran away.
        width, height = tuple(calibration["image_size"])
        focal_limit = max(width, height)
        return (focal_limit * 0.1 <= matrix[0, 0] <= focal_limit * 10.0 and
                focal_limit * 0.1 <= matrix[1, 1] <= focal_limit * 10.0)
    except (KeyError, TypeError, ValueError):
        return False


def _undistort(frame, calibration):
    height, width = frame.shape[:2]
    key = (id(calibration), width, height)
    maps = _map_cache.get(key)
    if maps is None:
        matrix, output = _camera_model(width, height, calibration)
        maps = cv2.initUndistortRectifyMap(
            matrix, calibration["dist_coeffs"], None, output,
            (width, height), cv2.CV_32FC1)
        _map_cache.clear()
        _map_cache[key] = maps
    return cv2.remap(frame, maps[0], maps[1], cv2.INTER_LINEAR)


def _corrected_worker():
    """Encode correction once; all Gamemaster/player viewers share this cache."""
    global _corrected_jpeg
    while True:
        frame = _frame()
        if frame is not None:
            with _lock:
                calibration = _calibration
            if _usable_calibration(calibration):
                try:
                    frame = _undistort(frame, calibration)
                except cv2.error:
                    pass
            ok, buf = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
            if ok:
                with _lock:
                    _corrected_jpeg = buf.tobytes()
        time.sleep(1 / 12)


def _ensure_corrected_worker():
    global _corrected_thread
    with _lock:
        if _corrected_thread is None or not _corrected_thread.is_alive():
            _corrected_thread = threading.Thread(
                target=_corrected_worker, name="corrected-camera", daemon=True)
            _corrected_thread.start()


def _corrected_stream():
    _ensure_corrected_worker()
    while True:
        with _lock:
            jpeg = _corrected_jpeg
        if jpeg:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        time.sleep(1 / 12)


def _preview():
    while True:
        frame = _frame()
        if frame is None:
            frame = np.full((720, 1280, 3), 18, np.uint8)
            cv2.putText(frame, "Open a camera in the Webcam tab", (45, 360),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (150, 160, 175), 2)
        else:
            with _lock:
                calibration = _calibration
                enabled = _enabled
            if _usable_calibration(calibration) and enabled:
                try:
                    frame = _undistort(frame, calibration)
                except cv2.error:
                    calibration = None
            _draw_grid(frame)
            label = "CORRECTED" if _usable_calibration(calibration) and enabled else "RAW"
            cv2.putText(frame, label, (18, 35), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (80, 255, 120) if label == "CORRECTED" else (70, 210, 255),
                        2, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if ok:
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" +
                   buf.tobytes() + b"\r\n")
        time.sleep(1 / PREVIEW_FPS)


@bp.route("/")
def index():
    return send_from_directory(HERE, "controller.html")


@bp.route("/screen")
def screen():
    resp = send_from_directory(HERE, "screen.html")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@bp.route("/api/stream")
def stream():
    return Response(_preview(), mimetype="multipart/x-mixed-replace; boundary=frame")


@bp.route("/api/corrected-stream")
def corrected_stream():
    """Clean corrected feed for Auto PP and other camera consumers."""
    return Response(_corrected_stream(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


def _correct_tag(tag, width, height, calibration):
    corners = tag.get("corners") or []
    source = corners if len(corners) >= 4 else [[tag["x"], tag["y"]]]
    points = np.asarray(source, dtype=np.float64).reshape(-1, 1, 2)
    matrix, output = _camera_model(width, height, calibration)
    fixed = cv2.undistortPoints(
        points, matrix, calibration["dist_coeffs"], P=output).reshape(-1, 2)
    center = fixed.mean(axis=0)
    result = dict(tag)
    result.update({
        "x": round(float(center[0]), 1), "y": round(float(center[1]), 1),
        "nx": round(float(center[0] / width), 4),
        "ny": round(float(center[1] / height), 4),
        "corners": [[round(float(x), 1), round(float(y), 1)] for x, y in fixed],
        "ncorners": [
            [round(float(x / width), 6), round(float(y / height), 6)]
            for x, y in fixed
        ],
    })
    if len(fixed) >= 2:
        edge = fixed[1] - fixed[0]
        result["rotation"] = round(
            float(np.degrees(np.arctan2(edge[1], edge[0]))), 1)
    return result


def correct_tag_sets(tags, detections):
    """Transform Webcam detections while preserving its reliable API route."""
    manager = getattr(_webcam, "_mgr", None) if _webcam is not None else None
    camera_status = manager.status() if manager is not None else {}
    width = int(camera_status.get("width") or 0)
    height = int(camera_status.get("height") or 0)
    with _lock:
        calibration = _calibration
    if not (_usable_calibration(calibration) and width and height):
        return tags, detections, False
    try:
        return (
            [_correct_tag(tag, width, height, calibration) for tag in tags],
            [_correct_tag(tag, width, height, calibration) for tag in detections],
            True,
        )
    except (cv2.error, ValueError, TypeError, KeyError):
        return tags, detections, False


@bp.route("/api/tags")
def corrected_tags():
    """Webcam tag reads transformed into the corrected feed's coordinates."""
    manager = getattr(_webcam, "_mgr", None) if _webcam is not None else None
    tracker = getattr(_webcam, "_tracker", None) if _webcam is not None else None
    if manager is None or tracker is None:
        return jsonify({"tags": [], "detections": [], "corrected": False}), 503
    now = time.monotonic()
    tags = tracker.tags(now)
    detections = manager.detections(now)
    tags, detections, corrected = correct_tag_sets(tags, detections)
    camera_status = manager.status()
    width = int(camera_status.get("width") or 0)
    height = int(camera_status.get("height") or 0)
    return jsonify({
        "tags": tags, "detections": detections,
        "corrected": corrected, "width": width, "height": height,
    })


@bp.route("/api/status")
def status():
    frame = _frame()
    found = _cached_board_check(frame) if frame is not None else False
    with _lock:
        calibration = _calibration
        return jsonify({
            "camera_ready": frame is not None,
            "board_found": bool(found),
            "samples": len(_samples),
            "minimum_samples": MINIMUM_PATTERN_SAMPLES,
            "calibrated": _usable_calibration(calibration),
            "enabled": _enabled,
            "rms": calibration.get("rms") if calibration else None,
            "board": list(_board),
            "square_size": _square_size,
        })


@bp.route("/api/focus", methods=["GET", "POST"])
def focus():
    """Read or update UVC focus and exposure controls on the active webcam."""
    manager = getattr(_webcam, "_mgr", None) if _webcam is not None else None
    if manager is None or not hasattr(manager, "focus_controls"):
        return jsonify({
            "ok": False,
            "error": "webcam focus controls are unavailable",
        }), 503
    if request.method == "GET":
        return jsonify({"ok": True, **manager.focus_controls()})
    data = request.get_json(silent=True) or {}
    autofocus = data.get("autofocus") if "autofocus" in data else None
    if autofocus is not None and not isinstance(autofocus, bool):
        return jsonify({"ok": False, "error": "autofocus must be true or false"}), 400
    focus_value = data.get("focus") if "focus" in data else None
    autoexposure = data.get("autoexposure") if "autoexposure" in data else None
    if autoexposure is not None and not isinstance(autoexposure, bool):
        return jsonify({
            "ok": False, "error": "autoexposure must be true or false",
        }), 400
    exposure_value = data.get("exposure") if "exposure" in data else None
    ok, error, controls = manager.set_focus_controls(
        autofocus=autofocus, focus=focus_value,
        autoexposure=autoexposure, exposure=exposure_value)
    if not ok:
        return jsonify({"ok": False, "error": error}), 409
    return jsonify({"ok": True, **controls})


@bp.route("/api/cameras")
def cameras():
    """List webcams for the focus panel and identify the active selection."""
    manager = getattr(_webcam, "_mgr", None) if _webcam is not None else None
    probe = getattr(_webcam, "probe_cameras", None) if _webcam is not None else None
    if manager is None or not callable(probe):
        return jsonify({"ok": False, "error": "webcam controls are unavailable"}), 503
    return jsonify({
        "ok": True,
        "cameras": probe(),
        "active_index": manager.active_index(),
    })


@bp.route("/api/camera", methods=["POST"])
def select_camera():
    """Select and remember the webcam whose focus is being adjusted."""
    manager = getattr(_webcam, "_mgr", None) if _webcam is not None else None
    load_settings = getattr(_webcam, "_load_settings", None) if _webcam is not None else None
    if manager is None:
        return jsonify({"ok": False, "error": "webcam controls are unavailable"}), 503
    data = request.get_json(silent=True) or {}
    try:
        index = int(data["index"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "expected integer camera index"}), 400
    remembered = load_settings() if callable(load_settings) else None
    remembered = remembered or {}
    width = int(remembered.get("width") or 1920)
    height = int(remembered.get("height") or 1080)
    tracking_width = int(remembered.get("tracking_width") or width)
    tracking_height = int(remembered.get("tracking_height") or height)
    detection_fps = float(remembered.get("detection_fps") or 15)
    ok, error = manager.open(index, width, height, tracking_width,
                             tracking_height, detection_fps)
    if not ok:
        return jsonify({"ok": False, "error": error}), 502
    return jsonify({"ok": True, "status": manager.status()})


@bp.route("/api/config", methods=["GET", "POST"])
def config():
    global _board, _square_size
    if request.method == "GET":
        with _lock:
            return jsonify({
                "board": list(_board), "square_size": _square_size,
                "pattern": {**_target_pattern, "revision": _pattern_revision},
            })
    data = request.get_json(silent=True) or {}
    try:
        cols, rows = int(data["cols"]), int(data["rows"])
        square = float(data["square_size"])
        if not (3 <= cols <= 20 and 3 <= rows <= 20 and square > 0):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid board configuration"}), 400
    with _lock:
        if (cols, rows) != _board:
            _samples.clear()
        _board = (cols, rows)
        _square_size = square
    return jsonify({"ok": True})


@bp.route("/api/pattern-ack", methods=["POST"])
def pattern_ack():
    """Confirm that the target screen has rendered a requested pattern."""
    global _pattern_ack_revision
    try:
        revision = int((request.get_json(silent=True) or {})["revision"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid pattern revision"}), 400
    with _lock:
        _pattern_ack_revision = max(_pattern_ack_revision, revision)
    return jsonify({"ok": True})


@bp.route("/api/capture", methods=["POST"])
def capture():
    global _image_size
    captured, skipped, size = [], [], None
    try:
        for index, pattern in enumerate(CALIBRATION_PATTERNS, 1):
            attempts = [pattern]
            # A physical frame commonly masks one or two squares at a display
            # corner. Retry corner samples slightly inward before skipping them.
            if pattern["x"] in (0.0, 1.0) and pattern["y"] in (0.0, 1.0):
                attempts.append({
                    **pattern,
                    "x": 0.18 if pattern["x"] == 0.0 else 0.82,
                    "y": 0.18 if pattern["y"] == 0.0 else 0.82,
                })
            sample = None
            for attempt in attempts:
                revision = _show_pattern(attempt)
                if not _wait_for_pattern(revision):
                    return jsonify({
                        "ok": False,
                        "error": "calibration target did not respond; open the "
                                 "calibration grid screen and try again",
                    }), 409
                time.sleep(0.18)  # display refresh and exposure settling
                frame = _frame()
                if frame is None:
                    return jsonify({"ok": False, "error": "no camera frame"}), 409
                found, corners = _corners(frame)
                if found:
                    sample = corners.copy()
                    break
            if sample is None:
                skipped.append(index)
                continue
            h, w = frame.shape[:2]
            if size not in (None, (w, h)):
                return jsonify({"ok": False, "error": "camera size changed during scan"}), 409
            size = (w, h)
            captured.append(sample)
    finally:
        _restore_alignment_pattern()
    if len(captured) < MINIMUM_PATTERN_SAMPLES:
        return jsonify({
            "ok": False,
            "error": f"only {len(captured)}/9 checkerboards were detected; "
                     f"at least {MINIMUM_PATTERN_SAMPLES} are required",
            "skipped": skipped,
        }), 422
    with _lock:
        _samples.clear()
        _samples.extend(captured)
        _image_size = size
    return jsonify({
        "ok": True, "samples": len(captured), "patterns": len(captured),
        "skipped": skipped,
    })


@bp.route("/api/reset", methods=["POST"])
def reset():
    global _image_size
    with _lock:
        _samples.clear()
        _image_size = None
    return jsonify({"ok": True})


@bp.route("/api/calibration", methods=["DELETE"])
def clear_calibration():
    global _calibration, _image_size
    with _lock:
        _calibration = None
        _samples.clear()
        _image_size = None
        _map_cache.clear()
        try:
            os.remove(CALIBRATION_PATH)
        except FileNotFoundError:
            pass
        except OSError as exc:
            return jsonify({
                "ok": False,
                "error": f"could not remove saved calibration: {exc}",
            }), 500
    return jsonify({"ok": True})


@bp.route("/api/calibrate", methods=["POST"])
def calibrate():
    global _calibration
    with _lock:
        samples = [x.copy() for x in _samples]
        size = _image_size
    if size is None or not samples:
        return jsonify({"ok": False, "error": "capture a calibration sample"}), 409
    objects = [_object_points() for _ in samples]
    width, height = size
    focal = float(max(width, height))
    initial_matrix = np.asarray([
        [focal, 0.0, width / 2.0],
        [0.0, focal, height / 2.0],
        [0.0, 0.0, 1.0],
    ], dtype=np.float64)
    # A single planar view cannot reliably estimate pixel aspect ratio or the
    # principal point independently. Constrain both to their physical camera
    # properties to prevent a 16:9 frame being calibrated into a squeezed view.
    flags = (cv2.CALIB_USE_INTRINSIC_GUESS |
             cv2.CALIB_FIX_ASPECT_RATIO |
             cv2.CALIB_FIX_PRINCIPAL_POINT)
    rms, matrix, dist, _, _ = cv2.calibrateCamera(
        objects, samples, size, initial_matrix, None, flags=flags)
    data = {
        "camera_matrix": matrix, "dist_coeffs": dist, "image_size": size,
        "rms": float(rms), "samples": len(samples),
        "aspect_constrained": True,
    }
    with _lock:
        _calibration = data
        _save_calibration(data)
    return jsonify({"ok": True, "rms": float(rms)})


@bp.route("/api/enabled", methods=["POST"])
def enabled():
    global _enabled
    _enabled = bool((request.get_json(silent=True) or {}).get("enabled", True))
    return jsonify({"ok": True, "enabled": _enabled})
