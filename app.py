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

# ---------------------------------------------------------------- underwriting (v1.3)
REHAB_ROOMS = ["Bedrooms", "Bathrooms", "Kitchen", "Living Room", "Dining Room", "Laundry Room",
               "Fireplace", "Basement", "Attic", "General Updates/Repairs", "Misc"]
REHAB_FLATS = [("adu", "ADU & Additions"), ("hvac", "HVAC"), ("electrical", "Electrical"),
               ("plumbing", "Plumbing"), ("exterior", "Exterior & Structural"),
               ("contingency", "Repairs Contingency"), ("other", "Other (flat)")]
APPLIANCE_ROWS = ["Refrigerator", "Mini Fridge", "Washer and Dryer", "Water Heater", "Stove",
                  "Microwave", "Air Fryer", "Toaster"]
CONVERSION_ROWS = ["Security Cameras", "Smart Locks", "Smart Thermostat", "Door Sensors",
                   "IT Closet", "Property Pictures", "Compliance Items"]
FURNISH_GROUPS = [
    ("Bedrooms", ["Bed frame", "Mattress", "Bedding", "Nightstand", "Lamp", "Desk & Chair",
                  "Closet/Dresser", "Bedroom Décor"]),
    ("Bathrooms", ["Towel sets", "Shower curtain", "Bath mat", "Trash can"]),
    ("Kitchen Furnishing", ["Cookware set", "Dishes & cutlery", "Small appliances", "Utensils & knives"]),
    ("Common Area Furnishing", ["Sofa", "Coffee table", "TV", "Dining set", "Lamps"]),
    ("Cleaning Supplies", ["Vacuum", "Mop & broom set", "Cleaning starter kit"]),
]
REHAB_LEVELS = {"cosmetic": {"label": "Average / Cosmetic Rehab", "low": 25, "high": 35, "mid": 30},
                "heavy": {"label": "Heavy / Full Gut Rehab", "low": 40, "high": 50, "mid": 45}}
FUNDING_TYPES = ["Conventional", "DSCR", "Hard Money", "Private Money", "Bridge", "HELOC",
                 "Seller Financing", "Subject-To", "Cash", "Equity", "Loan", "Private", "SubTo", "Seller Finance"]

def _lines_total(lines):
    return sum((l.get("qty", 1) or 1) * ((l.get("material", 0) or 0) + (l.get("labor", 0) or 0)) for l in lines or [])

def _loan_payment(amount, rate_pct, term_years):
    """Monthly payment: amortized when a term is given, interest-only otherwise (v1.2 behavior)."""
    amt = fnum(amount, 0) or 0
    if amt <= 0: return 0.0
    rate = fnum(rate_pct, 0) or 0
    yrs = fnum(term_years, 0) or 0
    if yrs > 0:
        r = rate / 100 / 12; n = int(yrs * 12)
        if r > 0: return amt * r / (1 - (1 + r) ** -n)
        return amt / n
    return amt * rate / 100 / 12

def canon_inputs(strategy, inp, prop):
    """Translate stored/legacy (v1.2) scenario inputs into the v1.3 canonical field set.
    The underwrite route stores parsed JSON groups AND the raw *_json strings; drop the strings.
    Legacy aggregates (rooms + rent_per_room, utilities_mo, ...) are mapped by compute fallbacks;
    the Co-Living per-room table is materialized here from the legacy rooms/rent fields."""
    c = {}
    for k, v in (inp or {}).items():
        if k.endswith("_json"): continue
        c[k] = v
    if c.get("booking_days") in (None, "") and c.get("booking_fee_days") not in (None, ""):
        c["booking_days"] = c.get("booking_fee_days")
    if strategy == "Co-Living":
        rows = c.get("rooms_rows")
        if not rows:
            n = int(fnum(c.get("rooms"), (prop["beds"] if prop else None) or 4) or 4)
            rpm = fnum(c.get("rent_per_room"), 0) or 0
            wk = round(rpm * 12 / 52, 2) if rpm else 0
            vac = fnum(c.get("vacancy_pct"), 15)
            c["rooms_rows"] = [{"bath": "Shared", "rent_wk": wk, "vacancy": 15 if vac is None else vac}
                               for _ in range(max(n, 0))]
    return c

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

