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
Purpose: Capture all shot metadata and asset uploads from the artist/operator before the AI pipeline runs.

- **1A. HTML Form Design** — single-page form for per-shot metadata entry.
- **1B. Form Fields** — Shot ID, Character Count (1–N), Character Identity (dropdown linked to model sheets), Character Position per character (Exact Center / Center-Left / Center-Right / Left / Right), Shot Duration in frames @ 25 FPS.
- **1C. MP4 Batch Upload UI** — multi-file upload for shot MP4s; preview thumbnails.
- **1D. Character Model Sheet Upload UI** — one-time upload per project; multi-angle sheets stored keyed by character name.
- **1E. Batch Size Configuration** — numeric input to set how many shots the RunPod instance processes per batch (governs VRAM headroom).
- **1F. Metadata Export** — writes a JSON (or CSV) file pairing each MP4 with its metadata row; this file is the hand-off into Node 2.

### NODE 2 — Metadata Ingestion & Validation
Purpose: Load the form's JSON, sanity-check it, and build the processing queue.

- **2A. Parse metadata file** — read JSON produced by Node 1F.
- **2B. Validate character references** — confirm every character name listed in metadata has a matching uploaded model sheet.
- **2C. Shot ↔ Character mapping** — build in-memory table: shot ID → list of (character, position) tuples.
- **2D. Processing queue** — ordered list of shots to render, respecting batch size.
- **2E. Shot ID generation** — deterministic naming (e.g., `shot_001`, `shot_002`) used by every downstream node.

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
