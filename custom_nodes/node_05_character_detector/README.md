# Node 5 — Character Detection & Position

Thin ComfyUI wrapper around `pipeline/node5.py`. Exposes one node:

**Animatic - Node 5: Character Detector** (`AnimaticNode5CharacterDetector`)

| Input | Type | Default | Meaning |
|---|---|---|---|
| `node4_result_path` | STRING | — | Absolute path to `node4_result.json` produced by Node 4. |
| `queue_path` | STRING | — | Absolute path to `queue.json` produced by Node 2. Needed for each shot's expected characters + positions. |
| `min_area_ratio` | FLOAT | 0.001 | Drop connected-component blobs whose area is below this fraction of frame area. |
| `merge_iou` | FLOAT | 0.5 | Merge two bounding boxes whose IoU meets or exceeds this threshold. |

| Output | Type | Meaning |
|---|---|---|
| `node5_result_json` | STRING | JSON-serialized `Node5Result` (same shape as `node5_result.json` on disk). |

## Behavior

For each shot in `node4_result.json`, Node 5 walks every key-pose PNG and runs:

1. **Binarize (Otsu).** Adaptive threshold picks foreground (ink) pixels vs. background.
2. **Connected components** (scipy `ndimage.label`, 8-connectivity). Each separate island of ink is a candidate character silhouette.
3. **Cleanup.**
   * Drop blobs whose area is below `min_area_ratio × frame_area` (compression speckle, stray ink).
   * Merge any two bounding boxes whose IoU ≥ `merge_iou` (a character's floating detail — e.g. a separate eye dot — reunited with the parent silhouette).
4. **Reconcile** against metadata's `len(characters)`:
   * **Too many** → drop smallest-area blobs until count matches. Log in `warnings[]`.
   * **Too few** → retry detection after `scipy.ndimage.binary_erosion` (up to 3 iterations) to pull touching characters apart.
   * **Still wrong** after max erosion → log `reconcile-failed`; Node 6 will fail cleanly on that key pose.
5. **Position bin** each silhouette's normalized centre-x using the locked 25/20/10/20/25 split → `L`/`CL`/`C`/`CR`/`R`.
6. **Identity assign** (Strategy A — positional). Sort silhouettes left→right by centre-x; sort metadata characters left→right by position rank (`L<CL<C<CR<R`); zip.

## Output layout

```
<shotId>/
    character_map.json      per-shot detection map Node 6 consumes
    keyposes/               (from Node 4)
<work-dir>/
    node5_result.json       aggregate across all shots
```

`character_map.json` carries per-key-pose `detections[]` (with identity, position, bbox, area) plus a `warnings[]` log of every reconcile action.

## Why no ML

Chota Bhim animatics are hand-drawn line art. Most ML detectors were trained on photos or anime and miss this style; even pre-trained on line-art they would need a 2+ GB download per RunPod pod. The animator usually draws characters separated on purpose, so a classical CC pass plus small cleanup rules handles the 95% case — with a reconcile fallback for the remaining 5% (touching characters → erode; floating details → merge).

If real-world mismatch rates turn out to be worse than 5%, we add a v2 Strategy B (reference-sheet-similarity verification) as a post-pass without replacing Strategy A.

## Why a thin wrapper

All logic (argparse, manifest shape, error types) lives in `pipeline/node5.py` so the exact same code path is hit whether Node 5 runs from CLI, tests, CI, or this ComfyUI node. Don't add business logic here — add it upstream in `pipeline/`.

## See also

- `pipeline/node5.py` — core implementation
- `pipeline/cli_node5.py` — CLI entry point
- `docs/PLAN.md` Node 5 section — locked decisions + sub-steps
