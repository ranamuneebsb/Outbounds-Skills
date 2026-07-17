// 03_informa_ebooth_scraper.js
// For Informa's classic a2z EventMap platform (eBooth.aspx?BoothID=... link pattern).
// Used successfully for Newtopia Now 2026 (375 exhibitors).
//
// Usage:
//   node 03_informa_ebooth_scraper.js <baseUrl> <listPath> <outDir> <confName> <confDate> <city> <state>
// Example (Windows path — use backslashes to avoid Git-Bash path munging):
//   node 03_informa_ebooth_scraper.js "https://exhibitor.newtopianow.com/2026" "/Public/EventMap.aspx" "C:\out\NewtopiaNow_2026" "Newtopia Now 2026" "Aug 19-20 2026" "Denver" "CO"

const fs = require('fs');
const path = require('path');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

async function fetchText(url) {
  try {
    const res = await fetch(url, { headers: { 'User-Agent': UA } });
    return await res.text();
  } catch { return ''; }
}

function csvEsc(s) { return '"' + String(s || '').replace(/"/g, '""') + '"'; }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function pMapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let idx = 0;
  async function worker() {
    while (idx < items.length) { const cur = idx++; results[cur] = await fn(items[cur], cur); }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

async function main() {
  const [,, baseUrl, listPath, outDir, confName, confDate, city, state] = process.argv;
  if (!baseUrl) {
    console.error('Usage: node 03_informa_ebooth_scraper.js <baseUrl> <listPath> <outDir> <confName> <confDate> <city> <state>');
    process.exit(1);
  }

  console.log('Fetching exhibitor list from:', `${baseUrl}${listPath}`);
  const listHtml = await fetchText(`${baseUrl}${listPath}`);
  const matches = [...listHtml.matchAll(/href="(eBooth\.aspx\?[^"]+)"[^>]*>\s*([^<]+?)\s*</g)];
  const exhibitors = matches.map(m => ({
    href: m[1].replace(/&amp;/g, '&'),
    name: m[2].trim(),
  }));
  console.log(`Found ${exhibitors.length} exhibitor links.`);

  const details = await pMapLimit(exhibitors, 4, async (ex) => {
    await sleep(150);
    const html = await fetchText(`${baseUrl}/Public/${ex.href}`);
    const text = html
      .replace(/<script[\s\S]*?<\/script>/g, '')
      .replace(/<style[\s\S]*?<\/style>/g, '')
      .replace(/<[^>]+>/g, ' ')
      .replace(/[ \t]+/g, ' ')
      .replace(/\n\s*\n+/g, '\n')
      .trim();
    const idx = text.indexOf(ex.name);
    const chunk = idx !== -1 ? text.slice(idx, idx + 300) : '';
    const websiteMatch = chunk.match(/(www\.[a-zA-Z0-9.-]+\.[a-z]{2,}|https?:\/\/[a-zA-Z0-9.\/-]+)/);
    return {
      website: websiteMatch ? websiteMatch[1] : '',
      raw: chunk.slice(0, 200),
    };
  });

  fs.mkdirSync(outDir, { recursive: true });
  const header = 'Conference Name,Event Date,City,State,Country,Type,Name,Company,Company Website,LinkedIn URL,Company Description,Title/Role,Source URL,Notes';
  const rows = [header];
  exhibitors.forEach((ex, i) => {
    const d = details[i] || {};
    const website = d.website && !d.website.includes('eBooth')
      ? (d.website.startsWith('http') ? d.website : 'https://' + d.website)
      : '';
    rows.push([csvEsc(confName), csvEsc(confDate), csvEsc(city), csvEsc(state), csvEsc('United States'),
               csvEsc('Exhibitor'), csvEsc(''), csvEsc(ex.name), csvEsc(website), csvEsc(''),
               csvEsc(''), csvEsc(''), csvEsc(`${baseUrl}/Public/${ex.href}`), csvEsc(d.raw)].join(','));
  });
  fs.writeFileSync(path.join(outDir, 'exhibitors_speakers.csv'), rows.join('\n') + '\n');

  const enriched = details.filter(d => d.website).length;
  fs.writeFileSync(path.join(outDir, 'notes.txt'), `Informa/a2z classic EventMap fast-scrape (eBooth.aspx pattern), direct HTTP, no browser.
Exhibitors: ${exhibitors.length} captured from the full server-rendered list page.
Website enrichment: ${enriched}/${exhibitors.length} (this platform only exposes website + city/state/country per exhibitor, no description/LinkedIn field exists on this system).
No CAPTCHA/login gating encountered.
No individual attendee names published (expected).
`);
  console.log(`DONE. ${exhibitors.length} exhibitors, ${enriched} with website. Written to ${outDir}`);
}

main().catch(e => { console.error(e); process.exit(1); });
