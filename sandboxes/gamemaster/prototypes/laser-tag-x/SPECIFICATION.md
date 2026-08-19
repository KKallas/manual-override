# Laser Tag X tab — reconstruction specification

Status: specification of the currently implemented behavior, including operator UI, game rules, sensor fusion, backend routes, recovery behavior, and diagnostic integration.

This document is intended to be sufficient to rebuild the Laser Tag X tab without consulting its original implementation. Where the product wording and the actual acceptance rule differ, this document gives the actual rule. “Must” describes behavior required for compatibility; “should” describes behavior that is desirable but not required by an existing integration.

## 1. Product definition

Laser Tag X is a gamemaster-operated, two-arm cooperative placement game. Four physical ArUco/Atom tags are divided between a green team and a blue team, two tags per team. The green team is associated with the green MG400 arm. The blue team is associated with the relay’s purple MG400 arm, but is called blue everywhere in the Laser Tag X UI.

During the main stage, either team may repeatedly move either of its assigned physical tags onto any unactivated outer target. Eight outer targets, markers 30–37, form two mirrored sides of a ring around center marker 38. A confirmed outer placement:

1. turns that outer target red;
2. links the center target to it;
3. links it to its mirrored counterpart if that counterpart is already active; and
4. returns that physical tag to the beginning of its detection sequence so it can be moved again.

The outer stage is complete only when all eight outer targets have been activated. Merely creating one or more visually crossing links is not a win condition.

After all eight outer targets are active, the playfield is reduced to center marker 38. The center is covered twice:

1. any assigned physical tag covers marker 38; then
2. a different assigned physical tag covers the first physical tag while both remain over marker 38.

The second valid center cover ends the game.

## 2. Route, manifest, and access context

The prototype manifest must expose:

- name: `Laser Tag X`
- description: `Two-player cooperative placement puzzle with crossing playfield beams.`
- default page: `game`
- one page at `game`, labeled `Laser Tag X`

The page is served at both the prototype root and `/game`. Its HTML response must disable caching with `Cache-Control: no-store, max-age=0`.

All relative service URLs are derived from the current sandbox prefix. Given a page path such as `/s/gamemaster/p/laser-tag-x/game`, the shared root is `/s/gamemaster`, and dependencies live beneath that root:

- Laser Tag X: `/p/laser-tag-x`
- Playfield Areas: `/p/playfield-areas`
- Webcam: `/p/webcam`
- Camera Calibration: `/p/camera-calibration`
- Auto Pickup Game: `/p/auto-pickup-game`
- Atom Manager: `/p/atom-manager`
- Dobot MG400 Relay: `/p/dobot-mg400-relay`

The Laser Tag X page is a gamemaster page. Mutating Laser Tag X backend routes require a request role of `gamemaster`, except that green-side diagnostic intention events may be accepted from a request with the `green` role.

## 3. System boundaries

The Laser Tag X tab owns:

- the operator page and its local, high-frequency detection state;
- a small server-side game state shared over server-sent events;
- an in-memory current diagnostic run;
- durable JSONL diagnostic files;
- an adapter that maps Laser Tag X’s blue team to the relay’s purple arm; and
- an atomic “replace the entire playfield” operation.

It depends on other prototypes for:

- corrected camera video and corrected ArUco coordinates;
- playfield areas, links, view settings, and external rendering;
- MG400 arm poses and pump modes;
- physical Atom tag impact counters;
- Auto PP Cal 2 arm/camera calibration; and
- optional Green and Purple LTX intention telemetry.

The browser, not the Laser Tag X server, runs the game’s sensor-fusion state machine. The server persists only coarse game phase/finale state and diagnostic events. Consequently, a browser reload reconstructs what it can from the server and live playfield, rather than resuming every per-tag detection step exactly.

The backend must serialize access to coarse state, current-run metadata, event sequence numbers, in-memory event retention, and JSONL appends with one mutual-exclusion boundary. State snapshots must copy both team ID arrays so callers cannot mutate server state through a returned object.

## 4. Terminology and identifiers

### 4.1 Teams and arms

| Laser Tag X name | Relay side | Default physical tags | Presentation color |
|---|---|---:|---|
| green | green | 100, 101 | `#35d07f` |
| blue | purple | 102, 103 | `#3b82f6` |

The physical tag assignment may be changed during setup. Exactly four unique IDs must be selected from 100 through 105 inclusive.

### 4.2 Playfield targets

The mirrored index is the array position in each side below. Targets with the same index are linked when both are active.

| Index | Green-side marker and position `(x,z)` | Blue-side marker and position `(x,z)` |
|---:|---|---|
| 0 | 30 at `(-4,-2)` | 34 at `(4,2)` |
| 1 | 31 at `(-5,-0.7)` | 35 at `(5,0.7)` |
| 2 | 32 at `(-5,0.7)` | 36 at `(5,-0.7)` |
| 3 | 33 at `(-4,2)` | 37 at `(4,-2)` |

Center marker 38 is at `(0,0)`.

“Owner team” means the team to which a physical tag was assigned. “Target side” means whether an outer marker is in 30–33 or 34–37. They are independent: a green-owned tag may activate a blue-side target and vice versa.

## 5. Operator interface

### 5.1 Overall page structure

The page uses a dark, nearly black control-room aesthetic and contains, in order:

1. a header;
2. a large corrected overhead camera stage;
3. a horizontal operator control bar;
4. a gamemaster desk containing team assignments, calibration state, sequence editor, progress, diagnostics, and event log.

The content width of the header, control bar, and desk is `min(viewport width - 24 px, 1500 px)`, centered with 12 px vertical margins.

Core visual tokens and dimensions:

| Element | Compatibility styling |
|---|---|
| page | background `#05070a`, foreground `#f5f8ff`, zero body margin, system sans-serif |
| muted copy | `#9aa6b6` or `#c4cedc`, normally 12–13 px |
| generic panel | background `#101620`, 1 px `#293647` border, 10 px radius, 14 px padding |
| button/link | minimum height 44 px, 8×14 px padding, background `#19212d`, 1 px `#354153` border, 8 px radius, white bold text |
| primary Start | background `#d9243d`, border `#ff5367` |
| disabled control | 40% opacity and noninteractive cursor |
| heading | 21 px, uppercase, 0.1 em letter spacing |
| team panel | background `#0a1018`, 1 px `#304054` border, 9 px radius, 14 px padding, 4 px team-colored top border |
| phase/timer HUD | translucent `#05080dcc`, subtle white border, 9 px radius, 9×14 px padding, blurred backdrop |
| timer | 50 px bold tabular numerals, line-height 1 |
| mission banner | 18 px bold centered text, background `#05080ddd`, 13 px padding, 9 px radius |
| sequence node | fixed 210 px basis, minimum height 92 px, background `#111b28`, 1 px `#40516a` border, 10 px radius, 11 px padding |
| Start/End node | 92 px basis; Start uses `#172334`, End uses green-toned `#10261c` |
| active sequence node | gold border `#ffd35a`, dark-gold background `#2a2410`, glow and 1.15 s pulse |
| local log | background `#06090e`, 1 px `#293647` border, 8 px radius, 12 px/1.5 monospace |

Team colors are green `#35d07f`, blue `#3b82f6`, activation red `#ff334d`, and gold `#ffd35a`.

### 5.2 Header

The left side contains:

- heading `Laser Tag X`
- subtitle `Cooperative crossing-beam puzzle`

The right side contains a button-style link labeled `Open external playfield ↗`. It opens the Playfield Areas `/screen` route in a new tab.

### 5.3 Camera stage

The stage has a black background, a minimum height of 420 px, and a normal height of the lesser of 68 viewport-height units and 720 px. The corrected MJPEG stream is contained without cropping.

An SVG overlay shares the camera coordinate system. For every frame-current tag that has at least four finite corner coordinates and is not explicitly `tracked: false`, the overlay draws:

- an unfilled bright-green polygon around the corners; and
- a bright-green, dark-outlined monospace label `#<id>` above the polygon’s upper-left extent.

The overlay’s view box is the camera response’s pixel width and height. Its aspect-ratio behavior must match the contained video (`xMidYMid meet`) so boxes remain aligned with the displayed image. Clear the overlay whenever tracking fails or flash-latency mode is active.

The camera stage also contains:

- a top-left phase badge: `Setup`, `Live`, or `Complete`;
- a top-right count-up timer shown to one decimal place; and
- a full-width mission banner near the bottom.

Mission text is:

| Condition | Text |
|---|---|
| setup | `Choose four different physical tags and start the game.` |
| running, outer stage | `Activate all eight outer targets. Any assigned physical tag may move to either side.` |
| running, center ready | `Outer ring complete. Marker 38 is white at 25% brightness; cover it with any assigned tag.` |
| running, first center cover complete | `Marker 38 is green at 25%. Cover tag #<first tag> with any different assigned tag.` |
| won | `Marker 38 is red at 50% illumination and maximum glow.` |

### 5.4 Operator control bar

The controls, left to right, are:

1. `Load Laser Tag X playfield`
2. primary red button `Start Laser Tag X`
3. `Reset game`
4. the player-game selector
5. a `Manual tag placement` selector and `Activate` button
6. a flexible status message

Button behavior:

