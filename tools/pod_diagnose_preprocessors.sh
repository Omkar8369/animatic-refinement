#!/usr/bin/env bash
# Diagnostic for Node 7's "Node 'LineArtPreprocessor' not found" error.
#
# Answers three questions:
#   1. Is ComfyUI actually running a fresh process, or is the old one
#      still up (i.e. the pod "restart" didn't really restart)?
#   2. What classes are registered in the currently-running ComfyUI?
#      (Queries /object_info; this is ground truth.)
#   3. Did comfyui_controlnet_aux emit any import errors in the log?
#
# Run from repo root on the pod:
#   cd /workspace/animatic-refinement
#   bash tools/pod_diagnose_preprocessors.sh

set -uo pipefail

LOG=/workspace/comfyui.log
COMFY_URL="${COMFY_URL:-http://127.0.0.1:8188}"

echo "============================================================"
echo " 1. Process listening on 8188 -- when did it start?"
echo "============================================================"

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

if [ -n "$PID" ]; then
  echo "  PID=$PID"
  echo "  /proc/$PID/exe -> $(readlink /proc/$PID/exe 2>/dev/null || echo '?')"
  ps -o pid,lstart,etime,cmd -p "$PID" 2>/dev/null || true
  echo ""
  echo "  [interpretation] If 'etime' (elapsed time) is measured in DAYS,"
  echo "  the pod wasn't actually restarted after the deps install --"
  echo "  the old broken ComfyUI process is still alive."
else
  echo "  (no local PID found on 8188 -- port tools don't see the process)"
  echo ""
  echo "  This does NOT necessarily mean ComfyUI is down. On some RunPod"
  echo "  container layouts the ComfyUI process runs in a different"
  echo "  network namespace from this shell, so lsof/ss can't see it"
  echo "  even though HTTP still reaches it. Section 2 will verify via"
  echo "  an actual HTTP request."
fi
echo ""

echo "============================================================"
echo " 2. Registered preprocessor classes (ground truth from"
echo "    /object_info)"
echo "============================================================"

TMP_JSON=$(mktemp)
HTTP_CODE=$(curl -s --max-time 15 -o "$TMP_JSON" -w "%{http_code}" "$COMFY_URL/object_info" || echo "000")

if [ "$HTTP_CODE" != "200" ]; then
  echo "  ERROR: /object_info returned HTTP $HTTP_CODE"
  echo "  body: $(head -c 500 "$TMP_JSON" 2>/dev/null)"
  echo ""
  echo "  HTTP 000 == curl couldn't connect at all. Likely means ComfyUI"
  echo "  really is down OR is bound to a different interface than"
  echo "  127.0.0.1. Try the RunPod dashboard's 'ComfyUI' link to see"
  echo "  if the web UI loads; if it does, ComfyUI is up but bound to"
  echo "  0.0.0.0 + proxied externally only."
  rm -f "$TMP_JSON"
  exit 1
fi

echo "  /object_info returned HTTP 200"
python3 - "$TMP_JSON" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
keys = sorted(d.keys())
print(f"  Total registered classes: {len(keys)}")
print()
print("  --- LineArt / Scribble / DWPose / generic preprocessor matches ---")
matches = [
    k for k in keys
    if any(s in k.lower() for s in
           ("lineart", "scribble", "dwpose", "preprocessor", "pidinet", "xdog"))
]
if not matches:
    print("    (NONE -- comfyui_controlnet_aux did not register anything)")
else:
    for m in matches:
        print(f"    {m}")
print()
print("  --- IPAdapter / ControlNet loader nodes (sanity check) ---")
other = [k for k in keys if "ipadapter" in k.lower() or k.lower().startswith("controlnet")]
for m in other[:20]:
    print(f"    {m}")
PY

rm -f "$TMP_JSON"
echo ""

echo "============================================================"
echo " 3. comfyui_controlnet_aux startup log lines"
echo "============================================================"
if [ ! -f "$LOG" ]; then
  echo "  (no $LOG found)"
else
  echo "  -- most recent controlnet_aux + import-error lines --"
  grep -E "controlnet_aux|IMPORT FAILED|ModuleNotFoundError|Traceback" "$LOG" 2>/dev/null | tail -40 || echo "  (no matches)"
fi
echo ""
echo "============================================================"
echo " Interpretation guide"
echo "============================================================"
cat <<'EOF'
  If section 2 shows ~500+ total classes AND lists lineart/scribble
  preprocessors -> controlnet_aux loaded. Paste the exact preprocessor
  names back; we'll patch workflow.json to match.

  If section 2 shows ~200 classes and ZERO preprocessor matches ->
  controlnet_aux silently failed again. Check section 3 for the
  import error; section 1's etime will also tell us whether the pod
  actually restarted or not.

  If section 1's etime is DAYS -> pod was NOT restarted; the live
  ComfyUI is still the original broken process. Restart for real
  (dashboard -> Restart Pod) and rerun this script.
EOF
