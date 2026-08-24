"""
Webcam prototype — OpenCV camera capture for Manual Override.

Opens a webcam with OpenCV, streams the live video into the config GUI as an
MJPEG feed, and lets you pick which camera (by index) to use from a dropdown.
This is the "eyes" for the perception stage (missions 2.1 / 2.2): for now it just
captures and shows frames; ArUco-marker detection drops into the grab loop later
(see `_process_frame`).

A single background thread owns the capture and keeps the latest JPEG-encoded
frame in memory; the /api/stream route serves it as multipart/x-mixed-replace, so
many viewers share one capture. Switching cameras swaps the capture underneath
the same stream, so the <img> never needs reloading.

Loaded by hub.py; registered under /p/webcam. No app.run() of its own — it only
runs inside the hub server. If OpenCV isn't installed the import fails and the hub
reports the prototype as failed to load while the others keep running; install
this folder's requirements.txt to enable it.
"""

import json
import math
import os
import re
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
from flask import Blueprint, Response, jsonify, request, send_from_directory

import live   # shared push helper (prototypes/live.py)

HERE = os.path.dirname(os.path.abspath(__file__))

MANIFEST = {
    "name": "Webcam",
    "description": "Live OpenCV camera feed with ArUco tracking (printed DICT_4X4_50 "
                   "tags, DICT_4X4_100 ids 50-55, and the Atom-screen 3x3 set "
                   "as ids 100-105): a debounced "
                   "list of tags (id, x, y, rotation) at /api/tags, for other "
                   "prototypes. Needs opencv-python (see requirements.txt).",
    "default_page": "controller",   # the config screen the hub embeds
    "pages": [
        {"path": "controller", "label": "Controller"},
        {"path": "view", "label": "Open clean view ↗", "newtab": True},
    ],
}
bp = Blueprint("webcam", __name__)

# ---- configuration --------------------------------------------------------
DEFAULT_INDEX = 0
PROBE_MAX = 6          # probe camera indices 0..PROBE_MAX-1 when listing
JPEG_QUALITY = 80      # MJPEG frame quality (0-100)
STREAM_FPS = 30        # cap on how fast the stream route pushes frames
DEFAULT_W = 1920       # resolution we ask the camera for (it may pick another)
DEFAULT_H = 1080
DEFAULT_DETECTION_FPS = 15
# Resolutions offered in the GUI. A webcam reports the closest mode it supports;
# the status panel shows what you actually got. Reaching 1080p+/4K usually needs
# the MJPG capture codec (set below) — many UVC cams only expose high modes there.
RESOLUTIONS = [
    {"w": 640,  "h": 480,  "label": "640×480 (VGA)"},
    {"w": 1280, "h": 720,  "label": "1280×720 (720p)"},
    {"w": 1920, "h": 1080, "label": "1920×1080 (1080p)"},
    {"w": 2560, "h": 1440, "label": "2560×1440 (1440p)"},
    {"w": 3840, "h": 2160, "label": "3840×2160 (4K)"},
]
SETTINGS_PATH = os.path.join(HERE, "camera-settings.json")
NATIVE_FOCUS_APP = os.path.join(
    HERE, "native-focus-lock", "dist", "Focus Lock.app", "Contents", "MacOS",
    "focus-lock")

# ---- ArUco tag tracking ----------------------------------------------------
# Detect DICT_4X4_50 markers each frame and keep a debounced list of tags:
#   * a marker must be seen for > PROMOTE_SECS before it's added to the list
#     (so a one-frame false positive never appears), and
#   * once tracked it stays in the list until it's been missing > DROP_SECS.
# The list (id, x, y, rotation) is exposed over /api/tags and via get_tags() for
# other prototypes through the hub.
TAG_DICT = cv2.aruco.DICT_4X4_50
PROMOTE_SECS = 1.0       # seen this long (continuously) before it's tracked
DROP_SECS = 3.0          # removed this long after it goes missing
STREAK_GRACE = 0.5       # a gap longer than this restarts the qualifying clock
DETECTION_HOLD_SECS = 10.0  # keep one-shot reads long enough for game clients

_aruco_dict = cv2.aruco.getPredefinedDictionary(TAG_DICT)
_detector = cv2.aruco.ArucoDetector(_aruco_dict, cv2.aruco.DetectorParameters())

# Tower Defense adds fixed socket ids 50-55. DICT_4X4_50 cannot encode those
# ids, so an additive DICT_4X4_100 pass is filtered to just that range. The
# original detector and camera stream stay unchanged for every existing game.
TOWER_TAG_MIN = 50
TOWER_TAG_MAX = 55
_tower_aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_100)
_tower_detector = cv2.aruco.ArucoDetector(
    _tower_aruco_dict, cv2.aruco.DetectorParameters())

# Second, coarse dictionary used by the Atom screens: atom-manager renders markers
# with cv2.aruco.extendDictionary(6, 3); we build it identically here so we can read
# them. Their ids are shifted by SCREEN_BASE so they never collide with the printed
# DICT_4X4_50 tags (corners 1-4, head 10, ...): screen marker N appears as 100 + N.
SCREEN_NMARKERS = 6
SCREEN_BITS = 3
SCREEN_BASE = 100
_screen_dict = cv2.aruco.extendDictionary(SCREEN_NMARKERS, SCREEN_BITS)
_screen_detector = cv2.aruco.ArucoDetector(_screen_dict, cv2.aruco.DetectorParameters())


