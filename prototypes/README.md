# Prototypes — the hub, and how to build a new one

This folder holds small, self-contained prototypes for **Manual Override**, all
served by one **hub** server. The hub auto-discovers every prototype, mounts it,
and shows a dashboard with **one tab per prototype** — each tab embeds that
prototype's GUI live, so you can see and drive it from a single page.

```
prototypes/
├── hub.py              # the server: discovers + mounts every prototype
├── dashboard.html      # the hub UI (tabs + embedded GUI)
├── live.py             # shared helper: push state to pages over SSE (no polling)
├── requirements.txt    # Flask (the hub's only dependency)
├── README.md           # ← you are here
├── joint-angles-test/  # a prototype
│   ├── prototype.py    #   MANIFEST + Flask blueprint  (required)
│   ├── index.html      #   its GUI
│   └── dobot.py        #   helper module
└── playfield-areas/    # a prototype
    ├── prototype.py    #   MANIFEST + Flask blueprint  (required)
    ├── controller.html #   default GUI (shown in the hub tab)
    └── screen.html     #   extra page (opens clean in its own tab)
```

## Run

```bash
pip install -r requirements.txt
python hub.py                 # then open http://localhost:8000
#   --port 8000   change port
```

The hub prints each prototype it mounts. Open <http://localhost:8000>, pick a
tab. Adding or removing a prototype requires a hub restart (blueprints mount at
startup).

**Enable / disable.** Each tab has a switch. Turning a prototype off dims its
tab and stops loading its GUI (so it drops its live stream) — handy for parking
prototypes you're not using. The choice is **saved** to `hub-settings.json` and
restored on the next start. Disabling is a dashboard-level convenience; the
prototype's API stays mounted either way.

---

## How a prototype is structured

A prototype is **any sub-folder containing a `prototype.py`**. That file must
define exactly two top-level names:

| Name       | What it is                                                        |
|------------|------------------------------------------------------------------|
| `MANIFEST` | a dict describing the prototype (name, pages, which GUI to embed) |
| `bp`       | a `flask.Blueprint` holding the prototype's pages + API           |

The hub imports the file, mounts `bp` under `/p/<folder-name>/`, and reads
`MANIFEST` to build the dashboard tab. There is **no `app.run()`** in a
prototype — it only ever runs inside the hub.

### The manifest

```python
MANIFEST = {
    "name": "Playfield Areas",                 # tab label
    "description": "One-line summary shown in the toolbar.",
    "default_page": "controller",              # page embedded in the hub tab ("" = blueprint root)
    "pages": [                                  # links shown in the toolbar
        {"path": "controller", "label": "Controller"},
        {"path": "screen", "label": "Open clean view ↗", "newtab": True},
    ],
}
```

- `default_page` is the GUI the hub embeds in its tab — usually your
  **controller**. Use `""` if your single page lives at the blueprint root.
- Each `pages` entry is a link in the toolbar. `"newtab": True` opens it in its
  own browser tab (use this for full-screen / canvas views like a 3D scene);
  otherwise it swaps the embedded iframe.

### The blueprint

```python
import os
from flask import Blueprint, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
bp = Blueprint("playfield_areas", __name__)   # name must be unique-ish; the hub also re-namespaces it

@bp.route("/")                 # served at /p/<slug>/
def index():
    return send_from_directory(HERE, "controller.html")

@bp.route("/controller")       # served at /p/<slug>/controller
def controller():
    return send_from_directory(HERE, "controller.html")

@bp.route("/api/state")        # served at /p/<slug>/api/state — one-shot snapshot
def state():
    return jsonify({"hello": "world"})

# For live state, add a push stream too — see "Live updates" below:
# @bp.route("/api/events")     # SSE; pages open an EventSource on it
# def events():
#     return _live.stream(_snapshot)
```

Serve HTML with `send_from_directory(HERE, "...")`. Define your API under
`/api/...`. The hub adds the `/p/<slug>` prefix — **you never hard-code it.**

### The frontend rule: use base-relative URLs

Because the hub mounts you under a prefix, your HTML must **not** call `/api/...`
(absolute). Resolve URLs against the current page's directory instead:

```js
const BASE = location.pathname.replace(/\/[^/]*$/, '/');  // dir of this page
const U = p => BASE + p.replace(/^\//, '');               // U('/api/state') -> <dir>/api/state

await fetch(U('/api/state'));
```

Links between your own pages should be **relative** too (`href="screen"`, not
`href="/screen"`). This is the one thing to get right — everything else is
ordinary Flask + HTML.

### Helper modules

