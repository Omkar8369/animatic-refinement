"""Generate ai-toolkit training configs + caption files for Phase 2e
per-character LoRAs (TAPPU, JETHALAL).

For each character:
  1. Writes a one-line caption .txt next to each .jpg in the training folder.
     Caption is "[trigger], TMKOC cartoon style" — ai-toolkit substitutes
     "[trigger]" with the character name at training time.
  2. Writes a YAML config matching ai-toolkit's expected structure for
     Flux LoRA training (cloned from config/examples/train_lora_flux_24gb.yaml
     and adapted for our paths + characters).

Run on the pod. The configs are written into the ai-toolkit dir so
ai-toolkit can find them with relative paths.

Example:
  python3 /workspace/animatic-refinement/tools/phase2e/setup_training.py
"""

from __future__ import annotations

from pathlib import Path
import textwrap

# ---------- paths (pod-side) ----------
TRAINING_DATA_ROOT = Path("/workspace/phase2e_training_data")
AI_TOOLKIT_DIR = Path("/workspace/ai-toolkit")
OUTPUT_DIR = Path("/workspace/ai_toolkit_output")  # where LoRAs land
CONFIG_OUT_DIR = AI_TOOLKIT_DIR / "config"  # configs live with ai-toolkit


CHARACTERS = ["TAPPU", "JETHALAL"]
CAPTION_TEMPLATE = "[trigger], TMKOC cartoon style"

# Sample prompts surfaced every 250 steps during training so we can see
# how the LoRA is evolving. Mix of common TMKOC poses.
SAMPLE_PROMPTS = {
    "TAPPU": [
        "[trigger], full body, walking, smiling, TMKOC cartoon style, white background, clean line art",
        "[trigger], close up of face, neutral expression, TMKOC cartoon style",
        "[trigger], standing, hands on hips, angry expression, TMKOC cartoon style",
        "[trigger], sitting on the floor, surprised, TMKOC cartoon style",
        "[trigger], running, side view, TMKOC cartoon style, white background, line art",
        "[trigger], yellow shirt, dark shorts, full body shot, TMKOC cartoon style",
    ],
    "JETHALAL": [
        "[trigger], full body, hand on head, frustrated expression, TMKOC cartoon style",
        "[trigger], close up of face, angry expression, TMKOC cartoon style",
        "[trigger], standing, hand on hip, neutral expression, TMKOC cartoon style",
        "[trigger], walking, surprised expression, TMKOC cartoon style",
        "[trigger], full body, yellow shirt and dark trousers, TMKOC cartoon style, white background, line art",
        "[trigger], close up, sneaky expression, hiding behind something, TMKOC cartoon style",
    ],
}


def write_captions(char: str) -> int:
    """Write a .txt caption file next to each .jpg in the character folder."""
    char_dir = TRAINING_DATA_ROOT / char
    if not char_dir.is_dir():
        print(f"  WARNING: {char_dir} not found, skipping captions")
        return 0
    written = 0
    for jpg in sorted(char_dir.glob("*.jpg")):
        txt = jpg.with_suffix(".txt")
        if not txt.exists():
            txt.write_text(CAPTION_TEMPLATE, encoding="utf-8")
            written += 1
    return written


def yaml_for_character(char: str) -> str:
    """Generate ai-toolkit YAML config string for one character."""
    char_lower = char.lower()
    prompts_yaml = "\n".join(
        f'                  - "{p}"' for p in SAMPLE_PROMPTS[char]
    )
    return textwrap.dedent(f"""\
        ---
        job: extension
        config:
          name: "{char_lower}_lora_v1"
          process:
            - type: 'sd_trainer'
              training_folder: "{OUTPUT_DIR}"
              device: cuda:0
              trigger_word: "{char}"
              network:
                type: "lora"
                linear: 16
                linear_alpha: 16
              save:
                dtype: float16
                save_every: 250
                max_step_saves_to_keep: 4
                push_to_hub: false
              datasets:
                - folder_path: "{TRAINING_DATA_ROOT / char}"
                  caption_ext: "txt"
                  caption_dropout_rate: 0.05
                  shuffle_tokens: false
                  cache_latents_to_disk: true
                  resolution: [ 512, 768, 1024 ]
              train:
                batch_size: 1
                steps: 2000
                gradient_accumulation_steps: 1
                train_unet: true
                train_text_encoder: false
                gradient_checkpointing: true
                noise_scheduler: "flowmatch"
                optimizer: "adamw8bit"
                lr: 1e-4
                ema_config:
                  use_ema: true
                  ema_decay: 0.99
                dtype: bf16
              model:
                name_or_path: "black-forest-labs/FLUX.1-dev"
                is_flux: true
                quantize: true
              sample:
                sampler: "flowmatch"
                sample_every: 250
                width: 768
                height: 1024
                prompts:
{prompts_yaml}
                neg: ""
                seed: 42
                walk_seed: true
                guidance_scale: 4
                sample_steps: 20
        meta:
          name: "{char_lower}_lora_v1"
          version: '1.0'
        """)


def main():
    print(f"training data root: {TRAINING_DATA_ROOT}")
    print(f"ai-toolkit dir: {AI_TOOLKIT_DIR}")
    print(f"config out dir: {CONFIG_OUT_DIR}")
    print(f"lora output dir: {OUTPUT_DIR}")
    print()

    if not TRAINING_DATA_ROOT.is_dir():
        print(f"ERROR: training data root not found: {TRAINING_DATA_ROOT}")
        return 1
    if not AI_TOOLKIT_DIR.is_dir():
        print(f"ERROR: ai-toolkit not found: {AI_TOOLKIT_DIR}")
        return 1

    CONFIG_OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for char in CHARACTERS:
        print(f"=== {char} ===")
        n_caps = write_captions(char)
        print(f"  wrote {n_caps} caption files")

        cfg_path = CONFIG_OUT_DIR / f"phase2e_{char.lower()}_lora_v1.yaml"
        cfg_path.write_text(yaml_for_character(char), encoding="utf-8")
        print(f"  wrote {cfg_path}")
        print()

    print("To start training:")
    for char in CHARACTERS:
        cfg = CONFIG_OUT_DIR / f"phase2e_{char.lower()}_lora_v1.yaml"
        print(f"  cd {AI_TOOLKIT_DIR} && python3 run.py {cfg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
