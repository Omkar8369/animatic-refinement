"""Pytest suite for Node 10 - Output Generation (PNG -> MP4).

Mirrors the test layout of test_node5/6/7/8/9.py:

  * Tiny helpers synthesize a Node 9-output-shaped fixture
    (node9_result.json + timed_map.json + timed/frame_NNNN.png) in
    `tmp_path`, then exercise Node 10's load + encode + verify chain.
  * Each test hits one locked decision, one error path, or one CLI
    behavior.

Unlike Nodes 8/9 these tests invoke REAL ffmpeg (via imageio-ffmpeg's
static binary). Encoding is measurable but small fixtures keep each
ffmpeg call to ~0.3-1.0 s. To bound total test time we use 32x32
canvases and 5-frame shots wherever possible.

Locked decisions tested (CLAUDE.md "Node 10 - locked decisions"):
  1.  ffmpeg via imageio-ffmpeg.                       [implicit -- import]
  2.  Codec H.264.                                      [test_output_codec_is_h264]
  3.  yuv420p pixel format.                             [test_output_codec_is_h264]
  4.  CRF 18 default + tunable.                         [test_crf_default_18 / test_crf_override]
  6.  25 FPS hardcoded.                                 [test_output_fps_is_25]
  7.  Output to <work>/output/.                         [test_output_location]
  8.  Filename = <shotId>_refined.mp4.                  [test_output_filename]
  9.  ffprobe-style verify (frame count + duration).    [test_verify_frame_count_match]
  10. Don't delete upstream artifacts.                  [test_does_not_delete_inputs]
  11. Odd dims = hard error.                            [test_odd_dims_raise]
  12. ffmpeg non-zero raises with stderr.               [implicit -- handled by _ffmpeg_encode]
  13. Missing PNG in 1..N raises.                       [test_missing_frame_raises]
  14. nb_frames mismatch raises.                        [via verify -- caught by ffmpeg's own count]
  15. --node9-result only.                              [test_chases_timed_map_from_path]
  17. Rerun overwrites with -y.                         [test_rerun_overwrites]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import imageio_ffmpeg
import pytest
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from pipeline.errors import (  # noqa: E402
    FFmpegEncodeError,
    Node9ResultInputError,
    Node10Error,
    PipelineError,
    TimedFramesError,
)
from pipeline.node10 import (  # noqa: E402
    CODEC_LABEL,
    DEFAULT_CRF,
    DEFAULT_FPS,
    Node10Result,
    ShotEncodeSummary,
    _ffmpeg_encode,
    _probe_canvas_dims,
    _verify_output,
    _verify_timed_frames,
    encode_for_queue,
    load_node9_result,
    load_timed_map,
)


# -------------------------------------------------------------------
# Test fixtures
# -------------------------------------------------------------------

# Small dims keep ffmpeg encodes fast. 32x32 satisfies libx264's
# even-side requirement; 5 frames per shot is enough for verify
# checks. We use 8 frames in the multi-frame tests to give ffmpeg's
# count_frames_and_secs a stable target.
SMALL_W, SMALL_H = 32, 32
SMALL_FRAMES = 5


def _draw_frame(path: Path, frame_idx: int,
                w: int = SMALL_W, h: int = SMALL_H) -> None:
    """A tiny 32x32 frame with a moving dot so each frame is
    visually distinct (helps ffmpeg compress correctly + makes
    nb_frames probes deterministic)."""
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # A 4x4 black square at column == frame_idx to make each frame
    # unique. Wraps if frame_idx >= w-4.
    x = (frame_idx * 4) % max(1, w - 4)
    draw.rectangle((x, 8, x + 4, 12), fill=(0, 0, 0))
    img.save(path, "PNG")


def _make_workdir(
    tmp_path: Path,
    *,
    frames: int = SMALL_FRAMES,
    canvas: tuple[int, int] = (SMALL_W, SMALL_H),
    project: str = "test10",
    skip_frames: list[int] | None = None,
    n_shots: int = 1,
) -> dict[str, Any]:
    """Build a Node 9-output-shaped scaffold. Returns dict with
    paths."""
    skip_frames = skip_frames or []
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    n9_shots = []
    shot_paths = []
    for s in range(n_shots):
        shot_id = f"shot_{s + 1:03d}"
        shot_root = work_dir / shot_id
        timed_dir = shot_root / "timed"
        timed_dir.mkdir(parents=True, exist_ok=True)

        # Synthesize timed/ frames
        for i in range(1, frames + 1):
            if i in skip_frames:
                continue
            _draw_frame(timed_dir / f"frame_{i:04d}.png", i,
                        canvas[0], canvas[1])

        # timed_map.json (per-shot, Node 9 output)
        timed_map = {
            "schemaVersion": 1,
            "shotId": shot_id,
            "timedDir": str(timed_dir),
            "totalFrames": frames,
            "frames": [
                {
                    "frameIndex": i,
                    "sourceKeyPoseIndex": 0,
                    "offset": [0, 0],
                    "composedSourcePath": str(shot_root / "composed" / "000_composite.png"),
                    "timedPath": str(timed_dir / f"frame_{i:04d}.png"),
                    "isAnchor": (i == 1),
                }
                for i in range(1, frames + 1)
            ],
        }
        timed_map_path = shot_root / "timed_map.json"
        timed_map_path.write_text(json.dumps(timed_map, indent=2))

        n9_shots.append({
            "shotId": shot_id,
            "totalFrames": frames,
            "keyPoseCount": 1,
            "anchorCount": 1,
            "heldCount": frames - 1,
            "timedMapPath": str(timed_map_path),
        })
        shot_paths.append({
            "shot_id": shot_id,
            "shot_root": shot_root,
            "timed_dir": timed_dir,
            "timed_map_path": timed_map_path,
        })

    n9 = {
        "schemaVersion": 1,
        "projectName": project,
        "workDir": str(work_dir),
        "reconstructedAt": "2026-04-25T00:00:00+00:00",
        "shots": n9_shots,
    }
    n9_path = work_dir / "node9_result.json"
    n9_path.write_text(json.dumps(n9, indent=2))

    return {
        "work_dir": work_dir,
        "node9_result_path": n9_path,
        "shot_paths": shot_paths,
        "expected_frames": frames,
        "canvas": canvas,
    }


# -------------------------------------------------------------------
# Error-hierarchy invariants
# -------------------------------------------------------------------

class TestErrorHierarchy:

    def test_node10_error_is_pipeline_error(self):
        assert issubclass(Node10Error, PipelineError)

    def test_subclasses_are_node10_errors(self):
        assert issubclass(Node9ResultInputError, Node10Error)
        assert issubclass(TimedFramesError, Node10Error)
        assert issubclass(FFmpegEncodeError, Node10Error)


# -------------------------------------------------------------------
# 10A - Input validation
# -------------------------------------------------------------------

class TestLoadNode9Result:

    def test_loads_valid(self, tmp_path):
        env = _make_workdir(tmp_path)
        loaded = load_node9_result(env["node9_result_path"])
        assert loaded["schemaVersion"] == 1
        assert len(loaded["shots"]) == 1

    def test_missing_raises(self, tmp_path):
        with pytest.raises(Node9ResultInputError, match="not found"):
            load_node9_result(tmp_path / "absent.json")

    def test_invalid_json_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not json")
        with pytest.raises(Node9ResultInputError, match="not valid JSON"):
            load_node9_result(bad)

    def test_wrong_schema_version_raises(self, tmp_path):
        env = _make_workdir(tmp_path)
        path = env["node9_result_path"]
        data = json.loads(path.read_text())
        data["schemaVersion"] = 99
        path.write_text(json.dumps(data))
        with pytest.raises(Node9ResultInputError, match="schemaVersion"):
            load_node9_result(path)

    def test_missing_timed_map_path_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({
            "schemaVersion": 1, "workDir": "/tmp",
            "shots": [{"shotId": "shot_001"}],
        }))
        with pytest.raises(Node9ResultInputError, match="timedMapPath"):
            load_node9_result(bad)


class TestLoadTimedMap:

    def test_loads_valid(self, tmp_path):
        env = _make_workdir(tmp_path)
        sp = env["shot_paths"][0]
        loaded = load_timed_map(sp["timed_map_path"], sp["shot_id"])
        assert loaded["totalFrames"] == env["expected_frames"]

    def test_wrong_shot_id_raises(self, tmp_path):
        env = _make_workdir(tmp_path)
        sp = env["shot_paths"][0]
        with pytest.raises(Node9ResultInputError, match="Stale work dir"):
            load_timed_map(sp["timed_map_path"], "shot_999")

    def test_zero_total_frames_rejected(self, tmp_path):
        env = _make_workdir(tmp_path)
        sp = env["shot_paths"][0]
        path = sp["timed_map_path"]
        data = json.loads(path.read_text())
        data["totalFrames"] = 0
        path.write_text(json.dumps(data))
        with pytest.raises(Node9ResultInputError, match="positive int"):
            load_timed_map(path, sp["shot_id"])


# -------------------------------------------------------------------
# 10A - Frame existence
# -------------------------------------------------------------------

class TestVerifyTimedFrames:

    def test_passes_with_full_sequence(self, tmp_path):
        env = _make_workdir(tmp_path)
        # Should not raise
        _verify_timed_frames(
            env["shot_paths"][0]["timed_dir"],
            env["expected_frames"],
            "shot_001",
        )

    def test_missing_frame_raises(self, tmp_path):
        """Locked decision #13."""
        env = _make_workdir(tmp_path, skip_frames=[3])
        with pytest.raises(TimedFramesError, match="missing"):
            _verify_timed_frames(
                env["shot_paths"][0]["timed_dir"],
                env["expected_frames"],
                "shot_001",
            )

    def test_missing_directory_raises(self, tmp_path):
        with pytest.raises(TimedFramesError, match="not found"):
            _verify_timed_frames(tmp_path / "nonexistent", 5, "shot_001")


