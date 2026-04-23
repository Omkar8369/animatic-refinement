# Animatic Refinement Workflow — Part 1 Structural Plan

## Context

This plan defines **Part 1** of a 2-part AI workflow that converts **rough MP4 animatic shots** (Chota Bhim Indian cartoon style) into **Black & White line-art animatic shots** with **refined, reference-accurate characters** in correct positions and poses.

**Problem being solved:** Traditional animators manually redraw rough key poses into clean character line-art based on model sheets. We are replacing that manual refinement with an AI pipeline so the output is ready for Part 2 (ToonCrafter-based in-between generation).

**Inputs:**
- MP4 shot(s) — rough sketch animatic from client (broken down scene-by-scene)
- Character Model Sheets — manually drawn, multiple angles per character
- Shot Metadata (via HTML form) — character count, identity, position, duration

**Output:**
- Refined MP4 shot(s) — same timing, same held-frame pattern, but with clean BnW line-art characters drawn consistently with the model sheets.

**Part 2 (out of scope here):** Frame interpolation via ToonCrafter to generate in-between poses.

**Target platform:** RunPod (GPU rendering, adjustable batch size). Workflow will most likely be implemented in ComfyUI (ControlNet + IP-Adapter/Reference + SD line-art checkpoint) with FFmpeg for video I/O and an HTML form frontend for metadata capture.

---

## NODE-WISE STRUCTURAL PLAN

### NODE 1 — Project Input & Setup Interface
Purpose: Capture all shot metadata and character references from the artist/operator before the AI pipeline runs. **Pure static frontend** (no server) — outputs are downloaded JSON files the user drops into the pipeline folder before running RunPod.

**Architecture decisions (locked):**
- **Two HTML pages, no server.** Page 1 = Character Library (one-time per project). Page 2 = Shot Metadata Form (repeating "+ Add shot" blocks).
- **Cross-page state** persisted in `localStorage` so the shot form's identity dropdown stays populated.
- **Delivery:** browser download of `characters.json` and `metadata.json` via `Blob` + `<a download>`. No HTTP POST anywhere.
- **MP4s are NOT uploaded by the form** (browsers cannot write to disk paths). The form captures the MP4 filename per shot and shows a local thumbnail; the operator copies the actual MP4 files into the pipeline input folder separately. `metadata.json` references files by filename only.

**Sub-steps:**

- **1A. Character Library Page (`characters.html`)** — pre-registration UI. For each character: upload one model-sheet PNG (8-angle horizontal strip, transparent or black background, full color) + type a display name (e.g., `Bhim`, `Chutki`). Persists the character list in `localStorage` and offers a "Download `characters.json`" button. The user also keeps the uploaded sheet PNGs (re-named on download to a canonical `<name>_sheet.png`) for later placement in the pipeline folder.
- **1B. Sheet-format quick check** — client-side validation on each uploaded sheet: non-zero dimensions, aspect ratio consistent with a horizontal strip (width ≫ height), and a count-of-alpha-islands ≈ 8 sanity check (full slicing happens in Node 6; this is just an early-warning preview).
- **1C. Shot Metadata Form Page (`index.html`)** — one big form with a repeating "+ Add shot" block pattern (1a). Each block is one shot row; user can add or remove rows freely. Reads the character library from `localStorage` to populate per-shot identity dropdowns; if the library is empty, the page prompts the user to visit `characters.html` first.
- **1D. Per-shot fields** — `Shot ID` (auto-generated `shot_001`, `shot_002`… but user-overridable), `MP4 filename` (file picker that captures the filename + shows a `<video>` preview but does NOT upload the bytes anywhere), `Character Count` (1–N), per-character (`identity` dropdown sourced from library, `position` ∈ {L, CL, C, CR, R}), `Duration` in **frames @ 25 FPS** (integer).
- **1E. Batch-level fields** — `project_name`, `batch_size` (integer; governs RunPod VRAM headroom), and a notes field for the operator.
- **1F. `metadata.json` export (browser download)** — serializes the full form state (project, batch_size, ordered shot list with per-character details) to JSON and triggers a browser download via `Blob` + `URL.createObjectURL` + a hidden anchor. This file is the canonical hand-off contract into Node 2.
- **1G. Operator handoff workflow** — README in `frontend/` documents the manual steps: (i) run `characters.html`, download `characters.json` and the named sheet PNGs; (ii) run `index.html`, download `metadata.json`; (iii) place `metadata.json`, `characters.json`, the sheet PNGs, and the shot MP4s into the pipeline input folder structure expected by Node 2.

### NODE 2 — Metadata Ingestion & Validation
Purpose: Load the form's two JSON files, confirm every file they reference exists on disk, and hand Node 3 an ordered, batched processing queue.

