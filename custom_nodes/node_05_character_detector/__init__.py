"""ComfyUI custom-node registration for Node 5 - Character Detection.

This file is the ComfyUI-facing adapter. All business logic lives in
`pipeline/node5.py` — this module only:
  1. Declares the node's INPUT_TYPES + RETURN_TYPES so ComfyUI can wire
     it into a workflow graph.
  2. Calls `detect_characters_for_queue()` and hands its result back as
     a single JSON-serializable string for downstream nodes.

Kept intentionally thin so the same core code is exercised by:
  * `python run_node5.py ...`                 (CLI, tests, CI)
  * `python -m pipeline.cli_node5 ...`        (RunPod shell)
  * this custom node                          (ComfyUI graph)

If you add a ComfyUI-only knob (e.g. a slider), expose it here and pass
it down to `pipeline.node5` — do NOT fork the logic.
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

from pipeline.node5 import (  # noqa: E402 - path fixup must happen first
    DEFAULT_DARK_THRESHOLD,
    DEFAULT_MERGE_IOU,
    DEFAULT_MIN_AREA_RATIO,
    detect_characters_for_queue,
)


class AnimaticNode5CharacterDetector:
    """ComfyUI node: classical connected-components character detection
    on each key pose, with position binning + identity assignment from
    metadata, returning the aggregate result as JSON."""

    CATEGORY = "animatic-refinement"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)  # JSON string with the Node5Result payload
    RETURN_NAMES = ("node5_result_json",)
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "node4_result_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Absolute path to node4_result.json from Node 4. "
                            "Node 5 writes node5_result.json alongside it."
                        ),
                    },
                ),
                "queue_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Absolute path to queue.json from Node 2 — "
                            "needed to look up each shot's expected "
                            "characters + positions."
                        ),
                    },
                ),
                "min_area_ratio": (
                    "FLOAT",
                    {
                        "default": DEFAULT_MIN_AREA_RATIO,
                        "min": 0.0,
                        "max": 0.5,
                        "step": 0.0001,
                        "tooltip": (
                            "Drop connected-component blobs whose area "
                            "is below this fraction of frame area."
                        ),
                    },
                ),
                "merge_iou": (
                    "FLOAT",
                    {
                        "default": DEFAULT_MERGE_IOU,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                        "tooltip": (
                            "Merge two bounding boxes whose IoU meets or "
                            "exceeds this threshold (reunites floating "
                            "details with parent silhouettes)."
                        ),
                    },
                ),
                "dark_threshold": (
                    "INT",
                    {
                        "default": DEFAULT_DARK_THRESHOLD,
                        "min": 1,
                        "max": 254,
                        "step": 1,
                        "tooltip": (
                            "Phase 2f: luminance threshold separating "
                            "dark character outlines from lighter BG "
                            "furniture lines. Pixels with grayscale "
                            "luminance < this value are kept as "
                            "character ink; pixels >= are erased to "
                            "white BG. Default 80 fits the storyboard "
                            "convention (dark bold black ~0-50, light "
                            "grey BG ~80-180)."
                        ),
                    },
                ),
            }
        }

    def run(
        self,
        node4_result_path: str,
        queue_path: str,
        min_area_ratio: float,
        merge_iou: float,
        dark_threshold: int,
    ) -> tuple[str]:
        result = detect_characters_for_queue(
            node4_result_path=node4_result_path,
            queue_path=queue_path,
            min_area_ratio=float(min_area_ratio),
            merge_iou=float(merge_iou),
            dark_threshold=int(dark_threshold),
        )
        # Serialize via the core dataclass' to_dict so the wire format is
        # identical to what node5_result.json on disk contains.
        return (json.dumps(result.to_dict(), ensure_ascii=False),)


NODE_CLASS_MAPPINGS = {
    "AnimaticNode5CharacterDetector": AnimaticNode5CharacterDetector,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaticNode5CharacterDetector": "Animatic - Node 5: Character Detector",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
