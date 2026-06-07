# Calibration & pick-and-place (prototype)

Ties the perception stage to the robot: learn the **camera→robot mapping** from 4
screen corners (captured at 3 heights), then **pick a selected ArUco tag and drop
it at a dispenser**. It drives two other prototypes through the hub:

- **[webcam](../webcam/)** — the tracked ArUco tags (`get_tags()` / `get_tag(id)`)
- **[cartesian-xyz-test](../cartesian-xyz-test/)** — moves the MG400 in workspace
  XYZ and runs the air pump (`robot_ready()`, `current_pose()`, `move_to()`, `pump()`)

## How calibration works

The camera is roughly top-down and fisheye. With 4 corners we fit a **planar
homography** (`cv2.getPerspectiveTransform`) mapping camera `nx,ny` (normalised
0–1) → robot `x,y` (mm). It corrects perspective and is *roughly right* over the
work area; residual fisheye bow at the very edges remains. (A fisheye-undistort
step can slot in ahead of the homography later.)

Because parallax/fisheye shift the mapping with height, the 4 corners are captured
**separately at 3 levels** — `floor`, `pickup`, `move` — giving one homography per
level. Each level also stores a **Z height**.

### Steps (needs the MG400 + camera)

1. In the **Cartesian** tab: Connect + Enable the arm (this starts the follower
   the calibration moves stream to).
2. In **Calibration**: set the **tool marker id** (the ArUco on the tool head) and
   each level's **Z**.
3. Calibrate the **floor** (surface) by hand: jog the arm (Cartesian sliders) so
   the tool marker sits over a corner, then **Capture** — it auto-fills the robot
   XY from the live pose and the tool marker's camera position. Repeat for all 4
   floor corners. **Move here** drives the arm back to a captured corner to check
   it.
   Then the higher levels capture themselves: **Auto** on the pickup/move card
   drives to each floor corner's XY, rises to that level's Z, and records the
   marker's new camera position (parallax shifts where it appears).
4. Jog to your drop spot and **Capture** the **dispenser**.
5. Pick a tag id from the dropdown and **Launch** — the arm travels over the tag,
   descends, suctions, lifts, carries to the dispenser, and releases.

Corner robot XY is editable if you'd rather type it. Everything persists to
`calibration.json` and survives a restart.

## API

| Verb | Path | Action |
|------|------|--------|
| `GET` | `/api/state` | robot/webcam/opencv status, tags, the calibration, level completeness, job status |
| `PATCH` | `/api/calib` | set `tool_marker`, `object_level`, per-level `z`, `dispenser` |
| `POST` | `/api/capture` `{level, corner}` | store a corner (auto-fill robot pose + tool cam) |
| `PATCH` | `/api/corner` `{level, corner, robot:{x,y}}` | manual corner edit |
| `POST` | `/api/dispenser/capture` | store current pose as the dispenser |
| `POST` | `/api/jog` `{level, corner}` or `{dispenser:true}` | drive the arm there |
| `POST` | `/api/autocapture` `{level}` | auto-capture pickup/move from the floor calibration |
| `POST` | `/api/pick` `{id}` | run pick-and-place of a tag → dispenser (background job) |
| `POST` | `/api/abort` | stop the running job (and drop) |

`cam_to_robot(nx, ny, level)` is also exposed as a programmatic function for other
prototypes.

## Run

Runs in the **prototype hub**. Install this folder's requirements (OpenCV):

```bash
cd prototypes
pip install -r requirements.txt
pip install -r calibration/requirements.txt
python hub.py            # http://localhost:8000 → Calibration tab
```

## Caveats

- Robot motion only works with the MG400 connected + enabled (via the Cartesian
  tab); otherwise every motion call returns a clear error and nothing moves.
- The homography is a rough planar fit; objects are localised with the
  `object_level` map while the tool marker sits slightly above the surface —
  "roughly right" by design.
