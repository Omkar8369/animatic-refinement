"""ComfyUI custom-node registration for Node 4 - Key Pose Extraction.

This file is the ComfyUI-facing adapter. All business logic lives in
`pipeline/node4.py` — this module only:
  1. Declares the node's INPUT_TYPES + RETURN_TYPES so ComfyUI can wire
     it into a workflow graph.
  2. Calls `extract_keyposes_for_queue()` and hands its result back as
     a single JSON-serializable string for downstream nodes.

Kept intentionally thin so the same core code is exercised by:
  * `python run_node4.py ...`                 (CLI, tests, CI)
  * `python -m pipeline.cli_node4 ...`        (RunPod shell)
  * this custom node                          (ComfyUI graph)

If you add a ComfyUI-only knob (e.g. a slider), expose it here and pass
it down to `pipeline.node4` — do NOT fork the logic.
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

from pipeline.node4 import (  # noqa: E402 - path fixup must happen first
    DEFAULT_MAE_THRESHOLD,
    DEFAULT_MAX_EDGE,
    extract_keyposes_for_queue,
)


class AnimaticNode4KeyPoseExtractor:
    """ComfyUI node: partition each shot into key poses + held-frame runs
    (translation-aware) and return the aggregate result as JSON."""

    CATEGORY = "animatic-refinement"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)  # JSON string with the Node4Result payload
    RETURN_NAMES = ("node4_result_json",)
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "node3_result_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Absolute path to node3_result.json from Node 3. "
                            "Node 4 writes node4_result.json alongside it."
                        ),
                    },
                ),
                "threshold": (
                    "FLOAT",
                    {
                        "default": DEFAULT_MAE_THRESHOLD,
                        "min": 0.0,
                        "max": 128.0,
                        "step": 0.1,
                        "tooltip": (
                            "Aligned-MAE threshold on 0-255 grayscale. "
                            "Frames whose aligned MAE against the current "
                            "anchor exceeds this become new key poses."
                        ),
                    },
                ),
                "max_edge": (
                    "INT",
                    {
                        "default": DEFAULT_MAX_EDGE,
                        "min": 16,
                        "max": 1024,
                        "step": 1,
                        "tooltip": (
                            "Downscale so max(H, W) = this before comparison. "
                            "Offsets are scaled back to full resolution on write."
                        ),
                    },
                ),
            }
        }

    def run(
        self,
        node3_result_path: str,
        threshold: float,
        max_edge: int,
    ) -> tuple[str]:
        result = extract_keyposes_for_queue(
            node3_result_path=node3_result_path,
            threshold=float(threshold),
            max_edge=int(max_edge),
        )
        # Serialize via the core dataclass' to_dict so the wire format is
        # identical to what node4_result.json on disk contains.
        return (json.dumps(result.to_dict(), ensure_ascii=False),)


NODE_CLASS_MAPPINGS = {
    "AnimaticNode4KeyPoseExtractor": AnimaticNode4KeyPoseExtractor,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaticNode4KeyPoseExtractor": "Animatic - Node 4: Key Pose Extractor",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
