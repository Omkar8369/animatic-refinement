"""CLI entry point for Node 8 - Scene Assembly.

Usage:
    python -m pipeline.cli_node8 \\
        --node7-result <work>/node7_result.json \\
        [--background white]

Exit codes:
    0  success (substitute-rough warnings still exit 0 -- they're
       data, recorded in composed_map.json)
    1  expected failure (Node8Error subclass: Node7ResultInputError /
       RefinedPngError / CompositingError)
    2  unexpected error (bug, not operator error)

Per-character substitute-rough events are warnings, not failures, and
do NOT fail the CLI -- they're collected in `composed_map.json` so
the operator can drive a Node 7 retry pass for the affected key poses
without losing the timing of intervening shots.

Kept separate from sibling nodes' CLIs so each node's invocation
surface stays stable across other nodes' changes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import Node8Error
from .node8 import (
    DEFAULT_BACKGROUND,
    SUPPORTED_BACKGROUNDS,
    compose_for_queue,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node8",
        description=(
            "Node 8 -- Composite Node 7's per-character refined PNGs "
            "into a single source-MP4-resolution frame per key pose. "
            "Bbox is the single source of truth for placement (Node 5 "
            "wrote it, Node 7 cropped with it, Node 8 places back with "
            "it). Feet-pinned scaling: refined character's feet land "
            "exactly at bbox.bottomY. Substitute-rough on Node 7 "
            "errors -- warn-and-reconcile, exit 0."
        ),
    )
    parser.add_argument(
        "--node7-result",
        required=True,
        type=Path,
        help=(
            "Path to node7_result.json produced by Node 7. Node 8 "
            "writes node8_result.json alongside it in the same work "
            "dir, plus per-shot composed_map.json + "
            "composed/<keyPoseIndex>_composite.png files."
        ),
    )
    parser.add_argument(
        "--background",
        choices=SUPPORTED_BACKGROUNDS,
        default=DEFAULT_BACKGROUND,
        help=(
            "Canvas background color. Only 'white' is supported in v1 "
            "(matches the BnW line-art deliverable contract). 'black' "
            "and 'transparent' are reserved for future use."
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
        result = compose_for_queue(
            node7_result_path=args.node7_result,
            background=args.background,
        )
    except Node8Error as e:
        print(f"[node8] FAILED:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort catch for clean CLI exit
        print(
            f"[node8] UNEXPECTED ERROR: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    if not args.quiet:
        total_keyposes = sum(s.keyPoseCount for s in result.shots)
        total_composed = sum(s.composedCount for s in result.shots)
        total_substitute = sum(s.substituteCount for s in result.shots)
        sub_suffix = (
            f", {total_substitute} substitute-rough warning(s)"
            if total_substitute
            else ""
        )
        print(
            f"[node8] OK - project='{result.projectName}', "
            f"{len(result.shots)} shot(s), {total_composed} composited "
            f"frame(s) across {total_keyposes} key pose(s) "
            f"(background={result.background}){sub_suffix}. "
            f"Wrote {Path(result.workDir) / 'node8_result.json'}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
