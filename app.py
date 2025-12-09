import asyncio
import nest_asyncio
import json
import logging
import base64
import os
import time
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright, Route
import requests
import re      
import urllib.parse 
from urllib.parse import urlparse, parse_qs, unquote
from typing import Optional, Dict

# Engedélyezi az aszinkron funkciók beágyazását
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
# Fontos: DEBUG szintre állítva a részletes hálózati logokhoz
logging.basicConfig(level=logging.DEBUG)

# --- KONFIGURÁCIÓS ÁLLANDÓK ---
MAX_RETRIES = 3 # Maximum ennyi újrapróbálkozás a token megszerzésére
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"

# 1. Tubi API URL TEMPLATE ELŐTAGJA: Ez a rész a search= paramétert tartalmazza
TUBI_API_TEMPLATE_PREFIX = (
    "https://search.production-public.tubi.io/api/v2/search?\"images%5Bposterarts%5D=w408h583_poster&images%5Bhero_422%5D=w422h360_hero&\"images%5Bhero_feature_desktop_tablet%5D=w1920h768_hero&images%5Bhero_feature_large%5D=w1920h768_hero&\"images%5Btile_1x1%5D=w272h272_tile&images%5Btile_16x9%5D=w500h281_tile&images%5Btile_3x4%5D=w272h363_tile&query="
)
# 2. Tubi API URL TEMPLATE HÁTULJA: A lekérdezés utáni rész
TUBI_API_TEMPLATE_SUFFIX = (
    "&use_limit_for_count=true&page=1&per_page=12&recommendation_type=0&hide_duplicates=true&filter%5Bis_new%5D=false"
)

# ----------------------------------------------------------------------
# SEGÉDFÜGGVÉNYEK
# ----------------------------------------------------------------------

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    """Dekódolja a JWT payload részét és kinyeri a device_id-t."""
    try:
        payload_base64 = jwt_token.split('.')[1]
        padding = '=' * (4 - len(payload_base64) % 4)
        payload_decoded = base64.b64bdecode(payload_base64 + padding).decode('utf-8')
        
        payload_data = json.loads(payload_decoded)
        return payload_data.get('device_id')
    except Exception as e:
        logging.debug(f"DEBUG: [JWT HIBA] Hiba a JWT dekódolásánál: {e}")
        return None

def make_internal_tubi_api_call(search_term: str, token: str, device_id: str, user_agent: str) -> Optional[Dict]:
    """A Tubi belső API-jának hívása a kinyert tokennel és Device ID-vel."""
    if not token or not device_id:
        logging.error("Hiányzó token vagy device_id a belső API híváshoz.")
        return None

    # Search query kódolása az URL-hez
    encoded_search_term = urllib.parse.quote_plus(search_term)
    
    # Tubi API URL összeállítása
    tubi_api_url = f"{TUBI_API_TEMPLATE_PREFIX}{encoded_search_term}{TUBI_API_TEMPLATE_SUFFIX}"
    
    # Headerek beállítása
    headers = {
        'Authorization': f'Bearer {token}',
        DEVICE_ID_HEADER: device_id,
        'User-Agent': user_agent,
        'Accept': 'application/json'
    }
    
    try:
        logging.info(f"🚀 Belső Tubi API hívás indítása: {tubi_api_url[:80]}...")
        response = requests.get(tubi_api_url, headers=headers, timeout=15)
        response.raise_for_status() # HTTP hibák (4xx vagy 5xx) kiváltása
        
        logging.info("✅ Belső Tubi API válasz sikeresen fogadva.")
        return response.json()
    
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Hiba a belső Tubi API hívásban: {e}")
        if response is not None:
             logging.error(f"API válasz állapota: {response.status_code}")
             logging.error(f"API válasz tartalma (részlet): {response.text[:200]}...")
        return None

def extract_search_term_from_url(url: str) -> str:
    """Kinyeri a keresési kifejezést egy TubiTV URL-ből (pl. /search/film cím)."""
    parsed_url = urlparse(url)
    path_segments = [s for s in parsed_url.path.split('/') if s]
    
    if len(path_segments) >= 2 and path_segments[0].lower() == 'search':
        # Várható formátum: /search/film cím
        search_term = path_segments[1]
    else:
        # Visszaállás az URL domain névre, ha nem Tubi search URL
        search_term = parsed_url.netloc

    return unquote(search_term).replace('-', ' ')

