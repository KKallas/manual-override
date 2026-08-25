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
- Four movable Atom activation units use IDs 100 through 103 exactly once.
- Green owns units 100 and 101.
- Purple owns units 102 and 103.
- All four units begin in the right-side grey staging area.
- Four authored left approaches converge through the path graph on the central
  point.

The shared Webcam detector retains its existing DICT_4X4_50 and Atom-screen
detection. An additive DICT_4X4_100 pass is filtered to IDs 50–55 so the full
fixed range can be observed without changing the existing camera feed.

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
enabled, the Gamemaster selects Atom unit 100–103, picks up a Machine Gun,
Flamethrower, or Mortar with the mouse, and drops it on a compatible socket.
Virtual placement uses the same ownership, occupancy, loadout, and activation
rules as physical play; it does not claim camera evidence and never weakens
robot safety.

Active placements are linked in activation order. Two or more endpoints form a
damaging slowing force field, and four endpoints close the ring. Every force
field endpoint is an active placed tower.

## Run behavior

The Gamemaster can Start, Pause, Resume, and Reset. Start immediately launches
wave 1. Orcs spawn from the authored left-side lanes, advance along the path
graph, and continuously attack the central point after reaching it. Machine
Guns provide fast single-target damage, Flamethrowers provide short-range area
damage, and Mortars provide long-range splash damage.

The server is authoritative and pushes snapshots with Server-Sent Events. The
browser renders those snapshots. There are never more than 1,000 living active
enemies; excess scheduled pressure is bounded.

## Settings

The Gamemaster-only Tower Defense settings page controls:

- number of waves and interval between wave launches;
- orc release rate and count per wave;
- active-orc cap, never above 1,000;
- orc health, movement speed, and central-point attack damage;
- central-point integrity; and
- force-field damage and slow factor.

All fields use finite bounded validation with field-level errors. Balanced,
Training, and Onslaught presets plus Reset to defaults are available. Start
copies the validated draft into an immutable run snapshot, so later edits affect
only the next run.
