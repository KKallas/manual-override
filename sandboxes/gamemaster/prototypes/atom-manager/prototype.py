"""
Atom Manager — manage attached M5Stack AtomS3R devices over USB serial.

Part 1: discover connected serial devices (more than one may be attached) and,
per device, flash the atom-image-server firmware, push WiFi credentials, and read
back the DHCP IP. Flashing shells out to PlatformIO (`pio run -t upload`) against
the ESP/atom-image-server project; WiFi/IP talk to that firmware's serial console.

Loaded by hub.py; registered under /p/atom-manager.
"""

import csv
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

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
CAPTURE_DIR = os.path.join(HERE, "logs")
CAPTURE_RECORD = struct.Struct("<I7fB")
CAPTURE_SAMPLES = 1600


def _find_repo_root():
    """Walk up from this machine's folder to the checkout that holds ESP/.
    The machine lives at <root>/sandboxes/<name>/prototypes/atom-manager, but
    don't hardcode the depth — exported setups may sit elsewhere."""
    d = HERE
    while True:
        if os.path.isdir(os.path.join(d, "ESP", "atom-image-server")):
            return d
        parent = os.path.dirname(d)
        if parent == d:                                   # filesystem root
            return os.path.dirname(os.path.dirname(HERE))  # legacy fallback
        d = parent


REPO_ROOT = _find_repo_root()
ESP_PROJECT = os.path.join(REPO_ROOT, "ESP", "atom-image-server")  # firmware to flash
def _find_pio():
    """Resolve PlatformIO when it is needed, not only when the hub starts.

    This lets an operator install the expected repo-local .venv while the hub
    is already running and then press Flash again without restarting it.
    """
    candidates = (
        os.environ.get("PLATFORMIO_CLI"),
        os.path.join(REPO_ROOT, ".venv", "bin", "pio"),
        shutil.which("pio"),
        os.path.expanduser("~/.platformio/penv/bin/pio"),
    )
    return next((p for p in candidates if p and (shutil.which(p) or os.path.exists(p))), None)


PIO = _find_pio()
PIO_CORE_DIR = os.environ.get("PLATFORMIO_CORE_DIR") or os.path.join(REPO_ROOT, ".platformio")

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
    env = os.environ.copy()
    env.setdefault("PLATFORMIO_CORE_DIR", PIO_CORE_DIR)
    env.setdefault("PLATFORMIO_SETTING_ENABLE_TELEMETRY", "no")
    try:
        proc = subprocess.Popen(cmd, cwd=ESP_PROJECT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                env=env)
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
    global PIO
    port = (request.get_json(silent=True) or {}).get("port")
    if not port:
        return jsonify({"ok": False, "error": "no port given"}), 400
    if not os.path.isdir(ESP_PROJECT):
        return jsonify({"ok": False, "error": f"firmware project not found at {ESP_PROJECT}"}), 500
    PIO = _find_pio()  # pick up a repo-local install made after hub startup
    if not PIO:
        return jsonify({"ok": False, "error": "PlatformIO (pio) not found; expected repo-local .venv/bin/pio"}), 500
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
ONLINE_POLL_SECS = 0.075       # ~13 Hz per healthy tag
OFFLINE_POLL_SECS = 2.0        # failed tags back off independently
EMPTY_POLL_SECS = 5.0
TAG_HTTP_TIMEOUT = (0.6, 0.9)  # tolerate brief WiFi/ESP web-server stalls
FAILURES_BEFORE_OFFLINE = 3    # do not flap on one missed state response
_units = [{"ip": None, "name": None, "online": False, "last_seen": None, "mid": 0,
           "bri": 255, "col": "#ffffff", "bat": None, "accel": None,
           "poll_failures": 0,
           "accel_history": None, "accel_history_at": 0.0,
           "accel_history_captured_at": 0.0,
           "accel_on": True, "accel_sens": 0.85, "accel_hit_thr": 1.05,
           "accel_drop_ms": 10, "accel_require_drop": True}
          for _ in range(N_UNITS)]
