"""Generate a persistent Node 6 work dir on disk for Node 7 smoke testing.

Why this exists
---------------
Node 7's live path (ComfyUI on a RunPod pod) needs realistic input:
- `<work-dir>/node6_result.json`
- `<work-dir>/<shotId>/reference_map.json`
- `<work-dir>/<shotId>/keyposes/frame_0001.png` (DWPose preprocessor reads this)
- `<work-dir>/<shotId>/reference_crops/<identity>_<angle>.png` (IP-Adapter reads this)
- `<input-dir>/queue.json` (poseExtractor routing per character)

`tests/test_node7.py::_build_fixture` synthesizes the same scaffold in a
`tmp_path`, but it writes PNG stubs (just the 8-byte PNG header) because
the test suite runs in --dry-run and never actually uploads them to
ComfyUI. The pod's live run *does* upload them, so we need real PNGs.

This script produces exactly that: a scaffold on disk at `--work-dir`
(default `/workspace/smoke_workdir`) with crude but real 512x512 PNGs:
- A stick-figure silhouette as the rough key pose (so DWPose has a
  recognizable biped to skeletonize).
- Per-character solid-color silhouettes as reference crops (so
  IP-Adapter sees a colored image, which is what its identity
  embedding expects).

Then you run Node 7 against it:

    python3 run_node7.py \
        --node6-result /workspace/smoke_workdir/work/node6_result.json \
        --queue        /workspace/smoke_workdir/input/queue.json

CLI
---
    python3 tools/make_smoke_node6_workdir.py \
        [--work-dir /workspace/smoke_workdir] \
        [--routes "Bhim=dwpose,Jaggu=lineart-fallback"] \
        [--force]

`--force` wipes the target directory first. Otherwise refuses to
overwrite an existing scaffold so you can't accidentally clobber a real
work dir.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError as exc:  # pragma: no cover - stdlib-only environments
    raise SystemExit(
        "[smoke] Pillow not installed. Run `pip install pillow` first."
    ) from exc


CANVAS = 512
KEYPOSE_FILENAME = "frame_0001.png"
KEYPOSE_ANGLE = "front"


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _draw_stick_figure(path: Path) -> None:
    """Crude biped silhouette on white. DWPose should skeletonize it."""
    img = Image.new("RGB", (CANVAS, CANVAS), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    cx = CANVAS // 2
    # Head (circle)
    draw.ellipse((cx - 40, 80, cx + 40, 160), fill=(0, 0, 0))
    # Torso (thick line)
    draw.line((cx, 160, cx, 330), fill=(0, 0, 0), width=18)
    # Arms (spread)
    draw.line((cx, 200, cx - 100, 280), fill=(0, 0, 0), width=12)
    draw.line((cx, 200, cx + 100, 280), fill=(0, 0, 0), width=12)
    # Legs
    draw.line((cx, 330, cx - 70, 460), fill=(0, 0, 0), width=14)
    draw.line((cx, 330, cx + 70, 460), fill=(0, 0, 0), width=14)
    img.save(path, "PNG")


def _draw_color_reference(path: Path, identity: str, color: tuple[int, int, int]) -> None:
    """Solid-color silhouette of a character on white.

    IP-Adapter's identity embedding expects a textured/colored image. A
    solid silhouette is crude but enough to prove the pipeline plumbs
    the right crop through to IP-Adapter-Plus without type errors.
    """
    img = Image.new("RGB", (CANVAS, CANVAS), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    cx = CANVAS // 2
    # Head
    draw.ellipse((cx - 60, 60, cx + 60, 180), fill=color)
    # Body (trapezoid-ish rectangle)
    draw.rectangle((cx - 90, 180, cx + 90, 380), fill=color)
    # Legs
    draw.rectangle((cx - 70, 380, cx - 10, 480), fill=color)
    draw.rectangle((cx + 10, 380, cx + 70, 480), fill=color)
    # Label so the two references are visually distinguishable
    draw.text((cx - 40, CANVAS - 30), identity, fill=(0, 0, 0))
    img.save(path, "PNG")


def _parse_routes(routes_csv: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in routes_csv.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise SystemExit(f"[smoke] bad --routes entry: {pair!r} (want Name=route)")
        ident, route = (p.strip() for p in pair.split("=", 1))
        if route not in ("dwpose", "lineart-fallback"):
            raise SystemExit(
                f"[smoke] bad route {route!r} for {ident!r}; "
                "must be 'dwpose' or 'lineart-fallback'"
            )
        out[ident] = route
    if not out:
        raise SystemExit("[smoke] --routes produced an empty dict")
    return out


# Per-identity palette so the reference crops are visually distinct
IDENTITY_COLORS: dict[str, tuple[int, int, int]] = {
    "Bhim": (220, 60, 60),      # red
    "Jaggu": (60, 160, 80),     # green
    "Chutki": (220, 100, 180),  # pink
    "Raju": (80, 120, 220),     # blue
    "Kalia": (80, 60, 60),      # dark brown
    "Dholu": (200, 180, 60),    # yellow
    "Bholu": (200, 140, 60),    # orange
}
FALLBACK_COLOR = (120, 120, 120)


def build(work_dir: Path, routes: dict[str, str]) -> dict[str, Path]:
    input_dir = work_dir / "input"
    work_out = work_dir / "work"
    input_dir.mkdir(parents=True, exist_ok=True)
    work_out.mkdir(parents=True, exist_ok=True)

    shot_id = "shot_001"
    shot_root = work_out / shot_id
    keyposes_dir = shot_root / "keyposes"
    refined_dir = shot_root / "refined"
    ref_crops_dir = shot_root / "reference_crops"
    for d in (keyposes_dir, refined_dir, ref_crops_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1) Rough key pose PNG (shared by every character in the frame).
    kp_path = keyposes_dir / KEYPOSE_FILENAME
    _draw_stick_figure(kp_path)

    # 2) Per-identity reference crops (color + line-art placeholder).
    matches: list[dict] = []
    for identity, route in routes.items():
        color = IDENTITY_COLORS.get(identity, FALLBACK_COLOR)
        color_crop = ref_crops_dir / f"{identity}_{KEYPOSE_ANGLE}.png"
        line_crop = ref_crops_dir / f"{identity}_{KEYPOSE_ANGLE}_lineart.png"
        _draw_color_reference(color_crop, identity, color)
        # Line-art variant: same silhouette in pure black (not actually
        # consumed by the default DWPose / LineArt-fallback graphs; emitted
        # here so Node 6's contract stays intact).
        _draw_color_reference(line_crop, identity, (0, 0, 0))
        matches.append({
            "identity": identity,
            "expectedPosition": "C",
            "boundingBox": [0, 0, CANVAS, CANVAS],
            "selectedAngle": KEYPOSE_ANGLE,
            "scoreBreakdown": {},
            "allScores": {},
            "referenceColorCropPath": str(color_crop),
            "referenceLineArtCropPath": str(line_crop),
        })

    # 3) Per-shot reference_map.json (Node 6's contract to Node 7).
    ref_map_path = shot_root / "reference_map.json"
    _write_json(ref_map_path, {
        "schemaVersion": 1,
        "shotId": shot_id,
        "sourceFramesDir": str(shot_root / "frames"),
        "keyPosesDir": str(keyposes_dir),
        "referenceCropsDir": str(ref_crops_dir),
        "lineArtMethod": "dog",
        "keyPoses": [{
            "keyPoseIndex": 0,
            "keyPoseFilename": KEYPOSE_FILENAME,
            "sourceFrame": 1,
            "matches": matches,
            "skipped": [],
        }],
    })

    # 4) Aggregate node6_result.json (Node 7's CLI entrypoint input).
    node6_result_path = work_out / "node6_result.json"
    _write_json(node6_result_path, {
        "schemaVersion": 1,
        "projectName": "smoke",
        "workDir": str(work_out),
        "shots": [{
            "shotId": shot_id,
            "keyPoseCount": 1,
            "detectionCount": len(matches),
            "skippedCount": 0,
            "referenceMapPath": str(ref_map_path),
            "angleHistogram": {KEYPOSE_ANGLE: len(matches)},
        }],
        "lineArtMethod": "dog",
    })

    # 5) queue.json (poseExtractor routing per character).
    queue_path = input_dir / "queue.json"
    _write_json(queue_path, {
        "schemaVersion": 1,
        "projectName": "smoke",
        "batchSize": 1,
        "totalShots": 1,
        "batchCount": 1,
        "batches": [[{
            "shotId": shot_id,
            "mp4Path": str(input_dir / "shot_001.mp4"),
            "durationFrames": 25,
            "durationSeconds": 1.0,
            "characters": [
                {
                    "identity": identity,
                    "sheetPath": str(input_dir / f"{identity}_sheet.png"),
                    "position": "C",
                    "poseExtractor": route,
                }
                for identity, route in routes.items()
            ],
        }]],
    })

    return {
        "work_dir": work_dir,
        "node6_result_path": node6_result_path,
        "queue_path": queue_path,
        "shot_root": shot_root,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Synthesize a Node 6 work dir for Node 7 smoke testing.",
    )
    ap.add_argument(
        "--work-dir",
        default="/workspace/smoke_workdir",
        help="Root of the scaffold (default: /workspace/smoke_workdir).",
    )
    ap.add_argument(
        "--routes",
        default="Bhim=dwpose,Jaggu=lineart-fallback",
        help=(
            "Comma-separated 'Identity=route' pairs. Routes: 'dwpose' or "
            "'lineart-fallback'. Default exercises BOTH routes — Bhim "
            "(humanoid biped silhouette) goes through dwpose, Jaggu "
            "(quadruped) goes through lineart-fallback. Both routes are "
            "live-verified on RunPod as of 2026-04-25. If you're on a "
            "fresh pod and DWPose's onnx weights haven't auto-downloaded "
            "yet, the first dwpose run will pause to fetch them — "
            "subsequent runs are fast. Override with "
            "'Bhim=lineart-fallback,Jaggu=lineart-fallback' to skip "
            "DWPose entirely (e.g. on a restricted-network pod)."
        ),
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Wipe --work-dir if it exists (default: refuse to overwrite).",
    )
    args = ap.parse_args(argv)

    work_dir = Path(args.work_dir).resolve()
    if work_dir.exists():
        if not args.force:
            print(
                f"[smoke] refusing to overwrite existing {work_dir}. "
                "Pass --force to wipe it.",
                file=sys.stderr,
            )
            return 1
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    routes = _parse_routes(args.routes)

    paths = build(work_dir, routes)

    print("[smoke] scaffold written.")
    print(f"[smoke]   work_dir         = {paths['work_dir']}")
    print(f"[smoke]   node6_result.json = {paths['node6_result_path']}")
    print(f"[smoke]   queue.json       = {paths['queue_path']}")
    print(f"[smoke]   shot root        = {paths['shot_root']}")
    print()
    print("[smoke] Next: run Node 7 live against this scaffold:")
    print()
    print("    python3 run_node7.py \\")
    print(f"        --node6-result {paths['node6_result_path']} \\")
    print(f"        --queue        {paths['queue_path']}")
    print()
    print("[smoke] Or --dry-run for a no-ComfyUI sanity check first:")
    print()
    print("    python3 run_node7.py --dry-run \\")
    print(f"        --node6-result {paths['node6_result_path']} \\")
    print(f"        --queue        {paths['queue_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
