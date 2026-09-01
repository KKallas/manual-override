"""Purple player wrapper for the shared LTZ Score visual mockup."""

import os

from flask import Blueprint, send_from_directory


HERE = os.path.dirname(os.path.abspath(__file__))
SHARED_SCORE_DIR = os.path.abspath(
    os.path.join(HERE, "..", "..", "..", "green", "prototypes", "ltz-score")
)
GAME_ART_DIR = os.path.abspath(
    os.path.join(HERE, "..", "..", "..", "..", "assets", "game-art", "z-pixel-v2", "normalized")
)

MANIFEST = {
    "name": "LTZ Score",
    "description": (
        "Player progression-screen mockup with editable test credits, independent "
        "control/weapon-unit/force-field upgrades, unique color-tiered turret sprites, and "
        "replayable unlock reveals."
    ),
    "default_page": "",
    "pages": [{"path": "", "label": "LTZ Score"}],
}

bp = Blueprint("purple_player_ltz_score", __name__)


@bp.route("/")
def index():
    return send_from_directory(SHARED_SCORE_DIR, "index.html")


@bp.route("/assets/<path:filename>")
def score_asset(filename):
    return send_from_directory(os.path.join(SHARED_SCORE_DIR, "assets"), filename)


@bp.route("/art/<path:filename>")
def game_art(filename):
    return send_from_directory(GAME_ART_DIR, filename)
