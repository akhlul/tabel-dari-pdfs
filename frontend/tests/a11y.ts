/**
 * Accessibility check (axe-core) in light and dark color schemes, plus a narrow-window layout check.
 *
 *   mise run check:a11y               # against http://127.0.0.1:14433
 *   BASE=http://127.0.0.1:8010 bun tests/a11y.ts
 *
 * Needs a running server and a Chromium-based browser (CHROME=/path/to/chrome, default google-chrome-stable).
 * Exits 1 when any serious or critical violation is found.
 */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { chromium } from 'playwright-core';

const BASE = process.env.BASE ?? 'http://127.0.0.1:14433';
const axeSource = readFileSync(createRequire(import.meta.url).resolve('axe-core/axe.min.js'), 'utf8');

const docs = (await (await fetch(`${BASE}/api/documents`)).json()) as { id: string }[];
const pages = ['/', '/search', '/tables?state=none,valid,invalid', ...(docs[0] ? [`/doc?id=${docs[0].id}`] : [])];

const browser = await chromium.launch({ executablePath: process.env.CHROME ?? '/usr/bin/google-chrome-stable', headless: true });
let failed = 0;
for (const scheme of ['light', 'dark'] as const) {
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 }, colorScheme: scheme });
  const page = await ctx.newPage();
  for (const url of pages) {
    await page.goto(BASE + url);
    await page.waitForTimeout(2500);
    await page.addScriptTag({ content: axeSource });
    const r: any = await page.evaluate(async () => (window as any).axe.run(document, { resultTypes: ['violations'] }));
    const bad = r.violations.filter((v: any) => v.impact === 'serious' || v.impact === 'critical');
    failed += bad.length;
    console.log(`${scheme.padEnd(5)} ${url.padEnd(40)} ${r.violations.length ? r.violations.map((v: any) => `${v.id}(${v.impact})`).join(' ') : 'ok'}`);
    for (const v of bad) for (const n of v.nodes.slice(0, 3)) console.log(`      ${v.id}: ${n.target.join(' ')}`);
  }
  await ctx.close();
}
// narrow window: the workspace must fall back to tabs
if (docs[0]) {
  const page = await browser.newPage({ viewport: { width: 900, height: 900 } });
  await page.goto(`${BASE}/doc?id=${docs[0].id}`);
  await page.waitForTimeout(2000);
  const tabs = await page.locator('[role=tab]').count();
  console.log(`narrow 900px: ${tabs ? 'tabs layout' : 'NO TABS'}`);
  if (!tabs) failed++;
}
await browser.close();
process.exit(failed ? 1 : 0);
