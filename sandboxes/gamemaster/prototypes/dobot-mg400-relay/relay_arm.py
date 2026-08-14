"""
Dobot MG400 TCP/IP driver — unified relay driver (joint OR cartesian follower).

This merges the joint (ServoJ) and cartesian (ServoP) prototype drivers into ONE
class that owns a single connection and can run EITHER follower at a time. The
two original drivers are ~95% identical (connect/close/feedback parsing/dashboard
commands/_servo_loop math are the same); the only real difference is whether the
streamed setpoint is joint angles (ServoJ) or a tool pose (ServoP). Here both
followers live behind one connection and `start_servo(mode)` picks which runs.

Three communication layers (same as the originals):

  * Dashboard  (29999) — enable/disable/clear/stop/estop/speed/digital outputs.
  * Motion     (30003) — ServoJ (joint) OR ServoP (pose) streamed setpoints.
  * Feedback   (30004) — 1440-byte real-time packet @ ~8 ms: robot mode, actual
                         joint angles (offset 432) AND actual TCP pose (offset 624).

A background thread streams setpoints toward a target with an acceleration-limited
(trapezoidal) velocity profile, so live dragging eases in and out smoothly without
overshoot. Switching mode = stop_servo() then start_servo(other); the connection
and the enabled state are kept intact across a switch (only the follower changes).

Interface pinning (dual arms on ONE IP): both MG400s keep Dobot's factory IP, so
plain sockets can't tell them apart — the OS routes everything to one interface
(binding the source IP alone does NOT fix that; routing is destination-based).
Every socket is therefore pinned to a local network interface with the macOS
IP_BOUND_IF option BEFORE connecting, so each driver instance reaches the arm on
ITS OWN cable (same trick as dualdobottest). Only `local_ip` (the fixed address
of that side's USB dongle) is needed: the interface NAME is auto-detected as
whichever interface owns that IP. Pass `iface` to override the detection.
"""

import socket
import struct
import subprocess
import threading
import time
import json


# ---- ports ----------------------------------------------------------------
PORT_DASHBOARD = 29999
PORT_MOTION = 30003
PORT_FEEDBACK = 30004

IP_BOUND_IF = 25  # macOS setsockopt(IPPROTO_IP, IP_BOUND_IF, ifindex)


def iface_for_ip(local_ip):
    """Which local interface (e.g. "en10") owns `local_ip`, or None. Parses
    ifconfig so config only needs the fixed per-side dongle IP — the interface
    name varies by Mac/USB port and is discovered here."""
    try:
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    iface = None
    for line in out.splitlines():
        if line and not line[0].isspace():
            iface = line.split(":", 1)[0]
        elif f"inet {local_ip} " in line:
            return iface
    return None

# ---- feedback packet (offsets per Dobot's MyType struct) ------------------
FEEDBACK_SIZE = 1440
FEEDBACK_MAGIC = 0x0123456789ABCDEF
OFF_DIGITAL_IN = 8       # int64
OFF_DIGITAL_OUT = 16     # int64
OFF_ROBOT_MODE = 24      # int64
OFF_TEST_VALUE = 48      # int64 (magic, validates alignment)
OFF_Q_ACTUAL = 432       # 6 x double (actual joint angles, degrees)
OFF_TOOL_VECTOR_ACTUAL = 624  # 6 x double (actual TCP pose: x,y,z,rx,ry,rz)

ROBOT_MODES = {
    1: "INIT", 2: "BRAKE_OPEN", 3: "RESERVED", 4: "DISABLED",
    5: "ENABLED (idle)", 6: "BACKDRIVE", 7: "RUNNING", 8: "SINGLE_MOVE",
    9: "ERROR", 10: "PAUSE", 11: "JOG",
}
ENABLED_MODES = {5, 6, 7, 8, 10, 11}
# Modes in which the arm is NOT following our ServoJ/ServoP stream: it is being
# hand-dragged via the unlock button (BACKDRIVE), disabled, faulted, or still
# starting up. While in these the follower must slave its setpoint to the live
# position and stop streaming, so re-locking the arm doesn't snap it back to a
# stale setpoint. (Excludes 5 ENABLED-idle and 7 RUNNING — our normal streaming.)
NO_SERVO_MODES = {1, 2, 3, 4, 6, 9}
# How many consecutive feedback ticks in a NO_SERVO mode before we conclude the
# arm really left servo-follow (debounce against one-frame glitches).
NO_SERVO_TRIP = 3

