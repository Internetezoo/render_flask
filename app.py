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

nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# Részletes logolás a Render konzolhoz
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)

DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def decode_jwt_device_id(token: str) -> Optional[str]:
    try:
        parts = token.split('.')
        if len(parts) < 2: return None
        payload_b64 = parts[1] + ("=" * (4 - len(parts[1]) % 4))
        payload = base64.b64decode(payload_b64).decode('utf-8')
        return json.loads(payload).get('device_id')
    except:
        return None

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'series/(\d+)', url)
    if not match:
        match = re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def call_content_api(content_id, token, device_id, season_num, page_num, page_size):
    final_device_id = device_id or decode_jwt_device_id(token) or "48882a5d-40a1-4fc3-9fb5-4a68b8f393cb"
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: final_device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://tubitv.com",
        "Referer": "https://tubitv.com/"
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
        return resp.json() if resp.status_code == 200 else {"error": "NotFound", "details": resp.text}
    except Exception as e:
        return {"error": "ConnectionError", "message": str(e)}

async def scrape_auth_and_html(url: str):
    """Böngésző indítása headless módban, várakozással a tokenre."""
    res = {'token': None, 'device_id': None, 'html': ""}
    
    async with async_playwright() as p:
        # Álcázott indítás
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        # Fix képernyőméret és User-Agent
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        async def handle_route(route: Route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER.lower())
            
            if auth and "Bearer" in auth:
                token_val = auth.replace("Bearer ", "").strip()
                if token_val and token_val != "undefined":
                    res['token'] = token_val
                    logging.info(f"🔑 TOKEN ELKAPVA: {res['token'][:30]}...")
            
            if dev_id:
                res['device_id'] = dev_id
            
            await route.continue_()

        await page.route("**/*", handle_route)
        
        logging.info(f"🌐 Oldal betöltése: {url}")
        try:
            # Megvárjuk, amíg a hálózat elcsendesedik
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            # KRITIKUS: Várunk 5 másodpercet a háttérfolyamatokra!
            logging.info("⏳ Várakozás a token generálódására (5mp)...")
            await asyncio.sleep(5) 
            
            res['html'] = await page.content()
        except Exception as e:
            logging.error(f"❌ Hiba: {e}")
            
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main():
    # Paraméterek kezelése (web vagy url)
    url = request.args.get('web') or request.args.get('url')
    target_api = request.args.get('target_api') == 'true'
    season = request.args.get('season')
    
    manual_token = request.args.get('token')
    manual_device_id = request.args.get('device_id')

    if not url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    # Token beszerzése
    if not manual_token:
        logging.info("🕵️ Playwright indítása...")
        auth = asyncio.run(scrape_auth_and_html(url))
        token = auth['token']
        device_id = auth['device_id']
        html_content = auth['html']
    else:
        logging.info("♻️ Manuális token használata.")
        token = manual_token
        device_id = manual_device_id
        html_content = "Pre-authenticated"

    result = {
        "status": "success",
        "tubi_token": token,
        "tubi_device_id": device_id,
        "html_content": html_content,
        "page_data": []
    }

    # API hívás
    if target_api and season:
        if not token:
            logging.error("❌ NINCS TOKEN AZ API HÍVÁSHOZ!")
            result["page_data"].append({"json_content": "Token not found", "page_number": 1})
        else:
            c_id = extract_content_id(url)
            if c_id:
                api_resp = call_content_api(c_id, token, device_id, season, 1, 20)
                result["page_data"].append({"page": 1, "json_content": api_resp})

    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