def _score_label(total):
    return "Strong" if total >= 75 else ("Workable" if total >= 55 else ("Thin" if total >= 40 else "Weak"))

def compute_underwriting(strategy, raw_inp, prop):
    """v1.3 ReiSearch-mirrored shell: post-reno details, rehab (calculated rows or sqft estimate),
    appliances, conversion, furnishing, revenue per strategy, detailed operating expenses,
    funding stack + existing mortgage, holding. Math mirrors static/uw.js exactly."""
    inp = canon_inputs(strategy, raw_inp, prop)
    arv = fnum(inp.get("arv"), prop["arv"] if prop else None) or 0
    price = fnum(inp.get("purchase_price"), (prop["purchase_price"] or prop["list_price"]) if prop else None) or 0
    rehab_calc = _lines_total(inp.get("rehab_lines")) + (fnum(inp.get("rehab_flat"), 0) or 0)
    rehab_mode = inp.get("rehab_mode") or "calc"
    rehab_est = fnum(inp.get("rehab_est_total"), 0) or 0
    rehab = rehab_est if (rehab_mode == "est" and rehab_est) else rehab_calc
    appliances = _lines_total(inp.get("appliance_lines"))
    furnishing = 0 if strategy == "Long Term" else _lines_total(inp.get("furnishing_lines"))
    conversion = _lines_total(inp.get("conversion_lines")) if strategy == "Co-Living" else 0
    total_project = price + rehab + appliances + furnishing + conversion
    # holding
    h_period = fnum(inp.get("holding_period"), None)
    if h_period is None:
        h_period = fnum(inp.get("holding_months"), 6 if strategy == "Fix & Flip" else 0) or 0
        h_unit = "Months"
    else:
        h_unit = inp.get("holding_unit") or "Months"
    h_months = h_period if h_unit == "Months" else h_period / 30.0
    holding_mo = fnum(inp.get("holding_mo"), 0) or 0
    holding_total = holding_mo * h_months
    # funding stack
    stack = inp.get("funding_stack") or []
    funded = cash_equity = debt_amt = debt_rows_service = 0.0
    paydown_y1 = 0.0
    for s in stack:
        amt = fnum(s.get("amount"), 0) or 0
        funded += amt
        if s.get("type") in ("Cash", "Equity"):
            cash_equity += amt
        else:
            debt_amt += amt
            debt_rows_service += _loan_payment(amt, s.get("rate"), s.get("term_years"))
            yrs = fnum(s.get("term_years"), 0) or 0
            if yrs > 0 and amt > 0:
                am = amortization(amt, fnum(s.get("rate"), 0) or 0, yrs, 1)
                if am["rows"]: paydown_y1 += am["rows"][0]["principal"]
    # existing mortgage data (all strategies except Fix & Flip)
    entry_fee = md_payment = 0.0
    if strategy != "Fix & Flip":
        entry_fee = fnum(inp.get("md_entry_fee"), 0) or 0
        md_payment = fnum(inp.get("md_monthly_payment"), 0) or 0
        paydown_y1 += (fnum(inp.get("md_piti_principal"), 0) or 0) * 12
    debt_service = debt_rows_service + md_payment
    closing = fnum(inp.get("closing_costs"), 0) or 0
    uses = total_project + holding_total
    gap = uses - funded
    # Cash/equity stack lines ARE cash invested; any unfunded gap is also cash (v1.2 fix).
    cash_in = cash_equity + max(gap, 0) + closing + entry_fee
    out = {"strategy": strategy, "arv": arv, "purchase_price": price, "rehab": round(rehab),
           "rehab_calc": round(rehab_calc), "rehab_mode": rehab_mode, "appliances": round(appliances),
           "furnishing": round(furnishing), "conversion": round(conversion),
           "total_project": round(total_project), "holding_cost": round(holding_total),
           "holding_months": round(h_months, 2), "uses": round(uses), "funded": round(funded),
           "gap": round(gap), "cash_in": round(cash_in), "financing_costs": round(closing),
           "debt_service_mo": round(debt_service), "entry_fee": round(entry_fee)}

    if strategy == "Fix & Flip":
        sale = fnum(inp.get("sale_price"), 0) or arv
        comm_pct = fnum(inp.get("commission_pct"), 6) or 0
        close_pct = fnum(inp.get("closing_pct"), 2) or 0
        comm_amt = sale * comm_pct / 100; close_amt = sale * close_pct / 100
        net = sale - comm_amt - close_amt
        profit = net - total_project - holding_total
        all_in = total_project + holding_total
        roi = round(profit / cash_in * 100, 1) if cash_in else None
        margin = round(profit / sale * 100, 1) if sale else None
        ann = round(profit / cash_in * 100 * 12 / h_months, 1) if cash_in and h_months else None
        def _flip(sale2, rehab2, hold2):
            n2 = sale2 * (1 - comm_pct / 100 - close_pct / 100)
            return {"sale": round(sale2), "all_in": round(price + rehab2 + appliances + furnishing + hold2),
                    "net": round(n2 - (price + rehab2 + appliances + furnishing) - hold2)}
        scen = {"Downside (-10% sale price)": _flip(sale * 0.9, rehab, holding_total),
                "Base": _flip(sale, rehab, holding_total),
                "Upside (+10% sale price)": _flip(sale * 1.1, rehab, holding_total),
                "Overrun (rehab +20%, +3 mo hold)": _flip(sale, rehab * 1.2, holding_mo * (h_months + 3))}
        # Flip Deal Score: ReiSearch shows one for flips; scale = profit margin (50%, target 30%)
        # + ROI (50%, target 50%), labeled as our scale in Details.
        def cs(x, t): return max(0.0, min(100.0, (x / t) * 100)) if t else 0.0
        parts = {"Profit margin (50%)": round(0.5 * cs((margin or 0) / 100, 0.30), 1),
                 "ROI on cash (50%)": round(0.5 * cs((roi or 0) / 100, 0.50), 1)}
        fscore = max(1, min(99, round(sum(parts.values()))))
        out.update({"sale_price": round(sale), "commission_pct": comm_pct, "closing_pct": close_pct,
                    "commission_amt": round(comm_amt), "closing_amt": round(close_amt),
                    "net_proceeds": round(net), "all_in": round(all_in), "profit": round(profit),
                    "roi": roi, "annualized_roi": ann, "margin_pct": margin,
                    "rule70_mao": round(arv * 0.70 - rehab), "scenarios": scen,
                    "score": fscore, "label": _score_label(fscore), "score_parts": parts, "penalties": []})
        return out

    # ---------------- rental strategies ----------------
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
    # revenue
    cleaning_income = 0.0
    if strategy == "Co-Living":
        rows = inp.get("rooms_rows") or []
        gross = sum((fnum(r.get("rent_wk"), 0) or 0) * 52 / 12 for r in rows)
        vac_loss = sum((fnum(r.get("rent_wk"), 0) or 0) * 52 / 12 * ((fnum(r.get("vacancy"), 15) or 0) / 100) for r in rows)
        rooms_n = len(rows) or int(fnum(inp.get("rooms"), (prop["beds"] if prop else None) or 4) or 4)
        laundry_amt = fnum(inp.get("laundry_amt"), 0) or 0
        laundry_mo = laundry_amt * 52 / 12 if (inp.get("laundry_period") or "wk") == "wk" else laundry_amt
        other = (fnum(inp.get("amenities_mo"), 0) or 0) + (fnum(inp.get("late_fees_mo"), 0) or 0) \
            + laundry_mo + (fnum(inp.get("misc_income_mo"), 0) or 0)
    elif strategy == "Short Term":
        units = fnum(inp.get("units"), 1) or 1
        rooms_n = units
        gross = (fnum(inp.get("nightly_rate"), 0) or 0) * 30 * ((fnum(inp.get("occupancy_pct"), 0) or 0) / 100) * units
        stays = (30 / (fnum(inp.get("avg_stay_nights"), 3) or 3)) * units
        cleaning_income = stays * (fnum(inp.get("cleaning_fee"), 0) or 0)
        vac_loss = 0.0
        other = (fnum(inp.get("pet_fees_mo"), 0) or 0) + (fnum(inp.get("parking_mo"), 0) or 0) \
            + (fnum(inp.get("misc_mo"), 0) or 0) + cleaning_income
    else:  # Mid Term / Long Term
        units = fnum(inp.get("units"), 1) or 1
        rooms_n = units
        gross = (fnum(inp.get("rent_per_unit"), 0) or 0) * units
        vac_loss = gross * ((fnum(inp.get("vacancy_pct"), 5) or 0) / 100)
        other = sum(fnum(inp.get(k), 0) or 0 for k in ("pet_mo", "parking_mo", "storage_mo", "laundry_mo", "misc_mo"))
    egi = gross - vac_loss + other
    # booking & service fees (PadSplit logic, mirrored for all rental strategies per ReiSearch)
    days_default = 10 if strategy == "Co-Living" else 0
    booking_days = fnum(inp.get("booking_days"), days_default) or 0
    booking_fee = booking_days * (gross / 30) / 12 if gross else 0.0
    service_fee = egi * ((fnum(inp.get("service_fee_pct"), 0) or 0) / 100)
    mgmt_basis = inp.get("mgmt_fee_basis") or "gross"
    mgmt_fee = (gross if mgmt_basis == "gross" else egi) * ((fnum(inp.get("mgmt_fee_pct"), 0) or 0) / 100)
    platform_fee = gross * ((fnum(inp.get("platform_pct"), 0) or 0) / 100) if strategy == "Short Term" else 0.0
    fees_cat = booking_fee + service_fee + mgmt_fee + platform_fee
    # expense categories with legacy fallbacks
    util_new = sum(fnum(inp.get(k), 0) or 0 for k in ("electric_mo", "water_mo", "gas_mo", "internet_mo"))
    util = util_new if util_new > 0 else (fnum(inp.get("utilities_mo"), 0) or 0)
    cleaning = fnum(inp.get("cleaning_mo"), 0) or 0
    maint_new = sum(fnum(inp.get(k), 0) or 0 for k in ("landscaping_mo", "pest_mo", "handyman_mo"))
    maint = maint_new if maint_new > 0 else (fnum(inp.get("maintenance_mo"), 0) or 0)
    mgmt_new = sum(fnum(inp.get(k), 0) or 0 for k in ("property_manager_mo", "turnover_reserve_mo", "additional_insurance_mo"))
    mgmt_lines = mgmt_new if mgmt_new > 0 else (fnum(inp.get("management_mo"), 0) or 0)
    hoa = fnum(inp.get("hoa_mo"), (prop["hoa_dues"] if prop and prop["hoa_freq"] == "Monthly" else 0) or 0) or 0
    opex_cats = {"Booking & Service Fees": round(fees_cat), "Utilities": round(util),
                 "Cleaning Service": round(cleaning), "Maintenance": round(maint),
                 "Management": round(mgmt_lines), "Taxes, Insurance & HOA": round(taxes_mo + ins_mo + hoa)}
    opex = sum(opex_cats.values())
    if util == 0 and maint == 0:
        penalties.append({"points": 10, "reason": "No utilities/maintenance entered — expenses look unrealistically low"})
    noi = egi - opex
    cf = noi - debt_service
    value = arv or price
    metrics = {"cf_per_room": (cf / rooms_n) if rooms_n else 0,
               "dscr": (noi / debt_service) if debt_service else (1.5 if noi > 0 else 0),
               "coc": (cf * 12 / cash_in) if cash_in else 0,
               "cap_rate": (noi * 12 / value) if value else 0,
               "breakeven": ((opex + debt_service) / (gross + other)) if (gross + other) else 1.3,
               "downside_cf": (egi * 0.9 - opex - debt_service)}
    def _scen(mult, no_vac=False):
        e2 = gross + other if no_vac else (gross - vac_loss) * mult + other
        return {"revenue": round(e2), "expenses": round(opex + debt_service), "net": round(e2 - opex - debt_service)}
    scen = {"Downside (-10% revenue)": _scen(0.9), "Base": _scen(1.0), "Upside (+10% revenue)": _scen(1.1)}
    if strategy == "Co-Living":
        scen["Fully booked (0% vacancy)"] = _scen(1.0, no_vac=True)
    sc = score_rental(metrics, penalties)
    refi_gross = round(0.75 * arv) if arv else 0
    refi_net = max(0, refi_gross - debt_amt)
    out.update({"gross_mo": round(gross), "vacancy_loss_mo": round(vac_loss),
                "other_income_mo": round(other), "cleaning_income_mo": round(cleaning_income),
                "fees_mo": round(fees_cat), "booking_fee_mo": round(booking_fee),
                "platform_fee_mo": round(platform_fee),
                "service_fee_mo": round(service_fee), "mgmt_fee_mo": round(mgmt_fee),
                "egi_mo": round(egi), "opex_mo": round(opex), "opex_cats": opex_cats,
                "taxes_mo": taxes_mo, "insurance_mo": ins_mo, "noi_mo": round(noi),
                "cashflow_mo": round(cf), "cf_per_room": round(metrics["cf_per_room"]),
                "cash_per_room": round(cash_in / rooms_n) if rooms_n else None,
                "dscr": round(metrics["dscr"], 2), "coc_pct": round(metrics["coc"] * 100, 1),
                "cap_rate_pct": round(metrics["cap_rate"] * 100, 2),
                "breakeven_pct": round(metrics["breakeven"] * 100, 1),
                "payback_yrs": round(1 / metrics["coc"], 1) if metrics["coc"] > 0 else None,
                "scenarios": scen, "rooms": rooms_n,
                "refi_proceeds": refi_gross, "remaining_in_deal": round(max(0, cash_in - refi_net)),
                "paydown_y1": round(paydown_y1), "cashflow_y1": round(cf * 12),
                "total_return_y1": round(cf * 12 + paydown_y1),
                "total_roi_y1": round((cf * 12 + paydown_y1) / cash_in * 100, 1) if cash_in else None,
                "score": sc["score"], "label": sc["label"], "score_parts": sc["parts"],
                "penalties": sc["penalties"], "flags": flags})
    return out

