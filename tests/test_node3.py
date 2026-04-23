"""Tests for Node 3 - Shot Pre-processing (MP4 -> PNG).

Every test generates its own MP4 fixture on disk using imageio-ffmpeg
(same dep we use in production) so nothing binary is committed to git.

Run from repo root with:

    python -m pytest tests/ -v
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# imageio-ffmpeg is a runtime dep; tests fail loudly if it's absent.
import imageio_ffmpeg  # type: ignore[import-not-found]

from pipeline.cli_node3 import main as cli_main
from pipeline.errors import (
    FFmpegError,
    FrameExtractionError,
    Node3Error,
    PipelineError,
    QueueInputError,
)
from pipeline.node3 import (
    Node3Result,
    extract_frames_for_queue,
    extract_frames_for_shot,
)


# ---------------------------------------------------------------
# Fixture helpers: generate tiny MP4s on demand
# ---------------------------------------------------------------

def _make_mp4(path: Path, num_frames: int, fps: int = 25) -> None:
    """Generate a `num_frames`-frame MP4 at `path` using ffmpeg directly.

    Uses ffmpeg's `testsrc2` filter (no external assets, works headless
    on any OS). Each frame is a visually-distinct test pattern, so our
    extractor's per-frame count is trustworthy.
    """
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    duration_seconds = num_frames / fps
    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"testsrc2=size=64x64:rate={fps}:duration={duration_seconds}",
        "-pix_fmt", "yuv420p",
        "-frames:v", str(num_frames),
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"test fixture MP4 generation failed: {proc.stderr!r}"
        )


def _write_queue(
    queue_path: Path,
    shots: list[dict],
    project_name: str = "ChhotaBhim_Ep042",
    batch_size: int = 4,
) -> None:
    """Write a minimal queue.json matching Node 2's schema v1."""
    # Mimic Node 2's batched shape.
    batches = [shots[i : i + batch_size] for i in range(0, len(shots), batch_size)]
    queue_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "projectName": project_name,
                "batchSize": batch_size,
                "totalShots": len(shots),
                "batchCount": len(batches),
                "batches": batches,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def two_shot_queue(tmp_path: Path) -> Path:
    """Build a queue with two shots whose MP4s exist and have known frame counts.

    shot_001: 5 frames, metadata says 5  -> no warning
    shot_002: 3 frames, metadata says 3  -> no warning
    """
    mp4_1 = tmp_path / "scene01_shot01.mp4"
    mp4_2 = tmp_path / "scene01_shot02.mp4"
    _make_mp4(mp4_1, num_frames=5)
    _make_mp4(mp4_2, num_frames=3)
    shots = [
        {
            "shotId": "shot_001",
            "mp4Path": str(mp4_1),
            "durationFrames": 5,
            "durationSeconds": 0.2,
            "characters": [],
        },
        {
            "shotId": "shot_002",
            "mp4Path": str(mp4_2),
            "durationFrames": 3,
            "durationSeconds": 0.12,
            "characters": [],
        },
    ]
    queue_path = tmp_path / "queue.json"
    _write_queue(queue_path, shots)
    return queue_path


# ---------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------

