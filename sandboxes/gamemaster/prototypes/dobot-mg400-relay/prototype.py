"""
Dobot MG400 Relay — two-arm game-master relay server (a Flask blueprint).

This machine OWNS TWO MG400 connections — one per side ("purple" / "green") — and
relays each side's move/pump commands to ITS OWN arm. The two sides are fully
INDEPENDENT and drive concurrently: purple's controller drives the purple arm
while green's controller drives the green arm, with no blocking between sides.

Both arms keep Dobot's factory IP (192.168.1.6) and are told apart by which
LOCAL NETWORK INTERFACE each side's sockets are pinned to (macOS IP_BOUND_IF —
see relay_arm.py and the dualdobottest proof of concept).

Per SIDE the relay still arbitrates: a side must *acquire* its arm and gets an
opaque token + a lease; a per-side watchdog smooth-stops that side's arm if its
holder stops heartbeating. This stops two tabs of the SAME side from fighting,
but it NEVER blocks across sides.

Every command for a side passes the same safety filter (joint limits / reachable
workspace clamp mirrored from the joint and cartesian prototypes) before it
reaches that side's arm.

A single GLOBAL E-STOP latch stops BOTH arms; `clear` un-latches it and clears
both arms' errors.

Loaded by hub.py; registered under /p/dobot-mg400-relay. No app.run() of its own —
it only runs inside the hub server. It needs each arm reachable on the network to
move it; without a robot the operator GUI + every endpoint still work and
move/pump fail cleanly with "Not connected".

Put each robot in API mode first — see ../../docs/operations/dobot-api-mode.md.
Safety: keep the hardware E-stop within reach; start with a low speed.
"""

import json
import math
import os
import sys
import threading
import time
from collections import deque

from flask import Blueprint, jsonify, request, send_from_directory

import live   # shared push helper (prototypes/live.py)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so the sibling driver imports cleanly
from relay_arm import DobotMG400, DobotError  # noqa: E402

MANIFEST = {
    "name": "Dobot MG400 Relay",
    "description": (
        "Two-arm relay: owns BOTH MG400 arms and forwards each side's commands "
        "to its OWN arm. Player controllers connect through its HTTP API.\n"
        "**Need to know to get it working:**\n"
        "- Both robots keep the factory IP `192.168.1.6` — do not change it.\n"
        "- Each robot needs its OWN USB Ethernet dongle on this Mac, cable "
        "straight from dongle to robot (no switch in between).\n"
        "- Purple's dongle: IP `192.168.1.50`. Green's dongle: IP `192.168.1.51`.\n"
        "- Both dongles: subnet mask `255.255.255.0`; leave router and DNS empty.\n"
        "- No interface names needed — the relay finds the right dongle by its IP.\n"
        "- Put both robots in API mode, then Connect each side below."
    ),
    "default_page": "",
    "pages": [{"path": "", "label": "Relay (operator)"}],
}
bp = Blueprint("dobot_mg400_relay", __name__)

# ---- configuration --------------------------------------------------------
# BOTH arms keep Dobot's factory IP; they are told apart by WHICH USB dongle
# (local network interface) the connection leaves through. Each side's sockets
# are pinned to its dongle's interface (macOS IP_BOUND_IF — see
# relay_arm.DobotMG400), the same trick as the dualdobottest proof of concept.
# Only the dongles' FIXED local IPs are configured here; the interface names
# (en10/en11/... — they vary by Mac and USB port) are auto-detected at connect
# time as whichever interface owns that IP. Operator-editable in the GUI / via
# /api/connect ({side, local_ip?, ip?, iface?}).
ROBOT_IP = "192.168.1.6"
DEFAULT_LINKS = {
    "purple": {"local_ip": "192.168.1.50"},
    "green": {"local_ip": "192.168.1.51"},
}

SIDES = ("purple", "green")


@bp.before_request
def _enforce_team_side():
    """Players reach this machine through the hub's shared-API door, which puts
    their authenticated roles in request.environ["hhh.roles"]. A request whose
    roles are ONLY team roles may target ONLY its own side — so a hand-edited
    green controller can never acquire/move/kick the purple arm. Operator
    (gamemaster) sessions and engine deployments without the auth layer are
    unrestricted."""
    if request.method != "POST":
        return None
    roles = request.environ.get("hhh.roles")
    if roles is None:               # engine without the auth layer
        return None
    teams = {r for r in roles if r in SIDES}
    if not teams or roles - teams:  # operator/admin role present → unrestricted
        return None
    side = (request.get_json(silent=True) or {}).get("side")
    if side is not None and side not in teams:
        return jsonify({"ok": False,
                        "error": f"side '{side}' is not your team"}), 403
    return None
