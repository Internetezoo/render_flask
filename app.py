import asyncio
import nest_asyncio
import logging
import re
import os
import requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from typing import Optional

nest_asyncio.apply()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

session_cache = {"token": None, "device_id": None}
DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'/(?:series|movies|video)/(\d+)', url)
    return match.group(1) if match else None

def make_direct_content_api_call(content_id, token, device_id, season_num):
    logging.info(f"📡 API HÍVÁS: ID={content_id}, Season={season_num}")
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    params = [
        ('app_id', 'tubitv'), ('platform', 'web'), ('content_id', content_id),
        ('device_id', device_id), ('include_channels', 'true'),
        ('pagination[season]', str(season_num)), ('pagination[page_in_season]', '1'),
        ('pagination[page_size_in_season]', '50'), ('limit_resolutions[]', 'h264_1080p'),
        ('video_resources[]', 'hlsv6')
    ]
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=15)
        return resp.json() if resp.status_code == 200 else {"error": f"API error: {resp.status_code}"}
    except Exception as e:
        return {"error": str(e)}

async def run_playwright_scrapper(url):
    data = {"token": None, "device_id": None, "html": "", "console_logs": [], "simple_log": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        async def handle_request(route):
            auth = route.request.headers.get("authorization")
            dev_id = route.request.headers.get(DEVICE_ID_HEADER)
            if auth and "Bearer" in auth and not data["token"]:
                data["token"] = auth.replace("Bearer ", "")
                data["device_id"] = dev_id
            data["simple_log"].append(f"{route.request.method} {route.request.url}")
            await route.continue_()

        page.on("console", lambda msg: data["console_logs"].append({"t": msg.type, "x": msg.text}))
        await page.route("**/*", handle_request)
        
        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(3)
            data["html"] = await page.content()
        except Exception as e:
            data["html"] = f"Hiba: {str(e)}"
        
        await browser.close()
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    target = request.args.get('url')
    web_mode = request.args.get('web') == 'true'
    season = request.args.get('season')
    
    if not target:
        return jsonify({"error": "Hiányzó URL!"}), 400

    # Gyorsítótár (Tubi-hoz)
    if season and session_cache["token"] and "tubitv.com" in target:
        c_id = extract_content_id(target)
        api_data = make_direct_content_api_call(c_id, session_cache["token"], session_cache["device_id"], season)
        return jsonify({"status": "cached", "page_data": [api_data], "tubi_token": session_cache["token"]})

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        res = loop.run_until_complete(run_playwright_scrapper(target))
        if res["token"]:
            session_cache["token"] = res["token"]
            session_cache["device_id"] = res["device_id"]
    finally:
        loop.close()

    if web_mode:
        return Response(res["html"], mimetype='text/html')

    output = {
        "status": "success",
        "tubi_token": session_cache["token"],
        "html_content": res["html"],
        "console_logs": res["console_logs"],
        "simple_log": res["simple_log"],
        "page_data": []
    }

    if season and session_cache["token"]:
        c_id = extract_content_id(target)
        output["page_data"] = [make_direct_content_api_call(c_id, session_cache["token"], session_cache["device_id"], season)]

    return jsonify(output)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
