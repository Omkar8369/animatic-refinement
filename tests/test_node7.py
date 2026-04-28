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

import numpy as np
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
    DEFAULT_PRECISION,
    DEFAULT_STYLE_LORA,
    DEFAULT_WORKFLOW,
    FLUX_T5XXL_BY_PRECISION,
    FLUX_UNET_BY_PRECISION,
    NODE_FLUX_CLIP,
    NODE_FLUX_CN_APPLY,
    NODE_FLUX_CN_LOADER,
    NODE_FLUX_CN_UNION_TYPE,
    NODE_FLUX_GUIDANCE,
    NODE_FLUX_IPADAPTER_APPLY,
    NODE_FLUX_IPADAPTER_LOADER,
    NODE_FLUX_IPADAPTER_REF_IMAGE,
    NODE_FLUX_KSAMPLER,
    NODE_FLUX_LATENT_INIT,
    NODE_FLUX_LOAD_ROUGH,
    NODE_FLUX_NEG_PROMPT,
    NODE_FLUX_POS_PROMPT,
    NODE_FLUX_POSE_PREPROC,
    NODE_FLUX_SAVE_IMAGE,
    NODE_FLUX_STYLE_LORA,
    NODE_FLUX_UNET,
    NODE_FLUX_VAE,
    NODE_FLUX_VAE_DECODE,
    NODE_KSAMPLER,
    NODE_LOAD_KEY_POSE,
    NODE_LOAD_REF_COLOR,
    NODE_NEGATIVE_PROMPT,
    NODE_POSITIVE_PROMPT,
    NODE_SAVE_IMAGE,
    NEGATIVE_PROMPT,
    OrchestrateConfig,
    POSITIVE_PROMPT_TEMPLATE,
    PRECISION_CHOICES,
    STYLE_LORA_CHOICES,
    STYLE_LORA_FILENAMES,
    STYLE_LORA_STRENGTHS,
    STRENGTH_DWPOSE,
    STRENGTH_IP_ADAPTER,
    STRENGTH_LINEART,
    STRENGTH_SCRIBBLE,
    V2_BBOX_FLUX_MULTIPLE,
    V2_BBOX_MARGIN_RATIO,
    V2_BBOX_TARGET_MAX_EDGE,
    V2_CFG,
    V2_DENOISE,
    V2_FLUX_GUIDANCE,
    V2_HEIGHT,
    V2_NEGATIVE_PROMPT,
    V2_POSITIVE_PROMPT_TEMPLATE,
    V2_SAMPLER_NAME,
    V2_SCHEDULER,
    V2_STEPS,
    V2_STRENGTH_CONTROLNET,
    V2_STRENGTH_IP_ADAPTER,
    V2_STYLE_LORA_STRENGTH,
    V2_WIDTH,
    WORKFLOW_CHOICES,
    _cn_strengths_for,
    _load_workflow_templates,
    _parameterize_workflow,
    _prepare_rough_bbox_crop,
    _resolve_dark_lines_source,
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
    """Phase 1 dwpose route: DWPose CN @ 0.75 + IP-Adapter @ 0.8.
    Phase 2c flipped DEFAULT_WORKFLOW to 'v2', so this test now passes
    workflow='v1' explicitly to assert the Phase 1 split."""
    s = _cn_strengths_for("dwpose", workflow="v1")
    assert s == {
        "dwposeControlnet": STRENGTH_DWPOSE,
        "ipAdapter": STRENGTH_IP_ADAPTER,
    }


def test_cn_strengths_for_lineart_fallback() -> None:
    """Phase 1 lineart-fallback route: LineArt CN @ 0.6 + Scribble CN
    @ 0.6 + IP-Adapter @ 0.8."""
    s = _cn_strengths_for("lineart-fallback", workflow="v1")
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
        _load_workflow_templates("v1", tmp_path)


def test_load_workflow_templates_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "workflow.json").write_text("{not json}", encoding="utf-8")
    (tmp_path / "workflow_lineart_fallback.json").write_text(
        "{}", encoding="utf-8"
    )
    with pytest.raises(WorkflowTemplateError, match="not valid JSON"):
        _load_workflow_templates("v1", tmp_path)


def test_load_workflow_templates_missing_prompt_key(tmp_path: Path) -> None:
    _write_json(tmp_path / "workflow.json", {"hello": "world"})
    _write_json(tmp_path / "workflow_lineart_fallback.json", {"prompt": {}})
    with pytest.raises(WorkflowTemplateError, match="'prompt' key"):
        _load_workflow_templates("v1", tmp_path)


def test_load_workflow_templates_prompt_not_dict(tmp_path: Path) -> None:
    _write_json(tmp_path / "workflow.json", {"prompt": [1, 2]})
    _write_json(tmp_path / "workflow_lineart_fallback.json", {"prompt": {}})
    with pytest.raises(WorkflowTemplateError, match="must be a dict"):
        _load_workflow_templates("v1", tmp_path)


def test_shipped_workflow_templates_load() -> None:
    """The JSON templates we ship in this repo must load cleanly."""
    workflow_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_nodes" / "node_07_pose_refiner"
    )
    templates = _load_workflow_templates("v1", workflow_dir)
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


def _v1_config(tmp_path: Path | None = None) -> OrchestrateConfig:
    """Build a minimal OrchestrateConfig for tests that don't care about
    the file-system paths but need a `config` to pass to the workflow
    parameterizer. `tmp_path` is optional; when None we use sentinel
    paths because these tests never actually open the files.
    """
    p = tmp_path or Path("/dev/null")
    return OrchestrateConfig(
        node6_result_path=p / "node6.json",
        queue_path=p / "queue.json",
        workflow="v1",
        precision="fp16",
    )


def _v2_config(tmp_path: Path | None = None, precision: str = "fp16") -> OrchestrateConfig:
    """Same as `_v1_config` but for Phase 2 v2 tests."""
    p = tmp_path or Path("/dev/null")
    return OrchestrateConfig(
        node6_result_path=p / "node6.json",
        queue_path=p / "queue.json",
        workflow="v2",
        precision=precision,
    )


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
    graph = _parameterize_workflow(_minimal_template(), task, _v1_config())
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
        _parameterize_workflow(template, _make_task(), _v1_config())


def test_parameterize_workflow_deep_copies_input() -> None:
    """Mutating the returned graph must not affect the shared template."""
    tpl = _minimal_template()
    graph = _parameterize_workflow(tpl, _make_task(seed=7), _v1_config())
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


# =====================================================================
# PHASE 2 (locked decisions 2026-04-26) — Flux migration
# =====================================================================
# These tests cover the Phase 2a additions: --workflow {v1,v2} and
# --precision {fp16,fp8} flags; workflow_flux_v2.json template loader;
# v2 routing (DWPreprocessor swap + SetUnionControlNetType); precision
# substitution; manifest schema additions (workflowName + precision +
# characterLoraFilename); CharacterSpec backward-compat; OrchestrateConfig
# validation. Per locked decision #10, EVERY existing Phase 1 test
# above this section must continue to pass — Phase 2 is purely additive.


# ---------------------------------------------------------------
# OrchestrateConfig validation (locked decisions #11 + #13)
# ---------------------------------------------------------------

def test_orchestrate_config_default_workflow_is_v2() -> None:
    """Phase 2c (2026-04-27) flipped the default from v1 to v2 per
    locked decision (Phase 2c roadmap entry: 'flip --workflow default
    to v2'). v1 stays callable via --workflow=v1 for the 6-month
    deprecation window per locked decision #12."""
    assert DEFAULT_WORKFLOW == "v2"
    cfg = OrchestrateConfig(
        node6_result_path=Path("/dev/null"),
        queue_path=Path("/dev/null"),
    )
    assert cfg.workflow == "v2"


def test_orchestrate_config_default_precision_is_fp16() -> None:
    """Locked decision #11: A100 80GB + fp16 default; fp8 is the
    smaller-GPU fallback."""
    assert DEFAULT_PRECISION == "fp16"
    cfg = OrchestrateConfig(
        node6_result_path=Path("/dev/null"),
        queue_path=Path("/dev/null"),
    )
    assert cfg.precision == "fp16"