**Architecture decisions (locked):**
- **Runs both locally and on RunPod.** Node 2 is pure-Python with no GPU deps, so the same code runs under the Windows portable embedded Python (local dev / unit tests) and under the standard Python on each RunPod pod (production). `run_node2.py` at the repo root papers over the one Windows-specific quirk (embedded Python's `python313._pth` ignores `PYTHONPATH`) by inserting the repo root onto `sys.path` before importing `pipeline.cli`.
- **Hard-fail the entire batch on any validation error** — per-shot skipping is explicitly rejected. Rationale: Node 2 runs in seconds, its errors are operator-data errors (typo, missing file, wrong `characterCount`), and silently dropping a shot would waste a RunPod batch later and make partial outputs hard to reason about. Every error raises a typed `Node2Error` subclass with a message that points at the exact shot/character/filename to fix.
- **Schema validation via pydantic v2** (`extra="forbid"` on every model). Gives rich, field-path error messages for free and already ships inside ComfyUI's embedded Python, so the only new runtime dep is `pydantic>=2.5,<3` in `pipeline/requirements.txt`.
- **Flat input directory layout.** `metadata.json`, `characters.json`, every sheet PNG, and every shot MP4 sit side-by-side in one folder — the same folder the operator assembles by following Node 1G's handoff instructions. Node 2 takes that folder via `--input-dir` and emits `queue.json` into it.

**Sub-steps:**

- **2A. Parse & schema-validate `metadata.json`** — load via `pipeline.schemas.MetadataFile`. Pydantic enforces: `schemaVersion ≥ 1`, ISO-8601 `generatedAt`, `project.fps == 25` (locked convention), `project.batchSize` ∈ [1, 64], at least one shot, per-shot `shotId` matching `^shot_\d{3,}$`, `mp4Filename` with no path separators, `durationFrames ≥ 1`, `durationSeconds > 0`, `characterCount == len(characters)`, and each character's `position` ∈ {L, CL, C, CR, R}.
- **2B. Parse & schema-validate `characters.json`** — load via `pipeline.schemas.CharactersFile`. Enforces: non-empty character list, `sheetFilename` with no path separators, `width`/`height ≥ 1`, and carries through the `conventions.angleOrderConfirmed` flag so Node 6 can check it before slicing.
- **2C. Cross-reference checks** — (i) every `sheetFilename` in `characters.json` exists as a file in `input-dir`; (ii) every `identity` referenced by any shot resolves to exactly one character in the library; (iii) every `mp4Filename` in `metadata.json` exists as a file in `input-dir`. Each failure lists ALL offenders, not just the first, so the operator can fix everything in one pass.
- **2D. Shot-ID integrity** — duplicate `shotId` across shots is rejected; the shot list must be a contiguous ascending sequence starting at `shot_001` (`shot_001`, `shot_002`, …). Downstream nodes assume this ordering for deterministic filenames.
- **2E. Build & serialize processing queue** — in-memory: ordered list of `ShotJob` records (resolved absolute `mp4Path`, resolved absolute `sheetPath` per character, position, durationFrames), chunked into batches of size `project.batchSize`. On disk: `queue.json` written to `input-dir` (or `--output-file`) as the contract Node 3 reads. CLI exit codes: `0` success, `1` validation error (`Node2Error`), `2` unexpected error.

### NODE 3 — Shot Pre-processing (MP4 → PNG Sequence)
Purpose: Convert the rough animatic MP4 into individually-addressable frames.

- **3A. Load MP4 shot** — pull current shot from queue.
- **3B. FFmpeg PNG extraction at 25 FPS** — decode shot into PNG sequence.
- **3C. Frame indexing** — filename convention `shot_XXX_frame_YYY.png`.
- **3D. Working directory storage** — isolated temp folder per shot.
- **3E. Frame-count sanity check** — verify extracted frame count ≈ metadata duration; log mismatch.

### NODE 4 — Key Pose Extraction (Ignore Held Frames)
Purpose: Isolate only the frames where the drawing actually changes. Held frames (timing duplicates) are noted but not sent to the AI generator.

- **4A. Frame-by-frame difference analysis** — pixel-diff or perceptual-hash between consecutive frames.
- **4B. Threshold-based motion detection** — tunable threshold flags "new pose" vs "held".
- **4C. Unique key pose identification** — extract the frames that represent distinct poses (e.g., point-A and point-B of a slide/walk cycle).
- **4D. Timing map** — JSON describing `{key_pose_index: [held_frame_positions, held_duration]}` so held timing can be reconstructed at Node 9.
- **4E. Export key pose frames** — save the distinct key poses to a `/keyposes/` subfolder; these are the only frames that enter the AI generator.

### NODE 5 — Character Detection & In-Frame Position Analysis
Purpose: For each key pose frame, figure out which character is where and at what angle.

- **5A. Analyze rough sketch frame** — vision pass (segmentation or silhouette detection).
- **5B. Detect character silhouettes / count** — count detected characters in the frame.
- **5C. Cross-validate with metadata character count** — flag mismatch against Node 2 mapping.
- **5D. Detect in-frame position** — classify each silhouette into L / CL / C / CR / R bins.
- **5E. Detect pose/angle** — estimate body angle (front / three-quarter / profile / back) for each character.
- **5F. Multi-character disambiguation** — if metadata lists >1 character, match silhouettes to identities by left-to-right ordering against metadata positions.

### NODE 6 — Character Reference Sheet Matching
Purpose: Pick the right view from the model sheet for each detected character.

- **6A. Load character model sheets** per metadata.
- **6B. Identify required angle** from Node 5E output.
- **6C. Select closest matching reference view** from the model sheet (front / 3/4 / profile / back).
- **6D. Extract pose characteristics** — arm position, gesture, expression cues from rough sketch.
- **6E. Prepare reference conditioning images** — crop/normalize the selected reference ready for IP-Adapter / Reference ControlNet.

### NODE 7 — AI-Powered Pose Refinement (Replace Rough With BnW Line Art)
Purpose: The actual generation step — produce clean BnW line-art of the correct character in the same pose as the rough sketch.

- **7A. ControlNet conditioning** — use rough sketch as pose/lineart control input (Scribble / LineArt / OpenPose CN).
- **7B. Character consistency** — IP-Adapter or Reference-Only ControlNet fed with the Node 6E reference crop.
- **7C. Generate BnW line art** — SD checkpoint tuned for line-art; sampler settings locked for consistency across frames.
- **7D. Enforce position per metadata** — inpaint/compose so the generated character lands in the metadata-specified screen region.
- **7E. Multi-character compositing** — if multiple characters, generate each separately and composite onto a single frame.
- **7F. QC gate** — regenerate if character identity drift, double-lines, or position offset detected.

### NODE 8 — Scene Assembly (Per Key Pose Frame)
Purpose: Produce the finished, fully-composed refined key pose frame.

- **8A. Place refined characters at correct positions** on a transparent/white canvas.
- **8B. Scale matching** — match the scale implied by the rough sketch.
- **8C. Handle multiple characters** — z-order preserved from rough.
- **8D. BnW line-art consistency** — unify line weight / contrast across characters.
- **8E. Composite final refined key pose frames** — output one clean PNG per key pose.

### NODE 9 — Timing Reconstruction (Re-apply Held Frames)
Purpose: Rebuild the full-length sequence using the timing map from Node 4.

- **9A. Read timing map** from Node 4D.
- **9B. Map new key poses → timing slots** — each refined key pose inherits the same frame index its rough counterpart held.
- **9C. Duplicate refined frames for held durations** — same-pose held frames are duplicated (not re-generated) to match original timing exactly.
- **9D. Assemble complete PNG sequence** with continuous frame numbering.
- **9E. Verify total frame count** matches metadata duration.

### NODE 10 — Output Generation (PNG → MP4)
Purpose: Encode the final refined PNG sequence back into an MP4.

- **10A. FFmpeg encoding** — PNG sequence to MP4.
- **10B. 25 FPS encoding** — match source rate.
- **10C. Filename convention** — `shot_XXX_refined.mp4`.
- **10D. Output validation** — duration, frame count, codec.
- **10E. Archive working files** — keep PNG sequence and intermediate folders for debugging / Part 2 reuse.

### NODE 11 — Batch Management
Purpose: Keep the pipeline moving shot-by-shot across the whole batch.

- **11A. Queue next shot** after current completes.
- **11B. Progress tracking & logging** — per-shot status, timing, errors.
- **11C. Error handling & retry** — configurable retries per failed node.
- **11D. RunPod GPU / VRAM monitoring** — pause or reduce batch if OOM risk.
- **11E. Final batch report** — summary of successes, failures, and per-shot runtime.

---

## Critical files / artifacts to be created

- HTML form (Node 1) — frontend for metadata capture.
- `metadata.json` (Node 1F → Node 2) — the canonical contract between UI and pipeline.
- ComfyUI workflow JSON (Nodes 3–10) — the actual render graph.
- Timing map JSON per shot (Node 4D) — the bridge between key pose extraction and final assembly.
- Refined MP4s (Node 10) — the deliverable.

## Reusable / external components

- FFmpeg — MP4↔PNG conversion (Nodes 3, 10).
- ComfyUI + ControlNet (LineArt / Scribble) — pose guidance (Node 7A).
- IP-Adapter or Reference-Only ControlNet — character consistency (Node 7B).
- SD line-art checkpoint — BnW line-art generation (Node 7C).

## Verification (end-to-end test)

1. Fill the HTML form for one test shot (1 character, known position, known duration).
2. Upload one MP4 shot + corresponding character model sheet.
3. Run the pipeline with batch size = 1.
4. Inspect intermediates: PNG sequence (Node 3), extracted key poses (Node 4), refined key poses (Node 8), reconstructed sequence (Node 9).
5. Open final MP4 (Node 10) and confirm: same duration as source, same held-frame timing, character matches model sheet, positioned per metadata.
6. Scale up to a multi-shot, multi-character batch to validate Nodes 5F, 7E, and 11.

## Follow-up (not in this plan)

After plan approval, create an **Excel workbook** mirroring this node structure — one row per sub-step (1A, 1B, … 11E) — with columns for Node, Sub-step, Purpose, Inputs, Outputs, Tools/Tech, and Notes. This file will serve as the editable working document for refining each node.
