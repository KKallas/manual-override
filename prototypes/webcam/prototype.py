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
import os
import sys
import threading
import time

import cv2
import numpy as np
from flask import Blueprint, Response, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))

MANIFEST = {
    "name": "Webcam",
    "description": "Live OpenCV camera feed with a camera-selection dropdown. The "
                   "eyes for the perception stage — ArUco reading builds on this. "
                   "Needs opencv-python (see the prototype's requirements.txt).",
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
REQUEST_W = 1280       # resolution we ask the camera for (it may pick another)
REQUEST_H = 720
SETTINGS_PATH = os.path.join(HERE, "camera-settings.json")


def _backend():
    """Pick a sensible OpenCV capture backend per platform."""
    if sys.platform == "darwin":
        return cv2.CAP_AVFOUNDATION
    if sys.platform.startswith("win"):
        return cv2.CAP_DSHOW
    return cv2.CAP_ANY


def _placeholder(text):
    """A grey 'no signal' frame, JPEG-encoded, for when no camera is open."""
    img = np.full((REQUEST_H, REQUEST_W, 3), 18, np.uint8)
    cv2.putText(img, text, (40, REQUEST_H // 2),
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
        self._thread = None
        self._running = False
        self._latest = None        # latest JPEG bytes
        self._w = 0
        self._h = 0
        self._fps = 0.0            # measured capture fps (EMA)
        self._last_ts = 0.0
        self._error = None         # last open/read error, for the UI
        self._no_signal = _placeholder("No camera selected")

    # -- lifecycle ------------------------------------------------------------
    def open(self, index):
        """Open camera `index`, replacing any current one. Returns (ok, error)."""
        with self._lock:
            self._stop_locked()
            cap = cv2.VideoCapture(index, _backend())
            if not cap.isOpened():
                cap.release()
                self._error = f"could not open camera {index}"
                return False, self._error
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, REQUEST_W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, REQUEST_H)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # lower latency; not always honoured
            except cv2.error:
                pass
            self._cap = cap
            self._index = index
            self._error = None
            self._latest = None
            self._fps = 0.0
            self._last_ts = 0.0
            self._running = True
            self._thread = threading.Thread(
                target=self._grab_loop, name=f"webcam-grab-{index}", daemon=True
            )
            self._thread.start()
        _save_settings(index)
        return True, None

    def close(self):
        with self._lock:
            self._stop_locked()
            self._index = None

    def _stop_locked(self):
        """Stop the grab thread and release the capture. Call with the lock held."""
        self._running = False
        thread = self._thread
        cap = self._cap
        self._thread = None
        self._cap = None
        self._latest = None
        # release the lock while joining so the grab loop can exit its read()
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            self._lock.release()
            try:
                thread.join(timeout=2.0)
            finally:
                self._lock.acquire()
        if cap is not None:
            cap.release()

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
            frame = self._process_frame(frame)
            ok, buf = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
            )
            if not ok:
                continue
            now = time.monotonic()
            with self._lock:
                self._latest = buf.tobytes()
                self._h, self._w = frame.shape[:2]
                if self._last_ts:
                    inst = 1.0 / max(1e-6, now - self._last_ts)
                    self._fps = inst if self._fps == 0 else 0.9 * self._fps + 0.1 * inst
                self._last_ts = now
                self._error = None

    def _process_frame(self, frame):
        """Hook for per-frame work before encoding. Today: identity.

        ArUco detection lands here later — detect markers on `frame`, draw the
        overlay, and stash the world/pixel positions for an /api/markers route."""
        return frame

    # -- readers --------------------------------------------------------------
    def latest_jpeg(self):
        with self._lock:
            return self._latest or self._no_signal

    def status(self):
        with self._lock:
            open_ = self._cap is not None and self._running
            return {
                "open": open_,
                "index": self._index,
                "width": self._w,
                "height": self._h,
                "fps": round(self._fps, 1),
                "error": self._error,
                "backend": "AVFoundation" if sys.platform == "darwin"
                else ("DirectShow" if sys.platform.startswith("win") else "default"),
            }

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


# ---- camera enumeration ----------------------------------------------------
def probe_cameras(max_index=PROBE_MAX):
    """Probe indices 0..max_index-1 and report which ones open.

    The currently-active camera is reported from live state (not reopened, which
    would fight the grab thread). Probing is best-effort: missing indices may emit
    OpenCV warnings to stderr — that's normal."""
    active = _mgr.active_index()
    st = _mgr.status()
    found = []
    for i in range(max_index):
        if i == active:
            found.append({
                "index": i, "active": True,
                "width": st["width"], "height": st["height"],
                "label": f"Camera {i}" + (f" · {st['width']}×{st['height']}"
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
                "label": f"Camera {i}" + (f" · {w}×{h}" if w else ""),
            })
    return found


# ---- persisted selection ---------------------------------------------------
def _load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            idx = json.load(f).get("index")
            return int(idx) if idx is not None else None
    except (OSError, ValueError, TypeError):
        return None


def _save_settings(index):
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump({"index": index}, f)
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
    """List available cameras (probed) plus the remembered selection."""
    return jsonify({"cameras": probe_cameras(), "remembered": _load_settings()})


@bp.route("/api/status")
def api_status():
    st = _mgr.status()
    st["remembered"] = _load_settings()
    return jsonify(st)


@bp.route("/api/select", methods=["POST"])
def api_select():
    """Open a camera by index (and remember it)."""
    data = request.get_json(silent=True) or {}
    try:
        index = int(data["index"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "expected integer 'index'"}), 400
    ok, err = _mgr.open(index)
    if not ok:
        return jsonify({"ok": False, "error": err}), 502
    return jsonify({"ok": True, "status": _mgr.status()})


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
            _mgr.open(remembered)
    return Response(_mgr.mjpeg(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")
