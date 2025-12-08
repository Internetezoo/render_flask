import asyncio
import nest_asyncio
import json
import logging
import base64
import tempfile 
import os        
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright
import requests
import re      
from urllib.parse import urlparse, parse_qs, unquote
from typing import Optional, Dict

# Engedélyezi az aszinkron funkciók beágyazását
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.INFO)

# Tubi API URL TEMPLATE
TUBI_API_TEMPLATE = (
    "https://search.production-public.tubi.io/api/v2/search?"
    "images%5Bposterarts%5D=w408h583_poster&images%5Bhero_422%5D=w422h360_hero&"
    "images%5Bhero_feature_desktop_tablet%5D=w1920h768_hero&images%5Bhero_feature_large_mobile%5D=w960h480_hero&"
    "images%5Bhero_feature_small_mobile%5D=w540h450_hero&images%5Bhero_feature%5D=w375h355_hero&"
    "images%5Blandscape_images%5D=w978h549_landscape&images%5Blinear_larger_poster%5D=w978h549_landscape&"
    "images%5Bbackgrounds%5D=w1614h906_background&images%5Btitle_art%5D=w430h180_title&"
    "search={search_term}&include_channels=true&include_linear=true&is_kids_mode=false"
)

# --- JAVÍTOTT SEGÉDFÜGGVÉNY: TOKEN KINYERÉSE ---
def extract_tubi_token_from_har(har_data: dict) -> str | None:
    """
    Kinyeri az access_token-t a Tubi TV HAR logjából a 'device/anonymous/token' válaszából,
    vagy az 'Authorization' fejlécből.
    """
    TUBI_TOKEN_ENDPOINT = "account.production-public.tubi.io/device/anonymous/token"
    
    if not har_data or not isinstance(har_data, dict) or 'log' not in har_data:
        return None
        
    try:
        for entry in har_data['log']['entries']:
            # 1. Access Token keresése a 'device/anonymous/token' válaszában
            url = entry['request']['url']
            if TUBI_TOKEN_ENDPOINT in url:
                response_content = entry['response']['content']
                if response_content and 'text' in response_content:
                    response_text = response_content['text']
                    
                    if response_content.get('encoding') == 'base64':
                        try:
                             response_text = base64.b64decode(response_text).decode('utf-8')
                        except:
                            continue
                            
                    try:
                        token_data = json.loads(response_text)
                        if 'access_token' in token_data:
                            logging.info("Tubi access token sikeresen kinyerve a VÁLASZBÓL.")
                            return token_data['access_token']
                    except json.JSONDecodeError:
                        continue
                        
            # 2. Access Token keresése az Authorization fejlécben (Robusztusabb módszer)
            if 'request' in entry and 'headers' in entry['request']:
                for header in entry['request']['headers']:
                    if header.get('name', '').lower() == 'authorization':
                        value = header.get('value', '')
                        if value.startswith('Bearer '):
                            logging.info("Tubi access token sikeresen kinyerve a KÉRÉS FEJLÉCBŐL.")
                            return value.split('Bearer ')[1].strip()

        logging.warning("Nem találtam Tubi access tokent a HAR logban.")
        return None
    except Exception as e:
        logging.error(f"Hiba a Tubi token kinyerésekor: {e}")
        return None


