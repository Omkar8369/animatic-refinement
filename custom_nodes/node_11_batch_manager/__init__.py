"""ComfyUI custom-node registration for Node 11 - Batch Management.

This file is the ComfyUI-facing adapter. All business logic lives in
`pipeline/node11.py` -- this module only:
  1. Declares the node's INPUT_TYPES + RETURN_TYPES so ComfyUI can wire
     it into a workflow graph.
  2. Calls `run_batch()` and hands its result back as a single
     JSON-serializable string for downstream nodes.

Kept intentionally thin so the same core code is exercised by:
  * `python run_node11.py ...`                 (CLI, tests, CI)
  * `python -m pipeline.cli_node11 ...`        (RunPod shell)
  * this custom node                           (ComfyUI graph)

If you add a ComfyUI-only knob, expose it here and pass it down to
`pipeline.node11` -- do NOT fork the logic.

Note: Node 11 is the project-level orchestrator. Inside a ComfyUI
graph it would typically be the SOLE node (driving Nodes 2-10 via
subprocess); chaining other custom nodes around it doesn't make
much sense since it already handles the entire pipeline. The
ComfyUI registration is provided for symmetry with the other 9
custom nodes; the primary use is via `run_node11.py`.
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

from pipeline.node11 import (  # noqa: E402 - path fixup must happen first
    DEFAULT_COMFYUI_URL,
    DEFAULT_CRF,
    NODE_RANGE,
    run_batch,
)


class AnimaticNode11BatchManager:
    """ComfyUI node: project-level orchestrator. Runs Nodes 2-10
    in sequence against a single batch via subprocess, with per-node
    retry policy + JSONL progress log + final aggregate report.
    Replaces the operator's eight-command shell sequence with one
    invocation."""

    CATEGORY = "animatic-refinement"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)  # JSON string with the Node11Result payload
    RETURN_NAMES = ("node11_result_json",)
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "input_dir": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Directory containing Node 1's outputs "
                            "(metadata.json + characters.json + "
                            "sheet PNGs + shot MP4s). Same dir Node "
                            "2 reads."
                        ),
                    },
                ),
                "work_dir": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Directory where every downstream node "
                            "writes outputs (per-shot frames, "
                            "manifests, refined PNGs, composed "
                            "frames, timed sequences, and final "
                            "MP4s under output/)."
                        ),
                    },
                ),
                "comfyui_url": (
                    "STRING",
                    {
                        "default": DEFAULT_COMFYUI_URL,
                        "tooltip": (
                            "ComfyUI HTTP API endpoint passed to "
                            "Node 7. Ignored when dry_run is True."
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
                            "H.264 CRF value passed to Node 10. "
                            "Lower = higher quality, bigger files."
                        ),
                    },
                ),
                "dry_run": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": (
                            "Pass --dry-run to Node 7 (skip live "
                            "ComfyUI submission). Useful for testing "
                            "the orchestration plumbing without GPU."
                        ),
                    },
                ),
            }
        }

    def run(
        self,
        input_dir: str,
        work_dir: str,
        comfyui_url: str,
        crf: int,
        dry_run: bool,
    ) -> tuple[str]:
        # Per-node retries are not exposed in the ComfyUI node UI
        # (would need 9 INT inputs); CLI users get them via flags.
        retries_by_node = {n: 0 for n in NODE_RANGE}
        result = run_batch(
            input_dir=Path(input_dir),
            work_dir=Path(work_dir),
            comfyui_url=str(comfyui_url),
            crf=int(crf),
            retries_by_node=retries_by_node,
            dry_run=bool(dry_run),
            quiet=True,
        )
        return (json.dumps(result.to_dict(), ensure_ascii=False),)


NODE_CLASS_MAPPINGS = {
    "AnimaticNode11BatchManager": AnimaticNode11BatchManager,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaticNode11BatchManager": "Animatic - Node 11: Batch Manager",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
