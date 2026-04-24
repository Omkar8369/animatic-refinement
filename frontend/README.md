# Node 1 - Project Input & Setup Interface

Static frontend (no server). Two HTML pages that capture all metadata the
pipeline needs and produce two JSON files (`characters.json`, `metadata.json`)
plus the canonically-named character sheet PNGs.

See [`../docs/PLAN.md`](../docs/PLAN.md) Node 1 for the canonical sub-step
breakdown and the design decisions that are locked in.

## Files

| File              | Purpose                                                                 |
| ----------------- | ----------------------------------------------------------------------- |
| `characters.html` | Step 1 - Character Library (pre-register every character in the project)|
| `index.html`      | Step 2 - Shot Metadata Form (one big form with `+ Add shot` blocks)     |
| `storage.js`      | Shared helpers: localStorage I/O, downloads, sheet sanity check         |
| `characters.js`   | Character Library page logic                                            |
| `shots.js`        | Shot Metadata Form page logic                                           |
| `styles.css`      | Shared styles                                                           |

## Operator workflow (per project)

1. **Open `characters.html`** in any modern browser (Chrome, Edge, Firefox).
   No web server needed - just double-click the file or open it via
   `file:///.../frontend/characters.html`.
   - For each character in the project: type the display name (e.g. `Bhim`)
     and pick the model-sheet PNG (an 8-angle horizontal strip, transparent
     or solid background, full color).
   - The page runs a quick client-side sanity check (aspect ratio, alpha
     islands ~ 8) and warns if a sheet doesn't look like a horizontal strip.
     Full slicing happens later in Node 6; the warning is just an early flag.
   - When all characters are added, click **Download library**. Your browser
     will download `characters.json` and one renamed PNG per character
     (`bhim_sheet.png`, `chutki_sheet.png`, ...).

2. **Open `index.html`**. The character library you just registered is read
   from the browser's `localStorage`, so the per-character identity dropdown
   is already populated.
   - Fill in `Project name` and `Batch size`.
   - Click **+ Add shot** for each shot in the batch.
   - Per shot: set `Shot ID` (auto-filled), pick the MP4 file (this only
     captures the filename and shows a preview - the bytes are NOT uploaded
     anywhere), set `Duration (frames @ 25 FPS)`, set `Character count`, and
     for each character pick the identity from the dropdown and the position
     (`L` / `CL` / `C` / `CR` / `R`).
   - Click **Download metadata.json**. The form validates first and prints
     any blocking issues (duplicate shot IDs, missing identities, etc).

3. **Move files into the pipeline input folder**. The browser cannot write
   directly to disk paths, so the operator does this manually:
   - Place `metadata.json` into the pipeline input folder.
   - Place `characters.json` and the renamed sheet PNGs into the pipeline
     input folder.
   - Place every MP4 file referenced in `metadata.json` into the same folder
     (filenames must match exactly).
   - Node 2 reads from this folder and validates everything before kicking
     off the rest of the pipeline.

## Why no server?

Locked design decision **2a** (see `../docs/PLAN.md`): browser download via
`Blob` + `<a download>`. Reasons:

- Zero install on the operator's machine. Just open an HTML file.
- Repo can be cloned and used immediately on Windows without Python or Node.
- The pipeline itself runs on RunPod, so adding a local server would be a
  second deployment surface to maintain for no real gain.

The trade-off is the manual file-move step in workflow point 3 above. That
is documented; an automated handoff is out of scope for Part 1.

## Why MP4s aren't uploaded by the form

Browsers can't write to arbitrary disk paths from a static page. Embedding
MP4s as base64 in `metadata.json` would balloon the file to hundreds of MB.
So the form captures the MP4 *filename* (and shows a local thumbnail for the
operator's confidence) but the operator copies the actual MP4 files into the
pipeline folder themselves. `metadata.json` references files by name only.

## Draft state

Both pages persist to `localStorage`:

- `animaticRefinement.characters.v1` - the character library.
- `animaticRefinement.shots.v1` - the in-progress shot list (no MP4 binary).
- `animaticRefinement.project.v1` - project name, batch size, notes.

So if the operator closes the browser mid-edit, the work is recoverable.
The MP4 file picker selections must be re-done after a reload (browsers
intentionally don't remember `<input type="file">` values across reloads).

## Output schema (metadata.json)

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-04-22T12:34:56.000Z",
  "project": {
    "name": "ChhotaBhim_Ep042",
    "batchSize": 4,
    "fps": 25,
    "notes": ""
  },
  "shots": [
    {
      "shotId": "shot_001",
      "mp4Filename": "scene01_shot01.mp4",
      "durationFrames": 75,
      "durationSeconds": 3.0,
      "characterCount": 2,
      "characters": [
        { "identity": "Bhim",   "position": "CL" },
        { "identity": "Chutki", "position": "CR" }
      ]
    }
  ]
}
```

## Output schema (characters.json)

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-04-22T12:34:56.000Z",
  "conventions": {
    "sheetFormat": "8-angle horizontal strip",
    "backgroundExpected": "transparent or solid; sliced via alpha-island bbox in Node 6",
    "angleOrderLeftToRight": [
      "back", "back-3q-L", "profile-L", "front-3q-L",
      "front", "front-3q-R", "profile-R", "back-3q-R"
    ],
    "angleOrderConfirmed": true
  },
  "characters": [
    {
      "name": "Bhim",
      "sheetFilename": "bhim_sheet.png",
      "width": 4096,
      "height": 512,
      "quality": { "ok": true, "detectedIslands": 8, "backgroundMode": "transparent" },
      "addedAt": "2026-04-22T12:30:00.000Z"
    }
  ]
}
```

`angleOrderConfirmed` defaults to `true` since the canonical left-to-right
angle order was confirmed by the user on 2026-04-23 against the Bhim
reference template. Node 6 still respects `false` — if an operator flips
the flag or hand-edits `characters.json` for a new project with a different
angle layout, Node 6 fails loudly so a wrong order can't silently ship.

**Status:** Built (initial implementation - awaiting user testing on a real shot).
