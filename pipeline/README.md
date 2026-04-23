# `pipeline/` — Nodes 2, 3 (+ Node 11 later)

Pure-Python, zero GPU deps, zero ComfyUI dependency. Runs identically on:

- **Local Windows** with ComfyUI's embedded Python (via `run_node2.py` / `run_node3.py` wrappers — required because the embedded `python313._pth` ignores `PYTHONPATH`)
- **Standard Linux Python on RunPod** (via the wrappers or `python -m pipeline.cli` / `python -m pipeline.cli_node3` directly)

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

## Files

| File | Purpose |
|---|---|
| `schemas.py` | Pydantic v2 models for `metadata.json` (`MetadataFile`) and `characters.json` (`CharactersFile`) |
| `errors.py` | Typed hierarchy: `PipelineError` base → `Node2Error` + `Node3Error` subtrees |
| `node2.py` | Node 2 core: `validate_and_build_queue(input_dir) -> ProcessingQueue`, `serialize_queue(queue)`, and the private sub-step helpers (2A–2E) |
| `cli.py` | Node 2 argparse entrypoint; exit 0/1/2 |
| `node3.py` | Node 3 core: `extract_frames_for_queue(queue_path, work_dir) -> Node3Result`, `extract_frames_for_shot(...)`, sub-step helpers (3A–3E) |
| `cli_node3.py` | Node 3 argparse entrypoint; exit 0/1/2 |
| `requirements.txt` | `pydantic>=2.5,<3` + `imageio-ffmpeg>=0.5,<1` — the full Node 2+3 runtime |

## Invoke

```bash
# Node 2 — validate inputs + build queue.json
python run_node2.py --input-dir <path>
python -m pipeline.cli --input-dir <path>

# Node 3 — decode MP4s to PNG frames
python run_node3.py --queue <queue.json> --work-dir <work>
python -m pipeline.cli_node3 --queue <queue.json> --work-dir <work>
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

**Exit codes (both nodes):** `0` success · `1` expected error (`Node2Error` / `Node3Error` subclass) · `2` unexpected error.

Node 3 treats frame-count warnings as data, not errors — CLI still exits `0` when only warnings fire.

## Test

```bash
python -m pytest tests/ -v
```

46 tests cover: Node 2 (schema validation, cross-refs, shot-ID integrity, CLI exit codes — 26 tests) and Node 3 (happy path, warnings, queue-input errors, ffmpeg errors, CLI integration — 20 tests).

## See also

- `docs/PLAN.md` → Node 2 and Node 3 sections (locked decisions + sub-steps)
- `docs/Node_Plan.xlsx` → rows 10–21
- `CLAUDE.md` → locked decisions per node + per-node ship checklist
- `../custom_nodes/node_03_mp4_to_png/` → ComfyUI wrapper (reference template for Nodes 4–10)
