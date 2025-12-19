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

DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def decode_jwt_device_id(token: str) -> Optional[str]:
    try:
        parts = token.split('.')
        if len(parts) != 3: return None
        payload = parts[1]
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
    final_device_id = device_id or decode_jwt_device_id(token)
    
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: final_device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://tubitv.com",
        "Referer": "https://tubitv.com/"
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
            logging.info(f"🚀 API hívás indítása: S{season_num} P{page_num}")
            resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                all_pages_data.append({"page": page_num, "json_content": resp.json()})
            else:
                logging.error(f"❌ API hiba: {resp.status_code}")
        except Exception as e:
            logging.error(f"❌ Hálózati hiba: {e}")
            
    return all_pages_data

async def scrape_tubi(url: str):
    res = {'tubi_token': None, 'tubi_device_id': None, 'debug_info': []}
    async with async_playwright() as p:
        # Lassabb indítás a detektálás ellen
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        page = await context.new_page()

        async def handle_route(route: Route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER.lower())
            if auth and "Bearer" in auth:
                res['tubi_token'] = auth.replace("Bearer ", "")
                logging.info("✅ Token elkapva a hálózatból!")
            if dev_id:
                res['tubi_device_id'] = dev_id
            await route.continue_()

        await page.route("**/*", handle_route)
        
        try:
            # Úgy teszünk, mintha a Google-ről jönnénk
            await page.set_extra_http_headers({"Referer": "https://www.google.com/"})
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Emberi mozgás szimulálása
            await page.mouse.move(500, 500)
            await asyncio.sleep(2)
            await page.mouse.wheel(0, 800)
            
            # Várunk, amíg a token megérkezik (max 15 mp)
            for _ in range(15):
                if res['tubi_token']: break
                await asyncio.sleep(1)
            
            if res['tubi_token'] and not res['tubi_device_id']:
                res['tubi_device_id'] = decode_jwt_device_id(res['tubi_token'])
                
        except Exception as e:
            logging.error(f"Scrape hiba: {e}")
        finally:
            await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main_endpoint():
    url = request.args.get('url')
    season = request.args.get('season')
    pages = request.args.get('pages', 2)
    size = request.args.get('page_size', 20)
    
    if not url: return jsonify({"status": "error", "message": "URL hiányzik"}), 400
    
    data = asyncio.run(scrape_tubi(url))
    
    if data['tubi_token']:
        if season:
            c_id = extract_content_id(url)
            data['page_data'] = make_paginated_api_call(c_id, data['tubi_token'], data['tubi_device_id'], season, pages, size)
        data['status'] = 'success'
    else:
        data['status'] = 'failure'
        data['message'] = "Nem sikerült kinyerni a tokent. Próbáld újra!"

    return jsonify(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
