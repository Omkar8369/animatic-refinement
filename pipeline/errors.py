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
      Node5Error
        Node4ResultInputError
        QueueLookupError        # shared with Node 6 (see below)
        CharacterDetectionError
      Node6Error
        Node5ResultInputError
        CharactersInputError
          AngleOrderUnconfirmedError
        ReferenceSheetFormatError
        ReferenceSheetSliceError
        AngleMatchingError
      Node7Error
        Node6ResultInputError
        WorkflowTemplateError
        ComfyUIConnectionError
        RefinementGenerationError

`QueueLookupError` lives under `Node5Error` but is also raised by Node 6
AND Node 7 (same semantics each time: queue.json is missing or does not
contain a shotId that appears in the upstream manifest). Node 6 and
Node 7's CLIs catch `(NodeNError, QueueLookupError)` explicitly rather
than introducing a parallel class name per node.

Deliberately mirrors the locked "fail fast" design decision: for Node 2,
any error raised aborts the entire batch. Node 3 follows the same
fail-fast rule for unrecoverable errors (FFmpegError, QueueInputError)
but emits a non-fatal warning record (not an exception) when a shot's
actual frame count differs from `durationFrames` in metadata — the
operator sees the warning and can decide whether to fix the MP4 or
accept the drift. Node 4 also fail-fasts on I/O or decode errors;
the key-pose/held-frame partition itself is data (no warnings needed
— every frame lands in exactly one key-pose group). Node 5 fail-fasts
on I/O and manifest errors, but count-mismatches between detection and
metadata are a reconcile-and-warn flow (not exceptions) — the "warn
AND reconcile" locked decision. Node 6 fail-fasts on I/O, manifest,
and sheet-format errors; per-key-pose angle matching itself is data
(score breakdowns in reference_map.json, no exceptions).
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


# -------------------------------------------------------------------
# Node 5 — Character Detection & Position
# -------------------------------------------------------------------

class Node5Error(PipelineError):
    """Base class for all Node 5 character-detection failures."""


class Node4ResultInputError(Node5Error):
    """node4_result.json (from Node 4) is missing, malformed, or
    references a keyposes folder that no longer exists on disk.

    Distinct from Node 4's Node3ResultInputError so the operator can
    tell "Node 4 never ran" from "Node 5 couldn't consume what Node 4
    wrote".
    """


class QueueLookupError(Node5Error):
    """queue.json (from Node 2) is missing or does not contain a shotId
    that appears in node4_result.json.

    Happens when queue.json and node4_result.json are from different
    runs (stale state) or when Node 2 was re-run after Node 4 with a
    mutated shot list.
    """


class CharacterDetectionError(Node5Error):
    """A key-pose PNG could not be read, decoded, or analyzed during
    connected-component detection — e.g. the file is truncated, Pillow
    rejected it, or the scipy/numpy path raised on malformed data.

    Count mismatches between detection and metadata are NOT this class
    (they are reconcile-and-warn records in node5_result.json).
    """


# -------------------------------------------------------------------
# Node 6 — Character Reference Sheet Matching
# -------------------------------------------------------------------

class Node6Error(PipelineError):
    """Base class for all Node 6 reference-matching failures."""


class Node5ResultInputError(Node6Error):
    """node5_result.json (from Node 5) is missing, malformed, or
    references a per-shot character_map.json / keyposes folder that
    no longer exists on disk.

    Distinct from Node 5's Node4ResultInputError so the operator can
    tell "Node 5 never ran" from "Node 6 couldn't consume what Node 5
    wrote".
    """


class CharactersInputError(Node6Error):
    """characters.json is missing, unreadable, or fails schema
    validation at Node 6 invocation time.

    Node 2 already validated characters.json before writing queue.json,
    so a fresh Node 2 -> Node 6 pipeline run should never trip this —
    but an operator invoking Node 6 directly with a hand-edited or
    stale characters.json will land here.
    """


class AngleOrderUnconfirmedError(CharactersInputError):
    """`characters.json.conventions.angleOrderConfirmed` is False.

    The canonical 8-angle order was locked on 2026-04-23. A False flag
    means the operator either forked the template to a non-canonical
    layout OR hand-edited the file incorrectly. Node 6 will not silently
    proceed against an unconfirmed layout because the slice->angle
    assignment depends on that order being exactly:

        back, back-3q-L, profile-L, front-3q-L,
        front, front-3q-R, profile-R, back-3q-R

    The operator should confirm the sheet's layout matches this order
    and flip the flag (or re-download from the Character Library page).
    """


