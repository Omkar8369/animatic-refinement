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

### NODE 9 — Timing Reconstruction (Re-apply Held Frames)
Purpose: Rebuild the full-length sequence from the per-shot `keypose_map.json` (Node 4D) + the refined key-pose PNGs (Node 8).

- **9A. Read `keypose_map.json`** from Node 4D — one per shot; enumerates key poses plus each held frame's source index and `(dy, dx)` offset from the anchor.
- **9B. Map new key poses → timing slots** — each refined key pose inherits the same frame index its rough counterpart held.
- **9C. Replay held frames by translate-and-copy** — a held frame with offset `(0, 0)` is a pixel-duplicate of the refined anchor; a held frame with non-zero offset (slide) is the refined anchor translated by `(dy, dx)`. No AI regeneration on held frames.
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
