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
| 7    | AI-Powered Pose Refinement             | **DONE — both routes live-verified on RunPod (2026-04-25). 47 tests pass (207 repo-wide); CLI + `run_node7.py` wrapper + ComfyUI custom node verified in dry-run on embedded Python; two workflow templates (`workflow.json` dwpose + `workflow_lineart_fallback.json`) + `models.json` weight pins shipped; `runpod_setup.sh` extended with custom-node clone + weight curl + sha256 verify. First live run (lineart-fallback both chars): 2 gen / 0 skip / 0 err, 36s. DWPose verification (Bhim flipped to dwpose, Jaggu still lineart-fallback in same Node 7 invocation): 2 gen / 0 skip / 0 err, 41s; per-character routing table works; Bhim's PNG bytes differ from baseline (sha `d77d9b18…` vs `038f69e6…`) confirming DWPose contributes pose info; Jaggu's bytes are bit-identical (sha `5aa3c619…`) confirming the deterministic-seed contract holds for unchanged routes. Bringup on the runpod-slim pod image (controlnet_aux symlink + `extra_model_paths.yaml` + `IPAdapter.weight_type` + DWPose-specific venv deps `matplotlib` `scikit-image` `onnxruntime`) captured in `tools/POD_NOTES_runpod_slim.md`.** |
| 8    | Scene Assembly                         | **DONE — 51 tests pass (258 repo-wide); CLI + `run_node8.py` wrapper + ComfyUI custom node verified on embedded Python; pure-Python compositing (PIL+numpy), no GPU; bbox-anchored feet-pinned scaling, white background, z-order by bbox.bottomY, BnW threshold; substitute-rough fallback (warn-and-reconcile) when Node 7 marked a generation as errored or empty; rerun wipes `<shotId>/composed/` first** |
| 9    | Timing Reconstruction                  | **DONE — 42 tests pass (300 repo-wide); CLI + `run_node9.py` wrapper + ComfyUI custom node verified on embedded Python; pure-Python translate-and-copy (PIL + numpy), no GPU; whole-frame translation on a fresh white canvas, anchor frames are bit-identical copies of Node 8's composite, held frames are pasted at `(dx, dy)` offset from `keypose_map.json`; off-canvas translates are NOT errors (mathematically valid for end-of-slide shots); fail-loud on missing composed PNG or totalFrames mismatch; rerun wipes `<shotId>/timed/` first; chases `keypose_map.json` from shot root via `--node8-result` only** |
| 10   | Output Generation (PNG → MP4)          | **DONE — 42 tests pass (342 repo-wide); CLI + `run_node10.py` wrapper + ComfyUI custom node verified on embedded Python; pure-Python (subprocess + imageio_ffmpeg + json), no GPU; ffmpeg via imageio-ffmpeg static binary, codec H.264 (libx264) + yuv420p + CRF 18 + preset medium + 25 FPS hardcoded; output to `<work-dir>/output/<shotId>_refined.mp4`; ffprobe-style verification via `imageio_ffmpeg.count_frames_and_secs` (frame count + duration tolerance); odd canvas dims fail-loud (libx264 requires even W/H); does NOT delete upstream artifacts** |
| 11   | Batch Management                       | **DONE — live-verified end-to-end on RunPod (2026-04-25). 46 tests pass (388 repo-wide); CLI + `run_node11.py` wrapper + ComfyUI custom node verified on embedded Python; pure-Python orchestrator (subprocess + json + datetime), no GPU. Subprocess-invokes `run_nodeN.py` for N in 2..10 with per-node retry policy + JSONL progress log + final aggregate report. Pre-Node-7 best-effort `nvidia-smi` (warn but proceed); partial-success semantic = exit 0 with `failedShots > 0`; 100% failure = `BatchAllFailedError`. **First live RunPod run (4090):** 1 shot / 1 succeeded / 0 failed, 33.8s total — Node 2 + 3 + 4 + 5 + 6 + 8 + 9 + 10 each finished in <1s; Node 7 (real SD generation via `lineart-fallback`) dominated at 30.4s; orchestration overhead ~2s. Bringup gap caught: system python3 needs `pipeline/requirements.txt` installed (the runpod-slim venv has Comfy's deps but the system Python is what `run_nodeN.py` subprocesses use). Captured in `tools/POD_NOTES_runpod_slim.md`.** |

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
13. **Runtime topology — ComfyUI runs ON the RunPod pod, not on the
    laptop.** The laptop's role for Node 7 is zero GPU compute:
    1. Laptop runs Nodes 2-6 via `run_node{2..6}.py` (pure CPU).
    2. User pushes the repo and syncs the `<work-dir>` (which holds
       `queue.json`, per-shot frames, keyposes, `character_map.json`,
       `reference_map.json`, `node6_result.json`, reference crops) up
       to the RunPod pod.
    3. On the pod: `bash runpod_setup.sh` symlinks
       `custom_nodes/node_07_pose_refiner/` into ComfyUI's
       `custom_nodes/` and downloads the pinned weights declared in
       `models.json` via curl + sha256 check.
    4. ComfyUI boots on the pod (port 8188). Either the operator opens
       the web UI and loads `workflow.json` manually, OR
       `python run_node7.py --node6-result <path> --queue <path>` runs
       **on the pod** and POSTs `workflow.json` to ComfyUI's HTTP API.
    5. All GPU generation stays on the pod; outputs land in
       `<work-dir>/<shotId>/refined/` on the pod's disk and are synced
       back to the laptop only for inspection (Node 8+ also run on
       the pod). Local ComfyUI (the `ComfyUI_windows_portable` install
       sibling to this repo) is reserved for authoring and smoke-
       testing the Nodes 3-6 ComfyUI wrappers; it does **not** have
       the VRAM for Node 7's SD 1.5 + ControlNet + IP-Adapter + DWPose
       stack and Node 7 must never be executed there.

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
- **Typed error hierarchy (shipped):** `Node7Error` base +
  `Node6ResultInputError`, `RefinementGenerationError`,
  `WorkflowTemplateError`, `ComfyUIError`. `QueueLookupError` reused
  from Node 5 with identical semantics. All under the shared
  `PipelineError` root.
- **CLI (shipped):** `run_node7.py --node6-result <n6> --queue <q>
  [--comfyui-url http://127.0.0.1:8188] [--per-prompt-timeout 600]
  [--dry-run] [--quiet]`. Exit codes `0` success (per-generation
  errors still exit 0 — they're recorded in `refined_map.json`), `1`
  `(Node7Error, QueueLookupError)` (whole-run failure: bad manifest,
  bad template, ComfyUI unreachable), `2` unexpected bug.
- **Shipped file layout (diverges from Nodes 2–6 on purpose — decision
  #9: no `pipeline/node7.py`):**
  - `pipeline/cli_node7.py` — CLI thin wrapper (argparse → orchestrate)
  - `run_node7.py` — repo-root wrapper (sys.path fixup for embedded Py)
  - `custom_nodes/node_07_pose_refiner/__init__.py` — ComfyUI custom
    node `AnimaticNode7PoseRefiner` (category `animatic-refinement`,
    returns a `node7_result_json` STRING)
  - `custom_nodes/node_07_pose_refiner/manifest.py` — pure-Python
    manifest I/O (importable from CLI + tests + ComfyUI)
  - `custom_nodes/node_07_pose_refiner/comfyui_client.py` — stdlib
    urllib client for `/prompt`, `/history/{id}`, `/view` (no new deps)
  - `custom_nodes/node_07_pose_refiner/orchestrate.py` — top-level
    driver: routing table, workflow parameterization, submit + poll
    + download
  - `custom_nodes/node_07_pose_refiner/workflow.json` — DWPose graph
    (humans)
  - `custom_nodes/node_07_pose_refiner/workflow_lineart_fallback.json`
    — LineArt + Scribble graph (non-humans)
  - `custom_nodes/node_07_pose_refiner/models.json` — weight pins
    (schemaVersion 1; 9 models + 2 external custom-node repos)
  - `runpod_setup.sh` extended with custom-node clone + curl-download
    + sha256 verify loops keyed off `models.json`
  - `tests/test_node7.py` — 47 tests (manifest I/O, orchestrator
    dry-run end-to-end, workflow template loader, parameterization,
    CLI exit codes, error-hierarchy invariants)
- **Contractual ComfyUI node IDs in both workflow JSONs** (fixed so
  `orchestrate.py` can parameterize them by ID):
  `"3"`=KSampler, `"6"`=positive CLIPTextEncode, `"7"`=negative
  CLIPTextEncode, `"11"`=LoadImage (rough key pose), `"12"`=LoadImage
  (reference color crop), `"20"`=SaveImage. Re-exporting the graph
  must preserve these IDs OR update the `NODE_*` constants in
  `orchestrate.py` in the same commit.
- **Deterministic seed:** `SHA256(f"{project}|{shotId}|{keyPoseIndex}|
  {identity}")` masked to 31 bits. Re-running Node 7 produces the
  same generation for the same detection, so partial-failure retries
  are reproducible.

## Node 7 — live-run addendum (learned 2026-04-25 on runpod-slim pod)

First live Node 7 run (2 PNGs / 0 errors / 36s on the 2-character
synthetic smoke fixture, lineart-fallback route) plus the DWPose-route
verification later the same day (2 PNGs / 0 errors / 41s, Bhim flipped
to dwpose) shook out FOUR issues worth capturing permanently so the
next pod bringup isn't a re-debug:

1. **`IPAdapter.weight_type` is required** by the
   `comfyui_ipadapter_plus` fork on the runpod-slim image. Both
   `workflow.json` and `workflow_lineart_fallback.json` now set node
   `"51".inputs.weight_type = "standard"` (the identity-conditioning
   default — the other options are `"prompt is more important"` and
   `"style transfer"`, neither of which we want for identity
   preservation). This is a real forward-compatibility fix, not a
   runpod-slim quirk; any pod running a current build of the fork will
   demand the field. If we ever re-export the workflows from the UI,
   preserve this input.

2. **The runpod-slim pod image runs ComfyUI from
   `/workspace/runpod-slim/ComfyUI/`, not `/workspace/ComfyUI/`.** The
   old path still exists on the persistent volume and holds all the
   model weights + historical custom-nodes (including
   `comfyui_controlnet_aux`), but runpod-slim ComfyUI can't see any of
   it. `runpod_setup.sh`'s default assumption (`COMFY_DIR=/workspace/ComfyUI`)
   is therefore wrong on this pod. Concrete bringup fix is documented
   in `tools/POD_NOTES_runpod_slim.md` — symlink controlnet_aux into
   runpod-slim's `custom_nodes/`, install its Python deps into the
   venv at `/workspace/runpod-slim/ComfyUI/.venv-cu128/bin/python` (not
   system `/usr/bin/python3`), and drop an `extra_model_paths.yaml` at
   runpod-slim's root pointing checkpoints/controlnets/loras/ipadapter
   at `/workspace/ComfyUI/models/`. Future work: optionally teach
   `runpod_setup.sh` a `COMFY_EXTRA_MODEL_BASE` env knob so a plain pod
   stays plain.

3. **`/start.sh` does NOT auto-restart ComfyUI on crash.** It runs
   `python main.py &; wait $COMFY_PID || true; sleep infinity` — if the
   ComfyUI child dies, the parent bash just hangs in `sleep infinity`
   forever. In-session restarts need `setsid nohup "$VENV_PY" main.py
   ... </dev/null >LOG 2>&1 & ; disown`. The RunPod dashboard's
   "Restart Pod" is the only path that re-runs `/start.sh` from the
   top and is the safest thing to tell the operator.

4. **The DWPose route needs three Python deps that are NOT installed
   by `comfyui_controlnet_aux/requirements.txt` alone:** `matplotlib`,
   `scikit-image`, and `onnxruntime`. Without them, ComfyUI silently
   skips registering `DWPreprocessor` — the only visible symptom is the
   class count drops from 984 to 977 in `/object_info`, and
   `comfyui.log` shows `Failed to import module dwpose because
   ModuleNotFoundError: No module named 'matplotlib'` at startup. The
   `lineart-fallback` route is unaffected (only needs the basic
   controlnet_aux deps), which is why the first 2026-04-25 live run
   passed without these but the DWPose verification couldn't until they
   were installed into `/workspace/runpod-slim/ComfyUI/.venv-cu128`.
   `tools/POD_NOTES_runpod_slim.md` step 2 has been amended to install
   them explicitly. Restart ComfyUI after install so it re-imports
   custom_nodes.

Diagnostic + fix scripts `tools/pod_fix_controlnet_aux.sh` and
`tools/pod_diagnose_preprocessors.sh` stay valid for other pod layouts
(they auto-detect the running Python via `/proc` and query
`/object_info` rather than trusting `lsof`), but on the runpod-slim pod
the symlink + YAML bridge from `tools/POD_NOTES_runpod_slim.md` is what
actually gets to 0 errors.

## Node 8 — locked decisions (do not re-litigate)

Resolved on 2026-04-25 (design-locked + shipped same day):

1. **The bbox is the single source of truth for character placement.**
   Node 5 wrote each character's bbox in original-frame coordinates;
   Node 7 cropped the rough using that bbox; Node 8 places the refined
   character back using the same bbox. Symmetric — what came out
   (size + position) is what goes back in. No new positioning logic, no
   inferring positions from other signals. This is the fundamental
   Node-8 invariant and is non-negotiable.

2. **Feet-pinned scaling, NOT stretch-to-fit.** The 512×512 refined
   PNG isn't fully filled by the character — SD typically leaves white
   margin around the silhouette. Stretching the 512×512 into the bbox
   would float the feet inside the bbox instead of anchoring them at
   the bbox bottom. Algorithm: find the lowest non-white pixel in the
   512×512 (= refined character's feet), scale the refined PNG by
   `bbox.height / character_height_in_512`, paste it onto the canvas
   centered on `(bbox.centerX, bbox.bottomY)` so the feet land at the
   bbox bottom. Standard 2D-cel anchoring; works for
   standing/walking/jumping/running shots alike since Node 5's bbox
   already moves with the character.

3. **Output canvas resolution = source MP4 resolution exactly.**
   Whatever Node 3 decoded into. Reasons: (a) Node 9 will do
   translate-and-copy of held frames at the same dims, mismatched res
   would force a per-frame resize; (b) any project-level normalization
   can be applied later without contract change. Get original dims by
   probing one of Node 3's `frame_*.png` files per shot (~1 ms; cheaper
   than adding `frameWidth`/`frameHeight` to `node3_result.json` and
   bumping its consumer count).

4. **Background = solid white.** Part 1's deliverable is BnW line art
   on white. Black would invert polarity; transparent defers a decision
   Node 10's encoder will have to make anyway. Compositing onto white
   directly keeps Node 8's output ready for Node 9 (translate-and-copy)
   and Node 10 (PNG → MP4) without further format changes.

5. **Z-order = bbox-bottom-y descending** (lower-on-screen drawn last
   = "closer to camera"). Standard 2D-cel convention; matches what
   storyboard artists usually intend. Future override path (e.g. a
   metadata `z` field) is non-blocking — add when first real shot
   needs it.

6. **Line-weight unification = threshold to BnW only, no dilate/erode
   normalize in v1.** Each Node 7 generation is independent and can
   have slightly different line weights. Cheapest path is per-character
   luminance-threshold to pure BnW, which removes color/grey artifacts
   but does not try to normalize stroke widths across characters. Full
   stroke-width unification (a dilate/erode pass driven by a
   target-width parameter) is a future tuning pass against real client
   shots; premature now.

7. **Substitute-rough on Node 7 failure, NOT fail-loud.** When
   `refined_map.json` shows a generation with `status="error"` (or the
   refined PNG exists but is empty/transparent), Node 8 substitutes the
   rough key-pose frame at the same bbox location and appends a
   structured warning to `composed_map.json`. CLI still exits 0 — same
   warn-and-reconcile pattern as Node 5. Rationale: keeps timing intact
   for Node 9 (no holes in the keypose sequence), gives the operator a
   clear list of which key poses need re-generation, and means future
   Node 11 retry logic can be additive without changing Node 8's
   contract.

8. **Architecture template = same as Nodes 3-6** (Option C thin ComfyUI
   wrapper). All logic in `pipeline/node8.py`;
   `custom_nodes/node_08_scene_assembler/__init__.py` only declares
   `INPUT_TYPES` / `RETURN_TYPES` and calls into the core. Pure-Python
   compositing (PIL + numpy), GPU-agnostic. No reason to break the
   template (unlike Node 7's HTTP-driven case). Same code runs from
   CLI, tests, CI, and ComfyUI.

9. **Single-threaded.** Same as Nodes 3-6. Per-shot composite is
   ~10-30 ms per key pose at 1280×720. Parallelism is Node 11's concern.

10. **Rerun safety:** each `<shotId>/composed/` directory is wiped of
    stale `*_composite.png` before each run so `composed_map.json`
    always matches the directory exactly. Mirrors Nodes 3/4/5/6's
    wipe-before-write pattern.

Consequences locked in:
- **Inputs (one required CLI path):**
  - `--node7-result <path>` (Node 7's aggregate manifest).
  - Node 8 chases pointers from there: `node7_result.json` → per-shot
    `refined_map.json` (refined PNG paths + status), then via the same
    shot-root directory: `character_map.json` (Node 5 bboxes),
    `keypose_map.json` (Node 4 keypose list), and one
    `frames/frame_NNNN.png` per shot for original dims.
  - **No `--queue` needed** — Node 8 doesn't care about character
    routing or sheet paths, only positions.
- **Outputs:**
  - `<shotId>/composed/<keyPoseIndex>_composite.png` — RGB,
    source-MP4 resolution, white background, all characters composited
    in z-order.
  - `<shotId>/composed_map.json` — per-shot list of
    `{keyPoseIndex, sourceFrame, composedPath, characters: [{identity,
    bbox, status, substitutedFromRough}], warnings[]}`.
  - Aggregate `<work-dir>/node8_result.json` — one
    `ShotComposeSummary` per shot (shotId, keyPoseCount, composedCount,
    substituteCount, composedMapPath). Node 9 reads `composed_map.json`
    directly.
- **Typed error hierarchy** grew `Node8Error` base +
  `Node7ResultInputError` (malformed / missing / stale
  `node7_result.json`) + `RefinedPngError` (decode / empty refined PNG
  AND we cannot even substitute-rough because the source frame is also
  missing) + `CompositingError` (PIL/numpy crash during composite). All
  under the shared `PipelineError` root.
- **CLI:** `run_node8.py --node7-result <path> [--background white]
  [--quiet]` at repo root; `python -m pipeline.cli_node8` equivalent on
  standard Python. Exit codes `0` success (substitute-rough warnings
  still 0), `1` `Node8Error` subclass, `2` unexpected.
- **No new Python dependencies.** PIL + numpy already on RunPod + CI
  from Nodes 4/5/6.
- **Output is RGB (no alpha).** White background is opaque; Node 9's
  translate-and-copy works fine without alpha; Node 10's PNG → MP4
  doesn't want alpha anyway.

## Node 9 — locked decisions (do not re-litigate)

Resolved on 2026-04-25 (design-locked + shipped same day):

1. **Translate-and-copy on a fresh white canvas — no AI on held
   frames.** For every frame in the original timeline: if it's a
   key pose's anchor frame, copy Node 8's composite as-is; if it's a
   held frame, paste Node 8's composite onto a fresh white canvas at
   offset `(dx, dy)` from `keypose_map.json`. PIL's standard paste
   auto-clips at boundaries; uncovered regions stay white. Zero
   regeneration on held frames is the whole reason Node 4 went
   translation-aware in the first place; this property is
   non-negotiable.

2. **The bbox / per-character placement is already baked into Node
   8's composite.** Node 9 operates ONE LAYER UP from per-character
   bboxes — it translates the WHOLE composited frame as a single
   image, not each character independently. This matches Node 4's
   contract: the `(dy, dx)` offset is whole-frame, computed from
   phase correlation against the rough's anchor frame, NOT
   per-character. If a future shot needs per-character translation
   (e.g., one character slides while another stays still in the same
   key pose group), Node 4's classifier would have split them into
   separate key poses already -- by construction we never see that
   case here.

3. **Output canvas resolution = Node 8 composite resolution = source
   MP4 resolution.** Node 9 does pure translation, no resampling.
   Probed implicitly from each loaded composite PNG.

4. **Exposed-region fill = solid white.** Matches Node 8's
   white-background contract and Part 1's BnW-on-white deliverable.
   Black or transparent fill would either invert polarity or push a
   format decision into Node 10's encoder.

5. **Output frame numbering = 1-indexed, 4-digit zero-padded
   `frame_NNNN.png`.** Same convention as Node 3's frame extraction;
   Node 10 will glob `<shot>/timed/frame_*.png` directly.

6. **Inputs (one required CLI path):** `--node8-result <path>`. Node
   9 chases pointers from there: `node8_result.json` → per-shot
   `composed_map.json` → shot root → `keypose_map.json` (Node 4's
   timing data, sibling to composed_map.json). No second `--node4-`
   flag needed; the implicit chase keeps the CLI surface tight and
   avoids requiring the operator to remember two manifest paths
   when one points at the other.

7. **Fail-loud on missing composed PNG, NOT substitute-rough.**
   Held frames REQUIRE the anchor's composed PNG; without it, every
   held frame in that key pose's group is unreconstructable.
   Different from Node 8's substitute-rough policy, which had a
   meaningful fallback (the rough key-pose pixels). Node 9 has no
   such fallback — the upstream rough has the same content as the
   composite at the anchor frame, but it's not refined, and silently
   substituting would silently downgrade the output. Better to raise
   `TimingReconstructionError` with a clear "Node 8 didn't produce
   composed PNG for keyPoseIndex N in shot X — re-run Node 8" so the
   operator can fix and rerun.

8. **Total-frame-count mismatch is a hard error**
   (`FrameCountMismatchError`). Every frame in `keypose_map.totalFrames`
   must belong to exactly one key pose's anchor or heldFrames list
   (Node 4 invariant); if our reconstructed PNG count disagrees,
   something upstream broke. No warn-and-reconcile here.

9. **Translation offsets larger than canvas are NOT errors.** If
   the held frame's `(dy, dx)` pushes the character entirely
   off-screen, the resulting PNG is mostly-white. Mathematically
   valid; happens for end-of-slide shots where the character slid
   off-screen. Original rough also showed white pixels at that
   point, so timing is preserved. No warning either — visual
   inspection catches these.

10. **Same-frame-in-multiple-keyposes is a hard error**
    (`KeyPoseMapInputError`). Node 4 invariant violation; refuse to
    proceed rather than silently overwriting one with another.

11. **Architecture template = same as Nodes 3-6** (Option C thin
    ComfyUI wrapper). All logic in `pipeline/node9.py`;
    `custom_nodes/node_09_timing_reconstructor/__init__.py` only
    declares `INPUT_TYPES` / `RETURN_TYPES` and calls into the core.
    Pure-Python (PIL + numpy), GPU-agnostic.

12. **Single-threaded.** Per-frame translate is ~1 ms at typical
    1280×720; per-shot total is sub-second even for long shots.
    Parallelism is Node 11's concern.

13. **Rerun safety:** each `<shotId>/timed/` directory is wiped of
    stale `frame_*.png` before each run so `timed_map.json` always
    matches the directory exactly. Mirrors Nodes 3/4/5/6/8's
    wipe-before-write pattern.

Consequences locked in:
- **Inputs (one required CLI path):**
  - `--node8-result <path>` (Node 8's aggregate manifest).
  - Node 9 chases pointers: `node8_result.json` → per-shot
    `composed_map.json` → `<shot_root>/keypose_map.json` (Node 4).
- **Outputs:**
  - `<shotId>/timed/frame_NNNN.png` — RGB, source MP4 resolution,
    white background, one PNG per frame of the original shot.
  - `<shotId>/timed_map.json` — per-shot list of
    `{frameIndex, sourceKeyPoseIndex, offset: [dy, dx],
    composedSourcePath, timedPath, isAnchor}`.
  - Aggregate `<work-dir>/node9_result.json` — one
    `ShotTimingSummary` per shot (shotId, totalFrames,
    keyPoseCount, anchorCount, heldCount, timedMapPath). Node 10
    reads `timed_map.json` for the encode order.
- **Typed error hierarchy** grew `Node9Error` base +
  `Node8ResultInputError` (malformed/missing node8_result.json or
  composed_map.json) + `KeyPoseMapInputError`
  (malformed/missing keypose_map.json, or its data violates Node 4
  invariants like duplicate frame indices) +
  `TimingReconstructionError` (missing composed PNG, can't
  translate-and-copy) + `FrameCountMismatchError` (totalFrames
  disagrees with reconstructed count). All under the shared
  `PipelineError` root.
- **CLI:** `run_node9.py --node8-result <path> [--quiet]` at repo
  root; `python -m pipeline.cli_node9` equivalent on standard
  Python. Exit codes `0` success, `1` `Node9Error` subclass, `2`
  unexpected. No `--background` flag — white is hardcoded.
- **No new Python dependencies.** PIL + numpy already on RunPod +
  CI from Nodes 4/5/6/8.
- **Output is RGB (no alpha).** Same shape as Node 8's composites
  by design; Node 10's PNG → MP4 encoder doesn't want alpha.

## Node 10 — locked decisions (do not re-litigate)

Resolved on 2026-04-25 (design-locked + shipped same day):

1. **ffmpeg via `imageio-ffmpeg` static binary, NOT system ffmpeg.**
   Same wheel Node 3 uses for decode; the `pipeline/requirements.txt`
   line is already there. No new dep, identical behavior on Windows
   embedded Python + RunPod Linux + CI.

2. **Codec = H.264 (libx264).** Maximum playback compatibility.
   ToonCrafter (Part 2) and every reasonable downstream consumer
   reads it. H.265 would shave file size at the cost of broken
   playback in older tools — not worth the trade for a deliverable
   pipeline.

3. **Pixel format = yuv420p.** Maximum compatibility. yuv444 saves
   a tiny bit of quality on BnW line art but breaks playback in
   QuickTime / hardware decoders. Not worth it.

4. **Quality = CRF 18.** Visually lossless for most content. BnW
   line art compresses extremely well (large white regions kill
   bitrate naturally), so file size stays small even at CRF 18.
   CRF is exposed via `--crf` so an operator can tune for an
   unusually tight file-size budget without forking the contract.

5. **Preset = `medium`** (libx264 default). Balanced speed vs file
   size; faster presets bloat file size, slower presets shave
   bytes for time we don't care about at this stage.

6. **Frame rate = 25** (locked project convention; already in this
   file's "Locked conventions" section). No `--fps` flag — silently
   accepting a different rate would corrupt the timing Node 9
   carefully reconstructed.

7. **Output location = `<work-dir>/output/<shotId>_refined.mp4`.**
   Project-level `output/` directory collects every shot's
   deliverable in one place — easy to zip + ship to client.
   Per-shot location (`<shot>/refined.mp4`) was the alternative
   but scatters deliverables across the work dir.

8. **Filename pattern = `<shotId>_refined.mp4`** (e.g.
   `shot_001_refined.mp4`). Matches what PLAN.md 10C originally
   sketched. Underscore + descriptive suffix keeps the shotId
   greppable and the file kind unambiguous.

9. **Post-encode verification via ffprobe.** Check: file exists +
   size > 0; codec is `h264`; fps is `25`; nb_frames matches the
   count of input PNGs Node 9 produced (within ±1 for encoder
   rounding). Catches silent ffmpeg corruption (exit 0 but
   malformed file). Cheap (~50 ms per shot).

10. **Do NOT delete upstream artifacts.** PLAN.md 10E's archive
    decision: `timed/`, `composed/`, `refined/`, etc. all stay on
    disk for debugging and Part 2 (ToonCrafter) which may want
    refined PNGs as additional input. Node 10 is purely additive —
    write the MP4, leave everything else alone.

11. **Odd canvas dimensions are a hard error**
    (`FFmpegEncodeError`). libx264 requires even W and H. If frames
    are odd-dimensioned, the source MP4 was odd-dimensioned (Node 3
    decoded 1:1). Operator should re-encode source rather than have
    us silently auto-pad — auto-padding would shift every character
    by half a pixel and silently desync from Node 9's
    translate-and-copy positions.

12. **ffmpeg non-zero exit → `FFmpegEncodeError` with last 10
    stderr lines.** Mirrors Node 3's pattern. Operator gets enough
    context to fix the input or report a bug without having to
    re-run with verbose logging.

13. **Missing PNG in 1..NNNN gap → `TimedFramesError`.** Node 9's
    invariant: every frame in `1..totalFrames` is on disk. A hole
    means an upstream bug; refuse to encode rather than produce a
    short MP4.

14. **nb_frames mismatch after encode → `FFmpegEncodeError`** with
    "ffmpeg said done but produced N frames, expected M". Catches
    silent encoder dropouts (rare but possible with corrupt PNG
    inputs).

15. **Architecture template = same as Nodes 3-6/8/9** (Option C
    thin ComfyUI wrapper). All logic in `pipeline/node10.py`;
    `custom_nodes/node_10_png_to_mp4/__init__.py` only declares
    `INPUT_TYPES` / `RETURN_TYPES` and calls into the core.
    Pure-Python (subprocess + imageio_ffmpeg + json), GPU-agnostic.

16. **Single-threaded.** Per-shot encode is sub-second to a few
    seconds; ffmpeg itself uses multiple cores per encode anyway.
    Parallelism across shots is Node 11's concern.

17. **Rerun safety: ffmpeg `-y` flag overwrites output MP4.**
    Simpler than wipe-then-encode; ffmpeg handles atomicity. The
    output dir itself is created on first encode; not wiped on
    subsequent runs (since multi-shot batches may add new shots
    incrementally).

Consequences locked in:
- **Inputs (one required CLI path):**
  - `--node9-result <path>` (Node 9's aggregate manifest).
  - Node 10 chases pointers: `node9_result.json` → per-shot
    `timed_map.json` → per-shot `<shot>/timed/` directory. Probes
    one frame for dims (fail-loud if odd).
- **Outputs:**
  - `<work-dir>/output/<shotId>_refined.mp4` — H.264, yuv420p,
    25 FPS, CRF 18 by default.
  - `<work-dir>/node10_result.json` — aggregate one-line summary
    per shot (shotId, outputPath, frameCount, durationSeconds,
    codec, fps, fileSizeBytes).
  - No per-shot manifest beyond the entry in `node10_result.json` —
    Node 10's output is the deliverable itself, not a pointer.
- **Typed error hierarchy** grew `Node10Error` base +
  `Node9ResultInputError` (malformed/missing node9_result.json or
  per-shot timed_map.json) + `TimedFramesError` (missing PNG in
  1..N gap) + `FFmpegEncodeError` (ffmpeg non-zero exit, odd dims,
  ffprobe verification failure). All under the shared
  `PipelineError` root.
- **CLI:** `run_node10.py --node9-result <path> [--crf 18]
  [--quiet]` at repo root; `python -m pipeline.cli_node10`
  equivalent on standard Python. Exit codes `0` success, `1`
  `Node10Error` subclass, `2` unexpected. CRF is the only
  knob — codec/preset/pixel-format are locked.
- **No new Python dependencies.** `imageio-ffmpeg` already on
  RunPod + CI from Node 3.

## Node 11 — locked decisions (do not re-litigate)

Resolved on 2026-04-25 (design-locked + shipped same day):

1. **Subprocess each `run_nodeN.py` and read its exit code, NOT
   in-process import.** Each node already has a stable CLI + exit
   codes; subprocess matches what an operator does by hand → identical
   failure modes → easier to debug. In-process invocation would
   couple Node 11 to every node's import surface, make argparse
   namespace collisions a real risk, and complicate test mocking
   (a Node 11 unit test would have to monkey-patch every node's
   internals to test orchestration logic). The subprocess overhead
   per node is ~50 ms one-time interpreter spin-up — invisible next
   to the actual work.

2. **A single Node 11 invocation runs the entire `queue.json`
   through Nodes 2-10 once.** Node 11 does NOT iterate
   batch-by-batch even though `queue.json.batches` exists -- every
   downstream node already processes all shots in one pass. The
   batches concept inside queue.json was originally for Node 7 GPU
   memory management, but in practice Node 7's per-character SD
   generations are independent and don't need cross-shot batch
   boundaries. One Node 11 invocation = one whole project end-to-end.

3. **No resume capability in v1.** If a Node 11 run fails midway,
   the operator re-runs from scratch. Each node is already
   rerun-safe (wipes its own outputs first), so this just regenerates
   everything. Documented as a known limitation. Resume would
   require per-node "is this output current?" checks (mtime-based
   would be fragile, content-hash-based would be expensive); not
   worth it for 1-50 shot projects.

4. **Single-threaded.** Nodes execute sequentially; within each node
   the existing single-threaded behavior holds. Node 7 is GPU-bound
   (single GPU = serial); Nodes 3-6 are CPU-bound but write to the
   same work dir → parallel runs would have I/O contention.
   Documented as a known limitation; defer parallelism to v2.

5. **Default retries per node = 0 (fail-fast).** Same default as
   Nodes 2-10's CLI behavior. An operator who wants resilience
   opts into it via `--retry-nodeN <int>` flags. Most useful for
   Node 7 (most likely to flake on transient ComfyUI hangs / VRAM
   spikes); other nodes are deterministic and a retry won't change
   the outcome.

6. **Per-node retry override = `--retry-nodeN <int>` flags.** One
   flag per node N (e.g., `--retry-node7=2 --retry-node3=1`).
   Retries are immediate; no exponential backoff in v1 since
   ComfyUI is on the same pod and there's no remote rate-limiting
   to back off from.

7. **Pre-Node-7 GPU visibility check via best-effort `nvidia-smi`
   shell-out.** If it works, log GPU name + free VRAM. If it
   fails (no GPU, no `nvidia-smi` binary, e.g., on the laptop
   --dry-run path), warn but proceed -- Node 7's own `--dry-run`
   handles the no-GPU case. Cheap diagnostic; catches "operator
   forgot to set up the pod" before Node 7 starts and burns minutes.

8. **NO active VRAM monitoring / batch-size auto-reduce in v1.**
   Real VRAM-based pausing requires polling nvidia-smi mid-encode +
   interrupting Node 7 mid-shot, way out of scope. If Node 7 OOMs,
   operator re-runs with a smaller `batchSize` set in the form.
   Documented as a known limitation.

9. **Progress log = `<work-dir>/node11_progress.jsonl`** (newline-
   delimited JSON, append-only). One JSON object per node-step
   start + completion event, with timestamp + duration + exit code +
   attempt-number. Operator can `tail -f` during long pod runs;
   external tools can grep for failures after.

10. **Stdout/stderr passes through Node 11 to the operator's
    terminal in real time.** `subprocess.run` without
    `capture_output` so the operator sees each downstream node's
    progress lines as they happen. Each line is also tee'd to the
    JSONL progress log. Real-time visibility matters for long
    runs; hiding output would make Node 11 feel hung.

11. **Final report = `<work-dir>/node11_result.json`** with
    per-shot per-node status + timing + final-MP4 path + total batch
    wall time. Single consumable artifact -- no XML, no HTML, no
    CSV; other tools transform if needed.

12. **Stdout summary at end = same `[node11] OK ...` shape as other
    nodes.** Format: `N shots / M succeeded / K failed, total Xs,
    MP4s in <output_dir>/`. Consistent with the other 9 CLIs.

13. **Exit-code semantics (DIFFERENT from Nodes 2-10):**
    - All shots succeeded → exit 0
    - **Some shots failed but at least one succeeded → exit 0**
      (partial success), with `failedCount > 0` in
      `node11_result.json` for CI/automation to read
    - All shots failed → exit 1 (`BatchAllFailedError`)
    - Bad inputs (`--input-dir` missing) → exit 1 (`InputDirError`)
    - Unexpected exception → exit 2
    Node 11 owns the partial-success semantic because individual
    nodes can't (they fail the whole batch by design).

14. **Architecture template = same as Nodes 3-6/8/9/10** (Option C
    thin ComfyUI wrapper). All logic in `pipeline/node11.py`;
    `custom_nodes/node_11_batch_manager/__init__.py` only declares
    `INPUT_TYPES` / `RETURN_TYPES` and calls into the core.
    Pure-Python (subprocess + json + datetime + optional
    nvidia-smi shell-out), GPU-agnostic (the GPU dependency is
    pushed entirely into Node 7).

15. **Rerun safety:** Node 11 wipes `node11_progress.jsonl` +
    `node11_result.json` at start of each run. Each subprocess
    invokes a node whose CLI already wipes its own outputs.
    Mirrors the wipe-before-write pattern from every other node.

16. **Dry-run mode:** `--dry-run` passes through to Node 7's
    `--dry-run` flag. Other nodes ignore it (they don't have one).
    Useful for testing the full pipeline plumbing on the laptop
    before pod runs.

Consequences locked in:
- **Inputs (one required CLI path):**
  - `--input-dir <path>` (Node 2's input dir; same shape Node 2
    expects -- metadata.json, characters.json, sheet PNGs, MP4s).
  - `--work-dir <path>` (where every node writes its outputs).
  - Optional: `--comfyui-url <url>` (default
    `http://127.0.0.1:8188`, passed to Node 7), `--crf <int>`
    (default 18, passed to Node 10),
    `--retry-nodeN <int>` per-node retry overrides, `--dry-run`,
    `--quiet`.
- **Outputs:**
  - Every Node 2-10 output (Node 11 doesn't write its own per-shot
    artifacts; it just runs the others).
  - `<work-dir>/node11_progress.jsonl` -- append-only event log
    (start/complete events per `(shotId, nodeNumber, attempt)`).
  - `<work-dir>/node11_result.json` -- aggregate batch report:
    `{schemaVersion, projectName, workDir, startedAt, completedAt,
    totalSeconds, shotResults: [{shotId, status: "ok"|"failed",
    nodeStatuses: [{node: int, status: "ok"|"error", attempts:
    int, durationSeconds: float, exitCode: int}, ...],
    refinedMp4Path: str|null}], totalShots, succeededShots,
    failedShots}`.
- **Typed error hierarchy** grew `Node11Error` base +
  `InputDirError` (--input-dir missing or empty) +
  `NodeStepError` (a specific node N failed after all retries --
  carries node number, exit code, attempt count, last 10 stderr
  lines) + `BatchAllFailedError` (100% failure rate). All under
  the shared `PipelineError` root.
- **CLI:** `run_node11.py --input-dir <i> --work-dir <w>
  [--comfyui-url <url>] [--crf <int>] [--retry-nodeN <int>...]
  [--dry-run] [--quiet]` at repo root; `python -m
  pipeline.cli_node11` equivalent on standard Python. Exit codes
  `0` success or partial success, `1` `Node11Error` subclass, `2`
  unexpected.
- **No new Python dependencies.** subprocess + json + datetime
  + (optional) nvidia-smi shell-out are stdlib / system tools.

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