`prototype.py` may import sibling files (e.g. a driver). The hub puts the
prototype's folder on `sys.path` while importing, so a plain `import dobot`
works. Give helpers **unique names** across prototypes (they share one module
namespace); when in doubt, keep logic inside `prototype.py`.

### Depending on another prototype (`hub_init` hook)

A prototype can run background work and/or use another prototype's code. Define
an optional hook — the hub calls it once, after every prototype is loaded and the
enable/disable state is restored:

```python
_ctx = None

def hub_init(ctx):           # optional; called once at startup
    global _ctx
    _ctx = ctx
    # start a background thread here if you need one (use a daemon thread)

# inside that thread / your routes:
#   ctx.is_enabled()                      -> is THIS prototype enabled right now?
#   ctx.get_prototype("playfield-areas")  -> another prototype's module, or None (not installed)
#   ctx.is_prototype_enabled("playfield-areas")  -> installed AND enabled?
```

These reads are **live** — they track runtime enable/disable changes. Use them so
your work respects the toggles: do nothing while `is_enabled()` is false, and
degrade gracefully when a dependency is missing or off.

To call another prototype's logic, expose **plain functions** from its
`prototype.py` (not route handlers — those need a request) and call them through
`ctx.get_prototype(slug)`. For example, `playfield-areas` exposes
`create_area()`, `update_area()`, `remove_area()`, `list_areas()` over its shared
store, so another prototype can drive what the playfield renders.

#### Worked example: `approaching-box`

[approaching-box/](approaching-box/) is the canonical sample of one prototype
driving another. It **renders nothing of its own** — a background loop drives the
`playfield-areas` store, and you watch the result on the Playfield tab:

> spawn a blue box at depth `z = -10` → move it toward the camera to `z = 0` →
> hold ~2 s → delete it → wait ~1 s → repeat.

It is worth copying because it shows every part of the cross-prototype contract
working together:

- **Start a daemon loop from `hub_init`.** The hook stores `ctx` and launches one
  background thread (only once — guard with `if _thread is None`):

  ```python
  def hub_init(ctx):
      global _ctx, _thread
      _ctx = ctx
      if _thread is None:
          _thread = threading.Thread(target=_loop, name="approaching-box", daemon=True)
          _thread.start()
  ```

- **Gate every action on live state.** Before (and during) each move it checks one
  helper that folds the three live reads together, so the loop respects runtime
  toggles and a missing dependency:

  ```python
  def _can_drive():
      if _ctx is None or not _ctx.is_enabled():      # am I on?
          return False, "disabled"
      if _ctx.get_prototype(PLAYFIELD) is None:      # is the dependency installed?
          return False, "not_installed"
      if not _ctx.is_prototype_enabled(PLAYFIELD):   # is the dependency on?
          return False, "playfield_disabled"
      return True, None
  ```

- **Drive the dependency through its plain functions**, not HTTP — mutating the
  same in-memory store the playfield's own pages render:

  ```python
  pf  = _ctx.get_prototype(PLAYFIELD)                 # the imported module
  box = pf.create_area(name="Approaching", x=0, y=0, z=-10, color="#3b82f6", glow=2.0)
  pf.update_area(box["id"], z=new_z)                  # animate it forward each tick
  pf.remove_area(box["id"])                           # clean up at the end
  ```

- **Degrade gracefully.** Sleeps and the animation step re-check `_can_drive()` in
  small chunks, so disabling either prototype mid-run **idles the loop and removes
  the box** rather than leaving a stray area on the playfield. Its own
  `controller.html` shows the live phase, the current `z`, and dependency health
  (and offers an *Enable Playfield* button when that dependency is off).

The resulting behaviour, end to end:

| Situation | What happens |
|-----------|--------------|
| Approaching Box disabled | loop idles, its box is removed — nothing happens |
| Playfield not installed | status error `not_installed` |
| Playfield disabled | status error `playfield_disabled`; box cleaned up |
| Both enabled | box appears and its `z` animates, live on the Playfield tab |

See [approaching-box/README.md](approaching-box/README.md) for the full
write-up.

### State, persistence, dependencies

- Keep runtime state in memory in `prototype.py` (module globals). Guard it with
  a `threading.Lock` — the hub runs threaded.
- To persist (settings, presets), write a JSON file next to `prototype.py` and
  add it to a local `.gitignore`. See `playfield-areas/settings.json`.
- Extra Python deps: add a `requirements.txt` in your prototype folder and note
  it in your prototype's README. (The hub itself only needs Flask.)

---

## Minimal new-prototype checklist

