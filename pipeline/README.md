# `pipeline/` — Nodes 2, 3, 4 (+ Node 11 later)

Pure-Python, zero GPU deps, zero ComfyUI dependency. Runs identically on:

- **Local Windows** with ComfyUI's embedded Python (via `run_node2.py` / `run_node3.py` / `run_node4.py` wrappers — required because the embedded `python313._pth` ignores `PYTHONPATH`)
- **Standard Linux Python on RunPod** (via the wrappers or `python -m pipeline.cli` / `python -m pipeline.cli_node3` / `python -m pipeline.cli_node4` directly)

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

## Files

| File | Purpose |
|---|---|
| `schemas.py` | Pydantic v2 models for `metadata.json` (`MetadataFile`) and `characters.json` (`CharactersFile`) |
| `errors.py` | Typed hierarchy: `PipelineError` base → `Node2Error` + `Node3Error` + `Node4Error` subtrees |
| `node2.py` | Node 2 core: `validate_and_build_queue(input_dir) -> ProcessingQueue`, `serialize_queue(queue)`, and the private sub-step helpers (2A–2E) |
| `cli.py` | Node 2 argparse entrypoint; exit 0/1/2 |
| `node3.py` | Node 3 core: `extract_frames_for_queue(queue_path, work_dir) -> Node3Result`, `extract_frames_for_shot(...)`, sub-step helpers (3A–3E) |
| `cli_node3.py` | Node 3 argparse entrypoint; exit 0/1/2 |
| `node4.py` | Node 4 core: `extract_keyposes_for_queue(node3_result_path, threshold, max_edge) -> Node4Result`, `extract_keyposes_for_shot(...)`, phase-correlation + aligned-MAE helpers, sub-step helpers (4A–4E) |
| `cli_node4.py` | Node 4 argparse entrypoint; exit 0/1/2 |
| `requirements.txt` | `pydantic>=2.5,<3` + `imageio-ffmpeg>=0.5,<1` + `numpy>=1.26,<3` + `pillow>=10,<12` — the full Node 2+3+4 runtime |

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

**Exit codes (all three nodes):** `0` success · `1` expected error (`Node2Error` / `Node3Error` / `Node4Error` subclass) · `2` unexpected error.

Node 3 treats frame-count warnings as data, not errors — CLI still exits `0` when only warnings fire.

## Test

```bash
python -m pytest tests/ -v
```

72 tests cover: Node 2 (schema validation, cross-refs, shot-ID integrity, CLI exit codes — 26 tests), Node 3 (happy path, warnings, queue-input errors, ffmpeg errors, CLI integration — 20 tests), and Node 4 (static/slide/two-pose fixtures, threshold behavior, aggregate + per-shot API, input/extraction error paths, CLI — 26 tests).

## See also

- `docs/PLAN.md` → Node 2, Node 3, and Node 4 sections (locked decisions + sub-steps)
- `docs/Node_Plan.xlsx` → rows 10–27
- `CLAUDE.md` → locked decisions per node + per-node ship checklist
- `../custom_nodes/node_03_mp4_to_png/` + `../custom_nodes/node_04_keypose_extractor/` → ComfyUI wrappers (reference template for Nodes 5–10)
