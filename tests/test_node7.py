"""Tests for Node 7 - AI-Powered Pose Refinement.

Node 7's live path requires ComfyUI + GPU + weight downloads, none of
which are available in CI or on the user's laptop (locked decision #13:
Node 7 is RunPod-only for real runs). So these tests exercise the three
layers that CAN run offline:

  1. Manifest I/O:    load_node6_result / load_queue / load_reference_map
                      / build_pose_extractor_lookup / build_routing_table
                      / write_refined_map / write_node7_result
  2. Seed derivation: deterministic, reproducible across reruns.
  3. Orchestrator in dry-run mode: every detection recorded as
                      status='skipped' without ever contacting ComfyUI.
  4. Workflow template loader: loud failure on stale / missing / malformed
                      templates.
  5. ComfyUIClient's stdlib helpers (extract_first_image) -- pure data,
                      no network.
  6. CLI exit codes: 0 on dry-run success, 1 on Node7Error, 2 on bug.

Live ComfyUI submission + real PNG fetching are covered by a smoke test
that runs on the pod after `runpod_setup.sh` completes; they are not
pytest-testable here.

Run from repo root with:

    python -m pytest tests/test_node7.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_nodes.node_07_pose_refiner.comfyui_client import (
    extract_first_image,
)
from custom_nodes.node_07_pose_refiner.manifest import (
    DetectionTask,
    RefinedGeneration,
    ShotRefinedSummary,
    _derive_seed,
    _safe_segment,
    build_pose_extractor_lookup,
    build_routing_table,
    load_node6_result,
    load_queue,
    load_reference_map,
    write_node7_result,
    write_refined_map,
)
from custom_nodes.node_07_pose_refiner.orchestrate import (
    DEFAULT_COMFYUI_URL,
    NODE_KSAMPLER,
    NODE_LOAD_KEY_POSE,
    NODE_LOAD_REF_COLOR,
    NODE_NEGATIVE_PROMPT,
    NODE_POSITIVE_PROMPT,
    NODE_SAVE_IMAGE,
    NEGATIVE_PROMPT,
    OrchestrateConfig,
    POSITIVE_PROMPT_TEMPLATE,
    STRENGTH_DWPOSE,
    STRENGTH_IP_ADAPTER,
    STRENGTH_LINEART,
    STRENGTH_SCRIBBLE,
    _cn_strengths_for,
    _load_workflow_templates,
    _parameterize_workflow,
    refine_queue,
)
from pipeline.cli_node7 import main as cli_main
from pipeline.errors import (
    ComfyUIConnectionError,
    Node6ResultInputError,
    Node7Error,
    PipelineError,
    QueueLookupError,
    RefinementGenerationError,
    WorkflowTemplateError,
)


# ---------------------------------------------------------------
# _safe_segment + _derive_seed
# ---------------------------------------------------------------

def test_safe_segment_allows_alnum_dash_underscore() -> None:
    assert _safe_segment("Bhim") == "Bhim"
    assert _safe_segment("bhim_01") == "bhim_01"
    assert _safe_segment("jaggu-the-monkey") == "jaggu-the-monkey"


def test_safe_segment_replaces_unsafe_chars() -> None:
    assert _safe_segment("b/him") == "b_him"
    assert _safe_segment("b him") == "b_him"
    assert _safe_segment("b\\him") == "b_him"
    assert _safe_segment("b:him") == "b_him"


def test_derive_seed_is_deterministic() -> None:
    seed_a = _derive_seed("proj", "shot_001", 0, "Bhim")
    seed_b = _derive_seed("proj", "shot_001", 0, "Bhim")
    assert seed_a == seed_b


def test_derive_seed_varies_with_inputs() -> None:
    base = _derive_seed("proj", "shot_001", 0, "Bhim")
    assert _derive_seed("proj", "shot_001", 1, "Bhim") != base
    assert _derive_seed("proj", "shot_002", 0, "Bhim") != base
    assert _derive_seed("proj", "shot_001", 0, "Jaggu") != base
    assert _derive_seed("proj2", "shot_001", 0, "Bhim") != base


def test_derive_seed_is_non_negative_31bit() -> None:
    for args in [
        ("p", "s", 0, "a"),
        ("p", "s", 999999, "z"),
        ("", "", 0, ""),
    ]:
        seed = _derive_seed(*args)
        assert 0 <= seed < (1 << 31)


# ---------------------------------------------------------------
# Orchestrator constant / prompt guarantees
# ---------------------------------------------------------------

def test_cn_strengths_for_dwpose() -> None:
    s = _cn_strengths_for("dwpose")
    assert s == {
        "dwposeControlnet": STRENGTH_DWPOSE,
        "ipAdapter": STRENGTH_IP_ADAPTER,
    }


def test_cn_strengths_for_lineart_fallback() -> None:
    s = _cn_strengths_for("lineart-fallback")
    assert s == {
        "lineartControlnet": STRENGTH_LINEART,
        "scribbleControlnet": STRENGTH_SCRIBBLE,
        "ipAdapter": STRENGTH_IP_ADAPTER,
    }


def test_cn_strength_defaults_match_locked_decisions() -> None:
    # Locked decisions #2 / #3 from CLAUDE.md (Node 7 block).
    assert STRENGTH_DWPOSE == 0.75
    assert STRENGTH_LINEART == 0.60
    assert STRENGTH_SCRIBBLE == 0.60
    assert STRENGTH_IP_ADAPTER == 0.80


def test_positive_prompt_template_has_required_fields() -> None:
    assert "{identity}" in POSITIVE_PROMPT_TEMPLATE
    assert "{angle_descriptor}" in POSITIVE_PROMPT_TEMPLATE
    assert "line art" in POSITIVE_PROMPT_TEMPLATE
    assert "black and white" in POSITIVE_PROMPT_TEMPLATE


def test_negative_prompt_is_non_empty() -> None:
    assert NEGATIVE_PROMPT.strip() != ""
    assert "color" in NEGATIVE_PROMPT


# ---------------------------------------------------------------
# load_node6_result
# ---------------------------------------------------------------

def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_load_node6_result_missing_file(tmp_path: Path) -> None:
    with pytest.raises(Node6ResultInputError, match="not found"):
        load_node6_result(tmp_path / "nope.json")


def test_load_node6_result_not_json(tmp_path: Path) -> None:
    p = tmp_path / "node6_result.json"
    p.write_text("{not json}", encoding="utf-8")
    with pytest.raises(Node6ResultInputError, match="not valid JSON"):
        load_node6_result(p)


def test_load_node6_result_wrong_type(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "node6_result.json", [1, 2, 3])
    with pytest.raises(Node6ResultInputError, match="must be a JSON object"):
        load_node6_result(p)


def test_load_node6_result_bad_schema_version(tmp_path: Path) -> None:
    p = _write_json(
        tmp_path / "node6_result.json",
        {"schemaVersion": 2, "workDir": str(tmp_path), "shots": []},
    )
    with pytest.raises(Node6ResultInputError, match="schemaVersion"):
        load_node6_result(p)


def test_load_node6_result_missing_key(tmp_path: Path) -> None:
    p = _write_json(
        tmp_path / "node6_result.json",
        {"schemaVersion": 1, "workDir": str(tmp_path)},
    )
    with pytest.raises(Node6ResultInputError, match="shots"):
        load_node6_result(p)


def test_load_node6_result_happy_path(tmp_path: Path) -> None:
    p = _write_json(
        tmp_path / "node6_result.json",
        {
            "schemaVersion": 1,
            "projectName": "proj",
            "workDir": str(tmp_path),
            "shots": [{"shotId": "shot_001", "referenceMapPath": "x"}],
        },
    )
    raw = load_node6_result(p)
    assert raw["schemaVersion"] == 1
    assert raw["shots"][0]["shotId"] == "shot_001"


# ---------------------------------------------------------------
# load_queue + build_pose_extractor_lookup
# ---------------------------------------------------------------

def test_load_queue_missing_file(tmp_path: Path) -> None:
    with pytest.raises(QueueLookupError, match="not found"):
        load_queue(tmp_path / "nope.json")


def test_load_queue_wrong_schema_version(tmp_path: Path) -> None:
    p = _write_json(
        tmp_path / "queue.json",
        {"schemaVersion": 2, "batches": []},
    )
    with pytest.raises(QueueLookupError, match="schemaVersion"):
        load_queue(p)


def test_load_queue_missing_batches(tmp_path: Path) -> None:
    p = _write_json(
        tmp_path / "queue.json",
        {"schemaVersion": 1},
    )
    with pytest.raises(QueueLookupError, match="batches"):
        load_queue(p)


def test_build_pose_extractor_lookup_happy(tmp_path: Path) -> None:
    queue = {
        "schemaVersion": 1,
        "batches": [[
            {
                "shotId": "shot_001",
                "characters": [
                    {"identity": "Bhim", "poseExtractor": "dwpose"},
                    {"identity": "Jaggu", "poseExtractor": "lineart-fallback"},
                ],
            },
        ]],
    }
    lookup = build_pose_extractor_lookup(queue)
    assert lookup[("shot_001", "Bhim")] == "dwpose"
    assert lookup[("shot_001", "Jaggu")] == "lineart-fallback"


def test_build_pose_extractor_lookup_missing_route() -> None:
    queue = {
        "schemaVersion": 1,
        "batches": [[
            {
                "shotId": "shot_001",
                "characters": [
                    {"identity": "Bhim"},  # missing poseExtractor
                ],
            },
        ]],
    }
    with pytest.raises(QueueLookupError, match="poseExtractor"):
        build_pose_extractor_lookup(queue)


# ---------------------------------------------------------------
# End-to-end manifest + dry-run fixture
# ---------------------------------------------------------------

def _build_fixture(
    tmp_path: Path,
    *,
    identity_routes: dict[str, str] | None = None,
) -> dict[str, Path]:
    """Synthesize a minimal Node 6 -> Node 7 scaffold.

    Produces:
      - work_dir/<shotId>/keyposes/frame_0001.png  (empty stub -- we do
        not actually send it to ComfyUI in dry-run)
      - work_dir/<shotId>/reference_crops/<id>_<angle>.png + _lineart.png
      - work_dir/<shotId>/reference_map.json
      - work_dir/node6_result.json
      - input/queue.json

    Single shot 'shot_001', single key pose (index 0), two characters
    with routes chosen by `identity_routes` (defaults: Bhim=dwpose,
    Jaggu=lineart-fallback).
    """
    routes = identity_routes or {
        "Bhim": "dwpose",
        "Jaggu": "lineart-fallback",
    }

    input_dir = tmp_path / "input"
    work_dir = tmp_path / "work"
    input_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    shot_id = "shot_001"
    shot_root = work_dir / shot_id
    keyposes_dir = shot_root / "keyposes"
    ref_crops_dir = shot_root / "reference_crops"
    keyposes_dir.mkdir(parents=True, exist_ok=True)
    ref_crops_dir.mkdir(parents=True, exist_ok=True)

    kp_filename = "frame_0001.png"
    kp_path = keyposes_dir / kp_filename
    kp_path.write_bytes(b"\x89PNG\r\n\x1a\n")  # valid PNG header stub

    matches: list[dict[str, Any]] = []
    for identity in routes:
        angle = "front"
        color_crop = ref_crops_dir / f"{identity}_{angle}.png"
        line_crop = ref_crops_dir / f"{identity}_{angle}_lineart.png"
        for p in (color_crop, line_crop):
            p.write_bytes(b"\x89PNG\r\n\x1a\n")
        matches.append({
            "identity": identity,
            "expectedPosition": "C",
            "boundingBox": [10, 20, 30, 40],
            "selectedAngle": angle,
            "scoreBreakdown": {},
            "allScores": {},
            "referenceColorCropPath": str(color_crop),
            "referenceLineArtCropPath": str(line_crop),
        })

    ref_map_path = shot_root / "reference_map.json"
    _write_json(ref_map_path, {
        "schemaVersion": 1,
        "shotId": shot_id,
        "sourceFramesDir": str(shot_root / "frames"),
        "keyPosesDir": str(keyposes_dir),
        "referenceCropsDir": str(ref_crops_dir),
        "lineArtMethod": "dog",
        "keyPoses": [{
            "keyPoseIndex": 0,
            "keyPoseFilename": kp_filename,
            "sourceFrame": 1,
            "matches": matches,
            "skipped": [],
        }],
    })

    node6_result_path = work_dir / "node6_result.json"
    _write_json(node6_result_path, {
        "schemaVersion": 1,
        "projectName": "testproj",
        "workDir": str(work_dir),
        "shots": [{
            "shotId": shot_id,
            "keyPoseCount": 1,
            "detectionCount": len(matches),
            "skippedCount": 0,
            "referenceMapPath": str(ref_map_path),
            "angleHistogram": {"front": len(matches)},
        }],
        "lineArtMethod": "dog",
    })

    queue_path = input_dir / "queue.json"
    _write_json(queue_path, {
        "schemaVersion": 1,
        "projectName": "testproj",
        "batchSize": 1,
        "totalShots": 1,
        "batchCount": 1,
        "batches": [[{
            "shotId": shot_id,
            "mp4Path": str(input_dir / "shot_001.mp4"),
            "durationFrames": 25,
            "durationSeconds": 1.0,
            "characters": [
                {
                    "identity": identity,
                    "sheetPath": str(input_dir / f"{identity}_sheet.png"),
                    "position": "C",
                    "poseExtractor": route,
                }
                for identity, route in routes.items()
            ],
        }]],
    })

    return {
        "input_dir": input_dir,
        "work_dir": work_dir,
        "node6_result_path": node6_result_path,
        "queue_path": queue_path,
        "shot_root": shot_root,
        "ref_map_path": ref_map_path,
    }


def test_load_reference_map_happy(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)
    rm = load_reference_map(paths["ref_map_path"], "shot_001")
    assert rm["schemaVersion"] == 1
    assert len(rm["keyPoses"]) == 1


def test_load_reference_map_missing(tmp_path: Path) -> None:
    with pytest.raises(Node6ResultInputError, match="not found"):
        load_reference_map(tmp_path / "nope.json", "shot_X")


def test_build_routing_table_shape(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)
    node6 = load_node6_result(paths["node6_result_path"])
    queue = load_queue(paths["queue_path"])
    tasks = build_routing_table(node6, queue)

    assert len(tasks) == 2
    tasks_by_ident = {t.identity: t for t in tasks}
    bhim = tasks_by_ident["Bhim"]
    jaggu = tasks_by_ident["Jaggu"]

    assert bhim.shotId == "shot_001"
    assert bhim.poseExtractor == "dwpose"
    assert bhim.selectedAngle == "front"
    assert bhim.boundingBox == (10, 20, 30, 40)
    assert bhim.keyPoseIndex == 0
    assert bhim.sourceFrame == 1
    assert bhim.keyPosePath.name == "frame_0001.png"
    assert bhim.refinedPath.name == "000_Bhim.png"
    assert 0 <= bhim.seed < (1 << 31)

    assert jaggu.poseExtractor == "lineart-fallback"


def test_build_routing_table_missing_key_pose_png(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)
    # delete the one key pose PNG
    (paths["shot_root"] / "keyposes" / "frame_0001.png").unlink()
    node6 = load_node6_result(paths["node6_result_path"])
    queue = load_queue(paths["queue_path"])
    with pytest.raises(Node6ResultInputError, match="PNG missing"):
        build_routing_table(node6, queue)


def test_build_routing_table_missing_reference_crop(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)
    ref_crops = paths["shot_root"] / "reference_crops"
    # delete every crop for Bhim
    for p in ref_crops.glob("Bhim_*.png"):
        p.unlink()
    node6 = load_node6_result(paths["node6_result_path"])
    queue = load_queue(paths["queue_path"])
    with pytest.raises(Node6ResultInputError, match="reference crop missing"):
        build_routing_table(node6, queue)


def test_build_routing_table_missing_pose_extractor(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)
    node6 = load_node6_result(paths["node6_result_path"])
    # Strip poseExtractor from Jaggu in queue.json.
    queue_data = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    chars = queue_data["batches"][0][0]["characters"]
    for c in chars:
        if c["identity"] == "Jaggu":
            del c["poseExtractor"]
    paths["queue_path"].write_text(
        json.dumps(queue_data, indent=2), encoding="utf-8"
    )
    queue = load_queue(paths["queue_path"])
    # build_pose_extractor_lookup raises before build_routing_table does.
    with pytest.raises(QueueLookupError, match="poseExtractor"):
        build_routing_table(node6, queue)


# ---------------------------------------------------------------
# write_refined_map + write_node7_result
# ---------------------------------------------------------------

def test_write_refined_map_roundtrips(tmp_path: Path) -> None:
    shot_root = tmp_path / "shot_001"
    shot_root.mkdir()
    gens = [
        RefinedGeneration(
            identity="Bhim",
            keyPoseIndex=0,
            sourceFrame=1,
            selectedAngle="front",
            poseExtractor="dwpose",
            seed=42,
            refinedPath=str(shot_root / "refined" / "000_Bhim.png"),
            boundingBox=[1, 2, 3, 4],
            status="skipped",
            errorMessage="dry-run",
            cnStrengths={"ipAdapter": 0.8},
        ),
    ]
    out = write_refined_map("shot_001", shot_root, gens)
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schemaVersion"] == 1
    assert data["shotId"] == "shot_001"
    assert data["generations"][0]["identity"] == "Bhim"
    assert data["generations"][0]["status"] == "skipped"
    assert data["generations"][0]["cnStrengths"] == {"ipAdapter": 0.8}


def test_write_node7_result_roundtrips(tmp_path: Path) -> None:
    node6 = {
        "projectName": "proj",
        "workDir": str(tmp_path),
    }
    summaries = [
        ShotRefinedSummary(
            shotId="shot_001",
            keyPoseCount=1,
            generatedCount=0,
            skippedCount=2,
            errorCount=0,
            refinedMapPath=str(tmp_path / "shot_001" / "refined_map.json"),
        )
    ]
    result = write_node7_result(
        node6_result=node6,
        shot_summaries=summaries,
        comfyui_url="http://example:8188",
        dry_run=True,
    )
    assert result.dryRun is True
    assert result.projectName == "proj"
    assert result.comfyUIUrl == "http://example:8188"
    assert result.refinedAt  # non-empty UTC ISO timestamp

    on_disk = json.loads(
        (tmp_path / "node7_result.json").read_text(encoding="utf-8")
    )
    assert on_disk["schemaVersion"] == 1
    assert on_disk["dryRun"] is True
    assert on_disk["shots"][0]["skippedCount"] == 2


# ---------------------------------------------------------------
# Orchestrator dry-run end-to-end
# ---------------------------------------------------------------

def test_refine_queue_dry_run_writes_manifests(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)
    config = OrchestrateConfig(
        node6_result_path=paths["node6_result_path"],
        queue_path=paths["queue_path"],
        dry_run=True,
    )
    result = refine_queue(config)

    assert result.dryRun is True
    assert len(result.shots) == 1
    s = result.shots[0]
    assert s.shotId == "shot_001"
    assert s.skippedCount == 2
    assert s.generatedCount == 0
    assert s.errorCount == 0

    # refined_map.json exists with 2 skipped generations.
    rm_path = paths["shot_root"] / "refined_map.json"
    assert rm_path.is_file()
    rm = json.loads(rm_path.read_text(encoding="utf-8"))
    assert len(rm["generations"]) == 2
    for g in rm["generations"]:
        assert g["status"] == "skipped"
        assert g["errorMessage"] == "dry-run"
        assert g["seed"] >= 0

    # Aggregate node7_result.json exists.
    n7_path = paths["work_dir"] / "node7_result.json"
    assert n7_path.is_file()
    n7 = json.loads(n7_path.read_text(encoding="utf-8"))
    assert n7["dryRun"] is True
    assert n7["comfyUIUrl"] == DEFAULT_COMFYUI_URL


# ---------------------------------------------------------------
# Workflow template loader
# ---------------------------------------------------------------

def test_load_workflow_templates_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(WorkflowTemplateError, match="Missing workflow template"):
        _load_workflow_templates(tmp_path)


def test_load_workflow_templates_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "workflow.json").write_text("{not json}", encoding="utf-8")
    (tmp_path / "workflow_lineart_fallback.json").write_text(
        "{}", encoding="utf-8"
    )
    with pytest.raises(WorkflowTemplateError, match="not valid JSON"):
        _load_workflow_templates(tmp_path)


def test_load_workflow_templates_missing_prompt_key(tmp_path: Path) -> None:
    _write_json(tmp_path / "workflow.json", {"hello": "world"})
    _write_json(tmp_path / "workflow_lineart_fallback.json", {"prompt": {}})
    with pytest.raises(WorkflowTemplateError, match="'prompt' key"):
        _load_workflow_templates(tmp_path)


def test_load_workflow_templates_prompt_not_dict(tmp_path: Path) -> None:
    _write_json(tmp_path / "workflow.json", {"prompt": [1, 2]})
    _write_json(tmp_path / "workflow_lineart_fallback.json", {"prompt": {}})
    with pytest.raises(WorkflowTemplateError, match="must be a dict"):
        _load_workflow_templates(tmp_path)


def test_shipped_workflow_templates_load() -> None:
    """The JSON templates we ship in this repo must load cleanly."""
    workflow_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_nodes" / "node_07_pose_refiner"
    )
    templates = _load_workflow_templates(workflow_dir)
    assert set(templates.keys()) == {"dwpose", "lineart-fallback"}
    for route, graph in templates.items():
        for node_id in (
            NODE_KSAMPLER,
            NODE_LOAD_KEY_POSE,
            NODE_LOAD_REF_COLOR,
            NODE_POSITIVE_PROMPT,
            NODE_NEGATIVE_PROMPT,
            NODE_SAVE_IMAGE,
        ):
            assert node_id in graph, (
                f"route {route!r}: workflow template missing node {node_id!r}"
            )


# ---------------------------------------------------------------
# _parameterize_workflow
# ---------------------------------------------------------------

def _minimal_template() -> dict[str, Any]:
    """A bare template that has only the required node IDs, enough to
    let _parameterize_workflow run to completion."""
    nodes = {
        NODE_KSAMPLER: {"class_type": "KSampler", "inputs": {}},
        NODE_LOAD_KEY_POSE: {"class_type": "LoadImage", "inputs": {}},
        NODE_LOAD_REF_COLOR: {"class_type": "LoadImage", "inputs": {}},
        NODE_POSITIVE_PROMPT: {"class_type": "CLIPTextEncode", "inputs": {}},
        NODE_NEGATIVE_PROMPT: {"class_type": "CLIPTextEncode", "inputs": {}},
        NODE_SAVE_IMAGE: {"class_type": "SaveImage", "inputs": {}},
    }
    return nodes


def _make_task(seed: int = 42) -> DetectionTask:
    return DetectionTask(
        shotId="shot_001",
        keyPoseIndex=0,
        keyPoseFilename="frame_0001.png",
        sourceFrame=1,
        identity="Bhim",
        poseExtractor="dwpose",
        expectedPosition="C",
        boundingBox=(0, 0, 10, 10),
        selectedAngle="front-3q-L",
        keyPosePath=Path("/tmp/key.png"),
        referenceColorCropPath=Path("/tmp/ref.png"),
        referenceLineArtCropPath=Path("/tmp/ref_line.png"),
        refinedPath=Path("/tmp/refined/000_Bhim.png"),
        seed=seed,
    )


def test_parameterize_workflow_injects_required_fields() -> None:
    task = _make_task(seed=1234)
    graph = _parameterize_workflow(_minimal_template(), task)
    assert graph[NODE_KSAMPLER]["inputs"]["seed"] == 1234
    assert graph[NODE_KSAMPLER]["inputs"]["sampler_name"] == "dpmpp_2m"
    assert graph[NODE_KSAMPLER]["inputs"]["scheduler"] == "karras"
    assert graph[NODE_KSAMPLER]["inputs"]["steps"] == 25
    assert graph[NODE_KSAMPLER]["inputs"]["cfg"] == 7.0

    # Path serialization is platform-dependent (Windows uses backslashes);
    # compare via str(task.<path>) rather than a hardcoded POSIX string.
    assert graph[NODE_LOAD_KEY_POSE]["inputs"]["image"] == str(task.keyPosePath)
    assert graph[NODE_LOAD_REF_COLOR]["inputs"]["image"] == str(
        task.referenceColorCropPath
    )

    pos = graph[NODE_POSITIVE_PROMPT]["inputs"]["text"]
    assert "Bhim" in pos
    assert "front 3q L" in pos  # dash -> space for prose-like prompt

    neg = graph[NODE_NEGATIVE_PROMPT]["inputs"]["text"]
    assert neg == NEGATIVE_PROMPT

    assert graph[NODE_SAVE_IMAGE]["inputs"]["filename_prefix"] == (
        "animatic/shot_001/000_Bhim"
    )


def test_parameterize_workflow_missing_node_raises() -> None:
    template = _minimal_template()
    del template[NODE_KSAMPLER]
    with pytest.raises(WorkflowTemplateError, match="KSampler"):
        _parameterize_workflow(template, _make_task())


def test_parameterize_workflow_deep_copies_input() -> None:
    """Mutating the returned graph must not affect the shared template."""
    tpl = _minimal_template()
    graph = _parameterize_workflow(tpl, _make_task(seed=7))
    graph[NODE_KSAMPLER]["inputs"]["seed"] = 99999
    assert "seed" not in tpl[NODE_KSAMPLER]["inputs"]


# ---------------------------------------------------------------
# ComfyUIClient helper (no network -- just the pure parser)
# ---------------------------------------------------------------

def test_extract_first_image_happy() -> None:
    hist = {
        "outputs": {
            "20": {
                "images": [
                    {"filename": "abc.png", "subfolder": "animatic/shot_001"}
                ]
            }
        }
    }
    filename, subfolder = extract_first_image(hist, "20")
    assert filename == "abc.png"
    assert subfolder == "animatic/shot_001"


def test_extract_first_image_missing_node() -> None:
    with pytest.raises(RefinementGenerationError, match="no outputs"):
        extract_first_image({"outputs": {}}, "20")


def test_extract_first_image_empty_images() -> None:
    hist = {"outputs": {"20": {"images": []}}}
    with pytest.raises(RefinementGenerationError, match="no images"):
        extract_first_image(hist, "20")


# ---------------------------------------------------------------
# CLI exit codes
# ---------------------------------------------------------------

def test_cli_dry_run_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = _build_fixture(tmp_path)
    code = cli_main([
        "--node6-result", str(paths["node6_result_path"]),
        "--queue", str(paths["queue_path"]),
        "--dry-run",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "DRY-RUN" in captured.out
    assert "testproj" in captured.out


def test_cli_quiet_suppresses_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = _build_fixture(tmp_path)
    code = cli_main([
        "--node6-result", str(paths["node6_result_path"]),
        "--queue", str(paths["queue_path"]),
        "--dry-run", "--quiet",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_cli_missing_node6_result_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _build_fixture(tmp_path)
    bogus = tmp_path / "does_not_exist.json"
    code = cli_main([
        "--node6-result", str(bogus),
        "--queue", str(paths["queue_path"]),
        "--dry-run",
    ])
    assert code == 1
    captured = capsys.readouterr()
    assert "FAILED" in captured.err


def test_cli_missing_queue_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _build_fixture(tmp_path)
    bogus = tmp_path / "no_queue.json"
    code = cli_main([
        "--node6-result", str(paths["node6_result_path"]),
        "--queue", str(bogus),
        "--dry-run",
    ])
    assert code == 1


# ---------------------------------------------------------------
# Error-class subclass invariants (locked design from pipeline/errors.py)
# ---------------------------------------------------------------

def test_node7_error_hierarchy_under_pipeline_error() -> None:
    assert issubclass(Node7Error, PipelineError)
    assert issubclass(Node6ResultInputError, Node7Error)
    assert issubclass(WorkflowTemplateError, Node7Error)
    assert issubclass(ComfyUIConnectionError, Node7Error)
    assert issubclass(RefinementGenerationError, Node7Error)


def test_queue_lookup_error_shared_with_node5() -> None:
    """QueueLookupError is reused from Node 5's module for Node 6 + 7."""
    # Imported successfully from pipeline.errors -- just assert it's a
    # PipelineError sibling, not a Node7Error subclass (we don't want
    # Node 7 to own it).
    assert issubclass(QueueLookupError, PipelineError)
    assert not issubclass(QueueLookupError, Node7Error)
