# Manual Override — The Idea

Manual Override is built the way you teach it: **one small, playable step at a
time.** Each step is a self-contained little app with its own GUI, it is tested
by *actually playing with it*, and once it works it becomes the floor the next
step stands on. You never build the whole robot game at once — you build the
smallest thing that does something real, prove it on screen, then stack the next
thing on top.

This document is about that idea — the development rhythm and the problems
students solve and play at each step. The roadmap of steps lives in
[missions.md](missions.md); the working steps live in
[prototypes/](prototypes/).

---

## The rhythm: small step → GUI → test by playing → build upon

Every step follows the same loop, and the loop *is* the lesson:

1. **Build the smallest real thing.** Not a plan, not a diagram — a tiny program
   that moves the arm, or draws a blob, or reads a tag. One capability.
2. **Give it a GUI.** Every step has a web page you can open and poke. Sliders,
   buttons, a 3D view. The GUI is not decoration — it is how you *see* whether
   the thing works, and it is how you test it without writing more code.
3. **Test it by playing.** Each step comes with a small challenge or mini-game.
   You don't "run the unit tests" — you try to pick up the block before the timer
   runs out, or collect the most areas, or watch the box reach the centre. If you
   can play it, it works.
4. **Build the next step on top.** A finished step is a black box you no longer
   have to think about. The next step assumes it and adds exactly one new idea.

Because each step is its own folder with its own page, a student can own one step
completely — make *that* work — without touching or understanding the others. If
one step breaks, the rest still run.

---

## One place to see all your steps: the hub

All the small steps are plugged into a single server — the **hub**
([prototypes/hub.py](prototypes/hub.py)). It finds every step automatically and
shows a dashboard with **one tab per step**, each tab embedding that step's live
GUI. You open one page and you can see and drive everything you've built so far.

- Each step is just a folder with a `prototype.py`. Drop a new folder in, restart,
  and a new tab appears. That low ceremony is deliberate: adding a step should
  feel cheap, because the whole method is *add another small step*.
- Each tab has an on/off switch, so you can park steps you're not using and focus
  on the one you're working on.
- Steps can even **drive each other** through the hub. That is how the robot side
  and the game side eventually meet — see the *approaching box* step below.

The hub turns "a pile of little experiments" into "one growing system you can
play with end to end." Watching the same page update live on a second laptop is
itself the debugging tool for the whole stack.

---

## The three stages of problems students play

The steps climb through three stages — **control → perception → decision** — and
each stage adds exactly one new idea while inheriting everything below it as a
black box. Stages 1.x and 2.x build the two halves an agent will later sit
between; stage 3.x drops the agent into the socket the students already built.

### Stage 1 — Control: *make the robot do what you say*

The first problem is the most basic and the most satisfying: move a real robot
arm and pick something up.

- **1.1 — Drive the arm + the gripper.** A page of sliders rotates the arm's
  joints; a separate control runs the air pump (suck to grab, blow to release).
  Motion alone isn't enough — an arm that can't grip is an expensive fan, so
  earning the suction is part of the problem.
  - **The game:** in 90 seconds, pick up one M5 Atom block from the tool tray and
    place it on the red blob. The faster you do it, the more points you score.
  - *Built today:* [joint-slider-test](prototypes/joint-slider-test/).

- **1.2 — Think in space, not in joints.** Steering each joint by hand is fiddly.
  The robot can move in straight workspace lines (X / Y / Z) on its own, so this
  step replaces joint-sliders with **Cartesian** sliders — you say "go here," the
  robot figures out the joints.
  - **The game:** in 90 seconds, collect as many areas as you can. A second after
    you reach one it disappears and reappears somewhere else — so faster, more
    direct moves win.
  - *Built today:* [cartesian-xyz-test](prototypes/cartesian-xyz-test/).

The game screen those blobs/areas live on is its own playable step too —
[playfield-areas](prototypes/playfield-areas/) — a 3D view of glowing zones you
can add, move and light however you like, with no robot attached. It is the board
the later games are played on, and a clean example of "a step that is fun to poke
on its own."

### Stage 2 — Perception: *let the system see*

Now the problem changes from "move where I point" to "move where *the world*
is." A camera looks down on the table and the system starts finding things for
you.

- **2.1 — Top camera helps you aim.** Add a camera looking straight down and use
  it together with the XYZ moves to pick up with far less guesswork — the camera
  closes the gap between where you think the block is and where it actually is.

- **2.2 — Read the tags automatically.** The blocks wear ArUco tags. The system
  reads the next target's position from the camera view and *stages it* — loads it
  into the sliders, ready and waiting for a "go" command.
  - **The game (full complexity):** blobs now appear at random and rise at
    different speeds — the real game, not the gentle version.

- **2.3 — Click near a target, the arm does the rest.** You click near a proposed
  target; the system grabs the next tag, moves the hand there, and lays the tag
  down. The human points at *intent*; the machine handles the *motion*.

### Stage 3 — Decision: *let the agent play*

The final problem hands the controls to **Claude Code**. Crucially, nothing is
rewired — the agent calls the *same* controls a student clicks. The students
built both halves; stage 3 only inserts the brain.

- **3.1 — The agent runs the sliders.** The agent reads positions and issues the
  moves and grips itself, and scores at all. The lesson is trust: watching a
  program do the thing you did by hand.

- **3.2 — The agent avoids the other arm.** A second arm (run by the game) shares
  the table. The agent has to handle collisions over a shared workspace.

- **3.3 — The agent plays cleverly.** It reasons about tools left on the table by
  the other player and decides whether grabbing one is worth the time it costs —
  using its toolset, not just running it.

---

## Why each step has a GUI and a game

Two choices define the whole method, and both are about making progress
*visible*:

- **A GUI per step** means you test by looking and poking, not by reading logs.
  A 14-step pile of command-line scripts is invisible; a hub of 14 tabs you can
  open and drive is a system you can feel. The page also keeps itself live (it
  polls its own little API), so "the backend changed a value and the screen moved
  on its own" is something you watch happen — the debugging structure for
  everything layered on top.

- **A game per step** means the test is motivating and honest. The green
  checkmark is *also a robot doing the thing*. "Did you score before the timer?"
  is a better test of a pick-and-place primitive than "did this function return
  the right number," because it checks the code against the real, physical world.

The steps already in [prototypes/](prototypes/) show the method is real, not
aspirational: the joint and Cartesian controllers each drive a real arm behind a
slider page; the playfield renders the game board; and the *approaching box* step
([approaching-box](prototypes/approaching-box/)) does nothing but quietly drive
the playfield from the background — the proof that one finished step can become a
building block for the next.

---

## Running what exists today

```bash
cd prototypes
pip install -r requirements.txt
python hub.py                 # then open http://localhost:8000
```

Pick a tab and play. The robot steps need the arm reachable on the network in
API mode first — see
[docs/operations/dobot-api-mode.md](docs/operations/dobot-api-mode.md). To add a
step of your own, drop a folder with a `prototype.py` and a GUI and restart the
hub; the contract is in [prototypes/README.md](prototypes/README.md). That is the
whole method in one action: **add one more small, playable step.**
