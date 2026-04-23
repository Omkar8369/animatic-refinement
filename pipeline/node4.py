"""Node 4 — Key Pose Extraction.

Reads `node3_result.json` and partitions each shot's PNG frame sequence
into **key poses** + **held-frame runs**. A "held" frame is one whose
content matches the current key-pose anchor *after translation
compensation* — so a character sliding L→R across 10 frames without
changing pose is recorded as ONE key pose with 10 held frames at
growing offsets, not as 10 separate key poses.

That shape is what Node 7 (AI pose refinement) and Node 9 (timing
reconstruction) need: refine ONCE per key pose, then replay the held
frames by copy-and-translate.

Sub-steps (aligned with `docs/PLAN.md` Node 4):

  4A. Load + validate `node3_result.json`.
  4B. Per shot: walk frames sequentially, comparing each to the current
      key-pose anchor via phase correlation + aligned MAE.
  4C. Per shot: when aligned MAE > threshold, emit a new key pose.
  4D. Per shot: copy key-pose frames to `<shotId>/keyposes/` and write
      `<shotId>/keypose_map.json`.
  4E. Write top-level `node4_result.json` (aggregate across shots).

Design decisions (locked 2026-04-23):

  * **Phase correlation via numpy FFT** for translation estimation,
    then **aligned pixel-diff (MAE on 0-255 grayscale)** for similarity
    scoring. A slide shot — same pose translated across the frame — is
    held with non-zero `offset`, not a run of new key poses.
  * **Downscaled comparison** (max edge = 128) for speed and
    encoder-noise tolerance. Offsets scaled back to full-resolution
    pixels on write, so Node 9 replays at the MP4's native resolution.
  * **Global MAE threshold** (`--threshold`, default 8.0). One number
    per run; no per-shot adaptive logic in this pass.
  * **No minimum hold-length filter.** Even a 1-frame segment between
    two key poses is recorded as its own 1-frame run.
  * **Key-pose PNGs are COPIED** (not renamed) to `<shotId>/keyposes/`
    preserving source filenames — Node 9 can cross-reference original
    frame numbers trivially.
  * **Core logic here; ComfyUI wrapper is a one-liner.** Same template
    as Node 3: `custom_nodes/node_04_keypose_extractor/` only does
    INPUT_TYPES + RETURN_TYPES + a call to `extract_keyposes_for_queue`.
  * **Single-threaded.** Shots are independent; parallelism is a future
    Node 11 concern, not this one.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import KeyPoseExtractionError, Node3ResultInputError


# -------------------------------------------------------------------
# Defaults
# -------------------------------------------------------------------

DEFAULT_MAE_THRESHOLD = 8.0
"""MAE (on 0-255 grayscale) above which a frame is considered a new key
pose. Picked to tolerate mild encoder artifacts on clean line-art MP4s
without absorbing actual pose changes. CLI flag `--threshold` overrides.
"""

DEFAULT_MAX_EDGE = 128
"""Downscale frames so max(H, W) <= this before comparison. 128 is small
enough to keep phase correlation + MAE fast on a full shot and large
enough to preserve the structure of a cartoon pose.
"""

_FRAME_NAME_RE = re.compile(r"^frame_(\d{4,})\.png$")


# -------------------------------------------------------------------
# Public result types
# -------------------------------------------------------------------

@dataclass
class HeldFrame:
    """One held frame's relation to its key-pose anchor.

    `offset` is `[dy, dx]` in **full-resolution** pixels. Positive `dy`
    = pose shifted down from anchor; positive `dx` = shifted right.
    The key-pose frame itself is the first held frame with offset `[0, 0]`.
    """
    frame: int
    offset: list[int]


@dataclass
class KeyPoseEntry:
    """One key pose within a shot, plus every held frame it anchors."""
    keyPoseIndex: int
    sourceFrame: int
    keyPoseFilename: str
    heldFrames: list[HeldFrame] = field(default_factory=list)


@dataclass
class KeyPoseMap:
    """Per-shot key-pose partition. Written to `<shotId>/keypose_map.json`."""
    schemaVersion: int = 1
    shotId: str = ""
    totalFrames: int = 0
    sourceFramesDir: str = ""
    keyPosesDir: str = ""
    threshold: float = DEFAULT_MAE_THRESHOLD
    maxEdge: int = DEFAULT_MAX_EDGE
    keyPoses: list[KeyPoseEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "shotId": self.shotId,
            "totalFrames": self.totalFrames,
            "sourceFramesDir": self.sourceFramesDir,
            "keyPosesDir": self.keyPosesDir,
            "threshold": self.threshold,
            "maxEdge": self.maxEdge,
            "keyPoses": [
                {
                    "keyPoseIndex": kp.keyPoseIndex,
                    "sourceFrame": kp.sourceFrame,
                    "keyPoseFilename": kp.keyPoseFilename,
                    "heldFrames": [asdict(h) for h in kp.heldFrames],
                }
                for kp in self.keyPoses
            ],
        }


@dataclass
class ShotKeyPoseSummary:
    """One-line summary of a shot's Node 4 output."""
    shotId: str
    totalFrames: int
    keyPoseCount: int
    sourceFramesDir: str
    keyPosesDir: str
    keyPoseMapPath: str


