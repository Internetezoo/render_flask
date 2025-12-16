#app.py TUBI + ROKU + POST + WEB MODE
import asyncio
import nest_asyncio
import json
import logging
import base64
import os
import time
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright, Route, Response as PlaywrightResponse 
from urllib.parse import urlparse, parse_qs, unquote
import requests
import re
import urllib.parse
from typing import Optional, Dict, List, Any

# Engedélyezi az aszinkron funkciók beágyazását
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
# Fontos: Debug szintről Info szintre váltva, hogy kevesebb legyen a felesleges log
logging.basicConfig(level=logging.INFO) 

# --- LISTHANDLER OSZTÁLY a logok gyűjtésére ---
class ListHandler(logging.Handler):
    """Egyéni logger kezelő, amely a logüzeneteket egy listába gyűjti."""
    def __init__(self, log_list):
        super().__init__()
        self.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s')) 
        self.log_list = log_list

    def emit(self, record):
        if record.levelno >= logging.DEBUG:
            self.log_list.append(self.format(record))
# ------------------------------------------------------------------

# --- KONFIGURÁCIÓS ÁLLANDÓK ---
MAX_RETRIES = 3
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"

# 1. Tubi SEARCH API URL TEMPLATE ELŐTAGJA
TUBI_SEARCH_API_PREFIX = (
    "https://search.production-public.tubi.io/api/v2/search?"
    "images%5Bposterarts%5D=w408h583_poster&images%5Bhero_422%5D=w422h360_hero&"
    "images%5Bhero_feature_desktop_tablet%5D=w1920h768_hero&images%5Bhero_feature_large_mobile%5D=w960h480_hero&"
    "images%5Bhero_feature_small_mobile%5D=w540h450_hero&images%5Bhero_feature%5D=w375h355_hero&"
    "images%5Blandscape_images%5D=w978h549_landscape&images%5Blinear_larger_poster%5D=w978h549_landscape&"
    "images%5Bbackgrounds%5D=w1614h906_background&images%5Btitle_art%5D=w430h180_title&"
    "search="
)

# 2. Tubi SEARCH API URL TEMPLATE UTÓTAGJA
TUBI_SEARCH_API_SUFFIX = "&include_channels=true&include_linear=true&is_kids_mode=false"

# 3. Tubi CONTENT API BASE URL
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"

# 4. Tubi CONTENT API PARAMÉTER SABLON (Paginated hívásokhoz)
TUBI_CONTENT_API_PARAMS = (
    "app_id=tubitv&platform=web&"
    "content_id={content_id}&device_id={device_id}&"
    "include_channels=true&"
    "pagination%5Bseason%5D={season_num}&pagination%5Bpage_in_season%5D={page_num}&pagination%5Bpage_size_in_season%5D={page_size}&"
    "limit_resolutions%5B%5D=h264_1080p&limit_resolutions%5B%5D=h265_1080p&"
    "video_resources%5B%5D=hlsv6_widevine_nonclearlead&video_resources%5B%5D=hlsv6_playready_psshv0&video_resources%5B%5D=hlsv6_fairplay&video_resources%5B%5D=hlsv6&"
    "images%5Bposterarts%5D=w408h583_poster&images%5Bhero_422%5D=w422h360_hero&images%5Bhero_feature_desktop_tablet%5D=w1920h768_hero&images%5Bhero_feature_large_mobile%5D=w960h480_hero&"
    "images%5Bhero_feature_small_mobile%5D=w540h450_hero&images%5Bhero_feature%5D=w375h355_hero&"
    "images%5Blandscape_images%5D=w978h549_landscape&images%5Blinear_larger_poster%5D=w978h549_landscape&"
    "images%5Bbackgrounds%5D=w1614h906_background&images%5Btitle_art%5D=w430h180_title"
)
# ----------------------------------------------------------------------
# SEGÉDFÜGGVÉNYEK
# ----------------------------------------------------------------------
def extract_content_id_from_url(url: str) -> Optional[str]:
    """Kinyeri a content_id-t a tubitv.com URL path-ból."""
    url_parsed = urlparse(url)
    path_segments = url_parsed.path.rstrip('/').split('/')
    for segment in reversed(path_segments):
        if segment.isdigit():
            return segment
    return None

def is_tubi_url(url: str) -> bool:
    """Ellenőrzi, hogy a megadott URL a tubitv.com domainhez tartozik-e."""
    try:
        domain = urlparse(url).netloc
        return 'tubitv.com' in domain.lower()
    except Exception:
        return False

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    """Dekódolja a JWT payload részét és kinyeri a device_id-t."""
    try:
        payload_base64 = jwt_token.split('.')[1]
        padding = '=' * (4 - len(payload_base64) % 4)
        payload_decoded = base64.b64decode(payload_base64 + padding).decode('utf-8')
        payload_data = json.loads(payload_decoded)
        return payload_data.get('device_id')
    except Exception as e:
        logging.debug(f"DEBUG: [JWT HIBA] Hiba a JWT dekódolásánál: {e}") 
        return None

