# Property Deal Analyzer (HouseAble)

Clean-room rebuild of the ReiSearch property page, built exactly to
`~/workspace/reisearch-clone/BLUEPRINT.md` — every feature re-implemented in our own
code on free data sources. No ReiSearch code, branding, or paid APIs.

## Run it

```bash
cd ~/workspace/reisearch-clone/app
python3 -m venv .venv            # first time only (a venv already exists here)
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py          # serves http://127.0.0.1:5050
```

- Port override: `PDA_PORT=8080 .venv/bin/python app.py`
- Database: single SQLite file `analyzer.db` next to `app.py` (override with `PDA_DB=/path/file.db`).
  Delete it to start empty; the schema recreates itself on boot.
- The shipped `analyzer.db` contains the verified test property (110 E Garland Ct, Tampa)
  with its pulled comps and scenarios, so the app is populated on first open.

## How it works (blueprint mapping)

- **Add Property**: address → **ArcGIS geocode** (primary — OpenStreetMap/Nominatim returns
  nothing for some real streets, e.g. Garland Ct) → **FEMA NFHL flood zone** (layer 28,
  hazards.fema.gov/arcgis) → property page with a 42-slot Documents checklist auto-seeded.
- **Property page** mirrors ReiSearch's layout/order: left nav (Dashboard, Properties,
  Documents, Contractors, Settings) → photo/price header + deal-method badge → Property
  Details + Market Value estimates → Deal Structure (SubTo Bank Loan block, liens,
  wholesale, closing) → Disposition → tabs in ReiSearch order:
  Comparables | Demographics | Underwriting | Floor Plans | Contractors | Market Data |
  Taxes (+ Documents). (v1.3: the sticky verdict bar, Lead Monitoring, and Skiptrace were
  removed at Sherif's direction — see the v1.3 section.)
- **Comps**: HomeHarvest queried **by ZIP + radius with ZIP/coordinate post-filtering**
  (never raw full-address mode — it mis-resolves). Sold 365d + rentals. Views switch
  **in place** (no page reload): pill bar Maps | Sold Comps | Co-Living | Short Term
  Rental | Mid Term Rental | Long Term Rental | Affordable Housing, every view rendered
  server-side into the page. Each comp card keeps its photo, price, badges, specs, diffs
  and pin button, and gains a **mini-map strip** (~130px, subject + that comp) with a
  Fullscreen button opening a full-viewport overlay map with all pins; the one large
  map lives in the Maps view. Pin/unpin recomputes the pinned averages + ARV live via
  fetch and persists. Match score uses ReiSearch's exact weights — distance 40 / sqft 20 /
  beds 13 / baths 13 / year 13 — one score used everywhere. ARV = average of
  adjusted-comp average and $/sqft method, shown.
- **Underwriting (v1.3 rebuild to the live-inspected ReiSearch spec)**: strategy
  **segmented button bar** (Fix & Flip | Co-Living | Short Term | Mid Term | Long Term)
  swapping panels in place, left accordion column (~73%) + right sidebar (~25%).
  Sections in ReiSearch order per strategy: Post Renovation Property Details; Repair &
  Renovation Costs (room-by-room Material/Labor rows **or** the estimator — Average /
  Cosmetic $25–$35/sqft, Heavy / Full Gut $40–$50+/sqft × living sqft, "Use midpoint"
  applies $30/$45 per sqft; header shows CALCULATED TOTAL vs ESTIMATED TOTAL);
  Appliance Costs; Co-Living Conversion Costs (Co-Living only); Furnishing Costs;
  Operating Revenue (Co-Living per-room table, rent $/wk, vacancy default 15%, prefilled
  from the pulled ZIP benchmark; STR nightly model; MTR/LTR monthly, prefilled from the
  pinned/top-3 rental average); Operating Expenses (incl. PadSplit booking fee —
  first-days fee once per booking amortized monthly — service fee % of EGI, management
  fee % gross/net); Funding Structure with preset buttons (Cash, Conventional, DSCR,
  Hard Money, Private Money, Seller Financing, Subject-To, Custom Stack), a Funding
  Snapshot card, and **Existing Mortgage Data** nested inside Funding for every strategy
  except Fix & Flip (Price Details incl. Total Entry Fee + PITI fields, all feeding the
  math: entry fee → cash needed, PITI payment → debt service); Holding Costs; Sale &
  Exit (Flip only, ARV from pinned comps with the pinned-comp table). **All math is
  reactive in the browser** (a JS mirror of the server formulas — no Calculate button);
  saving a scenario posts the form and the server recomputes authoritatively. Right
  sidebar: tabbed **Underwriting Summary** (Overview | Visual | Scenarios | Details |
  Capital) for Co-Living and Fix & Flip; sparse **Deal Analysis Summary** (Cash Outlay,
  Income, Expenses) for Short/Mid/Long Term. Co-Living also gets the main-column bottom
  summary (Cash Needed / Net CF / CoC / DSCR cards, MONEY IN / MONEY OUT block, Year 1
  Returns, Per-Room Economics). Scoring keeps ReiSearch's exact weights (CF/room 25,
  DSCR 20, CoC 20, cap 15, break-even 12, downside CF 8) and the v1.2 realism behavior
  only as default values (taxes/insurance prefilled when $0, with the same v1.2 score
  penalties — nothing new invented). Scenarios save server-side per property; the
  saved-scenarios table flags the **best exit** across strategies. Amortization
  calculator renders the annual interest/principal/balance table.
