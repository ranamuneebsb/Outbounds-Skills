<!--
============================================================================
FORMAT REFERENCE ONLY — NOT A DEFAULT. The pipeline NEVER reads this file.
The user supplies their OWN ICP/classification prompt every run
(--icp-prompt PATH). Never reuse, assume, or invent ICP rules from a previous
campaign. If the ICP prompt is missing, ASK the user for it.
============================================================================
-->

# ICP — <campaign name>  (reference shape — user provides their own each run)

The user's icp.md is injected verbatim into the classifier system prompt
(classify_icp.py wraps it with the fixed JSON-output and scoring scaffold). It
is plain English. The classifier judges WHO THE AD IS SPEAKING TO from the ad
copy. Keep it tight — one screen is plenty.

These are the sections an ICP prompt typically carries (the user fills them in):

---

TARGET ICP: <one sentence — who you are trying to reach, e.g. "B2B service
businesses selling high-ticket services to OTHER BUSINESSES">

QUALIFIES (score 7-10) — ad targets <your buyer> as the customer:
<comma- or semicolon-separated list of business types that QUALIFY. Be concrete.
e.g. Managed IT/MSP/cybersecurity; custom software dev / B2B SaaS;
accounting/bookkeeping/fractional CFO for businesses; B2B law firms;
business brokers / M&A advisory; commercial financing / equipment leasing; ...>

DISQUALIFY (score 1-3):
<list what does NOT qualify and why. e.g.
- Ad speaks to CONSUMERS (gyms, salons, dental patients, ecommerce shoppers,
  residential real estate, personal coaching, weight loss, consumer insurance)
- COMPETITORS (lead gen, appointment setting, cold email, paid-ads agencies)
- RECRUITING individuals (IMO / insurance-agent / MLM / "become an agent")
- course sellers / make-money-online / coaching individuals to start a business
- pure job postings (advertiser is only hiring, not selling a service)>

KEY TEST: <the one-line discriminator, e.g. "business owner buying a service =
qualify; individual consumer or recruit = disqualify">

CLARIFICATIONS (apply strictly — these are the edge cases that trip the model):
1. <e.g. FRANCHISE: a brand recruiting franchisees to OPEN a location = QUALIFY
   (niche: Franchise Development); this is DIFFERENT from MLM / agent recruiting
   = DISQUALIFY.>
2. <e.g. INSURANCE: workers' comp, general liability, commercial auto, group
   benefits are bought by EMPLOYERS = QUALIFY; life/personal auto/home for
   individuals = DISQUALIFY.>
3. <e.g. COACHING: coaching EXISTING business owners to scale a real company =
   QUALIFY; coaching individuals to start a business / trade / make money online
   = DISQUALIFY.>

NICHE (the classifier must return exactly one of these strings; list every niche
you want to bucket Qualified advertisers into, then OTHER / NOT-ICP / ERROR):
<Niche A | Niche B | Niche C | ... | OTHER B2B | NOT B2B | ERROR>
