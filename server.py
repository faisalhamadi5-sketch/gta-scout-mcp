"""
GTA Scout MCP Server — Repliers Edition v3
===========================================
Production-grade MCP server. Live TRREB/RAHB data via Repliers API.

Changes from v2 (based on ChatGPT code review):
  [HIGH]   Implements official JSON-RPC 2.0 MCP protocol
           (initialize, notifications/initialized, tools/list, tools/call)
  [HIGH]   Parallel API calls via ThreadPoolExecutor (city x keyword searches)
  [HIGH]   Auto-pagination — fetches all pages, not just first 25/50
  [HIGH]   Retry logic with exponential backoff (429/500/502/503)
  [MEDIUM] Structured JSON errors throughout
  [MEDIUM] lat/lng range validation
  [MEDIUM] Decimal radius precision preserved
  [MEDIUM] logging module replaces print()
  [MEDIUM] Keyword lists as module-level constants
  [MEDIUM] requests library replaces urllib
  [PERF]   Concurrent city+keyword searches
  [SEC]    Optional bearer token auth (MCP_AUTH_TOKEN env var)
  [SEC]    Request size limit (1MB)
  [SEC]    CORS restricted to configurable origins
  [PROD]   30-second in-memory cache (TTL per tool)
  [PROD]   Request ID tracking
  [PROD]   /health checks Repliers connectivity
  [PROD]   /version endpoint
  [PROD]   Graceful shutdown on SIGTERM/SIGINT

Deploy: Railway, Render, or any Python 3.10+ host.
Env vars:
  REPLIERS_API_KEY  — required
  PORT              — default 8000
  MCP_AUTH_TOKEN    — optional bearer token to protect the server
  CORS_ORIGIN       — default * (restrict in production)
"""

import json
import logging
import math
import os
import re
import signal
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GTA Scout] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gta_scout")

# ── Config ────────────────────────────────────────────────────────────────────

REPLIERS_API_KEY  = os.environ.get("REPLIERS_API_KEY", "")
REPLIERS_BASE     = "https://api.repliers.io/listings"
PORT              = int(os.environ.get("PORT", 8000))
MCP_AUTH_TOKEN    = os.environ.get("MCP_AUTH_TOKEN", "")
CORS_ORIGIN       = os.environ.get("CORS_ORIGIN", "*")
MAX_BODY_BYTES    = 1 * 1024 * 1024  # 1 MB
CACHE_TTL_SECS    = 30
MAX_WORKERS       = 8

HALTON_CITIES = ["Burlington", "Milton", "Oakville", "Halton Hills"]

LISTING_FIELDS = (
    "mlsNumber,listPrice,soldPrice,daysOnMarket,numBedrooms,numBathrooms,"
    "status,lastStatus,address,details,map,timestamps,publicRemarks,office,lot"
)

# Module-level keyword constants
POS_KEYWORDS = [
    "POWER OF SALE", "POS ", "COURT ORDER",
    "AS IS WHERE IS", "AS-IS WHERE-IS",
    "MORTGAGEE", "RECEIVERSHIP", "LENDER APPROVAL",
]

DEV_KEYWORDS = [
    "DEVELOPMENT", "ZONING", "SEVERANCE", "ASSEMBLY",
    "HOLDING ZONE", "REDEVELOP", "OPA", "ZBA",
    "INTENSIFICATION", "OFFICIAL PLAN", "DRAFT PLAN",
    "H-DRH", "SITE PLAN", "MIXED USE", "REZONING",
]

POS_SEARCH_TERMS = ["power of sale", "court order", "mortgagee"]
DEV_SEARCH_TERMS = ["development", "zoning", "severance", "OPA",
                    "holding zone", "land assembly", "rezoning"]

# ── Distress scoring engine ──────────────────────────────────────────────────
# A score 0-100 estimating how likely a listing is to close below asking, fast.
# Weights are tunable — this is the moat.
#
# Signals are grouped by strength of evidence:
#   Explicit distress language (heavy)      — POS, court order, as-is, estate
#   Time pressure (medium)                  — DOM, relisted after expiry
#   Price behavior (medium)                 — price cuts, below-median pricing
#   Property condition (light)              — fixer-upper language
#   Negative signals (reduce score)         — multiple offers, hot area, new
#
# High score (70+)  = call today
# Medium (40-69)    = warm lead worth researching
# Low (<40)         = probably not distressed

