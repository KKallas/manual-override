"""
Local launcher — runs the hub engine over the machines in THIS folder.

The engine itself now lives in the sibling `hub/` package (the part that can be
upgraded / split into its own repo). This file is just the local entry point:
it points the engine at this `prototypes/` folder, which holds the machines —
the local content you edit. Enable/disable state persists to
`prototypes/hub-settings.json`.

Run:
    pip install -r requirements.txt
    python hub.py            # then open http://localhost:8000

Flags:
    --host  (default 0.0.0.0)
    --port  (default 8000)
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))   # the machines (prototypes) dir
ROOT = os.path.dirname(HERE)                         # repo root, holds hub/ engine
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hub import Hub


def main():
    parser = argparse.ArgumentParser(description="Manual Override — local hub launcher")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    Hub(machines_dir=HERE).run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
