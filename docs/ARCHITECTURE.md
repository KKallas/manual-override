# Architecture

This document describes the system that runs **Manual Override**: the components,
how they talk to each other, the data that flows between them, and the planned
code layout. It is a specification — the code described here is not all written
yet.

---

## 1. High-level picture

The system has four physical pieces — two robots, a screen, and a camera — bound
together by one **game server**, and acted on by one or more **clients** (a human
control page, or an AI agent bridge).

```
        ┌───────────────────────── Top camera ─────────────────────────┐
        │  USB/CSI overhead camera, sees the whole screen + both trays  │
        └───────────────────────────────┬───────────────────────────────┘
                                         │ raw frames
                                         ▼
                              ┌────────────────────┐
                              │   Vision service    │  detect ArUco tags,
                              │  (in game server)   │  estimate node poses,
                              └─────────┬───────────┘  map to screen coords
                                        │ node poses (player_id, x, y, θ)
                                        ▼
   ┌────────────┐   robot     ┌────────────────────┐   render   ┌────────────┐
   │ MG400  A   │◀──driver───▶│    Game server      │──state────▶│  27" screen │
   │ (TCP/IP)   │             │  - game loop        │            │ (playfield) │
   └────────────┘             │  - score/rules      │            └────────────┘
   ┌────────────┐   robot     │  - referee GUI      │
   │ MG400  B   │◀──driver───▶│  - websocket API    │
   │ (TCP/IP)   │             └─────────┬───────────┘
   └────────────┘                       │ WebSocket / HTTP
                                         ▼
                              ┌────────────────────┐
                              │   Client layer      │
                              │  - human HTML page  │  joint sliders (Class 1)
                              │  - agent bridge     │  Claude Code ↔ Kimi LLM
                              └────────────────────┘
```

---

## 2. Components

### 2.1 Game server (Python)

The authoritative core. Responsibilities:

- **Game loop** — spawns blobs, scrolls them "in depth," tracks their
  active/inactive windows, and advances game time.
- **Scoring & rules** — decides when a node is *on* an active blob and awards
  points. Owns all game state; clients never score themselves.
