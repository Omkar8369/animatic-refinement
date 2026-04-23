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
4. **The canonical spec lives in `docs/PLAN.md` + `docs/Node_Plan.xlsx`.** Any
   design change to a node updates BOTH files before the code is written.

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
| 2    | Metadata Ingestion & Validation        | **NEXT** |
| 3    | Shot Pre-processing (MP4 → PNG)        | Pending  |
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

## Active work — next up: Node 2

Node 2 = Metadata Ingestion & Validation. Open questions to resolve before
writing code:

1. **Where does the pipeline run?** Local Python on RunPod only, or also
   runnable on a CPU box for unit tests?
2. **Validation strictness:** hard-fail the whole batch on any per-shot
   error, or skip the bad shot and continue? (PLAN.md currently says
   "fail fast" at the node level but doesn't specify per-shot behavior.)
3. **Schema validation library:** plain-Python guards, or `pydantic` /
   `jsonschema`? Affects RunPod deps.

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
4. If a node is marked ACTIVE in the status table, resume its open questions
   (listed above). If not, confirm with the user which node to start next.
5. **Never** write code for a node whose design discussion hasn't been resolved
   with the user.

## References

- Approved plan: `docs/PLAN.md`
- Editable working spec: `docs/Node_Plan.xlsx`
- On-disk auto-memory: `C:\Users\Omkar Hajare\.claude\projects\C--Users-Omkar-Hajare-Desktop-download\memory\`
