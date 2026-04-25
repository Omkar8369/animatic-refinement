"""CLI entry point for Node 9 - Timing Reconstruction.

Usage:
    python -m pipeline.cli_node9 \\
        --node8-result <work>/node8_result.json

Exit codes:
    0  success
    1  expected failure (Node9Error subclass: Node8ResultInputError /
       KeyPoseMapInputError / TimingReconstructionError /
       FrameCountMismatchError)
    2  unexpected error (bug, not operator error)

Node 9 has no warn-and-reconcile pattern (unlike Nodes 5 and 8) --
held frames REQUIRE the anchor's composed PNG, and there's no
meaningful substitute. Any failure is a hard contract violation that
needs operator action.

Kept separate from sibling nodes' CLIs so each node's invocation
surface stays stable across other nodes' changes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import Node9Error
from .node9 import reconstruct_timing_for_queue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node9",
        description=(
            "Node 9 -- Reconstruct the full per-frame sequence from "
            "Node 8's per-key-pose composites + Node 4's keypose_map "
            "(timing data). Anchor frames copy Node 8's composite "
            "as-is; held frames are translate-and-copied onto a "
            "fresh white canvas at the (dy, dx) offset Node 4 "
            "recorded. Zero AI regeneration on held frames. Output: "
            "one PNG per frame of the original shot, ready for Node "
            "10 to encode back to MP4."
        ),
    )
    parser.add_argument(
        "--node8-result",
        required=True,
        type=Path,
        help=(
            "Path to node8_result.json produced by Node 8. Node 9 "
            "writes node9_result.json alongside it in the same work "
            "dir, plus per-shot timed_map.json + "
            "timed/frame_NNNN.png files."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the success summary line.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = reconstruct_timing_for_queue(
            node8_result_path=args.node8_result,
        )
    except Node9Error as e:
        print(f"[node9] FAILED:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort catch for clean CLI exit
        print(
            f"[node9] UNEXPECTED ERROR: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    if not args.quiet:
        total_frames = sum(s.totalFrames for s in result.shots)
        total_anchors = sum(s.anchorCount for s in result.shots)
        total_held = sum(s.heldCount for s in result.shots)
        print(
            f"[node9] OK - project='{result.projectName}', "
            f"{len(result.shots)} shot(s), {total_frames} frame(s) "
            f"reconstructed ({total_anchors} anchor + {total_held} "
            f"held). Wrote {Path(result.workDir) / 'node9_result.json'}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
