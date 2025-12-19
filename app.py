import asyncio
import nest_asyncio
import json
import logging
import base64
import os
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
    try:
        payload_b64 = jwt_token.split('.')[1]
        padding = '=' * (4 - len(payload_b64) % 4)
        return json.loads(base64.b64decode(payload_b64 + padding).decode('utf-8')).get('device_id')
    except: return None

def extract_id(url):
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
                logging.info(f"🔑 TOKEN ELCSÍPVE")
            if DEVICE_ID_HEADER.lower() in h:
                res['tubi_device_id'] = h[DEVICE_ID_HEADER.lower()]
            await route.continue_()

        await page.route("**/*", intercept)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        
        for _ in range(20):
            if res['tubi_token']: break
            await asyncio.sleep(0.5)
            
        res['html'] = await page.content()
        if res['tubi_token'] and not res['tubi_device_id']:
            res['tubi_device_id'] = decode_jwt_payload(res['tubi_token'])
            
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main_scrape():
    url = request.args.get('url')
    is_api = request.args.get('target_api') == 'true'
    season = request.args.get('season')
    pages = int(request.args.get('pages', 1))
    size = int(request.args.get('page_size', 50))
    
    if not url: return jsonify({"status": "error", "message": "Nincs URL"}), 400

    data = asyncio.run(scrape_tubi_core(url))
    data['page_data'] = []

    # Ha évadot kértek, lehozzuk a paginált adatokat
    if season and data['tubi_token']:
        c_id = extract_id(url)
        d_id = data.get('tubi_device_id', 'unknown')
        
        for p in range(1, pages + 1):
            api_url = f"{TUBI_CONTENT_API_BASE}?{TUBI_CONTENT_API_PARAMS.format(content_id=c_id, device_id=d_id, season_num=season, page_num=p, page_size=size)}"
            try:
                r = requests.get(api_url, headers={"Authorization": f"Bearer {data['tubi_token']}", DEVICE_ID_HEADER: d_id}, timeout=15)
                if r.status_code == 200:
                    data['page_data'].append({"page_number": p, "json_content": r.json()})
            except: continue

    if is_api: return jsonify(data)
    return Response(data['html'], mimetype='text/html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
