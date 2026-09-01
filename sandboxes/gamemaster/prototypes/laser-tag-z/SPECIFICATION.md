# Laser Tag Z — Tower Defense integration specification

For the chronology, alternatives, reversals, and human/AI attribution behind
these requirements, consult the [development journal](../../../../DEVELOPMENT_JOURNAL.md).
This specification remains authoritative for current behavior; the journal
records reasoning and historical context.

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

In physical mode, a field-only canvas remains aligned over the corrected camera
feed. It renders the same authoritative setup previews and active force fields
without duplicating virtual towers, enemies, the registered map, or controls.
Force-field visuals never cover an ArUco code. The shared renderer subtracts a
20-pixel-clearance rectangle around the rendered bounds of fixed markers 38 and
40–55, splitting a straight field into visible fragments without changing its
authoritative endpoints. This rendering guard is the final visual safeguard;
authoritative topology separately rejects any field crossing the protected
marker 38 footprint and blocks or occludes fields at empty socket footprints.
The physical overlay additionally
uses every currently detected marker's corrected corner bounds, scaled into the
1696×960 playfield, so movable Atom tags and small calibration differences are
also protected. Force-field impact art is suppressed whenever it would overlap
one of those keep-outs. The virtual feed and external screen use the same fixed-
marker masking rule.

## Map and marker contract

The production map is `assets/tiled/levels/z-pixel-first-map.tmj`. It remains a
modular, editable TMJ/TSJ project and must pass the bundled validator plus a
native Tiled round-trip and render.

- Sixteen fixed tower sockets use ArUco IDs 40 through 55 exactly once.
- The central objective uses ArUco ID 38 and is the endpoint for the completed
  ring sequence.
- Every authored socket starts with a 208×208 visual footprint. Its center panel
  renders the dictionary-correct ArUco code instead of a generic grey plate and
  text label.
- Connectivity uses a separate immutable `aruco_code_footprint_px` map property
  of 77 pixels. This identification footprint never follows the editable visual
  or interaction size of a socket.
- The central marker uses the immutable `core_aruco_code_footprint_px` property
  of 116 pixels plus `force_field_marker_clearance_px` of 20 pixels on every
  side. That 156-pixel protected square is a permanent hard blocker for force-
  field topology, independent of placement order, ring state, or camera state.
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

Missing, stale, uncalibrated, wrong-owner, or ambiguous evidence fails closed.
Once the stability window activates an unoccupied socket, that defence remains
active when the physical Atom moves away. Moving the same Atom onto another
socket may activate another defence of the same type, up to the 16-socket map
capacity. Moving a 100-series Atom back onto an occupied, active defence
replenishes that unit and its connected force fields.

## Virtual play

The game page contains a checkbox whose exact label is **Virtual play**. When
enabled, the Gamemaster selects an Atom tag in the Camera markers panel and
then selects any number of fixed camera markers on the map. Each socket owns a
placement, so the same Atom may seed multiple simultaneous defences. Atom 100
always activates a Machine Gun, 101 a Flamethrower, 102 a Mortar, and 103 a
Tesla Coil. Placing any 100-series Atom on an active, occupied defence restores
the existing unit to full health without changing its type and resets the
durability of every force field connected to that unit. A destroyed defence and
its compact opaque placement pod disappear, revealing the original ArUco marker. The
live pod is rendered at 112 pixels and its turret art at 88 pixels, while the larger
208-pixel authored socket remains the interaction and placement area. Placing
any Atom on that destroyed socket replaces the old defence with the activating
Atom's fixed weapon type, so replacement may change the tower type while
preserving and replenishing the socket's established force-field topology. Every Atom may use
any unoccupied turret socket regardless of the socket's authored team colour.
The Atom's owner still determines its visual team colour and the physical arm
that may carry it. Virtual placement uses the same occupancy and activation
rules as physical play; it does not claim camera evidence and never weakens
robot safety.

## Linked turret scaling

