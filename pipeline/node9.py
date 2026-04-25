"""Node 9 - Timing Reconstruction (Re-apply Held Frames).

Rebuilds the full per-frame sequence from Node 8's per-key-pose
composites + Node 4's per-frame timing map (`keypose_map.json`).
Output is one PNG per frame of the original shot, ready for Node 10
to encode back to MP4 at the original timing.

Critical property: zero AI regeneration on held frames. Held frames
are pixel-translates of refined anchors. This was the whole reason
Node 4 went translation-aware in the first place.

Locked decisions (do not re-litigate without updating CLAUDE.md):

1.  Translate-and-copy on a fresh white canvas - NO AI on held frames.
2.  Whole-frame translation, not per-character (per-character is
    already baked into Node 8's composite).
3.  Output canvas resolution = Node 8 composite resolution = source
    MP4 resolution.
4.  Exposed-region fill = solid white.
5.  Output frame numbering = 1-indexed, 4-digit zero-padded
    `frame_NNNN.png`.
6.  Inputs = `--node8-result <path>` only. Node 9 chases pointers
    from there.
7.  Fail-loud on missing composed PNG (TimingReconstructionError),
    NOT substitute-rough.
8.  Total-frame-count mismatch is a hard error.
9.  Translation offsets larger than canvas are NOT errors (the
    resulting PNG is mostly-white; mathematically valid).
10. Same frame index in multiple keyPoses is a hard error.
11. Pure-Python (PIL + numpy), GPU-agnostic.
12. Single-threaded.
13. Rerun safety: <shotId>/timed/ wiped before each run.

Inputs:
  * `node8_result.json` -- Node 8's aggregate. Points at each shot's
    `composed_map.json`. Node 9 chases `composed_map.json -> shot
    root -> keypose_map.json` (Node 4's timing data, sibling to
    composed_map.json).

Outputs:
  * `<shotId>/timed/frame_NNNN.png` -- RGB, source MP4 resolution,
    white background, one PNG per frame of the original shot.
  * `<shotId>/timed_map.json` -- per-shot per-frame record.
  * `<work-dir>/node9_result.json` -- aggregate.

This module is GPU-agnostic and importable from:
  * `pipeline.cli_node9.main` (CLI)
  * `custom_nodes.node_09_timing_reconstructor.__init__` (ComfyUI)
  * `tests/test_node9.py` (pytest)

All error paths raise a `pipeline.errors.Node9Error` subclass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from pipeline.errors import (
    FrameCountMismatchError,
    KeyPoseMapInputError,
    Node8ResultInputError,
    TimingReconstructionError,
)


# -------------------------------------------------------------------
# Public result types
# -------------------------------------------------------------------

@dataclass
class TimedFrameRecord:
    """One per-frame entry in `timed_map.json`."""
    frameIndex: int
    sourceKeyPoseIndex: int
    offset: list[int]  # [dy, dx]
    composedSourcePath: str
    timedPath: str
    isAnchor: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "frameIndex": self.frameIndex,
            "sourceKeyPoseIndex": self.sourceKeyPoseIndex,
            "offset": list(self.offset),
            "composedSourcePath": self.composedSourcePath,
            "timedPath": self.timedPath,
            "isAnchor": self.isAnchor,
        }


@dataclass
class TimedMap:
    """Per-shot timing manifest. Written to
    `<shotId>/timed_map.json`."""
    schemaVersion: int = 1
    shotId: str = ""
    timedDir: str = ""
    totalFrames: int = 0
    frames: list[TimedFrameRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "shotId": self.shotId,
            "timedDir": self.timedDir,
            "totalFrames": self.totalFrames,
            "frames": [f.to_dict() for f in self.frames],
        }


@dataclass
class ShotTimingSummary:
    """One-line aggregate per shot."""
    shotId: str
    totalFrames: int
    keyPoseCount: int
    anchorCount: int
    heldCount: int
    timedMapPath: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "shotId": self.shotId,
            "totalFrames": self.totalFrames,
            "keyPoseCount": self.keyPoseCount,
            "anchorCount": self.anchorCount,
            "heldCount": self.heldCount,
            "timedMapPath": self.timedMapPath,
        }


@dataclass
class Node9Result:
    """Aggregate Node 9 result. Written to
    `<work-dir>/node9_result.json`."""
    schemaVersion: int = 1
    projectName: str = ""
    workDir: str = ""
    reconstructedAt: str = ""
    shots: list[ShotTimingSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "projectName": self.projectName,
            "workDir": self.workDir,
            "reconstructedAt": self.reconstructedAt,
            "shots": [s.to_dict() for s in self.shots],
        }


# -------------------------------------------------------------------
# 9A - Input loading + validation
# -------------------------------------------------------------------

def load_node8_result(path: Path) -> dict[str, Any]:
    """Load and minimally validate node8_result.json.

    Raises:
        Node8ResultInputError: missing, not JSON, wrong shape, or
            wrong schemaVersion.
    """
    if not path.is_file():
        raise Node8ResultInputError(
            f"node8_result.json not found at {path}. Run Node 8 first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node8ResultInputError(
            f"node8_result.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node8ResultInputError(
            f"node8_result.json at {path} must be a JSON object; "
            f"got {type(raw).__name__}."
        )
    if raw.get("schemaVersion") != 1:
        raise Node8ResultInputError(
            f"node8_result.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 9 expects 1."
        )
    for key in ("workDir", "shots"):
        if key not in raw:
            raise Node8ResultInputError(
                f"node8_result.json at {path} missing required key '{key}'."
            )
    if not isinstance(raw["shots"], list):
        raise Node8ResultInputError(
            f"node8_result.json at {path}: 'shots' must be a list."
        )
    for s_idx, shot in enumerate(raw["shots"]):
        if not isinstance(shot, dict):
            raise Node8ResultInputError(
                f"node8_result.json: shots[{s_idx}] is not an object."
            )
        for key in ("shotId", "composedMapPath"):
            if key not in shot:
                raise Node8ResultInputError(
                    f"node8_result.json: shots[{s_idx}] missing '{key}'."
                )
    return raw


def load_composed_map(path: Path, shot_id: str) -> dict[str, Any]:
    """Load a shot's `composed_map.json` (Node 8 output).

    Raises:
        Node8ResultInputError: missing, malformed, or schema mismatch.
    """
    if not path.is_file():
        raise Node8ResultInputError(
            f"composed_map.json for shot {shot_id!r} not found at "
            f"{path}. Did Node 8 finish for this shot?"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node8ResultInputError(
            f"composed_map.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node8ResultInputError(
            f"composed_map.json at {path} must be a JSON object."
        )
    if raw.get("schemaVersion") != 1:
        raise Node8ResultInputError(
            f"composed_map.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 9 expects 1."
        )
    for key in ("shotId", "composedDir", "keyPoses"):
        if key not in raw:
            raise Node8ResultInputError(
                f"composed_map.json at {path} missing '{key}'."
            )
    if raw["shotId"] != shot_id:
        raise Node8ResultInputError(
            f"composed_map.json at {path} has shotId={raw['shotId']!r} "
            f"but node8_result.json said {shot_id!r}. Stale work dir?"
        )
    if not isinstance(raw["keyPoses"], list):
        raise Node8ResultInputError(
            f"composed_map.json at {path}: 'keyPoses' must be a list."
        )
    for kp_idx, kp in enumerate(raw["keyPoses"]):
        if not isinstance(kp, dict):
            raise Node8ResultInputError(
                f"composed_map.json: keyPoses[{kp_idx}] is not an object."
            )
        for key in ("keyPoseIndex", "composedPath"):
            if key not in kp:
                raise Node8ResultInputError(
                    f"composed_map.json: keyPoses[{kp_idx}] missing "
                    f"'{key}'."
                )
    return raw


def load_keypose_map(path: Path, shot_id: str) -> dict[str, Any]:
    """Load a shot's `keypose_map.json` (Node 4 output, sibling to
    composed_map.json).

    Raises:
        KeyPoseMapInputError: missing, malformed, schema mismatch, or
            Node 4 per-frame invariants violated.
    """
    if not path.is_file():
        raise KeyPoseMapInputError(
            f"keypose_map.json for shot {shot_id!r} not found at "
            f"{path}. Did Node 4 finish for this shot? "
            "(Node 9 chases <shot_root>/keypose_map.json from "
            "composed_map.json's composedDir parent.)"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise KeyPoseMapInputError(
            f"keypose_map.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise KeyPoseMapInputError(
            f"keypose_map.json at {path} must be a JSON object."
        )
    if raw.get("schemaVersion") != 1:
        raise KeyPoseMapInputError(
            f"keypose_map.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 9 expects 1."
        )
    for key in ("shotId", "totalFrames", "keyPoses"):
        if key not in raw:
            raise KeyPoseMapInputError(
                f"keypose_map.json at {path} missing '{key}'."
            )
    if raw["shotId"] != shot_id:
        raise KeyPoseMapInputError(
            f"keypose_map.json at {path} has shotId={raw['shotId']!r} "
            f"but node8_result.json said {shot_id!r}. Stale work dir?"
        )
    total = raw["totalFrames"]
    if not isinstance(total, int) or total < 1:
        raise KeyPoseMapInputError(
            f"keypose_map.json at {path}: totalFrames must be a "
            f"positive int; got {total!r}."
        )
    if not isinstance(raw["keyPoses"], list) or not raw["keyPoses"]:
        raise KeyPoseMapInputError(
            f"keypose_map.json at {path}: 'keyPoses' must be a "
            "non-empty list."
        )
    return raw


# -------------------------------------------------------------------
# 9B - Build per-frame lookup table (validates Node 4 invariants)
# -------------------------------------------------------------------

def _build_frame_lookup(
    keypose_map: dict[str, Any],
    shot_id: str,
) -> dict[int, tuple[int, list[int], bool]]:
    """Walk every key pose's anchor + heldFrames, build a
    `frame_index -> (keyPoseIndex, [dy, dx], isAnchor)` lookup.

    Validates Node 4's invariants along the way:
      - every frame index in [1, totalFrames]
      - no frame index appears in two keyPoses
      - no duplicate keyPoseIndex
      - offset is a list of two ints

    Raises:
        KeyPoseMapInputError: any invariant violated.
    """
    total = keypose_map["totalFrames"]
    lookup: dict[int, tuple[int, list[int], bool]] = {}
    seen_kp_indices: set[int] = set()

    for kp in keypose_map["keyPoses"]:
        if not isinstance(kp, dict):
            raise KeyPoseMapInputError(
                f"keypose_map.json shot={shot_id!r}: keyPoses entry "
                f"is not an object: {kp!r}"
            )
        kp_idx = kp.get("keyPoseIndex")
        anchor_frame = kp.get("sourceFrame")
        if not isinstance(kp_idx, int) or kp_idx < 0:
            raise KeyPoseMapInputError(
                f"keypose_map.json shot={shot_id!r}: invalid "
                f"keyPoseIndex={kp_idx!r}."
            )
        if kp_idx in seen_kp_indices:
            raise KeyPoseMapInputError(
                f"keypose_map.json shot={shot_id!r}: duplicate "
                f"keyPoseIndex={kp_idx} -- Node 4 invariant violation."
            )
        seen_kp_indices.add(kp_idx)
        if not isinstance(anchor_frame, int) or not (1 <= anchor_frame <= total):
            raise KeyPoseMapInputError(
                f"keypose_map.json shot={shot_id!r} keyPoseIndex="
                f"{kp_idx}: sourceFrame={anchor_frame!r} outside "
                f"[1, totalFrames={total}]."
            )
        # Anchor frame: offset is identity, isAnchor=True
        if anchor_frame in lookup:
            raise KeyPoseMapInputError(
                f"keypose_map.json shot={shot_id!r}: frame "
                f"{anchor_frame} appears in multiple keyPoses (anchor "
                f"of keyPoseIndex={kp_idx} collides with previously "
                f"recorded keyPoseIndex={lookup[anchor_frame][0]}) -- "
                "Node 4 invariant violation."
            )
        lookup[anchor_frame] = (kp_idx, [0, 0], True)

        # Held frames
        held_list = kp.get("heldFrames", [])
        if not isinstance(held_list, list):
            raise KeyPoseMapInputError(
                f"keypose_map.json shot={shot_id!r} keyPoseIndex="
                f"{kp_idx}: heldFrames must be a list."
            )
        for h_idx, held in enumerate(held_list):
            if not isinstance(held, dict):
                raise KeyPoseMapInputError(
                    f"keypose_map.json shot={shot_id!r} keyPoseIndex="
                    f"{kp_idx} heldFrames[{h_idx}]: not an object."
                )
            fidx = held.get("frame")
            offset = held.get("offset")
            if not isinstance(fidx, int) or not (1 <= fidx <= total):
                raise KeyPoseMapInputError(
                    f"keypose_map.json shot={shot_id!r} keyPoseIndex="
                    f"{kp_idx} heldFrames[{h_idx}]: frame={fidx!r} "
                    f"outside [1, totalFrames={total}]."
                )
            if not (isinstance(offset, list) and len(offset) == 2
                    and all(isinstance(v, int) for v in offset)):
                raise KeyPoseMapInputError(
                    f"keypose_map.json shot={shot_id!r} keyPoseIndex="
                    f"{kp_idx} heldFrames[{h_idx}]: offset must be a "
                    f"list of 2 ints [dy, dx]; got {offset!r}."
                )
            if fidx in lookup:
                raise KeyPoseMapInputError(
                    f"keypose_map.json shot={shot_id!r}: frame {fidx} "
                    f"appears in multiple keyPoses (heldFrame of "
                    f"keyPoseIndex={kp_idx} collides with previously "
                    f"recorded keyPoseIndex={lookup[fidx][0]}) -- "
                    "Node 4 invariant violation."
                )
            lookup[fidx] = (kp_idx, list(offset), False)

    return lookup


def _build_composite_path_lookup(
    composed_map: dict[str, Any],
    shot_id: str,
) -> dict[int, str]:
    """Walk composed_map's keyPoses, return
    `keyPoseIndex -> composedPath`."""
    out: dict[int, str] = {}
    for ck in composed_map["keyPoses"]:
        kp_idx = ck.get("keyPoseIndex")
        comp_path = ck.get("composedPath")
        if not isinstance(kp_idx, int) or not comp_path:
            raise Node8ResultInputError(
                f"composed_map.json shot={shot_id!r}: keyPose entry "
                f"missing keyPoseIndex/composedPath: {ck!r}"
            )
        if kp_idx in out:
            raise Node8ResultInputError(
                f"composed_map.json shot={shot_id!r}: duplicate "
                f"keyPoseIndex={kp_idx}."
            )
        out[kp_idx] = comp_path
    return out


# -------------------------------------------------------------------
# 9C - Translate-and-copy primitives
# -------------------------------------------------------------------

def _translate_and_copy(
    composite: Image.Image,
    offset_dy_dx: list[int],
) -> Image.Image:
    """Locked decision #1: paste `composite` onto a fresh white
    canvas at offset `(dx, dy)`. PIL auto-clips at boundaries;
    uncovered regions stay white. Mathematically valid for any offset
    including ones that push the character entirely off-canvas."""
    dy, dx = offset_dy_dx
    canvas = Image.new("RGB", composite.size, (255, 255, 255))
    canvas.paste(composite, (dx, dy))
    return canvas


# -------------------------------------------------------------------
# Top-level driver
# -------------------------------------------------------------------

def reconstruct_timing_for_queue(
    *,
    node8_result_path: Path,
) -> Node9Result:
    """Drive Node 9 across every shot in `node8_result.json`.

    For each shot:
      * load `composed_map.json` (Node 8 output)
      * load sibling `keypose_map.json` (Node 4 output)
      * build per-frame lookup table (validates Node 4 invariants)
      * build per-keyPoseIndex composed-PNG path lookup
      * wipe `<shot_root>/timed/`
      * for each frame in `1..totalFrames`: open composite (cached),
        copy as-is for anchor frames or translate-and-copy for held
        frames, save to `<shot_root>/timed/frame_NNNN.png`
      * verify reconstructed PNG count == totalFrames
      * write `<shot_root>/timed_map.json`
    Then write the aggregate `<work-dir>/node9_result.json`.

    Returns:
        Node9Result describing what was written.

    Raises:
        Node8ResultInputError: malformed/missing Node 8 manifest.
        KeyPoseMapInputError: malformed/missing Node 4 manifest, or
            its data violates Node 4 invariants.
        TimingReconstructionError: a slot's source composed PNG is
            missing or unreadable (no fallback).
        FrameCountMismatchError: reconstructed count != totalFrames.
    """
    n8_path = Path(node8_result_path)
    n8 = load_node8_result(n8_path)
    work_dir = Path(n8["workDir"])

    summaries: list[ShotTimingSummary] = []
    for shot in n8["shots"]:
        shot_id = shot["shotId"]
        composed_map_path = Path(shot["composedMapPath"])
        composed_map = load_composed_map(composed_map_path, shot_id)

        # <shot_root> is the parent of composed_map's composedDir
        # (which is `<shot_root>/composed`). Same shape as Node 8's
        # `_shot_root_for(refined_map)`.
        shot_root = Path(composed_map["composedDir"]).parent
        keypose_map_path = shot_root / "keypose_map.json"
        keypose_map = load_keypose_map(keypose_map_path, shot_id)

        # 9B - validate + build frame lookup
        frame_lookup = _build_frame_lookup(keypose_map, shot_id)
        composite_path_by_kp = _build_composite_path_lookup(
            composed_map, shot_id,
        )

        # Locked decision #13: rerun safety -- wipe <shot>/timed/
        timed_dir = shot_root / "timed"
        if timed_dir.exists():
            for stale in timed_dir.glob("frame_*.png"):
                stale.unlink()
        else:
            timed_dir.mkdir(parents=True, exist_ok=True)

        # 9C - translate-and-copy per frame, with composite cache
        composite_cache: dict[int, Image.Image] = {}
        timed_records: list[TimedFrameRecord] = []
        anchor_count = 0
        held_count = 0
        for fidx in sorted(frame_lookup.keys()):
            kp_idx, offset, is_anchor = frame_lookup[fidx]

            # Resolve + cache composite
            if kp_idx not in composite_cache:
                comp_path_str = composite_path_by_kp.get(kp_idx)
                if not comp_path_str:
                    raise TimingReconstructionError(
                        f"shot={shot_id!r} keyPoseIndex={kp_idx} "
                        f"frame={fidx}: keypose_map.json references "
                        "this keyPoseIndex but composed_map.json has "
                        "no composedPath for it. Re-run Node 8."
                    )
                comp_path = Path(comp_path_str)
                if not comp_path.is_file():
                    raise TimingReconstructionError(
                        f"shot={shot_id!r} keyPoseIndex={kp_idx} "
                        f"frame={fidx}: composedPath {comp_path} "
                        "does not exist on disk. Re-run Node 8."
                    )
                try:
                    composite_cache[kp_idx] = Image.open(comp_path).convert("RGB")
                except Exception as e:  # noqa: BLE001
                    raise TimingReconstructionError(
                        f"shot={shot_id!r} keyPoseIndex={kp_idx}: "
                        f"failed to open composedPath {comp_path}: "
                        f"{type(e).__name__}: {e}"
                    ) from e

            composite = composite_cache[kp_idx]
            timed_path = timed_dir / f"frame_{fidx:04d}.png"

            if is_anchor:
                # Bit-identical save -- no translation step needed.
                composite.save(timed_path, "PNG")
                anchor_count += 1
            else:
                # Locked decisions #1, #2, #4, #9: translate-and-copy
                # on fresh white canvas, off-canvas is OK.
                translated = _translate_and_copy(composite, offset)
                translated.save(timed_path, "PNG")
                held_count += 1

            timed_records.append(TimedFrameRecord(
                frameIndex=fidx,
                sourceKeyPoseIndex=kp_idx,
                offset=list(offset),
                composedSourcePath=composite_path_by_kp[kp_idx],
                timedPath=str(timed_path),
                isAnchor=is_anchor,
            ))

        # 9D - assemble + verify
        expected_total = keypose_map["totalFrames"]
        actual_total = anchor_count + held_count
        if actual_total != expected_total:
            raise FrameCountMismatchError(
                f"shot={shot_id!r}: reconstructed {actual_total} "
                f"frame(s) but keypose_map.totalFrames={expected_total}. "
                "Node 4 invariant violation upstream."
            )

        # 9E - emit per-shot manifest
        timed_map_path = shot_root / "timed_map.json"
        timed_map = TimedMap(
            schemaVersion=1,
            shotId=shot_id,
            timedDir=str(timed_dir),
            totalFrames=expected_total,
            frames=timed_records,
        )
        timed_map_path.write_text(
            json.dumps(timed_map.to_dict(), indent=2),
            encoding="utf-8",
        )

        summaries.append(ShotTimingSummary(
            shotId=shot_id,
            totalFrames=expected_total,
            keyPoseCount=len(composite_path_by_kp),
            anchorCount=anchor_count,
            heldCount=held_count,
            timedMapPath=str(timed_map_path),
        ))

    # Aggregate
    result = Node9Result(
        schemaVersion=1,
        projectName=n8.get("projectName", ""),
        workDir=str(work_dir),
        reconstructedAt=datetime.now(timezone.utc).isoformat(),
        shots=summaries,
    )
    aggregate_path = work_dir / "node9_result.json"
    aggregate_path.write_text(
        json.dumps(result.to_dict(), indent=2),
        encoding="utf-8",
    )
    return result
