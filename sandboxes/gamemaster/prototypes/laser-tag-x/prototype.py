"""Laser Tag X: two-player cooperative ArUco beam puzzle."""

import os
import json
import math
import uuid
import threading
import time
import datetime as dt

from flask import Blueprint, jsonify, request, send_file, send_from_directory

import live

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_LOG_DIR = os.path.join(HERE, "run-logs")
SCORE_LOG_PATH = os.path.join(HERE, "score-log.txt")
SCORE_TABLE_STATE_PATH = os.path.join(HERE, "score-table-state.json")
SETTINGS_PATH = os.path.join(HERE, "ltx-settings.json")

LTX_GAME_MODES = {
    "game_1": {
        "auto_pp_x": False,
        "joint_angles": True,
        "tcp_pose": False,
        "auto_pick_place": False,
        "video_click_move": False,
    },
    "game_2": {
        "auto_pp_x": False,
        "joint_angles": False,
        "tcp_pose": True,
        "auto_pick_place": False,
        "video_click_move": False,
    },
    "game_3": {
        "auto_pp_x": False,
        "joint_angles": False,
        "tcp_pose": True,
        "auto_pick_place": True,
        "video_click_move": True,
    },
    "game_4": {
        "auto_pp_x": True,
        "joint_angles": False,
        "tcp_pose": True,
        "auto_pick_place": True,
        "video_click_move": True,
    },
}
DEFAULT_LTX_GAME_MODE = "game_4"
LTX_GAME_LABELS = {
    "game_1": "Game 1",
    "game_2": "Game 2",
    "game_3": "Game 3",
    "game_4": "Game 4",
}

MANIFEST = {
    "name": "Laser Tag X",
    "description": "Two-player cooperative placement puzzle with crossing playfield beams.",
    "default_page": "game",
    "pages": [{"path": "game", "label": "Laser Tag X"}],
}

bp = Blueprint("laser_tag_x", __name__)
_lock = threading.Lock()
_live = live.LiveState()
_hub_ctx = None
_run = None


def _load_ltx_game_mode():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as handle:
            mode = json.load(handle).get("game_mode")
            if mode in LTX_GAME_MODES:
                return mode
    except (OSError, AttributeError, json.JSONDecodeError):
        pass
    return DEFAULT_LTX_GAME_MODE


def _save_ltx_game_mode(mode):
    temp_path = f"{SETTINGS_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump({"game_mode": mode}, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, SETTINGS_PATH)


def _ltx_visibility(mode):
    return {
        panel: visible
        for panel, visible in LTX_GAME_MODES[mode].items()
        if panel != "video_click_move"
    }


def _load_score_reset_at():
    try:
        with open(SCORE_TABLE_STATE_PATH, encoding="utf-8") as handle:
            return float(json.load(handle).get("reset_at", 0.0))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 0.0


_score_reset_at = _load_score_reset_at()


def _iso_now():
    return dt.datetime.now(dt.UTC).isoformat()


def _safe_run_id(value):
    value = str(value or "")
    return value if value and all(c.isalnum() or c in "-_" for c in value) else None


def _run_path(run_id):
    return os.path.join(RUN_LOG_DIR, f"{run_id}.jsonl")


