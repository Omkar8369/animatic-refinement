// Animatic Refinement - Node 1 frontend
// Shot Metadata Form page logic.
// Depends on storage.js being loaded first.

(function () {
  const warningEl     = document.getElementById('library-warning');
  const projectName   = document.getElementById('project-name');
  const batchSize     = document.getElementById('batch-size');
  const projectNotes  = document.getElementById('project-notes');
  const shotListEl    = document.getElementById('shot-list');
  const addShotBtn    = document.getElementById('add-shot-btn');
  const downloadBtn   = document.getElementById('download-metadata-btn');
  const saveDraftBtn  = document.getElementById('save-draft-btn');
  const clearDraftBtn = document.getElementById('clear-draft-btn');
  const statusEl      = document.getElementById('export-status');

  // In-memory shot list. Each entry is the data the form would serialize.
  // We re-render the DOM from this array on every change so add/remove is simple.
  let shots = [];
  let nextShotIdSeq = 1;

  function setStatus(msg, kind) {
    statusEl.textContent = msg || '';
    statusEl.style.color = kind === 'error' ? 'var(--danger)' :
                           kind === 'ok'    ? 'var(--ok)' :
                           'var(--text-muted)';
  }

  function defaultShotId() {
    const id = `shot_${String(nextShotIdSeq).padStart(3, '0')}`;
    nextShotIdSeq++;
    return id;
  }

  function newShot() {
    return {
      shotId: defaultShotId(),
      mp4Filename: '',
      mp4PreviewUrl: '',
      durationFrames: 25,
      characters: [
        { identity: '', position: 'C' },
      ],
    };
  }

  function rerender() {
    const characters = loadCharacters();
    warningEl.style.display = characters.length === 0 ? 'block' : 'none';

    shotListEl.innerHTML = '';
    if (shots.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = 'No shots yet. Click "+ Add shot" to add one.';
      shotListEl.appendChild(empty);
      return;
    }

    shots.forEach((shot, shotIdx) => {
      shotListEl.appendChild(buildShotBlock(shot, shotIdx, characters));
    });
  }

  function buildShotBlock(shot, shotIdx, characters) {
    const block = document.createElement('div');
    block.className = 'shot-block';

    // Header: tag + remove button
    const header = document.createElement('div');
    header.className = 'shot-block-header';
    const tag = document.createElement('div');
    tag.className = 'shot-tag';
    tag.textContent = `Shot ${shotIdx + 1}`;
    header.appendChild(tag);
    const removeShotBtn = document.createElement('button');
    removeShotBtn.className = 'danger';
    removeShotBtn.textContent = 'Remove shot';
    removeShotBtn.addEventListener('click', () => {
      if (confirm(`Remove ${shot.shotId}?`)) {
        shots.splice(shotIdx, 1);
        rerender();
      }
    });
    header.appendChild(removeShotBtn);
    block.appendChild(header);

    // Row 1: shot id, MP4 picker, duration
    const row1 = document.createElement('div');
    row1.className = 'row cols-3';

    row1.appendChild(field('Shot ID', () => {
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.value = shot.shotId;
      inp.addEventListener('input', () => { shot.shotId = inp.value.trim(); });
      return inp;
    }));

    row1.appendChild(field('MP4 file (filename only - not uploaded)', () => {
      const inp = document.createElement('input');
      inp.type = 'file';
      inp.accept = 'video/mp4,video/*';
      inp.addEventListener('change', () => {
        const f = inp.files && inp.files[0];
        if (!f) return;
        shot.mp4Filename = f.name;
        if (shot.mp4PreviewUrl) URL.revokeObjectURL(shot.mp4PreviewUrl);
        shot.mp4PreviewUrl = URL.createObjectURL(f);
        rerender();
      });
      return inp;
    }));

    row1.appendChild(field('Duration (frames @ 25 FPS)', () => {
      const inp = document.createElement('input');
      inp.type = 'number';
      inp.min = '1';
      inp.value = String(shot.durationFrames);
      inp.addEventListener('input', () => {
        const n = parseInt(inp.value, 10);
        shot.durationFrames = isNaN(n) || n < 1 ? 1 : n;
      });
      return inp;
    }));

    block.appendChild(row1);

    // MP4 filename hint + preview
    if (shot.mp4Filename) {
      const fileLine = document.createElement('div');
      fileLine.className = 'muted';
      fileLine.style.marginTop = '6px';
      const seconds = (shot.durationFrames / FPS).toFixed(2);
      fileLine.innerHTML = `<code>${escapeHtml(shot.mp4Filename)}</code> &middot; ${shot.durationFrames} frames (${seconds}s @ 25 FPS)`;
      block.appendChild(fileLine);
    }
    if (shot.mp4PreviewUrl) {
      const video = document.createElement('video');
      video.className = 'video-preview';
      video.src = shot.mp4PreviewUrl;
      video.controls = true;
      video.muted = true;
      video.style.marginTop = '8px';
      block.appendChild(video);
    }

    // Character count
    const ccRow = document.createElement('div');
    ccRow.className = 'row cols-2';
    ccRow.style.marginTop = '12px';

    ccRow.appendChild(field('Character count', () => {
      const inp = document.createElement('input');
      inp.type = 'number';
      inp.min = '0';
      inp.max = '20';
      inp.value = String(shot.characters.length);
      inp.addEventListener('input', () => {
        let n = parseInt(inp.value, 10);
        if (isNaN(n) || n < 0) n = 0;
        if (n > 20) n = 20;
        const current = shot.characters.length;
        if (n > current) {
          for (let i = current; i < n; i++) {
            shot.characters.push({ identity: '', position: 'C' });
          }
        } else if (n < current) {
          shot.characters.length = n;
        }
        rerender();
      });
      return inp;
    }));

    ccRow.appendChild(field('Quick reference', () => {
      const span = document.createElement('div');
      span.className = 'muted';
      span.style.fontFamily = 'var(--mono)';
      span.style.fontSize = '12px';
      span.textContent = 'Positions: L | CL | C | CR | R (left -> right)';
      return span;
    }));

    block.appendChild(ccRow);

    // Per-character rows
    const charRowsContainer = document.createElement('div');
    charRowsContainer.className = 'character-rows';
    shot.characters.forEach((ch, chIdx) => {
      charRowsContainer.appendChild(buildCharacterRow(shot, chIdx, characters));
    });
    block.appendChild(charRowsContainer);

    return block;
  }

  function buildCharacterRow(shot, chIdx, characters) {
    const ch = shot.characters[chIdx];
    const row = document.createElement('div');
    row.className = 'character-row';

    const idx = document.createElement('div');
    idx.className = 'idx';
    idx.textContent = `#${chIdx + 1}`;
    row.appendChild(idx);

    // Identity dropdown
    const identityWrap = document.createElement('div');
    const identityLabel = document.createElement('label');
    identityLabel.textContent = 'Identity';
    identityWrap.appendChild(identityLabel);
    const select = document.createElement('select');
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = characters.length === 0 ? '(no characters in library)' : '-- choose --';
    select.appendChild(placeholder);
    for (const c of characters) {
      const opt = document.createElement('option');
      opt.value = c.name;
      opt.textContent = c.name;
      if (ch.identity === c.name) opt.selected = true;
      select.appendChild(opt);
    }
    select.addEventListener('change', () => { ch.identity = select.value; });
    identityWrap.appendChild(select);
    row.appendChild(identityWrap);

    // Position dropdown
    const posWrap = document.createElement('div');
    const posLabel = document.createElement('label');
    posLabel.textContent = 'Position';
    posWrap.appendChild(posLabel);
    const posSelect = document.createElement('select');
    for (const p of POSITIONS) {
      const opt = document.createElement('option');
      opt.value = p.code;
      opt.textContent = `${p.code} - ${p.label}`;
      if (ch.position === p.code) opt.selected = true;
      posSelect.appendChild(opt);
    }
    posSelect.addEventListener('change', () => { ch.position = posSelect.value; });
    posWrap.appendChild(posSelect);
    row.appendChild(posWrap);

    return row;
  }

  function field(labelText, builder) {
    const wrap = document.createElement('div');
    const label = document.createElement('label');
    label.textContent = labelText;
    wrap.appendChild(label);
    wrap.appendChild(builder());
    return wrap;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
  }

  // ----- Validation + export ------------------------------------------------

  function validateAndBuildMetadata() {
    const errors = [];
    const project = (projectName.value || '').trim();
    if (!project) errors.push('Project name is required.');

    const batch = parseInt(batchSize.value, 10);
    if (isNaN(batch) || batch < 1) errors.push('Batch size must be >= 1.');

    if (shots.length === 0) errors.push('Add at least one shot.');

    const knownNames = new Set(loadCharacters().map(c => c.name));

    const seenShotIds = new Set();
    const cleanShots = shots.map((s, i) => {
      const tag = `shot[${i + 1}]`;
      if (!s.shotId) errors.push(`${tag}: shot ID is empty.`);
      else if (seenShotIds.has(s.shotId)) errors.push(`${tag}: duplicate shot ID "${s.shotId}".`);
      seenShotIds.add(s.shotId);

      if (!s.mp4Filename) errors.push(`${tag} (${s.shotId}): no MP4 file picked.`);

      if (!s.durationFrames || s.durationFrames < 1) {
        errors.push(`${tag} (${s.shotId}): duration must be >= 1 frame.`);
      }

      const cleanChars = s.characters.map((c, ci) => {
        const ctag = `${tag} character[${ci + 1}]`;
        if (!c.identity) errors.push(`${ctag}: identity not chosen.`);
        else if (!knownNames.has(c.identity)) {
          errors.push(`${ctag}: identity "${c.identity}" not in library.`);
        }
        if (!POSITIONS.find(p => p.code === c.position)) {
          errors.push(`${ctag}: invalid position "${c.position}".`);
        }
        return { identity: c.identity, position: c.position };
      });

      return {
        shotId: s.shotId,
        mp4Filename: s.mp4Filename,
        durationFrames: s.durationFrames,
        durationSeconds: +(s.durationFrames / FPS).toFixed(4),
        characterCount: cleanChars.length,
        characters: cleanChars,
      };
    });

    const metadata = {
      schemaVersion: 1,
      generatedAt: new Date().toISOString(),
      project: {
        name: project,
        batchSize: batch,
        fps: FPS,
        notes: (projectNotes.value || '').trim(),
      },
      shots: cleanShots,
    };

    return { errors, metadata };
  }

  function handleDownloadMetadata() {
    const { errors, metadata } = validateAndBuildMetadata();
    if (errors.length > 0) {
      setStatus('Cannot export - fix these issues:\n' + errors.map(e => '  - ' + e).join('\n'), 'error');
      statusEl.style.whiteSpace = 'pre-wrap';
      return;
    }
    statusEl.style.whiteSpace = 'normal';
    downloadText('metadata.json', JSON.stringify(metadata, null, 2));
    saveDraft();
    setStatus(`Exported metadata.json with ${metadata.shots.length} shot(s).`, 'ok');
  }

  function saveDraft() {
    const stripped = shots.map(s => ({
      shotId: s.shotId,
      mp4Filename: s.mp4Filename,
      durationFrames: s.durationFrames,
      characters: s.characters.map(c => ({ identity: c.identity, position: c.position })),
    }));
    saveShotsDraft({ shots: stripped, nextShotIdSeq });
    saveProjectDraft({
      name: projectName.value,
      batchSize: parseInt(batchSize.value, 10) || 1,
      notes: projectNotes.value,
    });
  }

  function loadDraft() {
    const proj = loadProjectDraft();
    if (proj) {
      projectName.value = proj.name || '';
      batchSize.value = proj.batchSize || 1;
      projectNotes.value = proj.notes || '';
    }
    const draft = loadShotsDraft();
    if (draft && Array.isArray(draft.shots)) {
      shots = draft.shots.map(s => ({
        shotId: s.shotId || '',
        mp4Filename: s.mp4Filename || '',
        mp4PreviewUrl: '',
        durationFrames: s.durationFrames || 25,
        characters: Array.isArray(s.characters) && s.characters.length > 0
          ? s.characters.map(c => ({ identity: c.identity || '', position: c.position || 'C' }))
          : [{ identity: '', position: 'C' }],
      }));
      nextShotIdSeq = draft.nextShotIdSeq || (shots.length + 1);
    }
  }

  // Wire up
  addShotBtn.addEventListener('click', () => {
    shots.push(newShot());
    rerender();
  });

  downloadBtn.addEventListener('click', handleDownloadMetadata);

  saveDraftBtn.addEventListener('click', () => {
    saveDraft();
    setStatus('Draft saved to browser localStorage.', 'ok');
  });

  clearDraftBtn.addEventListener('click', () => {
    if (!confirm('Clear the saved shot draft? Project name + character library are kept.')) return;
    clearShotsDraft();
    shots = [];
    nextShotIdSeq = 1;
    rerender();
    setStatus('Draft cleared.', 'ok');
  });

  // Re-render whenever the user comes back to the tab in case they added/removed
  // characters in the other tab.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) rerender();
  });

  loadDraft();
  rerender();
})();