MODES = ("joint", "cartesian")
LEASE_SECS = 2.0          # holder must heartbeat within this or it's dropped
WATCHDOG_HZ = 5.0         # how often the watchdog checks each side's lease
COMMAND_LOG_MAX = 160     # recent relay/operator + robot command events
COMMAND_LOG_FILE = os.path.join(HERE, "command-monitor.log")

# Per-joint angle limits (degrees) — mirrored from joint-angles-test/prototype.py.
JOINT_LIMITS = {
    "j1": [-160.0, 160.0],   # base rotation
    "j2": [-25.0, 85.0],     # arm joint 1
    "j3": [-25.0, 105.0],    # arm joint 2
    "j4": [-160.0, 160.0],   # end rotation (R)
}

# Approximate MG400 workspace — mirrored EXACTLY from cartesian-xyz-test/prototype.py.
# The reachable area is an ANNULUS, so X/Y are clamped to a min/max radius too.
WORKSPACE = {
    "x": [-450.0, 450.0],
    "y": [-450.0, 450.0],
    "z": [-150.0, 230.0],
    "r": [-160.0, 160.0],
}
RADIUS_MIN = 150.0   # mm — inside this the arm can't reach (too folded)
RADIUS_MAX = 440.0   # mm — max horizontal reach

# Conservative Tag Game defaults: 10% speed with a gentler ramp/brake profile.
MAX_JOINT_VEL = 12.0    # deg/s
MAX_LIN_VEL = 20.0      # mm/s
MAX_ANG_VEL = 9.0       # deg/s
RAMP_SECS = 0.50        # follower ramp/brake time -> accel = vel-cap / ramp-time

# Air pump box (two-line suck/blow model — see joint prototype).
SUCK_DO_INDEX = 2
BLOW_DO_INDEX = 1

# Per-side arm connections, guarded by _arm_locks[side] while reconnecting.
_robots = {s: None for s in SIDES}
_arm_locks = {s: threading.Lock() for s in SIDES}
# Last link params each side connected with, so /api/reset can rebuild the driver
# (fresh sockets) without the operator re-typing the IP.
_last_link = {s: None for s in SIDES}
# Sampled state (live feedback + lease seconds tick down), so the SSE stream
# re-snapshots on a short interval rather than on a bump. See prototypes/live.py.
_live = live.LiveState()
_command_log = deque(maxlen=COMMAND_LOG_MAX)
_command_log_lock = threading.Lock()
_command_log_seq = 0
_command_log_file_error_reported = False


# ---- arbitration state -----------------------------------------------------
# Guarded by _arb_lock. NEVER do blocking arm socket I/O while holding this lock;
# read what's needed under it, release, then talk to the arm.
_arb_lock = threading.Lock()
_token_counter = 0             # bumps on every acquire → opaque per-side tokens
# Per side: who (if anyone) holds it, the mode they acquired with, and their lease.
_sides = {
    s: {"present": False, "last_seen": 0.0, "token": None, "mode": "joint"}
    for s in SIDES
}


def _arm(side):
    return _robots.get(side)


def _redact_payload(value):
    """Return a JSON-safe, compact payload copy for the operator monitor."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "token":
                out[k] = "..."
            else:
                out[k] = _redact_payload(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_payload(v) for v in value]
    if isinstance(value, float):
        return round(value, 3)
    return value


def _log_command(direction, side, command, payload=None, ok=None, error=None):
    """Append one command monitor entry to memory/disk and wake the SSE stream."""
    global _command_log_seq, _command_log_file_error_reported
    with _command_log_lock:
        _command_log_seq += 1
        entry = {
            "id": _command_log_seq,
            "ts": round(time.time(), 3),
            "time": time.strftime("%H:%M:%S"),
            "direction": direction,  # "incoming" | "robot"
            "side": side,
            "command": command,
            "payload": _redact_payload(payload or {}),
            "ok": ok,
            "error": error,
        }
        _command_log.appendleft(entry)
        try:
            with open(COMMAND_LOG_FILE, "a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            # Logging must never stop a robot command. Report the first failure
            # to the hub console without flooding it on every move event.
            if not _command_log_file_error_reported:
                print(f"dobot relay: cannot write {COMMAND_LOG_FILE}: {exc}", file=sys.stderr)
                _command_log_file_error_reported = True
    _live.bump()


def _command_log_snapshot():
    with _command_log_lock:
        return list(_command_log)


def _incoming_payload(data):
    payload = dict(data or {})
    payload["from"] = request.headers.get("X-HHH-Source") or request.remote_addr or ""
    return payload


def _log_incoming(command, data, ok=None, error=None):
    _log_command("incoming", _side_from(data), command, _incoming_payload(data), ok, error)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _clamp_joints(j1, j2, j3, j4):
    """Clamp a joint target to each joint's limit (mirror joint-angles-test)."""
    return (
        _clamp(j1, *JOINT_LIMITS["j1"]),
        _clamp(j2, *JOINT_LIMITS["j2"]),
        _clamp(j3, *JOINT_LIMITS["j3"]),
        _clamp(j4, *JOINT_LIMITS["j4"]),
    )


