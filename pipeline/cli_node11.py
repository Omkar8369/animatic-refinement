"""CLI entry point for Node 11 - Batch Management.

Usage:
    python -m pipeline.cli_node11 \\
        --input-dir <i> \\
        --work-dir <w> \\
        [--comfyui-url http://127.0.0.1:8188] \\
        [--crf 18] \\
        [--retry-node7 2] \\
        [--dry-run] \\
        [--quiet]

Exit codes (DIFFER from Nodes 2-10):
    0  success or partial success (succeededShots > 0); read
       failedShots in node11_result.json to see if any failed.
    1  expected failure (Node11Error subclass: InputDirError /
       NodeStepError / BatchAllFailedError) -- 100% failure or
       Node 2 (queue.json producer) failed.
    2  unexpected error (bug, not operator error)

Per-node retry overrides use a flag-per-node pattern:
`--retry-node3 1 --retry-node7 2 ...`. Defaults to 0 retries
per node.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import Node11Error
from .node11 import (
    DEFAULT_COMFYUI_URL,
    DEFAULT_CRF,
    NODE_RANGE,
    run_batch,
)

# Phase 2: import the same workflow + precision constants the Node 7
# CLI uses so the Node 11 CLI's --workflow and --precision flags
# accept the same set of values without duplicating the source of
# truth.
from custom_nodes.node_07_pose_refiner.orchestrate import (  # noqa: E402
    DEFAULT_PRECISION,
    DEFAULT_WORKFLOW,
    PRECISION_CHOICES,
    WORKFLOW_CHOICES,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node11",
        description=(
            "Node 11 -- Project-level orchestrator. Runs Nodes 2-10 "
            "in sequence against a single batch via subprocess, with "
            "per-node retry policy + JSONL progress log + final "
            "aggregate report. Replaces the operator's eight-command "
            "shell sequence with one CLI invocation."
        ),
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        type=Path,
        help=(
            "Directory containing Node 1's outputs (metadata.json + "
            "characters.json + sheet PNGs + shot MP4s). Same dir Node "
            "2 reads."
        ),
    )
    parser.add_argument(
        "--work-dir",
        required=True,
        type=Path,
        help=(
            "Directory where every downstream node writes outputs "
            "(per-shot frames, manifests, refined PNGs, composed "
            "frames, timed sequences, and final MP4s under output/)."
        ),
    )
    parser.add_argument(
        "--comfyui-url",
        default=DEFAULT_COMFYUI_URL,
        help=(
            f"ComfyUI HTTP API endpoint (default {DEFAULT_COMFYUI_URL}). "
            "Passed to Node 7. Ignored when --dry-run is set."
        ),
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=DEFAULT_CRF,
        help=(
            f"H.264 CRF value (default {DEFAULT_CRF}). Passed to Node "
            "10. Lower = higher quality and bigger files."
        ),
    )
    for n in NODE_RANGE:
        parser.add_argument(
            f"--retry-node{n}",
            type=int,
            default=0,
            metavar="N",
            help=(
                f"Number of retries for Node {n} on subprocess "
                "non-zero exit (default 0 = fail-fast). Most useful "
                "for Node 7 (most likely to flake)."
            ),
        )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Pass --dry-run to Node 7 (skip live ComfyUI submission; "
            "Node 7 records every generation as 'skipped'). Useful for "
            "testing the full orchestration plumbing on the laptop "
            "without GPU. Other nodes ignore the flag."
        ),
    )
    parser.add_argument(
        "--workflow",
        choices=WORKFLOW_CHOICES,
        default=DEFAULT_WORKFLOW,
        help=(
            f"Node 7 workflow stack (default {DEFAULT_WORKFLOW!r}). "
            "v1 = Phase 1 SD 1.5 + AnyLoRA + DWPose / lineart-fallback. "
            "v2 = Phase 2 Flux Dev + Flat Cartoon Style LoRA + "
            "ControlNet Union Pro. Passes through to Node 7's "
            "--workflow flag; ignored by other nodes."
        ),
    )
    parser.add_argument(
        "--precision",
        choices=PRECISION_CHOICES,
        default=DEFAULT_PRECISION,
        help=(
            f"Node 7 v2 model precision (default {DEFAULT_PRECISION!r}). "
            "fp16 = full Flux Dev (A100 80GB). fp8 = quantized fallback "
            "(4090 24GB). Ignored when --workflow=v1."
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

    retries_by_node = {
        n: getattr(args, f"retry_node{n}") for n in NODE_RANGE
    }

    try:
        result = run_batch(
            input_dir=args.input_dir,
            work_dir=args.work_dir,
            comfyui_url=args.comfyui_url,
            crf=args.crf,
            retries_by_node=retries_by_node,
            dry_run=args.dry_run,
            quiet=args.quiet,
            workflow=args.workflow,
            precision=args.precision,
        )
    except Node11Error as e:
        print(f"[node11] FAILED:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort catch for clean CLI exit
        print(
            f"[node11] UNEXPECTED ERROR: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    if not args.quiet:
        out_dir = Path(result.workDir) / "output"
        partial = (
            f" (PARTIAL: {result.failedShots} failed)"
            if result.failedShots
            else ""
        )
        print(
            f"[node11] OK - project='{result.projectName}', "
            f"{result.totalShots} shot(s) / "
            f"{result.succeededShots} succeeded / "
            f"{result.failedShots} failed{partial}, "
            f"total {result.totalSeconds:.1f}s, "
            f"MP4s in {out_dir}/."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
