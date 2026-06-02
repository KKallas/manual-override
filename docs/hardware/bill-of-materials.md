# Bill of Materials

Parts for one **Manual Override** station. Quantities are per station. Specific
models and prices are left blank to fill in for your region/supplier.

## Core

| # | Item | Qty | Notes |
|---|------|-----|-------|
| 1 | Dobot MG400 robot arm | 2 | One per player; TCP/IP control |
| 2 | 27" monitor | 1 | Playfield; flat-mount capable |
| 3 | Overhead camera | 1 | Wide FOV, good frame rate for ArUco |
| 4 | Camera boom / mount | 1 | Holds camera looking straight down |
| 5 | Compute machine | 1 | Runs server, vision, render, ref GUI |

## Nodes & trays

| # | Item | Qty | Notes |
|---|------|-----|-------|
| 6 | Node body (printed) | 6 | 3 per robot; holds ArUco face + tip |
| 7 | ArUco marker face | 6 | Printed, or small e-ink/display per node |
| 8 | (Optional) tiny display/e-ink | 6 | If showing live ID + countdown on node |
| 9 | Tool tray (printed), 3 slots | 2 | One per robot |
| 10 | Node end-effector / holder | 2 | Mounts to each MG400 to grip a node |

## Frame, safety, lighting

| # | Item | Qty | Notes |
|---|------|-----|-------|
| 11 | Rigid frame / baseboard | 1 | Fixes robots + screen + camera positions |
| 12 | E-stop button (hardware) | 1 | Cuts robot motion |
| 13 | Diffuse lighting | 1+ | Even light, no screen glare |
| 14 | Screen protector sheet | 1 | Protects monitor from node tips |
| 15 | Work-zone markers | — | Visual boundary for hands-clear rule |

## Cabling & misc

| # | Item | Qty | Notes |
|---|------|-----|-------|
| 16 | Network switch / router | 1 | Robots ↔ server over TCP/IP |
| 17 | Ethernet cables | 2+ | One per robot + uplink |
| 18 | USB cable for camera | 1 | If USB camera |
| 19 | Power strips / supplies | — | For robots, screen, camera, compute |

## Consumables / calibration

| # | Item | Qty | Notes |
|---|------|-----|-------|
| 20 | Camera calibration board | 1 | Checkerboard/ChArUco for intrinsics |
| 21 | Printed ArUco set | 1 | Marker dictionary used by the game |

> Fill in exact models, links, and costs for your build. As CAD/printable parts
> are finalized, link them from [HARDWARE.md](HARDWARE.md).
