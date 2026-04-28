# Animatic Refinement Workflow — Part 1

AI pipeline that converts rough MP4 animatic shots (Chota Bhim Indian cartoon style) into **BnW line-art MP4s** with refined, reference-accurate characters in correct positions and poses.

> Part 2 (ToonCrafter frame interpolation) is tracked separately.

## What this repo contains

| Path | Purpose |
|---|---|
| `frontend/` | Node 1 — Character Library page + Shot Metadata form (browser-only; writes `characters.json` + `metadata.json`) |
| `pipeline/` | Nodes 2, 3, 4, 5, 6 (+ Node 11 later) — pure-Python, GPU-agnostic core logic (validator, frame extractor, key-pose partitioner, character detector, reference matcher). Node 7 breaks this template on purpose: the authoritative artifact is `workflow.json`. |
| `run_node2.py` / `run_node3.py` / `run_node4.py` / `run_node5.py` / `run_node6.py` / `run_node7.py` | Thin repo-root wrappers so each node's CLI runs on both Windows embedded Python and standard Python (Node 7's live path is RunPod-only; laptop can only `--dry-run`) |
| `custom_nodes/` | ComfyUI custom nodes for Nodes 3–10 (one folder per node; thin wrappers around `pipeline/` where applicable) |
| `workflows/` | ComfyUI workflow graph JSONs wiring the custom nodes |
| `docs/` | `PLAN.md` + `Node_Plan.xlsx` — canonical node-by-node design |
| `tests/` | Per-node tests (pytest) |
| `runpod_setup.sh` | One-shot bootstrap for a fresh RunPod pod |

## Status

