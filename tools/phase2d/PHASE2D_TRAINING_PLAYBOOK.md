# Phase 2d — TMKOC v1 Line-Art Style LoRA Training Playbook

This is the operational runbook for the **Phase 2d-run** ship: training
a custom `tmkoc_style_v1.safetensors` LoRA. Phase 2d-prep already
shipped the integration infrastructure; this runbook is the live-pod
work that produces the actual safetensors weight.

When this runbook lands a working LoRA, the follow-up commit ("Phase
2d-run") fills in `models.json`'s `tmkoc-style-v1` entry's `url` +
`sha256` and runs the regression tests against the new weight.

## Phase 2-revision change to training data (IMPORTANT)

The **original Phase 2d-prep playbook (2026-04-27) used "Path A"**:
generate a synthetic dataset by running Phase 2c v2 img2img across many
seeds against rough animatic shots, then curate the best 60-80 outputs
as the training set. That approach assumed v2's deliverable was COLORED
TMKOC scenes (which is what Phase 2c briefly produced).

**Phase 2-revision (2026-04-28) corrected the deliverable**: Part 1's
locked spec is BnW line art on white BG with characters-only (no BG
furniture), and the user works from storyboard scene cuts that already
look exactly like that. So Phase 2d-run **uses the user's storyboard
cuts directly as training data** — they're already curated, already in
the target aesthetic, and don't need a synthetic-bootstrap step. The
Path A bootstrap is OBSOLETE for this LoRA.

The training procedure (ai-toolkit on A100) is unchanged; only the
dataset source + the captions + a few sanity-check rules differ.

## Status

- **Prep (code) shipped:** 2026-04-27 — `--style-lora {flat_cartoon_v12,tmkoc_v1}`
  flag, placeholder `models.json` entry, this runbook + the ai-toolkit
  config template, ComfyUI dropdown.
- **Phase 2-revision (2026-04-28):** training-data approach swapped
  from synthetic Path A bootstrap to direct storyboard scene cuts.
- **Run (LoRA file) pending:** requires a live A100 80GB session
  (~2 hours wall-time, ~$5 RunPod community spot pricing) plus ~2-4
  hours of caption-cleanup work outside that session (curation already
  done by the storyboard artist when they drew the cuts).

## Prerequisites

- A working RunPod pod with A100 80GB (the design-locked target per
  Phase 2 locked decision #11). 4090 24GB is technically fine for
  training a rank-16 LoRA on Flux but tight on VRAM; A100 is safer.
- Phase 2-revision shipped (verify: `git log --oneline | head -5`
  shows the Phase 2-revision commit: per-character bbox crop +
  BnW line-art prompts + STYLE_LORA_STRENGTHS table).
- `runpod_setup.sh` completed on the pod (Flux + Union CN +
  IP-Adapter weights all downloaded; verify
  `ls /workspace/ComfyUI/models/diffusion_models/` shows `flux1-dev-fp16.safetensors`).
- **Storyboard scene cuts** scp'd to the pod. The user provides clean
  digital BnW line drawings on white background — characters in their
  shot positions, with light-line BG furniture, dashed safe-area
  rectangles. These ARE the training data; no synthetic bootstrap.
  Target dataset size: 60-100 storyboard cut PNGs covering varied
  shots / poses / characters / scenes.
- `ai-toolkit` (ostris) cloned + installed on the pod —
  `git clone https://github.com/ostris/ai-toolkit.git /workspace/ai-toolkit
  && cd /workspace/ai-toolkit && pip install -r requirements.txt`.

## High-level flow

```
[1] dataset stage    → scp the user's storyboard cuts to /workspace/phase2d/dataset_curated/
                       (no generation step — cuts are already in the target aesthetic)
[2] sanity check     → confirm count + visual quality + uniform aspect / DPI
[3] caption          → BLIP-2 / CogVLM / GPT-4V captions, manually cleaned
                       to emphasize BnW line art (NOT color)
[4] training         → ai-toolkit runs ~2000 steps on the dataset,
                       saves checkpoints every 250 steps
[5] validation       → ComfyUI test runs on each checkpoint, pick winner
[6] ship             → sha256 the winning checkpoint, fill in models.json,
                       commit + push as Phase 2d-run
```

Total time: ~3-4 hours of human time (mostly caption cleanup) + ~1.5
hours of GPU time per LoRA iteration. Expect 1-2 iterations — the
storyboard cuts are clean training data so the LoRA converges fast.

## Step 1 — Stage the storyboard dataset (no GPU time)

The user provides clean digital BnW storyboard scene cuts — these are
the training data. **No synthetic generation step.**

Each storyboard cut is a PNG/JPG showing:
- Black ink line drawings on a white background
- Characters in their shot positions (correct scale + spatial relations)
- Optionally: light-line BG furniture (counters, walls, props)
- Optionally: dashed safe-area rectangles
- Optionally: small red action marks (motion arrows, etc.)

These three elements (characters + BG furniture + safe-area marks)
together represent the target aesthetic. The LoRA learns the
character-line aesthetic; v2's prompts will tell Flux to skip the BG
furniture + safe-area marks at generation time. (Including the red
action marks in training data is optional — they're shot-direction
artifacts, not part of the deliverable line aesthetic. If your dataset
has many of them, captioning each as "with red motion mark" lets the
LoRA learn to associate them with the trigger and skip them by default.)

### Stage the cuts on the pod

```bash
# On your laptop, scp the storyboard cuts up to the pod:
scp -P <pod-port> -r ./storyboard_cuts/ \
    root@<pod-ip>:/workspace/phase2d/dataset_curated/

# On the pod, count + verify:
ls /workspace/phase2d/dataset_curated/ | wc -l   # should be 60-100
file /workspace/phase2d/dataset_curated/*.png    # confirm PNG, not corrupt
```

### Sanity check

```bash
# Look for unusually small files (might be corrupt)
find /workspace/phase2d/dataset_curated/ -size -50k -print

# Confirm they're roughly uniform DPI / aspect — wildly varied
# sizes hurt LoRA training. Flux wants ≥768px on the longest edge.
python3 - <<'PY'
from PIL import Image
from pathlib import Path
for p in sorted(Path("/workspace/phase2d/dataset_curated").glob("*.png")):
    img = Image.open(p)
    print(f"{p.name}: {img.size}")
PY
```

If many images are <768px on the longest edge, upscale them to ~1024px
with PIL before training (`img.resize((new_w, new_h), Image.LANCZOS)`).
Don't training-time-upscale below the source resolution — the LoRA
will memorize the upscaling artifacts.

## Step 2 — Curate (mostly already done — quick visual scan)

Storyboard cuts are already curated by the artist when they drew them.
You're just doing a quick scan to drop:

- **Corrupt / unreadable PNGs** (rare but possible after scp)
- **Cuts with non-line-art content baked in** (color fills, half-finished
  shading) — those train the LoRA wrong
- **Wildly stylistic outliers** that don't match the bulk of the
  dataset (a single sketchy cut among 80 clean ones will pull the
  LoRA toward sketchy output)

Target: **60-100 images** in the curated folder. Fewer than 40 starves
the LoRA; more than 150 just slows training without quality benefit
for a rank-16 LoRA at this resolution.

## Step 3 — Caption (~1 hour: 30 min auto-caption + 30 min cleanup)

Auto-caption with BLIP-2 (lightweight, runs in ~1 sec/image on GPU)
or CogVLM / GPT-4V (slower, more accurate). Then manually edit each
caption to add the TMKOC trigger and remove style descriptors.

### Auto-caption with BLIP-2

```bash
pip install transformers pillow torch
cd /workspace/phase2d

python3 - <<'PY'
from transformers import Blip2Processor, Blip2ForConditionalGeneration
from PIL import Image
import torch
from pathlib import Path

processor = Blip2Processor.from_pretrained("Salesforce/blip2-opt-2.7b")
model = Blip2ForConditionalGeneration.from_pretrained(
    "Salesforce/blip2-opt-2.7b",
    torch_dtype=torch.float16,
).to("cuda")

curated = Path("/workspace/phase2d/dataset_curated")
for img_path in sorted(curated.glob("*.png")):
    image = Image.open(img_path).convert("RGB")
    inputs = processor(images=image, return_tensors="pt").to("cuda", torch.float16)
    out = model.generate(**inputs, max_new_tokens=50)
    caption = processor.decode(out[0], skip_special_tokens=True).strip()
    txt_path = img_path.with_suffix(".txt")
    txt_path.write_text(caption, encoding="utf-8")
    print(f"{img_path.name}: {caption}")
PY
```

### Manual caption cleanup

For each `.txt` file, edit so that:

1. **First words are the trigger:** `TMKOC line art` (the trigger is
   what v2's prompt template will use to invoke the LoRA at
   generation time).
2. **Caption everything that ISN'T the line aesthetic** — subject,
   pose, expression, scene. The LoRA learns "TMKOC line art" as the
   residual.
3. **Remove anti-style descriptors:** "anime", "manga", "realistic",
   "3d", "photo" — these confuse training.
4. **DO NOT add color descriptors** ("red shirt", "green hat",
   "blue sky") even if the storyboard artist's line drawing makes
   the character recognizable as "Tappu" (who has a green hoodie in
   the colored show). The LoRA's job is to bias toward BnW line art;
   color references in captions confuse the bias.
5. **DO NOT describe BG furniture / safe-area marks** in the caption.
   The LoRA will learn that "TMKOC line art" includes those things
   visually; v2's negative prompt at generation time tells Flux to
   skip them. If you caption "TMKOC line art, two boys talking" and
   show training data with BG furniture, the LoRA learns to draw
   only what the caption says (subject) and the negative prompt
   filters out the rest.

Example:

```
# BAD (BLIP-2 raw):
"a young indian boy in green hoodie smiling, anime style"

# GOOD (cleaned up for Phase 2-revision):
"TMKOC line art, young boy, hoodie, smiling, head and shoulders shot"

# ALSO GOOD (multi-character cut):
"TMKOC line art, two boys, one tall and one short, talking,
 indoor scene"
```

Use `find` + `sed` for batch cleanup of common BLIP-2 outputs:

```bash
# Strip color references + style descriptors that conflict with the
# line aesthetic:
sed -i 's/, anime//g; s/anime //g; s/manga //g; s/, realistic//g' \
    /workspace/phase2d/dataset_curated/*.txt
sed -i 's/red //g; s/green //g; s/blue //g; s/yellow //g; s/orange //g' \
    /workspace/phase2d/dataset_curated/*.txt
sed -i 's/, color//g; s/colorful //g' \
    /workspace/phase2d/dataset_curated/*.txt

# Prepend the trigger to every caption:
for f in /workspace/phase2d/dataset_curated/*.txt; do
  caption=$(cat "$f")
  echo "TMKOC line art, $caption" > "$f"
done
```

## Step 4 — Training (~1.5 hours GPU time)

Copy `tools/phase2d/ai_toolkit_config_template.yaml` to a per-run name,
fill in the dataset path, and launch ai-toolkit.

```bash
cd /workspace/animatic-refinement
cp tools/phase2d/ai_toolkit_config_template.yaml \
   /workspace/phase2d/train_tmkoc_v1.yaml

# Edit train_tmkoc_v1.yaml — set:
#   - process[0].config.training_folder: /workspace/phase2d/output_v1
#   - process[0].config.datasets[0].folder_path: /workspace/phase2d/dataset_curated
#   - process[0].config.train.steps: 2000

# Launch ai-toolkit:
cd /workspace/ai-toolkit
python run.py /workspace/phase2d/train_tmkoc_v1.yaml
```

ai-toolkit saves a checkpoint every 250 steps. After ~1.5 hours the
training completes and you'll have:

```
/workspace/phase2d/output_v1/
  TMKOC_v1_000250.safetensors  ~80 MB
  TMKOC_v1_000500.safetensors  ~80 MB
  TMKOC_v1_000750.safetensors  ~80 MB
  TMKOC_v1_001000.safetensors  ~80 MB
  TMKOC_v1_001250.safetensors  ~80 MB
  TMKOC_v1_001500.safetensors  ~80 MB
  TMKOC_v1_001750.safetensors  ~80 MB
  TMKOC_v1_002000.safetensors  ~80 MB  (final)
  samples/
    sample_step_250_*.png   (test images at each checkpoint)
    sample_step_500_*.png
    ...
```

## Step 5 — Validation (~1 hour, ~$2 of GPU time)

Per-checkpoint test in ComfyUI:

```bash
# For each checkpoint, drop it into models/loras/ and run a Node 7 v2
# generation against the TMKOC fixture. Compare outputs visually.

for step in 1000 1500 2000; do
  cp /workspace/phase2d/output_v1/TMKOC_v1_00${step}.safetensors \
     /workspace/ComfyUI/models/loras/tmkoc_style_v1.safetensors

  python3 run_node7.py \
    --node6-result /workspace/<work>/node6_result.json \
    --queue        /workspace/<work>/queue.json \
    --workflow=v2 --precision=fp16 \
    --style-lora=tmkoc_v1

  # Pull outputs back; tag with the checkpoint number:
  mkdir -p /workspace/phase2d/validation/step_${step}
  cp /workspace/<work>/shot_*/refined/*.png \
     /workspace/phase2d/validation/step_${step}/
done
```

Open each `validation/step_NNNN/*.png` and pick the **earliest checkpoint
that looks right**. Earlier is generally better (later checkpoints often
overfit and lose variety). Most Flux character/style LoRAs converge
around step 1000-1500 at LR 1e-4.

**Acceptance criteria for the winning checkpoint:**

- ✅ Output is BnW line art on white BG (no color, no fill, no
  shading) — even when the prompt doesn't explicitly say "line art"
- ✅ Output looks like the storyboard cuts' aesthetic on novel prompts
  ("TMKOC line art, an elderly man waving" — even if not in training set)
- ✅ Line weight is consistent + bold (matches storyboard cut style)
- ✅ Style stays consistent across multiple test prompts
- ✅ Doesn't override character identity (when paired with IP-Adapter
  on a colored reference crop, the IP-Adapter pulls identity, the
  LoRA shapes the line aesthetic)
- ✅ No double lines, no sketchy / pencil texture, no weird artifacts
- ❌ Doesn't memorize specific training images (test with prompts
  unrelated to training set)
