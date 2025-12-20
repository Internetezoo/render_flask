import asyncio
import nest_asyncio
import logging
import re
import os
import json
import base64
import requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from urllib.parse import urlparse, urljoin
from typing import Optional

# Flask + Playwright aszinkron híd
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.INFO)

# --- KONFIGURÁCIÓK ---
DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"
TUBI_CONTENT_API_PARAMS = (
    "app_id=tubitv&platform=web&content_id={content_id}&device_id={device_id}&"
    "include_channels=true&pagination%5Bseason%5D={season_num}&"
    "pagination%5Bpage_in_season%5D={page_num}&pagination%5Bpage_size_in_season%5D={page_size}&"
    "limit_resolutions[]=h264_1080p&video_resources[]=hlsv6"
)

# Dinamikus stealth importálás javítása
try:
    import playwright_stealth
    # Megpróbáljuk elérni az aszinkron függvényt közvetlenül
    stealth_func = getattr(playwright_stealth, 'stealth_async', None)
except ImportError:
    stealth_func = None

def is_tubi_url(url):
    """Ellenőrzi, hogy Tubi linkről van-e szó."""
    return url and "tubitv.com" in urlparse(url).netloc

def extract_content_id(url):
    """Kinyeri a film/sorozat azonosítót az URL-ből."""
    match = re.search(r'series/(\d+)', url) or re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def fix_links(html, base_url):
    """Abszolúttá teszi a relatív linkeket a megjelenítéshez."""
    def replacer(match):
        attr = match.group(1)
        url = match.group(2)
        if url.startswith('/') and not url.startswith('//'):
            return f'{attr}="{urljoin(base_url, url)}"'
        return match.group(0)
    pattern = r'(href|src|action)="([^"]+)"'
    return re.sub(pattern, replacer, html)

def make_paginated_api_call(content_id, token, device_id, season_num, pages=1, size=50):
    """Meghívja a Tubi belső API-ját az epizódokért."""
    all_pages = []
    headers = {"Authorization": f"Bearer {token}", DEVICE_ID_HEADER: device_id}
    
    for p in range(1, int(pages) + 1):
        query = TUBI_CONTENT_API_PARAMS.format(
            content_id=content_id, device_id=device_id, 
            season_num=season_num, page_num=p, page_size=size
        )
        api_url = f"{TUBI_CONTENT_API_BASE}?{query}"
        try:
            r = requests.get(api_url, headers=headers, timeout=15)
            if r.status_code == 200:
                all_pages.append({"page_number": p, "json_content": r.json()})
        except Exception as e:
            logging.error(f"API Hiba: {e}")
    return all_pages

async def run_browser_logic(url, is_tubi, full_render=False):
    """Böngésző futtatása token és HTML kinyeréséhez."""
    data = {'html': "", 'token': None, 'device_id': None}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        if stealth_func:
            try:
                await stealth_func(page)
            except Exception: pass

        async def handle_request(route):
            # Csak Tubi esetén figyeljük a fejléceket a tokenért
            if is_tubi:
                h = route.request.headers
                auth, dev_id = h.get("authorization"), h.get(DEVICE_ID_HEADER)
                if auth and "Bearer" in auth: 
                    data['token'] = auth.replace("Bearer ", "").strip()
                if dev_id: 
                    data['device_id'] = dev_id
            
            # Sebesség optimalizálás: ha nem full web nézet, tiltjuk a felesleges elemeket
            if not full_render and route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", handle_request)
        
        try:
            wait_strategy = "load" if full_render else "domcontentloaded"
            await page.goto(url, wait_until=wait_strategy, timeout=45000)
            
            if is_tubi: await asyncio.sleep(5) # Várunk a tokenre

            raw_html = await page.content()
            data['html'] = fix_links(raw_html, url) if full_render else raw_html
        finally:
            await browser.close()
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    web_url = request.args.get('web')
    python_url = request.args.get('url')
    season = request.args.get('season')
    target = web_url or python_url
    
    if not target: return jsonify({"error": "Hiányzó URL!"}), 400
    
    is_tubi = is_tubi_url(target)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        res = loop.run_until_complete(run_browser_logic(target, is_tubi, full_render=bool(web_url)))
    finally:
        loop.close()

    # Ha évadot is kértek és van token (Tubi esetén)
    page_data = []
    if is_tubi and season and res['token']:
        c_id = extract_content_id(target)
        if c_id:
            page_data = make_paginated_api_call(
                c_id, res['token'], res['device_id'], season
            )

    # Válasz összeállítása
    if web_url:
        return Response(res['html'], mimetype='text/html')

    output = {
        "status": "success",
        "tubi_token": res['token'],
        "tubi_device_id": res['device_id'],
        "page_data": page_data,
        "html_content": res['html']
    }
    return Response(json.dumps(output, ensure_ascii=False), mimetype='application/json')

@app.route('/')
def health():
    return "Tubi Scraper is Online!"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
