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

**Phase 1 DONE — both routes live-verified on RunPod (2026-04-25).**
Manifest layer + orchestrator + CLI + ComfyUI wrapper + two workflow
templates + models.json all shipped. Dry-run smoke verified on laptop;
live run (real ComfyUI + real weights) happens on the pod once
`runpod_setup.sh` completes the first weight pull.

**Phase 2a (Flux migration) — SHIPPED 2026-04-26.** Real-shot TMKOC
test (EP35 SH004, 88 frames) had run end-to-end in 33.8s on the 4090
but produced anime-girl output because SD 1.5 + AnyLoRA is anime-
trained; a bare-bones hug-test workflow (Flux Dev fp8 + Flat Cartoon
Style v1.2 LoRA + ControlNet Union Pro + DWPose) on the same pod
generated dramatically better output (~70s on the 4090) — recognizable
Tappu + Champak Lal in TMKOC style. Phase 2a's `workflow_flux_v2.json`
(this folder) is the productionised + parameterized version of that
hug-test workflow. 14 architectural decisions locked (see `CLAUDE.md`
"Node 7 v2 — locked decisions" for full rationale; see
`docs/PLAN.md` "NODE 7 v2 — Phase 2 Flux migration" for the
implementation roadmap):

1. Base model: **Flux Kontext Dev fp16** (img2img-specialized variant)
2. Style LoRA: **Flat Cartoon Style v1.2** (Civitai 644541) at 0.75; replaced by custom-trained TMKOC v1 in Phase 2d
3. Pose ControlNet: **ControlNet Union Pro** (single CN, routes via `SetUnionControlNetType`) at 0.65
4. Identity injection: **XLabs Flux IP-Adapter v2** (requires `x-flux-comfyui` custom node) at 0.8
5. Generation mode: **img2img with denoise=0.55** (reverses Phase 1's txt2img — Flux Kontext Dev's superior denoising resolves the rough-pixel-bleed concern)
6. Conditioning scales: CN 0.65 + IP-Adapter 0.8
7. Resolution: **1280×720 native** (matches source MP4; no per-shot resize)
8. Sampler: **dpmpp_2m_sde** + scheduler `simple` + 40 steps + FluxGuidance 4.0 + CFG 1.0
9. Per-character LoRAs (Phase 2e): plan architecture, defer training; bootstrap data via Phase 2b IP-Adapter; ai-toolkit (ostris) on A100 80GB; ~$5-15 + 1.5h per character
10. Backward compatibility: schemaVersion stays at 1; all changes additive (new fields get defaults)
11. Hardware + precision: **A100 80GB + Flux Dev fp16** default; fp8 fallback via `--precision fp8`
12. Phase 1 weights archived (`deprecated: true`, scheduled removal 2026-10-26 = 6-month rollback window)
13. Architecture template **unchanged** from Phase 1 (workflow JSON + thin custom-node wrapper; no `pipeline/node7.py`)
14. Failure mode unchanged: log + continue, no QC gate in v1; future Node 11C retry hook is the right home for operator-level retries

**Implementation roadmap (each phase = its own ship-checklist commit):**

| Phase | Status | Title | Ships |
|-------|--------|-------|-------|
| **2a** | **DONE 2026-04-26** | Flux + Style LoRA + Union CN integration | Shipped: `workflow_flux_v2.json` (16 locked node IDs); `models.json` schema bump (additive deprecation fields); Phase 1 weights flipped to `deprecated:true` (2026-10-26 removal); Phase 2 weights pinned (Flux Dev fp16/fp8 + T5-XXL fp16/fp8 + CLIP-L + Flux VAE + Flat Cartoon Style v1.2 + ControlNet Union Pro); `runpod_setup.sh` honours `DOWNLOAD_DEPRECATED` env var; `--workflow {v1,v2}` + `--precision {fp16,fp8}` flags on Node 7 + Node 11 CLIs (default `v1` for safety). 84 Node 7 tests pass (47 Phase 1 + 37 new), 429 repo-wide, zero regressions. |
| **2b** | **DONE 2026-04-27** | Add XLabs Flux IP-Adapter | Shipped: 3 additive nodes in `workflow_flux_v2.json` (22 `Load Flux IPAdatpter` [sic — upstream typo preserved], 23 LoadImage for reference COLOR crop, 24 `Apply Flux IPAdapter`); KSampler model input rewired from node 20 to node 24 (only existing-node change); `ip_scale=0.8` per locked decision #6; reference image is COLOR crop per locked decision #4. `models.json` gained `flux-ip-adapter-v2.safetensors` (~1 GB) + `clip-vit-large-patch14.safetensors` (~600 MB CLIP-L vision encoder) + `x-flux-comfyui` custom node clone. 91 Node 7 tests pass (47 Phase 1 + 44 Phase 2; 7 new for IP-Adapter wiring + class_type typo lock + KSampler rewire + reference path swap guard); 436 repo-wide, zero regressions. |
| **2c** | pending | Switch Node 7 default to v2 (img2img mode) | Flip `--workflow` default; switch workflow to img2img + `denoise=0.55`. |
| **2d** | pending | Train TMKOC style LoRA | Replace generic Flat Cartoon Style with custom TMKOC v1. |
| **2e** | pending | Train per-character LoRAs (one commit per character) | TAPPU first, then CHAMPAK_LAL, … `CharacterSpec.characterLoraFilename` + `characterLoraStrength` (additive schema; **fields ALREADY shipped in Phase 2a**, populated per-character in 2e). |
| **2f** | pending | Fix Node 5 background-line detection bug (upstream prerequisite) | Otsu fallback when bbox spans >70% of frame. |
| **2g** | pending | Simplify Node 6 (always pick "front" angle when IP-Adapter handles identity) | Make Node 6 angle picking optional. |

