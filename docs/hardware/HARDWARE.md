# Hardware

Physical build for a **Manual Override** station. This is the specification; CAD
drawings and printable parts will live alongside this file as they are produced
(see [Drawings](#drawings)).

## Station overview

```
                 overhead camera (on a boom, looking straight down)
                              │
                              ▼
        ┌──────────────────────────────────────────────────┐
        │                                                    │
   ┌────┴────┐        ┌──────────────────────┐         ┌────┴────┐
   │ MG400 A │        │   27" screen (flat)   │         │ MG400 B │
   │  +tray  │        │     = playfield       │         │  +tray  │
   └─────────┘        └──────────────────────┘         └─────────┘
        │                                                    │
   tray A: 3 ArUco nodes                          tray B: 3 ArUco nodes
```

The two arms are positioned so their reachable areas **overlap** over the middle
of the screen (the contested zone). The camera boom holds the camera high enough
to see the whole screen and both trays without an arm occluding the center at
rest.

## Major components

### Robots — 2× Dobot MG400

- Desktop 4-axis robot arms with a TCP/IP control interface.
- Each fitted with a lightweight **node holder** end-effector that can pick/hold a
  node from its tray and rest the node tip on the screen.
- Mounted at a fixed, repeatable position relative to the screen (calibration
  depends on this not moving).

### Playfield — 27" display

- Standard 27" monitor used as the game surface, mounted flat (or slightly
  angled) between the robots.
- Protect the surface: the nodes rest *on or just above* the glass. Consider a
  thin protective sheet and ensure node tips are soft/rounded.

### Overhead camera

- Single camera on a boom, looking straight down at the playfield.
- Wide enough field of view to see the whole screen and both trays.
- Global-shutter or high-frame-rate preferred for clean ArUco detection of moving
  nodes; a good rolling-shutter USB webcam can work for a first build.

### Tool trays — 2× (one per robot), 3 nodes each

- A tray within each robot's reach holding **three nodes** in known slots.
- Slots are positioned so the robot can reliably re-acquire a node.

### Nodes — 6 total (3 per robot)

- A lightweight pointer the robot holds, with a flat top face that displays:
  - an **ArUco marker** (encodes the owning player/multiplayer slot), and
  - a **countdown** to when ownership rotates.
- The ArUco face must stay visible to the overhead camera while the tip is on the
  screen.
- Two design options for the displayed marker/timer:
  1. **Static printed ArUco** + a separate indication of rotation (simplest), or
  2. **Small e-ink / tiny display** on the node showing the current ArUco ID and
     countdown (richer, more build effort).

### Frame & lighting

- A rigid frame holding the two robots, screen, and camera boom in fixed relative
  positions — rigidity is what keeps calibration valid.
- Even, diffuse lighting; avoid glare on the screen and hard shadows that hurt
  ArUco detection.

### Compute

- One machine (or small server) running the Python game server, vision, rendering,
  and referee GUI. See [../operations/SETUP.md](../operations/SETUP.md).
- Student/teacher machines for Claude Code + the Kimi LLM agent bridge (Classes
  2–3).

## Safety hardware

- A reachable **E-stop** that cuts robot motion, in addition to the software
  E-stop in the referee GUI.
- Clear physical markers for the work zone so students keep hands clear.
- Speed-limited robot configuration appropriate for a classroom.

## Drawings

CAD files, printable node/tray parts, and frame dimensions will be added to this
folder as they are produced. Planned files:

- `frame.*` — frame dimensions and assembly.
- `node.*` — node body + ArUco face.
- `tray.*` — three-slot tray.
- `camera-boom.*` — camera mount height/position.

See [bill-of-materials.md](bill-of-materials.md) for the parts list.