- **Demographics**: US Census ACS 5-year, keyless, ZIP vs city toggle, source+date stamped.
- **Nearby amenities (v1.1)**: on the Demographics (Area) tab, a **Nearby** section pulled live
  from **OpenStreetMap Overpass** (free, no key) around the property's stored ArcGIS
  coordinates (~1 mile): schools, groceries/supermarkets, hospitals/clinics, pharmacies,
  parks, transit stops — name + distance in miles, grouped by category, stamped
  "OpenStreetMap, pulled \<date\>". The result is **cached on the property record** (no
  re-pull per page load); a **Refresh** button re-pulls. Failed pulls show an honest
  "Not found" status — never fake data. Implementation detail: Overpass is queried via
  **GET** (POST stalls behind some proxies/firewalls) and a full pull takes ~25–40s,
  which is exactly why it is cached.
- **Market Data**: computed from your stored comps (median price, $/sqft, DOM, monthly
  trend) — always fresh, always stamped, unlike ReiSearch's stale Redfin tab.
- **Taxes**: real editable county fields (market/assessed/taxable, exemptions, millage,
  sales history). Blank = "Not found", never invented.
- **Documents**: the full 42-slot checklist (Contracts incl. redacted variants and
  Assignment A→B / B→C, Mortgage, TC/Title, Property, Financials, Contractors, Utilities,
  Other) with check-off + file-reference notes per property.
- **Contractors**: database CRUD + per-property assignment by trade with walkthrough + quote.
  **Pre-loaded (v1.1)** from HouseAble's recruited contractor database
  (`~/workspace/contractor-recruiting/contractor-prospects.json`): business/contact name,
  inferred trade, service area, phone + email extracted from the public contact info, and
  score/category/licensing notes. Re-import any time from the Contractors page button
  ("Import / refresh from prospect database") — it is **idempotent** (dedupes by
  name + phone), so re-running adds nothing twice.
- **Floor Plans**: upload + notes, saved in the app (not device-only).
- **Settings**: buy boxes, optional free API keys
  (Census/RentCast/apillow, stored server-side only), and **Export/Import JSON + CSV**
  covering every table — the migration path to the hosted version later.

Every number carries a source + date. Missing data shows "Not found" — nothing is invented.

## v1.3 — Sherif's parity corrections (2026-11-01)

Built from a live, logged-in inspection of ReiSearch's property page (spec:
`~/workspace/reisearch-clone/research/reisearch-uw-spec.md`), applying Sherif's fix
list from testing v1.2 live:

