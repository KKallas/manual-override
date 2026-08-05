"""Auto PP Cal 2 player tab."""
import os
from flask import Blueprint, send_from_directory

SHARED = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                      "green", "prototypes", "auto-pp-cal-2"))
MANIFEST = {
    "name": "Auto PP Cal 2",
    "description": "Six-point outer-edge per-arm Auto PP calibration.",
    "default_page": "",
    "pages": [{"path": "", "label": "Auto PP Cal 2"}],
}
bp = Blueprint("purple_auto_pp_cal_2", __name__)


@bp.route("/")
def index():
    response = send_from_directory(SHARED, "calibrate2.html")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response