def test_orchestrate_config_workflow_choices_locked() -> None:
    """The set of valid workflow names is locked at v1 + v2."""
    assert WORKFLOW_CHOICES == ("v1", "v2")


def test_orchestrate_config_precision_choices_locked() -> None:
    """The set of valid precision values is locked at fp16 + fp8."""
    assert PRECISION_CHOICES == ("fp16", "fp8")


def test_orchestrate_config_invalid_workflow_raises() -> None:
    with pytest.raises(ValueError, match="workflow="):
        OrchestrateConfig(
            node6_result_path=Path("/dev/null"),
            queue_path=Path("/dev/null"),
            workflow="v3",
        )


def test_orchestrate_config_invalid_precision_raises() -> None:
    with pytest.raises(ValueError, match="precision="):
        OrchestrateConfig(
            node6_result_path=Path("/dev/null"),
            queue_path=Path("/dev/null"),
            precision="bf16",
        )


# ---------------------------------------------------------------
# v2 workflow template loader
# ---------------------------------------------------------------

def test_load_workflow_templates_v2_returns_v2_only(tmp_path: Path) -> None:
    """v2 mode loads exactly one template under the 'v2' key."""
    _write_json(
        tmp_path / "workflow_flux_v2.json",
        {"prompt": {"10": {"class_type": "UNETLoader", "inputs": {}}}},
    )
    templates = _load_workflow_templates("v2", tmp_path)
    assert set(templates.keys()) == {"v2"}


def test_load_workflow_templates_v2_missing_file(tmp_path: Path) -> None:
    """v2 mode raises if workflow_flux_v2.json is absent."""
    with pytest.raises(WorkflowTemplateError, match="workflow_flux_v2"):
        _load_workflow_templates("v2", tmp_path)


def test_load_workflow_templates_unknown_workflow(tmp_path: Path) -> None:
    with pytest.raises(WorkflowTemplateError, match="Unknown workflow"):
        _load_workflow_templates("v3", tmp_path)


def test_shipped_workflow_flux_v2_loads() -> None:
    """The shipped workflow_flux_v2.json must load cleanly and contain
    every locked Phase 2 node ID (16 from Phase 2a + 3 added in Phase
    2b). Mirrors the Phase 1 test_shipped_workflow_templates_load test."""
    workflow_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_nodes" / "node_07_pose_refiner"
    )
    templates = _load_workflow_templates("v2", workflow_dir)
    assert set(templates.keys()) == {"v2"}
    graph = templates["v2"]
    for node_id in (
        NODE_FLUX_UNET,
        NODE_FLUX_CLIP,
        NODE_FLUX_VAE,
        NODE_FLUX_STYLE_LORA,
        # Phase 2b additions: XLabs Flux IP-Adapter v2 wiring.
        NODE_FLUX_IPADAPTER_LOADER,
        NODE_FLUX_IPADAPTER_REF_IMAGE,
        NODE_FLUX_IPADAPTER_APPLY,
        NODE_FLUX_POS_PROMPT,
        NODE_FLUX_NEG_PROMPT,
        NODE_FLUX_GUIDANCE,
        NODE_FLUX_LOAD_ROUGH,
        NODE_FLUX_POSE_PREPROC,
        NODE_FLUX_CN_LOADER,
        NODE_FLUX_CN_UNION_TYPE,
        NODE_FLUX_CN_APPLY,
        NODE_FLUX_LATENT_INIT,
        NODE_FLUX_KSAMPLER,
        NODE_FLUX_VAE_DECODE,
        NODE_FLUX_SAVE_IMAGE,
    ):
        assert node_id in graph, (
            f"workflow_flux_v2.json missing locked Phase 2 node {node_id!r}"
        )


def test_shipped_workflow_flux_v2_ipadapter_class_types() -> None:
    """Phase 2b + Phase 2d-fixup (2026-04-27): the upstream
    `x-flux-comfyui` repo registers IP-Adapter classes under
    NODE_CLASS_MAPPINGS keys 'LoadFluxIPAdapter' and 'ApplyFluxIPAdapter'
    (no spaces, no typo) — these are the strings ComfyUI's wire format
    expects in workflow JSON's class_type. The 'IPAdatpter' typo is
    upstream's NODE_DISPLAY_NAME_MAPPINGS artifact (the GUI menu label
    only) and does NOT carry into class_type. The original Phase 2b
    commit got this wrong because the agent that researched the API
    confused display names with class_type strings; live-pod debug
    on 2026-04-27 caught it.

    The input FIELD name 'ipadatper' on LoadFluxIPAdapter IS typo'd
    verbatim (that's how XLabs registered the field name in
    INPUT_TYPES) and stays as-is.

    This test pins the exact strings so a future regression that
    reverts to the wrong display-name strings can't silently ship."""
    workflow_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_nodes" / "node_07_pose_refiner"
    )
    templates = _load_workflow_templates("v2", workflow_dir)
    graph = templates["v2"]
    assert graph[NODE_FLUX_IPADAPTER_LOADER]["class_type"] == (
        "LoadFluxIPAdapter"
    )
    # The typo'd input field name must also stay.
    assert "ipadatper" in graph[NODE_FLUX_IPADAPTER_LOADER]["inputs"]
    assert graph[NODE_FLUX_IPADAPTER_APPLY]["class_type"] == (
        "ApplyFluxIPAdapter"
    )


def test_shipped_workflow_flux_v2_ksampler_model_input_wired_to_24() -> None:
    """Phase 2b: KSampler's model input MUST come from node 24 (the
    IP-Adapter wrapped model), not node 20 (the style LoRA output that
    Phase 2a wired). This is the one and only existing-node change in
    the Phase 2b workflow JSON; everything else is additive."""
    workflow_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_nodes" / "node_07_pose_refiner"
    )
    templates = _load_workflow_templates("v2", workflow_dir)
    graph = templates["v2"]
    model_input = graph[NODE_FLUX_KSAMPLER]["inputs"]["model"]
    assert model_input == [NODE_FLUX_IPADAPTER_APPLY, 0], (
        f"Phase 2b KSampler model input must be wired to node "
        f"{NODE_FLUX_IPADAPTER_APPLY!r} (ApplyFluxIPAdapter); "
        f"got {model_input!r}. Phase 2a wired this to node 20 directly; "
        "if you reverted to that, you broke Phase 2b's IP-Adapter."
    )


def test_shipped_workflow_flux_v2_ipadapter_apply_inputs_wired() -> None:
    """Phase 2b: ApplyFluxIPAdapter's three inputs must be wired to
    the right upstream nodes."""
    workflow_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_nodes" / "node_07_pose_refiner"
    )
    templates = _load_workflow_templates("v2", workflow_dir)
    graph = templates["v2"]
    inputs = graph[NODE_FLUX_IPADAPTER_APPLY]["inputs"]
    # model: from style LoRA output (node 20). Phase 2e will insert a
    # character LoRA at node 21 between 20 and 24; until then 20 -> 24.
    assert inputs["model"] == [NODE_FLUX_STYLE_LORA, 0]
    # ip_adapter_flux: from the IP-Adapter loader (node 22).
    assert inputs["ip_adapter_flux"] == [NODE_FLUX_IPADAPTER_LOADER, 0]
    # image: from the reference-color LoadImage (node 23).
    assert inputs["image"] == [NODE_FLUX_IPADAPTER_REF_IMAGE, 0]


# ---------------------------------------------------------------
# v2 parameterization (locked decisions #6, #7, #8, #11)
# ---------------------------------------------------------------

