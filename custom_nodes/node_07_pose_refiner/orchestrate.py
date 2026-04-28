"""Node 7 orchestrator: iterate detections, submit workflows, save outputs.

This module is the top-level "driver" that the CLI (`pipeline.cli_node7`)
and the ComfyUI custom node (`custom_nodes.node_07_pose_refiner.__init__`)
both call into. It:

  1. Reads `node6_result.json` + `queue.json` and builds a `DetectionTask`
     list via `manifest.build_routing_table`.
  2. For each task picks the right `workflow.json` template (dwpose vs.
     lineart-fallback), parameterizes it, POSTs it to ComfyUI, waits
     for completion, downloads the saved PNG, and records a
     `RefinedGeneration`.
  3. Writes per-shot `refined_map.json` + aggregate `node7_result.json`.

`dry_run=True` skips the actual ComfyUI submission and just records
`status="skipped"` generations -- useful for local laptop smoke tests
that exercise the manifest layer without needing the pod to be up.

Workflow-template parameterization is done by walking the loaded JSON
dict and setting known `(node_id, input_key)` pairs. The node IDs are
constants that match the workflow.json files we ship -- a stale /
hand-edited template will raise `WorkflowTemplateError` naming the
missing node so the operator can re-export the graph in ComfyUI's API
format.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.errors import (
    ComfyUIConnectionError,
    Node6ResultInputError,
    RefinementGenerationError,
    WorkflowTemplateError,
)

from .comfyui_client import (
    ComfyUIClient,
    DEFAULT_COMFYUI_URL,
    extract_first_image,
)
from .manifest import (
    DetectionTask,
    Node7Result,
    RefinedGeneration,
    ShotRefinedSummary,
    _safe_segment,
    build_routing_table,
    load_node6_result,
    load_queue,
    write_node7_result,
    write_refined_map,
)


# -------------------------------------------------------------------
# Workflow template contracts (node_id constants)
# -------------------------------------------------------------------
# These must match the node-id keys in the shipped workflow.json files.
# We hold them here (not in workflow.json itself) so the orchestrator
# fails loudly if a workflow was re-exported from the ComfyUI UI in a
# way that reshuffled IDs -- the operator then either re-pins to the
# canonical file in git or updates these constants in a follow-up PR.

# -------------------------------------------------------------------
# Phase 1 (v1) node IDs -- shared by dwpose + lineart-fallback templates.
# -------------------------------------------------------------------
NODE_KSAMPLER = "3"
NODE_LOAD_KEY_POSE = "11"         # LoadImage(rough key pose)
NODE_LOAD_REF_COLOR = "12"        # LoadImage(reference color crop)
NODE_POSITIVE_PROMPT = "6"
NODE_NEGATIVE_PROMPT = "7"
NODE_SAVE_IMAGE = "20"

# Locked v1 sampler defaults (decision #7).
SAMPLER_NAME = "dpmpp_2m"
SCHEDULER = "karras"
STEPS = 25
CFG = 7.0
WIDTH = 512
HEIGHT = 512

# v1 ControlNet strength defaults (decisions #2 + #3).
STRENGTH_DWPOSE = 0.75
STRENGTH_LINEART = 0.60
STRENGTH_SCRIBBLE = 0.60
STRENGTH_IP_ADAPTER = 0.80

# Locked v1 prompt templates (decision #4).
POSITIVE_PROMPT_TEMPLATE = (
    "line art, black and white, clean outlines, "
    "{identity}, {angle_descriptor}"
)
NEGATIVE_PROMPT = (
    "color, shading, blur, messy, duplicate, extra limbs"
)

# -------------------------------------------------------------------
# Phase 2 (v2) node IDs -- single workflow_flux_v2.json file.
# Locked across Phase 2a-2g per locked decision #13. Re-exporting the
# graph from ComfyUI's GUI must preserve these OR update the constants
# below in the same commit.
# -------------------------------------------------------------------
NODE_FLUX_UNET = "10"             # UNETLoader (Flux base; precision-dependent)
NODE_FLUX_CLIP = "11"             # DualCLIPLoader (T5-XXL + CLIP-L)
NODE_FLUX_VAE = "12"              # VAELoader
NODE_FLUX_STYLE_LORA = "20"       # LoraLoader (style)
NODE_FLUX_CHAR_LORA = "21"        # LoraLoader (character; Phase 2e — not used in 2a/2b)
# Phase 2b additions: XLabs Flux IP-Adapter v2 wiring. Locked decision
# #4 (Phase 2). Three new nodes. Phase 2d-fixup (2026-04-27, post-live-
# pod-debug): the class_type strings on nodes 22 + 24 are 'LoadFluxIPAdapter'
# and 'ApplyFluxIPAdapter' (no spaces, no typo) — the INTERNAL class
# names from x-flux-comfyui/nodes.py NODE_CLASS_MAPPINGS. The 'IPAdatpter'
# typo is upstream's NODE_DISPLAY_NAME_MAPPINGS artifact (the GUI menu
# label only); workflow JSON's class_type uses the INTERNAL name. The
# input FIELD name 'ipadatper' on LoadFluxIPAdapter IS typo'd verbatim
# (that's how XLabs registered the field in INPUT_TYPES) and stays as-is.
NODE_FLUX_IPADAPTER_LOADER = "22"     # LoadFluxIPAdapter
NODE_FLUX_IPADAPTER_REF_IMAGE = "23"  # LoadImage (reference COLOR crop from Node 6E)
NODE_FLUX_IPADAPTER_APPLY = "24"      # ApplyFluxIPAdapter
NODE_FLUX_POS_PROMPT = "30"       # CLIPTextEncode (positive)
NODE_FLUX_NEG_PROMPT = "31"       # CLIPTextEncode (negative)
NODE_FLUX_GUIDANCE = "40"         # FluxGuidance
NODE_FLUX_LOAD_ROUGH = "50"       # LoadImage (rough crop; img2img/CN source)
NODE_FLUX_POSE_PREPROC = "51"     # DWPreprocessor / LineArtPreprocessor
NODE_FLUX_CN_LOADER = "60"        # ControlNetLoader (Union Pro)
NODE_FLUX_CN_UNION_TYPE = "61"    # SetUnionControlNetType
NODE_FLUX_CN_APPLY = "70"         # ControlNetApplyAdvanced
NODE_FLUX_LATENT_INIT = "80"      # EmptySD3LatentImage (txt2img) / VAEEncode (Phase 2c img2img)
NODE_FLUX_KSAMPLER = "90"         # KSampler
NODE_FLUX_VAE_DECODE = "100"      # VAEDecode
NODE_FLUX_SAVE_IMAGE = "110"      # SaveImage

# Locked v2 generation defaults (decision #8).
V2_SAMPLER_NAME = "dpmpp_2m_sde"
V2_SCHEDULER = "simple"
V2_STEPS = 40
V2_CFG = 1.0  # Flux requires cfg=1.0; FluxGuidance does the work.
V2_FLUX_GUIDANCE = 4.0
V2_WIDTH = 1280
V2_HEIGHT = 720
# Phase 2c: img2img denoise (locked decision #5). At 0.55 the rough's
# scribbles vanish but the underlying composition stays — Flux Kontext
# Dev's superior denoising resolves Phase 1's "rough pixels would
# bleed into output" concern. denoise=1.0 was Phase 2a's txt2img mode
# value; Phase 2c flipped to 0.55 along with the node 80 swap from
# EmptySD3LatentImage to VAEEncode of the rough crop. Phase 2-revision
# (2026-04-28) keeps denoise=0.55 but switches the VAEEncode source
# from the whole-frame keypose to a per-character bbox crop (see
# V2_BBOX_* constants below + _prepare_rough_bbox_crop).
V2_DENOISE = 0.55

# Phase 2-revision (2026-04-28): per-character bbox crop parameters
# for img2img. When v2 runs, `_run_one_task` pre-crops the keypose to
# `(bbox + margin)` and resizes to a Flux-compatible canvas before
# submitting; the bbox crop becomes the input to node 50 (LoadImage),
# which feeds both the pose preprocessor (node 51) and VAEEncode
# (node 80). This isolates each character from BG furniture and other
# characters in the same keypose, aligning with Phase 1 locked
# decision #5 (per-character generation, NOT whole-frame inpaint).
# Phase 2c briefly fed node 50 the whole 1280×720 rough; that was a
# regression and is undone by Phase 2-revision.
V2_BBOX_MARGIN_RATIO = 0.20    # 20% headroom around the bbox before crop
V2_BBOX_TARGET_MAX_EDGE = 768  # Resize so longest edge = 768 (Flux-friendly)
V2_BBOX_FLUX_MULTIPLE = 16     # Flux requires both dims % 16 == 0

# v2 ControlNet strength defaults (decision #6).
V2_STRENGTH_CONTROLNET = 0.65
V2_STRENGTH_IP_ADAPTER = 0.80

# v2 LoRA strength defaults (decisions #2 + #9). Phase 2-revision
# (2026-04-28) introduced per-LoRA strength override (see
# STYLE_LORA_STRENGTHS below) because the generic Flat Cartoon Style
# v1.2 LoRA biases toward color, conflicting with Part 1's BnW line-
# art deliverable. The locked-decision-#2 production strength of 0.75
# survives intact for the LoRA we actually want to use (the custom
# TMKOC v1 LoRA shipping in Phase 2d-run); the placeholder Flat
# Cartoon LoRA is bypassed via per-LoRA strength = 0.0. The
# V2_STYLE_LORA_STRENGTH constant below equals tmkoc_v1's strength
# (the locked production value); the actual per-detection strength is
# read from STYLE_LORA_STRENGTHS at parameterize time.
V2_CHAR_LORA_STRENGTH = 0.85

# v2 precision -> Flux weight filename map (decision #11).
# These match the destination files declared in models.json.
FLUX_UNET_BY_PRECISION = {
    "fp16": "flux1-dev-fp16.safetensors",
    "fp8": "flux1-dev-fp8.safetensors",
}
FLUX_T5XXL_BY_PRECISION = {
    "fp16": "t5xxl_fp16.safetensors",
    "fp8": "t5xxl_fp8_e4m3fn.safetensors",
}

# v2 pose-extractor route -> SetUnionControlNetType type + preprocessor
# class. DWPreprocessor's input dict has more fields than
# LineArtPreprocessor; the orchestrator REPLACES the entire node 51
# definition (class_type + inputs) per route, not just one field.
V2_PREPROCESSOR_BY_ROUTE: dict[str, dict[str, Any]] = {
    "dwpose": {
        "class_type": "DWPreprocessor",
        "inputs": {
            "image": [NODE_FLUX_LOAD_ROUGH, 0],
            "detect_hand": "enable",
            "detect_body": "enable",
            "detect_face": "enable",
            "resolution": 1024,
            "bbox_detector": "yolox_l.onnx",
            "pose_estimator": "dw-ll_ucoco_384.onnx",
        },
    },
    "lineart-fallback": {
        "class_type": "LineArtPreprocessor",
        "inputs": {
            "image": [NODE_FLUX_LOAD_ROUGH, 0],
            "coarse": "disable",
            "resolution": 1024,
        },
    },
}
V2_UNION_TYPE_BY_ROUTE = {
    "dwpose": "openpose",
    "lineart-fallback": "lineart",
}

# Locked v2 prompt templates -- richer than v1 to take advantage of
# Flux's stronger prompt adherence (decision #8 FluxGuidance 4.0).
# `{identity}` is the character name (TAPPU, BHIM, ...), `{angle_descriptor}`
# is the human-readable angle from Node 6 (e.g. "front 3q L"). Operators
# can override per project by re-pointing `--workflow=v1` while we
# experiment, but the v2 default is locked to this template.
#
# Phase 2-revision (2026-04-28): replaced colored-output prompts with
# BnW line-art prompts. Phase 2c's earlier prompts asked for "simple
# flat solid colors" + "bright daytime colors" and rejected
# "monochrome" — that biased v2 toward color, conflicting with Part
# 1's locked spec ("Output color is Black & White line art"). The
# storyboard cuts the operator works from are clean digital lines on
# white with no fill; Phase 2's deliverable is the same. The TMKOC v1
# LoRA trained in Phase 2d-run will reinforce the line aesthetic.
V2_POSITIVE_PROMPT_TEMPLATE = (
    "clean black ink line art, bold uniform line weight, "
    "{identity} character, {angle_descriptor} view, "
    "white background, no fill, no color, no shading, "
    "TMKOC animation style outline drawing"
)
V2_NEGATIVE_PROMPT = (
    "color, colored, fill, shading, gradient, gray fill, "
    "background, scene, furniture, sketchy, pencil, "
    "rough lines, double lines, anime, manga, photo, 3d"
)

# Workflow names accepted on the CLI (--workflow flag).
WORKFLOW_CHOICES = ("v1", "v2")
DEFAULT_WORKFLOW = "v2"  # Phase 2c (2026-04-27) flipped the default
                         # from "v1" to "v2". Phase 1 (--workflow=v1)
                         # stays callable for the deprecation window
                         # (per locked decision #12, until 2026-10-26).

# Phase 2d (2026-04-27): style LoRA choice on the v2 workflow.
# Locked decision #2 says Phase 2d will swap the generic Flat Cartoon
# Style v1.2 LoRA for a custom-trained TMKOC v1 LoRA. This commit
# (Phase 2d-prep) ships the infrastructure to swap them — the actual
# tmkoc_style_v1.safetensors weight is trained in a separate live
# session per the runbook at tools/phase2d/PHASE2D_TRAINING_PLAYBOOK.md.
# Until that LoRA exists, the default stays at flat_cartoon_v12 so
# everything keeps working; --style-lora=tmkoc_v1 raises a readable
# error if the weight isn't downloaded yet.
STYLE_LORA_FILENAMES = {
    "flat_cartoon_v12": "flat_cartoon_style_v12.safetensors",
    "tmkoc_v1":         "tmkoc_style_v1.safetensors",
}
STYLE_LORA_CHOICES = tuple(STYLE_LORA_FILENAMES.keys())
DEFAULT_STYLE_LORA = "flat_cartoon_v12"  # Stays at generic Flat Cartoon
                                          # until Phase 2d's actual TMKOC
                                          # training run lands a real
                                          # tmkoc_style_v1.safetensors.

# Phase 2-revision (2026-04-28): per-LoRA strength override. Locked
# decision #2 said "style LoRA at 0.75"; that holds for the LoRA we
# actually want to use (TMKOC v1, Phase 2d-run). The generic Flat
# Cartoon Style v1.2 LoRA biases toward color, which conflicts with
# Part 1's BnW line-art deliverable, so its effective strength is 0.0
# (LoRA still loads — LoraLoader can't be skipped without rewiring
# nodes 30 + 24 — but contributes nothing). When Phase 2d-run ships
# the custom-trained TMKOC line-art LoRA, the table below already has
# strength 0.75 ready, so flipping --style-lora=tmkoc_v1 picks up the
# locked-decision production value automatically.
STYLE_LORA_STRENGTHS = {
    "flat_cartoon_v12": 0.0,    # Phase 2-revision bypass (color-biased)
    "tmkoc_v1":         0.75,   # Phase 2d-run target (locked decision #2)
}

# Backwards-compat: equals tmkoc_v1's strength (the locked-decision
# production value). Tests + external callers that imported
# V2_STYLE_LORA_STRENGTH still see the locked 0.75; the actual per-
# detection strength applied to the workflow is read from
# STYLE_LORA_STRENGTHS at parameterize time.
V2_STYLE_LORA_STRENGTH = STYLE_LORA_STRENGTHS["tmkoc_v1"]

# Precision values accepted on the CLI (--precision flag).
PRECISION_CHOICES = ("fp16", "fp8")
DEFAULT_PRECISION = "fp16"  # Locked decision #11.


@dataclass(frozen=True)
class OrchestrateConfig:
    """Everything `refine_queue` needs that isn't a per-task field."""
    node6_result_path: Path
    queue_path: Path
    comfyui_url: str = DEFAULT_COMFYUI_URL
    dry_run: bool = False
    per_prompt_timeout_s: float = 600.0
    workflow_dir: Path = Path(__file__).resolve().parent
    # Phase 2 additions (locked decisions #11 + #13). `workflow` selects
    # between the Phase 1 dwpose / lineart-fallback templates and the
    # Phase 2 unified workflow_flux_v2.json. `precision` selects between
    # Flux Dev fp16 (default) and fp8 fallback for 4090-class GPUs;
    # ignored when `workflow == "v1"`.
    workflow: str = DEFAULT_WORKFLOW
    precision: str = DEFAULT_PRECISION
    # Phase 2d-prep (2026-04-27): style LoRA choice for the v2 workflow.
    # Default stays at the generic Flat Cartoon Style v1.2 until the
    # custom-trained TMKOC v1 LoRA ships. Ignored when `workflow == "v1"`.
    style_lora: str = DEFAULT_STYLE_LORA

    def __post_init__(self) -> None:
        if self.workflow not in WORKFLOW_CHOICES:
            raise ValueError(
                f"workflow={self.workflow!r} not in {WORKFLOW_CHOICES}"
            )
        if self.precision not in PRECISION_CHOICES:
            raise ValueError(
                f"precision={self.precision!r} not in {PRECISION_CHOICES}"
            )
        if self.style_lora not in STYLE_LORA_CHOICES:
            raise ValueError(
                f"style_lora={self.style_lora!r} not in {STYLE_LORA_CHOICES}"
            )


