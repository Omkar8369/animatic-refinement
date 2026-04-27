"""ComfyUI custom-node registration for Node 7 - Pose Refinement.

Node 7 is the first node in the pipeline that breaks the
`pipeline/nodeN.py + thin wrapper` template on purpose (locked decision
#9): the authoritative artifact is `workflow.json` (a ComfyUI graph).

What lives here:
  * The ComfyUI-facing custom node `AnimaticNode7PoseRefiner`, which
    ComfyUI's loader discovers via `NODE_CLASS_MAPPINGS`. It's a thin
    adapter that delegates to
    `custom_nodes.node_07_pose_refiner.orchestrate.refine_queue`.
  * An equivalent CLI path exists at `pipeline.cli_node7.main` (invoked
    via `run_node7.py` or `python -m pipeline.cli_node7`).

Both paths drive the same orchestrator, so the same code is exercised
by ComfyUI graph invocation, CLI, tests, and CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the repo root importable so `from pipeline...` works whether
# ComfyUI loaded us via a symlink (RunPod) or a direct clone.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from .orchestrate import (  # noqa: E402 - path fixup must happen first
    DEFAULT_COMFYUI_URL,
    DEFAULT_PRECISION,
    DEFAULT_WORKFLOW,
    PRECISION_CHOICES,
    WORKFLOW_CHOICES,
    OrchestrateConfig,
    refine_queue,
)


class AnimaticNode7PoseRefiner:
    """ComfyUI node: iterate every detection in Node 6's output, submit
    a per-character pose-refinement workflow via ComfyUI's HTTP API,
    collect the refined PNGs, and emit refined_map.json per shot +
    node7_result.json aggregate.

    NOTE: when this node runs INSIDE ComfyUI it is recursive by design --
    it uses ComfyUI's HTTP API from inside a ComfyUI workflow. The
    simpler invocation path is the CLI (`python run_node7.py ...` on the
    pod), which avoids the self-call. This custom node exists so a
    graph author can wire Node 7 as one step in a larger
    Node-3..Node-10 ComfyUI pipeline.
    """

    CATEGORY = "animatic-refinement"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)  # JSON string with the Node7Result payload
    RETURN_NAMES = ("node7_result_json",)
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "node6_result_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Absolute path to node6_result.json from "
                            "Node 6. Node 7 writes node7_result.json "
                            "alongside it."
                        ),
                    },
                ),
                "queue_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Absolute path to queue.json from Node 2 - "
                            "supplies each character's poseExtractor "
                            "route (dwpose vs. lineart-fallback)."
                        ),
                    },
                ),
                "comfyui_url": (
                    "STRING",
                    {
                        "default": DEFAULT_COMFYUI_URL,
                        "tooltip": (
                            "ComfyUI HTTP API root. Default is the "
                            "loopback address on the pod where this "
                            "graph is running."
                        ),
                    },
                ),
                "dry_run": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": (
                            "When True, skip ComfyUI submission and "
                            "write status='skipped' generations only. "
                            "Useful for laptop smoke tests of the "
                            "manifest layer."
                        ),
                    },
                ),
                "workflow": (
                    list(WORKFLOW_CHOICES),
                    {
                        "default": DEFAULT_WORKFLOW,
                        "tooltip": (
                            "Workflow stack: v1 (Phase 1 SD 1.5 + "
                            "AnyLoRA + DWPose / lineart-fallback) or "
                            "v2 (Phase 2 Flux Dev + Flat Cartoon Style "
                            "LoRA + ControlNet Union Pro). Phase 2a "
                            "ships v1 as default for safety."
                        ),
                    },
                ),
                "precision": (
                    list(PRECISION_CHOICES),
                    {
                        "default": DEFAULT_PRECISION,
                        "tooltip": (
                            "Flux model precision (workflow=v2 only). "
                            "fp16 = full Flux Dev, A100 80GB. fp8 = "
                            "quantized Flux Dev, 4090 24GB fallback."
                        ),
                    },
                ),
            }
        }

    def run(
        self,
        node6_result_path: str,
        queue_path: str,
        comfyui_url: str,
        dry_run: bool,
        workflow: str = DEFAULT_WORKFLOW,
        precision: str = DEFAULT_PRECISION,
    ) -> tuple[str]:
        config = OrchestrateConfig(
            node6_result_path=Path(node6_result_path),
            queue_path=Path(queue_path),
            comfyui_url=str(comfyui_url) or DEFAULT_COMFYUI_URL,
            dry_run=bool(dry_run),
            workflow=str(workflow) or DEFAULT_WORKFLOW,
            precision=str(precision) or DEFAULT_PRECISION,
        )
        result = refine_queue(config)
        return (json.dumps(result.to_dict(), ensure_ascii=False),)


NODE_CLASS_MAPPINGS = {
    "AnimaticNode7PoseRefiner": AnimaticNode7PoseRefiner,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaticNode7PoseRefiner": "Animatic - Node 7: Pose Refiner",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
