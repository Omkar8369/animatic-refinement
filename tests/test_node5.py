"""Tests for Node 5 — Character Detection & Position.

Every test synthesizes key-pose PNGs via PIL to mimic what Node 4 produces:
a white background with solid dark rectangles as "character silhouettes".
Otsu binarization picks the dark rectangles as foreground, scipy.ndimage.label
recovers them as connected components, and the rest of the Node 5 pipeline
(cleanup, reconcile, position-bin, identity-zip) takes over.

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

from pipeline.cli_node5 import main as cli_main
from pipeline.errors import (
    CharacterDetectionError,
    Node4ResultInputError,
    Node5Error,
    PipelineError,
    QueueLookupError,
)
from pipeline.node5 import (
    DEFAULT_DARK_THRESHOLD,
    DEFAULT_MERGE_IOU,
    DEFAULT_MIN_AREA_RATIO,
    DEFAULT_OUTLINE_CLOSING_KERNEL,
    POSITION_CODES,
    Detection,
    Node5Result,
    ShotDetectionSummary,
    _bin_position,
    _close_outline_gaps,
    _extract_dark_lines,
    _iou,
    _merge_overlapping,
    _save_dark_lines_png,
    _wipe_dark_lines_dir,
    detect_characters_for_queue,
    detect_characters_for_shot,
)


# ---------------------------------------------------------------
# Fixture helpers — synthesize a Node 4 work-dir layout
# ---------------------------------------------------------------

# Key-pose frame resolution. Small enough that tests run fast; large enough
# that 25/20/10/20/25 position bins get at least a few pixels of resolution
# (96 * 0.10 = 9.6 px for the narrow C band).
FRAME_W = 96
FRAME_H = 64

# Background = white (255); ink = dark (20). Otsu will pick a threshold in
# between and treat ink pixels as foreground.
BG = 255
INK = 20


def _save_frame(arr: np.ndarray, path: Path) -> None:
    """Save an `arr` of shape (H, W) uint8 as a grayscale PNG."""
    img = Image.fromarray(arr, mode="L")
    img.save(path)


def _blank_frame() -> np.ndarray:
    """A solid-white frame."""
    return np.full((FRAME_H, FRAME_W), BG, dtype=np.uint8)


def _draw_rect(arr: np.ndarray, x: int, y: int, w: int, h: int) -> None:
    """Draw a solid dark (ink) rectangle onto `arr`. In-place."""
    arr[y:y + h, x:x + w] = INK


def _single_char_frame() -> np.ndarray:
    """One character blob centered at ~x=48 (frame center)."""
    arr = _blank_frame()
    _draw_rect(arr, x=40, y=20, w=16, h=24)  # centre x = 48 -> C
    return arr


def _two_char_frame_LR() -> np.ndarray:
    """Two blobs: left at x=10-30 (C≈20, L), right at x=70-90 (C≈80, R)."""
    arr = _blank_frame()
    _draw_rect(arr, x=10, y=16, w=20, h=30)  # L
    _draw_rect(arr, x=70, y=16, w=20, h=30)  # R
    return arr


def _three_char_frame() -> np.ndarray:
    """Three blobs; the smallest is the middle one.

    Used for the count-mismatch-over reconcile test: metadata will say 2,
    Node 5 should drop the smallest-area blob (the center one) and keep L+R.
    """
    arr = _blank_frame()
    _draw_rect(arr, x=8, y=16, w=20, h=30)   # L, area=600
    _draw_rect(arr, x=42, y=24, w=12, h=16)  # C, area=192 (smallest)
    _draw_rect(arr, x=68, y=16, w=20, h=30)  # R, area=600
    return arr


def _two_char_touching_frame() -> np.ndarray:
    """Two blobs joined by a 2-pixel-tall bridge (one CC).

    Before erosion: one connected component.
    After 1 iteration of 3x3 binary_erosion the bridge disappears while the
    blobs survive, giving two components — the reconcile-eroded warning path.
    """
    arr = _blank_frame()
    _draw_rect(arr, x=10, y=16, w=20, h=30)  # L body
    _draw_rect(arr, x=70, y=16, w=20, h=30)  # R body
    _draw_rect(arr, x=30, y=30, w=40, h=2)   # thin horizontal bridge
    return arr


def _write_keypose_map(
    shot_dir: Path,
    shot_id: str,
    key_pose_frames: list[np.ndarray],
    total_frames: int | None = None,
) -> Path:
    """Write keypose_map.json + copy key-pose PNGs into shot_dir/keyposes/.

    Returns the keypose_map.json path.
    """
    keyposes_dir = shot_dir / "keyposes"
    keyposes_dir.mkdir(parents=True, exist_ok=True)

    key_pose_entries: list[dict] = []
    filenames: list[str] = []
    for i, arr in enumerate(key_pose_frames, start=1):
        name = f"frame_{i:04d}.png"
        _save_frame(arr, keyposes_dir / name)
        filenames.append(name)
        key_pose_entries.append({
            "keyPoseIndex": i - 1,
            "sourceFrame": i,
            "keyPoseFilename": name,
            "heldFrames": [{"frame": i, "offset": [0, 0]}],
        })

    tf = total_frames if total_frames is not None else len(key_pose_frames)
    km = {
        "schemaVersion": 1,
        "shotId": shot_id,
        "totalFrames": tf,
        "sourceFramesDir": str(shot_dir),
        "keyPosesDir": str(keyposes_dir),
        "threshold": 8.0,
        "maxEdge": 128,
        "keyPoses": key_pose_entries,
    }
    km_path = shot_dir / "keypose_map.json"
    km_path.write_text(json.dumps(km, indent=2), encoding="utf-8")
    return km_path


def _make_shot(
    work_dir: Path,
    shot_id: str,
    key_pose_frames: list[np.ndarray],
) -> dict:
    """Build one shot's on-disk layout + return a shot dict for node4_result.json."""
    shot_dir = work_dir / shot_id
    shot_dir.mkdir(parents=True, exist_ok=True)
    km_path = _write_keypose_map(shot_dir, shot_id, key_pose_frames)
    return {
        "shotId": shot_id,
        "totalFrames": len(key_pose_frames),
        "keyPoseCount": len(key_pose_frames),
        "sourceFramesDir": str(shot_dir),
        "keyPosesDir": str(shot_dir / "keyposes"),
        "keyPoseMapPath": str(km_path),
    }


