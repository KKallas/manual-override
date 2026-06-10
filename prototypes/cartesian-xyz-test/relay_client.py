"""
Relay client — Cartesian (XYZ) prototype.

Mirrors the subset of the DobotMG400 interface this controller uses, but instead
of opening TCP sockets to a robot it forwards every command over HTTP to a remote
game-master *relay* mounted at `<host>/p/dobot-mg400-relay`. The relay owns the
actual arm; we acquire a per-side lease ("purple" / "green"), heartbeat to keep
it, and POST moves/pump/hold/estop. This keeps the browser talking only to its
own backend (no CORS): the browser hits prototype.py, prototype.py hits the relay.

Drop-in: prototype.py stores this behind the same `_robot` global as the direct
driver, so the existing routes and `_status_dict` keep working unchanged. The
caps (velocity / acceleration) live on the relay, so the cap setters here are
no-ops that accept their args and return (0, "").

Stdlib only (urllib + json + threading); no new dependencies.
"""

import json
import threading
import time
import urllib.error
import urllib.request


# Relay path + protocol constants (see the relay HTTP contract).
RELAY_PATH = "/p/dobot-mg400-relay"
HEARTBEAT_SECS = 0.7    # refresh the ~2 s lease comfortably ahead of expiry
HTTP_TIMEOUT = 4.0      # per-request timeout (seconds)


class RelayError(Exception):
    """A relay request failed. `holder` names the side that owns the floor when
    an acquire is rejected with 409, so the UI can say who's in control."""

    def __init__(self, message, holder=None, status=None):
        self.holder = holder
        self.status = status
        super().__init__(message)