def refine_queue(config: OrchestrateConfig) -> Node7Result:
    """Drive the whole Node 7 pass. Returns the aggregate Node7Result
    with per-shot + aggregate manifests already written to disk.
    """
    node6 = load_node6_result(config.node6_result_path)
    queue = load_queue(config.queue_path)
    tasks = build_routing_table(node6, queue)

    # Pre-load workflow templates once (fail fast if they're missing).
    # `workflow` selects which template files we need (v1 = two files,
    # one per route; v2 = one file shared across routes).
    templates = _load_workflow_templates(config.workflow, config.workflow_dir)

    client = None if config.dry_run else ComfyUIClient(
        base_url=config.comfyui_url
    )

    # Group tasks by shot so we can write one refined_map.json per shot.
    tasks_by_shot: dict[str, list[DetectionTask]] = {}
    shot_order: list[str] = []
    for t in tasks:
        if t.shotId not in tasks_by_shot:
            tasks_by_shot[t.shotId] = []
            shot_order.append(t.shotId)
        tasks_by_shot[t.shotId].append(t)

    shot_summaries: list[ShotRefinedSummary] = []

    for shot_id in shot_order:
        shot_tasks = tasks_by_shot[shot_id]
        shot_root = shot_tasks[0].refinedPath.parent.parent  # <work>/<shotId>/
        refined_dir = shot_root / "refined"
        if not config.dry_run:
            refined_dir.mkdir(parents=True, exist_ok=True)

        generations = [
            _run_one_task(t, templates, client, config)
            for t in shot_tasks
        ]

        map_path = write_refined_map(
            shot_id=shot_id,
            shot_root=shot_root,
            generations=generations,
        )

        kp_indexes = {g.keyPoseIndex for g in generations}
        shot_summaries.append(
            ShotRefinedSummary(
                shotId=shot_id,
                keyPoseCount=len(kp_indexes),
                generatedCount=sum(
                    1 for g in generations if g.status == "ok"
                ),
                skippedCount=sum(
                    1 for g in generations if g.status == "skipped"
                ),
                errorCount=sum(
                    1 for g in generations if g.status == "error"
                ),
                refinedMapPath=str(map_path),
            )
        )

    return write_node7_result(
        node6_result=node6,
        shot_summaries=shot_summaries,
        comfyui_url=config.comfyui_url,
        dry_run=config.dry_run,
    )


