#!/usr/bin/env python3
"""Build square first/last keyframes for Kling turret-install videos.

The last keyframe is composed from the exact runtime socket cover, turret base,
and turret head. Image-generated first-frame sources are normalized onto the
same 1024px transparent canvas with an 896px maximum mechanical footprint.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "assets" / "game-art" / "z-pixel-v2"
RUNTIME = PACK / "normalized" / "structures" / "runtime"
SOURCES = PACK / "source-sheets" / "kling-keyframes"
OUTPUT = PACK / "normalized" / "structures" / "runtime" / "kling-keyframes"
MANIFEST = PACK / "kling-turret-install-keyframes.json"
PROMPTS = PACK / "KLING_TURRET_INSTALL_PROMPTS.md"

CANVAS_SIZE = 1024
CONTENT_SIZE = 896
TOWER_FRAME_SIZE = 112
TOWER_ART_SIZE = 88
ALPHA_CUTOFF = 8

TOWER_TYPES = {
    "machine-gun": {"head_version": 1},
    "flamethrower": {"head_version": 1},
    "mortar": {"head_version": 1},
    "tesla-coil": {"head_version": 2},
}


def clean_alpha(image: Image.Image) -> Image.Image:
    image = image.convert("RGBA")
    alpha = image.getchannel("A").point(
        lambda value: 0 if value < ALPHA_CUTOFF else value
    )
    image.putalpha(alpha)
    return image


def square_canvas(image: Image.Image, *, source_is_pixel_art: bool) -> Image.Image:
    image = clean_alpha(image)
    bounds = image.getchannel("A").getbbox()
    if not bounds:
        raise RuntimeError("keyframe source must contain visible pixels")
    cropped = image.crop(bounds)
    scale = min(CONTENT_SIZE / cropped.width, CONTENT_SIZE / cropped.height)
    resampling = (
        Image.Resampling.NEAREST
        if source_is_pixel_art
        else Image.Resampling.LANCZOS
    )
    resized = cropped.resize(
        (
            max(1, round(cropped.width * scale)),
            max(1, round(cropped.height * scale)),
        ),
        resampling,
    )
    frame = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    frame.alpha_composite(
        resized,
        ((CANVAS_SIZE - resized.width) // 2, (CANVAS_SIZE - resized.height) // 2),
    )
    return clean_alpha(frame)


def active_runtime_frame(tower_type: str, head_version: int) -> Image.Image:
    cover = Image.open(RUNTIME / "tower-socket-cover-v1.png").convert("RGBA")
    cover = cover.resize(
        (TOWER_FRAME_SIZE, TOWER_FRAME_SIZE), Image.Resampling.NEAREST
    )
    base = Image.open(RUNTIME / f"{tower_type}-base-v1.png").convert("RGBA")
    head = Image.open(
        RUNTIME / f"{tower_type}-head-v{head_version}.png"
    ).convert("RGBA")
    base = base.resize((TOWER_ART_SIZE, TOWER_ART_SIZE), Image.Resampling.NEAREST)
    head = head.resize((TOWER_ART_SIZE, TOWER_ART_SIZE), Image.Resampling.NEAREST)
    inset = (TOWER_FRAME_SIZE - TOWER_ART_SIZE) // 2
    cover.alpha_composite(base, (inset, inset))
    cover.alpha_composite(head, (inset, inset))
    return clean_alpha(cover)


def build() -> dict[str, object]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    assets: list[dict[str, object]] = []
    for tower_type, config in TOWER_TYPES.items():
        last_path = OUTPUT / f"{tower_type}-install-last-v1.png"
        square_canvas(
            active_runtime_frame(tower_type, config["head_version"]),
            source_is_pixel_art=True,
        ).save(last_path, optimize=True)

        first_source = SOURCES / f"{tower_type}-install-first-source-v1.png"
        first_path = OUTPUT / f"{tower_type}-install-first-v1.png"
        if first_source.is_file():
            square_canvas(
                Image.open(first_source), source_is_pixel_art=False
            ).save(first_path, optimize=True)

        assets.append(
            {
                "tower_type": tower_type.replace("-", "_"),
                "first_source": str(first_source.relative_to(ROOT)),
                "first_frame": (
                    str(first_path.relative_to(ROOT)) if first_path.is_file() else None
                ),
                "last_frame": str(last_path.relative_to(ROOT)),
                "canvas_size": [CANVAS_SIZE, CANVAS_SIZE],
                "content_size_px": CONTENT_SIZE,
                "pivot": [0.5, 0.5],
                "duration_s": 3.0,
            }
        )

    manifest = {
        "schema_version": 1,
        "aspect_ratio": "1:1",
        "duration_s": 3.0,
        "canvas_size": [CANVAS_SIZE, CANVAS_SIZE],
        "prompt_file": str(PROMPTS.relative_to(ROOT)),
        "assets": assets,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
