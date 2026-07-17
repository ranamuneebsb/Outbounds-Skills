// 04_header_menu_hunt.js
// For custom-platform sites: fetch homepage, list every link in the nav/header that
// looks like it points at an exhibitor / sponsor / attendee / partner / floor page.
// This is Step 4 in the process — feed the output URLs into 05_playwright_generic.mjs
// (or curl with browser headers) to actually extract company data.
//
// Usage: node 04_header_menu_hunt.js <conference_homepage_url>

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

const KEYWORDS = {
  exhibitor: /exhibit(?!ing\s+opportunit)/i,
  sponsor:   /sponsor(?!ship)/i,
  attendee:  /attend|attending|who[- ]?is/i,
  partner:   /partner/i,
  floor:     /floor[- ]?plan|show[- ]?floor|exhibit[- ]?hall|expo[- ]?floor/i,
};

function absolutize(href, base) {
  try { return new URL(href, base).href; }
  catch { return href; }
}

async function main() {
  const homepage = process.argv[2];
  if (!homepage) {
    console.error('Usage: node 04_header_menu_hunt.js <conference_homepage_url>');
    process.exit(1);
  }
  const res = await fetch(homepage, { headers: HEADERS, redirect: 'follow' });
  const html = await res.text();
  console.log(`Homepage: ${res.url}  (${res.status}, ${html.length} bytes)\n`);

  const links = [...html.matchAll(/<a[^>]+href="([^"]+)"[^>]*>([^<]{0,120})<\/a>/g)];
  const buckets = {};
  for (const [, href, text] of links) {
    const cleanText = text.replace(/\s+/g, ' ').trim();
    const combined = href + ' | ' + cleanText;
    for (const [tag, re] of Object.entries(KEYWORDS)) {
      if (re.test(combined)) {
        if (!buckets[tag]) buckets[tag] = [];
        const abs = absolutize(href, res.url);
        // dedupe by URL
        if (!buckets[tag].some(l => l.href === abs)) {
          buckets[tag].push({ href: abs, text: cleanText });
        }
      }
    }
  }

  for (const tag of ['exhibitor', 'sponsor', 'attendee', 'partner', 'floor']) {
    if (!buckets[tag]) continue;
    console.log(`--- ${tag.toUpperCase()} (${buckets[tag].length} link${buckets[tag].length !== 1 ? 's' : ''})`);
    for (const l of buckets[tag]) {
      console.log(`   ${l.text.slice(0, 60).padEnd(60)}  ${l.href}`);
    }
    console.log('');
  }

  if (Object.keys(buckets).length === 0) {
    console.log('No exhibitor/sponsor/attendee/partner links found in nav. Site may be a full JS SPA — try 05_playwright_generic.mjs.');
  }
}

main().catch(e => { console.error(e); process.exit(1); });
