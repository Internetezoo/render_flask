import asyncio
import nest_asyncio
import json
import logging
import base64
import tempfile 
import os        
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright

# Engedélyezi az aszinkron funkciók beágyazását
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.INFO)

# --- SEGÉDFÜGGVÉNY A TOKEN KINYERÉSÉRE ---
# (Változatlan a korábbi fixhez képest)
def extract_tubi_token_from_har(har_data: dict) -> str | None:
    """Kinyeri az access_token-t a Tubi TV HAR logjából a 'device/anonymous/token' válaszából."""
    TUBI_TOKEN_ENDPOINT = "account.production-public.tubi.io/device/anonymous/token"
    
    if not har_data or not isinstance(har_data, dict) or 'log' not in har_data:
        return None
        
    try:
        for entry in har_data['log']['entries']:
            url = entry['request']['url']
            
            if TUBI_TOKEN_ENDPOINT in url:
                response_content = entry['response']['content']
                
                if response_content and 'text' in response_content:
                    response_text = response_content['text']
                    
                    if response_content.get('encoding') == 'base64':
                        try:
                             response_text = base64.b64decode(response_text).decode('utf-8')
                        except:
                             logging.warning("Base64 dekódolási hiba.")
                             continue
                            
                    try:
                        response_json = json.loads(response_text)
                        access_token = response_json.get('access_token')
                        
                        if access_token:
                            return access_token
                            
                    except json.JSONDecodeError:
                        logging.warning("Nem érvényes JSON válasz a token endpoint-ról.")
                        continue
                        
        return None
        
    except KeyError as e:
        logging.error(f"Hiba a HAR bejegyzés feldolgozásakor: {e}")
        return None

# --- PLAYWRIGHT SCRAPING FÜGGVÉNY (JAVÍTVA) ---
async def scrape_website_with_network_log(url: str, har_enabled: bool = False, request_args: dict = None) -> dict:
    
    results = {
        'status': 'failure',
        'error': 'Ismeretlen hiba történt.',
        'full_html': None,
        'console_logs': [],
        'simple_network_log': [],
        'har_log': None,
        'tubi_token': None,
    }
    
    har_path = None
    browser = None
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch() 

            context_options = {}
            if har_enabled:
                temp_file = tempfile.NamedTemporaryFile(suffix=".har", delete=False)
                har_path = temp_file.name
                temp_file.close() 

                context_options['record_har_path'] = har_path
                context_options['record_har_omit_content'] = False 
                
                logging.info(f"HAR logolás engedélyezve, mentés ideiglenes fájlba: {har_path}")

            # Context létrehozása a HAR logolási opciókkal
            context = await browser.new_context(**context_options)
            
            # --- JAVÍTVA: A 'page' OBJEKTUM LÉTREHOZÁSA AZONNAL ---
            page = await context.new_page() 
            
            # --- JAVÍTVA: ESEMÉNYKEZELŐK RÖGZÍTÉSE MÁR A LÉTREHOZOTT 'page' OBJEKTUMRA ---
            simple_network_log = []
            page.on("request", lambda request: simple_network_log.append(f"KÉRÉS | Típus: {request.resource_type:<10} | URL: {request.url}"))
            page.on("response", lambda response: simple_network_log.append(f"VÁLASZ | Státusz: {response.status:<3} | URL: {response.url}"))

            console_logs = []
            page.on("console", lambda msg: console_logs.append({
                'type': msg.type, 
                'text': msg.text, 
                'location': msg.location['url'] if msg.location and 'url' in msg.location else 'N/A'
            }))
            # --------------------------------------------------------------------------------

            # Navigálás
            await page.goto(url, wait_until='domcontentloaded', timeout=45000) 
            await asyncio.sleep(1.5)

            results['full_html'] = await page.content()
            results['console_logs'] = console_logs
            results['simple_network_log'] = simple_network_log
            results['status'] = 'success'

            # --- SZERVER OLDALI FELDOLGOZÁS ---
            if har_enabled and har_path:
                # ... (A HAR fájl beolvasása és token kinyerés logikája változatlan) ...
                try:
                    with open(har_path, 'r', encoding='utf-8') as f:
                        har_log = json.load(f)
                    
                    token = extract_tubi_token_from_har(har_log)
                    if token:
                        results['tubi_token'] = token
                        logging.info("Tubi token sikeresen kinyerve a szerveren.")
                    
                    if request_args and request_args.get('har', 'false').lower() == 'true':
                        results['har_log'] = har_log
                        logging.info("HAR log visszaküldve a kliens kérésére.")
                    else:
                        logging.info("HAR log elhagyva a válaszból (optimalizáció).")

                except Exception as file_e:
                    logging.error(f"Hiba a HAR fájl olvasásakor/elemzésekor: {file_e}")
                    results['error'] = results.get('error', '') + f" (HAR feldolgozási hiba: {file_e})"


    except Exception as e:
        results['status'] = 'failure'
        results['error'] = str(e)
        logging.error(f"Scraping hiba: {e}")
        
    finally:
        if browser:
            await browser.close()
        # Végül töröljük az ideiglenes fájlt!
        if har_path and os.path.exists(har_path):
            try:
                os.remove(har_path)
                logging.info(f"Ideiglenes HAR fájl törölve: {har_path}")
            except Exception as remove_e:
                logging.warning(f"Nem sikerült törölni az ideiglenes fájlt: {har_path}. Hiba: {remove_e}")
            
    return results

# --- FLASK ROUTE (VÁLTOZATLAN) ---
@app.route('/scrape', methods=['GET'])
def scrape_endpoint():
    url = request.args.get('url')
    har_enabled = request.args.get('har', 'false').lower() == 'true' or request.args.get('target_api', 'false').lower() == 'true'

    if not url:
        return jsonify({'status': 'failure', 'error': 'Hiányzó URL paraméter.'}), 400

    logging.info(f"Kérés érkezett: {url}, HAR logolás: {har_enabled}")
    
    loop = asyncio.get_event_loop()
    if loop.is_running():
        data = loop.run_until_complete(scrape_website_with_network_log(url, har_enabled, request.args))
    else:
        data = asyncio.run(scrape_website_with_network_log(url, har_enabled, request.args))

    return jsonify(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