def _minimal_v2_template() -> dict[str, Any]:
    """Bare v2 template that has only the required Phase 2 node IDs
    (16 from Phase 2a + 3 added in Phase 2b), enough to let
    _parameterize_workflow_v2 run to completion. Mirrors the Phase 1
    _minimal_template helper above."""
    return {
        NODE_FLUX_UNET: {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "PLACEHOLDER", "weight_dtype": "default"},
        },
        NODE_FLUX_CLIP: {
            "class_type": "DualCLIPLoader",
            "inputs": {
                "clip_name1": "PLACEHOLDER",
                "clip_name2": "clip_l.safetensors",
                "type": "flux",
            },
        },
        NODE_FLUX_VAE: {"class_type": "VAELoader", "inputs": {}},
        NODE_FLUX_STYLE_LORA: {
            "class_type": "LoraLoader",
            "inputs": {
                "lora_name": "flat_cartoon_style_v12.safetensors",
                "strength_model": 0.5,
                "strength_clip": 0.5,
            },
        },
        # Phase 2b additions: IP-Adapter wiring. class_type strings
        # 'LoadFluxIPAdapter' and 'ApplyFluxIPAdapter' are the INTERNAL
        # NODE_CLASS_MAPPINGS keys from x-flux-comfyui (no spaces, no
        # typo). The 'IPAdatpter' typo is in upstream's
        # NODE_DISPLAY_NAME_MAPPINGS only. The input field name
        # 'ipadatper' IS typo'd verbatim (upstream registered the
        # field name that way in INPUT_TYPES).
        NODE_FLUX_IPADAPTER_LOADER: {
            "class_type": "LoadFluxIPAdapter",
            "inputs": {
                "ipadatper": "flux-ip-adapter-v2.safetensors",
                "clip_vision": "clip-vit-large-patch14.safetensors",
                # Phase 2-revision-fixup-2 (2026-04-28): live-pod test
                # caught Phase 2b's wrong "CUDA" provider value. The
                # x-flux-comfyui LoadFluxIPAdapter registers
                # `"provider": (["CPU", "GPU"],)` in INPUT_TYPES — "CUDA"
                # is not a valid choice. ComfyUI's prompt validator
                # rejects the workflow with "Value not in list:
                # provider: 'CUDA' not in ['CPU', 'GPU']".
                "provider": "GPU",
            },
        },
        NODE_FLUX_IPADAPTER_REF_IMAGE: {
            "class_type": "LoadImage",
            "inputs": {"image": ""},
        },
        NODE_FLUX_IPADAPTER_APPLY: {
            "class_type": "ApplyFluxIPAdapter",
            "inputs": {
                "model": [NODE_FLUX_STYLE_LORA, 0],
                "ip_adapter_flux": [NODE_FLUX_IPADAPTER_LOADER, 0],
                "image": [NODE_FLUX_IPADAPTER_REF_IMAGE, 0],
                "ip_scale": 0.0,  # parameterizer overrides to V2_STRENGTH_IP_ADAPTER
            },
        },
        NODE_FLUX_POS_PROMPT: {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": ""},
        },
        NODE_FLUX_NEG_PROMPT: {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": ""},
        },
        NODE_FLUX_GUIDANCE: {
            "class_type": "FluxGuidance",
            "inputs": {"guidance": 0.0},
        },
        NODE_FLUX_LOAD_ROUGH: {
            "class_type": "LoadImage",
            "inputs": {"image": ""},
        },
        NODE_FLUX_POSE_PREPROC: {
            # Will be REPLACED by the parameterizer per route.
            "_role": "pose-preprocessor",
            "class_type": "PLACEHOLDER",
            "inputs": {},
        },
        NODE_FLUX_CN_LOADER: {
            "class_type": "ControlNetLoader",
            "inputs": {},
        },
        NODE_FLUX_CN_UNION_TYPE: {
            "class_type": "SetUnionControlNetType",
            "inputs": {"type": "PLACEHOLDER"},
        },
        NODE_FLUX_CN_APPLY: {
            "class_type": "ControlNetApplyAdvanced",
            "inputs": {"strength": 0.0},
        },
        NODE_FLUX_LATENT_INIT: {
            "class_type": "EmptySD3LatentImage",
            "inputs": {},
        },
        NODE_FLUX_KSAMPLER: {
            "class_type": "KSampler",
            "inputs": {},
        },
        NODE_FLUX_VAE_DECODE: {"class_type": "VAEDecode", "inputs": {}},
        NODE_FLUX_SAVE_IMAGE: {"class_type": "SaveImage", "inputs": {}},
    }


def test_v2_parameterize_picks_fp16_unet_by_default() -> None:
    """Locked decision #11: --precision fp16 default uses
    flux1-dev-fp16.safetensors."""
    task = _make_task(seed=42)
    cfg = _v2_config(precision="fp16")
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_UNET]["inputs"]["unet_name"] == (
        FLUX_UNET_BY_PRECISION["fp16"]
    )
    assert graph[NODE_FLUX_CLIP]["inputs"]["clip_name1"] == (
        FLUX_T5XXL_BY_PRECISION["fp16"]
    )


def test_v2_parameterize_swaps_to_fp8_unet() -> None:
    """--precision fp8 swaps both the UNET and the T5-XXL filename."""
    task = _make_task(seed=42)
    cfg = _v2_config(precision="fp8")
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_UNET]["inputs"]["unet_name"] == (
        FLUX_UNET_BY_PRECISION["fp8"]
    )
    assert graph[NODE_FLUX_CLIP]["inputs"]["clip_name1"] == (
        FLUX_T5XXL_BY_PRECISION["fp8"]
    )


def test_v2_parameterize_dwpose_route_uses_dwpreprocessor() -> None:
    """Locked decision #3: dwpose route -> DWPreprocessor + openpose
    SetUnionControlNetType."""
    task = _make_task(seed=42)
    # _make_task defaults to poseExtractor='dwpose'.
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_POSE_PREPROC]["class_type"] == "DWPreprocessor"
    assert "detect_hand" in graph[NODE_FLUX_POSE_PREPROC]["inputs"]
    assert graph[NODE_FLUX_CN_UNION_TYPE]["inputs"]["type"] == "openpose"


def test_v2_parameterize_lineart_route_uses_lineart_preprocessor() -> None:
    """Locked decision #3: lineart-fallback route -> LineArtPreprocessor
    + lineart SetUnionControlNetType. Replaces node 51 ENTIRELY because
    LineArtPreprocessor has different inputs than DWPreprocessor."""
    task = DetectionTask(
        shotId="shot_001",
        keyPoseIndex=0,
        keyPoseFilename="frame_0001.png",
        sourceFrame=1,
        identity="Jaggu",
        poseExtractor="lineart-fallback",
        expectedPosition="R",
        boundingBox=(0, 0, 10, 10),
        selectedAngle="profile-R",
        keyPosePath=Path("/tmp/key.png"),
        referenceColorCropPath=Path("/tmp/ref.png"),
        referenceLineArtCropPath=Path("/tmp/ref_line.png"),
        refinedPath=Path("/tmp/refined/000_Jaggu.png"),
        seed=42,
    )
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_POSE_PREPROC]["class_type"] == "LineArtPreprocessor"
    # DWPreprocessor's detect_hand field MUST NOT survive the swap.
    assert "detect_hand" not in graph[NODE_FLUX_POSE_PREPROC]["inputs"]
    assert "coarse" in graph[NODE_FLUX_POSE_PREPROC]["inputs"]
    assert graph[NODE_FLUX_CN_UNION_TYPE]["inputs"]["type"] == "lineart"


def test_v2_parameterize_preserves_role_annotation() -> None:
    """The orchestrator REPLACES node 51 to swap class_type, but it
    must carry the existing _role annotation forward so re-exports
    from the GUI keep the documentation."""
    task = _make_task(seed=42)
    tpl = _minimal_v2_template()
    tpl[NODE_FLUX_POSE_PREPROC]["_role"] = "pose-preprocessor"
    cfg = _v2_config()
    graph = _parameterize_workflow(tpl, task, cfg)
    assert graph[NODE_FLUX_POSE_PREPROC].get("_role") == "pose-preprocessor"


def test_v2_parameterize_locks_sampler_settings() -> None:
    """Locked decision #8: dpmpp_2m_sde + simple + 40 steps + cfg=1.0."""
    task = _make_task(seed=1234)
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    inputs = graph[NODE_FLUX_KSAMPLER]["inputs"]
    assert inputs["seed"] == 1234
    assert inputs["sampler_name"] == V2_SAMPLER_NAME == "dpmpp_2m_sde"
    assert inputs["scheduler"] == V2_SCHEDULER == "simple"
    assert inputs["steps"] == V2_STEPS == 40
    assert inputs["cfg"] == V2_CFG == 1.0


def test_v2_parameterize_locks_flux_guidance() -> None:
    """Locked decision #8: FluxGuidance 4.0 (vs BFL default 3.5)."""
    task = _make_task(seed=42)
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_GUIDANCE]["inputs"]["guidance"] == V2_FLUX_GUIDANCE
    assert V2_FLUX_GUIDANCE == 4.0


