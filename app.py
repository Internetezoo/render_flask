#app.py - FULL VERSION: TUBI + ROKU + SMART STEALTH WEB MODE
import asyncio
import nest_asyncio
import json
import logging
import base64
import os
import time
import random
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright, Route
from urllib.parse import urlparse, parse_qs, unquote
import requests
import urllib.parse
from typing import Optional, Dict, List, Any

nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.INFO)

# --- KONFIGURÁCIÓK ---
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_SEARCH_API_PREFIX = "https://search.production-public.tubi.io/api/v2/search?images%5Bposterarts%5D=w408h583_poster&images%5Bhero_422%5D=w422h360_hero&images%5Bhero_feature_desktop_tablet%5D=w1920h768_hero&images%5Bhero_feature_large_mobile%5D=w960h480_hero&images%5Bhero_feature_small_mobile%5D=w540h450_hero&images%5Bhero_feature%5D=w375h355_hero&images%5Blandscape_images%5D=w978h549_landscape&images%5Blinear_larger_poster%5D=w978h549_landscape&images%5Bbackgrounds%5D=w1614h906_background&images%5Btitle_art%5D=w430h180_title&search="
TUBI_SEARCH_API_SUFFIX = "&include_channels=true&include_linear=true&is_kids_mode=false"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"
TUBI_CONTENT_API_PARAMS = "app_id=tubitv&platform=web&content_id={content_id}&device_id={device_id}&include_channels=true&pagination%5Bseason%5D={season_num}&pagination%5Bpage_in_season%5D={page_num}&pagination%5Bpage_size_in_season%5D={page_size}&limit_resolutions%5B%5D=h264_1080p&limit_resolutions%5B%5D=h265_1080p&video_resources%5B%5D=hlsv6_widevine_nonclearlead&video_resources%5B%5D=hlsv6_playready_psshv0&video_resources%5B%5D=hlsv6_fairplay&video_resources%5B%5D=hlsv6&images%5Bposterarts%5D=w408h583_poster&images%5Bhero_422%5D=w422h360_hero&images%5Bbackgrounds%5D=w1614h906_background&images%5Btitle_art%5D=w430h180_title"

# --- SEGÉDFÜGGVÉNYEK ---
def extract_content_id_from_url(url: str) -> Optional[str]:
    path_segments = urlparse(url).path.rstrip('/').split('/')
    for segment in reversed(path_segments):
        if segment.isdigit(): return segment
    return None

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    try:
        payload_base64 = jwt_token.split('.')[1]
        padding = '=' * (4 - len(payload_base64) % 4)
        payload_decoded = base64.b64decode(payload_base64 + padding).decode('utf-8')
        return json.loads(payload_decoded).get('device_id')
    except: return None

def make_paginated_tubi_api_call(content_id, token, device_id, user_agent, season_num, max_pages, page_size):
    collected = []
    headers = {"Authorization": f"Bearer {token}", "User-Agent": user_agent, DEVICE_ID_HEADER: device_id, "Accept": "application/json"}
    for page_num in range(1, max_pages + 1):
        full_url = f"{TUBI_CONTENT_API_BASE}?{TUBI_CONTENT_API_PARAMS.format(content_id=content_id, device_id=device_id, season_num=season_num, page_num=page_num, page_size=page_size)}"
        try:
            res = requests.get(full_url, headers=headers, timeout=10)
            if res.status_code == 200: collected.append({"page_number": page_num, "season_number": season_num, "json_content": res.json()})
            else: break
        except: break
    return collected

def make_internal_tubi_api_call(api_type, url, content_id, token, device_id, user_agent):
    if not token or not device_id: return None
    if api_type == 'content' and content_id:
        full_url = f"{TUBI_CONTENT_API_BASE}?{TUBI_CONTENT_API_PARAMS.format(content_id=content_id, device_id=device_id, season_num=1, page_num=1, page_size=50)}"
    elif api_type == 'search':
        search_term = unquote(urlparse(url).path.split('/')[-1]).replace('-', ' ')
        full_url = f"{TUBI_SEARCH_API_PREFIX}{urllib.parse.quote(search_term)}{TUBI_SEARCH_API_SUFFIX}"
    else: return None
    try:
        headers = {"Authorization": f"Bearer {token}", "User-Agent": user_agent, DEVICE_ID_HEADER: device_id}
        return requests.get(full_url, headers=headers, timeout=10).json()
    except: return None

