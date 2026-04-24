"""Tests for Node 6 — Character Reference Sheet Matching.

Each test synthesizes a minimal Node 5 + Node 2 + Node 1 output layout
on a temporary work dir, plus a synthetic 8-angle RGBA reference sheet,
and runs Node 6 end-to-end against it. The classical multi-signal
scorer is deterministic on these synthetic fixtures (rectangular
silhouettes on white backgrounds), so tests assert on the written
files' structure + which angle wins, not on hand-crafted numerical
thresholds.

Run from repo root with:

    python -m pytest tests/ -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from pipeline.cli_node6 import main as cli_main
from pipeline.errors import (
    AngleMatchingError,
    AngleOrderUnconfirmedError,
    CharactersInputError,
    Node5ResultInputError,
    Node6Error,
    PipelineError,
    QueueLookupError,
    ReferenceSheetFormatError,
    ReferenceSheetSliceError,
)
from pipeline.node6 import (
    CANONICAL_ANGLES,
    DEFAULT_LINEART_METHOD,
    LINEART_METHODS,
    NORM_CANVAS,
    SCORE_WEIGHTS,
    Node6Result,
    ReferenceMap,
    match_references_for_queue,
    match_references_for_shot,
)


# ---------------------------------------------------------------
# Canonical-order sanity checks (locked 2026-04-23)
# ---------------------------------------------------------------

def test_canonical_angles_locked_order() -> None:
    assert CANONICAL_ANGLES == (
        "back",
        "back-3q-L",
        "profile-L",
        "front-3q-L",
        "front",
        "front-3q-R",
        "profile-R",
        "back-3q-R",
    )


def test_canonical_angles_use_ascii_3q_not_unicode() -> None:
    for name in CANONICAL_ANGLES:
        assert "¾" not in name, f"Angle {name!r} uses unicode ¾; expected ASCII '3q'"


def test_score_weights_sum_to_one() -> None:
    assert abs(sum(SCORE_WEIGHTS.values()) - 1.0) < 1e-9


def test_lineart_methods_include_dog_default() -> None:
    assert DEFAULT_LINEART_METHOD == "dog"
    assert "dog" in LINEART_METHODS
    assert "canny" in LINEART_METHODS
    assert "threshold" in LINEART_METHODS


# ---------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------

# Solid-grey silhouette color inside each reference-sheet island.
REF_INK = (96, 96, 96)
# Dark foreground ink on the synthetic key-pose PNGs. Otsu picks a
# threshold between this and the white background.
KEYPOSE_INK = 20
KEYPOSE_BG = 255


def _make_reference_sheet(
    path: Path,
    n_islands: int = 8,
    island_w: int = 24,
    island_h: int = 24,
    gap: int = 8,
) -> None:
    """Write an 8-island RGBA horizontal-strip reference sheet.

    Each island is a solid-grey rectangle separated by transparent gaps
    so `scipy.ndimage.label` sees them as distinct alpha islands.
    """
    H = island_h + 2 * gap
    stride = island_w + gap
    W = gap + n_islands * stride
    arr = np.zeros((H, W, 4), dtype=np.uint8)
    for i in range(n_islands):
        x0 = gap + i * stride
        x1 = x0 + island_w
        y0 = gap
        y1 = y0 + island_h
        arr[y0:y1, x0:x1, :3] = REF_INK
        arr[y0:y1, x0:x1, 3] = 255
    Image.fromarray(arr, mode="RGBA").save(path)


def _make_asymmetric_reference_sheet(path: Path) -> None:
    """Write a sheet where each of the 8 islands has a DIFFERENT shape.

    Used to verify angle selection actually varies with the detection.
    Angles 0..7 (canonical order: back, back-3q-L, profile-L,
    front-3q-L, front, front-3q-R, profile-R, back-3q-R).
    """
    # (w, h) per angle — deliberately varied aspect ratios.
    dims = [
        (16, 40),  # 0 back — tall narrow
        (20, 32),  # 1 back-3q-L
        (30, 20),  # 2 profile-L — wide short
        (22, 30),  # 3 front-3q-L
        (18, 42),  # 4 front — even taller narrow (distinct from back)
        (22, 30),  # 5 front-3q-R
        (30, 20),  # 6 profile-R
        (20, 32),  # 7 back-3q-R
    ]
    gap = 6
    max_h = max(h for _, h in dims)
    H = max_h + 2 * gap
    widths_with_gaps = [w + gap for w, _ in dims]
    W = gap + sum(widths_with_gaps)
    arr = np.zeros((H, W, 4), dtype=np.uint8)
    cursor = gap
    for (w, h) in dims:
        x0 = cursor
        x1 = x0 + w
        y0 = gap
        y1 = y0 + h
        arr[y0:y1, x0:x1, :3] = REF_INK
        arr[y0:y1, x0:x1, 3] = 255
        cursor = x1 + gap
    Image.fromarray(arr, mode="RGBA").save(path)


def _make_rgb_sheet_without_alpha(path: Path) -> None:
    """An 8-island sheet saved as RGB (no alpha channel). Should fail."""
    arr = np.full((40, 256, 3), 255, dtype=np.uint8)
    for i in range(8):
        x0 = 8 + i * 32
        arr[8:32, x0:x0 + 16, :] = 64  # grey rectangles on white
    Image.fromarray(arr, mode="RGB").save(path)


def _make_sheet_with_wrong_island_count(path: Path, n: int) -> None:
    """RGBA sheet with exactly `n` alpha islands (not 8)."""
    _make_reference_sheet(path, n_islands=n)


def _make_keypose_png(
    path: Path,
    width: int,
    height: int,
    boxes: list[tuple[int, int, int, int]],
) -> None:
    """Write a grayscale-compatible PNG with dark rectangles on white."""
    arr = np.full((height, width), KEYPOSE_BG, dtype=np.uint8)
    for (x, y, w, h) in boxes:
        arr[y:y + h, x:x + w] = KEYPOSE_INK
    Image.fromarray(arr, mode="L").save(path)


def _build_fixture(
    tmp_path: Path,
    *,
    shots: list[dict[str, Any]],
    identities: list[str] | None = None,
    angle_confirmed: bool = True,
    sheet_factory=_make_reference_sheet,
    identity_sheet_overrides: dict[str, Any] | None = None,
) -> dict[str, Path]:
    """Set up a complete Node 1 -> 2 -> 5 scaffold on disk for Node 6.

    Returns a dict of important paths: {input_dir, work_dir,
    queue_path, node5_result_path, characters_path}.

    `shots` is a list of dicts, one per shot:
        {
          "shotId": "shot_001",
          "kp_frame_w": 96, "kp_frame_h": 64,
          "key_poses": [
              {
                  "keyPoseIndex": 0,
                  "keyPoseFilename": "frame_0001.png",
                  "sourceFrame": 1,
                  "boxes": [(x, y, w, h), ...],   # ink rects for the PNG
                  "detections": [
                      {
                          "identity": "Bhim",
                          "expectedPosition": "CL",
                          "boundingBox": [x, y, w, h],
                      },
                      ...
                  ],
              },
              ...
          ],
        }
    """
    input_dir = tmp_path / "input"
    work_dir = tmp_path / "work"
    input_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    if identities is None:
        identities = sorted({
            det["identity"]
            for shot in shots
            for kp in shot["key_poses"]
            for det in kp["detections"]
            if det.get("identity")
        })

    # characters.json
    character_entries: list[dict[str, Any]] = []
    for ident in identities:
        sheet_path = input_dir / f"{ident.lower()}_sheet.png"
        overrides = (identity_sheet_overrides or {}).get(ident)
        if overrides is not None and callable(overrides):
            overrides(sheet_path)
        else:
            sheet_factory(sheet_path)
        character_entries.append({
            "name": ident,
            "sheetFilename": sheet_path.name,
            "width": 0,  # not read by Node 6
            "height": 0,
            "quality": {"ok": True, "detectedIslands": 8,
                        "backgroundMode": "transparent"},
            "addedAt": "2026-04-23T00:00:00.000Z",
        })

    characters_path = input_dir / "characters.json"
    characters_path.write_text(json.dumps({
        "schemaVersion": 1,
        "generatedAt": "2026-04-23T00:00:00.000Z",
        "conventions": {
            "sheetFormat": "8-angle horizontal strip",
            "backgroundExpected": "transparent",
            "angleOrderLeftToRight": list(CANONICAL_ANGLES),
            "angleOrderConfirmed": angle_confirmed,
        },
        "characters": character_entries,
    }, indent=2), encoding="utf-8")

    # queue.json — Node 2's output. Batches contain ShotJob-shaped dicts
    # with absolute sheetPath for each character.
    batches_payload = []
    for shot in shots:
        characters_payload = []
        for det in shot["key_poses"][0]["detections"]:
            if not det.get("identity"):
                continue
            ident = det["identity"]
            characters_payload.append({
                "identity": ident,
                "sheetPath": str(input_dir / f"{ident.lower()}_sheet.png"),
                "position": det.get("expectedPosition", "C"),
            })
        batches_payload.append([{
            "shotId": shot["shotId"],
            "mp4Path": str(input_dir / f"{shot['shotId']}.mp4"),
            "durationFrames": 1,
            "durationSeconds": 0.04,
            "characters": characters_payload,
        }])
    queue_path = input_dir / "queue.json"
    queue_path.write_text(json.dumps({
        "schemaVersion": 1,
        "projectName": "test-project",
        "batchSize": 1,
        "totalShots": len(shots),
        "batchCount": len(batches_payload),
        "batches": batches_payload,
    }, indent=2), encoding="utf-8")

    # Per-shot layout: key-pose PNGs + character_map.json
    # + aggregate node5_result.json.
    n5_shots_summary = []
    for shot in shots:
        shot_root = work_dir / shot["shotId"]
        keyposes_dir = shot_root / "keyposes"
        keyposes_dir.mkdir(parents=True, exist_ok=True)
        kp_frame_w = shot.get("kp_frame_w", 96)
        kp_frame_h = shot.get("kp_frame_h", 64)

        cm_keyposes = []
        for kp in shot["key_poses"]:
            kp_png_path = keyposes_dir / kp["keyPoseFilename"]
            _make_keypose_png(
                kp_png_path,
                width=kp_frame_w,
                height=kp_frame_h,
                boxes=kp.get("boxes", []),
            )
            cm_keyposes.append({
                "keyPoseIndex": kp["keyPoseIndex"],
                "keyPoseFilename": kp["keyPoseFilename"],
                "sourceFrame": kp["sourceFrame"],
                "frameWidth": kp_frame_w,
                "frameHeight": kp_frame_h,
                "detections": [
                    {
                        "identity": d.get("identity", ""),
                        "expectedPosition": d.get("expectedPosition", ""),
                        "boundingBox": list(d["boundingBox"]),
                        "centerX": 0.5,
                        "positionCode": d.get("expectedPosition", ""),
                        "area": int(d["boundingBox"][2] * d["boundingBox"][3]),
                    }
                    for d in kp["detections"]
                ],
                "warnings": [],
            })

        cm_path = shot_root / "character_map.json"
        cm_path.write_text(json.dumps({
            "schemaVersion": 1,
            "shotId": shot["shotId"],
            "expectedCharacterCount": len(
                shot["key_poses"][0]["detections"]
            ),
            "expectedCharacters": [],
            "sourceFramesDir": str(shot_root),
            "keyPosesDir": str(keyposes_dir),
            "minAreaRatio": 0.001,
            "mergeIou": 0.5,
            "keyPoses": cm_keyposes,
        }, indent=2), encoding="utf-8")

        n5_shots_summary.append({
            "shotId": shot["shotId"],
            "expectedCharacterCount": len(
                shot["key_poses"][0]["detections"]
            ),
            "keyPoseCount": len(shot["key_poses"]),
            "totalDetections": sum(
                len(kp["detections"]) for kp in shot["key_poses"]
            ),
            "warningCount": 0,
            "characterMapPath": str(cm_path),
        })

    node5_result_path = work_dir / "node5_result.json"
    node5_result_path.write_text(json.dumps({
        "schemaVersion": 1,
        "projectName": "test-project",
        "workDir": str(work_dir),
        "minAreaRatio": 0.001,
        "mergeIou": 0.5,
        "detectedAt": "2026-04-23T00:00:00.000Z",
        "shots": n5_shots_summary,
    }, indent=2), encoding="utf-8")

    return {
        "input_dir": input_dir,
        "work_dir": work_dir,
        "queue_path": queue_path,
        "node5_result_path": node5_result_path,
        "characters_path": characters_path,
    }


# ---------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------

def test_happy_path_single_character_single_keypose(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{
            "keyPoseIndex": 0,
            "keyPoseFilename": "frame_0001.png",
            "sourceFrame": 1,
            "boxes": [(40, 20, 16, 24)],
            "detections": [{
                "identity": "Bhim",
                "expectedPosition": "C",
                "boundingBox": [40, 20, 16, 24],
            }],
        }],
    }])

    result = match_references_for_queue(
        node5_result_path=fx["node5_result_path"],
        queue_path=fx["queue_path"],
        characters_path=fx["characters_path"],
    )

    assert isinstance(result, Node6Result)
    assert result.projectName == "test-project"
    assert len(result.shots) == 1
    s = result.shots[0]
    assert s.shotId == "shot_001"
    assert s.keyPoseCount == 1
    assert s.detectionCount == 1
    assert s.skippedCount == 0
    assert s.angleHistogram
    # exactly one angle picked, with exactly one occurrence
    assert sum(s.angleHistogram.values()) == 1

    # node6_result.json written alongside node5_result.json
    assert (fx["work_dir"] / "node6_result.json").is_file()
    # reference_map.json written in the shot root
    rm_path = Path(s.referenceMapPath)
    assert rm_path.is_file()
    rm = json.loads(rm_path.read_text(encoding="utf-8"))
    assert rm["schemaVersion"] == 1
    assert rm["shotId"] == "shot_001"
    assert rm["lineArtMethod"] == DEFAULT_LINEART_METHOD
    assert len(rm["keyPoses"]) == 1
    matches = rm["keyPoses"][0]["matches"]
    assert len(matches) == 1
    m = matches[0]
    assert m["identity"] == "Bhim"
    assert m["selectedAngle"] in CANONICAL_ANGLES
    # Full breakdown present.
    for key in ("iou", "symmetry", "aspect", "edgeDensity", "final"):
        assert key in m["scoreBreakdown"]
    # All 8 angles scored.
    assert set(m["allScores"].keys()) == set(CANONICAL_ANGLES)

    # Color + line-art PNGs written
    assert Path(m["referenceColorCropPath"]).is_file()
    assert Path(m["referenceLineArtCropPath"]).is_file()


def test_two_characters_same_shot_both_matched(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{
            "keyPoseIndex": 0,
            "keyPoseFilename": "frame_0001.png",
            "sourceFrame": 1,
            "boxes": [(8, 16, 20, 30), (68, 16, 20, 30)],
            "detections": [
                {"identity": "Bhim", "expectedPosition": "L",
                 "boundingBox": [8, 16, 20, 30]},
                {"identity": "Chutki", "expectedPosition": "R",
                 "boundingBox": [68, 16, 20, 30]},
            ],
        }],
    }])

    result = match_references_for_queue(
        node5_result_path=fx["node5_result_path"],
        queue_path=fx["queue_path"],
        characters_path=fx["characters_path"],
    )
    assert result.shots[0].detectionCount == 2
    rm = json.loads(Path(result.shots[0].referenceMapPath).read_text())
    idents = [m["identity"] for m in rm["keyPoses"][0]["matches"]]
    assert sorted(idents) == ["Bhim", "Chutki"]


def test_multiple_keyposes_share_cache_when_same_angle(tmp_path: Path) -> None:
    """Two key poses for the same identity picking the same angle should
    produce ONE color crop + ONE lineart crop (not two of each)."""
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [
            {"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
             "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
             "detections": [{"identity": "Bhim", "expectedPosition": "C",
                             "boundingBox": [40, 20, 16, 24]}]},
            {"keyPoseIndex": 1, "keyPoseFilename": "frame_0030.png",
             "sourceFrame": 30, "boxes": [(40, 20, 16, 24)],
             "detections": [{"identity": "Bhim", "expectedPosition": "C",
                             "boundingBox": [40, 20, 16, 24]}]},
        ],
    }])

    result = match_references_for_queue(
        node5_result_path=fx["node5_result_path"],
        queue_path=fx["queue_path"],
        characters_path=fx["characters_path"],
    )
    s = result.shots[0]
    rm = json.loads(Path(s.referenceMapPath).read_text())

    # Both key poses resolved with identical (Bhim, <some angle>) so
    # both point at the SAME color + lineart files.
    m0 = rm["keyPoses"][0]["matches"][0]
    m1 = rm["keyPoses"][1]["matches"][0]
    assert m0["selectedAngle"] == m1["selectedAngle"]
    assert m0["referenceColorCropPath"] == m1["referenceColorCropPath"]
    assert m0["referenceLineArtCropPath"] == m1["referenceLineArtCropPath"]

    # Exactly one pair of files in reference_crops/
    crops_dir = Path(rm["referenceCropsDir"])
    color_files = sorted(crops_dir.glob("*.png"))
    # Expect 2 files: <identity>_<angle>.png and ..._lineart.png
    assert len(color_files) == 2


def test_different_detections_can_pick_different_angles(tmp_path: Path) -> None:
    """When the detection silhouettes have DIFFERENT aspect ratios and
    the reference sheet has angle-varied shapes, different key poses
    should be able to pick different angles (though ties are allowed)."""
    fx = _build_fixture(
        tmp_path,
        shots=[{
            "shotId": "shot_001",
            "kp_frame_w": 128,
            "kp_frame_h": 80,
            "key_poses": [
                # Tall narrow detection — best matches a tall narrow angle
                {"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                 "sourceFrame": 1, "boxes": [(56, 10, 16, 60)],
                 "detections": [{"identity": "Bhim", "expectedPosition": "C",
                                 "boundingBox": [56, 10, 16, 60]}]},
                # Wide short detection — best matches a wide short angle
                {"keyPoseIndex": 1, "keyPoseFilename": "frame_0030.png",
                 "sourceFrame": 30, "boxes": [(40, 30, 48, 20)],
                 "detections": [{"identity": "Bhim", "expectedPosition": "C",
                                 "boundingBox": [40, 30, 48, 20]}]},
            ],
        }],
        sheet_factory=_make_asymmetric_reference_sheet,
    )

    result = match_references_for_queue(
        node5_result_path=fx["node5_result_path"],
        queue_path=fx["queue_path"],
        characters_path=fx["characters_path"],
    )
    rm = json.loads(Path(result.shots[0].referenceMapPath).read_text())
    tall_pick = rm["keyPoses"][0]["matches"][0]["selectedAngle"]
    wide_pick = rm["keyPoses"][1]["matches"][0]["selectedAngle"]
    # Tall-narrow detection should prefer a tall-narrow reference
    # angle (angles 0 and 4 in our asymmetric sheet are tall-narrow;
    # angles 2 and 6 are wide-short).
    tall_angles = {"back", "front"}
    wide_angles = {"profile-L", "profile-R"}
    assert tall_pick in tall_angles, (
        f"Tall detection should prefer a tall angle; picked {tall_pick}"
    )
    assert wide_pick in wide_angles, (
        f"Wide detection should prefer a wide angle; picked {wide_pick}"
    )


# ---------------------------------------------------------------
# Reference-sheet format / slicing errors
# ---------------------------------------------------------------

def test_rgb_sheet_without_alpha_channel_fails(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }], identity_sheet_overrides={"Bhim": _make_rgb_sheet_without_alpha})

    with pytest.raises(ReferenceSheetFormatError) as ei:
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )
    assert "RGBA" in str(ei.value) or "transparent" in str(ei.value)


def test_sheet_with_seven_islands_fails(tmp_path: Path) -> None:
    def _seven(path: Path) -> None:
        _make_sheet_with_wrong_island_count(path, n=7)
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }], identity_sheet_overrides={"Bhim": _seven})

    with pytest.raises(ReferenceSheetSliceError) as ei:
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )
    assert "7" in str(ei.value)


def test_sheet_with_nine_islands_fails(tmp_path: Path) -> None:
    def _nine(path: Path) -> None:
        _make_sheet_with_wrong_island_count(path, n=9)
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }], identity_sheet_overrides={"Bhim": _nine})

    with pytest.raises(ReferenceSheetSliceError) as ei:
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )
    assert "9" in str(ei.value)


# ---------------------------------------------------------------
# characters.json gate
# ---------------------------------------------------------------

def test_angle_order_not_confirmed_fails(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }], angle_confirmed=False)

    with pytest.raises(AngleOrderUnconfirmedError) as ei:
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )
    # Helpful message names the canonical order.
    msg = str(ei.value)
    assert "back" in msg and "front" in msg and "profile-L" in msg


def test_characters_json_missing_fails(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }])
    fx["characters_path"].unlink()

    with pytest.raises(CharactersInputError):
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )


def test_characters_json_invalid_json_fails(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }])
    fx["characters_path"].write_text("{not valid json", encoding="utf-8")

    with pytest.raises(CharactersInputError):
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )


# ---------------------------------------------------------------
# Node 5 result + queue input errors
# ---------------------------------------------------------------

def test_node5_result_missing_fails(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }])
    fx["node5_result_path"].unlink()

    with pytest.raises(Node5ResultInputError):
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )


def test_node5_result_schema_version_guard(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }])
    raw = json.loads(fx["node5_result_path"].read_text(encoding="utf-8"))
    raw["schemaVersion"] = 2
    fx["node5_result_path"].write_text(
        json.dumps(raw, indent=2), encoding="utf-8"
    )

    with pytest.raises(Node5ResultInputError) as ei:
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )
    assert "schemaVersion" in str(ei.value)


def test_queue_missing_fails(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }])
    fx["queue_path"].unlink()

    with pytest.raises(QueueLookupError):
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )


def test_queue_missing_shot_id_fails(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }])
    # Rename the queue's shotId so node5_result's reference misses.
    raw = json.loads(fx["queue_path"].read_text(encoding="utf-8"))
    raw["batches"][0][0]["shotId"] = "shot_999"
    fx["queue_path"].write_text(json.dumps(raw, indent=2), encoding="utf-8")

    with pytest.raises(QueueLookupError) as ei:
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )
    assert "shot_001" in str(ei.value)


# ---------------------------------------------------------------
# Detection-side error paths
# ---------------------------------------------------------------

def test_unpaired_detection_is_skipped_not_fatal(tmp_path: Path) -> None:
    """Node 5 may emit a detection with identity="" when reconcile
    couldn't pair it. Node 6 should skip it and record in `skipped`."""
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{
            "keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
            "sourceFrame": 1,
            "boxes": [(8, 16, 20, 30), (68, 16, 20, 30)],
            "detections": [
                {"identity": "Bhim", "expectedPosition": "L",
                 "boundingBox": [8, 16, 20, 30]},
                # Unpaired leftover (Node 5 reconcile-leftover).
                {"identity": "", "expectedPosition": "",
                 "boundingBox": [68, 16, 20, 30]},
            ],
        }],
    }])

    result = match_references_for_queue(
        node5_result_path=fx["node5_result_path"],
        queue_path=fx["queue_path"],
        characters_path=fx["characters_path"],
    )
    s = result.shots[0]
    assert s.detectionCount == 1
    assert s.skippedCount == 1
    rm = json.loads(Path(s.referenceMapPath).read_text())
    skipped = rm["keyPoses"][0]["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "unpaired-detection"


def test_bbox_entirely_outside_frame_raises(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "kp_frame_w": 64,
        "kp_frame_h": 48,
        "key_poses": [{
            "keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
            "sourceFrame": 1,
            "boxes": [],
            "detections": [{"identity": "Bhim", "expectedPosition": "C",
                            "boundingBox": [200, 200, 10, 10]}],
        }],
    }])

    with pytest.raises(AngleMatchingError):
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )


