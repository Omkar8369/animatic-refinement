"""Manual bbox crop tool. Two modes:

1. BUILD HTML (default): scans frame folders + generates a self-contained
   HTML page where you click-drag a rectangle over each target character,
   then exports a manual_bboxes.json file.

2. APPLY (--apply): reads manual_bboxes.json + crops each frame to the
   user-drawn bbox (with optional padding), saves into cropped/<char>/.

Keyboard shortcuts in the HTML tool:
    Mouse drag      Draw bounding box
    Enter / Space   Save bbox & next frame
    S               Skip current frame (character not present)
    A / Left arrow  Go back to previous frame
    E               Export manual_bboxes.json
    R               Reset (clear current bbox)

State auto-saves to localStorage, so closing/reopening keeps progress.

Example usage:

  PYTHON="/c/Users/Omkar Hajare/Desktop/download/ComfyUI_windows_portable/python_embeded/python.exe"

  # 1. Build the HTML tool
  "$PYTHON" tools/phase2e/manual_crop.py

  # 2. Open manual_crop.html in browser, draw bboxes, click "Export"
  # 3. Move the downloaded manual_bboxes.json next to the HTML

  # 4. Apply crops
  "$PYTHON" tools/phase2e/manual_crop.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------- config ----------
SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_ROOT_DEFAULT = SCRIPT_DIR / "training_candidates" / "EP35"
CHARACTERS_DEFAULT = ["TAPPU", "JETHALAL"]
PADDING_RATIO = 0.10  # 10% padding around drawn bbox


# ---------- HTML template ----------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>__TITLE__</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif;
         margin: 0; padding: 0; background: #1a1a1a; color: #ddd;
         display: flex; flex-direction: column; height: 100vh;
         overflow: hidden; }
  header { background: #2a2a2a; padding: 8px 16px;
           border-bottom: 1px solid #444; flex-shrink: 0;
           display: flex; align-items: center; gap: 16px;
           flex-wrap: wrap; }
  h1 { margin: 0; font-size: 14px; font-weight: 600; }
  .progress { font-family: monospace; font-size: 13px; color: #ccc; }
  .character { font-weight: 900; font-family: monospace; padding: 6px 14px;
               border-radius: 4px; font-size: 18px;
               letter-spacing: 1px; text-transform: uppercase; }
  .character.TAPPU { background: #2a5a2a; color: #cfc;
                     border: 2px solid #4a8a4a; }
  .character.JETHALAL { background: #2a4a8a; color: #cce;
                        border: 2px solid #4a6aaa; }
  .character::before { content: "→ CROP "; }
  button { background: #333; color: #ddd; border: 1px solid #555;
           padding: 5px 12px; border-radius: 4px; cursor: pointer;
           font-family: inherit; font-size: 12px; }
  button:hover { background: #444; }
  button.primary { background: #2a5a2a; border-color: #4a8a4a; }
  button.danger { background: #5a2a2a; border-color: #8a4a4a; }
  .shortcut { color: #888; font-size: 11px; font-family: monospace; }
  .filename { font-family: monospace; font-size: 11px; color: #888;
              flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; }
  main { flex: 1; display: flex; align-items: center; justify-content: center;
         padding: 16px; overflow: hidden; position: relative;
         background: #0d0d0d; }
  #stage { position: relative; max-width: 100%; max-height: 100%;
           cursor: crosshair; user-select: none; }
  #stage img { display: block; max-width: 100%; max-height: calc(100vh - 110px);
               width: auto; height: auto; pointer-events: none; }
  #bbox { position: absolute; border: 2px solid #ffa500;
          background: rgba(255, 165, 0, 0.15); pointer-events: none;
          display: none; }
  #bbox.confirmed { border-color: #4a8; background: rgba(74, 168, 74, 0.15); }
  .empty-state { color: #666; font-size: 14px; }
  footer { background: #222; padding: 8px 16px; border-top: 1px solid #333;
           font-size: 11px; color: #888; flex-shrink: 0;
           display: flex; gap: 16px; flex-wrap: wrap; }
  footer kbd { background: #333; padding: 2px 6px; border-radius: 3px;
               font-family: monospace; color: #cfc; font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="character" id="charLabel">—</span>
  <span class="progress" id="progress">0 / 0</span>
  <span class="filename" id="filename"></span>
  <button class="primary" onclick="saveNext()">Save & Next <kbd>↵</kbd></button>
  <button onclick="skipCurrent()">Skip <kbd>S</kbd></button>
  <button onclick="goBack()">Back <kbd>A</kbd></button>
  <button onclick="resetBbox()">Reset <kbd>R</kbd></button>
  <button class="danger" onclick="exportJson()">Export <kbd>E</kbd></button>
</header>
<main>
  <div id="stage">
    <img id="frame" alt="">
    <div id="bbox"></div>
  </div>
  <div class="empty-state" id="emptyState" style="display:none">All done! Click Export.</div>
</main>
<footer>
  <span><kbd>drag</kbd> draw bbox</span>
  <span><kbd>Enter</kbd>/<kbd>Space</kbd> save & next</span>
  <span><kbd>S</kbd> skip</span>
  <span><kbd>A</kbd>/<kbd>←</kbd> back</span>
  <span><kbd>R</kbd> reset</span>
  <span><kbd>E</kbd> export</span>
  <span style="color:#cfc">state auto-saves to localStorage</span>
</footer>

<script>
const ITEMS = __ITEMS_JSON__;
const STORAGE_KEY = '__STORAGE_KEY__';

let idx = 0;
let bboxes = loadState();  // path -> {x, y, w, h, skip}
let drawing = false;
let dragStart = null;
let currentBbox = null;  // {x, y, w, h} in DISPLAYED pixels
let img = null;

function loadState() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; }
  catch { return {}; }
}
function saveState() { localStorage.setItem(STORAGE_KEY, JSON.stringify(bboxes)); }

function setIndex(i) {
  if (i < 0 || i > ITEMS.length) return;
  idx = i;
  if (idx >= ITEMS.length) {
    document.getElementById('stage').style.display = 'none';
    document.getElementById('emptyState').style.display = 'block';
    updateProgress();
    return;
  }
  document.getElementById('stage').style.display = '';
  document.getElementById('emptyState').style.display = 'none';
  const item = ITEMS[idx];
  document.getElementById('charLabel').textContent = item.character;
  document.getElementById('charLabel').className = 'character ' + item.character;
  document.getElementById('filename').textContent = item.path;
  const imgEl = document.getElementById('frame');
  imgEl.onload = function() {
    img = { naturalW: imgEl.naturalWidth, naturalH: imgEl.naturalHeight };
    redrawSaved();
  };
  imgEl.src = item.path;
  updateProgress();
}

function updateProgress() {
  const total = ITEMS.length;
  let done = 0, skipped = 0, bboxed = 0;
  for (const item of ITEMS) {
    const b = bboxes[item.path];
    if (b) {
      done++;
      if (b.skip) skipped++; else bboxed++;
    }
  }
  document.getElementById('progress').textContent =
    `${idx + 1} / ${total}  |  done: ${done} (bbox: ${bboxed}, skipped: ${skipped})`;
}

function redrawSaved() {
  const item = ITEMS[idx];
  const b = bboxes[item.path];
  const bboxEl = document.getElementById('bbox');
  if (!b || b.skip) {
    bboxEl.style.display = 'none';
    currentBbox = null;
    return;
  }
  // Saved bbox is in IMAGE-NATIVE pixels. Convert to displayed pixels.
  const imgEl = document.getElementById('frame');
  const dispW = imgEl.clientWidth, dispH = imgEl.clientHeight;
  const sx = dispW / img.naturalW, sy = dispH / img.naturalH;
  bboxEl.style.left = (b.x * sx) + 'px';
  bboxEl.style.top  = (b.y * sy) + 'px';
  bboxEl.style.width  = (b.w * sx) + 'px';
  bboxEl.style.height = (b.h * sy) + 'px';
  bboxEl.classList.add('confirmed');
  bboxEl.style.display = 'block';
  currentBbox = null;  // user can re-draw to overwrite
}

function getStageRelative(e) {
  const imgEl = document.getElementById('frame');
  const rect = imgEl.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top,
           w: rect.width, h: rect.height };
}

const stage = document.getElementById('stage');
stage.addEventListener('mousedown', e => {
  if (idx >= ITEMS.length) return;
  drawing = true;
  const p = getStageRelative(e);
  dragStart = { x: p.x, y: p.y };
  const bboxEl = document.getElementById('bbox');
  bboxEl.style.left = p.x + 'px';
  bboxEl.style.top = p.y + 'px';
  bboxEl.style.width = '0px';
  bboxEl.style.height = '0px';
  bboxEl.classList.remove('confirmed');
  bboxEl.style.display = 'block';
});

stage.addEventListener('mousemove', e => {
  if (!drawing) return;
  const p = getStageRelative(e);
  const x = Math.min(dragStart.x, p.x);
  const y = Math.min(dragStart.y, p.y);
  const w = Math.abs(p.x - dragStart.x);
  const h = Math.abs(p.y - dragStart.y);
  const bboxEl = document.getElementById('bbox');
  bboxEl.style.left = x + 'px';
  bboxEl.style.top = y + 'px';
  bboxEl.style.width = w + 'px';
  bboxEl.style.height = h + 'px';
  currentBbox = { x, y, w, h, dispW: p.w, dispH: p.h };
});

stage.addEventListener('mouseup', () => { drawing = false; });
stage.addEventListener('mouseleave', () => { drawing = false; });

function saveNext() {
  if (idx >= ITEMS.length) return;
  const item = ITEMS[idx];
  if (currentBbox && currentBbox.w > 5 && currentBbox.h > 5) {
    // Convert displayed pixels -> image-native pixels.
    const sx = img.naturalW / currentBbox.dispW;
    const sy = img.naturalH / currentBbox.dispH;
    bboxes[item.path] = {
      x: Math.round(currentBbox.x * sx),
      y: Math.round(currentBbox.y * sy),
      w: Math.round(currentBbox.w * sx),
      h: Math.round(currentBbox.h * sy),
      skip: false,
    };
    saveState();
  }
  setIndex(idx + 1);
}

function skipCurrent() {
  if (idx >= ITEMS.length) return;
  const item = ITEMS[idx];
  bboxes[item.path] = { skip: true };
  saveState();
  setIndex(idx + 1);
}

function goBack() { setIndex(Math.max(0, idx - 1)); }

function resetBbox() {
  if (idx >= ITEMS.length) return;
  const item = ITEMS[idx];
  delete bboxes[item.path];
  saveState();
  currentBbox = null;
  document.getElementById('bbox').style.display = 'none';
  updateProgress();
}

function exportJson() {
  const blob = new Blob([JSON.stringify(bboxes, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'manual_bboxes.json'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

document.addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); saveNext(); }
  else if (e.key === 's' || e.key === 'S') { skipCurrent(); }
  else if (e.key === 'a' || e.key === 'A' || e.key === 'ArrowLeft') { goBack(); }
  else if (e.key === 'r' || e.key === 'R') { resetBbox(); }
  else if (e.key === 'e' || e.key === 'E') { exportJson(); }
});

// Resume at the first un-bboxed image, if any.
function firstUnannotated() {
  for (let i = 0; i < ITEMS.length; i++) {
    if (!bboxes[ITEMS[i].path]) return i;
  }
  return 0;
}
setIndex(firstUnannotated());
window.addEventListener('resize', () => redrawSaved());
</script>
</body>
</html>
"""