# Keyword → point weights. Uppercase; matched against uppercased remarks.
# Calibrated with practitioner input (Halton market): POS and estate signals
# weighted heavily because a distressed seller who has already gone to market
# is a serious lead.
DISTRESS_KEYWORD_WEIGHTS: list[tuple[str, int]] = [
    # Explicit distress (highest confidence)
    ("POWER OF SALE",       45),
    ("COURT ORDER",         45),
    ("MORTGAGEE",           35),
    ("RECEIVERSHIP",        35),
    ("LENDER APPROVAL",     30),
    ("AS IS WHERE IS",      20),
    ("AS-IS WHERE-IS",      20),
    ("SOLD AS IS",          10),
    ("ESTATE SALE",         65),
    ("PROBATE",             65),
    ("ESTATE OF",           65),
    ("ESTATE",              60),
    # Motivation language
    ("MOTIVATED SELLER",    12),
    ("MUST SELL",           12),
    ("BRING ALL OFFERS",     8),
    ("BRING OFFERS",         8),
    ("PRICED TO SELL",       6),
    # Property condition (soft distress)
    ("FIXER-UPPER",          5),
    ("FIXER UPPER",          5),
    ("HANDYMAN SPECIAL",     5),
    ("NEEDS TLC",            5),
    ("COSMETIC UPDATES",     3),
    ("NEEDS WORK",           5),
]

# Negative signals — reduce score
DISTRESS_NEGATIVE_WEIGHTS: list[tuple[str, int]] = [
    ("MULTIPLE OFFERS",    -15),
    ("OFFER DATE",          -8),   # explicit offer date = seller controls
    ("OVER ASKING",        -10),
    ("HOLD BACK OFFERS",    -8),
]

def score_distress(listing_dict: dict, price_history: Optional[list] = None) -> tuple[int, list[str]]:
    """
    Compute a 0-100 distress score plus a human-readable breakdown.
    Takes the RAW listing dict (before normalize) because it needs details
    that don't survive normalization (price history, original price, etc).

    Returns: (score, [reason strings for tooltip/breakdown])

    Score is clamped to 0-100.
    """
    reasons: list[str] = []
    score = 0

    remarks_raw = (
        safe_str(listing_dict.get("publicRemarks")) + " " +
        safe_str((listing_dict.get("details") or {}).get("extras"))
    ).upper()

    # ── Explicit keyword signals ──
    # Track which "families" of keywords have already scored so we don't
    # double-count (e.g. 'ESTATE SALE' shouldn't also trigger 'ESTATE').
    matched_families: set[str] = set()

    def keyword_family(kw: str) -> str:
        """Group related keywords so only the highest-weight one scores."""
        if "ESTATE" in kw or "PROBATE" in kw: return "estate"
        if "POWER OF SALE" in kw or "POS" in kw: return "pos"
        if "COURT ORDER" in kw or "MORTGAGEE" in kw or "RECEIVERSHIP" in kw or "LENDER APPROVAL" in kw: return "court"
        if "AS IS" in kw or "AS-IS" in kw or "SOLD AS IS" in kw: return "asis"
        if "MOTIVATED" in kw or "MUST SELL" in kw: return "motivated"
        if "BRING" in kw or "PRICED TO SELL" in kw: return "priced"
        if "FIXER" in kw or "HANDYMAN" in kw or "TLC" in kw or "COSMETIC" in kw or "NEEDS WORK" in kw: return "condition"
        return kw   # single-keyword family

    for kw, weight in DISTRESS_KEYWORD_WEIGHTS:
        if kw in remarks_raw:
            fam = keyword_family(kw)
            if fam not in matched_families:
                score += weight
                reasons.append(f"{kw.title().strip()} +{weight}")
                matched_families.add(fam)

    # Standalone "POS" abbreviation — use regex word boundary so 'POS.' 'POS!'
    # 'POS,' or 'POS ' all match, but 'position'/'posted' don't.
    if "pos" not in matched_families and re.search(r"\bPOS\b", remarks_raw):
        score += 45
        reasons.append("POS +45")
        matched_families.add("pos")

    # ── Negative signals ──
    for kw, weight in DISTRESS_NEGATIVE_WEIGHTS:
        if kw in remarks_raw:
            score += weight   # weight is negative
            reasons.append(f"{kw.title().strip()} {weight}")

    # ── Time pressure ──
    dom = safe_int(listing_dict.get("daysOnMarket"))
    if dom >= 120:
        score += 15
        reasons.append(f"On market {dom}d +15")
    elif dom >= 90:
        score += 10
        reasons.append(f"On market {dom}d +10")
    elif dom >= 60:
        score += 5
        reasons.append(f"On market {dom}d +5")
    elif dom > 0 and dom < 7:
        score -= 10
        reasons.append(f"Newly listed ({dom}d) -10")

    # ── Relisted after expiry — big signal ──
    last_status = safe_str(listing_dict.get("lastStatus")).upper()
    if last_status in ("EXP", "EXT", "TER"):
        # This listing was previously expired/terminated and came back
        score += 15
        reasons.append("Previously expired & relisted +15")

    # ── Price cuts (if we can detect them) ──
    # Repliers provides originalPrice on some records; fall back to soldPrice comparison
    list_price = safe_float(listing_dict.get("listPrice"))
    orig_price = safe_float(listing_dict.get("originalPrice"))
    if list_price > 0 and orig_price > 0 and orig_price > list_price:
        cut_pct = ((orig_price - list_price) / orig_price) * 100
        if cut_pct >= 10:
            score += 15
            reasons.append(f"Price cut {cut_pct:.0f}% +15")
        elif cut_pct >= 5:
            score += 8
            reasons.append(f"Price cut {cut_pct:.0f}% +8")

    # ── Price history array (Repliers 'history' field, if available) ──
    if price_history and isinstance(price_history, list):
        cuts = sum(1 for h in price_history if isinstance(h, dict) and h.get("event") == "PriceDrop")
        if cuts >= 2:
            score += cuts * 5
            reasons.append(f"{cuts} price drops +{cuts * 5}")

    # ── Compound signals — practitioner-derived multipliers ──
    # These are the "gut instincts" a Halton REALTOR has that raw keyword
    # counting misses. A distressed seller who's ALSO been sitting is
    # exponentially more distressed than either signal alone.
    has_explicit_distress = any(kw in remarks_raw for kw in [
        "POWER OF SALE", "COURT ORDER", "MORTGAGEE", "RECEIVERSHIP",
        "ESTATE SALE", "PROBATE",
    ])
    if has_explicit_distress and dom >= 100:
        score += 15
        reasons.append("Distress + 100+ DOM (seller stuck) +15")
    elif has_explicit_distress and dom >= 60:
        score += 8
        reasons.append("Distress + 60+ DOM +8")

    # Clamp 0-100
    score = max(0, min(100, score))
    return score, reasons


