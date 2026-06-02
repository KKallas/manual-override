# Game Design

**Manual Override** is a two-player, robot-controlled, rhythm-meets-targeting
game played on a shared screen. This document defines the rules, scoring, and
tunable parameters.

---

## 1. Setup

- A **27" screen** is the playfield, mounted flat or slightly angled between two
  robots.
- A **Dobot MG400** stands on each side. Their reachable work areas **overlap**
  in the middle band of the screen — the *contested zone*.
- Each robot has a **tool tray** with **three nodes**. A node is a physical
  pointer tipped with an **ArUco tag**.
- An **overhead camera** sees the whole screen and both trays and tracks every
  tag.

```
   ┌──────────────────────────── 27" screen ────────────────────────────┐
   │  Player A only │           CONTESTED ZONE          │  Player B only  │
   │   (A reaches)  │      (both A and B can reach)      │   (B reaches)   │
   └──────────────────────────────────────────────────────────────────────┘
     ▲                                                                  ▲
  MG400 A          tray A: [node a1][node a2][node a3]            MG400 B
                   tray B: [node b1][node b2][node b3]
```

---

## 2. Core loop — "Guitar Hero in depth"

Targets called **blobs** appear and scroll toward the players "in depth" (a
perspective lane, like notes approaching in Guitar Hero). Each blob has:

- a **screen position** `(x, y)`,
- an **active window** — a short interval during which it can be scored,
- a **player assignment** — which player (and therefore which colored nodes) may
  score it, and
- a **point value**.

**To score:** have one of *your* nodes resting on a blob *while that blob is
active*. New blobs keep arising, so there is always a next opportunity — the
skill is choosing which to chase.

---

## 3. Scoring rule (authoritative)

On each server tick, for every blob `B` that is currently **active**:

```
for each node N belonging to B.assigned_player:
    if distance(N.position, B.position) <= HIT_RADIUS:
        award B.points to that player
        mark B as consumed
```

- Scoring is decided **only by the server**, from camera-tracked node poses.
  Clients never self-report a hit.
- A consumed blob stops scoring; a blob whose active window expires unscored is a
  miss.

---

## 4. Nodes, ArUco IDs, and rotation

Each node shows an **ArUco marker** that encodes the **player/multiplayer slot**
that currently owns it, plus a **countdown timer** to when that ownership
**rotates**.

- Three nodes per robot lets a player pre-position spare nodes and swap which one
  is "live."
- Periodic rotation of ownership forces re-planning and keeps a single node from
  camping one spot — a strategic pressure developed in
  [Class 2](curriculum/class-2-vision-and-strategy.md).

---

## 5. The contested zone

Blobs spawned in the overlapping middle band can be reached by **either** robot.
These are worth chasing but risky: the server's safety arbitration prevents both
arms occupying the same space, so committing to a contested blob may mean yielding
elsewhere. This is the main source of head-to-head tension.

---

## 6. Match structure

- A match is a fixed length (default **3 minutes**).
- Blobs spawn on a schedule that ramps in density/speed over the match.
- Highest score at time-out wins; ties broken by most blobs scored in the
  contested zone.

---

## 7. Tunable parameters

These are the knobs a teacher/operator can adjust per session (exposed in the
referee GUI):

| Parameter | Meaning | Default |
|-----------|---------|---------|
| `MATCH_LENGTH` | match duration | 180 s |
| `HIT_RADIUS` | how close a node must be to a blob to score | tuned to node size |
| `BLOB_ACTIVE_WINDOW` | how long a blob stays scorable | 1.5 s |
| `BLOB_SPAWN_RATE` | blobs per second (ramps up) | 0.5 → 1.5 |
| `BLOB_SCROLL_SPEED` | approach speed "in depth" | medium |
| `NODE_ROTATION_PERIOD` | how often node ownership rotates | 20 s |
| `CONTESTED_FRACTION` | share of blobs spawned in the overlap zone | 0.3 |

---

## 8. Control modes (tie-in to the course)

The same game is played under different control schemes as the course
progresses:

- **Manual sliders** — raw joint control (Class 1).
- **Assisted** — higher-level targeting, after students improve the image→command
  mapping (Class 2).
- **Agent** — a Claude Code + Kimi LLM agent drives a node, learning from camera
  feedback (Class 3).

The [tournament](curriculum/tournament.md) can mix these for asymmetric, dramatic
matchups.