def build_html(input_root: Path, characters: list[str]) -> tuple[str, list[dict]]:
    """Build the manual_crop.html content + return the items list."""
    items = []
    for char in characters:
        char_dir = input_root / char
        if not char_dir.is_dir():
            print(f"  WARNING: {char_dir} not found, skipping")
            continue
        for jpg in sorted(char_dir.glob("*.jpg")):
            # Path relative to where manual_crop.html lives.
            rel = f"{char}/{jpg.name}"
            items.append({"character": char, "path": rel, "filename": jpg.name})

    storage_key = "phase2e_manual_crop_" + "_".join(characters)
    html = (
        HTML_TEMPLATE
        .replace("__TITLE__", "Phase 2e — manual bbox cropping")
        .replace("__ITEMS_JSON__", json.dumps(items))
        .replace("__STORAGE_KEY__", storage_key)
    )
    return html, items


def cmd_build(args):
    input_root = Path(args.input_root)
    if not input_root.is_dir():
        print(f"ERROR: input root not found: {input_root}", file=sys.stderr)
        sys.exit(2)

    html, items = build_html(input_root, args.characters)
    html_path = input_root / "manual_crop.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"Wrote: {html_path}")
    print(f"  {len(items)} total frames to crop")
    for ch in args.characters:
        n = sum(1 for it in items if it["character"] == ch)
        print(f"    {ch}: {n}")
    print()
    print("Next steps:")
    print(f"  1. Open in browser: {html_path}")
    print(f"  2. Drag a rectangle over each target character")
    print(f"     Enter/Space = save & next, S = skip, A = back, E = export")
    print(f"  3. Click Export at the end -> downloads manual_bboxes.json")
    print(f"  4. Move manual_bboxes.json into {input_root}")
    print(f"  5. Apply: python manual_crop.py --apply")


