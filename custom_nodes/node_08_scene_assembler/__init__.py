"""ComfyUI custom-node registration for Node 8 - Scene Assembly.

This file is the ComfyUI-facing adapter. All business logic lives in
`pipeline/node8.py` -- this module only:
  1. Declares the node's INPUT_TYPES + RETURN_TYPES so ComfyUI can wire
     it into a workflow graph.
  2. Calls `compose_for_queue()` and hands its result back as a single
     JSON-serializable string for downstream nodes.

Kept intentionally thin so the same core code is exercised by:
  * `python run_node8.py ...`                 (CLI, tests, CI)
  * `python -m pipeline.cli_node8 ...`        (RunPod shell)
  * this custom node                          (ComfyUI graph)

If you add a ComfyUI-only knob, expose it here and pass it down to
`pipeline.node8` -- do NOT fork the logic.
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

from pipeline.node8 import (  # noqa: E402 - path fixup must happen first
    DEFAULT_BACKGROUND,
    SUPPORTED_BACKGROUNDS,
    compose_for_queue,
)


class AnimaticNode8SceneAssembler:
    """ComfyUI node: composite Node 7's per-character refined PNGs into
    a single source-MP4-resolution frame per key pose, using each
    character's bbox (from Node 5, persisted through Node 7) as the
    placement anchor with feet-pinned scaling."""

    CATEGORY = "animatic-refinement"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)  # JSON string with the Node8Result payload
    RETURN_NAMES = ("node8_result_json",)
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "node7_result_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Absolute path to node7_result.json from "
                            "Node 7. Node 8 writes node8_result.json "
                            "alongside it in the same work dir."
                        ),
                    },
                ),
                "background": (
                    list(SUPPORTED_BACKGROUNDS),
                    {
                        "default": DEFAULT_BACKGROUND,
                        "tooltip": (
                            "Canvas background. v1 supports 'white' "
                            "only (matches the BnW deliverable)."
                        ),
                    },
                ),
            }
        }

    def run(
        self,
        node7_result_path: str,
        background: str,
    ) -> tuple[str]:
        result = compose_for_queue(
            node7_result_path=Path(node7_result_path),
            background=str(background),
        )
        return (json.dumps(result.to_dict(), ensure_ascii=False),)


NODE_CLASS_MAPPINGS = {
    "AnimaticNode8SceneAssembler": AnimaticNode8SceneAssembler,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaticNode8SceneAssembler": "Animatic - Node 8: Scene Assembler",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