# Alarm groups from Dobot's official MG400/M1Pro alarm tables. Robot feedback
# reports every alarm as mode 9 (ERROR), so GetErrorID() is required to tell an
# emergency lock from a joint/workspace limit or a collision.
EMERGENCY_ALARM_IDS = {85, 86, 12288, 12289, 20484, 21570}
WORKSPACE_ALARM_IDS = {
    17, 18, 23, 24, 29, 30, 32, 33, 34,
    64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75,
    20480, 20481, 20482, 20483, 36122,
}
COLLISION_ALARM_IDS = {-3, -2, 112, 12294}


def classify_alarm_ids(alarm_ids):
    """Classify Dobot GetErrorID values into operator-relevant stop causes."""
    ids = {int(value) for value in (alarm_ids or [])}
    if ids & EMERGENCY_ALARM_IDS:
        return "emergency_lock", "Emergency stop / safety lock"
    if ids & WORKSPACE_ALARM_IDS:
        return "workspace_limit", "Usable-area or joint-limit stop"
    if ids & COLLISION_ALARM_IDS:
        return "collision", "Collision safety stop"
    if ids:
        return "controller_fault", "Controller fault"
    return "unknown_fault", "Fault reported; alarm code unavailable"


class DobotError(Exception):
    def __init__(self, errid, resp, command):
        self.errid = errid
        self.resp = resp
        self.command = command
        super().__init__(f"{command} -> ErrorID {errid}: {resp}")


def parse_feedback(packet):
    """Parse a 1440-byte feedback packet; return dict or None if misaligned."""
    if len(packet) < FEEDBACK_SIZE:
        return None
    if struct.unpack_from("<Q", packet, OFF_TEST_VALUE)[0] != FEEDBACK_MAGIC:
        return None
    robot_mode = struct.unpack_from("<Q", packet, OFF_ROBOT_MODE)[0]
    di = struct.unpack_from("<Q", packet, OFF_DIGITAL_IN)[0]
    do = struct.unpack_from("<Q", packet, OFF_DIGITAL_OUT)[0]
    q_actual = struct.unpack_from("<6d", packet, OFF_Q_ACTUAL)
    tool = struct.unpack_from("<6d", packet, OFF_TOOL_VECTOR_ACTUAL)
    return {
        "robot_mode": int(robot_mode),
        "digital_in": int(di),
        "digital_out": int(do),
        "joints": [round(v, 3) for v in q_actual[:4]],
        "pose": [round(v, 3) for v in tool[:4]],  # x, y, z, r
    }


# ---- stall / crash detection ----------------------------------------------
# Signature of a wedged/crashed MG400 (observed live): the controller stays
# connected + enabled and ACKS every ServoJ/ServoP with ErrorID 0, but never
# executes them — robot_mode stays idle and the joints don't move. We detect it
# by watching the follower: if it is commanding the arm somewhere it isn't, and
# the arm hasn't physically moved for a grace period, we flag a suspected crash.
STALL_GRACE_SECS = 1.5    # commanded-but-static this long → suspect crash
STALL_MOVE_EPS = 0.2      # deg; joint change above this counts as "the arm moved"
STALL_ERR_JOINT = 3.0     # deg; follower-vs-actual gap that means "told to move"
STALL_ERR_CART = 6.0      # mm; same idea for the cartesian follower


