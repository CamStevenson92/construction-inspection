/* ============================================================
   Meinhardt ANZ — Site Inspection Processor
   ============================================================ */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let sessionId    = null;
let photos       = [];
let currentIdx   = -1;
let saveTimer    = null;
let annotateMode = null;   // 'voice' | 'text'
let aiAvailable  = false;

// Voice state
let recognition  = null;
let isRecording  = false;
let liveText     = '';

// Swipe state
let swipeTouchStartX = 0;
let swipeTouchStartY = 0;
let swipeActive      = false;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
  setupDropZone();
  document.getElementById('fileInput').addEventListener('change', e => {
    handleFiles(Array.from(e.target.files));
    e.target.value = '';
  });

  try {
    const res = await api('POST', '/api/session');
    sessionId = res.session_id;
  } catch {
    showToast('Cannot reach server — is web_app.py running?');
  }

  try {
    const cfg = await api('GET', '/api/config');
    aiAvailable = cfg.ai_available;
  } catch { /* offline */ }

  setupRecordButton();
  setupSwipe();
});

// ---------------------------------------------------------------------------
// View navigation
// ---------------------------------------------------------------------------
function stepClick(name) {
  if (name === 'annotate' && photos.length && !annotateMode) {
    showModeModal(); return;
  }
  goToView(name);
}

function goToView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  document.querySelectorAll('.step-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.view === name));

  if (name === 'annotate') {
    const isVoice = annotateMode === 'voice';
    document.getElementById('voice-annotate').classList.toggle('hidden', !isVoice);
    document.getElementById('text-annotate').classList.toggle('hidden', isVoice);
    if (photos.length && currentIdx === -1) {
      isVoice ? voiceShowPhoto(0) : showPhoto(0);
    }
  }
  if (name === 'report') updateReportSummary();
}

// ---------------------------------------------------------------------------
// Mode modal
// ---------------------------------------------------------------------------
function showModeModal() {
  document.getElementById('modeModal').classList.remove('hidden');
}

function selectMode(mode) {
  annotateMode = mode;
  document.getElementById('modeModal').classList.add('hidden');
  goToView('annotate');
}

function switchMode() {
  annotateMode = annotateMode === 'voice' ? 'text' : 'voice';
  const isVoice = annotateMode === 'voice';
  document.getElementById('voice-annotate').classList.toggle('hidden', !isVoice);
  document.getElementById('text-annotate').classList.toggle('hidden', isVoice);
  if (isVoice) voiceShowPhoto(currentIdx);
  else showPhoto(currentIdx);
}

// ---------------------------------------------------------------------------
// Drop zone
// ---------------------------------------------------------------------------
function setupDropZone() {
  const zone = document.getElementById('dropZone');
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.classList.remove('drag-over');
    handleFiles(Array.from(e.dataTransfer.files));
  });
  zone.addEventListener('click', e => {
    if (e.target.tagName !== 'LABEL') document.getElementById('fileInput').click();
  });
}

// ---------------------------------------------------------------------------
// Upload + processing
// ---------------------------------------------------------------------------
async function handleFiles(files) {
  if (!sessionId) { showToast('No server session — refresh the page.'); return; }
  const imgs = files.filter(f => /\.(jpe?g|png|heic|heif|tiff?|webp)$/i.test(f.name));
  if (!imgs.length) { showToast('No supported image files selected.'); return; }

  photos = []; currentIdx = -1; annotateMode = null;
  document.getElementById('fileList').innerHTML = '';
  document.getElementById('fileList').classList.add('hidden');
  document.getElementById('uploadFooter').classList.add('hidden');

  const progressCard  = document.getElementById('uploadProgress');
  progressCard.classList.remove('hidden');
  document.getElementById('progressLabel').textContent = 'Uploading…';
  document.getElementById('progressCount').textContent = `0 / ${imgs.length}`;
  document.getElementById('progressFill').style.width = '0%';

  const fd = new FormData();
  imgs.forEach(f => fd.append('photos', f));
  try {
    await api('POST', `/api/upload/${sessionId}`, fd);
  } catch (err) {
    showToast('Upload failed: ' + err.message);
    progressCard.classList.add('hidden'); return;
  }

  document.getElementById('progressLabel').textContent = 'Extracting metadata & fetching weather…';

  const poll = setInterval(async () => {
    try {
      const st = await api('GET', `/api/status/${sessionId}`);
      const pct = st.total ? Math.round(st.processed / st.total * 100) : 0;
      document.getElementById('progressFill').style.width = pct + '%';
      document.getElementById('progressCount').textContent = `${st.processed} / ${st.total}`;
      if (!st.processing) { clearInterval(poll); progressCard.classList.add('hidden'); await loadPhotos(); }
    } catch { /* retry */ }
  }, 800);
}

