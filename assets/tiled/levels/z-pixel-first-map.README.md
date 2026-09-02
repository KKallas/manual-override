# Z pixel first map

Open `../photon-crane.tiled-project` in Tiled 1.12.2, then open
`z-pixel-first-map.tmj`.

## Route layout

Four enemies lanes enter from the left: two upper and two lower. Each lane pair
has crossovers at x=400 and x=1040. A force wall can close the following lane
segment and make enemies use the vertical crossover, travel along the paired
lane, and optionally switch back at the second crossover.

At the far right, each pair merges, turns through 180 degrees, and enters one
return route. The upper and lower return routes both converge on the central
mega tower at x=880, y=480.

The two return lanes now terminate at explicit upper and lower arrival nodes
before their short final approach to the core. A hidden 256×256 no-build zone
keeps this central arrival plaza free for enemies; it permits enemy traversal
and preserves both approach lanes after the maze converges.

The two platform-bearing road tiles immediately above and below the core have
been removed. A centered 256×256 open sunken-road plaza now overlaps the ends
of both arrival lanes, leaving the square objective accessible from every side
inside its clearance area. The plaza is a modular derivative of the road-bed
texture rather than a flattened map edit.

The plaza perimeter uses the same recessed industrial curb pixels as the road
modules. Its upper and lower edges are closed and its left and right edges each
contain two lane-width openings aligned to the arrival roads, eliminating the
former hard square cut while preserving all four approaches into the clearing.
The side openings extend cleanly into the upper and lower corners, so no short
vertical curb stubs remain attached to the plaza's top or bottom edge.
The four ends of the horizontal curbs use a short pixel-stepped taper rather
than a square terminal face, removing the remaining vertical end-cap lines at
the highlighted lane junctions.

The core damage zone now matches the full 144×144 objective footprint and
allows simultaneous contact attackers. Orcs reaching the plaza can therefore
surround the square and continuously drain its health instead of queueing at
the former narrow T-junction mouths.

Two additional direct lines branch from the inner left entries into the open
middle band. One approaches the core from y=400 and the other from y=560. These
short routes reach the central tower without travelling to the right-side
turnarounds, creating a faster emergency lane alongside the longer gate-driven
routes.

At x=80, the existing upper and lower vertical branches now continue through
the middle as one bidirectional passthrough. Enemies can switch between the two
direct lines before committing to the central approach.

A second bidirectional passthrough at x=400 extends the existing upper and
lower crossover junctions through both direct lanes. Moving this full modular
column 160 pixels left preserves the routing topology while opening a high-
ground strip beside the tag-50 turret placement.

## Placement and gates

- Exactly 16 logical square turret sockets remain authored at 208×208 pixels
  and visible in Tiled for editing. The gameplay renderer hides this placement
  art while a socket is empty or destroyed, leaving only its ArUco code visible.
- Fixed ArUco IDs 40–55 are permanently rendered immediately to the left or
  right of the 112×112 active-turret position on adjacent high ground. Their
  inner edge touches the turret pod position and their vertical center uses the
  authored optical alignment. Activating, destroying, or replacing a turret
  never moves its marker; marker identity remains bound to the same stable
  socket and force-field endpoint.
- Virtual activation accepts clicks only inside the rendered ArUco square; the
  larger editor socket is not a gameplay hit target. Physical activation keeps
  the same direct fixed-code/Atom overlap requirement.
- A newly placed or replacement turret plays a 72-frame, 24-fps, three-second
  deployment before it can fire: the concrete slab rotates open like a trap
  door, the turret rotates into place around its axis, stabilizers lock, the
  weapon extends, and red/amber/cyan calibration lights settle to active.
  Replenishing an already-active turret preserves its position and uses a
  0.35-second status-light pulse without replaying deployment.
- Tag 50 is side-mounted to the left of socket 11 in the clearance created by
  the x=400 crossover column.
- Six Purple, six Green, and four shared spots.
- Eight legal force-wall endpoint pairs.
- The published turret centers are part of the deterministic revision-17
  builder, so regeneration preserves the operator-edited layout.
- Layout editing prevents code/code and code/pad overlaps, keeps side-mounted
  markers inside the playfield, and limits turret-pad size to 96–208 pixels.
- Four visible preview walls consume eight active endpoints, matching the
  maximum of eight active defense structures.
- Runtime walls are legal only while both referenced endpoint tags/towers are
  active.
- Every gate records its blocked path edge and preferred crossover detour.
- The active preview is illustrative; inactive gate objects remain editable in
  the Tiled layer with visibility disabled.

## Modular construction

- 18 opaque ground objects cover the canvas.
- Road modules render at 160×160 on the 160px grid, an exact 1:2 reduction of
  the 320px pixel-art sources. This replaces the old 164px non-integer scale
  that created uneven curb pixels at rotated seams.