# ------- form state for the v1.3 underwriting page (server-rendered defaults + scenario load) -------
def _split_rehab(lines):
    """Split stored rehab_lines into ordered room rows + flat-row dict (qty folded into $)."""
    rooms = {name: {"label": name, "material": 0, "labor": 0} for name in REHAB_ROOMS}
    flats = {k: {"material": 0, "labor": 0} for k, _ in REHAB_FLATS}
    flat_by_label = {lab.lower(): k for k, lab in REHAB_FLATS}
    extra = []
    for l in lines or []:
        qty = l.get("qty", 1) or 1
        mat = (l.get("material", 0) or 0) * qty; lab = (l.get("labor", 0) or 0) * qty
        lab_name = (l.get("label") or "").strip()
        key = l.get("flat") or flat_by_label.get(lab_name.lower())
        if key and key in flats:
            flats[key]["material"] += mat; flats[key]["labor"] += lab; continue
        hit = None
        for name in REHAB_ROOMS:
            nl, ll = name.lower(), lab_name.lower()
            if ll == nl or nl.startswith(ll) or ll.startswith(nl.split()[0]):
                hit = name; break
        if hit:
            rooms[hit]["material"] += mat; rooms[hit]["labor"] += lab
        elif lab_name:
            extra.append({"label": lab_name, "material": mat, "labor": lab})
    room_rows = [rooms[n] for n in REHAB_ROOMS] + extra
    return room_rows, flats

