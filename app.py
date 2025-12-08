# app.py (RENDER.COM szerver)

async def scrape_website_with_network_log(url, har_enabled=False): # Változás itt: har_enabled bekerül
    results = {
        "url": url,
        "title": "",
        "full_html": "",
        "har_log": "HAR log nem készült.",
        "status": "failure",
        "tubi_token": None, # <--- ÚJ: Token tárolása
        "simple_network_log": [] # Simple log megmarad, ha használják
    }
    
    har_path = f"/tmp/network_{os.getpid()}.har" 

    async with async_playwright() as p:
        # ... browser beállítások ...
        browser = await p.chromium.launch(
            args=['--no-sandbox', '--disable-setuid-sandbox'] 
        )
        
        # A HAR rögzítést csak akkor kapcsoljuk be, ha kérjük
        context = await browser.new_context(record_har_path=har_path if har_enabled else None)
        page = await context.new_page()

        # --- ÚJ: Hálózati Figyelő a Token Kinyerésére ---
        async def handle_response(response):
            # A Tubi hitelesítési API URL-jét figyeljük
            if "account.production-public.tubi.io/device/anonymous/token" in response.url:
                try:
                    # Kinyerjük a választ (response body)
                    response_text = await response.text()
                    data = json.loads(response_text)
                    if 'token' in data:
                        # Mentjük a tokent a results dictionary-be
                        results['tubi_token'] = data['token']
                        print(f"Server Log: Tubi token found: {data['token'][:10]}...") 
                except Exception as e:
                    print(f"Server Log: Error parsing token response: {e}")
            
        page.on('response', handle_response)
        # ----------------------------------------------------

        try:
            # 1. Navigáció és JS futtatás
            await page.goto(url, wait_until="networkidle", timeout=60000) 
            # ... többi logika ...

            # 4. A rögzített HAR fájl tartalmának beolvasása (csak ha engedélyezve volt)
            if har_enabled:
                 # ... meglévő HAR beolvasási logika ...
                 try:
                    with open(har_path, 'r', encoding='utf-8') as f:
                        results["har_log"] = json.load(f)
                 except (FileNotFoundError, json.JSONDecodeError):
                      results["har_log"] = "Hiba: HAR log fájl probléma."
                 # ... meglévő tisztítás ...
                 if os.path.exists(har_path):
                     os.remove(har_path)

            results["full_html"] = await page.content()
            results["title"] = await page.title()
            results["status"] = "success"

        except Exception as e:
            results["error"] = f"Playwright hiba: {str(e)}"
        finally:
            # 3. Böngésző/Context bezárása
            await context.close()
            await browser.close()
            
    return results

@app.route('/scrape', methods=['GET'])
def run_scrape():
    """Flask útvonal a scrape folyamat indításához."""
    target_url = request.args.get('url', 'https://example.com')
    # <--- ÚJ: har paraméter beolvasása, ami most már van a kliens kódunkban
    har_enabled = request.args.get('har', 'false').lower() == 'true' 

    try:
        # Pass har_enabled to the async function
        data = asyncio.run(scrape_website_with_network_log(target_url, har_enabled))
    except RuntimeError as e:
        return jsonify({
            "status": "failure",
            "error": f"Aszinkron futási hiba: {str(e)}"
        }), 500
    
    # ... meglévő logikák ...
    return jsonify(data)
