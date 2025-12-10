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
# Fontos: DEBUG szintre állítva a részletes hálózati logokhoz
logging.basicConfig(level=logging.DEBUG)

# --- ÚJ: LISTHANDLER OSZTÁLY a logok gyűjtésére (5. opcióhoz) ---
class ListHandler(logging.Handler):
    """Egyéni logger kezelő, amely a logüzeneteket egy listába gyűjti."""
    def __init__(self, log_list):
        super().__init__()
        # Formátum beállítása: LOGLEVEL:LOGGER_NAME:MESSAGE
        self.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s'))
        self.log_list = log_list

    def emit(self, record):
        self.log_list.append(self.format(record))
# ------------------------------------------------------------------

# --- KONFIGURÁCIÓS ÁLLANDÓK ---
MAX_RETRIES = 3
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"

# 1. Tubi API URL TEMPLATE ELŐTAGJA
TUBI_API_TEMPLATE_PREFIX = (
    "https://search.production-public.tubi.io/api/v2/search?"
    "images%5Bposterarts%5D=w408h583_poster&images%5Bhero_422%5D=w422h360_hero&"
    "images%5Bhero_feature_desktop_tablet%5D=w1920h768_hero&images%5Bhero_feature_large_mobile%5D=w960h480_hero&"
    "images%5Bhero_feature_small_mobile%5D=w540h450_hero&images%5Bhero_feature%5D=w375h355_hero&"
    "images%5Blandscape_images%5D=w978h549_landscape&images%5Blinear_larger_poster%5D=w978h549_landscape&"
    "images%5Bbackgrounds%5D=w1614h906_background&images%5Btitle_art%5D=w430h180_title&"
    "search="
)

# 2. Tubi API URL TEMPLATE UTÓTAGJA
TUBI_API_TEMPLATE_SUFFIX = (
    "&include_channels=true&include_linear=true&is_kids_mode=false"
)

# ----------------------------------------------------------------------
# SEGÉDFÜGGVÉNYEK
# ----------------------------------------------------------------------

def is_tubi_url(url: str) -> bool:
    """Ellenőrzi, hogy a megadott URL a tubitv.com domainhez tartozik-e."""
    try:
        domain = urlparse(url).netloc
        # Ellenőrizzük a tubitv.com (vagy aldomaineit) jelenlétét.
        return 'tubitv.com' in domain.lower()
    except Exception:
        return False

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    """Dekódolja a JWT payload részét és kinyeri a device_id-t."""
    try:
        # A payload a 2. szegmens (index 1)
        payload_base64 = jwt_token.split('.')[1]
        # Base64 padding hozzáadása
        padding = '=' * (4 - len(payload_base64) % 4)
        payload_decoded = base64.b64bdecode(payload_base64 + padding).decode('utf-8')
        
        payload_data = json.loads(payload_decoded)
        # Kinyerjük a 'device_id'-t
        return payload_data.get('device_id')
    except Exception as e:
        logging.debug(f"DEBUG: [JWT HIBA] Hiba a JWT dekódolásánál: {e}")
        return None

def make_internal_tubi_api_call(search_term: str, token: str, device_id: str, user_agent: str) -> Optional[Dict]:
    """A Tubi belső API-jának hívása a kinyert tokennel és Device ID-vel."""
    if not token or not device_id:
        logging.error("Hiányzó token vagy device_id a belső API híváshoz.")
        return None

    # Összeállítjuk a teljes Tubi API URL-t
    encoded_search_term = urllib.parse.quote(search_term) 
    full_api_url = f"{TUBI_API_TEMPLATE_PREFIX}{encoded_search_term}{TUBI_API_TEMPLATE_SUFFIX}"

    # Összeállítjuk a fejléceket
    request_headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": user_agent,
        DEVICE_ID_HEADER: device_id,
        "Accept": "application/json"
    }

    try:
        logging.info(f"Belső API hívás indítása: {full_api_url[:80]}...")
        response = requests.get(full_api_url, headers=request_headers, timeout=10)
        response.raise_for_status() 
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Belső API hívási hiba: {e}")
        return None

# ----------------------------------------------------------------------
# ASZINKRON PLAYWRIGHT SCRAPE FÜGGVÉNY 
# ----------------------------------------------------------------------

