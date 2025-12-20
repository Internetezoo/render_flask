import asyncio
import nest_asyncio
import logging
import re
import os
import requests
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright
# JAVÍTÁS: Az aszinkron híváshoz a stealth_async szükséges
from playwright_stealth import stealth_async
from urllib.parse import urlparse

# Szükséges a Flask + Playwright aszinkron futtatásához
nest_asyncio.apply()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def is_tubi_url(url):
    return "tubitv.com" in urlparse(url).netloc

def extract_content_id(url):
    match = re.search(r'series/(\d+)', url) or re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def call_content_api(content_id, token, device_id, season_num):
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Tubi-Client-Device-ID": device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }
    params = {
        "app_id": "tubitv", 
        "platform": "web", 
        "content_id": content_id,
        "device_id": device_id, 
        "pagination[season]": str(season_num)
    }
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=15)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

async def run_browser_logic(url, is_tubi):
    data = {'html': "", 'console_logs': [], 'token': None, 'device_id': None}
    
    async with async_playwright() as p:
        # Browser indítása
        browser = await p.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # JAVÍTÁS: A stealth_async használata a modul helyett
        await stealth_async(page)

        # Konzol logok mentése
        page.on("console", lambda msg: data['console_logs'].append({'t': msg.type, 'x': msg.text}))

        # Hálózati kérések figyelése (Tubi token kinyerése)
        async def handle_request(route):
            if route.request.resource_type in ["image", "media", "font"]:
                await route.abort()
            else:
                if is_tubi:
                    h = route.request.headers
                    auth, dev_id = h.get("authorization"), h.get(DEVICE_ID_HEADER)
                    if auth and "Bearer" in auth: 
                        data['token'] = auth.replace("Bearer ", "").strip()
                    if dev_id: 
                        data['device_id'] = dev_id
                await route.continue_()

        await page.route("**/*", handle_request)
        
        try:
            # Oldal betöltése
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Várunk egy kicsit, hogy a scriptek lefussanak (pl. Cloudflare vagy Tubi auth)
            await asyncio.sleep(5)
            data['html'] = await page.content()
        except Exception as e:
            data['html'] = f"Error during scraping: {str(e)}"
        finally:
            await browser.close()
            
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    # URL kinyerése a paraméterekből
    web_url = request.args.get('web')
    python_url = request.args.get('url')
    target = web_url or python_url
    
    if not target:
        return jsonify({"error": "No URL provided. Use ?web= or ?url="}), 400
    
    is_tubi = is_tubi_url(target)
    
    # Event loop kezelése Flask alatt
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        res = loop.run_until_complete(run_browser_logic(target, is_tubi))
    except Exception as e:
        return jsonify({"error": f"Event loop error: {str(e)}"}), 500

    # HA BÖNGÉSZŐBŐL HÍVTAD (?web=) -> Nyers HTML-t adunk vissza
    if web_url:
        return res['html']

    # HA KLIENSBŐL HÍVTAD (?url=) -> JSON válasz
    response_data = {
        "status": "success", 
        "html_content": res['html'],
        "console_logs": res['console_logs'],
        "tubi_token": res['token'],
        "tubi_device_id": res['device_id']
    }
    
    # Ha Tubi és kérték az API hívást is
    if is_tubi and request.args.get('target_api') == 'true' and res['token']:
        c_id = extract_content_id(target)
        if c_id:
            season = request.args.get('season', '1')
            api_res = call_content_api(c_id, res['token'], res['device_id'], season)
            response_data["page_data"] = api_res

    return jsonify(response_data)

@app.route('/')
def index():
    return "Playwright Scraper is running. Use /scrape?web=URL"

if __name__ == '__main__':
    # Render.com-hoz szükséges port beállítás
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
