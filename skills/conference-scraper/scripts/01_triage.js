// 01_triage.js — Detect which platform each conference site uses.
// Usage:  node 01_triage.js sites.csv   (csv format: name,url  — one per line, no header)
// Or edit the SHOWS array below and run: node 01_triage.js
//
// Prints platform detection + likely exhibitor/sponsor page hints for each conference.

const fs = require('fs');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';
const HEADERS = {
  'User-Agent': UA,
  'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
  'Accept-Language': 'en-US,en;q=0.9',
  'Sec-Fetch-Dest': 'document',
  'Sec-Fetch-Mode': 'navigate',
  'Sec-Fetch-Site': 'none',
  'Upgrade-Insecure-Requests': '1',
};

const PLATFORMS = {
  mapyourshow:   /mapyourshow\.com/i,
  a2z_personify: /libs\.a2zinc\.net|s\d+\.a2zinc\.net|core-apps\.com|personifycorp/i,
  smallworldlabs:/smallworldlabs\.com/i,
  eBooth_a2z:    /eBooth\.aspx\?BoothID=|Public\/EventMap\.aspx/i,
  informa_sitefinity: /informaconnect\.com/i,
  spargo:        /events\.jspargo\.com/i,
  expoplatform:  /api-[a-z0-9]+\.expoplatform\.com|expoplatform\.com/i,
  bizzabo:       /bizzabo\.com|bz-event/i,
  goexpo:        /goexposoftware\.com/i,
  cvent:         /cvent\.com/i,
  swapcard:      /swapcard\.com/i,
  brella:        /brella\.io/i,
  rainfocus:     /rainfocus\.com/i,
};

const SHOWS = [
  ['Example Show A', 'https://www.example-conference.com'],
  ['Example Show B', 'https://another-conference.org'],
];

async function fetchOnce(url) {
  try {
    const res = await fetch(url, { headers: HEADERS, redirect: 'follow' });
    const html = await res.text();
    return { ok: true, status: res.status, finalUrl: res.url, html };
  } catch (e) {
    return { ok: false, error: String(e).slice(0, 120) };
  }
}

function detectPlatforms(html) {
  return Object.entries(PLATFORMS).filter(([, re]) => re.test(html)).map(([k]) => k);
}

function findMenuLinks(html) {
  const links = [...html.matchAll(/<a[^>]+href="([^"]+)"[^>]*>([^<]{0,80})<\/a>/g)];
  const hits = { exhibitor: [], sponsor: [], attendee: [], partner: [], floor: [] };
  for (const [, href, text] of links) {
    const combined = (href + ' ' + text).toLowerCase();
    if (/exhibit/.test(combined)) hits.exhibitor.push({ href, text: text.trim() });
    if (/sponsor/.test(combined)) hits.sponsor.push({ href, text: text.trim() });
    if (/attend/.test(combined)) hits.attendee.push({ href, text: text.trim() });
    if (/partner/.test(combined)) hits.partner.push({ href, text: text.trim() });
    if (/floor[- ]?plan|show[- ]?floor|exhibit[- ]?hall|expo[- ]?floor/.test(combined)) hits.floor.push({ href, text: text.trim() });
  }
  return hits;
}

async function main() {
  let shows = SHOWS;
  if (process.argv[2]) {
    const csv = fs.readFileSync(process.argv[2], 'utf-8');
    shows = csv.split(/\r?\n/).filter(Boolean).map(line => line.split(','));
  }
  console.log(`Triaging ${shows.length} conferences...\n`);

  for (const [name, url] of shows) {
    const r = await fetchOnce(url);
    if (!r.ok) {
      console.log(`--- ${name}`);
      console.log(`   URL: ${url}`);
      console.log(`   ERROR: ${r.error}\n`);
      continue;
    }
    const platforms = detectPlatforms(r.html);
    const menu = findMenuLinks(r.html);
    console.log(`--- ${name}`);
    console.log(`   URL: ${url}   (${r.status}, ${r.html.length} bytes)`);
    console.log(`   Platform(s): ${platforms.length ? platforms.join(', ') : 'custom / unknown'}`);
    ['exhibitor', 'sponsor', 'attendee', 'partner', 'floor'].forEach(k => {
      if (menu[k].length) {
        const shown = menu[k].slice(0, 3).map(l => `${l.text || '(no text)'} => ${l.href.slice(0, 80)}`);
        console.log(`   ${k}: ${menu[k].length} links, e.g.  ${shown.join(' | ')}`);
      }
    });
    console.log('');
  }
}

main().catch(e => { console.error(e); process.exit(1); });
