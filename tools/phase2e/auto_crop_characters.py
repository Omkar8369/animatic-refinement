"""Auto-crop each training frame to JUST the target character using YOLO.

Problem this fixes: the training frames for TAPPU and JETHALAL are full
1280x720 episode shots that often show MULTIPLE characters. With identical
captions, the LoRA learned all-characters-as-one-identity, producing
contaminated outputs (e.g., TAPPU samples wearing JETHALAL's yellow polo).

Fix: detect all 'person' bboxes per frame via YOLOv8, then heuristically
pick the target character per folder:
    TAPPU/    -> pick SMALLEST detected person bbox (kid heuristic)
    JETHALAL/ -> pick LARGEST detected person bbox (adult heuristic)
Crop tight (with 15% padding), save into cropped/<character>/<name>.jpg.

Then writes review.html — a thumbnail grid showing each (original | crop)
side-by-side so you can visually flag wrong crops. Use Windows Explorer
to delete bad crops from cropped/<character>/ before retraining.

Run locally (no pod needed). First run downloads YOLOv8m weights (~50MB).

Example (Windows / Git Bash):

  PYTHON="/c/Users/Omkar Hajare/Desktop/download/ComfyUI_windows_portable/python_embeded/python.exe"
  "$PYTHON" -m pip install ultralytics
  "$PYTHON" tools/phase2e/auto_crop_characters.py
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from pathlib import Path

try:
    from ultralytics import YOLO
except ImportError:
    print("ERROR: ultralytics not installed.", file=sys.stderr)
    print("  Install with: <python.exe> -m pip install ultralytics", file=sys.stderr)
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow not installed.", file=sys.stderr)
    sys.exit(1)


# ---------- config ----------
SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_ROOT_DEFAULT = SCRIPT_DIR / "training_candidates" / "EP35"
PADDING_RATIO = 0.15
MIN_BBOX_HEIGHT_RATIO = 0.20  # bbox must be at least 20% of frame height to count
YOLO_MODEL_NAME = "yolov8m.pt"   # 50 MB; balance of speed + accuracy on cartoons
YOLO_CONF_THRESHOLD = 0.20       # low because TMKOC cartoons are not COCO-photo style
PERSON_CLASS = 0                  # COCO 'person' class

# Per-character selection strategy.
# TAPPU is a kid, shorter than adults → smallest bbox usually = TAPPU.
# JETHALAL is an adult man → largest bbox usually = him.
STRATEGY = {
    "TAPPU": "smallest",
    "JETHALAL": "largest",
}

# Thumbnail size in the review HTML.
THUMB_HEIGHT = 200


# ---------- detection + cropping ----------
def detect_persons(model, image_path: Path) -> list[tuple[float, float, float, float, float]]:
    """Return list of (x1, y1, x2, y2, conf) for all detected persons."""
    results = model(
        str(image_path),
        classes=[PERSON_CLASS],
        conf=YOLO_CONF_THRESHOLD,
        verbose=False,
    )
    boxes = []
    for r in results:
        for box in r.boxes:
            xyxy = box.xyxy[0].cpu().numpy().tolist()
            conf = float(box.conf[0].cpu().numpy())
            x1, y1, x2, y2 = xyxy
            boxes.append((x1, y1, x2, y2, conf))
    return boxes


def select_bbox(boxes, strategy: str, img_height: int):
    """Filter by min height, then pick based on strategy."""
    h_min = img_height * MIN_BBOX_HEIGHT_RATIO
    qualifying = [b for b in boxes if (b[3] - b[1]) >= h_min]
    if not qualifying:
        return None
    if strategy == "smallest":
        return min(qualifying, key=lambda b: b[3] - b[1])
    elif strategy == "largest":
        return max(qualifying, key=lambda b: b[3] - b[1])
    elif strategy == "highest_confidence":
        return max(qualifying, key=lambda b: b[4])
    else:
        return qualifying[0]


def crop_with_padding(img: Image.Image, bbox, padding: float = PADDING_RATIO) -> Image.Image:
    x1, y1, x2, y2, _conf = bbox
    w, h = x2 - x1, y2 - y1
    pad_w = w * padding
    pad_h = h * padding
    x1 = max(0, x1 - pad_w)
    y1 = max(0, y1 - pad_h)
    x2 = min(img.width, x2 + pad_w)
    y2 = min(img.height, y2 + pad_h)
    return img.crop((int(x1), int(y1), int(x2), int(y2)))


def thumb_jpg_b64(image: Image.Image, height: int = THUMB_HEIGHT) -> str:
    """Resize image to target height (maintain aspect), return base64 JPG."""
    ar = image.width / image.height
    w = max(1, int(height * ar))
    thumb = image.resize((w, height), Image.LANCZOS)
    buf = io.BytesIO()
    thumb.convert("RGB").save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------- HTML review tool ----------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>__TITLE__</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0;
         padding: 0; background: #1a1a1a; color: #ddd; }
  .controls { position: sticky; top: 0; background: #1a1a1a;
              padding: 12px 16px; border-bottom: 1px solid #333;
              z-index: 100; display: flex; align-items: center; gap: 16px; }
  h1 { margin: 0; font-size: 14px; }
  .counts { font-family: monospace; color: #999; font-size: 12px; }
  .instructions { font-size: 12px; color: #ccc; padding: 12px 16px;
                  background: #222; }
  .instructions code { background: #333; padding: 2px 6px; border-radius: 3px;
                        font-family: monospace; color: #cfc; }
  .grid { padding: 16px; display: grid;
          grid-template-columns: repeat(auto-fill, minmax(640px, 1fr));
          gap: 12px; }
  .tile { background: #222; border: 2px solid #333; border-radius: 6px;
          padding: 8px; }
  .tile.bad { border-color: #c44; opacity: 0.5; }
  .tile.skipped { border-color: #777; opacity: 0.4; }
  .pair { display: flex; gap: 6px; align-items: flex-start; }
  .pair img { display: block; height: __THUMB__px; width: auto; }
  .pair .label { font-size: 10px; color: #888; font-family: monospace;
                 margin-bottom: 2px; }
  .meta { font-family: monospace; font-size: 11px; color: #aaa;
          margin-top: 4px; word-break: break-all; }
  .meta .name { color: #ddd; }
</style>
</head>
<body>
<div class="controls">
  <h1>__TITLE__</h1>
  <span class="counts" id="counts"></span>
</div>
<div class="instructions">
  Quick review: each row shows <strong>(original on left | YOLO crop on right)</strong>.
  If a crop is wrong (wrong character picked, character cut off, etc.), open
  Windows Explorer at <code>__OUTPUT_PATH__</code> and DELETE that .jpg file.
  Then refresh this page. <strong>SKIPPED</strong> tiles had no person detected and are not
  in the cropped/ folder — they're shown for completeness only.
</div>
<div class="grid" id="grid"></div>

<script>
const ITEMS = __ITEMS_JSON__;

function render() {
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  let ok = 0, skipped = 0;
  for (const item of ITEMS) {
    const tile = document.createElement('div');
    tile.className = 'tile';
    if (item.skipped) tile.classList.add('skipped');
    let cropHtml;
    if (item.skipped) {
      cropHtml = `<div><div class="label">(skipped - no person ≥ ${item.min_h_pct}%)</div></div>`;
      skipped++;
    } else {
      cropHtml = `<div><div class="label">crop</div><img src="data:image/jpeg;base64,${item.crop_b64}"></div>`;
      ok++;
    }
    tile.innerHTML = `
      <div class="pair">
        <div><div class="label">${item.name}</div><img src="data:image/jpeg;base64,${item.orig_b64}"></div>
        ${cropHtml}
      </div>
      <div class="meta">
        <span class="name">${item.character}</span>
        ${item.bbox ? ` | bbox=${item.bbox} | conf=${item.conf.toFixed(2)} | size=${item.size}` : ''}
      </div>
    `;
    grid.appendChild(tile);
  }
  document.getElementById('counts').innerHTML =
    `<strong>${ok}</strong> cropped &nbsp; <strong>${skipped}</strong> skipped &nbsp; (total ${ITEMS.length})`;
}
render();
</script>
</body>
</html>
"""


