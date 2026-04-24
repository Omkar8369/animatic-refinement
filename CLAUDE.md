# CLAUDE.md — Animatic Refinement Workflow

This file is auto-loaded by Claude Code. It is the **first thing** you (future Claude)
should read when starting a new session in this repo. Read it fully before acting.

## What this project is

A 2-part AI pipeline that replaces the manual animator step for 2D Indian cartoon
production (Chota Bhim style).

- **Part 1 (this repo, in progress):** rough MP4 animatic shot → BnW line-art MP4
  with reference-accurate characters in correct positions.
- **Part 2 (future, separate effort):** ToonCrafter frame interpolation to generate
  in-between poses.

Input per project: client-provided MP4 shots + manually drawn character model
sheets (8-angle horizontal strip, transparent background, full color) + per-shot
metadata captured via an HTML form (count, identity, position L/CL/C/CR/R,
duration in frames @ 25 FPS).

Target platform: ComfyUI on RunPod. Repo is cloned fresh on each RunPod pod
(that's why custom nodes live here, not in a registry).

## Build method — IMPORTANT, do not deviate

1. **One node at a time.** Discuss design → update plan/Excel if needed → write
   code → commit → move on. Never skip ahead.
2. **Commit per node.** Each node's history is independently reviewable.
3. **GitHub is the deployment bridge.** Local dev → push → RunPod clones.
4. **The canonical state of this project lives across SIX files that MUST
   stay in sync:**
   - `docs/PLAN.md` — design spec
   - `docs/Node_Plan.xlsx` — editable working spec (rows 1:1 with PLAN.md)
   - `CLAUDE.md` — Claude-facing session notes + status table + locked decisions
   - `README.md` — user-facing project status + how-to-run
   - `<node-folder>/README.md` (where a node has one) — contributor usage
   - `requirements.txt` — runtime dep aggregator that `-r`-includes each
     node's own `requirements.txt`

   Any design change, any status change ("Pending" → "DONE" → "NEXT"), any
   new dependency, any new user-facing invocation command updates ALL
   applicable files in one commit. "Canonical spec" used to mean just the
   two `docs/` files — that was too narrow and caused `README.md` +
   `requirements.txt` + `pipeline/README.md` to go stale across the ships
   of Nodes 1 and 2. The ship checklist below exists to kill that pattern.

## Per-node commit + deploy flow — IMPORTANT, follow every node

Claude Code runs in an isolated session worktree at
`.claude/worktrees/<session-id>/` on a branch named `claude/<session-id>`.
Parallel sessions don't clobber each other's work, but it means the flow to
get a node's code onto `main` (where RunPod clones from) is fixed:

1. **Commit on the session branch** with the standard trailer
   `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.
   One commit per node. Stage files by name, never `git add -A` (secrets risk).
2. **Fast-forward push to `origin/main`.** Because the session branch was cut
   from current `main` and `main` hasn't moved, the push is always a pure
   fast-forward — no merge commit, no PR overhead:
   ```
   git push origin HEAD:main
   ```
3. **User's main working copy is now stale.** The primary checkout at
   `C:\Users\Omkar Hajare\Desktop\download\animatic-refinement\` (where the
   user works when not in a Claude session) still points at the old `main`
   tip. Claude **cannot** pull there from inside the session worktree —
   git refuses to have `main` checked out in two worktrees at once.
   After every push, tell the user explicitly:
   > "Run `git pull` in your main working copy to sync."
4. **RunPod sees the new commit on its next `git pull` / fresh clone.** The
   deployment bridge closes the moment step 2 succeeds, regardless of
   whether the user has pulled locally yet.

## Per-node ship checklist — run through BEFORE the commit

This exists because Nodes 1 and 2 both shipped without catching that the
root-level scaffold files (`README.md`, `requirements.txt`, node folder
READMEs) had gone stale. Every future node ship must pass through this:

- [ ] **Code** — node's files written; no accidental secrets/creds.
- [ ] **Tests** — new tests added; ALL tests pass (previous nodes' too).
- [ ] **End-to-end smoke test** — the node runs against a real fixture in
  the environment the user will actually use (not just pytest).
- [ ] **`docs/PLAN.md`** — node section has a "locked decisions" block +
  sub-steps reflecting what was actually built (not the original sketch).
- [ ] **`docs/Node_Plan.xlsx`** — node's summary row + sub-step rows
  match PLAN.md. Round-trip via openpyxl preserves formatting.
- [ ] **`CLAUDE.md` status table** — this node → DONE, next node → NEXT.
- [ ] **`CLAUDE.md` locked-decisions section** — add this node's block.
- [ ] **`CLAUDE.md` Active work section** — remove resolved questions;
  stage the next node's open questions.
- [ ] **Root `README.md` status table** — must match CLAUDE.md's table.
- [ ] **Root `README.md` "Running Node X"** — add/update if the node has
  a user-facing invocation.
- [ ] **Root `requirements.txt`** — if the node added Python deps in its
  own requirements file, add a `-r <path>` include line here.
- [ ] **Node folder's own `README.md`** (if it has one) — reflect reality.
- [ ] **Drift grep** — before staging, these commands should return only
  expected hits (genuinely pending nodes, no direct package names):
  ```bash
  # Status of shipped nodes must agree across the 3 status tables.
  grep -n "Pending\|NEXT\|DONE" README.md CLAUDE.md docs/PLAN.md

  # Root requirements.txt must only contain comments, blank lines, and -r
  # includes — no bare package names (those belong in per-node requirements).
  grep -v "^#\|^\s*$\|^-r " requirements.txt   # should print nothing

  # No file should still describe the just-shipped node as Pending/Next.
  grep -n "Node N.*Pending\|Node N.*NEXT" README.md CLAUDE.md docs/PLAN.md
  ```
- [ ] **Commit message** — `Implement Node N: <name>` for node work;
  `chore:` for scaffold reconciliation. Include the Co-Authored-By trailer.
- [ ] **Stage by file name**, never `git add -A` (secrets risk).

## Repo map

```
frontend/               Node 1 (HTML form + metadata.json writer)
pipeline/               Node 2 + Node 11 (orchestrator, validator, batch mgr)
custom_nodes/           ComfyUI custom nodes, one folder per node 3-10
  node_03_mp4_to_png/
  node_04_keypose_extractor/
  node_05_character_detector/
  node_06_reference_matcher/
  node_07_pose_refiner/
  node_08_scene_assembler/
  node_09_timing_reconstructor/
  node_10_png_to_mp4/
workflows/              ComfyUI graph JSONs wiring the custom nodes
docs/                   PLAN.md + Node_Plan.xlsx (canonical design)
tests/                  Per-node + end-to-end tests
```

## Current status (update at end of each node)

| Node | Name                                   | Status   |
|------|----------------------------------------|----------|
| 1    | Project Input & Setup Interface        | **DONE — initial build, awaiting first real-shot test** |
| 2    | Metadata Ingestion & Validation        | **DONE — 26 tests pass; CLI + `run_node2.py` wrapper verified on embedded Python** |
| 3    | Shot Pre-processing (MP4 → PNG)        | **DONE — 20 tests pass; CLI + `run_node3.py` wrapper + ComfyUI wrapper verified; 125-frame end-to-end smoke test passes** |
| 4    | Key Pose Extraction                    | **DONE — 26 tests pass (72 repo-wide); CLI + `run_node4.py` wrapper + ComfyUI wrapper verified; translation-aware partition handles slide shots (one key pose with per-held-frame offsets)** |
| 5    | Character Detection & Position         | **DONE — 50 tests pass (122 repo-wide); CLI + `run_node5.py` wrapper + ComfyUI wrapper verified; end-to-end Node 2→3→4→5 smoke test passes (Bhim bound to L, Jaggu bound to R on real MP4); classical CC + Otsu + Strategy A positional identity** |
| 6    | Character Reference Sheet Matching     | **DONE — 34 tests pass (156 repo-wide); CLI + `run_node6.py` wrapper + ComfyUI wrapper verified; end-to-end Node 2→3→4→5→6 smoke test (`tests/_smoke_node6.py`) passes on embedded Python with synthesized RGBA sheets + MP4; alpha-island sheet slicing + Otsu silhouette recompute + 128×128 multi-signal scoring (IoU + symmetry + aspect + upper-region interior-edge density) + DoG/canny/threshold line-art; per-(identity, angle) crop cache; rerun wipes reference_crops/** |
| 7    | AI-Powered Pose Refinement             | **NEXT** |
| 8    | Scene Assembly                         | Pending  |
| 9    | Timing Reconstruction                  | Pending  |
| 10   | Output Generation (PNG → MP4)          | Pending  |
| 11   | Batch Management                       | Pending  |

## Node 1 — locked decisions (do not re-litigate)

User chose **1a + 2a + 3a** on 2026-04-22:

1. **Form shape: 1a** — one big form with repeating "+ Add shot" blocks
   (`frontend/index.html`).
2. **metadata.json delivery: 2a** — browser download via `Blob` + `<a download>`.
   No server, no POST. Operator manually moves files into the pipeline folder.
3. **Character identity dropdown: 3a** — separate Character Library page
   (`frontend/characters.html`) where the operator pre-registers every
   character. The shot form reads the library from `localStorage` to populate
   per-shot identity dropdowns.

Consequences locked in:
- MP4s are NOT uploaded by the form (browsers can't write to disk paths).
  The form captures filenames + previews only; operator copies MP4s into the
  pipeline input folder separately.
- Both pages persist drafts to `localStorage` (keys prefixed
  `animaticRefinement.*.v1`).
- `characters.json` carries an `angleOrderConfirmed` flag — defaults to
  `true` since the user confirmed the canonical 8-angle order on 2026-04-23
  against the Bhim reference template. Node 6 still respects `false` and
  fails loudly so an operator who forks the template to a new angle layout
  can't silently ship a bad order.

## Node 2 — locked decisions (do not re-litigate)

Resolved on 2026-04-23:

1. **Runs locally AND on RunPod.** Pure-Python, zero GPU deps. Local runs
   go through `run_node2.py` at the repo root, which inserts the repo root
   onto `sys.path` before importing `pipeline.cli` — necessary because the
   Windows-portable embedded Python uses a `python313._pth` that ignores
   `PYTHONPATH`. RunPod can use the same wrapper or `python -m pipeline.cli`
   directly; both work.
2. **Hard-fail the entire batch** on any validation error. No per-shot
   skipping. Node 2 runs in seconds, its errors are operator-data errors,
   and silently dropping a shot would waste a RunPod batch later.
3. **Schema validation via pydantic v2**, `extra="forbid"` on every model.
   Already in ComfyUI's embedded Python, so the only new RunPod dep is
   `pydantic>=2.5,<3` in `pipeline/requirements.txt`.

Consequences locked in:
- **Flat input layout:** `metadata.json` + `characters.json` + sheet PNGs
  + shot MP4s all side-by-side in one folder passed as `--input-dir`.
- **Output:** `queue.json` written into the input folder (or
  `--output-file`) with absolute `mp4Path` / `sheetPath` entries, chunked
  into batches of `project.batchSize`. This is the contract Node 3 reads.
- **CLI exit codes:** 0 success, 1 validation error (`Node2Error`
  subclass), 2 unexpected error.
- **Error messages list ALL offenders**, not just the first, so the
  operator can fix everything in one pass.
- **Typed error hierarchy** in `pipeline/errors.py`: `Node2Error` base +
  `MissingInputError`, `SchemaValidationError`, `CrossReferenceError`,
  `DuplicateShotIdError`, `ShotIdSequenceError`.

## Node 3 — locked decisions (do not re-litigate)

Resolved on 2026-04-23:

1. **ffmpeg via `imageio-ffmpeg` pip wheel** (bundles a static ffmpeg
   binary). No system ffmpeg dep. Identical behavior on Windows embedded
   Python, RunPod Linux, and CI.
2. **Per-shot folders:** `<work-dir>/<shotId>/frame_NNNN.png` (NNNN
   4-digit zero-padded, 1-indexed). Isolated per shot so Node 4's scan
   loop is trivially per-shot.
3. **Fail-fast on hard errors, warn-and-continue on frame-count drift.**
   Queue format issues, missing MP4s, ffmpeg crashes, numbering gaps →
   raise + abort. Actual-vs-expected frame-count mismatch → structured
   `FrameCountWarning` in `node3_result.json`, batch continues. Node 9
   will use the actual count when reconstructing timing.
4. **Core logic in `pipeline/node3.py` + thin ComfyUI wrapper** in
   `custom_nodes/node_03_mp4_to_png/`. Same code runs from CLI, tests,
   CI, and ComfyUI. This is the architectural template for every
   pipeline-runtime node going forward: business logic in `pipeline/`,
   GPU-agnostic; ComfyUI adapter is JUST INPUT_TYPES + RETURN_TYPES +
   a one-liner into the core. Don't fork logic into the wrapper.
5. **1:1 decode, no `-r` flag.** The rough MP4 is already 25 FPS per
   locked convention; any `-r` would silently resample. Node 3's
   contract is "decode and nothing else".

Consequences locked in:
- **`queue.json` schemaVersion guard.** Node 3 loudly refuses any
  `schemaVersion != 1` so a future Node 2 contract change can never
  silently half-run Node 3.
- **Rerun wipes stale frames** in `<work-dir>/<shotId>/` before decoding
  so `_manifest.json` always matches the directory exactly.
- **Typed error hierarchy** in `pipeline/errors.py` grew a
  `PipelineError` base; `Node2Error` and `Node3Error` are siblings under
  it. Node 3 subclasses: `QueueInputError`, `FFmpegError`,
  `FrameExtractionError`. Warnings are data, not exceptions.
- **CLI:** `run_node3.py --queue <q> --work-dir <w>` at the repo root;
  `python -m pipeline.cli_node3` equivalent on standard Python. Exit
  codes `0` success (warnings are still `0`), `1` `Node3Error`, `2`
  unexpected.
- **Node 3 requires `imageio-ffmpeg>=0.5,<1`** — added to
  `pipeline/requirements.txt`. Root `requirements.txt` already
  `-r`-includes it, so RunPod gets it automatically.

## Node 4 — locked decisions (do not re-litigate)

Resolved on 2026-04-23:

1. **Translation-aware partition, not pixel-identity.** Each frame is
   phase-correlated (FFT cross-power spectrum) against the current
   key-pose anchor to estimate `(dy, dx)`, then aligned MAE is computed
   over the overlap region. This is critical: animatic shots often show
   a character sliding across the frame without changing pose. A naïve
   pixel-diff would flag every intermediate slide frame as a new key
   pose, forcing Node 7 to re-refine an identical pose N times. Phase
   correlation recovers the translation; aligned MAE measures similarity
   *after* translating, so slides collapse to ONE key pose + per-held
   `(dy, dx)` offsets that Node 9 replays by translate-and-copy.
2. **Global default threshold of 8.0** on 0–255 grayscale aligned MAE.
   Exposed as a CLI flag (`--threshold`) and a ComfyUI node input, not
   adaptive per shot. User can tune per project if needed; 8.0 was
   chosen to tolerate encoder jitter on clean line-art animatics
   without missing real pose changes.
3. **No minimum held-run length.** We dropped the proposed "≥3 frames
   or it becomes a new key pose" floor. With translation-aware
   comparison the slide false-positive case is already solved, and
   imposing a floor would silently promote a 1- or 2-frame flicker
   into the nearest key pose's hold — changing the timing. Node 9
   replays exactly what Node 4 writes, frame-accurate.
4. **`max_edge = 128` downscale for the compare.** Frames are
   LANCZOS-downscaled so `max(H, W) = 128` before FFT + MAE. Offsets
   detected at low-res are scaled back to full-resolution pixels on
   write (`keypose_map.json` carries full-res `(dy, dx)`). Keeps FFT
   fast (~10ms/frame) and makes the MAE metric resolution-independent.
5. **JSON shape:** `keypose_map.json` per shot + aggregate
   `node4_result.json` at work-dir root. Per-shot schema:
   ```
   {schemaVersion: 1, shotId, totalFrames, sourceFramesDir,
    keyPosesDir, threshold, maxEdge,
    keyPoses: [{keyPoseIndex, sourceFrame, keyPoseFilename,
                heldFrames: [{frame, offset: [dy, dx]}, ...]},
               ...]}
   ```
   Aggregate carries one `ShotKeyPoseSummary` per shot (shotId,
   totalFrames, keyPoseCount, sourceFramesDir, keyPosesDir,
   keyPoseMapPath). Node 9 reads `keypose_map.json` directly (PLAN.md
   Node 9 9A/9C updated to match).
6. **Key-pose copies preserve source filenames.** `keyposes/` holds
   literal copies of the chosen frames, e.g. `frame_0004.png` — NOT
   renamed to `key_pose_01.png`. Node 5 (character detection) and
   Node 7 (pose refinement) already know how to locate a frame by its
   source name; preserving that identity means zero rename-juggling
   between nodes.
7. **Option C thin ComfyUI wrapper** (same template as Node 3). All
   logic in `pipeline/node4.py`; `custom_nodes/node_04_keypose_extractor/`
   only declares `INPUT_TYPES` / `RETURN_TYPES` and calls
   `extract_keyposes_for_queue()`. Same code runs from CLI, tests, CI,
   and ComfyUI.
8. **Single-threaded partition.** Per-shot FFT is ~10ms/frame at
   `max_edge=128`; a 500-frame shot finishes in ~5s. Multiprocessing
   overhead isn't worth the complexity at this scale, and RunPod pods
   aren't CPU-bound by Node 4 anyway (Node 7's GPU pass dominates).
9. **Rerun safety:** `keyposes/` is cleared of stale `frame_*.png`
   before each run so `keypose_map.json` always matches the directory
   exactly. Mirrors Node 3's frame-folder wipe.

Consequences locked in:
- **Input contract:** reads `<work-dir>/node3_result.json` produced by
  Node 3. Loudly refuses `schemaVersion != 1` (mirrors Node 3's guard
  on `queue.json`).
- **Output contract for Node 9:** `keypose_map.json` is the single
  source of truth for timing reconstruction. Every frame in
  `totalFrames` is accounted for either as a key-pose `sourceFrame` or
  as a `heldFrame.frame`; Node 9 replays in that order, copying the
  refined key-pose PNG and translating by `offset` for each held.
- **Typed error hierarchy** grew `Node4Error` base +
  `Node3ResultInputError` (malformed / missing / stale manifest) and
  `KeyPoseExtractionError` (per-frame decode/compare failures,
  resolution mismatches against the anchor). All under the shared
  `PipelineError` root from Node 3.
- **CLI:** `run_node4.py --node3-result <path> [--threshold N]
  [--max-edge N] [--quiet]` at the repo root;
  `python -m pipeline.cli_node4` equivalent on standard Python. Exit
  codes `0` success, `1` `Node4Error`, `2` unexpected.
- **Node 4 adds `numpy>=1.26,<3` and `pillow>=10,<12`** to
  `pipeline/requirements.txt`. Both ship with ComfyUI's embedded
  Python; listing them here guarantees RunPod + CI install them too.

## Node 5 — locked decisions (do not re-litigate)

Resolved on 2026-04-23:

1. **Classical connected-components, not ML.** Chota Bhim animatics are
   hand-drawn line art where the animator deliberately separates
   characters. Otsu binarization + `scipy.ndimage.label` (8-connectivity)
   handles the 95% case; an IoU-based bbox merge pass + progressive
   `binary_erosion` reconcile covers the remaining 5% (floating detail →
   merge, touching characters → erode apart). ML alternatives would need
   a 2+ GB download per RunPod pod and overfit to photos/anime rather
   than stylized Indian-cartoon outlines. User option B.
2. **Detect on every key pose**, not once per shot. CC is milliseconds
   per frame and safer — a character can enter/exit mid-shot, and
   metadata's `characterCount` is per-shot so we'd still have to
   re-detect per key pose regardless. User explicit choice.
3. **Position binning: 25/20/10/20/25 split of normalized frame width.**
   L = `[0.00, 0.25)`, CL = `[0.25, 0.45)`, C = `[0.45, 0.55)` (narrow
   10% exact-centre band), CR = `[0.55, 0.75)`, R = `[0.75, 1.00]`.
   Each detection's normalized centre-x falls into exactly one bin.
   User option B.
4. **Identity = Strategy A (positional), v1.** Sort detections
   left→right by centre-x; sort metadata characters left→right by
   position rank (L<CL<C<CR<R, ties break on metadata order); zip. No
   ML similarity check. If real-world mismatch rates exceed ~5%, add a
   v2 Strategy B (reference-sheet-similarity verification) as a
   post-pass without replacing Strategy A.
5. **Warn AND reconcile on count mismatch — never fail-fast.**
   Too-many → sort by area descending, drop smallest until count
   matches, append `count-mismatch-over` warning. Too-few → progressive
   `binary_erosion` x1/x2/x3 (stop as soon as count meets expected),
   append `reconcile-eroded` warning. Still-wrong after max iterations
   → append `reconcile-failed` warning; Node 6 fails cleanly on that
   key pose. Only genuine I/O errors (missing manifest, unreadable
   PNG) raise. User explicit choice.

Consequences locked in:
- **Output contract for Node 6:** `<shotId>/character_map.json` sits
  at the shot root (next to `keyposes/`), not inside `keyposes/`. Per
  key pose it carries `detections[]` (identity, expectedPosition,
  boundingBox `[x, y, w, h]`, centerX normalized, positionCode, area)
  plus `warnings[]` (one record per reconcile action).
- **Input contract:** both `--node4-result` AND `--queue` paths are
  required. Node 4's manifest has the frames, queue.json has the
  character metadata; they're deliberately separate so Node 2's
  output remains the sole source of truth for expected characters.
- **Typed error hierarchy** grew `Node5Error` base +
  `Node4ResultInputError` (manifest missing / malformed / stale
  keyposes folder), `QueueLookupError` (missing queue, or queue
  does not contain a shotId that appears in `node4_result.json`),
  `CharacterDetectionError` (PNG decode / analysis failure). All
  under the shared `PipelineError` root.
- **CLI:** `run_node5.py --node4-result <path> --queue <path>
  [--min-area-ratio 0.001] [--merge-iou 0.5] [--quiet]` at the repo
  root; `python -m pipeline.cli_node5` equivalent on standard Python.
  Exit codes `0` success (reconcile warnings are still `0`), `1`
  `Node5Error`, `2` unexpected.
- **Node 5 adds `scipy>=1.11,<2`** to `pipeline/requirements.txt`.
  Ships with ComfyUI's embedded Python; listing here guarantees
  RunPod + CI install it.
- **Angle detection is NOT in Node 5.** Body-angle estimation
  (front / 3⁄4 / profile / back) was moved to Node 6 where it
  belongs alongside reference-sheet 8-angle matching. Node 5 stays
  focused on detect + position + identity; Node 6 handles the
  similarity-based problem with its own tool.

## Node 6 — locked decisions (do not re-litigate)

Resolved on 2026-04-23:

1. **Sheet slicing via alpha-island bbox; RGB-only sheets fail loud.**
   `scipy.ndimage.label` on `alpha > 0` → bboxes sorted left→right →
   verify count == 8. If a sheet PNG has no alpha channel (mode != RGBA),
   Node 6 raises `ReferenceSheetFormatError` naming the file and telling
   the operator to re-export with a transparent background. Node 1B
   already validates transparent-background PNGs at upload; auto-falling
   back to Otsu-on-grayscale would reward ignoring that gate and add a
   second untested code path for zero real benefit.
2. **Canonical 8-angle order confirmed on 2026-04-23** against the
   user's Bhim reference template:
   `back, back-3q-L, profile-L, front-3q-L, front, front-3q-R, profile-R, back-3q-R`.
   **L/R refers to the character's anatomical left/right**, not the
   viewer's. Identifiers in code/JSON use ASCII `3q` (matches what
   `frontend/characters.js` emits); `¾` in CLAUDE.md/PLAN.md prose is
   equivalent.
3. **Structured-fail on `characters.json.conventions.angleOrderConfirmed
   == false`.** No interactive prompt (RunPod + ComfyUI are both
   non-interactive). Error `AngleOrderUnconfirmedError` carries the
   canonical-order text and tells the operator to flip the flag.
   `frontend/characters.js` now defaults the flag to `true` for
   newly-created libraries — the gate still trips if an operator
   deliberately sets it false or hand-edits `characters.json`.
4. **Angle matching: classical, per key pose.** For each Node-5
   detection: crop the key-pose PNG to the bbox → Otsu → largest CC =
   rough silhouette. For each of the 8 reference-angle silhouettes
   (alpha mask of each reference crop), scale-normalize both to a
   128×128 canvas (aspect-preserving pad, centered on centroid) and
   score via a weighted combination of: (i) silhouette IoU,
   (ii) horizontal-symmetry score, (iii) bbox aspect ratio,
   (iv) interior-edge density in the upper head region (disambiguates
   front from back, which have near-identical silhouettes). Pick the
   angle with max score. **No ML, no CLIP, no OpenPose/MediaPipe in
   v1.** Chota Bhim art is crisp flat-fill cartoon with distinctive
   silhouettes per angle; ML preprocessors add GB-scale downloads per
   pod for an 8-way classification problem that classical methods
   handle well. If real-shot hit rate falls below ~90% we can promote
   to a CLIP-based tiebreaker as a post-pass without changing Node 6's
   contract.
5. **Silhouette recomputed in Node 6, no retro-change to Node 5.**
   Node 5's `character_map.json` stays text-only (bboxes + identities
   + warnings); Node 6 re-derives masks from those bboxes. Known edge
   case: detections Node 5 reconciled via `binary_erosion` may produce
   slightly noisy recomputes, but 8-way angle matching is coarse
   enough to tolerate it.
6. **Line-art conversion of color reference = classical DoG
   (Difference of Gaussians).** Per chosen 8-angle color crop:
   luminance channel → DoG (σ₁=1.0, σ₂=2.0) → threshold to binary →
   OR-combined with the alpha-channel's boundary outline → optional
   1-pixel thinning. Pure numpy/scipy. An ML lineart extractor
   (lllyasviel annotators, etc.) would add 100–300 MB cold-start per
   pod for marginal quality gain on already-clean cartoon art. CLI
   flag `--lineart-method {dog,canny,threshold}` keeps the door open
   for a swap without code changes.
7. **Per-key-pose angle selection.** A single shot can contain
   multiple key poses showing the character from different angles
   (Node 4 already split them as separate key poses); per-key-pose
   is the natural granularity. Classical scoring is ~ms per pose, so
   there's no runtime reason to go coarser. Matches Node 5's
   per-key-pose cadence — `character_map.json` and `reference_map.json`
   share the same `(shotId, keyPoseIndex, identity)` key shape.
8. **Option C thin ComfyUI wrapper** (same template as Nodes 3/4/5).
   All logic in `pipeline/node6.py`; `custom_nodes/node_06_reference_matcher/`
   only declares `INPUT_TYPES` / `RETURN_TYPES` and calls into the core.
9. **Rerun safety:** each `<shotId>/reference_crops/` directory is
   wiped of stale PNGs before a new Node-6 run so `reference_map.json`
   always matches the directory exactly. Mirrors Nodes 3/4/5's
   wipe-before-write pattern.
10. **Single-threaded.** Parallelism is a Node 11 concern.

Consequences locked in:
- **Inputs (three required CLI paths):**
  - `--node5-result <path>` (from Node 5)
  - `--queue <path>` (for per-character `sheetPath`)
  - `--characters <path>` (for `conventions.angleOrderConfirmed`;
    not carried in queue.json, so we read characters.json directly)
- **Outputs:**
  - `<shotId>/reference_map.json` at the shot root (next to
    `character_map.json` and `keyposes/`). Per key pose per detection:
    `{identity, keyPoseIndex, selectedAngle, scoreBreakdown,
     referenceColorCropPath, referenceLineArtCropPath}`.
  - `<shotId>/reference_crops/<identity>_<angle>.png` (color crop) and
    `_lineart.png` (DoG line-art crop). Crops are cached per unique
    `(identity, angle)` within a shot so multiple key poses picking
    the same angle reuse the same file.
  - Aggregate `<work-dir>/node6_result.json`: one
    `ShotReferenceSummary` per shot (shotId, keyPoseCount,
    referenceMapPath, angleHistogram).
- **Typed error hierarchy** grew `Node6Error` base +
  `Node5ResultInputError`, `CharactersInputError` (with
  `AngleOrderUnconfirmedError` subclass), `ReferenceSheetFormatError`,
  `ReferenceSheetSliceError`, `AngleMatchingError`. `QueueLookupError`
  is reused from Node 5's module with identical semantics. All under
  the shared `PipelineError` root.
- **CLI:** `run_node6.py --node5-result <n5> --queue <q>
  --characters <c> [--lineart-method dog] [--quiet]` at repo root;
  `python -m pipeline.cli_node6` equivalent on standard Python. Exit
  codes `0` success, `1` `Node6Error`, `2` unexpected.
- **No new Python dependencies.** numpy/scipy/pillow are already on
  RunPod + CI from Nodes 4/5.
- **Frontend change:** `frontend/characters.js` now defaults
  `conventions.angleOrderConfirmed: true` (canonical order locked
  2026-04-23). Libraries saved before this commit with `false` still
  trip Node 6's gate until the operator flips the flag.
- **Numerical tuning is a code-time concern**, not a contract one:
  weight choice for the combined-score, DoG sigmas, normalization
  canvas size. Defaults will be picked + tuned against the Bhim smoke
  fixture during implementation.

## Node 7 — locked decisions (do not re-litigate)

Resolved on 2026-04-23 (design-lock commit; Node 7 code still to ship):

1. **Separate pose from identity.** The rough animatic's action pose
   rarely matches any of the 8 static angles on the model sheet
   (character throwing a punch, running, jumping). Treating
   rough-as-lineart and reference-as-lineart pushes two conflicting
   drawings into the same CN channel. Instead: extract a skeleton from
   the rough → feed a pose-aware ControlNet; feed Node 6's **color**
   reference crop to IP-Adapter for identity. The generator draws the
   reference character *in the rough's pose*. This decomposition is the
   fundamental Node-7 insight and is non-negotiable.
2. **Pose extraction is PER-CHARACTER, routed via
   `characters.json.poseExtractor`** (new
   `Literal["dwpose", "lineart-fallback"]` field, default `"dwpose"`).
   - `dwpose` — humans. DWPose preprocessor (2023+, handles stylized
     cartoon proportions better than classical OpenPose) → skeleton →
     DWPose ControlNet at strength 0.75.
   - `lineart-fallback` — non-humans (quadrupeds, Jaggu the monkey).
     DWPose cannot reliably extract a skeleton for non-biped cartoon
     characters. These route through LineArt + Scribble CN at 0.6 each
     from the rough crop. Every character still has a model sheet (user
     confirmed 2026-04-23) so identity conditioning is unchanged.
3. **IP-Adapter is fed Node 6's COLOR reference crop**, not the DoG
   line-art crop. IP-Adapter-Plus's identity embedding expects a
   textured/colored image. Strength 0.8. The DoG line-art crop is
   available for Reference-Only CN as a secondary tiebreaker at lower
   priority, but it is not the primary identity channel.
4. **txt2img, not img2img.** Rough animatics have messy scribbles, stray
   marks, timing annotations. img2img would bleed all of that into the
   output. Pose CN + IP-Adapter + txt2img gives the generator the pose
   skeleton and the identity without the rough's pixel noise.
5. **Per-character generation, NOT whole-frame inpaint.** Each detection
   (per key pose per character) is generated on its own 512×512 canvas
   with its own IP-Adapter reference; Node 8 composites. This keeps
   identity clean when multiple characters share a frame.
6. **Base model: SD 1.5 + AnyLoRA line-art checkpoint + optional BnW
   LoRA.** SDXL adds VRAM pressure for zero line-art quality win at
   512×512. Exact checkpoint + LoRA versions pinned in
   `custom_nodes/node_07_pose_refiner/models.json`.
7. **Locked sampler defaults across a shot:** DPM++ 2M Karras, 25 steps,
   CFG 7.0, 512×512 canvas. Seed logged per `(shotId, keyPoseIndex,
   identity)` so a failed retry can be re-run deterministically. A
   shot's multiple key poses share a seed base for visual coherence.
8. **RunPod-only node.** Local dev does not have the VRAM headroom for
   SD 1.5 + ControlNet + IP-Adapter + DWPose preprocessors on the
   user's laptop. All Node 7 development + testing happens against a
   RunPod pod. Local pytest / CI only exercises the adapter glue +
   manifest I/O, not the generation itself.
9. **Deployment breaks the `pipeline/node*.py` template on purpose.**
   Nodes 2-6 live in `pipeline/` because they're pure-Python,
   GPU-agnostic. Node 7 is **ComfyUI workflow JSON + thin custom-node
   wrapper**: `custom_nodes/node_07_pose_refiner/workflow.json` is the
   authoritative graph. No `pipeline/node7.py`. The custom-node wrapper
   marshals inputs/outputs and logs metadata.
10. **Model management via BOTH ComfyUI-Manager AND explicit pins.**
    Manager is the dev-convenience path for local workflow authoring
    (click-install). RunPod production uses `curl <url> && sha256sum
    --check` in `runpod_setup.sh` + `models.json` declaring
    `(name, url, sha256, size_mb, destination)` for every weight.
    Dual-path avoids silent drift when Manager's version and the
    pinned version diverge.
11. **No QC gate in v1 — metadata logging only.** The original 7F
    sketched an automatic identity-drift / double-lines regenerate
    loop. v1 logs seed + reference crop paths + CN strengths per
    generation to `node7_result.json` and leaves QC as a future Node
    11C retry hook. Early output failures are more useful as training
    signal than as silent regenerations.
12. **Transparent-background PNG output.** Each generated character
    lands on a transparent 512×512 canvas (alpha from the generated
    silhouette + luminance threshold → BnW). Node 8 composites these
    onto the final frame canvas.

Consequences locked in:
- **Schema change (additive):** `CharacterSpec` gains
  `poseExtractor: Literal["dwpose", "lineart-fallback"] = "dwpose"`.
  Default `"dwpose"` means `characters.json` files saved before this
  field existed still load cleanly. `queue.json`'s per-character
  dicts gain a `poseExtractor` field alongside `identity` /
  `sheetPath` / `position` so Node 7 reads one file. Node 2
  propagates it from `char_by_name` in `shot_chars`.
- **queue.json `schemaVersion` stays at 1.** The change is purely
  additive — Nodes 3/4/5/6 don't read per-character pose data, and
  their schema guards check the version not field exhaustion, so
  they continue to pass. No cascading schema bumps.
- **Frontend change:** `frontend/characters.html` grows a 2-option
  `poseExtractor` dropdown (`dwpose` default). `characters.js`
  persists it to `localStorage` per character, emits it in the
  downloaded `characters.json`, and defaults old localStorage
  entries predating this field to `"dwpose"`.
- **Test coverage (Node 2, not Node 7):** `tests/test_node2.py`
  grows four cases — default-to-dwpose when field absent, explicit
  `lineart-fallback` propagates through `queue.json`, poseExtractor
  present in the serialized queue, and schema rejection of invalid
  values (e.g. `"openpose"`).
- **No new `pipeline/requirements.txt` deps.** Schema change is a
  pure Literal type. Node 7's actual runtime deps (torch, ComfyUI
  custom nodes, DWPose weights) install on the RunPod pod via
  `runpod_setup.sh` + `models.json`, not via pipeline requirements.
- **Typed error hierarchy (to be added when Node 7 code ships):**
  `Node7Error` base + `Node6ResultInputError`,
  `RefinementGenerationError` subclasses. `QueueLookupError` reused
  from Node 5 with identical semantics.
- **CLI (to be added when Node 7 code ships):**
  `run_node7.py --node6-result <n6> --queue <q>`. Exit codes `0`
  success (per-generation skips still exit 0), `1` `Node7Error`,
  `2` unexpected.

## Locked conventions (do not re-litigate)

- **25 FPS** is fixed. No variable frame rate anywhere.
- **Output color** is Black & White line art. No color pass in Part 1.
- **Held frames are NEVER regenerated** by the AI — only duplicated at Node 9C
  to preserve the original rough-animatic timing exactly. This is core to the
  design and is why Node 4 splits key poses from held frames.
- **Character model sheet = 8-angle horizontal strip**, transparent/black bg,
  full color. Node 6 must auto-slice via alpha-island bbox, use **8-bin** angle
  matching (not 4), and run line-art extraction on the selected crop before
  IP-Adapter/Reference-Only conditioning so the reference is BnW-aligned.
- **Canonical angle order** (left→right on the sheet): back, back-¾-L, profile-L,
  front-¾-L, front, front-¾-R, profile-R, back-¾-R. Confirmed by the user
  on 2026-04-23 against the Bhim reference template. L/R refers to the
  character's own anatomical left/right (not the viewer's). The ASCII form
  `back-3q-L` is the canonical JSON/Python identifier (matches
  `frontend/characters.js`); the `¾` form in prose is purely readability.
- **Position codes:** L / CL / C (exact center) / CR / R.

## Environment gotchas

- **GitHub CLI path:** `C:\Users\Omkar Hajare\AppData\Local\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin\gh.exe`.
  New PowerShell windows find `gh` on PATH; older sessions need the full path.
- **Git identity** is set **locally in this repo only** (not globally) —
  `Omkar Hajare <189162401+Omkar8369@users.noreply.github.com>`. Do not change
  the global config.
- **GitHub user:** `Omkar8369` (id `189162401`). Repo is **public** at
  https://github.com/Omkar8369/animatic-refinement.
- **Python (no system install, embedded only).** No system-wide Python on this
  machine — the `python.exe` under `...\WindowsApps\` is a Microsoft-Store
  alias stub and fails with `"Python was not found"` when run. The only real
  Python is embedded in ComfyUI portable:
  ```
  C:\Users\Omkar Hajare\Desktop\download\ComfyUI_windows_portable\python_embeded\python.exe
  ```
  Version 3.13.12, with `torch 2.9.1+cu128` installed (matches driver CUDA
  12.8 — do NOT upgrade to cu130 wheels; driver 573.05 can't handle them and
  ComfyUI segfaults on boot). Always invoke the full path; never rely on
  `python` / `python3` on PATH.
- **ComfyUI local install** (sibling folder, outside the repo — intentional,
  so 6 GB of Windows binaries don't pollute git history):
  `C:\Users\Omkar Hajare\Desktop\download\ComfyUI_windows_portable\`.
  Launch from that dir with:
  `./python_embeded/python.exe -s ComfyUI/main.py --windows-standalone-build`
  GUI at `http://127.0.0.1:8188/`. ComfyUI-Manager is pre-installed in
  `ComfyUI/custom_nodes/ComfyUI-Manager`. Used for local workflow authoring
  + testing before deploying the same workflow to RunPod.
- **Shell:** user's primary shell is Git Bash (Unix-style); PowerShell tool is
  also available for Windows-specific operations.

## How to pick up where we left off

1. Read this file (you just did).
2. Read `docs/PLAN.md` for the full 11-node spec.
3. Check `git log --oneline` to see the last committed node.
4. **Inherited-drift check.** Run the drift-grep commands from the ship
   checklist against the current `main`. If the previous node left any of
   the six canonical files stale (happened with both Node 1 AND Node 2),
   reconcile them FIRST as a separate `chore:` commit, BEFORE writing any
   new code. Inheriting stale state is how the drift pattern started;
   breaking the chain requires catching it at pickup, not at the next ship.
5. If the last locked-decisions section corresponds to a node still marked
   NEXT in the status table (design locked but code not yet shipped), resume
   that node's implementation per the locked decisions. If every
   locked-decisions section is for a DONE node, confirm with the user which
   node to start next and walk through its open design questions.
6. **Never** write code for a node whose design discussion hasn't been resolved
   with the user.

## References

- Approved plan: `docs/PLAN.md`
- Editable working spec: `docs/Node_Plan.xlsx`
- On-disk auto-memory: `C:\Users\Omkar Hajare\.claude\projects\C--Users-Omkar-Hajare-Desktop-download\memory\`
