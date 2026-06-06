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

        # servo (smooth live following) state
        self._servo_thread = None
        self._servo_running = False
        self._servo_lock = threading.Lock()
        self._target = None      # desired joint vector [j1,j2,j3,j4]
        self._setpoint = None    # current streamed setpoint (slew-limited)
        self._max_vel = 45.0     # deg/s, per-joint velocity cap for following

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
            "target": None,
            "digital_in": 0,
            "digital_out": 0,
            "last_feedback": 0.0,
            "feedback_ok": False,
            "servo_active": False,
            "servo_error": None,
        }

    def get_state(self):
        with self._state_lock:
            return dict(self._state)

    def get_target(self):
        """The commanded joint target the follower is slewing toward, or None
        before the servo loop is initialised. Exposing it lets several control
        windows sync their sliders to the same setpoint."""
        with self._servo_lock:
            return list(self._target) if self._target is not None else None

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
        self.stop_servo()
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

    def set_digital_output(self, index, status, immediate=True):
        """Set a controller digital output (used for the vacuum pump / suction
        cup kit). `immediate` uses DOExecute so it fires now rather than waiting
        in the motion queue behind streamed ServoJ points."""
        value = 1 if status else 0
        cmd = (
            f"DOExecute({int(index)},{value})"
            if immediate
            else f"DO({int(index)},{value})"
        )
        return self._dash(cmd, raise_on_error=False)

    def set_pump(self, mode, suck_do, blow_do):
        """Drive the air pump box (I/O mode) in one of three modes: 'suck',
        'blow', 'off'.

        The Mini Vacuum Pump Box exposes two independent control lines — one
        drives suction, one drives blowing. At most one may be energised at a
        time (both high is a conflicting state); both low turns the pump off.
        We therefore always drop the opposite line before raising the active
        one, and 'off' pulls both low. Returns (errid, resp).
        """
        mode = (mode or "").lower()
        resp = []
        errid = 0

        def out(index, value, label):
            nonlocal errid
            e, r = self.set_digital_output(index, value)
            resp.append(f"{label}={r}")
            errid = e or errid

        if mode == "suck":
            out(blow_do, 0, "blow")   # ensure blow is off first
            out(suck_do, 1, "suck")
        elif mode == "blow":
            out(suck_do, 0, "suck")   # ensure suck is off first
            out(blow_do, 1, "blow")
        elif mode == "off":
            out(suck_do, 0, "suck")
            out(blow_do, 0, "blow")
        else:
            raise DobotError(-1, f"unknown pump mode {mode!r}", "set_pump")

        return errid, "; ".join(resp)

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

    def servo_j(self, j1, j2, j3, j4, t=0.1):
        """Stream one servo setpoint (degrees). ServoJ is meant to be called
        repeatedly at a steady cadence; `t` is the time to reach this point."""
        cmd = f"ServoJ({j1:.3f},{j2:.3f},{j3:.3f},{j4:.3f},t={t:.3f})"
        return self._move(cmd, raise_on_error=False)

    # -- smooth live following (servo streaming) ------------------------------
    def set_max_velocity(self, deg_per_sec):
        with self._servo_lock:
            self._max_vel = max(1.0, float(deg_per_sec))

    def set_target(self, j1, j2, j3, j4=None):
        """Set the desired joint angles the follower slews toward. J4 is left at
        its initialized (current) angle unless given."""
        with self._servo_lock:
            if self._target is None:
                self._target = [0.0, 0.0, 0.0, 0.0]
            self._target[0] = float(j1)
            self._target[1] = float(j2)
            self._target[2] = float(j3)
            if j4 is not None:
                self._target[3] = float(j4)

    def hold(self):
        """Smooth stop: snap the target to the current setpoint so the follower
        freezes in place within one tick (no decel-to-stop lurch)."""
        with self._servo_lock:
            if self._setpoint is not None:
                self._target = list(self._setpoint)

    def start_servo(self):
        """Begin streaming ServoJ, initialised to the current actual pose so the
        first move doesn't jump. Restarts cleanly if already running."""
        self.stop_servo()
        # wait briefly for live feedback so we start from the real pose
        deadline = time.time() + 1.0
        while time.time() < deadline and not self.get_state()["feedback_ok"]:
            time.sleep(0.02)
        actual = [float(x) for x in self.get_state()["joints"]]
        with self._servo_lock:
            self._setpoint = list(actual)
            self._target = list(actual)
        self._servo_running = True
        with self._state_lock:
            self._state["servo_active"] = True
            self._state["servo_error"] = None
        self._servo_thread = threading.Thread(
            target=self._servo_loop, name="dobot-servo", daemon=True
        )
        self._servo_thread.start()

    def stop_servo(self):
        self._servo_running = False
        thread = self._servo_thread
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=1.0)
        self._servo_thread = None
        with self._state_lock:
            self._state["servo_active"] = False

    def _servo_loop(self):
        interval = 0.08   # send rate (~12.5 Hz)
        t_param = 0.10    # slightly longer than interval for overlap/smoothness
        consecutive_errors = 0
        next_t = time.monotonic()
        while self._servo_running:
            with self._servo_lock:
                target = list(self._target) if self._target is not None else None
                setpoint = list(self._setpoint) if self._setpoint is not None else None
                max_step = self._max_vel * interval
            if target is None or setpoint is None:
                time.sleep(interval)
                continue
            # slew the setpoint toward the target, capped at max_step per joint
            for i in range(4):
                delta = target[i] - setpoint[i]
                if delta > max_step:
                    setpoint[i] += max_step
                elif delta < -max_step:
                    setpoint[i] -= max_step
                else:
                    setpoint[i] = target[i]
            with self._servo_lock:
                self._setpoint = list(setpoint)
            errid, resp = self.servo_j(*setpoint, t=t_param)
            if errid != 0:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    with self._state_lock:
                        self._state["servo_error"] = (
                            f"ServoJ ErrorID {errid}: {resp}"
                        )
                    break
            else:
                consecutive_errors = 0
            # pace the loop to a steady cadence
            next_t += interval
            sleep = next_t - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_t = time.monotonic()
        self._servo_running = False
        with self._state_lock:
            self._state["servo_active"] = False

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
