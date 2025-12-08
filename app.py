from flask import Flask, jsonify, request
import asyncio
from playwright.async_api import async_playwright
import os
import json
import datetime 

app = Flask(__name__)

# A TUBI_API_BASE_URL_PATTERN-t most már csak a kliens script használja, de itt hagyhatjuk megjegyzésben.
# TUBI_API_BASE_URL_PATTERN = "https://search.production-public.tubi.io/api/v2/search"

# Bevezetjük a har_enabled kapcsolót
async def scrape_website_with_network_log(url, har_enabled=False):
    log_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    results = {
        "url": url,
        "title": "",
        "full_html": "",
        "har_log": "HAR log kérés szerint letiltva." if not har_enabled else "HAR log rögzítése folyamatban...",
        "console_logs": [], 
        "simple_network_log": [f"[{log_time}] --- Egyszerűsített Hálózati Log Indul ---"],
        "status": "failure",
        "error": "" 
    }
    
    har_path = None
    context_options = {}

    # HAR logolás konfigurálása csak akkor, ha engedélyezve van
    if har_enabled:
        har_path = f"/tmp/network_{os.getpid()}.har" 
        context_options["record_har_path"] = har_path

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=['--no-sandbox', '--disable-setuid-sandbox'] 
        )
        
        # A context_options vagy üres, vagy tartalmazza a HAR rögzítési útvonalat
        context = await browser.new_context(**context_options)
        page = await context.new_page()

        # Konzol és hálózati logolás (változatlan)
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

        try:
            results["simple_network_log"].append(f"Navigálás az oldalra: {url}")
            
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            results["simple_network_log"].append("A fő kérés (networkidle) befejeződött.")
            
            # Csak akkor várunk, ha a HAR engedélyezve van (a HAR logolási race condition elkerülése végett)
            if har_enabled:
                await asyncio.sleep(2) 
                results["simple_network_log"].append("2 másodpercnyi extra várakozás a HAR log teljességéért.")
            
            results["title"] = await page.title()
            results["full_html"] = await page.content() 
            results["status"] = "success"

        except Exception as e:
            error_msg = f"Playwright hiba történt a navigáció során: {str(e)}"
            results["error"] = error_msg
            results["simple_network_log"].append(f"HIBA: {error_msg}")
        
        finally:
            await context.close()
            await browser.close()
            
            # HAR log beolvasása csak akkor, ha engedélyezve volt
            if har_enabled and har_path:
                try:
                    with open(har_path, 'r', encoding='utf-8') as f:
                        results["har_log"] = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                     results["har_log"] = "Hiba: HAR log nem készült vagy érvénytelen."
                
                # Tisztítás
                if os.path.exists(har_path):
                     os.remove(har_path)
            
            results["simple_network_log"].append("--- Egyszerűsített Hálózati Log Befejeződött ---")
            
    return results

@app.route('/scrape', methods=['GET'])
def run_scrape():
    target_url = request.args.get('url', 'https://example.com')
    
    # ÚJ: Megnézzük, hogy a 'har=true' paramétert elküldték-e
    har_flag = request.args.get('har', 'false').lower() == 'true'
    
    try:
        data = asyncio.run(scrape_website_with_network_log(target_url, har_flag))
    except RuntimeError as e:
        return jsonify({"status": "failure", "error": f"Aszinkron futási hiba: {str(e)}"}), 500
    if data.get('status') == 'failure':
         return jsonify(data), 500 
    return jsonify(data)

if __name__ == '__main__':
    app.run(debug=True, port=os.environ.get('PORT', 5000))
