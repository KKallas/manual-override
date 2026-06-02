# Class 3 — Closing the Loop

> Control mode: an **agent** (Claude Code + Kimi LLM) operates a node, using the
> camera feed and ArUco tags as real-time feedback.

## Learning goals

By the end of this class students can:

- Describe a **closed loop**: act → observe → evaluate → adjust.
- Explain, at a 7–9th-grade level, the idea behind **reinforcement learning** —
  trying actions, seeing results, and keeping what works.
- Set up the agent so it receives feedback from the camera/ArUco tags and **stores
  working strategies** to play better over time.

## The big idea

Until now, perception and action were one-directional. In this class we **close
the loop**: the agent doesn't just send a command — it *sees the result* through
the camera and ArUco tags, judges whether it scored, and remembers what worked.

This is the seed of **reinforcement learning**. The agent:

1. picks an action (move a node toward a blob),
2. observes the outcome in real time (did the node land on the active blob?),
3. records the result, and
4. favors actions that scored next time.

Over a session it builds a small **model of good play** from its own experience —
students watch a strategy *emerge* rather than being hand-coded.

## Setup

- Classes 1–2 completed; mapping improvements from Class 2 in place.
- Game server running; target robot(s) in **agent** mode.
- The **agent bridge** connected: Claude Code with the **Kimi LLM engine** has
  access to the game state, the camera feed, and ArUco poses, and can issue the
  same commands a human would.
- A place for the agent to **store strategies** (a simple memory/log the agent
  reads and writes between attempts).

## Session flow (~75 min)

1. **Recap (10 min)** — perception (fast model) vs. reasoning (LLM); today we add
   *feedback*.
2. **Open loop vs. closed loop demo (15 min)** — run the agent *without* feedback
   (it acts blind), then *with* camera/ArUco feedback. Contrast the behavior.
3. **Wire the feedback (20 min)** — students connect the camera/ArUco outcome back
   to the agent and define what counts as success (scored a blob).
4. **Watch it learn (20 min)** — run repeated rounds; the agent stores what works
   and reuses it. Students observe score trends across attempts.
5. **Reflect (10 min)** — where did it improve? Where does it still fail? What is
   it actually "learning"?

## What students do

- Run the agent with and without feedback and compare.
- Define the success signal and connect it to the agent's memory.
- Run several rounds and chart whether the agent improves.

## Talking points

- A reward signal turns "an agent that acts" into "an agent that *learns*."
- Real-time feedback is what makes the loop closed; without it the agent can't
  tell good moves from bad.
- Stored strategies are a simple, honest version of a learned policy.
- Limits: it learns from *its own* games; it can overfit to one opponent or one
  blob pattern. Good lead-in to the tournament.

## Assessment

- Can the student draw the act→observe→evaluate→adjust loop for this game?
- Can they point to evidence (score over rounds) that the agent improved?
- Can they explain, simply, what reinforcement learning is and where the reward
  comes from here?
