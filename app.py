import asyncio
import nest_asyncio
import json
import logging
import base64
import requests
import re
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright, Route
from typing import Optional, Dict

nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def decode_jwt_device_id(token: str) -> Optional[str]:
    try:
        parts = token.split('.')
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
    """Kizárólag a Content API hívása, böngésző nélkül."""
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
            resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=20)
            if resp.status_code == 200:
                all_pages_data.append({"page": page_num, "json_content": resp.json()})
        except Exception as e:
            logging.error(f"API hiba: {e}")
            
    return all_pages_data

async def get_token_and_html(url: str):
    """Csak a tokent és a HTML-t szedi le az évadok kinyeréséhez."""
    res = {'tubi_token': None, 'tubi_device_id': None, 'html_content': ""}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        async def handle_route(route: Route):
            auth = route.request.headers.get("authorization")
            if auth and "Bearer" in auth:
                res['tubi_token'] = auth.replace("Bearer ", "")
            await route.continue_()

        await page.route("**/*", handle_route)
        await page.goto(url, wait_until="domcontentloaded")
        # Megvárjuk a HTML-t az évadokhoz
        res['html_content'] = await page.content()
        
        # Ha 5 mp alatt nincs token, megyünk tovább a HTML-el
        for _ in range(5):
            if res['tubi_token']: break
            await asyncio.sleep(1)
            
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main_endpoint():
    url = request.args.get('url')
    season = request.args.get('season')
    
    if not url: return jsonify({"status": "error"}), 400

    # 1. LÉPÉS: Mindig kell a token (és az első hívásnál a HTML az évadokhoz)
    data = asyncio.run(get_token_and_html(url))
    
    # 2. LÉPÉS: Csak ha van season paraméter, AKKOR hívjuk a Content API-t
    if season and data['tubi_token']:
        c_id = extract_content_id(url)
        # Itt már nincs böngésző, csak gyors requests hívás
        data['page_data'] = make_paginated_api_call(
            c_id, data['tubi_token'], data['tubi_device_id'], 
            season, request.args.get('pages', 2), request.args.get('page_size', 20)
        )
    else:
        data['page_data'] = []

    data['status'] = 'success' if (data['tubi_token'] or data['html_content']) else 'failure'
    return jsonify(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
