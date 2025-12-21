import asyncio
import nest_asyncio
import logging
import re
import os
import requests
import json
import uuid
import base64
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright
from typing import Optional

# Engedélyezi az aszinkron futást Flask alatt
nest_asyncio.apply()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# --- GLOBÁLIS KONSTANSOK ÉS MUNKAMENET ---
PLUTO_BOOT_BASE_URL = "https://boot.pluto.tv/v4/start"
DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

session_cache = {
    "tubi_token": None,
    "tubi_device_id": None,
    "roku_csrf": None
}

# --- SEGÉDFÜGGVÉNYEK ---
def extract_tubi_id(url: str) -> Optional[str]:
    match = re.search(r'/(?:series|movies|video)/(\d+)', url)
    return match.group(1) if match else None

def is_roku_url(url: str) -> bool:
    return "therokuchannel.roku.com" in url

def is_pluto_url(url: str) -> bool:
    return "pluto.tv" in url

# --- DIREKT API HÍVÁSOK ---
def make_direct_tubi_call(content_id, token, device_id, season_num):
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    params = [
        ('app_id', 'tubitv'), ('platform', 'web'), ('content_id', content_id),
        ('device_id', device_id), ('pagination[season]', str(season_num)),
        ('video_resources[]', 'hlsv6_widevine_nonclearlead')
    ]
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=15)
        return resp.json() if resp.status_code == 200 else {"error": f"Tubi API hiba: {resp.status_code}"}
    except Exception as e:
        return {"error": str(e)}

def get_pluto_token_direct(target_url):
    """Pluto TV specifikus token kérés"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    }
    try:
        logging.info(f"🛰️ Pluto TV API hívás: {target_url}")
        resp = requests.get(target_url, headers=headers, timeout=20)
        # A kliens kódja (pluto_token_extractor_RENDER.py) statusCode-ot és content-et vár
        return {
            "statusCode": resp.status_code,
            "content": resp.text
        }
    except Exception as e:
        return {"statusCode": 500, "content": str(e)}

# --- BÖNGÉSZŐ ALAPÚ SCRAPER ---
async def run_playwright_scrapper(url):
    data = {"tubi_token": None, "tubi_device_id": None, "roku_csrf": None, "html": ""}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        async def handle_request(route):
            auth = route.request.headers.get("authorization")
            if auth and "Bearer" in auth:
                data["tubi_token"] = auth.replace("Bearer ", "")
                data["tubi_device_id"] = route.request.headers.get(DEVICE_ID_HEADER)
            
            csrf = route.request.headers.get("csrf-token")
            if csrf:
                data["roku_csrf"] = csrf
            await route.continue_()

        await page.route("**/*", handle_request)
        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(5) 
            data["html"] = await page.content()
        except Exception as e:
            logging.error(f"Playwright hiba: {e}")
        finally:
            await browser.close()
    return data

# --- FŐ VÉGPONT ---
@app.route('/scrape', methods=['GET', 'POST'])
def scrape():
    req_data = request.get_json() if request.method == 'POST' else request.args
    web_url = req_data.get('web')
    target_url = req_data.get('url') or web_url
    season = req_data.get('season')

    if not target_url:
        return jsonify({"error": "Hiányzó URL!"}), 400

    # 1. PLUTO TV LOGIKA (Gyors hívás, nincs szükség böngészőre)
    if is_pluto_url(target_url):
        logging.info("🌟 Pluto TV kérés feldolgozása")
        return jsonify(get_pluto_token_direct(target_url))

    # 2. ROKU V3 DIREKT POST LOGIKA
    json_payload = req_data.get('json_data')
    if is_roku_url(target_url) and request.method == 'POST' and json_payload:
        logging.info(f"📡 ROKU V3 DIREKT HÍVÁS")
        headers = req_data.get('headers', {})
        try:
            resp = requests.post(target_url, json=json_payload, headers=headers, timeout=20)
            return jsonify({
                "status": "success",
                "statusCode": resp.status_code,
                "content": resp.text
            })
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)})

    # 3. TUBI GYORSÍTÓTÁR LOGIKA
    if not is_roku_url(target_url) and season and session_cache["tubi_token"]:
        logging.info("⚡ TUBI GYORSÍTÓTÁR: Közvetlen API hívás")
        content_id = extract_tubi_id(target_url)
        api_data = make_direct_tubi_call(
            content_id, session_cache["tubi_token"], session_cache["tubi_device_id"], season
        )
        return jsonify({
            "status": "success",
            "tubi_token": session_cache["tubi_token"],
            "html_content": api_data
        })

    # 4. PLAYWRIGHT LOGIKA (Ha semmi más nem illik)
    logging.info(f"🌐 PLAYWRIGHT INDÍTÁSA: {target_url}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        browser_res = loop.run_until_complete(run_playwright_scrapper(target_url))
        if browser_res["tubi_token"]:
            session_cache["tubi_token"] = browser_res["tubi_token"]
            session_cache["tubi_device_id"] = browser_res["tubi_device_id"]
        if browser_res["roku_csrf"]:
            session_cache["roku_csrf"] = browser_res["roku_csrf"]
    finally:
        loop.close()

    if web_url:
        return Response(browser_res["html"], mimetype='text/html')

    output = {
        "status": "success",
        "tubi_token": session_cache["tubi_token"],
        "roku_csrf": session_cache["roku_csrf"],
        "html_content": browser_res["html"]
    }

    if not is_roku_url(target_url) and season and session_cache["tubi_token"]:
        content_id = extract_tubi_id(target_url)
        output["html_content"] = make_direct_tubi_call(
            content_id, session_cache["tubi_token"], session_cache["tubi_device_id"], season
        )

    return jsonify(output)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
