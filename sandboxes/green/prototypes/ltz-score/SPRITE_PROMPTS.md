# LTZ Score sprite-generation notes

Style references:

- `assets/game-art/source-sheets/cranes-4x4.png` for the three control-system sprites.
- `assets/game-art/source-sheets/hud-skin-4x4.png` for the lock shutter.

Final production sprites are stored in `assets/` beside this file. Generated
masters remain in the Codex generated-image store; the project copies were
normalized to 320 px square for control icons and 640 px wide for the shutter.

## `assets/control-xyz-v1.png`

> Using the attached Z-style industrial crane sprite sheet only as visual style
> reference, create one new square game UI sprite for the Cartesian XYZ
> control-system upgrade. Orthographic/top-down three-quarter pixel-art
> aesthetic, chunky hand-pixeled edges, weathered bronze and gunmetal
> machinery, a compact robotic arm controller module centered inside a cyan
> three-axis X/Y/Z wireframe gizmo, three clearly different axis arrows, subtle
> electric-blue lamps, and a bold readable silhouette at small size. Use a
> genuinely transparent background. Include no frame, text, numerals, UI panel,
> or extra objects.

## `assets/control-click-v1.png`

> Using the attached Z-style industrial crane sprite sheet only as visual style
> reference, create one new square game UI sprite for a point-and-click image
> targeting control upgrade. Use a weathered bronze and gunmetal robotic arm
> module, a bright cyan target reticle projected onto a small dark camera plate,
> and a stout mechanical pointer pressing the reticle. Keep the orthographic
> pixel-art silhouette readable at 96 px with a genuinely transparent
> background. Include no frame, words, numerals, mouse illustration, human hand,
> or extra objects.

## `assets/control-cue-v1.png`

> Using the attached Z-style industrial crane sprite sheet only as visual style
> reference, create one new square game UI sprite for cue-based automatic
> robot-arm control. Use a weathered bronze and gunmetal robotic arm command
> core, a glowing cyan sensor eye reading three pulsing beacon nodes that form a
> path, and an arm following that signal. Keep the emblem-like pixel-art
> silhouette legible at 96 px. Include no frame, words, numerals, people,
> musical notes, or extra objects.

Transparency correction:

> Preserve the bronze robotic arm, circular cyan three-node cue display,
> silhouette, palette, and pixel detail. Remove every checkerboard square and
> replace the background with genuine fully transparent alpha. Do not add a new
> background, texture, floor, frame, text, or shadow.

## `assets/upgrade-lock-shutter-v1.png`

> Using the attached Z-style HUD sprite sheet only as visual style reference,
> create one new rectangular mechanical locked upgrade-card shutter sprite.
> Straight-on orthographic pixel art, wide 2:1 aspect ratio, dark gunmetal
> center, weathered bronze reinforced border, central heavy bronze padlock with
> cyan status slit, four corner bolts, and symmetrical left/right construction
> so it can split open from the center. Keep the panel opaque inside its outline
> and transparent outside its stepped metal silhouette. Include no words,
> numerals, background scene, extra objects, or perspective tilt.

Transparency correction:

> Preserve the complete gunmetal-and-bronze shutter, central padlock, cyan
> lamps, rectangular proportions, and pixel-art detail. Replace every pale
> pixel outside the metal silhouette with genuine transparent alpha without
> cropping the metal. Keep the panel interior fully opaque.

## `art/structures/upgrades/*-upgrade-l{2,3,4}-v1.png`

Final turret-upgrade prompt set (built-in image generation, precise-object
edit):

> Use the matching L1 turret as the edit target and identity reference. Create
> a unique top-down three-quarter Z Bitmap Brothers industrial pixel-art
> upgrade sprite. Preserve the recognizable weapon head and maximum horizontal
> footprint; make the turret taller only by integrating straight, faceted,
> load-bearing armor tiers into the base. Level 2 has one emerald-green tier.
> Level 3 has emerald-green on the bottom and cyan-blue directly above. Level 4
> has emerald-green on the bottom, cyan-blue in the middle, and amber-orange on
> top. Add weapon-specific bolts, vents, braces, and machinery so every tier is
> a distinct sprite. Use genuine transparent RGBA with the complete sprite
> visible and padded. No rings, circles, halos, floating indicators, detached
> diamonds, arrows, text, backdrop, checkerboard, shadow, blur, or cropping.

Weapon-specific identity constraints:

- Machine gun: retain paired barrels; upgrade shrouds, feeds, and cooling.
- Flamethrower: retain the central flame and paired fuel nozzles; upgrade heat
  shields, feed lines, and the combustion chamber.
- Mortar: retain one large open bore; upgrade recoil dampers, loaders, and the
  siege breech without adding a second barrel.
- Photon detonator: retain one central cyan crystal; upgrade focus vanes,
  capacitors, conduits, and the singularity-core cradle without detached gems.

Transparency correction used when a generated preview background was baked
into a candidate:

> Remove only the checkerboard or white preview background and return the
> existing turret as a clean hard-edged RGBA cutout. Pixels outside the exact
> turret silhouette must be alpha 0. Preserve the turret design, tier colors,
> pixel edges, proportions, viewpoint, padding, and composition unchanged. Do
> not redraw, crop, resize, soften, add shadow, add glow, or add objects.
