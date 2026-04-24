"""ComfyUI custom-node registration for Node 6 - Reference Sheet Matching.

This file is the ComfyUI-facing adapter. All business logic lives in
`pipeline/node6.py` — this module only:
  1. Declares the node's INPUT_TYPES + RETURN_TYPES so ComfyUI can wire
     it into a workflow graph.
  2. Calls `match_references_for_queue()` and hands its result back as
     a single JSON-serializable string for downstream nodes.

Kept intentionally thin so the same core code is exercised by:
  * `python run_node6.py ...`                 (CLI, tests, CI)
  * `python -m pipeline.cli_node6 ...`        (RunPod shell)
  * this custom node                          (ComfyUI graph)

If you add a ComfyUI-only knob, expose it here and pass it down to
`pipeline.node6` — do NOT fork the logic.
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

from pipeline.node6 import (  # noqa: E402 - path fixup must happen first
    DEFAULT_LINEART_METHOD,
    LINEART_METHODS,
    match_references_for_queue,
)


class AnimaticNode6ReferenceMatcher:
    """ComfyUI node: slice each character's 8-angle reference sheet by
    alpha-island bbox, score each detection from Node 5 against all 8
    angles with a classical multi-signal function, pick the winner, and
    emit color + line-art crops per (identity, angle)."""

    CATEGORY = "animatic-refinement"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)  # JSON string with the Node6Result payload
    RETURN_NAMES = ("node6_result_json",)
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "node5_result_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Absolute path to node5_result.json from "
                            "Node 5. Node 6 writes node6_result.json "
                            "alongside it."
                        ),
                    },
                ),
                "queue_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Absolute path to queue.json from Node 2 — "
                            "supplies each character's sheet PNG path."
                        ),
                    },
                ),
                "characters_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Absolute path to characters.json from "
                            "Node 1 — Node 6 checks that "
                            "conventions.angleOrderConfirmed is True."
                        ),
                    },
                ),
                "lineart_method": (
                    list(LINEART_METHODS),
                    {
                        "default": DEFAULT_LINEART_METHOD,
                        "tooltip": (
                            "Classical method for converting the "
                            "chosen color reference crop into a "
                            "black-line line-art PNG. 'dog' is the "
                            "v1 default."
                        ),
                    },
                ),
            }
        }

    def run(
        self,
        node5_result_path: str,
        queue_path: str,
        characters_path: str,
        lineart_method: str,
    ) -> tuple[str]:
        result = match_references_for_queue(
            node5_result_path=node5_result_path,
            queue_path=queue_path,
            characters_path=characters_path,
            lineart_method=str(lineart_method),
        )
        return (json.dumps(result.to_dict(), ensure_ascii=False),)


NODE_CLASS_MAPPINGS = {
    "AnimaticNode6ReferenceMatcher": AnimaticNode6ReferenceMatcher,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaticNode6ReferenceMatcher": "Animatic - Node 6: Reference Matcher",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
