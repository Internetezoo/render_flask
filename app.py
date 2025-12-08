from flask import Flask, jsonify, request
import asyncio
from playwright.async_api import async_playwright

app = Flask(__name__)

# Aszinkron funkció a web scraping elvégzésére Playwright-tal
async def scrape_website(url):
    results = {}
    async with async_playwright() as p:
        # Indítsuk el a böngészőt (headless módban)
        browser = await p.chromium.launch()
        page = await browser.new_page()

        try:
            # Navigáció a megadott URL-re
            await page.goto(url)

            # Várjunk meg egy elemet, hogy biztosan betöltődjön a tartalom
            await page.wait_for_selector('h1', timeout=5000)

            # Példa: Kinyerjük a weboldal címét és az első H1 tag tartalmát
            title = await page.title()
            h1_text = await page.inner_text('h1')

            results = {
                "title": title,
                "first_heading": h1_text,
                "status": "success"
            }
        except Exception as e:
            results = {
                "error": str(e),
                "status": "failure"
            }
        finally:
            await browser.close()
            
    return results

@app.route('/scrape', methods=['GET'])
def run_scrape():
    # Kinyerjük a scrape-elendő URL-t a query paraméterekből
    target_url = request.args.get('url', 'https://example.com')

    # A Playwright aszinkron, ezért futtatni kell az asyncio.run segítségével
    # Fontos: A Flask szálkezelése miatt ez nem mindig ideális éles környezetben,
    # de egyszerű példának megfelel.
    data = asyncio.run(scrape_website(target_url))
    
    # Visszaadjuk az eredményt JSON formátumban
    return jsonify(data)

if __name__ == '__main__':
    # A Render.com portot ad meg környezeti változóban
    # Éles környezetben ezt használd:
    # app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000))
    # Helyi teszteléshez:
    app.run(debug=True, port=5000)
