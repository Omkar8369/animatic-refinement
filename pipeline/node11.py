"""Node 11 - Batch Management (Project-level Orchestrator).

Runs Nodes 2-10 in sequence against a single batch. Replaces the
operator's eight-command shell sequence with one CLI invocation.
Subprocess each `run_nodeN.py`, read its exit code, retry per
configurable per-node policy, log progress to JSONL, and emit a
final aggregate report.

Locked decisions (do not re-litigate without updating CLAUDE.md):

1.  Subprocess each run_nodeN.py and read its exit code, NOT
    in-process import.
2.  A single Node 11 invocation runs the entire queue.json through
    Nodes 2-10 once.
3.  No resume capability in v1.
4.  Single-threaded.
5.  Default retries per node = 0 (fail-fast).
6.  Per-node retry override via --retry-nodeN <int>.
7.  Pre-Node-7 best-effort nvidia-smi check (warn but proceed).
8.  NO active VRAM monitoring in v1.
9.  Progress log = <work-dir>/node11_progress.jsonl.
10. Stdout passes through real time + tee'd to JSONL.
11. Final report = <work-dir>/node11_result.json.
12. Stdout summary = same [node11] OK ... shape as other nodes.
13. Exit-code semantics differ from Nodes 2-10: partial success =
    exit 0; 100% failure = exit 1.
14. Architecture template = same as Nodes 3-6/8/9/10. Pure-Python.
15. Rerun safety: wipe node11 outputs at start.
16. --dry-run passes through to Node 7's --dry-run.

Inputs:
  * `--input-dir <path>` -- the directory Node 2 reads. Must contain
    metadata.json + characters.json + sheet PNGs + shot MP4s.
  * `--work-dir <path>` -- where every downstream node writes its
    outputs.

Outputs:
  * Every Node 2-10 output (Node 11 just orchestrates).
  * `<work-dir>/node11_progress.jsonl` -- append-only event log.
  * `<work-dir>/node11_result.json` -- aggregate batch report.

This module is GPU-agnostic and importable from:
  * `pipeline.cli_node11.main` (CLI)
  * `custom_nodes.node_11_batch_manager.__init__` (ComfyUI)
  * `tests/test_node11.py` (pytest)

All error paths raise a `pipeline.errors.Node11Error` subclass.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pipeline.errors import (
    BatchAllFailedError,
    InputDirError,
    NodeStepError,
)


# -------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_RANGE = list(range(2, 11))  # Nodes 2..10 inclusive
DEFAULT_COMFYUI_URL = "http://127.0.0.1:8188"
DEFAULT_CRF = 18

PROGRESS_FILENAME = "node11_progress.jsonl"
RESULT_FILENAME = "node11_result.json"


# -------------------------------------------------------------------
# Public result types
# -------------------------------------------------------------------

@dataclass
class NodeStepResult:
    """One per-node-step record (one node, all attempts collapsed)."""
    node: int  # 2..10
    status: str  # "ok" | "error"
    attempts: int  # 1 + retries actually used
    durationSeconds: float
    exitCode: int  # final attempt's exit code
    lastStderrTail: str = ""  # for "error" status only

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "status": self.status,
            "attempts": self.attempts,
            "durationSeconds": self.durationSeconds,
            "exitCode": self.exitCode,
            "lastStderrTail": self.lastStderrTail,
        }


@dataclass
class ShotResult:
    """One per-shot final outcome."""
    shotId: str
    status: str  # "ok" | "failed"
    failingNode: int | None  # None on "ok"
    refinedMp4Path: str | None  # None on "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "shotId": self.shotId,
            "status": self.status,
            "failingNode": self.failingNode,
            "refinedMp4Path": self.refinedMp4Path,
        }


@dataclass
class Node11Result:
    """Aggregate Node 11 result. Written to
    `<work-dir>/node11_result.json`."""
    schemaVersion: int = 1
    projectName: str = ""
    workDir: str = ""
    inputDir: str = ""
    startedAt: str = ""
    completedAt: str = ""
    totalSeconds: float = 0.0
    nodeSteps: list[NodeStepResult] = field(default_factory=list)
    shotResults: list[ShotResult] = field(default_factory=list)
    totalShots: int = 0
    succeededShots: int = 0
    failedShots: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "projectName": self.projectName,
            "workDir": self.workDir,
            "inputDir": self.inputDir,
            "startedAt": self.startedAt,
            "completedAt": self.completedAt,
            "totalSeconds": self.totalSeconds,
            "nodeSteps": [n.to_dict() for n in self.nodeSteps],
            "shotResults": [s.to_dict() for s in self.shotResults],
            "totalShots": self.totalShots,
            "succeededShots": self.succeededShots,
            "failedShots": self.failedShots,
        }


# -------------------------------------------------------------------
# 11A - Pre-flight checks
# -------------------------------------------------------------------

def _validate_input_dir(input_dir: Path) -> None:
    """Raises InputDirError if the input dir doesn't exist, isn't a
    directory, or lacks the minimum files Node 2 will need."""
    if not input_dir.exists():
        raise InputDirError(
            f"--input-dir {input_dir} does not exist."
        )
    if not input_dir.is_dir():
        raise InputDirError(
            f"--input-dir {input_dir} is not a directory."
        )
    metadata_path = input_dir / "metadata.json"
    characters_path = input_dir / "characters.json"
    missing: list[str] = []
    if not metadata_path.is_file():
        missing.append("metadata.json")
    if not characters_path.is_file():
        missing.append("characters.json")
    if missing:
        raise InputDirError(
            f"--input-dir {input_dir} is missing required file(s): "
            f"{', '.join(missing)}. Make sure Node 1's downloads "
            "(metadata.json + characters.json) are placed in the "
            "input dir before running Node 11."
        )


def try_log_gpu_info() -> str | None:
    """Best-effort `nvidia-smi` shell-out (locked decision #7).

    Returns:
        A short human-readable GPU description on success, or None
        if nvidia-smi isn't available (or the call fails for any
        other reason).
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    return out