def test_v2_parameterize_locks_controlnet_strength() -> None:
    """Locked decision #6: CN 0.65, IP-Adapter 0.8."""
    task = _make_task(seed=42)
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_CN_APPLY]["inputs"]["strength"] == (
        V2_STRENGTH_CONTROLNET
    )
    assert V2_STRENGTH_CONTROLNET == 0.65
    assert V2_STRENGTH_IP_ADAPTER == 0.80


def test_v2_parameterize_uses_v2_prompt_template() -> None:
    """v2 prompts (Phase 2-revision 2026-04-28): BnW line art on white
    background, no color/fill/shading. Replaces Phase 2c's earlier
    colored-output prompts ('flat cartoon style ... bright daytime
    colors ... reject monochrome') which conflicted with Part 1's
    locked spec ('Output color is Black & White line art'). Phase
    2d-run's TMKOC v1 LoRA will reinforce the line aesthetic; until
    then prompts + IP-Adapter alone do the work."""
    task = _make_task(seed=42)
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    pos = graph[NODE_FLUX_POS_PROMPT]["inputs"]["text"]
    # Positive must ask for BnW line art on white BG.
    assert "line art" in pos
    assert "white background" in pos
    assert "no color" in pos
    assert "TMKOC" in pos
    assert "Bhim" in pos
    # Phase 2c colored prompts must be gone.
    assert "flat cartoon style" not in pos, (
        "Phase 2c's color-biased prompt must not survive Phase 2-"
        "revision; expected BnW line-art prompt instead."
    )
    assert "bright daytime colors" not in pos
    # Negative must reject color, fill, scene/background furniture.
    neg = graph[NODE_FLUX_NEG_PROMPT]["inputs"]["text"]
    assert neg == V2_NEGATIVE_PROMPT
    assert "color" in neg
    assert "fill" in neg
    assert "background" in neg
    # Phase 2c rejected "monochrome" — that's exactly what Part 1
    # wants. Must NOT be in the negative anymore.
    assert "monochrome" not in neg, (
        "Phase 2c's 'monochrome' negative biased away from BnW "
        "output; Phase 2-revision must remove it."
    )


def test_v2_parameterize_loads_rough_into_node_50() -> None:
    """Phase 2-revision (2026-04-28) default contract: when called
    WITHOUT a rough_image_override (parameterizer-only unit tests),
    node 50 falls back to ``task.keyPosePath``. Production v2 runs
    pre-crop the keypose to (bbox + margin) in ``_run_one_task`` and
    pass that crop's path via the override — see
    ``test_v2_parameterize_uses_rough_image_override_when_provided``
    for the production path."""
    task = _make_task(seed=42)
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_LOAD_ROUGH]["inputs"]["image"] == str(task.keyPosePath)


def test_v2_parameterize_uses_rough_image_override_when_provided() -> None:
    """Phase 2-revision (2026-04-28): production v2 runs pre-crop the
    keypose to (bbox + margin) before submitting; ``_run_one_task``
    threads the crop's path via ``rough_image_override`` so node 50
    receives a per-character image instead of the whole-frame
    keypose. When the override is None (default), parameterizer
    falls back to ``task.keyPosePath`` (Phase 2c behavior preserved
    for tests). When a string is passed, that path takes precedence."""
    task = _make_task(seed=42)
    cfg = _v2_config()

    crop_path = "/tmp/_crop_000_Bhim.png"
    g_override = _parameterize_workflow(
        _minimal_v2_template(), task, cfg, rough_image_override=crop_path
    )
    assert g_override[NODE_FLUX_LOAD_ROUGH]["inputs"]["image"] == crop_path

    # No override → fall back to keyPosePath (existing contract).
    g_default = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert g_default[NODE_FLUX_LOAD_ROUGH]["inputs"]["image"] == str(task.keyPosePath)
    # Sanity: the override variant is distinct from the no-override
    # variant — confirms the override actually changed node 50.
    assert (
        g_override[NODE_FLUX_LOAD_ROUGH]["inputs"]["image"]
        != g_default[NODE_FLUX_LOAD_ROUGH]["inputs"]["image"]
    )


def test_prepare_rough_bbox_crop_outputs_flux_compatible_dims(
    tmp_path: Path,
) -> None:
    """Phase 2-revision: ``_prepare_rough_bbox_crop`` produces a PNG
    sized to multiples of V2_BBOX_FLUX_MULTIPLE (Flux requires
    multiples of 16) with longest edge ≤ V2_BBOX_TARGET_MAX_EDGE.
    Both dims must be ≥ V2_BBOX_FLUX_MULTIPLE so VAEEncode doesn't
    receive a degenerate canvas."""
    from PIL import Image

    src = tmp_path / "keypose.png"
    Image.new("RGB", (1280, 720), color="white").save(src)

    out = tmp_path / "_crop.png"
    bbox = (100, 100, 400, 500)  # 400x500 bbox at offset (100, 100)

    result = _prepare_rough_bbox_crop(src, bbox, out)
    assert result == out
    assert out.is_file()

    crop = Image.open(out)
    cw, ch = crop.size
    assert cw % V2_BBOX_FLUX_MULTIPLE == 0, (
        f"width {cw} not a multiple of {V2_BBOX_FLUX_MULTIPLE}"
    )
    assert ch % V2_BBOX_FLUX_MULTIPLE == 0, (
        f"height {ch} not a multiple of {V2_BBOX_FLUX_MULTIPLE}"
    )
    assert max(cw, ch) <= V2_BBOX_TARGET_MAX_EDGE
    assert min(cw, ch) >= V2_BBOX_FLUX_MULTIPLE


def test_prepare_rough_bbox_crop_clamps_to_image_bounds(
    tmp_path: Path,
) -> None:
    """If bbox + margin extends outside the image, the crop region
    must clamp to image bounds. No IndexError, no negative-pixel
    region."""
    from PIL import Image

    src = tmp_path / "keypose.png"
    Image.new("RGB", (200, 200), color="white").save(src)

    # bbox at the bottom-right corner — margin would push outside.
    out = tmp_path / "_crop.png"
    bbox = (180, 180, 20, 20)

    result = _prepare_rough_bbox_crop(src, bbox, out)
    assert result == out
    assert out.is_file()


def test_prepare_rough_bbox_crop_raises_when_source_missing(
    tmp_path: Path,
) -> None:
    """Missing keypose PNG raises ``RefinementGenerationError`` with
    a readable message that names the missing path so the operator
    can rerun Node 4."""
    bbox = (0, 0, 10, 10)
    out = tmp_path / "_crop.png"
    with pytest.raises(
        RefinementGenerationError, match="keypose PNG not found"
    ):
        _prepare_rough_bbox_crop(
            keypose_path=tmp_path / "missing.png",
            bbox=bbox,
            output_path=out,
        )


# ---------------------------------------------------------------
# Phase 2f (2026-04-28) — bbox crop reads dark_lines/ when present
# ---------------------------------------------------------------

def test_resolve_dark_lines_source_prefers_dark_lines_when_present(
    tmp_path: Path,
) -> None:
    """Phase 2f: when ``<shot>/dark_lines/<filename>`` exists, the
    bbox crop step reads from there instead of the raw keypose. Gives
    Flux clean character-only pixels with BG furniture lines erased."""
    shot_root = tmp_path / "shot_001"
    keyposes = shot_root / "keyposes"
    dark_lines = shot_root / "dark_lines"
    keyposes.mkdir(parents=True)
    dark_lines.mkdir(parents=True)
    keypose_path = keyposes / "frame_0001.png"
    dark_path = dark_lines / "frame_0001.png"
    keypose_path.write_bytes(b"raw_keypose_placeholder")
    dark_path.write_bytes(b"dark_lines_placeholder")

    resolved = _resolve_dark_lines_source(keypose_path)
    assert resolved == dark_path, (
        "When dark_lines/ exists, _resolve_dark_lines_source must "
        "return the dark_lines path, not the raw keypose."
    )


