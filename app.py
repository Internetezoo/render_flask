import asyncio
import nest_asyncio
import logging
import re
import os
import requests
import time
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from typing import Optional

# Flask + Playwright aszinkron híd
nest_asyncio.apply()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# --- GLOBÁLIS MUNKAMENET TÁROLÓ ---
# Ez tárolja a tokent, hogy a második hívás villámgyors legyen
session_cache = {
    "token": None,
    "device_id": None
}

DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'/(?:series|movies|video)/(\d+)', url)
    return match.group(1) if match else None

def make_paginated_api_call(content_id, token, device_id, season_num):
    """
    Ez a függvény hívja meg KARAKTERRE PONTOSAN azt az API linket, amit kértél.
    """
    logging.info(f"📡 KÖZVETLEN API HÍVÁS -> ID: {content_id}, Season: {season_num}")
    
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    # Pontos paraméterek: 50-es limit, DRM források (Widevine, Playready, Fairplay)
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
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=20)
        if resp.status_code == 200:
            logging.info("✅ API VÁLASZ SIKERES!")
            return [resp.json()]
        else:
            logging.error(f"❌ API HIBA: {resp.status_code} - {resp.text}")
    except Exception as e:
        logging.error(f"❌ API KIVÉTEL: {e}")
    return []

async def run_browser_logic(url):
    """
    Böngésző indítása a Token és Device ID ellopásához.
    """
    logging.info(f"🌐 BÖNGÉSZŐ INDÍTÁSA: {url}")
    data = {"html": "", "token": None, "device_id": None}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Hálózati forgalom figyelése a tokenhez
        async def handle_request(route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER)
            if auth and "Bearer" in auth and not data["token"]:
                data["token"] = auth.replace("Bearer ", "")
                data["device_id"] = dev_id
                logging.info(f"🔑 TOKEN ELKAPVA!")
            await route.continue_()

        await page.route("**/*", handle_request)
        
        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(5) # Várjunk, hogy minden API kérés lefusson
            data["html"] = await page.content()
        finally:
            await browser.close()
            
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    target_url = request.args.get('url')
    season = request.args.get('season')
    
    if not target_url:
        return jsonify({"error": "Hiányzó URL!"}), 400

    # --- FUNKCIÓ 1: Ha van season és van mentett token -> GYORS API MÓD ---
    if season and session_cache["token"]:
        logging.info("♻️ GYORS MÓD: Mentett token használata, nincs böngésző nyitás.")
        c_id = extract_content_id(target_url)
        if c_id:
            p_data = make_paginated_api_call(
                c_id, session_cache["token"], session_cache["device_id"], season
            )
            return jsonify({
                "status": "success",
                "tubi_token": session_cache["token"],
                "page_data": p_data,
                "html_content": "API MODE ACTIVE"
            })

    # --- FUNKCIÓ 2: Első hívás vagy nincs token -> BÖNGÉSZŐS MÓD ---
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        res = loop.run_until_complete(run_browser_logic(target_url))
        
        # Token elmentése a memóriába a következő híváshoz
        if res["token"]:
            session_cache["token"] = res["token"]
            session_cache["device_id"] = res["device_id"]
            logging.info("✅ TOKEN ELMENTVE A MEMÓRIÁBA.")
    finally:
        loop.close()

    # Ha már az első hívásnál is kértek évadot (ritka eset)
    page_data = []
    if season and session_cache["token"]:
        c_id = extract_content_id(target_url)
        page_data = make_paginated_api_call(
            c_id, session_cache["token"], session_cache["device_id"], season
        )

    return jsonify({
        "status": "success",
        "tubi_token": session_cache["token"],
        "tubi_device_id": session_cache["device_id"],
        "page_data": page_data,
        "html_content": res["html"]
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