- 58 grid-road objects use the standard recessed modules, plus two small seam
  caps and one 256×256 open core-access plaza. Both
  right-side downward corners reuse the centered upward-corner art with a
  counterclockwise rotation, avoiding the off-center south port in the
  generated `corner-es` sprite.
- The two unrotated upward corners use documented `(+3, -6)` pixel visual
  offsets, aligning both their north ports with the T junctions and their west
  ports with the horizontal straights. The two rotated downward corners use a
  `(0, -2)` correction for the smaller west-port discrepancy. The two rotated
  T junctions also use `(0, -2)` because their west mouths share that measured
  discrepancy. Logical grid and path geometry remain unchanged.
- The road layer uses index draw order with the two right-side T junctions
  placed last. Their straight south lips cover the four-pixel overlap where the
  generated upward-corner sprite briefly tapers inward at its north edge.
- Two native Tiled seam-cap objects finish the repeated right-turn joins. Each
  rotates the 6×66 tile object sourced from an interior slice of normalized
  straight-horizontal art via `z-pixel-v2-seam-caps.tsj`.
- Standard road objects are sourced through `z-pixel-v2-seam-safe-roads.tsj`.
  Its derivatives extend opaque road-bed pixels eight source pixels into each
  declared open port. This removes the one-pixel ground lines that previously
  appeared at module boundaries across the whole map while leaving the original
  generated and normalized bitmaps unchanged.
- The center plaza is sourced independently through
  `z-pixel-v2-core-plaza.tsj`. Its 512×512 source renders at 256×256, preserving
  the map's exact 1:2 pixel-art scale while covering the removed center cells.
- Road art, path geometry, gate logic, placement spots, reach areas, gameplay
  zones, and the mega tower are separate layers.
- The central core uses the same 144×144 square footprint as a placement target,
  with a 96×96 photon crown layered above it. The former oversized circular
  reactor presentation is no longer used, so the final roads remain visible and
  enemies have open space around the objective.
- The square core, its 144×144 multi-attacker damage zone, its 256×256 open
  plaza and arrival-clearance zone, and the two final core-approach edges remain
  independently editable in Tiled.
- No robot arms or decorative industrial props are drawn.
- The style reference is locked and hidden.

## Files

- `z-pixel-first-map.tmj` — editable Tiled map.
- `z-pixel-first-map.waves.json` — initial twelve-wave configuration.
- `../../game-art/z-pixel-v2/runtime-activation.json` — activation timing and
  four-turret sprite-sheet contract.
- `../tilesets/z-pixel-v2-seam-safe-roads.tsj` — all production road modules,
  with opaque open-port edges.
- `../tilesets/z-pixel-v2-core-plaza.tsj` — the open center-access road module.
- `../../../tools/build_core_access_plaza.py` — deterministic generator for the
  plaza bitmap and Tiled tileset.
- `../../../tools/build_seam_safe_roads.py` — deterministic map-wide open-port
  normalization and opacity validation.
- `../z-pixel-first-map-validation.json` — builder validation.
- `../z-pixel-first-map-modular-validation.json` — project-skill validation.
- `../previews/z-pixel-first-map-tiled-render.png` — Tiled-native render.
- `../previews/z-pixel-central-core-clearance.png` — focused visual audit of
  the square core and its two open arrival lanes.
- `../previews/z-pixel-central-core-access.png` — focused audit of the open
  multi-attacker plaza around the core.
- `../previews/z-pixel-right-corner-alignment.png` — focused right-corner seam
  audit from the Tiled-native render.
- `../previews/z-pixel-first-map-tiled-render.tiled-verification.json` — native
  round-trip result.

## Regeneration

```sh
PYTHONDONTWRITEBYTECODE=1 <bundled-python> tools/build_core_access_plaza.py
PYTHONDONTWRITEBYTECODE=1 <bundled-python> tools/build_turret_activation_assets.py
PYTHONDONTWRITEBYTECODE=1 <bundled-python> tools/build_z_pixel_first_map.py
PYTHONDONTWRITEBYTECODE=1 <bundled-python> \
  .agents/skills/tiled-modular-map-builder/scripts/validate_modular_map.py \
  assets/tiled/levels/z-pixel-first-map.tmj \
  --report assets/tiled/z-pixel-first-map-modular-validation.json
PYTHONDONTWRITEBYTECODE=1 <bundled-python> \
  .agents/skills/tiled-modular-map-builder/scripts/tiled_verify.py \
  --project assets/tiled/photon-crane.tiled-project \
  --map assets/tiled/levels/z-pixel-first-map.tmj \
  --render assets/tiled/previews/z-pixel-first-map-tiled-render.png
```
