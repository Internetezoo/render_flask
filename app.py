#app.py - Tubi TV Scraper és Generikus Proxy Server
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
DEFAULT_REQUEST_TIMEOUT = 15
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"

# Tubi API URL SABLONOK
TUBI_SEARCH_API_PREFIX = (
    "https://search.production-public.tubi.io/api/v2/search?"
    "images%5Bposterarts%5D=w408h583_poster&images%5Bhero_422%5D=w422h360_hero&"
    "images%5Bhero_feature_desktop_tablet%5D=w1920h768_hero&images%5Bhero_feature_large_mobile%5D=w960h480_hero&"
    "images%5Bhero_feature_small_mobile%5D=w540h450_hero&images%5Bhero_feature%5D=w375h355_hero&"
    "images%5Blandscape_images%5D=w978h549_landscape&images%5Blinear_larger_poster%5D=w978h549_landscape&"
    "images%5Bbackgrounds%5D=w1614h906_background&images%5Btitle_art%5D=w430h180_title&"
    "search="
)
TUBI_SEARCH_API_SUFFIX = (
    "&include_channels=true&include_linear=true&is_kids_mode=false"
)
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"
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

# ----------------------------------------------------------------------
# SEGÉDFÜGGVÉNYEK (Tubi, API HÍVÁSOK)
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
            response = requests.get(full_api_url, headers=request_headers, timeout=DEFAULT_REQUEST_TIMEOUT)
            response.raise_for_status() 
            json_data = response.json()
            
            collected_page_data.append({
                "page_number": page_num,
                "season_number": season_num,
                "page_size": page_size,
                "json_content": json_data
            })
            logging.info(f"✅ S{season_num}/Lap {page_num} sikeresen letöltve.")

        except requests.exceptions.HTTPError as e:
            logging.error(f"❌ S{season_num}/Lap {page_num} API hívási hiba: {e}. Állapotkód: {response.status_code}")
            if page_num == 1: return []
        except Exception as e:
            logging.error(f"❌ Ismeretlen hiba S{season_num}/Lap {page_num} letöltésekor: {e}")
            
    return collected_page_data

def make_internal_tubi_api_call(api_type: str, url: str, content_id: Optional[str], token: str, device_id: str, user_agent: str) -> Optional[Dict]:
    """A Tubi API-jának hívása a kinyert tokennel (S1 Meta-adatokhoz VAGY SEARCH-höz)."""
    if not token or not device_id:
        logging.error("Hiányzó token vagy device_id a belső API híváshoz.")
        return None
        
    full_api_url = None
    api_name = "N/A"

    if api_type == 'content':
        if not content_id:
            logging.error("Hiányzó content_id a content API híváshoz.")
            return None
            
        full_api_url = f"{TUBI_CONTENT_API_BASE}?{TUBI_CONTENT_API_PARAMS.format(content_id=content_id, device_id=device_id, season_num=1, page_num=1, page_size=50)}"
        api_name = "CONTENT (S1 Metadata)"

    elif api_type == 'search':
        url_parsed = urlparse(url)
        search_term_raw = None

        query_params = parse_qs(url_parsed.query)
        search_term_raw = query_params.get('search', query_params.get('q', [None]))[0]
        
        if not search_term_raw and 'search/' in url_parsed.path:
            path_segments = urlparse(url).path.rstrip('/').split('/')
            if path_segments[-2] == 'search':
                search_term_raw = path_segments[-1]
        elif not search_term_raw and url_parsed.path:
            path_segments = urlparse(url).path.rstrip('/').split('/')
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

    request_headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": user_agent,
        DEVICE_ID_HEADER: device_id,
        "Accept": "application/json",
    }

    try:
        logging.info(f"Belső {api_name} API hívás indítása: {full_api_url[:80]}...")
        response = requests.get(full_api_url, headers=request_headers, timeout=DEFAULT_REQUEST_TIMEOUT)
        response.raise_for_status() 
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Belső {api_name} API hívási hiba: {e}")
        return None

# ----------------------------------------------------------------------
# ASZINKRON PLAYWRIGHT SCRAPE FÜGGVÉNY - TUBI SPECIFIKUS
# ----------------------------------------------------------------------