# -------------------------------------------------------------------
# Per-task execution
# -------------------------------------------------------------------

def _run_one_task(
    task: DetectionTask,
    templates: dict[str, dict[str, Any]],
    client: ComfyUIClient | None,
    config: OrchestrateConfig,
) -> RefinedGeneration:
    """Refine one detection. In dry-run mode, record a 'skipped' entry;
    in live mode, submit to ComfyUI and download the output.
    """
    cn_strengths = _cn_strengths_for(task.poseExtractor, config.workflow)
    # v1's "precision" field on the manifest is locked to "fp8" per
    # decision #14 (Phase 1 records get the canonical Phase 1 precision
    # label). v2 uses the actual --precision flag value.
    precision_for_record = (
        config.precision if config.workflow == "v2" else "fp8"
    )

    if config.dry_run or client is None:
        return RefinedGeneration(
            identity=task.identity,
            keyPoseIndex=task.keyPoseIndex,
            sourceFrame=task.sourceFrame,
            selectedAngle=task.selectedAngle,
            poseExtractor=task.poseExtractor,
            seed=task.seed,
            refinedPath=str(task.refinedPath),
            boundingBox=list(task.boundingBox),
            status="skipped",
            errorMessage="dry-run",
            cnStrengths=cn_strengths,
            workflowName=config.workflow,
            precision=precision_for_record,
            characterLoraFilename=None,  # Phase 2e populates this
        )

    try:
        # Pick the right template per workflow. v1 picks per-route
        # (dwpose / lineart-fallback); v2 always uses the unified Flux
        # template + parameterizes node 51 + node 61 per route.
        if config.workflow == "v1":
            template = templates[task.poseExtractor]
        else:
            template = templates["v2"]
        # Phase 2-revision (2026-04-28): for v2, pre-crop the keypose
        # to (bbox + margin) so node 50 receives a per-character image
        # instead of the whole-frame keypose Phase 2c briefly fed it.
        # Aligns with Phase 1 locked decision #5 (per-character
        # generation, NOT whole-frame inpaint). Phase 1 (v1) is
        # untouched — its workflow already operates per-character via
        # the bbox-aware code path in workflow.json /
        # workflow_lineart_fallback.json.
        rough_override: str | None = None
        if config.workflow == "v2":
            crop_filename = (
                f"_crop_{task.keyPoseIndex:03d}_"
                f"{_safe_segment(task.identity)}.png"
            )
            crop_path = task.refinedPath.parent / crop_filename
            try:
                _prepare_rough_bbox_crop(
                    keypose_path=task.keyPosePath,
                    bbox=task.boundingBox,
                    output_path=crop_path,
                )
            except (RefinementGenerationError, OSError) as e:
                return RefinedGeneration(
                    identity=task.identity,
                    keyPoseIndex=task.keyPoseIndex,
                    sourceFrame=task.sourceFrame,
                    selectedAngle=task.selectedAngle,
                    poseExtractor=task.poseExtractor,
                    seed=task.seed,
                    refinedPath=str(task.refinedPath),
                    boundingBox=list(task.boundingBox),
                    status="error",
                    errorMessage=f"bbox crop: {type(e).__name__}: {e}",
                    cnStrengths=cn_strengths,
                    workflowName=config.workflow,
                    precision=precision_for_record,
                    characterLoraFilename=None,
                )
            rough_override = str(crop_path)
        graph = _parameterize_workflow(
            template=template,
            task=task,
            config=config,
            rough_image_override=rough_override,
        )
        save_node = (
            NODE_SAVE_IMAGE if config.workflow == "v1"
            else NODE_FLUX_SAVE_IMAGE
        )
        submission = client.submit_prompt(graph)
        history = client.wait_for_completion(
            prompt_id=submission.promptId,
            total_timeout_seconds=config.per_prompt_timeout_s,
        )
        filename, subfolder = extract_first_image(
            history, save_node
        )
        client.fetch_output_image(
            filename=filename,
            subfolder=subfolder,
            image_type="output",
            dest_path=task.refinedPath,
        )
    except (
        ComfyUIConnectionError,
        RefinementGenerationError,
        WorkflowTemplateError,
    ) as e:
        return RefinedGeneration(
            identity=task.identity,
            keyPoseIndex=task.keyPoseIndex,
            sourceFrame=task.sourceFrame,
            selectedAngle=task.selectedAngle,
            poseExtractor=task.poseExtractor,
            seed=task.seed,
            refinedPath=str(task.refinedPath),
            boundingBox=list(task.boundingBox),
            status="error",
            errorMessage=f"{type(e).__name__}: {e}",
            cnStrengths=cn_strengths,
            workflowName=config.workflow,
            precision=precision_for_record,
            characterLoraFilename=None,
        )

    return RefinedGeneration(
        identity=task.identity,
        keyPoseIndex=task.keyPoseIndex,
        sourceFrame=task.sourceFrame,
        selectedAngle=task.selectedAngle,
        poseExtractor=task.poseExtractor,
        seed=task.seed,
        refinedPath=str(task.refinedPath),
        boundingBox=list(task.boundingBox),
        status="ok",
        errorMessage="",
        cnStrengths=cn_strengths,
        workflowName=config.workflow,
        precision=precision_for_record,
        characterLoraFilename=None,
    )


