"""Pytest suite for Node 8 - Scene Assembly.

Mirrors the test layout of test_node5.py / test_node6.py / test_node7.py:

  * Tiny helpers synthesize a Node 7 work dir (node7_result.json +
    refined_map.json + keyposes/ + refined/) directly in `tmp_path`.
  * Each test hits one locked decision, one error path, or one CLI
    behavior.

We do NOT spin up ComfyUI; Node 8 is pure-Python compositing and has
no GPU dependency.

Locked decisions (CLAUDE.md "Node 8 - locked decisions"):
  1.  Bbox is single source of truth.                      [implicit in many tests]
  2.  Feet-pinned scaling, NOT stretch-to-fit.             [test_feet_pinned_*]
  3.  Output canvas = source MP4 res exactly.              [test_canvas_dims_match_keypose]
  4.  Background = solid white.                             [test_background_default_is_white]
  5.  Z-order = bbox.bottomY ascending.                     [test_z_order_paint_lower_on_top]
  6.  Threshold to BnW (no normalize).                      [test_output_is_bnw_only]
  7.  Substitute-rough on Node 7 errors, warn-and-recon.   [test_substitute_rough_*]
  8.  Pure-Python.                                          [implicit -- imports]
  9.  Single-threaded.                                      [n/a]
  10. Rerun wipes <shotId>/composed/.                       [test_rerun_wipes_composed]
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
    CompositingError,
    Node7ResultInputError,
    Node8Error,
    PipelineError,
    RefinedPngError,
)
from pipeline.node8 import (  # noqa: E402
    ComposedKeyPose,
    ComposedMap,
    Node8Result,
    SUPPORTED_BACKGROUNDS,
    _detect_character_extent,
    _feet_pinned_paste,
    _group_by_keypose,
    _is_refined_ok,
    _substitute_rough,
    _threshold_to_bnw,
    compose_for_queue,
    load_node7_result,
    load_refined_map,
)


# -------------------------------------------------------------------
# Test fixtures
# -------------------------------------------------------------------

CANVAS_W, CANVAS_H = 256, 192  # smaller than 512 to keep tests fast
REFINED_SIZE = 64  # shrunk too


def _draw_humanoid(path: Path, color: tuple[int, int, int],
                   size: int = REFINED_SIZE) -> None:
    """A tiny 64x64 humanoid silhouette: head + torso + legs.
    Centered in the canvas with the lowest pixel near the bottom edge.
    """
    img = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    cx = size // 2
    # Head
    draw.ellipse(
        (cx - 8, 4, cx + 8, 20),
        fill=color,
    )
    # Body
    draw.rectangle(
        (cx - 6, 20, cx + 6, 44),
        fill=color,
    )
    # Legs (lowest non-white at y=size-2)
    draw.rectangle((cx - 5, 44, cx - 1, size - 2), fill=color)
    draw.rectangle((cx + 1, 44, cx + 5, size - 2), fill=color)
    img.save(path, "PNG")


def _draw_blank_white(path: Path, size: int = REFINED_SIZE) -> None:
    Image.new("RGB", (size, size), (255, 255, 255)).save(path, "PNG")


def _draw_keypose(path: Path, w: int = CANVAS_W, h: int = CANVAS_H) -> None:
    """A rough key-pose frame. Draws a dim grey character in each
    half so substitute-rough produces visible pixels."""
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Left character (rough sketch)
    draw.rectangle((30, 40, 70, h - 30), fill=(60, 60, 60))
    # Right character
    draw.rectangle((w - 70, 40, w - 30, h - 30), fill=(60, 60, 60))
    img.save(path, "PNG")


def _make_minimal_workdir(
    tmp_path: Path,
    *,
    background_only_refined: bool = False,
    error_status_for: list[str] | None = None,
    bboxes: dict[str, list[int]] | None = None,
    project: str = "test",
    extra_keypose: bool = False,
) -> dict[str, Any]:
    """Synthesize a Node 7 work dir under `tmp_path`. Returns:
        {
          'work_dir', 'shot_root', 'keyposes_dir', 'refined_dir',
          'refined_map_path', 'node7_result_path',
        }
    """
    work_dir = tmp_path / "work"
    shot_id = "shot_001"
    shot_root = work_dir / shot_id
    keyposes_dir = shot_root / "keyposes"
    refined_dir = shot_root / "refined"
    for d in (keyposes_dir, refined_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Default per-character bboxes split the canvas left/right.
    if bboxes is None:
        bboxes = {
            "Bhim": [20, 30, 60, CANVAS_H - 50],
            "Jaggu": [CANVAS_W - 80, 35, 60, CANVAS_H - 60],
        }
    error_status_for = error_status_for or []

    # Key pose 1 (sourceFrame=1)
    _draw_keypose(keyposes_dir / "frame_0001.png")

    # Refined PNGs
    bhim_refined = refined_dir / "000_Bhim.png"
    jaggu_refined = refined_dir / "000_Jaggu.png"
    if background_only_refined:
        _draw_blank_white(bhim_refined)
        _draw_humanoid(jaggu_refined, (0, 160, 80))
    else:
        _draw_humanoid(bhim_refined, (220, 60, 60))
        _draw_humanoid(jaggu_refined, (0, 160, 80))

    generations: list[dict[str, Any]] = [
        {
            "identity": "Bhim",
            "keyPoseIndex": 0,
            "sourceFrame": 1,
            "selectedAngle": "front",
            "poseExtractor": "dwpose",
            "seed": 1,
            "refinedPath": str(bhim_refined),
            "boundingBox": list(bboxes["Bhim"]),
            "status": "error" if "Bhim" in error_status_for else "ok",
            "errorMessage": "test error" if "Bhim" in error_status_for else "",
            "cnStrengths": {},
        },
        {
            "identity": "Jaggu",
            "keyPoseIndex": 0,
            "sourceFrame": 1,
            "selectedAngle": "front",
            "poseExtractor": "lineart-fallback",
            "seed": 2,
            "refinedPath": str(jaggu_refined),
            "boundingBox": list(bboxes["Jaggu"]),
            "status": "error" if "Jaggu" in error_status_for else "ok",
            "errorMessage": "",
            "cnStrengths": {},
        },
    ]

    if extra_keypose:
        # second key pose at sourceFrame=10
        _draw_keypose(keyposes_dir / "frame_0010.png")
        bhim_kp1 = refined_dir / "001_Bhim.png"
        jaggu_kp1 = refined_dir / "001_Jaggu.png"
        _draw_humanoid(bhim_kp1, (220, 60, 60))
        _draw_humanoid(jaggu_kp1, (0, 160, 80))
        generations.append({
            "identity": "Bhim",
            "keyPoseIndex": 1,
            "sourceFrame": 10,
            "selectedAngle": "front",
            "poseExtractor": "dwpose",
            "seed": 3,
            "refinedPath": str(bhim_kp1),
            "boundingBox": list(bboxes["Bhim"]),
            "status": "ok",
            "errorMessage": "",
            "cnStrengths": {},
        })
        generations.append({
            "identity": "Jaggu",
            "keyPoseIndex": 1,
            "sourceFrame": 10,
            "selectedAngle": "front",
            "poseExtractor": "lineart-fallback",
            "seed": 4,
            "refinedPath": str(jaggu_kp1),
            "boundingBox": list(bboxes["Jaggu"]),
            "status": "ok",
            "errorMessage": "",
            "cnStrengths": {},
        })

    refined_map = {
        "schemaVersion": 1,
        "shotId": shot_id,
        "refinedDir": str(refined_dir),
        "generations": generations,
    }
    refined_map_path = shot_root / "refined_map.json"
    refined_map_path.write_text(json.dumps(refined_map, indent=2))

    n7 = {
        "schemaVersion": 1,
        "projectName": project,
        "workDir": str(work_dir),
        "comfyUIUrl": "",
        "dryRun": False,
        "refinedAt": "2026-04-25T00:00:00+00:00",
        "shots": [{
            "shotId": shot_id,
            "keyPoseCount": 2 if extra_keypose else 1,
            "generatedCount": len(generations),
            "skippedCount": 0,
            "errorCount": len([g for g in generations if g["status"] == "error"]),
            "refinedMapPath": str(refined_map_path),
        }],
    }
    n7_path = work_dir / "node7_result.json"
    n7_path.write_text(json.dumps(n7, indent=2))

    return {
        "work_dir": work_dir,
        "shot_root": shot_root,
        "keyposes_dir": keyposes_dir,
        "refined_dir": refined_dir,
        "refined_map_path": refined_map_path,
        "node7_result_path": n7_path,
    }


# -------------------------------------------------------------------
# Error-hierarchy invariants
# -------------------------------------------------------------------

class TestErrorHierarchy:

    def test_node8_error_is_pipeline_error(self):
        assert issubclass(Node8Error, PipelineError)

    def test_subclasses_are_node8_errors(self):
        assert issubclass(Node7ResultInputError, Node8Error)
        assert issubclass(RefinedPngError, Node8Error)
        assert issubclass(CompositingError, Node8Error)

    def test_node7_input_error_distinct_from_node7s_node6_error(self):
        """Node 7 also has a `Node6ResultInputError`; Node 8's input
        error is a different class so the operator can tell which
        node's input is bad. Both happen to be named `Node*Error` --
        they're cleanly distinguished by their pipeline-prefixed
        parent (Node7Error vs Node8Error)."""
        from pipeline.errors import Node6ResultInputError as N7N6Err
        # Node 7's "I can't read Node 6's output"
        from pipeline.errors import Node7ResultInputError as N8N7Err
        # Node 8's "I can't read Node 7's output"
        assert N7N6Err is not N8N7Err
        assert not issubclass(N8N7Err, N7N6Err)


# -------------------------------------------------------------------
# 8A - Input validation
# -------------------------------------------------------------------

class TestLoadNode7Result:

    def test_loads_valid_manifest(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        loaded = load_node7_result(env["node7_result_path"])
        assert loaded["schemaVersion"] == 1
        assert len(loaded["shots"]) == 1

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(Node7ResultInputError, match="not found"):
            load_node7_result(tmp_path / "absent.json")

    def test_invalid_json_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not json")
        with pytest.raises(Node7ResultInputError, match="not valid JSON"):
            load_node7_result(bad)

    def test_wrong_schema_version_raises(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        path = env["node7_result_path"]
        data = json.loads(path.read_text())
        data["schemaVersion"] = 99
        path.write_text(json.dumps(data))
        with pytest.raises(Node7ResultInputError, match="schemaVersion"):
            load_node7_result(path)

    def test_missing_required_keys_raise(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"schemaVersion": 1, "shots": []}))
        with pytest.raises(Node7ResultInputError, match="workDir"):
            load_node7_result(bad)

    def test_shots_must_be_list(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({
            "schemaVersion": 1, "workDir": "/tmp", "shots": "not a list",
        }))
        with pytest.raises(Node7ResultInputError, match="must be a list"):
            load_node7_result(bad)

    def test_shot_missing_refined_map_path_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({
            "schemaVersion": 1, "workDir": "/tmp",
            "shots": [{"shotId": "shot_001"}],
        }))
        with pytest.raises(Node7ResultInputError, match="refinedMapPath"):
            load_node7_result(bad)


class TestLoadRefinedMap:

    def test_loads_valid(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        loaded = load_refined_map(env["refined_map_path"], "shot_001")
        assert loaded["shotId"] == "shot_001"
        assert len(loaded["generations"]) == 2

    def test_missing_raises(self, tmp_path):
        with pytest.raises(Node7ResultInputError, match="not found"):
            load_refined_map(tmp_path / "absent.json", "shot_001")

    def test_wrong_shot_id_raises(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        with pytest.raises(Node7ResultInputError, match="Stale work dir"):
            load_refined_map(env["refined_map_path"], "shot_002")

    def test_bbox_must_be_4_ints(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        path = env["refined_map_path"]
        data = json.loads(path.read_text())
        data["generations"][0]["boundingBox"] = [1, 2, 3]  # only 3 ints
        path.write_text(json.dumps(data))
        with pytest.raises(Node7ResultInputError, match="boundingBox"):
            load_refined_map(path, "shot_001")

    def test_bbox_with_floats_rejected(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        path = env["refined_map_path"]
        data = json.loads(path.read_text())
        data["generations"][0]["boundingBox"] = [1, 2, 3, 4.5]
        path.write_text(json.dumps(data))
        with pytest.raises(Node7ResultInputError, match="boundingBox"):
            load_refined_map(path, "shot_001")


# -------------------------------------------------------------------
# 8B/8C - Compositing primitives
# -------------------------------------------------------------------

class TestDetectCharacterExtent:

    def test_finds_top_bottom_centroid_of_humanoid(self, tmp_path):
        path = tmp_path / "h.png"
        _draw_humanoid(path, (0, 0, 0))
        arr = np.asarray(Image.open(path).convert("RGB"))
        top_y, bottom_y, center_x = _detect_character_extent(arr)
        # Head starts at y=4, legs end at y=size-2=62
        assert top_y == 4
        assert bottom_y == REFINED_SIZE - 2
        # Centroid should be near canvas centerX
        assert abs(center_x - REFINED_SIZE // 2) <= 2

    def test_empty_image_raises_value_error(self, tmp_path):
        path = tmp_path / "blank.png"
        _draw_blank_white(path)
        arr = np.asarray(Image.open(path).convert("RGB"))
        with pytest.raises(ValueError, match="non-white"):
            _detect_character_extent(arr)


class TestFeetPinnedPaste:
    """Locked decision #2 -- feet land at bbox.bottomY, NOT in the
    middle of the bbox. This is the most consequential algorithm
    test."""

    def test_feet_land_at_bbox_bottom(self, tmp_path):
        refined_path = tmp_path / "h.png"
        _draw_humanoid(refined_path, (0, 0, 0))  # pure black ink
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
        # bbox at left edge, height 100, bottom at y=160
        bbox = [10, 60, 40, 100]
        ok = _feet_pinned_paste(canvas, refined_path, bbox)
        assert ok is True

        # Find the lowest non-white pixel in the canvas. It must be at
        # bbox.bottomY = bbox_y + bbox_h - 1 (last inclusive row),
        # within ±2 px of LANCZOS edge anti-aliasing.
        arr = np.asarray(canvas.convert("L"))
        nonwhite = arr < 250
        nonwhite_rows = np.flatnonzero(nonwhite.any(axis=1))
        lowest_y = int(nonwhite_rows[-1])
        expected_bottom = bbox[1] + bbox[3] - 1
        assert abs(lowest_y - expected_bottom) <= 2, (
            f"feet should land near bbox.bottomY={expected_bottom}; "
            f"got {lowest_y} (LANCZOS tolerance ±2)"
        )
        # Crucially, the feet should NOT land in the middle of the
        # bbox (which would be the bug feet-pinning is meant to fix).
        bbox_middle_y = bbox[1] + bbox[3] // 2
        assert lowest_y > bbox_middle_y, (
            f"feet at row {lowest_y} should be below bbox middle "
            f"({bbox_middle_y}); upper-half feet means stretch-to-fit "
            "regression."
        )

    def test_centerx_lands_on_bbox_centerx(self, tmp_path):
        refined_path = tmp_path / "h.png"
        _draw_humanoid(refined_path, (0, 0, 0))
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
        # bbox right side
        bbox = [180, 50, 60, 100]
        ok = _feet_pinned_paste(canvas, refined_path, bbox)
        assert ok is True
        arr = np.asarray(canvas.convert("L"))
        nonwhite = arr < 250
        nonwhite_cols = np.flatnonzero(nonwhite.any(axis=0))
        leftmost = int(nonwhite_cols[0])
        rightmost = int(nonwhite_cols[-1])
        actual_centerX = (leftmost + rightmost) // 2
        expected_centerX = bbox[0] + bbox[2] // 2
        # Within 2 pixels (resize-rounding margin)
        assert abs(actual_centerX - expected_centerX) <= 2, (
            f"centerX should land near bbox.centerX={expected_centerX}; "
            f"got {actual_centerX}"
        )

    def test_scale_matches_bbox_height(self, tmp_path):
        """Resized character height in canvas should equal bbox.height."""
        refined_path = tmp_path / "h.png"
        _draw_humanoid(refined_path, (0, 0, 0))
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
        bbox = [80, 40, 50, 80]
        ok = _feet_pinned_paste(canvas, refined_path, bbox)
        assert ok is True
        arr = np.asarray(canvas.convert("L"))
        nonwhite = arr < 250
        rows = np.flatnonzero(nonwhite.any(axis=1))
        char_height = int(rows[-1] - rows[0] + 1)
        # Scale calculated from `char_height_in_refined` which is
        # 64-2 - 4 + 1 = 59 for our humanoid; bbox.height=80. Final
        # rendered height should be near bbox.height; LANCZOS edge
        # anti-aliasing can extend the apparent silhouette by up to
        # 2 pixels per side.
        assert abs(char_height - bbox[3]) <= 3, (
            f"rendered height {char_height} should be near bbox.h={bbox[3]} "
            "(LANCZOS tolerance ±3)"
        )

    def test_empty_refined_returns_false(self, tmp_path):
        path = tmp_path / "blank.png"
        _draw_blank_white(path)
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
        ok = _feet_pinned_paste(canvas, path, [10, 10, 50, 80])
        assert ok is False
        # Canvas should remain pristine white
        arr = np.asarray(canvas.convert("L"))
        assert (arr == 255).all()

    def test_zero_area_bbox_returns_false(self, tmp_path):
        path = tmp_path / "h.png"
        _draw_humanoid(path, (0, 0, 0))
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
        ok = _feet_pinned_paste(canvas, path, [10, 10, 0, 80])
        assert ok is False
        ok = _feet_pinned_paste(canvas, path, [10, 10, 50, 0])
        assert ok is False

    def test_unreadable_refined_returns_false(self, tmp_path):
        """Bad PNG bytes -> caller falls back to substitute-rough."""
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not an image")
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
        ok = _feet_pinned_paste(canvas, bad, [10, 10, 50, 80])
        assert ok is False


class TestSubstituteRough:

    def test_pastes_rough_bbox_region(self, tmp_path):
        kp = tmp_path / "kp.png"
        _draw_keypose(kp)
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
        bbox = [30, 40, 40, 100]
        ok = _substitute_rough(canvas, kp, bbox)
        assert ok is True
        # Rough draws a (60,60,60) rect at (30,40)-(70,h-30); inside
        # bbox we should see that grey copied onto canvas.
        arr = np.asarray(canvas)
        assert (arr[50, 50] == [60, 60, 60]).all()

    def test_missing_keypose_returns_false(self, tmp_path):
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
        ok = _substitute_rough(canvas, tmp_path / "absent.png", [0, 0, 10, 10])
        assert ok is False

    def test_bbox_clipped_to_frame(self, tmp_path):
        kp = tmp_path / "kp.png"
        _draw_keypose(kp)
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
        # bbox extends past right edge -- should clip and still succeed.
        bbox = [CANVAS_W - 30, 50, 100, 50]
        ok = _substitute_rough(canvas, kp, bbox)
        assert ok is True

    def test_bbox_entirely_outside_returns_false(self, tmp_path):
        kp = tmp_path / "kp.png"
        _draw_keypose(kp)
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
        ok = _substitute_rough(canvas, kp, [CANVAS_W + 10, 0, 50, 50])
        assert ok is False


class TestThresholdToBnw:

    def test_below_threshold_is_black_above_is_white(self):
        canvas = Image.new("RGB", (4, 4), (200, 200, 200))
        # Set one pixel to (50, 50, 50) -> below threshold
        canvas.putpixel((1, 1), (50, 50, 50))
        bnw = _threshold_to_bnw(canvas)
        arr = np.asarray(bnw)
        assert (arr[1, 1] == [0, 0, 0]).all()
        assert (arr[0, 0] == [255, 255, 255]).all()

    def test_output_is_rgb_no_alpha(self, tmp_path):
        canvas = Image.new("RGB", (8, 8), (200, 200, 200))
        bnw = _threshold_to_bnw(canvas)
        assert bnw.mode == "RGB"


# -------------------------------------------------------------------
# Top-level driver: compose_for_queue
# -------------------------------------------------------------------

class TestComposeForQueue:

    def test_writes_composed_png_per_keypose(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        result = compose_for_queue(node7_result_path=env["node7_result_path"])
        assert isinstance(result, Node8Result)
        assert len(result.shots) == 1
        s = result.shots[0]
        assert s.composedCount == 1
        composed = env["shot_root"] / "composed" / "000_composite.png"
        assert composed.is_file()

    def test_canvas_dims_match_keypose(self, tmp_path):
        """Locked decision #3."""
        env = _make_minimal_workdir(tmp_path)
        compose_for_queue(node7_result_path=env["node7_result_path"])
        composed = env["shot_root"] / "composed" / "000_composite.png"
        with Image.open(composed) as img:
            assert img.size == (CANVAS_W, CANVAS_H)

    def test_background_default_is_white(self, tmp_path):
        """Locked decision #4. Pixels far from any character bbox
        should be white."""
        env = _make_minimal_workdir(
            tmp_path,
            bboxes={
                "Bhim": [10, 10, 30, 50],
                "Jaggu": [60, 10, 30, 50],
            },
        )
        compose_for_queue(node7_result_path=env["node7_result_path"])
        composed = env["shot_root"] / "composed" / "000_composite.png"
        arr = np.asarray(Image.open(composed))
        # Bottom-right corner is far from any character -- must be
        # white in the composited frame.
        assert (arr[CANVAS_H - 1, CANVAS_W - 1] == [255, 255, 255]).all()

    def test_output_is_bnw_only(self, tmp_path):
        """Locked decision #6. Every pixel should be either pure
        black (0,0,0) or pure white (255,255,255)."""
        env = _make_minimal_workdir(tmp_path)
        compose_for_queue(node7_result_path=env["node7_result_path"])
        composed = env["shot_root"] / "composed" / "000_composite.png"
        arr = np.asarray(Image.open(composed).convert("RGB"))
        unique_values = np.unique(arr)
        assert set(unique_values.tolist()).issubset({0, 255}), (
            f"BnW output must contain only 0 and 255; "
            f"found {sorted(unique_values.tolist())}"
        )

    def test_z_order_paint_lower_on_top(self, tmp_path):
        """Locked decision #5. The character with the lower-on-screen
        bbox should be painted last (= on top of the other)."""
        # Two characters fully overlapping; the one with the larger
        # bbox.bottomY paints last and should dominate the overlap.
        env = _make_minimal_workdir(
            tmp_path,
            bboxes={
                # Bhim: bbox.bottomY = 60+50 = 110
                "Bhim": [60, 60, 60, 50],
                # Jaggu: bbox.bottomY = 60+100 = 160 (lower on screen)
                "Jaggu": [60, 60, 60, 100],
            },
        )
        compose_for_queue(node7_result_path=env["node7_result_path"])
        # Inspect composed_map.json -- characters should be sorted by
        # bottomY ascending, so Bhim (110) comes before Jaggu (160).
        cm_path = env["shot_root"] / "composed_map.json"
        cm = json.loads(cm_path.read_text())
        kp = cm["keyPoses"][0]
        identities_in_paint_order = [c["identity"] for c in kp["characters"]]
        assert identities_in_paint_order == ["Bhim", "Jaggu"], (
            "Bhim's bbox bottomY=110 < Jaggu's=160; Bhim should be "
            "first in the paste order so Jaggu lands on top."
        )

    def test_substitute_rough_on_node7_error(self, tmp_path):
        """Locked decision #7. Bhim status='error' -> rough pixels
        substituted, warning recorded, CLI still exits 0 -- here we
        check the in-memory result."""
        env = _make_minimal_workdir(
            tmp_path,
            error_status_for=["Bhim"],
        )
        result = compose_for_queue(node7_result_path=env["node7_result_path"])
        assert result.shots[0].substituteCount == 1
        cm = json.loads((env["shot_root"] / "composed_map.json").read_text())
        kp = cm["keyPoses"][0]
        bhim_record = next(c for c in kp["characters"] if c["identity"] == "Bhim")
        assert bhim_record["substitutedFromRough"] is True
        warning_codes = [w["code"] for w in kp["warnings"]]
        assert "node7-error" in warning_codes

    def test_substitute_rough_on_empty_refined(self, tmp_path):
        """Locked decision #7. Bhim's refined PNG is all-white ->
        treated as empty -> substitute-rough -> warning recorded."""
        env = _make_minimal_workdir(
            tmp_path,
            background_only_refined=True,  # Bhim refined is blank
        )
        result = compose_for_queue(node7_result_path=env["node7_result_path"])
        assert result.shots[0].substituteCount == 1
        cm = json.loads((env["shot_root"] / "composed_map.json").read_text())
        kp = cm["keyPoses"][0]
        bhim_record = next(c for c in kp["characters"] if c["identity"] == "Bhim")
        assert bhim_record["substitutedFromRough"] is True
        warning_codes = [w["code"] for w in kp["warnings"]]
        assert "refined-empty-or-unreadable" in warning_codes

    def test_rerun_wipes_composed(self, tmp_path):
        """Locked decision #10. Stale `*_composite.png` from an earlier
        run with different inputs should be removed before the new run
        writes its own outputs."""
        env = _make_minimal_workdir(tmp_path)
        composed_dir = env["shot_root"] / "composed"
        composed_dir.mkdir(parents=True, exist_ok=True)
        stale = composed_dir / "999_composite.png"
        Image.new("RGB", (16, 16), (123, 45, 67)).save(stale)
        assert stale.is_file()
        compose_for_queue(node7_result_path=env["node7_result_path"])
        assert not stale.is_file()

    def test_multi_keypose(self, tmp_path):
        env = _make_minimal_workdir(tmp_path, extra_keypose=True)
        result = compose_for_queue(node7_result_path=env["node7_result_path"])
        assert result.shots[0].keyPoseCount == 2
        for idx in (0, 1):
            assert (env["shot_root"] / "composed" /
                    f"{idx:03d}_composite.png").is_file()

    def test_aggregate_result_written(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        compose_for_queue(node7_result_path=env["node7_result_path"])
        agg_path = env["work_dir"] / "node8_result.json"
        assert agg_path.is_file()
        agg = json.loads(agg_path.read_text())
        assert agg["schemaVersion"] == 1
        assert agg["projectName"] == "test"
        assert agg["background"] == "white"
        assert len(agg["shots"]) == 1

    def test_per_shot_manifest_shape(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        compose_for_queue(node7_result_path=env["node7_result_path"])
        cm = json.loads((env["shot_root"] / "composed_map.json").read_text())
        assert cm["schemaVersion"] == 1
        assert cm["shotId"] == "shot_001"
        assert len(cm["keyPoses"]) == 1
        kp = cm["keyPoses"][0]
        assert kp["keyPoseIndex"] == 0
        assert kp["sourceFrame"] == 1
        assert Path(kp["composedPath"]).name == "000_composite.png"
        assert len(kp["characters"]) == 2

    def test_missing_keypose_raises_refined_png_error(self, tmp_path):
        """If both the refined PNG works AND the rough is missing,
        we still need the rough for canvas dim probing -- so the run
        fails with RefinedPngError."""
        env = _make_minimal_workdir(tmp_path)
        # Delete the keypose PNG that's the canvas dim source
        (env["keyposes_dir"] / "frame_0001.png").unlink()
        with pytest.raises(RefinedPngError, match="rough key-pose PNG"):
            compose_for_queue(node7_result_path=env["node7_result_path"])

    def test_bbox_outside_frame_with_node7_error_unfillable(self, tmp_path):
        """status=error -> need rough; if the bbox is entirely outside
        the rough frame, substitute-rough also fails -> RefinedPngError."""
        env = _make_minimal_workdir(
            tmp_path,
            error_status_for=["Bhim"],
            bboxes={
                "Bhim": [CANVAS_W + 100, 0, 50, 50],  # off-canvas
                "Jaggu": [50, 50, 30, 80],
            },
        )
        with pytest.raises(RefinedPngError, match="unfillable"):
            compose_for_queue(node7_result_path=env["node7_result_path"])

    def test_unsupported_background_raises_compositing_error(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        with pytest.raises(CompositingError, match="not supported"):
            compose_for_queue(
                node7_result_path=env["node7_result_path"],
                background="rainbow",
            )


# -------------------------------------------------------------------
# Helpers — _group_by_keypose, _is_refined_ok
# -------------------------------------------------------------------

class TestHelpers:

    def test_group_by_keypose_handles_multi(self):
        gens = [
            {"keyPoseIndex": 0, "identity": "A"},
            {"keyPoseIndex": 1, "identity": "B"},
            {"keyPoseIndex": 0, "identity": "C"},
        ]
        out = _group_by_keypose(gens)
        assert sorted(out.keys()) == [0, 1]
        assert len(out[0]) == 2
        assert len(out[1]) == 1

    def test_is_refined_ok_status_must_be_ok(self, tmp_path):
        path = tmp_path / "p.png"
        path.write_bytes(b"x")  # exists
        assert _is_refined_ok(path, "ok") is True
        assert _is_refined_ok(path, "error") is False
        assert _is_refined_ok(path, "skipped") is False

    def test_is_refined_ok_file_must_exist(self, tmp_path):
        absent = tmp_path / "absent.png"
        assert _is_refined_ok(absent, "ok") is False


# -------------------------------------------------------------------
# CLI smoke tests (subprocess into run_node8.py)
# -------------------------------------------------------------------

class TestCli:

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "run_node8.py"), *args],
            capture_output=True,
            text=True,
        )

    def test_cli_exit_0_on_success(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        r = self._run("--node7-result", str(env["node7_result_path"]))
        assert r.returncode == 0, (
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert "[node8] OK" in r.stdout

    def test_cli_quiet_suppresses_summary(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        r = self._run("--node7-result", str(env["node7_result_path"]),
                      "--quiet")
        assert r.returncode == 0
        assert "[node8] OK" not in r.stdout

    def test_cli_exit_1_on_typed_error(self, tmp_path):
        bad = tmp_path / "missing.json"
        r = self._run("--node7-result", str(bad))
        assert r.returncode == 1
        assert "[node8] FAILED" in r.stderr

    def test_cli_substitute_warning_summary(self, tmp_path):
        env = _make_minimal_workdir(tmp_path, error_status_for=["Bhim"])
        r = self._run("--node7-result", str(env["node7_result_path"]))
        assert r.returncode == 0
        assert "substitute-rough warning" in r.stdout

    def test_cli_rejects_unknown_background(self, tmp_path):
        env = _make_minimal_workdir(tmp_path)
        r = self._run("--node7-result", str(env["node7_result_path"]),
                      "--background", "rainbow")
        # argparse choices=... rejects -> exit 2
        assert r.returncode == 2
