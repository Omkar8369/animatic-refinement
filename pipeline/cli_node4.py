"""CLI entry point for Node 4.

Usage:
    python -m pipeline.cli_node4 --node3-result <path> [--threshold 8.0] [--max-edge 128]

Exit codes:
    0  success (key-pose partition written for every shot)
    1  expected failure (Node3ResultInputError, KeyPoseExtractionError)
    2  unexpected error (bug, not operator error)

Kept separate from `pipeline/cli.py` (Node 2) and `pipeline/cli_node3.py`
(Node 3) so each node's invocation surface stays stable across future
changes to its siblings.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import Node4Error
from .node4 import (
    DEFAULT_MAE_THRESHOLD,
    DEFAULT_MAX_EDGE,
    extract_keyposes_for_queue,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node4",
        description=(
            "Node 4 — Partition each shot's frame sequence into key poses "
            "plus held-frame runs, with per-frame translation offsets so a "
            "character sliding across the shot is recorded as ONE key pose, "
            "not many."
        ),
    )
    parser.add_argument(
        "--node3-result",
        required=True,
        type=Path,
        help=(
            "Path to node3_result.json produced by Node 3. Node 4 writes "
            "its own node4_result.json alongside it in the same work dir."
        ),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_MAE_THRESHOLD,
        help=(
            f"Aligned-MAE threshold on 0-255 grayscale. A frame whose "
            f"aligned MAE against the current anchor exceeds this becomes "
            f"a new key pose. Default: {DEFAULT_MAE_THRESHOLD}."
        ),
    )
    parser.add_argument(
        "--max-edge",
        type=int,
        default=DEFAULT_MAX_EDGE,
        help=(
            f"Downscale so max(height, width) = this before comparison. "
            f"Offsets are scaled back to full resolution on write. "
            f"Default: {DEFAULT_MAX_EDGE}."
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
        result = extract_keyposes_for_queue(
            node3_result_path=args.node3_result,
            threshold=args.threshold,
            max_edge=args.max_edge,
        )
    except Node4Error as e:
        print(f"[node4] FAILED:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort catch for clean CLI exit
        print(
            f"[node4] UNEXPECTED ERROR: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    if not args.quiet:
        total_frames = sum(s.totalFrames for s in result.shots)
        total_keys = sum(s.keyPoseCount for s in result.shots)
        print(
            f"[node4] OK - project='{result.projectName}', "
            f"{len(result.shots)} shot(s), {total_keys} key pose(s) "
            f"across {total_frames} frame(s). threshold={result.threshold}. "
            f"Wrote {Path(result.workDir) / 'node4_result.json'}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
