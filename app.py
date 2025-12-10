#app.py
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
from typing import Optional, Dict

# Engedélyezi az aszinkron funkciók beágyazását
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.DEBUG)

# --- LISTHANDLER OSZTÁLY a logok gyűjtésére (Változatlan) ---
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

# 1. Tubi SEARCH API URL TEMPLATE ELŐTAGJA (Visszaállítva a keresési végponthoz)
TUBI_SEARCH_API_PREFIX = (
    "https://search.production-public.tubi.io/api/v2/search?"
    "images%5Bposterarts%5D=w408h583_poster&images%5Bhero_422%5D=w422h360_hero&"
    "images%5Bhero_feature_desktop_tablet%5D=w1920h768_hero&images%5Bhero_feature_large_mobile%5D=w960h480_hero&"
    "images%5Bhero_feature_small_mobile%5D=w540h450_hero&images%5Bhero_feature%5D=w375h355_hero&"
    "images%5Blandscape_images%5D=w978h549_landscape&images%5Blinear_larger_poster%5D=w978h549_landscape&"
    "images%5Bbackgrounds%5D=w1614h906_background&images%5Btitle_art%5D=w430h180_title&"
    "search="
)

# 2. Tubi SEARCH API URL TEMPLATE UTÓTAGJA (Visszaállítva)
TUBI_SEARCH_API_SUFFIX = (
    "&include_channels=true&include_linear=true&is_kids_mode=false"
)

# 3. Tubi CONTENT API URL TEMPLATE (A helyes végpont a Content ID-hoz)
TUBI_CONTENT_API_TEMPLATE = (
    "https://content-cdn.production-public.tubi.io/api/v2/content?"
    "app_id=tubitv&platform=web&"
    "content_id={content_id}&device_id={device_id}&"
    "include_channels=true&"
    "pagination%5Bseason%5D=1&pagination%5Bpage_in_season%5D=1&pagination%5Bpage_size_in_season%5D=50&"
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

def is_tubi_url(url: str) -> bool:
    """Ellenőrzi, hogy a megadott URL a tubitv.com domainhez tartozik-e."""
    try:
        domain = urlparse(url).netloc
        return 'tubitv.com' in domain.lower()
    except Exception:
        return False

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    """Dekódolja a JWT payload részét és kinyeri a device_id-t. (Változatlan)"""
    try:
        payload_base64 = jwt_token.split('.')[1]
        padding = '=' * (4 - len(payload_base64) % 4)
        payload_decoded = base64.b64decode(payload_base64 + padding).decode('utf-8')
        payload_data = json.loads(payload_decoded)
        return payload_data.get('device_id')
    except Exception as e:
        logging.debug(f"DEBUG: [JWT HIBA] Hiba a JWT dekódolásánál: {e}") 
        return None

def make_internal_tubi_api_call(api_type: str, url: str, content_id: Optional[str], token: str, device_id: str, user_agent: str) -> Optional[Dict]:
    """A Tubi API-jának hívása a kinyert tokennel és a választott végponttal (Content/Search)."""
    if not token or not device_id:
        logging.error("Hiányzó token vagy device_id a belső API híváshoz.")
        return None

    full_api_url: str = ""
    api_name: str = ""
    
    # --- CONTENT API LOGIKA ---
    if api_type == 'content':
        if not content_id:
            logging.error("Hiányzó content_id a content API híváshoz.")
            return None
            
        full_api_url = TUBI_CONTENT_API_TEMPLATE.format(content_id=content_id, device_id=device_id)
        api_name = "CONTENT"
        
    # --- SEARCH API LOGIKA ---
    elif api_type == 'search':
        url_parsed = urlparse(url)
        search_term_raw = None

        # Kinyerési logika a search_term-re (path-ból vagy query-ből)
        query_params = parse_qs(url_parsed.query)
        search_term_raw = query_params.get('search', query_params.get('q', [None]))[0]
        
        if not search_term_raw and 'search/' in url_parsed.path:
            path_segments = url_parsed.path.rstrip('/').split('/')
            if path_segments[-2] == 'search':
                search_term_raw = path_segments[-1]
        elif not search_term_raw and url_parsed.path:
            path_segments = url_parsed.path.rstrip('/').split('/')
            if len(path_segments) > 1 and path_segments[-1]:
                search_term_raw = path_segments[-1]

        search_term = unquote(search_term_raw).replace('-', ' ') if search_term_raw else "ismeretlen"

        if search_term == 'ismeretlen':
            logging.error("Nem sikerült kinyerni a search_term-et a search API híváshoz.")
            return None

        encoded_search_term = urllib.parse.quote(search_term)
        full_api_url = f"{TUBI_SEARCH_API_PREFIX}{encoded_search_term}{TUBI_SEARCH_API_SUFFIX}"
        api_name = "SEARCH"
        
    else:
        logging.error(f"Érvénytelen api_type: {api_type}. Támogatott: content, search.")
        return None


    # Összeállítjuk a fejléceket
    request_headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": user_agent,
        DEVICE_ID_HEADER: device_id,
        "Accept": "application/json"
    }

    try:
        logging.info(f"Belső {api_name} API hívás indítása: {full_api_url[:80]}...")
        response = requests.get(full_api_url, headers=request_headers, timeout=10)
        response.raise_for_status() 
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Belső {api_name} API hívási hiba: {e}")
        return None

