# hub — the engine

This folder is the **engine**: the content-free part that hosts *machines*. It
knows how to discover, mount, and serve them, but ships none itself. The machines
— the local content you build and edit — live in a separate folder (here,
`../prototypes/`).

This split is deliberate: the engine can be upgraded (or one day split into its
own repo) while your local machines and their runtime state stay untouched.

## What's here

| File             | Role                                                          |
|------------------|--------------------------------------------------------------|
| `__init__.py`    | the `Hub` class — discovery, blueprint mounting, dashboard    |
| `live.py`        | shared push helper (SSE); machines `import live`              |
| `dashboard.html` | the hub shell UI (one tab per machine)                       |
| `theme.css`      | shared visual theme, linked by every machine page            |
| `upgrade.py`     | update the engine in place from its upstream repo            |
| `hub.json`       | engine version + upstream repo/ref                          |

## Running

Use the local launcher next to your machines:

```bash
pip install -r requirements.txt
cd ../prototypes && python hub.py     # http://localhost:8000
```

Or embed the engine directly:

```python
from hub import Hub
Hub(machines_dir="/path/to/your/machines").run(port=8000)
```

`Hub` parameters: `machines_dir` (folder scanned for `<slug>/prototype.py`),
`settings_path` (enable/disable state; defaults to
`<machines_dir>/hub-settings.json`), `asset_dir` (where `dashboard.html` /
`theme.css` live; defaults to this engine).

## The machine contract

A machine is any sub-folder of the machines directory with a `prototype.py` that
defines:

```python
MANIFEST = { "name", "description", "default_page", "pages": [...] }
bp       = flask.Blueprint(...)   # pages + API, all paths relative to /p/<slug>/
```

Optional: a `hub_init(ctx)` hook receives a `HubContext` to see whether it (or
another machine) is enabled and to reach another machine's module for
cross-machine calls. Shared helpers like `live` are importable by plain name —
the engine puts this folder on `sys.path` during discovery.

## Upgrading the engine (pull updates into a consuming repo)

A consuming repo — e.g. a `gamemaster` repo with its own `prototypes/` and a
vendored copy of this `hub/` folder — keeps the engine current with `upgrade.py`.
Point `hub.json` at the upstream that holds the canonical engine:

```json
// pulling from the Manual Override monorepo (engine lives in its hub/ subdir):
{ "version": "0.1.0",
  "repo": "https://github.com/<you>/Manual-Override.git",
  "ref": "main", "engine_path": "hub" }

// or from a standalone engine repo (engine at the repo root):
{ "version": "0.1.0",
  "repo": "https://github.com/<you>/<engine-repo>.git",
  "ref": "main", "engine_path": "" }
```

Then:

```bash
python hub/upgrade.py --check       # report local vs. upstream version
python hub/upgrade.py               # pull the latest engine on the configured ref
python hub/upgrade.py --ref v0.2.0  # pin to a specific tag/branch/commit
```

`upgrade.py` rewrites only the files in this `hub/` folder; it preserves your
local `hub.json` source config (`repo`/`ref`/`engine_path`) while adopting the new
version, and never touches the machines or their state. A deployment is a git
checkout, so `git restore hub/` reverts an upgrade.

## Contributing engine changes back to Manual Override

The engine's single source of truth is Manual Override's `hub/`. Two ways to get
an engine fix there as a PR to `main`:

**Preferred — make the change upstream.** Fix it in a Manual Override clone, PR it,
merge, then `upgrade.py` it down into the consuming repo. Clean history, one source.

**Escape hatch — you already changed `hub/` in the consuming repo.** Copy just the
engine source into a Manual Override branch and open a PR (skip `hub.json`, which
holds consumer-local config, not engine source):

```bash
cd /path/to/Manual-Override
git checkout main && git pull
git checkout -b engine-<change>
rsync -a --delete --exclude hub.json --exclude __pycache__ \
      /path/to/gamemaster/hub/ hub/      # bring over your engine edits
# bump "version" in hub/hub.json if this is a release
git add hub/ && git commit -m "engine: <what changed>"
git push -u origin engine-<change>
gh pr create --base main --fill
```

The PR diff is exactly your engine changes — the machines in either repo are never
involved.

## Splitting into its own repo (later)

When the engine stabilises and several repos consume it, this folder becomes its
own repo whose root *is* these files (set `engine_path: ""` in consumers). At that
point `git subtree` or a submodule can replace the copy-based flow if you want
push/pull with full history. Until then, keep it in this monorepo so the engine
and the machines can evolve together in one commit.