# -------------------------------------------------------------------
# Workflow template handling
# -------------------------------------------------------------------

def _load_workflow_templates(
    workflow: str,
    workflow_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Load only the templates needed for the requested workflow.

    For workflow="v1" returns {"dwpose": <wf>, "lineart-fallback": <wf>}
    so the orchestrator can pick per-route. For workflow="v2" returns
    {"v2": <wf>} -- the single Flux template handles both routes via
    per-detection node-51 / node-61 swap in `_parameterize_workflow_v2`.
    """
    if workflow == "v1":
        spec = (
            ("dwpose", "workflow.json"),
            ("lineart-fallback", "workflow_lineart_fallback.json"),
        )
    elif workflow == "v2":
        spec = (("v2", "workflow_flux_v2.json"),)
    else:
        raise WorkflowTemplateError(
            f"Unknown workflow={workflow!r}; must be one of "
            f"{WORKFLOW_CHOICES}."
        )

    out: dict[str, dict[str, Any]] = {}
    for key, fname in spec:
        path = workflow_dir / fname
        if not path.is_file():
            raise WorkflowTemplateError(
                f"Missing workflow template for {key!r} at "
                f"{path}. Node 7 ships these JSONs in "
                "custom_nodes/node_07_pose_refiner/; if the file is "
                "absent the symlink on the pod is broken."
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise WorkflowTemplateError(
                f"{path} is not valid JSON: {e}"
            ) from e
        if not isinstance(raw, dict) or "prompt" not in raw:
            raise WorkflowTemplateError(
                f"{path} must be a JSON object with a top-level "
                "'prompt' key containing ComfyUI's API-format graph "
                "(node_id -> {class_type, inputs})."
            )
        if not isinstance(raw["prompt"], dict):
            raise WorkflowTemplateError(
                f"{path}: 'prompt' must be a dict of node_id -> object."
            )
        out[key] = raw["prompt"]
    return out


def _parameterize_workflow(
    template: dict[str, Any],
    task: DetectionTask,
    config: OrchestrateConfig,
    rough_image_override: str | None = None,
) -> dict[str, Any]:
    """Return a deep-copied, parameterized ComfyUI prompt graph.

    Dispatches to v1 or v2 parameterizer based on `config.workflow`.

    `rough_image_override` (Phase 2-revision, 2026-04-28) lets
    `_run_one_task` pass the path to a per-detection bbox crop so
    node 50 (LoadImage rough) receives a character-only image instead
    of the whole-frame keypose. When None (the test default), the
    parameterizer falls back to `task.keyPosePath`. Ignored by v1
    which has its own bbox handling baked into workflow.json /
    workflow_lineart_fallback.json.
    """
    if config.workflow == "v1":
        return _parameterize_workflow_v1(template, task)
    return _parameterize_workflow_v2(
        template, task, config.precision, config.style_lora,
        rough_image_override=rough_image_override,
    )


def _parameterize_workflow_v1(
    template: dict[str, Any],
    task: DetectionTask,
) -> dict[str, Any]:
    """Phase 1 (SD 1.5 + AnyLoRA + DWPose / lineart-fallback) parameterization."""
    graph = copy.deepcopy(template)

    _require_node(graph, NODE_KSAMPLER, "KSampler")
    _require_node(graph, NODE_LOAD_KEY_POSE, "LoadImage (key pose)")
    _require_node(graph, NODE_LOAD_REF_COLOR, "LoadImage (reference color)")
    _require_node(graph, NODE_POSITIVE_PROMPT, "CLIPTextEncode (positive)")
    _require_node(graph, NODE_NEGATIVE_PROMPT, "CLIPTextEncode (negative)")
    _require_node(graph, NODE_SAVE_IMAGE, "SaveImage")

    graph[NODE_KSAMPLER]["inputs"]["seed"] = task.seed
    graph[NODE_KSAMPLER]["inputs"]["steps"] = STEPS
    graph[NODE_KSAMPLER]["inputs"]["cfg"] = CFG
    graph[NODE_KSAMPLER]["inputs"]["sampler_name"] = SAMPLER_NAME
    graph[NODE_KSAMPLER]["inputs"]["scheduler"] = SCHEDULER

    graph[NODE_LOAD_KEY_POSE]["inputs"]["image"] = str(task.keyPosePath)
    graph[NODE_LOAD_REF_COLOR]["inputs"]["image"] = str(
        task.referenceColorCropPath
    )

    graph[NODE_POSITIVE_PROMPT]["inputs"]["text"] = (
        POSITIVE_PROMPT_TEMPLATE.format(
            identity=task.identity,
            angle_descriptor=task.selectedAngle.replace("-", " "),
        )
    )
    graph[NODE_NEGATIVE_PROMPT]["inputs"]["text"] = NEGATIVE_PROMPT

    output_prefix = (
        f"animatic/{task.shotId}/"
        f"{task.keyPoseIndex:03d}_{task.identity}"
    )
    graph[NODE_SAVE_IMAGE]["inputs"]["filename_prefix"] = output_prefix

    return graph


def _parameterize_workflow_v2(
    template: dict[str, Any],
    task: DetectionTask,
    precision: str,
    style_lora: str = DEFAULT_STYLE_LORA,
    rough_image_override: str | None = None,
) -> dict[str, Any]:
    """Phase 2 (Flux Dev + Style LoRA + Union CN + IP-Adapter + img2img) parameterization.

    Per-detection swaps:
      - node 10 unet_name and node 11 clip_name1 chosen by `precision`
      - node 20 lora_name chosen by `style_lora` (Phase 2d-prep)
      - node 20 strength_model + strength_clip from STYLE_LORA_STRENGTHS
        per-LoRA (Phase 2-revision: 0.0 for flat_cartoon_v12, 0.75 for
        tmkoc_v1)
      - node 51 entire dict (DWPreprocessor vs LineArtPreprocessor) by route
      - node 61 type ("openpose" vs "lineart") by route
      - node 90 seed
      - node 50 image (per-detection bbox crop when
        `rough_image_override` is set, falling back to
        task.keyPosePath when None — test paths only; production
        runs always pre-crop in `_run_one_task`)
      - node 30 / node 31 prompt text
      - node 110 filename_prefix

    Phase 2c made the workflow img2img (node 80 = VAEEncode of the rough
    crop, KSampler denoise=0.55). Phase 2-revision (2026-04-28) tightened
    that to a per-character bbox crop instead of the whole frame, so
    node 50 receives an isolated character with margin and the pose
    preprocessor + VAEEncode + KSampler all operate on character-only
    pixels. Phase 2b wired the XLabs Flux IP-Adapter; Phase 2e will
    populate node 21 (Character LoraLoader) per-detection from
    `characters.json.characterLoraFilename`. All extensions slot in
    without renaming the locked Phase 2 node IDs.
    """
    graph = copy.deepcopy(template)

    # Validate every Phase 2 v2 node ID up-front so a re-exported graph
    # that reshuffled IDs fails loudly with a readable error.
    for node_id, human_name in (
        (NODE_FLUX_UNET, "UNETLoader"),
        (NODE_FLUX_CLIP, "DualCLIPLoader"),
        (NODE_FLUX_VAE, "VAELoader"),
        (NODE_FLUX_STYLE_LORA, "LoraLoader (style)"),
        # Phase 2b additions: XLabs Flux IP-Adapter v2.
        (NODE_FLUX_IPADAPTER_LOADER, "LoadFluxIPAdapter"),
        (NODE_FLUX_IPADAPTER_REF_IMAGE, "LoadImage (reference color)"),
        (NODE_FLUX_IPADAPTER_APPLY, "ApplyFluxIPAdapter"),
        (NODE_FLUX_POS_PROMPT, "CLIPTextEncode (positive)"),
        (NODE_FLUX_NEG_PROMPT, "CLIPTextEncode (negative)"),
        (NODE_FLUX_GUIDANCE, "FluxGuidance"),
        (NODE_FLUX_LOAD_ROUGH, "LoadImage (rough)"),
        (NODE_FLUX_POSE_PREPROC, "Pose preprocessor"),
        (NODE_FLUX_CN_LOADER, "ControlNetLoader"),
        (NODE_FLUX_CN_UNION_TYPE, "SetUnionControlNetType"),
        (NODE_FLUX_CN_APPLY, "ControlNetApplyAdvanced"),
        (NODE_FLUX_LATENT_INIT, "EmptySD3LatentImage / VAEEncode"),
        (NODE_FLUX_KSAMPLER, "KSampler"),
        (NODE_FLUX_VAE_DECODE, "VAEDecode"),
        (NODE_FLUX_SAVE_IMAGE, "SaveImage"),
    ):
        _require_node(graph, node_id, human_name)

    # Precision-dependent Flux UNET + T5-XXL.
    graph[NODE_FLUX_UNET]["inputs"]["unet_name"] = (
        FLUX_UNET_BY_PRECISION[precision]
    )
    graph[NODE_FLUX_CLIP]["inputs"]["clip_name1"] = (
        FLUX_T5XXL_BY_PRECISION[precision]
    )

    # Route-dependent preprocessor + ControlNet Union type. Replace
    # node 51 in full because DWPreprocessor and LineArtPreprocessor
    # have different input dicts.
    if task.poseExtractor not in V2_PREPROCESSOR_BY_ROUTE:
        raise WorkflowTemplateError(
            f"v2 workflow does not know how to route poseExtractor="
            f"{task.poseExtractor!r}; expected one of "
            f"{tuple(V2_PREPROCESSOR_BY_ROUTE)}."
        )
    preproc = copy.deepcopy(V2_PREPROCESSOR_BY_ROUTE[task.poseExtractor])
    # Carry the existing _role annotation forward if present.
    if "_role" in graph[NODE_FLUX_POSE_PREPROC]:
        preproc["_role"] = graph[NODE_FLUX_POSE_PREPROC]["_role"]
    graph[NODE_FLUX_POSE_PREPROC] = preproc
    graph[NODE_FLUX_CN_UNION_TYPE]["inputs"]["type"] = (
        V2_UNION_TYPE_BY_ROUTE[task.poseExtractor]
    )

    # KSampler: per-detection seed; rest stays at locked v2 defaults
    # (already baked into workflow_flux_v2.json but re-asserted here so
    # the orchestrator's locked decisions win even if the JSON was
    # hand-edited mid-debug).
    graph[NODE_FLUX_KSAMPLER]["inputs"]["seed"] = task.seed
    graph[NODE_FLUX_KSAMPLER]["inputs"]["steps"] = V2_STEPS
    graph[NODE_FLUX_KSAMPLER]["inputs"]["cfg"] = V2_CFG
    graph[NODE_FLUX_KSAMPLER]["inputs"]["sampler_name"] = V2_SAMPLER_NAME
    graph[NODE_FLUX_KSAMPLER]["inputs"]["scheduler"] = V2_SCHEDULER
    # Phase 2c: img2img denoise locked at V2_DENOISE (0.55). Re-asserted
    # by the parameterizer so a hand-edited JSON can't silently revert
    # to txt2img's denoise=1.0 default.
    graph[NODE_FLUX_KSAMPLER]["inputs"]["denoise"] = V2_DENOISE

    # FluxGuidance and ControlNet strength stay locked at v2 defaults.
    graph[NODE_FLUX_GUIDANCE]["inputs"]["guidance"] = V2_FLUX_GUIDANCE
    graph[NODE_FLUX_CN_APPLY]["inputs"]["strength"] = (
        V2_STRENGTH_CONTROLNET
    )

    # Rough-crop input. Phase 2a was txt2img (rough fed only the CN
    # preprocessor); Phase 2c made it img2img (rough fed CN + VAEEncode
    # of the whole frame); Phase 2-revision (2026-04-28) keeps img2img
    # but switches the source to a per-character bbox crop produced by
    # `_run_one_task` and passed in via `rough_image_override`. When
    # called directly from a test without an override, falls back to
    # `task.keyPosePath` so existing parameterizer-only unit tests
    # continue to pass without needing real PNGs on disk.
    rough_path = (
        rough_image_override
        if rough_image_override is not None
        else str(task.keyPosePath)
    )
    graph[NODE_FLUX_LOAD_ROUGH]["inputs"]["image"] = rough_path

    # Phase 2b: reference COLOR crop into the IP-Adapter (locked
    # decision #4 — IP-Adapter expects a textured/colored image, not the
    # DoG line-art crop). ip_scale stays locked at the workflow JSON's
    # baked-in value (V2_STRENGTH_IP_ADAPTER = 0.8 per locked decision
    # #6). We re-assert it here so a hand-edited template can't silently
    # drift away from the locked default.
    graph[NODE_FLUX_IPADAPTER_REF_IMAGE]["inputs"]["image"] = str(
        task.referenceColorCropPath
    )
    graph[NODE_FLUX_IPADAPTER_APPLY]["inputs"]["ip_scale"] = (
        V2_STRENGTH_IP_ADAPTER
    )

    # Prompts.
    graph[NODE_FLUX_POS_PROMPT]["inputs"]["text"] = (
        V2_POSITIVE_PROMPT_TEMPLATE.format(
            identity=task.identity,
            angle_descriptor=task.selectedAngle.replace("-", " "),
        )
    )
    graph[NODE_FLUX_NEG_PROMPT]["inputs"]["text"] = V2_NEGATIVE_PROMPT

    # Style LoRA per-detection swap. Phase 2d-prep parameterized
    # `lora_name` from the --style-lora flag. Phase 2-revision
    # (2026-04-28) adds per-LoRA strength override via
    # STYLE_LORA_STRENGTHS: the generic Flat Cartoon LoRA gets
    # strength 0.0 (effectively bypassed because it biases toward
    # color, conflicting with Part 1's BnW deliverable); the custom
    # TMKOC v1 LoRA (Phase 2d-run) gets the locked-decision-#2 0.75.
    # Locked decision #2 ("style LoRA at 0.75") is unchanged for the
    # LoRA we actually want to use.
    style_strength = STYLE_LORA_STRENGTHS[style_lora]
    graph[NODE_FLUX_STYLE_LORA]["inputs"]["lora_name"] = (
        STYLE_LORA_FILENAMES[style_lora]
    )
    graph[NODE_FLUX_STYLE_LORA]["inputs"]["strength_model"] = style_strength
    graph[NODE_FLUX_STYLE_LORA]["inputs"]["strength_clip"] = style_strength

    # Output filename prefix (mirrors v1's pattern).
    output_prefix = (
        f"animatic/{task.shotId}/"
        f"{task.keyPoseIndex:03d}_{task.identity}"
    )
    graph[NODE_FLUX_SAVE_IMAGE]["inputs"]["filename_prefix"] = (
        output_prefix
    )

    return graph


def _resolve_dark_lines_source(keypose_path: Path) -> Path:
    """Phase 2f (2026-04-28): pick the right input image for the bbox
    crop step. Prefers ``<shot_root>/dark_lines/<filename>`` (BG-
    stripped, written by Node 5) over the raw keypose at
    ``<shot_root>/keyposes/<filename>``.

    The dark_lines/ version has BG furniture lines erased to white BG,
    which gives Flux a clean character-only input — no BG to fight at
    generation time, no prompt-vs-pixel battle. When dark_lines/ is
    missing (e.g., the work-dir was produced by an older Node 5 run
    that pre-dates Phase 2f), falls back to the raw keypose so Phase
    2f is backward-compatible with old work dirs.

    Args:
        keypose_path: ``<shot_root>/keyposes/<filename>``

    Returns:
        ``<shot_root>/dark_lines/<filename>`` if it exists;
        ``keypose_path`` otherwise.
    """
    # task.keyPosePath layout: <shot_root>/keyposes/<filename>
    # → dark_lines version: <shot_root>/dark_lines/<filename>
    dark_lines_path = (
        keypose_path.parent.parent / "dark_lines" / keypose_path.name
    )
    if dark_lines_path.is_file():
        return dark_lines_path
    return keypose_path


def _prepare_rough_bbox_crop(
    keypose_path: Path,
    bbox: tuple[int, int, int, int],
    output_path: Path,
    margin_ratio: float = V2_BBOX_MARGIN_RATIO,
    target_max_edge: int = V2_BBOX_TARGET_MAX_EDGE,
    flux_multiple: int = V2_BBOX_FLUX_MULTIPLE,
) -> Path:
    """Crop the keypose PNG to (bbox + margin) and resize to a Flux-
    compatible canvas. Save to ``output_path`` and return it.

    Phase 2-revision (2026-04-28): the bbox crop is the per-character
    image fed to node 50 (LoadImage rough) in workflow_flux_v2.json.
    Replaces Phase 2c's whole-frame img2img — that approach pulled BG
    furniture and other characters into Flux's view, which broke
    Phase 1 locked decision #5 (per-character generation, NOT
    whole-frame inpaint) and produced colored TMKOC scenes instead of
    BnW per-character keyposes the rest of the pipeline expects.

    Phase 2f (2026-04-28) added a second improvement: the source
    image is the BG-stripped ``<shot>/dark_lines/<filename>`` written
    by Node 5 (when present), not the raw keypose. This gives Flux
    character lines on clean white BG with no BG furniture to fight.
    Falls back to the raw keypose when dark_lines/ is missing
    (backward compat with pre-Phase-2f work dirs).

    The crop region is (bbox + margin_ratio * max(w, h)) clamped to
    image bounds. The result is resized so longest edge =
    `target_max_edge` and both dims are rounded down to multiples of
    `flux_multiple` (Flux requires both dims to be multiples of 16).

    Node 8's compositor places the refined output back at the bbox
    position via feet-pinned scaling — it doesn't care what canvas
    size Node 7 used, only that the refined PNG contains a single
    character with white margin around it.
    """
    # PIL is already a Node 4/5/6 dep; importing locally keeps
    # `orchestrate` importable on environments that don't have
    # Pillow yet (e.g., very-stripped CI). Module-level import would
    # force the dep on every consumer, including dry-run paths that
    # don't need it.
    from PIL import Image  # type: ignore[import-not-found]

    if not keypose_path.is_file():
        raise RefinementGenerationError(
            f"bbox crop source missing: keypose PNG not found at "
            f"{keypose_path}. Did Node 4 produce keyposes/?"
        )

    # Phase 2f: prefer the BG-stripped dark_lines/ version when Node 5
    # produced one. Falls back to the raw keypose for old work-dirs.
    source_path = _resolve_dark_lines_source(keypose_path)
    img = Image.open(source_path).convert("RGB")
    W, H = img.size
    x, y, w, h = bbox

    margin = int(round(margin_ratio * max(w, h)))
    left = max(0, x - margin)
    top = max(0, y - margin)
    right = min(W, x + w + margin)
    bottom = min(H, y + h + margin)

    if right <= left or bottom <= top:
        raise RefinementGenerationError(
            f"bbox crop region collapsed for keypose={keypose_path.name} "
            f"bbox=({x},{y},{w},{h}) image={W}x{H} margin={margin}: "
            f"crop region ({left},{top},{right},{bottom}) is empty. "
            "Check Node 5's character_map.json — bbox is likely "
            "off-canvas."
        )

    crop = img.crop((left, top, right, bottom))
    cw, ch = crop.size

    scale = target_max_edge / max(cw, ch)
    new_w = max(
        flux_multiple,
        (int(round(cw * scale)) // flux_multiple) * flux_multiple,
    )
    new_h = max(
        flux_multiple,
        (int(round(ch * scale)) // flux_multiple) * flux_multiple,
    )

    crop = crop.resize((new_w, new_h), Image.LANCZOS)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(output_path, format="PNG")
    return output_path


def _require_node(
    graph: dict[str, Any], node_id: str, human_name: str
) -> None:
    if node_id not in graph:
        raise WorkflowTemplateError(
            f"Workflow template is missing expected node "
            f"{human_name!r} at id {node_id!r}. Ensure the graph was "
            "exported in ComfyUI's API format and that node IDs match "
            "custom_nodes/node_07_pose_refiner/orchestrate.py's "
            "NODE_* constants."
        )


def _cn_strengths_for(
    pose_extractor: str,
    workflow: str = DEFAULT_WORKFLOW,
) -> dict[str, float]:
    """Return the CN strengths recorded in each RefinedGeneration.

    The strengths themselves are baked into the workflow.json templates
    -- we record them in the manifest so the operator can cross-check
    without opening the graph.

    For workflow=v1: split per-route (DWPose CN @ 0.75 OR LineArt+Scribble
    CN @ 0.6 each) plus IP-Adapter @ 0.8.

    For workflow=v2: single ControlNet Union Pro @ 0.65 + IP-Adapter
    @ 0.8 (Phase 2b will wire the IP-Adapter; until then strengths are
    recorded for forward-compat). Per-route routing is via
    SetUnionControlNetType, not separate ControlNet weights.
    """
    if workflow == "v2":
        return {
            "controlnetUnion": V2_STRENGTH_CONTROLNET,
            "ipAdapter": V2_STRENGTH_IP_ADAPTER,
        }
    if pose_extractor == "dwpose":
        return {
            "dwposeControlnet": STRENGTH_DWPOSE,
            "ipAdapter": STRENGTH_IP_ADAPTER,
        }
    # lineart-fallback
    return {
        "lineartControlnet": STRENGTH_LINEART,
        "scribbleControlnet": STRENGTH_SCRIBBLE,
        "ipAdapter": STRENGTH_IP_ADAPTER,
    }


# Re-export Node7Result/Node6ResultInputError for callers that only
# import `orchestrate` and never the manifest module directly.
__all__ = [
    "OrchestrateConfig",
    "refine_queue",
    "DEFAULT_COMFYUI_URL",
    "DEFAULT_WORKFLOW",
    "DEFAULT_PRECISION",
    "DEFAULT_STYLE_LORA",
    "WORKFLOW_CHOICES",
    "PRECISION_CHOICES",
    "STYLE_LORA_CHOICES",
    "STYLE_LORA_FILENAMES",
    "STYLE_LORA_STRENGTHS",   # Phase 2-revision
    "V2_BBOX_MARGIN_RATIO",   # Phase 2-revision
    "V2_BBOX_TARGET_MAX_EDGE",  # Phase 2-revision
    "V2_BBOX_FLUX_MULTIPLE",  # Phase 2-revision
    "Node6ResultInputError",
    "Node7Result",
]
