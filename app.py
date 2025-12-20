import asyncio
import nest_asyncio
import logging
import re
import requests
import time
import os
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from typing import Optional

nest_asyncio.apply()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# --- GLOBÁLIS TOKEN TÁROLÓ ---
token_storage = {"token": None, "device_id": None}

DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'/(?:series|movies|video)/(\d+)', url)
    return match.group(1) if match else None

def make_paginated_api_call(content_id, token, device_id, season_num):
    logging.info(f"📡 API HÍVÁS -> ID: {content_id}, Season: {season_num}")
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
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
            return [resp.json()]
    except Exception as e:
        logging.error(f"API Hiba: {e}")
    return []

async def get_token(url):
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
            await route.continue_()
        await page.route("**/*", handle_request)
        await page.goto(url, wait_until="networkidle")
        await asyncio.sleep(4)
        res["html"] = await page.content()
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def scrape():
    target = request.args.get('url') or request.args.get('web')
    season = request.args.get('season')
    
    if not target: return jsonify({"error": "No URL"}), 400

    # Ha van elmentett token ÉS évadot kérnek, ne nyissunk böngészőt!
    if season and token_storage["token"]:
        logging.info("♻️ CACHE HASZNÁLATA")
        c_id = extract_content_id(target)
        p_data = make_paginated_api_call(c_id, token_storage["token"], token_storage["device_id"], season)
        return jsonify({
            "status": "success",
            "tubi_token": token_storage["token"],
            "page_data": p_data,
            "html_content": "API MODE"
        })

    # Egyébként (vagy ha nincs token) böngésző indítása
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    browser_data = loop.run_until_complete(get_token(target))
    
    if browser_data["token"]:
        token_storage["token"] = browser_res = browser_data["token"]
        token_storage["device_id"] = browser_data["device_id"]
        logging.info("🔑 TOKEN ELMENTVE")

    p_data = []
    if season and token_storage["token"]:
        c_id = extract_content_id(target)
        p_data = make_paginated_api_call(c_id, token_storage["token"], token_storage["device_id"], season)

    return jsonify({
        "status": "success",
        "tubi_token": token_storage["token"],
        "tubi_device_id": token_storage["device_id"],
        "page_data": p_data,
        "html_content": browser_data["html"]
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