def _unit_rows(lines, presets, qty_default=0):
    """qty x unit-cost rows from stored lines (unit = material + labor per unit)."""
    found = {}
    extra = []
    for l in lines or []:
        qty = l.get("qty", 1) or 1
        unit = ((l.get("material", 0) or 0) + (l.get("labor", 0) or 0))
        lab = (l.get("label") or "").strip()
        if lab in presets: found[lab] = {"label": lab, "qty": qty, "unit": unit}
        elif lab: extra.append({"label": lab, "qty": qty, "unit": unit})
    return [found.get(p, {"label": p, "qty": qty_default, "unit": 0}) for p in presets] + extra

def uw_state(strategy, prop, saved_inp, ctx):
    """Canonical form state (scalars + row sets) for one strategy panel.
    saved_inp=None -> fresh defaults with comp prefills (v1.2 behavior)."""
    inp = canon_inputs(strategy, saved_inp or {}, prop)
    def g(k, default=""):
        v = inp.get(k)
        return default if v in (None, "") else v
    beds = int(prop["beds"] or 4)
    st = {"strategy": strategy}
    st["purchase_price"] = g("purchase_price", prop["purchase_price"] or prop["list_price"] or "")
    st["arv"] = g("arv", prop["arv"] or ctx.get("rec_arv") or "")
    st["closing_costs"] = g("closing_costs", 3000)
    # post-reno details
    st["postreno_beds"] = g("postreno_beds", prop["beds"] or "")
    st["postreno_baths"] = g("postreno_baths", prop["baths"] or "")
    st["postreno_private_baths"] = g("postreno_private_baths", "")
    st["postreno_shared_baths"] = g("postreno_shared_baths", prop["baths"] or "")
    st["postreno_sqft"] = g("postreno_sqft", prop["sqft"] or "")
    # rehab
    st["rehab_mode"] = g("rehab_mode", "calc")
    st["rehab_level"] = g("rehab_level", "cosmetic")
    st["rehab_est_total"] = g("rehab_est_total", "")
    room_rows, flats = _split_rehab(inp.get("rehab_lines"))
    if fnum(inp.get("rehab_flat"), 0):
        flats["other"]["material"] += fnum(inp.get("rehab_flat"), 0)
    st["rehab_rooms"] = room_rows
    st["rehab_flats"] = {k: flats[k] for k, _ in REHAB_FLATS}
    # appliances / conversion / furnishing
    st["appliances"] = _unit_rows(inp.get("appliance_lines"), APPLIANCE_ROWS)
    st["conversion"] = _unit_rows(inp.get("conversion_lines"), CONVERSION_ROWS)
    furn = []
    seen_groups = set()
    for l in inp.get("furnishing_lines") or []:
        qty = l.get("qty", 1) or 1
        furn.append({"group": l.get("group") or "Bedrooms", "label": (l.get("label") or "").strip(),
                     "qty": qty, "unit": (l.get("material", 0) or 0) + (l.get("labor", 0) or 0)})
        seen_groups.add(l.get("group") or "Bedrooms")
    if not furn:
        for gname, items in FURNISH_GROUPS:
            for it in items:
                furn.append({"group": gname, "label": it,
                             "qty": beds if gname == "Bedrooms" else 0, "unit": 0})
    st["furnishing"] = furn
    # funding
    st["funding"] = [{"type": s.get("type") or "Conventional", "amount": s.get("amount") or 0,
                      "rate": s.get("rate") or 0, "term_years": s.get("term_years") or ""}
                     for s in (inp.get("funding_stack") or [])]
    # holding
    if inp.get("holding_period") not in (None, ""):
        st["holding_period"] = inp.get("holding_period"); st["holding_unit"] = inp.get("holding_unit") or "Months"
    elif inp.get("holding_months") not in (None, ""):
        st["holding_period"] = inp.get("holding_months"); st["holding_unit"] = "Months"
    else:
        st["holding_period"] = 6 if strategy == "Fix & Flip" else 0; st["holding_unit"] = "Months"
    st["holding_mo"] = g("holding_mo", 450 if strategy == "Fix & Flip" else 0)
    # existing mortgage data
    st["md_avg_price"] = g("md_avg_price", "")
    st["md_repairs"] = g("md_repairs", prop["repairs"] or "")
    st["md_arv"] = g("md_arv", st["arv"])
    st["md_mao"] = g("md_mao", ctx.get("mao") or "")
    st["md_contract_price"] = g("md_contract_price", st["purchase_price"])
    st["md_listing_price"] = g("md_listing_price", prop["list_price"] or "")
    for k in ("md_assignment_fee", "md_cash_to_seller", "md_mls_commission", "md_entry_fee",
              "md_piti_principal", "md_piti_interest", "md_piti_pmi", "md_piti_escrow"):
        st[k] = g(k, "")
    st["md_piti_taxes"] = g("md_piti_taxes", round(prop["tax_amount"] / 12) if prop["tax_amount"] else "")
    st["md_piti_insurance"] = g("md_piti_insurance", "")
    st["md_liens"] = g("md_liens", ctx.get("liens_total") or "")
    st["md_monthly_payment"] = g("md_monthly_payment", prop["loan_piti"] or "")
    st["md_rate"] = g("md_rate", prop["loan_rate"] or "")
    # revenue per strategy
    if strategy == "Co-Living":
        rows = inp.get("rooms_rows") if saved_inp else None
        if not rows:
            wk = (ctx.get("coli") or {}).get("weekly_per_room") or 189
            rows = [{"bath": "Shared", "rent_wk": wk, "vacancy": 15} for _ in range(beds)]
        st["rooms_rows"] = rows
        st["amenities_mo"] = g("amenities_mo", 0); st["late_fees_mo"] = g("late_fees_mo", 0)
        st["laundry_amt"] = g("laundry_amt", 0); st["laundry_period"] = g("laundry_period", "wk")
        st["misc_income_mo"] = g("misc_income_mo", 0); st["moving_fee"] = g("moving_fee", 0)
    elif strategy == "Short Term":
        st["units"] = g("units", 1); st["nightly_rate"] = g("nightly_rate", 120)
        st["occupancy_pct"] = g("occupancy_pct", 65); st["avg_stay_nights"] = g("avg_stay_nights", 3)
        st["cleaning_fee"] = g("cleaning_fee", 90); st["platform_pct"] = g("platform_pct", 15)
        st["pet_fees_mo"] = g("pet_fees_mo", 0); st["parking_mo"] = g("parking_mo", 0); st["misc_mo"] = g("misc_mo", 0)
    elif strategy in ("Mid Term", "Long Term"):
        st["units"] = g("units", 1)
        st["rent_per_unit"] = g("rent_per_unit", (ctx.get("ltr") or {}).get("avg_rent") or 1600)
        st["pet_mo"] = g("pet_mo", 0); st["parking_mo"] = g("parking_mo", 0)
        st["storage_mo"] = g("storage_mo", 0); st["laundry_mo"] = g("laundry_mo", 0); st["misc_mo"] = g("misc_mo", 0)
    # shared rental fields
    if strategy != "Fix & Flip":
        st["vacancy_pct"] = g("vacancy_pct", 15 if strategy == "Co-Living" else 5)
        st["booking_days"] = g("booking_days", 10 if strategy == "Co-Living" else 0)
        st["service_fee_pct"] = g("service_fee_pct", 8 if strategy == "Co-Living" else 0)
        st["mgmt_fee_pct"] = g("mgmt_fee_pct", 0); st["mgmt_fee_basis"] = g("mgmt_fee_basis", "gross")
        for k in ("electric_mo", "water_mo", "gas_mo", "internet_mo", "cleaning_mo",
                  "landscaping_mo", "pest_mo", "handyman_mo", "property_manager_mo",
                  "turnover_reserve_mo", "additional_insurance_mo", "taxes_mo", "insurance_mo", "hoa_mo"):
            st[k] = g(k, 0)
        # legacy aggregates surface as fallbacks in compute; show them on the matching first line
        if not any(fnum(st[k], 0) for k in ("electric_mo", "water_mo", "gas_mo", "internet_mo")) and fnum(inp.get("utilities_mo"), 0):
            st["electric_mo"] = inp.get("utilities_mo")
        if not any(fnum(st[k], 0) for k in ("landscaping_mo", "pest_mo", "handyman_mo")) and fnum(inp.get("maintenance_mo"), 0):
            st["landscaping_mo"] = inp.get("maintenance_mo")
        if not any(fnum(st[k], 0) for k in ("property_manager_mo", "turnover_reserve_mo", "additional_insurance_mo")) and fnum(inp.get("management_mo"), 0):
            st["property_manager_mo"] = inp.get("management_mo")
    # flip sale & exit
    if strategy == "Fix & Flip":
        st["sale_price"] = g("sale_price", st["arv"])
        st["commission_pct"] = g("commission_pct", 6)
        st["closing_pct"] = g("closing_pct", 2)
    return st


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
    recent = db().execute("""SELECT s.*, p.address FROM scenarios s JOIN properties p ON p.id=s.property_id
                             ORDER BY s.id DESC LIMIT 6""").fetchall()
    return render_template("dashboard.html", props=props, counts=counts, recent=recent)

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
    # ---- comparables views (v1.3: all rendered, switched in place) ----
    cview = request.args.get("view", "sold")
    if cview not in ("maps", "sold", "coliving", "str", "mtr", "ltr", "affordable"): cview = "sold"
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
    # ---- underwriting form states (v1.3): fresh defaults + every saved scenario, canonicalized ----
    strategies = ["Fix & Flip", "Co-Living", "Short Term", "Mid Term", "Long Term"]
    uw_ctx = {"coli": coli, "ltr": ltr, "rec_arv": v["rec"]["arv"], "mao": v["mao"],
              "liens_total": sum((l["amount"] or 0) for l in liens)}
    uw_fresh, uw_saved, uw_current = {}, {}, {}
    scen_by_strategy = {}
    for s in scenarios:
        try: scen_by_strategy.setdefault(s["strategy"], []).append(s)
        except Exception: pass
    for strat in strategies:
        fresh = uw_state(strat, p, None, uw_ctx)
        uw_fresh[strat] = fresh
        latest = None
        for s in scen_by_strategy.get(strat, []):
            try: saved_inp = json.loads(s["inputs"] or "{}")
            except Exception: saved_inp = {}
            uw_saved[s["id"]] = {"strategy": strat, "name": s["name"],
                                 "state": uw_state(strat, p, saved_inp, uw_ctx)}
            if latest is None: latest = uw_saved[s["id"]]["state"]
        uw_current[strat] = latest if latest is not None else fresh
    # pinned (or top-3) sold comps for the Flip Sale & Exit table
    flip_pool = [c for c in sold_view if c["pinned"]] or \
        sorted(sold_view, key=lambda c: -(c["match_score"] or 0))[:3]
    flip_prices = [c["price"] for c in flip_pool if c["price"]]
    return render_template("property.html", p=p, comps=comps, sold=sold, rents=rents, liens=liens, docs=docs,
                           assigns=assigns, contractors=contractors, scenarios=scenarios, plans=plans,
                           v=v, market=market, demo=demo, tab=tab, amort=amort, amenities=amenities,
                           photos=photos, job=latest_job(pid), steps=steps,
                           cview=cview, window=window, rmax=rmax, sold_view=sold_view, rents_view=rents_view,
                           sold_stats=sold_stats, ltr=ltr, coli=coli, med_rent=med_rent, afford_view=afford_view,
                           breakdowns=breakdowns, room_est=room_est,
                           strategies=strategies, uw_fresh=uw_fresh, uw_saved=uw_saved, uw_current=uw_current,
                           flip_pool=flip_pool,
                           flip_range=(min(flip_prices), max(flip_prices)) if flip_prices else None)

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
        db().commit()
        nc = db().execute("SELECT pinned FROM comps WHERE id=?", (cid,)).fetchone()
        if request.form.get("ajax") or request.headers.get("X-Requested-With") == "fetch":
            # v1.3 in-place pinning: return the recomputed stats so the page updates live.
            # The stats respect the viewer's current filter (sold window + radius),
            # matching the filtered stat bars rendered on the page.
            pid = c["property_id"]; p = get_prop(pid)
            allc = db().execute("SELECT * FROM comps WHERE property_id=? AND excluded=0", (pid,)).fetchall()
            sold_all = [x for x in allc if x["kind"] == "sold"]
            rent_all = [x for x in allc if x["kind"] == "rent"]
            win = request.form.get("win") or request.form.get("window") or ""
            if win in ("30d", "90d", "6mo", "1yr"):
                _days = {"30d": 30, "90d": 90, "6mo": 182, "1yr": 365}[win]
                _cut = (datetime.date.today() - datetime.timedelta(days=_days)).isoformat()
                sold_all = [x for x in sold_all
                            if not x["event_date"] or x["event_date"] >= _cut]
            try:
                _rmax = float(request.form.get("rmax") or "")
            except (TypeError, ValueError):
                _rmax = None
            if _rmax:
                sold_all = [x for x in sold_all
                            if x["distance_mi"] is None or x["distance_mi"] <= _rmax]
                rent_all = [x for x in rent_all
                            if x["distance_mi"] is None or x["distance_mi"] <= _rmax]
            rec = arv_reconciliation(p, allc)
            return jsonify({"pinned": nc["pinned"], "kind": c["kind"],
                            "sold_stats": pinned_sold_stats(sold_all), "ltr": ltr_stats(rent_all),
                            "arv": rec["arv"], "arv_note": rec["note"],
                            "mao": round(rec["arv"] * 0.70 - (p["repairs"] or 0)) if rec["arv"] else None})
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
    for group in ("rehab_lines", "appliance_lines", "furnishing_lines", "conversion_lines", "funding_stack", "rooms_rows"):
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