class RelayClient:
    """Talks to the dobot-mg400-relay over HTTP, presenting the same methods the
    Cartesian controller calls on the direct driver.

    host        : base URL of the relay machine, e.g. "http://localhost:8000".
    side        : "purple" | "green" — which side's lease we hold.
    control_mode: "cartesian" (this controller) — sent as the relay move/acquire mode.
    """

    def __init__(self, host, side, control_mode="cartesian"):
        self.base = host.rstrip("/") + RELAY_PATH
        self.side = side
        self.control_mode = control_mode

        self._token = None
        self._lock = threading.Lock()
        self._hb_thread = None
        self._hb_running = False
        self._hb_ok = False           # last heartbeat succeeded
        self._relay_state = None      # cached relay state object (see contract)

    # -- HTTP plumbing --------------------------------------------------------
    def _post(self, path, payload=None):
        """POST JSON to the relay; return (status_code, parsed_json_or_None)."""
        url = self.base + path
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as e:
            # Read the error body so callers can see {ok:false, holder, error}.
            try:
                body = e.read().decode("utf-8", errors="replace")
                return e.code, (json.loads(body) if body else {})
            except (ValueError, OSError):
                return e.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return None, None

    def _get(self, path):
        url = self.base + path
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status, (json.loads(body) if body else {})
        except (urllib.error.URLError, OSError, ValueError):
            return None, None

    # -- connection (lease) ---------------------------------------------------
    def connect(self):
        """Acquire the floor for our side and start heartbeating. Raises
        RelayError on 409 (someone else holds it) or if the relay is unreachable."""
        status, body = self._post("/api/acquire",
                                  {"side": self.side, "mode": self.control_mode})
        if status == 409:
            holder = (body or {}).get("holder")
            raise RelayError(
                (body or {}).get("error", "relay is held by the other side"),
                holder=holder, status=409)
        if status is None:
            raise RelayError("relay unreachable")
        if not body or not body.get("ok"):
            raise RelayError((body or {}).get("error", "acquire failed"), status=status)
        with self._lock:
            self._token = body.get("token")
            self._hb_ok = True
        self._start_heartbeat()

    def _start_heartbeat(self):
        self._hb_running = True
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="relay-heartbeat", daemon=True)
        self._hb_thread.start()

    def _heartbeat_loop(self):
        """Refresh the lease ~every HEARTBEAT_SECS and cache the returned state."""
        while self._hb_running:
            token = self._token
            if token is None:
                break
            status, body = self._post("/api/heartbeat",
                                      {"side": self.side, "token": token})
            if status is not None and body and body.get("ok"):
                with self._lock:
                    self._hb_ok = True
                    self._relay_state = body.get("state") or self._relay_state
            else:
                # Lost the lease (expired / revoked / unreachable): stop holding it.
                with self._lock:
                    self._hb_ok = False
                    if status is not None:      # a definite rejection, not a blip
                        self._token = None
            time.sleep(HEARTBEAT_SECS)

    def is_connected(self):
        """True while we hold a valid token and heartbeats are succeeding."""
        with self._lock:
            return self._token is not None and self._hb_ok

    # -- state mapping --------------------------------------------------------
    def _relay_pump_to_do_bits(self, pump_mode):
        """Re-encode the relay's pump_mode string into the digital-output bit
        pattern the controller's _pump_mode() decodes (suck=DO2, blow=DO1)."""
        if pump_mode == "suck":
            return 1 << 1   # DO index 2 -> bit 1
        if pump_mode == "blow":
            return 1 << 0   # DO index 1 -> bit 0
        return 0

    def get_state(self):
        """Return the controller-shaped status dict derived from the cached relay
        state. Shape matches the direct driver's get_state() so _status_dict and
        the existing UI keep working. Degrades gracefully (connected=False) when
        no relay state has arrived yet."""
        with self._lock:
            rs = dict(self._relay_state) if self._relay_state else None
            have_token = self._token is not None
            hb_ok = self._hb_ok
        arm = (rs or {}).get("arm") or {}
        pump_mode = arm.get("pump_mode", "off")
        connected = bool(have_token and hb_ok and arm.get("connected"))
        return {
            "connected": connected,
            "robot_mode": 0,
            "mode_name": arm.get("mode_name", "RELAY"),
            "enabled": bool(arm.get("enabled")),
            "error": bool(arm.get("servo_error")),
            "joints": list(arm.get("joints") or [0.0, 0.0, 0.0, 0.0]),
            "pose": list(arm.get("pose") or [0.0, 0.0, 0.0, 0.0]),
            "target": (rs or {}).get("target"),
            "digital_in": 0,
            "digital_out": self._relay_pump_to_do_bits(pump_mode),
            "pump_mode": pump_mode,
            "last_feedback": time.time() if connected else 0.0,
            "feedback_ok": connected,
            "servo_active": bool(arm.get("servo_active")),
            "servo_error": arm.get("servo_error"),
            # relay-only extras the UI surfaces for the relay link
            "estop": bool((rs or {}).get("estop")),
            "holder": (rs or {}).get("holder"),
            "lease_secs": (rs or {}).get("lease_secs"),
        }

    def get_target(self):
        """The relay state's commanded target ([x,y,z,r]) or None."""
        with self._lock:
            rs = self._relay_state
        return (rs or {}).get("target") if rs else None

    # -- motion ---------------------------------------------------------------
    def set_target_pose(self, x, y, z, r=None):
        """Forward a Cartesian target to the relay. Signature matches the direct
        driver's set_target_pose so prototype.py / recall call it unchanged. R
        falls back to the last cached target's R when omitted."""
        if r is None:
            cur = self.get_target()
            r = cur[3] if cur and len(cur) > 3 else 0.0
        token = self._token
        if token is None:
            return
        self._post("/api/move", {
            "side": self.side, "token": token, "mode": "cartesian",
            "pose": [float(x), float(y), float(z), float(r)],
        })

    def hold(self):
        token = self._token
        if token is not None:
            self._post("/api/hold", {"side": self.side, "token": token})

    # -- pump -----------------------------------------------------------------
    def set_pump(self, mode, suck_do=None, blow_do=None):
        """Forward the pump command. Accepts the suck/blow DO indices the direct
        driver takes (ignored — the relay owns the I/O mapping). Returns (errid,
        resp) like the direct driver so _command() reports it the same way."""
        token = self._token
        if token is None:
            return -1, "not connected"
        status, body = self._post("/api/pump",
                                  {"side": self.side, "token": token, "mode": mode})
        ok = bool(body and body.get("ok"))
        return (0 if ok else -1), json.dumps(body) if body else ""

    # -- dashboard-style commands (mapped to relay endpoints) -----------------
    def enable(self):
        """Ask the relay to enable the arm (operator-level; clients MAY call it)."""
        status, body = self._post("/api/enable", {})
        ok = bool(body and body.get("ok"))
        return (0 if ok else -1), json.dumps(body) if body else ""

    def disable(self):
        # No relay disable in the contract; releasing/heartbeat-lapse hands the
        # floor back. Treat as a no-op success so the route doesn't error.
        return 0, "relay: disable is a no-op"

    def clear_error(self):
        # The operator clears errors on the relay side; no-op success here.
        return 0, "relay: clear_error is a no-op"

    def emergency_stop(self):
        """E-stop the arm via the relay (no token required)."""
        status, body = self._post("/api/estop", {})
        ok = bool(body and body.get("ok"))
        return (0 if ok else -1), json.dumps(body) if body else ""

    # -- caps (live on the relay) — accept args, do nothing -------------------
    def speed_factor(self, ratio):
        return 0, ""

    def set_max_velocity(self, lin_mm_s, ang_deg_s):
        return 0, ""

    def set_max_accel(self, lin_mm_s2, ang_deg_s2):
        return 0, ""

    # -- servo follower runs on the relay — no-op success ---------------------
    def start_servo(self):
        return 0, ""

    def stop_servo(self):
        return 0, ""

    # -- teardown -------------------------------------------------------------
    def close(self):
        """Stop heartbeating and release the floor."""
        self._hb_running = False
        thread = self._hb_thread
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=1.0)
        self._hb_thread = None
        token = self._token
        if token is not None:
            self._post("/api/release", {"side": self.side, "token": token})
        with self._lock:
            self._token = None
            self._hb_ok = False
