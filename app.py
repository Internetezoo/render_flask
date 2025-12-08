import asyncio
import nest_asyncio
import json
import logging
import base64 # Szükséges a Base64 dekódoláshoz, ha a HAR log úgy tárolja a válasz tartalmát
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright

# Engedélyezi az aszinkron funkciók beágyazását (szükséges a Gunicorn + Playwright async használatához)
nest_asyncio.apply()

app = Flask(__name__)
# Csökkenti a JSON válasz méretét, ami gyorsítja az átvitelt
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
logging.basicConfig(level=logging.INFO)

# --- SEGÉDFÜGGVÉNY A TOKEN KINYERÉSÉRE (SZERVER OLDALON) ---
def extract_tubi_token_from_har(har_data: dict) -> str | None:
    """Kinyeri az access_token-t a Tubi TV HAR logjából a 'device/anonymous/token' válaszából."""
    TUBI_TOKEN_ENDPOINT = "account.production-public.tubi.io/device/anonymous/token"
    
    if not har_data or not isinstance(har_data, dict) or 'log' not in har_data:
        return None
        
    try:
        for entry in har_data['log']['entries']:
            url = entry['request']['url']
            
            # 1. Megkeresi a token lekérő kérést
            if TUBI_TOKEN_ENDPOINT in url:
                
                # 2. Megvizsgálja a válasz tartalmát (content)
                response_content = entry['response']['content']
                
                if response_content and 'text' in response_content:
                    response_text = response_content['text']
                    
                    # HAR specifikáció: ha az encoding Base64, dekódolni kell
                    if response_content.get('encoding') == 'base64':
                        try:
                             response_text = base64.b64decode(response_text).decode('utf-8')
                        except:
                             logging.warning("Base64 dekódolási hiba.")
                             continue
                            
                    # 3. Elemezi a JSON stringet
                    try:
                        response_json = json.loads(response_text)
                        
                        # 4. Kinyeri a tokent
                        access_token = response_json.get('access_token')
                        
                        if access_token:
                            return access_token
                            
                    except json.JSONDecodeError:
                        logging.warning(f"Nem érvényes JSON válasz a token endpoint-ról.")
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
        'tubi_token': None, # Ez lesz a szerver által kinyert token
    }
    
    # HAR logolás mindenképp szükséges, ha a token kinyerése a cél
    if not har_enabled:
        # Ha a HAR-t nem kérte a kliens, de a token kell, a belső logolást bekapcsoljuk
        # A Flask route-ban a 'har' paramétert használjuk ennek eldöntésére
        pass # A Flask route-ban kezeljük, de itt a har_enabled jelzi, hogy mi történik
    
    
    browser = None # Inicializálás a finally blokk számára
    try:
        async with async_playwright() as p:
            # Csak a Chromium kell a Playwright install chromium miatt
            browser = await p.chromium.launch() 
            
            # Context létrehozása HAR logolással, ha engedélyezve van
            context = await browser.new_context(
                record_har_mode='full' if har_enabled else None,
                # Fontos: omit_content=False, hogy a válasz testét is rögzítse
                record_har_omit_content=False if har_enabled else True, 
            )
            if har_enabled:
                logging.info("HAR logolás engedélyezve.")
                
            page = await context.new_page()
            
            # ... (Hálózati logok és Konzol logok gyűjtése logikája) ...
            simple_network_log = []
            page.on("request", lambda request: simple_network_log.append(f"KÉRÉS | Típus: {request.resource_type:<10} | URL: {request.url}"))
            page.on("response", lambda response: simple_network_log.append(f"VÁLASZ | Státusz: {response.status:<3} | URL: {response.url}"))

            console_logs = []
            page.on("console", lambda msg: console_logs.append({
                'type': msg.type, 
                'text': msg.text, 
                'location': msg.location['url'] if msg.location and 'url' in msg.location else 'N/A'
            }))
            
            # Navigálás: 45mp-re emelve a Gunicorn timeout miatt
            await page.goto(url, wait_until='domcontentloaded', timeout=45000) 
            await asyncio.sleep(1.5) # Várakozás a dinamikus betöltésre

            results['full_html'] = await page.content()
            results['console_logs'] = console_logs
            results['simple_network_log'] = simple_network_log
            results['status'] = 'success'

            # --- SZERVER OLDALI FELDOLGOZÁS ---
            if har_enabled:
                har_log = await context.har() 
                
                # 1. Token kinyerése
                token = extract_tubi_token_from_har(har_log)
                if token:
                    results['tubi_token'] = token
                    logging.info("Tubi token sikeresen kinyerve a szerveren.")
                
                # 2. HAR log feltöltése a válaszba, ha a kliens KÉRTE ('har' paraméter = true)
                # A request_args-ot a Flask route-ból kapjuk meg
                if request_args and request_args.get('har', 'false').lower() == 'true':
                    results['har_log'] = har_log
                    logging.info("HAR log visszaküldve a kliens kérésére.")
                else:
                    # Sávszélesség spórolás: ha a token megvan és nem kérték a HAR-t, nem küldjük el
                    logging.info("HAR log elhagyva a válaszból (optimalizáció).")


    except Exception as e:
        results['status'] = 'failure'
        results['error'] = str(e)
        logging.error(f"Scraping hiba: {e}")
        
    finally:
        if browser:
            await browser.close()
            
    return results

# --- FLASK ROUTE (JAVÍTVA) ---
@app.route('/scrape', methods=['GET'])
def scrape_endpoint():
    url = request.args.get('url')
    # A HAR logolás engedélyezése szükséges a szerver oldali kinyeréshez
    har_enabled = request.args.get('har', 'false').lower() == 'true' or request.args.get('target_api', 'false').lower() == 'true'

    if not url:
        return jsonify({'status': 'failure', 'error': 'Hiányzó URL paraméter.'}), 400

    logging.info(f"Kérés érkezett: {url}, HAR logolás: {har_enabled}")
    
    # asyncio futtatása a Flask szálban
    loop = asyncio.get_event_loop()
    if loop.is_running():
        # Átadjuk a kérés argumentumait a scrape függvénynek
        data = loop.run_until_complete(scrape_website_with_network_log(url, har_enabled, request.args))
    else:
        # Ez a blokk fut le, ha a Flaskot simán futtatjuk
        data = asyncio.run(scrape_website_with_network_log(url, har_enabled, request.args))

    return jsonify(data)

if __name__ == '__main__':
    # Helyi teszteléshez
    app.run(host='0.0.0.0', port=5000, debug=True)
