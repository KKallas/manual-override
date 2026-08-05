"""Second-generation Auto PP calibration operator tab."""
import os
from flask import Blueprint, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = {
    "name": "Auto PP Cal 2",
    "description": "Six-point outer-edge per-arm Auto PP calibration.",
    "default_page": "",
    "pages": [{"path": "", "label": "Auto PP Cal 2"}],
}
bp = Blueprint("auto_pp_cal_2_operator", __name__)


@bp.route("/")
def index():
    response = send_from_directory(HERE, "controller.html")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response
