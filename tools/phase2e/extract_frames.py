"""Extract per-character training frames from the shots marked in selections.json.

For each shot in the TAPPU and JETHALAL buckets of <selections>:
  * read the .mp4 from --shots-dir
  * compute 5 timestamps at 10%, 30%, 50%, 70%, 90% of duration
  * extract one frame at each as JPG q=92 (small enough for ~100s of frames,
    high enough for LoRA training)
  * save to <output-dir>/<CHARACTER>/<shot>_f01.jpg ... _f05.jpg

OTHER bucket is skipped (we extract those only when training those
characters later). SKIP is never extracted.

Resume-safe: skips files that already exist with non-zero size.

Run locally (no pod required).

Example (Windows / Git Bash):

  PYTHON="/c/Users/Omkar Hajare/Desktop/download/ComfyUI_windows_portable/python_embeded/python.exe"
  "$PYTHON" tools/phase2e/extract_frames.py \\
    --selections tools/phase2e/training_candidates/EP35/selections.json \\
    --shots-dir "/c/Users/Omkar Hajare/Downloads/New folder (1)/New folder" \\
    --output-dir tools/phase2e/training_candidates/EP35

Outputs into <output-dir>/TAPPU/ and <output-dir>/JETHALAL/.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import imageio_ffmpeg
except ImportError:
    print("ERROR: imageio_ffmpeg not installed.", file=sys.stderr)
    sys.exit(1)


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def get_duration_seconds(mp4_path: Path, ffmpeg_exe: str) -> float | None:
    r = subprocess.run(
        [ffmpeg_exe, "-i", str(mp4_path)],
        capture_output=True,
        text=True,
    )
    m = _DURATION_RE.search(r.stderr)
    if not m:
        return None
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def extract_jpg(
    mp4_path: Path,
    time_sec: float,
    out_path: Path,
    ffmpeg_exe: str,
    quality: int = 3,  # ffmpeg q:v 2=highest, 31=lowest; 3 ≈ q=92 in PIL terms
) -> bool:
    """Fast-seek + 1 frame, full source resolution, JPG."""
    r = subprocess.run(
        [
            ffmpeg_exe,
            "-y",
            "-loglevel", "error",
            "-ss", str(time_sec),
            "-i", str(mp4_path),
            "-frames:v", "1",
            "-q:v", str(quality),
            str(out_path),
        ],
        capture_output=True,
    )
    return r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


# Timestamps (as fractions of shot duration) at which to extract.
TIMESTAMP_FRACTIONS = [0.10, 0.30, 0.50, 0.70, 0.90]


def extract_shot_frames(
    mp4_path: Path,
    out_dir: Path,
    shot_name: str,
    ffmpeg_exe: str,
) -> tuple[int, int]:
    """Extract 5 frames from a shot. Returns (extracted_count, skipped_count)."""
    duration = get_duration_seconds(mp4_path, ffmpeg_exe)
    if duration is None or duration < 0.3:
        print(f"  {shot_name}: bad duration ({duration}), skip whole shot")
        return (0, 0)

    extracted = 0
    skipped = 0
    for idx, frac in enumerate(TIMESTAMP_FRACTIONS, start=1):
        ts = duration * frac
        # Don't seek past the very end (causes flaky behavior).
        ts = min(ts, max(0.0, duration - 0.05))
        out_jpg = out_dir / f"{shot_name}_f{idx:02d}.jpg"
        if out_jpg.exists() and out_jpg.stat().st_size > 0:
            skipped += 1
            continue
        if extract_jpg(mp4_path, ts, out_jpg, ffmpeg_exe):
            extracted += 1
        else:
            print(f"  {shot_name}: frame {idx} (t={ts:.2f}s) extraction failed")
    return (extracted, skipped)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--selections", required=True)
    parser.add_argument("--shots-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--buckets",
        nargs="+",
        default=["TAPPU", "JETHALAL"],
        help="character buckets to extract (default: TAPPU JETHALAL)",
    )
    args = parser.parse_args()

    selections_path = Path(args.selections)
    shots_dir = Path(args.shots_dir)
    out_dir = Path(args.output_dir)

    if not selections_path.is_file():
        print(f"ERROR: --selections file not found: {selections_path}", file=sys.stderr)
        sys.exit(2)
    if not shots_dir.is_dir():
        print(f"ERROR: --shots-dir not found: {shots_dir}", file=sys.stderr)
        sys.exit(2)

    selections = json.loads(selections_path.read_text())
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    print(f"ffmpeg: {ffmpeg_exe}")
    print(f"shots dir: {shots_dir}")
    print(f"output dir: {out_dir}")
    print(f"selections: {selections_path}")
    print()

    total_summary: dict[str, dict[str, int]] = {}

    for bucket in args.buckets:
        shot_names = selections.get(bucket, [])
        if not shot_names:
            print(f"== {bucket}: 0 shots in selections, skipping ==")
            continue

        bucket_dir = out_dir / bucket
        bucket_dir.mkdir(parents=True, exist_ok=True)
        print(f"== {bucket}: {len(shot_names)} shots -> {bucket_dir} ==")

        bucket_extracted = 0
        bucket_skipped = 0
        bucket_missing = 0
        for i, shot in enumerate(shot_names, start=1):
            mp4 = shots_dir / f"{shot}.mp4"
            if not mp4.is_file():
                print(f"  [{i}/{len(shot_names)}] {shot}: mp4 missing, skip")
                bucket_missing += 1
                continue
            ext, skp = extract_shot_frames(mp4, bucket_dir, shot, ffmpeg_exe)
            bucket_extracted += ext
            bucket_skipped += skp
            if ext > 0:
                print(f"  [{i}/{len(shot_names)}] {shot}: extracted {ext} (skipped {skp} existing)")
            elif skp > 0:
                print(f"  [{i}/{len(shot_names)}] {shot}: already complete ({skp} existing)")

        total_summary[bucket] = {
            "shots": len(shot_names),
            "extracted": bucket_extracted,
            "skipped_existing": bucket_skipped,
            "missing_mp4": bucket_missing,
        }
        print(
            f"  {bucket} subtotal: {bucket_extracted} new + "
            f"{bucket_skipped} existing = {bucket_extracted + bucket_skipped} frames "
            f"({bucket_missing} mp4s missing)"
        )
        print()

    print("=== Summary ===")
    for bucket, stats in total_summary.items():
        print(f"  {bucket}: {stats}")
    print()
    print("Next steps:")
    for bucket in args.buckets:
        bd = out_dir / bucket
        if bd.is_dir():
            n = len(list(bd.glob("*.jpg")))
            print(f"  Review {bd} in Windows Explorer ({n} jpgs)")
    print()
    print("  Use 'Large/Extra-large icons' view in Explorer.")
    print("  Delete frames where: character is blocked, off-screen, blurred,")
    print("  back-of-head, or duplicate of another keeper.")
    print("  Target ~60-80 per character.")


if __name__ == "__main__":
    main()