class ReferenceSheetFormatError(Node6Error):
    """A reference sheet PNG is not in the expected format — i.e. it
    has no alpha channel, so alpha-island slicing cannot run.

    Node 1's Character Library page warns on upload if a sheet doesn't
    have transparent background; Node 2 does not re-verify (it only
    checks file existence). Node 6 is the first hard gate on sheet
    format, so the message names the file and tells the operator to
    re-export with a transparent background.
    """


class ReferenceSheetSliceError(Node6Error):
    """Alpha-island bbox slicing of a reference sheet did not produce
    exactly 8 islands.

    Usually means: sheet isn't the canonical 8-angle layout, OR a
    character's silhouette has a floating detail the alpha-island
    labeller counted as a separate island (e.g. a separate eye blob).
    The operator fixes the sheet PNG or flattens the detail into the
    main body.
    """


class AngleMatchingError(Node6Error):
    """A key-pose crop failed to recompute a silhouette, or the
    classical multi-signal score path raised on malformed data.

    Distinct from ReferenceSheet* errors because the culprit is the
    DETECTION side (Node 5 gave us a bbox with no ink in it, e.g.
    reconcile-failed produced a phantom detection) rather than the
    reference-sheet side.
    """


# -------------------------------------------------------------------
# Node 7 - AI-Powered Pose Refinement
# -------------------------------------------------------------------

class Node7Error(PipelineError):
    """Base class for all Node 7 pose-refinement failures.

    Node 7 is ComfyUI workflow JSON + thin custom-node wrapper (locked
    decision #9): the exception surface therefore covers manifest I/O,
    the ComfyUI HTTP bridge, and the per-detection generation record --
    but NOT the generation itself (that's ComfyUI's own error domain).
    """


class Node6ResultInputError(Node7Error):
    """node6_result.json (from Node 6) is missing, malformed, or
    references a per-shot reference_map.json / reference_crops folder
    that no longer exists on disk.

    Distinct from Node 6's Node5ResultInputError so the operator can
    tell "Node 6 never ran" from "Node 7 couldn't consume what Node 6
    wrote".
    """


class WorkflowTemplateError(Node7Error):
    """custom_nodes/node_07_pose_refiner/workflow.json (or the lineart
    fallback variant) is missing, unreadable, or lacks the placeholders
    the orchestrator expects to parameterize per detection.

    Usually means the workflow file was hand-edited in the ComfyUI web
    UI in a way that renamed required nodes, or the symlink on the pod
    points at the wrong revision. The message names the missing
    placeholder so the operator can fix the graph.
    """


class ComfyUIConnectionError(Node7Error):
    """The ComfyUI HTTP API at the configured URL is not reachable, or
    responded with a non-2xx status to a workflow submission.

    Distinct from RefinementGenerationError because the remedy is
    operational, not a data fix: check that ComfyUI is running on the
    pod (`ps aux | grep main.py`), that port 8188 is reachable, and
    that runpod_setup.sh finished without errors.
    """


class RefinementGenerationError(Node7Error):
    """A specific (shotId, keyPoseIndex, identity) generation did not
    produce a usable refined PNG.

    Raised on: ComfyUI reporting a workflow-execution error for that
    prompt, the expected output file not appearing on disk, or the
    output file failing post-processing (empty alpha, wrong dimensions,
    etc.). The message names the exact triple so the operator can pull
    the seed from node7_result.json and re-run.
    """


# -------------------------------------------------------------------
# Node 8 - Scene Assembly
# -------------------------------------------------------------------

class Node8Error(PipelineError):
    """Base class for all Node 8 scene-assembly failures.

    Node 8 takes Node 7's per-character refined PNGs and composites
    them onto a single source-MP4-resolution frame per key pose. Most
    runtime issues (Node 7 marked a generation as errored, a refined
    PNG is empty, etc.) are handled by the substitute-rough fallback
    and surface as warnings in `composed_map.json`, not as exceptions
    -- so this hierarchy only fires on hard I/O / contract problems.
    """