# -------------------------------------------------------------------
# JSONL progress log
# -------------------------------------------------------------------

def _append_jsonl(path: Path, event: dict[str, Any]) -> None:
    """Append one JSON line to the progress log. Adds 'ts' if absent."""
    if "ts" not in event:
        event = dict(event)
        event["ts"] = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False))
        f.write("\n")


# -------------------------------------------------------------------
# 11B - Build per-node argv + run subprocess (with retries)
# -------------------------------------------------------------------

def _build_argv_for_node(
    node: int,
    *,
    input_dir: Path,
    work_dir: Path,
    comfyui_url: str,
    crf: int,
    dry_run: bool,
    workflow: str,
    precision: str,
) -> list[str]:
    """Construct argv for `run_nodeN.py` based on the chained
    upstream artifacts each node expects.

    `workflow` and `precision` are passed through to Node 7 only
    (locked decision #13 — they're Phase 2 Node-7-specific flags). The
    other nodes ignore them.
    """
    runner = REPO_ROOT / f"run_node{node}.py"
    argv = [sys.executable, str(runner)]

    queue_path = input_dir / "queue.json"
    chars_path = input_dir / "characters.json"
    n3 = work_dir / "node3_result.json"
    n4 = work_dir / "node4_result.json"
    n5 = work_dir / "node5_result.json"
    n6 = work_dir / "node6_result.json"
    n7 = work_dir / "node7_result.json"
    n8 = work_dir / "node8_result.json"
    n9 = work_dir / "node9_result.json"

    if node == 2:
        argv += ["--input-dir", str(input_dir)]
    elif node == 3:
        argv += ["--queue", str(queue_path), "--work-dir", str(work_dir)]
    elif node == 4:
        argv += ["--node3-result", str(n3)]
    elif node == 5:
        argv += ["--node4-result", str(n4), "--queue", str(queue_path)]
    elif node == 6:
        argv += [
            "--node5-result", str(n5),
            "--queue", str(queue_path),
            "--characters", str(chars_path),
        ]
    elif node == 7:
        argv += [
            "--node6-result", str(n6),
            "--queue", str(queue_path),
            "--comfyui-url", comfyui_url,
            "--workflow", workflow,
            "--precision", precision,
        ]
        if dry_run:
            argv.append("--dry-run")
    elif node == 8:
        argv += ["--node7-result", str(n7)]
    elif node == 9:
        argv += ["--node8-result", str(n8)]
    elif node == 10:
        argv += ["--node9-result", str(n9), "--crf", str(crf)]
    else:
        raise ValueError(f"unknown node {node}")
    return argv


