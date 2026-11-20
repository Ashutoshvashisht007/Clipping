// Vercel's build for the landing page (see vercel.json): copies the page into dist/ and, when the
// WEB3FORMS_ACCESS_KEY environment variable is set, switches the waitlist form on. Without the variable the
// form stays off and says the waitlist opens soon, so the page never pretends to send.
//
// The key ends up in the served HTML: Web3Forms access keys are made to be public (they only let people send
// sign-ups to your inbox). It is kept out of git by living in Vercel's settings instead.
//
// Local check:  node build.mjs   (or WEB3FORMS_ACCESS_KEY=... node build.mjs), then look in dist/.
import { cpSync, existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, 'dist');
rmSync(out, { recursive: true, force: true });
for (const item of ['index.html', 'favicon.svg', 'share.jpg', 'assets', 'media']) {
  if (existsSync(join(here, item))) cpSync(join(here, item), join(out, item), { recursive: true });
}

const key = (process.env.WEB3FORMS_ACCESS_KEY || '').trim();
const page = join(out, 'index.html');
let html = readFileSync(page, 'utf8');

if (!/^[A-Za-z0-9-]{20,64}$/.test(key)) {
  console.log(key ? 'waitlist: WEB3FORMS_ACCESS_KEY does not look like a key; the form stays off'
                  : 'waitlist: no WEB3FORMS_ACCESS_KEY; the form stays off');
} else {
  const swaps = [
    ['<form class="waitlist" data-endpoint=""',
     '<form class="waitlist" action="https://api.web3forms.com/submit" method="post" data-endpoint="https://api.web3forms.com/submit"'],
    ['<input type="hidden" name="access_key" value="">', `<input type="hidden" name="access_key" value="${key}">`],
    ['<fieldset disabled>', '<fieldset>'],
    ['<p>The waitlist opens soon. The code is on GitHub today.</p>',
     '<p>Get one email when Clipping opens; your address is used for nothing else. The code is on GitHub today.</p>'],
  ];
  for (const [from, to] of swaps) {
    if (!html.includes(from)) throw new Error(`waitlist: could not find ${from} in index.html`);
    html = html.replace(from, to);
  }
  writeFileSync(page, html);
  console.log('waitlist: on');
}
