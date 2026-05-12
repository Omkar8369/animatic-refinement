"""Build an HTML thumbnail browser from a folder of TMKOC shot MP4s.

For each .mp4 in --input-dir:
  * extracts 3 frames (10%, 50%, 90% of duration)
  * downscales them to thumb_height (default 120 px tall)
  * composites them into a single horizontal strip PNG saved to
    <output-dir>/thumbs/<shot_name>.png

Then writes <output-dir>/browse.html — a self-contained HTML page with
all thumbnails in a grid, each with 4 checkboxes (TAPPU / JETHALAL /
OTHER / SKIP). State persists to the browser's localStorage. "Export
selections" downloads a JSON file mapping each character to a list of
shot IDs.

Run locally (no pod required). Uses imageio_ffmpeg's static ffmpeg
binary — no system ffmpeg install needed.

Example (Windows / Git Bash):

  PYTHON="/c/Users/Omkar Hajare/Desktop/download/ComfyUI_windows_portable/python_embeded/python.exe"
  "$PYTHON" tools/phase2e/build_thumbnails.py \\
    --input-dir "/c/Users/Omkar Hajare/Downloads/New folder (1)/New folder" \\
    --output-dir tools/phase2e/training_candidates/EP35

Resume-safe: skips shots whose thumb PNG already exists.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import imageio_ffmpeg
except ImportError:
    print("ERROR: imageio_ffmpeg not installed.", file=sys.stderr)
    print("  Install with: pip install imageio-ffmpeg", file=sys.stderr)
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow not installed.", file=sys.stderr)
    print("  Install with: pip install pillow", file=sys.stderr)
    sys.exit(1)


# ---------- ffmpeg helpers ----------
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def get_duration_seconds(mp4_path: Path, ffmpeg_exe: str) -> float | None:
    r = subprocess.run(
        [ffmpeg_exe, "-i", str(mp4_path)],
        capture_output=True,
        text=True,
    )
    m = _DURATION_RE.search(r.stderr)
    if not m:
        return None
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def extract_frame(
    mp4_path: Path,
    time_sec: float,
    out_path: Path,
    ffmpeg_exe: str,
    target_height: int,
) -> bool:
    """Seek to time_sec (fast seek before -i) and write 1 frame scaled to
    target_height as a JPG. Returns True on success."""
    r = subprocess.run(
        [
            ffmpeg_exe,
            "-y",
            "-loglevel", "error",
            "-ss", str(time_sec),
            "-i", str(mp4_path),
            "-frames:v", "1",
            "-vf", f"scale=-2:{target_height}",
            "-q:v", "3",
            str(out_path),
        ],
        capture_output=True,
    )
    return r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


def build_strip(frames: list[Path], target_height: int) -> Image.Image | None:
    """Stack frames horizontally into one image at target_height."""
    imgs = [Image.open(f).convert("RGB") for f in frames if f.exists()]
    if not imgs:
        return None
    resized = []
    for im in imgs:
        if im.height != target_height:
            ar = im.width / im.height
            new_w = max(1, int(target_height * ar))
            resized.append(im.resize((new_w, target_height), Image.LANCZOS))
        else:
            resized.append(im)
    total_w = sum(im.width for im in resized) + (len(resized) - 1) * 2  # 2px gap
    strip = Image.new("RGB", (total_w, target_height), (32, 32, 32))
    x = 0
    for i, im in enumerate(resized):
        strip.paste(im, (x, 0))
        x += im.width + 2
    return strip


# ---------- HTML template ----------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>__TITLE__</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         margin: 0; padding: 0; background: #1a1a1a; color: #ddd; }
  .controls { position: sticky; top: 0; background: #1a1a1a;
              padding: 12px 16px; z-index: 100; border-bottom: 1px solid #333;
              display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
  h1 { margin: 0; font-size: 14px; font-weight: 600; }
  button { background: #2a2a2a; color: #ddd; border: 1px solid #444;
           padding: 6px 14px; cursor: pointer; border-radius: 4px;
           font-size: 13px; font-family: inherit; }
  button:hover { background: #3a3a3a; border-color: #666; }
  button.primary { background: #2a5a2a; border-color: #4a8a4a; }
  button.primary:hover { background: #3a7a3a; }
  .counts { font-size: 12px; color: #999; font-family: monospace; }
  .counts strong { color: #ddd; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(440px, 1fr));
          gap: 10px; padding: 16px; }
  .tile { background: #222; border: 2px solid #333; border-radius: 6px;
          padding: 8px; transition: border-color 0.1s; }
  .tile.tappu { border-color: #4a8; }
  .tile.jeth { border-color: #48a; }
  .tile.other { border-color: #a84; }
  .tile.skip { opacity: 0.35; border-color: #555; }
  .shot-id { font-family: monospace; font-size: 12px; color: #aaa;
             margin-bottom: 4px; }
  .thumb { width: 100%; height: auto; display: block; border-radius: 3px;
           background: #111; }
  .checks { display: flex; gap: 4px; margin-top: 6px; font-size: 11px;
            flex-wrap: wrap; }
  .checks label { display: flex; align-items: center; gap: 4px;
                  cursor: pointer; padding: 2px 8px; border-radius: 3px;
                  background: #2a2a2a; user-select: none; }
  .checks label:hover { background: #3a3a3a; }
  .checks input { cursor: pointer; margin: 0; }
  .filter-row { display: flex; gap: 8px; align-items: center; }
  .filter-row label { font-size: 12px; color: #999; cursor: pointer; }
  details { background: #2a2a2a; padding: 4px 12px; border-radius: 4px;
            font-size: 12px; }
  details summary { cursor: pointer; }
</style>
</head>
<body>
<div class="controls">
  <h1>__TITLE__</h1>
  <button class="primary" onclick="exportSelections()">Export selections.json</button>
  <button onclick="clearAll()">Clear all</button>
  <div class="filter-row">
    <label><input type="checkbox" id="hideMarked" onchange="render()"> Hide marked</label>
    <label><input type="checkbox" id="hideSkip" onchange="render()" checked> Hide SKIP</label>
  </div>
  <div class="counts" id="counts"></div>
  <details>
    <summary>shortcuts</summary>
    Click a tile body (not the image) to toggle SKIP. Hover image to enlarge.
  </details>
</div>
<div class="grid" id="grid"></div>

<script>
const SHOTS = __SHOTS_JSON__;
const STORAGE_KEY = '__STORAGE_KEY__';

function loadState() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; }
  catch { return {}; }
}
function saveState(s) { localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); }
let state = loadState();

function tileClass(s) {
  let cls = 'tile';
  if (s.skip) return cls + ' skip';
  if (s.tappu) cls += ' tappu';
  else if (s.jethalal) cls += ' jeth';
  else if (s.other) cls += ' other';
  return cls;
}

function render() {
  const hideMarked = document.getElementById('hideMarked').checked;
  const hideSkip = document.getElementById('hideSkip').checked;
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  for (const shot of SHOTS) {
    const s = state[shot] || {};
    const marked = s.tappu || s.jethalal || s.other || s.skip;
    if (hideMarked && marked && !s.skip) continue;
    if (hideSkip && s.skip) continue;
    const tile = document.createElement('div');
    tile.className = tileClass(s);
    tile.innerHTML = `
      <div class="shot-id">${shot}</div>
      <img class="thumb" src="thumbs/${shot}.png" loading="lazy" alt="${shot}">
      <div class="checks">
        <label><input type="checkbox" data-key="tappu" ${s.tappu?'checked':''}> TAPPU</label>
        <label><input type="checkbox" data-key="jethalal" ${s.jethalal?'checked':''}> JETHALAL</label>
        <label><input type="checkbox" data-key="other" ${s.other?'checked':''}> OTHER</label>
        <label><input type="checkbox" data-key="skip" ${s.skip?'checked':''}> SKIP</label>
      </div>
    `;
    tile.querySelectorAll('input').forEach(cb => {
      cb.addEventListener('change', e => {
        const key = e.target.dataset.key;
        state[shot] = state[shot] || {};
        state[shot][key] = e.target.checked;
        saveState(state);
        tile.className = tileClass(state[shot]);
        updateCounts();
      });
    });
    grid.appendChild(tile);
  }
  updateCounts();
}

function updateCounts() {
  let tappu=0, jeth=0, other=0, skip=0, marked=0;
  for (const shot of SHOTS) {
    const s = state[shot] || {};
    if (s.tappu) tappu++;
    if (s.jethalal) jeth++;
    if (s.other) other++;
    if (s.skip) skip++;
    if (s.tappu || s.jethalal || s.other || s.skip) marked++;
  }
  document.getElementById('counts').innerHTML =
    `<strong>TAPPU</strong>: ${tappu} &nbsp; ` +
    `<strong>JETHALAL</strong>: ${jeth} &nbsp; ` +
    `<strong>OTHER</strong>: ${other} &nbsp; ` +
    `<strong>SKIP</strong>: ${skip} &nbsp; ` +
    `<strong>Marked</strong>: ${marked}/${SHOTS.length}`;
}

function exportSelections() {
  const out = { TAPPU: [], JETHALAL: [], OTHER: [], SKIP: [] };
  for (const shot of SHOTS) {
    const s = state[shot] || {};
    if (s.tappu) out.TAPPU.push(shot);
    if (s.jethalal) out.JETHALAL.push(shot);
    if (s.other) out.OTHER.push(shot);
    if (s.skip) out.SKIP.push(shot);
  }
  const blob = new Blob([JSON.stringify(out, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'selections.json'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function clearAll() {
  if (confirm('Clear ALL selections? This cannot be undone.')) {
    state = {};
    saveState(state);
    render();
  }
}

render();
</script>
</body>
</html>
"""


