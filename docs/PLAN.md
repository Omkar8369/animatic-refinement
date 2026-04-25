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
- **5B. Per-key-pose detection** — load each key-pose PNG as grayscale, apply Otsu binarization (foreground = dark ink), run `scipy.ndimage.label` with 8-connectivity, drop any blob whose area is below `min_area_ratio × frame_area` (default 0.1%), merge any two bounding boxes whose IoU ≥ `merge_iou` (default 0.5; reunites floating details like a separate eye dot with the parent silhouette).
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
