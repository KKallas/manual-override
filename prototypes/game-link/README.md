# Game Link

The **player-side** machine for the Manual Override hub. A player picks their
**team** (purple / green) and the **game-master host** (the address of the hub
running `dobot-mg400-relay`), and this machine remembers it and shows the live
relay connection status. Mounted at `/p/game-link`.

It never owns the arm and never sends move/pump commands — it only READS the
relay's published state. It is the player's counterpart to the relay's operator
GUI: choose your side once, then watch whether the game-master is reachable, the
arm is connected/enabled, who holds the floor, and whether **you** hold it.

The joint / cartesian controllers **pre-fill** their relay connection (host +
side) from this selection, so a player doesn't retype it every time.

## Contract

The selection is persisted to `game-link.json` next to the module (default team
`purple`, host `http://localhost:8000`).

| Method | Path | Body | Returns |
|---|---|---|---|
| GET  | `/api/link`   | — | `{team, host}` — the current selection. |
| POST | `/api/link`   | `{team?, host?}` | Validates `team ∈ {purple, green}`, persists, returns the new `{team, host}`. |
| GET  | `/api/status` | — | Live snapshot (below). |
| GET  | `/api/events` | — | SSE stream of the same snapshot, ~2x/s. |

`/api/status` snapshot:

```jsonc
{
  "team": "purple"|"green",
  "host": "http://…",
  "reachable": bool,            // could we poll the game-master's relay state?
  "holder": "purple"|"green"|null,
  "you_hold": bool,             // holder == team
  "other_holds": bool,
  "arm_connected": bool|null,
  "arm_enabled": bool|null,
  "estop": bool|null,           // relay E-STOP latched
  "lease_secs": float|null,     // seconds left on the holder's lease
  "error": str|null,            // last poll error, if unreachable
  "port": str|null
}
```

A background poller (~1 Hz, short timeout) fetches
`<host>/p/dobot-mg400-relay/api/state` with stdlib `urllib` and caches it; the
request thread only ever reads the cache, so a dead host never stalls a request
and `/api/status` simply reports `reachable: false`.

## Programmatic API (for other machines via the hub)

`link()` → `{team, host}` — the current player selection. The joint / cartesian
controllers call this through the hub context (`get_prototype("game-link").link()`)
to default their relay host + side.
