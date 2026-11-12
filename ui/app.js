'use strict';
/* Clipping page. Talks to bin/clipui.py on this Mac; every heavy step runs clipkit.py in the background. */

const TOKEN = document.querySelector('meta[name="clip-token"]').content;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pad = (n) => String(n).padStart(2, '0');
const ACTIVE = new Set(['queued', 'running']);

function fmt(sec) {
  if (sec == null || Number.isNaN(+sec)) return '';
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
function parseT(v) {
  const s = String(v ?? '').trim().toLowerCase();
  if (!s) return NaN;
  const m = s.match(/^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$/);
  if (m && (m[1] || m[2] || m[3])) return (+m[1] || 0) * 3600 + (+m[2] || 0) * 60 + (+m[3] || 0);
  const parts = s.split(':');
  if (parts.length <= 3 && parts.every((p) => /^\d+(\.\d+)?$/.test(p))) return parts.reduce((t, p) => t * 60 + parseFloat(p), 0);
  return NaN;
}
const twitchT = (sec) => { sec = Math.max(0, Math.floor(sec)); return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m${sec % 60}s`; };
const day = (t) => new Date(t * 1000).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

async function api(path, body) {
  const opt = body === undefined ? { cache: 'no-store' } : {
    method: 'POST', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-Clip-Token': TOKEN },
    body: JSON.stringify(body),
  };
  let r;
  try { r = await fetch(path, opt); } catch {
    setOffline(true);
    throw new Error('The Clipping app isn’t running. Double-click Clipping.command, then reload this page.');
  }
  setOffline(false);
  let data = null;
  try { data = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}
function setOffline(v) { const el = $('#offline'); if (el) el.hidden = !v; }

function toast(msg, kind = 'info', ms = 4500) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), ms);
}
async function copyText(text, what) {
  try { await navigator.clipboard.writeText(text); toast(`${what} copied`, 'good', 2000); } catch { toast('Couldn’t copy. Select the text and press ⌘C.', 'bad'); }
}

const POST_FIELDS = { title: 'title', yt: 'yt_description', ig: 'ig_caption' };
const POST_NAMES = { title: 'YouTube title', yt: 'YouTube description', ig: 'Instagram caption' };

// ───────────────────────────── state ─────────────────────────────

const S = {
  tab: 'find', status: null, streamers: [], streams: [], stream: null, editor: null,
  clips: [], clipFilter: 'all', dirtyClips: new Set(), jobs: [], jobsOpen: false, openJobs: new Set(),
  findJob: null, jobInfo: {}, dismissed: new Set(), addPrefill: null, flash: null, aiJobs: {},
};
let player = null;
let CAM = null;

const streamerOf = (name) => S.streamers.find((s) => s.name === String(name || '').toLowerCase());
const displayOf = (name) => (streamerOf(name) || {}).display || name || 'Unknown';
const jobById = (id) => S.jobs.find((j) => j.id === id);
const makeJobFor = (ref) => S.jobs.find((j) => j.kind === 'make' && j.meta && j.meta.ref === ref);
function upsertJob(job) { const i = S.jobs.findIndex((j) => j.id === job.id); if (i >= 0) S.jobs[i] = job; else S.jobs.unshift(job); }

// ───────────────────────────── boot ─────────────────────────────

async function init() {
  $$('.tab').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
  $('#find-form').addEventListener('submit', onFind);
  $('#find-opts-btn').addEventListener('click', () => {
    const o = $('#find-opts'); o.hidden = !o.hidden; $('#find-opts-btn').setAttribute('aria-expanded', String(!o.hidden));
  });
  $('#jobs-btn').addEventListener('click', () => { S.jobsOpen = !S.jobsOpen; renderJobs(); });
  document.addEventListener('click', onClick);
  document.addEventListener('submit', onSubmit);
  document.addEventListener('input', onInput);
  document.addEventListener('change', onChange);
  document.addEventListener('toggle', (e) => {
    const d = e.target; if (d.matches && d.matches('details.job')) { if (d.open) S.openJobs.add(d.dataset.id); else S.openJobs.delete(d.dataset.id); }
  }, true);
  window.addEventListener('hashchange', route);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && S.jobsOpen) { S.jobsOpen = false; renderJobs(); } });
  await Promise.allSettled([loadStatus(), loadStreamers(), loadStreams(), loadClips()]);
  route();
  poll();
}

function route() {
  const t = (location.hash || '#find').slice(1);
  showTab(['find', 'clips', 'streamers'].includes(t) ? t : 'find', false);
}

function showTab(name, push = true) {
  S.tab = name;
  $$('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tab-page').forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
  if (push && location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  if (name !== 'find' && player) player.pause();
  if (name === 'find') { renderStreams(); renderMoments(); renderFindJobs(); }
  if (name === 'clips') { renderClips(); loadClips(); }
  if (name === 'streamers') renderStreamers();
}

async function loadStatus() {
  try { S.status = await api('/api/status'); } catch (e) { return; }
  const ai = S.status.ai;
  const pill = $('#ai-pill');
  pill.innerHTML = ai.on ? '<span class="dot on"></span>AI on'
    : ai.paused ? '<span class="dot warn"></span>AI paused' : '<span class="dot"></span>AI off';
  pill.title = ai.on ? `${ai.model}, key from ${ai.key}. Ranks moments and writes the YouTube and Instagram text.`
    : ai.paused ? `${ai.paused}. Until then: moments are ranked by chat and volume, and clips get the plain title and hashtags. Click to try now.`
      : 'Off: moments are ranked by chat and volume, and clips get the plain title and hashtags. See Streamers & setup.';
  if (S.tab === 'streamers' && !document.activeElement.closest?.('#tab-streamers form')) renderStreamers();
}
async function loadStreamers() {
  try { S.streamers = await api('/api/streamers'); } catch (e) { return; }
  const sel = $('#opt-streamer');
  const cur = sel.value;
  sel.innerHTML = '<option value="">Work it out from the link</option>' +
    S.streamers.map((s) => `<option value="${esc(s.name)}">${esc(s.display)}</option>`).join('');
  sel.value = cur;
}
async function loadStreams() {
  try { S.streams = await api('/api/streams'); } catch (e) { return; }
  if (S.tab === 'find') renderStreams();
}
async function loadClips() {
  try { S.clips = await api('/api/clips'); } catch (e) { return; }
  $('#clips-count').textContent = S.clips.length || '';
  if (S.tab !== 'clips') return;
  const playing = $$('#tab-clips video').some((v) => !v.paused);
  if (!S.dirtyClips.size && !playing) renderClips();
  else toast('A new clip is ready. Switch tabs and back to see it.', 'info', 4000);
}

// ───────────────────────────── events ─────────────────────────────

const ACTS = {
  'open-stream': (el) => openStream(el.dataset.key),
  'open-moment': (el) => openMoment(+el.dataset.n),
  'close-editor': () => closeEditor(),
  'dismiss-job': (el) => { S.dismissed.add(el.dataset.id); renderFindJobs(); },
  'stop-job': async (el) => { try { upsertJob(await api(`/api/jobs/${el.dataset.id}/stop`, {})); renderAllJobs(); } catch (e) { toast(e.message, 'bad'); } },
  'reanalyse': (el) => startFind(el.dataset.url, { again: true }),
  'whole-clip': (el) => openClipEditor(JSON.parse(el.dataset.info)),
  'nudge': (el) => { const E = S.editor; if (!E) return; setTime(el.dataset.w, E[el.dataset.w] + +el.dataset.d); },
  'from-player': (el) => {
    const t = player && player.time();
    if (t == null) return toast('Start the video first, then press this at the right moment.', 'info');
    setTime(el.dataset.w, Math.round(t));
  },
  'play-range': () => { const E = S.editor; if (player && E) player.playRange(E.start, E.end); },
  'seek': (el) => { if (player) { player.seek(+el.dataset.t); player.play(); } },
  'layout': (el) => { if (!S.editor || el.disabled) return; S.editor.layout = el.dataset.v; refreshLayout(); },
  'make': () => makeClip(false),
  'make-again': () => makeClip(true),
  'goto-clips': () => showTab('clips'),
  'goto-streamer': (el) => gotoStreamer(el.dataset.name),
  'add-streamer-from': (el) => { S.addPrefill = { name: el.dataset.name || '', youtube: el.dataset.youtube || '' }; showTab('streamers'); setTimeout(() => { const f = $('#add-form'); if (f) { f.scrollIntoView({ behavior: 'smooth', block: 'center' }); $('[name=name]', f).focus(); } }, 50); },
  'reveal': async (el) => { try { await api('/api/reveal', { file: el.dataset.file }); } catch (e) { toast(e.message, 'bad'); } },
  'open': async (el) => { try { await api('/api/open', { what: el.dataset.what }); } catch (e) { toast(e.message, 'bad'); } },
  'copy-result': (el) => {
    const j = S.editor && jobById(S.editor.jobId); const m = j && j.result && j.result.meta; if (!m) return;
    const w = el.dataset.what;
    copyText(w === 'title' ? m.title : w === 'yt' ? m.yt_description : m.ig_caption, POST_NAMES[w]);
  },
  'clip-filter': (el) => { S.clipFilter = el.dataset.v; renderClips(); },
  'clip-copy': (el) => {
    const card = el.closest('.clip'); const w = el.dataset.what;
    copyText($(`[data-f=${POST_FIELDS[w]}]`, card).value.trim(), POST_NAMES[w]);
  },
  'clip-ai': (el) => writeWithAI(el.closest('.clip')),
  'ai-resume': async () => {
    try { S.status = await api('/api/ai/resume', {}); await loadStatus(); toast('The AI will be tried on the next step.', 'good', 3000); } catch (e) { toast(e.message, 'bad'); }
  },
  'goto-setup': () => showTab('streamers'),
  'brand-set': (el) => { if (!el.disabled) setBrand({ [el.dataset.k]: el.dataset.v }); },
  'brand-apply': async () => {
    try { upsertJob(await api('/api/brand/apply', {})); renderBrandApply(true); pollSoon(); } catch (e) { toast(e.message, 'bad'); }
  },
  'cap-open': (el) => openCaptions(el.dataset.file || el.closest('.clip').dataset.file),
  'cap-close': () => closeCaptions(),
  'cap-seek': (el) => { const v = $('#cap-video'); if (v) { v.currentTime = +el.dataset.t; v.play().catch(() => {}); } },
  'cap-replace': () => capReplace(),
  'cap-rule-remove': (el) => capRuleRemove(el.dataset.from, el.dataset.scope),
  'cap-remake': () => capRemake(false),
  'cap-reset': () => { if (confirm('Go back to the captions exactly as they were heard? Your caption edits on this clip are undone.')) capRemake(true); },
  'clip-save': (el) => saveClipText(el.closest('.clip')),
  'log-save': (el) => saveClipLog(el.closest('.clip')),
  'clip-trash': (el) => trashClip(el.closest('.clip')),
  'cam-open': (el) => openCamera(el.dataset.name),
  'cam-from-editor': () => {
    const E = S.editor; if (!E || !E.known) return toast('Pick the streamer first.', 'info');
    const t = player && player.time();
    openCamera(E.streamer, E.link, fmt(t != null ? t : (E.start || 0)));
  },
  'cam-grab': () => grabFrame(),
  'cam-save': () => saveCamera(),
  'cam-none': (el) => cameraNone(el.dataset.name),
  'cam-close': () => { $('#cam-dialog').close(); CAM = null; },
  'record': async (el) => {
    try { upsertJob(await api('/api/record', { name: el.dataset.name })); renderStreamers(); pollSoon(); toast('Recording. Press Stop when you have enough.'); } catch (e) { toast(e.message, 'bad', 7000); }
  },
  'find-file': (el) => startFind(el.dataset.file, { streamer: el.dataset.name, again: true }),
  'close-jobs': () => { S.jobsOpen = false; renderJobs(); },
};

function onClick(e) {
  const el = e.target.closest('[data-act]');
  if (!el) return;
  const fn = ACTS[el.dataset.act];
  if (!fn) return;
  e.preventDefault();
  fn(el, e);
}
function onSubmit(e) {
  const f = e.target;
  if (f.matches('.perm')) { e.preventDefault(); savePermission(f); }
  if (f.id === 'add-form') { e.preventDefault(); addStreamer(f); }
}
function onInput(e) {
  const t = e.target;
  const E = S.editor;
  if (E && t.id === 'ed-hook') E.hook = t.value;
  if (E && t.id === 'ed-title') E.title = t.value;
  if (t.matches('.clip textarea')) {
    const card = t.closest('.clip'); S.dirtyClips.add(card.dataset.file);
    $('[data-act=clip-save]', card).hidden = false;
  }
}
function onChange(e) {
  const t = e.target;
  const E = S.editor;
  if (E && (t.id === 'ed-start' || t.id === 'ed-end')) {
    const v = parseT(t.value);
    if (Number.isNaN(v)) { t.classList.add('bad'); toast('Write the time like 57:12 or 1:02:30.', 'bad'); return; }
    t.classList.remove('bad');
    setTime(t.id === 'ed-start' ? 'start' : 'end', v);
  }
  if (E && t.id === 'ed-keep') E.keepSilence = t.checked;
  if (E && t.id === 'ed-practice') E.practice = t.checked;
  if (E && t.id === 'ed-streamer') { setEditorStreamer(t.value); renderEditor(false); }
  if (t.matches('.checklist input')) {
    const card = t.closest('.clip');
    const done = $$('.checklist input', card).map((x) => x.checked);
    try { localStorage.setItem('checks:' + card.dataset.file, JSON.stringify(done)); } catch { /* fine */ }
    $('.checks-count', card).textContent = `${done.filter(Boolean).length}/${done.length}`;
  }
}

// ───────────────────────────── find ─────────────────────────────

async function onFind(e) {
  e.preventDefault();
  const link = $('#find-link').value.trim();
  if (!link) { $('#find-link').focus(); return; }
  let info;
  try { info = await api('/api/inspect', { link }); } catch (err) { toast(err.message, 'bad', 8000); return; }
  if (info.kind === 'twitch-clip' || info.kind === 'kick-clip') { openClipEditor(info); return; }
  const again = $('#opt-again').checked;
  if (info.analysed && !again) {
    await openStream(info.key);
    toast('You analysed this one before, so here are its moments. To redo it, tick “Analyse again” under Options.', 'info', 7000);
    return;
  }
  startFind(info.link, {
    streamer: $('#opt-streamer').value, from: $('#opt-from').value.trim(), to: $('#opt-to').value.trim(),
    full_audio: $('#opt-full').checked, again, info,
  });
}

async function startFind(link, opts = {}) {
  try {
    const job = await api('/api/find', { link, streamer: opts.streamer || '', from: opts.from || '', to: opts.to || '', full_audio: !!opts.full_audio, again: !!opts.again });
    if (opts.info) S.jobInfo[job.id] = opts.info;
    S.findJob = job.id; upsertJob(job);
    showTab('find'); renderFindJobs(); pollSoon();
    toast('Finding moments. About 2–3 minutes for a 1-hour stream; you can keep working meanwhile.');
  } catch (err) { toast(err.message, 'bad', 8000); }
}

function renderFindJobs() {
  const el = $('#find-jobs');
  const jobs = S.jobs.filter((j) => j.kind === 'find' && (ACTIVE.has(j.status) || (j.status === 'failed' && !S.dismissed.has(j.id))));
  if (!changed(el, jobs.map(jobSig).join('|'))) return;
  el.innerHTML = jobs.map((j) => {
    const last = j.lines.length ? j.lines[j.lines.length - 1] : 'Waiting to start…';
    if (j.status === 'failed') {
      const info = S.jobInfo[j.id];
      const short = /shorter than 2 minutes/i.test(j.error) && info;
      return `<div class="job-card bad"><b>Couldn’t find moments</b><p>${esc(j.error)}</p>
        <div class="row">${short ? `<button class="btn small" data-act="whole-clip" data-info="${esc(JSON.stringify(info))}">Make it one clip</button>` : ''}
        <button class="btn small ghost" data-act="dismiss-job" data-id="${j.id}">Dismiss</button></div></div>`;
    }
    return `<div class="job-card"><div class="row"><div class="spinner"></div><b>${j.status === 'queued' ? 'Waiting for the one before' : 'Finding moments…'}</b></div>
      <p class="mono">${esc(last)}</p>
      <div class="row"><span class="faint small">${esc(j.meta.url || '')}</span><span class="spacer"></span><button class="btn small ghost" data-act="stop-job" data-id="${j.id}">Stop</button></div></div>`;
  }).join('');
}

function renderStreams() {
  const el = $('#streams');
  if (!S.streams.length) { el.innerHTML = '<p class="empty small">Nothing analysed yet. Paste a stream link above.</p>'; return; }
  el.innerHTML = S.streams.map((s) => `
    <button class="stream ${S.stream && S.stream.key === s.key ? 'active' : ''}" data-act="open-stream" data-key="${esc(s.key)}">
      <span class="stream-who">${esc(displayOf(s.streamer))}</span>
      <span class="stream-title">${esc(s.title || s.url)}</span>
      <span class="stream-meta">${esc(s.date)} · ${s.count} moments</span>
    </button>`).join('');
}

async function openStream(key) {
  try { S.stream = await api('/api/moments?key=' + encodeURIComponent(key)); } catch (e) { toast(e.message, 'bad'); return; }
  closeEditor(false);
  if (S.tab !== 'find') showTab('find');
  renderStreams(); renderMoments();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
async function refreshStream() {
  if (!S.stream) return;
  try { S.stream = await api('/api/moments?key=' + encodeURIComponent(S.stream.key)); } catch { return; }
  if (S.tab === 'find') renderMoments();
}

function renderMoments() {
  const el = $('#moments');
  const st = S.stream;
  if (!st) {
    el.innerHTML = `<div class="placeholder"><h2>Find the best moments in a stream</h2><ol>
      <li>Paste a finished Twitch VOD, a YouTube video or a Kick VOD above, then press <b>Find moments</b>.</li>
      <li>Click a moment to watch it right here, and set the exact start and end.</li>
      <li>Add a hook line and press <b>Make clip</b>. It appears under <b>Clips</b>, ready to post.</li></ol>
      <p class="small">Got a single Twitch clip link instead (Videos → Clips → Top 24H)? Paste it above to make it into a vertical clip straight away.</p></div>`;
    return;
  }
  const sm = streamerOf(st.streamer);
  let notes = '';
  if (!sm) {
    notes += `<div class="note warn"><b>${esc(st.streamer)}</b> isn’t in your streamers list, so clips can’t be made yet.
      <button class="link" data-act="add-streamer-from" data-name="${esc(st.streamer)}">Add them</button>, or pick a streamer in the editor.</div>`;
  } else if (!sm.permission.ok) {
    notes += `<div class="note warn">${esc(sm.display)}’s permission isn’t recorded, so clips from this stream are <b>practice clips</b> (not for posting).
      <button class="link" data-act="goto-streamer" data-name="${esc(sm.name)}">Record permission</button></div>`;
  }
  if (st.partial) {
    notes += `<div class="note">This stream was analysed before the page existed, so transcripts and chat aren’t shown.
      <button class="link" data-act="reanalyse" data-url="${esc(st.url)}">Analyse it again</button> (about 2 minutes, reuses the download) to get them.</div>`;
  }
  el.innerHTML = `
    <div class="mom-head"><div><h2>${esc(sm ? sm.display : st.display || st.streamer)}</h2>
      <a href="${esc(st.url)}" target="_blank" rel="noopener">${esc(st.title || st.url)}</a></div>
      <div class="mom-meta">${esc(st.ranking || '')}</div></div>
    ${notes}
    <div class="cards">${st.moments.map((m) => momentCard(st, m)).join('')}</div>`;
}

function momentCard(st, m) {
  const E = S.editor;
  const active = E && E.key === st.key && E.n === m.n;
  const sig = [];
  if (m.chat_x) sig.push(`chat ${(+m.chat_x).toFixed(1)}× normal`);
  if (m.loud_db != null) sig.push(`volume +${Math.round(m.loud_db)} dB`);
  const j = makeJobFor(`${st.key}#${m.n}`);
  const badges = [];
  if (m.score) badges.push(`<span class="score">${esc(m.score)}/10</span>`);
  if (m.spoiler) badges.push('<span class="badge bad">Possible spoiler</span>');
  if (j && ACTIVE.has(j.status)) badges.push('<span class="badge busy">Making…</span>');
  else if (m.made && m.made.length) badges.push(`<span class="badge good">${m.made.every((x) => x.practice) ? 'Practice made' : 'Clip made'}</span>`);
  const said = (m.said || []).filter(([t]) => t >= m.start - 2 && t <= m.end).slice(0, 2).map(([, x]) => `<q>“${esc(x)}”</q>`).join('');
  return `<article class="mcard ${active ? 'active' : ''}" data-act="open-moment" data-n="${m.n}">
    <div class="mcard-top"><span class="rank">${m.n}</span><h3>${esc(m.title || 'Moment at ' + fmt(m.start))}</h3>${badges.join('')}</div>
    <div class="mcard-time">${fmt(m.start)} → ${fmt(m.end)} · ${Math.round(m.end - m.start)} s${sig.length ? ' · ' + sig.join(' · ') : ''}</div>
    ${m.why ? `<p class="why">${esc(m.why)}</p>` : ''}
    ${said ? `<div class="said">${said}</div>` : ''}
    ${m.chat && m.chat.length ? `<div class="chat">💬 ${m.chat.slice(0, 4).map(esc).join(' · ')}</div>` : ''}
  </article>`;
}

// ───────────────────────────── editor ─────────────────────────────

function setEditorStreamer(name) {
  const E = S.editor; const sm = streamerOf(name);
  E.streamer = sm ? sm.name : (name || '');
  E.known = !!sm;
  E.permissionOk = !!(sm && sm.permission.ok);
  E.camera = !!(sm && sm.layout.mode === 'camera_top');
  E.layout = E.camera ? 'camera_top' : 'full';
}

function openMoment(n) {
  const st = S.stream; if (!st) return;
  const m = st.moments.find((x) => x.n === n); if (!m) return;
  if (S.editor && S.editor.key === st.key && S.editor.n === n) return;
  const ref = `${st.key}#${n}`;
  const j = makeJobFor(ref);
  S.editor = {
    mode: 'moment', key: st.key, n, ref, src: { kind: st.kind, id: st.id, url: st.url }, link: st.url,
    // without AI ranking, a moment's "title" is just its first spoken line: leave the title to the AI instead
    start: m.start, end: m.end, hook: m.hook || '', title: st.ai ? (m.title || '') : '', keepSilence: false, practice: false,
    said: m.said || [], spoiler: !!m.spoiler, jobId: j ? j.id : null, heading: m.title || `Moment at ${fmt(m.start)}`,
  };
  setEditorStreamer(st.streamer);
  renderEditor();
  $$('.mcard').forEach((c) => c.classList.toggle('active', +c.dataset.n === n));
}

function openClipEditor(info) {
  S.stream = null;
  S.editor = {
    mode: 'clip', key: info.key, n: null, ref: info.key, src: { kind: info.kind, id: info.id, url: info.url }, link: info.link || info.url,
    start: null, end: null, hook: '', title: '', keepSilence: false, practice: false, said: [], spoiler: false,
    jobId: (makeJobFor(info.key) || {}).id || null, heading: 'Make a vertical clip from this clip',
  };
  setEditorStreamer(info.streamer || '');
  if (S.tab !== 'find') showTab('find');
  renderStreams(); renderMoments(); renderEditor();
}

function closeEditor(rerender = true) {
  if (player) { player.destroy(); player = null; }
  S.editor = null;
  $('#editor').className = 'editor';
  $('#editor').innerHTML = '';
  $('#find-grid').classList.remove('has-editor');
  if (rerender) $$('.mcard.active').forEach((c) => c.classList.remove('active'));
}

// The head and the video player are drawn once per moment; everything below them can be redrawn freely
// (redrawing an embedded player would reload it).
function renderEditor(full = true) {
  const E = S.editor; const el = $('#editor');
  if (!E) { closeEditor(); return; }
  if (full || !$('#ed-body')) {
    if (player) { player.destroy(); player = null; }
    el.className = 'editor open';
    $('#find-grid').classList.add('has-editor');
    el.innerHTML = `
      <div class="ed-head"><h2>${esc(E.heading)}</h2><button class="icon-btn" data-act="close-editor" aria-label="Close" title="Close">✕</button></div>
      <div class="player-wrap"><div id="player-host"></div></div>
      <div id="ed-body"></div>`;
    player = new Player($('#player-host'), E.src, E.start || 0);
    el.scrollTop = 0;
  }
  renderEditorBody();
}

function renderEditorBody() {
  const E = S.editor;
  const sm = streamerOf(E.streamer);
  const showPicker = E.mode === 'clip' || !E.known;
  $('#ed-body').innerHTML = `
    ${E.mode === 'moment' ? `
    <div class="times">
      <div class="tbox"><label for="ed-start">Start</label>
        <div class="tline"><button class="nudge" data-act="nudge" data-w="start" data-d="-1" title="1 second earlier">−1</button><input id="ed-start" class="tinput" value="${fmt(E.start)}"><button class="nudge" data-act="nudge" data-w="start" data-d="1" title="1 second later">+1</button></div>
        <button class="btn small ghost" data-act="from-player" data-w="start">Start = video’s time now</button></div>
      <div class="tbox"><label for="ed-end">End</label>
        <div class="tline"><button class="nudge" data-act="nudge" data-w="end" data-d="-1" title="1 second earlier">−1</button><input id="ed-end" class="tinput" value="${fmt(E.end)}"><button class="nudge" data-act="nudge" data-w="end" data-d="1" title="1 second later">+1</button></div>
        <button class="btn small ghost" data-act="from-player" data-w="end">End = video’s time now</button></div>
      <div class="tdur"><span class="len" id="ed-len"></span><span class="faint small" id="ed-len-hint"></span><span class="spacer"></span><button class="btn small" data-act="play-range">▶ Play this part</button></div>
    </div>` : '<p class="muted small">The whole clip is used. Pauses longer than 0.8 s are cut unless you tick “Keep pauses”.</p>'}
    ${showPicker ? `<div class="field"><label for="ed-streamer">Streamer</label>
      <select id="ed-streamer"><option value="">Pick the streamer…</option>${S.streamers.map((s) => `<option value="${esc(s.name)}" ${s.name === E.streamer ? 'selected' : ''}>${esc(s.display)}</option>`).join('')}</select>
      <span class="small muted">Not in the list? <button class="link" data-act="add-streamer-from" data-name="${esc(E.known ? '' : E.streamer)}">Add a streamer</button></span></div>` : ''}
    <div class="field"><label for="ed-hook">Hook <span class="faint">(big line at the top for the whole clip; up to ~8 words)</span></label>
      <input id="ed-hook" maxlength="80" value="${esc(E.hook)}" placeholder="e.g. Kai finally beat GTA 5"></div>
    <div class="field"><label for="ed-title">YouTube title <span class="faint">(optional; you can change it later)</span></label>
      <input id="ed-title" maxlength="150" value="${esc(E.title)}" placeholder="${S.status && S.status.ai.on ? 'Leave empty and the AI writes it, with the descriptions' : 'Leave empty to use the moment’s first line'}"></div>
    <div class="field-row">
      <div class="seg" role="group" aria-label="Layout">
        <button class="seg-btn" data-act="layout" data-v="camera_top">Camera on top</button>
        <button class="seg-btn" data-act="layout" data-v="full">Full frame</button>
      </div>
      <button class="link small" data-act="cam-from-editor" id="cam-link-btn"></button>
    </div>
    <div class="ed-checks">
      <label class="check"><input type="checkbox" id="ed-keep" ${E.keepSilence ? 'checked' : ''}> Keep pauses (don’t cut silences)</label>
      ${practiceBlock(sm)}
      ${E.spoiler ? '<div class="note bad">Flagged as a possible GTA 6 story spoiler. Don’t post story moments.</div>' : ''}
    </div>
    <div class="ed-actions"><button class="btn primary big" data-act="make" id="make-btn">Make clip</button></div>
    <div id="ed-job"></div>
    ${E.mode === 'moment' ? transcriptBlock() : ''}`;
  refreshTimes(); refreshLayout(); renderEditorJob(true);
}

function practiceBlock(sm) {
  const E = S.editor;
  if (!E.known) return '<div class="note warn">Pick the streamer above to make the clip.</div>';
  if (!E.permissionOk) {
    return `<div class="note">This will be a <b>practice clip</b> (saved in out/practice, not for posting): ${esc(sm.display)}’s permission isn’t recorded.
      <button class="link" data-act="goto-streamer" data-name="${esc(sm.name)}">Record it</button></div>`;
  }
  return `<label class="check"><input type="checkbox" id="ed-practice" ${E.practice ? 'checked' : ''}> Practice clip (not logged, not for posting)</label>`;
}

function transcriptBlock() {
  const E = S.editor;
  const rows = E.said.map(([t, x]) => `<button class="tr-line" data-act="seek" data-t="${t}"><span class="tr-t">${fmt(t)}</span><span>${esc(x)}</span></button>`).join('');
  return `<div class="transcript"><div class="tr-head">What’s said around here <span class="faint small">(click a line to jump the video there)</span></div>
    ${rows || '<p class="muted small">No transcript for this moment.</p>'}</div>`;
}

function setTime(which, v) {
  const E = S.editor; if (!E || Number.isNaN(v)) return;
  E[which] = Math.max(0, Math.round(v));
  refreshTimes();
}

function refreshTimes() {
  const E = S.editor; if (!E || E.mode !== 'moment') return;
  const s = $('#ed-start'), e = $('#ed-end');
  if (s && document.activeElement !== s) s.value = fmt(E.start);
  if (e && document.activeElement !== e) e.value = fmt(E.end);
  const len = Math.round(E.end - E.start);
  const lenEl = $('#ed-len'), hint = $('#ed-len-hint');
  if (lenEl) {
    lenEl.textContent = len > 0 ? `${len} s` : '—';
    let h = '';
    if (len <= 0) h = 'the end must be after the start';
    else if (len > 180) h = 'too long: 3 minutes at most';
    else if (len > 60) h = 'long: 20–45 s usually does better';
    else if (len < 8) h = 'very short';
    lenEl.classList.toggle('warn', !!h);
    hint.textContent = h;
  }
  $$('.tr-line').forEach((b) => { const t = +b.dataset.t; b.classList.toggle('in', t >= E.start - 1 && t < E.end); });
}

function refreshLayout() {
  const E = S.editor; if (!E) return;
  $$('.seg-btn[data-act=layout]').forEach((b) => {
    b.classList.toggle('on', b.dataset.v === E.layout);
    b.disabled = b.dataset.v === 'camera_top' && !E.camera;
    b.title = b.disabled ? 'Set the camera box first' : '';
  });
  const l = $('#cam-link-btn');
  if (l) { l.textContent = E.camera ? 'Change camera box' : 'Set camera box'; l.hidden = !E.known; }
}

async function makeClip(again) {
  const E = S.editor; if (!E) return;
  if (!E.known) { toast('Pick the streamer first.', 'bad'); return; }
  if (E.mode === 'moment') {
    if (!(E.end > E.start)) { toast('The end must be after the start.', 'bad'); return; }
    if (E.end - E.start > 180) { toast('Clips can be 3 minutes at most.', 'bad'); return; }
  }
  try {
    const job = await api('/api/make', {
      link: E.link, start: E.mode === 'moment' ? E.start : null, end: E.mode === 'moment' ? E.end : null,
      streamer: E.streamer, hook: (E.hook || '').trim(), title: (E.title || '').trim(), layout: E.layout,
      keep_silence: E.keepSilence, practice: !E.permissionOk || E.practice, again, ref: E.ref,
    });
    E.jobId = job.id; upsertJob(job);
    renderEditorJob(); if (S.stream) renderMoments(); pollSoon();
  } catch (e) { toast(e.message, 'bad', 8000); }
}

function renderEditorJob(force = false) {
  const box = $('#ed-job'); const E = S.editor; if (!box || !E) return;
  const btn = $('#make-btn');
  const j = E.jobId && jobById(E.jobId);
  if (!changed(box, jobSig(j)) && !force) return;
  if (!j) { box.innerHTML = ''; btn.disabled = false; btn.textContent = 'Make clip'; return; }
  if (ACTIVE.has(j.status)) {
    btn.disabled = true;
    btn.textContent = j.status === 'queued' ? 'Waiting for the clip before it…' : 'Making the clip…';
    const lines = j.lines.slice(-5);
    box.innerHTML = `<div class="progress"><div class="spinner"></div>
      <ul>${(lines.length ? lines : ['Waiting to start…']).map((l) => `<li>${esc(l)}</li>`).join('')}</ul>
      <button class="btn small ghost" data-act="stop-job" data-id="${j.id}">Stop</button></div>`;
    return;
  }
  btn.disabled = false;
  btn.textContent = j.status === 'done' ? 'Make it again with these settings' : 'Make clip';
  if (j.status === 'stopped') { box.innerHTML = '<div class="note">Stopped.</div>'; return; }
  if (j.status === 'failed') {
    const dup = /already made a clip from this part/i.test(j.error);
    box.innerHTML = `<div class="note bad"><b>${dup ? 'You already made a clip from this part.' : 'That didn’t work.'}</b><pre>${esc(j.error)}</pre>
      ${dup ? '<button class="btn small" data-act="make-again">Make it anyway</button>' : ''}</div>`;
    return;
  }
  const r = j.result || {}; const m = r.meta || {};
  if (!r.url) { box.innerHTML = '<div class="note">Done. Find it under Clips.</div>'; return; }
  box.innerHTML = `<div class="result">
    <video src="${esc(r.url)}" controls playsinline preload="metadata"></video>
    <div class="result-side"><b>✓ Clip ready</b>
      <span class="muted small">${Math.round(m.seconds || 0)} s · 1080×1920${m.practice ? ' · practice' : ''}${m.ai_text ? ' · text by AI' : ''}</span>
      ${m.title ? `<span class="small">“${esc(m.title)}”</span>` : ''}
      <div class="row"><button class="btn small" data-act="cap-open" data-file="${esc(r.file)}">Edit captions</button></div>
      <div class="row"><button class="btn small ghost" data-act="copy-result" data-what="title">Copy YouTube title</button>
        <button class="btn small ghost" data-act="copy-result" data-what="yt">Copy description</button>
        <button class="btn small ghost" data-act="copy-result" data-what="ig">Copy Instagram caption</button></div>
      <div class="row"><button class="btn small ghost" data-act="reveal" data-file="${esc(r.file)}">Show in Finder</button><button class="btn small ghost" data-act="goto-clips">Open in Clips</button></div>
      ${m.spoiler ? '<div class="note bad small">The AI thinks this may show GTA 6 story. Don’t post story moments.</div>' : ''}
      <p class="faint small">Watch it on your phone before posting. To redo it, change the hook or times above and press the button again; the download is reused.</p></div></div>`;
}

// ───────────────────────────── player ─────────────────────────────

const scripts = {};
function loadScript(src) {
  return scripts[src] || (scripts[src] = new Promise((res, rej) => {
    const s = document.createElement('script'); s.src = src; s.async = true; s.onload = res;
    s.onerror = () => { delete scripts[src]; rej(new Error('Couldn’t load the video player (are you online?)')); };
    document.head.appendChild(s);
  }));
}
let ytPromise = null;
function ytApi() {
  if (window.YT && window.YT.Player) return Promise.resolve();
  return ytPromise || (ytPromise = new Promise((res, rej) => {
    const prev = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => { if (prev) prev(); res(); };
    loadScript('https://www.youtube.com/iframe_api').catch((e) => { ytPromise = null; rej(e); });
  }));
}

class Player {
  constructor(host, src, start) {
    this.host = host; this.src = src; this.p = null; this.kind = null; this.timer = null; this.dead = false;
    this.mount(start).catch((e) => this.fail(e.message));
  }
  async mount(start) {
    const k = this.src.kind;
    const div = document.createElement('div');
    div.id = 'pl-' + Math.random().toString(36).slice(2);
    this.host.replaceChildren(div);
    if (k === 'youtube') {
      await ytApi(); if (this.dead) return;
      await new Promise((res) => {
        this.p = new YT.Player(div.id, {
          videoId: this.src.id, width: '100%', height: '100%',
          playerVars: { start: Math.floor(start), rel: 0, playsinline: 1, modestbranding: 1, origin: location.origin },
          events: { onReady: () => res(), onError: (e) => { this.fail(`YouTube won’t play this video here (error ${e.data}).`); res(); } },
        });
      });
      this.kind = 'yt';
    } else if (k === 'twitch') {
      await loadScript('https://player.twitch.tv/js/embed/v1.js'); if (this.dead) return;
      this.p = new Twitch.Player(div.id, {
        video: this.src.id, parent: [location.hostname], time: twitchT(start), autoplay: false, width: '100%', height: '100%',
      });
      this.kind = 'tw';
    } else if (k === 'twitch-clip') {
      div.innerHTML = `<iframe src="https://clips.twitch.tv/embed?clip=${encodeURIComponent(this.src.id)}&parent=${encodeURIComponent(location.hostname)}&autoplay=false" allowfullscreen></iframe>`;
      this.kind = 'iframe';
    } else {
      this.fail('No preview for this kind of link here.');
    }
  }
  fail(msg) {
    if (this.dead) return;
    this.kind = null;
    const t = S.editor && S.editor.start != null ? S.editor.start : 0;
    const href = this.src.kind === 'youtube' ? `https://www.youtube.com/watch?v=${this.src.id}&t=${Math.floor(t)}s`
      : this.src.kind === 'twitch' ? `${this.src.url}?t=${twitchT(t)}` : this.src.url;
    this.host.innerHTML = `<div class="player-msg"><div>${esc(msg)}<br><a href="${esc(href)}" target="_blank" rel="noopener">Watch it in a new tab</a>
      <div class="faint small">You can still type the start and end times below.</div></div></div>`;
  }
  time() {
    try {
      if (this.kind === 'yt' && this.p.getCurrentTime) return this.p.getCurrentTime();
      if (this.kind === 'tw') return this.p.getCurrentTime();
    } catch { /* not ready */ }
    return null;
  }
  seek(t) { try { if (this.kind === 'yt') this.p.seekTo(t, true); else if (this.kind === 'tw') this.p.seek(t); } catch { /* not ready */ } }
  play() { try { if (this.kind === 'yt') this.p.playVideo(); else if (this.kind === 'tw') this.p.play(); } catch { /* not ready */ } }
  pause() { try { if (this.kind === 'yt') this.p.pauseVideo(); else if (this.kind === 'tw') this.p.pause(); } catch { /* not ready */ } }
  playRange(a, b) {
    if (!this.kind || this.kind === 'iframe') { toast('This player can’t jump to a time. Use the link to watch it.', 'info'); return; }
    this.seek(a); this.play();
    clearInterval(this.timer);
    this.timer = setInterval(() => { const t = this.time(); if (t != null && t >= b) { this.pause(); clearInterval(this.timer); } }, 200);
  }
  destroy() {
    this.dead = true; clearInterval(this.timer);
    try { if (this.kind === 'yt' && this.p.destroy) this.p.destroy(); } catch { /* gone */ }
    this.host.replaceChildren();
  }
}

// ───────────────────────────── clips ─────────────────────────────

function renderClips() {
  const el = $('#tab-clips');
  const f = S.clipFilter;
  const list = S.clips.filter((c) => f === 'all' || (f === 'practice' ? c.practice : !c.practice));
  const n = { all: S.clips.length, post: S.clips.filter((c) => !c.practice).length, practice: S.clips.filter((c) => c.practice).length };
  const seg = [['all', 'All'], ['post', 'To post'], ['practice', 'Practice']]
    .map(([v, l]) => `<button class="seg-btn ${f === v ? 'on' : ''}" data-act="clip-filter" data-v="${v}">${l} ${n[v]}</button>`).join('');
  el.innerHTML = `<div class="page-head"><h1>Clips</h1><div class="seg">${seg}</div><span class="spacer"></span>
      <button class="btn ghost small" data-act="open" data-what="log">Open log.csv</button>
      <button class="btn ghost small" data-act="open" data-what="out">Open clips folder</button></div>
    ${list.length ? `<div class="clip-list">${list.map(clipCard).join('')}</div>` : '<p class="empty">No clips here yet. Make one from Find moments.</p>'}`;
  S.dirtyClips.clear();
}

function clipCard(c) {
  const m = c.meta || {};
  const items = (S.status ? S.status.checklist : []).concat(['Hook line at the top']);
  let done = [];
  try { done = JSON.parse(localStorage.getItem('checks:' + c.file) || '[]'); } catch { /* fine */ }
  const checks = items.map((x, i) => `<label><input type="checkbox" ${done[i] ? 'checked' : ''}> ${esc(x)}</label>`).join('');
  const when = m.start != null ? `${fmt(m.start)}${m.end != null ? '–' + fmt(m.end) : ''}` : '';
  return `<article class="clip" data-file="${esc(c.file)}">
    <video src="${esc(c.url)}#t=0.1" controls playsinline preload="metadata"></video>
    <div class="clip-body">
      <div class="clip-head"><h3>${esc(m.display || m.streamer || c.name)}${when ? ' · ' + when : ''}</h3>
        ${c.practice ? '<span class="badge">Practice</span>' : '<span class="badge good">To post</span>'}
        ${c.log && (c.log.youtube_url || c.log.instagram_url) ? '<span class="badge good">Posted</span>' : ''}
        ${m.spoiler ? '<span class="badge bad" title="The AI thinks this may show GTA 6 story">Possible spoiler</span>' : ''}</div>
      <div class="faint small">${esc(c.name)} · made ${esc(day(c.mtime))}${m.seconds ? ` · ${Math.round(m.seconds)} s` : ''} · ${c.size_mb} MB</div>
      <div class="row clip-tools">
        <button class="btn small" data-act="cap-open" ${c.editable ? '' : 'disabled title="Made before clips kept their details: make it again to edit its captions"'}>Edit captions</button>
        <button class="btn small" data-act="clip-ai" ${c.editable ? (S.aiJobs[c.file] ? 'disabled' : '') : 'disabled title="Made before clips kept their details"'}>${S.aiJobs[c.file] ? 'Writing with AI…' : '✨ Write with AI'}</button>
        ${m.ai_text ? '<span class="faint small">text written by AI</span>' : ''}
      </div>
      ${postField('title', 'YouTube title', m.title, 2, 'A short title a stranger would click')}
      ${postField('yt', 'YouTube description', m.yt_description, 5)}
      ${postField('ig', 'Instagram caption', m.ig_caption, 5)}
      <div class="row"><button class="btn small primary" data-act="clip-save" hidden>Save changes</button></div>
      <details class="box"><summary>Before posting <span class="checks-count faint">${done.filter(Boolean).length}/${items.length}</span></summary>
        <div class="inner checklist">${checks}</div></details>
      ${c.practice ? '' : logBlock(c)}
      <div class="row end">
        <button class="btn small ghost" data-act="reveal" data-file="${esc(c.file)}">Show in Finder</button>
        <button class="btn small ghost danger" data-act="clip-trash">Delete</button>
      </div>
    </div></article>`;
}

function postField(what, label, value, rows, placeholder = '') {
  return `<div class="post-field"><div class="post-head"><label class="lbl">${label}</label>
      <button class="btn small ghost" data-act="clip-copy" data-what="${what}">Copy</button></div>
    <textarea data-f="${POST_FIELDS[what]}" rows="${rows}" placeholder="${esc(placeholder)}">${esc(value || '')}</textarea></div>`;
}

async function writeWithAI(card) {
  const file = card.dataset.file;
  const ai = S.status && S.status.ai;
  if (ai && !ai.on) { toast(ai.paused ? `${ai.paused}.` : 'The AI is off. See Streamers & setup.', 'bad', 6000); return; }
  if (S.dirtyClips.has(file) && !confirm('Replace your unsaved changes with new text from the AI?')) return;
  try {
    const job = await api('/api/clip/posttext', { file });
    upsertJob(job); S.aiJobs[file] = job.id; setAiBusy(card, true); pollSoon();
  } catch (e) { toast(e.message, 'bad', 7000); }
}
function setAiBusy(card, on) {
  const b = card && $('[data-act=clip-ai]', card); if (!b) return;
  b.disabled = on; b.textContent = on ? 'Writing with AI…' : '✨ Write with AI';
}
function clipAiDone(j) {
  const file = j.meta.file; delete S.aiJobs[file];
  const card = $(`.clip[data-file="${CSS.escape(file)}"]`);
  setAiBusy(card, false);
  if (j.status !== 'done') { toast(`The AI text didn’t work: ${j.error}`, 'bad', 8000); loadStatus(); return; }
  const m = j.result && j.result.meta; if (!m) return;
  const c = S.clips.find((x) => x.file === file); if (c) c.meta = m;
  if (card) {
    $('[data-f=title]', card).value = m.title || ''; $('[data-f=yt_description]', card).value = m.yt_description || '';
    $('[data-f=ig_caption]', card).value = m.ig_caption || '';
    S.dirtyClips.delete(file); $('[data-act=clip-save]', card).hidden = true;
  }
  toast('New YouTube and Instagram text written. Read it before posting.', 'good', 4000);
}

const LOG_FIELDS = [
  ['youtube_url', 'YouTube link', 'wide'], ['instagram_url', 'Instagram link', 'wide'],
  ['views_24h', 'Views after 24 h'], ['views_7d', 'Views after 7 days'],
  ['avg_pct_watched', 'Average % watched'], ['viewed_vs_swiped', 'Viewed vs swiped away'],
  ['followers_gained', 'Followers gained'], ['shares', 'Shares'], ['notes', 'Notes', 'wide'],
];
function logBlock(c) {
  const log = c.log || {};
  return `<details class="box"><summary>After posting: links and results <span class="faint small">(saved in log.csv)</span></summary>
    <div class="inner"><div class="grid2">${LOG_FIELDS.map(([k, l, w]) => `<label class="${w || ''}">${l}<input data-log="${k}" value="${esc(log[k] || '')}"></label>`).join('')}</div>
    <div class="row"><button class="btn small primary" data-act="log-save">Save results</button><span class="faint small">Fill views on Sundays, then make more of what brings follows.</span></div></div></details>`;
}

async function saveClipText(card) {
  const file = card.dataset.file;
  try {
    const r = await api('/api/clip/save', {
      file, title: $('[data-f=title]', card).value, yt_description: $('[data-f=yt_description]', card).value,
      ig_caption: $('[data-f=ig_caption]', card).value,
    });
    const c = S.clips.find((x) => x.file === file); if (c) c.meta = r.meta;
    S.dirtyClips.delete(file); $('[data-act=clip-save]', card).hidden = true;
    toast('Saved to the clip’s .txt', 'good', 2000);
  } catch (e) { toast(e.message, 'bad'); }
}
async function saveClipLog(card) {
  const fields = {};
  $$('[data-log]', card).forEach((i) => { fields[i.dataset.log] = i.value; });
  try {
    const r = await api('/api/clip/log', { file: card.dataset.file, fields });
    const c = S.clips.find((x) => x.file === card.dataset.file); if (c) c.log = r.log;
    toast('Saved to log.csv', 'good', 2000);
  } catch (e) { toast(e.message, 'bad'); }
}
async function trashClip(card) {
  const c = S.clips.find((x) => x.file === card.dataset.file); if (!c) return;
  if (!confirm(`Delete ${c.name}?\n\nIt goes to the Bin with its .txt, so you can still get it back.`)) return;
  try {
    await api('/api/clip/trash', { file: c.file });
    S.clips = S.clips.filter((x) => x.file !== c.file); S.dirtyClips.delete(c.file);
    renderClips(); $('#clips-count').textContent = S.clips.length || '';
    refreshStream();
    toast('Moved to the Bin', 'good', 2000);
  } catch (e) { toast(e.message, 'bad'); }
}

// ───────────────────────────── streamers ─────────────────────────────

const SOURCES = [
  ['none', 'Not checked yet'], ['whop_campaign', 'Paid clipping campaign on Whop'],
  ['written', 'They or their team said yes (DM or email)'], ['public_statement', 'Their bio, panels or Discord welcomes clips'],
];

function renderStreamers() {
  const el = $('#tab-streamers');
  el.innerHTML = `<div class="page-head"><h1>Streamers &amp; setup</h1><span class="spacer"></span>
      <button class="btn ghost small" data-act="open" data-what="streamers">Open streamers.yaml</button>
      <button class="btn ghost small" data-act="open" data-what="config">Open config.yaml</button>
      <button class="btn ghost small" data-act="open" data-what="folder">Open Clipping folder</button></div>
    ${setupBlock()}
    <section class="brand-card" id="brand-card"></section>
    <div class="streamer-grid">${S.streamers.map(streamerCard).join('')}${addCard()}</div>`;
  renderBrand();
  if (!S.brand) loadBrand();
  if (S.flash) {
    const card = document.getElementById('s-' + S.flash);
    if (card) { card.classList.add('flash'); card.scrollIntoView({ behavior: 'smooth', block: 'center' }); setTimeout(() => card.classList.remove('flash'), 1800); }
    S.flash = null;
  }
}

function setupBlock() {
  const st = S.status; if (!st) return '';
  const row = (ok, title, detail) => `<div class="setup-row"><span class="${ok ? 'ok' : 'no'}">${ok ? '✓' : '!'}</span><div>${title}<small>${detail}</small></div></div>`;
  const ai = st.ai;
  const aiDetail = ai.on ? `${esc(ai.model)} · key from ${esc(ai.key)}. Ranks moments and writes the YouTube and Instagram text.`
    : ai.paused ? `${esc(ai.paused)}. It's tried again by itself then. <button class="link" data-act="ai-resume">Try AI now</button>`
      : ai.provider === 'none' ? 'Off in config.yaml (rank.provider: none). Moments are ranked by chat and volume.'
        : 'No key found. Put your OpenCode Go key in Keychain (see README) or in the .env file as opencode_go_key=…';
  return `<div class="setup"><div class="setup-rows">
    ${row(st.ffmpeg, 'Video tools', st.ffmpeg ? 'ffmpeg with captions' : 'ffmpeg-full is missing: brew install ffmpeg-full')}
    ${row(st.model, 'Caption model', st.model ? 'Parakeet v3, runs on your Mac' : 'Not downloaded yet. Run clip check in Terminal.')}
    ${row(ai.on, 'AI', aiDetail)}
    ${row(st.disk_free_gb > 20, 'Disk space', `${st.disk_free_gb} GB free`)}
  </div></div>`;
}

function streamerCard(s) {
  const p = s.permission;
  const rec = S.jobs.find((j) => j.kind === 'record' && j.meta.streamer === s.name);
  const recording = rec && ACTIVE.has(rec.status);
  const recFile = rec && !recording && rec.result && rec.result.file;
  return `<article class="scard" id="s-${esc(s.name)}">
    <div class="scard-head"><div><h3>${esc(s.display)}</h3>
      <a class="small" href="${esc(s.channel)}" target="_blank" rel="noopener">${esc(s.channel)}</a>${s.youtube ? ` <span class="faint small">· YouTube ${esc(s.youtube)}</span>` : ''}</div>
      ${p.ok ? '<span class="badge good">Clips allowed</span>' : '<span class="badge warn">Permission not recorded</span>'}</div>
    <div class="scard-body">
      <div class="cam">${s.layout.preview ? `<img src="${esc(s.layout.preview)}" alt="How ${esc(s.display)}’s clips look">` : '<div class="cam-empty">Full frame<br>no camera box</div>'}
        <button class="btn small" data-act="cam-open" data-name="${esc(s.name)}">${s.layout.mode === 'camera_top' ? 'Change camera' : 'Set camera'}</button>
        ${s.layout.mode === 'camera_top' ? `<button class="btn small ghost" data-act="cam-none" data-name="${esc(s.name)}">No camera</button>` : ''}</div>
      <form class="perm" data-name="${esc(s.name)}">
        <div class="lbl">Permission to clip</div>
        <select name="source" aria-label="Where the permission comes from">${SOURCES.map(([v, l]) => `<option value="${v}" ${p.source === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
        <input name="proof" placeholder="Proof: a link or screenshot path you could show" value="${esc(p.proof)}" aria-label="Proof">
        <div class="row"><label class="small muted">Checked on <input name="checked_on" type="date" value="${esc(p.checked_on)}"></label>
          <label class="check small"><input type="checkbox" name="monetize" ${p.monetize ? 'checked' : ''}> Money from clips allowed</label></div>
        <div class="row"><button class="btn small primary" type="submit">Save permission</button></div>
      </form>
    </div>
    <div class="scard-foot"><span class="muted small">Credit: ${esc(s.credit)}</span><span class="spacer"></span>
      ${recording ? `<span class="rec-on small">Recording</span><button class="btn small" data-act="stop-job" data-id="${rec.id}">Stop</button>`
        : `<button class="btn small ghost" data-act="record" data-name="${esc(s.name)}" title="Records their live stream to inbox/live (volume only, no chat)">Record live</button>`}
      ${recFile ? `<button class="btn small" data-act="find-file" data-file="${esc(recFile)}" data-name="${esc(s.name)}">Find moments in the recording</button>` : ''}</div>
    ${s.notes ? `<p class="notes small">${esc(s.notes)}</p>` : ''}
  </article>`;
}

function addCard() {
  const f = S.addPrefill || {};
  S.addPrefill = null;
  return `<article class="scard add-card"><h3>Add a streamer</h3>
    <form id="add-form"><div class="grid2">
      <label>Short name<input name="name" placeholder="kaicenat" value="${esc(f.name || '')}" required></label>
      <label>Display name<input name="display" placeholder="Kai Cenat"></label>
      <label class="wide">Twitch or Kick channel link<input name="channel" placeholder="https://www.twitch.tv/kaicenat" required></label>
      <label>YouTube handle <span class="faint">(optional)</span><input name="youtube" placeholder="@KaiCenatLive" value="${esc(f.youtube || '')}"></label>
      <label>Credit handle<input name="credit" placeholder="@kaicenat"></label>
      <label class="wide">Notes <span class="faint">(optional)</span><input name="notes"></label>
    </div><div class="row"><button class="btn small primary" type="submit">Add streamer</button>
      <span class="faint small">Look for streamers with a clipping campaign on Whop: that’s permission and pay.</span></div></form></article>`;
}

function gotoStreamer(name) { S.flash = name; showTab('streamers'); }

// ── your logo on every clip ──
const SPOT_NAMES = { top_left: 'Top left', top_center: 'Top middle', top_right: 'Top right' };
const SIZE_NAMES = { small: 'Small', medium: 'Medium', large: 'Large' };

async function loadBrand() {
  try { S.brand = await api('/api/brand'); } catch { return; }
  renderBrand();
}

function renderBrand() {
  const el = $('#brand-card'); if (!el) return;
  const b = S.brand;
  if (!b) { el.innerHTML = '<div class="msg small muted">Loading your logo settings…</div>'; return; }
  const seg = (key, opts, names) => `<div class="seg">${opts.map((v) => `<button class="seg-btn ${b[key] === v ? 'on' : ''}" data-act="brand-set" data-k="${key}" data-v="${v}" ${b.logo ? '' : 'disabled'}>${names[v]}</button>`).join('')}</div>`;
  el.innerHTML = `
    <div class="brand-logo">${b.logo ? `<img src="${esc(b.logo)}" alt="Your logo">` : '<div class="cam-empty">No logo yet</div>'}
      <label class="btn small">${b.logo ? 'Change logo' : 'Add your logo'}<input type="file" id="brand-file" accept="image/png,image/jpeg,image/webp" hidden></label></div>
    <div class="brand-opts">
      <h2>Your logo on every clip</h2>
      <label class="check"><input type="checkbox" id="brand-on" ${b.enabled ? 'checked' : ''} ${b.logo ? '' : 'disabled'}> Put the logo on every new clip</label>
      <div class="brand-row"><span class="lbl">Where</span>${seg('position', b.spots, SPOT_NAMES)}</div>
      <div class="brand-row"><span class="lbl">Size</span>${seg('size', b.sizes, SIZE_NAMES)}</div>
      <div class="brand-apply" id="brand-apply"></div>
      <p class="faint small">It sits below the apps’ top bar and under the captions, so it never covers them. The background of your picture is cut away automatically.</p>
    </div>
    <div class="brand-prev">${b.preview ? `<img src="${esc(b.preview)}" alt="How clips look with your logo">` : ''}</div>`;
  $('#brand-file').addEventListener('change', uploadLogo);
  $('#brand-on').addEventListener('change', (e) => setBrand({ enabled: e.target.checked }));
  renderBrandApply(true);
}

function renderBrandApply(force = false) {
  const el = $('#brand-apply'); const b = S.brand; if (!el || !b) return;
  const job = S.jobs.find((j) => j.kind === 'brand' && ACTIVE.has(j.status));
  if (!changed(el, `${jobSig(job)}|${b.outdated}|${b.enabled}`) && !force) return;
  if (job) {
    el.innerHTML = `<div class="spinner"></div><span class="small">${esc(job.lines[job.lines.length - 1] || 'Starting…')}</span>`;
  } else if (b.outdated && b.logo) {
    el.innerHTML = `<button class="btn small primary" data-act="brand-apply">${b.enabled ? 'Add the logo to' : 'Take the logo off'} ${b.outdated} existing clip${b.outdated > 1 ? 's' : ''}</button>
      <span class="faint small">about 30 seconds per clip</span>`;
  } else {
    el.innerHTML = b.logo ? `<span class="faint small">✓ All ${b.clips} clips match these settings.</span>` : '';
  }
}

async function setBrand(d) {
  try { S.brand = await api('/api/brand', d); renderBrand(); } catch (e) { toast(e.message, 'bad', 6000); }
}

async function uploadLogo(e) {
  const f = e.target.files[0]; if (!f) return;
  if (f.size > 15e6) { toast('That picture is bigger than 15 MB.', 'bad'); return; }
  const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(f); });
  toast('Cutting your logo out of its background…', 'info', 3000);
  try { S.brand = await api('/api/brand/logo', { data }); renderBrand(); toast('Logo ready. Check the preview.', 'good'); }
  catch (err) { toast(err.message, 'bad', 7000); }
}

async function savePermission(form) {
  const d = new FormData(form);
  try {
    const r = await api('/api/permission', { name: form.dataset.name, source: d.get('source'), proof: (d.get('proof') || '').trim(), checked_on: d.get('checked_on') || '', monetize: d.get('monetize') === 'on' });
    S.streamers = r.streamers; afterStreamersChanged();
    toast('Permission saved in streamers.yaml', 'good', 2500);
  } catch (e) { toast(e.message, 'bad', 7000); }
}
async function addStreamer(form) {
  const d = Object.fromEntries(new FormData(form).entries());
  try {
    const r = await api('/api/streamer', d);
    S.streamers = r.streamers; afterStreamersChanged();
    toast(`${d.display || d.name} added`, 'good', 2500);
  } catch (e) { toast(e.message, 'bad', 7000); }
}
function afterStreamersChanged() {
  loadStreamers().then(() => {
    if (S.tab === 'streamers') renderStreamers();
    if (S.editor) { const E = S.editor; const keep = E.layout; setEditorStreamer(E.streamer); if (keep === 'full') E.layout = 'full'; renderEditor(false); }
    if (S.stream) renderMoments();
  });
}

// ───────────────────────────── camera box ─────────────────────────────

function openCamera(name, link, time) {
  const s = streamerOf(name); if (!s) return;
  link = link || s.layout.set_link || s.last_stream || '';
  time = time || s.layout.set_time || '';
  CAM = { name, link, time, frame: null, w: 0, h: 0, box: null, jobId: null };
  const d = $('#cam-dialog');
  d.innerHTML = `<div class="cam-form">
    <div class="ed-head"><h2>Camera box · ${esc(s.display)}</h2><button class="icon-btn" data-act="cam-close" aria-label="Close">✕</button></div>
    <p class="muted small" style="margin:0">Pick a moment in one of their streams where the camera is showing, grab that frame, then drag a box around the camera. Clips then put the camera on top and the game below.</p>
    <div class="row"><input id="cam-link" placeholder="Stream link" value="${esc(link)}"><input id="cam-time" placeholder="e.g. 57:20" size="9" value="${esc(time)}">
      <button class="btn" data-act="cam-grab">Grab frame</button></div>
    <div id="cam-stage" class="cam-stage"><div class="msg">The frame shows up here.</div></div>
    <div class="row end"><span class="faint small" id="cam-hint"></span><span class="spacer"></span>
      <button class="btn ghost" data-act="cam-none" data-name="${esc(name)}">No camera (full frame)</button>
      <button class="btn primary" data-act="cam-save" disabled>Save camera box</button></div></div>`;
  d.showModal();
  if (link && time) grabFrame();
}

async function grabFrame() {
  if (!CAM) return;
  CAM.link = $('#cam-link').value.trim(); CAM.time = $('#cam-time').value.trim();
  if (!CAM.link || Number.isNaN(parseT(CAM.time))) { toast('Give a stream link and a time like 57:20.', 'bad'); return; }
  $('#cam-stage').innerHTML = '<div class="msg"><div class="spinner"></div>Grabbing the frame…</div>';
  $('[data-act=cam-save]').disabled = true; CAM.box = null;
  try { const job = await api('/api/frame', { link: CAM.link, time: CAM.time, name: CAM.name }); CAM.jobId = job.id; upsertJob(job); pollSoon(); }
  catch (e) { $('#cam-stage').innerHTML = `<div class="msg">${esc(e.message)}</div>`; }
}

function camFrameReady(j) {
  if (!CAM || CAM.jobId !== j.id) return;
  if (j.status !== 'done' || !j.result) {
    $('#cam-stage').innerHTML = `<div class="msg">Couldn’t grab that frame: ${esc(j.error || j.status)}</div>`; return;
  }
  CAM.frame = j.result.frame_rel; CAM.w = j.result.w; CAM.h = j.result.h;
  $('#cam-stage').innerHTML = `<div class="cam-img-wrap" id="cam-wrap"><img id="cam-img" src="${esc(j.result.frame_url)}" alt="Frame from the stream"><div id="cam-box" class="cam-box" hidden></div></div>`;
  $('#cam-hint').textContent = 'Drag a box around the camera.';
  const img = $('#cam-img');
  img.addEventListener('load', () => {
    const s = streamerOf(CAM.name);
    if (s && s.layout.cam) {  // show the box saved before
      const [fx, fy, fw, fh] = s.layout.cam; const r = img.getBoundingClientRect();
      drawBox(fx * r.width, fy * r.height, fw * r.width, fh * r.height);
      CAM.box = [fx * CAM.w, fy * CAM.h, fw * CAM.w, fh * CAM.h].map(Math.round);
      $('[data-act=cam-save]').disabled = false;
      $('#cam-hint').textContent = 'This is the box saved before. Drag a new one to change it.';
    }
  }, { once: true });
  bindCamDraw();
}

function drawBox(l, t, w, h) { const b = $('#cam-box'); Object.assign(b.style, { left: l + 'px', top: t + 'px', width: w + 'px', height: h + 'px' }); b.hidden = false; }

function bindCamDraw() {
  const wrap = $('#cam-wrap'), img = $('#cam-img');
  let x0 = 0, y0 = 0, on = false;
  const pt = (e) => { const r = img.getBoundingClientRect(); return [Math.min(Math.max(e.clientX - r.left, 0), r.width), Math.min(Math.max(e.clientY - r.top, 0), r.height), r]; };
  wrap.addEventListener('pointerdown', (e) => { e.preventDefault(); [x0, y0] = pt(e); on = true; wrap.setPointerCapture(e.pointerId); drawBox(x0, y0, 0, 0); });
  wrap.addEventListener('pointermove', (e) => { if (!on) return; const [x, y] = pt(e); drawBox(Math.min(x0, x), Math.min(y0, y), Math.abs(x - x0), Math.abs(y - y0)); });
  wrap.addEventListener('pointerup', (e) => {
    if (!on) return; on = false;
    const [x, y, r] = pt(e); const sx = CAM.w / r.width, sy = CAM.h / r.height;
    const l = Math.min(x0, x), t = Math.min(y0, y), w = Math.abs(x - x0), h = Math.abs(y - y0);
    if (w < 8 || h < 8) { CAM.box = null; $('#cam-box').hidden = true; } else CAM.box = [l * sx, t * sy, w * sx, h * sy].map(Math.round);
    $('[data-act=cam-save]').disabled = !CAM.box;
    $('#cam-hint').textContent = CAM.box ? 'Looks right? Save it. Or drag again.' : 'Drag a bigger box.';
  });
}

async function saveCamera() {
  if (!CAM || !CAM.box) return;
  $('[data-act=cam-save]').disabled = true;
  try {
    const r = await api('/api/camera', { name: CAM.name, frame: CAM.frame, box: CAM.box, set_from: `${CAM.link} at ${CAM.time}` });
    S.streamers = r.streamers;
    const s = streamerOf(CAM.name);
    $('#cam-stage').innerHTML = `<div class="cam-done">${s && s.layout.preview ? `<img src="${esc(s.layout.preview)}" alt="Preview">` : ''}
      <div><b>Saved.</b><p class="muted">This is how ${esc(s ? s.display : CAM.name)}’s clips will look: camera on top, game below, captions in the middle.
      If the camera looks too small or blurry, use “No camera” instead.</p><button class="btn" data-act="cam-close">Done</button></div></div>`;
    $('#cam-hint').textContent = '';
    afterStreamersChanged();
  } catch (e) { toast(e.message, 'bad', 7000); $('[data-act=cam-save]').disabled = false; }
}

async function cameraNone(name) {
  try {
    const r = await api('/api/camera', { name, none: true });
    S.streamers = r.streamers;
    if ($('#cam-dialog').open) { $('#cam-dialog').close(); CAM = null; }
    toast(`${displayOf(name)}: full frame from now on`, 'good', 2500);
    afterStreamersChanged();
  } catch (e) { toast(e.message, 'bad'); }
}

// ───────────────────────────── caption editor ─────────────────────────────

let CAP = null;
const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

async function openCaptions(file) {
  if (!file) return;
  if (player) player.pause();
  CAP = { file, lines: [], orig: {}, replace: [], jobId: null, prepJob: null, data: null };
  const d = $('#cap-dialog');
  d.innerHTML = `<div class="cap-grid">
    <div class="cap-video"><video id="cap-video" controls playsinline preload="metadata"></video>
      <p class="faint small">Click a line’s time to jump there. The video shows captions in capitals, so capitals don’t matter here.</p></div>
    <div class="cap-side">
      <div class="ed-head"><h2>Edit captions</h2><button class="icon-btn" data-act="cap-close" aria-label="Close">✕</button></div>
      <div id="cap-body"><div class="msg"><div class="spinner"></div>Loading the captions…</div></div>
    </div></div>`;
  d.showModal();
  d.addEventListener('close', () => { const v = $('#cap-video'); if (v) v.pause(); CAP = null; }, { once: true });
  await loadCaptions();
}

function closeCaptions() { const d = $('#cap-dialog'); if (d.open) d.close(); }

async function loadCaptions() {
  if (!CAP) return;
  let data;
  try { data = await api('/api/clip/captions?file=' + encodeURIComponent(CAP.file)); }
  catch (e) { $('#cap-body').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  if (!CAP) return;
  CAP.data = data;
  const v = $('#cap-video');
  if (v && v.getAttribute('src') !== data.url) v.src = data.url;
  $('.cap-side h2').textContent = `Edit captions · ${data.display}`;
  if (!data.ready) {
    $('#cap-body').innerHTML = '<div class="msg"><div class="spinner"></div>Reading the captions on your Mac (about 10 seconds)…</div>';
    try { const job = await api('/api/clip/prepare', { file: CAP.file }); CAP.prepJob = job.id; upsertJob(job); pollSoon(); }
    catch (e) { $('#cap-body').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
    return;
  }
  CAP.lines = data.lines;
  CAP.orig = Object.fromEntries(data.lines.map((l) => [String(l.i), l.text]));
  CAP.replace = [];
  renderCapBody();
}

function capPrepared(j) {
  if (j.status === 'done') loadCaptions();
  else $('#cap-body').innerHTML = `<div class="note bad"><b>Couldn’t read the captions.</b><pre>${esc(j.error)}</pre></div>`;
}

function renderCapBody() {
  const d = CAP.data;
  $('#cap-body').innerHTML = `
    <div class="cap-replace">
      <input id="rep-from" placeholder="Find, e.g. cold warmer" aria-label="Find">
      <span class="faint">→</span>
      <input id="rep-to" placeholder="Replace with, e.g. Cole Palmer" aria-label="Replace with">
      <button class="btn small" data-act="cap-replace">Replace everywhere</button>
      <label class="check small"><input type="checkbox" id="rep-always"> Always fix this in ${esc(d.display)}’s new clips</label>
    </div>
    <div id="cap-rules"></div>
    <div class="cap-lines" id="cap-lines">${CAP.lines.map((l) => `
      <div class="cap-line" data-i="${l.i}" data-start="${l.start}">
        <button class="cap-t" data-act="cap-seek" data-t="${l.start}" title="Jump the video here">${fmt(l.start)}</button>
        <input class="cap-in" data-i="${l.i}" value="${esc(l.text)}" aria-label="Caption at ${fmt(l.start)}" spellcheck="false">
      </div>`).join('')}</div>
    <div class="cap-foot"><span id="cap-status" class="small muted">${d.edited ? 'These captions were edited before.' : `${CAP.lines.length} lines`}</span><span class="spacer"></span>
      <button class="btn ghost small" data-act="cap-reset">Undo all changes</button>
      <button class="btn primary" data-act="cap-remake" id="cap-remake">Re-make with these captions</button></div>`;
  renderCapRules();
  $$('.cap-in').forEach((inp) => {
    inp.addEventListener('input', () => markLine(inp));
    inp.addEventListener('keydown', (e) => {  // Enter / ↑ / ↓ move between lines
      const all = $$('.cap-in'); const k = all.indexOf(inp);
      if (e.key === 'Enter' || e.key === 'ArrowDown') { e.preventDefault(); if (all[k + 1]) all[k + 1].focus(); }
      if (e.key === 'ArrowUp') { e.preventDefault(); if (all[k - 1]) all[k - 1].focus(); }
    });
  });
  const v = $('#cap-video');
  v.ontimeupdate = () => capFollow(v.currentTime);
  const j = CAP.jobId && jobById(CAP.jobId);
  if (j && ACTIVE.has(j.status)) capBusy(true);
}

function renderCapRules() {
  const d = CAP && CAP.data; const el = $('#cap-rules'); if (!el) return;
  el.innerHTML = d.rules.length ? `<div class="rules"><span class="faint small">Fixed automatically in new clips:</span>
    ${d.rules.map((r) => `<span class="rule">${esc(r.from)} → ${esc(r.to)}<span class="faint"> · ${r.scope === 'all' ? 'every streamer' : esc(d.display)}</span>
      <button class="rule-x" data-act="cap-rule-remove" data-from="${esc(r.from)}" data-scope="${esc(r.scope)}" aria-label="Stop fixing ${esc(r.from)}">✕</button></span>`).join('')}</div>` : '';
}

function markLine(inp) {
  inp.closest('.cap-line').classList.toggle('changed', inp.value.trim() !== CAP.orig[inp.dataset.i]);
  const n = $$('.cap-line.changed').length;
  const s = $('#cap-status'); if (s) s.textContent = n ? `${n} line${n > 1 ? 's' : ''} changed` : `${CAP.lines.length} lines`;
}

function capFollow(t) {
  const rows = $$('.cap-line'); let cur = null;
  for (const r of rows) { if (+r.dataset.start <= t + 0.05) cur = r; else break; }
  rows.forEach((r) => r.classList.toggle('now', r === cur));
  const typing = document.activeElement && document.activeElement.classList.contains('cap-in');
  if (cur && !typing && !$('#cap-video').paused) cur.scrollIntoView({ block: 'nearest' });
}

async function capReplace() {
  const from = $('#rep-from').value.trim(), to = $('#rep-to').value.trim();
  if (!from || !to) { toast('Fill in both: what the captions say now, and what they should say.', 'info'); return; }
  const re = new RegExp(`(^|[^\\w'])${from.split(/\s+/).map(escRe).join('\\s+')}(?=$|[^\\w'])`, 'gi');
  let n = 0;
  $$('.cap-in').forEach((inp) => {
    const v = inp.value.replace(re, (m, pre) => { n += 1; return pre + to; });
    if (v !== inp.value) { inp.value = v; markLine(inp); }
  });
  CAP.replace.push({ from, to });  // also catches it where the words are split across two lines
  if ($('#rep-always').checked) {
    try { const r = await api('/api/fixes', { streamer: CAP.data.streamer, from, to }); CAP.data.rules = r.rules; renderCapRules(); }
    catch (e) { toast(e.message, 'bad'); }
  }
  toast(n ? `Replaced in ${n} place${n > 1 ? 's' : ''}. Press “Re-make with these captions” to put it on the video.`
    : 'Not found within a single line (it may be split across two). It’s still fixed when you re-make.', 'info', 5000);
  $('#rep-from').value = ''; $('#rep-to').value = '';
}

async function capRuleRemove(from, scope) {
  try { const r = await api('/api/fixes/remove', { streamer: CAP.data.streamer, from, scope }); CAP.data.rules = r.rules; renderCapRules(); }
  catch (e) { toast(e.message, 'bad'); }
}

async function capRemake(reset) {
  if (!CAP || !CAP.data || !CAP.data.ready) return;
  const edits = {};
  if (!reset) $$('.cap-in').forEach((inp) => { if (inp.value.trim() !== CAP.orig[inp.dataset.i]) edits[inp.dataset.i] = inp.value.trim(); });
  if (!reset && !Object.keys(edits).length && !CAP.replace.length) { toast('Nothing changed yet.', 'info'); return; }
  try {
    const job = await api('/api/clip/recaption', { file: CAP.file, edits, replace: CAP.replace, reset });
    CAP.jobId = job.id; upsertJob(job); capBusy(true); pollSoon();
  } catch (e) { toast(e.message, 'bad', 7000); }
}

function capBusy(on) {
  const b = $('#cap-remake'); if (b) { b.disabled = on; b.textContent = on ? 'Re-making… (about 30 s)' : 'Re-make with these captions'; }
  $$('.cap-in').forEach((i) => { i.disabled = on; });
}

function renderCapJob() {
  const j = CAP && CAP.jobId && jobById(CAP.jobId);
  const s = $('#cap-status');
  if (j && ACTIVE.has(j.status) && s) s.textContent = j.lines[j.lines.length - 1] || 'Waiting for the clip before it…';
}

function capRemade(j) {
  capBusy(false);
  if (j.status !== 'done') {
    toast(`Re-making didn’t work: ${j.error}`, 'bad', 8000);
    const s = $('#cap-status'); if (s) s.textContent = 'Not re-made. Your edits are still here; try again.';
    CAP.jobId = null; return;
  }
  toast('Captions updated on the video.', 'good', 3000);
  const v = $('#cap-video');
  if (v && j.result && j.result.url) v.src = j.result.url;
  CAP.jobId = null;
  loadCaptions();
}

// ───────────────────────────── jobs ─────────────────────────────

let pollTimer = null;
async function poll() {
  clearTimeout(pollTimer);
  try {
    const jobs = await api('/api/jobs');
    const before = new Map(S.jobs.map((j) => [j.id, j.status]));
    S.jobs = jobs;
    for (const j of jobs) {
      const was = before.get(j.id);
      if (was && ACTIVE.has(was) && !ACTIVE.has(j.status)) onJobEnd(j);
    }
  } catch { /* the offline banner is shown by api() */ }
  renderAllJobs();
  const busy = S.jobs.some((j) => ACTIVE.has(j.status));
  pollTimer = setTimeout(poll, busy ? 1000 : 4000);
}
function pollSoon() { clearTimeout(pollTimer); pollTimer = setTimeout(poll, 300); }

function onJobEnd(j) {
  if (j.kind === 'find') {
    loadStreams().then(() => {
      if (j.status === 'done' && S.findJob === j.id) {
        const key = (j.result && j.result.key) || j.meta.key;
        openStream(key).then(() => toast('Moments found. Click one to watch it and make a clip.', 'good', 6000));
      }
    });
    if (j.status === 'failed') toast('Finding moments didn’t work. See the box on the left.', 'bad', 6000);
  }
  if (j.kind === 'make') {
    loadClips(); refreshStream();
    if (j.status === 'done') toast(`Clip ready: ${j.result && j.result.file ? j.result.file.split('/').pop() : ''}`, 'good', 5000);
    else if (j.status === 'failed' && !(S.editor && S.editor.jobId === j.id)) toast(`A clip didn’t work: ${j.error}`, 'bad', 8000);
  }
  if (j.kind === 'frame') camFrameReady(j);
  if (j.kind === 'record') { if (S.tab === 'streamers') renderStreamers(); }
  if (j.kind === 'posttext') clipAiDone(j);
  if (j.kind === 'captions' && CAP && CAP.prepJob === j.id) capPrepared(j);
  if (j.kind === 'recaption') {
    if (CAP && CAP.jobId === j.id) capRemade(j);
    else if (j.status === 'done') toast(`Captions updated: ${j.meta.file.split('/').pop()}`, 'good', 4000);
    else if (j.status === 'failed') toast(`Re-making didn’t work: ${j.error}`, 'bad', 8000);
    loadClips(); refreshStream();
  }
  if (['find', 'make', 'posttext'].includes(j.kind)) loadStatus();  // the AI may have paused or come back
  if (j.kind === 'brand') {
    loadBrand(); loadClips();
    const r = j.result || {};
    if (j.status === 'done') toast(`Logo updated on ${r.done ?? 0} clip(s)${r.failed ? `; ${r.failed} didn’t work (see Jobs)` : ''}.`, r.failed ? 'bad' : 'good', 6000);
    else if (j.status === 'failed') toast(`Updating the logo didn’t work: ${j.error}`, 'bad', 8000);
  }
  if (j.kind === 'make' || j.kind === 'recaption') S.brand = null;  // clip counts changed
}

function renderAllJobs() {
  renderJobs();
  if (S.tab === 'find') renderFindJobs();
  renderEditorJob();
  renderCapJob();
  renderBrandApply();
}
// Redraw a box only when its job changed, so a playing preview or an open menu isn't reset every second.
function changed(el, sig) { if (el.dataset.sig === sig) return false; el.dataset.sig = sig; return true; }
const jobSig = (j) => j ? `${j.id}:${j.status}:${j.lines.length}:${j.lines[j.lines.length - 1] || ''}` : 'none';

function statusWord(j) {
  if (j.status === 'running') return `running ${Math.round((Date.now() / 1000) - j.started)} s`;
  if (j.status === 'queued') return 'waiting';
  if (j.status === 'done') return 'done';
  if (j.status === 'failed') return 'failed';
  return j.status;
}
function renderJobs() {
  const n = S.jobs.filter((j) => ACTIVE.has(j.status)).length;
  $('#jobs-count').textContent = n || '';
  document.title = n ? `(${n}) Clipping` : 'Clipping';
  $('#jobs-btn').setAttribute('aria-expanded', String(S.jobsOpen));
  const el = $('#jobs'); el.hidden = !S.jobsOpen;
  if (!S.jobsOpen) return;
  const scroll = el.scrollTop;
  el.innerHTML = `<div class="jobs-head"><b>Jobs</b><span class="faint small">since the app started</span><button class="icon-btn" data-act="close-jobs" aria-label="Close">✕</button></div>` +
    (S.jobs.length ? S.jobs.map((j) => `<details class="job" data-id="${j.id}" ${S.openJobs.has(j.id) ? 'open' : ''}>
      <summary><span class="dot ${j.status}"></span><span class="lbl2">${esc(j.label)}</span><span class="faint small">${statusWord(j)}</span></summary>
      <pre>${esc(j.lines.join('\n') || 'Nothing yet.')}</pre>
      ${ACTIVE.has(j.status) ? `<div class="row" style="margin-bottom:10px"><button class="btn small ghost" data-act="stop-job" data-id="${j.id}">Stop</button></div>` : ''}
    </details>`).join('') : '<p class="empty">Nothing has run yet.</p>');
  el.scrollTop = scroll;
}

init();