def _run_node_step(
    *,
    node: int,
    argv: list[str],
    retries: int,
    progress_path: Path,
    stdout_writer: Callable[[str], None] | None = None,
) -> NodeStepResult:
    """Run `argv` as a subprocess up to `1 + retries` times. Streams
    stdout/stderr to the operator's terminal in real time AND tees
    each line to the JSONL progress log.

    Returns:
        NodeStepResult with final status / attempts / duration / exit
        code (the LAST attempt's exit code, even on success).

    Note: This function does NOT raise on per-step failure. The
    caller decides whether to continue (record failure + carry on)
    or abort (raise NodeStepError). For Node 11's orchestration,
    failure of the FIRST node (Node 2) aborts; failure of any later
    node still tries to collect partial-success info.
    """
    if stdout_writer is None:
        stdout_writer = lambda line: print(line, end="", flush=True)

    started_at = datetime.now(timezone.utc)
    last_exit = -1
    last_stderr_tail = ""
    attempts_used = 0
    max_attempts = 1 + max(0, retries)

    for attempt in range(1, max_attempts + 1):
        attempts_used = attempt
        _append_jsonl(progress_path, {
            "event": "node_step_start",
            "node": node,
            "attempt": attempt,
            "argv": argv,
        })

        # Use Popen so we can stream stdout in real time AND capture
        # stderr separately (for the lastStderrTail on failure).
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered
            )
        except (FileNotFoundError, OSError) as e:
            # Couldn't even spawn; treat as error attempt.
            last_exit = -1
            last_stderr_tail = f"{type(e).__name__}: {e}"
            _append_jsonl(progress_path, {
                "event": "node_step_complete",
                "node": node,
                "attempt": attempt,
                "exitCode": last_exit,
                "stderrTail": last_stderr_tail,
            })
            continue

        # Stream stdout line by line to the terminal + JSONL.
        if proc.stdout is not None:
            for line in proc.stdout:
                stdout_writer(line)
                _append_jsonl(progress_path, {
                    "event": "stdout",
                    "node": node,
                    "attempt": attempt,
                    "line": line.rstrip("\n"),
                })

        # Drain stderr (don't tee per-line; just collect for tail).
        stderr_text = proc.stderr.read() if proc.stderr is not None else ""
        proc.wait()
        last_exit = proc.returncode
        # Always print stderr to operator's terminal so failures are
        # visible (mirrors what an operator would see running by hand).
        if stderr_text:
            for line in stderr_text.splitlines(keepends=True):
                # stderr goes to our stderr, not stdout
                print(line, end="", flush=True, file=sys.stderr)
        last_stderr_tail = "\n".join(stderr_text.splitlines()[-10:])

        _append_jsonl(progress_path, {
            "event": "node_step_complete",
            "node": node,
            "attempt": attempt,
            "exitCode": last_exit,
            "stderrTail": last_stderr_tail,
        })

        if last_exit == 0:
            break
        # Non-zero -> retry (if any retries left)

    completed_at = datetime.now(timezone.utc)
    duration = (completed_at - started_at).total_seconds()

    return NodeStepResult(
        node=node,
        status="ok" if last_exit == 0 else "error",
        attempts=attempts_used,
        durationSeconds=duration,
        exitCode=last_exit,
        lastStderrTail=last_stderr_tail if last_exit != 0 else "",
    )


# -------------------------------------------------------------------
# 11C - Per-shot status aggregation
# -------------------------------------------------------------------

