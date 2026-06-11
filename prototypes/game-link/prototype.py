"""
Game Link prototype — the player-side "machine" (a Flask blueprint).

A player picks their TEAM (purple / green) and the game-master HOST (the address
of the hub that runs the dobot-mg400-relay), and this machine remembers it and
shows the live relay connection status: is the game-master reachable, is the arm
connected/enabled, who holds the floor, and do YOU (your team) hold it.

It is the player side's counterpart to the relay's operator GUI: it never owns the
arm and never sends move/pump commands. It only READS the relay's published state
(`<host>/p/dobot-mg400-relay/api/state`) and stores the team/host selection, which
the joint / cartesian controllers read via the hub so they can pre-fill their
relay connection fields (so a player doesn't retype host + side every time).

Loaded by hub.py; registered under /p/game-link. No app.run() of its own — it only
runs inside the hub server. The relay poll is best-effort with a short timeout; if
the game-master is unreachable every endpoint still works and just reports
`reachable: false`.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request, send_from_directory

import live   # shared push helper (prototypes/live.py)

HERE = os.path.dirname(os.path.abspath(__file__))

MANIFEST = {
    "name": "Game Link",
    "description": "Pick your team (purple/green) and the game-master host; shows "
                   "live relay connection status. Player-side.",
    "default_page": "",
    "pages": [{"path": "", "label": "Game Link"}],
}
bp = Blueprint("game_link", __name__)

# ---- configuration --------------------------------------------------------
TEAMS = ("purple", "green")
DEFAULT_TEAM = "purple"
DEFAULT_HOST = "http://localhost:8000"
LINK_PATH = os.path.join(HERE, "game-link.json")   # persisted team/host selection

RELAY_STATE_PATH = "/p/dobot-mg400-relay/api/state"  # what we poll on the host
POLL_HZ = 1.0          # how often the background poller fetches relay state
POLL_TIMEOUT = 1.5     # seconds — short, so a dead host never stalls a request

# Persisted selection (team + game-master host). Guarded by _link_lock; the poller
# reads the host under it, then does network I/O OUTSIDE the lock.
_link_lock = threading.Lock()
_team = DEFAULT_TEAM
_host = DEFAULT_HOST

# Cached relay state from the background poller. Guarded by _poll_lock; NEVER do
# network I/O while holding it (the snapshot reads it; the poller writes it).
_poll_lock = threading.Lock()
_relay_state = None     # last good relay state dict, or None
_reachable = False      # did the last poll succeed?
_last_error = None      # last poll error string, or None

# Sampled state (the relay poll + lease seconds tick on their own), so the SSE
# stream re-snapshots on a short interval rather than on a bump. See prototypes/live.py.
_live = live.LiveState()


# ---- persisted selection ---------------------------------------------------
def _load_link():
    """Restore the remembered {team, host} into the module globals (best-effort)."""
    global _team, _host
    try:
        with open(LINK_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return
    team = data.get("team")
    host = data.get("host")
    with _link_lock:
        if team in TEAMS:
            _team = team
        if isinstance(host, str) and host.strip():
            _host = host.strip()


def _save_link():
    """Persist the current {team, host} next to this module (best-effort)."""
    with _link_lock:
        data = {"team": _team, "host": _host}
    try:
        with open(LINK_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


_load_link()


# ---- programmatic API (for other machines via the hub) ---------------------
def link():
    """The current player selection — {team, host}. Other machines (the joint /
    cartesian controllers) call this through the hub context to pre-fill their
    relay connection fields, so a player doesn't retype host + side."""
    with _link_lock:
        return {"team": _team, "host": _host}


# ---- background relay poller ----------------------------------------------
def _fetch_relay_state(host):
    """Fetch `<host>/p/dobot-mg400-relay/api/state` with a short timeout. Returns
    (state_dict, error_str) — exactly one is non-None. Never raises."""
    url = host.rstrip("/") + RELAY_STATE_PATH
    try:
        with urllib.request.urlopen(url, timeout=POLL_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} from relay"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return None, f"unreachable: {reason}"
    except (ValueError, TypeError):
        return None, "relay returned non-JSON"


def _poll_loop():
    """~1 Hz: fetch the relay state for the current host and cache it. Network I/O
    happens OUTSIDE both locks; the request thread only ever reads the cache. Any
    error is captured into the cache, never raised."""
    global _relay_state, _reachable, _last_error
    period = 1.0 / POLL_HZ
    while True:
        with _link_lock:
            host = _host
        state, error = _fetch_relay_state(host)
        with _poll_lock:
            if state is not None:
                _relay_state = state
                _reachable = True
                _last_error = None
            else:
                _reachable = False
                _last_error = error
                # keep _relay_state as the last good value for context
        _live.bump()
        time.sleep(period)


_poll_thread = threading.Thread(target=_poll_loop, name="game-link-poll", daemon=True)
_poll_thread.start()


# ---- response helpers ------------------------------------------------------
def _ok(**kw):
    return jsonify({"ok": True, **kw})


def _fail(error, **kw):
    return jsonify({"ok": False, "error": error, **kw})


def _status_dict():
    """A snapshot for GET /api/status and the SSE stream: the selection plus the
    cached relay connection state, distilled for the UI. Reads only cached data —
    no network I/O — so it never blocks the SSE lock."""
    with _link_lock:
        team, host = _team, _host
    with _poll_lock:
        state, reachable, error = _relay_state, _reachable, _last_error
    holder = arm_connected = arm_enabled = estop = lease_secs = None
    if state is not None:
        holder = state.get("holder")
        estop = state.get("estop")
        lease_secs = state.get("lease_secs")
        arm = state.get("arm") or {}
        arm_connected = arm.get("connected")
        arm_enabled = arm.get("enabled")
    return {
        "team": team,
        "host": host,
        "reachable": reachable,
        "holder": holder,
        "you_hold": holder is not None and holder == team,
        "other_holds": holder is not None and holder != team,
        "arm_connected": arm_connected,
        "arm_enabled": arm_enabled,
        "estop": estop,
        "lease_secs": lease_secs,
        "error": error,
        "port": request.host.split(":")[-1] if ":" in request.host else None,
    }


# ---- pages ----------------------------------------------------------------
@bp.route("/")
def index():
    return send_from_directory(HERE, "controller.html")


# ---- API -------------------------------------------------------------------
@bp.route("/api/link", methods=["GET"])
def api_link():
    """The remembered player selection: {team, host}."""
    return jsonify(link())


@bp.route("/api/link", methods=["POST"])
def api_set_link():
    """Update the team and/or host, validate, persist, and push the change."""
    global _team, _host
    data = request.get_json(silent=True) or {}
    with _link_lock:
        if "team" in data:
            team = data.get("team")
            if team not in TEAMS:
                return _fail("team must be 'purple' or 'green'")
            _team = team
        if "host" in data:
            host = data.get("host")
            if not isinstance(host, str) or not host.strip():
                return _fail("host must be a non-empty string")
            _host = host.strip()
    _save_link()
    _live.bump()   # poller will re-target the new host; tell open windows now
    return jsonify(link())


@bp.route("/api/status")
def api_status():
    return jsonify(_status_dict())


@bp.route("/api/events")
def api_events():
    """Push the link selection + live relay connection status ~2x/s while it
    changes (the lease counts down, so it changes most samples)."""
    return _live.stream(_status_dict, interval=0.5)