def build_html(title: str, shot_list: list[str], storage_key: str) -> str:
    return (
        HTML_TEMPLATE
        .replace("__TITLE__", title)
        .replace("__SHOTS_JSON__", json.dumps(shot_list))
        .replace("__STORAGE_KEY__", storage_key)
    )


# ---------- main ----------
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input-dir", required=True, help="folder containing .mp4 shots")
    parser.add_argument("--output-dir", required=True, help="where to write thumbs/ + browse.html")
    parser.add_argument("--thumb-height", type=int, default=120, help="strip height in px")
    parser.add_argument("--title", default=None, help="page title (default = output dir name)")
    parser.add_argument("--storage-key", default=None, help="localStorage key (default = derived from title)")
    args = parser.parse_args()

    in_dir = Path(args.input_dir)
    out_dir = Path(args.output_dir)
    if not in_dir.is_dir():
        print(f"ERROR: --input-dir not found: {in_dir}", file=sys.stderr)
        sys.exit(2)

    thumbs_dir = out_dir / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    title = args.title or f"{out_dir.name} curation"
    storage_key = args.storage_key or f"phase2e_{out_dir.name}_selections"

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    print(f"ffmpeg: {ffmpeg_exe}")
    print(f"input dir: {in_dir}")
    print(f"output dir: {out_dir}")

    mp4_files = sorted(in_dir.glob("*.mp4"))
    print(f"found {len(mp4_files)} mp4 file(s)\n")
    if not mp4_files:
        print("nothing to do.")
        return

    shot_list = []
    skipped = 0
    failed = 0
    for i, mp4 in enumerate(mp4_files, start=1):
        shot_name = mp4.stem
        thumb_path = thumbs_dir / f"{shot_name}.png"

        if thumb_path.exists() and thumb_path.stat().st_size > 0:
            print(f"[{i}/{len(mp4_files)}] {shot_name}: thumb exists, skip")
            shot_list.append(shot_name)
            skipped += 1
            continue

        duration = get_duration_seconds(mp4, ffmpeg_exe)
        if duration is None or duration < 0.3:
            print(f"[{i}/{len(mp4_files)}] {shot_name}: bad duration ({duration}), skip")
            failed += 1
            continue

        # 3 timestamps spread across the shot.
        if duration < 1.0:
            timestamps = [duration * 0.5]
        elif duration < 3.0:
            timestamps = [duration * 0.2, duration * 0.5, duration * 0.8]
        else:
            timestamps = [duration * 0.1, duration * 0.5, duration * 0.9]

        tmp_frames = []
        for j, ts in enumerate(timestamps):
            tmp = thumbs_dir / f".{shot_name}_tmp{j}.jpg"
            if extract_frame(mp4, ts, tmp, ffmpeg_exe, args.thumb_height):
                tmp_frames.append(tmp)

        if not tmp_frames:
            print(f"[{i}/{len(mp4_files)}] {shot_name}: no frames extracted, skip")
            failed += 1
            continue

        strip = build_strip(tmp_frames, args.thumb_height)
        if strip is None:
            print(f"[{i}/{len(mp4_files)}] {shot_name}: strip build failed, skip")
            for f in tmp_frames:
                f.unlink(missing_ok=True)
            failed += 1
            continue

        strip.save(thumb_path, optimize=True)
        for f in tmp_frames:
            f.unlink(missing_ok=True)
        shot_list.append(shot_name)
        print(f"[{i}/{len(mp4_files)}] {shot_name}: ok ({strip.size[0]}x{strip.size[1]})")

    html = build_html(title, shot_list, storage_key)
    html_path = out_dir / "browse.html"
    html_path.write_text(html, encoding="utf-8")

    print()
    print(f"Done. {len(shot_list)} thumbs ready ({skipped} pre-existing, {failed} failed).")
    print(f"Open in browser: {html_path}")
    print()
    print(f"Browser state persists to localStorage key: {storage_key!r}")
    print(f"When done, click 'Export selections.json' and save next to browse.html.")


if __name__ == "__main__":
    main()