# --- ÚJ ASZINKRON FÜGGVÉNY a Pollinghoz (Változatlan) ---
async def wait_for_token(results: Dict, timeout: int = 15, interval: float = 0.5) -> bool:
    """Várakozik a 'tubi_token' megjelenésére a 'results' szótárban, polling módszerrel."""
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        if results.get('tubi_token'):
            return True
        await asyncio.sleep(interval)
        
    return False
# -----------------------------------------

# ----------------------------------------------------------------------
# ASZINKRON PLAYWRIGHT SCRAPE FÜGGVÉNY 
# ----------------------------------------------------------------------

async def scrape_tubitv(url: str, target_api_enabled: bool, har_enabled: bool, simple_log_enabled: bool, api_type: str) -> Dict: 
    """Betölti az oldalt és kezeli a tokent és a logokat."""
    
    results = {
        'status': 'success',
        'url': url,
        'tubi_token': None,
        'tubi_device_id': None,
        'user_agent': None,
        'tubi_api_data': None,
        'html_content': None, 
        'simple_logs': [], 
        'har_content': None 
    }
    
    # ... (logolás, browser indítás, route kezelés - Változatlan) ...
    
    root_logger = logging.getLogger()
    list_handler = None
    
    if simple_log_enabled:
        list_handler = ListHandler(results['simple_logs'])
        list_handler.setLevel(logging.DEBUG) 
        root_logger.addHandler(list_handler)
    
    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(headless=True, timeout=15000) 
            
            temp_context = await browser.new_context() 
            temp_page = await temp_context.new_page() 
            user_agent = await temp_page.evaluate('navigator.userAgent')
            await temp_context.close()
            results['user_agent'] = user_agent
            
            har_config = {'path': 'network.har', 'mode': 'minimal'} if har_enabled else {}
            
            context = await browser.new_context(
                locale='en-US', 
                timezone_id='America/New_York', 
                ignore_https_errors=True, 
                **har_config
            )
                
            page = await context.new_page()
            page.set_default_timeout(30000)

            # --- ROUTE BLOKKOLÁS ÉS KEZELÉS ---
            await page.route("**/google-analytics**", lambda route: route.abort())
            await page.route(lambda url: url.lower().endswith(('.png', '.jpg', '.gif', '.css', '.woff2', '.webp')) or 'md0.tubitv.com/web-k8s/dist' in url.lower(), lambda route: route.abort())


            if simple_log_enabled or target_api_enabled:
                
                async def handle_request_token_and_log(route: Route):
                    request = route.request
                    
                    if simple_log_enabled:
                        logging.debug(f"DEBUG: [HÁLÓZAT KÉRÉS] {request.method} - URL: {request.url}")
                    
                    if target_api_enabled:
                        headers = request.headers
                        
                        if not results['tubi_token'] and 'authorization' in headers and headers['authorization'].startswith('Bearer'):
                            token = headers['authorization'].split('Bearer ')[1].strip()
                            results['tubi_token'] = token
                            logging.info(f"🔑 Token rögzítve élő elfogással a KÉRÉS fejlécéből. (TOKEN MÉRET: {len(token)})")
                        
                        if not results['tubi_device_id'] and DEVICE_ID_HEADER.lower() in headers:
                            results['tubi_device_id'] = headers[DEVICE_ID_HEADER.lower()]
                            logging.info(f"📱 Device ID rögzítve élő elfogással a KÉRÉS fejlécéből. ({results['tubi_device_id']})")

                        if not results['tubi_device_id'] and ('tubi.io' in request.url or 'tubitv.com' in request.url):
                             query_params = parse_qs(urlparse(request.url).query)
                             device_id_from_url = query_params.get('device_id', [None])[0]
                             if device_id_from_url:
                                 results['tubi_device_id'] = device_id_from_url
                                 logging.info(f"📱 Device ID rögzítve az URL query paraméterből (Fallback 1). ({results['tubi_device_id']})")
                        
                    await route.continue_() 

                await page.route("**/*", handle_request_token_and_log)
            # --- ROUTE BLOKKOLÁS ÉS KEZELÉS VÉGE ---

            logging.info("🌐 Oldal betöltése (wait_until='domcontentloaded')...")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000) 
            
            if target_api_enabled:
                logging.info("⏳ Várakozás a token rögzítésére (Polling módszer, max. 15 másodperc)...")
                token_found = await wait_for_token(results, timeout=15)
                
                if token_found:
                    logging.info("🔑 Token sikeresen rögzítve a várakozási ciklusban.")
                elif not results.get('tubi_token'):
                    logging.warning("❌ A token nem került rögzítésre a 15 másodperces várakozási időn belül.")

            logging.info("🧹 Playwright útvonal-kezelők leállítása.")
            if simple_log_enabled or target_api_enabled:
                await page.unroute_all(behavior='ignoreErrors') 

            try:
                html_content = await page.content()
                results['html_content'] = html_content 
                logging.info("📝 A lap tartalmát (HTML) sikeresen kimentette.")
            except Exception as e_content:
                logging.error(f"❌ Hiba a lap tartalmának (HTML) kimentésekor: {e_content}")
                results['html_content'] = "ERROR: Failed to retrieve HTML content."

        except Exception as e:
            results['status'] = 'failure'
            results['error'] = f"Playwright hiba: {str(e)}"
            logging.error(f"❌ Playwright hiba: {e}")
            
        finally:
            # Szerver DEBUG Log Fogás Tisztítása
            if list_handler:
                root_logger.removeHandler(list_handler)
            
            if browser:
                 await browser.close()
            logging.info("✅ Playwright befejezve.")

            # ... (HAR fájl beolvasása és törlése - Változatlan) ...
            if har_enabled:
                try:
                    with open('network.har', 'r', encoding='utf-8') as f:
                        results['har_content'] = json.load(f)
                    os.remove('network.har')
                    logging.info("📝 HAR tartalom sikeresen kimentve.")
                except Exception as e:
                    logging.error(f"❌ Hiba a HAR mentésekor: {e}")
                    results['har_content'] = "ERROR: Failed to retrieve HAR content."
            # ----------------------------------------------------

            # 3. Kiegészítés: Device ID kinyerése a tokenből, ha hiányzik (Fallback 2)
            if target_api_enabled:
                if results['tubi_token'] and not results['tubi_device_id']:
                    device_id_from_token = decode_jwt_payload(results['tubi_token'])
                    if device_id_from_token:
                        results['tubi_device_id'] = device_id_from_token
                        logging.info("📱 Device ID kinyerve a token payloadból (Fallback 2).")

            # 4. Belső API hívás (CONTENT/SEARCH API)
            if target_api_enabled and results['tubi_token'] and results['tubi_device_id']:
                
                content_id = None
                
                # Content ID kinyerése (csak akkor kell, ha a 'content' API-t hívjuk)
                if api_type == 'content':
                    url_parsed = urlparse(url)
                    path_segments = url_parsed.path.rstrip('/').split('/')
                    # Megkeressük a Content ID-t a path-ban
                    for segment in reversed(path_segments):
                         if segment.isdigit():
                             content_id = segment
                             break
                    
                    if not content_id:
                        logging.warning("Nem sikerült kinyerni a content_id-t az URL-ből. Content API hívás kimaradt.")
                
                # API HÍVÁS A VÁLASZTOTT TÍPUSHOZ
                if api_type == 'search' or (api_type == 'content' and content_id):
                    tubi_api_data = make_internal_tubi_api_call(
                        api_type=api_type, 
                        url=url, 
                        content_id=content_id, 
                        token=results['tubi_token'], 
                        device_id=results['tubi_device_id'], 
                        user_agent=results['user_agent']
                    )
                    results['tubi_api_data'] = tubi_api_data
                    
                    if not tubi_api_data:
                        if results['status'] == 'success':
                            results['status'] = 'partial_success'
                        results['error'] = results.get('error', f"Sikertelen belső Tubi {api_type.upper()} API hívás a kinyert tokennel.")
                else:
                     # Csak akkor fut le, ha api_type='content', de content_id hiányzik.
                     logging.warning(f"A {api_type} API hívás elmaradt a hiányzó content_id miatt.")
                
            return results

