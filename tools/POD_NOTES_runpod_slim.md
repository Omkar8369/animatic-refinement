# RunPod pod bringup notes — "runpod-slim" image

The user's pod image (`runpod-slim`) is a **trimmed** ComfyUI layout that
doesn't match the shape `runpod_setup.sh` assumes. This file captures the
exact 3-step fix the first live Node 7 run needed (2026-04-25) so the
next pod bringup is copy-paste, not re-debug.

If the pod image changes, re-verify; the learnings below are specific to
this persistent-volume setup.

## Symptom

`runpod_setup.sh` completes without error, but `run_node7.py` fails with
HTTP 400s from ComfyUI like:

- `Node 'LineArtPreprocessor' not found`
- `ckpt_name: 'anyloraCheckpoint_bakedvaeBlessedFp16.safetensors' not in []`

## Why

**Two ComfyUI installations coexist on the same persistent volume:**

| Path                                | What it has                                    |
|-------------------------------------|------------------------------------------------|
| `/workspace/ComfyUI/`               | Older install. Has all weights in `models/`, and `comfyui_controlnet_aux` under `custom_nodes/`. |
| `/workspace/runpod-slim/ComfyUI/`   | What `/start.sh` actually launches. Has a venv at `.venv-cu128/`, but its `custom_nodes/` has only 6 baked nodes and its `models/` is empty. |

`/start.sh`:
- Sources `/workspace/runpod-slim/ComfyUI/.venv-cu128/bin/activate` (venv
  created with `--system-site-packages`, so `torch`/`numpy` come from
  `/usr/local/lib/python3.12/dist-packages`).
- `cd /workspace/runpod-slim/ComfyUI && python main.py --listen 0.0.0.0
  --port 8188 --enable-cors-header`. No supervisor; if the child dies,
  `/start.sh` falls through to `sleep infinity`.

ComfyUI thus scans
`/workspace/runpod-slim/ComfyUI/custom_nodes/` (missing controlnet_aux)
and `/workspace/runpod-slim/ComfyUI/models/` (empty). The older install's
content is invisible to it.

Our `runpod_setup.sh` historically cloned `comfyui_controlnet_aux` into
`/workspace/ComfyUI/custom_nodes/` and pointed pip at the wrong
interpreter, compounding the confusion.

## Fix (4 shell commands + 1 restart, + 1 system-python install for Node 11)

Run all four from the pod, as root, once per fresh pod:

```bash
# 1. Bridge comfyui_controlnet_aux into the runpod-slim install.
#    (custom-nodes that runpod_setup.sh clones should land here instead
#    of /workspace/ComfyUI/custom_nodes/.)
ln -sfn /workspace/ComfyUI/custom_nodes/comfyui_controlnet_aux \
        /workspace/runpod-slim/ComfyUI/custom_nodes/comfyui_controlnet_aux

# 2. Install controlnet_aux's Python deps into the RUNNING venv's Python.
#    The venv inherits from system dist-packages but needs these added.
#    matplotlib + scikit-image + onnxruntime are required for the DWPose
#    preprocessor specifically — without them ComfyUI silently skips
#    registering DWPreprocessor (visible only as 977-vs-984 class count
#    in /object_info; comfyui.log shows the import failure).
VENV_PY=/workspace/runpod-slim/ComfyUI/.venv-cu128/bin/python
"$VENV_PY" -m pip install -r \
  /workspace/ComfyUI/custom_nodes/comfyui_controlnet_aux/requirements.txt
"$VENV_PY" -m pip install matplotlib scikit-image onnxruntime ultralytics
# matplotlib + scikit-image + onnxruntime: DWPose preprocessor
# ultralytics: Impact-Pack subpack (not on Node 7's path, but commonly needed)

# 3. Tell runpod-slim ComfyUI where the weights live. ComfyUI auto-reads
#    extra_model_paths.yaml at its root on startup.
cat > /workspace/runpod-slim/ComfyUI/extra_model_paths.yaml <<'YAML'
old_install:
  base_path: /workspace/ComfyUI/
  checkpoints: models/checkpoints/
  clip: models/clip/
  clip_vision: models/clip_vision/
  configs: models/configs/
  controlnet: models/controlnet/
  embeddings: models/embeddings/
  ipadapter: models/ipadapter/
  loras: models/loras/
  text_encoders: models/text_encoders/
  upscale_models: models/upscale_models/
  vae: models/vae/
  unet: models/unet/
YAML

# 4. Restart ComfyUI so it re-imports custom_nodes and re-reads the YAML.
#    The simplest correct path is a pod Restart from the dashboard.
#    The in-session alternative, using the exact /start.sh pattern:
pkill -TERM -f 'main.py --listen 0.0.0.0 --port 8188' ; sleep 3
cd /workspace/runpod-slim/ComfyUI
setsid nohup "$VENV_PY" main.py --listen 0.0.0.0 --port 8188 \
  --enable-cors-header </dev/null >/workspace/comfyui.log 2>&1 &
disown

# 5. Install pipeline deps into SYSTEM python3 (NOT the venv).
#    Required for Node 11 orchestrator to subprocess-invoke Nodes 2-6
#    + 8-10 + 11 -- those nodes use `python3 run_nodeN.py` (system
#    python), NOT the venv python (which is for ComfyUI itself). The
#    venv has Comfy's deps but the system Python doesn't until this
#    runs. Skip this step if you only intend to run Node 7 from outside
#    (e.g., the Node 7 smoke fixture path that doesn't go through
#    Node 11). REQUIRED for Node 11 end-to-end runs.
cd /workspace/animatic-refinement
python3 -m pip install -r requirements.txt
# Verify imageio_ffmpeg + pydantic + PIL + numpy + scipy now importable:
python3 -c "import imageio_ffmpeg, pydantic, PIL, numpy, scipy; print('OK')"
```

