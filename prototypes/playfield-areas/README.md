# Playfield-areas prototype

A small two-window playground for the Manual Override playfield: a **3D screen
window** that renders coloured "square areas" with glow and depth-of-field, and
a **controller window** that edits them. Both talk to one Python server, so an
edit in one window shows up in the other within a poll tick.

This is a UI/architecture prototype — there is no robot or camera involved.

## Stack

- **3D view:** [Three.js](https://threejs.org) (loaded from a CDN via an
  import-map — no build step), with post-processing:
  - `UnrealBloomPass` → the **glow**
  - `BokehPass` → the **depth-of-field**
  - `OrbitControls` (camera) + `TransformControls` (drag an area to move it)
- **Backend:** Flask, in-memory area store, REST API (matches the other
  prototypes' style).

### Smooth on a slow update rate

The screen polls at a fixed **~6.7 fps** (every 150 ms) but never snaps: each
area mesh stores the latest server values as a *target*, and the render loop
eases toward it every frame (frame-rate-independent exponential smoothing). So
the playfield looks smooth even though the data only arrives a few times a
second.

### Everything persists across restarts

Both the **areas** and the **view/effect settings** are written to disk
(`areas.json` and `settings.json`) and reloaded on startup, so the playfield
comes back exactly where you left it — deletes stick, edits stick. The seed
presets only appear on a first-ever run (when no `areas.json` exists). Areas are
flushed by a background thread that batches rapid changes, so a high-rate
animation driving the store doesn't hammer the disk.

### Glowing links between zones

Each zone can **link** to other zones: in its card, pick a target from the
*link to…* dropdown and **+ Link**. A straight **glowing** line (in the source
zone's colour) is drawn between them — handy to outline a region (e.g. the area
being calibrated). Links are part of the zone, so **deleting a zone removes every
link to it**. Lines follow the zones as they move, glow with the bloom, and
persist (they live in each area's `links` array).

### Camera tracking (ArUco markers)

Each area carries a `marker` id and renders a real **OpenCV `DICT_4X4_50`**
ArUco marker (black pattern, centred) on **both** its top and bottom faces, so it
reads whether the playfield is viewed from above or from underneath. New areas
get a distinct id automatically; set it per area with the **marker id** field.
The markers come from the server (`/api/marker/<id>`, generated with `cv2.aruco`),
so they're detectable by a standard `cv2.aruco` detector — verified by round-trip.

The markers are drawn as a **crisp overlay on top of the glow**: they sit on
their own render layer and are composited *after* the bloom/DOF pass, so they
stay sharp for a camera while the glow stays pretty — no mode to toggle. (The
bottom face is a 180° rotation of the top, which is a rotation, not a mirror, so
it remains a valid detectable marker.)

> OpenCV is an optional dependency. Without it the playfield still runs; the
> `/api/marker` endpoint returns 503 and the markers just don't render.

### View & effects live in the controller (and persist)

Glow, depth-of-field and a **Blender-style camera transform** are edited in the
**controller** and stored server-side in `settings.json`, which is read on
startup and written on every change — so reopening either window restores where
you left off. The transform is **location (x, y, z)**, **rotation (x, y, z Euler
degrees; z is roll)** and **fov**. The camera is bidirectional: orbiting on the
screen updates the controller's location + rotation x/y fields, and editing any
field drives the screen camera. Roll (rotation z) is applied on top each frame,
so it survives mouse-orbit (which only changes pitch/yaw).

## Run

This prototype runs inside the **prototype hub** (it has no standalone server):

```bash
cd prototypes
pip install -r requirements.txt
python hub.py            # then open http://localhost:8000
```

In the hub, choose the **Playfield Areas** tab — it embeds the controller. Click
**Open clean playfield ↗** to open the 3D screen full-size in its own tab. Put
them side by side: add/edit areas in the controller and watch the screen update;
or click an area on the screen and drag the gizmo to move it — the new position
flows back to the server and into the controller.

The pages are mounted under `/p/playfield-areas/` by the hub
(`/p/playfield-areas/controller`, `/p/playfield-areas/screen`); the API paths
below are relative to that prefix.

In the screen window: drag to orbit, scroll to zoom, click an area to select.
Press `t` / `r` / `s` to switch the gizmo between translate / rotate / scale.
The HUD sliders tune the glow and depth-of-field live.

## API

An **area** is `{ id, name, x, y, z, size, color, glow, marker, links }`.
Positions are in scene units (1 unit ≈ 10 cm); `y` is height above the ground;
`glow` scales the bloom emissive (0 = matte); `marker` is the ArUco id (0–49)
shown on it; `links` is a list of other area ids this zone draws a glowing line to.

| Verb     | Path               | Action                                   |
|----------|--------------------|------------------------------------------|
| `GET`    | `/api/areas`       | **list** all areas (`{rev, areas}`)      |
| `POST`   | `/api/areas`       | **create** (body may set any fields)     |
| `GET`    | `/api/areas/<id>`  | **get** one                              |
| `PATCH`  | `/api/areas/<id>`  | **set** — partial update of fields       |
| `DELETE` | `/api/areas/<id>`  | **delete**                               |
| `GET`    | `/api/state`       | poll snapshot: `{rev, areas, srev, settings}` |
| `GET`    | `/api/settings`    | get view/effect settings (`{srev, settings}`) |
| `PATCH`  | `/api/settings`    | **set** view/effect settings (persisted)  |
| `GET`    | `/api/marker/<id>` | PNG of the ArUco marker for that id (DICT_4X4_50) |
| `POST`   | `/api/areas/<id>/links` | link a zone to another (`{to}`)       |
| `DELETE` | `/api/areas/<id>/links/<to>` | remove a link                    |

`rev` (areas) and `srev` (settings) are counters that bump on every change, so a
poller can skip work when nothing changed. Settings are `{ bloom, dof, fov,
cam:{x,y,z}, rot:{x,y,z} }` (cam = location, rot = Euler degrees with z = roll);
partial `PATCH`es merge (e.g. `{"rot":{"z":15}}` leaves the other axes alone).

### Examples

```bash
P=localhost:8000/p/playfield-areas      # the hub mount

# list
curl $P/api/areas

# create a red area at (2, 0, -3)
curl -X POST $P/api/areas \
  -H 'Content-Type: application/json' \
  -d '{"name":"Target","x":2,"z":-3,"color":"#ff5462","glow":2.5}'

# move / recolour it (set)
curl -X PATCH $P/api/areas/a4 \
  -H 'Content-Type: application/json' -d '{"x":-1,"size":2.5}'

# delete
curl -X DELETE $P/api/areas/a4
```

Numeric fields are clamped server-side (e.g. `x,z ∈ [-12,12]`, `size ∈
[0.2,6]`); `color` must be `#rrggbb`.

## Files

- `prototype.py` — Flask blueprint (in-memory area store + REST API + pages),
  mounted by the hub. See [../README.md](../README.md).
- `controller.html` — default GUI: list editor + View & Effects panel
- `screen.html` — Three.js 3D view (bloom + DOF), live poller, drag-to-move
- `areas.json` — runtime-persisted areas (git-ignored)
- `settings.json` — runtime-persisted view/effect state (git-ignored)
