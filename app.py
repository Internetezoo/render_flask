import asyncio, nest_asyncio, json, logging, base64, os, requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright, Route
from typing import Optional, Dict

nest_asyncio.apply()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- GLOBÁLIS LOGIKA ÉS KONFIGURÁCIÓ ---
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_PARAMS = "app_id=tubitv&platform=web&content_id={content_id}&device_id={device_id}&limit_resolutions[]=h264_1080p&video_resources[]=hlsv6"

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    """JWT Token dekódolása a Device ID kinyeréséhez, ha a fejléc hiányzik."""
    try:
        payload_part = jwt_token.split('.')[1]
        padding = '=' * (4 - len(payload_part) % 4)
        payload = json.loads(base64.b64decode(payload_part + padding).decode('utf-8'))
        return payload.get('device_id')
    except Exception: return None

async def scrape_smart_stealth(url: str, opts: Dict):
    res = {
        'status': 'success', 'url': url, 'tubi_token': None, 
        'tubi_device_id': None, 'html_content': None,
        'console_logs': [], 'har_content': None, 'simple_log': []
    }
    har_path = f"temp_{os.getpid()}.har" if opts.get('har') else None
    
    async with async_playwright() as p:
        # Smart Stealth: Automatizáció elrejtése
        browser = await p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        context = await browser.new_context(record_har_path=har_path) if har_path else await browser.new_context()
        page = await context.new_page()

        # Konzol logok gyűjtése (Opcionális)
        if opts.get('console'):
            page.on("console", lambda m: res['console_logs'].append({'t': m.type, 'x': m.text}))
        
        async def handle_route(route: Route):
            auth = route.request.headers.get('authorization', '')
            # Bearer Token elcsípése
            if 'Bearer ' in auth and not res['tubi_token']:
                token = auth.split('Bearer ')[1].strip()
                res['tubi_token'] = token
                # Device ID meghatározása (Fejléc VAGY JWT Payload)
                res['tubi_device_id'] = route.request.headers.get(DEVICE_ID_HEADER.lower()) or decode_jwt_payload(token)
                res['simple_log'].append(f"🔑 [AUTH] Token elkapva: {token[:15]}...")
                res['simple_log'].append(f"🆔 [DEVICE] ID: {res['tubi_device_id']}")
            
            # Simple Log: Minden hálózati kérés rögzítése (Opcionális)
            if opts.get('simple'):
                res['simple_log'].append(f"{route.request.method} | {route.request.url[:120]}")
            
            await route.continue_()

        await page.route("**/*", handle_route)
        
        try:
            # Várakozás, amíg a hálózat elcsendesedik (biztos token elkapás)
            await page.goto(url, wait_until="networkidle", timeout=60000)
            res['html_content'] = await page.content()
        except Exception as e:
            res['status'], res['error'] = 'failure', str(e)

        await context.close()
        # HAR tartalom beolvasása ha kérték
        if har_path and os.path.exists(har_path):
            with open(har_path, "r", encoding="utf-8") as f:
                res['har_content'] = json.load(f)
            os.remove(har_path)
        await browser.close()
    return res

@app.route('/scrape', methods=['GET', 'POST'])
def scrape_endpoint():
    # POST ág: Pluto TV Redirect Fix & Proxy
    if request.method == 'POST':
        d = request.get_json()
        try:
            r = requests.request(d.get('method', 'GET'), d['url'], headers=d.get('headers'), timeout=30, allow_redirects=True)
            return jsonify({"status": "success", "content": r.text, "finalUrl": r.url, "statusCode": r.status_code})
        except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

    # GET ág: Tubi / Roku / Smart Stealth Scrape
    url = request.args.get('url')
    if not url: return jsonify({'error': 'Missing URL'}), 400

    opts = {
        'har': request.args.get('har') == 'true',
        'console': request.args.get('console') == 'true',
        'simple': request.args.get('simple') == 'true'
    }
    
    data = asyncio.run(scrape_smart_stealth(url, opts))
    
    # Web Mode: Csak a renderelt HTML
    if request.args.get('web') == 'true':
        return Response(data.get('html_content', ''), mimetype='text/html')
    
    # Alap Mode: A teljes JSON csomag (API paraméterekkel kiegészítve a válaszban)
    data['api_template'] = TUBI_CONTENT_API_PARAMS
    return jsonify(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
