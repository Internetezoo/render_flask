# app.py - Teljes "Mindenes" verzió: Tubi, Roku, Pluto TV + Smart Stealth + HAR + JWT
import asyncio
import nest_asyncio
import json
import logging
import base64
import os
import time
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright, Route
import requests
from typing import Optional, Dict

# Engedélyezzük az aszinkron loop-ot Flask alatt
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# Logging beállítása
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- GLOBÁLIS KONSTRUKCIÓK ÉS PARAMÉTEREK ---
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
# Ez a sablon elengedhetetlen a Tubi API közvetlen hívásához (1080p kényszerítés)
TUBI_CONTENT_API_PARAMS = (
    "app_id=tubitv&platform=web&content_id={content_id}&device_id={device_id}"
    "&limit_resolutions[]=h264_1080p&video_resources[]=hlsv6&include_channels=true"
)

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    """
    JWT Token payload dekódolása. 
    Ha a fejlécben nincs Device ID, ebből bányásszuk ki az azonosítót.
    """
    try:
        parts = jwt_token.split('.')
        if len(parts) != 3:
            return None
        payload_part = parts[1]
        # Padding javítása a base64 dekódoláshoz
        padding = '=' * (4 - len(payload_part) % 4)
        payload_json = base64.b64decode(payload_part + padding).decode('utf-8')
        payload = json.loads(payload_json)
        return payload.get('device_id')
    except Exception as e:
        logging.error(f"❌ JWT dekódolási hiba: {str(e)}")
        return None

async def scrape_smart_stealth(url: str, opts: Dict):
    """
    Playwright alapú Smart Stealth scraper.
    Kezeli a hálózati forgalmat, elkapja a tokeneket és rögzíti a logokat.
    """
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
    
    # Egyedi HAR fájlnév generálása az ütközések elkerülésére
    har_filename = f"temp_traffic_{int(time.time())}.har"
    har_path = har_filename if opts.get('har') else None
    
    async with async_playwright() as p:
        # Smart Stealth: '--disable-blink-features=AutomationControlled' a bot-detektálás ellen
        browser = await p.chromium.launch(
            headless=True, 
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )
        
        context = await browser.new_context(
            record_har_path=har_path,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        ) if har_path else await browser.new_context()
        
        page = await context.new_page()

        # Konzol logok gyűjtése, ha kérték (4-es opció)
        if opts.get('console'):
            page.on("console", lambda m: res['console_logs'].append({'type': m.type, 'text': m.text}))
        
        async def handle_route(route: Route):
            req = route.request
            headers = req.headers
            auth = headers.get('authorization', '')
            
            # Bearer Token és Device ID kinyerése
            if 'Bearer ' in auth and not res['tubi_token']:
                token = auth.split('Bearer ')[1].strip()
                res['tubi_token'] = token
                # Első körben a fejlécből, másodikban a JWT-ből próbáljuk az ID-t
                res['tubi_device_id'] = headers.get(DEVICE_ID_HEADER.lower()) or decode_jwt_payload(token)
                res['simple_log'].append(f"🔑 [FOUND] Bearer Token elkapva!")
                res['simple_log'].append(f"🆔 [DEVICE] ID meghatározva: {res['tubi_device_id']}")
            
            # Egyszerűsített hálózati log (5-ös opció)
            if opts.get('simple'):
                res['simple_log'].append(f"{req.method} | {req.url[:110]}...")
            
            await route.continue_()

        # Minden hálózati kérés figyelése
        await page.route("**/*", handle_route)
        
        try:
            # Networkidle: Megvárja a hálózati csendet (fontos a tokenekhez)
            logging.info(f"🚀 Navigálás: {url}")
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            # Pluto TV és lassabb oldalak esetén adunk 5 mp extra időt a HAR-nak és tokeneknek
            await page.wait_for_timeout(5000)
            
            res['html_content'] = await page.content()
            logging.info("✅ Oldal sikeresen betöltve.")
        except Exception as e:
            logging.error(f"❌ Hiba a navigáció során: {str(e)}")
            res['status'], res['error'] = 'failure', str(e)

        # Kontextus lezárása (ez írja ki a HAR fájlt a lemezre)
        await context.close()
        
        # HAR beolvasása és törlése, ha kérték
        if har_path and os.path.exists(har_path):
            try:
                with open(har_path, "r", encoding="utf-8") as f:
                    res['har_content'] = json.load(f)
                os.remove(har_path)
                logging.info("📦 HAR adat beágyazva a válaszba.")
            except Exception as e:
                logging.error(f"❌ HAR beolvasási hiba: {str(e)}")

        await browser.close()
    return res

@app.route('/scrape', methods=['GET', 'POST'])
def scrape_endpoint():
    """
    A fő Flask végpont.
    POST: Pluto TV Proxy / Redirect kezelés
    GET: Tubi, Roku, Smart Scrape
    """
    # --- POST ÁG: PROXY MÓD ---
    if request.method == 'POST':
        data_in = request.get_json()
        target_url = data_in.get('url')
        if not target_url:
            return jsonify({"status": "error", "message": "URL hiányzik"}), 400
            
        try:
            # allow_redirects=True oldja meg a Pluto TV 404-es hibáját!
            r = requests.request(
                method=data_in.get('method', 'GET'),
                url=target_url,
                headers=data_in.get('headers'),
                timeout=30,
                allow_redirects=True
            )
            return jsonify({
                "status": "success",
                "content": r.text,
                "finalUrl": r.url,
                "statusCode": r.status_code
            })
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    # --- GET ÁG: SMART SCRAPE MÓD ---
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'URL paraméter megadása kötelező!'}), 400

    # Opciók kinyerése a kliens kérése alapján
    opts = {
        'har': request.args.get('har') == 'true',
        'console': request.args.get('console') == 'true',
        'simple': request.args.get('simple') == 'true'
    }
    
    # Aszinkron scraper futtatása
    scrape_data = asyncio.run(scrape_smart_stealth(url, opts))
    
    # "web" mód: csak tiszta HTML böngészőhöz
    if request.args.get('web') == 'true':
        return Response(scrape_data.get('html_content', ''), mimetype='text/html')
    
    # "url" mód: Teljes JSON válasz Pythonhoz és mentéshez
    scrape_data['api_template'] = TUBI_CONTENT_API_PARAMS
    return jsonify(scrape_data)

if __name__ == '__main__':
    # Render-kompatibilis port beállítás
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