@dataclass
class Node4Result:
    """Aggregate Node 4 result. Written to `<work-dir>/node4_result.json`."""
    schemaVersion: int = 1
    projectName: str = ""
    workDir: str = ""
    threshold: float = DEFAULT_MAE_THRESHOLD
    maxEdge: int = DEFAULT_MAX_EDGE
    extractedAt: str = ""
    shots: list[ShotKeyPoseSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "projectName": self.projectName,
            "workDir": self.workDir,
            "threshold": self.threshold,
            "maxEdge": self.maxEdge,
            "extractedAt": self.extractedAt,
            "shots": [asdict(s) for s in self.shots],
        }


# -------------------------------------------------------------------
# Public entry points
# -------------------------------------------------------------------

def extract_keyposes_for_queue(
    node3_result_path: Path | str,
    threshold: float = DEFAULT_MAE_THRESHOLD,
    max_edge: int = DEFAULT_MAX_EDGE,
) -> Node4Result:
    """Partition every shot's frames into key poses + held runs.

    Args:
        node3_result_path: Path to `node3_result.json` (Node 3's output).
            Node 4's aggregate manifest is written alongside it in the
            same work directory.
        threshold: MAE (0-255) threshold for "aligned-same-pose". Frames
            whose aligned MAE exceeds this become new key poses.
        max_edge: Downscale so `max(H, W) = max_edge` before comparison.

    Returns:
        `Node4Result` — success if no exception raised.

    Raises:
        Node3ResultInputError: `node3_result.json` missing, malformed, or
            references a frames folder that no longer exists.
        KeyPoseExtractionError: a frame failed to load/decode, or the
            target `keyposes/` folder couldn't be written.
    """
    n3_path = Path(node3_result_path).resolve()
    n3 = _load_node3_result(n3_path)
    work_dir = Path(n3["workDir"]).resolve()

    result = Node4Result(
        projectName=n3.get("projectName", ""),
        workDir=str(work_dir),
        threshold=threshold,
        maxEdge=max_edge,
        extractedAt=datetime.now(timezone.utc).isoformat(),
    )

    for shot in n3["shots"]:
        summary = extract_keyposes_for_shot(
            shot_id=shot["shotId"],
            source_frames_dir=Path(shot["framesDir"]),
            frame_filenames=list(shot["frameFilenames"]),
            threshold=threshold,
            max_edge=max_edge,
        )
        result.shots.append(summary)

    (work_dir / "node4_result.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


def extract_keyposes_for_shot(
    shot_id: str,
    source_frames_dir: Path | str,
    frame_filenames: list[str],
    threshold: float = DEFAULT_MAE_THRESHOLD,
    max_edge: int = DEFAULT_MAX_EDGE,
) -> ShotKeyPoseSummary:
    """Partition one shot's frames into key poses + held runs.

    Writes:
        `<source_frames_dir>/keyposes/frame_NNNN.png` (copies of the
            source frames chosen as key poses)
        `<source_frames_dir>/keypose_map.json` (the full partition)

    Returns:
        `ShotKeyPoseSummary`: one-line entry for `node4_result.json`.
    """
    source_frames_dir = Path(source_frames_dir).resolve()

    if not source_frames_dir.is_dir():
        raise Node3ResultInputError(
            f"{shot_id}: frames folder does not exist: {source_frames_dir}. "
            "Did Node 3 complete?"
        )
    if not frame_filenames:
        raise KeyPoseExtractionError(
            f"{shot_id}: frame list from node3_result.json is empty."
        )

    keyposes_dir = source_frames_dir / "keyposes"
    try:
        keyposes_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise KeyPoseExtractionError(
            f"{shot_id}: could not create keyposes folder {keyposes_dir} ({e})"
        ) from e

    # Clear stale copies from a previous run so the map matches reality.
    for stale in keyposes_dir.glob("frame_*.png"):
        try:
            stale.unlink()
        except OSError:  # pragma: no cover
            pass

    km = _partition_frames(
        shot_id=shot_id,
        source_frames_dir=source_frames_dir,
        frame_filenames=frame_filenames,
        keyposes_dir=keyposes_dir,
        threshold=threshold,
        max_edge=max_edge,
    )

    map_path = source_frames_dir / "keypose_map.json"
    map_path.write_text(
        json.dumps(km.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return ShotKeyPoseSummary(
        shotId=shot_id,
        totalFrames=km.totalFrames,
        keyPoseCount=len(km.keyPoses),
        sourceFramesDir=str(source_frames_dir),
        keyPosesDir=str(keyposes_dir),
        keyPoseMapPath=str(map_path),
    )


# -------------------------------------------------------------------
# 4A: input loading
# -------------------------------------------------------------------

def _load_node3_result(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise Node3ResultInputError(
            f"node3_result.json not found at {path}. Run Node 3 first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Node3ResultInputError(
            f"node3_result.json at {path} is not valid JSON: {e}"
        ) from e
    if not isinstance(raw, dict):
        raise Node3ResultInputError(
            f"node3_result.json at {path} must be a JSON object; "
            f"got {type(raw).__name__}."
        )
    if raw.get("schemaVersion") != 1:
        raise Node3ResultInputError(
            f"node3_result.json at {path} has unsupported schemaVersion="
            f"{raw.get('schemaVersion')!r}; Node 4 expects 1."
        )
    for key in ("workDir", "shots"):
        if key not in raw:
            raise Node3ResultInputError(
                f"node3_result.json at {path} missing required key '{key}'."
            )
    if not isinstance(raw["shots"], list):
        raise Node3ResultInputError(
            f"node3_result.json at {path}: 'shots' must be a list."
        )
    for s_idx, shot in enumerate(raw["shots"]):
        if not isinstance(shot, dict):
            raise Node3ResultInputError(
                f"node3_result.json: shots[{s_idx}] is not an object."
            )
        for key in ("shotId", "framesDir", "frameFilenames"):
            if key not in shot:
                raise Node3ResultInputError(
                    f"node3_result.json: shots[{s_idx}] missing '{key}'."
                )
    return raw


# -------------------------------------------------------------------
# 4B–4D: the partition itself
# -------------------------------------------------------------------

def _partition_frames(
    shot_id: str,
    source_frames_dir: Path,
    frame_filenames: list[str],
    keyposes_dir: Path,
    threshold: float,
    max_edge: int,
) -> KeyPoseMap:
    """Walk a sorted frame list, emit key poses + held runs.

    Algorithm:
      - The first frame is always the first key pose (anchor).
      - For each subsequent frame, phase-correlate against the current
        anchor to estimate translation, then compute aligned MAE.
      - If aligned MAE <= threshold → held frame (with offset).
      - Otherwise → new key pose, becomes the new anchor.
    """
    km = KeyPoseMap(
        shotId=shot_id,
        totalFrames=len(frame_filenames),
        sourceFramesDir=str(source_frames_dir),
        keyPosesDir=str(keyposes_dir),
        threshold=threshold,
        maxEdge=max_edge,
    )

    # Prime: first frame = key pose 0.
    first_name = frame_filenames[0]
    first_idx = _parse_frame_index(first_name, shot_id)
    anchor_small, anchor_scale = _load_downscaled(
        source_frames_dir / first_name, max_edge, shot_id
    )
    _copy_frame(
        source_frames_dir / first_name,
        keyposes_dir / first_name,
        shot_id,
    )
    current_kp = KeyPoseEntry(
        keyPoseIndex=0,
        sourceFrame=first_idx,
        keyPoseFilename=first_name,
        heldFrames=[HeldFrame(frame=first_idx, offset=[0, 0])],
    )

    for fname in frame_filenames[1:]:
        fidx = _parse_frame_index(fname, shot_id)
        frame_small, frame_scale = _load_downscaled(
            source_frames_dir / fname, max_edge, shot_id
        )

        # Guard: all frames in a shot must share dimensions.
        if frame_small.shape != anchor_small.shape:
            raise KeyPoseExtractionError(
                f"{shot_id}: frame {fname} downscaled shape "
                f"{frame_small.shape} mismatches anchor "
                f"{anchor_small.shape} — all frames in a shot must be "
                "the same resolution."
            )

        dy_s, dx_s = _phase_correlate(anchor_small, frame_small)
        mae = _aligned_mae(anchor_small, frame_small, dy_s, dx_s)

        if mae <= threshold:
            dy_full = int(round(dy_s * anchor_scale))
            dx_full = int(round(dx_s * anchor_scale))
            current_kp.heldFrames.append(
                HeldFrame(frame=fidx, offset=[dy_full, dx_full])
            )
        else:
            # New key pose starts here.
            km.keyPoses.append(current_kp)
            anchor_small, anchor_scale = frame_small, frame_scale
            _copy_frame(
                source_frames_dir / fname,
                keyposes_dir / fname,
                shot_id,
            )
            current_kp = KeyPoseEntry(
                keyPoseIndex=len(km.keyPoses),
                sourceFrame=fidx,
                keyPoseFilename=fname,
                heldFrames=[HeldFrame(frame=fidx, offset=[0, 0])],
            )

    km.keyPoses.append(current_kp)
    return km


def _parse_frame_index(fname: str, shot_id: str) -> int:
    m = _FRAME_NAME_RE.match(fname)
    if not m:
        raise KeyPoseExtractionError(
            f"{shot_id}: unexpected frame filename {fname!r}; "
            "Node 3 should have produced frame_NNNN.png."
        )
    return int(m.group(1))


# -------------------------------------------------------------------
# Frame I/O, phase correlation, aligned MAE
# -------------------------------------------------------------------

def _load_downscaled(
    path: Path,
    max_edge: int,
    shot_id: str,
) -> tuple[Any, float]:
    """Load PNG as grayscale uint8 array, downscaled so max(H, W) <= max_edge.

    Returns `(array, scale)` where `scale >= 1.0` is the ratio
    original / downscaled. To convert a small-space offset back to
    full-resolution pixels: `dy_full = round(dy_small * scale)`.
    """
    try:
        from PIL import Image  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover - caught by requirements.txt
        raise KeyPoseExtractionError(
            f"{shot_id}: required package missing ({e}). "
            "Install numpy + Pillow: pip install numpy pillow"
        ) from e

    # Pillow 9.1+ deprecated the top-level LANCZOS alias; keep both paths.
    lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS", None)
    if lanczos is None:  # pragma: no cover - extremely old Pillow
        lanczos = Image.LANCZOS

    try:
        img = Image.open(path).convert("L")
    except Exception as e:  # noqa: BLE001
        raise KeyPoseExtractionError(
            f"{shot_id}: could not open {path}: {e}"
        ) from e

    w, h = img.size
    max_dim = max(h, w)
    if max_dim > max_edge:
        scale = max_dim / float(max_edge)
        new_w = max(1, int(round(w / scale)))
        new_h = max(1, int(round(h / scale)))
        img = img.resize((new_w, new_h), lanczos)
    else:
        scale = 1.0
    arr = np.asarray(img, dtype=np.uint8)
    return arr, scale


def _phase_correlate(a: Any, b: Any) -> tuple[int, int]:
    """Return signed `(dy, dx)` such that `b[y, x] ≈ a[y - dy, x - dx]`.

    Equivalently: `b` is `a` translated by `(dy, dx)` (positive `dy` =
    shifted down, positive `dx` = shifted right).

    Both inputs must be 2-D numpy arrays with identical shape.

    Implementation note: for `b(y, x) = a(y - dy, x - dx)` the cross-
    power-spectrum IFFT peaks at `(-dy, -dx)` (mod shape), so after
    unwrapping the wrap-around we negate to match the docstring.
    """
    import numpy as np  # local import — module must load without numpy

    fa = np.fft.fft2(a.astype(np.float32))
    fb = np.fft.fft2(b.astype(np.float32))
    cross = fa * np.conj(fb)
    mag = np.abs(cross)
    # Uniform frames zero out the spectrum; the peak is trivially (0, 0).
    mag = np.where(mag == 0, 1.0, mag)
    ratio = cross / mag
    corr = np.fft.ifft2(ratio).real
    h, w = a.shape
    idx = int(np.argmax(corr))
    py = idx // w
    px = idx % w
    # Unwrap the periodic FFT coordinate into signed integers.
    if py > h // 2:
        py -= h
    if px > w // 2:
        px -= w
    # Peak at (-dy, -dx) -> negate to report the actual shift b = a + (dy, dx).
    return int(-py), int(-px)


def _aligned_mae(a: Any, b: Any, dy: int, dx: int) -> float:
    """Mean absolute error between `a` and `b` shifted by `(dy, dx)`.

    Computed on the valid overlap region only. If `(dy, dx)` pushes the
    entire overlap off either frame, returns `inf` (forcing a new-key-pose
    emission).
    """
    import numpy as np

    h, w = a.shape
    y0 = max(0, dy)
    y1 = min(h, h + dy)
    x0 = max(0, dx)
    x1 = min(w, w + dx)
    if y1 <= y0 or x1 <= x0:
        return float("inf")
    b_region = b[y0:y1, x0:x1]
    a_region = a[y0 - dy:y1 - dy, x0 - dx:x1 - dx]
    diff = a_region.astype(np.float32) - b_region.astype(np.float32)
    return float(np.mean(np.abs(diff)))


def _copy_frame(src: Path, dst: Path, shot_id: str) -> None:
    try:
        shutil.copyfile(src, dst)
    except OSError as e:
        raise KeyPoseExtractionError(
            f"{shot_id}: could not copy {src} -> {dst}: {e}"
        ) from e


__all__ = [
    "HeldFrame",
    "KeyPoseEntry",
    "KeyPoseMap",
    "ShotKeyPoseSummary",
    "Node4Result",
    "extract_keyposes_for_queue",
    "extract_keyposes_for_shot",
    "DEFAULT_MAE_THRESHOLD",
    "DEFAULT_MAX_EDGE",
]
