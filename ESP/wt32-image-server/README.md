# wt32-image-server

A 4-slot, touch-navigable image **kiosk** on a **WT32-SC01 (V3.2)** (320×480
ST7796). It holds four full-screen images, switches between them over HTTP, and
overlays each with an invisible 2×2 grid of tappable hotspots that fire HTTP GETs
— so a fleet of these can drive each other's pages.

- **MCU/display:** classic ESP32 (WROVER), 3.5″ 320×480 ST7796 SPI, FT5x06 touch
- **Framework:** Arduino + [LovyanGFX](https://github.com/lovyan03/LovyanGFX)
- **Build:** [PlatformIO](https://platformio.org/)

Images are native full-screen 320×480 RGB565 (no upscaling). The 4 slots live in
LittleFS (raw, ~300 KB each — a no-OTA [`partitions.csv`](partitions.csv) gives
the data partition room); one 300 KB PSRAM buffer holds the current slot.

> **Panel note:** the linked reference and this config target a **320×480 ST7796**
> board (3.5″). If your unit is a different panel (e.g. an 800×480 5″ RGB),
> change `lgfx::Panel_*`, `PANEL_W`/`PANEL_H` and the pins in
> [`include/lgfx_wt32.h`](include/lgfx_wt32.h); nothing else needs to move.

## How it works

1. On boot the device restores the last-shown slot and displays it (or shows a
   WiFi **SoftAP** status screen if no slot is filled yet).
2. Connect and open the device IP (`http://192.168.4.1/` in AP mode, or the DHCP
   address in client mode).
3. In the **WT32 FRAMER** page: pick a **slot (1–4)**, frame a 320×480 region,
   **SEND → SLOT** (stores + shows it). Edit that slot's **2×2 hotspot URLs** and
   **SAVE HOTSPOTS**.
4. **Tapping a quadrant** on the device fires that cell's URL: a leading `/`
   targets this device (change its own page), an absolute `http://other/…`
   targets another unit.

## Hotspots

Each slot has a 2×2 grid of touch cells (top-left, top-right, bottom-left,
bottom-right). A cell's value decides what a tap does:

- **`1`–`4`** — show that slot **on this device** (1-based). The simplest case.
- **`red_oak:2`** — show slot 2 on the fleet device **named** `red_oak` (see *Naming*).
- `/show?slot=1` — also this device (0-based), handled locally.
- `http://192.168.1.50/show?slot=0` — flips **another** unit to its slot 1 (explicit IP).

Self actions (a bare number or `/show?…`) are handled **locally** — no HTTP call
to ourselves (the single-threaded web server would deadlock on a self-request);
only cross-device `http://…` targets go out over the network. Empty cells do
nothing. Cells that share the **same** value flash together on tap, so two (or
more) quadrants pointing at one endpoint read as a single larger button. URLs are
stored as 16 newline-separated lines (`slot*4 + cell`) in `/buttons.txt`.

The framer page reads the stored bitmap back (`GET /frame?slot=N`) and shows it
in the preview when you open/refresh it or switch slots, so you always see what
the device actually holds.

## Endpoints

| Method | Path            | Body / query                            | Effect                                   |
|--------|-----------------|-----------------------------------------|------------------------------------------|
| GET    | `/`             | —                                       | serves the framer UI                     |
| GET    | `/state`        | —                                       | JSON: current slot, filled[], battery    |
| POST   | `/frame`        | `?slot=N`, multipart `frame` 307200 B   | store RGB565 into slot N, show it         |
| GET    | `/frame`        | `?slot=N`                               | stream slot N's raw RGB565 back (read-back)|
| GET    | `/show`         | `?slot=N`                               | display stored slot N (the hotspot target)|
| GET    | `/buttons`      | —                                       | the 16-line hotspot URL table            |
| POST   | `/buttons`      | text body, 16 lines                     | replace the hotspot URL table            |
| GET    | `/peers`        | —                                       | JSON list of discovered fleet devices    |
| GET    | `/name`         | —                                       | this device's name                       |
| POST   | `/name`         | text body                               | rename this device                       |

## Naming & discovery

Each device picks a persistent random **`<colour>_<tree>`** name (e.g. `red_oak`)
on first boot and broadcasts `"<name> <ip>"` over **UDP port 50505** every ~8 s,
while listening to build a name→IP table of the fleet. So you can address peers
by **name** (`red_oak:2`) in a hotspot instead of a brittle IP — names are shown
on each device's status screen and in the framer page's **Network** list (click a
peer there to drop `name:1` into the focused field).

Because the table refreshes from live broadcasts (90 s TTL) and STA mode
auto-reconnects, **swapping the WiFi infrastructure** (same SSID, new router) just
works: each unit rejoins, re-announces its new IP, and `red_oak:2` keeps resolving
— back to normal in well under a minute. Rename via `POST /name`, the framer page,
or the serial `name <new>` command.

`/state` returns:

```json
{ "name": "red_oak", "slot": 0, "slots": 4, "filled": [true,false,false,false], "hasFrame": true, "battery": null }
```

`battery` is `null` — the WT32-SC01 is USB-powered with no battery divider by
default. If you wire a 2:1 divider to an ADC pin, set `BAT_ADC_PIN` in
[`src/main.cpp`](src/main.cpp) to enable the reading.

## Serial console (client mode)

Open the serial monitor at **115200** (`pio device monitor`) and type:

| command                    | effect                                                    |
|----------------------------|-----------------------------------------------------------|
| `wifi <ssid>:<password>`   | join that network via DHCP; credentials are **saved**     |
| `ip`                       | print the current IP                                      |
| `status`                   | mode / ssid / ip / rssi                                   |
| `ap`                       | forget the saved network and start the SoftAP             |
| `help`                     | list commands                                             |

## Build & flash

```sh
cd ESP/wt32-image-server
pio run                 # build
pio run -t upload       # flash over USB
pio device monitor      # serial @ 115200
```

## Config

SoftAP credentials and the (disabled) battery pin are at the top of
[`src/main.cpp`](src/main.cpp); the panel/touch/backlight wiring is in
[`include/lgfx_wt32.h`](include/lgfx_wt32.h).

## Status

Builds clean for `esp-wrover-kit` (`pio run`). **Not yet verified on hardware** —
the panel init, touch hotspot mapping and cross-device GETs want a real
WT32-SC01 to confirm.
