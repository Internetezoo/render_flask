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

nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- KONFIGURÁCIÓK ---
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def decode_jwt_device_id(token: str) -> Optional[str]:
    """Kinyeri a device_id-t a JWT token payload-jából."""
    try:
        parts = token.split('.')
        if len(parts) != 3: return None
        payload = parts[1]
        # Padding pótlása a base64 dekódoláshoz
        payload += '=' * (-len(payload) % 4)
        data = json.loads(base64.b64decode(payload).decode('utf-8'))
        return data.get('device_id')
    except:
        return None

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def make_paginated_api_call(content_id, token, device_id, season_num, pages, page_size):
    all_pages_data = []
    # Ha a device_id null, megpróbáljuk a tokenből kiszedni
    final_device_id = device_id or decode_jwt_device_id(token)
    
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: final_device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    for p_idx in range(int(pages)):
        page_num = p_idx + 1
        params = {
            "app_id": "tubitv",
            "platform": "web",
            "content_id": content_id,
            "device_id": final_device_id,
            "include_channels": "true",
            "pagination[season]": season_num,
            "pagination[page_in_season]": page_num,
            "pagination[page_size_in_season]": page_size
        }
        
        try:
            logging.info(f"🚀 Content API: S{season_num} P{page_num} (ID: {final_device_id})")
            resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                all_pages_data.append({"page": page_num, "json_content": resp.json()})
            else:
                logging.error(f"❌ API Hiba: {resp.status_code}")
        except Exception as e:
            logging.error(f"❌ Hiba: {e}")
            
    return all_pages_data

async def scrape_tubi(url: str):
    res = {'tubi_token': None, 'tubi_device_id': None, 'debug_info': []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = await context.new_page()

        async def handle_route(route: Route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER.lower())
            if auth and "Bearer" in auth and not res['tubi_token']:
                res['tubi_token'] = auth.replace("Bearer ", "")
            if dev_id and not res['tubi_device_id']:
                res['tubi_device_id'] = dev_id
            await route.continue_()

        await page.route("**/*", handle_route)
        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(5000)
            # Ha nincs device_id, kiszedjük a tokenből
            if res['tubi_token'] and not res['tubi_device_id']:
                res['tubi_device_id'] = decode_jwt_device_id(res['tubi_token'])
        finally:
            await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main_endpoint():
    url = request.args.get('url')
    season = request.args.get('season')
    pages = request.args.get('pages', 2)
    size = request.args.get('page_size', 20)
    
    if not url: return jsonify({"status": "error"}), 400
    
    data = asyncio.run(scrape_tubi(url))
    
    if data['tubi_token'] and season:
        c_id = extract_content_id(url)
        data['page_data'] = make_paginated_api_call(c_id, data['tubi_token'], data['tubi_device_id'], season, pages, size)
        data['status'] = 'success' if data['page_data'] else 'failure'
    else:
        data['status'] = 'success' if data['tubi_token'] else 'failure'
        data['page_data'] = []

    return jsonify(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
