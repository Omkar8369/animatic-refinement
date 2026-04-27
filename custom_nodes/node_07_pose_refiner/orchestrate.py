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
# #4 (Phase 2). Three new nodes; nodes 22 + 24 use the upstream typo
# 'IPAdatpter' verbatim because that's the registered class_type and
# input field name in XLabs-AI/x-flux-comfyui. Don't "fix" the typo here.
NODE_FLUX_IPADAPTER_LOADER = "22"     # 'Load Flux IPAdatpter' (sic)
NODE_FLUX_IPADAPTER_REF_IMAGE = "23"  # LoadImage (reference COLOR crop from Node 6E)
NODE_FLUX_IPADAPTER_APPLY = "24"      # 'Apply Flux IPAdapter'
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
# EmptySD3LatentImage to VAEEncode of the rough crop.
V2_DENOISE = 0.55

# v2 ControlNet strength defaults (decision #6).
V2_STRENGTH_CONTROLNET = 0.65
V2_STRENGTH_IP_ADAPTER = 0.80

# v2 LoRA strength defaults (decisions #2 + #9).
V2_STYLE_LORA_STRENGTH = 0.75
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
V2_POSITIVE_PROMPT_TEMPLATE = (
    "flat cartoon style, TMKOC, Indian children's animation, "
    "{identity} character, {angle_descriptor} view, "
    "simple flat solid colors, clean bold line art, "
    "bright daytime colors, Toon Boom animated television show style"
)
V2_NEGATIVE_PROMPT = (
    "anime, manga, realistic, photo, 3d render, cgi, dark, gritty, "
    "blurry, ugly, deformed, sketchy, pencil sketch, monochrome"
)

# Workflow names accepted on the CLI (--workflow flag).
WORKFLOW_CHOICES = ("v1", "v2")
DEFAULT_WORKFLOW = "v2"  # Phase 2c (2026-04-27) flipped the default
                         # from "v1" to "v2". Phase 1 (--workflow=v1)
                         # stays callable for the deprecation window
                         # (per locked decision #12, until 2026-10-26).

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

    def __post_init__(self) -> None:
        if self.workflow not in WORKFLOW_CHOICES:
            raise ValueError(
                f"workflow={self.workflow!r} not in {WORKFLOW_CHOICES}"
            )
        if self.precision not in PRECISION_CHOICES:
            raise ValueError(
                f"precision={self.precision!r} not in {PRECISION_CHOICES}"
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
        graph = _parameterize_workflow(
            template=template,
            task=task,
            config=config,
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
) -> dict[str, Any]:
    """Return a deep-copied, parameterized ComfyUI prompt graph.

    Dispatches to v1 or v2 parameterizer based on `config.workflow`.
    """
    if config.workflow == "v1":
        return _parameterize_workflow_v1(template, task)
    return _parameterize_workflow_v2(template, task, config.precision)


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
) -> dict[str, Any]:
    """Phase 2 (Flux Dev + Flat Cartoon LoRA + Union CN) parameterization.

    Per-detection swaps:
      - node 10 unet_name and node 11 clip_name1 chosen by `precision`
      - node 51 entire dict (DWPreprocessor vs LineArtPreprocessor) by route
      - node 61 type ("openpose" vs "lineart") by route
      - node 90 seed
      - node 50 image (rough key pose path)
      - node 30 / node 31 prompt text
      - node 110 filename_prefix

    Phase 2a is txt2img only (node 80 = EmptySD3LatentImage); Phase 2c
    will swap node 80 to VAEEncode of the rough crop with KSampler
    denoise=0.55. Phase 2b will wire the XLabs Flux IP-Adapter; Phase
    2e will populate node 21 (Character LoraLoader) per-detection from
    `characters.json.characterLoraFilename`. Both extensions slot in
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
        (NODE_FLUX_IPADAPTER_LOADER, "Load Flux IPAdatpter (sic)"),
        (NODE_FLUX_IPADAPTER_REF_IMAGE, "LoadImage (reference color)"),
        (NODE_FLUX_IPADAPTER_APPLY, "Apply Flux IPAdapter"),
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

    # Rough-crop input. Phase 2a uses this for the ControlNet
    # preprocessor only (txt2img mode). Phase 2c will additionally feed
    # node 80 (VAEEncode) for img2img.
    graph[NODE_FLUX_LOAD_ROUGH]["inputs"]["image"] = str(task.keyPosePath)

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

    # Style LoRA strength stays locked at v2 default; lora_name
    # parameterized in Phase 2d when the custom-trained TMKOC LoRA
    # ships and replaces flat_cartoon_style_v12.
    graph[NODE_FLUX_STYLE_LORA]["inputs"]["strength_model"] = (
        V2_STYLE_LORA_STRENGTH
    )
    graph[NODE_FLUX_STYLE_LORA]["inputs"]["strength_clip"] = (
        V2_STYLE_LORA_STRENGTH
    )

    # Output filename prefix (mirrors v1's pattern).
    output_prefix = (
        f"animatic/{task.shotId}/"
        f"{task.keyPoseIndex:03d}_{task.identity}"
    )
    graph[NODE_FLUX_SAVE_IMAGE]["inputs"]["filename_prefix"] = (
        output_prefix
    )

    return graph


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
    "WORKFLOW_CHOICES",
    "PRECISION_CHOICES",
    "Node6ResultInputError",
    "Node7Result",
]