_units_lock = threading.Lock()
_poller_started = False
_poll_wake = [threading.Event() for _ in range(N_UNITS)]
_tag_io_locks = [threading.Lock() for _ in range(N_UNITS)]


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
                name = u.get("name")
                if isinstance(name, str) and name.strip():
                    _units[i]["name"] = name.strip()
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
                if isinstance(u.get("accel_on"), bool):
                    _units[i]["accel_on"] = u["accel_on"]
                if isinstance(u.get("accel_require_drop"), bool):
                    _units[i]["accel_require_drop"] = u["accel_require_drop"]
                # accel_thr is the pre-split manager key; migrate it to the
                # independent hit threshold without changing its meaning.
                try:
                    sensitivity = float(u.get("accel_sens"))
                    if 0.01 <= sensitivity <= 0.95:
                        _units[i]["accel_sens"] = sensitivity
                except (TypeError, ValueError):
                    pass
                try:
                    drop_ms = int(u.get("accel_drop_ms"))
                    if 5 <= drop_ms <= 250:
                        _units[i]["accel_drop_ms"] = drop_ms
                except (TypeError, ValueError):
                    pass
                try:
                    threshold = float(u.get("accel_hit_thr", u.get("accel_thr")))
                    if 1.05 <= threshold <= 8.0:
                        _units[i]["accel_hit_thr"] = threshold
                except (TypeError, ValueError):
                    pass
    else:                                             # legacy format: {"ips": [...]}
        for i, ip in enumerate((data.get("ips") or [])[:N_UNITS]):
            _units[i]["ip"] = _clean_ip(ip)
        _save_units()                                 # migrate to the new format


def _save_units():
    try:
        with open(UNITS_PATH, "w") as f:
            json.dump({"units": [{"ip": u["ip"], "name": u["name"], "mid": u["mid"],
                                  "bri": u["bri"], "col": u["col"],
                                  "accel_on": u["accel_on"],
                                  "accel_sens": u["accel_sens"],
                                  "accel_hit_thr": u["accel_hit_thr"],
                                  "accel_drop_ms": u["accel_drop_ms"],
                                  "accel_require_drop": u["accel_require_drop"]}
                                 for u in _units]}, f, indent=2)
    except OSError:
        pass


def _accel_state(a):
    """Normalize accelerometer JSON returned by either /state or its setter."""
    if not isinstance(a, dict):
        return None
    split_settings = a.get("hitThreshold") is not None
    return {
        "available": bool(a.get("available", True)),
        "enabled": bool(a.get("enabled")),
        "requireDrop": bool(a.get("requireDrop", True)),
        "dropRequirementSetting": "requireDrop" in a,
        "sampleRateHz": a.get("sampleRateHz"),
        "rangeG": a.get("rangeG"),
        "bufferWindowMs": a.get("bufferWindowMs"),
        "bufferSamples": a.get("bufferSamples"),
        "shapeRiseWindowMs": a.get("shapeRiseWindowMs"),
        "shapeMinRiseG": a.get("shapeMinRiseG"),
        # Legacy firmware called its combined threshold "sensitivity".
        "sensitivity": a.get("sensitivity") if split_settings else 0.85,
        "hitThreshold": a.get("hitThreshold", a.get("sensitivity")),
        "effectiveThreshold": a.get("effectiveThreshold",
                                    a.get("hitThreshold", a.get("sensitivity"))),
        "dropConfirmMs": a.get("dropConfirmMs", 10),
        "separateSettings": split_settings,
        "x": a.get("x"), "y": a.get("y"), "z": a.get("z"),
        "magnitude": a.get("magnitude"),
        "dropArmed": bool(a.get("dropArmed")),
        "hardSurfaceHit": bool(a.get("hardSurfaceHit")),
        "lastImpactG": a.get("lastImpactG"),
        "lastShapeDropG": a.get("lastShapeDropG"),
        "lastShapeDeltaG": a.get("lastShapeDeltaG"),
        "lastShapeRiseMs": a.get("lastShapeRiseMs"),
        "impactCount": a.get("impactCount"),
        "lastImpactAgoMs": a.get("lastImpactAgoMs"),
    }


