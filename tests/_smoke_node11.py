"""End-to-end smoke test: full pipeline via Node 11 orchestrator.

Not a pytest file (leading underscore). The CAPSTONE smoke for the
whole 11-node pipeline. Verifies that the orchestrator actually
fires every node 2-10 in sequence against a Node 1-shaped input dir
and produces a real on-disk MP4 deliverable.

Differs from `tests/test_node11.py` (which mocks subprocess.Popen
to test orchestration logic in isolation) and from the per-node
smoke scripts (`_smoke_node{8,9,10}.py`, which each cover one node):
this script spawns REAL run_nodeN.py subprocesses for ALL nine
downstream nodes via Node 11's own subprocess loop.

We use --dry-run so Node 7 (the GPU node) doesn't try to contact
ComfyUI -- instead it records every generation as status="skipped"
and Node 8's substitute-rough fallback fills in by copying rough
key-pose pixels. End result: an MP4 of rough animatic pixels (no AI
generation), but with the entire orchestration + manifest chain
exercised end-to-end on the laptop.

Run from repo root with the embedded Python:

    .../python_embeded/python.exe tests/_smoke_node11.py
    .../python_embeded/python.exe tests/_smoke_node11.py --work-dir <path>

Wall time: ~30-60 s on a typical laptop (real ffmpeg encode +
real PIL ops + subprocess spin-up overhead per node).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------
# Synthesis: a complete Node 1-shaped input dir
# ---------------------------------------------------------------

# Tiny dimensions to keep the smoke fast. 128x96 frames are enough
# for Node 5 detection + Node 6 alpha-island slicing; 5-frame shots
# keep ffmpeg encode/decode under a few seconds.
FRAME_W, FRAME_H = 128, 96
SHOT_FRAMES = 5
PROJECT_FPS = 25
SHOT_ID = "shot_001"
MP4_FILENAME = f"{SHOT_ID}.mp4"
CHAR_IDENTITY = "TestChar"
SHEET_FILENAME = f"{CHAR_IDENTITY}_sheet.png"

CANONICAL_ANGLES = [
    "back", "back-3q-L", "profile-L", "front-3q-L",
    "front", "front-3q-R", "profile-R", "back-3q-R",
]


def _draw_rough_frame(frame_idx: int) -> np.ndarray:
    """Tiny rough-animatic frame with one centered dark character
    blob. Position = "C" so Node 5's centerX bin falls in [0.45, 0.55]."""
    arr = np.full((FRAME_H, FRAME_W, 3), 255, dtype=np.uint8)
    # Dark character blob in the center
    cx = FRAME_W // 2
    arr[20:75, cx - 12:cx + 12] = 30
    # Slight per-frame variation so phase-correlation has signal
    # to work with (otherwise every frame is identical and Node 4
    # produces a single key pose — which is fine but boring).
    if frame_idx > 0:
        # Tiny vertical wobble to differentiate frames
        wobble = (frame_idx % 2) * 2
        arr[20 + wobble:75 + wobble, cx - 12:cx + 12] = 30
        if wobble:
            arr[20:20 + wobble, cx - 12:cx + 12] = 255
    return arr


