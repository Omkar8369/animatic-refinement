"""Pydantic schemas for Node 2 — strict validation of Node 1's JSON outputs.

These mirror the shapes emitted by the browser frontend (see
`frontend/README.md` for the authoritative descriptions). Any shape
mismatch becomes a readable pydantic error that the operator uses to
fix `metadata.json` or `characters.json` and re-run.

Schema-level validation (here, pydantic) covers:
  * field types, required vs optional, ranges, regex patterns
  * cross-field rules within a single object (e.g. characterCount == len(characters))
  * non-empty collections

Cross-file / on-disk validation (shot references → characters library,
sheet PNG files exist, MP4 files exist, shot IDs unique + sequential)
lives in `node2.py`, not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# The 5 legal screen positions. Locked by project convention.
Position = Literal["L", "CL", "C", "CR", "R"]


# -------------------------------------------------------------------
# metadata.json shapes
# -------------------------------------------------------------------

class ProjectSpec(BaseModel):
    """Batch-level fields at the top of metadata.json."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Operator-typed project name.")
    batchSize: int = Field(
        ..., ge=1, le=64,
        description="Shots per RunPod batch; governs VRAM headroom.",
    )
    # 25 FPS is a hard-locked convention; anything else is operator error.
    fps: Literal[25] = 25
    notes: str = ""


class ShotCharacter(BaseModel):
    """One (identity, position) pair inside a shot's characters list."""

    model_config = ConfigDict(extra="forbid")

    identity: str = Field(
        ..., min_length=1,
        description="Character name; must match a character in characters.json.",
    )
    position: Position


class ShotSpec(BaseModel):
    """One shot block from the metadata form."""

    model_config = ConfigDict(extra="forbid")

    shotId: str = Field(
        ..., pattern=r"^shot_\d{3,}$",
        description="Canonical id like 'shot_001'. Sequence check is in node2.py.",
    )
    mp4Filename: str = Field(
        ..., min_length=1,
        description="Filename only, no directory. Existence check is in node2.py.",
    )
    durationFrames: int = Field(..., ge=1)
    durationSeconds: float = Field(..., gt=0)
    characterCount: int = Field(..., ge=0)
    characters: list[ShotCharacter]

    @model_validator(mode="after")
    def _consistency_checks(self) -> "ShotSpec":
        if len(self.characters) != self.characterCount:
            raise ValueError(
                f"{self.shotId}: characterCount={self.characterCount} does not "
                f"match len(characters)={len(self.characters)}"
            )
        if "/" in self.mp4Filename or "\\" in self.mp4Filename:
            raise ValueError(
                f"{self.shotId}: mp4Filename must be a bare filename, not a path"
            )
        return self


class MetadataFile(BaseModel):
    """Top-level shape of metadata.json."""

    model_config = ConfigDict(extra="forbid")

    schemaVersion: int = Field(..., ge=1)
    generatedAt: datetime
    project: ProjectSpec
    shots: list[ShotSpec] = Field(..., min_length=1)


# -------------------------------------------------------------------
# characters.json shapes
# -------------------------------------------------------------------

class QualitySpec(BaseModel):
    """Client-side sanity-check result from the Character Library page.

    The frontend's characters.html records these fields when the operator
    uploads a sheet PNG; they're informational (Node 6 re-derives its own
    island count from the actual file). Schema mirrors the JS exactly so
    operator-produced characters.json round-trips through Node 2's
    pydantic validator without a forbidden-extras error.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    detectedIslands: int | None = None
    backgroundMode: str | None = None
    reasons: list[str] = []
    # Sheet dimensions captured by the frontend at upload time. Useful
    # for debugging "operator uploaded a tiny 64x64 sheet" issues.
    # Optional because earlier characters.json files (predating this
    # frontend feature) won't have them.
    width: int | None = None
    height: int | None = None


class CharacterSpec(BaseModel):
    """One character entry in characters.json."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    sheetFilename: str = Field(..., min_length=1)
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)
    quality: QualitySpec
    addedAt: datetime
    # Node 7 routing: humans go through DWPose skeleton extraction +
    # DWPose ControlNet; non-humans (quadrupeds, props with model sheets
    # like Jaggu the monkey) fall back to a LineArt/Scribble CN stack
    # from the rough. Default "dwpose" so libraries written before this
    # field was introduced still load cleanly.
    poseExtractor: Literal["dwpose", "lineart-fallback"] = "dwpose"
    # Phase 2e (locked decision #9, additive per #10): optional
    # per-character LoRA. When set, Node 7 v2 stacks this LoRA on top of
    # the style LoRA via a chained second LoraLoader (workflow node 21).
    # When None, Node 7 falls back to "style LoRA + IP-Adapter only" —
    # adequate for ~85% identity quality without the per-character
    # training overhead. Phase 2e adds the actual training infrastructure
    # + populates these fields per-character (TAPPU first, then
    # CHAMPAK_LAL, …). Phase 1 + Phase 2a characters.json files load
    # cleanly because the field defaults to None.
    characterLoraFilename: str | None = None
    # Strength applied when stacking the character LoRA on top of the
    # style LoRA (locked decision #9). 0.85 is the recommended starting
    # point — over-stacking causes the two LoRAs to fight; tune
    # per-character based on test runs.
    characterLoraStrength: float = Field(0.85, ge=0.0, le=2.0)

    @model_validator(mode="after")
    def _bare_filename(self) -> "CharacterSpec":
        if "/" in self.sheetFilename or "\\" in self.sheetFilename:
            raise ValueError(
                f"{self.name}: sheetFilename must be a bare filename, not a path"
            )
        if self.characterLoraFilename is not None and (
            "/" in self.characterLoraFilename
            or "\\" in self.characterLoraFilename
        ):
            raise ValueError(
                f"{self.name}: characterLoraFilename must be a bare "
                "filename, not a path"
            )
        return self


class ConventionsSpec(BaseModel):
    """The sheet-format conventions Node 6 will check before slicing."""

    model_config = ConfigDict(extra="forbid")

    sheetFormat: str
    backgroundExpected: str
    angleOrderLeftToRight: list[str]
    # Defaults to True in newly-generated libraries since the canonical 8-angle
    # order was confirmed on 2026-04-23; Node 6 still enforces that False trips
    # a hard error so an operator forking the template can't silently ship a
    # new angle layout.
    angleOrderConfirmed: bool


class CharactersFile(BaseModel):
    """Top-level shape of characters.json."""

    model_config = ConfigDict(extra="forbid")

    schemaVersion: int = Field(..., ge=1)
    generatedAt: datetime
    conventions: ConventionsSpec
    characters: list[CharacterSpec] = Field(..., min_length=1)
