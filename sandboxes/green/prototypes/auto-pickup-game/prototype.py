"""
Player view for the Auto Pick and Place game.

Mounted in a team sandbox. The player enters a name first, then drives XYZ +
the air pump through this team's cartesian relay controller to move the block
from one square to the other. The gamemaster logs the time to CSV.
"""

import os

from flask import Blueprint, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
CAL2_DIR = os.path.abspath(os.path.join(HERE, "..", "auto-pp-cal-2"))

MANIFEST = {
    "name": "Auto Pick and Place",
    "description": "Player view for the pick-and-place game: enter name, "
                   "watch the live feed, drive XYZ + pump, then finish.",
    "default_page": "",
    "pages": [{"path": "", "label": "Auto Pick and Place"}],
}
bp = Blueprint("player_auto_pickup_game", __name__)


@bp.route("/")
def index():
    resp = send_from_directory(HERE, "controller.html")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@bp.route("/calibrate")
def calibrate():
    """Keep the established player URL, now backed only by six-point Cal 2."""
    resp = send_from_directory(CAL2_DIR, "calibrate2.html")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp
