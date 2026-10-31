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
  Lead Monitoring, Documents, Contractors, Settings) → photo/price header + deal-method
  badge → sticky **verdict bar** (deal score + confidence, ONE reconciled ARV with the math
  shown, MAO, buy-box PASS/FAIL) → Property Details + Market Value estimates → Deal Structure
  (SubTo Bank Loan block, liens, wholesale, closing) → Disposition → tabs in ReiSearch order:
  Comparables | Demographics | Underwriting | Floor Plans | Contractors | Market Data |
  Taxes | Skiptrace (+ Documents).
- **Comps**: HomeHarvest queried **by ZIP + radius with ZIP/coordinate post-filtering**
  (never raw full-address mode — it mis-resolves). Sold 365d + rentals. Leaflet map pins +
  sortable table with $/sqft, per-comp $ adjustments, pin/exclude. Match score uses
  ReiSearch's exact weights — distance 40 / sqft 20 / beds 13 / baths 13 / year 13 — one
  score used everywhere. ARV = average of adjusted-comp average and $/sqft method, shown.
- **Underwriting**: 5 strategies (Fix & Flip, Co-Living, Short Term, Mid Term, Long Term)
  sharing one shell (rehab/appliance/furnishing/conversion line items as JSON, funding-stack
  builder, holding costs). Co-Living scoring uses the exact weights (CF/room 25, DSCR 20,
  CoC 20, cap 15, break-even 12, downside CF 8) and every strategy is scored (an improvement
  over ReiSearch). **Realism penalties**: $0 taxes/insurance get sanity defaults and point
  deductions — a $0-expense deal cannot score "Strong". Fix & Flip exit: ARV −6% commission
  −2% closing, profit/ROI/annualized/margin/70% rule. Scenarios (downside/base/upside/fully
  booked) save server-side per property; the saved-scenarios table flags the **best exit**
  across strategies. Amortization calculator renders the annual interest/principal/balance
  table and can feed the funding stack.
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
- **Skiptrace**: identifier-first SOP notes with Verified / Unverified / Ambiguous labels.
- **Lead Monitoring**: per-property monitors (frequency, price/DOM thresholds, stop on
  Sold/Off Market/Rented) with a manual **Check now** that re-pulls live listings.
- **Settings**: buy boxes (drive the verdict PASS/FAIL), optional free API keys
  (Census/RentCast/apillow, stored server-side only), and **Export/Import JSON + CSV**
  covering every table — the migration path to the hosted version later.

Every number carries a source + date. Missing data shows "Not found" — nothing is invented.

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
| Lead Monitoring | ✅ Yes — manual "Check now" runs async; no background scheduler yet |
| Skiptrace | ⚠️ Partial — identifier-first notes + verified labels; no paid trace data |
| Short Term Rental comps | ❌ No free nationwide STR comp feed exists (ReiSearch showed 0 for Garland too) — nightly rate is manual in Underwriting |
| Presentations generator / Dispo marketplace board / team logins | ❌ Not yet — hosted-version work |
| Deal Tracker pipeline board | ❌ Not yet — deal stage is tracked per property only |

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
- **Monitoring** is manual "Check now" in v1 (no background scheduler yet).
- Presentations generator, dispo marketplace board, and team logins/multi-user auth are
  hosted-version work (Supabase migration) — the data model and export/import are already
  shaped for it: one codebase, clean data layer in `app.py`.
