# Manual Override

> A mixed-reality robotics game and course for grades 7–9, built around two Dobot
> MG400 arms, one shared screen, and a path from *manual control* to *autonomous
> agents*.

**Manual Override** is the name and the lesson: students start by overriding the
robot's motion themselves — pushing raw joint sliders — and over three classes
work toward *handing the controls back* to a vision-driven AI agent that they
have taught to play. The course teaches spatial reasoning, the value of
purpose-built vision models, and the basics of closing a reinforcement-learning
loop, all through a competitive game.

---

## The game in one paragraph

Two Dobot MG400 robot arms sit on either side of a 27" screen laid flat (or
angled) between them. The screen runs a rhythm-style game — think *Guitar Hero*
seen in depth — where **blobs** (targets) scroll toward the players and briefly
become *active*. Each robot carries a **tool tray with three "nodes."** A node is
a physical marker tipped with an **ArUco tag** that identifies the player and
shows a countdown to when its assignment changes. A **top-down camera** tracks
every tag in real time. **If a player's node is resting on a blob while that blob
is active, the player scores.** The two robots' reachable work areas *overlap* in
the middle of the screen, so the center is contested ground.

See [docs/game-design.md](docs/game-design.md) for the full rules.

---

## Why this exists (the educational arc)

The course is three classes plus a tournament. Each class changes *how* the
student controls the robot, and each change teaches a concept.

1. **[Thinking in planes](docs/curriculum/class-1-thinking-in-planes.md)** —
   Students drive the arm with raw rotational-joint sliders. There is no "move to
   X,Y" button. To put a node on a blob you have to reason about how each motor's
   rotation maps into the plane of the screen. The UI is deliberately awkward;
   fighting it *is* the lesson.

2. **[Vision & strategy](docs/curriculum/class-2-vision-and-strategy.md)** —
   Students notice how much time they waste fighting the interface, and learn to
   convert that into strategy. They then use **Claude Code** in a live session to
   improve the **image → robot-command** mapping, and discover *why* a small,
   specialized vision model beats a general LLM for fast perception: an LLM can
   read an image, but it is far slower and more resource-hungry than a
   purpose-built detector.

3. **[Closing the loop](docs/curriculum/class-3-closing-the-loop.md)** —
   Students wire the camera feed and ArUco tags back into the agent as feedback.
   Now the LLM can act, *see the result in real time*, and start storing the
   strategies that work — the first steps of a reinforcement-learning loop and a
   learned model of good play.

The arc ends with a **[tournament](docs/curriculum/tournament.md)** where
human-tuned and agent-driven strategies compete.

---

## What's in this repo

This repository is the home for everything needed to build, run, and teach the
course:

| Area | Where | Status |
|------|-------|--------|
| System architecture | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | spec |
| Game design & rules | [docs/game-design.md](docs/game-design.md) | spec |
| Course curriculum | [docs/curriculum/](docs/curriculum/) | spec |
| Hardware & drawings | [docs/hardware/](docs/hardware/) | spec |
| Operating / setup guide | [docs/operations/SETUP.md](docs/operations/SETUP.md) | spec |
| Dobot API-mode setup | [docs/operations/dobot-api-mode.md](docs/operations/dobot-api-mode.md) | done |
| Joint-slider comms prototype | [prototypes/joint-slider-test/](prototypes/joint-slider-test/) | working |
| Game server (Python) | `server/` *(planned)* | not started |
| Referee GUI | `server/refgui/` *(planned)* | not started |
| Client / agent bridge | `client/` *(planned)* | not started |

> **Current state:** mostly specification, with the **first hardware prototype**
> live — a [joint-slider test](prototypes/joint-slider-test/) that talks to a real
> MG400 over all three TCP channels (control / motion / feedback) and drives the
> base + first two joints smoothly, plus vacuum/blow control. The full `server/`
> and `client/` trees described in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
> are not built yet. To connect a robot, first follow
> [docs/operations/dobot-api-mode.md](docs/operations/dobot-api-mode.md).

---

## System overview

```
                 ┌───────────────────────────────────────────┐
                 │              Top-down camera                │
                 │        (tracks all ArUco node tags)         │
                 └───────────────────┬─────────────────────────┘
                                     │ frames + tag poses
                                     ▼
   ┌──────────────┐         ┌──────────────────┐         ┌──────────────┐
   │ Dobot MG400  │◀──TCP──▶│   Game server    │◀──TCP──▶│ Dobot MG400  │
   │  (Player A)  │         │   (Python)       │         │  (Player B)  │
   └──────────────┘         │  + Referee GUI   │         └──────────────┘
                            │  + game state    │
                            └───────┬──────────┘
                                    │ renders
                                    ▼
                            ┌──────────────────┐
                            │   27" screen     │  blobs scroll in depth
                            │  (the playfield) │
                            └──────────────────┘
                                    ▲
                                    │ HTTP / WebSocket
                            ┌──────────────────┐
                            │  Client / agent  │  HTML control pages +
                            │     bridge       │  Claude Code ↔ Kimi LLM
                            └──────────────────┘
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for component responsibilities,
protocols, and the planned code layout.

---

## Hardware at a glance

- **2× Dobot MG400** desktop robot arms
- **1× 27" display** as the shared playfield
- **1× overhead camera** for ArUco tracking
- **2× tool trays**, each holding **3 ArUco-tagged nodes**
- Mounting frame, lighting, and calibration targets

Full details and drawings in [docs/hardware/](docs/hardware/).

---

## Status & contributing

This is an educational project in active design. The specs in `docs/` are the
source of truth right now; implementation follows. Issues and suggestions are
welcome.

## License

[MIT](LICENSE)
