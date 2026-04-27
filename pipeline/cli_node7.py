"""CLI entry point for Node 7 - AI-Powered Pose Refinement.

Usage:
    python -m pipeline.cli_node7 \\
        --node6-result <work>/node6_result.json \\
        --queue <input>/queue.json \\
        [--comfyui-url http://127.0.0.1:8188] \\
        [--dry-run] [--quiet]

Exit codes:
    0  success (refined_map.json written for every shot; per-generation
       errors are recorded in refined_map.json, they do NOT fail the CLI)
    1  expected failure (Node7Error subclass OR QueueLookupError -- the
       latter is shared with Node 5's module but raised here too)
    2  unexpected error (bug, not operator error)

Node 7 differs from Nodes 2-6 in two ways worth noting at the CLI level:

  * It is RunPod-only for real runs: ComfyUI must be listening at
    `--comfyui-url` (default `http://127.0.0.1:8188`). `--dry-run` is the
    escape hatch for laptop smoke tests -- it runs the manifest layer
    end-to-end and writes `status="skipped"` generations without ever
    touching ComfyUI.

  * It breaks the `pipeline/nodeN.py` template on purpose (locked
    decision #9): the CLI delegates to
    `custom_nodes.node_07_pose_refiner.orchestrate.refine_queue` rather
    than a `pipeline.node7` module, because the authoritative artifact
    is `workflow.json` (a ComfyUI graph).

Per-generation errors (ComfyUI connection, workflow-template mismatch,
generation-level failures) are captured into per-shot `refined_map.json`
as `status="error"` records with `errorMessage`. The CLI only exits
non-zero when the whole pass cannot produce manifests at all.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import Node7Error, QueueLookupError

# Import through the package path rather than a top-level relative so this
# module remains importable whether the caller put the repo root on
# sys.path (via `run_node7.py`) or is running from a standard checkout.
from custom_nodes.node_07_pose_refiner.orchestrate import (  # noqa: E402
    DEFAULT_COMFYUI_URL,
    DEFAULT_PRECISION,
    DEFAULT_STYLE_LORA,
    DEFAULT_WORKFLOW,
    PRECISION_CHOICES,
    STYLE_LORA_CHOICES,
    WORKFLOW_CHOICES,
    OrchestrateConfig,
    refine_queue,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node7",
        description=(
            "Node 7 - Per-character pose refinement. For every detection "
            "from Node 5/6, submit a ComfyUI workflow that uses a pose "
            "ControlNet (dwpose OR lineart fallback, per character) + "
            "IP-Adapter on the reference color crop to generate a BnW "
            "line-art version of the character in the rough's pose. "
            "Writes refined_map.json per shot + node7_result.json "
            "aggregate. Real runs require ComfyUI on a RunPod pod; "
            "--dry-run exercises the manifest layer offline."
        ),
    )
    parser.add_argument(
        "--node6-result",
        required=True,
        type=Path,
        help=(
            "Path to node6_result.json produced by Node 6. Node 7 writes "
            "node7_result.json alongside it in the same work dir."
        ),
    )
    parser.add_argument(
        "--queue",
        required=True,
        type=Path,
        help=(
            "Path to queue.json produced by Node 2. Node 7 reads each "
            "character's poseExtractor route (dwpose vs. "
            "lineart-fallback) from here."
        ),
    )
    parser.add_argument(
        "--comfyui-url",
        default=DEFAULT_COMFYUI_URL,
        help=(
            f"ComfyUI HTTP API root. Default {DEFAULT_COMFYUI_URL} is "
            "correct for running on the RunPod pod alongside a local "
            "ComfyUI on port 8188. Ignored when --dry-run is set."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip ComfyUI submission entirely. Every generation lands "
            "as status='skipped' in refined_map.json. Useful for "
            "laptop smoke tests that exercise the manifest layer "
            "without the pod's GPU."
        ),
    )
    parser.add_argument(
        "--per-prompt-timeout",
        type=float,
        default=600.0,
        help=(
            "Per-detection timeout (seconds) while polling ComfyUI's "
            "/history endpoint. Default 600s covers SD 1.5 + ControlNet "
            "+ IP-Adapter cold-load on a fresh pod (Phase 1) and Flux "
            "Dev fp16 + Union CN cold-load (Phase 2 v2)."
        ),
    )
    parser.add_argument(
        "--workflow",
        choices=WORKFLOW_CHOICES,
        default=DEFAULT_WORKFLOW,
        help=(
            f"Workflow stack to use (default {DEFAULT_WORKFLOW!r}). "
            "v1 = Phase 1 SD 1.5 + AnyLoRA + DWPose / lineart-fallback "
            "(two workflow JSONs, routed by characters.json's "
            "poseExtractor). v2 = Phase 2 Flux Dev + Flat Cartoon Style "
            "v1.2 LoRA + ControlNet Union Pro (single workflow JSON; "
            "txt2img in Phase 2a, img2img with denoise=0.55 from Phase "
            "2c onward). Phase 2a ships v1 as default for safety; "
            "Phase 2c will flip the default to v2."
        ),
    )
    parser.add_argument(
        "--precision",
        choices=PRECISION_CHOICES,
        default=DEFAULT_PRECISION,
        help=(
            f"Flux model precision (default {DEFAULT_PRECISION!r}). "
            "fp16 = full-precision Flux Dev (~23 GB UNET + ~9.5 GB "
            "T5-XXL); requires A100 80GB or larger. fp8 = quantized "
            "Flux Dev (~12 GB UNET + ~4.6 GB T5-XXL); fits on 4090 "
            "24GB with the rest of the v2 stack. IGNORED when "
            "--workflow=v1 (SD 1.5 has its own precision baked into "
            "the checkpoint)."
        ),
    )
    parser.add_argument(
        "--style-lora",
        choices=STYLE_LORA_CHOICES,
        default=DEFAULT_STYLE_LORA,
        help=(
            f"Style LoRA loaded at workflow_flux_v2.json node 20 "
            f"(default {DEFAULT_STYLE_LORA!r}). flat_cartoon_v12 = "
            "generic Flat Cartoon Style v1.2 (Civitai 644541, ~82 MB) "
            "shipped in Phase 2a. tmkoc_v1 = custom-trained TMKOC v1 "
            "LoRA (Phase 2d). Until Phase 2d's training run lands a "
            "real tmkoc_style_v1.safetensors weight, --style-lora="
            "tmkoc_v1 will fail at ComfyUI submission with a missing-"
            "file error from node 20's LoraLoader. See "
            "tools/phase2d/PHASE2D_TRAINING_PLAYBOOK.md for the "
            "Path A bootstrap + ai-toolkit training procedure. "
            "IGNORED when --workflow=v1 (Phase 1 has its own LoRA "
            "stack). Strength is locked at 0.75 per locked decision #2."
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

    config = OrchestrateConfig(
        node6_result_path=args.node6_result,
        queue_path=args.queue,
        comfyui_url=args.comfyui_url,
        dry_run=args.dry_run,
        per_prompt_timeout_s=args.per_prompt_timeout,
        workflow=args.workflow,
        precision=args.precision,
        style_lora=args.style_lora,
    )

    try:
        result = refine_queue(config)
    except (Node7Error, QueueLookupError) as e:
        print(f"[node7] FAILED:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort catch for clean CLI exit
        print(
            f"[node7] UNEXPECTED ERROR: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    if not args.quiet:
        total_generated = sum(s.generatedCount for s in result.shots)
        total_skipped = sum(s.skippedCount for s in result.shots)
        total_errors = sum(s.errorCount for s in result.shots)
        mode = "DRY-RUN" if result.dryRun else f"LIVE ({result.comfyUIUrl})"
        # Phase 2: include workflow + precision + style-lora in the
        # summary so the operator sees exactly what stack ran. Phase 2c
        # flipped the default to v2; precision + style-lora only show
        # up under v2 because they're ignored under v1.
        stack = (
            f"workflow={args.workflow}"
            + (
                f" precision={args.precision} style_lora={args.style_lora}"
                if args.workflow == "v2"
                else ""
            )
        )
        manifest = Path(result.workDir) / "node7_result.json"
        print(
            f"[node7] OK [{mode}] [{stack}] project='{result.projectName}', "
            f"{len(result.shots)} shot(s), "
            f"{total_generated} generated / {total_skipped} skipped / "
            f"{total_errors} error(s). Wrote {manifest}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
