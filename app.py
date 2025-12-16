# app.py - TELJES MINDENES VERZIÓ (Tubi, Roku, Pluto TV Ready)
import asyncio
import nest_asyncio
import json
import logging
import base64
import os
import time
import requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright, Route
from typing import Optional, Dict

# Aszinkron környezet inicializálása Flask alatt
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# Részletes naplózás a Render konzolhoz
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- GLOBÁLIS KONSTANSOK ---
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
# Ez a sablon biztosítja a legjobb minőséget az API hívásoknál
TUBI_CONTENT_API_PARAMS = (
    "app_id=tubitv&platform=web&content_id={content_id}&device_id={device_id}"
    "&limit_resolutions[]=h264_1080p&video_resources[]=hlsv6&include_channels=true"
)

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    """JWT payload dekódolása a Device ID kinyeréséhez, ha a fejléc hiányzik."""
    try:
        parts = jwt_token.split('.')
        if len(parts) < 2: return None
        payload_part = parts[1]
        # Base64 padding javítása
        padding = '=' * (4 - len(payload_part) % 4)
        decoded = base64.b64decode(payload_part + padding).decode('utf-8')
        return json.loads(decoded).get('device_id')
    except Exception as e:
        logging.error(f"JWT Decode Error: {e}")
        return None

async def scrape_full_stealth(url: str, opts: Dict):
    """Fő kaparó logika minden funkcióval."""
    res = {
        'status': 'success',
        'url': url,
        'tubi_token': None,
        'tubi_device_id': None,
        'html_content': None,
        'console_logs': [],
        'har_content': None,
        'simple_log': []
    }
    
    # Egyedi HAR fájlnév az ütközések ellen
    har_filename = f"traffic_{int(time.time())}.har"
    har_path = har_filename if opts.get('har') else None
    
    async with async_playwright() as p:
        # Browser indítása bot-védelem elrejtésével
        browser = await p.chromium.launch(
            headless=True, 
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )
        
        context = await browser.new_context(
            record_har_path=har_path,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        ) if har_path else await browser.new_context()
        
        page = await context.new_page()

        # [4] Konzol logok figyelése
        page.on("console", lambda m: res['console_logs'].append({'t': m.type, 'x': m.text}))
        
        async def handle_route(route: Route):
            req = route.request
            headers = req.headers
            auth = headers.get('authorization', '')
            
            # Token és Device ID elcsípése
            if 'Bearer ' in auth and not res['tubi_token']:
                token = auth.split('Bearer ')[1].strip()
                res['tubi_token'] = token
                # Device ID: fejlécből VAGY dekódolva
                res['tubi_device_id'] = headers.get(DEVICE_ID_HEADER.lower()) or decode_jwt_payload(token)
                res['simple_log'].append(f"🔑 [AUTH] Token/ID elkapva!")

            # [5] Simple Network Log
            if opts.get('simple'):
                res['simple_log'].append(f"{req.method} | {req.url[:110]}...")
            
            await route.continue_()

        await page.route("**/*", handle_route)
        
        try:
            logging.info(f"🚀 Indítás: {url}")
            await page.goto(url, wait_until="networkidle", timeout=60000)
            # Extra várakozás a dinamikus tartalmakhoz (Pluto/Roku)
            await page.wait_for_timeout(5000)
            res['html_content'] = await page.content()
        except Exception as e:
            logging.error(f"Hiba: {e}")
            res['status'], res['error'] = 'failure', str(e)

        # Kontextus zárása generálja le a HAR fájlt
        await context.close()
        
        if har_path and os.path.exists(har_path):
            with open(har_path, "r", encoding="utf-8") as f:
                res['har_content'] = json.load(f)
            os.remove(har_path) # Tisztítás a szerveren

        await browser.close()
    return res

@app.route('/scrape', methods=['GET', 'POST'])
def handle_scrape():
    # --- POST: Pluto TV Redirect Fix & Proxy ---
    if request.method == 'POST':
        d = request.get_json()
        try:
            r = requests.request(
                method=d.get('method', 'GET'),
                url=d['url'],
                headers=d.get('headers'),
                timeout=30,
                allow_redirects=True # Kulcsfontosságú a 302/404 ellen!
            )
            return jsonify({"status": "success", "content": r.text, "finalUrl": r.url, "statusCode": r.status_code})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- GET: Általános Scrape ---
    url = request.args.get('url')
    if not url: return jsonify({'error': 'URL hiányzik'}), 400

    opts = {
        'har': request.args.get('har') == 'true',
        'console': request.args.get('console') == 'true',
        'simple': request.args.get('simple') == 'true'
    }
    
    data = asyncio.run(scrape_full_stealth(url, opts))
    
    # "web" mód: direkt HTML böngészőnek
    if request.args.get('web') == 'true':
        return Response(data.get('html_content', ''), mimetype='text/html')
    
    # "url" mód: JSON minden extrával
    data['api_template'] = TUBI_CONTENT_API_PARAMS
    return jsonify(data)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