- Start is enabled only in setup and while a start request is not already running.
- Physical-tag inputs and all detection-sequence editing controls are enabled only in setup.
- Playfield loading disables only its own button while in progress.
- Reset remains available in all phases.
- During the running outer-ring stage, Manual tag placement lists the unactivated outer markers 30–37. Pressing `Activate` applies the same red target, center link, mirrored-pair link, progress, and outer-ring completion effects as an automatically confirmed placement. The activated marker is removed from the selector immediately.
- Once the outer ring is complete, the selector offers `Target #38 — confirm first cover`. Activating it advances to the durable manual first-cover state and changes the option to `Target #38 — final cover / finish game`. Activating #38 again applies the normal final visual, ends the game, stops the timer, and records the paired winning score.

### 5.5 Team assignment panels

Two equal-width panels are shown side-by-side on wide screens:

- `Green team · left side`, with a green top border;
- `Blue team · right side`, with a blue top border.

Each contains numeric inputs `Physical tag A` and `Physical tag B`, with HTML minimum 100 and maximum 105. Defaults are 100/101 for green and 102/103 for blue.

During setup, each panel reports either:

- `VISIBLE — tags #A and #B detected`, in green, if both currently appear in the frame-current set; or
- `Not visible: #<missing ids> · start is still allowed`.

During play, a panel reports `<n>/4 <side>-side targets activated`. This count is based on target side, not physical-tag ownership.

Camera visibility is advisory at start. Missing selected tags must not block the Start button.

### 5.6 Calibration status

Below the team panels, show one line describing Auto PP Cal 2:

- success: `Auto PP Cal 2 TPS loaded for both teams and used with corrected camera coordinates.`
- incomplete: `Auto PP Cal 2 TPS incomplete for: <comma-separated Laser Tag X teams>.`
- request or format failure: `Auto PP Cal 2 could not be loaded: <reason>`

### 5.7 Detection-sequence editor

The editor is titled `Expected detection sequence`, with helper text `Build the shortest Start → End path needed for every tag.`

It displays two read-only-equivalent lanes backed by the same sequence definition:

- `Green arm`
- `Blue arm`

Each lane renders:

`Start → event node → event node ... → End`

Every event node shows its label, sensing device, explanation, dynamic tag/status pills, and setup-only controls to move it left, move it right, or remove it. The End node shows pills for physical tags whose sequence index has reached the sequence length. The active node is gold and pulsing unless reduced motion is requested. Completed nodes are green-toned.

The `target_hidden` node has a target-consideration trail. A team lane displays only the physical tag with the most recent consideration activity, including that tag's considered, selected, and discarded entries. When a target activation is confirmed, clear that physical tag's trail immediately; if the other tag has an in-progress trail, it then becomes the displayed trail.

Below both lanes, a select lists events not already in the sequence and an `Add node` button appends the chosen event. Duplicate event types are forbidden.

The valid event catalog is:

| Stored value | Label | Device | Meaning |
|---|---|---|---|
| `pickup` | Tag picked up | Overhead camera | The assigned physical tag was seen, becomes hidden under a suitably close arm, and suction confirms the pickup. |
| `suction_start` | Suction start | MG400 robot pump | Pump mode becomes `suck`. Also opens the target-coverage observation window. |
| `pickup_impact` | Pickup impact | Atom tag accelerometer | The assigned Atom’s impact counter increases; fires a team-colored flash. |
| `target_hidden` | Target covered by arm | Overhead camera | A previously visible target disappears after the coverage window opened while the arm is spatially compatible. This is provisional occlusion evidence; sustained final camera overlap chooses the outer target. |
| `suction_off` | Suction off | MG400 robot pump | Optional diagnostic step: after confirmed coverage, a prior suction state transitions to `off` or `blow`. |
| `blow` | Blow | MG400 robot pump | The pump reaches `blow`. This is the default release milestone. |
| `drop` | Drop impact | Atom tag accelerometer | The Atom impact counter increases; records the arm pose and fires a flash. |
| `verify` | Confirm drop | Overhead camera | The physical tag is uniquely and sustainably confirmed over a target after parallax correction. The release pose is retained only as a diagnostic. |

The default sequence is:

`pickup → suction_start → pickup_impact → target_hidden → blow → drop → verify`

`suction_off` is deliberately excluded from the default because turning suction off can leave an Atom attached; `blow` is treated as the reliable default release signal.

Sequence persistence and migration:

- Store the sequence in browser local storage under `laser-tag-x-detection-sequence-v1`.
- A stored sequence is accepted only if it is a nonempty array, contains no duplicates, and every value is in the event catalog.
- Two historical exact defaults must be replaced by the current default: `pickup,pickup_impact,drop,verify,suction_start,suction_off,blow,target_hidden` and the complete catalog order including `suction_off`.
- On page load, if `verify` is not last, move it to the end and persist the normalized array.
- The editor itself permits arbitrary ordering while the page remains open. Compatibility validation checks only nonempty/unique/known values; it does not enforce semantic prerequisites.
- If the in-memory sequence is invalid when Start is pressed, silently replace it with and persist the current default, while logging that the invalid saved sequence was ignored.

For a practically completable game, `verify` must be present. The existing validation does not enforce that requirement, but outer targets and center covers can only be finalized through verification.

### 5.8 Scoring table

Place the scoring table immediately below the complete Expected detection sequence section and above the progress summary. Each completed game produces one cooperative row for `Green + Purple`; it does not create separate team scores. Rows show rank, both player sides followed by their registered player names, the played game type (`Game 1`, `Game 2`, `Game 3`, or `Game 4`), the shared winning time to one decimal place, the time gap to the immediately faster score, and the local completion date/time. The leading row says `Fastest`; every other row says how many seconds faster the preceding row is and identifies that row by rank and player label. Do not show the assigned 100-series physical tag IDs in the player labels. Rank all currently visible rows by ascending elapsed time and then by recording time. Historical rows recorded before game types were added show an em dash in that column.

When a game finishes, highlight its row as the latest finish and report its ordinal scoreboard place. The LTX player finish HUD also shows that place. For any place below first, both views show how many seconds faster the immediately preceding score is; first place is labeled fastest.

The score header contains:

- a `Download score log` link; and
- a `Reset scoring table` button.

The reset clears only the table view by persisting a cutoff timestamp in `score-table-state.json`. It must never truncate or rewrite `score-log.txt`. The text log is append-only, uses one JSON object per winning line, and preserves every win across table resets and server restarts. A win record includes the registered Green/Purple player names and shared player label, the selected `game_mode` and human-readable `game_type`, elapsed/start/finish timestamps, both sides' assigned tag IDs, and the diagnostic run ID when one exists. Snapshot both registered names and the selected game mode when the game starts so later player or mode changes cannot relabel a completed score. If either start-time name is empty because that controller registration was still completing or retrying, fill only that missing side from the current registered-name snapshot when the win is recorded; never replace a nonempty start-time name. Record a win exactly once when the server phase first transitions into `won`.

### 5.9 Progress summary

Four summary cards show:

1. `1 · Pick up`: number of the four per-tag flows whose sequence index is past the pickup node, or `Pickup is not in this sequence.`
2. `2 · Drop`: number past the drop node, or `Drop impact is not in this sequence.`
3. `3 · Verify`: `<n>/8 outer targets confirmed.`
4. `4 · Cross`: `<n>/8 outer targets activated.`, changing after transition to `Outer ring complete · centre 38 remains.`

The sequence section also lists sorted activated IDs as `Activated outer targets: #... (n/8)`, or `none (0/8)`.

### 5.10 Diagnostics and local event log

Two diagnostics panels are titled `Green LTX ↔ Laser Tag X` and `Purple LTX ↔ Laser Tag X`. Each contains:

- a state pill;
- an optional `Download JSONL` link;
- the latest eight LTX intention events for that player side; and
- the latest eight Laser Tag X events applicable to its mapped game team, excluding plain log events.

Each displayed event line contains local time, event name, optional physical tag, optional target arrow, and the first eight characters of an operation ID.

The state pill is:

- `No active run` when no current run exists;
- `Paths aligned` for a current active run with no detected divergence;
- `Run ended · paths aligned` for an ended current run; or
- `Board truth confirmed · operation unattributed` when a physical result was accepted but no unique released LTX operation could be associated; or
- `DIVERGED · <reason>` in red.

Once that side has emitted `script_started`, new terminal board decisions carry an `operation_attribution` status. Only `true_sequence_divergence`, produced after a stable board result contradicts one uniquely correlated released operation, turns the state pill red. `matched_sequence`, `matched_sequence_late`, and `board_truth_unattributed` do not. Legacy events without attribution retain the earlier matching-release diagnostic check.

The bottom log is a 150 px high, scrollable, monospace, live region. Lines are prefixed with the browser’s local time. If a diagnostic run is active, every log message is also submitted as a diagnostic `log` event.

### 5.11 Responsive and accessibility behavior

At widths at or below 850 px:

- team panels become a single column;
- the four progress cards become a single column;
- the header and operator controls stack vertically;
- the camera stage height becomes 58 viewport-height units; and
- add-event controls stack, with a full-width select.

Interactive controls must use native buttons, links, selects, and inputs. The camera image has alt text `Overhead tag camera`. The tracking SVG is hidden from assistive technology. Each sequence graph has an arm-specific accessible label. Disable the active-node animation for `prefers-reduced-motion: reduce`.

