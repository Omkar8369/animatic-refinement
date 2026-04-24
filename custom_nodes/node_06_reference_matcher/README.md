# Node 6 - Character Reference Sheet Matching

ComfyUI custom node. Thin wrapper over `pipeline/node6.py` — all business
logic lives there. See [../../docs/PLAN.md](../../docs/PLAN.md) Node 6
for sub-steps and locked decisions.

## What it does

Given a completed Node 5 run, for every detected character silhouette
on every key pose:

1. Slice the character's 8-angle horizontal-strip sheet into 8 crops
   via alpha-island bbox labelling (RGBA PNGs only — RGB sheets fail
   loud).
2. Recompute the detection silhouette from the key-pose PNG + Node 5's
   bbox (Otsu + largest connected component).
3. Score each of the 8 reference angles against the detection using a
   classical multi-signal function: silhouette IoU + horizontal-symmetry
   consistency + bbox aspect match + upper-region interior-edge density.
4. Pick the winning angle, write its color crop into
   `<shotId>/reference_crops/<identity>_<angle>.png`, and write a
   Difference-of-Gaussians line-art copy into
   `<shotId>/reference_crops/<identity>_<angle>_lineart.png`.

Multiple key poses that select the same angle for the same character
share one color file + one line-art file.

## Inputs (ComfyUI node)

| Name | Type | Purpose |
|---|---|---|
| `node5_result_path` | STRING | Absolute path to `node5_result.json` from Node 5 |
| `queue_path` | STRING | Absolute path to `queue.json` from Node 2 |
| `characters_path` | STRING | Absolute path to `characters.json` from Node 1 |
| `lineart_method` | STRING | `dog` (default) / `canny` / `threshold` |

## Outputs

- Per shot: `<shotId>/reference_map.json` next to `character_map.json`.
- Per shot: `<shotId>/reference_crops/` populated with cached color +
  line-art PNGs (one pair per unique `(identity, angle)`).
- Aggregate: `<work-dir>/node6_result.json` with a
  `ShotReferenceSummary` per shot (angle histogram, skip count).
- Return value: a JSON string containing the full `Node6Result` payload
  for chaining into downstream ComfyUI nodes.

## Status

Built. CLI + wrapper + ComfyUI adapter verified. See
[../../pipeline/README.md](../../pipeline/README.md) for the full flag
list.
