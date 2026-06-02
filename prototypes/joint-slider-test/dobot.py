"""
Dobot MG400 TCP/IP driver — first prototype.

Implements all three communication layers exposed by the MG400 controller when
it is in TCP/IP ("API") mode:

  * Dashboard  (port 29999) — control & settings: enable, disable, clear error,
                              reset/stop, emergency stop, speed factor, queries.
  * Motion     (port 30003) — motion commands: JointMovJ (absolute joint move).
  * Feedback   (port 30004) — 1440-byte real-time status packet @ ~8 ms,
                              parsed for robot mode + actual joint angles.

The class is thread-safe: each command socket has its own lock, and the feedback
socket is read by a dedicated background thread that updates a shared state dict.

References: Dobot TCP/IP protocol (4-axis, MG400/M1Pro). Joint angles are in
degrees. The feedback struct offsets used below are the documented layout; the
packet is validated with the 0x0123456789ABCDEF magic at offset 48.
"""

import socket
import struct
import threading
import time
import json


# ---- ports ----------------------------------------------------------------
PORT_DASHBOARD = 29999
PORT_MOTION = 30003
PORT_FEEDBACK = 30004

# ---- feedback packet ------------------------------------------------------
FEEDBACK_SIZE = 1440
FEEDBACK_MAGIC = 0x0123456789ABCDEF  # 'test_value' field, validates alignment
OFF_DIGITAL_IN = 8     # uint64
OFF_DIGITAL_OUT = 16   # uint64
OFF_ROBOT_MODE = 24    # uint64
OFF_TEST_VALUE = 48    # uint64 (magic)
OFF_Q_ACTUAL = 432     # 6 x double (actual joint angles, degrees)

# ---- robot modes (best-effort labels per Dobot docs) ----------------------
ROBOT_MODES = {
    1: "INIT",
    2: "BRAKE_OPEN",
    3: "RESERVED",
    4: "DISABLED",
    5: "ENABLED (idle)",
    6: "BACKDRIVE",
    7: "RUNNING",
    8: "SINGLE_MOVE",
    9: "ERROR",
    10: "PAUSE",
    11: "JOG",
}
# modes in which the servos are powered on
ENABLED_MODES = {5, 6, 7, 8, 10, 11}


class DobotError(Exception):
    """Raised when the controller reports a non-zero ErrorID for a command."""

    def __init__(self, errid, resp, command):
        self.errid = errid
        self.resp = resp
        self.command = command
        super().__init__(f"{command} -> ErrorID {errid}: {resp}")


def parse_feedback(packet):
    """Parse a single 1440-byte feedback packet. Returns dict or None if the
    packet is not aligned (magic mismatch)."""
    if len(packet) < FEEDBACK_SIZE:
        return None
    test_value = struct.unpack_from("<Q", packet, OFF_TEST_VALUE)[0]
    if test_value != FEEDBACK_MAGIC:
        return None
    robot_mode = struct.unpack_from("<Q", packet, OFF_ROBOT_MODE)[0]
    digital_in = struct.unpack_from("<Q", packet, OFF_DIGITAL_IN)[0]
    digital_out = struct.unpack_from("<Q", packet, OFF_DIGITAL_OUT)[0]
    q_actual = struct.unpack_from("<6d", packet, OFF_Q_ACTUAL)
    return {
        "robot_mode": int(robot_mode),
        "digital_in": int(digital_in),
        "digital_out": int(digital_out),
        # MG400 is 4-axis; first four values are J1..J4 in degrees
        "joints": [round(v, 3) for v in q_actual[:4]],
    }


