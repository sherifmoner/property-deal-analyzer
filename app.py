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
        # v58: a one-time disk I/O error killed a live pull (2026-11-06,
        # job 48) — a plain SELECT dying on a transient lock. Give
        # every connection a 30s busy window instead of failing fast.
        g.db.execute("PRAGMA busy_timeout=30000")
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
    # v56: the subject's Zillow zpid (persisted by the subject fill so the
    # zpid-only backup APIs can answer when the by-address path cannot) and
    # the stored official-PadSplit benchmark pull (Tier A room rents).
    for col in ("zpid", "padsplit_json", "padsplit_date"):
        if col not in cols: d.execute(f"ALTER TABLE properties ADD COLUMN {col} TEXT")
    ccols = [r[1] for r in d.execute("PRAGMA table_info(comps)")]
    if "ptype" not in ccols: d.execute("ALTER TABLE comps ADD COLUMN ptype TEXT")
    if "lot_sqft" not in ccols: d.execute("ALTER TABLE comps ADD COLUMN lot_sqft REAL")
    # v57: bulk-sale flag (Seaford defect) — a kept bulk-sale row is one
    # stored row for a whole complex's portfolio sale, flagged so no
    # average/median/ARV ever treats the complex total as a per-unit comp.
    if "bulk_sale" not in ccols: d.execute("ALTER TABLE comps ADD COLUMN bulk_sale INTEGER DEFAULT 0")
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

def find_subject_row(df, prop, exact_only=False):
    """Locate the subject's own row in a HomeHarvest frame: exact house number + street name,
    else the nearest row within 0.05 mi. Returns the row or None.
    v3.4: exact_only=True (the subject spec/photo FILL path) never falls back to a
    neighbor — a near row with the wrong house's specs/photos is how a subject
    record gets silently mutated (live Garland showed a neighbor's 2,172 sqft /
    1976). In exact mode the ZIP pre-filter is also dropped: an exact house
    number + street-name match is stronger evidence than a ZIP boundary."""
    if df is None or not len(df): return None
    pnum, pcore = _street_key(prop["street"] or prop["address"])
    best, best_dist = None, None
    for _, r in df.iterrows():
        if not exact_only and str(r.get("zip_code")) != str(prop["zip"]): continue
        lat, lon = r.get("latitude"), r.get("longitude")
        dist = None
        if lat is not None and lon is not None and not (isinstance(lat, float) and math.isnan(lat)):
            dist = haversine_mi(prop["lat"], prop["lon"], float(lat), float(lon))
        rnum, rcore = _street_key(_s(r.get("street")))
        if pnum and rnum == pnum and rcore and rcore == pcore:
            return r  # exact address match wins immediately
        if exact_only: continue
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

def arv_reconciliation(prop, comps, bulk_keys=None):
    """ONE reconciled ARV with the math shown: average of (adjusted avg comp price) and (avg $/sqft x subject sqft).
    v57: the pool is stats-usable sold rows only — a bulk-flagged row or
    a $/sqft outlier can never enter the ARV, even when pinned."""
    usable = _stats_usable_sold(comps, bulk_keys)
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

def verdict(prop, comps, boxes, bulk_keys=None):
    """Sticky verdict: score + confidence + MAO + buy-box PASS/FAIL."""
    rec = arv_reconciliation(prop, comps, bulk_keys)
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
    n_sold = len(_stats_usable_sold(comps, bulk_keys))  # v57: bulk/outlier rows are not market comps
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

def _cval(c, key, default=None):
    """One tolerant field read off a comp row (v57): sqlite3.Row raises
    on a missing key, a plain dict answers .get — gates hand both shapes
    to the stats helpers, so neither may crash the page."""
    try:
        return c[key]
    except Exception:
        pass
    try:
        return c.get(key, default)
    except Exception:
        return default

# ---------------------------------------------------------------- v57 bulk-sale stats defense
# Seaford defect (live 2026-11-05): the county stamps one apartment
# complex's $31M bulk/portfolio sale price on EVERY unit's parcel
# record, so ~120 units stored as ~120 independent $31M sales and the
# whole-set average ($4,727,190) and Market Data median ($500,000)
# followed them. Two layers stop it ever again:
#   (a) the merge gate collapses a bulk group to ONE flagged row
#       (see _bulk_group_key / _merge_sold_frame);
#   (b) this read-side predicate keeps bulk-flagged rows AND $/sqft
#       extreme outliers AND (v57.3) PRICE extreme outliers out of
#       EVERY stat — so even a bulk row that slipped in before the
#       fix (or a lone $31M/$35M singleton with no group to collapse
#       and whole-building sqft) can never move a headline again.
#       Rows stay LISTED in Comparables; only the stats ignore them.
_PPSF_OUTLIER_MULT = 20      # a sold row above 20x the pool median $/sqft is not a market comp
_PPSF_OUTLIER_MIN_N = 5      # ...but only when >= 5 rows carry a $/sqft, so the median is trustworthy
_PRICE_OUTLIER_MULT = 20     # v57.3: a sold row above 20x the pool median PRICE is not a market comp either
_PRICE_OUTLIER_MIN_N = 5     # ...but only when >= 5 rows carry a price, so the median is trustworthy

def _price_of(c):
    """Price for one sold row as a float, else None (v57.3 — mirrors _ppsf_of)."""
    p = _cval(c, "price")
    if p is None:
        return None
    try: return float(p)
    except Exception: return None

def _ppsf_of(c):
    """$/sqft for one sold row: the stored ppsf, else price/sqft, else
    None (a row with no sqft cannot be judged on $/sqft and is kept)."""
    p = _cval(c, "ppsf")
    if p:
        try: return float(p)
        except Exception: return None
    price, sqft = _cval(c, "price"), _cval(c, "sqft")
    if price and sqft:
        try: return float(price) / float(sqft)
        except Exception: return None
    return None

def _stats_usable_sold(rows, bulk_keys=None):
    """The sold rows any average/median/ARV/trend may use (v57, v57.1, v57.3):
    kind sold, priced, not excluded, not bulk-flagged, not a member of
    a bulk group (v57.1 — read-side detection over the STORED pool, so
    already-stored bulk rows are fixed without any DB write; ALL
    members are excluded, the kept representative included, because
    its price is the complex total, not a per-unit market price), and
    not a $/sqft extreme outlier, and not a PRICE extreme outlier
    (v57.3). The medians are computed over the bulk/excluded-free
    pool first (a median is robust while the outliers are a minority),
    then rows above 20x that median drop. The price guard is the
    symmetric twin of the $/sqft guard: it catches a bulk SINGLETON
    (live 2026-11-05: 14566 Seaford Cir 101, the only row for its
    building at $31M with whole-building sqft, so no grouping rule
    and no $/sqft rule can catch it) and any other lone $31M/$35M
    row, while genuine high-price sales ($1-3M = 2.5-7.6x the median)
    stay. bulk_keys may be supplied (the view computes it once over
    the full stored pool so a group split by a date window still
    counts); otherwise it is detected within rows. Callers that ALSO
    need the nominal filter (Market Data) apply it themselves, as
    before — this helper never invents a price rule."""
    if bulk_keys is None:
        bulk_keys = _bulk_group_keys(rows)
    base = [c for c in (rows or [])
            if _cval(c, "kind") == "sold" and _cval(c, "price")
            and not _cval(c, "excluded") and not _cval(c, "bulk_sale")
            and _bulk_group_key(c) not in bulk_keys]
    ppsfs = sorted(p for p in (_ppsf_of(c) for c in base) if p is not None)
    if len(ppsfs) >= _PPSF_OUTLIER_MIN_N:
        med = _median(ppsfs)
        if med:
            cap = med * _PPSF_OUTLIER_MULT
            base = [c for c in base if (_ppsf_of(c) is None or _ppsf_of(c) <= cap)]
    prices = sorted(p for p in (_price_of(c) for c in base) if p is not None)
    if len(prices) >= _PRICE_OUTLIER_MIN_N:
        med = _median(prices)
        if med:
            cap = med * _PRICE_OUTLIER_MULT
            base = [c for c in base if (_price_of(c) is None or _price_of(c) <= cap)]
    return base

def _pool_source_label(rows):
    """The provenance line for a comp pool (v48): the ACTUAL stored
    sources present in the rows, never a hardcoded origin — the audit
    found these headers claiming 'HomeHarvest rental comps
    (Realtor.com)' for pools that came entirely from RapidAPI.
    Distinct stored labels, in first-seen order; 'Not found' when the
    rows carry none."""
    seen = []
    for c in rows or []:
        try: s = c["source"]
        except Exception: s = None
        if s and s not in seen: seen.append(s)
    return " + ".join(seen) if seen else "Not found"

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
            "source": _pool_source_label(pool), "date": datetime.date.today().isoformat()}

def coliving_stats(rents, prop=None):
    """Co-Living benchmark. Tier A (v56): a stored official-PadSplit
    pull for THIS property wins — it is the area's actual listed
    $/wk per room (Sherif's PadSplit revenue rule, 2026-11-04).
    Tier B (the fallback, unchanged): derived honestly from
    whole-house rental comps — per-room monthly rent = listing rent /
    bedrooms; weekly = monthly x 12 / 52; median across comps with
    usable beds+rent; needs >= 3 comps, else Not found."""
    if prop is not None:
        _ta = _padsplit_benchmark(prop)
        if _ta: return _ta
    per_room_wk = []
    for c in rents:
        if c["price"] and c["beds"] and c["beds"] >= 1:
            per_room_wk.append(c["price"] / c["beds"] * 12 / 52)
    if len(per_room_wk) < 3: return None
    wk = _median(per_room_wk)
    return {"weekly_per_room": round(wk), "monthly_per_room": round(wk * 52 / 12), "n": len(per_room_wk),
            "method": "Median of (monthly rent ÷ bedrooms × 12 ÷ 52) across pulled whole-house rental comps — "
                      "an estimate of shared-bath room rent for this ZIP, not PadSplit listing data.",
            "source": "Derived from " + _pool_source_label(rents), "date": datetime.date.today().isoformat()}

def room_estimate_weekly(comp):
    """Per-room weekly rent estimate for one rental comp (same method as the benchmark)."""
    if comp["price"] and comp["beds"] and comp["beds"] >= 1:
        return round(comp["price"] / comp["beds"] * 12 / 52)
    return None

# ---------------------------------------------------------------- PadSplit Tier A (v56)
# Sherif's PADSPLIT REVENUE RULE (2026-11-04) always named the area's
# actual PadSplit rent per room as the standard; until now only the
# Tier-B whole-house derivation could answer. The official PadSplit
# search page server-renders its full result set as JSON inside
# <script id="__NEXT_DATA__"> (Next.js dehydrated react-query state —
# proven by direct fetch 2026-11-05, fixtures saved in research/
# padsplit-fixture-*.html): the "properties-list" query's data holds
# count / totalPages / results[], one card per listed home with
# roomMinPrice / roomMaxPrice (WEEKLY dollars), status (1 = listed,
# 0 = not yet bookable — "coming soon") and listingStatus. A plain
# requests fetch reads it; no key, no JS execution. Pagination is
# ?page=N. An unknown ZIP answers count=0.
PADSPLIT_SEARCH_BASE = "https://www.padsplit.com/rooms-for-rent/"
PADSPLIT_MIN_CARDS = 3     # below this the ZIP is too thin to benchmark
PADSPLIT_MAX_PAGES = 3     # city-widen fetch cap

def _padsplit_slug(city, state):
    """'Tampa' + 'FL' -> 'tampa-fl' (the verified URL form)."""
    c = re.sub(r"[^a-z0-9]+", "-", (city or "").strip().lower()).strip("-")
    s = (state or "").strip().lower()[:2]
    return f"{c}-{s}" if c and s else ""

def _padsplit_url(city, state, zipc=None, page=1):
    slug = _padsplit_slug(city, state)
    if not slug: return ""
    u = PADSPLIT_SEARCH_BASE + slug
    qs = []
    if zipc: qs.append(f"zip={str(zipc)[:5]}")
    if page and page > 1: qs.append(f"page={page}")
    return u + ("?" + "&".join(qs) if qs else "")

def _padsplit_parse(html_text):
    """One PadSplit search page -> {"cards": [...], "count": n,
    "total_pages": n}. Each card: {"rent": weekly dollars (the card's
    advertised lowest room price), "available": bool, "label": the
    listingStatus text}. Any shape surprise parses to zero cards —
    never an exception, never an invented rent."""
    out = {"cards": [], "count": 0, "total_pages": 0}
    if not html_text: return out
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html_text, re.S)
    if not m:
        # v58: no SSR payload — the text is reader markdown (the
        # reader-first fetch). Parse the listing lines instead.
        return _padsplit_parse_markdown(html_text)
    try:
        data = json.loads(m.group(1))
    except Exception:
        return out
    queries = _rapi_dig(data, "props", "pageProps", "dehydratedState", "queries")
    if not isinstance(queries, list): return out
    for q in queries:
        if not isinstance(q, dict): continue
        qk = q.get("queryKey")
        if not (isinstance(qk, list) and qk and qk[0] == "properties-list"): continue
        d = _rapi_dig(q, "state", "data")
        if not isinstance(d, dict): continue
        results = d.get("results")
        if not isinstance(results, list): continue
        for r in results:
            if not isinstance(r, dict): continue
            rent = _sabri_num(r.get("roomMinPrice"))
            if rent is None: rent = _sabri_num(r.get("minPriceWithoutDiscount"))
            if rent is None or rent <= 0: continue
            out["cards"].append({"rent": rent,
                                 "available": r.get("status") == 1,
                                 "label": _s(r.get("listingStatus"))})
        out["count"] = int(d.get("count") or len(out["cards"]))
        out["total_pages"] = int(d.get("totalPages") or 1)
        return out
    return out

def _padsplit_parse_markdown(md_text):
    """PadSplit search markdown (the jina-reader form) -> the same
    {"cards": [...], "count": n, "total_pages": 1} shape as
    _padsplit_parse. One card per 'Room for rent — $N per week — ...'
    line; the title/count lines ('415 Rooms for Rent ... from $156
    per week') are NOT listing lines and are never counted — the
    'Room for rent' (singular) prefix is the discriminator.
    Availability is unknowable from the reader markdown, so every card
    counts as available (the benchmark median uses all rents either
    way). Identical (rent, label) pairs dedupe within the page."""
    out = {"cards": [], "count": 0, "total_pages": 1}
    if not md_text: return out
    seen = set()
    for line in md_text.splitlines():
        s = line.strip()
        if not s.lower().startswith("room for rent"): continue
        m = re.search(r"\$([\d,]+)\s+per week", s)
        if not m: continue
        try:
            rent = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if rent <= 0: continue
        label = s[m.end():].strip(" —-").strip()
        key = (rent, label)
        if key in seen: continue
        seen.add(key)
        out["cards"].append({"rent": rent, "available": True, "label": label})
    out["count"] = len(out["cards"])
    return out

