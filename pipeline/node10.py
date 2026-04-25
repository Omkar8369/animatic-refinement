"""Node 10 - Output Generation (PNG -> MP4).

Encodes each shot's full per-frame PNG sequence (from Node 9) into a
single deliverable MP4 at 25 FPS using ffmpeg via imageio-ffmpeg's
static binary. Output goes to a project-level `output/` directory so
all deliverables collect in one place for client hand-off.

Locked decisions (do not re-litigate without updating CLAUDE.md):

1.  ffmpeg via imageio-ffmpeg static binary, NOT system ffmpeg.
2.  Codec = H.264 (libx264).
3.  Pixel format = yuv420p.
4.  Quality = CRF 18 (visually lossless on BnW line art); exposed
    via --crf for tighter file-size budgets.
5.  Preset = medium (libx264 default).
6.  Frame rate = 25 (locked project convention; no --fps flag).
7.  Output location = <work-dir>/output/<shotId>_refined.mp4.
8.  Filename pattern = <shotId>_refined.mp4.
9.  Post-encode verification via imageio_ffmpeg.count_frames_and_secs
    (catches silent ffmpeg corruption: exit 0 but malformed file).
10. Do NOT delete upstream artifacts.
11. Odd canvas dimensions are a hard error (libx264 requires even
    W/H; auto-padding would silently desync Node 9 positions).
12. ffmpeg non-zero exit raises FFmpegEncodeError with last 10
    stderr lines.
13. Missing PNG in 1..N gap raises TimedFramesError.
14. nb_frames mismatch after encode raises FFmpegEncodeError.
15. CLI inputs: --node9-result <path> only (chases pointers).
16. Architecture template = same as Nodes 3-6/8/9. Pure-Python.
17. Rerun safety: ffmpeg -y overwrites output MP4.

Inputs:
  * `node9_result.json` -- Node 9's aggregate. Points at each shot's
    `timed_map.json` (which carries timedDir + totalFrames).

Outputs:
  * `<work-dir>/output/<shotId>_refined.mp4` -- H.264, yuv420p, 25
    FPS, CRF 18 by default.
  * `<work-dir>/node10_result.json` -- aggregate one-line summary
    per shot.

This module is GPU-agnostic and importable from:
  * `pipeline.cli_node10.main` (CLI)
  * `custom_nodes.node_10_png_to_mp4.__init__` (ComfyUI)
  * `tests/test_node10.py` (pytest)

All error paths raise a `pipeline.errors.Node10Error` subclass.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import imageio_ffmpeg
from PIL import Image

from pipeline.errors import (
    FFmpegEncodeError,
    Node9ResultInputError,
    TimedFramesError,
)


# -------------------------------------------------------------------
# Tunables (defaults; CLI / ComfyUI can override)
# -------------------------------------------------------------------

DEFAULT_CRF = 18
DEFAULT_FPS = 25  # Locked by project convention; not a CLI knob
DEFAULT_CODEC = "libx264"
DEFAULT_PIXEL_FORMAT = "yuv420p"
DEFAULT_PRESET = "medium"

CODEC_LABEL = "h264"  # what the encoder writes into the container
FILENAME_TEMPLATE = "{shotId}_refined.mp4"


# -------------------------------------------------------------------
# Public result types
# -------------------------------------------------------------------

@dataclass
class ShotEncodeSummary:
    """One-line aggregate per shot."""
    shotId: str
    outputPath: str
    frameCount: int
    durationSeconds: float
    codec: str
    fps: int
    fileSizeBytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "shotId": self.shotId,
            "outputPath": self.outputPath,
            "frameCount": self.frameCount,
            "durationSeconds": self.durationSeconds,
            "codec": self.codec,
            "fps": self.fps,
            "fileSizeBytes": self.fileSizeBytes,
        }


@dataclass
class Node10Result:
    """Aggregate Node 10 result. Written to
    `<work-dir>/node10_result.json`."""
    schemaVersion: int = 1
    projectName: str = ""
    workDir: str = ""
    outputDir: str = ""
    crf: int = DEFAULT_CRF
    encodedAt: str = ""
    shots: list[ShotEncodeSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "projectName": self.projectName,
            "workDir": self.workDir,
            "outputDir": self.outputDir,
            "crf": self.crf,
            "encodedAt": self.encodedAt,
            "shots": [s.to_dict() for s in self.shots],
        }


# -------------------------------------------------------------------
# 10A - Input validation
# -------------------------------------------------------------------

def load_node9_result(path: Path) -> dict[str, Any]:
    """Load and minimally validate node9_result.json.

    Raises:
        Node9ResultInputError: missing, not JSON, wrong shape, or
            wrong schemaVersion.
    """
    if not path.is_file():
        raise Node9ResultInputError(
            f"node9_result.json not found at {path}. Run Node 9 first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node9ResultInputError(
            f"node9_result.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node9ResultInputError(
            f"node9_result.json at {path} must be a JSON object."
        )
    if raw.get("schemaVersion") != 1:
        raise Node9ResultInputError(
            f"node9_result.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 10 expects 1."
        )
    for key in ("workDir", "shots"):
        if key not in raw:
            raise Node9ResultInputError(
                f"node9_result.json at {path} missing required key '{key}'."
            )
    if not isinstance(raw["shots"], list):
        raise Node9ResultInputError(
            f"node9_result.json at {path}: 'shots' must be a list."
        )
    for s_idx, shot in enumerate(raw["shots"]):
        if not isinstance(shot, dict):
            raise Node9ResultInputError(
                f"node9_result.json: shots[{s_idx}] is not an object."
            )
        for key in ("shotId", "timedMapPath"):
            if key not in shot:
                raise Node9ResultInputError(
                    f"node9_result.json: shots[{s_idx}] missing '{key}'."
                )
    return raw


def load_timed_map(path: Path, shot_id: str) -> dict[str, Any]:
    """Load a shot's `timed_map.json` (Node 9 output).

    Raises:
        Node9ResultInputError: missing, malformed, or schema mismatch.
    """
    if not path.is_file():
        raise Node9ResultInputError(
            f"timed_map.json for shot {shot_id!r} not found at "
            f"{path}. Did Node 9 finish for this shot?"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node9ResultInputError(
            f"timed_map.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node9ResultInputError(
            f"timed_map.json at {path} must be a JSON object."
        )
    if raw.get("schemaVersion") != 1:
        raise Node9ResultInputError(
            f"timed_map.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 10 expects 1."
        )
    for key in ("shotId", "timedDir", "totalFrames"):
        if key not in raw:
            raise Node9ResultInputError(
                f"timed_map.json at {path} missing '{key}'."
            )
    if raw["shotId"] != shot_id:
        raise Node9ResultInputError(
            f"timed_map.json at {path} has shotId={raw['shotId']!r} "
            f"but node9_result.json said {shot_id!r}. Stale work dir?"
        )
    total = raw["totalFrames"]
    if not isinstance(total, int) or total < 1:
        raise Node9ResultInputError(
            f"timed_map.json at {path}: totalFrames must be a "
            f"positive int; got {total!r}."
        )
    return raw


def _verify_timed_frames(timed_dir: Path, expected: int, shot_id: str) -> None:
    """Confirm `<timed_dir>/frame_NNNN.png` exists for N in
    1..expected (no holes, no extras-that-matter).

    Raises:
        TimedFramesError: directory missing OR any expected frame
            absent.
    """
    if not timed_dir.is_dir():
        raise TimedFramesError(
            f"shot={shot_id!r}: timed/ directory not found at "
            f"{timed_dir}. Did Node 9 finish for this shot?"
        )
    missing: list[int] = []
    for i in range(1, expected + 1):
        if not (timed_dir / f"frame_{i:04d}.png").is_file():
            missing.append(i)
    if missing:
        # Truncate the message if many frames are missing.
        head = missing[:5]
        more = "" if len(missing) <= 5 else f" (+ {len(missing) - 5} more)"
        raise TimedFramesError(
            f"shot={shot_id!r}: {len(missing)} expected frame(s) "
            f"missing in {timed_dir}; first missing: "
            f"{['frame_%04d.png' % i for i in head]}{more}. "
            "Re-run Node 9."
        )


# -------------------------------------------------------------------
# 10B - Probe canvas dimensions (locked decision #11: odd dims fail loud)
# -------------------------------------------------------------------

def _probe_canvas_dims(timed_dir: Path, shot_id: str) -> tuple[int, int]:
    """Open `<timed_dir>/frame_0001.png`, return (W, H).

    Raises:
        FFmpegEncodeError: either dim is odd (libx264 requires even),
            or the probe target can't be decoded.
    """
    probe = timed_dir / "frame_0001.png"
    try:
        with Image.open(probe) as img:
            w, h = img.size
    except Exception as e:  # noqa: BLE001
        raise FFmpegEncodeError(
            f"shot={shot_id!r}: failed to probe dims from {probe}: "
            f"{type(e).__name__}: {e}"
        ) from e
    if w % 2 != 0 or h % 2 != 0:
        raise FFmpegEncodeError(
            f"shot={shot_id!r}: canvas dims {w}x{h} have an odd "
            f"side; libx264 requires even W and H. Source MP4 was "
            "odd-dimensioned -- re-encode it with even dimensions "
            "before re-running Node 3 (auto-padding here would shift "
            "every character by half a pixel and silently desync "
            "Node 9's translate-and-copy positions)."
        )
    return w, h


# -------------------------------------------------------------------
# 10C - ffmpeg encode
# -------------------------------------------------------------------

def _ffmpeg_encode(
    timed_dir: Path,
    output_path: Path,
    crf: int,
    fps: int,
    shot_id: str,
) -> None:
    """Invoke ffmpeg to encode `<timed_dir>/frame_%04d.png` to
    `<output_path>` as H.264 / yuv420p / CRF / 25 FPS.

    Raises:
        FFmpegEncodeError: ffmpeg exits non-zero (last 10 stderr
            lines attached to the message).
    """
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-framerate", str(fps),
        "-i", str(timed_dir / "frame_%04d.png"),
        "-c:v", DEFAULT_CODEC,
        "-pix_fmt", DEFAULT_PIXEL_FORMAT,
        "-crf", str(crf),
        "-preset", DEFAULT_PRESET,
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        last_lines = "\n".join(proc.stderr.splitlines()[-10:]) or "(no stderr)"
        raise FFmpegEncodeError(
            f"shot={shot_id!r}: ffmpeg exit {proc.returncode}. "
            f"Last 10 stderr lines:\n{last_lines}"
        )


# -------------------------------------------------------------------
# 10D - ffprobe-style verification (via imageio_ffmpeg)
# -------------------------------------------------------------------

def _verify_output(
    output_path: Path,
    expected_frames: int,
    expected_fps: int,
    shot_id: str,
) -> dict[str, Any]:
    """Verify the encoded MP4 is non-empty and has the expected
    frame count + duration.

    Note: imageio-ffmpeg ships only ffmpeg (NOT ffprobe), so we use
    `imageio_ffmpeg.count_frames_and_secs` (which decodes the file
    using the bundled ffmpeg binary) instead of a separate ffprobe
    call. Codec verification is implicit: we asked for libx264 with
    `-c:v libx264` and ffmpeg would have errored at encode time if
    it couldn't honor that flag.

    Returns:
        Dict with `frameCount`, `durationSeconds`, `fileSizeBytes`.

    Raises:
        FFmpegEncodeError: any verification check fails.
    """
    if not output_path.is_file():
        raise FFmpegEncodeError(
            f"shot={shot_id!r}: ffmpeg exited 0 but output file "
            f"{output_path} is missing on disk."
        )
    file_size = output_path.stat().st_size
    if file_size == 0:
        raise FFmpegEncodeError(
            f"shot={shot_id!r}: output file {output_path} is empty "
            "(0 bytes) -- silent ffmpeg corruption?"
        )

    try:
        n_frames, n_secs = imageio_ffmpeg.count_frames_and_secs(
            str(output_path)
        )
    except Exception as e:  # noqa: BLE001
        raise FFmpegEncodeError(
            f"shot={shot_id!r}: post-encode frame-count probe of "
            f"{output_path} failed: {type(e).__name__}: {e}"
        ) from e

    if abs(n_frames - expected_frames) > 1:
        raise FFmpegEncodeError(
            f"shot={shot_id!r}: ffmpeg encoded {n_frames} frame(s) "
            f"but expected {expected_frames} (input PNG count). "
            "Encoder dropout?"
        )

    expected_secs = expected_frames / expected_fps
    if abs(n_secs - expected_secs) > 0.5:
        raise FFmpegEncodeError(
            f"shot={shot_id!r}: output duration {n_secs:.3f}s but "
            f"expected ~{expected_secs:.3f}s ({expected_frames} "
            f"frames @ {expected_fps} fps). Frame-rate mismatch?"
        )

    return {
        "frameCount": int(n_frames),
        "durationSeconds": float(n_secs),
        "fileSizeBytes": int(file_size),
    }


# -------------------------------------------------------------------
# Top-level driver
# -------------------------------------------------------------------

def encode_for_queue(
    *,
    node9_result_path: Path,
    crf: int = DEFAULT_CRF,
) -> Node10Result:
    """Drive Node 10 across every shot in `node9_result.json`.

    For each shot:
      * load `timed_map.json` (Node 9 output) via the path in
        node9_result.json
      * verify all `1..totalFrames` PNGs exist in `<shot>/timed/`
      * probe canvas dims (fail-loud on odd)
      * ffmpeg encode -> `<work-dir>/output/<shotId>_refined.mp4`
      * verify output (file exists, non-empty, frame count matches,
        duration matches)
    Then write the aggregate `<work-dir>/node10_result.json`.

    Args:
        node9_result_path: path to Node 9's aggregate manifest.
        crf: H.264 CRF value (default 18). Lower = higher quality
            and bigger files.

    Returns:
        Node10Result describing what was encoded.

    Raises:
        Node9ResultInputError: malformed/missing Node 9 manifest.
        TimedFramesError: a shot has missing/holey PNGs in 1..N.
        FFmpegEncodeError: encode failed, output corrupt, or odd
            canvas dimensions.
    """
    n9_path = Path(node9_result_path)
    n9 = load_node9_result(n9_path)
    work_dir = Path(n9["workDir"])
    output_dir = work_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[ShotEncodeSummary] = []
    for shot in n9["shots"]:
        shot_id = shot["shotId"]
        timed_map_path = Path(shot["timedMapPath"])
        timed_map = load_timed_map(timed_map_path, shot_id)
        timed_dir = Path(timed_map["timedDir"])
        expected_frames = int(timed_map["totalFrames"])

        # 10A - frame existence verification
        _verify_timed_frames(timed_dir, expected_frames, shot_id)

        # 10B - dim probe + odd-dim fail-loud
        _probe_canvas_dims(timed_dir, shot_id)

        # 10C - ffmpeg encode
        output_path = output_dir / FILENAME_TEMPLATE.format(shotId=shot_id)
        _ffmpeg_encode(
            timed_dir=timed_dir,
            output_path=output_path,
            crf=crf,
            fps=DEFAULT_FPS,
            shot_id=shot_id,
        )

        # 10D - post-encode verification
        verify = _verify_output(
            output_path=output_path,
            expected_frames=expected_frames,
            expected_fps=DEFAULT_FPS,
            shot_id=shot_id,
        )

        summaries.append(ShotEncodeSummary(
            shotId=shot_id,
            outputPath=str(output_path),
            frameCount=verify["frameCount"],
            durationSeconds=verify["durationSeconds"],
            codec=CODEC_LABEL,
            fps=DEFAULT_FPS,
            fileSizeBytes=verify["fileSizeBytes"],
        ))

    # 10E - aggregate
    result = Node10Result(
        schemaVersion=1,
        projectName=n9.get("projectName", ""),
        workDir=str(work_dir),
        outputDir=str(output_dir),
        crf=crf,
        encodedAt=datetime.now(timezone.utc).isoformat(),
        shots=summaries,
    )
    aggregate_path = work_dir / "node10_result.json"
    aggregate_path.write_text(
        json.dumps(result.to_dict(), indent=2),
        encoding="utf-8",
    )
    return result