def _poll_one_impl(i):
    ip = _units[i]["ip"]
    was_online = _units[i]["online"]
    if not ip or not _HAS_REQUESTS:
        _units[i]["online"] = False
        _units[i]["poll_failures"] = 0
        _units[i]["bat"] = None
        _units[i]["accel"] = None
        _units[i]["accel_history"] = None
        _units[i]["accel_history_captured_at"] = 0.0
        return
    bat = None
    accel = None
    try:
        # GET /state reports marker id + battery; the device is the source of
        # truth for what it's currently showing. Older firmware without /state
        # still answers (404 < 500) so it just reads as online with no battery.
        resp = requests.get(f"http://{ip}/state", timeout=TAG_HTTP_TIMEOUT)
        ok = resp.status_code < 500
        if resp.ok:
            st = resp.json()
            state_captured_at = time.monotonic()
            b = st.get("battery") or {}
            if b.get("mv") is not None:
                pct = b.get("pct")
                bat = {"mv": int(b["mv"]), "pct": int(pct) if pct is not None else None}
            a = st.get("accelerometer")
            accel = _accel_state(a)
            if accel and accel["available"]:
                # The tag exposes a compact binary 3-second history. Refresh it
                # up to about 6.7 fps; each response contains up to 480 points
                # (every tenth sample = 160 samples/s from the 1600 Hz source).
                history = _units[i].get("accel_history")
                history_now = time.monotonic()
                if history_now - _units[i].get("accel_history_at", 0.0) >= 0.15:
                    _units[i]["accel_history_at"] = history_now
                    try:
                        hist_resp = requests.get(
                            f"http://{ip}/accelerometer/history",
                            timeout=TAG_HTTP_TIMEOUT)
                        raw = hist_resp.content
                        if hist_resp.ok and raw and len(raw) % 2 == 0:
                            count = len(raw) // 2
                            milli_g = struct.unpack(f"<{count}H", raw)
                            history = {
                                "windowMs": float(
                                    hist_resp.headers.get("X-Window-Ms", 3000)),
                                "durationMs": int(
                                    hist_resp.headers.get("X-Duration-Us", 0)) / 1000,
                                "sourceRateHz": int(
                                    hist_resp.headers.get("X-Source-Rate-Hz", 1600)),
                                "sourceSamples": int(
                                    hist_resp.headers.get("X-Source-Samples", 0)),
                                "decimation": int(
                                    hist_resp.headers.get("X-Display-Decimation", 3)),
                                "sequence": time.time_ns(),
                                "capturedAtEpochMs": time.time() * 1000,
                                "samples": [v / 1000.0 for v in milli_g],
                            }
                            _units[i]["accel_history"] = history
                            _units[i]["accel_history_captured_at"] = time.monotonic()
                    except Exception:
                        pass
                if history:
                    history_for_client = dict(history)
                    history_for_client["relativeToStateMs"] = (
                        _units[i].get("accel_history_captured_at", 0.0) -
                        state_captured_at) * 1000
                    accel["history"] = history_for_client
            mid = st.get("markerId")
            if isinstance(mid, int) and mid >= 0:
                _units[i]["mid"] = mid % ARUCO_NMARKERS
            # the device's persistent broadcast name is its unique id
            name = st.get("name")
            if isinstance(name, str) and name.strip() and name.strip() != _units[i]["name"]:
                _units[i]["name"] = name.strip()
                _save_units()

            # Keep a manager-side copy as a reconnect fallback. On the first
            # encounter learn the tag's settings; after a disconnect, reapply
            # the remembered values if the tag came back with different ones.
            if accel and accel["available"]:
                desired_on = _units[i]["accel_on"]
                desired_sens = _units[i]["accel_sens"]
                desired_hit_thr = _units[i]["accel_hit_thr"]
                desired_drop_ms = _units[i]["accel_drop_ms"]
                desired_require_drop = _units[i]["accel_require_drop"]
                if desired_on is None:
                    _units[i]["accel_on"] = accel["enabled"]
                if desired_hit_thr is None:
                    _units[i]["accel_hit_thr"] = float(accel["hitThreshold"])
                if desired_sens is None:
                    _units[i]["accel_sens"] = float(accel["sensitivity"])
                if desired_drop_ms is None:
                    _units[i]["accel_drop_ms"] = int(accel["dropConfirmMs"])
                if (desired_on is None or desired_sens is None or
                        desired_hit_thr is None or desired_drop_ms is None):
                    _save_units()
                elif accel["separateSettings"] and not was_online and (
                    accel["enabled"] != desired_on or
                    abs(float(accel["sensitivity"]) - desired_sens) > 0.005 or
                    abs(float(accel["hitThreshold"]) - desired_hit_thr) > 0.005 or
                    int(accel["dropConfirmMs"]) != desired_drop_ms or
                    (accel["dropRequirementSetting"] and
                     accel["requireDrop"] != desired_require_drop)
                ):
                    sync = requests.post(
                        f"http://{ip}/accelerometer",
                        params={"enabled": 1 if desired_on else 0,
                                "sensitivity": desired_sens,
                                "hitThreshold": desired_hit_thr,
                                "dropConfirmMs": desired_drop_ms,
                                "requireDrop": 1 if desired_require_drop else 0},
                        timeout=3,
                    )
                    if sync.ok:
                        accel = _accel_state(sync.json()) or accel
                        if history and accel:
                            accel["history"] = history_for_client
    except Exception:
        ok = False
    if ok:
        _units[i]["poll_failures"] = 0
        _units[i]["online"] = True
        _units[i]["bat"] = bat
        _units[i]["accel"] = accel
        _units[i]["last_seen"] = time.time()
    else:
        _units[i]["poll_failures"] += 1
        # Preserve the last good telemetry and online state through isolated
        # packet loss. Only a consecutive-failure streak declares the tag down.
        if not was_online or _units[i]["poll_failures"] >= FAILURES_BEFORE_OFFLINE:
            _units[i]["online"] = False
            _units[i]["bat"] = None
            _units[i]["accel"] = None