async def scrape_tubitv(url: str, target_api_enabled: bool, har_enabled: bool, simple_log_enabled: bool) -> Dict: 
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
    
    # Szerver DEBUG Log Fogás Beállítása (5. opcióhoz)
    root_logger = logging.getLogger()
    list_handler = None
    
    if simple_log_enabled:
        list_handler = ListHandler(results['simple_logs'])
        list_handler.setLevel(logging.DEBUG) 
        root_logger.addHandler(list_handler)
    
    async with async_playwright() as p:
        browser = None
        try:
            # Csökkentett launch timeout a gyorsabb hibakezelés érdekében (ha a böngésző nem indul)
            browser = await p.chromium.launch(headless=True, timeout=15000) 
            
            # 1. User Agent kinyerése
            temp_context = await browser.new_context() 
            temp_page = await temp_context.new_page() 
            user_agent = await temp_page.evaluate('navigator.userAgent')
            await temp_context.close()
            results['user_agent'] = user_agent
            
            # 2. A tényleges context létrehozása
            har_config = {'path': 'network.har', 'mode': 'minimal'} if har_enabled else {}
            
            # ignore_https_errors=True hozzáadása minden kontextushoz az SSL hibák kezelésére
            context = await browser.new_context(
                locale='en-US', 
                timezone_id='America/New_York', 
                ignore_https_errors=True, 
                **har_config
            )
                
            page = await context.new_page()
            page.set_default_timeout(30000)

            # --- ROUTE BLOKKOLÁS ÉS KEZELÉS ---

            # 1. Blokkoljuk a felesleges erőforrásokat
            await page.route("**/google-analytics**", lambda route: route.abort())
            await page.route(lambda url: url.lower().endswith(('.png', '.jpg', '.gif', '.css', '.woff2', '.webp')), lambda route: route.abort())

            # Router a forgalom naplózására és a token rögzítésére
            if simple_log_enabled or target_api_enabled:
                
                async def handle_request_token_and_log(route: Route):
                    request = route.request
                    
                    # 1. Hálózati logolás (MINDIG fut, ha az 5-ös opció engedélyezve van)
                    if simple_log_enabled:
                        logging.debug(f"DEBUG: [HÁLÓZAT KÉRÉS] {request.method} - URL: {request.url}")
                    
                    # 2. Token rögzítés (CSAK ha target_api_enabled)
                    if target_api_enabled:
                        headers = request.headers
                        
                        # --- 1. Token rögzítése a KÉRÉS fejlécéből ---
                        # Ez a legmegbízhatóbb módszer, mivel minden belső API hívásban szerepel.
                        if not results['tubi_token'] and 'authorization' in headers and headers['authorization'].startswith('Bearer'):
                            token = headers['authorization'].split('Bearer ')[1].strip()
                            results['tubi_token'] = token
                            logging.info(f"🔑 Token rögzítve élő elfogással a KÉRÉS fejlécéből. (TOKEN MÉRET: {len(token)})")
                        
                        # --- 2. Device ID rögzítése a KÉRÉS fejlécéből ---
                        if not results['tubi_device_id'] and DEVICE_ID_HEADER.lower() in headers:
                            results['tubi_device_id'] = headers[DEVICE_ID_HEADER.lower()]
                            logging.info(f"📱 Device ID rögzítve élő elfogással a KÉRÉS fejlécéből. ({results['tubi_device_id']})")

                        # --- 3. JAVÍTÁS: Device ID rögzítése az URL query paraméterből (Fallback) ---
                        if not results['tubi_device_id'] and ('tubi.io' in request.url or 'tubitv.com' in request.url):
                             query_params = parse_qs(urlparse(request.url).query)
                             device_id_from_url = query_params.get('device_id', [None])[0]
                             if device_id_from_url:
                                 results['tubi_device_id'] = device_id_from_url
                                 logging.info(f"📱 Device ID rögzítve az URL query paraméterből (Fallback). ({results['tubi_device_id']})")
                        
                        # --- A VÁLASZ BODY elemzés (amit a TargetClosedError miatt kivettünk) ide nem jön ---
                        
                    await route.continue_() 

                await page.route("**/*", handle_request_token_and_log)
            # --- ROUTE BLOKKOLÁS ÉS KEZELÉS VÉGE ---

            # Betöltjük az oldalt
            logging.info("🌐 Oldal betöltése (wait_until='networkidle')...")
            # Megnövelt navigation timeout az esetleges lassú hálózat miatt
            await page.goto(url, wait_until="networkidle", timeout=60000) 
            
            if target_api_enabled:
                # --- JAVÍTÁS: Robusztus várakozás a token-tartalmú kérésre ---
                logging.info("⏳ Várakozás egy belső API hívásra, amely tartalmazza az 'Authorization' tokent...")
                try:
                    # Keressük az első olyan request-et, aminek van Authorization fejléce
                    await page.wait_for_request(
                        lambda req: 'authorization' in req.headers, 
                        timeout=15000 # 15 másodpercet várunk
                    )
                    logging.info("🔑 Token-tartalmú kérés elfogva. Az útvonal-kezelő rögzítette a tokent.")
                except Exception as e:
                    # Ha a várakozás időtúllépés miatt bukik, de a token már rögzítve van, az OK.
                    if not results['tubi_token']:
                        logging.warning(f"❌ Token-tartalmú kérés nem jött meg a 15 másodperces időtúllépés alatt. Lehet, hogy a token nem került rögzítésre. Hiba: {e}")
                    else:
                        logging.info("✅ A token már rögzítve volt a várakozás előtt.")
                # -------------------------------------------------------------

            # --- JAVÍTÁS: Unroute a TargetClosedError elkerülésére ---
            logging.info("🧹 Playwright útvonal-kezelők leállítása.")
            # Unroute a route() leállítása után kell futnia
            if simple_log_enabled or target_api_enabled:
                 # Ha a route() regisztrálva volt, unroute_all-t hívunk
                await page.unroute_all(behavior='ignoreErrors') 
            # ----------------------------------------------------

            # A NYERS HTML TARTALOM KIMENTÉSE
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
                 # A context.close() automatikusan zárja a böngészőt, ha nincs más context
                 await browser.close()
            logging.info("✅ Playwright befejezve.")

            # --- HAR fájl beolvasása és törlése ---
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

            # 3. Kiegészítés: Device ID kinyerése a tokenből, ha hiányzik (csak ha target_api_enabled)
            if target_api_enabled:
                # Ezt a lépést csak akkor futtatjuk, ha a fenti két módszer nem járt sikerrel
                if results['tubi_token'] and not results['tubi_device_id']:
                    device_id_from_token = decode_jwt_payload(results['tubi_token'])
                    if device_id_from_token:
                        results['tubi_device_id'] = device_id_from_token
                        logging.info("📱 Device ID kinyerve a token payloadból (Fallback 2).")

            # 4. Belső API hívás (csak ha target_api_enabled)
            if target_api_enabled and results['tubi_token'] and results['tubi_device_id']:
                url_parsed = urlparse(url)
                query_params = parse_qs(url_parsed.query)
                search_term_raw = query_params.get('search', query_params.get('q', [None]))[0]
                
                # Path alapú search_term kinyerése (pl. /search/film-cim)
                if not search_term_raw and 'search/' in url_parsed.path:
                    path_segments = url_parsed.path.rstrip('/').split('/')
                    if path_segments[-2] == 'search':
                        search_term_raw = path_segments[-1]
                # Bármilyen utolsó path szegmens kinyerése
                elif not search_term_raw and url_parsed.path:
                    path_segments = url_parsed.path.rstrip('/').split('/')
                    if len(path_segments) > 1 and path_segments[-1]:
                        search_term_raw = path_segments[-1]

                search_term = unquote(search_term_raw).replace('-', ' ') if search_term_raw else "ismeretlen" 

                if search_term and search_term != 'ismeretlen':
                    tubi_api_data = make_internal_tubi_api_call(search_term, results['tubi_token'], results['tubi_device_id'], results['user_agent'])
                    results['tubi_api_data'] = tubi_api_data
                    
                    if not tubi_api_data:
                        if results['status'] == 'success':
                            results['status'] = 'partial_success'
                        results['error'] = results.get('error', 'Sikertelen belső Tubi API hívás a kinyert tokennel.')
                else:
                    logging.warning(f"Nem talált search paramétert az URL-ben a belső API híváshoz. Alapértelmezett: '{search_term}'")

            return results

