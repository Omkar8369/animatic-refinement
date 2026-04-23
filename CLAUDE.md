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
| 3    | Shot Pre-processing (MP4 → PNG)        | **NEXT** |
| 4    | Key Pose Extraction                    | Pending  |
| 5    | Character Detection & Position         | Pending  |
| 6    | Character Reference Sheet Matching     | Pending  |
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

## Active work — next up: Node 3

Node 3 = Shot Pre-processing (MP4 → PNG Sequence). Open questions to
resolve before writing code:

1. **How is FFmpeg invoked?** Plain `subprocess` to the system `ffmpeg`
   binary, or a Python binding like `imageio-ffmpeg` that bundles one?
   RunPod images typically ship ffmpeg; the Windows embedded Python does
   not. Affects portability vs local-dev friction.
2. **Working directory layout.** One `tmp/shot_NNN/frame_YYY.png` per
   shot (isolated), or one flat folder `tmp/shot_XXX_frame_YYY.png` for
   all frames? PLAN.md 3C/3D lean "isolated per shot" but Node 4 needs
   to know.
3. **Frame-count mismatch policy.** PLAN.md 3E says "log mismatch" — is
   a mismatch fatal (like Node 2), a warning that continues, or does
   Node 3 auto-resample to match metadata duration? Real rough animatics
   are often a few frames off.
4. **ComfyUI custom node vs standalone CLI.** Node 3 is the first
   pipeline-runtime node. Does it live as a ComfyUI custom node under
   `custom_nodes/node_03_mp4_to_png/` (workflow-driven, same as Nodes
   4-10 will be), or as another `pipeline/` CLI like Node 2? The
   PLAN's architecture implies ComfyUI, but Node 3 has no GPU work —
   a CLI would be simpler to test.

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
