# `pipeline/` — Nodes 2, 3, 4, 5 (+ Node 11 later)

Pure-Python, zero GPU deps, zero ComfyUI dependency. Runs identically on:

- **Local Windows** with ComfyUI's embedded Python (via `run_node2.py` / `run_node3.py` / `run_node4.py` / `run_node5.py` wrappers — required because the embedded `python313._pth` ignores `PYTHONPATH`)
- **Standard Linux Python on RunPod** (via the wrappers or `python -m pipeline.cli` / `python -m pipeline.cli_node3` / `python -m pipeline.cli_node4` / `python -m pipeline.cli_node5` directly)

**Architectural convention** (locked at Node 3): all per-node business logic lives here; each ComfyUI custom node under `../custom_nodes/node_NN_*/` is a thin wrapper that only does input-type declaration + a one-liner into the core function. Never fork logic into the wrapper — add a parameter here and pass it down.

## Node 2 — Metadata Ingestion & Validation

Reads an input folder containing `metadata.json` + `characters.json` + sheet PNGs + shot MP4s. Validates both JSON schemas, confirms every referenced file exists on disk, checks shot-ID uniqueness and sequence, and writes `queue.json` — the contract Node 3 reads.

**Design decisions (locked, see `CLAUDE.md`):**

- **Hard-fail the entire batch** on any validation error (no per-shot skipping).
- **Schema validation via pydantic v2**, `extra="forbid"` on every model.
- **Every error lists ALL offenders**, not just the first, so the operator fixes everything in one pass.
- **Input layout is flat** — one folder holds everything.

## Node 3 — Shot Pre-processing (MP4 → PNG)

Reads `queue.json`, decodes each shot's MP4 into `<work-dir>/<shotId>/frame_NNNN.png`, writes a per-shot `_manifest.json` and an aggregate `<work-dir>/node3_result.json`.

**Design decisions (locked, see `CLAUDE.md`):**

- **ffmpeg via `imageio-ffmpeg` pip wheel** — no system dep, works identically on Windows embedded Python, RunPod Linux, and CI.
- **1:1 decode, no `-r` flag.** Any resampling would silently corrupt timing.
- **Per-shot folder** `<work-dir>/<shotId>/frame_NNNN.png` (1-indexed, 4-digit padded).
- **Fail-fast on hard errors**, **warn-and-continue on frame-count drift.** Mismatch between `durationFrames` and actual decoded count is a structured warning in `node3_result.json`; Node 9 uses the actual count.
- **Reruns wipe stale frames** before decoding so `_manifest.json` always matches the directory exactly.

## Node 4 — Key Pose Extraction (Translation-Aware)

Reads `node3_result.json`, walks each shot's PNG frames in order, phase-correlates every frame against the current key-pose anchor on a downscaled grayscale copy, computes aligned MAE over the overlap region, and partitions into key poses + held frames with per-held `(dy, dx)` offsets in full-res pixels. Writes `<work-dir>/<shotId>/keypose_map.json` and an aggregate `<work-dir>/node4_result.json`; copies the chosen key-pose PNGs (preserving source filenames) into `<work-dir>/<shotId>/keyposes/`.

**Design decisions (locked, see `CLAUDE.md`):**

- **Translation-aware comparison.** Phase correlation + aligned MAE, not pixel-identity. A character sliding across the frame without changing pose collapses to ONE key pose + per-held-frame offsets. Node 9 replays slides by translate-and-copy — no re-refinement by Node 7.
- **Global default threshold 8.0** on 0–255 grayscale aligned MAE; exposed as `--threshold`. No per-shot adaptive tuning.
- **`max_edge = 128`** downscale for the compare (LANCZOS). Offsets scaled back to full-res on write. Keeps FFT fast (~10ms/frame) and makes the MAE metric resolution-independent.
- **No minimum held-run length.** Translation-aware compare already solved the slide false-positive; a floor would silently change timing that Node 9 must replay frame-accurately.
- **Key-pose copies preserve source filenames** (e.g. `frame_0004.png`, not `key_pose_01.png`) so Node 5 / Node 7 don't re-map identities between nodes.
- **Schema-version guard** on `node3_result.json` mirrors Node 3's guard on `queue.json` — loudly refuses `schemaVersion != 1`.
- **Rerun safety:** `keyposes/` is wiped of stale `frame_*.png` before each run so `keypose_map.json` always matches the directory.

