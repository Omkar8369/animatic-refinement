# `pipeline/` — Node 2 (validator) + Node 11 (batch manager, future)

Pure-Python, zero GPU deps, zero ComfyUI dependency. Runs identically on:

- **Local Windows** with ComfyUI's embedded Python (via `run_node2.py` wrapper — required because the embedded `python313._pth` ignores `PYTHONPATH`)
- **Standard Linux Python on RunPod** (via `run_node2.py` or `python -m pipeline.cli` directly)

## Node 2 — Metadata Ingestion & Validation

Reads an input folder containing `metadata.json` + `characters.json` + sheet PNGs + shot MP4s. Validates both JSON schemas, confirms every referenced file exists on disk, checks shot-ID uniqueness and sequence, and writes `queue.json` — the contract Node 3 reads.

**Design decisions (locked, see `CLAUDE.md`):**

- **Hard-fail the entire batch** on any validation error (no per-shot skipping).
- **Schema validation via pydantic v2**, `extra="forbid"` on every model.
- **Every error lists ALL offenders**, not just the first, so the operator fixes everything in one pass.
- **Input layout is flat** — one folder holds everything.

## Files

| File | Purpose |
|---|---|
| `schemas.py` | Pydantic v2 models for `metadata.json` (`MetadataFile`) and `characters.json` (`CharactersFile`) |
| `errors.py` | Typed hierarchy: `Node2Error` base + `MissingInputError`, `SchemaValidationError`, `CrossReferenceError`, `DuplicateShotIdError`, `ShotIdSequenceError` |
| `node2.py` | Core: `validate_and_build_queue(input_dir) -> ProcessingQueue`, `serialize_queue(queue)`, and the private sub-step helpers (2A–2E) |
| `cli.py` | argparse entrypoint; exit 0/1/2 |
| `requirements.txt` | `pydantic>=2.5,<3` — the only runtime dep |

## Invoke

```bash
python run_node2.py --input-dir <path>
# or equivalently on standard Python:
python -m pipeline.cli --input-dir <path>
```

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--input-dir <path>` | *required* | Folder containing `metadata.json` + `characters.json` + sheets + MP4s |
| `--output-file <path>` | `<input-dir>/queue.json` | Where to write the resolved queue |
| `--quiet` | off | Suppress the success line on stdout |

Exit codes: `0` success · `1` validation error (readable message on stderr) · `2` unexpected error.

## Test

```bash
python -m pytest tests/ -v
```

26 tests cover: schema validation, cross-reference errors, shot-ID integrity, and CLI exit codes.

## See also

- `docs/PLAN.md` → Node 2 section (locked decisions + 2A–2E)
- `docs/Node_Plan.xlsx` → rows 10–15
- `CLAUDE.md` → Node 2 locked decisions
