"""End-to-end smoke test: (synthetic Node 9 output) -> Node 10.

Not a pytest file (leading underscore). Exercised manually after
each Node 10 ship to verify the full CLI chain works in the
embedded-Python environment the user will actually use, not just
against minified in-memory test fixtures.

Differs from `tests/test_node10.py::TestCli` in three ways:

1. **Realistic dimensions + frame count.** 1280x720 timed frames,
   25 frames per shot (= 1 second @ 25 FPS) -- matches what a real
   client shot would produce. Test-suite fixtures use 32x32 5-frame
   shots to keep ffmpeg invocations fast.

2. **Visible motion.** Each frame contains a moving black square +
   stripe so the resulting MP4 shows a clear left-to-right slide
   when played. The operator can open the output file and confirm
   the deliverable looks sensible.

3. **Subprocess invocation of run_node10.py.** Spawns the real CLI
   as the operator would, picking up sys.path + argparse + exit-code
   plumbing rather than calling encode_for_queue() in-process.

Why we synthesize Node 9's output instead of running it: the upstream
chain (Nodes 7 + 8 + 9) needs ComfyUI + GPU + a Node 6 work dir to
produce realistic data. Node 10 reads only Node 9's manifests +
timed/ PNGs. Forging Node 9's output (a 25-frame slide animation
drawn with PIL) gives Node 10 the same shape of input it would get
from a real pod-end-to-end run.

Run from repo root with the embedded Python:

    .../python_embeded/python.exe tests/_smoke_node10.py
    .../python_embeded/python.exe tests/_smoke_node10.py --work-dir <path>

The resulting `<work-dir>/output/shot_001_refined.mp4` is playable
in any standard video player (VLC, QuickTime, browser).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------
# Synthesis: 25-frame slide animation at 1280x720
# ---------------------------------------------------------------

CANVAS_W, CANVAS_H = 1280, 720
SHOT_ID = "shot_001"
TOTAL_FRAMES = 25  # = 1 second @ 25 FPS

# Black square + horizontal stripe slide left-to-right across the
# frame -- visible motion when played as MP4.
SQUARE_SIZE = 80
SQUARE_Y = 250
SLIDE_START_X = 100
SLIDE_END_X = CANVAS_W - SQUARE_SIZE - 100


def _draw_timed_frame(path: Path, frame_idx: int) -> None:
    """One 1280x720 timed frame in the slide sequence.

    `frame_idx` is 1-based. The square slides linearly from
    SLIDE_START_X (frame 1) to SLIDE_END_X (frame TOTAL_FRAMES).
    """
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Linear interpolation for x position
    progress = (frame_idx - 1) / max(1, TOTAL_FRAMES - 1)
    x = int(SLIDE_START_X + progress * (SLIDE_END_X - SLIDE_START_X))
    # Black square
    draw.rectangle(
        (x, SQUARE_Y, x + SQUARE_SIZE, SQUARE_Y + SQUARE_SIZE),
        fill=(0, 0, 0),
    )
    # Mid-canvas horizontal stripe (so vertical position is clear)
    draw.rectangle(
        (50, CANVAS_H // 2 - 5, CANVAS_W - 50, CANVAS_H // 2 + 5),
        fill=(0, 0, 0),
    )
    # Frame counter in top-right (so playback shows frame progression)
    draw.text(
        (CANVAS_W - 200, 30),
        f"frame {frame_idx}/{TOTAL_FRAMES}",
        fill=(0, 0, 0),
    )
    img.save(path, "PNG")


def _build_fixture(work_dir: Path) -> dict[str, Path]:
    """Synthesize a complete Node 9-output-shaped fixture under
    `work_dir`. Returns key paths."""
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    shot_root = work_dir / SHOT_ID
    timed_dir = shot_root / "timed"
    timed_dir.mkdir(parents=True, exist_ok=True)

    # 1) Timed PNG sequence
    for i in range(1, TOTAL_FRAMES + 1):
        _draw_timed_frame(timed_dir / f"frame_{i:04d}.png", i)

    # 2) timed_map.json (Node 9's contract)
    timed_map = {
        "schemaVersion": 1,
        "shotId": SHOT_ID,
        "timedDir": str(timed_dir),
        "totalFrames": TOTAL_FRAMES,
        "frames": [
            {
                "frameIndex": i,
                "sourceKeyPoseIndex": 0,
                "offset": [0, 0],
                "composedSourcePath": str(shot_root / "composed" / "000_composite.png"),
                "timedPath": str(timed_dir / f"frame_{i:04d}.png"),
                "isAnchor": (i == 1),
            }
            for i in range(1, TOTAL_FRAMES + 1)
        ],
    }
    timed_map_path = shot_root / "timed_map.json"
    timed_map_path.write_text(json.dumps(timed_map, indent=2))

    # 3) node9_result.json (aggregate)
    n9 = {
        "schemaVersion": 1,
        "projectName": "smoke10",
        "workDir": str(work_dir),
        "reconstructedAt": "2026-04-25T08:00:00+00:00",
        "shots": [{
            "shotId": SHOT_ID,
            "totalFrames": TOTAL_FRAMES,
            "keyPoseCount": 1,
            "anchorCount": 1,
            "heldCount": TOTAL_FRAMES - 1,
            "timedMapPath": str(timed_map_path),
        }],
    }
    n9_path = work_dir / "node9_result.json"
    n9_path.write_text(json.dumps(n9, indent=2))

    return {
        "work_dir": work_dir,
        "shot_root": shot_root,
        "timed_dir": timed_dir,
        "timed_map_path": timed_map_path,
        "node9_result_path": n9_path,
    }


# ---------------------------------------------------------------
# Drive: run_node10.py via subprocess + validate outputs
# ---------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "End-to-end Node 10 smoke (synthetic Node 9 output -> Node 10 CLI)."
        ),
    )
    ap.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Where to put the synthesized work dir (and where the "
            "MP4 deliverable will land). Default: a fresh tmpdir."
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
        work_dir = Path(tempfile.mkdtemp(prefix="smoke10_")) / "work"

    print(f"[smoke10] building fixture at {work_dir}")
    paths = _build_fixture(work_dir)

    cli_path = REPO_ROOT / "run_node10.py"
    cmd = [sys.executable, str(cli_path),
           "--node9-result", str(paths["node9_result_path"])]
    print(f"[smoke10] invoking: {' '.join(cmd)}")

    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(f"[smoke10] CLI exit={proc.returncode}")
    if proc.stdout:
        print(f"[smoke10] CLI stdout: {proc.stdout.strip()}")
    if proc.stderr:
        print(f"[smoke10] CLI stderr: {proc.stderr.strip()}")
    assert proc.returncode == 0, "Node 10 CLI did not exit 0"

    # ---------- Validate aggregate node10_result.json ----------
    n10_path = work_dir / "node10_result.json"
    assert n10_path.is_file(), f"Missing aggregate {n10_path}"
    n10 = json.loads(n10_path.read_text(encoding="utf-8"))
    assert n10["schemaVersion"] == 1
    assert n10["projectName"] == "smoke10"
    assert n10["crf"] == 18
    assert len(n10["shots"]) == 1
    shot0 = n10["shots"][0]
    assert shot0["shotId"] == SHOT_ID
    assert shot0["codec"] == "h264"
    assert shot0["fps"] == 25
    assert shot0["fileSizeBytes"] > 0
    print(
        f"[smoke10] aggregate: codec={shot0['codec']} fps={shot0['fps']} "
        f"frames={shot0['frameCount']} duration={shot0['durationSeconds']:.3f}s "
        f"size={shot0['fileSizeBytes']:,} bytes"
    )

    # ---------- Validate output MP4 on disk ----------
    output_dir = work_dir / "output"
    assert output_dir.is_dir(), f"Missing output dir {output_dir}"
    expected_mp4 = output_dir / f"{SHOT_ID}_refined.mp4"
    assert expected_mp4.is_file(), f"Missing deliverable {expected_mp4}"
    assert expected_mp4.stat().st_size > 0, "MP4 is zero bytes"
    assert str(expected_mp4) == shot0["outputPath"], (
        "outputPath in aggregate doesn't match actual file"
    )
    print(f"[smoke10] MP4 deliverable: {expected_mp4}")

    # ---------- Re-probe the output via imageio_ffmpeg ----------
    n_frames, n_secs = imageio_ffmpeg.count_frames_and_secs(str(expected_mp4))
    assert abs(n_frames - TOTAL_FRAMES) <= 1, (
        f"MP4 has {n_frames} frame(s) but expected {TOTAL_FRAMES}"
    )
    expected_secs = TOTAL_FRAMES / 25
    assert abs(n_secs - expected_secs) < 0.5, (
        f"MP4 duration {n_secs:.3f}s but expected ~{expected_secs:.3f}s "
        f"({TOTAL_FRAMES} frames @ 25 FPS)"
    )
    print(
        f"[smoke10] independent re-probe: {n_frames} frames / {n_secs:.3f}s "
        f"(expected {TOTAL_FRAMES}/{expected_secs:.3f}s)"
    )

    # ---------- Validate Node 10 did NOT delete upstream artifacts ----------
    timed_pngs = sorted(p.name for p in paths["timed_dir"].glob("frame_*.png"))
    assert len(timed_pngs) == TOTAL_FRAMES, (
        f"Node 10 deleted upstream timed PNGs! Expected {TOTAL_FRAMES}, "
        f"found {len(timed_pngs)}"
    )
    assert paths["timed_map_path"].is_file(), "Node 10 deleted timed_map.json!"
    print(
        f"[smoke10] upstream artifacts preserved: {len(timed_pngs)} timed PNGs "
        "+ timed_map.json all present"
    )

    # ---------- Decode one frame from the MP4 and confirm dimensions ----------
    # Use imageio_ffmpeg.read_frames to pull the first frame's metadata.
    reader = imageio_ffmpeg.read_frames(str(expected_mp4))
    meta = next(reader)  # first yielded item is metadata dict
    assert meta["size"] == (CANVAS_W, CANVAS_H), (
        f"MP4 frame dims {meta['size']} != source ({CANVAS_W}, {CANVAS_H})"
    )
    # Drain at least one frame so the reader doesn't leak the subprocess
    try:
        next(reader)
    except StopIteration:
        pass
    print(
        f"[smoke10] MP4 frame dims: {meta['size'][0]}x{meta['size'][1]} "
        f"(matches source {CANVAS_W}x{CANVAS_H})"
    )

    # ---------- Sanity: file size is reasonable for BnW line art ----------
    # 25 frames of 1280x720 BnW line art at CRF 18 should be well under
    # 1 MB (BnW with large white regions compresses very well).
    file_size_kb = expected_mp4.stat().st_size / 1024
    assert file_size_kb < 1000, (
        f"MP4 file size {file_size_kb:.1f} KB seems too large for "
        f"{TOTAL_FRAMES} frames of BnW line art"
    )
    print(
        f"[smoke10] file size sanity: {file_size_kb:.1f} KB "
        f"({file_size_kb / TOTAL_FRAMES:.1f} KB/frame) -- compresses well"
    )

    print(f"\n[smoke10] OK -- MP4 deliverable at: {expected_mp4}")
    print(f"[smoke10] artifacts in: {work_dir}")
    print(f"[smoke10] open the MP4 in any standard video player to watch")
    print(f"[smoke10] the slide animation (black square moving left-to-right).")

    if not args.keep and not args.work_dir:
        try:
            shutil.rmtree(work_dir.parent)
            print(f"[smoke10] cleaned up tmpdir {work_dir.parent}")
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