- ❌ Doesn't pull BG furniture into outputs that don't ask for it
  (v2's negative prompt should still filter, but a well-trained LoRA
  shouldn't fight the negative)

## Step 6 — Ship the LoRA (~30 minutes laptop time)

When you've picked a winner:

```bash
# Compute the sha256 on the pod:
WINNER=/workspace/phase2d/output_v1/TMKOC_v1_001500.safetensors
sha256sum "$WINNER"

# Note the hash + size; you'll fill them into models.json.

# Upload the LoRA to a hosting target. Two options:
#   (A) Private HuggingFace repo (recommended): create a model repo,
#       `huggingface-cli login`, `huggingface-cli upload <repo> $WINNER`
#   (B) S3 / R2 / personal CDN — bare HTTPS URL.

# Note the URL.
```

Then on the laptop, edit `custom_nodes/node_07_pose_refiner/models.json`
and update the `tmkoc-style-v1` entry:

```diff
   {
     "name": "tmkoc-style-v1",
-    "url": "TODO: replace after the first known-good Phase 2d training run...",
-    "sha256": "",
-    "sizeMB": 80,
+    "url": "https://huggingface.co/<your-repo>/resolve/main/tmkoc_style_v1.safetensors",
+    "sha256": "<the sha256 from sha256sum>",
+    "sizeMB": <actual MB>,
     "destination": "models/loras/tmkoc_style_v1.safetensors",
     ...
   }
```

Run the test suite (still passes since the schema didn't change), then
commit:

```
git add custom_nodes/node_07_pose_refiner/models.json
git commit -m "Phase 2d-run: ship TMKOC v1 style LoRA (...)" \
  -m "Co-Authored-By: ..."
git push origin HEAD:main
```

That's the Phase 2d-run commit. After this, `--style-lora=tmkoc_v1`
works end-to-end without manual file copy: a fresh pod boot pulls the
LoRA via `runpod_setup.sh`'s curl + sha256 verify loop, ComfyUI loads
it at node 20, and Phase 2 v2 generations use the custom-trained
TMKOC style.

## Iteration

If checkpoint testing reveals the LoRA isn't good enough, common fixes:

| Symptom | Fix |
|---|---|
| Output too generic (no TMKOC line aesthetic) | More training data; OR train longer (3000-4000 steps) |
| Output has color / fill bleeding in | Caption discipline failed (color words leaked through); OR Flat Cartoon LoRA wasn't bypassed (verify workflow_flux_v2.json node 20 strength is 0.0 + Phase 2-revision's prompt is in use); OR storyboard cuts had color in them (re-screen the dataset) |
| Output looks like training images directly | Overtrained; pick an earlier checkpoint OR reduce steps |
| Style is inconsistent across prompts | Improve caption discipline; ensure "TMKOC line art" trigger is consistent |
| Output is sketchy / pencil-textured | Storyboard cuts had sketchy artifacts in them; re-screen dataset for clean digital lines only |
| Output overrides character identity | Lower strength_clip in STYLE_LORA_STRENGTHS (was 0.75; try 0.6) |

Each iteration is another ~1.5 hours of GPU. Budget for 1-2 iterations
before you have a shippable LoRA — storyboard cuts are clean training
data so convergence is faster than synthetic-bootstrap datasets.

## Cost summary

Per iteration (RunPod A100 80GB community spot @ $1.19/hr):

| Step | Time | Cost |
|---|---|---|
| Step 1 (dataset stage) | 0 GPU | $0 (scp only) |
| Step 4 (training) | 1.5 hr | $1.80 |
| Step 5 (validation) | 0.5 hr | $0.60 |
| **Per iteration** | **2 hr** | **$2.40** |

Plus ~3-4 hours of human caption-cleanup per iteration (no GPU cost).
Curation is mostly free — the storyboard artist already curated when
they drew the cuts.

For 1-2 iterations: ~$3-5 + ~4-8 hours of human time to ship a
production-ready LoRA. Phase 2-revision's direct-storyboard approach
is roughly 3-4× cheaper than Phase 2d-prep's synthetic Path A
estimate (~$10-15 + ~12-18 hrs) because the dataset stage is free
and curation is much faster.
