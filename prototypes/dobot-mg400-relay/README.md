# Dobot MG400 Relay

A game-master **relay** machine for the Manual Override hub. It owns the single
MG400 connection and arbitrates control between two remote sides — **purple** and
**green** — applying a safety filter, then exposes an HTTP + SSE API that remote
client controllers talk to. Mounted at `/p/dobot-mg400-relay`.

Unlike the joint / cartesian test machines (which drive the arm directly from one
window), this machine never opens the arm to more than one side at a time. A side
must *acquire the floor*; only the holder's `move`/`pump` commands are forwarded,
and they pass through the same safety clamps as the originals.

The operator GUI and every endpoint work with **no robot connected** — the arm
just reports `connected:false` and `move`/`pump` fail cleanly.

## Driver

`relay_arm.py` is a unified MG400 driver merging the joint (ServoJ) and cartesian
(ServoP) drivers into one class that owns a single connection and runs EITHER
follower. `start_servo("joint"|"cartesian")` picks the follower; switching modes
stops the old follower and starts the other while keeping the connection +
enabled state intact. `control_mode()` reports which is running.

## Contract (what clients depend on)

Sides: `purple`, `green`. Modes: `joint`, `cartesian`. `LEASE_SECS = 2.0`.

### State

`GET /api/state` (also SSE `GET /api/events`, sampled ~5x/s):

```jsonc
{
  "arm": {"connected": bool, "enabled": bool, "mode_name": str,
          "joints": [j1,j2,j3,j4], "pose": [x,y,z,r],
          "servo_active": bool, "servo_error": str|null,
          "pump_mode": "off|suck|blow|conflict",
          "control_mode": "joint|cartesian|null", "ip": str|null},
  "estop": bool,                 // latched
  "holder": "purple"|"green"|null,
  "lease_secs": float,           // seconds left on the holder's lease (0 if none)
  "sides": {"purple": {"present": bool, "last_seen": float},
            "green":  {"present": bool, "last_seen": float}},
  "target": [..]|null
}
```

`pump_mode` is derived from the digital-out bits: suck = index 2, blow = index 1;
both set = `conflict`.

### Operator endpoints (no token)

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/connect` | `{ip}` | Replace any connection; start feedback. |
| POST | `/api/disconnect` | — | Close the connection. |
| POST | `/api/enable` | — | Enable + start the follower in the holder's mode (else `joint`). Idempotent. |
| POST | `/api/disable` | — | Stop follower + disable. |
| POST | `/api/clear` | — | `clear_error()` AND un-latch the E-STOP. |
| POST | `/api/estop` | — | Latch E-STOP, stop follower, emergency-stop. (Also callable by clients.) |
| POST | `/api/kick` | `{side}` | Force-release that side; smooth-stop the arm. |

### Client / side endpoints

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/acquire` | `{side, mode}` | Grant the floor if free or already held by `side`; mint a token. `409` if the other side holds it. |
| POST | `/api/release` | `{side, token}` | Release the floor. |
| POST | `/api/heartbeat` | `{side, token}` | Refresh the lease; returns the full state. |
| POST | `/api/move` | `{side, token, mode, joints?\|pose?}` | Safety-clamp then set the follower target. |
| POST | `/api/pump` | `{side, token, mode}` | Set pump `suck\|blow\|off`. |
| POST | `/api/hold` | `{side, token}` | Smooth-stop, keep the floor. |

Tokens are opaque strings (`f"{side}-{counter}"`). A new `acquire` invalidates the
old token for that side; any token mismatch on `release`/`heartbeat`/`move`/`pump`
fails.

## Safety notes

- **Single owner.** Exactly one side holds the floor; the other's commands are
  rejected (`"you do not hold the floor"`).
- **Watchdog (~5 Hz).** If the holder's lease expires (no contact within
  `LEASE_SECS`), the arm is smooth-stopped (`hold()`) and the floor is freed —
  a dropped client can never leave the arm driving.
- **E-STOP is latched.** While latched, `move`/`pump`/`enable` are refused; only
  `estop` and `clear` work. `clear` un-latches.
- **Safety clamps** mirror the test machines exactly:
  - joint: each angle clamped to `JOINT_LIMITS`
    (J1 ±160, J2 −25…85, J3 −25…105, J4 ±160 deg);
  - cartesian: Z clamped to −150…230 mm, R to ±160°, and X/Y to the reachable
    annulus (radius 150…440 mm) then the ±450 mm box.
- Keep the hardware E-stop within reach and start with a low speed. Put the robot
  in API mode first — see `../../docs/operations/dobot-api-mode.md`.
