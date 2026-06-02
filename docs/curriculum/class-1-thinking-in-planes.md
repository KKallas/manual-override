# Class 1 — Thinking in Planes

> Control mode: **raw rotational-joint sliders**. No "move to X,Y" button.

## Learning goals

By the end of this class students can:

- Explain that a robot arm reaches a point in a plane by *combining several
  rotations*, not by moving along X and Y directly.
- Predict, roughly, how turning one joint moves the node on the screen.
- Feel — concretely — how hard direct joint control is, which motivates every
  later class.

## The big idea

We control the MG400 with **regular sliders, one per rotational motor**. To put a
node on a blob, you cannot ask the robot to "go to that spot." You must reason:
*if I rotate this joint a little, where does the tip go? And this one?* This is
**thinking in planes** — building the intuition that links joint rotations to a
position on the flat playfield.

The interface is deliberately awkward. **Fighting it is the lesson.** The
frustration students feel here is the raw material for Class 2, where they learn
to convert that wasted effort into strategy and better tools.

## Setup

- Robots built and calibrated ([../operations/SETUP.md](../operations/SETUP.md)).
- Game server running; both robots in **manual sliders** control mode (set in the
  referee GUI).
- The human control page open for each player, showing **only joint sliders**.
- A gentle game profile: slow blob scroll, wide active windows, low spawn rate.

## Session flow (~60 min)

1. **Safety & intro (10 min)** — E-stop, no hands in the work zone, how the game
   scores. Show one blob being hit.
2. **Free play with sliders (15 min)** — students just try to touch a stationary
   blob. Let the struggle happen.
3. **Guided discovery (15 min)** — pause and ask: *which slider moves the node
   left/right near the center? Does the same slider do the same thing at the edge?*
   Draw the answer out; introduce the word *plane* and the idea of combined
   rotations.
4. **Scored mini-round (15 min)** — slow game, students chase active blobs with
   sliders.
5. **Wrap (5 min)** — "How much of your time was spent *fighting the controls*
   versus *deciding where to go*?" Plant the Class 2 question.

## What students do

- Move each joint one at a time and watch the node trace an arc.
- Find a slider combination that lands the node on a target.
- Score blobs in a slow match.

## Talking points

- Joints rotate; the *tip* moves through a plane. The mapping is not linear and
  changes depending on the arm's pose.
- Humans are slow at this — that's expected and important.
- Foreshadow: *what if something could do this mapping for us?*

## Assessment

- Can the student name which joint(s) to move to go in a desired direction from a
  given pose?
- Can they articulate why "go to X,Y" is harder than it sounds for a jointed arm?
