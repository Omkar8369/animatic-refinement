"""ComfyUI custom-node registration for Node 3 - MP4 -> PNG sequence.

This file is the ComfyUI-facing adapter. All business logic lives in
`pipeline/node3.py` — this module only:
  1. Declares the node's INPUT_TYPES + RETURN_TYPES so ComfyUI can wire
     it into a workflow graph.
  2. Calls `extract_frames_for_queue()` and hands its result back as a
     single JSON-serializable dict that downstream nodes can consume.

Kept intentionally thin so the same core code is exercised by:
  * `python run_node3.py ...`                 (CLI, tests, CI)
  * `python -m pipeline.cli_node3 ...`        (RunPod shell)
  * this custom node                          (ComfyUI graph)

If you add a ComfyUI-only knob (e.g. a checkbox), expose it here and
pass it down to `pipeline.node3` — do NOT fork the logic.
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

from pipeline.node3 import extract_frames_for_queue  # noqa: E402


class AnimaticNode3Mp4ToPng:
    """ComfyUI node: decode every MP4 listed in queue.json into per-shot
    PNG sequences and return the aggregate result as JSON."""

    CATEGORY = "animatic-refinement"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)  # JSON string with the Node3Result payload
    RETURN_NAMES = ("node3_result_json",)
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "queue_json_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Absolute path to queue.json from Node 2.",
                    },
                ),
                "work_dir": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Folder to write per-shot frame folders and "
                            "node3_result.json into."
                        ),
                    },
                ),
            }
        }

    def run(self, queue_json_path: str, work_dir: str) -> tuple[str]:
        result = extract_frames_for_queue(queue_json_path, work_dir)
        # Serialize via the core dataclasses' to_dict so the wire format
        # is identical to what node3_result.json on disk contains.
        return (json.dumps(result.to_dict(), ensure_ascii=False),)


NODE_CLASS_MAPPINGS = {
    "AnimaticNode3Mp4ToPng": AnimaticNode3Mp4ToPng,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaticNode3Mp4ToPng": "Animatic - Node 3: MP4 -> PNG frames",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
