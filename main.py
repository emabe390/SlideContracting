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


# --- BACKGROUND SCRAPER ---
async def scrape_contracts():
    try:
        while True:
            print("\n" + "="*50)
            print("[SCRAPER] Starting sync cycle (SMART SCRAPE)...")
            print("="*50)
            
            token = get_director_access_token(DIRECTOR_REFRESH_TOKEN)
            if not token:
                print("[ERROR] No valid token. Skipping this cycle.")
                await asyncio.sleep(60)
                continue
                
            headers = {"Authorization": f"Bearer {token}"}
            
            # Fetch Contracts
            url = f"https://esi.evetech.net/latest/corporations/{DIRECTOR_CORPORATION_ID}/contracts/"
            res = requests.get(url, headers=headers)
            
            if res.status_code != 200:
                print(f"[ERROR] ESI returned status code {res.status_code}")
                await asyncio.sleep(60)
                continue
                
            raw_contracts = res.json()
            contracts = [c for c in raw_contracts if c.get("type") == "item_exchange" and c.get("status") == "outstanding" and c.get("title")]
            
            print(f"[SCRAPER] Found {len(contracts)} valid doctrine contracts. Inspecting contents...")

            conn = sqlite3.connect("contracts.db")
            c = conn.cursor()
            c.execute("DELETE FROM contracts")
            
# ... Inside scrape_contracts loop ...
            for index, contract in enumerate(contracts, 1):
                c_id = contract["contract_id"]
                price = contract["price"]
                issuer_id = contract["issuer_id"]
                title = contract["title"].strip()
                
                items_url = f"https://esi.evetech.net/latest/corporations/{DIRECTOR_CORPORATION_ID}/contracts/{c_id}/items/"
                items_res = requests.get(items_url, headers=headers)
                
                ship_type_id = 0
                class_weight = 99
                fallback_candidate = 0 # Remembers the most valuable raw asset found
                
                if items_res.status_code == 200:
                    items = items_res.json()
                    
                    # CRITICAL FIX: Sort items so higher-value single items are processed first.
                    # This prevents raw ammo stacks (qty 1000) from blinding the loop tracker.
                    items = sorted(items, key=lambda x: x.get("quantity", 1))
                    
                    for item in items:
                        tid = item["type_id"]
                        
                        # Keep track of a valid item type to ensure image renders even if categorizer fails
                        if fallback_candidate == 0:
                            fallback_candidate = tid
                            
                        is_ship, weight = resolve_item_type(tid)
                        if is_ship:
                            ship_type_id = tid
                            class_weight = weight
                            break # Found the validated hull, stop searching this contract
                
                # If no explicit ship category was caught but we have items, apply fallback for icon integrity
                if ship_type_id == 0 and fallback_candidate > 0:
                    ship_type_id = fallback_candidate
                    
                c.execute("INSERT INTO contracts VALUES (?, ?, ?, ?, ?, ?)", (c_id, title, price, issuer_id, ship_type_id, class_weight))

                if index % 20 == 0:
                    print(f"  -> Processed {index}/{len(contracts)} contracts...")
                    await asyncio.sleep(0.1) # Yield to the webserver so we don't freeze the UI
            
            conn.commit()
            conn.close()
            
            # Export the exact same data to a public JSON file
            c_export = conn.cursor()
            c_export.execute("SELECT title, type_id, class_weight, COUNT(*), MIN(price), MAX(price), MIN(contract_id) FROM contracts GROUP BY title, type_id, class_weight")
            
            export_data = []
            for row in c_export.fetchall():
                export_data.append({
                    "title": row[0], "type_id": row[1], "class_weight": row[2],
                    "stock": row[3], "min_price": row[4], "max_price": row[5], "cheapest_id": row[6]
                })
            
            # Save it right into your root folder
            with open("contracts.json", "w") as json_file:
                json.dump(export_data, json_file)
                
            print("[SCRAPER] Public contracts.json file generated successfully.")            

            # --- AUTOMATIC GIT UPDATE ROUTINE ---
            try:
                print("[SCRAPER] Pushing fresh data to GitHub...")
                # Sequentially staging, committing, and uploading the data file
                os.system("git add contracts.json")
                os.system("git commit -m 'Automated asset cache sync update'")
                os.system("git push origin main") 
                print("[SCRAPER] GitHub Repository sync complete.")
            except Exception as git_err:
                print(f"[WARNING] Automated Git push skipped or failed: {git_err}")


            print("[SCRAPER] Sync complete successfully.")
            print("[SCRAPER] Sleeping for 15 minutes.\n")
            await asyncio.sleep(900)
            
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