def _poll_one(i):
    """Poll only when no foreground operation owns this tag's HTTP channel."""
    if not _tag_io_locks[i].acquire(blocking=False):
        return
    try:
        _poll_one_impl(i)
    finally:
        _tag_io_locks[i].release()


def _start_poller():
    """Run one polling loop per slot so a slow/offline tag blocks only itself."""
    global _poller_started
    if _poller_started:
        return
    _poller_started = True

    def loop(i):
        while True:
            started = time.monotonic()
            try:
                _poll_one(i)
            except Exception:
                pass
            if not _units[i]["ip"]:
                interval = EMPTY_POLL_SECS
            elif _units[i]["online"]:
                interval = ONLINE_POLL_SECS
            else:
                interval = OFFLINE_POLL_SECS
            remaining = max(0.0, interval - (time.monotonic() - started))
            _poll_wake[i].wait(remaining)
            _poll_wake[i].clear()

    for i in range(N_UNITS):
        threading.Thread(target=loop, args=(i,), daemon=True,
                         name=f"atom-poll-{i + 1}").start()


def _request_poll(i):
    """Wake one unit's existing worker instead of starting an overlapping poll."""
    if 0 <= i < N_UNITS:
        _poll_wake[i].set()


@bp.route("/api/units")
def api_units():
    now = time.time()
    with _units_lock:
        return jsonify({"units": [
            {"id": i, "ip": u["ip"], "name": u["name"], "online": u["online"],
             "ago": round(now - u["last_seen"], 1) if u["last_seen"] else None,
             "mid": u["mid"], "bri": u["bri"], "col": u["col"], "bat": u["bat"],
             "accel": u["accel"], "accel_enabled": u["accel_on"],
             "accel_sensitivity": u["accel_sens"],
             "accel_hit_threshold": u["accel_hit_thr"],
             "accel_drop_ms": u["accel_drop_ms"],
             "accel_require_drop": u["accel_require_drop"]}
            for i, u in enumerate(_units)]})