def _usb_camera_devices():
    """Return real USB camera names/ids, primarily for macOS AVFoundation."""
    if sys.platform != "darwin":
        return []
    try:
        result = subprocess.run(
            ["/usr/sbin/system_profiler", "SPUSBDataType", "-json"],
            capture_output=True, text=True, timeout=8, check=True)
        roots = json.loads(result.stdout).get("SPUSBDataType", [])
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return []
    devices = []

    def visit(items):
        for item in items or []:
            name = str(item.get("_name") or "")
            if re.search(r"(camera|webcam)", name, re.I):
                vendor = str(item.get("vendor_id") or "").split()[0]
                product = str(item.get("product_id") or "").split()[0]
                devices.append({
                    "name": name,
                    "vendor": vendor,
                    "product": product,
                    "location": str(item.get("location_id") or ""),
                })
            visit(item.get("_items"))

    visit(roots)
    return devices


def _native_focus(device, command="status", value=None):
    """Run the original macOS UVC helper while the capture device is closed."""
    if not device or not os.access(NATIVE_FOCUS_APP, os.X_OK):
        raise RuntimeError("native Focus Lock helper has not been built")
    args = [
        NATIVE_FOCUS_APP, command, device["vendor"], device["product"],
    ]
    if value is not None:
        args.append(str(value))
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=5, check=False)
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        raise RuntimeError(f"native Focus Lock failed: {exc}") from exc
    if result.returncode or not data.get("ok"):
        raise RuntimeError(data.get("error") or "native Focus Lock request failed")
    return data


def _start_native_focus(device):
    """Open and retain the macOS UVC control interface before AVFoundation."""
    if not device or not os.access(NATIVE_FOCUS_APP, os.X_OK):
        raise RuntimeError("native Focus Lock helper has not been built")
    process = subprocess.Popen(
        [NATIVE_FOCUS_APP, "serve", device["vendor"], device["product"]],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    try:
        data = json.loads(process.stdout.readline())
    except (ValueError, TypeError) as exc:
        process.terminate()
        raise RuntimeError("native Focus Lock returned invalid startup data") from exc
    if not data.get("ok"):
        process.terminate()
        raise RuntimeError(data.get("error") or "native Focus Lock could not start")
    return process, data


def _native_focus_request(process, command, value=None):
    if process is None or process.poll() is not None:
        raise RuntimeError("native Focus Lock is not running")
    line = command if value is None else f"{command} {value}"
    try:
        process.stdin.write(line + "\n")
        process.stdin.flush()
        data = json.loads(process.stdout.readline())
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"native Focus Lock request failed: {exc}") from exc
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "native Focus Lock request failed")
    return data


def _collect(dets, corners, ids, scale, w, h, id_offset=0,
             id_min=None, id_max=None):
    """Fold detectMarkers() output into `dets` in full-res coords; ids are shifted
    by `id_offset` so markers from different dictionaries don't clash."""
    if ids is None:
        return
    for c, mid in zip(corners, ids.flatten()):
        marker_id = int(mid)
        if id_min is not None and marker_id < id_min:
            continue
        if id_max is not None and marker_id > id_max:
            continue
        pts = c.reshape(4, 2) / scale            # back to full-res pixels
        cx, cy = pts.mean(axis=0)
        dx, dy = pts[1] - pts[0]                  # top edge -> orientation
        dets[id_offset + marker_id] = {
            "x": float(cx), "y": float(cy),
            "nx": float(cx / w), "ny": float(cy / h),
            "rot": float(math.degrees(math.atan2(dy, dx))),
            "corners": pts,
        }


def _detect(frame, target_width, target_height):
    """Detect markers from all supported dictionaries in full-res pixel coords.

    Printed DICT_4X4_50 tags keep their ids;
    Atom-screen (3x3) markers come back as SCREEN_BASE + id (100..105)."""
    h, w = frame.shape[:2]
    scale = min(1.0, target_width / w, target_height / h)
    small = cv2.resize(frame, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_AREA) if scale < 1.0 else frame
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    dets = {}
    c, i, _ = _detector.detectMarkers(gray)
    _collect(dets, c, i, scale, w, h)                       # printed 4x4 tags
    c, i, _ = _tower_detector.detectMarkers(gray)
    _collect(dets, c, i, scale, w, h,
             id_min=TOWER_TAG_MIN, id_max=TOWER_TAG_MAX)    # fixed sockets 50-55
    c, i, _ = _screen_detector.detectMarkers(gray)
    _collect(dets, c, i, scale, w, h, SCREEN_BASE)          # Atom-screen markers
    return dets