Every living turret belongs to one connected component of established force
fields. Only an intact, unobstructed field with two living endpoints joins two
turrets; provisional ring previews, broken fields, occluded fields, and fields
with a destroyed endpoint do not. An isolated turret is a one-turret component.
Each turret in a component receives the power-and-health multiplier
`start + step × (turret count - 1)`. The Gamemaster settings page validates and
saves both `tower_link_start_multiplier` and `tower_link_step`, and Start freezes
them into the run's immutable settings snapshot. Their defaults are `0.9` and
`0.1`, so one turret is `×0.9`, two are `×1.0`, three are `×1.1`, and the
16-turret map maximum is `×2.4`. The same page continues to control base defence
health and every weapon's base damage independently of this multiplier.

The multiplier applies to Machine Gun shot damage, Flamethrower direct and burn
damage, Mortar shell damage at launch, Tesla Coil base damage before chain
falloff, and the turret's maximum health. Force-field contact damage remains a
separate setting and is not multiplied. When a link joins or splits a component,
current health keeps the same percentage while maximum health changes; linking
therefore grants durability without repairing existing damage. Replenishing a
turret still restores that turret to its newly calculated full health. Every
public turret snapshot exposes `linked_turret_count` and `link_multiplier`. The
shared renderer displays the multiplier over every living turret, and the
Gamemaster's selected-turret panel shows the group size and multiplier together.

Force fields are absent from play during setup: the playable `gates` list stays
empty and fields cannot affect orcs. Established connections are nevertheless
exposed in `force_field_visuals` as dim preview segments, so the shared renderer
and the physical field-only overlay show the authoritative partially closed
chain before Start. Each newly occupied socket requests exactly one ordinary
link to the previously activated living defence. Requests are retained and an
idempotent reconciliation pass runs after placement, Start, replenishment,
replacement, destruction, and level reload. A live, clear endpoint pair is
therefore created exactly once; unavailable endpoints remain pending and resume
after repair without changing any established field or its durability.

Force-field line of sight combines immutable authored geometry with current
socket occupancy. A segment is blocked when it intersects either a Tiled
rectangle or polygon whose type/class is `ForceFieldBlocker`, or the fixed
77-pixel identification footprint of any empty or destroyed ArUco socket other
than its two endpoints. Marker 38 is always blocked using its 116-pixel code
footprint plus the map-authored 20-pixel clearance, even though it is not a
turret socket. Living occupied sockets do not block a segment. The
editable 96–320 pixel visual/interaction size, team colour, and activation
source never affect this calculation. Diagnostics report stable authored
`blocker_id` values plus `empty_socket:<socket_id>` or
`protected_marker:38`, `blocker_socket_ids`, and `blocker_markers`. A new
request crossing the core remains `pending_core_marker`; a request blocked by
an empty position remains
`pending_empty_socket` and is deterministically retried when reconciliation
runs. If an established field later acquires an empty-socket obstruction, it
becomes `occluded`: it is invisible, non-collidable, and excluded from routing
until the position is occupied again, while retaining its identity and
durability. Any legacy or stale established field found to cross marker 38 is
retired instead of merely hidden; if it was a ring boundary, ring completion
and immunity are invalidated before topology is reevaluated. Activation order
controls ordinary links, but never controls which
living turrets qualify as the objective ring.

The objective preview is separate from ordinary combat topology. With two
through seven living turrets, the engine sorts them by angle around the core,
opens the chain at the largest angular gap, and uses canonical marker IDs to
break exact ties. With eight or more turrets it uses the selected spatial ring,
or the highest-ranked rejected candidate when none is closable, and applies the
same opening rule. Clear missing preview edges are dashed, visible, and
explicitly `provisional`; they are never collidable or durability-bearing. A
preview edge obstructed by an empty ArUco position is reported with blocker
diagnostics but is not drawn. Thus the same set of living turrets and socket
occupancy shows the same partial ring for every activation order.

