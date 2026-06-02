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
| Dashboard (control) | 29999 | Enable, Disable, Clear Error, Stop (ResetRobot), Emergency Stop, Speed Factor, error queries |
| Motion | 30003 | `JointMovJ` — absolute joint moves driven by the sliders |
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
5. Drag **J1 / J2 / J3** — each slider sets an absolute target angle and sends
   `JointMovJ`. J4 is held at its current angle.

## Stop & error handling

- **Stop motion** — `ResetRobot()`: halts the current move and clears the motion
  queue. The robot stays enabled.
- **■ EMERGENCY STOP** (or press **Esc**) — `EmergencyStop()`: cuts servo power
  immediately. Recover with **Clear Error → Enable**.
- Out-of-range or invalid moves come back as a controller **ErrorID** and are
  shown in the command log (never silently swallowed).
- When the robot enters error mode (mode 9), the mode pill turns red; use
  **Clear Error**.
- Connection drops are detected by the feedback thread and reflected in the UI.

## Safety

- Keep the **hardware E-stop** within reach — the software E-stop is a convenience,
  not a substitute.
- Start with a **low speed factor** and small slider movements.
- Joint slider ranges are conservative; the controller enforces the true limits
  and working-envelope constraints, and rejects anything unsafe with an error.

## Notes & known limitations

- Sliders use **absolute targeting** with client-side throttling (~8 sends/sec).
  Dragging fast queues intermediate targets, so the arm may briefly trail the
  slider; **Stop motion** clears the queue. A later prototype can switch to jog
  (`MoveJog`) for direct velocity control.
- Joint limits and the default IP live in `app.py` (`JOINT_LIMITS`, `DEFAULT_IP`)
  and are served to the page via `/api/config` — edit them in one place.
- The feedback struct offsets (mode @24, magic @48, joints @432) follow the
  documented Dobot 4-axis layout and are validated by the `0x0123456789ABCDEF`
  magic; if your firmware differs, the page falls back to showing no feedback
  rather than garbage.
