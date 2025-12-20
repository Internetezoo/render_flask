import asyncio
import nest_asyncio
import logging
import re
import os
import requests
import json
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from typing import Optional

# Engedélyezi az aszinkron futást Flask alatt
nest_asyncio.apply()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# --- GLOBÁLIS MUNKAMENET TÁROLÓ ---
session_cache = {
    "token": None,
    "device_id": None
}

DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'/(?:series|movies|video)/(\d+)', url)
    return match.group(1) if match else None

def make_direct_content_api_call(content_id, token, device_id, season_num):
    logging.info(f"📡 KÖZVETLEN API HÍVÁS: ID={content_id}, Season={season_num}")
    
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
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
        return {"error": f"API error: {resp.status_code}"}
    except Exception as e:
        return {"error": str(e)}

async def run_playwright_scrapper(url, record_har=False):
    """
    Böngésző indítása token elkapáshoz és opcionális HAR rögzítéshez.
    """
    data = {"token": None, "device_id": None, "html": "", "har_content": None}
    har_path = f"temp_{os.getpid()}.har"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        # HAR rögzítés beállítása, ha a kliens kérte
        context_args = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        if record_har:
            context_args["record_har_path"] = har_path

        context = await browser.new_context(**context_args)
        page = await context.new_page()

        async def handle_request(route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER)
            if auth and "Bearer" in auth and not data["token"]:
                data["token"] = auth.replace("Bearer ", "")
                data["device_id"] = dev_id
            await route.continue_()

        await page.route("**/*", handle_request)
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(5)
        
        data["html"] = await page.content()
        
        # Fontos: Le kell zárni a context-et, hogy a HAR fájl kiíródjon!
        await context.close()
        await browser.close()

    # HAR fájl beolvasása és törlése
    if record_har and os.path.exists(har_path):
        try:
            with open(har_path, "r", encoding="utf-8") as f:
                data["har_content"] = json.load(f)
            os.remove(har_path)
        except Exception as e:
            logging.error(f"HAR hiba: {e}")
            
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    web_url = request.args.get('web')
    python_url = request.args.get('url')
    season = request.args.get('season')
    want_har = request.args.get('har') == 'true'
    
    target = web_url or python_url
    if not target:
        return jsonify({"error": "Hiányzó URL!"}), 400

    # Gyorsítótárazott hívás Tubi Content API-hoz
    if season and session_cache["token"]:
        c_id = extract_content_id(target)
        api_data = make_direct_content_api_call(
            c_id, session_cache["token"], session_cache["device_id"], season
        )
        return jsonify({
            "status": "success",
            "page_data": [api_data],
            "tubi_token": session_cache["token"]
        })

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        browser_res = loop.run_until_complete(run_playwright_scrapper(target, record_har=want_har))
        if browser_res["token"]:
            session_cache["token"] = browser_res["token"]
            session_cache["device_id"] = browser_res["device_id"]
    finally:
        loop.close()

    if web_url:
        return Response(browser_res["html"], mimetype='text/html')

    output = {
        "status": "success",
        "tubi_token": session_cache["token"],
        "tubi_device_id": session_cache["device_id"],
        "html_content": browser_res["html"],
        "har_content": browser_res.get("har_content"),
        "page_data": []
    }

    if season and session_cache["token"]:
        c_id = extract_content_id(target)
        output["page_data"] = [make_direct_content_api_call(
            c_id, session_cache["token"], session_cache["device_id"], season
        )]

    return jsonify(output)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