# ----------------------------------------------------------------------
# FLASK ÚTVONAL KEZELÉS 
# ----------------------------------------------------------------------

@app.route('/scrape', methods=['GET'])
def scrape_tubi_endpoint():
    url = request.args.get('url')
    if not url:
        return jsonify({'status': 'failure', 'error': 'Hiányzó "url" paraméter.'}), 400
    
    # Kinyerjük az eredeti kérést
    initial_target_api_enabled = request.args.get('target_api', '').lower() == 'true'
    har_enabled = request.args.get('har', '').lower() == 'true'
    simple_log_enabled = request.args.get('simple_log', '').lower() == 'true'

    # JAVÍTÁS: Token/API logika csak tubitv.com esetén engedélyezett (ha a kliens kérte)
    if initial_target_api_enabled and is_tubi_url(url):
        target_api_enabled = True
        should_retry_for_token = True
    else:
        target_api_enabled = False
        should_retry_for_token = False
    
    # A retry_count csak akkor lehet > 1, ha a target_api engedélyezve van
    retry_count = MAX_RETRIES if should_retry_for_token else 1 

    json_outputs_requested = any(
        request.args.get(p, '').lower() == 'true' 
        for p in ['full_json', 'har', 'simple_log', 'target_api']
    )
    html_requested = request.args.get('html', '').lower() == 'true'
    
    logging.info(f"API hívás indítása. Cél URL: {url}. Belső API hívás engedélyezve: {target_api_enabled}.")

    final_data = {}

    for attempt in range(1, retry_count + 1):
        logging.info(f"Kísérlet {attempt}/{retry_count} a scrape futtatására. URL: {url} (Belső API engedélyezve: {target_api_enabled})")
        
        loop = asyncio.get_event_loop()
        final_data = loop.run_until_complete(scrape_tubitv(url, target_api_enabled, har_enabled, simple_log_enabled))
        
        # --- Visszatérési logika ---
        
        # 1. Ha CSAK Tiszta HTML volt kérve
        is_only_html_requested = html_requested and not json_outputs_requested
        
        if is_only_html_requested and final_data.get('html_content') and final_data.get('status') == 'success':
              logging.info("Visszatérés (Sikeres, Tiszta HTML kinyerés).")
              return Response(final_data['html_content'], mimetype='text/html')
              
        # 2. Sikeres Kimenet VAGY Technikai hiba VAGY Nem kérték a token keresést
        
        # Technikai hiba esetén (pl. Playwright hiba), de nem kértünk TubiTV specifikus adatok, azonnal visszaadjuk.
        if final_data.get('status') == 'failure' and not target_api_enabled:
              logging.info("Visszatérés (Playwright hiba nem TubiTV URL esetén).")
              return jsonify(final_data)
        
        # Ha a target_api_enabled True, de a token/API hívás nem sikerült
        token_present = final_data.get('tubi_token') is not None
        api_data_present = final_data.get('tubi_api_data') is not None

        if target_api_enabled and (not token_present or not api_data_present):
              # Folytatjuk az újrapróbálkozást, ha van még esély (a retry_count gondoskodik erről)
              if attempt < retry_count:
                  logging.warning(f"Token/API hiba TubiTV esetén. Újrapróbálkozás {attempt + 1}. kísérlet...")
                  # Növeljük a sleep-et, mert a token generálás időt vehet igénybe
                  time.sleep(3) 
                  continue # Ugrás a következő kísérletre
              else:
                  # 5. Végső visszatérés hiba esetén (ha kifutott az újrapróbálkozásokból)
                  logging.error("A kért TubiTV adatok nem voltak kinyerhetők az összes kísérlet után sem.")
                  return jsonify(final_data)

        # 3. Sikeres Eredmény visszaadása (bármilyen sikeres futtatás)
        if final_data.get('status') == 'success' and (not target_api_enabled or (token_present and api_data_present)):
              logging.info(f"Adatok sikeresen kinyerve a(z) {attempt}. kísérletben. Visszatérés JSON-ben.")
              return jsonify(final_data)
        
        # 4. Ha volt Playwright hiba, de nem TubiTV URL-re hívtuk (itt már nem futna le a fenti logika miatt)
        if final_data.get('status') == 'failure' and target_api_enabled:
             # Ha TubiTV-nél bukott el, de már kifutottunk a kísérletekből (ezt a fenti if blokk is kezeli, de biztonság kedvéért)
            if attempt == retry_count:
                logging.error("A kért TubiTV adatok nem voltak kinyerhetők Playwright hiba miatt az összes kísérlet után sem.")
                return jsonify(final_data)
            # Egyébként mehet az újrapróbálkozás.
            logging.warning(f"Playwright hiba TubiTV esetén. Újrapróbálkozás {attempt + 1}. kísérlet...")
            time.sleep(3)
        
    # Végső visszatérés, ha a ciklus kifutott
    return jsonify(final_data)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000))
