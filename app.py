import asyncio
import nest_asyncio
import json
import logging
import base64
import os
import time
import requests
import re
import urllib.parse
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright, Route
from typing import Optional, Dict, List, Any

# Engedélyezi az aszinkron funkciók beágyazását Flask alatt
nest_asyncio.apply()

app = Flask(__name__)
# Kikapcsoljuk az alapértelmezett JSON rendezést a gyorsaság és tisztaság érdekében
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
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

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    try:
        parts = jwt_token.split('.')
        if len(parts) < 2: return None
        payload_b64 = parts[1]
        padding = '=' * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64 + padding).decode('utf-8'))
        return payload.get('device_id')
    except: return None

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def make_paginated_api_call(content_id, token, device_id, season_num, pages=1, size=50):
    all_pages = []
    headers = {"Authorization": f"Bearer {token}", DEVICE_ID_HEADER: device_id}
    
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

async def scrape_tubi(url):
    res = {"status": "success", "tubi_token": None, "tubi_device_id": None, "html": "", "debug_info": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = await context.new_page()

        async def intercept(route: Route):
            headers = route.request.headers
            auth = headers.get('authorization')
            if auth and 'Bearer' in auth and not res['tubi_token']:
                res['tubi_token'] = auth.replace('Bearer ', '')
                logging.info("🔑 Token elcsípve!")
            
            d_id = headers.get(DEVICE_ID_HEADER.lower())
            if d_id: res['tubi_device_id'] = d_id
            await route.continue_()

        await page.route("**/*", intercept)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Polling a tokenre a szerveren belül is (max 10mp)
            for _ in range(20):
                if res['tubi_token']: break
                await asyncio.sleep(0.5)
            
            res['html'] = await page.content()
            if res['tubi_token'] and not res['tubi_device_id']:
                res['tubi_device_id'] = decode_jwt_payload(res['tubi_token'])
        finally:
            await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main_endpoint():
    url = request.args.get('url')
    if not url: return "Hiba: Hiányzó URL paraméter!", 400
    
    season = request.args.get('season')
    pages = request.args.get('pages', 1)
    size = request.args.get('page_size', 50)
    
    # 1. Scraping indítása
    data = asyncio.run(scrape_tubi(url))
    
    # 2. Ha van évad kérés, az API-t is meghívjuk
    if season and data['tubi_token']:
        c_id = extract_content_id(url)
        if c_id:
            data['page_data'] = make_paginated_api_call(
                c_id, data['tubi_token'], data['tubi_device_id'], 
                season, pages, size
            )
        else:
            data['page_data'] = []
            data['debug_info'].append("Nem sikerült kinyerni a Content ID-t az URL-ből.")
    else:
        data['page_data'] = []

    # --- JAVÍTÁS: TISZTA JSON VÁLASZ VISSZAPERJELEK NÉLKÜL ---
    # Az ensure_ascii=False megőrzi az ékezeteket
    # A json.dumps NEM tesz \-t a / elé, ha nem kényszerítjük
    clean_json = json.dumps(data, ensure_ascii=False)
    
    return Response(clean_json, mimetype='application/json')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
