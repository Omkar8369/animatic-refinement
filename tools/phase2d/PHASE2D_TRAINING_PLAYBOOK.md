# Phase 2d — TMKOC v1 Style LoRA Training Playbook

This is the operational runbook for the **Phase 2d-run** ship: training
a custom `tmkoc_style_v1.safetensors` LoRA via Path A (img2img-generated
synthetic dataset). Phase 2d-prep already shipped the integration
infrastructure; this runbook is the live-pod work that produces the
actual safetensors weight.

When this runbook lands a working LoRA, the follow-up commit ("Phase
2d-run") fills in `models.json`'s `tmkoc-style-v1` entry's `url` +
`sha256` and runs the regression tests against the new weight.

## Status

- **Prep (code) shipped:** 2026-04-27 — `--style-lora {flat_cartoon_v12,tmkoc_v1}`
  flag, placeholder `models.json` entry, this runbook + the ai-toolkit
  config template, ComfyUI dropdown.
- **Run (LoRA file) pending:** requires a live A100 80GB session
  (~2 hours wall-time, ~$5 RunPod community spot pricing) plus ~6
  hours of curation / captioning work outside that session.

## Prerequisites

- A working RunPod pod with A100 80GB (the design-locked target per
  Phase 2 locked decision #11). 4090 24GB is technically fine for
  training a rank-16 LoRA on Flux but tight on VRAM; A100 is safer.
- Phase 2c shipped (verify: `git log --oneline | head -5` shows
  `cce5e2b Implement Node 7 Phase 2c: img2img mode + flip --workflow default to v2`).
- `runpod_setup.sh` completed on the pod (Flux + Style LoRA + Union CN +
  IP-Adapter weights all downloaded; verify
  `ls /workspace/ComfyUI/models/diffusion_models/` shows `flux1-dev-fp16.safetensors`).
- `Actual_Testing/` (or any folder with rough animatic shots +
  `characters.json` + character sheets) staged on the pod for dataset
  generation. The original TMKOC EP35 SH004 fixture works, but more
  shots produce more diverse training data and a better LoRA.
- `ai-toolkit` (ostris) cloned + installed on the pod —
  `git clone https://github.com/ostris/ai-toolkit.git /workspace/ai-toolkit
  && cd /workspace/ai-toolkit && pip install -r requirements.txt`.

## High-level flow

```
[1] dataset-gen   → run Phase 2c v2 with varied seeds across N rough shots
                    → ~100 candidate img2img outputs + their captions
[2] curation      → human eyeballs each output, drops the bad ones,
                    keeps 60-80 best as the training set
[3] caption-fix   → BLIP-2 / CogVLM / GPT-4V captions, manually cleaned
[4] training      → ai-toolkit runs ~2000 steps on the curated set,
                    saves checkpoints every 250 steps
[5] validation    → ComfyUI test runs on each checkpoint, pick winner
[6] ship          → sha256 the winning checkpoint, fill in models.json,
                    commit + push as Phase 2d-run
```

Total time: ~6 hours of human time + ~1.5 hours of GPU time per LoRA
iteration. Expect 2-3 iterations before you ship.

## Step 1 — Generate the bootstrap dataset (~1 hour GPU time)

For each rough animatic shot in your training pool, run Phase 2c v2
across multiple seeds + slight prompt variations. The goal is 100-150
candidate images covering varied poses, characters, scenes, expressions.

### Build the dataset gen command

The Phase 2c CLI handles seed-driven variation natively (every detection
gets a deterministic seed derived from `(project, shotId, keyPoseIndex,
identity)`). To get N variations of the SAME detection, run Node 7 with
N different "project" names (which changes the seed):

```bash
# On the pod, from the repo root:
cd /workspace/animatic-refinement

# For each variation seed in 1..10, run a Node 7 dry-run-OFF generation
# against the same Node 6 result. Each run produces 1 image per detection
# at a different seed — over 10 runs across ~5 detections that's 50
# images. Add more shots / detections to get more.
mkdir -p /workspace/phase2d/dataset_raw

for seed_variation in $(seq 1 10); do
  # Patch the project name in the Node 6 result file so the seed derives
  # differently each iteration. This is a quick hack; production tooling
  # will get a proper --seed-suffix flag. For now jq does the trick.
  cp /workspace/<work>/node6_result.json /tmp/n6_${seed_variation}.json
  jq ".projectName = \"phase2d_v${seed_variation}\"" \
    /workspace/<work>/node6_result.json > /tmp/n6_${seed_variation}.json

  python3 run_node7.py \
    --node6-result /tmp/n6_${seed_variation}.json \
    --queue        /workspace/<work>/queue.json \
    --workflow=v2 --precision=fp16 \
    --style-lora=flat_cartoon_v12   # bootstrap on top of generic flat-cartoon

  # Move the outputs into a flat folder for curation.
  cp /workspace/<work>/shot_*/refined/*.png \
     /workspace/phase2d/dataset_raw/v${seed_variation}_$(date +%s).png
done
```

Expected output: 100+ PNGs in `/workspace/phase2d/dataset_raw/`. Each
is a Phase 2c v2 generation — Flux + Flat Cartoon Style + Union CN +
IP-Adapter + img2img output, varied by seed across the rough animatic.

### Sanity check

```bash
ls /workspace/phase2d/dataset_raw/ | wc -l   # should be 100+
file /workspace/phase2d/dataset_raw/v1_*.png  # confirm PNG, not corrupt
```

If a generation produced a black/empty image (rare but happens with
Flux when the rough is degenerate), it'll be obvious in the file size:

```bash
find /workspace/phase2d/dataset_raw/ -size -50k -print  # tiny files = bad
```

## Step 2 — Curate (~2 hours of human eyeballs, no GPU)

Open the dataset folder in any image viewer (the pod's web UI works,
or `scp -r` it to your laptop and use Windows Explorer / Preview).

**Drop:** corrupted / mostly-empty outputs, generations where the
character lost clear identity, generations where the style is way off
(too anime, too realistic, too dark), duplicates of the same pose.

**Keep:** clean line art, recognizable TMKOC-style characters, varied
poses and expressions, varied backgrounds, clean character shapes.

Target: **60-80 images** in the curated folder. Fewer than 50 starves
the LoRA; more than 100 just slows training without quality benefit
for a rank-16 LoRA at this resolution.

```bash
# After curation, move the picks into a clean folder:
mkdir -p /workspace/phase2d/dataset_curated
# (manually copy the keepers from dataset_raw/ to dataset_curated/)
```

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

1. **First word is the trigger:** `TMKOC` (or your chosen trigger).
2. **Caption everything that ISN'T the style** — subject, pose,
   expression, background. The LoRA learns "TMKOC style" as the
   residual.
3. **Remove anti-style descriptors:** "anime", "manga", "realistic",
   "3d", "photo" — these confuse training.

Example:

```
# BAD (BLIP-2 raw):
"a young indian boy in green hoodie, anime style, smiling"

# GOOD (cleaned up):
"TMKOC, young indian boy, green hoodie, brown shorts, standing,
 smiling, indoor living room background"
```

Use `find` + `sed` for batch cleanup of common BLIP-2 outputs:

```bash
# Strip "anime" and similar style-descriptors:
sed -i 's/, anime//g; s/anime //g; s/manga //g; s/, realistic//g' \
    /workspace/phase2d/dataset_curated/*.txt

# Prepend the trigger to every caption:
for f in /workspace/phase2d/dataset_curated/*.txt; do
  caption=$(cat "$f")
  echo "TMKOC, $caption" > "$f"
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

- ✅ Output looks like TMKOC style on novel prompts
  ("TMKOC, an elderly man waving" — even if not in training set)
- ✅ Style stays consistent across multiple test prompts
- ✅ Doesn't override character identity (when paired with IP-Adapter)
- ✅ No double lines or weird artifacts
- ❌ Doesn't memorize specific training images (test with prompts
  unrelated to training set)

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
| Output too generic (no TMKOC style) | More training data; OR train longer (3000-4000 steps) |
| Output overrides characters | Lower strength_clip in workflow_flux_v2.json node 20 (was 0.75; try 0.6) |
| Output looks like training images directly | Overtrained; pick an earlier checkpoint OR reduce steps |
| Style is inconsistent across prompts | Improve caption discipline; ensure trigger word is consistent |
| Output is washed out / desaturated | Lower learning rate (5e-5 instead of 1e-4) |

Each iteration is another ~1.5 hours of GPU. Budget for 2-3 iterations
before you have a shippable LoRA.

## Cost summary

Per iteration (RunPod A100 80GB community spot @ $1.19/hr):

| Step | Time | Cost |
|---|---|---|
| Step 1 (dataset gen) | 1 hr | $1.20 |
| Step 4 (training) | 1.5 hr | $1.80 |
| Step 5 (validation) | 0.5 hr | $0.60 |
| **Per iteration** | **3 hr** | **$3.60** |

Plus ~6 hours of human curation/captioning per iteration (no GPU cost).

For 2-3 iterations: ~$10-15 + ~12-18 hours of human time to ship a
production-ready LoRA. This matches the `~$5-15 + 6-10 hours per
character` estimate from the design-lock conversation; style LoRAs are
typically slightly more expensive than character LoRAs because dataset
quality matters more.
