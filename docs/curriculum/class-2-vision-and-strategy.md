# Class 2 — Vision & Strategy

> Control mode: students **improve the image→robot-command mapping** using Claude
> Code, and play with assisted targeting.

## Learning goals

By the end of this class students can:

- Convert time *not* spent fighting the interface into **strategic decisions**.
- Explain why a **specialized vision model** (e.g. an ArUco/blob detector) is the
  right tool for *fast* perception, and why a general **LLM**, although it *can*
  read an image, is much **slower and more resource-intensive**.
- Use **Claude Code** in a guided session to improve how the system turns what the
  camera sees into robot commands.

## The big idea

In Class 1 most effort went into *operating* the arm. Now we ask: if the mapping
from **image → command** were better, what would you do with the time you get
back? Answer: **strategy** — which blobs to chase, when to reposition spare nodes,
how to use the contested zone, how to handle node-ownership rotation.

Then we look at *how* perception happens. Two ways to find a node on the screen:

1. A **purpose-built detector** (OpenCV ArUco / blob detection): tiny, runs at
   camera frame rate, costs almost nothing.
2. An **LLM** that looks at the frame: flexible and able to reason about novel
   situations, but **orders of magnitude slower and heavier**.

Students experience the trade-off directly, then use **Claude Code** to *improve
the fast mapping* — better calibration, smarter command generation — rather than
putting the LLM in the fast loop.

## Setup

- Class 1 completed; calibration verified.
- Game server running; robots can be switched between **manual** and **assisted**
  modes in the referee GUI.
- **Claude Code** available on student/teacher machines, pointed at this repo.
- The agent bridge available so students can compare LLM-based perception against
  the specialized detector (for illustration, not for the fast loop).

## Session flow (~75 min)

1. **Recap & reframe (10 min)** — revisit "how much time did you fight the UI?"
   Introduce the idea of buying back time with better tooling.
2. **Demo: two ways to see (15 min)** — show the ArUco detector finding nodes at
   frame rate; then show an LLM reading the same frame. Time them. Discuss
   latency, cost, and when each is appropriate.
3. **Guided Claude Code session (25 min)** — students use Claude Code to improve
   the **image→command** mapping (e.g. refine the target-to-joint translation, add
   a smoothing or aiming helper). Small, observable changes.
4. **Strategy round (20 min)** — with assisted targeting freeing them from raw
   sliders, students plan: which blobs, when to swap nodes, how to play the
   contested zone and the rotation timer.
5. **Wrap (5 min)** — set up Class 3: *what if the agent could also see the result
   of its actions and learn?*

## What students do

- Race the detector against the LLM and record the timing gap.
- Make a concrete improvement to the mapping with Claude Code and test it live.
- Write down a strategy and try it in a scored round.

## Talking points

- "Can see an image" ≠ "should be in the fast loop." Latency and cost decide
  where each tool belongs.
- The fast/specialized model handles perception; the slow/general model is for
  reasoning and improving the system — a pattern that returns in Class 3.
- Strategy is what you can afford once the mechanics are cheap.

## Assessment

- Can the student justify using a specialized detector over an LLM for real-time
  node tracking?
- Did their Claude Code change measurably help, and can they explain what it did?
- Can they state a strategy and why it fits the game's pressures?
