import asyncio
import sqlite3
import requests
import traceback
import json
import subprocess
import sys

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

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect("contracts.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS contracts 
                 (contract_id INTEGER PRIMARY KEY, title TEXT, price REAL, issuer_id INTEGER, type_id INTEGER, class_weight INTEGER)''')
    conn.commit()
    conn.close()

# --- ESI AUTHENTICATION (BACKEND) ---
def get_director_access_token(refresh_token):
    url = "https://login.eveonline.com/v2/oauth/token"
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    auth = (BACKEND_CLIENT_ID, BACKEND_CLIENT_SECRET)
    try:
        res = requests.post(url, data=data, auth=auth)
        res.raise_for_status()
        return res.json().get("access_token")
    except Exception as e:
        print(f"[ERROR] Failed to get director access token: {e}")
        return None

# --- EXTENSIVE GROUP MAPPING ---
SHIP_GROUPS = {
    # Frigates (Weight 1)
    25: 1, 324: 1, 830: 1, 831: 1, 834: 1, 893: 1, 1283: 1, 1527: 1,
    # Destroyers (Weight 2)
    420: 2, 541: 2, 1305: 2, 1534: 2,
    # Cruisers (Weight 3)
    26: 3, 358: 3, 832: 3, 833: 3, 894: 3, 906: 3, 963: 3, 1972: 3,
    # Battlecruisers (Weight 4)
    27: 4, 898: 4, 1201: 4,
    # Battleships (Weight 5)
    28: 5, 899: 5, 900: 5, 1202: 5,
    # Capitals / Industrial Freighters (Weight 6)
    30: 6, 419: 6, 485: 6, 513: 6, 547: 6, 659: 6, 883: 6, 902: 6, 1538: 6
}

def resolve_item_type(type_id):
    if type_id in TYPE_CACHE:
        return TYPE_CACHE[type_id]
        
    try:
        res_type = requests.get(f"https://esi.evetech.net/latest/universe/types/{type_id}/")
        if res_type.status_code != 200:
            return (False, 99)
            
        type_data = res_type.json()
        group_id = type_data.get("group_id")
        
        if group_id in SHIP_GROUPS:
            weight = SHIP_GROUPS[group_id]
            TYPE_CACHE[type_id] = (True, weight)
            return (True, weight)
            
        res_group = requests.get(f"https://esi.evetech.net/latest/universe/groups/{group_id}/")
        if res_group.status_code == 200:
            category_id = res_group.json().get("category_id")
            if category_id == 6:
                TYPE_CACHE[type_id] = (True, 3) 
                return (True, 3)
                
        TYPE_CACHE[type_id] = (False, 99)
        return (False, 99)
            
    except Exception:
        return (False, 99)

# --- BACKGROUND SCRAPER ENGINE ---
async def scrape_contracts():
    # Outer loop ensures if a critical error happens, it recovers and keeps running indefinitely
    while True:
        try:
            print("\n" + "="*50)
            print("[SCRAPER] Starting Smart Sync Cycle...")
            print("="*50)
            
            token = get_director_access_token(DIRECTOR_REFRESH_TOKEN)
            if not token:
                print("[ERROR] No valid token. Skipping this cycle.")
                await asyncio.sleep(60)
                continue
                
            headers = {"Authorization": f"Bearer {token}"}
            
            # 1. Fetch active contracts
            url = f"https://esi.evetech.net/latest/corporations/{DIRECTOR_CORPORATION_ID}/contracts/"
            res = requests.get(url, headers=headers)
            
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
            c.execute("SELECT contract_id FROM contracts")
            existing_ids = set([row[0] for row in c.fetchall()])
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

            # 5. Process batches
            BATCH_SIZE = 200
            ids_to_process = new_ids[:BATCH_SIZE]

            if ids_to_process:
                print(f"[SCRAPER] Fetching item details for batch of {len(ids_to_process)} contracts...")
                for index, c_id in enumerate(ids_to_process, 1):
                    contract = active_contracts[c_id]
                    price = contract["price"]
                    issuer_id = contract["issuer_id"]
                    title = contract["title"].strip()
                    
                    items_url = f"https://esi.evetech.net/latest/corporations/{DIRECTOR_CORPORATION_ID}/contracts/{c_id}/items/"
                    items_res = requests.get(items_url, headers=headers)
                    
                    ship_type_id = 0
                    class_weight = 99
                    fallback_candidate = 0
                    
                    if items_res.status_code == 200:
                        items = items_res.json()
                        items = sorted(items, key=lambda x: x.get("quantity", 1))
                        
                        for item in items:
                            tid = item["type_id"]
                            if fallback_candidate == 0:
                                fallback_candidate = tid
                                
                            is_ship, weight = resolve_item_type(tid)
                            if is_ship:
                                ship_type_id = tid
                                class_weight = weight
                                break

                    if ship_type_id == 0 and fallback_candidate > 0:
                        ship_type_id = fallback_candidate
                        
                    c.execute("INSERT INTO contracts VALUES (?, ?, ?, ?, ?, ?)", 
                              (c_id, title, price, issuer_id, ship_type_id, class_weight))
                    
                    if index % 5 == 0:
                        print(f"  -> {index}/{len(ids_to_process)} processed in this batch...")
                        await asyncio.sleep(0.2)
            else:
                print("[SCRAPER] Database is fully up to date with EVE ESI.")

            conn.commit()

            # --- 6. EXPORT ENTIRE DATABASE TO JSON AND PUSH ---
            c.execute("SELECT title, type_id, class_weight, COUNT(*), MIN(price), MAX(price), MIN(contract_id) FROM contracts GROUP BY title, type_id, class_weight")
            
            export_data = []
            for r in c.fetchall():
                export_data.append({
                    "title": r[0], 
                    "type_id": r[1], 
                    "class_weight": r[2], 
                    "stock": r[3], 
                    "min_price": r[4], 
                    "max_price": r[5], 
                    "cheapest_id": r[6]
                })
            
            with open("contracts.json", "w") as json_file:
                json.dump(export_data, json_file)
                
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

            # --- 7. DYNAMIC SLEEP PACING ---
            if len(new_ids) > BATCH_SIZE:
                print(f"[SCRAPER] Still {len(new_ids) - BATCH_SIZE} contracts in backlog. Sleeping 10 seconds before next batch...")
                await asyncio.sleep(10)
            else:
                print("[SCRAPER] Cycle complete. Sleeping for 15 minutes.\n")
                await asyncio.sleep(900)
            
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
        # Fires up the async loop natively without needing FastAPI/Uvicorn
        asyncio.run(scrape_contracts())
    except KeyboardInterrupt:
        print("\n[SYSTEM] Manual shutdown requested. Goodbye! o7")
        sys.exit(0)