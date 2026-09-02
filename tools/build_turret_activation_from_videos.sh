#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
SWIFT_BIN=${SWIFT_BIN:-swift}
FRAME_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/turret-activation-frames.XXXXXX")
MODULE_CACHE="${TMPDIR:-/tmp}/turret-swift-module-cache"

cleanup() {
  rm -rf -- "$FRAME_ROOT"
}
trap cleanup EXIT INT TERM

mkdir -p "$MODULE_CACHE"
export CLANG_MODULE_CACHE_PATH="$MODULE_CACHE"
export SWIFT_MODULECACHE_PATH="$MODULE_CACHE"

extract() {
  tower_type=$1
  "$SWIFT_BIN" "$PROJECT_ROOT/tools/extract_video_frames.swift" \
    "$PROJECT_ROOT/assets/game-art/z-pixel-v2/source-videos/runtime-activation/${tower_type}-activation-source-v2.mp4" \
    "$FRAME_ROOT/$tower_type"
}

extract machine-gun
extract flamethrower
extract mortar
extract tesla-coil

"$PYTHON_BIN" "$PROJECT_ROOT/tools/build_turret_activation_assets.py" \
  --frames-root "$FRAME_ROOT"
