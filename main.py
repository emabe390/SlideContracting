import asyncio
import datetime
import sqlite3
import traceback
import json
import subprocess
import sys
import httpx

# --- CONFIGURATION IMPORT ---
# Stripped out frontend variables, only importing what the headless backend needs
from configuration import (
    DEBUG, 
    BACKEND_CLIENT_ID, 
    BACKEND_CLIENT_SECRET, 
    DIRECTOR_REFRESH_TOKEN, 
    DIRECTOR_CORPORATION_ID
)

# --- LOCAL CACHE ---
# Stores {type_id: (is_ship_boolean, class_weight)} so we don't spam ESI
TYPE_CACHE = {}
GROUP_CACHE = {}  # {group_id: (category_id, name)}

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect("contracts.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS contracts 
                 (contract_id INTEGER PRIMARY KEY, title TEXT, price REAL, issuer_id INTEGER, type_id INTEGER, class_weight INTEGER)''')
    conn.commit()
    conn.close()

# --- ESI AUTHENTICATION (BACKEND) ---
async def get_director_access_token(refresh_token, client: httpx.AsyncClient):
    url = "https://login.eveonline.com/v2/oauth/token"
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    auth = (BACKEND_CLIENT_ID, BACKEND_CLIENT_SECRET)
    try:
        res = await client.post(url, data=data, auth=auth)
        res.raise_for_status()
        return res.json().get("access_token")
    except Exception as e:
        print(f"[ERROR] Failed to get director access token: {e}")
        return None

# --- SHIP GROUP MAPPING ---
# Weights:
#   1 = Frigate      2 = Destroyer     3 = Cruiser
#   4 = Battlecruiser               5 = Battleship
#   6 = Capital                     7 = Industrial/Freighter
SHIP_GROUPS = {
    # Frigates (Weight 1)
    25: 1,    # Frigate
    237: 1,   # Corvette
    324: 1,   # Assault Frigate
    830: 1,   # Covert Ops
    831: 1,   # Interceptor
    834: 1,   # Stealth Bomber
    893: 1,   # Electronic Attack Ship
    1283: 1,  # Expedition Frigate
    1527: 1,  # Logistics Frigate
    # Destroyers (Weight 2)
    420: 2,   # Destroyer
    541: 2,   # Interdictor
    1305: 2,  # Tactical Destroyer
    1534: 2,  # Command Destroyer
    # Cruisers (Weight 3)
    26: 3,    # Cruiser
    358: 3,   # Heavy Assault Cruiser
    832: 3,   # Logistics
    833: 3,   # Force Recon Ship
    894: 3,   # Heavy Interdiction Cruiser
    906: 3,   # Combat Recon Ship
    963: 3,   # Strategic Cruiser
    1972: 3,  # Flag Cruiser
    # Battlecruisers (Weight 4)
    419: 4,   # Combat Battlecruiser
    1201: 4,  # Attack Battlecruiser
    # Battleships (Weight 5)
    27: 5,    # Battleship
    898: 5,   # Black Ops (T2 Battleship)
    900: 5,   # Marauder
    # Capitals (Weight 6)
    30: 6,    # Titan
    485: 6,   # Dreadnought
    547: 6,   # Carrier
    659: 6,   # Supercarrier
    1538: 6,  # Force Auxiliary
    # Industrial / Freighter (Weight 7)
    28: 7,    # Hauler
    513: 7,   # Freighter
    883: 7,   # Capital Industrial Ship
    902: 7,   # Jump Freighter
    1202: 7,  # Blockade Runner
}

async def esi_get_with_retry(client: httpx.AsyncClient, url: str, headers=None, max_retries=3):
    """GET with exponential backoff for transient ESI errors."""
    for attempt in range(max_retries):
        try:
            res = await client.get(url, headers=headers or {}, timeout=15.0)
            # Rate limit or transient server errors → retry
            if res.status_code in (429, 502, 503, 504):
                backoff = 2 ** attempt
                # Honor Retry-After if present
                retry_after = res.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    backoff = int(retry_after)
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    continue
            return res
        except Exception:
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise
    return res


def derive_weight_from_group_name(name: str) -> int:
    """Derive a class_weight from an EVE group name for unknown ship groups."""
    name_lower = name.lower()
    if any(k in name_lower for k in ("frigate", "corvette", "expedition")):
        return 1
    if any(k in name_lower for k in ("destroyer", "dictor", "tactical destroyer", "command destroyer")):
        return 2
    if "cruiser" in name_lower:
        return 3
    if "battlecruiser" in name_lower:
        return 4
    if any(k in name_lower for k in ("battleship", "marauder", "black ops")):
        return 5
    if any(k in name_lower for k in ("titan", "dreadnought", "carrier", "supercarrier", "force auxiliary", "capital")):
        return 6
    if any(k in name_lower for k in ("freighter", "hauler", "industrial", "blockade runner", "jump freighter")):
        return 7
    return 99


async def resolve_item_type(type_id, client: httpx.AsyncClient):
    if type_id in TYPE_CACHE:
        return TYPE_CACHE[type_id]

    try:
        res_type = await esi_get_with_retry(
            client,
            f"https://esi.evetech.net/latest/universe/types/{type_id}/"
        )
        if res_type.status_code >= 500:
            return (False, 99)  # transient, don't cache
        if res_type.status_code == 404:
            TYPE_CACHE[type_id] = (False, 99)
            return (False, 99)
        if res_type.status_code != 200:
            TYPE_CACHE[type_id] = (False, 99)
            return (False, 99)

        type_data = res_type.json()
        group_id = type_data.get("group_id")

        if group_id in SHIP_GROUPS:
            weight = SHIP_GROUPS[group_id]
            TYPE_CACHE[type_id] = (True, weight)
            return (True, weight)

        # Check group cache first
        if group_id in GROUP_CACHE:
            category_id, group_name = GROUP_CACHE[group_id]
            if category_id == 6:
                weight = derive_weight_from_group_name(group_name)
                TYPE_CACHE[type_id] = (True, weight)
                return (True, weight)
            TYPE_CACHE[type_id] = (False, 99)
            return (False, 99)

        res_group = await esi_get_with_retry(
            client,
            f"https://esi.evetech.net/latest/universe/groups/{group_id}/"
        )
        if res_group.status_code >= 500:
            return (False, 99)
        if res_group.status_code == 200:
            gdata = res_group.json()
            category_id = gdata.get("category_id")
            group_name = gdata.get("name", "")
            GROUP_CACHE[group_id] = (category_id, group_name)
            if category_id == 6:
                weight = derive_weight_from_group_name(group_name)
                TYPE_CACHE[type_id] = (True, weight)
                return (True, weight)

        TYPE_CACHE[type_id] = (False, 99)
        return (False, 99)

    except Exception:
        return (False, 99)


async def fetch_contract_items(client: httpx.AsyncClient, corp_id, contract_id, headers):
    """Fetch all pages of contract items from ESI with retry."""
    all_items = []
    page = 1
    while True:
        url = (
            f"https://esi.evetech.net/latest/corporations/{corp_id}/"
            f"contracts/{contract_id}/items/?page={page}"
        )
        try:
            res = await esi_get_with_retry(client, url, headers=headers)
            if res.status_code != 200:
                return None
            data = res.json()
            if not isinstance(data, list):
                return None
            all_items.extend(data)
            x_pages = int(res.headers.get("x-pages", "1"))
            if page >= x_pages:
                break
            page += 1
        except Exception:
            return None
    return all_items


async def classify_contract(client, corp_id, contract_id, headers, active_contracts):
    """Classify a single contract and return its DB row tuple."""
    contract = active_contracts[contract_id]
    price = contract["price"]
    issuer_id = contract["issuer_id"]
    title = contract["title"].strip()

    items = await fetch_contract_items(client, corp_id, contract_id, headers)

    ship_type_id = 0
    class_weight = 99
    fallback_candidate = 0

    if items is not None:
        # Heuristic: assembled ship hulls are singletons with qty 1.
        items.sort(
            key=lambda x: (not x.get("is_singleton", False), x.get("quantity", 1))
        )

        for item in items:
            tid = item["type_id"]
            if fallback_candidate == 0:
                fallback_candidate = tid

            is_ship, weight = await resolve_item_type(tid, client)
            if is_ship:
                ship_type_id = tid
                class_weight = weight
                break

    if ship_type_id == 0 and fallback_candidate > 0:
        ship_type_id = fallback_candidate

    return (contract_id, title, price, issuer_id, ship_type_id, class_weight)


# --- BACKGROUND SCRAPER ENGINE ---
async def scrape_contracts():
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    timeout = httpx.Timeout(15.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        while True:
            try:
                print("\n" + "="*50)
                print("[SCRAPER] Starting Smart Sync Cycle...")
                print("="*50)

                token = await get_director_access_token(DIRECTOR_REFRESH_TOKEN, client)
                if not token:
                    print("[ERROR] No valid token. Skipping this cycle.")
                    await asyncio.sleep(60)
                    continue

                headers = {"Authorization": f"Bearer {token}"}

                # 1. Fetch active contracts
                url = f"https://esi.evetech.net/latest/corporations/{DIRECTOR_CORPORATION_ID}/contracts/"
                res = await client.get(url, headers=headers)

                if res.status_code != 200:
                    print(f"[ERROR] ESI returned status code {res.status_code}")
                    await asyncio.sleep(60)
                    continue

                raw_contracts = res.json()
                active_contracts = {
                    c.get("contract_id"): c
                    for c in raw_contracts
                    if c.get("type") == "item_exchange" and c.get("status") == "outstanding" and c.get("title")
                }

                # 2. Check local database
                conn = sqlite3.connect("contracts.db")
                c = conn.cursor()
                c.execute("SELECT contract_id, type_id, class_weight FROM contracts")
                db_rows = c.fetchall()
                existing_ids = {row[0] for row in db_rows}
                live_ids = set(active_contracts.keys())

                # 3. Clean up dead contracts
                dead_ids = existing_ids - live_ids
                if dead_ids:
                    print(f"[SCRAPER] Removing {len(dead_ids)} dead contracts from DB.")
                    for d_id in dead_ids:
                        c.execute("DELETE FROM contracts WHERE contract_id = ?", (d_id,))

                # 4. Find new contracts
                new_ids = list(live_ids - existing_ids)
                print(f"[SCRAPER] Found {len(new_ids)} brand new contracts to evaluate.")

                # 5. Find stale / bad existing contracts to re-evaluate
                bad_existing = [
                    row[0] for row in db_rows
                    if row[0] in live_ids and (row[1] == 0 or row[2] == 99)
                ]
                REEVAL_LIMIT = 50
                ids_to_reeval = bad_existing[:REEVAL_LIMIT]
                if ids_to_reeval:
                    print(f"[SCRAPER] Re-evaluating {len(ids_to_reeval)} existing contracts with bad classification.")

                # 6. Process batches
                BATCH_SIZE = 200
                ids_to_process = new_ids[:BATCH_SIZE]
                total_to_scan = ids_to_process + ids_to_reeval

                if total_to_scan:
                    print(f"[SCRAPER] Fetching item details for batch of {len(total_to_scan)} contracts...")
                    for index, c_id in enumerate(total_to_scan, 1):
                        row = await classify_contract(
                            client, DIRECTOR_CORPORATION_ID, c_id, headers, active_contracts
                        )

                        if c_id in existing_ids:
                            # UPDATE existing record
                            c.execute(
                                "UPDATE contracts SET title=?, price=?, issuer_id=?, type_id=?, class_weight=? WHERE contract_id=?",
                                (row[1], row[2], row[3], row[4], row[5], row[0])
                            )
                        else:
                            # INSERT new record
                            c.execute(
                                "INSERT INTO contracts VALUES (?, ?, ?, ?, ?, ?)",
                                row
                            )

                        if index % 5 == 0:
                            print(f"  -> {index}/{len(total_to_scan)} processed in this batch...")
                            await asyncio.sleep(0.2)
                else:
                    print("[SCRAPER] Database is fully up to date with EVE ESI.")

                conn.commit()

                # --- 7. EXPORT ENTIRE DATABASE TO JSON AND PUSH ---
                c.execute("SELECT title, type_id, class_weight, price, contract_id FROM contracts")

                grouped_contracts = {}
                for r in c.fetchall():
                    title, type_id, class_weight, price, contract_id = r
                    key = (title, type_id, class_weight)

                    if key not in grouped_contracts:
                        grouped_contracts[key] = []
                    grouped_contracts[key].append({"price": price, "id": contract_id})

                export_data = []
                for key, contracts in grouped_contracts.items():
                    min_price = min(c["price"] for c in contracts)
                    max_price = max(c["price"] for c in contracts)

                    cheapest_ids = [c["id"] for c in contracts if c["price"] == min_price]

                    export_data.append({
                        "title": key[0],
                        "type_id": key[1],
                        "class_weight": key[2],
                        "stock": len(contracts),
                        "min_price": min_price,
                        "max_price": max_price,
                        "cheapest_ids": cheapest_ids
                    })

                output = {
                    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                    "contracts": export_data
                }

                with open("contracts.json", "w") as json_file:
                    json.dump(output, json_file)

                print(f"[SCRAPER] Saved {len(export_data)} doctrine types to contracts.json. Syncing with GitHub...")

                try:
                    subprocess.run(["git", "add", "contracts.json"], check=True, timeout=15)
                    commit_result = subprocess.run(
                        ["git", "commit", "-m", "Automated contract sync update"],
                        capture_output=True, text=True
                    )

                    if commit_result.returncode == 0:
                        subprocess.run(["git", "push", "origin", "main"], check=True, timeout=15)
                        print("[SCRAPER] Git Push successful. GitHub Pages is updating!")
                    else:
                        print("[SCRAPER] No price or stock changes detected. Skipped Git push to save bandwidth.")

                except subprocess.TimeoutExpired:
                    print("[WARNING] Git push timed out! GitHub might be slow. Will try again next cycle.")
                except Exception as e:
                    print(f"[ERROR] Unexpected Git error: {e}")

                conn.close()

                # --- 8. DYNAMIC SLEEP PACING ---
                backlog = len(new_ids) - BATCH_SIZE
                if backlog > 0:
                    print(f"[SCRAPER] Still {backlog} contracts in backlog. Sleeping 10 seconds before next batch...")
                    await asyncio.sleep(10)
                else:
                    print("[SCRAPER] Cycle complete. Sleeping for 15 minutes.\n")
                    for i in range(15):
                        await asyncio.sleep(60)
                        print(f"[SCRAPER] Waiting {15-i} more minutes")

            except asyncio.CancelledError:
                print("\n[SERVER] Shutdown signal received. Exiting scraper safely.")
                break
            except Exception as e:
                print(f"\n[CRITICAL ERROR] The background scraper crashed: {e}")
                traceback.print_exc()
                print("[SERVER] Restarting script in 60 seconds...")
                await asyncio.sleep(60)

# --- SCRIPT ENTRY POINT ---
if __name__ == "__main__":
    print("[SYSTEM] Initializing Headless EVE Contract Scraper...")
    init_db()

    try:
        asyncio.run(scrape_contracts())
    except KeyboardInterrupt:
        print("\n[SYSTEM] Manual shutdown requested. Goodbye! o7")
        sys.exit(0)
