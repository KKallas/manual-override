# Dobot MG400 Relay (two arms)

A game-master **relay** machine for the Manual Override hub. It owns **two** MG400
connections — one per side, **purple** and **green** — and routes each side's
commands to *its own* arm, applying a safety filter, then exposes an HTTP + SSE
API that remote client controllers talk to. Mounted at `/p/dobot-mg400-relay`.

The two sides are **independent** and drive **concurrently**: purple's controller
drives the purple arm while green's controller drives the green arm. There is no
blocking across sides.

Per side the relay still arbitrates: a side must *acquire its arm* and gets an
opaque token + a lease. This stops two tabs of the **same** side from fighting,
but it **never** conflicts across sides (no 409). A per-side watchdog smooth-stops
a side's arm if its holder stops heartbeating. Every command for a side passes the
same safety clamps as the joint / cartesian test machines.

The operator GUI and every endpoint work with **no robot connected** — an arm just
reports `connected:false` and that side's `move`/`pump` fail cleanly.

## Driver

`relay_arm.py` is the unified MG400 driver merging the joint (ServoJ) and
cartesian (ServoP) drivers into one class that owns a single connection and runs
EITHER follower. The relay instantiates it **once per side**. `start_servo(...)`
picks the follower; switching modes stops the old follower and starts the other
while keeping the connection + enabled state intact. `control_mode()` reports
which is running.

## Contract (what clients depend on)

Sides: `purple`, `green`. Modes: `joint`, `cartesian`. `LEASE_SECS = 2.0`.
Per-side default arm IPs (operator-editable; placeholders):
`{"purple": "192.168.1.6", "green": "192.168.1.7"}`.

### State

`GET /api/state` (also SSE `GET /api/events`, sampled ~5x/s):

```jsonc
{
  "arms": {
    "purple": {"connected": bool, "enabled": bool, "mode_name": str,
               "joints": [j1,j2,j3,j4], "pose": [x,y,z,r],
               "servo_active": bool, "servo_error": str|null,
               "pump_mode": "off|suck|blow|conflict",
               "control_mode": "joint|cartesian|null",
               "ip": str|null, "target": [..]|null},
    "green":  { ...same shape... }
  },
  "estop": bool,                 // GLOBAL latch; stops BOTH arms
  "sides": {
    "purple": {"present": bool, "last_seen": float, "lease_secs": float},
    "green":  { ...same... }
  }
}
```

`pump_mode` is derived from each arm's digital-out bits: suck = index 2, blow =
index 1; both set = `conflict`. `control_mode` is that arm's running follower.

### Operator endpoints (no token)

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/connect` | `{side, ip}` | Connect THAT side's arm, replacing any existing connection for it. |
| POST | `/api/disconnect` | `{side}` | Disconnect that side's arm. |
| POST | `/api/enable` | `{side}` | Enable that side's arm + start its follower in its current mode (else `joint`). Idempotent. |
| POST | `/api/disable` | `{side}` | Stop that side's follower + disable that side's arm. |
| POST | `/api/clear` | — | `clear_error()` on BOTH arms AND un-latch the GLOBAL E-STOP. |
| POST | `/api/estop` | — | Latch GLOBAL E-STOP, stop follower + emergency-stop BOTH arms. (Also callable by clients.) |
| POST | `/api/kick` | `{side}` | Force-release that side's controller; smooth-stop that side's arm. |

### Client / side endpoints (operate on the side's OWN arm)

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/acquire` | `{side, mode}` | Acquire THIS side's arm; mint a token. NEVER 409 across sides — both sides can hold concurrently. |
| POST | `/api/release` | `{side, token}` | Release this side's arm. |
| POST | `/api/heartbeat` | `{side, token}` | Refresh this side's lease; returns the full state. Stale token → `{ok:false, error:"stale token"}`. |
| POST | `/api/move` | `{side, token, mode, joints?\|pose?}` | Safety-clamp then set that side's follower target. |
| POST | `/api/pump` | `{side, token, mode}` | Set that side's pump `suck\|blow\|off`. |
| POST | `/api/hold` | `{side, token}` | Smooth-stop that side's arm, keep its lease. |

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
  is freed. The other side is unaffected — a dropped client can never leave its
  arm driving.
- **GLOBAL E-STOP is latched.** While latched, `move`/`pump`/`enable` are refused
  on BOTH sides; only `estop` and `clear` work. `clear` un-latches and clears both
  arms' errors.
- **Safety clamps** (per arm) mirror the test machines exactly:
  - joint: each angle clamped to `JOINT_LIMITS`
    (J1 ±160, J2 −25…85, J3 −25…105, J4 ±160 deg);
  - cartesian: Z clamped to −150…230 mm, R to ±160°, and X/Y to the reachable
    annulus (radius 150…440 mm) then the ±450 mm box.
- Keep each hardware E-stop within reach and start with a low speed. Put each
  robot in API mode first — see `../../docs/operations/dobot-api-mode.md`.
