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
# EZ A KRITIKUS VÉGPONT AZ EPIZÓDOKHOZ
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def decode_jwt_device_id(token: str) -> Optional[str]:
    """Ha a böngésző nem látja a fejlécet, a tokenből szedjük ki a Device ID-t."""
    try:
        parts = token.split('.')
        payload = base64.b64decode(parts[1] + "==").decode('utf-8')
        return json.loads(payload).get('device_id')
    except:
        return None

def extract_content_id(url: str) -> Optional[str]:
    """Kinyeri a numerikus azonosítót az URL-ből (pl. 300002691)."""
    match = re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def call_content_api(content_id, token, device_id, season_num, page_num, page_size):
    """Közvetlen HTTP kérés a Content API-ra a Playwright kihagyásával."""
    final_device_id = device_id or decode_jwt_device_id(token)
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: final_device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    params = {
        "app_id": "tubitv",
        "platform": "web",
        "content_id": content_id,
        "device_id": final_device_id,
        "include_channels": "true",
        "pagination[season]": str(season_num),
        "pagination[page_in_season]": str(page_num),
        "pagination[page_size_in_season]": str(page_size)
    }
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=20)
        return resp.json() if resp.status_code == 200 else None
    except:
        return None

async def scrape_auth_and_html(url: str):
    """Elindítja a böngészőt, hogy megszerezze a tokent és a kezdő HTML-t."""
    res = {'token': None, 'device_id': None, 'html': ""}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        async def handle_route(route: Route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER.lower())
            if auth and "Bearer" in auth: res['token'] = auth.replace("Bearer ", "")
            if dev_id: res['device_id'] = dev_id
            await route.continue_()

        await page.route("**/*", handle_route)
        await page.goto(url, wait_until="networkidle")
        res['html'] = await page.content()
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main():
    url = request.args.get('url')
    target_api = request.args.get('target_api') == 'true'
    season = request.args.get('season')
    pages = int(request.args.get('pages', 1))
    size = int(request.args.get('page_size', 20))

    if not url: return jsonify({"status": "error", "message": "No URL"}), 400

    # 1. Token és HTML beszerzése (mindig kell)
    auth = asyncio.run(scrape_auth_and_html(url))
    
    result = {
        "status": "success",
        "tubi_token": auth['token'],
        "tubi_device_id": auth['device_id'],
        "html_content": auth['html'],
        "page_data": []
    }

    # 2. HA epizódokat kérünk (season megadva), hívjuk a Content API-t
    if target_api and season and auth['token']:
        c_id = extract_content_id(url)
        if c_id:
            for p in range(1, pages + 1):
                api_resp = call_content_api(c_id, auth['token'], auth['device_id'], season, p, size)
                if api_resp:
                    result["page_data"].append({"page": p, "json_content": api_resp})
        else:
            result["status"] = "error"
            result["message"] = "Could not find Content ID"

    return jsonify(result)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