**Removed entirely (Sherif: "does not serve me"):** Lead Monitoring (feature, nav,
dashboard alerts), Skiptrace (tab), the sticky/floating red deal-score panel, and the
Adjustment Worksheet under the comp cards. Comp cards themselves are unchanged.
Deal Tracker / presentations / dispo boards are **not wanted** — they are out of
scope, not gaps.

**Comparables:** views switch in place with JavaScript (no reload, URL and scroll
kept); the big map moved out of the card views into its own first pill (Maps); every
comp card gained ReiSearch's mini-map strip with a Fullscreen overlay map; pin/unpin
still drives averages + ARV live and persists.

**Underwriting:** full rebuild to the spec — see the Underwriting bullet above.
Rehab estimator uses Sherif's numbers (Cosmetic $25–$35/sqft, Heavy $40–$50+/sqft;
Garland 1,758 sqft → $43,950–$61,530 / $70,320–$87,900; midpoint apply $52,740 /
$79,110). Subject-To deals now read clearly: $20K entry fee + $1,700 PITI in Existing
Mortgage Data flow straight into Cash Needed and monthly debt service. v1.2 saved
scenarios (incl. Garland's Co-Living base, Co-Living base v2, Flip base) load
unchanged through a canonical-input compatibility layer.

**Verified by a browser click-through of the shipped zip (2026-11-01, Playwright/Chromium,
fresh install on port 5053 — 33/33 checks passed, zero uncaught JS errors; screenshots in
`~/workspace/reisearch-clone/research/v13-shots/`, script `research/gate_v13.py`):**
Garland comps — Sold → Co-Living → Long Term → back with **no page reload** (URL + JS
marker survived), 80 mini-map strips on the sold cards, fullscreen overlay opened/closed;
unpinning a pinned comp moved the average **$378,300 → $369,950** and ARV **$401,109 →
$381,173**, persisted after reload, and re-pinning restored both exactly. Underwriting —
all 5 strategies render their sections in spec order; Co-Living revenue prefilled
(4 rooms × $189/wk from the saved scenario; fresh panels prefill $154/wk from the pulled
ZIP benchmark), raising utilities by $300/mo dropped Net Cash Flow **$1,510 → $1,210**
(exactly $300, no button click); Long Term + Subject-To preset + entry fee $20,000 +
PITI $1,700 moved Cash Needed **$379,300 → $399,300** and debt service **$0 → $1,700/mo**
exactly; Fix & Flip estimator showed Cosmetic **$43,950 – $61,530** and midpoint apply
set **$52,740** (Net Profit −$11,891, All-in $385,440, 70%-rule MAO $231,482 with those
inputs). Monitoring 404s and is absent from nav; no Skiptrace, no sticky verdict panel,
no Adjustment Worksheet, no JSON textareas anywhere in Underwriting; Garland's three
v1.2 scenarios still listed and loading. The shipped `analyzer.db` is byte-identical to
its pre-build backup (md5-verified) — all gate interactions ran against the fresh
install's own copy.

## v1.2 — ReiSearch parity pass + reliability (2026-10-31)

**Reliability first (why v1.1 felt "dead" on a PC):** comp pulls measured 8–32s *synchronous*
with zero feedback, the demographics tab could hang ~45s on a keyless Census call, the
subject-photo code was a comment-only stub, and 75 of 88 comp photos sat in the DB never
rendered. v1.2 fixes all of it:

- **Nothing is silent anymore.** Pulls (comps, amenities, demographics, monitor checks) run
  as background jobs: the click lands instantly, a status banner shows "Working…", the page
  refreshes itself when the job finishes, and the banner reports the outcome
  ("✓ 81 sold comps + 8 rental comps found") or a plain-English error with the reason.
  Jobs running over 10 minutes are marked timed out instead of spinning forever.
- **Demographics can't hang the tab:** Census calls time out at 8s and the result — including
  an honest "Not found" — is cached on the property.
- **Photos are real now:** pulling comps also finds the subject's own listing (exact
  address match, else nearest listing within 0.05 mi) in HomeHarvest for-sale/sold data and
  stores its full photo set (`primary_photo` + `alt_photos`). The property header is a
  ReiSearch-style gallery (cover + thumbnails + "+N more"). If the property has no listing
  in the data, the header says so honestly instead of showing a broken image.