def _clamp_pose(x, y, z, r):
    """Clamp a target pose into the (approximate) reachable workspace: Z and R to
    their ranges, X/Y to the reachable annulus, then to the X/Y box. Mirrored
    EXACTLY from cartesian-xyz-test/prototype.py."""
    z = _clamp(z, *WORKSPACE["z"])
    r = _clamp(r, *WORKSPACE["r"])
    radius = math.hypot(x, y)
    if radius == 0.0:
        x, y = RADIUS_MIN, 0.0  # base axis is unreachable; nudge outward
    elif radius > RADIUS_MAX:
        s = RADIUS_MAX / radius
        x, y = x * s, y * s
    elif radius < RADIUS_MIN:
        s = RADIUS_MIN / radius
        x, y = x * s, y * s
    x = _clamp(x, *WORKSPACE["x"])
    y = _clamp(y, *WORKSPACE["y"])
    return x, y, z, r


# ---- Z soft-floor guard ----------------------------------------------------
# A configurable Cartesian Z floor (mm, in the arm's base frame) the TCP may not
# drop below. Cartesian/XYZ targets are clamped directly. Joint targets are
# clamped through a learned local height model z ≈ a + b·J2 + c·J3 per side
# (J1/J4 don't change height), so a descending joint target can be stopped at the
# floor instead of crashing through it. Operator-editable from the relay screen;
# persisted across restarts.
Z_FLOOR_PATH = os.path.join(HERE, "z-floor.json")
DEFAULT_Z_FLOOR = -72.0
_z_lock = threading.Lock()
_z_floor = DEFAULT_Z_FLOOR
_z_floor_enabled = True
_z_samples = {s: deque(maxlen=120) for s in SIDES}  # recent (j2, j3, z) feedback
_z_coef = {s: None for s in SIDES}                  # fitted (a, b, c) or None
_Z_MIN_SAMPLES = 6


def _load_z_floor():
    global _z_floor, _z_floor_enabled
    try:
        with open(Z_FLOOR_PATH) as f:
            data = json.load(f)
        _z_floor = float(data.get("z_floor", DEFAULT_Z_FLOOR))
        _z_floor_enabled = bool(data.get("enabled", True))
    except (OSError, ValueError, TypeError):
        pass


def _save_z_floor():
    try:
        with open(Z_FLOOR_PATH, "w") as f:
            json.dump({"z_floor": _z_floor, "enabled": _z_floor_enabled}, f, indent=2)
    except OSError:
        pass


def _solve3(S, rhs):
    """Solve the 3x3 system S·x = rhs by Cramer's rule. None if near-singular."""
    def det3(m):
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    d = det3(S)
    if abs(d) < 1e-6:
        return None
    out = []
    for col in range(3):
        m = [row[:] for row in S]
        for row in range(3):
            m[row][col] = rhs[row]
        out.append(det3(m) / d)
    return out  # [a, b, c]


def _refit_z_model(side):
    samples = _z_samples[side]
    if len(samples) < _Z_MIN_SAMPLES:
        _z_coef[side] = None
        return
    j2s = [s[0] for s in samples]
    j3s = [s[1] for s in samples]
    zs = [s[2] for s in samples]
    n = len(samples)
    sj2, sj3, sz = sum(j2s), sum(j3s), sum(zs)
    sj2j2 = sum(a * a for a in j2s)
    sj3j3 = sum(a * a for a in j3s)
    sj2j3 = sum(j2s[i] * j3s[i] for i in range(n))
    sj2z = sum(j2s[i] * zs[i] for i in range(n))
    sj3z = sum(j3s[i] * zs[i] for i in range(n))
    # Ridge on the two SLOPE terms only (not the intercept): keeps the fit solvable
    # even when one joint barely moved (its slope shrinks toward 0 instead of making
    # the whole system singular — which previously dropped the model entirely and
    # left the floor unenforced).
    r2 = 1e-3 * sj2j2 + 1e-6
    r3 = 1e-3 * sj3j3 + 1e-6
    S = [[n, sj2, sj3], [sj2, sj2j2 + r2, sj2j3], [sj3, sj2j3, sj3j3 + r3]]
    coef = _solve3(S, [sz, sj2z, sj3z])
    _z_coef[side] = tuple(coef) if coef else None


def _sample_z_model(side, robot):
    """Feed one feedback sample (only if the arm moved enough) and refit."""
    try:
        st = robot.get_state()
        joints, pose = st.get("joints"), st.get("pose")
        j2, j3, z = float(joints[1]), float(joints[2]), float(pose[2])
    except (TypeError, ValueError, IndexError):
        return
    dq = _z_samples[side]
    if dq:
        lj2, lj3, _ = dq[-1]
        if abs(j2 - lj2) < 0.5 and abs(j3 - lj3) < 0.5:
            return
    dq.append((j2, j3, z))
    _refit_z_model(side)


