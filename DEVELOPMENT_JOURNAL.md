# HHH Gamemaster development journal

Last reconstructed: 2026-08-19, through commit `81f19fd` on branch `Kaur3`.

This journal explains how the repository reached its current design: what changed, what was learned, why the next decision followed, and whether the repository records the work as human-only or AI-assisted. The reconstruction is based on commit history and messages, the current README and specifications, implementation diffs, calibration files, and local Laser Tag X run and score logs.

The specifications remain the authority for **what the software must do now**. This journal records **why it came to do that**. If historical reasoning and a current specification disagree, follow the specification and add a new journal entry explaining the change.

## Attribution and evidence rules

Git identifies Kaspar Kallas as the author of every commit in the available history and as the merger of both recorded pull requests. Ten development commits also contain an explicit `Co-Authored-By` trailer for an Anthropic Claude model. This journal therefore uses these labels:

- **Human decision; AI-assisted implementation** — Kaspar authored or accepted the commit and the commit explicitly credits an AI co-author. The record supports AI participation, but not a claim that the AI had final decision authority.
- **Human-recorded decision; AI involvement not evidenced** — Kaspar authored the commit and there is no AI attribution in the commit. This does not prove that no AI was used; it means the repository does not say so.
- **Inference** — the reason or lesson is reconstructed from the order and content of changes rather than stated in a commit message. Inferences are called out instead of being presented as remembered fact.

No commit in the available history supports an **AI-only decision**. Final repository acceptance was human in every recorded case.

Other human participants appear in gameplay score logs, but the repository does not identify them as design decision owners. Their influence must not be invented retroactively.

## Where we are now

HHH Gamemaster is a single Python/Flask hub serving three password-gated sandboxes: `gamemaster`, `green`, and `purple`. Machines remain modular prototypes, but authentication, discovery, routing, setup import/export, and cross-sandbox access live in the shared hub.

During normal play, the gamemaster relay exclusively owns two Dobot MG400 arms. Each team receives a separately leased, server-enforced side; a watchdog stops an abandoned arm. Because both robots retain the same factory IP, each arm's sockets are pinned to a dedicated USB Ethernet interface.

The physical-game stack combines corrected ArUco camera observations, camera-to-robot calibration, relay pose and pump state, Atom impact sensors, shared playfield rendering, and per-team automation. Laser Tag X is the current culmination: a two-arm cooperative placement game with a browser-side high-frequency fusion state machine, coarse server state, append-only diagnostics and scores, recovery from visible board state, and operator escape hatches.

Its most important present rule is that the **stable corrected-camera result is board truth**. A robot operation says what was intended and supplies transaction receipts; it helps attribute or diagnose the result, but it cannot replace or overrule a uniquely observed physical placement.

## Chronological decision record

### 2026-06-10 — Start from a modular game-host baseline

**Evidence:** `2e64247` introduced a vendored Manual Override hub plus three machines: the MG400 relay, webcam/ArUco tracking, and playfield areas.

**Lesson available at the time:** the existing hub already supplied machine discovery and a UI shell, so the fastest route to a working physical-game host was to reuse it and keep hardware capabilities in separate machines.

**Decision and why:** vendor the hub and make the relay the sole robot owner, with camera and playfield as peer services. This established the modular boundary still visible today.

**Owner:** Human decision by Kaspar; AI-assisted implementation explicitly credited to Claude Opus 4.8.

### 2026-06-11 — Add takeover, two-arm concurrency, and a first deployment topology

**Evidence:** `970eb18` added direct joint and Cartesian manipulators for operator takeover; `c99a4b6` launched gamemaster and two player sandboxes as three processes; `2f027f8` made one relay own both arms concurrently. `1fa2861` added the calibrated tag-game flow and Atom/WT32 firmware.

**Lessons:** one connection owner per arm is a real hardware constraint, and unattended remote control needs a safe handoff. Two teams also need independent arm state rather than a single global controller. The calibrated tag flow showed that gameplay had to span firmware, vision, playfield, and robot services.

