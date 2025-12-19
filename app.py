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

# Aszinkron loop engedélyezése Flask alatt
nest_asyncio.apply()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- KONFIGURÁCIÓK ---
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"
TUBI_CONTENT_API_PARAMS = (
    "app_id=tubitv&platform=web&content_id={content_id}&device_id={device_id}&"
    "include_channels=true&pagination%5Bseason%5D={season_num}&"
    "pagination%5Bpage_in_season%5D={page_num}&pagination%5Bpage_size_in_season%5D={page_size}&"
    "limit_resolutions[]=h264_1080p&video_resources[]=hlsv6"
)

def decode_jwt_payload(jwt_token: str):
    """Kinyeri a device_id-t a tokenből, ha a fejléc hiányzik."""
    try:
        payload_b64 = jwt_token.split('.')[1]
        padding = '=' * (4 - len(payload_b64) % 4)
        return json.loads(base64.b64decode(payload_b64 + padding).decode('utf-8')).get('device_id')
    except: return None

def extract_id(url):
    """Kinyeri a numerikus ID-t a Tubi URL-ből."""
    m = re.search(r'/(\d+)/', url)
    return m.group(1) if m else None

async def scrape_tubi_core(url):
    res = {"status": "success", "tubi_token": None, "tubi_device_id": None, "html": "", "debug_info": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = await context.new_page()

        async def intercept(route: Route):
            h = route.request.headers
            if 'authorization' in h and 'Bearer' in h['authorization'] and not res['tubi_token']:
                res['tubi_token'] = h['authorization'].replace('Bearer ', '')
                msg = f"🔑 TOKEN ELCSÍPVE: {res['tubi_token'][:15]}..."
                res['debug_info'].append(msg)
                logging.info(msg)
            if DEVICE_ID_HEADER.lower() in h:
                res['tubi_device_id'] = h[DEVICE_ID_HEADER.lower()]
            await route.continue_()

        await page.route("**/*", intercept)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        
        # Várakozás a hálózati forgalomra
        for _ in range(20):
            if res['tubi_token']: break
            await asyncio.sleep(0.5)
            
        res['html'] = await page.content()
        # Fallback ha a fejlécben nem volt Device ID
        if res['tubi_token'] and not res['tubi_device_id']:
            res['tubi_device_id'] = decode_jwt_payload(res['tubi_token'])
            res['debug_info'].append("📱 Device ID kinyerve a JWT tokenből.")
            
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main_scrape():
    url = request.args.get('url')
    is_api = request.args.get('target_api') == 'true'
    season = request.args.get('season')
    
    if not url: return jsonify({"error": "Nincs URL megadva"}), 400

    # 1. Scrape futtatása a tokenért
    data = asyncio.run(scrape_tubi_core(url))

    # 2. Ha van token és évadot kértek, hívjuk meg az API-t a szerveren
    if season and data['tubi_token']:
        c_id = extract_id(url)
        d_id = data.get('tubi_device_id', 'unknown')
        api_url = f"{TUBI_CONTENT_API_BASE}?{TUBI_CONTENT_API_PARAMS.format(content_id=c_id, device_id=d_id, season_num=season, page_num=1, page_size=50)}"
        
        try:
            r = requests.get(api_url, headers={
                "Authorization": f"Bearer {data['tubi_token']}",
                DEVICE_ID_HEADER: d_id
            }, timeout=15)
            if r.status_code == 200:
                data['tubi_api_data'] = r.json()
                data['debug_info'].append("✅ Szerveroldali API hívás sikeres.")
            else:
                data['debug_info'].append(f"❌ API hiba: {r.status_code}")
        except Exception as e:
            data['debug_info'].append(f"❌ Rendszerhiba az API hívásnál: {str(e)}")

    if is_api:
        return jsonify(data)
    return Response(data['html'], mimetype='text/html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
