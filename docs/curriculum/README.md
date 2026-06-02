# Curriculum

**Manual Override** is taught as **three classes plus a final tournament**, aimed
at grades **7–9**. The through-line is the title: students begin by *manually
overriding* the robot's motion themselves, and progressively hand control to a
vision-driven AI agent they have taught.

Each class is defined by *how* the student controls the robot — and each change of
control teaches a concept.

| # | Class | Control mode | Core concept |
|---|-------|--------------|--------------|
| 1 | [Thinking in planes](class-1-thinking-in-planes.md) | Raw joint sliders | Spatial reasoning: how rotational motors map into a plane |
| 2 | [Vision & strategy](class-2-vision-and-strategy.md) | Improved image→command mapping | Strategy, and why specialized vision models beat general LLMs for fast perception |
| 3 | [Closing the loop](class-3-closing-the-loop.md) | Agent with camera feedback | Reinforcement-learning basics: act, observe, store what works |
| ★ | [Tournament](tournament.md) | Mixed | Putting it together; human vs. agent strategies compete |

## How to read these

Each class doc contains:

- **Learning goals** — what students should walk away understanding.
- **Setup** — what to prepare before students arrive.
- **Session flow** — a minute-by-minute outline.
- **What students do** — the hands-on activity.
- **Talking points** — the concepts to draw out.
- **Assessment** — how to tell it landed.

## Prerequisites

- No prior robotics or programming required for Class 1.
- Classes 2–3 introduce **Claude Code** and the **Kimi LLM engine** through guided
  sessions — students do not need prior AI experience, but a teacher comfortable
  with the tools helps.
- The hardware must be built and calibrated first — see
  [../operations/SETUP.md](../operations/SETUP.md).

## Age-appropriateness & safety

The robots move real metal arms over a screen. Safety rules (E-stop location,
no-hands-in-the-zone, operator authority) are covered in
[../operations/SETUP.md](../operations/SETUP.md) and should be reviewed at the
start of every session.
