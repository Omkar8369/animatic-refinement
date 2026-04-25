"""ComfyUI custom-node registration for Node 9 - Timing Reconstruction.

This file is the ComfyUI-facing adapter. All business logic lives in
`pipeline/node9.py` -- this module only:
  1. Declares the node's INPUT_TYPES + RETURN_TYPES so ComfyUI can wire
     it into a workflow graph.
  2. Calls `reconstruct_timing_for_queue()` and hands its result back
     as a single JSON-serializable string for downstream nodes.

Kept intentionally thin so the same core code is exercised by:
  * `python run_node9.py ...`                 (CLI, tests, CI)
  * `python -m pipeline.cli_node9 ...`        (RunPod shell)
  * this custom node                          (ComfyUI graph)

If you add a ComfyUI-only knob, expose it here and pass it down to
`pipeline.node9` -- do NOT fork the logic.
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

from pipeline.node9 import (  # noqa: E402 - path fixup must happen first
    reconstruct_timing_for_queue,
)


class AnimaticNode9TimingReconstructor:
    """ComfyUI node: rebuild the full per-frame sequence from Node 8's
    per-key-pose composites + Node 4's keypose_map (timing data) using
    translate-and-copy on a fresh white canvas. Zero AI on held
    frames."""

    CATEGORY = "animatic-refinement"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)  # JSON string with the Node9Result payload
    RETURN_NAMES = ("node9_result_json",)
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "node8_result_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Absolute path to node8_result.json from "
                            "Node 8. Node 9 writes node9_result.json "
                            "alongside it in the same work dir."
                        ),
                    },
                ),
            }
        }

    def run(
        self,
        node8_result_path: str,
    ) -> tuple[str]:
        result = reconstruct_timing_for_queue(
            node8_result_path=Path(node8_result_path),
        )
        return (json.dumps(result.to_dict(), ensure_ascii=False),)


NODE_CLASS_MAPPINGS = {
    "AnimaticNode9TimingReconstructor": AnimaticNode9TimingReconstructor,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaticNode9TimingReconstructor": "Animatic - Node 9: Timing Reconstructor",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
