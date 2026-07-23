# Tag Invaders

Cooperative endless-loop ArUco placement game for the gamemaster hub.

- The blue and green hands follow independent repeating target routes while a count-up clock measures total play time.
- The current opening layout has a non-glowing Purple start pad at x 10.2, size-2 Purple target at x 2.5, and size-2.3 Green target at x -2.5.
- Blue target 22 is at (-10, 0.02, -1.2); Green target 23 is at (-10, 0.1, 1.2). Both use size 2.
- Each hand must pick up its tag (camera occlusion), place it, and produce a new Atom accelerometer drop event. The clock pauses while the robot hand moves clear for verification.
- Verification succeeds only when the physical tag is visible at the cached target position and the target ArUco is hidden. If both markers are visible, that hand retries and the clock continues.
- Blue tag 102 loops between targets 20 and 22. Green tag 103 loops between targets 21 and 23.
- A verified target turns red. On the hand's next successful target, the previous target returns to its team color and the new target turns red.
- The game has no automatic win or final phase; both routes continue independently until the gamemaster resets the game.
- An on-screen activity log records state changes and describes the next trigger the game is awaiting.
- Separate blue-hand and green-hand guidance shows the next required level-one action.

The opening playfield reproduces the currently saved Playfield Areas arrangement and uses a top-down camera with FOV 12. Area sizes stay fixed while only target glow intensity changes during gameplay. The tab uses the Webcam prototype for tracking, the Playfield Areas prototype for the external display, and the Auto Pick and Place calibration API for readiness information.
