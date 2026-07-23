"""Tag Invaders: cooperative two-stage physical ArUco placement game."""

import os
import threading
import time

from flask import Blueprint, jsonify, request, send_from_directory

import live

HERE = os.path.dirname(os.path.abspath(__file__))

MANIFEST = {
    "name": "Tag Invaders",
    "description": "Two players cover glowing screen ArUcos with physical tags before each 60 second invasion timer expires.",
    "default_page": "game",
    "pages": [{"path": "game", "label": "Tag Invaders"}],
}
bp = Blueprint("tag_invaders", __name__)
_lock = threading.Lock()
_live = live.LiveState()


def _fresh_state():
    return {
        "phase": "setup",  # setup | level1 | won | lost
        "level": 0,
        "deadline": None,
        "started_at": None,
        "completed": {"purple": False, "green": False},
        "tag_ids": {"purple": 102, "green": 103},
        "message": "Ready for invasion",
        "updated_at": time.time(),
    }


_state = _fresh_state()


def _roles():
    return request.environ.get("hhh.roles") or set()


def _operator_only():
    if "gamemaster" not in _roles():
        return jsonify({"ok": False, "error": "gamemaster required"}), 403
    return None


def _snapshot_locked():
    out = dict(_state)
    out["completed"] = dict(_state["completed"])
    out["tag_ids"] = dict(_state["tag_ids"])
    out["server_time"] = time.time()
    return out


def _patch(data):
    with _lock:
        for key in ("phase", "level", "deadline", "started_at", "message"):
            if key in data:
                _state[key] = data[key]
        for key in ("completed", "tag_ids"):
            if isinstance(data.get(key), dict):
                _state[key].update(data[key])
        _state["updated_at"] = time.time()
        out = _snapshot_locked()
    _live.bump()
    return out


@bp.route("/")
@bp.route("/game")
def game():
    resp = send_from_directory(HERE, "game.html")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@bp.route("/api/state")
def state():
    with _lock:
        return jsonify(_snapshot_locked())


@bp.route("/api/events")
def events():
    def snapshot():
        with _lock:
            return _snapshot_locked()
    return _live.stream(snapshot, interval=0.2)


@bp.route("/api/operator", methods=["POST"])
def operator():
    denied = _operator_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    if data.get("reset"):
        global _state
        with _lock:
            tag_ids = dict(_state["tag_ids"])
            _state = _fresh_state()
            _state["tag_ids"] = tag_ids
            out = _snapshot_locked()
        _live.bump()
        return jsonify(out)
    return jsonify(_patch(data))
