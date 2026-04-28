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

- **1A. Character Library Page (`characters.html`)** — pre-registration UI. For each character: upload one model-sheet PNG (8-angle horizontal strip, transparent or black background, full color) + type a display name (e.g., `Bhim`, `Chutki`) + pick the **Node 7 pose extractor** from a 2-option dropdown (`dwpose` default, `lineart-fallback` for non-humans like Jaggu the monkey). Persists the character list in `localStorage` and offers a "Download `characters.json`" button. The user also keeps the uploaded sheet PNGs (re-named on download to a canonical `<name>_sheet.png`) for later placement in the pipeline folder. The `poseExtractor` field is carried on every character in `characters.json` and through to `queue.json` so Node 7 routes per-character without re-reading `characters.json`.
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
- **2B. Parse & schema-validate `characters.json`** — load via `pipeline.schemas.CharactersFile`. Enforces: non-empty character list, `sheetFilename` with no path separators, `width`/`height ≥ 1`, carries through the `conventions.angleOrderConfirmed` flag so Node 6 can check it before slicing, and validates each character's `poseExtractor` ∈ {`dwpose`, `lineart-fallback`} for Node 7 routing (defaults to `dwpose` if absent, so libraries saved before the field existed still load cleanly). `queue.json`'s per-character dicts gain a `poseExtractor` field alongside `identity` / `sheetPath` / `position` so Node 7 has everything on one read.
- **2C. Cross-reference checks** — (i) every `sheetFilename` in `characters.json` exists as a file in `input-dir`; (ii) every `identity` referenced by any shot resolves to exactly one character in the library; (iii) every `mp4Filename` in `metadata.json` exists as a file in `input-dir`. Each failure lists ALL offenders, not just the first, so the operator can fix everything in one pass.
- **2D. Shot-ID integrity** — duplicate `shotId` across shots is rejected; the shot list must be a contiguous ascending sequence starting at `shot_001` (`shot_001`, `shot_002`, …). Downstream nodes assume this ordering for deterministic filenames.
- **2E. Build & serialize processing queue** — in-memory: ordered list of `ShotJob` records (resolved absolute `mp4Path`, resolved absolute `sheetPath` per character, position, durationFrames), chunked into batches of size `project.batchSize`. On disk: `queue.json` written to `input-dir` (or `--output-file`) as the contract Node 3 reads. CLI exit codes: `0` success, `1` validation error (`Node2Error`), `2` unexpected error.

### NODE 3 — Shot Pre-processing (MP4 → PNG Sequence)
Purpose: Convert every rough-animatic MP4 listed in `queue.json` into individually-addressable PNG frames, one folder per shot.

**Architecture decisions (locked 2026-04-23):**
- **ffmpeg binary via `imageio-ffmpeg` pip wheel.** No system-level ffmpeg needed; identical behavior on Windows embedded Python, RunPod Linux, and CI. Zero operator setup.
- **Per-shot folders** under `<work-dir>/<shotId>/frame_NNNN.png` (NNNN 4-digit zero-padded, 1-indexed). Keeps Node 4's scan loop trivially per-shot.
- **1:1 decode, no resampling.** No `-r` flag passed to ffmpeg — the rough MP4 is already 25 FPS per locked convention, and any `-r` would drop/dup frames silently. Node 3's contract is "decode and nothing else".
- **Fail-fast on hard errors, warn-and-continue on frame-count drift.** Queue-format errors, missing MP4s, ffmpeg crashes, and numbering gaps all raise and abort. A frame-count mismatch between `durationFrames` in metadata and the actual decoded count is a structured warning in `node3_result.json` — the operator sees it, the batch still completes. Node 9 uses the actual count when reconstructing timing.
- **Core logic in `pipeline/node3.py` + thin ComfyUI wrapper** in `custom_nodes/node_03_mp4_to_png/`. Same code is exercised by the CLI, pytest, and ComfyUI — the wrapper has no business logic.

Sub-steps:

- **3A. Load + validate queue.json** — read `<queue-path>`, check `schemaVersion == 1`, every shot has `shotId`/`mp4Path`/`durationFrames`, no duplicate shotIds, every `mp4Path` exists on disk. Raises `QueueInputError` on any structural problem.
- **3B. Per-shot folder setup** — create `<work-dir>/<shotId>/`, wipe any stale `frame_*.png` from a previous partial run so the manifest matches the directory exactly.
- **3C. FFmpeg decode** — `ffmpeg -y -hide_banner -loglevel error -i <mp4> -start_number 1 -vsync 0 <out>/frame_%04d.png`. Non-zero exit code → `FFmpegError` with the last 10 stderr lines attached.
- **3D. Frame-count check + sequence verify** — count `frame_*.png`, confirm contiguous 1..N numbering (gap → `FrameExtractionError`), compare to `durationFrames`. On mismatch, append a `FrameCountWarning{shotId, expectedFrames, actualFrames, message}` to the result — batch continues.
- **3E. Write manifests** — per-shot `_manifest.json` (shotId, paths, counts, ffmpeg binary, extractedAt, optional warning) and top-level `<work-dir>/node3_result.json` aggregating every shot + all warnings. Node 4 reads `node3_result.json` to walk the frame sequences.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node3.py --queue <path-to-queue.json> --work-dir <path>

# Windows embedded Python (local dev):
"C:\...\python_embeded\python.exe" run_node3.py --queue <q> --work-dir <w>
```

CLI exit codes: `0` success (warnings are still exit 0), `1` `Node3Error` subclass, `2` unexpected error.

### NODE 4 — Key Pose Extraction (Ignore Held Frames)
Purpose: Partition each shot's PNG frame sequence into **key poses** (the frames where the drawing actually changes) plus **held-frame runs** (timing duplicates that do NOT go to the AI generator). Only the key poses reach Node 7's refinement pass; Node 9 replays the held frames at the end.

**Architecture decisions (locked 2026-04-23):**
- **Phase correlation (numpy FFT) + aligned pixel-diff (MAE on 0-255 grayscale), NOT naïve inter-frame diff.** Storyboard artists often animate a walk/run by translating the exact same pose L→R across several frames. A naïve pixel-diff would flag every intermediate slide-step as a new key pose and waste Node 7's AI budget refining the same drawing multiple times. Phase correlation recovers the translation (`(dy, dx)`); the aligned-overlap MAE scores similarity *after* translation. Slides become ONE key pose with per-held-frame `(dy, dx)` offsets that Node 9 replays by copy-and-translate.
- **Compare frames against the current key-pose ANCHOR, not against frame N−1.** Each held frame's offset is cumulative from the anchor, which is exactly the shape Node 9 needs.
- **Downscale grayscale, max edge = 128, before comparison.** Speed + encoder-noise tolerance. Offsets are scaled back to full-resolution pixels on write.
- **Global MAE threshold, default 8.0, exposed via `--threshold`.** One number per run. No per-shot adaptive logic in this pass — if the batch has wildly different shot types we can revisit later.
- **No minimum hold-length filter.** Even a 1-frame segment between two key poses is recorded as its own 1-frame run.
- **Key-pose PNGs are COPIED, not renamed, to `<shotId>/keyposes/`.** Source filenames (`frame_NNNN.png`) are preserved so Node 9 can cross-reference original frame numbers trivially.
- **Fail-fast on I/O or decode errors** (`Node3ResultInputError`, `KeyPoseExtractionError`). The partition itself is pure data — every frame lands in exactly one key-pose group, no warnings needed.
- **Core logic in `pipeline/node4.py` + thin ComfyUI wrapper** in `custom_nodes/node_04_keypose_extractor/`. Same code runs from CLI, tests, CI, and ComfyUI.
- **Single-threaded.** Shots are independent; parallelism is a future Node 11 concern.

Sub-steps:

- **4A. Load + validate `node3_result.json`** — check `schemaVersion == 1`, required keys (`workDir`, `shots`), each shot has `shotId`/`framesDir`/`frameFilenames`, frames folder exists on disk. Raises `Node3ResultInputError` on any problem.
- **4B. Per-shot anchor walk** — load frame 1 as grayscale, downscale so `max(H, W) = maxEdge`. Frame 1 is always the first key pose (anchor). For frames 2..N: load + downscale, run phase correlation against the anchor (FFT-based) to get `(dy_small, dx_small)`, compute aligned MAE over the valid overlap.
- **4C. Classify** — if aligned MAE ≤ `threshold`, the frame is **held** against the current anchor (offset stored as `[round(dy_small × scale), round(dx_small × scale)]` in full-resolution pixels). Otherwise it becomes a **new key pose**, replacing the anchor; subsequent frames compare against the new anchor.
- **4D. Per-shot output** — create `<shotId>/keyposes/`, wipe any stale copies, copy each key-pose frame in (source filename preserved), write `<shotId>/keypose_map.json` describing the partition (schemaVersion, shotId, totalFrames, sourceFramesDir, keyPosesDir, threshold, maxEdge, keyPoses: `[{keyPoseIndex, sourceFrame, keyPoseFilename, heldFrames: [{frame, offset: [dy, dx]}, ...]}]`).
- **4E. Aggregate result** — write `<work-dir>/node4_result.json` with a one-line `ShotKeyPoseSummary` per shot (shotId, totalFrames, keyPoseCount, sourceFramesDir, keyPosesDir, keyPoseMapPath) plus the run-wide `threshold` and `maxEdge`. This is the contract Node 5 consumes.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node4.py --node3-result <path-to-node3_result.json> [--threshold 8.0] [--max-edge 128]

# Windows embedded Python (local dev):
"C:\...\python_embeded\python.exe" run_node4.py --node3-result <n3> [--threshold 8.0]
```

CLI exit codes: `0` success, `1` `Node4Error` subclass (`Node3ResultInputError`, `KeyPoseExtractionError`), `2` unexpected error.

### NODE 5 — Character Detection & In-Frame Position Analysis
Purpose: For each key pose, figure out **which character is where**. Angle detection is deferred to Node 6, which does reference-sheet 8-angle matching — a separate problem with a separate tool (classical here, similarity-based there).

**Architecture decisions (locked 2026-04-23):**
- **Classical connected-components, NOT ML.** Chota Bhim animatics are hand-drawn line art with deliberately separated characters. Any ML segmentation model would need a 2+ GB download per RunPod pod, would overfit to its training distribution (photos / anime), and would miss the stylized outlines. Otsu binarization + `scipy.ndimage.label` (8-connectivity) + small cleanup rules handles the 95% case. For the remaining 5% (touching or floating-detail cases) a reconcile pass with `binary_erosion` or IoU-based bbox merge fixes it mechanically.
- **Run on every key pose, not just the first.** Per-frame is safer and nearly free (CC is milliseconds per frame). A character can enter/exit mid-shot, and metadata's `characterCount` is per-shot — so we'd still have to re-detect on every key pose to know whether a specific key pose has everyone present.
- **Position binning: 25/20/10/20/25 split of normalized frame width.** L: `[0.00, 0.25)`, CL: `[0.25, 0.45)`, C: `[0.45, 0.55)` (exact-center only, narrow 10% band), CR: `[0.55, 0.75)`, R: `[0.75, 1.00]`. Each detection's normalized centre-x lands in exactly one bin.
- **Identity assignment via Strategy A (positional), v1.** Sort detected silhouettes left→right by centre-x; sort metadata characters left→right by position rank (L<CL<C<CR<R, ties break on metadata order); zip. No ML similarity check. If real-world mismatch rates warrant it later, we add a v2 Strategy B (reference-sheet-similarity verification) as a *post-pass* without replacing Strategy A.
- **Warn AND reconcile on count mismatch — never fail-fast.** If CC produces a different blob count than metadata expects: too-many → drop smallest-area blobs until count matches; too-few → re-run CC after progressive `binary_erosion` (up to 3 iterations) to pull touching characters apart. Every reconcile action is logged as a structured `DetectionWarning` in `character_map.json` so the operator can review what was auto-fixed. Only genuine I/O errors (missing manifest, unreadable PNG) raise.
- **Core logic in `pipeline/node5.py` + thin ComfyUI wrapper** in `custom_nodes/node_05_character_detector/`. Same code path runs from CLI, tests, CI, and ComfyUI.
- **Single-threaded.** Shots are independent; parallelism is a future Node 11 concern. `_detect_bboxes` on a 1280×720 frame finishes in ~30 ms.

