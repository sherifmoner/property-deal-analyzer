"""Property Deal Analyzer — HouseAble clean-room rebuild of the ReiSearch property page.
Flask + SQLite, server-rendered. Free data only: ArcGIS geocoder, FEMA NFHL, HomeHarvest, US Census ACS.
Run: .venv/bin/python app.py  ->  http://127.0.0.1:5050
"""
import json, math, os, re, sqlite3, datetime, io, csv, threading, ast
from functools import wraps
import requests
from flask import Flask, request, redirect, url_for, render_template, jsonify, g, send_file, abort, flash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PDA_DB", os.path.join(BASE, "analyzer.db"))
UPLOAD_DIR = os.path.join(BASE, "uploads"); os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = "pda-local-dev"

# ---------------------------------------------------------------- DB layer
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def _close(_e):
    d = g.pop("db", None)
    if d: d.close()

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties(
 id INTEGER PRIMARY KEY, address TEXT, street TEXT, city TEXT, state TEXT, zip TEXT,
 lat REAL, lon REAL, flood_zone TEXT, flood_source TEXT, flood_date TEXT,
 status TEXT DEFAULT 'Draft', stage TEXT DEFAULT 'New', deal_method TEXT DEFAULT 'Cash',
 list_price REAL, photo_url TEXT, property_type TEXT, beds REAL, baths REAL, sqft REAL,
 lot_sqft REAL, year_built INTEGER, apn TEXT, hoa_dues REAL, hoa_freq TEXT, subdivision TEXT,
 legal_desc TEXT, sewer TEXT, water TEXT, electric TEXT, appliances TEXT, flooring TEXT,
 cooling TEXT, heating TEXT, roof_age TEXT, foundation TEXT, occupancy_now TEXT, occupancy_closing TEXT,
 as_is_value REAL, arv REAL, repairs REAL, mao REAL,
 purchase_price REAL, inspection_days REAL, inspection_unit TEXT, inspection_deadline TEXT,
 buy_it_now REAL, min_offer REAL, wholesale_contract_amt REAL, emd_ab REAL, emd_bc REAL,
 title_company TEXT, escrow_officer TEXT, closing_date TEXT, tc_name TEXT, offers_deadline TEXT,
 loan_balance REAL, loan_type TEXT, loan_rate REAL, loan_piti REAL,
 dispo_asking REAL, dispo_bin REAL, dispo_min REAL, dispo_inspection_days REAL, offer_review TEXT,
 est_zillow REAL, est_realtor REAL, est_redfin REAL, est_county REAL, est_date TEXT,
 notes TEXT, skiptrace_notes TEXT, skiptrace_status TEXT,
 tax_market REAL, tax_assessed REAL, tax_taxable REAL, tax_amount REAL, tax_exemptions TEXT,
 tax_millage TEXT, tax_year TEXT, sales_history TEXT,
 amenities_json TEXT, amenities_date TEXT,
 created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS liens(id INTEGER PRIMARY KEY, property_id INTEGER, lien_type TEXT,
 amount REAL, payment REAL, rate REAL, maturity TEXT);
CREATE TABLE IF NOT EXISTS comps(id INTEGER PRIMARY KEY, property_id INTEGER, kind TEXT DEFAULT 'sold',
 address TEXT, city TEXT, zip TEXT, price REAL, ppsf REAL, beds REAL, baths REAL, sqft REAL,
 year_built INTEGER, event_date TEXT, dom REAL, lat REAL, lon REAL, distance_mi REAL,
 match_score REAL, source TEXT, source_url TEXT, pinned INTEGER DEFAULT 0, excluded INTEGER DEFAULT 0,
 adj_condition REAL DEFAULT 0, adj_size REAL DEFAULT 0, adj_other REAL DEFAULT 0, photo_url TEXT);
CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, property_id INTEGER, category TEXT,
 slot TEXT, done INTEGER DEFAULT 0, notes TEXT, UNIQUE(property_id, slot));