**Hug-test proof-of-concept**: workflow JSON at
`_pod_out/flux_tmkoc_test_workflow.json`, output PNG at
`_pod_out/flux_test/flux_test_tappu_hug_00001_.png`. Phase 2a will use
this workflow JSON as the implementation starting point for
`workflow_flux_v2.json`.

**Phase 2 workflow node IDs** (locked across all Phase 2 phases —
`workflow_flux_v2.json` pins them; re-exporting from ComfyUI's GUI
must preserve these OR update `orchestrate.py`'s `NODE_FLUX_*`
constants in the same commit; same contract as the Phase 1 IDs):

| Node ID | Role | Parameterized fields | Shipped in |
|---|---|---|---|
| `"10"` | UNETLoader (Flux base) | `unet_name` ← `--precision` flag | 2a |
| `"11"` | DualCLIPLoader (T5-XXL + CLIP-L) | `clip_name1` ← `--precision` flag | 2a |
| `"12"` | VAELoader (Flux VAE) | (static) | 2a |
| `"20"` | LoraLoader (Style LoRA) | `lora_name` ← Phase 2d swap | 2a |
| `"21"` | LoraLoader (Character LoRA) | `lora_name` ← per-character from `characters.json` | reserved for **2e** |
| `"22"` | `Load Flux IPAdatpter` (sic) | (static — `ipadatper` + `clip_vision` filenames pinned) | **2b** |
| `"23"` | LoadImage (reference COLOR crop) | `image` ← `task.referenceColorCropPath` | **2b** |
| `"24"` | `Apply Flux IPAdapter` | `ip_scale` (locked at 0.8); model from node 20 (or 21 in 2e); image from 23 | **2b** |
| `"30"` | CLIPTextEncode (positive) | `text` (per-detection prompt) | 2a |
| `"31"` | CLIPTextEncode (negative) | (static — locked v2 negative prompt) | 2a |
| `"40"` | FluxGuidance | `guidance` (locked at 4.0) | 2a |
| `"50"` | LoadImage (rough crop) | `image` ← `task.keyPosePath` | 2a |
| `"51"` | DWPreprocessor / LineArtPreprocessor | full dict swap per route | 2a |
| `"60"` | ControlNetLoader (Union Pro) | (static) | 2a |
| `"61"` | SetUnionControlNetType | `type` ← `"openpose"` or `"lineart"` | 2a |
| `"70"` | ControlNetApplyAdvanced | `strength` (locked at 0.65) | 2a |
| `"80"` | EmptySD3LatentImage / VAEEncode | switches between txt2img / img2img | 2a (txt2img); 2c will flip to img2img |
| `"90"` | KSampler | `seed` per-detection; model wired from `"24"` (Phase 2b); rest locked | 2a (model from 20); 2b rewires to 24 |
| `"100"` | VAEDecode | (static) | 2a |
| `"110"` | SaveImage | `filename_prefix` per-detection | 2a |

Two upstream typos in node 22 are PRESERVED VERBATIM because they're
the actual registered class_type / input field names in the
`x-flux-comfyui` repo: the class_type is `"Load Flux IPAdatpter"`
(extra `t`), and its IP-Adapter filename input is `"ipadatper"`. Do
NOT "fix" them — the node won't register if you do.