def ensure_https_protocol_server(url: str) -> str:
    """Biztosítja, hogy az URL tartalmazza a https:// protokollt, ha hiányzik."""
    if not url:
        return ""
    if not re.match(r'https?://', url):
        return f"https://{url}"
    return url

# ----------------------------------------------------------------------
# ASZINKRON PLAYWRIGHT SCRAPE FÜGGVÉNY 
# ----------------------------------------------------------------------

async def scrape_tubitv(url: str, target_api_enabled: bool, html_enabled: bool, har_enabled: bool, console_log_enabled: bool) -> Dict:
    """Betölti az oldalt, elvégzi a scrape-et, és kinyeri a tokent (ha szükséges)."""
    
    # 1. URL tisztítás a Playwright hiba elkerülésére
    url = ensure_https_protocol_server(url) 
    
    results = {
        'status': 'success',
        'url': url,
        'tubi_token': None,
        'tubi_device_id': None,
        'user_agent': None,
        'console_logs': [],
        'har_content': None,
        'tubi_api_data': None,
        'html_content': None,
        'simple_logs': []
    }
    
    async with async_playwright() as p:
        browser = None
        try:
            # Böngésző indítása
            browser = await p.chromium.launch(headless=True)
            
            # User Agent kinyerése
            results['user_agent'] = await browser.version()
            
            # Context létrehozása (Tubi esetén specifikus beállítások)
            context = await browser.new_context(locale='en-US', timezone_id='America/New_York') if target_api_enabled else await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(30000)

            # --- Eseménykezelők beállítása ---

            # Konzol logok rögzítése (ha kérték)
            if console_log_enabled:
                page.on('console', lambda msg: results['console_logs'].append({'type': msg.type, 'text': msg.text}))
                page.on('pageerror', lambda error: results['console_logs'].append({'type': 'error', 'text': str(error)}))
            
            # Handler függvény a blokkoláshoz
            async def abort_requests(route):
                await route.abort()

            # Hálózati forgalom blokkolása (minden esetben a gyorsabb betöltésért)
            await page.route("**/google-analytics**", abort_requests)
            
            # --- JAVÍTÁS ---
            # Glob mintával a Python regex/lambda callable hiba elkerülésére.
            # Blokkolja a leggyakoribb statikus fájlokat.
            await page.route("**/*.{png,jpg,gif,css,woff2,ico,svg,webp,jpeg}", abort_requests)
            # --- END JAVÍTÁS ---
            
            # --- MÓDOSÍTOTT LOGIKA: CSAK AKKOR KELL AZ ÉLŐFOGÁS, HA 'target_api' IS FUT ---
            if target_api_enabled:
                 # Eseménykezelő a token és Device ID élő rögzítéséhez 
                async def handle_request_for_token(route: Route):
                    request = route.request
                    
                    # DEBUG: Hálózati forgalom logolása
                    if 'tubi' in request.url.lower() or 'device' in request.url.lower():
                         logging.debug(f"DEBUG: [HÁLÓZAT KÉRÉS] {request.method} - URL: {request.url}")
                    
                    headers = request.headers
                    
                    # --- 1. Ellenőrzés a KÉRÉS fejlécében ---
                    if not results['tubi_token'] and 'authorization' in headers and headers['authorization'].startswith('Bearer'):
                        token = headers['authorization'].split('Bearer ')[1].strip()
                        results['tubi_token'] = token
                        logging.info(f"🔑 Token rögzítve élő elfogással a KÉRÉS fejlécéből. ({token[:10]}...)")
                    
                    if not results['tubi_device_id'] and DEVICE_ID_HEADER.lower() in headers:
                        results['tubi_device_id'] = headers[DEVICE_ID_HEADER.lower()]
                        logging.info(f"📱 Device ID rögzítve élő elfogással a KÉRÉS fejlécéből. ({results['tubi_device_id']})")

                    await route.continue_() 
                    
                    # --- 2. Ellenőrzés a VÁLASZ testében (token generáló végpont) ---
                    if not results['tubi_token'] and 'device/anonymous/token' in request.url:
                         response = await request.response() 
                         if response and response.ok:
                             try:
                                 response_json = await response.json()
                                 token = response_json.get('access_token')
                                 
                                 if token:
                                     results['tubi_token'] = token
                                     # Kinyerjük az ID-t a token payloadból
                                     device_id_from_token = decode_jwt_payload(token)
                                     if device_id_from_token:
                                          results['tubi_device_id'] = device_id_from_token
                                     
                                     logging.info(f"🔑 Token rögzítve élő elfogással a VÁLASZ testéből! ({token[:10]}...)")
                                     
                             except Exception as e:
                                 logging.warning(f"Figyelem: Token válasz JSON dekódolási hiba: {e}")
                                 pass
                
                await page.route("**/*", handle_request_for_token)
            # ------------------------------------------------------------------------------------------------------

            # Betöltjük az oldalt
            logging.info("🌐 Oldal betöltése (wait_until='networkidle')...")
            await page.goto(url, wait_until="networkidle", timeout=30000) 
            
            # Kényszerített várakozás: Csak token keresésnél van értelme
            if target_api_enabled:
                logging.info("⏳ Kényszerített várakozás 5 másodperc a token rögzítésére.")
                await page.wait_for_timeout(5000) 

            # --- HTML TARTALOM KIMENTÉSE ---
            if html_enabled or target_api_enabled: # Belső hívásnál is kell a HTML
                 try:
                    results['html_content'] = await page.content()
                    logging.info("📝 A lap tartalmát (HTML) sikeresen kimentette.")
                 except Exception as e_content:
                    logging.error(f"❌ Hiba a lap tartalmának (HTML) kimentésekor: {e_content}")
                    results['html_content'] = "ERROR: Failed to retrieve HTML content."

            # --- HAR LOG KIMENTÉSE ---
            if har_enabled:
                try:
                    # Szükséges, hogy a HAR-t a böngésző futásának végén mentse
                    results['har_content'] = json.loads(await context.har_export())
                    logging.info("📝 HAR logok sikeresen kimentve.")
                except Exception as e:
                    logging.error(f"❌ Hiba a HAR kimentésekor: {e}")

            # --- Egyszerűsített logok kimenete (csak ha kérték) ---
            if target_api_enabled or not (html_enabled or har_enabled or console_log_enabled):
                 results['simple_logs'].append(f"Render státusz: Siker. (Token keresés: {target_api_enabled})")

            
        except Exception as e:
            results['status'] = 'failure'
            results['error'] = f"Playwright hiba: {str(e)}"
            logging.error(f"❌ Playwright hiba: {e}")
            
        finally:
            if browser:
                await browser.close()
            logging.info("✅ Playwright befejezve.")
            
            # 3. Kiegészítés: Device ID kinyerése a tokenből, ha hiányzik
            if target_api_enabled and results['tubi_token'] and not results['tubi_device_id']:
                device_id_from_token = decode_jwt_payload(results['tubi_token'])
                if device_id_from_token:
                    results['tubi_device_id'] = device_id_from_token
                    logging.info("📱 Device ID kinyerve a token payloadjából.")
                else:
                    logging.warning("Figyelem: Nem sikerült Device ID-t kinyerni a token payloadjából.")

            # 4. Belső API hívás
            if target_api_enabled and results['tubi_token'] and results['tubi_device_id']:
                search_term = extract_search_term_from_url(url)
                
                tubi_api_data = make_internal_tubi_api_call(
                    search_term=search_term,
                    token=results['tubi_token'],
                    device_id=results['tubi_device_id'],
                    user_agent=results['user_agent']
                )
                
                results['tubi_api_data'] = tubi_api_data
                if tubi_api_data is None:
                    # Belső API hiba esetén felülírjuk a státuszt
                    results['status'] = 'failure'
                    results['error'] = 'Sikertelen Tubi belső API hívás (Token megvan, de a hívás hibás).'
                else:
                    logging.info("✅ Belső API adatok rögzítve.")

        return results