# -------------------------------------------------------------------
# 10B - Canvas dim probe (locked decision #11)
# -------------------------------------------------------------------

class TestProbeCanvasDims:

    def test_even_dims_accepted(self, tmp_path):
        env = _make_workdir(tmp_path, canvas=(64, 32))
        w, h = _probe_canvas_dims(
            env["shot_paths"][0]["timed_dir"],
            "shot_001",
        )
        assert (w, h) == (64, 32)

    def test_odd_width_raises(self, tmp_path):
        """Locked decision #11. libx264 requires even W. Must NOT
        auto-pad (would silently desync Node 9 positions)."""
        env = _make_workdir(tmp_path, canvas=(33, 32))
        with pytest.raises(FFmpegEncodeError, match="odd"):
            _probe_canvas_dims(
                env["shot_paths"][0]["timed_dir"],
                "shot_001",
            )

    def test_odd_height_raises(self, tmp_path):
        env = _make_workdir(tmp_path, canvas=(32, 33))
        with pytest.raises(FFmpegEncodeError, match="odd"):
            _probe_canvas_dims(
                env["shot_paths"][0]["timed_dir"],
                "shot_001",
            )

    def test_missing_probe_target_raises(self, tmp_path):
        env = _make_workdir(tmp_path, skip_frames=[1])
        with pytest.raises(FFmpegEncodeError, match="probe"):
            _probe_canvas_dims(
                env["shot_paths"][0]["timed_dir"],
                "shot_001",
            )


