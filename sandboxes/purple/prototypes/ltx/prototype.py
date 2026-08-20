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
CRANE_CONTROLS_DIR = os.path.join(SHARED_PLAYER_DIR, "assets", "crane-controls")

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


@bp.route("/assets/crane-controls/<path:filename>")
def crane_control_asset(filename):
    response = send_from_directory(CRANE_CONTROLS_DIR, filename)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@bp.route("/calibrate")
def calibrate():
    response = send_from_directory(CAL2_DIR, "calibrate2.html")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response