class TagTracker:
    """Debounced ArUco tag list with promote-after-1s / drop-after-3s logic."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tags = {}      # id -> record

    def update(self, dets, now):
        with self._lock:
            for mid, d in dets.items():
                rec = self._tags.get(mid)
                if rec is None:
                    rec = {"id": mid, "first_seen": now, "tracked": False, "tracked_since": None}
                    self._tags[mid] = rec
                elif not rec["tracked"] and (now - rec["last_seen"]) > STREAK_GRACE:
                    rec["first_seen"] = now          # streak broke; restart the clock
                rec["last_seen"] = now
                rec["x"], rec["y"] = d["x"], d["y"]
                rec["nx"], rec["ny"] = d["nx"], d["ny"]
                rec["rot"] = d["rot"]
                rec["corners"] = d["corners"]
                if not rec["tracked"] and (now - rec["first_seen"]) >= PROMOTE_SECS:
                    rec["tracked"] = True
                    rec["tracked_since"] = now
            for mid in list(self._tags):
                rec = self._tags[mid]
                age = now - rec["last_seen"]
                if (rec["tracked"] and age > DROP_SECS) or (not rec["tracked"] and age > DETECTION_HOLD_SECS):
                    del self._tags[mid]

    def tags(self, now):
        """The confirmed tracked tags, sorted by id."""
        with self._lock:
            out = []
            for rec in self._tags.values():
                if not rec["tracked"] or (now - rec["last_seen"]) > DROP_SECS:
                    continue
                out.append({
                    "id": rec["id"],
                    "x": round(rec["x"], 1), "y": round(rec["y"], 1),
                    "nx": round(rec["nx"], 4), "ny": round(rec["ny"], 4),
                    "rotation": round(rec["rot"], 1),
                    "missing": round(now - rec["last_seen"], 2),
                    "corners": [
                        [round(float(x), 1), round(float(y), 1)]
                        for x, y in rec.get("corners", [])
                    ],
                })
            out.sort(key=lambda t: t["id"])
            return out

    def detections(self, now):
        """Immediate marker reads, including tags still inside the debounce window."""
        with self._lock:
            out = []
            for rec in self._tags.values():
                missing = now - rec["last_seen"]
                if missing > DETECTION_HOLD_SECS:
                    continue
                out.append({
                    "id": rec["id"],
                    "x": round(rec["x"], 1), "y": round(rec["y"], 1),
                    "nx": round(rec["nx"], 4), "ny": round(rec["ny"], 4),
                    "rotation": round(rec["rot"], 1),
                    "missing": round(missing, 2),
                    "tracked": bool(rec["tracked"]),
                    "corners": [
                        [round(float(x), 1), round(float(y), 1)]
                        for x, y in rec.get("corners", [])
                    ],
                })
            out.sort(key=lambda t: t["id"])
            return out


_tracker = TagTracker()


def get_tags():
    """Programmatic API for other prototypes (via the hub): the confirmed list of
    tracked ArUco tags — [{id, x, y, nx, ny, rotation, missing}]."""
    return _tracker.tags(time.monotonic())


def get_tag(tag_id):
    """One tracked tag by id, or None if it isn't currently tracked."""
    tag_id = int(tag_id)
    for t in _tracker.tags(time.monotonic()):
        if t["id"] == tag_id:
            return t
    return None


def _backend():
    """Pick a sensible OpenCV capture backend per platform."""
    if sys.platform == "darwin":
        return cv2.CAP_AVFOUNDATION
    if sys.platform.startswith("win"):
        return cv2.CAP_DSHOW
    return cv2.CAP_ANY


