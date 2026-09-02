#!/usr/bin/env python3
"""Convert decoded MP4 frames into deterministic turret activation sprites.

Each supplied three-second video decodes to 73 images because it includes the
sample at exactly 3.000 seconds. Runtime sheets use video frames 0 through 70
and replace frame 71 with the exact active runtime composite. This produces 72
frames at 24 fps while guaranteeing a seamless final state.

Only the black region connected to the outside of the square installation is
removed. Black pixels inside the trap-door silo and turret mechanisms remain
opaque. Every decoded frame is normalized by its outer square silhouette to
remove video-generation framing jitter before being packed horizontally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "assets" / "game-art" / "z-pixel-v2"
SOURCES = PACK / "source-videos" / "runtime-activation"
DEFAULT_FRAME_ROOT = PACK / "source-frames" / "runtime-activation"
RUNTIME = PACK / "normalized" / "structures" / "runtime"
OUTPUT = RUNTIME / "activation"
MANIFEST = PACK / "runtime-activation.json"

DECODED_FRAME_COUNT = 73
VIDEO_FRAME_COUNT = 71
FRAME_COUNT = 72
FRAME_SIZE = 112
FPS = 24
DURATION_S = FRAME_COUNT / FPS
PREVIEW_COLUMNS = 12
PREVIEW_ROWS = 6
REPLENISH_PULSE_S = 0.35
TOWER_ART_SIZE = 88
BLACK_KEY_THRESHOLD = 8
RUNTIME_BLACK_KEY_THRESHOLD = 24
MIN_FOREGROUND_ROW_PIXELS = 32
ALPHA_CUTOFF = 3

TOWER_TYPES = {
    "machine-gun": {"head_version": 1},
    "flamethrower": {"head_version": 1},
    "mortar": {"head_version": 1},
    "tesla-coil": {"head_version": 2},
}

TIMELINE = [
    {"frames": [0, 16], "time_s": [0.0, 0.7], "stage": "trap_door_emergence"},
    {"frames": [17, 30], "time_s": [0.7, 1.3], "stage": "stabilizer_lock"},
    {"frames": [31, 47], "time_s": [1.3, 2.0], "stage": "weapon_extension"},
    {"frames": [48, 64], "time_s": [2.0, 2.7], "stage": "boot_calibration"},
    {"frames": [65, 71], "time_s": [2.7, 3.0], "stage": "active_settle"},
]


def clean_alpha(image: Image.Image) -> Image.Image:
    image = image.convert("RGBA")
    alpha = image.getchannel("A").point(
        lambda value: 0 if value < ALPHA_CUTOFF else value
    )
    image.putalpha(alpha)
    return image


def remove_surrounding_black(image: Image.Image) -> Image.Image:
    """Make only the exterior black field transparent.

    The outer installation rim is present throughout every source video. For
    each occupied scanline, filling between its first and last non-black pixel
    preserves dark silo/mechanical pixels inside that closed rim while dropping
    the black field surrounding it.
    """

    rgba = np.asarray(image.convert("RGBA")).copy()
    intensity = rgba[:, :, :3].max(axis=2)
    foreground = intensity > BLACK_KEY_THRESHOLD
    occupied_rows = np.flatnonzero(
        foreground.sum(axis=1) >= MIN_FOREGROUND_ROW_PIXELS
    )
    if occupied_rows.size == 0:
        raise RuntimeError("decoded video frame contains no visible installation")

    alpha = np.zeros(foreground.shape, dtype=np.uint8)
    for y in occupied_rows:
        columns = np.flatnonzero(foreground[y])
        alpha[y, columns[0] : columns[-1] + 1] = 255
    rgba[:, :, 3] = alpha
    return Image.fromarray(rgba, mode="RGBA")


def key_runtime_exterior(image: Image.Image) -> Image.Image:
    """Remove dark pixels connected to the runtime frame's outside edge."""

    image = image.convert("RGBA")
    rgba = np.asarray(image).copy()
    dark = rgba[:, :, :3].max(axis=2) <= RUNTIME_BLACK_KEY_THRESHOLD
    # A one-pixel dark border guarantees a reliable exterior seed even when a
    # generated square drifts against one edge of the normalized crop.
    candidate = np.full((FRAME_SIZE + 2, FRAME_SIZE + 2), 255, dtype=np.uint8)
    candidate[0, :] = 0
    candidate[-1, :] = 0
    candidate[:, 0] = 0
    candidate[:, -1] = 0
    candidate[1:-1, 1:-1][dark] = 0
    # Copy detaches the PIL image from NumPy's read-only array storage so the
    # flood fill can mutate it.
    mask = Image.fromarray(candidate, mode="L").copy()
    ImageDraw.floodfill(mask, (0, 0), 128, thresh=0)
    exterior = np.asarray(mask)[1:-1, 1:-1] == 128
    rgba[:, :, 3][exterior] = 0
    return Image.fromarray(rgba, mode="RGBA")


def normalize_video_frame(image: Image.Image) -> Image.Image:
    keyed = remove_surrounding_black(image)
    bounds = keyed.getchannel("A").getbbox()
    if not bounds:
        raise RuntimeError("black-keyed video frame contains no visible pixels")
    left, top, right, bottom = bounds
    side = max(right - left, bottom - top)
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    crop_box = (
        round(center_x - side / 2),
        round(center_y - side / 2),
        round(center_x + side / 2),
        round(center_y + side / 2),
    )
    square = keyed.crop(crop_box)
    return key_runtime_exterior(
        clean_alpha(
            square.resize((FRAME_SIZE, FRAME_SIZE), Image.Resampling.LANCZOS)
        )
    )


