import asyncio
import nest_asyncio
import logging
import re
import os
import requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
import playwright_stealth
from urllib.parse import urlparse, urljoin

# Aszinkron hurok engedélyezése Flask alatt
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

def fix_links(html, base_url):
    """Átalakítja a relatív linkeket abszolútra, hogy localhoston is legyen CSS/Kép."""
    def replacer(match):
        attr = match.group(1)
        url = match.group(2)
        if url.startswith('/') and not url.startswith('//'):
            return f'{attr}="{urljoin(base_url, url)}"'
        return match.group(0)
    
    pattern = r'(href|src|action)="([^"]+)"'
    return re.sub(pattern, replacer, html)

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
    except Exception as e:
        return {"error": str(e)}

async def run_browser_logic(url, is_tubi, full_render=False):
    data = {'html': "", 'console_logs': [], 'token': None, 'device_id': None}
    
    async with async_playwright() as p:
        # Render.com kompatibilis indítás
        browser = await p.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Stealth mód aktiválása (Javított hívás)
        await playwright_stealth.stealth_async(page)

        page.on("console", lambda msg: data['console_logs'].append({'t': msg.type, 'x': msg.text}))

        async def handle_request(route):
            # Ha csak adat kell (?url=), blokkolunk mindent a sebességért. 
            # Ha böngészni akarunk (?web=), akkor hagyunk mindent betöltődni.
            if not full_render and route.request.resource_type in ["image", "media", "font", "stylesheet"]:
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
            # Várakozási stratégia: networkidle a teljes rendereléshez, domcontentloaded a gyors adathoz
            wait_strategy = "networkidle" if full_render else "domcontentloaded"
            await page.goto(url, wait_until=wait_strategy, timeout=60000)
            
            # Tubi esetén kell pár másodperc a tokenek lehalászásához
            if is_tubi:
                await asyncio.sleep(5)
            elif full_render:
                await asyncio.sleep(2)

            raw_html = await page.content()
            data['html'] = fix_links(raw_html, url) if full_render else raw_html
            
        except Exception as e:
            data['html'] = f"Error: {str(e)}"
        finally:
            await browser.close()
            
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    web_url = request.args.get('web')
    python_url = request.args.get('url')
    target = web_url or python_url
    
    if not target:
        return jsonify({"error": "Hasznald a ?web=URL vagy ?url=URL parametert!"}), 400
    
    is_tubi = is_tubi_url(target)
    
    # Event loop kezelése
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Ha 'web' paraméter van, full_render=True (lassabb, de szebb)
        res = loop.run_until_complete(run_browser_logic(target, is_tubi, full_render=bool(web_url)))
    finally:
        loop.close()

    if web_url:
        # Rendes HTML válasz a böngészőnek
        return Response(res['html'], mimetype='text/html')

    # JSON válasz API hívásokhoz
    response_data = {
        "status": "success", 
        "tubi_token": res['token'],
        "tubi_device_id": res['device_id'],
        "html_content": res['html']
    }
    
    if is_tubi and request.args.get('target_api') == 'true' and res['token']:
        c_id = extract_content_id(target)
        if c_id:
            response_data["api_data"] = call_content_api(c_id, res['token'], res['device_id'], request.args.get('season', '1'))

    return jsonify(response_data)

@app.route('/')
def health():
    return "Scraper is Online!"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
