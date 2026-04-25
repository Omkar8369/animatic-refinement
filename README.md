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
| 5 | Character Detection & Position | **DONE** — 50 tests pass (122 repo-wide); CLI + wrapper + ComfyUI node verified; end-to-end Node 2→3→4→5 smoke test passes on real MP4 |
| 6 | Character Reference Sheet Matching | **DONE** — 34 tests pass (156 repo-wide); CLI + wrapper + ComfyUI node verified; end-to-end Node 2→3→4→5→6 smoke test passes; classical alpha-island slicing + multi-signal angle scoring + DoG line-art |
| 7 | AI-Powered Pose Refinement | **DONE — live-verified** — 47 tests pass (207 repo-wide); CLI + `run_node7.py` wrapper + ComfyUI custom node verified in dry-run on embedded Python; two workflow templates (dwpose + lineart-fallback) + `models.json` weight pins shipped; **first live RunPod run (2026-04-25) produced 2 PNGs / 0 errors in 36s on the 2-character synthetic smoke fixture via the lineart-fallback route.** Bringup notes for the runpod-slim pod image (symlink + `extra_model_paths.yaml` + `IPAdapter.weight_type` quirks) captured in `tools/POD_NOTES_runpod_slim.md`. DWPose route shipped but not yet exercised (no human-only smoke shot yet). |
| 8 | Scene Assembly | Pending |
| 9 | Timing Reconstruction | Pending |
| 10 | Output Generation (PNG → MP4) | Pending |
| 11 | Batch Management | Pending |

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
