#!/usr/bin/env bash
# One-shot setup script for a fresh RunPod pod.
# Assumes: Ubuntu-ish base image, Python 3.10+, CUDA drivers already present.

set -euo pipefail

echo "[animatic-refinement] system deps"
apt-get update -y
apt-get install -y --no-install-recommends ffmpeg git-lfs
git lfs install || true

echo "[animatic-refinement] python deps"
pip install --upgrade pip
# Root requirements.txt is an aggregator that -r-includes every per-node
# requirements file. Add a new -r line there when a node ships Python deps.
pip install -r requirements.txt

# Expect ComfyUI to live at /workspace/ComfyUI (RunPod ComfyUI template default).
COMFY_DIR="${COMFY_DIR:-/workspace/ComfyUI}"
if [ -d "$COMFY_DIR" ]; then
  echo "[animatic-refinement] linking custom_nodes into ComfyUI at $COMFY_DIR"
  ln -sfn "$(pwd)/custom_nodes" "$COMFY_DIR/custom_nodes/animatic_refinement"
else
  echo "[animatic-refinement] ComfyUI dir not found at $COMFY_DIR - skipping link step"
  echo "  set COMFY_DIR env var and re-run, or symlink custom_nodes manually"
fi

echo "[animatic-refinement] setup complete"
