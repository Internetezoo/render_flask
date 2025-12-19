import asyncio
import nest_asyncio
import json
import logging
import base64
import requests
import re
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright, Route
from typing import Optional

nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
# KÖTELEZŐ CONTENT API URL
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def decode_jwt_device_id(token: str) -> Optional[str]:
    """Kinyeri a device_id-t a JWT tokenből."""
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
    """
    Ez a rész felelős a Page 1, Page 2 letöltéséért a Content API-ból.
    """
    all_pages_data = []
    final_device_id = device_id or decode_jwt_device_id(token)
    
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: final_device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    for p_idx in range(int(pages)):
        page_num = p_idx + 1
        # A régi tubi_season.py_regi és az API paraméterezése
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
            logging.info(f"📡 API HÍVÁS: S{season_num} P{page_num}")
            resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                all_pages_data.append({
                    "page": page_num,
                    "json_content": resp.json()
                })
        except Exception as e:
            logging.error(f"Hiba: {e}")
            
    return all_pages_data

async def get_token_and_html(url: str):
    """Böngészővel kinyeri a tokent és a teljes HTML-t az évadkereséshez."""
    res = {'tubi_token': None, 'tubi_device_id': None, 'html_content': ""}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

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
        # Ez kell az évadok kinyeréséhez a tubi_season.py-nak
        res['html_content'] = await page.content()
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main_endpoint():
    url = request.args.get('url')
    season = request.args.get('season')
    
    if not url: return jsonify({"status": "error"}), 400

    # 1. Először mindig leszedjük a tokent és a HTML-t
    data = asyncio.run(get_token_and_html(url))
    
    # 2. Csak ha a tubi_season konkrét évadot kér (&season=X), akkor hívjuk a Content API-t
    if season and data['tubi_token']:
        c_id = extract_content_id(url)
        # Content API hívás (Page 1-2)
        data['page_data'] = make_paginated_api_call(
            c_id, data['tubi_token'], data['tubi_device_id'], 
            season, request.args.get('pages', 2), request.args.get('page_size', 20)
        )
    else:
        # Évadkereséskor üres a page_data, a tubi_season a HTML-ből dolgozik
        data['page_data'] = []

    data['status'] = 'success'
    return jsonify(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
