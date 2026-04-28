"""Node 5 — Character Detection & In-Frame Position Analysis.

Reads `node4_result.json` (Node 4) and `queue.json` (Node 2), runs a
classical connected-component analysis on each shot's key-pose PNGs,
bins each detected silhouette into a position zone, and assigns an
identity from metadata.

**No ML, no GPU.** Chota Bhim animatics are line-drawings where the
animator deliberately separates characters — a connected-components
pass plus small cleanup rules handles the 95% case. When the blob
count disagrees with metadata, a reconcile pass tries to fix it
(merge floating details if too many; erode to separate touching
characters if too few) before emitting a warning.

Sub-steps (aligned with `docs/PLAN.md` Node 5):

  5A. Load + validate `node4_result.json` and `queue.json`. Build a
      `shotId -> [(identity, position)]` lookup from the queue.
  5B. Per shot, per key pose: binarize (Otsu), find connected
      components (scipy.ndimage.label with 8-connectivity), drop tiny
      fragments, merge overlapping bounding boxes.
  5C. Reconcile count against metadata's `len(characters)`:
      too many → drop smallest-area blobs; too few → re-detect with
      progressive binary erosion to pull touching characters apart.
      Emit a structured warning record for every reconcile action
      (even successful ones) so the operator can review what Node 5
      auto-fixed.
  5D. Position binning: normalize each bbox's centre-x to [0.0, 1.0]
      and bucket by the locked 25/20/10/20/25 thresholds
      (L / CL / C / CR / R).
  5E. Identity assignment (Strategy A — positional): sort detections
      left-to-right by centre-x, sort metadata characters by position
      rank (L<CL<C<CR<R), zip. Tiebreaker for same position code:
      metadata list order. Write per-shot `character_map.json` and
      aggregate `node5_result.json`.

Design decisions (locked 2026-04-23):

  * **Classical connected-components**, not ML. scipy.ndimage.label
    + numpy. Binarization is Otsu's method (adaptive); line-art MP4
    encoder halos are absorbed into the foreground, which actually
    helps by producing more solid blobs.
  * **8-connectivity** for CC — diagonally-touching pixels count as
    connected. Line-art character outlines rarely have single-pixel
    diagonal gaps.
  * **Tiny-blob filter**: drop any blob whose bounding-box area is
    below `min_area_ratio` of the frame area (default 0.1% = 0.001).
    A Chota Bhim character silhouette is much bigger; anything smaller
    is compression speckle or stray ink.
  * **Overlap merge**: two bounding boxes whose IoU >= `merge_iou`
    (default 0.5) are merged. This reunites floating details (a
    character's eye drawn as a separate disconnected blob) with their
    parent character.
  * **Warn AND reconcile** on count mismatch. Every reconcile action
    logs a `DetectionWarning` in `node5_result.json`. Node 5 never
    throws on a count mismatch.
  * **Position binning is 25/20/10/20/25** of normalized frame width.
    "C" is a narrow 10% dead-centre band; L and R are 25% each.
  * **Identity = Strategy A (positional)** for v1. Sort detections
    and metadata left-to-right, zip. No ML similarity check.
  * **Single-threaded.** Same rationale as Node 4 — shots are
    independent; parallelism is Node 11's concern.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import (
    CharacterDetectionError,
    Node4ResultInputError,
    QueueLookupError,
)


# -------------------------------------------------------------------
# Tuning defaults
# -------------------------------------------------------------------

DEFAULT_MIN_AREA_RATIO = 0.001
"""Blobs smaller than this fraction of frame area are dropped as
noise. 0.1% of a 1280x720 frame is ~920 px² — well below any real
character silhouette, well above most encoder speckle.
"""

DEFAULT_MERGE_IOU = 0.5
"""Two bounding boxes whose IoU >= this are merged into one. 0.5 is
strict enough that two characters standing side-by-side don't merge,
but loose enough to reunite a character's floating details (separate
eye/hand blobs) with the parent silhouette.
"""

DEFAULT_MAX_ERODE_ITERATIONS = 3
"""When blob count is less than metadata expects, Node 5 re-runs
detection after `binary_erosion(k=3)` up to this many times, trying
to split touching characters. Beyond 3 iterations we're eroding away
legitimate character features — emit a warning instead.
"""

DEFAULT_DARK_THRESHOLD = 80
"""Phase 2f (2026-04-28) — luminance threshold separating dark
character outlines from lighter BG furniture lines. Pixels with
grayscale luminance < this value are kept as character ink; pixels
>= this value are erased to white BG. The user's storyboard
convention puts character outlines at luminance ~0-50 (dark bold
black) and BG furniture at ~80-180 (light grey), so 80 sits at the
boundary — anything definitely-dark passes, anything definitely-light
fails. Operator can tune via `--dark-threshold N` if a project's ink
darkness drifts.
"""

DEFAULT_OUTLINE_CLOSING_KERNEL = 3
"""Phase 2f morphological closing kernel size. A 3×3 closing
(`binary_closing` = dilate then erode by 1 pixel) seals 1-2 pixel
gaps in the character outline that result from BG-line crossings
(when the artist drew BG on top of character at the intersection
point), without merging genuinely separate characters."""

# (L | CL | C | CR | R) — the locked 25/20/10/20/25 split of frame width.
# A silhouette's normalized centre-x falls into exactly one bin.
POSITION_THRESHOLDS: tuple[float, ...] = (0.25, 0.45, 0.55, 0.75)
POSITION_CODES: tuple[str, ...] = ("L", "CL", "C", "CR", "R")
POSITION_RANK: dict[str, int] = {code: i for i, code in enumerate(POSITION_CODES)}

_FRAME_NAME_RE = re.compile(r"^frame_(\d{4,})\.png$")


# -------------------------------------------------------------------
# Public result types
# -------------------------------------------------------------------

@dataclass
class Detection:
    """One detected silhouette on a key pose.

    `boundingBox` is `[x, y, w, h]` in full-resolution pixels.
    `centerX` is normalized to `[0.0, 1.0]` across frame width.
    `positionCode` is the *detected* position (L/CL/C/CR/R).
    `identity` + `expectedPosition` come from metadata via the
    Strategy-A zip in step 5E.
    """
    identity: str
    expectedPosition: str
    boundingBox: list[int]
    centerX: float
    positionCode: str
    area: int


@dataclass
class DetectionWarning:
    """One structured note attached to a key pose's detection record.

    Emitted for every reconcile action (merges, drops, erosions) so
    the operator can see exactly what Node 5 auto-fixed vs. what still
    needs human review. `kind` is one of:
      * "count-mismatch-over"   (too many blobs; after reconcile)
      * "count-mismatch-under"  (too few blobs; after reconcile)
      * "reconcile-merged"      (bboxes merged by overlap)
      * "reconcile-dropped"     (small blobs dropped to hit target)
      * "reconcile-eroded"      (erosion iteration pulled blobs apart)
      * "reconcile-failed"      (still wrong count after max attempts)
    """
    kind: str
    message: str


@dataclass
class KeyPoseDetections:
    """All detections + warnings for a single key pose."""
    keyPoseIndex: int
    keyPoseFilename: str
    sourceFrame: int
    frameWidth: int
    frameHeight: int
    detections: list[Detection] = field(default_factory=list)
    warnings: list[DetectionWarning] = field(default_factory=list)


@dataclass
class CharacterMap:
    """Per-shot character detection map.

    Written to `<shotId>/character_map.json` alongside the keyposes/
    folder. Node 6 reads this to route each silhouette to the right
    reference-sheet matcher.
    """
    schemaVersion: int = 1
    shotId: str = ""
    expectedCharacterCount: int = 0
    expectedCharacters: list[dict[str, str]] = field(default_factory=list)
    sourceFramesDir: str = ""
    keyPosesDir: str = ""
    minAreaRatio: float = DEFAULT_MIN_AREA_RATIO
    mergeIou: float = DEFAULT_MERGE_IOU
    # Phase 2f (2026-04-28): luminance threshold + BG-stripped dark_lines/
    # dir. Additive — old character_map.json files load unchanged
    # because dataclass defaults fill in (`darkThreshold` = 80,
    # `darkLinesDir` = "" meaning "this run pre-dates Phase 2f").
    darkThreshold: int = DEFAULT_DARK_THRESHOLD
    darkLinesDir: str = ""
    keyPoses: list[KeyPoseDetections] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "shotId": self.shotId,
            "expectedCharacterCount": self.expectedCharacterCount,
            "expectedCharacters": self.expectedCharacters,
            "sourceFramesDir": self.sourceFramesDir,
            "keyPosesDir": self.keyPosesDir,
            "minAreaRatio": self.minAreaRatio,
            "mergeIou": self.mergeIou,
            "darkThreshold": self.darkThreshold,
            "darkLinesDir": self.darkLinesDir,
            "keyPoses": [
                {
                    "keyPoseIndex": kp.keyPoseIndex,
                    "keyPoseFilename": kp.keyPoseFilename,
                    "sourceFrame": kp.sourceFrame,
                    "frameWidth": kp.frameWidth,
                    "frameHeight": kp.frameHeight,
                    "detections": [asdict(d) for d in kp.detections],
                    "warnings": [asdict(w) for w in kp.warnings],
                }
                for kp in self.keyPoses
            ],
        }


@dataclass
class ShotDetectionSummary:
    """One-line summary of a shot's Node 5 output."""
    shotId: str
    expectedCharacterCount: int
    keyPoseCount: int
    totalDetections: int
    warningCount: int
    characterMapPath: str


