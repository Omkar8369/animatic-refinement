"""CLI entry point for Node 6.

Usage:
    python -m pipeline.cli_node6 \\
        --node5-result <work>/node5_result.json \\
        --queue <input>/queue.json \\
        --characters <input>/characters.json \\
        [--lineart-method dog|canny|threshold]

Exit codes:
    0  success (reference_map.json written for every shot)
    1  expected failure (Node6Error subclass OR QueueLookupError —
       the latter is shared with Node 5's module but raised here too)
    2  unexpected error (bug, not operator error)

Per-detection angle picks are always data (written into
`reference_map.json`'s `scoreBreakdown` + `allScores`) and do NOT fail
the CLI. The only failure paths are I/O / manifest / sheet-format
errors (see `pipeline.errors` for the full Node 6 hierarchy).

Kept separate from Nodes 2-5 CLIs so each node's invocation surface
stays stable across sibling changes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import Node6Error, QueueLookupError
from .node6 import (
    DEFAULT_LINEART_METHOD,
    LINEART_METHODS,
    match_references_for_queue,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node6",
        description=(
            "Node 6 — Slice each character's 8-angle reference sheet via "
            "alpha-island bbox, score every detection from Node 5 against "
            "all 8 angles using a classical multi-signal function "
            "(silhouette IoU + horizontal symmetry + bbox aspect + upper "
            "interior-edge density), pick the winning angle per key pose, "
            "and write a color crop + DoG line-art copy into each shot's "
            "reference_crops/ folder."
        ),
    )
    parser.add_argument(
        "--node5-result",
        required=True,
        type=Path,
        help=(
            "Path to node5_result.json produced by Node 5. Node 6 writes "
            "node6_result.json alongside it in the same work dir."
        ),
    )
    parser.add_argument(
        "--queue",
        required=True,
        type=Path,
        help=(
            "Path to queue.json produced by Node 2. Node 6 uses it to "
            "resolve each character's sheet PNG path."
        ),
    )
    parser.add_argument(
        "--characters",
        required=True,
        type=Path,
        help=(
            "Path to characters.json produced by Node 1. Node 6 reads it "
            "to verify conventions.angleOrderConfirmed is True."
        ),
    )
    parser.add_argument(
        "--lineart-method",
        choices=LINEART_METHODS,
        default=DEFAULT_LINEART_METHOD,
        help=(
            "Method used to convert each chosen color reference crop "
            "into a black-line line-art PNG. 'dog' (Difference of "
            "Gaussians) is the v1 default. 'canny' and 'threshold' are "
            "simple classical stand-ins kept available for A/B testing."
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
        result = match_references_for_queue(
            node5_result_path=args.node5_result,
            queue_path=args.queue,
            characters_path=args.characters,
            lineart_method=args.lineart_method,
        )
    except (Node6Error, QueueLookupError) as e:
        print(f"[node6] FAILED:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort catch for clean CLI exit
        print(
            f"[node6] UNEXPECTED ERROR: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    if not args.quiet:
        total_detections = sum(s.detectionCount for s in result.shots)
        total_keyposes = sum(s.keyPoseCount for s in result.shots)
        total_skipped = sum(s.skippedCount for s in result.shots)
        skip_suffix = (
            f", {total_skipped} detection(s) skipped"
            if total_skipped
            else ""
        )
        print(
            f"[node6] OK - project='{result.projectName}', "
            f"{len(result.shots)} shot(s), {total_detections} reference "
            f"match(es) across {total_keyposes} key pose(s) "
            f"(lineart_method={result.lineArtMethod}){skip_suffix}. "
            f"Wrote {Path(result.workDir) / 'node6_result.json'}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