async function loadPhotos() {
  photos = await api('GET', `/api/photos/${sessionId}`);

  const list = document.getElementById('fileList');
  list.innerHTML = ''; list.classList.remove('hidden');
  photos.forEach((p, i) => {
    const item = document.createElement('div');
    item.className = 'file-item' + (p.is_duplicate ? ' is-dup' : p.similar_to ? ' is-sim' : '');
    const badges = [];
    if (p.has_gps)       badges.push('<span class="badge badge-gps">📍 GPS</span>');
    if (p.has_direction) badges.push('<span class="badge badge-dir">🧭 Dir</span>');
    if (p.weather)       badges.push('<span class="badge badge-wx">🌤 Wx</span>');
    if (p.is_duplicate)  badges.push('<span class="badge badge-dup">⚠ DUPLICATE</span>');
    else if (p.similar_to) badges.push(`<span class="badge badge-sim">≈ Similar to ${esc(p.similar_to)}</span>`);
    item.innerHTML = `
      <img class="file-thumb" src="/api/photo/${sessionId}/${i}/thumb" alt="">
      <div class="file-meta">
        <div class="file-name">${esc(p.filename)}</div>
        <div class="file-detail">${esc(p.datetime)} &nbsp;|&nbsp; ${esc(p.coords)}</div>
        ${badges.length ? `<div class="file-badges">${badges.join('')}</div>` : ''}
      </div>`;
    list.appendChild(item);
  });

  const gps  = photos.filter(p => p.has_gps).length;
  const dups = photos.filter(p => p.is_duplicate).length;
  document.getElementById('statsRow').textContent =
    `${photos.length} photos  ·  ${gps} with GPS  ·  ${dups} duplicate(s) detected`;
  document.getElementById('uploadFooter').classList.remove('hidden');

  document.querySelectorAll('.step-btn[data-view="annotate"], .step-btn[data-view="report"]')
    .forEach(b => b.disabled = false);

  buildThumbStrip();
}

// ---------------------------------------------------------------------------
// TEXT MODE
// ---------------------------------------------------------------------------
function buildThumbStrip() {
  const strip = document.getElementById('thumbStrip');
  strip.innerHTML = '';
  photos.forEach((p, i) => {
    const el = document.createElement('div');
    el.className = 'thumb-item' + (p.is_duplicate ? ' is-dup' : p.similar_to ? ' is-sim' : '');
    el.dataset.idx = i;
    el.onclick = () => showPhoto(i);
    el.innerHTML = `<img src="/api/photo/${sessionId}/${i}/thumb" alt="" loading="lazy">
                    <div class="thumb-label">${esc(p.filename)}</div>`;
    strip.appendChild(el);
  });
}