def _apply_z_floor(side, j1, j2, j3, j4, robot):
    """Clamp a joint target so the TCP won't drop below the Z floor. Returns
    (j1, j2, j3, j4, note); note is a human string if anything was changed."""
    with _z_lock:
        floor, enabled = _z_floor, _z_floor_enabled
    if not enabled:
        return j1, j2, j3, j4, None
    try:
        st = robot.get_state()
        joints, pose = st.get("joints"), st.get("pose")
        aj2, aj3, az = float(joints[1]), float(joints[2]), float(pose[2])
    except (TypeError, ValueError, IndexError):
        return j1, j2, j3, j4, None
    coef = _z_coef[side]
    if coef is None:
        # No reliable height model yet — allow the move. We must NOT freeze J2/J3
        # here: freezing them stops the very motion the model is learned from, so
        # the arm could never escape (and an arm resting below the floor would be
        # locked out entirely). Jog J2/J3 around once; the model fits within a
        # second or two and the predictive clamp below takes over.
        return j1, j2, j3, j4, None
    _a, b, c = coef
    dz = b * (j2 - aj2) + c * (j3 - aj3)     # predicted Z change from here to target
    if (az + dz) >= floor or dz >= 0:
        return j1, j2, j3, j4, None           # target stays above floor (or rises)
    alpha = max(0.0, min(1.0, (floor - az) / dz))   # fraction of the descent allowed
    nj2 = aj2 + alpha * (j2 - aj2)
    nj3 = aj3 + alpha * (j3 - aj3)
    return j1, nj2, nj3, j4, f"Z floor {floor:.0f} mm reached — no room to go lower"


def _apply_pose_z_floor(x, y, z, r):
    """Clamp a Cartesian/XYZ target pose so the TCP target never goes below the
    configured Z floor. Returns (x, y, z, r, note)."""
    with _z_lock:
        floor, enabled = _z_floor, _z_floor_enabled
    if not enabled or z >= floor:
        return x, y, z, r, None
    return x, y, floor, r, f"Z floor {floor:.0f} mm reached — target Z clamped"


def _is_operator_request():
    """True for gamemaster/admin sessions (or engine runs without the auth layer)."""
    roles = request.environ.get("hhh.roles")
    if roles is None:
        return True
    return bool(roles - set(SIDES))


_load_z_floor()


def _apply_motion(robot, mode):
    """Push conservative velocity + acceleration caps to the follower for `mode`
    (accel = velocity-cap / ramp-time)."""
    secs = max(0.05, RAMP_SECS)
    if mode == "cartesian":
        robot.set_max_velocity_cartesian(MAX_LIN_VEL, MAX_ANG_VEL)
        robot.set_max_accel_cartesian(MAX_LIN_VEL / secs, MAX_ANG_VEL / secs)
    else:
        robot.set_max_velocity_joint(MAX_JOINT_VEL)
        robot.set_max_accel_joint(MAX_JOINT_VEL / secs)


def _ensure_follower(robot, mode):
    """Make sure the arm is running the follower for `mode` (switching followers if
    a different one is active). Keeps the connection + enabled state intact. No
    arbitration lock is held here — this does blocking arm I/O."""
    if robot is None or not robot.is_connected():
        return
    if not robot.get_state().get("enabled"):
        return
    if robot.control_mode() != mode:
        robot.start_servo(mode)   # start_servo stops any current follower first
        _apply_motion(robot, mode)


# ---- lease / arbitration helpers (call with _arb_lock held) ----------------
def _lease_secs_left_locked(side):
    if _sides[side]["token"] is None:
        return 0.0
    left = LEASE_SECS - (time.time() - _sides[side]["last_seen"])
    return max(0.0, left)


def _touch_locked(side):
    """Refresh a side's presence + lease (call with _arb_lock held)."""
    _sides[side]["present"] = True
    _sides[side]["last_seen"] = time.time()


# ---- programmatic API (for other machines via the hub) ---------------------
def arm_state(side):
    """That side's arm sub-object (same shape as state['arms'][side])."""
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}")
    return _arm_dict(side)


def side_holder(side):
    """The opaque token currently holding `side`, or None if free."""
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}")
    with _arb_lock:
        return _sides[side]["token"]


def full_state():
    """The relay's full state object (same shape as GET /api/state)."""
    return _state_dict()


