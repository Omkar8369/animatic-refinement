"""End-to-end smoke test: (synthetic Node 7 output) -> Node 8.

Not a pytest file (leading underscore). Exercised manually after each
Node 8 ship to verify the full CLI chain works in the embedded-Python
environment the user will actually use, not just against minified
in-memory test fixtures.

Differs from `tests/test_node8.py::TestCli` in three ways:

1. **Realistic dimensions.** 1280x720 rough keypose (typical animatic
   res), 512x512 refined PNGs (Node 7's actual output size), realistic
   per-character bboxes (Bhim on left half, Jaggu on right half) so the
   resulting composite shows both characters at proper positions, NOT
   degenerate full-canvas overlap.

2. **Subprocess invocation of run_node8.py.** Spawns the real CLI as
   the operator would, picking up sys.path + argparse + exit-code
   plumbing rather than calling compose_for_queue() in-process.

3. **Visual artifact.** Leaves the composed PNG on disk in the
   working directory (passed via `--work-dir` if provided, otherwise
   under the system tmpdir) so the operator can open it and confirm
   the output is sensible.

Why we synthesize Node 7's output instead of running it: Node 7
needs ComfyUI + GPU + 5+ GB of model weights. Node 8 has zero GPU
dependency and reads only Node 7's manifests + refined PNGs. Forging
Node 7's output (humanoid silhouettes drawn with PIL, marked
`status="ok"`) gives Node 8 the same shape of input it would get
from a real pod run.

Run from repo root with the embedded Python:

    .../python_embeded/python.exe tests/_smoke_node8.py
    .../python_embeded/python.exe tests/_smoke_node8.py --work-dir <path>
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------
# Synthesis: 1280x720 rough keypose with two characters, 512x512
# refined silhouettes per character.
# ---------------------------------------------------------------

CANVAS_W, CANVAS_H = 1280, 720
REFINED_SIZE = 512
SHOT_ID = "shot_001"

# Realistic per-character bboxes within the 1280x720 frame.
# Both ~480px tall; Bhim on left third, Jaggu on right third.
BHIM_BBOX = [200, 150, 200, 480]   # x, y, w, h
JAGGU_BBOX = [880, 150, 200, 480]


def _draw_rough_frame(path: Path) -> None:
    """1280x720 rough animatic frame: two crude character silhouettes
    drawn at the bbox locations. Real animatics would be hand-sketched
    line art; for smoke purposes a filled grey blob exercises the
    substitute-rough fallback path realistically (its bbox region gets
    copied onto the canvas)."""
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Bhim region: dark grey humanoid blob
    bx, by, bw, bh = BHIM_BBOX
    draw.ellipse((bx + 70, by + 10, bx + 130, by + 90), fill=(80, 80, 80))
    draw.rectangle((bx + 60, by + 90, bx + 140, by + bh - 80), fill=(80, 80, 80))
    draw.rectangle((bx + 70, by + bh - 80, bx + 95, by + bh - 5), fill=(80, 80, 80))
    draw.rectangle((bx + 105, by + bh - 80, bx + 130, by + bh - 5), fill=(80, 80, 80))
    # Jaggu region: dark grey humanoid blob
    jx, jy, jw, jh = JAGGU_BBOX
    draw.ellipse((jx + 70, jy + 10, jx + 130, jy + 90), fill=(80, 80, 80))
    draw.rectangle((jx + 60, jy + 90, jx + 140, jy + jh - 80), fill=(80, 80, 80))
    draw.rectangle((jx + 70, jy + jh - 80, jx + 95, jy + jh - 5), fill=(80, 80, 80))
    draw.rectangle((jx + 105, jy + jh - 80, jx + 130, jy + jh - 5), fill=(80, 80, 80))
    img.save(path, "PNG")


def _draw_refined_humanoid(path: Path, ink: tuple[int, int, int]) -> None:
    """Refined character: 512x512 humanoid silhouette in `ink` color
    on white. Mimics Node 7's output shape (a clean line-art figure
    centered roughly in the canvas, with white margin around it)."""
    size = REFINED_SIZE
    img = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    cx = size // 2
    # Head circle
    draw.ellipse((cx - 50, 50, cx + 50, 150), fill=ink, outline=ink, width=4)
    # Torso
    draw.rectangle((cx - 40, 150, cx + 40, 360), fill=(255, 255, 255),
                   outline=ink, width=4)
    # Arms
    draw.line((cx - 40, 180, cx - 110, 320), fill=ink, width=8)
    draw.line((cx + 40, 180, cx + 110, 320), fill=ink, width=8)
    # Legs (lowest non-white near y = size - 20)
    draw.line((cx - 20, 360, cx - 40, size - 20), fill=ink, width=10)
    draw.line((cx + 20, 360, cx + 40, size - 20), fill=ink, width=10)
    img.save(path, "PNG")


def _build_fixture(work_dir: Path) -> dict[str, Path]:
    """Synthesize a complete Node 7-output-shaped fixture under
    `work_dir`. Returns key paths."""
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    shot_root = work_dir / SHOT_ID
    keyposes_dir = shot_root / "keyposes"
    refined_dir = shot_root / "refined"
    for d in (keyposes_dir, refined_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1) Rough key-pose at sourceFrame=1 (canvas dim source for Node 8)
    keypose_path = keyposes_dir / "frame_0001.png"
    _draw_rough_frame(keypose_path)

    # 2) Refined PNGs (one per character) -- realistic Node 7 output
    bhim_refined = refined_dir / "000_Bhim.png"
    jaggu_refined = refined_dir / "000_Jaggu.png"
    _draw_refined_humanoid(bhim_refined, ink=(0, 0, 0))
    _draw_refined_humanoid(jaggu_refined, ink=(0, 0, 0))

    # 3) Per-shot refined_map.json (Node 7's contract to Node 8)
    refined_map = {
        "schemaVersion": 1,
        "shotId": SHOT_ID,
        "refinedDir": str(refined_dir),
        "generations": [
            {
                "identity": "Bhim",
                "keyPoseIndex": 0,
                "sourceFrame": 1,
                "selectedAngle": "front",
                "poseExtractor": "dwpose",
                "seed": 1109949314,
                "refinedPath": str(bhim_refined),
                "boundingBox": list(BHIM_BBOX),
                "status": "ok",
                "errorMessage": "",
                "cnStrengths": {"dwposeControlnet": 0.75, "ipAdapter": 0.8},
            },
            {
                "identity": "Jaggu",
                "keyPoseIndex": 0,
                "sourceFrame": 1,
                "selectedAngle": "front",
                "poseExtractor": "lineart-fallback",
                "seed": 1716951504,
                "refinedPath": str(jaggu_refined),
                "boundingBox": list(JAGGU_BBOX),
                "status": "ok",
                "errorMessage": "",
                "cnStrengths": {
                    "lineartControlnet": 0.6,
                    "scribbleControlnet": 0.6,
                    "ipAdapter": 0.8,
                },
            },
        ],
    }
    refined_map_path = shot_root / "refined_map.json"
    refined_map_path.write_text(json.dumps(refined_map, indent=2))

    # 4) Aggregate node7_result.json
    n7_result = {
        "schemaVersion": 1,
        "projectName": "smoke8",
        "workDir": str(work_dir),
        "comfyUIUrl": "http://127.0.0.1:8188",
        "dryRun": False,
        "refinedAt": "2026-04-25T06:30:00+00:00",
        "shots": [{
            "shotId": SHOT_ID,
            "keyPoseCount": 1,
            "generatedCount": 2,
            "skippedCount": 0,
            "errorCount": 0,
            "refinedMapPath": str(refined_map_path),
        }],
    }
    n7_path = work_dir / "node7_result.json"
    n7_path.write_text(json.dumps(n7_result, indent=2))

    return {
        "work_dir": work_dir,
        "shot_root": shot_root,
        "keypose_path": keypose_path,
        "bhim_refined": bhim_refined,
        "jaggu_refined": jaggu_refined,
        "refined_map_path": refined_map_path,
        "node7_result_path": n7_path,
    }


# ---------------------------------------------------------------
# Drive: run_node8.py via subprocess + validate outputs
# ---------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="End-to-end Node 8 smoke (synthetic Node 7 output -> Node 8 CLI)."
    )
    ap.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Where to put the synthesized work dir (and where the "
            "composite PNG will land). Default: a fresh tmpdir."
        ),
    )
    ap.add_argument(
        "--keep",
        action="store_true",
        help="Don't delete the work dir after a successful run.",
    )
    args = ap.parse_args(argv)

    if args.work_dir:
        work_dir = args.work_dir.resolve()
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="smoke8_")) / "work"

    print(f"[smoke8] building fixture at {work_dir}")
    paths = _build_fixture(work_dir)

    cli_path = REPO_ROOT / "run_node8.py"
    cmd = [sys.executable, str(cli_path),
           "--node7-result", str(paths["node7_result_path"])]
    print(f"[smoke8] invoking: {' '.join(cmd)}")

    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(f"[smoke8] CLI exit={proc.returncode}")
    if proc.stdout:
        print(f"[smoke8] CLI stdout: {proc.stdout.strip()}")
    if proc.stderr:
        print(f"[smoke8] CLI stderr: {proc.stderr.strip()}")
    assert proc.returncode == 0, "Node 8 CLI did not exit 0"

    # ---------- Validate aggregate node8_result.json ----------
    n8_path = work_dir / "node8_result.json"
    assert n8_path.is_file(), f"Missing aggregate {n8_path}"
    n8 = json.loads(n8_path.read_text(encoding="utf-8"))
    assert n8["schemaVersion"] == 1
    assert n8["projectName"] == "smoke8"
    assert n8["background"] == "white"
    assert len(n8["shots"]) == 1
    shot0 = n8["shots"][0]
    assert shot0["shotId"] == SHOT_ID
    assert shot0["composedCount"] == 1
    assert shot0["substituteCount"] == 0, (
        f"Both characters had status=ok; expected 0 substitute-rough "
        f"events, got {shot0['substituteCount']}."
    )
    print(
        f"[smoke8] aggregate: composedCount={shot0['composedCount']} "
        f"substituteCount={shot0['substituteCount']}"
    )

    # ---------- Validate per-shot composed_map.json ----------
    cm_path = paths["shot_root"] / "composed_map.json"
    assert cm_path.is_file()
    cm = json.loads(cm_path.read_text(encoding="utf-8"))
    assert cm["shotId"] == SHOT_ID
    assert len(cm["keyPoses"]) == 1
    kp = cm["keyPoses"][0]
    assert kp["keyPoseIndex"] == 0
    assert kp["sourceFrame"] == 1
    assert len(kp["characters"]) == 2
    identities = {c["identity"] for c in kp["characters"]}
    assert identities == {"Bhim", "Jaggu"}
    for c in kp["characters"]:
        assert c["status"] == "ok"
        assert c["substitutedFromRough"] is False
    assert kp["warnings"] == []
    print(f"[smoke8] composed_map.json: 2 characters, no warnings")

    # ---------- Validate the composite PNG ----------
    composite_path = paths["shot_root"] / "composed" / "000_composite.png"
    assert composite_path.is_file(), f"Missing composite: {composite_path}"
    with Image.open(composite_path) as img:
        assert img.size == (CANVAS_W, CANVAS_H), (
            f"Composite dims {img.size} != source MP4 dims "
            f"({CANVAS_W}, {CANVAS_H})"
        )
        assert img.mode == "RGB"
        arr = np.asarray(img.convert("RGB"))

    # Locked decision #6: BnW only -- every pixel is 0 or 255
    unique_values = sorted(np.unique(arr).tolist())
    assert set(unique_values).issubset({0, 255}), (
        f"BnW output must contain only 0 and 255; got {unique_values}"
    )
    print(f"[smoke8] composite is BnW-only: unique values = {unique_values}")

    # Both bbox regions must contain non-white (character) pixels
    bx, by, bw, bh = BHIM_BBOX
    bhim_region = arr[by:by + bh, bx:bx + bw]
    bhim_nonwhite = int(((bhim_region == 0).all(axis=2)).sum())
    assert bhim_nonwhite > 100, (
        f"Bhim's bbox region should contain character ink "
        f"(>100 black pixels); got {bhim_nonwhite}"
    )
    jx, jy, jw, jh = JAGGU_BBOX
    jaggu_region = arr[jy:jy + jh, jx:jx + jw]
    jaggu_nonwhite = int(((jaggu_region == 0).all(axis=2)).sum())
    assert jaggu_nonwhite > 100, (
        f"Jaggu's bbox region should contain character ink "
        f"(>100 black pixels); got {jaggu_nonwhite}"
    )
    print(
        f"[smoke8] character ink present: Bhim={bhim_nonwhite} px, "
        f"Jaggu={jaggu_nonwhite} px"
    )

    # The OUTSIDE of both bboxes (far-away corners) must be white
    # (proves the canvas background didn't get clobbered).
    assert (arr[5, 5] == [255, 255, 255]).all()
    assert (arr[CANVAS_H - 5, CANVAS_W - 5] == [255, 255, 255]).all()
    print(f"[smoke8] background is white in distant corners")

    # Locked decision #2: feet land near bbox.bottomY, NOT in middle.
    # Find lowest non-white row inside Bhim's bbox column-band -- it
    # should be within ±5 px of bbox.bottomY (LANCZOS edge tolerance
    # at 1280x720 + the substitute-rough rough-pixel residue).
    bhim_col_band = arr[:, bx:bx + bw]
    nonwhite_rows = np.flatnonzero(
        (bhim_col_band == 0).all(axis=2).any(axis=1)
    )
    if len(nonwhite_rows) > 0:
        lowest_y = int(nonwhite_rows[-1])
        bbox_bottom = by + bh - 1
        assert abs(lowest_y - bbox_bottom) <= 5, (
            f"Bhim's feet at row {lowest_y} should be near "
            f"bbox.bottomY={bbox_bottom} (LANCZOS tolerance ±5)"
        )
        bbox_middle = by + bh // 2
        assert lowest_y > bbox_middle, (
            f"Bhim's feet at row {lowest_y} should be below bbox "
            f"middle {bbox_middle} (stretch-to-fit regression check)"
        )
        print(
            f"[smoke8] Bhim feet-pinned: lowest_y={lowest_y} "
            f"(bbox.bottomY={bbox_bottom}, middle={bbox_middle})"
        )

    print(f"\n[smoke8] OK -- composite at: {composite_path}")
    print(f"[smoke8] artifacts in: {work_dir}")

    if not args.keep and not args.work_dir:
        # Only auto-clean tmpdirs we created; never auto-delete a
        # user-provided --work-dir.
        try:
            shutil.rmtree(work_dir.parent)
            print(f"[smoke8] cleaned up tmpdir {work_dir.parent}")
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