@bp.route("/api/units/<int:i>", methods=["PATCH"])
def set_unit(i):
    if not 0 <= i < N_UNITS:
        return jsonify({"ok": False, "error": "bad unit"}), 404
    ip = _clean_ip((request.get_json(silent=True) or {}).get("ip"))
    with _units_lock:
        if ip != _units[i]["ip"]:
            _units[i]["name"] = None      # re-learned from /state on the next poll
            _units[i]["accel_history"] = None
            _units[i]["accel_history_at"] = 0.0
            _units[i]["accel_history_captured_at"] = 0.0
        _units[i]["ip"] = ip
        _units[i]["online"] = False
        _units[i]["poll_failures"] = 0
        _units[i]["last_seen"] = None
        _save_units()
    _request_poll(i)
    return jsonify({"ok": True, "id": i, "ip": _units[i]["ip"]})


@bp.route("/api/units/<int:i>/accelerometer", methods=["POST"])
def unit_accelerometer(i):
    """Update a physical tag's persisted accelerometer settings."""
    if not 0 <= i < N_UNITS:
        return jsonify({"ok": False, "error": "bad unit"}), 404
    if not _HAS_REQUESTS:
        return jsonify({"ok": False, "error": "requests not installed"}), 503
    ip = _units[i]["ip"]
    if not ip:
        return jsonify({"ok": False, "error": "no IP set for this unit"}), 400

    body = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled"))
    require_drop = bool(body.get("requireDrop", True))
    try:
        sensitivity = float(body.get("sensitivity", 0.85))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid sensitivity"}), 400
    try:
        hit_threshold = float(body.get("hitThreshold", 1.05))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid hit threshold"}), 400
    try:
        drop_ms = int(body.get("dropConfirmMs", 10))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid drop duration"}), 400
    if not 0.01 <= sensitivity <= 0.95:
        return jsonify({"ok": False, "error":
                        "drop sensitivity must be 0.01–0.95 g"}), 400
    if not 1.05 <= hit_threshold <= 8.0:
        return jsonify({"ok": False, "error": "hit threshold must be 1.05–8.0 g"}), 400
    if not 5 <= drop_ms <= 250:
        return jsonify({"ok": False, "error": "drop duration must be 5–250 ms"}), 400

    # Save the operator's desired values before contacting the tag. They remain
    # visible while offline and are re-applied automatically after reconnect or
    # after compatible firmware is flashed.
    with _units_lock:
        _units[i]["accel_on"] = enabled
        _units[i]["accel_sens"] = sensitivity
        _units[i]["accel_hit_thr"] = hit_threshold
        _units[i]["accel_drop_ms"] = drop_ms
        _units[i]["accel_require_drop"] = require_drop
        _save_units()

    current_accel = _units[i]["accel"]
    if current_accel and not current_accel.get("separateSettings"):
        return jsonify({"ok": False, "error":
                        "tag firmware combines sensitivity and threshold; reflash this tag"}), 409

    try:
        r = requests.post(
            f"http://{ip}/accelerometer",
            params={"enabled": 1 if enabled else 0, "sensitivity": sensitivity,
                    "hitThreshold": hit_threshold, "dropConfirmMs": drop_ms,
                    "requireDrop": 1 if require_drop else 0},
            timeout=3,
        )
        try:
            data = r.json()
        except (TypeError, ValueError):
            data = {}
    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": f"{ip} timed out (offline?)"}), 502
    except requests.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": f"can't reach {ip} (offline or wrong IP)"}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": f"send failed ({e.__class__.__name__})"}), 502
    if not r.ok:
        return jsonify({"ok": False, "error": data.get("error") or
                        f"{ip} returned HTTP {r.status_code}"}), 502

    with _units_lock:
        _units[i]["accel"] = _accel_state(data)
        _save_units()

    # Re-poll immediately so the units table reflects the device's source of truth.
    _request_poll(i)
    return jsonify(data)