# ---- watchdog --------------------------------------------------------------
def _watchdog_loop():
    """~5 Hz: for EACH side, if its holder's lease expired, smooth-stop THAT side's
    arm and free that side (mark not present). The other side is unaffected. Arm
    I/O happens outside the lock."""
    period = 1.0 / WATCHDOG_HZ
    while True:
        time.sleep(period)
        # Learn each connected+enabled arm's height model from live feedback so the
        # Z floor can clamp descending joint targets smoothly.
        for s in SIDES:
            robot = _arm(s)
            if robot is not None and robot.is_connected() and robot.get_state().get("enabled"):
                _sample_z_model(s, robot)
        expired = []
        with _arb_lock:
            for s in SIDES:
                if _sides[s]["token"] is not None and _lease_secs_left_locked(s) <= 0.0:
                    expired.append(s)
                    _sides[s]["present"] = False
                    _sides[s]["token"] = None
        for s in expired:
            robot = _arm(s)
            if robot is not None and robot.is_connected():
                try:
                    robot.hold()      # smooth stop; keep servos enabled
                except Exception:     # pragma: no cover - defensive
                    pass
        if expired:
            _live.bump()


_watchdog_thread = threading.Thread(
    target=_watchdog_loop, name="relay-watchdog", daemon=True
)
_watchdog_thread.start()


# ---- response helpers ------------------------------------------------------
def _ok(**kw):
    return jsonify({"ok": True, **kw})


def _fail(error, **kw):
    return jsonify({"ok": False, "error": error, **kw})


def _pump_mode(do_bits):
    suck = bool(do_bits & (1 << (SUCK_DO_INDEX - 1)))
    blow = bool(do_bits & (1 << (BLOW_DO_INDEX - 1)))
    if suck and blow:
        return "conflict"
    if suck:
        return "suck"
    if blow:
        return "blow"
    return "off"


def _arm_dict(side):
    """The arm sub-object for one side (works with no robot connected)."""
    robot = _arm(side)
    raw = DobotMG400._blank_state() if robot is None else robot.get_state()
    return {
        "connected": raw["connected"],
        "robot_mode": raw["robot_mode"],
        "enabled": raw["enabled"],
        "mode_name": raw["mode_name"],
        "joints": raw["joints"],
        "pose": raw["pose"],
        "servo_active": raw["servo_active"],
        "servo_error": raw["servo_error"],
        "error": raw.get("error", False),
        "alarm_ids": raw.get("alarm_ids", []),
        "fault_kind": raw.get("fault_kind"),
        "fault_label": raw.get("fault_label"),
        "stalled": raw.get("stalled", False),
        "stall_error": raw.get("stall_error", 0.0),
        "pump_mode": _pump_mode(raw.get("digital_out", 0)),
        "control_mode": raw.get("control_mode"),
        "target": None if robot is None else robot.get_target(),
    }


def _state_dict():
    """The full relay state (GET /api/state and the SSE stream)."""
    arms = {s: _arm_dict(s) for s in SIDES}
    with _arb_lock:
        sides = {
            s: {
                "present": _sides[s]["present"],
                "lease_secs": round(_lease_secs_left_locked(s), 3),
            }
            for s in SIDES
        }
        state = {
            "arms": arms,
            "sides": sides,
            "command_log": _command_log_snapshot(),
        }
    with _z_lock:
        state["z_floor"] = {
            "value": _z_floor,
            "enabled": _z_floor_enabled,
            "models": {s: (_z_coef[s] is not None) for s in SIDES},
        }
    return state


# ---- pages ----------------------------------------------------------------
@bp.route("/")
def index():
    return send_from_directory(HERE, "controller.html")


@bp.route("/api/config")
def config():
    return jsonify({
        "joint_limits": JOINT_LIMITS,
        "workspace": WORKSPACE,
        "radius_min": RADIUS_MIN,
        "radius_max": RADIUS_MAX,
        "robot_ip": ROBOT_IP,
        "default_links": DEFAULT_LINKS,
        "sides": list(SIDES),
        "modes": list(MODES),
        "lease_secs": LEASE_SECS,
        "z_floor": {"value": _z_floor, "enabled": _z_floor_enabled,
                    "default": DEFAULT_Z_FLOOR},
    })


@bp.route("/api/state")
def state():
    return jsonify(_state_dict())


@bp.route("/api/events")
def events():
    """Push the relay state (both arms' feedback, sides, leases) ~5x/s while it
    changes; the leases count down so it changes every sample."""
    return _live.stream(_state_dict, interval=0.2)


@bp.route("/api/z-floor", methods=["GET", "POST"])
def z_floor():
    """Read or set the Cartesian Z soft floor (mm). GET is open; POST (set
    value/enabled) is gamemaster-only and persisted."""
    global _z_floor, _z_floor_enabled
    if request.method == "GET":
        with _z_lock:
            return _ok(value=_z_floor, enabled=_z_floor_enabled, default=DEFAULT_Z_FLOOR,
                       models={s: (_z_coef[s] is not None) for s in SIDES})
    if not _is_operator_request():
        return _fail("gamemaster required"), 403
    data = request.json or {}
    _log_incoming("z-floor", data)
    with _z_lock:
        if "value" in data:
            try:
                _z_floor = float(data["value"])
            except (TypeError, ValueError):
                return _fail("value must be a number")
        if "enabled" in data:
            _z_floor_enabled = bool(data["enabled"])
        _save_z_floor()
        out = {"value": _z_floor, "enabled": _z_floor_enabled}
    _live.bump()
    return _ok(**out)