## 6. Playfield installation and rendering

### 6.1 Starting target areas

Loading the Laser Tag X playfield atomically replaces the complete shared Playfield Areas store with 13 areas: nine game targets and four staging extras.

Every outer game target has:

- ID `laser-<team>-<marker>`
- name `Laser Tag X - <team> <marker>`
- `y: 0.03`
- `size: 2`
- team color
- `glow: 1.25`
- its specified marker ID
- empty links
- area, ArUco, and links all visible

The center has:

- ID `laser-centre`
- name `Laser Tag X - centre`
- position `(x,y,z) = (0,0.03,0)`
- `size: 2`
- color `#ffd35a`
- `glow: 1.25`
- marker 38
- empty links
- area, ArUco, and links all visible

The four staging extras are exact compatibility data:

| ID | Name | `(x,y,z)` | Size | Color | Glow | Marker | ArUco visible |
|---|---|---|---:|---|---:|---:|---|
| `a5101` | Area 5101 | `(2.3,0,4.5)` | 1 | `#35d07f` | 1 | 0 | no |
| `a5102` | Area 5102 | `(1.2,0,4.5)` | 1 | `#35d07f` | 1 | 1 | no |
| `a5103` | Area 5103 | `(-1.2,0,-4.5)` | 1 | `#4f9dff` | 1 | 2 | no |
| `a5104` | Area 5104 | `(-2.3,0,-4.5)` | 1 | `#4f9dff` | 1 | 3 | no |

All extras show their area and outgoing links and begin with an empty links array.

### 6.2 Starting view settings

Install these Playfield Areas settings in the same logical operation:

```json
{
  "bloom": 0.3,
  "dof": 0,
  "global_brightness": 1,
  "atom_brightness": 1,
  "fov": 12,
  "cam": {"x": 0, "y": 18, "z": 0},
  "rot": {"x": -90, "y": 0, "z": 0}
}
```

The install endpoint must call the Playfield Areas in-process replacement API, apply settings, and immediately persist areas when those capabilities exist. This avoids exposing an intermediate half-deleted board to LTX, which may be polling the same store.

The client must reject a nominally successful install response unless it contains all 13 installed areas.

### 6.3 Meaning of “Load Laser Tag X playfield”

Loading is also a game reset:

1. end the current diagnostic run with reason `playfield_reload`;
2. locally return to setup while preserving current tag assignments;
3. clear local detection progress and latency mode;
4. request a server reset;
5. replace the full playfield and settings; and
6. reload calibration because the target coordinate cache was cleared.

This action intentionally replaces any unrelated areas in the shared playfield.

### 6.4 Outer-target activation visuals and links

When an outer placement is confirmed:

- patch that target to color `#ff334d` and glow `3`;
- add a directed link from center 38 to the activated target if absent;
- record it as activated exactly once;
- add a directed link from each active green-side target to the active blue-side target with the same mirrored index if absent.

Thus center has up to eight outgoing links, and there are up to four green-to-blue mirrored links.

### 6.5 Impact flash

An accepted pickup or drop impact triggers a short board flash for that physical tag’s owner team, provided the game is running and no flash is already active:

1. snapshot all areas whose names begin `Laser Tag X -`;
2. concurrently recolor all of them to the team color and set bloom to 2.5;
3. after 180 ms, restore every snapped area color and set bloom to 0.3;
4. refuse another flash until 240 ms after the original trigger.

Flash errors must not stop the game loop. A flash uses a snapshot, so an implementation should preserve the compatibility behavior that restoration is based on colors captured at flash start.

### 6.6 Center-only transition

When all eight outer markers are active:

1. locate the Laser Tag X area for marker 38 or fail and retry later;
2. delete every playfield area except that center area, including the four staging extras and any unrelated area that may have appeared;
3. fail the transition if any deletion fails;
4. patch center to white, glow 4, area visible, ArUco visible;
5. set bloom to 2.5 and both global and Atom brightness to 0.25;
6. persist server `final_stage: centre_ready` and the center-cover mission;
7. mark all four tag flows armed for center pickup.

Center visual stages are:

| Stage | Center color | Glow | Bloom | Global/Atom brightness |
|---|---|---:|---:|---:|
| ready | `#ffffff` | 4 | 2.5 | 0.25 |
| first covered | green `#35d07f` | 4 | 2.5 | 0.25 |
| complete | red `#ff334d` | 4 | 2.5 | 0.50 |

## 7. Coarse game state

The server’s initial state is:

```json
{
  "phase": "setup",
  "started_at": null,
  "finished_at": null,
  "tag_ids": {"green": [100, 101], "blue": [102, 103]},
  "activated_targets": [],
  "final_stage": "outer_ring",
  "first_center_manual": false,
  "first_center_tag": null,
  "first_center_team": null,
  "first_center_position": null,
  "first_center_confirmed_at": null,
  "message": "Configure four physical tags, then start.",
  "player_names": {"green": "", "purple": ""},
  "score_result": null,
  "updated_at": "<server epoch seconds>"
}
```

Every state snapshot also includes `server_time` in epoch seconds. After a win, `score_result` contains the recorded `score_id`, the row's `rank`, the current visible score count, and the immediately faster row's rank, player label, and time gap. It remains `null` in setup/running state and after a scoring-table reset.

Valid compatibility phases are:

- `setup`
- `running`
- `won`

Valid finale stages are:

- `outer_ring`
- `centre_ready`
- `centre_first_covered`
- `complete`

The server accepts partial operator updates for these coarse fields. It does not impose a transition graph; the browser is responsible for valid transitions.

On reset, preserve the current four tag IDs but replace every other server field with a fresh setup state.

The count-up timer uses the browser’s current epoch minus `started_at`. It begins when the running state is written and stops at `finished_at` when phase becomes won. It is not a countdown and does not pause in latency mode.

The server state and current run are process memory, not durable game recovery. JSONL event files are durable, but restarting the server does not automatically reconstruct `_state` or make an old log the current run.

## 8. Starting, resetting, and reloading

### 8.1 Start

Start is accepted only from setup and only when no start is already in flight.

Validate the four numeric inputs as integers, all unique, each from 100 through 105. On failure, show `Choose four different physical tag IDs from 100–105.` and do nothing else.

Selected tags need not be visible. For missing tags, add a log message noting the gamemaster override and that the tags may appear after the game starts.

The start transaction is:

1. clear local run progress (flows, placements, activations, impact baselines, timer stop, latency mode, and arm bindings) while retaining reusable camera/calibration caches;
2. create a new diagnostic run containing the selected IDs and sequence;
3. patch server state to running, setting start time, IDs, outer finale stage, empty center fields, and message `Build two mirrored crossing beams`;
4. log that the count-up timer is running;
5. make a best-effort Atom Manager request with a 1.2 s timeout to establish initial impact counters.

Start does not enable accelerometers. It logs which selected physical tags lack an online, available, enabled accelerometer and continues. If Atom Manager is unavailable, continue and initialize impact baselines when it reconnects.

If run creation or the state patch fails, remain visually in setup, show `Could not start logged run: <reason>`, and log the failure. Compatibility note: if run creation succeeds and the state patch then fails, the new diagnostic run remains active.

### 8.2 Reset

Reset:

1. ends an active run with reason `reset`;
2. immediately returns the browser to setup, preserving current assignments;
3. clears local flows, placements, activations, impact baselines, timer stop, latency state, and arm bindings, while retaining camera target caches, calibration, and the last relay snapshot;
4. logs that the current playfield was left unchanged; and
5. requests a server reset with a 3 s timeout.

If server confirmation is late or fails, keep the local reset and log the delay. Reset never reconstructs or reloads the starting playfield.

### 8.3 Initialization and resume

On page initialization:

1. construct fresh local flows;
2. start or recover the camera;
3. fetch coarse Laser Tag X state;
4. if phase is running, inspect the live playfield to recover progress;
5. log when resuming a non-setup game;
6. independently load Auto PP Cal 2;
7. independently poll the current diagnostic run; and
8. start the 200 ms game loop and 500 ms diagnostics poll.

Coarse state changes are also received over server-sent events. State application updates tag ownership, center-first-cover state, timer stop, the diagnostics run end-on-win attempt, and the rendered UI.

Outer-stage recovery:

- find the Laser Tag X center area;
- treat an outer target as activated if center links to it or its color is red;
- emit a `target_recovered` observation for each recovered marker;
- if all eight are recovered, attempt the center-only transition.

Finale-stage recovery while phase is still running, for `centre_ready` or `centre_first_covered` (and defensively for an inconsistent running/`complete` state):

- mark the outer stage complete;
- arm all flows at the end of their sequences;
- reconstruct the first center tag from coarse state when present, or reconstruct a manual first-cover sentinel when `first_center_manual` is true;
- reapply the appropriate center visual.

After diagnostic events load, independently recover the newest unsettled, completed, receipt-valid LTX center operation for each team. For `centre_ready`, its intended target must be marker 38. For `centre_first_covered`, its intended target must be the persisted first physical tag and its source must be a different assigned tag. Rebuild the operation-scoped center flow in the background from suction, release, and blow receipts, then stop at the normal live camera verification gate. `center_first_covered` and `center_second_covered` settle their operation IDs so this replay is idempotent.