## Verify

```bash
# Wait ~10s, then:
curl -s http://127.0.0.1:8188/system_stats | head
curl -s http://127.0.0.1:8188/object_info/CheckpointLoaderSimple \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
    print(d['CheckpointLoaderSimple']['input']['required']['ckpt_name'][0])"
# Expect: list of safetensors files, including
# 'anyloraCheckpoint_bakedvaeBlessedFp16.safetensors'.

bash tools/pod_diagnose_preprocessors.sh
# Expect: ~980+ classes, LineArtPreprocessor / ScribblePreprocessor /
# DWPreprocessor all present.

cd /workspace/animatic-refinement
python3 run_node7.py \
  --node6-result /workspace/smoke_workdir/work/node6_result.json \
  --queue        /workspace/smoke_workdir/input/queue.json
# Expect: 'N generated / 0 skipped / 0 error(s)' and PNGs in
# /workspace/smoke_workdir/work/<shotId>/refined/.
```

## Known-good state (2026-04-25)

**First live run (lineart-fallback route, both characters):**
- 2 PNGs / 0 errors / **36s wall time** on the 2-character synthetic
  smoke fixture.

**DWPose-route verification (same fixture, Bhim flipped to dwpose):**
- 2 PNGs / 0 errors / **41s wall time** (extra ~9s = first-time DWPose
  model load + ONNX inference).

**Node 11 end-to-end orchestrator verification (synthetic Node 1
input dir, lineart-fallback, single 5-frame 128×96 shot):**
- 1 shot succeeded / 0 failed / **33.8s wall time** end-to-end via
  one `python3 run_node11.py --input-dir <i> --work-dir <w>` call.
- Per-node breakdown: Nodes 2 + 3 + 4 + 5 + 6 + 8 + 9 + 10 each
  finished in <1s (pure-Python work); Node 7 dominated at 30.4s
  (real SD generation on 4090); Node 11 orchestration overhead ~2s.
- Pre-Node-7 GPU check fired: `nvidia-smi` correctly logged GPU info
  to `node11_progress.jsonl` before Node 7 ran.
- Final deliverable: 1.8 KB H.264 MP4 in
  `/workspace/<work>/output/shot_001_refined.mp4`.
- Same Node 7 invocation routed Bhim through dwpose + Jaggu through
  lineart-fallback. Per-character routing table works.
- Bhim PNG bytes differ from the lineart-fallback baseline (117 796 B
  vs 112 130 B; sha256 `d77d9b18…` vs `038f69e6…`) — proves DWPose
  actually contributed pose info, didn't silently no-op.
- Jaggu PNG bytes are bit-identical across runs (sha256 `5aa3c619…`)
  — proves the deterministic-seed contract still holds for unchanged
  routes.

**Common environment:**
- ComfyUI Python: 3.12.3 (venv at `/workspace/runpod-slim/ComfyUI/.venv-cu128`).
- PyTorch: 2.11.0+cu128.
- `comfyui_controlnet_aux` registered 64 preprocessor classes (984 total).
- Checkpoints visible: `anyloraCheckpoint_bakedvaeBlessedFp16`,
  `anything-v5-PrtRE`, `flat2DAnimerge_v45Sharp`, `sd_xl_base_1.0`.
- ControlNets: `control_v11p_sd15_{lineart,scribble,openpose}.pth`.
- LoRAs: `bnw_lineart_v1` (symlink to `thick_line_cartoon_lora`).
- IP-Adapter: `ip-adapter-plus_sd15.safetensors`.

## Open items (not blockers for Node 7 live-verified status)

- `runpod_setup.sh` still targets `/workspace/ComfyUI` by default.
  Consider teaching it a `COMFY_EXTRA_MODEL_BASE` env knob that writes
  `extra_model_paths.yaml` automatically. Keep the default behavior so
  plain-layout pods still work.
- Output quality: smoke fixture PNGs are stylized renders of the
  synthetic stick-figure references (red Bhim, green Jaggu). Faint
  artist-signature bleed-through in the output indicates the empty
  negative-prompt could be tightened (`text, signature, watermark`), but
  that's a future tuning pass, not a bringup blocker.