class DobotMG400:
    def __init__(self, ip, connect_timeout=5.0, command_timeout=5.0):
        self.ip = ip
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

        self._dashboard = None
        self._motion = None
        self._feedback = None

        self._dash_lock = threading.Lock()
        self._motion_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self._feed_thread = None
        self._running = False

        self._state = self._blank_state()

    # -- state ----------------------------------------------------------------
    @staticmethod
    def _blank_state():
        return {
            "connected": False,
            "robot_mode": 0,
            "mode_name": "DISCONNECTED",
            "enabled": False,
            "error": False,
            "joints": [0.0, 0.0, 0.0, 0.0],
            "digital_in": 0,
            "digital_out": 0,
            "last_feedback": 0.0,
            "feedback_ok": False,
        }

    def get_state(self):
        with self._state_lock:
            return dict(self._state)

    def is_connected(self):
        with self._state_lock:
            return self._state["connected"]

    # -- connection -----------------------------------------------------------
    def _open(self, port, read_timeout):
        s = socket.create_connection((self.ip, port), timeout=self.connect_timeout)
        s.settimeout(read_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return s

    def connect(self):
        """Open all three sockets and start the feedback thread. Raises on
        failure (and cleans up any partially-opened sockets)."""
        try:
            self._dashboard = self._open(PORT_DASHBOARD, self.command_timeout)
            self._motion = self._open(PORT_MOTION, self.command_timeout)
            self._feedback = self._open(PORT_FEEDBACK, 2.0)
        except OSError as e:
            self.close()
            raise DobotError(-1, str(e), "connect")

        self._running = True
        with self._state_lock:
            self._state["connected"] = True
        self._feed_thread = threading.Thread(
            target=self._feed_loop, name="dobot-feedback", daemon=True
        )
        self._feed_thread.start()

    def close(self):
        """Stop the feedback thread and close every socket. Safe to call twice."""
        self._running = False
        for sock in (self._feedback, self._motion, self._dashboard):
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
        self._dashboard = self._motion = self._feedback = None
        if self._feed_thread and self._feed_thread.is_alive():
            self._feed_thread.join(timeout=1.0)
        self._feed_thread = None
        with self._state_lock:
            self._state = self._blank_state()

    # -- low-level command I/O ------------------------------------------------
    def _recv_response(self, sock):
        """Read one Dobot response, terminated by ';'."""
        data = b""
        while b";" not in data:
            chunk = sock.recv(1024)
            if not chunk:
                raise DobotError(-1, "connection closed by robot", "recv")
            data += chunk
        return data.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _parse_errid(resp):
        try:
            return int(resp.split(",", 1)[0])
        except (ValueError, IndexError):
            return -1

    def _command(self, sock, lock, command, raise_on_error=True):
        """Send one command on a control socket and return (errid, response)."""
        if sock is None:
            raise DobotError(-1, "not connected", command)
        with lock:
            try:
                sock.sendall(command.encode("utf-8"))
                resp = self._recv_response(sock)
            except (OSError, socket.timeout) as e:
                raise DobotError(-1, str(e), command)
        errid = self._parse_errid(resp)
        if raise_on_error and errid != 0:
            raise DobotError(errid, resp, command)
        return errid, resp

    def _dash(self, command, raise_on_error=True):
        return self._command(self._dashboard, self._dash_lock, command, raise_on_error)

    def _move(self, command, raise_on_error=True):
        return self._command(self._motion, self._motion_lock, command, raise_on_error)

    # -- dashboard (control) --------------------------------------------------
    def enable(self):
        return self._dash("EnableRobot()")

    def disable(self):
        return self._dash("DisableRobot()")

    def clear_error(self):
        return self._dash("ClearError()")

    def reset(self):
        """Soft stop: stop current motion and clear the motion queue."""
        return self._dash("ResetRobot()")

    def emergency_stop(self):
        """Emergency stop: cut servo power immediately. Recovery requires
        ClearError() + EnableRobot(). Not raised-on-error so the UI always sees
        the result even on older firmware."""
        return self._dash("EmergencyStop()", raise_on_error=False)

    def speed_factor(self, ratio):
        ratio = max(1, min(100, int(ratio)))
        return self._dash(f"SpeedFactor({ratio})")

    def get_angle(self):
        """Query current joint angles via the dashboard (alternative to the
        feedback stream). Returns a list of floats."""
        _, resp = self._dash("GetAngle()")
        return self._extract_floats(resp)

    def get_error_id(self):
        """Return the controller/servo error IDs as a flat list of non-zero
        integers (empty if none)."""
        _, resp = self._dash("GetErrorID()", raise_on_error=False)
        return self._extract_error_ids(resp)

    # -- motion ---------------------------------------------------------------
    def joint_move(self, j1, j2, j3, j4):
        """Absolute joint-space move (degrees). Returns (errid, resp); does not
        raise on a controller error so out-of-range targets surface to the UI."""
        cmd = f"JointMovJ({j1:.3f},{j2:.3f},{j3:.3f},{j4:.3f})"
        return self._move(cmd, raise_on_error=False)

    # -- response parsing helpers ---------------------------------------------
    @staticmethod
    def _extract_braces(resp):
        start = resp.find("{")
        end = resp.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return ""
        return resp[start + 1 : end]

    def _extract_floats(self, resp):
        inner = self._extract_braces(resp)
        out = []
        for tok in inner.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                out.append(float(tok))
            except ValueError:
                pass
        return out

    def _extract_error_ids(self, resp):
        inner = self._extract_braces(resp)
        try:
            nested = json.loads(inner)
        except (ValueError, TypeError):
            return []
        ids = []

        def walk(x):
            if isinstance(x, list):
                for v in x:
                    walk(v)
            elif isinstance(x, (int, float)):
                if int(x) != 0:
                    ids.append(int(x))

        walk(nested)
        return ids

    # -- feedback loop --------------------------------------------------------
    def _feed_loop(self):
        buf = b""
        while self._running:
            try:
                chunk = self._feedback.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            # Process all complete, aligned packets in the buffer.
            while len(buf) >= FEEDBACK_SIZE:
                parsed = parse_feedback(buf[:FEEDBACK_SIZE])
                if parsed is None:
                    # Misaligned stream: slide forward one byte to resync.
                    buf = buf[1:]
                    continue
                buf = buf[FEEDBACK_SIZE:]
                self._apply_feedback(parsed)

        with self._state_lock:
            self._state["connected"] = False
            self._state["feedback_ok"] = False
            self._state["mode_name"] = "DISCONNECTED"

    def _apply_feedback(self, parsed):
        mode = parsed["robot_mode"]
        with self._state_lock:
            self._state["robot_mode"] = mode
            self._state["mode_name"] = ROBOT_MODES.get(mode, f"UNKNOWN({mode})")
            self._state["enabled"] = mode in ENABLED_MODES
            self._state["error"] = mode == 9
            self._state["joints"] = parsed["joints"]
            self._state["digital_in"] = parsed["digital_in"]
            self._state["digital_out"] = parsed["digital_out"]
            self._state["last_feedback"] = time.time()
            self._state["feedback_ok"] = True