| Node | Name | Status |
|---|---|---|
| 1 | Project Input & Setup Interface | **DONE** — initial build, awaiting first real-shot test |
| 2 | Metadata Ingestion & Validation | **DONE** — 26 tests pass; CLI + wrapper verified on embedded Python |
| 3 | Shot Pre-processing (MP4 → PNG) | **DONE** — 20 tests pass; CLI + wrapper + ComfyUI node verified; end-to-end smoke against real MP4s |
| 4 | Key Pose Extraction | **DONE** — 26 tests pass (72 repo-wide); CLI + wrapper + ComfyUI node verified; translation-aware partition handles slide shots |
| 5 | Character Detection & Position | **DONE** — initial build 2026-04-23; **Phase 2f shipped 2026-04-28** (luminance pre-threshold + morph closing + `dark_lines/` artifact for Node 7). 70 tests pass (479 repo-wide); CLI + wrapper + ComfyUI node verified; end-to-end Node 2→3→4→5 smoke test passes on real MP4. The Phase 2f update replaces the original Otsu binarization with a fixed luminance threshold (default 80, `--dark-threshold N` tunable) that separates dark character outlines from lighter BG furniture lines per the user's storyboard convention, followed by 3×3 morphological closing to seal 1-2 pixel gaps where character outlines crossed BG lines. Side effect: writes `<shot>/dark_lines/<filename>.png` (BnW: black ink on white BG) per keypose. Node 7's bbox crop now reads from `dark_lines/` when present (falls back to raw keypose for pre-Phase-2f work dirs), so Flux gets character-only pixels with BG furniture erased. |
| 6 | Character Reference Sheet Matching | **DONE** — 34 tests pass (156 repo-wide); CLI + wrapper + ComfyUI node verified; end-to-end Node 2→3→4→5→6 smoke test passes; classical alpha-island slicing + multi-signal angle scoring + DoG line-art |
| 7 | AI-Powered Pose Refinement | **Phase 1 DONE (live-verified 2026-04-25). Phase 2a-2d-prep SHIPPED 2026-04-26→27. Phase 2d-fixup SHIPPED 2026-04-27 (post-live-pod-debug: corrected workflow_flux_v2.json class_type strings from GUI display names to wire-protocol class names; corrected models.json IP-Adapter destination path). Phase 2-revision SHIPPED 2026-04-28 (post-design-review correction of Phase 2c full-frame img2img regression: pre-crop keypose to per-character bbox + 20% margin before submission; flip prompts colored TMKOC → BnW line art on white BG; bypass generic Flat Cartoon LoRA via per-LoRA strength=0.0 until Phase 2d-run's TMKOC line-art LoRA ships). Phase 2f SHIPPED 2026-04-28 (Node 5 luminance pre-threshold + morph closing + `<shot>/dark_lines/<filename>.png` side-effect; Node 7's bbox crop reads from `dark_lines/` when present, so Flux's input is character lines on clean white BG with no BG furniture to fight). Phase 2d-run pending — will use user's storyboard scene cuts (clean BnW lines on white) directly as training data. Phase 2e + 2g pending.** Phase 1: 47 tests pass; live-verified on RunPod 4090. Bringup notes captured in `tools/POD_NOTES_runpod_slim.md`. **Phase 2a (2026-04-26):** Flux + Style LoRA + ControlNet Union Pro integration in `workflow_flux_v2.json` (16 locked Phase 2 node IDs); `models.json` schema bumped (additive deprecation fields; Phase 1 weights flipped to `deprecated:true` with 2026-10-26 removal); Phase 2 weight pins; `runpod_setup.sh` honours `DOWNLOAD_DEPRECATED`; `--workflow {v1,v2}` + `--precision {fp16,fp8}` CLI flags; `RefinedGeneration` + `CharacterSpec` gained Phase 2 fields (additive). **Phase 2b (2026-04-27):** Wired XLabs Flux IP-Adapter v2 into `workflow_flux_v2.json` via 3 additive locked node IDs (22 `Load Flux IPAdatpter` [sic — upstream typo preserved], 23 LoadImage for reference COLOR crop, 24 `Apply Flux IPAdapter`); KSampler's model input rewires from node 20 → node 24; `ip_scale=0.8` per locked decision #6. `models.json` gained `flux-ip-adapter-v2.safetensors` (~1 GB) + `clip-vit-large-patch14.safetensors` (~600 MB CLIP-L vision encoder) + `x-flux-comfyui` custom node clone. **Phase 2c (2026-04-27):** 441 tests pass repo-wide (96 in test_node7.py — 47 Phase 1 + 49 Phase 2 — plus 30 in test_node2.py; zero regressions). TWO architecturally significant flips: (1) `workflow_flux_v2.json` node 80 swapped from `EmptySD3LatentImage` (txt2img) to `VAEEncode` (img2img — wired to take pixels from node 50 rough crop + Flux VAE from node 12); KSampler `denoise` dropped from 1.0 to 0.55 per locked decision #5. (2) `DEFAULT_WORKFLOW` flipped from `"v1"` to `"v2"` in `orchestrate.py` — v2 is now the production default; Phase 1 stays callable via `--workflow=v1` for the 6-month deprecation window. `V2_DENOISE = 0.55` constant added; parameterizer re-asserts it. CLI success line for v2 now reports `precision=<value>` alongside `workflow=v2`. 5 new tests cover img2img wiring + denoise lock + v2-as-default + v1 still-callable-via-flag. **Phase 2d-prep (2026-04-27):** 449 tests pass repo-wide (104 in test_node7.py — 47 Phase 1 + 57 Phase 2; zero regressions). Shipped infrastructure for the TMKOC v1 style LoRA: new `--style-lora {flat_cartoon_v12,tmkoc_v1}` flag (default `flat_cartoon_v12`) on Node 7 + Node 11 CLIs + ComfyUI wrapper dropdown; `orchestrate.py` gained `STYLE_LORA_FILENAMES` + `STYLE_LORA_CHOICES` + `DEFAULT_STYLE_LORA`; `models.json` gained `tmkoc-style-v1` placeholder entry (`url: "TODO"` until Phase 2d-run lands); new `tools/phase2d/PHASE2D_TRAINING_PLAYBOOK.md` runbook + `tools/phase2d/ai_toolkit_config_template.yaml` (rank=16, LR=1e-4, 2000 steps, locked params). 8 new tests cover the flag plumbing + parameterizer wiring + config validation. **Phase 2-revision (2026-04-28, post-design-review):** Corrected the Phase 2c full-frame img2img regression that broke Phase 1 locked decision #5 (per-character generation) and produced colored TMKOC scenes instead of BnW per-character keyposes. Three architectural flips: (1) `_run_one_task` now pre-crops the keypose to (Node-5 bbox + 20% margin) clamped to image bounds + resized to multiples of 16 (Flux requirement) with longest edge ≤ 768 — the bbox crop becomes node 50's input so pose preprocessor + VAEEncode + KSampler all operate on character-only pixels. (2) `V2_POSITIVE_PROMPT_TEMPLATE` + `V2_NEGATIVE_PROMPT` swapped from "flat cartoon style ... bright daytime colors" + reject "monochrome" → "clean black ink line art, white background, no fill, no color" + reject "color, fill, shading, scene, furniture" (the negative no longer rejects "monochrome" — that was Phase 2c's bug). (3) New `STYLE_LORA_STRENGTHS` per-LoRA strength table (`flat_cartoon_v12 → 0.0`, `tmkoc_v1 → 0.75`) so the generic Flat Cartoon LoRA is bypassed without removing the LoraLoader chain; locked decision #2 ("style LoRA at 0.75") survives intact for the LoRA we actually want to use. Phase 2d training-data approach also flipped from synthetic Path A bootstrap → user's storyboard scene cuts directly (clean BnW digital lines on white BG) — `tools/phase2d/PHASE2D_TRAINING_PLAYBOOK.md` revised end-to-end. **Phase 2f (2026-04-28):** Replaced Node 5's Otsu binarization with a fixed luminance threshold (default 80, `--dark-threshold N` to tune) + 3×3 morphological closing. Side effect: writes `<shot>/dark_lines/<filename>.png` per keypose (BnW: black ink on white BG). Node 7's bbox crop reads from `dark_lines/` (falls back to raw keypose for pre-Phase-2f work dirs). User's "dark vs light line" insight is a real semantic signal (character outlines ~0-50 luminance, BG furniture ~80-180) that beats the original heuristic-based "bbox > 70% of frame" approach. **70 Node 5 tests pass + 114 Node 7 tests pass; 479 repo-wide, zero regressions** (24 new Phase 2f tests: 20 in test_node5.py, 4 in test_node7.py). Phase 2d-run is a live A100 pod session per the revised playbook (~$3-5 GPU + ~4-8 hours human time across 1-2 iterations — cheaper than 2d-prep's Path A estimate). Phase 2e-2g pending: 2e (per-character LoRAs) → 2f (Node 5 background-line fix) → 2g (Node 6 simplification). |
| 8 | Scene Assembly | **DONE** — 51 tests pass (258 repo-wide); CLI + `run_node8.py` wrapper + ComfyUI custom node verified on embedded Python; pure-Python compositing (PIL + numpy), no GPU. Feet-pinned scaling places each refined character so its feet land at `bbox.bottomY`; z-order by bbox.bottomY ascending; BnW threshold; substitute-rough fallback (warn-and-reconcile) when Node 7 marked a generation as errored or empty. |
| 9 | Timing Reconstruction | **DONE** — 42 tests pass (300 repo-wide); CLI + `run_node9.py` wrapper + ComfyUI custom node verified on embedded Python; pure-Python translate-and-copy (PIL + numpy), no GPU. Anchor frames are bit-identical copies of Node 8's composite; held frames are pasted onto a fresh white canvas at `(dx, dy)` offset from `keypose_map.json`. Off-canvas translates are NOT errors (mathematically valid for end-of-slide shots). Fail-loud on missing composed PNG / totalFrames mismatch / Node 4 invariant violations. |
| 10 | Output Generation (PNG → MP4) | **DONE** — 42 tests pass (342 repo-wide); CLI + `run_node10.py` wrapper + ComfyUI custom node verified on embedded Python; pure-Python (subprocess + imageio_ffmpeg + json), no GPU. ffmpeg via imageio-ffmpeg static binary; H.264 + yuv420p + CRF 18 + 25 FPS (codec/preset/pixel-format locked, CRF tunable). Output to `<work-dir>/output/<shotId>_refined.mp4`. Post-encode verification via `imageio_ffmpeg.count_frames_and_secs` (frame count + duration). Odd canvas dims fail-loud. Does NOT delete upstream artifacts (intermediates kept for Part 2 reuse). |
| 11 | Batch Management | **DONE — live-verified end-to-end on RunPod** — 46 tests pass (388 repo-wide); CLI + `run_node11.py` wrapper + ComfyUI custom node verified on embedded Python; pure-Python orchestrator (subprocess + json + datetime), no GPU. Subprocess-invokes `run_nodeN.py` for N in 2..10 with per-node retry policy + JSONL progress log + final aggregate report. Pre-Node-7 best-effort `nvidia-smi` (warn but proceed). Partial-success: some shots ok / some failed = exit 0 (`failedShots > 0` in `node11_result.json`); 100% failure = exit 1. `--dry-run` passes through to Node 7. **First live RunPod run (2026-04-25, 4090):** 1 shot succeeded / 0 failed in 33.8s end-to-end (Node 7 SD generation 30.4s; everything else ~3s). Bringup gap captured: system python3 needs `pipeline/requirements.txt` installed for Nodes 2-6 + 8-10 + 11 to run as subprocesses. |

## Running Node 1 (browser)

Open `frontend/characters.html` — register each character, upload its 8-angle model sheet, download `characters.json` and the named sheet PNGs. Then open `frontend/index.html`, fill in the shot form, download `metadata.json`. See `frontend/README.md` for the full operator workflow.

## Running Node 2 (validator)

Node 2 reads `metadata.json` + `characters.json` from a flat input folder, cross-checks every reference against files on disk, and writes `queue.json` — the contract Node 3 will consume.

**Input folder layout** (flat, all files side-by-side):

```
<input-dir>/
  metadata.json          from Node 1 shot form
  characters.json        from Node 1 character library
  <name>.png             sheet PNGs named per characters.json
  shot_001.mp4, ...      rough animatic MP4s named per metadata.json
```

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node2.py --input-dir /path/to/input

# Windows embedded Python (local dev with ComfyUI portable):
"C:\...\ComfyUI_windows_portable\python_embeded\python.exe" run_node2.py --input-dir /path/to/input
```

**Exit codes:** `0` success, `1` validation error (readable message to stderr), `2` unexpected error.

On success, writes `<input-dir>/queue.json` — ordered, batched, absolute-path-resolved, ready for Node 3.

## Running Node 3 (MP4 → PNG frames)

Node 3 reads `queue.json` and decodes each shot's rough MP4 into a per-shot folder of PNG frames (`frame_NNNN.png`), plus a `_manifest.json` summarizing each shot and a top-level `node3_result.json` that Node 4 will consume.

ffmpeg is provided by the `imageio-ffmpeg` pip wheel — no system ffmpeg needed on Windows or RunPod.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node3.py --queue /path/to/queue.json --work-dir /path/to/work

# Windows embedded Python (local dev with ComfyUI portable):
"C:\...\ComfyUI_windows_portable\python_embeded\python.exe" run_node3.py --queue <q> --work-dir <w>
```

**Output layout:**

```
<work-dir>/
  node3_result.json         aggregate: every shot + all warnings
  <shotId>/
    frame_0001.png
    frame_0002.png
    ...
    _manifest.json          per-shot summary
```

**Exit codes:** `0` success (even with frame-count warnings — those are data, not failures), `1` `Node3Error` (queue/ffmpeg/disk problem), `2` unexpected.

**Frame-count drift** (actual decoded frames ≠ `durationFrames` in metadata) is a non-fatal warning in `node3_result.json` — Node 9 uses the actual count when reconstructing timing.

## Running Node 4 (key poses)

Node 4 reads `node3_result.json` and partitions each shot's PNG frames into **key poses** (unique poses) and **held frames** (duplicates of an earlier key pose, possibly translated). It is **translation-aware**: a character sliding across the frame without changing pose collapses to a single key pose plus per-held-frame `(dy, dx)` offsets — Node 9 will replay that slide by translate-and-copy.

Under the hood: each frame is phase-correlated (FFT cross-power spectrum) against the current key-pose anchor on a downscaled grayscale copy, then aligned MAE is computed over the overlap region. Aligned MAE ≤ `--threshold` → held; otherwise → new key pose.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node4.py --node3-result /path/to/work/node3_result.json

# Windows embedded Python (local dev with ComfyUI portable):
"C:\...\ComfyUI_windows_portable\python_embeded\python.exe" run_node4.py --node3-result <path>

# Tune threshold or downscale if defaults misclassify:
python run_node4.py --node3-result <path> --threshold 8.0 --max-edge 128
```

**Flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--node3-result <path>` | *required* | Aggregate manifest written by Node 3 |
| `--threshold <float>` | `8.0` | Aligned-MAE threshold on 0–255 grayscale; frames above this become new key poses |
| `--max-edge <int>` | `128` | Downscale so `max(H, W) = N` before FFT + MAE; offsets scaled back to full-res on write |
| `--quiet` | off | Suppress the success line |

**Output layout** (added next to Node 3's output):

```
<work-dir>/
  node3_result.json         (from Node 3)
  node4_result.json         aggregate: one summary per shot
  <shotId>/
    frame_0001.png          (from Node 3)
    ...
    _manifest.json          (from Node 3)
    keypose_map.json        per-shot partition Node 9 reads
    keyposes/
      frame_0001.png        copies of chosen key poses,
      frame_0015.png        source filenames preserved
      ...
```

**Exit codes:** `0` success, `1` `Node4Error` (malformed `node3_result.json`, missing frames, resolution mismatch against anchor), `2` unexpected.

## Running Node 5 (character detection & position)

Node 5 reads **both** `node4_result.json` (for the key-pose frames) **and** `queue.json` (for each shot's expected character identities + positions), runs classical connected-component detection on every key pose, bins each silhouette into a position zone (L/CL/C/CR/R), and assigns an identity by zipping detections left→right with metadata characters sorted by position rank (Strategy A). Count mismatches between detection and metadata are **reconciled and warned** — they do not fail the CLI.

No ML, no GPU. Otsu binarization + `scipy.ndimage.label` (8-connectivity) + IoU-based bbox merge + progressive `binary_erosion` for the too-few-blobs case. Same tool everywhere (CLI, tests, CI, ComfyUI).

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node5.py \
    --node4-result /path/to/work/node4_result.json \
    --queue /path/to/input/queue.json

# Windows embedded Python (local dev with ComfyUI portable):
"C:\...\ComfyUI_windows_portable\python_embeded\python.exe" run_node5.py \
    --node4-result <n4> --queue <q>

# Tune cleanup thresholds if defaults misclassify:
python run_node5.py --node4-result <n4> --queue <q> \
    --min-area-ratio 0.001 --merge-iou 0.5
```

**Flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--node4-result <path>` | *required* | Aggregate manifest written by Node 4 |
| `--queue <path>` | *required* | queue.json from Node 2 — needed for each shot's expected characters + positions |
| `--min-area-ratio <float>` | `0.001` | Drop connected-component blobs whose area is below this fraction of frame area |
| `--merge-iou <float>` | `0.5` | Merge two bounding boxes whose IoU meets or exceeds this threshold |
| `--quiet` | off | Suppress the success line |

**Output layout** (added next to Nodes 3 + 4):

```
<work-dir>/
  node3_result.json         (from Node 3)
  node4_result.json         (from Node 4)
  node5_result.json         aggregate: per-shot detection summary
  <shotId>/
    frame_0001.png          (from Node 3)
    ...
    keypose_map.json        (from Node 4)
    keyposes/               (from Node 4)
      frame_0001.png
      ...
    character_map.json      per-shot detection map Node 6 consumes
```

`character_map.json` lists per-key-pose `detections[]` (identity, expectedPosition, boundingBox `[x, y, w, h]`, centerX normalized, positionCode, area) plus a `warnings[]` log of every reconcile action (`count-mismatch-over`, `reconcile-eroded`, `reconcile-merged`, `reconcile-dropped`, `reconcile-failed`).

**Exit codes:** `0` success (reconcile warnings are still exit 0), `1` `Node5Error` (`Node4ResultInputError`, `QueueLookupError`, `CharacterDetectionError`), `2` unexpected.

## Running Node 6 (reference sheet matching)

Node 6 reads **three** JSON manifests (`node5_result.json` + `queue.json` + `characters.json`). For every Node-5 detection on every key pose, it slices the character's 8-angle reference sheet via alpha-island bbox labelling, recomputes a clean silhouette from the detection bbox (Otsu + largest CC), scores each of the 8 reference angles against the detection using a classical multi-signal function (silhouette IoU + horizontal-symmetry + bbox aspect + upper-region interior-edge density), picks the winning angle, and emits a color reference crop plus a Difference-of-Gaussians line-art copy.

No ML, no GPU. Same tool everywhere (CLI, tests, CI, ComfyUI). Chota Bhim art is crisp flat-fill cartoon with distinctive silhouettes per angle; 8-way classical classification is reliable, fast (~ms per key pose), and adds zero cold-start to RunPod pods.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node6.py \
    --node5-result /path/to/work/node5_result.json \
    --queue /path/to/input/queue.json \
    --characters /path/to/input/characters.json

# Windows embedded Python (local dev with ComfyUI portable):
"C:\...\ComfyUI_windows_portable\python_embeded\python.exe" run_node6.py \
    --node5-result <n5> --queue <q> --characters <c>

# A/B a different line-art method:
python run_node6.py --node5-result <n5> --queue <q> --characters <c> \
    --lineart-method canny
```

**Flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--node5-result <path>` | *required* | Aggregate manifest written by Node 5 |
| `--queue <path>` | *required* | queue.json from Node 2 — supplies each character's 8-angle sheet PNG path |
| `--characters <path>` | *required* | characters.json from Node 1 — Node 6 checks `conventions.angleOrderConfirmed` |
| `--lineart-method {dog,canny,threshold}` | `dog` | Classical method to convert the chosen color crop into a black-line line-art PNG |
| `--quiet` | off | Suppress the success line |

**Output layout** (added next to Nodes 3 + 4 + 5):

```
<work-dir>/
  node3_result.json         (from Node 3)
  node4_result.json         (from Node 4)
  node5_result.json         (from Node 5)
  node6_result.json         aggregate: per-shot reference summary + angle histogram
  <shotId>/
    frame_0001.png          (from Node 3)
    ...
    keypose_map.json        (from Node 4)
    keyposes/               (from Node 4)
    character_map.json      (from Node 5)
    reference_map.json      per-detection reference + scoring (Node 6)
    reference_crops/
      <identity>_<angle>.png         color reference crop
      <identity>_<angle>_lineart.png line-art version of same crop
      ...
```

`reference_map.json` lists per-key-pose `matches[]` (identity, selectedAngle, scoreBreakdown, allScores, referenceColorCropPath, referenceLineArtCropPath) plus a `skipped[]` array for detections Node 6 couldn't score (e.g. an unpaired Node-5 detection with an empty identity). Crops are cached per `(identity, angle)` within a shot — multiple key poses picking the same angle share one color + one line-art file.

**Exit codes:** `0` success, `1` `Node6Error` (`Node5ResultInputError`, `CharactersInputError`, `AngleOrderUnconfirmedError`, `ReferenceSheetFormatError`, `ReferenceSheetSliceError`, `AngleMatchingError`) or the shared `QueueLookupError`, `2` unexpected.

## Running Node 7 (AI pose refinement — RunPod-only live path)

Node 7 replaces each rough-animatic detection with a **BnW line-art drawing of the reference character in the rough's pose**, by decoupling pose (from the rough key-pose via a pose ControlNet) from identity (from Node 6's color reference crop via IP-Adapter-Plus). Two workflow templates ship — `dwpose` for humans, `lineart-fallback` for non-humans (quadrupeds, Jaggu) — and are picked per character from `queue.json.batches[].characters[].poseExtractor`.

**Node 7 runs on the RunPod pod, never the laptop** (locked decision #13). The user's laptop has neither the VRAM nor the weight downloads for SD 1.5 + ControlNet + IP-Adapter + DWPose. Laptop runs Nodes 2–6 on CPU, syncs the work dir up to the pod, and executes Node 7 there. A `--dry-run` flag on the CLI exercises the manifest layer end-to-end without contacting ComfyUI (useful for local smoke tests).

**Invoke (on the pod):**

```bash
# Full live path — pod with ComfyUI listening on 8188:
python run_node7.py \
    --node6-result /path/to/work/node6_result.json \
    --queue /path/to/input/queue.json

# Laptop smoke test (skips ComfyUI, records status="skipped" per generation):
"C:\...\ComfyUI_windows_portable\python_embeded\python.exe" run_node7.py \
    --node6-result <n6> --queue <q> --dry-run
```

**Flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--node6-result <path>` | *required* | Aggregate manifest written by Node 6 |
| `--queue <path>` | *required* | queue.json from Node 2 — supplies per-character `poseExtractor` routing |
| `--comfyui-url <url>` | `http://127.0.0.1:8188` | ComfyUI HTTP API endpoint (ignored when `--dry-run`) |
| `--per-prompt-timeout <sec>` | `600` | Seconds to wait for a single generation before marking it errored |
| `--dry-run` | off | Skip ComfyUI submission; record `status="skipped"` generations only |
| `--quiet` | off | Suppress the success line |

**Output layout** (added next to Nodes 3 + 4 + 5 + 6):

```
<work-dir>/
  node6_result.json         (from Node 6)
  node7_result.json         aggregate: per-shot generated/skipped/error counts
  <shotId>/
    reference_map.json      (from Node 6)
    reference_crops/        (from Node 6)
    refined_map.json        per-(keyPoseIndex, identity) generation record
    refined/
      001_Bhim.png          transparent 512×512 BnW line-art (NNN = key-pose index)
      002_Jaggu.png
      ...
```

`refined_map.json` records every `RefinedGeneration` — pose-extractor route, seed, CN strengths, seed derivation inputs, output PNG path, and a `status` of `ok` / `skipped` / `error` (dry-run marks every generation `skipped`; a live run marks the generation `error` and logs the failure reason if ComfyUI returns an error, so the CLI still exits 0 for partial success and the operator can retry individual generations).

**Exit codes:** `0` success (per-generation errors are recorded in `refined_map.json` — CLI still exits 0), `1` `Node7Error` (`Node6ResultInputError`, `RefinementGenerationError`) or the shared `QueueLookupError` (missing shot, unknown identity, bad workflow template, whole-run ComfyUI connection failure), `2` unexpected.

**Model weights + custom-node deps are resolved by `runpod_setup.sh`** — it reads `custom_nodes/node_07_pose_refiner/models.json` (schemaVersion 1) and `git clone`s every `customNodes[]` entry into ComfyUI's `custom_nodes/`, then `curl`-downloads every `models[]` entry to the declared destination with sha256 verification. Operator fills in the TODO URLs + sha256 pins after the first known-good download.

**Phase 2a (Flux migration) — SHIPPED 2026-04-26. Phase 2b (XLabs Flux IP-Adapter) — SHIPPED 2026-04-27. Phase 2c (img2img mode + v2 default) — SHIPPED 2026-04-27. Phase 2d-prep (`--style-lora` flag + TMKOC v1 placeholder) — SHIPPED 2026-04-27.** Node 7's CLI accepts `--workflow {v1,v2}` (default `v2`), `--precision {fp16,fp8}` (default `fp16`), and `--style-lora {flat_cartoon_v12,tmkoc_v1}` (default `flat_cartoon_v12`). v2 routes both human and non-human characters through a single `workflow_flux_v2.json` (Flux Dev + Flat Cartoon Style v1.2 LoRA + ControlNet Union Pro + **XLabs Flux IP-Adapter v2 for character identity** + **img2img mode @ denoise=0.55**), with the SetUnionControlNetType node handling per-character `openpose` vs `lineart` switching. The custom-trained TMKOC v1 style LoRA (`--style-lora=tmkoc_v1`) is the next phase to actually ship — Phase 2d-prep wired the flag + placeholder; Phase 2d-run is the live A100 training session per `tools/phase2d/PHASE2D_TRAINING_PLAYBOOK.md`. Phase 2c's img2img mode preserves rough composition (positions, scale, silhouette) while regenerating identity + line quality — Flux Kontext Dev's superior denoising resolves Phase 1's "rough pixels would bleed into output" concern at denoise=0.55. Phase 1 stays callable via `--workflow=v1` for the 6-month deprecation window (until 2026-10-26); both flags pass through Node 11 unchanged. Phase 1 weights are marked `deprecated:true` in `models.json` with a scheduled-removal date of 2026-10-26; fresh pods skip them by default (saves ~6.5 GB) but `DOWNLOAD_DEPRECATED=true bash runpod_setup.sh` opts back in. **Try v2 in dry-run on the laptop (no ComfyUI needed):**

```bash
# Default Phase 2 v2 path (since Phase 2c flipped the default):
python run_node7.py \
    --node6-result <work>/node6_result.json \
    --queue        <input>/queue.json \
    --dry-run

# Force the Phase 1 path (still callable for the deprecation window):
python run_node7.py --node6-result <n6> --queue <q> --workflow=v1 --dry-run

# Same flag passthrough on the orchestrator:
python run_node11.py --input-dir <i> --work-dir <w> --dry-run

# Force fp8 fallback on a 4090-class GPU:
python run_node11.py --input-dir <i> --work-dir <w> --precision=fp8
```

Phases 2a-2c + 2d-prep + 2d-fixup shipped 2026-04-26→27. Phase 2-revision shipped 2026-04-28 (corrected Phase 2c full-frame img2img regression: per-character bbox crop + BnW line-art prompts + Flat Cartoon LoRA bypass via per-LoRA strength=0.0). Phase 2f shipped 2026-04-28 (Node 5 luminance pre-threshold + morph closing + `dark_lines/` side-effect Node 7 reads from). Phases 2d-run + 2e + 2g still pending: 2d-run (custom-trained TMKOC line-art LoRA from storyboard scene cuts) → 2e (per-character LoRAs) → 2g (Node 6 simplification). See `CLAUDE.md` "Node 7 v2 — locked decisions" for the full design walkthrough including the Phase 2-revision corrections, and `docs/PLAN.md` "NODE 7 v2 — Phase 2 Flux migration" for the implementation roadmap.

## Running Node 8 (scene assembly — pure-Python, no GPU)

Node 8 takes Node 7's per-character refined PNGs (one 512×512 PNG per character per key pose) and composites them onto a single source-MP4-resolution frame per key pose, ready for Node 9 to translate-and-copy held frames from. The bbox is the single source of truth for placement: Node 5 wrote it, Node 7 cropped with it, Node 8 places back with it. Refined characters are **feet-pinned** — their feet land at `bbox.bottomY`, never floating in the middle of the bbox. Z-order is bbox.bottomY ascending so lower-on-screen characters paint on top. Output is BnW-thresholded (no dilate/erode normalization in v1).

When Node 7 marked a generation as `error` (or its refined PNG turned out empty), Node 8 falls back to **substitute-rough**: pastes the rough key-pose pixels at the same bbox location and appends a structured warning. Same warn-and-reconcile pattern as Node 5 — keeps timing intact for Node 9, gives the operator a clear list of which key poses need re-generation. CLI still exits 0.

Pure-Python (PIL + numpy). Same code runs from CLI, pytest, CI, and ComfyUI. No GPU needed; no pod needed.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node8.py --node7-result /path/to/work/node7_result.json

# Windows embedded Python (local dev with ComfyUI portable):
"C:\...\ComfyUI_windows_portable\python_embeded\python.exe" run_node8.py \
    --node7-result <n7>

# Override background (default white; only 'white' supported in v1):
python run_node8.py --node7-result <n7> --background white
```

**Flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--node7-result <path>` | *required* | Aggregate manifest written by Node 7 |
| `--background <color>` | `white` | Canvas background color. v1 supports `white` only (matches the BnW deliverable) |
| `--quiet` | off | Suppress the success summary line |

**Output layout** (added next to Nodes 3–7):

```
<work-dir>/
  node7_result.json         (from Node 7)
  node8_result.json         aggregate: per-shot keyPose / composed / substitute counts
  <shotId>/
    refined_map.json        (from Node 7)
    refined/                (from Node 7)
    composed_map.json       per-key-pose record (Node 8)
    composed/
      000_composite.png     RGB, source MP4 res, white bg, BnW (Node 8)
      001_composite.png
      ...
```

`composed_map.json` lists per-key-pose `characters[]` (identity, boundingBox, status, substitutedFromRough) plus `warnings[]` for every Node-7 error or empty-refined-PNG event Node 8 had to substitute around. Node 9 reads `composed_map.json` directly.

**Exit codes:** `0` success (substitute-rough warnings are still exit 0), `1` `Node8Error` (`Node7ResultInputError`, `RefinedPngError`, `CompositingError`), `2` unexpected.

## Running Node 9 (timing reconstruction — pure-Python, no GPU)

Node 9 takes Node 8's per-key-pose composites + Node 4's per-frame timing map (`keypose_map.json`) and rebuilds the **full per-frame sequence**. For every frame in the original timeline: anchor frames are bit-identical copies of Node 8's composite; held frames are pasted onto a fresh white canvas at offset `(dx, dy)` from `keypose_map.json` — exposed regions stay white. **Zero AI regeneration on held frames** — they're pure pixel-translates of refined anchors. This is the whole reason Node 4 went translation-aware in the first place.

Whole-frame translation, not per-character (per-character placement was already baked into Node 8's composite). Off-canvas translates (where the character has slid entirely off-screen) are not errors — they produce mostly-white frames, which is mathematically valid.

Pure-Python (PIL + numpy). Same code runs from CLI, pytest, CI, and ComfyUI. No GPU needed; no pod needed.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node9.py --node8-result /path/to/work/node8_result.json

# Windows embedded Python (local dev with ComfyUI portable):
"C:\...\ComfyUI_windows_portable\python_embeded\python.exe" run_node9.py \
    --node8-result <n8>
```

**Flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--node8-result <path>` | *required* | Aggregate manifest written by Node 8. Node 9 chases pointers from here to `composed_map.json` (Node 8) → shot root → `keypose_map.json` (Node 4); no second `--node4-` flag needed |
| `--quiet` | off | Suppress the success summary line |

**Output layout** (added next to Nodes 3–8):

```
<work-dir>/
  node8_result.json         (from Node 8)
  node9_result.json         aggregate: per-shot totalFrames / anchor + held counts
  <shotId>/
    composed_map.json       (from Node 8)
    composed/               (from Node 8)
    keypose_map.json        (from Node 4 — Node 9 reads this for per-frame timing)
    timed_map.json          per-frame record (Node 9)
    timed/
      frame_0001.png        RGB, source MP4 res, white bg (Node 9)
      frame_0002.png
      ...
      frame_NNNN.png        — one PNG per frame of the original shot
```

`timed_map.json` lists per-frame `{frameIndex, sourceKeyPoseIndex, offset, composedSourcePath, timedPath, isAnchor}`. Node 10 reads `timed_map.json` for the encode order.

**Exit codes:** `0` success, `1` `Node9Error` (`Node8ResultInputError`, `KeyPoseMapInputError`, `TimingReconstructionError`, `FrameCountMismatchError`), `2` unexpected.

## Running Node 10 (PNG → MP4 — pure-Python, no GPU)

Node 10 takes Node 9's full per-frame PNG sequence (`<shot>/timed/frame_NNNN.png`) and encodes it into a single deliverable MP4 per shot at 25 FPS. Output goes to `<work-dir>/output/<shotId>_refined.mp4` so all shots' deliverables collect in one place for client hand-off.

Encoding is via the `imageio-ffmpeg` static binary (same wheel Node 3 uses for decode — no system ffmpeg dependency). Codec is **H.264 (libx264)** + **yuv420p** + **medium preset** at **CRF 18** for visually lossless BnW line art; only CRF is tunable via `--crf`. Frame rate is hardcoded to 25 FPS (the locked project convention).

After encode, post-verification via `imageio_ffmpeg.count_frames_and_secs` confirms the output has the expected frame count and duration — catches silent ffmpeg corruption (exit 0 but malformed file). **Odd canvas dimensions fail-loud** (libx264 requires even W/H; auto-padding would silently desync from Node 9's translate-and-copy positions).

Pure-Python (subprocess + imageio_ffmpeg + json). No GPU; no pod. **Does NOT delete upstream artifacts** — `timed/`, `composed/`, `refined/`, etc. all stay on disk for debugging and Part 2 (ToonCrafter) reuse.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node10.py --node9-result /path/to/work/node9_result.json

# Windows embedded Python (local dev with ComfyUI portable):
"C:\...\ComfyUI_windows_portable\python_embeded\python.exe" run_node10.py \
    --node9-result <n9>

# Tighter file size (smaller files, slightly visible edge artifacts):
python run_node10.py --node9-result <n9> --crf 23
```

**Flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--node9-result <path>` | *required* | Aggregate manifest written by Node 9. Node 10 chases pointers from here to per-shot `timed_map.json` → `<shot>/timed/` directory |
| `--crf <int>` | `18` | H.264 CRF value. Lower = higher quality and bigger files. CRF 18 is visually lossless on BnW line art; 23 (libx264 default) trades visible edge artifacts for smaller files |
| `--quiet` | off | Suppress the success summary line |

**Output layout** (added next to Nodes 3–9):

```
<work-dir>/
  node9_result.json                (from Node 9)
  node10_result.json               aggregate: per-shot codec / fps / frame count / size
  output/                          NEW: deliverables collect here for client hand-off
    shot_001_refined.mp4
    shot_002_refined.mp4
    ...
  <shotId>/
    timed/                         (from Node 9 — preserved, NOT deleted)
    composed/                      (from Node 8 — preserved)
    refined/                       (from Node 7 — preserved)
    ...
```

`node10_result.json` lists per-shot `{shotId, outputPath, frameCount, durationSeconds, codec, fps, fileSizeBytes}`. There's no per-shot manifest — Node 10's output is the deliverable itself.

**Exit codes:** `0` success, `1` `Node10Error` (`Node9ResultInputError`, `TimedFramesError`, `FFmpegEncodeError`), `2` unexpected.

## Running Node 11 (full pipeline orchestrator)

Node 11 is the **project-level orchestrator**. One CLI invocation runs Nodes 2-10 in sequence against a single batch, replacing what was previously an eight-command shell sequence. Per-node retry policy + JSONL progress log + final aggregate report.

The operator's previous workflow:
```bash
python run_node2.py --input-dir <i>
python run_node3.py --queue <i>/queue.json --work-dir <w>
python run_node4.py --node3-result <w>/node3_result.json
python run_node5.py --node4-result <w>/node4_result.json --queue <i>/queue.json
python run_node6.py --node5-result <w>/node5_result.json --queue <i>/queue.json --characters <i>/characters.json
python run_node7.py --node6-result <w>/node6_result.json --queue <i>/queue.json
python run_node8.py --node7-result <w>/node7_result.json
python run_node9.py --node8-result <w>/node8_result.json
python run_node10.py --node9-result <w>/node9_result.json
```

…becomes:

```bash
python run_node11.py --input-dir <i> --work-dir <w>
```

Each downstream node is invoked as a subprocess so failure modes are identical to running the chain by hand. Stdout/stderr from each node passes through to your terminal in real time, AND every line is tee'd to `<work-dir>/node11_progress.jsonl` for post-mortem inspection. Pre-Node-7, Node 11 best-effort calls `nvidia-smi` and logs the GPU info (warns but proceeds if not available).

**Partial-success semantic:** unlike Nodes 2-10 (which fail the whole batch on any error), Node 11 owns the partial-success case — if some shots succeed and some fail, exit code is **0** with `failedShots > 0` recorded in `node11_result.json` for CI to read. Only a 100% failure rate (`BatchAllFailedError`) exits 1.

**Invoke:**

```bash
# Standard full pipeline (RunPod, GPU available):
python run_node11.py --input-dir /path/to/input --work-dir /path/to/work

# Laptop test path: skip Node 7's live ComfyUI submission, exercise
# everything else (substitute-rough fallback fills the refined slots):
"C:\...\ComfyUI_windows_portable\python_embeded\python.exe" run_node11.py \
    --input-dir <i> --work-dir <w> --dry-run

# Allow Node 7 to retry twice on transient ComfyUI hangs:
python run_node11.py --input-dir <i> --work-dir <w> --retry-node7 2

# Override Node 10 quality (smaller files, slightly visible artifacts):
python run_node11.py --input-dir <i> --work-dir <w> --crf 23
```

**Flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--input-dir <path>` | *required* | Directory containing Node 1's downloads (metadata.json + characters.json + sheet PNGs + shot MP4s). Same dir Node 2 reads |
| `--work-dir <path>` | *required* | Where every downstream node writes outputs |
| `--comfyui-url <url>` | `http://127.0.0.1:8188` | Passed to Node 7 |
| `--crf <int>` | `18` | Passed to Node 10 |
| `--retry-nodeN <int>` | `0` | Per-node retry on subprocess non-zero exit. Most useful for `--retry-node7` |
| `--dry-run` | off | Pass `--dry-run` to Node 7 (skip live ComfyUI; record every generation as `skipped`). Useful for testing the orchestration plumbing without GPU |
| `--quiet` | off | Suppress the success summary line |

**Output layout:**

```
<work-dir>/
  node2_result.json ... node10_result.json   (from Nodes 2-10)
  node11_progress.jsonl   NEW: append-only event log (start/stop/exit per node-step)
  node11_result.json      NEW: aggregate batch report
  output/                 (from Node 10)
    shot_001_refined.mp4  ← deliverables
    shot_002_refined.mp4
    ...
  <shotId>/                (per-shot intermediates from every node)
    frames/, keyposes/, character_map.json, ..., timed/, ...
```

`node11_result.json` carries: per-node-step status + timing + exit code, per-shot status + failingNode + refinedMp4Path, total batch wall time, succeeded/failed counts. `node11_progress.jsonl` is the append-only event log — `tail -f` it during long pod runs.

**Exit codes (DIFFER from Nodes 2-10):** `0` success or partial success (read `failedShots` in JSON), `1` `Node11Error` (`InputDirError`, `NodeStepError`, `BatchAllFailedError`), `2` unexpected.

## Running on RunPod

```bash
git clone https://github.com/Omkar8369/animatic-refinement.git
cd animatic-refinement
bash runpod_setup.sh
```

`runpod_setup.sh` installs system deps (ffmpeg, git-lfs), installs Python deps via the aggregator `requirements.txt` (which `-r`-includes every per-node requirements file), and symlinks `custom_nodes/` into ComfyUI under `/workspace/ComfyUI`.

## Tests

```bash
python -m pytest tests/ -v
```

## Design

Canonical design lives in **[docs/PLAN.md](docs/PLAN.md)** — 11-node pipeline, each node with lettered sub-steps (1A…11E). **One node at a time** is the build convention. The same structure is mirrored in `docs/Node_Plan.xlsx` (editable working spec). Session handoff notes + locked decisions live in `CLAUDE.md`.
