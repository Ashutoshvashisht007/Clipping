// Photographs the demo's cards (cards/cards.html) as PNGs. Called by make_demo.py.
// usage: node cards.js <data.json> <out dir>
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

(async () => {
  const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const out = process.argv[3];
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const url = 'file://' + path.join(__dirname, 'cards', 'cards.html');
  const shots = [['bg', 1920, 1080], ['title', 1920, 1080], ['hood', 1920, 1080], ['end', 1920, 1080], ['hook', 1920, 1080]];
  for (const [w, h, r] of data.masks) shots.push([`mask-${w}x${h}x${r}`, w, h]);
  for (const [kind, hls] of Object.entries(data.highlights || {})) for (const hl of hls) shots.push([kind, 1920, 1080, hl]);
  for (const [kind, w, h, hl] of shots) {
    const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 1 });
    await page.addInitScript((d) => { window.DATA = d; }, data);
    await page.goto(`${url}?kind=${kind}${hl ? `&hl=${hl}` : ''}`);
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: path.join(out, `${kind}${hl ? `-hl-${hl}` : ''}.png`), clip: { x: 0, y: 0, width: w, height: h } });
    await page.close();
  }
  await browser.close();
})().catch((e) => { console.error('cards failed:', e.message); process.exit(1); });