## Node 5 — Character Detection & Position

Reads **both** `node4_result.json` (for each shot's key-pose frame paths) and `queue.json` (for each shot's expected character identities + positions). For every key pose, runs classical connected-component detection on an Otsu-binarized copy of the frame, merges overlapping bounding boxes by IoU, bins each silhouette into a position zone (L/CL/C/CR/R) by normalized centre-x, and zips detections left→right with metadata characters sorted by position rank (Strategy A — positional identity assignment). Writes `<work-dir>/<shotId>/character_map.json` and an aggregate `<work-dir>/node5_result.json`.

**Design decisions (locked, see `CLAUDE.md`):**

- **Classical connected components, no ML, no GPU.** Otsu binarization + `scipy.ndimage.label` (8-connectivity) + IoU-based bbox merge. Chota Bhim animatics are clean BnW line-art on solid backgrounds; ML segmentation would overfit to the training distribution and miss stylized characters. Same tool everywhere (CLI, tests, CI, ComfyUI).
- **Per-key-pose detection**, not per-shot. Each key pose is detected independently so Node 6 gets fresh position data per pose — a character that moves between key poses is correctly tracked, not assumed to stay put.
- **Locked 25/20/10/20/25 position split.** Thresholds `(0.25, 0.45, 0.55, 0.75)` on normalized centre-x map to codes `L / CL / C / CR / R`. `C` is the narrowest bin (10%) because true-centre compositions are rare; `L` and `R` are the widest (25%) because off-centre placement is typical in 2-character shots.
- **Strategy A identity assignment (positional).** Sort detections left→right by centre-x, sort metadata characters by position rank (L < CL < C < CR < R), zip them together. Pose-based reference-sheet matching (Strategy B) is deferred to Node 6 where the reference sheet is actually sliced.
- **Warn-and-reconcile on count mismatch**, never raise. If detection count != `metadata.characterCount`: over-detect → sort by area, drop smallest + warn `count-mismatch-over` + `reconcile-dropped`; under-detect → apply progressive `binary_erosion` x1/x2/x3 to split touching characters + warn `count-mismatch-under` + `reconcile-eroded` or `reconcile-failed`. All reconcile actions are logged to `character_map.json`'s `warnings[]` array; CLI still exits `0`.
- **Thin ComfyUI wrapper** (same template as Nodes 3 + 4). All logic here; `custom_nodes/node_05_character_detector/` only declares `INPUT_TYPES` + `RETURN_TYPES` and calls `detect_characters_for_queue()`.
- **Rerun safety:** reruns overwrite `character_map.json` + `node5_result.json` atomically; no stale files linger.

## Files

| File | Purpose |
|---|---|
| `schemas.py` | Pydantic v2 models for `metadata.json` (`MetadataFile`) and `characters.json` (`CharactersFile`) |
| `errors.py` | Typed hierarchy: `PipelineError` base → `Node2Error` + `Node3Error` + `Node4Error` + `Node5Error` subtrees |
| `node2.py` | Node 2 core: `validate_and_build_queue(input_dir) -> ProcessingQueue`, `serialize_queue(queue)`, and the private sub-step helpers (2A–2E) |
| `cli.py` | Node 2 argparse entrypoint; exit 0/1/2 |
| `node3.py` | Node 3 core: `extract_frames_for_queue(queue_path, work_dir) -> Node3Result`, `extract_frames_for_shot(...)`, sub-step helpers (3A–3E) |
| `cli_node3.py` | Node 3 argparse entrypoint; exit 0/1/2 |
| `node4.py` | Node 4 core: `extract_keyposes_for_queue(node3_result_path, threshold, max_edge) -> Node4Result`, `extract_keyposes_for_shot(...)`, phase-correlation + aligned-MAE helpers, sub-step helpers (4A–4E) |
| `cli_node4.py` | Node 4 argparse entrypoint; exit 0/1/2 |
| `node5.py` | Node 5 core: `detect_characters_for_queue(node4_result_path, queue_path, min_area_ratio, merge_iou) -> Node5Result`, `detect_characters_for_shot(...)`, Otsu + connected-component + IoU-merge + reconcile + position-binning helpers, sub-step helpers (5A–5E) |
| `cli_node5.py` | Node 5 argparse entrypoint; exit 0/1/2 |
| `requirements.txt` | `pydantic>=2.5,<3` + `imageio-ffmpeg>=0.5,<1` + `numpy>=1.26,<3` + `pillow>=10,<12` + `scipy>=1.11,<2` — the full Node 2+3+4+5 runtime |

