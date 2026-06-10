# Updating & fixing the hub engine

Quick reference. Full details in [README.md](README.md).

The engine (this `hub/` folder) is shared. Its home is **Manual Override's `hub/`**.
A repo that uses it (e.g. a `gamemaster` repo) keeps a vendored copy and points
`hub.json` at the upstream:

```json
{ "repo": "https://github.com/<you>/Manual-Override.git",
  "ref": "main", "engine_path": "hub" }
```

## Pull the latest engine

Updates only `hub/` — your `prototypes/` and their state are never touched.

```bash
python hub/upgrade.py --check     # is a newer engine available?
python hub/upgrade.py             # apply it, then restart the hub
```

Undo: `git restore hub/`.

## Push a fix back to Manual Override

Best: make the fix in a Manual Override clone, PR it, then `upgrade.py` it down.

Already fixed it here? Copy the engine source into a Manual Override branch and PR
(skip `hub.json` — it's local config, not engine source):

```bash
cd /path/to/Manual-Override
git checkout main && git pull && git checkout -b engine-<fix>
rsync -a --delete --exclude hub.json --exclude __pycache__ \
      /path/to/your-repo/hub/ hub/
git add hub/ && git commit -m "engine: <what changed>"
git push -u origin engine-<fix> && gh pr create --base main --fill
```

Bump `"version"` in `hub/hub.json` in that PR if it's a release.
