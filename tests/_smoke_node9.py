"""End-to-end smoke test: (synthetic Node 8 output) -> Node 9.

Not a pytest file (leading underscore). Exercised manually after
each Node 9 ship to verify the full CLI chain works in the
embedded-Python environment the user will actually use, not just
against minified in-memory test fixtures.

Differs from `tests/test_node9.py::TestCli` in three ways:

1. **Realistic dimensions.** 1280x720 composites (matches the Node 8
   smoke output), 2 key poses with 4 held frames each (~9 total
   frames per shot), realistic per-held-frame `(dy, dx)` offsets
   (including a slide ramp from 0 to +30 px to exercise the
   translate path properly).

2. **Subprocess invocation of run_node9.py.** Spawns the real CLI
   as the operator would, picking up sys.path + argparse + exit-code
   plumbing rather than calling reconstruct_timing_for_queue()
   in-process.

3. **Visual artifacts.** Leaves the full `timed/` PNG sequence on
   disk (passed via `--work-dir` if provided, otherwise under the
   system tmpdir) so the operator can browse the per-frame outputs
   and confirm the timing reconstruction is sensible.

Why we synthesize Node 8's output instead of running it: Node 8 +
Node 7 need a real Node 6 work dir + ComfyUI + GPU. Node 9 reads
only Node 8's manifests + composite PNGs + Node 4's keypose_map.json
(Node 9 chases it from shot root). Forging Node 8's output (two
distinctively-marked composites drawn with PIL) gives Node 9 the
same shape of input it would get from a real pod run.

Run from repo root with the embedded Python:

    .../python_embeded/python.exe tests/_smoke_node9.py
    .../python_embeded/python.exe tests/_smoke_node9.py --work-dir <path>
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
# Synthesis: 1280x720 composites with distinctive markers per
# key pose, plus realistic held-frame offsets.
# ---------------------------------------------------------------

CANVAS_W, CANVAS_H = 1280, 720
SHOT_ID = "shot_001"

# Two key poses. Anchor frames at sourceFrame=1 and sourceFrame=6.
# Each has 4 held frames -> totalFrames = 1 + 4 + 1 + 4 = 10.
KEYPOSE_COUNT = 2
HELD_PER_KEYPOSE = [4, 4]

# Held-frame offsets: KP0 stays still then slides right; KP1 stays
# still then slides down-left.
HELD_OFFSETS = [
    [[0, 0], [0, 10], [0, 20], [0, 30]],   # KP0 anchor=frame1, helds=frames2..5
    [[0, 0], [5, 0], [10, -5], [15, -10]],  # KP1 anchor=frame6, helds=frames7..10
]

# Per-key-pose marker colors so the operator can visually verify
# which composite each timed frame came from.
KP_COLORS = [
    (220, 60, 60),   # KP0: red
    (60, 160, 80),   # KP1: green
]


def _draw_composite(path: Path, marker_color: tuple[int, int, int],
                    label: str) -> None:
    """A 1280x720 composite PNG with:
    - A distinctive colored block in the upper-left so post-translate
      tests can locate it.
    - A horizontal stripe across the middle so vertical translates
      are visually obvious.
    - A text label so the operator can tell which composite is which.
    """
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Upper-left marker block (50-150 horizontally, 50-150 vertically)
    draw.rectangle((50, 50, 150, 150), fill=marker_color)
    # Mid-canvas horizontal stripe (so vertical translates show)
    draw.rectangle((100, 350, CANVAS_W - 100, 370), fill=marker_color)
    # Vertical stripe near left edge (so horizontal translates show)
    draw.rectangle((200, 100, 220, CANVAS_H - 100), fill=marker_color)
    # Label near top-right
    draw.text((CANVAS_W - 200, 30), label, fill=(0, 0, 0))
    img.save(path, "PNG")


def _build_fixture(work_dir: Path) -> dict[str, Path]:
    """Synthesize a complete Node 8-output-shaped fixture under
    `work_dir`. Returns key paths."""
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    shot_root = work_dir / SHOT_ID
    composed_dir = shot_root / "composed"
    composed_dir.mkdir(parents=True, exist_ok=True)

    # Build keyPoses, anchors, heldFrames
    composed_keyposes = []
    keypose_records = []
    next_frame = 1
    composite_paths = []
    for k in range(KEYPOSE_COUNT):
        anchor_frame = next_frame
        held_offsets_for_k = HELD_OFFSETS[k]
        held_count = HELD_PER_KEYPOSE[k]
        # Build heldFrames list. The first held offset corresponds to
        # the SECOND timeline frame (anchor+1); offsets array has
        # `held_count` entries that match anchor+1..anchor+held_count.
        held_frames = []
        for h in range(held_count):
            held_frames.append({
                "frame": anchor_frame + 1 + h,
                "offset": list(held_offsets_for_k[h]),
            })
        next_frame = anchor_frame + 1 + held_count

        keypose_records.append({
            "keyPoseIndex": k,
            "sourceFrame": anchor_frame,
            "keyPoseFilename": f"frame_{anchor_frame:04d}.png",
            "heldFrames": held_frames,
        })

        composite_path = composed_dir / f"{k:03d}_composite.png"
        _draw_composite(composite_path, KP_COLORS[k], f"KP{k}")
        composite_paths.append(composite_path)
        composed_keyposes.append({
            "keyPoseIndex": k,
            "sourceFrame": anchor_frame,
            "composedPath": str(composite_path),
            "characters": [],
            "warnings": [],
        })

    total_frames = next_frame - 1

    # composed_map.json (Node 8's contract)
    composed_map = {
        "schemaVersion": 1,
        "shotId": SHOT_ID,
        "composedDir": str(composed_dir),
        "keyPoses": composed_keyposes,
    }
    composed_map_path = shot_root / "composed_map.json"
    composed_map_path.write_text(json.dumps(composed_map, indent=2))

    # keypose_map.json (Node 4's output, sibling)
    keypose_map = {
        "schemaVersion": 1,
        "shotId": SHOT_ID,
        "totalFrames": total_frames,
        "sourceFramesDir": str(shot_root / "frames"),
        "keyPosesDir": str(shot_root / "keyposes"),
        "threshold": 8.0,
        "maxEdge": 128,
        "keyPoses": keypose_records,
    }
    keypose_map_path = shot_root / "keypose_map.json"
    keypose_map_path.write_text(json.dumps(keypose_map, indent=2))

    # node8_result.json
    n8 = {
        "schemaVersion": 1,
        "projectName": "smoke9",
        "workDir": str(work_dir),
        "background": "white",
        "composedAt": "2026-04-25T07:00:00+00:00",
        "shots": [{
            "shotId": SHOT_ID,
            "keyPoseCount": KEYPOSE_COUNT,
            "composedCount": KEYPOSE_COUNT,
            "substituteCount": 0,
            "composedMapPath": str(composed_map_path),
        }],
    }
    n8_path = work_dir / "node8_result.json"
    n8_path.write_text(json.dumps(n8, indent=2))

    return {
        "work_dir": work_dir,
        "shot_root": shot_root,
        "composed_dir": composed_dir,
        "composite_paths": composite_paths,
        "composed_map_path": composed_map_path,
        "keypose_map_path": keypose_map_path,
        "node8_result_path": n8_path,
        "total_frames": total_frames,
    }


# ---------------------------------------------------------------
# Drive: run_node9.py via subprocess + validate outputs
# ---------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "End-to-end Node 9 smoke (synthetic Node 8 output -> Node 9 CLI)."
        ),
    )
    ap.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Where to put the synthesized work dir (and where the "
            "timed/ frames will land). Default: a fresh tmpdir."
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
        work_dir = Path(tempfile.mkdtemp(prefix="smoke9_")) / "work"

    print(f"[smoke9] building fixture at {work_dir}")
    paths = _build_fixture(work_dir)

    cli_path = REPO_ROOT / "run_node9.py"
    cmd = [sys.executable, str(cli_path),
           "--node8-result", str(paths["node8_result_path"])]
    print(f"[smoke9] invoking: {' '.join(cmd)}")

    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(f"[smoke9] CLI exit={proc.returncode}")
    if proc.stdout:
        print(f"[smoke9] CLI stdout: {proc.stdout.strip()}")
    if proc.stderr:
        print(f"[smoke9] CLI stderr: {proc.stderr.strip()}")
    assert proc.returncode == 0, "Node 9 CLI did not exit 0"

    # ---------- Validate aggregate node9_result.json ----------
    n9_path = work_dir / "node9_result.json"
    assert n9_path.is_file(), f"Missing aggregate {n9_path}"
    n9 = json.loads(n9_path.read_text(encoding="utf-8"))
    assert n9["schemaVersion"] == 1
    assert n9["projectName"] == "smoke9"
    assert len(n9["shots"]) == 1
    shot0 = n9["shots"][0]
    assert shot0["shotId"] == SHOT_ID
    assert shot0["totalFrames"] == paths["total_frames"]
    assert shot0["keyPoseCount"] == KEYPOSE_COUNT
    assert shot0["anchorCount"] == KEYPOSE_COUNT
    assert shot0["heldCount"] == sum(HELD_PER_KEYPOSE)
    print(
        f"[smoke9] aggregate: totalFrames={shot0['totalFrames']} "
        f"anchors={shot0['anchorCount']} held={shot0['heldCount']}"
    )

    # ---------- Validate per-shot timed_map.json ----------
    tm_path = paths["shot_root"] / "timed_map.json"
    assert tm_path.is_file()
    tm = json.loads(tm_path.read_text(encoding="utf-8"))
    assert tm["shotId"] == SHOT_ID
    assert tm["totalFrames"] == paths["total_frames"]
    assert len(tm["frames"]) == paths["total_frames"]
    # Anchor frames in correct positions
    anchors = [f for f in tm["frames"] if f["isAnchor"]]
    assert [a["frameIndex"] for a in anchors] == [1, 6]
    print(f"[smoke9] timed_map.json: {len(tm['frames'])} frames "
          f"({len(anchors)} anchors at {[a['frameIndex'] for a in anchors]})")

    # ---------- Validate every PNG file exists ----------
    timed_dir = paths["shot_root"] / "timed"
    expected_files = [f"frame_{i:04d}.png" for i in range(1, paths["total_frames"] + 1)]
    actual_files = sorted(p.name for p in timed_dir.glob("frame_*.png"))
    assert actual_files == expected_files, (
        f"timed/ files mismatch: expected {expected_files}, got {actual_files}"
    )
    print(f"[smoke9] all {len(actual_files)} timed PNGs present")

    # ---------- Locked decision #1 (anchor): frame 1 should be
    # bit-identical to composite 0 ----------
    anchor1_arr = np.asarray(
        Image.open(timed_dir / "frame_0001.png").convert("RGB")
    )
    composite0_arr = np.asarray(
        Image.open(paths["composite_paths"][0]).convert("RGB")
    )
    assert np.array_equal(anchor1_arr, composite0_arr), (
        "Anchor frame 1 should be pixel-identical to composite 0 "
        "(offset=[0,0] short-circuits translate)"
    )
    anchor6_arr = np.asarray(
        Image.open(timed_dir / "frame_0006.png").convert("RGB")
    )
    composite1_arr = np.asarray(
        Image.open(paths["composite_paths"][1]).convert("RGB")
    )
    assert np.array_equal(anchor6_arr, composite1_arr), (
        "Anchor frame 6 should be pixel-identical to composite 1"
    )
    print(f"[smoke9] anchor frames bit-identical to composites")

    # ---------- Locked decision #1 (held, zero offset): frame 2
    # should also equal composite 0 (held with offset [0, 0]) ----------
    held2_arr = np.asarray(
        Image.open(timed_dir / "frame_0002.png").convert("RGB")
    )
    assert np.array_equal(held2_arr, composite0_arr), (
        "Held frame 2 with offset [0, 0] should equal composite 0"
    )
    print(f"[smoke9] zero-offset held frame matches its anchor")

    # ---------- Locked decision #1 (held, positive dx): frame 5
    # has offset [0, 30] -> upper-left red block at (50,50)-(150,150)
    # should land at (80,50)-(180,150) ----------
    held5_arr = np.asarray(
        Image.open(timed_dir / "frame_0005.png").convert("RGB")
    )
    # Original block center was at (100, 100); after dx=30 it's at (130, 100)
    assert (held5_arr[100, 130] == KP_COLORS[0]).all(), (
        f"Frame 5 (offset [0, 30]): KP0 marker should land at (130, 100); "
        f"got {tuple(held5_arr[100, 130])}"
    )
    # Original (100, 100) position is now exposed -> white (since the
    # original (70, 100) in source was also white)
    # The leftmost 30 pixels are the exposed strip
    assert (held5_arr[100, 5] == [255, 255, 255]).all(), (
        "Exposed strip on left should be white"
    )
    print(f"[smoke9] held frame 5 (offset [0, 30]) translated correctly + "
          f"exposed region is white")

    # ---------- Locked decision #1 (held, dy != 0): frame 10 has
    # offset [15, -10] -> KP1 marker at (50,50)-(150,150) shifts to
    # (40, 65)-(140, 165) ----------
    held10_arr = np.asarray(
        Image.open(timed_dir / "frame_0010.png").convert("RGB")
    )
    # Pixel (75, 50) in original (well inside the marker) lands at
    # (75 + dx, 50 + dy) = (65, 65)
    assert (held10_arr[65, 65] == KP_COLORS[1]).all(), (
        f"Frame 10 (offset [15, -10]): KP1 marker should be at (65, 65); "
        f"got {tuple(held10_arr[65, 65])}"
    )
    print(f"[smoke9] held frame 10 (offset [15, -10]) translated correctly")

    # ---------- Locked decision #4 (white background): every frame's
    # bottom-right corner is far from any drawn content -> must be
    # white ----------
    for fname in actual_files:
        arr = np.asarray(
            Image.open(timed_dir / fname).convert("RGB")
        )
        assert (arr[CANVAS_H - 5, CANVAS_W - 5] == [255, 255, 255]).all(), (
            f"{fname}: bottom-right corner should be white"
        )
    print(f"[smoke9] white background preserved in distant corners "
          f"of all {len(actual_files)} frames")

    # ---------- Output canvas dims = composite dims = source MP4 res ----
    for fname in actual_files:
        with Image.open(timed_dir / fname) as img:
            assert img.size == (CANVAS_W, CANVAS_H), (
                f"{fname}: dims {img.size} != composite ({CANVAS_W}, {CANVAS_H})"
            )
    print(f"[smoke9] all timed PNGs are {CANVAS_W}x{CANVAS_H} "
          f"(matches composite dims)")

    print(f"\n[smoke9] OK -- timed/ at: {timed_dir}")
    print(f"[smoke9] artifacts in: {work_dir}")

    if not args.keep and not args.work_dir:
        try:
            shutil.rmtree(work_dir.parent)
            print(f"[smoke9] cleaned up tmpdir {work_dir.parent}")
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
