"""Node 8 - Scene Assembly (Per Key Pose Frame).

Takes Node 7's per-character refined PNGs (one 512x512 PNG per
character per key pose) and composites them onto a single
source-MP4-resolution frame per key pose, ready for Node 9 to
translate-and-copy held frames from.

Locked decisions (do not re-litigate without updating CLAUDE.md):

1.  The bbox is the single source of truth for character placement.
    Node 5 wrote it, Node 7 cropped with it, Node 8 places back with it.
2.  Feet-pinned scaling, NOT stretch-to-fit. Find lowest non-white
    pixel in the 512x512 = character feet; scale by
    `bbox.height / character_height_in_512`; paste centered on
    `(bbox.centerX, bbox.bottomY)` so feet land at the bbox bottom.
3.  Output canvas resolution = source MP4 resolution exactly. Probed
    from `<shotId>/keyposes/frame_<sourceFrame:04d>.png` (~1ms).
4.  Background = solid white.
5.  Z-order = bbox.bottomY ascending (lower-on-screen drawn last).
6.  Line-weight unification = threshold to BnW only, no dilate/erode
    normalize in v1.
7.  Substitute-rough on Node 7 failure (status="error" or empty refined
    PNG), warn-and-reconcile, NOT fail-loud. CLI exits 0; warnings go
    into composed_map.json so Node 11 retry logic can be additive.
8.  Pure-Python (PIL + numpy), GPU-agnostic. Same code runs from CLI,
    pytest, CI, and ComfyUI custom-node wrapper.
9.  Single-threaded (Node 11's concern).
10. Rerun safety: `<shotId>/composed/` is wiped before each run so
    `composed_map.json` always matches the directory exactly.

Inputs:
  * `node7_result.json` -- Node 7's aggregate. Points at each shot's
    `refined_map.json`. That manifest carries (per generation):
    `identity`, `keyPoseIndex`, `sourceFrame`, `refinedPath`,
    `boundingBox: [x, y, w, h]`, `status`. Everything Node 8 needs.

Outputs:
  * `<shotId>/composed/<keyPoseIndex>_composite.png` -- RGB,
    source-MP4 resolution, white background, characters composited
    in z-order, thresholded to BnW.
  * `<shotId>/composed_map.json` -- per-shot list of
    `{keyPoseIndex, sourceFrame, composedPath, characters[],
    warnings[]}`.
  * `<work-dir>/node8_result.json` -- aggregate one-line summary per
    shot.

This module is GPU-agnostic and importable from:
  * `pipeline.cli_node8.main` (CLI)
  * `custom_nodes.node_08_scene_assembler.__init__` (ComfyUI node)
  * `tests/test_node8.py` (pytest)

All error paths raise a `pipeline.errors.Node8Error` subclass.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from pipeline.errors import (
    CompositingError,
    Node7ResultInputError,
    RefinedPngError,
)


# -------------------------------------------------------------------
# Tunables (defaults; CLI / ComfyUI can override)
# -------------------------------------------------------------------

DEFAULT_BACKGROUND = "white"
SUPPORTED_BACKGROUNDS = ("white",)  # "black" / "transparent" reserved for future

# A pixel is "non-white" (i.e., character ink) if its luminance is
# below this. The smoke fixtures use pure-black ink (luminance 0); SD
# outputs are typically <40 on actual line strokes. 250 is intentionally
# high to also count near-white anti-aliased edges as character pixels.
_NONWHITE_LUMA_THRESHOLD = 250

# A refined PNG is treated as "empty" (-> substitute-rough) if fewer
# than this many pixels are below _NONWHITE_LUMA_THRESHOLD. Catches
# all-white outputs from a degenerate SD generation.
_MIN_NONWHITE_PIXELS_FOR_VALID_REFINED = 32

# BnW threshold: any pixel below this becomes black, otherwise white.
_BNW_THRESHOLD = 128


# -------------------------------------------------------------------
# Public result types
# -------------------------------------------------------------------

@dataclass
class CharacterRecord:
    """One character's slot in a composed frame."""
    identity: str
    boundingBox: list[int]  # [x, y, w, h]
    status: str  # "ok" | "skipped" | "error" | "substituted-empty"
    substitutedFromRough: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "boundingBox": list(self.boundingBox),
            "status": self.status,
            "substitutedFromRough": self.substitutedFromRough,
        }