def _load_queue(queue_path: Path) -> dict[str, Any]:
    """Minimal queue.json load -- only used to enumerate shotIds.
    Returns {} if absent (means Node 2 never produced it)."""
    if not queue_path.is_file():
        return {}
    try:
        return json.loads(queue_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _enumerate_shot_ids(queue: dict[str, Any]) -> list[str]:
    """Walk queue.json's batches and collect shotIds in order."""
    out: list[str] = []
    for batch in queue.get("batches", []):
        for shot in batch:
            sid = shot.get("shotId")
            if sid:
                out.append(sid)
    return out


def _aggregate_shot_results(
    *,
    queue: dict[str, Any],
    work_dir: Path,
    node_steps: list[NodeStepResult],
) -> list[ShotResult]:
    """For each shotId from queue.json, determine "succeeded" iff
    `<work-dir>/output/<shotId>_refined.mp4` exists. Otherwise mark
    as failed and identify the failing node by walking expected
    artifacts in pipeline order."""
    output_dir = work_dir / "output"
    shot_ids = _enumerate_shot_ids(queue)
    results: list[ShotResult] = []

    # If a node failed in node_steps, all subsequent shots will fail
    # at that node too -- find the lowest failing node number.
    earliest_failing_node: int | None = None
    for step in node_steps:
        if step.status == "error":
            earliest_failing_node = step.node
            break

    for sid in shot_ids:
        mp4_path = output_dir / f"{sid}_refined.mp4"
        if mp4_path.is_file():
            results.append(ShotResult(
                shotId=sid,
                status="ok",
                failingNode=None,
                refinedMp4Path=str(mp4_path),
            ))
            continue
        # Failed -- figure out which node failed for this shot.
        # First, the global failing node (if any node returned
        # non-zero, every shot fails at that node).
        if earliest_failing_node is not None:
            failing = earliest_failing_node
        else:
            # All nodes returned 0 but this shot still has no MP4 --
            # walk per-shot artifacts to find the missing one.
            failing = _diagnose_per_shot_failure(work_dir, sid)
        results.append(ShotResult(
            shotId=sid,
            status="failed",
            failingNode=failing,
            refinedMp4Path=None,
        ))
    return results


def _diagnose_per_shot_failure(work_dir: Path, shot_id: str) -> int:
    """When all node steps returned 0 but a shot has no MP4, walk
    expected per-shot artifacts to find the first missing one. The
    node that owns that artifact is the failing node.

    This handles the (rare) case where a downstream node's CLI exits
    0 but per-shot status had errors that never bubbled up to the
    top-level exit code (e.g., Node 7 with all generations marked
    status=error -> Node 8 substitute-rough -> Node 10 encodes the
    rough -> MP4 exists). In that case this code wouldn't fire
    because the MP4 DOES exist; the operator can grep refined_map
    .json for warnings if they want details. This code only fires
    if the MP4 is genuinely missing."""
    shot_root = work_dir / shot_id
    # Order matches the pipeline; first missing artifact = failing node
    checks = [
        (3, work_dir / "node3_result.json"),
        (4, shot_root / "keypose_map.json"),
        (5, shot_root / "character_map.json"),
        (6, shot_root / "reference_map.json"),
        (7, shot_root / "refined_map.json"),
        (8, shot_root / "composed_map.json"),
        (9, shot_root / "timed_map.json"),
        (10, work_dir / "output" / f"{shot_id}_refined.mp4"),
    ]
    for node, p in checks:
        if not p.exists():
            return node
    # Everything exists but MP4 doesn't (impossible given the last
    # check is the MP4 itself). Default to Node 10 as the most likely
    # culprit.
    return 10


# -------------------------------------------------------------------
# Top-level driver
# -------------------------------------------------------------------

def run_batch(
    *,
    input_dir: Path,
    work_dir: Path,
    comfyui_url: str = DEFAULT_COMFYUI_URL,
    crf: int = DEFAULT_CRF,
    retries_by_node: dict[int, int] | None = None,
    dry_run: bool = False,
    quiet: bool = False,
    workflow: str = "v1",
    precision: str = "fp16",
) -> Node11Result:
    """Drive the full Nodes 2-10 sequence against `input_dir` /
    `work_dir`.

    Returns:
        Node11Result describing what happened (per-node + per-shot
        status, paths to deliverables, total wall time).

    Raises:
        InputDirError: --input-dir bad.
        NodeStepError: Node 2 (queue.json producer) failed.
        BatchAllFailedError: 100% of shots failed to produce a final
            MP4. Note: partial success (some MP4s, some failures) does
            NOT raise -- the caller reads `failedShots > 0` from the
            result.
    """
    retries_by_node = dict(retries_by_node or {})
    input_dir = Path(input_dir)
    work_dir = Path(work_dir)

    # 11A - pre-flight
    _validate_input_dir(input_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Locked decision #15: wipe Node 11's own outputs before each run.
    progress_path = work_dir / PROGRESS_FILENAME
    result_path = work_dir / RESULT_FILENAME
    progress_path.unlink(missing_ok=True)
    result_path.unlink(missing_ok=True)

    started_at = datetime.now(timezone.utc)
    _append_jsonl(progress_path, {
        "event": "batch_start",
        "inputDir": str(input_dir),
        "workDir": str(work_dir),
        "comfyUIUrl": comfyui_url,
        "crf": crf,
        "dryRun": dry_run,
        "retriesByNode": retries_by_node,
        # Phase 2 additions: which Node 7 stack runs this batch.
        "workflow": workflow,
        "precision": precision,
    })

    # Best-effort GPU check before Node 7 (locked decision #7)
    if not dry_run:
        gpu_info = try_log_gpu_info()
        if gpu_info:
            _append_jsonl(progress_path, {
                "event": "gpu_visible",
                "info": gpu_info,
            })
            if not quiet:
                print(f"[node11] GPU visible: {gpu_info}", flush=True)
        else:
            _append_jsonl(progress_path, {
                "event": "gpu_unavailable",
                "note": "nvidia-smi not available or returned no GPUs",
            })
            if not quiet:
                print(
                    "[node11] WARNING: nvidia-smi not available or "
                    "returned no GPUs. Node 7 may fail. Pass --dry-run "
                    "to skip Node 7's live path.",
                    file=sys.stderr,
                    flush=True,
                )

    # 11B - sequential per-node execution
    node_steps: list[NodeStepResult] = []
    for node in NODE_RANGE:
        argv = _build_argv_for_node(
            node=node,
            input_dir=input_dir,
            work_dir=work_dir,
            comfyui_url=comfyui_url,
            crf=crf,
            dry_run=dry_run,
            workflow=workflow,
            precision=precision,
        )
        retries = retries_by_node.get(node, 0)
        step = _run_node_step(
            node=node,
            argv=argv,
            retries=retries,
            progress_path=progress_path,
        )
        node_steps.append(step)

        if step.status != "ok" and node == 2:
            # Node 2 produces queue.json; without it every downstream
            # node will fail too. Abort with NodeStepError so the
            # operator gets a clear message.
            completed_at = datetime.now(timezone.utc)
            _write_partial_result(
                result_path=result_path,
                input_dir=input_dir,
                work_dir=work_dir,
                started_at=started_at,
                completed_at=completed_at,
                node_steps=node_steps,
                shot_results=[],
                project_name="",
            )
            raise NodeStepError(
                f"Node {node} failed after {step.attempts} attempt(s) "
                f"(exit {step.exitCode}). Cannot continue without "
                f"queue.json. Last stderr:\n{step.lastStderrTail}"
            )

        if step.status != "ok":
            # A later node failed. We can still aggregate per-shot
            # status (probably "all failed at this node") and emit
            # the report so the operator knows where it died.
            break

    # 11C - per-shot aggregation
    queue = _load_queue(input_dir / "queue.json")
    shot_results = _aggregate_shot_results(
        queue=queue,
        work_dir=work_dir,
        node_steps=node_steps,
    )
    succeeded = sum(1 for s in shot_results if s.status == "ok")
    failed = len(shot_results) - succeeded

    # 11D - final report
    completed_at = datetime.now(timezone.utc)
    project_name = queue.get("projectName", "")
    result = _build_node11_result(
        input_dir=input_dir,
        work_dir=work_dir,
        started_at=started_at,
        completed_at=completed_at,
        node_steps=node_steps,
        shot_results=shot_results,
        project_name=project_name,
        succeeded=succeeded,
        failed=failed,
    )
    result_path.write_text(
        json.dumps(result.to_dict(), indent=2),
        encoding="utf-8",
    )
    _append_jsonl(progress_path, {
        "event": "batch_complete",
        "totalSeconds": result.totalSeconds,
        "totalShots": result.totalShots,
        "succeededShots": result.succeededShots,
        "failedShots": result.failedShots,
    })

    # 11E - exit-code semantics handled by CLI based on the result.
    # Here we only raise BatchAllFailedError for the 100% failure case
    # so the caller can distinguish "ran but everything broke" from
    # "ran with partial success".
    if shot_results and succeeded == 0:
        raise BatchAllFailedError(
            f"All {len(shot_results)} shot(s) failed to produce a "
            f"refined MP4. See {result_path} for per-shot details."
        )

    return result


def _build_node11_result(
    *,
    input_dir: Path,
    work_dir: Path,
    started_at: datetime,
    completed_at: datetime,
    node_steps: list[NodeStepResult],
    shot_results: list[ShotResult],
    project_name: str,
    succeeded: int,
    failed: int,
) -> Node11Result:
    return Node11Result(
        schemaVersion=1,
        projectName=project_name,
        workDir=str(work_dir),
        inputDir=str(input_dir),
        startedAt=started_at.isoformat(),
        completedAt=completed_at.isoformat(),
        totalSeconds=(completed_at - started_at).total_seconds(),
        nodeSteps=node_steps,
        shotResults=shot_results,
        totalShots=len(shot_results),
        succeededShots=succeeded,
        failedShots=failed,
    )


def _write_partial_result(
    *,
    result_path: Path,
    input_dir: Path,
    work_dir: Path,
    started_at: datetime,
    completed_at: datetime,
    node_steps: list[NodeStepResult],
    shot_results: list[ShotResult],
    project_name: str,
) -> None:
    """Used when we abort early (e.g., Node 2 failed). Best-effort:
    write the report so the operator has something to inspect even
    on a hard abort."""
    succeeded = sum(1 for s in shot_results if s.status == "ok")
    failed = len(shot_results) - succeeded
    result = _build_node11_result(
        input_dir=input_dir,
        work_dir=work_dir,
        started_at=started_at,
        completed_at=completed_at,
        node_steps=node_steps,
        shot_results=shot_results,
        project_name=project_name,
        succeeded=succeeded,
        failed=failed,
    )
    try:
        result_path.write_text(
            json.dumps(result.to_dict(), indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass  # best-effort