def _write_node4_result(
    work_dir: Path,
    shots: list[dict],
    project_name: str = "ChhotaBhim_Ep042",
) -> Path:
    """Write node4_result.json."""
    payload = {
        "schemaVersion": 1,
        "projectName": project_name,
        "workDir": str(work_dir),
        "threshold": 8.0,
        "maxEdge": 128,
        "extractedAt": "2026-04-23T00:00:00+00:00",
        "shots": shots,
    }
    path = work_dir / "node4_result.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_queue(
    work_dir: Path,
    shots_meta: list[dict],
    project_name: str = "ChhotaBhim_Ep042",
    batch_size: int = 4,
) -> Path:
    """Write queue.json with the given per-shot character metadata.

    shots_meta: list of {"shotId": ..., "characters": [{"identity":..., "position":...}, ...]}
    """
    batches = [shots_meta[i:i + batch_size] for i in range(0, len(shots_meta), batch_size)]
    payload = {
        "schemaVersion": 1,
        "projectName": project_name,
        "batchSize": batch_size,
        "totalShots": len(shots_meta),
        "batchCount": len(batches),
        "batches": [
            [
                {
                    "shotId": s["shotId"],
                    "mp4Path": str(work_dir / f"{s['shotId']}.mp4"),
                    "durationFrames": 25,
                    "durationSeconds": 1.0,
                    "characters": [
                        {
                            "identity": c["identity"],
                            "sheetPath": str(work_dir / f"{c['identity']}.png"),
                            "position": c["position"],
                        }
                        for c in s["characters"]
                    ],
                }
                for s in batch
            ]
            for batch in batches
        ],
    }
    path = work_dir / "queue.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------
# Fixture factories
# ---------------------------------------------------------------

ShotBuilder = Callable[..., dict]


@pytest.fixture
def make_single_char_shot(tmp_path: Path) -> ShotBuilder:
    """Factory: shot with one key pose containing one blob (center)."""
    def _build(shot_id: str) -> dict:
        return _make_shot(tmp_path, shot_id, [_single_char_frame()])
    return _build


@pytest.fixture
def make_two_char_shot(tmp_path: Path) -> ShotBuilder:
    """Factory: shot with one key pose containing two blobs (L + R)."""
    def _build(shot_id: str) -> dict:
        return _make_shot(tmp_path, shot_id, [_two_char_frame_LR()])
    return _build


@pytest.fixture
def make_three_blob_shot(tmp_path: Path) -> ShotBuilder:
    """Factory: shot with one key pose containing three blobs (L + small C + R)."""
    def _build(shot_id: str) -> dict:
        return _make_shot(tmp_path, shot_id, [_three_char_frame()])
    return _build


@pytest.fixture
def make_touching_chars_shot(tmp_path: Path) -> ShotBuilder:
    """Factory: shot where two characters are connected by a thin bridge."""
    def _build(shot_id: str) -> dict:
        return _make_shot(tmp_path, shot_id, [_two_char_touching_frame()])
    return _build


# ---------------------------------------------------------------
# Happy path — single character
# ---------------------------------------------------------------