async function showPhoto(idx) {
  if (!photos.length || idx < 0 || idx >= photos.length) return;
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; await flushNotes(); }
  currentIdx = idx;
  const p = photos[idx];

  // Smooth fade transition
  const img = document.getElementById('photoImg');
  img.classList.add('fading');
  setTimeout(() => {
    img.src = `/api/photo/${sessionId}/${idx}/image`;
    img.onload = () => img.classList.remove('fading');
  }, 180);

  document.getElementById('photoCounter').textContent = `${idx + 1} / ${photos.length}`;
  document.getElementById('btnPrev').disabled = idx === 0;
  document.getElementById('btnNext').disabled = idx === photos.length - 1;

  document.getElementById('mDatetime').textContent  = p.datetime  || '—';
  document.getElementById('mCoords').textContent    = p.coords    || '—';
  document.getElementById('mDirection').textContent = p.direction || '—';
  document.getElementById('mAltitude').textContent  = p.altitude  || '—';
  document.getElementById('mCamera').textContent    = p.camera    || '—';
  document.getElementById('mWeather').textContent   = p.weather   || 'No data';

  const banner = document.getElementById('dupBanner');
  banner.className = 'dup-banner';
  if (p.is_duplicate) {
    banner.classList.add('is-dup'); banner.classList.remove('hidden');
    banner.textContent = `⚠  DUPLICATE — very similar to "${p.similar_to}"`;
  } else if (p.similar_to) {
    banner.classList.add('is-sim'); banner.classList.remove('hidden');
    banner.textContent = `≈  SIMILAR to "${p.similar_to}"`;
  } else { banner.classList.add('hidden'); }

  const ta1 = document.getElementById('txtInspected');
  const ta2 = document.getElementById('txtIssues');
  const ta3 = document.getElementById('txtActions');
  ta1._loading = ta2._loading = ta3._loading = true;
  ta1.value = p.what_inspected   || '';
  ta2.value = p.issues_found     || '';
  ta3.value = p.actions_required || '';
  ta1._loading = ta2._loading = ta3._loading = false;
  document.getElementById('notesSaved').textContent = '';

  document.querySelectorAll('.thumb-item').forEach(el =>
    el.classList.toggle('active', parseInt(el.dataset.idx) === idx));
  document.querySelector(`.thumb-item[data-idx="${idx}"]`)
    ?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
}

function navigate(delta) {
  const next = currentIdx + delta;
  if (next >= 0 && next < photos.length) showPhoto(next);
}

// Keyboard (text mode)
document.addEventListener('keydown', e => {
  if (!document.getElementById('view-annotate').classList.contains('active')) return;
  if (document.activeElement.tagName === 'TEXTAREA') return;
  if (e.key === 'ArrowLeft')  navigate(-1);
  if (e.key === 'ArrowRight') navigate(1);
});

function scheduleNoteSave() {
  if (document.getElementById('txtInspected')._loading) return;
  document.getElementById('notesSaved').textContent = '';
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(flushNotes, 800);
}

async function flushNotes() {
  saveTimer = null;
  if (currentIdx < 0 || !sessionId) return;
  const body = {
    what_inspected:   document.getElementById('txtInspected').value,
    issues_found:     document.getElementById('txtIssues').value,
    actions_required: document.getElementById('txtActions').value,
  };
  if (photos[currentIdx]) Object.assign(photos[currentIdx], body);
  try {
    await api('POST', `/api/notes/${sessionId}/${currentIdx}`, body);
    const el = document.getElementById('notesSaved');
    el.textContent = 'Saved ✓';
    setTimeout(() => { el.textContent = ''; }, 1800);
  } catch { document.getElementById('notesSaved').textContent = 'Save failed'; }
}

// ---------------------------------------------------------------------------
// VOICE MODE — photo display
// ---------------------------------------------------------------------------
function voiceNavigate(delta) {
  const next = currentIdx + delta;
  if (next < 0 || next >= photos.length) return;
  voiceShowPhoto(next, delta);
}

