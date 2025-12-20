import asyncio
import nest_asyncio
import logging
import re
import requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from typing import Optional

nest_asyncio.apply()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Globális tároló a munkamenetnek
session_cache = {"token": None, "device_id": None}

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
    
    # A pontos paraméterek az 50-es limittel
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
        "video_resources[]": ["hlsv6_widevine_nonclearlead", "hlsv6_playready_psshv0", "hlsv6_fairplay", "hlsv6"]
    }
    
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=20)
        if resp.status_code == 200:
            logging.info("✅ SZERVER: API válasz sikeresen megérkezett!")
            return [resp.json()]
        logging.error(f"❌ SZERVER: API hiba: {resp.status_code}")
    except Exception as e:
        logging.error(f"❌ SZERVER: API kivétel: {e}")
    return []

async def get_token_via_playwright(url):
    logging.info(f"🌐 SZERVER: Böngésző indítása a tokenért...")
    res = {"token": None, "device_id": None, "html": ""}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        async def handle_request(route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER)
            if auth and "Bearer" in auth and not res["token"]:
                res["token"] = auth.replace("Bearer ", "")
                res["device_id"] = dev_id
                logging.info("🔑 SZERVER: Token elkapva!")
            await route.continue_()

        await page.route("**/*", handle_request)
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(5) # Biztonsági várakozás
        res["html"] = await page.content()
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def scrape():
    python_url = request.args.get('url')
    web_url = request.args.get('web')
    season = request.args.get('season')
    target = python_url or web_url

    if not target: return jsonify({"error": "Nincs URL"}), 400

    # 1. LÉPÉS: Ha van season, próbáljuk meg a cache-elt tokent használni
    if season and session_cache["token"]:
        logging.info("♻️ SZERVER: Cache-elt token használata, átugorjuk a böngészőt.")
        c_id = extract_content_id(target)
        page_data = make_paginated_api_call(c_id, session_cache["token"], session_cache["device_id"], season)
        if page_data:
            return jsonify({
                "status": "success",
                "tubi_token": session_cache["token"],
                "page_data": page_data,
                "source": "cache_api"
            })

    # 2. LÉPÉS: Ha nincs token, vagy első hívás, lefut a Playwright
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        browser_res = loop.run_until_complete(get_token_via_playwright(target))
        session_cache["token"] = browser_res["token"]
        session_cache["device_id"] = browser_res["device_id"]
    finally:
        loop.close()

    # Ha évadot kért a kliens az első hívással
    page_data = []
    if season and session_cache["token"]:
        c_id = extract_content_id(target)
        page_data = make_paginated_api_call(c_id, session_cache["token"], session_cache["device_id"], season)

    # 3. LÉPÉS: Válasz küldése
    if web_url:
        return Response(browser_res["html"], mimetype='text/html')

    return jsonify({
        "status": "success",
        "tubi_token": session_cache["token"],
        "tubi_device_id": session_cache["device_id"],
        "page_data": page_data,
        "html_content": browser_res["html"] if not season else "API mode"
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
