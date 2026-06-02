# Joint Slider Test (prototype)

First prototype for **Manual Override**: a tiny web app to verify communication
with a real **Dobot MG400** in TCP/IP ("API") mode. Three sliders rotate the base
joint (J1) and the first two arm joints (J2, J3); the page shows live status from
the robot's real-time feedback stream and gives you proper stop/error controls.

This is a **communications test**, not the game. It exercises all three MG400
channels end-to-end.

## What it implements

| Channel | Port | Used for |
|---------|------|----------|
| Dashboard (control) | 29999 | Enable, Disable, Clear Error, Stop, Emergency Stop, Speed Factor, vacuum pump (`DOExecute`), error queries |
| Motion | 30003 | `ServoJ` — streamed setpoints for smooth live following (`JointMovJ` also available) |
| Feedback | 30004 | 1440-byte real-time packet (~8 ms): robot mode + actual joint angles |

- **`dobot.py`** — thread-safe MG400 driver (sockets, command I/O, feedback parser).
- **`app.py`** — Flask REST server + serves the control page.
- **`index.html`** — single-page slider UI with live status and a command log.

## Run

```bash
cd prototypes/joint-slider-test
pip install -r requirements.txt
python app.py            # add --port 8080 to change the port
```

Open **http://localhost:8000**, enter the robot IP (default `192.168.1.6`), and
press **Connect**.

### Typical flow

1. **Connect** — opens all three channels; the status panel goes live.
2. **Enable** — powers the servos (auto-clears a latched error first).
3. (optional) Set the **Speed factor** low (e.g. 20%) for first tests.
4. **Sync sliders to robot** — snap the sliders to the current pose so the first
   move isn't a jump.
5. Drag **J1 / J2 / J3** — each slider sets a *target* angle. The arm follows it
   smoothly (see below). J4 is held at its current angle.

## Smooth live following

Dragging a slider does **not** send a queued point-to-point move per change —
that's what makes naive control jumpy (each `JointMovJ` accelerates and
decelerates to a full stop). Instead:

- A slider move just updates a **target** on the server (cheap, no socket wait).
- A background thread streams **`ServoJ`** setpoints at ~12.5 Hz, moving the
  commanded setpoint toward the target while **velocity-capping** each joint.
- The **Following speed** slider sets that cap (`ratio% × 90 °/s`), so dragging
  feels like continuous, bounded-speed motion regardless of how fast you drag.
- It's initialised to the current pose on Enable, so it never jumps on the first
  move.

Tunables live in `dobot.py` (`_max_vel`, loop `interval`, `t_param`) and `app.py`
(`MAX_JOINT_VEL`).

## Stop & error handling

- **Stop motion** — freezes the follower at its current setpoint (snaps target to
  setpoint), so the arm stops within one tick without a lurch and stays enabled.
  The sliders re-sync to where it stopped. (`ResetRobot()` is also available in
  the driver for a hard queue-clear.)
- **■ EMERGENCY STOP** (or press **Esc**) — `EmergencyStop()`: cuts servo power
  immediately. Recover with **Clear Error → Enable**.
- Out-of-range or invalid moves come back as a controller **ErrorID** and are
  shown in the command log (never silently swallowed).
- When the robot enters error mode (mode 9), the mode pill turns red; use
  **Clear Error**.
- Connection drops are detected by the feedback thread and reflected in the UI.

## Vacuum pump

The **End tool** panel toggles the suction cup on/off via a controller digital
output (`DOExecute`, which fires immediately rather than queuing behind motion).
The button reflects the **real** pump state, read back from the feedback packet's
digital-output bits. The output index is `VACUUM_DO_INDEX` in `app.py` (default
`1`) — change it to match how your pump kit is wired.

## Safety

- Keep the **hardware E-stop** within reach — the software E-stop is a convenience,
  not a substitute.
- Start with a **low speed factor** and small slider movements.
- Joint slider ranges are conservative; the controller enforces the true limits
  and working-envelope constraints, and rejects anything unsafe with an error.

## Notes & known limitations

- Smooth following relies on **`ServoJ`** being supported by your firmware (it is
  on the MG400). If a `ServoJ` call errors three times in a row, the follower
  stops and the reason is shown in the command log instead of failing silently.
- Joint limits and the default IP live in `app.py` (`JOINT_LIMITS`, `DEFAULT_IP`)
  and are served to the page via `/api/config` — edit them in one place.
- The feedback struct offsets (mode @24, magic @48, joints @432) follow the
  documented Dobot 4-axis layout and are validated by the `0x0123456789ABCDEF`
  magic; if your firmware differs, the page falls back to showing no feedback
  rather than garbage.