async def scrape_tubitv(url: str, target_api_enabled: bool, har_enabled: bool, simple_log_enabled: bool, api_type: str) -> Dict: 
    
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
    
    MAX_POLL_TIME = 40  
    POLL_INTERVAL = 5   
    start_time = time.time() 
    
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
            temp_page = await browser.new_page() 
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
            await page.goto(url, wait_until="domcontentloaded", timeout=15000) 
            
            
            # --- 5 MÁSODPERCENKÉNTI TOKEN POLLING ---
            if target_api_enabled:
                logging.info(f"⏳ Token ellenőrzés indítása {POLL_INTERVAL} másodpercenkénti pollinggal (Max. {MAX_POLL_TIME}s)...")
                
                while not results.get('tubi_token') and (time.time() - start_time) < MAX_POLL_TIME:
                    
                    if results.get('tubi_token'):
                        logging.info(f"🔑 Token sikeresen kinyerve a {int(time.time() - start_time)} másodperc alatt. Kilépés a pollingból.")
                        break
                        
                    elapsed_time = int(time.time() - start_time)
                    
                    if elapsed_time >= MAX_POLL_TIME:
                        logging.warning(f"❌ Elérte a maximális {MAX_POLL_TIME} másodperces várakozási időt. Kilépés a pollingból.")
                        break
                        
                    logging.debug(f"DEBUG: Token ellenőrzés (Eltelt: {elapsed_time}s / Max: {MAX_POLL_TIME}s). Vár {POLL_INTERVAL} másodpercet...")
                    await asyncio.sleep(POLL_INTERVAL)
                    
                if not results.get('tubi_token'):
                    logging.warning(f"❌ A token nem került rögzítésre a {MAX_POLL_TIME} másodperces várakozási időn belül.")
            # --- POLLING VÉGE ---


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

            # HAR fájl beolvasása és törlése
            if har_enabled:
                try:
                    with open('network.har', 'r', encoding='utf-8') as f:
                        results['har_content'] = json.load(f)
                    os.remove('network.har')
                    logging.info("📝 HAR tartalom sikeresen kimentve.")
                except Exception as e:
                    logging.error(f"❌ Hiba a HAR mentésekor: {e}")
                    results['har_content'] = "ERROR: Failed to retrieve HAR content."
            
            # 3. Kiegészítés: Device ID kinyerése a tokenből, ha hiányzik (Fallback 2)
            if target_api_enabled:
                if results['tubi_token'] and not results['tubi_device_id']:
                    device_id_from_token = decode_jwt_payload(results['tubi_token'])
                    if device_id_from_token:
                        results['tubi_device_id'] = device_id_from_token
                        logging.info("📱 Device ID kinyerve a token payloadból (Fallback 2).")
            
            return results

# ----------------------------------------------------------------------
# FLASK ÚTVONAL KEZELÉS - TUBI ÉS GENERIKUS PROXY
# ----------------------------------------------------------------------