class Node7ResultInputError(Node8Error):
    """node7_result.json (from Node 7) is missing, malformed, or
    references a per-shot refined_map.json that no longer exists on
    disk.

    Distinct from Node 7's Node6ResultInputError so the operator can
    tell "Node 7 never ran" from "Node 8 couldn't consume what Node 7
    wrote".
    """


class RefinedPngError(Node8Error):
    """A refined PNG slot is unfillable: the Node-7 refined PNG can't
    be decoded / is empty AND the rough key-pose source frame is also
    missing or unreadable, so the substitute-rough fallback also fails.

    Only raised when BOTH the refined PNG and the rough fallback are
    dead. If just the refined is bad, Node 8 substitutes the rough
    silently and appends a warning to composed_map.json (warn-and-
    reconcile, locked decision #7).
    """


class CompositingError(Node8Error):
    """PIL / numpy raised an unexpected exception while building or
    saving a composed frame (out-of-memory, disk full, paste at an
    impossible offset, etc.).

    Distinct from RefinedPngError because the cause is not a missing
    input -- the inputs loaded fine, the math just blew up. Usually
    indicates an environmental problem rather than a data one.
    """


# -------------------------------------------------------------------
# Node 9 - Timing Reconstruction
# -------------------------------------------------------------------

class Node9Error(PipelineError):
    """Base class for all Node 9 timing-reconstruction failures.

    Node 9 takes Node 8's per-key-pose composites + Node 4's per-frame
    timing map and rebuilds the full per-frame sequence. Almost every
    failure mode is a hard contract violation (missing composite,
    invalid keypose_map, totalFrames mismatch) -- there's no
    warn-and-reconcile fallback like Node 5 / Node 8 because Node 9
    has no meaningful substitute for the refined-key-pose anchor.
    """


class Node8ResultInputError(Node9Error):
    """node8_result.json (from Node 8) is missing, malformed, or
    references a per-shot composed_map.json that no longer exists on
    disk.

    Distinct from Node 8's Node7ResultInputError so the operator can
    tell "Node 8 never ran" from "Node 9 couldn't consume what Node 8
    wrote".
    """


class KeyPoseMapInputError(Node9Error):
    """keypose_map.json (Node 4D output, sibling to composed_map.json)
    is missing, malformed, or its data violates Node 4's per-frame
    invariants:

    - every frame index in [1, totalFrames] must appear in exactly
      one keyPose's anchor (sourceFrame) or heldFrames list,
    - no frame index may appear in two keyPoses,
    - no offset may be missing or non-2-int.

    Different from Node 8ResultInputError because keypose_map.json is
    NOT something Node 8 writes -- it's the upstream Node 4 output
    that Node 9 chases via the shot-root convention. So "Node 8 ran
    fine but Node 4's manifest is stale or hand-edited" is its own
    failure mode worth distinguishing.
    """


class TimingReconstructionError(Node9Error):
    """A frame can't be reconstructed because its source composed PNG
    (Node 8's output) is missing, unreadable, or has unexpected dims.

    Locked decision #7: NO substitute-rough fallback. Node 9 has no
    meaningful substitute -- Node 8's composite IS the refined source
    of truth, and silently substituting the rough would downgrade the
    output. The message names the (shotId, keyPoseIndex) so the
    operator can re-run Node 8 for just the affected shot.
    """


class FrameCountMismatchError(Node9Error):
    """The reconstructed PNG count for a shot disagrees with
    keypose_map.json's totalFrames.

    Indicates a Node 4 invariant violation upstream (e.g., a frame
    index outside [1, totalFrames] or a hole in the per-frame
    coverage). Hard error; no warn-and-reconcile because silent
    drift here would corrupt Node 10's MP4 timing.
    """


# -------------------------------------------------------------------
# Node 10 - Output Generation (PNG -> MP4)
# -------------------------------------------------------------------

class Node10Error(PipelineError):
    """Base class for all Node 10 encoding failures.

    Node 10 is the simplest node algorithmically (one ffmpeg
    invocation per shot) but the test surface includes real
    subprocess ffmpeg + imageio_ffmpeg.count_frames_and_secs
    verification, so failure modes split cleanly into
    'inputs are bad' (Node9ResultInputError, TimedFramesError) vs
    'encode itself blew up' (FFmpegEncodeError).
    """


class Node9ResultInputError(Node10Error):
    """node9_result.json (from Node 9) is missing, malformed, or
    references a per-shot timed_map.json that no longer exists on
    disk.

    Distinct from Node 9's Node8ResultInputError so the operator can
    tell "Node 9 never ran" from "Node 10 couldn't consume what Node 9
    wrote".
    """