class DobotMG400:
    def __init__(self, ip, iface=None, local_ip=None,
                 connect_timeout=5.0, command_timeout=5.0):
        self.ip = ip
        self.iface = iface          # interface to pin to; auto-detected from local_ip if None
        self.local_ip = local_ip    # fixed address of this side's USB dongle
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

        self._dashboard = None
        self._motion = None
        self._feedback = None

        self._dash_lock = threading.Lock()
        self._motion_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self._feed_thread = None
        self._alarm_thread = None
        self._running = False

        # Follower state. The same setpoint/target/vel arrays serve both
        # followers — they hold joint angles in "joint" mode and a tool pose in
        # "cartesian" mode. `control_mode` says which follower is running (or None).
        self._servo_thread = None
        self._servo_running = False
        self._servo_lock = threading.Lock()
        self._control_mode = None  # "joint" | "cartesian" | None
        self._target = None        # desired [j1..j4] OR [x,y,z,r]
        self._setpoint = None      # current streamed setpoint
        self._vel = [0.0, 0.0, 0.0, 0.0]   # current setpoint velocity per axis
        self._last_motion_ts = 0.0         # last time the arm physically moved (stall detection)

        # Joint follower caps (conservative defaults from the joint prototype).
        self._max_vel = 60.0       # deg/s
        self._max_acc = 240.0      # deg/s^2 (ramp ~0.25 s to 60 deg/s; lower = gentler)
        # Cartesian follower caps (conservative defaults from the cartesian one).
        self._max_lin = 80.0       # mm/s
        self._max_ang = 60.0       # deg/s
        self._max_lin_acc = 400.0  # mm/s^2 (ramp ~0.2 s to 80 mm/s; lower = gentler)
        self._max_ang_acc = 300.0  # deg/s^2

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
            "alarm_ids": [],
            "fault_kind": None,
            "fault_label": None,
            "joints": [0.0, 0.0, 0.0, 0.0],
            "pose": [0.0, 0.0, 0.0, 0.0],
            "target": None,
            "digital_in": 0,
            "digital_out": 0,
            "last_feedback": 0.0,
            "feedback_ok": False,
            "servo_active": False,
            "servo_error": None,
            "control_mode": None,   # which follower is running: joint | cartesian | None
            "stalled": False,       # suspected crash: commanded to move but not moving
            "stall_error": 0.0,     # follower-vs-actual gap (deg or mm) behind `stalled`
        }

    def get_state(self):
        with self._state_lock:
            return dict(self._state)

    def get_target(self):
        """The commanded target the follower is slewing toward, or None before the
        servo loop is initialised. It is joint angles in joint mode and a tool pose
        in cartesian mode (see control_mode)."""
        with self._servo_lock:
            return list(self._target) if self._target is not None else None

    def control_mode(self):
        """Which follower is running: "joint", "cartesian", or None."""
        with self._servo_lock:
            return self._control_mode

    def is_connected(self):
        with self._state_lock:
            return self._state["connected"]

    # -- connection -----------------------------------------------------------
    def _open(self, port, read_timeout):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if self.iface:
                s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF,
                             socket.if_nametoindex(self.iface))
            if self.local_ip:
                s.bind((self.local_ip, 0))
            s.settimeout(self.connect_timeout)
            s.connect((self.ip, port))
        except OSError:
            s.close()
            raise
        s.settimeout(read_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return s

    def connect(self):
        if self.local_ip and not self.iface:
            self.iface = iface_for_ip(self.local_ip)
            if self.iface is None:
                raise DobotError(
                    -1,
                    f"no local interface has IP {self.local_ip} — is that side's "
                    "USB dongle plugged in and configured?",
                    "connect",
                )
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
        # GetErrorID uses the dashboard socket and can take up to its command
        # timeout. Keep that read-only diagnosis off the feedback and watchdog
        # threads so it can never delay live state or lease safety.
        self._alarm_thread = threading.Thread(
            target=self._alarm_loop, name="dobot-alarm-diagnosis", daemon=True
        )
        self._alarm_thread.start()

    def close(self):
        self.stop_servo()
        # Graceful halt: stop any motion the controller still has queued/streaming
        # BEFORE we drop the sockets, so the arm isn't left mid-move to lurch from
        # when the next link opens. Best-effort — the socket may already be gone.
        try:
            if self._dashboard is not None:
                self.stop_robot()
        except (DobotError, OSError):
            pass
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
        if self._alarm_thread and self._alarm_thread.is_alive():
            self._alarm_thread.join(timeout=1.1)
        self._alarm_thread = None
        with self._state_lock:
            self._state = self._blank_state()

    # -- low-level command I/O ------------------------------------------------
    def _recv_response(self, sock):
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
        return self._dash("ResetRobot()")

    def stop_robot(self):
        """Halt any queued/streamed motion on the controller (StopRobot). Used for
        a graceful stop before closing sockets so the arm isn't left mid-ServoJ."""
        return self._dash("StopRobot()", raise_on_error=False)

    def speed_factor(self, ratio):
        ratio = max(1, min(100, int(ratio)))
        return self._dash(f"SpeedFactor({ratio})")

    def set_digital_output(self, index, status, immediate=True):
        value = 1 if status else 0
        cmd = (
            f"DOExecute({int(index)},{value})"
            if immediate
            else f"DO({int(index)},{value})"
        )
        return self._dash(cmd, raise_on_error=False)

    def set_pump(self, mode, suck_do, blow_do):
        """Air pump box (I/O mode): two independent lines (suck / blow). Energise
        at most one; both low = off. See the joint prototype for the rationale."""
        mode = (mode or "").lower()
        resp = []
        errid = 0

        def out(index, value, label):
            nonlocal errid
            e, r = self.set_digital_output(index, value)
            resp.append(f"{label}={r}")
            errid = e or errid

        if mode == "suck":
            out(blow_do, 0, "blow")
            out(suck_do, 1, "suck")
        elif mode == "blow":
            out(suck_do, 0, "suck")
            out(blow_do, 1, "blow")
        elif mode == "off":
            out(suck_do, 0, "suck")
            out(blow_do, 0, "blow")
        else:
            raise DobotError(-1, f"unknown pump mode {mode!r}", "set_pump")
        return errid, "; ".join(resp)

    def get_angle(self):
        """Query the current joint angles via the dashboard. Returns [j1..j4]."""
        _, resp = self._dash("GetAngle()")
        vals = self._extract_floats(resp)
        return vals[:4]

    def get_pose(self):
        """Query the current TCP pose via the dashboard. Returns [x, y, z, r]."""
        _, resp = self._dash("GetPose()")
        vals = self._extract_floats(resp)
        return vals[:4]

    def positive_solution(self, j1, j2, j3, j4, user=0, tool=0):
        """Return the TCP pose predicted by the controller for a joint target.

        The LTX joint controls use this before motion so the target can be
        checked against the Auto PP Cal 2 Cartesian boundary.
        """
        _, resp = self._dash(
            f"PositiveSolution({float(j1):.6f},{float(j2):.6f},"
            f"{float(j3):.6f},{float(j4):.6f},{int(user)},{int(tool)})"
        )
        vals = self._extract_floats(resp)
        if len(vals) < 4:
            raise DobotError(-1, f"invalid PositiveSolution response: {resp}",
                             "PositiveSolution")
        return vals[:4]

    # -- motion ---------------------------------------------------------------
    def joint_move(self, j1, j2, j3, j4):
        """Absolute joint-space move (queued). Returns (errid, resp); does not
        raise so out-of-range targets surface to the UI."""
        return self._move(
            f"JointMovJ({j1:.3f},{j2:.3f},{j3:.3f},{j4:.3f})", raise_on_error=False
        )

    def mov_l(self, x, y, z, r):
        """Point-to-point linear Cartesian move (queued). Returns (errid, resp)."""
        return self._move(
            f"MovL({x:.3f},{y:.3f},{z:.3f},{r:.3f})", raise_on_error=False
        )

    def servo_j(self, j1, j2, j3, j4, t=0.1):
        """Stream one joint servo setpoint (degrees). ServoJ is meant to be sent
        repeatedly at a steady cadence; `t` is the time to reach this point."""
        return self._move(
            f"ServoJ({j1:.3f},{j2:.3f},{j3:.3f},{j4:.3f},t={t:.3f})", raise_on_error=False
        )

    def servo_p(self, x, y, z, r):
        """Stream one Cartesian servo setpoint. ServoP takes no optional params
        and should be sent at <= ~33 Hz."""
        return self._move(
            f"ServoP({x:.3f},{y:.3f},{z:.3f},{r:.3f})", raise_on_error=False
        )

    # -- velocity / accel caps (separate joint vs cartesian setters) ----------
    def set_max_velocity_joint(self, deg_per_sec):
        with self._servo_lock:
            self._max_vel = max(1.0, float(deg_per_sec))

    def set_max_accel_joint(self, deg_per_sec2):
        """Joint follower acceleration cap. Lower = gentler ramp up/down and a
        longer braking distance, which removes the overshoot a hard velocity step
        causes; higher = snappier but can overshoot on stop."""
        with self._servo_lock:
            self._max_acc = max(1.0, float(deg_per_sec2))

    def set_max_velocity_cartesian(self, lin_mm_s, ang_deg_s):
        with self._servo_lock:
            self._max_lin = max(1.0, float(lin_mm_s))
            self._max_ang = max(1.0, float(ang_deg_s))

    def set_max_accel_cartesian(self, lin_mm_s2, ang_deg_s2):
        """Cartesian follower acceleration caps (lin + ang)."""
        with self._servo_lock:
            self._max_lin_acc = max(1.0, float(lin_mm_s2))
            self._max_ang_acc = max(1.0, float(ang_deg_s2))

    # -- targets --------------------------------------------------------------
    def set_target_joints(self, j1, j2, j3, j4=None):
        """Set the desired joint angles the joint follower slews toward. J4 is held
        at its current angle unless given."""
        with self._servo_lock:
            if self._target is None:
                self._target = [0.0, 0.0, 0.0, 0.0]
            self._target[0] = float(j1)
            self._target[1] = float(j2)
            self._target[2] = float(j3)
            if j4 is not None:
                self._target[3] = float(j4)
        self._last_motion_ts = time.time()   # give a fresh move its grace window

    def set_target_pose(self, x, y, z, r=None):
        """Set the desired tool pose the cartesian follower slews toward. R is held
        at its current value unless given."""
        with self._servo_lock:
            if self._target is None:
                self._target = [0.0, 0.0, 0.0, 0.0]
            self._target[0] = float(x)
            self._target[1] = float(y)
            self._target[2] = float(z)
            if r is not None:
                self._target[3] = float(r)
        self._last_motion_ts = time.time()   # give a fresh move its grace window

    def hold(self):
        """Smooth stop: aim the target at the follower's natural braking point so
        it decelerates to rest instead of snapping (which would overshoot)."""
        with self._servo_lock:
            if self._setpoint is None:
                return
            tgt = list(self._setpoint)
            accels = self._accels_locked()
            for i, a in enumerate(accels):
                v = self._vel[i]
                if v:                       # coast to a stop one braking-distance ahead
                    tgt[i] += (1.0 if v > 0 else -1.0) * (v * v) / (2.0 * max(1.0, a))
            self._target = tgt

    # -- smooth live following (ServoJ / ServoP streaming) --------------------
    def _caps_locked(self):
        """Per-axis (velocity, accel) caps for the active follower. Call with the
        servo lock held."""
        if self._control_mode == "cartesian":
            return ((self._max_lin, self._max_lin_acc),
                    (self._max_lin, self._max_lin_acc),
                    (self._max_lin, self._max_lin_acc),
                    (self._max_ang, self._max_ang_acc))
        return ((self._max_vel, self._max_acc),) * 4   # joint: one cap for all four

    def _accels_locked(self):
        """Per-axis accel caps for the active follower (used by hold)."""
        return tuple(a for _, a in self._caps_locked())

    def start_servo(self, mode):
        """Start the follower for `mode` ("joint" or "cartesian"). Seeds the
        setpoint from the live feedback (joint angles or pose) so the arm doesn't
        jump on the first frame. Stops any current follower first."""
        mode = (mode or "joint").lower()
        if mode not in ("joint", "cartesian"):
            raise DobotError(-1, f"unknown servo mode {mode!r}", "start_servo")
        self.stop_servo()
        deadline = time.time() + 1.0
        while time.time() < deadline and not self.get_state()["feedback_ok"]:
            time.sleep(0.02)
        st = self.get_state()
        seed = [float(v) for v in (st["pose"] if mode == "cartesian" else st["joints"])]
        with self._servo_lock:
            self._control_mode = mode
            self._setpoint = list(seed)
            self._target = list(seed)
            self._vel = [0.0, 0.0, 0.0, 0.0]
        self._last_motion_ts = time.time()
        self._servo_running = True
        with self._state_lock:
            self._state["servo_active"] = True
            self._state["servo_error"] = None
            self._state["control_mode"] = mode
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
        with self._servo_lock:
            self._control_mode = None
        with self._state_lock:
            self._state["servo_active"] = False
            self._state["control_mode"] = None
            self._state["stalled"] = False
            self._state["stall_error"] = 0.0

    def _servo_loop(self):
        # ServoJ minimum cycle is ~80 ms (12.5 Hz); ServoP is ~30 ms (≤33 Hz).
        with self._servo_lock:
            cartesian = self._control_mode == "cartesian"
        interval = 0.04 if cartesian else 0.08
        t_param = 0.10    # ServoJ point duration; slightly > interval for overlap
        cmd = "ServoP" if cartesian else "ServoJ"
        consecutive_errors = 0
        no_servo_count = 0
        next_t = time.monotonic()
        while self._servo_running:
            with self._servo_lock:
                target = list(self._target) if self._target is not None else None
                setpoint = list(self._setpoint) if self._setpoint is not None else None
                vel = list(self._vel)
                caps = self._caps_locked()
            if target is None or setpoint is None:
                time.sleep(interval)
                continue
            # Drag/unlock guard. If the arm leaves servo-follow (unlock button →
            # BACKDRIVE, disabled, or faulted) it is NOT consuming our stream. We
            # must NOT keep firing ServoJ/ServoP at it — continuing to do so does
            # nothing (the points are ACKed but ignored) and, across unlock/lock
            # cycles, wedges the controller. So: re-seed to the live position (so a
            # one-frame glitch can't snap it), and if it stays out of servo mode,
            # STOP the follower entirely. A fresh Enable re-arms a clean session.
            st = self.get_state()
            if st.get("robot_mode") in NO_SERVO_MODES:
                if st.get("feedback_ok"):
                    actual = list(st["pose"] if cartesian else st["joints"])
                    with self._servo_lock:
                        self._setpoint = list(actual)
                        self._target = list(actual)
                        self._vel = [0.0, 0.0, 0.0, 0.0]
                no_servo_count += 1
                consecutive_errors = 0
                if no_servo_count >= NO_SERVO_TRIP:
                    with self._state_lock:
                        self._state["servo_error"] = (
                            "arm left servo mode (unlocked / disabled / faulted) — "
                            "press Enable to resume control"
                        )
                    break   # stop the follower; an explicit Enable restarts it
                next_t += interval
                pause = next_t - time.monotonic()
                if pause > 0:
                    time.sleep(pause)
                else:
                    next_t = time.monotonic()
                continue
            no_servo_count = 0
            dt = interval
            # Cartesian XYZ and joint J1-J4 each share one path-progress value.
            # Independent per-axis slewing bows away from the validated segment
            # whenever distances differ. A shared scalar keeps every streamed
            # setpoint on the path checked by the Auto PP Cal 2 joint guard.
            # Cartesian tool rotation remains an independent scalar axis.
            scalar_axes = 3 if cartesian else 4
            first_scalar_axis = scalar_axes
            if scalar_axes:
                delta = [target[i] - setpoint[i] for i in range(scalar_axes)]
                distance = sum(value * value for value in delta) ** 0.5
                current_speed = sum(vel[i] * vel[i] for i in range(scalar_axes)) ** 0.5
                velocity_cap = self._max_lin if cartesian else caps[0][0]
                acceleration = self._max_lin_acc if cartesian else caps[0][1]
                desired_speed = min(velocity_cap, (2.0 * acceleration * distance) ** 0.5)
                speed_delta = max(-acceleration * dt,
                                  min(acceleration * dt, desired_speed - current_speed))
                next_speed = max(0.0, current_speed + speed_delta)
                travel = min(distance, next_speed * dt)
                if distance <= 1e-9 or travel >= distance:
                    for i in range(scalar_axes):
                        setpoint[i] = target[i]
                        vel[i] = 0.0
                else:
                    direction = [value / distance for value in delta]
                    for i in range(scalar_axes):
                        setpoint[i] += direction[i] * travel
                        vel[i] = direction[i] * next_speed

            # Cartesian tool rotation retains its independent scalar slew.
            for i in range(first_scalar_axis, 4):
                v_max, a = caps[i]
                remaining = target[i] - setpoint[i]
                dist = abs(remaining)
                direction = 1.0 if remaining >= 0 else -1.0
                v_brake = (2.0 * a * dist) ** 0.5        # fastest we can still stop from
                v_des = min(v_max, v_brake) * direction   # desired signed velocity
                dv = v_des - vel[i]                        # ramp velocity by <= a*dt
                max_dv = a * dt
                dv = max(-max_dv, min(max_dv, dv))
                vel[i] += dv
                step = vel[i] * dt
                if abs(step) >= dist and (step >= 0) == (remaining >= 0):
                    setpoint[i] = target[i]               # would reach/pass it this tick
                    vel[i] = 0.0
                else:
                    setpoint[i] += step
            with self._servo_lock:
                self._setpoint = list(setpoint)
                self._vel = list(vel)
            if cartesian:
                errid, resp = self.servo_p(*setpoint)
            else:
                errid, resp = self.servo_j(*setpoint, t=t_param)
            if errid != 0:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    with self._state_lock:
                        self._state["servo_error"] = f"{cmd} ErrorID {errid}: {resp}"
                    break
            else:
                consecutive_errors = 0
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
        out = []
        for tok in self._extract_braces(resp).split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                out.append(float(tok))
            except ValueError:
                pass
        return out

    def get_error_id(self):
        _, resp = self._dash("GetErrorID()", raise_on_error=False)
        try:
            nested = json.loads(self._extract_braces(resp))
        except (ValueError, TypeError):
            return []
        ids = []

        def walk(x):
            if isinstance(x, list):
                for v in x:
                    walk(v)
            elif isinstance(x, (int, float)) and int(x) != 0:
                ids.append(int(x))

        walk(nested)
        return ids

    def _alarm_loop(self):
        """Poll alarm IDs only while feedback reports ERROR.

        Robot mode alone cannot identify the cause: emergency lock, collision,
        and workspace-limit faults all use mode 9. This loop is intentionally
        read-only and never clears, enables, or moves the arm.
        """
        while self._running:
            with self._state_lock:
                faulted = self._state.get("error", False)
                connected = self._state.get("connected", False)
                if not faulted:
                    self._state["alarm_ids"] = []
                    self._state["fault_kind"] = None
                    self._state["fault_label"] = None
            if not connected or not faulted:
                time.sleep(0.25)
                continue
            try:
                alarm_ids = self.get_error_id()
            except (DobotError, OSError):
                time.sleep(1.0)
                continue
            fault_kind, fault_label = classify_alarm_ids(alarm_ids)
            with self._state_lock:
                if self._state.get("error", False):
                    self._state["alarm_ids"] = alarm_ids
                    self._state["fault_kind"] = fault_kind
                    self._state["fault_label"] = fault_label
            time.sleep(1.0)

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
            while len(buf) >= FEEDBACK_SIZE:
                parsed = parse_feedback(buf[:FEEDBACK_SIZE])
                if parsed is None:
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
        now = time.time()
        # Snapshot the follower's intent first (avoid nesting the servo lock inside
        # the state lock — the servo loop never holds both at once).
        with self._servo_lock:
            running = self._servo_running
            ctrl = self._control_mode
            setpoint = list(self._setpoint) if self._setpoint is not None else None
        with self._state_lock:
            prev_joints = self._state["joints"]
            self._state["robot_mode"] = mode
            self._state["mode_name"] = ROBOT_MODES.get(mode, f"UNKNOWN({mode})")
            self._state["enabled"] = mode in ENABLED_MODES
            self._state["error"] = mode == 9
            self._state["joints"] = parsed["joints"]
            self._state["pose"] = parsed["pose"]
            self._state["digital_in"] = parsed["digital_in"]
            self._state["digital_out"] = parsed["digital_out"]
            self._state["last_feedback"] = now
            self._state["feedback_ok"] = True
            # --- suspected-crash detection (see notes near the constants) ---
            if prev_joints and max(abs(a - b) for a, b in zip(parsed["joints"], prev_joints)) > STALL_MOVE_EPS:
                self._last_motion_ts = now          # the arm physically moved
            err = 0.0
            stalled = False
            if running and ctrl and setpoint is not None:
                if ctrl == "cartesian":
                    err = max(abs(setpoint[i] - parsed["pose"][i]) for i in range(3))
                    thr = STALL_ERR_CART
                else:
                    err = max(abs(s - a) for s, a in zip(setpoint, parsed["joints"]))
                    thr = STALL_ERR_JOINT
                # Follower is commanding motion the arm isn't executing for too long.
                stalled = (err > thr) and (now - self._last_motion_ts > STALL_GRACE_SECS)
            self._state["stalled"] = stalled
            self._state["stall_error"] = round(err, 2)
