# Generation prompts

All five source families were generated with the built-in image-generation
tool. The simplified gameplay concept was supplied as a style reference only.

## Shared direction

Classic *Z* by The Bitmap Brothers visual character: direct-overhead 1990s
British Amiga/DOS strategy pixel art, hard-edged pixels, chunky readable
military-industrial silhouettes, limited charcoal/gunmetal/dirty-gray/muted
amber palette with restrained green, violet, and cyan. Preserve intricate
hand-authored mechanical detail at sprite scale while using roughly 50% less
visual clutter than the earlier painted concept. No text, labels, perspective,
robot arms, or fake gameplay scenery.

## Roads — 4×4

Create exactly sixteen isolated, independently sliceable road modules in a
regular 4×4 grid: horizontal and vertical straights; four corners; four T
junctions; cross; Y merge; two U-turns; S bend; and four-to-two merge. Every
cell has one equal square footprint. Every road uses the same channel width and
curb thickness and reaches the midpoint of its connected edge. Roads are
sunken channels with a dark bed, bright upper rim, and deep inner-wall shadow,
never elevated bridges. Use transparent gutters and no extra objects.

## Square tag targets — 4×4

Create exactly sixteen targets with stable centers and footprints. Columns are
neutral, Green, Purple, and shared amber. Rows are inactive, detected, active,
and stressed. Every target has one solid square physical tag plate centered on
top. The tag never changes size or position. Activation changes only the outer
collar: corner lights appear, the collar expands into a stepped-octagonal
silhouette, then becomes segmented when stressed. No circular sockets, towers,
roads, beams, or scenery. Use transparent gutters.

## Towers — 4×4

Create exactly sixteen towers. Columns are twin-barrel machine gun, broad-nozzle
flamethrower, heavy mortar, and faceted photon detonator. Rows are dormant,
active level 1, upgraded level 5, and damaged. All sprites share one center
pivot and compact base footprint. Tower types must remain recognizable at 64
pixels by silhouette. Do not draw an activation pad under a tower. Use
transparent gutters.

## Core — 2×2

Create stable, stressed, critical, and destroyed states of one compact central
reactor. Keep the same center and maximum footprint. Use a square-meets-octagon
armored silhouette, four short cardinal conduits, and a violet-white aperture.
Progress from clean energy through amber warnings and red-violet fractures to a
dark collapsed state. No road, platform, target, tower, or scenery. Use
transparent gutters.

## Ground — 2×2

Create four opaque, edge-to-edge industrial ground tiles: clean gunmetal,
lightly cracked concrete/steel, sparse service panels, and restrained energy
conduits. Keep the same tile scale and line weight, large calm areas, no roads,
curbs, targets, props, crates, landmarks, shadows, or gradients. Each cell must
be independently sliceable and suitable for nearest-neighbor rendering.

## Force-field walls — 2×2

Using the earlier wall row as a behavioral reference and the Z pixel map as the
style reference, create stable cyan, stressed cyan, cooperative green-violet,
and breaking violet-cyan vertical energy membranes. Every sprite uses the same
tall narrow footprint and is designed to span one horizontal road between two
separately rendered active square targets. Do not include endpoint towers,
posts, sockets, roads, or scenery. Use coarse pixel scanlines, restrained
sparks, and readable cracks instead of glossy modern bloom.

## Transparency cleanup

For targets, towers, core, and force-field walls, the built-in
background-extraction edit removed
the generated checkerboard and preserved the grid. The road extraction changed
the sheet aspect ratio, so it was rejected. Roads are sliced from the untouched
square source sheet; the build script removes only the connected pale
checkerboard during deterministic derivative preparation.