def test_resolve_dark_lines_source_falls_back_when_missing(
    tmp_path: Path,
) -> None:
    """Phase 2f backward-compat: pre-Phase-2f work dirs don't have
    dark_lines/ — the resolver must fall back to the raw keypose so
    Phase 2-revision's bbox crop still works."""
    shot_root = tmp_path / "shot_001"
    keyposes = shot_root / "keyposes"
    keyposes.mkdir(parents=True)
    keypose_path = keyposes / "frame_0001.png"
    keypose_path.write_bytes(b"raw_keypose_placeholder")
    # Note: dark_lines/ deliberately not created.

    resolved = _resolve_dark_lines_source(keypose_path)
    assert resolved == keypose_path, (
        "When dark_lines/ is missing, fall back to the raw keypose."
    )


def test_prepare_rough_bbox_crop_uses_dark_lines_when_available(
    tmp_path: Path,
) -> None:
    """End-to-end: when dark_lines/<filename> exists, the bbox crop's
    pixel content matches the dark_lines image (all white BG with
    black character outline drawn at the bbox), not the raw keypose
    image. Verifies _prepare_rough_bbox_crop is wired through the
    resolver."""
    from PIL import Image

    shot_root = tmp_path / "shot_001"
    keyposes = shot_root / "keyposes"
    dark_lines = shot_root / "dark_lines"
    refined_dir = shot_root / "refined"
    keyposes.mkdir(parents=True)
    dark_lines.mkdir(parents=True)
    refined_dir.mkdir(parents=True)

    # Raw keypose: all GREY (luminance 128) — would normally be ambiguous.
    raw_arr = np.full((128, 128, 3), 128, dtype=np.uint8)
    raw_path = keyposes / "frame_0001.png"
    Image.fromarray(raw_arr).save(raw_path)

    # dark_lines version: all WHITE (luminance 255) with a black
    # rectangle in the center. This is the "BG-stripped" version
    # Node 5 would write — character outline on clean white BG.
    dark_arr = np.full((128, 128, 3), 255, dtype=np.uint8)
    dark_arr[40:80, 40:80] = 0  # black 40x40 character rect
    dark_path = dark_lines / "frame_0001.png"
    Image.fromarray(dark_arr).save(dark_path)

    bbox = (40, 40, 40, 40)
    out_path = refined_dir / "_crop.png"
    _prepare_rough_bbox_crop(raw_path, bbox, out_path)

    # The crop should contain the dark_lines content, not the raw grey.
    crop = np.asarray(Image.open(out_path).convert("L"), dtype=np.uint8)
    # Center of crop should be 0 (black character) since dark_lines/ wins.
    cy, cx = crop.shape[0] // 2, crop.shape[1] // 2
    assert crop[cy, cx] < 50, (
        f"Expected dark center (from dark_lines/, ~0); got {crop[cy, cx]}. "
        "This means the resolver fell back to the raw keypose (grey 128) "
        "instead of using the dark_lines version."
    )


def test_orchestrate_imports_safe_segment_from_manifest() -> None:
    """Phase 2-revision regression (caught 2026-04-28 on first live-pod
    smoke test): ``_run_one_task`` uses ``_safe_segment(task.identity)``
    when constructing the bbox-crop filename. ``orchestrate.py`` must
    import ``_safe_segment`` from ``manifest.py`` — Phase 2-revision
    initially missed the import because the parameterizer-only unit
    tests never exercised the ``_run_one_task`` non-dry-run code path
    where ``_safe_segment`` is actually called.

    Regression guard: assert ``_safe_segment`` is reachable through the
    orchestrate module's namespace. If this test fails, every live
    ComfyUI submission will hit ``NameError: _safe_segment is not
    defined`` at the bbox-crop step, just like the 2026-04-28 pod run."""
    from custom_nodes.node_07_pose_refiner import orchestrate
    assert hasattr(orchestrate, "_safe_segment"), (
        "orchestrate.py must import _safe_segment from manifest.py — "
        "_run_one_task calls it for the bbox crop filename and dies "
        "with NameError otherwise on the live (non-dry-run) path."
    )
    # Spot-check the function actually does what manifest._safe_segment does.
    assert orchestrate._safe_segment("Tappu") == "Tappu"
    assert orchestrate._safe_segment("a/b") == "a_b"


def test_prepare_rough_bbox_crop_falls_back_to_raw_when_no_dark_lines(
    tmp_path: Path,
) -> None:
    """End-to-end backward-compat: pre-Phase-2f work dirs have no
    dark_lines/ — _prepare_rough_bbox_crop must still produce a valid
    crop from the raw keypose."""
    from PIL import Image

    shot_root = tmp_path / "shot_001"
    keyposes = shot_root / "keyposes"
    refined_dir = shot_root / "refined"
    keyposes.mkdir(parents=True)
    refined_dir.mkdir(parents=True)

    raw_arr = np.full((128, 128, 3), 200, dtype=np.uint8)
    raw_arr[40:80, 40:80] = 50  # darker rect
    raw_path = keyposes / "frame_0001.png"
    Image.fromarray(raw_arr).save(raw_path)

    bbox = (40, 40, 40, 40)
    out_path = refined_dir / "_crop.png"
    result = _prepare_rough_bbox_crop(raw_path, bbox, out_path)
    assert result == out_path
    assert out_path.is_file(), (
        "Crop must be produced even without dark_lines/ (backward compat)."
    )


def test_v2_parameterize_locks_style_lora_strength_per_lora() -> None:
    """Locked decision #2: style LoRA strength 0.75 (production value).
    Phase 2-revision (2026-04-28): per-LoRA strength override via
    STYLE_LORA_STRENGTHS — flat_cartoon_v12 → 0.0 (bypass; biases
    toward color, conflicts with Part 1's BnW line-art deliverable),
    tmkoc_v1 → 0.75 (locked decision #2 production). The locked
    decision survives intact for the LoRA we actually want to use;
    the placeholder LoRA is bypassed without removing the chain.
    """
    task = _make_task(seed=42)

    # Default config uses flat_cartoon_v12 → strength 0.0 (Phase
    # 2-revision bypass).
    cfg_default = _v2_config()
    g_default = _parameterize_workflow(
        _minimal_v2_template(), task, cfg_default
    )
    assert g_default[NODE_FLUX_STYLE_LORA]["inputs"]["strength_model"] == 0.0
    assert g_default[NODE_FLUX_STYLE_LORA]["inputs"]["strength_clip"] == 0.0

    # tmkoc_v1 → strength 0.75 (locked decision #2 production value;
    # picked up automatically once the custom-trained LoRA ships).
    cfg_tmkoc = OrchestrateConfig(
        node6_result_path=Path("/dev/null") / "n6.json",
        queue_path=Path("/dev/null") / "q.json",
        workflow="v2",
        precision="fp16",
        style_lora="tmkoc_v1",
    )
    g_tmkoc = _parameterize_workflow(
        _minimal_v2_template(), task, cfg_tmkoc
    )
    assert g_tmkoc[NODE_FLUX_STYLE_LORA]["inputs"]["strength_model"] == 0.75
    assert g_tmkoc[NODE_FLUX_STYLE_LORA]["inputs"]["strength_clip"] == 0.75

    # Backward-compat: V2_STYLE_LORA_STRENGTH still equals the locked
    # production strength (the value applied to tmkoc_v1).
    assert V2_STYLE_LORA_STRENGTH == 0.75
    assert STYLE_LORA_STRENGTHS["flat_cartoon_v12"] == 0.0
    assert STYLE_LORA_STRENGTHS["tmkoc_v1"] == 0.75


def test_phase_2_revision_per_lora_strength_table_complete() -> None:
    """Phase 2-revision (2026-04-28): every choice in
    STYLE_LORA_CHOICES must have an entry in STYLE_LORA_STRENGTHS;
    a missing entry would raise KeyError at parameterize time."""
    for choice in STYLE_LORA_CHOICES:
        assert choice in STYLE_LORA_STRENGTHS, (
            f"--style-lora={choice} in choices but missing from "
            "STYLE_LORA_STRENGTHS table."
        )