- **Pinned requirements** (`flask==3.1.3`, `homeharvest==0.8.18`, `pandas==2.3.3`,
  `requests==2.34.2`) so a PC install resolves exactly what was tested.

**ReiSearch parity checklist:**

| ReiSearch feature | Here? |
|---|---|
| Property header with photo gallery (+N more) | ✅ Yes — auto-pulled from the listing |
| Comp views: Sold / Co-Living / Long Term / Short Term / Affordable Housing | ✅ Yes (STR = honest empty state, see below) |
| Comp cards: photo, price, score + breakdown, diffs vs subject, distance | ✅ Yes |
| Map with pins; pin/unpin comps | ✅ Yes — gold pins = pinned; clicking a pin jumps to its card |
| Pinned comps drive header averages + ARV; top-3 auto-pinned after a pull | ✅ Yes |
| Radius presets + custom; sold-date window (30d/90d/6mo/1yr) | ✅ Yes |
| Underwriting: Fix & Flip, Co-Living, Short, Mid, Long Term — ReiSearch math | ✅ Yes |
| Co-Living room rent from ZIP benchmark (theirs: PadSplit stats) | ✅ Derived from rental comps, method labeled — no free PadSplit feed exists |
| Demographics (ZIP vs city) | ✅ Yes (Census ACS, cached) |
| Market Data | ✅ Yes — computed live from your comps (theirs was a stale Redfin tab) |
| Taxes tab | ✅ Real county fields (theirs is still "Coming Soon") |
| Documents checklist (42 slots) | ✅ Yes |
| Contractors | ✅ Yes — incl. HouseAble prospect preload |
| Floor plans (BluPad) | ⚠️ Partial — upload + notes saved in-app; no drawing tool |
| Comp views switch in place (no reload) | ✅ Yes (v1.3) — all views rendered server-side, pills swap visibility |
| Mini-map strip on every comp card + fullscreen overlay map | ✅ Yes (v1.3), mirroring ReiSearch |
| Reactive underwriting (no Calculate button) + segmented strategy bar | ✅ Yes (v1.3) — JS mirror of the server formulas, live totals everywhere |
| Rehab estimator ($/sqft levels with midpoint apply) | ✅ Yes (v1.3) — Cosmetic $25–$35, Heavy $40–$50+ per sqft |
| Existing Mortgage Data / Subject-To entry fee + PITI in the math | ✅ Yes (v1.3) — nested in Funding Structure, all strategies except Flip |
| Short Term Rental comps | ❌ No free nationwide STR comp feed exists (ReiSearch showed 0 for Garland too) — nightly rate is manual in Underwriting |
| Lead Monitoring / Skiptrace | 🚫 Removed in v1.3 at Sherif's direction — not wanted |
| Presentations generator / Dispo board / Deal Tracker / team logins | 🚫 Not wanted (Sherif, 2026-11-01) — out of scope, not gaps |

**Getting started strip:** a property that still needs work shows three big steps —
1 Pull comps → 2 Review the ARV → 3 Run underwriting — so the next action is always obvious.

**Verified by a browser click-through of the shipped zip (2026-10-31, Playwright/Chromium,
fresh install on a second port):** added 212 S Cortez Ave, Winter Springs, FL 32708 →
async pull finished in ~19–23s with the banner "✓ 134 sold comps + 15 rental comps found ·
1 listing photo found" → Sold view 134 cards, top-3 auto-pinned (avg $352,933, ARV
$329,007) → unpinning one pinned comp moved the averages ($361,950) and ARV ($326,061),
re-pinning restored both exactly after a page reload → Co-Living benchmark $170/wk
($736/mo) prefilled the underwriting rent/room; scenario saved with a score → map rendered
152 comp markers + subject pin (Leaflet is vendored in `static/vendor/` — no CDN needed)
→ **zero uncaught JS errors**. Garland re-checked: 81 sold cards, 3 pinned (avg $378,300,
ARV $401,109), photo placeholder honestly shown (no listing in the data). Note: in the
test sandbox the Realtor photo CDN is network-blocked, so photos showed as placeholders
there; the photo URLs themselves are stored and load normally on an unrestricted
connection. The click-through also caught and fixed two real bugs: a pandas truth-value
crash when the subject's own listing was found, and an inverted pin-button style.

