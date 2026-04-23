# Animatic Refinement Workflow — Part 1

AI pipeline that converts rough MP4 animatic shots (Chota Bhim Indian cartoon style) into **BnW line-art MP4s** with refined, reference-accurate characters in correct positions and poses.

> Part 2 (ToonCrafter frame interpolation) is tracked separately.

## What this repo contains

| Path | Purpose |
|---|---|
| `frontend/` | Node 1 — Character Library page + Shot Metadata form (browser-only; writes `characters.json` + `metadata.json`) |
| `pipeline/` | Node 2 (+ Node 11 later) — schema/cross-reference validator that builds the batched processing queue |
| `run_node2.py` | Thin repo-root wrapper so Node 2's CLI runs on both Windows embedded Python and standard Python |
| `custom_nodes/` | ComfyUI custom nodes for Nodes 3–10 (one folder per node) |
| `workflows/` | ComfyUI workflow graph JSONs wiring the custom nodes |
| `docs/` | `PLAN.md` + `Node_Plan.xlsx` — canonical node-by-node design |
| `tests/` | Per-node tests (pytest) |
| `runpod_setup.sh` | One-shot bootstrap for a fresh RunPod pod |

## Status

| Node | Name | Status |
|---|---|---|
| 1 | Project Input & Setup Interface | **DONE** — initial build, awaiting first real-shot test |
| 2 | Metadata Ingestion & Validation | **DONE** — 26 tests pass; CLI + wrapper verified on embedded Python |
| 3 | Shot Pre-processing (MP4 → PNG) | **NEXT** |
| 4 | Key Pose Extraction | Pending |
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
