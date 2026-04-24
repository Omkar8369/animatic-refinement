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

# Shared by both dwpose + lineart-fallback workflows.
NODE_KSAMPLER = "3"
NODE_LOAD_KEY_POSE = "11"         # LoadImage(rough key pose)
NODE_LOAD_REF_COLOR = "12"        # LoadImage(reference color crop)
NODE_POSITIVE_PROMPT = "6"
NODE_NEGATIVE_PROMPT = "7"
NODE_SAVE_IMAGE = "20"

# Locked sampler defaults (decision #7).
SAMPLER_NAME = "dpmpp_2m"
SCHEDULER = "karras"
STEPS = 25
CFG = 7.0
WIDTH = 512
HEIGHT = 512

# ControlNet strength defaults (decisions #2 + #3).
STRENGTH_DWPOSE = 0.75
STRENGTH_LINEART = 0.60
STRENGTH_SCRIBBLE = 0.60
STRENGTH_IP_ADAPTER = 0.80

# Locked prompt templates (decision #4).
POSITIVE_PROMPT_TEMPLATE = (
    "line art, black and white, clean outlines, "
    "{identity}, {angle_descriptor}"
)
NEGATIVE_PROMPT = (
    "color, shading, blur, messy, duplicate, extra limbs"
)


@dataclass(frozen=True)
class OrchestrateConfig:
    """Everything `refine_queue` needs that isn't a per-task field."""
    node6_result_path: Path
    queue_path: Path
    comfyui_url: str = DEFAULT_COMFYUI_URL
    dry_run: bool = False
    per_prompt_timeout_s: float = 600.0
    workflow_dir: Path = Path(__file__).resolve().parent


def refine_queue(config: OrchestrateConfig) -> Node7Result:
    """Drive the whole Node 7 pass. Returns the aggregate Node7Result
    with per-shot + aggregate manifests already written to disk.
    """
    node6 = load_node6_result(config.node6_result_path)
    queue = load_queue(config.queue_path)
    tasks = build_routing_table(node6, queue)

    # Pre-load workflow templates once (fail fast if they're missing).
    templates = _load_workflow_templates(config.workflow_dir)

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
    cn_strengths = _cn_strengths_for(task.poseExtractor)

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
        )

    try:
        graph = _parameterize_workflow(
            template=templates[task.poseExtractor],
            task=task,
        )
        submission = client.submit_prompt(graph)
        history = client.wait_for_completion(
            prompt_id=submission.promptId,
            total_timeout_seconds=config.per_prompt_timeout_s,
        )
        filename, subfolder = extract_first_image(
            history, NODE_SAVE_IMAGE
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
    )


# -------------------------------------------------------------------
# Workflow template handling
# -------------------------------------------------------------------

def _load_workflow_templates(
    workflow_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Load both workflow templates (dwpose + lineart-fallback)."""
    out: dict[str, dict[str, Any]] = {}
    for route, fname in (
        ("dwpose", "workflow.json"),
        ("lineart-fallback", "workflow_lineart_fallback.json"),
    ):
        path = workflow_dir / fname
        if not path.is_file():
            raise WorkflowTemplateError(
                f"Missing workflow template for route={route!r} at "
                f"{path}. Node 7 ships both JSONs in "
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
        out[route] = raw["prompt"]
    return out


def _parameterize_workflow(
    template: dict[str, Any],
    task: DetectionTask,
) -> dict[str, Any]:
    """Return a deep-copied, parameterized ComfyUI prompt graph."""
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


def _cn_strengths_for(pose_extractor: str) -> dict[str, float]:
    """Return the CN strengths recorded in each RefinedGeneration.

    The strengths themselves are baked into the workflow.json templates
    -- we record them in the manifest so the operator can cross-check
    without opening the graph.
    """
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
    "Node6ResultInputError",
    "Node7Result",
]