## Verified 2026-10-30 (live test, this codebase)

Added **110 E Garland Ct, Tampa, FL 33613** through the real UI flow:
ArcGIS geocode score 98.87 → FEMA flood **Zone X** → **80 sold comps + 8 rentals** within
1 mile, including ReiSearch's own pinned comps (112 Floral Dr $395,000 match 89.5 ·
114 Connie Ave $393,000 · 119 E 143rd Ave $425,000) → verdict **ARV $406,032** (math shown),
**MAO $249,222**, buy-box **PASS** → Co-Living scenario (4 rooms × $819, 15% vacancy,
PadSplit-style fees, all-cash stack): **score 63 "Workable"**, CF $1,470/mo, with the $0-tax/
insurance sanity penalties applied → Fix & Flip: net proceeds $373,549 (= ARV × 0.92),
70%-rule MAO $249,222 → amortization on $335,238 @ 6.495%/30yr: **$2,118/mo**, 17-row annual
table for a 17-yr term → Market tab: median $395,000, $246/sqft, DOM 67 (matches independent
research) → Export JSON (1 property, 88 comps, 3 scenarios, 42 doc slots, buy box, monitor)
re-imported into a fresh instance with identical counts.

**v1.1 verified 2026-10-30 (live, same property)**: Nearby amenities pulled from
OpenStreetMap and rendered on the Area tab — **ALDI 0.38 mi · Walmart Supercenter 0.71 mi ·
Winn-Dixie 0.74 mi · Buchanan Middle School 0.90 mi · Forcare Medical Center 0.83 mi ·
George Russell Park 0.86 mi · Hamner Tower Park 1.00 mi**, nearest transit stop
"Florida Avenue @ Garland Court" 0.12 mi; pharmacies honestly "None found within 1 mile".
Cached reload is instant; Refresh re-pulled successfully. **Contractor preload: 26 of 26
prospects imported** from `contractor-prospects.json` (phones extracted for 12, e.g.
ValuePro Remodeling 863-614-0045, Trident Roof Solutions 941-837-7717); second import run
added 0 (26 duplicates skipped) via both the CLI function and the web route. Post-upgrade
regression check: dashboard 200, comps still 88 (80 sold + 8 rentals), 42 doc slots,
export JSON/CSV intact (1 property, 88 comps, 42 docs, 26 contractors, 3 scenarios,
1 buy box, 1 monitor).

## Known limitations / deferred (honest list)

- **Census demographics**: the code path is complete (keyless ACS), but api.census.gov was
  unreachable from the build sandbox (timeouts/empty replies), so values could not be
  verified live here; the tab honestly shows "Not found" when the API can't be reached and
  will populate on a normal network. City toggle covers Tampa/Orlando/Jacksonville/
  Kissimmee/Winter Springs place codes in v1.
- **Amenity POIs** — DONE in v1.1 (see above). One caveat: Overpass POST requests stall
  from some networks (observed in the build sandbox, where POST timed out but GET
  succeeded in ~40s); the app uses GET and caches, so worst case is a slow first pull
  or an honest failure status on the tab.
- GHL buyer-pool matching plugs in through Settings import when an export is provided
  (contractor preload from the recruiting database is done — see above).
- **Subject photo**: auto-filled since v1.2 from the property's own listing row in the
  ZIP-mode HomeHarvest pull (never raw full-address mode — it mis-resolves). A property
  with no current/recent listing in that data honestly shows "No photos found"
  (Garland is such a case: it is not in the for-sale or sold feeds).
- Presentations generator, dispo marketplace board, Deal Tracker, and team
  logins/multi-user auth are **out of scope** — Sherif confirmed 2026-11-01 he does not
  want them. The data model and export/import remain the migration path to the hosted
  version: one codebase, clean data layer in `app.py`.