When coarse phase is already `won`, initialization does not run playfield recovery; it restores the timer/mission from coarse state and relies on the already-persisted playfield visual.

Per-tag sequence indices, frozen arm poses, and outer placement ownership are not persisted. Existing playfield links remain the authoritative visual artifact after a reload.

## 9. Camera and tracking contract

### 9.1 Camera startup

Request Webcam `/api/status` with a 2.5 s timeout.

If the camera is not open, or reports an error without positive width and height, use its `remembered` selection. A remembered selection must include `index`; submit the entire object to Webcam `/api/select` with an 8 s timeout. If no selection exists, report that no camera has been selected in Webcam settings.

The displayed image source is Camera Calibration `/api/corrected-stream` with a cache-busting timestamp.

On startup failure, remove the image source, log the error, and show `Camera unavailable: <reason>`.

### 9.2 Tag polling

Outside latency mode, every game-loop iteration requests:

`GET <webcam>/api/tags?space=corrected`

The response must include:

- `corrected: true`; otherwise stop processing and report that distortion calibration is unavailable;
- `visible_ids` as an array; otherwise report that Webcam must be restarted for frame-current visibility;
- `tags`: debounced tracked tag records;
- `detections`: recent detection records;
- `width` and `height` for overlay geometry.

Merge `tags` and `detections` by numeric ID, with a later detection replacing an earlier tracked record of the same ID. Do not filter this merged position cache by its `missing` field. Separately form the live set by retaining only IDs present in `visible_ids`. All visibility-sensitive rules must use the live set.

A tag record used for geometry needs:

- numeric `id`;
- normalized `nx`, `ny` for its center;
- four pixel `corners` when size/overlay data is needed;
- optional `tracked` and `missing` metadata.

### 9.3 Geometric overlap

For tag record `t`, define its normalized center as `(nx, ny)` when both are finite.

Define normalized radius:

```text
pixel_diagonal = hypot(max(corner.x)-min(corner.x), max(corner.y)-min(corner.y))
radius = clamp(pixel_diagonal / 2200, 0.035, 0.16)
```

If corners are unavailable, use radius 0.06.

Record `a` overlaps target record `b` when both centers exist and:

```text
distance(center(a), center(b)) < max(0.025, radius(b) * 0.65 + 0.005)
```

### 9.4 Target caching and visibility transitions

On every successful camera poll:

- cache the newest merged record for each outer target 30–37 and center 38;
- for every outer target, mark `seen` when live;
- when a previously seen outer target first becomes non-live, record `missingSince` using browser monotonic time;
- reset `missingSince` to zero when it becomes live again.

A target counts as an occlusion candidate only if it was seen, became missing at or after the active physical tag’s coverage window opened, is still absent, has a cached position, and is not already activated.

## 10. Auto PP Cal 2 spatial model

### 10.1 Required calibration schema

Request Auto Pickup Game `/api/calibration2`. The response must contain `calibration.format: auto-pp-calibration-2-v1`.

For each arm, calibration must provide at least six usable points. A usable point contains:

- `camera.u`, `camera.v` as finite normalized coordinates; and
- `pose.set: true` with finite robot `pose.x`, `pose.y`.

Laser Tag X green uses calibration arm `green`. Laser Tag X blue uses calibration arm `purple`.

Each arm may specify `camera_view: normal` or `rot180`, but this is a player-rendering preference only. Spatial attribution uses the raw normalized coordinates returned by the corrected-camera API. A view change must never alter the robot pose predicted for the same physical camera detection.

For physical-tag height correction, the arm may provide at least four `parallax_points`, each with finite raised `(u,v)` and ground `(u,v)` coordinates.

### 10.2 Thin-plate spline definition

Compatibility requires thin-plate spline (TPS) interpolation, not nearest-neighbor interpolation.

For squared radius `r²`, use:

```text
U(r²) = 0                         when r² <= 1e-16
U(r²) = r² * ln(r²) / 2          otherwise
```

Fit the conventional TPS linear system containing one radial coefficient per sample plus affine terms `1`, `u`, and `v`. Add `1e-7` to each radial diagonal entry. Solve with Gaussian elimination and partial pivoting. Treat a pivot magnitude below `1e-12` as an unstable field and reject the model.

Build:

1. camera-to-robot TPS models `x(u,v)` and `y(u,v)` from calibration points;
2. robot-to-camera TPS models `cameraU(x/500,y/500)` and `cameraV(x/500,y/500)` from the same points reversed; and
3. when four parallax samples exist, raised-to-ground TPS models from raised camera coordinates to ground camera coordinates.

### 10.3 Pose/tag compatibility

An arm pose is taken from `arm.pose`, falling back to `arm.target`. It must contain finite X and Y; missing/invalid Z becomes zero.

For physical IDs 100–105, apply raised-to-ground parallax correction when available before mapping camera position to expected robot position.

The compatibility computation returns both:

- camera-space distance between the tag and the arm pose projected into camera space; and
- robot-space distance between the live arm pose and the tag projected into robot space.

An arm/tag match is acceptable when either:

```text
camera distance <= max(0.10, tag radius * 1.8)
```

or:

```text
robot distance <= 90 mm
```

If one distance is unavailable, the other may establish the match. If tag center, arm pose, or both spatial projections are unavailable, fail closed.

For exact coordinate compatibility, keep every spatial value in one raw corrected-camera sensor frame:

- Cal 2 camera samples are used exactly as stored;
- live corrected-camera tag centers are used without applying `camera_view`;
- physical-tag parallax maps raw raised coordinates to raw ground coordinates;
- camera-to-robot TPS consumes raw corrected-camera coordinates; and
- robot-to-camera TPS produces raw corrected-camera coordinates for direct comparison with live tag centers.

Do not mix raw calibration samples with view-rotated query points. If a player uses `rot180`, rotate only that player's rendered feed and controls. The spatial result must remain invariant under that presentation setting.

The game loop must skip all processing for a team while that team’s spatial model is missing. This includes verification after a page reload; pose-based activation is intentionally fail-closed.

## 11. Hardware polling and main loop

The main loop is scheduled every 200 ms, but must not overlap itself. Render immediately at the beginning of each tick. If the document is hidden, skip sensor work.

Opening the tab must not by itself poll shared robot or Atom state. Begin hardware polling only while phase is running and either:

- at least one selected physical tag appears in the merged camera records; or
- any flow has previously seen a tag, advanced beyond index zero, or is awaiting verification.

When hardware is needed:

- poll Atom Manager `/api/units` on each tick with a 1.2 s timeout, treating failure as an empty unit list;
- poll Laser Tag X `/api/arms` no more often than every 180 ms, with a 1.2 s timeout;
- reuse the last arm snapshot between relay polls and on relay request failure.

An Atom unit represents physical tag ID `100 + mid`. Only an online unit is eligible. Its impact counter is `unit.accel.impactCount`.

For each per-tag flow, the first finite impact counter observed establishes a baseline without firing. A later strictly larger counter consumes one impact and replaces the baseline. Counter decreases are not treated as impacts.

After a normal successful tick, status reads:

`Corrected camera live: <#ids or no tags> · active arms: <0..2>/2`

On any normal tracking exception, clear the overlay and show `Tracking unavailable: <reason>`. The next scheduled tick retries.

## 12. Per-tag and per-team state

There are four per-tag flows keyed by owner team and input position: `green0`, `green1`, `blue0`, `blue1`.

Each flow tracks at least:

- flow state: `sequencing`, `awaiting_verify`, `armed`, or `centre_first`;
- sequence index and context (`board` or `center`);
- whether the tag has ever been seen and its last camera record;
- activated slot and current/provisional/covered/release target markers;
- target-coverage window and occlusion candidates;
- frozen drop and release arm poses;
- arm/tag match details and pickup priming;
- suction and blow edge state;
- five-frame/400 ms confirmation and 2 s contradiction timers;
- placement ID plus an optional team LTX operation ID; and
- center-pickup candidacy.

Each team has exactly one physical arm and therefore at most one foreground `sequencing` flow. `awaiting_verify` flows do not own the arm; they continue camera-only verification in the background while the arm may pick up the team’s other tag.

### 12.1 Binding an arm to one tag

If that team's LTX telemetry is present and the script is active, its operation owns an exclusive arm lease through `operation_completed` or `operation_failed`. If the operation's flow has already entered background verification, do not fall through to spatial binding for another tag while that lease remains active. For a noncompleted/nonfailed `operation_started`, bind only that team's arm to the assigned physical tag named by the operation. Copy its operation ID, reset pump-edge evidence for the new operation, and treat a logged `source_pickup` as pickup-arm confirmation. Green maps to `green_ltx`; the blue game team maps to the purple player/relay and `purple_ltx`.

When the bound operation completes with valid pump receipts but its operation ID has already received a terminal Laser Tag X disposition, restart that tag's sequencing flow before selecting the current active operation. This releases stale bindings after a terminal activation or safety block while preserving the arm lease until the controller reports `operation_completed`. An unsettled, receipt-valid completed operation remains bound so its durable receipt bundle can drain through replay and visual verification.

Otherwise:

