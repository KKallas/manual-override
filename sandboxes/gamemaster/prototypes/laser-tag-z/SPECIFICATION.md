# Laser Tag Z — Tower Defense integration specification

## Product boundary

Laser Tag Z is a separate Gamemaster prototype copied from Laser Tag X. The
original Laser Tag X prototype, routes, settings, logs, score history, and page
assets remain unchanged. Laser Tag Z owns its own mutable settings and log
paths.

Laser Tag Z replaces only the Laser Tag X gamefield and gameplay overlay. It
retains Laser Tag X's corrected overhead camera, frame-current ArUco tracking,
and read-only calibrated arm visualization for physical play. The Tower Defense
map and gameplay are a separate visual feed and never replace or weaken the
existing robot-control and safety boundaries.

## Gamemaster pages

- `game`, labeled **Tower Defense**, is the default page.
- `settings`, labeled **Tower Defense settings**, tunes future runs.
- `stats`, labeled **Score history**, reads Laser Tag Z storage only.
- `screen` is a clean external Tower Defense display launched from `game`; it is
  not a separate navigation tab in the Gamemaster manifest.

Mutating routes require the authenticated `gamemaster` role.

## Display topology and mode switching

The Gamemaster page and external game display are two views of the same
server-authoritative Tower Defense state.

With **Virtual play** unchecked, the Gamemaster stage shows only the corrected
overhead camera, frame-current ArUco outlines, and both calibrated robot-arm
overlays. The registered Tower Defense map, towers, force fields, orcs, and core
health are hidden in that stage. The camera is contained without cropping so
all overlays share its coordinate system.

Each connected Green or Purple arm is projected from its live relay TCP pose
through that arm's existing six-point Auto PP Cal 2 model. The overlay is a
read-only line of red square cells from the calibrated base direction to the
TCP, labeled by side. Invalid, disconnected, uncalibrated, or more than
1.5-second-stale poses are removed rather than frozen. Rendering an arm never
issues a robot command and never changes placement authority.

The `game` page provides a button labeled **Open game screen ↗** that launches
`screen` in a separate browser window or tab. That external screen fills its
viewport with the complete 1696×960 Tower Defense feed: registered map,
towers, force fields, animated orcs, and core health. It consumes the same
state snapshot and Server-Sent Events stream as the Gamemaster page and remains
usable in both physical and virtual modes. It contains no camera image and no
operator controls.

With **Virtual play** checked, the Gamemaster stage replaces the camera,
tracking, and arm overlays with the same complete Tower Defense feed shown by
the external screen. Toggling either direction does not reset the run,
placements, wave clock, or external display. Hidden camera and arm-rendering
clients may be suspended, but the server-side camera and physical-ingest safety
path remain unchanged.

## Map and marker contract

The production map is `assets/tiled/levels/z-pixel-first-map.tmj`. It remains a
modular, editable TMJ/TSJ project and must pass the bundled validator plus a
native Tiled round-trip and render.

- Sixteen fixed tower sockets use ArUco IDs 40 through 55 exactly once.
- Every authored socket starts with a 208×208 visual footprint. Its center panel
  renders the dictionary-correct ArUco code instead of a generic grey plate and
  text label.
- Four movable Atom activation units use IDs 100 through 103 exactly once.
- Green owns units 100 and 101.
- Purple owns units 102 and 103.
- All four units begin in the right-side grey staging area.
- Four authored left approaches converge through the path graph on the central
  point.

The shared Webcam detector retains its existing DICT_4X4_50 and Atom-screen
detection. An additive DICT_4X4_100 pass is filtered to IDs 50–55 so the full
fixed range can be observed without changing the existing camera feed.

Before a run, the authenticated Gamemaster may open **Edit turret positions**,
drag sockets with pixel precision, hold Shift for an optional 8-pixel
corner-alignment grid, enter exact center coordinates, and resize the selected
square from 96 to 320 pixels. Save validates all sixteen stable socket IDs,
updates their linked force-field hints, writes the TMJ atomically, clears setup
placements, and reloads both the authoritative engine and connected displays.
Cancel leaves the published TMJ unchanged. Layout editing never issues an arm
command.

## Physical placement and arms

Physical play is the default. Robot movement remains exclusively behind the
existing Green/Purple Player LTX controls and MG400 relay safety boundary; Laser
Tag Z never creates a second motion path.

A physical placement becomes authoritative only when:

1. camera calibration/correction is valid;
2. a fixed marker and a uniquely owned Atom marker overlap stably;
3. the matching Green or Purple arm is connected and enabled; and
4. the arm pump is off, proving the unit has been released.

Missing, stale, uncalibrated, wrong-owner, occupied, or ambiguous evidence fails
closed. Moving a unit away removes its former activation after the stability
window.

## Virtual play

The game page contains a checkbox whose exact label is **Virtual play**. When
enabled, the Gamemaster selects an Atom tag in the Camera markers panel and
then selects a fixed camera marker on the map. Atom 100 always activates a
Machine Gun, 101 a Flamethrower, and 102 a Mortar. Atom 103 is the reserve
reset unit: placing it on a destroyed defence unit restores that unit without
creating a fourth gun. Re-placing a destroyed unit's original Atom tag also
restores it. Virtual placement uses the same occupancy and activation rules as
physical play; it does not claim camera evidence and never weakens robot
safety.

Force fields are absent during setup. After Start, each newly activated unit
may link to the previously activated unit when the segment has clean line of
sight. Any inactive socket whose 208-pixel footprint crosses the segment blocks
that link. Every field endpoint is an active, living defence unit. Each field
counts one impact per unique orc, turns the orc back toward an alternate path,
and breaks after its configured impact capacity. Destroying an endpoint also
removes its field.

## Run behavior

The Gamemaster can Start, Pause, Resume, and Reset. Start immediately launches
wave 1. Orcs spawn from the authored left-side lanes, advance along the path
graph, and continuously attack the central point after reaching it. Machine
Guns provide fast single-target damage, Flamethrowers provide short-range area
damage plus a three-second burn, and Mortars provide aimed splash damage. Each
defence unit starts with health equal to 15 percent of the run's central-point
integrity by default. Orcs close enough to a unit damage it until it becomes
inactive and requires an Atom-tag reset.

The Gamemaster may select any active gun and adjust its direction and reach.
Machine Guns and Flamethrowers interpolate between narrow/long and wide/short
targeting cones. Mortars use an aimed target circle that grows and loses damage
intensity as it moves farther away. Fine line overlays show all automatic
targeting areas, with the selected unit highlighted on the Gamemaster view and
mirrored on the external display.

The server is authoritative and pushes snapshots with Server-Sent Events. The
browser renders those snapshots. There are never more than 1,000 living active
enemies; excess scheduled pressure is bounded.

## Settings

The Gamemaster-only Tower Defense settings page controls:

- number of waves and interval between wave launches;
- orc release rate and count per wave;
- active-orc cap, never above 1,000;
- orc health, movement speed, and central-point attack damage;
- central-point integrity;
- defence-unit health as a percentage of central-point integrity;
- Machine Gun, Flamethrower direct/burn, and Mortar near/far damage; and
- force-field impact capacity, impact damage, and turnaround factor.

All fields use finite bounded validation with field-level errors. Balanced,
Training, and Onslaught presets plus Reset to defaults are available. Start
copies the validated draft into an immutable run snapshot, so later edits affect
only the next run.
