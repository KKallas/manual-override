# Prototypes — the hub, and how to build a new one

This folder holds small, self-contained prototypes for **Manual Override**, all
served by one **hub** server. The hub auto-discovers every prototype, mounts it,
and shows a dashboard with **one tab per prototype** — each tab embeds that
prototype's GUI live, so you can see and drive it from a single page.

```
prototypes/
├── hub.py              # the server: discovers + mounts every prototype
├── dashboard.html      # the hub UI (tabs + embedded GUI)
├── requirements.txt    # Flask (the hub's only dependency)
├── README.md           # ← you are here
├── joint-slider-test/  # a prototype
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
tab and stops loading its GUI (so it stops polling) — handy for parking
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

@bp.route("/api/state")        # served at /p/<slug>/api/state
def state():
    return jsonify({"hello": "world"})
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
   own pages relatively.
4. (Optional) `requirements.txt`, a `README.md`, a `.gitignore` for any
   persisted state.
5. Restart `python hub.py` — your tab appears automatically.

## Live updates

The hub embeds your GUI in an iframe; **your page keeps itself current by
polling its own API** (see the existing prototypes: a `setInterval` fetch of
`/api/state` with a `rev` counter to skip redundant work). The toolbar's
**↻ Reload** button force-refreshes the embedded GUI if you need it.

## Conventions worth copying

- A monotonically increasing `rev` returned by your state endpoint lets clients
  poll cheaply and skip work when nothing changed.
- Clamp/validate inputs server-side so a slider drag can't wedge your state.
- A low-rate poll (5–10 fps) plus client-side interpolation looks smooth without
  a chatty server — see `playfield-areas/screen.html`.