def _placeholder(text):
    """A grey 'no signal' frame, JPEG-encoded, for when no camera is open."""
    img = np.full((720, 1280, 3), 18, np.uint8)
    cv2.putText(img, text, (40, 360),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (140, 150, 170), 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes() if ok else b""


class CameraManager:
    """Owns one OpenCV capture + a grab thread that keeps the latest JPEG frame.

    Thread-safe: callers read the latest frame / status under a lock; opening a
    new camera cleanly stops the previous grab thread first."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cap = None
        self._index = None
        self._device_info = None
        self._native_focus_state = None
        self._native_focus_error = None
        self._native_focus_process = None
        self._thread = None
        self._running = False
        self._latest = None        # latest JPEG bytes
        self._latest_raw = None    # unannotated BGR frame for calibration/vision clients
        self._w = 0                # actual capture width
        self._h = 0                # actual capture height
        self._req_w = 0            # requested width
        self._req_h = 0            # requested height
        self._preview_w = DEFAULT_W
        self._preview_h = DEFAULT_H
        self._actual_preview_w = 0
        self._actual_preview_h = 0
        self._tracking_w = DEFAULT_W
        self._tracking_h = DEFAULT_H
        self._detection_fps_limit = DEFAULT_DETECTION_FPS
        self._detection_fps = 0.0
        self._last_detection_ts = 0.0
        self._fps = 0.0            # measured capture fps (EMA)
        self._last_ts = 0.0
        self._error = None         # last open/read error, for the UI
        self._last_detections = {}
        self._overlay_detections = {}
        self._last_detections_at = 0.0
        self._no_signal = _placeholder("No camera selected")

    # -- lifecycle ------------------------------------------------------------
    def open(self, index, width=None, height=None, tracking_width=None,
             tracking_height=None, detection_fps=None):
        """Open camera `index` at a requested resolution, replacing any current
        one. The camera picks the closest mode it supports; status reports what
        was actually achieved. Returns (ok, error)."""
        width = int(width or DEFAULT_W)
        height = int(height or DEFAULT_H)
        tracking_width = int(tracking_width or width)
        tracking_height = int(tracking_height or height)
        detection_fps = max(1.0, min(30.0, float(
            detection_fps or DEFAULT_DETECTION_FPS)))
        capture_width = max(width, tracking_width)
        capture_height = max(height, tracking_height)
        with self._lock:
            self._stop_locked()
            devices = _usb_camera_devices()
            device_info = devices[index] if 0 <= index < len(devices) else None
            native_state = None
            native_error = None
            native_process = None
            if sys.platform == "darwin" and device_info:
                try:
                    native_process, native_state = _start_native_focus(device_info)
                except RuntimeError as exc:
                    native_error = str(exc)
            remembered = _load_settings() or {}
            if (native_state and
                    int(remembered.get("index", -1)) == int(index)):
                try:
                    autofocus = remembered.get("autofocus")
                    focus = remembered.get("focus")
                    autoexposure = remembered.get("autoexposure")
                    exposure = remembered.get("exposure")
                    if isinstance(autofocus, bool):
                        native_state = _native_focus_request(
                            native_process, "set-auto", 1 if autofocus else 0)
                    if autofocus is False and focus is not None:
                        native_state = _native_focus_request(
                            native_process, "set-focus", int(round(float(focus))))
                    if isinstance(autoexposure, bool):
                        native_state = _native_focus_request(
                            native_process, "set-autoexposure",
                            1 if autoexposure else 0)
                    if autoexposure is False and exposure is not None:
                        native_state = _native_focus_request(
                            native_process, "set-exposure",
                            int(round(float(exposure))))
                except (RuntimeError, TypeError, ValueError) as exc:
                    native_error = str(exc)
            cap = cv2.VideoCapture(index, _backend())
            if not cap.isOpened():
                cap.release()
                if native_process is not None:
                    native_process.terminate()
                self._error = f"could not open camera {index}"
                return False, self._error
            # Request MJPG first: most UVC webcams only expose 1080p+/4K and
            # higher frame rates through the MJPG codec, not raw (YUY2) frames.
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            except (cv2.error, AttributeError):
                pass
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, capture_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, capture_height)
            if (not native_state and
                    int(remembered.get("index", -1)) == int(index)):
                autofocus = remembered.get("autofocus")
                focus = remembered.get("focus")
                try:
                    if isinstance(autofocus, bool):
                        if not cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if autofocus else 0):
                            pass
                    if autofocus is False and focus is not None:
                        if not cap.set(cv2.CAP_PROP_FOCUS, float(focus)):
                            pass
                except (cv2.error, TypeError, ValueError):
                    pass
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # lower latency; not always honoured
            except cv2.error:
                pass
            self._cap = cap
            self._index = index
            self._device_info = device_info
            self._native_focus_state = native_state
            self._native_focus_error = native_error
            self._native_focus_process = native_process
            self._req_w, self._req_h = capture_width, capture_height
            self._preview_w, self._preview_h = width, height
            self._tracking_w, self._tracking_h = tracking_width, tracking_height
            self._detection_fps_limit = detection_fps
            self._error = None
            self._latest = None
            self._latest_raw = None
            self._last_detections = {}
            self._overlay_detections = {}
            self._fps = 0.0
            self._detection_fps = 0.0
            self._last_ts = 0.0
            self._last_detection_ts = 0.0
            self._running = True
            self._thread = threading.Thread(
                target=self._grab_loop, name=f"webcam-grab-{index}", daemon=True
            )
            self._thread.start()
        _save_settings(index, width, height, tracking_width, tracking_height,
                       detection_fps)
        return True, None

    def close(self):
        with self._lock:
            self._stop_locked()
            self._index = None
            self._device_info = None
            self._native_focus_state = None
            self._native_focus_error = None

    def _stop_locked(self):
        """Stop the grab thread and release the capture. Call with the lock held."""
        self._running = False
        thread = self._thread
        cap = self._cap
        native_process = self._native_focus_process
        self._thread = None
        self._cap = None
        self._latest = None
        self._latest_raw = None
        self._overlay_detections = {}
        self._native_focus_process = None
        # release the lock while joining so the grab loop can exit its read()
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            self._lock.release()
            try:
                thread.join(timeout=2.0)
            finally:
                self._lock.acquire()
        if cap is not None:
            cap.release()
        if native_process is not None:
            native_process.terminate()
            try:
                native_process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                native_process.kill()

    # -- capture loop ---------------------------------------------------------
    def _grab_loop(self):
        cap = self._cap
        while self._running and cap is not None:
            ok, frame = cap.read()
            if not ok or frame is None:
                with self._lock:
                    self._error = "camera returned no frame"
                time.sleep(0.05)
                continue
            with self._lock:
                self._latest_raw = frame.copy()
            capture_h, capture_w = frame.shape[:2]
            preview = self._process_frame(frame)
            ok, buf = cv2.imencode(
                ".jpg", preview, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
            )
            if not ok:
                continue
            now = time.monotonic()
            with self._lock:
                self._latest = buf.tobytes()
                self._w, self._h = capture_w, capture_h
                self._actual_preview_h, self._actual_preview_w = preview.shape[:2]
                if self._last_ts:
                    inst = 1.0 / max(1e-6, now - self._last_ts)
                    self._fps = inst if self._fps == 0 else 0.9 * self._fps + 0.1 * inst
                self._last_ts = now
                self._error = None

    def set_rotate(self, on):
        return None

    @staticmethod
    def _control_supported(cap, prop):
        """Best-effort OpenCV/UVC capability check without changing the value."""
        try:
            value = float(cap.get(prop))
            if not math.isfinite(value) or value < 0:
                return False, None
            return bool(cap.set(prop, value)), value
        except (cv2.error, TypeError, ValueError):
            return False, None

    def focus_controls(self):
        """Return autofocus/manual-focus state for the active UVC camera."""
        with self._lock:
            cap = self._cap
            if cap is None or not self._running:
                return {
                    "camera_open": False,
                    "camera_index": None,
                    "autofocus_supported": False,
                    "focus_supported": False,
                    "autofocus": None,
                    "focus": None,
                    "focus_min": 0,
                    "focus_max": 255,
                    "focus_step": 1,
                    "autoexposure_supported": False,
                    "exposure_supported": False,
                    "autoexposure": None,
                    "exposure": None,
                    "exposure_min": 0,
                    "exposure_max": 0,
                    "exposure_step": 1,
                }
            autofocus_supported, autofocus = self._control_supported(
                cap, cv2.CAP_PROP_AUTOFOCUS)
            focus_supported, focus = self._control_supported(cap, cv2.CAP_PROP_FOCUS)
            focus_min, focus_max, focus_step = 0, 255, 1
            control_backend = "opencv"
            native = self._native_focus_state
            if native and not (autofocus_supported and focus_supported):
                autofocus_supported = bool(native.get("autofocus_supported"))
                focus_supported = bool(native.get("focus_supported"))
                autofocus = native.get("autofocus")
                focus = native.get("focus")
                focus_min = native.get("focus_min", 0)
                focus_max = native.get("focus_max", 255)
                focus_step = native.get("focus_step", 1)
                control_backend = "native-focus-lock"
            autoexposure_supported = bool(
                native and native.get("autoexposure_supported"))
            exposure_supported = bool(native and native.get("exposure_supported"))
            return {
                "camera_open": True,
                "camera_index": self._index,
                "camera_name": (self._device_info or {}).get("name"),
                "autofocus_supported": autofocus_supported,
                "focus_supported": focus_supported,
                "autofocus": bool(round(autofocus)) if autofocus_supported else None,
                "focus": round(focus, 1) if focus_supported else None,
                "focus_min": focus_min,
                "focus_max": focus_max,
                "focus_step": focus_step,
                "autoexposure_supported": autoexposure_supported,
                "exposure_supported": exposure_supported,
                "autoexposure": native.get("autoexposure") if native else None,
                "exposure": native.get("exposure") if native else None,
                "exposure_min": native.get("exposure_min", 0) if native else 0,
                "exposure_max": native.get("exposure_max", 0) if native else 0,
                "exposure_step": native.get("exposure_step", 1) if native else 1,
                "control_backend": control_backend if (
                    autofocus_supported or focus_supported) else None,
                "uvc_helper_available": os.access(NATIVE_FOCUS_APP, os.X_OK),
                "uvc_error": self._native_focus_error,
            }

    def set_focus_controls(self, autofocus=None, focus=None,
                           autoexposure=None, exposure=None):
        """Apply supported UVC focus/exposure properties and return readback."""
        with self._lock:
            cap = self._cap
            if cap is None or not self._running:
                return False, "no camera is open", None
            index = self._index
            native_process = self._native_focus_process
            use_native = bool(self._native_focus_state and native_process)
        if use_native:
            try:
                native = None
                if autofocus is not None:
                    native = _native_focus_request(
                        native_process, "set-auto", 1 if autofocus else 0)
                if focus is not None:
                    native = _native_focus_request(
                        native_process, "set-focus", int(round(float(focus))))
                if autoexposure is not None:
                    native = _native_focus_request(
                        native_process, "set-autoexposure",
                        1 if autoexposure else 0)
                if exposure is not None:
                    native = _native_focus_request(
                        native_process, "set-exposure",
                        int(round(float(exposure))))
            except (RuntimeError, TypeError, ValueError) as exc:
                return False, str(exc), None
            saved_auto = (
                native.get("autofocus") if native else autofocus)
            saved_focus = native.get("focus") if native else focus
            saved_autoexposure = (
                native.get("autoexposure") if native else autoexposure)
            saved_exposure = native.get("exposure") if native else exposure
            _save_focus_settings(index, saved_auto, saved_focus,
                                 saved_autoexposure, saved_exposure)
            with self._lock:
                self._native_focus_state = native
            return True, None, self.focus_controls()

        with self._lock:
            cap = self._cap
            try:
                if autofocus is not None and not cap.set(
                        cv2.CAP_PROP_AUTOFOCUS, 1 if autofocus else 0):
                    return False, "this camera does not expose autofocus control", None
                if focus is not None and not cap.set(
                        cv2.CAP_PROP_FOCUS, float(focus)):
                    return False, "this camera does not expose manual focus control", None
            except (cv2.error, TypeError, ValueError):
                return False, "invalid or unsupported focus value", None
        controls = self.focus_controls()
        _save_focus_settings(index, controls.get("autofocus"), controls.get("focus"),
                             controls.get("autoexposure"), controls.get("exposure"))
        return True, None, controls

    def _process_frame(self, frame):
        """Detect ArUco markers, update the debounced tracker, and draw an
        overlay (green = tracked, amber = still qualifying)."""
        now = time.monotonic()
        period = 1.0 / self._detection_fps_limit
        if not self._last_detection_ts or now - self._last_detection_ts >= period:
            dets = _detect(frame, self._tracking_w, self._tracking_h)
            _tracker.update(dets, now)
            with self._lock:
                if self._last_detection_ts:
                    measured = 1.0 / max(1e-6, now - self._last_detection_ts)
                    self._detection_fps = (measured if not self._detection_fps else
                                           0.9 * self._detection_fps + 0.1 * measured)
                self._last_detection_ts = now
                self._overlay_detections = {
                    int(mid): {
                        "id": int(mid), "x": float(d["x"]), "y": float(d["y"]),
                        "rot": float(d["rot"]),
                        "corners": np.array(d["corners"], copy=True),
                    }
                    for mid, d in dets.items()
                }
                if dets:
                    self._last_detections = {
                        int(mid): {
                            "id": int(mid),
                            "x": float(d["x"]),
                            "y": float(d["y"]),
                            "nx": float(d["nx"]),
                            "ny": float(d["ny"]),
                            "rot": float(d["rot"]),
                            "corners": np.array(d["corners"], copy=True),
                        }
                        for mid, d in dets.items()
                    }
                    self._last_detections_at = now
                elif now - self._last_detections_at > DETECTION_HOLD_SECS:
                    self._last_detections = {}
        with self._lock:
            dets = {mid: {**d, "corners": np.array(d["corners"], copy=True)}
                    for mid, d in self._overlay_detections.items()}
        source_h, source_w = frame.shape[:2]
        preview_scale = min(1.0, self._preview_w / source_w,
                            self._preview_h / source_h)
        preview = cv2.resize(frame, None, fx=preview_scale, fy=preview_scale,
                             interpolation=cv2.INTER_AREA) if preview_scale < 1.0 else frame
        tracked = {t["id"] for t in _tracker.tags(now)}
        for mid, d in dets.items():
            color = (0, 220, 0) if mid in tracked else (0, 190, 255)  # BGR
            corners = (d["corners"] * preview_scale).astype(np.int32)
            cv2.polylines(preview, [corners], True, color, 2)
            cx, cy = int(d["x"] * preview_scale), int(d["y"] * preview_scale)
            cv2.putText(preview, f"#{mid}  {int(d['rot'])}deg", (cx + 8, cy - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
        return preview

    # -- readers --------------------------------------------------------------
    def latest_jpeg(self):
        with self._lock:
            return self._latest or self._no_signal

    def latest_frame(self):
        """Return a copy of the newest unannotated BGR frame, or None.

        Keeping camera ownership here lets sibling prototypes (notably Camera
        Calibration) do OpenCV work without fighting over the physical device.
        """
        with self._lock:
            return None if self._latest_raw is None else self._latest_raw.copy()

    def status(self):
        with self._lock:
            open_ = self._cap is not None and self._running
            tracking_scale = min(
                1.0,
                self._tracking_w / self._w if self._w else 1.0,
                self._tracking_h / self._h if self._h else 1.0,
            )
            return {
                "open": open_,
                "index": self._index,
                "width": self._w,
                "height": self._h,
                "requested_width": self._req_w,
                "requested_height": self._req_h,
                "preview_width": self._actual_preview_w or self._preview_w,
                "preview_height": self._actual_preview_h or self._preview_h,
                "tracking_width": round(self._w * tracking_scale) if self._w else 0,
                "tracking_height": round(self._h * tracking_scale) if self._h else 0,
                "requested_tracking_width": self._tracking_w,
                "requested_tracking_height": self._tracking_h,
                "detection_fps": round(self._detection_fps, 1),
                "detection_fps_limit": self._detection_fps_limit,
                "fps": round(self._fps, 1),
                "error": self._error,
                "rotate180": False,
                "backend": "AVFoundation" if sys.platform == "darwin"
                else ("DirectShow" if sys.platform.startswith("win") else "default"),
            }

    def detection_snapshot(self, now):
        """Held positions plus IDs present in the latest processed frame."""
        with self._lock:
            missing = now - self._last_detections_at
            if missing > DETECTION_HOLD_SECS:
                out = []
            else:
                tracked_ids = {item["id"] for item in _tracker.tags(now)}
                out = [{
                        "id": rec["id"],
                        "x": round(rec["x"], 1), "y": round(rec["y"], 1),
                        "nx": round(rec["nx"], 4), "ny": round(rec["ny"], 4),
                        "rotation": round(rec["rot"], 1),
                        "missing": round(missing, 2),
                        "tracked": rec["id"] in tracked_ids,
                        "corners": [
                            [round(float(x), 1), round(float(y), 1)]
                            for x, y in rec.get("corners", [])
                        ],
                    } for rec in self._last_detections.values()]
            out.sort(key=lambda t: t["id"])
            return out, sorted(self._overlay_detections)

    def detections(self, now):
        """Immediate reads from the same detections used to draw the MJPEG overlay."""
        return self.detection_snapshot(now)[0]

    def active_index(self):
        with self._lock:
            return self._index

    def mjpeg(self):
        """Generator yielding the latest frame as a multipart MJPEG stream."""
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
        period = 1.0 / STREAM_FPS
        while True:
            frame = self.latest_jpeg()
            yield boundary + frame + b"\r\n"
            time.sleep(period)


_mgr = CameraManager()
_tag_transformer = None
# Sampled state (camera fps + tracked tags change continuously), so the SSE
# stream re-snapshots on a short interval rather than on a bump. The live VIDEO
# is a separate MJPEG stream at /api/stream — this only pushes the status+tags
# panels. See prototypes/live.py.
_live = live.LiveState()


def set_tag_transformer(transformer):
    """Register an optional sibling-prototype transform for /api/tags."""
    global _tag_transformer
    _tag_transformer = transformer if callable(transformer) else None


# ---- camera enumeration ----------------------------------------------------
def probe_cameras(max_index=PROBE_MAX):
    """Probe indices 0..max_index-1 and report which ones open.

    The currently-active camera is reported from live state (not reopened, which
    would fight the grab thread). Probing is best-effort: missing indices may emit
    OpenCV warnings to stderr — that's normal."""
    active = _mgr.active_index()
    st = _mgr.status()
    device_names = _usb_camera_devices()
    found = []
    for i in range(max_index):
        device_name = (
            device_names[i]["name"] if i < len(device_names) else f"Camera {i}")
        if i == active:
            found.append({
                "index": i, "active": True,
                "width": st["width"], "height": st["height"],
                "name": device_name,
                "label": device_name + (f" · {st['width']}×{st['height']}"
                                        if st["width"] else "") + " (active)",
            })
            continue
        cap = cv2.VideoCapture(i, _backend())
        opened = cap.isOpened()
        w = h = 0
        if opened:
            ok, frame = cap.read()
            if frame is not None:
                h, w = frame.shape[:2]
        cap.release()
        if opened:
            found.append({
                "index": i, "active": False, "width": w, "height": h,
                "name": device_name,
                "label": device_name + (f" · {w}×{h}" if w else ""),
            })
    return found


# ---- persisted selection ---------------------------------------------------
def _load_settings():
    """Return the remembered {index, width, height} dict, or None."""
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        if data.get("index") is None:
            return None
        settings = {
            "index": int(data["index"]),
            "width": int(data.get("width") or DEFAULT_W),
            "height": int(data.get("height") or DEFAULT_H),
            "tracking_width": int(data.get("tracking_width") or
                                  data.get("width") or DEFAULT_W),
            "tracking_height": int(data.get("tracking_height") or
                                   data.get("height") or DEFAULT_H),
            "detection_fps": float(data.get("detection_fps") or
                                   DEFAULT_DETECTION_FPS),
        }
        if isinstance(data.get("autofocus"), bool):
            settings["autofocus"] = data["autofocus"]
        if isinstance(data.get("autoexposure"), bool):
            settings["autoexposure"] = data["autoexposure"]
        try:
            if data.get("focus") is not None:
                settings["focus"] = float(data["focus"])
            if data.get("exposure") is not None:
                settings["exposure"] = float(data["exposure"])
        except (ValueError, TypeError):
            pass
        return settings
    except (OSError, ValueError, TypeError):
        return None


def _save_settings(index=None, width=None, height=None, tracking_width=None,
                   tracking_height=None, detection_fps=None):
    """Merge the given fields into camera-settings.json."""
    cur = {}
    try:
        with open(SETTINGS_PATH) as f:
            cur = json.load(f)
    except (OSError, ValueError, TypeError):
        cur = {}
    if index is not None:
        cur["index"] = index
    if width:
        cur["width"] = width
    if height:
        cur["height"] = height
    if tracking_width:
        cur["tracking_width"] = tracking_width
    if tracking_height:
        cur["tracking_height"] = tracking_height
    if detection_fps is not None:
        cur["detection_fps"] = detection_fps
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump(cur, f)
    except OSError:
        pass


def _save_focus_settings(index, autofocus, focus,
                         autoexposure=None, exposure=None):
    """Persist the last accepted optical controls for the selected camera."""
    try:
        with open(SETTINGS_PATH) as f:
            cur = json.load(f)
    except (OSError, ValueError, TypeError):
        cur = {}
    cur["index"] = index
    if isinstance(autofocus, bool):
        cur["autofocus"] = autofocus
    if focus is not None:
        cur["focus"] = focus
    if isinstance(autoexposure, bool):
        cur["autoexposure"] = autoexposure
    if exposure is not None:
        cur["exposure"] = exposure
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump(cur, f)
    except OSError:
        pass


# ---- pages -----------------------------------------------------------------
@bp.route("/")
@bp.route("/controller")
def controller():
    return send_from_directory(HERE, "controller.html")


@bp.route("/view")
def view():
    return send_from_directory(HERE, "view.html")


# ---- API -------------------------------------------------------------------
@bp.route("/api/cameras")
def api_cameras():
    """List available cameras (probed), the resolution options, and the
    remembered selection."""
    return jsonify({
        "cameras": probe_cameras(),
        "resolutions": RESOLUTIONS,
        "remembered": _load_settings(),
    })


@bp.route("/api/status")
def api_status():
    st = _mgr.status()
    st["remembered"] = _load_settings()
    return jsonify(st)


def _events_dict():
    st = _mgr.status()
    st["remembered"] = _load_settings()
    now = time.monotonic()
    return {
        "status": st,
        "tags": _tracker.tags(now),
        "detections": _mgr.detections(now),
    }


@bp.route("/api/events")
def api_events():
    """Push the camera status + tracked tags ~3x/s while they change. (The live
    video is the separate MJPEG /api/stream.)"""
    return _live.stream(_events_dict, interval=0.3)


@bp.route("/api/tags")
def api_tags():
    """The debounced list of tracked ArUco tags: id, x, y (pixels), nx/ny
    (normalised 0..1), rotation (degrees), and how long it's been `missing`."""
    now = time.monotonic()
    tags = _tracker.tags(now)
    detections, visible_ids = _mgr.detection_snapshot(now)
    corrected = False
    if request.args.get("space") == "corrected" and _tag_transformer is not None:
        try:
            tags, detections, corrected = _tag_transformer(tags, detections)
        except Exception:
            # Detection must remain available even if a saved lens model is bad.
            corrected = False
    return jsonify({
        "tags": tags,
        "detections": detections,
        "visible_ids": visible_ids,
        "corrected": corrected,
        "width": _mgr.status()["width"],
        "height": _mgr.status()["height"],
        "dict": "DICT_4X4_50",
        "additional_dicts": ["DICT_4X4_100:50-55", "ATOM_SCREEN_3X3:100-105"],
        "promote_secs": PROMOTE_SECS,
        "drop_secs": DROP_SECS,
        "detection_hold_secs": DETECTION_HOLD_SECS,
    })


@bp.route("/api/select", methods=["POST"])
def api_select():
    """Open a camera with independent preview and ArUco tracking settings."""
    data = request.get_json(silent=True) or {}
    try:
        index = int(data["index"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "expected integer 'index'"}), 400
    remembered = _load_settings() or {}
    try:
        width = int(data.get("width") or remembered.get("width") or DEFAULT_W)
        height = int(data.get("height") or remembered.get("height") or DEFAULT_H)
        tracking_width = int(data.get("tracking_width") or
                             remembered.get("tracking_width") or width)
        tracking_height = int(data.get("tracking_height") or
                              remembered.get("tracking_height") or height)
        detection_fps = float(data.get("detection_fps") or
                              remembered.get("detection_fps") or
                              DEFAULT_DETECTION_FPS)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "invalid resolution or FPS"}), 400
    allowed = {(item["w"], item["h"]) for item in RESOLUTIONS}
    if (width, height) not in allowed or (tracking_width, tracking_height) not in allowed:
        return jsonify({"ok": False, "error": "unsupported resolution selection"}), 400
    if not 1 <= detection_fps <= 30:
        return jsonify({"ok": False, "error": "detection FPS must be between 1 and 30"}), 400
    ok, err = _mgr.open(index, width, height, tracking_width, tracking_height,
                        detection_fps)
    if not ok:
        return jsonify({"ok": False, "error": err}), 502
    return jsonify({"ok": True, "status": _mgr.status()})


@bp.route("/api/rotate", methods=["POST"])
def api_rotate():
    """Deprecated: source frames stay unrotated so tag tracking remains stable."""
    _mgr.set_rotate(False)
    return jsonify({"ok": True, "rotate180": False})


@bp.route("/api/stop", methods=["POST"])
def api_stop():
    """Release the camera (the stream falls back to a 'no signal' frame)."""
    _mgr.close()
    return jsonify({"ok": True})


@bp.route("/api/stream")
def api_stream():
    """Live MJPEG video. If a camera was remembered but none is open yet, open it
    so just viewing the page brings the feed up."""
    if _mgr.active_index() is None:
        remembered = _load_settings()
        if remembered is not None:
            _mgr.open(remembered["index"], remembered["width"],
                      remembered["height"], remembered["tracking_width"],
                      remembered["tracking_height"], remembered["detection_fps"])
    return Response(_mgr.mjpeg(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")