def distress_tier(score: int) -> str:
    """Human-readable tier label."""
    if score >= 65: return "HIGH — call today"
    if score >= 40: return "MEDIUM — warm lead"
    if score >= 20: return "LOW — worth watching"
    return "MINIMAL"

# ── HTTP session with retry ───────────────────────────────────────────────────

def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.0,           # 1s, 2s, 4s, 8s
        status_forcelist=[429, 500, 502, 503],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update({
        "REPLIERS-API-KEY": REPLIERS_API_KEY,
        "Accept":           "application/json",
        "Content-Type":     "application/json",
    })
    return session

_SESSION: Optional[requests.Session] = None
_SESSION_LOCK = __import__("threading").Lock()

def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:          # double-checked locking
                _SESSION = _build_session()
    return _SESSION

# ── In-memory cache ───────────────────────────────────────────────────────────

_cache: dict[str, tuple[Any, float]] = {}

CACHE_MAX_ENTRIES = 500

def cache_get(key: str) -> Optional[Any]:
    entry = _cache.get(key)          # atomic read, no TOCTOU
    if entry is None:
        return None
    val, ts = entry
    if time.time() - ts < CACHE_TTL_SECS:
        return val
    _cache.pop(key, None)            # pop with default — safe if another thread already deleted
    return None