# ---- side helpers ----------------------------------------------------------
def _side_from(data):
    side = (data or {}).get("side")
    if side not in SIDES:
        return None
    return side


# ---- operator endpoints (no token) ----------------------------------------
@bp.route("/api/connect", methods=["POST"])
def connect():
    """Connect THAT side's arm (replacing any existing connection for it)."""
    data = request.json or {}
    _log_incoming("connect", data)
    side = _side_from(data)
    if side is None:
        _log_incoming("connect", data, ok=False, error="side must be 'purple' or 'green'")
        return _fail("side must be 'purple' or 'green'")
    ip = (data.get("ip") or ROBOT_IP).strip()
    local_ip = (data.get("local_ip") or DEFAULT_LINKS[side]["local_ip"]).strip()
    iface = (data.get("iface") or "").strip() or None  # optional override
    link = {"ip": ip, "local_ip": local_ip, "iface": iface}
    with _arm_locks[side]:
        if _robots[side] is not None:
            _robots[side].close()
            _robots[side] = None
        robot = DobotMG400(ip, iface=iface, local_ip=local_ip)
        try:
            robot.connect()
        except DobotError as e:
            _log_command("robot", side, "connect", link, ok=False, error=str(e))
            return _fail(f"Could not connect to {ip} via {local_ip}: {e}")
        _robots[side] = robot
    link["iface"] = robot.iface  # the auto-detected (or given) interface
    _last_link[side] = dict(link)
    _log_command("robot", side, "connect", link, ok=True)
    _live.bump()
    return _ok(side=side, **link)


@bp.route("/api/disconnect", methods=["POST"])
def disconnect():
    """Disconnect that side's arm."""
    data = request.json or {}
    _log_incoming("disconnect", data)
    side = _side_from(data)
    if side is None:
        _log_incoming("disconnect", data, ok=False, error="side must be 'purple' or 'green'")
        return _fail("side must be 'purple' or 'green'")
    with _arm_locks[side]:
        if _robots[side] is not None:
            _robots[side].close()
            _robots[side] = None
            _log_command("robot", side, "close", {}, ok=True)
    _live.bump()
    return _ok(side=side)


@bp.route("/api/clear_error", methods=["POST"])
def clear_error():
    """Clear alarms/warnings on that side's arm without enabling or moving it."""
    data = request.json or {}
    _log_incoming("clear_error", data)
    side = _side_from(data)
    if side is None:
        _log_incoming("clear_error", data, ok=False, error="side must be 'purple' or 'green'")
        return _fail("side must be 'purple' or 'green'")
    robot = _arm(side)
    if robot is None or not robot.is_connected():
        _log_command("robot", side, "clear_error", {}, ok=False, error="Not connected")
        return _fail("Not connected")
    try:
        errid, resp = robot.clear_error()
    except DobotError as e:
        _log_command("robot", side, "clear_error", {}, ok=False, error=str(e))
        return _fail(str(e), errid=e.errid)
    ok = errid == 0
    _log_command("robot", side, "clear_error", {"errid": errid, "resp": resp}, ok=ok)
    _live.bump()
    return jsonify({"ok": ok, "errid": errid, "resp": resp, "side": side})


@bp.route("/api/enable", methods=["POST"])
def enable():
    """Enable that side's arm + start its follower in its current mode (default
    "joint"). Idempotent."""
    data = request.json or {}
    _log_incoming("enable", data)
    side = _side_from(data)
    if side is None:
        _log_incoming("enable", data, ok=False, error="side must be 'purple' or 'green'")
        return _fail("side must be 'purple' or 'green'")
    robot = _arm(side)
    if robot is None or not robot.is_connected():
        _log_command("robot", side, "enable", {}, ok=False, error="Not connected")
        return _fail("Not connected")
    with _arb_lock:
        mode = _sides[side]["mode"] if _sides[side]["token"] is not None else "joint"
    try:
        robot.clear_error()
        _log_command("robot", side, "clear_error", {}, ok=True)
    except DobotError:
        pass
    try:
        errid, resp = robot.enable()
    except DobotError as e:
        _log_command("robot", side, "enable", {}, ok=False, error=str(e))
        return _fail(str(e), errid=e.errid)
    if errid == 0:
        robot.start_servo(mode)
        _apply_motion(robot, mode)
        _log_command("robot", side, "enable + start_servo", {"mode": mode}, ok=True)
    else:
        _log_command("robot", side, "enable", {"errid": errid, "resp": resp}, ok=False)
    _live.bump()
    return jsonify({"ok": errid == 0, "errid": errid, "resp": resp, "side": side})