- The smoke fixture's "Bhim" rough is a crude biped silhouette (head +
  torso + 4 limbs). DWPose extracts a usable skeleton from it, but
  output quality on real Chota Bhim shots will only be confirmable
  against a real client MP4 — that's a separate end-to-end test, not a
  bringup blocker.

## Phase 2-revision-fixup-3 (2026-05-03) — known pod gotchas to look out for

Captured during the third live-pod test session (TMKOC EP35 SH004
fixture, EU-RO-1 community-cloud A100 80GB SXM). Three issues bit us
in succession; all three are now auto-handled by `runpod_setup.sh`,
but writing them down so the next live-pod debug doesn't have to
re-discover.

### 1. ComfyUI's `/workspace/ComfyUI/.git` working tree may have files MISSING

The persistent network volume's `/workspace/ComfyUI/` git working
tree had ~50 files marked as `deleted` in `git status` even though
the tree was at HEAD. Symptom: `python3 main.py` fails immediately
with `ModuleNotFoundError: No module named 'utils.install_util'` —
the `utils/` dir contains `__pycache__/` but no `*.py` source files.

**Fix:** before launching ComfyUI, run `git checkout -- .` inside
`/workspace/ComfyUI/` to restore the missing files. (Don't `git
reset --hard origin/master` — that loses the `models/` dir's
contents tracked-by-LFS and you'll re-download 38 GB of weights.)

### 2. `x-flux-comfyui` blocks reject ComfyUI's new `attn_mask` kwarg

Recent ComfyUI (>= 0.19) calls Flux's `DoubleStreamBlock.forward(...)`
and `SingleStreamBlock.forward(...)` with `attn_mask=...` and
`transformer_options=...` keyword arguments. Upstream `x-flux-comfyui`
declares those signatures positional-only — `(img, txt, vec, pe)` and
`(x, vec, pe)`. Result: ComfyUI's prompt validator passes (the
class_type "LoadFluxIPAdapter" is registered), but every KSampler
step crashes with:

```
TypeError: DoubleStreamBlock.forward() got an unexpected keyword argument 'attn_mask'
```

**Auto-fixed in `runpod_setup.sh`** as of Phase 2-revision-fixup-3
(2026-05-03): the script `sed -i`s a `**kwargs` into both signatures
right after cloning x-flux-comfyui. Idempotent — it greps for the
unpatched signature first and skips if already patched.

### 3. PyTorch 2.4.x + driver CUDA 13.0 = Flux silently falls back to CPU

The `runpod-slim` image we used had PyTorch 2.4.1+cu124 system-installed
but the GPU driver reports CUDA 13.0. Flux loads fine into GPU memory
(`/usr/bin/python3 -c 'import torch; torch.cuda.is_available()'` = True;
`nvidia-smi` shows ~36 GB used by ComfyUI), but the actual sampling
computation runs on CPU. Symptom: `nvidia-smi --query-gpu=utilization.gpu`
reads `0 %` for hundreds of seconds while `top` shows the ComfyUI
process at 2000+% CPU (20+ cores at 100%). KSampler step time becomes
~18 sec instead of <2 sec; full 40-step Flux generation takes ~12 min
instead of <1 min.

ComfyUI's boot log warns about this but it's easy to miss:

```
WARNING: You need pytorch with cu130 or higher to use optimized CUDA operations.
Found comfy_kitchen backend cuda: {'available': True, 'disabled': True, ...}
```

**Auto-warned in `runpod_setup.sh`** as of Phase 2-revision-fixup-3:
the script logs `WARNING: PyTorch X.Y is below the recommended 2.5+
for Flux GPU acceleration` if it detects torch 2.4.x or older.

**Fix:**

```bash
pip install --upgrade 'torch>=2.5,<2.7' 'torchvision' 'torchaudio' \
    --index-url https://download.pytorch.org/whl/cu130
```

Or use a newer pod template (e.g. `runpod/pytorch:2.5.1-py3.11-cuda12.4.1-devel-ubuntu22.04`
or its cu130 successor) that ships PyTorch 2.5+ with cu130 kernels
out of the box.

### Bonus: Node 11 default per-prompt timeout was too short

`pipeline.cli_node7`'s default `--per-prompt-timeout` is 600 seconds
(10 min). On the CPU-bound Flux runs (issue #3 above), each generation
took ~12-19 min, so orchestrate.py timed out and marked every detection
as `error` even though ComfyUI was still happily generating in the
background. The image WAS produced and saved to ComfyUI's `output/`
dir, just not pulled back via the `/view` endpoint.

**Auto-fixed in Phase 2-revision-fixup-3:** Node 11's CLI gained a
`--per-prompt-timeout SECONDS` flag that passes through to Node 7.
Bump it (e.g. `--per-prompt-timeout 1800` for 30 min) when running
on a CPU-bound or otherwise slow Flux setup.
