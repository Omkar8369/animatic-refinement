"""Node 6 — Character Reference Sheet Matching.

Reads three inputs:

  * `node5_result.json` — Node 5's aggregate manifest (points at each
    shot's `character_map.json` + keyposes folder).
  * `queue.json`        — Node 2's output (supplies each character's
    absolute `sheetPath`).
  * `characters.json`   — Node 1's output (only read for
    `conventions.angleOrderConfirmed`; the canonical 8-angle order is
    hard-coded and a `false` flag trips `AngleOrderUnconfirmedError`).

For every detection in every key pose in every shot Node 6:

  1. Slices the character's reference sheet into 8 alpha-island crops
     (once per `(shotId, identity)` in a run — sheet PNGs on disk are
     never opened twice for the same shot-character pair).
  2. Recomputes a silhouette for the detection from the key-pose PNG +
     Node 5's bbox (Otsu on the crop, largest connected component).
  3. Scores each of the 8 angles against the detection silhouette using
     a classical multi-signal function:
       * silhouette IoU on a 128x128 normalized canvas,
       * horizontal-symmetry consistency (front/back/profile signal),
       * bbox aspect-ratio match,
       * interior-edge density in the upper head region (front vs. back
         tie-break — back has no face features).
  4. Picks the maximum-scoring angle, writes the selected color crop
     into `<shotId>/reference_crops/<identity>_<angle>.png`, and writes
     a DoG line-art copy into `..._lineart.png`. Multiple key poses that
     select the same angle for the same identity share one file.

Writes per-shot `<shotId>/reference_map.json` + aggregate
`<work-dir>/node6_result.json`.

Sub-steps (aligned with `docs/PLAN.md` Node 6):

  6A. Load + validate `node5_result.json`, `queue.json`, and
      `characters.json` (the last for the angle-order-confirmed gate).
  6B. Slice each referenced sheet into 8 alpha-island crops, sorted
      left-to-right; assign canonical angle names by position.
  6C. Per detection: recompute silhouette, score all 8 angles, pick
      the maximum.
  6D. Cache-aware write of color + DoG line-art crops into
      `<shotId>/reference_crops/`.
  6E. Emit per-shot `reference_map.json` + aggregate
      `node6_result.json`.

Design decisions (locked 2026-04-23) — see CLAUDE.md's Node 6 section
for the full reasoning on each of the 10 points. Summary:

  * Sheet slicing requires an alpha channel; RGB-only sheets fail loud
    (`ReferenceSheetFormatError`). No Otsu-on-grayscale fallback.
  * Canonical 8-angle order is fixed in code; a `false` flag in
    `characters.json` raises `AngleOrderUnconfirmedError`.
  * Angle matching is classical (no ML, no GPU). Multi-signal score.
  * Silhouettes are recomputed here, not imported from Node 5. Node 5's
    `character_map.json` stays text-only (bboxes + identities +
    warnings).
  * Line-art method is `dog` by default; `--lineart-method` keeps a
    door open for `canny` / `threshold` without changing the contract.
  * Per-key-pose angle selection (not per-shot): one shot can span
    multiple angles for the same character when the camera/body
    rotates.
  * Thin ComfyUI wrapper (Option C — same template as Nodes 3/4/5).
  * `reference_crops/` is wiped before each run.
  * Single-threaded.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import (
    AngleMatchingError,
    AngleOrderUnconfirmedError,
    CharactersInputError,
    Node5ResultInputError,
    QueueLookupError,
    ReferenceSheetFormatError,
    ReferenceSheetSliceError,
)


# -------------------------------------------------------------------
# Canonical angle order (locked 2026-04-23)
# -------------------------------------------------------------------

# Left-to-right on the reference sheet. L/R refers to the CHARACTER's
# anatomical left/right, not the viewer's. ASCII `3q` matches what
# `frontend/characters.js` emits; `¾` in prose is equivalent.
CANONICAL_ANGLES: tuple[str, ...] = (
    "back",
    "back-3q-L",
    "profile-L",
    "front-3q-L",
    "front",
    "front-3q-R",
    "profile-R",
    "back-3q-R",
)

# Line-art methods accepted by the CLI. Only `dog` is tuned for v1;
# the others exist so we can A/B them without a code change later.
LINEART_METHODS: tuple[str, ...] = ("dog", "canny", "threshold")
DEFAULT_LINEART_METHOD = "dog"

# Canvas size for normalized silhouette comparison. 128x128 is small
# enough to keep scoring in milliseconds and large enough that 8-way
# angle discrimination is stable.
NORM_CANVAS = 128

# Classical scoring weights. IoU dominates because it's the most direct
# shape signal; symmetry/aspect/edge-density are tie-breakers.
SCORE_WEIGHTS = {
    "iou": 0.50,
    "symmetry": 0.20,
    "aspect": 0.15,
    "edgeDensity": 0.15,
}


# -------------------------------------------------------------------
# Public result types
# -------------------------------------------------------------------

@dataclass
class ReferenceMatch:
    """One identity's reference-sheet match on one key pose.

    `scoreBreakdown` carries the four sub-signals plus the final
    weighted score. `allScores` maps every angle name to its final
    score so an operator can debug a questionable pick.
    """
    identity: str
    expectedPosition: str
    boundingBox: list[int]
    selectedAngle: str
    scoreBreakdown: dict[str, float]
    allScores: dict[str, float]
    referenceColorCropPath: str
    referenceLineArtCropPath: str


@dataclass
class KeyPoseReferences:
    """All reference matches + warnings for a single key pose."""
    keyPoseIndex: int
    keyPoseFilename: str
    sourceFrame: int
    matches: list[ReferenceMatch] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyPoseIndex": self.keyPoseIndex,
            "keyPoseFilename": self.keyPoseFilename,
            "sourceFrame": self.sourceFrame,
            "matches": [
                {
                    "identity": m.identity,
                    "expectedPosition": m.expectedPosition,
                    "boundingBox": m.boundingBox,
                    "selectedAngle": m.selectedAngle,
                    "scoreBreakdown": m.scoreBreakdown,
                    "allScores": m.allScores,
                    "referenceColorCropPath": m.referenceColorCropPath,
                    "referenceLineArtCropPath": m.referenceLineArtCropPath,
                }
                for m in self.matches
            ],
            "skipped": list(self.skipped),
        }


@dataclass
class ReferenceMap:
    """Per-shot reference-match manifest.

    Written to `<shotId>/reference_map.json` alongside
    `character_map.json` and the keyposes folder. Node 7 reads this to
    route each detection to the right conditioning image.
    """
    schemaVersion: int = 1
    shotId: str = ""
    sourceFramesDir: str = ""
    keyPosesDir: str = ""
    referenceCropsDir: str = ""
    lineArtMethod: str = DEFAULT_LINEART_METHOD
    keyPoses: list[KeyPoseReferences] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "shotId": self.shotId,
            "sourceFramesDir": self.sourceFramesDir,
            "keyPosesDir": self.keyPosesDir,
            "referenceCropsDir": self.referenceCropsDir,
            "lineArtMethod": self.lineArtMethod,
            "keyPoses": [kp.to_dict() for kp in self.keyPoses],
        }


@dataclass
class ShotReferenceSummary:
    """One-line summary of a shot's Node 6 output."""
    shotId: str
    keyPoseCount: int
    detectionCount: int
    skippedCount: int
    referenceMapPath: str
    angleHistogram: dict[str, int] = field(default_factory=dict)


