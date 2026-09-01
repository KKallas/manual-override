#!/usr/bin/env python3
"""Build normalized runtime weapon layers from preserved ImageGen sources.

The generated source sheets stay untouched. This script slices their real alpha,
normalizes pivots, and draws the small deterministic rotating heads needed by the
Canvas renderer so barrel direction stays exact at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "assets" / "game-art" / "z-pixel-v2"
SOURCES = PACK / "source-sheets" / "runtime-effects"
STRUCTURES = PACK / "normalized" / "structures" / "runtime"
EFFECTS = PACK / "normalized" / "effects" / "combat"

TOWER_SOURCE = SOURCES / "turret-layers-source-v1.png"
PROJECTILE_SOURCE = SOURCES / "combat-projectiles-source-v1.png"
HEALTH_SOURCE = SOURCES / "tower-health-effects-source-v1.png"
FLAME_RIBBON_SOURCE = SOURCES / "flame-ribbon-source-v2.png"
FLAME_GASOLINE_SOURCE = SOURCES / "flame-gasoline-source-v3.png"
TESLA_HEAD_SOURCE = SOURCES / "tesla-coil-head-source-v2.png"
STRESS_CRACKS_SOURCE = SOURCES / "tower-stress-cracks-source-v1.png"
DESTRUCTION_BLAST_SOURCE = SOURCES / "tower-destruction-blast-source-v1.png"
TOWER_DEBRIS_SOURCE = SOURCES / "tower-debris-source-v1.png"

TOWER_TYPES = ("machine-gun", "flamethrower", "mortar", "tesla-coil")


def cell(sheet: Image.Image, column: int, row: int, columns: int, rows: int) -> Image.Image:
    left = round(sheet.width * column / columns)
    top = round(sheet.height * row / rows)
    right = round(sheet.width * (column + 1) / columns)
    bottom = round(sheet.height * (row + 1) / rows)
    return sheet.crop((left, top, right, bottom)).convert("RGBA")


def clean_alpha(image: Image.Image, cutoff: int = 10) -> Image.Image:
    image = image.copy().convert("RGBA")
    alpha = image.getchannel("A").point(lambda value: 0 if value < cutoff else value)
    image.putalpha(alpha)
    return image


def contain(
    image: Image.Image,
    size: tuple[int, int],
    padding: int = 8,
    *,
    vertical_alignment: str = "bottom",
) -> Image.Image:
    image = clean_alpha(image)
    bbox = image.getchannel("A").getbbox()
    target = Image.new("RGBA", size, (0, 0, 0, 0))
    if not bbox:
        return target
    cropped = image.crop(bbox)
    maximum_width = max(1, size[0] - padding * 2)
    maximum_height = max(1, size[1] - padding * 2)
    scale = min(maximum_width / cropped.width, maximum_height / cropped.height)
    resized = cropped.resize(
        (max(1, round(cropped.width * scale)), max(1, round(cropped.height * scale))),
        Image.Resampling.LANCZOS,
    )
    x = (size[0] - resized.width) // 2
    if vertical_alignment == "bottom":
        y = size[1] - padding - resized.height
    elif vertical_alignment == "center":
        y = (size[1] - resized.height) // 2
    else:
        raise ValueError(f"unsupported vertical alignment: {vertical_alignment}")
    target.alpha_composite(resized, (x, y))
    return target


def alpha_centroid_y(image: Image.Image, *, right: int | None = None) -> float:
    """Return the alpha-weighted vertical center of an image or leading strip."""
    alpha = image.getchannel("A")
    right = alpha.width if right is None else max(1, min(alpha.width, right))
    pixels = alpha.load()
    total = 0
    weighted = 0
    for y in range(alpha.height):
        row = sum(pixels[x, y] for x in range(right))
        total += row
        weighted += y * row
    if total == 0:
        raise ValueError("cannot calculate the centroid of an empty alpha channel")
    return weighted / total


def build_flame_gasoline() -> Path:
    """Center the flame centerline and verify its muzzle segment before saving."""
    EFFECTS.mkdir(parents=True, exist_ok=True)
    flame = contain(
        Image.open(FLAME_GASOLINE_SOURCE).convert("RGBA"),
        (384, 128),
        padding=0,
        vertical_alignment="center",
    )
    first_segment_width = max(1, round(flame.width / 18))
    root_center_y = alpha_centroid_y(flame, right=first_segment_width)
    if abs(root_center_y - flame.height / 2) > 1.0:
        raise RuntimeError(
            "flamethrower root centerline drifted: "
            f"{root_center_y:.2f} != {flame.height / 2:.2f}"
        )
    output = EFFECTS / "flame-gasoline-v3.png"
    flame.save(output, optimize=True)
    return output


def tesla_head_image() -> Image.Image:
    """Normalize the reference-inspired overhead coil around the runtime pivot."""
    return contain(
        Image.open(TESLA_HEAD_SOURCE).convert("RGBA"),
        (320, 320),
        padding=12,
        vertical_alignment="center",
    )


def build_tesla_head() -> Path:
    STRUCTURES.mkdir(parents=True, exist_ok=True)
    output = STRUCTURES / "tesla-coil-head-v2.png"
    tesla_head_image().save(output, optimize=True)
    return output


def pixel_line(draw: ImageDraw.ImageDraw, points: list[tuple[int, int]], fill: str, width: int) -> None:
    draw.line(points, fill=fill, width=width, joint="curve")


def draw_head(kind: str) -> Image.Image:
    image = Image.new("RGBA", (320, 320), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    outline = "#090b0d"
    dark = "#252a2e"
    mid = "#555a5d"
    light = "#aeb2ad"
    amber = "#ff9e16"
    cyan = "#36dfff"
    violet = "#b56cff"

    draw.ellipse((96, 96, 224, 224), fill=outline)
    draw.ellipse((104, 104, 216, 216), fill=dark, outline=light, width=8)
    draw.ellipse((126, 126, 194, 194), fill="#13171a", outline=amber, width=5)

    if kind == "machine-gun":
        for left in (120, 170):
            draw.rounded_rectangle((left, 38, left + 30, 164), radius=8, fill=outline)
            draw.rectangle((left + 6, 43, left + 24, 158), fill=mid)
            for y in (65, 96, 127):
                draw.rectangle((left + 5, y, left + 25, y + 7), fill=light)
            draw.rectangle((left + 8, 30, left + 22, 48), fill="#161a1e")
        draw.rectangle((142, 126, 178, 192), fill=mid, outline=outline, width=7)
        draw.rectangle((151, 142, 169, 177), fill=amber)
    elif kind == "flamethrower":
        draw.polygon(((132, 164), (122, 70), (140, 34), (180, 34), (198, 70), (188, 164)), fill=outline)
        draw.polygon(((141, 158), (135, 73), (148, 46), (172, 46), (185, 73), (179, 158)), fill=mid)
        draw.rectangle((139, 67, 181, 82), fill=light)
        draw.rectangle((143, 38, 177, 55), fill="#171b1e", outline=amber, width=5)
        draw.rectangle((150, 118, 170, 171), fill=amber, outline=outline, width=5)
    elif kind == "mortar":
        draw.ellipse((112, 92, 208, 188), fill=outline)
        draw.ellipse((121, 101, 199, 179), fill="#161a1d", outline=light, width=7)
        draw.polygon(((122, 143), (128, 50), (145, 29), (175, 29), (192, 50), (198, 143)), fill=outline)
        draw.polygon(((136, 137), (141, 56), (151, 42), (169, 42), (179, 56), (184, 137)), fill=mid)
        draw.ellipse((140, 30, 180, 63), fill=outline)
        draw.ellipse((147, 36, 173, 56), fill="#070809", outline=light, width=4)
        draw.rectangle((137, 92, 183, 105), fill=amber)
    elif kind == "tesla-coil":
        draw.ellipse((116, 104, 204, 192), fill=outline)
        for y, radius in ((176, 42), (154, 36), (132, 30), (110, 24)):
            draw.ellipse((160 - radius, y - 15, 160 + radius, y + 15), outline="#d07a24", width=8)
            draw.ellipse((160 - radius + 7, y - 8, 160 + radius - 7, y + 8), outline=light, width=4)
        draw.ellipse((129, 69, 191, 131), fill=outline)
        draw.ellipse((138, 78, 182, 122), fill="#e9ecff", outline=violet, width=7)
        pixel_line(draw, [(111, 136), (91, 117), (109, 94)], violet, 8)
        pixel_line(draw, [(209, 136), (229, 117), (211, 94)], violet, 8)
        draw.ellipse((86, 86, 116, 116), fill="#eef6ff", outline=cyan, width=5)
        draw.ellipse((204, 86, 234, 116), fill="#eef6ff", outline=cyan, width=5)
    else:
        raise ValueError(kind)
    return image


def socket_cover() -> Image.Image:
    image = Image.new("RGBA", (320, 320), "#11161b")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 319, 319), outline="#080a0c", width=18)
    draw.rectangle((18, 18, 301, 301), outline="#555d61", width=10)
    draw.rectangle((34, 34, 285, 285), fill="#252b30", outline="#111519", width=7)
    draw.rectangle((52, 52, 267, 267), fill="#1b2025", outline="#696f70", width=6)
    draw.rectangle((62, 150, 258, 170), fill="#0c0f12")
    draw.rectangle((150, 62, 170, 258), fill="#0c0f12")
    for x, y in ((30, 30), (266, 30), (30, 266), (266, 266)):
        draw.rectangle((x, y, x + 24, y + 24), fill="#36dfff", outline="#071015", width=5)
    return image


def composite_state(base: Image.Image, head: Image.Image, mode: str) -> Image.Image:
    result = Image.new("RGBA", (320, 320), (0, 0, 0, 0))
    result.alpha_composite(base)
    next_head = head
    if mode == "dormant":
        next_head = ImageEnhance.Brightness(ImageEnhance.Color(head).enhance(0.25)).enhance(0.62)
    elif mode == "upgraded-l5":
        next_head = head.resize((352, 352), Image.Resampling.NEAREST).crop((16, 16, 336, 336))
    elif mode == "damaged":
        next_head = ImageEnhance.Brightness(ImageEnhance.Color(head).enhance(0.35)).enhance(0.55)
    result.alpha_composite(next_head)
    if mode == "damaged":
        draw = ImageDraw.Draw(result)
        for points in (
            [(116, 104), (143, 133), (129, 166), (157, 192)],
            [(207, 119), (184, 145), (202, 172), (181, 205)],
        ):
            pixel_line(draw, points, "#08090a", 8)
            pixel_line(draw, points, "#ff573d", 3)
    return result


def tiled_property(name: str, value: object, kind: str = "string") -> dict[str, object]:
    return {"name": name, "type": kind, "value": value}


def write_metadata() -> None:
    runtime_assets = []
    for tower_type in TOWER_TYPES:
        for layer in ("base", "head"):
            version = 2 if tower_type == "tesla-coil" and layer == "head" else 1
            runtime_assets.append({
                "asset_id": f"runtime/{tower_type}-{layer}",
                "file": f"assets/game-art/z-pixel-v2/normalized/structures/runtime/{tower_type}-{layer}-v{version}.png",
                "category": "runtime_tower_layer",
                "normalized_size": [320, 320],
                "pivot": [0.5, 0.5],
                "rotates": layer == "head" and tower_type != "tesla-coil",
            })
    runtime_assets.append({
        "asset_id": "runtime/tower-socket-cover",
        "file": "assets/game-art/z-pixel-v2/normalized/structures/runtime/tower-socket-cover-v1.png",
        "category": "runtime_socket_cover",
        "normalized_size": [320, 320],
        "opaque": True,
    })
    effect_sizes = {
        "machine-gun-bullet": [200, 80],
        "flame-gasoline": [384, 128],
        "mortar-shell": [96, 144],
        "tesla-spark": [180, 180],
        "tower-smoke": [640, 192],
        "tower-fire": [640, 192],
        "tower-stress-cracks": [480, 160],
        "tower-destruction-blast": [768, 192],
        "tower-debris": [384, 192],
        "force-field-zap-skeleton": [320, 320],
    }
    for effect_name, size in effect_sizes.items():
        filename = "flame-gasoline-v3.png" if effect_name == "flame-gasoline" else f"{effect_name}-v1.png"
        frame_counts = {
            "tower-smoke": 4,
            "tower-fire": 4,
            "tower-stress-cracks": 3,
            "tower-destruction-blast": 4,
            "tower-debris": 8,
        }
        runtime_assets.append({
            "asset_id": f"runtime/{effect_name}",
            "file": f"assets/game-art/z-pixel-v2/normalized/effects/combat/{filename}",
            "category": "runtime_effect",
            "normalized_size": size,
            "frames": frame_counts.get(effect_name, 1),
        })
        if effect_name == "flame-gasoline":
            runtime_assets[-1]["pivot"] = [0.0, 0.5]
            runtime_assets[-1]["centerline"] = "alpha_centered"
        if effect_name == "tower-debris":
            runtime_assets[-1]["grid"] = [4, 2]
    manifest = {
        "version": 5,
        "source_sheets": [
            str(TOWER_SOURCE.relative_to(ROOT)),
            str(PROJECTILE_SOURCE.relative_to(ROOT)),
            str(HEALTH_SOURCE.relative_to(ROOT)),
            str(FLAME_RIBBON_SOURCE.relative_to(ROOT)),
            str(FLAME_GASOLINE_SOURCE.relative_to(ROOT)),
            str(TESLA_HEAD_SOURCE.relative_to(ROOT)),
            str(STRESS_CRACKS_SOURCE.relative_to(ROOT)),
            str(DESTRUCTION_BLAST_SOURCE.relative_to(ROOT)),
            str(TOWER_DEBRIS_SOURCE.relative_to(ROOT)),
        ],
        "assets": runtime_assets,
    }
    (PACK / "normalized" / "runtime-weapons.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    combat_metadata = {
        "version": 5,
        "purpose": "Generated combat feedback sprites for Laser Tag Z virtual play.",
        "style": "Top-down industrial sci-fi pixel art with transparent backgrounds.",
        "assets": [
            {"file": "machine-gun-impact-v1.png", "use": "Machine-gun muzzle and target impact flash"},
            {"file": "machine-gun-bullet-v1.png", "use": "Repeated directional machine-gun projectile stream"},
            {"file": "flame-burn-v1.png", "use": "Enemy burn overlay"},
            {"file": "flame-jet-v1.png", "use": "Legacy rigid flamethrower jet retained for compatibility"},
            {"file": "flame-ribbon-v2.png", "use": "Nozzle-free segmented flamethrower ribbon for curved motion"},
            {"file": "flame-gasoline-v3.png", "use": "Narrow nozzle-free gasoline stream bent along the synchronized flame spline"},
            {"file": "mortar-shell-v1.png", "use": "Forced-perspective airborne mortar shell"},
            {"file": "mortar-impact-v1.png", "use": "Staggered mortar area explosions"},
            {"file": "tesla-spark-v1.png", "use": "Tesla chain contact spark"},
            {"file": "tower-smoke-v1.png", "use": "Four-frame smoke strip below 30 percent tower health"},
            {"file": "tower-fire-v1.png", "use": "Four-frame fire strip below 10 percent tower health"},
            {"file": "tower-stress-cracks-v1.png", "use": "Three severity frames below 50, 30, and 10 percent tower health"},
            {"file": "tower-destruction-blast-v1.png", "use": "Four-frame universal tower destruction blast"},
            {"file": "tower-debris-v1.png", "use": "Four-by-two universal tower component atlas"},
            {"file": "force-field-impact-v1.png", "use": "Force-field contact and break feedback"},
            {"file": "force-field-zap-skeleton-v1.png", "use": "Half-second cyan-white skeleton overlay for an orc struck by a force field"},
        ],
    }
    (EFFECTS / "combat-effects.json").write_text(
        json.dumps(combat_metadata, indent=2) + "\n", encoding="utf-8"
    )

    tileset_path = ROOT / "assets" / "tiled" / "tilesets" / "z-pixel-v2-towers.tsj"
    tileset = json.loads(tileset_path.read_text(encoding="utf-8"))
    tileset["tiles"] = [tile for tile in tileset["tiles"] if int(tile["id"]) < 16]
    for offset, state in enumerate(("dormant", "active-l1", "upgraded-l5", "damaged")):
        tileset["tiles"].append({
            "id": 16 + offset,
            "image": f"../../game-art/z-pixel-v2/normalized/structures/tesla-coil-{state}.png",
            "imagewidth": 320,
            "imageheight": 320,
            "properties": [
                tiled_property("asset_id", f"structures/tesla-coil-{state}"),
                tiled_property("module_family", "towers"),
                tiled_property("normalized_size", 320, "int"),
                tiled_property("pixel_filter", "nearest"),
                tiled_property("tower_type", "tesla-coil"),
                tiled_property("tower_state", state),
            ],
        })
    tileset["tilecount"] = len(tileset["tiles"])
    tileset_path.write_text(json.dumps(tileset, indent=2) + "\n", encoding="utf-8")


def build() -> None:
    STRUCTURES.mkdir(parents=True, exist_ok=True)
    EFFECTS.mkdir(parents=True, exist_ok=True)

    tower_sheet = Image.open(TOWER_SOURCE).convert("RGBA")
    bases: dict[str, Image.Image] = {}
    heads: dict[str, Image.Image] = {}
    for index, tower_type in enumerate(TOWER_TYPES):
        base = contain(cell(tower_sheet, index, 0, 4, 2), (320, 320), padding=5)
        head = tesla_head_image() if tower_type == "tesla-coil" else draw_head(tower_type)
        bases[tower_type] = base
        heads[tower_type] = head
        base.save(STRUCTURES / f"{tower_type}-base-v1.png", optimize=True)
        head_version = 2 if tower_type == "tesla-coil" else 1
        head.save(
            STRUCTURES / f"{tower_type}-head-v{head_version}.png",
            optimize=True,
        )

    socket_cover().save(STRUCTURES / "tower-socket-cover-v1.png", optimize=True)
    for state in ("dormant", "active-l1", "upgraded-l5", "damaged"):
        composite_state(bases["tesla-coil"], heads["tesla-coil"], state).save(
            PACK / "normalized" / "structures" / f"tesla-coil-{state}.png",
            optimize=True,
        )

    projectile_sheet = Image.open(PROJECTILE_SOURCE).convert("RGBA")
    projectile_specs = (
        ("machine-gun-bullet-v1.png", (200, 80)),
        ("flame-jet-v1.png", (256, 128)),
        ("mortar-shell-v1.png", (96, 144)),
        ("tesla-spark-v1.png", (180, 180)),
    )
    for index, (filename, size) in enumerate(projectile_specs):
        contain(cell(projectile_sheet, index, 0, 4, 1), size, padding=4).save(
            EFFECTS / filename, optimize=True
        )

    contain(
        Image.open(FLAME_RIBBON_SOURCE).convert("RGBA"),
        (256, 128),
        padding=0,
    ).save(EFFECTS / "flame-ribbon-v2.png", optimize=True)

    build_flame_gasoline()

    crack_sheet = Image.open(STRESS_CRACKS_SOURCE).convert("RGBA")
    crack_frames = [contain(cell(crack_sheet, index, 0, 3, 1), (160, 160), padding=3) for index in range(3)]
    cracks = Image.new("RGBA", (160 * 3, 160), (0, 0, 0, 0))
    for index, frame in enumerate(crack_frames):
        cracks.alpha_composite(frame, (index * 160, 0))
    cracks.save(EFFECTS / "tower-stress-cracks-v1.png", optimize=True)

    blast_sheet = Image.open(DESTRUCTION_BLAST_SOURCE).convert("RGBA")
    blast_frames = [contain(cell(blast_sheet, index, 0, 4, 1), (192, 192), padding=2) for index in range(4)]
    blast = Image.new("RGBA", (192 * 4, 192), (0, 0, 0, 0))
    for index, frame in enumerate(blast_frames):
        blast.alpha_composite(frame, (index * 192, 0))
    blast.save(EFFECTS / "tower-destruction-blast-v1.png", optimize=True)

    debris_sheet = Image.open(TOWER_DEBRIS_SOURCE).convert("RGBA")
    debris = Image.new("RGBA", (96 * 4, 96 * 2), (0, 0, 0, 0))
    for row in range(2):
        for column in range(4):
            frame = contain(cell(debris_sheet, column, row, 4, 2), (96, 96), padding=4)
            debris.alpha_composite(frame, (column * 96, row * 96))
    debris.save(EFFECTS / "tower-debris-v1.png", optimize=True)

    health_sheet = Image.open(HEALTH_SOURCE).convert("RGBA")
    for row, effect_name in ((0, "tower-smoke-v1.png"), (1, "tower-fire-v1.png")):
        frames = [contain(cell(health_sheet, index, row, 4, 2), (160, 192), padding=3) for index in range(4)]
        strip = Image.new("RGBA", (160 * 4, 192), (0, 0, 0, 0))
        for index, frame in enumerate(frames):
            strip.alpha_composite(frame, (index * 160, 0))
        strip.save(EFFECTS / effect_name, optimize=True)

    write_metadata()

    print(f"wrote runtime structures to {STRUCTURES.relative_to(ROOT)}")
    print(f"wrote runtime effects to {EFFECTS.relative_to(ROOT)}")


if __name__ == "__main__":
    build()