@bp.route("/api/units/<int:i>/capture", methods=["POST"])
def capture_unit(i):
    """Capture, save, and return one second of full-rate IMU data."""
    if not 0 <= i < N_UNITS:
        return jsonify({"ok": False, "error": "bad unit"}), 404
    if not _HAS_REQUESTS:
        return jsonify({"ok": False, "error": "requests not installed"}), 503
    body = request.get_json(silent=True) or {}
    manual_label = 1 if bool(body.get("manual_drop_label")) else 0
    with _units_lock:
        ip = _units[i]["ip"]
        name = _units[i]["name"] or f"unit-{i + 1}"
        online = _units[i]["online"]
    if not ip:
        return jsonify({"ok": False, "error": "no tag assigned to this unit"}), 400
    if not online:
        return jsonify({"ok": False, "error": "selected tag is offline"}), 409

    with _tag_io_locks[i]:
        try:
            response = requests.get(f"http://{ip}/accelerometer/capture", timeout=5)
        except requests.exceptions.Timeout:
            return jsonify({"ok": False, "error": "tag capture timed out"}), 502
        except requests.exceptions.ConnectionError:
            return jsonify({"ok": False, "error": "tag went offline"}), 502
        except Exception as exc:
            return jsonify({"ok": False, "error": f"capture failed ({exc.__class__.__name__})"}), 502
    if not response.ok:
        try:
            tag_error = response.json().get("error")
        except (TypeError, ValueError, AttributeError):
            tag_error = None
        return jsonify({"ok": False, "error": tag_error or
                        f"tag returned HTTP {response.status_code}; reflash it with current firmware"}), 502
    raw = response.content
    if len(raw) % CAPTURE_RECORD.size:
        return jsonify({"ok": False, "error": "tag returned a malformed capture"}), 502
    count = len(raw) // CAPTURE_RECORD.size
    if count != CAPTURE_SAMPLES:
        return jsonify({"ok": False, "error":
                        f"tag returned {count}/{CAPTURE_SAMPLES} samples; reflash current firmware or check its IMU connection"}), 409

    records = []
    for values in CAPTURE_RECORD.iter_unpack(raw):
        timestamp_us, magnitude, ax, ay, az, gx, gy, gz, detector_hit = values
        records.append({
            "timestamp_us": timestamp_us,
            "accel_magnitude": round(magnitude, 6),
            "accel_x": round(ax, 6), "accel_y": round(ay, 6),
            "accel_z": round(az, 6), "gyro_x": round(gx, 6),
            "gyro_y": round(gy, 6), "gyro_z": round(gz, 6),
            "manual_drop_label": manual_label,
            "detector_hit": int(bool(detector_hit)),
        })

    os.makedirs(CAPTURE_DIR, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or f"unit-{i + 1}"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{stamp}-{safe_name}.csv"
    path = os.path.join(CAPTURE_DIR, filename)
    fields = list(records[0])
    try:
        with open(path, "w", newline="") as log_file:
            writer = csv.DictWriter(log_file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"could not save log: {exc}"}), 500

    duration_us = ((records[-1]["timestamp_us"] - records[0]["timestamp_us"])
                   & 0xFFFFFFFF)
    return jsonify({"ok": True, "unit": i, "tag": name, "samples": count,
                    "duration_us": duration_us,
                    "log_file": os.path.relpath(path, REPO_ROOT), "records": records})


# ---- scanner: UDP discovery of broadcasting units ----------------------------
# Every flashed unit announces "ESPKIOSK1 <name> <ip>" to 255.255.255.255:50505
# every ~8s (see ESP/*/include/names_discovery.h). We listen on that port and
# keep a name -> ip table, so the UI can show devices on the network that no
# unit slot has claimed yet. The name is the device's persistent unique id.
DISCO_PORT = 50505
DISCO_MAGIC = "ESPKIOSK1"
DISCO_TTL = 90.0                  # forget a device unheard this long (matches firmware)
_discovered = {}                  # name -> {"ip": str, "last_seen": float}
_disco_lock = threading.Lock()
_disco_error = None               # why listening failed, or None
_disco_started = False


