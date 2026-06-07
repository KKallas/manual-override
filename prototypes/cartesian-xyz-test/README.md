# Cartesian XYZ Test (prototype)

Second hardware prototype for **Manual Override**. Like the
[Joint Angles test](../joint-angles-test/), but the sliders drive the **Cartesian
tool pose** (X, Y, Z in mm, R in degrees) in the robot's **TCP workspace** instead
of individual joints. The MG400's controller does the inverse kinematics; you
think in workspace coordinates.

This is the "think in planes" follow-on: joint control first, then Cartesian.

## What it implements

| Channel | Port | Used for |
|---------|------|----------|
| Dashboard | 29999 | enable / disable / clear / stop / e-stop / speed / pump |
| Motion | 30003 | **`ServoP`** streamed pose setpoints (and `MovL` available) |
| Feedback | 30004 | robot mode, joints, and **actual TCP pose** (`tool_vector_actual`) |

- **`dobot.py`** — driver with a Cartesian follower: streams `ServoP` toward the
  target pose while velocity-limiting (separate linear mm/s and rotational deg/s
  caps).
- **`prototype.py`** — Flask blueprint (REST API + serves the page); clamps
  targets to the reachable workspace. Mounted by the hub — see [../README.md](../README.md).
- **`index.html`** — X / Y / Z / R sliders, live pose readout, pump, stop/e-stop.

## Before you run

Put the robot in **API mode** and set up networking first — see
[../../docs/operations/dobot-api-mode.md](../../docs/operations/dobot-api-mode.md).

## Run

This prototype runs inside the **prototype hub** (it has no standalone server):

```bash
cd prototypes
pip install -r requirements.txt
python hub.py            # then open http://localhost:8000
```

In the hub, choose the **Cartesian XYZ Test** tab, enter the robot IP, then
**Connect → Enable**. On Enable the sliders auto-sync to the current pose so the
first move doesn't jump. Drag **X / Y / Z / R** and the tool follows smoothly.

## Smooth following (same model as the joint demo)

A slider updates a **target pose**; a background thread streams `ServoP` at ~25 Hz
(ServoP's minimum cycle is ~30 ms), slewing the commanded pose toward the target
with a per-tick cap derived from the **Following speed** slider. So dragging gives
continuous, bounded-speed Cartesian motion instead of queued point-to-point moves.

## Workspace clamping

The MG400's reachable area is an **annulus, not a box**, so targets are clamped:

- **Z** and **R** to their configured ranges, and
- **X/Y** to a reachable **radius ring** (`RADIUS_MIN`–`RADIUS_MAX` from the base
  axis).

Anything still unreachable (e.g. a singular pose) is rejected by the controller
as a `ServoP` error; after three in a row the follower stops and the reason is
shown in the command log. Limits live in `prototype.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `WORKSPACE` | x/y ±450, z −150..230, r ±160 | per-axis slider ranges (mm / deg) |
| `RADIUS_MIN` / `RADIUS_MAX` | `150` / `440` mm | reachable X/Y ring |
| `MAX_LIN_VEL` / `MAX_ANG_VEL` | `200` mm/s / `90` deg/s | following speed at 100% |

> These are **approximate** starting values — verify and tighten against your
> robot. The controller is the final authority on what's reachable.

## Stop, errors, pump

Same behaviour as the joint prototype: **Stop motion** freezes the pose,
**Emergency Stop** (or **Esc**) cuts servo power, controller errors surface in the
log, and the **air pump** uses the two-line suck/blow model (configurable
`SUCK_DO_INDEX` / `BLOW_DO_INDEX`).

## Safety

Keep the **hardware E-stop** in reach, start with a **low speed**, and keep the
first targets near the current pose. Cartesian moves can swing the arm widely
between two nearby-looking poses (different arm configurations) — go slow.