1. keep the current team binding while that flow remains `sequencing`;
2. if exactly one flow expecting `pickup` had been seen and is now hidden, bind it only if its last position matches the arm;
3. if multiple such tags disappear, choose the acceptable arm match with the smallest robot-space distance, or camera distance scaled by 1000 when robot distance is unavailable;
4. if no hidden-pickup case binds, consider sequencing tags whose live or last positions match the arm and choose the same smallest score.

There is no time-based grace cutoff on a cached last tag. Spatial compatibility is the gate.

### 12.2 Unexpected reappearance

After a flow has advanced beyond its first node, if its physical tag becomes visible while the next expected step is not `verify`, restart that flow at Start. This prevents a tag that reappeared during an alleged carry from continuing through the sequence.

## 13. Detection-step semantics

Process only the flow’s current expected node. A customized sequence may omit or reorder events; step-specific internal observations, such as release-edge capture, still run when relevant even if the corresponding node is absent.

### 13.1 Pickup

Pickup requires all of the following:

1. the physical tag has previously been seen;
2. the arm has been spatially matched to that tag;
3. the tag becomes non-live, which primes pickup; and
4. while primed, pump mode is `suck`.

For green while LTX telemetry is active, a matching `source_pickup` intention may supply the pickup-arm confirmation in place of the local spatial match. Center pickup also has the explicit fallback/ambiguity rules in section 16.

If the tag reappears before suction, cancel priming. On success, create a placement ID when one does not already exist, emit `placement_started` and `pickup`, and advance one node. Scripted placements reuse their operation ID as the placement ID; manually controlled placements generate an independent ID.

Center pickup has an additional binding mechanism described in section 16. Once a center pickup is uniquely bound, a leading `pickup` node is consumed immediately because the suction transaction itself supplied the identity evidence.

### 13.2 Suction start

Detect whenever current pump mode is `suck`. On success:

- set the coverage-window monotonic timestamp;
- clear provisional target state;
- emit `suction_start` with pump mode; and
- advance.

### 13.3 Pickup impact

When the assigned Atom impact counter increases:

- fire the owner-team flash;
- emit `pickup_impact`; and
- advance.

### 13.4 Continuous release-edge capture

For every non-background flow, track pump mode even when no pump node is currently expected.

A release edge occurs when previous mode was `suck` and current mode becomes `off` or `blow`. Record that release independently of pose availability, and freeze the first valid arm pose at or after the edge only once for that placement.

For manual operation, latch `blow` only after suction has been observed in the same per-tag pump transaction. If blow occurs after that transaction's suction but a release pose was not yet captured, capture it then when available. A missing manual pose sample does not invalidate coherent final camera evidence.

For Green and Purple LTX, pump evidence is operation-scoped instead of inferred from a shared sampled level. Before queue movement begins, LTX issues an idempotent pump-off protocol probe and requires the relay to return the same operation ID, the authenticated player side, a positive command sequence, and a valid measured pose. Every subsequent scoped pump command requires the same receipt fields, and its sequence must be strictly greater than the preceding receipt. A missing or contradictory receipt stops the script and commands the pump to its safe off state.

Successful suction emits `suction_completed` with the operation ID and relay pump-command sequence; only that event can satisfy scripted pickup and suction-start detection. `release_completed` must carry the matching operation ID, physical tag, actual release pose returned by the successful pump-off command, and relay pump-command sequence. Resolve and freeze that pose only for the matching flow. `blow_completed` must carry the same operation ID and its relay command sequence; consume it only after the matching release pose has been accepted. The suction, release, and blow sequences must be strictly ordered. Events belonging to another operation cannot advance the flow, and every command sequence is consumed idempotently by at most one flow. A completed operation with a missing, invalid, or unordered receipt bundle emits `operation_protocol_error`, returns that tag flow to Start, and releases that team's arm lease instead of retaining a stale flow.

If the browser did not consume those events while the Laser Tag X document was visible, replay the newest completed, receipt-valid operation for that physical tag from the durable run log. The reducer binds by operation ID and physical tag, consumes pickup and suction from `suction_completed`, restores any previously logged provisional camera occlusion, consumes `blow_completed`, and stops at visual verification. Replaying the same bundle is idempotent and must not emit a second activation.

An operation is permanently ineligible for replay once Laser Tag X emits `target_activated`, `activation_blocked`, or `operation_protocol_error` with that operation ID. Record that disposition synchronously in the browser before posting the event, and derive it again from the durable run log so neither event-posting latency nor a browser reload can rebind a rejected operation. Legacy `release_pose_target_unresolved` records remain terminal for compatibility, but new runs emit only nonauthoritative `release_pose_diagnostic` records.

For board context, rank the frozen release pose against outer targets only for diagnostics as described in section 14. For center context, retain only the release pose and release timestamp.

### 13.5 Target hidden

In board context, find the closest acceptable newly hidden target under the arm, mark coverage provisionally, freeze its last frame-current camera detection, emit a nonauthoritative `target_hidden`, and advance. This provisional marker cannot by itself activate a target; sustained final camera evidence remains authoritative.

Center context succeeds when cached center marker 38 exists, marker 38 is currently non-live, and the arm spatially matches center. It records center coverage and advances.

### 13.6 Suction off

The optional `suction_off` node succeeds only when:

- target coverage is confirmed;
- suction has previously been observed;
- current pump mode is `off` or `blow`; and
- the stored release-edge time is not earlier than the coverage-confirmation time.

### 13.7 Blow

The `blow` node succeeds when the flow’s blow latch is set. Consume the latch, emit `blow`, and advance.

### 13.8 Drop impact

When the Atom impact counter increases:

- freeze the current arm pose as the drop pose;
- fire the owner-team flash;
- emit `drop`; and
- advance.

### 13.9 Verify transition

As soon as `verify` becomes current for a sequencing flow:

- change it to `awaiting_verify`;
- release its team-arm binding; and
- continue checking it in the background on every tick.

If the physical tag is not live, clear manual visual confirmation and contradiction evidence and keep waiting. For a scripted board placement, a missing-tag poll is not an observed contradiction: it neither advances nor erases the scripted contradiction counter.

Use the frozen release pose, else the frozen drop pose, else current arm pose as the recorded pose for final checks.

## 14. Outer target attribution and verification

Outer activation is owned by a camera-observed placement transaction. Pump and pickup evidence bind the physical tag to the transaction; final frame-current camera evidence chooses the target. Arm release pose is diagnostic and can neither select a different marker nor veto coherent camera evidence.

### 14.1 Live target snapshots

Update an outer target's cache only while that marker is present in the webcam service's frame-current `visible_ids`. Never refresh its timestamp or position from a stale merged tracker entry. When a previously seen target becomes non-live after the placement's suction coverage window opened, retain its last live detection and monotonic missing-since time as a provisional occlusion candidate.

### 14.2 Release-pose diagnostics

Freeze the release arm pose once per placement when one is available. Rank that pose against calibrated outer targets using the existing distance, height, spacing, and margin calculations, but treat the result only as a diagnostic suggestion. Emit `release_pose_diagnostic` with the ranked targets, suggested marker when one exists, release pose, pump receipt, placement ID, operation ID, and provisional occlusion candidates. A manually observed pump release remains valid if no pose sample is available.

An unresolved or disagreeing release-pose ranking is not terminal. It must not set `releaseTargetMarker`, mark coverage authoritative, emit an activation, or discard a camera candidate.

### 14.3 Final visual candidate

For each unactivated target that is frame-current hidden and either became hidden after this placement's suction window opened or appears in this placement's recorded occlusion candidates:

1. project every live physical tag ID 100–105 from its raised camera position back onto the ground plane using its owning team's Auto PP Cal 2 parallax model;
2. rank the projected physical tags by normalized distance to the target's frozen last-live position;
3. require this flow's physical tag to be nearest by at least 0.012 normalized camera units unless there is no runner-up;
4. require the projected physical tag to geometrically overlap the frozen target; and
5. if the same physical tag overlaps multiple hidden targets, require the nearest target to lead the runner-up by at least 0.012.

Use marker number as the final tie-breaker only after the uniqueness margins have passed. Otherwise there is no candidate.

### 14.4 Success and contradiction handling

The same visual candidate must appear in at least three of the latest five observed frames spanning at least 400 ms for both manual and scripted control, with normalized position jitter no greater than 0.008. A changed or missing candidate resets confirmation. A missing physical tag waits and does not count as contradiction evidence.

The sustained camera-selected marker and physical tag remain authoritative for both scripted and manual placement. Scripted operation data is correlated after the visual result is stable; it provides transaction attribution and divergence diagnostics but cannot substitute a future active cue for the released operation that caused the placement.

On success:

- freeze `releaseTargetMarker` to the camera-selected marker;
- emit `verify` with raw and ground-projected physical detections, the frozen target detection, placement ID, optional operation ID, occlusion evidence, and release/arm diagnostics;
- activate and link that exact marker; and
- emit `target_activated` carrying the placement ID.

When a live released tag does not uniquely overlap a target hidden during its placement, manual control requires the contradiction continuously for 2000 ms. Scripted control requires at least five contradictory live-tag polls spanning at least 2000 ms. Then emit `activation_blocked` and restart the tag flow. Release-pose disagreement is logged but is not a contradiction category.