class TimedFramesError(Node10Error):
    """A shot's <shot>/timed/ directory is missing PNG files in
    1..totalFrames range, or has a hole in the contiguous numbering.

    Node 9's invariant: every frame in 1..totalFrames is on disk.
    A hole means an upstream bug; refuse to encode rather than
    produce a short MP4.
    """


class FFmpegEncodeError(Node10Error):
    """ffmpeg encode or post-encode verification failed.

    Covers four cases:
      1. Source frames have odd canvas dimensions (libx264 requires
         even W/H; auto-padding would silently desync Node 9
         positions).
      2. ffmpeg subprocess returned a non-zero exit code (last 10
         stderr lines attached to the message).
      3. The output MP4 is missing or zero-bytes after a 'successful'
         encode (silent corruption).
      4. imageio_ffmpeg.count_frames_and_secs reports a frame count
         that doesn't match the input PNG count (encoder dropout).
    """


# -------------------------------------------------------------------
# Node 11 - Batch Management
# -------------------------------------------------------------------

class Node11Error(PipelineError):
    """Base class for all Node 11 batch-management failures.

    Node 11 is the project-level orchestrator that runs Nodes 2-10
    via subprocess and aggregates per-shot status. Failure modes
    split into three flavors: bad inputs (InputDirError), a specific
    downstream node failing after all retries (NodeStepError), and a
    100% per-shot failure rate after the run (BatchAllFailedError).
    The partial-success case (some shots ok, some failed) is NOT a
    failure -- it's reported via failedCount in node11_result.json
    and the CLI exits 0.
    """


class InputDirError(Node11Error):
    """--input-dir is missing, not a directory, or doesn't contain
    the files Node 2 will need (metadata.json + characters.json
    minimally).

    Pre-flight check; raised before any downstream node runs so the
    operator gets immediate feedback on a typo'd path or wrong dir.
    """


class NodeStepError(Node11Error):
    """A specific downstream node's subprocess returned a non-zero
    exit code on EVERY attempt (initial + all configured retries).

    Carries:
      - node number (2..10)
      - final exit code
      - attempt count (1 + retries)
      - last 10 stderr lines from the final attempt

    Distinct from BatchAllFailedError because one node failing
    early (e.g., Node 2) means we couldn't even ATTEMPT per-shot
    work -- there's no partial-success story to tell. Whereas
    BatchAllFailedError fires when nodes ran but no shot reached
    the final MP4 deliverable.
    """


class BatchAllFailedError(Node11Error):
    """All nodes ran without subprocess errors but no shot produced
    a final MP4 deliverable in <work-dir>/output/. Indicates a 100%
    failure rate at the per-shot level.

    Concretely happens when, e.g., every shot's Node 7 generations
    were marked status=error (and Node 8's substitute-rough
    fallback then itself failed) so Node 10 never got a real
    sequence to encode. The exit code is 1 because something is
    fundamentally broken (wrong inputs, GPU down, bad weights, etc.)
    that needs operator intervention -- it's NOT a partial-success
    situation that the operator might want to ignore.
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
    # Node 5
    "Node5Error",
    "Node4ResultInputError",
    "QueueLookupError",
    "CharacterDetectionError",
    # Node 6
    "Node6Error",
    "Node5ResultInputError",
    "CharactersInputError",
    "AngleOrderUnconfirmedError",
    "ReferenceSheetFormatError",
    "ReferenceSheetSliceError",
    "AngleMatchingError",
    # Node 7
    "Node7Error",
    "Node6ResultInputError",
    "WorkflowTemplateError",
    "ComfyUIConnectionError",
    "RefinementGenerationError",
    # Node 8
    "Node8Error",
    "Node7ResultInputError",
    "RefinedPngError",
    "CompositingError",
    # Node 9
    "Node9Error",
    "Node8ResultInputError",
    "KeyPoseMapInputError",
    "TimingReconstructionError",
    "FrameCountMismatchError",
    # Node 10
    "Node10Error",
    "Node9ResultInputError",
    "TimedFramesError",
    "FFmpegEncodeError",
    # Node 11
    "Node11Error",
    "InputDirError",
    "NodeStepError",
    "BatchAllFailedError",
]