def _write_smoke_mp4(path: Path) -> None:
    """Encode a tiny 5-frame MP4 at 25 FPS via imageio-ffmpeg's static
    binary (same wheel Node 3 uses for decode)."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        for i in range(SHOT_FRAMES):
            Image.fromarray(_draw_rough_frame(i), mode="RGB").save(
                tdp / f"f_{i:04d}.png"
            )
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-framerate", str(PROJECT_FPS),
            "-i", str(tdp / "f_%04d.png"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "18",
            str(path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)


def _draw_smoke_sheet(path: Path) -> None:
    """Synthesize an RGBA character sheet with 8 alpha islands sorted
    left-to-right (one per canonical angle). Each island is a 6x12
    colored rectangle with 2-pixel transparent gutters on either side
    -- enough for Node 6's alpha-island bbox detection AND its
    multi-signal angle scoring (different per-angle silhouettes give
    different IoU/symmetry/aspect signals)."""
    sheet_w = 8 * 8  # 8 islands at 8 px stride = 64 wide
    sheet_h = 16
    img = Image.new("RGBA", (sheet_w, sheet_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Per-angle silhouettes that look at least minimally different
    # so Node 6's per-angle scoring isn't picking blindly. Vary
    # height + horizontal offset so symmetry/aspect signals differ.
    palette = [
        (220, 60, 60),   # back
        (200, 100, 60),  # back-3q-L
        (180, 140, 60),  # profile-L
        (140, 180, 60),  # front-3q-L
        (60, 200, 60),   # front
        (60, 180, 140),  # front-3q-R
        (60, 140, 180),  # profile-R
        (60, 100, 200),  # back-3q-R
    ]
    for i, color in enumerate(palette):
        x0 = i * 8 + 1
        x1 = x0 + 6
        y0 = 1 + (i % 3)        # vary top
        y1 = sheet_h - 1 - (i % 2)  # vary bottom
        draw.rectangle((x0, y0, x1, y1),
                       fill=(color[0], color[1], color[2], 255))
    img.save(path, "PNG")


def _build_input_dir(input_dir: Path) -> dict[str, Path]:
    """Synthesize every file Node 2 expects under `input_dir`.

    Returns dict with paths.
    """
    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True)

    # 1) MP4
    mp4_path = input_dir / MP4_FILENAME
    _write_smoke_mp4(mp4_path)

    # 2) Sheet PNG
    sheet_path = input_dir / SHEET_FILENAME
    _draw_smoke_sheet(sheet_path)
    sheet_dims = Image.open(sheet_path).size

    # 3) characters.json (Node 1's character library output) -- schema
    # shapes per pipeline/schemas.py CharactersFile / ConventionsSpec /
    # CharacterSpec / QualitySpec.
    now_iso = datetime.now(timezone.utc).isoformat()
    chars = {
        "schemaVersion": 1,
        "generatedAt": now_iso,
        "conventions": {
            "sheetFormat": "8-angle horizontal strip",
            "backgroundExpected": "transparent",
            "angleOrderLeftToRight": CANONICAL_ANGLES,
            "angleOrderConfirmed": True,
        },
        "characters": [
            {
                "name": CHAR_IDENTITY,
                "sheetFilename": SHEET_FILENAME,
                "width": sheet_dims[0],
                "height": sheet_dims[1],
                "quality": {
                    "ok": True,
                    "detectedIslands": 8,
                    "backgroundMode": "RGBA",
                    "reasons": [],
                },
                "addedAt": now_iso,
                "poseExtractor": "lineart-fallback",
            }
        ],
    }
    chars_path = input_dir / "characters.json"
    chars_path.write_text(json.dumps(chars, indent=2))

    # 4) metadata.json (Node 1's shot form output)
    meta = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "project": {
            "name": "smoke11",
            "fps": PROJECT_FPS,
            "batchSize": 1,
            "notes": "Node 11 capstone smoke test (--dry-run, no GPU).",
        },
        "shots": [
            {
                "shotId": SHOT_ID,
                "mp4Filename": MP4_FILENAME,
                "characterCount": 1,
                "characters": [
                    {"identity": CHAR_IDENTITY, "position": "C"}
                ],
                "durationFrames": SHOT_FRAMES,
                "durationSeconds": SHOT_FRAMES / PROJECT_FPS,
            }
        ],
    }
    meta_path = input_dir / "metadata.json"
    meta_path.write_text(json.dumps(meta, indent=2))

    return {
        "input_dir": input_dir,
        "mp4_path": mp4_path,
        "sheet_path": sheet_path,
        "characters_path": chars_path,
        "metadata_path": meta_path,
    }


# ---------------------------------------------------------------
# Drive: run_node11.py via subprocess + validate outputs
# ---------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "End-to-end Node 11 orchestrator smoke (synthetic Node 1 "
            "input dir -> run_node11.py --dry-run -> real MP4 deliverable)."
        ),
    )
    ap.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Where to put the synthesized input dir + the work dir "
            "Node 11 writes into. Default: a fresh tmpdir."
        ),
    )
    ap.add_argument(
        "--keep",
        action="store_true",
        help="Don't delete the work dir after a successful run.",
    )
    args = ap.parse_args(argv)

    if args.work_dir:
        root = args.work_dir.resolve()
    else:
        root = Path(tempfile.mkdtemp(prefix="smoke11_"))

    input_dir = root / "input"
    work_dir = root / "work"
    print(f"[smoke11] building input dir at {input_dir}")
    paths = _build_input_dir(input_dir)
    print(f"[smoke11]   metadata.json    = {paths['metadata_path']}")
    print(f"[smoke11]   characters.json  = {paths['characters_path']}")
    print(f"[smoke11]   sheet PNG        = {paths['sheet_path']}")
    print(f"[smoke11]   shot MP4         = {paths['mp4_path']}")
    print()

    cli_path = REPO_ROOT / "run_node11.py"
    cmd = [
        sys.executable, str(cli_path),
        "--input-dir", str(input_dir),
        "--work-dir", str(work_dir),
        "--dry-run",
    ]
    print(f"[smoke11] invoking: {' '.join(cmd)}")
    print(f"[smoke11] (this will spawn 9 real subprocess invocations of "
          f"run_nodeN.py and may take 30-60s)")
    print()

    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(f"[smoke11] CLI exit={proc.returncode}")
    if proc.stdout:
        print(f"[smoke11] CLI stdout (last 30 lines):")
        for line in proc.stdout.splitlines()[-30:]:
            print(f"           | {line}")
    if proc.stderr:
        print(f"[smoke11] CLI stderr (last 30 lines):")
        for line in proc.stderr.splitlines()[-30:]:
            print(f"           | {line}")
    print()

    assert proc.returncode == 0, (
        f"run_node11.py exited {proc.returncode} (expected 0). "
        "See stdout/stderr above."
    )

    # ---------- Validate aggregate node11_result.json ----------
    n11_path = work_dir / "node11_result.json"
    assert n11_path.is_file(), f"Missing aggregate {n11_path}"
    n11 = json.loads(n11_path.read_text(encoding="utf-8"))
    assert n11["schemaVersion"] == 1
    assert n11["projectName"] == "smoke11"
    assert n11["totalShots"] == 1
    # In --dry-run, Node 7's substitute-rough fallback should produce
    # a valid MP4, so this should be a SUCCESS (not partial).
    assert n11["succeededShots"] == 1, (
        f"Expected 1 succeeded shot; got {n11['succeededShots']} "
        f"(failed: {n11['failedShots']})"
    )
    assert n11["failedShots"] == 0
    print(f"[smoke11] node11_result.json: totalShots={n11['totalShots']} "
          f"succeeded={n11['succeededShots']} failed={n11['failedShots']} "
          f"totalSeconds={n11['totalSeconds']:.1f}s")

    # ---------- Validate per-node-step records ----------
    node_steps = n11["nodeSteps"]
    assert len(node_steps) == 9, (
        f"Expected 9 node-step records (Nodes 2-10); got {len(node_steps)}"
    )
    for step in node_steps:
        assert step["status"] == "ok", (
            f"Node {step['node']} status={step['status']} "
            f"(exit {step['exitCode']}); expected 'ok'. "
            f"Last stderr tail:\n{step['lastStderrTail']}"
        )
        assert step["attempts"] == 1, (
            f"Node {step['node']} took {step['attempts']} attempts; "
            "expected 1 (default retries=0)"
        )
    nodes_invoked = [s["node"] for s in node_steps]
    assert nodes_invoked == list(range(2, 11)), (
        f"Expected nodes [2..10] invoked in order; got {nodes_invoked}"
    )
    print(f"[smoke11] all 9 node-step records 'ok' in correct order: "
          f"{nodes_invoked}")

    # ---------- Validate progress JSONL ----------
    progress_path = work_dir / "node11_progress.jsonl"
    assert progress_path.is_file()
    events = [
        json.loads(line)
        for line in progress_path.read_text(encoding="utf-8").splitlines()
    ]
    event_types = [e["event"] for e in events]
    assert "batch_start" in event_types
    assert "batch_complete" in event_types
    # Each node 2-10 should have exactly 1 start + 1 complete
    for n in range(2, 11):
        starts = [e for e in events
                  if e.get("event") == "node_step_start" and e.get("node") == n]
        completes = [e for e in events
                     if e.get("event") == "node_step_complete" and e.get("node") == n]
        assert len(starts) == 1, f"Node {n}: expected 1 start, got {len(starts)}"
        assert len(completes) == 1, f"Node {n}: expected 1 complete, got {len(completes)}"
        assert completes[0]["exitCode"] == 0, f"Node {n}: non-zero exit"
    # GPU check should be SKIPPED in --dry-run (locked decision #16)
    assert "gpu_visible" not in event_types
    assert "gpu_unavailable" not in event_types
    print(f"[smoke11] progress JSONL: {len(events)} events, batch_start "
          f"+ 9 (start, complete) pairs + batch_complete, GPU check "
          f"skipped under --dry-run")

    # ---------- Validate every Node 2-10 wrote its expected outputs ----------
    expected_artifacts = [
        ("Node 2 queue.json", input_dir / "queue.json"),
        ("Node 3 result", work_dir / "node3_result.json"),
        ("Node 4 result", work_dir / "node4_result.json"),
        ("Node 5 result", work_dir / "node5_result.json"),
        ("Node 6 result", work_dir / "node6_result.json"),
        ("Node 7 result", work_dir / "node7_result.json"),
        ("Node 8 result", work_dir / "node8_result.json"),
        ("Node 9 result", work_dir / "node9_result.json"),
        ("Node 10 result", work_dir / "node10_result.json"),
    ]
    for label, p in expected_artifacts:
        assert p.is_file(), f"{label} missing at {p}"
    print(f"[smoke11] all 9 nodeN_result.json files present on disk")

    # ---------- Validate per-shot artifacts ----------
    shot_root = work_dir / SHOT_ID
    per_shot_artifacts = [
        ("Node 4 keypose_map", shot_root / "keypose_map.json"),
        ("Node 5 character_map", shot_root / "character_map.json"),
        ("Node 6 reference_map", shot_root / "reference_map.json"),
        ("Node 7 refined_map", shot_root / "refined_map.json"),
        ("Node 8 composed_map", shot_root / "composed_map.json"),
        ("Node 9 timed_map", shot_root / "timed_map.json"),
    ]
    for label, p in per_shot_artifacts:
        assert p.is_file(), f"{label} missing at {p}"
    print(f"[smoke11] all 6 per-shot manifests present")

    # ---------- Validate Node 7's --dry-run propagation ----------
    refined_map = json.loads(
        (shot_root / "refined_map.json").read_text(encoding="utf-8")
    )
    for gen in refined_map.get("generations", []):
        assert gen["status"] == "skipped", (
            f"Node 7 generation status={gen['status']!r} but expected "
            "'skipped' under --dry-run"
        )
    print(f"[smoke11] Node 7 --dry-run propagated correctly: "
          f"{len(refined_map.get('generations', []))} generation(s) "
          f"all status='skipped'")

    # ---------- Validate Node 8's substitute-rough fired ----------
    composed_map = json.loads(
        (shot_root / "composed_map.json").read_text(encoding="utf-8")
    )
    sub_count = sum(
        1
        for kp in composed_map.get("keyPoses", [])
        for c in kp.get("characters", [])
        if c.get("substitutedFromRough")
    )
    assert sub_count > 0, (
        "Node 8 substitute-rough should have fired on every "
        "Node-7-skipped generation; got 0 substitute events"
    )
    print(f"[smoke11] Node 8 substitute-rough fired {sub_count} time(s) "
          f"(Node 7 dry-run skipped every generation -> Node 8 fell "
          f"back to rough pixels)")

    # ---------- Validate the final MP4 deliverable ----------
    output_dir = work_dir / "output"
    expected_mp4 = output_dir / f"{SHOT_ID}_refined.mp4"
    assert expected_mp4.is_file(), (
        f"Final MP4 deliverable missing at {expected_mp4}"
    )
    assert expected_mp4.stat().st_size > 0, "MP4 is zero bytes"

    n_frames, n_secs = imageio_ffmpeg.count_frames_and_secs(str(expected_mp4))
    assert abs(n_frames - SHOT_FRAMES) <= 1, (
        f"MP4 has {n_frames} frame(s) but expected {SHOT_FRAMES}"
    )
    expected_secs = SHOT_FRAMES / PROJECT_FPS
    assert abs(n_secs - expected_secs) < 0.5
    file_size_kb = expected_mp4.stat().st_size / 1024
    print(f"[smoke11] final MP4 deliverable: {expected_mp4}")
    print(f"[smoke11]   {n_frames} frames / {n_secs:.3f}s / "
          f"{file_size_kb:.1f} KB")

    # ---------- Validate node11_result also points at the MP4 ----------
    shot0 = n11["shotResults"][0]
    assert shot0["status"] == "ok"
    assert shot0["refinedMp4Path"] == str(expected_mp4)
    print(f"[smoke11] node11_result.json shotResults[0] points at "
          f"the MP4 correctly")

    print()
    print(f"[smoke11] OK -- ENTIRE 11-NODE PIPELINE END-TO-END VERIFIED.")
    print(f"[smoke11] artifacts in: {root}")
    print(f"[smoke11] open the MP4 in any video player to confirm "
          f"the deliverable is playable.")

    if not args.keep and not args.work_dir:
        try:
            shutil.rmtree(root)
            print(f"[smoke11] cleaned up tmpdir {root}")
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
