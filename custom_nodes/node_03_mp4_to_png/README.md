# Node 3 — Shot Pre-processing (MP4 → PNG Sequence)

Thin ComfyUI wrapper around `pipeline/node3.py`. Exposes one node:

**Animatic - Node 3: MP4 → PNG frames** (`AnimaticNode3Mp4ToPng`)

| Input | Type | Meaning |
|---|---|---|
| `queue_json_path` | STRING | Absolute path to `queue.json` produced by Node 2. |
| `work_dir` | STRING | Folder where per-shot frame folders + `node3_result.json` will be written. |

| Output | Type | Meaning |
|---|---|---|
| `node3_result_json` | STRING | JSON-serialized `Node3Result` (same shape as `node3_result.json` on disk). |

## Behavior

Iterates every shot in `queue.json`, decodes its MP4 with ffmpeg (bundled via
`imageio-ffmpeg`, no PATH dependency), writes:

```
<work_dir>/<shotId>/
    frame_0001.png
    frame_0002.png
    ...
    _manifest.json
<work_dir>/node3_result.json
```

- **Fail-fast** on queue/ffmpeg/disk errors (raises; ComfyUI surfaces the Python traceback).
- **Warn-and-continue** on frame-count drift vs. `metadata.json`'s `durationFrames`. Warnings appear in the returned JSON's `warnings` array.

## Why a thin wrapper

All logic (argparse, manifest shape, error types) lives in `pipeline/node3.py` so
the exact same code path is hit whether Node 3 runs from CLI, tests, CI, or this
ComfyUI node. Don't add business logic here — add it upstream in `pipeline/`.

## See also

- `pipeline/node3.py` — core implementation
- `pipeline/cli_node3.py` — CLI entry point
- `docs/PLAN.md` Node 3 section — locked decisions + sub-steps