@dataclass
class Node5Result:
    """Aggregate Node 5 result. Written to `<work-dir>/node5_result.json`."""
    schemaVersion: int = 1
    projectName: str = ""
    workDir: str = ""
    minAreaRatio: float = DEFAULT_MIN_AREA_RATIO
    mergeIou: float = DEFAULT_MERGE_IOU
    # Phase 2f (2026-04-28): luminance threshold for the dark-line
    # extraction step. Additive — old aggregate manifests load through
    # this dataclass with the default value filled in.
    darkThreshold: int = DEFAULT_DARK_THRESHOLD
    detectedAt: str = ""
    shots: list[ShotDetectionSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "projectName": self.projectName,
            "workDir": self.workDir,
            "minAreaRatio": self.minAreaRatio,
            "mergeIou": self.mergeIou,
            "darkThreshold": self.darkThreshold,
            "detectedAt": self.detectedAt,
            "shots": [asdict(s) for s in self.shots],
        }


# -------------------------------------------------------------------
# Public entry points
# -------------------------------------------------------------------

def detect_characters_for_queue(
    node4_result_path: Path | str,
    queue_path: Path | str,
    min_area_ratio: float = DEFAULT_MIN_AREA_RATIO,
    merge_iou: float = DEFAULT_MERGE_IOU,
    dark_threshold: int = DEFAULT_DARK_THRESHOLD,
) -> Node5Result:
    """Detect characters on every key pose in every shot.

    Args:
        node4_result_path: Path to `node4_result.json` (Node 4's output).
            Node 5 writes its aggregate manifest alongside it in the
            same work directory.
        queue_path: Path to `queue.json` (Node 2's output). Needed to
            look up each shot's expected character identities + positions.
        min_area_ratio: Blobs smaller than this fraction of frame area
            are dropped as noise.
        merge_iou: Two bounding boxes whose IoU exceeds this are merged
            into one (reunites floating details with parent silhouettes).
        dark_threshold: Phase 2f (2026-04-28). Pixels with grayscale
            luminance < this value are kept as character ink; pixels
            >= this are erased to white BG. Default 80 separates dark
            character outlines from lighter BG furniture lines on the
            user's storyboard convention.

    Returns:
        Node5Result — success if no exception raised. Reconcile actions
        appear as per-key-pose warnings in the per-shot `character_map.json`
        (not exceptions).

    Raises:
        Node4ResultInputError: manifest missing, malformed, or references
            a keyposes folder that no longer exists.
        QueueLookupError: queue.json missing, unreadable, or does not
            contain a shotId that appears in node4_result.json.
        CharacterDetectionError: a PNG failed to load/decode.
    """
    n4_path = Path(node4_result_path).resolve()
    q_path = Path(queue_path).resolve()

    n4 = _load_node4_result(n4_path)
    queue = _load_queue(q_path)
    shot_chars = _build_shot_character_lookup(queue, n4)

    work_dir = Path(n4["workDir"]).resolve()

    result = Node5Result(
        projectName=n4.get("projectName", ""),
        workDir=str(work_dir),
        minAreaRatio=min_area_ratio,
        mergeIou=merge_iou,
        darkThreshold=dark_threshold,
        detectedAt=datetime.now(timezone.utc).isoformat(),
    )

    for shot in n4["shots"]:
        shot_id = shot["shotId"]
        expected = shot_chars[shot_id]

        summary = detect_characters_for_shot(
            shot_id=shot_id,
            keyposes_dir=Path(shot["keyPosesDir"]),
            key_pose_map_path=Path(shot["keyPoseMapPath"]),
            source_frames_dir=Path(shot["sourceFramesDir"]),
            expected_characters=expected,
            min_area_ratio=min_area_ratio,
            merge_iou=merge_iou,
            dark_threshold=dark_threshold,
        )
        result.shots.append(summary)

    (work_dir / "node5_result.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


def detect_characters_for_shot(
    shot_id: str,
    keyposes_dir: Path | str,
    key_pose_map_path: Path | str,
    source_frames_dir: Path | str,
    expected_characters: list[dict[str, str]],
    min_area_ratio: float = DEFAULT_MIN_AREA_RATIO,
    merge_iou: float = DEFAULT_MERGE_IOU,
    dark_threshold: int = DEFAULT_DARK_THRESHOLD,
) -> ShotDetectionSummary:
    """Detect characters on every key pose of one shot.

    Writes `<source_frames_dir>/character_map.json` and returns a
    summary entry for `node5_result.json`. Phase 2f (2026-04-28) also
    writes a BG-stripped `<source_frames_dir>/dark_lines/<filename>.png`
    per key pose for Node 7 to consume — character outlines on a clean
    white BG with BG furniture lines erased.
    """
    keyposes_dir = Path(keyposes_dir).resolve()
    source_frames_dir = Path(source_frames_dir).resolve()
    key_pose_map_path = Path(key_pose_map_path).resolve()

    if not keyposes_dir.is_dir():
        raise Node4ResultInputError(
            f"{shot_id}: keyposes folder does not exist: {keyposes_dir}. "
            "Did Node 4 complete?"
        )
    if not key_pose_map_path.is_file():
        raise Node4ResultInputError(
            f"{shot_id}: keypose_map.json not found at {key_pose_map_path}. "
            "Did Node 4 complete?"
        )

    key_pose_entries = _load_key_pose_entries(key_pose_map_path, shot_id)

    # Phase 2f: dark_lines/ as a sibling of keyposes/. Wipe stale PNGs
    # before each run so the dir matches the current keypose set
    # exactly (same wipe-before-write pattern as Nodes 3/4/5/6/8/9).
    dark_lines_dir = source_frames_dir / "dark_lines"
    _wipe_dark_lines_dir(dark_lines_dir)

    cm = CharacterMap(
        shotId=shot_id,
        expectedCharacterCount=len(expected_characters),
        expectedCharacters=list(expected_characters),
        sourceFramesDir=str(source_frames_dir),
        keyPosesDir=str(keyposes_dir),
        minAreaRatio=min_area_ratio,
        mergeIou=merge_iou,
        darkThreshold=dark_threshold,
        darkLinesDir=str(dark_lines_dir),
    )

    for kp in key_pose_entries:
        kp_detections = _detect_on_key_pose(
            shot_id=shot_id,
            keyposes_dir=keyposes_dir,
            key_pose_index=kp["keyPoseIndex"],
            key_pose_filename=kp["keyPoseFilename"],
            source_frame=kp["sourceFrame"],
            expected_characters=expected_characters,
            min_area_ratio=min_area_ratio,
            merge_iou=merge_iou,
            dark_threshold=dark_threshold,
            dark_lines_dir=dark_lines_dir,
        )
        cm.keyPoses.append(kp_detections)

    map_path = source_frames_dir / "character_map.json"
    map_path.write_text(
        json.dumps(cm.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    total_detections = sum(len(kp.detections) for kp in cm.keyPoses)
    total_warnings = sum(len(kp.warnings) for kp in cm.keyPoses)

    return ShotDetectionSummary(
        shotId=shot_id,
        expectedCharacterCount=len(expected_characters),
        keyPoseCount=len(cm.keyPoses),
        totalDetections=total_detections,
        warningCount=total_warnings,
        characterMapPath=str(map_path),
    )


# -------------------------------------------------------------------
# 5A: input loading + queue cross-reference
# -------------------------------------------------------------------

def _load_node4_result(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise Node4ResultInputError(
            f"node4_result.json not found at {path}. Run Node 4 first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node4ResultInputError(
            f"node4_result.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node4ResultInputError(
            f"node4_result.json at {path} must be a JSON object; "
            f"got {type(raw).__name__}."
        )
    if raw.get("schemaVersion") != 1:
        raise Node4ResultInputError(
            f"node4_result.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 5 expects 1."
        )
    for key in ("workDir", "shots"):
        if key not in raw:
            raise Node4ResultInputError(
                f"node4_result.json at {path} missing required key '{key}'."
            )
    if not isinstance(raw["shots"], list):
        raise Node4ResultInputError(
            f"node4_result.json at {path}: 'shots' must be a list."
        )
    for s_idx, shot in enumerate(raw["shots"]):
        if not isinstance(shot, dict):
            raise Node4ResultInputError(
                f"node4_result.json: shots[{s_idx}] is not an object."
            )
        for key in ("shotId", "keyPosesDir", "keyPoseMapPath", "sourceFramesDir"):
            if key not in shot:
                raise Node4ResultInputError(
                    f"node4_result.json: shots[{s_idx}] missing '{key}'."
                )
    return raw


def _load_queue(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise QueueLookupError(
            f"queue.json not found at {path}. Node 5 needs it to look up "
            "each shot's expected characters + positions."
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
            f"{raw.get('schemaVersion')!r}; Node 5 expects 1."
        )
    if "batches" not in raw or not isinstance(raw["batches"], list):
        raise QueueLookupError(
            f"queue.json at {path}: missing or non-list 'batches'."
        )
    return raw


def _build_shot_character_lookup(
    queue: dict[str, Any], n4: dict[str, Any]
) -> dict[str, list[dict[str, str]]]:
    """Map every shotId in node4_result.json to its metadata character list.

    Raises QueueLookupError if any n4 shotId is missing from queue.json.
    """
    by_id: dict[str, list[dict[str, str]]] = {}
    for batch in queue.get("batches", []):
        for shot in batch:
            chars = []
            for c in shot.get("characters", []):
                if c.get("position") not in POSITION_RANK:
                    raise QueueLookupError(
                        f"queue.json: shot {shot.get('shotId', '?')} "
                        f"character has unknown positionCode "
                        f"{c.get('position')!r}."
                    )
                chars.append({
                    "identity": c["identity"],
                    "position": c["position"],
                })
            by_id[shot["shotId"]] = chars

    missing = [s["shotId"] for s in n4["shots"] if s["shotId"] not in by_id]
    if missing:
        raise QueueLookupError(
            "queue.json does not contain every shotId listed in "
            f"node4_result.json. Missing: {missing}. "
            "Likely stale state — rerun Node 2 with the updated metadata."
        )
    return by_id


def _load_key_pose_entries(
    key_pose_map_path: Path, shot_id: str
) -> list[dict[str, Any]]:
    """Read `keypose_map.json` and return the ordered key-pose list."""
    try:
        raw = json.loads(key_pose_map_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node4ResultInputError(
            f"{shot_id}: keypose_map.json is not valid JSON: {e}"
        ) from e
    if raw.get("schemaVersion") != 1:
        raise Node4ResultInputError(
            f"{shot_id}: keypose_map.json schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 5 expects 1."
        )
    keyposes = raw.get("keyPoses")
    if not isinstance(keyposes, list):
        raise Node4ResultInputError(
            f"{shot_id}: keypose_map.json missing or non-list 'keyPoses'."
        )
    for k_idx, kp in enumerate(keyposes):
        for key in ("keyPoseIndex", "sourceFrame", "keyPoseFilename"):
            if key not in kp:
                raise Node4ResultInputError(
                    f"{shot_id}: keypose_map.json keyPoses[{k_idx}] "
                    f"missing '{key}'."
                )
    return keyposes


# -------------------------------------------------------------------
# 5B–5E: per-key-pose detection
# -------------------------------------------------------------------

def _detect_on_key_pose(
    shot_id: str,
    keyposes_dir: Path,
    key_pose_index: int,
    key_pose_filename: str,
    source_frame: int,
    expected_characters: list[dict[str, str]],
    min_area_ratio: float,
    merge_iou: float,
    dark_threshold: int = DEFAULT_DARK_THRESHOLD,
    dark_lines_dir: Path | None = None,
) -> KeyPoseDetections:
    """Run 5B → 5E on a single key-pose PNG.

    Phase 2f (2026-04-28) replaced Otsu binarization with a fixed
    luminance threshold + morphological closing. The user's storyboard
    convention puts character outlines at luminance ~0-50 and lighter
    BG furniture lines at ~80-180; a threshold of 80 separates them
    cleanly. Closing seals 1-2 pixel gaps where the character outline
    crossed a BG line. The resulting binary mask is also saved as
    `<dark_lines_dir>/<key_pose_filename>` (white BG, black character
    outlines) for Node 7 to consume — gives Flux clean character-only
    pixels with no BG to fight at generation time.
    """
    import numpy as np

    png_path = keyposes_dir / key_pose_filename
    gray = _load_grayscale(png_path, shot_id)
    h, w = gray.shape
    min_area_px = max(1, int(round(min_area_ratio * h * w)))

    result = KeyPoseDetections(
        keyPoseIndex=key_pose_index,
        keyPoseFilename=key_pose_filename,
        sourceFrame=source_frame,
        frameWidth=w,
        frameHeight=h,
    )

    # Phase 2f: luminance threshold + morphological closing replaces
    # Otsu binarization. Skip Otsu — the threshold + closing already
    # produces a clean binary mask, and Otsu on a binary input would
    # be a no-op.
    binary = _extract_dark_lines(gray, dark_threshold)
    binary = _close_outline_gaps(binary)

    # Phase 2f side-effect: write the BG-stripped PNG for Node 7.
    if dark_lines_dir is not None:
        _save_dark_lines_png(binary, dark_lines_dir / key_pose_filename)

    bboxes = _detect_bboxes(binary, min_area_px, merge_iou, result.warnings)

    # 5C — reconcile blob count against metadata's expected count.
    expected = len(expected_characters)
    if len(bboxes) != expected:
        bboxes = _reconcile(
            binary=binary,
            bboxes=bboxes,
            expected=expected,
            min_area_px=min_area_px,
            merge_iou=merge_iou,
            warnings=result.warnings,
        )

    # 5D + 5E — position binning + identity assignment.
    detections = _assign_positions_and_identities(
        bboxes=bboxes,
        frame_width=w,
        expected_characters=expected_characters,
    )
    result.detections.extend(detections)
    return result


def _load_grayscale(path: Path, shot_id: str):
    """Load a PNG as a 2-D grayscale numpy array (uint8)."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise CharacterDetectionError(
            f"{shot_id}: required package missing ({e}). "
            "Install numpy + Pillow: pip install numpy pillow"
        ) from e

    try:
        img = Image.open(path).convert("L")
    except Exception as e:  # noqa: BLE001
        raise CharacterDetectionError(
            f"{shot_id}: could not open {path}: {e}"
        ) from e
    return np.asarray(img, dtype=np.uint8)


def _extract_dark_lines(gray, dark_threshold: int):
    """Phase 2f (2026-04-28): isolate dark character outlines from
    lighter BG furniture lines via a fixed luminance threshold.

    Args:
        gray: 2-D uint8 grayscale array.
        dark_threshold: pixels with luminance < this are kept as
            character ink (returned True); pixels >= are treated as
            BG and returned False.

    Returns:
        Boolean ndarray. Same shape as `gray`. True = character ink.

    The user's storyboard convention puts character outlines at
    luminance ~0-50 (dark bold black) and BG furniture / safe-area
    marks at ~80-180 (light grey). A threshold of 80 (default) sits
    at the boundary — anything definitely-dark passes, anything
    definitely-light fails. Operator can tune via `--dark-threshold N`
    if a project's ink darkness drifts.

    Replaces `_binarize_otsu` for the production Phase 2f pipeline:
    Otsu adapts to whatever range of grays exists in the frame, which
    is exactly the wrong behavior when BG lines should be discarded
    rather than treated as another foreground class.
    """
    return gray < dark_threshold


def _close_outline_gaps(
    binary,
    kernel_size: int = DEFAULT_OUTLINE_CLOSING_KERNEL,
):
    """Phase 2f (2026-04-28): morphological closing to seal small
    gaps in the character outline.

    `binary_closing` = dilate then erode. A 3×3 kernel closes gaps
    1-2 pixels wide (e.g., where a character outline crossed a BG
    line and the artist drew BG on top at the intersection point,
    leaving a tiny break in the character outline) without merging
    genuinely separate characters.

    Args:
        binary: boolean mask (True = ink).
        kernel_size: square kernel side length. Default 3.

    Returns:
        Boolean ndarray, same shape, with small gaps closed.
    """
    import numpy as np
    try:
        from scipy import ndimage  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise CharacterDetectionError(
            f"scipy is required for Node 5 (morphological closing): {e}. "
            "Install with: pip install scipy"
        ) from e
    structure = np.ones((kernel_size, kernel_size), dtype=bool)
    return ndimage.binary_closing(binary, structure=structure)


def _save_dark_lines_png(binary, output_path: Path) -> None:
    """Phase 2f (2026-04-28): save the BG-stripped binary mask as a
    PNG that Node 7 reads instead of the raw keypose.

    Polarity matches storyboard convention + Part 1's deliverable:
    True (ink/character) → 0 (black), False (BG) → 255 (white). RGB
    mode (3 channels) so PIL/Flux/Node 8 all consume the same shape
    they consumed before Phase 2f for the raw keyposes.
    """
    try:
        from PIL import Image  # type: ignore[import-not-found]
        import numpy as np
    except ImportError as e:  # pragma: no cover
        raise CharacterDetectionError(
            f"required package missing for dark_lines/ output ({e}). "
            "Install numpy + Pillow."
        ) from e
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rgb = np.where(binary, 0, 255).astype(np.uint8)
    Image.fromarray(rgb, mode="L").convert("RGB").save(
        output_path, format="PNG"
    )


def _wipe_dark_lines_dir(dark_lines_dir: Path) -> None:
    """Phase 2f (2026-04-28) rerun safety: remove stale `*.png` from a
    previous run so `dark_lines/` matches the current keyposes/ exactly.

    Mirrors the wipe-before-write pattern Nodes 3/4/5/6/8/9 already
    use for their per-shot output dirs. Non-PNG entries are left
    alone so an operator can drop debug notes alongside without them
    being clobbered.
    """
    if dark_lines_dir.is_dir():
        for old in dark_lines_dir.glob("*.png"):
            try:
                old.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
    dark_lines_dir.mkdir(parents=True, exist_ok=True)


def _binarize_otsu(gray):
    """Binarize a grayscale image. Foreground = ink (dark) = True.

    Uses Otsu's method: pick the threshold that maximizes between-class
    variance. Robust to variation in paper / ink tone across shots.

    NOTE: As of Phase 2f (2026-04-28), this function is no longer used
    by `_detect_on_key_pose` — the production pipeline uses
    `_extract_dark_lines` + `_close_outline_gaps` instead. Otsu is
    retained because some existing tests exercise it directly and the
    function may be useful for debugging / future workflows.
    """
    import numpy as np

    hist, _ = np.histogram(gray.ravel(), bins=256, range=(0, 256))
    total = gray.size
    if total == 0:
        return np.zeros_like(gray, dtype=bool)

    # Running sums for weight-0 and weight-1 classes.
    cumsum = hist.cumsum().astype(np.float64)
    # Intensity-weighted cumulative sum.
    levels = np.arange(256, dtype=np.float64)
    cumsum_x = (hist * levels).cumsum()
    total_sum = cumsum_x[-1]
    total_count = float(total)

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

    # Pixels DARKER than threshold are ink (foreground, True).
    return gray <= best_t


def _detect_bboxes(
    binary,
    min_area_px: int,
    merge_iou: float,
    warnings: list[DetectionWarning],
) -> list[tuple[int, int, int, int, int]]:
    """Run connected components + cleanup. Return [(x, y, w, h, area), ...]."""
    import numpy as np
    try:
        from scipy import ndimage  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise CharacterDetectionError(
            f"scipy is required for Node 5 (connected components): {e}. "
            "Install with: pip install scipy"
        ) from e

    # 8-connectivity structure (diagonals count as connected).
    structure = np.ones((3, 3), dtype=bool)
    labeled, num = ndimage.label(binary, structure=structure)
    if num == 0:
        return []

    raw_boxes: list[tuple[int, int, int, int, int]] = []
    slices = ndimage.find_objects(labeled)
    for label_idx, sl in enumerate(slices, start=1):
        if sl is None:  # pragma: no cover
            continue
        y_sl, x_sl = sl
        component_mask = labeled[sl] == label_idx
        area = int(component_mask.sum())
        x, y = int(x_sl.start), int(y_sl.start)
        w, h = int(x_sl.stop - x_sl.start), int(y_sl.stop - y_sl.start)
        raw_boxes.append((x, y, w, h, area))

    # Drop tiny (noise/speckle).
    dropped_tiny = [b for b in raw_boxes if b[4] < min_area_px]
    filtered = [b for b in raw_boxes if b[4] >= min_area_px]
    if dropped_tiny:
        warnings.append(DetectionWarning(
            kind="reconcile-dropped",
            message=(
                f"Dropped {len(dropped_tiny)} blob(s) below "
                f"{min_area_px}px area: {[b[4] for b in dropped_tiny]}."
            ),
        ))

    # Merge overlapping (floating detail -> parent).
    merged = _merge_overlapping(filtered, merge_iou)
    if len(merged) < len(filtered):
        warnings.append(DetectionWarning(
            kind="reconcile-merged",
            message=(
                f"Merged {len(filtered) - len(merged)} overlapping "
                f"bounding box(es) (IoU>={merge_iou})."
            ),
        ))

    return merged


def _merge_overlapping(
    boxes: list[tuple[int, int, int, int, int]],
    merge_iou: float,
) -> list[tuple[int, int, int, int, int]]:
    """Greedy-merge any two boxes whose IoU >= merge_iou. Repeat to fixpoint."""
    if not boxes:
        return []
    current = list(boxes)
    changed = True
    while changed:
        changed = False
        new_boxes: list[tuple[int, int, int, int, int]] = []
        used = [False] * len(current)
        for i in range(len(current)):
            if used[i]:
                continue
            ax, ay, aw, ah, a_area = current[i]
            merged_this = False
            for j in range(i + 1, len(current)):
                if used[j]:
                    continue
                bx, by, bw, bh, b_area = current[j]
                if _iou((ax, ay, aw, ah), (bx, by, bw, bh)) >= merge_iou:
                    # Merge via bounding union. Area becomes sum (approximation
                    # — overlapping pixels are over-counted slightly, but the
                    # area is only used as a tiebreaker so that's fine).
                    ux = min(ax, bx)
                    uy = min(ay, by)
                    uw = max(ax + aw, bx + bw) - ux
                    uh = max(ay + ah, by + bh) - uy
                    new_boxes.append((ux, uy, uw, uh, a_area + b_area))
                    used[i] = True
                    used[j] = True
                    merged_this = True
                    changed = True
                    break
            if not merged_this and not used[i]:
                new_boxes.append(current[i])
                used[i] = True
        current = new_boxes
    return current


def _iou(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix0 = max(ax, bx)
    iy0 = max(ay, by)
    ix1 = min(ax + aw, bx + bw)
    iy1 = min(ay + ah, by + bh)
    iw = max(0, ix1 - ix0)
    ih = max(0, iy1 - iy0)
    inter = iw * ih
    if inter == 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    if union <= 0:  # pragma: no cover
        return 0.0
    return inter / union


# -------------------------------------------------------------------
# 5C: reconcile count against metadata
# -------------------------------------------------------------------

def _reconcile(
    binary,
    bboxes: list[tuple[int, int, int, int, int]],
    expected: int,
    min_area_px: int,
    merge_iou: float,
    warnings: list[DetectionWarning],
) -> list[tuple[int, int, int, int, int]]:
    """Try to bring detected count into line with metadata.

    Too many → drop smallest-area blobs until count matches.
    Too few  → re-run CC after progressive binary erosion to pull
               touching characters apart.
    Still wrong after max attempts → emit a "reconcile-failed" warning
    and return whatever we have; Node 6 will fail cleanly.
    """
    if len(bboxes) == expected:
        return bboxes

    if len(bboxes) > expected:
        # Drop smallest-area blobs down to the expected count.
        by_area = sorted(bboxes, key=lambda b: b[4], reverse=True)
        kept = by_area[:expected]
        dropped = by_area[expected:]
        warnings.append(DetectionWarning(
            kind="count-mismatch-over",
            message=(
                f"Detected {len(bboxes)} blob(s), expected {expected}. "
                f"Dropped {len(dropped)} smallest-area blob(s): "
                f"areas={[b[4] for b in dropped]}."
            ),
        ))
        # Re-sort kept by original order (x,y) to preserve spatial layout.
        return sorted(kept, key=lambda b: (b[1], b[0]))

    # Too few — try erosion to split touching characters.
    try:
        from scipy import ndimage  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        warnings.append(DetectionWarning(
            kind="reconcile-failed",
            message=(
                f"Detected {len(bboxes)} blob(s), expected {expected}; "
                "scipy unavailable for erosion retry."
            ),
        ))
        return bboxes

    import numpy as np

    eroded = binary
    for iteration in range(1, DEFAULT_MAX_ERODE_ITERATIONS + 1):
        eroded = ndimage.binary_erosion(
            eroded, structure=np.ones((3, 3), dtype=bool)
        )
        if not eroded.any():
            break
        retry_warnings: list[DetectionWarning] = []
        candidate = _detect_bboxes(
            eroded, min_area_px, merge_iou, retry_warnings
        )
        if len(candidate) >= expected:
            warnings.append(DetectionWarning(
                kind="reconcile-eroded",
                message=(
                    f"Detected {len(bboxes)} blob(s) initially, expected "
                    f"{expected}; erosion x{iteration} produced "
                    f"{len(candidate)} blob(s)."
                ),
            ))
            if len(candidate) > expected:
                # Drop smallest to hit the target exactly.
                by_area = sorted(candidate, key=lambda b: b[4], reverse=True)
                candidate = sorted(
                    by_area[:expected], key=lambda b: (b[1], b[0])
                )
                warnings.append(DetectionWarning(
                    kind="count-mismatch-over",
                    message=(
                        f"After erosion, detected {len(by_area)} blob(s); "
                        f"kept {expected} largest."
                    ),
                ))
            return candidate

    # Exhausted the erosion budget.
    warnings.append(DetectionWarning(
        kind="reconcile-failed",
        message=(
            f"Detected {len(bboxes)} blob(s), expected {expected}; "
            f"still too few after {DEFAULT_MAX_ERODE_ITERATIONS} "
            "erosion pass(es). Node 6 will fail cleanly on this pose."
        ),
    ))
    return bboxes


# -------------------------------------------------------------------
# 5D + 5E: position binning + identity assignment
# -------------------------------------------------------------------

def _bin_position(center_x_normalized: float) -> str:
    """Map a normalized centre-x (0.0..1.0) to L / CL / C / CR / R.

    Uses the locked 25/20/10/20/25 split:
        [0.00, 0.25) -> L
        [0.25, 0.45) -> CL
        [0.45, 0.55) -> C
        [0.55, 0.75) -> CR
        [0.75, 1.00] -> R
    """
    for i, threshold in enumerate(POSITION_THRESHOLDS):
        if center_x_normalized < threshold:
            return POSITION_CODES[i]
    return POSITION_CODES[-1]


def _assign_positions_and_identities(
    bboxes: list[tuple[int, int, int, int, int]],
    frame_width: int,
    expected_characters: list[dict[str, str]],
) -> list[Detection]:
    """Strategy A — sort silhouettes left→right by centre-x, sort
    metadata characters left→right by position rank, zip.

    If we have more bboxes than metadata entries (reconcile couldn't
    drop enough) we zip to the shorter list; leftover bboxes become
    detections with identity="" (Node 6 operator review).
    Same logic if we have fewer bboxes — extra metadata is dropped.
    """
    # Sort bboxes by centre-x.
    by_x = sorted(
        bboxes,
        key=lambda b: (b[0] + b[2] / 2.0),
    )
    # Sort metadata by position rank, tiebreak by original order.
    indexed_meta = list(enumerate(expected_characters))
    by_pos = sorted(
        indexed_meta,
        key=lambda im: (POSITION_RANK[im[1]["position"]], im[0]),
    )

    detections: list[Detection] = []
    pair_count = min(len(by_x), len(by_pos))
    for i in range(pair_count):
        x, y, w, h, area = by_x[i]
        _, meta = by_pos[i]
        center_x_abs = x + w / 2.0
        center_x_norm = (
            float(center_x_abs / frame_width) if frame_width > 0 else 0.0
        )
        detections.append(Detection(
            identity=meta["identity"],
            expectedPosition=meta["position"],
            boundingBox=[x, y, w, h],
            centerX=float(center_x_norm),
            positionCode=_bin_position(center_x_norm),
            area=area,
        ))

    # Unmatched leftovers (extra bboxes with no metadata pair):
    for j in range(pair_count, len(by_x)):
        x, y, w, h, area = by_x[j]
        center_x_abs = x + w / 2.0
        center_x_norm = (
            float(center_x_abs / frame_width) if frame_width > 0 else 0.0
        )
        detections.append(Detection(
            identity="",
            expectedPosition="",
            boundingBox=[x, y, w, h],
            centerX=float(center_x_norm),
            positionCode=_bin_position(center_x_norm),
            area=area,
        ))

    return detections


__all__ = [
    "Detection",
    "DetectionWarning",
    "KeyPoseDetections",
    "CharacterMap",
    "ShotDetectionSummary",
    "Node5Result",
    "detect_characters_for_queue",
    "detect_characters_for_shot",
    "DEFAULT_MIN_AREA_RATIO",
    "DEFAULT_MERGE_IOU",
    "DEFAULT_DARK_THRESHOLD",      # Phase 2f
    "DEFAULT_OUTLINE_CLOSING_KERNEL",  # Phase 2f
    "POSITION_CODES",
    "POSITION_RANK",
]