CREATE TABLE IF NOT EXISTS contractors(id INTEGER PRIMARY KEY, name TEXT, company TEXT, trade TEXT,
 phone TEXT, email TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS assignments(id INTEGER PRIMARY KEY, property_id INTEGER, contractor_id INTEGER,
 trade TEXT, walkthrough TEXT, quote REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS scenarios(id INTEGER PRIMARY KEY, property_id INTEGER, strategy TEXT,
 name TEXT, inputs TEXT, results TEXT, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS buy_boxes(id INTEGER PRIMARY KEY, name TEXT, cities TEXT, max_price REAL,
 min_beds REAL, min_sqft REAL, strategies TEXT, min_arv_spread_pct REAL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS monitors(id INTEGER PRIMARY KEY, property_id INTEGER, frequency TEXT DEFAULT 'Weekly',
 price_threshold REAL, dom_threshold REAL, stop_on_status INTEGER DEFAULT 1, active INTEGER DEFAULT 1,
 last_checked TEXT, last_price REAL, last_status TEXT, last_note TEXT);
CREATE TABLE IF NOT EXISTS floorplans(id INTEGER PRIMARY KEY, property_id INTEGER, title TEXT,
 filename TEXT, notes TEXT, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, property_id INTEGER, kind TEXT,
 status TEXT DEFAULT 'running', started_at TEXT, finished_at TEXT, message TEXT, detail TEXT,
 created_at TEXT DEFAULT (datetime('now')));
"""

DOC_SLOTS = [
 ("Contracts","Purchase Contract"),("Contracts","Purchase Contract (Redacted)"),
 ("Contracts","Assignment A to B"),("Contracts","Assignment A to B (Redacted)"),
 ("Contracts","Assignment B to C"),("Contracts","Assignment B to C (Redacted)"),
 ("Contracts","JV Agreement"),("Contracts","Seller Disclosures"),("Contracts","Lead-Based Paint Disclosure"),
 ("Mortgage","Mortgage Statement"),("Mortgage","HELOC Statement"),("Mortgage","Payoff Statement"),
 ("Mortgage","SubTo Worksheet"),("Mortgage","Insurance Binder"),
 ("TC / Title","Title Commitment"),("TC / Title","Escrow Instructions"),("TC / Title","EMD Receipt (Buyer)"),
 ("TC / Title","EMD Receipt (Seller)"),("TC / Title","Closing Disclosure / HUD-1"),
 ("Property","Inspection Report"),("Property","Appraisal"),("Property","Survey"),
 ("Property","Floor Plan"),("Property","County Tax Statement"),("Property","Photos"),
 ("Financials","T-12"),("Financials","Profit & Loss"),("Financials","Rent Roll"),("Financials","Leases"),
 ("Contractors","Contractor Bid 1"),("Contractors","Contractor Bid 2"),("Contractors","Contractor Bid 3"),
 ("Contractors","Scope of Work"),("Contractors","Contractor Agreement"),
 ("Utilities","Electric Bill"),("Utilities","Water Bill"),("Utilities","Gas Bill"),("Utilities","HOA Statement"),
 ("Other","Skiptrace Report"),("Other","Comp Report"),("Other","Offer Presentation"),("Other","Miscellaneous"),
]
assert len(DOC_SLOTS) == 42, len(DOC_SLOTS)

def init_db():
    d = sqlite3.connect(DB_PATH); d.executescript(SCHEMA)
    # migrations for existing DBs: v1.1 amenity cache; v1.2 photo gallery + demographics cache
    cols = [r[1] for r in d.execute("PRAGMA table_info(properties)")]
    for col in ("amenities_json", "amenities_date", "photos_json", "photos_date", "demo_json", "demo_date"):
        if col not in cols: d.execute(f"ALTER TABLE properties ADD COLUMN {col} TEXT")
    d.commit(); d.close()

def get_setting(k, default=""):
    row = db().execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return row["value"] if row else default

def set_setting(k, v):
    db().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v)); db().commit()

def seed_docs(pid):
    db().executemany("INSERT OR IGNORE INTO documents(property_id,category,slot) VALUES(?,?,?)",
                     [(pid, c, s) for c, s in DOC_SLOTS]); db().commit()

# ---------------------------------------------------------------- helpers
def fnum(v, default=None):
    try:
        if v is None or v == "": return default
        return float(str(v).replace(",", "").replace("$", ""))
    except Exception: return default

def _s(v):
    """Safe string for pandas values (None / NaN / pd.NA -> '')."""
    if v is None: return ""
    if isinstance(v, float) and math.isnan(v): return ""
    if type(v).__name__ == "NAType": return ""
    return str(v)

def haversine_mi(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2): return None
    R = 3958.8; p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return round(2 * R * math.asin(math.sqrt(a)), 3)

def extract_photos(row):
    """HomeHarvest photo data: 'primary_photo' (single URL) + 'alt_photos'
    (comma-joined string, list, or stringified list depending on source). Returns clean URL list."""
    urls = []
    def add(u):
        u = str(u or "").strip()
        if u.startswith("http") and u not in urls: urls.append(u)
    add(_s(row.get("primary_photo")))
    raw = row.get("alt_photos")
    if raw is not None and not (isinstance(raw, float) and math.isnan(raw)) and type(raw).__name__ != "NAType":
        if isinstance(raw, (list, tuple)):
            for u in raw: add(u)
        else:
            s = str(raw).strip()
            if s.startswith("["):
                try:
                    for u in ast.literal_eval(s): add(u)
                except Exception: pass
            else:
                for u in s.split(","): add(u)
    return urls[:40]

_STREET_SUFFIXES = {"ct", "court", "ave", "avenue", "st", "street", "dr", "drive", "rd", "road",
                    "ln", "lane", "blvd", "boulevard", "way", "cir", "circle", "ter", "terrace",
                    "pl", "place", "trl", "trail", "hwy", "highway", "n", "s", "e", "w",
                    "north", "south", "east", "west", "ste", "unit", "apt"}

def _street_key(street):
    """(house_number, core street name) for matching a subject to its own listing row."""
    toks = re.findall(r"[a-z0-9]+", (street or "").lower())
    num = toks[0] if toks and toks[0].isdigit() else ""
    core = [t for t in toks[1:] if t not in _STREET_SUFFIXES and not t.isdigit()]
    return num, " ".join(core)

def find_subject_row(df, prop):
    """Locate the subject's own row in a HomeHarvest frame: exact house number + street name,
    else the nearest row within 0.05 mi. Returns the row or None."""
    if df is None or not len(df): return None
    pnum, pcore = _street_key(prop["street"] or prop["address"])
    best, best_dist = None, None
    for _, r in df.iterrows():
        if str(r.get("zip_code")) != str(prop["zip"]): continue
        lat, lon = r.get("latitude"), r.get("longitude")
        dist = None
        if lat is not None and lon is not None and not (isinstance(lat, float) and math.isnan(lat)):
            dist = haversine_mi(prop["lat"], prop["lon"], float(lat), float(lon))
        rnum, rcore = _street_key(_s(r.get("street")))
        if pnum and rnum == pnum and rcore and rcore == pcore:
            return r  # exact address match wins immediately
        if dist is not None and dist < 0.05 and (best_dist is None or dist < best_dist):
            best, best_dist = r, dist
    return best

def geocode_arcgis(single_line):
    """ArcGIS World Geocoder — PRIMARY. (Nominatim returns nothing for some real streets, e.g. Garland Ct, Tampa.)"""
    try:
        r = requests.get("https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates",
                         params={"SingleLine": single_line, "f": "json", "outFields": "*", "maxLocations": 1}, timeout=30)
        cands = r.json().get("candidates", [])
        if not cands: return None
        c = cands[0]; a = c.get("attributes", {})
        return {"lat": c["location"]["y"], "lon": c["location"]["x"], "score": c.get("score"),
                "street": a.get("Address") or a.get("StAddr"), "city": a.get("City"),
                "state": a.get("RegionAbbr") or a.get("Region"), "zip": a.get("Postal"),
                "matched": c.get("address")}
    except Exception:
        return None

def fema_flood(lat, lon):
    """FEMA NFHL flood hazard zones layer (28). Returns (zone, source, date) or Not found."""
    today = datetime.date.today().isoformat()
    url = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
    for _attempt in range(2):  # FEMA service occasionally times out on first hit — retry once
        try:
            r = requests.get(url, params={"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
                                          "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
                                          "outFields": "FLD_ZONE,ZONE_SUBTY", "f": "json"}, timeout=30)
            feats = r.json().get("features", [])
            if feats:
                at = feats[0]["attributes"]
                return (at.get("FLD_ZONE") or "Not found", "FEMA NFHL (hazards.fema.gov)", today)
        except Exception:
            continue
    return ("Not found", "FEMA NFHL (hazards.fema.gov)", today)

def score_breakdown(subj, comp):
    """ReiSearch weights: distance 40, sqft 20, beds 13, baths 13, year built 13.
    Returns weighted points per component (sums to the match score). One score, used everywhere."""
    def closeness(a, b, tol):
        if a is None or b is None: return 0.5
        return max(0.0, 1.0 - abs(a - b) / tol)
    d = comp.get("distance_mi")
    d_score = 0.5 if d is None else max(0.0, 1.0 - d / 2.0)          # 0 mi = 1.0, 2+ mi = 0
    return {
        "Distance (40)": round(40 * d_score, 1),
        "Sqft (20)": round(20 * closeness(comp.get("sqft"), subj.get("sqft"), (subj.get("sqft") or 1500)), 1),
        "Beds (13)": round(13 * closeness(comp.get("beds"), subj.get("beds"), 3), 1),
        "Baths (13)": round(13 * closeness(comp.get("baths"), subj.get("baths"), 2.5), 1),
        "Year built (13)": round(13 * closeness(comp.get("year_built"), subj.get("year_built"), 50), 1),
    }

def match_score(subj, comp):
    return round(sum(score_breakdown(subj, comp).values()), 1)

def adjusted_price(comp):
    adj = (comp["adj_condition"] or 0) + (comp["adj_size"] or 0) + (comp["adj_other"] or 0)
    return (comp["price"] or 0) + adj

def arv_reconciliation(prop, comps):
    """ONE reconciled ARV with the math shown: average of (adjusted avg comp price) and (avg $/sqft x subject sqft)."""
    usable = [c for c in comps if c["kind"] == "sold" and not c["excluded"] and c["price"]]
    pool = [c for c in usable if c["pinned"]] or sorted(usable, key=lambda c: -(c["match_score"] or 0))[:6]
    if not pool or not prop["sqft"]:
        return {"arv": None, "pool": pool, "note": "Not found — pull comps first."}
    adj_prices = [adjusted_price(c) for c in pool]
    avg_price = sum(adj_prices) / len(adj_prices)
    ppsfs = [(adjusted_price(c) / c["sqft"]) for c in pool if c["sqft"]]
    avg_ppsf = sum(ppsfs) / len(ppsfs) if ppsfs else 0
    ppsf_value = avg_ppsf * prop["sqft"]
    arv = round((avg_price + ppsf_value) / 2) if ppsf_value else round(avg_price)
    basis = f"your {len(pool)} pinned comp(s)" if any(c["pinned"] for c in pool) else f"top {len(pool)} comps by match score (pin comps to choose your own)"
    return {"arv": arv, "pool": pool, "avg_price": round(avg_price), "avg_ppsf": round(avg_ppsf, 2),
            "ppsf_value": round(ppsf_value), "count": len(pool),
            "note": f"ARV = average of adjusted comp average (${round(avg_price):,}) and $/sqft method "
                    f"(${round(avg_ppsf,2)}/sqft x {int(prop['sqft'])} sqft = ${round(ppsf_value):,}), "
                    f"over {basis}. Source: HomeHarvest sold data, pulled {datetime.date.today().isoformat()}."}

def verdict(prop, comps, boxes):
    """Sticky verdict: score + confidence + MAO + buy-box PASS/FAIL."""
    rec = arv_reconciliation(prop, comps)
    arv = prop["arv"] or rec["arv"]
    repairs = prop["repairs"] or 0
    price = prop["purchase_price"] or prop["list_price"]
    mao = round(arv * 0.70 - repairs) if arv else None
    score, reasons = None, []
    if arv and price:
        spread_pct = (arv - price - repairs) / arv * 100
        score = max(1, min(99, round(35 + spread_pct * 1.6)))
        reasons.append(f"Spread after repairs = {spread_pct:.1f}% of ARV")
        if mao and price <= mao: score = min(99, score + 8); reasons.append("At/under MAO (70% rule)")
        elif mao: score = max(1, score - 10); reasons.append("Above MAO (70% rule)")
    n_sold = len([c for c in comps if c["kind"] == "sold" and not c["excluded"]])
    completeness = sum(1 for v in [prop["beds"], prop["sqft"], prop["year_built"], prop["list_price"]] if v) / 4
    conf_pts = min(50, n_sold * 6) + completeness * 50
    confidence = "High" if conf_pts >= 75 else ("Medium" if conf_pts >= 45 else "Low")
    bb_result, bb_name = "No buy box set", None
    for b in boxes:
        if not b["active"]: continue
        ok = True
        if b["max_price"] and price and price > b["max_price"]: ok = False
        if b["min_beds"] and prop["beds"] and prop["beds"] < b["min_beds"]: ok = False
        if b["min_sqft"] and prop["sqft"] and prop["sqft"] < b["min_sqft"]: ok = False
        if b["cities"] and prop["city"] and prop["city"].lower() not in b["cities"].lower(): ok = False
        if b["strategies"] and prop["deal_method"] and prop["deal_method"].lower() not in b["strategies"].lower(): ok = False
        if ok: bb_result, bb_name = "PASS", b["name"]; break
        bb_result, bb_name = "FAIL", b["name"]
    color = "gray" if score is None else ("green" if score >= 70 else ("yellow" if score >= 45 else "red"))
    label = "—" if score is None else ("Strong" if score >= 75 else ("Workable" if score >= 55 else ("Thin" if score >= 40 else "Weak")))
    return {"score": score, "label": label, "color": color, "confidence": confidence,
            "arv": arv, "mao": mao, "buybox": bb_result, "buybox_name": bb_name,
            "reasons": reasons, "rec": rec}

def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs: return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

def ltr_stats(rents):
    """Long Term Rental view stats. Header average = average monthly rent of the PINNED rental
    comps when any are pinned, else of the top 3 by match score (ReiSearch behavior)."""
    pool = [c for c in rents if c["price"]]
    if not pool: return None
    pinned = [c for c in pool if c["pinned"]]
    basis = pinned or sorted(pool, key=lambda c: -(c["match_score"] or 0))[:3]
    avg = sum(c["price"] for c in basis) / len(basis)
    return {"avg_rent": round(avg), "basis": "pinned" if pinned else "top3", "basis_n": len(basis),
            "median_rent": round(_median([c["price"] for c in pool]) or 0), "count": len(pool),
            "source": "HomeHarvest rental comps (Realtor.com)", "date": datetime.date.today().isoformat()}

def coliving_stats(rents):
    """Co-Living benchmark derived honestly from whole-house rental comps:
    per-room monthly rent = listing rent / bedrooms; weekly = monthly x 12 / 52.
    Benchmark = median across comps with usable beds+rent. Needs >= 3 comps, else Not found."""
    per_room_wk = []
    for c in rents:
        if c["price"] and c["beds"] and c["beds"] >= 1:
            per_room_wk.append(c["price"] / c["beds"] * 12 / 52)
    if len(per_room_wk) < 3: return None
    wk = _median(per_room_wk)
    return {"weekly_per_room": round(wk), "monthly_per_room": round(wk * 52 / 12), "n": len(per_room_wk),
            "method": "Median of (monthly rent ÷ bedrooms × 12 ÷ 52) across pulled whole-house rental comps — "
                      "an estimate of shared-bath room rent for this ZIP, not PadSplit listing data.",
            "source": "Derived from HomeHarvest rental comps (Realtor.com)", "date": datetime.date.today().isoformat()}

def room_estimate_weekly(comp):
    """Per-room weekly rent estimate for one rental comp (same method as the benchmark)."""
    if comp["price"] and comp["beds"] and comp["beds"] >= 1:
        return round(comp["price"] / comp["beds"] * 12 / 52)
    return None

def pinned_sold_stats(sold):
    """Sold-view header averages from the PINNED set (falls back to top 3 by score, labeled)."""
    pool = [c for c in sold if c["price"]]
    if not pool: return None
    pinned = [c for c in pool if c["pinned"]]
    basis = pinned or sorted(pool, key=lambda c: -(c["match_score"] or 0))[:3]
    doms = [c["dom"] for c in basis if c["dom"]]
    return {"n": len(basis), "basis": "pinned" if pinned else "top3",
            "avg_price": round(sum(c["price"] for c in basis) / len(basis)),
            "avg_ppsf": round(sum(c["ppsf"] for c in basis if c["ppsf"]) / max(1, len([c for c in basis if c["ppsf"]])), 2) if any(c["ppsf"] for c in basis) else None,
            "avg_dom": round(sum(doms) / len(doms)) if doms else None}

def amortization(principal, annual_rate_pct, amort_years, loan_years=None):
    """Annual interest/principal/balance table."""
    loan_years = loan_years or amort_years
    r = (annual_rate_pct or 0) / 100 / 12; n = int(amort_years * 12)
    if principal <= 0: return {"payment": 0, "rows": [], "balloon": 0}
    pmt = principal * r / (1 - (1 + r) ** -n) if r > 0 else principal / n
    bal, rows = float(principal), []
    for yr in range(1, int(loan_years) + 1):
        yi = yp = 0.0
        for _ in range(12):
            if bal <= 0: break
            i = bal * r; p = min(pmt - i, bal); yi += i; yp += p; bal -= p
        rows.append({"year": yr, "interest": round(yi, 2), "principal": round(yp, 2), "balance": round(max(bal, 0), 2)})
        if bal <= 0: break
    return {"payment": round(pmt, 2), "rows": rows, "balloon": round(max(bal, 0), 2)}

# ---------------------------------------------------------------- underwriting
def _lines_total(lines):
    return sum((l.get("qty", 1) or 1) * ((l.get("material", 0) or 0) + (l.get("labor", 0) or 0)) for l in lines or [])

def score_rental(m, penalties):
    """Co-Living weights: CF/room 25, DSCR 20, CoC 20, cap 15, break-even 12, downside CF 8."""
    def cap_scale(x, target): return max(0.0, min(100.0, (x / target) * 100)) if target else 0.0
    parts = {
        "Cash flow / room (25%)": round(0.25 * cap_scale(m.get("cf_per_room", 0), 300), 1),
        "DSCR (20%)": round(0.20 * cap_scale(m.get("dscr", 0), 1.5), 1),
        "Cash-on-cash (20%)": round(0.20 * cap_scale(m.get("coc", 0), 0.25), 1),
        "Cap rate (15%)": round(0.15 * cap_scale(m.get("cap_rate", 0), 0.10), 1),
        "Break-even occupancy (12%)": round(0.12 * max(0.0, min(100.0, (1.3 - m.get("breakeven", 1.3)) / 0.6 * 100)), 1),
        "Downside cash flow (8%)": round(0.08 * (100 if m.get("downside_cf", 0) > 0 else (50 if m.get("downside_cf", 0) == 0 else 10)), 1),
    }
    total = sum(parts.values())
    for p in penalties: total -= p["points"]
    total = max(1, min(99, round(total)))
    label = "Strong" if total >= 75 else ("Workable" if total >= 55 else ("Thin" if total >= 40 else "Weak"))
    return {"score": total, "label": label, "parts": parts, "penalties": penalties}

def compute_underwriting(strategy, inp, prop):
    """Shared shell: post-reno details, rehab lines, appliances, furnishing, funding stack, mortgage, holding."""
    arv = fnum(inp.get("arv"), prop["arv"] if prop else None) or 0
    price = fnum(inp.get("purchase_price"), prop["purchase_price"] or prop["list_price"] if prop else None) or 0
    rehab = _lines_total(inp.get("rehab_lines")) + fnum(inp.get("rehab_flat"), 0) or 0
    appliances = _lines_total(inp.get("appliance_lines"))
    furnishing = 0 if strategy == "Long Term" else _lines_total(inp.get("furnishing_lines"))
    conversion = _lines_total(inp.get("conversion_lines")) if strategy == "Co-Living" else 0
    stack = inp.get("funding_stack") or []
    funded = sum(fnum(s.get("amount"), 0) or 0 for s in stack)
    total_project = price + rehab + appliances + furnishing + conversion
    gap = total_project - funded
    cash_equity = sum(fnum(s.get("amount"), 0) or 0 for s in stack if s.get("type") in ("Cash", "Equity"))
    # Cash/equity stack lines ARE cash invested; any unfunded gap is also cash. (Counting only the
    # gap made an all-cash deal show a near-zero denominator and a fantasy cash-on-cash return.)
    cash_in = cash_equity + max(gap, 0) + (fnum(inp.get("closing_costs"), 0) or 0)
    debt_service_mo = sum((fnum(s.get("amount"), 0) or 0) * ((fnum(s.get("rate"), 0) or 0) / 100) / 12 for s in stack if s.get("type") not in ("Cash", "Equity"))
    # sanity defaults + realism penalties (a $0-expense deal must NOT score Strong)
    penalties, flags = [], []
    taxes_mo = fnum(inp.get("taxes_mo"), 0) or 0
    ins_mo = fnum(inp.get("insurance_mo"), 0) or 0
    if taxes_mo == 0:
        taxes_mo = round((price or arv) * 0.011 / 12) if (price or arv) else 0
        penalties.append({"points": 6, "reason": "Taxes were $0 — sanity default applied (1.1% of price/yr, county-style estimate)"})
        flags.append("Taxes estimated — verify with county")
    if ins_mo == 0:
        ins_mo = 180
        penalties.append({"points": 6, "reason": "Insurance was $0 — sanity default applied ($180/mo estimate)"})
        flags.append("Insurance estimated")
    out = {"strategy": strategy, "arv": arv, "purchase_price": price, "rehab": rehab, "appliances": appliances,
           "furnishing": furnishing, "conversion": conversion, "total_project": round(total_project),
           "funded": round(funded), "gap": round(gap), "cash_in": round(cash_in), "flags": flags}

    if strategy == "Fix & Flip":
        net = arv * (1 - 0.06 - 0.02)
        holding = fnum(inp.get("holding_months"), 6) or 6
        hold_cost = (fnum(inp.get("holding_mo"), 0) or 0) * holding
        profit = net - total_project - hold_cost
        out.update({"net_proceeds": round(net), "holding_cost": round(hold_cost), "profit": round(profit),
                    "roi": round(profit / cash_in * 100, 1) if cash_in else None,
                    "annualized_roi": round(profit / cash_in * 100 * 12 / holding, 1) if cash_in and holding else None,
                    "margin_pct": round(profit / arv * 100, 1) if arv else None,
                    "rule70_mao": round(arv * 0.70 - rehab),
                    "score": None, "label": "Exit math only (no deal score — same as ReiSearch Fix & Flip)"})
        return out

    # rental strategies
    vacancy = (fnum(inp.get("vacancy_pct"), 5) or 0) / 100
    other_income = sum(fnum(inp.get(k), 0) or 0 for k in ("pet_mo", "parking_mo", "storage_mo", "laundry_mo", "misc_mo"))
    if strategy == "Co-Living":
        rooms = fnum(inp.get("rooms"), prop["beds"] if prop else 4) or 4
        rent_room = fnum(inp.get("rent_per_room"), 0) or 0
        gross = rooms * rent_room
        booking = gross * 0.0  # booking fee modeled in days below
        booking_fee = (rent_room * rooms) * ((fnum(inp.get("booking_fee_days"), 0) or 0) / 30) / 12
        service = gross * ((fnum(inp.get("service_fee_pct"), 0) or 0) / 100)
        fees = booking_fee + service
    elif strategy == "Short Term":
        units = fnum(inp.get("units"), 1) or 1
        gross = (fnum(inp.get("nightly_rate"), 0) or 0) * 30 * ((fnum(inp.get("occupancy_pct"), 0) or 0) / 100) * units
        stays = max(1.0, 30 / (fnum(inp.get("avg_stay_nights"), 3) or 3)) * units
        other_income += stays * (fnum(inp.get("cleaning_fee"), 0) or 0)
        fees = gross * ((fnum(inp.get("platform_pct"), 0) or 0) / 100)
        rooms = units
    else:  # Mid Term / Long Term
        units = fnum(inp.get("units"), 1) or 1
        gross = (fnum(inp.get("rent_per_unit"), 0) or 0) * units
        fees = 0; rooms = units
    egi = gross * (1 - vacancy) + other_income - fees
    opex = taxes_mo + ins_mo + (fnum(inp.get("utilities_mo"), 0) or 0) + (fnum(inp.get("maintenance_mo"), 0) or 0) \
        + (fnum(inp.get("management_mo"), 0) or 0) + (fnum(inp.get("hoa_mo"), (prop["hoa_dues"] if prop and prop["hoa_freq"] == "Monthly" else 0) or 0))
    noi = egi - opex
    cf = noi - debt_service_mo
    value = arv or price
    metrics = {"cf_per_room": (cf / rooms) if rooms else 0, "dscr": (noi / debt_service_mo) if debt_service_mo else (1.5 if noi > 0 else 0),
               "coc": (cf * 12 / cash_in) if cash_in else 0, "cap_rate": (noi * 12 / value) if value else 0,
               "breakeven": ((opex + debt_service_mo) / (gross + other_income)) if (gross + other_income) else 1.3,
               "downside_cf": (egi * 0.9 - opex - debt_service_mo)}
    if opex <= taxes_mo + ins_mo and (fnum(inp.get("utilities_mo"), 0) or 0) == 0 and (fnum(inp.get("maintenance_mo"), 0) or 0) == 0:
        penalties.append({"points": 10, "reason": "No utilities/maintenance entered — expenses look unrealistically low"})
    scen = {}
    for name, mult in (("Downside (-10% revenue)", 0.9), ("Base", 1.0), ("Upside (+10% revenue)", 1.1), ("Fully booked (0% vacancy)", None)):
        g2 = gross if mult is not None else gross
        e2 = (g2 * mult * (1 - vacancy) if mult else g2) + other_income - fees
        scen[name] = round(e2 - opex - debt_service_mo)
    sc = score_rental(metrics, penalties) if strategy == "Co-Living" else None
    if strategy != "Co-Living":
        sc = score_rental(metrics, penalties)  # improvement over ReiSearch: every strategy scored
    out.update({"gross_mo": round(gross), "fees_mo": round(fees), "other_income_mo": round(other_income),
                "egi_mo": round(egi), "opex_mo": round(opex), "taxes_mo": taxes_mo, "insurance_mo": ins_mo,
                "noi_mo": round(noi), "debt_service_mo": round(debt_service_mo), "cashflow_mo": round(cf),
                "cf_per_room": round(metrics["cf_per_room"]), "dscr": round(metrics["dscr"], 2),
                "coc_pct": round(metrics["coc"] * 100, 1), "cap_rate_pct": round(metrics["cap_rate"] * 100, 2),
                "breakeven_pct": round(metrics["breakeven"] * 100, 1), "scenarios": scen,
                "score": sc["score"], "label": sc["label"], "score_parts": sc["parts"], "penalties": sc["penalties"]})
    return out

# ---------------------------------------------------------------- comps pull (HomeHarvest)
def pull_comps(prop, radius_mi=1.0, past_days=365):
    """ZIP-mode query + post-filter on ZIP and coordinates. NEVER raw full-address mode (mis-resolves).
    Pulls sold comps + rental comps, the subject's own listing photos, fills blank subject
    facts from its listing row, and auto-pins the top 3 sold comps (ReiSearch default) when
    the user has not pinned any yet. Returns counts per kind. Raises on total failure so the
    job layer can surface the reason — partial failures are reported in 'warnings'."""
    from homeharvest import scrape_property
    d = db(); today = datetime.date.today().isoformat()
    warnings = []
    sold = scrape_property(location=prop["zip"], listing_type="sold", past_days=past_days)
    added = updated = 0; n_sold = 0
    subj = {"sqft": prop["sqft"], "beds": prop["beds"], "baths": prop["baths"], "year_built": prop["year_built"]}
    for _, r in sold.iterrows():
        if str(r.get("zip_code")) != str(prop["zip"]): continue
        lat, lon = r.get("latitude"), r.get("longitude")
        if lat is None or lon is None or (isinstance(lat, float) and math.isnan(lat)): continue
        dist = haversine_mi(prop["lat"], prop["lon"], float(lat), float(lon))
        if dist is None or dist > radius_mi: continue
        addr = _s(r.get("street")).strip()
        if not addr: continue
        price = fnum(r.get("sold_price")); sqft = fnum(r.get("sqft"))
        if not price: continue  # a "sold" row with no price is not a usable comp
        n_sold += 1
        vals = dict(kind="sold", address=addr, city=_s(r.get("city")) or prop["city"],
                    zip=_s(r.get("zip_code")), price=price,
                    ppsf=(price / sqft) if price and sqft else fnum(r.get("price_per_sqft")),
                    beds=fnum(r.get("beds")), baths=fnum(r.get("full_baths")), sqft=sqft,
                    year_built=int(r["year_built"]) if fnum(r.get("year_built")) else None,
                    event_date=_s(r.get("last_sold_date"))[:10] or None,
                    dom=fnum(r.get("days_on_mls")), lat=float(lat), lon=float(lon), distance_mi=dist,
                    source="HomeHarvest (Realtor.com)", source_url=_s(r.get("property_url")),
                    photo_url=_s(r.get("primary_photo")))
        vals["match_score"] = match_score(subj, {"distance_mi": dist, "sqft": sqft, "beds": vals["beds"],
                                                 "baths": vals["baths"], "year_built": vals["year_built"]})
        ex = d.execute("SELECT id FROM comps WHERE property_id=? AND kind='sold' AND address=? AND price=?",
                       (prop["id"], addr, price)).fetchone()
        cols = ",".join(vals.keys()); ph = ",".join("?" * len(vals))
        if ex:
            d.execute(f"UPDATE comps SET {','.join(k+'=?' for k in vals)} WHERE id=?", (*vals.values(), ex["id"])); updated += 1
        else:
            d.execute(f"INSERT INTO comps(property_id,{cols}) VALUES(?,{ph})", (prop["id"], *vals.values())); added += 1
    # rentals (LTR / Co-Living / Affordable views all read this set)
    n_rent = 0; rent_df = None
    try:
        rent_df = scrape_property(location=prop["zip"], listing_type="for_rent", past_days=90)
        for _, r in rent_df.iterrows():
            if str(r.get("zip_code")) != str(prop["zip"]): continue
            lat, lon = r.get("latitude"), r.get("longitude")
            if lat is None or lon is None or (isinstance(lat, float) and math.isnan(lat)): continue
            dist = haversine_mi(prop["lat"], prop["lon"], float(lat), float(lon))
            if dist is None or dist > radius_mi: continue
            addr = _s(r.get("street")).strip(); price = fnum(r.get("list_price"))
            if not addr or not price: continue
            sqft = fnum(r.get("sqft"))
            vals = dict(kind="rent", address=addr, city=_s(r.get("city")) or prop["city"],
                        zip=_s(r.get("zip_code")), price=price,
                        ppsf=(price / sqft) if sqft else None,
                        beds=fnum(r.get("beds")), baths=fnum(r.get("full_baths")), sqft=sqft,
                        year_built=int(r["year_built"]) if fnum(r.get("year_built")) else None,
                        event_date=None, dom=fnum(r.get("days_on_mls")),
                        lat=float(lat), lon=float(lon), distance_mi=dist,
                        source="HomeHarvest (Realtor.com rentals)", source_url=_s(r.get("property_url")),
                        photo_url=_s(r.get("primary_photo")))
            vals["match_score"] = match_score(subj, {"distance_mi": dist, "sqft": sqft, "beds": vals["beds"],
                                                     "baths": vals["baths"], "year_built": vals["year_built"]})
            ex = d.execute("SELECT id FROM comps WHERE property_id=? AND kind='rent' AND address=? AND price=?",
                           (prop["id"], addr, price)).fetchone()
            cols = ",".join(vals.keys()); ph = ",".join("?" * len(vals))
            if ex:
                d.execute(f"UPDATE comps SET {','.join(k+'=?' for k in vals)} WHERE id=?", (*vals.values(), ex["id"]))
            else:
                d.execute(f"INSERT INTO comps(property_id,{cols}) VALUES(?,{ph})", (prop["id"], *vals.values())); added += 1
            n_rent += 1
    except Exception as e:
        warnings.append(f"rental comps failed ({e})")
        app.logger.warning(f"rent pull failed: {e}")
    d.commit()
    # --- subject's own listing: photos + blank-fact fill (never overwrite user data) ---
    photos_found = 0
    try:
        fs = scrape_property(location=prop["zip"], listing_type="for_sale", past_days=365)
    except Exception as e:
        fs = None; warnings.append(f"for-sale lookup for subject photos failed ({e})")
    srow = find_subject_row(fs, prop)
    if srow is None:
        srow = find_subject_row(sold, prop)
    if srow is None:
        srow = find_subject_row(rent_df, prop)
    if srow is not None:
        photos = extract_photos(srow)
        fills = {}
        if photos:
            fills["photos_json"] = json.dumps(photos); fills["photos_date"] = today
            fills["photo_url"] = photos[0]; photos_found = len(photos)
        for col, key in (("beds", "beds"), ("baths", "full_baths"), ("sqft", "sqft"),
                         ("year_built", "year_built"), ("lot_sqft", "lot_sqft"), ("property_type", "style")):
            if not prop[col]:
                v = fnum(srow.get(key))
                if v: fills[col] = int(v) if col == "year_built" else v
                elif key == "style" and _s(srow.get("style")): fills[col] = _s(srow.get("style"))
        if not prop["list_price"] and fnum(srow.get("list_price")):
            fills["list_price"] = fnum(srow.get("list_price"))
        if not prop["est_realtor"] and fnum(srow.get("estimated_value")):
            fills["est_realtor"] = fnum(srow.get("estimated_value")); fills["est_date"] = today
        if fills:
            d.execute(f"UPDATE properties SET {','.join(k+'=?' for k in fills)} WHERE id=?",
                      (*fills.values(), prop["id"])); d.commit()
    # --- ReiSearch default: top 3 sold by match score auto-pinned (only if user pinned none) ---
    pinned_now = d.execute("SELECT COUNT(*) c FROM comps WHERE property_id=? AND kind='sold' AND pinned=1",
                           (prop["id"],)).fetchone()["c"]
    if not pinned_now:
        top3 = d.execute("""SELECT id FROM comps WHERE property_id=? AND kind='sold' AND excluded=0
                            ORDER BY match_score DESC LIMIT 3""", (prop["id"],)).fetchall()
        for t in top3: d.execute("UPDATE comps SET pinned=1 WHERE id=?", (t["id"],))
        d.commit()
    total_sold = d.execute("SELECT COUNT(*) c FROM comps WHERE property_id=? AND kind='sold' AND excluded=0",
                           (prop["id"],)).fetchone()["c"]
    total_rent = d.execute("SELECT COUNT(*) c FROM comps WHERE property_id=? AND kind='rent'",
                           (prop["id"],)).fetchone()["c"]
    return {"added": added, "updated": updated, "sold": total_sold, "rentals": total_rent,
            "sold_new": n_sold, "photos": photos_found, "warnings": warnings, "date": today}

def census_demographics(zip_code, city=None, state_fips="12"):
    """Keyless Census ACS 5-year. ZIP (ZCTA) + optional city (place) basics."""
    key = get_setting("census_key"); out = {"zip": None, "city": None, "source": "US Census ACS 2023 5-year (api.census.gov)", "date": datetime.date.today().isoformat()}
    vars_ = "NAME,B01003_001E,B25077_001E,B25064_001E,B19013_001E,B25003_002E,B25003_003E,B23025_005E,B23025_003E"
    def fetch(geo_params):
        p = {"get": vars_, "for": geo_params}
        if key: p["key"] = key
        r = requests.get("https://api.census.gov/data/2023/acs/acs5", params=p, timeout=8)
        rows = r.json()
        if len(rows) < 2: return None
        h, v = rows[0], rows[1]; d = dict(zip(h, v))
        def n(k):
            x = fnum(d.get(k)); return x if x is not None and x > 0 else None
        own, rent = n("B25003_002E") or 0, n("B25003_003E") or 0
        un, lf = n("B23025_005E"), n("B23025_003E")
        return {"name": d.get("NAME"), "population": n("B01003_001E"), "median_home_value": n("B25077_001E"),
                "median_rent": n("B25064_001E"), "median_income": n("B19013_001E"),
                "owner_pct": round(own / (own + rent) * 100, 1) if (own + rent) else None,
                "renter_pct": round(rent / (own + rent) * 100, 1) if (own + rent) else None,
                "unemployment_pct": round(un / lf * 100, 2) if un and lf else None}
    try: out["zip"] = fetch(f"zip code tabulation area:{zip_code}")
    except Exception: out["zip"] = None
    # Tampa place FIPS 1271000; generic: only attempt known FL places map for v1
    PLACES = {"tampa": "71000", "winter springs": "78025", "kissimmee": "36975", "orlando": "53000", "jacksonville": "35000"}
    if city and city.lower() in PLACES:
        try: out["city"] = fetch(f"place:{PLACES[city.lower()]}&in=state:{state_fips}")
        except Exception: out["city"] = None
    return out

# ---------------------------------------------------------------- OSM amenities (v1.1)
AMENITY_ORDER = ["Schools", "Groceries & Supermarkets", "Hospitals & Clinics", "Pharmacies", "Parks", "Transit Stops"]

def pull_amenities(prop, radius_m=1609):
    """Live OpenStreetMap Overpass pull (~1 mi) around the property's stored coordinates.
    Returns a cacheable dict; on failure returns {"error": ...} — never fake data."""
    today = datetime.date.today().isoformat()
    src = "OpenStreetMap (Overpass API)"
    lat, lon = prop["lat"], prop["lon"]
    if lat is None or lon is None:
        return {"error": "Not found — property has no stored coordinates (geocode it first).", "date": today, "source": src}
    q = f"""[out:json][timeout:120];
(
  nwr["amenity"="school"](around:{radius_m},{lat},{lon});
  nwr["shop"~"^(supermarket|grocery)$"](around:{radius_m},{lat},{lon});
  nwr["amenity"~"^(hospital|clinic)$"](around:{radius_m},{lat},{lon});
  nwr["amenity"="pharmacy"](around:{radius_m},{lat},{lon});
  nwr["leisure"="park"](around:{radius_m},{lat},{lon});
  nwr["highway"="bus_stop"](around:{radius_m},{lat},{lon});
  nwr["railway"="station"](around:{radius_m},{lat},{lon});
  nwr["public_transport"="station"](around:{radius_m},{lat},{lon});
);
out center tags;"""
    try:
        # GET, not POST: POST to Overpass stalls behind some proxies/firewalls; GET is equally supported.
        r = requests.get("https://overpass-api.de/api/interpreter", params={"data": q},
                         headers={"User-Agent": "HouseAble-DealAnalyzer/1.1"}, timeout=150)
        r.raise_for_status()
        elements = r.json().get("elements", [])
    except Exception as e:
        return {"error": f"Not found — OpenStreetMap Overpass pull failed ({e}). Use Refresh to retry.",
                "date": today, "source": src}
    def cat_of(t):
        if t.get("amenity") == "school": return "Schools"
        if t.get("shop") in ("supermarket", "grocery"): return "Groceries & Supermarkets"
        if t.get("amenity") in ("hospital", "clinic") or t.get("healthcare") in ("hospital", "clinic"): return "Hospitals & Clinics"
        if t.get("amenity") == "pharmacy": return "Pharmacies"
        if t.get("leisure") == "park": return "Parks"
        if t.get("highway") == "bus_stop" or t.get("railway") == "station" or t.get("public_transport") == "station": return "Transit Stops"
        return None
    found = {}
    for el in elements:
        t = el.get("tags") or {}
        cat = cat_of(t)
        if not cat: continue
        elat = el.get("lat") or (el.get("center") or {}).get("lat")
        elon = el.get("lon") or (el.get("center") or {}).get("lon")
        if elat is None or elon is None: continue
        dist = haversine_mi(lat, lon, elat, elon)
        if dist is None: continue
        name = (t.get("name") or t.get("brand") or "").strip()
        if not name:
            if cat == "Transit Stops": name = f"Bus stop {t['ref']}" if t.get("ref") else "Bus stop (unnamed)"
            elif cat == "Parks": name = "Park (unnamed)"
            else: continue  # unnamed school/store/clinic = not useful, skip honestly
        key = (cat, name.lower(), round(dist, 2))
        if key not in found or dist < found[key]["dist"]:
            found[key] = {"name": name, "dist": dist}
    groups, total = {}, 0
    for cat in AMENITY_ORDER:
        items = sorted((v for k, v in found.items() if k[0] == cat), key=lambda x: x["dist"])[:8]
        groups[cat] = items; total += len(items)
    return {"groups": groups, "total": total, "radius_mi": round(radius_m / 1609.34, 2),
            "source": src, "date": today}

# ---------------------------------------------------------------- HouseAble contractor import (v1.1)
PROSPECTS_PATH = os.environ.get("PDA_PROSPECTS", os.path.expanduser("~/workspace/contractor-recruiting/contractor-prospects.json"))
_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w\-]+\.[\w.]+")

def _norm_phone(raw):
    digs = re.sub(r"\D", "", raw or "")
    if len(digs) == 11 and digs.startswith("1"): digs = digs[1:]
    return digs

def _fmt_phone(digs):
    return f"{digs[0:3]}-{digs[3:6]}-{digs[6:10]}" if len(digs) == 10 else (digs or "")

def _infer_trade(p):
    cat = (p.get("category") or "").lower()
    text = ((p.get("business_name") or "") + " " + " ".join(p.get("specialized_services") or [])).lower()
    if "tier-1-gc" in cat: return "General Contractor"
    if "tier-3-handyman" in cat: return "Handyman"
    for kw, trade in (("roof", "Roofing"), ("paint", "Painting"), ("excavat", "Excavation"),
                      ("granite", "Countertops / Stone"), ("stone", "Countertops / Stone"),
                      ("drain", "Plumbing / Drainage"), ("plumb", "Plumbing"), ("tile", "Tile"),
                      ("drywall", "Drywall"), ("floor", "Flooring"), ("remodel", "Remodeling"),
                      ("construction", "General Contractor"), ("home improvement", "Remodeling")):
        if kw in text: return trade
    if "tier-4-crew" in cat: return "Remodeling Crew"
    if "tier-2-trade" in cat: return "Trade Contractor"
    return "General Contractor"

def import_houseable_prospects(path=None):
    """Import Sherif's recruited contractor prospects into the contractors table.
    Own SQLite connection (works from web route and CLI). Idempotent: dedupe by name+phone."""
    path = path or PROSPECTS_PATH
    with open(path) as f:
        data = json.load(f)
    prospects = data.get("prospects", []) if isinstance(data, dict) else data
    src_date = data.get("last_updated", "") if isinstance(data, dict) else ""
    d = sqlite3.connect(DB_PATH); d.row_factory = sqlite3.Row
    seen = set()
    for r in d.execute("SELECT name, company, phone FROM contractors"):
        ph = _norm_phone(r["phone"])
        seen.add(((r["name"] or "").strip().lower(), ph))
        seen.add(((r["company"] or "").strip().lower(), ph))
    imported = dupes = noname = 0
    for p in prospects:
        business = (p.get("business_name") or "").strip()
        contact = (p.get("contact_name") or "").strip()
        name = contact or business
        if not name and not business:
            noname += 1; continue
        contact_info = p.get("public_contact_info") or ""
        m = _PHONE_RE.search(contact_info)
        digits = _norm_phone(m.group(0)) if m else ""
        phone = _fmt_phone(digits)
        key = (name.strip().lower(), digits)
        if key in seen or (business.strip().lower(), digits) in seen:
            dupes += 1; continue
        em = _EMAIL_RE.search(contact_info)
        area = ", ".join(p.get("service_area") or []) or (p.get("location") or "")
        notes = []
        if p.get("score") is not None:
            notes.append(f"HouseAble recruiting score: {p['score']}/100" + (f" ({p['classification']})" if p.get("classification") else ""))
        if p.get("category"): notes.append(f"Category: {p['category']}")
        if area: notes.append(f"Service area: {area}")
        if p.get("specialized_services"): notes.append("Services: " + ", ".join(p["specialized_services"]))
        if p.get("licensing"): notes.append("Licensing: " + str(p["licensing"])[:300])
        if p.get("recommended_next_step"): notes.append("Next step: " + str(p["recommended_next_step"])[:200])
        if p.get("website"): notes.append("Website: " + str(p["website"]))
        notes.append(f"Imported from HouseAble contractor-prospects.json (DB updated {src_date}). Recruiting prospect — verify license/insurance before engaging.")
        d.execute("INSERT INTO contractors(name,company,trade,phone,email,notes) VALUES(?,?,?,?,?,?)",
                  (name, business, _infer_trade(p), phone, em.group(0) if em else "", " · ".join(notes)))
        seen.add(key); seen.add((business.strip().lower(), digits)); imported += 1
    d.commit(); d.close()
    return {"imported": imported, "duplicates_skipped": dupes, "skipped_no_name": noname, "prospects_in_file": len(prospects)}

# ---------------------------------------------------------------- async jobs (v1.2: never silent)
def _job_conn():
    c = sqlite3.connect(DB_PATH, timeout=30); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); return c

def start_job(pid, kind, fn):
    """Record a job row and run fn in a background thread. fn returns a success message string.
    Any exception becomes a visible, plain-English job error on the property — never a bare 500."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c = _job_conn()
    cur = c.execute("INSERT INTO jobs(property_id,kind,status,started_at) VALUES(?,?,'running',?)", (pid, kind, now))
    jid = cur.lastrowid; c.commit(); c.close()
    def _run():
        try:
            with app.app_context():
                msg = fn()
            c2 = _job_conn()
            c2.execute("UPDATE jobs SET status='done', finished_at=?, message=? WHERE id=?",
                       (datetime.datetime.now().isoformat(timespec="seconds"), msg, jid)); c2.commit(); c2.close()
        except Exception as e:
            app.logger.exception(f"job {jid} ({kind}) failed")
            c2 = _job_conn()
            c2.execute("UPDATE jobs SET status='error', finished_at=?, message=? WHERE id=?",
                       (datetime.datetime.now().isoformat(timespec="seconds"),
                        _plain_error(kind, e), jid)); c2.commit(); c2.close()
    threading.Thread(target=_run, daemon=True).start()
    return jid

def _plain_error(kind, e):
    s = str(e) or e.__class__.__name__
    low = s.lower()
    if any(k in low for k in ("403", "forbidden", "blocked", "captcha")):
        return f"Data source blocked the request (Realtor.com/HomeHarvest: {s[:140]}) — wait a minute and try again."
    if any(k in low for k in ("timeout", "timed out", "connection", "unreachable", "name resolution", "max retries")):
        return f"Couldn't reach the data source — check the internet connection and try again. ({s[:140]})"
    return f"{kind.replace('_', ' ').title()} failed: {s[:200]}"

def latest_job(pid, kind=None):
    q = "SELECT * FROM jobs WHERE property_id=?"; args = [pid]
    if kind: q += " AND kind=?"; args.append(kind)
    row = db().execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
    if row and row["status"] == "running" and row["started_at"]:
        try:
            started = datetime.datetime.fromisoformat(row["started_at"])
            if (datetime.datetime.now() - started).total_seconds() > 600:
                db().execute("UPDATE jobs SET status='error', finished_at=?, message=? WHERE id=?",
                             (datetime.datetime.now().isoformat(timespec="seconds"),
                              "This took too long (over 10 minutes) and was stopped — the data source may be "
                              "slow or unreachable. Please try again.", row["id"]))
                db().commit()
                row = db().execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        except Exception:
            pass
    return row

def job_comps(pid, radius, past_days):
    def fn():
        p = get_prop(pid)
        res = pull_comps(p, radius_mi=radius, past_days=past_days)
        msg = f"✓ {res['sold']} sold comps + {res['rentals']} rental comps found (radius {radius:g} mi)"
        if res["photos"]: msg += f" · {res['photos']} listing photos found for this property"
        if res["warnings"]: msg += " · ⚠ " + "; ".join(res["warnings"])
        return msg
    return start_job(pid, "comps", fn)

def job_amenities(pid):
    def fn():
        p = get_prop(pid)
        res = pull_amenities(p)
        db().execute("UPDATE properties SET amenities_json=?, amenities_date=? WHERE id=?",
                     (json.dumps(res), res.get("date"), pid)); db().commit()
        if res.get("error"): raise RuntimeError(res["error"])
        return f"✓ {res['total']} nearby places found within {res['radius_mi']} mi (OpenStreetMap)"
    return start_job(pid, "amenities", fn)

def job_demographics(pid):
    def fn():
        p = get_prop(pid)
        demo = census_demographics(p["zip"], p["city"])
        db().execute("UPDATE properties SET demo_json=?, demo_date=? WHERE id=?",
                     (json.dumps(demo), demo.get("date"), pid)); db().commit()
        if not demo.get("zip") and not demo.get("city"):
            raise RuntimeError("Census API didn't return data for this ZIP — it may be unreachable right now; try again shortly.")
        return f"✓ Demographics loaded for ZIP {p['zip']} (US Census ACS)"
    return start_job(pid, "demographics", fn)

def job_monitor_check(mid, pid):
    def fn():
        return _monitor_check_now(mid)
    return start_job(pid, "monitor", fn)

# ---------------------------------------------------------------- routes
def get_prop(pid):
    p = db().execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
    if not p: abort(404)
    return p

def all_boxes():
    return db().execute("SELECT * FROM buy_boxes ORDER BY id").fetchall()

EDITABLE = ["status","stage","deal_method","list_price","property_type","beds","baths","sqft","lot_sqft",
 "year_built","apn","hoa_dues","hoa_freq","subdivision","legal_desc","sewer","water","electric","appliances",
 "flooring","cooling","heating","roof_age","foundation","occupancy_now","occupancy_closing","as_is_value","arv",
 "repairs","purchase_price","inspection_days","inspection_unit","inspection_deadline","buy_it_now","min_offer",
 "wholesale_contract_amt","emd_ab","emd_bc","title_company","escrow_officer","closing_date","tc_name",
 "offers_deadline","loan_balance","loan_type","loan_rate","loan_piti","dispo_asking","dispo_bin","dispo_min",
 "dispo_inspection_days","offer_review","est_zillow","est_realtor","est_redfin","est_county","notes",
 "skiptrace_notes","skiptrace_status","tax_market","tax_assessed","tax_taxable","tax_amount","tax_exemptions",
 "tax_millage","tax_year","sales_history"]

@app.route("/")
def dashboard():
    props = db().execute("SELECT * FROM properties ORDER BY id DESC LIMIT 12").fetchall()
    counts = {r["status"]: r["c"] for r in db().execute("SELECT status, COUNT(*) c FROM properties GROUP BY status")}
    alerts = db().execute("""SELECT m.*, p.address FROM monitors m JOIN properties p ON p.id=m.property_id
                             WHERE m.last_note IS NOT NULL ORDER BY m.last_checked DESC LIMIT 8""").fetchall()
    recent = db().execute("""SELECT s.*, p.address FROM scenarios s JOIN properties p ON p.id=s.property_id
                             ORDER BY s.id DESC LIMIT 6""").fetchall()
    return render_template("dashboard.html", props=props, counts=counts, alerts=alerts, recent=recent)

@app.route("/properties")
def properties():
    q = "SELECT * FROM properties WHERE 1=1"; args = []
    if request.args.get("status"): q += " AND status=?"; args.append(request.args["status"])
    if request.args.get("method"): q += " AND deal_method=?"; args.append(request.args["method"])
    rows = db().execute(q + " ORDER BY id DESC", args).fetchall()
    view = request.args.get("view", "list")
    return render_template("properties.html", props=rows, view=view)

@app.route("/add", methods=["GET", "POST"])
def add_property():
    if request.method == "POST":
        addr = request.form.get("address", "").strip()
        method = request.form.get("deal_method", "Cash")
        if not addr: flash("Enter an address first."); return redirect(url_for("add_property"))
        geo = geocode_arcgis(addr)
        if not geo:
            flash("Not found — ArcGIS could not geocode that address. Check spelling/ZIP.")
            return redirect(url_for("add_property"))
        flood_zone, flood_src, flood_date = fema_flood(geo["lat"], geo["lon"])
        street = geo["street"] or addr.split(",")[0]
        full = f"{street}, {geo['city']}, {geo['state']} {geo['zip']}".strip(", ")
        cur = db().execute("""INSERT INTO properties(address,street,city,state,zip,lat,lon,flood_zone,flood_source,
            flood_date,deal_method,status) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'Draft')""",
            (full, street, geo["city"], geo["state"], geo["zip"], geo["lat"], geo["lon"], flood_zone, flood_src, flood_date, method))
        db().commit(); pid = cur.lastrowid; seed_docs(pid)
        flash(f"Geocoded (ArcGIS score {geo['score']}) — flood zone: {flood_zone}. Now fill details and pull comps.")
        return redirect(url_for("property_page", pid=pid))
    return render_template("add.html")

@app.route("/property/<int:pid>", methods=["GET", "POST"])
def property_page(pid):
    p = get_prop(pid); d = db()
    if request.method == "POST":
        sets, args = [], []
        for f in EDITABLE:
            if f in request.form:
                v = request.form.get(f)
                sets.append(f"{f}=?"); args.append(v if v != "" else None)
        if sets:
            args.append(pid); d.execute(f"UPDATE properties SET {','.join(sets)} WHERE id=?", args); d.commit()
            p = get_prop(pid); flash("Saved.")
    comps = d.execute("SELECT * FROM comps WHERE property_id=? ORDER BY excluded, pinned DESC, match_score DESC", (pid,)).fetchall()
    liens = d.execute("SELECT * FROM liens WHERE property_id=?", (pid,)).fetchall()
    docs = d.execute("SELECT * FROM documents WHERE property_id=? ORDER BY category, slot", (pid,)).fetchall()
    assigns = d.execute("""SELECT a.*, c.name, c.company FROM assignments a LEFT JOIN contractors c
                           ON c.id=a.contractor_id WHERE a.property_id=?""", (pid,)).fetchall()
    contractors = d.execute("SELECT * FROM contractors ORDER BY trade, name").fetchall()
    scenarios = d.execute("SELECT * FROM scenarios WHERE property_id=? ORDER BY id DESC", (pid,)).fetchall()
    plans = d.execute("SELECT * FROM floorplans WHERE property_id=? ORDER BY id DESC", (pid,)).fetchall()
    monitors = d.execute("SELECT * FROM monitors WHERE property_id=?", (pid,)).fetchall()
    v = verdict(p, comps, all_boxes())
    sold = [c for c in comps if c["kind"] == "sold" and not c["excluded"]]
    rents = [c for c in comps if c["kind"] == "rent"]
    market = None
    if sold:
        prices = sorted(c["price"] for c in sold if c["price"]); ppsfs = sorted(c["ppsf"] for c in sold if c["ppsf"])
        doms = sorted(c["dom"] for c in sold if c["dom"])
        med = lambda xs: xs[len(xs)//2] if xs else None
        by_month = {}
        for c in sold:
            if c["event_date"] and c["price"]: by_month.setdefault(c["event_date"][:7], []).append(c["price"])
        trend = [{"month": m, "median": sorted(vv)[len(vv)//2], "count": len(vv)} for m, vv in sorted(by_month.items())]
        market = {"count": len(sold), "median_price": med(prices), "median_ppsf": med(ppsfs), "median_dom": med(doms),
                  "trend": trend, "source": "Computed from stored HomeHarvest sold comps", "date": datetime.date.today().isoformat()}
    # demographics: served from cache (async job fills it); never block page load on Census
    demo = None
    if p["demo_json"]:
        try: demo = json.loads(p["demo_json"])
        except Exception: demo = None
    tab = request.args.get("tab", "overview")
    amort = None
    amenities = None
    if tab == "demographics" and p["amenities_json"]:
        try: amenities = json.loads(p["amenities_json"])
        except Exception: amenities = None
    # ---- photos gallery ----
    photos = []
    if p["photos_json"]:
        try: photos = json.loads(p["photos_json"])
        except Exception: photos = []
    if not photos and p["photo_url"]: photos = [p["photo_url"]]
    # ---- comparables views (v1.2) ----
    cview = request.args.get("view", "sold")
    if cview not in ("sold", "coliving", "ltr", "str", "affordable"): cview = "sold"
    window = int(fnum(request.args.get("window"), 365) or 365)
    if window not in (30, 90, 180, 365): window = 365
    rmax = fnum(request.args.get("rmax"), None)
    subj_d = {"sqft": p["sqft"], "beds": p["beds"], "baths": p["baths"], "year_built": p["year_built"]}
    def in_radius(c): return rmax is None or (c["distance_mi"] is not None and c["distance_mi"] <= rmax)
    cutoff = (datetime.date.today() - datetime.timedelta(days=window)).isoformat()
    sold_view = [c for c in sold if in_radius(c) and (not c["event_date"] or c["event_date"] >= cutoff)]
    rents_view = [c for c in rents if in_radius(c)]
    sold_stats = pinned_sold_stats(sold_view)
    ltr = ltr_stats(rents_view)
    coli = coliving_stats(rents_view)
    med_rent = ltr["median_rent"] if ltr else None
    afford_view = [c for c in rents_view if med_rent and c["price"] and c["price"] < med_rent]
    breakdowns = {}
    for c in sold_view + rents_view:
        breakdowns[c["id"]] = score_breakdown(subj_d, {"distance_mi": c["distance_mi"], "sqft": c["sqft"],
                                                       "beds": c["beds"], "baths": c["baths"], "year_built": c["year_built"]})
    room_est = {c["id"]: room_estimate_weekly(c) for c in rents_view}
    steps = {"comps": len(sold) > 0, "scenarios": len(scenarios) > 0}
    return render_template("property.html", p=p, comps=comps, sold=sold, rents=rents, liens=liens, docs=docs,
                           assigns=assigns, contractors=contractors, scenarios=scenarios, plans=plans,
                           monitors=monitors, v=v, market=market, demo=demo, tab=tab, amort=amort, amenities=amenities,
                           photos=photos, job=latest_job(pid), steps=steps,
                           cview=cview, window=window, rmax=rmax, sold_view=sold_view, rents_view=rents_view,
                           sold_stats=sold_stats, ltr=ltr, coli=coli, med_rent=med_rent, afford_view=afford_view,
                           breakdowns=breakdowns, room_est=room_est,
                           strategies=["Fix & Flip", "Co-Living", "Short Term", "Mid Term", "Long Term"])

@app.route("/property/<int:pid>/amenities/refresh", methods=["POST"])
def refresh_amenities(pid):
    get_prop(pid)
    job_amenities(pid)
    flash("Refreshing nearby places from OpenStreetMap — this takes ~30 seconds; the result will appear here.", "info")
    return redirect(url_for("property_page", pid=pid, tab="demographics"))

@app.route("/property/<int:pid>/demographics/pull", methods=["POST"])
def pull_demographics(pid):
    get_prop(pid)
    job_demographics(pid)
    flash("Loading demographics from the US Census — the result will appear here in a moment.", "info")
    return redirect(url_for("property_page", pid=pid, tab="demographics"))

@app.route("/property/<int:pid>/pull-comps", methods=["POST"])
def do_pull_comps(pid):
    get_prop(pid)
    radius = fnum(request.form.get("radius_custom"), None) or fnum(request.form.get("radius"), 1.0) or 1.0
    days = int(fnum(request.form.get("window_days"), 365) or 365)
    days = days if days in (30, 90, 180, 365) else 365
    job_comps(pid, radius, days)
    flash(f"Pulling comps — sold (last {days} days) + rentals + listing photos within {radius:g} mi. "
          "This usually takes about a minute; the results will appear here automatically.", "info")
    return redirect(url_for("property_page", pid=pid, tab="comps", view="sold"))

@app.route("/job/<int:jid>")
def job_status(jid):
    row = db().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row: return jsonify({"status": "missing"}), 404
    return jsonify({"id": row["id"], "kind": row["kind"], "status": row["status"],
                    "message": row["message"], "started_at": row["started_at"], "finished_at": row["finished_at"]})

@app.route("/comp/<int:cid>/<action>", methods=["POST"])
def comp_action(cid, action):
    c = db().execute("SELECT * FROM comps WHERE id=?", (cid,)).fetchone()
    if not c: abort(404)
    if action == "pin":
        db().execute("UPDATE comps SET pinned=1-pinned WHERE id=?", (cid,))
        nc = db().execute("SELECT pinned FROM comps WHERE id=?", (cid,)).fetchone()
        flash(f"{'📌 Pinned' if nc['pinned'] else 'Unpinned'}: {c['address']} — header averages and ARV now use the pinned set.", "ok")
    elif action == "exclude": db().execute("UPDATE comps SET excluded=1-excluded WHERE id=?", (cid,))
    elif action == "adjust":
        db().execute("UPDATE comps SET adj_condition=?, adj_size=?, adj_other=? WHERE id=?",
                     (fnum(request.form.get("adj_condition"), 0), fnum(request.form.get("adj_size"), 0),
                      fnum(request.form.get("adj_other"), 0), cid))
    db().commit()
    kw = {"pid": c["property_id"], "tab": "comps"}
    for k in ("view", "window", "rmax"):
        if request.form.get(k): kw[k] = request.form.get(k)
    if c["kind"] == "rent" and kw.get("view") not in ("ltr", "coliving", "affordable"):
        kw["view"] = "ltr"
    return redirect(url_for("property_page", **kw) + f"#comp-{cid}")

@app.route("/property/<int:pid>/lien", methods=["POST"])
def add_lien(pid):
    db().execute("INSERT INTO liens(property_id,lien_type,amount,payment,rate,maturity) VALUES(?,?,?,?,?,?)",
                 (pid, request.form.get("lien_type"), fnum(request.form.get("amount")),
                  fnum(request.form.get("payment")), fnum(request.form.get("rate")), request.form.get("maturity")))
    db().commit(); return redirect(url_for("property_page", pid=pid))

@app.route("/lien/<int:lid>/delete", methods=["POST"])
def del_lien(lid):
    row = db().execute("SELECT property_id FROM liens WHERE id=?", (lid,)).fetchone()
    db().execute("DELETE FROM liens WHERE id=?", (lid,)); db().commit()
    return redirect(url_for("property_page", pid=row["property_id"]))

@app.route("/property/<int:pid>/underwrite", methods=["POST"])
def underwrite(pid):
    p = get_prop(pid)
    strategy = request.form.get("strategy")
    inp = {}
    for k, val in request.form.items():
        if k in ("strategy", "save_name"): continue
        inp[k] = fnum(val, val if val != "" else None)
    for group in ("rehab_lines", "appliance_lines", "furnishing_lines", "conversion_lines", "funding_stack"):
        raw = request.form.get(group + "_json", "").strip()
        if raw:
            try: inp[group] = json.loads(raw)
            except Exception: inp[group] = []
    res = compute_underwriting(strategy, inp, p)
    name = request.form.get("save_name") or f"{strategy} — {datetime.date.today().isoformat()}"
    db().execute("INSERT INTO scenarios(property_id,strategy,name,inputs,results) VALUES(?,?,?,?,?)",
                 (pid, strategy, name, json.dumps(inp), json.dumps(res)))
    db().commit()
    if strategy == "Fix & Flip" and res.get("rule70_mao"):
        pass
    flash(f"{strategy} scenario saved: " + (f"score {res['score']} ({res['label']})" if res.get("score") else f"profit ${res.get('profit'):,}"))
    return redirect(url_for("property_page", pid=pid, tab="underwriting"))

@app.route("/scenario/<int:sid>/delete", methods=["POST"])
def del_scenario(sid):
    row = db().execute("SELECT property_id FROM scenarios WHERE id=?", (sid,)).fetchone()
    db().execute("DELETE FROM scenarios WHERE id=?", (sid,)); db().commit()
    return redirect(url_for("property_page", pid=row["property_id"], tab="underwriting"))

@app.route("/property/<int:pid>/amortize", methods=["POST"])
def amortize(pid):
    p = get_prop(pid)
    principal = fnum(request.form.get("loan_amount"), 0) or 0
    rate = fnum(request.form.get("rate"), 0) or 0
    yrs = fnum(request.form.get("amort_years"), 30) or 30
    term = fnum(request.form.get("loan_years"), yrs) or yrs
    res = amortization(principal, rate, yrs, term)
    if request.form.get("add_to_stack"):
        sc = db().execute("SELECT * FROM scenarios WHERE property_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        flash(f"Amortization: payment ${res['payment']:,}/mo, balloon after year {int(term)}: ${res['balloon']:,}. "
              "Add it as a funding-stack line in your next underwriting run (type: Loan).")
    return render_template("amortization.html", p=p, res=res, principal=principal, rate=rate, yrs=yrs, term=term)

@app.route("/doc/<int:did>", methods=["POST"])
def doc_update(did):
    row = db().execute("SELECT property_id FROM documents WHERE id=?", (did,)).fetchone()
    db().execute("UPDATE documents SET done=?, notes=? WHERE id=?",
                 (1 if request.form.get("done") else 0, request.form.get("notes", ""), did))
    db().commit(); return redirect(url_for("property_page", pid=row["property_id"], tab="documents"))

@app.route("/documents")
def documents_index():
    rows = db().execute("""SELECT p.id, p.address, COUNT(d.id) total, SUM(d.done) done FROM properties p
                           LEFT JOIN documents d ON d.property_id=p.id GROUP BY p.id ORDER BY p.id DESC""").fetchall()
    return render_template("documents.html", rows=rows)

@app.route("/contractors", methods=["GET", "POST"])
def contractors_page():
    if request.method == "POST":
        db().execute("INSERT INTO contractors(name,company,trade,phone,email,notes) VALUES(?,?,?,?,?,?)",
                     (request.form.get("name"), request.form.get("company"), request.form.get("trade"),
                      request.form.get("phone"), request.form.get("email"), request.form.get("notes")))
        db().commit(); return redirect(url_for("contractors_page"))
    rows = db().execute("SELECT * FROM contractors ORDER BY trade, name").fetchall()
    return render_template("contractors.html", rows=rows)

@app.route("/contractors/import", methods=["POST"])
def do_import_contractors():
    try:
        res = import_houseable_prospects()
        flash(f"HouseAble prospect import: {res['imported']} imported, {res['duplicates_skipped']} duplicates skipped, "
              f"{res['skipped_no_name']} skipped (no name), of {res['prospects_in_file']} prospects in file.")
    except Exception as e:
        flash(f"Prospect import failed: {e}")
    return redirect(url_for("contractors_page"))

@app.route("/contractor/<int:cid>/delete", methods=["POST"])
def del_contractor(cid):
    db().execute("DELETE FROM contractors WHERE id=?", (cid,)); db().commit()
    return redirect(url_for("contractors_page"))

@app.route("/property/<int:pid>/assign", methods=["POST"])
def assign(pid):
    db().execute("INSERT INTO assignments(property_id,contractor_id,trade,walkthrough,quote,notes) VALUES(?,?,?,?,?,?)",
                 (pid, request.form.get("contractor_id") or None, request.form.get("trade"),
                  request.form.get("walkthrough"), fnum(request.form.get("quote")), request.form.get("notes")))
    db().commit(); return redirect(url_for("property_page", pid=pid, tab="contractors"))

@app.route("/property/<int:pid>/floorplan", methods=["POST"])
def floorplan(pid):
    fn = None
    f = request.files.get("file")
    if f and f.filename:
        fn = f"{pid}_{f.filename}".replace("/", "_")
        f.save(os.path.join(UPLOAD_DIR, fn))
    db().execute("INSERT INTO floorplans(property_id,title,filename,notes) VALUES(?,?,?,?)",
                 (pid, request.form.get("title"), fn, request.form.get("notes")))
    db().commit(); return redirect(url_for("property_page", pid=pid, tab="floorplans"))

@app.route("/property/<int:pid>/monitor", methods=["POST"])
def add_monitor(pid):
    db().execute("""INSERT INTO monitors(property_id,frequency,price_threshold,dom_threshold,stop_on_status)
                    VALUES(?,?,?,?,?)""",
                 (pid, request.form.get("frequency", "Weekly"), fnum(request.form.get("price_threshold")),
                  fnum(request.form.get("dom_threshold")), 1 if request.form.get("stop_on_status") else 0))
    db().commit(); return redirect(url_for("monitoring_page"))

@app.route("/monitoring")
def monitoring_page():
    rows = db().execute("""SELECT m.*, p.address, p.list_price FROM monitors m JOIN properties p
                           ON p.id=m.property_id ORDER BY m.id DESC""").fetchall()
    mjobs = {}
    for r in db().execute("SELECT * FROM jobs WHERE kind='monitor' ORDER BY id DESC"):
        mjobs.setdefault(r["property_id"], r)
        if len(mjobs) >= 50: break
    return render_template("monitoring.html", rows=rows, mjobs=mjobs)

def _monitor_check_now(mid):
    """One live monitor check. Returns a plain-English result message (job wrapper surfaces errors)."""
    m = db().execute("SELECT * FROM monitors WHERE id=?", (mid,)).fetchone()
    if not m: return "Monitor no longer exists."
    p = get_prop(m["property_id"]); note = "No live listing found in ZIP pull (may be off market)"
    new_price, new_status = None, "Not found"
    from homeharvest import scrape_property
    df = scrape_property(location=p["zip"], listing_type="for_sale", past_days=365)
    srow = find_subject_row(df, p)
    if srow is not None:
        new_price = fnum(srow.get("list_price")); new_status = _s(srow.get("status")) or "For Sale"
    if new_price:
        note = f"Price now ${new_price:,.0f}" + (f" (was ${m['last_price']:,.0f})" if m["last_price"] and m["last_price"] != new_price else "")
        if m["stop_on_status"] and new_status.lower() in ("sold", "off market", "rented"):
            db().execute("UPDATE monitors SET active=0 WHERE id=?", (mid,)); note += " — monitor stopped per stop rule"
    db().execute("UPDATE monitors SET last_checked=?, last_price=?, last_status=?, last_note=? WHERE id=?",
                 (datetime.datetime.now().isoformat(timespec="seconds"), new_price, new_status, note, mid))
    db().commit()
    return f"✓ Check complete for {p['address']}: {note}"

@app.route("/monitor/<int:mid>/check", methods=["POST"])
def monitor_check(mid):
    m = db().execute("SELECT * FROM monitors WHERE id=?", (mid,)).fetchone()
    if not m: abort(404)
    job_monitor_check(mid, m["property_id"])
    flash("Monitor check started — the result will appear on this page in a moment.", "info")
    return redirect(url_for("monitoring_page"))

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        if "census_key" in request.form:
            for k in ("census_key", "rentcast_key", "apillow_key"):
                set_setting(k, request.form.get(k, ""))
            flash("API keys saved (stored server-side in local DB only).")
        elif "bb_name" in request.form:
            db().execute("""INSERT INTO buy_boxes(name,cities,max_price,min_beds,min_sqft,strategies,min_arv_spread_pct)
                            VALUES(?,?,?,?,?,?,?)""",
                         (request.form.get("bb_name"), request.form.get("bb_cities"), fnum(request.form.get("bb_max_price")),
                          fnum(request.form.get("bb_min_beds")), fnum(request.form.get("bb_min_sqft")),
                          request.form.get("bb_strategies"), fnum(request.form.get("bb_spread"))))
            db().commit(); flash("Buy box added.")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", boxes=all_boxes(),
                           census_key=get_setting("census_key"), rentcast_key=get_setting("rentcast_key"),
                           apillow_key=get_setting("apillow_key"))

@app.route("/buybox/<int:bid>/toggle", methods=["POST"])
def bb_toggle(bid):
    db().execute("UPDATE buy_boxes SET active=1-active WHERE id=?", (bid,)); db().commit()
    return redirect(url_for("settings_page"))

@app.route("/export.json")
def export_json():
    d = db(); bundle = {}
    for t in ("properties","liens","comps","documents","contractors","assignments","scenarios","buy_boxes","monitors","floorplans"):
        bundle[t] = [dict(r) for r in d.execute(f"SELECT * FROM {t}")]
    buf = io.BytesIO(json.dumps(bundle, indent=1, default=str).encode())
    return send_file(buf, mimetype="application/json", as_attachment=True, download_name="deal-analyzer-export.json")

@app.route("/export.csv")
def export_csv():
    d = db(); out = io.StringIO(); w = csv.writer(out)
    w.writerow(["type","id","address","city","state","zip","status","deal_method","price","beds","baths","sqft","match_score","source"])
    for r in d.execute("SELECT * FROM properties"):
        w.writerow(["property", r["id"], r["address"], r["city"], r["state"], r["zip"], r["status"], r["deal_method"], r["list_price"], r["beds"], r["baths"], r["sqft"], "", ""])
    for r in d.execute("SELECT * FROM comps"):
        w.writerow(["comp", r["id"], r["address"], r["city"], "", r["zip"], r["kind"], "", r["price"], r["beds"], r["baths"], r["sqft"], r["match_score"], r["source"]])
    buf = io.BytesIO(out.getvalue().encode())
    return send_file(buf, mimetype="text/csv", as_attachment=True, download_name="deal-analyzer-export.csv")

@app.route("/import", methods=["POST"])
def import_json():
    f = request.files.get("file")
    if not f: flash("No file."); return redirect(url_for("settings_page"))
    bundle = json.load(f)
    d = db(); idmap = {}
    for t in ("contractors", "buy_boxes", "properties"):
        for row in bundle.get(t, []):
            old = row.pop("id", None)
            cols = ",".join(row.keys()); ph = ",".join("?" * len(row))
            cur = d.execute(f"INSERT INTO {t}({cols}) VALUES({ph})", tuple(row.values()))
            if t == "properties": idmap[old] = cur.lastrowid; seed_docs(cur.lastrowid)
    for t in ("liens","comps","documents","assignments","scenarios","monitors","floorplans"):
        for row in bundle.get(t, []):
            row.pop("id", None)
            if row.get("property_id") in idmap: row["property_id"] = idmap[row["property_id"]]
            if t == "documents":
                d.execute("INSERT OR IGNORE INTO documents(property_id,category,slot,done,notes) VALUES(?,?,?,?,?)",
                          (row["property_id"], row["category"], row["slot"], row.get("done", 0), row.get("notes"))); continue
            cols = ",".join(row.keys()); ph = ",".join("?" * len(row))
            try: d.execute(f"INSERT INTO {t}({cols}) VALUES({ph})", tuple(row.values()))
            except Exception: pass
    d.commit(); flash("Import complete.")
    return redirect(url_for("settings_page"))

@app.template_filter("money")
def money(v):
    return "Not found" if v is None else f"${v:,.0f}"

@app.template_filter("num")
def numf(v, dec=0):
    return "—" if v is None else f"{v:,.{dec}f}"

@app.template_filter("jload")
def jload(v):
    try: return json.loads(v) if v else {}
    except Exception: return {}

if __name__ == "__main__":
    init_db()
    app.run(host=os.environ.get("PDA_HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", os.environ.get("PDA_PORT", "5050"))), debug=False)