@bp.route("/api/kick", methods=["POST"])
def kick():
    """Force-release that side's controller (smooth-stop that side's arm + free its
    lease). The other side is unaffected."""
    data = request.json or {}
    _log_incoming("kick", data)
    side = _side_from(data)
    if side is None:
        _log_incoming("kick", data, ok=False, error="side must be 'purple' or 'green'")
        return _fail("side must be 'purple' or 'green'")
    kick_side(side)
    return _ok(side=side)


def kick_side(side):
    """Programmatic sibling-prototype helper for releasing one side's relay."""
    if side not in SIDES:
        return False
    with _arb_lock:
        _sides[side]["present"] = False
        _sides[side]["token"] = None
    robot = _arm(side)
    if robot is not None and robot.is_connected():
        try:
            robot.hold()
            _log_command("robot", side, "hold", {"reason": "kick"}, ok=True)
        except Exception:   # pragma: no cover - defensive
            _log_command("robot", side, "hold", {"reason": "kick"}, ok=False, error="hold failed")
    _live.bump()
    return True


# ---- client / side endpoints ----------------------------------------------
def _check_token(data):
    """Validate (side, token) against that side's holder. Returns (side, error)
    where error is None on success. Refreshes the side's lease on success.
    Acquires/releases _arb_lock internally; does no arm I/O."""
    side = _side_from(data)
    if side is None:
        return None, "side must be 'purple' or 'green'"
    token = data.get("token")
    with _arb_lock:
        if _sides[side]["token"] is None or token != _sides[side]["token"]:
            return None, "stale token"
        _touch_locked(side)
    return side, None


@bp.route("/api/acquire", methods=["POST"])
def acquire():
    """Acquire THIS side's arm. Records the mode and mints a fresh token
    (invalidating any old one for that side). NEVER conflicts with the other side -
    purple and green can both hold concurrently (no 409)."""
    global _token_counter
    data = request.json or {}
    _log_incoming("acquire", data)
    side = _side_from(data)
    if side is None:
        _log_incoming("acquire", data, ok=False, error="side must be 'purple' or 'green'")
        return _fail("side must be 'purple' or 'green'"), 400
    mode = (data.get("mode") or "joint").lower()
    if mode not in MODES:
        _log_incoming("acquire", data, ok=False, error="mode must be 'joint' or 'cartesian'")
        return _fail("mode must be 'joint' or 'cartesian'"), 400
    with _arb_lock:
        _token_counter += 1
        token = f"{side}-{_token_counter}"
        _sides[side]["token"] = token
        _sides[side]["mode"] = mode
        _touch_locked(side)
    # If this side's arm is live + enabled, make sure the right follower runs
    # (outside the lock — blocking arm I/O).
    robot = _arm(side)
    if robot is not None and robot.is_connected():
        try:
            _ensure_follower(robot, mode)
            _log_command("robot", side, "ensure_follower", {"mode": mode}, ok=True)
        except Exception:   # pragma: no cover - defensive
            _log_command("robot", side, "ensure_follower", {"mode": mode}, ok=False, error="follower failed")
    _live.bump()
    return _ok(token=token, lease_secs=LEASE_SECS, side=side)


@bp.route("/api/release", methods=["POST"])
def release():
    """Release this side's arm. Requires a valid (side, token)."""
    data = request.json or {}
    _log_incoming("release", data)
    side = _side_from(data)
    if side is None:
        _log_incoming("release", data, ok=False, error="side must be 'purple' or 'green'")
        return _fail("side must be 'purple' or 'green'")
    token = data.get("token")
    with _arb_lock:
        if _sides[side]["token"] is None or token != _sides[side]["token"]:
            _log_incoming("release", data, ok=False, error="stale token")
            return _fail("stale token")
        _sides[side]["token"] = None
        _sides[side]["present"] = False
    robot = _arm(side)
    if robot is not None and robot.is_connected():
        try:
            robot.hold()
            _log_command("robot", side, "hold", {"reason": "release"}, ok=True)
        except Exception:   # pragma: no cover - defensive
            _log_command("robot", side, "hold", {"reason": "release"}, ok=False, error="hold failed")
    _live.bump()
    return _ok(side=side)


@bp.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    """Refresh this side's lease; returns the full state object. Stale token ->
    {ok:false, error:"stale token"}."""
    side, err = _check_token(request.json or {})
    if err:
        return _fail(err)
    return _ok(state=_state_dict())


