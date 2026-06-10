"""
The hub engine — one Flask server that hosts a folder of "machines".

A *machine* (historically called a prototype) is any sub-folder of the machines
directory containing a `prototype.py` that defines:

    MANIFEST = { "name", "description", "default_page", "pages": [...] }
    bp       = flask.Blueprint(...)   # its pages + API, all paths relative

On startup the hub scans the machines directory, mounts each machine's blueprint
under `/p/<slug>`, and serves a dashboard at `/` with one tab per machine. Pages
flagged `newtab` in a manifest open in their own browser tab.

This package is the reusable engine. It is content-free: it knows how to host
machines but ships none. A deployment points it at a local machines directory:

    from hub import Hub
    Hub(machines_dir="/path/to/prototypes").run()

Shared helpers that machines import by plain name (e.g. `import live`) live in
this package's directory, which the engine puts on sys.path during discovery —
so machines need no knowledge of where the engine lives.

See README.md for the full contract; see upgrade.py to update the engine in place.
"""

import importlib.util
import json
import os
import sys
import threading
import traceback

from flask import Flask, jsonify, request, send_from_directory

# Directory of this engine package. Holds dashboard.html, theme.css, live.py.
HUB_DIR = os.path.dirname(os.path.abspath(__file__))


class HubContext:
    """Handed to a machine's optional hub_init(ctx) hook. Lets a machine see
    whether it (or another machine) is enabled, and reach another machine's
    module to call its programmatic API. Looks are live — they reflect the
    current enable/disable state, which can change at runtime."""

    def __init__(self, hub, slug):
        self._hub = hub
        self.slug = slug

    def is_enabled(self):
        """Is THIS machine currently enabled in the hub?"""
        return self.slug not in self._hub._disabled

    def get_prototype(self, slug):
        """The imported module of another machine, or None if not installed."""
        return self._hub._modules.get(slug)

    def is_prototype_enabled(self, slug):
        """Is another machine both installed and enabled?"""
        return slug in self._hub._modules and slug not in self._hub._disabled


class Hub:
    """A server that hosts the machines found in `machines_dir`.

    machines_dir : folder scanned for `<slug>/prototype.py` machines (local content).
    settings_path: where enable/disable state is persisted. Defaults to
                   `<machines_dir>/hub-settings.json` so local state stays local.
    asset_dir    : where dashboard.html / theme.css live. Defaults to the engine.
    """

    def __init__(self, machines_dir, settings_path=None, asset_dir=None):
        self.machines_dir = os.path.abspath(machines_dir)
        self.settings_path = settings_path or os.path.join(self.machines_dir, "hub-settings.json")
        self.asset_dir = asset_dir or HUB_DIR

        self.app = Flask(__name__)
        self._prototypes = []   # discovered + mounted, in display order
        self._modules = {}      # slug -> imported machine module
        # We persist the *disabled* slugs (default = everything enabled).
        self._disabled = set()
        self._settings_lock = threading.Lock()

        self._add_routes()

    # ---- settings persistence ----------------------------------------------
    def _load_settings(self):
        try:
            with open(self.settings_path) as f:
                return set(json.load(f).get("disabled", []))
        except (OSError, ValueError, TypeError):
            return set()

    def _save_settings(self):
        try:
            with open(self.settings_path, "w") as f:
                json.dump({"disabled": sorted(self._disabled)}, f, indent=2)
        except OSError:
            pass

    # ---- discovery ----------------------------------------------------------
    def _load_one(self, slug):
        """Import <machines_dir>/<slug>/prototype.py and register its blueprint."""
        path = os.path.join(self.machines_dir, slug, "prototype.py")
        modname = f"proto_{slug.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(modname, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        # Put the machine's own folder on sys.path while it imports, so it can
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
        # name= overrides the blueprint's own name so two machines can't collide.
        self.app.register_blueprint(bp, url_prefix=prefix, name=modname)
        self._modules[slug] = module

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

    def discover(self):
        """Find and mount every machine; report any that fail to load."""
        # Make shared engine helpers (e.g. live.py) importable by plain name
        # from inside machine modules, without the machine knowing where the
        # engine lives.
        if HUB_DIR not in sys.path:
            sys.path.insert(0, HUB_DIR)

        self._prototypes.clear()
        self._modules.clear()
        for slug in sorted(os.listdir(self.machines_dir)):
            if not os.path.isfile(os.path.join(self.machines_dir, slug, "prototype.py")):
                continue
            try:
                self._prototypes.append(self._load_one(slug))
                print(f"  mounted /p/{slug}")
            except Exception:
                print(f"  FAILED to load machine '{slug}':")
                traceback.print_exc()
        # restore which machines were turned off last time
        self._disabled.clear()
        self._disabled.update(self._load_settings())
        # now that every module + the disabled set are loaded, run optional hooks
        for slug, module in self._modules.items():
            hook = getattr(module, "hub_init", None)
            if callable(hook):
                try:
                    hook(HubContext(self, slug))
                except Exception:
                    print(f"  hub_init failed for '{slug}':")
                    traceback.print_exc()

    # ---- routes -------------------------------------------------------------
    def _add_routes(self):
        app = self.app

        @app.after_request
        def _no_html_cache(resp):
            """Never cache machine pages (their app code is inline), so editing a
            controller/screen and reloading always picks up the new version — no
            stale 'why isn't my change showing' from a cached tab."""
            if resp.mimetype == "text/html":
                resp.headers["Cache-Control"] = "no-store, max-age=0"
            return resp

        @app.route("/")
        def dashboard():
            return send_from_directory(self.asset_dir, "dashboard.html")

        @app.route("/theme.css")
        def theme_css():
            """Shared visual theme, linked by every machine page for a uniform look."""
            return send_from_directory(self.asset_dir, "theme.css")

        @app.route("/api/prototypes")
        def prototypes():
            return jsonify([{**p, "enabled": p["slug"] not in self._disabled} for p in self._prototypes])

        @app.route("/api/prototypes/<slug>/enabled", methods=["POST"])
        def set_enabled(slug):
            """Enable/disable a machine and persist the choice."""
            if slug not in {p["slug"] for p in self._prototypes}:
                return jsonify({"ok": False, "error": "unknown machine"}), 404
            enabled = bool((request.get_json(silent=True) or {}).get("enabled", True))
            with self._settings_lock:
                self._disabled.discard(slug) if enabled else self._disabled.add(slug)
                self._save_settings()
            return jsonify({"ok": True, "slug": slug, "enabled": enabled})

    # ---- run ----------------------------------------------------------------
    def run(self, host="0.0.0.0", port=8000):
        print("Discovering machines:")
        self.discover()
        print(f"\nHub:  http://localhost:{port}")
        self.app.run(host=host, port=port, threaded=True)
