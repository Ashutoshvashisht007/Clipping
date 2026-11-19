// Records the Clipping page for the demo video: drives it like a person (visible cursor, click highlights),
// saves every frame, and notes when each scene starts and where the long waits are. Called by make_demo.py.
// usage: node record.js <port> <out dir> <plan.json>
//   plan.json = {stream, streamer, squeeze, targets: {sceneId: seconds the scene should last on screen}}
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const [PORT, OUT, PLAN] = process.argv.slice(2);
const plan = JSON.parse(fs.readFileSync(PLAN, 'utf8'));
const SQUEEZE = plan.squeeze || 3.5;  // a long wait is shown sped up to about this many seconds
const FRAMES = path.join(OUT, 'frames');
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(FRAMES, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const frames = [];
const timeline = { squeeze: SQUEEZE, scenes: [], size: [1920, 1080] };
let T0 = 0;
let page;
let cur = null;
const now = () => (Date.now() - T0) / 1000;
const say = (...a) => console.log(`[${now().toFixed(1).padStart(6)}s]`, ...a);

// ── scene bookkeeping ──
function sceneStart(id) { cur = { id, t0: now(), waits: [] }; say('scene', id); }
function shownSoFar() {  // how long this scene will last in the video so far (waits squeezed)
  let raw = 0; let shown = 0;
  for (const w of cur.waits) { const len = (w[1] ?? now()) - w[0]; raw += len; shown += len > SQUEEZE ? SQUEEZE : len; }
  return now() - cur.t0 - raw + shown;
}
async function sceneEnd() {
  const target = plan.targets[cur.id] || 0;
  while (shownSoFar() < target) await sleep(100);
  cur.t1 = now(); timeline.scenes.push(cur); cur = null;
}
async function waitFor(what, fn) {  // a slow step (finding moments, making the clip): sped up in the video
  const w = [now(), null]; cur.waits.push(w); say('  waiting:', what);
  try { return await fn(); } finally { w[1] = now(); say(`  done in ${(w[1] - w[0]).toFixed(0)} s`); }
}

// ── the cursor, drawn into the page (automated browsers don't show the mouse) ──
const OVERLAY = () => {
  if (window.top !== window) return;  // the page only, not the YouTube/Twitch player inside it
  const put = () => {
    if (document.getElementById('demo-cur')) return;
    const css = document.createElement('style');
    css.textContent = `
      #demo-cur, #demo-ring { position: fixed; inset: auto; margin: 0; padding: 0; border: 0; background: none; overflow: visible;
        pointer-events: none; color: inherit; }
      #demo-cur { left: 0; top: 0; width: 1px; height: 1px; transition: transform .7s cubic-bezier(.45,.05,.25,1); }
      #demo-cur svg { position: absolute; left: -3px; top: -2px; width: 26px; height: 32px; filter: drop-shadow(0 2px 3px rgba(0,0,0,.55)); }
      #demo-cur .rip { position: absolute; left: -20px; top: -20px; width: 40px; height: 40px; border-radius: 50%;
        border: 3px solid #ffe600; opacity: 0; }
      #demo-cur .rip.go { animation: demo-rip .55s ease-out; }
      @keyframes demo-rip { from { transform: scale(.25); opacity: 1 } to { transform: scale(1.5); opacity: 0 } }
      #demo-ring { left: 0; top: 0; width: 10px; height: 10px; border-radius: 10px; opacity: 0;
        box-shadow: 0 0 0 3px #ffe600, 0 0 0 10px rgba(255,230,0,.16); transition: all .35s ease; }`;
    document.head.appendChild(css);
    const c = document.createElement('div'); c.id = 'demo-cur'; c.setAttribute('popover', 'manual');
    c.innerHTML = `<svg viewBox="0 0 26 32"><path d="M3 2 L3 25 L8.6 19.6 L12.4 28.6 L16.4 26.9 L12.7 18.2 L20.5 18.2 Z"
      fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg><span class="rip"></span>`;
    const r = document.createElement('div'); r.id = 'demo-ring'; r.setAttribute('popover', 'manual');
    document.body.append(r, c);
    r.showPopover(); c.showPopover();
    window.__demo = {
      x: innerWidth * 0.62, y: innerHeight * 0.58,
      move(x, y, ms) { c.style.transitionDuration = `${ms}ms`; c.style.transform = `translate(${x}px, ${y}px)`; this.x = x; this.y = y; },
      click() { const s = c.querySelector('.rip'); s.classList.remove('go'); void s.offsetWidth; s.classList.add('go'); },
      ring(b) {
        if (!b) { r.style.opacity = '0'; r.dataset.on = ''; return; }
        const p = 6;
        const geo = { left: `${b.x - p}px`, top: `${b.y - p}px`, width: `${b.width + 2 * p}px`, height: `${b.height + 2 * p}px` };
        if (!r.dataset.on) {  // appear on the target (no gliding in from wherever it was last)
          r.style.transition = 'none'; Object.assign(r.style, geo); void r.offsetWidth; r.style.transition = '';
        }
        Object.assign(r.style, geo, { opacity: '1' }); r.dataset.on = '1';
      },
      raise() { for (const el of [r, c]) { el.hidePopover(); el.showPopover(); } },  // above an open <dialog>
    };
    window.__demo.move(window.__demo.x, window.__demo.y, 0);
  };
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', put); else put();
};

async function settle(loc) {  // the element's box once the page has stopped scrolling or shifting
  let prev = null;
  for (let i = 0; i < 30; i += 1) {
    const b = await loc.boundingBox();
    if (b && prev && Math.abs(b.x - prev.x) < 0.5 && Math.abs(b.y - prev.y) < 0.5 && Math.abs(b.height - prev.height) < 0.5) return b;
    prev = b;
    await sleep(90);
  }
  return prev;
}
async function visible(loc) {  // scroll the whole element into view (as much as fits) if it isn't
  let b = await settle(loc);
  const vp = page.viewportSize();
  const need = Math.min(b ? b.height : 0, vp.height - 160);
  if (!b || b.y < 70 || b.y + need > vp.height - 16) {
    await page.evaluate(() => window.__demo.ring(null));  // the ring is fixed on screen: hide it while the page scrolls
    await loc.evaluate((e) => e.scrollIntoView({ behavior: 'smooth', block: 'center' }));
    await sleep(350);
    b = await settle(loc);
  }
  return b;
}
async function point(sel, { ring = true, ms = 750, fx = 0.5, fy = 0.5, pause = 0 } = {}) {
  const loc = page.locator(sel).first();
  await loc.waitFor({ state: 'visible', timeout: 30000 });
  const b = await visible(loc);
  const x = b.x + b.width * fx; const y = b.y + Math.min(b.height * fy, 60);
  await page.evaluate(([x, y, ms]) => window.__demo.move(x, y, ms), [x, y, ms]);
  await sleep(ms + 60);
  if (ring) await page.evaluate((b) => window.__demo.ring(b), b);
  if (pause) await sleep(pause);
  return { x, y };
}
const unring = () => page.evaluate(() => window.__demo.ring(null));
async function click(sel, opts = {}) {
  const p = await point(sel, opts);
  await sleep(260);
  await page.evaluate(() => window.__demo.click());
  await page.mouse.click(p.x, p.y);
  await sleep(420);
  await unring();
}
const playVideo = (sel) => page.evaluate((s) => { const v = document.querySelector(s); if (v) { v.muted = true; v.currentTime = 0; v.play().catch(() => {}); } }, sel);

(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true,
    args: ['--autoplay-policy=no-user-gesture-required', '--hide-scrollbars', '--force-color-profile=srgb'] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 810 }, deviceScaleFactor: 4 / 3, colorScheme: 'dark' });
  await ctx.addInitScript(OVERLAY);
  page = await ctx.newPage();
  page.on('pageerror', (e) => console.log('page error:', e.message));
  page.on('dialog', (d) => d.accept());
  await page.goto(`http://localhost:${PORT}/#find`);
  await page.waitForSelector('#moments .placeholder');
  await sleep(1200);

  // record every frame the browser paints
  const cdp = await ctx.newCDPSession(page);
  cdp.on('Page.screencastFrame', (f) => {
    const name = `f${String(frames.length).padStart(6, '0')}.jpg`;
    fs.writeFileSync(path.join(FRAMES, name), Buffer.from(f.data, 'base64'));
    frames.push([name, (Date.now() - T0) / 1000]);
    cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }).catch(() => {});
  });
  T0 = Date.now();
  await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 90, maxWidth: 1920, maxHeight: 1080, everyNthFrame: 1 });
  await page.evaluate(() => window.__demo.move(window.__demo.x + 1, window.__demo.y, 50));  // paint a first frame
  await sleep(600);

  try {
    // 1. paste a link, find moments
    sceneStart('find');
    await click('#find-link', { pause: 200 });
    await page.keyboard.type(plan.stream, { delay: 26 });
    await sleep(500);
    await click('#find-form button[type=submit]');
    await sleep(900);
    await point('#find-jobs .job-card', { ms: 650 });
    await waitFor('moments', () => page.waitForSelector('.mcard', { timeout: 15 * 60000 }));
    await unring();
    await sleep(900);
    await sceneEnd();

    // 2. the list of moments
    sceneStart('moments');
    page.evaluate(() => (typeof ytApi === 'function' ? ytApi() : null)).catch(() => {});  // load YouTube's player code early
    await point('.mom-head h2', { pause: 700 });
    for (let i = 0; i < 3; i += 1) await point(`.mcard >> nth=${i}`, { fx: 0.32, fy: 0.25, pause: 1100 });
    await unring();
    await sceneEnd();

    // 3. open the top moment: it plays, and the context is worked out
    sceneStart('editor');
    // the player stays hidden until the video is actually running (so its thumbnail never shows), and is
    // muted: headless Chrome has no sound output, and an unmuted YouTube player barely moves there
    await page.addStyleTag({ content: '#player-host iframe { opacity: 0; transition: opacity .35s } #player-host.demo-on iframe { opacity: 1 }' });
    await click('.mcard >> nth=0', { fx: 0.3, fy: 0.25 });
    await page.waitForSelector('#ed-start');
    const ready = () => page.evaluate(() => !!(player && player.p && (typeof player.p.mute === 'function' || typeof player.p.setMuted === 'function')));
    if (!(await ready())) {  // a slow load is sped up in the video, like the other waits
      await waitFor('the player', () => page.waitForFunction(() => player && player.p && (typeof player.p.mute === 'function'
        || typeof player.p.setMuted === 'function'), null, { timeout: 30000, polling: 150 }).catch(() => {}));
    }
    // start it straight away, like a preview, while the cursor moves over to it
    await page.evaluate(() => { try { if (player.p.mute) { player.p.mute(); player.p.playVideo(); } else { player.p.setMuted(true); player.p.play(); } } catch (e) { /* fine */ } });
    const moving = point('#editor .player-wrap', { fx: 0.62, fy: 0.55, ms: 700 });
    await waitFor('playback', () => page.waitForFunction(() => { try {
      return (player.p.getPlayerState ? player.p.getPlayerState() === 1 : !player.p.isPaused()) && player.p.getCurrentTime() > 0;
    } catch (e) { return false; } }, null, { timeout: 20000, polling: 100 }).catch(() => {}));
    await page.evaluate(() => document.getElementById('player-host').classList.add('demo-on'));
    await moving;
    await sleep(3800);  // watch it play for a moment
    await unring();
    await point('#ctx-status', { ms: 700 });
    if (await page.evaluate(() => S.editor && S.editor.ctxState === 'busy')) {
      await waitFor('context', () => page.waitForFunction(() => S.editor && S.editor.ctxState !== 'busy', null, { timeout: 300000, polling: 400 }));
    }
    await sleep(500);
    await point('#ed-ctx', { pause: 1600 });
    await point('#ed-start', { pause: 1200 });
    await unring();
    await sceneEnd();

    // 4. make the clip
    sceneStart('make');
    await click('[data-act=make]');
    await sleep(700);
    await point('#ed-job', { ms: 600, fy: 0.2 });
    const res = await waitFor('the clip', () => page.waitForSelector('#ed-job .result video, #ed-job .note.bad', { timeout: 15 * 60000 }));
    if (await res.evaluate((e) => e.matches('.note.bad'))) throw new Error('Making the clip failed: ' + (await res.innerText()));
    await unring();
    await sceneEnd();

    // 5. the result
    sceneStart('result');
    await playVideo('#ed-job .result video');
    await point('#ed-job .result', { ms: 650, fx: 0.2, fy: 0.5, pause: 2600 });
    await point('#ed-job [data-act=copy-result][data-what=title]', { pause: 900 });
    timeline.clip = await page.getAttribute('#ed-job [data-act=cap-open]', 'data-file');
    timeline.clip_meta = await page.evaluate(() => { const j = S.editor && jobById(S.editor.jobId); return (j && j.result && j.result.meta) || null; });
    await unring();
    await sceneEnd();

    // 6. the caption editor
    sceneStart('captions');
    await click('#ed-job [data-act=cap-open]');
    await page.evaluate(() => window.__demo.raise());
    if (!(await page.$('#cap-dialog .cap-in'))) {
      await waitFor('captions', () => page.waitForSelector('#cap-dialog .cap-in', { timeout: 180000 }));
    }
    await playVideo('#cap-video');
    await sleep(500);
    await point('#rep-from', { pause: 1100 });
    await point('#rep-to', { pause: 900 });
    await point('label:has(#rep-always)', { pause: 1100 });
    await point('#cap-lines .cap-line >> nth=1', { pause: 500 });
    await unring();
    await page.evaluate(() => { const l = document.getElementById('cap-lines'); if (l) l.scrollBy({ top: 260, behavior: 'smooth' }); });
    await sleep(900);
    await sceneEnd();

    // 7. the Clips page
    sceneStart('clips');
    await click('[data-act=cap-close]');
    await click('.tab[data-tab=clips]');
    await page.waitForSelector('.clip');
    await sleep(700);
    await point('.clip textarea >> nth=0', { pause: 900 });
    await point('.clip textarea >> nth=2', { pause: 900 });
    await point('.clip details.box', { fy: 0.3, pause: 900 });
    await unring();
    await sceneEnd();

    // 8. Streamers & setup: permission, camera box, logo, Follow live
    sceneStart('streamers');
    await click('.tab[data-tab=streamers]');
    const card = `#s-${plan.streamer}`;
    await page.waitForSelector(card);
    await page.waitForSelector('#brand-card [data-act=brand-set]', { timeout: 20000 });  // the logo card loads a moment later
    await sleep(800);
    await point(`${card} .scard-head .badge`, { pause: 900 });
    await point(`${card} .perm select`, { pause: 900 });
    await point(`${card} .cam`, { pause: 1000 });
    await point('#brand-card', { fy: 0.2, pause: 1200 });
    await point(`${card} [data-act=live-follow]`, { pause: 1200 });
    await unring();
    await sceneEnd();
  } catch (e) {
    await page.screenshot({ path: path.join(OUT, 'failed.png') }).catch(() => {});
    console.error('RECORDING FAILED:', e.message);
    fs.writeFileSync(path.join(OUT, 'frames.json'), JSON.stringify(frames));
    await browser.close();
    process.exit(1);
  }
  await sleep(400);
  await cdp.send('Page.stopScreencast');
  timeline.end = now();
  fs.writeFileSync(path.join(OUT, 'frames.json'), JSON.stringify(frames));
  fs.writeFileSync(path.join(OUT, 'timeline.json'), JSON.stringify(timeline, null, 1));
  say(`recorded ${frames.length} frames`);
  await browser.close();
})().catch((e) => { console.error('RECORDING FAILED:', e.message); process.exit(1); });
