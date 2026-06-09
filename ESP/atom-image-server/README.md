# atom-image-server

Turns an [M5Stack AtomS3R](https://docs.m5stack.com/en/core/AtomS3R) into a
network image frame: it serves a web UI and displays whatever 128×128 image you
push to it as raw **RGB565** bitmap data.

- **MCU/display:** ESP32-S3, 0.85" 128×128 IPS LCD
- **Framework:** Arduino + [M5Unified](https://github.com/m5stack/M5Unified) (M5GFX)
- **Build:** [PlatformIO](https://platformio.org/)

## How it works

1. On boot the device starts a WiFi **SoftAP** (or auto-joins a saved network —
   see *Serial console* below) and prints the SSID / IP on the LCD.
2. Connect to that network and open the device IP (`http://192.168.4.1/` in AP
   mode, or the DHCP address in client mode).
3. The **ATOM FRAMER** page loads an image, lets you pan/zoom into the lime
   128×128 device frame, and shows an exact device preview.
4. **SEND → DEVICE** packs the framed pixels to RGB565 (big-endian, 32768 bytes)
   and POSTs them to `/frame`; the firmware `pushImage()`s them to the panel.

The last frame is **mirrored to flash** (LittleFS), so a reboot or power-cycle
comes back up showing the same image instead of the status screen. Tap the
screen-button to recall the WiFi/IP status; it returns to the frame on the next
push or reboot.

## Serial console (client mode)

Open the serial monitor at **115200** (`pio device monitor`) and type:

| command                    | effect                                                    |
|----------------------------|-----------------------------------------------------------|
| `wifi <ssid>:<password>`   | join that network via DHCP; credentials are **saved** and re-joined on boot |
| `ip`                       | print the current IP (and `http://…/` URL)                |
| `status`                   | mode / ssid / ip / rssi                                   |
| `ap`                       | forget the saved network and start the SoftAP             |
| `help`                     | list commands                                             |

A bare `ssid:password` line works too. The split is on the **first** colon, so
the password may contain colons (the SSID may not). On a failed join the device
falls back to the SoftAP after ~20 s.

### `tools/atomctl.py`

A small host helper that drives the console for you (needs `pip install pyserial`;
close `pio device monitor` first — only one program can own the port):

```sh
tools/atomctl.py client MyWiFi "my password"   # join (STA), wait for the DHCP IP
tools/atomctl.py ip                             # ask for the current IP
tools/atomctl.py master                         # back to SoftAP
tools/atomctl.py status
```

The port is auto-detected (ESP32-S3 native USB); override with `--port`.

You can also `EXPORT .DAT` (raw 32768-byte RGB565) or `SAVE PNG + DATA` (a normal
PNG with the raw RGB565 tucked into a private `daTa` chunk) for archival, and load
either back in later.

## Endpoints

| Method | Path     | Body                                   | Effect                          |
|--------|----------|----------------------------------------|---------------------------------|
| GET    | `/`      | —                                      | serves the framing UI           |
| GET    | `/state` | —                                      | JSON: marker id, frame + battery state |
| POST   | `/frame` | multipart file `frame`, 32768 B RGB565 | draws the frame, saves it to flash |

`/frame` uses the core `WebServer` multipart **upload** handler, which is
binary-safe and needs no async-web dependency. The page sends the bytes as a
`FormData` blob. An optional `?mid=<n>` query records which ArUco marker the
frame is, so `/state` can report it back (the atom-manager host sends this).

`/state` returns the device's current state for polling clients:

```json
{ "markerId": 3, "hasFrame": true, "battery": { "mv": 4050, "pct": 83 } }
```

- `markerId` — ArUco id of the displayed frame, or `-1` if unknown.
- `hasFrame` — whether a full frame is stored/displayed.
- `battery` — pack voltage in millivolts and a rough 0–100 % (1S LiPo,
  3.3 V → 0 %, 4.2 V → 100 %), read from the GPIO 8 ADC (2:1 divider).

## Build & flash

```sh
cd ESP/atom-image-server
pio run                 # build
pio run -t upload       # flash over USB-C
pio device monitor      # serial @ 115200
```

The AtomS3R has no dedicated PlatformIO board id; we target `m5stack-atoms3`
(same ESP32-S3 / 8 MB flash) and M5Unified auto-detects the actual panel at
runtime.

## Config

Edit the top of [`src/main.cpp`](src/main.cpp):

```cpp
static const char* AP_SSID = "AtomFramer";
static const char* AP_PASS = "atomframer";   // >= 8 chars, or "" for an open AP
```

To join an existing network instead of hosting an AP, swap the `WiFi.mode/softAP`
calls in `setup()` for `WiFi.begin(ssid, pass)` and report `WiFi.localIP()`.

## Notes

- RGB565 is packed **big-endian** by the page; the firmware calls
  `M5.Display.setSwapBytes(true)` before `pushImage`. If colors look swapped on a
  different panel, flip that flag.
- Tap the screen-button to recall the AP info on the LCD.
