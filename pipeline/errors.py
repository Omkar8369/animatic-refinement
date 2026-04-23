"""Custom exception types for Node 2 validation.

Every failure mode raises a subclass of `Node2Error` so the CLI has a
single catch site. Each subclass's message is operator-readable — the
operator should be able to fix `metadata.json` / `characters.json` or
place missing files based on the message alone.

Deliberately mirrors the locked "fail fast" design decision: any one of
these being raised aborts the entire batch. The pipeline never partially
runs.
"""

from __future__ import annotations


class Node2Error(Exception):
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
