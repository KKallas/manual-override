# Dobot MG400 Relay (two arms)

One game-master machine that owns **both** robot arms — **purple** and **green** —
and forwards each player's commands to *their own* arm, with safety clamps in
between. Players never talk to a robot directly; they talk to this relay.
Mounted at `/p/dobot-mg400-relay`.

## Get it working ASAP

### 1. Plug in the hardware

- **2 USB Ethernet dongles**, both into the gamemaster Mac.
- **2 Ethernet cables**, each straight from a dongle to a robot —
  **no switch or router in between**.
- purple robot ↔ dongle #1, green robot ↔ dongle #2.

### 2. Give the dongles their fixed IPs

System Settings → Network → (the dongle) → Details → TCP/IP:

- Configure IPv4: **Manually**
- purple's dongle: IP **192.168.1.50**
- green's dongle: IP **192.168.1.51**
- subnet mask (both): **255.255.255.0**
- router (gateway) and DNS: leave **empty**

Do **not** touch the robots' own IP — both stay on the Dobot factory default
`192.168.1.6`. These numbers are fixed and the same everywhere in this project.

### 3. Put both robots in API mode

See `../../docs/operations/dobot-api-mode.md`. Keep each robot's hardware
E-stop within reach and start at a low speed.

### 4. Connect and drive

- Start the hub (`python hub.py` in the repo root) and open the
  **Game Master** sandbox → **Dobot MG400 Relay**.
- Press **Connect** for each side, then **Enable**.
- Use **Following speed** below the Z soft limit to set the shared TCP path,
  TCP rotation, joint path, and ramp/braking limits for both arms.
- Players: open the controller in your own sandbox and press **Connect** —
  it links through the relay to your own color automatically.

### If something's off

- *"no local interface has IP 192.168.1.5x"* → that dongle isn't plugged in or
  isn't configured. Check with `ifconfig | grep 192.168.1.5`.