def cache_set(key: str, val: Any) -> None:
    # Cap memory: evict expired entries first, then oldest, before inserting
    if len(_cache) >= CACHE_MAX_ENTRIES:
        now = time.time()
        expired = [k for k, (_, ts) in list(_cache.items()) if now - ts >= CACHE_TTL_SECS]
        for k in expired:
            _cache.pop(k, None)
        # Still full? Drop oldest ~20% by timestamp
        if len(_cache) >= CACHE_MAX_ENTRIES:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][1])[: CACHE_MAX_ENTRIES // 5]
            for k, _ in oldest:
                _cache.pop(k, None)
    _cache[key] = (val, time.time())

def cache_key(tool: str, args: dict) -> str:
    raw = json.dumps({"tool": tool, "args": args}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()

# ── Safe type helpers ─────────────────────────────────────────────────────────

def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        result = float(str(v).replace(",", "").strip())
        # inf/nan serialize as 'Infinity'/'NaN' — invalid JSON for strict parsers
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default

def safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError, OverflowError):
        return default

def safe_str(v: Any, default: str = "") -> str:
    return str(v) if v is not None else default

def safe_date_days(date_str: Any) -> Optional[int]:
    if not date_str:
        return None
    try:
        return (datetime.today() - datetime.fromisoformat(str(date_str)[:10])).days
    except (ValueError, TypeError):
        return None

def safe_round(v: Any, default: int = 0) -> int:
    try:
        return round(float(v or default))
    except (TypeError, ValueError, OverflowError):
        return default

def validate_latlng(lat: float, lng: float) -> Optional[str]:
    if not (-90 <= lat <= 90):
        return f"latitude {lat} out of range (-90 to 90)"
    if not (-180 <= lng <= 180):
        return f"longitude {lng} out of range (-180 to 180)"
    if lat == 0 and lng == 0:
        return "lat/lng both 0 — likely a missing geocode"
    return None

# ── Repliers API caller ───────────────────────────────────────────────────────

def repliers_get(params: dict) -> dict:
    """Single page GET to Repliers. Raises RuntimeError on failure."""
    if not REPLIERS_API_KEY:
        raise RuntimeError(
            "REPLIERS_API_KEY is not set. "
            "Add it in Railway → Settings → Variables."
        )
    clean = {k: v for k, v in params.items() if v is not None}
    # Repliers rejects minPrice=0 — omit when zero or negative
    if "minPrice" in clean and safe_float(clean["minPrice"]) <= 0:
        del clean["minPrice"]
    try:
        resp = get_session().get(REPLIERS_BASE, params=clean, timeout=20)
        if resp.status_code == 401:
            raise RuntimeError("Repliers API key invalid or expired (401)")
        if resp.status_code == 403:
            raise RuntimeError("Repliers API key lacks permission (403)")
        if not resp.ok:
            raise RuntimeError(f"Repliers error {resp.status_code}: {resp.text[:200]}")
        return resp.json()
    except requests.RequestException as e:
        raise RuntimeError(f"Network error: {e}")


def repliers_get_all(params: dict, max_pages: int = 5) -> list[dict]:
    """Paginate through Repliers results, returning all listings up to max_pages."""
    all_listings = []
    page = 1
    per_page = params.get("resultsPerPage", 50)

    while page <= max_pages:
        p = {**params, "pageNum": page, "resultsPerPage": per_page}
        data = repliers_get(p)
        batch = data.get("listings", [])
        all_listings.extend(batch)

        total = data.get("count", 0)
        fetched = page * per_page
        if fetched >= total or len(batch) < per_page:
            break
        page += 1

    return all_listings


# ── Dataclass for listings ────────────────────────────────────────────────────

@dataclass
class Listing:
    mls: str
    address: str
    city: str
    status: str
    last_status: str
    price: float
    sold_price: float
    dom: int
    beds: int
    baths: int
    type: str
    style: str
    sqft: str
    list_date: str
    exp_date: str
    days_since_expiry: Optional[int]
    lat: float
    lng: float
    remarks: str
    brokerage: str
    is_pos: bool
    is_dev: bool
    lot_width: float
    lot_depth: float
    lot_size: str
    distress_score: int = 0
    distress_tier: str = "MINIMAL"
    distress_reasons: list = field(default_factory=list)
    distance_km: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def normalize(l: dict) -> Listing:
    """Flatten a Repliers listing object into a Listing dataclass."""
    addr    = l.get("address")    or {}
    details = l.get("details")    or {}
    ts      = l.get("timestamps") or {}
    map_    = l.get("map")        or {}
    office  = l.get("office")     or {}
    lot     = l.get("lot")        or {}

    remarks = (
        safe_str(l.get("publicRemarks")).upper() + " " +
        safe_str(details.get("extras")).upper()
    )

    street = " ".join(filter(None, [
        safe_str(addr.get("streetNumber")),
        safe_str(addr.get("streetName")),
        safe_str(addr.get("streetSuffix")),
    ]))
    city         = safe_str(addr.get("city"))
    full_address = f"{street}, {city}".strip(", ")
    exp_raw      = safe_str(ts.get("expiryDate"))

    score, reasons = score_distress(l, price_history=l.get("history"))

    return Listing(
        mls               = safe_str(l.get("mlsNumber")),
        address           = full_address,
        city              = city,
        status            = safe_str(l.get("status")),
        last_status       = safe_str(l.get("lastStatus")),
        price             = safe_float(l.get("listPrice")),
        sold_price        = safe_float(l.get("soldPrice")),
        dom               = safe_int(l.get("daysOnMarket")),
        beds              = safe_int(l.get("numBedrooms")),
        baths             = safe_int(l.get("numBathrooms")),
        type              = safe_str(details.get("propertyType")),
        style             = safe_str(details.get("style")),
        sqft              = safe_str(details.get("sqft")),
        list_date         = safe_str(ts.get("listingEntryDate"))[:10],
        exp_date          = exp_raw[:10] if exp_raw else "",
        days_since_expiry = safe_date_days(exp_raw),
        lat               = safe_float(map_.get("latitude")),
        lng               = safe_float(map_.get("longitude")),
        remarks           = safe_str(l.get("publicRemarks"))[:400].strip(),
        brokerage         = safe_str(office.get("brokerageName")),
        is_pos            = any(kw in remarks for kw in POS_KEYWORDS),
        is_dev            = any(kw in remarks for kw in DEV_KEYWORDS),
        lot_width         = safe_float(lot.get("width")),
        lot_depth         = safe_float(lot.get("depth")),
        lot_size          = safe_str(lot.get("size")),
        distress_score    = score,
        distress_tier     = distress_tier(score),
        distress_reasons  = reasons,
    )


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R    = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a    = math.sin(dlat / 2) ** 2 + \
           math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * \
           math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Parallel search helper ────────────────────────────────────────────────────

def parallel_search(cities: list[str], keywords: list[str],
                    base_params: dict, filter_fn) -> list[Listing]:
    """
    Run city x keyword searches in parallel via ThreadPoolExecutor.
    Deduplicates by MLS number. Applies filter_fn(Listing) -> bool.
    """
    seen: set[str]    = set()
    results: list[Listing] = []

    # Fail loud BEFORE spawning workers if the key is missing — otherwise
    # every fetch fails identically and we'd return misleading empty results.
    if not REPLIERS_API_KEY:
        raise RuntimeError("REPLIERS_API_KEY is not set. Add it in Railway → Settings → Variables.")

    def fetch(city: str, kw: str) -> list[Listing]:
        params = {**base_params, "city": city, "search": kw}
        try:
            raw = repliers_get_all(params, max_pages=3)
            return [normalize(l) for l in raw]
        except Exception as e:
            # Isolate ANY per-worker failure — one bad city/keyword combo must not
            # kill the whole search. Partial results beat no results.
            log.warning(f"Search failed city={city} kw={kw!r}: {type(e).__name__}: {e}")
            return []

    tasks = [(c, kw) for c in cities for kw in keywords]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch, c, kw): (c, kw) for c, kw in tasks}
        for future in as_completed(futures):
            for listing in future.result():
                if listing.mls and listing.mls not in seen:
                    seen.add(listing.mls)
                    if filter_fn(listing):
                        results.append(listing)

    return results


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_expired_listings",
        "description": (
            "Search for recently expired MLS listings near a lat/lng point within a radius. "
            "Returns listings that expired within the last N days. "
            "Perfect for prospecting — these sellers are motivated and ready for a new agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat":       {"type": "number",  "description": "Latitude of search centre"},
                "lng":       {"type": "number",  "description": "Longitude of search centre"},
                "radius_km": {"type": "number",  "description": "Search radius in km (default 1.0)", "default": 1.0},
                "days":      {"type": "integer", "description": "Days back to look for expireds (default 90)", "default": 90},
                "city":      {"type": "string",  "description": "Optional city filter e.g. Burlington", "default": ""},
                "min_price": {"type": "number",  "description": "Minimum list price", "default": 0},
                "max_price": {"type": "number",  "description": "Maximum list price", "default": 9999999},
            },
            "required": ["lat", "lng"],
        },
    },
    {
        "name": "search_pos_listings",
        "description": (
            "Search for Power of Sale (POS) listings across Halton Region. "
            "Detects: 'Power of Sale', 'court order', 'as-is where-is', 'mortgagee', 'receivership'. "
            "Runs parallel searches across Burlington, Milton, Oakville, Halton Hills."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city":      {"type": "string", "description": "City filter. Blank = all Halton.", "default": ""},
                "min_price": {"type": "number", "description": "Minimum list price", "default": 0},
                "max_price": {"type": "number", "description": "Maximum list price", "default": 9999999},
                "status":    {"type": "string", "description": "A=active only, U=expired only, blank=both", "default": ""},
            },
            "required": [],
        },
    },
    {
        "name": "search_development_land",
        "description": (
            "Search for development land and redevelopment opportunities across Halton. "
            "Detects: development, zoning, severance, OPA, ZBA, holding zone, "
            "land assembly, intensification, H-DRH, draft plan, site plan, mixed use, rezoning."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city":      {"type": "string", "description": "City filter. Blank = all Halton.", "default": ""},
                "min_price": {"type": "number", "description": "Minimum list price", "default": 0},
                "max_price": {"type": "number", "description": "Maximum list price", "default": 3000000},
                "status":    {"type": "string", "description": "A=active, U=expired, blank=both", "default": "A"},
            },
            "required": [],
        },
    },
    {
        "name": "get_market_stats",
        "description": (
            "Return live market statistics for any Halton city: "
            "active listing count, average list price, average DOM, "
            "expired count in last 90 days."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City to summarise. Blank = all Halton.", "default": ""},
            },
            "required": [],
        },
    },
    {
        "name": "search_active_listings",
        "description": (
            "Search listings in a single Halton city with optional lot-frontage filtering "
            "(min/max lot width in feet, as reported by Repliers' 'lot' object). "
            "Useful for land-scouting queries like '100 ft frontage lots in Burlington'. "
            "Lot data is not populated on every listing — records without it are excluded "
            "when a frontage filter is applied."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city":            {"type": "string", "description": "City to search, e.g. Burlington"},
                "min_price":       {"type": "number", "description": "Minimum list price", "default": 0},
                "max_price":       {"type": "number", "description": "Maximum list price", "default": 9999999},
                "min_frontage_ft": {"type": "number", "description": "Minimum lot frontage/width in feet. 0 = no filter.", "default": 0},
                "max_frontage_ft": {"type": "number", "description": "Maximum lot frontage/width in feet. 0 = no filter.", "default": 0},
                "status":          {"type": "string", "description": "A=active only, U=expired only, blank=both", "default": "A"},
            },
            "required": ["city"],
        },
    },
    {
        "name": "search_sold_listings",
        "description": (
            "Search sold listings in a single Halton city within a lookback window. "
            "Returns sold price alongside original list price — useful for comps and "
            "underwriting against recent closed deals."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city":        {"type": "string", "description": "City to search, e.g. Burlington"},
                "min_price":   {"type": "number",  "description": "Minimum list price", "default": 0},
                "max_price":   {"type": "number",  "description": "Maximum list price", "default": 9999999},
                "months_back": {"type": "integer", "description": "How many months back to look for sold listings (default 6)", "default": 6},
            },
            "required": ["city"],
        },
    },
]

