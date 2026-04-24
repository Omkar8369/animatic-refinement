"""End-to-end smoke test: Node 2 -> Node 3 -> Node 4 -> Node 5 -> Node 6.

Not a pytest file (leading underscore). Exercised manually after each
Node 6 ship so the full CLI chain is verified against the actual
embedded-Python environment the user will use, not just against
in-memory PIL fixtures.

Run from repo root with the embedded Python:

    .../python_embeded/python.exe tests/_smoke_node6.py

Extends the Node 5 smoke fixture: same 2-character MP4 + same sheets,
but flips `conventions.angleOrderConfirmed` to True (Node 6 gates on
it) and then runs Node 6's CLI to produce reference_map.json +
node6_result.json + per-(identity, angle) color + line-art crops.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------
# Synthesize input: one shot MP4 with two moving characters
# ---------------------------------------------------------------

FRAME_W, FRAME_H = 128, 96
FPS = 25
SHOT_FRAMES = 30  # ~1.2s


def _make_line_art_frame(frame_idx: int) -> np.ndarray:
    """One 8-bit grayscale frame: white bg + two dark character blobs."""
    arr = np.full((FRAME_H, FRAME_W), 255, dtype=np.uint8)
    arr[30:70, 12:36] = 20
    rx = 92 + frame_idx // 5
    if rx + 24 < FRAME_W:
        arr[30:70, rx:rx + 24] = 20
    return arr


def _write_mp4(path: Path) -> None:
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        for i in range(SHOT_FRAMES):
            Image.fromarray(_make_line_art_frame(i), mode="L").save(
                tdp / f"f_{i:04d}.png"
            )
        cmd = [
            ffmpeg, "-y",
            "-framerate", str(FPS),
            "-i", str(tdp / "f_%04d.png"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "18",
            str(path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)


def _write_sheet(path: Path) -> None:
    """8-angle horizontal strip: 8 distinct alpha islands on transparent bg.

    Each of the 8 squares is a separate alpha island so
    `scipy.ndimage.label` on `alpha > 0` recovers exactly 8 bboxes.
    """
    sheet = np.zeros((64, 64 * 8, 4), dtype=np.uint8)
    colors = [
        (255, 100, 100), (255, 180, 80), (255, 240, 80), (100, 220, 100),
        (100, 200, 240), (120, 120, 255), (200, 100, 220), (240, 120, 160),
    ]
    for i, rgb in enumerate(colors):
        sheet[8:56, i * 64 + 8:i * 64 + 56, 0:3] = rgb
        sheet[8:56, i * 64 + 8:i * 64 + 56, 3] = 255
    Image.fromarray(sheet, mode="RGBA").save(path)


# ---------------------------------------------------------------
# Runner
# ---------------------------------------------------------------

def main() -> int:
    work_root = Path(tempfile.mkdtemp(prefix="animatic_smoke6_"))
    print(f"[smoke6] Working in {work_root}")

    input_dir = work_root / "input"
    work_dir = work_root / "work"
    input_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)

    _write_sheet(input_dir / "bhim.png")
    _write_sheet(input_dir / "jaggu.png")
    _write_mp4(input_dir / "shot_001.mp4")

    (input_dir / "metadata.json").write_text(json.dumps({
        "schemaVersion": 1,
        "generatedAt": "2026-04-23T00:00:00Z",
        "project": {"name": "SmokeTest6_Ep001", "batchSize": 4, "fps": 25},
        "shots": [{
            "shotId": "shot_001",
            "mp4Filename": "shot_001.mp4",
            "durationFrames": SHOT_FRAMES,
            "durationSeconds": SHOT_FRAMES / FPS,
            "characterCount": 2,
            "characters": [
                {"identity": "bhim", "position": "L"},
                {"identity": "jaggu", "position": "R"},
            ],
        }],
    }, indent=2), encoding="utf-8")

    (input_dir / "characters.json").write_text(json.dumps({
        "schemaVersion": 1,
        "generatedAt": "2026-04-23T00:00:00Z",
        "conventions": {
            "sheetFormat": "8-angle-horizontal-strip",
            "backgroundExpected": "transparent",
            "angleOrderLeftToRight": [
                "back", "back-3q-L", "profile-L", "front-3q-L",
                "front", "front-3q-R", "profile-R", "back-3q-R",
            ],
            "angleOrderConfirmed": True,
        },
        "characters": [
            {
                "name": "bhim",
                "sheetFilename": "bhim.png",
                "width": 512, "height": 64,
                "quality": {"ok": True, "detectedIslands": 8, "backgroundMode": "transparent", "reasons": []},
                "addedAt": "2026-04-23T00:00:00Z",
            },
            {
                "name": "jaggu",
                "sheetFilename": "jaggu.png",
                "width": 512, "height": 64,
                "quality": {"ok": True, "detectedIslands": 8, "backgroundMode": "transparent", "reasons": []},
                "addedAt": "2026-04-23T00:00:00Z",
            },
        ],
    }, indent=2), encoding="utf-8")

    def _run(label: str, argv: list[str]) -> None:
        print(f"\n[smoke6] === {label} ===")
        rc = subprocess.run(
            [sys.executable, *argv],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        print(rc.stdout)
        if rc.returncode != 0:
            print("STDERR:", rc.stderr)
            raise SystemExit(f"[smoke6] {label} failed with rc={rc.returncode}")

    _run("Node 2", [
        str(REPO_ROOT / "run_node2.py"),
        "--input-dir", str(input_dir),
        "--output-file", str(input_dir / "queue.json"),
    ])
    _run("Node 3", [
        str(REPO_ROOT / "run_node3.py"),
        "--queue", str(input_dir / "queue.json"),
        "--work-dir", str(work_dir),
    ])
    _run("Node 4", [
        str(REPO_ROOT / "run_node4.py"),
        "--node3-result", str(work_dir / "node3_result.json"),
    ])
    _run("Node 5", [
        str(REPO_ROOT / "run_node5.py"),
        "--node4-result", str(work_dir / "node4_result.json"),
        "--queue", str(input_dir / "queue.json"),
    ])
    _run("Node 6", [
        str(REPO_ROOT / "run_node6.py"),
        "--node5-result", str(work_dir / "node5_result.json"),
        "--queue", str(input_dir / "queue.json"),
        "--characters", str(input_dir / "characters.json"),
    ])

    # ---------- Validate node6_result.json ----------
    n6_path = work_dir / "node6_result.json"
    assert n6_path.is_file(), f"Expected {n6_path}"
    n6 = json.loads(n6_path.read_text(encoding="utf-8"))
    assert n6["schemaVersion"] == 1
    assert n6["projectName"] == "SmokeTest6_Ep001"
    assert n6["lineArtMethod"] == "dog"
    assert len(n6["shots"]) == 1
    shot0 = n6["shots"][0]
    assert shot0["shotId"] == "shot_001"
    assert shot0["detectionCount"] >= 2
    print(f"\n[smoke6] Aggregate angle histogram: {shot0['angleHistogram']}")

    # ---------- Validate per-shot reference_map.json ----------
    rm_path = work_dir / "shot_001" / "reference_map.json"
    assert rm_path.is_file()
    rm = json.loads(rm_path.read_text(encoding="utf-8"))
    assert rm["shotId"] == "shot_001"
    assert rm["lineArtMethod"] == "dog"
    assert len(rm["keyPoses"]) >= 1

    # ---------- Validate reference_crops/ exists and contains expected pairs ----------
    crops_dir = work_dir / "shot_001" / "reference_crops"
    assert crops_dir.is_dir(), f"Missing {crops_dir}"
    pngs = sorted(p.name for p in crops_dir.glob("*.png"))
    assert len(pngs) >= 2, f"Expected at least 2 crops (color + lineart), got {pngs}"
    print(f"\n[smoke6] Crops written: {pngs}")

    # ---------- Spot-check a match's paths resolve on disk ----------
    first_kp = rm["keyPoses"][0]
    for m in first_kp.get("matches", []):
        color_path = Path(m["referenceColorCropPath"])
        lineart_path = Path(m["referenceLineArtCropPath"])
        assert color_path.is_file(), f"Missing color crop: {color_path}"
        assert lineart_path.is_file(), f"Missing lineart crop: {lineart_path}"
        print(
            f"  - identity={m['identity']!r} angle={m['selectedAngle']!r} "
            f"scores={ {k: round(v, 3) for k, v in m['scoreBreakdown'].items()} }"
        )

    print(f"\n[smoke6] OK - {len(rm['keyPoses'])} key pose(s), "
          f"{shot0['detectionCount']} detection(s), "
          f"{shot0['skippedCount']} skipped.")
    print(f"[smoke6] Artifacts in: {work_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