Links are not rejected for blocking every authored road route. A broken field
stays broken until a connected unit is replenished. Destroying an endpoint
suspends the same field, and repairing that endpoint resumes and replenishes it.
On contact, an
orc reverses along its recorded current road edge to the preceding junction,
then uses the route whose crossed field has the lowest remaining durability; an
unblocked route is preferred when one exists. Each field counts one durability
impact per unique orc and breaks after its configured impact capacity. Every
contact records an independent `force_field_impacts` entry with the projected
point on that exact field segment and the impacted orc identity and position.
The renderer places the field flash at that contact point and overlays a cyan
glowing skeleton on the same moving orc; both fade linearly over 0.5 seconds,
and simultaneous contacts render independently.

A ring forms when 8 through 16 living turrets make a non-self-intersecting
polygon containing ArUco 38. One spatial solver examines the current set of
living turrets independently of Atom activation order and ordinary-link
history. It evaluates subsets from 16 down to 8 turrets, orders each subset by
its angle around the core, and rejects any boundary that self-intersects,
excludes the core, or crosses an authored blocker or empty ArUco identification
footprint. This preflight applies to every boundary edge, including an already
established field that has become occluded. The
authored `ring_neighbors` cycles remain editor validation and diagnostic hints;
they are not a competing gameplay detector.

The solver first maximizes the number of boundary turrets. Ties minimize the
longest boundary edge, then total perimeter, then use stable marker ordering.
This ranking depends only on current spatial state, so the same living turret
set produces the same selected ring for every activation permutation. Active
turrets outside the selected cycle remain valid. The engine preflights every
missing boundary and creates all of them in a rollback-protected transaction,
or none when any boundary fails. Matching ordinary fields are reused without
changing their endpoints. The boundary pair spanning the largest angular gap
is reported as the deterministic closing edge, matching the partial preview.
Seven or fewer turrets cannot complete the objective and a
loop larger than sixteen is invalid. Completion replenishes every selected
boundary field, turns the core white, and makes the ring immune to durability
damage for 100 simulation seconds by default. A completed ring and all
established endpoints remain fixed if more turrets are activated.

Every snapshot exposes `connection_contract_version: 2` and one authoritative
`connections` array keyed by canonical
socket-pair field IDs. Each record carries existence, placement-attempt,
endpoint, durability, phase, visibility, collision, blocker, and ring-boundary
state plus fixed endpoint coordinates, roles, `provisional` and `occluded`
flags, and empty-socket blocker IDs and markers.
`collidable` always implies `visible`; a visible connection without an
authoritative field is valid only when it is a live, non-collidable provisional
ring preview. Setup connections are visible but never collidable. The legacy
`gates`, `force_field_visuals`, `force_field_topology`, and `placement_links`
projections are derived from that array in the same locked snapshot instead of
recomputing their own lifecycle rules. The shared renderer consumes
`connections` directly and uses the older fields only as compatibility
fallbacks. Runtime validation fails loudly if canonical IDs duplicate, public
existence diverges from authoritative fields, an established attempt lacks its
field, a collidable field is invisible, or a completed ring lacks a marked
boundary edge.

Snapshots also expose all ring candidates, including active, broken, suspended,
blocked, and missing states plus blocker and missing-marker details. Ring diagnostics
also identify the selected spatial candidate, excluded active markers, boundary
scores and per-edge states, evaluated candidate count, next-best alternatives,
and rejection reasons. Those diagnostics remain available after the playable
gate list is hidden at the end of a run.
A first Atom 100–103 placed on ArUco 38 turns the core green. Stably stacking an
Atom owned by the opposing team on the first tag activates the core force
field, starts a red radial detonation, sets every on-screen orc on fire, stops
further spawning, and completes the run after the purge reaches the whole
playfield. Virtual play exposes the same two-stage ArUco 38 interaction by
clicking its camera-marker chip or the rendered core.

## Run behavior

