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
| **2b** | **DONE 2026-04-27** (corrected by 2d-fixup) | Add XLabs Flux IP-Adapter | Shipped: 3 additive nodes in `workflow_flux_v2.json` (22 `LoadFluxIPAdapter`, 23 LoadImage for reference COLOR crop, 24 `ApplyFluxIPAdapter`); KSampler model input rewired from node 20 to node 24 (only existing-node change); `ip_scale=0.8` per locked decision #6; reference image is COLOR crop per locked decision #4. `models.json` gained `flux-ip-adapter-v2.safetensors` (~1 GB at `models/xlabs/ipadapters/`) + `clip-vit-large-patch14.safetensors` (~600 MB CLIP-L vision encoder) + `x-flux-comfyui` custom node clone. 91 Node 7 tests pass (47 Phase 1 + 44 Phase 2; 7 new for IP-Adapter wiring + class_type lock + KSampler rewire + reference path swap guard); 436 repo-wide, zero regressions. **NOTE:** Phase 2b's original commit had `class_type` strings as `"Load Flux IPAdatpter"` / `"Apply Flux IPAdapter"` (the GUI display names from upstream NODE_DISPLAY_NAME_MAPPINGS) and `models.json` destination `models/ipadapter-flux/` — both wrong. Phase 2d-fixup (2026-04-27, post-live-pod-debug) corrected to the actual NODE_CLASS_MAPPINGS keys + the path x-flux-comfyui's `folder_paths` registration scans. |
| **2c** | **DONE 2026-04-27** (reverted by Phase 2-revision) | Switch Node 7 default to v2 (img2img mode) | Shipped: TWO architecturally significant flips. (1) `workflow_flux_v2.json` node 80 swapped from `EmptySD3LatentImage` (txt2img) to `VAEEncode` (img2img — wired to take pixels from node 50 rough crop + Flux VAE from node 12); KSampler `denoise` dropped from 1.0 to 0.55 per locked decision #5. (2) `DEFAULT_WORKFLOW` flipped from `"v1"` to `"v2"` in `orchestrate.py` — v2 is now the production default; Phase 1 still callable via `--workflow=v1`. `V2_DENOISE = 0.55` constant; parameterizer re-asserts it. CLI success line for v2 reports `precision=<value>`. 96 Node 7 tests pass (47 Phase 1 + 49 Phase 2; 5 new for img2img wiring + denoise lock + v2-as-default + v1 still-callable-via-flag); 441 repo-wide, zero regressions. **NOTE:** Phase 2c's "img2img on the rough" was implemented as VAEEncode of the WHOLE-FRAME keypose. That broke Phase 1 locked decision #5 (per-character generation) and produced colored TMKOC scenes instead of BnW per-character keyposes. Phase 2-revision (2026-04-28) corrected node 50 to receive a per-character bbox crop instead of the whole frame. |
| **2d-prep** | **DONE 2026-04-27** | Wire `--style-lora` flag + TMKOC v1 placeholder | Shipped: integration infrastructure for the TMKOC v1 style LoRA without yet training the actual safetensors weight (Phase 2d-run is a separate live-pod follow-up). New `--style-lora {flat_cartoon_v12,tmkoc_v1}` flag (default `flat_cartoon_v12`) on Node 7 + Node 11 CLIs + ComfyUI wrapper dropdown — parameterizes node 20's `lora_name`. `STYLE_LORA_FILENAMES` table + `STYLE_LORA_CHOICES` + `DEFAULT_STYLE_LORA` constants in `orchestrate.py`. `tmkoc-style-v1` placeholder entry in `models.json` (URL = TODO until Phase 2d-run). New `tools/phase2d/PHASE2D_TRAINING_PLAYBOOK.md` (Path A bootstrap → curate → caption → train → validate → ship runbook) + `tools/phase2d/ai_toolkit_config_template.yaml` (locked Flux LoRA training params: rank=16, LR=1e-4, 2000 steps). 104 Node 7 tests pass (47 Phase 1 + 57 Phase 2; 8 new for style-lora flag plumbing + parameterizer wiring + config validation); 449 repo-wide, zero regressions. |
| **Phase 2-revision** | **DONE 2026-04-28** | Per-character bbox crop + BnW line-art prompts + Flat Cartoon LoRA bypass | Shipped: corrected the Phase 2c full-frame img2img regression that broke Phase 1 locked decision #5 (per-character generation). Three architectural flips: (1) `_run_one_task` now pre-crops the keypose to (Node-5 bbox + 20% margin) and resizes to a Flux-compatible canvas (multiples of 16, longest edge ≤ 768); the crop becomes node 50's input so the pose preprocessor + VAEEncode + KSampler all operate on character-only pixels. (2) `V2_POSITIVE_PROMPT_TEMPLATE` + `V2_NEGATIVE_PROMPT` swapped from "flat cartoon style ... bright daytime colors" + reject "monochrome" → "clean black ink line art, white background, no fill, no color" + reject "color, fill, shading, scene, furniture". (3) New `STYLE_LORA_STRENGTHS` per-LoRA strength table: `flat_cartoon_v12 → 0.0` (LoRA loads but is bypassed because it biases toward color, conflicting with Part 1's BnW deliverable), `tmkoc_v1 → 0.75` (locked decision #2 production value, applied automatically once Phase 2d-run ships the custom-trained LINE-ART LoRA). Phase 2d training data path also flipped from Path A synthetic bootstrap → user's storyboard scene cuts directly (clean BnW lines on white, the same target aesthetic v2 produces). `workflow_flux_v2.json` node 20 strength updated to 0.0 in the JSON; new regression guards verify the shipped JSON values + the per-LoRA table + the BnW prompt strings + the bbox-crop helper. **110 Node 7 tests pass** (47 Phase 1 + 63 Phase 2; 6 new Phase 2-revision tests + 1 modified prompt-template test); **455 repo-wide, zero regressions**. |
| **2d-run** | pending | Train TMKOC line-art LoRA on live A100 | Follow `tools/phase2d/PHASE2D_TRAINING_PLAYBOOK.md` (Phase 2-revision direct-storyboard approach) — scp curated storyboard cuts → caption with "TMKOC line art" trigger → train via ai-toolkit (rank=16, LR=1e-4, ~2000 steps) → validate per-checkpoint → pick winner → ship. Estimated ~$3-5 GPU + ~4-8 hours human time across 1-2 iterations (faster than 2d-prep's synthetic Path A estimate because storyboard cuts are already curated). After training, fill in `models.json`'s `tmkoc-style-v1` URL + sha256; flipping `--style-lora=tmkoc_v1` then picks up STYLE_LORA_STRENGTHS["tmkoc_v1"] = 0.75 (locked production value) automatically. |
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
| `"20"` | LoraLoader (Style LoRA) | `lora_name` ← Phase 2d swap; `strength_model` + `strength_clip` ← `STYLE_LORA_STRENGTHS[style_lora]` per Phase 2-revision (flat_cartoon_v12 → 0.0 bypass, tmkoc_v1 → 0.75 production) | 2a; per-LoRA strength added in Phase 2-revision |
| `"21"` | LoraLoader (Character LoRA) | `lora_name` ← per-character from `characters.json` | reserved for **2e** |
| `"22"` | `LoadFluxIPAdapter` (input field `ipadatper` IS typo'd verbatim — that's how XLabs registered it) | (static — `ipadatper` + `clip_vision` filenames pinned) | **2b** |
| `"23"` | LoadImage (reference COLOR crop) | `image` ← `task.referenceColorCropPath` | **2b** |
| `"24"` | `ApplyFluxIPAdapter` | `ip_scale` (locked at 0.8); model from node 20 (or 21 in 2e); image from 23 | **2b** |
| `"30"` | CLIPTextEncode (positive) | `text` (per-detection prompt) | 2a |
| `"31"` | CLIPTextEncode (negative) | (static — locked v2 negative prompt) | 2a |
| `"40"` | FluxGuidance | `guidance` (locked at 4.0) | 2a |
| `"50"` | LoadImage (rough crop) | `image` ← per-character bbox crop (saved by `_prepare_rough_bbox_crop`); falls back to `task.keyPosePath` only when called from a parameterizer-only unit test (no `rough_image_override`) | 2a (full keypose); Phase 2-revision flipped to per-character bbox crop |
| `"51"` | DWPreprocessor / LineArtPreprocessor | full dict swap per route | 2a |
| `"60"` | ControlNetLoader (Union Pro) | (static) | 2a |
| `"61"` | SetUnionControlNetType | `type` ← `"openpose"` or `"lineart"` | 2a |
| `"70"` | ControlNetApplyAdvanced | `strength` (locked at 0.65) | 2a |
| `"80"` | VAEEncode (img2img since 2c) / EmptySD3LatentImage (txt2img — replaced) | (static — pixels from 50, vae from 12) | 2a (EmptySD3LatentImage); 2c flipped to VAEEncode |
| `"90"` | KSampler | `seed` per-detection; model wired from `"24"` (Phase 2b); `denoise=0.55` since 2c; rest locked | 2a (model from 20, denoise=1.0); 2b rewired to 24; 2c dropped denoise to 0.55 |
| `"100"` | VAEDecode | (static) | 2a |
| `"110"` | SaveImage | `filename_prefix` per-detection | 2a |

**The `IPAdatpter` typo and `Load Flux ...` GUI display name don't
appear in workflow JSON.** ComfyUI's wire format (the `class_type`
field in workflow JSON) uses `NODE_CLASS_MAPPINGS` keys, not
`NODE_DISPLAY_NAME_MAPPINGS` values. From `x-flux-comfyui/nodes.py`:

