"""CLI entry point for Node 10 - Output Generation (PNG -> MP4).

Usage:
    python -m pipeline.cli_node10 \\
        --node9-result <work>/node9_result.json \\
        [--crf 18]

Exit codes:
    0  success
    1  expected failure (Node10Error subclass: Node9ResultInputError /
       TimedFramesError / FFmpegEncodeError)
    2  unexpected error (bug, not operator error)

CRF is the only quality knob -- codec / preset / pixel-format are
locked at the architecture level (libx264 / medium / yuv420p) for
maximum playback compatibility.

Kept separate from sibling nodes' CLIs so each node's invocation
surface stays stable across other nodes' changes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import Node10Error
from .node10 import (
    DEFAULT_CRF,
    encode_for_queue,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node10",
        description=(
            "Node 10 -- Encode each shot's full per-frame PNG sequence "
            "(from Node 9) into a single deliverable MP4 at 25 FPS. "
            "Output to <work-dir>/output/<shotId>_refined.mp4. "
            "Codec / preset / pixel-format are locked (H.264 / medium "
            "/ yuv420p); CRF is exposed as the single quality knob."
        ),
    )
    parser.add_argument(
        "--node9-result",
        required=True,
        type=Path,
        help=(
            "Path to node9_result.json produced by Node 9. Node 10 "
            "writes node10_result.json alongside it in the same work "
            "dir, plus per-shot <shotId>_refined.mp4 files in "
            "<work-dir>/output/."
        ),
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=DEFAULT_CRF,
        help=(
            f"H.264 CRF value (default {DEFAULT_CRF}). Lower = higher "
            "quality and bigger files. CRF 18 is visually lossless on "
            "BnW line art; 23 (the libx264 default) trades visible "
            "edge artifacts for smaller files."
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
        result = encode_for_queue(
            node9_result_path=args.node9_result,
            crf=args.crf,
        )
    except Node10Error as e:
        print(f"[node10] FAILED:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort catch for clean CLI exit
        print(
            f"[node10] UNEXPECTED ERROR: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    if not args.quiet:
        total_frames = sum(s.frameCount for s in result.shots)
        total_seconds = sum(s.durationSeconds for s in result.shots)
        total_bytes = sum(s.fileSizeBytes for s in result.shots)
        total_mb = total_bytes / (1024 * 1024)
        print(
            f"[node10] OK - project='{result.projectName}', "
            f"{len(result.shots)} shot(s), {total_frames} frame(s) "
            f"encoded ({total_seconds:.2f}s total, {total_mb:.1f} MB, "
            f"crf={result.crf}). Wrote "
            f"{Path(result.workDir) / 'node10_result.json'}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