### 14.5 Released-operation attribution

At board-truth confirmation, inspect all unsettled matching-side operations for the observed physical tag. An operation is eligible only when it:

- has `operation_started`, `suction_completed`, `release_completed`, and `blow_completed`;
- has strictly ordered positive pump command sequences;
- has no `operation_failed`; and
- has not been settled by a Laser Tag X `target_activated`, `activation_blocked`, or `operation_protocol_error` event with the same operation ID. Treat legacy `release_pose_target_unresolved` events as terminal when replaying an older run.

Never compare a stable board result with an operation that has only started or is merely the newest active cue. Rank eligible released operations by exact physical tag and typed destination, using the target-hidden interval and release time to break ties. An exact match produces `matched_sequence` when it is still the flow's current operation or `matched_sequence_late` after the queue has advanced.

When there is no destination match, emit `true_sequence_divergence` only if one released operation is uniquely correlated by physical tag and timing. If correlation is ambiguous or unavailable, preserve the board decision with `board_truth_unattributed` and do not emit a divergence. `target_activated`, `center_first_covered`, and `center_second_covered` carry the chosen operation ID when available plus `decision_source: visual_board_truth`, the typed observed destination, and the full attribution record.

Typed destinations distinguish `{"kind":"board_marker","id":38}` from `{"kind":"physical_tag","id":102,"board_marker":38}`. `target_marker` remains in the event envelope for backward compatibility.

## 15. Outer completion and beam semantics

Every activated target records:

- physical tag owner team;
- physical tag ID;
- target marker;
- target side;
- total activation count; and
- optional operation ID.

The UI and transition use the size of the unique activated-marker set. While the outer ring is incomplete, the camera also tracks whether one physical tag in the 100–105 range is already covering cached marker 38. Marker 38 must be hidden and the physical tag must be live and overlap its cached position. The candidate is cleared if marker 38 reappears or no qualifying physical tag remains visibly over the center.

At eight activated markers, run the center-only transition. If an early center candidate is still present, persist it immediately as the first center cover, apply the green first-cover visual, and enter `centre_first_covered` so only the different final tag remains. Otherwise enter the ordinary white `centre_ready` stage. A failed transition logs `Outer-ring completion will retry: <reason>` and is attempted again on later ticks.

Mirrored links are visual progress only. The implemented game does not use geometric line-crossing analysis as an acceptance condition.

## 16. Center finale

### 16.1 Arming

After the outer transition, each flow becomes `armed` at End. If a prepositioned first center tag belongs to one of the four assigned flows, that flow becomes `centre_first` instead. For each team, a separate center-pickup transaction watches its assigned tags. Manual operation watches sampled arm pump mode; LTX automation watches matching-side operation-scoped command receipts.

The first physical tag already covering center is excluded from eligibility for the second pickup.

### 16.2 Center pickup identity

A tag becomes a center-pickup candidate after the flow is considered previously seen and the tag becomes non-live. At the outer-to-center transition, “previously seen” is initialized from the flow’s entire outer-stage history, not only from visibility in the transition frame; subsequent live frames also set it. Store the tag’s last camera position and arm-match evidence.

A manual transition into pump mode `suck` opens one center pickup transaction. For either LTX player, only a valid matching-side `suction_completed` receipt opens the transaction, and its operation ID, physical tag, typed destination, and relay command sequence are retained. While an LTX script is active, a sampled pump edge never opens an unscoped manual fallback; the center detector waits for the operation receipt. Scripted candidates are restricted to the receipt's physical tag. Candidates already hidden are latched, and eligible tags that become hidden while suction remains active are added.

Each scripted operation ID may open at most one center-pickup transaction during a run. Once that transaction binds or closes unresolved, later camera polls must not reopen it from the same retained suction/release receipts. This keeps `center_pickup_release_pending` and `center_pickup_unresolved` transition-based: each may occur at most once for that operation. Independent board-truth voting remains active after an unresolved transaction and may still confirm the physical placement.

- No candidates: wait and log once that suction is active.
- One candidate: bind it to the arm and begin its center sequence.
- Multiple candidates: do not rank or guess; remain ambiguous until camera evidence removes candidates or release-time placement resolves identity.
- A candidate that reappears while suction remains active is eliminated.

Unlike outer pickup, a sole latched center candidate is permitted even when the stored spatial match was not acceptable; record this as fallback evidence. Ambiguity is resolved through physical reappearance/release evidence, not a guessed nearest tag. When binding a scripted candidate, copy the operation ID and suction command sequence into the center flow and all resulting observations. The normal operation-scoped sequence processor then consumes only that operation's suction, release, and blow receipts; it must not clear or replace the operation because the leading pickup node has already advanced.

### 16.3 Release-time ambiguity resolution

When suction changes to a nonsuction mode during an unresolved manual center transaction, freeze the release pose. For automated LTX, freeze only the pose carried by the matching-side operation's valid `release_completed` receipt.

For every remaining candidate, test whether its live tag is now a valid center placement. If exactly one qualifies, bind it, begin its center sequence, retain the release pose, and immediately commit the center placement. If multiple qualify, remain ambiguous and commit nothing. Candidates that reappear away from center are eliminated. Close the transaction if all candidates are eliminated.

### 16.4 First center cover

Center coverage may first be observed while the live arm hides marker 38. If marker 38 becomes hidden only after release, use the matching operation's frozen `release_completed` pose instead of the arm's later return pose. The chosen pose must spatially match cached marker 38. Record a changed pending reason when the marker is still visible, the target is unavailable, or the chosen pose does not match.

A first cover is valid when:

- cached center marker 38 exists;
- marker 38 is currently hidden;
- the verifying physical tag is live; and
- that tag overlaps the cached center position.

On the ordinary sequence-driven verification path, the recorded release/drop/current arm pose must additionally match the physical tag. The special post-release ambiguity-resolution path may commit immediately from its unique visual placement evidence without repeating that pose-match gate.

On success:

- finish that flow;
- persist `final_stage: centre_first_covered`;
- persist first tag ID, owner team, normalized position/corners/timestamp, and confirmation epoch;
- set message `Tag #<id> covers marker 38. Cover it with a different assigned tag.`;
- change center to green at 25% brightness and maximum glow;
- emit `center_first_covered`; and
- keep the game running.

### 16.5 Second center cover and win

The second cover must satisfy the first-cover conditions and also:

- use a different physical tag ID;
- keep the first physical tag hidden;
- have a persisted or live last position for the first physical tag; and
- overlap that first-tag position.

On success:

1. change center to red, glow 4, bloom 2.5, and 50% global/Atom brightness;
2. emit `center_second_covered` with the ID of the covered first tag;
3. patch phase to `won`, finale stage to `complete`, finish time to now, and message `Laser grid completed`;
4. stop the timer; and
5. end the diagnostic run with reason `won` when the current run is known to the browser.

The second tag may belong to either team.

The gamemaster may bypass physical center detection from the Manual tag placement control. The first manual #38 activation sets the first-covered visual, persists `first_center_manual: true` with no invented physical tag ID, clears in-progress center flows, and suppresses automatic center binding. The next manual #38 activation performs the normal final visual and `won` transition, including score and diagnostic-run completion behavior. If the first center cover was detected normally, the same final manual #38 option may still be used to finish the game.

## 17. Flash latency test mode

Latency mode is an operator diagnostic available only during a running game.

On enable:

- clear latency baselines;
- leave the phase and timer unchanged;
- suspend all camera polling, tracking overlay, placement sequencing, arm polling, and normal progress detection;
- establish best-effort Atom impact baselines with a 1.5 s timeout; and
- log that only global flashes will be triggered.

During each tick:

- poll Atom Manager with a 1.2 s timeout;
- count selected tags with a finite online impact counter;
- establish missing baselines without flashing;
- on each counter increase, fire the owner-team flash and log it;
- show `Flash latency test · <n>/4 drop sensors connected · visual tracking disabled`.

On disable, clear only latency baselines and resume normal visual sequencing from its existing state. Ordinary per-flow impact baselines are not synchronized during latency mode. Therefore, if an ordinary baseline already existed and the Atom counter rose during latency mode, the next normal tick may consume that cumulative increase when the flow is waiting at an impact node; if no ordinary baseline existed, the first normal reading merely establishes it.

## 18. Laser Tag X backend API

All responses are JSON unless the route serves HTML, SSE, or a JSONL attachment.

### 18.1 `GET /` and `GET /game`

Serve the page with no-store caching.

### 18.2 `GET /api/state`

Return the coarse state snapshot plus `server_time`.

### 18.3 `GET /api/events`

Server-sent event stream of full state snapshots. Use a 0.2 s stream interval and wake clients whenever the state changes.

### 18.4 `POST /api/operator`

Requires `gamemaster`, else 403:

```json
{"ok": false, "error": "gamemaster required"}
```

With `{"reset": true}`, reset all fields except current tag assignments.

Otherwise accept these optional fields:

- `phase`
- `started_at`
- `finished_at`
- `message`
- `final_stage`
- `first_center_manual`
- `first_center_tag`
- `first_center_team`
- `first_center_position`
- `first_center_confirmed_at`
- `tag_ids.green` and `tag_ids.blue`, each only when it is an array of exactly two values; coerce both values to integers
- `activated_targets`, replacing progress with the sorted unique marker IDs 30–37
- `activated_target`, atomically adding one marker ID 30–37 to existing progress