**Decisions and why:**

- keep normal play behind the relay, but let the operator disable it and take over directly;
- give each relay side its own state and allow the two sides to run concurrently;
- initially run the three roles as separate processes so the complete system could be exercised quickly.

The three-process launcher was a useful stepping stone, not the final architecture; it was superseded the next day.

**Owner:** Human decisions by Kaspar. The takeover, launcher, and relay work explicitly credit Claude Opus 4.8; the calibrated tag-game commit has no recorded AI attribution.

### 2026-06-12 — Replace three hosts with one authenticated hub

**Evidence:** `486573a` and `93260e3`, merged by `5c86e5b`, folded the gamemaster and player repositories into `sandboxes/<name>/prototypes/`. They added per-sandbox passwords, roles, cross-sandbox service tokens, setup ZIP import/export, a landing page, and sandbox-aware discovery. Stale upstream-upgrade machinery was removed.

**Lesson:** the three-process/repository split duplicated configuration and deployment work, while team isolation still needed to be enforced centrally. A physical event setup benefits more from one process, one port, and portable sandbox bundles than from independently administered clones.

**Decision and why:** use one hub as the deployment and security boundary, but preserve three logical sandboxes. Team code may call shared services only through explicit doors, and the relay must still enforce `side == authenticated team` rather than trusting the browser.

**Owner:** Human decision and merge by Kaspar; AI-assisted implementation explicitly credited to Claude Fable 5.

### 2026-06-12 — Solve identical robot addressing at the socket layer

**Evidence:** `a2e4b00`, merged by `e02c347`, records that both MG400s had to keep factory IP `192.168.1.6`. `4ebaef5` then derived controller identity from the authenticated sandbox, locked players to relay-only control of their own color, removed the now-redundant Game Link machine, and kept the player controller copies identical.

**Lesson:** ordinary routing cannot distinguish two devices with the same destination IP. Separately, asking players to re-enter team and host data creates avoidable mismatch and security risk when both values are already known by the server.

**Decision and why:** pin every robot socket to the USB Ethernet interface that owns a fixed local IP (`.50` purple, `.51` green), auto-detect the interface name, and fail with a setup-specific error if it is absent. Derive side and relay host from trusted hub context.

**Owner:** Human decisions by Kaspar; AI-assisted implementation explicitly credited to Claude Fable 5.

### 2026-06-13 to 2026-06-18 — Grow from manual play to automated pick-and-place

**Evidence:** `5042744` added the two-player tag game; `5664549` added team-specific webcam rotation; `0b3e8c0` and `df67311` introduced pickup controllers and a side-by-side video/control view; `f929a5a` and `ccc5c17` added and expanded automatic pickup; `abc47cd` added relay Z-floor and state handling; `c14ae88` revised calibration field mapping; `867cb32` added Auto PP X; `6466987` required player-name confirmation.

**Lessons:**

- manual motion controls are necessary for commissioning but are not a game workflow;
- camera presentation may differ per player, while robot identity and calibration must remain stable;
- automation needs explicit calibration, Z constraints, state feedback, and a visible relationship between camera and controls;
- participant identity must be confirmed before an autonomous run if scores and logs are to remain meaningful.

**Decision and why:** build automation incrementally on top of the same relay and sensing contracts instead of creating a second robot-control path. Keep safety and ownership server-side while iterating player workflow in the sandbox UIs.

**Owner:** Human-recorded decisions by Kaspar. The June 15 pickup and layout commits explicitly credit Claude Opus 4.8; the other commits in this period contain no AI attribution.

### 2026-07-23 — Integrate the Kaur3 physical build

**Evidence:** `647324d` updated Atom firmware and management, relay diagnostics and Z limits, auto-pickup calibration and logs, and added Tag Invaders.

**Lesson:** physical-game development was producing useful state outside the original narrow game flow: device health, impact data, command history, calibration values, and score/run records all needed operator visibility.