# ── Tool handlers ─────────────────────────────────────────────────────────────

def handle_search_expired(args: dict) -> dict:
    lat       = safe_float(args.get("lat"))
    lng       = safe_float(args.get("lng"))
    radius    = safe_float(args.get("radius_km"), 1.0)  # preserve decimal precision
    days      = safe_int(args.get("days"), 90)
    city      = safe_str(args.get("city"))
    min_price = safe_int(args.get("min_price"), 0)
    max_price = safe_int(args.get("max_price"), 9999999)

    err = validate_latlng(lat, lng)
    if err:
        return {"error": err, "count": 0, "listings": []}

    min_date = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    params: dict = {
        "status":             "U",
        "lastStatus":         "Exp",
        "minUnavailableDate": min_date,
        "lat":                lat,
        "long":               lng,
        "radius":             math.ceil(radius),   # API needs int, round up
        "minPrice":           min_price,
        "maxPrice":           max_price,
        "resultsPerPage":     50,
        "sortBy":             "updatedOnDesc",
        "fields":             LISTING_FIELDS,
    }
    if city:
        params["city"] = city

    raw      = repliers_get_all(params, max_pages=5)
    listings = [normalize(l) for l in raw]

    # Precise haversine filter (API radius is coarse integer km)
    filtered: list[Listing] = []
    for l in listings:
        if l.lat and l.lng:
            dist = haversine_km(lat, lng, l.lat, l.lng)
            if dist <= radius:
                l.distance_km = round(dist, 3)
                filtered.append(l)
        else:
            l.distance_km = None
            filtered.append(l)

    filtered.sort(key=lambda x: (x.distance_km is None, x.distance_km or 999, -x.distress_score))
    return {"count": len(filtered), "listings": [l.to_dict() for l in filtered]}


