"""Node 2 — Metadata Ingestion & Validation.

Pure Python (no GPU, no ML deps beyond pydantic). Designed to run
identically on the user's laptop and inside a RunPod pod: the input is
a directory of files the operator prepared, the output is either a
validated ProcessingQueue or a hard-fail exception with an operator-
actionable message.

Expected input-directory layout (prepared manually by the operator per
the Node 1 README workflow):

    <input-dir>/
      metadata.json        # Node 1F output
      characters.json      # Node 1A output
      <name>_sheet.png     # one per character listed in characters.json
      <shot>.mp4           # one per shot listed in metadata.json

Sub-steps (aligned with docs/PLAN.md Node 2):
  2A. Parse metadata.json + characters.json.
  2B. Validate character references (sheet PNGs exist, identities resolve).
  2C. Build shot -> character mapping.
  2D. Chunk shots into batches per project.batchSize.
  2E. Verify shot IDs are sequentially numbered with no gaps.

Design decisions (locked 2026-04-23):
  * Runs locally (laptop) and on RunPod. Pure Python, no GPU imports.
  * Hard-fail on any error; never partially process a batch.
  * Schema validation via pydantic v2 (already in ComfyUI's env).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .errors import (
    CrossReferenceError,
    DuplicateShotIdError,
    MissingInputError,
    Node2Error,
    SchemaValidationError,
    ShotIdSequenceError,
)
from .schemas import CharactersFile, CharacterSpec, MetadataFile


# -------------------------------------------------------------------
# Public result types
# -------------------------------------------------------------------

@dataclass(frozen=True)
class ShotJobCharacter:
    """One (identity, sheet-file, position) resolution in a shot job."""
    identity: str
    sheetPath: Path
    position: str


@dataclass(frozen=True)
class ShotJob:
    """One unit of work handed to Nodes 3+.

    Paths are fully resolved against the input directory so downstream
    nodes can open them directly.
    """
    shotId: str
    mp4Path: Path
    durationFrames: int
    durationSeconds: float
    characters: list[ShotJobCharacter] = field(default_factory=list)


@dataclass(frozen=True)
class ProcessingQueue:
    """Output of Node 2. Consumed by the orchestrator that drives Nodes 3-10."""
    projectName: str
    batchSize: int
    batches: list[list[ShotJob]]  # outer = batches, inner = shots in a batch

    @property
    def totalShots(self) -> int:
        return sum(len(b) for b in self.batches)


# -------------------------------------------------------------------
# Public entrypoint
# -------------------------------------------------------------------

def validate_and_build_queue(input_dir: Path | str) -> ProcessingQueue:
    """Run every validation step and return a ProcessingQueue.

    Raises a subclass of Node2Error on any failure. The CLI catches
    these and exits non-zero; library callers handle them however they
    like.
    """
    input_dir = Path(input_dir).resolve()
    if not input_dir.is_dir():
        raise MissingInputError(f"input-dir does not exist: {input_dir}")

    # 2A. Parse metadata + characters.
    metadata = _parse_metadata_file(input_dir / "metadata.json")
    characters = _parse_characters_file(input_dir / "characters.json")

    # 2B. Validate character references against the library + disk.
    char_by_name: dict[str, CharacterSpec] = {c.name: c for c in characters.characters}
    _check_sheet_files_exist(input_dir, characters)
    _check_identities_resolve(metadata, char_by_name)
    _check_mp4_files_exist(input_dir, metadata)

    # 2E. Shot ID uniqueness + sequence (pre-2C so the queue we build is trustable).
    _check_shot_ids_unique(metadata)
    _check_shot_id_sequence(metadata)

    # 2C. Shot -> character mapping, resolving sheet paths.
    # 2D. Chunk by batch size.
    jobs: list[ShotJob] = []
    for shot in metadata.shots:
        shot_chars = [
            ShotJobCharacter(
                identity=c.identity,
                sheetPath=input_dir / char_by_name[c.identity].sheetFilename,
                position=c.position,
            )
            for c in shot.characters
        ]
        jobs.append(
            ShotJob(
                shotId=shot.shotId,
                mp4Path=input_dir / shot.mp4Filename,
                durationFrames=shot.durationFrames,
                durationSeconds=shot.durationSeconds,
                characters=shot_chars,
            )
        )

    bs = metadata.project.batchSize
    batches = [jobs[i : i + bs] for i in range(0, len(jobs), bs)]

    return ProcessingQueue(
        projectName=metadata.project.name,
        batchSize=bs,
        batches=batches,
    )


def serialize_queue(queue: ProcessingQueue) -> dict:
    """Turn a ProcessingQueue into a JSON-safe dict for queue.json.

    queue.json is the artifact Node 11 (orchestrator) reads to drive
    the ComfyUI workflow submissions for Nodes 3-10.
    """
    return {
        "schemaVersion": 1,
        "projectName": queue.projectName,
        "batchSize": queue.batchSize,
        "totalShots": queue.totalShots,
        "batchCount": len(queue.batches),
        "batches": [
            [
                {
                    "shotId": j.shotId,
                    "mp4Path": str(j.mp4Path),
                    "durationFrames": j.durationFrames,
                    "durationSeconds": j.durationSeconds,
                    "characters": [
                        {
                            "identity": c.identity,
                            "sheetPath": str(c.sheetPath),
                            "position": c.position,
                        }
                        for c in j.characters
                    ],
                }
                for j in batch
            ]
            for batch in queue.batches
        ],
    }


# -------------------------------------------------------------------
# Sub-step internals
# -------------------------------------------------------------------

def _parse_metadata_file(path: Path) -> MetadataFile:
    if not path.is_file():
        raise MissingInputError(f"metadata.json not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SchemaValidationError(f"metadata.json is not valid JSON: {e}") from e
    try:
        return MetadataFile.model_validate(raw)
    except ValidationError as e:
        raise SchemaValidationError(
            f"metadata.json failed schema validation:\n{e}"
        ) from e


def _parse_characters_file(path: Path) -> CharactersFile:
    if not path.is_file():
        raise MissingInputError(f"characters.json not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SchemaValidationError(f"characters.json is not valid JSON: {e}") from e
    try:
        return CharactersFile.model_validate(raw)
    except ValidationError as e:
        raise SchemaValidationError(
            f"characters.json failed schema validation:\n{e}"
        ) from e


def _check_sheet_files_exist(input_dir: Path, characters: CharactersFile) -> None:
    missing = [
        f"{c.name} -> {c.sheetFilename}"
        for c in characters.characters
        if not (input_dir / c.sheetFilename).is_file()
    ]
    if missing:
        raise CrossReferenceError(
            "Character sheet PNG(s) missing from input directory:\n  "
            + "\n  ".join(missing)
        )


def _check_identities_resolve(
    metadata: MetadataFile, char_by_name: dict[str, CharacterSpec]
) -> None:
    orphans: list[str] = []
    for shot in metadata.shots:
        for c in shot.characters:
            if c.identity not in char_by_name:
                orphans.append(
                    f"{shot.shotId}: '{c.identity}' (not in characters.json)"
                )
    if orphans:
        raise CrossReferenceError(
            "Shot(s) reference characters not in the character library:\n  "
            + "\n  ".join(orphans)
        )


def _check_mp4_files_exist(input_dir: Path, metadata: MetadataFile) -> None:
    missing = [
        f"{shot.shotId}: {shot.mp4Filename}"
        for shot in metadata.shots
        if not (input_dir / shot.mp4Filename).is_file()
    ]
    if missing:
        raise CrossReferenceError(
            "MP4 file(s) referenced by metadata.json not found in input directory:\n  "
            + "\n  ".join(missing)
        )


def _check_shot_ids_unique(metadata: MetadataFile) -> None:
    seen: dict[str, int] = {}
    dupes: list[str] = []
    for i, shot in enumerate(metadata.shots):
        if shot.shotId in seen:
            dupes.append(f"{shot.shotId} (indices {seen[shot.shotId]} and {i})")
        else:
            seen[shot.shotId] = i
    if dupes:
        raise DuplicateShotIdError("Duplicate shotId(s):\n  " + "\n  ".join(dupes))


def _check_shot_id_sequence(metadata: MetadataFile) -> None:
    expected = [f"shot_{i:03d}" for i in range(1, len(metadata.shots) + 1)]
    actual = [s.shotId for s in metadata.shots]
    if actual != expected:
        raise ShotIdSequenceError(
            f"Shot IDs are not sequential shot_001..shot_{len(metadata.shots):03d}.\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}"
        )


__all__ = [
    "ProcessingQueue",
    "ShotJob",
    "ShotJobCharacter",
    "validate_and_build_queue",
    "serialize_queue",
    "Node2Error",
]
