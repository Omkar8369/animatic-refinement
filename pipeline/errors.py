"""Custom exception types for the pipeline package.

Every failure mode in every node raises a subclass of `PipelineError` so
each node's CLI has a single catch site. Each subclass's message is
operator-readable — the operator should be able to fix inputs, rerun
extraction, or re-encode a shot based on the message alone.

Hierarchy:

    PipelineError
      Node2Error
        MissingInputError
        SchemaValidationError
        CrossReferenceError
        DuplicateShotIdError
        ShotIdSequenceError
      Node3Error
        QueueInputError
        FFmpegError
        FrameExtractionError
      Node4Error
        Node3ResultInputError
        KeyPoseExtractionError

Deliberately mirrors the locked "fail fast" design decision: for Node 2,
any error raised aborts the entire batch. Node 3 follows the same
fail-fast rule for unrecoverable errors (FFmpegError, QueueInputError)
but emits a non-fatal warning record (not an exception) when a shot's
actual frame count differs from `durationFrames` in metadata — the
operator sees the warning and can decide whether to fix the MP4 or
accept the drift. Node 4 also fail-fasts on I/O or decode errors;
the key-pose/held-frame partition itself is data (no warnings needed
— every frame lands in exactly one key-pose group).
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base class for every pipeline node's error hierarchy."""


# -------------------------------------------------------------------
# Node 2 — Metadata Ingestion & Validation
# -------------------------------------------------------------------

class Node2Error(PipelineError):
    """Base class for all Node 2 validation failures."""


class MissingInputError(Node2Error):
    """A required input file or directory is missing."""


class SchemaValidationError(Node2Error):
    """metadata.json or characters.json has a structural problem
    (malformed JSON, wrong types, out-of-range value, etc.).

    Raised by wrapping a pydantic ValidationError so the operator sees
    one unified failure mode regardless of which file blew up.
    """


class CrossReferenceError(Node2Error):
    """A cross-file reference is unresolved — e.g.:
      * a shot references an identity not in the character library, or
      * a character's sheetFilename does not exist on disk, or
      * a shot's mp4Filename does not exist on disk.
    """


class DuplicateShotIdError(Node2Error):
    """Two or more shots share the same shotId."""


class ShotIdSequenceError(Node2Error):
    """Shot IDs are not sequentially numbered starting at shot_001
    (the form should emit them in order; a gap/skip usually means a
    shot block was deleted mid-edit and the form wasn't re-renumbered).
    """


# -------------------------------------------------------------------
# Node 3 — Shot Pre-processing (MP4 -> PNG)
# -------------------------------------------------------------------

class Node3Error(PipelineError):
    """Base class for all Node 3 frame-extraction failures."""


class QueueInputError(Node3Error):
    """queue.json (from Node 2) is missing, malformed, or references
    an MP4 that no longer exists on disk.

    Distinct from Node 2's MissingInputError so the operator can tell
    "Node 2 never ran" from "Node 3 couldn't consume what Node 2 wrote".
    """


class FFmpegError(Node3Error):
    """ffmpeg invocation failed, or produced zero frames.

    Wraps the non-zero exit code plus stderr tail so the operator can
    diagnose codec issues, corrupt MP4s, missing streams, etc.
    """


class FrameExtractionError(Node3Error):
    """An extracted-frames folder is in an inconsistent state — e.g.
    frame numbering gap, unreadable file, or the target directory
    could not be created.
    """


# -------------------------------------------------------------------
# Node 4 — Key Pose Extraction
# -------------------------------------------------------------------

class Node4Error(PipelineError):
    """Base class for all Node 4 key-pose-extraction failures."""


class Node3ResultInputError(Node4Error):
    """node3_result.json (from Node 3) is missing, malformed, or
    references a per-shot frames folder that no longer exists on disk.

    Distinct from Node 3's QueueInputError so the operator can tell
    "Node 3 never ran" from "Node 4 couldn't consume what Node 3 wrote".
    """


class KeyPoseExtractionError(Node4Error):
    """A frame could not be read, decoded, or compared during the
    key-pose partition — e.g. a PNG is truncated, Pillow rejected it,
    or the numpy FFT path raised on malformed data.

    Wraps the underlying error so the operator can see which shot +
    frame triggered the failure and re-run after fixing the source.
    """


__all__ = [
    "PipelineError",
    # Node 2
    "Node2Error",
    "MissingInputError",
    "SchemaValidationError",
    "CrossReferenceError",
    "DuplicateShotIdError",
    "ShotIdSequenceError",
    # Node 3
    "Node3Error",
    "QueueInputError",
    "FFmpegError",
    "FrameExtractionError",
    # Node 4
    "Node4Error",
    "Node3ResultInputError",
    "KeyPoseExtractionError",
]
