import asyncio
import nest_asyncio
import json
import logging
import base64
import requests
import re
import os
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright, Route
from playwright_stealth import stealth  # 'stealth_async' helyett csak 'stealth'
from urllib.parse import urlparse

nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)

DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"
PROXY_SERVER = os.environ.get("PROXY_SERVER")

def is_tubi_url(url: str) -> bool:
    return "tubitv.com" in urlparse(url).netloc

def extract_content_id(url: str):
    match = re.search(r'series/(\d+)', url) or re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def call_content_api(content_id, token, device_id, season_num):
    headers = {
        "Authorization": f"Bearer {token}",
        DEVICE_ID_HEADER: device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Origin": "https://tubitv.com",
        "Referer": "https://tubitv.com/"
    }
    params = {
        "app_id": "tubitv", "platform": "web", "content_id": content_id,
        "device_id": device_id, "pagination[season]": str(season_num),
        "pagination[page_size_in_season]": "50"
    }
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=20)
        return resp.json() if resp.status_code == 200 else {"error": resp.status_code, "msg": resp.text}
    except Exception as e:
        return {"error": str(e)}

async def smart_scraper(url: str, is_tubi: bool, use_stealth: bool):
    res = {'token': None, 'device_id': None, 'html': ""}
    
    async with async_playwright() as p:
        launch_args = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-blink-features=AutomationControlled"]
        }
        if PROXY_SERVER:
            launch_args["proxy"] = {"server": PROXY_SERVER}

        browser = await p.chromium.launch(**launch_args)
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Stealth mód alkalmazása az új szintaxis szerint
        if use_stealth:
            await stealth(page)

        async def block_aggressively(route: Route):
            # Erőforrás-takarékos mód Render-hez
            if route.request.resource_type in ["image", "media", "font"]:
                await route.abort()
            else:
                if is_tubi:
                    auth = route.request.headers.get("authorization")
                    dev_id = route.request.headers.get(DEVICE_ID_HEADER.lower())
                    if auth and "Bearer" in auth:
                        res['token'] = auth.replace("Bearer ", "").strip()
                    if dev_id:
                        res['device_id'] = dev_id
                await route.continue_()

        await page.route("**/*", block_aggressively)

        try:
            logging.info(f"🚀 Navigálás: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Dinamikus tartalom bevárása
            await asyncio.sleep(8 if is_tubi else 4)
            res['html'] = await page.content()
        except Exception as e:
            logging.error(f"❌ Hiba: {e}")
            res['html'] = f"Error: {str(e)}"
        
        await browser.close()
    return res

@app.route('/scrape', methods=['GET'])
def main():
    target_url = request.args.get('web') or request.args.get('url')
    is_web_mode = request.args.get('web') is not None
    target_api = request.args.get('target_api') == 'true'
    season = request.args.get('season', '1')

    if not target_url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    is_tubi = is_tubi_url(target_url)
    scraped_data = asyncio.run(smart_scraper(target_url, is_tubi, use_stealth=is_web_mode or is_tubi))
    
    result = {
        "status": "success",
        "is_tubi": is_tubi,
        "html_content": scraped_data['html']
    }

    if is_tubi:
        token = scraped_data['token'] or request.args.get('token')
        device_id = scraped_data['device_id'] or request.args.get('device_id') or "48882a5d-40a1-4fc3-9fb5-4a68b8f393cb"
        result.update({"tubi_token": token, "tubi_device_id": device_id})

        if target_api and token:
            c_id = extract_content_id(target_url)
            if c_id:
                api_data = call_content_api(c_id, token, device_id, season)
                result["page_data"] = [{"json_content": api_data}]

    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
