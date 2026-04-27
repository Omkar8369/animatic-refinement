"""Pure-Python manifest I/O for Node 7 - Pose Refinement.

Node 7 breaks the `pipeline/nodeN.py` template on purpose (locked
decision #9): the authoritative artifact is `workflow.json` (a ComfyUI
graph), and the custom-node wrapper only marshals inputs/outputs and
logs metadata. So the "business logic" in Node 7 is entirely manifest
I/O + orchestration glue, which is what this module + `orchestrate.py`
+ `comfyui_client.py` provide.

This module is GPU-agnostic and importable from:
  * `pipeline.cli_node7.main` (CLI)
  * `custom_nodes.node_07_pose_refiner.__init__` (ComfyUI node)
  * `tests/test_node7.py` (pytest)

All error paths raise a `pipeline.errors.Node7Error` subclass (or
`QueueLookupError`, reused from Node 5's module with identical
semantics).

Inputs:
  * `node6_result.json` - Node 6's aggregate. Points at each shot's
    `reference_map.json`.
  * `queue.json`        - Node 2's output. Supplies each character's
    `poseExtractor` route (`dwpose` vs. `lineart-fallback`).

For every (shotId, keyPoseIndex, identity) triple in each shot's
`reference_map.json` this module produces one `DetectionTask` with
every path the downstream orchestrator needs (rough key-pose PNG, Node
5 bbox, Node 6 color + line-art reference crops, pose extractor
route). `orchestrate.py` iterates those tasks, submits one ComfyUI
workflow per task, collects the refined output, and writes the Node 7
manifests via helpers in this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.errors import (
    Node6ResultInputError,
    QueueLookupError,
    RefinementGenerationError,
)


# -------------------------------------------------------------------
# Public result / task types
# -------------------------------------------------------------------

@dataclass(frozen=True)
class DetectionTask:
    """One refinement unit: the rough + reference + pose route for a
    single character in a single key pose in a single shot.

    `seed` is deterministic: derived from the project name + this
    triple so the same work produces the same seed across reruns
    (locked decision #7 -- per-(shotId, keyPoseIndex, identity) seed
    logging for deterministic re-runs).
    """
    shotId: str
    keyPoseIndex: int
    keyPoseFilename: str
    sourceFrame: int
    identity: str
    poseExtractor: str  # "dwpose" | "lineart-fallback"
    expectedPosition: str
    boundingBox: tuple[int, int, int, int]  # (x, y, w, h) from Node 5
    selectedAngle: str  # from Node 6
    keyPosePath: Path  # absolute: keyposes/<keyPoseFilename>
    referenceColorCropPath: Path  # from Node 6E
    referenceLineArtCropPath: Path  # from Node 6E
    refinedPath: Path  # target output: <shotId>/refined/<keyPoseIndex>_<identity>.png
    seed: int


@dataclass
class RefinedGeneration:
    """One (DetectionTask, outcome) record written to refined_map.json.

    Phase 2 (locked decision #14, additive per #10) added three optional
    fields recording WHICH workflow + precision + character LoRA were
    used for this generation. Defaults preserve Phase 1 record shape so
    old refined_map.json files still load through Phase 2 readers and
    new Phase 1 generations still write the same on-disk shape they did
    before Phase 2 (`workflowName="v1"`, `precision="fp8"` per locked
    decision #14, `characterLoraFilename=None`).
    """
    identity: str
    keyPoseIndex: int
    sourceFrame: int
    selectedAngle: str
    poseExtractor: str
    seed: int
    refinedPath: str
    boundingBox: list[int]
    status: str  # "ok" | "skipped" | "error"
    errorMessage: str = ""
    cnStrengths: dict[str, float] = field(default_factory=dict)
    # Phase 2 additions (locked decision #14). All have Phase 1 defaults
    # so old code paths that build a RefinedGeneration without these
    # fields still produce a valid record.
    workflowName: str = "v1"
    precision: str = "fp8"
    characterLoraFilename: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "keyPoseIndex": self.keyPoseIndex,
            "sourceFrame": self.sourceFrame,
            "selectedAngle": self.selectedAngle,
            "poseExtractor": self.poseExtractor,
            "seed": self.seed,
            "refinedPath": self.refinedPath,
            "boundingBox": self.boundingBox,
            "status": self.status,
            "errorMessage": self.errorMessage,
            "cnStrengths": dict(self.cnStrengths),
            "workflowName": self.workflowName,
            "precision": self.precision,
            "characterLoraFilename": self.characterLoraFilename,
        }


@dataclass
class RefinedMap:
    """Per-shot refined-character manifest.

    Written to `<shotId>/refined_map.json` alongside `character_map.json`,
    `reference_map.json`, and `keyposes/`. Node 8 reads this to know
    which refined PNG to composite into each slot.
    """
    schemaVersion: int = 1
    shotId: str = ""
    refinedDir: str = ""
    generations: list[RefinedGeneration] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "shotId": self.shotId,
            "refinedDir": self.refinedDir,
            "generations": [g.to_dict() for g in self.generations],
        }


@dataclass
class ShotRefinedSummary:
    """One-line summary of a shot's Node 7 output."""
    shotId: str
    keyPoseCount: int
    generatedCount: int
    skippedCount: int
    errorCount: int
    refinedMapPath: str


@dataclass
class Node7Result:
    """Aggregate Node 7 result. Written to `<work-dir>/node7_result.json`."""
    schemaVersion: int = 1
    projectName: str = ""
    workDir: str = ""
    comfyUIUrl: str = ""
    dryRun: bool = False
    refinedAt: str = ""
    shots: list[ShotRefinedSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "projectName": self.projectName,
            "workDir": self.workDir,
            "comfyUIUrl": self.comfyUIUrl,
            "dryRun": self.dryRun,
            "refinedAt": self.refinedAt,
            "shots": [
                {
                    "shotId": s.shotId,
                    "keyPoseCount": s.keyPoseCount,
                    "generatedCount": s.generatedCount,
                    "skippedCount": s.skippedCount,
                    "errorCount": s.errorCount,
                    "refinedMapPath": s.refinedMapPath,
                }
                for s in self.shots
            ],
        }


# -------------------------------------------------------------------
# Input loaders (7A - validate + parse)
# -------------------------------------------------------------------

def load_node6_result(path: Path) -> dict[str, Any]:
    """Load and minimally validate node6_result.json.

    Raises:
        Node6ResultInputError: missing, not JSON, wrong shape, or
            wrong schemaVersion.
    """
    if not path.is_file():
        raise Node6ResultInputError(
            f"node6_result.json not found at {path}. Run Node 6 first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node6ResultInputError(
            f"node6_result.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node6ResultInputError(
            f"node6_result.json at {path} must be a JSON object; "
            f"got {type(raw).__name__}."
        )
    if raw.get("schemaVersion") != 1:
        raise Node6ResultInputError(
            f"node6_result.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 7 expects 1."
        )
    for key in ("workDir", "shots"):
        if key not in raw:
            raise Node6ResultInputError(
                f"node6_result.json at {path} missing required key '{key}'."
            )
    if not isinstance(raw["shots"], list):
        raise Node6ResultInputError(
            f"node6_result.json at {path}: 'shots' must be a list."
        )
    for s_idx, shot in enumerate(raw["shots"]):
        if not isinstance(shot, dict):
            raise Node6ResultInputError(
                f"node6_result.json: shots[{s_idx}] is not an object."
            )
        for key in ("shotId", "referenceMapPath"):
            if key not in shot:
                raise Node6ResultInputError(
                    f"node6_result.json: shots[{s_idx}] missing '{key}'."
                )
    return raw


def load_queue(path: Path) -> dict[str, Any]:
    """Load queue.json (reused from Node 2's output) and validate only
    what Node 7 needs: `batches[][].shotId` + per-character `identity`
    + `poseExtractor`. Schema-level validation was already done by
    Node 2 via pydantic; we re-check just enough to produce a readable
    error if queue.json is stale or hand-edited.
    """
    if not path.is_file():
        raise QueueLookupError(
            f"queue.json not found at {path}. Node 7 needs it to look "
            "up each character's poseExtractor route."
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
            f"{raw.get('schemaVersion')!r}; Node 7 expects 1."
        )
    if "batches" not in raw or not isinstance(raw["batches"], list):
        raise QueueLookupError(
            f"queue.json at {path}: missing or non-list 'batches'."
        )
    return raw


def build_pose_extractor_lookup(
    queue: dict[str, Any],
) -> dict[tuple[str, str], str]:
    """Map `(shotId, identity)` -> `poseExtractor` for every character
    in queue.json.

    Raises:
        QueueLookupError: a shot character is missing identity or
            poseExtractor (stale queue.json written before Node 2
            added the field).
    """
    lookup: dict[tuple[str, str], str] = {}
    for batch in queue.get("batches", []):
        for shot in batch:
            shot_id = shot.get("shotId")
            if not shot_id:
                continue
            for c in shot.get("characters", []):
                identity = c.get("identity")
                pose_ex = c.get("poseExtractor")
                if not identity or not pose_ex:
                    raise QueueLookupError(
                        f"queue.json shot {shot_id!r}: character "
                        f"missing identity or poseExtractor. Rerun "
                        "Node 2 after confirming characters.json has "
                        "the poseExtractor field."
                    )
                lookup[(shot_id, identity)] = pose_ex
    return lookup


def load_reference_map(path: Path, shot_id: str) -> dict[str, Any]:
    """Load a shot's `reference_map.json` (Node 6E output).

    Raises:
        Node6ResultInputError: missing, unreadable, or wrong schema.
    """
    if not path.is_file():
        raise Node6ResultInputError(
            f"{shot_id}: reference_map.json not found at {path}. "
            "Did Node 6 complete for this shot?"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node6ResultInputError(
            f"{shot_id}: reference_map.json is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node6ResultInputError(
            f"{shot_id}: reference_map.json must be a JSON object."
        )
    if raw.get("schemaVersion") != 1:
        raise Node6ResultInputError(
            f"{shot_id}: reference_map.json schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 7 expects 1."
        )
    for key in ("keyPosesDir", "keyPoses"):
        if key not in raw:
            raise Node6ResultInputError(
                f"{shot_id}: reference_map.json missing '{key}'."
            )
    if not isinstance(raw["keyPoses"], list):
        raise Node6ResultInputError(
            f"{shot_id}: reference_map.json 'keyPoses' must be a list."
        )
    return raw


# -------------------------------------------------------------------
# 7A: routing table builder
# -------------------------------------------------------------------

def build_routing_table(
    node6_result: dict[str, Any],
    queue: dict[str, Any],
) -> list[DetectionTask]:
    """Walk node6_result.json + every per-shot reference_map.json and
    emit one `DetectionTask` per (shotId, keyPoseIndex, identity).

    Paths are fully resolved. Missing reference_map.json files, stale
    key-pose PNGs, stale reference crops, and missing poseExtractor
    routes all surface as readable errors the operator can action.
    """
    project_name = str(node6_result.get("projectName", ""))
    pose_lookup = build_pose_extractor_lookup(queue)
    work_dir = Path(node6_result["workDir"]).resolve()

    tasks: list[DetectionTask] = []

    for shot_summary in node6_result["shots"]:
        shot_id = str(shot_summary["shotId"])
        ref_map_path = Path(shot_summary["referenceMapPath"]).resolve()
        rm = load_reference_map(ref_map_path, shot_id)

        shot_root = ref_map_path.parent
        keyposes_dir = Path(rm["keyPosesDir"]).resolve()
        refined_dir = shot_root / "refined"

        for kp in rm["keyPoses"]:
            kp_index = int(kp["keyPoseIndex"])
            kp_filename = str(kp["keyPoseFilename"])
            source_frame = int(kp["sourceFrame"])
            kp_path = keyposes_dir / kp_filename
            if not kp_path.is_file():
                raise Node6ResultInputError(
                    f"{shot_id} key-pose {kp_index}: PNG missing at "
                    f"{kp_path}. Rerun Node 4 to refresh keyposes/."
                )

            for match in kp.get("matches", []):
                identity = str(match.get("identity", ""))
                if not identity:
                    # Node 6 already skips unpaired detections (Node 5
                    # reconcile leftovers). Node 7 does the same.
                    continue

                bbox = match.get("boundingBox")
                if not isinstance(bbox, list) or len(bbox) != 4:
                    raise Node6ResultInputError(
                        f"{shot_id} key-pose {kp_index} identity="
                        f"{identity!r}: missing or malformed boundingBox "
                        f"in reference_map.json."
                    )

                color_path = Path(match["referenceColorCropPath"]).resolve()
                lineart_path = Path(match["referenceLineArtCropPath"]).resolve()
                for p in (color_path, lineart_path):
                    if not p.is_file():
                        raise Node6ResultInputError(
                            f"{shot_id} key-pose {kp_index} identity="
                            f"{identity!r}: reference crop missing at "
                            f"{p}. Rerun Node 6 to refresh "
                            "reference_crops/."
                        )

                key = (shot_id, identity)
                if key not in pose_lookup:
                    raise QueueLookupError(
                        f"{shot_id} key-pose {kp_index}: identity "
                        f"{identity!r} has no poseExtractor in "
                        "queue.json. Rerun Node 2 after confirming "
                        "characters.json has poseExtractor for this "
                        "character."
                    )
                pose_ex = pose_lookup[key]

                refined_filename = (
                    f"{kp_index:03d}_{_safe_segment(identity)}.png"
                )
                refined_path = refined_dir / refined_filename
                seed = _derive_seed(
                    project_name, shot_id, kp_index, identity
                )

                tasks.append(
                    DetectionTask(
                        shotId=shot_id,
                        keyPoseIndex=kp_index,
                        keyPoseFilename=kp_filename,
                        sourceFrame=source_frame,
                        identity=identity,
                        poseExtractor=pose_ex,
                        expectedPosition=str(
                            match.get("expectedPosition", "")
                        ),
                        boundingBox=(
                            int(bbox[0]), int(bbox[1]),
                            int(bbox[2]), int(bbox[3]),
                        ),
                        selectedAngle=str(match.get("selectedAngle", "")),
                        keyPosePath=kp_path,
                        referenceColorCropPath=color_path,
                        referenceLineArtCropPath=lineart_path,
                        refinedPath=refined_path,
                        seed=seed,
                    )
                )

    # Keep `work_dir` reachable for the orchestrator without a second
    # node6_result load.
    _ = work_dir
    return tasks


# -------------------------------------------------------------------
# 7F: manifest writers
# -------------------------------------------------------------------

def write_refined_map(
    shot_id: str,
    shot_root: Path,
    generations: list[RefinedGeneration],
) -> Path:
    """Write `<shot_root>/refined_map.json`. Returns the path."""
    refined_dir = shot_root / "refined"
    rm = RefinedMap(
        shotId=shot_id,
        refinedDir=str(refined_dir.resolve()),
        generations=list(generations),
    )
    out = shot_root / "refined_map.json"
    out.write_text(
        json.dumps(rm.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out


def write_node7_result(
    node6_result: dict[str, Any],
    shot_summaries: list[ShotRefinedSummary],
    comfyui_url: str,
    dry_run: bool,
) -> Node7Result:
    """Build + write `<work-dir>/node7_result.json`. Returns the
    Node7Result so the caller can hand the exact same object back to
    ComfyUI or a test -- with `refinedAt` already stamped.
    """
    work_dir = Path(node6_result["workDir"]).resolve()
    result = Node7Result(
        projectName=str(node6_result.get("projectName", "")),
        workDir=str(work_dir),
        comfyUIUrl=comfyui_url,
        dryRun=dry_run,
        refinedAt=datetime.now(timezone.utc).isoformat(),
        shots=list(shot_summaries),
    )
    out = work_dir / "node7_result.json"
    out.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


# -------------------------------------------------------------------
# Internals
# -------------------------------------------------------------------

def _safe_segment(s: str) -> str:
    """Make a string safe to use as a filename segment."""
    return "".join(
        ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in s
    )


def _derive_seed(
    project_name: str,
    shot_id: str,
    key_pose_index: int,
    identity: str,
) -> int:
    """Deterministic 32-bit seed per (project, shotId, keyPoseIndex, identity).

    SD samplers take a signed 32-bit seed range but ComfyUI accepts any
    non-negative int; we mask to 31 bits to stay safely portable. This
    is a locked-decision choice: re-running Node 7 with the same inputs
    produces the same seed so re-runs are visually reproducible.
    """
    key = f"{project_name}|{shot_id}|{key_pose_index}|{identity}"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


# Re-exported to keep orchestrate.py / CLI imports stable even if
# internals change.
__all__ = [
    "DetectionTask",
    "RefinedGeneration",
    "RefinedMap",
    "ShotRefinedSummary",
    "Node7Result",
    "load_node6_result",
    "load_queue",
    "load_reference_map",
    "build_pose_extractor_lookup",
    "build_routing_table",
    "write_refined_map",
    "write_node7_result",
]

# Re-exported from RefinementGenerationError's module so sibling code can
# `from ...manifest import RefinementGenerationError` for convenience.
__all__.append("RefinementGenerationError")