# ----------------------------------------------------------------------
# FLASK ÚTVONAL KEZELÉS 
# ----------------------------------------------------------------------

@app.route('/scrape', methods=['GET'])
def scrape_tubi_endpoint():
    
    url = request.args.get('url')
    if not url:
        return jsonify({'status': 'failure', 'error': 'Hiányzó "url" paraméter.'}), 400
    
    # Kimenetek engedélyezése
    target_api_enabled = request.args.get('target_api', '').lower() == 'true'
    html_enabled = request.args.get('html', '').lower() == 'true'
    har_enabled = request.args.get('har', '').lower() == 'true'
    console_log_enabled = request.args.get('console_log', '').lower() == 'true'
    full_json_enabled = request.args.get('full_json', '').lower() == 'true'
    simple_log_enabled = request.args.get('simple_log', '').lower() == 'true'
    
    # Csak akkor próbálkozzunk újra, ha tokent keresünk (target_api)
    should_retry_for_token = target_api_enabled
    retry_count = MAX_RETRIES if should_retry_for_token else 1 

    logging.info(f"API hívás indítása. Cél URL: {url}. Belső API hívás engedélyezve: {target_api_enabled}.")

    final_data = {'status': 'failure', 'error': 'Playwright futás nem indult el.'}

    for attempt in range(1, retry_count + 1):
        logging.info(f"Kísérlet {attempt}/{retry_count} a scrape futtatására. URL: {url} (Belső API engedélyezve: {target_api_enabled})")
        
        loop = asyncio.get_event_loop()
        final_data = loop.run_until_complete(scrape_tubitv(
            url, 
            target_api_enabled, 
            html_enabled, 
            har_enabled, 
            console_log_enabled
        ))
        
        # --- SIKER ÉS HIBA ELLENŐRZÉS ---
        
        # 1. Hiba történt a Playwright futásban (és nem a belső API hívásban)
        if final_data.get('status') == 'failure' and 'Playwright hiba' in final_data.get('error', ''):
             logging.info("Visszatérés (Playwright hiba).")
             # Ne próbálkozzon újra, ha a Playwright futásban volt alapvető hiba
             return jsonify(final_data), 500

        # 2. Ha tokent keresünk, de az nem sikerült
        if target_api_enabled and not final_data.get('tubi_token'):
             if attempt < retry_count:
                logging.warning(f"Token nem található. Újrapróbálkozás {attempt + 1}. kísérlet...")
                time.sleep(2) # Rövid várakozás
                continue
             else:
                logging.error("A token nem volt kinyerhető az összes kísérlet után sem.")
                return jsonify(final_data) # Visszatérés a Playwright eredeti outputjával
        
        # 3. Siker (vagy nem kértünk token keresést, de a Playwright sikeresen futott)
        # Ha target_api volt kéréve, és az API hívás sikerült, itt térünk vissza.
        if target_api_enabled and final_data.get('tubi_api_data') is not None:
             logging.info(f"Token és API adatok sikeresen kinyerve a(z) {attempt}. kísérletben. Visszatérés.")
             return jsonify(final_data)
        
        # Ha target_api volt kéréve, és a belső hívás hibás (target_api_data=None)
        if target_api_enabled and final_data.get('status') == 'failure':
            logging.info("Visszatérés (Sikertelen belső API hívás).")
            return jsonify(final_data)
        
        # Ha NEM target_api volt kéréve, de a Playwright lefutott, visszatérés.
        if not target_api_enabled and final_data.get('status') == 'success':
            # Ha csak HTML-t kértek, tisztán küldjük vissza
            if html_enabled and not (full_json_enabled or har_enabled or console_log_enabled or simple_log_enabled):
                if final_data.get('html_content'):
                    return Response(final_data['html_content'], mimetype='text/html')
            
            # Minden más esetben JSON-ként küldjük vissza
            return jsonify(final_data)

    # Elméletileg sosem érjük el, de biztonsági visszatérés
    return jsonify(final_data)


if __name__ == '__main__':
    # Helyi futtatáshoz (nem Renderen)
    app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000))