def handle_search_pos(args: dict) -> dict:
    city      = safe_str(args.get("city"))
    min_price = safe_int(args.get("min_price"), 0)
    max_price = safe_int(args.get("max_price"), 9999999)
    status    = safe_str(args.get("status"))

    cities = [city] if city else HALTON_CITIES

    base: dict = {
        "minPrice":       min_price,
        "maxPrice":       max_price,
        "resultsPerPage": 25,
        "sortBy":         "listPriceAsc",
        "fields":         LISTING_FIELDS,
    }
    if status:
        base["status"] = status
    else:
        base["status"] = ["A", "U"]

    results = parallel_search(cities, POS_SEARCH_TERMS, base, lambda l: l.is_pos)
    results.sort(key=lambda l: (-l.distress_score, l.price))
    return {"count": len(results), "listings": [l.to_dict() for l in results]}


def handle_search_dev_land(args: dict) -> dict:
    city      = safe_str(args.get("city"))
    min_price = safe_int(args.get("min_price"), 0)
    max_price = safe_int(args.get("max_price"), 3000000)
    status    = safe_str(args.get("status"), "A")

    cities = [city] if city else HALTON_CITIES

    base: dict = {
        "minPrice":       min_price,
        "maxPrice":       max_price,
        "resultsPerPage": 20,
        "sortBy":         "listPriceAsc",
        "fields":         LISTING_FIELDS,
    }
    if status:
        base["status"] = status

    results = parallel_search(cities, DEV_SEARCH_TERMS, base, lambda l: l.is_dev)
    results.sort(key=lambda l: l.price)
    return {"count": len(results), "listings": [l.to_dict() for l in results]}


def handle_get_market_stats(args: dict) -> dict:
    city   = safe_str(args.get("city"))
    cities = [city] if city else HALTON_CITIES
    output: dict = {}

    def fetch_city_stats(c: str) -> tuple[str, dict]:
        # Repliers' statistics param requires specific stat names; simpler and
        # more portable to aggregate a page of listings client-side.
        active_data = repliers_get({
            "city": c, "status": "A",
            "resultsPerPage": 100,
            "fields": "listPrice,daysOnMarket",
        })
        active = active_data.get("listings", []) or []

        min_date     = (datetime.today() - timedelta(days=90)).strftime("%Y-%m-%d")
        expired_data = repliers_get({
            "city": c, "status": "U", "lastStatus": "Exp",
            "minUnavailableDate": min_date,
            "resultsPerPage": 100,
            "fields": "listPrice,daysOnMarket",
        })
        expired = expired_data.get("listings", []) or []

        def avg_of(rows: list, key: str) -> int:
            vals = [safe_float(r.get(key)) for r in rows]
            vals = [v for v in vals if v > 0]
            return safe_round(sum(vals) / len(vals)) if vals else 0

        return c, {
            "active_count":      active_data.get("count", 0),
            "avg_active_price":  avg_of(active, "listPrice"),
            "avg_active_dom":    avg_of(active, "daysOnMarket"),
            "expired_90d_count": expired_data.get("count", 0),
            "avg_expired_price": avg_of(expired, "listPrice"),
            "avg_expired_dom":   avg_of(expired, "daysOnMarket"),
            "sample_size_note":  "averages from up to 100 listings per group",
        }

    with ThreadPoolExecutor(max_workers=len(cities)) as pool:
        for city_name, stats in pool.map(fetch_city_stats, cities):
            output[city_name] = stats

    return output