# -------------------------------------------------------------------
# 10C - ffmpeg encode (real subprocess)
# -------------------------------------------------------------------

class TestFfmpegEncode:

    def test_encodes_a_tiny_sequence(self, tmp_path):
        env = _make_workdir(tmp_path)
        sp = env["shot_paths"][0]
        out = tmp_path / "out.mp4"
        _ffmpeg_encode(
            timed_dir=sp["timed_dir"],
            output_path=out,
            crf=DEFAULT_CRF,
            fps=DEFAULT_FPS,
            shot_id="shot_001",
        )
        assert out.is_file()
        assert out.stat().st_size > 0

    def test_raises_on_nonexistent_pattern(self, tmp_path):
        out = tmp_path / "out.mp4"
        with pytest.raises(FFmpegEncodeError, match="ffmpeg exit"):
            _ffmpeg_encode(
                timed_dir=tmp_path / "no_such_dir",
                output_path=out,
                crf=DEFAULT_CRF,
                fps=DEFAULT_FPS,
                shot_id="shot_001",
            )


# -------------------------------------------------------------------
# 10D - Post-encode verify (real ffmpeg count_frames_and_secs)
# -------------------------------------------------------------------

class TestVerifyOutput:

    def _encode_then_verify(self, tmp_path, frames):
        env = _make_workdir(tmp_path, frames=frames)
        sp = env["shot_paths"][0]
        out = tmp_path / "out.mp4"
        _ffmpeg_encode(sp["timed_dir"], out, DEFAULT_CRF, DEFAULT_FPS, "shot_001")
        return out

    def test_verify_frame_count_match(self, tmp_path):
        """Locked decision #9 + #14."""
        out = self._encode_then_verify(tmp_path, frames=8)
        verify = _verify_output(out, expected_frames=8,
                                expected_fps=DEFAULT_FPS,
                                shot_id="shot_001")
        assert verify["frameCount"] in (7, 8, 9)  # +/-1 tolerance
        assert verify["fileSizeBytes"] > 0
        # 8 frames @ 25 FPS = 0.32s; allow generous tolerance for codec
        assert 0.0 < verify["durationSeconds"] < 1.0

    def test_verify_missing_output_raises(self, tmp_path):
        with pytest.raises(FFmpegEncodeError, match="missing on disk"):
            _verify_output(tmp_path / "absent.mp4", 5, DEFAULT_FPS, "shot_001")

    def test_verify_empty_output_raises(self, tmp_path):
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        with pytest.raises(FFmpegEncodeError, match="empty"):
            _verify_output(empty, 5, DEFAULT_FPS, "shot_001")

    def test_verify_frame_count_mismatch_raises(self, tmp_path):
        out = self._encode_then_verify(tmp_path, frames=5)
        # Lie about expected -- the encoded file has ~5 frames; we
        # claim 50 expected.
        with pytest.raises(FFmpegEncodeError, match="expected 50"):
            _verify_output(out, expected_frames=50,
                           expected_fps=DEFAULT_FPS,
                           shot_id="shot_001")


