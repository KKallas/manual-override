"""Laser Tag: two-player cooperative ArUco beam puzzle."""

import os
import json
import uuid
import threading
import time
import datetime as dt

from flask import Blueprint, jsonify, request, send_from_directory

import live

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_LOG_DIR = os.path.join(HERE, "run-logs")

MANIFEST = {
    "name": "Laser Tag",
    "description": "Two-player cooperative placement puzzle with crossing playfield beams.",
    "default_page": "game",
    "pages": [{"path": "game", "label": "Laser Tag"}],
}

bp = Blueprint("laser_tag", __name__)
_lock = threading.Lock()
_live = live.LiveState()
_hub_ctx = None
_run = None


def _iso_now():
    return dt.datetime.now(dt.UTC).isoformat()


def _safe_run_id(value):
    value = str(value or "")
    return value if value and all(c.isalnum() or c in "-_" for c in value) else None


def _run_path(run_id):
    return os.path.join(RUN_LOG_DIR, f"{run_id}.jsonl")


def _append_run_event_locked(source, kind, detail=None, category=None):
    if not _run:
        return None
    detail = dict(detail) if isinstance(detail, dict) else {}
    category = category or detail.pop("category", None) or (
        "intent" if source == "green_auto_pp_x" else
        "log" if kind == "log" else
        "lifecycle" if kind in ("run_started", "run_ended") else
        "observation"
    )
    event = {
        "schema_version": 2, "event_id": uuid.uuid4().hex,
        "seq": _run["next_seq"], "run_id": _run["run_id"],
        "wall_time": _iso_now(), "server_epoch": time.time(),
        "producer": source, "category": category, "event": kind,
        "operation_id": detail.pop("operation_id", None),
        "team": detail.pop("team", None),
        "physical_tag": detail.pop("physical_tag", None),
        "target_marker": detail.pop("target_marker", None),
        "queue_index": detail.pop("queue_index", None),
        "attempt": detail.pop("attempt", detail.pop("pickup_attempt", None)),
        "client_monotonic_ms": detail.pop("client_monotonic_ms", None),
        "payload": detail,
    }
    _run["next_seq"] += 1
    _run["events"].append(event)
    _run["events"] = _run["events"][-1000:]
    os.makedirs(RUN_LOG_DIR, exist_ok=True)
    with open(_run_path(_run["run_id"]), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event


def _load_run_events(run_id, limit=1000):
    path = _run_path(run_id)
    if not os.path.isfile(path):
        return []
    events = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                events.append(json.loads(line))
            except (TypeError, ValueError):
                continue
    return events[-limit:]


def append_external_event(source, kind, detail=None, category=None):
    """Programmatic ingest used by an already-authenticated sibling prototype."""
    if source != "green_auto_pp_x" or not isinstance(kind, str) or not kind[:80]:
        return None, "invalid event"
    with _lock:
        if not _run or not _run.get("active"):
            return None, "no active Laser Tag run"
        event = _append_run_event_locked(source, kind[:80], detail, category=category)
    _live.bump()
    return event, None


def hub_init(ctx):
    global _hub_ctx
    _hub_ctx = ctx


def _fresh_state():
    return {
        "phase": "setup",
        "started_at": None,
        "finished_at": None,
        "tag_ids": {"green": [100, 101], "blue": [102, 103]},
        "message": "Configure four physical tags, then start.",
        "updated_at": time.time(),
    }


_state = _fresh_state()


def _roles():
    return request.environ.get("hhh.roles") or set()


def _snapshot_locked():
    out = dict(_state)
    out["tag_ids"] = {team: list(ids) for team, ids in _state["tag_ids"].items()}
    out["server_time"] = time.time()
    return out


@bp.route("/")
@bp.route("/game")
def game():
    response = send_from_directory(HERE, "game.html")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@bp.route("/api/state")
def state():
    with _lock:
        return jsonify(_snapshot_locked())


@bp.route("/api/arms")
def arms():
    """Small relay snapshot used by the game loop.

    The relay's public full-state response also carries its command log. Laser
    Tag needs each arm's pump mode and live pose, so return the compact per-arm
    states without repeatedly transferring unrelated operator data.
    """
    relay = _hub_ctx.get_prototype("dobot-mg400-relay") if _hub_ctx is not None else None
    if relay is None or not hasattr(relay, "arm_state"):
        return jsonify({"arms": {}})
    return jsonify({"arms": {
        "green": relay.arm_state("green"),
        # Laser Tag calls the second team blue; the relay calls that arm purple.
        "blue": relay.arm_state("purple"),
    }})


@bp.route("/api/events")
def events():
    def snapshot():
        with _lock:
            return _snapshot_locked()
    return _live.stream(snapshot, interval=0.2)


@bp.route("/api/operator", methods=["POST"])
def operator():
    if "gamemaster" not in _roles():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    data = request.get_json(silent=True) or {}
    global _state
    with _lock:
        if data.get("reset"):
            ids = {team: list(values) for team, values in _state["tag_ids"].items()}
            _state = _fresh_state()
            _state["tag_ids"] = ids
        else:
            for key in ("phase", "started_at", "finished_at", "message"):
                if key in data:
                    _state[key] = data[key]
            incoming = data.get("tag_ids")
            if isinstance(incoming, dict):
                for team in ("green", "blue"):
                    values = incoming.get(team)
                    if isinstance(values, list) and len(values) == 2:
                        _state["tag_ids"][team] = [int(values[0]), int(values[1])]
            _state["updated_at"] = time.time()
        out = _snapshot_locked()
    _live.bump()
    return jsonify(out)


@bp.route("/api/run", methods=["GET", "POST"])
def run_log():
    """Create/read the durable diagnostic log for one Laser Tag run."""
    global _run
    roles = _roles()
    if request.method == "POST":
        if "gamemaster" not in roles:
            return jsonify({"ok": False, "error": "gamemaster required"}), 403
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        with _lock:
            if action == "start":
                run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
                _run = {"run_id": run_id, "next_seq": 1, "events": [], "active": True}
                event = _append_run_event_locked("gamemaster", "run_started", data.get("detail"))
            elif action == "end" and _run:
                event = _append_run_event_locked("gamemaster", "run_ended", data.get("detail"))
                _run["active"] = False
            else:
                return jsonify({"ok": False, "error": "action must be start or end"}), 400
            out = dict(_run)
        _live.bump()
        return jsonify({"ok": True, "run": out, "event": event})
    with _lock:
        if not _run:
            return jsonify({"ok": True, "run": None, "events": []})
        return jsonify({"ok": True, "run": {k: v for k, v in _run.items() if k != "events"}, "events": list(_run["events"])})


@bp.route("/api/run/event", methods=["POST"])
def run_event():
    """Append an observed Laser Tag event or a green Auto PP X intention."""
    roles = _roles()
    data = request.get_json(silent=True) or {}
    source = str(data.get("source") or "")
    if "gamemaster" in roles:
        if source not in ("laser", "gamemaster"):
            return jsonify({"ok": False, "error": "invalid gamemaster source"}), 400
    elif "green" in roles:
        source = "green_auto_pp_x"
    else:
        return jsonify({"ok": False, "error": "gamemaster or green required"}), 403
    kind = str(data.get("kind") or "")[:80]
    if not kind:
        return jsonify({"ok": False, "error": "kind required"}), 400
    with _lock:
        if not _run or not _run.get("active"):
            return jsonify({"ok": False, "error": "no active Laser Tag run"}), 409
        requested_run = _safe_run_id(data.get("run_id"))
        if requested_run and requested_run != _run["run_id"]:
            return jsonify({"ok": False, "error": "run changed"}), 409
        event = _append_run_event_locked(
            source, kind, data.get("detail"), category=data.get("category")
        )
    _live.bump()
    return jsonify({"ok": True, "event": event})


@bp.route("/api/run/<run_id>.jsonl")
def download_run(run_id):
    if "gamemaster" not in _roles():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    safe = _safe_run_id(run_id)
    if not safe or not os.path.isfile(_run_path(safe)):
        return jsonify({"ok": False, "error": "run not found"}), 404
    return send_from_directory(RUN_LOG_DIR, f"{safe}.jsonl", as_attachment=True)


@bp.route("/api/runs")
def list_runs():
    if "gamemaster" not in _roles():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    os.makedirs(RUN_LOG_DIR, exist_ok=True)
    runs = []
    for name in sorted(os.listdir(RUN_LOG_DIR), reverse=True):
        if not name.endswith(".jsonl"):
            continue
        run_id = _safe_run_id(name[:-6])
        if run_id:
            runs.append({"run_id": run_id, "bytes": os.path.getsize(_run_path(run_id))})
    return jsonify({"ok": True, "runs": runs})


@bp.route("/api/playfield", methods=["POST"])
def install_playfield():
    """Install the Laser Tag board as one playfield revision.

    The playfield is shared with Auto PP calibration.  Replacing its complete
    store in-process avoids exposing a half-deleted/half-created board to an
    Auto PP X tab that is polling at the same time.
    """
    if "gamemaster" not in _roles():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    data = request.get_json(silent=True) or {}
    areas = data.get("areas")
    settings = data.get("settings")
    if not isinstance(areas, list) or not isinstance(settings, dict):
        return jsonify({"ok": False, "error": "areas and settings are required"}), 400
    playfield = _hub_ctx.get_prototype("playfield-areas") if _hub_ctx is not None else None
    if playfield is None or not hasattr(playfield, "replace_areas"):
        return jsonify({"ok": False, "error": "playfield unavailable"}), 503
    try:
        installed = playfield.replace_areas(areas)
        if hasattr(playfield, "set_view_settings"):
            playfield.set_view_settings(**settings)
        if hasattr(playfield, "save_areas_now"):
            playfield.save_areas_now()
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "areas": installed})
