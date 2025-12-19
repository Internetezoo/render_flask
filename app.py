import asyncio
import nest_asyncio
import json
import logging
import base64
import requests
import re
import os
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright, Route
from typing import Optional
from urllib.parse import urlparse

nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)

DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def is_tubi_url(url: str) -> bool:
    """Ellenőrzi, hogy a célpont TubiTV-e."""
    return "tubitv.com" in urlparse(url).netloc

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'series/(\d+)', url)
    if not match:
        match = re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def call_content_api(content_id, token, device_id, season_num):
    # (A kód változatlan marad, de csak Tubi esetén hívjuk meg)
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Origin": "https://tubitv.com",
        "Referer": "https://tubitv.com/"
    }
    params = {
        "app_id": "tubitv", "platform": "web", "content_id": content_id,
        "device_id": device_id, "pagination[season]": str(season_num),
        "pagination[page_size_in_season]": "50"
    }
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=25)
        return resp.json() if resp.status_code == 200 else {"error": "API_ERROR", "status": resp.status_code}
    except Exception as e:
        return {"error": str(e)}

async def general_scrape(url: str, is_tubi: bool):
    """Általános scraper, ami Tubi esetén figyeli a hálózatot is."""
    res = {'token': None, 'device_id': None, 'html': ""}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent="Mozilla/5.0 ...")
        page = await context.new_page()

        if is_tubi:
            # Csak Tubi esetén figyeljük a fejlécet
            async def handle_route(route: Route):
                auth = route.request.headers.get("authorization")
                dev_id = route.request.headers.get(DEVICE_ID_HEADER.lower())
                if auth and "Bearer" in auth:
                    res['token'] = auth.replace("Bearer ", "").strip()
                if dev_id:
                    res['device_id'] = dev_id
                await route.continue_()
            await page.route("**/*", handle_route)

        try:
            logging.info(f"🌐 Navigáció: {url}")
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            # Tubi esetén várunk kicsit többet a dinamikus tartalomra
            if is_tubi:
                await asyncio.sleep(5)
            
            res['html'] = await page.content()
        except Exception as e:
            logging.error(f"Hiba: {e}")
        
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main():
    target_url = request.args.get('web') or request.args.get('url')
    target_api = request.args.get('target_api') == 'true'
    season = request.args.get('season', '1')

    if not target_url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    # 1. Eldöntjük, hogy Tubi-e
    is_tubi = is_tubi_url(target_url)
    
    # 2. Scrape végrehajtása
    # Ha Tubi és van küldött token, nem kell böngésző a tokenhez (de a HTML-hez igen)
    scraped_data = asyncio.run(general_scrape(target_url, is_tubi))
    
    result = {
        "status": "success",
        "is_tubi": is_tubi,
        "html_content": scraped_data['html']
    }

    # 3. Csak ha TubiTV, akkor rakjuk bele a tokeneket és hívjuk az API-t
    if is_tubi:
        token = scraped_data['token'] or request.args.get('token')
        device_id = scraped_data['device_id'] or request.args.get('device_id') or "48882a5d-40a1-4fc3-9fb5-4a68b8f393cb"
        
        result.update({
            "tubi_token": token,
            "tubi_device_id": device_id,
            "page_data": []
        })

        if target_api and token:
            c_id = extract_content_id(target_url)
            if c_id:
                api_data = call_content_api(c_id, token, device_id, season)
                result["page_data"].append({"json_content": api_data})

    return jsonify(result)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