**Decision and why:** bring that working rig state back into one branch and make hardware diagnostics part of the operator system rather than an external debugging exercise.

**Owner:** Human-recorded decision by Kaspar; no AI attribution is present. The commit message says only “Upload current codebase,” so the detailed rationale above is an inference from the files changed.

### 2026-08-05 — Establish a corrected camera and second-generation calibration path

**Evidence:** `7291f9d` added chessboard camera calibration, corrected streams and coordinates, native focus lock, Auto PP Cal 2 data and UI, Atom testing, persistent relay command monitoring, and the first Laser Tag prototype.

**Lesson:** raw camera coordinates and autofocus behavior are not stable enough for repeatable robot-to-board placement. Calibration must cover the lens field, all consumers must share the same corrected frame, and the rig needs a way to inspect commands when physical behavior differs from intent.

**Decision and why:** share the webcam capture instead of opening competing camera handles; make one correction map feed preview, video, and tag coordinates; add a denser camera/arm calibration path; retain command evidence; build Laser Tag on those services.

**Owner:** Human-recorded decision by Kaspar; no AI attribution is present. The stated lesson is partly inferred from the calibration design and the later corrections it enabled.

### 2026-08-09 — Make behavior reconstructable and runs diagnosable

**Evidence:** `3b62e4f` created the first 1,419-line Laser Tag reconstruction specification, expanded Laser Tag and green-side automation integration, and extended camera and relay diagnostics.

**Lesson:** once game acceptance depends on timing and evidence from several asynchronous systems, the code alone is a poor account of the intended behavior. Comparing player automation with gamemaster decisions requires shared operation IDs and durable events.

**Decision and why:** document compatibility behavior as a reconstruction specification and record cross-system diagnostic events so an observed activation can be compared with the operation that intended it.

**Owner:** Human-recorded decision by Kaspar; no AI attribution is present.

### 2026-08-14 — Fork Laser Tag X as the integrated game

**Evidence:** `1b249f2` introduced `laser-tag-x`, a 1,462-line reconstruction specification, per-team LTX endpoints, motion settings, scoring, diagnostic run logs, and coordinated updates to Auto Pickup, calibration, relay, and player controllers.

**Lesson:** the original Laser Tag tab had become a systems-integration surface, not just a game screen. A high-frequency sensor-fusion loop, durable diagnostics, team automation, scoring, recovery, and operator controls needed an explicit contract.

**Decision and why:** create Laser Tag X as a separately specified integration. Keep high-frequency per-tag fusion in the browser for rapid iteration, persist only coarse phase/finale state on the server, and append detailed diagnostic events to JSONL. Map the game's blue presentation to the relay's purple side at one adapter boundary.

**Owner:** Human-recorded decision by Kaspar; no AI attribution is present. The reason for the browser/server split is reconstructed from the specification's stated boundaries.

### 2026-08-17 — Add operator completion paths and meaningful scoring feedback

**Evidence:** `7f730c5` added manual activation for outer targets; `99cef90` extended it to the two center covers and made that state recoverable; `366d54a` added scoreboard rank/gap feedback and safer late player-name capture. Physical Laser Tag X run and score logs begin before this date and continue through it.

**Lesson:** a live physical game must remain finishable when a sensor, calibration, or automated sequence fails. A manual action must use the same state transition and visual effects as an automatic action, or recovery creates a second, inconsistent game. Scores also need immutable start-time context but a narrow fallback for registration that finishes late.

**Decision and why:** give the gamemaster explicit outer and center placement controls, persist a manual first-center sentinel instead of inventing a tag ID, and route manual completion through normal win/scoring behavior. Make the score table explain place and time gap while preserving append-only history.

**Owner:** Human-recorded decisions by Kaspar; no AI attribution is present.

### 2026-08-18 — Persist board progress and tighten motion/calibration semantics