class TestHappyPath:
    def test_extracts_every_shot(self, two_shot_queue: Path, tmp_path: Path):
        work = tmp_path / "work"
        result = extract_frames_for_queue(two_shot_queue, work)

        assert isinstance(result, Node3Result)
        assert len(result.shots) == 2
        assert result.warnings == []

        # shot_001 -> 5 frames
        s1 = next(s for s in result.shots if s.shotId == "shot_001")
        assert s1.actualFrames == 5
        assert s1.expectedFrames == 5
        assert s1.frameFilenames == [f"frame_{i:04d}.png" for i in range(1, 6)]
        for name in s1.frameFilenames:
            assert (Path(s1.framesDir) / name).is_file()

        # shot_002 -> 3 frames
        s2 = next(s for s in result.shots if s.shotId == "shot_002")
        assert s2.actualFrames == 3
        assert s2.frameFilenames == [f"frame_{i:04d}.png" for i in range(1, 4)]

    def test_aggregate_json_written(self, two_shot_queue: Path, tmp_path: Path):
        work = tmp_path / "work"
        extract_frames_for_queue(two_shot_queue, work)
        agg_path = work / "node3_result.json"
        assert agg_path.is_file()
        payload = json.loads(agg_path.read_text(encoding="utf-8"))
        assert payload["schemaVersion"] == 1
        assert payload["projectName"] == "ChhotaBhim_Ep042"
        assert len(payload["shots"]) == 2
        assert payload["warnings"] == []

    def test_per_shot_manifest_written(self, two_shot_queue: Path, tmp_path: Path):
        work = tmp_path / "work"
        extract_frames_for_queue(two_shot_queue, work)
        manifest_path = work / "shot_001" / "_manifest.json"
        assert manifest_path.is_file()
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert m["shotId"] == "shot_001"
        assert m["actualFrames"] == 5
        assert m["warning"] is None
        assert len(m["frameFilenames"]) == 5

    def test_rerun_overwrites_cleanly(self, two_shot_queue: Path, tmp_path: Path):
        """Second run with a different MP4 should not leave stale frames."""
        work = tmp_path / "work"
        extract_frames_for_queue(two_shot_queue, work)
        # Rebuild shot_001 with fewer frames.
        mp4_1 = tmp_path / "scene01_shot01.mp4"
        _make_mp4(mp4_1, num_frames=2)
        # Update queue to declare 2 frames so no warning fires.
        shots = [
            {
                "shotId": "shot_001",
                "mp4Path": str(mp4_1),
                "durationFrames": 2,
                "durationSeconds": 0.08,
                "characters": [],
            },
        ]
        _write_queue(two_shot_queue, shots)
        extract_frames_for_queue(two_shot_queue, work)
        remaining = sorted((work / "shot_001").glob("frame_*.png"))
        assert len(remaining) == 2  # not 5


# ---------------------------------------------------------------
# Frame-count warning behavior (not an error)
# ---------------------------------------------------------------

class TestFrameCountWarning:
    def test_actual_less_than_expected_emits_warning(self, tmp_path: Path):
        mp4 = tmp_path / "short.mp4"
        _make_mp4(mp4, num_frames=4)  # actual: 4
        shots = [
            {
                "shotId": "shot_001",
                "mp4Path": str(mp4),
                "durationFrames": 10,  # expected: 10
                "durationSeconds": 0.4,
                "characters": [],
            }
        ]
        queue = tmp_path / "queue.json"
        _write_queue(queue, shots)

        result = extract_frames_for_queue(queue, tmp_path / "work")
        assert len(result.warnings) == 1
        w = result.warnings[0]
        assert w.shotId == "shot_001"
        assert w.expectedFrames == 10
        assert w.actualFrames == 4
        # Warning is recorded but does NOT abort: frames still extracted.
        assert result.shots[0].actualFrames == 4

    def test_actual_greater_than_expected_emits_warning(self, tmp_path: Path):
        mp4 = tmp_path / "long.mp4"
        _make_mp4(mp4, num_frames=10)
        shots = [
            {
                "shotId": "shot_001",
                "mp4Path": str(mp4),
                "durationFrames": 5,
                "durationSeconds": 0.2,
                "characters": [],
            }
        ]
        queue = tmp_path / "queue.json"
        _write_queue(queue, shots)

        result = extract_frames_for_queue(queue, tmp_path / "work")
        assert len(result.warnings) == 1
        assert result.warnings[0].actualFrames == 10


# ---------------------------------------------------------------
# Queue-input failures (QueueInputError)
# ---------------------------------------------------------------

class TestQueueInput:
    def test_missing_queue_file(self, tmp_path: Path):
        with pytest.raises(QueueInputError, match="queue.json not found"):
            extract_frames_for_queue(tmp_path / "nope.json", tmp_path / "w")

    def test_malformed_json(self, tmp_path: Path):
        q = tmp_path / "queue.json"
        q.write_text("{ not valid json")
        with pytest.raises(QueueInputError, match="not valid JSON"):
            extract_frames_for_queue(q, tmp_path / "w")

    def test_missing_batches_key(self, tmp_path: Path):
        q = tmp_path / "queue.json"
        q.write_text(json.dumps({"schemaVersion": 1, "projectName": "x"}))
        with pytest.raises(QueueInputError, match="missing a 'batches' list"):
            extract_frames_for_queue(q, tmp_path / "w")

    def test_wrong_schema_version(self, tmp_path: Path):
        q = tmp_path / "queue.json"
        q.write_text(json.dumps({"schemaVersion": 2, "batches": []}))
        with pytest.raises(QueueInputError, match="unsupported schemaVersion"):
            extract_frames_for_queue(q, tmp_path / "w")

    def test_shot_missing_mp4_on_disk(self, tmp_path: Path):
        shots = [
            {
                "shotId": "shot_001",
                "mp4Path": str(tmp_path / "ghost.mp4"),  # doesn't exist
                "durationFrames": 5,
                "durationSeconds": 0.2,
                "characters": [],
            }
        ]
        q = tmp_path / "queue.json"
        _write_queue(q, shots)
        with pytest.raises(QueueInputError, match="does not exist"):
            extract_frames_for_queue(q, tmp_path / "w")

    def test_shot_missing_required_field(self, tmp_path: Path):
        q = tmp_path / "queue.json"
        q.write_text(json.dumps({
            "schemaVersion": 1,
            "projectName": "x",
            "batches": [[{"shotId": "shot_001"}]],  # missing mp4Path + durationFrames
        }))
        with pytest.raises(QueueInputError, match="missing 'mp4Path'"):
            extract_frames_for_queue(q, tmp_path / "w")


