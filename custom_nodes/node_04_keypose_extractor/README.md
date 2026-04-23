# Node 4 — Key Pose Extraction

Thin ComfyUI wrapper around `pipeline/node4.py`. Exposes one node:

**Animatic - Node 4: Key Pose Extractor** (`AnimaticNode4KeyPoseExtractor`)

| Input | Type | Default | Meaning |
|---|---|---|---|
| `node3_result_path` | STRING | — | Absolute path to `node3_result.json` produced by Node 3. |
| `threshold` | FLOAT | 8.0 | Aligned-MAE (0–255 grayscale) above which a frame becomes a new key pose. |
| `max_edge` | INT | 128 | Downscale so `max(H, W) = max_edge` before comparison. Offsets are scaled back to full resolution on write. |

| Output | Type | Meaning |
|---|---|---|
| `node4_result_json` | STRING | JSON-serialized `Node4Result` (same shape as `node4_result.json` on disk). |

## Behavior

For each shot in `node3_result.json`:

1. Walks `frameFilenames` in order.
2. Phase-correlates each frame against the current key-pose anchor (downscaled grayscale, FFT) to estimate translation.
3. Computes aligned MAE over the overlap region.
4. If aligned MAE ≤ `threshold` → held frame (stored with `(dy, dx)` offset in full-res pixels).
5. Otherwise → new key pose (becomes the new anchor).

Writes:

```
<framesDir>/keyposes/
    frame_NNNN.png      copies of the frames chosen as key poses,
                        source filenames preserved
<framesDir>/keypose_map.json     per-shot partition
<workDir>/node4_result.json       aggregate across all shots
```

## Why translation-aware?

Animatic shots often show a character sliding across the frame without changing pose (a storyboard shortcut for walking/running). A naïve pixel-diff would flag every intermediate frame as a new key pose, forcing Node 7 to re-refine an identical pose N times. Phase correlation recovers the translation; aligned MAE measures similarity *after* translating, so slides become one key pose with per-held-frame offsets that Node 9 replays by translate-and-copy.

## Why a thin wrapper

All logic (argparse, manifest shape, error types) lives in `pipeline/node4.py` so
the exact same code path is hit whether Node 4 runs from CLI, tests, CI, or this
ComfyUI node. Don't add business logic here — add it upstream in `pipeline/`.

## See also

- `pipeline/node4.py` — core implementation
- `pipeline/cli_node4.py` — CLI entry point
- `docs/PLAN.md` Node 4 section — locked decisions + sub-steps
