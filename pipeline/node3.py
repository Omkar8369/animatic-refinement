"""Node 3 — Shot Pre-processing (MP4 -> PNG).

Reads `queue.json` produced by Node 2, decodes each shot's rough-animatic
MP4 into a 1:1 sequence of PNG frames (no resampling, no frame-rate
conversion), writes a per-shot `_manifest.json`, and a top-level
`node3_result.json` aggregating every shot plus any non-fatal warnings.

Sub-steps (aligned with docs/PLAN.md Node 3):
  3A. Load + validate queue.json (structure, absolute MP4 paths exist).
  3B. Per shot: create `<work-dir>/<shotId>/` folder.
  3C. Per shot: invoke ffmpeg to decode MP4 -> `frame_NNNN.png`.
  3D. Per shot: count actual frames, compare to `durationFrames` from
      metadata. On mismatch, emit a structured warning (non-fatal).
  3E. Write per-shot `_manifest.json` and top-level `node3_result.json`.

Design decisions (locked 2026-04-23):
  * ffmpeg binary comes from `imageio-ffmpeg` (pip wheel bundles the
    binary). No system-level ffmpeg dependency. Identical on Windows
    embedded Python, RunPod Linux, and CI.
  * Per-shot folders: `<work-dir>/<shotId>/frame_NNNN.png` (NNNN zero-
    padded to 4 digits, 1-indexed so `frame_0001.png` = first frame).
  * Fail fast on ffmpeg errors, queue.json problems, or missing MP4s.
    Warn-and-continue on frame-count mismatches (the core-logic lives
    here, not in the CLI — that way the ComfyUI wrapper inherits the
    same behavior).
  * Pure Python. No GPU imports. Decoding is CPU-bound and runs in
    sub-second per short shot.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import (
    FFmpegError,
    FrameExtractionError,
    QueueInputError,
)


# -------------------------------------------------------------------
# Public result types
# -------------------------------------------------------------------

@dataclass(frozen=True)
class FrameCountWarning:
    """Non-fatal drift between metadata.json durationFrames and the
    actual frame count decoded from the MP4. Surfaced to the operator
    via node3_result.json; does not abort the batch."""
    shotId: str
    expectedFrames: int
    actualFrames: int
    message: str


@dataclass(frozen=True)
class ShotFrameResult:
    """Summary of one shot's extraction.

    `framesDir` is the folder that contains the PNG sequence.
    `frameFilenames` is sorted ascending ("frame_0001.png", ...).
    """
    shotId: str
    mp4Path: str
    framesDir: str
    expectedFrames: int
    actualFrames: int
    frameFilenames: list[str]


@dataclass
class Node3Result:
    """Aggregate result of Node 3 across every shot in the queue.

    Written to `<work-dir>/node3_result.json` for Node 4 to consume.
    """
    schemaVersion: int = 1
    projectName: str = ""
    workDir: str = ""
    ffmpegBinary: str = ""
    extractedAt: str = ""
    shots: list[ShotFrameResult] = field(default_factory=list)
    warnings: list[FrameCountWarning] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "projectName": self.projectName,
            "workDir": self.workDir,
            "ffmpegBinary": self.ffmpegBinary,
            "extractedAt": self.extractedAt,
            "shots": [asdict(s) for s in self.shots],
            "warnings": [asdict(w) for w in self.warnings],
        }


# -------------------------------------------------------------------
# ffmpeg binary resolution
# -------------------------------------------------------------------

def _resolve_ffmpeg_binary() -> str:
    """Return the absolute path to a usable ffmpeg executable.

    Uses `imageio-ffmpeg`'s bundled binary so we don't depend on the
    operator having ffmpeg on PATH — identical behavior on Windows
    embedded Python, RunPod Linux, and CI.
    """
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover - caught by requirements.txt
        raise FFmpegError(
            "imageio-ffmpeg is not installed. Install with:\n"
            "  pip install imageio-ffmpeg"
        ) from e
    return imageio_ffmpeg.get_ffmpeg_exe()


# -------------------------------------------------------------------
# Public entrypoint
# -------------------------------------------------------------------

def extract_frames_for_queue(
    queue_path: Path | str,
    work_dir: Path | str,
) -> Node3Result:
    """Extract frames for every shot listed in queue.json.

    Args:
        queue_path: Path to queue.json (Node 2's output).
        work_dir:   Folder to write per-shot frame folders and the
                    aggregate node3_result.json. Created if missing.

    Returns:
        Node3Result — success if no exception raised. Warnings (e.g.
        frame-count drift) appear in `.warnings`; they do NOT abort.

    Raises:
        QueueInputError: queue.json missing, unreadable, or malformed.
        FFmpegError:     an ffmpeg invocation failed or produced no frames.
        FrameExtractionError: post-ffmpeg folder state is inconsistent.
    """
    queue_path = Path(queue_path).resolve()
    work_dir = Path(work_dir).resolve()

    queue = _load_queue(queue_path)
    work_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin = _resolve_ffmpeg_binary()

    result = Node3Result(
        projectName=queue.get("projectName", ""),
        workDir=str(work_dir),
        ffmpegBinary=ffmpeg_bin,
        extractedAt=datetime.now(timezone.utc).isoformat(),
    )

    # Flatten the queue's batched shape into a flat list for this node —
    # Node 3 processes every shot identically, batching is Node 11's job.
    for batch in queue.get("batches", []):
        for shot in batch:
            shot_result, warning = extract_frames_for_shot(
                mp4_path=Path(shot["mp4Path"]),
                out_dir=work_dir / shot["shotId"],
                expected_frames=int(shot["durationFrames"]),
                shot_id=shot["shotId"],
                ffmpeg_bin=ffmpeg_bin,
            )
            result.shots.append(shot_result)
            if warning is not None:
                result.warnings.append(warning)

    # Top-level aggregate manifest.
    (work_dir / "node3_result.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


def extract_frames_for_shot(
    mp4_path: Path,
    out_dir: Path,
    expected_frames: int,
    shot_id: str,
    ffmpeg_bin: str | None = None,
) -> tuple[ShotFrameResult, FrameCountWarning | None]:
    """Decode one MP4 into `out_dir/frame_NNNN.png`.

    Also writes `out_dir/_manifest.json` so a downstream node (or a
    human) can inspect one shot's extraction state without loading the
    aggregate node3_result.json.

    Returns `(ShotFrameResult, warning_or_None)` — the caller decides
    whether to record the warning at the batch level.
    """
    mp4_path = Path(mp4_path).resolve()
    out_dir = Path(out_dir).resolve()

    if not mp4_path.is_file():
        raise QueueInputError(
            f"{shot_id}: MP4 path from queue.json does not exist: {mp4_path}"
        )

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise FrameExtractionError(
            f"{shot_id}: could not create frames folder: {out_dir} ({e})"
        ) from e

    # Clear any stale frames from a previous partial run so the manifest
    # we write matches the directory contents exactly.
    for stale in out_dir.glob("frame_*.png"):
        try:
            stale.unlink()
        except OSError:  # pragma: no cover
            pass

    if ffmpeg_bin is None:
        ffmpeg_bin = _resolve_ffmpeg_binary()

    _run_ffmpeg(mp4_path, out_dir, shot_id, ffmpeg_bin)

    frame_names = sorted(p.name for p in out_dir.glob("frame_*.png"))
    if not frame_names:
        raise FFmpegError(
            f"{shot_id}: ffmpeg produced zero frames from {mp4_path}. "
            "The MP4 may be empty, corrupt, or lack a video stream."
        )

    # Verify contiguous 1-indexed numbering.
    _verify_frame_sequence(frame_names, shot_id)

    actual = len(frame_names)
    warning: FrameCountWarning | None = None
    if actual != expected_frames:
        warning = FrameCountWarning(
            shotId=shot_id,
            expectedFrames=expected_frames,
            actualFrames=actual,
            message=(
                f"{shot_id}: decoded {actual} frame(s) but metadata.json "
                f"declared durationFrames={expected_frames}. "
                "Node 9 will use the actual count; verify the MP4 if this "
                "is unexpected."
            ),
        )

    shot_result = ShotFrameResult(
        shotId=shot_id,
        mp4Path=str(mp4_path),
        framesDir=str(out_dir),
        expectedFrames=expected_frames,
        actualFrames=actual,
        frameFilenames=frame_names,
    )

    # Per-shot manifest.
    manifest = {
        "schemaVersion": 1,
        "shotId": shot_id,
        "mp4Path": str(mp4_path),
        "framesDir": str(out_dir),
        "expectedFrames": expected_frames,
        "actualFrames": actual,
        "frameFilenames": frame_names,
        "ffmpegBinary": ffmpeg_bin,
        "extractedAt": datetime.now(timezone.utc).isoformat(),
        "warning": asdict(warning) if warning is not None else None,
    }
    (out_dir / "_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return shot_result, warning


# -------------------------------------------------------------------
# Sub-step internals
# -------------------------------------------------------------------

def _load_queue(queue_path: Path) -> dict[str, Any]:
    """3A: Load queue.json and sanity-check its top-level shape."""
    if not queue_path.is_file():
        raise QueueInputError(
            f"queue.json not found at {queue_path}. "
            "Run Node 2 first to produce it."
        )
    try:
        raw = json.loads(queue_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise QueueInputError(
            f"queue.json at {queue_path} is not valid JSON: {e}"
        ) from e

    if not isinstance(raw, dict):
        raise QueueInputError(
            f"queue.json at {queue_path} must be a JSON object; "
            f"got {type(raw).__name__}."
        )
    if "batches" not in raw or not isinstance(raw["batches"], list):
        raise QueueInputError(
            f"queue.json at {queue_path} is missing a 'batches' list. "
            "Did Node 2 complete successfully?"
        )
    # Schema-version guard — if Node 2's contract ever changes we want a
    # loud failure here, not a silent half-run.
    if raw.get("schemaVersion") != 1:
        raise QueueInputError(
            f"queue.json at {queue_path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 3 expects 1."
        )

    # Verify each shot has the fields we'll pull, and its MP4 exists on disk.
    seen_ids: set[str] = set()
    for b_idx, batch in enumerate(raw["batches"]):
        if not isinstance(batch, list):
            raise QueueInputError(
                f"queue.json: batches[{b_idx}] is not a list."
            )
        for s_idx, shot in enumerate(batch):
            if not isinstance(shot, dict):
                raise QueueInputError(
                    f"queue.json: batches[{b_idx}][{s_idx}] is not an object."
                )
            for key in ("shotId", "mp4Path", "durationFrames"):
                if key not in shot:
                    raise QueueInputError(
                        f"queue.json: batches[{b_idx}][{s_idx}] missing '{key}'."
                    )
            if shot["shotId"] in seen_ids:
                raise QueueInputError(
                    f"queue.json: duplicate shotId {shot['shotId']!r} "
                    "(Node 2 should have caught this)."
                )
            seen_ids.add(shot["shotId"])
    return raw


def _run_ffmpeg(
    mp4_path: Path,
    out_dir: Path,
    shot_id: str,
    ffmpeg_bin: str,
) -> None:
    """3C: Invoke ffmpeg to decode MP4 -> frame_NNNN.png.

    Flags:
      -y                  overwrite any stale file without prompting
      -i <mp4>            input MP4
      -start_number 1     first frame is frame_0001.png (1-indexed)
      -vsync 0            one decoded frame -> one output file; no drop/dup
      -hide_banner        quieter logs
      -loglevel error     only print actual errors

    We don't pass -r (frame rate) — the MP4 is already at 25 FPS per
    locked convention, and any -r would resample. Node 3's contract is
    "1:1 decode, nothing else."
    """
    output_pattern = str(out_dir / "frame_%04d.png")
    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(mp4_path),
        "-start_number", "1",
        "-vsync", "0",
        output_pattern,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise FFmpegError(
            f"{shot_id}: failed to spawn ffmpeg ({ffmpeg_bin}): {e}"
        ) from e

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-10:]
        raise FFmpegError(
            f"{shot_id}: ffmpeg exited {proc.returncode} on {mp4_path}.\n"
            f"stderr tail:\n  " + "\n  ".join(tail)
        )


_FRAME_NAME_RE = re.compile(r"^frame_(\d{4,})\.png$")


def _verify_frame_sequence(frame_names: list[str], shot_id: str) -> None:
    """3D guard: frames must be 1..N with no gaps.

    `frame_names` must already be sorted ascending.
    """
    indices: list[int] = []
    for name in frame_names:
        m = _FRAME_NAME_RE.match(name)
        if not m:
            raise FrameExtractionError(
                f"{shot_id}: unexpected file in frames folder: {name}"
            )
        indices.append(int(m.group(1)))
    if indices[0] != 1:
        raise FrameExtractionError(
            f"{shot_id}: frame numbering starts at {indices[0]}, expected 1."
        )
    expected = list(range(1, len(indices) + 1))
    if indices != expected:
        missing = sorted(set(expected) - set(indices))
        raise FrameExtractionError(
            f"{shot_id}: frame numbering has gaps; missing indices: {missing[:10]}"
            + (" ..." if len(missing) > 10 else "")
        )


__all__ = [
    "Node3Result",
    "ShotFrameResult",
    "FrameCountWarning",
    "extract_frames_for_queue",
    "extract_frames_for_shot",
]
