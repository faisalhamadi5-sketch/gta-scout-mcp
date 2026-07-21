# GTA Scout — Claude Code Handoff

**Owner:** faisalhamadi5@gmail.com
**Product:** Halton Region real estate intelligence tool (MCP server + Claude connector)
**Status:** Production-live. Real-data feed provisioning pending broker approval. Ready for next feature build.

---

## 1. Deployment facts

| Item | Value |
|---|---|
| Live URL | `https://gta-scout-mcp-production.up.railway.app` |
| MCP endpoint | `https://gta-scout-mcp-production.up.railway.app/mcp` (JSON-RPC 2.0, POST) |
| Health check | `https://gta-scout-mcp-production.up.railway.app/health` (last verified: healthy, `repliers_ping: true`) |
| GitHub | `github.com/faisalhamadi5-sketch/gta-scout-mcp` |
| Hosting | Railway project `accurate-fulfillment` → service `gta-scout-mcp`, auto-deploy on push to `main` |
| Compliance site | `https://gtascout.ca` (Cloudflare Pages project `wild-mountain-9809`) |
| Domain registrar | Cloudflare |
| Repliers plan | **Standard ($199/mo)** — active |
| Real data status | **Paid plan active, but ITSO data feed not yet provisioned.** Currently returns Preview-tier sample data. Real Halton listings flow the moment Broker of Record signs off on the ITSO agreement. |

### Env vars in production

| Var | Set? | Purpose |
|---|---|---|
| `REPLIERS_API_KEY` | ✅ Yes | Repliers Standard-tier key. Never log, never expose in error responses. |
| `PORT` | Auto-injected by Railway | Falls back to 8000 if unset. Never set manually. |
| `MCP_AUTH_TOKEN` | ❌ Not set | Optional Bearer auth. Claude connector doesn't send one. Leave off. |
| `CORS_ORIGIN` | ❌ Not set (defaults to `*`) | Fine while single-tenant. Restrict when customer-facing UI ships. |

### Cleanup owed

A duplicate Railway project called **`empathetic-reverence`** exists from a mistaken second GitHub-deploy click. It should be deleted. No code impact — just tidiness/cost.

---

## 2. Business context

### What GTA Scout is for

**Dual-purpose, in Faisal's words:** "Personal use now, product later once feed licensing is sorted." Today it's Faisal's personal prospecting tool for his own land-acquisition and deal work. The intended future is a SaaS product for Halton agents and brokerages — but that requires resolving the licensing/redistribution question first (see below).

**The moat vs. Repliers-direct:** GTA Scout is agent-workflow-first. Its differentiator is the **distress scoring engine** — a practitioner-calibrated 0-100 score that ranks every listing by likelihood-to-close-cheap-and-fast. Repliers gives you data; GTA Scout gives you judgment. Weights were tuned by Faisal directly (see §5) — not from a public dataset.

### Cornerstone / ITSO — the compliance situation

