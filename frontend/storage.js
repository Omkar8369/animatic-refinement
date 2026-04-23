// Animatic Refinement - Node 1 frontend
// Shared utilities: localStorage character library, file helpers, validation.
//
// Locked conventions (see ../docs/PLAN.md and ../CLAUDE.md):
//   - 25 FPS fixed
//   - Position codes: L / CL / C / CR / R
//   - Character sheet = 8-angle horizontal strip (sliced in Node 6, not here)
//
// This module is loaded by characters.html and index.html as a plain <script>.

const FPS = 25;

const POSITIONS = [
  { code: 'L',  label: 'Left' },
  { code: 'CL', label: 'Center-Left' },
  { code: 'C',  label: 'Center (exact)' },
  { code: 'CR', label: 'Center-Right' },
  { code: 'R',  label: 'Right' },
];

const STORAGE_KEY = 'animaticRefinement.characters.v1';
const SHOTS_KEY   = 'animaticRefinement.shots.v1';
const PROJECT_KEY = 'animaticRefinement.project.v1';

// ----- Character library (localStorage) -------------------------------------

function loadCharacters() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (e) {
    console.warn('Failed to load character library:', e);
    return [];
  }
}

function saveCharacters(list) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
}

function addCharacter(character) {
  const list = loadCharacters();
  // Replace existing by name (case-insensitive) so re-uploading a sheet just updates it.
  const existingIdx = list.findIndex(c => c.name.toLowerCase() === character.name.toLowerCase());
  if (existingIdx >= 0) list[existingIdx] = character;
  else list.push(character);
  saveCharacters(list);
  return list;
}

function removeCharacter(name) {
  const list = loadCharacters().filter(c => c.name !== name);
  saveCharacters(list);
  return list;
}

function clearCharacters() {
  localStorage.removeItem(STORAGE_KEY);
}

// ----- Shot list draft (localStorage) so the user doesn't lose work --------

function loadShotsDraft() {
  try {
    const raw = localStorage.getItem(SHOTS_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (e) {
    return null;
  }
}

function saveShotsDraft(state) {
  localStorage.setItem(SHOTS_KEY, JSON.stringify(state));
}

function clearShotsDraft() {
  localStorage.removeItem(SHOTS_KEY);
}

function loadProjectDraft() {
  try {
    const raw = localStorage.getItem(PROJECT_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (e) {
    return null;
  }
}

function saveProjectDraft(state) {
  localStorage.setItem(PROJECT_KEY, JSON.stringify(state));
}

// ----- File helpers ---------------------------------------------------------

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('Image failed to load'));
    img.src = src;
  });
}

// Convert "Bhim Singh" -> "bhim_singh_sheet.png"
function canonicalSheetFilename(name) {
  const slug = String(name)
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return `${slug || 'character'}_sheet.png`;
}

// Trigger a browser download of arbitrary text content.
function downloadText(filename, text, mime = 'application/json') {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

// Trigger a browser download of a DataURL (for PNG sheets).
function downloadDataURL(filename, dataUrl) {
  const a = document.createElement('a');
  a.href = dataUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ----- Sheet quality sanity check ------------------------------------------
// Loose check that the uploaded sheet "looks like" an 8-angle horizontal strip.
// Real slicing happens in Node 6; this is just an early-warning preview.
//
// Heuristic:
//   1. Aspect ratio width/height should be > ~3 (a strip, not a portrait).
//   2. Count the number of horizontal "non-empty column runs" -- ideally near 8.
//      "Non-empty" = column has at least one pixel above an opacity threshold,
//      OR (for solid-black backgrounds) at least one pixel that is not fully black.
async function inspectSheetImage(img) {
  const w = img.naturalWidth, h = img.naturalHeight;
  if (!w || !h) return { ok: false, reasons: ['Image has zero dimensions.'] };

  const reasons = [];
  if (w / h < 2.5) {
    reasons.push(`Aspect ratio ${(w / h).toFixed(2)} doesn't look like a horizontal strip (expected width/height > ~3).`);
  }

  // Downsample for speed: cap analysis canvas at 800px wide.
  const scale = Math.min(1, 800 / w);
  const cw = Math.max(8, Math.round(w * scale));
  const ch = Math.max(8, Math.round(h * scale));
  const canvas = document.createElement('canvas');
  canvas.width = cw;
  canvas.height = ch;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0, cw, ch);
  const data = ctx.getImageData(0, 0, cw, ch).data;

  // Determine background mode: if average alpha is < 250, treat as transparent-bg
  // (count alpha > 32 as "ink"). Otherwise treat as opaque-bg (count non-near-black as "ink").
  let totalAlpha = 0;
  const sampleStep = Math.max(1, Math.floor((cw * ch) / 4000));
  let samples = 0;
  for (let i = 0; i < data.length; i += 4 * sampleStep) {
    totalAlpha += data[i + 3];
    samples++;
  }
  const avgAlpha = samples > 0 ? totalAlpha / samples : 255;
  const transparentBg = avgAlpha < 250;

  function colHasInk(x) {
    for (let y = 0; y < ch; y++) {
      const idx = (y * cw + x) * 4;
      const r = data[idx], g = data[idx + 1], b = data[idx + 2], a = data[idx + 3];
      if (transparentBg) {
        if (a > 32) return true;
      } else {
        // opaque background: assume background is a uniform color (often black or white).
        // call a column "ink" if any pixel deviates significantly from pure black or pure white.
        const isNearBlack = r < 24 && g < 24 && b < 24;
        const isNearWhite = r > 232 && g > 232 && b > 232;
        if (!isNearBlack && !isNearWhite) return true;
      }
    }
    return false;
  }

  // Count runs of consecutive ink columns; require a small min-run to ignore stray pixels.
  const minRunPx = Math.max(2, Math.floor(cw * 0.01));
  let runs = 0, currentRun = 0;
  for (let x = 0; x < cw; x++) {
    if (colHasInk(x)) {
      currentRun++;
    } else {
      if (currentRun >= minRunPx) runs++;
      currentRun = 0;
    }
  }
  if (currentRun >= minRunPx) runs++;

  if (runs < 4) {
    reasons.push(`Only ~${runs} distinct horizontal islands detected (expected ~8). Sheet may be wrong format or wrong background.`);
  } else if (runs > 12) {
    reasons.push(`~${runs} horizontal islands detected (expected ~8). Sheet may have stray marks.`);
  }

  return {
    ok: reasons.length === 0,
    reasons,
    width: w,
    height: h,
    detectedIslands: runs,
    backgroundMode: transparentBg ? 'transparent' : 'opaque',
  };
}
