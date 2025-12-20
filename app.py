import asyncio
import nest_asyncio
import logging
import re
import requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from typing import Optional, Dict

nest_asyncio.apply()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Globális tároló a tokennek, hogy ne kelljen kétszer böngészőt nyitni
cache = {"token": None, "device_id": None}

DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def extract_content_id(url: str) -> Optional[str]:
    match = re.search(r'/(?:series|movies|video)/(\d+)', url)
    return match.group(1) if match else None

def make_paginated_api_call(content_id, token, device_id, season_num):
    logging.info(f"🚀 KÜLDÉS A TUBI API-NAK: ID={content_id}, Season={season_num}")
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    # Pontos paraméterek, amiket küldtél
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
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            logging.info("✅ API VÁLASZ SIKERES!")
            return [resp.json()]
        logging.error(f"❌ API HIBA: {resp.status_code} - {resp.text}")
    except Exception as e:
        logging.error(f"❌ API KIVÉTEL: {e}")
    return []

async def get_token_with_playwright(url):
    logging.info(f"🌐 Böngésző indítása tokenért: {url}")
    res = {"html": "", "token": None, "device_id": None}
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
                logging.info(f"🔑 TOKEN ELKAPVA A SZERVEREN!")
            await route.continue_()

        await page.route("**/*", handle_request)
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(5) # Várjunk, hogy minden betöltődjön
        res["html"] = await page.content()
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def scrape():
    web_url = request.args.get('web')
    python_url = request.args.get('url')
    season = request.args.get('season')
    target = web_url or python_url

    if not target: return jsonify({"error": "Nincs URL"}), 400

    # 1. HA VAN SEASON ÉS VAN CACHELT TOKEN -> AZONNAL API HÍVÁS
    if season and cache["token"]:
        logging.info("♻️ Cachelt token használata, nincs böngésző nyitás.")
        c_id = extract_content_id(target)
        page_data = make_paginated_api_call(c_id, cache["token"], cache["device_id"], season)
        return jsonify({
            "status": "success",
            "tubi_token": cache["token"],
            "page_data": page_data
        })

    # 2. HA NINCS TOKEN VAGY ELSŐ HÍVÁS -> BÖNGÉSZŐ INDÍTÁSA
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    res = loop.run_until_complete(get_token_with_playwright(target))

    if res["token"]:
        cache["token"] = res["token"]
        cache["device_id"] = res["device_id"]
        print(f"--- TOKEN STÁTUSZ: MEGVAN ---")
    else:
        print("--- TOKEN STÁTUSZ: HIÁNYZIK! ---")

    # Ha már az első hívásnál is volt season (ritka, de kezeljük)
    page_data = []
    if season and res["token"]:
        c_id = extract_content_id(target)
        page_data = make_paginated_api_call(c_id, res["token"], res["device_id"], season)

    if web_url:
        return Response(res['html'], mimetype='text/html')

    return jsonify({
        "status": "success",
        "tubi_token": res['token'],
        "tubi_device_id": res['device_id'],
        "page_data": page_data,
        "html_content": res['html']
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
