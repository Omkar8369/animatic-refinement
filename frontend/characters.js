// Animatic Refinement - Node 1 frontend
// Character Library page logic.
// Depends on storage.js being loaded first.

(function () {
  const nameInput   = document.getElementById('char-name');
  const fileInput   = document.getElementById('char-sheet');
  const addBtn      = document.getElementById('add-character-btn');
  const clearFormBtn = document.getElementById('clear-form-btn');
  const statusEl    = document.getElementById('add-status');
  const listEl      = document.getElementById('character-list');
  const downloadBtn = document.getElementById('download-library-btn');
  const clearLibBtn = document.getElementById('clear-library-btn');

  function setStatus(msg, kind) {
    statusEl.textContent = msg || '';
    statusEl.style.color = kind === 'error' ? 'var(--danger)' :
                           kind === 'ok'    ? 'var(--ok)' :
                           'var(--text-muted)';
  }

  function renderList() {
    const characters = loadCharacters();
    listEl.innerHTML = '';

    if (characters.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = 'No characters registered yet. Add one above.';
      listEl.appendChild(empty);
      downloadBtn.disabled = true;
      return;
    }

    downloadBtn.disabled = false;

    for (const c of characters) {
      const card = document.createElement('div');
      card.className = 'character-card';

      const img = document.createElement('img');
      img.className = 'thumb';
      img.src = c.dataUrl;
      img.alt = c.name;
      card.appendChild(img);

      const name = document.createElement('div');
      name.className = 'name';
      name.textContent = c.name;
      card.appendChild(name);

      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = `${c.width}x${c.height} - ${c.sheetFilename}`;
      card.appendChild(meta);

      const quality = document.createElement('div');
      const q = c.quality || {};
      quality.className = 'quality ' + (q.ok ? 'ok' : 'warn');
      quality.textContent = q.ok
        ? `OK - ${q.detectedIslands || '?'} islands (${q.backgroundMode || '?'} bg)`
        : `Warning - ${(q.reasons || []).join(' ')}`;
      card.appendChild(quality);

      const removeBtn = document.createElement('button');
      removeBtn.className = 'danger';
      removeBtn.textContent = 'Remove';
      removeBtn.addEventListener('click', () => {
        if (confirm(`Remove "${c.name}" from the library?`)) {
          removeCharacter(c.name);
          renderList();
        }
      });
      card.appendChild(removeBtn);

      listEl.appendChild(card);
    }
  }

  async function handleAdd() {
    const name = (nameInput.value || '').trim();
    const file = fileInput.files && fileInput.files[0];

    if (!name) { setStatus('Enter a character name first.', 'error'); return; }
    if (!file) { setStatus('Pick a model sheet image first.', 'error'); return; }

    addBtn.disabled = true;
    setStatus('Reading sheet...');

    try {
      const dataUrl = await readFileAsDataURL(file);
      const img = await loadImage(dataUrl);
      setStatus('Inspecting sheet...');
      const quality = await inspectSheetImage(img);

      const sheetFilename = canonicalSheetFilename(name);
      const character = {
        name,
        sheetFilename,
        sourceFilename: file.name,
        width: img.naturalWidth,
        height: img.naturalHeight,
        addedAt: new Date().toISOString(),
        dataUrl,
        quality,
      };
      addCharacter(character);
      renderList();

      nameInput.value = '';
      fileInput.value = '';
      const summary = quality.ok
        ? `Added "${name}" - looks ok (${quality.detectedIslands} islands).`
        : `Added "${name}" with warnings: ${quality.reasons.join(' ')}`;
      setStatus(summary, quality.ok ? 'ok' : 'error');
    } catch (err) {
      console.error(err);
      setStatus('Failed to add character: ' + (err.message || err), 'error');
    } finally {
      addBtn.disabled = false;
    }
  }

  function handleDownload() {
    const characters = loadCharacters();
    if (characters.length === 0) return;

    const manifest = {
      schemaVersion: 1,
      generatedAt: new Date().toISOString(),
      conventions: {
        sheetFormat: '8-angle horizontal strip',
        backgroundExpected: 'transparent or solid; sliced via alpha-island bbox in Node 6',
        angleOrderLeftToRight: [
          'back', 'back-3q-L', 'profile-L', 'front-3q-L',
          'front', 'front-3q-R', 'profile-R', 'back-3q-R'
        ],
        angleOrderConfirmed: false,
      },
      characters: characters.map(c => ({
        name: c.name,
        sheetFilename: c.sheetFilename,
        width: c.width,
        height: c.height,
        quality: c.quality,
        addedAt: c.addedAt,
      })),
    };

    downloadText('characters.json', JSON.stringify(manifest, null, 2));

    // Trigger one PNG download per character, lightly staggered so the browser
    // doesn't drop them. Filename is canonicalised so Node 6 has a stable lookup.
    characters.forEach((c, i) => {
      setTimeout(() => downloadDataURL(c.sheetFilename, c.dataUrl), 200 * (i + 1));
    });

    setStatus(
      `Downloading characters.json + ${characters.length} sheet PNG(s). ` +
      `If your browser blocks multi-file downloads, allow them and click again.`,
      'ok'
    );
  }

  function handleClearLibrary() {
    if (!confirm('Clear the entire character library? This cannot be undone.')) return;
    clearCharacters();
    renderList();
    setStatus('Library cleared.', 'ok');
  }

  addBtn.addEventListener('click', handleAdd);
  clearFormBtn.addEventListener('click', () => {
    nameInput.value = '';
    fileInput.value = '';
    setStatus('');
  });
  downloadBtn.addEventListener('click', handleDownload);
  clearLibBtn.addEventListener('click', handleClearLibrary);

  renderList();
})();