def cmd_apply(args):
    try:
        from PIL import Image
    except ImportError:
        print("ERROR: Pillow not installed.", file=sys.stderr)
        sys.exit(1)

    input_root = Path(args.input_root)
    bboxes_path = input_root / "manual_bboxes.json"
    if not bboxes_path.is_file():
        print(f"ERROR: bboxes file not found: {bboxes_path}", file=sys.stderr)
        print("  Did you click Export in the HTML tool and move the file here?")
        sys.exit(2)

    bboxes = json.loads(bboxes_path.read_text())
    output_root = input_root / "cropped_manual"
    output_root.mkdir(parents=True, exist_ok=True)

    counts = {}
    skipped = {}
    missing = []
    for rel_path, bbox_data in bboxes.items():
        source_char = rel_path.split("/")[0]
        # If --force-character is set, ALL crops go to that folder regardless
        # of source. Useful when the bbox-drawing pass didn't follow header
        # labels (e.g., user drew TAPPU bboxes on both TAPPU/ and JETHALAL/
        # source frames).
        char = args.force_character or source_char
        if bbox_data.get("skip"):
            skipped[char] = skipped.get(char, 0) + 1
            continue
        src = input_root / rel_path
        if not src.is_file():
            missing.append(rel_path)
            continue
        try:
            img = Image.open(src).convert("RGB")
        except Exception as e:
            print(f"  WARN {rel_path}: open failed ({e})")
            continue

        x = int(bbox_data["x"])
        y = int(bbox_data["y"])
        w = int(bbox_data["w"])
        h = int(bbox_data["h"])
        pad_w = int(w * args.padding)
        pad_h = int(h * args.padding)
        x1 = max(0, x - pad_w)
        y1 = max(0, y - pad_h)
        x2 = min(img.width, x + w + pad_w)
        y2 = min(img.height, y + h + pad_h)
        crop = img.crop((x1, y1, x2, y2))

        out_dir = output_root / char
        out_dir.mkdir(parents=True, exist_ok=True)
        # Prefix source-folder letter to avoid collisions when force-character
        # pulls from multiple source folders (e.g., TAPPU/ + JETHALAL/ both
        # remap to TAPPU output).
        basename = Path(rel_path).name
        if args.force_character and source_char != args.force_character:
            out_name = f"{source_char[0].lower()}_{basename}"
        else:
            out_name = basename
        out_path = out_dir / out_name
        crop.save(out_path, format="JPEG", quality=92)
        counts[char] = counts.get(char, 0) + 1

    print()
    print(f"=== Manual crop complete ===")
    print(f"Output: {output_root}")
    for char, n in sorted(counts.items()):
        s = skipped.get(char, 0)
        print(f"  {char}: {n} cropped, {s} skipped")
    if missing:
        print(f"  Missing source files ({len(missing)}):")
        for m in missing[:5]:
            print(f"    - {m}")
        if len(missing) > 5:
            print(f"    ... and {len(missing) - 5} more")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="apply manual_bboxes.json to crop images (default mode: build HTML)")
    parser.add_argument("--input-root", default=str(INPUT_ROOT_DEFAULT))
    parser.add_argument("--characters", nargs="+", default=CHARACTERS_DEFAULT)
    parser.add_argument("--padding", type=float, default=PADDING_RATIO,
                        help="padding ratio around drawn bbox (default 0.10)")
    parser.add_argument("--force-character", default=None,
                        help="apply mode only: force all crops into this character "
                             "folder regardless of which folder the source was in. "
                             "Use when you bbox'd one character throughout, ignoring "
                             "the per-frame header label.")
    args = parser.parse_args()

    if args.apply:
        cmd_apply(args)
    else:
        cmd_build(args)


if __name__ == "__main__":
    main()
