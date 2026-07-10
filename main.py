import asyncio
import sqlite3
import requests
import traceback
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
import urllib.parse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.mount("/static", StaticFiles(directory="img"), name="static")

# --- CORS CONFIGURATION ---
# Replace this with your actual GitHub Pages URL so only your site can access it
origins = [
    "https://emabe390.github.io", 
    "http://localhost:8000", # Good for local testing
    "http://localhost:801",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Change to origins list above later if you want strict security!
    allow_credentials=True,
    allow_methods=["*"], # Allows GET, POST, etc.
    allow_headers=["*"], # Allows all headers
)

# --- CONFIGURATION IMPORT ---
from configuration import (
    DEBUG, 
    BACKEND_CLIENT_ID, 
    BACKEND_CLIENT_SECRET, 
    DIRECTOR_REFRESH_TOKEN, 
    DIRECTOR_CORPORATION_ID, 
    FRONTEND_CLIENT_ID, 
    FRONTEND_CLIENT_SECRET, 
    CALLBACK_URL
)

scraper_task = None

# --- LOCAL CACHE ---
# Stores {type_id: (is_ship_boolean, class_weight)} so we don't spam ESI
TYPE_CACHE = {}

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect("contracts.db")
    c = conn.cursor()
    # Added type_id and class_weight to the database
    c.execute('''CREATE TABLE IF NOT EXISTS contracts 
                 (contract_id INTEGER PRIMARY KEY, title TEXT, price REAL, issuer_id INTEGER, type_id INTEGER, class_weight INTEGER)''')
    conn.commit()
    conn.close()

init_db()

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
# Expanded list of EVE Online Ship Group IDs to avoid 'Unknown' tags on complex hulls
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
    """Hits ESI to check if a type_id is a ship, and caches the result accurately."""
    if type_id in TYPE_CACHE:
        return TYPE_CACHE[type_id]
        
    try:
        res_type = requests.get(f"https://esi.evetech.net/latest/universe/types/{type_id}/")
        if res_type.status_code != 200:
            return (False, 99)
            
        type_data = res_type.json()
        group_id = type_data.get("group_id")
        
        # Method A: Check our pre-defined exhaustive Group ID index
        if group_id in SHIP_GROUPS:
            weight = SHIP_GROUPS[group_id]
            TYPE_CACHE[type_id] = (True, weight)
            return (True, weight)
            
        # Method B: Fallback verification via ESI Category endpoint
        res_group = requests.get(f"https://esi.evetech.net/latest/universe/groups/{group_id}/")
        if res_group.status_code == 200:
            category_id = res_group.json().get("category_id")
            if category_id == 6:  # Category 6 is explicitly 'Ship'
                TYPE_CACHE[type_id] = (True, 3) # Default weight to Cruiser baseline if a rare group
                return (True, 3)
                
        # Not a ship
        TYPE_CACHE[type_id] = (False, 99)
        return (False, 99)
            
    except Exception:
        return (False, 99)

# --- BACKGROUND SCRAPER (SMART DELTA SYNC) ---
async def scrape_contracts():
    try:
        while True:
            print("\n" + "="*50)
            print("[SCRAPER] Starting Smart Sync Cycle...")
            print("="*50)
            
            token = get_director_access_token(DIRECTOR_REFRESH_TOKEN)
            if not token:
                print("[ERROR] No valid token. Skipping this cycle.")
                await asyncio.sleep(60)
                continue
                
            headers = {"Authorization": f"Bearer {token}"}
            
            # 1. Fetch ALL currently active contracts (One fast call)
            url = f"https://esi.evetech.net/latest/corporations/{DIRECTOR_CORPORATION_ID}/contracts/"
            res = requests.get(url, headers=headers)
            
            if res.status_code != 200:
                print(f"[ERROR] ESI returned status code {res.status_code}")
                await asyncio.sleep(60)
                continue
                
            raw_contracts = res.json()
            
            # Build a dictionary of valid active contracts
            active_contracts = {
                c.get("contract_id"): c 
                for c in raw_contracts 
                if c.get("type") == "item_exchange" and c.get("status") == "outstanding" and c.get("title")
            }

            # 2. Check what we ALREADY have in our local database
            conn = sqlite3.connect("contracts.db")
            c = conn.cursor()
            c.execute("SELECT contract_id FROM contracts")
            existing_ids = set([row[0] for row in c.fetchall()])
            live_ids = set(active_contracts.keys())

            # 3. Remove contracts that have been completed, sold, or cancelled
            dead_ids = existing_ids - live_ids
            if dead_ids:
                print(f"[SCRAPER] Removing {len(dead_ids)} dead contracts from DB.")
                for d_id in dead_ids:
                    c.execute("DELETE FROM contracts WHERE contract_id = ?", (d_id,))

            # 4. Find completely NEW contracts to process
            new_ids = list(live_ids - existing_ids)
            print(f"[SCRAPER] Found {len(new_ids)} brand new contracts to evaluate.")

            # 5. Process a safe BATCH of new contracts to avoid timeouts
            BATCH_SIZE = 200
            ids_to_process = new_ids[:BATCH_SIZE]

            if ids_to_process:
                print(f"[SCRAPER] Fetching item details for batch of {len(ids_to_process)} contracts...")
                for index, c_id in enumerate(ids_to_process, 1):
                    contract = active_contracts[c_id]
                    price = contract["price"]
                    issuer_id = contract["issuer_id"]
                    title = contract["title"].strip()
                    
                    # Fetch items (This is the slow part, which is why we batch it)
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
                        await asyncio.sleep(0.2) # Yield to prevent UI freezing
                print("[SCRAPER] Synchronization done!")
            else:
                print("[SCRAPER] Database is fully up to date with EVE ESI.")

            # Save the DB changes
            conn.commit()

            # --- 6. ONLY EXPORT AND PUSH IF SOMETHING CHANGED ---
            if dead_ids or ids_to_process:
                c.execute("SELECT title, type_id, class_weight, COUNT(*), MIN(price), MAX(price), MIN(contract_id) FROM contracts GROUP BY title, type_id, class_weight")
                export_data = [{"title": r[0], "type_id": r[1], "class_weight": r[2], "stock": r[3], "min_price": r[4], "max_price": r[5], "cheapest_id": r[6]} for r in c.fetchall()]
                
                with open("contracts.json", "w") as json_file:
                    json.dump(export_data, json_file)
                    
                print("[SCRAPER] Exported updated contracts.json. Pushing to GitHub...")
                try:
                    # Using subprocess with a 15-second timeout so it CANNOT freeze the server
                    subprocess.run(["git", "add", "contracts.json"], check=True, timeout=15)
                    
                    # We allow this to fail silently if there are no changes to commit
                    subprocess.run(["git", "commit", "-m", "Automated contract sync update"], check=False)
                    
                    subprocess.run(["git", "push", "origin", "main"], check=True, timeout=15)
                    print("[SCRAPER] GitHub Repository sync complete.")
                except subprocess.TimeoutExpired:
                    print("[WARNING] Git push timed out! GitHub might be slow. Will try again next cycle.")
                except subprocess.CalledProcessError as e:
                    print(f"[WARNING] Git command failed. (This is normal if there were no new changes to push).")
                except Exception as e:
                    print(f"[ERROR] Unexpected Git error: {e}")

            conn.close()

            # --- 7. DYNAMIC SLEEP PACING ---
            if len(new_ids) > BATCH_SIZE:
                print(f"[SCRAPER] Still {len(new_ids) - BATCH_SIZE} contracts in backlog. Sleeping 10 seconds before next batch...")
                await asyncio.sleep(10)
            else:
                print("[SCRAPER] Cycle complete. Sleeping for 5 minutes.\n")
                await asyncio.sleep(300)
            
    except asyncio.CancelledError:
        print("\n[SERVER] Shutdown signal received. Scraper task cancelled safely.")