- Purple GUI moves the **green** arm? The cables are crossed — swap the two
  robot cables (or the dongles' `.50`/`.51` IPs).
- You never need the interface names (`en10`, `en11`, … vary by Mac and USB
  port) — the relay finds the right dongle by its IP at connect time.

## How it tells two identical robots apart

Both arms answer on the **same IP** (`192.168.1.6`), so plain sockets can't
tell them apart — the OS would route everything out one port (binding the
source IP alone doesn't help; routing is destination-based). The relay
therefore pins every socket (dashboard / motion / feedback) to its side's
dongle with the macOS `IP_BOUND_IF` option *before* connecting — the same
trick as the `dualdobottest` proof of concept. The interface name is
auto-detected as whichever interface owns the side's fixed dongle IP.

`relay_arm.py` is the unified MG400 driver (merging the joint ServoJ and
cartesian ServoP drivers): one class, one connection per arm, either follower.
The relay instantiates it once per side; `start_servo(mode)` picks the
follower and `control_mode()` reports which one runs.

The two sides are **independent** and drive **concurrently** — purple's
controller drives the purple arm while green's drives the green arm, with no
blocking across sides. Per side the relay arbitrates: a controller must
*acquire* its arm (opaque token + ~2 s lease, refreshed by heartbeats); a
watchdog smooth-stops a side's arm if its holder goes quiet. Everything works
with no robot connected — an arm just reports `connected:false` and that
side's `move`/`pump` fail cleanly.

## Reference — the HTTP contract (what clients depend on)

Sides: `purple`, `green`. Modes: `joint`, `cartesian`. `LEASE_SECS = 2.0`.
Shared robot IP: `192.168.1.6` (both arms). Per-side default links (the fixed
dongle IP each side's sockets are pinned to; interface name auto-detected):
`{"purple": {"local_ip": "192.168.1.50"}, "green": {"local_ip": "192.168.1.51"}}`.

### State

`GET /api/state` (also SSE `GET /api/events`, sampled ~5x/s):

```jsonc
{
  "arms": {
    "purple": {"connected": bool, "enabled": bool, "mode_name": str,
               "joints": [j1,j2,j3,j4], "pose": [x,y,z,r],
               "servo_active": bool, "servo_error": str|null,
               "alarm_ids": [int, ...],
               "fault_kind": "emergency_lock|workspace_limit|collision|controller_fault|unknown_fault|null",
               "fault_label": str|null,
               "pump_mode": "off|suck|blow|conflict",
               "control_mode": "joint|cartesian|null",
               "target": [..]|null},
    "green":  { ...same shape... }
  },
  "sides": {
    "purple": {"present": bool, "lease_secs": float},
    "green":  { ...same... }
  },
  "motion_settings": {
    "tcp_xyz": 20.0, "tcp_rotation": 9.0,
    "joint": 12.0, "ramp_secs": 0.5
  }
}
```

`pump_mode` is derived from each arm's digital-out bits: suck = index 2, blow =
index 1; both set = `conflict`. `control_mode` is that arm's running follower.
When `robot_mode` is ERROR, the relay reads `GetErrorID()` in a separate,
read-only background thread and uses `fault_kind` to distinguish an emergency
lock from a workspace/joint-limit stop, collision stop, or another controller
fault. A regular software hold remains enabled/idle and has no `fault_kind`.

### Operator endpoints (no token)

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/connect` | `{side, local_ip?, ip?, iface?}` | Connect THAT side's arm via its pinned dongle, replacing any existing connection for it. Omitted fields fall back to that side's default dongle IP / the shared robot IP; `iface` overrides the auto-detection. |
| POST | `/api/disconnect` | `{side}` | Disconnect that side's arm. |
| POST | `/api/enable` | `{side}` | Enable that side's arm + start its follower in its current mode (else `joint`). Idempotent. Clears the arm's error first. (Also callable by clients.) |
| POST | `/api/kick` | `{side}` | Force-release that side's controller; smooth-stop that side's arm. |
| GET/POST | `/api/motion-settings` | `{tcp_xyz?, tcp_rotation?, joint?, ramp_secs?}` | Read or update the persistent follower settings shared by both arms. Updates apply immediately to connected arms and to later connects/mode switches. |

### Client / side endpoints (operate on the side's OWN arm)

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/acquire` | `{side, mode}` | Acquire THIS side's arm; mint a token. NEVER 409 across sides — both sides can hold concurrently. |
| POST | `/api/release` | `{side, token}` | Release this side's arm. |
| POST | `/api/heartbeat` | `{side, token}` | Refresh this side's lease; returns the full state. Stale token → `{ok:false, error:"stale token"}`. |
| POST | `/api/move` | `{side, token, mode, joints?\|pose?}` | Safety-clamp then set that side's follower target. |
| POST | `/api/pump` | `{side, token, mode}` | Set that side's pump `suck\|blow\|off`. |
| POST | `/api/hold` | `{side, token}` | Smooth-stop that side's arm, keep its lease. |

Player relay clients may also call `/api/connect` for their side if the operator
has not connected that arm yet. This lets a player sandbox bring up its own arm
through the relay without first opening the operator page. Requests whose
authenticated roles are only team roles (see the hub's `shared_api` door) are
rejected with 403 unless `side` is their own team.

The operator command monitor is also persisted as newline-delimited JSON in
`command-monitor.log` in this directory. Each command is appended immediately;
the on-page monitor still keeps only the latest 160 entries.

Tokens are opaque strings (`f"{side}-{counter}"`). A new `acquire` for a side
invalidates that side's old token; any token mismatch on
`release`/`heartbeat`/`move`/`pump`/`hold` fails with `"stale token"`.

### Programmatic API (for other machines via the hub)

- `arm_state(side)` → that side's arm sub-object (same shape as `state["arms"][side]`).
- `side_holder(side)` → the opaque token currently holding `side`, or `None`.
- `full_state()` → the full relay state (same shape as `GET /api/state`).

## Safety notes

- **Per-side ownership.** Each side has its own lease + token; two tabs of the
  same side can't fight (a fresh `acquire` invalidates the old token). The two
  sides never block each other.
- **Per-side watchdog (~5 Hz).** If a side's holder lease expires (no contact
  within `LEASE_SECS`), THAT side's arm is smooth-stopped (`hold()`) and the side
  is freed. The other side is unaffected.
- **No software E-STOP.** Stopping is mechanical — use each robot's hardware
  E-stop button. The relay has no global software stop/latch to get stuck in.
- **Safety clamps** (per arm) mirror the test machines exactly:
  - joint: each angle clamped to `JOINT_LIMITS`
    (J1 ±160, J2 −25…85, J3 −25…105, J4 ±160 deg);
  - cartesian: Z clamped to −150…230 mm, R to ±160°, and X/Y to the reachable
    annulus (radius 150…440 mm) then the ±450 mm box.
- **Shared following limits.** The operator controls both arms together. Defaults
  are 20 mm/s TCP XYZ, 9 deg/s TCP rotation, 12 deg/s joint-space path, and a
  0.50 s acceleration/braking ramp. Settings persist in `motion-settings.json`.
