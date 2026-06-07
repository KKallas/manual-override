# Webcam (prototype)

The **eyes** for Manual Override's perception stage. It opens a webcam with
**OpenCV**, shows the live video in its config screen, lets you pick which camera
to use, and **detects + tracks ArUco markers** (DICT_4X4_50) in the feed. The
tracked tags are published for other prototypes to consume.

This is a UI/perception prototype — no robot involved.

## ArUco tag tracking

Every frame, markers are detected (downscaled for speed) and run through a
**debounced tracker**:

- a marker must be seen continuously for **> 1 s** before it's **added** to the
  list (a one-frame false positive never appears), and
- once tracked it stays until it's been **missing > 3 s**, then it's **removed**.

Each tracked tag reports `id`, `x`/`y` (pixels), `nx`/`ny` (normalised 0–1),
`rotation` (degrees), and `missing` (seconds since last seen). The live feed
outlines markers — **green** once tracked, **amber** while still qualifying — and
the controller lists the tags.

### Use it from another prototype

The list is available two ways:

- **REST:** `GET /api/tags` → `{ tags: [{id, x, y, nx, ny, rotation, missing}], … }`
- **In-process** (through the hub's [`hub_init` context](../README.md)):

  ```python
  cam = ctx.get_prototype("webcam")
  if cam and ctx.is_prototype_enabled("webcam"):
      for tag in cam.get_tags():
          ...  # {id, x, y, nx, ny, rotation, missing}
  ```

## What it implements

- **`prototype.py`** — Flask blueprint mounted by the hub. A `CameraManager`
  owns one OpenCV capture and a background **grab thread** that keeps the latest
  JPEG-encoded frame in memory. Routes serve that frame as an **MJPEG** stream,
  list/select cameras, and report status. Mounted by the hub — see
  [../README.md](../README.md).
- **`controller.html`** — the **config screen** (shown in the hub tab): camera
  dropdown + Rescan/Stop, the live video, and resolution/fps/index readouts.
- **`view.html`** — a chrome-free, full-screen view of the same stream, opened in
  its own tab.

## Install + run

This prototype needs OpenCV, so install its own requirements (the hub itself only
needs Flask):

```bash
cd prototypes
pip install -r requirements.txt              # the hub (Flask)
pip install -r webcam/requirements.txt       # opencv-python + numpy
python hub.py                                # then open http://localhost:8000
```

> If OpenCV isn't installed, the hub prints `FAILED to load prototype 'webcam'`
> and keeps running the other prototypes — install the requirements to enable it.

Open the **Webcam** tab, pick a camera and a **resolution** from the dropdowns,
and the feed appears. Both choices are remembered (`camera-settings.json`), so
next time the feed comes up on its own.

### Resolution (up to 4K)

The resolution dropdown offers up to **3840×2160 (4K)**. The camera reports the
closest mode it actually supports, and the **Resolution** stat shows what you got
— so if you pick 4K on a 1080p camera you'll see 1080p. Reaching 1080p+/4K relies
on the **MJPG** capture codec, which the driver requests before setting the size
(most UVC webcams only expose their high-res / high-fps modes through MJPG, not
raw frames). Note that streaming and (later) per-frame ArUco work at 4K cost real
CPU and bandwidth — 1080p is usually the better default for detection.

> **macOS:** the first capture triggers a camera-permission prompt for whatever
> runs `python` (Terminal / your IDE). Allow it, then Rescan. Capture uses the
> AVFoundation backend; on Windows it uses DirectShow.

## How it works

- **One capture, many viewers.** A single grab thread reads frames and stores the
  latest JPEG; `/api/stream` serves it as `multipart/x-mixed-replace`. Selecting a
  different camera swaps the capture underneath the same stream, so the `<img>`
  never needs reloading.
- **Camera list is probed.** `/api/cameras` tries indices `0..PROBE_MAX-1` and
  reports the ones that open (the active camera is reported from live state rather
  than reopened). Missing indices may print harmless OpenCV warnings to stderr.
- **Smooth on a slow poll.** The page streams video continuously but polls
  `/api/status` once a second for the stats line — same cheap-poll pattern as the
  other prototypes.

### Where ArUco goes later

Marker detection drops into `CameraManager._process_frame(frame)` — detect on the
frame, draw the overlay before it's encoded, and stash marker positions for a new
`/api/markers` route. The capture/stream plumbing here stays unchanged.

## API

| Verb   | Path           | Action                                            |
|--------|----------------|---------------------------------------------------|
| `GET`  | `/api/cameras` | probe + list cameras (`{cameras, remembered}`)    |
| `GET`  | `/api/status`  | `{open, index, width, height, fps, error, ...}`   |
| `POST` | `/api/select`  | open a camera — body `{ "index": 0 }`             |
| `POST` | `/api/stop`    | release the camera                                |
| `GET`  | `/api/stream`  | live MJPEG video (`multipart/x-mixed-replace`)    |

## Config

Constants live at the top of `prototype.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `DEFAULT_INDEX` | `0` | camera opened when nothing is remembered |
| `PROBE_MAX` | `6` | how many indices `/api/cameras` probes |
| `JPEG_QUALITY` | `80` | MJPEG frame quality (0–100) |
| `STREAM_FPS` | `30` | cap on stream push rate |
| `DEFAULT_W` / `DEFAULT_H` | `1920` / `1080` | resolution used when none is chosen/remembered |
| `RESOLUTIONS` | VGA … 4K | options offered in the resolution dropdown |
