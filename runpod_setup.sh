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

# -------------------------------------------------------------------
# Node 7 - external custom nodes + model weights
# -------------------------------------------------------------------
# Locked decision #10: Node 7 pins every weight in models.json with
# sha256 so reruns on a fresh pod are deterministic. ComfyUI-Manager
# is still the dev-convenience path for local workflow authoring.
NODE7_MODELS="$(pwd)/custom_nodes/node_07_pose_refiner/models.json"
export WORKSPACE="$(pwd)"
export COMFY_DIR

if [ -d "$COMFY_DIR" ] && [ -f "$NODE7_MODELS" ]; then
  echo "[animatic-refinement][node7] cloning external custom-node deps"
  python3 - <<'PY'
import json
import os
import subprocess
from pathlib import Path

comfy = Path(os.environ["COMFY_DIR"])
spec = json.loads(
    (
        Path(os.environ["WORKSPACE"])
        / "custom_nodes"
        / "node_07_pose_refiner"
        / "models.json"
    ).read_text()
)

custom_root = comfy / "custom_nodes"
custom_root.mkdir(parents=True, exist_ok=True)
for node in spec.get("customNodes", []):
    name = node["name"]
    target = custom_root / name
    if target.exists():
        print(f"[node7] custom-node '{name}' already present -- skipping clone")
        continue
    print(f"[node7] git clone {node['repoUrl']} -> {target}")
    subprocess.check_call(["git", "clone", "--depth", "1", node["repoUrl"], str(target)])

    # install each cloned node's own Python deps if it ships them
    req = target / "requirements.txt"
    if req.is_file():
        subprocess.check_call(["pip", "install", "-r", str(req)])
PY

  echo "[animatic-refinement][node7] downloading pinned model weights"
  python3 - <<'PY'
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

comfy = Path(os.environ["COMFY_DIR"])
spec_path = Path(os.environ["WORKSPACE"]) / "custom_nodes" / "node_07_pose_refiner" / "models.json"
spec = json.loads(spec_path.read_text())

errors = []
for model in spec.get("models", []):
    name = model["name"]
    url = model["url"]
    if not url or url.startswith("TODO"):
        print(f"[node7] {name}: URL is a TODO placeholder -- skipping (fill models.json before production)")
        continue
    dest = comfy / model["destination"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        print(f"[node7] {name}: already present at {dest}")
    else:
        print(f"[node7] {name}: downloading -> {dest}")
        try:
            subprocess.check_call(["curl", "-L", "--fail", "-o", str(dest), url])
        except subprocess.CalledProcessError as e:
            errors.append(f"{name}: curl failed ({e})")
            continue

    want = (model.get("sha256") or "").strip().lower()
    if not want:
        print(f"[node7] {name}: no sha256 pinned yet -- skipping verify (fill models.json after first known-good download)")
        continue
    hasher = hashlib.sha256()
    with dest.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(chunk)
    got = hasher.hexdigest()
    if got != want:
        errors.append(f"{name}: sha256 mismatch (want {want}, got {got}) at {dest}")

if errors:
    print("[node7] ERROR -- weight integrity/download failures:", file=sys.stderr)
    for err in errors:
        print(f"  - {err}", file=sys.stderr)
    sys.exit(1)
PY
else
  echo "[animatic-refinement][node7] skipping model pulls (no ComfyUI or no models.json)"
fi

echo "[animatic-refinement] setup complete"
