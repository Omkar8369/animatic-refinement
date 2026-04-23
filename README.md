# Animatic Refinement Workflow — Part 1

AI pipeline that converts rough MP4 animatic shots (Chota Bhim Indian cartoon style) into **BnW line-art MP4s** with refined, reference-accurate characters in correct positions and poses.

> Part 2 (ToonCrafter frame interpolation) is tracked separately.

## What this repo contains

| Path | Purpose |
|---|---|
| `frontend/` | Node 1 — Character Library page + Shot Metadata form (browser-only; writes `characters.json` + `metadata.json`) |
| `pipeline/` | Nodes 2 + 3 (+ Node 11 later) — pure-Python, GPU-agnostic core logic (validator, frame extractor, batch manager) |
| `run_node2.py` / `run_node3.py` | Thin repo-root wrappers so each node's CLI runs on both Windows embedded Python and standard Python |
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
| 4 | Key Pose Extraction | **NEXT** |
| 5 | Character Detection & Position | Pending |
| 6 | Character Reference Sheet Matching | Pending |
| 7 | AI-Powered Pose Refinement | Pending |
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