**Evidence:** `d2f4fa4` persisted `activated_targets`, recovered visible progress after reload, added player distance meters, and introduced horizontal/Z drop windows. `801a993` reduced target-drop Z arrival tolerance from 40 mm to 1 mm and applied the 31.5 mm height offset only when stacking on a physical tag.

**Lessons:**

- coarse phase alone cannot reconstruct an in-progress board after a browser reload;
- a single broad distance check can report arrival while the robot is still descending;
- a flat board target and a physical tag used as a stacking target require different Z geometry;
- operators and players need the same calibration error made visible, not just a silent accept/reject result.

**Decision and why:** make activated marker IDs part of coarse shared state; recover the rest from the live playfield; separate horizontal and vertical acceptance; require near-exact final Z before release; add tag height only for tag-on-tag placement; expose the measured error in the LTX player HUD.

**Owner:** Human-recorded decisions by Kaspar; no AI attribution is present.

### 2026-08-19 — Move from command truth to camera-observed board truth

**Evidence:**

- `ab81919` changed outer verification from release-pose target selection to sustained, unique, parallax-corrected visual overlap. Release pose became diagnostic, missing pose ceased to invalidate coherent camera evidence, and placement IDs separated a physical placement from an optional automation operation.
- `659441d` updated the measured camera and robot calibration data.
- `6ccd3b2` made a video click a safe temporary motion override: finish atomic pump work, lift, move, then retry the interrupted cue without changing queue index or operation identity.
- `81f19fd` made stable camera evidence authoritative even when operation receipts are late or ambiguous. It added typed destinations, post-observation operation attribution, an “unattributed board truth” state, and divergence only when one uniquely correlated released operation actually contradicts the observed result.

**Lessons:**

- robot pose and queued destination describe intended motion, not proof of where the tag ended up;
- camera occlusion is useful provisional evidence but is not enough by itself;
- a physical result needs multi-frame voting, jitter limits, uniqueness margins, and parallax correction;
- asynchronous receipts can arrive after the automation queue advances, so “newest active operation” is not a safe proxy for the operation that caused the visible result;
- an operator correction during a cue should not destroy transaction identity or silently skip a queue item.

**Decision and why:** let the corrected camera decide the physical board only after stable geometric confirmation. Correlate completed, receipt-valid operations afterward; report matched, late-matched, unattributed, or true divergence explicitly. Preserve an observed result when attribution is ambiguous, and never compare it with an operation that has merely started. Keep runtime overrides transactional and return to the interrupted operation.

**Owner:** Human-recorded decisions by Kaspar; no AI attribution is present.

### 2026-08-19 — Separate current requirements from development reasoning

**Evidence:** the current human request asked for a development journal covering decisions, reasons, lessons, timing, and human/AI ownership, plus a note from the specification to the journal. This entry and the specification link are pending worktree changes rather than part of the earlier commit history.

**Lesson:** the reconstruction specification is detailed about current behavior but intentionally does not explain the sequence of discoveries and reversals that produced it. Commit author fields also do not, by themselves, distinguish human decision authority from AI implementation help.

**Decision and why:** keep the Laser Tag X specification focused on current normative behavior, create this repository-level journal for rationale and attribution, and link the two documents with an explicit precedence rule.

**Owner:** Human-requested decision. OpenAI Codex performed the evidence review and drafted the journal and cross-reference; final review and acceptance remain with the human.

### 2026-08-19 — Put Game 2 Cartesian control in the camera context

**Evidence:** the human requested a crane-themed overlay at the bottom of the LTX player video for Game 2 and supplied a versioned manifest, empty triangular frame, and three transparent neon handle images. The requested mapping was left handle to Z, right handle to forward/back, and bottom handle to left/right.

**Lesson:** the player makes Cartesian adjustments by watching the camera, so the most relevant controls can live in that visual context. The existing native X/Y/Z sliders and their input handler already feed the established queue, relay ownership, limits, and safety behavior; bypassing them would create an unnecessary second robot-control path.