class TestSingleCharacter:
    def test_one_blob_one_detection(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [{
            "shotId": "shot_001",
            "characters": [{"identity": "Bhim", "position": "C"}],
        }])

        result = detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )

        assert len(result.shots) == 1
        s = result.shots[0]
        assert s.shotId == "shot_001"
        assert s.expectedCharacterCount == 1
        assert s.keyPoseCount == 1
        assert s.totalDetections == 1
        assert s.warningCount == 0

        cm = json.loads(Path(s.characterMapPath).read_text(encoding="utf-8"))
        assert cm["schemaVersion"] == 1
        assert cm["shotId"] == "shot_001"
        assert len(cm["keyPoses"]) == 1
        kp = cm["keyPoses"][0]
        assert kp["sourceFrame"] == 1
        assert kp["keyPoseFilename"] == "frame_0001.png"
        assert kp["frameWidth"] == FRAME_W
        assert kp["frameHeight"] == FRAME_H
        assert len(kp["detections"]) == 1
        det = kp["detections"][0]
        assert det["identity"] == "Bhim"
        assert det["expectedPosition"] == "C"
        # Blob centered at x=48; 48/96 = 0.5 -> C (the [0.45, 0.55) bin).
        assert det["positionCode"] == "C"
        assert abs(det["centerX"] - 0.5) < 0.05
        assert det["area"] > 0

    def test_character_map_at_shot_root(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        """character_map.json sits at the shot root (next to keyposes/)."""
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [{
            "shotId": "shot_001",
            "characters": [{"identity": "Bhim", "position": "C"}],
        }])

        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )

        assert (tmp_path / "shot_001" / "character_map.json").is_file()
        # NOT inside keyposes/
        assert not (tmp_path / "shot_001" / "keyposes" / "character_map.json").exists()


# ---------------------------------------------------------------
# Happy path — two characters, L + R
# ---------------------------------------------------------------

class TestTwoCharactersLR:
    def test_detects_two_blobs(
        self, tmp_path: Path, make_two_char_shot: ShotBuilder
    ):
        shot = make_two_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [{
            "shotId": "shot_001",
            "characters": [
                {"identity": "Bhim", "position": "L"},
                {"identity": "Jaggu", "position": "R"},
            ],
        }])

        result = detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )
        s = result.shots[0]
        assert s.totalDetections == 2
        assert s.warningCount == 0

        cm = json.loads(Path(s.characterMapPath).read_text())
        dets = cm["keyPoses"][0]["detections"]
        assert len(dets) == 2

        # Left-most detection (by centre-x) should get Bhim + L.
        by_x = sorted(dets, key=lambda d: d["centerX"])
        assert by_x[0]["identity"] == "Bhim"
        assert by_x[0]["positionCode"] == "L"
        assert by_x[1]["identity"] == "Jaggu"
        assert by_x[1]["positionCode"] == "R"

    def test_metadata_order_does_not_matter(
        self, tmp_path: Path, make_two_char_shot: ShotBuilder
    ):
        """Strategy A sorts metadata by position rank before zipping, so
        reversed metadata order should produce the same bindings."""
        shot = make_two_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [{
            "shotId": "shot_001",
            "characters": [
                # R first, L second — Node 5 must reorder by position rank.
                {"identity": "Jaggu", "position": "R"},
                {"identity": "Bhim", "position": "L"},
            ],
        }])

        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )
        cm = json.loads(
            (tmp_path / "shot_001" / "character_map.json").read_text()
        )
        dets = sorted(cm["keyPoses"][0]["detections"], key=lambda d: d["centerX"])
        assert dets[0]["identity"] == "Bhim"   # still left
        assert dets[1]["identity"] == "Jaggu"  # still right


# ---------------------------------------------------------------
# Reconcile — too many blobs (count-mismatch-over)
# ---------------------------------------------------------------

class TestCountReconcileOver:
    def test_drops_smallest_blob(
        self, tmp_path: Path, make_three_blob_shot: ShotBuilder
    ):
        """3 blobs detected, metadata says 2 -> drop smallest, warn."""
        shot = make_three_blob_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [{
            "shotId": "shot_001",
            "characters": [
                {"identity": "Bhim", "position": "L"},
                {"identity": "Jaggu", "position": "R"},
            ],
        }])

        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )
        cm = json.loads(
            (tmp_path / "shot_001" / "character_map.json").read_text()
        )
        kp = cm["keyPoses"][0]
        # Reconcile must have dropped the smallest (the center blob).
        assert len(kp["detections"]) == 2
        kinds = [w["kind"] for w in kp["warnings"]]
        assert "count-mismatch-over" in kinds

        # The surviving two should be the L and R blobs (area=600 each).
        by_x = sorted(kp["detections"], key=lambda d: d["centerX"])
        assert by_x[0]["positionCode"] == "L"
        assert by_x[1]["positionCode"] == "R"


# ---------------------------------------------------------------
# Reconcile — too few blobs (erosion)
# ---------------------------------------------------------------

class TestCountReconcileUnder:
    def test_erosion_splits_touching_characters(
        self, tmp_path: Path, make_touching_chars_shot: ShotBuilder
    ):
        """1 CC detected (blobs joined by bridge), metadata says 2.
        One iteration of binary_erosion should break the bridge."""
        shot = make_touching_chars_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [{
            "shotId": "shot_001",
            "characters": [
                {"identity": "Bhim", "position": "L"},
                {"identity": "Jaggu", "position": "R"},
            ],
        }])

        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )
        cm = json.loads(
            (tmp_path / "shot_001" / "character_map.json").read_text()
        )
        kp = cm["keyPoses"][0]
        assert len(kp["detections"]) == 2
        kinds = [w["kind"] for w in kp["warnings"]]
        assert "reconcile-eroded" in kinds