function voiceShowPhoto(idx, direction = 0) {
  if (!photos.length || idx < 0 || idx >= photos.length) return;
  currentIdx = idx;
  const p    = photos[idx];
  const img  = document.getElementById('voiceImg');

  // Swipe-out current, swap src, swipe-in
  if (direction !== 0) {
    img.classList.add(direction > 0 ? 'swipe-left' : 'swipe-right');
    setTimeout(() => {
      img.src = `/api/photo/${sessionId}/${idx}/image`;
      img.classList.remove('swipe-left', 'swipe-right');
    }, 180);
  } else {
    img.src = `/api/photo/${sessionId}/${idx}/image`;
  }

  document.getElementById('vmCounter').textContent  = `${idx + 1} / ${photos.length}`;
  document.getElementById('vmDatetime').textContent = p.datetime || '';
  document.getElementById('vmCoords').textContent   = p.coords   || '';
  document.getElementById('vmWeather').textContent  = p.weather  || '';
  document.getElementById('btnVoicePrev').disabled  = idx === 0;
  document.getElementById('btnVoiceNext').disabled  = idx === photos.length - 1;

  const dup = document.getElementById('voiceDupBanner');
  dup.className = 'voice-dup-banner';
  if (p.is_duplicate) {
    dup.classList.add('is-dup'); dup.classList.remove('hidden');
    dup.textContent = `⚠  DUPLICATE — similar to "${p.similar_to}"`;
  } else if (p.similar_to) {
    dup.classList.add('is-sim'); dup.classList.remove('hidden');
    dup.textContent = `≈  SIMILAR to "${p.similar_to}"`;
  } else { dup.classList.add('hidden'); }

  document.getElementById('voiceTranscript').textContent = '';
  document.getElementById('voiceLiveText').classList.add('hidden');
}

// ---------------------------------------------------------------------------
// SWIPE — works on both voice photo area and text mode photo frame
// ---------------------------------------------------------------------------
function setupSwipe() {
  // Voice photo area
  const voiceArea = document.getElementById('voicePhotoArea');
  if (voiceArea) {
    voiceArea.addEventListener('touchstart', e => {
      swipeTouchStartX = e.touches[0].clientX;
      swipeTouchStartY = e.touches[0].clientY;
      swipeActive = true;
    }, { passive: true });

    voiceArea.addEventListener('touchend', e => {
      if (!swipeActive) return;
      swipeActive = false;
      const dx = e.changedTouches[0].clientX - swipeTouchStartX;
      const dy = e.changedTouches[0].clientY - swipeTouchStartY;
      // Only horizontal swipes, ignore vertical scroll
      if (Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 44) {
        if (!isRecording) voiceNavigate(dx < 0 ? 1 : -1);
      }
    }, { passive: true });

    voiceArea.addEventListener('touchcancel', () => { swipeActive = false; }, { passive: true });
  }

  // Text mode photo frame — also swipeable
  const textFrame = document.getElementById('photoFrame');
  if (textFrame) {
    let tx0 = 0, ty0 = 0, tactive = false;
    textFrame.addEventListener('touchstart', e => {
      tx0 = e.touches[0].clientX; ty0 = e.touches[0].clientY; tactive = true;
    }, { passive: true });
    textFrame.addEventListener('touchend', e => {
      if (!tactive) return; tactive = false;
      const dx = e.changedTouches[0].clientX - tx0;
      const dy = e.changedTouches[0].clientY - ty0;
      if (Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 44) navigate(dx < 0 ? 1 : -1);
    }, { passive: true });
    textFrame.addEventListener('touchcancel', () => { tactive = false; }, { passive: true });
  }
}

// ---------------------------------------------------------------------------
// VOICE RECORDING — toggle on single tap/click
// ---------------------------------------------------------------------------
function setupRecordButton() {
  const btn = document.getElementById('btnRecord');
  if (!btn) return;

  // Single tap / click toggles recording
  btn.addEventListener('click', toggleRecording);

  // Prevent double-fire on mobile (touchend fires click)
  btn.addEventListener('touchend', e => {
    e.preventDefault();   // stops the subsequent click event
    toggleRecording();
  }, { passive: false });
}

