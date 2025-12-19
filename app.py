import asyncio
import nest_asyncio
import json
import logging
import base64
import os
import time
import requests
import re
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright, Route
from typing import Optional, Dict, List, Any

# Aszinkron loop engedélyezése Flask környezetben (pl. Render-en)
nest_asyncio.apply()

app = Flask(__name__)
# Kikapcsoljuk az alapértelmezett JSON rendezést a tisztaság érdekében
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# Naplózás beállítása
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- KONFIGURÁCIÓK ---
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"
TUBI_CONTENT_API_PARAMS = (
    "app_id=tubitv&platform=web&content_id={content_id}&device_id={device_id}&"
    "include_channels=true&pagination%5Bseason%5D={season_num}&"
    "pagination%5Bpage_in_season%5D={page_num}&pagination%5Bpage_size_in_season%5D={page_size}&"
    "limit_resolutions[]=h264_1080p&video_resources[]=hlsv6"
)

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    """Kinyeri a device_id-t a JWT tokenből, ha a fejléc hiányzik."""
    try:
        parts = jwt_token.split('.')
        if len(parts) < 2: return None
        payload_b64 = parts[1]
        padding = '=' * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64 + padding).decode('utf-8'))
        return payload.get('device_id')
    except:
        return None

def extract_content_id(url: str) -> Optional[str]:
    """Kinyeri a numerikus Content ID-t a Tubi URL-ből."""
    match = re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def make_paginated_api_call(content_id, token, device_id, season_num, pages=1, size=50):
    """Meghívja a Tubi belső API-ját az epizódokért."""
    all_pages = []
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    for p in range(1, int(pages) + 1):
        query = TUBI_CONTENT_API_PARAMS.format(
            content_id=content_id, device_id=device_id, 
            season_num=season_num, page_num=p, page_size=size
        )
        api_url = f"{TUBI_CONTENT_API_BASE}?{query}"
        try:
            r = requests.get(api_url, headers=headers, timeout=15)
            if r.status_code == 200:
                all_pages.append({"page_number": p, "json_content": r.json()})
            else:
                logging.error(f"API Hiba Page {p}: {r.status_code}")
        except Exception as e:
            logging.error(f"API Kivétel Page {p}: {e}")
    return all_pages

async def scrape_full_stealth(url):
    """Playwright használata a tokenek és a tiszta HTML eléréséhez."""
    res = {
        "status": "success", 
        "tubi_token": None, 
        "tubi_device_id": None, 
        "html": "", 
        "debug_info": []
    }
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Hálózati forgalom figyelése a tokenekért
        async def intercept_routes(route: Route):
            headers = route.request.headers
            auth = headers.get('authorization')
            if auth and 'Bearer' in auth and not res['tubi_token']:
                res['tubi_token'] = auth.replace('Bearer ', '')
                logging.info(f"🔑 TOKEN ELCSÍPVE: {res['tubi_token'][:20]}...")
            
            d_id = headers.get(DEVICE_ID_HEADER.lower())
            if d_id:
                res['tubi_device_id'] = d_id
                
            await route.continue_()

        await page.route("**/*", intercept_routes)
        
        try:
            # Oldal betöltése
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Polling a tokenre (várunk max 15 mp-et, amíg megérkezik a háttérben)
            for _ in range(30):
                if res['tubi_token']: break
                await asyncio.sleep(0.5)
            
            # Tiszta HTML kimentése
            res['html'] = await page.content()
            
            # Ha a fejlécből nem jött meg a Device ID, kiszedjük a tokenből
            if res['tubi_token'] and not res['tubi_device_id']:
                res['tubi_device_id'] = decode_jwt_payload(res['tubi_token'])
                
        except Exception as e:
            res['status'] = "error"
            res['debug_info'].append(str(e))
        finally:
            await browser.close()
            
    return res

@app.route('/scrape', methods=['GET'])
def main_endpoint():
    # Paraméterek
    url = request.args.get('url') or request.args.get('web')
    mode = 'html' if request.args.get('web') else 'json'
    
    if not url:
        return "Hiba: Használd a ?url=URL (adatokhoz) vagy ?web=URL (tiszta HTML-hez) formátumot!", 400
    
    season = request.args.get('season')
    pages = request.args.get('pages', 1)
    size = request.args.get('page_size', 50)

    # 1. Playwright futtatása
    data = asyncio.run(scrape_full_stealth(url))

    # --- HTML MÓD (yt-dlp-hez vagy böngészőhöz) ---
    if mode == 'html':
        logging.info(f"🌐 [WEB MODE] Tiszta HTML válasz küldése.")
        return Response(data['html'], mimetype='text/html')

    # --- JSON MÓD (A tubi_season.py-nak) ---
    if season and data['tubi_token']:
        c_id = extract_content_id(url)
        if c_id:
            data['page_data'] = make_paginated_api_call(
                c_id, data['tubi_token'], data['tubi_device_id'], 
                season, pages, size
            )
        else:
            data['page_data'] = []
            data['debug_info'].append("Content ID nem található az URL-ben.")
    else:
        data['page_data'] = []

    # JAVÍTÁS: JSON válasz összeállítása visszaperjelek nélkül
    # Az ensure_ascii=False megőrzi az ékezeteket, a replace kiszedi a \/ jeleket
    json_string = json.dumps(data, ensure_ascii=False).replace('\\/', '/')
    
    return Response(json_string, mimetype='application/json')

if __name__ == '__main__':
    # Render.com-nak megfelelő port beállítás
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