# -------------------------------------------------------------------
# Top-level driver
# -------------------------------------------------------------------

class TestEncodeForQueue:

    def test_writes_mp4_in_output_dir(self, tmp_path):
        """Locked decision #7 + #8."""
        env = _make_workdir(tmp_path)
        result = encode_for_queue(node9_result_path=env["node9_result_path"])
        assert isinstance(result, Node10Result)
        assert len(result.shots) == 1
        s = result.shots[0]
        assert s.shotId == "shot_001"
        assert Path(s.outputPath).name == "shot_001_refined.mp4"
        # Output is under <work-dir>/output/
        assert Path(s.outputPath).parent.name == "output"
        assert Path(s.outputPath).is_file()

    def test_aggregate_node10_result_written(self, tmp_path):
        env = _make_workdir(tmp_path)
        encode_for_queue(node9_result_path=env["node9_result_path"])
        agg_path = env["work_dir"] / "node10_result.json"
        assert agg_path.is_file()
        agg = json.loads(agg_path.read_text())
        assert agg["schemaVersion"] == 1
        assert agg["projectName"] == "test10"
        assert agg["crf"] == DEFAULT_CRF
        assert agg["outputDir"] == str(env["work_dir"] / "output")
        assert len(agg["shots"]) == 1

    def test_output_codec_is_h264(self, tmp_path):
        """Locked decisions #2 + #3 (implicit -- if libx264 / yuv420p
        weren't accepted, ffmpeg would have errored)."""
        env = _make_workdir(tmp_path)
        result = encode_for_queue(node9_result_path=env["node9_result_path"])
        assert result.shots[0].codec == CODEC_LABEL  # "h264"

    def test_output_fps_is_25(self, tmp_path):
        """Locked decision #6."""
        env = _make_workdir(tmp_path)
        result = encode_for_queue(node9_result_path=env["node9_result_path"])
        assert result.shots[0].fps == 25

    def test_crf_default_18(self, tmp_path):
        """Locked decision #4."""
        env = _make_workdir(tmp_path)
        result = encode_for_queue(node9_result_path=env["node9_result_path"])
        assert result.crf == 18

    def test_crf_override(self, tmp_path):
        """Locked decision #4 -- CRF is the single quality knob."""
        env = _make_workdir(tmp_path)
        result = encode_for_queue(
            node9_result_path=env["node9_result_path"],
            crf=23,
        )
        assert result.crf == 23

    def test_odd_dims_raise(self, tmp_path):
        """Locked decision #11."""
        env = _make_workdir(tmp_path, canvas=(33, 32))
        with pytest.raises(FFmpegEncodeError, match="odd"):
            encode_for_queue(node9_result_path=env["node9_result_path"])

    def test_missing_frame_raises(self, tmp_path):
        """Locked decision #13."""
        env = _make_workdir(tmp_path, skip_frames=[3])
        with pytest.raises(TimedFramesError, match="missing"):
            encode_for_queue(node9_result_path=env["node9_result_path"])

    def test_chases_timed_map_from_path(self, tmp_path):
        """Locked decision #15 -- only --node9-result needed."""
        env = _make_workdir(tmp_path)
        # Verify Node 10 didn't need any other --flag
        result = encode_for_queue(node9_result_path=env["node9_result_path"])
        assert result.shots[0].frameCount > 0

    def test_does_not_delete_inputs(self, tmp_path):
        """Locked decision #10 -- timed/ stays after encode."""
        env = _make_workdir(tmp_path)
        sp = env["shot_paths"][0]
        timed_pngs_before = sorted(p.name for p in sp["timed_dir"].glob("*.png"))
        encode_for_queue(node9_result_path=env["node9_result_path"])
        timed_pngs_after = sorted(p.name for p in sp["timed_dir"].glob("*.png"))
        assert timed_pngs_before == timed_pngs_after
        assert len(timed_pngs_after) == env["expected_frames"]

    def test_rerun_overwrites(self, tmp_path):
        """Locked decision #17 -- ffmpeg -y handles rerun atomicity."""
        env = _make_workdir(tmp_path)
        encode_for_queue(node9_result_path=env["node9_result_path"])
        out_path = env["work_dir"] / "output" / "shot_001_refined.mp4"
        first_size = out_path.stat().st_size
        # Re-run; should not error
        result = encode_for_queue(node9_result_path=env["node9_result_path"])
        # File still exists, was overwritten (mtime changed; size should
        # match within encoder determinism for the same input)
        assert out_path.is_file()
        assert result.shots[0].fileSizeBytes > 0

    def test_multi_shot(self, tmp_path):
        env = _make_workdir(tmp_path, n_shots=2)
        result = encode_for_queue(node9_result_path=env["node9_result_path"])
        assert len(result.shots) == 2
        for s in result.shots:
            assert Path(s.outputPath).is_file()
        names = sorted(p.name for p in (env["work_dir"] / "output").glob("*.mp4"))
        assert names == ["shot_001_refined.mp4", "shot_002_refined.mp4"]

    def test_summary_size_is_file_size(self, tmp_path):
        """fileSizeBytes in the summary matches actual file size."""
        env = _make_workdir(tmp_path)
        result = encode_for_queue(node9_result_path=env["node9_result_path"])
        s = result.shots[0]
        assert s.fileSizeBytes == Path(s.outputPath).stat().st_size

    def test_output_filename_pattern(self, tmp_path):
        """Locked decision #8."""
        env = _make_workdir(tmp_path, n_shots=2)
        encode_for_queue(node9_result_path=env["node9_result_path"])
        out_dir = env["work_dir"] / "output"
        for shot_id in ("shot_001", "shot_002"):
            assert (out_dir / f"{shot_id}_refined.mp4").is_file()


