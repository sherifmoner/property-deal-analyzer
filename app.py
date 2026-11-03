"""Property Deal Analyzer — HouseAble clean-room rebuild of the ReiSearch property page.
Flask + SQLite, server-rendered. Free data only: ArcGIS geocoder, FEMA NFHL, HomeHarvest, US Census ACS.
Run: .venv/bin/python app.py  ->  http://127.0.0.1:5050
"""
import json, math, os, re, sqlite3, datetime, io, csv, threading, ast, time
from functools import wraps
import requests
from flask import Flask, request, redirect, url_for, render_template, jsonify, g, send_file, abort, flash, Response
import hmac

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PDA_DB", os.path.join(BASE, "analyzer.db"))
UPLOAD_DIR = os.path.join(BASE, "uploads"); os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("PDA_SECRET", "pda-local-dev")

@app.before_request
def _require_login():
    """Optional password gate. Set PDA_PASSWORD (and optionally PDA_USER, default 'admin') in the
    hosting dashboard to lock the whole app behind a browser login. Unset = open (local use)."""
    pw = os.environ.get("PDA_PASSWORD")
    if not pw:
        return None
    a = request.authorization
    if a and hmac.compare_digest(a.password or "", pw) and hmac.compare_digest(a.username or "", os.environ.get("PDA_USER", "admin")):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Deal Analyzer"'})

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
CREATE TABLE IF NOT EXISTS uw_drafts(property_id INTEGER, strategy TEXT, state TEXT, results TEXT,
 updated_at TEXT, PRIMARY KEY(property_id, strategy));
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
CREATE TABLE IF NOT EXISTS property_notes(id INTEGER PRIMARY KEY, property_id INTEGER,
 author TEXT, text TEXT, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS property_trades(id INTEGER PRIMARY KEY, property_id INTEGER,
 trade TEXT, UNIQUE(property_id, trade));
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
    for col in ("amenities_json", "amenities_date", "photos_json", "photos_date", "demo_json", "demo_date",
                "demo_error", "amenities_error"):
        if col not in cols: d.execute(f"ALTER TABLE properties ADD COLUMN {col} TEXT")
    ccols = [r[1] for r in d.execute("PRAGMA table_info(comps)")]
    if "ptype" not in ccols: d.execute("ALTER TABLE comps ADD COLUMN ptype TEXT")
    if "lot_sqft" not in ccols: d.execute("ALTER TABLE comps ADD COLUMN lot_sqft REAL")
    jcols = [r[1] for r in d.execute("PRAGMA table_info(jobs)")]
    if "dismissed" not in jcols: d.execute("ALTER TABLE jobs ADD COLUMN dismissed INTEGER DEFAULT 0")
    # v1.7b: overview deal-structure extras + seller finance + disposition extras
    for col in ("cash_to_seller", "total_entry_fee", "closing_costs_paid_by", "purchase_deposit",
                "sf_loan_amount", "sf_amort_years", "sf_amort_unit", "sf_rate", "sf_payment",
                "sf_balloon", "dispo_exit_method", "dispo_buyer_deposit", "dispo_day_type"):
        if col not in cols: d.execute(f"ALTER TABLE properties ADD COLUMN {col} TEXT")
    # v1.7b: per-trade assignments gain visit/quote status + property-local contact snapshots
    acols = [r[1] for r in d.execute("PRAGMA table_info(assignments)")]
    for col in ("visit_status", "quote_status", "contact_company", "contact_name",
                "contact_phone", "contact_email"):
        if col not in acols: d.execute(f"ALTER TABLE assignments ADD COLUMN {col} TEXT")
    # v2.1 repair: text that slipped into numeric columns ("$410,000") becomes a real number (or NULL)
    for col in NUMERIC_PROP_FIELDS:
        if col in cols:
            for rid, raw in d.execute(f"SELECT id, {col} FROM properties WHERE typeof({col})='text'").fetchall():
                d.execute(f"UPDATE properties SET {col}=? WHERE id=?", (clean_num(raw), rid))
    # v2.1 repair: autosaved drafts still carrying the old PadSplit-only defaults on non-co-living
    # strategies (untouched values only; anything the user changed is left alone).
    _old = {"electric_mo": 400, "water_mo": 80, "gas_mo": 50, "internet_mo": 70, "cleaning_mo": 120,
            "property_manager_mo": 300, "booking_days": 10, "service_fee_pct": 8}
    _fix = {"Long Term": {"electric_mo": 0, "water_mo": 0, "gas_mo": 0, "internet_mo": 0, "cleaning_mo": 0,
                          "property_manager_mo": 0, "booking_days": 0, "service_fee_pct": 0},
            "Mid Term": {"cleaning_mo": 0, "property_manager_mo": 0, "booking_days": 0, "service_fee_pct": 0},
            "Short Term": {"booking_days": 0, "service_fee_pct": 0}}
    for pid_, strat_, st_ in d.execute("SELECT property_id, strategy, state FROM uw_drafts WHERE strategy IN ('Long Term','Mid Term','Short Term')").fetchall():
        try: s_ = json.loads(st_ or "{}")
        except Exception: continue
        fix_ = _fix[strat_]
        if all(fnum(s_.get(k)) == _old[k] for k in fix_):
            s_.update(fix_)
            d.execute("UPDATE uw_drafts SET state=? WHERE property_id=? AND strategy=?", (json.dumps(s_), pid_, strat_))
    d.commit(); d.close()

# Schema creation must happen at import time, not only under __main__: production
# serves via `gunicorn app:app` (module import — see repo Dockerfile / render.yaml),
# where the __main__ block never runs. v1.5 shipped with init_db() called only in
# __main__, so the uw_drafts table was never created on the deployed instance and
# every /uw-autosave POST died with "no such table: uw_drafts" (HTTP 500), while
# local runs via `python app.py` looked fine. init_db() is idempotent
# (CREATE TABLE IF NOT EXISTS + PRAGMA-guarded ALTERs), so calling it is safe.
# v2.1: the call now sits at the bottom of the file, after the helpers init_db() uses are defined.

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

# v2.1: property columns that are REAL/INTEGER in the schema. A typed "$410,000" / "250k" used to be
# stored as literal text and then crashed every property tab with a 500 error.
NUMERIC_PROP_FIELDS = (
    "list_price", "beds", "baths", "sqft", "lot_sqft", "year_built", "hoa_dues", "as_is_value", "arv",
    "repairs", "purchase_price", "inspection_days", "buy_it_now", "min_offer", "wholesale_contract_amt",
    "emd_ab", "emd_bc", "loan_balance", "loan_rate", "loan_piti", "dispo_asking", "dispo_bin", "dispo_min",
    "dispo_inspection_days", "est_zillow", "est_realtor", "est_redfin", "est_county", "tax_market",
    "tax_assessed", "tax_taxable", "tax_amount")

def clean_num(v):
    """Typed money/number text -> float, or None when blank/unreadable. Accepts $ , % spaces and k/m suffix."""
    if v is None: return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else None
    t = str(v).strip().lower().replace("$", "").replace(",", "").replace("%", "").replace(" ", "")
    if not t: return None
    mult = 1
    if t.endswith("k"): mult, t = 1000, t[:-1]
    elif t.endswith("m"): mult, t = 1000000, t[:-1]
    try:
        x = float(t) * mult
        return x if math.isfinite(x) else None
    except ValueError:
        return None

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

COMP_WEIGHT_DEFAULTS = {"distance": 30, "sqft": 20, "beds": 14, "baths": 14, "year": 14, "lot": 8}

def comp_weights():
    """Comp match weights from app settings (Weights dialog on the Comparables tab),
    ReiSearch pass-2 defaults: Distance 30 / Sqft 20 / Beds 14 / Baths 14 / Year 14 / Lot 8."""
    try: w = json.loads(get_setting("comp_weights") or "{}")
    except Exception: w = {}
    out = {}
    for k, dv in COMP_WEIGHT_DEFAULTS.items():
        v = fnum(w.get(k), None)
        out[k] = v if (v is not None and v >= 0) else dv
    if sum(out.values()) <= 0: return dict(COMP_WEIGHT_DEFAULTS)
    return out

def score_breakdown(subj, comp):
    """ReiSearch comp-match model (pass 2): SIX factors — Distance, Sqft, Beds, Baths,
    Year Built, Lot Size — with editable weights (defaults 30/20/14/14/14/8) and
    tolerances 1 mi / 35% of subject sqft / 2 bd / 2 ba / 20 yrs / 50% of subject lot.
    Points are each factor's normalized share of 100, so the total stays a 0-100
    match score whatever weights are saved. Missing data scores a neutral 0.5.
    One score, used everywhere."""
    w = comp_weights()
    tot = sum(w.values()) or 100
    def share(k): return w[k] / tot * 100
    def closeness(a, b, tol):
        a = fnum(a, None); b = fnum(b, None)
        if a is None or b is None: return 0.5
        return max(0.0, 1.0 - abs(a - b) / tol)
    d = comp.get("distance_mi")
    d_score = 0.5 if d is None else max(0.0, 1.0 - d / 1.0)          # 0 mi = 1.0, 1+ mi = 0
    sq = subj.get("sqft") or 1500
    lot = subj.get("lot_sqft")
    lot_c = closeness(comp.get("lot_sqft"), lot, lot * 0.5) if lot else 0.5
    def lab(name, k): return f"{name} ({share(k):.0f})"
    return {
        lab("Distance", "distance"): round(share("distance") * d_score, 1),
        lab("Sqft", "sqft"): round(share("sqft") * closeness(comp.get("sqft"), subj.get("sqft"), sq * 0.35), 1),
        lab("Beds", "beds"): round(share("beds") * closeness(comp.get("beds"), subj.get("beds"), 2), 1),
        lab("Baths", "baths"): round(share("baths") * closeness(comp.get("baths"), subj.get("baths"), 2), 1),
        lab("Year built", "year"): round(share("year") * closeness(comp.get("year_built"), subj.get("year_built"), 20), 1),
        lab("Lot size", "lot"): round(share("lot") * lot_c, 1),
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
    pool = [c for c in sold if c["price"] and c["kind"] == "sold"]
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

def _norm_funding(f):
    """v1.5 funding-card normalization: legacy rows {type, amount, rate, term_years} gain the
    ReiSearch card fields (preset, loan type, position, term value+unit, points, fees,
    payment type, escrow). Defaults chosen so legacy payments are unchanged:
    term present -> Amortized, no term -> Interest Only.
    v2.3 TWO-TERM MODEL (ReiSearch pass 2026-11-03): cards carry a Loan Term
    (payment-invariant; amortization horizon/balloon only) AND an Amortization
    Term (drives the payment). Legacy rows carry only the single term — it WAS
    the payment term, so it migrates to BOTH fields and payments are unchanged."""
    f = dict(f or {})
    ty = f.get("type") or "Conventional"
    tv = f.get("term_value")
    if tv in (None, "") and f.get("term_years") not in (None, ""):
        tv = (fnum(f.get("term_years"), 0) or 0) * 12
        f["term_unit"] = "Months"
    if tv in (None, ""):
        tv = ""
    f["type"] = ty
    f["preset"] = f.get("preset") or ty
    f["loan_type"] = f.get("loan_type") or ty
    f["position"] = f.get("position") or "1st Position"
    f["term_value"] = tv
    f["term_unit"] = f.get("term_unit") or "Months"
    months = (fnum(tv, 0) or 0) * (1 if f["term_unit"] == "Months" else 12)
    f["term_years"] = (months / 12) if months else ""
    if "amort_value" not in f or f.get("amort_value") is None:
        f["amort_value"] = tv
        f["amort_unit"] = f["term_unit"]
    else:
        f["amort_unit"] = f.get("amort_unit") or "Months"
    f["lender"] = f.get("lender") or ""
    if f.get("pct") in (None, ""):
        f["pct"] = ""
    f["basis"] = f.get("basis") or "price"
    f["interest_type"] = f.get("interest_type") or "simple"
    for k in ("points", "fees", "taxes_mo", "insurance_mo"):
        f[k] = fnum(f.get(k), 0) or 0
    if not f.get("payment_type"):
        a_months = (fnum(f["amort_value"], 0) or 0) * (1 if f["amort_unit"] == "Months" else 12)
        f["payment_type"] = "fixed" if fnum(f.get("fixed_payment"), 0) else ("amortized" if a_months else "interest_only")
    if f.get("fixed_payment") in (None, ""):
        f["fixed_payment"] = ""
    return f

def _amort_months(f):
    """The AMORTIZATION term in months (v2.3: this is the payment term).
    Rows that never passed _norm_funding fall back to the legacy single term."""
    if "amort_value" in f and f.get("amort_value") is not None:
        tv = fnum(f.get("amort_value"), 0) or 0
        return tv if (f.get("amort_unit") or "Months") == "Months" else tv * 12
    tv = fnum(f.get("term_value"), 0) or 0
    return tv if (f.get("term_unit") or "Months") == "Months" else tv * 12

def _row_payment(f):
    """(principal+interest payment, escrow taxes+insurance) for one funding card.
    Payment types mirror ReiSearch: Amortized | Interest Only | Principal Only | Fixed Payment."""
    if (f.get("type") or "") in ("Cash", "Equity"):
        return 0.0, 0.0
    rate = fnum(f.get("rate"), 0) or 0
    months = _amort_months(f)  # v2.3: the AMORTIZATION term drives the payment (Loan Term never does)
    pt = f.get("payment_type") or "amortized"
    # v1.8b: a Fixed payment IS the payment — it must not require a loan amount
    # (Subject-To cards have a blank amount by design; blank cards start at 0).
    if pt == "fixed":
        return (fnum(f.get("fixed_payment"), 0) or 0,
                (fnum(f.get("taxes_mo"), 0) or 0) + (fnum(f.get("insurance_mo"), 0) or 0))
    amt = fnum(f.get("amount"), 0) or 0
    if amt <= 0: return 0.0, 0.0
    esc = (fnum(f.get("taxes_mo"), 0) or 0) + (fnum(f.get("insurance_mo"), 0) or 0)
    if pt in ("amortized", "principal_only") and not months:
        # v2.0 (P0-3, mirrors uw.js): no term typed -> NO payment. The old
        # else-branch priced a term-less Amortized card as interest-only
        # (_loan_payment with 0 years), so the term field looked dead.
        return 0.0, esc
    if pt == "interest_only":
        pay = amt * rate / 100 / 12
    elif pt == "principal_only":
        # ReiSearch: Principal Only = monthly interest + straight-line principal (balance / amort months)
        pay = amt * rate / 100 / 12 + amt / months
    else:
        pay = _loan_payment(amt, rate, months / 12)
    return pay, esc

def _paydown_y1(f):
    """Year-1 principal paydown for one funding card, by payment type.
    Interest Only pays NO principal (balance flat) — v1.6 fix: the old code ran the
    generic amortization table for IO loans and booked the whole balance as paydown."""
    if (f.get("type") or "") in ("Cash", "Equity"):
        return 0.0
    amt = fnum(f.get("amount"), 0) or 0
    if amt <= 0: return 0.0
    rate = fnum(f.get("rate"), 0) or 0
    months = _amort_months(f)  # v2.3: payment (hence paydown) follows the AMORTIZATION term
    pt = f.get("payment_type") or "amortized"
    if pt == "interest_only":
        return 0.0
    if pt == "principal_only":
        return min(amt, amt / months * 12) if months else 0.0
    if pt == "fixed":
        pay = fnum(f.get("fixed_payment"), 0) or 0
    elif not months:
        return 0.0  # v2.0: Amortized with no term -> no payment -> no paydown
    else:
        pay = _loan_payment(amt, rate, months / 12)
    if pay <= 0: return 0.0
    r = rate / 100 / 12; bal = amt; prin = 0.0
    for _ in range(12):
        if bal <= 0: break
        p = min(max(pay - bal * r, 0.0), bal)
        prin += p; bal -= p
    return prin

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
    if c.get("funding_stack"):
        c["funding_stack"] = [_norm_funding(f) for f in c["funding_stack"]]
    if strategy == "Co-Living":
        rows = c.get("rooms_rows")
        if not rows:
            n = int(fnum(c.get("rooms"), (prop["beds"] if prop else None) or 4) or 4)
            rpm = fnum(c.get("rent_per_room"), 0) or 0
            wk = round(rpm * 12 / 52, 2) if rpm else 0
            vac = fnum(c.get("vacancy_pct"), 15)
            rows = [{"bath": "Shared", "rent_wk": wk, "vacancy": 15 if vac is None else vac}
                    for _ in range(max(n, 0))]
        norm = []
        for i, r in enumerate(rows):
            r = dict(r or {})
            wk = fnum(r.get("rent_wk"), 0) or 0
            if r.get("rent_mo") in (None, ""):
                r["rent_mo"] = round(wk * 52 / 12, 2) if wk else 0
            if not r.get("bath_group"):
                r["bath_group"] = "A" if (i // 2) % 2 == 0 else "B"
            if r.get("amenity_mo") in (None, ""):
                r["amenity_mo"] = 0
            norm.append(r)
        c["rooms_rows"] = norm
    return c

# v2.4: Deal Score = ReiSearch's MEASURED model (pass 2026-11-03, Addenda
# 2+3; mirrors uw.js exactly). Score = weighted average over the SCORED
# factors only (a "no data" factor is EXCLUDED from the denominator), minus
# penalties, rounded half-up. Per-factor curves are piecewise-linear through
# ReiSearch's measured points; between points the simplest monotone
# interpolation, beyond the end points the end segment's slope, clamped
# 0..100. The total is computed from UNROUNDED factor scores — Senisa
# proves it: its displayed factors average exactly 84.5 yet ReiSearch shows
# 84, because its CF/room sits between knots at 64.6 unrounded. Cap rate
# scores percent x 10 capped at 100 (measured 8.9->89, 8.6->86, 14.9->100).
# v3.0 (Pass #3, 2026-11-03 evening): Cash-on-cash does NOT use that line —
# it is REFUTED at CoC 14.5% (the line gives 100; ReiSearch scores 93).
# CoC is now a piecewise-linear curve through the three measured anchors
# (-27.7% -> 0, 14.5% -> 93, 49.0% -> 100 — all Hard Money states at Cash
# Needed $6,397), reproducing the published totals 15/41/54 with the other
# factors on the v2.8 curves. Still FLAGGED: a 3-anchor curve — the
# segments between anchors are interpolation, not measurement.
# v2.8 REFIT (evening measurement pass 2026-11-03, research/
# reisearch-measurement-2026-11-03-evening.md): the curves are now
# piecewise-linear through EVERY published (value -> displayed score)
# point — the v2.4 knots plus R1-R8 and the Electric probes. Senisa's
# CF/room is deliberately NOT a knot: it interpolates to 64.6 on the
# (194.42,64)->(241.64,73) segment, which its total of 84 requires (a
# knot there would push the total to 85). The CF saturation knot at
# (571.15, 100) is DERIVED, not directly published: the $500 probe's
# total of 90 forces CF(571) >= 99.7 and the $300 probe's 91 forces
# CF(621) >= 98.5, while the $150 probe publishes CF(658.65) = 100
# outright. All 17 fixtures (7 v2.4 + R1-R8 + 3 probes, factors AND
# totals) reproduce exactly under these constants — full residual table
# in research/v28-build-log.md.
_SCORE_CURVES = {
    "cf": [(-219.17, 0), (-56.45, 0), (-36.85, 0), (18.98, 8), (33.07, 13),
           (68.98, 28), (125.15, 46), (194.42, 64), (195.44, 64), (241.64, 73),
           (571.15, 100), (658.65, 100), (696.15, 100)],
    "dscr": [(0.7312, 0), (0.925, 12), (0.9497, 15), (1.0303, 25), (1.0527, 29),
             (1.11, 40), (1.1996, 56), (1.3903, 79), (1.5316, 91), (1.8265, 97)],
    "be": [(0, 100), (4.58, 100), (55.5, 95), (61.14, 89), (63.68, 86),
           (70.86, 78), (76.58, 67), (80.74, 58), (82.68, 52), (89.5, 32),
           (91.89, 24), (111.76, 0)],
    "ds": [(-288.78, 0), (-126.07, 0), (-106.47, 0), (-50.63, 0), (-32.95, 14),
           (-0.63, 39), (49.92, 57), (124.8, 81), (125.83, 81), (130.62, 83),
           (172.02, 93), (589.04, 100), (626.54, 100)],
    "coc": [(-27.7, 0), (14.5, 93), (49.0, 100)],
}

def _pw_curve(x, knots):
    n = len(knots)
    if x <= knots[0][0]: i = 0
    elif x >= knots[-1][0]: i = n - 2
    else:
        i = 0
        while not (knots[i][0] <= x <= knots[i + 1][0]): i += 1
    x0, y0 = knots[i]; x1, y1 = knots[i + 1]
    return max(0.0, min(100.0, y0 + (x - x0) * (y1 - y0) / (x1 - x0)))

def _rhu(x):
    return math.floor(x + 0.5)

def _fmt_money0(x):
    return ("−$" if x < 0 else "$") + f"{_rhu(abs(x)):,}" + "/mo"

def _fmt_pct1(x):
    if not x: return "0%"
    return ("−" if x < 0 else "") + f"{_rhu(abs(x) * 10) / 10:.1f}" + "%"

def score_factors(m):
    """m: cf_per_room, dscr_val (None = no debt -> scored 100, 'No debt'),
    coc_val (fraction, None = no data -> EXCLUDED), cap_rate (fraction),
    breakeven (fraction), downside_cf_per_room."""
    dscr_val = m.get("dscr_val")
    coc_val = m.get("coc_val")
    return [
        {"key": "cf", "name": "Cash flow per room", "weight": 25,
         "value": _fmt_money0(m.get("cf_per_room", 0)),
         "score": _pw_curve(m.get("cf_per_room", 0), _SCORE_CURVES["cf"])},
        {"key": "dscr", "name": "DSCR", "weight": 20,
         "value": "No debt" if dscr_val is None else f"{dscr_val:.2f}",
         "score": 100.0 if dscr_val is None else _pw_curve(dscr_val, _SCORE_CURVES["dscr"])},
        {"key": "coc", "name": "Cash-on-cash", "weight": 20,
         "value": "No data" if coc_val is None else _fmt_pct1(coc_val * 100),
         "score": None if coc_val is None else _pw_curve(coc_val * 100, _SCORE_CURVES["coc"])},
        {"key": "be", "name": "Break-even occupancy", "weight": 12,
         "value": _fmt_pct1(m.get("breakeven", 0) * 100),
         "score": _pw_curve(m.get("breakeven", 0) * 100, _SCORE_CURVES["be"])},
        {"key": "ds", "name": "Downside cash flow per room", "weight": 8,
         "value": _fmt_money0(m.get("downside_cf_per_room", 0)),
         "score": _pw_curve(m.get("downside_cf_per_room", 0), _SCORE_CURVES["ds"])},
        {"key": "cap", "name": "Cap rate", "weight": 15,
         "value": _fmt_pct1(m.get("cap_rate", 0) * 100),
         "score": max(0.0, min(100.0, m.get("cap_rate", 0) * 1000))},
    ]

def _insight_from_factors(factors):
    """Note logic derived from ReiSearch's five measured notes (Addenda
    2+3): strongest = highest factor score, ties broken by the higher
    weight (Cash: CF/room wins a four-way tie at 100 on weight 25;
    Conventional: Cap rate beats Break-even at 89 on weight 15 > 12;
    Hard Money weakest: CF/room wins a three-way tie at 0 on weight 25).
    A 'Weakest' is named only when the lowest scored factor is below 65
    (Conventional's 64 is named; Senisa's 65 is not). Comparisons use the
    DISPLAYED (rounded) factor scores, as ReiSearch does: Conventional's
    Break-even (88.96 unrounded) and Cap rate (88.80) both display 89, and
    ReiSearch names Cap rate — the tie is broken by weight; Senisa's
    CF/room (64.6 unrounded) displays 65 and ReiSearch reports 'No weak
    spots', so the <65 weak test also reads the displayed value."""
    scored = [f for f in factors if f["score"] is not None]
    if not scored: return ""
    strong = sorted(scored, key=lambda f: (-_rhu(f["score"]), -f["weight"]))[0]
    weak = sorted(scored, key=lambda f: (_rhu(f["score"]), -f["weight"]))[0]
    return "Strongest: " + strong["name"] + ". " + \
        ("Weakest: " + weak["name"] + "." if _rhu(weak["score"]) < 65 else "No weak spots.")

def score_rental(m, penalties):
    factors = score_factors(m)
    sw = ss = 0.0
    parts = {}
    for fc in factors:
        if fc["score"] is None: continue
        sw += fc["weight"]; ss += fc["weight"] * fc["score"]
        parts[f"{fc['name']} ({fc['weight']}%)"] = round(fc["score"], 1)
    total = (ss / sw) if sw else 0.0
    for p in penalties: total -= p["points"]
    total = max(1, min(99, _rhu(total)))
    label = "Strong" if total >= 75 else ("Good" if total >= 65 else ("Fair" if total >= 55 else "Weak"))
    return {"score": total, "label": label, "parts": parts, "factors": factors, "penalties": penalties}

def _score_label(total):
    return "Strong" if total >= 75 else ("Good" if total >= 65 else ("Fair" if total >= 55 else "Weak"))

# v2.8 (A8, evening pass 2026-11-03): Add Funding Source dialog card kinds.
# Amount-only types render a single Amount field and contribute it to
# Funding Added only. Assumption types render the measured single-Term
# anatomy and price through the standard debt branch.
AMOUNT_ONLY_TYPES = ("Private Money Partner", "JV Equity", "Reserves")
ASSUMP_TYPES = ("Loan Assumption", "Wraparound")

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
    # funding stack (v1.5 cards: payment type per card + points/fees into financing costs)
    stack = inp.get("funding_stack") or []
    funded = cash_equity = debt_amt = debt_rows_service = 0.0
    paydown_y1 = 0.0
    points_fees = 0.0
    carry_mo = 0.0          # v2.1: monthly cost of carrying funded debt (Fix & Flip profit)
    piti_included = False   # v2.1: True only when a loan payment really contains taxes+insurance
    debt_lines = []
    for s in stack:
        amt = fnum(s.get("amount"), 0) or 0
        funded += amt
        # v2.8 (A8): Amount-only cards (Private Money Partner / JV Equity /
        # Reserves, from the Add Funding Source dialog) contribute their
        # Amount to Funding Added ONLY — no financing costs, no cash-equity
        # bucket, no debt service (measured anatomy: a single Amount field).
        _kind = s.get("kind") or ("amount" if s.get("type") in AMOUNT_ONLY_TYPES else
                                  "assump" if s.get("type") in ASSUMP_TYPES else "loan")
        if _kind == "amount":
            continue
        points_fees += amt * (fnum(s.get("points"), 0) or 0) / 100 + (fnum(s.get("fees"), 0) or 0)
        if s.get("type") in ("Cash", "Equity"):
            cash_equity += amt
        else:
            debt_amt += amt
            if _kind == "assump":
                # v2.8 (A8, FLAGGED INFERENCE — see v28 build log): Loan
                # Assumption / Wraparound cards carry ONE Term field; the
                # payment is priced by the standard engine using that Term
                # as the amortization term (filled-card behavior was not
                # directly measured; blank state shows $0 either way).
                s = dict(s)
                if not (fnum(s.get("amort_value"), 0) or 0) and (fnum(s.get("term_value"), 0) or 0):
                    s["amort_value"] = s.get("term_value")
                    s["amort_unit"] = s.get("term_unit") or "Years"
            pay, esc = _row_payment(s)
            debt_rows_service += pay + esc
            paydown_y1 += _paydown_y1(s)
            if esc > 0 or (s.get("payment_type") == "fixed" and pay > 0): piti_included = True
            if s.get("payment_type") == "fixed": carry_mo += pay + esc
            else: carry_mo += amt * (fnum(s.get("rate"), 0) or 0) / 100 / 12 + esc
            lab = s.get("loan_type") or s.get("type") or "Loan"
            if s.get("preset") and s.get("preset") != lab: lab = f"{lab} · {s['preset']}"
            debt_lines.append({"label": lab, "payment": round(pay + esc)})
    # existing mortgage data (all strategies except Fix & Flip)
    # v1.8a PITI wiring: the breakdown fields are LIVE components of the payment,
    # not decoration. Base = the typed Monthly Payment, or the Principal+Interest
    # split when no total is typed. Taxes / Insurance / PMI / Escrow are escrow
    # add-ons counted on top (same pattern as funding-card escrow in _row_payment).
    entry_fee = md_payment = 0.0
    md_base = md_escrow = 0.0
    md_addons = {}
    if strategy != "Fix & Flip":
        entry_fee = fnum(inp.get("md_entry_fee"), 0) or 0
        _prin = fnum(inp.get("md_piti_principal"), 0) or 0
        _int = fnum(inp.get("md_piti_interest"), 0) or 0
        md_base = fnum(inp.get("md_monthly_payment"), 0) or 0
        if md_base <= 0: md_base = _prin + _int
        for k, lab in (("md_piti_taxes", "Taxes"), ("md_piti_insurance", "Insurance"),
                       ("md_piti_pmi", "PMI"), ("md_piti_escrow", "Escrow")):
            v = fnum(inp.get(k), 0) or 0
            if v: md_addons[lab] = round(v)
            md_escrow += v
        md_payment = md_base + md_escrow
        if md_payment > 0: piti_included = True
        paydown_y1 += _prin * 12
    debt_service = debt_rows_service + md_payment
    # v2.3: ANY loan payment retires the OpEx taxes/insurance auto-estimates
    # (mirrors uw.js; ReiSearch fixtures Conventional NOI $2,785 / NCF $782 /
    # DSCR 1.39 only reproduce this way — see uw.js note).
    if debt_service > 0: piti_included = True
    closing = fnum(inp.get("closing_costs"), 0) or 0
    financing_costs = closing + points_fees
    uses = total_project + holding_total
    gap = uses - funded
    # v1.6 Cash Needed semantics (ReiSearch): cash contributions + points/fees + closing
    # + entry fee. A loan-only funding GAP does not inflate Cash Needed on rentals
    # (ReiSearch: Conventional 80% loan -> Cash Needed $0, CoC "—"). Fix & Flip keeps
    # the gap in Cash Needed — the flip buyer brings the gap in cash at closing
    # (Senisa fixture: gap $61,684 = Cash Needed $61,684).
    cash_in = cash_equity + points_fees + closing + entry_fee
    if strategy == "Fix & Flip":
        cash_in += max(gap, 0)
    # v2.3: the v2.1 "gap for every strategy" override is REVERTED on measured
    # ReiSearch evidence (pass 2026-11-03): Hard Money Cash Needed $6,397 =
    # financing costs alone against a $56,445 gap, and ReiSearch's Total ROI
    # -27.7% = -$1,769 / $6,397 confirms gap-free Cash Needed on rentals.
    # v2.9 (Pass #2, 2026-11-03 evening; supersedes the v1.6/v2.3 lines
    # above AND the 2026-11-01 behavior spec's "moves with cash
    # contributions" claim): ReiSearch's Cash Needed = cash costs that
    # sit OUTSIDE Total Uses = financing costs (points + fees) + the
    # Subject-To entry fee. Measured across 8 funding states (Total Uses
    # $376,300 constant): Cash-only / empty / Conventional / Private /
    # Seller / Conv+$50k-cash / Cash+Private ALL read Cash Needed $0 —
    # contributions and gaps up to $376,300 move NOTHING; Hard Money
    # reads exactly $6,397 = its points. cash_in below is KEPT as the
    # separate total-cash-in figure (refi remaining_in_deal + the
    # scenarios "Cash in" column) but no longer feeds any Cash Needed /
    # CoC surface. Fix & Flip keeps its published fixture: cash_in
    # (gap-inclusive) IS the flip's Cash Needed (test_fixtures_v15.py:
    # FF Cash Needed $61,684; parity-plan-v16-v17 Senisa fixture list).
    cash_needed = cash_in if strategy == "Fix & Flip" else points_fees + entry_fee
    if funded <= 0:
        funding_status = "Not Funded Yet"
    elif gap < -0.05 * max(uses, 1):
        funding_status = "Over Funded"
    elif gap <= 0:
        funding_status = "Fully Funded"
    else:
        funding_status = "Partially Funded"
    # v2.8 (A10, evening pass 2026-11-03): Override Total Uses. While an
    # override is set, the Funding Snapshot's Total Uses / Remaining
    # Funding Gap / status read the OVERRIDE; everything else (Cash
    # Needed, debt service, CoC, the Details-pane uses) keeps the REAL
    # computed uses — measured: a $400,000 override on the $376,300
    # Cash-only state moved only the snapshot (Gap $23,700, Partially
    # Funded, "$23,700 of funding requirement is not yet covered.") while
    # Cash Contribution, Monthly Debt Service and the UW Summary Cash
    # Needed line did not move.
    uses_override = fnum(inp.get("uses_override"), None)
    if uses_override:
        snap_uses = uses_override
        snap_gap = snap_uses - funded
        if funded <= 0: snap_status = "Not Funded Yet"
        elif snap_gap < -0.05 * max(snap_uses, 1): snap_status = "Over Funded"
        elif snap_gap <= 0: snap_status = "Fully Funded"
        else: snap_status = "Partially Funded"
    else:
        uses_override = None
        snap_uses, snap_gap, snap_status = uses, gap, funding_status
    out = {"strategy": strategy, "arv": arv, "purchase_price": price, "rehab": round(rehab),
           "rehab_calc": round(rehab_calc), "rehab_mode": rehab_mode, "appliances": round(appliances),
           "furnishing": round(furnishing), "conversion": round(conversion),
           "total_project": round(total_project), "holding_cost": round(holding_total),
           "holding_months": round(h_months, 2), "uses": round(uses), "funded": round(funded),
           "gap": round(gap), "cash_in": round(cash_in), "cash_needed": round(cash_needed),
           "financing_costs": round(financing_costs),
           "funding_status": funding_status,
           "uses_override": round(uses_override) if uses_override else None,
           "snap_uses": round(snap_uses), "snap_gap": round(snap_gap), "snap_status": snap_status,
           "debt_service_mo": round(debt_service), "entry_fee": round(entry_fee),
           "cash_contrib": round(cash_equity), "points_fees": round(points_fees),
           "closing_costs": round(closing), "debt_lines": debt_lines,
           "md_payment_mo": round(md_payment), "md_base_mo": round(md_base),
           "md_escrow_mo": round(md_escrow), "md_addons": md_addons}

    if strategy == "Fix & Flip":
        sale = fnum(inp.get("sale_price"), 0) or arv
        comm_pct = fnum(inp.get("commission_pct"), 6) or 0
        close_pct = fnum(inp.get("closing_pct"), 2) or 0
        comm_amt = sale * comm_pct / 100; close_amt = sale * close_pct / 100
        net = sale - comm_amt - close_amt
        # v2.1: financing is a real cost of the flip: buy-side closing, loan points/fees and the interest
        # (or fixed payment) carried while the project runs. Profit used to ignore all of it.
        financing_carry = carry_mo * h_months
        financing_total = financing_costs + financing_carry
        profit = net - total_project - holding_total - financing_total
        # ReiSearch All-in Cost includes the sale costs (commission + seller closing) — Senisa fixture.
        all_in = total_project + holding_total + financing_total + comm_amt + close_amt
        roi = round(profit / cash_in * 100, 1) if cash_in else None
        margin = round(profit / sale * 100, 1) if sale else None
        ann = round(profit / cash_in * 100 * 12 / h_months, 1) if cash_in and h_months else None
        def _flip(sale2, rehab2, hold2):
            n2 = sale2 * (1 - comm_pct / 100 - close_pct / 100)
            proj2 = price + rehab2 + appliances + furnishing
            fin2 = financing_costs + carry_mo * (hold2 / holding_mo if holding_mo else h_months)
            return {"sale": round(sale2), "all_in": round(proj2 + hold2 + fin2 + sale2 * (comm_pct + close_pct) / 100),
                    "net": round(n2 - proj2 - hold2 - fin2)}
        scen = {"Downside (-10%)": _flip(sale * 0.9, rehab, holding_total),
                "Base Case (0%)": _flip(sale, rehab, holding_total),
                "Upside (+10%)": _flip(sale * 1.1, rehab, holding_total),
                # v1.8a: the Fully Booked chip exists on the Flip panel too; a flip
                # has no vacancy, so Fully Booked IS the Base Case there (it must
                # still select/recompute, never sit cosmetic).
                "Fully Booked (0% vacancy)": _flip(sale, rehab, holding_total),
                "Overrun (rehab +20%, +3 mo)": _flip(sale, rehab * 1.2, holding_mo * (h_months + 3))}
        # Flip Deal Score: ReiSearch shows one for flips; scale = profit margin (50%, target 30%)
        # + ROI (50%, target 50%), labeled as our scale in Details.
        def cs(x, t): return max(0.0, min(100.0, (x / t) * 100)) if t else 0.0
        parts = {"Profit margin (50%)": round(0.5 * cs((margin or 0) / 100, 0.30), 1),
                 "ROI on cash (50%)": round(0.5 * cs((roi or 0) / 100, 0.50), 1)}
        fscore = max(0, min(99, round(sum(parts.values()))))
        out.update({"sale_price": round(sale), "commission_pct": comm_pct, "closing_pct": close_pct,
                    "commission_amt": round(comm_amt), "closing_amt": round(close_amt),
                    "net_proceeds": round(net), "all_in": round(all_in), "profit": round(profit),
                    "financing_carry": round(financing_carry), "financing_total": round(financing_total),
                    "roi": roi, "annualized_roi": ann, "margin_pct": margin,
                    "rule70_mao": round(arv * 0.70 - rehab), "scenarios": scen,
                    "score": fscore, "label": _score_label(fscore), "score_parts": parts, "penalties": [],
                    "insight": "The flip loses money at these numbers." if profit < 0 else
                               ("Strongest: " + max(parts, key=lambda k: parts[k]).split(" (")[0] + ".")})
        return out

    # ---------------- rental strategies ----------------
    # v1.5: the $0 taxes/insurance sanity defaults fire ONLY when the deal carries no loan
    # payment. ReiSearch keeps taxes/insurance inside the loan's PITI (e.g. the Senisa
    # subject-to at $1,921/mo PITI) — charging them again in opex would double-count.
    # All-cash deals (Garland) still get the defaults + score penalties, as before.
    penalties, flags = [], []
    taxes_mo = fnum(inp.get("taxes_mo"), 0) or 0
    ins_mo = fnum(inp.get("insurance_mo"), 0) or 0
    # v1.6 standard-numbers rule (Sherif): standard costs AUTO-POPULATE visibly in their
    # fields as editable defaults — the real annual tax from the property record / 12
    # where known, else a labeled estimate. The UI writes the estimate into the field
    # (flagged taxes_est/insurance_est) so fields and totals tie out on screen; the
    # estimate withdraws automatically when a loan payment exists (taxes/insurance then
    # live inside PITI) and never touches a value the user typed themselves.
    prop_tax_mo = 0
    if prop is not None:
        try: prop_tax_mo = round((fnum(prop["tax_amount"], 0) or 0) / 12)
        except Exception: prop_tax_mo = 0
    def _est_flag(k):
        return str(inp.get(k) or "") in ("1", "True", "true")
    taxes_est = ins_est = False
    # v1.7 score root-cause (Garland clean state measured 80 vs ReiSearch's 93):
    # the auto-estimates above used to ALSO subtract 6+6 Deal Score penalty points.
    # That double-punishes: under the standard-numbers rule the estimate is real,
    # labeled data sitting in the field — not missing data. ReiSearch's Garland
    # baseline scores 93 with no such drag. The estimates therefore carry FLAGS
    # (transparency) but no score penalty; the genuinely-empty-expense penalty
    # (no utilities/maintenance at all) below is unchanged.
    if not piti_included and taxes_mo == 0:
        taxes_mo = prop_tax_mo or (round((price or arv) * 0.011 / 12) if (price or arv) else 0)
        taxes_est = taxes_mo > 0
        if taxes_est:
            flags.append("Taxes estimated — verify with county")
    elif not piti_included and _est_flag("taxes_est") and taxes_mo > 0:
        taxes_est = True
        flags.append("Taxes estimated — verify with county")
    if not piti_included and ins_mo == 0:
        ins_mo = 180
        ins_est = True
        flags.append("Insurance estimated")
    elif not piti_included and _est_flag("insurance_est") and ins_mo > 0:
        ins_est = True
        flags.append("Insurance estimated")
    # revenue
    cleaning_income = 0.0
    amenity_rows = 0.0
    if strategy == "Co-Living":
        rows = inp.get("rooms_rows") or []
        def _row_mo(r):
            mo = fnum(r.get("rent_mo"), None)
            return mo if mo is not None else (fnum(r.get("rent_wk"), 0) or 0) * 52 / 12
        gross = sum(_row_mo(r) for r in rows)
        vac_loss = sum(_row_mo(r) * ((fnum(r.get("vacancy"), 15) or 0) / 100) for r in rows)
        rooms_n = len(rows) or int(fnum(inp.get("rooms"), (prop["beds"] if prop else None) or 4) or 4)
        laundry_amt = fnum(inp.get("laundry_amt"), 0) or 0
        laundry_mo = laundry_amt * 52 / 12 if (inp.get("laundry_period") or "wk") == "wk" else laundry_amt
        amenity_rows = sum(fnum(r.get("amenity_mo"), 0) or 0 for r in rows)
        other = amenity_rows + (fnum(inp.get("amenities_mo"), 0) or 0) + (fnum(inp.get("late_fees_mo"), 0) or 0) \
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
    if strategy == "Co-Living":
        # ReiSearch/PadSplit: first-days fee = (weekly rent / 7) x days, per room, per year.
        booking_fee = sum((fnum(r.get("rent_wk"), 0) or 0) / 7 * booking_days for r in rows) / 12
    else:
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
    # v2.3: the old -10 "no utilities/maintenance" score penalty is REMOVED.
    # It was an app-side invention: every ReiSearch state measured in the
    # 2026-11-03 pass has BLANK utilities/maintenance, including its 93-Strong
    # Cash state and its 73-Good Conventional state — ReiSearch demonstrably
    # does not dock the score for blank expense lines. (Mirrors uw.js.)
    noi = egi - opex
    cf = noi - debt_service
    value = arv or price
    metrics = {"cf_per_room": (cf / rooms_n) if rooms_n else 0,
               "dscr": (noi / debt_service) if debt_service else (1.5 if noi > 0 else 0),
               "coc": (cf * 12 / cash_needed) if cash_needed else 0,
               "cap_rate": (noi * 12 / value) if value else 0,
               "breakeven": ((opex + debt_service) / (gross + other)) if (gross + other) else 1.3,
               "downside_cf": (egi * 0.9 - opex - debt_service)}
    # v2.9 score-factor inputs (mirrors uw.js): Cash-on-cash is scored on
    # ReiSearch's Cash Needed (v2.9 definition: points/fees + entry fee
    # only) and is "no data" — excluded from the weighted average — when
    # that is $0 (Pass #2, 8 states). This SUPERSEDES the v2.4 reading
    # (cash_in - cash_equity): on the shipped saved state the old score
    # denominator was the $3,000 closing costs (CoC 603.9%) while the
    # summary showed 4.2% on $428,900 — one definition now feeds both
    # (Hard Money: both -27.7% on $6,397).
    metrics["dscr_val"] = (noi / debt_service) if debt_service else None
    metrics["coc_val"] = (cf * 12 / cash_needed) if cash_needed > 0 else None
    metrics["downside_cf_per_room"] = (metrics["downside_cf"] / rooms_n) if rooms_n else 0
    def _scen(mult, no_vac=False):
        if no_vac and strategy == "Short Term":
            # Fully Booked for STR = 100% occupancy (gross is occupancy-scaled;
            # cleaning income inside `other` stays at the booked level — a
            # documented, conservative simplification).
            _occ = fnum(inp.get("occupancy_pct"), 0) or 0
            e2 = (gross / (_occ / 100) if _occ else gross) + other
        elif no_vac:
            e2 = gross + other
        else:
            e2 = (gross - vac_loss) * mult + other
        noi_s = e2 - opex
        return {"revenue": round(e2), "expenses": round(opex), "loan": round(debt_service),
                "net": round(e2 - opex - debt_service),
                "dscr": round(noi_s / debt_service, 2) if debt_service else (1.5 if noi_s > 0 else 0),
                "cap_rate_pct": round(noi_s * 12 / value * 100, 2) if value else 0,
                "coc_pct": round((e2 - opex - debt_service) * 12 / cash_needed * 100, 1) if cash_needed else None}
    scen = {"Downside (-10%)": _scen(0.9), "Base Case (0%)": _scen(1.0), "Upside (+10%)": _scen(1.1)}
    # v1.8a: Fully Booked exists for EVERY rental strategy (it was Co-Living-only,
    # which left the chip dead on STR/MTR/LTR panels).
    scen["Fully Booked (0% vacancy)"] = _scen(1.0, no_vac=True)
    # v2.4 penalty (measured, Addendum 2; mirrors uw.js): ReiSearch docks 5
    # points when operating expenses are an unrealistically low share of
    # revenue, with the note below. v2.8 (evening pass 2026-11-03): the
    # threshold is now MEASURED to fire at opex/revenue 0%, 3.7%, 5.4%,
    # 7.2%, 10.8%, 14.4% and 18.0%, and ABSENT at Senisa's 35% — the true
    # threshold lies in (18%, 35%]; 20% is the narrowest round bound
    # consistent with every anchor and is flagged as still unverified
    # within that interval (v28 build log). The note renders the ratio
    # rounded to the nearest INTEGER (measured: "only 5%" at 5.4%, "7%"
    # at 7.2%, "11%" at 10.8%, "14%" at 14.4%, "18%" at 18.0%).
    if egi > 0 and opex / egi < 0.20:
        _or = opex / egi * 100
        penalties.append({"points": 5, "reason": "Operating expenses are only " +
                          str(_rhu(_or)) +
                          "% of revenue, which looks unrealistically low."})
    sc = score_rental(metrics, penalties)
    insight = _insight_from_factors(sc["factors"])
    refi_gross = round(0.75 * arv) if arv else 0
    refi_net = max(0, refi_gross - debt_amt)
    out.update({"gross_mo": round(gross), "vacancy_loss_mo": round(vac_loss),
                "other_income_mo": round(other), "cleaning_income_mo": round(cleaning_income),
                "fees_mo": round(fees_cat), "booking_fee_mo": round(booking_fee),
                "platform_fee_mo": round(platform_fee),
                "service_fee_mo": round(service_fee), "mgmt_fee_mo": round(mgmt_fee),
                "egi_mo": round(egi), "opex_mo": round(opex), "opex_cats": opex_cats,
                "taxes_mo": taxes_mo, "insurance_mo": ins_mo,
                "taxes_est": taxes_est, "insurance_est": ins_est, "noi_mo": round(noi),
                "cashflow_mo": round(cf), "cf_per_room": round(metrics["cf_per_room"]),
                "cash_per_room": round(cash_needed / rooms_n) if rooms_n else None,
                "dscr": round(metrics["dscr"], 2) if debt_service else None,   # v2.1: N/A when there is no debt
                "coc_pct": round(metrics["coc"] * 100, 1) if cash_needed else None,
                "cap_rate_pct": round(metrics["cap_rate"] * 100, 2),
                "breakeven_pct": round(metrics["breakeven"] * 100, 1),
                "payback_yrs": round(1 / metrics["coc"], 1) if metrics["coc"] > 0 else None,
                "scenarios": scen, "rooms": rooms_n,
                "refi_proceeds": refi_gross, "remaining_in_deal": round(max(0, cash_in - refi_net)),
                "paydown_y1": round(paydown_y1), "cashflow_y1": round(cf * 12),
                "total_return_y1": round(cf * 12 + paydown_y1),
                "total_roi_y1": round((cf * 12 + paydown_y1) / cash_needed * 100, 1) if cash_needed else None,
                "score": sc["score"], "label": sc["label"], "score_parts": sc["parts"],
                "score_factors": sc["factors"],
                "penalties": sc["penalties"], "flags": flags, "insight": insight,
                "amenity_mo": round(amenity_rows + (fnum(inp.get("amenities_mo"), 0) or 0))})
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
    # funding (v1.5 card fields; canon_inputs already normalized legacy rows)
    st["funding"] = [{"kind": s.get("kind") or "",
                      "type": s.get("type") or "Conventional", "preset": s.get("preset") or s.get("type") or "Conventional",
                      "loan_type": s.get("loan_type") or s.get("type") or "Conventional",
                      "position": s.get("position") or "1st Position",
                      "amount": s.get("amount") or 0, "rate": s.get("rate") or 0,
                      "term_value": s.get("term_value") if s.get("term_value") not in (None,) else "",
                      "term_unit": s.get("term_unit") or "Months",
                      "term_years": s.get("term_years") or "",
                      "amort_value": s.get("amort_value") if s.get("amort_value") not in (None,) else "",
                      "amort_unit": s.get("amort_unit") or "Months",
                      "lender": s.get("lender") or "",
                      "pct": s.get("pct") if s.get("pct") not in (None,) else "",
                      "basis": s.get("basis") or "price",
                      "interest_type": s.get("interest_type") or "simple",
                      "points": s.get("points") or 0, "fees": s.get("fees") or 0,
                      "payment_type": s.get("payment_type") or "amortized",
                      "fixed_payment": s.get("fixed_payment") if s.get("fixed_payment") not in (None,) else "",
                      "taxes_mo": s.get("taxes_mo") or 0, "insurance_mo": s.get("insurance_mo") or 0}
                     for s in (inp.get("funding_stack") or [])]
    # v2.8 (A10): the Total Uses override rides along as panel state.
    st["uses_override"] = g("uses_override", "")
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
            rows = [{"bath": "Shared", "bath_group": "A" if (i // 2) % 2 == 0 else "B",
                     "rent_wk": wk, "rent_mo": round(wk * 52 / 12, 2), "vacancy": 15, "amenity_mo": 0}
                    for i in range(beds)]
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
        fresh = saved_inp is None
        st["vacancy_pct"] = g("vacancy_pct", 15 if strategy == "Co-Living" else 5)
        # v2.1: PadSplit booking fee (10 days) + 8% service fee exist ONLY for Co-Living. They used to be
        # applied to every fresh rental panel (and double-charged Short Term, which has its own platform %).
        st["booking_days"] = g("booking_days", 10 if strategy == "Co-Living" else 0)
        st["service_fee_pct"] = g("service_fee_pct", 8 if strategy == "Co-Living" else 0)
        st["mgmt_fee_pct"] = g("mgmt_fee_pct", 0); st["mgmt_fee_basis"] = g("mgmt_fee_basis", "gross")
        # v1.5: a FRESH panel opens with ReiSearch's PadSplit expense template prefilled
        # (screenshot spec). Saved scenarios/drafts keep exactly what they stored.
        # v2.1: only Co-Living / Short Term get the cleaning + property-manager lines; a Long Term tenant
        # pays utilities, so utilities start at $0 there. All fields stay editable.
        _owner_utils = strategy in ("Co-Living", "Short Term", "Mid Term")
        _hosted = strategy in ("Co-Living", "Short Term")
        template = {"electric_mo": 400 if _owner_utils else 0, "water_mo": 80 if _owner_utils else 0,
                    "gas_mo": 50 if _owner_utils else 0, "internet_mo": 70 if _owner_utils else 0,
                    "cleaning_mo": 120 if _hosted else 0, "landscaping_mo": 100, "pest_mo": 50, "handyman_mo": 150,
                    "property_manager_mo": 300 if _hosted else 0, "turnover_reserve_mo": 0, "additional_insurance_mo": 0}
        for k in ("electric_mo", "water_mo", "gas_mo", "internet_mo", "cleaning_mo",
                  "landscaping_mo", "pest_mo", "handyman_mo", "property_manager_mo",
                  "turnover_reserve_mo", "additional_insurance_mo", "taxes_mo", "insurance_mo", "hoa_mo"):
            st[k] = g(k, template.get(k, 0) if fresh else 0)
        st["taxes_est"] = g("taxes_est", ""); st["insurance_est"] = g("insurance_est", "")
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


def _norm_ptype(raw):
    """HomeHarvest 'style' text -> Market Data filter bucket."""
    t = (raw or "").lower()
    if "condo" in t: return "Condo"
    if "townhouse" in t or "townhome" in t or "town house" in t: return "Townhouse"
    if "multi" in t: return "Multi-Family"
    if t: return "Single Family"
    return None

# ---------------------------------------------------------------- comps pull (HomeHarvest)
def _merge_sold_frame(d, prop, df, radius_mi, subj):
    """Merge one HomeHarvest sold frame into the comps table (ZIP + radius
    post-filter; never address mode). Returns (added, updated, usable)."""
    added = updated = n = 0
    if df is None: return added, updated, n
    for _, r in df.iterrows():
        if str(r.get("zip_code")) != str(prop["zip"]): continue
        lat, lon = r.get("latitude"), r.get("longitude")
        if lat is None or lon is None or (isinstance(lat, float) and math.isnan(lat)): continue
        dist = haversine_mi(prop["lat"], prop["lon"], float(lat), float(lon))
        if dist is None or dist > radius_mi: continue
        addr = _s(r.get("street")).strip()
        if not addr: continue
        price = fnum(r.get("sold_price")); sqft = fnum(r.get("sqft"))
        if not price: continue  # a "sold" row with no price is not a usable comp
        n += 1
        vals = dict(kind="sold", address=addr, city=_s(r.get("city")) or prop["city"],
                    zip=_s(r.get("zip_code")), price=price,
                    ppsf=(price / sqft) if price and sqft else fnum(r.get("price_per_sqft")),
                    beds=fnum(r.get("beds")), baths=fnum(r.get("full_baths")), sqft=sqft,
                    year_built=int(r["year_built"]) if fnum(r.get("year_built")) else None,
                    event_date=_s(r.get("last_sold_date"))[:10] or None,
                    dom=fnum(r.get("days_on_mls")), lat=float(lat), lon=float(lon), distance_mi=dist,
                    lot_sqft=fnum(r.get("lot_sqft")),
                    ptype=_norm_ptype(_s(r.get("style")) or _s(r.get("property_type"))),
                    source="HomeHarvest (Realtor.com)", source_url=_s(r.get("property_url")),
                    photo_url=_s(r.get("primary_photo")))
        vals["match_score"] = match_score(subj, {"distance_mi": dist, "sqft": sqft, "beds": vals["beds"],
                                                 "baths": vals["baths"], "year_built": vals["year_built"],
                                                 "lot_sqft": vals["lot_sqft"]})
        ex = d.execute("SELECT id FROM comps WHERE property_id=? AND kind='sold' AND address=? AND price=?",
                       (prop["id"], addr, price)).fetchone()
        cols = ",".join(vals.keys()); ph = ",".join("?" * len(vals))
        if ex:
            d.execute(f"UPDATE comps SET {','.join(k+'=?' for k in vals)} WHERE id=?", (*vals.values(), ex["id"])); updated += 1
        else:
            d.execute(f"INSERT INTO comps(property_id,{cols}) VALUES(?,{ph})", (prop["id"], *vals.values())); added += 1
    return added, updated, n

def _merge_rent_frame(d, prop, df, radius_mi, subj):
    """Merge one HomeHarvest for_rent frame. Returns (added, usable)."""
    added = n = 0
    if df is None: return added, n
    for _, r in df.iterrows():
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
                    lot_sqft=fnum(r.get("lot_sqft")),
                    ptype=_norm_ptype(_s(r.get("style")) or _s(r.get("property_type"))),
                    source="HomeHarvest (Realtor.com rentals)", source_url=_s(r.get("property_url")),
                    photo_url=_s(r.get("primary_photo")))
        vals["match_score"] = match_score(subj, {"distance_mi": dist, "sqft": sqft, "beds": vals["beds"],
                                                 "baths": vals["baths"], "year_built": vals["year_built"],
                                                 "lot_sqft": vals["lot_sqft"]})
        ex = d.execute("SELECT id FROM comps WHERE property_id=? AND kind='rent' AND address=? AND price=?",
                       (prop["id"], addr, price)).fetchone()
        cols = ",".join(vals.keys()); ph = ",".join("?" * len(vals))
        if ex:
            d.execute(f"UPDATE comps SET {','.join(k+'=?' for k in vals)} WHERE id=?", (*vals.values(), ex["id"]))
        else:
            d.execute(f"INSERT INTO comps(property_id,{cols}) VALUES(?,{ph})", (prop["id"], *vals.values())); added += 1
        n += 1
    return added, n

def comp_tier(subj, c):
    """Sherif's match-first tiers for SOLD comps (2026-11-02, P0-1).
    Tier 1: beds == subject AND baths == subject AND sqft within +/-20%.
    Tier 2: (beds within +/-1 OR baths within +/-1) AND sqft within +/-35%.
    Tier 3: everything else. A comp with missing specs is never promoted on a
    guess — unknown specs land in Tier 3 (not found != matched)."""
    sb, sba, ssq = subj.get("beds"), subj.get("baths"), subj.get("sqft")
    b, ba, sq = c.get("beds"), c.get("baths"), c.get("sqft")
    if sb is None or sba is None or not ssq: return 3
    if b == sb and ba == sba and sq and ssq * 0.8 <= sq <= ssq * 1.2: return 1
    if sq and ssq * 0.65 <= sq <= ssq * 1.35 and \
            ((b is not None and abs(b - sb) <= 1) or (ba is not None and abs(ba - sba) <= 1)): return 2
    return 3

def rent_match_group(subj, c):
    """Rental match group for the LTR / Co-Living views (P0-2):
    0 = exact beds+baths match, 1 = beds or baths within +/-1 (near),
    2 = the rest or unknown specs."""
    sb, sba = subj.get("beds"), subj.get("baths")
    b, ba = c.get("beds"), c.get("baths")
    if sb is None or sba is None: return 2
    if b == sb and ba == sba: return 0
    if (b is not None and abs(b - sb) <= 1) or (ba is not None and abs(ba - sba) <= 1): return 1
    return 2

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
    subj = {"sqft": prop["sqft"], "beds": prop["beds"], "baths": prop["baths"], "year_built": prop["year_built"], "lot_sqft": prop["lot_sqft"]}
    added, updated, n_sold = _merge_sold_frame(d, prop, sold, radius_mi, subj)
    # rentals (LTR / Co-Living / Affordable views all read this set)
    n_rent = 0; rent_df = None
    try:
        rent_df = scrape_property(location=prop["zip"], listing_type="for_rent", past_days=90)
        a2, n_rent = _merge_rent_frame(d, prop, rent_df, radius_mi, subj)
        added += a2
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
    if srow is None:
        # v1.6 (item N): DEEP subject lookup — the subject's own last sale is often
        # older than the comp window, so the frames above miss it and photos/specs
        # never populate. One wider sold scrape (10 years, ZIP mode) finds the
        # subject's own listing row: photos + blank specs come from it.
        try:
            deep = scrape_property(location=prop["zip"], listing_type="sold", past_days=3650)
            srow = find_subject_row(deep, prop)
        except Exception as e:
            warnings.append(f"deep subject lookup failed ({e})")
    if srow is None:
        # v2.0 honest state: no listing row for this address in ANY free frame
        # (including the 10-year deep sold scrape) — specs/photos stay blank
        # because no source has them, and the job message says so instead of
        # the property silently opening with empty specs.
        warnings.append("no listing row found for this address in free sources (HomeHarvest) — "
                        "specs and photos could not be auto-filled")
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
    # --- P0-1/P0-2 widening (Sherif 2026-11-02): the match-first views need a
    # real matched set. If the stored sold set holds <10 Tier-1 matches, run ONE
    # widened sold scrape (2x radius, min 2 mi; 730 days) and merge it; if the
    # rental set is thin (<11 stored or <5 exact/near matches), run ONE widened
    # rental scrape (2x radius, min 2 mi; past_days 365). Widening only ADDS
    # real listings — views never pad with mismatches. ---
    prop2 = d.execute("SELECT * FROM properties WHERE id=?", (prop["id"],)).fetchone()
    subj2 = {"sqft": prop2["sqft"], "beds": prop2["beds"], "baths": prop2["baths"],
             "year_built": prop2["year_built"], "lot_sqft": prop2["lot_sqft"]}
    def _tier1_count():
        rows = d.execute("SELECT beds,baths,sqft FROM comps WHERE property_id=? AND kind='sold' AND excluded=0",
                         (prop["id"],)).fetchall()
        return sum(1 for r in rows if comp_tier(subj2, dict(r)) == 1)
    t1 = _tier1_count()
    if t1 < 10:
        try:
            wide = scrape_property(location=prop["zip"], listing_type="sold", past_days=730)
            a3, u3, _ = _merge_sold_frame(d, prop2, wide, max(radius_mi * 2, 2.0), subj2)
            added += a3; updated += u3; d.commit()
            t1b = _tier1_count()
            warnings.append(f"sold set widened to {max(radius_mi * 2, 2.0):g} mi / 730 days to find Tier-1 matches ({t1} -> {t1b} found)")
        except Exception as e:
            warnings.append(f"sold widening scrape failed ({e})")
    def _rent_match_counts():
        rows = d.execute("SELECT beds,baths FROM comps WHERE property_id=? AND kind='rent'",
                         (prop["id"],)).fetchall()
        ex_ = sum(1 for r in rows if rent_match_group(subj2, dict(r)) == 0)
        near = sum(1 for r in rows if rent_match_group(subj2, dict(r)) == 1)
        return len(rows), ex_, near
    rn, rex, rnear = _rent_match_counts()
    if rn < 11 or (rex + rnear) < 5:
        try:
            wide_r = scrape_property(location=prop["zip"], listing_type="for_rent", past_days=365)
            a4, _ = _merge_rent_frame(d, prop2, wide_r, max(radius_mi * 2, 2.0), subj2)
            added += a4; d.commit()
            rn2, rex2, rnear2 = _rent_match_counts()
            warnings.append(f"rental set widened to {max(radius_mi * 2, 2.0):g} mi / 365 days for matched rentals ({rn} -> {rn2} listings; {rex2} exact + {rnear2} near beds/baths matches)")
        except Exception as e:
            warnings.append(f"rental widening scrape failed ({e})")
    # --- Auto-pin (only if user pinned none): top 3 by TIER first (P0-1: Tier 1
    # only when >=3 exist), then match score, then distance. ---
    pinned_now = d.execute("SELECT COUNT(*) c FROM comps WHERE property_id=? AND kind='sold' AND pinned=1",
                           (prop["id"],)).fetchone()["c"]
    if not pinned_now:
        rows = [dict(r) for r in d.execute(
            "SELECT * FROM comps WHERE property_id=? AND kind='sold' AND excluded=0", (prop["id"],)).fetchall()]
        rows.sort(key=lambda c: (comp_tier(subj2, c), -(c["match_score"] or 0),
                                 c["distance_mi"] if c["distance_mi"] is not None else 9e9))
        for t in rows[:3]: d.execute("UPDATE comps SET pinned=1 WHERE id=?", (t["id"],))
        d.commit()
    total_sold = d.execute("SELECT COUNT(*) c FROM comps WHERE property_id=? AND kind='sold' AND excluded=0",
                           (prop["id"],)).fetchone()["c"]
    total_rent = d.execute("SELECT COUNT(*) c FROM comps WHERE property_id=? AND kind='rent'",
                           (prop["id"],)).fetchone()["c"]
    return {"added": added, "updated": updated, "sold": total_sold, "rentals": total_rent,
            "sold_new": n_sold, "photos": photos_found, "warnings": warnings, "date": today}

ACS_YEAR = 2023
STATE_FIPS = {"AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09",
              "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17",
              "IN": "18", "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24",
              "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29", "MT": "30", "NE": "31",
              "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38",
              "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
              "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53", "WV": "54",
              "WI": "55", "WY": "56"}

def _census_key():
    """Census DATA API key: env CENSUS_API_KEY first (v1.6 — the data API now requires
    a key and 302s to a 'Missing Key' page without one), then the settings store."""
    return os.environ.get("CENSUS_API_KEY") or get_setting("census_key") or ""

def _acs_fetch(year, varlist, geo_params):
    """One ACS 5-year query -> {var: value} dict, or None on any failure.
    The Census *geocoder* is keyless; the *data* API needs the key when configured."""
    key = _census_key()
    for path in (f"https://api.census.gov/data/{year}/acs/acs5", f"https://api.census.gov/data/{year}/acs5"):
        try:
            p = {"get": varlist, "for": geo_params}
            if key: p["key"] = key
            r = requests.get(path, params=p, timeout=10)
            rows = r.json()
            if isinstance(rows, list) and len(rows) >= 2:
                return dict(zip(rows[0], rows[1]))
        except Exception:
            continue
    return None

def _acs_num(d, k):
    x = fnum((d or {}).get(k))
    return x if x is not None and x > 0 else None

def _income_buckets(d):
    """B19001 16 brackets -> ReiSearch's 4 buckets (<25k / 25-50k / 50-100k / >100k)."""
    if not d: return []
    total = _acs_num(d, "B19001_001E")
    if not total: return []
    groups = [("<$25k", (2, 3, 4, 5)), ("$25–50k", (6, 7, 8, 9, 10)),
              ("$50–100k", (11, 12, 13)), (">$100k", (14, 15, 16, 17))]
    out = []
    for lab, idxs in groups:
        c = sum(_acs_num(d, f"B19001_{i:03d}E") or 0 for i in idxs)
        out.append({"label": lab, "count": int(c), "pct": round(c / total * 100, 1)})
    return out

_RACE_DEFS = [("White", "B02001_002E", "#2563eb"), ("Hispanic or Latino", "B03003_003E", "#d97706"),
              ("Black", "B02001_003E", "#16a34a"), ("Asian", "B02001_005E", "#7c3aed"),
              ("Two or More", "B02001_008E", "#db2777"), ("Other", "B02001_007E", "#64748b"),
              ("Native American", "B02001_004E", "#0891b2"), ("Native Hawaiian / Pacific Isl.", "B02001_006E", "#94a3b8")]

def _race_table(d, pop):
    if not d or not pop: return []
    out = []
    for lab, var, color in _RACE_DEFS:
        c = _acs_num(d, var)
        if c is None: continue
        out.append({"label": lab, "count": int(c), "pct": round(c / pop * 100, 1), "color": color})
    out.sort(key=lambda x: -x["count"])
    acc = 0.0  # conic-gradient stops for the donut
    for e in out:
        e["from"] = round(acc, 1); acc += e["pct"]; e["to"] = round(min(acc, 100.0), 1)
    return out

def _age_pyramid(d):
    """B01001 sex-by-age cells -> 5-year bands (male left / female right)."""
    if not d: return []
    def cell(sex_off, i): return _acs_num(d, f"B01001_{sex_off + i:03d}E") or 0
    bands = [("0–4", (3,)), ("5–9", (4,)), ("10–14", (5,)), ("15–19", (6, 7)),
             ("20–24", (8, 9, 10)), ("25–29", (11,)), ("30–34", (12,)), ("35–39", (13,)),
             ("40–44", (14,)), ("45–49", (15,)), ("50–54", (16,)), ("55–59", (17,)),
             ("60–64", (18, 19)), ("65–69", (20, 21)), ("70–74", (22,)), ("75–79", (23,)),
             ("80+", (24, 25))]
    out = []
    for lab, cells in bands:
        m = sum(cell(0, c) for c in cells); f = sum(cell(24, c) for c in cells)
        out.append({"band": lab, "male": int(m), "female": int(f)})
    mx = max([max(e["male"], e["female"]) for e in out] + [1])
    for e in out:
        e["male_w"] = round(e["male"] / mx * 100, 1); e["female_w"] = round(e["female"] / mx * 100, 1)
    return out

def _block_from_vals(v, name=None):
    """Dashboard block from a flat {VAR_E: value} dict (either Census source)."""
    if not v: return None
    pop = _acs_num(v, "B01003_001E")
    if pop is None: return None
    own, rent = _acs_num(v, "B25003_002E") or 0, _acs_num(v, "B25003_003E") or 0
    un, lf = _acs_num(v, "B23025_005E"), _acs_num(v, "B23025_003E")
    blk = {"name": v.get("NAME") or name, "population": int(pop) if pop is not None else None,
           "median_home_value": _acs_num(v, "B25077_001E"), "median_rent": _acs_num(v, "B25064_001E"),
           "median_income": _acs_num(v, "B19013_001E"),
           "owner_pct": round(own / (own + rent) * 100, 1) if (own + rent) else None,
           "renter_pct": round(rent / (own + rent) * 100, 1) if (own + rent) else None,
           "unemployment_pct": round(un / lf * 100, 2) if un and lf else None}
    blk["income_buckets"] = _income_buckets(v)
    blk["race"] = _race_table(v, pop)
    blk["pyramid"] = _age_pyramid(v)
    return blk

def _geo_block(geo_params, year=ACS_YEAR):
    """Direct api.census.gov path (OPTIONAL ALTERNATE — needs CENSUS_API_KEY)."""
    core = _acs_fetch(year, "NAME,B01003_001E,B25077_001E,B25064_001E,B19013_001E,"
                            "B25003_002E,B25003_003E,B23025_003E,B23025_005E", geo_params)
    if not core: return None
    merged = dict(core)
    for varlist in (",".join(["B19001_001E"] + [f"B19001_{i:03d}E" for i in range(2, 18)]),
                    "B02001_001E,B02001_002E,B02001_003E,B02001_004E,B02001_005E,"
                    "B02001_006E,B02001_007E,B02001_008E,B03003_003E",
                    ",".join(["B01001_001E"] + [f"B01001_{i:03d}E" for i in range(3, 26)]
                             + [f"B01001_{i:03d}E" for i in range(27, 50)])):
        d = _acs_fetch(year, varlist, geo_params)
        if d: merged.update(d)
    return _block_from_vals(merged)

# ------------------------------------------------ Census Reporter (PRIMARY, keyless)
CR_TABLES = "B01003,B01001,B19001,B19013,B02001,B03003,B25003,B25064,B25077,B23025"
# v2.1: PIN the Census Reporter release. "latest" is NOT one vintage across
# scopes: it resolves to ACS 2024 5-year for ZCTAs but ACS 2024 1-year for
# places (verified live 2026-11-02: Tampa place "latest" = pop 414,575 /
# MHI $84,114 / MHV $494,400, while acs2024_5yr = 401,618 / $75,475 /
# $420,400). Pinning acs2024_5yr keeps ZIP and city like-for-like under the
# one "ACS 2024 5-year" label the page prints. (acs2023_5yr is NOT served by
# Census Reporter: it 404s "isn't supported".)
CR_RELEASE = "acs2024_5yr"

def _cr_fetch(geo_id, release=CR_RELEASE, tables=CR_TABLES):
    """Census Reporter API (NO key): one call -> flat {B01003_001E: value} dict in
    api.census.gov naming + the release label. Pinned-release responses are cached
    server-side per geography in settings (30 days) so repeat opens are instant."""
    ck = f"crvals_{release}_{geo_id}"
    if release == CR_RELEASE:
        try:
            cached = json.loads(get_setting(ck) or "{}")
            if cached.get("vals") and cached.get("date") and \
                    (datetime.date.today() - datetime.date.fromisoformat(cached["date"])).days < 30:
                return cached["vals"], cached.get("rel") or "ACS 5-year"
        except Exception:
            pass
    r = requests.get(f"https://api.censusreporter.org/1.0/data/show/{release}",
                     params={"table_ids": tables, "geo_ids": geo_id}, timeout=60,
                     headers={"User-Agent": "PropertyDealAnalyzer/1.6 (Census Reporter client)"})
    r.raise_for_status()
    j = r.json()
    rel = ((j.get("release") or {}).get("name") or "ACS 5-year").replace("_", " ")
    vals = {}
    for t, blk in ((j.get("data") or {}).get(geo_id) or {}).items():
        for code, v in ((blk or {}).get("estimate") or {}).items():
            m = re.match(r"^(B\d{5})(\d{3})$", str(code))
            if m: vals[f"{m.group(1)}_{m.group(2)}E"] = v
    if vals and release == CR_RELEASE:
        try: set_setting(ck, json.dumps({"vals": vals, "rel": rel,
                                         "date": datetime.date.today().isoformat()}))
        except Exception: pass
    return vals, rel

def _geo_block_any(geo_id, name, direct_params):
    """Dashboard block for one geography: Census Reporter first (keyless);
    the keyed direct Census API only as an optional alternate. Returns (block, source)."""
    try:
        vals, rel = _cr_fetch(geo_id)
    except Exception:
        vals, rel = {}, None
    if vals:
        blk = _block_from_vals(vals, name)
        if blk: return blk, f"US Census {rel} via Census Reporter"
    if direct_params and _census_key():
        blk = _geo_block(direct_params)
        if blk: return blk, "US Census ACS 5-year (api.census.gov, keyed alternate)"
    return None, None

def _growth_series(pts):
    pts.sort(key=lambda x: x["year"])
    for i, pt in enumerate(pts):
        if i and pts[i - 1]["pop"]:
            pt["growth"] = round((pt["pop"] - pts[i - 1]["pop"]) / pts[i - 1]["pop"] * 100, 2)
        else: pt["growth"] = None
        prev_g = pts[i - 1].get("growth") if i else None
        pt["growth_change"] = round(pt["growth"] - prev_g, 2) if (pt["growth"] is not None and prev_g is not None) else None
    return pts

def _pop_trend(zip_code):
    """Population trend via the direct API (optional alternate path)."""
    pts = []
    for year in range(2011, ACS_YEAR + 1):
        d = _acs_fetch(year, "B01003_001E", f"zip code tabulation area:{zip_code}")
        pop = _acs_num(d, "B01003_001E") if d else None
        if pop: pts.append({"year": year, "pop": int(pop)})
    return _growth_series(pts)

def _pop_trend_cr(geo_id):
    """Population trend from Census Reporter releases: ACS 2014 / 2019 5-year + pinned CR_RELEASE."""
    pts = []
    for release, yr in (("acs2014_5yr", 2014), ("acs2019_5yr", 2019)):
        try:
            vals, _r = _cr_fetch(geo_id, release=release, tables="B01003")
            pop = _acs_num(vals, "B01003_001E")
            if pop: pts.append({"year": yr, "pop": int(pop)})
        except Exception:
            continue
    try:
        vals, rel = _cr_fetch(geo_id, tables="B01003")
        pop = _acs_num(vals, "B01003_001E")
        m = re.search(r"(20\d\d)", rel or "")
        if pop: pts.append({"year": int(m.group(1)) if m else 2024, "pop": int(pop)})
    except Exception:
        pass
    return _growth_series(pts)

def census_geographies(address):
    """Keyless Census Geocoder: address -> {state_fips, place_fips, place_name, tract}.
    Replaces the old hard-coded FL place map (audit gap 3.10)."""
    if not address: return None
    try:
        r = requests.get("https://geocoding.census.gov/geocoder/geographies/onelineaddress",
                         params={"address": address, "benchmark": "Public_AR_2020",
                                 "vintage": "Current_Current", "format": "json"}, timeout=10)
        matches = ((r.json().get("result") or {}).get("addressMatches")) or []
        if not matches: return None
        geos = matches[0].get("geographies") or {}
        def first(layer):
            v = geos.get(layer) or []
            return v[0] if v else {}
        st = first("States")
        pl = first("Incorporated Places") or first("Census Designated Places")
        return {"state_fips": st.get("STATE"), "place_fips": pl.get("PLACE"),
                "place_name": pl.get("NAME"), "tract": first("Census Tracts").get("TRACT"),
                "county": first("Counties").get("NAME")}
    except Exception:
        return None

def _place_key(name):
    """Normalize a Census place name for the bundled Gazetteer table:
    lowercase, periods dropped, trailing city/town/village/CDP suffix dropped,
    'st' -> 'saint'. Byte-identical to the generator that built _FL_PLACES."""
    toks = str(name or "").lower().replace(".", " ").split()
    while toks and toks[-1] in ("city", "town", "village", "cdp"):
        toks.pop()
    return " ".join("saint" if t == "st" else t for t in toks)

# Bundled Florida Census place FIPS (place code only; state FIPS 12 prepends at
# use). Source: US Census Bureau 2024 Gazetteer place file (public, official) —
# embedded so city-scope demographics resolve OFFLINE and deterministically:
# the keyless Census Geocoder is unreachable from some networks and the
# place-list fallback needs a Census API key this app does not require.
_FL_PLACES = {
    "acacia villas": "00075",
    "alachua": "00375",
    "alafaya": "00410",
    "alford": "00625",
    "allentown": "00800",
    "altamonte springs": "00950",
    "altha": "01000",
    "altoona": "01050",
    "alturas": "01100",
    "alva": "01125",
    "andrews": "01353",
    "anna maria": "01475",
    "apalachicola": "01625",
    "apollo beach": "01675",
    "apopka": "01700",
    "arcadia": "01750",
    "archer": "01775",
    "aripeka": "01875",
    "asbury lake": "02187",
    "astatula": "02250",
    "astor": "02275",
    "atlantic beach": "02400",
    "atlantis": "02500",
    "auburndale": "02550",
    "aucilla": "02575",
    "avalon": "02660",
    "ave maria": "02677",
    "aventura": "02681",
    "avon park": "02750",
    "azalea park": "02850",
    "babson park": "02900",
    "bagdad": "02925",
    "bal harbour": "03275",
    "baldwin": "03250",
    "balm": "03375",
    "bardmoor": "03480",
    "bartow": "03675",
    "bascom": "03725",
    "bay harbor islands": "03975",
    "bay hill": "04070",
    "bay lake": "04150",
    "bay pines": "04200",
    "bayonet point": "04162",
    "bayport": "04250",
    "bayshore gardens": "04350",
    "beacon square": "04650",
    "bear creek": "04735",
    "bee ridge": "04925",
    "bell": "04975",
    "bellair-meadowbrook terrace": "05025",
    "belle glade": "05200",
    "belle isle": "05300",
    "belleair": "05075",
    "belleair beach": "05100",
    "belleair bluffs": "05125",
    "belleair shore": "05150",
    "belleview": "05375",
    "bellview": "05462",
    "berkshire lakes": "05806",
    "berrydale": "05825",
    "beverly beach": "06100",
    "beverly hills": "06125",
    "big coppitt key": "06350",
    "big pine key": "06425",
    "biscayne gardens": "06575",
    "biscayne park": "06600",
    "bithlo": "06625",
    "black diamond": "06667",
    "black hammock": "06670",
    "bloomingdale": "06875",
    "blountstown": "06925",
    "boca raton": "07300",
    "bokeelia": "07375",
    "bonifay": "07450",
    "bonita springs": "07525",
    "boulevard gardens": "07687",
    "bowling green": "07775",
    "boynton beach": "07875",
    "bradenton": "07950",
    "bradenton beach": "07975",
    "bradfordville": "08075",
    "bradley junction": "08125",
    "brandon": "08150",
    "branford": "08175",
    "brent": "08300",
    "briny breezes": "08575",
    "bristol": "08600",
    "broadview park": "08650",
    "bronson": "08700",
    "brooker": "08725",
    "brookridge": "08762",
    "brooksville": "08800",
    "brownsdale": "08925",
    "brownsville": "09000",
    "buckhead ridge": "09315",
    "buckingham": "09350",
    "buenaventura lakes": "09415",
    "bunnell": "09550",
    "burnt store marina": "09612",
    "bushnell": "09625",
    "butler beach": "09630",
    "cabana colony": "09655",
    "callahan": "09700",
    "callaway": "09725",
    "campbell": "09875",
    "campbellton": "09900",
    "canal point": "10100",
    "cape canaveral": "10250",
    "cape coral": "10275",
    "capitola": "10350",
    "captiva": "10425",
    "carrabelle": "10725",
    "carrollwood": "10825",
    "caryville": "10975",
    "casselberry": "11050",
    "cedar grove": "11150",
    "cedar key": "11225",
    "celebration": "11285",
    "center hill": "11325",
    "century": "11362",
    "chaires": "11525",
    "charleston park": "11597",
    "charlotte harbor": "11625",
    "charlotte park": "11650",
    "chattahoochee": "11800",
    "cheval": "11912",
    "chiefland": "11925",
    "chipley": "11975",
    "chokoloskee": "12150",
    "christmas": "12250",
    "chuluota": "12275",
    "chumuckla": "12300",
    "cinco bayou": "12325",
    "citrus hills": "12412",
    "citrus park": "12425",
    "citrus springs": "12450",
    "clarcona": "12675",
    "clearwater": "12875",
    "clermont": "12925",
    "cleveland": "12950",
    "clewiston": "13000",
    "cloud lake": "13050",
    "cobbtown": "13125",
    "cocoa": "13150",
    "cocoa beach": "13175",
    "cocoa west": "13225",
    "coconut creek": "13275",
    "coleman": "13400",
    "combee settlement": "13775",
    "connerton": "13965",
    "conway": "14050",
    "cooper": "14125",
    "coral gables": "14250",
    "coral springs": "14400",
    "coral terrace": "14412",
    "cortez": "14700",
    "cottondale": "14850",
    "country club": "14895",
    "country walk": "15055",
    "crawfordville": "15300",
    "crescent": "15375",
    "crescent beach": "15325",
    "crestview": "15475",
    "crooked lake park": "15515",
    "cross": "15575",
    "crystal lake": "15725",
    "crystal river": "15775",
    "crystal springs": "15800",
    "cudjoe key": "15862",
    "cutler bay": "15968",
    "cypress gardens": "16050",
    "cypress lake": "16062",
    "cypress quarters": "16100",
    "dade": "16125",
    "dade city north": "16175",
    "dania beach": "16335",
    "davenport": "16450",
    "davie": "16475",
    "day": "16500",
    "daytona beach": "16525",
    "daytona beach shores": "16550",
    "de leon springs": "16975",
    "debary": "16675",
    "deerfield beach": "16725",
    "defuniak springs": "16800",
    "deland": "16875",
    "deland southwest": "16937",
    "delray beach": "17100",
    "deltona": "17200",
    "desoto acres": "17273",
    "desoto lakes": "17300",
    "destin": "17325",
    "dickerson": "17450",
    "dixonville": "17710",
    "doctor phillips": "17725",
    "doral": "17935",
    "dover": "18075",
    "duck key": "18425",
    "dundee": "18550",
    "dunedin": "18575",
    "dunnellon": "18675",
    "eagle lake": "18875",
    "east bronson": "19025",
    "east lake": "19206",
    "east lake-orient park": "19212",
    "east milton": "19275",
    "east palatka": "19350",
    "east williston": "19575",
    "eastpoint": "19400",
    "eatonville": "19650",
    "ebro": "19725",
    "edgewater": "19825",
    "edgewood": "19900",
    "eglin afb": "20050",
    "egypt lake-leto": "20108",
    "el portal": "20650",
    "elfers": "20275",
    "ellenton": "20375",
    "englewood": "20825",
    "ensley": "20925",
    "estero": "21150",
    "esto": "21250",
    "eustis": "21350",
    "everglades": "21425",
    "fairview shores": "21750",
    "fanning springs": "21850",
    "feather sound": "21945",
    "fellsmere": "22100",
    "fern park": "22250",
    "fernandina beach": "22175",
    "ferndale": "22225",
    "ferry pass": "22275",
    "fidelis": "22325",
    "fish hawk": "22387",
    "fisher island": "22375",
    "five points": "22475",
    "flagler beach": "22550",
    "flagler estates": "22560",
    "fleming island": "22660",
    "floral": "22775",
    "florida": "22975",
    "florida gulf coast university": "23005",
    "florida ridge": "23050",
    "floridatown": "23125",
    "forest": "23375",
    "fort braden": "23680",
    "fort denaud": "23730",
    "fort green": "23850",
    "fort green springs": "23875",
    "fort lauderdale": "24000",
    "fort meade": "24100",
    "fort myers": "24125",
    "fort myers beach": "24150",
    "fort myers shores": "24175",
    "fort pierce": "24300",
    "fort pierce north": "24337",
    "fort pierce south": "24387",
    "fort walton beach": "24475",
    "fort white": "24500",
    "fountainebleau": "24562",
    "four corners": "24581",
    "franklin park": "24710",
    "freeport": "24825",
    "frostproof": "24900",
    "fruit cove": "24925",
    "fruitland park": "24975",
    "fruitville": "25000",
    "fuller heights": "25025",
    "fussels corner": "25125",
    "gainesville": "25175",
    "garcon point": "25392",
    "garden grove": "25475",
    "gardner": "25550",
    "gateway": "25655",
    "geneva": "25750",
    "gibsonton": "25900",
    "gifford": "25925",
    "gladeview": "25987",
    "glen ridge": "26050",
    "glen saint mary": "26075",
    "glencoe": "26000",
    "glenvar heights": "26100",
    "golden beach": "26250",
    "golden gate": "26300",
    "goldenrod": "26475",
    "golf": "26550",
    "gonzalez": "26700",
    "goodland": "26775",
    "gotha": "26900",
    "goulding": "26925",
    "goulds": "26950",
    "graceville": "27000",
    "grand ridge": "27175",
    "grant-valkaria": "27256",
    "green cove springs": "27400",
    "greenacres": "27322",
    "greenbriar": "27360",
    "greensboro": "27550",
    "greenville": "27575",
    "greenwood": "27600",
    "grenelefe": "27635",
    "gretna": "27650",
    "grove": "27775",
    "groveland": "27800",
    "gulf breeze": "28000",
    "gulf gate": "28045",
    "gulf stream": "28275",
    "gulfport": "28175",
    "gun club estates": "28305",
    "haines": "28400",
    "hallandale beach": "28452",
    "hampton": "28575",
    "harbor bluffs": "28650",
    "harbour heights": "28800",
    "harlem": "28925",
    "harlem heights": "28937",
    "harold": "29000",
    "hastings": "29100",
    "havana": "29150",
    "haverhill": "29200",
    "hawthorne": "29275",
    "heathrow": "29320",
    "heritage bay": "29381",
    "heritage pines": "29385",
    "hernando": "29425",
    "hernando beach": "29437",
    "hialeah": "30000",
    "hialeah gardens": "30025",
    "high point": "30494",
    "high springs": "30525",
    "highland": "30225",
    "highland beach": "30200",
    "highland park": "30325",
    "hill 'n dale": "30800",
    "hillcrest heights": "30700",
    "hilliard": "30750",
    "hillsboro beach": "30850",
    "hillsboro pines": "30858",
    "hobe sound": "30975",
    "holden heights": "31025",
    "holden lakes": "31030",
    "holiday": "31075",
    "holley": "31275",
    "holly hill": "31350",
    "hollywood": "32000",
    "holmes beach": "32150",
    "homeland": "32250",
    "homestead": "32275",
    "homestead base": "32325",
    "homosassa": "32375",
    "homosassa springs": "32400",
    "horizon west": "32610",
    "horseshoe beach": "32650",
    "hosford": "32675",
    "howey-in-the-hills": "32775",
    "hudson": "32825",
    "hunters creek": "32967",
    "hurlburt field": "32975",
    "hutchinson island south": "32993",
    "hypoluxo": "33150",
    "immokalee": "33250",
    "indialantic": "33375",
    "indian creek": "33425",
    "indian harbour beach": "33450",
    "indian lake estates": "33500",
    "indian river estates": "33585",
    "indian river shores": "33600",
    "indian rocks beach": "33625",
    "indian shores": "33675",
    "indiantown": "33700",
    "inglis": "33800",
    "interlachen": "33900",
    "inverness": "33950",
    "inverness highlands north": "33958",
    "inverness highlands south": "33966",
    "inwood": "34000",
    "iona": "34012",
    "islamorada, village of islands": "34132",
    "island walk": "34185",
    "istachatta": "34275",
    "ives estates": "34400",
    "jacksonville": "35000",
    "jacksonville beach": "35050",
    "jacob": "35200",
    "jan phyl": "35300",
    "jasmine estates": "35350",
    "jasper": "35375",
    "jay": "35425",
    "jennings": "35525",
    "jensen beach": "35550",
    "june park": "35800",
    "juno beach": "35850",
    "juno ridge": "35862",
    "jupiter": "35875",
    "jupiter farms": "35890",
    "jupiter inlet colony": "35900",
    "jupiter island": "35925",
    "kathleen": "35950",
    "kendale lakes": "36062",
    "kendall": "36100",
    "kendall west": "36121",
    "kenneth": "36175",
    "kensington park": "36200",
    "kenwood estates": "36220",
    "key biscayne": "36300",
    "key colony beach": "36325",
    "key largo": "36375",
    "key vista": "36535",
    "key west": "36550",
    "keystone": "36462",
    "keystone heights": "36475",
    "kissimmee": "36950",
    "la crosse": "37300",
    "labelle": "37225",
    "lacoochee": "37275",
    "lady lake": "37375",
    "laguna beach": "37500",
    "lake": "37775",
    "lake alfred": "37525",
    "lake belvedere estates": "37542",
    "lake buena vista": "37625",
    "lake butler": "37648",
    "lake clarke shores": "37800",
    "lake hamilton": "37975",
    "lake harbor": "38000",
    "lake hart": "38012",
    "lake helen": "38025",
    "lake kathryn": "38162",
    "lake kerr": "38238",
    "lake lindsey": "38275",
    "lake lorraine": "38287",
    "lake mack-forest hills": "38332",
    "lake magdalene": "38350",
    "lake mary": "38425",
    "lake mary jane": "38430",
    "lake mystic": "38519",
    "lake panasoffkee": "38575",
    "lake park": "38600",
    "lake placid": "38625",
    "lake sarasota": "38690",
    "lake wales": "38950",
    "lake worth beach": "39081",
    "lakeland": "38250",
    "lakeland highlands": "38262",
    "lakeside": "38813",
    "lakewood park": "39062",
    "lakewood ranch": "39067",
    "lamont": "39100",
    "land o' lakes": "39200",
    "lantana": "39375",
    "largo": "39425",
    "lauderdale lakes": "39525",
    "lauderdale-by-the-sea": "39475",
    "lauderhill": "39550",
    "laurel": "39600",
    "laurel hill": "39650",
    "lawtey": "39700",
    "layton": "39725",
    "lazy lake": "39750",
    "lealman": "39775",
    "lecanto": "39825",
    "lee": "39850",
    "leesburg": "39875",
    "lehigh acres": "39925",
    "leisure": "39950",
    "lely": "39987",
    "lely resort": "40037",
    "lemon grove": "40100",
    "liberty triangle": "40387",
    "lighthouse point": "40450",
    "limestone": "40525",
    "limestone creek": "40562",
    "lisbon": "40750",
    "live oak": "40875",
    "lloyd": "40950",
    "lochmoor waterway estates": "40985",
    "lockhart": "41025",
    "longboat key": "41150",
    "longwood": "41250",
    "loughman": "41400",
    "lower grand lagoon": "41562",
    "loxahatchee groves": "41577",
    "lutz": "41775",
    "lynn haven": "41825",
    "macclenny": "41950",
    "madeira beach": "42400",
    "madison": "42425",
    "maitland": "42575",
    "malabar": "42625",
    "malone": "42650",
    "manalapan": "42700",
    "manasota key": "42750",
    "manatee road": "42787",
    "mango": "42850",
    "mangonia park": "42900",
    "marathon": "43000",
    "marco island": "43083",
    "marco shores-hammock bay": "43090",
    "margate": "43125",
    "marianna": "43175",
    "marineland": "43250",
    "marion oaks": "43300",
    "mary esther": "43375",
    "masaryktown": "43400",
    "mascotte": "43425",
    "matlacha": "43475",
    "matlacha isles-matlacha shores": "43484",
    "mayo": "43575",
    "mcgregor": "42090",
    "mcintosh": "42150",
    "meadow oaks": "43785",
    "meadow woods": "43800",
    "medley": "43900",
    "medulla": "43925",
    "melbourne": "43975",
    "melbourne beach": "44000",
    "memphis": "44175",
    "merritt island": "44275",
    "mexico beach": "44300",
    "miami": "45000",
    "miami beach": "45025",
    "miami gardens": "45060",
    "miami lakes": "45100",
    "miami shores": "45175",
    "miami springs": "45200",
    "micanopy": "45225",
    "micco": "45275",
    "miccosukee": "45300",
    "middleburg": "45350",
    "midway": "45425",
    "milton": "45750",
    "mims": "45775",
    "minneola": "45900",
    "miramar": "45975",
    "miramar beach": "46000",
    "molino": "46225",
    "monticello": "46500",
    "montura": "46520",
    "montverde": "46525",
    "moon lake": "46540",
    "moore haven": "46550",
    "morriston": "46725",
    "mount carmel": "47000",
    "mount dora": "47050",
    "mount plymouth": "47125",
    "mulat": "47175",
    "mulberry": "47200",
    "munson": "47300",
    "myrtle grove": "47550",
    "naples": "47625",
    "naples manor": "47650",
    "naples park": "47675",
    "naranja": "47700",
    "nassau village-ratliff": "47787",
    "navarre": "48050",
    "navarre beach": "48055",
    "neptune beach": "48100",
    "new port richey": "48500",
    "new port richey east": "48525",
    "new smyrna beach": "48625",
    "newberry": "48200",
    "niceville": "48750",
    "nobleton": "48825",
    "nocatee": "48850",
    "nokomis": "48875",
    "noma": "48900",
    "north bay": "49225",
    "north brooksville": "49237",
    "north deland": "49275",
    "north fort myers": "49350",
    "north key largo": "49406",
    "north lauderdale": "49425",
    "north merritt island": "49435",
    "north miami": "49450",
    "north miami beach": "49475",
    "north palm beach": "49600",
    "north port": "49675",
    "north redington beach": "49725",
    "north river shores": "49750",
    "north sarasota": "49787",
    "north weeki wachee": "49905",
    "northdale": "49260",
    "oak hill": "50450",
    "oak ridge": "50638",
    "oakland": "50525",
    "oakland park": "50575",
    "oakleaf plantation": "50630",
    "ocala": "50750",
    "ocala estates": "50756",
    "ocean": "50925",
    "ocean breeze": "50875",
    "ocean ridge": "50950",
    "ocklawaha": "51050",
    "ocoee": "51075",
    "odessa": "51100",
    "ojus": "51125",
    "okahumpka": "51150",
    "okeechobee": "51200",
    "old miakka": "51342",
    "oldsmar": "51350",
    "olga": "51400",
    "olympia heights": "51475",
    "on top of the world": "51587",
    "ona": "51500",
    "opa-locka": "51650",
    "orange": "51825",
    "orange park": "52125",
    "orangetree": "52162",
    "orchid": "52175",
    "oriole beach": "52250",
    "orlando": "53000",
    "orlovista": "53100",
    "ormond beach": "53150",
    "ormond-by-the-sea": "53200",
    "osprey": "53425",
    "otter creek": "53500",
    "oviedo": "53575",
    "pace": "53725",
    "page park": "53775",
    "pahokee": "53800",
    "paisley": "53850",
    "palatka": "53875",
    "palm": "54175",
    "palm bay": "54000",
    "palm beach": "54025",
    "palm beach gardens": "54075",
    "palm beach shores": "54150",
    "palm coast": "54200",
    "palm harbor": "54350",
    "palm river-clair mel": "54387",
    "palm shores": "54425",
    "palm springs": "54450",
    "palm springs north": "54500",
    "palm valley": "54525",
    "palmer ranch": "54238",
    "palmetto": "54250",
    "palmetto bay": "54275",
    "palmetto estates": "54300",
    "palmona park": "54357",
    "panacea": "54625",
    "panama": "54700",
    "panama city beach": "54725",
    "paradise heights": "54912",
    "parker": "55075",
    "parkland": "55125",
    "pasadena hills": "55305",
    "patrick afb": "55375",
    "paxton": "55475",
    "pea ridge": "55532",
    "pebble creek": "55540",
    "pelican bay": "55650",
    "pelican marsh": "55680",
    "pembroke park": "55750",
    "pembroke pines": "55775",
    "penney farms": "55875",
    "pensacola": "55925",
    "pensacola station": "56004",
    "perry": "56150",
    "pierson": "56425",
    "pine air": "56475",
    "pine castle": "56500",
    "pine hills": "56825",
    "pine island": "56840",
    "pine island center": "56850",
    "pine lakes": "56865",
    "pine level": "56955",
    "pine manor": "57025",
    "pine ridge": "57058",
    "pinecraft": "56525",
    "pinecrest": "56625",
    "pineland": "56875",
    "pinellas park": "56975",
    "pinewood": "57250",
    "pioneer": "57320",
    "pittman": "57380",
    "plant": "57550",
    "plantation": "57425",
    "plantation island": "57492",
    "plantation mobile home park": "57518",
    "poinciana": "57900",
    "point baker": "57908",
    "polk": "57950",
    "pomona park": "58025",
    "pompano beach": "58050",
    "ponce de leon": "58175",
    "ponce inlet": "58200",
    "port charlotte": "58350",
    "port labelle": "58420",
    "port orange": "58575",
    "port richey": "58600",
    "port saint joe": "58675",
    "port saint john": "58700",
    "port saint lucie": "58715",
    "port salerno": "58727",
    "pretty bayou": "58962",
    "princeton": "58975",
    "progress": "59050",
    "punta gorda": "59200",
    "punta rassa": "59250",
    "quail ridge": "59311",
    "quincy": "59325",
    "raiford": "59400",
    "rainbow lakes estates": "59450",
    "rainbow park": "59466",
    "rainbow springs": "59470",
    "raleigh": "59475",
    "reddick": "59675",
    "redington beach": "59725",
    "redington shores": "59750",
    "richmond heights": "60225",
    "richmond west": "60230",
    "ridge manor": "60325",
    "ridge wood heights": "60475",
    "ridgecrest": "60275",
    "rio": "60550",
    "rio pinar": "60562",
    "river park": "60785",
    "river ridge": "60835",
    "riverview": "60950",
    "riviera beach": "60975",
    "rockledge": "61500",
    "roeville": "61615",
    "roosevelt gardens": "61807",
    "roseland": "61875",
    "rotonda": "61937",
    "royal palm beach": "62100",
    "royal palm estates": "62118",
    "ruskin": "62275",
    "safety harbor": "62425",
    "saint augustine": "62500",
    "saint augustine beach": "62525",
    "saint augustine shores": "62550",
    "saint augustine south": "62562",
    "saint cloud": "62625",
    "saint george island": "62650",
    "saint james": "62675",
    "saint leo": "62775",
    "saint lucie": "62800",
    "saint marks": "62825",
    "saint pete beach": "62885",
    "saint petersburg": "63000",
    "samoset": "63225",
    "samsula-spruce creek": "63362",
    "san antonio": "63375",
    "san carlos park": "63425",
    "san castle": "63435",
    "sanford": "63650",
    "sanibel": "63700",
    "sarasota": "64175",
    "sarasota springs": "64325",
    "satellite beach": "64400",
    "sawgrass": "64525",
    "schall circle": "64547",
    "scottsmoor": "64625",
    "sea ranch lakes": "64725",
    "sebastian": "64825",
    "sebring": "64875",
    "seffner": "64925",
    "seminole": "64975",
    "seminole manor": "65100",
    "seville": "65200",
    "sewall's point": "65225",
    "shady hills": "65385",
    "shalimar": "65425",
    "sharpes": "65525",
    "siesta key": "66000",
    "silver lake": "66062",
    "silver springs": "66125",
    "silver springs shores": "66175",
    "silver springs shores east": "66179",
    "sky lake": "66425",
    "sneads": "66725",
    "solana": "66875",
    "sopchoppy": "66925",
    "sorrento": "66975",
    "south apopka": "67163",
    "south bay": "67175",
    "south beach": "67192",
    "south bradenton": "67258",
    "south brooksville": "67266",
    "south daytona": "67325",
    "south gate ridge": "67450",
    "south highpoint": "67462",
    "south miami": "67550",
    "south miami heights": "67575",
    "south palm beach": "67650",
    "south pasadena": "67675",
    "south patrick shores": "67725",
    "south sarasota": "67887",
    "south venice": "68100",
    "southchase": "67270",
    "southeast arcadia": "67355",
    "southgate": "67425",
    "southport": "67800",
    "southwest ranches": "68135",
    "spring hill": "68350",
    "spring lake": "68375",
    "spring ridge": "68435",
    "springfield": "68275",
    "springhill": "68365",
    "stacey street": "68487",
    "starke": "68525",
    "steinhatchee": "68625",
    "stock island": "68800",
    "stuart": "68875",
    "sugarmill woods": "68950",
    "sumatra": "69025",
    "sun city center": "69250",
    "suncoast estates": "69275",
    "sunny isles beach": "69555",
    "sunrise": "69700",
    "sunset": "69812",
    "surfside": "70075",
    "sweetwater": "70345",
    "taft": "70525",
    "tallahassee": "70600",
    "tamarac": "70675",
    "tamiami": "70700",
    "tampa": "71000",
    "tangelo park": "71075",
    "tangerine": "71100",
    "tarpon springs": "71150",
    "tavares": "71225",
    "tavernier": "71250",
    "taylor creek": "71300",
    "temple terrace": "71400",
    "tequesta": "71525",
    "the acreage": "71564",
    "the crossings": "71567",
    "the hammocks": "71569",
    "the meadows": "71580",
    "the villages": "71625",
    "thonotosassa": "71725",
    "three lakes": "71741",
    "three oaks": "71758",
    "tice": "71800",
    "tierra verde": "71825",
    "tiger point": "71842",
    "tildenville": "71850",
    "timber pines": "71867",
    "titusville": "71900",
    "town 'n' country": "72145",
    "treasure island": "72325",
    "trenton": "72350",
    "trilby": "72425",
    "trinity": "72442",
    "tropical park": "72521",
    "tyndall afb": "72875",
    "umatilla": "73025",
    "union park": "73075",
    "university": "73163",
    "upper grand lagoon": "73312",
    "valparaiso": "73675",
    "valrico": "73700",
    "vamo": "73725",
    "venice": "73900",
    "venice gardens": "73950",
    "verandah": "74016",
    "vernon": "74125",
    "vero beach": "74150",
    "vero beach south": "74200",
    "vero lake estates": "74225",
    "verona walk": "74235",
    "viera east": "74368",
    "viera west": "74370",
    "vilano beach": "74400",
    "villas": "74512",
    "vineyards": "74562",
    "virginia gardens": "74575",
    "wabasso": "74625",
    "wabasso beach": "74635",
    "wacissa": "74700",
    "wahneta": "74775",
    "waldo": "74925",
    "wallace": "74975",
    "warm mineral springs": "75175",
    "warrington": "75200",
    "washington park": "75225",
    "watergate": "75275",
    "watertown": "75300",
    "wauchula": "75375",
    "waukeenah": "75425",
    "wausau": "75450",
    "waverly": "75475",
    "webster": "75600",
    "wedgefield": "75612",
    "weeki wachee gardens": "75641",
    "wekiwa springs": "75725",
    "welaka": "75750",
    "wellington": "75812",
    "wesley chapel": "75875",
    "west bradenton": "76050",
    "west canaveral groves": "76058",
    "west deland": "76087",
    "west lealman": "76481",
    "west little river": "76487",
    "west melbourne": "76500",
    "west miami": "76525",
    "west palm beach": "76600",
    "west park": "76658",
    "west pensacola": "76675",
    "west perrine": "76700",
    "west samoset": "76740",
    "west vero corridor": "76937",
    "westchase": "76062",
    "westchester": "76075",
    "westgate": "76290",
    "westlake": "76417",
    "weston": "76582",
    "westview": "76950",
    "westville": "76975",
    "westwood lakes": "77075",
    "wewahitchka": "77100",
    "whiskey creek": "77137",
    "white": "77275",
    "white springs": "77400",
    "whitfield": "77467",
    "wildwood": "77675",
    "williamsburg": "77735",
    "williston": "77825",
    "williston highlands": "77837",
    "willow oak": "77862",
    "wilton manors": "78000",
    "wimauma": "78025",
    "windermere": "78050",
    "winding cypress": "78063",
    "windsor": "78110",
    "winter beach": "78225",
    "winter garden": "78250",
    "winter haven": "78275",
    "winter park": "78300",
    "winter springs": "78325",
    "wiscon": "78340",
    "woodlawn beach": "78500",
    "woodville": "78700",
    "world golf": "78740",
    "worthington springs": "78775",
    "wright": "78800",
    "yalaha": "78900",
    "yankeetown": "78925",
    "yeehaw junction": "78975",
    "yulee": "79175",
    "zellwood": "79200",
    "zephyrhills": "79225",
    "zephyrhills north": "79231",
    "zephyrhills south": "79237",
    "zephyrhills west": "79243",
    "zolfo springs": "79250"
}

def _fl_place_fips(city):
    if not city: return None
    return _FL_PLACES.get(_place_key(city))

def _resolve_place(city, state_abbr, address):
    """(state_fips, place_fips) for the property's city. Order: (1) the bundled
    Florida Gazetteer table — offline and deterministic; (2) the keyless Census
    Geocoder; (3) a state place-list name match (needs the optional Census key
    from most networks)."""
    if (state_abbr or "").upper() == "FL":
        pf = _fl_place_fips(city)
        if pf: return STATE_FIPS["FL"], pf
    geo = census_geographies(address)
    if geo and geo.get("place_fips"):
        return geo.get("state_fips"), geo.get("place_fips")
    fips = STATE_FIPS.get((state_abbr or "").upper())
    if not fips or not city: return fips, None
    try:
        key = _census_key()
        p = {"get": "NAME", "for": "place:*", "in": f"state:{fips}"}
        if key: p["key"] = key
        r = requests.get(f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5", params=p, timeout=10)
        for row in (r.json() or [])[1:]:
            nm = (row[0] or "").lower()
            if nm.startswith(city.lower() + ",") or nm.startswith(city.lower() + " "):
                return fips, row[2]
    except Exception:
        pass
    return fips, None

def census_demographics(zip_code, city=None, state=None, address=None):
    """Demographics dashboard data: ZIP (ZCTA) + city (place) blocks with income
    distribution (B19001), race (B02001+B03003), age pyramid (B01001), population
    trend, and prior-release change for home value / rent.
    PRIMARY source: Census Reporter API (keyless, api.censusreporter.org).
    The direct api.census.gov path remains only as an optional keyed alternate."""
    out = {"zip": None, "city": None, "source": None,
           "date": datetime.date.today().isoformat()}
    zip_geo = f"86000US{zip_code}"
    blk, src = _geo_block_any(zip_geo, f"ZCTA5 {zip_code}", f"zip code tabulation area:{zip_code}")
    if blk: blk["geoid"] = zip_geo; blk["scope_label"] = f"ZIP {zip_code} (ZCTA)"
    out["zip"] = blk
    if src: out["source"] = src
    if blk:
        blk["pop_trend"] = _pop_trend_cr(zip_geo)
        if len(blk["pop_trend"]) < 2 and _census_key():
            # Census Reporter's public API serves named 5-year releases
            # (acs2014_5yr / acs2019_5yr / acs2024_5yr verified) but NOT
            # acs2023_5yr — vintage trend falls back to the keyed direct API.
            blk["pop_trend"] = _pop_trend(zip_code)
        prev_vals = {}
        try:
            prev_vals, _r = _cr_fetch(zip_geo, release="acs2019_5yr", tables="B25077,B25064")
        except Exception:
            prev_vals = {}
        if not prev_vals and _census_key():
            prev_vals = _acs_fetch(ACS_YEAR - 5, "B25077_001E,B25064_001E",
                                   f"zip code tabulation area:{zip_code}") or {}
        chg = {}
        for var, key, field in (("B25077_001E", "home_value_pct", "median_home_value"),
                                ("B25064_001E", "rent_pct", "median_rent")):
            cur, prev = blk[field], _acs_num(prev_vals, var)
            chg[key] = round((cur - prev) / prev * 100, 1) if (cur and prev) else None
        blk["change"] = chg
    sfips, pfips = _resolve_place(city, state, address)
    if pfips:
        cblk, csrc = _geo_block_any(f"16000US{sfips}{pfips}", city, f"place:{pfips}&in=state:{sfips}")
        if cblk: cblk["geoid"] = f"16000US{sfips}{pfips}"; cblk["scope_label"] = f"{city} (Census place)"
        out["city"] = cblk
        if not out["source"] and csrc: out["source"] = csrc
    if not out["source"]:
        out["source"] = "US Census ACS 5-year via Census Reporter (api.censusreporter.org)"
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
    q = f"""[out:json][timeout:60];
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
    # v2.3 (P1): root cause of the Naples 502 (Sherif's screenshot, 2026-11-03)
    # is Overpass capacity, not the query: overpass-api.de answers a small
    # test query with "502 Gateway Timeout — this Overpass API server is
    # currently at capacity. All query workers are busy and the request queue
    # is full. Retry your request using exponential backoff" (measured live
    # from this machine). POST was also tested: under load it QUEUES instead
    # of failing, hanging past 120s per endpoint. So: GET (fails fast on a
    # capacity 502, succeeds as soon as a worker frees) across the primary +
    # two public mirrors, one backoff pass after 20s (as Overpass itself
    # instructs), per-attempt timeout 60s. And ALL user-facing failure text
    # is human — the old code interpolated the raw exception, which embeds
    # the full query URL, straight into the page (the raw-URL dump in the
    # screenshot). Technical detail goes to the server log only.
    elements, last_err = None, None
    for attempt in range(2):
        for url in ("https://overpass-api.de/api/interpreter",
                    "https://overpass.kumi.systems/api/interpreter",
                    "https://overpass.nchc.org.tw/api/interpreter"):
            try:
                r = requests.get(url, params={"data": q},
                                 headers={"User-Agent": "HouseAble-DealAnalyzer/1.1"}, timeout=60)
                r.raise_for_status()
                elements = r.json().get("elements", [])
                break
            except Exception as e:
                last_err = e
                continue
        if elements is not None:
            break
        if attempt == 0:
            time.sleep(20)
    if elements is None:
        print(f"[amenities] Overpass pull failed on all endpoints for ({lat},{lon}): {last_err}", flush=True)
        return {"error": "Not found — nearby places couldn't be loaded from OpenStreetMap just now "
                         "(the service may be busy). Use Refresh to retry.",
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

JOB_TIMEOUT_S = 600

def reap_stale_jobs():
    """v2.0 reaper: ANY job still 'running' past the timeout is dead (its worker
    thread is gone) — move it to a terminal state with a human message, not just
    the single latest job per property. Called on job reads so pollers and
    banners never wait forever."""
    try:
        db().execute("""UPDATE jobs SET status='error', finished_at=datetime('now'),
                        message='This took too long (over 10 minutes) and was stopped — the data source may be slow or unreachable. Please try again.'
                        WHERE status='running' AND started_at < datetime('now', ?)""",
                     (f"-{JOB_TIMEOUT_S} seconds",))
        db().commit()
    except Exception:
        pass

def _humanize_job_message(msg):
    """Plain-English banner text: never render raw exception/sqlite text."""
    m = (msg or "").strip()
    low = m.lower()
    if "closed database" in low or "database is locked" in low or "disk i/o" in low:
        return "A storage hiccup interrupted that job — please run it again."
    if low.startswith("error:"):
        m = m[len("error:"):].strip()
    return m

def latest_job(pid, kind=None):
    reap_stale_jobs()
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
        if res.get("error"):
            # v1.6 honest-failure: a failed refresh must NEVER destroy cached places —
            # keep amenities_json/amenities_date, record the error for the tab notice.
            db().execute("UPDATE properties SET amenities_error=? WHERE id=?", (res["error"], pid)); db().commit()
            raise RuntimeError(res["error"])
        db().execute("UPDATE properties SET amenities_json=?, amenities_date=?, amenities_error=NULL WHERE id=?",
                     (json.dumps(res), res.get("date"), pid)); db().commit()
        return f"✓ {res['total']} nearby places found within {res['radius_mi']} mi (OpenStreetMap)"
    return start_job(pid, "amenities", fn)

def job_demographics(pid):
    def fn():
        p = get_prop(pid)
        try:
            demo = census_demographics(p["zip"], p["city"], p["state"], p["address"])
        except Exception:
            demo = None
        if not demo or not demo.get("zip"):
            # v1.6 honest-failure: keep cached demo_json/demo_date untouched; record
            # the error so the tab shows a scoped, dismissible notice instead of a
            # fresh 'pulled' stamp over empty data.
            # v2.0: the ZIP block is the property's OWN geography and is required
            # for success — a city-only result (bad ZIP, resolvable city) must not
            # pose as a successful pull or overwrite the cache.
            msg = ("Census data didn't come back — Census Reporter (and the Census Geocoder for the "
                   "city scope) may be unreachable right now. Cached data, if any, is unchanged.")
            db().execute("UPDATE properties SET demo_error=? WHERE id=?", (msg, pid)); db().commit()
            raise RuntimeError("Census demographics failed (key/network) — see the notice on the Demographics tab")
        db().execute("UPDATE properties SET demo_json=?, demo_date=?, demo_error=NULL WHERE id=?",
                     (json.dumps(demo), demo.get("date"), pid)); db().commit()
        return f"✓ Demographics loaded for ZIP {p['zip']} (US Census ACS)"
    return start_job(pid, "demographics", fn)

# ---------------------------------------------------------------- routes
def get_prop(pid):
    row = db().execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
    if not row: abort(404)
    p = dict(row)
    for k in NUMERIC_PROP_FIELDS:      # heal any text that already slipped into a numeric column
        if isinstance(p.get(k), str): p[k] = clean_num(p[k])
    return p

def all_boxes():
    return db().execute("SELECT * FROM buy_boxes ORDER BY id").fetchall()

EDITABLE = ["status","stage","deal_method","list_price","property_type","beds","baths","sqft","lot_sqft",
 "year_built","apn","hoa_dues","hoa_freq","subdivision","legal_desc","sewer","water","appliances",
 "flooring","cooling","heating","roof_age","foundation","occupancy_now","occupancy_closing","as_is_value","arv",
 "repairs","purchase_price","inspection_days","inspection_unit","inspection_deadline","buy_it_now","min_offer",
 "wholesale_contract_amt","emd_ab","emd_bc","title_company","escrow_officer","closing_date","tc_name",
 "offers_deadline","loan_balance","loan_type","loan_rate","loan_piti","dispo_asking","dispo_bin","dispo_min",
 "dispo_inspection_days","offer_review","est_zillow","est_realtor","est_redfin","est_county","notes",
 "skiptrace_notes","skiptrace_status","tax_market","tax_assessed","tax_taxable","tax_amount","tax_exemptions",
 "tax_millage","tax_year","sales_history",
 "cash_to_seller","total_entry_fee","closing_costs_paid_by","purchase_deposit",
 "sf_loan_amount","sf_amort_years","sf_amort_unit","sf_rate","sf_payment","sf_balloon",
 "dispo_exit_method","dispo_buyer_deposit","dispo_day_type"]

DEFAULT_TRADES = ["Plumbing", "Roofing", "Electrical", "HVAC", "General", "Foundation", "Pest Control"]

@app.route("/")
def dashboard():
    props = db().execute("SELECT * FROM properties ORDER BY id DESC LIMIT 12").fetchall()
    counts = {r["status"]: r["c"] for r in db().execute("SELECT status, COUNT(*) c FROM properties GROUP BY status")}
    recent = db().execute("""SELECT s.*, p.address FROM scenarios s JOIN properties p ON p.id=s.property_id
                             ORDER BY s.id DESC LIMIT 6""").fetchall()
    # v1.8a: same score reconciliation as the property page — recompute the
    # displayed results with the current engine (stored results can be stale).
    _rec = []
    for _s in recent:
        _s = dict(_s)
        try:
            _s["results"] = json.dumps(compute_underwriting(
                _s["strategy"], json.loads(_s["inputs"] or "{}"), get_prop(_s["property_id"])))
        except Exception:
            pass
        _rec.append(_s)
    recent = _rec
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
        # v1.6 (item O): auto-enrichment — a fresh property must open POPULATED like
        # ReiSearch's does, not as an empty shell waiting on manual pulls. The comp
        # pull also fills the subject's specs + listing photos (incl. the deep
        # subject lookup); demographics loads from Census Reporter in parallel.
        job_comps(pid, 1.0, 365)
        job_demographics(pid)
        flash(f"Geocoded (ArcGIS score {geo['score']}) — flood zone: {flood_zone}. "
              "Setting this property up now: comps, listing photos, specs and demographics "
              "are loading automatically (watch the banner — about a minute).")
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
                v = clean_num(v) if f in NUMERIC_PROP_FIELDS else (v if v != "" else None)
                sets.append(f"{f}=?"); args.append(v)
        if sets:
            args.append(pid); d.execute(f"UPDATE properties SET {','.join(sets)} WHERE id=?", args); d.commit()
            p = get_prop(pid)
            # v1.7b: the Overview's inline autosave posts the same form via fetch —
            # answer with JSON so the page can show its "Last saved" indicator
            # without a full reload. The classic Save-button POST keeps the
            # flash + re-render path below, byte-for-byte the old behavior.
            if request.form.get("ajax") == "1":
                return jsonify(ok=True, saved_at=datetime.datetime.now().strftime("%I:%M %p"))
            flash("Saved.")
            # Post/Redirect/Get: a browser reload after a classic save must
            # never resubmit the (now stale) form and clobber newer autosaved
            # edits — redirect to the same tab instead of re-rendering here.
            return redirect(url_for("property_page", pid=pid, tab=request.args.get("tab", "overview")))
    comps = d.execute("SELECT * FROM comps WHERE property_id=? ORDER BY excluded, pinned DESC, match_score DESC", (pid,)).fetchall()
    liens = d.execute("SELECT * FROM liens WHERE property_id=?", (pid,)).fetchall()
    docs = d.execute("SELECT * FROM documents WHERE property_id=? ORDER BY category, slot", (pid,)).fetchall()
    assigns = d.execute("""SELECT a.*, c.name, c.company FROM assignments a LEFT JOIN contractors c
                           ON c.id=a.contractor_id WHERE a.property_id=?""", (pid,)).fetchall()
    contractors = d.execute("SELECT * FROM contractors ORDER BY trade, name").fetchall()
    scenarios = d.execute("SELECT * FROM scenarios WHERE property_id=? ORDER BY id DESC", (pid,)).fetchall()
    # v1.8a score reconciliation: displayed scenario scores are recomputed with
    # the CURRENT engine from the stored inputs, so the scenarios table can
    # never drift from the live rail. The stored `results` are a snapshot of
    # whatever code version saved them (shipped scenarios 1/2 predate the
    # penalty rework and showed 80/63 for identical inputs).
    _disp = []
    for _s in scenarios:
        _s = dict(_s)
        try:
            _s["results"] = json.dumps(compute_underwriting(
                _s["strategy"], json.loads(_s["inputs"] or "{}"), p))
        except Exception:
            pass
        _disp.append(_s)
    scenarios = _disp
    plans = d.execute("SELECT * FROM floorplans WHERE property_id=? ORDER BY id DESC", (pid,)).fetchall()
    v = verdict(p, comps, all_boxes())
    sold = [c for c in comps if c["kind"] == "sold" and not c["excluded"]]
    rents = [c for c in comps if c["kind"] == "rent"]
    market = None
    if sold:
        # v1.6 working controls: property-type filter recomputes cards + trend +
        # nearby table; 1Y/3Y/5Y range affects the trend only (ReiSearch semantics).
        mtype = request.args.get("mtype", "All")
        if mtype not in ("All", "Single Family", "Condo", "Townhouse"): mtype = "All"
        mrange = request.args.get("mrange", "3Y")
        if mrange not in ("1Y", "3Y", "5Y"): mrange = "3Y"
        def _ptype_of(c): return (c["ptype"] if "ptype" in c.keys() else None) or "Single Family"  # pre-v1.6 rows/DBs carry no stored type
        msold = [c for c in sold if mtype == "All" or _ptype_of(c) == mtype]
        type_counts = {}
        for c in sold: type_counts[_ptype_of(c)] = type_counts.get(_ptype_of(c), 0) + 1
        prices = sorted(c["price"] for c in msold if c["price"]); ppsfs = sorted(c["ppsf"] for c in msold if c["ppsf"])
        doms = sorted(c["dom"] for c in msold if c["dom"])
        med = lambda xs: xs[len(xs)//2] if xs else None
        by_month = {}
        for c in msold:
            if c["event_date"] and c["price"]: by_month.setdefault(c["event_date"][:7], []).append(c["price"])
        trend_all = [{"month": m, "median": sorted(vv)[len(vv)//2], "count": len(vv)} for m, vv in sorted(by_month.items())]
        trend = trend_all[-{"1Y": 12, "3Y": 36, "5Y": 60}[mrange]:]
        spark = ""
        if len(trend) >= 2:
            vals = [t["median"] for t in trend]; lo, hi = min(vals), max(vals); span = (hi - lo) or 1
            spark = " ".join(f"{round(i / (len(vals) - 1) * 300, 1)},{round(34 - (vv - lo) / span * 30, 1)}"
                             for i, vv in enumerate(vals))
        zip_groups = {}
        for c in msold:
            if c["zip"] and c["price"]: zip_groups.setdefault(c["zip"], []).append(c)
        nearby = []
        for z, cs in zip_groups.items():
            zp = sorted(c["price"] for c in cs if c["price"]); zd = sorted(c["dom"] for c in cs if c["dom"])
            nearby.append({"zip": z, "median_price": med(zp), "median_dom": med(zd), "n": len(cs), "you": z == p["zip"]})
        nearby.sort(key=lambda r: (not r["you"], -r["n"]))
        # v2.6 Market Snapshot: insights computed ONLY from the stored set
        # (no editorializing, no outside figures). ReiSearch's own snapshot
        # block is a "COMING SOON" placeholder; ours carries real computed
        # statements or nothing.
        snap = []
        if med(prices) is not None:
            snap.append(f"Median sale price {money(med(prices))} across {len(msold)} stored sold "
                        f"comps in ZIP {p['zip']}" + (f" (property-type filter: {mtype})" if mtype != "All" else "") + ".")
        if len(trend_all) >= 2 and trend_all[0]["median"]:
            _f, _l = trend_all[0], trend_all[-1]
            _chg = (_l["median"] - _f["median"]) / _f["median"] * 100
            snap.append(f"Median sale price moved from {money(_f['median'])} ({_f['month']}) to "
                        f"{money(_l['median'])} ({_l['month']}) — {'+' if _chg >= 0 else ''}{_chg:.1f}% over the stored trend window.")
        if med(doms) is not None:
            snap.append(f"Median days on market: {med(doms):g} (of the stored sold comps with a recorded DOM).")
        if med(ppsfs) is not None:
            snap.append(f"Median sold price per sq ft: ${med(ppsfs):,.0f}.")
        market = {"count": len(msold), "median_price": med(prices), "median_ppsf": med(ppsfs), "median_dom": med(doms),
                  "snapshot": snap,
                  "trend": trend, "spark": spark,
                  "trend_min": min([t["median"] for t in trend]) if trend else None,
                  "trend_max": max([t["median"] for t in trend]) if trend else None,
                  "nearby": nearby, "mtype": mtype, "mrange": mrange, "type_counts": type_counts,
                  "untyped": sum(1 for c in sold if not ("ptype" in c.keys() and c["ptype"])),
                  "source": "Computed from your stored HomeHarvest sold comps", "date": datetime.date.today().isoformat()}
    # demographics: served from cache (async job fills it); never block page load on Census
    demo = None
    if p["demo_json"]:
        try: demo = json.loads(p["demo_json"])
        except Exception: demo = None
    demo_error = p["demo_error"] if "demo_error" in p.keys() else None
    amenities_error = p["amenities_error"] if "amenities_error" in p.keys() else None
    dscope = request.args.get("dscope", "zip")
    if dscope not in ("zip", "city"): dscope = "zip"
    dgeo = None; trend_svg = None; demo_scope_fallback = False
    if demo:
        dgeo = demo.get("zip") if dscope == "zip" else demo.get("city")
        if dgeo is None and dscope == "city":
            # v2.0: never silently show ZIP numbers under a city label — fall
            # back AND say so on the page.
            dscope, dgeo = "zip", demo.get("zip"); demo_scope_fallback = True
        if dgeo is None and dscope == "zip": dscope, dgeo = "city", demo.get("city")
        pts = (dgeo or {}).get("pop_trend") or []
        if len(pts) >= 2:
            vals = [e["pop"] for e in pts]; lo, hi = min(vals), max(vals); span = (hi - lo) or 1
            trend_svg = {"points": " ".join(f"{round(i / (len(vals) - 1) * 300, 1)},{round(44 - (vv - lo) / span * 38, 1)}"
                                            for i, vv in enumerate(vals)),
                         "first_year": pts[0]["year"], "last_year": pts[-1]["year"],
                         "first_pop": pts[0]["pop"], "last_pop": pts[-1]["pop"]}
    tab = request.args.get("tab", "overview")
    # ---- v1.7b Overview: timestamped notes feed (audit 1.11) ----
    # The legacy single `notes` textarea text migrates in as the feed's first
    # entry the first time the Overview opens with an empty feed — the text is
    # copied, never deleted, so no data can be lost by the restructure.
    prop_notes = []
    if tab == "overview":
        rows = d.execute("SELECT * FROM property_notes WHERE property_id=? ORDER BY id DESC", (pid,)).fetchall()
        if not rows and p["notes"]:
            d.execute("INSERT INTO property_notes(property_id,author,text,created_at) VALUES(?,?,?,?)",
                      (pid, "Imported note", p["notes"], p["created_at"]))
            d.commit()
            rows = d.execute("SELECT * FROM property_notes WHERE property_id=? ORDER BY id DESC", (pid,)).fetchall()
        for r in rows:
            nd = dict(r)
            try:
                nd["when"] = datetime.datetime.fromisoformat(str(r["created_at"])).strftime("%b %d, %Y · %I:%M %p")
            except Exception:
                nd["when"] = str(r["created_at"] or "")
            prop_notes.append(nd)
    # ---- v1.7b: Market Value headline (audit 1.3) — mean of the listing-site
    # estimates on file, exactly ReiSearch's formula; county only as fallback.
    est3 = [p[k] for k in ("est_zillow", "est_realtor", "est_redfin") if p[k]]
    if est3:
        market_value = round(sum(est3) / len(est3)); mv_basis = f"average of {len(est3)} estimate(s) on file"
    elif p["est_county"]:
        market_value = round(p["est_county"]); mv_basis = "county appraiser estimate"
    else:
        market_value, mv_basis = None, ""
    try:
        added_pretty = datetime.datetime.fromisoformat(str(p["created_at"])[:19]).strftime("%b %d, %Y")
    except Exception:
        added_pretty = str(p["created_at"] or "")[:10]
    # ---- v1.7b Contractors: per-property per-trade rows (ReiSearch pass 2) ----
    # Computed only for the Contractors tab — the only tab that renders them.
    trades_rows = []
    dir_trades = []
    if tab == "contractors":
        custom_trades = [r["trade"] for r in d.execute(
            "SELECT trade FROM property_trades WHERE property_id=? ORDER BY trade", (pid,))]
        trade_names = list(DEFAULT_TRADES)
        for t in custom_trades + [a["trade"] for a in assigns if a["trade"]]:
            if t and t not in trade_names: trade_names.append(t)
        for t in trade_names:
            latest = None
            for a in assigns:  # assigns are unordered; the highest id for the trade wins
                if a["trade"] == t and (latest is None or a["id"] > latest["id"]): latest = a
            trades_rows.append({"trade": t, "a": latest})
        dir_trades = sorted({c["trade"] for c in contractors if c["trade"]})
    # v1.7 demographics auto-load: opening the tab with no cached data starts the
    # pull itself — the source is the keyless Census Reporter API, so there is no
    # manual step and no API key to configure. A job already running is left
    # alone; a failure less than 15 minutes old is NOT auto-retried (the tab then
    # shows the scoped failure notice + the manual Load button instead of a loop).
    if request.method == "GET" and tab == "demographics" and not demo:
        dj = latest_job(pid, "demographics")
        start_demo = dj is None or dj["status"] != "running"
        if start_demo and dj is not None and dj["status"] == "error" and dj["finished_at"]:
            try:
                fin = datetime.datetime.fromisoformat(str(dj["finished_at"]))
                if (datetime.datetime.now() - fin).total_seconds() < 900:
                    start_demo = False
            except Exception:
                pass
        if start_demo:
            job_demographics(pid)
    # v1.6: job banners are scoped to the tab their job belongs to (a stale Census
    # error must not follow the user across every tab), timestamps are human, and
    # finished/error banners can be dismissed.
    JOB_TAB = {"comps": "comps", "demographics": "demographics", "amenities": "demographics"}
    banner_job = None
    for _try in range(5):
        raw_job = latest_job(pid)
        if not raw_job: break
        if (raw_job["dismissed"] if "dismissed" in raw_job.keys() else 0): break
        if JOB_TAB.get(raw_job["kind"]) != tab: break
        # v2.0: finished banners auto-expire after 24h (dismissed in place)
        # instead of lingering forever; messages are humanized before render
        # so raw exception text never reaches the page.
        if raw_job["status"] in ("done", "error") and raw_job["finished_at"]:
            try:
                fin = datetime.datetime.fromisoformat(str(raw_job["finished_at"]))
                if (datetime.datetime.now() - fin).total_seconds() > 24 * 3600:
                    d.execute("UPDATE jobs SET dismissed=1 WHERE id=?", (raw_job["id"],)); d.commit()
                    continue
            except Exception:
                pass
        banner_job = dict(raw_job)
        banner_job["message"] = _humanize_job_message(banner_job.get("message"))
        ts = raw_job["finished_at"] or raw_job["started_at"] or ""
        try:
            banner_job["when"] = datetime.datetime.fromisoformat(str(ts)).strftime("%b %d, %I:%M %p")
        except Exception:
            banner_job["when"] = ts
        break
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
    subj_d = {"sqft": p["sqft"], "beds": p["beds"], "baths": p["baths"], "year_built": p["year_built"], "lot_sqft": p["lot_sqft"]}
    def in_radius(c): return rmax is None or (c["distance_mi"] is not None and c["distance_mi"] <= rmax)
    cutoff = (datetime.date.today() - datetime.timedelta(days=window)).isoformat()
    actives = [c for c in comps if c["kind"] == "active" and not c["excluded"]]
    sold_view = [c for c in (sold + actives) if in_radius(c) and (not c["event_date"] or c["event_date"] >= cutoff)]
    rents_view = [c for c in rents if in_radius(c)]
    # P0-1/P0-2 (Sherif 2026-11-02): MATCH-FIRST ordering. Sold: tier asc, then
    # pinned, then the weights score, then distance — the weights dialog still
    # orders WITHIN a tier, it no longer outranks a better beds/baths/sqft
    # match. Rentals: exact beds/baths matches first, then near, then the rest.
    _subj = {"beds": p["beds"], "baths": p["baths"], "sqft": p["sqft"]}
    sold_view.sort(key=lambda c: (comp_tier(_subj, dict(c)), -(c["pinned"] or 0),
                                  -(c["match_score"] or 0),
                                  c["distance_mi"] if c["distance_mi"] is not None else 9e9))
    rents_view.sort(key=lambda c: (rent_match_group(_subj, dict(c)), -(c["match_score"] or 0),
                                   c["distance_mi"] if c["distance_mi"] is not None else 9e9))
    rent_match = {"exact": 0, "near": 0, "total": len(rents_view)}
    for c in rents_view:
        _g = rent_match_group(_subj, dict(c))
        if _g == 0: rent_match["exact"] += 1
        elif _g == 1: rent_match["near"] += 1
    sold_stats = pinned_sold_stats(sold_view)
    # v2.5: "Pinned #N" labels (ReiSearch comp-row anatomy) — rank among the
    # pinned comps in each view's own display (match-first) order.
    pinned_rank = {}
    for _lst in (sold_view, rents_view):
        _n = 0
        for _c in _lst:
            if _c["pinned"]:
                _n += 1
                pinned_rank[_c["id"]] = _n
    # v2.0: whole-filtered-set averages (the sold set the user is looking at —
    # window + radius applied) beside the pinned/top-3 line.
    _set_sold = [c for c in sold_view if c["kind"] == "sold"]
    _set_prices = [c["price"] for c in _set_sold if c["price"]]
    _set_ppsf = [(c["price"] / c["sqft"]) for c in _set_sold if c["price"] and c["sqft"]]
    _set_doms = [c["dom"] for c in _set_sold if c["dom"] is not None]
    set_stats = {"n": len(_set_sold),
                 "avg": round(sum(_set_prices) / len(_set_prices)) if _set_prices else None,
                 "ppsf": round(sum(_set_ppsf) / len(_set_ppsf)) if _set_ppsf else None,
                 "dom": round(sum(_set_doms) / len(_set_doms)) if _set_doms else None}
    # v2.0 count reconciliation: the header count is ALL stored sold comps;
    # the Sold view applies the date window — the difference is dated sales
    # older than the window. Both bases are labeled on the page.
    sold_older_n = len([c for c in sold if c["event_date"] and c["event_date"] < cutoff])
    ltr = ltr_stats(rents_view)
    coli = coliving_stats(rents_view)
    med_rent = ltr["median_rent"] if ltr else None
    afford_view = [c for c in rents_view if med_rent and c["price"] and c["price"] < med_rent]
    breakdowns = {}
    for c in sold_view + rents_view:
        breakdowns[c["id"]] = score_breakdown(subj_d, {"distance_mi": c["distance_mi"], "sqft": c["sqft"],
                                                       "beds": c["beds"], "baths": c["baths"], "year_built": c["year_built"],
                                                       "lot_sqft": c["lot_sqft"] if "lot_sqft" in c.keys() else None})
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
    # v1.5 autosave drafts: the working state per property+strategy, restored on return.
    drafts = {}
    try:
        for dr in d.execute("SELECT * FROM uw_drafts WHERE property_id=?", (pid,)):
            drafts[dr["strategy"]] = dr
    except Exception:
        drafts = {}
    uw_draft_meta = {}
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
        if strat in drafts:
            try: draft_inp = json.loads(drafts[strat]["state"] or "{}")
            except Exception: draft_inp = {}
            uw_current[strat] = uw_state(strat, p, draft_inp, uw_ctx)
            uw_draft_meta[strat] = drafts[strat]["updated_at"]
        else:
            uw_current[strat] = latest if latest is not None else fresh
    # pinned (or top-3) sold comps for the Flip Sale & Exit table
    # Fallback pool = first 3 of the tier-ordered sold view (P0-1: Tier 1 first).
    flip_pool = [c for c in sold_view if c["pinned"] and c["kind"] == "sold"] or \
        [c for c in sold_view if c["kind"] == "sold"][:3]
    flip_prices = [c["price"] for c in flip_pool if c["price"]]
    # ---- v1.7b comp filters popover vocabularies (Sold view) ----
    sold_years = sorted({c["event_date"][:4] for c in sold_view if c["event_date"]}, reverse=True)
    sold_zips = sorted({c["zip"] for c in sold_view if c["zip"]})
    return render_template("property.html", p=p, comps=comps, sold=sold, rents=rents, liens=liens, docs=docs,
                           assigns=assigns, contractors=contractors, scenarios=scenarios, plans=plans,
                           v=v, market=market, demo=demo, demo_error=demo_error, amenities_error=amenities_error,
                           dscope=dscope, dgeo=dgeo, demo_scope_fallback=demo_scope_fallback,
                           trend_svg=trend_svg, tab=tab, amort=amort, amenities=amenities,
                           photos=photos, job=banner_job, steps=steps,
                           cview=cview, window=window, rmax=rmax, sold_view=sold_view, rents_view=rents_view,
                           rent_match=rent_match,
                           sold_stats=sold_stats, set_stats=set_stats, sold_older_n=sold_older_n,
                           ltr=ltr, coli=coli, med_rent=med_rent, afford_view=afford_view,
                           breakdowns=breakdowns, room_est=room_est,
                           strategies=strategies, uw_fresh=uw_fresh, uw_saved=uw_saved, uw_current=uw_current,
                           uw_draft_meta=uw_draft_meta,
                           flip_pool=flip_pool, comp_weights=comp_weights(),
                           prop_notes=prop_notes, market_value=market_value, mv_basis=mv_basis,
                           mv_count=len(est3), pinned_rank=pinned_rank,
                           added_pretty=added_pretty, trades_rows=trades_rows, dir_trades=dir_trades,
                           sold_years=sold_years, sold_zips=sold_zips,
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

@app.route("/job/<int:jid>/dismiss", methods=["POST"])
def dismiss_job(jid):
    row = db().execute("SELECT property_id, kind FROM jobs WHERE id=?", (jid,)).fetchone()
    if row:
        db().execute("UPDATE jobs SET dismissed=1 WHERE id=?", (jid,)); db().commit()
        tab = {"comps": "comps", "demographics": "demographics", "amenities": "demographics"}.get(row["kind"], "overview")
        return redirect(url_for("property_page", pid=row["property_id"], tab=tab))
    return redirect(url_for("index"))

@app.route("/property/<int:pid>/data-error/dismiss", methods=["POST"])
def dismiss_data_error(pid):
    get_prop(pid)
    which = request.form.get("which", "")
    if which == "demo":
        db().execute("UPDATE properties SET demo_error=NULL WHERE id=?", (pid,))
    elif which == "amenities":
        db().execute("UPDATE properties SET amenities_error=NULL WHERE id=?", (pid,))
    db().commit()
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

# ------------------------------------------------- comp weights (item L) + add comp (item M)
def rerank_all_comps():
    """Recompute every comp's stored match score with the current weights."""
    d = db(); n = 0
    props = {r["id"]: r for r in d.execute("SELECT * FROM properties")}
    for c in d.execute("SELECT * FROM comps"):
        p = props.get(c["property_id"])
        if not p: continue
        subj = {"sqft": p["sqft"], "beds": p["beds"], "baths": p["baths"],
                "year_built": p["year_built"], "lot_sqft": p["lot_sqft"]}
        comp = {"distance_mi": c["distance_mi"], "sqft": c["sqft"], "beds": c["beds"],
                "baths": c["baths"], "year_built": c["year_built"],
                "lot_sqft": c["lot_sqft"] if "lot_sqft" in c.keys() else None}
        d.execute("UPDATE comps SET match_score=? WHERE id=?", (match_score(subj, comp), c["id"]))
        n += 1
    d.commit()
    return n

@app.route("/property/<int:pid>/comp-weights", methods=["POST"])
def comp_weights_save(pid):
    get_prop(pid)
    w = {}
    for k in COMP_WEIGHT_DEFAULTS:
        v = fnum(request.form.get("w_" + k), None)
        w[k] = v if (v is not None and v >= 0) else COMP_WEIGHT_DEFAULTS[k]
    set_setting("comp_weights", json.dumps(w))
    n = rerank_all_comps()
    flash(f"✓ Comp weights saved — {n} comps re-ranked. New weights: "
          + " · ".join(f"{k} {w[k]:g}" for k in COMP_WEIGHT_DEFAULTS) + ".", "info")
    return redirect(url_for("property_page", pid=pid, tab="comps", view="sold"))

_ADDR_SUFFIX = {"place": "pl", "pl": "pl", "drive": "dr", "dr": "dr", "court": "ct", "ct": "ct",
                "avenue": "ave", "ave": "ave", "street": "st", "st": "st", "lane": "ln", "ln": "ln",
                "road": "rd", "rd": "rd", "circle": "cir", "cir": "cir", "boulevard": "blvd",
                "blvd": "blvd", "terrace": "ter", "ter": "ter", "trail": "trl", "trl": "trl",
                "way": "way", "parkway": "pkwy", "pkwy": "pkwy", "highway": "hwy", "hwy": "hwy"}

def _norm_addr(s):
    """Normalize a street address for lookup matching: lowercase, no punctuation,
    canonical suffixes, single spaces. '15037 Abby Birch Place' -> '15037 abby birch pl'."""
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    toks = [_ADDR_SUFFIX.get(t, t) for t in s.split() if t not in ("fl", "florida")]
    return " ".join(toks)

_DIR_TOKENS = {"n", "s", "e", "w", "ne", "nw", "se", "sw",
               "north", "south", "east", "west"}

def _street_name_token(toks):
    """First real street-name token of a normalized address: skip the house
    number, directionals and suffixes. v2.0 root cause: the old fallback
    compared raw token #2, which for '1220 E Comanche Ave' is the directional
    'e' — so it matched '1220 E North St', a different house. A directional
    can never BE the street name; fail closed (None) when no name token."""
    for t in toks[1:]:
        if t in _DIR_TOKENS or t in _ADDR_SUFFIX.values():
            continue
        return t
    return None

def _lookup_in_frame(address, z, kind_choice):
    from homeharvest import scrape_property
    lt = "sold" if kind_choice == "sold" else "for_sale"
    kw = {"location": z, "listing_type": lt}
    if lt == "sold": kw["past_days"] = 365
    try:
        df = scrape_property(**kw)
    except Exception:
        return None
    if df is None or len(df) == 0: return None
    target = _norm_addr(address)
    ttoks = target.split(" ")
    tnum = ttoks[0] if ttoks else ""
    tname = _street_name_token(ttoks)
    best = None
    for _, r in df.iterrows():
        st = _norm_addr(_s(r.get("street")) + " " + (_s(r.get("city")) or ""))
        if not st: continue
        if st == target or target in st or st in target:
            return r
        stoks = st.split(" ")
        sname = _street_name_token(stoks)
        if (tnum and tname and sname and stoks[0] == tnum and sname == tname
                and best is None):
            best = r  # same house number AND street name, suffix/city form differs
    return best

def lookup_comp_by_address(prop, address, kind_choice):
    """Add Comp (ReiSearch parity): resolve a typed address through HomeHarvest with a
    ZIP-mode query + normalized-address match — NEVER raw full-address mode (mis-resolves).
    Returns the matched HomeHarvest row (dict-like) or None."""
    m = re.search(r"\b(\d{5})\b", address or "")
    z = m.group(1) if m else (prop["zip"] or "")
    if not z: return None
    hit = _lookup_in_frame(address, z, kind_choice)
    if hit is not None: return hit
    # The ZIP frame missed (no ZIP typed, or the typed ZIP isn't the ZIP the
    # house actually sits in): geocode the typed address and retry once with
    # the ZIP the geocoder reports for it.
    try:
        g = geocode_arcgis(address)
        gz = (g or {}).get("zip")
        if gz and gz != z:
            return _lookup_in_frame(address, gz, kind_choice)
    except Exception:
        pass
    return None

def _insert_comp(prop, vals):
    d = db()
    subj = {"sqft": prop["sqft"], "beds": prop["beds"], "baths": prop["baths"],
            "year_built": prop["year_built"], "lot_sqft": prop["lot_sqft"]}
    vals["match_score"] = match_score(subj, {"distance_mi": vals.get("distance_mi"), "sqft": vals.get("sqft"),
                                             "beds": vals.get("beds"), "baths": vals.get("baths"),
                                             "year_built": vals.get("year_built"), "lot_sqft": vals.get("lot_sqft")})
    cols = ",".join(vals.keys()); ph = ",".join("?" * len(vals))
    cur = d.execute(f"INSERT INTO comps(property_id,{cols}) VALUES(?,{ph})", (prop["id"], *vals.values()))
    d.commit()
    return cur.lastrowid

@app.route("/property/<int:pid>/comps/add-lookup", methods=["POST"])
def add_comp_lookup(pid):
    prop = get_prop(pid)
    addr = (request.form.get("address") or "").strip()
    kind_choice = request.form.get("kind_choice", "sold")
    if kind_choice not in ("sold", "active"): kind_choice = "sold"
    r = lookup_comp_by_address(prop, addr, kind_choice) if addr else None
    if r is None:
        flash(f"No HomeHarvest match found for “{addr}” — use the manual-entry form below; "
              "the comp still enters pin/average math.", "error")
        return redirect(url_for("property_page", pid=pid, tab="comps", view="sold", addmanual=1, addr=addr))
    lat, lon = r.get("latitude"), r.get("longitude")
    lat = float(lat) if lat is not None and not (isinstance(lat, float) and math.isnan(lat)) else None
    lon = float(lon) if lon is not None and not (isinstance(lon, float) and math.isnan(lon)) else None
    price = fnum(r.get("sold_price")) if kind_choice == "sold" else fnum(r.get("list_price"))
    sqft = fnum(r.get("sqft"))
    vals = dict(kind=kind_choice, address=_s(r.get("street")).strip() or addr, city=_s(r.get("city")) or prop["city"],
                zip=_s(r.get("zip_code")) or prop["zip"], price=price,
                ppsf=(price / sqft) if price and sqft else fnum(r.get("price_per_sqft")),
                beds=fnum(r.get("beds")), baths=fnum(r.get("full_baths")), sqft=sqft,
                year_built=int(r["year_built"]) if fnum(r.get("year_built")) else None,
                event_date=(_s(r.get("last_sold_date"))[:10] or None) if kind_choice == "sold" else (_s(r.get("list_date"))[:10] or None),
                dom=fnum(r.get("days_on_mls")), lat=lat, lon=lon,
                distance_mi=haversine_mi(prop["lat"], prop["lon"], lat, lon) if lat is not None else None,
                lot_sqft=fnum(r.get("lot_sqft")),
                ptype=_norm_ptype(_s(r.get("style")) or _s(r.get("property_type"))),
                source="HomeHarvest (Realtor.com) — added by address lookup",
                source_url=_s(r.get("property_url")), photo_url=_s(r.get("primary_photo")))
    new_cid = _insert_comp(prop, vals)
    flash(f"✓ Comp added from HomeHarvest: {vals['address']}, {vals['city']} — "
          f"{'sold' if kind_choice == 'sold' else 'active listing'} {('$' + format(int(price), ',')) if price else ''}.", "info")
    return redirect(url_for("property_page", pid=pid, tab="comps", view="sold", highlight=new_cid))

@app.route("/property/<int:pid>/comps/add-manual", methods=["POST"])
def add_comp_manual(pid):
    prop = get_prop(pid)
    kind_choice = request.form.get("kind_choice", "sold")
    if kind_choice not in ("sold", "active"): kind_choice = "sold"
    addr = (request.form.get("address") or "").strip()
    city = (request.form.get("city") or prop["city"] or "").strip()
    state = prop["state"] or "FL"
    zp = (request.form.get("zip") or prop["zip"] or "").strip()
    if not addr:
        flash("Manual comp needs at least a street address.", "error")
        return redirect(url_for("property_page", pid=pid, tab="comps", view="sold", addmanual=1))
    geo = geocode_arcgis(f"{addr}, {city}, {state} {zp}")
    lat = geo["lat"] if geo else None; lon = geo["lon"] if geo else None
    price = fnum(request.form.get("price"), None); sqft = fnum(request.form.get("sqft"), None)
    vals = dict(kind=kind_choice, address=addr, city=city, zip=zp, price=price,
                ppsf=(price / sqft) if price and sqft else None,
                beds=fnum(request.form.get("beds"), None), baths=fnum(request.form.get("baths"), None),
                sqft=sqft,
                year_built=int(fnum(request.form.get("year_built"), 0)) or None,
                event_date=(request.form.get("event_date") or "").strip() or None,
                dom=fnum(request.form.get("dom"), None), lat=lat, lon=lon,
                distance_mi=haversine_mi(prop["lat"], prop["lon"], lat, lon) if lat is not None else None,
                lot_sqft=fnum(request.form.get("lot_sqft"), None),
                ptype=None, source="Manual entry (added by user)",
                source_url=(request.form.get("source_url") or "").strip() or None, photo_url=None)
    new_cid = _insert_comp(prop, vals)
    flash(f"✓ Comp added manually: {addr}, {city} — it counts in pin/average math like a pulled comp.", "info")
    return redirect(url_for("property_page", pid=pid, tab="comps", view="sold", highlight=new_cid))

@app.route("/job/<int:jid>")
def job_status(jid):
    reap_stale_jobs()
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
    # the saved scenario now IS the working state; the autosave draft is redundant
    db().execute("DELETE FROM uw_drafts WHERE property_id=? AND strategy=?", (pid, strategy))
    db().commit()
    if strategy == "Fix & Flip" and res.get("rule70_mao"):
        pass
    flash(f"{strategy} scenario saved: " + (f"score {res['score']} ({res['label']})" if res.get("score") else f"profit ${res.get('profit'):,}"))
    return redirect(url_for("property_page", pid=pid, tab="underwriting"))

@app.route("/property/<int:pid>/uw-autosave", methods=["POST"])
def uw_autosave(pid):
    """v1.5 autosave: debounce-saved working draft per property+strategy (JSON body
    {strategy, state} where state is the canonical scenario-input shape). Restored by
    property_page ahead of saved scenarios, so tab switches and reloads keep edits."""
    p = get_prop(pid)
    data = request.get_json(force=True, silent=True) or {}
    strategy = data.get("strategy")
    state = data.get("state") or {}
    if strategy not in ("Fix & Flip", "Co-Living", "Short Term", "Mid Term", "Long Term"):
        return jsonify(ok=False, error="bad strategy"), 400
    if not isinstance(state, dict):
        return jsonify(ok=False, error="bad state"), 400
    res = compute_underwriting(strategy, state, p)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db().execute("""INSERT INTO uw_drafts(property_id,strategy,state,results,updated_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(property_id,strategy) DO UPDATE SET state=excluded.state,
                    results=excluded.results, updated_at=excluded.updated_at""",
                 (pid, strategy, json.dumps(state), json.dumps(res), now))
    db().commit()
    return jsonify(ok=True, saved_at=now)

@app.route("/scenario/<int:sid>/delete", methods=["POST"])
def del_scenario(sid):
    row = db().execute("SELECT property_id FROM scenarios WHERE id=?", (sid,)).fetchone()
    db().execute("DELETE FROM scenarios WHERE id=?", (sid,)); db().commit()
    return redirect(url_for("property_page", pid=row["property_id"], tab="underwriting"))

@app.route("/amortization")
def amortization_standalone():
    """v2.0: the amortization tool lives inside Underwriting now — keep the
    old standalone URL working as a redirect to the most recent property's
    underwriting tab (dashboard when there are no properties)."""
    row = db().execute("SELECT id FROM properties ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        return redirect(url_for("property_page", pid=row["id"], tab="underwriting"))
    return redirect(url_for("dashboard"))

@app.route("/property/<int:pid>/amortize", methods=["POST"])
def amortize(pid):
    p = get_prop(pid)
    principal = fnum(request.form.get("loan_amount"), 0) or 0
    rate = fnum(request.form.get("rate"), 0) or 0
    yrs = fnum(request.form.get("amort_years"), 0) or 0
    term = fnum(request.form.get("loan_years"), 0) or 0
    if principal <= 0 or rate <= 0 or yrs <= 0 or term <= 0:
        # v2.0 (Sherif clarification): compute ONLY from numbers the user typed.
        # The old `or 30` defaults invented a 30-year term on blank fields.
        flash("Enter the loan amount, rate, amortization and loan term — the calculator uses only the numbers you type.", "warn")
        return redirect(url_for("property_page", pid=pid, tab="underwriting"))
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

@app.route("/property/<int:pid>/documents/add", methods=["POST"])
def doc_add(pid):
    """v1.6 (item T): the checklist is no longer fixed — users add their own documents."""
    get_prop(pid)
    title = (request.form.get("title") or "").strip()
    cat = (request.form.get("category") or "Custom").strip() or "Custom"
    if not title:
        flash("Give the document a title first.", "error")
        return redirect(url_for("property_page", pid=pid, tab="documents"))
    try:
        db().execute("INSERT INTO documents(property_id,category,slot,notes) VALUES(?,?,?,?)",
                     (pid, cat, title, (request.form.get("notes") or "").strip() or None))
        db().commit()
        flash(f"✓ Document added: {title}", "info")
    except Exception:
        flash(f"A document named “{title}” already exists on this property.", "error")
    return redirect(url_for("property_page", pid=pid, tab="documents"))

@app.route("/document/<int:did>/delete", methods=["POST"])
def doc_delete(did):
    row = db().execute("SELECT property_id, slot FROM documents WHERE id=?", (did,)).fetchone()
    if row:
        db().execute("DELETE FROM documents WHERE id=?", (did,)); db().commit()
        flash(f"Document removed: {row['slot']}", "info")
        return redirect(url_for("property_page", pid=row["property_id"], tab="documents"))
    return redirect(url_for("index"))

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
    db().execute("""INSERT INTO assignments(property_id,contractor_id,trade,walkthrough,quote,notes,
                    visit_status,quote_status) VALUES(?,?,?,?,?,?,?,?)""",
                 (pid, request.form.get("contractor_id") or None, request.form.get("trade"),
                  request.form.get("walkthrough"), fnum(request.form.get("quote")), request.form.get("notes"),
                  request.form.get("visit_status") or "Contractor needed",
                  request.form.get("quote_status") or "Not requested"))
    db().commit(); return redirect(url_for("property_page", pid=pid, tab="contractors"))

# ------------------------------------------------- v1.7b: notes feed + per-trade contractors
@app.route("/property/<int:pid>/notes/add", methods=["POST"])
def add_note(pid):
    get_prop(pid)
    text = (request.form.get("text") or "").strip()
    if text:
        db().execute("INSERT INTO property_notes(property_id,author,text) VALUES(?,?,?)",
                     (pid, (request.form.get("author") or "You").strip() or "You", text))
        db().commit()
    return redirect(url_for("property_page", pid=pid, tab="overview"))

@app.route("/property/<int:pid>/notes/<int:nid>/edit", methods=["POST"])
def edit_note(pid, nid):
    """v2.0: notes feed edit — same PRG discipline as add (POST -> redirect),
    so a reload can never resubmit a stale edit over newer data."""
    get_prop(pid)
    text = (request.form.get("text") or "").strip()
    if text:
        db().execute("UPDATE property_notes SET text=? WHERE id=? AND property_id=?",
                     (text, nid, pid))
        db().commit()
    return redirect(url_for("property_page", pid=pid, tab="overview"))

@app.route("/property/<int:pid>/notes/<int:nid>/delete", methods=["POST"])
def delete_note(pid, nid):
    """v2.0: notes feed delete (POST -> redirect, PRG like add/edit)."""
    get_prop(pid)
    db().execute("DELETE FROM property_notes WHERE id=? AND property_id=?", (nid, pid))
    db().commit()
    return redirect(url_for("property_page", pid=pid, tab="overview"))

@app.route("/property/<int:pid>/trades/add", methods=["POST"])
def add_trade(pid):
    get_prop(pid)
    t = (request.form.get("trade_name") or "").strip()
    if t:
        db().execute("INSERT OR IGNORE INTO property_trades(property_id,trade) VALUES(?,?)", (pid, t))
        db().commit()
    return redirect(url_for("property_page", pid=pid, tab="contractors"))

@app.route("/property/<int:pid>/contractors/new-contact", methods=["POST"])
def assign_new_contact(pid):
    """ReiSearch's 'New Contact' assign path: the contact is saved ON THIS
    PROPERTY (a snapshot on the assignment row) — it does not create a
    directory entry or an account for the contractor."""
    get_prop(pid)
    company = (request.form.get("company") or "").strip()
    if not company:
        flash("Company is required for a new contact.")
        return redirect(url_for("property_page", pid=pid, tab="contractors"))
    db().execute("""INSERT INTO assignments(property_id,contractor_id,trade,walkthrough,quote,notes,
                    visit_status,quote_status,contact_company,contact_name,contact_phone,contact_email)
                    VALUES(?,NULL,?,?,?,?,?,?,?,?,?,?)""",
                 (pid, request.form.get("trade"), request.form.get("walkthrough"),
                  fnum(request.form.get("quote")), request.form.get("notes"),
                  request.form.get("visit_status") or "Contractor needed",
                  request.form.get("quote_status") or "Not requested",
                  company, request.form.get("name"), request.form.get("phone"), request.form.get("email")))
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
    v = clean_num(v)
    return "Not found" if v is None else f"${v:,.0f}"

@app.template_filter("num")
def numf(v, dec=0):
    v = clean_num(v)
    return "—" if v is None else f"{v:,.{dec}f}"

@app.template_filter("datefmt")
def datefmt(v):
    """ISO date -> ReiSearch's display form ('Oct 27, 2025')."""
    if not v: return "—"
    try: return datetime.datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
    except Exception: return str(v)

@app.template_filter("jload")
def jload(v):
    try: return json.loads(v) if v else {}
    except Exception: return {}

init_db()

if __name__ == "__main__":
    init_db()
    app.run(host=os.environ.get("PDA_HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", os.environ.get("PDA_PORT", "5050"))), debug=False)