## Invoke

```bash
# Node 2 — validate inputs + build queue.json
python run_node2.py --input-dir <path>
python -m pipeline.cli --input-dir <path>

# Node 3 — decode MP4s to PNG frames
python run_node3.py --queue <queue.json> --work-dir <work>
python -m pipeline.cli_node3 --queue <queue.json> --work-dir <work>

# Node 4 — partition PNG frames into key poses + held frames
python run_node4.py --node3-result <work>/node3_result.json
python -m pipeline.cli_node4 --node3-result <work>/node3_result.json

# Node 5 — detect characters + assign identities on each key pose
python run_node5.py --node4-result <work>/node4_result.json --queue <queue.json>
python -m pipeline.cli_node5 --node4-result <work>/node4_result.json --queue <queue.json>
```

**Node 2 flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--input-dir <path>` | *required* | Folder with `metadata.json` + `characters.json` + sheets + MP4s |
| `--output-file <path>` | `<input-dir>/queue.json` | Where to write the resolved queue |
| `--quiet` | off | Suppress the success line on stdout |

**Node 3 flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--queue <path>` | *required* | `queue.json` from Node 2 |
| `--work-dir <path>` | *required* | Folder to write per-shot frame folders + `node3_result.json` |
| `--quiet` | off | Suppress the success + warning-preview lines |

**Node 4 flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--node3-result <path>` | *required* | `node3_result.json` from Node 3 |
| `--threshold <float>` | `8.0` | Aligned-MAE threshold on 0–255 grayscale; frames above this become new key poses |
| `--max-edge <int>` | `128` | Downscale so `max(H, W) = N` before FFT + MAE; offsets scaled back on write |
| `--quiet` | off | Suppress the success line |

**Node 5 flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--node4-result <path>` | *required* | `node4_result.json` from Node 4 |
| `--queue <path>` | *required* | `queue.json` from Node 2 — supplies expected character identities + positions per shot |
| `--min-area-ratio <float>` | `0.001` | Drop connected-component blobs whose area is below this fraction of frame area |
| `--merge-iou <float>` | `0.5` | Merge two bounding boxes whose IoU meets or exceeds this threshold |
| `--quiet` | off | Suppress the success line |

**Exit codes (all four nodes):** `0` success · `1` expected error (`Node2Error` / `Node3Error` / `Node4Error` / `Node5Error` subclass) · `2` unexpected error.

Node 3 treats frame-count warnings as data, not errors — CLI still exits `0` when only warnings fire. Node 5 treats count-mismatch reconcile actions the same way — warnings fire, CLI still exits `0`.

## Test

```bash
python -m pytest tests/ -v
```

122 tests cover: Node 2 (schema validation, cross-refs, shot-ID integrity, CLI exit codes — 26 tests), Node 3 (happy path, warnings, queue-input errors, ffmpeg errors, CLI integration — 20 tests), Node 4 (static/slide/two-pose fixtures, threshold behavior, aggregate + per-shot API, input/extraction error paths, CLI — 26 tests), and Node 5 (single / two-character / overlapping / touching fixtures, count-reconcile over & under, position binning at every bin edge, IoU + merge, Strategy A identity zip, aggregate + per-shot API, node4-result + queue input errors, CLI — 50 tests).

## See also

- `docs/PLAN.md` → Node 2, Node 3, Node 4, and Node 5 sections (locked decisions + sub-steps)
- `docs/Node_Plan.xlsx` → rows 10–33
- `CLAUDE.md` → locked decisions per node + per-node ship checklist
- `../custom_nodes/node_03_mp4_to_png/` + `../custom_nodes/node_04_keypose_extractor/` + `../custom_nodes/node_05_character_detector/` → ComfyUI wrappers (reference template for Nodes 6–10)
