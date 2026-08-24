# Photon Yard Z pixel v2 modular asset pass

This versioned pack translates the simplified Photon Yard visual direction into
editable bitmap modules. It is not a flattened map background and it contains
no illustrated robot-arm hardware.

## Contents

| Family | Source layout | Normalized sprites | Tiled tileset |
|---|---:|---:|---|
| Sunken roads | 4×4 | 16 | `z-pixel-v2-roads.tsj` |
| Square tag targets | 4×4 | 16 | `z-pixel-v2-targets.tsj` |
| Towers | 4×4 | 16 | `z-pixel-v2-towers.tsj` |
| Central core | 2×2 | 4 | `z-pixel-v2-core.tsj` |
| Ground | 2×2 | 4 | `z-pixel-v2-ground.tsj` |
| Force-field walls | 2×2 | 4 | `z-pixel-v2-force-fields.tsj` |
| **Total** |  | **60** | **6 tilesets** |

Every production sprite under `normalized/` uses a 320×320 canvas. Placement
sprites have clean transparency; ground remains opaque. Use nearest-neighbor
filtering in PixiJS and Tiled previews.

## Visual rules

- Direct-overhead 1990s British Amiga/DOS strategy-game pixel art.
- Chunky military-industrial silhouettes with restrained micro-detail.
- Charcoal, gunmetal, dirty gray, muted amber, green, violet, and sparse cyan.
- Recessed road beds with a consistent curb and square meeting footprint.
- Approximately half the decorative density of the earlier painted pack.
- Square physical tag plates stay centered in every activation state.
- Active target collars expand into stepped silhouettes and change team color.
- No robot arms, crane bases, grippers, or arm segments are included.
- Force walls are vertical route blockers rendered separately from their active target endpoints.

## Target state matrix

Columns are neutral, Green, Purple, and shared amber. Rows are inactive,
detected, active, and stressed. The tag plate itself remains stable while the
surrounding collar communicates state.

## Tower state matrix

Columns are machine gun, flamethrower, mortar, and photon detonator. Rows are
dormant, active level 1, upgraded level 5, and damaged.

## Road metadata

The Tiled road tileset records visually verified `ports`, `port_profile`, a
160-pixel snap step, recessed treatment, and rotation safety. Do not infer
connectivity from a generated filename.

Three requested T shapes were rendered as four-way cross variants. They remain
usable alternate cross textures. `junction-t-esw` is the verified T module and
can be rotated for the other three orientations. `corner-es` and `corner-sw`
share the same S,W contacts; rotate a verified corner when another orientation
is required.

## Build and verification

```sh
PYTHONDONTWRITEBYTECODE=1 <bundled-python> tools/build_z_pixel_asset_pass.py
PYTHONDONTWRITEBYTECODE=1 <bundled-python> .agents/skills/tiled-modular-map-builder/scripts/normalize_components.py \
  --manifest assets/game-art/z-pixel-v2/asset-pack.json \
  --output assets/game-art/z-pixel-v2/normalized --size 320 \
  --categories ground,roads,structures,objectives
PYTHONDONTWRITEBYTECODE=1 <bundled-python> tools/build_z_pixel_tiled_tilesets.py
PYTHONDONTWRITEBYTECODE=1 <bundled-python> tools/validate_z_pixel_asset_pass.py
```

The deterministic validator reports 60 valid assets and no errors. All six
TSJ files also pass a Tiled 1.12.2 JSON round-trip; see
`tiled-verification.json`.