def _read_score_log():
    if not os.path.isfile(SCORE_LOG_PATH):
        return []
    scores = []
    with open(SCORE_LOG_PATH, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                score = json.loads(line)
                elapsed = float(score.get("elapsed_seconds"))
                recorded_at = float(score.get("recorded_at"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not math.isfinite(elapsed) or not math.isfinite(recorded_at) or elapsed < 0:
                continue
            score["elapsed_seconds"] = elapsed
            score["recorded_at"] = recorded_at
            scores.append(score)
    return scores


def _visible_scores_locked():
    scores = [
        score for score in _read_score_log()
        if score["recorded_at"] > _score_reset_at
    ]
    scores = sorted(
        scores,
        key=lambda score: (score["elapsed_seconds"], score["recorded_at"]),
    )
    ranked = []
    for index, score in enumerate(scores):
        row = dict(score)
        row["rank"] = index + 1
        if index:
            faster = scores[index - 1]
            row["next_faster_rank"] = index
            row["next_faster_seconds"] = round(
                score["elapsed_seconds"] - faster["elapsed_seconds"], 3)
            row["next_faster_player_label"] = faster.get("player_label") or ""
        else:
            row["next_faster_rank"] = None
            row["next_faster_seconds"] = None
            row["next_faster_player_label"] = ""
        ranked.append(row)
    return ranked


def _score_result_locked(score_id):
    """Return the visible table position for one just-recorded score."""
    rows = _visible_scores_locked()
    for row in rows:
        if row.get("score_id") != score_id:
            continue
        return {
            "score_id": score_id,
            "rank": row["rank"],
            "total_scores": len(rows),
            "elapsed_seconds": row["elapsed_seconds"],
            "next_faster_rank": row["next_faster_rank"],
            "next_faster_seconds": row["next_faster_seconds"],
            "next_faster_player_label": row["next_faster_player_label"],
        }
    return None


def _registered_player_names():
    """Read the names entered in the shared Green/Purple player controllers."""
    auto_pickup = (
        _hub_ctx.get_prototype("auto-pickup-game")
        if _hub_ctx is not None else None
    )
    if auto_pickup is None or not hasattr(auto_pickup, "player_names_snapshot"):
        return {"green": "", "purple": ""}
    try:
        names = auto_pickup.player_names_snapshot()
    except (AttributeError, TypeError):
        return {"green": "", "purple": ""}
    return {
        side: str((names or {}).get(side) or "").strip()[:32]
        for side in ("green", "purple")
    }


def _append_winning_score_locked():
    try:
        started_at = float(_state.get("started_at"))
        finished_at = float(_state.get("finished_at"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(started_at) or not math.isfinite(finished_at) or finished_at < started_at:
        return None
    tags = _state.get("tag_ids") or {}
    # Preserve names that were captured when play began, but repair either
    # missing side from the live registration snapshot. Player controllers may
    # still be completing/retrying registration when the operator presses
    # Start. The old ``snapshot or fallback`` expression never reached its
    # fallback because {"green": "", "purple": ""} is truthy.
    player_names = dict(_state.get("player_names") or {})
    if any(not str(player_names.get(side) or "").strip() for side in ("green", "purple")):
        registered_names = _registered_player_names()
        for side in ("green", "purple"):
            if not str(player_names.get(side) or "").strip():
                player_names[side] = registered_names.get(side) or ""
    _state["player_names"] = player_names
    green_player_name = str(player_names.get("green") or "").strip()[:32]
    purple_player_name = str(player_names.get("purple") or "").strip()[:32]
    green_label = f"Green · {green_player_name}" if green_player_name else "Green"
    purple_label = f"Purple · {purple_player_name}" if purple_player_name else "Purple"
    game_mode = _state.get("game_mode_at_start") or _state.get("ltx_game_mode")
    if game_mode not in LTX_GAME_MODES:
        game_mode = DEFAULT_LTX_GAME_MODE
    entry = {
        "score_id": uuid.uuid4().hex,
        "logged_at": _iso_now(),
        "recorded_at": time.time(),
        "players": ["green", "purple"],
        "player_label": f"{green_label} + {purple_label}",
        "green_player_name": green_player_name,
        "purple_player_name": purple_player_name,
        "game_mode": game_mode,
        "game_type": LTX_GAME_LABELS[game_mode],
        "elapsed_seconds": round(finished_at - started_at, 3),
        "started_at": started_at,
        "finished_at": finished_at,
        "green_tag_ids": list(tags.get("green") or []),
        # Laser Tag X calls this team blue, while the player/relay side is purple.
        "purple_tag_ids": list(tags.get("blue") or []),
        "run_id": _run.get("run_id") if _run else None,
    }
    with open(SCORE_LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def _save_score_reset_at(reset_at):
    with open(SCORE_TABLE_STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump({"reset_at": reset_at}, handle, indent=2)
        handle.write("\n")


def _append_run_event_locked(source, kind, detail=None, category=None):
    if not _run:
        return None
    detail = dict(detail) if isinstance(detail, dict) else {}
    category = category or detail.pop("category", None) or (
        "intent" if source in ("green_ltx", "purple_ltx") else
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
    if source not in ("green_ltx", "purple_ltx") or not isinstance(kind, str) or not kind[:80]:
        return None, "invalid event"
    with _lock:
        if not _run or not _run.get("active"):
            return None, "no active Laser Tag X run"
        event = _append_run_event_locked(source, kind[:80], detail, category=category)
    _live.bump()
    return event, None


def hub_init(ctx):
    global _hub_ctx
    _hub_ctx = ctx


def _fresh_state(game_mode=None):
    game_mode = game_mode if game_mode in LTX_GAME_MODES else _load_ltx_game_mode()
    return {
        "phase": "setup",
        "started_at": None,
        "finished_at": None,
        "tag_ids": {"green": [100, 101], "blue": [102, 103]},
        "activated_targets": [],
        "final_stage": "outer_ring",
        "first_center_manual": False,
        "first_center_tag": None,
        "first_center_team": None,
        "first_center_position": None,
        "first_center_confirmed_at": None,
        "message": "Configure four physical tags, then start.",
        "player_names": {"green": "", "purple": ""},
        "score_result": None,
        "ltx_game_mode": game_mode,
        "game_mode_at_start": None,
        "ltx_visibility": _ltx_visibility(game_mode),
        "updated_at": time.time(),
    }


_state = _fresh_state()


def _roles():
    return request.environ.get("hhh.roles") or set()


def _snapshot_locked():
    out = dict(_state)
    out["tag_ids"] = {team: list(ids) for team, ids in _state["tag_ids"].items()}
    out["activated_targets"] = list(_state.get("activated_targets") or [])
    out["player_names"] = dict(_state["player_names"])
    out["score_result"] = (
        dict(_state["score_result"])
        if isinstance(_state.get("score_result"), dict) else None
    )
    out["ltx_visibility"] = dict(_state["ltx_visibility"])
    out["server_time"] = time.time()
    return out


def ltx_visibility_snapshot():
    """Legacy read-only panel visibility exposed through the shared player API."""
    with _lock:
        return dict(_state["ltx_visibility"])


def ltx_config_snapshot():
    """Read-only game mode and controls exposed through the shared player API."""
    with _lock:
        game_mode = _state["ltx_game_mode"]
        return {
            "game_mode": game_mode,
            "visibility": dict(_state["ltx_visibility"]),
            "video_click_move": LTX_GAME_MODES[game_mode]["video_click_move"],
        }


def ltx_player_state_snapshot():
    """Read-only game state exposed to players through the shared Auto Pickup API."""
    with _lock:
        return _snapshot_locked()


def _public_pose(value):
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        pose = [float(item) for item in value[:4]]
    except (TypeError, ValueError):
        return None
    return pose if all(math.isfinite(item) for item in pose) else None


def _public_arm_state(raw):
    """Limit player arm tracking to live motion fields, never relay logs."""
    raw = raw if isinstance(raw, dict) else {}
    pump_mode = str(raw.get("pump_mode") or "off")
    return {
        "connected": bool(raw.get("connected")),
        "enabled": bool(raw.get("enabled")),
        "mode_name": str(raw.get("mode_name") or "")[:80],
        "pose": _public_pose(raw.get("pose")),
        "target": _public_pose(raw.get("target")),
        "pump_mode": pump_mode if pump_mode in {"suck", "blow", "off", "conflict"} else "off",
    }


def ltx_player_arms_snapshot():
    """The same compact relay poses used by the gamemaster's LTX game loop."""
    relay = _hub_ctx.get_prototype("dobot-mg400-relay") if _hub_ctx is not None else None
    if relay is None or not hasattr(relay, "arm_state"):
        return {"arms": {}, "server_time": time.time()}
    return {
        "arms": {
            "green": _public_arm_state(relay.arm_state("green")),
            "purple": _public_arm_state(relay.arm_state("purple")),
        },
        "server_time": time.time(),
    }


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
        # Laser Tag X calls the second team blue; the relay calls that arm purple.
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
        previous_phase = _state.get("phase")
        if data.get("reset"):
            ids = {team: list(values) for team, values in _state["tag_ids"].items()}
            game_mode = _state["ltx_game_mode"]
            _state = _fresh_state(game_mode)
            _state["tag_ids"] = ids
        else:
            for key in (
                "phase", "started_at", "finished_at", "message", "final_stage",
                "first_center_manual",
                "first_center_tag", "first_center_team", "first_center_position",
                "first_center_confirmed_at",
            ):
                if key in data:
                    _state[key] = data[key]
            incoming = data.get("tag_ids")
            if isinstance(incoming, dict):
                for team in ("green", "blue"):
                    values = incoming.get(team)
                    if isinstance(values, list) and len(values) == 2:
                        _state["tag_ids"][team] = [int(values[0]), int(values[1])]
            incoming_activated = data.get("activated_targets")
            if isinstance(incoming_activated, list):
                _state["activated_targets"] = sorted({
                    int(marker) for marker in incoming_activated
                    if isinstance(marker, (int, float)) and 30 <= int(marker) <= 37
                })
            incoming_activated_target = data.get("activated_target")
            if isinstance(incoming_activated_target, (int, float)):
                marker = int(incoming_activated_target)
                if 30 <= marker <= 37:
                    _state["activated_targets"] = sorted({
                        *(_state.get("activated_targets") or []), marker,
                    })
            incoming_game_mode = data.get("ltx_game_mode")
            if incoming_game_mode is not None:
                if incoming_game_mode not in LTX_GAME_MODES:
                    return jsonify({"ok": False, "error": "invalid Laser Tag X game mode"}), 400
                try:
                    _save_ltx_game_mode(incoming_game_mode)
                except OSError as exc:
                    return jsonify({"ok": False, "error": f"could not save Laser Tag X game mode: {exc}"}), 500
                _state["ltx_game_mode"] = incoming_game_mode
                _state["ltx_visibility"] = _ltx_visibility(incoming_game_mode)
            if previous_phase != "running" and _state.get("phase") == "running":
                _state["player_names"] = _registered_player_names()
                _state["game_mode_at_start"] = _state["ltx_game_mode"]
            _state["updated_at"] = time.time()
            if previous_phase != "won" and _state.get("phase") == "won":
                score = _append_winning_score_locked()
                if score is not None:
                    _state["score_result"] = _score_result_locked(score["score_id"])
        out = _snapshot_locked()
    _live.bump()
    return jsonify(out)


@bp.route("/api/scores")
def scores():
    if "gamemaster" not in _roles():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    with _lock:
        rows = _visible_scores_locked()
        reset_at = _score_reset_at
    return jsonify({
        "ok": True,
        "scores": rows,
        "reset_at": reset_at,
        "log_file": os.path.basename(SCORE_LOG_PATH),
    })


@bp.route("/api/scores/reset", methods=["POST"])
def reset_scores():
    if "gamemaster" not in _roles():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    global _score_reset_at
    with _lock:
        _score_reset_at = time.time()
        _save_score_reset_at(_score_reset_at)
        _state["score_result"] = None
        _state["updated_at"] = time.time()
        reset_at = _score_reset_at
    _live.bump()
    return jsonify({
        "ok": True,
        "scores": [],
        "reset_at": reset_at,
        "log_preserved": True,
        "log_file": os.path.basename(SCORE_LOG_PATH),
    })


@bp.route("/api/scores/log")
def download_score_log():
    if "gamemaster" not in _roles():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    if not os.path.exists(SCORE_LOG_PATH):
        with _lock:
            if not os.path.exists(SCORE_LOG_PATH):
                with open(SCORE_LOG_PATH, "a", encoding="utf-8"):
                    pass
    return send_file(
        SCORE_LOG_PATH,
        as_attachment=True,
        download_name="laser-tag-x-score-log.txt",
        mimetype="text/plain",
    )


@bp.route("/api/run", methods=["GET", "POST"])
def run_log():
    """Create/read the durable diagnostic log for one Laser Tag X run."""
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
    """Append an observed Laser Tag X event or an authenticated LTX intention."""
    roles = _roles()
    data = request.get_json(silent=True) or {}
    source = str(data.get("source") or "")
    detail = dict(data.get("detail")) if isinstance(data.get("detail"), dict) else {}
    if "gamemaster" in roles:
        if source not in ("laser", "gamemaster"):
            return jsonify({"ok": False, "error": "invalid gamemaster source"}), 400
    elif "green" in roles:
        source = "green_ltx"
        detail["team"] = "green"
    elif "purple" in roles:
        source = "purple_ltx"
        detail["team"] = "purple"
    else:
        return jsonify({"ok": False, "error": "gamemaster or player required"}), 403
    kind = str(data.get("kind") or "")[:80]
    if not kind:
        return jsonify({"ok": False, "error": "kind required"}), 400
    with _lock:
        if not _run or not _run.get("active"):
            return jsonify({"ok": False, "error": "no active Laser Tag X run"}), 409
        requested_run = _safe_run_id(data.get("run_id"))
        if requested_run and requested_run != _run["run_id"]:
            return jsonify({"ok": False, "error": "run changed"}), 409
        event = _append_run_event_locked(
            source, kind, detail, category=data.get("category")
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
    """Install the Laser Tag X board as one playfield revision.

    The playfield is shared with Auto PP calibration.  Replacing its complete
    store in-process avoids exposing a half-deleted/half-created board to an
    LTX tab that is polling at the same time.
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
