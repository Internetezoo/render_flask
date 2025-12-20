import asyncio
import nest_asyncio
import logging
import re
import os
import requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from urllib.parse import urlparse, urljoin

# Flask + Playwright aszinkron híd
nest_asyncio.apply()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Dinamikus stealth importálás a TypeError elkerülésére
try:
    from playwright_stealth import stealth_async as stealth_func
except ImportError:
    try:
        from playwright_stealth import stealth as stealth_func
    except ImportError:
        stealth_func = None

DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def is_tubi_url(url):
    return "tubitv.com" in urlparse(url).netloc

def extract_content_id(url):
    match = re.search(r'series/(\d+)', url) or re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def fix_links(html, base_url):
    """Abszolúttá teszi a linkeket a böngészőhöz, hogy legyen CSS/Kép."""
    def replacer(match):
        attr = match.group(1)
        url = match.group(2)
        if url.startswith('/') and not url.startswith('//'):
            return f'{attr}="{urljoin(base_url, url)}"'
        return match.group(0)
    pattern = r'(href|src|action)="([^"]+)"'
    return re.sub(pattern, replacer, html)

async def run_browser_logic(url, is_tubi, full_render=False):
    data = {'html': "", 'token': None, 'device_id': None}
    async with async_playwright() as p:
        # Render kompatibilis böngésző indítás
        browser = await p.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Stealth mód aktiválása (ha elérhető a könyvtár)
        if stealth_func:
            try:
                await stealth_func(page)
            except Exception as e:
                logging.warning(f"Stealth error: {e}")

        async def handle_request(route):
            # Sebesség optimalizálás: csak azt töltjük be, ami kell
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
            # "load" várakozás a stabilitásért
            wait_strategy = "load" if full_render else "domcontentloaded"
            await page.goto(url, wait_until=wait_strategy, timeout=45000)
            
            if is_tubi:
                await asyncio.sleep(5)
            elif full_render:
                await asyncio.sleep(2)

            raw_html = await page.content()
            data['html'] = fix_links(raw_html, url) if full_render else raw_html
        except Exception as e:
            data['html'] = f"Hiba történt a betöltés során: {str(e)}"
        finally:
            await browser.close()
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    web_url = request.args.get('web')
    python_url = request.args.get('url')
    target = web_url or python_url
    if not target: return jsonify({"error": "Használd: ?web=URL vagy ?url=URL"}), 400
    
    is_tubi = is_tubi_url(target)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        res = loop.run_until_complete(run_browser_logic(target, is_tubi, full_render=bool(web_url)))
    finally:
        loop.close()

    if web_url:
        return Response(res['html'], mimetype='text/html')

    return jsonify({
        "status": "success",
        "tubi_token": res['token'],
        "tubi_device_id": res['device_id'],
        "html_content": res['html']
    })

@app.route('/')
def health():
    return "Scraper is Online!"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
