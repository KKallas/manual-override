# atom-image-server

Turns an [M5Stack AtomS3R](https://docs.m5stack.com/en/core/AtomS3R) into a
4-slot image **kiosk**: it holds four 128×128 RGB565 images, switches between
them over HTTP, and maps the single button's gestures to HTTP GET actions — the
AtomS3R sibling of [`wt32-image-server`](../wt32-image-server) (which uses a 2×2
touch grid instead of one button). Still speaks the atom-manager protocol.

- **MCU/display:** ESP32-S3, 0.85" 128×128 IPS LCD, one button
- **Framework:** Arduino + [M5Unified](https://github.com/m5stack/M5Unified) (M5GFX)
- **Build:** [PlatformIO](https://platformio.org/)

## How it works

1. On boot the device restores the last-shown slot and displays it (or starts a
   WiFi **SoftAP** + status screen if no slot is filled).
2. Connect and open the device IP (`http://192.168.4.1/` in AP mode, or the DHCP
   address in client mode).
3. In the **ATOM FRAMER** page: pick a **slot (1–4)**, frame an image,
   **SEND → SLOT** (stores + shows it). Edit that slot's gesture actions and
   **SAVE GESTURES**.
4. Slots persist in flash (LittleFS); the displayed slot survives reboot.

## Button gestures

The AtomS3R has one button. Per slot, three gestures map to HTTP GET actions:

| gesture       | timing                              |
|---------------|-------------------------------------|
| short click   | release **< 0.5 s**                 |
| long click    | held **> 0.7 s**                    |
| double click  | two short clicks within **1.5 s**   |

(A release between 0.5 s and 0.7 s is in neither band and is ignored. The single
short fires only after the 1.5 s double-click window passes, so it can become a
double instead.) Each action value is interpreted like the WT32 hotspots:

- **`1`–`4`** — show that slot **on this device** (handled locally).
- **`red_oak:2`** — show slot 2 on the fleet device **named** `red_oak` (see *Naming*).
- `/show?slot=1` — also this device (0-based).
- `http://192.168.1.50/show?slot=0` — flips **another** unit (explicit IP).

An **unconfigured long-press** falls back to showing WiFi/IP status on the LCD.
Actions are stored as 12 lines (`slot*3 + gesture`, gesture = short/long/double)
in `/buttons.txt`. The framer page reads the stored bitmap back
(`GET /frame?slot=N`) and shows it on open / slot switch.

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

| Method | Path       | Body / query                            | Effect                                   |
|--------|------------|-----------------------------------------|------------------------------------------|
| GET    | `/`        | —                                       | serves the framing UI                    |
| GET    | `/state`   | —                                       | JSON: slot, filled[], marker id, battery |
| POST   | `/frame`   | `?slot=N&mid=M`, multipart `frame` 32768 B | store into slot N, show it            |
| GET    | `/frame`   | `?slot=N`                               | stream slot N's raw RGB565 back (read-back)|
| GET    | `/show`    | `?slot=N`                               | display stored slot N (the gesture target)|
| GET    | `/buttons` | —                                       | the 12-line gesture-action table         |
| POST   | `/buttons` | text body, 12 lines                     | replace the gesture-action table         |
| GET    | `/peers`   | —                                       | JSON list of discovered fleet devices    |
| GET    | `/name`    | — / text body (POST)                    | get / set this device's name             |

## Naming & discovery

Each device picks a persistent random **`<colour>_<tree>`** name (e.g. `red_oak`)
on first boot and broadcasts `"<name> <ip>"` over **UDP port 50505** every ~8 s,
listening to build a fleet name→IP table. Address peers by **name** (`red_oak:2`)
in a gesture instead of an IP — the name shows on the LCD status screen and in the
framer page's **Network** list (click a peer to drop `name:1` into the focused
field). The table refreshes from live broadcasts (90 s TTL) and STA auto-reconnects,
so **swapping the WiFi infrastructure** (same SSID) recovers in under a minute.
Rename via `POST /name`, the framer page, or the serial `name <new>` command.
This is the same discovery module as [`wt32-image-server`](../wt32-image-server)
(`names_discovery.h`).

`/frame` uses the core `WebServer` multipart **upload** handler, which is
binary-safe and needs no async-web dependency. The page sends the bytes as a
`FormData` blob. `?slot=N` (default = current) picks the target slot; `?mid=<n>`
records the ArUco marker so `/state` can report it (the atom-manager host sends
this, with no `slot`, so it keeps pushing to the current slot).

`/state` returns the device's current state for polling clients:

```json
{ "name": "red_oak", "slot": 0, "slots": 4, "filled": [true,false,false,false], "markerId": 3, "hasFrame": true, "battery": { "mv": 4050, "pct": 83 } }
```

- `slot` / `filled` — the current slot and which of the 4 are stored.
- `markerId` — ArUco id of the current slot, or `-1` if unknown.
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

- RGB565 is packed **big-endian** by the page; the firmware types the source as
  `m5gfx::swap565_t` so M5GFX converts to the panel's native order.
- A **long-press with no action configured** shows the WiFi/IP status on the LCD.
- Gesture timings (`SHORT_MAX_MS` / `LONG_MIN_MS` / `DOUBLE_MS`) are constants at
  the top of [`src/main.cpp`](src/main.cpp).
