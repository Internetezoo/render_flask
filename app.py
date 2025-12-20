import asyncio
import nest_asyncio
import logging
import re
import requests
import time
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from typing import Optional, Dict

nest_asyncio.apply()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# --- GLOBÁLIS CACHE ---
# Ez tárolja a tokent a memóriában, így nem kell minden kérésnél Playwright-ot indítani
session_cache = {
    "token": None, 
    "device_id": None,
    "last_updated": 0
}

DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'/(?:series|movies|video)/(\d+)', url)
    return match.group(1) if match else None

def make_paginated_api_call(content_id, token, device_id, season_num):
    logging.info(f"📡 SZERVER: Content API hívás indítása -> ID: {content_id}, Season: {season_num}")
    
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    # Karakterre pontos paraméterezés a DRM (Widevine) és az 50-es epizódlimit érdekében
    params = {
        "app_id": "tubitv",
        "platform": "web",
        "content_id": content_id,
        "device_id": device_id,
        "include_channels": "true",
        "pagination[season]": season_num,
        "pagination[page_in_season]": "1",
        "pagination[page_size_in_season]": "50",
        "limit_resolutions[]": ["h264_1080p", "h265_1080p"],
        "video_resources[]": [
            "hlsv6_widevine_nonclearlead", 
            "hlsv6_playready_psshv0", 
            "hlsv6_fairplay", 
            "hlsv6"
        ]
    }
    
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=20)
        if resp.status_code == 200:
            logging.info("✅ SZERVER: API válasz sikeres!")
            return [resp.json()]
        logging.error(f"❌ SZERVER: API hiba: {resp.status_code} - {resp.text}")
    except Exception as e:
        logging.error(f"❌ SZERVER: API kivétel: {e}")
    return []

async def get_token_via_playwright(url):
    logging.info(f"🌐 SZERVER: Böngésző indítása tokenért: {url}")
    res = {"token": None, "device_id": None, "html": ""}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        async def handle_request(route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER)
            if auth and "Bearer" in auth and not res["token"]:
                res["token"] = auth.replace("Bearer ", "")
                res["device_id"] = dev_id
                logging.info(f"🔑 SZERVER: Token elkapva!")
            await route.continue_()

        await page.route("**/*", handle_request)
        
        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(5) # Biztonsági várakozás a háttérfolyamatoknak
            res["html"] = await page.content()
        finally:
            await browser.close()
            
    return res

@app.route('/scrape', methods=['GET'])
def scrape():
    python_url = request.args.get('url') # JSON / API mód
    web_url = request.args.get('web')    # HTML / Böngésző mód
    season = request.args.get('season')
    target = python_url or web_url

    if not target:
        return jsonify({"error": "Hiányzó URL paraméter!"}), 400

    # 1. ELLENŐRZÉS: Van már érvényes token a cache-ben?
    # (A tokenek általában 1-2 óráig jók, itt most egyszerűen megnézzük, létezik-e)
    if season and session_cache["token"]:
        logging.info("♻️ SZERVER: Cache-elt token használata (Nincs böngésző indítás)")
        c_id = extract_content_id(target)
        if c_id:
            page_data = make_paginated_api_call(
                c_id, session_cache["token"], session_cache["device_id"], season
            )
            return jsonify({
                "status": "success",
                "tubi_token": session_cache["token"],
                "tubi_device_id": session_cache["device_id"],
                "page_data": page_data,
                "source": "cache"
            })

    # 2. HA NINCS TOKEN: Playwright indítása
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        browser_res = loop.run_until_complete(get_token_via_playwright(target))
        
        # Token mentése a cache-be a következő kérésekhez
        if browser_res["token"]:
            session_cache["token"] = browser_res["token"]
            session_cache["device_id"] = browser_res["device_id"]
            session_cache["last_updated"] = time.time()
            logging.info("--- TOKEN STÁTUSZ: SIKERESEN TÁROLVA ---")
        else:
            logging.warning("--- TOKEN STÁTUSZ: HIÁNYZIK! ---")
            
    finally:
        loop.close()

    # 3. API HÍVÁS (ha az első kérésben már benne volt a season)
    page_data = []
    if season and session_cache["token"]:
        c_id = extract_content_id(target)
        if c_id:
            page_data = make_paginated_api_call(
                c_id, session_cache["token"], session_cache["device_id"], season
            )

    # 4. VÁLASZ FORMÁZÁSA
    if web_url:
        return Response(browser_res["html"], mimetype='text/html')

    return jsonify({
        "status": "success",
        "tubi_token": session_cache["token"],
        "tubi_device_id": session_cache["device_id"],
        "page_data": page_data,
        "html_content": browser_res["html"] if not season else "API mode active"
    })

if __name__ == '__main__':
    # Render port beállítás
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
