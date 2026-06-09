"""
Atom Manager — manage attached M5Stack AtomS3R devices over USB serial.

Part 1: discover connected serial devices (more than one may be attached) and,
per device, flash the atom-image-server firmware, push WiFi credentials, and read
back the DHCP IP. Flashing shells out to PlatformIO (`pio run -t upload`) against
the ESP/atom-image-server project; WiFi/IP talk to that firmware's serial console.

Loaded by hub.py; registered under /p/atom-manager.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, jsonify, request, send_from_directory

# pyserial is optional: without it the prototype loads but can only explain that
# it needs `pip install pyserial`.
try:
    import serial
    from serial.tools import list_ports
    _HAS_SERIAL = True
except Exception:
    _HAS_SERIAL = False

# requests is used to poll units and push ArUco frames over the network.
try:
    import requests
    _HAS_REQUESTS = True
except Exception:
    _HAS_REQUESTS = False

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))                 # prototypes/.. = repo root
ESP_PROJECT = os.path.join(REPO_ROOT, "ESP", "atom-image-server")  # firmware to flash
PIO = shutil.which("pio") or os.path.expanduser("~/.platformio/penv/bin/pio")

ATOM_VID = 0x303A    # Espressif native-USB / USB-Serial-JTAG vendor id
BAUD = 115200

MANIFEST = {
    "name": "Atom Manager",
    "description": "Discover attached AtomS3R devices over serial; flash firmware, "
                   "set WiFi, and read the IP. Supports several devices at once.",
    "default_page": "controller",
    "pages": [{"path": "controller", "label": "Controller"}],
}
bp = Blueprint("atom_manager", __name__)

# Flash jobs (one per port) run in background threads; the UI polls their log.
_jobs = {}                       # port -> {"status": running|done|error, "log": [str], "rc": int|None}
_jobs_lock = threading.Lock()
# A lock per port so a WiFi/IP serial command can't open a port mid-use.
_port_locks = {}
_port_locks_guard = threading.Lock()


def _port_lock(port):
    with _port_locks_guard:
        return _port_locks.setdefault(port, threading.Lock())


def _is_flashing(port):
    with _jobs_lock:
        j = _jobs.get(port)
        return bool(j and j["status"] == "running")


# ---- pages -----------------------------------------------------------------
@bp.route("/")
@bp.route("/controller")
def controller():
    return send_from_directory(HERE, "controller.html")


# ---- device discovery ------------------------------------------------------
@bp.route("/api/devices")
def devices():
    if not _HAS_SERIAL:
        return jsonify({"ok": False, "error": "pyserial not installed (pip install pyserial)",
                        "devices": []})
    out = []
    for p in list_ports.comports():
        dev = p.device or ""
        # hide macOS internal ports that are never our devices
        if dev.endswith("debug-console") or "Bluetooth" in dev:
            continue
        is_atom = p.vid == ATOM_VID or "usbmodem" in dev or "usbserial" in dev
        out.append({
            "port": dev,
            "description": p.description or "",
            "vid": f"{p.vid:04x}" if p.vid else None,
            "pid": f"{p.pid:04x}" if p.pid else None,
            "is_atom": is_atom,
            "flashing": _is_flashing(dev),
        })
    # likely-Atom devices first
    out.sort(key=lambda d: (not d["is_atom"], d["port"]))
    return jsonify({"ok": True, "devices": out})


# ---- serial console helpers ------------------------------------------------
_IP_RE = re.compile(r"IP (\d{1,3}(?:\.\d{1,3}){3})")


def _extract_ip(lines):
    for s in lines:
        m = _IP_RE.search(s)
        if m:
            return m.group(1)
    return None


def _serial_cmd(port, cmd, stop=None, settle=0.3, timeout=6.0):
    """Open the port, send one console line, collect reply lines until `stop`
    matches or `timeout`. Returns the list of received lines."""
    ser = serial.Serial(port, BAUD, timeout=0.1)
    try:
        time.sleep(settle)            # let the USB-CDC port settle
        ser.reset_input_buffer()
        ser.write((cmd + "\n").encode())
        ser.flush()
        lines, buf, deadline = [], b"", time.time() + timeout
        while time.time() < deadline:
            data = ser.read(256)
            if not data:
                time.sleep(0.05)
                continue
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                s = raw.decode("utf-8", "replace").rstrip("\r")
                lines.append(s)
                if stop and stop(s):
                    return lines
        return lines
    finally:
        ser.close()


def _precheck(port):
    if not _HAS_SERIAL:
        return jsonify({"ok": False, "error": "pyserial not installed"}), 503
    if not port:
        return jsonify({"ok": False, "error": "no port given"}), 400
    if _is_flashing(port):
        return jsonify({"ok": False, "error": "device is flashing"}), 409
    return None


@bp.route("/api/ip", methods=["POST"])
def api_ip():
    port = (request.get_json(silent=True) or {}).get("port")
    err = _precheck(port)
    if err:
        return err
    lock = _port_lock(port)
    if not lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "port busy"}), 409
    try:
        lines = _serial_cmd(port, "ip",
                            stop=lambda s: s.startswith("IP ") or "not connected" in s,
                            timeout=5.0)
    except Exception as e:
        return jsonify({"ok": False, "error": f"serial: {e}"}), 502
    finally:
        lock.release()
    return jsonify({"ok": True, "ip": _extract_ip(lines), "output": "\n".join(lines)})


@bp.route("/api/wifi", methods=["POST"])
def api_wifi():
    """Push the SAVED WiFi credentials to the device over serial. The creds live
    server-side (see /api/wifi-creds); the password is never sent back to the UI."""
    port = (request.get_json(silent=True) or {}).get("port")
    err = _precheck(port)
    if err:
        return err
    ssid = _wifi.get("ssid", "")
    password = _wifi.get("password", "")
    if not ssid:
        return jsonify({"ok": False, "error": "set WiFi credentials first"}), 400
    lock = _port_lock(port)
    if not lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "port busy"}), 409
    try:
        lines = _serial_cmd(port, f"wifi {ssid}:{password}",
                            stop=lambda s: s.startswith("IP ") or "timed out" in s,
                            timeout=25.0)
    except Exception as e:
        return jsonify({"ok": False, "error": f"serial: {e}"}), 502
    finally:
        lock.release()
    ip = _extract_ip(lines)
    return jsonify({"ok": True, "ip": ip, "joined": ip is not None, "output": "\n".join(lines)})


# ---- flashing (background) -------------------------------------------------
def _flash_worker(port):
    job = _jobs[port]
    cmd = [PIO, "run", "-e", "atoms3r", "-t", "upload", "--upload-port", port]
    job["log"].append("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, cwd=ESP_PROJECT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
    except Exception as e:
        job["log"].append(f"failed to start pio: {e}")
        job["status"] = "error"
        job["rc"] = -1
        return
    for line in proc.stdout:
        job["log"].append(line.rstrip("\n"))
    job["rc"] = proc.wait()
    job["status"] = "done" if job["rc"] == 0 else "error"


@bp.route("/api/flash", methods=["POST"])
def api_flash():
    port = (request.get_json(silent=True) or {}).get("port")
    if not port:
        return jsonify({"ok": False, "error": "no port given"}), 400
    if not os.path.isdir(ESP_PROJECT):
        return jsonify({"ok": False, "error": f"firmware project not found at {ESP_PROJECT}"}), 500
    if not (PIO and (shutil.which(PIO) or os.path.exists(PIO))):
        return jsonify({"ok": False, "error": "PlatformIO (pio) not found on PATH"}), 500
    with _jobs_lock:
        if _jobs.get(port, {}).get("status") == "running":
            return jsonify({"ok": False, "error": "already flashing"}), 409
        _jobs[port] = {"status": "running", "log": [], "rc": None}
    threading.Thread(target=_flash_worker, args=(port,), daemon=True).start()
    return jsonify({"ok": True, "started": True})


@bp.route("/api/flash-status")
def api_flash_status():
    port = request.args.get("port")
    with _jobs_lock:
        j = _jobs.get(port)
    if not j:
        return jsonify({"status": "idle", "log": "", "rc": None})
    return jsonify({"status": j["status"], "rc": j["rc"], "log": "\n".join(j["log"])})


# ---- saved WiFi credentials (persisted; password never returned to the UI) --
WIFI_PATH = os.path.join(HERE, "wifi.json")
_wifi = {"ssid": "", "password": ""}


def _load_wifi():
    try:
        data = json.load(open(WIFI_PATH))
        _wifi["ssid"] = str(data.get("ssid", "") or "")
        _wifi["password"] = str(data.get("password", "") or "")
    except (OSError, ValueError, TypeError):
        pass


def _save_wifi():
    try:
        with open(WIFI_PATH, "w") as f:
            json.dump(_wifi, f, indent=2)
    except OSError:
        pass


@bp.route("/api/wifi-creds", methods=["GET"])
def get_wifi_creds():
    """SSID + whether a password is stored — but NEVER the password itself."""
    return jsonify({"ssid": _wifi["ssid"], "has_password": bool(_wifi["password"])})


@bp.route("/api/wifi-creds", methods=["POST"])
def set_wifi_creds():
    """Save SSID (always) and password (only if a non-empty one is supplied, so a
    blank field keeps the stored password instead of wiping it)."""
    data = request.get_json(silent=True) or {}
    _wifi["ssid"] = (data.get("ssid") or "").strip()
    if data.get("password"):
        _wifi["password"] = data["password"]
    _save_wifi()
    return jsonify({"ok": True, "ssid": _wifi["ssid"], "has_password": bool(_wifi["password"])})


# ---- units: 10 IP slots, polled for online/offline, push ArUco to one -------
N_UNITS = 10
UNITS_PATH = os.path.join(HERE, "units.json")
POLL_SECS = 15
_units = [{"ip": None, "online": False, "last_seen": None, "mid": 0, "bri": 255,
           "col": "#ffffff", "bat": None}
          for _ in range(N_UNITS)]
_units_lock = threading.Lock()
_poller_started = False


_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def _clean_ip(s):
    """Pull a clean host out of whatever was typed/pasted — tolerate an 'ip '
    prefix, surrounding spaces, or a trailing URL. Returns None if blank."""
    s = (s or "").strip()
    if not s:
        return None
    m = _IPV4.search(s)
    return m.group(0) if m else s


def _load_units():
    try:
        data = json.load(open(UNITS_PATH)) or {}
    except (OSError, ValueError, TypeError):
        return
    items = data.get("units")
    if isinstance(items, list):                       # current format: per-unit dicts
        for i in range(N_UNITS):
            if i < len(items) and isinstance(items[i], dict):
                u = items[i]
                _units[i]["ip"] = _clean_ip(u.get("ip"))
                try:
                    _units[i]["mid"] = int(u.get("mid", 0)) % ARUCO_NMARKERS
                except (TypeError, ValueError):
                    pass
                try:
                    _units[i]["bri"] = max(0, min(255, int(u.get("bri", 255))))
                except (TypeError, ValueError):
                    pass
                if "col" in u:
                    _units[i]["col"] = _hex_color(_parse_color(u.get("col")))
    else:                                             # legacy format: {"ips": [...]}
        for i, ip in enumerate((data.get("ips") or [])[:N_UNITS]):
            _units[i]["ip"] = _clean_ip(ip)
        _save_units()                                 # migrate to the new format


def _save_units():
    try:
        with open(UNITS_PATH, "w") as f:
            json.dump({"units": [{"ip": u["ip"], "mid": u["mid"], "bri": u["bri"],
                                  "col": u["col"]}
                                 for u in _units]}, f, indent=2)
    except OSError:
        pass


def _poll_one(i):
    ip = _units[i]["ip"]
    if not ip or not _HAS_REQUESTS:
        _units[i]["online"] = False
        _units[i]["bat"] = None
        return
    bat = None
    try:
        # GET /state reports marker id + battery; the device is the source of
        # truth for what it's currently showing. Older firmware without /state
        # still answers (404 < 500) so it just reads as online with no battery.
        resp = requests.get(f"http://{ip}/state", timeout=2)
        ok = resp.status_code < 500
        if resp.ok:
            st = resp.json()
            b = st.get("battery") or {}
            if b.get("mv") is not None:
                pct = b.get("pct")
                bat = {"mv": int(b["mv"]), "pct": int(pct) if pct is not None else None}
            mid = st.get("markerId")
            if isinstance(mid, int) and mid >= 0:
                _units[i]["mid"] = mid % ARUCO_NMARKERS
    except Exception:
        ok = False
    _units[i]["online"] = ok
    _units[i]["bat"] = bat if ok else None
    if ok:
        _units[i]["last_seen"] = time.time()


def _start_poller():
    """Background loop: probe every unit's IP concurrently every POLL_SECS."""
    global _poller_started
    if _poller_started:
        return
    _poller_started = True

    def loop():
        while True:
            try:
                with ThreadPoolExecutor(max_workers=N_UNITS) as ex:
                    list(ex.map(_poll_one, range(N_UNITS)))
            except Exception:
                pass
            time.sleep(POLL_SECS)

    threading.Thread(target=loop, daemon=True).start()


