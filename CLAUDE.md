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
| 6    | Character Reference Sheet Matching     | **NEXT** |
| 7    | AI-Powered Pose Refinement             | Pending  |
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
- `characters.json` carries an `angleOrderConfirmed: false` flag — Node 6
  must confirm the canonical 8-angle order with the user before slicing.

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

## Active work — next up: Node 6

Node 6 = Character Reference Sheet Matching. Open questions to
resolve before writing code:

1. **8-angle sheet slicing.** Each character's sheet is one
   horizontal PNG with 8 poses side-by-side on a transparent/black
   background. Slicing must find each character's alpha-island
   bbox (not assume equal widths, because the animator draws freely).
   Pillow alpha-channel + scipy CC on alpha islands is the obvious
   path — same classical toolkit as Node 5. Open: do we require the
   sheet have alpha (and fail loudly on RGB-only), or auto-fallback
   to Otsu-on-grayscale like Node 5? Recommend: require alpha, fail
   loud with a pointer to re-export from the art tool.
2. **Canonical 8-angle order confirmation.** CLAUDE.md locks the
   order as `back, back-¾-L, profile-L, front-¾-L, front, front-¾-R,
   profile-R, back-¾-R` *pending final user confirmation*. Node 6
   must respect `characters.json.conventions.angleOrderConfirmed`
   — if `false`, prompt the user (or fail with a clear message)
   before slicing. Open: interactive prompt (bad in RunPod) vs
   structured-fail with instructions to flip the flag.
3. **Angle selection per key pose.** Given a key-pose PNG with a
   detected character silhouette (from Node 5), which of the 8
   reference angles matches? Options: (a) classical silhouette-shape
   similarity (Hu moments, IoU on centred silhouettes), (b) small
   pre-trained pose model (OpenPose / MediaPipe), (c) CLIP image-image
   similarity. Start classical; promote to CLIP only if hit rate is
   poor. Open: do we need Node 5 to also emit a silhouette mask
   (PNG) so Node 6 can compare directly, or recompute the mask in
   Node 6 from the same bbox? Recommend: recompute — keeps Node 5's
   output small and text-only.
4. **Line-art conversion of the color reference.** The animator's
   sheet is full-color but Node 7 generates BnW line art. Node 6
   must line-art-ify the chosen reference crop before IP-Adapter /
   Reference-Only conditioning so the reference is BnW-aligned.
   Options: (a) classical Canny / LoG / difference-of-gaussians,
   (b) a pre-trained line-art extraction model. Open: does classical
   edge detection preserve enough character identity, or do we
   need the ML model? Lean classical first; profile on real sheets.
5. **Per-key-pose or per-shot matching?** Same question Node 5 faced.
   Per-key-pose is safer (character can turn). Per-shot is faster
   (one reference crop per character per shot). Given Node 5 settled
   on per-key-pose and Node 7 is the expensive step anyway, Node 6
   should also be per-key-pose. Confirm with user.

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
  front-¾-L, front, front-¾-R, profile-R, back-¾-R. *(Pending final user
  confirmation — flag this when first touching Node 6.)*
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
5. If a node is marked ACTIVE in the status table, resume its open questions
   (listed above). If not, confirm with the user which node to start next.
6. **Never** write code for a node whose design discussion hasn't been resolved
   with the user.

## References

- Approved plan: `docs/PLAN.md`
- Editable working spec: `docs/Node_Plan.xlsx`
- On-disk auto-memory: `C:\Users\Omkar Hajare\.claude\projects\C--Users-Omkar-Hajare-Desktop-download\memory\`
