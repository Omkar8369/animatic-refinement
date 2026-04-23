"""CLI entry point for Node 3.

Usage:
    python -m pipeline.cli_node3 --queue <path-to-queue.json> --work-dir <path>

Exit codes:
    0  success (frames written; warnings are still exit 0)
    1  expected failure (QueueInputError, FFmpegError, FrameExtractionError)
    2  unexpected error (bug, not operator error)

Kept separate from `pipeline/cli.py` (Node 2's CLI) so Node 2's
invocation stays stable across future changes to Node 3.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import Node3Error
from .node3 import extract_frames_for_queue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node3",
        description=(
            "Node 3 — Decode rough-animatic MP4s into per-shot PNG frame "
            "sequences using ffmpeg."
        ),
    )
    parser.add_argument(
        "--queue",
        required=True,
        type=Path,
        help="Path to queue.json produced by Node 2.",
    )
    parser.add_argument(
        "--work-dir",
        required=True,
        type=Path,
        help=(
            "Working directory. Each shot gets `<work-dir>/<shotId>/` "
            "containing frame_NNNN.png + _manifest.json. An aggregate "
            "node3_result.json is written at the work-dir root."
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
        result = extract_frames_for_queue(args.queue, args.work_dir)
    except Node3Error as e:
        print(f"[node3] FAILED:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort catch for clean CLI exit
        print(
            f"[node3] UNEXPECTED ERROR: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    if not args.quiet:
        total = sum(s.actualFrames for s in result.shots)
        warning_line = (
            f"  {len(result.warnings)} warning(s) — see node3_result.json"
            if result.warnings
            else "  0 warnings"
        )
        print(
            f"[node3] OK - project='{result.projectName}', "
            f"{len(result.shots)} shot(s), {total} frame(s) total. "
            f"Wrote {Path(result.workDir) / 'node3_result.json'}."
        )
        print(warning_line)
        # If there are warnings, also echo the first few so the operator
        # sees them without opening the JSON.
        for w in result.warnings[:5]:
            print(f"  WARN: {w.message}")
        if len(result.warnings) > 5:
            print(f"  ... ({len(result.warnings) - 5} more; full list in node3_result.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
