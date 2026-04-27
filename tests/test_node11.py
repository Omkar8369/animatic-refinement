"""Pytest suite for Node 11 - Batch Management.

Mirrors the test layout of test_node5/6/7/8/9/10.py but with one big
difference: Node 11 is an ORCHESTRATOR, so most tests mock subprocess
.Popen rather than run actual subprocesses. The `_smoke_node11.py`
script (separate file) covers real-end-to-end-pipeline runs.

Locked decisions tested (CLAUDE.md "Node 11 - locked decisions"):
  1.  Subprocess each run_nodeN.py.                  [test_builds_correct_argv_per_node]
  2.  Single Node 11 invocation = whole queue.json.  [test_runs_nodes_2_through_10_in_order]
  5.  Default retries = 0.                            [test_default_no_retries]
  6.  Per-node retry override.                        [test_retry_node7_passes_through]
  7.  Pre-Node-7 nvidia-smi check (best-effort).      [test_gpu_check_handles_no_nvidia_smi]
  9.  JSONL progress log appended.                    [test_jsonl_records_start_and_complete]
  11. Final report on disk.                           [test_writes_node11_result_json]
  13. Exit code semantics (partial success = 0).      [test_partial_success_exits_0 / test_all_failed_raises]
  14. Pure-Python orchestrator.                       [implicit -- imports]
  15. Rerun wipes node11 outputs.                     [test_rerun_wipes_outputs]
  16. --dry-run propagation.                          [test_dry_run_appends_flag_for_node7]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from pipeline.errors import (  # noqa: E402
    BatchAllFailedError,
    InputDirError,
    Node11Error,
    NodeStepError,
    PipelineError,
)
from pipeline.node11 import (  # noqa: E402
    DEFAULT_COMFYUI_URL,
    DEFAULT_CRF,
    NODE_RANGE,
    NodeStepResult,
    Node11Result,
    PROGRESS_FILENAME,
    RESULT_FILENAME,
    ShotResult,
    _aggregate_shot_results,
    _build_argv_for_node,
    _diagnose_per_shot_failure,
    _enumerate_shot_ids,
    _validate_input_dir,
    run_batch,
    try_log_gpu_info,
)


# -------------------------------------------------------------------
# Subprocess mock helpers
# -------------------------------------------------------------------

class _FakePopen:
    """A minimal subprocess.Popen stand-in. Returns the configured
    exit code; emits the configured stdout lines (one at a time, like
    real Popen line-buffering); and exposes the configured stderr."""

    def __init__(self, *, returncode: int = 0,
                 stdout_lines: list[str] | None = None,
                 stderr_text: str = "") -> None:
        self._returncode = returncode
        self._stdout_lines = stdout_lines or []
        self._stderr_text = stderr_text
        self.stdout = iter(self._stdout_lines) if self._stdout_lines else iter([])
        self.stderr = MagicMock()
        self.stderr.read = MagicMock(return_value=stderr_text)
        self.returncode = -1  # set in wait()

    def wait(self) -> int:
        self.returncode = self._returncode
        return self._returncode


def _make_popen_factory(per_node_results: dict[int, list[dict[str, Any]]]):
    """Return a Popen factory that consumes one configured result per
    call. Indexed by node number; each entry is a list of attempts so
    we can simulate retries."""
    state: dict[int, int] = {n: 0 for n in per_node_results}

    def factory(argv, **kwargs):  # noqa: ANN001
        # Identify which node this argv targets via the runner script
        # name (run_nodeN.py).
        runner = Path(argv[1]).name
        node = int(runner.replace("run_node", "").replace(".py", ""))
        attempts = per_node_results.get(node, [{"returncode": 0}])
        idx = state.get(node, 0)
        if idx >= len(attempts):
            idx = len(attempts) - 1
        cfg = attempts[idx]
        state[node] = idx + 1
        return _FakePopen(
            returncode=cfg.get("returncode", 0),
            stdout_lines=cfg.get("stdout_lines", []),
            stderr_text=cfg.get("stderr_text", ""),
        )
    return factory


def _make_input_dir(tmp_path: Path, *, with_metadata: bool = True,
                    with_characters: bool = True) -> Path:
    """Create a minimal --input-dir with the files Node 11's
    pre-flight check requires."""
    d = tmp_path / "input"
    d.mkdir(parents=True, exist_ok=True)
    if with_metadata:
        (d / "metadata.json").write_text("{}")
    if with_characters:
        (d / "characters.json").write_text("{}")
    return d


def _seed_queue_json(input_dir: Path, shot_ids: list[str]) -> Path:
    """Create a queue.json under input_dir mimicking what Node 2
    would write."""
    queue = {
        "schemaVersion": 1,
        "projectName": "test11",
        "batchSize": len(shot_ids),
        "totalShots": len(shot_ids),
        "batchCount": 1,
        "batches": [[
            {
                "shotId": sid,
                "mp4Path": str(input_dir / f"{sid}.mp4"),
                "durationFrames": 25,
                "characters": [],
            }
            for sid in shot_ids
        ]],
    }
    p = input_dir / "queue.json"
    p.write_text(json.dumps(queue))
    return p


def _seed_output_mp4(work_dir: Path, shot_id: str) -> Path:
    """Pretend Node 10 wrote `<shot_id>_refined.mp4`."""
    out_dir = work_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    mp4 = out_dir / f"{shot_id}_refined.mp4"
    mp4.write_bytes(b"fake mp4")
    return mp4


# -------------------------------------------------------------------
# Error-hierarchy invariants
# -------------------------------------------------------------------

class TestErrorHierarchy:

    def test_node11_error_is_pipeline_error(self):
        assert issubclass(Node11Error, PipelineError)

    def test_subclasses_are_node11_errors(self):
        assert issubclass(InputDirError, Node11Error)
        assert issubclass(NodeStepError, Node11Error)
        assert issubclass(BatchAllFailedError, Node11Error)


# -------------------------------------------------------------------
# 11A - Pre-flight checks
# -------------------------------------------------------------------

class TestValidateInputDir:

    def test_passes_with_metadata_and_characters(self, tmp_path):
        d = _make_input_dir(tmp_path)
        _validate_input_dir(d)  # should not raise

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(InputDirError, match="does not exist"):
            _validate_input_dir(tmp_path / "absent")

    def test_not_a_directory_raises(self, tmp_path):
        f = tmp_path / "afile"
        f.write_text("nope")
        with pytest.raises(InputDirError, match="not a directory"):
            _validate_input_dir(f)

    def test_missing_metadata_raises(self, tmp_path):
        d = _make_input_dir(tmp_path, with_metadata=False)
        with pytest.raises(InputDirError, match="metadata.json"):
            _validate_input_dir(d)

    def test_missing_characters_raises(self, tmp_path):
        d = _make_input_dir(tmp_path, with_characters=False)
        with pytest.raises(InputDirError, match="characters.json"):
            _validate_input_dir(d)


class TestTryLogGpuInfo:

    def test_returns_none_on_filenotfound(self):
        with patch("pipeline.node11.subprocess.run",
                   side_effect=FileNotFoundError("nvidia-smi not found")):
            assert try_log_gpu_info() is None

    def test_returns_none_on_nonzero_exit(self):
        mock_proc = MagicMock(returncode=1, stdout="")
        with patch("pipeline.node11.subprocess.run", return_value=mock_proc):
            assert try_log_gpu_info() is None

    def test_returns_stdout_on_success(self):
        mock_proc = MagicMock(
            returncode=0,
            stdout="NVIDIA GeForce RTX 4090, 23698, 24576\n",
        )
        with patch("pipeline.node11.subprocess.run", return_value=mock_proc):
            info = try_log_gpu_info()
            assert info is not None
            assert "RTX 4090" in info


# -------------------------------------------------------------------
# 11B - Argv construction per node
# -------------------------------------------------------------------

class TestBuildArgvForNode:

    def _argv(self, node, **overrides):
        kwargs = dict(
            input_dir=Path("/in"),
            work_dir=Path("/work"),
            comfyui_url=DEFAULT_COMFYUI_URL,
            crf=18,
            dry_run=False,
            # Phase 2: --workflow + --precision are passed through to
            # Node 7. Existing tests only care about Phase 1 behaviour;
            # default to v1 + fp16 (Node 7's CLI defaults) so adding
            # the kwargs here is a no-op for older test cases.
            workflow="v1",
            precision="fp16",
        )
        kwargs.update(overrides)
        return _build_argv_for_node(node, **kwargs)

    def test_node2_uses_input_dir(self):
        argv = self._argv(2)
        assert "--input-dir" in argv
        assert str(Path("/in")) in argv

    def test_node3_uses_queue_and_work_dir(self):
        argv = self._argv(3)
        assert "--queue" in argv
        assert "--work-dir" in argv

    def test_node4_uses_node3_result(self):
        argv = self._argv(4)
        assert "--node3-result" in argv
        assert str(Path("/work") / "node3_result.json") in argv

    def test_node5_uses_node4_result_and_queue(self):
        argv = self._argv(5)
        assert "--node4-result" in argv
        assert "--queue" in argv

    def test_node6_uses_three_inputs(self):
        argv = self._argv(6)
        assert "--node5-result" in argv
        assert "--queue" in argv
        assert "--characters" in argv

    def test_node7_includes_comfyui_url(self):
        argv = self._argv(7, comfyui_url="http://other:8188")
        assert "--comfyui-url" in argv
        assert "http://other:8188" in argv

    def test_node7_dry_run_appends_flag(self):
        argv = self._argv(7, dry_run=True)
        assert "--dry-run" in argv

    def test_node7_no_dry_run_omits_flag(self):
        argv = self._argv(7, dry_run=False)
        assert "--dry-run" not in argv

    def test_node8_uses_node7_result(self):
        argv = self._argv(8)
        assert "--node7-result" in argv

    def test_node9_uses_node8_result(self):
        argv = self._argv(9)
        assert "--node8-result" in argv

    def test_node10_uses_node9_result_and_crf(self):
        argv = self._argv(10, crf=23)
        assert "--node9-result" in argv
        assert "--crf" in argv
        assert "23" in argv

    def test_unknown_node_raises(self):
        with pytest.raises(ValueError, match="unknown node"):
            self._argv(99)

    def test_runner_script_is_run_nodeN_py(self):
        for n in NODE_RANGE:
            argv = self._argv(n)
            runner = Path(argv[1]).name
            assert runner == f"run_node{n}.py", (
                f"Node {n}: runner script should be run_node{n}.py, "
                f"got {runner}"
            )


# -------------------------------------------------------------------
# Helpers tested in isolation
# -------------------------------------------------------------------

class TestEnumerateShotIds:

    def test_walks_batches_in_order(self):
        queue = {
            "batches": [
                [{"shotId": "shot_001"}, {"shotId": "shot_002"}],
                [{"shotId": "shot_003"}],
            ]
        }
        assert _enumerate_shot_ids(queue) == ["shot_001", "shot_002", "shot_003"]

    def test_empty_queue_returns_empty_list(self):
        assert _enumerate_shot_ids({}) == []
        assert _enumerate_shot_ids({"batches": []}) == []


class TestDiagnosePerShotFailure:

    def test_returns_3_when_node3_result_missing(self, tmp_path):
        # No artifacts at all -> the first missing one is Node 3
        assert _diagnose_per_shot_failure(tmp_path, "shot_001") == 3

    def test_returns_4_when_keypose_map_missing(self, tmp_path):
        (tmp_path / "node3_result.json").write_text("{}")
        # shot_root/keypose_map.json missing -> Node 4
        assert _diagnose_per_shot_failure(tmp_path, "shot_001") == 4

    def test_returns_10_when_only_mp4_missing(self, tmp_path):
        (tmp_path / "node3_result.json").write_text("{}")
        shot_root = tmp_path / "shot_001"
        shot_root.mkdir()
        for fn in ("keypose_map.json", "character_map.json",
                   "reference_map.json", "refined_map.json",
                   "composed_map.json", "timed_map.json"):
            (shot_root / fn).write_text("{}")
        # Everything present except the final MP4 -> Node 10
        assert _diagnose_per_shot_failure(tmp_path, "shot_001") == 10


class TestAggregateShotResults:

    def test_all_succeeded_when_all_mp4s_present(self, tmp_path):
        input_dir = _make_input_dir(tmp_path)
        _seed_queue_json(input_dir, ["shot_001", "shot_002"])
        work = tmp_path / "work"
        _seed_output_mp4(work, "shot_001")
        _seed_output_mp4(work, "shot_002")
        queue = json.loads((input_dir / "queue.json").read_text())

        results = _aggregate_shot_results(
            queue=queue, work_dir=work, node_steps=[],
        )
        assert all(r.status == "ok" for r in results)
        assert len(results) == 2

    def test_partial_success_when_one_mp4_missing(self, tmp_path):
        input_dir = _make_input_dir(tmp_path)
        _seed_queue_json(input_dir, ["shot_001", "shot_002"])
        work = tmp_path / "work"
        _seed_output_mp4(work, "shot_001")
        # shot_002 has no MP4
        queue = json.loads((input_dir / "queue.json").read_text())

        results = _aggregate_shot_results(
            queue=queue, work_dir=work, node_steps=[],
        )
        statuses = {r.shotId: r.status for r in results}
        assert statuses == {"shot_001": "ok", "shot_002": "failed"}

    def test_failing_node_propagated_when_global_node_failed(self, tmp_path):
        input_dir = _make_input_dir(tmp_path)
        _seed_queue_json(input_dir, ["shot_001"])
        work = tmp_path / "work"
        # No output MP4 + Node 7 marked as failed in node_steps
        queue = json.loads((input_dir / "queue.json").read_text())
        node_steps = [
            NodeStepResult(node=7, status="error", attempts=1,
                           durationSeconds=1.0, exitCode=1,
                           lastStderrTail="boom"),
        ]
        results = _aggregate_shot_results(
            queue=queue, work_dir=work, node_steps=node_steps,
        )
        assert results[0].status == "failed"
        assert results[0].failingNode == 7


# -------------------------------------------------------------------
# Top-level driver: run_batch (with mocked Popen)
# -------------------------------------------------------------------

class TestRunBatch:

    def _all_pass(self) -> dict[int, list[dict[str, Any]]]:
        """Mock config: every node 2-10 returns 0 on first try."""
        return {n: [{"returncode": 0,
                     "stdout_lines": [f"[node{n}] OK\n"]}]
                for n in NODE_RANGE}

    def _setup_for_full_run(self, tmp_path):
        """Build input_dir with metadata.json + characters.json AND a
        seeded queue.json (so the post-Node-2 'load queue' step works
        even though Node 2 was mocked)."""
        input_dir = _make_input_dir(tmp_path)
        _seed_queue_json(input_dir, ["shot_001"])
        work_dir = tmp_path / "work"
        return input_dir, work_dir

    def test_runs_nodes_2_through_10_in_order(self, tmp_path):
        input_dir, work_dir = self._setup_for_full_run(tmp_path)
        _seed_output_mp4(work_dir, "shot_001")  # so partial-success path triggers ok

        invocations: list[int] = []
        per_node = self._all_pass()

        def factory(argv, **kwargs):
            runner = Path(argv[1]).name
            n = int(runner.replace("run_node", "").replace(".py", ""))
            invocations.append(n)
            return _FakePopen(returncode=0)

        with patch("pipeline.node11.subprocess.Popen", side_effect=factory):
            with patch("pipeline.node11.subprocess.run",
                       side_effect=FileNotFoundError):  # no nvidia-smi
                run_batch(input_dir=input_dir, work_dir=work_dir,
                          dry_run=True)

        assert invocations == list(NODE_RANGE), (
            f"Expected nodes invoked in order {list(NODE_RANGE)}; got {invocations}"
        )

    def test_writes_node11_result_json(self, tmp_path):
        """Locked decision #11."""
        input_dir, work_dir = self._setup_for_full_run(tmp_path)
        _seed_output_mp4(work_dir, "shot_001")

        with patch("pipeline.node11.subprocess.Popen",
                   side_effect=_make_popen_factory(self._all_pass())):
            with patch("pipeline.node11.subprocess.run",
                       side_effect=FileNotFoundError):
                run_batch(input_dir=input_dir, work_dir=work_dir,
                          dry_run=True)

        result_path = work_dir / RESULT_FILENAME
        assert result_path.is_file()
        result = json.loads(result_path.read_text())
        assert result["schemaVersion"] == 1
        assert result["totalShots"] == 1
        assert result["succeededShots"] == 1
        assert result["failedShots"] == 0

    def test_jsonl_records_start_and_complete(self, tmp_path):
        """Locked decision #9."""
        input_dir, work_dir = self._setup_for_full_run(tmp_path)
        _seed_output_mp4(work_dir, "shot_001")

        with patch("pipeline.node11.subprocess.Popen",
                   side_effect=_make_popen_factory(self._all_pass())):
            with patch("pipeline.node11.subprocess.run",
                       side_effect=FileNotFoundError):
                run_batch(input_dir=input_dir, work_dir=work_dir,
                          dry_run=True)

        progress_path = work_dir / PROGRESS_FILENAME
        assert progress_path.is_file()
        events = [json.loads(line) for line in progress_path.read_text().splitlines()]
        event_types = [e["event"] for e in events]
        assert "batch_start" in event_types
        assert "batch_complete" in event_types
        # Each node should have 1 start + 1 complete
        for n in NODE_RANGE:
            starts = [e for e in events if e.get("event") == "node_step_start" and e.get("node") == n]
            completes = [e for e in events if e.get("event") == "node_step_complete" and e.get("node") == n]
            assert len(starts) == 1, f"Node {n}: expected 1 start event, got {len(starts)}"
            assert len(completes) == 1

    def test_default_no_retries(self, tmp_path):
        """Locked decision #5 -- on Node-7 failure with default
        retries, only ONE attempt should be logged."""
        input_dir, work_dir = self._setup_for_full_run(tmp_path)
        # Node 7 fails once, every other node passes
        per_node = self._all_pass()
        per_node[7] = [{"returncode": 1, "stderr_text": "node 7 boom"}]

        with patch("pipeline.node11.subprocess.Popen",
                   side_effect=_make_popen_factory(per_node)):
            with patch("pipeline.node11.subprocess.run",
                       side_effect=FileNotFoundError):
                # No MP4 will get written -> all shots fail -> raises
                with pytest.raises(BatchAllFailedError):
                    run_batch(input_dir=input_dir, work_dir=work_dir,
                              dry_run=True)

        events = [json.loads(line) for line in
                  (work_dir / PROGRESS_FILENAME).read_text().splitlines()]
        n7_starts = [e for e in events if e.get("event") == "node_step_start" and e.get("node") == 7]
        assert len(n7_starts) == 1, (
            "Default retries=0 -> Node 7 should have been attempted only once"
        )

    def test_retry_node7_passes_through(self, tmp_path):
        """Locked decision #6 -- --retry-node7=2 -> 3 total attempts."""
        input_dir, work_dir = self._setup_for_full_run(tmp_path)
        # Node 7 fails 3 times in a row (so retries are exhausted)
        per_node = self._all_pass()
        per_node[7] = [
            {"returncode": 1, "stderr_text": "boom 1"},
            {"returncode": 1, "stderr_text": "boom 2"},
            {"returncode": 1, "stderr_text": "boom 3"},
        ]

        with patch("pipeline.node11.subprocess.Popen",
                   side_effect=_make_popen_factory(per_node)):
            with patch("pipeline.node11.subprocess.run",
                       side_effect=FileNotFoundError):
                with pytest.raises(BatchAllFailedError):
                    run_batch(
                        input_dir=input_dir, work_dir=work_dir,
                        retries_by_node={7: 2}, dry_run=True,
                    )

        events = [json.loads(line) for line in
                  (work_dir / PROGRESS_FILENAME).read_text().splitlines()]
        n7_starts = [e for e in events if e.get("event") == "node_step_start" and e.get("node") == 7]
        assert len(n7_starts) == 3, (
            f"--retry-node7=2 -> 3 total attempts; got {len(n7_starts)}"
        )

    def test_node2_failure_raises_node_step_error(self, tmp_path):
        input_dir = _make_input_dir(tmp_path)
        # Don't seed queue.json; Node 2 will be mocked to fail anyway
        work_dir = tmp_path / "work"
        per_node = self._all_pass()
        per_node[2] = [{"returncode": 1, "stderr_text": "node 2 boom"}]

        with patch("pipeline.node11.subprocess.Popen",
                   side_effect=_make_popen_factory(per_node)):
            with patch("pipeline.node11.subprocess.run",
                       side_effect=FileNotFoundError):
                with pytest.raises(NodeStepError, match="Node 2 failed"):
                    run_batch(input_dir=input_dir, work_dir=work_dir,
                              dry_run=True)

    def test_partial_success_exits_0(self, tmp_path):
        """Locked decision #13 -- mixed success/failure does NOT raise."""
        input_dir = _make_input_dir(tmp_path)
        _seed_queue_json(input_dir, ["shot_001", "shot_002"])
        work_dir = tmp_path / "work"
        # Only shot_001 gets an MP4 (simulate partial success)
        _seed_output_mp4(work_dir, "shot_001")

        with patch("pipeline.node11.subprocess.Popen",
                   side_effect=_make_popen_factory(self._all_pass())):
            with patch("pipeline.node11.subprocess.run",
                       side_effect=FileNotFoundError):
                # Should NOT raise -- partial success is OK
                result = run_batch(input_dir=input_dir, work_dir=work_dir,
                                    dry_run=True)

        assert result.succeededShots == 1
        assert result.failedShots == 1
        assert isinstance(result, Node11Result)

    def test_all_failed_raises_batch_all_failed(self, tmp_path):
        """Locked decision #13 -- 100% failure raises."""
        input_dir = _make_input_dir(tmp_path)
        _seed_queue_json(input_dir, ["shot_001", "shot_002"])
        work_dir = tmp_path / "work"
        # No MP4s at all

        with patch("pipeline.node11.subprocess.Popen",
                   side_effect=_make_popen_factory(self._all_pass())):
            with patch("pipeline.node11.subprocess.run",
                       side_effect=FileNotFoundError):
                with pytest.raises(BatchAllFailedError):
                    run_batch(input_dir=input_dir, work_dir=work_dir,
                              dry_run=True)

    def test_dry_run_appends_flag_for_node7(self, tmp_path):
        """Locked decision #16."""
        input_dir, work_dir = self._setup_for_full_run(tmp_path)
        _seed_output_mp4(work_dir, "shot_001")

        node7_argvs: list[list[str]] = []

        def factory(argv, **kwargs):
            runner = Path(argv[1]).name
            n = int(runner.replace("run_node", "").replace(".py", ""))
            if n == 7:
                node7_argvs.append(list(argv))
            return _FakePopen(returncode=0)

        with patch("pipeline.node11.subprocess.Popen", side_effect=factory):
            with patch("pipeline.node11.subprocess.run",
                       side_effect=FileNotFoundError):
                run_batch(input_dir=input_dir, work_dir=work_dir,
                          dry_run=True)

        assert len(node7_argvs) == 1
        assert "--dry-run" in node7_argvs[0]

    def test_rerun_wipes_outputs(self, tmp_path):
        """Locked decision #15 -- pre-run wipe of node11_progress.jsonl
        + node11_result.json."""
        input_dir, work_dir = self._setup_for_full_run(tmp_path)
        _seed_output_mp4(work_dir, "shot_001")
        # Pre-seed stale outputs
        work_dir.mkdir(parents=True, exist_ok=True)
        stale_progress = work_dir / PROGRESS_FILENAME
        stale_result = work_dir / RESULT_FILENAME
        stale_progress.write_text('{"event": "stale"}\n')
        stale_result.write_text('{"stale": true}')

        with patch("pipeline.node11.subprocess.Popen",
                   side_effect=_make_popen_factory(self._all_pass())):
            with patch("pipeline.node11.subprocess.run",
                       side_effect=FileNotFoundError):
                run_batch(input_dir=input_dir, work_dir=work_dir,
                          dry_run=True)

        # Stale lines/contents should be gone (the new run wrote
        # different stuff; specifically the first JSONL line of the new
        # run is "batch_start", not "stale")
        events = [json.loads(line) for line in
                  stale_progress.read_text().splitlines()]
        assert events[0]["event"] == "batch_start"
        # And the result.json no longer has the stale flag
        result = json.loads(stale_result.read_text())
        assert "stale" not in result

    def test_input_dir_validation_runs_first(self, tmp_path):
        """If input dir is bad, NO subprocess should be invoked."""
        bad_dir = tmp_path / "absent"
        work_dir = tmp_path / "work"

        called = []

        def factory(argv, **kwargs):
            called.append(argv)
            return _FakePopen(returncode=0)

        with patch("pipeline.node11.subprocess.Popen", side_effect=factory):
            with pytest.raises(InputDirError):
                run_batch(input_dir=bad_dir, work_dir=work_dir,
                          dry_run=True)
        assert called == [], "Subprocess should not have been invoked on input dir error"

    def test_gpu_check_skipped_in_dry_run(self, tmp_path):
        """Pre-Node-7 GPU check should NOT fire when --dry-run is set
        (Node 7 won't actually need GPU)."""
        input_dir, work_dir = self._setup_for_full_run(tmp_path)
        _seed_output_mp4(work_dir, "shot_001")

        with patch("pipeline.node11.subprocess.Popen",
                   side_effect=_make_popen_factory(self._all_pass())):
            with patch("pipeline.node11.try_log_gpu_info") as gpu_mock:
                run_batch(input_dir=input_dir, work_dir=work_dir,
                          dry_run=True)
                gpu_mock.assert_not_called()

    def test_gpu_check_runs_when_not_dry_run(self, tmp_path):
        input_dir, work_dir = self._setup_for_full_run(tmp_path)
        _seed_output_mp4(work_dir, "shot_001")

        with patch("pipeline.node11.subprocess.Popen",
                   side_effect=_make_popen_factory(self._all_pass())):
            with patch("pipeline.node11.try_log_gpu_info",
                       return_value="RTX 4090, 23698, 24576") as gpu_mock:
                run_batch(input_dir=input_dir, work_dir=work_dir,
                          dry_run=False)
                gpu_mock.assert_called_once()


# -------------------------------------------------------------------
# CLI subprocess
# -------------------------------------------------------------------

class TestCli:

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "run_node11.py"), *args],
            capture_output=True,
            text=True,
        )

    def test_cli_exit_1_on_input_dir_error(self, tmp_path):
        # Real subprocess; bad input dir -> InputDirError -> exit 1
        bad_dir = tmp_path / "absent"
        work_dir = tmp_path / "work"
        r = self._run(
            "--input-dir", str(bad_dir),
            "--work-dir", str(work_dir),
            "--dry-run",
        )
        assert r.returncode == 1
        assert "[node11] FAILED" in r.stderr
        assert "does not exist" in r.stderr

    def test_cli_help_lists_per_node_retry_flags(self):
        r = self._run("--help")
        assert r.returncode == 0
        # All 9 per-node flags should be listed
        for n in NODE_RANGE:
            assert f"--retry-node{n}" in r.stdout