def test_v2_parameterize_default_style_lora_is_flat_cartoon() -> None:
    """Phase 2d-prep default --style-lora is flat_cartoon_v12 (the
    Phase 2a generic style LoRA). Stays the default until Phase 2d's
    custom-trained tmkoc_v1 LoRA actually ships and operators flip the
    flag explicitly."""
    task = _make_task(seed=42)
    cfg = _v2_config()  # uses DEFAULT_STYLE_LORA
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_STYLE_LORA]["inputs"]["lora_name"] == (
        "flat_cartoon_style_v12.safetensors"
    )
    assert DEFAULT_STYLE_LORA == "flat_cartoon_v12"


def test_v2_parameterize_swaps_to_tmkoc_v1_style_lora() -> None:
    """Phase 2d-prep: --style-lora=tmkoc_v1 swaps node 20's lora_name
    to the custom-trained TMKOC v1 LoRA. Until Phase 2d's training
    run lands a real weight, this swap will fail at ComfyUI submission
    with a missing-file error from LoraLoader — but the orchestrator
    parameterization itself is fully wired."""
    task = _make_task(seed=42)
    cfg = OrchestrateConfig(
        node6_result_path=Path("/dev/null") / "n6.json",
        queue_path=Path("/dev/null") / "q.json",
        workflow="v2",
        precision="fp16",
        style_lora="tmkoc_v1",
    )
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_STYLE_LORA]["inputs"]["lora_name"] == (
        "tmkoc_style_v1.safetensors"
    )


def test_orchestrate_config_style_lora_choices_locked() -> None:
    """The set of valid --style-lora values is locked at flat_cartoon_v12 +
    tmkoc_v1 for Phase 2d-prep. Future Phase 2 LoRA additions
    (e.g. character LoRAs in 2e) get their own flag/field, not new
    entries here."""
    assert STYLE_LORA_CHOICES == ("flat_cartoon_v12", "tmkoc_v1")


def test_orchestrate_config_style_lora_filenames_match_locked_destinations() -> None:
    """Filenames in STYLE_LORA_FILENAMES must match models.json
    destinations exactly so ComfyUI's LoraLoader can find the weight.
    This is a contract between orchestrate.py and models.json."""
    assert STYLE_LORA_FILENAMES["flat_cartoon_v12"] == (
        "flat_cartoon_style_v12.safetensors"
    )
    assert STYLE_LORA_FILENAMES["tmkoc_v1"] == "tmkoc_style_v1.safetensors"


def test_orchestrate_config_invalid_style_lora_raises() -> None:
    """OrchestrateConfig.__post_init__ rejects --style-lora values
    not in STYLE_LORA_CHOICES so an operator typo fails loudly."""
    with pytest.raises(ValueError, match="style_lora="):
        OrchestrateConfig(
            node6_result_path=Path("/dev/null"),
            queue_path=Path("/dev/null"),
            style_lora="future_lora_v3",
        )


