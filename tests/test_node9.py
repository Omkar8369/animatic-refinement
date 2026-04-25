"""Pytest suite for Node 9 - Timing Reconstruction.

Mirrors the test layout of test_node5/6/7/8.py:

  * Tiny helpers synthesize a Node 8-output-shaped fixture
    (node8_result.json + composed_map.json + composed/ + sibling
    keypose_map.json) directly in `tmp_path`.
  * Each test hits one locked decision, one error path, or one CLI
    behavior.

We do NOT spin up ComfyUI; Node 9 is pure-Python translate-and-copy.

Locked decisions tested (CLAUDE.md "Node 9 - locked decisions"):
  1.  Translate-and-copy on white canvas, NO AI.       [test_held_frame_translate_*]
  2.  Whole-frame translation, not per-character.       [implicit -- algorithm is whole-image paste]
  3.  Output canvas = composite size = source MP4 res.  [test_output_dims_match_composite]
  4.  Exposed-region fill = solid white.                [test_off_canvas_translate_is_white]
  5.  Output frame numbering = frame_NNNN.png.          [test_output_filenames_4digit]
  6.  --node8-result only (chases other manifests).     [test_chases_keypose_map_from_shot_root]
  7.  Fail-loud on missing composed PNG.                [test_missing_composed_png_raises]
  8.  totalFrames mismatch is a hard error.             [test_total_frames_mismatch_raises]
  9.  Off-canvas translates are NOT errors.             [test_off_canvas_translate_is_white]
  10. Same frame in multiple keyPoses = hard error.     [test_duplicate_frame_index_raises]
  11. Pure-Python.                                      [implicit -- imports]
  12. Single-threaded.                                  [n/a]
  13. Rerun wipes <shot>/timed/.                        [test_rerun_wipes_timed]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from pipeline.errors import (  # noqa: E402
    FrameCountMismatchError,
    KeyPoseMapInputError,
    Node8ResultInputError,
    Node9Error,
    PipelineError,
    TimingReconstructionError,
)
from pipeline.node9 import (  # noqa: E402
    Node9Result,
    TimedFrameRecord,
    TimedMap,
    _build_composite_path_lookup,
    _build_frame_lookup,
    _translate_and_copy,
    load_composed_map,
    load_keypose_map,
    load_node8_result,
    reconstruct_timing_for_queue,
)


# -------------------------------------------------------------------
# Test fixtures
# -------------------------------------------------------------------

CANVAS_W, CANVAS_H = 256, 192


def _draw_distinctive_composite(path: Path, marker_color: tuple[int, int, int]) -> None:
    """A composite PNG with a distinctive colored mark in the
    upper-left corner so we can verify which composite ended up at a
    given frame after translation."""
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Big rectangle in upper-left (so even after translation it's
    # easy to find)
    draw.rectangle((20, 20, 80, 80), fill=marker_color)
    # Text-like horizontal bar centered to mark vertical center
    draw.rectangle((20, 100, 100, 110), fill=marker_color)
    img.save(path, "PNG")


def _make_workdir(
    tmp_path: Path,
    *,
    keypose_count: int = 2,
    held_per_keypose: list[int] | None = None,
    held_offsets: list[list[list[int]]] | None = None,
    project: str = "test9",
    skip_compose_keypose: int | None = None,
    duplicate_frame: tuple[int, int] | None = None,  # (kp_idx, frame_to_dup)
    out_of_range_frame: int | None = None,
    bad_total_frames_offset: int = 0,
) -> dict[str, Any]:
    """Build a Node 8-output-shaped scaffold under `tmp_path`.

    Default: 2 key poses, 3 held frames each (so totalFrames=8 with
    anchors at frame 1 and frame 5).

    Returns dict with paths.
    """
    if held_per_keypose is None:
        held_per_keypose = [3] * keypose_count
    assert len(held_per_keypose) == keypose_count
    if held_offsets is None:
        # Default offsets: ramp dx by 5 per held frame
        held_offsets = [
            [[0, 5 * (i + 1)] for i in range(held_per_keypose[k])]
            for k in range(keypose_count)
        ]

    work_dir = tmp_path / "work"
    shot_id = "shot_001"
    shot_root = work_dir / shot_id
    composed_dir = shot_root / "composed"
    composed_dir.mkdir(parents=True, exist_ok=True)

    # Build keyPoses, anchors, heldFrames
    kp_records: list[dict[str, Any]] = []
    composed_keyposes: list[dict[str, Any]] = []
    next_frame = 1
    palette = [
        (220, 60, 60), (60, 160, 80), (80, 120, 220),
        (200, 180, 60), (220, 100, 180),
    ]
    for k in range(keypose_count):
        anchor_frame = next_frame
        held_count = held_per_keypose[k]
        held_frames = []
        offsets_for_k = held_offsets[k]
        for h in range(held_count):
            held_frames.append({
                "frame": anchor_frame + 1 + h,
                "offset": list(offsets_for_k[h]),
            })
        next_frame = anchor_frame + 1 + held_count

        kp_records.append({
            "keyPoseIndex": k,
            "sourceFrame": anchor_frame,
            "keyPoseFilename": f"frame_{anchor_frame:04d}.png",
            "heldFrames": held_frames,
        })

        # Synthesize composite PNG (unless skipped)
        comp_path = composed_dir / f"{k:03d}_composite.png"
        if k != skip_compose_keypose:
            _draw_distinctive_composite(comp_path, palette[k % len(palette)])
        composed_keyposes.append({
            "keyPoseIndex": k,
            "sourceFrame": anchor_frame,
            "composedPath": str(comp_path),
            "characters": [],
            "warnings": [],
        })

    total_frames = next_frame - 1
    # Allow caller to deliberately set a wrong totalFrames
    if bad_total_frames_offset:
        total_frames = total_frames + bad_total_frames_offset

    # Optionally inject duplicate-frame violation
    if duplicate_frame is not None:
        target_kp_idx, dup_frame = duplicate_frame
        kp_records[target_kp_idx]["heldFrames"].append({
            "frame": dup_frame,
            "offset": [0, 99],
        })

    # Optionally inject out-of-range frame
    if out_of_range_frame is not None:
        kp_records[0]["heldFrames"].append({
            "frame": out_of_range_frame,
            "offset": [0, 0],
        })

    # composed_map.json
    composed_map = {
        "schemaVersion": 1,
        "shotId": shot_id,
        "composedDir": str(composed_dir),
        "keyPoses": composed_keyposes,
    }
    composed_map_path = shot_root / "composed_map.json"
    composed_map_path.write_text(json.dumps(composed_map, indent=2))

    # keypose_map.json (Node 4 output, sibling)
    keypose_map = {
        "schemaVersion": 1,
        "shotId": shot_id,
        "totalFrames": total_frames,
        "sourceFramesDir": str(shot_root / "frames"),
        "keyPosesDir": str(shot_root / "keyposes"),
        "threshold": 8.0,
        "maxEdge": 128,
        "keyPoses": kp_records,
    }
    keypose_map_path = shot_root / "keypose_map.json"
    keypose_map_path.write_text(json.dumps(keypose_map, indent=2))

    # node8_result.json
    n8 = {
        "schemaVersion": 1,
        "projectName": project,
        "workDir": str(work_dir),
        "background": "white",
        "composedAt": "2026-04-25T00:00:00+00:00",
        "shots": [{
            "shotId": shot_id,
            "keyPoseCount": keypose_count,
            "composedCount": keypose_count,
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
        "composed_map_path": composed_map_path,
        "keypose_map_path": keypose_map_path,
        "node8_result_path": n8_path,
        "total_frames": total_frames,
    }


# -------------------------------------------------------------------
# Error-hierarchy invariants
# -------------------------------------------------------------------

class TestErrorHierarchy:

    def test_node9_error_is_pipeline_error(self):
        assert issubclass(Node9Error, PipelineError)

    def test_subclasses_are_node9_errors(self):
        assert issubclass(Node8ResultInputError, Node9Error)
        assert issubclass(KeyPoseMapInputError, Node9Error)
        assert issubclass(TimingReconstructionError, Node9Error)
        assert issubclass(FrameCountMismatchError, Node9Error)


# -------------------------------------------------------------------
# 9A - Input validation
# -------------------------------------------------------------------

class TestLoadNode8Result:

    def test_loads_valid(self, tmp_path):
        env = _make_workdir(tmp_path)
        loaded = load_node8_result(env["node8_result_path"])
        assert loaded["schemaVersion"] == 1

    def test_missing_raises(self, tmp_path):
        with pytest.raises(Node8ResultInputError, match="not found"):
            load_node8_result(tmp_path / "absent.json")

    def test_invalid_json_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not json")
        with pytest.raises(Node8ResultInputError, match="not valid JSON"):
            load_node8_result(bad)

    def test_wrong_schema_version_raises(self, tmp_path):
        env = _make_workdir(tmp_path)
        path = env["node8_result_path"]
        data = json.loads(path.read_text())
        data["schemaVersion"] = 99
        path.write_text(json.dumps(data))
        with pytest.raises(Node8ResultInputError, match="schemaVersion"):
            load_node8_result(path)

    def test_missing_composed_map_path_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({
            "schemaVersion": 1, "workDir": "/tmp",
            "shots": [{"shotId": "shot_001"}],
        }))
        with pytest.raises(Node8ResultInputError, match="composedMapPath"):
            load_node8_result(bad)


class TestLoadKeyposeMap:

    def test_loads_valid(self, tmp_path):
        env = _make_workdir(tmp_path)
        loaded = load_keypose_map(env["keypose_map_path"], "shot_001")
        assert loaded["totalFrames"] == env["total_frames"]

    def test_missing_raises(self, tmp_path):
        with pytest.raises(KeyPoseMapInputError, match="not found"):
            load_keypose_map(tmp_path / "absent.json", "shot_001")

    def test_wrong_shot_id_raises(self, tmp_path):
        env = _make_workdir(tmp_path)
        with pytest.raises(KeyPoseMapInputError, match="Stale work dir"):
            load_keypose_map(env["keypose_map_path"], "shot_002")

    def test_zero_total_frames_rejected(self, tmp_path):
        env = _make_workdir(tmp_path)
        path = env["keypose_map_path"]
        data = json.loads(path.read_text())
        data["totalFrames"] = 0
        path.write_text(json.dumps(data))
        with pytest.raises(KeyPoseMapInputError, match="positive int"):
            load_keypose_map(path, "shot_001")

    def test_empty_keyposes_rejected(self, tmp_path):
        env = _make_workdir(tmp_path)
        path = env["keypose_map_path"]
        data = json.loads(path.read_text())
        data["keyPoses"] = []
        path.write_text(json.dumps(data))
        with pytest.raises(KeyPoseMapInputError, match="non-empty"):
            load_keypose_map(path, "shot_001")


# -------------------------------------------------------------------
# 9B - Frame lookup table + Node 4 invariants
# -------------------------------------------------------------------

class TestBuildFrameLookup:

    def test_basic_lookup_covers_all_frames(self, tmp_path):
        env = _make_workdir(tmp_path)
        km = json.loads(env["keypose_map_path"].read_text())
        lookup = _build_frame_lookup(km, "shot_001")
        assert sorted(lookup.keys()) == list(range(1, env["total_frames"] + 1))

    def test_anchor_has_zero_offset_and_is_anchor_true(self, tmp_path):
        env = _make_workdir(tmp_path, keypose_count=2,
                            held_per_keypose=[2, 2])
        km = json.loads(env["keypose_map_path"].read_text())
        lookup = _build_frame_lookup(km, "shot_001")
        # anchor of kp 0 is frame 1; anchor of kp 1 is frame 4
        assert lookup[1] == (0, [0, 0], True)
        assert lookup[4] == (1, [0, 0], True)

    def test_held_frames_carry_offsets(self, tmp_path):
        env = _make_workdir(tmp_path, keypose_count=1,
                            held_per_keypose=[3],
                            held_offsets=[[[0, 5], [0, 10], [0, 15]]])
        km = json.loads(env["keypose_map_path"].read_text())
        lookup = _build_frame_lookup(km, "shot_001")
        assert lookup[2] == (0, [0, 5], False)
        assert lookup[3] == (0, [0, 10], False)
        assert lookup[4] == (0, [0, 15], False)

    def test_duplicate_frame_index_raises(self, tmp_path):
        # Inject frame 2 (already a held of kp 0) as ALSO a held of kp 1
        env = _make_workdir(
            tmp_path, keypose_count=2, held_per_keypose=[2, 2],
            duplicate_frame=(1, 2),
        )
        km = json.loads(env["keypose_map_path"].read_text())
        with pytest.raises(KeyPoseMapInputError, match="multiple keyPoses"):
            _build_frame_lookup(km, "shot_001")

    def test_out_of_range_frame_raises(self, tmp_path):
        env = _make_workdir(tmp_path, out_of_range_frame=99)
        km = json.loads(env["keypose_map_path"].read_text())
        with pytest.raises(KeyPoseMapInputError, match="outside"):
            _build_frame_lookup(km, "shot_001")

    def test_bad_offset_raises(self, tmp_path):
        env = _make_workdir(tmp_path)
        path = env["keypose_map_path"]
        data = json.loads(path.read_text())
        data["keyPoses"][0]["heldFrames"][0]["offset"] = [1, 2, 3]  # 3 ints
        path.write_text(json.dumps(data))
        with pytest.raises(KeyPoseMapInputError, match="offset"):
            _build_frame_lookup(data, "shot_001")

    def test_duplicate_keyposeindex_raises(self, tmp_path):
        env = _make_workdir(tmp_path, keypose_count=2, held_per_keypose=[1, 1])
        path = env["keypose_map_path"]
        data = json.loads(path.read_text())
        data["keyPoses"][1]["keyPoseIndex"] = 0  # duplicate
        path.write_text(json.dumps(data))
        with pytest.raises(KeyPoseMapInputError, match="duplicate keyPoseIndex"):
            _build_frame_lookup(data, "shot_001")


class TestBuildCompositePathLookup:

    def test_returns_kp_index_to_path(self, tmp_path):
        env = _make_workdir(tmp_path)
        cm = json.loads(env["composed_map_path"].read_text())
        out = _build_composite_path_lookup(cm, "shot_001")
        assert sorted(out.keys()) == [0, 1]
        assert "000_composite.png" in out[0]


# -------------------------------------------------------------------
# 9C - Translate-and-copy primitive
# -------------------------------------------------------------------

class TestTranslateAndCopy:

    def test_zero_offset_is_bit_identical(self):
        src = Image.new("RGB", (32, 32), (200, 100, 50))
        ImageDraw.Draw(src).rectangle((5, 5, 25, 25), fill=(0, 0, 0))
        out = _translate_and_copy(src, [0, 0])
        assert np.array_equal(np.asarray(src), np.asarray(out))

    def test_positive_offset_shifts_right_down(self):
        src = Image.new("RGB", (32, 32), (255, 255, 255))
        src.putpixel((0, 0), (0, 0, 0))
        out = _translate_and_copy(src, [3, 5])  # dy=3, dx=5
        arr = np.asarray(out)
        # Original (0, 0) black pixel now lands at (5, 3) -- (col, row)
        assert (arr[3, 5] == [0, 0, 0]).all()
        assert (arr[0, 0] == [255, 255, 255]).all()

    def test_negative_offset_shifts_left_up(self):
        src = Image.new("RGB", (32, 32), (255, 255, 255))
        src.putpixel((10, 10), (0, 0, 0))
        out = _translate_and_copy(src, [-3, -5])
        arr = np.asarray(out)
        # (10, 10) pixel now at (10-5, 10-3) = (5, 7)
        assert (arr[7, 5] == [0, 0, 0]).all()

    def test_off_canvas_translate_is_white(self):
        """Locked decision #9: pushing the character entirely off-canvas
        is NOT an error. Result is a fully-white frame."""
        src = Image.new("RGB", (32, 32), (255, 255, 255))
        src.putpixel((10, 10), (0, 0, 0))
        # Translate by (-100, -100): every source pixel lands off-canvas
        out = _translate_and_copy(src, [-100, -100])
        arr = np.asarray(out)
        assert (arr == 255).all()

    def test_exposed_region_filled_white(self):
        """Locked decision #4: the part of the canvas not covered by
        the shifted source must be white, NOT black or transparent."""
        src = Image.new("RGB", (32, 32), (200, 100, 50))  # solid color
        out = _translate_and_copy(src, [10, 10])
        arr = np.asarray(out)
        # Top-left strip is exposed -> must be white
        assert (arr[0, 0] == [255, 255, 255]).all()
        assert (arr[5, 5] == [255, 255, 255]).all()
        # Center is the shifted source -> must be the source color
        assert (arr[20, 20] == [200, 100, 50]).all()

    def test_output_dims_match_composite(self):
        src = Image.new("RGB", (123, 45), (0, 0, 0))
        out = _translate_and_copy(src, [10, 20])
        assert out.size == (123, 45)


# -------------------------------------------------------------------
# Top-level driver
# -------------------------------------------------------------------

class TestReconstructTimingForQueue:

    def test_anchors_are_bit_identical_to_composites(self, tmp_path):
        """Locked decision #1 (anchors) -- anchor frames should be
        bit-identical copies of Node 8's composite, since offset is
        [0, 0] and the code skips the translate-and-copy step."""
        env = _make_workdir(tmp_path, keypose_count=1,
                            held_per_keypose=[2])
        result = reconstruct_timing_for_queue(
            node8_result_path=env["node8_result_path"]
        )
        assert isinstance(result, Node9Result)
        timed_dir = env["shot_root"] / "timed"
        anchor_path = timed_dir / "frame_0001.png"
        composite_path = env["composed_dir"] / "000_composite.png"
        assert anchor_path.is_file()
        # Pixel-equal to the source composite
        a = np.asarray(Image.open(anchor_path).convert("RGB"))
        c = np.asarray(Image.open(composite_path).convert("RGB"))
        assert np.array_equal(a, c)

    def test_held_frame_translate_is_correct(self, tmp_path):
        """Locked decision #1 -- held frame is the anchor's composite
        translated by (dy, dx)."""
        env = _make_workdir(
            tmp_path, keypose_count=1, held_per_keypose=[1],
            held_offsets=[[[10, 20]]],  # frame 2: offset (10, 20)
        )
        reconstruct_timing_for_queue(
            node8_result_path=env["node8_result_path"]
        )
        timed_dir = env["shot_root"] / "timed"
        composite = np.asarray(
            Image.open(env["composed_dir"] / "000_composite.png").convert("RGB")
        )
        held = np.asarray(
            Image.open(timed_dir / "frame_0002.png").convert("RGB")
        )
        # Composite's top-left rectangle (20-80, 20-80) shifts to (40-100, 30-90)
        # in the held frame (dx=20, dy=10).
        assert (held[30, 40] == composite[20, 20]).all()
        # And the original (20, 20) position is now exposed = white
        # (only true if (20-dy=10, 20-dx=0) was white in source)
        # Check the truly-exposed band: (0, 0) onwards
        assert (held[0, 0] == [255, 255, 255]).all()

    def test_output_filenames_4digit(self, tmp_path):
        """Locked decision #5 -- 1-indexed, 4-digit zero-padded."""
        env = _make_workdir(tmp_path, keypose_count=1,
                            held_per_keypose=[2])
        reconstruct_timing_for_queue(
            node8_result_path=env["node8_result_path"]
        )
        timed_dir = env["shot_root"] / "timed"
        names = sorted(p.name for p in timed_dir.glob("frame_*.png"))
        assert names == ["frame_0001.png", "frame_0002.png", "frame_0003.png"]

    def test_total_frames_match_reconstructed(self, tmp_path):
        env = _make_workdir(tmp_path, keypose_count=2,
                            held_per_keypose=[3, 2])
        result = reconstruct_timing_for_queue(
            node8_result_path=env["node8_result_path"]
        )
        s = result.shots[0]
        assert s.totalFrames == 7  # 2 anchors + 5 held
        assert s.anchorCount == 2
        assert s.heldCount == 5
        assert s.keyPoseCount == 2
        # Files on disk
        timed_dir = env["shot_root"] / "timed"
        assert len(list(timed_dir.glob("frame_*.png"))) == 7

    def test_chases_keypose_map_from_shot_root(self, tmp_path):
        """Locked decision #6 -- only --node8-result needed; Node 9
        chases keypose_map.json from shot root."""
        env = _make_workdir(tmp_path)
        # Sanity-check the chase: keypose_map.json must be a sibling
        # of composed_map.json, both at shot_root.
        shot_root = env["composed_dir"].parent
        assert (shot_root / "composed_map.json").is_file()
        assert (shot_root / "keypose_map.json").is_file()
        # Driver should find both
        reconstruct_timing_for_queue(
            node8_result_path=env["node8_result_path"]
        )
        # Output landed
        assert (env["work_dir"] / "node9_result.json").is_file()

    def test_missing_composed_png_raises(self, tmp_path):
        """Locked decision #7 -- fail-loud, no substitute."""
        env = _make_workdir(tmp_path, keypose_count=2,
                            held_per_keypose=[1, 1],
                            skip_compose_keypose=1)
        with pytest.raises(TimingReconstructionError, match="does not exist"):
            reconstruct_timing_for_queue(
                node8_result_path=env["node8_result_path"]
            )

    def test_total_frames_mismatch_raises(self, tmp_path):
        """Locked decision #8 -- hard error on totalFrames disagreement."""
        env = _make_workdir(tmp_path, keypose_count=1,
                            held_per_keypose=[2],
                            bad_total_frames_offset=5)  # claim 5 too many
        with pytest.raises(FrameCountMismatchError, match="totalFrames"):
            reconstruct_timing_for_queue(
                node8_result_path=env["node8_result_path"]
            )

    def test_duplicate_frame_index_in_keyposes_raises(self, tmp_path):
        """Locked decision #10 -- same frame in 2 keyPoses = hard error."""
        env = _make_workdir(
            tmp_path, keypose_count=2, held_per_keypose=[2, 2],
            duplicate_frame=(1, 2),
        )
        with pytest.raises(KeyPoseMapInputError, match="multiple keyPoses"):
            reconstruct_timing_for_queue(
                node8_result_path=env["node8_result_path"]
            )

    def test_rerun_wipes_timed(self, tmp_path):
        """Locked decision #13."""
        env = _make_workdir(tmp_path)
        timed_dir = env["shot_root"] / "timed"
        timed_dir.mkdir(parents=True, exist_ok=True)
        stale = timed_dir / "frame_9999.png"
        Image.new("RGB", (16, 16), (1, 2, 3)).save(stale)
        assert stale.is_file()
        reconstruct_timing_for_queue(
            node8_result_path=env["node8_result_path"]
        )
        assert not stale.is_file()

    def test_aggregate_node9_result(self, tmp_path):
        env = _make_workdir(tmp_path)
        reconstruct_timing_for_queue(
            node8_result_path=env["node8_result_path"]
        )
        agg = json.loads((env["work_dir"] / "node9_result.json").read_text())
        assert agg["schemaVersion"] == 1
        assert agg["projectName"] == "test9"
        assert len(agg["shots"]) == 1

    def test_per_shot_timed_map_shape(self, tmp_path):
        env = _make_workdir(tmp_path, keypose_count=1, held_per_keypose=[2])
        reconstruct_timing_for_queue(
            node8_result_path=env["node8_result_path"]
        )
        tm = json.loads((env["shot_root"] / "timed_map.json").read_text())
        assert tm["schemaVersion"] == 1
        assert tm["shotId"] == "shot_001"
        assert tm["totalFrames"] == 3
        assert len(tm["frames"]) == 3
        # Anchor first
        assert tm["frames"][0]["frameIndex"] == 1
        assert tm["frames"][0]["isAnchor"] is True
        assert tm["frames"][0]["offset"] == [0, 0]
        # Held next
        assert tm["frames"][1]["isAnchor"] is False

    def test_composite_caching_does_not_double_open(self, tmp_path):
        """Held frames sharing a keyPoseIndex with the anchor reuse
        the cached composite -- proxy check: the run completes without
        error and produces all expected outputs even with many
        held-frames per anchor."""
        env = _make_workdir(tmp_path, keypose_count=1, held_per_keypose=[20])
        result = reconstruct_timing_for_queue(
            node8_result_path=env["node8_result_path"]
        )
        assert result.shots[0].totalFrames == 21