def handle_search_active_listings(args: dict) -> dict:
    city         = safe_str(args.get("city")).strip()
    min_price    = safe_int(args.get("min_price"), 0)
    max_price    = safe_int(args.get("max_price"), 9999999)
    min_frontage = safe_float(args.get("min_frontage_ft"), 0)
    max_frontage = safe_float(args.get("max_frontage_ft"), 0)
    status       = safe_str(args.get("status"), "A")

    if not city:
        return {"error": "city is required", "count": 0, "listings": []}

    params: dict = {
        "city":           city,
        "minPrice":       min_price,
        "maxPrice":       max_price,
        "resultsPerPage": 50,
        "sortBy":         "listPriceAsc",
        "fields":         LISTING_FIELDS,
    }
    params["status"] = status if status else ["A", "U"]

    raw      = repliers_get_all(params, max_pages=5)
    listings = [normalize(l) for l in raw]

    if min_frontage > 0 or max_frontage > 0:
        listings = [
            l for l in listings
            if l.lot_width > 0
            and (min_frontage <= 0 or l.lot_width >= min_frontage)
            and (max_frontage <= 0 or l.lot_width <= max_frontage)
        ]

    listings.sort(key=lambda x: x.price)
    return {"count": len(listings), "listings": [l.to_dict() for l in listings]}


def handle_search_sold(args: dict) -> dict:
    city        = safe_str(args.get("city")).strip()
    min_price   = safe_int(args.get("min_price"), 0)
    max_price   = safe_int(args.get("max_price"), 9999999)
    months_back = max(1, safe_int(args.get("months_back"), 6))

    if not city:
        return {"error": "city is required", "count": 0, "listings": []}

    min_date = (datetime.today() - timedelta(days=months_back * 30)).strftime("%Y-%m-%d")

    params: dict = {
        "city":               city,
        "status":             "U",
        "lastStatus":         "Sld",
        "minUnavailableDate": min_date,
        "minPrice":           min_price,
        "maxPrice":           max_price,
        "resultsPerPage":     50,
        "sortBy":             "updatedOnDesc",
        "fields":             LISTING_FIELDS,
    }

    raw      = repliers_get_all(params, max_pages=5)
    listings = [normalize(l) for l in raw]
    listings.sort(key=lambda x: x.price)
    return {"count": len(listings), "listings": [l.to_dict() for l in listings]}


HANDLERS: dict[str, Any] = {
    "search_expired_listings": handle_search_expired,
    "search_pos_listings":     handle_search_pos,
    "search_development_land": handle_search_dev_land,
    "get_market_stats":        handle_get_market_stats,
    "search_active_listings":  handle_search_active_listings,
    "search_sold_listings":    handle_search_sold,
}

# ── MCP JSON-RPC protocol ─────────────────────────────────────────────────────

def mcp_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

def mcp_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}

def handle_jsonrpc(payload: Any) -> Optional[dict]:
    """
    Dispatch a JSON-RPC 2.0 MCP request.
    Returns None for notifications (no response required).
    """
    # Guard: payload must be an object. Batch arrays and primitives → -32600.
    if not isinstance(payload, dict):
        kind = "batch requests are not supported" if isinstance(payload, list) \
               else f"request must be a JSON object, got {type(payload).__name__}"
        return mcp_error(None, -32600, f"Invalid Request: {kind}")

    req_id  = payload.get("id")
    method  = payload.get("method", "")
    params  = payload.get("params") or {}

    # Guard: params must be an object if present
    if not isinstance(params, dict):
        return mcp_error(req_id, -32602, f"Invalid params: expected object, got {type(params).__name__}")

    log.info(f"JSON-RPC method={method!r} id={req_id}")

    # Notifications — no response
    if method == "notifications/initialized":
        return None

    # initialize — capability handshake
    if method == "initialize":
        return mcp_result(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": "gta-scout-mcp", "version": "3.0.0"},
        })

    # tools/list
    if method == "tools/list":
        return mcp_result(req_id, {"tools": TOOLS})

    # tools/call
    if method == "tools/call":
        name      = (params.get("name") or "")
        tool_args = params.get("arguments")
        if tool_args is None:
            tool_args = {}
        if not isinstance(tool_args, dict):
            return mcp_error(req_id, -32602, f"Invalid arguments: expected object, got {type(tool_args).__name__}")

        if name not in HANDLERS:
            return mcp_error(req_id, -32601, f"Unknown tool: {name!r}. Available: {list(HANDLERS)}")

        ck = cache_key(name, tool_args)
        cached = cache_get(ck)
        if cached is not None:
            log.info(f"Cache hit for {name}")
            return mcp_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(cached)}]
            })

        try:
            result = HANDLERS[name](tool_args)
            cache_set(ck, result)
            return mcp_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, allow_nan=False)}]
            })
        except RuntimeError as e:
            return mcp_error(req_id, -32000, str(e))
        except Exception as e:
            log.exception(f"Unexpected error in {name}")
            return mcp_error(req_id, -32603, f"Internal error: {e}")

    return mcp_error(req_id, -32601, f"Method not found: {method!r}")