# ---------------------------------------------------------------
# ffmpeg-level failures (FFmpegError)
# ---------------------------------------------------------------

class TestFFmpegFailures:
    def test_corrupt_mp4_raises_ffmpeg_error(self, tmp_path: Path):
        bogus = tmp_path / "not_really.mp4"
        bogus.write_bytes(b"this is not an mp4 at all")
        shots = [
            {
                "shotId": "shot_001",
                "mp4Path": str(bogus),
                "durationFrames": 5,
                "durationSeconds": 0.2,
                "characters": [],
            }
        ]
        q = tmp_path / "queue.json"
        _write_queue(q, shots)
        with pytest.raises(FFmpegError):
            extract_frames_for_queue(q, tmp_path / "w")


# ---------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------

class TestCLI:
    def test_cli_success(self, two_shot_queue: Path, tmp_path: Path, capsys):
        work = tmp_path / "work"
        rc = cli_main([
            "--queue", str(two_shot_queue),
            "--work-dir", str(work),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[node3] OK" in out
        assert "0 warnings" in out
        assert (work / "node3_result.json").is_file()

    def test_cli_quiet(self, two_shot_queue: Path, tmp_path: Path, capsys):
        rc = cli_main([
            "--queue", str(two_shot_queue),
            "--work-dir", str(tmp_path / "w"),
            "--quiet",
        ])
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_cli_failure_returns_1(self, tmp_path: Path, capsys):
        rc = cli_main([
            "--queue", str(tmp_path / "nope.json"),
            "--work-dir", str(tmp_path / "w"),
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "[node3] FAILED" in err

    def test_cli_warning_still_exit_zero(self, tmp_path: Path, capsys):
        """Warnings are non-fatal: CLI must still exit 0 and list them."""
        mp4 = tmp_path / "s.mp4"
        _make_mp4(mp4, num_frames=3)
        shots = [{
            "shotId": "shot_001",
            "mp4Path": str(mp4),
            "durationFrames": 10,
            "durationSeconds": 0.4,
            "characters": [],
        }]
        q = tmp_path / "queue.json"
        _write_queue(q, shots)
        rc = cli_main(["--queue", str(q), "--work-dir", str(tmp_path / "w")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "1 warning" in out
        assert "WARN:" in out


# ---------------------------------------------------------------
# Exception-hierarchy sanity
# ---------------------------------------------------------------

def test_all_node3_errors_are_pipeline_errors():
    for cls in (QueueInputError, FFmpegError, FrameExtractionError):
        assert issubclass(cls, Node3Error)
        assert issubclass(cls, PipelineError)


def test_node3_error_distinct_from_node2_error():
    """Node 2 and Node 3 share the PipelineError root but are otherwise siblings."""
    from pipeline.errors import Node2Error
    assert not issubclass(Node3Error, Node2Error)
    assert not issubclass(Node2Error, Node3Error)


# ---------------------------------------------------------------
# extract_frames_for_shot direct entry point
# ---------------------------------------------------------------

def test_extract_for_shot_direct(tmp_path: Path):
    """Exercise the per-shot API so ComfyUI wrappers can reuse it."""
    mp4 = tmp_path / "s.mp4"
    _make_mp4(mp4, num_frames=4)
    shot_result, warning = extract_frames_for_shot(
        mp4_path=mp4,
        out_dir=tmp_path / "out",
        expected_frames=4,
        shot_id="shot_001",
    )
    assert warning is None
    assert shot_result.actualFrames == 4
    assert (tmp_path / "out" / "_manifest.json").is_file()
