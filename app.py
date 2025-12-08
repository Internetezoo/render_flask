from flask import Flask, jsonify, request
import asyncio
from playwright.async_api import async_playwright
import os
import json

app = Flask(__name__)

# Aszinkron funkció a web scraping és hálózati logolás elvégzésére
async def scrape_website_with_network_log(url):
    results = {
        "url": url,
        "title": "",
        "full_html": "",
        "har_log": "HAR log nem készült.",
        "status": "failure"
    }
    
    # A HAR (HTTP Archive) a teljes hálózati forgalmat rögzíti, mint a DevTools.
    # Ideiglenes fájl szükséges a HAR mentéséhez, a Playwright ezt kezeli.
    har_path = f"/tmp/network_{os.getpid()}.har" 

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            # Fontos: sandbox kikapcsolása konténer környezetben
            args=['--no-sandbox', '--disable-setuid-sandbox'] 
        )
        
        # Létrehozunk egy BrowserContext-et a HAR rögzítéssel
        context = await browser.new_context(record_har_path=har_path)
        page = await context.new_page()

        try:
            # 1. Navigáció és JS futtatás
            # Várakozás a teljes oldal betöltésére (lassabb, de biztosítja a JS futását)
            await page.goto(url, wait_until="networkidle", timeout=60000) # 60 mp timeout
            
            # 2. HTML és Cím kinyerése (JS futás utáni állapot)
            results["title"] = await page.title()
            results["full_html"] = await page.content() 
            results["status"] = "success"

        except Exception as e:
            results["error"] = f"Playwright hiba történt a navigáció során: {str(e)}"
        
        finally:
            # 3. HAR adatok kinyerése és a fájl bezárása
            await context.close()
            await browser.close()
            
            # 4. A rögzített HAR fájl tartalmának beolvasása
            try:
                with open(har_path, 'r', encoding='utf-8') as f:
                    # Mivel a HAR egy nagy JSON, beolvassuk és eltároljuk a válaszban
                    results["har_log"] = json.load(f)
            except FileNotFoundError:
                 results["har_log"] = "Hiba: HAR log fájl nem található."
            except json.JSONDecodeError:
                 results["har_log"] = "Hiba: A HAR fájl tartalma nem érvényes JSON."
            
            # 5. Tisztítás: Töröljük az ideiglenes HAR fájlt
            if os.path.exists(har_path):
                 os.remove(har_path)
            
    return results

@app.route('/scrape', methods=['GET'])
def run_scrape():
    """Flask útvonal a scrape folyamat indításához."""
    target_url = request.args.get('url', 'https://example.com')

    try:
        data = asyncio.run(scrape_website_with_network_log(target_url))
    except RuntimeError as e:
        return jsonify({
            "status": "failure",
            "error": f"Aszinkron futási hiba: {str(e)}"
        }), 500
    
    # Ha a Playwright hiba történt, 500-as státusszal térünk vissza a kliensnek
    if data.get('status') == 'failure':
        return jsonify(data), 500
        
    return jsonify(data)

if __name__ == '__main__':
    # Helyi teszteléshez
    app.run(debug=True, port=5000)
