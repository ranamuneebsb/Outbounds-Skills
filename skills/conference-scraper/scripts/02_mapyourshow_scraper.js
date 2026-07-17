// 02_mapyourshow_scraper.js
// Fast-path scraper for any *.mapyourshow.com conference site.
// Pure HTTP — no browser needed. Handles pagination and per-exhibitor detail enrichment.
//
// Usage:
//   node 02_mapyourshow_scraper.js <showSlug> <version> <outDir> <confName> <confDate> <city> <state>
// Example:
//   node 02_mapyourshow_scraper.js pack26 8_0 "C:\out\PACKEXPO_2026" "PACK EXPO International 2026" "Nov 2-5 2026" "Chicago" "IL"

const fs = require('fs');
const path = require('path');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

async function fetchJSON(url, referer) {
  try {
    const res = await fetch(url, {
      headers: {
        'User-Agent': UA,
        'X-Requested-With': 'XMLHttpRequest',   // MANDATORY — 403 without this
        'Referer': referer,
      },
    });
    const text = await res.text();
    return JSON.parse(text);
  } catch (e) { return null; }
}

async function fetchText(url) {
  try {
    const res = await fetch(url, { headers: { 'User-Agent': UA } });
    const text = await res.text();
    if (res.status === 202 || text.length === 0) return '__THROTTLED__';
    return text;
  } catch (e) { return ''; }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function csvEsc(s) { return '"' + String(s || '').replace(/"/g, '""') + '"'; }

// Extract website/LinkedIn/etc from the Vue contactinfov3 block embedded in each detail page.
function extractContactInfo(html) {
  const anchorIdx = html.indexOf('contactinfov3 = Vue');
  if (anchorIdx === -1) return {};
  const window = html.slice(anchorIdx, anchorIdx + 4000);
  const block = window.match(/data: function\(\)\{[\s\S]{0,20}?return \{([\s\S]*?)\r?\n\s*\}\r?\n\s*\},?\s*\r?\n\s*mounted/);
  if (!block) return {};
  const body = block[1];
  const grab = (key) => {
    const m = body.match(new RegExp(key + '\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"'));
    return m ? m[1].replace(/\\\//g, '/').replace(/\\"/g, '"') : '';
  };
  const addressMatch = body.match(/addressValues\s*:\s*(\{[^}]*\})/);
  let address = '';
  if (addressMatch) {
    try {
      const addr = JSON.parse(addressMatch[1]);
      address = [addr.ADDRESS1, addr.ADDRESS2, addr.CITY, addr.STATE, addr.ZIP, addr.COUNTRY].filter(Boolean).join(', ');
    } catch {}
  }
  return {
    website: grab('websiteValue'),
    linkedin: grab('linkedInValue'),
    facebook: grab('facebookValue'),
    twitter: grab('twitterValue'),
    instagram: grab('instagramValue'),
    phone: grab('phoneValue'),
    address,
  };
}

async function pMapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let idx = 0;
  async function worker() {
    while (idx < items.length) {
      const cur = idx++;
      results[cur] = await fn(items[cur], cur);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

async function main() {
  const [,, showSlug, showVersion, outDir, confName, confDate, city, state] = process.argv;
  if (!showSlug) {
    console.error('Usage: node 02_mapyourshow_scraper.js <slug> <version> <outDir> <confName> <confDate> <city> <state>');
    process.exit(1);
  }
  const baseUrl = `https://${showSlug}.mapyourshow.com/${showVersion}`;
  const referer = `${baseUrl}/explore/exhibitor-gallery.cfm`;

  console.log('Fetching exhibitor list...');
  let first = await fetchJSON(`${baseUrl}/ajax/remote-proxy.cfm?action=search&searchtype=exhibitorgallery&searchsize=50&start=0`, referer);
  if (!first || !first.SUCCESS) {
    console.error('ERROR: exhibitor gallery API returned no data — check show slug/version or platform');
    process.exit(1);
  }
  const totalExh = first.DATA.totalhits;
  let exhibitors = [...first.DATA.results.exhibitor.hit];
  let start = exhibitors.length;
  while (start < totalExh) {
    await sleep(250);
    const p = await fetchJSON(`${baseUrl}/ajax/remote-proxy.cfm?action=search&searchtype=exhibitorgallery&searchsize=50&start=${start}`, referer);
    const h = p?.DATA?.results?.exhibitor?.hit;
    if (!h || h.length === 0) break;
    exhibitors.push(...h);
    start += h.length;
  }
  console.log(`Got ${exhibitors.length}/${totalExh} exhibitors. Fetching detail pages (concurrency 8, 250ms pacing)...`);

  let throttled = false;
  const details = await pMapLimit(exhibitors, 8, async (ex) => {
    if (throttled) return {};
    await sleep(250);
    const html = await fetchText(`${baseUrl}/exhibitor/exhibitor-details.cfm?exhid=${ex.fields.exhid_l}`);
    if (html === '__THROTTLED__') { throttled = true; return {}; }
    return extractContactInfo(html);
  });
  if (throttled) console.log('WARNING: platform throttled mid-run — remaining detail pages unenriched. Retry in ~5 min.');

  console.log('Fetching sessions/speakers...');
  let sessions = [];
  let firstS = await fetchJSON(`${baseUrl}/ajax/remote-proxy.cfm?action=search&searchtype=sessionfulllist&searchsize=50&start=0`, referer);
  if (firstS?.SUCCESS) {
    const totalSess = firstS.DATA.totalhits;
    sessions = [...firstS.DATA.results.session.hit];
    let sStart = sessions.length;
    while (sStart < totalSess) {
      await sleep(250);
      const p = await fetchJSON(`${baseUrl}/ajax/remote-proxy.cfm?action=search&searchtype=sessionfulllist&searchsize=50&start=${sStart}`, referer);
      const h = p?.DATA?.results?.session?.hit;
      if (!h || h.length === 0) break;
      sessions.push(...h);
      sStart += h.length;
    }
  }
  console.log(`Got ${sessions.length} sessions.`);

  fs.mkdirSync(outDir, { recursive: true });
  const header = 'Conference Name,Event Date,City,State,Country,Type,Name,Company,Company Website,LinkedIn URL,Company Description,Title/Role,Source URL,Notes';
  const rows = [header];

  exhibitors.forEach((ex, i) => {
    const d = details[i] || {};
    const name = ex.fields.exhname_t || '';
    const desc = (ex.fields.exhdesc_t || '').replace(/\s+/g, ' ').trim();
    const srcUrl = `${baseUrl}/exhibitor/exhibitor-details.cfm?exhid=${ex.fields.exhid_l}`;
    const notes = [
      d.address ? `Address: ${d.address}` : '',
      d.phone ? `Phone: ${d.phone}` : '',
      d.facebook ? `FB: ${d.facebook}` : '',
      d.twitter ? `X: ${d.twitter}` : ''
    ].filter(Boolean).join(' | ');
    rows.push([csvEsc(confName), csvEsc(confDate), csvEsc(city), csvEsc(state), csvEsc('United States'),
               csvEsc('Exhibitor'), csvEsc(''), csvEsc(name), csvEsc(d.website), csvEsc(d.linkedin),
               csvEsc(desc), csvEsc(''), csvEsc(srcUrl), csvEsc(notes)].join(','));
  });

  const seenSpeakers = new Set();
  sessions.forEach((s) => {
    const names = s.speakername || [];
    const companies = s.speakercompany || [];
    names.forEach((n, i) => {
      const key = n + '|' + (companies[i] || '');
      if (seenSpeakers.has(key)) return;
      seenSpeakers.add(key);
      const title = s.title || '';
      rows.push([csvEsc(confName), csvEsc(confDate), csvEsc(city), csvEsc(state), csvEsc('United States'),
                 csvEsc('Speaker'), csvEsc(n), csvEsc(companies[i] || ''), csvEsc(''), csvEsc(''),
                 csvEsc(''), csvEsc(title ? `Session: ${title}` : ''), csvEsc(referer), csvEsc('')].join(','));
    });
  });

  fs.writeFileSync(path.join(outDir, 'exhibitors_speakers.csv'), rows.join('\n') + '\n');

  const enrichedCount = details.filter(d => d.website || d.linkedin).length;
  const notesTxt = `MapYourShow fast-scrape via public remote-proxy.cfm API (no browser needed).
Show: ${showSlug} (${baseUrl})
Exhibitors: ${exhibitors.length} of ${totalExh} total captured.
Exhibitor detail enrichment: ${enrichedCount}/${exhibitors.length} got website and/or LinkedIn from the embedded Vue contactinfov3 data block on each detail page.
Speakers/sessions: ${sessions.length} sessions processed, ${seenSpeakers.size} unique speaker/company pairs extracted.
No CAPTCHA or login gating encountered - this platform's exhibitor gallery and session list are fully public via the AJAX search endpoint once the X-Requested-With: XMLHttpRequest header is set.
No individual attendee names are published anywhere on this platform (expected).
`;
  fs.writeFileSync(path.join(outDir, 'notes.txt'), notesTxt);
  console.log(`DONE. ${exhibitors.length} exhibitors (${enrichedCount} enriched), ${seenSpeakers.size} speakers. Written to ${outDir}`);
}

main().catch(e => { console.error(e); process.exit(1); });
