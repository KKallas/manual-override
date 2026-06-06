"""
Prototype hub — one server that hosts every prototype in this folder.

On startup it scans `prototypes/*/prototype.py`, mounts each prototype's Flask
blueprint under `/p/<slug>`, and serves a dashboard at `/` with one tab per
prototype. Each tab embeds that prototype's default GUI (its controller); the
controller's own polling keeps the embedded view live. Pages flagged `newtab`
in a prototype's manifest (e.g. the playfield's clean 3D screen) open in their
own browser tab.

A prototype is any sub-folder containing a `prototype.py` that defines:
    MANIFEST = { "name", "description", "default_page", "pages": [...] }
    bp       = flask.Blueprint(...)   # its pages + API, all paths relative
See README.md for the full contract.

Run:
    pip install -r requirements.txt
    python hub.py            # then open http://localhost:8000
"""

import argparse
import importlib.util
import json
import os
import sys
import threading
import traceback

from flask import Flask, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
_prototypes = []   # discovered + mounted, in display order
_modules = {}      # slug -> imported prototype module (for cross-prototype use)

# Per-prototype enable/disable state, persisted across restarts. We store the
# *disabled* slugs (default = everything enabled). Disabled prototypes stay
# mounted, but the dashboard dims them and won't load their GUI.
HUB_SETTINGS_PATH = os.path.join(HERE, "hub-settings.json")
_disabled = set()
_settings_lock = threading.Lock()


def _load_hub_settings():
    try:
        with open(HUB_SETTINGS_PATH) as f:
            return set(json.load(f).get("disabled", []))
    except (OSError, ValueError, TypeError):
        return set()


def _save_hub_settings():
    try:
        with open(HUB_SETTINGS_PATH, "w") as f:
            json.dump({"disabled": sorted(_disabled)}, f, indent=2)
    except OSError:
        pass


def _load_one(slug):
    """Import prototypes/<slug>/prototype.py and register its blueprint."""
    path = os.path.join(HERE, slug, "prototype.py")
    modname = f"proto_{slug.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(modname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    # Put the prototype's own folder on sys.path while it imports, so it can
    # `import` sibling helper modules (e.g. dobot.py) by plain name.
    sys.path.insert(0, os.path.dirname(path))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(os.path.dirname(path))

    manifest = getattr(module, "MANIFEST", {})
    bp = getattr(module, "bp", None)
    if bp is None:
        raise AttributeError("prototype.py defines no `bp` blueprint")

    prefix = f"/p/{slug}"
    # name= overrides the blueprint's own name so two prototypes can't collide.
    app.register_blueprint(bp, url_prefix=prefix, name=modname)
    _modules[slug] = module

    default_page = manifest.get("default_page", "")
    return {
        "slug": slug,
        "name": manifest.get("name", slug),
        "description": manifest.get("description", ""),
        "base": prefix,
        "default_url": f"{prefix}/{default_page}".rstrip("/") + ("/" if not default_page else ""),
        "pages": [
            {
                "label": p.get("label", p.get("path", "Open")),
                "url": f"{prefix}/{p.get('path', '')}",
                "newtab": bool(p.get("newtab", False)),
            }
            for p in manifest.get("pages", [{"path": "", "label": "Open"}])
        ],
    }


class HubContext:
    """Handed to a prototype's optional hub_init(ctx) hook. Lets a prototype see
    whether it (or another prototype) is enabled, and reach another prototype's
    module to call its programmatic API. Looks are live — they reflect the
    current enable/disable state, which can change at runtime."""

    def __init__(self, slug):
        self.slug = slug

    def is_enabled(self):
        """Is THIS prototype currently enabled in the hub?"""
        return self.slug not in _disabled

    def get_prototype(self, slug):
        """The imported module of another prototype, or None if not installed."""
        return _modules.get(slug)

    def is_prototype_enabled(self, slug):
        """Is another prototype both installed and enabled?"""
        return slug in _modules and slug not in _disabled


def discover():
    """Find and mount every prototype; report any that fail to load."""
    _prototypes.clear()
    _modules.clear()
    for slug in sorted(os.listdir(HERE)):
        if not os.path.isfile(os.path.join(HERE, slug, "prototype.py")):
            continue
        try:
            _prototypes.append(_load_one(slug))
            print(f"  mounted /p/{slug}")
        except Exception:
            print(f"  FAILED to load prototype '{slug}':")
            traceback.print_exc()
    # restore which prototypes were turned off last time
    _disabled.clear()
    _disabled.update(_load_hub_settings())
    # now that every module + the disabled set are loaded, run optional hooks
    for slug, module in _modules.items():
        hook = getattr(module, "hub_init", None)
        if callable(hook):
            try:
                hook(HubContext(slug))
            except Exception:
                print(f"  hub_init failed for '{slug}':")
                traceback.print_exc()


# ---- hub routes ------------------------------------------------------------
@app.route("/")
def dashboard():
    return send_from_directory(HERE, "dashboard.html")


@app.route("/api/prototypes")
def prototypes():
    return jsonify([{**p, "enabled": p["slug"] not in _disabled} for p in _prototypes])


@app.route("/api/prototypes/<slug>/enabled", methods=["POST"])
def set_enabled(slug):
    """Enable/disable a prototype and persist the choice."""
    if slug not in {p["slug"] for p in _prototypes}:
        return jsonify({"ok": False, "error": "unknown prototype"}), 404
    enabled = bool((request.get_json(silent=True) or {}).get("enabled", True))
    with _settings_lock:
        _disabled.discard(slug) if enabled else _disabled.add(slug)
        _save_hub_settings()
    return jsonify({"ok": True, "slug": slug, "enabled": enabled})


def main():
    parser = argparse.ArgumentParser(description="Manual Override prototype hub")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print("Discovering prototypes:")
    discover()
    print(f"\nPrototype hub:  http://localhost:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
