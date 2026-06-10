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
                   "tags, plus the Atom-screen 3x3 set as ids 100-105): a debounced "
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
DETECT_MAX_W = 960       # downscale wide frames for fast detection (coords rescaled)

_aruco_dict = cv2.aruco.getPredefinedDictionary(TAG_DICT)
_detector = cv2.aruco.ArucoDetector(_aruco_dict, cv2.aruco.DetectorParameters())

# Second, coarse dictionary used by the Atom screens: atom-manager renders markers
# with cv2.aruco.extendDictionary(6, 3); we build it identically here so we can read
# them. Their ids are shifted by SCREEN_BASE so they never collide with the printed
# DICT_4X4_50 tags (corners 1-4, head 10, ...): screen marker N appears as 100 + N.
SCREEN_NMARKERS = 6
SCREEN_BITS = 3
SCREEN_BASE = 100
_screen_dict = cv2.aruco.extendDictionary(SCREEN_NMARKERS, SCREEN_BITS)
_screen_detector = cv2.aruco.ArucoDetector(_screen_dict, cv2.aruco.DetectorParameters())


def _collect(dets, corners, ids, scale, w, h, id_offset=0):
    """Fold detectMarkers() output into `dets` in full-res coords; ids are shifted
    by `id_offset` so markers from different dictionaries don't clash."""
    if ids is None:
        return
    for c, mid in zip(corners, ids.flatten()):
        pts = c.reshape(4, 2) / scale            # back to full-res pixels
        cx, cy = pts.mean(axis=0)
        dx, dy = pts[1] - pts[0]                  # top edge -> orientation
        dets[id_offset + int(mid)] = {
            "x": float(cx), "y": float(cy),
            "nx": float(cx / w), "ny": float(cy / h),
            "rot": float(math.degrees(math.atan2(dy, dx))),
            "corners": pts,
        }


