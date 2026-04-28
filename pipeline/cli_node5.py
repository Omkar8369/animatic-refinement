"""CLI entry point for Node 5.

Usage:
    python -m pipeline.cli_node5 \\
        --node4-result <work>/node4_result.json \\
        --queue <input>/queue.json \\
        [--min-area-ratio 0.001] [--merge-iou 0.5]

Exit codes:
    0  success (character map written for every shot)
    1  expected failure (Node4ResultInputError, QueueLookupError,
       CharacterDetectionError)
    2  unexpected error (bug, not operator error)

Count mismatches between detection and metadata are reconciled
internally and logged as warnings inside each shot's
`character_map.json`. They do NOT fail the CLI.

Kept separate from `pipeline/cli.py` (Node 2), `pipeline/cli_node3.py`
(Node 3), and `pipeline/cli_node4.py` (Node 4) so each node's
invocation surface stays stable across sibling changes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import Node5Error
from .node5 import (
    DEFAULT_DARK_THRESHOLD,
    DEFAULT_MERGE_IOU,
    DEFAULT_MIN_AREA_RATIO,
    detect_characters_for_queue,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node5",
        description=(
            "Node 5 — Detect characters (connected components, no ML) on "
            "each key pose, bin each silhouette into a position zone "
            "(L/CL/C/CR/R), and assign identities by matching metadata's "
            "expected position list to detected silhouettes left-to-right."
        ),
    )
    parser.add_argument(
        "--node4-result",
        required=True,
        type=Path,
        help=(
            "Path to node4_result.json produced by Node 4. Node 5 writes "
            "node5_result.json alongside it in the same work dir."
        ),
    )
    parser.add_argument(
        "--queue",
        required=True,
        type=Path,
        help=(
            "Path to queue.json produced by Node 2. Node 5 uses it to look "
            "up each shot's expected characters + positions."
        ),
    )
    parser.add_argument(
        "--min-area-ratio",
        type=float,
        default=DEFAULT_MIN_AREA_RATIO,
        help=(
            f"Drop connected-component blobs whose bounding-box area is "
            f"below this fraction of frame area. "
            f"Default: {DEFAULT_MIN_AREA_RATIO}."
        ),
    )
    parser.add_argument(
        "--merge-iou",
        type=float,
        default=DEFAULT_MERGE_IOU,
        help=(
            f"Merge two bounding boxes whose IoU meets or exceeds this "
            f"threshold (reunites floating details with parent silhouettes). "
            f"Default: {DEFAULT_MERGE_IOU}."
        ),
    )
    parser.add_argument(
        "--dark-threshold",
        type=int,
        default=DEFAULT_DARK_THRESHOLD,
        help=(
            f"Phase 2f (2026-04-28): luminance threshold (0-255) "
            f"separating dark character outlines from lighter BG "
            f"furniture lines. Pixels with grayscale luminance < this "
            f"value are kept as character ink; pixels >= are erased to "
            f"white BG. Default: {DEFAULT_DARK_THRESHOLD} (storyboard "
            f"convention: dark bold black ~0-50 vs light grey BG "
            f"~80-180). Tune up if a project's character lines are "
            f"slightly faded; tune down if BG lines bleed through."
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
        result = detect_characters_for_queue(
            node4_result_path=args.node4_result,
            queue_path=args.queue,
            min_area_ratio=args.min_area_ratio,
            merge_iou=args.merge_iou,
            dark_threshold=args.dark_threshold,
        )
    except Node5Error as e:
        print(f"[node5] FAILED:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort catch for clean CLI exit
        print(
            f"[node5] UNEXPECTED ERROR: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    if not args.quiet:
        total_detections = sum(s.totalDetections for s in result.shots)
        total_warnings = sum(s.warningCount for s in result.shots)
        total_keyposes = sum(s.keyPoseCount for s in result.shots)
        warn_suffix = (
            f", {total_warnings} reconcile warning(s)"
            if total_warnings
            else ""
        )
        print(
            f"[node5] OK - project='{result.projectName}', "
            f"{len(result.shots)} shot(s), {total_detections} detection(s) "
            f"across {total_keyposes} key pose(s){warn_suffix}. "
            f"Wrote {Path(result.workDir) / 'node5_result.json'}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
