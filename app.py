import asyncio
import nest_asyncio
import logging
import re
import os
import requests
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

async def run_playwright_scrapper(url):
    data = {"token": None, "device_id": None, "html": ""}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        async def handle_request(route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER)
            if auth and "Bearer" in auth and not data["token"]:
                data["token"] = auth.replace("Bearer ", "")
                data["device_id"] = dev_id
                logging.info("🔑 Token elkapva!")
            await route.continue_()

        await page.route("**/*", handle_request)
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(5)
        data["html"] = await page.content()
        await browser.close()
    return data

@app.route('/scrape', methods=['GET', 'POST'])
def scrape():
    # --- ADATOK KINYERÉSE (POST JSON VAGY GET ARGS) ---
    if request.method == 'POST':
        post_data = request.get_json() or {}
        web_url = post_data.get('web')
        python_url = post_data.get('url')
        season = post_data.get('season')
    else:
        web_url = request.args.get('web')
        python_url = request.args.get('url')
        season = request.args.get('season')
    
    target = web_url or python_url
    if not target:
        return jsonify({"error": "Hiányzó URL!", "status": "error"}), 400

    # 1. LOGIKA: Gyorsítótár használata (Token már megvan)
    if season and session_cache["token"]:
        logging.info("⚡ GYORSÍTÓTÁR: Közvetlen Content API hívás.")
        c_id = extract_content_id(target)
        api_data = make_direct_content_api_call(
            c_id, session_cache["token"], session_cache["device_id"], season
        )
        return jsonify({
            "status": "success",
            "page_data": [api_data],
            "tubi_token": session_cache["token"]
        })

    # 2. LOGIKA: Playwright futtatása
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        browser_res = loop.run_until_complete(run_playwright_scrapper(target))
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
