# Focus Lock

Native macOS focus controller used by the Gamemaster camera-calibration tab.

## Build

Requires macOS 14 or later and Xcode Command Line Tools.

```sh
chmod +x build-app.sh
./build-app.sh
```

The hub keeps Focus Lock running while the webcam is selected. This prevents
other applications from silently re-enabling autofocus.

## macOS compatibility

AVFoundation identifies the selected camera while IOKit resolves its matching
USB device and UVC VideoControl interface. Focus Lock sends the standard
Camera Terminal controls directly without claiming or opening that interface:

- `CT_FOCUS_AUTO_CONTROL` (`0x08`)
- `CT_FOCUS_ABSOLUTE_CONTROL` (`0x06`)
- `CT_AE_MODE_CONTROL` (`0x02`)
- `CT_EXPOSURE_TIME_ABSOLUTE_CONTROL` (`0x04`)

The manual-focus slider uses the minimum, maximum, resolution, default, and
current values reported by the camera. For the tested Verbatim 49580/AWC-03,
the reported absolute-focus range is 0–110 with a step of 1.
The tested camera reports an absolute-exposure range of 1–80000 with a step
of 1; exposure values are expressed in standard UVC 100 µs units.
