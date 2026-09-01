"""Player-side LTZ Score progression-screen visual mockup."""

import os

from flask import Blueprint, send_from_directory


HERE = os.path.dirname(os.path.abspath(__file__))
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

bp = Blueprint("green_player_ltz_score", __name__)


@bp.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@bp.route("/assets/<path:filename>")
def score_asset(filename):
    return send_from_directory(os.path.join(HERE, "assets"), filename)


@bp.route("/art/<path:filename>")
def game_art(filename):
    return send_from_directory(GAME_ART_DIR, filename)
