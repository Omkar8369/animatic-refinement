"""ComfyUI custom-node registration for Node 10 - Output Generation.

This file is the ComfyUI-facing adapter. All business logic lives in
`pipeline/node10.py` -- this module only:
  1. Declares the node's INPUT_TYPES + RETURN_TYPES so ComfyUI can wire
     it into a workflow graph.
  2. Calls `encode_for_queue()` and hands its result back as a single
     JSON-serializable string for downstream nodes.

Kept intentionally thin so the same core code is exercised by:
  * `python run_node10.py ...`                 (CLI, tests, CI)
  * `python -m pipeline.cli_node10 ...`        (RunPod shell)
  * this custom node                           (ComfyUI graph)

If you add a ComfyUI-only knob, expose it here and pass it down to
`pipeline.node10` -- do NOT fork the logic.
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

from pipeline.node10 import (  # noqa: E402 - path fixup must happen first
    DEFAULT_CRF,
    encode_for_queue,
)


class AnimaticNode10PngToMp4:
    """ComfyUI node: encode each shot's full per-frame PNG sequence
    (from Node 9) into a deliverable MP4 at 25 FPS using ffmpeg via
    imageio-ffmpeg's static binary. Codec / preset / pixel-format are
    locked (libx264 / medium / yuv420p) for maximum compatibility;
    CRF is the single quality knob."""

    CATEGORY = "animatic-refinement"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)  # JSON string with the Node10Result payload
    RETURN_NAMES = ("node10_result_json",)
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "node9_result_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Absolute path to node9_result.json from "
                            "Node 9. Node 10 writes node10_result.json "
                            "alongside it in the same work dir, plus "
                            "<work-dir>/output/<shotId>_refined.mp4 "
                            "files."
                        ),
                    },
                ),
                "crf": (
                    "INT",
                    {
                        "default": DEFAULT_CRF,
                        "min": 0,
                        "max": 51,
                        "step": 1,
                        "tooltip": (
                            "H.264 CRF value (default 18). Lower = "
                            "higher quality and bigger files."
                        ),
                    },
                ),
            }
        }

    def run(
        self,
        node9_result_path: str,
        crf: int,
    ) -> tuple[str]:
        result = encode_for_queue(
            node9_result_path=Path(node9_result_path),
            crf=int(crf),
        )
        return (json.dumps(result.to_dict(), ensure_ascii=False),)


NODE_CLASS_MAPPINGS = {
    "AnimaticNode10PngToMp4": AnimaticNode10PngToMp4,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaticNode10PngToMp4": "Animatic - Node 10: PNG to MP4",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
