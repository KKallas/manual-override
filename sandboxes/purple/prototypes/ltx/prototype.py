"""LTX player tab: an isolated route using the shared Auto PP X controller."""

import os

from flask import Blueprint, send_from_directory

SHARED_PLAYER_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..",
                 "green", "prototypes", "auto-pickup-game")
)
CAL2_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..",
                 "green", "prototypes", "auto-pp-cal-2")
)

MANIFEST = {
    "name": "LTX",
    "description": "Laser Tag X player cue builder and Cartesian arm controller.",
    "default_page": "",
    "pages": [{"path": "", "label": "LTX"}],
}
bp = Blueprint("purple_player_ltx", __name__)


@bp.route("/")
def index():
    response = send_from_directory(SHARED_PLAYER_DIR, "controller.html")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@bp.route("/calibrate")
def calibrate():
    response = send_from_directory(CAL2_DIR, "calibrate2.html")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response
