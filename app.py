import asyncio
import nest_asyncio
import logging
import re
import os
import json
import requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from typing import Optional

nest_asyncio.apply()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# --- KONFIGURÁCIÓK ---
DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"
# JAVÍTÁS: Widevine DRM paraméter hozzáadva a videó linkekért
TUBI_CONTENT_API_PARAMS = (
    "app_id=tubitv&platform=web&content_id={content_id}&device_id={device_id}&"
    "include_channels=true&pagination%5Bseason%5D={season_num}&"
    "pagination%5Bpage_in_season%5D={page_num}&pagination%5Bpage_size_in_season%5D={page_size}&"
    "limit_resolutions[]=h264_1080p&video_resources[]=hlsv6_widevine_nonclearlead&video_resources[]=hlsv6"
)

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'/(?:series|movies|video)/(\d+)', url)
    return match.group(1) if match else None

def make_paginated_api_call(content_id, token, device_id, season_num):
    logging.info(f"🚀 CONTENT API HÍVÁS -> ID: {content_id}, Season: {season_num}")
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    query = TUBI_CONTENT_API_PARAMS.format(
        content_id=content_id, device_id=device_id, 
        season_num=season_num, page_num=1, page_size=50
    )
    api_url = f"{TUBI_CONTENT_API_BASE}?{query}"
    try:
        resp = requests.get(api_url, headers=headers, timeout=15)
        if resp.status_code == 200:
            logging.info("✅ TUBI API VÁLASZ SIKERES!")
            return [resp.json()]
    except Exception as e:
        logging.error(f"❌ API HIBA: {e}")
    return []

async def run_browser_logic(url, is_tubi):
    data = {"html": "", "token": None, "device_id": None}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        if is_tubi:
            async def handle_request(route):
                auth = route.request.headers.get("authorization")
                dev_id = route.request.headers.get(DEVICE_ID_HEADER)
                if auth and "Bearer" in auth and not data["token"]:
                    data["token"] = auth.replace("Bearer ", "")
                    data["device_id"] = dev_id
                    logging.info(f"🔑 TOKEN ELKAPVA: {data['token'][:20]}...")
                await route.continue_()
            await page.route("**/*", handle_request)

        await page.goto(url, wait_until="networkidle", timeout=60000)
        data["html"] = await page.content()
        await browser.close()
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    web_url = request.args.get('web')     # Böngészős nézethez
    python_url = request.args.get('url')   # JSON adatokhoz
    target = web_url or python_url
    season = request.args.get('season')

    if not target: return jsonify({"error": "Nincs URL"}), 400

    is_tubi = "tubitv.com" in target
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    res = loop.run_until_complete(run_browser_logic(target, is_tubi))

    # --- DEBUG LOG A SZERVER KONZOLRA ---
    if res['token']:
        print(f"--- TOKEN STÁTUSZ: MEGVAN ({res['token'][:10]}...) ---")
    else:
        print("--- TOKEN STÁTUSZ: HIÁNYZIK! ---")

    page_data = []
    if is_tubi and season and res['token']:
        c_id = extract_content_id(target)
        if c_id:
            page_data = make_paginated_api_call(c_id, res['token'], res['device_id'], season)

    # WEB KAPCSOLÓ KEZELÉSE
    if web_url:
        # Ha a 'web' paramétert használtad, HTML-t kapsz a böngészőbe
        return Response(res['html'], mimetype='text/html')

    # Alapértelmezett: JSON válasz a python programnak
    return jsonify({
        "status": "success",
        "tubi_token": res['token'],
        "tubi_device_id": res['device_id'],
        "page_data": page_data,
        "html_content": res['html']
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