def _disco_loop():
    global _disco_error
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):     # play nice with other listeners
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", DISCO_PORT))
            sock.settimeout(2.0)
            _disco_error = None
            while True:
                try:
                    data, _addr = sock.recvfrom(256)
                except socket.timeout:
                    continue
                parts = data.decode("utf-8", "replace").strip().split()
                if len(parts) >= 3 and parts[0] == DISCO_MAGIC:
                    name, ip = parts[1], parts[2]
                    if _IPV4.fullmatch(ip):
                        with _disco_lock:
                            _discovered[name] = {"ip": ip, "last_seen": time.time()}
        except OSError as e:
            _disco_error = f"UDP {DISCO_PORT}: {e}"
            time.sleep(5)         # port busy / no network — retry
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def _start_disco():
    global _disco_started
    if _disco_started:
        return
    _disco_started = True
    threading.Thread(target=_disco_loop, name="atom-disco", daemon=True).start()


def _free_slot_locked():
    """Index of the first unit slot with no IP, or None (call with _units_lock)."""
    for i, u in enumerate(_units):
        if not u["ip"]:
            return i
    return None


@bp.route("/api/scanner")
def api_scanner():
    """Devices currently broadcasting on the network, each labeled:
    new (no slot has it), claimed (a slot has this name+ip), or moved (a slot
    has this name but the device now broadcasts a different IP)."""
    now = time.time()
    with _disco_lock:
        # prune silently-gone devices while we're here
        for name in [n for n, d in _discovered.items() if now - d["last_seen"] > DISCO_TTL]:
            del _discovered[name]
        snapshot = {n: dict(d) for n, d in _discovered.items()}
    found = []
    with _units_lock:
        by_ip = {u["ip"]: i for i, u in enumerate(_units) if u["ip"]}
        by_name = {u["name"]: i for i, u in enumerate(_units) if u["name"]}
        free = _free_slot_locked()
        for name in sorted(snapshot):
            d = snapshot[name]
            slot = by_name.get(name, by_ip.get(d["ip"]))
            if slot is None:
                status = "new"
            elif _units[slot]["ip"] == d["ip"]:
                status = "claimed"
            else:
                status = "moved"
            found.append({"name": name, "ip": d["ip"],
                          "ago": round(now - d["last_seen"], 1),
                          "status": status, "slot": slot})
    return jsonify({"ok": True, "found": found, "free_slot": free,
                    "listening": _disco_error is None, "error": _disco_error})


@bp.route("/api/scanner/claim", methods=["POST"])
def api_claim():
    """Claim a discovered device: assign its IP (+ name) to a unit slot — the
    first free one, a caller-chosen one, or (for a moved device) the slot that
    already holds its name."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    with _disco_lock:
        d = _discovered.get(name)
        ip = d["ip"] if d else None
    if not ip:
        return jsonify({"ok": False, "error": f"'{name}' is no longer broadcasting"}), 404
    with _units_lock:
        slot = data.get("slot")
        if slot is None:
            by_name = {u["name"]: i for i, u in enumerate(_units) if u["name"]}
            slot = by_name.get(name)                  # moved device → its own slot
        if slot is None:
            slot = _free_slot_locked()
        if slot is None:
            return jsonify({"ok": False, "error": "no free unit slot"}), 409
        if not 0 <= int(slot) < N_UNITS:
            return jsonify({"ok": False, "error": "bad slot"}), 400
        slot = int(slot)
        _units[slot]["ip"] = ip
        _units[slot]["name"] = name
        _units[slot]["online"] = False
        _units[slot]["last_seen"] = None
        _save_units()
    _request_poll(slot)
    return jsonify({"ok": True, "slot": slot, "name": name, "ip": ip})


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
    _start_disco()