# -------------------------------------------------------------------
# CLI subprocess
# -------------------------------------------------------------------

class TestCli:

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "run_node9.py"), *args],
            capture_output=True,
            text=True,
        )

    def test_cli_exit_0_on_success(self, tmp_path):
        env = _make_workdir(tmp_path)
        r = self._run("--node8-result", str(env["node8_result_path"]))
        assert r.returncode == 0, (
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert "[node9] OK" in r.stdout

    def test_cli_quiet_suppresses_summary(self, tmp_path):
        env = _make_workdir(tmp_path)
        r = self._run("--node8-result", str(env["node8_result_path"]),
                      "--quiet")
        assert r.returncode == 0
        assert "[node9] OK" not in r.stdout

    def test_cli_exit_1_on_typed_error(self, tmp_path):
        bad = tmp_path / "missing.json"
        r = self._run("--node8-result", str(bad))
        assert r.returncode == 1
        assert "[node9] FAILED" in r.stderr

    def test_cli_summary_reports_anchor_held_counts(self, tmp_path):
        env = _make_workdir(tmp_path, keypose_count=2,
                            held_per_keypose=[3, 2])
        r = self._run("--node8-result", str(env["node8_result_path"]))
        assert r.returncode == 0
        assert "2 anchor" in r.stdout
        assert "5 held" in r.stdout