**Decision and why:** show a responsive triangular overlay only in `game_2`, alpha-extract the frame so black pixels are genuinely transparent, and make the supplied handles draggable along their pictured rails. Map left to Z down/up, right to X back/forward, and bottom to Y right/left. Treat the handles as synchronized presentations of the native sliders, disable them when Cartesian control is disconnected without recoloring the supplied artwork, and include keyboard slider behavior. Place the game instructions below the video instead of stacking them over the camera. This gives Game 2 the requested direct manipulation without changing the robot-control contract.

**Verification:** both Green and Purple asset routes returned the PNG assets; the inline JavaScript and both LTX Python entry points passed syntax checks; projection endpoint tests covered all three rails; and browser inspection confirmed Game 2-only visibility, loaded assets, responsive landscape and portrait layouts, a true-alpha frame, instructions below the video, separated apex labels, reference-colored levers, accessibility roles, and no console errors. No live robot was connected or moved during UI verification.

**Owner:** The feature, visual assets, game-mode restriction, and axis mapping are human decisions from the request. OpenAI Codex chose the reuse-and-synchronization implementation, added the code and documentation, and performed non-hardware verification. Final acceptance remains with the human.

### 2026-08-19 — Share both arm positions in each LTX camera view

**Evidence:** the human requested an `Arm` checkbox in both player LTX windows. When enabled, each player must see where their own arm and the other player's arm are moving, represented as straight lines of the same red squares used for the unsafe base-center area. The request identified the gamemaster Laser Tag X arm movement information as the tracking source.

**Lesson:** collision awareness is a shared visual problem even though robot ownership remains team-specific. The gamemaster already reads both relay arm poses, and Auto PP Cal 2 already contains a separate robot-to-camera model for each arm. Reusing those contracts is more reliable than inferring an arm from camera occlusion or creating a second motion channel.

**Decision and why:** expose a role-checked, read-only player snapshot containing only the two arms' compact live relay fields and never the relay command log. Poll it only while the player's `Arm` checkbox is enabled. Project each pose through its owning arm's calibration into the viewing player's current camera orientation, then render a clipped base-to-TCP line of red squares with explicit `YOUR ARM` and `OTHER ARM` endpoint labels. Hide disconnected, invalid, or stale positions. This supplies cross-arm awareness without changing leases, motion commands, or safety authority.

**Verification:** Python and inline JavaScript syntax checks pass; authenticated Green and Purple requests return the same canonical Green/Purple compact snapshot while the gamemaster route retains its Green/Blue naming adapter; mathematical tests cover line clipping, square-line generation, and both calibration projections; and browser inspection confirms the checkbox layout in both player views, toggle behavior, and endpoint polling only while checked. With both local relay arms disconnected, the fail-closed behavior was verified; no live robot was moved for this UI test.

**Owner:** The checkbox, two-arm visibility, red-square form, and gamemaster tracking source are human decisions from the request. OpenAI Codex chose the read-only snapshot boundary, per-arm projection, stale-data handling, labels, and implementation. Final acceptance remains with the human.

## Lessons retained in the current design

1. **Prototype topology is disposable; security boundaries are not.** The three-process launcher was replaced within a day, but role isolation and relay-side enforcement survived in the single hub.
2. **Derive identity from trusted context.** Team and local service location come from the authenticated sandbox, reducing both setup burden and spoofable inputs.
3. **One component owns each actuator.** The relay arbitrates all normal robot access; leases, stale-token rejection, watchdog holds, workspace clamps, and hardware E-stops define the safety envelope.
4. **Presentation transforms must not alter calibration math.** A player's rotated video is a UI concern. Calibration and live detections stay in one raw corrected-camera coordinate frame.
5. **Intent is not outcome.** Motion commands, release poses, and queue destinations are evidence about intent. Sustained physical observation is the final board authority.
6. **Do not turn missing attribution into false divergence.** Late or ambiguous telemetry yields “board truth unattributed”; divergence is reserved for a unique, evidence-backed contradiction.
7. **Recovery paths must share normal transitions.** Manual placement, reload recovery, and late operation replay reuse normal activation, center, scoring, and run-ending behavior.
8. **Preserve evidence before polishing it.** Command monitoring, JSONL run logs, append-only score logs, operation IDs, and typed destinations make later corrections possible.
9. **A live operator needs escape hatches.** Direct takeover, pause/resume, stop, runtime click override, manual placement, and score-table reset each solve a different operational failure without erasing durable history.
10. **Write “what” and “why” separately.** Reconstruction specifications define current compatibility behavior. This journal records chronology, reversals, evidence, and rationale.