@bp.route("/api/units")
def api_units():
    now = time.time()
    with _units_lock:
        return jsonify({"units": [
            {"id": i, "ip": u["ip"], "online": u["online"],
             "ago": round(now - u["last_seen"], 1) if u["last_seen"] else None,
             "mid": u["mid"], "bri": u["bri"], "col": u["col"], "bat": u["bat"]}
            for i, u in enumerate(_units)]})


@bp.route("/api/units/<int:i>", methods=["PATCH"])
def set_unit(i):
    if not 0 <= i < N_UNITS:
        return jsonify({"ok": False, "error": "bad unit"}), 404
    ip = _clean_ip((request.get_json(silent=True) or {}).get("ip"))
    with _units_lock:
        _units[i]["ip"] = ip
        _units[i]["online"] = False
        _units[i]["last_seen"] = None
        _save_units()
    threading.Thread(target=_poll_one, args=(i,), daemon=True).start()   # probe it now
    return jsonify({"ok": True, "id": i, "ip": _units[i]["ip"]})


# A small, coarse fiducial set tuned for an emissive panel seen under bloom:
# 6 markers at 3x3 bits (bigger modules than 4x4), maximally distinct under
# rotation so orientation is still recovered. NOTE: the *reader* must use this
# same dictionary — build it identically with cv2.aruco.extendDictionary(6, 3).
ARUCO_NMARKERS = 6
ARUCO_BITS = 3
_custom_dict = None