def test_bbox_over_pure_background_raises(tmp_path: Path) -> None:
    """Bbox inside the frame but at a pixel region with no ink."""
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{
            "keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
            "sourceFrame": 1,
            # No ink rectangles at all — keypose is pure white.
            "boxes": [],
            "detections": [{"identity": "Bhim", "expectedPosition": "C",
                            "boundingBox": [10, 10, 12, 12]}],
        }],
    }])

    with pytest.raises(AngleMatchingError):
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
        )


# ---------------------------------------------------------------
# Line-art methods
# ---------------------------------------------------------------

@pytest.mark.parametrize("method", ["dog", "canny", "threshold"])
def test_lineart_method_produces_nonempty_png(
    tmp_path: Path, method: str,
) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{
            "keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
            "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
            "detections": [{"identity": "Bhim", "expectedPosition": "C",
                            "boundingBox": [40, 20, 16, 24]}],
        }],
    }])

    result = match_references_for_queue(
        node5_result_path=fx["node5_result_path"],
        queue_path=fx["queue_path"],
        characters_path=fx["characters_path"],
        lineart_method=method,
    )
    assert result.lineArtMethod == method
    rm = json.loads(Path(result.shots[0].referenceMapPath).read_text())
    assert rm["lineArtMethod"] == method
    m = rm["keyPoses"][0]["matches"][0]
    lineart = Image.open(m["referenceLineArtCropPath"]).convert("RGBA")
    # Ensure we wrote SOMETHING — at least a few non-zero alpha pixels
    # (the alpha-boundary contribution is guaranteed for a non-empty
    # reference island).
    arr = np.asarray(lineart)
    assert int(arr[..., 3].sum()) > 0


