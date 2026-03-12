"""Debug Uniper — inspecte le DOM après chargement Playwright."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36")

    print("Chargement de la page...")
    try:
        page.goto("https://careers.uniper.energy/en/search/?searchKeyword=", wait_until="networkidle", timeout=30000)
    except Exception:
        page.goto("https://careers.uniper.energy/en/search/?searchKeyword=", wait_until="load", timeout=30000)

    page.wait_for_timeout(5000)  # attendre 5s de plus pour le JS

    # Total liens <a>
    all_links = page.query_selector_all("a[href]")
    print(f"\nTotal <a href> dans le DOM : {len(all_links)}")

    # Liens contenant "job"
    job_links = [a for a in all_links if "job" in (a.get_attribute("href") or "").lower()]
    print(f"Liens contenant 'job' : {len(job_links)}")
    for a in job_links[:10]:
        print(f"  href={a.get_attribute('href')} | text={a.inner_text()[:60]}")

    # Chercher des éléments qui pourraient être des offres (div, li, etc.)
    print("\n--- Recherche d'éléments avec data-job ou data-id ---")
    for sel in ["[data-job-id]", "[data-id]", "[data-posting-id]", ".job-listing", ".job-card", ".search-result"]:
        els = page.query_selector_all(sel)
        if els:
            print(f"  {sel} : {len(els)} éléments")

    # Intercepter les requêtes réseau pour trouver l'API
    print("\n--- Sauvegarder le HTML pour inspection ---")
    with open("uniper_debug.html", "w", encoding="utf-8") as f:
        f.write(page.content())
    print("HTML sauvegardé dans uniper_debug.html")

    browser.close()
    print("\nTerminé.")