def _aruco_dict():
    global _custom_dict
    if _custom_dict is None:
        import cv2
        _custom_dict = cv2.aruco.extendDictionary(ARUCO_NMARKERS, ARUCO_BITS)
    return _custom_dict


def _parse_color(s, default=(255, 255, 255)):
    """Accept a '#rrggbb' string or an [r,g,b] list and return an (r,g,b) tuple."""
    if isinstance(s, (list, tuple)) and len(s) == 3:
        try:
            return tuple(max(0, min(255, int(c))) for c in s)
        except (TypeError, ValueError):
            return default
    if isinstance(s, str):
        h = s.strip().lstrip("#")
        if len(h) == 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                return default
    return default


def _hex_color(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def _make_aruco_frame(marker_id, border=4, brightness=255, color=(255, 255, 255)):
    """128x128 RGB565 (big-endian) image of custom-dictionary marker `marker_id`
    (0..5). The marker's lit (white) pixels are tinted to `color` (an (r,g,b)
    tuple, default white); brightness 0-255 then scales the whole image (the
    panel is emissive, so this dims toward black — 255 = full, 0 = off)."""
    import cv2
    import numpy as np
    d = _aruco_dict()
    side = 128 - 2 * border
    marker = cv2.aruco.generateImageMarker(d, int(marker_id) % ARUCO_NMARKERS, side)
    canvas = np.full((128, 128), 255, np.uint8)
    canvas[border:border + side, border:border + side] = marker

    # Lit fraction per pixel (0 black .. 1 white) tinted by color and dimmed by
    # brightness, then packed to RGB565 big-endian (the byte order the firmware
    # decodes as swap565_t).
    br = max(0, min(255, int(brightness))) / 255.0
    r, g, b = (max(0, min(255, int(c))) for c in color)
    lit = canvas.astype(np.float32) / 255.0
    chan = lambda c: np.clip(lit * (c / 255.0) * br * 255.0, 0, 255).astype(np.uint16)
    cr, cg, cb = chan(r), chan(g), chan(b)
    rgb = ((cr & 0xF8) << 8) | ((cg & 0xFC) << 3) | (cb >> 3)
    flat = rgb.flatten()
    out = np.empty(flat.size * 2, np.uint8)
    out[0::2] = (flat >> 8) & 0xFF   # high byte first (big-endian)
    out[1::2] = flat & 0xFF
    return out.tobytes()


@bp.route("/api/units/<int:i>/aruco", methods=["POST"])
def unit_aruco(i):
    if not 0 <= i < N_UNITS:
        return jsonify({"ok": False, "error": "bad unit"}), 404
    if not _HAS_REQUESTS:
        return jsonify({"ok": False, "error": "requests not installed"}), 503
    ip = _units[i]["ip"]
    if not ip:
        return jsonify({"ok": False, "error": "no IP set for this unit"}), 400
    body = request.get_json(silent=True) or {}
    mid = int(body.get("id", 0))
    bright = max(0, min(255, int(body.get("brightness", 255))))
    color = _parse_color(body.get("color"), (255, 255, 255))
    with _units_lock:                                 # remember the last-sent values
        _units[i]["mid"] = mid % ARUCO_NMARKERS
        _units[i]["bri"] = bright
        _units[i]["col"] = _hex_color(color)
        _save_units()
    try:
        data = _make_aruco_frame(mid, brightness=bright, color=color)
    except Exception as e:
        return jsonify({"ok": False, "error": f"render failed (opencv?): {e}"}), 500
    try:
        r = requests.post(f"http://{ip}/frame",
                          params={"mid": mid % ARUCO_NMARKERS},   # device records the id for /state
                          files={"frame": ("frame.dat", data, "application/octet-stream")},
                          timeout=10)
    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": f"{ip} timed out (offline?)"}), 502
    except requests.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": f"can't reach {ip} (offline or wrong IP)"}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": f"send failed ({e.__class__.__name__})"}), 502
    if not r.ok:
        return jsonify({"ok": False, "error": f"{ip} returned HTTP {r.status_code}"}), 502
    return jsonify({"ok": True, "status": r.status_code, "id": mid % ARUCO_NMARKERS,
                    "brightness": bright, "color": _hex_color(color), "ip": ip})


def hub_init(ctx):
    _load_wifi()
    _load_units()
    _start_poller()