@dataclass
class Node6Result:
    """Aggregate Node 6 result. Written to `<work-dir>/node6_result.json`."""
    schemaVersion: int = 1
    projectName: str = ""
    workDir: str = ""
    lineArtMethod: str = DEFAULT_LINEART_METHOD
    matchedAt: str = ""
    shots: list[ShotReferenceSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "projectName": self.projectName,
            "workDir": self.workDir,
            "lineArtMethod": self.lineArtMethod,
            "matchedAt": self.matchedAt,
            "shots": [
                {
                    "shotId": s.shotId,
                    "keyPoseCount": s.keyPoseCount,
                    "detectionCount": s.detectionCount,
                    "skippedCount": s.skippedCount,
                    "referenceMapPath": s.referenceMapPath,
                    "angleHistogram": dict(s.angleHistogram),
                }
                for s in self.shots
            ],
        }


# -------------------------------------------------------------------
# Public entry points
# -------------------------------------------------------------------

def match_references_for_queue(
    node5_result_path: Path | str,
    queue_path: Path | str,
    characters_path: Path | str,
    lineart_method: str = DEFAULT_LINEART_METHOD,
) -> Node6Result:
    """Run reference-sheet matching across every shot in Node 5's output.

    Args:
        node5_result_path: Path to `node5_result.json` (Node 5's aggregate).
            Node 6 writes `node6_result.json` alongside it in the same
            work directory.
        queue_path: Path to `queue.json` (Node 2's output). Provides
            each character's absolute `sheetPath`.
        characters_path: Path to `characters.json` (Node 1's output).
            Only `conventions.angleOrderConfirmed` is read — a `False`
            flag aborts Node 6 with `AngleOrderUnconfirmedError`.
        lineart_method: One of `LINEART_METHODS`. `dog` is the default
            and recommended v1 setting.

    Returns:
        Node6Result. Per-shot + aggregate manifests are already on disk
        when this returns.

    Raises:
        Node5ResultInputError: `node5_result.json` missing/malformed or
            references files that no longer exist.
        QueueLookupError: `queue.json` missing/malformed or does not
            contain a shotId that appears in `node5_result.json`.
        CharactersInputError: `characters.json` missing/malformed.
        AngleOrderUnconfirmedError: `conventions.angleOrderConfirmed`
            is `False`.
        ReferenceSheetFormatError: a sheet PNG lacks an alpha channel.
        ReferenceSheetSliceError: alpha-island slicing of a sheet did
            not produce exactly 8 islands.
        AngleMatchingError: a key-pose crop could not be re-silhouetted
            (e.g. Node 5 bbox points at an empty region).
    """
    if lineart_method not in LINEART_METHODS:
        raise CharactersInputError(
            f"Unsupported lineart_method={lineart_method!r}; "
            f"expected one of {LINEART_METHODS}."
        )

    n5_path = Path(node5_result_path).resolve()
    q_path = Path(queue_path).resolve()
    c_path = Path(characters_path).resolve()

    n5 = _load_node5_result(n5_path)
    queue = _load_queue(q_path)
    shot_sheet_lookup = _build_shot_sheet_lookup(queue, n5)

    _check_angle_order_confirmed(c_path)

    work_dir = Path(n5["workDir"]).resolve()

    result = Node6Result(
        projectName=n5.get("projectName", ""),
        workDir=str(work_dir),
        lineArtMethod=lineart_method,
        matchedAt=datetime.now(timezone.utc).isoformat(),
    )

    for shot in n5["shots"]:
        shot_id = shot["shotId"]
        sheets = shot_sheet_lookup[shot_id]
        summary = match_references_for_shot(
            shot_id=shot_id,
            character_map_path=Path(shot["characterMapPath"]),
            sheet_paths_by_identity=sheets,
            lineart_method=lineart_method,
        )
        result.shots.append(summary)

    (work_dir / "node6_result.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


def match_references_for_shot(
    shot_id: str,
    character_map_path: Path | str,
    sheet_paths_by_identity: dict[str, Path],
    lineart_method: str = DEFAULT_LINEART_METHOD,
) -> ShotReferenceSummary:
    """Run reference-sheet matching on every detection in every key
    pose of one shot.

    Writes `<shot-root>/reference_map.json` and populates
    `<shot-root>/reference_crops/` with color + line-art crops.
    """
    character_map_path = Path(character_map_path).resolve()
    if not character_map_path.is_file():
        raise Node5ResultInputError(
            f"{shot_id}: character_map.json not found at "
            f"{character_map_path}. Did Node 5 complete?"
        )
    cm = _load_character_map(character_map_path, shot_id)

    shot_root = character_map_path.parent
    keyposes_dir = Path(cm["keyPosesDir"]).resolve()
    source_frames_dir = Path(cm["sourceFramesDir"]).resolve()
    if not keyposes_dir.is_dir():
        raise Node5ResultInputError(
            f"{shot_id}: keyposes folder does not exist: {keyposes_dir}."
        )

    # Wipe + recreate reference_crops/ so reference_map.json always
    # matches what's on disk.
    crops_dir = shot_root / "reference_crops"
    if crops_dir.exists():
        shutil.rmtree(crops_dir)
    crops_dir.mkdir(parents=True, exist_ok=True)

    rm = ReferenceMap(
        shotId=shot_id,
        sourceFramesDir=str(source_frames_dir),
        keyPosesDir=str(keyposes_dir),
        referenceCropsDir=str(crops_dir),
        lineArtMethod=lineart_method,
    )

    # Slice each referenced identity's sheet once for this shot.
    sliced_sheets: dict[str, list[dict[str, Any]]] = {}
    for identity, sheet_path in sheet_paths_by_identity.items():
        sliced_sheets[identity] = _slice_sheet_by_alpha_islands(
            Path(sheet_path), identity
        )

    # (identity, angle) -> (color path, lineart path) — dedup across key poses.
    crop_cache: dict[tuple[str, str], tuple[str, str]] = {}
    angle_histogram: dict[str, int] = {}
    detection_count = 0
    skipped_count = 0

    for kp_record in cm.get("keyPoses", []):
        kp_index = int(kp_record["keyPoseIndex"])
        kp_filename = str(kp_record["keyPoseFilename"])
        source_frame = int(kp_record["sourceFrame"])
        kp = KeyPoseReferences(
            keyPoseIndex=kp_index,
            keyPoseFilename=kp_filename,
            sourceFrame=source_frame,
        )

        kp_png_path = keyposes_dir / kp_filename
        if not kp_png_path.is_file():
            raise Node5ResultInputError(
                f"{shot_id}: key-pose PNG missing: {kp_png_path}."
            )

        for detection in kp_record.get("detections", []):
            identity = str(detection.get("identity", ""))
            expected_position = str(detection.get("expectedPosition", ""))
            bbox = detection.get("boundingBox")

            if not identity or not isinstance(bbox, list) or len(bbox) != 4:
                # Empty identity means Node 5 couldn't pair it with a
                # metadata character (reconcile leftover). Skip quietly
                # — Node 5 already logged the warning in character_map.
                kp.skipped.append({
                    "identity": identity or "<none>",
                    "reason": "unpaired-detection",
                    "message": (
                        "Node 5 emitted a detection without an identity "
                        "(reconcile leftover). Node 6 skips; Node 7 will "
                        "have nothing to condition for this slot."
                    ),
                })
                skipped_count += 1
                continue

            if identity not in sliced_sheets:
                raise QueueLookupError(
                    f"{shot_id} key-pose {kp_index}: detection identity "
                    f"{identity!r} has no sheetPath in queue.json. Likely "
                    "stale state — rerun Node 2 after editing metadata."
                )

            try:
                match = _match_one_detection(
                    shot_id=shot_id,
                    keyposes_png_path=kp_png_path,
                    bbox=tuple(int(v) for v in bbox),
                    identity=identity,
                    expected_position=expected_position,
                    sheet_crops=sliced_sheets[identity],
                    crops_dir=crops_dir,
                    lineart_method=lineart_method,
                    crop_cache=crop_cache,
                )
            except AngleMatchingError:
                raise
            except Exception as e:  # noqa: BLE001
                raise AngleMatchingError(
                    f"{shot_id} key-pose {kp_index} identity={identity!r}: "
                    f"{type(e).__name__}: {e}"
                ) from e

            kp.matches.append(match)
            detection_count += 1
            angle_histogram[match.selectedAngle] = (
                angle_histogram.get(match.selectedAngle, 0) + 1
            )

        rm.keyPoses.append(kp)

    map_path = shot_root / "reference_map.json"
    map_path.write_text(
        json.dumps(rm.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return ShotReferenceSummary(
        shotId=shot_id,
        keyPoseCount=len(rm.keyPoses),
        detectionCount=detection_count,
        skippedCount=skipped_count,
        referenceMapPath=str(map_path),
        angleHistogram=angle_histogram,
    )


# -------------------------------------------------------------------
# 6A: input loading + queue / characters cross-reference
# -------------------------------------------------------------------

def _load_node5_result(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise Node5ResultInputError(
            f"node5_result.json not found at {path}. Run Node 5 first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node5ResultInputError(
            f"node5_result.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node5ResultInputError(
            f"node5_result.json at {path} must be a JSON object; "
            f"got {type(raw).__name__}."
        )
    if raw.get("schemaVersion") != 1:
        raise Node5ResultInputError(
            f"node5_result.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 6 expects 1."
        )
    for key in ("workDir", "shots"):
        if key not in raw:
            raise Node5ResultInputError(
                f"node5_result.json at {path} missing required key '{key}'."
            )
    if not isinstance(raw["shots"], list):
        raise Node5ResultInputError(
            f"node5_result.json at {path}: 'shots' must be a list."
        )
    for s_idx, shot in enumerate(raw["shots"]):
        if not isinstance(shot, dict):
            raise Node5ResultInputError(
                f"node5_result.json: shots[{s_idx}] is not an object."
            )
        for key in ("shotId", "characterMapPath"):
            if key not in shot:
                raise Node5ResultInputError(
                    f"node5_result.json: shots[{s_idx}] missing '{key}'."
                )
    return raw


def _load_queue(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise QueueLookupError(
            f"queue.json not found at {path}. Node 6 needs it to look "
            "up each character's sheet PNG path."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise QueueLookupError(
            f"queue.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise QueueLookupError(
            f"queue.json at {path} must be a JSON object; "
            f"got {type(raw).__name__}."
        )
    if raw.get("schemaVersion") != 1:
        raise QueueLookupError(
            f"queue.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 6 expects 1."
        )
    if "batches" not in raw or not isinstance(raw["batches"], list):
        raise QueueLookupError(
            f"queue.json at {path}: missing or non-list 'batches'."
        )
    return raw


def _build_shot_sheet_lookup(
    queue: dict[str, Any], n5: dict[str, Any]
) -> dict[str, dict[str, Path]]:
    """Map every shotId in node5_result.json to `{identity: sheetPath}`."""
    by_id: dict[str, dict[str, Path]] = {}
    for batch in queue.get("batches", []):
        for shot in batch:
            shot_id = shot.get("shotId")
            if not shot_id:
                continue
            sheets: dict[str, Path] = {}
            for c in shot.get("characters", []):
                identity = c.get("identity")
                sheet_path = c.get("sheetPath")
                if not identity or not sheet_path:
                    raise QueueLookupError(
                        f"queue.json shot {shot_id!r}: character missing "
                        "identity or sheetPath."
                    )
                sheets[identity] = Path(sheet_path)
            by_id[shot_id] = sheets

    missing = [s["shotId"] for s in n5["shots"] if s["shotId"] not in by_id]
    if missing:
        raise QueueLookupError(
            "queue.json does not contain every shotId listed in "
            f"node5_result.json. Missing: {missing}. "
            "Likely stale state — rerun Node 2 with the updated metadata."
        )
    return by_id


def _check_angle_order_confirmed(characters_path: Path) -> None:
    if not characters_path.is_file():
        raise CharactersInputError(
            f"characters.json not found at {characters_path}. Node 6 "
            "reads it to confirm the canonical 8-angle order is locked."
        )
    try:
        raw = json.loads(characters_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CharactersInputError(
            f"characters.json at {characters_path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise CharactersInputError(
            f"characters.json at {characters_path} must be a JSON object; "
            f"got {type(raw).__name__}."
        )
    conv = raw.get("conventions")
    if not isinstance(conv, dict):
        raise CharactersInputError(
            f"characters.json at {characters_path} missing "
            "'conventions' object."
        )
    if not bool(conv.get("angleOrderConfirmed", False)):
        raise AngleOrderUnconfirmedError(
            "characters.json has "
            "conventions.angleOrderConfirmed=False. The canonical "
            "8-angle order (left->right) is: "
            + ", ".join(CANONICAL_ANGLES) + ". "
            "Confirm the reference sheet's layout matches this order, "
            "then flip the flag to True (or re-download the library "
            "from the Character Library page)."
        )


def _load_character_map(path: Path, shot_id: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node5ResultInputError(
            f"{shot_id}: character_map.json is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node5ResultInputError(
            f"{shot_id}: character_map.json must be a JSON object."
        )
    if raw.get("schemaVersion") != 1:
        raise Node5ResultInputError(
            f"{shot_id}: character_map.json schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 6 expects 1."
        )
    for key in ("keyPosesDir", "sourceFramesDir", "keyPoses"):
        if key not in raw:
            raise Node5ResultInputError(
                f"{shot_id}: character_map.json missing '{key}'."
            )
    if not isinstance(raw["keyPoses"], list):
        raise Node5ResultInputError(
            f"{shot_id}: character_map.json 'keyPoses' must be a list."
        )
    return raw


# -------------------------------------------------------------------
# 6B: alpha-island sheet slicing
# -------------------------------------------------------------------

def _slice_sheet_by_alpha_islands(
    sheet_path: Path, identity: str
) -> list[dict[str, Any]]:
    """Slice an 8-angle horizontal-strip sheet into 8 per-angle crops.

    Returns a list of 8 dicts in canonical-angle order:
        [{name, rgba, mask, norm_mask, norm_lum, bbox}, ...]
    """
    import numpy as np
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise ReferenceSheetFormatError(
            f"Pillow is required for sheet slicing: {e}."
        ) from e
    try:
        from scipy import ndimage  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise ReferenceSheetFormatError(
            f"scipy is required for alpha-island labelling: {e}."
        ) from e

    if not sheet_path.is_file():
        raise ReferenceSheetFormatError(
            f"Reference sheet for identity={identity!r} not found: "
            f"{sheet_path}."
        )
    try:
        img = Image.open(sheet_path)
    except Exception as e:  # noqa: BLE001
        raise ReferenceSheetFormatError(
            f"Could not open reference sheet {sheet_path}: {e}"
        ) from e

    if img.mode != "RGBA":
        raise ReferenceSheetFormatError(
            f"Reference sheet {sheet_path} for identity={identity!r} "
            f"has mode {img.mode!r}; Node 6 requires an RGBA PNG "
            "(transparent background). Re-export the sheet with a "
            "transparent background and rerun."
        )

    arr = np.asarray(img, dtype=np.uint8)
    alpha = arr[..., 3] > 0
    if not alpha.any():
        raise ReferenceSheetFormatError(
            f"Reference sheet {sheet_path} for identity={identity!r} "
            "has an alpha channel but it is entirely transparent."
        )

    structure = np.ones((3, 3), dtype=bool)
    labeled, num = ndimage.label(alpha, structure=structure)
    if num == 0:  # pragma: no cover — already caught by alpha.any()
        raise ReferenceSheetSliceError(
            f"Reference sheet {sheet_path} produced 0 alpha islands."
        )

    slices = ndimage.find_objects(labeled)
    bboxes: list[tuple[int, int, int, int]] = []
    for sl in slices:
        if sl is None:  # pragma: no cover
            continue
        y_sl, x_sl = sl
        bboxes.append(
            (int(x_sl.start), int(y_sl.start), int(x_sl.stop), int(y_sl.stop))
        )
    # Left-to-right by x-start.
    bboxes.sort(key=lambda b: b[0])

    if len(bboxes) != 8:
        raise ReferenceSheetSliceError(
            f"Reference sheet {sheet_path} for identity={identity!r} "
            f"produced {len(bboxes)} alpha island(s); expected exactly "
            "8 (the canonical 8-angle horizontal strip). Check for "
            "floating detail (stray eye/hand blobs not connected to "
            "the main body) — flatten those into the silhouette and "
            "re-export."
        )

    crops: list[dict[str, Any]] = []
    for i, (x0, y0, x1, y1) in enumerate(bboxes):
        rgba_crop = arr[y0:y1, x0:x1, :].copy()
        mask_crop = alpha[y0:y1, x0:x1].copy()
        norm_mask = _normalize_mask_to_canvas(mask_crop, NORM_CANVAS)
        norm_lum = _normalize_luminance_to_canvas(
            rgba_crop, mask_crop, NORM_CANVAS
        )
        crops.append({
            "name": CANONICAL_ANGLES[i],
            "rgba": rgba_crop,
            "mask": mask_crop,
            "norm_mask": norm_mask,
            "norm_lum": norm_lum,
            "bbox": (x0, y0, x1, y1),
        })
    return crops


# -------------------------------------------------------------------
# 6C: per-detection silhouette + multi-signal scoring
# -------------------------------------------------------------------

def _match_one_detection(
    shot_id: str,
    keyposes_png_path: Path,
    bbox: tuple[int, int, int, int],
    identity: str,
    expected_position: str,
    sheet_crops: list[dict[str, Any]],
    crops_dir: Path,
    lineart_method: str,
    crop_cache: dict[tuple[str, str], tuple[str, str]],
) -> ReferenceMatch:
    """Recompute the detection silhouette, score the 8 angles, pick
    the winner, and persist the cached color + line-art crops.
    """
    import numpy as np

    det_mask = _recompute_detection_silhouette(
        keyposes_png_path=keyposes_png_path,
        bbox=bbox,
        shot_id=shot_id,
        identity=identity,
    )
    det_lum = _load_detection_luminance(
        keyposes_png_path=keyposes_png_path,
        bbox=bbox,
        shot_id=shot_id,
        identity=identity,
    )

    norm_det_mask = _normalize_mask_to_canvas(det_mask, NORM_CANVAS)
    norm_det_lum = _normalize_luminance_to_canvas_from_gray(
        det_lum, det_mask, NORM_CANVAS
    )

    best_idx = 0
    best_final = -1.0
    best_breakdown: dict[str, float] = {}
    all_scores: dict[str, float] = {}

    det_symmetry = _self_symmetry(norm_det_mask)
    det_aspect = _mask_aspect(norm_det_mask)
    det_edge_density = _upper_edge_density(norm_det_lum, norm_det_mask)

    for i, ref in enumerate(sheet_crops):
        iou = _iou_score(norm_det_mask, ref["norm_mask"])
        ref_symmetry = _self_symmetry(ref["norm_mask"])
        symmetry = 1.0 - abs(det_symmetry - ref_symmetry)
        ref_aspect = _mask_aspect(ref["norm_mask"])
        aspect = _aspect_score(det_aspect, ref_aspect)
        ref_edge_density = _upper_edge_density(
            ref["norm_lum"], ref["norm_mask"]
        )
        edge = 1.0 - abs(det_edge_density - ref_edge_density)
        if edge < 0.0:  # pragma: no cover — densities are already in [0,1]
            edge = 0.0

        final = (
            SCORE_WEIGHTS["iou"] * iou
            + SCORE_WEIGHTS["symmetry"] * symmetry
            + SCORE_WEIGHTS["aspect"] * aspect
            + SCORE_WEIGHTS["edgeDensity"] * edge
        )
        all_scores[ref["name"]] = float(final)

        if final > best_final:
            best_final = float(final)
            best_idx = i
            best_breakdown = {
                "iou": float(iou),
                "symmetry": float(symmetry),
                "aspect": float(aspect),
                "edgeDensity": float(edge),
                "final": float(final),
            }

    selected = sheet_crops[best_idx]
    selected_angle = selected["name"]

    # Persist cached color + line-art crops per (identity, angle).
    color_path, lineart_path = _get_or_write_crop(
        crop_cache=crop_cache,
        crops_dir=crops_dir,
        identity=identity,
        angle=selected_angle,
        rgba=selected["rgba"],
        lineart_method=lineart_method,
    )

    return ReferenceMatch(
        identity=identity,
        expectedPosition=expected_position,
        boundingBox=list(bbox),
        selectedAngle=selected_angle,
        scoreBreakdown=best_breakdown,
        allScores=all_scores,
        referenceColorCropPath=color_path,
        referenceLineArtCropPath=lineart_path,
    )


def _recompute_detection_silhouette(
    keyposes_png_path: Path,
    bbox: tuple[int, int, int, int],
    shot_id: str,
    identity: str,
):
    """Otsu-binarize a bbox crop of the key-pose PNG and return the
    largest connected component as a boolean mask.
    """
    import numpy as np
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise AngleMatchingError(
            f"{shot_id}: Pillow is required for silhouette recompute: {e}."
        ) from e
    try:
        from scipy import ndimage  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise AngleMatchingError(
            f"{shot_id}: scipy is required for silhouette recompute: {e}."
        ) from e

    try:
        img = Image.open(keyposes_png_path).convert("L")
    except Exception as e:  # noqa: BLE001
        raise AngleMatchingError(
            f"{shot_id} identity={identity!r}: could not open key pose "
            f"{keyposes_png_path}: {e}"
        ) from e

    gray = np.asarray(img, dtype=np.uint8)
    h, w = gray.shape
    x, y, bw, bh = bbox
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(w, x + bw)
    y1 = min(h, y + bh)
    if x1 <= x0 or y1 <= y0:
        raise AngleMatchingError(
            f"{shot_id} identity={identity!r}: bbox {bbox} is outside "
            f"key-pose frame ({w}x{h})."
        )
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0:  # pragma: no cover
        raise AngleMatchingError(
            f"{shot_id} identity={identity!r}: empty key-pose crop."
        )

    threshold = _otsu_threshold(crop)
    binary = crop <= threshold
    if not binary.any():
        raise AngleMatchingError(
            f"{shot_id} identity={identity!r}: Otsu found no ink pixels "
            f"in bbox {bbox}. Likely a phantom detection from Node 5 "
            "reconcile — operator should review character_map.json."
        )
    structure = np.ones((3, 3), dtype=bool)
    labeled, num = ndimage.label(binary, structure=structure)
    if num == 0:  # pragma: no cover
        raise AngleMatchingError(
            f"{shot_id} identity={identity!r}: no connected components "
            f"in bbox {bbox}."
        )
    sizes = ndimage.sum(binary, labeled, index=list(range(1, num + 1)))
    largest = int(np.argmax(sizes)) + 1
    return labeled == largest


def _load_detection_luminance(
    keyposes_png_path: Path,
    bbox: tuple[int, int, int, int],
    shot_id: str,
    identity: str,
):
    """Return the grayscale crop of the key-pose at the bbox (no threshold)."""
    import numpy as np
    from PIL import Image  # type: ignore[import-not-found]

    try:
        img = Image.open(keyposes_png_path).convert("L")
    except Exception as e:  # noqa: BLE001 — already checked in silhouette path
        raise AngleMatchingError(
            f"{shot_id} identity={identity!r}: could not open key pose "
            f"{keyposes_png_path}: {e}"
        ) from e

    gray = np.asarray(img, dtype=np.uint8)
    h, w = gray.shape
    x, y, bw, bh = bbox
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(w, x + bw)
    y1 = min(h, y + bh)
    return gray[y0:y1, x0:x1].astype(np.float32)


def _otsu_threshold(gray) -> int:
    """Otsu's method — pick the 0-255 cut-off that maximizes
    between-class variance on the grayscale histogram.
    """
    import numpy as np

    hist, _ = np.histogram(gray.ravel(), bins=256, range=(0, 256))
    total_count = float(gray.size)
    if total_count == 0:  # pragma: no cover
        return 127
    cumsum = hist.cumsum().astype(np.float64)
    levels = np.arange(256, dtype=np.float64)
    cumsum_x = (hist * levels).cumsum()
    total_sum = cumsum_x[-1]

    best_t = 127
    best_var = -1.0
    for t in range(256):
        w0 = cumsum[t]
        w1 = total_count - w0
        if w0 == 0 or w1 == 0:
            continue
        mu0 = cumsum_x[t] / w0
        mu1 = (total_sum - cumsum_x[t]) / w1
        var_b = w0 * w1 * (mu0 - mu1) ** 2
        if var_b > best_var:
            best_var = var_b
            best_t = t
    return best_t


# -------------------------------------------------------------------
# Normalization + scoring helpers (classical multi-signal)
# -------------------------------------------------------------------

def _normalize_mask_to_canvas(mask, size: int):
    """Aspect-preserving resize of a boolean mask's bbox to fit inside
    a (size x size) canvas, centered. Nearest-neighbour to preserve
    binarity.
    """
    import numpy as np
    from PIL import Image  # type: ignore[import-not-found]

    if not mask.any():
        return np.zeros((size, size), dtype=bool)
    rows, cols = np.where(mask)
    y0, y1 = int(rows.min()), int(rows.max()) + 1
    x0, x1 = int(cols.min()), int(cols.max()) + 1
    crop = mask[y0:y1, x0:x1]
    h, w = crop.shape
    scale = float(size) / float(max(h, w))
    new_h = max(1, int(round(h * scale)))
    new_w = max(1, int(round(w * scale)))
    pil = Image.fromarray((crop.astype(np.uint8) * 255), mode="L")
    pil = pil.resize((new_w, new_h), Image.NEAREST)
    resized = np.asarray(pil, dtype=np.uint8) > 127
    canvas = np.zeros((size, size), dtype=bool)
    y_off = (size - new_h) // 2
    x_off = (size - new_w) // 2
    canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
    return canvas


def _normalize_luminance_to_canvas(rgba, mask, size: int):
    """Aspect-preserving resize of an RGBA crop's luminance channel,
    zeroed outside the alpha mask, into a (size x size) float32 canvas.
    """
    import numpy as np

    r = rgba[..., 0].astype(np.float32)
    g = rgba[..., 1].astype(np.float32)
    b = rgba[..., 2].astype(np.float32)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    lum = lum * mask.astype(np.float32)
    return _normalize_luminance_to_canvas_from_gray(lum, mask, size)


def _normalize_luminance_to_canvas_from_gray(lum, mask, size: int):
    """Aspect-preserving resize of a grayscale crop to a (size x size)
    float32 canvas, using the mask's bbox to frame the resize and pad.
    """
    import numpy as np
    from PIL import Image  # type: ignore[import-not-found]

    if not mask.any():
        return np.zeros((size, size), dtype=np.float32)
    rows, cols = np.where(mask)
    y0, y1 = int(rows.min()), int(rows.max()) + 1
    x0, x1 = int(cols.min()), int(cols.max()) + 1
    crop = lum[y0:y1, x0:x1]
    h, w = crop.shape
    scale = float(size) / float(max(h, w))
    new_h = max(1, int(round(h * scale)))
    new_w = max(1, int(round(w * scale)))
    pil = Image.fromarray(
        np.clip(crop, 0, 255).astype(np.uint8), mode="L"
    )
    pil = pil.resize((new_w, new_h), Image.BILINEAR)
    resized = np.asarray(pil, dtype=np.float32)
    canvas = np.zeros((size, size), dtype=np.float32)
    y_off = (size - new_h) // 2
    x_off = (size - new_w) // 2
    canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
    return canvas


def _iou_score(a, b) -> float:
    inter = int((a & b).sum())
    union = int((a | b).sum())
    if union == 0:
        return 0.0
    return inter / union


def _self_symmetry(mask) -> float:
    """IoU of a mask with its horizontal flip. 1.0 = perfectly symmetric."""
    import numpy as np

    if not mask.any():
        return 0.0
    flipped = np.fliplr(mask)
    inter = int((mask & flipped).sum())
    union = int((mask | flipped).sum())
    if union == 0:  # pragma: no cover
        return 0.0
    return inter / union


def _mask_aspect(mask) -> float:
    """Width/height of the mask's bbox. Returns 0.0 on an empty mask."""
    import numpy as np

    if not mask.any():
        return 0.0
    rows, cols = np.where(mask)
    h = int(rows.max() - rows.min() + 1)
    w = int(cols.max() - cols.min() + 1)
    if h == 0:  # pragma: no cover
        return 0.0
    return float(w) / float(h)


def _aspect_score(det_aspect: float, ref_aspect: float) -> float:
    """Normalized aspect-ratio match score in [0, 1].

    1.0 when the two bbox aspect ratios are identical; falls off linearly
    with their relative difference (divided by the larger ratio).
    """
    if det_aspect <= 0.0 or ref_aspect <= 0.0:
        return 0.0
    big = max(det_aspect, ref_aspect)
    small = min(det_aspect, ref_aspect)
    return float(small / big)


def _upper_edge_density(lum, mask) -> float:
    """Density of luminance-gradient edges inside the silhouette's
    upper 40% (rows 0..0.4*H of the mask's bbox).

    Front views of a character typically have face features (eyes,
    mouth) producing interior edges; back views are smoother. This is
    the classical front/back tie-breaker.

    Returns a clamped value in [0, 1]: `edges_in_region / pixels_in_region`.
    """
    import numpy as np
    try:
        from scipy import ndimage  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        return 0.0

    if not mask.any():
        return 0.0

    rows = np.where(mask.any(axis=1))[0]
    if rows.size == 0:  # pragma: no cover
        return 0.0
    y0 = int(rows.min())
    y1 = int(rows.max()) + 1
    upper_cut = y0 + int(round(0.4 * (y1 - y0)))
    if upper_cut <= y0:  # pragma: no cover
        upper_cut = y0 + 1

    region_mask = np.zeros_like(mask, dtype=bool)
    region_mask[y0:upper_cut, :] = mask[y0:upper_cut, :]
    area = int(region_mask.sum())
    if area == 0:
        return 0.0

    # Sobel-magnitude interior edges. Normalize to [0, 1] by max.
    sx = ndimage.sobel(lum, axis=1)
    sy = ndimage.sobel(lum, axis=0)
    mag = np.sqrt(sx * sx + sy * sy)
    m_max = float(mag.max()) if mag.size else 0.0
    if m_max <= 0.0:
        return 0.0
    mag_norm = mag / m_max

    # An interior edge is a gradient pixel INSIDE the mask. Threshold
    # mildly so we don't just count every anti-aliased fringe.
    edges = (mag_norm > 0.15) & region_mask
    return float(int(edges.sum())) / float(area)


# -------------------------------------------------------------------
# 6D: crop caching + DoG line-art generation
# -------------------------------------------------------------------

def _get_or_write_crop(
    crop_cache: dict[tuple[str, str], tuple[str, str]],
    crops_dir: Path,
    identity: str,
    angle: str,
    rgba,
    lineart_method: str,
) -> tuple[str, str]:
    """Return the (color_path, lineart_path) for an (identity, angle).

    Writes the PNGs on first request and caches the paths so subsequent
    key poses picking the same (identity, angle) share one file.
    """
    key = (identity, angle)
    if key in crop_cache:
        return crop_cache[key]

    color_name = f"{_safe_segment(identity)}_{_safe_segment(angle)}.png"
    lineart_name = (
        f"{_safe_segment(identity)}_{_safe_segment(angle)}_lineart.png"
    )
    color_path = crops_dir / color_name
    lineart_path = crops_dir / lineart_name

    from PIL import Image  # type: ignore[import-not-found]

    Image.fromarray(rgba, mode="RGBA").save(color_path)
    lineart_rgba = _lineart_from_rgba(rgba, method=lineart_method)
    Image.fromarray(lineart_rgba, mode="RGBA").save(lineart_path)

    crop_cache[key] = (str(color_path), str(lineart_path))
    return crop_cache[key]


def _safe_segment(s: str) -> str:
    """Make a string safe to use as a filename segment."""
    return "".join(
        ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in s
    )


def _lineart_from_rgba(rgba, method: str):
    """Return an RGBA uint8 line-art image (black lines on transparent).

    Supported methods:
      * `dog`        — Difference of Gaussians on luminance, OR'd with
                       the alpha-channel boundary. Default.
      * `canny`      — Sobel-magnitude threshold (a simple classical
                       stand-in for true Canny; no ML dep required).
      * `threshold`  — Pure luminance threshold (darkest pixels).
    """
    import numpy as np

    if method == "dog":
        line_mask = _dog_lineart_mask(rgba)
    elif method == "canny":
        line_mask = _sobel_lineart_mask(rgba)
    elif method == "threshold":
        line_mask = _threshold_lineart_mask(rgba)
    else:  # pragma: no cover — validated at CLI entry
        raise CharactersInputError(
            f"Unsupported lineart_method={method!r}."
        )

    out = np.zeros((rgba.shape[0], rgba.shape[1], 4), dtype=np.uint8)
    out[line_mask, 3] = 255  # alpha on for line pixels
    # rgb already 0 (black) by np.zeros initialization
    return out


def _dog_lineart_mask(rgba):
    """DoG line mask: G(Y, sigma1) - G(Y, sigma2), thresholded, OR
    boundary of alpha mask. Inside-silhouette only.
    """
    import numpy as np
    try:
        from scipy import ndimage  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise CharactersInputError(
            f"scipy is required for DoG line-art: {e}."
        ) from e

    mask = rgba[..., 3] > 0
    r = rgba[..., 0].astype(np.float32)
    g = rgba[..., 1].astype(np.float32)
    b = rgba[..., 2].astype(np.float32)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    lum = lum * mask.astype(np.float32)

    blur1 = ndimage.gaussian_filter(lum, sigma=1.0)
    blur2 = ndimage.gaussian_filter(lum, sigma=2.0)
    dog = blur1 - blur2
    dog_binary = np.abs(dog) > 2.0

    inner = ndimage.binary_erosion(
        mask, structure=np.ones((3, 3), dtype=bool), iterations=1
    )
    boundary = mask & ~inner

    return (dog_binary | boundary) & mask


def _sobel_lineart_mask(rgba):
    """Classical Sobel-magnitude threshold (stand-in for Canny).
    Inside-silhouette only, OR'd with the alpha boundary.
    """
    import numpy as np
    from scipy import ndimage  # type: ignore[import-not-found]

    mask = rgba[..., 3] > 0
    r = rgba[..., 0].astype(np.float32)
    g = rgba[..., 1].astype(np.float32)
    b = rgba[..., 2].astype(np.float32)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    lum = lum * mask.astype(np.float32)
    sx = ndimage.sobel(lum, axis=1)
    sy = ndimage.sobel(lum, axis=0)
    mag = np.sqrt(sx * sx + sy * sy)
    m_max = float(mag.max()) if mag.size else 0.0
    if m_max > 0.0:
        mag_binary = (mag / m_max) > 0.15
    else:
        mag_binary = np.zeros_like(mask, dtype=bool)

    inner = ndimage.binary_erosion(
        mask, structure=np.ones((3, 3), dtype=bool), iterations=1
    )
    boundary = mask & ~inner
    return (mag_binary | boundary) & mask


def _threshold_lineart_mask(rgba):
    """Luminance threshold: the darkest pixels inside the silhouette."""
    import numpy as np

    mask = rgba[..., 3] > 0
    if not mask.any():
        return np.zeros(mask.shape, dtype=bool)
    r = rgba[..., 0].astype(np.float32)
    g = rgba[..., 1].astype(np.float32)
    b = rgba[..., 2].astype(np.float32)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    inside = lum[mask]
    # Keep the darkest 30% as "line" pixels.
    cutoff = float(np.quantile(inside, 0.30))
    dark = (lum <= cutoff) & mask

    try:
        from scipy import ndimage  # type: ignore[import-not-found]
        inner = ndimage.binary_erosion(
            mask, structure=np.ones((3, 3), dtype=bool), iterations=1
        )
        boundary = mask & ~inner
        return (dark | boundary) & mask
    except ImportError:  # pragma: no cover
        return dark


__all__ = [
    # Canonical constants
    "CANONICAL_ANGLES",
    "LINEART_METHODS",
    "DEFAULT_LINEART_METHOD",
    "NORM_CANVAS",
    "SCORE_WEIGHTS",
    # Result types
    "ReferenceMatch",
    "KeyPoseReferences",
    "ReferenceMap",
    "ShotReferenceSummary",
    "Node6Result",
    # Public entry points
    "match_references_for_queue",
    "match_references_for_shot",
]