# ---------------------------------------------------------------
# Position binning — the locked 25/20/10/20/25 split
# ---------------------------------------------------------------

class TestPositionBinning:
    """Direct unit tests against `_bin_position`."""

    @pytest.mark.parametrize("value,expected", [
        (0.00, "L"),
        (0.10, "L"),
        (0.249, "L"),
        (0.25, "CL"),
        (0.30, "CL"),
        (0.449, "CL"),
        (0.45, "C"),
        (0.50, "C"),
        (0.549, "C"),
        (0.55, "CR"),
        (0.60, "CR"),
        (0.749, "CR"),
        (0.75, "R"),
        (0.90, "R"),
        (1.00, "R"),
    ])
    def test_bin(self, value: float, expected: str):
        assert _bin_position(value) == expected

    def test_all_codes_emitted(self):
        # Sanity: every L/CL/C/CR/R appears for at least one value.
        seen = {_bin_position(v) for v in (0.1, 0.3, 0.5, 0.65, 0.9)}
        assert seen == set(POSITION_CODES)


# ---------------------------------------------------------------
# IoU + merge — direct unit tests on internals
# ---------------------------------------------------------------

class TestIoU:
    def test_disjoint(self):
        assert _iou((0, 0, 10, 10), (20, 20, 10, 10)) == 0.0

    def test_identical(self):
        assert _iou((5, 5, 10, 10), (5, 5, 10, 10)) == pytest.approx(1.0)

    def test_half_overlap(self):
        # A = (0,0,10,10) area=100; B = (5,0,10,10) area=100.
        # intersection = 5x10 = 50; union = 100+100-50 = 150; IoU = 1/3.
        assert _iou((0, 0, 10, 10), (5, 0, 10, 10)) == pytest.approx(1.0 / 3.0)


class TestMergeOverlapping:
    def test_no_merge_below_threshold(self):
        # Two disjoint boxes; IoU = 0 << 0.5 -> no merge.
        boxes = [(0, 0, 10, 10, 100), (50, 50, 10, 10, 100)]
        assert _merge_overlapping(boxes, 0.5) == boxes

    def test_merge_above_threshold(self):
        # Identical boxes -> IoU 1.0 -> merge.
        boxes = [(5, 5, 10, 10, 100), (5, 5, 10, 10, 100)]
        merged = _merge_overlapping(boxes, 0.5)
        assert len(merged) == 1
        x, y, w, h, area = merged[0]
        assert (x, y, w, h) == (5, 5, 10, 10)
        assert area == 200  # summed (approximation)

    def test_merge_to_fixpoint(self):
        # Three heavily-overlapping boxes should collapse to one.
        boxes = [
            (0, 0, 10, 10, 100),
            (1, 0, 10, 10, 100),   # IoU with #0 = 9/11 > 0.5
            (0, 1, 10, 10, 100),   # IoU with merged = ...
        ]
        merged = _merge_overlapping(boxes, 0.5)
        assert len(merged) == 1


# ---------------------------------------------------------------
# Aggregate — multiple shots
# ---------------------------------------------------------------

class TestAggregate:
    def test_multi_shot_aggregate(
        self,
        tmp_path: Path,
        make_single_char_shot: ShotBuilder,
        make_two_char_shot: ShotBuilder,
    ):
        shot_a = make_single_char_shot("shot_001")
        shot_b = make_two_char_shot("shot_002")
        _write_node4_result(tmp_path, [shot_a, shot_b])
        _write_queue(tmp_path, [
            {
                "shotId": "shot_001",
                "characters": [{"identity": "Bhim", "position": "C"}],
            },
            {
                "shotId": "shot_002",
                "characters": [
                    {"identity": "Bhim", "position": "L"},
                    {"identity": "Jaggu", "position": "R"},
                ],
            },
        ])

        result = detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )
        assert len(result.shots) == 2

        agg = json.loads((tmp_path / "node5_result.json").read_text())
        assert agg["schemaVersion"] == 1
        assert agg["projectName"] == "ChhotaBhim_Ep042"
        assert len(agg["shots"]) == 2
        by_id = {s["shotId"]: s for s in agg["shots"]}
        assert by_id["shot_001"]["totalDetections"] == 1
        assert by_id["shot_002"]["totalDetections"] == 2


# ---------------------------------------------------------------
# node4_result.json input failures
# ---------------------------------------------------------------