## Human and AI contribution record

The following commits explicitly record AI co-authorship:

| Commit | Recorded work | Human authority | Recorded AI contribution |
|---|---|---|---|
| `2e64247` | initial gamemaster host | Kaspar Kallas | Claude Opus 4.8 |
| `970eb18` | operator takeover controls | Kaspar Kallas | Claude Opus 4.8 |
| `c99a4b6` | three-process launcher | Kaspar Kallas | Claude Opus 4.8 |
| `2f027f8` | two-arm relay and controllers | Kaspar Kallas | Claude Opus 4.8 |
| `486573a`, `93260e3` | single authenticated hub | Kaspar Kallas | Claude Fable 5 |
| `a2e4b00` | dual-interface robot routing | Kaspar Kallas | Claude Fable 5 |
| `4ebaef5` | server-derived controller identity and UX | Kaspar Kallas | Claude Fable 5 |
| `0b3e8c0` | pickup prototypes and relay/controller updates | Kaspar Kallas | Claude Opus 4.8 |
| `df67311` | side-by-side pickup UI | Kaspar Kallas | Claude Opus 4.8 |

All later commits, including every Laser Tag X commit, are authored by Kaspar without an AI trailer. Their correct label is **human-recorded; AI involvement not evidenced**, not “definitely human-only.”

## Evidence of physical iteration

As of this reconstruction, the local working setup contains 89 Laser Tag X JSONL runs from August 9–19, totaling 21,968 events, and 26 appended win records. These runtime artifacts show repeated real-system iteration rather than a purely paper design. Their event mix also exposes the problems that drove the final changes: repeated release-pending/unresolved center transactions, activation blocks, late attribution, operation failures, reload recovery, and finally camera-authoritative decisions with matched operation attribution.

The repository does not contain a conventional automated test suite. The specifications' acceptance scenarios, runtime diagnostics, score/run logs, and physical trials currently carry most of the verification burden. This is a limitation, not a recommendation: deterministic tests should be added around state patches, recovery, event correlation, scoring, coordinate transforms, and multi-frame voting where hardware can be simulated.

## Known trade-offs and unfinished edges

- The browser owns high-frequency fusion state. Reload recovery can reconstruct activated targets and finale state, but not every per-tag sequence index, frozen pose, or historical binding.
- Server game state and the current diagnostic run are process memory. JSONL files are durable evidence, but a server restart does not automatically resume the previous run.
- Manual completion keeps an event running but intentionally weakens the claim that every transition was physically sensed; the diagnostic record must retain that distinction.
- Calibration is installation-specific data. Updating it is a real system change and should be paired with the physical setup and a dated journal entry.
- The current camera-authoritative rule depends on camera quality. It fails closed when corrected tracking or a valid spatial model is unavailable, and the gamemaster remains the recovery authority.

## How to record the next decision

Append a dated entry containing:

1. the observation, failure, user need, or measurement that triggered work;
2. the options considered and the chosen trade-off;
3. the decision and affected contracts;
4. verification performed, including run IDs when relevant;
5. the human decision owner;
6. AI participation exactly as recorded, without guessing; and
7. the commit or pull request that implemented the change.

If the decision changes required behavior, update the relevant specification in the same change. If it only explains existing behavior, update this journal without rewriting the specification as history.
