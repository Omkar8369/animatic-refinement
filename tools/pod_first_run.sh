#!/usr/bin/env bash
# One-shot bootstrap for a Node 7 smoke run on a RunPod pod that
# already has ComfyUI + the two external custom nodes + SD 1.5 models
# on disk, but under DIFFERENT filenames than our workflow expects.
#
# What this script does:
#   1. Creates filename aliases (symlinks) in /workspace/ComfyUI/models
#      so the names referenced by workflow.json and
#      workflow_lineart_fallback.json resolve on this pod.
#   2. Generates a synthetic Node 6 scaffold at /workspace/smoke_workdir
#      via tools/make_smoke_node6_workdir.py.
#   3. Prints the exact Node 7 invocation to run next.
#
# Safe to re-run: every step is idempotent (ln -sf, mkdir -p, --force).
#
# Assumes this is executed from the repo root on the pod:
#   cd /workspace/animatic-refinement
#   bash tools/pod_first_run.sh

set -euo pipefail

COMFY_MODELS="${COMFY_MODELS:-/workspace/ComfyUI/models}"
WORK_ROOT="${WORK_ROOT:-/workspace/smoke_workdir}"

echo "[pod-first-run] ComfyUI models root: $COMFY_MODELS"
echo "[pod-first-run] smoke work dir:       $WORK_ROOT"
echo ""

# ---------------------------------------------------------------------
# 1. Filename aliases for checkpoints + LoRA the workflow expects.
#    Edit the RHS of each alias if your pod has a different filename.
# ---------------------------------------------------------------------

declare -A CHECKPOINT_ALIASES=(
  ["anyloraCheckpoint_bakedvaeBlessedFp16.safetensors"]="flat2DAnimerge_v45Sharp.safetensors"
)
declare -A LORA_ALIASES=(
  ["bnw_lineart_v1.safetensors"]="thick_line_cartoon_lora.safetensors"
)

link_alias() {
  local dir="$1"
  local want="$2"
  local have="$3"

  if [ ! -f "$dir/$have" ]; then
    echo "[pod-first-run]   SKIP $dir/$want -> $have  (target file missing)"
    return 0
  fi
  if [ -e "$dir/$want" ] && [ ! -L "$dir/$want" ]; then
    echo "[pod-first-run]   SKIP $dir/$want (real file already exists, not clobbering)"
    return 0
  fi
  ln -sfn "$have" "$dir/$want"
  echo "[pod-first-run]   OK   $dir/$want -> $have"
}

echo "[pod-first-run] linking checkpoint aliases:"
for want in "${!CHECKPOINT_ALIASES[@]}"; do
  link_alias "$COMFY_MODELS/checkpoints" "$want" "${CHECKPOINT_ALIASES[$want]}"
done
echo ""

echo "[pod-first-run] linking LoRA aliases:"
for want in "${!LORA_ALIASES[@]}"; do
  link_alias "$COMFY_MODELS/loras" "$want" "${LORA_ALIASES[$want]}"
done
echo ""

# ---------------------------------------------------------------------
# 2. Synthetic Node 6 work dir so Node 7 has something to chew on.
# ---------------------------------------------------------------------

echo "[pod-first-run] generating smoke Node-6 work dir at $WORK_ROOT"
python3 tools/make_smoke_node6_workdir.py --work-dir "$WORK_ROOT" --force
echo ""

# ---------------------------------------------------------------------
# 3. Print the next command.
# ---------------------------------------------------------------------

cat <<EOF
[pod-first-run] ==========================================================
[pod-first-run] READY. Try a dry run first to confirm the manifest layer:
[pod-first-run]
    python3 run_node7.py --dry-run \\
        --node6-result $WORK_ROOT/work/node6_result.json \\
        --queue        $WORK_ROOT/input/queue.json
[pod-first-run]
[pod-first-run] Then the real one (hits ComfyUI on port 8188):
[pod-first-run]
    python3 run_node7.py \\
        --node6-result $WORK_ROOT/work/node6_result.json \\
        --queue        $WORK_ROOT/input/queue.json
[pod-first-run]
[pod-first-run] Results will land in:
[pod-first-run]   $WORK_ROOT/work/node7_result.json   (aggregate)
[pod-first-run]   $WORK_ROOT/work/shot_001/refined_map.json
[pod-first-run]   $WORK_ROOT/work/shot_001/refined/*.png
[pod-first-run] ==========================================================
EOF