- **Robot connections** — holds a driver connection to each MG400 and forwards
  validated motion commands. Enforces safety limits (joint ranges, speed caps,
  shared-zone arbitration so the two arms don't collide in the overlap region).
- **Vision integration** — consumes node poses from the vision service and feeds
  them into the scoring check.
- **Screen rendering** — drives the 27" playfield display.
- **WebSocket / HTTP API** — the single surface that all clients talk to.
- **Referee GUI** — an operator-facing window (see 2.2).

### 2.2 Referee GUI

A desktop control panel for the teacher/operator running a session:

- Start / pause / reset matches and set match length.
- Assign players to robots and to ArUco IDs.
- Monitor live scores, node positions, and the camera feed with detected tags
  overlaid.
- E-stop both robots.
- Switch control mode per robot: **manual sliders** (Class 1), **assisted**,
  or **agent** (Classes 2–3).

### 2.3 Vision service

Runs inside or alongside the game server. Pipeline:

1. Grab frame from the overhead camera.
2. Detect ArUco markers (OpenCV `cv2.aruco`).
3. Using the camera calibration + a known screen homography, map each tag's
   pixel pose into **screen coordinates** (the same coordinate frame the game
   renders blobs in).
4. Publish `{player_id, node_id, x, y, theta, timestamp}` for every visible node.

This is the component Class 2 contrasts with an LLM: it is fast and cheap, where
an LLM reading the same frame is accurate but slow and resource-heavy.

### 2.4 Robot drivers

One driver instance per MG400. The MG400 exposes a **TCP/IP control interface**
(Dobot's TCP protocol / dashboard + motion ports). The driver:

- Connects, enables, and homes the arm.
- Accepts **joint-space** commands (for the slider UI in Class 1) and
  **task-space** target commands (for assisted/agent modes).
- Clamps every command to configured safe ranges before sending.
- Reports current joint angles and tool pose back to the server.

### 2.5 Client layer

Two kinds of client, both talking to the same server API:

- **Human control page (HTML).** For Class 1 this is *raw rotational-joint
  sliders only* — no inverse-kinematics "go here" button. Later classes add
  richer views.
- **Agent bridge.** A local server / page that lets **Claude Code** drive a node,
  with the **Kimi LLM engine** available as the reasoning/vision model. The
  bridge exposes the game state and camera feed to the agent and translates the
  agent's decisions into the same server commands a human would send. This is how
  students give "direct access to an agent to operate the bots" in Classes 2–3.

---

## 3. Coordinate frames

Getting these aligned is the heart of the technical setup (and of Class 1's
"thinking in planes").

| Frame | Description |
|-------|-------------|
| **Joint space** | Per-motor rotation angles of an MG400. What Class 1 sliders drive directly. |
| **Robot task space** | The arm's own Cartesian frame (mm). |
| **Screen space** | Pixel/grid coordinates of the playfield where blobs live. |
| **Camera/pixel space** | What the overhead camera sees. |

Two calibrations bind them together:

1. **Camera ↔ screen** — a homography so detected tag pixels become screen
   coordinates.
2. **Robot ↔ screen** — a hand-eye style calibration so a robot can place its
   node at a target screen coordinate.

Calibration procedure lives in [operations/SETUP.md](operations/SETUP.md).

---

## 4. Data flow during a match

1. Server spawns a blob at screen coordinate `(x, y)` with an `active` window
   `[t0, t1]` and a player assignment.
2. Camera → vision service → node poses in screen space, ~30 Hz.
3. A client (human sliders or agent) sends motion commands to move its node
   toward the blob.
4. Each tick, the server checks: *is node N within radius R of blob B while B is
   active and N belongs to B's assigned player?* If yes → award points.
5. Server updates score, re-renders the screen, and broadcasts new state to all
   clients.

---

## 5. Nodes & ArUco tags

Each robot's tool tray holds **three nodes**. A node displays:

- An **ArUco marker** encoding which **player/multiplayer slot** currently owns
  it, and
- A **countdown timer** to when that assignment rotates.

Rotating assignments keep the game dynamic and force re-planning — a strategy
concept developed in Class 2. The mapping from ArUco ID → player is owned by the
server and surfaced in the referee GUI.

---

## 6. Planned code layout

> Not implemented yet — this is the target structure.

```
manual-override/
├── server/                 # Python game server
│   ├── game/               # game loop, blobs, scoring, rules
│   ├── robots/             # MG400 TCP drivers + safety limits
│   ├── vision/             # camera capture, ArUco detection, homography
│   ├── render/             # 27" playfield rendering
│   ├── api/                # websocket / HTTP server
│   └── refgui/             # referee operator GUI
├── client/                 # client-side software
│   ├── web/                # HTML control pages (joint sliders, views)
│   └── agent-bridge/       # Claude Code ↔ Kimi LLM bridge to the server API
├── calibration/            # calibration scripts + stored intrinsics/homography
├── docs/                   # this documentation set
│   ├── curriculum/         # the three classes + tournament
│   ├── hardware/           # drawings, BOM, build notes
│   └── operations/         # setup & run guides
└── tools/                  # dev/ops helper scripts
```

---

## 7. Protocols (planned)

- **Server ↔ robots:** Dobot MG400 TCP/IP (dashboard + motion ports).
- **Server ↔ clients:** WebSocket for live state and commands; HTTP for static
  assets and one-shot config.
- **Vision → game loop:** in-process queue (vision runs in the server) publishing
  node-pose messages.

## 8. Safety

The MG400s share an overlapping work area over the screen. The server is the
single authority that:

- clamps joint ranges and speeds,
- arbitrates the overlap zone so two arms are never commanded into the same space
  simultaneously,
- exposes a hardware-and-software **E-stop** in the referee GUI.

No client — human or agent — can bypass these limits; they are enforced
server-side before any command reaches a driver.