def build_html(title: str, items: list[dict], output_root: Path) -> str:
    items_json = json.dumps(items)
    return (
        HTML_TEMPLATE
        .replace("__TITLE__", title)
        .replace("__THUMB__", str(THUMB_HEIGHT))
        .replace("__OUTPUT_PATH__", str(output_root).replace("\\", "\\\\"))
        .replace("__ITEMS_JSON__", items_json)
    )


# ---------- main ----------
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input-root", default=str(INPUT_ROOT_DEFAULT),
                        help="folder containing TAPPU/ and JETHALAL/ subdirs of raw frames")
    parser.add_argument("--characters", nargs="+", default=["TAPPU", "JETHALAL"],
                        help="character folder names to process")
    parser.add_argument("--padding", type=float, default=PADDING_RATIO,
                        help="bbox padding ratio (default 0.15 = 15%%)")
    parser.add_argument("--min-h-ratio", type=float, default=MIN_BBOX_HEIGHT_RATIO,
                        help="min bbox height as fraction of frame height")
    parser.add_argument("--model", default=YOLO_MODEL_NAME,
                        help="YOLO model file (auto-downloads on first run)")
    args = parser.parse_args()

    input_root = Path(args.input_root)
    output_root = input_root / "cropped"

    if not input_root.is_dir():
        print(f"ERROR: input root not found: {input_root}", file=sys.stderr)
        sys.exit(2)

    print(f"Loading YOLO model: {args.model}")
    model = YOLO(args.model)
    print(f"  device: {model.device}")
    print()

    for character in args.characters:
        in_dir = input_root / character
        out_dir = output_root / character
        out_dir.mkdir(parents=True, exist_ok=True)
        if not in_dir.is_dir():
            print(f"=== {character}: input dir not found ({in_dir}), skipping ===")
            continue
        strategy = STRATEGY.get(character)
        if not strategy:
            print(f"=== {character}: no strategy defined, skipping ===")
            continue

        jpgs = sorted(in_dir.glob("*.jpg"))
        print(f"=== {character} ({strategy}) — {len(jpgs)} frames ===")

        items_for_html = []
        ok = 0
        skipped = 0
        for i, jpg in enumerate(jpgs, start=1):
            try:
                img = Image.open(jpg).convert("RGB")
            except Exception as e:
                print(f"  [{i}/{len(jpgs)}] {jpg.name}: open failed: {e}")
                continue

            boxes = detect_persons(model, jpg)
            chosen = select_bbox(boxes, strategy, img.height) if boxes else None

            if chosen is None:
                skipped += 1
                items_for_html.append({
                    "name": jpg.name,
                    "character": character,
                    "orig_b64": thumb_jpg_b64(img),
                    "skipped": True,
                    "min_h_pct": int(args.min_h_ratio * 100),
                })
                if i % 20 == 0 or i == len(jpgs):
                    print(f"  [{i}/{len(jpgs)}] ok={ok} skipped={skipped}")
                continue

            x1, y1, x2, y2, conf = chosen
            crop = crop_with_padding(img, chosen, padding=args.padding)
            out_path = out_dir / jpg.name
            crop.save(out_path, format="JPEG", quality=92)
            ok += 1
            items_for_html.append({
                "name": jpg.name,
                "character": character,
                "orig_b64": thumb_jpg_b64(img),
                "crop_b64": thumb_jpg_b64(crop),
                "skipped": False,
                "bbox": [int(x1), int(y1), int(x2), int(y2)],
                "conf": conf,
                "size": f"{crop.width}x{crop.height}",
            })
            if i % 20 == 0 or i == len(jpgs):
                print(f"  [{i}/{len(jpgs)}] ok={ok} skipped={skipped}")

        # Per-character HTML review.
        html_path = output_root / f"review_{character}.html"
        html_path.write_text(
            build_html(f"{character} crops — review", items_for_html, out_dir),
            encoding="utf-8",
        )
        print(f"  {character}: {ok} cropped, {skipped} skipped (no person detected ≥ {int(args.min_h_ratio*100)}% height)")
        print(f"  review HTML: {html_path}")
        print(f"  cropped dir: {out_dir}")
        print()

    print("All done.")
    print()
    print("Review each crop in the HTML files. To delete bad crops:")
    print(f"  open {output_root} in Windows Explorer (Extra Large Icons view)")
    print("  delete any .jpg where the wrong character was cropped, or character is cut off")
    print()
    print("When done, you'll be ready to retrain with clean per-character data.")


if __name__ == "__main__":
    main()
