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
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- KONFIGURÁCIÓK ---
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def extract_content_id(url: str) -> Optional[str]:
    """Kinyeri a numerikus Content ID-t a Tubi URL-ből."""
    match = re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def make_paginated_api_call(content_id, token, device_id, season_num, pages, page_size):
    """
    Meghívja a Tubi Content API-t több oldalon keresztül (Page 1, 2 stb.).
    """
    all_pages_data = []
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    for p_idx in range(int(pages)):
        page_num = p_idx + 1
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
            logging.info(f"🚀 Content API hívás: ID={content_id}, S={season_num}, Page={page_num}")
            resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=30)
            
            if resp.status_code == 200:
                all_pages_data.append({
                    "page": page_num,
                    "json_content": resp.json()
                })
            else:
                logging.error(f"❌ API hiba (Status: {resp.status_code}): {resp.text}")
        except Exception as e:
            logging.error(f"❌ Hiba az API hívás során: {e}")
            
    return all_pages_data

async def scrape_tubi(url: str):
    """
    Playwright-al betölti az oldalt, és kinyeri a Bearer tokent és Device ID-t.
    """
    res = {'tubi_token': None, 'tubi_device_id': None, 'debug_info': []}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Egy realisztikusabb User-Agent segít elkerülni a blokkolást
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        async def handle_route(route: Route):
            # Keressük az Authorization fejlécet bármelyik API hívásban
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER.lower())
            
            if auth and "Bearer" in auth and not res['tubi_token']:
                res['tubi_token'] = auth.replace("Bearer ", "")
                logging.info("✅ Token elkapva!")
            if dev_id and not res['tubi_device_id']:
                res['tubi_device_id'] = dev_id
                
            await route.continue_()

        await page.route("**/*", handle_route)
        
        try:
            # Megvárjuk, amíg az oldal hálózati forgalma elcsendesedik
            await page.goto(url, wait_until="networkidle", timeout=60000)
            # Extra várakozás, hogy a token biztosan beérkezzen
            await page.wait_for_timeout(5000)
            
            # Ha nem jött meg a token, egy görgetés gyakran triggereli az API-t
            if not res['tubi_token']:
                await page.mouse.wheel(0, 1000)
                await page.wait_for_timeout(3000)
                
        except Exception as e:
            res['debug_info'].append(f"Scrape hiba: {str(e)}")
        finally:
            await browser.close()
            
    return res

@app.route('/scrape', methods=['GET'])
def main_endpoint():
    url = request.args.get('url')
    season = request.args.get('season')
    pages = request.args.get('pages', 2) # Alapértelmezett 2 oldal a teljes évadhoz
    size = request.args.get('page_size', 20)
    
    if not url:
        return jsonify({"status": "error", "message": "Hiányzó URL paraméter"}), 400
    
    # 1. Scraping a tokenekért
    data = asyncio.run(scrape_tubi(url))
    
    # 2. Ha megvan a token ÉS kértünk évadot, hívjuk a Content API-t
    if data['tubi_token'] and season:
        content_id = extract_content_id(url)
        if content_id:
            data['page_data'] = make_paginated_api_call(
                content_id, data['tubi_token'], data['tubi_device_id'], 
                season, pages, size
            )
            data['status'] = 'success'
        else:
            data['status'] = 'error'
            data['message'] = "Nem sikerült kinyerni a Content ID-t"
            data['page_data'] = []
    else:
        # Ha csak tokent kértünk, vagy nem sikerült a token kinyerés
        data['status'] = 'success' if data['tubi_token'] else 'failure'
        data['page_data'] = []
        if not data['tubi_token']:
            data['message'] = "Nem sikerült kinyerni a tokent (Timeout/Blokkolás)"

    return jsonify(data)

if __name__ == '__main__':
    # A port 5000, ahogy a tubi_season.py várja
    app.run(host='0.0.0.0', port=5000, debug=False)
