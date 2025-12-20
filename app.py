import asyncio
import nest_asyncio
import logging
import re
import os
import requests
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright
# JAVÍTOTT IMPORT:
from playwright_stealth import stealth_async
from urllib.parse import urlparse

nest_asyncio.apply()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

DEVICE_ID_HEADER = "x-tubi-client-device-id"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

def is_tubi_url(url):
    return "tubitv.com" in urlparse(url).netloc

def extract_content_id(url):
    match = re.search(r'series/(\d+)', url) or re.search(r'/(\d+)/', url)
    return match.group(1) if match else None

def call_content_api(content_id, token, device_id, season_num):
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Tubi-Client-Device-ID": device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }
    params = {
        "app_id": "tubitv", "platform": "web", "content_id": content_id,
        "device_id": device_id, "pagination[season]": str(season_num)
    }
    try:
        resp = requests.get(TUBI_CONTENT_API_BASE, headers=headers, params=params, timeout=15)
        return resp.json()
    except: return None

async def run_browser_logic(url, is_tubi):
    data = {'token': None, 'device_id': None, 'html': ""}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        page = await context.new_page()

        # JAVÍTOTT HÍVÁS:
        await stealth_async(page)

        async def handle_request(route):
            if route.request.resource_type in ["image", "media", "font"]:
                await route.abort()
            else:
                if is_tubi:
                    h = route.request.headers
                    auth, dev_id = h.get("authorization"), h.get(DEVICE_ID_HEADER)
                    if auth and "Bearer" in auth: data['token'] = auth.replace("Bearer ", "").strip()
                    if dev_id: data['device_id'] = dev_id
                await route.continue_()

        await page.route("**/*", handle_request)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)
            data['html'] = await page.content()
        except Exception as e:
            data['html'] = f"Error: {str(e)}"
        finally:
            await browser.close()
    return data

@app.route('/scrape', methods=['GET'])
def scrape():
    target = request.args.get('web') or request.args.get('url')
    if not target: return jsonify({"error": "No URL"}), 400
    
    target_api = request.args.get('target_api') == 'true'
    season = request.args.get('season', '1')
    is_tubi = is_tubi_url(target)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result_data = loop.run_until_complete(run_browser_logic(target, is_tubi))
    finally:
        loop.close()

    response = {"status": "success", "html_content": result_data['html']}
    if is_tubi:
        token = result_data['token']
        response.update({"tubi_token": token, "tubi_device_id": result_data['device_id']})
        if target_api and token:
            c_id = extract_content_id(target)
            if c_id: response["page_data"] = [{"json_content": call_content_api(c_id, token, result_data['device_id'], season)}]

    return jsonify(response)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