# --- ASZINKRON SCRAPER (STEALTH BEÁLLÍTÁSOKKAL) ---
async def scrape_tubitv(url: str, target_api_enabled: bool, api_type: str, web_mode: bool = False) -> Dict:
    results = {'status': 'success', 'url': url, 'tubi_token': None, 'tubi_device_id': None, 'user_agent': None, 'tubi_api_data': None, 'html_content': None}
    start_time = time.time()

    async with async_playwright() as p:
        try:
            # Böngésző indítása
            browser = await p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
            
            # STEALTH CONTEXT: Fejlécek és beállítások a blokkolás ellen
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1"
                }
            )
            
            # WebDriver tulajdonság elrejtése JavaScripttel
            page = await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            if web_mode:
                logging.info(f"🌐 SMART WEB MODE AKTÍV: {url}")
                # "load" használata a timeout elkerülésére + véletlen várakozás
                await page.goto(url, wait_until="load", timeout=30000)
                await asyncio.sleep(random.uniform(3.5, 5.5)) 
                results['html_content'] = await page.content()
            else:
                # Token vadász mód
                await page.route(lambda u: any(x in u.lower() for x in ['.png', '.jpg', '.css', 'analytics']), lambda r: r.abort())
                async def handle_request(route: Route):
                    headers = route.request.headers
                    if not results['tubi_token'] and 'authorization' in headers and 'Bearer' in headers['authorization']:
                        results['tubi_token'] = headers['authorization'].split('Bearer ')[1].strip()
                    if not results['tubi_device_id'] and DEVICE_ID_HEADER.lower() in headers:
                        results['tubi_device_id'] = headers[DEVICE_ID_HEADER.lower()]
                    await route.continue_()

                await page.route("**/*", handle_request)
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                while not results['tubi_token'] and (time.time() - start_time) < 40:
                    await asyncio.sleep(2)
                results['html_content'] = await page.content()

            results['user_agent'] = await page.evaluate('navigator.userAgent')
            if results['tubi_token'] and not results['tubi_device_id']:
                results['tubi_device_id'] = decode_jwt_payload(results['tubi_token'])

            if not web_mode and target_api_enabled and results['tubi_token']:
                results['tubi_api_data'] = make_internal_tubi_api_call(api_type, url, extract_content_id_from_url(url), results['tubi_token'], results['tubi_device_id'], results['user_agent'])
            
            await browser.close()
        except Exception as e:
            results['status'] = 'failure'
            results['error'] = f"Playwright hiba: {str(e)}"
    return results

# --- FLASK ---
@app.route('/scrape', methods=['GET', 'POST'])
def scrape_tubi_endpoint():
    if request.method == 'POST':
        try:
            proxy_data = request.get_json()
            res = requests.request(method=proxy_data.get('method', 'GET'), url=proxy_data.get('url'), headers=proxy_data.get('headers', {}), json=proxy_data.get('json_data'), timeout=15)
            return jsonify({"status": "success", "statusCode": res.status_code, "content": res.text})
        except Exception as e: return jsonify({"status": "failure", "error": str(e)}), 500

    # SMART URL ÉS WEB MODE KEZELÉS
    web_param = request.args.get('web')
    url = request.args.get('url')
    web_mode = False

    if web_param and (web_param.startswith('http://') or web_param.startswith('https://')):
        url = web_param
        web_mode = True
    else:
        web_mode = str(request.args.get('web', '')).lower() == 'true'

    if not url: return jsonify({'status': 'failure', 'error': 'Hiányzó url vagy web link'}), 400

    target_api = request.args.get('target_api', '').lower() == 'true'
    s_num, m_pages = request.args.get('season'), request.args.get('pages')
    is_season = all([s_num, m_pages])

    loop = asyncio.get_event_loop()
    final_data = loop.run_until_complete(scrape_tubitv(url, (target_api or is_season), request.args.get('api_type', 'content'), web_mode))

    if web_mode and final_data['status'] == 'success':
        return Response(final_data.get('html_content', ''), mimetype='text/html')

    if is_season and final_data.get('tubi_token'):
        cid = extract_content_id_from_url(url)
        if cid: final_data['page_data'] = make_paginated_tubi_api_call(cid, final_data['tubi_token'], final_data['tubi_device_id'], final_data['user_agent'], int(s_num), int(m_pages), int(request.args.get('page_size', '50')))

    return jsonify(final_data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
