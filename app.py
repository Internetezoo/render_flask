from flask import Flask, jsonify, request
import asyncio
from playwright.async_api import async_playwright
import os
import json
import datetime
import nest_asyncio # 💡 Új import a Gunicorn stabilitásáért

# ALKALMAZÁS INICIALIZÁLÁSA (Kijavítva a NameError-t)
app = Flask(__name__)

# JAVÍTÁS: A Gunicorn/Playwright aszinkron probléma megoldása.
# Engedélyezi az asyncio.run() hívását egy már futó event loopon belül.
nest_asyncio.apply()

# A kliens script továbbra is ezt használja a kereséshez.
TUBI_API_BASE_URL_PATTERN = "https://search.production-public.tubi.io/api/v2/search"

async def scrape_website_with_network_log(url):
    log_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    results = {
        "url": url,
        "title": "",
        "full_html": "",
        "har_log": "HAR log nem készült.",
        "console_logs": [], 
        "simple_network_log": [f"[{log_time}] --- Egyszerűsített Hálózati Log Indul ---"],
        "status": "failure",
        "error": "" 
    }
    
    # A fájlútvonal az ideiglenes könyvtárban van definiálva a Render/Linux kompatibilitás érdekében
    har_path = f"/tmp/network_{os.getpid()}.har" 

    # Hozzáadtam a 'browser' változót None-ra inicializálva, hogy a 'finally' blokkban 
    # is biztonságosan tudja bezárni, ha a launch hibázna.
    browser = None
    
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                # A Render környezet megköveteli a --no-sandbox argumentumokat
                args=['--no-sandbox', '--disable-setuid-sandbox'] 
            )
            context = await browser.new_context(record_har_path=har_path)
            page = await context.new_page()

            # ... Konzol és hálózati logolás (változatlan) ...
            def log_console_message(msg):
                results["console_logs"].append({"type": msg.type, "text": msg.text, "location": msg.location['url'] if msg.location else 'N/A'})
            page.on("console", log_console_message)
            def log_request(request):
                log_entry = f"KÉRÉS | Típus: {request.resource_type:<10} | URL: {request.url}"
                results["simple_network_log"].append(log_entry)
            def log_response(response):
                log_entry = f"VÁLASZ | Státusz: {response.status:<3} | URL: {response.url}"
                results["simple_network_log"].append(log_entry)
            page.on("request", log_request)
            page.on("response", log_response)
            # ...

            results["simple_network_log"].append(f"Navigálás az oldalra: {url}")
            
            # Visszatérés a networkidle-höz.
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            results["simple_network_log"].append("A fő kérés (networkidle) befejeződött.")
            
            # KRITIKUS JAVÍTÁS: Extrém hosszú, 6 másodperces várakozás a HAR logolás befejezéséhez.
            await asyncio.sleep(6) 
            results["simple_network_log"].append("6 másodpercnyi extra várakozás a HAR log teljességéért.")
            
            results["title"] = await page.title()
            results["full_html"] = await page.content() 
            results["status"] = "success"

        except Exception as e:
            error_msg = f"Playwright hiba történt a navigáció során: {str(e)}"
            results["error"] = error_msg
            results["simple_network_log"].append(f"HIBA: {error_msg}")
        
        finally:
            if context:
                await context.close()
            if browser:
                await browser.close()
                
            # HAR log beolvasása (változatlan)
            try:
                with open(har_path, 'r', encoding='utf-8') as f:
                    results["har_log"] = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                results["har_log"] = "Hiba: HAR log nem készült vagy érvénytelen."
            
            if os.path.exists(har_path):
                os.remove(har_path)
            
            results["simple_network_log"].append("--- Egyszerűsített Hálózati Log Befejeződött ---")
            
    return results

# Útvonal-kezelő
@app.route('/scrape', methods=['GET'])
def run_scrape():
    target_url = request.args.get('url', 'https://example.com')
    try:
        # Az asyncio.run() hívás most már biztonságos a nest_asyncio.apply() miatt
        data = asyncio.run(scrape_website_with_network_log(target_url))
    except RuntimeError as e:
        return jsonify({"status": "failure", "error": f"Aszinkron futási hiba: {str(e)}"}), 500
        
    if data.get('status') == 'failure':
         return jsonify(data), 500 
         
    return jsonify(data)

# Ez a blokk csak akkor fut, ha lokálisan indítja (pl. python app.py), 
# Gunicorn nem használja a Render-en.
if __name__ == '__main__':
    # Helyi futtatáshoz a '0.0.0.0' használata javasolt, ha konténerben van
    # Bár a Gunicorn felülírja a portot a Render környezeti változójával.
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', debug=True, port=port)
