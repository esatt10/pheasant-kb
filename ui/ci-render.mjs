/**
 * Render the tuning page against a real region and fail on any console error.
 *
 * Typechecking proves the page compiles against the API's shapes. It does not
 * prove the page *renders*: `api/types.ts` is hand-written, so a field that is
 * actually nullable and typed as a number produces a type-clean build and a
 * blank panel in a browser. A chart dividing by zero on an empty sweep is the
 * same class of thing.
 *
 * So this boots the real server over a real region, opens the page, and treats
 * a console error as a failure. It also asserts the sections that must exist
 * are present — a page that renders nothing renders without errors too.
 *
 * The same region then serves the Notebook at phone width, which a desktop
 * viewport never exercises (see below).
 */
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium } from 'playwright';

const ROOT = new URL('..', import.meta.url).pathname;
const work = mkdtempSync(join(tmpdir(), 'pheasant-ci-'));

function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { cwd: ROOT, stdio: 'inherit', ...opts });
    child.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`${cmd} exited ${code}`))));
  });
}

// A region with indexed content, recorded proof, sampled traffic and one
// completed batch — everything the page has a panel for.
//
// The seeding lives in `scripts/seed_render_region.py`, not in a template
// literal here. Escaping a Python program through JavaScript is a minefield
// (the first attempt died on an unterminated string that was invisible in
// either file alone), and a Python program inside a `.mjs` is one nothing
// lints or formats.
// One port, stated once and passed to both sides. It used to be written into
// the config as `server.api.port` — which is not a field, so it was dropped
// and the region served on the default while this polled that default by
// accident.
const PORT = 8799;

await run('python', [
  join(ROOT, 'scripts', 'seed_render_region.py'),
  '--work', work,
  '--port', String(PORT),
]);

const server = spawn('python', ['-m', 'pheasant', 'serve', '--config', join(work, 'pheasant.yaml')], {
  cwd: ROOT,
  stdio: 'inherit',
});
process.on('exit', () => server.kill());

const base = `http://127.0.0.1:${PORT}`;
for (let i = 0; i < 60; i += 1) {
  try {
    const r = await fetch(`${base}/health`);
    if (r.ok) break;
  } catch { /* not up yet */ }
  await new Promise((r) => setTimeout(r, 1000));
}

const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM || undefined });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', (e) => errors.push(String(e)));

await page.goto(`${base}/tuning`, { waitUntil: 'networkidle' });
await page.waitForTimeout(3000);

const body = await page.textContent('body');
const required = [
  'Retrieval tuning',
  'Live pipeline health',
  'What this region ranks with',
  'Where retrieval loses documents',
];
const missing = required.filter((text) => !body.includes(text));

// A sweep chart must actually have drawn geometry — an empty <svg> passes a
// text assertion and tells a reader nothing.
const marks = await page.locator('.sweep svg circle').count();

// The Notebook at phone width. Below 860px its layout leaves the graph tile
// out of every column, and the graph used to render anyway, into a detached
// host: Cytoscape measured NaN, threw, and React unmounted the whole app, so
// a phone saw an empty page. A blank page logs the error once and then has
// nothing to assert on, so this checks for the composer as well.
const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
const phoneErrors = [];
phone.on('pageerror', (e) => phoneErrors.push(String(e)));
await phone.goto(`${base}/`, { waitUntil: 'networkidle' });
await phone.waitForTimeout(3000);
const composer = await phone.locator('textarea').count();

// Every page must lay out at the phone's own width. One element wider than
// the screen (the topbar's nav and buttons, once) makes a mobile browser
// lay the whole app out at that width and zoom out to fit it, so everything
// is unreadably small and nothing fails or logs. The nav lives behind a menu
// at this width, so that has to open too.
const tooWide = [];
for (const route of ['/', '/graph', '/memory', '/evaluation', '/tuning', '/sources', '/config']) {
  await phone.goto(`${base}${route}`, { waitUntil: 'networkidle' });
  await phone.waitForTimeout(800);
  const width = await phone.evaluate(() => document.documentElement.scrollWidth);
  if (width > 390) tooWide.push(`${route} laid out ${width}px wide`);
}
await phone.getByRole('button', { name: 'Open menu' }).click();
const menuLinks = await phone.locator('.topbar__menu--open .navlink').count();

await browser.close();
server.kill();

if (phoneErrors.length || composer === 0) {
  console.error('the Notebook did not render at phone width:', phoneErrors);
  process.exit(1);
}
if (tooWide.length) {
  console.error('pages wider than a 390px phone (the browser zooms them out):', tooWide);
  process.exit(1);
}
if (menuLinks === 0) {
  console.error('the topbar menu did not open at phone width');
  process.exit(1);
}

if (errors.length) {
  console.error('console errors:', errors);
  process.exit(1);
}
if (missing.length) {
  console.error('sections missing from the rendered page:', missing);
  process.exit(1);
}
if (marks === 0) {
  console.error('no sweep marks were drawn; the charts rendered empty');
  process.exit(1);
}
console.log(`tuning page rendered clean: ${marks} sweep marks, no console errors; every page fits a phone`);