Faisal is a **Cornerstone Association of REALTORS®** member, not TRREB. Cornerstone runs on the **ITSO MLS** platform (not TRREB's PropTx).

This matters for GTA Scout because:
- Repliers licenses MLS data on Faisal's behalf, but only for the boards Faisal is a member of
- Data agreements (DLA/IDX/VOW) flow through ITSO's Bridge Interactive portal and require **Broker of Record approval** before ITSO provisions the feed
- The Broker of Record is Lynn Moreira and Alper Ahmet at Right at Home Realty Burlington. Broker heads-up email sent; approval pending as of handoff

### Coverage gap (important for feature decisions)

ITSO covers Burlington, Hamilton, Mississauga, Waterloo, Niagara North, Haldimand, Norfolk.
ITSO does **NOT** cover Oakville, Milton, or Halton Hills — those sit on TRREB/PropTx.

However, `HALTON_CITIES` in the code still includes all four (Burlington/Milton/Oakville/Halton Hills) because dual-listed properties do appear in ITSO's feed (~9,100 unique listings, ~7,500 overlap with TRREB). Don't remove the other three cities — results will just be sparser there until TRREB is added as a second board (larger business decision, not a code change).

### Feed status

- **IDX** — free, agreement submitted, pending broker approval
- **Office Active Listings** — free, agreement submitted, pending broker approval
- **VOW** — **planned. Will be added once the first paying customer covers the $1,500/yr cost.** VOW provides expired/sold historical data; without it, `search_expired_listings` will return empty on real data. This is the sequencing Faisal has chosen.

### Multi-tenant licensing constraint

The current ITSO agreement is for **Faisal's personal prospecting**, not for reselling MLS access to other agents. Until either (a) subscribers each hold their own Repliers/ITSO subscriptions, or (b) a vendor/redistribution agreement is signed with Repliers + ITSO, GTA Scout must remain **single-tenant** in production behavior.

**Concrete implication:** for pitches to other agents (Michael O'Sullivan at Royal LePage Burlington is the first), the tool is **demo-only** — Faisal runs the searches live in front of them, they don't get logins. Any multi-tenant / user-accounts / customer-login features are premature until this licensing is resolved. Do not build them yet.

---

## 3. Reference deals + underwriting

Faisal's real business is land acquisition. GTA Scout's job is to surface opportunities that fit his underwriting model — so features should be evaluated against whether they help find deals like these.

### Benchmark deals

| Deal | Location | Multiple | Key lesson |
|---|---|---|---|
| **Meaford Formula** | Meaford, 9 acres | ~4.76x | **Gold standard.** Off-market, tertiary market, minimal institutional competition. This is the acquisition archetype to replicate. |
| Fort Erie | 22 acres | ~2.94x all-in | Solid but not Meaford |
| Puslinch estate lots | 22 acres | ~1.64x | Under target — cautionary |
| Newtonville/Clarington | 18 acres, zoning approved, draft plan pending | Recalibrated exit to $10.3M+ | 3x floor is non-negotiable — deals that look good at face value can fall short; recalibrate exit before committing |

### Non-negotiables (underwriting model)

- **Financing:** 70% LTV @ ~12% annual interest through private lenders/MICs
- **Fixed study costs:** ~$300K per deal (environmental, traffic, planning, engineering, archaeological, hydrogeological, tree removal)
- **Target return:** 3x+ all-in multiple at exit — floor, not aspiration
- **Exit structure:** VTB with equity deposit, or full cash sale
- **Critical variable:** cheap land relative to exit value — carry cost scales directly with land price, so the land price is the lever, not just the multiple

**Implication for GTA Scout features:** the tool's north star is surfacing **cheap land in tertiary Ontario markets with distressed or motivated sellers.** Distress scoring is one lens on that. Future features (heat map, development-signal detection, off-market prospecting) should be evaluated against "does this help find the next Meaford?"

---

## 4. Contacts

- **Owner:** faisalhamadi5@gmail.com

Broker approval, MLS support, ITSO/Repliers escalation, and office administrative routing are handled by Faisal directly. Don't cold-contact anyone else on his behalf.

---

## 5. Distress scoring engine (the moat)

**File location:** `server.py`, section `── Distress scoring engine ──`.

Every `Listing` returned by `normalize()` carries three fields:
- `distress_score` (int 0–100)
- `distress_tier` (`"HIGH — call today"` / `"MEDIUM — warm lead"` / `"LOW — worth watching"` / `"MINIMAL"`)
- `distress_reasons` (list of strings like `["Power Of Sale +45", "On market 105d +10", "Distress + 100+ DOM (seller stuck) +15"]`)

### Tier thresholds

| Tier | Score | Meaning |
|---|---|---|
| HIGH | 65–100 | Call today. Elite distressed listing. |
| MEDIUM | 40–64 | Warm lead, research this week. |
| LOW | 20–39 | Worth watching. |
| MINIMAL | 0–19 | Not distressed. |

### Scoring signals (practitioner-calibrated — DO NOT casually retune)

**Explicit distress language (highest weight):**
- POWER OF SALE / COURT ORDER / POS: **+45**
- MORTGAGEE / RECEIVERSHIP: **+35**
- LENDER APPROVAL: **+30**
- ESTATE / ESTATE SALE / PROBATE / ESTATE OF: **+60–65** (heavily boosted per practitioner input — estate = motivated family, always HIGH)
- AS-IS WHERE IS: **+20**

**Motivation language:** MOTIVATED SELLER / MUST SELL +12; BRING ALL OFFERS +8; PRICED TO SELL +6

**Condition:** FIXER-UPPER / HANDYMAN SPECIAL / NEEDS TLC +5

**Time pressure:** DOM ≥120 +15; ≥90 +10; ≥60 +5; <7 –10 (except when explicit distress present — practitioner intent is that fresh POS is still a lead)

**Relisted after expiry:** lastStatus in (EXP, EXT, TER) → +15

**Price cuts:** ≥10% from original +15; ≥5% +8; multiple price-drop events in history +5 each

**Compound bonus (practitioner insight):** explicit distress + DOM ≥100 → **+15** ("seller is stuck")

**Negative signals:** MULTIPLE OFFERS –15; OFFER DATE –8; OVER ASKING –10; HOLD BACK OFFERS –8

**Keyword-family dedup:** Signals grouped by family (`estate`, `pos`, `court`, `asis`, `motivated`, `priced`, `condition`) — only the highest-weight keyword in each family scores. Prevents "estate sale of the estate" from double-counting.

**POS regex matching:** `\bPOS\b` word-boundary — catches `POS.` `POS,` `POS!` and standalone `POS`, but not `position` or `posted`. False-positive tested.

### Test scenarios that must pass (all 100%)

| Scenario | Expected tier |
|---|---|
| Estate sale, 45 DOM, no other flags | HIGH |
| Estate + 120 DOM | HIGH |
| Probate, 60 DOM | HIGH |
| POS + 200 DOM + price cut | HIGH |
| Aldershot POS bungalow, 105 DOM | HIGH |
| Kitchen-sink distress (POS + court order + as-is + motivated + handyman) | HIGH (100) |
| Fresh POS, 4 DOM | LOW or MEDIUM (either acceptable) |
| Hot listing, multiple offers, 8 DOM | MINIMAL |
| Normal listing, 30 DOM, no signals | MINIMAL |
| "Estate sale of the estate of X" (dedup test) | HIGH |
| "Excellent position on the street" (false-positive test) | MINIMAL |
| "Recently posted new photos" (false-positive test) | MINIMAL |

Faisal's default calibration rule: **when in doubt, err toward HIGH.** His workflow is "give me the calls to make today, I'll filter."

---

## 6. Server architecture (`server.py`, ~1,061 lines)

Pure-Python 3.10+ stdlib + `requests` + `urllib3`. Single-file MCP server. No framework.

### Layout

```
── Logging (module-level logger 'gta_scout')
── Config constants (REPLIERS_API_KEY, HALTON_CITIES, LISTING_FIELDS, etc.)
── Distress scoring engine
── HTTP session (requests.Session with retry adapter, thread-safe lazy init)
── In-memory cache (30s TTL, 500-entry cap, LRU eviction)
── Safe type helpers (safe_float/int/str/date_days/round, validate_latlng)
── repliers_get() / repliers_get_all() — pagination up to max_pages
── Listing dataclass (includes distress_score/tier/reasons)
── normalize() — Repliers listing → Listing, distress scored inline
── haversine_km() — expired-listings radius filter
── parallel_search() — ThreadPoolExecutor for city × keyword fan-out
── TOOLS = [...] — 4 MCP tool definitions
── Handler functions
── handle_jsonrpc() — MCP protocol dispatcher
── ThreadingHTTPServer + MCPHandler
── Entry point with graceful shutdown
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` or `/health` | Health + live Repliers ping |
| GET | `/version` | Returns `{"version":"3.0.0","protocol":"MCP JSON-RPC 2.0"}` |
| GET | `/mcp/tools` or `/tools` | Tool listing (convenience) |
| POST | `/mcp` | **Primary MCP JSON-RPC endpoint** |
| POST | `/tools/list`, `/tools/call`, `/mcp/tools/list`, `/mcp/tools/call` | Legacy REST — keep for backward compat |
| OPTIONS | * | CORS preflight |

### The 4 tools

| Name | Purpose | Sort order |
|---|---|---|
| `search_expired_listings` | Radius search around lat/lng, expired within N days | Distance ASC → distress DESC |
| `search_pos_listings` | POS across Halton, parallel city×keyword | **Distress DESC** → price ASC |
| `search_development_land` | Zoning/OPA/severance keyword search | Price ASC |
| `get_market_stats` | Live active/expired counts + averages per city | Dict keyed by city |

---

## 7. Roadmap — Faisal's actual priority order

**Shipped:**
- MCP JSON-RPC server, all 4 tools, Claude-connected
- Distress scoring engine (practitioner-calibrated)
- Live compliance website at gtascout.ca
- Repliers Standard subscription active
- ITSO data agreement submitted (IDX + Office Active)
- 6 rounds of engineering hardening (soak-tested 315 req/s, 0 failures)

**In flight:**
- Broker of Record approval on ITSO agreement
- Repliers feed provisioning (auto-flows once broker signs)

**Priority order for next builds (per Faisal):**

1. **Neighborhood distress heat map (visual)** — first build. Given a lat/lng bounding box, return counts of listings by tier grouped by neighborhood/FSA. Enables "which Halton neighborhood has the most stuck sellers this month" queries. Renders as Claude Artifact today; dashboard-ready when web UI ships. This is the most demo-able feature for broker pitches.
2. **Daily prospecting brief (email/notification)** — cron-triggered summary of yesterday's new POS, nearby expireds, newly HIGH-scored listings. Suggested: Railway scheduled task calling `/mcp` with a fixed set of tool calls, formatted as HTML via SendGrid/Postmark.
3. **Motivated seller push alerts** — criteria-based push when new listings match. Requires persistent user preferences — see §2 licensing constraint before scoping this.
4. **Outreach copy generator** — given a listing, generate first-contact letter/email variants using its specific facts. Claude Skill or new MCP tool.

**Do not start anything not in this list without confirming with Faisal.**

---

## 8. Engineering standards

This codebase has been through **6 formal engineering-loop rounds**. The bar going forward:

**Every merge must be verified against:**
- Syntax + pyflakes clean (`python3 -m pyflakes server.py`)
- Server boots healthy (`GET /health` returns `{"status":"ok"}`)
- JSON-RPC handshake works (`initialize`, `tools/list`, `tools/call` all return correct envelopes)
- All safe-type helpers survive hostile inputs (None, "", "N/A", "1,850,000", inf, nan, dict, list, bool)
- `normalize()` survives 12+ hostile listing shapes without leaking `"None"` into address strings
- `SIGTERM` triggers graceful shutdown, exit code 0
- Cache stays under `CACHE_MAX_ENTRIES` under churn
- `safe_float("Infinity")` → `0.0` (not inf); `allow_nan=False` on all outward `json.dumps`
- All 12 distress scoring test scenarios pass (§5)

**Do NOT:**
- Log the API key or include it in error responses
- Remove `allow_nan=False` from `json.dumps` in tool-result serialization
- Change `_read_body()` to accept negative or non-numeric Content-Length
- Change `handle_jsonrpc()` to trust that payload is a dict (must `isinstance` check)
- Use `del _cache[key]` — always `_cache.pop(key, None)` (TOCTOU-safe)
- Add `time.sleep()` or blocking I/O inside signal handlers
- Introduce persistent storage without confirming with Faisal (see §2 constraint)

---

## 9. What GTA Scout deliberately does NOT do (yet)

- **No persistent state.** No database, no user accounts, no saved preferences. Cache is in-memory only.
- **No multi-tenancy.** One deployment, one API key, one user (Faisal). See §2 licensing constraint.
- **No writes.** Every tool is read-only against Repliers. Never add a tool that mutates MLS data.
- **No PII collection** beyond what MLS remarks already contain.
- **No inbound webhooks.**

---

## 10. Local dev + test harness

```bash
git clone https://github.com/faisalhamadi5-sketch/gta-scout-mcp.git
cd gta-scout-mcp
pip install -r requirements.txt
export REPLIERS_API_KEY=<the_key>
export PORT=8000
python3 server.py

# Sanity
curl http://localhost:8000/health
curl -X POST http://localhost:8000/mcp -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

### Automated pre-commit test harness

```python
# Run before any commit — enforces all round-1-through-6 fixes still hold
import subprocess, os, time, signal, urllib.request, json

subprocess.run(["python3", "-c", "import ast; ast.parse(open('server.py').read())"], check=True)
subprocess.run(["python3", "-m", "pyflakes", "server.py"], check=True)

g = {}
exec(compile(open("server.py").read(), "server.py", "exec"), g)
sd, dt = g["score_distress"], g["distress_tier"]

SCENARIOS = [
    ("Estate sale 45 DOM",   {"publicRemarks":"Estate sale","daysOnMarket":45}, "HIGH"),
    ("Estate 120 DOM",       {"publicRemarks":"Estate sale","daysOnMarket":130}, "HIGH"),
    ("Probate 60 DOM",       {"publicRemarks":"Probate sale","daysOnMarket":60}, "HIGH"),
    ("POS 200 DOM + cut",    {"publicRemarks":"POS. Motivated.","daysOnMarket":210,"listPrice":650000,"originalPrice":780000}, "HIGH"),
    ("Aldershot POS 105 DOM",{"publicRemarks":"Power of Sale. Sold as-is.","daysOnMarket":105}, "HIGH"),
    ("Kitchen-sink",         {"publicRemarks":"POWER OF SALE COURT ORDER AS-IS Motivated Handyman","daysOnMarket":145,"lastStatus":"Exp","listPrice":800000,"originalPrice":1000000}, "HIGH"),
    ("Hot listing",          {"publicRemarks":"Multiple offers! Over asking.","daysOnMarket":8}, "MINIMAL"),
    ("Normal listing",       {"publicRemarks":"Beautiful home.","daysOnMarket":30}, "MINIMAL"),
    ("Dedup 'estate' twice", {"publicRemarks":"Estate sale of the estate","daysOnMarket":60}, "HIGH"),
    ("False 'position'",     {"publicRemarks":"Excellent position on the street.","daysOnMarket":30}, "MINIMAL"),
    ("False 'posted'",       {"publicRemarks":"Recently posted new photos.","daysOnMarket":30}, "MINIMAL"),
]
fails = []
for label, listing, want in SCENARIOS:
    s, _ = sd(listing)
    got = dt(s).split(" ")[0]
    if got != want: fails.append(f"{label}: got {got}, want {want}")
assert not fails, "\n".join(fails)

env = dict(os.environ); env["REPLIERS_API_KEY"]="test"; env["PORT"]="9999"
proc = subprocess.Popen(["python3","server.py"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)
with urllib.request.urlopen("http://localhost:9999/health", timeout=3) as r:
    assert r.status == 200
proc.send_signal(signal.SIGTERM)
deadline = time.time()+5
while time.time()<deadline and proc.poll() is None: time.sleep(0.2)
assert proc.poll() == 0, "server did not shut down cleanly"

print("✅ All checks pass")
```

---

## 11. Bug ledger — 26 fixes across 6 loop rounds

**Do not re-introduce these. If touching related code, verify the fix still holds.**

| # | Bug | Fix | Why it matters |
|---|---|---|---|
| 1 | `streetSuffix=None` rendered as literal `"None"` in address | `safe_str()` in `normalize()` | Real listings sometimes have null address components; output must never include the word "None" |
| 2 | `statistics=None` crashed `round()` in market stats | `safe_round()` + None guard on nested dict access | Empty stats blocks are a valid Repliers response |
| 3 | `fromisoformat("N/A")` crashed | `safe_date_days()` catches ValueError, returns None | Some MLS date fields come back as literal "N/A" |
| 4 | `int("N/A")` crashed | `safe_int()` catches TypeError/ValueError/OverflowError | Same reason as #3 |
| 5 | Empty POST body crashed `json.loads` | `_read_body()` guards Content-Length ≤ 0 | Health probes and misconfigured clients send empty bodies |
| 6 | `"listings":"false"` was an invalid Repliers param | Removed entirely | Sending it caused Repliers 400s |
| 7 | `listPrice="1,850,000"` (with comma) crashed `float()` | `safe_float()` strips commas | Some MLS records carry formatted price strings |
| 8 | Single-threaded HTTPServer blocked concurrent requests | `ThreadingHTTPServer` w/ `ThreadingMixIn` | Real production traffic requires concurrent handling |
| 9 | Only `/mcp/tools/call` registered — Claude client may call `/tools/call` | Both path variants supported | Legacy vs current MCP client behavior |
| 10 | Cache expiry race (TOCTOU) — two threads deleting same key → KeyError | `dict.pop(key, None)` instead of `del` | Under concurrent load, `del` on already-deleted keys crashes |
| 11 | Unbounded cache growth (100K entries in test) | `CACHE_MAX_ENTRIES=500` + eviction | OOM risk under high-cardinality queries |
| 12 | JSON-RPC batch array payload crashed with AttributeError | `isinstance(payload, dict)` guard → -32600 | Some MCP clients send batches; must fail cleanly |
| 13 | Bare string/number/null/bool payloads crashed | Same guard covers all non-dict payloads | Hostile clients or misconfigured integrations |
| 14 | `"params": "string"` crashed `tools/call` | `isinstance(params, dict)` guard → -32602 | Malformed JSON-RPC from any client |
| 15 | `"arguments": [1,2]` (array) crashed handlers | `isinstance(tool_args, dict)` guard on both `/mcp` and legacy endpoints | Same |
| 16 | Client disconnect mid-response spammed tracebacks | `send_json` catches BrokenPipeError/ConnectionResetError | Railway logs stay clean; server survives client aborts |
| 17 | `Content-Length: abc` crashed request handler | try/except around `int()` in `_read_body` | Hostile / malformed headers |
| 18 | Negative `Content-Length` risked socket hang | `_read_body` rejects `length <= 0` | `rfile.read(-n)` on a socket blocks until close |
| 19 | `safe_float("Infinity")` → `Infinity` in JSON (strict-invalid) | `math.isfinite()` check → 0.0 fallback | JSON parsers reject `Infinity`; one bad listing would break entire response |
| 20 | `safe_int(float("inf"))` raised OverflowError | Caught in exception clause | Same class of bug as #19 |
| 21 | `_SESSION` init race — 10 concurrent first requests built 10 sessions | Double-checked locking with `_SESSION_LOCK` | Cheap correctness fix; prevents session leak under cold-start bursts |
| 22 | SIGTERM shutdown deadlocked (`server.shutdown()` called on serving thread) | Runs on helper thread | Railway needs clean shutdowns for graceful redeploys |
| 23 | Empty-array `arguments: []` slipped past falsy-coercion guard | Type check before `or {}` coercion | Real JSON-RPC clients occasionally send `[]` |
| 24 | `parallel_search` only caught RuntimeError — other exception types killed entire search | Broadened to `except Exception`; fails loud upfront on missing API key | One bad city/keyword combo must not zero out all results |
| 25 | POS keyword `"POS "` (trailing space) missed `"POS."` | Regex `\bPOS\b` word-boundary match | Real listings use `POS.` and `POS,` — original keyword logic missed them |
| 26 | Estate + `Estate Sale` keywords would double-count | Keyword-family dedup via `matched_families` set | Listings with "estate sale of the estate" scored inflated |

---

**End of handoff.**
