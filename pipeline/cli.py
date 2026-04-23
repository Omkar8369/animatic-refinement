"""CLI entry point for Node 2.

Usage:
    python -m pipeline.cli --input-dir <path-to-project-input-folder>

Exit codes:
    0  success (queue.json written)
    1  validation failure (any Node2Error subclass)
    2  unexpected error (bug, not operator error)

The CLI is what runs on RunPod between "operator uploaded files" and
"start ComfyUI workflows" — so its stdout/stderr is the first place an
operator looks when a batch won't start.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import Node2Error
from .node2 import serialize_queue, validate_and_build_queue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node2",
        description=(
            "Node 2 — Validate Node 1 outputs and build the processing queue."
        ),
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        type=Path,
        help=(
            "Directory containing metadata.json, characters.json, the character "
            "sheet PNGs, and every referenced .mp4 file."
        ),
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=None,
        help="Path to write queue.json. Defaults to <input-dir>/queue.json.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the success summary line; useful when chained from other tools.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    output_path: Path = args.output_file or (args.input_dir / "queue.json")

    try:
        queue = validate_and_build_queue(args.input_dir)
    except Node2Error as e:
        # Locked design: fail fast. One clean error message, exit 1.
        print(f"[node2] VALIDATION FAILED:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort catch for clean CLI exit
        print(
            f"[node2] UNEXPECTED ERROR: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(serialize_queue(queue), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if not args.quiet:
        print(
            f"[node2] OK - project='{queue.projectName}', "
            f"{queue.totalShots} shot(s) across {len(queue.batches)} batch(es) "
            f"of up to {queue.batchSize}. Wrote {output_path}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