```python
NODE_CLASS_MAPPINGS = {              NODE_DISPLAY_NAME_MAPPINGS = {
    "LoadFluxIPAdapter": ...,            "LoadFluxIPAdapter": "Load Flux IPAdatpter",  # GUI label only
    "ApplyFluxIPAdapter": ...,           "ApplyFluxIPAdapter": "Apply Flux IPAdapter", # GUI label only
}                                     }
```

So:
- **`class_type` in workflow JSON** uses the LEFT column → `LoadFluxIPAdapter` / `ApplyFluxIPAdapter` (no spaces, no typo)
- **GUI menu label** uses the RIGHT column → typo'd `IPAdatpter` only there
- **Input field name** `ipadatper` (in `LoadFluxIPAdapter.INPUT_TYPES`) IS typo'd verbatim and stays

Phase 2b's original commit got this wrong — used the GUI display name
strings as `class_type`. Phase 2d-fixup (2026-04-27, post-live-pod
debug) corrected it after running into "node class not found" errors
on a real ComfyUI instance.

**The IP-Adapter weight destination** is `models/xlabs/ipadapters/`
(NOT `models/ipadapter-flux/`) — that's where x-flux-comfyui's
`folder_paths.folder_names_and_paths['xlabs_ipadapters']` registration
points (`os.path.join(folder_paths.models_dir, 'xlabs', 'ipadapters')`).
Same Phase 2d-fixup commit corrected this too.

