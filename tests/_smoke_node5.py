"""End-to-end smoke test: Node 2 -> Node 3 -> Node 4 -> Node 5.

Not a pytest file (leading underscore). Exercised manually after each
Node 5 ship so the CLI wiring is verified on the actual environment
the user will use, not just against in-memory PIL fixtures.

Run from repo root with the embedded Python:

    .../python_embeded/python.exe tests/_smoke_node5.py
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
    """One 8-bit grayscale frame: white bg + two dark character blobs.

    Left character is static. Right character slides right by 1px/frame so
    Node 4 collapses it into one key pose with per-held offsets.
    """
    arr = np.full((FRAME_H, FRAME_W), 255, dtype=np.uint8)
    # Left character (static, at L zone, centre x ~ 24 -> 24/128 = 0.19 -> L)
    arr[30:70, 12:36] = 20
    # Right character (slides; starts at x=92 -> centre ~104 -> 104/128 = 0.81 -> R)
    rx = 92 + frame_idx // 5  # slow drift
    if rx + 24 < FRAME_W:
        arr[30:70, rx:rx + 24] = 20
    return arr


def _write_mp4(path: Path) -> None:
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    # Render frames to a temp PNG sequence, then encode with ffmpeg.
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        for i in range(SHOT_FRAMES):
            Image.fromarray(_make_line_art_frame(i), mode="L").save(
                tdp / f"f_{i:04d}.png"
            )
        cmd = [
            ffmpeg,
            "-y",
            "-framerate", str(FPS),
            "-i", str(tdp / "f_%04d.png"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "18",
            str(path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)


def _write_sheet(path: Path) -> None:
    """8-angle horizontal strip, transparent bg, full color. Minimal stub."""
    sheet = np.zeros((64, 64 * 8, 4), dtype=np.uint8)
    # 8 colored squares side-by-side.
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
    work_root = Path(tempfile.mkdtemp(prefix="animatic_smoke_"))
    print(f"[smoke] Working in {work_root}")

    input_dir = work_root / "input"
    work_dir = work_root / "work"
    input_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)

    # ---------- Fixture: metadata + characters + sheet + MP4 ----------
    _write_sheet(input_dir / "bhim.png")
    _write_sheet(input_dir / "jaggu.png")
    _write_mp4(input_dir / "shot_001.mp4")

    (input_dir / "metadata.json").write_text(json.dumps({
        "schemaVersion": 1,
        "generatedAt": "2026-04-23T00:00:00Z",
        "project": {
            "name": "SmokeTest_Ep001",
            "batchSize": 4,
            "fps": 25,
        },
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
            "angleOrderConfirmed": False,
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

    # ---------- Run each CLI in sequence ----------
    def _run(label: str, argv: list[str]) -> None:
        print(f"\n[smoke] === {label} ===")
        rc = subprocess.run(
            [sys.executable, *argv],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        print(rc.stdout)
        if rc.returncode != 0:
            print("STDERR:", rc.stderr)
            raise SystemExit(f"[smoke] {label} failed with rc={rc.returncode}")

    _run("Node 2 (metadata validation)", [
        str(REPO_ROOT / "run_node2.py"),
        "--input-dir", str(input_dir),
        "--output-file", str(input_dir / "queue.json"),
    ])
    _run("Node 3 (MP4 -> PNG)", [
        str(REPO_ROOT / "run_node3.py"),
        "--queue", str(input_dir / "queue.json"),
        "--work-dir", str(work_dir),
    ])
    _run("Node 4 (Key Pose Extraction)", [
        str(REPO_ROOT / "run_node4.py"),
        "--node3-result", str(work_dir / "node3_result.json"),
    ])
    _run("Node 5 (Character Detection)", [
        str(REPO_ROOT / "run_node5.py"),
        "--node4-result", str(work_dir / "node4_result.json"),
        "--queue", str(input_dir / "queue.json"),
    ])

    # ---------- Validate node5_result.json ----------
    n5_path = work_dir / "node5_result.json"
    assert n5_path.is_file(), f"Expected {n5_path}"
    n5 = json.loads(n5_path.read_text(encoding="utf-8"))
    assert n5["schemaVersion"] == 1
    assert len(n5["shots"]) == 1
    shot0 = n5["shots"][0]
    assert shot0["shotId"] == "shot_001"
    assert shot0["expectedCharacterCount"] == 2
    assert shot0["totalDetections"] >= 2, (
        f"Expected >= 2 detections (2 chars x N key poses); got {shot0['totalDetections']}"
    )

    # ---------- Validate per-shot character_map.json ----------
    cm_path = work_dir / "shot_001" / "character_map.json"
    assert cm_path.is_file()
    cm = json.loads(cm_path.read_text(encoding="utf-8"))
    assert cm["shotId"] == "shot_001"
    assert cm["expectedCharacterCount"] == 2
    assert len(cm["keyPoses"]) >= 1
    first_kp = cm["keyPoses"][0]
    print(f"\n[smoke] First key pose detections:")
    for d in first_kp["detections"]:
        print(
            f"  - identity={d['identity']!r} expected={d['expectedPosition']} "
            f"detected={d['positionCode']} centerX={d['centerX']:.3f} "
            f"area={d['area']}"
        )

    # Strategy A: left blob should bind to Bhim (L), right to Jaggu (R).
    by_x = sorted(first_kp["detections"], key=lambda d: d["centerX"])
    assert by_x[0]["identity"] == "bhim", f"Leftmost should be bhim, got {by_x[0]['identity']}"
    assert by_x[0]["positionCode"] == "L"
    assert by_x[1]["identity"] == "jaggu"
    assert by_x[1]["positionCode"] == "R"

    print(f"\n[smoke] OK - {len(cm['keyPoses'])} key pose(s), "
          f"{shot0['totalDetections']} detection(s), "
          f"{shot0['warningCount']} warning(s).")
    print(f"[smoke] Artifacts in: {work_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