def make_paginated_tubi_api_call(
    content_id: str, 
    token: str, 
    device_id: str, 
    user_agent: str, 
    season_num: int, 
    max_pages: int, 
    page_size: int
) -> List[Dict[str, Any]]:
    """Több Content API lapot hív meg egy adott évadhoz a proxy szerverről."""
    collected_page_data: List[Dict[str, Any]] = []
    request_headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": user_agent,
        DEVICE_ID_HEADER: device_id,
        "Accept": "application/json",
    }
    for page_num in range(1, max_pages + 1):
        full_api_url = f"{TUBI_CONTENT_API_BASE}?{TUBI_CONTENT_API_PARAMS.format(content_id=content_id, device_id=device_id, season_num=season_num, page_num=page_num, page_size=page_size)}"
        logging.info(f"Belső CONTENT API hívás (S{season_num}/Lap {page_num}): {full_api_url[:80]}...")
        try:
            response = requests.get(full_api_url, headers=request_headers, timeout=10)
            response.raise_for_status() 
            json_data = response.json()
            collected_page_data.append({
                "page_number": page_num,
                "season_number": season_num,
                "page_size": page_size,
                "json_content": json_data
            })
            logging.info(f"✅ S{season_num}/Lap {page_num} sikeresen letöltve.")
        except Exception as e:
            logging.error(f"❌ S{season_num}/Lap {page_num} API hívási hiba: {e}")
            if page_num == 1: return []
    return collected_page_data

def make_internal_tubi_api_call(api_type: str, url: str, content_id: Optional[str], token: str, device_id: str, user_agent: str) -> Optional[Dict]:
    """A Tubi API-jának hívása a kinyert tokennel."""
    if not token or not device_id:
        return None
    if api_type == 'content':
        if not content_id: return None
        full_api_url = f"{TUBI_CONTENT_API_BASE}?{TUBI_CONTENT_API_PARAMS.format(content_id=content_id, device_id=device_id, season_num=1, page_num=1, page_size=50)}"
        api_name = "CONTENT"
    elif api_type == 'search':
        url_parsed = urlparse(url)
        query_params = parse_qs(url_parsed.query)
        search_term_raw = query_params.get('search', query_params.get('q', [None]))[0]
        if not search_term_raw and 'search/' in url_parsed.path:
            search_term_raw = url_parsed.path.rstrip('/').split('/')[-1]
        search_term = unquote(search_term_raw).replace('-', ' ') if search_term_raw else "ismeretlen"
        full_api_url = f"{TUBI_SEARCH_API_PREFIX}{urllib.parse.quote(search_term)}{TUBI_SEARCH_API_SUFFIX}"
        api_name = "SEARCH"
    else: return None

    try:
        headers = {"Authorization": f"Bearer {token}", "User-Agent": user_agent, DEVICE_ID_HEADER: device_id, "Accept": "application/json"}
        response = requests.get(full_api_url, headers=headers, timeout=10)
        response.raise_for_status() 
        return response.json()
    except Exception as e:
        logging.error(f"Belső {api_name} API hiba: {e}")
        return None

