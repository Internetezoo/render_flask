import asyncio
import nest_asyncio
import json
import logging
import base64
import os
import time
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright, Route
import requests
import re      
import urllib.parse # <--- Ezt kellett hozzáadni a NameError miatt!
from urllib.parse import urlparse, parse_qs, unquote
from typing import Optional, Dict

# Engedélyezi az aszinkron funkciók beágyazását
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.INFO)

# --- KONFIGURÁCIÓS ÁLLANDÓK ---
MAX_RETRIES = 3 # Maximum ennyi újrapróbálkozás a token megszerzésére
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"

# Tubi API URL TEMPLATE
TUBI_API_TEMPLATE = (
    "https://search.production-public.tubi.io/api/v2/search?"
    "images%5Bposterarts%5D=w408h583_poster&images%5Bhero_422%5D=w422h360_hero&"
    "images%5Bhero_feature_desktop_tablet%5D=w1920h768_hero&images%5Bhero_feature_large_mobile%5D=w960h480_hero&"
    "images%5Bhero_feature_small_mobile%5D=w540h450_hero&images%5Bhero_feature%5D=w375h355_hero&"
    "images%5Blandscape_images%5D=w978h549_landscape&images%5Blinear_larger_poster%5D=w978h549_landscape&"
    "images%5Bbackgrounds%5D=w1614h906_background&images%5Btitle_art%5D=w430h180_title&"
    "include_channels=true&include_linear=true&is_kids_mode=false&search="
)

# ----------------------------------------------------------------------
# SEGÉDFÜGGVÉNYEK
# ----------------------------------------------------------------------

def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    """Dekódolja a JWT payload részét és kinyeri a device_id-t."""
    try:
        payload_base64 = jwt_token.split('.')[1]
        padding = '=' * (4 - len(payload_base64) % 4)
        payload_decoded = base64.b64decode(payload_base64 + padding).decode('utf-8')
        
        payload_data = json.loads(payload_decoded)
        return payload_data.get('device_id')
    except Exception:
        return None

def make_internal_tubi_api_call(search_term: str, token: str, device_id: str, user_agent: str) -> Optional[Dict]:
    """A Tubi belső API-jának hívása a kinyert tokennel és Device ID-vel."""
    if not token or not device_id:
        logging.error("Hiányzó token vagy device_id a belső API híváshoz.")
        return None

    # Összeállítjuk a teljes Tubi API URL-t
    encoded_search_term = urllib.parse.quote(search_term)
    full_api_url = f"{TUBI_API_TEMPLATE}{encoded_search_term}"

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

