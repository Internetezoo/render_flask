from flask import Flask, jsonify, request
import asyncio
# VISSZAÁLLÍTVA: TimeoutError importálását kivettük, hogy elkerüljük az 'Page' hiba miatti összeomlást
from playwright.async_api import async_playwright
import os
import json
import datetime 

app = Flask(__name__)

# A kliens scriptben továbbra is szükség van erre a mintára a kereséshez, de a szerveroldali kód most nem használja.
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
    
    har_path = f"/tmp/network_{os.getpid()}.har" 

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=['--no-sandbox', '--disable-setuid-sandbox'] 
        )
        context = await browser.new_context(record_har_path=har_path)
        page = await context.new_page()

        # Konzol és hálózati logolás (változatlan)
        def log_console_message(msg):
            results["console_logs"].append({
                "type": msg.type,
                "text": msg.text,
                "location": msg.location['url'] if msg.location else 'N/A'
            })
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
            
            # Visszatérés a networkidle-höz, ami a legtöbb esetben befejezi a kérést.
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            results["simple_network_log"].append("A fő kérés (networkidle) befejeződött.")
            
            # KRITIKUS JAVÍTÁS: Explicit várakozás 2 másodpercig. 
            # Ez a várakozás biztosítja, hogy a Tubi API válasza bekerüljön a HAR logba, mielőtt lezárnánk.
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
            
            # HAR log beolvasása
            try:
                with open(har_path, 'r', encoding='utf-8') as f:
                    results["har_log"] = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                 results["har_log"] = "Hiba: HAR log nem készült vagy érvénytelen."
            
            if os.path.exists(har_path):
                 os.remove(har_path)
            
            results["simple_network_log"].append("--- Egyszerűsített Hálózati Log Befejeződött ---")
            
    return results

@app.route('/scrape', methods=['GET'])
def run_scrape():
    target_url = request.args.get('url', 'https://example.com')

    try:
        data = asyncio.run(scrape_website_with_network_log(target_url))
    except RuntimeError as e:
        return jsonify({
            "status": "failure",
            "error": f"Aszinkron futási hiba: {str(e)}"
        }), 500
    
    if data.get('status') == 'failure':
         return jsonify(data), 500 
            
    return jsonify(data)

if __name__ == '__main__':
    app.run(debug=True, port=os.environ.get('PORT', 5000))
