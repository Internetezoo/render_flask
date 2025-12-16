import asyncio, nest_asyncio, json, logging, base64, os, requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright, Route
from typing import Optional, Dict

nest_asyncio.apply()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(message)s')

# --- KONFIGURÁCIÓK ÉS DINAMIKUS API PARAMÉTEREK ---
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
# Ez a sor biztosítja, hogy tudd, milyen paraméterekkel kell hívni a Tubi API-t a videókért
TUBI_CONTENT_API_PARAMS = "app_id=tubitv&platform=web&content_id={content_id}&device_id={device_id}&limit_resolutions[]=h264_1080p&video_resources[]=hlsv6"

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    """Ha a fejlécben nincs Device ID, a token közepéből (payload) fejtjük ki."""
    try:
        payload_part = jwt_token.split('.')[1]
        padding = '=' * (4 - len(payload_part) % 4)
        payload = json.loads(base64.b64decode(payload_part + padding).decode('utf-8'))
        return payload.get('device_id')
    except: return None

async def scrape_smart_stealth(url: str, opts: Dict):
    res = {
        'status': 'success', 'url': url, 'tubi_token': None, 
        'tubi_device_id': None, 'html_content': None
    }
    # Csak akkor hozunk létre HAR fájlt, ha a kliens kéri (opts['har'])
    har_path = f"temp_{os.getpid()}.har" if opts.get('har') else None
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        context = await browser.new_context(record_har_path=har_path) if har_path else await browser.new_context()
        page = await context.new_page()

        if opts.get('console'):
            res['console_logs'] = []
            page.on("console", lambda m: res['console_logs'].append({'t': m.type, 'x': m.text}))
        
        async def handle_route(route: Route):
            headers = route.request.headers
            auth = headers.get('authorization', '')
            
            if 'Bearer ' in auth and not res['tubi_token']:
                token = auth.split('Bearer ')[1].strip()
                res['tubi_token'] = token
                # Device ID kinyerése: 1. fejléc, 2. JWT dekódolás
                res['tubi_device_id'] = headers.get(DEVICE_ID_HEADER.lower()) or decode_jwt_payload(token)
                if opts.get('simple'):
                    res['simple_log'] = res.get('simple_log', []) + [f"Captured Token & DeviceID"]
            await route.continue_()

        await page.route("**/*", handle_route)
        
        try:
            # Networkidle: megvárja, amíg a háttér API hívások lefutnak (Token elkapás!)
            await page.goto(url, wait_until="networkidle", timeout=60000)
            res['html_content'] = await page.content()
        except Exception as e:
            res['status'], res['error'] = 'failure', str(e)

        await context.close()
        if har_path and os.path.exists(har_path):
            with open(har_path, "r", encoding="utf-8") as f:
                res['har_content'] = json.load(f)
            os.remove(har_path)
        await browser.close()
    return res

@app.route('/scrape', methods=['GET', 'POST'])
def scrape_endpoint():
    if request.method == 'POST': # Pluto Proxy / Redirect Fix
        d = request.get_json()
        try:
            r = requests.request(d.get('method', 'GET'), d['url'], headers=d.get('headers'), timeout=30, allow_redirects=True)
            return jsonify({"status": "success", "content": r.text, "finalUrl": r.url, "statusCode": r.status_code})
        except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

    url = request.args.get('url')
    if not url: return jsonify({'error': 'Missing URL'}), 400

    # Itt dől el a kliens kapcsolói alapján, mit futtatunk
    opts = {
        'har': request.args.get('har') == 'true',
        'console': request.args.get('console') == 'true',
        'simple': request.args.get('simple') == 'true'
    }
    
    data = asyncio.run(scrape_smart_stealth(url, opts))
    
    # "web" mód: csak a nyers, renderelt HTML-t adja vissza
    if request.args.get('web') == 'true':
        return Response(data.get('html_content', ''), mimetype='text/html')
    
    # "url" (alap) mód: visszaküldi a teljes JSON csomagot (Token, ID, HAR, Logs)
    return jsonify(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