def test_invalid_lineart_method_raises(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{
            "keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
            "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
            "detections": [{"identity": "Bhim", "expectedPosition": "C",
                            "boundingBox": [40, 20, 16, 24]}],
        }],
    }])
    with pytest.raises(CharactersInputError):
        match_references_for_queue(
            node5_result_path=fx["node5_result_path"],
            queue_path=fx["queue_path"],
            characters_path=fx["characters_path"],
            lineart_method="bogus",
        )


# ---------------------------------------------------------------
# Rerun safety
# ---------------------------------------------------------------

def test_rerun_wipes_stale_crops(tmp_path: Path) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{
            "keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
            "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
            "detections": [{"identity": "Bhim", "expectedPosition": "C",
                            "boundingBox": [40, 20, 16, 24]}],
        }],
    }])

    result1 = match_references_for_queue(
        node5_result_path=fx["node5_result_path"],
        queue_path=fx["queue_path"],
        characters_path=fx["characters_path"],
    )
    crops_dir = Path(json.loads(
        Path(result1.shots[0].referenceMapPath).read_text()
    )["referenceCropsDir"])

    # Plant a stale file
    stale = crops_dir / "stale_leftover.png"
    stale.write_bytes(b"stale")
    assert stale.is_file()

    result2 = match_references_for_queue(
        node5_result_path=fx["node5_result_path"],
        queue_path=fx["queue_path"],
        characters_path=fx["characters_path"],
    )
    # Stale file is wiped; only fresh cache output remains.
    assert not stale.exists()
    assert crops_dir.is_dir()
    # Re-run still produced the right files.
    rm2 = json.loads(Path(result2.shots[0].referenceMapPath).read_text())
    m2 = rm2["keyPoses"][0]["matches"][0]
    assert Path(m2["referenceColorCropPath"]).is_file()
    assert Path(m2["referenceLineArtCropPath"]).is_file()


