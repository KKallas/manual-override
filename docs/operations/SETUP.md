# Setup & Operating Guide

How to assemble, calibrate, and run a **Manual Override** session. This is the
operator/teacher reference. It describes the intended procedure; some steps depend
on software that is still to be implemented (see
[../ARCHITECTURE.md](../ARCHITECTURE.md)).

---

## 1. One-time build & calibration

Do this once per station (and again any time the frame, robots, or camera move).

### 1.1 Assemble the station

1. Mount both MG400s and the 27" screen on the rigid frame so the arms' reachable
   areas **overlap** over the middle of the screen.
2. Mount the camera on its boom looking straight down; confirm it sees the whole
   screen and both trays.
3. Place each tool tray within its robot's reach.
4. Install the hardware E-stop and mark the work zone.

See [../hardware/HARDWARE.md](../hardware/HARDWARE.md) for the physical layout.

### 1.2 Calibrate the camera

- Capture a calibration board from several angles and compute the camera
  **intrinsics**. Store them under `calibration/` (git-ignored).

### 1.3 Calibrate camera ↔ screen

- Establish the **homography** that maps camera pixels to screen coordinates
  (e.g. by showing known points on the screen and detecting them).

### 1.4 Calibrate robot ↔ screen

- For each robot, build the mapping that lets it place a node tip at a target
  screen coordinate (hand-eye style calibration). Verify by commanding the arm to
  a few known points and checking the node lands there.

> Calibration validity depends on nothing moving. If you bump the frame,
> re-run 1.2–1.4.

---

## 2. Safety rules (review every session)

- **E-stop** location is known to everyone; the operator can reach it at all
  times.
- **Hands clear** of the work zone whenever robots are enabled.
- Robots run with **classroom-safe speed limits**.
- The operator has final authority to pause/stop any match.
- The server enforces joint/speed limits and arbitrates the overlap zone — but
  these are a backstop, not a substitute for the rules above.

---

## 3. Start-up checklist (each session)

1. Power on robots, screen, camera, and the compute machine.
2. Confirm both robots are reachable over the network.
3. Start the game server.
4. Open the referee GUI; confirm:
   - both robots connected and homed,
   - camera feed live with ArUco tags detected,
   - screen rendering the playfield.
5. Verify calibration with a quick "touch this point" test per robot.
6. Set the **control mode** for the class you're running:
   - Class 1 → **manual sliders**
   - Class 2 → **assisted**
   - Class 3 → **agent**
7. Set match parameters (length, spawn rate, etc.) for the lesson.

---

## 4. Running a class

Follow the class doc for session flow:

- [Class 1 — Thinking in Planes](../curriculum/class-1-thinking-in-planes.md)
- [Class 2 — Vision & Strategy](../curriculum/class-2-vision-and-strategy.md)
- [Class 3 — Closing the Loop](../curriculum/class-3-closing-the-loop.md)

For Classes 2–3, also start the **agent bridge** (Claude Code + Kimi LLM) and
confirm it can read game state / camera feed and issue commands through the
server.

---

## 5. Running the tournament

See [Tournament](../curriculum/tournament.md) for formats and structure. Keep
control mode and match parameters constant within a bracket and announce them up
front.

---

## 6. Shut-down

1. Stop any running match; set robots to a safe park pose.
2. Disable/disconnect robots.
3. Stop the server and agent bridge.
4. Power down hardware.

---

## 7. Troubleshooting (starter list)

| Symptom | Likely cause | Try |
|---------|--------------|-----|
| ArUco tags not detected | Glare / lighting / focus | Fix lighting, refocus camera, re-check FOV |
| Node lands off-target | Calibration drifted | Re-run robot↔screen calibration (1.4) |
| Robot won't connect | Network / power / not homed | Check cabling, power-cycle, re-home |
| Two arms nearly collide | Overlap arbitration off/misconfigured | Stop match; verify server safety config |
| Agent acts but never scores | Feedback loop not wired (Class 3) | Confirm camera/ArUco outcome reaches the agent |

> Expand this table as real failure modes show up during builds and sessions.
