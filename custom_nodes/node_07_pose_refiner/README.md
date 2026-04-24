# Node 7 - AI-Powered Pose Refinement (Replace Rough With BnW Line Art)

See [../../docs/PLAN.md](../../docs/PLAN.md) Node 7 for the full spec and
[../../CLAUDE.md](../../CLAUDE.md) for the locked decisions.

## What it does

For every detection produced by Nodes 5+6, Node 7 submits a ComfyUI
workflow that generates a **BnW line-art version of that character in
the rough animatic's pose**, on a 512×512 transparent canvas. Node 8
composites them back onto the final frame.

The fundamental design insight (locked decision #1) is that pose must be
decoupled from identity:

- **Pose** comes from the rough key-pose PNG, via a pose ControlNet.
- **Identity** comes from the Node-6 reference **color** crop, via
  IP-Adapter-Plus.
- **txt2img** (not img2img) so the rough's scribbles / stray marks
  never bleed into the output.

Two workflow templates are shipped, switched per character via
`queue.json.batches[].characters[].poseExtractor`:

| Route | Template | Used for |
|---|---|---|
| `dwpose` (default) | `workflow.json` | Human characters. DWPose skeleton → DWPose ControlNet @ 0.75 |
| `lineart-fallback` | `workflow_lineart_fallback.json` | Non-human (quadrupeds, Jaggu). LineArt CN @ 0.6 + Scribble CN @ 0.6 from the rough crop. |

Both templates feed the reference color crop through IP-Adapter-Plus
@ 0.8 for identity.

## Runtime topology (locked decision #13)

**Node 7 runs on the RunPod pod, never the laptop.** The user's laptop
has neither the VRAM nor the checkpoint downloads for SD 1.5 +
ControlNet + IP-Adapter + DWPose. Flow:

1. Laptop runs Nodes 2-6 via their `run_nodeN.py` wrappers (pure CPU).
2. User syncs the work dir (queue.json, keyposes, character_map.json,
   reference_map.json, node6_result.json, reference_crops) up to the pod.
3. On the pod: `bash runpod_setup.sh` symlinks this folder into
   ComfyUI's `custom_nodes/` and downloads every weight in
   `models.json` via curl + sha256 check.
4. ComfyUI boots on the pod (port 8188).
5. `python run_node7.py --node6-result <path> --queue <path>` runs on
   the pod and POSTs each detection's workflow to ComfyUI's HTTP API.

The CLI has a `--dry-run` flag for laptop smoke tests: it exercises the
manifest layer end-to-end and writes `status="skipped"` generations
without ever touching ComfyUI.

## Inputs

### CLI
```
python run_node7.py \
    --node6-result <work>/node6_result.json \
    --queue        <input>/queue.json \
    [--comfyui-url http://127.0.0.1:8188] \
    [--per-prompt-timeout 600] \
    [--dry-run] [--quiet]
```

### ComfyUI node
| Name | Type | Purpose |
|---|---|---|
| `node6_result_path` | STRING | Absolute path to `node6_result.json` from Node 6 |
| `queue_path` | STRING | Absolute path to `queue.json` from Node 2 |
| `comfyui_url` | STRING | Default `http://127.0.0.1:8188`; ignored when `dry_run` is True |
| `dry_run` | BOOLEAN | Skip ComfyUI submission; record `skipped` generations only |

## Outputs

- Per shot: `<shotId>/refined_map.json` next to `reference_map.json` /
  `character_map.json` / `keyposes/`. Contains one `RefinedGeneration`
  per `(keyPoseIndex, identity)` with the refined PNG path, seed, CN
  strengths, and a `status` of `ok` / `skipped` / `error`.
- Per shot: `<shotId>/refined/` populated with the refined PNGs
  (`<NNN>_<identity>.png`, NNN = 3-digit key-pose index).
- Aggregate: `<work-dir>/node7_result.json` with a
  `ShotRefinedSummary` per shot (generated / skipped / error counts +
  refined-map path). Also records the `comfyUIUrl`, the `dryRun` flag,
  and a UTC `refinedAt` timestamp.
- ComfyUI-node return value: a JSON string of the full Node7Result
  payload, for chaining into downstream ComfyUI graph nodes.

## Exit codes (CLI)

| Code | Meaning |
|---|---|
| `0` | Success. Per-generation errors recorded in refined_map.json — CLI still exits 0. |
| `1` | `Node7Error` or `QueueLookupError` — manifest I/O, workflow-template, or whole-run ComfyUI-connection failure. |
| `2` | Unexpected error (bug, not operator error). |

## Key files in this folder

| File | Purpose |
|---|---|
| `__init__.py` | ComfyUI custom-node registration. Thin wrapper over `orchestrate.refine_queue`. |
| `manifest.py` | Pure-Python manifest I/O (GPU-agnostic, importable from CLI + tests + ComfyUI). |
| `comfyui_client.py` | Stdlib-urllib HTTP client for ComfyUI's `/prompt`, `/history`, `/view` endpoints. |
| `orchestrate.py` | Top-level driver: builds the routing table, parameterizes the right workflow template per detection, submits + polls + downloads. |
| `workflow.json` | DWPose workflow template (humans). |
| `workflow_lineart_fallback.json` | LineArt + Scribble fallback workflow (non-humans). |
| `models.json` | Weight pins consumed by `runpod_setup.sh` (curl + sha256 verify). |

## Extending the workflow templates

Both `workflow.json` and `workflow_lineart_fallback.json` are ComfyUI
**API-format** graphs (export via ComfyUI's "Save (API Format)" menu,
not the regular "Save"). Certain node IDs are contractually fixed
because `orchestrate.py` parameterizes them:

| Node ID | Role (see `_role` field) | Parameterized fields |
|---|---|---|
| `"3"` | `ksampler` | `seed`, `steps`, `cfg`, `sampler_name`, `scheduler` |
| `"6"` | `positive-prompt` | `text` (POSITIVE_PROMPT_TEMPLATE) |
| `"7"` | `negative-prompt` | `text` (NEGATIVE_PROMPT constant) |
| `"11"` | `load-key-pose` | `image` = rough key-pose PNG path |
| `"12"` | `load-reference-color` | `image` = Node 6 color crop path |
| `"20"` | `save-image` | `filename_prefix` |

If re-export renames these IDs, update `orchestrate.py`'s `NODE_*`
constants in the same commit — the orchestrator fails loud with a
`WorkflowTemplateError` naming the missing node.

## Status

**DONE — initial build.** Manifest layer + orchestrator + CLI + ComfyUI
wrapper + two workflow templates + models.json all shipped. Dry-run
smoke verified on laptop; live run (real ComfyUI + real weights)
happens on the pod once `runpod_setup.sh` completes the first weight
pull.
