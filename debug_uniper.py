"""Debug Uniper — capture toutes les requêtes réseau pour trouver l'API jobs."""
from playwright.sync_api import sync_playwright

all_responses = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    # Capturer TOUTES les réponses JSON
    def on_response(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        if "json" in ct and "_next" not in url and "piwik" not in url:
            try:
                body = response.json()
                all_responses.append((response.status, url, body))
            except Exception:
                pass

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

    # Attendre plus longtemps
    page.wait_for_timeout(8000)

    # Liens dans le DOM
    all_links = page.query_selector_all("a[href]")
    print(f"\nTotal <a href> : {len(all_links)}")
    job_links = [a for a in all_links if "/job/" in (a.get_attribute("href") or "")]
    print(f"Liens /job/ : {len(job_links)}")
    for a in job_links[:5]:
        print(f"  {a.get_attribute('href')[:100]}")

    # Réponses JSON capturées
    print(f"\n--- Réponses JSON ({len(all_responses)}) ---")
    for status, url, body in all_responses:
        keys = list(body.keys()) if isinstance(body, dict) else type(body).__name__
        count = len(body) if isinstance(body, list) else (len(body.get("jobs", body.get("postings", body.get("items", [])))) if isinstance(body, dict) else "?")
        print(f"  [{status}] {url[:100]}")
        print(f"    keys/type: {keys} | items: {count}")

    browser.close()
    print("\nTerminé.")
