# Animatic Refinement Workflow — Part 1

AI pipeline that converts rough MP4 animatic shots (Chota Bhim Indian cartoon style) into **BnW line-art MP4s** with refined, reference-accurate characters in correct positions and poses.

> Part 2 (ToonCrafter frame interpolation) is tracked separately.

## What this repo contains

| Path | Purpose |
|---|---|
| `frontend/` | Node 1 — HTML form that captures per-shot metadata and writes `metadata.json` |
| `pipeline/` | Node 2 + Node 11 — orchestrator that validates metadata and drives the batch |
| `custom_nodes/` | ComfyUI custom nodes for Nodes 3–10 (one folder per node) |
| `workflows/` | ComfyUI workflow graph JSONs that wire the custom nodes together |
| `docs/` | Node-wise plan (Markdown + Excel) — the canonical design document |
| `tests/` | Per-node tests and end-to-end fixtures |

## How to run locally

```bash
# 1. Clone the repo
git clone <this-repo-url>
cd animatic-refinement

# 2. Create a Python venv and install deps
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt

# 3. Point ComfyUI to our custom_nodes folder
# (either symlink or set COMFYUI_CUSTOM_NODES_PATH)
```

## How to run on RunPod

```bash
git clone <this-repo-url>
cd animatic-refinement
bash runpod_setup.sh
```

`runpod_setup.sh` installs system deps (FFmpeg), Python deps, and registers our custom nodes with ComfyUI.

## Design

The canonical design is **[docs/PLAN.md](docs/PLAN.md)** — an 11-node pipeline, each node with alphabetic sub-steps (1A…11E). **One node at a time** is the editing convention; see the plan for Inputs/Outputs/Tools per sub-step.

## Status

| Node | Name | Status |
|---|---|---|
| 1 | Project Input & Setup Interface | Pending |
| 2 | Metadata Ingestion & Validation | Pending |
| 3 | Shot Pre-processing (MP4 → PNG) | Pending |
| 4 | Key Pose Extraction | Pending |
| 5 | Character Detection & Position | Pending |
| 6 | Character Reference Sheet Matching | Pending |
| 7 | AI-Powered Pose Refinement | Pending |
| 8 | Scene Assembly | Pending |
| 9 | Timing Reconstruction | Pending |
| 10 | Output Generation (PNG → MP4) | Pending |
| 11 | Batch Management | Pending |
