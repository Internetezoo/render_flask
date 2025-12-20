import asyncio
import nest_asyncio
import logging
import re
import os
import requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from typing import Optional

# Engedélyezi az aszinkron futást Flask alatt (Render/Local környezetben szükséges)
nest_asyncio.apply()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# --- GLOBÁLIS MUNKAMENET TÁROLÓ ---
# Itt jegyezzük meg a tokent és a device_id-t, hogy ne kelljen minden kéréshez böngészőt indítani
session_cache = {
    "token": None,
    "device_id": None
}

DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def extract_content_id(url: str) -> Optional[str]:
    """Kinyeri a numerikus Content ID-t a Tubi URL-ből."""
    match = re.search(r'/(?:series|movies|video)/(\d+)', url)
    return match.group(1) if match else None

def make_direct_content_api_call(content_id, token, device_id, season_num):
    """
    Közvetlen hívás a Tubi Content API-ra a már megszerzett tokennel.
    """
    logging.info(f"📡 KÖZVETLEN API HÍVÁS: ID={content_id}, Season={season_num}")
    
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    # Tubi API paraméterek
    params = [
        ('app_id', 'tubitv'),
        ('platform', 'web'),
        ('content_id', content_id),
        ('device_id', device_id),
        ('include_channels', 'true'),
        ('pagination[season]', str(season_num)),
        ('pagination[page_in_season]', '1'),
        ('pagination[page_size_in_season]', '50'),
        ('limit_resolutions[]', 'h264_1080p'),
        ('limit_resolutions[]', 'h265_1080p'),
        ('video_resources[]', 'hlsv6_widevine_nonclearlead'),
        ('video_resources[]', 'hlsv6_playready_psshv0'),
        ('video_resources[]', 'hlsv6_fairplay'),
        ('video_resources[]', 'hlsv6')
    ]
    
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"API error: {resp.status_code}", "status": "error"}
    except Exception as e:
        return {"error": str(e), "status": "error"}

async def run_playwright_scrapper(url):
    """
    Playwright böngésző indítása a token elkapásához és a HTML kinyeréséhez.
    """
    data = {"token": None, "device_id": None, "html": ""}
    async with async_playwright() as p:
        # headless=True szükséges a felhő alapú futtatáshoz (Render)
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Token elkapása a hálózati forgalomból (Bearer token keresése)
        async def handle_request(route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER)
            if auth and "Bearer" in auth and not data["token"]:
                data["token"] = auth.replace("Bearer ", "")
                data["device_id"] = dev_id
                logging.info("🔑 Token sikeresen elkapva!")
            await route.continue_()

        await page.route("**/*", handle_request)
        await page.goto(url, wait_until="networkidle", timeout=60000)
        # Várunk egy kicsit, hogy minden API hívás lefusson
        await asyncio.sleep(5)
        data["html"] = await page.content()
        await browser.close()
    return data

@app.route('/scrape', methods=['GET', 'POST'])
def scrape():
    """
    Fő végpont, amely kezeli a GET és POST kéréseket is.
    """
    # Adatok kinyerése a kérés típusától függően
    if request.method == 'POST':
        # Ha JSON body-ban érkezik az adat
        req_data = request.get_json() or {}
        web_url = req_data.get('web')
        python_url = req_data.get('url')
        season = req_data.get('season')
    else:
        # Ha URL paraméterekben (GET) érkezik az adat
        web_url = request.args.get('web')
        python_url = request.args.get('url')
        season = request.args.get('season')
    
    target = web_url or python_url
    if not target:
        return jsonify({"error": "Hiányzó URL!", "status": "error"}), 400

    # 1. LOGIKA: Ha van season ÉS van már érvényes tokenünk -> Közvetlen API hívás (Gyors)
    if season and session_cache["token"]:
        logging.info("⚡ GYORSÍTÓTÁR: Közvetlen Content API hívás böngésző indítása nélkül.")
        c_id = extract_content_id(target)
        api_data = make_direct_content_api_call(
            c_id, session_cache["token"], session_cache["device_id"], season
        )
        return jsonify({
            "status": "success",
            "html_content": api_data,
            "tubi_token": session_cache["token"]
        })

    # 2. LOGIKA: Ha nincs token, vagy nem season hívás -> Playwright futtatása (Lassú)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        browser_res = loop.run_until_complete(run_playwright_scrapper(target))
        
        # Elmentjük a megszerzett adatokat a későbbi hívásokhoz
        if browser_res["token"]:
            session_cache["token"] = browser_res["token"]
            session_cache["device_id"] = browser_res["device_id"]
    finally:
        loop.close()

    # 3. VÁLASZ ÖSSZEÁLLÍTÁSA
    # Ha web_url volt megadva, nyers HTML-t küldünk vissza
    if web_url:
        return Response(browser_res["html"], mimetype='text/html')

    # Ha python_url (vagy csak szimpla scrape), JSON-t adunk vissza
    output = {
        "status": "success",
        "tubi_token": session_cache["token"],
        "tubi_device_id": session_cache["device_id"],
        "html_content": browser_res["html"],
        "page_data": []
    }

    # Ha ebben a hívásban kértek évadot, de most szereztünk tokent, akkor most hívjuk le az API-t
    if season and session_cache["token"]:
        c_id = extract_content_id(target)
        api_result = make_direct_content_api_call(
            c_id, session_cache["token"], session_cache["device_id"], season
        )
        output["html_content"] = api_result # Felülírjuk a HTML-t a tiszta JSON adattal

    return jsonify(output)

if __name__ == '__main__':
    # Render.com-hoz szükséges port beállítás
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