@app.on_event("startup")
async def startup_event():
    global scraper_task
    scraper_task = asyncio.create_task(scrape_contracts())

@app.on_event("shutdown")
async def shutdown_event():
    global scraper_task
    if scraper_task:
        scraper_task.cancel()

# --- API ENDPOINTS (FRONTEND) ---
@app.get("/api/contracts")
def get_contracts():
    conn = sqlite3.connect("contracts.db")
    c = conn.cursor()
    # We now group by BOTH title and type_id. This isolates incorrect ships!
    c.execute("SELECT title, type_id, class_weight, COUNT(*), MIN(price), MAX(price), MIN(contract_id) FROM contracts GROUP BY title, type_id, class_weight")
    
    data = []
    for row in c.fetchall():
        data.append({
            "title": row[0],
            "type_id": row[1],
            "class_weight": row[2],
            "stock": row[3],
            "min_price": row[4],
            "max_price": row[5],
            "cheapest_id": row[6]
        })
        
    conn.close()
    return data

@app.get("/login")
def login():
    encoded_callback = urllib.parse.quote(CALLBACK_URL, safe="")
    url = f"https://login.eveonline.com/v2/oauth/authorize?response_type=code&redirect_uri={encoded_callback}&client_id={FRONTEND_CLIENT_ID}&scope=esi-ui.open_window.v1&state=secure_string"
    return RedirectResponse(url)

@app.get("/callback")
def callback(code: str, state: str = None):
    url = "https://login.eveonline.com/v2/oauth/token"
    data = {"grant_type": "authorization_code", "code": code}
    auth = (FRONTEND_CLIENT_ID, FRONTEND_CLIENT_SECRET)
    res = requests.post(url, data=data, auth=auth)
    
    if res.status_code != 200:
        return HTMLResponse(f"<h1>Login Failed</h1><p>Error: {res.text}</p>")
        
    user_access_token = res.json().get("access_token")
    return RedirectResponse(f"https://emabe390.github.io/SlideContracting/?token={user_access_token}")
    #return RedirectResponse(f"/?token={user_access_token}")

@app.get("/data")
def get_public_json():
    # Strategy A: Try reading the cached JSON file first
    try:
        with open("contracts.json", "r") as f:
            data = json.load(f)
            if data:  # If it contains items, serve it!
                return data
    except FileNotFoundError:
        pass

    # Strategy B Fallback: If JSON is empty/missing, read the DB live so it's never blank
    print("[API] contracts.json empty or missing. Falling back to live DB query.")
    conn = sqlite3.connect("contracts.db")
    c = conn.cursor()
    c.execute("SELECT title, type_id, class_weight, COUNT(*), MIN(price), MAX(price), MIN(contract_id) FROM contracts GROUP BY title, type_id, class_weight")
    
    export_data = []
    for row in c.fetchall():
        export_data.append({
            "title": row[0], "type_id": row[1], "class_weight": row[2],
            "stock": row[3], "min_price": row[4], "max_price": row[5], "cheapest_id": row[6]
        })
    conn.close()
    return export_data

@app.post("/api/open_window/{contract_id}")
def open_window(contract_id: int, token: str):
    url = f"https://esi.evetech.net/latest/ui/openwindow/contract/?contract_id={contract_id}"
    headers = {"Authorization": f"Bearer {token}"}
    res = requests.post(url, headers=headers)
    
    if res.status_code == 204: 
        return {"status": "opened"}
    else:
        return {"status": "error", "esi_response": res.text}

@app.get("/", response_class=HTMLResponse)
def index():
    try:
        with open("index.html", "r") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>Backend is running, but index.html was not found in this folder!</h1>")