def _padsplit_fetch(url):
    """One PadSplit page fetch (browser UA — the site serves its SSR
    payload to ordinary agents). v58: the r.jina.ai reader is tried
    FIRST (PA's proxy 403-blocks padsplit.com directly, but the reader
    answered HTTP 200 with real rent data, 2026-11-06); the direct
    fetch stays as fallback. 15s cap per attempt. Returns the
    HTML/markdown or None."""
    if not url: return None
    for attempt_url in (f"{JINA_READER_BASE}{url}", url):
        try:
            r = requests.get(attempt_url, headers={"User-Agent":
                             "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
                             timeout=15)
            r.raise_for_status()
            return r.text
        except Exception as e:
            app.logger.warning(f"comp pull: PadSplit fetch failed for {attempt_url}: {e!r}")
    return None

def _padsplit_pull(prop):
    """Fetch the OFFICIAL PadSplit room rents for the subject (v56):
    the ZIP search page first; when the ZIP itself yields fewer than
    PADSPLIT_MIN_CARDS cards, widen to the city page (at most
    PADSPLIT_MAX_PAGES pages) and label the payload city-wide with
    the ZIP count noted. Returns the storable payload
    {"weekly_rents", "n", "scope", "zip_n", "url", "date",
    "available", "waitlist"} or None — any failure (blocked, no
    data, host egress) leaves the stored Tier-B benchmark path
    exactly as it was; the reason is logged at the failure point."""
    city, state = _s(prop["city"]), _s(prop["state"])
    zipc = str(prop["zip"] or "")[:5]
    if not city or not state or not zipc: return None
    today = datetime.date.today().isoformat()
    def _payload(cards, scope, zip_n, url):
        return {"weekly_rents": [c["rent"] for c in cards], "n": len(cards),
                "scope": scope, "zip_n": zip_n, "url": url, "date": today,
                "available": sum(1 for c in cards if c["available"]),
                "waitlist": sum(1 for c in cards if not c["available"])}
    zip_url = _padsplit_url(city, state, zipc)
    got = _padsplit_parse(_padsplit_fetch(zip_url) or "")
    zip_cards = got["cards"]
    if len(zip_cards) >= PADSPLIT_MIN_CARDS:
        return _payload(zip_cards, "zip", len(zip_cards), zip_url)
    city_cards, city_url = [], _padsplit_url(city, state)
    for page in range(1, PADSPLIT_MAX_PAGES + 1):
        got_c = _padsplit_parse(_padsplit_fetch(_padsplit_url(city, state, page=page)) or "")
        city_cards.extend(got_c["cards"])
        if len(city_cards) >= PADSPLIT_MIN_CARDS: break
        if page >= (got_c["total_pages"] or 1): break
    if city_cards:
        return _payload(city_cards, "city", len(zip_cards), city_url)
    app.logger.warning(f"comp pull: PadSplit pull for {prop['address']}: "
                       f"no room cards in ZIP {zipc} or the {city} pages")
    return None

def _padsplit_benchmark(prop):
    """The stored Tier-A benchmark for one property, in coliving_stats'
    dict shape — or None when no successful pull is stored (or the
    stored pull is too thin to benchmark). Median of ALL room cards
    the pull captured for the scope (available + waitlist, each card
    contributing its advertised weekly rent)."""
    try:
        raw = prop["padsplit_json"]
    except Exception:
        raw = None
    if not raw: return None
    try:
        ps = json.loads(raw)
    except Exception:
        return None
    rents = [float(x) for x in (ps.get("weekly_rents") or [])
             if isinstance(x, (int, float)) and x > 0]
    if len(rents) < PADSPLIT_MIN_CARDS: return None
    wk = _median(rents)
    if ps.get("scope") == "city":
        scope_txt = (f"the {prop['city']} area (city-wide listings — ZIP "
                     f"{str(prop['zip'] or '')[:5]} itself listed "
                     f"{ps.get('zip_n', 0)} room(s))")
    else:
        scope_txt = f"ZIP {str(prop['zip'] or '')[:5]}"
    try:
        pdate = prop["padsplit_date"]
    except Exception:
        pdate = None
    return {"weekly_per_room": round(wk), "monthly_per_room": round(wk * 52 / 12),
            "n": len(rents),
            "method": (f"Median of the {len(rents)} weekly room rents listed on "
                       f"PadSplit for {scope_txt} — available + waitlist rooms, "
                       f"one advertised rent per listed home. Actual PadSplit "
                       f"listing data (Tier A), not a whole-house estimate."),
            "source": "PadSplit (official listings)",
            "date": pdate or ps.get("date") or "",
            "padsplit_n": len(rents), "padsplit_scope": ps.get("scope") or "zip",
            "padsplit_url": ps.get("url") or ""}

def pinned_sold_stats(sold, bulk_keys=None):
    """Sold-view header averages from the PINNED set (falls back to top 3 by score, labeled).
    v57: the pool is stats-usable sold rows only — a pinned bulk row
    (or $/sqft outlier) drops out of the basis instead of pricing it."""
    pool = _stats_usable_sold(sold, bulk_keys)
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

def _eff_months(f):
    """v3.1 UNIFIED TERM RULE (ReiSearch decisive probe 2026-11-03): the
    effective amortization term in months = Amortization Term if set, else
    Loan Term if set, else 0 (interest-only). A blank Amortization Term
    (""/None/0) falls back to the Loan Term — it is NOT "not set".
    "Not set" survives only for a blank AMOUNT / Fixed-with-blank-amount."""
    av = f.get("amort_value")
    if av not in (None, "") and (fnum(av, 0) or 0) > 0:
        return (fnum(av, 0) or 0) * (1 if (f.get("amort_unit") or "Months") == "Months" else 12)
    tv = f.get("term_value")
    if tv not in (None, "") and (fnum(tv, 0) or 0) > 0:
        return (fnum(tv, 0) or 0) * (1 if (f.get("term_unit") or "Months") == "Months" else 12)
    return 0

def _norm_funding(f):
    """v1.5 funding-card normalization: legacy rows {type, amount, rate, term_years} gain the
    ReiSearch card fields (preset, loan type, position, term value+unit, points, fees,
    payment type, escrow). Defaults chosen so legacy payments are unchanged:
    either term present -> Amortized, neither term -> Interest Only.
    v2.3 TWO-TERM MODEL (ReiSearch pass 2026-11-03): cards carry a Loan Term
    (payment-invariant; amortization horizon/balloon only) AND an Amortization
    Term (drives the payment). Legacy rows carry only the single term — it WAS
    the payment term, so it migrates to BOTH fields and payments are unchanged.
    v3.1: a blank Amortization Term is NOT copied from the Loan Term at
    normalization time — the fallback is computed at pricing time, so a later
    Loan Term change still moves the payment (gate_v31 TM (j))."""
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
        a_months = _eff_months(f)  # v3.1: inference keys on the EFFECTIVE term (Amort else Loan)
        f["payment_type"] = "fixed" if fnum(f.get("fixed_payment"), 0) else ("amortized" if a_months else "interest_only")
    if f.get("fixed_payment") in (None, ""):
        f["fixed_payment"] = ""
    return f

def _amort_months(f):
    """The EFFECTIVE amortization term in months (v3.1 unified rule:
    Amortization Term if set, else Loan Term if set, else 0 =
    interest-only). This replaces the v2.3 amort-only reading; rows that
    never passed _norm_funding get the same fallback."""
    return _eff_months(f)

def _row_payment(f):
    """(principal+interest payment, escrow taxes+insurance) for one funding card.
    Payment types mirror ReiSearch: Amortized | Interest Only | Principal Only | Fixed Payment."""
    if (f.get("type") or "") in ("Cash", "Equity"):
        return 0.0, 0.0
    rate = fnum(f.get("rate"), 0) or 0
    months = _amort_months(f)  # v3.1: the EFFECTIVE term drives the payment (Amort else Loan else IO)
    pt = f.get("payment_type") or "amortized"
    # v1.8b: a Fixed payment IS the payment — it must not require a loan amount
    # (Subject-To cards have a blank amount by design; blank cards start at 0).
    if pt == "fixed":
        return (fnum(f.get("fixed_payment"), 0) or 0,
                (fnum(f.get("taxes_mo"), 0) or 0) + (fnum(f.get("insurance_mo"), 0) or 0))
    amt = fnum(f.get("amount"), 0) or 0
    if amt <= 0: return 0.0, 0.0
    esc = (fnum(f.get("taxes_mo"), 0) or 0) + (fnum(f.get("insurance_mo"), 0) or 0)
    if pt == "interest_only":
        pay = amt * rate / 100 / 12
    elif pt == "principal_only":
        # ReiSearch: Principal Only = monthly interest + straight-line principal
        # over the EFFECTIVE term; with no term at all the payment is
        # interest-only (v3.1 INFERRED — only Amortized measured, decisive
        # probe 2026-11-03: $1,756 "(no term set)").
        pay = amt * rate / 100 / 12 + (amt / months if months else 0)
    else:
        # _loan_payment with 0 years IS interest-only (v1.2 fallback) — so a
        # term-less Amortized card prices $1,756, exactly as ReiSearch does.
        # (v2.0's "not set" branch is SUPERSEDED by the decisive probe.)
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
    months = _amort_months(f)  # v3.1: payment (hence paydown) follows the EFFECTIVE term
    pt = f.get("payment_type") or "amortized"
    if pt == "interest_only":
        return 0.0
    if pt == "principal_only":
        return min(amt, amt / months * 12) if months else 0.0
    if pt == "fixed":
        pay = fnum(f.get("fixed_payment"), 0) or 0
    elif not months:
        return 0.0  # v3.1: no effective term -> interest-only pricing -> $0 paydown
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
           (257.0, 76),
           (571.15, 100), (658.65, 100), (696.15, 100)],
    "dscr": [(0.7312, 0), (0.925, 12), (0.9497, 15), (1.0303, 25), (1.0527, 29),
             (1.11, 40), (1.1996, 56), (1.3903, 79), (1.5316, 91), (1.8265, 97)],
    "be": [(0, 100), (4.58, 100), (53.6, 96), (55.5, 95), (61.14, 89), (63.68, 86),
           (70.86, 78), (76.58, 67), (80.74, 58), (82.68, 52), (89.5, 32),
           (91.89, 24), (111.76, 0)],
    "ds": [(-288.78, 0), (-126.07, 0), (-106.47, 0), (-50.63, 0), (-32.95, 14),
           (-0.63, 39), (49.92, 57), (124.8, 81), (125.83, 81), (130.62, 83),
           (172.02, 93), (188.0, 97), (589.04, 100), (626.54, 100)],
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
    # v3.7 (G14): the MD block's Principal, read up front — it is the
    # paydown source for the existing loan when present (see the funding
    # loop). Fix & Flip has no MD block.
    _md_prin = (fnum(inp.get("md_piti_principal"), 0) or 0) if strategy != "Fix & Flip" else 0.0
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
            # v3.7 (G14, loan-mechanics §19): when the Existing Mortgage
            # Data block carries a Principal figure, THAT is the existing
            # loan's paydown source — a Subject-To card for the same loan
            # must not ALSO book its fixed-payment principal split, or the
            # same principal is counted twice (proven: Senisa card + MD
            # Principal $1,228/mo read $29,472 = 2 x ReiSearch's pictured
            # $14,736). Other cards' paydown is separate debt and counts.
            if not (s.get("type") == "Subject-To" and _md_prin > 0):
                paydown_y1 += _paydown_y1(s)
            if esc > 0 or (s.get("payment_type") == "fixed" and pay > 0): piti_included = True
            if s.get("payment_type") == "fixed": carry_mo += pay + esc
            else: carry_mo += amt * (fnum(s.get("rate"), 0) or 0) / 100 / 12 + esc
            lab = s.get("loan_type") or s.get("type") or "Loan"
            if s.get("preset") and s.get("preset") != lab: lab = f"{lab} · {s['preset']}"
            if (s.get("payment_type") in ("amortized", "principal_only") and amt > 0
                    and not (fnum(s.get("amort_value"), 0) or 0) and not (fnum(s.get("term_value"), 0) or 0)):
                # v3.1: priced interest-only for lack of ANY term — ReiSearch's verbatim label.
                lab = f"{lab} — Interest only (no term set)"
            # v3.3: the Underwriting Summary's MONEY OUT block lists each loan
            # by its funding name with a payment-kind sublabel (pictured:
            # "Subject-To" / "P&I"). Additive fields — label/payment unchanged.
            _sub = {"fixed": "P&I", "amortized": "P&I", "interest_only": "Interest only",
                    "principal_only": "Principal only"}.get(s.get("payment_type") or "amortized", "P&I")
            debt_lines.append({"label": lab, "payment": round(pay + esc),
                               "name": s.get("preset") or s.get("type") or lab, "sub": _sub})
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
    opex_cats_computed = dict(opex_cats)
    # v3.3: category-total overrides (ReiSearch behavior — the category total
    # is editable independently of its sub-items and carries the money when
    # the user has taken manual control of it; the Senisa pictured state runs
    # 600/600/0/300/300 over $0 sub-items). The cat_manual list distinguishes
    # a typed total from the synced computed display value.
    _manual = set(x for x in str(inp.get("cat_manual") or "").split(",") if x)
    for _ck, _cat in (("fees", "Booking & Service Fees"), ("utilities", "Utilities"),
                      ("cleaning", "Cleaning Service"), ("maintenance", "Maintenance"),
                      ("management", "Management"), ("taxes", "Taxes, Insurance & HOA")):
        _ov = fnum(inp.get("cat_override_" + _ck), None)
        if _ov is not None and _ck in _manual: opex_cats[_cat] = round(_ov)
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
                "opex_cats_computed": opex_cats_computed,
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
            # v3.3 (Phase 3, methodology doc 2026-11-04): the fresh rent is the
            # area benchmark ONLY — the old hardcoded `or 189` fallback (ReiSearch's
            # ZIP-33613 Tier-A figure) fired on ANY property whose comp stats came
            # back empty, i.e. an invented rent. Below the data minimum the rooms
            # now prefill BLANK ("Not found — enter per-room rent" note on the page).
            wk = (ctx.get("coli") or {}).get("weekly_per_room")
            rows = [{"bath": "Shared", "bath_group": "A" if (i // 2) % 2 == 0 else "B",
                     "rent_wk": wk if wk else "", "rent_mo": round(wk * 52 / 12, 2) if wk else "",
                     "vacancy": 15, "amenity_mo": 0}
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
        # v3.3: ReiSearch category totals are editable independently of their
        # sub-items (Senisa pictures: totals 600/600/0/300/300 with every
        # sub-item $0). Blank = computed from sub-items; a value overrides.
        for _ck in ("fees", "utilities", "cleaning", "maintenance", "management", "taxes"):
            st["cat_override_" + _ck] = g("cat_override_" + _ck, "")
        st["cat_manual"] = g("cat_manual", "")
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
def _nn(v):
    """fnum with pandas NaN normalized to None (NaN is truthy — a NaN price
    used to slip past the 'no price' guard and inflate usable counts)."""
    x = fnum(v)
    return None if (x is not None and isinstance(x, float) and math.isnan(x)) else x

def _source_label(url, rent=False):
    """Comp source label from the listing URL's own host, so a frame pulled
    from any site labels itself honestly (frames today are Realtor's; the
    merge accepts any frame with the standard HomeHarvest columns)."""
    u = (url or "").lower()
    if "zillow." in u: site = "Zillow"
    elif "redfin." in u: site = "Redfin"
    elif "realtor." in u: site = "Realtor.com"
    else: site = None
    if site is None: return "HomeHarvest (rentals)" if rent else "HomeHarvest"
    return f"HomeHarvest ({site} rentals)" if rent else f"HomeHarvest ({site})"

_UNIT_RE = re.compile(r"(?:apartment|apt|unit|suite|ste|#)\s*[-#]?\s*([a-z0-9]+)", re.I)

def _unit_plausible(tok):
    """Whether a token captured after a unit keyword plausibly IS a
    unit designator (v49): it contains a digit ("208", "4b") or is a
    single letter ("A", "B"). A bare alphabetic word is not a unit —
    it is a slug/geography artifact ("# Usa" in one Sabri row's stored
    address, "-Usa_" in a Realtor URL) or the tail of a street word
    the keyword hid inside of ("United" -> "ed")."""
    t = (tok or "").lower()
    return bool(t) and (any(ch.isdigit() for ch in t) or len(t) == 1)

def _unit_designator(addr, source_url=None):
    """The unit designator of a comp row, or "" when it has none (v48).
    Read first from the street address itself ("123 Main St Unit 5"),
    then from the stored listing URL, whose path often carries it when
    the address doesn't (Realtor's "...-Unit-208_..." form — the
    shipped DB's 14522 N Nebraska Ave pair is told apart ONLY this
    way). Deliberately narrow: the token must follow a unit keyword or
    '#', so lookalikes never match ("-Usa_" in a Realtor URL is a
    geography artifact, not a unit). v49: a captured token counts only
    when it is PLAUSIBLE (_unit_plausible) — the v48 form promoted the
    "# Usa" slug tail stored in one live row's address into a fake
    unit "usa", giving the artifact row an identity of its own so no
    dedupe or collapse could ever fold it into its clean twin. An
    implausible capture no longer ends the search: a later plausible
    token in the same or the next text still wins. "Ste" joins the
    keyword list under the same guard ("Ste B" -> "b"; the Sainte of
    "Ste Marie" captures "marie", implausible, and is skipped)."""
    for text in (addr or "", source_url or ""):
        if not text: continue
        flat = str(text).replace("-", " ").replace("_", " ").replace("/", " ")
        for m in _UNIT_RE.finditer(flat):
            if _unit_plausible(m.group(1)): return m.group(1).lower()
    return ""

def _addr_unit_split(addr):
    """(street_text, unit) for one stored address (v50): the unit
    segment — the same plausible keyword+token _unit_designator would
    read — REMOVED from the address text, and the unit token itself
    ("" when the address carries no plausible unit). The v49 collapse
    keyed groups on _norm_addr(address) verbatim, so a unit living IN
    the address text ("... Unit 208" / "... # 208" — the RapidAPI
    storage shape) polluted the normalized street ("14522 n nebraska
    ave unit 208" / "... ave 208") while the unit-in-URL shape
    (HomeHarvest) keyed clean — the same home at the same unit keyed
    as two different groups and the live twin never folded. Identity
    is now derived from the cleaned display identity: this stripped
    street, combined with the unit designator."""
    flat = str(addr or "").replace("-", " ").replace("_", " ").replace("/", " ")
    for m in _UNIT_RE.finditer(flat):
        if _unit_plausible(m.group(1)):
            street = " ".join((flat[:m.start()] + " " + flat[m.end():]).split())
            return street, m.group(1).lower()
    return (addr or ""), ""

def _is_subject_addr(prop, addr):
    """Whether a frame row's street address IS the subject property's
    own address (v50). A property must never be its own comp: once the
    Cadastral frame served, 3907 Spence Ave's own parcel row stored as
    a sold comp for itself and auto-pinned at 0.00 mi — and the merges
    carried no subject comparison at all, so EVERY frame shape
    (Cadastral / RapidAPI / HomeHarvest) leaked the subject's own row
    the same way. The comparison is on the unit-stripped normalized
    street (a subject's own unit row is still the subject). Subject
    ENRICHMENT is unaffected: it reads the frames themselves
    (find_subject_row), never the comps table."""
    if not addr: return False
    subj_street = prop["street"] or (prop["address"] or "").split(",")[0]
    row_street, _ru = _addr_unit_split(addr)
    subj_clean, _su = _addr_unit_split(subj_street)
    return _norm_addr(row_street) == _norm_addr(subj_clean)

_STREET_TYPE = {"ct": "ct", "court": "ct", "ave": "ave", "avenue": "ave",
                "st": "st", "street": "st", "dr": "dr", "drive": "dr",
                "rd": "rd", "road": "rd", "ln": "ln", "lane": "ln",
                "blvd": "blvd", "boulevard": "blvd", "cir": "cir", "circle": "cir",
                "ter": "ter", "terrace": "ter", "pl": "pl", "place": "pl",
                "trl": "trl", "trail": "trl", "hwy": "hwy", "highway": "hwy",
                "way": "way", "pkwy": "pkwy", "parkway": "pkwy"}

def _street_type(street_text):
    """The canonical street-TYPE suffix of an address (ct/ave/st/...)
    or None when the address carries none. The last token decides:
    '110 Garland Ct' -> 'ct', '1210 E 140th Ave' -> 'ave'."""
    toks = re.findall(r"[a-z0-9]+", (street_text or "").lower())
    if toks and toks[0].isdigit(): toks = toks[1:]
    return _STREET_TYPE.get(toks[-1]) if toks else None

def _same_street(prop, comp_addr):
    """0 when the comp sits on the SUBJECT'S OWN STREET, else 1 (v56 —
    Sherif's same-street-first rule, 2026-11-05: "It has to pull comps
    on the same street first"). Street identity is the _street_key
    CORE (street name minus suffixes/directionals/house number) of
    the unit-stripped address on both sides, so a different house
    number on the subject's street counts and a unit-suffixed twin
    of the subject counts. One guard on top of core equality: when
    BOTH addresses carry a street-TYPE suffix, the types must agree
    (canonicalized, so Ct == Court) — _street_key strips the type by
    design, and without the guard "Garland Ave" would claim the
    street of a "Garland Ct" subject (Main St is not Main Ave). A
    missing suffix on either side is neutral. This is the FIRST sort
    key of the sold/rent views and of the pull-time auto-pin — ahead
    of comp_tier, which is deliberately left unchanged: ReiSearch
    (measured live 2026-11-05) orders pinned -> weighted score and
    holds ZERO same-street comps for Garland, so this rule is
    Sherif's own, beyond ReiSearch."""
    if not comp_addr: return 1
    subj_street = prop["street"] or (prop["address"] or "").split(",")[0]
    s_clean, _su = _addr_unit_split(subj_street)
    c_clean, _cu = _addr_unit_split(comp_addr)
    _sn, s_core = _street_key(s_clean)
    _cn, c_core = _street_key(c_clean)
    if not (s_core and c_core and s_core == c_core): return 1
    s_type, c_type = _street_type(s_clean), _street_type(c_clean)
    if s_type and c_type and s_type != c_type: return 1
    return 0

_ADDR_ARTIFACT_RE = re.compile(r"#\s*[-#]?\s*[a-z]{2,}\s*$", re.I)

def _strip_addr_artifact(s):
    """Remove a slug-artifact tail from a street address (v49): a
    trailing "# <word>" whose token is a bare alphabetic word of 2+
    letters ("# Usa" — a Realtor URL-slug tail that leaked into one
    live Sabri row's stored address) is not a unit and not part of the
    street; it is stripped wherever the address is normalized
    (_norm_addr — the dedupe/collapse keys) or displayed
    (_collapse_rent_view's display copies), so the artifact row folds
    into its clean twin and the tail never renders. A trailing
    "# 4B" / "# A" (digit or single letter — plausible units, see
    _unit_plausible) is KEPT. Stored rows are never rewritten; this is
    a normalization/display rule only."""
    return _ADDR_ARTIFACT_RE.sub("", s or "").rstrip(" ,")

def _dates_match(stored, incoming):
    """Sale-date equality for the sold dedupe key (v48): equal dates
    match; a MONTH-precision date ("2024-03" — the FL Cadastral form)
    also matches a full date inside that month, in either direction,
    so the county record and a listing source's record of the SAME
    sale merge instead of duplicating. Two full dates must be equal;
    a missing date on either side matches (nothing to contradict)."""
    a, b = _s(stored)[:10], _s(incoming)[:10]
    if not a or not b: return True
    if a == b: return True
    if len(a) in (4, 7) and b.startswith(a): return True
    if len(b) in (4, 7) and a.startswith(b): return True
    return False

def _find_existing_comp(d, pid, kind, addr, price, beds=None, event_date=None, source_url=None):
    """Dedupe keys (v48, from the 2026-11-05 rent audit):
    rent — normalized address + unit designator + beds, PRICE REMOVED:
    the same home re-listed (or cross-quoted) at a new rent used to
    store a twin card; now the stored row UPDATES in place, latest
    pull wins. The unit designator keeps genuinely distinct units of
    one address distinct (it lives in the address or only in the
    listing URL — both are read).
    sold/active — normalized address + price + event_date (v3.4's
    address+price, plus the sale date so two genuinely different
    sales of one house at one price both survive; month-precision
    dates match their full-date twins, see _dates_match)."""
    want = _norm_addr(addr)
    if kind == "rent":
        want_unit = _unit_designator(addr, source_url)
        for r in d.execute("SELECT id, address, beds, source_url FROM comps "
                           "WHERE property_id=? AND kind='rent'", (pid,)).fetchall():
            if _norm_addr(r["address"]) != want: continue
            if _unit_designator(r["address"], r["source_url"]) != want_unit: continue
            rb, nb = r["beds"], beds
            if rb is not None and nb is not None and float(rb) != float(nb): continue
            return r["id"]
        return None
    for r in d.execute("SELECT id, address, event_date FROM comps WHERE property_id=? AND kind=? AND price=?",
                       (pid, kind, price)).fetchall():
        if _norm_addr(r["address"]) == want and _dates_match(r["event_date"], event_date):
            return r["id"]
    return None

def _row_gate(row, prop, radius_mi, price_key, horizon_cutoff=None):
    """The shared row-acceptance core (v43): coordinates present and
    numeric, distance from the subject within the radius, a street
    address, and a usable price read from price_key ("sold_price" for
    sold rows, "list_price" for rents). _sold_row_gate /
    _rent_row_gate are the two specializations; each is called by
    BOTH its merge (the store decision) and the pool's in-gate
    counter for that kind, so counter and merge can never diverge
    again within a kind."""
    lat, lon = row.get("latitude"), row.get("longitude")
    if lat is None or lon is None: return False, "no_coords", None
    try: flat, flon = float(lat), float(lon)
    except (TypeError, ValueError): return False, "no_coords", None
    if math.isnan(flat) or math.isnan(flon): return False, "no_coords", None
    dist = haversine_mi(prop["lat"], prop["lon"], flat, flon)
    if dist is None: return False, "no_coords", None
    if dist > radius_mi: return False, "outside_radius", dist
    if not _s(row.get("street")).strip(): return False, "no_street", dist
    if not _nn(row.get(price_key)): return False, "no_price", dist
    if horizon_cutoff:
        _dt = _s(row.get("last_sold_date"))[:10]
        if _dt and _dt < horizon_cutoff: return False, "outside_window", dist
    return True, None, dist

def _sold_row_gate(row, prop, radius_mi, horizon_cutoff=None):
    """THE sold-row acceptance test (v43) — ONE implementation, called
    by BOTH the sold merge (its store decision) and the RapidAPI pool's
    in-gate counter (pagination early-stop + union top-up sequencing),
    so the two can never diverge again. The v42 live failure: the
    counter tested coordinates + distance only, while the merge ALSO
    required a street address and a usable sold price. On the live
    West Chicago payload all 45 rows the counter called in-gate failed
    the merge's price test — ReefAPI's sold search returns records
    with no last_sold_price (not-recently-sold / listing-type records;
    the normalizer rightly never substitutes the list price) — so the
    pull stored 0, and the inflated count (45 >= the "rich"
    threshold) suppressed the Redfin/Sabri top-up whose rows do carry
    sold prices. Returns (ok, reason, dist); reason is one of
    no_coords / outside_radius / no_street / no_price /
    outside_window when not ok, else None.
    horizon_cutoff (ISO date) is OPTIONAL and neither caller passes
    one today: the merge stores a set deliberately deeper than any
    single view window (the views window it), and the pool adapters
    pre-filter by their own horizon before counting. The rule lives
    here so any future caller applies the SAME test instead of
    growing a third variant. Dedupe is NOT part of this test: it is
    merge-side bookkeeping — a duplicate updates the stored row in
    place and counts as accepted (the merge's usable count has always
    included duplicates). One deliberate tightening vs the pre-v43
    merge: a NaN longitude used to slip past its distance check and
    store a NaN distance; unplottable coordinates are no_coords here,
    like a NaN latitude always was."""
    return _row_gate(row, prop, radius_mi, "sold_price", horizon_cutoff)

def _rent_row_gate(row, prop, radius_mi):
    """THE rent-row acceptance test (v43): the _row_gate specialization
    for rental rows (price read from list_price), called by BOTH the
    rent merge (its store decision) and the pool's rent in-gate
    counter — the rent outcome previously borrowed the SOLD counter,
    which reads sold_price and so counted every (priceless-in-sold-
    terms) rent row 0 in-gate once the sold gate became strict."""
    return _row_gate(row, prop, radius_mi, "list_price")

def _drop_note(drops, reason, row):
    """Record one non-stored row in a merge drop collector (v43):
    drops[reason] = [count, [up to 2 example addresses]]. The example
    is the row's street when it has one, else its city/ZIP in
    parentheses (a no-street row has no address to show)."""
    slot = drops.setdefault(reason, [0, []])
    slot[0] += 1
    if len(slot[1]) < 2:
        ex = _s(row.get("street")).strip()
        if not ex:
            ex = f"({_s(row.get('city'))} {_s(row.get('zip_code'))})".strip() or "(no address)"
        slot[1].append(ex)

# ---------------------------------------------------------------- v57 bulk-sale merge gate
# Seaford defect (live 2026-11-05): the county stamps ONE apartment
# complex's bulk/portfolio sale price on EVERY unit's parcel record
# (14595-14599 Seaford Cir, units 101-206, $31,000,000, sold 2025),
# and the merge stored each unit as its own $31M sale. Root cause:
# the merge had no notion of a repeated sale — it dedupes on
# address+price+date, and every unit IS a different address.
# The rule below is deliberately narrow, so a genuine sale is never
# collapsed: identical price AND identical sale PERIOD (year-month
# when the record carries one, else the year alone — v57.2: the
# Cadastral layer emits SALE_YR1 without SALE_MO1, so every live
# Cadastral row is year-only, and the old month requirement returned
# None for all of them) AND the same building (same street number +
# street name, unit designator stripped), repeated >= 3 times in ONE
# incoming frame. A group of 2, a different price, a different
# period, or a different street number is not a bulk group. A
# year-only row never groups with a year-month row (no month is
# fabricated). The kept row is the lowest unit number and
# keeps the county's $31M total untouched — a per-unit price is NEVER
# invented (total / units is forbidden). The kept row is flagged
# bulk_sale=1 so _stats_usable_sold keeps it out of every average,
# median, ARV and trend; it stays listed with a badge.
# v57.1: the live Cadastral layer carries condo units as a BARE
# trailing number with NO Unit/# keyword ("14599 SEAFORD CIR 201"),
# and _addr_unit_split only strips keyworded units — so every live
# unit keyed as its own building and B1 could never group (or collapse)
# a re-pulled frame; the live 0-new-rows re-pull was dedupe, not a
# collapse. _bulk_group_key now also strips that bare trailing unit
# from the NORMALIZED base (a trailing number after a street suffix
# only), so keyword + bare forms unify to one key. _addr_unit_split
# itself is untouched — no dedupe / subject / same-street change.
_BULK_MIN_GROUP = 3

# Street suffixes (canonical, post-_norm_addr) after which a bare
# trailing number is a unit ("cir 201", "st 5"). "hwy" is deliberately
# ABSENT: "1234 US Highway 41" carries the highway number, not a unit.
_BULK_STRIP_SUFFIXES = frozenset({"pl", "dr", "ct", "ave", "st", "ln", "rd",
                                  "cir", "blvd", "ter", "trl", "way", "pkwy"})

def _strip_bare_unit_norm(base):
    """Strip a bare trailing unit number from a NORMALIZED base address
    (v57.1): the Cadastral layer emits condo units as a bare number
    ("14599 seaford cir 201") with no Unit/# keyword, so without this
    every unit keys as a different building. A trailing purely-numeric
    token is a unit ONLY when it follows a street suffix; a number that
    IS the street ("314 e 137th ave") sits mid-address and is never
    stripped, and a highway number is never stripped (no "hwy" in the
    suffix set)."""
    toks = (base or "").split()
    if len(toks) >= 3 and toks[-1].isdigit() and toks[-2] in _BULK_STRIP_SUFFIXES:
        return " ".join(toks[:-1])
    return base

def _bulk_group_key(vals):
    """The bulk-group key for one candidate sold row, or None when the
    row cannot be grouped (no price, or no sale date at all). The key
    is (unit-stripped normalized base address, price, sale PERIOD):
    "14595 Seaford Cir Unit 101", "... Unit 204" AND the live bare
    form "14595 Seaford Cir 204" key together; "14596 Seaford Cir
    Unit 101" keys apart (a different building number is a different
    group — the live complex spans 14595-14599 with ~24 units per
    number, so every number-group independently meets the >= 3 rule
    and collapses to its own kept row). v57.2: the period is the
    stored date's year-month when present ("2025-06"), else the year
    alone ("2025") — the Cadastral layer emits SALE_YR1 without
    SALE_MO1, so every live Cadastral row is year-only, and the old
    7-char month requirement returned None for all of them (v57.1
    never fired live). A year-only row groups ONLY with same-building
    + same-price + same-year rows; a year-month row ONLY with
    same-month rows — mixed month/no-month rows do NOT group, because
    no month is fabricated. Reads via _cval so it works on stored
    sqlite3.Rows as well as frame dicts (the read-side path, v57.1)."""
    price = _cval(vals, "price")
    if not price: return None
    raw = _s(_cval(vals, "event_date"))
    period = raw[:7] if len(raw) >= 7 else (raw[:4] if len(raw) >= 4 else None)
    if not period: return None
    street, _unit = _addr_unit_split(_cval(vals, "address"))
    base = _strip_bare_unit_norm(_norm_addr(street))
    if not base: return None
    return (base, price, period)

def _bulk_rep_key(vals):
    """Sort key picking the group's representative: the lowest unit
    number first (Unit 101 before Unit 204); a unitless address (or a
    non-numeric unit) sorts after numbered units, frame order breaks
    the remaining ties at the call site."""
    _st, unit = _addr_unit_split(vals.get("address"))
    try: return (0, int(unit))
    except Exception: return (1, unit or "")

def _bulk_split(cands):
    """(kept, skip) index sets for one frame's candidate rows: every
    bulk group (>= _BULK_MIN_GROUP sharing one _bulk_group_key) keeps
    exactly its representative and skips the rest. Non-group rows are
    in neither set."""
    groups = {}
    for i, vals in enumerate(cands):
        k = _bulk_group_key(vals)
        if k is None: continue
        groups.setdefault(k, []).append(i)
    kept, skip = set(), set()
    for idxs in groups.values():
        if len(idxs) < _BULK_MIN_GROUP: continue
        rep = min(idxs, key=lambda i: (_bulk_rep_key(cands[i]), i))
        kept.add(rep)
        skip.update(j for j in idxs if j != rep)
    return kept, skip

def _bulk_group_keys(rows):
    """The bulk-group keys present >= _BULK_MIN_GROUP times among rows
    (v57.1, read-side; v57.2: the period is year-month when present,
    else the year alone — Cadastral rows are year-only): identical
    price + sale period + building (street number + street name, unit
    stripped — including the bare trailing unit the Cadastral layer
    emits) repeated >= 3 times is the bulk-sale fingerprint. Works on
    dicts and sqlite3.Rows (via _cval); rows without a price or any
    sale date never key, and a group of 2 is within coincidence and
    does not count. Callers pass the result into _stats_usable_sold
    so a group split by a date window still counts (a lone member
    inside a window is still bulk)."""
    counts = {}
    for c in (rows or []):
        if _cval(c, "kind") not in (None, "sold"):
            continue
        k = _bulk_group_key(c)
        if k is None: continue
        counts[k] = counts.get(k, 0) + 1
    return {k for k, n in counts.items() if n >= _BULK_MIN_GROUP}

def _merge_sold_frame(d, prop, df, radius_mi, subj, source_override=None, touched=None, drops=None):
    """Merge one HomeHarvest sold frame into the comps table. Returns
    (added, updated, usable). v3.4: DISTANCE from the subject governs
    inclusion — the ZIP-equality post-filter is gone (a capped whole-ZIP
    frame plus the ZIP gate crowded out / dropped nearer same-street sales
    across ZIP lines, Sherif 2026-11-04). Insert-only: an existing
    (normalized address, price) row updates in place, never duplicates.
    source_override (RapidAPI frames): the honest transport label for the
    whole frame; default None keeps the per-row URL-derived label.
    touched (v42, optional): a list the merge appends (comp_id,
    stored_label) to for every row it stores, in merge order — the
    caller (pull_comps) uses it to attribute stored rows to sources for
    the truthful banner. Default None = no tracking, behavior unchanged.
    drops (v43, optional): a drop collector dict (see _drop_note) the
    merge fills with the reason every non-stored row was rejected, as
    decided by the SHARED acceptance test _sold_row_gate — pull_comps
    passes one for RapidAPI frames and logs the per-reason counts, so
    a served-but-not-stored pull names the exact filter that ate the
    rows. Default None = no accounting, behavior unchanged.
    v50: the SUBJECT's own row is never stored (see _is_subject_addr
    — every frame shape leaked it; the row still reaches subject
    enrichment, which reads the frames, not this table). And a
    CADASTRAL row whose sale price is nominal (< _CAD_NOMINAL_MAX)
    stores with excluded=1 at INSERT only: the county record exists
    and may matter for history, but a $100 quitclaim-style transfer
    is not a market comp — it stays out of the Sold view, averages,
    ARV and auto-pinning. The update path never sets excluded: a
    nominal cadastral row landing on an existing comp must not
    poison a good comp another source stands behind. The threshold
    is Cadastral-only — listing-source rows keep their behavior.
    v57: BULK-SALE collapse (see _bulk_group_key) — the merge is
    two-pass: pass 1 builds every gate-passed row's vals, pass 2
    stores them, skipping non-representative members of a bulk group
    (drop reason "bulk_sale") and flagging the kept representative
    bulk_sale=1. Already-stored rows are NEVER rewritten by this:
    a bulk member that was stored before v57 stays stored (its
    representative updates in place and gains the flag; the other
    members are left for the approved exclude-flag cleanup), and the
    update path never sets excluded — the v50 posture, kept."""
    added = updated = n = 0
    if df is None: return added, updated, n
    # --- pass 1: build the candidate rows (gate + subject check only) ---
    cands = []
    for _, r in df.iterrows():
        ok, reason, dist = _sold_row_gate(r, prop, radius_mi)
        if not ok:
            if drops is not None: _drop_note(drops, reason, r)
            continue
        lat, lon = float(r.get("latitude")), float(r.get("longitude"))
        addr = _s(r.get("street")).strip()
        if _is_subject_addr(prop, addr): continue
        price = _nn(r.get("sold_price")); sqft = _nn(r.get("sqft"))
        n += 1
        vals = dict(kind="sold", address=addr, city=_s(r.get("city")) or prop["city"],
                    zip=_s(r.get("zip_code")), price=price,
                    ppsf=(price / sqft) if price and sqft else _nn(r.get("price_per_sqft")),
                    beds=_nn(r.get("beds")), baths=_nn(r.get("full_baths")), sqft=sqft,
                    year_built=int(_nn(r.get("year_built"))) if _nn(r.get("year_built")) else None,
                    event_date=_s(r.get("last_sold_date"))[:10] or None,
                    dom=_nn(r.get("days_on_mls")), lat=float(lat), lon=float(lon), distance_mi=dist,
                    lot_sqft=_nn(r.get("lot_sqft")),
                    ptype=_norm_ptype(_s(r.get("style")) or _s(r.get("property_type"))),
                    source=source_override or _source_label(_s(r.get("property_url"))), source_url=_s(r.get("property_url")),
                    photo_url=_s(r.get("primary_photo")))
        vals["match_score"] = match_score(subj, {"distance_mi": dist, "sqft": sqft, "beds": vals["beds"],
                                                 "baths": vals["baths"], "year_built": vals["year_built"],
                                                 "lot_sqft": vals["lot_sqft"]})
        cands.append((r, vals))
    kept, skip = _bulk_split([v for _r, v in cands])
    # --- pass 2: store (bulk members collapse, representative flagged) ---
    for i, (r, vals) in enumerate(cands):
        if i in skip:
            if drops is not None: _drop_note(drops, "bulk_sale", r)
            continue
        if i in kept:
            vals = dict(vals, bulk_sale=1)
        addr, price = vals["address"], vals["price"]
        ex = _find_existing_comp(d, prop["id"], "sold", addr, price,
                                 event_date=vals["event_date"])
        cols = ",".join(vals.keys()); ph = ",".join("?" * len(vals))
        if ex:
            d.execute(f"UPDATE comps SET {','.join(k+'=?' for k in vals)} WHERE id=?", (*vals.values(), ex)); updated += 1
            if touched is not None: touched.append((ex, vals["source"]))
            if drops is not None: _drop_note(drops, "duplicate", r)
        else:
            ivals, icols, iph = vals, cols, ph
            if source_override == CADASTRAL_SRC and price is not None and price < _CAD_NOMINAL_MAX:
                ivals = dict(vals, excluded=1)
                icols = ",".join(ivals.keys()); iph = ",".join("?" * len(ivals))
            _cur = d.execute(f"INSERT INTO comps(property_id,{icols}) VALUES(?,{iph})", (prop["id"], *ivals.values())); added += 1
            if touched is not None: touched.append((_cur.lastrowid, vals["source"]))
    return added, updated, n

def _merge_rent_frame(d, prop, df, radius_mi, subj, source_override=None, drops=None):
    """Merge one HomeHarvest for_rent frame. Returns (added, updated,
    usable). v3.4: same distance-governs rule as the sold merge (no
    ZIP gate). source_override: see _merge_sold_frame.
    drops (v45, optional): the same drop collector the sold merge
    takes (see _drop_note / _merge_sold_frame) — pull_comps passes one
    for RapidAPI frames and logs the per-reason counts, so a
    served-but-not-stored rent pull names the exact filter that ate
    the rows (the v43/v44 live pulls logged "served 13 rows, 0
    in-gate" with no reason anywhere). Default None = no accounting,
    behavior unchanged. The return gained `updated` (v45) for that
    line's duplicate count; it was bookkeeping the merge did silently
    before."""
    added = updated = n = 0
    if df is None: return added, updated, n
    for _, r in df.iterrows():
        # v43: the store decision is the SHARED rent acceptance test
        # (_rent_row_gate) — the same test the pool's rent in-gate
        # counter uses, so the two cannot diverge (see _sold_row_gate).
        ok, reason, dist = _rent_row_gate(r, prop, radius_mi)
        if not ok:
            if drops is not None: _drop_note(drops, reason, r)
            continue
        lat, lon = float(r.get("latitude")), float(r.get("longitude"))
        addr = _s(r.get("street")).strip(); price = _nn(r.get("list_price"))
        # v50: the subject's own rental row is not a rental comp either
        # (same shared store gate as the sold merge — _is_subject_addr).
        if _is_subject_addr(prop, addr): continue
        sqft = _nn(r.get("sqft"))
        vals = dict(kind="rent", address=addr, city=_s(r.get("city")) or prop["city"],
                    zip=_s(r.get("zip_code")), price=price,
                    ppsf=(price / sqft) if sqft else None,
                    beds=_nn(r.get("beds")), baths=_nn(r.get("full_baths")), sqft=sqft,
                    year_built=int(_nn(r.get("year_built"))) if _nn(r.get("year_built")) else None,
                    event_date=None, dom=_nn(r.get("days_on_mls")),
                    lat=float(lat), lon=float(lon), distance_mi=dist,
                    lot_sqft=_nn(r.get("lot_sqft")),
                    ptype=_norm_ptype(_s(r.get("style")) or _s(r.get("property_type"))),
                    source=source_override or _source_label(_s(r.get("property_url")), rent=True), source_url=_s(r.get("property_url")),
                    photo_url=_s(r.get("primary_photo")))
        vals["match_score"] = match_score(subj, {"distance_mi": dist, "sqft": sqft, "beds": vals["beds"],
                                                 "baths": vals["baths"], "year_built": vals["year_built"],
                                                 "lot_sqft": vals["lot_sqft"]})
        ex = _find_existing_comp(d, prop["id"], "rent", addr, price,
                                 beds=vals["beds"], source_url=vals["source_url"])
        cols = ",".join(vals.keys()); ph = ",".join("?" * len(vals))
        if ex:
            d.execute(f"UPDATE comps SET {','.join(k+'=?' for k in vals)} WHERE id=?", (*vals.values(), ex)); updated += 1
            if drops is not None: _drop_note(drops, "duplicate", r)
        else:
            d.execute(f"INSERT INTO comps(property_id,{cols}) VALUES(?,{ph})", (prop["id"], *vals.values())); added += 1
        n += 1
    return added, updated, n

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

def _collapse_richness(d):
    """Non-null field count of a comp row dict — the collapse's
    'richest row' measure (v49)."""
    return sum(1 for v in d.values() if v is not None and v != "")

def _collapse_rent_view(rows):
    """Read-side duplicate safety net for the rent views (v48, audit
    D6): rows stored BEFORE the merge-side dedupe keys were fixed can
    still duplicate (same home at two prices — the shipped DB's
    Nebraska pair class). Nothing is deleted: stored rows are
    untouched, the view simply shows each distinct home/unit once. A
    unit designator known only from the stored listing URL is appended
    to the DISPLAYED address ("... Unit 208") so genuinely distinct
    units stop looking identical. Rows become plain dicts (display
    copies).

    v49 loosening (live Nebraska evidence): rows group on (cleaned
    normalized address, unit designator) and FOLD when their beds are
    equal OR either side's beds is NULL — v48's exact-beds key kept a
    live identical Unit-208 $1,899 pair apart because one copy's beds
    was NULL. Compatibility is judged against the group's current
    survivor. The survivor is the RICHEST row (most non-null fields;
    ties go to the higher match_score, then to the earlier row —
    callers pass match-first display order). The displayed address is
    artifact-cleaned (_strip_addr_artifact), so the "# Usa" slug tail
    stored in one live row never renders.

    v50 (the twin v49 missed): the pair that survived v49 on PA was
    told apart by WHERE the unit is stored — one row carries it only
    in its listing URL (address text clean), the other only IN its
    address text ("... Unit 208", the RapidAPI shape). Both derive
    the same unit, but the group key's address component was
    _norm_addr of the raw stored address, which keeps the in-text
    unit segment ("... ave unit 208" / "... ave 208") — two keys for
    one home. The key now derives from the CLEANED DISPLAY identity:
    _addr_unit_split strips the unit segment out of the street before
    normalization, and the unit (from the address text, else the
    listing URL) is the key's second component."""
    out, meta = [], []   # meta[i] = (group key, survivor beds) for out[i]
    for c in rows or []:
        d = dict(c)
        street_text, unit = _addr_unit_split(d.get("address"))
        if not unit:
            unit = _unit_designator(None, d.get("source_url"))
        d["address"] = _strip_addr_artifact(d.get("address") or "")
        if unit:
            label = f"Unit {unit}"
            if label.lower() not in d["address"].lower():
                d["address"] = f"{d['address']} {label}".strip()
        # The key reads the row's STORED address with its unit segment
        # stripped (unit is its own key component — the display suffix
        # above must not enter it); _norm_addr strips the "# Usa"
        # artifact, so the artifact row groups with its clean twin.
        gkey = (_norm_addr(street_text), unit)
        beds = d.get("beds")
        beds = float(beds) if beds is not None else None
        hit = None
        for i, (gk, kb) in enumerate(meta):
            if gk == gkey and (kb is None or beds is None or kb == beds):
                hit = i; break
        if hit is None:
            out.append(d); meta.append((gkey, beds))
            continue
        kept = out[hit]
        new_rich, old_rich = _collapse_richness(d), _collapse_richness(kept)
        new_ms = d.get("match_score") or 0; old_ms = kept.get("match_score") or 0
        if new_rich > old_rich or (new_rich == old_rich and new_ms > old_ms):
            out[hit] = d; meta[hit] = (gkey, beds)
    return out

# ---------------------------------------------------------------- county GIS parcels (v3.4)
# Subject facts from the county's own parcel record — the assessor carries
# sales the listing frames never see (110 Garland Ct's own sale is from 2016:
# no HomeHarvest frame reaches it, the Hillsborough parcel record does).
# Registry entries are added only for endpoints PROBED live (2026-11-04):
#   DuPage County IL  — ParcelSearch/DuPageAssessmentParcelSearch layer 3.
#     (Sherif's spec named OpenData/Parcels/MapServer/0 — that path 404s on
#     the live server; this is the county's real parcel service.)
#     Fields: PIN, FCVTOTAL (assessor total fair cash value). No living specs.
#   Hillsborough County FL — Webmaps/HillsboroughFL_WebParcels layer 0.
#     Fields: folio (APN), TopSaleDate/TopSalePrice. No living specs.
# Fills are BLANK-ONLY, like the listing fill: a county value never
# overwrites anything already on the record. No registry match = Not found.
_DUPAGE_CITIES = {"downers grove", "naperville", "lisle", "lombard", "elmhurst", "wheaton",
                  "glen ellyn", "oak brook", "woodridge", "westmont", "darien",
                  "clarendon hills", "hinsdale", "villa park", "addison", "bensenville",
                  "bloomingdale", "carol stream", "glendale heights", "itasca", "roselle",
                  "warrenville", "west chicago", "willowbrook", "winfield", "burr ridge",
                  "bolingbrook", "aurora", "hanover park", "lemont", "wayne"}
COUNTY_GIS = [
    {"county": "DuPage County, IL",
     "match": lambda p: (p["state"] or "") == "IL" and (p["city"] or "").strip().lower() in _DUPAGE_CITIES,
     "url": "https://gis.dupageco.org/arcgis/rest/services/ParcelSearch/DuPageAssessmentParcelSearch/MapServer/3/query",
     "addr_field": "PROPSTNAME", "pin_field": "PIN", "value_field": "FCVTOTAL",
     "sale_date_field": None, "sale_price_field": None},
    {"county": "Hillsborough County, FL",
     "match": lambda p: (p["state"] or "") == "FL" and (p["city"] or "").strip().lower() == "tampa",
     "url": "https://gis.hcpafl.org/arcgis/rest/services/Webmaps/HillsboroughFL_WebParcels/MapServer/0/query",
     "addr_field": "FullAddress", "pin_field": "folio", "value_field": None,
     "sale_date_field": "TopSaleDate", "sale_price_field": "TopSalePrice"},
]

def county_parcel_fill(d, prop):
    """Blank-fill subject facts from the county parcel record (APN, assessor
    value, last-sale note). Returns a provenance string, or None when no
    registry entry covers the county / no parcel matches / the service is
    unreachable — the honest Not found. Never overwrites a non-blank field."""
    entry = next((e for e in COUNTY_GIS if e["match"](prop)), None)
    if entry is None or prop["lat"] is None or prop["lon"] is None: return None
    try:
        env = (f"{prop['lon'] - 0.0025},{prop['lat'] - 0.0025},"
               f"{prop['lon'] + 0.0025},{prop['lat'] + 0.0025}")
        r = requests.get(entry["url"], params={
            "geometry": env, "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
            "f": "json", "returnGeometry": "false", "resultRecordCount": 1000}, timeout=30)
        feats = r.json().get("features", [])
    except Exception:
        return None
    pnum, pcore = _street_key(prop["street"] or prop["address"])
    hit = None
    for f in feats:
        a = f.get("attributes", {})
        raw = _s(a.get(entry["addr_field"])).split(",")[0]
        rnum, rcore = _street_key(raw)
        if pnum and rnum == pnum and rcore and rcore == pcore: hit = a; break
    if hit is None: return None
    fills = {}
    pin = _s(hit.get(entry["pin_field"])).strip()
    if pin and not prop["apn"]: fills["apn"] = pin
    val = _nn(hit.get(entry["value_field"])) if entry["value_field"] else None
    if val:
        if not prop["est_county"]: fills["est_county"] = val; fills["est_date"] = datetime.date.today().isoformat()
        if not prop["tax_assessed"]: fills["tax_assessed"] = val
    if entry["sale_date_field"]:
        sd = _s(hit.get(entry["sale_date_field"]))[:10]; sp = _nn(hit.get(entry["sale_price_field"]))
        if sd and sp and not prop["sales_history"]:
            fills["sales_history"] = (f"Sold {sd} — ${sp:,.0f} "
                                      f"({entry['county']} parcel record)")
    if fills:
        d.execute(f"UPDATE properties SET {','.join(k+'=?' for k in fills)} WHERE id=?",
                  (*fills.values(), prop["id"]))
    parts = []
    if pin: parts.append(f"PIN {pin}")
    if val: parts.append(f"assessor value ${val:,.0f}")
    if entry["sale_date_field"] and _s(hit.get(entry["sale_date_field"])):
        parts.append(f"last sale {_s(hit.get(entry['sale_date_field']))[:10]}")
    return f"county parcel confirmed via {entry['county']} GIS" + (f" ({'; '.join(parts)})" if parts else "")

class SourceUnreachableError(Exception):
    """The listing source cannot be reached from this host at all — e.g. an
    egress-whitelisted host (PythonAnywhere free tier), where realtor.com
    is not on the allowlist and every HomeHarvest frame fails at the
    connection. The message IS the user-facing text: complete and human,
    no exception internals, no URLs. The job layer surfaces it verbatim."""

SOURCE_UNREACHABLE_MSG = (
    "Comp pull stopped — the listing data source (Realtor.com via HomeHarvest) "
    "can't be reached from this server, because this host's internet access "
    "doesn't include the listing sites. Nothing was added or changed.")

_CONN_ERR_MARKERS = ("connection", "timed out", "timeout", "unreachable",
                     "name resolution", "max retries", "proxy", "tunnel",
                     "whitelist", "forbidden", "403", "blocked", "reset",
                     "refused", "eof occurred", "ssl")

def _is_unreachable_err(e):
    """True when a frame failure is a connectivity failure (the source
    cannot be reached from this host), as opposed to the source answering
    with bad/empty data. Type first, message markers as fallback —
    HomeHarvest wraps requests errors in its own exception classes."""
    if e is None: return False
    if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    low = str(e).lower()
    return any(k in low for k in _CONN_ERR_MARKERS)

# ---------------------------------------------------------------- RapidAPI frames (v3.8; pool v41, union v42)
# A second frame source for hosts whose egress cannot reach the listing
# sites directly (PythonAnywhere free tier blocks realtor.com, but
# p.rapidapi.com IS whitelisted — hosting-options-2026-11-04).
# v41: the v38–v40 pool (apidojo "Realty in US" + "Zillow") is GONE —
# both APIs were DELISTED from RapidAPI. The pool below is built ONLY
# on APIs playground-verified from Sherif's own account on 2026-11-04
# (real HTTP 200s with real data; shapes in research/v41-build-log.md):
#   ReefAPI "Realtor.com Property Data API" (Basic free 100 req/mo):
#     POST /realtor/v1/sold   {location, sort, limit, offset} -> data.results
#                             (FLAT items, coordinates on every row)
#   "Redfin.Com Data" by Nathan (Basic free 100 req/mo):
#     GET  /properties/auto-complete?query="City, ST" -> regionId
#          ("6_20109" for West Chicago, "6_18142" for Tampa — captured)
#     GET  /properties/search-sold?regionId&limit&soldWithin -> data[]
#   "Realtor" by Sabri (Basic free 30 req/mo — LAST, the quota is precious):
#     GET  /search/forsold/coordinates?lat&lon&radius&page -> properties[]
#     GET  /search/forrent/coordinates?lat&lon&radius&page -> properties[]
# Responses are normalized into the standard HomeHarvest columns and fed
# through the SAME merge (distance gate, dedupe, match tiers) — no merge,
# tiering, or UI changes.
# v42 — the pool is a sequential UNION WITH TOP-UP, not first-serve-wins.
# The v41 PA pull stored 0 comps while ReefAPI "served": its single
# city-wide page (100 newest of 3,535) held nothing inside the pull's
# 1-mile gate, and rotation never asked anyone else. Probes after that
# pull (playground, 2026-11-04) proved ReefAPI takes a ZIP location and
# paginates, and that Sabri's forrent REQUIRES a radius (its absence was
# the live HTTP 400). So now: ReefAPI is queried by ZIP and PAGINATES
# (pages of 100, early stop at RAPIDAPI_PAGE_STOP in-gate rows); if the
# accumulated in-gate count is still under RAPIDAPI_UNION_RICH, Redfin
# unions in, and if it is STILL under, Sabri tops up. Every called
# source leaves an outcome record (served N rows / M in-gate, or the
# classified failure + a sanitized error-body snippet) that is ALWAYS
# written to the server log as the pool detail, and the pull's banner
# names only sources whose rows the merge actually STORED. Call budget
# per pull: ReefAPI <= 5 pages + Redfin 2 + Sabri 1 sold, Sabri 1 rent
# (v44 raises the wide-window budget — see below). Results persist in
# the comps table, so the pool is only walked on a fresh pull. With NO
# key configured, nothing in this section ever runs and the pull is
# the pre-RapidAPI one.
# v46 — rent price-path rebuild + Redfin auto-complete fix, both on
# LIVE-CAPTURED structures (playground, Sherif's account, 2026-11-04
# ~23:17 EET — the capture round the v45 log prescribed). The Sabri
# forrent capture proved the rent read was a dead path: a forrent
# item's description has NO list_price key (beds/baths/sqft arrive as
# scalars, [] — the API's empty-array null — or min/max ranges); the
# rent is the item's TOP-LEVEL list_price (a number on single-family
# items, [] on community items), a list_price_min/max range, or
# units[] (one priced entry per floor plan). _sabri_rent_rows
# resolves, in order: numeric top-level list_price -> one row; else
# one row PER priced unit; else a priceless row the merge drops as
# no_price (a range endpoint is never stored as a point rent). [] is
# missing in every Sabri spec read (min == max is the only range
# fallback). Sabri forrent pages 1..3 like forsold, and the radius is
# confirmed KILOMETRES (the playground's own description). The Redfin
# capture proved the other live failure was the REQUEST: the
# auto-complete endpoint takes query="City, ST" (the app sent a
# "location" parameter it does not take, so every live call resolved
# no region) and answers grouped rows — _redfin_region_id parses the
# Places group for the row named for the property's city.
# v44 — wider pull windows + deeper nets (the fixture-street hunt):
# the pull form offers Last 2 years / Last 3 years / All time beside
# Last year (the v35 view filter's exact list), and past_days plumbs
# through every adapter: the client horizon stays max(past_days, 1095)
# and follows past_days beyond it (All time filters nothing); Redfin's
# soldWithin follows the window (floor 365 — every pre-v44 window's
# value — cap 1095); ReefAPI's page cap is 5 at Last year and 8 at
# wider windows; Sabri's top-up pages 1..3 and sends sold_date-min on
# finite windows, retrying a rejected page once without it (the
# fallback is noted on the source's outcome record). Worst-case call
# budget for one wide pull with a thin union: ReefAPI 8 + Redfin 2 +
# Sabri 3 = 13 sold calls, plus Sabri 1 rent.
RAPIDAPI_REEF_HOST = "realtor-com-property-data-api.p.rapidapi.com"
RAPIDAPI_REDFIN_HOST = "redfin-com-data.p.rapidapi.com"
RAPIDAPI_SABRI_HOST = "realtor16.p.rapidapi.com"
# v48: private-zillow (ONEAPI) — playground-captured 2026-11-05
# (research/zillow-api-capture-2026-11-05.md): subject detail BY PLAIN
# STREET ADDRESS (/By Property Address) and sold comps by map bounds
# (/search/bymapbounds). BASIC plan 250 requests/month HARD cap — the
# subject lookup fires at most once per pull and only while the subject
# still lacks specs or photos; the bounds pull is one call per pull.
RAPIDAPI_PZ_HOST = "private-zillow.p.rapidapi.com"
RAPIDAPI_SRC_REEF = "RapidAPI (ReefAPI)"
RAPIDAPI_SRC_PZ = "RapidAPI (Zillow)"
RAPIDAPI_SRC_REDFIN = "RapidAPI (Redfin)"
RAPIDAPI_SRC_SABRI = "RapidAPI (Realtor16)"
# v58: Things4u — TWO new free BASIC subscriptions in Sherif's account
# (no card anywhere), playground-verified 2026-11-06 on Lindley
# (research/things4u-api-facts-2026-11-06.md): Realtor Search
# (150 req/mo hard limit) and Realty US (500 req/mo hard limit), both
# GET properties/search-sold AND properties/search-rent over the same
# Realtor.com data. Realty US is asked first; Realtor Search is the
# failover for the same data, not a second source.
RAPIDAPI_T4U_US_HOST = "realty-us.p.rapidapi.com"
RAPIDAPI_T4U_RS_HOST = "realtor-search.p.rapidapi.com"
RAPIDAPI_SRC_T4U_US = "RapidAPI (Realty US)"
RAPIDAPI_SRC_T4U_RS = "RapidAPI (Realtor Search)"

# v58: 429 skip-fast. A host that answers HTTP 429 is quota-dead for
# the rest of that calendar month — the settings row records the date
# of the 429, the frame loops skip the host (one probe/month), and a
# 429 never burns a retry. Both free quotas reset on the 1st (Sabri
# and ReefAPI reset Dec 4 in the 2026-11-06 incident because their
# cycles end then; the monthly date check is the general rule).
_RAPIDAPI_QUOTA_DEAD_PREFIX = "rapidapi_quota_dead:"

def _rapidapi_quota_dead(host):
    """True when this host answered HTTP 429 earlier this calendar
    month (settings row rapidapi_quota_dead:<host> = YYYY-MM-DD)."""
    try:
        d = get_setting(_RAPIDAPI_QUOTA_DEAD_PREFIX + host)
    except Exception:
        return False
    return bool(d) and d[:7] == datetime.date.today().strftime("%Y-%m")

def _rapidapi_mark_quota_dead(host):
    """Record a host's 429 (settings row, today's date). Never raises
    into the pull — a settings write failing must not mask the
    source's own failure."""
    try:
        set_setting(_RAPIDAPI_QUOTA_DEAD_PREFIX + host,
                    datetime.date.today().isoformat())
    except Exception as e:
        app.logger.warning(f"comp pull: could not record quota-dead for {host}: {e!r}")
# The Redfin auto-complete endpoint is LIVE-CAPTURED (playground,
# 2026-11-04): GET /properties/auto-complete with query="City, ST",
# answering grouped rows ({data: [{name: "Places", rows: [...]},
# {name: "Schools", ...}, {name: "Agents", ...}]}). _redfin_region_id
# parses that shape; an unusable answer can only classify the source
# "didn't serve" — it can never produce data.
RAPIDAPI_REDFIN_AC_PATH = "/properties/auto-complete"
# v42 union thresholds. RAPIDAPI_PAGE_STOP: the ReefAPI page walk stops
# early once this many accumulated rows pass BOTH gates (distance +
# date horizon). RAPIDAPI_UNION_RICH: once the union's in-gate count
# reaches this, no further source is called (top-up stops).
RAPIDAPI_PAGE_STOP = 30
RAPIDAPI_UNION_RICH = 25
RAPIDAPI_REEF_PAGES = 5
# v44: wider windows dig deeper — ReefAPI's page cap rises to
# RAPIDAPI_REEF_PAGES_WIDE when the pull window exceeds Last year, and
# Sabri's top-up walks up to RAPIDAPI_SABRI_PAGES pages of
# RAPIDAPI_SABRI_PAGE_SIZE (the playground-observed page size).
RAPIDAPI_REEF_PAGES_WIDE = 8
RAPIDAPI_SABRI_PAGES = 3
RAPIDAPI_SABRI_PAGE_SIZE = 50

def _rapidapi_key():
    """RapidAPI key: env RAPIDAPI_API_KEY first, then the rapidapi_key
    settings store — the _census_key pattern. The key is only ever sent
    as the X-RapidAPI-Key header: never in a URL, a log line, or a
    rendered page (Settings displays it masked)."""
    return os.environ.get("RAPIDAPI_API_KEY") or get_setting("rapidapi_key") or ""

def _rapidapi_get(host, path, params=None, timeout=15, retries=2):
    """One RapidAPI GET. Returns (json, None) or (None, err). No key ->
    (None, None) without any request. v58: 15s cap (was 30s — a hung
    provider must never stall the pull) and a bounded retry (x2, short
    backoff) on ConnectionError/Timeout ONLY — an HTTP error (429 quota
    included) surfaces immediately so the quota-dead flag can catch it."""
    if not _rapidapi_key(): return None, None
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(f"https://{host}{path}", params=params or {},
                             headers={"X-RapidAPI-Key": _rapidapi_key(), "X-RapidAPI-Host": host},
                             timeout=timeout)
            r.raise_for_status()
            return r.json(), None
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None, e
        except Exception as e:
            return None, e
    return None, last_err

def _rapidapi_post(host, path, body, timeout=15, retries=2):
    """One RapidAPI POST (the ReefAPI sold search takes a JSON body).
    Same contract as _rapidapi_get (v58: 15s cap, retry on
    ConnectionError/Timeout only)."""
    if not _rapidapi_key(): return None, None
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(f"https://{host}{path}", json=body,
                              headers={"X-RapidAPI-Key": _rapidapi_key(), "X-RapidAPI-Host": host},
                              timeout=timeout)
            r.raise_for_status()
            return r.json(), None
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None, e
        except Exception as e:
            return None, e
    return None, last_err

# v49: the private-zillow wrapper is flaky in production (live PA
# evidence 2026-11-05: a sold search answered HTTP 503 {"message":
# "503: Zillow is temporarily limiting requests, please retry in a
# few seconds"} and the Garland subject lookup answered HTTP 404 from
# PA minutes after the identical playground call returned 200).
# v58: the retry is GONE — the provider is DOWN (its sold search hangs
# in its own playground, 2026-11-06), so a retry only burns pull time.
# The sold search is no longer called at all; the by-address subject
# lookup gets ONE attempt with an 8s cap, as the LAST subject-fill
# resort. A 503/404/timeout degrades to the honest not-filled state.

def _err_status(err):
    """The HTTP status an exception carries (via its response), else
    None — the _rapi_fail_reason read, shared."""
    resp = getattr(err, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None

def _pz_get(path, params, retry_404=False):
    """One private-zillow GET (v58: SINGLE attempt, 8s cap — the
    v49 bounded retry is retired with the sold search). Returns
    (data, err, attempts, last_status) — the same contract the subject
    fill reads, so its call sites are unchanged. A failure degrades to
    that attempt's error; it can never raise into the pull (the v40
    containment rule). retry_404 is kept in the signature for the
    callers' sake and is ignored: a 404 is the honest not-on-Zillow
    answer, not a retry trigger."""
    data, err = _rapidapi_get(RAPIDAPI_PZ_HOST, path, params, timeout=8, retries=0)
    last_status = _err_status(err) if err is not None else 200
    return data, err, 1, last_status

def _rapi_dig(obj, *keys):
    cur = obj
    for k in keys:
        if not isinstance(cur, dict): return None
        cur = cur.get(k)
    return cur

_HH_FRAME_COLS = ("street", "city", "zip_code", "sold_price", "list_price", "sqft",
                  "beds", "full_baths", "year_built", "last_sold_date", "days_on_mls",
                  "latitude", "longitude", "lot_sqft", "style", "property_type",
                  "property_url", "primary_photo", "alt_photos", "estimated_value",
                  "price_per_sqft", "property_id")

def _hh_frame(rows):
    """Normalized row dicts -> a DataFrame carrying the standard
    HomeHarvest columns the merges and find_subject_row read."""
    import pandas as pd
    return pd.DataFrame([{c: r.get(c) for c in _HH_FRAME_COLS} for r in rows],
                        columns=list(_HH_FRAME_COLS))

def _rapi_photos(item):
    """(primary_photo, [all photo urls]) from a Realty v3 item:
    primary_photo is {"href": ...}; photos is a list of {"href": ...}."""
    pp = item.get("primary_photo")
    first = pp.get("href") if isinstance(pp, dict) else (pp if isinstance(pp, str) else None)
    alts = []
    for ph in item.get("photos") or []:
        href = ph.get("href") if isinstance(ph, dict) else (ph if isinstance(ph, str) else None)
        if href and href not in alts: alts.append(href)
    if first and first not in alts: alts.insert(0, first)
    return (alts[0] if alts else None), alts

def _reef_item_to_row(item):
    """One ReefAPI /realtor/v1/sold item -> the standard HomeHarvest
    columns. The item is FLAT (playground-verified 2026-11-04): address,
    price, specs and coordinates are all top-level. Sold price comes
    ONLY from last_sold_price — a sold row without one keeps
    sold_price=None and the merge drops it; a sold home's list price
    is not its sale price and is never substituted."""
    brand = item.get("branding") or {}
    photo = _s(item.get("primary_photo_url")) or None
    return dict(street=_s(item.get("address_line")), city=_s(item.get("city")),
                zip_code=_s(item.get("zip_code")),
                sold_price=item.get("last_sold_price"),
                list_price=item.get("list_price"),
                sqft=item.get("sqft"), beds=item.get("beds"),
                full_baths=item.get("baths"),
                year_built=item.get("year_built"), lot_sqft=None,
                last_sold_date=_s(item.get("last_sold_date"))[:10] or None,
                days_on_mls=None,
                latitude=item.get("latitude"), longitude=item.get("longitude"),
                style=None, property_type=None,
                property_url=_s(brand.get("url")) if isinstance(brand, dict) else "",
                primary_photo=photo, alt_photos=[photo] if photo else [],
                estimated_value=None, price_per_sqft=None)

def _rapi_horizon_cutoff(past_days):
    """Client-side sold-date horizon for the pool frames. The pool APIs
    return recency-sorted AREA lists (ReefAPI's city search spans
    thousands of sales), so the date bound is applied here, at
    max(past_days, 1095) — the same 3-year floor the retired v3/list
    route used server-side, keeping the depth the views window over.
    Rows whose sold date cannot be read are kept (a dateless row is
    stored dateless, never re-dated)."""
    return (datetime.date.today() - datetime.timedelta(days=max(past_days, 1095))).isoformat()

def _rapi_in_horizon(rows, past_days):
    cut = _rapi_horizon_cutoff(past_days)
    return [r for r in rows if not r.get("last_sold_date") or r["last_sold_date"] >= cut]

def _redfin_region_id(prop):
    """Resolve the property's city to a Redfin regionId via Nathan's
    auto-complete. Returns (region_id, label, err); label is the
    matched row's display name (v47: the success note names the region
    as "Name id", e.g. "Tampa 6_18142"). LIVE-CAPTURED request and
    response (playground, Sherif's account, 2026-11-04): the endpoint
    takes query="City, ST" — pre-v46 the app sent a "location"
    parameter the endpoint does not take, so every live call resolved
    nothing (the v43–v45 pulls' "region unresolved"). The response
    groups its rows: {data: [{name: "Places", rows: [{id: "6_18142",
    name: "Tampa", subName: "Tampa, FL, USA", ...}]}, {name:
    "Schools", ...}, {name: "Agents", ...}]}. The region id is a
    Places row's id ("6_<digits>"): prefer the row whose name IS the
    property's city (case-insensitive) — the groups also carry
    schools, agents, and near-name places whose ids share the form
    and must not win — else the first Places row with a well-formed
    id. No Places group: scan every group's rows by the same
    preference. Nothing usable -> (None, None): the source simply
    didn't serve. An id is NEVER guessed or constructed, and any
    wrong hit is backstopped by the merge's distance gate (a wrong
    region's rows land miles from the subject and are discarded
    there)."""
    data, err = _rapidapi_get(RAPIDAPI_REDFIN_HOST, RAPIDAPI_REDFIN_AC_PATH,
                              {"query": f"{prop['city']}, {prop['state']}"})
    if err is not None or data is None: return None, None, err
    import re as _re
    pat = _re.compile(r"^\d+_\d+$")
    city = (prop["city"] or "").strip().lower()
    groups = data.get("data") if isinstance(data, dict) else None
    places, all_rows = None, []
    if isinstance(groups, list):
        for g in groups:
            if not isinstance(g, dict): continue
            grows = g.get("rows")
            if not isinstance(grows, list): continue
            all_rows.extend(grows)
            if (g.get("name") or "") == "Places": places = grows
    pool = places if places is not None else all_rows
    def _rid(row):
        v = row.get("id") if isinstance(row, dict) else None
        return v.strip() if isinstance(v, str) and pat.match(v.strip()) else None
    for row in pool:
        if isinstance(row, dict) and city \
                and (row.get("name") or "").strip().lower() == city:
            rid = _rid(row)
            if rid: return rid, (row.get("name") or "").strip() or None, None
    for row in pool:
        rid = _rid(row)
        if rid: return rid, (row.get("name") or "").strip() or None, None
    return None, None, None

def _redfin_item_to_row(item):
    """One Redfin.Com Data search-sold item -> the standard HomeHarvest
    columns. Nested shape (playground-verified 2026-11-04): the sold
    price is priceInfo.amount, the date lastSaleData.lastSoldDate,
    specs under listingMetadata / sqftInfo / yearBuilt, the address and
    centroid under addressInfo, photos under photos.bigPhotos (small
    Photos as fallback), and homeData.url is a RELATIVE Redfin path,
    prefixed here with the site origin."""
    meta = item.get("listingMetadata") or {}
    addr = item.get("addressInfo") or {}
    cent = addr.get("centroid") or {}
    photos = item.get("photos") or {}
    alts = []
    for group in ("bigPhotos", "smallPhotos"):
        for ph in photos.get(group) or []:
            href = ph.get("photoUrlHref") if isinstance(ph, dict) else None
            if href and href not in alts: alts.append(href)
        if alts: break
    url = _s(_rapi_dig(item, "homeData", "url"))
    if url and not url.startswith("http"):
        url = "https://www.redfin.com" + ("" if url.startswith("/") else "/") + url
    amount = _rapi_dig(item, "priceInfo", "amount")
    return dict(street=_s(addr.get("formattedStreetLine")), city=_s(addr.get("city")),
                zip_code=_s(addr.get("zip")),
                sold_price=amount, list_price=amount,
                sqft=_rapi_dig(item, "sqftInfo", "amount"),
                beds=meta.get("beds"), full_baths=meta.get("baths"),
                year_built=_rapi_dig(item, "yearBuilt", "yearBuilt"), lot_sqft=None,
                last_sold_date=_s(_rapi_dig(item, "lastSaleData", "lastSoldDate"))[:10] or None,
                days_on_mls=None,
                latitude=cent.get("lat"), longitude=cent.get("lng"),
                style=None, property_type=None,
                property_url=url,
                primary_photo=(alts[0] if alts else None), alt_photos=alts,
                estimated_value=None, price_per_sqft=None)

def _sabri_num(v):
    """A Sabri scalar as a number, or None. The API's null convention
    is the EMPTY ARRAY [] (beds [], sqft [], list_price [] all mean
    "no value" — live-captured 2026-11-04), so anything that is not a
    finite number or a numeric string is missing: [], {}, None, junk.
    (fnum alone would stringify a list; the isinstance screen keeps
    containers out before it ever sees them.)"""
    if isinstance(v, bool) or isinstance(v, (list, dict)): return None
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    return fnum(v)

def _sabri_spec(desc, keys, min_key=None, max_key=None):
    """One Sabri spec value (beds / baths / sqft) from a description
    dict (an item's, or a rental unit's): the first numeric scalar
    among keys; when every scalar is [] / missing, the min when
    min == max (both numeric) — a range that is really a point —
    else None. A genuine range (min != max) is NOT a point and is
    never read as one (no guessed endpoint)."""
    for k in keys:
        v = _sabri_num(desc.get(k))
        if v is not None: return v
    if min_key and max_key:
        lo, hi = _sabri_num(desc.get(min_key)), _sabri_num(desc.get(max_key))
        if lo is not None and lo == hi: return lo
    return None

def _sabri_plan_tag(unit, idx=None):
    """The floor-plan identity of one Sabri community unit (v48):
    '2 bd · 1,034 sqft' from the unit's own specs, '1,034 sqft' or
    '2 bd' when only one is known, 'Unit N' (its position in the
    units[] list) when neither is. Appended to the row's street so a
    community's floor plans are VISIBLY distinct cards and the rent
    dedupe key (which reads the normalized address) keeps them apart
    instead of collapsing or cloning them."""
    desc = unit.get("description") or {}
    if not isinstance(desc, dict): desc = {}
    beds = _sabri_spec(desc, ("beds",), "beds_min", "beds_max")
    sqft = _sabri_spec(desc, ("sqft",), "sqft_min", "sqft_max")
    parts = []
    if beds: parts.append(f"{beds:g} bd")
    if sqft: parts.append(f"{int(sqft):,} sqft")
    if parts: return " · ".join(parts)
    return f"Unit {idx + 1}" if idx is not None else "Unit"

def _sabri_item_to_row(item, rent=False, unit=None, unit_idx=None):
    """One Sabri realtor16 item -> the standard HomeHarvest columns.
    The item is the Realtor data model (playground-verified for
    /search/forsold/coordinates 2026-11-04; year_built is read when
    present — it was not observed in the captured first item — else
    None). Sold price comes ONLY from description.sold_price, never
    the list price. Spec scalars pass through _sabri_spec: the API's
    empty-array null [] is missing everywhere, with min == max as
    the only range fallback (v46).
    rent=True reads the /search/forrent sibling in its LIVE-CAPTURED
    shape (2026-11-04): the description carries NO list_price — the
    rent is the item's TOP-LEVEL list_price. With unit=<a units[]
    entry>, the row is that unit's: rent and beds/baths/sqft from the
    unit, address/coords/photo/permalink from the parent item (see
    _sabri_rent_rows for the resolution order). A row without a
    usable rent reaches the merge with list_price=None and is stored
    as nothing."""
    desc = item.get("description") or {}
    if not isinstance(desc, dict): desc = {}
    addr = _rapi_dig(item, "location", "address") or {}
    coord = addr.get("coordinate") or {}
    perma = _s(item.get("permalink"))
    if perma.startswith("http"): url = perma
    elif perma: url = "https://www.realtor.com/realestateandhomes-detail/" + perma.lstrip("/")
    else: url = ""
    photo, alts = _rapi_photos(item)
    ptype = _s(desc.get("type")) if isinstance(desc.get("type"), str) else ""
    if rent:
        specs = desc
        street = _s(addr.get("line"))
        if unit is not None:
            specs = unit.get("description") or {}
            if not isinstance(specs, dict): specs = {}
            price = _sabri_num(unit.get("list_price"))
            if street:
                street = f"{street} (Plan {_sabri_plan_tag(unit, unit_idx)})"
        else:
            price = _sabri_num(item.get("list_price"))
        return dict(street=street, city=_s(addr.get("city")),
                    zip_code=_s(addr.get("postal_code")),
                    sold_price=None, list_price=price,
                    sqft=_sabri_spec(specs, ("sqft",), "sqft_min", "sqft_max"),
                    beds=_sabri_spec(specs, ("beds",), "beds_min", "beds_max"),
                    full_baths=_sabri_spec(specs, ("baths_consolidated", "baths_full", "baths"),
                                           "baths_min", "baths_max"),
                    year_built=_sabri_num(desc.get("year_built")),
                    lot_sqft=_sabri_num(desc.get("lot_sqft")),
                    last_sold_date=None,
                    days_on_mls=_sabri_num(desc.get("days_on_market")),
                    latitude=coord.get("lat"), longitude=coord.get("lon"),
                    style=ptype, property_type=ptype,
                    property_url=url, primary_photo=photo, alt_photos=alts,
                    estimated_value=None, price_per_sqft=None)
    return dict(street=_s(addr.get("line")), city=_s(addr.get("city")),
                zip_code=_s(addr.get("postal_code")),
                sold_price=_sabri_num(desc.get("sold_price")),
                list_price=_sabri_num(desc.get("list_price")),
                sqft=_sabri_spec(desc, ("sqft",), "sqft_min", "sqft_max"),
                beds=_sabri_spec(desc, ("beds",), "beds_min", "beds_max"),
                full_baths=_sabri_spec(desc, ("baths_consolidated", "baths_full", "baths"),
                                       "baths_min", "baths_max"),
                year_built=_sabri_num(desc.get("year_built")),
                lot_sqft=_sabri_num(desc.get("lot_sqft")),
                last_sold_date=_s(desc.get("sold_date"))[:10] or None,
                days_on_mls=_sabri_num(desc.get("days_on_market")),
                latitude=coord.get("lat"), longitude=coord.get("lon"),
                style=ptype, property_type=ptype,
                property_url=url, primary_photo=photo, alt_photos=alts,
                estimated_value=None, price_per_sqft=None)

def _sabri_rent_rows(item):
    """One forrent item -> the normalized rent rows it yields (v46),
    in the LIVE-CAPTURED structure's resolution order:
      (a) a numeric top-level list_price > 0 -> the single item row
          (specs from the item description);
      (b) else a non-empty units[] -> ONE ROW PER UNIT carrying a
          numeric list_price > 0 (community listings price per floor
          plan; the parent item itself has no point rent);
      (c) else the single item row with list_price=None — a
          range-only item (list_price_min/max, no units, no numeric
          list_price) reaches the merge priceless and is dropped
          there as no_price: a range endpoint is never stored as a
          point rent."""
    top = _sabri_num(item.get("list_price"))
    units = item.get("units")
    if (top is None or top <= 0) and isinstance(units, list) and units:
        rows = []
        for _ui, u in enumerate(units):
            if not isinstance(u, dict): continue
            rent = _sabri_num(u.get("list_price"))
            if rent is None or rent <= 0: continue
            rows.append(_sabri_item_to_row(item, rent=True, unit=u, unit_idx=_ui))
        if rows: return rows
    return [_sabri_item_to_row(item, rent=True)]

def _rapi_fail_reason(err):
    """Short human reason class for one source's failure, used in the
    failover note the pull surfaces. Never carries a URL or the key."""
    if err is None: return "no results"
    resp = getattr(err, "response", None)
    code = getattr(resp, "status_code", None)
    if code is None and "429" in str(err): code = 429
    if code == 429: return "quota reached (HTTP 429)"
    if isinstance(code, int): return f"HTTP {code}"
    if _is_unreachable_err(err): return "unreachable"
    return "error"

def _rapi_err_snippet(err):
    """A sanitized snippet of an HTTP error's RESPONSE BODY (v42): the
    first 200 chars, whitespace-collapsed. The v41 live pull's Sabri
    rent 400 was mute — the API's own body said exactly what was wrong
    ({"error": "Invalid query parameters", "fields": {"radius": ...}})
    and only the status reached the log. This reads the body off the
    error's response object ONLY: never request headers, never the key,
    never a URL. '' when the error carries no readable body."""
    resp = getattr(err, "response", None)
    if resp is None: return ""
    text = getattr(resp, "text", None)
    if not text:
        try:
            payload = resp.json()
            text = json.dumps(payload) if payload is not None else ""
        except Exception:
            text = ""
    if not text: return ""
    return " ".join(str(text).split())[:200]

def _rapi_fail_detail(err):
    """The LOG form of a failure: the classified reason plus the error
    body snippet when one exists (e.g. 'HTTP 400: {"error": ...}').
    The banner keeps the clean reason (_rapi_fail_reason); bodies are
    diagnostics and belong server-side."""
    reason = _rapi_fail_reason(err)
    snip = _rapi_err_snippet(err)
    return f"{reason}: {snip}" if snip else reason

# Adapter side-notes (v44): an adapter whose (rows, err, label) triple
# cannot carry a diagnostic — e.g. Sabri's sold_date-min fallback —
# leaves one here, keyed by its source key; _rapidapi_sold_frame takes
# it (once) into the source's outcome record. Thread-local: concurrent
# pulls can never read each other's notes.
_RAPI_NOTES = threading.local()

def _rapi_note(key, text):
    notes = getattr(_RAPI_NOTES, "notes", None)
    if notes is None:
        notes = _RAPI_NOTES.notes = {}
    notes[key] = text

def _rapi_take_note(key):
    notes = getattr(_RAPI_NOTES, "notes", None) or {}
    return notes.pop(key, None)

def _rapi_count_in_gate(rows, prop, radius_mi):
    """How many normalized rows the sold MERGE would accept (store
    new, or update in place as a duplicate) — the SHARED acceptance
    test _sold_row_gate over the adapter's horizon-filtered rows
    (v43). The union sequencing paginates and tops up on this count,
    so it must mean "storable", not merely "nearby": pre-v43 it
    tested coordinates + distance only, counted rows the merge then
    dropped for having no sold price or no street, and a rich-looking
    count suppressed the top-up sources (the live 45-in-gate /
    0-stored pull — see _sold_row_gate)."""
    return sum(1 for r in rows or [] if _sold_row_gate(r, prop, radius_mi)[0])

def _frame_in_gate(df, prop, radius_mi):
    """In-gate count for an already-built DataFrame (the Cadastral /
    jina frames, whose adapters don't return outcomes): rows the sold
    MERGE would accept, per the shared _sold_row_gate."""
    if df is None or not len(df): return 0
    return sum(1 for _, r in df.iterrows() if _sold_row_gate(dict(r), prop, radius_mi)[0])

def _sabri_radius(radius_mi):
    """The radius sent to Sabri's coordinate endpoints (v42). forrent
    REQUIRES radius (latitude/longitude/radius all required — the
    radius-less call was the live HTTP 400; the API's validation body
    names the field). The parameter's units are KILOMETRES — the
    playground's own parameter description, confirmed in the
    2026-11-04 capture round (v46 retires the v42 miles-vs-km hedge):
    the value is the pull radius converted to km, plus 1 km of slack.
    The merge remains the real gate — an over-wide Sabri net costs
    rows, never accuracy."""
    return int(math.ceil((radius_mi or 1.0) * 1.60934)) + 1

def _rapi_rows(items, fn):
    """Normalize a result list item by item (v40). One malformed item —
    a non-dict entry, a description of the wrong type, any shape the
    normalizer chokes on — is SKIPPED, never fatal: per the pool
    contract a broken shape degrades a source, it does not zero a
    frame that also carries good rows, and it never raises into the
    rotation (research/rapidapi-live-shapes-2026-11-04.md §6)."""
    rows = []
    for it in items or []:
        if not isinstance(it, dict): continue
        try: rows.append(fn(it))
        except Exception: continue
    return rows

def _rapi_rows_multi(items, fn):
    """_rapi_rows for a normalizer that returns a LIST of rows per
    item (v46: Sabri rent — one community item expands to one row per
    priced unit, see _sabri_rent_rows). Same per-item containment: a
    malformed item is skipped, never fatal."""
    rows = []
    for it in items or []:
        if not isinstance(it, dict): continue
        try: rows.extend(fn(it))
        except Exception: continue
    return rows

# --- the pool (Sherif 2026-11-04: subscribe to all the useful free
# APIs and SWITCH automatically when one hits its quota or fails).
# RapidAPI quotas are PER-API, so moving down the list moves to a fresh
# monthly quota behind the SAME account key. Adapter contract:
# fn(prop, past_days, radius_mi) -> (rows, err, source_label); rows
# empty means "this source didn't serve" (quota / unreachable / empty /
# broken response are all just reasons to move on). v58 pool, in order:
# Things4u Realty US (500/mo) -> Things4u Realtor Search (150/mo,
# same data — failover) -> ReefAPI (100/mo) -> Sabri (30/mo — precious).
# Retired v58: Redfin (rows carry no coordinates — nothing storable)
# and private-zillow sold (provider-side hang). v42: the sold side is
# a sequential UNION WITH TOP-UP over this order (see
# _rapidapi_sold_frame) — a source is only skipped when the union is
# already rich, and a failure never blocks the sources behind it.
# --- v58 Things4u (Realtor Search / Realty US) ---
# Two free BASIC subscriptions in Sherif's account (no card), playground-
# verified 2026-11-06 (research/things4u-api-facts-2026-11-06.md): both
# serve the same Realtor.com data over GET properties/search-sold AND
# properties/search-rent. Realty US (500/mo) is asked first; Realtor
# Search (150/mo) is the failover for the same data. The envelope and
# row shape were CAPTURED verbatim in the playground 2026-11-06
# (research/things4u-api-facts-2026-11-06.md): list at data.results;
# coords at location.address.coordinate {lat, lon}; specs in
# description.*; sold price last_sold_price; year built ONLY as
# "Year Built: NNNN" text inside details[]. The converter reads that
# shape FIRST and falls back to the flat Realtor-v3 keys v58 used
# (tolerance for host drift + gate_rapidapi's flat fake), and logs the
# live top-level keys on first use.
_YEAR_BUILT_RE = re.compile(r"Year Built:\s*(\d{4})")


def _things4u_year_built_from_details(details):
    """Year built out of a Things4u details[] list (v58.1): each entry
    is {text: [...], category: "..."} and the year lives ONLY as text
    ("Year Built: 1940" under "Building and Construction"). Bare
    strings are searched too. None when absent or malformed — never
    invented."""
    texts = []
    if isinstance(details, list):
        for d in details:
            if isinstance(d, dict):
                t = d.get("text")
                if isinstance(t, list): texts.extend(str(x) for x in t)
                elif isinstance(t, str): texts.append(t)
            elif isinstance(d, str):
                texts.append(d)
    elif isinstance(details, dict):
        for v in details.values():
            if isinstance(v, str): texts.append(v)
            elif isinstance(v, list): texts.extend(str(x) for x in v)
    for s in texts:
        m = _YEAR_BUILT_RE.search(s or "")
        if m:
            try: return int(m.group(1))
            except ValueError: continue
    return None


def _things4u_fields(item):
    """The canonical fields out of ONE Things4u search-row item (v58.1,
    captured shape first, flat fallbacks second). Shared by the sold /
    rent normalizer and the detail parser so both read the same shape.
    Pure; never raises on a malformed item (callers skip via _rapi_rows)."""
    if not isinstance(item, dict): return {}
    loc = item.get("location") if isinstance(item.get("location"), dict) else {}
    addr = loc.get("address") if isinstance(loc.get("address"), dict) else {}
    desc = item.get("description") if isinstance(item.get("description"), dict) else {}
    coord = addr.get("coordinate") if isinstance(addr.get("coordinate"), dict) else {}
    if not coord and isinstance(loc.get("coordinate"), dict):
        coord = loc["coordinate"]
    def _pick(*keys):
        for k in keys:
            v = item.get(k)
            if v is not None: return v
        return None
    def _dpick(*keys):
        for k in keys:
            v = desc.get(k)
            if v is not None: return v
        return None
    lat = _sabri_num(coord.get("lat")) if coord.get("lat") is not None else None
    lon = _sabri_num(coord.get("lon")) if coord.get("lon") is not None else None
    if lat is None: lat = _sabri_num(_pick("latitude", "lat"))
    if lon is None: lon = _sabri_num(_pick("longitude", "lon"))
    photo, alts = _rapi_photos(item)
    if photo is None:
        photo = _s(_pick("primary_photo_url", "primary_photo")) or None
        alts = [photo] if photo else []
    yb = _things4u_year_built_from_details(item.get("details"))
    if yb is None:
        yb = _sabri_num(_pick("year_built"))
    return dict(street=_s(addr.get("line")) or _s(_pick("address_line", "address")),
                city=_s(addr.get("city")) or _s(_pick("city")),
                zip_code=_s(addr.get("postal_code")) or _s(_pick("zip_code", "zip")),
                state=_s(addr.get("state_code")) or _s(_pick("state")),
                sold_price=_sabri_num(_pick("last_sold_price", "sold_price")),
                list_price=_sabri_num(_pick("list_price")),
                sqft=_sabri_num(_dpick("sqft") if _dpick("sqft") is not None else _pick("sqft", "livingArea")),
                beds=_sabri_num(_dpick("beds") if _dpick("beds") is not None else _pick("beds", "bedrooms")),
                full_baths=_sabri_num(_dpick("baths") if _dpick("baths") is not None else _pick("baths", "bathrooms")),
                year_built=int(yb) if yb else None,
                lot_sqft=_sabri_num(_dpick("lot_sqft") if _dpick("lot_sqft") is not None else _pick("lot_sqft", "lotSize")),
                last_sold_date=_s(_pick("last_sold_date"))[:10] or None,
                days_on_mls=None,
                latitude=lat, longitude=lon,
                style=_s(_dpick("type")) or _s(_pick("property_type", "style")),
                property_type=_s(_dpick("type")) or _s(_pick("property_type", "style")),
                property_url=_s(_pick("href", "property_url")) or None,
                primary_photo=photo, alt_photos=alts,
                property_id=_s(_pick("property_id")) or None,
                estimated_value=None, price_per_sqft=None)
def _things4u_results(data):
    """The result list out of a Things4u search payload: data.results
    first (the shape the hunt report implies), then the other
    documented Realtor-v3 nests, then a top-level list. Anything else
    is an empty list — never a shape error."""
    if not isinstance(data, dict): return []
    for keys in (("data", "results"), ("data", "home_search", "results"),
                 ("data", "listings"), ("data", "properties")):
        items = _rapi_dig(data, *keys)
        if isinstance(items, list): return items
    if isinstance(data.get("results"), list): return data["results"]
    return []

def _things4u_item_to_row(item, rent=False):
    """One Things4u search item -> the standard HomeHarvest columns
    (v58.1: captured shape via _things4u_fields). Sold price comes
    ONLY from last_sold_price; a rent row's price is list_price
    (sold_price stays None — the rent gate reads list_price).
    property_id travels with the row so the subject-detail ladder can
    call /properties/detail with the subject's own id."""
    f = _things4u_fields(item)
    return dict(street=f["street"], city=f["city"], zip_code=f["zip_code"],
                sold_price=None if rent else f["sold_price"],
                list_price=f["list_price"],
                sqft=f["sqft"], beds=f["beds"], full_baths=f["full_baths"],
                year_built=f["year_built"], lot_sqft=f["lot_sqft"],
                last_sold_date=f["last_sold_date"], days_on_mls=None,
                latitude=f["latitude"], longitude=f["longitude"],
                style=f["style"], property_type=f["property_type"],
                property_url=f["property_url"],
                primary_photo=f["primary_photo"], alt_photos=f["alt_photos"],
                property_id=f["property_id"],
                estimated_value=None, price_per_sqft=None)

def _things4u_search(host, path, prop, rent=False):
    """One Things4u search (sold or rent), paginated. Location is the
    Realtor-v3 city form the playground verified ('city:West Chicago,
    IL'); resultsPerPage=200 (the API's max); the walk stops on a
    short page, the reported total, or the page cap (3 sold / 2
    rent). minSoldDate is NOT sent: the hunt captured the parameter
    name but not its exact format, and a wrong date format would fail
    the whole call — the client-side horizon filter governs instead,
    exactly as ReefAPI's adapter does. The first response's top-level
    keys are logged so a live envelope mismatch is diagnosable."""
    loc = f"city:{_s(prop.get('city'))}, {_s(prop.get('state'))}"
    rows = []
    max_pages = 2 if rent else 3
    for page in range(1, max_pages + 1):
        data, err = _rapidapi_get(host, path,
                                  {"location": loc, "resultsPerPage": 200, "page": page})
        if err is not None:
            if page == 1: return [], err
            break
        if page == 1:
            keys = sorted(data.keys()) if isinstance(data, dict) else []
            app.logger.warning(f"comp pull: Things4u {path} top-level keys: {keys}")
            _items0 = _things4u_results(data)
            if _items0 and isinstance(_items0[0], dict):
                app.logger.warning(f"comp pull: Things4u {path} first-row keys: "
                                   f"{sorted(_items0[0].keys())}")
        items = _things4u_results(data)
        total = _rapi_dig(data, "data", "total")
        rows.extend(_rapi_rows(items, lambda it: _things4u_item_to_row(it, rent=rent)))
        if len(items) < 200: break
        if isinstance(total, (int, float)) and len(rows) >= total: break
    return rows, None

def _src_things4u(prop, past_days, radius_mi, host, label, rent=False):
    """Shared Things4u adapter: search, then the client-side horizon
    filter on the sold side (the rent side has no date window). A
    page-1 error is the source's failure; a later page's failure keeps
    the accumulated rows."""
    rows, err = _things4u_search(host,
                                 "/properties/search-rent" if rent else "/properties/search-sold",
                                 prop, rent=rent)
    if err is not None: return [], err, label
    if rent: return rows, None, label
    return _rapi_in_horizon(rows, past_days), None, label

def _src_things4u_realty_us_sold(prop, past_days, radius_mi=1.0):
    return _src_things4u(prop, past_days, radius_mi, RAPIDAPI_T4U_US_HOST,
                         RAPIDAPI_SRC_T4U_US)

def _src_things4u_realtor_search_sold(prop, past_days, radius_mi=1.0):
    return _src_things4u(prop, past_days, radius_mi, RAPIDAPI_T4U_RS_HOST,
                         RAPIDAPI_SRC_T4U_RS)

def _src_things4u_realty_us_rent(prop, past_days, radius_mi=1.0):
    return _src_things4u(prop, past_days, radius_mi, RAPIDAPI_T4U_US_HOST,
                         RAPIDAPI_SRC_T4U_US, rent=True)

def _src_things4u_realtor_search_rent(prop, past_days, radius_mi=1.0):
    return _src_things4u(prop, past_days, radius_mi, RAPIDAPI_T4U_RS_HOST,
                         RAPIDAPI_SRC_T4U_RS, rent=True)

def _src_reefapi_sold(prop, past_days, radius_mi=1.0):
    """ReefAPI sold, queried by ZIP and PAGINATED (v42). The v41 form —
    one page of the 100 newest sales in the whole CITY — held nothing
    inside a 1-mile gate on the live pull: ZIP narrows the net
    (playground: meta.total 3,484 for the ZIP vs 3,535 city-wide) and
    the page walk (pages of 100) digs past the newest
    cluster. After each page the accumulated rows are counted against
    BOTH gates (distance via _rapi_count_in_gate on horizon-filtered
    rows); the walk stops at RAPIDAPI_PAGE_STOP in-gate rows, a short
    page, meta.total exhaustion, or the page cap — RAPIDAPI_REEF_PAGES
    for a Last-year pull, RAPIDAPI_REEF_PAGES_WIDE when the window is
    wider (v44: older fixture-street sales sit deeper in the walk).
    A page error after page 1 keeps the accumulated rows; on page 1
    it is the source's failure. A full street address as location
    does NOT narrow (playground-verified — it returns generic city
    results), so it is never sent; no ZIP falls back to "City, ST"."""
    zipc = (prop.get("zip") if hasattr(prop, "get") else prop["zip"]) or ""
    loc = str(zipc).strip() or f"{prop['city']}, {prop['state']}"
    rows = []
    max_pages = RAPIDAPI_REEF_PAGES_WIDE if past_days > 365 else RAPIDAPI_REEF_PAGES
    for page in range(max_pages):
        body = {"location": loc, "sort": "newest", "limit": 100,
                "offset": page * 100}
        data, err = _rapidapi_post(RAPIDAPI_REEF_HOST, "/realtor/v1/sold", body)
        if err is not None:
            if page == 0: return [], err, RAPIDAPI_SRC_REEF
            break
        items = _rapi_dig(data, "data", "results")
        items = items if isinstance(items, list) else []
        rows.extend(_rapi_in_horizon(
            _rapi_rows(items, _reef_item_to_row), past_days))
        total = _rapi_dig(data, "meta", "total")
        if len(items) < 100: break
        if isinstance(total, (int, float)) and (page + 1) * 100 >= total: break
        if _rapi_count_in_gate(rows, prop, radius_mi) >= RAPIDAPI_PAGE_STOP: break
    return rows, None, RAPIDAPI_SRC_REEF

def _src_redfin_sold(prop, past_days, radius_mi=1.0):
    # v45: WHICH empty this was is part of the outcome. "No results"
    # used to cover the region never resolving (the two live pulls —
    # pre-v46 the auto-complete request itself was malformed, see
    # _redfin_region_id; an unresolvable region remains possible for
    # places Redfin doesn't know), the search answering empty, and
    # the search failing. Each non-serving path now leaves a distinct side-note
    # (the _rapi_note channel, taken into the outcome's LOG text by
    # _rapidapi_sold_frame; the banner keeps its clean reason).
    # v47: the SERVING path leaves one too — before v47 a successful
    # search wrote no note at all, so no pull's pool-detail line ever
    # named its resolved region (2026-11-05 00:44 live observation).
    _rapi_take_note("redfin_sold")  # discard any stale note from a direct call
    rid, rlabel, err = _redfin_region_id(prop)
    _rname = f"{rlabel} {rid}" if rlabel else rid
    if err is not None:
        _rapi_note("redfin_sold", "region lookup failed")
        return [], err, RAPIDAPI_SRC_REDFIN
    if not rid:
        _rapi_note("redfin_sold",
                   "region unresolved — auto-complete returned no region id")
        return [], None, RAPIDAPI_SRC_REDFIN
    # soldWithin follows the pull window (v44): floored at 365 — the
    # value every pre-v44 window used, so existing windows behave
    # exactly as before — and capped at 1095, the deepest the client
    # horizon can use; an All-time pull asks Redfin for its deepest
    # 3 years and the (unbounded) client horizon keeps what returns.
    data, err = _rapidapi_get(RAPIDAPI_REDFIN_HOST, "/properties/search-sold",
                              {"regionId": rid, "limit": 100,
                               "soldWithin": min(max(past_days, 365), 1095)})
    if err is not None:
        _rapi_note("redfin_sold", f"region {_rname} resolved; search failed")
        return [], err, RAPIDAPI_SRC_REDFIN
    items = data.get("data") if isinstance(data, dict) else None
    items = items if isinstance(items, list) else []
    if not items:
        _rapi_note("redfin_sold", f"region {_rname} resolved; search returned no rows")
    else:
        _rapi_note("redfin_sold",
                   f"region {_rname} resolved; search returned {len(items)} rows")
    rows = _rapi_rows(items, _redfin_item_to_row)
    return _rapi_in_horizon(rows, past_days), None, RAPIDAPI_SRC_REDFIN

def _src_sabri_forsold(prop, past_days, radius_mi=1.0):
    # Parameter names are the API's OWN (v43): its field list spells
    # latitude/longitude in full, and its live 400 validation body
    # keys the complaint "latitude" — the app sent lat/lon, which the
    # validator does not recognize as the required field.
    # v44: the top-up PAGES (1..RAPIDAPI_SABRI_PAGES of ~50): one page
    # of area results rarely reaches the subject's own street (the
    # v43 live top-up's single page held none of the fixture-street
    # sales). The walk stops on a short page, on the API's reported
    # total, or once Sabri's own accumulated rows reach
    # RAPIDAPI_UNION_RICH (the union is then rich by definition). On
    # finite windows it also sends sold_date-min at the same horizon
    # start the client filter applies — the playground's parameter
    # list names it, so the server's net and the client's agree. That
    # parameter is playground-listed but not live-verified: if a call
    # carrying it errors (any HTTP error), the page is retried ONCE
    # without it, later pages go without it directly, and the
    # fallback is noted on the source's outcome record (via
    # _rapi_note) — self-healing, never silent. A page-1 failure
    # (after any retry) is the source's failure; a later page's
    # failure keeps the accumulated rows.
    rows = []
    date_min = None if past_days >= 36500 else _rapi_horizon_cutoff(past_days)
    use_date_min = date_min is not None
    for page in range(1, RAPIDAPI_SABRI_PAGES + 1):
        params = {"latitude": prop["lat"], "longitude": prop["lon"],
                  "radius": _sabri_radius(radius_mi), "page": page}
        if use_date_min: params["sold_date-min"] = date_min
        data, err = _rapidapi_get(RAPIDAPI_SABRI_HOST, "/search/forsold/coordinates", params)
        if err is not None and use_date_min and _err_status(err) != 429:
            # v58: a 429 is QUOTA, not a parameter problem — retrying
            # without sold_date-min would burn a second request on a
            # dead quota. Only non-429 errors get the retry.
            _rapi_note("sabri_forsold",
                       f"sold_date-min rejected ({_rapi_fail_reason(err)})"
                       " — page retried without it")
            use_date_min = False
            params.pop("sold_date-min")
            data, err = _rapidapi_get(RAPIDAPI_SABRI_HOST, "/search/forsold/coordinates", params)
        if err is not None:
            if page == 1: return [], err, RAPIDAPI_SRC_SABRI
            break
        items = data.get("properties") if isinstance(data, dict) else None
        items = items if isinstance(items, list) else []
        rows.extend(_rapi_rows(items, _sabri_item_to_row))
        total = data.get("total") if isinstance(data, dict) else None
        if len(items) < RAPIDAPI_SABRI_PAGE_SIZE: break
        if isinstance(total, (int, float)) and len(rows) >= total: break
        if _rapi_count_in_gate(_rapi_in_horizon(rows, past_days),
                               prop, radius_mi) >= RAPIDAPI_UNION_RICH: break
    return _rapi_in_horizon(rows, past_days), None, RAPIDAPI_SRC_SABRI

def _src_sabri_forrent(prop, past_days, radius_mi=1.0):
    # v46: PAGES like forsold (1..RAPIDAPI_SABRI_PAGES of ~50): the
    # pre-v46 single page of area rentals held nothing storable on
    # either live pull (13 rows, 0 in-gate at West Chicago) — rentals
    # are sparse and the page is not distance-ordered. The walk stops
    # on a short page, on the API's reported total (counted in ITEMS:
    # one community item expands to several rows), or once the
    # accumulated rows reach RAPIDAPI_UNION_RICH in-gate. A page-1
    # failure is the source's failure; a later page's failure keeps
    # the accumulated rows. Items normalize through _sabri_rent_rows
    # (the captured price resolution: top-level list_price, else one
    # row per priced unit, else priceless).
    rows, items_seen = [], 0
    for page in range(1, RAPIDAPI_SABRI_PAGES + 1):
        data, err = _rapidapi_get(RAPIDAPI_SABRI_HOST, "/search/forrent/coordinates",
                                  {"latitude": prop["lat"], "longitude": prop["lon"],
                                   "radius": _sabri_radius(radius_mi), "page": page})
        if err is not None:
            if page == 1: return [], err, RAPIDAPI_SRC_SABRI
            break
        items = data.get("properties") if isinstance(data, dict) else None
        items = items if isinstance(items, list) else []
        items_seen += len(items)
        rows.extend(_rapi_rows_multi(items, _sabri_rent_rows))
        total = data.get("total") if isinstance(data, dict) else None
        if len(items) < RAPIDAPI_SABRI_PAGE_SIZE: break
        if isinstance(total, (int, float)) and items_seen >= total: break
        if _rapi_count_in_gate_rent(rows, prop, radius_mi) >= RAPIDAPI_UNION_RICH: break
    return rows, None, RAPIDAPI_SRC_SABRI

def _pz_subject_values(det):
    """One private-zillow propertyDetails object -> the candidate
    subject fill values (pure; no I/O). Shape is the playground
    capture (2026-11-05): bedrooms / bathrooms / livingArea,
    resoFacts.yearBuilt, zestimate, latitude/longitude, photos at
    originalPhotos[].mixedSources.jpeg[].url (several widths — the
    LARGEST is kept), hiResImageLink / desktopWebHdpImageLink as
    fallbacks. `price` on an off-market (homeStatus OTHER) property
    IS the Zestimate — it is read only as the estimate fallback and
    is NEVER a list price. The address sub-object's city/ZIP feed the
    sanity match (_pz_subject_match); they are read defensively and
    may be absent, in which case the match falls back to coords."""
    if not isinstance(det, dict): return {}
    photos = []
    for ph in det.get("originalPhotos") or []:
        if not isinstance(ph, dict): continue
        jpegs = _rapi_dig(ph, "mixedSources", "jpeg") or []
        best, best_w = None, -1
        for j in jpegs:
            if not isinstance(j, dict): continue
            u = j.get("url")
            if not u: continue
            try: w = float(j.get("width") or 0)
            except (TypeError, ValueError): w = 0
            if w >= best_w: best, best_w = u, w
        if best and best not in photos: photos.append(best)
    if not photos:
        for k in ("hiResImageLink", "desktopWebHdpImageLink"):
            if _s(det.get(k)):
                photos = [_s(det.get(k))]; break
    addr = det.get("address") if isinstance(det.get("address"), dict) else {}
    rf = det.get("resoFacts") if isinstance(det.get("resoFacts"), dict) else {}
    yb = _sabri_num(rf.get("yearBuilt"))
    return {"beds": _sabri_num(det.get("bedrooms")),
            "baths": _sabri_num(det.get("bathrooms")),
            "sqft": _sabri_num(det.get("livingArea")),
            "year_built": int(yb) if yb else None,
            "lat": _sabri_num(det.get("latitude")), "lon": _sabri_num(det.get("longitude")),
            "photos": photos,
            "zestimate": _sabri_num(det.get("zestimate")) or _sabri_num(det.get("price")),
            "city": _s(addr.get("city")) or _s(det.get("city")),
            "zip": _s(addr.get("zipcode")) or _s(det.get("zipcode")) or _s(det.get("zip")),
            "zpid": det.get("zpid")}

def _pz_subject_match(prop, det):
    """Sanity gate for the Zillow subject fill (v48): a detail payload
    whose city or ZIP (when it carries them) contradicts the subject
    is NO match and fills nothing — an address lookup that resolved
    to a different property must never write its specs/photos here.
    When both sides carry coordinates, the payload must sit within a
    mile of the subject (a same-name street across town fails). With
    neither signal present the by-address endpoint's own resolution
    stands."""
    v = _pz_subject_values(det)
    if v["city"] and prop["city"] and v["city"].strip().lower() != prop["city"].strip().lower():
        return False
    if v["zip"] and prop["zip"] and str(v["zip"])[:5] != str(prop["zip"])[:5]:
        return False
    if v["lat"] is not None and v["lon"] is not None and prop["lat"] is not None and prop["lon"] is not None:
        dist = haversine_mi(prop["lat"], prop["lon"], v["lat"], v["lon"])
        if dist is not None and dist > 1.0: return False
    return True

def _zillow_subject_fill(d, prop, today):
    """The private-zillow subject lookup (v48, item A): ONE
    /By Property Address call filling the subject's BLANK specs and
    photos from Zillow's own record — the HomeHarvest subject path
    cannot answer on egress-restricted hosts (its listing frames die
    at the proxy), which left PA subjects with no specs and no photos.
    Quota discipline (250/mo hard cap): the caller fires this only
    while the subject still lacks specs or photos, and this function
    makes one logical call — the v49 bounded retry inside _pz_get may
    repeat the HTTP request on a transient 503/404 (the wrapper's
    by-address resolution flickers; a 404 surviving every attempt is
    genuinely not-on-Zillow and degrades as before). Blank-fill ONLY (never overwrites
    user/saved data); zestimate lands ONLY in est_zillow, never
    list_price. Returns {"photos": n, "fields": [...]} when it filled
    something, else None (no key / error / 404 = address not on
    Zillow / sanity no-match / nothing blank to fill) — every one of
    those degrades silently to the pre-v48 honest state, logged."""
    if not _rapidapi_key(): return None
    data, err, attempts, last_status = _pz_get("/By Property Address",
                                               {"address": prop["address"]},
                                               retry_404=True)
    if err is not None:
        msg = (f"comp pull: private-zillow subject lookup failed for "
               f"{prop['address']}: {_rapi_fail_reason(err)}")
        if attempts > 1:
            msg += f" (after {attempts} attempts, last status {last_status})"
        app.logger.warning(msg)
        return None
    det = data.get("propertyDetails") if isinstance(data, dict) else None
    if not isinstance(det, dict) or not det:
        app.logger.warning(f"comp pull: private-zillow subject lookup for "
                           f"{prop['address']}: no propertyDetails in response")
        return None
    if not _pz_subject_match(prop, det):
        app.logger.warning(f"comp pull: private-zillow subject lookup for "
                           f"{prop['address']}: response city/ZIP/coords contradict "
                           f"the subject — treated as no-match, nothing filled")
        return None
    v = _pz_subject_values(det)
    if v.get("zpid") and not prop["zpid"]:
        # v56: persist the zpid the by-address payload carried. The
        # zpid-only backup APIs (ReefAPI Zillow detail, Unofficial
        # images) can only answer with it — v48 extracted it in
        # _pz_subject_values and then discarded it, so no backup path
        # could ever fire for a subject the primary path had touched.
        d.execute("UPDATE properties SET zpid=? WHERE id=?",
                  (str(v["zpid"]), prop["id"]))
        d.commit()
    fills = {}
    for col in ("beds", "baths", "sqft", "year_built", "lat", "lon"):
        if not prop[col] and v[col] and _sane_subject_fill(col, v[col]):
            fills[col] = v[col]
    if v["photos"] and not prop["photos_json"] and not prop["photo_url"]:
        fills["photos_json"] = json.dumps(v["photos"]); fills["photos_date"] = today
        fills["photo_url"] = v["photos"][0]
    if v["zestimate"] and not prop["est_zillow"] and _sane_subject_fill("est_zillow", v["zestimate"]):
        fills["est_zillow"] = v["zestimate"]; fills["est_date"] = today
    if not fills:
        app.logger.warning(f"comp pull: private-zillow subject lookup for "
                           f"{prop['address']}: matched, but every field was already filled")
        return None
    d.execute(f"UPDATE properties SET {','.join(k + '=?' for k in fills)} WHERE id=?",
              (*fills.values(), prop["id"]))
    app.logger.warning(f"comp pull: private-zillow subject lookup for {prop['address']}: "
                       f"filled {', '.join(sorted(fills))} "
                       f"({attempts} call{'s' if attempts > 1 else ''}, RapidAPI (Zillow))")
    return {"photos": len(v["photos"]) if "photos_json" in fills else 0,
            "fields": sorted(fills)}

# ---------------------------------------------------------------- v58 subject-fill sanity + Things4u detail
def _sane_subject_fill(col, v):
    """The subject-fill sanity guard (v58): a value is storable only
    when it is plausible for its field. ReiSearch itself auto-filled
    108 Ridgeland Ave's sqft as '1' (measured live 2026-11-06) — this
    guard exists so our app never reproduces that: sqft < 100 is
    missing, a $0 price is missing, a year outside 1800..next-year is
    missing. Applied at EVERY fill site (frame rows, pz, ReefAPI
    Zillow, Things4u detail)."""
    if v is None: return False
    try:
        n = float(v)
    except (TypeError, ValueError):
        return False
    if col == "sqft": return 100 <= n <= 100000
    if col in ("beds", "baths"): return 0 <= n <= 30
    if col == "year_built": return 1800 <= n <= datetime.date.today().year + 1
    if col == "lot_sqft": return 0 < n <= 10000000
    if col in ("list_price", "est_zillow", "est_realtor", "est_county"): return n > 0
    if col == "lat": return -90 <= n <= 90
    if col == "lon": return -180 <= n <= 180
    return True

def _things4u_detail_values(payload):
    """One Things4u detail payload -> candidate subject fill values
    (pure; no I/O). The detail object sits at data (or is the payload
    itself) and is read through the SAME field extraction as a search
    row (v58.1): description.beds/baths/sqft/lot_sqft,
    location.address.* (line/city/postal_code/coordinate), prices
    last_sold_price/list_price, primary_photo/photos, details[] Year
    Built text. The detail payload's exact key list was NOT captured
    live (the shape capture spent its one detail call on an
    auto-complete id and got 400) — the caller logs the payload's
    top-level keys on first use, and an empty/unparseable payload is
    an honest {} (never a shape error). livingArea null is sqft None
    — never 0, never 1, never invented."""
    if not isinstance(payload, dict): return {}
    det = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(det, dict) or not det: return {}
    f = _things4u_fields(det)
    zest = _sabri_num(det.get("zestimate"))
    if zest is None and isinstance(det.get("estimate"), (int, float)):
        zest = _sabri_num(det.get("estimate"))
    return {"beds": f["beds"], "baths": f["full_baths"], "sqft": f["sqft"],
            "year_built": f["year_built"], "lot_sqft": f["lot_sqft"],
            "lat": f["latitude"], "lon": f["longitude"],
            "photos": list(f["alt_photos"] or []),
            "list_price": f["list_price"], "zestimate": zest,
            "city": f["city"], "zip": f["zip_code"]}

def _things4u_subject_fill(d, prop, today, property_id=None):
    """The Things4u subject detail fill (v58.1): ONE detail call per
    host with the subject's NUMERIC property_id, taken from the
    subject's own search-row in the pull's frames (passed in by the
    caller — the frame row carries it via the property_id frame
    column). Auto-complete is NOT used here: it returns areas/streets
    only, and its ids ("postal_code:…", "street:…") are rejected by
    /properties/detail with 400 PROPERTY_ID_VALIDATION_ERROR
    ("propertyId must be a number", captured 2026-11-06). A missing or
    non-numeric id is an honest None with no request at all — the
    frame-row fill stands alone. Blank-fill ONLY, sanity-guarded per
    field; the Zestimate-shaped price lands in est_realtor, never
    list_price. Returns the fill summary or None. Realty US first,
    Realtor Search second (same data — the second fires only if the
    first did not serve)."""
    if not _rapidapi_key(): return None
    pid = str(property_id).strip() if property_id is not None else ""
    if not pid or not pid.isdigit():
        app.logger.warning(f"comp pull: Things4u subject detail for {prop['address']}: "
                           "no numeric property_id from the subject's search row — "
                           "detail skipped (auto-complete ids are not propertyIds)")
        return None
    for host in (RAPIDAPI_T4U_US_HOST, RAPIDAPI_T4U_RS_HOST):
        det, derr = _rapidapi_get(host, "/properties/detail", {"propertyId": pid})
        if derr is not None or det is None: continue
        if isinstance(det, dict):
            app.logger.warning(f"comp pull: Things4u detail payload top-level keys: "
                               f"{sorted(det.keys())}")
        v = _things4u_detail_values(det)
        if not v: continue
        if v["city"] and prop["city"] and v["city"].strip().lower() != prop["city"].strip().lower():
            app.logger.warning(f"comp pull: Things4u subject detail for {prop['address']}: "
                               "response city contradicts the subject — no-match")
            return None
        if v["zip"] and prop["zip"] and str(v["zip"])[:5] != str(prop["zip"])[:5]:
            app.logger.warning(f"comp pull: Things4u subject detail for {prop['address']}: "
                               "response ZIP contradicts the subject — no-match")
            return None
        fills = {}
        for col in ("beds", "baths", "sqft", "year_built", "lot_sqft", "lat", "lon"):
            if not prop[col] and v.get(col) is not None and _sane_subject_fill(col, v[col]):
                fills[col] = v[col]
        if v["photos"] and not prop["photos_json"] and not prop["photo_url"]:
            fills["photos_json"] = json.dumps(v["photos"]); fills["photos_date"] = today
            fills["photo_url"] = v["photos"][0]
        if v["list_price"] and not prop["list_price"] and _sane_subject_fill("list_price", v["list_price"]):
            fills["list_price"] = v["list_price"]
        if v["zestimate"] and not prop["est_realtor"] and _sane_subject_fill("est_realtor", v["zestimate"]):
            fills["est_realtor"] = v["zestimate"]; fills["est_date"] = today
        if not fills:
            app.logger.warning(f"comp pull: Things4u subject detail for {prop['address']}: "
                               "matched, but every field was already filled")
            return None
        d.execute(f"UPDATE properties SET {','.join(k + '=?' for k in fills)} WHERE id=?",
                  (*fills.values(), prop["id"]))
        app.logger.warning(f"comp pull: Things4u subject detail for {prop['address']}: "
                           f"filled {', '.join(sorted(fills))}")
        return {"photos": len(v["photos"]) if "photos_json" in fills else 0,
                "fields": sorted(fills)}
    app.logger.warning(f"comp pull: Things4u subject detail for {prop['address']}: "
                       "no id/detail served")
    return None

# v56: the two Zillow BACKUP subject paths (capture doc 2026-11-05).
# Both are zpid-only APIs, which is why the primary fill now persists
# the zpid: when private-zillow's by-address path cannot answer from
# a host (live PA evidence all night 2026-11-05: 503 throttling, then
# 404s), these are the remaining free routes to the subject's own
# Zillow record — same blank-fill discipline, same sanity posture.
RAPIDAPI_REEF_ZILLOW_HOST = "zillow-real-estate-data-api.p.rapidapi.com"
RAPIDAPI_UNOFF_ZILLOW_HOST = "unofficial-zillow-api2.p.rapidapi.com"

def _pz_autocomplete_zpid(prop):
    """ONE private-zillow /autocomplete attempt resolving the
    subject's zpid from its full address (v56). The response shape
    was not captured in the playground (the by-address endpoint made
    it unnecessary there), so the zpid is searched recursively —
    any nesting that carries one answers; no zpid anywhere is an
    honest None and no further backup fires."""
    data, err = _rapidapi_get(RAPIDAPI_PZ_HOST, "/autocomplete",
                              {"query": prop["address"]})
    if err is not None or data is None: return None
    def _find(o):
        if isinstance(o, dict):
            z = o.get("zpid")
            if z not in (None, ""): return z
            for v in o.values():
                r = _find(v)
                if r is not None: return r
        elif isinstance(o, list):
            for v in o:
                r = _find(v)
                if r is not None: return r
        return None
    z = _find(data)
    return str(z) if z is not None else None

def _reef_zillow_values(det):
    """One ReefAPI Zillow /property_detail `data` object -> candidate
    subject fill values (pure; no I/O). Captured shape (2026-11-05):
    beds / baths / sqft / year_built, zestimate_usd, list_price_usd
    (the SAME Zestimate-equivalent on an off-market property — read
    only as the estimate, never a list price), latitude / longitude,
    photos[] as plain URL strings."""
    if not isinstance(det, dict): return {}
    photos = []
    for u in det.get("photos") or []:
        u = _s(u)
        if u.startswith("http") and u not in photos: photos.append(u)
    return {"beds": _sabri_num(det.get("beds")),
            "baths": _sabri_num(det.get("baths")),
            "sqft": _sabri_num(det.get("sqft")),
            "year_built": (int(_sabri_num(det.get("year_built")))
                           if _sabri_num(det.get("year_built")) else None),
            "lat": _sabri_num(det.get("latitude")), "lon": _sabri_num(det.get("longitude")),
            "photos": photos,
            "zestimate": _sabri_num(det.get("zestimate_usd")) or _sabri_num(det.get("list_price_usd")),
            "city": _s(det.get("city")), "zip": _s(det.get("zipcode")) or _s(det.get("zip"))}

def _reef_zillow_subject_fill(d, prop, today):
    """The ReefAPI Zillow subject backup (v56): POST /property_detail
    by zpid — the STORED zpid when the subject has one, else ONE pz
    /autocomplete attempt. Fires only when the primary by-address
    fill returned nothing. Sanity posture mirrors _pz_subject_match
    (a contradicting city/ZIP, or coordinates over a mile away, is a
    no-match and fills nothing — and does not persist the zpid).
    A sanity-passed payload persists its zpid even when every field
    was already filled, so the images fallback below can trust it.
    Blank-fill ONLY; the Zestimate-shaped price lands in est_zillow,
    never list_price. Returns the fill summary or None."""
    if not _rapidapi_key(): return None
    zpid = prop["zpid"] or _pz_autocomplete_zpid(prop)
    if not zpid:
        app.logger.warning(f"comp pull: ReefAPI Zillow subject backup for "
                           f"{prop['address']}: no zpid resolvable — backup cannot fire")
        return None
    data, err = _rapidapi_post(RAPIDAPI_REEF_ZILLOW_HOST, "/property_detail",
                               {"zpid": str(zpid)})
    if err is not None:
        app.logger.warning(f"comp pull: ReefAPI Zillow subject backup failed for "
                           f"{prop['address']}: {_rapi_fail_reason(err)}")
        return None
    det = data.get("data") if isinstance(data, dict) else None
    if not isinstance(det, dict) or not det:
        app.logger.warning(f"comp pull: ReefAPI Zillow subject backup for "
                           f"{prop['address']}: no data in response")
        return None
    v = _reef_zillow_values(det)
    if v["city"] and prop["city"] and v["city"].strip().lower() != prop["city"].strip().lower():
        app.logger.warning(f"comp pull: ReefAPI Zillow subject backup for "
                           f"{prop['address']}: city contradicts the subject — no-match")
        return None
    if v["zip"] and prop["zip"] and str(v["zip"])[:5] != str(prop["zip"])[:5]:
        app.logger.warning(f"comp pull: ReefAPI Zillow subject backup for "
                           f"{prop['address']}: ZIP contradicts the subject — no-match")
        return None
    if v["lat"] is not None and v["lon"] is not None and prop["lat"] is not None and prop["lon"] is not None:
        dist = haversine_mi(prop["lat"], prop["lon"], v["lat"], v["lon"])
        if dist is not None and dist > 1.0:
            app.logger.warning(f"comp pull: ReefAPI Zillow subject backup for "
                               f"{prop['address']}: payload sits {dist} mi away — no-match")
            return None
    if not prop["zpid"]:
        d.execute("UPDATE properties SET zpid=? WHERE id=?", (str(zpid), prop["id"]))
        d.commit()
    fills = {}
    for col in ("beds", "baths", "sqft", "year_built", "lat", "lon"):
        if not prop[col] and v[col] and _sane_subject_fill(col, v[col]):
            fills[col] = v[col]
    if v["photos"] and not prop["photos_json"] and not prop["photo_url"]:
        fills["photos_json"] = json.dumps(v["photos"]); fills["photos_date"] = today
        fills["photo_url"] = v["photos"][0]
    if v["zestimate"] and not prop["est_zillow"] and _sane_subject_fill("est_zillow", v["zestimate"]):
        fills["est_zillow"] = v["zestimate"]; fills["est_date"] = today
    if not fills:
        app.logger.warning(f"comp pull: ReefAPI Zillow subject backup for "
                           f"{prop['address']}: matched, but every field was already filled")
        return None
    d.execute(f"UPDATE properties SET {','.join(k + '=?' for k in fills)} WHERE id=?",
              (*fills.values(), prop["id"]))
    app.logger.warning(f"comp pull: ReefAPI Zillow subject backup for {prop['address']}: "
                       f"filled {', '.join(sorted(fills))} (zpid {zpid})")
    return {"photos": len(v["photos"]) if "photos_json" in fills else 0,
            "fields": sorted(fills)}

def _unofficial_zillow_photos_fill(d, prop, today):
    """The third subject fallback (v56): Unofficial-Zillow images by
    zpid (captured shape: data.images[].url — 23 for Garland). It
    fills PHOTOS ONLY, and only under a TRUSTED zpid: the stored
    column, which is written solely by a sanity-matched primary or
    ReefAPI payload. With no trusted zpid it never fires — an
    address-autocomplete guess alone does not put another house's
    photos on this subject. Returns the fill summary or None."""
    if not _rapidapi_key(): return None
    zpid = prop["zpid"]
    if not zpid: return None
    if prop["photos_json"] or prop["photo_url"]: return None
    data, err = _rapidapi_get(RAPIDAPI_UNOFF_ZILLOW_HOST, "/api/property/images",
                              {"zpid": str(zpid)})
    if err is not None:
        app.logger.warning(f"comp pull: Unofficial-Zillow images failed for "
                           f"{prop['address']}: {_rapi_fail_reason(err)}")
        return None
    imgs = _rapi_dig(data, "data", "images")
    photos = []
    for im in imgs if isinstance(imgs, list) else []:
        u = _s(im.get("url")) if isinstance(im, dict) else _s(im)
        if u.startswith("http") and u not in photos: photos.append(u)
    if not photos:
        app.logger.warning(f"comp pull: Unofficial-Zillow images for "
                           f"{prop['address']}: no images in response")
        return None
    d.execute("UPDATE properties SET photos_json=?, photos_date=?, photo_url=? WHERE id=?",
              (json.dumps(photos), today, photos[0], prop["id"]))
    app.logger.warning(f"comp pull: Unofficial-Zillow images for {prop['address']}: "
                       f"filled {len(photos)} photos (zpid {zpid})")
    return {"photos": len(photos), "fields": ["photo_url", "photos_date", "photos_json"]}

def _pz_sold_item_to_row(item):
    """One private-zillow /search/bymapbounds item -> the standard
    HomeHarvest columns (playground-captured 2026-11-05): the sold
    price is price.value, the date lastSoldDate as EPOCH MILLISECONDS,
    and the row's listing URL is Zillow's own homedetails page built
    from its zpid (the zpid is data; the URL form is Zillow's). The
    address arrives as a sub-object (streetAddress/city/state/zipcode)
    or as one "street, city, ST ZIP" string — both are read. A row
    without a usable price keeps sold_price=None and the shared gate
    drops it as no_price, like every source."""
    addr = item.get("address")
    street = city = zipc = ""
    if isinstance(addr, dict):
        street = _s(addr.get("streetAddress")); city = _s(addr.get("city"))
        zipc = _s(addr.get("zipcode"))
    elif isinstance(addr, str):
        parts = [p.strip() for p in addr.split(",")]
        street = parts[0] if parts else ""
        if len(parts) >= 2: city = parts[1]
        if len(parts) >= 3:
            tail = parts[2].split()
            if tail: zipc = tail[-1]
    price = item.get("price")
    if isinstance(price, dict): price = price.get("value")
    ms = item.get("lastSoldDate")
    date_iso = None
    if isinstance(ms, (int, float)) and ms > 0:
        try:
            date_iso = datetime.datetime.fromtimestamp(
                ms / 1000, tz=datetime.timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            date_iso = None
    elif isinstance(ms, str) and ms[:4].isdigit():
        date_iso = ms[:10]
    thumb = item.get("thumb") or []
    photo = None
    if isinstance(thumb, list) and thumb and isinstance(thumb[0], dict):
        photo = _s(thumb[0].get("url")) or None
    zpid = item.get("zpid")
    return dict(street=street, city=city, zip_code=zipc,
                sold_price=_sabri_num(price), list_price=None,
                sqft=_sabri_num(item.get("sqft") or item.get("livingArea")),
                beds=_sabri_num(item.get("beds") or item.get("bedrooms")),
                full_baths=_sabri_num(item.get("baths") or item.get("bathrooms")),
                year_built=None, lot_sqft=None, last_sold_date=date_iso,
                days_on_mls=None,
                latitude=item.get("latitude"), longitude=item.get("longitude"),
                style=None, property_type=None,
                property_url=(f"https://www.zillow.com/homedetails/{zpid}_zpid/"
                              if zpid else ""),
                primary_photo=photo, alt_photos=[photo] if photo else [],
                estimated_value=None, price_per_sqft=None)

def _src_pzillow_sold(prop, past_days, radius_mi=1.0):
    """private-zillow sold comps by map bounds (v48): ONE call with a
    bounds box derived from the subject coords and the pull radius
    (degrees ~ radius/69 of latitude, longitude scaled by cos(lat)),
    listingStatus=Sold. Top-up source — it joins the union AFTER
    Sabri and is skipped whenever the union is already rich (the
    250/mo hard cap is spent only when the pull still needs rows)."""
    if prop["lat"] is None or prop["lon"] is None:
        return [], None, RAPIDAPI_SRC_PZ
    dlat = (radius_mi or 1.0) / 69.0
    coslat = max(math.cos(math.radians(prop["lat"])), 0.2)
    dlon = (radius_mi or 1.0) / (69.0 * coslat)
    params = {"west": round(prop["lon"] - dlon, 6), "east": round(prop["lon"] + dlon, 6),
              "south": round(prop["lat"] - dlat, 6), "north": round(prop["lat"] + dlat, 6),
              "listingStatus": "Sold"}
    data, err, attempts, last_status = _pz_get("/search/bymapbounds", params)
    if attempts > 1:
        _rapi_note("pzillow_sold",
                   f"{'served' if err is None else 'failed'} after {attempts} attempts "
                   f"(last status {last_status})")
    if err is not None: return [], err, RAPIDAPI_SRC_PZ
    items = data.get("searchResults") if isinstance(data, dict) else None
    items = items if isinstance(items, list) else []
    rows = _rapi_rows(items, _pz_sold_item_to_row)
    return _rapi_in_horizon(rows, past_days), None, RAPIDAPI_SRC_PZ

_RAPIDAPI_ADAPTERS = {"things4u_realty_us_sold": _src_things4u_realty_us_sold,
                      "things4u_realtor_search_sold": _src_things4u_realtor_search_sold,
                      "reefapi_sold": _src_reefapi_sold,
                      "sabri_forsold": _src_sabri_forsold,
                      "things4u_realty_us_rent": _src_things4u_realty_us_rent,
                      "things4u_realtor_search_rent": _src_things4u_realtor_search_rent,
                      "sabri_forrent": _src_sabri_forrent}
_RAPIDAPI_SOURCE_NAMES = {"things4u_realty_us_sold": "Things4u Realty US sold search",
                          "things4u_realtor_search_sold": "Things4u Realtor Search sold search",
                          "reefapi_sold": "ReefAPI Realtor sold",
                          "sabri_forsold": "Realtor16 (Sabri) sold search",
                          "things4u_realty_us_rent": "Things4u Realty US rent search",
                          "things4u_realtor_search_rent": "Things4u Realtor Search rent search",
                          "sabri_forrent": "Realtor16 (Sabri) rent search"}
# v58: retired from the pull path, functions kept for the record.
# redfin_sold — region rows carry no coordinates (live 2026-11-06):
# nothing storable, so it contributed zero comps on every pull.
# pzillow_sold — private-zillow's sold search hangs provider-side
# (503 in its own playground, 2026-11-06): not transient, do not call.
_RAPIDAPI_SOURCE_HOSTS = {"things4u_realty_us_sold": RAPIDAPI_T4U_US_HOST,
                          "things4u_realtor_search_sold": RAPIDAPI_T4U_RS_HOST,
                          "reefapi_sold": RAPIDAPI_REEF_HOST,
                          "sabri_forsold": RAPIDAPI_SABRI_HOST,
                          "things4u_realty_us_rent": RAPIDAPI_T4U_US_HOST,
                          "things4u_realtor_search_rent": RAPIDAPI_T4U_RS_HOST,
                          "sabri_forrent": RAPIDAPI_SABRI_HOST}
RAPIDAPI_SOLD_SOURCES = ("things4u_realty_us_sold", "things4u_realtor_search_sold",
                         "reefapi_sold", "sabri_forsold")
RAPIDAPI_RENT_SOURCES = ("things4u_realty_us_rent", "things4u_realtor_search_rent",
                         "sabri_forrent")

def _rapi_quota_skip_outcome(key):
    """The outcome record for a source SKIPPED on its quota-dead flag
    (v58): same shape as _rapi_outcome's failure record, so the pool
    detail and the banner read it identically — no request was made."""
    name = _RAPIDAPI_SOURCE_NAMES[key]
    return {"name": name, "label": None, "served": False, "rows": 0,
            "in_gate": 0, "reason": "quota reached (HTTP 429)",
            "log": f"{name} — skipped: quota reached earlier this month (HTTP 429)"}

def _rapi_call_source(key, prop, past_days, radius_mi):
    """One pool source, fully contained (the v40 rule): ANY failure —
    including an exception raised while normalizing an unexpected
    response shape — is just "didn't serve" with the error attached.
    It must never escape and mask the sources behind it (pre-fix, a
    normalizer AttributeError aborted the whole pull before
    HomeHarvest even ran — rapidapi-live-shapes-2026-11-04.md §6).
    v58: a 429 marks the host quota-dead (settings) so the frame
    loops skip it for the rest of the month."""
    try:
        rows, err, label = _RAPIDAPI_ADAPTERS[key](prop, past_days, radius_mi)
    except Exception as e:
        rows, err, label = [], e, None
    if err is not None and _err_status(err) == 429:
        _rapidapi_mark_quota_dead(_RAPIDAPI_SOURCE_HOSTS[key])
    return rows, err, label

def _rapi_count_in_gate_rent(rows, prop, radius_mi):
    """The rent twin of _rapi_count_in_gate (v43): rows the RENT merge
    would accept, per the shared _rent_row_gate (price = list_price).
    Rent outcomes counted with the sold test read sold_price and so
    reported every rent row as 0 in-gate once that gate became the
    full acceptance test."""
    return sum(1 for r in rows or [] if _rent_row_gate(r, prop, radius_mi)[0])

def _rapi_outcome(key, rows, err, label, prop, radius_mi, rent=False, note=None):
    """The outcome record for one CALLED source (v42): what it did,
    in numbers. 'log' is the pool-detail text (classified reason + the
    sanitized error-body snippet on failure); 'reason' is the clean
    banner form. In-gate counts use the shared acceptance test for
    the frame's kind (sold: _rapi_count_in_gate; rent:
    _rapi_count_in_gate_rent — v43) over the adapter's served rows.
    A 'note' (v44 — an adapter side-note taken via _rapi_take_note,
    e.g. Sabri's sold_date-min fallback) is appended to the log text
    only; the banner keeps its clean forms."""
    name = _RAPIDAPI_SOURCE_NAMES[key]
    suffix = f" [{note}]" if note else ""
    if rows:
        in_gate = (_rapi_count_in_gate_rent(rows, prop, radius_mi) if rent
                   else _rapi_count_in_gate(rows, prop, radius_mi))
        return {"name": name, "label": label, "served": True,
                "rows": len(rows), "in_gate": in_gate, "reason": None,
                "log": f"{name} — served {len(rows)} rows, {in_gate} in-gate" + suffix}
    reason = _rapi_fail_reason(err)
    return {"name": name, "label": None, "served": False, "rows": 0,
            "in_gate": 0, "reason": reason,
            "log": f"{name} — {_rapi_fail_detail(err)}" + suffix}

def _rapidapi_sold_frame(prop, past_days=365, radius_mi=1.0):
    """The RapidAPI sold side for one pull (v42): a sequential UNION
    with top-up. Returns (frames, err, outcomes): frames is a list of
    (DataFrame, source label) — one per source that served rows;
    outcomes records EVERY source actually called; err is the last
    error when NO source served (the pull then relies on HomeHarvest),
    else None. Sequencing: ReefAPI (paginated) first; if its in-gate
    count reaches RAPIDAPI_UNION_RICH the frame is ReefAPI's alone;
    else Redfin unions in; if the union is STILL under the threshold,
    Sabri tops up. A source that errors never blocks the others."""
    if not _rapidapi_key(): return [], None, []
    frames, outcomes, in_gate_total, last_err = [], [], 0, None
    ss_found = False   # v56: any accumulated row on the subject's own street
    for key in RAPIDAPI_SOLD_SOURCES:
        # v56 SAME-STREET GUARANTEE (Sherif 2026-11-05): the rich-stop
        # may only fire once the accumulated rows include the subject's
        # own street. A rich union of off-street rows (the live Garland
        # failure: 295 comps, none from Garland Ct in the top ranks)
        # must NOT skip the address-centered sources — Sabri's
        # coordinate search, then private-zillow's bounds search —
        # which are the ones that can answer street-level. When a
        # same-street row already exists the stop is exactly as before,
        # so the guarantee adds NO Sabri calls in that case (Sabri is
        # 30/mo; the discipline is part of the rule).
        if frames and in_gate_total >= RAPIDAPI_UNION_RICH and ss_found: break
        # v58: a quota-dead host is skipped without a request (one
        # probe/month); its outcome record keeps the pool detail and
        # the banner truthful.
        if _rapidapi_quota_dead(_RAPIDAPI_SOURCE_HOSTS[key]):
            outcomes.append(_rapi_quota_skip_outcome(key))
            continue
        rows, err, label = _rapi_call_source(key, prop, past_days, radius_mi)
        oc = _rapi_outcome(key, rows, err, label, prop, radius_mi,
                           note=_rapi_take_note(key))
        outcomes.append(oc)
        if err is not None: last_err = err
        if rows:
            frames.append((_hh_frame(rows), label))
            in_gate_total += oc["in_gate"]
            if not ss_found:
                ss_found = any(_same_street(prop, r.get("street")) == 0 for r in rows)
    if not frames: return [], last_err, outcomes
    return frames, None, outcomes

def _rapidapi_rent_frame(prop, radius_mi=1.0):
    """The RapidAPI rental frame: (df, err, source, outcomes) — the
    v41 contract plus the v42 outcome records. df is None when the
    rent pool produced nothing."""
    if not _rapidapi_key(): return None, None, None, []
    outcomes, last_err = [], None
    for key in RAPIDAPI_RENT_SOURCES:
        # v58: quota-dead skip, same as the sold frame.
        if _rapidapi_quota_dead(_RAPIDAPI_SOURCE_HOSTS[key]):
            outcomes.append(_rapi_quota_skip_outcome(key))
            continue
        rows, err, label = _rapi_call_source(key, prop, 365, radius_mi)
        outcomes.append(_rapi_outcome(key, rows, err, label, prop, radius_mi, rent=True))
        if rows: return _hh_frame(rows), None, label, outcomes
        if err is not None: last_err = err
    return None, last_err, None, outcomes

# ---------------------------------------------------------------- v58 jina-Zillow sold
# The nationwide no-quota sold layer (proven 2026-11-06): Zillow's
# sold-results page for the subject's ZIP, read through the r.jina.ai
# reader (no key, no quota, no expiry). Realtor via the reader is
# 429-rate-limited and Redfin via the reader is CAPTCHA/405 — both
# NOT usable; Zillow is the only reader route. The reader returns
# MARKDOWN: rows are "$450,000 — 4 bds, 3 ba, 2,254 sqft — Closed —
# 4N649 Petersdorf Rd, West Chicago, IL 60185 — Sold 11/02/26".
# The page carries no coordinates, so rows are geocoded (Nominatim
# first — PA-reachable — ArcGIS fallback) and pass the pull's radius
# gate like every other row; a row that cannot be geocoded is dropped.
JINA_READER_BASE = "https://r.jina.ai/"
JINA_ZILLOW_PAGES = 3
JINA_GEOCODE_MAX = 15  # rows geocoded per pull (Nominatim politeness)

def _jina_fetch(url):
    """One reader fetch (browser UA). Returns the markdown/HTML text or
    None. The reader is a best-effort top-up: any failure leaves the
    union exactly as it was."""
    if not url: return None
    try:
        r = requests.get(f"{JINA_READER_BASE}{url}", headers={"User-Agent":
                         "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
                         timeout=15)
        r.raise_for_status()
        return r.text
    except Exception as e:
        app.logger.warning(f"comp pull: jina reader fetch failed for {url}: {e!r}")
        return None

def _geocode_nominatim(addr):
    """One Nominatim geocode (PA-reachable; the jina-row path).
    Returns {"lat": .., "lon": ..} or None. The caller sleeps 1s
    between rows (Nominatim's usage policy)."""
    if not addr: return None
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": addr, "format": "json", "limit": 1},
                         headers={"User-Agent": "PropertyDealAnalyzer/1.0 (comp pull)"},
                         timeout=5)
        r.raise_for_status()
        rows = r.json()
        if isinstance(rows, list) and rows:
            return {"lat": float(rows[0]["lat"]), "lon": float(rows[0]["lon"])}
    except Exception:
        return None
    return None

def _jina_zillow_parse(md):
    """Zillow sold markdown -> normalized sold rows (the standard
    HomeHarvest columns, latitude/longitude None until geocoded).
    One row per markdown line of the "$P — N bds, M ba, S sqft —
    Closed — <address> — Sold MM/DD/YY" form. Title/count lines never
    match the form and are ignored. A line that fails any parse step
    is skipped — never an exception, never an invented row."""
    rows = []
    for line in (md or "").splitlines():
        parts = [p.strip() for p in line.split("—")]
        if len(parts) < 5: continue
        try:
            price = int(parts[0].replace("$", "").replace(",", "").strip())
        except (ValueError, AttributeError):
            continue
        m = re.match(r"(\d+)\s+bds?,\s+(\d+(?:\.\d+)?)\s+ba,\s+([\d,]+)\s+sqft",
                     parts[1])
        if not m: continue
        m2 = re.match(r"^(.+?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5})$", parts[3])
        if not m2: continue
        m3 = re.search(r"Sold\s+(\d{1,2})/(\d{1,2})/(\d{2})", parts[4])
        if not m3: continue
        mm, dd, yy = int(m3.group(1)), int(m3.group(2)), int(m3.group(3))
        rows.append(dict(street=m2.group(1), city=m2.group(2), zip_code=m2.group(4),
                         sold_price=price, list_price=None,
                         sqft=int(m.group(3).replace(",", "")),
                         beds=int(m.group(1)), full_baths=float(m.group(2)),
                         year_built=None, lot_sqft=None,
                         last_sold_date=f"20{yy:02d}-{mm:02d}-{dd:02d}",
                         days_on_mls=None, latitude=None, longitude=None,
                         style=None, property_type="Single Family",
                         property_url=None, primary_photo=None, alt_photos=[],
                         estimated_value=None, price_per_sqft=None))
    return rows

def _jina_zillow_sold_frame(prop, radius_mi=1.0, past_days=365):
    """The jina-Zillow sold top-up: (DataFrame, label, err). The
    subject's ZIP sold page via the reader, pages 1..3 with a
    same-rows guard (a page that returns only rows already seen stops
    the walk). Rows are geocoded (Nominatim, 1 req/s, max 15/pull;
    ArcGIS fallback) and horizon-filtered; the merge's radius gate is
    the final filter. ([], label, err) when the reader serves nothing
    — the pull degrades to the union it already has."""
    label = "Zillow (reader)"
    city = _s(prop.get("city")); state = _s(prop.get("state"))
    zipc = str(prop.get("zip") or "")[:5]
    if not city or not state or not zipc: return [], label, None
    slug = (re.sub(r"[^a-z0-9]+", "-", city.strip().lower()).strip("-")
            + "-" + state.strip().lower() + "-" + zipc)
    base = f"https://www.zillow.com/{slug}/sold/"
    rows, seen, geocoded = [], set(), 0
    for page in range(1, JINA_ZILLOW_PAGES + 1):
        url = base if page == 1 else f"{base}?page={page}"
        md = _jina_fetch(url)
        if md is None:
            if page == 1: return [], label, Exception("jina reader unreachable")
            break
        parsed = _jina_zillow_parse(md)
        new = [r for r in parsed if r["street"] not in seen]
        if not new: break  # same-rows guard (or an empty page)
        for r in new:
            seen.add(r["street"])
        for r in new:
            if geocoded >= JINA_GEOCODE_MAX: break
            addr = f"{r['street']}, {r['city']}, {state} {r['zip_code']}"
            g = _geocode_nominatim(addr)
            if g is None:
                g2 = geocode_arcgis(addr)
                if g2: g = {"lat": g2.get("lat"), "lon": g2.get("lon")}
            if g and g.get("lat") is not None and g.get("lon") is not None:
                r["latitude"], r["longitude"] = float(g["lat"]), float(g["lon"])
                geocoded += 1
                rows.append(r)
            time.sleep(1)  # Nominatim politeness (also paces the ArcGIS fallback)
        if geocoded >= JINA_GEOCODE_MAX: break
    if not rows: return [], label, None
    return _hh_frame(_rapi_in_horizon(rows, past_days)), label, None

# ---------------------------------------------------------------- FL Statewide
# Cadastral (v48) — OFFICIAL-RECORD sold comps for Florida subjects:
# the Florida Dept. of Revenue's statewide parcel layer (each county
# appraiser's tax-roll data, one schema for all 67 counties), served
# as an ArcGIS FeatureServer — no key, no signup. Fields (read from
# the live layer, research report 2026-11-05 section A): SALE_PRC1
# with SALE_YR1/SALE_MO1 (+ the PRC2/YR2/MO2 prior-sale set),
# TOT_LVG_AR living sqft, ACT_YR_BLT / EFF_YR_BLT, PHY_ADDR1/CITY/
# ZIPCD, PARCEL_ID, LND_SQFOOT. HARD LIMITS of the source, honored
# here: it carries NO beds/baths (rows store NULL — never invented),
# and sale dates are YEAR+MONTH only — the stored event_date is
# "YYYY-MM" and the display renders month+year (datefmt), never a
# fabricated day. QUAL_CD: the report names the field but no clean
# code rule, so ALL sales with SALE_PRC1 > 0 are taken (documented
# in the v48 build log). Vintage is annual-ish (tax roll), so this
# frame is the trust backbone, not the freshest feed. Host
# reachability from any given deployment is NOT assumed — a failure
# is just a failed frame, like every other source.
FL_CADASTRAL_URL = ("https://services9.arcgis.com/Gh9awoU677aKree0/ArcGIS/rest/services/"
                    "Florida_Statewide_Cadastral/FeatureServer/0/query")
CADASTRAL_SRC = "FL Official Records (Cadastral)"
# v50: a Cadastral sale below this is a NOMINAL transfer (quitclaim /
# intra-family / corrective deed — the county records them at $100 and
# the like), not a market comp. Such rows still store at insert, but
# excluded (see _merge_sold_frame); the threshold guards the Cadastral
# frame ONLY.
_CAD_NOMINAL_MAX = 10000
_CAD_FIELDS = ("PARCEL_ID,PHY_ADDR1,PHY_CITY,PHY_ZIPCD,SALE_PRC1,SALE_YR1,SALE_MO1,"
               "SALE_PRC2,SALE_YR2,SALE_MO2,TOT_LVG_AR,ACT_YR_BLT,EFF_YR_BLT,LND_SQFOOT")

def _cad_num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None

def _cad_centroid(feature):
    """(lat, lon) for one Cadastral feature: the service's returned
    centroid when present (returnCentroid), else the average of the
    polygon's exterior-ring vertices — a parcel is small, so the
    ring average is well inside the distance gate's tolerance; the
    shared gate remains the arbiter either way."""
    c = feature.get("centroid")
    if isinstance(c, dict) and _cad_num(c.get("x")) is not None and _cad_num(c.get("y")) is not None:
        return float(c["y"]), float(c["x"])
    geom = feature.get("geometry") or {}
    rings = geom.get("rings") if isinstance(geom, dict) else None
    if rings and isinstance(rings[0], list) and rings[0]:
        xs = [p[0] for p in rings[0] if isinstance(p, (list, tuple)) and len(p) >= 2
              and _cad_num(p[0]) is not None]
        ys = [p[1] for p in rings[0] if isinstance(p, (list, tuple)) and len(p) >= 2
              and _cad_num(p[1]) is not None]
        if xs and ys: return sum(ys) / len(ys), sum(xs) / len(xs)
    coords = geom.get("coordinates") if isinstance(geom, dict) else None
    if coords and isinstance(coords[0], list) and coords[0]:
        ring = coords[0]
        xs = [p[0] for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
        ys = [p[1] for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
        if xs and ys: return sum(ys) / len(ys), sum(xs) / len(xs)
    return None, None

def _cad_feature_rows(feature):
    """One Cadastral feature -> the standard rows (one per recorded
    sale): the most recent sale (PRC1) whenever it has a price and a
    year, plus the prior sale (PRC2) ONLY when it is clearly a
    distinct OLDER sale — a different price in an earlier year-month;
    a PRC2 echoing PRC1 (same price, same month — a roll artifact)
    yields no second row. Beds/baths are ALWAYS None (the layer has
    no such fields) and the date is month-precision "YYYY-MM"."""
    attrs = (feature or {}).get("attributes") or {}
    if not isinstance(attrs, dict): return []
    lat, lon = _cad_centroid(feature)
    street = _s(attrs.get("PHY_ADDR1")).strip()
    zipc = attrs.get("PHY_ZIPCD")
    zipc = str(int(zipc)) if _cad_num(zipc) else _s(zipc)
    yb = _cad_num(attrs.get("ACT_YR_BLT")) or _cad_num(attrs.get("EFF_YR_BLT"))
    base = dict(street=street, city=_s(attrs.get("PHY_CITY")).strip(),
                zip_code=zipc, list_price=None,
                sqft=_cad_num(attrs.get("TOT_LVG_AR")),
                beds=None, full_baths=None,
                year_built=int(yb) if yb else None, lot_sqft=_cad_num(attrs.get("LND_SQFOOT")),
                days_on_mls=None, latitude=lat, longitude=lon,
                style=None, property_type=None, property_url="",
                primary_photo=None, alt_photos=[], estimated_value=None,
                price_per_sqft=None)
    rows = []
    def _sale(prc, yr, mo):
        prc, yr = _cad_num(prc), _cad_num(yr)
        if not prc or prc <= 0 or not yr: return None
        mo = _cad_num(mo)
        # A missing/invalid month stores the YEAR only ("2024") — the
        # display shows it as-is; defaulting to January would fabricate
        # a month the record doesn't carry.
        date = f"{int(yr):04d}-{int(mo):02d}" if mo and 1 <= mo <= 12 else f"{int(yr):04d}"
        return dict(base, sold_price=prc, last_sold_date=date)
    r1 = _sale(attrs.get("SALE_PRC1"), attrs.get("SALE_YR1"), attrs.get("SALE_MO1"))
    if r1 is None: return []
    rows.append(r1)
    r2 = _sale(attrs.get("SALE_PRC2"), attrs.get("SALE_YR2"), attrs.get("SALE_MO2"))
    if r2 is not None and (r2["last_sold_date"], r2["sold_price"]) != (r1["last_sold_date"], r1["sold_price"]) \
            and r2["last_sold_date"] < r1["last_sold_date"]:
        rows.append(r2)
    return rows

def _fl_cadastral_frame(prop, radius_mi=1.0):
    """The Cadastral sold frame for one pull: (DataFrame, err) —
    (None, None) for a non-FL subject (no request is made), (None,
    err) when the service fails. A spatial envelope query at the pull
    radius around the subject; rows flow through the SAME shared gate
    + merge as every sold source.

    v49 query repair (live-validated from the sandbox against the
    real layer, 2026-11-05 — see the v49 build log): the layer's
    native SR is wkid 102967, so the envelope degrees must carry
    inSR=4326 AND outSR=4326 or the server reads them in its own
    units; returnCentroid=true with returnGeometry=false (the layer
    answers with centroids — shipping 315 parcel polygons was the
    slow payload; the centroid-only form answered in ~20-29s vs
    ~36-51s with geometry, and an outSR projection of full polygons
    gateway-timed-out at 60s); orderByFields newest-sale-first (the
    layer publishes supportsOrderBy) so a capped answer keeps the
    freshest sales; resultRecordCount at the layer's maxRecordCount
    (2000). THIS frame reads with its own 90s timeout — the shared
    30s read timeout killed every live v48 pull (the service answers
    this query in ~20-55s). An ArcGIS 200-with-error-body (the live
    PA failure mode) is detected below and fails the frame — it is
    never parsed as zero results."""
    if (_s(prop["state"]) or "").strip().upper() != "FL": return None, None
    if prop["lat"] is None or prop["lon"] is None: return None, None
    dlat = (radius_mi or 1.0) / 69.0
    dlon = (radius_mi or 1.0) / (69.0 * max(math.cos(math.radians(prop["lat"])), 0.2))
    env = (f"{prop['lon'] - dlon},{prop['lat'] - dlat},"
           f"{prop['lon'] + dlon},{prop['lat'] + dlat}")
    data, _cad_err = None, None
    for _attempt in range(3):
        try:
            r = requests.get(FL_CADASTRAL_URL, params={
                "where": "SALE_PRC1>0", "geometry": env,
                "geometryType": "esriGeometryEnvelope", "inSR": 4326, "outSR": 4326,
                "spatialRel": "esriSpatialRelIntersects", "outFields": _CAD_FIELDS,
                "returnGeometry": "false", "returnCentroid": "true",
                "orderByFields": "SALE_YR1 DESC,SALE_MO1 DESC",
                "resultRecordCount": 2000, "f": "json"}, timeout=90)
            r.raise_for_status()
            data = r.json()
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            # v58: frame-level retry x2 (short backoff) on a proxy/
            # transport failure — the 2026-11-06 pull saw ProxyErrors
            # on the Cadastral frames alongside the quotas.
            _cad_err = e
            if _attempt < 2:
                time.sleep(1 * (_attempt + 1))
                continue
            return None, e
        except Exception as e:
            return None, e
    if data is None:
        return None, _cad_err
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        ebody = data["error"]
        return None, Exception(f"Cadastral service error {ebody.get('code')}: "
                               f"{ebody.get('message')}")
    feats = data.get("features") if isinstance(data, dict) else None
    rows = _rapi_rows_multi(feats if isinstance(feats, list) else [], _cad_feature_rows)
    return _hh_frame(rows), None

# ---------------------------------------------------------------- v58 pull-time budget
_PULL_HH_CAP_S = 20      # one HomeHarvest call may never hold the pull longer than this
_PULL_HH_BUDGET_S = 60   # after this much pull time, HomeHarvest is skipped outright

def _run_capped(fn, cap_s):
    """Run fn() in a daemon thread; return its result, or None when it
    exceeds cap_s seconds. A hung call is ABANDONED (the daemon thread
    dies with the process) — the pull never waits on it. An exception
    inside fn re-raises to the caller."""
    box = {}
    def _target():
        try: box["v"] = fn()
        except Exception as e: box["e"] = e
    t = threading.Thread(target=_target, daemon=True)
    t.start(); t.join(timeout=cap_s)
    if t.is_alive(): return None
    if "e" in box: raise box["e"]
    return box.get("v")

def _hh_over_budget(started_at, budget_s=_PULL_HH_BUDGET_S):
    """True once budget_s seconds of pull time have passed — further
    HomeHarvest calls are skipped (the pull's other frames still run)."""
    return (time.time() - started_at) > budget_s

def pull_comps(prop, radius_mi=1.0, past_days=365):
    """Multi-frame union pull (v3.4). Frames: a SUBJECT-CENTERED radius frame
    (the scraper's comps mode — address + radius) at a 3-year horizon, unioned
    with the ZIP-area frame at the requested window; distance from the
    subject's own geocode governs inclusion (no ZIP-equality gate). The
    centered frame is what finds same-street sales a capped whole-ZIP frame
    crowds out — probed 2026-11-04: 4248 Lindley St's street has 5 sales in
    3 years (incl. all 3 of Sherif's fixture sales); the old 365-day ZIP pull
    surfaced 1. The distance gate is also the mis-resolution safety net: the
    old raw-address lookup mode mis-resolved cities, but any wrong-area row
    lands miles away and is discarded here.
    Also pulls rental comps, the subject's own listing photos, fills blank
    subject facts from its EXACT listing row only, blank-fills county parcel
    facts (APN / assessor value / last sale) where a probed GIS endpoint
    covers the county, and auto-pins the top 3 sold comps (ReiSearch
    default) when the user has not pinned any yet. Returns counts per kind.
    Raises on total sold-frame failure so the job layer can surface the
    reason — partial failures are reported in 'warnings'."""
    from homeharvest import scrape_property
    d = db(); today = datetime.date.today().isoformat()
    warnings = []
    addr_line = prop["address"]
    _pull_started = time.time()  # v58: the pull-wide HomeHarvest budget clock
    def _scrape(**kw):
        # v58: HomeHarvest never answers from PA (proxy block) and its
        # calls have no timeout of their own — each call is capped at
        # 20s in a thread, and once 60s of pull time has passed the
        # calls are skipped outright. A capped/skipped call returns
        # (None, err) like any other failed frame.
        if _hh_over_budget(_pull_started):
            return None, Exception("HomeHarvest skipped — pull time budget reached")
        try:
            res = _run_capped(lambda: scrape_property(**kw), _PULL_HH_CAP_S)
            if res is None:
                return None, Exception("HomeHarvest call timed out (20s cap)")
            return res, None
        except Exception as e:
            return None, e
    subj = {"sqft": prop["sqft"], "beds": prop["beds"], "baths": prop["baths"], "year_built": prop["year_built"], "lot_sqft": prop["lot_sqft"]}
    # --- RapidAPI sold frames (v3.8; union v42): when a key is
    # configured the pool runs FIRST — on egress-whitelisted hosts it
    # is the only frame source that can answer — and the HomeHarvest
    # frames below still union in (dedupe is address+price). The pool
    # itself is a sequential union with top-up (see
    # _rapidapi_sold_frame): one frame per source that served. No key:
    # this block never runs and the pull is byte-identical to the
    # pre-RapidAPI behavior. ---
    rapi_frames, rapi_outcomes = [], []
    if _rapidapi_key():
        try:
            rapi_frames, _rapi_err, rapi_outcomes = _rapidapi_sold_frame(prop, past_days, radius_mi)
        except Exception as _rapi_boom:
            # Last-resort guard (v40): the RapidAPI layer must never
            # abort a pull the HomeHarvest frames could still serve.
            rapi_frames, _rapi_err, rapi_outcomes = [], _rapi_boom, []
            app.logger.warning(f"comp pull: RapidAPI sold frame raised for {addr_line}: {_rapi_boom!r}")
        if not rapi_frames and _rapi_err is not None:
            app.logger.warning(f"comp pull: RapidAPI sold frame failed for {addr_line}: {_rapi_err!r}")
        if rapi_outcomes:
            # v42: the pool detail is logged on EVERY pull that used
            # the layer — each CALLED source's outcome (served counts
            # or failure + body snippet). v40/v41 logged it only on
            # total exhaustion, so a serving pull left no trace of
            # what the sources did (and a failing one hid the earlier
            # sources behind the last error — the v39 PA incident).
            app.logger.warning(f"comp pull: RapidAPI sold pool detail for {addr_line}: "
                               + "; ".join(o["log"] for o in rapi_outcomes))
    # --- FL Statewide Cadastral sold frame (v48): official records,
    # FL subjects only, keyless. Merged FIRST — it is the trust floor:
    # richer listing frames (RapidAPI, then HomeHarvest) update its
    # rows in place when they record the same sale (same address +
    # price + month-matching date), so a sale both sources know ends
    # stored once, in its richest form. ---
    cad_frame = None
    if (_s(prop["state"]) or "").strip().upper() == "FL":
        try:
            cad_frame, cad_err = _fl_cadastral_frame(prop, radius_mi)
        except Exception as _cad_boom:
            cad_frame, cad_err = None, _cad_boom
        if cad_err is not None:
            app.logger.warning(f"comp pull: FL Cadastral frame failed for {addr_line}: {cad_err!r}")
    # --- jina-Zillow sold top-up (v58): when the union above is thin
    # (fewer than RAPIDAPI_UNION_RICH in-gate rows), the reader's ZIP
    # sold page tops it up — no quota, no key. The frame merges through
    # the same shared gate as every sold source. ---
    rapi_in_gate = sum(o["in_gate"] for o in rapi_outcomes)
    cad_in_gate = _frame_in_gate(cad_frame, prop, radius_mi)
    jina_frame, jina_label, jina_err = [], "Zillow (reader)", None
    if rapi_in_gate + cad_in_gate < RAPIDAPI_UNION_RICH:
        try:
            jina_frame, jina_label, jina_err = _jina_zillow_sold_frame(prop, radius_mi, past_days)
        except Exception as _jina_boom:
            jina_frame, jina_err = [], _jina_boom
        if jina_err is not None:
            app.logger.warning(f"comp pull: jina-Zillow sold frame failed for {addr_line}: {jina_err!r}")
        elif jina_frame is not None and len(jina_frame):
            app.logger.warning(f"comp pull: jina-Zillow sold frame for {addr_line}: "
                               f"served {len(jina_frame)} rows")
    # --- sold frames: centered (3-yr horizon) UNION ZIP area (requested window) ---
    sold_frames = []
    frame_sources = {}
    rapi_ids = set()
    if cad_frame is not None:
        sold_frames.append(cad_frame); frame_sources[id(cad_frame)] = CADASTRAL_SRC
    for _rapi_df, _rapi_lbl in rapi_frames:
        sold_frames.append(_rapi_df); frame_sources[id(_rapi_df)] = _rapi_lbl
        rapi_ids.add(id(_rapi_df))
    if jina_frame is not None and len(jina_frame):
        sold_frames.append(jina_frame); frame_sources[id(jina_frame)] = jina_label
    centered, err_c = _scrape(location=addr_line, listing_type="sold",
                              past_days=max(past_days, 1095), radius=radius_mi)
    if centered is not None: sold_frames.append(centered)
    area, err_a = _scrape(location=prop["zip"], listing_type="sold", past_days=past_days)
    if area is not None: sold_frames.append(area)
    if not sold_frames:
        # v3.6: total sold-frame failure fails HUMAN. Raw exception text
        # (URLs, proxy internals) goes to the server log only — never the
        # UI. Nothing has been merged yet at this point, so a failed pull
        # stores zero rows.
        app.logger.warning(f"comp pull: all sold frames failed for {addr_line}: "
                           f"centered={err_c!r} area={err_a!r}")
        if _is_unreachable_err(err_c) or _is_unreachable_err(err_a):
            raise SourceUnreachableError(SOURCE_UNREACHABLE_MSG)
        raise RuntimeError("the listing source returned an error before any comps "
                           "came back — nothing was added or changed; please try again")
    if err_c is not None:
        app.logger.warning(f"comp pull: centered sold frame failed for {addr_line}: {err_c!r}")
        if not rapi_frames and cad_frame is None:
            warnings.append("centered sold frame failed — results came from the ZIP-area frame")
    if err_a is not None:
        app.logger.warning(f"comp pull: ZIP sold frame failed for {addr_line}: {err_a!r}")
        if not rapi_frames and cad_frame is None:
            warnings.append("ZIP-area sold frame failed — results came from the address-centered frame")
    added = updated = n_sold = 0
    sold_touched = []
    # v43: drop accounting for the RapidAPI frames ONLY — one collector
    # across all their merges, logged below next to the pool detail,
    # so a served-but-not-stored pull names the exact filter (with
    # counts and example addresses) instead of a bare zero.
    rapi_drops = {}
    rapi_served = rapi_added = rapi_updated = 0
    for fr in sold_frames:
        _is_rapi = id(fr) in rapi_ids
        a1, u1, n1 = _merge_sold_frame(d, prop, fr, radius_mi, subj,
                                       source_override=frame_sources.get(id(fr)),
                                       touched=sold_touched,
                                       drops=rapi_drops if _is_rapi else None)
        added += a1; updated += u1; n_sold += n1
        if _is_rapi:
            rapi_served += len(fr); rapi_added += a1; rapi_updated += u1
    if rapi_frames:
        # outside_window cannot appear here: the horizon lives in the
        # adapters (their served counts are already post-horizon) and
        # the merge deliberately stores deeper than the view window.
        _parts = []
        for _rsn in ("no_coords", "no_street", "no_price", "outside_radius",
                     "outside_window", "other"):
            _slot = rapi_drops.get(_rsn)
            if _slot and _slot[0]:
                _ex = "; ".join(_slot[1])
                _parts.append(f"{_rsn}={_slot[0]}" + (f" ({_ex})" if _ex else ""))
        _dup = rapi_drops.get("duplicate")
        app.logger.warning(
            f"comp pull: RapidAPI sold merge detail for {addr_line}: "
            f"served {rapi_served} rows -> {rapi_added} stored new"
            + (f" + {rapi_updated} duplicate updates" if rapi_updated else "")
            + (f" ({'; '.join(_dup[1])})" if _dup and _dup[1] else "")
            + f"; drops: {', '.join(_parts) if _parts else 'none'}")
    # v48: the Cadastral include-line, same truthful-attribution rule
    # as the RapidAPI banner below — named only when a stored row's
    # final touch is the Cadastral merge.
    _last_lbl = {}
    for _cid, _clbl in sold_touched: _last_lbl[_cid] = _clbl
    if CADASTRAL_SRC in _last_lbl.values():
        warnings.append("sold comps include results from " + CADASTRAL_SRC)
    if rapi_outcomes:
        # v42 TRUTHFUL BANNER. The v41 line named the serving source
        # even when the merge stored ZERO of its rows (the live pull's
        # banner claimed ReefAPI results next to a 0-comp count).
        # Attribution is exact: sold_touched records (comp_id, label)
        # for every row this pull's merges stored, in order; a row's
        # final touch is the label it is stored under. The include-line
        # names ONLY pool sources with >= 1 stored row; when the pool
        # answered but stored nothing, the banner says so plainly. The
        # failover note keeps the established wording for sources that
        # were passed over / failed while others served.
        _last_label = {}
        for _cid, _clbl in sold_touched: _last_label[_cid] = _clbl
        _stored_by = {}
        for _clbl in _last_label.values():
            _stored_by[_clbl] = _stored_by.get(_clbl, 0) + 1
        _stored_labels = [lbl for _f, lbl in rapi_frames if _stored_by.get(lbl)]
        _served_labels = [o["label"] for o in rapi_outcomes if o["served"]]
        _failed = [o for o in rapi_outcomes if not o["served"]]
        if _failed and _served_labels:
            warnings.append("RapidAPI failover: "
                            + "; ".join(f"{o['name']} — {o['reason']}" for o in _failed)
                            + "; sold comps served by " + ", ".join(_served_labels))
        if _stored_labels:
            warnings.append("sold comps include results from " + ", ".join(_stored_labels))
        elif _served_labels:
            _msg = (f"RapidAPI answered but no results within {radius_mi:g} mi / "
                    "the date window could be stored")
            # v43: when one merge filter ate most of the served rows,
            # the banner says which — in plain words, from the drop
            # accounting above (the v42 live pull's zero carried no
            # hint that every in-range row lacked a sold price).
            _drop_counts = {k: v[0] for k, v in rapi_drops.items() if k != "duplicate"}
            _drop_total = sum(_drop_counts.values())
            if _drop_total:
                _dom_rsn, _dom_n = max(_drop_counts.items(), key=lambda t: t[1])
                if _dom_n * 2 >= _drop_total:
                    _phrase = {"no_price": "most of the rows it returned had no sold price recorded",
                               "no_street": "most of the rows it returned had no street address",
                               "no_coords": "most of the rows it returned had no coordinates"}.get(_dom_rsn)
                    if _phrase: _msg += " — " + _phrase
            warnings.append(_msg)
    # --- rentals: centered UNION ZIP (LTR / Co-Living / Affordable views read this set) ---
    n_rent = 0; rent_frames = []; rent_sources = {}
    if _rapidapi_key():
        try:
            _rdf, _rerr, _rsrc, _rout = _rapidapi_rent_frame(prop, radius_mi)
        except Exception as _rboom:
            # Same v40 last-resort guard as the sold frame.
            _rdf, _rerr, _rsrc, _rout = None, _rboom, None, []
            app.logger.warning(f"rent pull: RapidAPI rent frame raised for {addr_line}: {_rboom!r}")
        if _rdf is not None:
            rent_frames.append(_rdf); rent_sources[id(_rdf)] = _rsrc
        elif _rerr is not None:
            app.logger.warning(f"rent pull: RapidAPI rent frame failed for {addr_line}: {_rerr!r}")
        if _rout:
            # v42: rent pool detail on EVERY pull (see the sold side).
            app.logger.warning(f"rent pull: RapidAPI rent pool detail for {addr_line}: "
                               + "; ".join(o["log"] for o in _rout))
    rc, _ = _scrape(location=addr_line, listing_type="for_rent", past_days=90, radius=radius_mi)
    if rc is not None: rent_frames.append(rc)
    ra, err_r = _scrape(location=prop["zip"], listing_type="for_rent", past_days=90)
    if ra is not None: rent_frames.append(ra)
    if not rent_frames:
        warnings.append("rental comps couldn't be pulled this time")
        app.logger.warning(f"rent pull failed: {err_r}")
    # v45: drop accounting for the RapidAPI rent frame, mirroring the
    # sold side (v43) — one collector across its merge, logged below,
    # so a served-but-not-stored rent frame names the exact filter.
    rent_rapi_drops = {}
    rent_rapi_served = rent_rapi_added = rent_rapi_updated = 0
    rent_rapi_merged = False
    for fr in rent_frames:
        _is_rapi_r = id(fr) in rent_sources
        a2, u2, n2 = _merge_rent_frame(d, prop, fr, radius_mi, subj,
                                       source_override=rent_sources.get(id(fr)),
                                       drops=rent_rapi_drops if _is_rapi_r else None)
        added += a2; n_rent += n2
        if _is_rapi_r:
            rent_rapi_merged = True
            rent_rapi_served += len(fr); rent_rapi_added += a2; rent_rapi_updated += u2
    if rent_rapi_merged:
        _rparts = []
        for _rsn in ("no_coords", "no_street", "no_price", "outside_radius",
                     "outside_window", "other"):
            _slot = rent_rapi_drops.get(_rsn)
            if _slot and _slot[0]:
                _ex = "; ".join(_slot[1])
                _rparts.append(f"{_rsn}={_slot[0]}" + (f" ({_ex})" if _ex else ""))
        _rdup = rent_rapi_drops.get("duplicate")
        app.logger.warning(
            f"rent pull: RapidAPI rent merge detail for {addr_line}: "
            f"served {rent_rapi_served} rows -> {rent_rapi_added} stored new"
            + (f" + {rent_rapi_updated} duplicate updates" if rent_rapi_updated else "")
            + (f" ({'; '.join(_rdup[1])})" if _rdup and _rdup[1] else "")
            + f"; drops: {', '.join(_rparts) if _rparts else 'none'}")
    d.commit()
    # --- subject's own listing: photos + blank-fact fill (never overwrite user data) ---
    # v3.4: EXACT row only. The old nearest-within-0.05mi fallback could fill a
    # subject with a NEIGHBOR's specs and photos (the live-record mutation
    # class); a missing row now honestly leaves the fields blank.
    photos_found = 0
    fs_frames = []
    fc, _ = _scrape(location=addr_line, listing_type="for_sale", past_days=365, radius=radius_mi)
    if fc is not None: fs_frames.append(fc)
    fz, err_f = _scrape(location=prop["zip"], listing_type="for_sale", past_days=365)
    if fz is not None: fs_frames.append(fz)
    if not fs_frames:
        app.logger.warning(f"comp pull: for-sale lookup failed for {addr_line}: {err_f!r}")
        warnings.append("for-sale lookup for subject photos failed")
    # v48: the subject row is the RICHEST exact match across all
    # frames, not the first — the FL Cadastral frame (merged first for
    # comps) also contains the subject's own parcel, but its row
    # carries no beds/baths/photos, and letting it win here silently
    # starved the subject fill whenever the official record answered
    # (gate_v20 SP1 caught exactly that). Photos weigh most, then
    # spec/price completeness; ties keep frame order.
    def _srow_richness(r):
        n = 10 if extract_photos(r) else 0
        return n + sum(1 for k in ("beds", "full_baths", "sqft", "year_built",
                                   "sold_price", "list_price", "lot_sqft")
                       if r.get(k) is not None)
    srow, _srow_best = None, -1
    for fr in fs_frames + sold_frames + rent_frames:
        cand = find_subject_row(fr, prop, exact_only=True)
        if cand is not None and _srow_richness(cand) > _srow_best:
            srow, _srow_best = cand, _srow_richness(cand)
    if srow is None:
        # v1.6 (item N): DEEP subject lookup — the subject's own last sale is often
        # older than the comp window, so the frames above miss it and photos/specs
        # never populate. One wider sold scrape (10 years) finds the subject's own
        # listing row: photos + blank specs come from it. v3.4: centered first.
        deep, err_d = _scrape(location=addr_line, listing_type="sold", past_days=3650,
                              radius=max(radius_mi, 1.0))
        if deep is not None:
            srow = find_subject_row(deep, prop, exact_only=True)
        if srow is None:
            deep2, err_d2 = _scrape(location=prop["zip"], listing_type="sold", past_days=3650)
            if deep2 is not None:
                srow = find_subject_row(deep2, prop, exact_only=True)
            elif err_d is not None and err_d2 is not None:
                app.logger.warning(f"comp pull: deep subject lookup failed for {addr_line}: {err_d2!r}")
                warnings.append("deep subject lookup failed")
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
                if v and _sane_subject_fill(col, v): fills[col] = int(v) if col == "year_built" else v
                elif key == "style" and _s(srow.get("style")): fills[col] = _s(srow.get("style"))
        if not prop["list_price"] and fnum(srow.get("list_price")) and _sane_subject_fill("list_price", fnum(srow.get("list_price"))):
            fills["list_price"] = fnum(srow.get("list_price"))
        if not prop["est_realtor"] and fnum(srow.get("estimated_value")) and _sane_subject_fill("est_realtor", fnum(srow.get("estimated_value"))):
            fills["est_realtor"] = fnum(srow.get("estimated_value")); fills["est_date"] = today
        if fills:
            d.execute(f"UPDATE properties SET {','.join(k+'=?' for k in fills)} WHERE id=?",
                      (*fills.values(), prop["id"])); d.commit()
    # --- private-zillow subject fill (v48): when a RapidAPI key is
    # configured and the subject STILL lacks specs or photos after the
    # HomeHarvest path (on egress-restricted hosts that path can never
    # answer), ONE Zillow by-address lookup fills the blanks — the
    # 250/mo hard cap is spent only on subjects that need it, never
    # on already-complete ones. Failure/no-match changes nothing: the
    # honest could-not-be-filled state above stands. ---
    if _rapidapi_key():
        def _still_lacks(row):
            return (not row["beds"] or not row["baths"] or not row["sqft"]
                    or not row["year_built"]
                    or (not row["photos_json"] and not row["photo_url"]))
        def _note_zfill(res, label):
            nonlocal photos_found
            d.commit()
            photos_found = max(photos_found, res["photos"])
            warnings[:] = [w for w in warnings if "could not be auto-filled" not in w]
            warnings.append(label)
        _prop_now = d.execute("SELECT * FROM properties WHERE id=?", (prop["id"],)).fetchone()
        if _still_lacks(_prop_now):
            # v58: Things4u detail FIRST — the Realtor.com record is
            # the freshest subject source that answers from this host;
            # private-zillow is the last resort (its provider is down).
            # v58.1: the detail id comes from the subject's own row in
            # the pull's frames (property_id column) — never from
            # auto-complete, whose ids are not propertyIds.
            try:
                _spid = None
                try:
                    _spid = srow.get("property_id") if srow is not None else None
                except Exception:
                    _spid = None
                _tf = _things4u_subject_fill(d, _prop_now, today, property_id=_spid)
            except Exception as _te:
                app.logger.warning(f"comp pull: Things4u subject detail raised for "
                                   f"{addr_line}: {_te!r}")
                _tf = None
            if _tf:
                _note_zfill(_tf, "subject specs filled from Realtor.com via RapidAPI (Things4u)")
            _prop_now = d.execute("SELECT * FROM properties WHERE id=?", (prop["id"],)).fetchone()
        if _still_lacks(_prop_now):
            try:
                _zf = _zillow_subject_fill(d, _prop_now, today)
            except Exception as _ze:
                app.logger.warning(f"comp pull: private-zillow subject fill raised for "
                                   f"{addr_line}: {_ze!r}")
                _zf = None
            if _zf:
                _note_zfill(_zf, "subject specs/photos filled from Zillow via RapidAPI")
            # v56 backups: the by-address path is ONE wrapper; when it
            # cannot answer from this host, the zpid-only ReefAPI
            # Zillow detail is the second route, and the Unofficial
            # images endpoint (trusted stored zpid only) the third.
            # Each step re-reads the subject and fires only while the
            # lack it can cure persists.
            _prop_now = d.execute("SELECT * FROM properties WHERE id=?", (prop["id"],)).fetchone()
            if _still_lacks(_prop_now):
                try:
                    _zf2 = _reef_zillow_subject_fill(d, _prop_now, today)
                except Exception as _ze2:
                    app.logger.warning(f"comp pull: ReefAPI Zillow subject backup raised for "
                                       f"{addr_line}: {_ze2!r}")
                    _zf2 = None
                if _zf2:
                    _note_zfill(_zf2, "subject specs/photos filled from Zillow via RapidAPI "
                                      "(ReefAPI backup)")
            _prop_now = d.execute("SELECT * FROM properties WHERE id=?", (prop["id"],)).fetchone()
            if (not _prop_now["photos_json"] and not _prop_now["photo_url"]
                    and _prop_now["zpid"]):
                try:
                    _zf3 = _unofficial_zillow_photos_fill(d, _prop_now, today)
                except Exception as _ze3:
                    app.logger.warning(f"comp pull: Unofficial-Zillow images fill raised for "
                                       f"{addr_line}: {_ze3!r}")
                    _zf3 = None
                if _zf3:
                    _note_zfill(_zf3, "subject photos filled from Zillow via RapidAPI "
                                      "(Unofficial-Zillow backup)")
    # --- county parcel record (v3.4): blank-fill APN / assessor value / last
    # sale from the county GIS where a probed endpoint covers the county ---
    try:
        _prop_now = d.execute("SELECT * FROM properties WHERE id=?", (prop["id"],)).fetchone()
        _note = county_parcel_fill(d, _prop_now)
        if _note: warnings.append(_note); d.commit()
    except Exception as e:
        app.logger.warning(f"comp pull: county parcel fill failed for {addr_line}: {e!r}")
        warnings.append("county parcel lookup failed")
    # --- official PadSplit benchmark pull (v56, Tier A): the area's
    # actual listed $/wk per room, stored on the property for
    # coliving_stats to prefer over the Tier-B derivation. Best-effort
    # like every frame here: any failure leaves the stored state and
    # Tier B exactly as they were (the reason is logged inside
    # _padsplit_pull / _padsplit_fetch). ---
    try:
        _ps = _padsplit_pull(prop)
    except Exception as _pse:
        app.logger.warning(f"comp pull: PadSplit pull raised for {addr_line}: {_pse!r}")
        _ps = None
    if _ps:
        d.execute("UPDATE properties SET padsplit_json=?, padsplit_date=? WHERE id=?",
                  (json.dumps(_ps), today, prop["id"])); d.commit()
        _ps_med = _median([float(x) for x in _ps["weekly_rents"]]) or 0
        _ps_scope = (f"ZIP {str(prop['zip'] or '')[:5]}" if _ps["scope"] == "zip"
                     else f"{prop['city']} city-wide (ZIP itself listed {_ps['zip_n']})")
        warnings.append(f"PadSplit (official listings): {_ps['n']} rooms listed in "
                        f"{_ps_scope} — median ${_ps_med:,.0f}/wk per room")
    # --- P0-1/P0-2 widening (Sherif 2026-11-02): the match-first views need a
    # real matched set. If the stored sold set holds <10 Tier-1 matches, run ONE
    # widened sold scrape (2x radius, min 2 mi; 730 days) and merge it; if the
    # rental set is thin (<11 stored or <5 exact/near matches), run ONE widened
    # rental scrape (2x radius, min 2 mi; past_days 365). Widening only ADDS
    # real listings — views never pad with mismatches. ---
    # v58: prop2 is a plain dict (not a sqlite Row) — the Things4u /
    # jina adapters read prop.get("city"), which a Row does not have,
    # and the widened frame silently died on it (the v58 gate caught
    # exactly that: widening reported "scrape failed" with S639
    # unstored).
    prop2 = dict(d.execute("SELECT * FROM properties WHERE id=?", (prop["id"],)).fetchone())
    subj2 = {"sqft": prop2["sqft"], "beds": prop2["beds"], "baths": prop2["baths"],
             "year_built": prop2["year_built"], "lot_sqft": prop2["lot_sqft"]}
    def _tier1_count(win_days=None):
        rows = d.execute("SELECT beds,baths,sqft,event_date FROM comps WHERE property_id=? AND kind='sold' AND excluded=0",
                         (prop["id"],)).fetchall()
        if win_days:
            cutoff = (datetime.date.today() - datetime.timedelta(days=win_days)).isoformat()
            rows = [r for r in rows if not r["event_date"] or r["event_date"] >= cutoff]
        return sum(1 for r in rows if comp_tier(subj2, dict(r)) == 1)
    t1 = _tier1_count()
    # v3.4: the widening must also serve the VIEW — the sold view shows a
    # date window, so Tier-1 depth is measured inside the requested window
    # too (a 3-year stored set can hold 31 Tier-1 all-time yet only 8 inside
    # the 365-day view, and the top cards are what Sherif judges).
    t1w = _tier1_count(past_days)
    if t1 < 10 or t1w < 10:
        wide_r = max(radius_mi * 2, 2.0)
        got = False
        for kw in (dict(location=prop2["address"], listing_type="sold", past_days=730, radius=wide_r),
                   dict(location=prop["zip"], listing_type="sold", past_days=730)):
            wide, _e = _scrape(**kw)
            if wide is None: continue
            a3, u3, _ = _merge_sold_frame(d, prop2, wide, wide_r, subj2)
            added += a3; updated += u3; got = True
        # v56: on egress-restricted hosts the HomeHarvest widening above
        # can never answer (PA blocks realtor.com), which left the
        # widening a no-op exactly where it was needed. The widening
        # now ALSO re-runs the two frames that CAN answer there — the
        # FL Cadastral frame (self-guards to FL subjects) and the
        # RapidAPI sold union — at the widened radius / 730 days,
        # merged through the same shared gate as every sold frame.
        try:
            cad_wide, _cadw_err = _fl_cadastral_frame(prop2, wide_r)
        except Exception as _cadw_boom:
            cad_wide = None
            app.logger.warning(f"comp pull: widened FL Cadastral frame raised for {addr_line}: {_cadw_boom!r}")
        if cad_wide is not None:
            a3, u3, _ = _merge_sold_frame(d, prop2, cad_wide, wide_r, subj2,
                                          source_override=CADASTRAL_SRC)
            added += a3; updated += u3; got = True
        if _rapidapi_key():
            try:
                rapi_wide, _rwide_err, _rwide_oc = _rapidapi_sold_frame(prop2, 730, wide_r)
            except Exception as _rwide_boom:
                rapi_wide = []
                app.logger.warning(f"comp pull: widened RapidAPI sold frame raised for {addr_line}: {_rwide_boom!r}")
            for _wdf, _wlbl in rapi_wide:
                a3, u3, _ = _merge_sold_frame(d, prop2, _wdf, wide_r, subj2,
                                              source_override=_wlbl)
                added += a3; updated += u3; got = True
        if got:
            d.commit()
            t1b = _tier1_count(); t1wb = _tier1_count(past_days)
            warnings.append(f"sold set widened to {wide_r:g} mi / 730 days to find Tier-1 matches ({t1} -> {t1b} found; {t1w} -> {t1wb} within {past_days} days)")
        else:
            warnings.append("sold widening scrape failed")
    def _rent_match_counts():
        rows = d.execute("SELECT beds,baths FROM comps WHERE property_id=? AND kind='rent'",
                         (prop["id"],)).fetchall()
        ex_ = sum(1 for r in rows if rent_match_group(subj2, dict(r)) == 0)
        near = sum(1 for r in rows if rent_match_group(subj2, dict(r)) == 1)
        return len(rows), ex_, near
    rn, rex, rnear = _rent_match_counts()
    if rn < 11 or (rex + rnear) < 5:
        wide_r = max(radius_mi * 2, 2.0)
        got = False
        for kw in (dict(location=prop2["address"], listing_type="for_rent", past_days=365, radius=wide_r),
                   dict(location=prop["zip"], listing_type="for_rent", past_days=365)):
            wide_rf, _e = _scrape(**kw)
            if wide_rf is None: continue
            a4, _, _ = _merge_rent_frame(d, prop2, wide_rf, wide_r, subj2)
            added += a4; got = True
        if got:
            d.commit()
            rn2, rex2, rnear2 = _rent_match_counts()
            warnings.append(f"rental set widened to {wide_r:g} mi / 365 days for matched rentals ({rn} -> {rn2} listings; {rex2} exact + {rnear2} near beds/baths matches)")
        else:
            warnings.append("rental widening scrape failed")
    # --- Auto-pin (only if user pinned none): top 3 by TIER first (P0-1: Tier 1
    # only when >=3 exist), then match score, then distance. ---
    pinned_now = d.execute("SELECT COUNT(*) c FROM comps WHERE property_id=? AND kind='sold' AND pinned=1",
                           (prop["id"],)).fetchone()["c"]
    if not pinned_now:
        rows = [dict(r) for r in d.execute(
            "SELECT * FROM comps WHERE property_id=? AND kind='sold' AND excluded=0", (prop["id"],)).fetchall()]
        # v57: a bulk-sale row never auto-pins — the ARV is built from
        # the pinned set, and a $31M complex total must never be one
        # of the three comps the ARV is built from.
        rows = [c for c in rows if not _cval(c, "bulk_sale")]
        # v57.1: a legacy bulk-group member (stored before v57, so
        # unflagged) never auto-pins either — read-side detection over
        # the stored pool, same rule as the stats path.
        _bk = _bulk_group_keys(rows)
        rows = [c for c in rows if _bulk_group_key(c) not in _bk]
        # v56: same-street first here too — the auto-pinned set is what
        # the ARV is built from, so a same-street comp (even a Tier-3
        # county row with no beds/baths) pins ahead of off-street
        # Tier-1 matches; tier/score/distance order inside the bucket.
        rows.sort(key=lambda c: (_same_street(prop2, c["address"]), comp_tier(subj2, c),
                                 -(c["match_score"] or 0),
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
    Keyless-first (v3.6): the key is added only when one is configured
    (CENSUS_API_KEY env or the census_key setting). Where the network
    demands a key, api.census.gov answers with an HTML 'Missing Key' page
    instead of JSON — that fails parsing here and returns None, which
    callers treat as 'source unavailable', never as data."""
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

# v3.6: the direct api.census.gov fallback serves the SAME vintage Census
# Reporter is pinned to (CR_RELEASE = acs2024_5yr), so a block built from
# either source is like-for-like under the "ACS 2024 5-year" label. (The
# ACS_YEAR constant still governs the _pop_trend year range and the
# _resolve_place place-list query — separate surfaces, unchanged.)
DIRECT_ACS_YEAR = 2024

def _geo_block(geo_params, year=DIRECT_ACS_YEAR):
    """Direct api.census.gov path — the fallback when Census Reporter is
    unreachable (v3.6). Keyless-first; uses CENSUS_API_KEY when configured."""
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

def _cr_fetch(geo_id, release=CR_RELEASE, tables=CR_TABLES, timeout=20):
    # v3.3: default timeout 60 -> 20. Census Reporter's healthy latency is
    # 0.5-2s, but a degraded Reporter (incl. slow negative answers for
    # geographies it doesn't have) could hold a demographics pull for the
    # full 60s per call, stacking past every consumer's wait window.
    # 20s is ~10x healthy latency; callers may pass a shorter one for
    # non-essential archive-release enrichment (see _pop_trend_cr).
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
                     params={"table_ids": tables, "geo_ids": geo_id}, timeout=timeout,
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
    direct api.census.gov whenever Reporter does not answer (v3.6 — on
    egress-restricted hosts like PythonAnywhere free, censusreporter.org
    is blocked but api.census.gov is reachable, so this fallback is what
    serves demographics there; keyless-first, key used when configured).
    Returns (block, source)."""
    try:
        vals, rel = _cr_fetch(geo_id)
    except Exception:
        vals, rel = {}, None
    if vals:
        blk = _block_from_vals(vals, name)
        if blk: return blk, f"US Census {rel} via Census Reporter"
    if direct_params:
        blk = _geo_block(direct_params)
        if blk: return blk, "US Census ACS 5-year (api.census.gov)"
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
            # v3.3: archive releases are non-essential enrichment and Census
            # Reporter's archive endpoints can take ~60s each when degraded;
            # a short timeout keeps one slow archive from holding the whole
            # demographics pull hostage (a miss just drops that trend point,
            # which this loop already tolerates). Healthy path unchanged.
            vals, _r = _cr_fetch(geo_id, release=release, tables="B01003", timeout=12)
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
    FALLBACK (v3.6): direct api.census.gov (same ACS 2024 5-year vintage)
    whenever Reporter does not answer — the only demographics route on
    egress-restricted hosts where censusreporter.org is blocked but
    api.census.gov is reachable. Keyless-first; CENSUS_API_KEY when set."""
    out = {"zip": None, "city": None, "source": None,
           "date": datetime.date.today().isoformat()}
    zip_geo = f"86000US{zip_code}"
    blk, src = _geo_block_any(zip_geo, f"ZCTA5 {zip_code}", f"zip code tabulation area:{zip_code}")
    if blk: blk["geoid"] = zip_geo; blk["scope_label"] = f"ZIP {zip_code} (ZCTA)"
    out["zip"] = blk
    if src: out["source"] = src
    if blk:
        blk["pop_trend"] = _pop_trend_cr(zip_geo)
        if len(blk["pop_trend"]) < 2:
            # Census Reporter's public API serves named 5-year releases
            # (acs2014_5yr / acs2019_5yr / acs2024_5yr verified) but NOT
            # acs2023_5yr — a thin/absent Reporter trend falls back to the
            # direct API (v3.6: no longer key-gated; keyless-first). The
            # fallback only REPLACES the Reporter trend when it returns
            # more points: on a keyless network where Census refuses the
            # direct calls, Reporter's single current-release point must
            # survive rather than be emptied by a zero-point fallback.
            direct_trend = _pop_trend(zip_code)
            if len(direct_trend) > len(blk["pop_trend"]):
                blk["pop_trend"] = direct_trend
        prev_vals = {}
        try:
            # v3.3: same archive-timeout rationale as _pop_trend_cr — the
            # prior-release comparison is enrichment; the ZIP block (current
            # release) is the required data and keeps the full timeout.
            prev_vals, _r = _cr_fetch(zip_geo, release="acs2019_5yr", tables="B25077,B25064", timeout=12)
        except Exception:
            prev_vals = {}
        if not prev_vals:
            # v3.6: direct-API prior-release comparison, keyless-first.
            # Year is DIRECT_ACS_YEAR - 5 = 2019 — the same vintage as the
            # acs2019_5yr Reporter archive it stands in for (the old keyed
            # path used ACS_YEAR - 5 = 2018, a different comparison basis).
            prev_vals = _acs_fetch(DIRECT_ACS_YEAR - 5, "B25077_001E,B25064_001E",
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
        # v3.6: never credit a source that did not serve. When neither
        # block loaded, say so plainly (the job layer fails the pull
        # honestly in that case and keeps any cached data untouched).
        out["source"] = ("US Census ACS 5-year — unavailable (neither Census Reporter "
                         "nor api.census.gov answered from this server)")
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
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")  # v58: same transient-lock guard as db()
    return c

def start_job(pid, kind, fn):
    """Record a job row and run fn in a background thread. fn returns a success message string.
    Any exception becomes a visible, plain-English job error on the property — never a bare 500.
    v58: one retry after 2s when fn dies with sqlite3.OperationalError (the pull is
    idempotent — merges are upserts — so a re-run after a transient disk/lock error is safe)."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c = _job_conn()
    cur = c.execute("INSERT INTO jobs(property_id,kind,status,started_at) VALUES(?,?,'running',?)", (pid, kind, now))
    jid = cur.lastrowid; c.commit(); c.close()
    def _run():
        try:
            with app.app_context():
                try:
                    msg = fn()
                except sqlite3.OperationalError:
                    time.sleep(2)
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
    if isinstance(e, SourceUnreachableError):
        return str(e)  # v3.6: the message is already complete human text
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
        if res["warnings"]:
            # v45: the "; " separator is STRUCTURAL, not per-segment
            # luck — each segment is stripped of surrounding space and
            # any trailing ';' it arrived with, and empties are
            # dropped, before the join. The v43 live banner showed the
            # include-results segment and the for-sale note run
            # together ("...(Realtor16) for-sale lookup..."): with the
            # raw join, a segment boundary is only as clean as the
            # segment text itself. Normalized here — the ONE place the
            # banner is assembled — every boundary is "; " by
            # construction.
            _segs = [str(w).strip().rstrip(";").strip() for w in res["warnings"]]
            _segs = [s for s in _segs if s]
            if _segs: msg += " · ⚠ " + "; ".join(_segs)
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
            msg = ("Census data didn't come back — Census Reporter and api.census.gov (and the "
                   "Census Geocoder for the city scope) may be unreachable right now. "
                   "Cached data, if any, is unchanged.")
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

_MKT_NOMINAL_MAX = 1000  # v53: county nominal-consideration marker for Market Data stats
def _market_pct_line(first, last):
    """Snapshot % line between two trend buckets, or None when the baseline
    median is nominal (< $1,000) — a % computed off a $100/$200 county
    quitclaim-style baseline is an artifact, never a market move."""
    if not first or not last: return None
    f, l = first.get("median"), last.get("median")
    if f is None or l is None or f < _MKT_NOMINAL_MAX: return None
    chg = (l - f) / f * 100
    return (f"Median sale price moved from {money(f)} ({first['month']}) to "
            f"{money(l)} ({last['month']}) — {'+' if chg >= 0 else ''}{chg:.1f}% over the stored trend window.")

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
    # v57.1: bulk-group keys over the FULL stored sold pool, computed
    # once so the stats and the card badges agree even when a date
    # window splits a group (a lone group member inside a window is
    # still bulk); bulk_group_ids is the member-row ids for the badge.
    _bulk_keys = _bulk_group_keys([c for c in comps if c["kind"] == "sold"])
    bulk_group_ids = {c["id"] for c in comps if c["kind"] == "sold" and _bulk_group_key(c) in _bulk_keys}
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
    v = verdict(p, comps, all_boxes(), _bulk_keys)
    sold = [c for c in comps if c["kind"] == "sold" and not c["excluded"]]
    # v48: rents respect `excluded` exactly like sold (the audit's
    # adjacent integrity gap — an excluded rental used to keep
    # rendering in every rent view).
    rents = [c for c in comps if c["kind"] == "rent" and not c["excluded"]]
    market = None
    if sold:
        # v1.6 working controls: property-type filter recomputes cards + trend +
        # nearby table; 1Y/3Y/5Y range affects the trend only (ReiSearch semantics).
        mtype = request.args.get("mtype", "All")
        if mtype not in ("All", "Single Family", "Condo", "Townhouse"): mtype = "All"
        mrange = request.args.get("mrange", "3Y")
        if mrange not in ("1Y", "3Y", "5Y"): mrange = "3Y"
        def _ptype_of(c): return (c["ptype"] if "ptype" in c.keys() else None) or "Single Family"  # pre-v1.6 rows/DBs carry no stored type
        # v53: Market Data is ZIP-scoped (the page's own claim: "Market
        # Overview for ZIP 33613" / "across N stored sold comps in ZIP ...")
        # and nominal county-consideration sales (< $1,000) are excluded
        # from the STATS ONLY — the rows stay stored and listed in
        # Comparables. The nearby-ZIP table keeps its radius-wide pool
        # (nominal-filtered), so its YOU row is the same ZIP pool the
        # headline reports: headline median == YOU-row median, always.
        # v57: both pools ALSO pass _stats_usable_sold — a bulk-flagged
        # row or a $/sqft outlier can never move the headline, the
        # trend, or the nearby table, even when it was stored before
        # the merge gate existed (the live Seaford rows, until the
        # approved cleanup exclude-flags them).
        _mkt = _stats_usable_sold([c for c in sold if c["zip"] == p["zip"] and not (c["price"] and c["price"] < _MKT_NOMINAL_MAX)], _bulk_keys)
        msold = [c for c in _mkt if mtype == "All" or _ptype_of(c) == mtype]
        type_counts = {}
        for c in _mkt: type_counts[_ptype_of(c)] = type_counts.get(_ptype_of(c), 0) + 1
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
        # v53: nearby table stays radius-wide but nominal-filtered (its YOU
        # row is the ZIP pool the headline reports). v57: stats-usable too.
        _rad = _stats_usable_sold([c for c in sold if not (c["price"] and c["price"] < _MKT_NOMINAL_MAX)
                and (mtype == "All" or _ptype_of(c) == mtype)], _bulk_keys)
        zip_groups = {}
        for c in _rad:
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
            _line = _market_pct_line(trend_all[0], trend_all[-1])
            if _line:
                snap.append(_line)
            else:
                # v53 defense-in-depth: never print a % off a nominal baseline.
                snap.append("Trend window starts from a nominal-consideration sale — no % change is shown.")
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
                  "untyped": sum(1 for c in _mkt if not ("ptype" in c.keys() and c["ptype"])),
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
    # v3.5: the view window extends to the stored horizon (v34 pulls 3 years
    # of sold comps) — 730 = Last 2 years, 1095 = Last 3 years, 36500 = All
    # time (a sentinel far beyond any real sale date). Default stays 365.
    if window not in (30, 90, 180, 365, 730, 1095, 36500): window = 365
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
    # v56: SAME STREET FIRST (Sherif 2026-11-05) — the street bucket
    # leads both sorts; tier / match group / pinned / score / distance
    # keep their established order inside it.
    sold_view.sort(key=lambda c: (_same_street(p, c["address"]), comp_tier(_subj, dict(c)),
                                  -(c["pinned"] or 0),
                                  -(c["match_score"] or 0),
                                  c["distance_mi"] if c["distance_mi"] is not None else 9e9))
    rents_view.sort(key=lambda c: (_same_street(p, c["address"]), rent_match_group(_subj, dict(c)),
                                   -(c["match_score"] or 0),
                                   c["distance_mi"] if c["distance_mi"] is not None else 9e9))
    # v56: the cards carrying the "Same street" badge (comp ids in view).
    same_street_ids = {c["id"] for c in sold_view + rents_view
                       if _same_street(p, c["address"]) == 0}
    # v48 D6: collapse stored duplicates for display (best-scored copy
    # of each address+unit+beds wins — the list is already in display
    # order). Header counts below read THIS list, so they reflect the
    # collapsed count.
    rents_view = _collapse_rent_view(rents_view)
    rent_match = {"exact": 0, "near": 0, "total": len(rents_view)}
    for c in rents_view:
        _g = rent_match_group(_subj, dict(c))
        if _g == 0: rent_match["exact"] += 1
        elif _g == 1: rent_match["near"] += 1
    sold_stats = pinned_sold_stats(sold_view, _bulk_keys)
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
    # v57: n stays the IN-VIEW count (the count-check line above uses it
    # as "currently shows N sold", and the cards render all N), but the
    # AVERAGES are computed over the stats-usable subset only — a bulk
    # row or $/sqft outlier shown in the view must never price the
    # average. stats_n / stats_excluded tell the template to say so.
    _set_sold = [c for c in sold_view if c["kind"] == "sold"]
    _set_usable = _stats_usable_sold(_set_sold, _bulk_keys)
    # v57.4: the id set the whole-set stats exclude — the SINGLE source
    # of truth for the client-side whole-set line. The template marks
    # these cards data-stat-excluded="1" and refreshSetStats() skips
    # them, so the JS average can never re-include a row the server
    # excluded (bulk/outlier rows were pricing the line at the raw
    # $4,727,190 after every Sold-view interaction). Computed from the
    # SAME _stats_usable_sold call that produces set_stats, never
    # re-derived in the template.
    _usable_ids = {c["id"] for c in _set_usable}
    stat_excluded_ids = {c["id"] for c in _set_sold if c["id"] not in _usable_ids}
    _set_prices = [c["price"] for c in _set_usable if c["price"]]
    _set_ppsf = [(c["price"] / c["sqft"]) for c in _set_usable if c["price"] and c["sqft"]]
    _set_doms = [c["dom"] for c in _set_usable if c["dom"] is not None]
    set_stats = {"n": len(_set_sold),
                 "stats_n": len(_set_usable),
                 "stats_excluded": len(_set_sold) - len(_set_usable),
                 "avg": round(sum(_set_prices) / len(_set_prices)) if _set_prices else None,
                 "ppsf": round(sum(_set_ppsf) / len(_set_ppsf)) if _set_ppsf else None,
                 "dom": round(sum(_set_doms) / len(_set_doms)) if _set_doms else None}
    # v2.0 count reconciliation: the header count is ALL stored sold comps;
    # the Sold view applies the date window — the difference is dated sales
    # older than the window. Both bases are labeled on the page.
    sold_older_n = len([c for c in sold if c["event_date"] and c["event_date"] < cutoff])
    ltr = ltr_stats(rents_view)
    coli = coliving_stats(rents_view, p)
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
    # v57: the flip pool is stats-usable only — a bulk row must never price the exit table.
    flip_pool = [c for c in _stats_usable_sold(sold_view, _bulk_keys) if c["pinned"] and c["kind"] == "sold"] or \
        [c for c in _stats_usable_sold(sold_view, _bulk_keys) if c["kind"] == "sold"][:3]
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
                           rent_match=rent_match, same_street_ids=same_street_ids,
                           bulk_group_ids=bulk_group_ids,
                           stat_excluded_ids=stat_excluded_ids,
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
    days = days if days in (30, 90, 180, 365, 730, 1095, 36500) else 365
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
    canonical suffixes, single spaces. '15037 Abby Birch Place' -> '15037 abby birch pl'.
    v49: a slug-artifact tail ("# Usa") is stripped first (_strip_addr_artifact),
    so an artifact row and its clean twin normalize identically."""
    s = _strip_addr_artifact(s)
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
    """Insert one comp from the add-by-address / manual paths (v48:
    dedupe FIRST, via the same keys the merges use — the add paths
    used to blind-insert, so adding an already-stored comp cloned
    it). A hit updates the stored row in place and returns ITS id."""
    d = db()
    subj = {"sqft": prop["sqft"], "beds": prop["beds"], "baths": prop["baths"],
            "year_built": prop["year_built"], "lot_sqft": prop["lot_sqft"]}
    vals["match_score"] = match_score(subj, {"distance_mi": vals.get("distance_mi"), "sqft": vals.get("sqft"),
                                             "beds": vals.get("beds"), "baths": vals.get("baths"),
                                             "year_built": vals.get("year_built"), "lot_sqft": vals.get("lot_sqft")})
    ex = _find_existing_comp(d, prop["id"], vals.get("kind") or "sold",
                             vals.get("address") or "", vals.get("price"),
                             beds=vals.get("beds"), event_date=vals.get("event_date"),
                             source_url=vals.get("source_url"))
    cols = ",".join(vals.keys()); ph = ",".join("?" * len(vals))
    if ex:
        d.execute(f"UPDATE comps SET {','.join(k + '=?' for k in vals)} WHERE id=?",
                  (*vals.values(), ex))
        d.commit()
        return ex
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

@app.route("/floorplan/<int:fid>/delete", methods=["POST"])
def floorplan_delete(fid):
    """v52 (whole-app test pass): Floor Plans had add but no delete —
    a saved plan could never be removed from the UI. Mirrors doc_delete."""
    row = db().execute("SELECT property_id, title FROM floorplans WHERE id=?", (fid,)).fetchone()
    if row:
        db().execute("DELETE FROM floorplans WHERE id=?", (fid,)); db().commit()
        flash(f"Floor plan removed: {row['title'] or 'Plan'}", "info")
        return redirect(url_for("property_page", pid=row["property_id"], tab="floorplans"))
    return redirect(url_for("index"))

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        if "census_key" in request.form:
            for k in ("census_key", "rentcast_key", "apillow_key"):
                set_setting(k, request.form.get(k, ""))
            # RapidAPI key (v3.8): the field renders MASKED (blank password
            # input — the saved key is never sent to the page), so a
            # non-empty submit is a NEW key, a blank submit keeps the saved
            # one, and only the explicit clear checkbox removes it.
            if request.form.get("rapidapi_key_clear"):
                set_setting("rapidapi_key", "")
            elif (request.form.get("rapidapi_key") or "").strip():
                set_setting("rapidapi_key", request.form.get("rapidapi_key").strip())
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
                           apillow_key=get_setting("apillow_key"),
                           rapidapi_key_set=bool(_rapidapi_key()),
                           rapidapi_env_set=bool(os.environ.get("RAPIDAPI_API_KEY")))

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
    """ISO date -> ReiSearch's display form ('Oct 27, 2025').
    Month-precision dates ('2025-06' — the FL Cadastral form) render
    as 'Jun 2025': the source records no day, so no day is shown."""
    if not v: return "—"
    s = str(v)
    if re.fullmatch(r"\d{4}-\d{2}", s):
        try: return datetime.datetime.strptime(s, "%Y-%m").strftime("%b %Y")
        except Exception: return s
    try: return datetime.datetime.strptime(s[:10], "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
    except Exception: return s

@app.template_filter("jload")
def jload(v):
    try: return json.loads(v) if v else {}
    except Exception: return {}

init_db()

if __name__ == "__main__":
    init_db()
    app.run(host=os.environ.get("PDA_HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", os.environ.get("PDA_PORT", "5050"))), debug=False)
