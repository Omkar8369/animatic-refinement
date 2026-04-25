#!/usr/bin/env bash
# Fix `Node 'LineArtPreprocessor' not found` on RunPod by installing
# comfyui_controlnet_aux's missing Python deps into the SAME Python
# interpreter ComfyUI itself uses.
#
# Problem:
#   - The pod may have multiple Pythons on PATH (e.g. python3 -> 3.12,
#     but ComfyUI runs on 3.11.10 at /usr/local/bin/python3.11).
#   - `pip install scikit-image` goes to the default python3's
#     dist-packages, which ComfyUI can't see.
#   - comfyui_controlnet_aux silently partial-loads, and
#     LineArtPreprocessor / ScribblePreprocessor / DWPreprocessor never
#     register with ComfyUI.
#
# What this does:
#   1. Finds ComfyUI's Python by scanning /workspace/comfyui.log for the
#      version line, then maps to /usr/local/bin/python3.<N>.
#   2. Bootstraps pip into that interpreter if it's missing (ensurepip).
#   3. `pip install -r` the custom node's own requirements.txt into that
#      exact interpreter (no `pip` command involved -- we always use
#      `$COMFY_PY -m pip` so there's zero version ambiguity).
#   4. Verifies the import (`import skimage`) in that same Python.
#   5. Prints what to do next (restart ComfyUI).
#
# Run from the repo root on the pod:
#   cd /workspace/animatic-refinement
#   bash tools/pod_fix_controlnet_aux.sh

set -euo pipefail

LOG=/workspace/comfyui.log
CUSTOM_NODE=/workspace/ComfyUI/custom_nodes/comfyui_controlnet_aux

echo "[fix-cna] 1/5 finding ComfyUI's Python"

# Target Python version (ComfyUI's own). Read from its log if available,
# else default to 3.11. This is only used as a sanity filter against
# whatever binary we find.
PYVER="3.11"
if [ -f "$LOG" ]; then
  DETECTED=$(grep -Eo 'Python version: 3\.[0-9]+' "$LOG" | head -1 | awk '{print $3}' || true)
  if [ -n "$DETECTED" ]; then
    PYVER="$DETECTED"
  fi
fi
echo "[fix-cna]   target Python version: $PYVER"

COMFY_PY=""

# --- Strategy A: find the Python process listening on 8188 via /proc ---
# This is the most authoritative: whatever binary the live ComfyUI runs
# under is, by definition, the one we need to pip-install into.
PID=""
if command -v lsof >/dev/null 2>&1; then
  PID=$(lsof -tiTCP:8188 -sTCP:LISTEN 2>/dev/null | head -1 || true)
fi
if [ -z "$PID" ] && command -v ss >/dev/null 2>&1; then
  PID=$(ss -lntpH 'sport = :8188' 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true)
fi
if [ -z "$PID" ] && command -v fuser >/dev/null 2>&1; then
  PID=$(fuser 8188/tcp 2>/dev/null | tr -s ' ' | awk '{print $NF}' || true)
fi

# Strategy A2: scan /proc cmdlines for ComfyUI's main.py
if [ -z "$PID" ]; then
  for p in /proc/[0-9]*/cmdline; do
    [ -r "$p" ] || continue
    if grep -aq "ComfyUI.*main\.py\|main\.py.*--listen\|main\.py.*--port" "$p" 2>/dev/null; then
      CAND_PID=$(basename "$(dirname "$p")")
      if [ -r "/proc/$CAND_PID/exe" ]; then
        PID="$CAND_PID"
        break
      fi
    fi
  done
fi

if [ -n "$PID" ] && [ -r "/proc/$PID/exe" ]; then
  RESOLVED=$(readlink "/proc/$PID/exe" 2>/dev/null || true)
  if [ -n "$RESOLVED" ] && [ -x "$RESOLVED" ]; then
    COMFY_PY="$RESOLVED"
    echo "[fix-cna]   found ComfyUI PID $PID -> $COMFY_PY"
  fi
fi

# --- Strategy B: scan known install locations ---
if [ -z "$COMFY_PY" ]; then
  echo "[fix-cna]   no live process match; scanning known install paths"
  CANDIDATES=(
    "/usr/local/bin/python${PYVER}"
    "/usr/bin/python${PYVER}"
    "/opt/python${PYVER}/bin/python${PYVER}"
    "/opt/venv/bin/python"
    "/opt/venv/bin/python${PYVER}"
    "/opt/conda/bin/python"
    "/opt/conda/envs/comfyui/bin/python"
    "/workspace/venv/bin/python"
    "/workspace/ComfyUI/venv/bin/python"
    "/root/.pyenv/versions/${PYVER}/bin/python"
    "/root/.pyenv/shims/python${PYVER}"
    "$(command -v python${PYVER} 2>/dev/null || true)"
  )
  for c in "${CANDIDATES[@]}"; do
    if [ -n "$c" ] && [ -x "$c" ]; then
      ACTUAL=$("$c" -V 2>&1 | grep -oE '3\.[0-9]+' | head -1 || true)
      if [ "$ACTUAL" = "$PYVER" ]; then
        COMFY_PY="$c"
        echo "[fix-cna]   found $COMFY_PY (Python $ACTUAL)"
        break
      fi
    fi
  done
