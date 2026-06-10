"""
Upgrade the hub engine in place from its upstream git repo.

This updates ONLY the engine — the files in this `hub/` folder. It never touches
your local content: the `prototypes/` machines, their `*.json` runtime state, or
`hub-settings.json` all stay exactly as they are. That is the whole point of the
engine/local split: you can pull a newer engine under an unchanged local setup.

How it works:
  1. Reads the upstream repo URL + ref from `hub.json` (next to this file).
  2. Shallow-clones that repo (whose root IS the engine — the contents of this
     folder) into a temp dir.
  3. Copies the engine files over this folder, preserving your local `hub.json`
     settings (repo/ref) while adopting the new version number.

Until you've created the engine repo, set "repo" in hub.json to its git URL.

Usage:
    python upgrade.py            # upgrade to the latest engine on the configured ref
    python upgrade.py --check    # report local vs. upstream version, change nothing
    python upgrade.py --ref v0.2.0   # upgrade to a specific tag/branch/commit

Safety net: a deployment is a git checkout, so `git checkout -- hub/` (or
`git restore hub/`) reverts an upgrade you don't want.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HUB_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HUB_DIR, "hub.json")

# Never copy these in from upstream / never let them be clobbered.
SKIP_NAMES = {".git", "__pycache__", ".DS_Store"}


def _load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read {CONFIG}: {e}")


def _version_of(path):
    try:
        with open(os.path.join(path, "hub.json")) as f:
            return json.load(f).get("version", "?")
    except (OSError, ValueError):
        return "?"


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _clone(repo, ref, dest):
    """Shallow-clone repo@ref into dest. Falls back to full clone + checkout if
    ref is a commit that --branch can't resolve."""
    try:
        _git("clone", "--depth", "1", "--branch", ref, repo, dest)
    except subprocess.CalledProcessError:
        shutil.rmtree(dest, ignore_errors=True)
        _git("clone", repo, dest)
        _git("checkout", ref, cwd=dest)


def _sync_engine(src, local_cfg):
    """Copy engine files from src over HUB_DIR, preserving local hub.json
    repo/ref but adopting the upstream version."""
    new_cfg = None
    for name in os.listdir(src):
        if name in SKIP_NAMES:
            continue
        s = os.path.join(src, name)
        d = os.path.join(HUB_DIR, name)
        if name == "hub.json":
            # merge: keep local source config (repo/ref/engine_path), take upstream version
            with open(s) as f:
                upstream = json.load(f)
            new_cfg = {**upstream,
                       "repo": local_cfg.get("repo", ""),
                       "ref": local_cfg.get("ref", "main"),
                       "engine_path": local_cfg.get("engine_path", "")}
            continue
        if os.path.isdir(s):
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(s, d, ignore=shutil.ignore_patterns(*SKIP_NAMES))
        else:
            shutil.copy2(s, d)
    if new_cfg is not None:
        with open(CONFIG, "w") as f:
            json.dump(new_cfg, f, indent=2)
            f.write("\n")


def main():
    parser = argparse.ArgumentParser(description="Upgrade the hub engine in place")
    parser.add_argument("--check", action="store_true",
                        help="report local vs. upstream version; change nothing")
    parser.add_argument("--ref", help="git tag/branch/commit to upgrade to (overrides hub.json)")
    args = parser.parse_args()

    cfg = _load_config()
    repo = cfg.get("repo", "").strip()
    ref = args.ref or cfg.get("ref", "main")
    local_version = cfg.get("version", "?")

    if not repo:
        print("No upstream repo configured.")
        print(f'Set "repo" in {CONFIG} to the engine\'s upstream:')
        print('  - from the Manual Override monorepo (engine in its hub/ subdir):')
        print('      "repo": "https://github.com/<you>/Manual-Override.git", "engine_path": "hub"')
        print('  - from a standalone engine repo (engine at the repo root):')
        print('      "repo": "https://github.com/<you>/<engine-repo>.git", "engine_path": ""')
        return

    # The engine may be the upstream repo's root ("") or a subdir of it (e.g.
    # "hub" when pulling from the Manual Override monorepo).
    engine_path = cfg.get("engine_path", "").strip().strip("/")

    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "engine")
        print(f"Fetching {repo}@{ref}" + (f" (subdir '{engine_path}')" if engine_path else "") + " ...")
        try:
            _clone(repo, ref, dest)
        except subprocess.CalledProcessError as e:
            sys.exit(f"git failed: {e}")
        src = os.path.join(dest, engine_path) if engine_path else dest
        if not os.path.isfile(os.path.join(src, "hub.json")):
            sys.exit(f"no engine found at '{engine_path or '.'}' in {repo} "
                     f"(expected a hub.json there) — check engine_path in {CONFIG}")
        remote_version = _version_of(src)

        if args.check:
            same = local_version == remote_version
            print(f"local:  {local_version}")
            print(f"remote: {remote_version}  ({ref})")
            print("up to date." if same else "an upgrade is available — run without --check to apply.")
            return

        print(f"Upgrading engine {local_version} -> {remote_version} ...")
        _sync_engine(src, cfg)
        print("Done. Engine updated; your local machines and settings are untouched.")
        print("Restart the hub to load the new engine.")


if __name__ == "__main__":
    main()