Set `updated_at` on nonreset patches and return the complete snapshot.

When a nonreset patch changes the phase from anything other than `won` to `won`, and it provides a valid finish time at or after the stored start time, append one cooperative Green/Purple result to `score-log.txt`. Include `green_player_name`, `purple_player_name`, `game_mode`, and `game_type` from the player names and selected mode snapshotted at the transition into `running`. A repeated `won` patch for the same current game must not append a duplicate score.

### 18.4.1 Score routes

All three routes require `gamemaster`.

`GET /api/scores` returns the score rows recorded after the current table-reset cutoff, ranked by fastest elapsed time first, plus `reset_at` and the score-log filename. Each returned row is annotated with its one-based `rank`; rows below first also include `next_faster_rank`, `next_faster_seconds`, and `next_faster_player_label` for the immediately preceding row.

`POST /api/scores/reset` persists the current server time as the new table cutoff and returns an empty score list with `log_preserved: true`. It must not modify `score-log.txt`.

`GET /api/scores/log` downloads the complete append-only log as `laser-tag-x-score-log.txt` with MIME type `text/plain`.

### 18.5 `GET /api/arms`

Return a compact relay adapter:

```json
{
  "arms": {
    "green": {"connected": true, "pose": [0,0,0,0], "target": [0,0,0,0], "pump_mode": "off"},
    "blue":  {"connected": true, "pose": [0,0,0,0], "target": [0,0,0,0], "pump_mode": "off"}
  }
}
```

Relay green maps to response green; relay purple maps to response blue. Preserve all arm-state fields supplied by the relay, including connection, robot mode, enabled state, pose, target, joints, faults, servo state, control mode, and pump mode. If the relay is unavailable, return `{"arms": {}}` with HTTP 200.

### 18.6 `POST /api/playfield`

Requires `gamemaster`.

Request:

```json
{"areas": ["<complete area objects>"], "settings": {"<view settings>"}}
```

Both must have the indicated types, else 400 `areas and settings are required`. Return 503 `playfield unavailable` if the in-process Playfield Areas replacement API is unavailable. Convert playfield validation errors to HTTP 400. On success:

```json
{"ok": true, "areas": ["<installed areas>"]}
```

### 18.7 `GET /api/run`

When no current run exists:

```json
{"ok": true, "run": null, "events": []}
```

Otherwise return current run metadata without its internal events array, plus up to the most recent 1000 in-memory events:

```json
{
  "ok": true,
  "run": {"run_id": "...", "next_seq": 42, "active": true},
  "events": ["<normalized events>"]
}
```

### 18.8 `POST /api/run`

Requires `gamemaster`.

Start request:

```json
{"action": "start", "detail": {"tag_ids": {"green": [100,101], "blue": [102,103]}, "sequence": ["..."]}}
```

Create an ID `<UTC YYYYMMDDTHHMMSS>-<8 lowercase hex>`, replace the current in-memory run with an active empty run, then append `run_started`.

End request:

```json
{"action": "end", "detail": {"reason": "reset|playfield_reload|won", "phase": "..."}}
```

Append `run_ended` to an existing current run and mark it inactive. An invalid action, or `end` without any current run, returns 400 `action must be start or end`.

Return:

```json
{"ok": true, "run": {"...including internal events..."}, "event": {"<appended event>"}}
```

Starting a run replaces current in-memory run metadata even if the prior run was still active; its existing JSONL file remains on disk.

### 18.9 `POST /api/run/event`

Request shape:

```json
{
  "source": "laser|gamemaster",
  "run_id": "optional-current-run-id",
  "kind": "nonempty event name, truncated to 80 chars",
  "category": "optional",
  "detail": {"...": "..."}
}
```

Authorization:

- `gamemaster` may submit only source `laser` or `gamemaster`;
- `green` without gamemaster is forced to source `green_ltx` regardless of submitted source;
- `purple` without gamemaster is forced to source `purple_ltx` regardless of submitted source;
- all others receive 403.

Reject missing kind with 400, inactive/missing current run with 409, and a supplied valid run ID that differs from current with 409 `run changed`. Append and return the normalized event on success.

### 18.10 Programmatic external-event ingestion

The Auto Pickup Game sibling may call an in-process function equivalent to `/api/run/event`. It accepts source `green_ltx` or `purple_ltx`, a nonempty string event name, optional detail/category, and an active current run. It returns either the normalized event or `no active Laser Tag X run`/`invalid event`.

Auto Pickup Game exposes this bridge as `POST /api/laser-tag-x-intent` to green, purple, and gamemaster roles. Player calls derive the producer from the authenticated side rather than trusting a submitted producer name.

### 18.11 `GET /api/run/<run_id>.jsonl`

Requires `gamemaster`. Run IDs may contain only alphanumerics, hyphen, and underscore. Return 404 `run not found` when invalid or absent. Otherwise serve the file as an attachment.

### 18.12 `GET /api/runs`

Requires `gamemaster`. List `.jsonl` files newest-name-first:

```json
{"ok": true, "runs": [{"run_id": "...", "bytes": 12345}]}
```

## 19. Diagnostic event format

Every persisted event is one compact JSON object on one line:

```json
{
  "schema_version": 2,
  "event_id": "32 lowercase hex characters",
  "seq": 1,
  "run_id": "20260807T110644-dd44b61f",
  "wall_time": "UTC ISO-8601",
  "server_epoch": 1786100804.502209,
  "producer": "gamemaster|laser|green_ltx|purple_ltx",
  "category": "lifecycle|intent|command|observation|decision|log",
  "event": "event_name",
  "operation_id": null,
  "team": null,
  "physical_tag": null,
  "target_marker": null,
  "queue_index": null,
  "attempt": null,
  "client_monotonic_ms": null,
  "payload": {}
}
```

Promote the named fields out of detail into top-level columns. Accept `pickup_attempt` as a legacy alias for `attempt`. Leave all remaining detail in `payload`.

Default category selection, when no category is supplied, is:

- `intent` for producer `green_ltx` or `purple_ltx`;
- `log` for event `log`;
- `lifecycle` for `run_started` and `run_ended`;
- `observation` otherwise.

Keep only the latest 1000 events in memory, but append every event to disk. Each append must flush and fsync before returning so the diagnostic path is durable.

Laser Tag X emits at least these event names:

- lifecycle/log: `run_started`, `run_ended`, `placement_started`, `log`
- sequence observations: `pickup`, `suction_start`, `pickup_impact`, `target_hidden`, `blow`, `drop`
- target evidence: `target_occlusion_candidate`, `release_pose_diagnostic`, `verify`, `activation_blocked`, `pickup_prior_mismatch`, `center_pickup_prior_mismatch`
- board decisions: `target_activated`, `target_recovered`
- attribution decisions: `board_truth_unattributed`, `ltx_prior_divergence`
- center decisions: `center_pickup_candidate`, `center_pickup_waiting`, `center_pickup_ambiguous`, `center_pickup_candidate_eliminated`, `center_pickup_release_pending`, `center_pickup_resolved_at_release`, `center_pickup_unresolved`, `center_pickup_bound`, `center_first_covered`, `center_second_covered`

Green and Purple LTX may emit:

- `script_started`, `script_completed`, `script_stopped`, `script_failed`
- `operation_started`, `operation_completed`, `operation_failed`
- `source_hover`, `source_pickup`, `suction_start`, `lift`
- `target_hover`, `target_descent_started`, `target_pose_reached`
- `release_started`, `release_completed`, `blow_started`, `blow_completed`, `return_hover`

Operation events carry `operation_id`, one-based `queue_index`, queue length, loop number, pickup attempt, physical tag, legacy intended target marker, and a typed `destination`. Targets below 100 use `board_marker`; a physical-tag destination uses `physical_tag` and includes center board marker 38 as context.

Browser-originated Laser Tag X event submission is best-effort and must not block game progression when the event request fails.

## 20. Dependency contracts

### 20.1 Webcam

Required routes:

- `GET /api/status`: camera open/error/dimensions plus remembered selection
- `POST /api/select`: open remembered camera configuration
- `GET /api/tags?space=corrected`: corrected merged tracking data and frame-current IDs

### 20.2 Camera Calibration

Required route:

- `GET /api/corrected-stream`: corrected MJPEG stream

### 20.3 Playfield Areas

Required routes/capabilities:

- get complete state;
- patch/delete an area;
- add a directed area link;
- patch view settings;
- external `/screen` page;
- in-process atomic area replacement, settings update, and immediate save.

Area lookups for game targets require both matching numeric marker and a name beginning `Laser Tag X -`.

### 20.4 Atom Manager

Required route:

- `GET /api/units`

Relevant unit fields are `online`, `mid`, and `accel.impactCount`; start-time advisory checks also inspect `accel.available` and `accel.enabled`.

### 20.5 MG400 Relay

Required in-process capability:

- get one arm state for relay side `green` or `purple`

Relevant fields are `connected`, `pose`, `target`, and `pump_mode`, where pump mode may be `suck`, `blow`, `off`, or `conflict`.

