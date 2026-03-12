"""Debug Uniper — capture requêtes vers /api/filter/query pour implémenter scraper direct."""
import json
from playwright.sync_api import sync_playwright

all_responses = []
api_requests = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    # Capturer les détails des requêtes vers l'API jobs
    def on_request(request):
        if "careers.uniper.energy/api" in request.url:
            api_requests.append({
                "method": request.method,
                "url": request.url,
                "post_data": request.post_data,
                "headers": {k: v for k, v in request.headers.items()
                            if k.lower() in ("content-type", "accept", "authorization", "x-requested-with")},
            })

    # Capturer les réponses JSON
    def on_response(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        if "json" in ct and "_next" not in url and "piwik" not in url and "linkedin" not in url:
            try:
                body = response.json()
                all_responses.append((response.status, url, body))
            except Exception:
                pass

    page.on("request", on_request)
    page.on("response", on_response)

    # URL correcte = /en (pas /en/search/ qui est une 404)
    print("Chargement de https://careers.uniper.energy/en ...")
    try:
        page.goto("https://careers.uniper.energy/en", wait_until="networkidle", timeout=30000)
    except Exception:
        page.goto("https://careers.uniper.energy/en", wait_until="load", timeout=30000)

    # Accepter le cookie consent si présent
    try:
        page.click("#ppms_cm_agree-to-all", timeout=3000)
        print("Cookie consent accepté")
        page.wait_for_timeout(3000)
    except Exception:
        pass

    page.wait_for_timeout(8000)

    # Requêtes API interceptées
    print(f"\n--- Requêtes vers /api ({len(api_requests)}) ---")
    for req in api_requests:
        print(f"  {req['method']} {req['url']}")
        print(f"    headers: {req['headers']}")
        if req['post_data']:
            print(f"    body: {req['post_data'][:500]}")
        else:
            print(f"    body: (none / GET params dans URL)")

    # Détail réponse jobs
    print(f"\n--- Réponses JSON ({len(all_responses)}) ---")
    for status, url, body in all_responses:
        if "filter/query" in url:
            print(f"  [{status}] {url}")
            print(f"    totalHits: {body.get('totalHits')} | jobsPerPage: {body.get('jobsPerPage')}")
            print(f"    page: {body.get('page')} | nextPage: {body.get('nextPage')}")
            jobs = body.get("jobs", [])
            print(f"    jobs[0] keys: {list(jobs[0].keys()) if jobs else 'N/A'}")
            if jobs:
                j = jobs[0]
                print(f"    exemple job: title={j.get('title')} | url={j.get('url') or j.get('link') or j.get('slug')}")
        else:
            print(f"  [{status}] {url[:100]}")

    browser.close()
    print("\nTerminé.")
