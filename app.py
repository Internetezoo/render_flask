# app.py - JAVÍTOTT VERZIÓ: PLUTO REDIRECT FIX + TUBI DEBUG MODE
import asyncio
import nest_asyncio
import json
import logging
import base64
import os
import time
import random
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright, Route
from urllib.parse import urlparse, parse_qs, unquote
import requests
import urllib.parse
from typing import Optional, Dict, List, Any

# Engedélyezzük az aszinkron loop-ot Flask alatt
nest_asyncio.apply()

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# Logging beállítása a szerveroldali debughoz
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- KONFIGURÁCIÓK ---
DEVICE_ID_HEADER = "X-Tubi-Client-Device-ID"
TUBI_CONTENT_API_BASE = "https://content-cdn.production-public.tubi.io/api/v2/content"
TUBI_CONTENT_API_PARAMS = "app_id=tubitv&platform=web&content_id={content_id}&device_id={device_id}&include_channels=true&pagination%5Bseason%5D={season_num}&pagination%5Bpage_in_season%5D={page_num}&pagination%5Bpage_size_in_season%5D={page_size}&limit_resolutions%5B%5D=h264_1080p&limit_resolutions%5B%5D=h265_1080p&video_resources%5B%5D=hlsv6_widevine_nonclearlead&video_resources%5B%5D=hlsv6_playready_psshv0&video_resources%5B%5D=hlsv6_fairplay&video_resources%5B%5D=hlsv6"

# --- SEGÉDFÜGGVÉNYEK ---
def decode_jwt_payload(jwt_token: str) -> Optional[str]:
    try:
        payload_base64 = jwt_token.split('.')[1]
        padding = '=' * (4 - len(payload_base64) % 4)
        payload_decoded = base64.b64decode(payload_base64 + padding).decode('utf-8')
        return json.loads(payload_decoded).get('device_id')
    except:
        return None

# --- ASZINKRON SCRAPER (STEALTH MODE) ---
async def scrape_tubitv(url: str, web_mode: bool = False) -> Dict:
    results = {'status': 'success', 'url': url, 'tubi_token': None, 'tubi_device_id': None, 'user_agent': None, 'html_content': None}
    
    async with async_playwright() as p:
        try:
            # Böngésző indítása stealth paraméterekkel
            browser = await p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            if not web_mode:
                # Token elkapó logika
                async def handle_request(route: Route):
                    headers = route.request.headers
                    # Tubi Token figyelése
                    if not results['tubi_token'] and 'authorization' in headers and 'Bearer' in headers['authorization']:
                        token = headers['authorization'].split('Bearer ')[1].strip()
                        results['tubi_token'] = token
                        # DEBUG kiírás a szerver logba
                        logging.info(f"🔑 [DEBUG] Tubi Token sikeresen kinyerve: {token[:20]}...")
                    
                    if not results['tubi_device_id'] and DEVICE_ID_HEADER.lower() in headers:
                        results['tubi_device_id'] = headers[DEVICE_ID_HEADER.lower()]
                    
                    await route.continue_()

                await page.route("**/*", handle_request)
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            else:
                # Egyszerű HTML mentés
                await page.goto(url, wait_until="load", timeout=30000)
                results['html_content'] = await page.content()

            results['user_agent'] = await page.evaluate('navigator.userAgent')
            await browser.close()
        except Exception as e:
            results['status'] = 'failure'
            results['error'] = str(e)
            logging.error(f"❌ Scrape hiba: {str(e)}")
            
    return results

# --- FLASK ENDPOINT ---
@app.route('/scrape', methods=['GET', 'POST'])
def scrape_endpoint():
    # --- POST MÓD: PLUTO TV ÉS EGYÉB PROXY KÉRÉSEK ---
    if request.method == 'POST':
        try:
            proxy_data = request.get_json()
            target_url = proxy_data.get('url')
            method = proxy_data.get('method', 'GET')
            
            # DEBUG: Logoljuk a bejövő kérést
            logging.info(f"📡 [PROXY] {method} kérés indítása -> {target_url}")

            # Kérés végrehajtása redirect követéssel
            res = requests.request(
                method=method,
                url=target_url,
                headers=proxy_data.get('headers', {}),
                json=proxy_data.get('json_data'),
                timeout=30,
                allow_redirects=True  # Fontos a 302-es átirányítások miatt!
            )
            
            # JAVÍTÁS: A finalUrl mező visszaküldése a letöltőnek
            logging.info(f"✅ [PROXY] Válasz: {res.status_code} | Végleges URL: {res.url}")
            
            return jsonify({
                "status": "success",
                "statusCode": res.status_code,
                "content": res.text,
                "finalUrl": res.url  # Ez oldja meg a Pluto 404-es hibát!
            })
        except Exception as e:
            logging.error(f"❌ [PROXY] Kritikus hiba: {str(e)}")
            return jsonify({"status": "failure", "error": str(e)}), 500

    # --- GET MÓD: TUBI / ROKU / WEB SCRAPE ---
    url = request.args.get('url')
    web_mode = str(request.args.get('web', '')).lower() == 'true'

    if not url:
        return jsonify({'status': 'failure', 'error': 'Hiányzó URL paraméter'}), 400

    logging.info(f"🔍 [SCRAPE] Feldolgozás: {url} (Web: {web_mode})")
    
    loop = asyncio.get_event_loop()
    final_data = loop.run_until_complete(scrape_tubitv(url, web_mode))

    if web_mode and final_data['status'] == 'success':
        return Response(final_data.get('html_content', ''), mimetype='text/html')

    return jsonify(final_data)

# --- INDÍTÁS ---
if __name__ == '__main__':
    # Render-en a PORT környezeti változót kell használni
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