1. `mkdir prototypes/my-thing`
2. Add `prototype.py` with a `MANIFEST` and a `bp` blueprint (copy the skeleton
   above).
3. Add your GUI HTML; fetch with the base-relative `U()` helper; link to your
   own pages relatively; stream live state with an `EventSource` on `/api/events`
   (see [Live updates](#live-updates--push-dont-poll)) instead of polling.
4. (Optional) `requirements.txt`, a `README.md`, a `.gitignore` for any
   persisted state.
5. Restart `python hub.py` — your tab appears automatically.

## Live updates — push, don't poll

**Your page keeps itself current by streaming from the server, not by polling.**
The shared helper [`live.py`](live.py) gives you a Server-Sent Events (SSE)
channel in a few lines; pages open one `EventSource` and the server pushes a
fresh snapshot whenever state changes. This is what makes a dragged control track
the 3D view live, and a server-driven variable move the controls smoothly instead
of stepping. (The old polling model lagged by the poll interval and made driven
variables jump.)

There are **two shapes of live state**, and the helper handles both:

| Your state is…                                   | How it changes        | Use                                   |
|--------------------------------------------------|-----------------------|----------------------------------------|
| **Event-driven** — mutated by discrete actions (areas, settings, a tween) | you know exactly when | `bump()` on every mutation; long keep-alive `interval` |
| **Sampled** — read continuously from hardware/video (a robot pose, camera fps, tracked tags) | no discrete event | no `bump()`; a short `interval` (e.g. `0.2`) |

Identical consecutive snapshots are coalesced into a keep-alive, so an
event-driven store stays quiet when idle and a sampled source only emits frames
that differ.

### Server side

```python
import live
_live = live.LiveState()

def _snapshot():                      # a no-arg callable returning a JSON-able dict
    with _lock:                       # may take your own data lock
        return {"rev": _rev, "items": list(_store.values())}

# EVENT-DRIVEN: bump on every mutation; the stream wakes instantly.
def _touch():
    global _rev
    _rev += 1
    _live.bump()

@bp.route("/api/events")
def events():
    return _live.stream(_snapshot)              # long keep-alive; every change bumps

# …or SAMPLED: no bump, re-snapshot ~5x/s (only frames that differ are sent).
@bp.route("/api/events")
def events():
    return _live.stream(_snapshot, interval=0.2)
```

Keep a plain `GET /api/state` (or `/api/status`) returning the same snapshot too:
it's the cheap one-shot a page fetches for its first paint and after a local edit.

### Page side

Replace `setInterval(poll, …)` with an `EventSource`, and split your old `poll()`
into a one-shot fetch plus an `apply(data)` that renders. `EventSource`
auto-reconnects if the connection drops.

```js
function applyState(data) { /* render from data — see below */ }
async function refresh() { applyState(await (await fetch(U('/api/state'))).json()); }

refresh();                                   // instant first paint
const es = new EventSource(U('/api/events'));  // live pushes; auto-reconnects
es.onmessage = e => { try { applyState(JSON.parse(e.data)); } catch (_) {} };
es.onerror = () => { /* show 'offline' */ };
```

> **Heads-up:** `/api/stream` is *not* a convention — the webcam already uses that
> name for its MJPEG video. Use **`/api/events`** for the SSE state channel.

## Conventions worth copying

These three rules are what keep updates smooth in both directions:

- **Update controls in place; never rebuild the DOM on each frame.** A driven
  variable can push 30×/s — rebuilding `innerHTML` thrashes the page and kills
  focus. Build inputs once, then set their values, **skipping any input that's
  focused or mid-drag** (track a per-input flag on `pointerdown`/`pointerup`).
  See `playfield-areas/controller.html` (`syncList`/`updateCard`).
- **Throttle slider writes — don't debounce them.** A debounce *swallows* the
  intermediate values of a drag and only sends the final one, so the other view
  jumps. A throttle (fire on the leading edge, then at most every ~50 ms, plus a
  trailing send) streams the drag so the other view tracks it live. See the
  `patch()` helper in `playfield-areas/controller.html`; `cartesian-xyz-test`'s
  `queueMove`/`flushMove` is the same idea.
- **Clamp/validate inputs server-side** so a fast slider drag can't wedge your
  state with an out-of-range value.

For a 3D / canvas view, also ease each value toward its latest target every frame
(frame-rate-independent interpolation) so motion stays smooth *between* pushes —
see `playfield-areas/screen.html` (`interpolate`). A `rev`/`srev` counter in your
snapshot still lets a client skip redundant re-renders when nothing changed.
