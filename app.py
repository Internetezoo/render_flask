import asyncio
import nest_asyncio
import logging
import re
import os
import requests
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright
# JAVÍTOTT IMPORT: A modulból importáljuk a függvényt
from playwright_stealth import stealth 
from urllib.parse import urlparse

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
        "app_id": "tubitv", "platform": "web", "content_id": content_id,
        "device_id": device_id, "pagination[season]": str(season_num)
    }
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=15)
        return resp.json()
    except: return None

async def run_browser_logic(url, is_tubi):
    data = {'html': "", 'console_logs': [], 'token': None, 'device_id': None}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        page = await context.new_page()

        # Most már a függvényt hívjuk meg, nem a modult
        await stealth(page)

        # Konzol logok gyűjtése a kliensnek
        page.on("console", lambda msg: data['console_logs'].append({'t': msg.type, 'x': msg.text}))

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
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)
            data['html'] = await page.content()
        except Exception as e:
            data['html'] = f"Error: {str(e)}"
        finally:
            await browser.close()
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    # Paraméterek: ?web= (böngészőnek HTML) vagy ?url= (kliensnek JSON)
    web_url = request.args.get('web')
    python_url = request.args.get('url')
    target = web_url or python_url
    
    if not target: return jsonify({"error": "No URL"}), 400
    
    is_tubi = is_tubi_url(target)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        res = loop.run_until_complete(run_browser_logic(target, is_tubi))
    finally:
        loop.close()

    # HA BÖNGÉSZŐBŐL HÍVTAD -> HTML
    if web_url:
        return res['html']

    # HA KLIENSBŐL HÍVTAD -> Teljes JSON
    response = {
        "status": "success", 
        "html_content": res['html'],
        "console_logs": res['console_logs'],
        "tubi_token": res['token'],
        "tubi_device_id": res['device_id']
    }
    
    # Tubi API automatikus hívása, ha van token és kérik
    if is_tubi and request.args.get('target_api') == 'true' and res['token']:
        c_id = extract_content_id(target)
        if c_id:
            response["page_data"] = call_content_api(c_id, res['token'], res['device_id'], request.args.get('season', '1'))

    return jsonify(response)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