fi

# --- Strategy C: last-resort filesystem scan ---
if [ -z "$COMFY_PY" ]; then
  echo "[fix-cna]   still nothing; doing a limited filesystem scan (<30s)"
  # Limit to common install roots + max depth to keep this fast
  for root in /opt /usr /root /workspace; do
    [ -d "$root" ] || continue
    while IFS= read -r c; do
      [ -x "$c" ] || continue
      ACTUAL=$("$c" -V 2>&1 | grep -oE '3\.[0-9]+' | head -1 || true)
      if [ "$ACTUAL" = "$PYVER" ]; then
        COMFY_PY="$c"
        echo "[fix-cna]   found $COMFY_PY (Python $ACTUAL)"
        break 2
      fi
    done < <(find "$root" -maxdepth 6 -name "python${PYVER}" -o -name "python" 2>/dev/null | head -30)
  done
fi

if [ -z "$COMFY_PY" ] || [ ! -x "$COMFY_PY" ]; then
  echo "" >&2
  echo "[fix-cna] ERROR: could not locate ComfyUI's Python interpreter." >&2
  echo "[fix-cna] Please paste the output of these commands so we can find it:" >&2
  echo "" >&2
  echo "    ls -la /proc/*/exe 2>/dev/null | grep -i python | head -10" >&2
  echo "    find / -maxdepth 7 -name 'python3*' -type f -executable 2>/dev/null | head -20" >&2
  echo "    lsof -iTCP:8188 -sTCP:LISTEN 2>/dev/null || ss -lntp 2>/dev/null | grep 8188" >&2
  echo "" >&2
  exit 1
fi

echo "[fix-cna]   using interpreter: $COMFY_PY"
"$COMFY_PY" -V

echo ""
echo "[fix-cna] 2/5 checking pip availability in that interpreter"
if "$COMFY_PY" -m pip --version >/dev/null 2>&1; then
  echo "[fix-cna]   pip present: $("$COMFY_PY" -m pip --version)"
else
  echo "[fix-cna]   pip missing, bootstrapping via ensurepip"
  "$COMFY_PY" -m ensurepip --upgrade
  "$COMFY_PY" -m pip install --upgrade pip
fi

echo ""
echo "[fix-cna] 3/5 installing controlnet_aux requirements into $COMFY_PY"
if [ ! -f "$CUSTOM_NODE/requirements.txt" ]; then
  echo "[fix-cna] ERROR: $CUSTOM_NODE/requirements.txt not found" >&2
  exit 1
fi
"$COMFY_PY" -m pip install -r "$CUSTOM_NODE/requirements.txt"

echo ""
echo "[fix-cna] 4/5 verifying imports in $COMFY_PY"
"$COMFY_PY" - <<'PY'
import importlib
failed = []
for mod in ("skimage", "cv2", "numpy", "onnxruntime"):
    try:
        m = importlib.import_module(mod)
        ver = getattr(m, "__version__", "?")
        print(f"  OK  {mod} == {ver}")
    except Exception as e:
        print(f"  FAIL {mod}: {type(e).__name__}: {e}")
        failed.append(mod)
if failed:
    print(f"\n[fix-cna] still missing: {failed}")
    raise SystemExit(1)
print("\n[fix-cna] all required deps importable")
PY

echo ""
echo "[fix-cna] 5/5 verifying controlnet_aux's lineart submodule loads"
"$COMFY_PY" - <<'PY'
import sys
sys.path.insert(0, "/workspace/ComfyUI")
try:
    from custom_nodes.comfyui_controlnet_aux.node_wrappers import lineart
    klass = [k for k in dir(lineart) if "preprocessor" in k.lower()]
    print(f"[fix-cna]   lineart module classes: {klass}")
except Exception as e:
    import traceback
    print(f"[fix-cna]   FAIL lineart import: {type(e).__name__}: {e}")
    traceback.print_exc()
    raise SystemExit(1)

try:
    from custom_nodes.comfyui_controlnet_aux.node_wrappers import scribble
    klass = [k for k in dir(scribble) if "preprocessor" in k.lower()]
    print(f"[fix-cna]   scribble module classes: {klass}")
except Exception as e:
    import traceback
    print(f"[fix-cna]   FAIL scribble import: {type(e).__name__}: {e}")
    traceback.print_exc()
    raise SystemExit(1)
PY

echo ""
cat <<EOF
[fix-cna] ==========================================================
[fix-cna] DONE. Deps installed into $COMFY_PY.
[fix-cna]
[fix-cna] But ComfyUI has already booted WITHOUT these deps. You must
[fix-cna] restart ComfyUI so it re-imports the custom nodes:
[fix-cna]
[fix-cna]   EASIEST: in the RunPod dashboard, click "Restart Pod"
[fix-cna]            (top-right of the pod card). Takes 1-2 minutes.
[fix-cna]
[fix-cna] After the pod is back, re-run Node 7:
[fix-cna]   cd /workspace/animatic-refinement
[fix-cna]   python3 run_node7.py \\
[fix-cna]       --node6-result /workspace/smoke_workdir/work/node6_result.json \\
[fix-cna]       --queue        /workspace/smoke_workdir/input/queue.json
[fix-cna] ==========================================================
EOF