@app.route('/scrape', methods=['GET', 'POST'])
def scrape_handler():
    
    # --- 1. GENERIKUS PROXY KEZELÉS (POST kérés) ---
    if request.method == 'POST':
        try:
            proxy_request_data = request.get_json()
            
            if not proxy_request_data:
                return jsonify({'status': 'failure', 'error': 'POST kérés érkezett, de a JSON törzs hiányzik vagy érvénytelen.'}), 400
                
            # Kinyerjük a továbbítandó kérés részleteit
            target_url = proxy_request_data.get('url')
            target_method = proxy_request_data.get('method', 'GET').upper() 
            target_headers = proxy_request_data.get('headers', {})
            target_json_data = proxy_request_data.get('json_data') # JSON törzs
            target_data = proxy_request_data.get('data') # Form/bináris törzs
            
            if not target_url:
                return jsonify({'status': 'failure', 'error': 'Hiányzó "url" a proxy kérés JSON-jában.'}), 400

            logging.info(f"🚀 GENERIKUS PROXY HÍVÁS: {target_method} {target_url[:80]}...")
            
            # Elküldjük a kérést az eredeti API-nak
            response = requests.request(
                method=target_method,
                url=target_url,
                headers=target_headers,
                json=target_json_data, 
                data=target_data,      
                verify=False,          
                timeout=DEFAULT_REQUEST_TIMEOUT 
            )
            
            # Válasz dekódolása
            try:
                content_decoded = response.json()
            except json.JSONDecodeError:
                content_decoded = response.text
                
            return jsonify({
                "status": "success",
                "proxy_status": "forwarded",
                "statusCode": response.status_code,
                "headers": dict(response.headers),
                "content": content_decoded 
            })
            
        except requests.exceptions.RequestException as e:
            logging.error(f"❌ Generikus proxy hívási hiba a külső API felé: {e}")
            return jsonify({
                "status": "failure", 
                "error": f"Hiba a külső API hívás során: {e}",
                "statusCode": getattr(e.response, 'status_code', 500) if e.response is not None else 504 
            }), 500
        except Exception as e:
            logging.error(f"❌ Generikus proxy belső hiba: {e}")
            return jsonify({"status": "failure", "error": f"Belső szerver hiba a proxy kezelésekor: {e}"}), 500
    
    # --- 2. TUBI TV SCRAPING ÉS API HÍVÁS KEZELÉS (GET kérés) ---
    elif request.method == 'GET':
        
        url = request.args.get('url')
        if not url:
            return jsonify({'status': 'failure', 'error': 'Hiányzó "url" paraméter a GET kérésben.'}), 400
        
        # Tubi specifikus paraméterek kinyerése
        initial_target_api_enabled = request.args.get('target_api', '').lower() == 'true'
        har_enabled = request.args.get('har', '').lower() == 'true'
        simple_log_enabled = request.args.get('simple_log', '').lower() == 'true'
        api_type = request.args.get('api_type', 'content').lower() 
        
        season_num_str = request.args.get('season')
        max_pages_str = request.args.get('pages')
        page_size_str = request.args.get('page_size')
        is_season_download = all([season_num_str, max_pages_str, page_size_str])

        if api_type not in ['content', 'search']:
            return jsonify({'status': 'failure', 'error': f'Érvénytelen api_type: {api_type}. Támogatott értékek: content, search.'}), 400

        # API hívás engedélyezése ha Tubi URL-t kaptunk, VAGY ha évadletöltés a cél
        target_api_enabled = is_tubi_url(url) and (initial_target_api_enabled or is_season_download)
        
        if not target_api_enabled and (initial_target_api_enabled or is_season_download):
            logging.warning("⚠️ Belső API hívás letiltva, mert a megadott URL nem Tubi TV-re mutat.")


        logging.info(f"🌐 TUBI SCRAPE INDÍTÁSA. Cél URL: {url}. API Hívás engedélyezve: {target_api_enabled}. Évadletöltés: {is_season_download}")

        # Csak egy kísérlet a Playwright-ra a belső 40s polling miatt
        loop = asyncio.get_event_loop()
        final_data = loop.run_until_complete(scrape_tubitv(url, target_api_enabled, har_enabled, simple_log_enabled, api_type))
        
        token_present = final_data.get('tubi_token') is not None
        device_id_present = final_data.get('tubi_device_id') is not None

        # --- TUBI ÉVAD LETÖLTÉS LOGIKA (Ha a paraméterek be vannak állítva) ---
        if is_season_download and token_present and device_id_present:
            
            try:
                season_num = int(season_num_str)
                max_pages = int(max_pages_str)
                page_size = int(page_size_str)
            except ValueError:
                return jsonify({'status': 'failure', 'error': 'Érvénytelen season/pages/page_size formátum.'}), 400
                
            content_id = extract_content_id_from_url(url)
            
            if not content_id:
                final_data['status'] = 'failure'
                final_data['error'] = 'Hiányzó Content ID az URL-ből az évadletöltéshez.'
                return jsonify(final_data)

            # TÖBBLAPOS API HÍVÁS
            paginated_data = make_paginated_tubi_api_call(
                content_id=content_id, 
                token=final_data['tubi_token'], 
                device_id=final_data['tubi_device_id'], 
                user_agent=final_data.get('user_agent', 'Mozilla/5.0'), 
                season_num=season_num, 
                max_pages=max_pages, 
                page_size=page_size
            )
            
            final_data['page_data'] = paginated_data
            if paginated_data:
                final_data['status'] = 'success'
                logging.info(f"✅ Évadletöltés befejezve. {len(paginated_data)} lap visszaküldve.")
            else:
                final_data['status'] = 'partial_success' 
                final_data['error'] = final_data.get('error', 'Sikertelen Content API hívás a szerveren (valószínűleg 403-as hiba).')
                
            return jsonify(final_data)
        
        elif is_season_download and not token_present:
            final_data['status'] = 'failure'
            final_data['error'] = 'Token/Device ID kinyerése sikertelen az évadletöltéshez (polling lejárt/sikertelen).'
            return jsonify(final_data)
        # --- ÉVAD LETÖLTÉS LOGIKA VÉGE ---

        # --- DEFAULT S1 METADATA LOGIKA ---
        # Ha a token és device_id megvan, de NEM évadletöltés történt, hívjuk meg az S1/Search API-t
        if target_api_enabled and token_present and device_id_present:
            
            content_id = extract_content_id_from_url(url) if api_type == 'content' else None

            final_data['tubi_api_data'] = make_internal_tubi_api_call(
                api_type=api_type, 
                url=url, 
                content_id=content_id, 
                token=final_data['tubi_token'], 
                device_id=final_data['tubi_device_id'], 
                user_agent=final_data.get('user_agent', 'Mozilla/5.0')
            )
            
            if final_data['tubi_api_data']:
                final_data['status'] = 'success'
            else:
                final_data['status'] = 'partial_success'
                final_data['error'] = final_data.get('error', 'Token kinyerve, de az S1/Search API hívás sikertelen volt.')
                
        
        # HTML válasz visszaadása, ha csak azt kérik
        html_requested = request.args.get('html', '').lower() == 'true'
        json_outputs_requested = any(
            request.args.get(p, '').lower() == 'true' 
            for p in ['full_json', 'har', 'simple_log', 'target_api']
        )
        is_only_html_requested = html_requested and not json_outputs_requested
        
        if is_only_html_requested and final_data.get('html_content') and final_data.get('status') in ['success', 'partial_success']:
            return Response(final_data['html_content'], mimetype='text/html')

        return jsonify(final_data)

    
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000))