def decoded_frames(frames_root: Path, tower_type: str) -> list[Path]:
    paths = sorted((frames_root / tower_type).glob("frame-*.png"))
    if len(paths) != DECODED_FRAME_COUNT:
        raise RuntimeError(
            f"{tower_type} must provide {DECODED_FRAME_COUNT} decoded frames; "
            f"found {len(paths)} in {frames_root / tower_type}"
        )
    return paths


def active_runtime_frame(tower_type: str, head_version: int) -> Image.Image:
    cover = Image.open(RUNTIME / "tower-socket-cover-v1.png").convert("RGBA")
    cover = cover.resize((FRAME_SIZE, FRAME_SIZE), Image.Resampling.LANCZOS)
    base = Image.open(RUNTIME / f"{tower_type}-base-v1.png").convert("RGBA")
    head = Image.open(
        RUNTIME / f"{tower_type}-head-v{head_version}.png"
    ).convert("RGBA")
    base = base.resize((TOWER_ART_SIZE, TOWER_ART_SIZE), Image.Resampling.LANCZOS)
    head = head.resize((TOWER_ART_SIZE, TOWER_ART_SIZE), Image.Resampling.LANCZOS)
    inset = (FRAME_SIZE - TOWER_ART_SIZE) // 2
    cover.alpha_composite(base, (inset, inset))
    cover.alpha_composite(head, (inset, inset))
    return clean_alpha(cover)


def pack_frames(frames: list[Image.Image]) -> Image.Image:
    sheet = Image.new(
        "RGBA", (FRAME_SIZE * FRAME_COUNT, FRAME_SIZE), (0, 0, 0, 0)
    )
    for index, frame in enumerate(frames):
        sheet.alpha_composite(frame, (index * FRAME_SIZE, 0))
    return sheet


def preview_sheet(frames: list[Image.Image]) -> Image.Image:
    preview = Image.new(
        "RGBA",
        (FRAME_SIZE * PREVIEW_COLUMNS, FRAME_SIZE * PREVIEW_ROWS),
        (0, 0, 0, 0),
    )
    for index, frame in enumerate(frames):
        preview.alpha_composite(
            frame,
            (
                (index % PREVIEW_COLUMNS) * FRAME_SIZE,
                (index // PREVIEW_COLUMNS) * FRAME_SIZE,
            ),
        )
    return preview


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_tower(
    frames_root: Path,
    tower_type: str,
    head_version: int,
) -> dict[str, object]:
    source = SOURCES / f"{tower_type}-activation-source-v2.mp4"
    if not source.is_file():
        raise RuntimeError(f"missing source video {source}")
    paths = decoded_frames(frames_root, tower_type)
    frames = [
        normalize_video_frame(Image.open(path))
        for path in paths[:VIDEO_FRAME_COUNT]
    ]
    frames.append(active_runtime_frame(tower_type, head_version))
    if len(frames) != FRAME_COUNT:
        raise RuntimeError(f"{tower_type} produced {len(frames)} runtime frames")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    runtime_path = OUTPUT / f"{tower_type}-activation-v2.png"
    preview_path = OUTPUT / f"{tower_type}-activation-preview-v2.png"
    pack_frames(frames).save(runtime_path, optimize=True)
    preview_sheet(frames).save(preview_path, optimize=True)

    runtime = Image.open(runtime_path).convert("RGBA")
    expected_size = (FRAME_SIZE * FRAME_COUNT, FRAME_SIZE)
    if runtime.size != expected_size:
        raise RuntimeError(
            f"{runtime_path.name} has size {runtime.size}, expected {expected_size}"
        )
    for index in range(FRAME_COUNT):
        alpha = runtime.crop((
            index * FRAME_SIZE,
            0,
            (index + 1) * FRAME_SIZE,
            FRAME_SIZE,
        )).getchannel("A")
        if not alpha.getbbox():
            raise RuntimeError(f"{runtime_path.name} frame {index} is empty")
        if index < VIDEO_FRAME_COUNT and alpha.getextrema()[0] != 0:
            raise RuntimeError(
                f"{runtime_path.name} frame {index} still has an opaque exterior"
            )

    return {
        "tower_type": tower_type.replace("-", "_"),
        "source_video": str(source.relative_to(ROOT)),
        "decoded_frame_count": DECODED_FRAME_COUNT,
        "video_frames_used": [0, VIDEO_FRAME_COUNT - 1],
        "runtime_sheet": str(runtime_path.relative_to(ROOT)),
        "preview_sheet": str(preview_path.relative_to(ROOT)),
        "frame_count": FRAME_COUNT,
        "frame_size": [FRAME_SIZE, FRAME_SIZE],
        "fps": FPS,
        "duration_s": DURATION_S,
        "pivot": [0.5, 0.5],
        "sha256": digest(runtime_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--frames-root",
        type=Path,
        default=DEFAULT_FRAME_ROOT,
        help="directory containing one decoded frame folder per turret type",
    )
    args = parser.parse_args()
    frames_root = args.frames_root.resolve()

    assets = [
        build_tower(frames_root, tower_type, config["head_version"])
        for tower_type, config in TOWER_TYPES.items()
    ]
    manifest = {
        "schema_version": 2,
        "generated_with": (
            "AVFoundation MP4 decode plus deterministic exterior-black keying "
            "and frame normalization"
        ),
        "frame_count": FRAME_COUNT,
        "frame_size_px": FRAME_SIZE,
        "fps": FPS,
        "duration_s": DURATION_S,
        "replenish_pulse_s": REPLENISH_PULSE_S,
        "black_key": {
            "method": "outer_silhouette_crop_plus_border_connected_black_key",
            "source_threshold": BLACK_KEY_THRESHOLD,
            "runtime_threshold": RUNTIME_BLACK_KEY_THRESHOLD,
            "preserves_enclosed_black_pixels": True,
        },
        "timeline": TIMELINE,
        "assets": assets,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