# ---------------------------------------------------------------
# Aggregate result shape
# ---------------------------------------------------------------

def test_aggregate_result_angle_histogram_sums_to_detection_count(
    tmp_path: Path,
) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [
            {"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
             "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
             "detections": [{"identity": "Bhim", "expectedPosition": "C",
                             "boundingBox": [40, 20, 16, 24]}]},
            {"keyPoseIndex": 1, "keyPoseFilename": "frame_0030.png",
             "sourceFrame": 30, "boxes": [(40, 20, 16, 24)],
             "detections": [{"identity": "Bhim", "expectedPosition": "C",
                             "boundingBox": [40, 20, 16, 24]}]},
        ],
    }])
    result = match_references_for_queue(
        node5_result_path=fx["node5_result_path"],
        queue_path=fx["queue_path"],
        characters_path=fx["characters_path"],
    )
    s = result.shots[0]
    assert sum(s.angleHistogram.values()) == s.detectionCount


def test_match_references_for_shot_direct_api(tmp_path: Path) -> None:
    """match_references_for_shot() is callable directly (no queue/n5
    manifest loading), with the caller supplying sheet paths."""
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{
            "keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
            "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
            "detections": [{"identity": "Bhim", "expectedPosition": "C",
                            "boundingBox": [40, 20, 16, 24]}],
        }],
    }])
    shot_root = fx["work_dir"] / "shot_001"
    summary = match_references_for_shot(
        shot_id="shot_001",
        character_map_path=shot_root / "character_map.json",
        sheet_paths_by_identity={
            "Bhim": fx["input_dir"] / "bhim_sheet.png",
        },
    )
    assert summary.shotId == "shot_001"
    assert summary.detectionCount == 1
    assert (shot_root / "reference_map.json").is_file()