Sub-steps:

- **5A. Load + validate inputs** — read `node4_result.json` (Node 4) and `queue.json` (Node 2); check both `schemaVersion == 1` and required keys. Build a `shotId → [{identity, position}, ...]` lookup from the queue. Any `shotId` in `node4_result.json` missing from the queue raises `QueueLookupError` (stale state — operator rerun Node 2). Malformed manifests raise `Node4ResultInputError`.
- **5B. Per-key-pose detection** — load each key-pose PNG as grayscale, apply the Phase 2f luminance-threshold pre-filter (default 80, `--dark-threshold N` to tune; pixels with luminance < threshold are kept as character ink, lighter pixels are erased to white BG to reject lighter BG-furniture lines per the user's storyboard convention), follow with 3×3 morphological closing to seal 1-2 pixel gaps where character outlines crossed BG lines, run `scipy.ndimage.label` with 8-connectivity, drop any blob whose area is below `min_area_ratio × frame_area` (default 0.1%), merge any two bounding boxes whose IoU ≥ `merge_iou` (default 0.5; reunites floating details like a separate eye dot with the parent silhouette). Otsu binarization (the original 2026-04-23 design) is retained as `_binarize_otsu` for tests/debugging but is no longer wired into the production path. As a side effect, Node 5 also writes `<shot>/dark_lines/<filename>.png` per keypose (BnW: black ink on white BG) for Node 7 to consume — gives Flux character-only pixels with BG furniture erased to white.
- **5C. Reconcile count against metadata** — compare blob count to `len(shot.characters)`. Too-many → sort by area descending, keep the top-N largest, append `count-mismatch-over` warning. Too-few → progressive `binary_erosion` x1, x2, x3 (stop as soon as count meets expected); append `reconcile-eroded` warning. Still-wrong after max iterations → append `reconcile-failed` warning and let Node 6 fail cleanly on that key pose.
- **5D. Position binning** — compute each bbox's normalized centre-x; bucket into L/CL/C/CR/R via the locked 25/20/10/20/25 thresholds.
- **5E. Identity assignment (Strategy A)** — sort detections left→right by centre-x; sort `shot.characters` by position rank; zip. Each detection emits `{identity, expectedPosition, boundingBox, centerX, positionCode, area}`. Extra unmatched detections (reconcile left more bboxes than metadata) carry `identity=""` for operator review. Write per-shot `character_map.json` at `<shotId>/character_map.json` (next to `keyposes/`) plus aggregate `<work-dir>/node5_result.json`.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node5.py --node4-result <path-to-node4_result.json> --queue <path-to-queue.json> [--min-area-ratio 0.001] [--merge-iou 0.5]

# Windows embedded Python (local dev):
"C:\...\python_embeded\python.exe" run_node5.py --node4-result <n4> --queue <q>
```

CLI exit codes: `0` success (reconcile warnings are still exit 0), `1` `Node5Error` subclass (`Node4ResultInputError`, `QueueLookupError`, `CharacterDetectionError`), `2` unexpected error.

### NODE 6 — Character Reference Sheet Matching
Purpose: For each character Node 5 detected in each key pose, pick the best-matching 8-angle view from that character's model sheet and emit it as a BnW line-art crop ready for Node 7's IP-Adapter / Reference-Only conditioning.

**Architecture decisions (locked 2026-04-23):**
- **Alpha-island bbox slicing; RGB-only sheets fail loud.** `scipy.ndimage.label` on `alpha > 0` → 8 bboxes sorted left→right. Non-RGBA sheet raises `ReferenceSheetFormatError` — operator re-exports with transparent background. Node 1B already enforces transparent export at upload, so a second Otsu-fallback code path would reward ignoring that gate and add an untested alternative for zero real benefit.
- **Canonical 8-angle order confirmed 2026-04-23** against the Bhim reference template: `back, back-3q-L, profile-L, front-3q-L, front, front-3q-R, profile-R, back-3q-R`. L/R is the character's anatomical left/right (not the viewer's). Identifiers are ASCII `3q` in code/JSON (matches `frontend/characters.js`); `¾` in prose is equivalent.
- **Structured-fail on `characters.json.conventions.angleOrderConfirmed == false`.** No interactive prompt (RunPod + ComfyUI are both non-interactive). Error text lists the canonical order and instructs the operator to flip the flag. `frontend/characters.js` now defaults the flag to `true`; old libraries saved before this commit with `false` still trip the gate.
- **Angle matching: classical, per-key-pose.** Per detection: crop key-pose PNG to bbox → Otsu → largest CC = silhouette. Scale-normalize that silhouette and each of the 8 reference alpha-mask silhouettes to a 128×128 canvas (aspect-preserving pad, centered on centroid). Score each (rough, reference) pair via a weighted combination of: silhouette IoU, horizontal-symmetry score, bbox aspect-ratio similarity, interior-edge density in the upper head band (this last signal disambiguates front from back, which are near-identical in outline). Pick the reference angle with max score. **No ML, no CLIP, no pose-detector in v1.** Chota Bhim art is crisp flat-fill cartoon with distinctive silhouettes per angle; ML preprocessors add GB-scale downloads per pod for an 8-way classification problem that classical methods handle well. If real-shot hit rate falls below ~90% we promote to a CLIP-based tiebreaker as a post-pass without changing the Node 6 contract.
- **Silhouette recomputed in Node 6**, not emitted by Node 5. Node 5's `character_map.json` stays text-only; Node 6 owns its silhouette pipeline. Edge case: detections Node 5 reconciled via `binary_erosion` produce slightly noisy recomputes, but 8-way matching is coarse enough to tolerate that.
- **Line-art conversion = classical DoG (Difference of Gaussians).** Per chosen color crop: luminance → DoG (σ₁=1.0, σ₂=2.0) → threshold → OR-combined with alpha boundary → optional 1-pixel thinning. Pure numpy/scipy, zero download. CLI flag `--lineart-method {dog,canny,threshold}` reserves a switch to an ML preprocessor without contract change.
- **Per-key-pose output.** A character turning mid-shot gets the correct reference on each of its key poses. Classical scoring is ~ms per pose, so per-key-pose has no runtime penalty versus per-shot.
- **Core logic in `pipeline/node6.py` + thin ComfyUI wrapper** in `custom_nodes/node_06_reference_matcher/`. Same template as Nodes 3/4/5. Same code exercised by CLI, pytest, CI, and ComfyUI.
- **Rerun safety:** each `<shotId>/reference_crops/` is wiped of stale PNGs before each run so `reference_map.json` always matches directory contents exactly.
- **Single-threaded.** Parallelism is a Node 11 concern.

Sub-steps:

- **6A. Load + validate inputs** — read `node5_result.json` (Node 5), `queue.json` (Node 2), and `characters.json` (Node 1). Check all three `schemaVersion == 1`. Build `shotId → [{identity, sheetPath}]` lookup from queue. If `characters.conventions.angleOrderConfirmed == false`, raise `AngleOrderUnconfirmedError` carrying the canonical-order text. Any `shotId` in `node5_result.json` missing from the queue raises `QueueLookupError` (reused from Node 5, same semantics). Malformed manifests raise `Node5ResultInputError` / `CharactersInputError`.
- **6B. Slice + cache reference sheets** — per unique identity referenced across all shots: load `sheetPath` as RGBA; raise `ReferenceSheetFormatError` if not RGBA; `scipy.ndimage.label` on `alpha > 0` → bboxes sorted left→right; verify count == 8 (else `ReferenceSheetSliceError`); cache the 8 crops in-memory keyed by `(identity, angle_name)` matching `characters.conventions.angleOrderLeftToRight`. Convert each color crop to line art via DoG + alpha-boundary union. Write both color and line-art PNGs to `<shotId>/reference_crops/<identity>_<angle>.png` / `_lineart.png`.
- **6C. Per-key-pose silhouette recomputation** — for each detection in every key pose's `character_map.json`: crop the key-pose PNG to the detection's `boundingBox`; Otsu-binarize; keep the largest connected component — that's the rough silhouette mask.
- **6D. Angle matching** — scale-normalize the rough silhouette and each of the 8 reference-angle silhouettes (alpha masks) to the 128×128 canvas, centered on centroid, aspect-preserving padded. Score each `(rough, reference)` pair via the weighted multi-signal described above. Pick the reference angle with max score.
- **6E. Emit manifest** — per shot write `<shotId>/reference_map.json`: `{schemaVersion: 1, shotId, keyPoses: [{keyPoseIndex, detections: [{identity, selectedAngle, scoreBreakdown, referenceColorCropPath, referenceLineArtCropPath}, ...]}, ...]}`. Aggregate `<work-dir>/node6_result.json`: one `ShotReferenceSummary` per shot (shotId, keyPoseCount, referenceMapPath, angleHistogram). Node 7 reads `reference_map.json` directly.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node6.py --node5-result <path-to-node5_result.json> --queue <path-to-queue.json> --characters <path-to-characters.json> [--lineart-method dog]

# Windows embedded Python (local dev):
"C:\...\python_embeded\python.exe" run_node6.py --node5-result <n5> --queue <q> --characters <c>
```

CLI exit codes: `0` success, `1` `Node6Error` subclass (`Node5ResultInputError`, `QueueLookupError`, `CharactersInputError`, `ReferenceSheetFormatError`, `ReferenceSheetSliceError`, `AngleMatchingError`), `2` unexpected error.

### NODE 7 — AI-Powered Pose Refinement (Replace Rough With BnW Line Art)
Purpose: The actual generation step — produce clean BnW line-art of the correct character in the same pose as the rough sketch. **Pose comes from the rough (action-accurate), identity comes from the reference sheet (character-accurate) — the two are kept on separate conditioning channels so the generator can follow both without compromising either.**

**Architecture decisions (locked 2026-04-23):**
- **Separate pose from identity.** The rough animatic's action pose rarely matches any of the 8 static angles on the model sheet (character throwing a punch, running, jumping). Treating the rough as "lineart" and the reference as "lineart" pushes two conflicting line-drawings into the same CN channel. Instead: extract skeleton/pose from the rough → feed through a pose-aware ControlNet; feed the **Node 6 color reference crop** to IP-Adapter for identity. The generator draws the reference character *in the rough's pose*. This is the fundamentally correct decomposition for the refinement problem.
- **Pose extraction is PER-CHARACTER, routed via `characters.json.poseExtractor`.**
  - **`dwpose` (default, humans)** — DWPose preprocessor (2023+, handles stylized / cartoon proportions better than classical OpenPose). Output skeleton feeds a DWPose ControlNet at strength 0.75.
  - **`lineart-fallback` (non-humans, e.g. Jaggu the monkey)** — quadrupeds and non-biped characters don't produce reliable DWPose skeletons. These route through a LineArt + Scribble CN stack from the rough character crop instead. Identity still comes from IP-Adapter. Every character has a model sheet regardless (Node 6 still produces a reference crop).
- **IP-Adapter is fed Node 6's COLOR reference crop, not the DoG line-art crop.** IP-Adapter's identity embedding expects a textured/colored image. The line-art crop is emitted by Node 6 for potential Reference-Only CN paths, but the primary identity channel is IP-Adapter with the color crop. Reference-Only CN is available as a fallback tiebreaker.
- **txt2img, not img2img.** The rough pixels must NOT bleed through into the output — rough animatics have messy scribbles, stray marks, timing annotations. `img2img` would inherit all that. Pose CN + IP-Adapter + txt2img gives the generator the pose skeleton and the identity without the rough's pixel noise.
- **Per-character generation, NOT whole-frame inpaint.** Each detection (per key pose per character) is generated on its own 512×512 canvas, then Node 8 composites. This keeps identity clean when multiple characters share a frame — IP-Adapter only ever sees one character's reference at a time.
- **Base model: SD 1.5 + line-art anime checkpoint.** Specifically AnyLoRA (or Animagine-line-art equivalent) with an optional BnW LoRA to bias output toward clean black-line output. SDXL adds VRAM pressure on RunPod without a quality win for 512×512 single-character line-art. SD 1.5 + line-art checkpoint is the right tradeoff. Exact checkpoint + LoRA pinned in `models.json`.
- **Locked sampler/seed defaults for consistency across a shot:** DPM++ 2M Karras, 25 steps, CFG 7.0, 512×512 canvas. Seed is logged per `(shotId, keyPoseIndex, identity)` so a failed retry can be re-run deterministically. A shot's multiple key poses share a seed base so the line weight / shading stays visually coherent within the shot.
- **RunPod-only node.** Local dev does not have the VRAM headroom for SD 1.5 + ControlNet + IP-Adapter + DWPose preprocessors on the user's laptop. All Node 7 development and testing happens against a RunPod pod — CI / local pytest only exercises the adapter glue + manifest I/O, not the generation itself.
- **Runtime topology — ComfyUI runs ON the RunPod pod, not the laptop.** The laptop produces Node 2-6 outputs via CPU-only CLIs (`run_node{2..6}.py`), pushes the repo, and syncs the `<work-dir>` (queue.json + per-shot frames/keyposes/character_map/reference_map + reference crops) up to the pod. On the pod, `runpod_setup.sh` symlinks `custom_nodes/node_07_pose_refiner/` into ComfyUI's `custom_nodes/` and curl-downloads the weights pinned in `models.json`. ComfyUI boots on the pod at port 8188; either the operator loads `workflow.json` in the web UI, or `python run_node7.py --node6-result <path> --queue <path>` runs **on the pod** and POSTs `workflow.json` to ComfyUI's HTTP API. Outputs land in `<work-dir>/<shotId>/refined/` on the pod's disk. The local ComfyUI install (sibling `ComfyUI_windows_portable/`) is for authoring + smoke-testing the Node 3-6 ComfyUI wrappers only — Node 7 must never be executed there.
- **Deployment breaks the pipeline/node*.py template on purpose.** Nodes 2-6 live in `pipeline/` because they're pure-Python, GPU-agnostic. Node 7 is **ComfyUI workflow JSON + thin custom-node wrapper**: `custom_nodes/node_07_pose_refiner/workflow.json` is the authoritative graph; the node wrapper only marshals inputs/outputs and logs the seed + metadata. No `pipeline/node7.py`. Same graph runs locally (ComfyUI-Manager loads it) and on RunPod (curl+SHA pinned `models.json` downloads pin model versions).
- **Model management via BOTH ComfyUI-Manager AND explicit pins.** ComfyUI-Manager is the dev-convenience path for local testing (click-install from the manager GUI). RunPod production uses explicit `curl <url> && sha256sum --check` lines in `runpod_setup.sh` plus a `custom_nodes/node_07_pose_refiner/models.json` that declares (name, url, sha256, size_mb, destination) for every checkpoint / ControlNet / IP-Adapter / preprocessor weight. Dual-path avoids silent drift when the manager version and the pinned version diverge.
- **No QC gate in v1 — metadata logging only.** The original 7F sketch had an automatic identity-drift / double-lines regenerate loop. For v1 we log the seed + reference crop paths + CN strengths per generation to `node7_result.json` and leave QC as a future Node 11C retry hook. Early v1 output failures are more useful as training signal than as silent regenerations.
- **Transparent-background PNG output.** Each generated character lands on a transparent 512×512 canvas (alpha mask driven by the generated silhouette + luminance threshold → BnW). Node 8 composites these onto the final canvas.

Sub-steps:

- **7A. Load + validate inputs** — read `node6_result.json` + `queue.json` (for `poseExtractor` per character). Check `schemaVersion == 1`. For every `(shotId, keyPoseIndex, identity)` tuple in each `reference_map.json`, resolve the character's `poseExtractor` route. Raises `Node6ResultInputError` / `QueueLookupError` on manifest problems.
- **7B. Pose conditioning (per-character, routed)** — For each detection crop the rough key-pose PNG to the Node-5 bbox with margin. If `poseExtractor == "dwpose"`: run DWPose preprocessor → skeleton → DWPose ControlNet conditioning (strength 0.75). If `poseExtractor == "lineart-fallback"`: feed the rough crop through LineArt + Scribble CN at reduced strength (0.6 each) instead.
- **7C. Identity conditioning** — IP-Adapter-Plus loaded with Node 6's `referenceColorCropPath` for that `(identity, angle)`. Strength 0.8. Reference-Only CN available as a secondary tiebreaker at lower priority.
- **7D. Generation** — SD 1.5 + AnyLoRA (line-art-tuned) checkpoint + optional BnW LoRA. DPM++ 2M Karras, 25 steps, CFG 7.0, 512×512. Prompt template: `"line art, black and white, clean outlines, <character_name>, <angle_descriptor>"`. Negative prompt template: `"color, shading, blur, messy, duplicate, extra limbs"`. Seed logged per `(shotId, keyPoseIndex, identity)`.
- **7E. Post-process to BnW alpha PNG** — luminance-threshold the generated RGB to pure BnW line art; build alpha mask from the skeleton/lineart regions so the output is a transparent-background 512×512 PNG suitable for Node 8's compositor. Write to `<shotId>/refined/<keyPoseIndex>_<identity>.png`.
- **7F. Emit manifest** — per-shot `<shotId>/refined_map.json` listing every generated character (sourceKeyPoseFrame, identity, angle, seed, refinedPath, poseExtractor used, CN strengths). Aggregate `<work-dir>/node7_result.json` with one `ShotRefinedSummary` per shot (shotId, generatedCount, skippedCount, refinedMapPath). Node 8 reads `refined_map.json` directly.

**Invoke (live path is RunPod-only; `--dry-run` is the laptop smoke path):**

```bash
# Live (pod, ComfyUI on port 8188):
python run_node7.py --node6-result <path-to-node6_result.json> \
    --queue <path-to-queue.json> \
    [--comfyui-url http://127.0.0.1:8188] [--per-prompt-timeout 600] [--quiet]

# Laptop smoke (skips ComfyUI, records status="skipped" per generation):
python run_node7.py --node6-result <n6> --queue <q> --dry-run
```

CLI exit codes: `0` success (per-generation errors logged in `refined_map.json` still exit 0), `1` `Node7Error` subclass or shared `QueueLookupError`, `2` unexpected bug.

**Status (2026-04-25): DONE — both routes live-verified on RunPod.** `pipeline/cli_node7.py` + `run_node7.py` wrapper + `custom_nodes/node_07_pose_refiner/` (manifest.py, comfyui_client.py, orchestrate.py, __init__.py ComfyUI wrapper, both workflow JSONs, models.json, README.md) + `runpod_setup.sh` custom-node + weight bootstrap + `tests/test_node7.py` (47 tests, all green). **First live run (lineart-fallback both characters):** 2 generated / 0 skipped / 0 error, 36s wall time, 2-character synthetic smoke fixture. **DWPose verification (same fixture, Bhim flipped to `dwpose`, Jaggu still `lineart-fallback`):** 2 generated / 0 skipped / 0 error, 41s wall time, both routes in the same Node 7 invocation; per-character routing table works; Bhim PNG bytes differ from the lineart-fallback baseline (sha `d77d9b18…` vs `038f69e6…`) — DWPose actually contributes pose info; Jaggu PNG bytes are bit-identical (sha `5aa3c619…`) — deterministic-seed contract holds for unchanged routes. Runpod-slim pod image needed FOUR bringup steps (symlink `comfyui_controlnet_aux` into the running install, write `extra_model_paths.yaml` bridging to `/workspace/ComfyUI/models/`, add `weight_type: "standard"` to both workflow JSONs for the current `comfyui_ipadapter_plus` API, and pip-install `matplotlib` + `scikit-image` + `onnxruntime` into the venv for the DWPose preprocessor) — all captured in `tools/POD_NOTES_runpod_slim.md` and the "Node 7 — live-run addendum" section of `CLAUDE.md`.

### NODE 7 v2 — Phase 2 Flux migration (design-locked 2026-04-26; implementation in progress)

Purpose: Replace Node 7 v1's SD 1.5 + AnyLoRA generation stack with Flux Dev fp16 + Flat Cartoon Style LoRA + ControlNet Union Pro + XLabs Flux IP-Adapter v2. Driven by a 2026-04-26 real-shot test (TMKOC EP35 SH004) that succeeded mechanically end-to-end on the 4090 (33.8s, 1 shot / 1 ok / 0 failed) but produced anime-girl output instead of TMKOC characters because SD 1.5 + AnyLoRA is anime-trained and there is no public TMKOC LoRA. A bare-bones hug-test workflow on the same pod (Flux Dev fp8 + Flat Cartoon Style v1.2 + ControlNet Union Pro, single generation, no IP-Adapter) generated dramatically better output (~70s on the 4090) — recognizable Tappu + Champak Lal in TMKOC style — making the migration a clear win.

**Architecture decisions (locked 2026-04-26 — full rationale + 14-decision walkthrough in `CLAUDE.md` "Node 7 v2 — locked decisions"):**

1. **Base model: Flux Kontext Dev fp16** — image-editing-specialized variant of Flux Dev with native multi-image reference support. fp16 default on A100 80GB; fp8 fallback for 4090-class GPUs via `--precision fp8`. Pinned in `models.json` as both variants.
2. **Style LoRA: Flat Cartoon Style v1.2** (Civitai 644541, version 740091) at strength 0.75. No public TMKOC LoRA exists; closest-matching generic style LoRA. Phase 2d trains a custom TMKOC v1 LoRA to replace it.
3. **Pose ControlNet: ControlNet Union Pro (Shakker-Labs)** — single CN model that routes via `SetUnionControlNetType` (`openpose` for humans, `lineart` for non-humans). Replaces v1's three separate CN models (`control_v11p_sd15_lineart.pth`, `control_v11p_sd15_scribble.pth`, `control_v11p_sd15_openpose.pth`). Strength 0.65.
4. **Identity injection: XLabs Flux IP-Adapter v2** (requires `x-flux-comfyui` custom node from XLabs-AI; `flux-ip-adapter-v2.safetensors` weight ~1 GB). Strength 0.8. Phase 2e adds optional per-character LoRAs that stack on top.
5. **Generation mode: img2img with denoise=0.55** (REVERSES v1's txt2img decision). Flux Kontext Dev is purpose-built for img2img refinement; rough composition (positions, scale, silhouette) is preserved while line quality + identity are regenerated. Flux's superior denoising resolves v1's "rough pixels would bleed" concern.
6. **Conditioning scales: ControlNet 0.65 + IP-Adapter 0.8** (tuned from hug-test proof-of-concept).
7. **Resolution: 1280×720 native** (matches source MP4; both dims clean multiples of 16; no per-shot resize math). v1's 512×512 was a SD 1.5 VRAM compromise; Flux on A100 80GB has the headroom to go native.
8. **Sampler/scheduler/steps: dpmpp_2m_sde + simple + 40 steps + FluxGuidance 4.0 + CFG 1.0.** Max-quality picks: SDE variant resolves fine details (mustache hairs, hoodie strings); `simple` is Flux's native trained scheduler; 40 is the diminishing-returns knee for `dpmpp_2m_sde`; FluxGuidance 4.0 (vs BFL default 3.5) tightens prompt adherence; CFG 1.0 is mandatory for Flux.
9. **Per-character LoRAs (Phase 2e): plan architecture, defer training.** `CharacterSpec` schema gains optional `characterLoraFilename` + `characterLoraStrength` (additive per #10). Trained when style LoRA is locked; bootstrapped via Phase 2b IP-Adapter + ai-toolkit (ostris) on A100 80GB; ~$5-15 + 1.5h GPU time per character.
10. **Backward compatibility: schemaVersion stays at 1; all changes additive.** No field renames / removals / type changes. Phase 1 fixtures + `characters.json` files + `node7_result.json` files all still load through Phase 2 schemas. New optional fields get sane defaults so old data round-trips.
11. **Hardware + precision: A100 80GB + Flux Dev fp16 default; fp8 fallback via `--precision fp8` flag.** A100 80GB is the smallest GPU that fits the full Phase 2 fp16 stack with stacked Phase 2e LoRAs. fp16 quality benefits over fp8 are subtle but real for fine line art + stacked LoRAs. Per-batch cost on RunPod A100 80GB community spot: ~$1.20-2.20 for a 10-shot batch.
12. **Phase 1 weights archived (`deprecated: true` flag), not deleted.** `models.json` schema gains `deprecated`, `deprecatedSince`, `deprecatedReason`, `scheduledRemovalDate` fields. Phase 1 weights marked deprecated 2026-04-26 with scheduled removal 2026-10-26 (6-month rollback window). `runpod_setup.sh` honors `DOWNLOAD_DEPRECATED` env var (default false → fresh pods skip deprecated weights).
13. **Architecture template unchanged from Phase 1.** Workflow JSON + thin custom-node wrapper + `pipeline/cli_node7.py`. No `pipeline/node7.py`. Phase 2 changes contained to: 1 new file (`workflow_flux_v2.json`); modifications to `orchestrate.py` + `models.json` + `cli_node7.py` + `runpod_setup.sh` + `test_node7.py`. Phase 2 workflow JSON locks 16 ComfyUI node IDs (10 / 11 / 12 / 20 / 21 / 30 / 31 / 40 / 50 / 51 / 60 / 61 / 70 / 80 / 90 / 100 / 110); re-exporting from ComfyUI's GUI must preserve them OR update `orchestrate.py`'s `NODE_*` constants in the same commit.
14. **Failure mode unchanged from Phase 1: log + continue, no QC gate in v1.** No auto-detect-and-regenerate for identity drift / double lines / wrong style — each is its own ML problem with high false-positive rates. Operator's eyes are the QC gate; Node 8's substitute-rough fallback keeps timing intact when generation fails. Future Node 11C retry hook is the right home for operator-level retry logic. Phase 2 ADDITIONS to `RefinedGeneration` manifest entries (per #10's additive rule): `workflowName`, `precision`, `characterLoraFilename` for failure-pattern diagnosis.

**Sub-steps (Phase 2 implementation roadmap; each phase is its own ship-checklist commit, mirroring Nodes 8-11 per-node discipline):**

- **7v2-A. Phase 2a — Flux + Style LoRA + Union CN integration.** New `workflow_flux_v2.json` (txt2img mode for first shipping iteration; img2img comes in 2c). Updated `models.json` (Flux Dev fp16/fp8 + T5-XXL fp16/fp8 + CLIP-L + Flux VAE + Flat Cartoon Style v1.2 + ControlNet Union Pro added; Phase 1 weights flipped to `deprecated: true`). Updated `runpod_setup.sh` (Flux weight downloads + `DOWNLOAD_DEPRECATED` env var handling). New `--workflow {v1,v2}` and `--precision {fp16,fp8}` CLI flags on Node 7 (default still v1 for safety; v2 callable via flag). Forward-compat: all flags pass through Node 11 unchanged.
- **7v2-B. Phase 2b — Add XLabs Flux IP-Adapter.** Clone `x-flux-comfyui` custom node in `runpod_setup.sh`. Pin `flux-ip-adapter-v2.safetensors` (~1 GB) in `models.json`. Wire IP-Adapter nodes into `workflow_flux_v2.json`. Identity-preservation test against the TMKOC fixture: same character generated across multiple key poses should have consistent face / outfit.
- **7v2-C. Phase 2c — Switch Node 7 default to v2 (img2img mode, denoise=0.55).** Flip `--workflow` default from `v1` to `v2`. Switch `workflow_flux_v2.json` from txt2img to img2img mode (VAEEncode the rough crop, KSampler `denoise=0.55`). Phase 1 path stays callable via `--workflow=v1` until cleanup commit.
- **7v2-Rev. Phase 2-revision — Per-character bbox crop + BnW line-art prompts + Flat Cartoon LoRA bypass (post-design-review correction of Phase 2c).** Phase 2c flipped node 80 to VAEEncode but fed it the WHOLE-FRAME keypose (broke Phase 1 locked decision #5 — per-character generation) AND shipped colored-output prompts (broke Part 1's BnW deliverable). Phase 2-revision fixes both: (1) `_run_one_task` pre-crops keypose to (Node-5 bbox + 20% margin) via new `_prepare_rough_bbox_crop` helper; the bbox crop becomes node 50's input; pose preprocessor + VAEEncode + KSampler all operate on character-only pixels. (2) Prompts swapped to "clean black ink line art ... white background, no fill, no color" + reject "color, fill, shading, scene, furniture" (the negative no longer rejects "monochrome" — that was Phase 2c's bug). (3) New `STYLE_LORA_STRENGTHS` per-LoRA strength table (`flat_cartoon_v12 → 0.0` bypass, `tmkoc_v1 → 0.75` locked decision #2 production); locked decision #2 ("style LoRA at 0.75") survives intact for the LoRA we actually want to use. Phase 2d training-data approach also flipped from synthetic Path A bootstrap → user's storyboard scene cuts directly. **110 Node 7 tests pass (47 Phase 1 + 63 Phase 2; 6 new Phase 2-revision tests + 1 modified prompt-template test); 455 repo-wide, zero regressions.**
- **7v2-D. Phase 2d — Train TMKOC line-art LoRA.** Use the user's storyboard scene cuts as training data (Phase 2-revision approach, NOT Path A synthetic bootstrap). Storyboard cuts are clean digital BnW lines on white BG, characters in shot positions, light-line BG furniture, no fill / no color — already in the target aesthetic, so dataset stage is `scp` only. Train on A100 with ai-toolkit (ostris); rank=16, LR=1e-4, ~2000 steps. Ship `tmkoc_style_v1.safetensors` to `models.json` (URL + sha256 fields). Once shipped, `--style-lora=tmkoc_v1` picks up `STYLE_LORA_STRENGTHS["tmkoc_v1"] = 0.75` (locked decision #2 production value) automatically. Per-iteration cost: ~$3-5 GPU + ~4-8 hrs human time across 1-2 iterations.
- **7v2-E. Phase 2e — Train per-character LoRAs (one commit per character).** Per character (TAPPU first, then CHAMPAK_LAL, then any future characters): bootstrap training data via Phase 2b IP-Adapter generating 100+ pose variations from the model sheet → curate best 60-80 → caption with CogVLM/GPT-4V → train on A100 with ai-toolkit (~1.5h, ~$5-15 per character). Ship `<NAME>_v1.safetensors` to `models.json`. Populate `characters.json.characterLoraFilename` per character (`CharacterSpec` schema gains the field per #9, additive). Workflow gains chained second `LoraLoader` (node ID `"21"`) parameterized per-detection.
- **7v2-F. Phase 2f — Fix Node 5 background-line detection bug (DONE 2026-04-28).** The original sketch ("Otsu fallback when bbox > 70% of frame + ROI hint via metadata") was a heuristic-based size check; the user pointed out that BG vs character lines have a much better semantic signal — luminance (dark bold black ~0-50 vs light grey BG ~80-180). Phase 2f replaces Otsu with a fixed luminance threshold (default 80, `--dark-threshold N` to tune) + 3×3 morphological closing to seal small outline-overlap gaps. Side effect: writes `<shot>/dark_lines/<filename>.png` per keypose (BnW: black ink on white BG, BG furniture erased). Node 7's bbox crop reads from `dark_lines/` when present (falls back to raw keypose for pre-Phase-2f work dirs), so Flux's VAEEncode + ControlNet operate on character-only pixels with no BG furniture for the negative prompt to fight at generation time. 70 Node 5 tests pass (+20 new) + 114 Node 7 tests pass (+4 new); 479 repo-wide, zero regressions. Schema additions are additive: `CharacterMap` gains `darkThreshold` + `darkLinesDir`; `Node5Result` gains `darkThreshold`. `schemaVersion` stays at 1.
- **7v2-G. Phase 2g — Simplify Node 6 (always pick "front" angle when IP-Adapter handles identity).** Make Node 6 angle picking optional; document fallback. Reduces Node 6's per-key-pose work since Phase 2b's IP-Adapter does identity correction regardless of which reference angle was chosen.

**Invoke (after Phase 2c switches default):**

```bash
# Default Phase 2 path (Flux Dev fp16 + img2img, A100 80GB):
python run_node7.py --node6-result <path-to-node6_result.json> --queue <path-to-queue.json>

# Force Phase 1 path (SD 1.5 + AnyLoRA + DWPose / lineart-fallback):
python run_node7.py --node6-result <n6> --queue <q> --workflow=v1

# Phase 2 on a 4090 (fp8 precision, smaller VRAM):
python run_node7.py --node6-result <n6> --queue <q> --precision=fp8

# Same flags pass through Node 11:
python run_node11.py --input-dir <i> --work-dir <w> --workflow=v2 --precision=fp16
```

**Status (2026-04-27): Phase 2a + 2b + 2c + 2d-prep + 2d-fixup SHIPPED — Flux + Style LoRA + Union CN + XLabs IP-Adapter + img2img@denoise=0.55 + `--style-lora` flag with tmkoc_v1 placeholder; v2 is the default workflow; class_type + path bugs caught in live-pod debug fixed.** 104 Node 7 tests pass (47 Phase 1 + 57 Phase 2; 449 repo-wide, zero regressions). Phase 2d-run pending — waiting on user to download rendered TMKOC episode frames for true-style training data. Shipped: `custom_nodes/node_07_pose_refiner/workflow_flux_v2.json` (single workflow JSON handling both routes via per-detection node 51 class swap + node 61 SetUnionControlNetType; 16 locked Phase 2 node IDs); modified `orchestrate.py` (v1/v2 dispatch + Flux variable substitution + precision-aware UNET selection); modified `models.json` (schema bumped with `deprecated`/`deprecatedSince`/`deprecatedReason`/`scheduledRemovalDate`/`precision` fields; Phase 1 weights flipped to `deprecated:true` with 2026-10-26 removal date; Phase 2 weights added: Flux Dev fp16/fp8 + T5-XXL fp16/fp8 + CLIP-L + Flux VAE + Flat Cartoon Style v1.2 + ControlNet Union Pro); modified `runpod_setup.sh` (honours `DOWNLOAD_DEPRECATED` env var, default false skips deprecated weights/custom-nodes for ~6.5 GB savings on fresh pods); `--workflow {v1,v2}` (default `v1`) + `--precision {fp16,fp8}` (default `fp16`) flags on `pipeline/cli_node7.py` AND `pipeline/cli_node11.py` (passthrough to Node 7 subprocess); `RefinedGeneration` manifest schema gained `workflowName`/`precision`/`characterLoraFilename` (additive, Phase 1 records load with `v1`/`fp8`/`None` defaults); `CharacterSpec` schema gained `characterLoraFilename`/`characterLoraStrength` (additive, Phase 1 `characters.json` files load with `None`/0.85 defaults; new validation rejects path-like LoRA filenames); Node 2 propagates the new fields into `queue.json`. Default `--workflow=v1` means existing pipelines keep working unchanged; v2 is opt-in until Phase 2c lands. Hug-test proof-of-concept (Flux Dev fp8 + Flat Cartoon Style v1.2 + Union CN + DWPose preprocessor + txt2img, single generation) lives at `_pod_out/flux_tmkoc_test_workflow.json` with output `_pod_out/flux_test/flux_test_tappu_hug_00001_.png` — Phase 2a's workflow_flux_v2.json builds on this with the locked v2 sampler/guidance/resolution defaults.

**Phase 2b shipped 2026-04-27.** Wired XLabs Flux IP-Adapter v2 into the existing `workflow_flux_v2.json` via 3 new locked node IDs: 22 (`Load Flux IPAdatpter` — upstream class_type typo `IPAdatpter` and input field `ipadatper` preserved verbatim per the upstream registration), 23 (LoadImage for the reference COLOR crop from Node 6E), 24 (`Apply Flux IPAdapter`). KSampler's model input rewires from node 20 (style LoRA output) to node 24 (IP-Adapter wrapped model). `ip_scale = 0.8` per locked decision #6 (XLabs default is 0.93). `models.json` gained 2 new weight pins (`flux-ip-adapter-v2.safetensors` ~1 GB and `clip-vit-large-patch14.safetensors` ~600 MB CLIP-L vision encoder) + 1 new custom-node clone (`x-flux-comfyui` from XLabs-AI).

**Phase 2c shipped 2026-04-27.** 96 Node 7 tests pass (47 Phase 1 + 49 Phase 2; 441 repo-wide, zero regressions). TWO architecturally significant flips:

1. **Generation mode: txt2img → img2img.** `workflow_flux_v2.json` node 80 swapped from `EmptySD3LatentImage(1280, 720)` to `VAEEncode(pixels=node-50, vae=node-12)` — the rough crop now feeds into the latent via Flux's VAE. KSampler `denoise` dropped from 1.0 (txt2img — pure noise → image) to 0.55 (img2img — preserves rough composition while regenerating identity + line quality). Locked decision #5: Flux Kontext Dev's superior denoising resolves Phase 1's "rough pixels would bleed into output" concern — at 0.55 the messy pencil scribbles vanish but the underlying composition (positions, scale, silhouette) stays. Output dimensions match the rough's native dims (Node 4 wrote them at source MP4 resolution); Node 8's compositor doesn't care about exact output dims since it places refined PNGs at bbox positions per Node 8's locked decision #1.
2. **CLI default: `--workflow=v1` → `--workflow=v2`.** `DEFAULT_WORKFLOW` constant in `orchestrate.py` flipped from `"v1"` to `"v2"`. Phase 2 v2 is now the production default — running `python run_node7.py --node6-result <n6> --queue <q>` (no `--workflow` flag) uses the Flux + Style LoRA + Union CN + XLabs IP-Adapter + img2img stack. Phase 1 path stays callable via `--workflow=v1` for the entire 6-month deprecation window per locked decision #12 (until 2026-10-26). The `--precision {fp16,fp8}` flag (default `fp16`) becomes operationally relevant since v2 actually uses it.

`V2_DENOISE = 0.55` constant added to `orchestrate.py`; the v2 parameterizer re-asserts it on every generation so a hand-edited JSON can't silently revert to txt2img's denoise=1.0. CLI success line for v2 now reports `precision=<value>` alongside `workflow=v2` (was hidden under v1 because precision is ignored there). 5 new tests cover img2img wiring (node 80 = `VAEEncode` with pixels from 50 + vae from 12 in the shipped JSON), denoise lock (KSampler denoise = 0.55 in shipped JSON + parameterizer re-asserts it), v2-as-default (DEFAULT_WORKFLOW == "v2", cfg.workflow == "v2" with no override, CLI success line shows `workflow=v2 precision=fp16`), and v1-still-callable-via-flag. 3 existing tests updated to pass `workflow="v1"` explicitly where they meant Phase 1 specifically (`test_cn_strengths_for_dwpose`, `test_cn_strengths_for_lineart_fallback`, `test_cn_strengths_for_v1_explicit_returns_v1_split`). **Phase 2d-prep shipped 2026-04-27.** 104 Node 7 tests pass (47 Phase 1 + 57 Phase 2; 449 repo-wide, zero regressions). Shipped the integration infrastructure for Phase 2d's TMKOC v1 style LoRA without yet training the actual safetensors weight (Phase 2d-run is a separate live-pod follow-up per the runbook below). New `--style-lora {flat_cartoon_v12,tmkoc_v1}` flag (default `flat_cartoon_v12`) on `pipeline/cli_node7.py` + `pipeline/cli_node11.py` + ComfyUI wrapper `style_lora` dropdown — parameterizes `workflow_flux_v2.json` node 20's `lora_name` based on the choice. `orchestrate.py` gained `STYLE_LORA_FILENAMES` table + `STYLE_LORA_CHOICES` + `DEFAULT_STYLE_LORA` constants; `OrchestrateConfig.__post_init__` validates the field. `tmkoc-style-v1` placeholder entry added to `models.json` with `url: "TODO..."` (`runpod_setup.sh` skips it gracefully until a real weight URL+sha256 ships) — destination `models/loras/tmkoc_style_v1.safetensors`. Two new files in `tools/phase2d/`: `PHASE2D_TRAINING_PLAYBOOK.md` (full Path A bootstrap → curate → caption → train → validate → ship runbook; 6-step procedure with cost/time estimates: ~$10-15 GPU + ~12-18 hours human time per LoRA across 2-3 iterations) and `ai_toolkit_config_template.yaml` (locked Flux LoRA training params: rank=16, LR=1e-4, 2000 steps, batch_size=1, quantize=true, sample_every=250 — operator copies to a per-run name and fills 3 TODO fields before launching ai-toolkit). 8 new tests cover the flag plumbing (CLI accepts/rejects + default `flat_cartoon_v12` + tmkoc_v1 swap), parameterizer wiring (node 20 `lora_name` flips per choice), config validation (invalid choice raises `ValueError`), and the contract between `STYLE_LORA_FILENAMES` and `models.json` destinations.

**Phase 2d-run is the next ship-checklist step** — actually train the TMKOC v1 LoRA on a live A100 pod by following `tools/phase2d/PHASE2D_TRAINING_PLAYBOOK.md`, then a follow-up commit fills in `models.json`'s `tmkoc-style-v1` URL + sha256 from the trained safetensors weight.

**Phase 2d-fixup shipped 2026-04-27 (post-live-pod-debug).** Live-pod work on 2026-04-27 booted ComfyUI on a fresh A100 80GB pod, downloaded all Phase 2 weights, and tried to verify the workflow_flux_v2.json end-to-end. Two real bugs surfaced that would have blocked Phase 2d-run / Phase 2e-run when actually invoking ComfyUI's HTTP API:

1. **Wrong `class_type` strings on workflow JSON nodes 22 + 24.** Phase 2b's commit pinned `"Load Flux IPAdatpter"` and `"Apply Flux IPAdapter"`. ComfyUI's `/object_info` endpoint reported these classes as NOT REGISTERED — but `LoadFluxIPAdapter` and `ApplyFluxIPAdapter` (no spaces, no typo) WERE registered. Reading `x-flux-comfyui/nodes.py` source revealed the issue: ComfyUI workflow JSON's `class_type` field uses the keys of `NODE_CLASS_MAPPINGS` (the internal class names — clean), NOT the values of `NODE_DISPLAY_NAME_MAPPINGS` (the GUI menu labels — typo'd). The original Phase 2b agent-research conflated the two.
2. **Wrong IP-Adapter weight destination path in `models.json`.** Phase 2b shipped `destination: "models/ipadapter-flux/flux-ip-adapter-v2.safetensors"`, but `LoadFluxIPAdapter`'s `ipadatper` input field reads from the directory registered by x-flux-comfyui as `xlabs_ipadapters` — which the source code defines as `os.path.join(folder_paths.models_dir, 'xlabs', 'ipadapters')` = `models/xlabs/ipadapters/`. Files in `models/ipadapter-flux/` would never be discovered by `folder_paths.get_filename_list("xlabs_ipadapters")`.

The Phase 2d-fixup commit corrects:
- `workflow_flux_v2.json` — class_type strings on nodes 22 + 24 + the file-level `_comment` describing them
- `models.json` — IP-Adapter destination + role descriptions for both `flux-ip-adapter-v2` and the `x-flux-comfyui` custom-node entries
- `orchestrate.py` — comment block + `_require_node` human-name strings on the Phase 2 v2 ID-validation table
- `tests/test_node7.py` — `test_shipped_workflow_flux_v2_ipadapter_class_types` pinned strings + `_minimal_v2_template` placeholder + `test_v2_parameterize_missing_ipadapter_node_raises` regex match
- `custom_nodes/node_07_pose_refiner/README.md` — Phase 2b roadmap row + 17-row workflow node ID table + footer note explaining the wire-protocol class_type vs GUI display name distinction

**The `ipadatper` input FIELD name (typo'd) IS real and stays as-is** — that's how XLabs registered the field name in `LoadFluxIPAdapter.INPUT_TYPES`. Phase 2d-fixup does NOT touch that. Only the class_type strings (used in workflow JSON's `class_type` field) and the destination path were wrong.

449 tests pass after the fix. Pod was terminated to save budget; Phase 2d-run resumes when user has line-art training data ready.

**Phase 2-revision shipped 2026-04-28 (post-design-review).** Phase 2c shipped img2img + flipped the v2 default, but during a real-shot test on a fresh pod the operator caught a fundamental mismatch: Phase 2c's prompts asked for "flat cartoon style ... bright daytime colors" and rejected "monochrome" — biasing v2 toward COLORED TMKOC scenes. But Part 1's locked spec is BnW line art on white BG with characters-only (no BG furniture), and Phase 1 locked decision #5 says per-character generation (NOT whole-frame inpaint). Phase 2c had broken both contracts: feeding the WHOLE-FRAME keypose into VAEEncode pulled BG furniture and other characters into Flux's view, and the colored prompts produced colored TMKOC scenes instead of BnW per-character keyposes. Phase 2-revision corrects three things at once:

1. **Per-character bbox crop, NOT whole frame.** `_run_one_task` now calls a new `_prepare_rough_bbox_crop` helper that crops the keypose to (Node-5 bbox + 20% margin), clamps to image bounds, and resizes so longest edge ≤ 768 with both dims rounded down to multiples of 16 (Flux requirement). The bbox crop becomes node 50's input — pose preprocessor + VAEEncode + KSampler all operate on character-only pixels. The parameterizer accepts a new `rough_image_override` parameter so production runs pass the bbox-crop path while parameterizer-only unit tests fall back to `task.keyPosePath`.
2. **BnW line-art prompts, NOT colored TMKOC.** `V2_POSITIVE_PROMPT_TEMPLATE` swapped from "flat cartoon style ... bright daytime colors" → "clean black ink line art, white background, no fill, no color". `V2_NEGATIVE_PROMPT` swapped from rejecting "monochrome" → rejecting "color, fill, shading, scene, furniture" (the negative no longer rejects "monochrome" — that was Phase 2c's bug). Net effect: v2's prompt explicitly asks for what Part 1's locked spec actually wants.
3. **Per-LoRA strength override.** New `STYLE_LORA_STRENGTHS` table maps `flat_cartoon_v12 → 0.0` (LoRA still loads but is bypassed because it biases toward color, conflicting with Part 1's BnW deliverable) and `tmkoc_v1 → 0.75` (locked decision #2 production value, applied automatically once Phase 2d-run ships the custom-trained LINE-ART LoRA). Locked decision #2 ("style LoRA at 0.75") survives intact for the LoRA we actually want to use; the placeholder is bypassed via per-LoRA strength rather than removing the LoraLoader chain (which would require rewiring nodes 30 + 24). `workflow_flux_v2.json` node 20 strength updated to `0.0` in JSON; `V2_STYLE_LORA_STRENGTH` constant retained for backwards compat (= `STYLE_LORA_STRENGTHS["tmkoc_v1"]` = 0.75).

Phase 2d training-data approach also flipped from synthetic Path A bootstrap → user's storyboard scene cuts directly. Storyboard cuts are clean digital BnW lines on white BG with characters in shot positions and light-line BG furniture — the same target aesthetic v2 produces. They're already curated by the artist, so Phase 2d-prep's "generate dataset via Phase 2c img2img + curate 60-80 best" step is now obsolete. `tools/phase2d/PHASE2D_TRAINING_PLAYBOOK.md` revised end-to-end: dataset stage is `scp` only (no GPU), captioning explicitly drops color references and uses "TMKOC line art" trigger. Per-iteration cost drops from ~$10-15 + 12-18 hrs (Path A) to ~$3-5 + 4-8 hrs (storyboard cuts).

The Phase 2-revision commit updates: `orchestrate.py` (new `_prepare_rough_bbox_crop` helper + `STYLE_LORA_STRENGTHS` table + `V2_BBOX_*` constants + new `rough_image_override` parameter on `_parameterize_workflow_v2` + `_run_one_task` pre-crops for v2 + revised `V2_POSITIVE_PROMPT_TEMPLATE` + `V2_NEGATIVE_PROMPT`), `pipeline/cli_node7.py` (revised `--style-lora` and `--workflow` help text), `workflow_flux_v2.json` (node 20 strength → 0.0; nodes 50 + 80 _comments mention bbox crop; file-level _comment updated), `models.json` (revised `flat-cartoon-style-v12` and `tmkoc-style-v1` roles), `tools/phase2d/PHASE2D_TRAINING_PLAYBOOK.md` (revised end-to-end for storyboard-cuts approach + line-art captioning), `custom_nodes/node_07_pose_refiner/README.md` (Phase 2-revision footer + workflow node ID table notes), `tests/test_node7.py` (6 new tests + 1 modified prompt-template test). **110 Node 7 tests pass; 455 repo-wide, zero regressions.** 

Net effect: v2's deliverable matches Phase 1's deliverable shape (per-character BnW line-art PNGs that Node 8 composites onto a white-BG frame at bbox positions), but with Flux's superior generation quality + character identity preservation via XLabs IP-Adapter. The 14 original Phase 2 locked decisions stand; Phase 2-revision corrects the implementation regression that contradicted decisions #4 (txt2img→img2img mode), #5 (per-character generation), and the BnW deliverable from Part 1.

**Phase 2f shipped 2026-04-28 (Node 5 BG-line fix that also benefits Node 7).** The user's storyboard convention puts character outlines at luminance ~0-50 (dark bold black) and BG furniture / safe-area marks at ~80-180 (light grey). The original Otsu binarization in Node 5 (locked decision #1, 2026-04-23) treated all dark-vs-light pixels as one foreground class, which broke when BG lines were drawn — Otsu would either swallow the actual characters into one giant connected component or falsely tag the BG as the character. Phase 2f replaces Otsu with a fixed luminance threshold (default 80, `--dark-threshold N` tunable) followed by 3×3 morphological closing (`scipy.ndimage.binary_closing`) to seal 1-2 pixel gaps where character outlines crossed BG lines (e.g., where the artist drew BG on top of character at the intersection point). The new pipeline produces a clean binary mask of character ink only; Node 5's CC + reconcile + position-bin + identity-zip steps work on that cleaner input. Side effect: each shot grows a `<shot>/dark_lines/<filename>.png` per keypose (white BG = 255, character ink = 0, RGB mode). Node 7's `_prepare_rough_bbox_crop` resolver prefers `dark_lines/<filename>` over the raw keypose when present, so Flux's VAEEncode + ControlNet operate on character-only pixels with no BG furniture for the negative prompt to fight at generation time. Falls back to raw keypose for pre-Phase-2f work dirs (no migration needed). 4 new Node-7 tests cover the resolver (prefers dark_lines/, falls back when missing) + end-to-end (bbox crop pulls dark_lines content vs raw keypose). 20 new Node-5 tests cover the four new helpers (`_extract_dark_lines`, `_close_outline_gaps`, `_save_dark_lines_png`, `_wipe_dark_lines_dir`) + end-to-end integration (dark_lines/ dir created + filenames match keyposes/ + polarity correct + rerun-wipe + character_map.json schema additions + node5_result.json schema additions + CLI flag). **70 Node-5 tests pass + 114 Node-7 tests pass; 479 repo-wide, zero regressions.**

### NODE 8 — Scene Assembly (Per Key Pose Frame)
Purpose: Composite each key pose's per-character refined PNGs into a single source-MP4-resolution frame, so Node 9 has complete pictures to translate-and-copy held frames from.

**Architecture decisions (locked 2026-04-25):**
- **The bbox is the single source of truth for character placement.** Node 5 wrote each character's bbox in original-frame coordinates; Node 7 cropped the rough using that bbox; Node 8 places the refined character back using the same bbox. Symmetric — what came out (size + position) is what goes back in. No new positioning logic, no inferring positions from other signals.
- **Feet-pinned scaling, NOT stretch-to-fit.** SD's 512×512 canvas isn't fully filled by the character — there's white margin around the silhouette. Stretching the 512×512 into the bbox would float the feet inside the bbox instead of anchoring them at the bbox bottom. Algorithm: find the lowest non-white pixel in the 512×512 (= refined character's feet), scale the refined PNG by `bbox.height / character_height_in_512`, paste it onto the canvas centered on `(bbox.centerX, bbox.bottomY)` so the feet land at the bbox bottom. Standard 2D-cel anchoring; works for standing/walking/jumping/running shots alike since Node 5's bbox already moves with the character.
- **Output canvas resolution = source MP4 resolution exactly.** Whatever Node 3 decoded into. Reasons: (a) Node 9 will do translate-and-copy of held frames at the same dims, mismatched res forces a per-frame resize; (b) any project-level normalization can be applied later without contract change. Get original dims by probing one of Node 3's `frame_*.png` files per shot (~1 ms; cheaper than adding `frameWidth`/`frameHeight` to `node3_result.json`).
- **Background = solid white.** Part 1's deliverable is BnW line art on white. Black would invert polarity; transparent defers a decision Node 10's encoder will have to make anyway.
- **Z-order = bbox-bottom-y descending** (lower-on-screen drawn last = "closer to camera"). Standard 2D-cel convention; future override (e.g. metadata `z` field) is non-blocking.
- **Line-weight unification = threshold to BnW only**, no dilate/erode normalize in v1. Cheapest path; full stroke-width unification is a future tuning pass against real client shots.
- **Substitute-rough on Node 7 failure, NOT fail-loud.** When `refined_map.json` shows `status="error"` (or the refined PNG is empty/transparent), Node 8 substitutes the rough key-pose frame at the same bbox location and appends a structured warning to `composed_map.json`. CLI still exits 0 — same warn-and-reconcile pattern as Node 5. Keeps timing intact for Node 9; operator gets a clear list of which key poses need re-generation.
- **Core logic in `pipeline/node8.py` + thin ComfyUI wrapper** in `custom_nodes/node_08_scene_assembler/`. Same Option C template as Nodes 3-6. Pure-Python (PIL + numpy), GPU-agnostic.
- **Rerun safety:** each `<shotId>/composed/` is wiped of stale `*_composite.png` before each run so `composed_map.json` always matches the directory exactly.
- **Single-threaded.** Per-shot composite is ~10-30 ms per key pose at 1280×720; parallelism is a future Node 11 concern.

Sub-steps:

- **8A. Load + validate inputs** — read `<work-dir>/node7_result.json`, check `schemaVersion == 1`. For each shot, chase pointers to per-shot `refined_map.json`, `character_map.json` (Node 5), `keypose_map.json` (Node 4). Probe one `<shotId>/frames/frame_NNNN.png` per shot to get the original-frame `(W, H)`. Raises `Node7ResultInputError` on any structural problem.
- **8B. Per-key-pose canvas build** — create a `(W, H)` RGB white canvas. Look up each character's bbox from `character_map.json` for that key pose.
- **8C. Per-character paste (feet-pinned)** — for each character with a refined PNG (`status == "ok"` in `refined_map.json`): open the 512×512 refined PNG, find lowest non-white pixel = `feet_y_in_512`, compute `scale = bbox.height / (feet_y_in_512 - top_y_in_512)`, resize aspect-preserving, threshold to BnW, paste centered on `(bbox.centerX, bbox.bottomY)`. For `status != "ok"`: copy the rough key-pose pixels in the bbox region instead (substitute-rough), append a warning entry to `composed_map.json.warnings[]`.
- **8D. Z-order resolution** — sort detections per key pose by `bbox.bottomY` ascending and paste in that order so lower-on-screen characters land last (= drawn on top).
- **8E. Emit manifests** — write `<shotId>/composed/<keyPoseIndex>_composite.png` (RGB, white bg, no alpha), per-shot `composed_map.json`, and the aggregate `<work-dir>/node8_result.json` (one `ShotComposeSummary` per shot: shotId, keyPoseCount, composedCount, substituteCount, composedMapPath). Node 9 reads `composed_map.json` directly.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node8.py --node7-result <path-to-node7_result.json>

# Windows embedded Python (local dev):
"C:\...\python_embeded\python.exe" run_node8.py --node7-result <n7>

# Override background (default white):
python run_node8.py --node7-result <n7> --background white
```

CLI exit codes: `0` success (substitute-rough warnings still 0), `1` `Node8Error` subclass (`Node7ResultInputError`, `RefinedPngError`, `CompositingError`), `2` unexpected error.

**Status (2026-04-25): DONE — design-locked + shipped same day.** `pipeline/node8.py` + `pipeline/cli_node8.py` + `run_node8.py` wrapper + `custom_nodes/node_08_scene_assembler/__init__.py` thin ComfyUI wrapper + `tests/test_node8.py` (51 tests, 258 repo-wide, all green). Pure-Python (PIL + numpy), no GPU, no new dependencies. Verified end-to-end on the embedded Python: feet-pinned scaling places refined character feet exactly at `bbox.bottomY` (within ±2 px LANCZOS tolerance, never in the bbox middle which would indicate a stretch-to-fit regression); BnW threshold output contains only `0` and `255` pixel values; rerun wipes `<shotId>/composed/` first; substitute-rough fallback fires on Node 7 `status="error"` AND on empty-but-decodable refined PNGs, recording `node7-error` and `refined-empty-or-unreadable` warnings to `composed_map.json` while exiting 0; an unfillable slot (refined PNG dead AND rough fallback dead — e.g. bbox entirely off-canvas) raises `RefinedPngError`. Live-pod smoke not run yet; deferred until first real client shot since Node 8 has no GPU dependency and the existing pod fixture's bboxes are degenerate (`[0, 0, 512, 512]` for both characters from the synthetic `make_smoke_node6_workdir.py` scaffold).

### NODE 9 — Timing Reconstruction (Re-apply Held Frames)
Purpose: Rebuild the full per-frame sequence from Node 8's per-key-pose composites + Node 4's per-frame timing map (`keypose_map.json`). Output is one PNG per frame of the original shot — ready for Node 10 to encode back to MP4 at the original timing.

**Architecture decisions (locked 2026-04-25):**
- **Translate-and-copy on a fresh white canvas — NO AI on held frames.** For every frame in the original timeline: if it's a key pose's anchor frame, copy Node 8's composite as-is; if it's a held frame, paste Node 8's composite onto a fresh white canvas at offset `(dx, dy)` from `keypose_map.json`. PIL's standard paste auto-clips at boundaries; uncovered regions stay white. Zero regeneration on held frames is the whole reason Node 4 went translation-aware in the first place; non-negotiable.
- **Whole-frame translation, not per-character.** Node 4's `(dy, dx)` is computed by phase correlation against the rough's anchor frame and is whole-frame. Node 9 translates Node 8's already-composited frame as a single image. If a future shot needed per-character translation, Node 4's classifier would have split those characters into separate key poses already.
- **Output canvas resolution = Node 8 composite resolution = source MP4 resolution.** Pure translation, no resampling.
- **Exposed-region fill = solid white.** Matches Node 8's white-background contract.
- **Output frame numbering = 1-indexed, 4-digit zero-padded `frame_NNNN.png`.** Same convention as Node 3's frame extraction; Node 10 globs `<shot>/timed/frame_*.png` directly.
- **Inputs (one required CLI path):** `--node8-result <path>`. Node 9 chases pointers from there: `node8_result.json` → per-shot `composed_map.json` → shot root → `keypose_map.json` (Node 4). No second `--node4-` flag needed.
- **Fail-loud on missing composed PNG, NOT substitute-rough.** Held frames REQUIRE the anchor's composed PNG; without it, every held frame in that key pose's group is unreconstructable. Node 9 has no meaningful fallback (substituting the rough would silently downgrade output quality), so it raises `TimingReconstructionError` with a clear message naming the missing key pose so the operator can re-run Node 8.
- **Total-frame-count mismatch is a hard error** (`FrameCountMismatchError`). Every frame in `keypose_map.totalFrames` must belong to exactly one key pose's anchor or heldFrames list (Node 4 invariant); a mismatch means an upstream bug.
- **Translation offsets larger than canvas are NOT errors.** Off-canvas translates produce mostly-white frames, which is mathematically valid for end-of-slide shots where the character has slid off-screen. Original rough showed white at that point too, so timing is preserved. No warning.
- **Same frame index in multiple keyPoses is a hard error** (`KeyPoseMapInputError`). Node 4 invariant violation; refuse to silently overwrite.
- **Core logic in `pipeline/node9.py` + thin ComfyUI wrapper** in `custom_nodes/node_09_timing_reconstructor/`. Same Option C template as Nodes 3-6/8. Pure-Python (PIL + numpy), GPU-agnostic.
- **Rerun safety:** each `<shotId>/timed/` is wiped of stale `frame_*.png` before each run so `timed_map.json` always matches the directory exactly.
- **Single-threaded.** Per-frame translate is ~1 ms at typical 1280×720; per-shot total is sub-second. Parallelism is Node 11's concern.

Sub-steps:

- **9A. Load + validate inputs** — read `<work-dir>/node8_result.json`, check `schemaVersion == 1`. For each shot, chase pointers to per-shot `composed_map.json` (Node 8) and sibling `keypose_map.json` (Node 4). Validate `keypose_map.json`'s totalFrames + per-key-pose anchor + heldFrames lists against the Node 4 invariants (every frame index appears exactly once, all indices in `[1, totalFrames]`, no duplicate keyPoseIndices). Raises `Node8ResultInputError` / `KeyPoseMapInputError` on any structural problem.
- **9B. Map key poses → timing slots** — build a per-shot `frame_index → (keyPoseIndex, offset_dy_dx, isAnchor)` lookup so each frame index resolves directly to its source composite + translation in O(1).
- **9C. Translate-and-copy per frame** — for each `frame_index` in `1..totalFrames`: open the source composite (cached per keyPoseIndex within a shot to avoid re-decoding), build a fresh white canvas at composite's `(W, H)`, paste the composite at offset `(dx, dy)`, save to `<shot>/timed/frame_<idx:04d>.png`. Anchor frames (offset == [0, 0]) are bit-identical copies of Node 8's composite (no translation step needed).
- **9D. Assemble + verify** — count `<shot>/timed/frame_*.png` files; must equal `keypose_map.totalFrames`. Mismatch → `FrameCountMismatchError` with shot ID + expected/actual counts.
- **9E. Emit manifests** — write `<shotId>/timed_map.json` (per-frame record: `{frameIndex, sourceKeyPoseIndex, offset, composedSourcePath, timedPath, isAnchor}`) and aggregate `<work-dir>/node9_result.json` (one `ShotTimingSummary` per shot: shotId, totalFrames, keyPoseCount, anchorCount, heldCount, timedMapPath). Node 10 reads `timed_map.json` for the encode order.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node9.py --node8-result <path-to-node8_result.json>

# Windows embedded Python (local dev):
"C:\...\python_embeded\python.exe" run_node9.py --node8-result <n8>
```

CLI exit codes: `0` success, `1` `Node9Error` subclass (`Node8ResultInputError`, `KeyPoseMapInputError`, `TimingReconstructionError`, `FrameCountMismatchError`), `2` unexpected error.

**Status (2026-04-25): DONE — design-locked + shipped same day.** `pipeline/node9.py` + `pipeline/cli_node9.py` + `run_node9.py` wrapper + `custom_nodes/node_09_timing_reconstructor/__init__.py` thin ComfyUI wrapper + `tests/test_node9.py` (42 tests, 300 repo-wide, all green). Pure-Python (PIL + numpy), no GPU, no new dependencies. Verified end-to-end on the embedded Python: anchor frames are bit-identical pixel copies of Node 8's composite; held frames are translate-and-copy (positive `(dy, dx)` shifts content right + down, negative shifts left + up); off-canvas translates produce fully-white frames (mathematically valid for end-of-slide shots); rerun wipes `<shot>/timed/` first; chases `keypose_map.json` from shot root via single `--node8-result` flag (no second `--node4-` needed). Hard-fails on missing composed PNG (`TimingReconstructionError`), totalFrames disagreement (`FrameCountMismatchError`), and any Node 4 invariant violation including duplicate `keyPoseIndex`, frames outside `[1, totalFrames]`, or the same frame appearing in multiple keyPoses' heldFrames lists (all `KeyPoseMapInputError`). Live-pod smoke not needed — Node 9 has zero GPU dependency and is fully exercised by the unit tests.

### NODE 10 — Output Generation (PNG → MP4)
Purpose: Encode each shot's full per-frame PNG sequence (from Node 9) into a single deliverable MP4 at 25 FPS. Output goes to a project-level `output/` directory so all deliverables collect in one place for client hand-off.

**Architecture decisions (locked 2026-04-25):**
- **ffmpeg via `imageio-ffmpeg` static binary, NOT system ffmpeg.** Same wheel Node 3 already brought in. Identical behavior on Windows + RunPod + CI.
- **Codec = H.264 (libx264)**, **pixel format = yuv420p**, **CRF = 18**, **preset = `medium`**. H.264+yuv420p is the maximum-compatibility deliverable; CRF 18 is visually lossless on BnW line art (which compresses extremely well anyway because of the large white regions). CRF is exposed via `--crf` for unusually tight file-size budgets; codec/preset/pixel-format are locked.
- **Frame rate = 25** (locked project convention; no `--fps` flag — silently accepting a different rate would corrupt the timing Node 9 carefully reconstructed).
- **Output location = `<work-dir>/output/<shotId>_refined.mp4`.** Project-level `output/` dir collects every shot's deliverable in one place for easy hand-off; per-shot location was the alternative but scatters deliverables.
- **Filename pattern = `<shotId>_refined.mp4`** (e.g. `shot_001_refined.mp4`).
- **Post-encode verification via ffprobe.** Check: file exists + size > 0; codec is `h264`; fps is `25`; nb_frames matches input PNG count (within ±1 for encoder rounding). Catches silent ffmpeg corruption (exit 0 but malformed file).
- **Do NOT delete upstream artifacts.** `timed/`, `composed/`, `refined/`, etc. all stay on disk for debugging and Part 2 (ToonCrafter) reuse.
- **Odd canvas dimensions are a hard error** (`FFmpegEncodeError`). libx264 requires even W and H; auto-padding would shift every character by half a pixel and silently desync from Node 9's translate-and-copy positions.
- **ffmpeg non-zero exit raises `FFmpegEncodeError`** with last 10 stderr lines attached (mirrors Node 3's pattern).
- **Inputs (one required CLI path):** `--node9-result <path>`. Node 10 chases pointers: `node9_result.json` → per-shot `timed_map.json` → per-shot `<shot>/timed/` directory. Probes one frame for dims.
- **Core logic in `pipeline/node10.py` + thin ComfyUI wrapper** in `custom_nodes/node_10_png_to_mp4/`. Same Option C template as Nodes 3-6/8/9. Pure-Python (subprocess + imageio_ffmpeg + json), GPU-agnostic.
- **Rerun safety: ffmpeg `-y` flag overwrites the output MP4.** Simpler than wipe-then-encode; ffmpeg handles atomicity. The `output/` dir itself is not wiped on rerun (so multi-shot batches can add new shots incrementally without losing earlier deliverables).
- **Single-threaded.** Per-shot encode is sub-second to a few seconds; ffmpeg already uses multiple cores per encode. Cross-shot parallelism is Node 11's concern.

Sub-steps:

- **10A. Load + validate inputs** — read `node9_result.json` (schemaVersion==1), chase pointers to per-shot `timed_map.json`, verify per-shot `<shot>/timed/` directory exists with the expected `frame_NNNN.png` count. Raises `Node9ResultInputError` / `TimedFramesError` on any structural problem.
- **10B. Probe canvas dimensions** — open one PNG (e.g., `frame_0001.png`) per shot, read `(W, H)`. If either is odd, raise `FFmpegEncodeError` with a clear "libx264 requires even dimensions; source MP4 was odd-dimensioned" message. Operator re-encodes source.
- **10C. ffmpeg encode** — `<ffmpeg> -y -hide_banner -loglevel error -framerate 25 -i <shot>/timed/frame_%04d.png -c:v libx264 -pix_fmt yuv420p -crf <crf> -preset medium <work>/output/<shotId>_refined.mp4`. Non-zero exit → `FFmpegEncodeError` with last 10 stderr lines.
- **10D. ffprobe verification** — `<ffprobe> -v error -select_streams v:0 -show_entries stream=codec_name,r_frame_rate,nb_frames -of json <output.mp4>`. Parse JSON; require `codec_name == "h264"`, `r_frame_rate` parses to 25.0 ± 0.01, `nb_frames` matches the input PNG count within ±1 (encoder rounding tolerance). Mismatch → `FFmpegEncodeError`.
- **10E. Emit aggregate manifest** — write `<work-dir>/node10_result.json` listing every shot's `{shotId, outputPath, frameCount, durationSeconds, codec, fps, fileSizeBytes}`. No per-shot manifest beyond this — Node 10's output is the deliverable itself, not a pointer.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node10.py --node9-result <path-to-node9_result.json>

# Windows embedded Python (local dev):
"C:\...\python_embeded\python.exe" run_node10.py --node9-result <n9>

# Tighter file size (smaller, slightly more visible artifacts):
python run_node10.py --node9-result <n9> --crf 23
```

CLI exit codes: `0` success, `1` `Node10Error` subclass (`Node9ResultInputError`, `TimedFramesError`, `FFmpegEncodeError`), `2` unexpected error.

**Status (2026-04-25): DONE — design-locked + shipped same day.** `pipeline/node10.py` + `pipeline/cli_node10.py` + `run_node10.py` wrapper + `custom_nodes/node_10_png_to_mp4/__init__.py` thin ComfyUI wrapper + `tests/test_node10.py` (42 tests, 342 repo-wide, all green). Pure-Python (subprocess + imageio_ffmpeg + json), no GPU, no new dependencies. Verified end-to-end on the embedded Python: real ffmpeg encode of synthesized 32×32 5-frame PNG sequences produces playable MP4s; codec/fps/frame-count round-trip correctly; CRF override works (CRF 18 default, CRF 23 alternate); odd canvas dims raise `FFmpegEncodeError` ("libx264 requires even W and H"); missing PNG in `1..N` gap raises `TimedFramesError` (Node 9 invariant guard); rerun is atomic via ffmpeg `-y` flag; multi-shot batches encode each into `<work-dir>/output/<shotId>_refined.mp4`; upstream `timed/` PNGs are NOT deleted after encode (locked decision #10 — intermediates kept for Part 2 ToonCrafter reuse). Live-pod smoke not needed — Node 10 has zero GPU dependency and is fully exercised by the unit tests including real ffmpeg invocations.

### NODE 11 — Batch Management
Purpose: Project-level orchestrator. Runs Nodes 2-10 in sequence against a single batch (one `metadata.json` + one `characters.json` + their referenced files), tracks per-node per-shot progress, supports per-node retries, and emits a single consumable batch report. Replaces the operator's current eight-command shell sequence with one CLI invocation.

**Architecture decisions (locked 2026-04-25):**
- **Subprocess each `run_nodeN.py` and read its exit code, NOT in-process import.** Each node already has a stable CLI + exit codes; subprocess matches what an operator does by hand → identical failure modes → easier to debug. ~50 ms interpreter spin-up per node is invisible next to the actual work.
- **A single Node 11 invocation runs the entire `queue.json` through Nodes 2-10 once.** Node 11 does NOT iterate batch-by-batch even though `queue.json.batches` exists -- every downstream node already processes all shots in one pass. One Node 11 invocation = one whole project end-to-end.
- **No resume capability in v1.** Each node is rerun-safe (wipes its own outputs first); a re-run regenerates everything. Documented as a known limitation; defer to v2 if/when projects scale to 500+ shots.
- **Single-threaded.** Sequential node execution; defer parallelism to v2.
- **Default retries per node = 0 (fail-fast); operator opts into resilience via `--retry-nodeN <int>` flags.** Most useful for Node 7 (most likely to flake on transient ComfyUI hangs / VRAM spikes).
- **Pre-Node-7 best-effort `nvidia-smi` shell-out.** Logs GPU + free VRAM if visible; warns but proceeds otherwise (the laptop --dry-run path has no GPU).
- **NO active VRAM monitoring or batch-size auto-reduce in v1.** Out of scope; if Node 7 OOMs, operator re-runs with smaller `batchSize` set in the form. Documented as a known limitation.
- **Stdout/stderr passes through Node 11 to the operator's terminal in real time.** Each line is also tee'd to `<work-dir>/node11_progress.jsonl` (newline-delimited JSON, append-only).
- **Exit-code semantics differ from Nodes 2-10:** all-succeed and partial-success both exit 0 (with `failedCount > 0` in `node11_result.json` for CI to read); 100% failure exits 1 (`BatchAllFailedError`). Node 11 owns the partial-success semantic because individual nodes can't (they fail the whole batch by design).
- **Core logic in `pipeline/node11.py` + thin ComfyUI wrapper** in `custom_nodes/node_11_batch_manager/`. Same Option C template as Nodes 3-6/8/9/10. Pure-Python (subprocess + json + datetime), GPU-agnostic.
- **Rerun safety:** Node 11 wipes `node11_progress.jsonl` + `node11_result.json` at start of each run; downstream nodes wipe their own outputs.
- **Dry-run:** `--dry-run` passes through to Node 7's `--dry-run`; other nodes ignore it (they don't have one).

Sub-steps:

- **11A. Pre-flight checks** — verify `--input-dir` exists and is readable; load metadata.json + characters.json minimally to confirm well-formed; if Node 7 will be live (not --dry-run), shell-out to `nvidia-smi` and log GPU info (warn but proceed if not available). Raises `InputDirError` on bad inputs.
- **11B. Sequential per-node execution** — for each node N in 2..10: build the appropriate `run_nodeN.py` argv (with the right `--node{N-1}-result` / `--queue` / `--node8-result` / etc. paths chained from the previous nodes' outputs), spawn the subprocess with stdout/stderr passthrough + JSONL tee, time it, capture exit code. On non-zero exit: retry up to `--retry-nodeN <int>` times (default 0 = no retry). On final non-zero: record as failed in the per-shot results.
- **11C. Per-shot status tracking** — Node 11's report aggregates per-shot per-node status by reading the downstream nodes' own per-shot manifests (e.g., `refined_map.json` per shot from Node 7). A shot is "succeeded" iff `<shotId>_refined.mp4` exists in `<work-dir>/output/` after Node 10. Otherwise "failed", with the failing node number recorded.
- **11D. Final report** — write `<work-dir>/node11_result.json` with per-shot per-node status + timing + final-MP4 path + total batch wall time. Also write the aggregate stats to stdout in the standard `[node11] OK ...` summary line.
- **11E. Exit-code resolution** — exit 0 if at least one shot succeeded (partial success counts as 0); exit 1 if all failed (`BatchAllFailedError`); exit 1 on `Node11Error` subclasses (`InputDirError`, `NodeStepError` if it escapes the retry-and-mark-failed loop); exit 2 on unexpected exception.

**Invoke:**

```bash
# Standard Python (RunPod, CI):
python run_node11.py --input-dir /path/to/input --work-dir /path/to/work

# Windows embedded Python (local dev with --dry-run for no-GPU):
"C:\...\python_embeded\python.exe" run_node11.py \
    --input-dir <i> --work-dir <w> --dry-run

# Allow Node 7 to retry twice on transient ComfyUI hangs:
python run_node11.py --input-dir <i> --work-dir <w> --retry-node7=2

# Override Node 10 quality (smaller files):
python run_node11.py --input-dir <i> --work-dir <w> --crf 23

# Quiet mode (suppress per-line summary; nodes still log to JSONL):
python run_node11.py --input-dir <i> --work-dir <w> --quiet
```

CLI exit codes: `0` success or partial success (`succeededShots > 0`), `1` `Node11Error` subclass (`InputDirError`, `NodeStepError`, `BatchAllFailedError`), `2` unexpected error.

**Status (2026-04-25): DONE — design-locked + shipped same day.** `pipeline/node11.py` + `pipeline/cli_node11.py` + `run_node11.py` wrapper + `custom_nodes/node_11_batch_manager/__init__.py` thin ComfyUI wrapper + `tests/test_node11.py` (46 tests, 388 repo-wide, all green). Pure-Python orchestrator (subprocess + json + datetime), no GPU, no new dependencies. Verified end-to-end on the embedded Python: subprocess-invokes `run_nodeN.py` for N in 2..10 in correct sequence with chained manifest paths; per-node retry policy via `--retry-nodeN <int>` flags (default 0 = fail-fast, tested at retries=0 and retries=2 for Node 7); pre-Node-7 `nvidia-smi` check is best-effort (warn-but-proceed when not available, skipped entirely under `--dry-run`); JSONL progress log records `batch_start` + per-step `node_step_start` / `node_step_complete` (with attempt + exitCode) + `batch_complete` events; final `node11_result.json` aggregates per-node + per-shot status (shot is "ok" iff `<work-dir>/output/<shotId>_refined.mp4` exists); Node 2 failure raises `NodeStepError` (can't proceed without queue.json); 100% per-shot failure raises `BatchAllFailedError`; partial success (1+ ok, 1+ failed) returns `Node11Result` cleanly without raising (CI reads `failedShots` from the JSON); `--dry-run` flag passes through to Node 7 only (other nodes ignore it); rerun wipes `node11_progress.jsonl` + `node11_result.json` first. With Node 11 shipped, **the entire 11-node pipeline is end-to-end complete**.

**Live RunPod verification (2026-04-25, RTX 4090):** ran `python3 run_node11.py --input-dir <i> --work-dir <w>` on the pod against a synthesized Node 1-shaped input dir (1 character, 5-frame 128×96 MP4). Result: 1 shot succeeded / 0 failed in **33.8 s** end-to-end. Per-node breakdown: Node 2 (0.32 s), Node 3 (0.42 s), Node 4 (0.40 s), Node 5 (0.53 s), Node 6 (0.59 s), **Node 7 LIVE SD generation via lineart-fallback workflow (30.4 s on the 4090)**, Node 8 (0.27 s), Node 9 (0.40 s), Node 10 (0.42 s); orchestration overhead ~2 s. Pre-Node-7 `nvidia-smi` check fired correctly (`NVIDIA GeForce RTX 4090, 23686 MB free` logged to JSONL). Final deliverable: 1.8 KB H.264 MP4 in `/workspace/<work>/output/shot_001_refined.mp4`. Bringup gap caught: **system python3 needs `pipeline/requirements.txt` installed** for Nodes 2-6 + 8-10 + 11 (which Node 11 invokes as subprocesses) — the runpod-slim venv has Comfy's deps but the system Python doesn't until `pip install -r requirements.txt` runs. New step added to `tools/POD_NOTES_runpod_slim.md`.

---

## Critical files / artifacts to be created

- HTML form (Node 1) — frontend for metadata capture.
- `metadata.json` (Node 1F → Node 2) — the canonical contract between UI and pipeline.
- ComfyUI workflow JSON (Nodes 3–10) — the actual render graph.
- `keypose_map.json` per shot + `node4_result.json` aggregate (Node 4D/4E) — the bridge between key-pose extraction and final timing reconstruction.
- `character_map.json` per shot + `node5_result.json` aggregate (Node 5E) — the bridge into Node 6's reference-sheet matcher.
- Refined MP4s (Node 10) — the deliverable.

## Reusable / external components

- FFmpeg — MP4↔PNG conversion (Nodes 3, 10).
- ComfyUI workflow graph — pipeline runtime for Node 7 (Node 7 custom-node wrapper submits `workflow.json` via ComfyUI API).
- DWPose preprocessor + DWPose ControlNet — humans-only skeleton pose guidance (Node 7B, `poseExtractor == "dwpose"`).
- LineArt + Scribble ControlNet stack — non-human (quadruped) pose fallback (Node 7B, `poseExtractor == "lineart-fallback"`).
- IP-Adapter-Plus — character-identity conditioning from Node 6E color reference crop (Node 7C). Reference-Only CN available as a secondary tiebreaker.
- SD 1.5 + AnyLoRA (line-art anime) checkpoint + optional BnW LoRA — BnW line-art generation (Node 7D). Exact versions pinned in `custom_nodes/node_07_pose_refiner/models.json` + `runpod_setup.sh`.

## Verification (end-to-end test)

1. Fill the HTML form for one test shot (1 character, known position, known duration).
2. Upload one MP4 shot + corresponding character model sheet.
3. Run the pipeline with batch size = 1.
4. Inspect intermediates: PNG sequence (Node 3), extracted key poses (Node 4), refined key poses (Node 8), reconstructed sequence (Node 9).
5. Open final MP4 (Node 10) and confirm: same duration as source, same held-frame timing, character matches model sheet, positioned per metadata.
6. Scale up to a multi-shot, multi-character batch to validate Nodes 5F, 7E, and 11.

## Follow-up (not in this plan)

After plan approval, create an **Excel workbook** mirroring this node structure — one row per sub-step (1A, 1B, … 11E) — with columns for Node, Sub-step, Purpose, Inputs, Outputs, Tools/Tech, and Notes. This file will serve as the editable working document for refining each node.