class TestNode4ResultInput:
    def test_missing_file(self, tmp_path: Path):
        _write_queue(tmp_path, [])
        with pytest.raises(Node4ResultInputError, match="not found"):
            detect_characters_for_queue(
                tmp_path / "nope.json",
                tmp_path / "queue.json",
            )

    def test_malformed_json(self, tmp_path: Path):
        p = tmp_path / "node4_result.json"
        p.write_text("{ not valid")
        _write_queue(tmp_path, [])
        with pytest.raises(Node4ResultInputError, match="not valid JSON"):
            detect_characters_for_queue(p, tmp_path / "queue.json")

    def test_wrong_schema_version(self, tmp_path: Path):
        p = tmp_path / "node4_result.json"
        p.write_text(json.dumps({
            "schemaVersion": 2,
            "workDir": str(tmp_path),
            "shots": [],
        }))
        _write_queue(tmp_path, [])
        with pytest.raises(Node4ResultInputError, match="unsupported schemaVersion"):
            detect_characters_for_queue(p, tmp_path / "queue.json")

    def test_missing_shots_key(self, tmp_path: Path):
        p = tmp_path / "node4_result.json"
        p.write_text(json.dumps({
            "schemaVersion": 1,
            "workDir": str(tmp_path),
        }))
        _write_queue(tmp_path, [])
        with pytest.raises(Node4ResultInputError, match="missing required key 'shots'"):
            detect_characters_for_queue(p, tmp_path / "queue.json")

    def test_shot_missing_required_field(self, tmp_path: Path):
        p = tmp_path / "node4_result.json"
        p.write_text(json.dumps({
            "schemaVersion": 1,
            "workDir": str(tmp_path),
            "shots": [{"shotId": "shot_001"}],  # missing keyPosesDir etc.
        }))
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": []}
        ])
        with pytest.raises(Node4ResultInputError, match="missing 'keyPosesDir'"):
            detect_characters_for_queue(p, tmp_path / "queue.json")

    def test_keyposes_dir_disappeared(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        shot = make_single_char_shot("shot_ghost")
        # Wipe the keyposes folder that Node 4 would have populated.
        import shutil
        shutil.rmtree(shot["keyPosesDir"])
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_ghost", "characters": [{"identity": "X", "position": "C"}]}
        ])
        with pytest.raises(Node4ResultInputError, match="keyposes folder does not exist"):
            detect_characters_for_queue(
                tmp_path / "node4_result.json",
                tmp_path / "queue.json",
            )

    def test_keypose_map_json_missing(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        shot = make_single_char_shot("shot_001")
        # Delete only the keypose_map.json, leave keyposes/.
        Path(shot["keyPoseMapPath"]).unlink()
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        with pytest.raises(Node4ResultInputError, match="keypose_map.json not found"):
            detect_characters_for_queue(
                tmp_path / "node4_result.json",
                tmp_path / "queue.json",
            )


# ---------------------------------------------------------------
# queue.json lookup failures
# ---------------------------------------------------------------

class TestQueueLookup:
    def test_missing_file(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        with pytest.raises(QueueLookupError, match="not found"):
            detect_characters_for_queue(
                tmp_path / "node4_result.json",
                tmp_path / "nope.json",
            )

    def test_malformed_json(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        q = tmp_path / "queue.json"
        q.write_text("{ nope")
        with pytest.raises(QueueLookupError, match="not valid JSON"):
            detect_characters_for_queue(
                tmp_path / "node4_result.json", q
            )

    def test_wrong_schema_version(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        q = tmp_path / "queue.json"
        q.write_text(json.dumps({"schemaVersion": 2, "batches": []}))
        with pytest.raises(QueueLookupError, match="unsupported schemaVersion"):
            detect_characters_for_queue(
                tmp_path / "node4_result.json", q
            )

    def test_shot_missing_from_queue(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        # queue.json describes shot_999, not shot_001.
        _write_queue(tmp_path, [
            {"shotId": "shot_999", "characters": [{"identity": "X", "position": "C"}]}
        ])
        with pytest.raises(QueueLookupError, match="does not contain every shotId"):
            detect_characters_for_queue(
                tmp_path / "node4_result.json",
                tmp_path / "queue.json",
            )

    def test_unknown_position_code(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        q = tmp_path / "queue.json"
        q.write_text(json.dumps({
            "schemaVersion": 1,
            "batches": [[{
                "shotId": "shot_001",
                "characters": [{"identity": "X", "position": "MIDDLE"}],
            }]],
        }))
        with pytest.raises(QueueLookupError, match="unknown positionCode"):
            detect_characters_for_queue(
                tmp_path / "node4_result.json", q
            )


# ---------------------------------------------------------------
# Character detection failures (bad PNGs)
# ---------------------------------------------------------------

class TestCharacterDetectionFailures:
    def test_unreadable_png(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        shot = make_single_char_shot("shot_001")
        # Corrupt the key-pose PNG (write zero bytes).
        bad_png = Path(shot["keyPosesDir"]) / "frame_0001.png"
        bad_png.write_bytes(b"")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "X", "position": "C"}]}
        ])
        with pytest.raises(CharacterDetectionError, match="could not open"):
            detect_characters_for_queue(
                tmp_path / "node4_result.json",
                tmp_path / "queue.json",
            )


# ---------------------------------------------------------------
# Direct per-shot API (used by ComfyUI wrapper too)
# ---------------------------------------------------------------

class TestPerShotAPI:
    def test_detect_for_shot_direct(
        self, tmp_path: Path, make_two_char_shot: ShotBuilder
    ):
        shot = make_two_char_shot("shot_001")
        summary = detect_characters_for_shot(
            shot_id="shot_001",
            keyposes_dir=shot["keyPosesDir"],
            key_pose_map_path=shot["keyPoseMapPath"],
            source_frames_dir=shot["sourceFramesDir"],
            expected_characters=[
                {"identity": "Bhim", "position": "L"},
                {"identity": "Jaggu", "position": "R"},
            ],
        )
        assert isinstance(summary, ShotDetectionSummary)
        assert summary.shotId == "shot_001"
        assert summary.totalDetections == 2
        assert Path(summary.characterMapPath).is_file()


# ---------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------

class TestCLI:
    def test_cli_success(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder, capsys
    ):
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        rc = cli_main([
            "--node4-result", str(tmp_path / "node4_result.json"),
            "--queue", str(tmp_path / "queue.json"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[node5] OK" in out
        assert (tmp_path / "node5_result.json").is_file()

    def test_cli_quiet(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder, capsys
    ):
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        rc = cli_main([
            "--node4-result", str(tmp_path / "node4_result.json"),
            "--queue", str(tmp_path / "queue.json"),
            "--quiet",
        ])
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_cli_failure_returns_1(self, tmp_path: Path, capsys):
        rc = cli_main([
            "--node4-result", str(tmp_path / "nope.json"),
            "--queue", str(tmp_path / "nope.json"),
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "[node5] FAILED" in err


# ---------------------------------------------------------------
# Exception-hierarchy sanity
# ---------------------------------------------------------------

def test_all_node5_errors_are_pipeline_errors():
    for cls in (Node4ResultInputError, QueueLookupError, CharacterDetectionError):
        assert issubclass(cls, Node5Error)
        assert issubclass(cls, PipelineError)


def test_node5_error_distinct_from_prior_nodes():
    from pipeline.errors import Node2Error, Node3Error, Node4Error
    for other in (Node2Error, Node3Error, Node4Error):
        assert not issubclass(Node5Error, other)
        assert not issubclass(other, Node5Error)


# ---------------------------------------------------------------
# Return-value type + default-knob sanity
# ---------------------------------------------------------------

def test_returns_node5_result_dataclass(
    tmp_path: Path, make_single_char_shot: ShotBuilder
):
    shot = make_single_char_shot("shot_001")
    _write_node4_result(tmp_path, [shot])
    _write_queue(tmp_path, [
        {"shotId": "shot_001", "characters": [{"identity": "X", "position": "C"}]}
    ])
    result = detect_characters_for_queue(
        tmp_path / "node4_result.json",
        tmp_path / "queue.json",
    )
    assert isinstance(result, Node5Result)
    assert result.minAreaRatio == DEFAULT_MIN_AREA_RATIO
    assert result.mergeIou == DEFAULT_MERGE_IOU


def test_detection_dataclass_shape():
    """Sanity: Detection fields match the on-disk schema."""
    d = Detection(
        identity="Bhim",
        expectedPosition="L",
        boundingBox=[10, 20, 30, 40],
        centerX=0.25,
        positionCode="CL",
        area=500,
    )
    # All fields present and correctly typed.
    assert d.identity == "Bhim"
    assert d.boundingBox == [10, 20, 30, 40]
    assert d.positionCode == "CL"
    assert d.area == 500


# ---------------------------------------------------------------
# Phase 2f (2026-04-28) — luminance threshold + closing + dark_lines/
# ---------------------------------------------------------------

class TestPhase2fHelpers:
    """Unit tests for the four new image-processing helpers."""

    def test_extract_dark_lines_keeps_dark_pixels(self):
        """Pixels with luminance < threshold pass through as ink (True)."""
        gray = np.array(
            [[10, 50, 79, 80, 100, 200, 255]],
            dtype=np.uint8,
        )
        # Default threshold = 80: <80 passes, >=80 fails.
        mask = _extract_dark_lines(gray, dark_threshold=80)
        assert mask.tolist() == [[True, True, True, False, False, False, False]]

    def test_extract_dark_lines_threshold_override(self):
        """Operator can dial threshold up or down via the parameter."""
        gray = np.array([[10, 50, 100, 150, 200]], dtype=np.uint8)
        # Strict (only very dark pixels): threshold=30
        strict = _extract_dark_lines(gray, dark_threshold=30)
        assert strict.tolist() == [[True, False, False, False, False]]
        # Lenient (most pixels pass): threshold=180
        lenient = _extract_dark_lines(gray, dark_threshold=180)
        assert lenient.tolist() == [[True, True, True, True, False]]

    def test_extract_dark_lines_default_constant_is_80(self):
        """The locked Phase 2f default is 80 — fits storyboard convention
        (dark bold black ~0-50 vs light grey BG ~80-180)."""
        assert DEFAULT_DARK_THRESHOLD == 80

    def test_close_outline_gaps_seals_one_pixel_gap(self):
        """A 1-pixel gap in a horizontal line gets sealed by closing."""
        # 5x5 image with a horizontal line that has a 1-pixel gap at x=2.
        binary = np.array([
            [False, False, False, False, False],
            [False, False, False, False, False],
            [True,  True,  False, True,  True ],   # gap at (2, 2)
            [False, False, False, False, False],
            [False, False, False, False, False],
        ])
        closed = _close_outline_gaps(binary)
        # The gap pixel (row=2, col=2) should now be True.
        assert bool(closed[2, 2]), (
            "Phase 2f closing must seal a 1-pixel gap; got open."
        )

    def test_close_outline_gaps_preserves_large_gaps(self):
        """A 5-pixel gap stays open (5x5 default kernel only closes "
           "1-2 pixel gaps; larger gaps survive)."""
        # 7x9 image: two 1-pixel-thick horizontal strokes with a 5-pixel gap.
        binary = np.zeros((7, 9), dtype=bool)
        binary[3, 0:2] = True   # left stroke (cols 0-1)
        binary[3, 7:9] = True   # right stroke (cols 7-8); gap at cols 2-6
        closed = _close_outline_gaps(binary)
        # The middle pixel of the gap (col=4) should still be False.
        assert not bool(closed[3, 4]), (
            "5-pixel gap should NOT be closed by a 3x3 kernel; got sealed."
        )

    def test_close_outline_gaps_default_kernel_is_3(self):
        assert DEFAULT_OUTLINE_CLOSING_KERNEL == 3

    def test_save_dark_lines_png_writes_correct_polarity(self, tmp_path: Path):
        """Phase 2f polarity: ink (True) → black (0); BG (False) → white (255)."""
        binary = np.array([
            [True,  False, True ],
            [False, True,  False],
            [True,  False, True ],
        ])
        out = tmp_path / "dark.png"
        _save_dark_lines_png(binary, out)
        assert out.is_file()
        loaded = np.asarray(Image.open(out).convert("L"), dtype=np.uint8)
        # Where binary was True, pixel should be 0 (black ink).
        # Where binary was False, pixel should be 255 (white BG).
        expected = np.where(binary, 0, 255).astype(np.uint8)
        assert (loaded == expected).all()

    def test_save_dark_lines_png_creates_parent_dir(self, tmp_path: Path):
        """`output_path.parent` is mkdir'd if it doesn't exist."""
        binary = np.array([[True, False]])
        out = tmp_path / "deeply" / "nested" / "dark.png"
        _save_dark_lines_png(binary, out)
        assert out.is_file()

    def test_wipe_dark_lines_dir_removes_stale_pngs(self, tmp_path: Path):
        """Rerun safety: existing *.png files in dark_lines/ are removed
        so the dir matches the current keypose set."""
        d = tmp_path / "dark_lines"
        d.mkdir()
        (d / "frame_0001.png").write_bytes(b"\x89PNG\x00")
        (d / "frame_0099.png").write_bytes(b"\x89PNG\x00")
        (d / "notes.txt").write_text("debug")  # non-PNG should survive
        _wipe_dark_lines_dir(d)
        assert not (d / "frame_0001.png").exists()
        assert not (d / "frame_0099.png").exists()
        assert (d / "notes.txt").exists(), (
            "Non-PNG files must survive the wipe (operator debug notes)."
        )

    def test_wipe_dark_lines_dir_creates_dir_when_missing(
        self, tmp_path: Path
    ):
        """First-run case: directory doesn't exist yet; wipe creates it."""
        d = tmp_path / "fresh_dark_lines"
        assert not d.exists()
        _wipe_dark_lines_dir(d)
        assert d.is_dir()


class TestPhase2fIntegration:
    """End-to-end tests that exercise the Phase 2f pipeline through
    `detect_characters_for_queue`."""

    def test_dark_lines_dir_created_per_shot(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        """Phase 2f writes `<shot>/dark_lines/<filename>` for each
        keypose Node 4 produced."""
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )
        dark_lines = tmp_path / "shot_001" / "dark_lines"
        assert dark_lines.is_dir(), "dark_lines/ must be created"
        pngs = sorted(dark_lines.glob("*.png"))
        assert len(pngs) >= 1, (
            "dark_lines/ must contain at least one PNG per keypose"
        )

    def test_dark_lines_filename_matches_keypose_filename(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        """Each dark_lines/ PNG is named the same as its source keypose
        — Node 7 derives the path by basename."""
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )
        keyposes = sorted((tmp_path / "shot_001" / "keyposes").glob("*.png"))
        dark_lines = sorted((tmp_path / "shot_001" / "dark_lines").glob("*.png"))
        kp_names = [p.name for p in keyposes]
        dl_names = [p.name for p in dark_lines]
        assert kp_names == dl_names, (
            f"dark_lines/ filenames must match keyposes/: "
            f"keyposes={kp_names} dark_lines={dl_names}"
        )

    def test_dark_lines_png_has_white_bg_black_ink(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        """Phase 2f writes BnW PNGs: white BG (255) + black ink (0).
        The character rectangle drawn at INK=20 should appear as 0 in
        the dark_lines output; surrounding BG=255 should appear as 255."""
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )
        dl = sorted((tmp_path / "shot_001" / "dark_lines").glob("*.png"))[0]
        arr = np.asarray(Image.open(dl).convert("L"), dtype=np.uint8)
        # Frame center should be black (the drawn character).
        cy, cx = arr.shape[0] // 2, arr.shape[1] // 2
        assert arr[cy, cx] == 0, (
            f"Expected black ink at frame center; got {arr[cy, cx]}"
        )
        # Top-left corner should be white BG.
        assert arr[0, 0] == 255

    def test_dark_lines_dir_wiped_on_rerun(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        """Stale dark_lines/*.png from a previous run get removed; new
        run's PNGs replace them."""
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        # Plant a stale file before running.
        dark_lines = tmp_path / "shot_001" / "dark_lines"
        dark_lines.mkdir(parents=True, exist_ok=True)
        stale = dark_lines / "stale_from_old_run.png"
        stale.write_bytes(b"\x89PNG\x00garbage")
        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )
        assert not stale.exists(), (
            "Stale dark_lines/*.png from old run must be wiped."
        )

    def test_character_map_records_dark_threshold_and_dir(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        """Phase 2f additive schema: character_map.json carries the
        threshold used + the dark_lines/ dir path."""
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
            dark_threshold=120,
        )
        cm = json.loads(
            (tmp_path / "shot_001" / "character_map.json").read_text(
                encoding="utf-8"
            )
        )
        assert cm["darkThreshold"] == 120
        assert cm["darkLinesDir"].endswith("dark_lines")

    def test_node5_result_records_dark_threshold(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        """Phase 2f additive schema: node5_result.json carries the
        threshold so downstream nodes / debugging can see what was used."""
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
            dark_threshold=120,
        )
        n5 = json.loads(
            (tmp_path / "node5_result.json").read_text(encoding="utf-8")
        )
        assert n5["darkThreshold"] == 120

    def test_default_dark_threshold_used_when_not_passed(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        """Calling without dark_threshold uses DEFAULT_DARK_THRESHOLD."""
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        result = detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
        )
        assert result.darkThreshold == DEFAULT_DARK_THRESHOLD

    def test_cli_dark_threshold_flag(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder, capsys
    ):
        """`--dark-threshold N` overrides the default."""
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        rc = cli_main([
            "--node4-result", str(tmp_path / "node4_result.json"),
            "--queue", str(tmp_path / "queue.json"),
            "--dark-threshold", "120",
            "--quiet",
        ])
        assert rc == 0
        n5 = json.loads(
            (tmp_path / "node5_result.json").read_text(encoding="utf-8")
        )
        assert n5["darkThreshold"] == 120

    def test_lenient_threshold_keeps_lighter_bg_lines(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        """Sanity: with --dark-threshold=255 (admit everything), the
        BG (255) gets erased (since pixels with luminance < 255 are kept,
        but pixel value = 255 fails strictly-less-than). Confirms the
        threshold semantics: pixels < threshold are kept."""
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
            dark_threshold=255,  # everything < 255 is kept
        )
        # Detection should still find the INK rectangle (luminance 20 < 255).
        cm = json.loads(
            (tmp_path / "shot_001" / "character_map.json").read_text(
                encoding="utf-8"
            )
        )
        kp = cm["keyPoses"][0]
        assert len(kp["detections"]) == 1, (
            "INK character at luminance 20 should always be detected when "
            "threshold >= 21; got count=" + str(len(kp["detections"]))
        )

    def test_strict_threshold_drops_normal_ink(
        self, tmp_path: Path, make_single_char_shot: ShotBuilder
    ):
        """With --dark-threshold=10 (admit only luminance 0-9), the
        INK=20 character gets erased and detection fails (no blob found).
        Confirms operator can tune to be stricter."""
        shot = make_single_char_shot("shot_001")
        _write_node4_result(tmp_path, [shot])
        _write_queue(tmp_path, [
            {"shotId": "shot_001", "characters": [{"identity": "Bhim", "position": "C"}]}
        ])
        detect_characters_for_queue(
            tmp_path / "node4_result.json",
            tmp_path / "queue.json",
            dark_threshold=10,  # only luminance 0-9 passes
        )
        cm = json.loads(
            (tmp_path / "shot_001" / "character_map.json").read_text(
                encoding="utf-8"
            )
        )
        kp = cm["keyPoses"][0]
        assert len(kp["detections"]) == 0, (
            "INK at luminance 20 should be erased when threshold=10; "
            "detections must drop to 0 (then reconcile-failed warning)."
        )
