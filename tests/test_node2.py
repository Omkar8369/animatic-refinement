"""Tests for Node 2 — Metadata Ingestion & Validation.

One test per documented failure mode plus a happy-path test. Each test
builds a fresh input directory inside pytest's tmp_path so no test can
leak state into another.

Run from repo root with:

    python -m pytest tests/ -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.cli import main as cli_main
from pipeline.errors import (
    CrossReferenceError,
    DuplicateShotIdError,
    MissingInputError,
    Node2Error,
    SchemaValidationError,
    ShotIdSequenceError,
)
from pipeline.node2 import serialize_queue, validate_and_build_queue


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------

def _base_characters_json() -> dict:
    return {
        "schemaVersion": 1,
        "generatedAt": "2026-04-23T00:00:00.000Z",
        "conventions": {
            "sheetFormat": "8-angle horizontal strip",
            "backgroundExpected": "transparent or solid; sliced via alpha-island bbox in Node 6",
            "angleOrderLeftToRight": [
                "back", "back-3q-L", "profile-L", "front-3q-L",
                "front", "front-3q-R", "profile-R", "back-3q-R",
            ],
            "angleOrderConfirmed": False,
        },
        "characters": [
            {
                "name": "Bhim",
                "sheetFilename": "bhim_sheet.png",
                "width": 4096,
                "height": 512,
                "quality": {
                    "ok": True, "detectedIslands": 8,
                    "backgroundMode": "transparent", "reasons": [],
                },
                "addedAt": "2026-04-23T00:00:00.000Z",
            },
            {
                "name": "Chutki",
                "sheetFilename": "chutki_sheet.png",
                "width": 4096,
                "height": 512,
                "quality": {
                    "ok": True, "detectedIslands": 8,
                    "backgroundMode": "transparent", "reasons": [],
                },
                "addedAt": "2026-04-23T00:00:00.000Z",
            },
        ],
    }


def _base_metadata_json() -> dict:
    return {
        "schemaVersion": 1,
        "generatedAt": "2026-04-23T00:00:00.000Z",
        "project": {
            "name": "ChhotaBhim_Ep042",
            "batchSize": 4,
            "fps": 25,
            "notes": "",
        },
        "shots": [
            {
                "shotId": "shot_001",
                "mp4Filename": "scene01_shot01.mp4",
                "durationFrames": 75,
                "durationSeconds": 3.0,
                "characterCount": 2,
                "characters": [
                    {"identity": "Bhim",   "position": "CL"},
                    {"identity": "Chutki", "position": "CR"},
                ],
            },
            {
                "shotId": "shot_002",
                "mp4Filename": "scene01_shot02.mp4",
                "durationFrames": 50,
                "durationSeconds": 2.0,
                "characterCount": 1,
                "characters": [
                    {"identity": "Bhim", "position": "C"},
                ],
            },
        ],
    }


def _write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


@pytest.fixture
def valid_inputs(tmp_path: Path) -> Path:
    """A well-formed input directory with everything Node 2 expects."""
    # Placeholder binary content — Node 2 only checks existence, not format.
    (tmp_path / "bhim_sheet.png").write_bytes(b"\x89PNG\r\n\x1a\n...")
    (tmp_path / "chutki_sheet.png").write_bytes(b"\x89PNG\r\n\x1a\n...")
    (tmp_path / "scene01_shot01.mp4").write_bytes(b"fake mp4")
    (tmp_path / "scene01_shot02.mp4").write_bytes(b"fake mp4")
    _write(tmp_path / "characters.json", _base_characters_json())
    _write(tmp_path / "metadata.json", _base_metadata_json())
    return tmp_path


# ---------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------

class TestHappyPath:
    def test_valid_inputs_produce_queue(self, valid_inputs: Path):
        q = validate_and_build_queue(valid_inputs)
        assert q.projectName == "ChhotaBhim_Ep042"
        assert q.batchSize == 4
        assert q.totalShots == 2
        assert len(q.batches) == 1  # 2 shots, batch size 4 -> single batch
        assert [j.shotId for j in q.batches[0]] == ["shot_001", "shot_002"]

    def test_characters_resolve_to_sheet_paths(self, valid_inputs: Path):
        q = validate_and_build_queue(valid_inputs)
        shot1 = q.batches[0][0]
        bhim = next(c for c in shot1.characters if c.identity == "Bhim")
        assert bhim.sheetPath == valid_inputs / "bhim_sheet.png"
        assert bhim.position == "CL"

    def test_serialize_round_trip(self, valid_inputs: Path):
        q = validate_and_build_queue(valid_inputs)
        payload = serialize_queue(q)
        assert payload["schemaVersion"] == 1
        assert payload["totalShots"] == 2
        assert len(payload["batches"]) == 1
        # Ensure JSON-serializable.
        json.dumps(payload)

    def test_batching_respects_batch_size(self, tmp_path: Path):
        chars = _base_characters_json()
        meta = _base_metadata_json()
        meta["project"]["batchSize"] = 2
        # 5 shots -> ceil(5/2) = 3 batches: [[1,2],[3,4],[5]]
        meta["shots"] = []
        for i in range(1, 6):
            shot_id = f"shot_{i:03d}"
            mp4 = f"s{i}.mp4"
            (tmp_path / mp4).write_bytes(b"x")
            meta["shots"].append({
                "shotId": shot_id,
                "mp4Filename": mp4,
                "durationFrames": 25,
                "durationSeconds": 1.0,
                "characterCount": 1,
                "characters": [{"identity": "Bhim", "position": "C"}],
            })
        (tmp_path / "bhim_sheet.png").write_bytes(b"x")
        (tmp_path / "chutki_sheet.png").write_bytes(b"x")
        _write(tmp_path / "characters.json", chars)
        _write(tmp_path / "metadata.json", meta)

        q = validate_and_build_queue(tmp_path)
        assert [len(b) for b in q.batches] == [2, 2, 1]


# ---------------------------------------------------------------
# Missing-input cases (MissingInputError)
# ---------------------------------------------------------------

class TestMissingInputs:
    def test_missing_input_dir(self, tmp_path: Path):
        with pytest.raises(MissingInputError, match="input-dir does not exist"):
            validate_and_build_queue(tmp_path / "nope")

    def test_missing_metadata_json(self, valid_inputs: Path):
        (valid_inputs / "metadata.json").unlink()
        with pytest.raises(MissingInputError, match="metadata.json not found"):
            validate_and_build_queue(valid_inputs)

    def test_missing_characters_json(self, valid_inputs: Path):
        (valid_inputs / "characters.json").unlink()
        with pytest.raises(MissingInputError, match="characters.json not found"):
            validate_and_build_queue(valid_inputs)


# ---------------------------------------------------------------
# Schema-level failures (SchemaValidationError)
# ---------------------------------------------------------------

class TestSchemaValidation:
    def test_metadata_not_valid_json(self, valid_inputs: Path):
        (valid_inputs / "metadata.json").write_text("{ this is not json")
        with pytest.raises(SchemaValidationError, match="not valid JSON"):
            validate_and_build_queue(valid_inputs)

    def test_metadata_wrong_fps(self, valid_inputs: Path):
        meta = _base_metadata_json()
        meta["project"]["fps"] = 30
        _write(valid_inputs / "metadata.json", meta)
        with pytest.raises(SchemaValidationError):
            validate_and_build_queue(valid_inputs)

    def test_metadata_empty_shots(self, valid_inputs: Path):
        meta = _base_metadata_json()
        meta["shots"] = []
        _write(valid_inputs / "metadata.json", meta)
        with pytest.raises(SchemaValidationError):
            validate_and_build_queue(valid_inputs)

    def test_shot_character_count_mismatch(self, valid_inputs: Path):
        meta = _base_metadata_json()
        meta["shots"][0]["characterCount"] = 3  # but only 2 are listed
        _write(valid_inputs / "metadata.json", meta)
        with pytest.raises(SchemaValidationError, match="characterCount"):
            validate_and_build_queue(valid_inputs)

    def test_shot_bad_position_code(self, valid_inputs: Path):
        meta = _base_metadata_json()
        meta["shots"][0]["characters"][0]["position"] = "MIDDLE"
        _write(valid_inputs / "metadata.json", meta)
        with pytest.raises(SchemaValidationError):
            validate_and_build_queue(valid_inputs)

    def test_shot_id_bad_pattern(self, valid_inputs: Path):
        meta = _base_metadata_json()
        meta["shots"][0]["shotId"] = "shotABC"
        _write(valid_inputs / "metadata.json", meta)
        with pytest.raises(SchemaValidationError):
            validate_and_build_queue(valid_inputs)

    def test_mp4_filename_has_path(self, valid_inputs: Path):
        meta = _base_metadata_json()
        meta["shots"][0]["mp4Filename"] = "subdir/scene01_shot01.mp4"
        _write(valid_inputs / "metadata.json", meta)
        with pytest.raises(SchemaValidationError, match="bare filename"):
            validate_and_build_queue(valid_inputs)

    def test_batch_size_out_of_range(self, valid_inputs: Path):
        meta = _base_metadata_json()
        meta["project"]["batchSize"] = 0
        _write(valid_inputs / "metadata.json", meta)
        with pytest.raises(SchemaValidationError):
            validate_and_build_queue(valid_inputs)

    def test_extra_unknown_field_rejected(self, valid_inputs: Path):
        # extra="forbid" on our schemas means typos get caught early.
        meta = _base_metadata_json()
        meta["project"]["unexpectedField"] = "oops"
        _write(valid_inputs / "metadata.json", meta)
        with pytest.raises(SchemaValidationError):
            validate_and_build_queue(valid_inputs)


# ---------------------------------------------------------------
# Cross-reference failures (CrossReferenceError)
# ---------------------------------------------------------------

class TestCrossReferences:
    def test_sheet_png_missing_on_disk(self, valid_inputs: Path):
        (valid_inputs / "bhim_sheet.png").unlink()
        with pytest.raises(CrossReferenceError, match="Character sheet PNG"):
            validate_and_build_queue(valid_inputs)

    def test_shot_identity_not_in_library(self, valid_inputs: Path):
        meta = _base_metadata_json()
        meta["shots"][0]["characters"][0]["identity"] = "Raju"  # not registered
        _write(valid_inputs / "metadata.json", meta)
        with pytest.raises(CrossReferenceError, match="not in the character library"):
            validate_and_build_queue(valid_inputs)

    def test_mp4_file_missing_on_disk(self, valid_inputs: Path):
        (valid_inputs / "scene01_shot01.mp4").unlink()
        with pytest.raises(CrossReferenceError, match="MP4 file"):
            validate_and_build_queue(valid_inputs)


# ---------------------------------------------------------------
# Shot ID uniqueness + sequence (DuplicateShotIdError, ShotIdSequenceError)
# ---------------------------------------------------------------

class TestShotIds:
    def test_duplicate_shot_id(self, valid_inputs: Path):
        meta = _base_metadata_json()
        meta["shots"][1]["shotId"] = "shot_001"  # same as shots[0]
        _write(valid_inputs / "metadata.json", meta)
        with pytest.raises(DuplicateShotIdError):
            validate_and_build_queue(valid_inputs)

    def test_shot_ids_out_of_sequence(self, valid_inputs: Path):
        meta = _base_metadata_json()
        meta["shots"][1]["shotId"] = "shot_005"  # skip 002, 003, 004
        _write(valid_inputs / "metadata.json", meta)
        with pytest.raises(ShotIdSequenceError):
            validate_and_build_queue(valid_inputs)


# ---------------------------------------------------------------
# CLI-level integration tests
# ---------------------------------------------------------------

class TestCLI:
    def test_cli_success_writes_queue_json(self, valid_inputs: Path, capsys):
        exit_code = cli_main(["--input-dir", str(valid_inputs)])
        assert exit_code == 0
        queue_path = valid_inputs / "queue.json"
        assert queue_path.is_file()
        payload = json.loads(queue_path.read_text(encoding="utf-8"))
        assert payload["projectName"] == "ChhotaBhim_Ep042"
        assert payload["totalShots"] == 2

    def test_cli_failure_returns_1_on_validation_error(
        self, valid_inputs: Path, capsys
    ):
        (valid_inputs / "bhim_sheet.png").unlink()
        exit_code = cli_main(["--input-dir", str(valid_inputs)])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "VALIDATION FAILED" in captured.err
        # queue.json must NOT be written on failure.
        assert not (valid_inputs / "queue.json").exists()

    def test_cli_custom_output_file(self, valid_inputs: Path, tmp_path: Path):
        out = tmp_path / "subdir" / "my_queue.json"
        exit_code = cli_main([
            "--input-dir", str(valid_inputs),
            "--output-file", str(out),
            "--quiet",
        ])
        assert exit_code == 0
        assert out.is_file()

    def test_cli_quiet_suppresses_success_line(self, valid_inputs: Path, capsys):
        cli_main(["--input-dir", str(valid_inputs), "--quiet"])
        captured = capsys.readouterr()
        assert captured.out == ""


# ---------------------------------------------------------------
# All Node2Error subclasses are actually that — sanity guard
# ---------------------------------------------------------------

def test_all_custom_errors_are_node2_errors():
    for cls in (
        MissingInputError, SchemaValidationError, CrossReferenceError,
        DuplicateShotIdError, ShotIdSequenceError,
    ):
        assert issubclass(cls, Node2Error)
