// 05_playwright_generic.mjs
// Generic Playwright scraper for JS-rendered exhibitor/sponsor pages.
// Loads the page, waits for network idle, scrolls to trigger lazy-load,
// clicks "Show more"/"Load more" until it disappears, dumps HTML+text+API responses.
//
// Usage:
//   node 05_playwright_generic.mjs <url> <outDir> [--selector=H3_CLASS]
// Example:
//   node 05_playwright_generic.mjs "https://www.globalgamingexpo.com/en-us/attend/exhibitor-list.html" "./out/G2E" --selector="h3.exhibitor-name"
//   node 05_playwright_generic.mjs "https://reg.salesforce.com/flow/plus/df26/sponsors/page/sponsorlisting" "./out/Dreamforce"
//
// Prerequisite: npm install playwright && npx playwright install chromium

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const [, , url, outDir, ...flags] = process.argv;
if (!url || !outDir) {
  console.error('Usage: node 05_playwright_generic.mjs <url> <outDir> [--selector="h3.exhibitor-name"]');
  process.exit(1);
}
const selector = flags.find(f => f.startsWith('--selector='))?.split('=')[1];
fs.mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({
  userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
  viewport: { width: 1440, height: 900 },
});
const page = await ctx.newPage();

// Capture XHR/fetch responses matching common data-source keywords.
const apiCalls = [];
page.on('response', async (res) => {
  const u = res.url();
  const ct = res.headers()['content-type'] || '';
  if ((u.includes('exhibit') || u.includes('sponsor') || u.includes('company') ||
       u.includes('/api/') || u.includes('/graphql')) && ct.includes('json')) {
    try {
      const body = await res.text();
      if (body.length > 500 && body.length < 3_000_000) {
        apiCalls.push({ url: u, status: res.status(), len: body.length, body });
      }
    } catch {}
  }
});

console.log('Loading', url);
try {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 90000 });
  await page.waitForTimeout(5000);   // let JS run
} catch (e) {
  console.error('goto failed:', e.message);
  await browser.close();
  process.exit(2);
}

// Scroll aggressively to trigger lazy-loading
for (let i = 0; i < 40; i++) {
  await page.evaluate((y) => window.scrollBy(0, y), 900);
  await page.waitForTimeout(150);
}

// Click any "Show more" / "Load more" button until it disappears (max 20 iterations)
for (let i = 0; i < 20; i++) {
  const btn = await page.$('button:has-text("Load more"), button:has-text("Show more"), a:has-text("Load more"), a:has-text("Show more")');
  if (!btn) break;
  try { await btn.click(); } catch { break; }
  await page.waitForTimeout(1500);
}
await page.waitForTimeout(3000);

const html = await page.content();
const text = await page.evaluate(() => document.body.innerText);
const finalUrl = page.url();

fs.writeFileSync(path.join(outDir, 'rendered.html'), html);
fs.writeFileSync(path.join(outDir, 'rendered.txt'), text);
apiCalls.forEach((c, i) => fs.writeFileSync(path.join(outDir, `api_${i}.json`), c.body));

console.log(`\n=== ${finalUrl} ===`);
console.log(`html: ${html.length} bytes, text: ${text.length} bytes, API responses saved: ${apiCalls.length}`);

// Optional selector-based extraction if --selector given
if (selector) {
  const items = await page.evaluate((sel) => {
    return [...document.querySelectorAll(sel)]
      .map(el => el.textContent.trim())
      .filter(t => t.length >= 2 && t.length <= 120);
  }, selector);
  const uniq = [...new Set(items)];
  console.log(`Selector "${selector}" matched ${uniq.length} unique items:`);
  uniq.slice(0, 20).forEach(n => console.log(`   ${n}`));
  fs.writeFileSync(path.join(outDir, 'selector_matches.txt'), uniq.join('\n'));
}

await browser.close();
console.log(`\nAll output in ${outDir}. Next: inspect rendered.txt for tier headings + company blocks, or api_*.json for JSON data source.`);
