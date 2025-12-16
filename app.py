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

# Aszinkron loop engedélyezése Flask alatt
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# Logging beállítása
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- KONFIGURÁCIÓK ---
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_PARAMS = (
    "app_id=tubitv&platform=web&content_id={content_id}&device_id={device_id}"
    "&limit_resolutions[]=h264_1080p&video_resources[]=hlsv6&include_channels=true"
)

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    """JWT Tokenből a Device ID kinyerése."""
    try:
        parts = jwt_token.split('.')
        if len(parts) < 2: return None
        p = parts[1]
        padding = '=' * (4 - len(p) % 4)
        return json.loads(base64.b64decode(p + padding).decode('utf-8')).get('device_id')
    except Exception as e:
        logging.error(f"JWT Decode Error: {e}")
        return None

async def scrape_full_stealth(url: str, opts: Dict):
    """Playwright alapú scraper minden extra funkcióval."""
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
    
    har_filename = f"traffic_{int(time.time())}.har"
    har_path = har_filename if opts.get('har') else None
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, 
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )
        
        context = await browser.new_context(
            record_har_path=har_path,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        ) if har_path else await browser.new_context()
        
        page = await context.new_page()

        # Konzol naplózás
        page.on("console", lambda m: res['console_logs'].append({'t': m.type, 'x': m.text}))
        
        async def handle_route(route: Route):
            auth = route.request.headers.get('authorization', '')
            if 'Bearer ' in auth and not res['tubi_token']:
                token = auth.split('Bearer ')[1].strip()
                res['tubi_token'] = token
                res['tubi_device_id'] = route.request.headers.get(DEVICE_ID_HEADER.lower()) or decode_jwt_payload(token)
                res['simple_log'].append(f"🔑 [AUTH] Token elkapva!")

            if opts.get('simple'):
                res['simple_log'].append(f"{route.request.method} | {route.request.url[:110]}")
            
            await route.continue_()

        await page.route("**/*", handle_route)
        
        try:
            logging.info(f"🚀 Betöltés: {url}")
            await page.goto(url, wait_until="networkidle", timeout=60000)
            # Várunk, hogy a Pluto/Tubi szkriptek lefussanak
            await page.wait_for_timeout(5000)
            res['html_content'] = await page.content()
        except Exception as e:
            logging.error(f"Hiba: {e}")
            res['status'], res['error'] = 'failure', str(e)

        await context.close()
        
        if har_path and os.path.exists(har_path):
            with open(har_path, "r", encoding="utf-8") as f:
                res['har_content'] = json.load(f)
            os.remove(har_path)

        await browser.close()
    return res

@app.route('/scrape', methods=['GET', 'POST'])
def handle():
    # --- POST ÁG (Közvetlen Proxy / Redirect Fix) ---
    if request.method == 'POST':
        d = request.get_json()
        try:
            r = requests.request(
                method=d.get('method', 'GET'),
                url=d['url'],
                headers=d.get('headers'),
                timeout=30,
                allow_redirects=True # Pluto TV 404 fix
            )
            return jsonify({
                "status": "success", 
                "content": r.text, 
                "finalUrl": r.url, 
                "statusCode": r.status_code
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- GET ÁG (Dinamikus választó: url vagy web) ---
    
    # Prioritás: ha van 'web', az a cél, ha nincs, akkor az 'url'
    target_url = request.args.get('web') or request.args.get('url')
    
    if not target_url:
        return "Hiba: Adj meg ?url= vagy ?web= paramétert!", 400

    # Opciók a kérésből
    opts = {
        'har': request.args.get('har') == 'true',
        'console': True, # Mindig gyűjtjük, a JSON-ben benne lesz
        'simple': request.args.get('simple') == 'true' or request.args.get('web') is not None
    }

    # Playwright futtatása
    data = asyncio.run(scrape_full_stealth(target_url, opts))

    # HA a felhasználó a 'web' paramétert használta (pl. yt-dlp vagy böngésző)
    if request.args.get('web'):
        logging.info(f"🌐 [WEB MODE] HTML válasz: {target_url}")
        return Response(data.get('html_content', 'Betöltési hiba'), mimetype='text/html')

    # HA a felhasználó az 'url' paramétert használta (Python kliens)
    logging.info(f"📊 [API MODE] JSON válasz: {target_url}")
    data['api_template'] = TUBI_CONTENT_API_PARAMS
    return jsonify(data)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