# ── HTTP server ───────────────────────────────────────────────────────────────

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MCPHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info(f"{self.address_string()} {fmt % args}")

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin",  CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def send_json(self, code: int, data: Any) -> None:
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type",   "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            log.warning("Client disconnected before response completed")

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _check_auth(self) -> bool:
        if not MCP_AUTH_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {MCP_AUTH_TOKEN}"

    def do_GET(self) -> None:
        if not self._check_auth():
            self.send_json(401, {"error": "unauthorized"})
            return

        path = urlparse(self.path).path

        if path in ("/", "/health"):
            # Check Repliers connectivity
            repliers_ok = False
            if REPLIERS_API_KEY:
                try:
                    r = get_session().get(REPLIERS_BASE, params={"resultsPerPage": 1}, timeout=5)
                    repliers_ok = r.status_code not in (401, 403)
                except Exception:
                    pass
            self.send_json(200, {
                "status":        "ok",
                "server":        "GTA Scout MCP v3",
                "tools":         len(TOOLS),
                "api_key_set":   bool(REPLIERS_API_KEY),
                "repliers_ping": repliers_ok,
                "cache_entries": len(_cache),
            })

        elif path == "/version":
            self.send_json(200, {"version": "3.0.0", "protocol": "MCP JSON-RPC 2.0"})

        elif path in ("/mcp/tools", "/tools"):
            self.send_json(200, {"tools": TOOLS})

        else:
            self.send_json(404, {"error": f"not found: {path}"})

    def _read_body(self) -> tuple[Optional[bytes], Optional[str]]:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            return None, "invalid Content-Length header"
        if length <= 0:
            return None, "empty request body"
        if length > MAX_BODY_BYTES:
            return None, f"request body too large ({length} > {MAX_BODY_BYTES})"
        raw = self.rfile.read(length)
        if not raw.strip():
            return None, "empty request body"
        return raw, None

    def do_POST(self) -> None:
        if not self._check_auth():
            self.send_json(401, {"error": "unauthorized"})
            return

        path = urlparse(self.path).path
        raw, err = self._read_body()
        if err:
            self.send_json(400, {"error": err})
            return

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.send_json(400, {"error": f"invalid JSON: {e}"})
            return

        # ── Official MCP JSON-RPC endpoint ──
        if path == "/mcp":
            response = handle_jsonrpc(payload)
            if response is None:
                # Notification — 204 no content
                self.send_response(204)
                self.end_headers()
            else:
                self.send_json(200, response)
            return

        # ── Legacy REST endpoints (backward compat) ──
        if path in ("/mcp/tools/list", "/tools/list"):
            self.send_json(200, {"tools": TOOLS})
            return

        if path in ("/mcp/tools/call", "/tools/call"):
            name      = payload.get("name", "")
            tool_args = payload.get("arguments") or payload.get("params") or {}
            if not isinstance(tool_args, dict):
                self.send_json(400, {"error": f"arguments must be an object, got {type(tool_args).__name__}"})
                return
            if name not in HANDLERS:
                self.send_json(404, {"error": f"unknown tool: {name!r}"})
                return
            try:
                result = HANDLERS[name](tool_args)
                self.send_json(200, {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, allow_nan=False)}]
                })
            except Exception as e:
                log.exception(f"Error in {name}")
                self.send_json(500, {"error": str(e)})
            return

        self.send_json(404, {"error": f"unknown endpoint: {path}"})


# ── Graceful shutdown ─────────────────────────────────────────────────────────

def _shutdown(server: ThreadingHTTPServer, signum: int, frame: Any) -> None:
    log.info(f"Signal {signum} received — shutting down gracefully")
    # server.shutdown() blocks until serve_forever() exits, but this signal
    # handler runs ON the main thread which is inside serve_forever() —
    # calling shutdown() directly here would deadlock. Run it on a helper thread.
    import threading
    threading.Thread(target=server.shutdown, daemon=True).start()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not REPLIERS_API_KEY:
        log.warning("REPLIERS_API_KEY not set.")
        log.warning("  Railway: Settings → Variables → REPLIERS_API_KEY = your_key")
        log.warning("  Local:   export REPLIERS_API_KEY=your_key && python server.py")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), MCPHandler)

    signal.signal(signal.SIGTERM, lambda s, f: _shutdown(server, s, f))
    signal.signal(signal.SIGINT,  lambda s, f: _shutdown(server, s, f))

    log.info(f"GTA Scout MCP v3 → port {PORT}")
    log.info(f"Tools:     {[t['name'] for t in TOOLS]}")
    log.info(f"Threading: enabled ({MAX_WORKERS} workers)")
    log.info(f"Cache TTL: {CACHE_TTL_SECS}s")
    log.info(f"Auth:      {'enabled' if MCP_AUTH_TOKEN else 'disabled'}")
    log.info(f"CORS:      {CORS_ORIGIN}")
    log.info("Protocol:  MCP JSON-RPC 2.0 at POST /mcp")

    server.serve_forever()
    server.server_close()
    log.info("Server stopped cleanly")