# ----------------------------------------------------------------------
# ASZINKRON SCRAPE FÜGGVÉNY - "WEB" MÓDDAL KIEGÉSZÍTVE
# ----------------------------------------------------------------------
async def scrape_tubitv(url: str, target_api_enabled: bool, har_enabled: bool, simple_log_enabled: bool, api_type: str, web_mode: bool = False) -> Dict: 
    results = {
        'status': 'success', 'url': url, 'tubi_token': None, 'tubi_device_id': None,
        'user_agent': None, 'tubi_api_data': None, 'html_content': None, 
        'simple_logs': [], 'har_content': None 
    }

    MAX_POLL_TIME = 40
    POLL_INTERVAL = 5
    start_time = time.time() 

    root_logger = logging.getLogger()
    list_handler = ListHandler(results['simple_logs']) if simple_log_enabled else None
    if list_handler:
        list_handler.setLevel(logging.DEBUG) 
        root_logger.addHandler(list_handler)

    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(headless=True, timeout=20000) 
            context = await browser.new_context(
                locale='en-US', timezone_id='America/New_York', ignore_https_errors=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                record_har_path='network.har' if har_enabled else None
            )
            page = await context.new_page()
            results['user_agent'] = await page.evaluate('navigator.userAgent')

            if web_mode:
                # --- "WEB" MÓD: Sima böngészés, nincs token vadászat ---
                logging.info(f"🌐 WEB MÓD AKTÍV: {url}")
                # Itt nem blokkolunk semmit a hű megjelenítésért
                await page.goto(url, wait_until="networkidle", timeout=30000)
                # Várunk egy kicsit a dinamikus elemekre
                await asyncio.sleep(2) 
                results['html_content'] = await page.content()
            
            else:
                # --- EREDETI TOKEN KERESŐ MÓD ---
                # Blokkolások a gyorsaságért
                await page.route("**/google-analytics**", lambda route: route.abort())
                await page.route(lambda u: any(x in u.lower() for x in ['.png', '.jpg', '.gif', '.css', '.woff2', '.webp']), lambda route: route.abort())

                async def handle_request(route: Route):
                    req = route.request
                    if simple_log_enabled: logging.debug(f"DEBUG: [REQ] {req.method} - {req.url}")
                    headers = req.headers
                    if not results['tubi_token'] and 'authorization' in headers and 'Bearer' in headers['authorization']:
                        results['tubi_token'] = headers['authorization'].split('Bearer ')[1].strip()
                    if not results['tubi_device_id'] and DEVICE_ID_HEADER.lower() in headers:
                        results['tubi_device_id'] = headers[DEVICE_ID_HEADER.lower()]
                    await route.continue_()

                await page.route("**/*", handle_request)
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)

                # Polling
                logging.info(f"⏳ Token polling indítása...")
                while not results['tubi_token'] and (time.time() - start_time) < MAX_POLL_TIME:
                    if results['tubi_token']: break
                    await asyncio.sleep(POLL_INTERVAL)
                
                results['html_content'] = await page.content()
                await page.unroute_all(behavior='ignoreErrors')

            # Utólagos belső API hívás (ha nem web mód és kérték)
            if not web_mode and target_api_enabled and results['tubi_token']:
                if not results['tubi_device_id']: results['tubi_device_id'] = decode_jwt_payload(results['tubi_token'])
                results['tubi_api_data'] = make_internal_tubi_api_call(api_type, url, extract_content_id_from_url(url), results['tubi_token'], results['tubi_device_id'], results['user_agent'])

        except Exception as e:
            results['status'] = 'failure'
            results['error'] = f"Playwright hiba: {str(e)}"
            logging.error(f"❌ Hiba: {e}")
        finally:
            if list_handler: root_logger.removeHandler(list_handler)
            if browser: await browser.close()

            if har_enabled and os.path.exists('network.har'):
                with open('network.har', 'r', encoding='utf-8') as f: results['har_content'] = json.load(f)
                os.remove('network.har')
            
    return results

# ----------------------------------------------------------------------
# FLASK ÚTVONAL KEZELÉS
# ----------------------------------------------------------------------
@app.route('/scrape', methods=['GET', 'POST'])
def scrape_tubi_endpoint():
    # 1. GENERIKUS PROXY POST
    if request.method == 'POST':
        try:
            proxy_data = request.get_json()
            if not proxy_data: return jsonify({'status': 'failure', 'error': 'No JSON'}), 400
            target_url = proxy_data.get('url')
            if not target_url: return jsonify({'status': 'failure', 'error': 'No URL'}), 400
            
            response = requests.request(
                method=proxy_data.get('method', 'GET').upper(),
                url=target_url,
                headers=proxy_data.get('headers', {}),
                json=proxy_data.get('json_data'),
                timeout=15 
            )
            return jsonify({
                "status": "success", "statusCode": response.status_code,
                "headers": dict(response.headers), "content": response.text 
            })
        except Exception as e:
            return jsonify({"status": "failure", "error": str(e)}), 500

    # 2. GET SCRAPE
    url = request.args.get('url')
    if not url: return jsonify({'status': 'failure', 'error': 'Hiányzó url'}), 400

    # Paraméterek
    web_mode = request.args.get('web', '').lower() == 'true'
    target_api = request.args.get('target_api', '').lower() == 'true'
    har = request.args.get('har', '').lower() == 'true'
    simple_log = request.args.get('simple_log', '').lower() == 'true'
    api_type = request.args.get('api_type', 'content').lower()
    
    # Évad paraméterek
    s_num, m_pages, p_size = request.args.get('season'), request.args.get('pages'), request.args.get('page_size')
    is_season = all([s_num, m_pages, p_size])

    loop = asyncio.get_event_loop()
    final_data = loop.run_until_complete(scrape_tubitv(url, (target_api or is_season), har, simple_log, api_type, web_mode))

    # WEB MÓD: Azonnali HTML válasz
    if web_mode and final_data.get('status') == 'success':
        return Response(final_data.get('html_content', ''), mimetype='text/html')

    # ÉVAD LETÖLTÉS
    if is_season and final_data.get('tubi_token') and final_data.get('tubi_device_id'):
        cid = extract_content_id_from_url(url)
        if cid:
            final_data['page_data'] = make_paginated_tubi_api_call(
                cid, final_data['tubi_token'], final_data['tubi_device_id'], 
                final_data.get('user_agent', 'Mozilla/5.0'), int(s_num), int(m_pages), int(p_size)
            )

    return jsonify(final_data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000))