# --- JAVÍTOTT SEGÉDFÜGGVÉNY: Device ID kinyerése ---
def extract_device_id_from_har(har_log: dict) -> str | None:
    """
    Kinyeri a friss 'deviceId' cookie-t a HAR logból.
    Keresi a Set-Cookie fejlécben (válasz), a Cookie fejlécben (kérés) és a POST törzsben.
    """
    if not har_log or 'entries' not in har_log.get('log', {}):
        return None

    for entry in har_log['log']['entries']:
        # 1. Keresés a Set-Cookie fejlécben (válasz)
        if 'response' in entry and 'headers' in entry['response']:
            for header in entry['response']['headers']:
                if header.get('name', '').lower() == 'set-cookie':
                    match = re.search(r'deviceId=([^;]+)', header.get('value', ''))
                    if match:
                        logging.info("Device ID sikeresen kinyerve a Set-Cookie-ból.")
                        return match.group(1).strip()
        
        # 2. Keresés a Cookie fejlécben (kérés) - ÚJ ROBUSZTUSÍTÁS
        if 'request' in entry and 'headers' in entry['request']:
            for header in entry['request']['headers']:
                 if header.get('name', '').lower() == 'cookie':
                     match = re.search(r'deviceId=([^;]+)', header.get('value', ''))
                     if match:
                         logging.info("Device ID sikeresen kinyerve a Cookie fejlécből.")
                         return match.group(1).strip()

        # 3. Keresés a POST kérések törzsében
        if 'request' in entry and entry['request'].get('method') == 'POST':
             if '/device/anonymous/' in entry['request'].get('url', ''):
                 post_data = entry['request'].get('postData', {}).get('text')
                 if post_data:
                     try:
                         data_obj = json.loads(post_data)
                         if 'device_id' in data_obj:
                             logging.info("Device ID sikeresen kinyerve a POST törzsből.")
                             return data_obj['device_id'].strip()
                     except:
                         pass
                         
    logging.warning("Nem találtam Device ID-t a HAR logban.")
    return None

# --- ÚJ FÜGGVÉNY: Tubi API hívás a Render szerveren belül ---
def make_internal_tubi_api_call(search_url: str, access_token: str, device_id: str) -> dict | None:
    """
    Belső Tubi API hívást hajt végre a Render szerveren lévő tokenekkel.
    """
    logging.info("Indul a belső Tubi API kérés a geo-korlátozás megkerülésére...")
    
    try:
        # A search term kinyerése az URL-ből
        parsed_url = urlparse(search_url)
        search_term_encoded = parsed_url.path.split('/')[-1]
        
        # A full_api_url felépítése a template-ből
        full_api_url = TUBI_API_TEMPLATE.format(search_term=search_term_encoded)
        
        # Headerek összeállítása
        cookie_value = f'deviceId={device_id}; at={access_token}'
        
        headers = {
            'X-Tubi-Client-Name': 'web',
            'X-Tubi-Client-Version': '5.2.1',  
            'Content-Type': 'application/json',
            'Referer': 'https://tubitv.com/',
            'Origin': 'https://tubitv.com',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Cookie': cookie_value, 
            'Authorization': f'Bearer {access_token}', # KRITIKUS FEJLÉC
            'x-client-id': 'web',
            'x-tubi-client-id': 'web',
        }
        
        # Szinkron requests hívás (Render szerverről)
        response = requests.get(full_api_url, headers=headers, timeout=30)
        response.raise_for_status()
        
        logging.info(f"Belső API kérés sikeres. Státusz: {response.status_code}")
        return response.json()
        
    except requests.exceptions.HTTPError as e:
        response_data = {'api_call_status': 'failure', 'error': f'HTTP Error {response.status_code}', 'api_response_text': response.text[:200]}
        logging.error(f"Belső Tubi API HTTP Hiba: Státusz {response.status_code}. Részletek: {e}")
        return response_data
    except requests.exceptions.RequestException as e:
        logging.error(f"Belső Tubi API Hálózati Hiba: {e}")
        return {'api_call_status': 'failure', 'error': f'Network Error: {e}'}
    except Exception as e:
        logging.error(f"Belső Tubi API váratlan hiba: {e}")
        return {'api_call_status': 'failure', 'error': f'Unexpected Error: {e}'}


