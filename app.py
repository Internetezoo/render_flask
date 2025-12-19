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

# Engedélyezzük az eseményhurok egymásba ágyazását a Flask/Playwright miatt
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# Részletes naplózás beállítása
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)

DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def decode_jwt_device_id(token: str) -> Optional[str]:
    """Kinyeri a device_id-t a JWT token payload részéből, ha a fejléc hiányozna."""
    try:
        parts = token.split('.')
        if len(parts) < 2: return None
        payload_b64 = parts[1] + ("=" * (4 - len(parts[1]) % 4))
        payload = base64.b64decode(payload_b64).decode('utf-8')
        return json.loads(payload).get('device_id')
    except Exception as e:
        logging.error(f"JWT dekódolási hiba: {e}")
        return None

def extract_content_id(url: str) -> Optional[str]:
    """Kinyeri a numerikus content_id-t a Tubi URL-ből."""
    match = re.search(r'series/(\d+)', url)
    if not match:
        match = re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def call_content_api(content_id, token, device_id, season_num):
    """
    Közvetlen API hívás a Tubi szerverei felé.
    A lapméretet 50-re állítottuk, hogy minden epizód beférjen egy oldalra.
    """
    # Device ID ellenőrzés és pótlás a tokenből
    if not device_id or device_id == "None":
        device_id = decode_jwt_device_id(token)
        logging.info(f"🧩 Device ID kinyerve a tokenből: {device_id}")

    final_device_id = device_id or "48882a5d-40a1-4fc3-9fb5-4a68b8f393cb"
    
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: final_device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://tubitv.com",
        "Referer": "https://tubitv.com/"
    }
    
    # A feltöltött fájlod alapján optimalizált paraméterek
    params = {
        "app_id": "tubitv",
        "platform": "web",
        "content_id": content_id,
        "device_id": final_device_id,
        "include_channels": "true",
        "pagination[season]": str(season_num),
        "pagination[page_in_season]": "1",
        "pagination[page_size_in_season]": "50",  # FELEMELVE 50-re, így meglesz a 2. fél is!
        "limit_resolutions[]": ["h264_1080p", "h265_1080p"],
        "video_resources[]": ["hlsv6", "hlsv6_widevine_nonclearlead"],
        "images[posterarts]": "w408h583_poster"
    }
    
    logging.info(f"🔗 API lekérés indítása -> ID: {content_id}, Évad: {season_num}, Limit: 50")
    
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=25)
        if resp.status_code == 200:
            return resp.json()
        else:
            logging.error(f"❌ API hiba: {resp.status_code} - {resp.text}")
            return {"error": "API_ERROR", "status": resp.status_code, "msg": resp.text}
    except Exception as e:
        return {"error": "EXCEPTION", "msg": str(e)}

async def scrape_auth_and_html(url: str):
    """Láthatatlan böngésző futtatása a hitelesítés elkapásához."""
    res = {'token': None, 'device_id': None, 'html': ""}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, 
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        async def handle_route(route: Route):
            # Elkapjuk az Authorization fejlécet a kimenő kérésekből
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
            await page.goto(url, wait_until="networkidle", timeout=60000)
            # Várunk, hogy a Tubi lejátszója inicializálódjon és generáljon tokent
            logging.info("⏳ Várakozás a token generálódására (5mp)...")
            await asyncio.sleep(5) 
            res['html'] = await page.content()
        except Exception as e:
            logging.error(f"❌ Böngésző hiba: {e}")
            
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main():
    # Elfogadjuk a 'web' és 'url' paramétereket is
    url = request.args.get('web') or request.args.get('url')
    target_api = request.args.get('target_api') == 'true'
    season = request.args.get('season')
    
    # Ha a kliens már rendelkezik tokennel (2. kör), visszaküldi nekünk
    token = request.args.get('token')
    device_id = request.args.get('device_id')

    if not url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    html_content = ""
    # 1. KÖR: Ha nincs még token, elindítjuk a Playwright-ot
    if not token or token == "None":
        logging.info("🕵️ Böngésző indítása a hitelesítéshez...")
        auth = asyncio.run(scrape_auth_and_html(url))
        token = auth['token']
        device_id = auth['device_id']
        html_content = auth['html']
    else:
        logging.info("♻️ Meglévő token használata, böngésző átugrása.")
        html_content = "Auth provided by client."

    result = {
        "status": "success",
        "tubi_token": token,
        "tubi_device_id": device_id,
        "html_content": html_content,
        "page_data": []
    }

    # 2. KÖR: Ha minden megvan az epizódokhoz, hívjuk az API-t
    if target_api and season and token:
        c_id = extract_content_id(url)
        if c_id:
            api_data = call_content_api(c_id, token, device_id, season)
            result["page_data"].append({"page": 1, "json_content": api_data})
        else:
            result["status"] = "error"
            result["message"] = "Invalid Content ID in URL"

    return jsonify(result)

if __name__ == '__main__':
    # Render port kezelése
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
