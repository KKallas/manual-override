# Webcam (prototype)

The **eyes** for Manual Override's perception stage. It opens a webcam with
**OpenCV**, shows the live video in its config screen, and lets you pick which
camera to use from a dropdown. For now it just captures and displays frames;
**ArUco-marker reading** (missions 2.1 / 2.2) builds on this same feed later.

This is a UI/perception prototype — no robot involved.

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

Open the **Webcam** tab, pick a camera from the dropdown, and the feed appears.
The choice is remembered (`camera-settings.json`), so next time the feed comes up
on its own.

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
| `REQUEST_W` / `REQUEST_H` | `1280` / `720` | resolution requested (camera may pick another) |