async def scrape_tubitv(url: str, target_api_enabled: bool) -> Dict:
    """Betölti a Tubi oldalt és élő hálózati forgalom elfogással kinyeri a tokent."""
    
    results = {
        'status': 'success',
        'url': url,
        'tubi_token': None,
        'tubi_device_id': None,
        'user_agent': None,
        'tubi_api_data': None 
    }
    
    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(headless=True)
            
            # 1. User Agent kinyerése
            temp_context = await browser.new_context() 
            temp_page = await temp_context.new_page() 
            user_agent = await temp_page.evaluate('navigator.userAgent')
            await temp_context.close()
            results['user_agent'] = user_agent
            
            # 2. A tényleges context létrehozása
            context = await browser.new_context(locale='en-US', timezone_id='America/New_York') if target_api_enabled else await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(30000)

            # Eseménykezelő a token és Device ID élő rögzítéséhez
            async def handle_request_for_token(route: Route):
                request = route.request
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
                                 device_id_from_token = decode_jwt_payload(token)
                                 if device_id_from_token:
                                      results['tubi_device_id'] = device_id_from_token
                                 
                                 logging.info(f"🔑 Token rögzítve élő elfogással a VÁLASZ testéből! ({token[:10]}...)")
                                 
                         except Exception as e:
                             logging.warning(f"Figyelem: Token válasz JSON dekódolási hiba: {e}")
                             pass

            await page.route("**/*", handle_request_for_token)
            
            # Blokkoljuk a felesleges erőforrásokat
            await page.route("**/google-analytics**", lambda route: route.abort())
            await page.route(lambda url: url.lower().endswith(('.png', '.jpg', '.gif', '.css', '.woff2')), lambda route: route.abort())

            # Betöltjük az oldalt
            await page.goto(url, wait_until="networkidle", timeout=30000) 
            
            # Rövid várakozás
            await page.wait_for_timeout(2000)

        except Exception as e:
            results['status'] = 'failure'
            results['error'] = f"Playwright hiba: {str(e)}"
            logging.error(f"Playwright hiba: {e}")
            
        finally:
            if browser:
                await browser.close()
            logging.info("✅ Playwright befejezve (élő elfogás).")

            # 3. Kiegészítés: Device ID kinyerése a tokenből, ha hiányzik
            if results['tubi_token'] and not results['tubi_device_id']:
                device_id_from_token = decode_jwt_payload(results['tubi_token'])
                if device_id_from_token:
                    results['tubi_device_id'] = device_id_from_token
                    logging.info("📱 Device ID kinyerve a token payloadból (Fallback).")

            # 4. Belső API hívás
            if target_api_enabled and results['tubi_token'] and results['tubi_device_id']:
                url_parsed = urlparse(url)
                query_params = parse_qs(url_parsed.query)
                search_term_raw = query_params.get('search', [None])[0]
                
                search_term = unquote(search_term_raw) if search_term_raw else "Sanford and Son" 

                if search_term:
                    tubi_api_data = make_internal_tubi_api_call(search_term, results['tubi_token'], results['tubi_device_id'], results['user_agent'])
                    results['tubi_api_data'] = tubi_api_data
                    if not tubi_api_data:
                        results['status'] = 'failure'
                        results['error'] = 'Sikertelen belső Tubi API hívás a kinyert tokennel.'
                else:
                    logging.warning("Nem talált search paramétert az URL-ben a belső API híváshoz.")

        return results

# ----------------------------------------------------------------------
# FLASK ÚTVONAL KEZELÉS
# ----------------------------------------------------------------------

@app.route('/scrape', methods=['GET'])
def scrape_tubi_endpoint():
    url = request.args.get('url')
    if not url:
        return jsonify({'status': 'failure', 'error': 'Hiányzó "url" paraméter.'}), 400
    
    target_api_enabled = request.args.get('target_api', '').lower() == 'true'
    
    logging.info(f"API hívás indítása. Cél URL: {url}. Belső API hívás engedélyezve: {target_api_enabled}.")

    should_retry_for_token = target_api_enabled

    retry_count = MAX_RETRIES if should_retry_for_token else 1 

    for attempt in range(1, retry_count + 1):
        logging.info(f"Kísérlet {attempt}/{retry_count} a scrape futtatására. URL: {url} (Belső API engedélyezve: {target_api_enabled})")
        
        loop = asyncio.get_event_loop()
        final_data = loop.run_until_complete(scrape_tubitv(url, target_api_enabled))
        
        # 1. Sikeres Kimenet VAGY Technikai hiba VAGY Nem kérték a token keresést
        if final_data.get('status') == 'failure' or not should_retry_for_token:
             logging.info("Visszatérés (Nem kérték a token keresést, vagy technikai hiba).")
             return jsonify(final_data)

        # 2. Tubi Token Check (Csak akkor érünk ide, ha should_retry_for_token=True)
        if final_data.get('tubi_token'): 
            logging.info(f"Token sikeresen kinyerve a(z) {attempt}. kísérletben. Visszatérés.")
            return jsonify(final_data)

        # 3. Újrapróbálkozás
        if attempt < retry_count:
            logging.warning(f"Token nem található. Újrapróbálkozás {attempt + 1}. kísérlet...")
            time.sleep(2) # Rövid várakozás
    
    logging.error("A token nem volt kinyerhető az összes kísérlet után sem.")
    return jsonify(final_data)


if __name__ == '__main__':
    # Helyi futtatáshoz (nem Renderen)
    app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000))
