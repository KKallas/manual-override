# Camera Calibration

Gamemaster-only OpenCV chessboard calibration for the shared Webcam prototype.
The default is 15×9 **inner corners**, producing 16×10 cells. The target is
letterboxed as needed so those cells remain physically square on any display.
One scan automatically shifts a smaller checkerboard through nine screen
positions while keeping every cell square. These samples cover the lens field
without moving the fixed camera or display. Calculate the lens model after the
scan. The resulting camera matrix and distortion
coefficients are saved in `calibration.json`.
Corner samples automatically retry slightly inward when a physical screen edge
blocks one or two squares. Up to three positions may be skipped because the
remaining samples overlap; at least six detected patterns are required.

The controller launches `screen.html` as a clean browser window for the physical
display beneath the camera. It renders the exact configured chessboard
at the largest square-cell size the display can fit, supports fullscreen and
inversion, and contains no camera or operator UI.

The preview uses OpenCV `calibrateCamera`, `getOptimalNewCameraMatrix`, and
`undistort`. It deliberately shares Webcam's raw frame rather than opening a
second `VideoCapture`. The corrected output matrix is recentered so the raw
image's geometric centre remains at the same pixel after undistortion.
OpenCV's cached `initUndistortRectifyMap` result is the fixed remapping mesh used
by the preview, corrected stream, and tag-coordinate correction.