@dataclass
class ComposedKeyPose:
    """One composed frame's record."""
    keyPoseIndex: int
    sourceFrame: int
    composedPath: str
    characters: list[CharacterRecord] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyPoseIndex": self.keyPoseIndex,
            "sourceFrame": self.sourceFrame,
            "composedPath": self.composedPath,
            "characters": [c.to_dict() for c in self.characters],
            "warnings": list(self.warnings),
        }


@dataclass
class ComposedMap:
    """Per-shot composite manifest. Written to
    `<shotId>/composed_map.json`."""
    schemaVersion: int = 1
    shotId: str = ""
    composedDir: str = ""
    keyPoses: list[ComposedKeyPose] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "shotId": self.shotId,
            "composedDir": self.composedDir,
            "keyPoses": [k.to_dict() for k in self.keyPoses],
        }


@dataclass
class ShotComposeSummary:
    """One-line aggregate-result entry per shot."""
    shotId: str
    keyPoseCount: int
    composedCount: int
    substituteCount: int
    composedMapPath: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "shotId": self.shotId,
            "keyPoseCount": self.keyPoseCount,
            "composedCount": self.composedCount,
            "substituteCount": self.substituteCount,
            "composedMapPath": self.composedMapPath,
        }


@dataclass
class Node8Result:
    """Aggregate Node 8 result. Written to
    `<work-dir>/node8_result.json`."""
    schemaVersion: int = 1
    projectName: str = ""
    workDir: str = ""
    background: str = DEFAULT_BACKGROUND
    composedAt: str = ""
    shots: list[ShotComposeSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "projectName": self.projectName,
            "workDir": self.workDir,
            "background": self.background,
            "composedAt": self.composedAt,
            "shots": [s.to_dict() for s in self.shots],
        }


# -------------------------------------------------------------------
# 8A - Input loading + validation
# -------------------------------------------------------------------

def load_node7_result(path: Path) -> dict[str, Any]:
    """Load and minimally validate node7_result.json.

    Raises:
        Node7ResultInputError: missing, not JSON, wrong shape, or
            wrong schemaVersion.
    """
    if not path.is_file():
        raise Node7ResultInputError(
            f"node7_result.json not found at {path}. Run Node 7 first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node7ResultInputError(
            f"node7_result.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node7ResultInputError(
            f"node7_result.json at {path} must be a JSON object; "
            f"got {type(raw).__name__}."
        )
    if raw.get("schemaVersion") != 1:
        raise Node7ResultInputError(
            f"node7_result.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 8 expects 1."
        )
    for key in ("workDir", "shots"):
        if key not in raw:
            raise Node7ResultInputError(
                f"node7_result.json at {path} missing required key '{key}'."
            )
    if not isinstance(raw["shots"], list):
        raise Node7ResultInputError(
            f"node7_result.json at {path}: 'shots' must be a list."
        )
    for s_idx, shot in enumerate(raw["shots"]):
        if not isinstance(shot, dict):
            raise Node7ResultInputError(
                f"node7_result.json: shots[{s_idx}] is not an object."
            )
        for key in ("shotId", "refinedMapPath"):
            if key not in shot:
                raise Node7ResultInputError(
                    f"node7_result.json: shots[{s_idx}] missing '{key}'."
                )
    return raw


