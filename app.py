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
# A Content API alap URL-je
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def extract_content_id(url: str) -> Optional[str]:
    """Kinyeri a numerikus Content ID-t a Tubi URL-ből."""
    match = re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def make_paginated_api_call(content_id, token, device_id, season_num, pages, page_size):
    """
    A régi app.py logikája alapján meghívja a Tubi Content API-t.
    Támogatja a több oldalas letöltést (Page 1, 2 stb.).
    """
    all_pages_data = []
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    for p_idx in range(int(pages)):
        page_num = p_idx + 1
        # Paraméterek összeállítása a régi kód és az API elvárásai szerint
        params = {
            "app_id": "tubitv",
            "platform": "web",
            "content_id": content_id,
            "device_id": device_id,
            "include_channels": "true",
            "pagination[season]": season_num,
            "pagination[page_in_season]": page_num,
            "pagination[page_size_in_season]": page_size
        }
        
        try:
            logging.info(f"API hívás: {content_id}, S{season_num}, Page {page_num}")
            resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=30)
            
            if resp.status_code == 200:
                all_pages_data.append({
                    "page": page_num,
                    "json_content": resp.json()
                })
            else:
                logging.error(f"API hiba (Status: {resp.status_code}): {resp.text}")
        except Exception as e:
            logging.error(f"Hiba az API hívás során: {e}")
            
    return all_pages_data

async def scrape_tubi(url: str):
    """Playwright alapú scraping a tokenek és alap adatok kinyeréséhez."""
    res = {'tubi_token': None, 'tubi_device_id': None, 'debug_info': []}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        async def handle_route(route: Route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER.lower())
            if auth and "Bearer" in auth:
                res['tubi_token'] = auth.replace("Bearer ", "")
            if dev_id:
                res['tubi_device_id'] = dev_id
            await route.continue_()

        await page.route("**/*", handle_route)
        await page.goto(url, wait_until="networkidle")
        await asyncio.sleep(2)
        
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main_endpoint():
    url = request.args.get('url')
    season = request.args.get('season')
    pages = request.args.get('pages', 2) # Alapértelmezett 2 oldal
    size = request.args.get('page_size', 20)
    
    if not url: return jsonify({"status": "error", "message": "Hiányzó URL"}), 400
    
    # 1. Scraping a hitelesítő adatokért
    data = asyncio.run(scrape_tubi(url))
    
    # 2. Ha van évad kérés, a Content API meghívása
    if season and data['tubi_token']:
        c_id = extract_content_id(url)
        if c_id:
            # Itt történik az új Content API hívás
            data['page_data'] = make_paginated_api_call(
                c_id, data['tubi_token'], data['tubi_device_id'], 
                season, pages, size
            )
            data['status'] = 'success'
        else:
            data['status'] = 'partial_success'
            data['message'] = "Content ID nem található"
    else:
        data['status'] = 'success'
        data['page_data'] = []

    return jsonify(data)

if __name__ == '__main__':
    app.run(port=5000)
