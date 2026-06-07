# Joint Angles Test (prototype)

Drive the MG400's **joint angles** (J1, J2, J3, J4 in degrees) directly. Each
slider commands one joint; the controller moves that axis. This is the
"think in joints" control — the counterpart to the
[Cartesian XYZ test](../cartesian-xyz-test/), which thinks in tool-pose
coordinates.

## What it implements

| Channel | Port | Used for |
|---------|------|----------|
| Dashboard | 29999 | enable / disable / clear / stop / e-stop / speed / pump |
| Motion | 30003 | **`ServoJ`** streamed joint setpoints (and `JointMovJ` available) |
| Feedback | 30004 | robot mode and **actual joint angles** (`q_actual`) |

- **`dobot.py`** — driver with a joint follower: streams `ServoJ` toward the
  target with an **acceleration-limited (trapezoidal) velocity profile**, so the
  start and the stop are eased and the arm doesn't overshoot.
- **`prototype.py`** — Flask blueprint (REST API + serves the page); clamps each
  joint to its limit, live state over SSE. Mounted by the hub — see
  [../README.md](../README.md).
- **`index.html`** — J1–J4 sliders + type-in boxes, live readout, Speed +
  Smoothness, 10 saved-location slots, pump, stop/e-stop.

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

In the hub, choose the **Joint Angles Test** tab, enter the robot IP, then
**Connect → Enable**. On Enable the sliders auto-sync to the current angles so the
first move doesn't jump. Drag **J1 / J2 / J3 / J4** (or type an exact value) and
the joint follows smoothly.

## Smooth following (eased start & stop)

A slider updates a **target angle**; a background thread streams `ServoJ`,
slewing the commanded setpoint toward the target with an **acceleration-limited**
profile: it ramps velocity up to the cap, cruises, then brakes early enough to
arrive at the target with ~zero speed. So the motion eases in and out and the arm
no longer overshoots and creeps back.

- **Speed** caps the joint following speed (deg/s).
- **Smoothness (ramp)** is the ramp-up / braking time — higher is gentler (no
  overshoot), lower is snappier. It maps to the follower's acceleration cap
  (`accel = speed-cap ÷ ramp-time`).

## Joint limits

Targets are clamped per joint. Anything still unreachable is rejected by the
controller as a `ServoJ` error; after three in a row the follower stops and the
reason is shown in the command log. Limits live in `prototype.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `JOINT_LIMITS` | J1 ±160, J2 −25..85, J3 −25..105, J4 ±160 | per-joint slider ranges (deg) |
| `MAX_VEL` | `120` deg/s | following speed at 100% |
| `RAMP_SECS` | `0.35` s | default ramp/brake time (the Smoothness knob) |

> These are **approximate** starting values — verify and tighten against your
> robot. The controller is the final authority on what's reachable.

## Saved locations

Ten fixed numbered slots (1–10), always present so `POST api/recall/<1–10>` is a
valid call even when a slot is empty. Edit a slot's label and J1–J4, or **Set** to
capture the current slider angles; **Recall** sends the arm there. Saved to
`locations.json` across restarts.

## Stop, errors, pump

**Stop motion** eases the joints to a halt, **Emergency Stop** (or **Esc**) cuts
servo power, controller errors surface in the log (and **Clear Error**
auto-re-arms so you can drive again), and the **air pump** uses the two-line
suck/blow model (`SUCK_DO_INDEX` / `BLOW_DO_INDEX`).

## Safety

Keep the **hardware E-stop** in reach, start with a **low speed**, and keep the
first targets near the current angles.
