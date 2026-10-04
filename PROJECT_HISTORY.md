# Project History — SlideContracting

## What This Is

A headless EVE Online contract scraper + GitHub Pages frontend that displays alliance doctrine fits as a market board. The scraper runs on a **remote machine** (not this repo's directory), polls ESI for corporation contracts, classifies ship hulls, and pushes `contracts.json` to GitHub. The frontend (`index.html`) renders cards with ship images, prices, stock counts, and tech-level badges.

---

## Session Timeline

### 1. Bug Fix: Ship Size Classification

**Problem:** `main.py` classified ships incorrectly (Typhoon as Battlecruiser, Prophecy Navy as Capital) and produced "Unknown" entries due to:
- Wrong group mappings in `SHIP_GROUPS` (group 27 → 4 instead of 5, group 419 → 6 instead of 4)
- No pagination on ESI contract items endpoint (ship hulls sometimes on page 2+)
- No retry logic for transient ESI errors
- Write-once DB policy — bad records never healed

**Fixes:**
- Corrected 6 wrong group mappings, added Industrial tier (weight 7)
- Added `fetch_contract_items()` with pagination via `?page=N` and `x-pages` header
- Replaced sync `requests` with `httpx.AsyncClient`
- Added exponential-backoff retry on 429/502/503/504
- Added heal-by-title: when a contract gets correct classification, siblings with same title are updated
- Added re-evaluation policy: up to 50 existing bad contracts re-scanned per cycle

### 2. Frontend Redesign: Table → Cards

**Changes:**
- Replaced HTML table with CSS Grid card layout
- Cards show 128px ship render, fit name, price, stock count
- Clicking ship image opens random cheapest contract in-game via ESI
- Stats box in top-right: total ISK on market, total ship count
- Sort buttons: Name and Price, toggling asc/desc

### 3. Usability Features Added

| Feature | Implementation |
|---------|---------------|
| Live search | `<input>` filters cards by title substring |
| Class filter chips | Dynamically generated from data; stacks with search |
| Human-readable prices | `120.0M ISK`, `1.50B ISK` instead of raw integers |
| Last updated timestamp | `contracts.json` carries `"updated_at": "2025-07-24T21:30:00Z"` |
| Image error fallback | `onerror` handler shows transparent pixel instead of broken icon |

### 4. Data Stability & Deduplication

**Problem:** Same fit under different names ("Brawl Phoon v4" vs "Typhoon: Brawl Phoon v4") created duplicate cards. Same fit with failed ESI lookup created `(type_id: 0, class_weight: 99)` clones.

**Fixes:**
- Export groups by `type_id` (ship hull), not title
- Within each hull, clusters titles by substring similarity — merges variants
- Canonical title = most common; longest breaks ties
- Stability check: deep-compares old vs new `contracts.json`; skips git push if unchanged
- Deterministic ordering: sorted type IDs, sorted titles, sorted cheapest_ids, final sort by `(class_weight, title, type_id)`

### 5. Tech Level Badges

**Attempted (failed):** `race_id` from ESI type endpoint — field doesn't exist. Corporation logos via market group tree walking — not what user wanted.

**Final solution:** CSS badges driven by backend `tech_level` detection:
- `TECH_GROUPS` mapping: known T2/T3 groups map directly (`832` → `"t2"`, `963` → `"t3"`)
- Market group tree walk: `"Navy Faction"` → `"faction"`, `"Pirate Faction"` → `"pirate"`
- Frontend CSS: gold "II", purple "III", green triangle, red "P"

### 6. Auto-Update Mechanism

- Scraper `git pull origin main` before every push
- Compares HEAD before/after pull
- If `main.py` changed → `os.execv()` restarts the script with new code
- If data unchanged → skips git add/commit/push entirely

### 7. Titleless Contracts Now Show the Ship Name

**Problem:** Contracts posted with no title/description were silently dropped: the live-contract filter required a truthy `title`, so they never entered the DB or `contracts.json` and never appeared on the website.

**Fixes:**
- Dropped the `title` requirement from the live-contract filter (`item_exchange` + `outstanding` is enough)
- `classify_contract()` falls back to the ESI display name of the classified ship type when a contract has no title (e.g. a fitless "Rifter" contract shows as *Rifter*)
- Type names cached in `TYPE_META_CACHE` (`resolve_item_type` / `resolve_type_tech_level` already fetch the type endpoint; `get_type_name()` reuses the cache)
- Hardened the titleless case downstream: heal-by-title only applies when the title is non-empty (a titleless hull is not proof of its siblings' identity), and title-similarity clustering ignores empty norms so unnamed contracts cluster by hull instead of merging into the first sibling

### 8. Standard Fits: Canonical Names for Exact-Match Contracts

**Problem:** The same doctrine fit gets posted under many user-chosen titles, so one fit can appear on the board under several names.

**Fixes:**
- `standard_fits/` folder holds one `.eft` per standard fit (standard EVE fit export format: `[Ship, Fit Name]` header + item lines, `xN` quantities)
- If a contract's items **100% match** a stored fit (hull + every item, exact per-item totals), its title is replaced with the fit name from the first line of the `.eft` file — the user's contract title is ignored (see `standard_fits/README.md`)
- Matching compares name multisets: contract items are mapped type_id → display name, and the resulting `Counter` must equal the fit's expected `Counter`. Any unknown type_id aborts the match (keeps user title)
- Item-name → type-ID translation uses the fuzzwork `invTypes.csv` dump instead of hammering ESI per name: auto-downloaded on first run, refreshed when older than a week, streamed to a temp file + atomic replace, old file kept on failure, git-ignored
- Matching is by total per item name, so split stacks (2×500 vs 1×1000) still count as a 100% match — consistent with the EFT format, which has no stack concept
- New `fit_version` DB column = SHA-1 digest of the fit library; when fits are added/edited and pulled, all live contracts are re-evaluated (cap raised from 50 to 200/cycle) so already-listed contracts get renamed automatically
- EFT header parsing: ship name before the first comma (hull, qty 1), fit name after; item lines parsed with optional `xN` suffix, duplicate lines summed

---

## Architecture

```
Remote machine (headless)
  └── main.py ──► ESI ──► contracts.db ──► contracts.json ──► git push
                                                    │
                                                    ▼
GitHub Pages (browser)                              GitHub repo
  └── index.html ◄── fetch ──► contracts.json
```

**Key files:**
- `main.py` — scraper, classifier, exporter, git sync
- `standard_fits/` — one `.eft` per standard fit + auto-managed `invTypes.csv` (fuzzwork dump, weekly refresh)
- `index.html` — card grid frontend, SSO login, ESI window opener
- `configuration.py` — secrets (ignored by git)
- `contracts.json` — generated data with `{"updated_at", "contracts": [...]}`
- `DEPLOYMENT.md` — notes about remote execution context

---

## ESI Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `GET /corporations/{id}/contracts/` | List active contracts |
| `GET /corporations/{id}/contracts/{id}/items/?page=N` | Get contract items (paginated) |
| `GET /universe/types/{id}/` | Resolve item type → group_id, market_group_id |
| `GET /universe/groups/{id}/` | Resolve group → category_id (ship check) |
| `GET /markets/groups/{id}/` | Walk market tree for faction detection |
| `POST /ui/openwindow/contract/` | Open contract in-game (frontend) |
| `POST /v2/oauth/token` | PKCE token exchange (frontend) |
| `GET fuzzwork.co.uk/dump/latest/csv/invTypes.csv` | Full type dump: item names → type_ids for standard-fit matching (weekly) |

---

## Known Limitations

- `type_id == 0` contracts (ESI lookup failures) still show no ship image; only stock/price visible
- Pirate faction ships show "P" badge instead of the in-game skull logo
- No client-side caching of `contracts.json` (re-fetches on every page load)
- GitHub Pages CDN can cache `index.html` for 5–10 minutes after push

---

## Tooling Notes

- **Backend:** Python 3, `httpx` (async HTTP), `sqlite3`
- **Frontend:** Vanilla JS, no framework, CSS Grid
- **CI/CD:** None — scraper handles its own git push
- **Hosting:** GitHub Pages (static frontend), remote headless host (scraper)