def test_cli_accepts_style_lora_tmkoc_v1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Phase 2d-prep CLI: --style-lora=tmkoc_v1 dry-run accepted; the
    success line reports it so the operator sees the chosen LoRA."""
    paths = _build_fixture(tmp_path)
    code = cli_main([
        "--node6-result", str(paths["node6_result_path"]),
        "--queue", str(paths["queue_path"]),
        "--dry-run",
        "--style-lora", "tmkoc_v1",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "style_lora=tmkoc_v1" in captured.out


def test_cli_rejects_invalid_style_lora(tmp_path: Path) -> None:
    """Argparse must reject unknown --style-lora values per
    STYLE_LORA_CHOICES."""
    paths = _build_fixture(tmp_path)
    with pytest.raises(SystemExit):
        cli_main([
            "--node6-result", str(paths["node6_result_path"]),
            "--queue", str(paths["queue_path"]),
            "--dry-run",
            "--style-lora", "future_lora_v3",
        ])


def test_cli_default_workflow_v2_shows_style_lora_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Phase 2c flipped --workflow default to v2; Phase 2d-prep added
    style-lora to the v2 success line. With no flags, the success line
    reports `workflow=v2 precision=fp16 style_lora=flat_cartoon_v12`."""
    paths = _build_fixture(tmp_path)
    code = cli_main([
        "--node6-result", str(paths["node6_result_path"]),
        "--queue", str(paths["queue_path"]),
        "--dry-run",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "workflow=v2" in captured.out
    assert "precision=fp16" in captured.out
    assert "style_lora=flat_cartoon_v12" in captured.out


def test_v2_parameterize_filename_prefix_includes_shot_and_keypose() -> None:
    """SaveImage filename_prefix follows the v1 convention so Node 8
    can find the output PNG via the same path math."""
    task = _make_task(seed=42)
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_SAVE_IMAGE]["inputs"]["filename_prefix"] == (
        "animatic/shot_001/000_Bhim"
    )


def test_v2_parameterize_unknown_route_raises() -> None:
    """A future poseExtractor value (not 'dwpose' or 'lineart-fallback')
    must fail loudly with a WorkflowTemplateError that names the
    unknown route."""
    task = DetectionTask(
        shotId="shot_001",
        keyPoseIndex=0,
        keyPoseFilename="frame_0001.png",
        sourceFrame=1,
        identity="X",
        poseExtractor="future-route",
        expectedPosition="C",
        boundingBox=(0, 0, 10, 10),
        selectedAngle="front",
        keyPosePath=Path("/tmp/k.png"),
        referenceColorCropPath=Path("/tmp/r.png"),
        referenceLineArtCropPath=Path("/tmp/rl.png"),
        refinedPath=Path("/tmp/r/0_X.png"),
        seed=1,
    )
    cfg = _v2_config()
    with pytest.raises(WorkflowTemplateError, match="future-route"):
        _parameterize_workflow(_minimal_v2_template(), task, cfg)


def test_v2_parameterize_missing_node_raises() -> None:
    """A re-exported workflow_flux_v2.json that drops one of the locked
    Phase 2 IDs must fail loudly per locked decision #13."""
    tpl = _minimal_v2_template()
    del tpl[NODE_FLUX_KSAMPLER]
    cfg = _v2_config()
    with pytest.raises(WorkflowTemplateError, match="KSampler"):
        _parameterize_workflow(tpl, _make_task(), cfg)


def test_v2_dimensions_are_multiples_of_16() -> None:
    """Locked decision #7: 1280x720 native; both clean multiples of 16."""
    assert V2_WIDTH == 1280 and V2_WIDTH % 16 == 0
    assert V2_HEIGHT == 720 and V2_HEIGHT % 16 == 0


# ---------------------------------------------------------------
# v2 _cn_strengths_for
# ---------------------------------------------------------------

def test_cn_strengths_for_v2_returns_union_strength() -> None:
    """v2 has a single ControlNet Union Pro at 0.65 (not two separate
    SD 1.5 ControlNets like v1's lineart-fallback)."""
    s = _cn_strengths_for("dwpose", workflow="v2")
    assert s == {
        "controlnetUnion": V2_STRENGTH_CONTROLNET,
        "ipAdapter": V2_STRENGTH_IP_ADAPTER,
    }
    # Same for the lineart-fallback route -- v2 routes via
    # SetUnionControlNetType, not via separate CN models.
    s = _cn_strengths_for("lineart-fallback", workflow="v2")
    assert s == {
        "controlnetUnion": V2_STRENGTH_CONTROLNET,
        "ipAdapter": V2_STRENGTH_IP_ADAPTER,
    }


def test_cn_strengths_for_v1_explicit_returns_v1_split() -> None:
    """When workflow='v1' is passed explicitly, _cn_strengths_for
    returns the Phase 1 split (DWPose CN 0.75 OR LineArt+Scribble
    CN 0.6 each, plus IP-Adapter 0.8). Phase 2c flipped
    DEFAULT_WORKFLOW so the no-arg behaviour is now v2; callers that
    want v1 strengths must say so explicitly."""
    assert _cn_strengths_for("dwpose", workflow="v1") == {
        "dwposeControlnet": STRENGTH_DWPOSE,
        "ipAdapter": STRENGTH_IP_ADAPTER,
    }
    assert _cn_strengths_for("lineart-fallback", workflow="v1") == {
        "lineartControlnet": STRENGTH_LINEART,
        "scribbleControlnet": STRENGTH_SCRIBBLE,
        "ipAdapter": STRENGTH_IP_ADAPTER,
    }


def test_cn_strengths_for_default_returns_v2_split() -> None:
    """Phase 2c (2026-04-27): DEFAULT_WORKFLOW flipped to 'v2', so
    _cn_strengths_for with no workflow arg returns the v2 split
    (single ControlNet Union @ 0.65 + IP-Adapter @ 0.8)."""
    assert _cn_strengths_for("dwpose") == {
        "controlnetUnion": V2_STRENGTH_CONTROLNET,
        "ipAdapter": V2_STRENGTH_IP_ADAPTER,
    }


# ---------------------------------------------------------------
# RefinedGeneration manifest schema (Phase 2 additions)
# ---------------------------------------------------------------

def test_refined_generation_phase1_defaults_round_trip() -> None:
    """Locked decision #14: a Phase 1 generation built without the new
    Phase 2 fields gets sensible defaults so the on-disk shape doesn't
    change for v1 runs."""
    g = RefinedGeneration(
        identity="Bhim",
        keyPoseIndex=0,
        sourceFrame=1,
        selectedAngle="front",
        poseExtractor="dwpose",
        seed=42,
        refinedPath="/tmp/0_Bhim.png",
        boundingBox=[0, 0, 10, 10],
        status="ok",
    )
    d = g.to_dict()
    # Phase 2 fields present with Phase 1 defaults.
    assert d["workflowName"] == "v1"
    assert d["precision"] == "fp8"
    assert d["characterLoraFilename"] is None


def test_refined_generation_phase2_records_fields() -> None:
    """v2 generations record workflowName + precision + characterLoraFilename
    so failure-pattern diagnosis can cross-reference."""
    g = RefinedGeneration(
        identity="Tappu",
        keyPoseIndex=0,
        sourceFrame=1,
        selectedAngle="front",
        poseExtractor="dwpose",
        seed=42,
        refinedPath="/tmp/0_Tappu.png",
        boundingBox=[0, 0, 10, 10],
        status="ok",
        workflowName="v2",
        precision="fp16",
        characterLoraFilename="TAPPU_v1.safetensors",
    )
    d = g.to_dict()
    assert d["workflowName"] == "v2"
    assert d["precision"] == "fp16"
    assert d["characterLoraFilename"] == "TAPPU_v1.safetensors"


# ---------------------------------------------------------------
# Dry-run records workflow + precision
# ---------------------------------------------------------------

def test_dry_run_records_workflow_v2_in_skipped_generation(
    tmp_path: Path,
) -> None:
    """A dry-run with --workflow=v2 should mark the skipped generations
    with workflowName='v2' so the manifest can be diffed against a
    later live run."""
    paths = _build_fixture(tmp_path)
    # Build a dummy workflow_flux_v2.json so _load_workflow_templates
    # passes -- dry-run never actually parameterizes the graph.
    wf_dir = paths["node6_result_path"].parent
    # Copy our shipped template so the dry-run loader is happy.
    shipped = (
        Path(__file__).resolve().parent.parent
        / "custom_nodes" / "node_07_pose_refiner"
        / "workflow_flux_v2.json"
    )
    config = OrchestrateConfig(
        node6_result_path=paths["node6_result_path"],
        queue_path=paths["queue_path"],
        dry_run=True,
        workflow="v2",
        precision="fp16",
        workflow_dir=shipped.parent,
    )
    result = refine_queue(config)
    # Read back refined_map.json for the sole shot in the fixture.
    refined_map = json.loads(
        (paths["shot_root"] / "refined_map.json").read_text(encoding="utf-8")
    )
    gens = refined_map["generations"]
    assert len(gens) >= 1
    for g in gens:
        assert g["status"] == "skipped"
        assert g["workflowName"] == "v2"
        assert g["precision"] == "fp16"


def test_dry_run_v1_records_v1_in_skipped_generation(tmp_path: Path) -> None:
    """A dry-run with --workflow=v1 (Phase 1 path, still callable for
    the deprecation window per locked decision #12) records
    workflowName='v1' + precision='fp8' so Phase 1 manifests still
    look exactly the same on disk. Phase 2c flipped the default to v2
    so this test now passes workflow='v1' explicitly."""
    paths = _build_fixture(tmp_path)
    config = OrchestrateConfig(
        node6_result_path=paths["node6_result_path"],
        queue_path=paths["queue_path"],
        dry_run=True,
        workflow="v1",
        # precision left at default — ignored by v1 anyway. Per locked
        # decision #14: v1 records always say "fp8" as the canonical
        # Phase 1 precision label regardless of --precision.
    )
    result = refine_queue(config)
    refined_map = json.loads(
        (paths["shot_root"] / "refined_map.json").read_text(encoding="utf-8")
    )
    for g in refined_map["generations"]:
        assert g["workflowName"] == "v1"
        assert g["precision"] == "fp8"
        assert g["characterLoraFilename"] is None


# ---------------------------------------------------------------
# CLI flag plumbing
# ---------------------------------------------------------------

def test_cli_accepts_workflow_v2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _build_fixture(tmp_path)
    code = cli_main([
        "--node6-result", str(paths["node6_result_path"]),
        "--queue", str(paths["queue_path"]),
        "--dry-run",
        "--workflow", "v2",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "workflow=v2" in captured.out


def test_cli_accepts_precision_fp8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _build_fixture(tmp_path)
    code = cli_main([
        "--node6-result", str(paths["node6_result_path"]),
        "--queue", str(paths["queue_path"]),
        "--dry-run",
        "--workflow", "v2",
        "--precision", "fp8",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "precision=fp8" in captured.out


def test_cli_rejects_invalid_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _build_fixture(tmp_path)
    with pytest.raises(SystemExit):
        cli_main([
            "--node6-result", str(paths["node6_result_path"]),
            "--queue", str(paths["queue_path"]),
            "--dry-run",
            "--workflow", "v3",
        ])


def test_cli_rejects_invalid_precision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _build_fixture(tmp_path)
    with pytest.raises(SystemExit):
        cli_main([
            "--node6-result", str(paths["node6_result_path"]),
            "--queue", str(paths["queue_path"]),
            "--dry-run",
            "--precision", "bf16",
        ])


def test_cli_workflow_v2_is_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Phase 2c (2026-04-27) flipped the CLI default from v1 to v2.
    The success line now reports workflow=v2 + precision=<value> (the
    precision suffix is only shown for v2 because the field is
    ignored under v1)."""
    paths = _build_fixture(tmp_path)
    code = cli_main([
        "--node6-result", str(paths["node6_result_path"]),
        "--queue", str(paths["queue_path"]),
        "--dry-run",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "workflow=v2" in captured.out
    # Phase 2c: precision shows up in the v2 success line (default fp16).
    assert "precision=fp16" in captured.out


def test_cli_workflow_v1_still_callable_via_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Locked decision #12: Phase 1 stays callable via --workflow=v1
    for the 6-month deprecation window after Phase 2c flipped the
    default. This test guards against accidental Phase 1 removal
    before the cleanup commit."""
    paths = _build_fixture(tmp_path)
    code = cli_main([
        "--node6-result", str(paths["node6_result_path"]),
        "--queue", str(paths["queue_path"]),
        "--dry-run",
        "--workflow", "v1",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "workflow=v1" in captured.out
    # v1 success line hides precision (the flag is ignored under v1).
    assert "precision=" not in captured.out


# ---------------------------------------------------------------
# Phase 1 fixtures still validate through Phase 2 schemas (decision #10)
# ---------------------------------------------------------------

def test_v2_parameterize_loads_reference_color_into_node_23() -> None:
    """Phase 2b: the IP-Adapter receives Node 6's COLOR reference crop
    (locked decision #4 — IP-Adapter expects a textured/colored image,
    NOT the DoG line-art crop). Orchestrator routes
    task.referenceColorCropPath into node 23's image field."""
    task = _make_task(seed=42)
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_IPADAPTER_REF_IMAGE]["inputs"]["image"] == (
        str(task.referenceColorCropPath)
    )


def test_v2_parameterize_locks_ip_scale_to_locked_default() -> None:
    """Phase 2b: ip_scale is locked at V2_STRENGTH_IP_ADAPTER (0.8 per
    decision #6). Re-asserted by the parameterizer so a hand-edited
    JSON can't silently drift to XLabs's 0.93 default."""
    task = _make_task(seed=42)
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_IPADAPTER_APPLY]["inputs"]["ip_scale"] == (
        V2_STRENGTH_IP_ADAPTER
    )
    assert V2_STRENGTH_IP_ADAPTER == 0.8


def test_v2_parameterize_does_not_swap_lineart_color_paths() -> None:
    """Sanity check: node 50 (rough) gets keyPosePath; node 23
    (reference) gets referenceColorCropPath; the two LoadImage nodes
    must not be swapped. A regression here would silently inject the
    rough animatic pixels into the IP-Adapter — exactly the failure
    mode locked decision #4 calls out (IP-Adapter wants color, not
    line-art / scribbles)."""
    task = _make_task(seed=42)
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    rough_image = graph[NODE_FLUX_LOAD_ROUGH]["inputs"]["image"]
    ref_image = graph[NODE_FLUX_IPADAPTER_REF_IMAGE]["inputs"]["image"]
    assert rough_image == str(task.keyPosePath)
    assert ref_image == str(task.referenceColorCropPath)
    assert rough_image != ref_image, (
        "Node 50 (rough) and node 23 (reference) must point at "
        "different images per locked decision #4."
    )


def test_v2_parameterize_locks_denoise_to_055() -> None:
    """Phase 2c: img2img denoise locked at V2_DENOISE (0.55) per locked
    decision #5. Re-asserted by the parameterizer so a hand-edited JSON
    can't silently revert to txt2img's denoise=1.0 default."""
    task = _make_task(seed=42)
    cfg = _v2_config()
    graph = _parameterize_workflow(_minimal_v2_template(), task, cfg)
    assert graph[NODE_FLUX_KSAMPLER]["inputs"]["denoise"] == V2_DENOISE
    assert V2_DENOISE == 0.55


def test_shipped_workflow_flux_v2_node_80_is_vaeencode() -> None:
    """Phase 2c: node 80 swapped from EmptySD3LatentImage (txt2img) to
    VAEEncode (img2img) per locked decision #5. Wired to take pixels
    from node 50 (rough crop) and the Flux VAE from node 12. Regression
    guard against accidentally reverting to txt2img during a workflow
    re-export."""
    workflow_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_nodes" / "node_07_pose_refiner"
    )
    templates = _load_workflow_templates("v2", workflow_dir)
    graph = templates["v2"]
    n80 = graph[NODE_FLUX_LATENT_INIT]
    assert n80["class_type"] == "VAEEncode", (
        f"Phase 2c node 80 must be VAEEncode (img2img mode); "
        f"got class_type={n80['class_type']!r}. If this reverted to "
        "EmptySD3LatentImage you broke Phase 2c's img2img switch."
    )
    assert n80["inputs"]["pixels"] == [NODE_FLUX_LOAD_ROUGH, 0]
    assert n80["inputs"]["vae"] == [NODE_FLUX_VAE, 0]


def test_shipped_workflow_flux_v2_ipadapter_provider_is_valid() -> None:
    """Phase 2-revision-fixup-2 (2026-04-28, post-live-pod-debug):
    LoadFluxIPAdapter's `provider` input must be one of x-flux-comfyui's
    registered values, which are ['CPU', 'GPU']. Phase 2b shipped
    `"CUDA"` here, which ComfyUI's prompt validator rejects with
    "Value not in list: provider: 'CUDA' not in ['CPU', 'GPU']". The
    fix is `"GPU"` — that's what tells x-flux-comfyui to load the
    IP-Adapter onto the GPU.

    Regression guard: this test only checks the shipped JSON has a
    valid value. The string "CUDA" in particular MUST NOT come back
    via a workflow re-export from a hypothetical fork that re-added
    the historic value.
    """
    workflow_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_nodes" / "node_07_pose_refiner"
    )
    templates = _load_workflow_templates("v2", workflow_dir)
    graph = templates["v2"]
    n22 = graph[NODE_FLUX_IPADAPTER_LOADER]
    provider = n22["inputs"].get("provider")
    assert provider in ("CPU", "GPU"), (
        f"LoadFluxIPAdapter provider must be 'CPU' or 'GPU' "
        f"(x-flux-comfyui INPUT_TYPES); got {provider!r}. If you see "
        "'CUDA' here, you regressed Phase 2-revision-fixup-2."
    )
    assert provider != "CUDA", (
        "Provider 'CUDA' triggers ComfyUI prompt-validation rejection."
    )


def test_shipped_workflow_flux_v2_node_20_style_lora_strength_is_zero() -> None:
    """Phase 2-revision (2026-04-28): shipped workflow_flux_v2.json
    has node 20's strength_model + strength_clip at 0.0. The generic
    Flat Cartoon Style v1.2 LoRA biases toward color, conflicting
    with Part 1's BnW deliverable, so it's bypassed at strength 0
    (LoRA still loads — LoraLoader can't be skipped without rewiring
    nodes 30 + 24). orchestrate.py's STYLE_LORA_STRENGTHS
    re-asserts this per-LoRA at parameterize time so a hand-edited
    JSON can't silently revert to Phase 2c's 0.75. Phase 2d-run will
    flip --style-lora=tmkoc_v1 (strength 0.75) once the custom
    line-art LoRA ships; orchestrate.py's per-LoRA table picks
    the right strength automatically.
    """
    workflow_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_nodes" / "node_07_pose_refiner"
    )
    templates = _load_workflow_templates("v2", workflow_dir)
    graph = templates["v2"]
    n20 = graph[NODE_FLUX_STYLE_LORA]
    assert n20["inputs"]["strength_model"] == 0.0, (
        f"Phase 2-revision shipped node 20 strength_model must be 0.0; "
        f"got {n20['inputs']['strength_model']!r}. If this drifted to "
        "0.75 you broke Phase 2-revision's Flat Cartoon LoRA bypass."
    )
    assert n20["inputs"]["strength_clip"] == 0.0


def test_shipped_workflow_flux_v2_ksampler_denoise_is_055() -> None:
    """Phase 2c: shipped workflow_flux_v2.json has KSampler.denoise at
    0.55 per locked decision #5. Regression guard against the JSON
    drifting back to denoise=1.0."""
    workflow_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_nodes" / "node_07_pose_refiner"
    )
    templates = _load_workflow_templates("v2", workflow_dir)
    graph = templates["v2"]
    assert graph[NODE_FLUX_KSAMPLER]["inputs"]["denoise"] == 0.55


def test_v2_parameterize_missing_ipadapter_node_raises() -> None:
    """Re-exporting the workflow JSON without one of the Phase 2b
    nodes (22, 23, or 24) must fail loudly with a WorkflowTemplateError
    naming the missing node — operator can re-pin to the canonical
    file in git or update the orchestrator constants. Phase 2d-fixup
    updated the human_name string from 'Load Flux IPAdatpter (sic)'
    (which was the GUI display name) to 'LoadFluxIPAdapter' (the
    actual class_type)."""
    tpl = _minimal_v2_template()
    del tpl[NODE_FLUX_IPADAPTER_LOADER]
    cfg = _v2_config()
    with pytest.raises(WorkflowTemplateError, match="LoadFluxIPAdapter"):
        _parameterize_workflow(tpl, _make_task(), cfg)


def test_phase1_refined_map_loads_through_phase2_reader() -> None:
    """A Phase 1 refined_map.json (no workflowName / precision /
    characterLoraFilename fields) deserializes through Phase 2's
    RefinedGeneration without exception. The Phase 2 reader supplies
    the locked Phase 1 defaults."""
    phase1_record = {
        "identity": "Bhim",
        "keyPoseIndex": 0,
        "sourceFrame": 1,
        "selectedAngle": "front",
        "poseExtractor": "dwpose",
        "seed": 42,
        "refinedPath": "/tmp/0_Bhim.png",
        "boundingBox": [0, 0, 10, 10],
        "status": "ok",
        "errorMessage": "",
        "cnStrengths": {"dwposeControlnet": 0.75, "ipAdapter": 0.8},
        # Note: NO workflowName / precision / characterLoraFilename --
        # this is what a 2026-04-25 Phase 1 manifest looks like.
    }
    g = RefinedGeneration(**phase1_record)
    # Defaults filled in.
    assert g.workflowName == "v1"
    assert g.precision == "fp8"
    assert g.characterLoraFilename is None