@bp.route("/api/move", methods=["POST"])
def move():
    """Set this side's follower target after safety-clamping. Requires a valid
    (side, token), and that side's arm connected + enabled."""
    data = request.json or {}
    _log_incoming("move", data)
    side, err = _check_token(data)
    if err:
        _log_incoming("move", data, ok=False, error=err)
        return _fail(err)
    robot = _arm(side)
    if robot is None or not robot.is_connected():
        _log_command("robot", side, "move", {}, ok=False, error="Not connected")
        return _fail("Not connected")
    if not robot.get_state().get("enabled"):
        _log_command("robot", side, "move", {}, ok=False, error="Arm not enabled")
        return _fail("Arm not enabled")
    mode = (data.get("mode") or "joint").lower()
    if mode not in MODES:
        _log_command("robot", side, "move", {"mode": mode}, ok=False, error="bad mode")
        return _fail("mode must be 'joint' or 'cartesian'")
    # Make sure the right follower is running for this command.
    try:
        _ensure_follower(robot, mode)
    except DobotError as e:
        _log_command("robot", side, "ensure_follower", {"mode": mode}, ok=False, error=str(e))
        return _fail(str(e), errid=e.errid)

    if mode == "joint":
        j = data.get("joints")
        if not (isinstance(j, (list, tuple)) and len(j) >= 3):
            return _fail("Expected joints [j1, j2, j3, j4]")
        try:
            j1, j2, j3 = float(j[0]), float(j[1]), float(j[2])
            j4 = float(j[3]) if len(j) > 3 else robot.get_state()["joints"][3]
        except (ValueError, TypeError, IndexError):
            return _fail("joints must be numeric")
        j1, j2, j3, j4 = _clamp_joints(j1, j2, j3, j4)
        _sample_z_model(side, robot)   # learn the height model from live jogging too
        j1, j2, j3, j4, znote = _apply_z_floor(side, j1, j2, j3, j4, robot)
        robot.set_target_joints(j1, j2, j3, j4)
        clamped = {"j1": round(j1, 2), "j2": round(j2, 2),
                   "j3": round(j3, 2), "j4": round(j4, 2)}
        if znote:
            clamped["z_floor"] = znote
        _log_command("robot", side, "set_target_joints", clamped, ok=True)
    else:
        p = data.get("pose")
        if not (isinstance(p, (list, tuple)) and len(p) >= 3):
            return _fail("Expected pose [x, y, z, r]")
        try:
            x, y, z = float(p[0]), float(p[1]), float(p[2])
            r = float(p[3]) if len(p) > 3 else robot.get_state()["pose"][3]
        except (ValueError, TypeError, IndexError):
            return _fail("pose must be numeric")
        x, y, z, r = _clamp_pose(x, y, z, r)
        x, y, z, r, znote = _apply_pose_z_floor(x, y, z, r)
        robot.set_target_pose(x, y, z, r)
        clamped = {"x": round(x, 2), "y": round(y, 2),
                   "z": round(z, 2), "r": round(r, 2)}
        if znote:
            clamped["z_floor"] = znote
        _log_command("robot", side, "set_target_pose", clamped, ok=True)
    _live.bump()
    return _ok(clamped=clamped, side=side)


@bp.route("/api/pump", methods=["POST"])
def pump():
    """Set this side's air pump (suck|blow|off). Requires a valid (side, token)."""
    data = request.json or {}
    _log_incoming("pump", data)
    side, err = _check_token(data)
    if err:
        _log_incoming("pump", data, ok=False, error=err)
        return _fail(err)
    mode = (data.get("mode") or "off").lower()
    if mode not in ("suck", "blow", "off"):
        _log_command("robot", side, "pump", {"mode": mode}, ok=False, error="bad mode")
        return _fail("mode must be 'suck', 'blow' or 'off'")
    robot = _arm(side)
    if robot is None or not robot.is_connected():
        _log_command("robot", side, "pump", {"mode": mode}, ok=False, error="Not connected")
        return _fail("Not connected")
    try:
        errid, resp = robot.set_pump(mode, SUCK_DO_INDEX, BLOW_DO_INDEX)
    except DobotError as e:
        _log_command("robot", side, "set_pump", {"mode": mode}, ok=False, error=str(e))
        return _fail(str(e), errid=e.errid)
    _log_command("robot", side, "set_pump", {"mode": mode, "errid": errid, "resp": resp}, ok=(errid == 0))
    _live.bump()
    return jsonify({"ok": errid == 0, "errid": errid, "resp": resp, "side": side})


@bp.route("/api/hold", methods=["POST"])
def hold():
    """Smooth-stop this side's arm but keep its lease. Requires a valid (side,
    token)."""
    data = request.json or {}
    _log_incoming("hold", data)
    side, err = _check_token(data)
    if err:
        _log_incoming("hold", data, ok=False, error=err)
        return _fail(err)
    robot = _arm(side)
    if robot is not None and robot.is_connected():
        try:
            robot.hold()
            _log_command("robot", side, "hold", {}, ok=True)
        except Exception:   # pragma: no cover - defensive
            _log_command("robot", side, "hold", {}, ok=False, error="hold failed")
    _live.bump()
    return _ok(side=side)