# --- FŐ ASZINKRON SCRAPE FÜGGVÉNY ---
async def scrape_tubitv(url: str, har_enabled: bool) -> dict:
    browser = None
    har_path = None
    results = {
        'status': 'failure',
        'url': url,
        'full_html': 'HTML tartalom nem elérhető.',
        'console_logs': [],
        'simple_network_log': [],
        'har_log': 'HAR log nem készült.',
        'tubi_token': None,
        'tubi_api_data': None, # <-- ÚJ MEZŐ
    }
    
    # Kinyeri a keresési szót, mert ez kell a Playwright logikájához
    search_query = urlparse(url).path.split('/')[-1] if 'search/' in url else 'home'
    
    # Ideiglenes fájl létrehozása a HAR-nak
    if har_enabled:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.har')
        har_path = temp_file.name
        temp_file.close()

    try:
        async with async_playwright() as p:
            # Playwright beállítása 
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                record_har_path=har_path if har_enabled else None,
                # További beállítások itt...
            )
            page = await context.new_page()

            # Navigálás és várakozás
            await page.goto(url, wait_until="networkidle", timeout=90000)
            
            # HTML kinyerése
            results['full_html'] = await page.content()
            # ... ide jönne a console_logs és simple_network_log gyűjtés
            
            # A HAR fájl lezárása
            await context.close()
            
            # HAR tartalom beolvasása, ha engedélyezve van
            if har_enabled and os.path.exists(har_path):
                with open(har_path, 'r', encoding='utf-8') as f:
                    har_data = json.load(f)
                results['har_log'] = har_data
                
                # Token kinyerése
                access_token = extract_tubi_token_from_har(har_data)
                results['tubi_token'] = access_token
                
                # --- KRITIKUS RÉSZ: BELSŐ API HÍVÁS ---
                if access_token:
                    device_id = extract_device_id_from_har(har_data)
                    
                    if device_id:
                        logging.info("Token és Device ID sikeresen kinyerve. Indul a belső API hívás.")
                        
                        # Aszinkron futtatás (Flask/asyncio környezetben elengedhetetlen a szinkron requests híváshoz)
                        loop = asyncio.get_event_loop()
                        api_data = await loop.run_in_executor(
                            None, 
                            make_internal_tubi_api_call, 
                            url, # Az eredeti keresési URL a keresési kifejezés kinyeréséhez
                            access_token, 
                            device_id
                        )
                        
                        results['tubi_api_data'] = api_data
                        
                    else:
                        logging.warning("Nem sikerült kinyerni a Device ID-t. A belső API hívás kihagyva.")
                        
            # Visszaállítjuk a státuszt success-re, ha a scrape sikeres volt
            results['status'] = 'success'
            
    except Exception as e:
        logging.error(f"Scraping hiba: {e}")
        results['error'] = str(e)
        
    finally:
        if browser:
            await browser.close()
            
        if har_path and os.path.exists(har_path):
            try:
                os.remove(har_path)
                logging.info(f"Ideiglenes HAR fájl törölve: {har_path}")
            except Exception as remove_e:
                logging.warning(f"Nem sikerült törölni az ideiglenes fájlt: {har_path}. Hiba: {remove_e}")
            
    return results

# --- FLASK ROUTE ---
@app.route('/scrape', methods=['GET'])
def scrape_endpoint():
    url = request.args.get('url')
    # A 'target_api' is bekapcsolja a HAR logolást, ahogy a kliens is teszi
    har_enabled = request.args.get('har', 'false').lower() == 'true' or request.args.get('target_api', 'false').lower() == 'true'

    if not url:
        return jsonify({'status': 'failure', 'error': 'Hiányzó URL paraméter.'}), 400

    logging.info(f"Kérés érkezett: {url}, HAR logolás: {har_enabled}")
    
    loop = asyncio.get_event_loop()
    # A loop futtatja az aszinkron scrape_tubitv függvényt
    data = loop.run_until_complete(scrape_tubitv(url, har_enabled))
    
    return jsonify(data)

# A többi Flask boilerplate (pl. 'if __name__ == "__main__":' vagy 'gunicorn' beállítások) ide jönne.