Operation-scoped `POST /api/pump` commands accept `operation_id` and return the relay-issued `operation_id`, `command_seq`, `side`, and measured `pose`. Player-side Cartesian proxies must preserve these request and response fields unchanged; they must not synthesize command sequences locally.

### 20.6 Auto Pickup Game

Required route:

- `GET /api/calibration2`

Optional but integrated route:

- `POST /api/laser-tag-x-intent`, forwarding authenticated Green or Purple LTX events into the active Laser Tag X run.

Neither LTX player waits for, polls for, or stops because of Laser Tag X verification or `target_activated` feedback. All Laser Tag X intention/receipt writes are fire-and-forget diagnostics from the player's perspective: success, rejection, delay, or unavailability must not gate the current robot operation or the next queue operation. After `operation_completed`, LTX immediately continues its own queue. Laser Tag X retains the released operation as a background-verification flow and verifies it when the arm naturally leaves the target and exposes the physical tag to the camera.

The LTX Cue Builder exposes `Go`, a `Pause`/`Resume` toggle, and `Stop`. Pause is cooperative and preserves the current queue index, pickup attempt, operation identity, pump receipts, and trusted tag placements. A pause request lets the current planned movement or atomic release/blow sequence reach its safe checkpoint, then prevents the next robot action. Resume continues the same cue from that checkpoint without emitting a replacement operation. Stop remains distinct: it aborts the cue, clears any paused state, and commands the arm to hold.

While a Cue Builder cue is running, a video click is a temporary motion override rather than a cue cancellation. The latest click supersedes any earlier click, waits for an in-flight pump or release/blow action to finish, lifts the arm vertically to the calibrated transport height, moves to the clicked X/Y position, and waits for arrival. The cue then retries the interrupted movement at the same queue index and operation identity. If the interrupted target is at pickup or drop height, the arm returns through that target's transport-height hover before descending. A click during a manually paused cue performs the override but leaves the cue paused afterward. Stop cancels both the cue and any pending click override.

Each Green and Purple LTX video shows distance meters during a running game:

- During the outer ring, show four orange meters for that player's four markers (Green 30–33; Purple 34–37). Each meter uses the uniquely closest assigned physical tag, shows horizontal and Z error in millimeters, and turns green with a `DROP` cue inside the same 90 mm horizontal and 60 mm vertical acceptance window. Remove a meter as soon as its marker appears in `activated_targets`.
- During `centre_ready`, replace the outer meters with distance to marker 38.
- During `centre_first_covered`, replace it with distance to `first_center_tag`, excluding that already-placed tag as a source.
- While suction is active, use the live arm pose as the carried tag pose only when the operation or a 12 mm nearest-tag margin identifies one physical tag. Otherwise keep the meter orange and report that the closest tag is unclear.

The Cue Builder's target-drop arrival wait uses independent 18 mm X/Y and 1 mm Z tolerances. It must not release while a vertical descent is still in progress. A flat target (IDs 30–38) uses the calibrated drop Z; only a physical 100-series target adds the 31.5 mm tag-height offset for stacking one physical tag on another. Laser Tag X requires sustained unique, parallax-corrected camera attribution before activation; its release-pose ranking remains diagnostic.

## 21. Error handling and safety properties

The reconstructed tab must preserve these safety properties:

- Never activate an outer target from camera occlusion alone.
- Never guess between multiple center-pickup candidates.
- Never process a team without a valid six-point TPS arm model.
- Never activate an outer target unless the released physical tag has sustained unique, parallax-corrected camera overlap with a target hidden during that placement.
- Do not let a background verification flow consume later pump edges from the same team arm.
- Do not overwrite a frozen release pose with a later release belonging to the team’s other tag.
- Never make LTX robot motion wait for or fail because of Laser Tag X feedback; Laser Tag X is a passive consumer of operation telemetry.
- Do not require tags or accelerometers to be present just to start; degrade and retry instead.
- Do not poll robot/Atom hardware merely because the tab is open.
- Do not half-install the shared starting board.
- Do not alter the live playfield during ordinary Reset.

Network behavior should be retry-friendly:

- camera/calibration/tracking failures are reported and retried by later ticks;
- missing Atom or relay data becomes empty/cached data and does not throw the game out of running phase;
- outer completion retries after failure;
- diagnostic event writes are best effort from the browser;
- server-side JSONL writes are synchronous and durable.

Known compatibility edge cases to preserve or explicitly decide to improve in a future version:

- the customizable sequence is only structurally validated, so a sequence without `verify` cannot finish;
- a successfully created run is not rolled back if the subsequent running-state patch fails;
- current game state and current-run metadata vanish on server restart even though JSONL files remain;
- outer recovery reconstructs activated IDs from visuals/links, but not per-tag placement ownership;
- impact counter decreases do not reset the baseline;
- starting a new run replaces an existing active current run without first appending `run_ended`;
- ending an existing inactive current run may append another `run_ended` event;
- the center transition deliberately deletes every noncenter area in the shared playfield;
- center visual mutations and coarse-state mutations are sequential rather than transactional, so a network failure between them can temporarily leave a center visual and finale state out of agreement;
- applying restored server assignments updates the internal ownership model, but ordinary initialization does not rewrite the four numeric input values from that state, so a reloaded running page may display disabled default IDs while processing the server-restored IDs.

## 22. Acceptance scenarios

A rebuild is behaviorally compatible when at least the following scenarios pass.

### 22.1 Setup and validation

1. Fresh page shows setup, timer 0.0, default IDs, default sequence, and camera startup status.
2. Duplicate IDs or an ID outside 100–105 prevents start with the exact validation message.
3. Four valid but invisible IDs can start and generate an override log.
4. Sequence edits appear identically in both arm lanes and persist across reload.
5. A stored sequence with verify in the middle reloads with verify last.

### 22.2 Board load

1. Loading installs exactly 13 areas with the exact IDs, marker positions, colors, and settings in section 6.
2. Loading ends an active diagnostic run, resets phase/progress, and preserves selected IDs.
3. A failed atomic install leaves a visible error and does not report ready.

### 22.3 Hardware gating

1. A running game with no selected tag ever seen makes no Atom or relay request from the normal loop.
2. Once a selected tag appears, polling begins.
3. Missing corrected coordinates or `visible_ids` prevents game processing and surfaces the required tracking error.
4. One invalid arm calibration blocks only that team.

### 22.4 One outer placement

1. A seen tag hidden under its matched arm and followed by suction consumes pickup.
2. Target disappearance after suction logs only provisional coverage.
3. Suck-to-off/blow freezes one release pose for diagnostics but does not choose or veto the target.
4. The same unique parallax-corrected physical-tag/hidden-target overlap must persist for at least five observed frames spanning at least 400 ms.
5. Success turns the target red, links it from center, records activation, frees the arm, and restarts the tag flow.
6. A persistent mismatch for 2 s blocks activation and restarts the flow.

### 22.5 Parallelism

1. Green and blue arms can each own a foreground flow simultaneously.
2. Within one team, one tag may await camera verification while the other owns the arm.
3. Pump edges for the second tag do not alter the first tag’s frozen evidence.

### 22.6 LTX integration

1. Green and Purple `operation_started` events bind only their mapped game arm to that operation’s physical tag.
2. After a side's `script_started`, its activation without an unconsumed released operation remains blocked.
3. Matching releases and Laser activations display aligned paths independently in both diagnostics panels.
4. A scripted operation whose intended target differs from the sustained camera-selected target is blocked and recorded as divergence evidence.

### 22.7 Outer completion

1. Activating matching index pairs adds four green-to-blue links as pairs become available.
2. Seven unique targets do not trigger the center transition.
3. The eighth deletes all areas except center, makes center white/glowing at 25%, and arms all four physical tags.
4. A deletion or center-patch failure logs and retries.

### 22.8 Center finale

1. One hidden candidate during suction binds; two hidden candidates remain unresolved.
2. Reappearance eliminates an ambiguous candidate.
3. Unique post-release visual overlap can resolve ambiguity and commit the first cover.
4. First cover makes center green and persists the physical tag’s position.
5. Reusing the same tag cannot win.
6. A different tag that covers both the hidden center and the first hidden tag wins, makes center red at 50%, stops the timer, and ends the run.

### 22.9 Resume and diagnostics

1. Reload during an outer game preserves elapsed time and recovers red/center-linked targets.
2. Reload during a center stage reapplies its visual and first-tag state.
3. Diagnostic GET returns only the latest 1000 in-memory events while the JSONL download contains the full run.
4. Run files survive server restart, while coarse active state correctly returns to fresh setup unless another persistence layer is deliberately added.

## 23. Minimum implementation deliverables

A replacement implementation is complete only when it includes:

- the responsive operator page and exact controls;
- corrected video with a frame-current marker overlay;
- sequence editor and local-storage migration;
- four per-tag flows with per-team arm arbitration;
- TPS and parallax calibration handling;
- Atom impact and pump-edge handling;
- camera-owned placement attribution, release-pose diagnostics, and visual safety gates;
- exact 13-area board installation and playfield effects;
- full outer-to-center-to-win lifecycle;
- latency mode;
- coarse state and SSE routes;
- relay and playfield adapters;
- durable normalized JSONL run logging;
- Green and Purple LTX intent correlation; and
- reload recovery and the acceptance scenarios above.
