import asyncio, nest_asyncio, json, logging, base64, os, requests
from flask import Flask, request, jsonify, Response
from playwright.async_api import async_playwright

nest_asyncio.apply()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

async def run_scrape(url, opts):
    res = {'status': 'success', 'url': url, 'tubi_token': None, 'html_content': None}
    har_path = f"temp_{os.getpid()}.har" if opts.get('har') else None
    
    async with async_playwright() as p:
        # Smart Stealth beállítások
        browser = await p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        context = await browser.new_context(record_har_path=har_path) if har_path else await browser.new_context()
        page = await context.new_page()

        if opts.get('console'):
            res['console_logs'] = []
            page.on("console", lambda m: res['console_logs'].append({'t': m.type, 'x': m.text}))
        
        async def intercept(route):
            auth = route.request.headers.get('authorization', '')
            if 'Bearer ' in auth and not res['tubi_token']:
                res['tubi_token'] = auth.split('Bearer ')[1].strip()
                if opts.get('simple'): res['simple_log'] = [f"Token captured: {res['tubi_token'][:15]}..."]
            await route.continue_()

        await page.route("**/*", intercept)
        try:
            # Networkidle a biztos token elkapáshoz
            await page.goto(url, wait_until="networkidle", timeout=60000)
            res['html_content'] = await page.content()
        except Exception as e:
            res['status'], res['error'] = 'failure', str(e)

        await context.close()
        if har_path and os.path.exists(har_path):
            with open(har_path, "r", encoding="utf-8") as f:
                res['har_content'] = json.load(f)
            os.remove(har_path)
        await browser.close()
    return res

@app.route('/scrape', methods=['GET', 'POST'])
def handle():
    if request.method == 'POST': # Pluto Proxy ág
        d = request.get_json()
        try:
            r = requests.request(d.get('method', 'GET'), d['url'], headers=d.get('headers'), timeout=30, allow_redirects=True)
            return jsonify({"status": "success", "content": r.text, "finalUrl": r.url, "statusCode": r.status_code})
        except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

    url = request.args.get('url')
    if not url: return jsonify({'error': 'Missing URL'}), 400
    
    # Paraméterek kinyerése a klienstől
    opts = {
        'har': request.args.get('har') == 'true',
        'console': request.args.get('console') == 'true',
        'simple': request.args.get('simple') == 'true'
    }
    
    data = asyncio.run(run_scrape(url, opts))
    
    if request.args.get('web') == 'true':
        return Response(data.get('html_content', ''), mimetype='text/html')
    return jsonify(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