The Gamemaster can Start, Pause, Resume, and Reset. Start immediately launches
wave 1. Orcs spawn from the authored left-side lanes, advance along the path
graph, and continuously attack the central point after reaching it. Machine
Guns provide fast single-target damage with two visible bullet streams, one
from each sprite barrel. Both streams use the authoritative target orc ID and
converge on that orc's current rendered position; the second stream is visual
only and never doubles server-side damage.
Flamethrowers sweep a narrow, gasoline-like jet back and forth across their target
area. Its eighteen segments follow the moving gun with up to 0.48 seconds of
progressive delay, bending like a flexible rope while applying a three-second
burn. The flame path, damage path, and weapon head are rebuilt from the current
aim in the same update, so a direction change cannot leave a stale jet firing in
the opposite direction. The normalized gasoline sprite is alpha-centered on
the spline, and the pilot flame, visible plume, and authoritative damage path
share the same 35-pixel nozzle origin at the rendered turret tip. Mortars launch visible shells through a
forced-perspective arc before blanketing their aimed target circle with
explosions. Tesla Coils strike one orc and progressively chain through nearby
orcs with diminishing damage and visual intensity. Their overhead runtime head
uses a polished terminal sphere inside concentric chrome toroids with exposed
copper windings, matching the physical Tesla reference without baking lightning
into the sprite. Between discharges the authoritative `weapon_charge` rises from
zero to one over the one-second firing interval. The renderer extrapolates that
charge between snapshots and animates increasingly numerous cyan-violet arcs,
a fluctuating terminal glow, and a bright full-charge corona; the idle effect is
briefly suppressed during the discharge flash. The selected Tesla reach runs
from 120 to 265 pixels: reducing reach continuously increases first-link damage
from 1.00× up to 1.75× and raises the electrical visual intensity, while maximum
reach is deliberately lighter. Each
defence unit starts with health equal to 15 percent of the run's central-point
integrity by default. Orc melee contact uses the authoritative 112-pixel pod,
an explicit 38-pixel reach, each orc's collision radius, and swept movement for
the exact fraction of each tick spent in range. Orcs close enough to a unit
damage it until it becomes inactive and exposes its replacement marker. Small
hits retain exact health while producing a short damage flash and a visible
minimum-width loss notch. A living tower below 50 percent
health develops progressively stronger stress cracks. Below 30 percent it also
emits large animated smoke plumes that rise and drift right with the wind. Below
10 percent it adds animated fire whose size, glow, layering, and ember count
increase toward zero health. At zero health all tower types share one bright
four-frame blast. Eight forced-perspective components arc outward, cast ground
shadows, land around (not over) the marker, remain for three seconds, then fade
over 0.5 seconds. The persistent damage effects and live pod clear so the ArUco
code remains readable for replacement.

The Gamemaster may select any active gun and adjust its direction and reach
independently, including when several guns were seeded by the same Atom.
Machine Guns and Flamethrowers interpolate between narrow/long and wide/short
targeting cones. Machine-gun heads face the tracked orc, Flamethrower heads and
damage follow the same synchronized sweep angle, and Mortar tubes face their
aimed target circle. Tesla Coils are omnidirectional, but expose a Reach / power
control using the inverse range-and-strength curve described above. Mortar target circles grow
and lose damage intensity as they move farther away. Fine line overlays show all automatic
targeting areas, with the selected unit highlighted on the Gamemaster view and
mirrored on the external display.

The server is authoritative and pushes snapshots with Server-Sent Events. The
browser renders those snapshots. Dense waves use compact, density-throttled
state frames, short visual motion extrapolation, cached directional enemy sprites, and an
adaptive 18 fps renderer at the 1,000-orc limit. The authoritative simulation
continues at 20 Hz. There are never more than 1,000 living active enemies;
excess scheduled pressure is bounded.

## Settings

The Gamemaster-only Tower Defense settings page controls:

- number of waves and interval between wave launches;
- orc release rate and count per wave;
- active-orc cap, never above 1,000;
- orc health, movement speed, central-point attack damage, and independently
  scaled defence-unit melee damage;
- central-point integrity;
- defence-unit health as a percentage of central-point integrity;
- Machine Gun, Flamethrower direct/burn, and Mortar near/far damage;
- Tesla first-link damage, maximum gap between chained orcs, and chain depth; and
- force-field impact capacity, impact damage, turnaround factor, and completed-
  ring immunity duration.

All fields use finite bounded validation with field-level errors. Balanced,
Training, and Onslaught presets plus Reset to defaults are available. Start
copies the validated draft into an immutable run snapshot, so later edits affect
only the next run.