# -------------------------------------------------------------------
# CLI subprocess
# -------------------------------------------------------------------

class TestCli:

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "run_node10.py"), *args],
            capture_output=True,
            text=True,
        )

    def test_cli_exit_0_on_success(self, tmp_path):
        env = _make_workdir(tmp_path)
        r = self._run("--node9-result", str(env["node9_result_path"]))
        assert r.returncode == 0, (
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert "[node10] OK" in r.stdout

    def test_cli_quiet_suppresses_summary(self, tmp_path):
        env = _make_workdir(tmp_path)
        r = self._run("--node9-result", str(env["node9_result_path"]),
                      "--quiet")
        assert r.returncode == 0
        assert "[node10] OK" not in r.stdout

    def test_cli_exit_1_on_typed_error(self, tmp_path):
        bad = tmp_path / "missing.json"
        r = self._run("--node9-result", str(bad))
        assert r.returncode == 1
        assert "[node10] FAILED" in r.stderr

    def test_cli_summary_reports_frames_and_size(self, tmp_path):
        env = _make_workdir(tmp_path)
        r = self._run("--node9-result", str(env["node9_result_path"]))
        assert r.returncode == 0
        assert "frame(s) encoded" in r.stdout
        assert "MB" in r.stdout
        assert "crf=18" in r.stdout

    def test_cli_crf_override(self, tmp_path):
        env = _make_workdir(tmp_path)
        r = self._run("--node9-result", str(env["node9_result_path"]),
                      "--crf", "23")
        assert r.returncode == 0
        assert "crf=23" in r.stdout
