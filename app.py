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
from urllib.parse import urlparse, parse_qs, unquote

# Engedélyezi az aszinkron funkciók beágyazását
nest_asyncio.apply()

app = Flask(__name__)
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

# --- SEGÉDFÜGGVÉNYEK ---
def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    try:
        payload_base64 = jwt_token.split('.')[1]
        padding = '=' * (4 - len(payload_base64) % 4)
        payload_decoded = base64.b64decode(payload_base64 + padding).decode('utf-8')
        return json.loads(payload_decoded).get('device_id')
    except: return None

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

# --- FEJLETT API HÍVÓ (A HOSSZABB KÓDBÓL) ---
def make_paginated_api_call(content_id, token, device_id, user_agent, season, pages, size):
    results = []
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": user_agent,
        DEVICE_ID_HEADER: device_id,
        "Accept": "application/json"
    }
    for p in range(1, pages + 1):
        url = f"{TUBI_CONTENT_API_BASE}?{TUBI_CONTENT_API_PARAMS.format(content_id=content_id, device_id=device_id, season_num=season, page_num=p, page_size=size)}"
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                results.append({"page": p, "json_content": r.json()})
                logging.info(f"✅ S{season} P{p} letöltve.")
        except Exception as e:
            logging.error(f"❌ Hiba az API hívásnál: {e}")
    return results

# --- A SCRAPE LOGIKA ---
async def scrape_tubi(url, target_api=False):
    res = {'status': 'success', 'tubi_token': None, 'tubi_device_id': None, 'html': '', 'ua': ''}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = await context.new_page()
        res['ua'] = await page.evaluate("navigator.userAgent")

        if target_api:
            async def handle_route(route: Route):
                headers = route.request.headers
                if 'authorization' in headers and not res['tubi_token']:
                    res['tubi_token'] = headers['authorization'].replace('Bearer ', '')
                if DEVICE_ID_HEADER.lower() in headers:
                    res['tubi_device_id'] = headers[DEVICE_ID_HEADER.lower()]
                await route.continue_()
            await page.route("**/*", handle_route)

        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        
        # Token várakozás (max 10mp)
        for _ in range(20):
            if res['tubi_token']: break
            await asyncio.sleep(0.5)
            
        res['html'] = await page.content()
        
        # Device ID fallback
        if res['tubi_token'] and not res['tubi_device_id']:
            res['tubi_device_id'] = decode_jwt_payload(res['tubi_token'])
            
        await browser.close()
    return res

# --- FLASK ENDPOINT ---
@app.route('/scrape', methods=['GET'])
def main_endpoint():
    url = request.args.get('url')
    if not url: return "Adj meg egy URL-t!", 400
    
    # Paraméterek az évadletöltéshez
    season = request.args.get('season')
    pages = request.args.get('pages', 1)
    size = request.args.get('page_size', 50)
    
    # Futtatás
    data = asyncio.run(scrape_tubi(url, target_api=True))
    
    # Ha évadot akarunk kinyerni ÉS megvan a token
    if season and data['tubi_token'] and data['tubi_device_id']:
        content_id = extract_content_id(url)
        if content_id:
            data['page_data'] = make_paginated_api_call(
                content_id, data['tubi_token'], data['tubi_device_id'], 
                data['ua'], int(season), int(pages), int(size)
            )
    
    # Ha csak HTML-t kértek (web mód)
    if request.args.get('target_api') != 'true':
        return Response(data['html'], mimetype='text/html')
    
    return jsonify(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