# ----------------------------------------------------------------------
# FLASK ÚTVONAL KEZELÉS 
# ----------------------------------------------------------------------

@app.route('/scrape', methods=['GET'])
def scrape_tubi_endpoint():
    url = request.args.get('url')
    if not url:
        return jsonify({'status': 'failure', 'error': 'Hiányzó "url" paraméter.'}), 400
    
    initial_target_api_enabled = request.args.get('target_api', '').lower() == 'true'
    har_enabled = request.args.get('har', '').lower() == 'true'
    simple_log_enabled = request.args.get('simple_log', '').lower() == 'true'
    
    # ÚJ KAPCSOLÓ: api_type (content vagy search) - Alapértelmezett: content
    api_type = request.args.get('api_type', 'content').lower() 

    if api_type not in ['content', 'search']:
        return jsonify({'status': 'failure', 'error': f'Érvénytelen api_type: {api_type}. Támogatott értékek: content, search.'}), 400

    # JAVÍTÁS: Token/API logika csak tubitv.com esetén engedélyezett (ha a kliens kérte)
    if initial_target_api_enabled and is_tubi_url(url):
        target_api_enabled = True
        should_retry_for_token = True
    else:
        target_api_enabled = False
        should_retry_for_token = False
    
    retry_count = MAX_RETRIES if should_retry_for_token else 1 

    json_outputs_requested = any(
        request.args.get(p, '').lower() == 'true' 
        for p in ['full_json', 'har', 'simple_log', 'target_api']
    )
    html_requested = request.args.get('html', '').lower() == 'true'
    
    logging.info(f"API hívás indítása. Cél URL: {url}. Belső API hívás engedélyezve: {target_api_enabled}. API Típus: {api_type.upper()}")

    final_data = {}

    for attempt in range(1, retry_count + 1):
        logging.info(f"Kísérlet {attempt}/{retry_count} a scrape futtatására. URL: {url} (Belső API engedélyezve: {target_api_enabled}. API Típus: {api_type.upper()})")
        
        loop = asyncio.get_event_loop()
        final_data = loop.run_until_complete(scrape_tubitv(url, target_api_enabled, har_enabled, simple_log_enabled, api_type))
        
        # --- Visszatérési logika (Változatlan) ---
        
        is_only_html_requested = html_requested and not json_outputs_requested
        
        if is_only_html_requested and final_data.get('html_content') and final_data.get('status') == 'success':
              logging.info("Visszatérés (Sikeres, Tiszta HTML kinyerés).")
              return Response(final_data['html_content'], mimetype='text/html')
              
        if final_data.get('status') == 'failure' and not target_api_enabled:
              logging.info("Visszatérés (Playwright hiba nem TubiTV URL esetén).")
              return jsonify(final_data)
        
        token_present = final_data.get('tubi_token') is not None
        api_data_present = final_data.get('tubi_api_data') is not None

        if target_api_enabled and (not token_present or not api_data_present):
              if attempt < retry_count:
                  logging.warning(f"Token/API hiba TubiTV esetén. Újrapróbálkozás {attempt + 1}. kísérlet...")
                  time.sleep(3) 
                  continue
              else:
                  logging.error("A kért TubiTV adatok nem voltak kinyerhetők az összes kísérlet után sem.")
                  return jsonify(final_data)

        if final_data.get('status') == 'success' and (not target_api_enabled or (token_present and api_data_present)):
              logging.info(f"Adatok sikeresen kinyerve a(z) {attempt}. kísérletben. Visszatérés JSON-ben.")
              return jsonify(final_data)
        
        if final_data.get('status') == 'failure' and target_api_enabled:
            if attempt == retry_count:
                logging.error("A kért TubiTV adatok nem voltak kinyerhetők Playwright hiba miatt az összes kísérlet után sem.")
                return jsonify(final_data)
            logging.warning(f"Playwright hiba TubiTV esetén. Újrapróbálkozás {attempt + 1}. kísérlet...")
            time.sleep(3)
        
    return jsonify(final_data)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000))