def load_refined_map(path: Path, shot_id: str) -> dict[str, Any]:
    """Load a shot's `refined_map.json` (Node 7 output).

    Raises:
        Node7ResultInputError: missing, malformed, or schema mismatch.
    """
    if not path.is_file():
        raise Node7ResultInputError(
            f"refined_map.json for shot {shot_id!r} not found at "
            f"{path}. Did Node 7 finish for this shot?"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node7ResultInputError(
            f"refined_map.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node7ResultInputError(
            f"refined_map.json at {path} must be a JSON object; "
            f"got {type(raw).__name__}."
        )
    if raw.get("schemaVersion") != 1:
        raise Node7ResultInputError(
            f"refined_map.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 8 expects 1."
        )
    for key in ("shotId", "refinedDir", "generations"):
        if key not in raw:
            raise Node7ResultInputError(
                f"refined_map.json at {path} missing required key "
                f"'{key}'."
            )
    if raw["shotId"] != shot_id:
        raise Node7ResultInputError(
            f"refined_map.json at {path} has shotId={raw['shotId']!r} "
            f"but node7_result.json said {shot_id!r}. Stale work dir?"
        )
    if not isinstance(raw["generations"], list):
        raise Node7ResultInputError(
            f"refined_map.json at {path}: 'generations' must be a list."
        )
    for g_idx, gen in enumerate(raw["generations"]):
        if not isinstance(gen, dict):
            raise Node7ResultInputError(
                f"refined_map.json: generations[{g_idx}] is not an object."
            )
        for key in (
            "identity", "keyPoseIndex", "sourceFrame",
            "refinedPath", "boundingBox", "status",
        ):
            if key not in gen:
                raise Node7ResultInputError(
                    f"refined_map.json: generations[{g_idx}] missing "
                    f"'{key}'."
                )
        bbox = gen["boundingBox"]
        if not (isinstance(bbox, list) and len(bbox) == 4
                and all(isinstance(v, int) for v in bbox)):
            raise Node7ResultInputError(
                f"refined_map.json: generations[{g_idx}] boundingBox "
                f"must be a list of 4 ints [x, y, w, h]; got {bbox!r}."
            )
    return raw


def _shot_root_for(refined_map: dict[str, Any]) -> Path:
    """`<shot_root>` directory: parent of `refinedDir` (which is
    `<shot_root>/refined`)."""
    refined_dir = Path(refined_map["refinedDir"])
    return refined_dir.parent


def _keypose_path(shot_root: Path, source_frame: int) -> Path:
    """Locate the rough key-pose PNG for `sourceFrame`. Node 4's locked
    decision #6 is that key-pose copies preserve source filenames in
    `frame_NNNN.png` format, so the path is deterministic from
    `sourceFrame` alone."""
    return shot_root / "keyposes" / f"frame_{source_frame:04d}.png"


# -------------------------------------------------------------------
# 8B / 8C - Compositing primitives
# -------------------------------------------------------------------

def _build_canvas(width: int, height: int, background: str) -> Image.Image:
    """Locked decision #4: solid white background. The `background`
    arg exists for future-proofing but currently only "white" is
    supported."""
    if background != "white":
        raise CompositingError(
            f"Background {background!r} not supported in v1. "
            f"Supported: {SUPPORTED_BACKGROUNDS}."
        )
    return Image.new("RGB", (width, height), (255, 255, 255))


def _detect_character_extent(refined_rgb: np.ndarray) -> tuple[int, int, int]:
    """Find the character's vertical extent + horizontal centroid in
    the refined PNG.

    Returns:
        (top_y, bottom_y, center_x) in refined-PNG coordinates.

    Raises:
        ValueError: refined image has too few non-white pixels (caller
            translates this to a substitute-rough fallback).
    """
    luma = (
        0.299 * refined_rgb[..., 0]
        + 0.587 * refined_rgb[..., 1]
        + 0.114 * refined_rgb[..., 2]
    )
    nonwhite = luma < _NONWHITE_LUMA_THRESHOLD
    if int(nonwhite.sum()) < _MIN_NONWHITE_PIXELS_FOR_VALID_REFINED:
        raise ValueError(
            f"refined PNG has {int(nonwhite.sum())} non-white pixel(s) "
            f"(<{_MIN_NONWHITE_PIXELS_FOR_VALID_REFINED} threshold) -- "
            "treating as empty"
        )
    rows_with_content = nonwhite.any(axis=1)
    cols_with_content = nonwhite.any(axis=0)
    nonwhite_rows = np.flatnonzero(rows_with_content)
    nonwhite_cols = np.flatnonzero(cols_with_content)
    top_y = int(nonwhite_rows[0])
    bottom_y = int(nonwhite_rows[-1])
    # horizontal centroid: midpoint of the leftmost and rightmost
    # non-white columns. Robust to small asymmetries in the silhouette.
    center_x = int((nonwhite_cols[0] + nonwhite_cols[-1]) // 2)
    return top_y, bottom_y, center_x


def _feet_pinned_paste(
    canvas: Image.Image,
    refined_path: Path,
    bbox: list[int],
) -> bool:
    """Paste a refined character onto `canvas` using feet-pinned scaling.

    Algorithm (locked decision #2):
      1. Open refined PNG, convert to RGB.
      2. Find vertical extent + centroid of character pixels.
      3. Scale by `bbox.height / character_height_in_refined`.
      4. Compute paste offset so character's (centerX, feet) lands on
         (bbox.centerX, bbox.bottomY).
      5. Paste with a "non-white" mask so only character ink lands on
         the canvas (leaves white margin around the silhouette
         transparent).

    Returns:
        True on successful paste; False if the refined PNG is empty
        and the caller should fall back to substitute-rough.

    Raises:
        CompositingError: PIL/numpy raised an unexpected exception
            (decode failure that wasn't caught earlier, OOM, etc.).
    """
    try:
        refined = Image.open(refined_path).convert("RGB")
    except Exception as e:  # noqa: BLE001 - any decode failure -> caller's substitute path
        return False
    arr = np.asarray(refined, dtype=np.uint8)
    try:
        top_y, bottom_y, center_x_refined = _detect_character_extent(arr)
    except ValueError:
        # Empty PNG -> caller substitutes the rough.
        return False

    bbox_x, bbox_y, bbox_w, bbox_h = bbox
    if bbox_w <= 0 or bbox_h <= 0:
        # Degenerate bbox -- can't paste anything sensible; let
        # caller fall back. This is a contract violation upstream
        # (Node 5 should never emit a zero-area bbox), but we tolerate
        # it rather than crash.
        return False

    char_height_in_refined = max(1, bottom_y - top_y + 1)
    scale = bbox_h / char_height_in_refined
    new_w = max(1, int(round(refined.width * scale)))
    new_h = max(1, int(round(refined.height * scale)))

    try:
        resized = refined.resize((new_w, new_h), Image.LANCZOS)
    except Exception as e:  # noqa: BLE001
        raise CompositingError(
            f"PIL resize failed for refined {refined_path}: "
            f"{type(e).__name__}: {e}"
        ) from e

    # In the resized image, the character's feet are at `bottom_y * scale`
    # and the centroid is at `center_x_refined * scale`.
    feet_y_resized = int(round(bottom_y * scale))
    center_x_resized = int(round(center_x_refined * scale))

    # Bbox `[x, y, w, h]` covers inclusive rows `y .. y+h-1` (PIL/Numpy
    # convention). The last paint-able row is `y+h-1`, so the feet land
    # on `y+h-1`, NOT `y+h`. Same for centerX (bbox_x + bbox_w // 2 is
    # already the inclusive column index since integer division rounds
    # down).
    bbox_centerX = bbox_x + bbox_w // 2
    bbox_bottomY = bbox_y + bbox_h - 1
    paste_x = bbox_centerX - center_x_resized
    paste_y = bbox_bottomY - feet_y_resized

    # Build a per-pixel "is character ink" mask from the resized image
    # so the white margin around the silhouette doesn't overpaint the
    # canvas. Without this, every paste would clobber the canvas with
    # the refined image's white background.
    resized_arr = np.asarray(resized, dtype=np.uint8)
    luma = (
        0.299 * resized_arr[..., 0]
        + 0.587 * resized_arr[..., 1]
        + 0.114 * resized_arr[..., 2]
    )
    mask_arr = np.where(
        luma < _NONWHITE_LUMA_THRESHOLD, 255, 0
    ).astype(np.uint8)
    mask = Image.fromarray(mask_arr, mode="L")

    try:
        canvas.paste(resized, (paste_x, paste_y), mask)
    except Exception as e:  # noqa: BLE001
        raise CompositingError(
            f"PIL paste failed at offset ({paste_x}, {paste_y}) "
            f"size ({new_w}, {new_h}) onto canvas {canvas.size}: "
            f"{type(e).__name__}: {e}"
        ) from e
    return True


def _substitute_rough(
    canvas: Image.Image,
    keypose_path: Path,
    bbox: list[int],
) -> bool:
    """Paste the rough key-pose pixels at `bbox` onto `canvas`.

    Locked decision #7: when Node 7 marked a generation as errored or
    its refined PNG is empty, we fall back to copying the rough's
    bbox region onto the final canvas. Keeps timing intact for Node 9
    (no holes in the keypose sequence).

    Returns:
        True on successful substitute; False if the rough itself is
        missing/unreadable (caller raises `RefinedPngError`).
    """
    if not keypose_path.is_file():
        return False
    try:
        rough = Image.open(keypose_path).convert("RGB")
    except Exception:  # noqa: BLE001
        return False
    bbox_x, bbox_y, bbox_w, bbox_h = bbox
    # Clip bbox to rough dims so a too-large bbox doesn't crash crop().
    rx0 = max(0, bbox_x)
    ry0 = max(0, bbox_y)
    rx1 = min(rough.width, bbox_x + bbox_w)
    ry1 = min(rough.height, bbox_y + bbox_h)
    if rx1 <= rx0 or ry1 <= ry0:
        # Bbox entirely outside rough frame -- nothing to substitute.
        return False
    try:
        crop = rough.crop((rx0, ry0, rx1, ry1))
        canvas.paste(crop, (rx0, ry0))
    except Exception as e:  # noqa: BLE001
        raise CompositingError(
            f"PIL substitute-rough failed at bbox={bbox} "
            f"keypose={keypose_path}: {type(e).__name__}: {e}"
        ) from e
    return True


def _threshold_to_bnw(canvas: Image.Image) -> Image.Image:
    """Locked decision #6: collapse the composed RGB canvas to pure
    BnW via luminance threshold. No dilate/erode normalize in v1."""
    arr = np.asarray(canvas.convert("L"), dtype=np.uint8)
    bnw = np.where(arr < _BNW_THRESHOLD, 0, 255).astype(np.uint8)
    bnw_rgb = np.stack([bnw, bnw, bnw], axis=2)
    return Image.fromarray(bnw_rgb, mode="RGB")


# -------------------------------------------------------------------
# 8D / 8E - Per-key-pose orchestration
# -------------------------------------------------------------------

def _group_by_keypose(
    generations: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """Group `refined_map.generations` by `keyPoseIndex`."""
    out: dict[int, list[dict[str, Any]]] = {}
    for gen in generations:
        out.setdefault(gen["keyPoseIndex"], []).append(gen)
    return out


def _is_refined_ok(refined_path: Path, status: str) -> bool:
    """Quick check: status must be "ok" AND the file must exist
    AND have at least one non-white pixel. The full content check
    happens inside `_feet_pinned_paste`; this fast-path lets the caller
    pick the right code path without opening the image twice."""
    if status != "ok":
        return False
    return refined_path.is_file()


def _compose_one_keypose(
    *,
    shot_id: str,
    shot_root: Path,
    composed_dir: Path,
    key_pose_index: int,
    generations: list[dict[str, Any]],
    background: str,
) -> ComposedKeyPose:
    """Build a single composed frame (one PNG per key pose).

    Steps:
        1. Probe the rough keypose for canvas dims (locked #3).
        2. Build white canvas (locked #4).
        3. Z-sort generations by bbox.bottomY ascending (locked #5).
        4. Per generation: feet-pinned paste OR substitute-rough.
        5. Threshold to BnW (locked #6).
        6. Save to <composed_dir>/<keyPoseIndex>_composite.png.
    """
    # All generations for the same key pose share the same sourceFrame
    # (Node 4's locked invariant: one key pose -> one source frame).
    source_frames = {g["sourceFrame"] for g in generations}
    if len(source_frames) != 1:
        raise Node7ResultInputError(
            f"refined_map.json shot={shot_id!r} keyPoseIndex="
            f"{key_pose_index}: multiple sourceFrame values "
            f"{sorted(source_frames)} for a single key pose. Stale or "
            "hand-edited Node 7 output?"
        )
    source_frame = source_frames.pop()
    keypose_path = _keypose_path(shot_root, source_frame)

    # 8A.5 -- probe canvas dims from rough keypose.
    if not keypose_path.is_file():
        raise RefinedPngError(
            f"shot={shot_id!r} keyPoseIndex={key_pose_index} "
            f"sourceFrame={source_frame}: rough key-pose PNG not "
            f"found at {keypose_path}. Cannot probe canvas dims and "
            "cannot substitute-rough as a fallback."
        )
    try:
        with Image.open(keypose_path) as kp:
            canvas_w, canvas_h = kp.size
    except Exception as e:  # noqa: BLE001
        raise RefinedPngError(
            f"shot={shot_id!r} keyPoseIndex={key_pose_index}: failed "
            f"to read rough key-pose {keypose_path}: "
            f"{type(e).__name__}: {e}"
        ) from e

    canvas = _build_canvas(canvas_w, canvas_h, background)

    # 8D -- z-sort by bbox.bottomY ascending so lower-on-screen
    # characters paste last (= drawn on top). Stable secondary sort on
    # identity for deterministic output when two characters share a
    # bottomY.
    sorted_gens = sorted(
        generations,
        key=lambda g: (
            g["boundingBox"][1] + g["boundingBox"][3],
            g["identity"],
        ),
    )

    character_records: list[CharacterRecord] = []
    warnings: list[dict[str, str]] = []

    # 8C -- per-character paste with substitute-rough fallback.
    for gen in sorted_gens:
        identity = gen["identity"]
        bbox = list(gen["boundingBox"])
        status = gen["status"]
        refined_path = Path(gen["refinedPath"])
        substituted = False
        record_status = status

        wants_paste = _is_refined_ok(refined_path, status)
        pasted = False
        if wants_paste:
            pasted = _feet_pinned_paste(canvas, refined_path, bbox)
            if not pasted:
                # Refined PNG was decodable but had too few non-white
                # pixels (or had a bad header). Treat as empty -> sub.
                record_status = "substituted-empty"
                warnings.append({
                    "level": "warn",
                    "code": "refined-empty-or-unreadable",
                    "identity": identity,
                    "keyPoseIndex": str(key_pose_index),
                    "refinedPath": str(refined_path),
                    "message": (
                        f"Refined PNG for {identity!r} at key pose "
                        f"{key_pose_index} was empty or unreadable -- "
                        "fell back to substitute-rough."
                    ),
                })

        if not pasted:
            sub_ok = _substitute_rough(canvas, keypose_path, bbox)
            if not sub_ok:
                raise RefinedPngError(
                    f"shot={shot_id!r} keyPoseIndex={key_pose_index} "
                    f"identity={identity!r}: refined PNG at "
                    f"{refined_path} unfillable AND substitute-rough "
                    f"from {keypose_path} also failed (bbox out of "
                    f"frame or rough unreadable). Cannot proceed for "
                    "this character slot."
                )
            substituted = True
            if status != "ok" and record_status == status:
                # Status was already error/skipped from Node 7 -- log
                # the substitute as a warning so the operator can
                # collect-and-retry.
                warnings.append({
                    "level": "warn",
                    "code": f"node7-{status}",
                    "identity": identity,
                    "keyPoseIndex": str(key_pose_index),
                    "refinedPath": str(refined_path),
                    "message": (
                        f"Node 7 marked {identity!r} at key pose "
                        f"{key_pose_index} as {status!r} -- "
                        "substituted rough key-pose pixels."
                    ),
                })

        character_records.append(CharacterRecord(
            identity=identity,
            boundingBox=bbox,
            status=record_status,
            substitutedFromRough=substituted,
        ))

    # 8D.5 -- locked decision #6: threshold composite to BnW.
    canvas = _threshold_to_bnw(canvas)

    # 8E -- save composed PNG.
    composed_filename = f"{key_pose_index:03d}_composite.png"
    composed_path = composed_dir / composed_filename
    try:
        canvas.save(composed_path, "PNG")
    except Exception as e:  # noqa: BLE001
        raise CompositingError(
            f"PIL save failed for {composed_path}: "
            f"{type(e).__name__}: {e}"
        ) from e

    return ComposedKeyPose(
        keyPoseIndex=key_pose_index,
        sourceFrame=source_frame,
        composedPath=str(composed_path),
        characters=character_records,
        warnings=warnings,
    )


# -------------------------------------------------------------------
# Top-level driver
# -------------------------------------------------------------------

def compose_for_queue(
    *,
    node7_result_path: Path,
    background: str = DEFAULT_BACKGROUND,
) -> Node8Result:
    """Drive Node 8 across every shot in `node7_result.json`.

    For each shot:
      * load `refined_map.json`
      * group generations by `keyPoseIndex`
      * wipe `<shot_root>/composed/`
      * compose each key pose
      * write `<shot_root>/composed_map.json`
    Then write the aggregate `<work-dir>/node8_result.json`.

    Returns:
        Node8Result describing what was written. Same structure as
        `node8_result.json`.

    Raises:
        Node7ResultInputError: malformed / missing manifest.
        RefinedPngError: a slot is unfillable (refined dead AND rough
            dead).
        CompositingError: PIL/numpy crashed unexpectedly.
    """
    n7_path = Path(node7_result_path)
    n7 = load_node7_result(n7_path)
    work_dir = Path(n7["workDir"])

    summaries: list[ShotComposeSummary] = []
    for shot in n7["shots"]:
        shot_id = shot["shotId"]
        refined_map_path = Path(shot["refinedMapPath"])
        refined_map = load_refined_map(refined_map_path, shot_id)
        shot_root = _shot_root_for(refined_map)
        composed_dir = shot_root / "composed"

        # Locked decision #10: rerun safety -- wipe stale composed/
        # PNGs first so composed_map.json always matches the dir.
        if composed_dir.exists():
            for stale in composed_dir.glob("*_composite.png"):
                stale.unlink()
        else:
            composed_dir.mkdir(parents=True, exist_ok=True)

        per_kp = _group_by_keypose(refined_map["generations"])
        composed_keyposes: list[ComposedKeyPose] = []
        substitute_count = 0
        for kp_idx in sorted(per_kp.keys()):
            ck = _compose_one_keypose(
                shot_id=shot_id,
                shot_root=shot_root,
                composed_dir=composed_dir,
                key_pose_index=kp_idx,
                generations=per_kp[kp_idx],
                background=background,
            )
            composed_keyposes.append(ck)
            substitute_count += sum(
                1 for c in ck.characters if c.substitutedFromRough
            )

        # Per-shot manifest
        composed_map_path = shot_root / "composed_map.json"
        composed_map = ComposedMap(
            schemaVersion=1,
            shotId=shot_id,
            composedDir=str(composed_dir),
            keyPoses=composed_keyposes,
        )
        composed_map_path.write_text(
            json.dumps(composed_map.to_dict(), indent=2),
            encoding="utf-8",
        )

        summaries.append(ShotComposeSummary(
            shotId=shot_id,
            keyPoseCount=len(composed_keyposes),
            composedCount=len(composed_keyposes),
            substituteCount=substitute_count,
            composedMapPath=str(composed_map_path),
        ))

    result = Node8Result(
        schemaVersion=1,
        projectName=n7.get("projectName", ""),
        workDir=str(work_dir),
        background=background,
        composedAt=datetime.now(timezone.utc).isoformat(),
        shots=summaries,
    )
    aggregate_path = work_dir / "node8_result.json"
    aggregate_path.write_text(
        json.dumps(result.to_dict(), indent=2),
        encoding="utf-8",
    )
    return result