function toggleRecording() {
  if (isRecording) stopRecording();
  else startRecording();
}

function startRecording() {
  if (isRecording) return;
  if (currentIdx < 0) { showToast('No photo selected.'); return; }

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    showToast('Speech recognition not supported — use Chrome or Safari iOS 14.5+');
    return;
  }

  liveText    = '';
  isRecording = true;

  const ring = document.getElementById('recordRing');
  const mic  = document.getElementById('recordMic');
  const lbl  = document.getElementById('recordLabel');
  ring.classList.add('recording');
  mic.textContent = '⏹';
  lbl.textContent = 'Tap to Stop';
  document.querySelector('.voice-wrap')?.classList.add('recording');
  document.getElementById('voiceLiveText').classList.remove('hidden');
  document.getElementById('voiceTranscript').textContent = '';

  recognition = new SR();
  recognition.continuous      = true;
  recognition.interimResults  = true;
  recognition.lang            = navigator.language || 'en-AU';

  let finalText = '';

  recognition.onresult = e => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) finalText += e.results[i][0].transcript + ' ';
      else interim += e.results[i][0].transcript;
    }
    liveText = finalText + interim;
    document.getElementById('voiceTranscript').textContent = liveText;
  };

  // iOS stops recognition after silence — restart automatically
  recognition.onend = () => {
    if (isRecording) {
      try { recognition.start(); } catch { /* already starting */ }
    }
  };

  recognition.onerror = e => {
    if (e.error !== 'aborted' && e.error !== 'no-speech') showToast(`Mic error: ${e.error}`);
  };

  try { recognition.start(); }
  catch (err) { showToast('Could not start mic: ' + err.message); isRecording = false; }
}

async function stopRecording() {
  if (!isRecording) return;
  isRecording = false;

  if (recognition) { try { recognition.stop(); } catch { /* ok */ } recognition = null; }

  const ring = document.getElementById('recordRing');
  const mic  = document.getElementById('recordMic');
  const lbl  = document.getElementById('recordLabel');
  ring.classList.remove('recording');
  document.querySelector('.voice-wrap')?.classList.remove('recording');

  const transcript = liveText.trim();
  liveText = '';

  if (!transcript) {
    mic.textContent = '🎤'; lbl.textContent = 'Tap to Record';
    document.getElementById('voiceLiveText').classList.add('hidden');
    showToast('No speech detected — try again.');
    return;
  }

  // Processing state
  ring.classList.add('processing');
  mic.textContent = '';
  lbl.textContent = 'Processing…';
  document.getElementById('btnRecord').disabled = true;

  try {
    const result = await api('POST', '/api/transcribe', { transcript });
    ring.classList.remove('processing');
    document.getElementById('btnRecord').disabled = false;
    mic.textContent = '🎤'; lbl.textContent = 'Tap to Record';
    document.getElementById('voiceLiveText').classList.add('hidden');
    showReviewSheet(result);
  } catch (err) {
    ring.classList.remove('processing');
    document.getElementById('btnRecord').disabled = false;
    mic.textContent = '🎤'; lbl.textContent = 'Tap to Record';
    document.getElementById('voiceLiveText').classList.add('hidden');
    showToast('Parse failed: ' + err.message);
  }
}

// ---------------------------------------------------------------------------
// REVIEW SHEET
// ---------------------------------------------------------------------------
function showReviewSheet(result) {
  document.getElementById('rvInspected').value = result.what_inspected   || '';
  document.getElementById('rvIssues').value    = result.issues_found     || '';
  document.getElementById('rvActions').value   = result.actions_required || '';

  const note = document.getElementById('reviewAiNote');
  if (result.ai_parsed) {
    note.className   = 'review-ai-note ai-yes';
    note.textContent = '✓ AI has sorted your narration into the three fields below — edit if needed.';
  } else {
    note.className   = 'review-ai-note ai-no';
    note.textContent = aiAvailable
      ? 'AI parsing unavailable — review and sort the fields manually.'
      : '⚠ No ANTHROPIC_API_KEY set — full transcript placed in first field.';
  }

  document.getElementById('reviewSheet').classList.remove('hidden');
  document.getElementById('reviewBackdrop').classList.remove('hidden');
}

