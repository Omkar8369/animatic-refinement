"""Tests for Node 4 — Key Pose Extraction.

Every test synthesizes PNG frames via PIL to mimic what Node 3 produces,
so nothing binary is committed to git.

Run from repo root with:

    python -m pytest tests/ -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import numpy as np
import pytest
from PIL import Image

from pipeline.cli_node4 import main as cli_main
from pipeline.errors import (
    KeyPoseExtractionError,
    Node3ResultInputError,
    Node4Error,
    PipelineError,
)
from pipeline.node4 import (
    DEFAULT_MAE_THRESHOLD,
    Node4Result,
    ShotKeyPoseSummary,
    extract_keyposes_for_queue,
    extract_keyposes_for_shot,
)


# ---------------------------------------------------------------
# Fixture helpers: synthesize a Node 3 work-dir layout
# ---------------------------------------------------------------

# All fixture frames use this resolution. Small enough that tests run fast,
# large enough that phase correlation still finds a clean peak.
FRAME_W = 96
FRAME_H = 64


def _save_frame(arr: np.ndarray, path: Path) -> None:
    """Save an `arr` of shape (H, W) uint8 as a grayscale PNG."""
    img = Image.fromarray(arr, mode="L")
    img.save(path)


def _static_frame(value: int = 128) -> np.ndarray:
    """A uniform-grey frame with one small dark square (a 'pose'). Static."""
    arr = np.full((FRAME_H, FRAME_W), value, dtype=np.uint8)
    # Draw a 12x12 dark square as the 'pose' at a fixed spot.
    arr[20:32, 30:42] = 20
    return arr


def _translated_frame(dy: int, dx: int, value: int = 128) -> np.ndarray:
    """Same pose as `_static_frame`, translated by (dy, dx) full-res pixels."""
    arr = np.full((FRAME_H, FRAME_W), value, dtype=np.uint8)
    y0 = max(0, 20 + dy)
    y1 = min(FRAME_H, 32 + dy)
    x0 = max(0, 30 + dx)
    x1 = min(FRAME_W, 42 + dx)
    if y1 > y0 and x1 > x0:
        arr[y0:y1, x0:x1] = 20
    return arr


def _new_pose_frame() -> np.ndarray:
    """A visually-distinct pose: diagonal stripe — nothing like _static_frame."""
    arr = np.full((FRAME_H, FRAME_W), 128, dtype=np.uint8)
    for i in range(FRAME_H):
        for j in range(FRAME_W):
            if (i + j) % 6 < 2:
                arr[i, j] = 20
    return arr


def _make_shot_dir(
    work_dir: Path,
    shot_id: str,
    frames: list[np.ndarray],
) -> dict:
    """Write one shot's frame PNGs + _manifest.json; return the shot dict
    that would appear in node3_result.json['shots'].
    """
    shot_dir = work_dir / shot_id
    shot_dir.mkdir(parents=True, exist_ok=True)
    filenames: list[str] = []
    for i, arr in enumerate(frames, start=1):
        name = f"frame_{i:04d}.png"
        _save_frame(arr, shot_dir / name)
        filenames.append(name)

    # Minimal Node 3 per-shot manifest (Node 4 doesn't read it, but the
    # folder shape should match production).
    (shot_dir / "_manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "shotId": shot_id,
                "framesDir": str(shot_dir),
                "frameFilenames": filenames,
                "expectedFrames": len(frames),
                "actualFrames": len(frames),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "shotId": shot_id,
        "mp4Path": str(shot_dir / "source.mp4"),
        "framesDir": str(shot_dir),
        "expectedFrames": len(frames),
        "actualFrames": len(frames),
        "frameFilenames": filenames,
    }


def _write_node3_result(
    work_dir: Path,
    shots: list[dict],
    project_name: str = "ChhotaBhim_Ep042",
) -> Path:
    """Write a minimal node3_result.json matching the v1 schema."""
    payload = {
        "schemaVersion": 1,
        "projectName": project_name,
        "workDir": str(work_dir),
        "ffmpegBinary": "/fake/ffmpeg",
        "extractedAt": "2026-04-23T00:00:00+00:00",
        "shots": shots,
        "warnings": [],
    }
    path = work_dir / "node3_result.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


ShotBuilder = Callable[..., dict]


@pytest.fixture
def make_static_shot(tmp_path: Path) -> ShotBuilder:
    """Factory: a shot of N identical frames (one key pose, all held, offset 0)."""
    def _build(shot_id: str, n: int = 5) -> dict:
        frames = [_static_frame() for _ in range(n)]
        return _make_shot_dir(tmp_path, shot_id, frames)
    return _build


@pytest.fixture
def make_slide_shot(tmp_path: Path) -> ShotBuilder:
    """Factory: a shot where the pose slides right by `step` px per frame."""
    def _build(shot_id: str, n: int = 5, step: int = 4) -> dict:
        frames = [_translated_frame(dy=0, dx=i * step) for i in range(n)]
        return _make_shot_dir(tmp_path, shot_id, frames)
    return _build


@pytest.fixture
def make_two_pose_shot(tmp_path: Path) -> ShotBuilder:
    """Factory: half static, half new pose — should split into two key poses."""
    def _build(shot_id: str, n_static: int = 3, n_new: int = 3) -> dict:
        frames = [_static_frame() for _ in range(n_static)] + [
            _new_pose_frame() for _ in range(n_new)
        ]
        return _make_shot_dir(tmp_path, shot_id, frames)
    return _build


# ---------------------------------------------------------------
# Happy path — the three canonical scenarios
# ---------------------------------------------------------------

class TestStaticShot:
    def test_single_key_pose_all_held(
        self, tmp_path: Path, make_static_shot: ShotBuilder
    ):
        shot = make_static_shot("shot_001", n=5)
        n3 = _write_node3_result(tmp_path, [shot])

        result = extract_keyposes_for_queue(n3)

        assert len(result.shots) == 1
        s = result.shots[0]
        assert s.shotId == "shot_001"
        assert s.totalFrames == 5
        assert s.keyPoseCount == 1

        # Verify per-shot map
        km = json.loads(Path(s.keyPoseMapPath).read_text(encoding="utf-8"))
        assert km["schemaVersion"] == 1
        assert km["shotId"] == "shot_001"
        assert len(km["keyPoses"]) == 1
        kp = km["keyPoses"][0]
        assert kp["keyPoseIndex"] == 0
        assert kp["sourceFrame"] == 1
        assert kp["keyPoseFilename"] == "frame_0001.png"
        assert len(kp["heldFrames"]) == 5
        for h in kp["heldFrames"]:
            assert h["offset"] == [0, 0]

    def test_keyposes_folder_contains_only_first_frame(
        self, tmp_path: Path, make_static_shot: ShotBuilder
    ):
        shot = make_static_shot("shot_001", n=5)
        n3 = _write_node3_result(tmp_path, [shot])
        extract_keyposes_for_queue(n3)

        kp_dir = Path(shot["framesDir"]) / "keyposes"
        copies = sorted(kp_dir.glob("frame_*.png"))
        assert [p.name for p in copies] == ["frame_0001.png"]


class TestSlideShot:
    def test_single_key_pose_with_growing_offsets(
        self, tmp_path: Path, make_slide_shot: ShotBuilder
    ):
        """Sliding pose should land in ONE key pose, NOT a new pose per frame."""
        shot = make_slide_shot("shot_001", n=5, step=4)
        n3 = _write_node3_result(tmp_path, [shot])

        result = extract_keyposes_for_queue(n3)
        assert result.shots[0].keyPoseCount == 1

        km = json.loads(Path(result.shots[0].keyPoseMapPath).read_text())
        held = km["keyPoses"][0]["heldFrames"]
        assert len(held) == 5

        # Frame 1 (anchor) offset is [0, 0]; subsequent offsets should
        # drift to the right (positive dx). We compare to the ideal offset
        # within a tolerance because LANCZOS downscale + rounding can
        # shift by +-1 px at full resolution.
        assert held[0]["offset"] == [0, 0]
        for i, h in enumerate(held):
            expected_dx = i * 4
            dy, dx = h["offset"]
            assert abs(dy) <= 1, f"frame {i}: dy should be ~0, got {dy}"
            assert abs(dx - expected_dx) <= 2, (
                f"frame {i}: dx should be ~{expected_dx}, got {dx}"
            )


class TestTwoPoseShot:
    def test_splits_at_pose_boundary(
        self, tmp_path: Path, make_two_pose_shot: ShotBuilder
    ):
        shot = make_two_pose_shot("shot_001", n_static=3, n_new=3)
        n3 = _write_node3_result(tmp_path, [shot])

        result = extract_keyposes_for_queue(n3)
        s = result.shots[0]
        assert s.keyPoseCount == 2

        km = json.loads(Path(s.keyPoseMapPath).read_text())
        kp0, kp1 = km["keyPoses"]

        # First key pose anchored at frame 1, holds frames 1-3.
        assert kp0["keyPoseIndex"] == 0
        assert kp0["sourceFrame"] == 1
        assert [h["frame"] for h in kp0["heldFrames"]] == [1, 2, 3]

        # Second key pose starts where the new pose appears (frame 4).
        assert kp1["keyPoseIndex"] == 1
        assert kp1["sourceFrame"] == 4
        assert [h["frame"] for h in kp1["heldFrames"]] == [4, 5, 6]

    def test_both_keyposes_copied(
        self, tmp_path: Path, make_two_pose_shot: ShotBuilder
    ):
        shot = make_two_pose_shot("shot_001", n_static=3, n_new=3)
        n3 = _write_node3_result(tmp_path, [shot])
        extract_keyposes_for_queue(n3)

        kp_dir = Path(shot["framesDir"]) / "keyposes"
        copies = sorted(kp_dir.glob("frame_*.png"))
        assert [p.name for p in copies] == ["frame_0001.png", "frame_0004.png"]


# ---------------------------------------------------------------
# Aggregate + rerun behavior
# ---------------------------------------------------------------

class TestAggregate:
    def test_aggregate_result_written(
        self, tmp_path: Path, make_static_shot: ShotBuilder,
        make_two_pose_shot: ShotBuilder,
    ):
        shot_a = make_static_shot("shot_001", n=4)
        shot_b = make_two_pose_shot("shot_002", n_static=2, n_new=2)
        n3 = _write_node3_result(tmp_path, [shot_a, shot_b])

        extract_keyposes_for_queue(n3)

        agg = json.loads((tmp_path / "node4_result.json").read_text())
        assert agg["schemaVersion"] == 1
        assert agg["projectName"] == "ChhotaBhim_Ep042"
        assert len(agg["shots"]) == 2
        by_id = {s["shotId"]: s for s in agg["shots"]}
        assert by_id["shot_001"]["keyPoseCount"] == 1
        assert by_id["shot_002"]["keyPoseCount"] == 2

    def test_rerun_clears_stale_keyposes(
        self, tmp_path: Path, make_two_pose_shot: ShotBuilder,
        make_static_shot: ShotBuilder,
    ):
        # Run 1: two poses -> two copies in keyposes/
        shot = make_two_pose_shot("shot_001", n_static=3, n_new=3)
        n3 = _write_node3_result(tmp_path, [shot])
        extract_keyposes_for_queue(n3)
        kp_dir = Path(shot["framesDir"]) / "keyposes"
        assert len(list(kp_dir.glob("frame_*.png"))) == 2

        # Run 2: replace the shot with a static one -> one copy only, and
        # the stale shot_0004.png from run 1 must be gone.
        for f in Path(shot["framesDir"]).glob("frame_*.png"):
            f.unlink()
        for f in Path(shot["framesDir"]).glob("_manifest.json"):
            f.unlink()
        shot_new = make_static_shot("shot_001", n=3)
        n3 = _write_node3_result(tmp_path, [shot_new])
        extract_keyposes_for_queue(n3)
        remaining = sorted(kp_dir.glob("frame_*.png"))
        assert [p.name for p in remaining] == ["frame_0001.png"]


# ---------------------------------------------------------------
# Threshold sensitivity
# ---------------------------------------------------------------

class TestThreshold:
    def test_very_high_threshold_merges_everything(
        self, tmp_path: Path, make_two_pose_shot: ShotBuilder
    ):
        shot = make_two_pose_shot("shot_001", n_static=3, n_new=3)
        n3 = _write_node3_result(tmp_path, [shot])
        result = extract_keyposes_for_queue(n3, threshold=250.0)
        # With an absurd threshold, ANY pair is "similar" -> 1 key pose.
        assert result.shots[0].keyPoseCount == 1

    def test_very_low_threshold_splits_noisy_holds(self, tmp_path: Path):
        """With threshold=0 and noisy frames, even 'held' frames exceed it.

        Clean integer-pixel fixtures give aligned MAE == 0.0 exactly, which
        tells us nothing about the threshold logic. Adding per-frame noise
        produces non-zero aligned MAE, so threshold=0 correctly splits.
        """
        shot_dir = tmp_path / "shot_001"
        shot_dir.mkdir()
        rng = np.random.default_rng(seed=42)
        filenames: list[str] = []
        for i in range(4):
            arr = _static_frame().astype(np.int16)
            # Per-frame noise — the pose is the same but pixels differ.
            noise = rng.integers(-4, 5, size=arr.shape, dtype=np.int16)
            arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
            name = f"frame_{i + 1:04d}.png"
            _save_frame(arr, shot_dir / name)
            filenames.append(name)
        shot = {
            "shotId": "shot_001",
            "mp4Path": str(shot_dir / "source.mp4"),
            "framesDir": str(shot_dir),
            "expectedFrames": 4,
            "actualFrames": 4,
            "frameFilenames": filenames,
        }
        n3 = _write_node3_result(tmp_path, [shot])
        result = extract_keyposes_for_queue(n3, threshold=0.0)
        # Threshold 0 + non-zero MAE -> every frame becomes its own key pose.
        assert result.shots[0].keyPoseCount >= 2


# ---------------------------------------------------------------
# node3_result.json input failures
# ---------------------------------------------------------------

class TestNode3ResultInput:
    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(Node3ResultInputError, match="not found"):
            extract_keyposes_for_queue(tmp_path / "nope.json")

    def test_malformed_json(self, tmp_path: Path):
        p = tmp_path / "node3_result.json"
        p.write_text("{ not valid")
        with pytest.raises(Node3ResultInputError, match="not valid JSON"):
            extract_keyposes_for_queue(p)

    def test_wrong_schema_version(self, tmp_path: Path):
        p = tmp_path / "node3_result.json"
        p.write_text(json.dumps({
            "schemaVersion": 2,
            "workDir": str(tmp_path),
            "shots": [],
        }))
        with pytest.raises(Node3ResultInputError, match="unsupported schemaVersion"):
            extract_keyposes_for_queue(p)

    def test_missing_shots_key(self, tmp_path: Path):
        p = tmp_path / "node3_result.json"
        p.write_text(json.dumps({
            "schemaVersion": 1,
            "workDir": str(tmp_path),
        }))
        with pytest.raises(Node3ResultInputError, match="missing required key 'shots'"):
            extract_keyposes_for_queue(p)

    def test_shot_missing_required_field(self, tmp_path: Path):
        p = tmp_path / "node3_result.json"
        p.write_text(json.dumps({
            "schemaVersion": 1,
            "workDir": str(tmp_path),
            "shots": [{"shotId": "shot_001"}],  # missing framesDir + frameFilenames
        }))
        with pytest.raises(Node3ResultInputError, match="missing 'framesDir'"):
            extract_keyposes_for_queue(p)

    def test_frames_dir_disappeared(self, tmp_path: Path):
        # node3_result.json references a frames folder that doesn't exist.
        p = tmp_path / "node3_result.json"
        p.write_text(json.dumps({
            "schemaVersion": 1,
            "workDir": str(tmp_path),
            "shots": [{
                "shotId": "shot_ghost",
                "framesDir": str(tmp_path / "missing_folder"),
                "frameFilenames": ["frame_0001.png"],
            }],
        }))
        with pytest.raises(Node3ResultInputError, match="frames folder does not exist"):
            extract_keyposes_for_queue(p)


# ---------------------------------------------------------------
# Key-pose extraction failures (bad frames)
# ---------------------------------------------------------------

class TestKeyPoseExtractionFailures:
    def test_empty_frame_list(self, tmp_path: Path):
        shot_dir = tmp_path / "shot_001"
        shot_dir.mkdir()
        p = tmp_path / "node3_result.json"
        p.write_text(json.dumps({
            "schemaVersion": 1,
            "workDir": str(tmp_path),
            "shots": [{
                "shotId": "shot_001",
                "framesDir": str(shot_dir),
                "frameFilenames": [],
            }],
        }))
        with pytest.raises(KeyPoseExtractionError, match="empty"):
            extract_keyposes_for_queue(p)

    def test_bad_frame_filename(self, tmp_path: Path, make_static_shot: ShotBuilder):
        shot = make_static_shot("shot_001", n=2)
        # Corrupt the filename listing so one entry doesn't match frame_NNNN.png.
        shot["frameFilenames"][1] = "wrong_name.png"
        n3 = _write_node3_result(tmp_path, [shot])
        with pytest.raises(KeyPoseExtractionError, match="unexpected frame filename"):
            extract_keyposes_for_queue(n3)

    def test_resolution_mismatch(self, tmp_path: Path):
        """All frames in a shot must share dimensions; mismatch -> clear error."""
        shot_dir = tmp_path / "shot_001"
        shot_dir.mkdir()
        _save_frame(_static_frame(), shot_dir / "frame_0001.png")
        # Save frame 2 at a different resolution.
        arr2 = np.full((FRAME_H // 2, FRAME_W // 2), 128, dtype=np.uint8)
        _save_frame(arr2, shot_dir / "frame_0002.png")
        p = tmp_path / "node3_result.json"
        p.write_text(json.dumps({
            "schemaVersion": 1,
            "workDir": str(tmp_path),
            "shots": [{
                "shotId": "shot_001",
                "framesDir": str(shot_dir),
                "frameFilenames": ["frame_0001.png", "frame_0002.png"],
            }],
        }))
        with pytest.raises(KeyPoseExtractionError, match="mismatches anchor"):
            extract_keyposes_for_queue(p)


# ---------------------------------------------------------------
# Direct per-shot API (used by ComfyUI wrapper too)
# ---------------------------------------------------------------

class TestPerShotAPI:
    def test_extract_for_shot_direct(
        self, tmp_path: Path, make_static_shot: ShotBuilder
    ):
        shot = make_static_shot("shot_001", n=3)
        summary = extract_keyposes_for_shot(
            shot_id="shot_001",
            source_frames_dir=shot["framesDir"],
            frame_filenames=shot["frameFilenames"],
        )
        assert isinstance(summary, ShotKeyPoseSummary)
        assert summary.totalFrames == 3
        assert summary.keyPoseCount == 1
        assert Path(summary.keyPoseMapPath).is_file()


# ---------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------

class TestCLI:
    def test_cli_success(
        self, tmp_path: Path, make_static_shot: ShotBuilder, capsys
    ):
        shot = make_static_shot("shot_001", n=4)
        n3 = _write_node3_result(tmp_path, [shot])
        rc = cli_main(["--node3-result", str(n3)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[node4] OK" in out
        assert (tmp_path / "node4_result.json").is_file()

    def test_cli_quiet(
        self, tmp_path: Path, make_static_shot: ShotBuilder, capsys
    ):
        shot = make_static_shot("shot_001", n=3)
        n3 = _write_node3_result(tmp_path, [shot])
        rc = cli_main(["--node3-result", str(n3), "--quiet"])
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_cli_threshold_override(
        self, tmp_path: Path, make_two_pose_shot: ShotBuilder, capsys
    ):
        shot = make_two_pose_shot("shot_001", n_static=2, n_new=2)
        n3 = _write_node3_result(tmp_path, [shot])
        rc = cli_main([
            "--node3-result", str(n3),
            "--threshold", "250.0",  # absurd -> everything merges
        ])
        assert rc == 0
        agg = json.loads((tmp_path / "node4_result.json").read_text())
        assert agg["shots"][0]["keyPoseCount"] == 1

    def test_cli_failure_returns_1(self, tmp_path: Path, capsys):
        rc = cli_main([
            "--node3-result", str(tmp_path / "nope.json"),
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "[node4] FAILED" in err


# ---------------------------------------------------------------
# Exception-hierarchy sanity
# ---------------------------------------------------------------

def test_all_node4_errors_are_pipeline_errors():
    for cls in (Node3ResultInputError, KeyPoseExtractionError):
        assert issubclass(cls, Node4Error)
        assert issubclass(cls, PipelineError)


def test_node4_error_distinct_from_node2_and_node3():
    from pipeline.errors import Node2Error, Node3Error
    assert not issubclass(Node4Error, Node2Error)
    assert not issubclass(Node4Error, Node3Error)
    assert not issubclass(Node2Error, Node4Error)
    assert not issubclass(Node3Error, Node4Error)


# ---------------------------------------------------------------
# Return-value types + default threshold sanity
# ---------------------------------------------------------------

def test_returns_node4_result_dataclass(
    tmp_path: Path, make_static_shot: ShotBuilder
):
    shot = make_static_shot("shot_001", n=2)
    n3 = _write_node3_result(tmp_path, [shot])
    result = extract_keyposes_for_queue(n3)
    assert isinstance(result, Node4Result)
    assert result.threshold == DEFAULT_MAE_THRESHOLD