# ---------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------

def test_cli_happy_path_exits_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{
            "keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
            "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
            "detections": [{"identity": "Bhim", "expectedPosition": "C",
                            "boundingBox": [40, 20, 16, 24]}],
        }],
    }])

    rc = cli_main([
        "--node5-result", str(fx["node5_result_path"]),
        "--queue", str(fx["queue_path"]),
        "--characters", str(fx["characters_path"]),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[node6] OK" in out


def test_cli_quiet_flag_suppresses_success_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{
            "keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
            "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
            "detections": [{"identity": "Bhim", "expectedPosition": "C",
                            "boundingBox": [40, 20, 16, 24]}],
        }],
    }])

    rc = cli_main([
        "--node5-result", str(fx["node5_result_path"]),
        "--queue", str(fx["queue_path"]),
        "--characters", str(fx["characters_path"]),
        "--quiet",
    ])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cli_returns_one_on_node6_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing node5_result.json should exit 1 (Node5ResultInputError
    is a Node6Error subclass)."""
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }])
    fx["node5_result_path"].unlink()

    rc = cli_main([
        "--node5-result", str(fx["node5_result_path"]),
        "--queue", str(fx["queue_path"]),
        "--characters", str(fx["characters_path"]),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "[node6] FAILED" in err


def test_cli_returns_one_on_shared_queue_lookup_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """QueueLookupError lives under Node5Error's subtree but is also
    raised from Node 6's code path — the CLI catches it explicitly."""
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }])
    fx["queue_path"].unlink()

    rc = cli_main([
        "--node5-result", str(fx["node5_result_path"]),
        "--queue", str(fx["queue_path"]),
        "--characters", str(fx["characters_path"]),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "[node6] FAILED" in err


def test_cli_rejects_invalid_lineart_method(
    tmp_path: Path,
) -> None:
    fx = _build_fixture(tmp_path, shots=[{
        "shotId": "shot_001",
        "key_poses": [{"keyPoseIndex": 0, "keyPoseFilename": "frame_0001.png",
                       "sourceFrame": 1, "boxes": [(40, 20, 16, 24)],
                       "detections": [{"identity": "Bhim",
                                       "expectedPosition": "C",
                                       "boundingBox": [40, 20, 16, 24]}]}],
    }])

    # argparse choices= will reject the bogus value with SystemExit(2).
    with pytest.raises(SystemExit):
        cli_main([
            "--node5-result", str(fx["node5_result_path"]),
            "--queue", str(fx["queue_path"]),
            "--characters", str(fx["characters_path"]),
            "--lineart-method", "bogus",
        ])


# ---------------------------------------------------------------
# Error-hierarchy sanity: every Node 6 error is a PipelineError.
# ---------------------------------------------------------------

def test_node6_error_is_pipeline_error_subclass() -> None:
    assert issubclass(Node6Error, PipelineError)
    assert issubclass(Node5ResultInputError, Node6Error)
    assert issubclass(CharactersInputError, Node6Error)
    assert issubclass(AngleOrderUnconfirmedError, CharactersInputError)
    assert issubclass(ReferenceSheetFormatError, Node6Error)
    assert issubclass(ReferenceSheetSliceError, Node6Error)
    assert issubclass(AngleMatchingError, Node6Error)
