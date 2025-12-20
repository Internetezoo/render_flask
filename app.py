import asyncio
import nest_asyncio
import json
import os
import re
import logging
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
    """Kinyeri a Tubi tartalom ID-t az URL-ből."""
    match = re.search(r'/(?:series|movies|video)/(\d+)', url)
    return match.group(1) if match else None

def make_direct_content_api_call(content_id, token, device_id, season_num):
    """Közvetlen hívás a Tubi Content API-ra a már meglévő tokennel."""
    logging.info(f"📡 API HÍVÁS: ID={content_id}, Season={season_num}")
    
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
        ('video_resources[]', 'hlsv6')
    ]
    
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"API hiba: {resp.status_code}"}
    except Exception as e:
        return {"error": str(e)}

async def run_advanced_scrapper(url, need_har=False):
    """Böngésző futtatása, token elkapása és adatok gyűjtése."""
    # Protokoll kiegészítése, ha hiányzik
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    data = {
        "token": None, 
        "device_id": None, 
        "html": "", 
        "console_logs": [], 
        "simple_log": [], 
        "har_content": None
    }
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # HAR rögzítés, ha kérik
        har_path = "temp.har" if need_har else None
        context = await browser.new_context(
            record_har_path=har_path,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Eseménykezelők: Token elkapás és Network log
        async def handle_request(route):
            req = route.request
            auth = req.headers.get("authorization")
            dev_id = req.headers.get(DEVICE_ID_HEADER)
            
            if auth and "Bearer" in auth and not data["token"]:
                data["token"] = auth.replace("Bearer ", "")
                data["device_id"] = dev_id
                logging.info("🔑 Token elkapva!")
                
            data["simple_log"].append(f"{req.method} {req.url}")
            await route.continue_()

        page.on("console", lambda msg: data["console_logs"].append({"t": msg.type, "x": msg.text}))
        await page.route("**/*", handle_request)
        
        try:
            logging.info(f"🚀 Navigálás: {url}")
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(2) # Várunk a dinamikus tartalomra
            data["html"] = await page.content()
        except Exception as e:
            data["html"] = f"Navigációs hiba: {str(e)}"
            logging.error(f"❌ Hiba: {e}")
        
        await context.close() # HAR lezárása
        
        if need_har and os.path.exists("temp.har"):
            with open("temp.har", "r", encoding="utf-8") as f:
                data["har_content"] = json.load(f)
            os.remove("temp.har")
            
        await browser.close()
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    target_url = request.args.get('url')
    web_mode = request.args.get('web') == 'true'
    need_har = request.args.get('har') == 'true'
    season = request.args.get('season')
    
    if not target_url:
        return jsonify({"error": "Hiányzó 'url' paraméter!"}), 400

    # 1. LOGIKA: Gyorsítótár használata (Season kérés esetén, ha van token)
    if season and session_cache["token"] and "tubitv.com" in target_url:
        logging.info("⚡ Cache használata...")
        c_id = extract_content_id(target_url)
        api_data = make_direct_content_api_call(
            c_id, session_cache["token"], session_cache["device_id"], season
        )
        return jsonify({
            "status": "cached",
            "page_data": [api_data],
            "tubi_token": session_cache["token"]
        })

    # 2. LOGIKA: Friss lekérés böngészővel
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        browser_res = loop.run_until_complete(run_advanced_scrapper(target_url, need_har))
        
        # Mentsük el a tokent későbbre
        if browser_res["token"]:
            session_cache["token"] = browser_res["token"]
            session_cache["device_id"] = browser_res["device_id"]
    finally:
        loop.close()

    # 3. VÁLASZ ADÁSA
    if web_mode:
        return Response(browser_res["html"], mimetype='text/html')

    output = {
        "status": "success",
        "tubi_token": session_cache["token"],
        "tubi_device_id": session_cache["device_id"],
        "html_content": browser_res["html"],
        "console_logs": browser_res["console_logs"],
        "simple_log": browser_res["simple_log"],
        "har_content": browser_res["har_content"],
        "page_data": []
    }

    # Ha az első hívásban kértek évadot, lefut a Content API is
    if season and session_cache["token"] and "tubitv.com" in target_url:
        c_id = extract_content_id(target_url)
        output["page_data"] = [make_direct_content_api_call(
            c_id, session_cache["token"], session_cache["device_id"], season
        )]

    return jsonify(output)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
