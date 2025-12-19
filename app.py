import asyncio
import nest_asyncio
import json
import logging
import base64
import requests
import re
import urllib.parse
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright, Route
from typing import Optional

nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# Logolás beállítása, hogy látszódjanak a tokenek a konzolon
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)

DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def decode_jwt_device_id(token: str) -> Optional[str]:
    """Ha a böngésző nem látja a fejlécet, a tokenből szedjük ki a Device ID-t."""
    try:
        parts = token.split('.')
        if len(parts) < 2: return None
        # Padding javítása a base64 dekódoláshoz
        payload_b64 = parts[1] + ("=" * (4 - len(parts[1]) % 4))
        payload = base64.b64decode(payload_b64).decode('utf-8')
        return json.loads(payload).get('device_id')
    except Exception as e:
        logging.error(f"JWT dekódolási hiba: {e}")
        return None

def extract_content_id(url: str) -> Optional[str]:
    """Kinyeri a numerikus azonosítót az URL-ből (pl. 300002691)."""
    match = re.search(r'series/(\d+)', url)
    if not match:
        match = re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def call_content_api(content_id, token, device_id, season_num, page_num, page_size):
    """Közvetlen HTTP kérés a Content API-ra."""
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
    
    logging.info(f"🚀 API Hívás indítása - Content ID: {content_id}, Season: {season_num}")
    
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=20)
        if resp.status_code == 200:
            logging.info("✅ API válasz sikeres (200 OK)")
            return resp.json()
        else:
            logging.error(f"❌ API hiba: {resp.status_code} - {resp.text}")
            return {"error": "NotFound", "details": resp.text}
    except Exception as e:
        logging.error(f"❌ Hiba az API hívás közben: {str(e)}")
        return {"error": "ConnectionError", "message": str(e)}

async def scrape_auth_and_html(url: str):
    """Elindítja a böngészőt a token és a HTML kinyeréséhez."""
    res = {'token': None, 'device_id': None, 'html': ""}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
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
                logging.info(f"📱 DEVICE ID ELKAPVA: {dev_id}")
                
            await route.continue_()

        await page.route("**/*", handle_route)
        
        logging.info(f"🌐 Oldal betöltése: {url}")
        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
            # Várunk egy kicsit, hogy az API hívások biztosan lefussanak
            await asyncio.sleep(2) 
            res['html'] = await page.content()
        except Exception as e:
            logging.error(f"❌ Hiba a betöltéskor: {e}")
            res['html'] = "Timeout or Error"
            
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main():
    url = request.args.get('url')
    target_api = request.args.get('target_api') == 'true'
    season = request.args.get('season')
    pages = int(request.args.get('pages', 1))
    size = int(request.args.get('page_size', 20))
    
    # Kliens által küldött manuális token (ha van)
    manual_token = request.args.get('token')
    manual_device_id = request.args.get('device_id')

    if not url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    # 1. Ha NINCS manuális token, futtatjuk a Playwright-ot
    if not manual_token:
        logging.info("🕵️ Playwright indítása token kinyeréséhez...")
        auth = asyncio.run(scrape_auth_and_html(url))
        token = auth['token']
        device_id = auth['device_id']
        html_content = auth['html']
    else:
        logging.info("♻️ Használjuk a kliens által küldött tokent.")
        token = manual_token
        device_id = manual_device_id
        html_content = "Using provided token, no HTML scraped."

    result = {
        "status": "success",
        "tubi_token": token,
        "tubi_device_id": device_id,
        "html_content": html_content,
        "page_data": []
    }

    # 2. API hívás, ha epizódokat kértek
    if target_api and season:
        if not token:
            logging.error("❌ Hiba: target_api=true, de nincs token!")
            result["page_data"].append({"json_content": "Token not found", "page_number": 1})
        else:
            c_id = extract_content_id(url)
            if c_id:
                for p in range(1, pages + 1):
                    api_resp = call_content_api(c_id, token, device_id, season, p, size)
                    result["page_data"].append({"page": p, "json_content": api_resp})
            else:
                result["status"] = "error"
                result["message"] = "Could not extract Content ID"

    return jsonify(result)

if __name__ == '__main__':
    # Render-en a portot környezeti változóból kell venni
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