## Phase 2-revision (2026-04-28) — what changed and why

Phase 2c (2026-04-27) flipped node 80 to `VAEEncode` and dropped
KSampler `denoise` to 0.55 — the right call architecturally
(img2img refines the rough's composition without throwing it away).
But the implementation fed VAEEncode the WHOLE-FRAME keypose at
node 50, which broke two contracts simultaneously:

1. **Phase 1 locked decision #5** ("Per-character generation, NOT
   whole-frame inpaint"). The whole-frame approach pulled BG
   furniture and other characters into Flux's view, exactly the
   problem locked decision #5 was meant to prevent.
2. **Part 1's locked deliverable** (BnW line art on white BG, no
   background scene). Phase 2c's prompts asked for "flat cartoon
   style ... bright daytime colors" — produced colored TMKOC scenes
   instead of BnW per-character keyposes.

Phase 2-revision corrects both:

- **`_run_one_task` pre-crops the keypose** to (Node-5 bbox + 20%
  margin), clamped to image bounds, resized so longest edge ≤ 768
  with both dims rounded down to multiples of 16 (Flux requirement).
  The crop becomes node 50's input — pose preprocessor + VAEEncode
  + KSampler all operate on character-only pixels.
- **Prompts ask for BnW line art on white** and reject color, fill,
  shading, scene, furniture. The negative prompt no longer rejects
  "monochrome" (Phase 2c's bug — that was rejecting exactly what
  Part 1 wants).
- **`STYLE_LORA_STRENGTHS` per-LoRA strength table** — `flat_cartoon_v12 → 0.0` (bypass; biases toward color), `tmkoc_v1 → 0.75` (locked decision #2 production value, applied automatically once Phase 2d-run ships the custom-trained LINE-ART LoRA). The locked decision survives intact for the LoRA we actually want; the placeholder is bypassed via per-LoRA strength rather than by removing the LoraLoader chain.
- **`workflow_flux_v2.json` node 20** updated to `strength_model: 0.0` + `strength_clip: 0.0` so a hand-launched workflow (without orchestrate.py re-asserting) also bypasses correctly. orchestrate.py re-asserts the per-LoRA strength at parameterize time.
- **Phase 2d training data** flipped from synthetic Path A (Phase 2c img2img bootstrap) → user's storyboard scene cuts directly. Storyboard cuts are clean digital BnW lines on white — already in the target aesthetic, no synthetic-curation step needed. Captioning emphasizes "TMKOC line art" trigger and explicitly drops color references.

Net effect: v2's deliverable now matches Phase 1's deliverable (per-character BnW line-art PNGs that Node 8 composites onto a white-BG frame at bbox positions), but with Flux's superior generation quality + character identity preservation via XLabs IP-Adapter.
