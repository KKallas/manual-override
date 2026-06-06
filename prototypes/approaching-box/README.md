# Approaching Box (prototype)

A tiny demo of **one prototype depending on another**. It has no rendering of
its own — a server-side loop drives the **[Playfield Areas](../playfield-areas/)**
module to animate a blue box:

> spawn a blue box at depth **z = -10** → move it toward the camera to **z = 0**
> → hold ~2 s → delete it → wait ~1 s → repeat.

You watch it on the **Playfield** tab (its screen or controller), where the box
appears and its `z` value changes live. This prototype's own controller just
shows status and dependency health.

## Run

Runs inside the **prototype hub** (no standalone server):

```bash
cd prototypes
pip install -r requirements.txt
python hub.py            # then open http://localhost:8000
```

Open the **Playfield Areas** tab → **Open clean playfield ↗** to watch the box,
or the **Approaching Box** tab to see status.

## How the dependency + enable/disable behaves

The animation loop runs on the server (a daemon thread), gated by live hub state:

| Situation | What happens |
|-----------|--------------|
| **Approaching Box disabled** (its tab switch off) | loop idles and removes its box — nothing happens |
| **Playfield not installed** | status error `not_installed`; controller explains to add the module |
| **Playfield disabled** | status error `playfield_disabled`; controller offers an **Enable Playfield** button; the box is cleaned up |
| **Both enabled** | the box appears and animates; visible live in the Playfield controller/screen |

The loop keeps running regardless of which tab you're viewing, because it lives
in the backend, not the page.

## How it works (the contract)

- The hub calls an optional `hub_init(ctx)` hook after all prototypes load,
  handing this prototype a context. The loop uses it to check `ctx.is_enabled()`
  (itself), `ctx.get_prototype("playfield-areas")` (installed?) and
  `ctx.is_prototype_enabled("playfield-areas")` (on?).
- It drives the playfield through that module's **programmatic API** —
  `create_area(**fields)`, `update_area(id, **fields)`, `remove_area(id)` — which
  mutate the same in-memory store the playfield's REST routes serve. So edits
  show up in the playfield's own windows.

See the hub guide in [../README.md](../README.md) for the `hub_init` hook and
cross-prototype access.

## Files

- `prototype.py` — the animation loop + status API (Flask blueprint).
- `controller.html` — status GUI (phase, live `z`, depth track, dependency alerts).