function dismissReview() {
  document.getElementById('reviewSheet').classList.add('hidden');
  document.getElementById('reviewBackdrop').classList.add('hidden');
}

async function confirmNotes(andNext) {
  const body = {
    what_inspected:   document.getElementById('rvInspected').value,
    issues_found:     document.getElementById('rvIssues').value,
    actions_required: document.getElementById('rvActions').value,
  };
  if (photos[currentIdx]) Object.assign(photos[currentIdx], body);
  try { await api('POST', `/api/notes/${sessionId}/${currentIdx}`, body); }
  catch { showToast('Could not save to server.'); }

  dismissReview();

  if (andNext && currentIdx < photos.length - 1) voiceNavigate(1);
  else if (andNext) showToast('Last photo — all notes saved.');
}

// ---------------------------------------------------------------------------
// Template + report
// ---------------------------------------------------------------------------
function onTemplateSelected(input) {
  const label = document.getElementById('templatePickLabel');
  if (input.files[0]) {
    label.textContent = '📄 ' + input.files[0].name;
    label.classList.add('selected');
  } else {
    label.textContent = '📄 Choose Template (.docx)';
    label.classList.remove('selected');
  }
}

function updateReportSummary() {
  const box = document.getElementById('reportSummary');
  if (!photos.length) { box.innerHTML = '<p>No photos loaded yet.</p>'; return; }
  const dups  = photos.filter(p => p.is_duplicate).length;
  const noted = photos.filter(p => p.what_inspected || p.issues_found || p.actions_required).length;
  box.innerHTML = `<b>${photos.length}</b> photos ready &nbsp;·&nbsp; <b>${noted}</b> with notes &nbsp;·&nbsp; <b>${dups}</b> duplicate(s)`;
}

async function generateReport() {
  if (!sessionId || !photos.length) { showToast('No photos loaded.'); return; }
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  await flushNotes();

  const btn = document.getElementById('btnGenerate');
  btn.disabled = true;
  document.getElementById('reportProgress').classList.remove('hidden');
  document.getElementById('reportResult').classList.add('hidden');

  const fd = new FormData();
  fd.append('site_name',      document.getElementById('fSiteName').value);
  fd.append('project_number', document.getElementById('fProjectNum').value);
  fd.append('inspector_name', document.getElementById('fInspector').value);
  fd.append('site_address',   document.getElementById('fAddress').value);
  const tpl = document.getElementById('templateInput');
  if (tpl.files[0]) fd.append('template', tpl.files[0]);

  try {
    const result = await api('POST', `/api/report/${sessionId}`, fd);
    document.getElementById('reportProgress').classList.add('hidden');
    const box = document.getElementById('reportResult');
    box.innerHTML = `
      <h3>✅ Report Ready</h3>
      <p>Generated with all ${photos.length} photos, metadata and inspection notes.</p>
      <a class="btn btn-primary dl-btn" href="${result.download_url}" download="${result.filename}">
        ⬇ Download Full Report (ZIP)
      </a><br><br>
      <a class="btn btn-outline" href="${result.docx_url}" download>⬇ Report Only (.docx)</a>`;
    box.classList.remove('hidden');
  } catch (err) {
    document.getElementById('reportProgress').classList.add('hidden');
    showToast('Report failed: ' + err.message);
  }
  btn.disabled = false;
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
async function api(method, url, body) {
  const opts = { method };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body && typeof body === 'object') {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body    = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (!res.ok) {
    const txt = await res.text().catch(() => res.statusText);
    throw new Error(txt || `HTTP ${res.status}`);
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

function esc(s) {
  return String(s || '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

let toastTimer = null;
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 3500);
}
