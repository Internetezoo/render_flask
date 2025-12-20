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

nest_asyncio.apply()
app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.INFO)

# --- JAVÍTOTT KONFIGURÁCIÓK ---
DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

# Itt hozzáadtuk a widevine_nonclearlead-et, hogy legyenek videó linkek!
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
    # DEBUG LOG
    logging.info(f"🚀 CONTENT API HÍVÁS INDÍTÁSA -> ID: {content_id}, Season: {season_num}")
    
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    # Paraméterek összeállítása (1. oldal, 50 elem)
    query = TUBI_CONTENT_API_PARAMS.format(
        content_id=content_id, device_id=device_id, 
        season_num=season_num, page_num=1, page_size=50
    )
    
    api_url = f"{TUBI_CONTENT_API_BASE}?{query}"
    
    try:
        resp = requests.get(api_url, headers=headers, timeout=15)
        if resp.status_code == 200:
            logging.info("✅ API VÁLASZ SIKERES!")
            return [resp.json()]
        else:
            logging.error(f"❌ API HIBA: {resp.status_code} - {resp.text}")
    except Exception as e:
        logging.error(f"❌ API KIVÉTEL: {str(e)}")
    
    return []

async def run_browser_logic(url, is_tubi, full_render=False):
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
                    # DEBUG LOG A SZERVEREN
                    logging.info(f"🔑 TOKEN ELKAPVA: {data['token'][:20]}...")
                await route.continue_()
            await page.route("**/*", handle_request)

        await page.goto(url, wait_until="networkidle", timeout=60000)
        data["html"] = await page.content()
        await browser.close()
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    python_url = request.args.get('url')
    web_url = request.args.get('web')
    target = python_url or web_url
    season = request.args.get('season')

    if not target: return jsonify({"error": "Nincs URL"}), 400

    is_tubi = "tubitv.com" in target
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    res = loop.run_until_complete(run_browser_logic(target, is_tubi))

    # --- DEBUG KIÍRÁS ---
    if res['token']:
        print(f"DEBUG: Token megvan: {res['token'][:15]}...")
    else:
        print("DEBUG: Token nem található!")

    page_data = []
    # Kényszerített API hívás, ha van season és token
    if is_tubi and season and res['token']:
        c_id = extract_content_id(target)
        if c_id:
            page_data = make_paginated_api_call(c_id, res['token'], res['device_id'], season)

    return jsonify({
        "status": "success",
        "tubi_token": res['token'],
        "tubi_device_id": res['device_id'],
        "page_data": page_data,
        "html_content": res['html'] if web_url else "HTML omitted for JSON mode"
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