def _detect(frame):
    """Detect markers from BOTH dictionaries; return {id: {x, y, nx, ny, rot,
    corners}} in full-res pixel coords. Printed DICT_4X4_50 tags keep their ids;
    Atom-screen (3x3) markers come back as SCREEN_BASE + id (100..105)."""
    h, w = frame.shape[:2]
    scale = DETECT_MAX_W / w if w > DETECT_MAX_W else 1.0
    small = cv2.resize(frame, None, fx=scale, fy=scale) if scale != 1.0 else frame
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    dets = {}
    c, i, _ = _detector.detectMarkers(gray)
    _collect(dets, c, i, scale, w, h)                       # printed 4x4 tags
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
                if not rec["tracked"] and (now - rec["first_seen"]) >= PROMOTE_SECS:
                    rec["tracked"] = True
                    rec["tracked_since"] = now
            for mid in list(self._tags):
                rec = self._tags[mid]
                age = now - rec["last_seen"]
                if (rec["tracked"] and age > DROP_SECS) or (not rec["tracked"] and age > STREAK_GRACE):
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
        self._thread = None
        self._running = False
        self._latest = None        # latest JPEG bytes
        self._w = 0                # actual capture width
        self._h = 0                # actual capture height
        self._req_w = 0            # requested width
        self._req_h = 0            # requested height
        self._fps = 0.0            # measured capture fps (EMA)
        self._last_ts = 0.0
        self._error = None         # last open/read error, for the UI
        self._rotate = False       # rotate every frame 180° (display + detection)
        self._no_signal = _placeholder("No camera selected")

    # -- lifecycle ------------------------------------------------------------
    def open(self, index, width=None, height=None):
        """Open camera `index` at a requested resolution, replacing any current
        one. The camera picks the closest mode it supports; status reports what
        was actually achieved. Returns (ok, error)."""
        width = int(width or DEFAULT_W)
        height = int(height or DEFAULT_H)
        with self._lock:
            self._stop_locked()
            cap = cv2.VideoCapture(index, _backend())
            if not cap.isOpened():
                cap.release()
                self._error = f"could not open camera {index}"
                return False, self._error
            # Request MJPG first: most UVC webcams only expose 1080p+/4K and
            # higher frame rates through the MJPG codec, not raw (YUY2) frames.
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            except (cv2.error, AttributeError):
                pass
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # lower latency; not always honoured
            except cv2.error:
                pass
            self._cap = cap
            self._index = index
            self._req_w, self._req_h = width, height
            self._error = None
            self._latest = None
            self._fps = 0.0
            self._last_ts = 0.0
            self._running = True
            self._thread = threading.Thread(
                target=self._grab_loop, name=f"webcam-grab-{index}", daemon=True
            )
            self._thread.start()
        _save_settings(index, width, height)
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

    def set_rotate(self, on):
        with self._lock:
            self._rotate = bool(on)
        _save_settings(rotate=self._rotate)

    def _process_frame(self, frame):
        """Detect ArUco markers, update the debounced tracker, and draw an
        overlay (green = tracked, amber = still qualifying)."""
        if self._rotate:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        now = time.monotonic()
        dets = _detect(frame)
        _tracker.update(dets, now)
        tracked = {t["id"] for t in _tracker.tags(now)}
        for mid, d in dets.items():
            color = (0, 220, 0) if mid in tracked else (0, 190, 255)  # BGR
            cv2.polylines(frame, [d["corners"].astype(np.int32)], True, color, 2)
            cx, cy = int(d["x"]), int(d["y"])
            cv2.putText(frame, f"#{mid}  {int(d['rot'])}deg", (cx + 8, cy - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
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
                "requested_width": self._req_w,
                "requested_height": self._req_h,
                "fps": round(self._fps, 1),
                "error": self._error,
                "rotate180": self._rotate,
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
# Sampled state (camera fps + tracked tags change continuously), so the SSE
# stream re-snapshots on a short interval rather than on a bump. The live VIDEO
# is a separate MJPEG stream at /api/stream — this only pushes the status+tags
# panels. See prototypes/live.py.
_live = live.LiveState()


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
    """Return the remembered {index, width, height} dict, or None."""
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        if data.get("index") is None:
            return None
        return {
            "index": int(data["index"]),
            "width": int(data.get("width") or DEFAULT_W),
            "height": int(data.get("height") or DEFAULT_H),
        }
    except (OSError, ValueError, TypeError):
        return None


def _save_settings(index=None, width=None, height=None, rotate=None):
    """Merge the given fields into camera-settings.json (so e.g. toggling rotate
    doesn't forget the remembered camera, and vice versa)."""
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
    if rotate is not None:
        cur["rotate180"] = bool(rotate)
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump(cur, f)
    except OSError:
        pass


def _load_rotate():
    try:
        with open(SETTINGS_PATH) as f:
            return bool(json.load(f).get("rotate180", False))
    except (OSError, ValueError, TypeError):
        return False


_mgr._rotate = _load_rotate()   # restore the remembered rotation


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
    return {"status": st, "tags": _tracker.tags(time.monotonic())}


@bp.route("/api/events")
def api_events():
    """Push the camera status + tracked tags ~3x/s while they change. (The live
    video is the separate MJPEG /api/stream.)"""
    return _live.stream(_events_dict, interval=0.3)


@bp.route("/api/tags")
def api_tags():
    """The debounced list of tracked ArUco tags: id, x, y (pixels), nx/ny
    (normalised 0..1), rotation (degrees), and how long it's been `missing`."""
    return jsonify({
        "tags": _tracker.tags(time.monotonic()),
        "dict": "DICT_4X4_50",
        "promote_secs": PROMOTE_SECS,
        "drop_secs": DROP_SECS,
    })


@bp.route("/api/select", methods=["POST"])
def api_select():
    """Open a camera by index at an optional width/height (and remember both).
    If width/height are omitted the remembered or default resolution is used."""
    data = request.get_json(silent=True) or {}
    try:
        index = int(data["index"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "expected integer 'index'"}), 400
    remembered = _load_settings() or {}
    try:
        width = int(data.get("width") or remembered.get("width") or DEFAULT_W)
        height = int(data.get("height") or remembered.get("height") or DEFAULT_H)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "width/height must be integers"}), 400
    ok, err = _mgr.open(index, width, height)
    if not ok:
        return jsonify({"ok": False, "error": err}), 502
    return jsonify({"ok": True, "status": _mgr.status()})


@bp.route("/api/rotate", methods=["POST"])
def api_rotate():
    """Toggle 180° rotation of the feed (display + detection). Persisted."""
    on = bool((request.get_json(silent=True) or {}).get("on"))
    _mgr.set_rotate(on)
    return jsonify({"ok": True, "rotate180": on})


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
            _mgr.open(remembered["index"], remembered["width"], remembered["height"])
    return Response(_mgr.mjpeg(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")
