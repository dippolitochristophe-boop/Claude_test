"""
playwright_strategies.py — Scraper Playwright multi-stratégies pour SPAs

Remplace le scrape_site() naïf en déployant toutes les stratégies connues
dans l'ordre, sans nécessiter de script de debug séparé.

Flux d'exécution :
  1. Intercepte TOUTES les réponses JSON dès le départ
  2. Navigue vers l'URL (networkidle → load → fallback)
  3. Accepte le cookie consent (multi-pattern)
  4. Attend les job links dans le DOM (multi-selector)
  5. Parse le DOM → si ≥1 job : retourne
  6. Sinon : analyse les APIs interceptées → auto-détecte structure jobs
  7. Dernier recours : fallback requests (pas de JS)

Usage :
  from playwright_strategies import smart_scrape_site
  jobs = smart_scrape_site(site_config, pw_page)
"""

import re
import sys
import time
import requests
import urllib3

# Fix Unicode output on Windows (cp1252 → utf-8)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
from bs4 import BeautifulSoup
from urllib.parse import urlparse

urllib3.disable_warnings()

# ── Patterns cookie consent (ordre : du plus spécifique au plus générique) ────

COOKIE_SELECTORS = [
    # Piwik Pro
    "#ppms_cm_agree-to-all",
    # OneTrust
    "#onetrust-accept-btn-handler",
    ".onetrust-accept-btn-handler",
    # CookieBot
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    # Cookieyes / generic
    ".cc-accept-all",
    ".cc-accept",
    "[data-testid='accept-all-cookies']",
    "[aria-label*='Accept all']",
    # Texte générique (Playwright :has-text)
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Alle akzeptieren')",
    "button:has-text('Tout accepter')",
    "button:has-text('Allow all')",
    "button:has-text('I agree')",
    "button:has-text('Agree')",
    "#accept-all",
    ".accept-all-btn",
    ".js-accept-all-cookies",
]

# ── Patterns de job links à tester dans le DOM ────────────────────────────────

JOB_LINK_PATTERNS = [
    "a[href*='/job/']",
    "a[href*='/jobs/']",
    "a[href*='/vacancy/']",
    "a[href*='/vacancies/']",
    "a[href*='/career/']",
    "a[href*='/careers/']",
    "a[href*='/position/']",
    "a[href*='/posting/']",
    "a[href*='/postings/']",
    "a[href*='/offre/']",
    "a[href*='/offer/']",
    "a[href*='/application/']",
    "a[href*='/detail/']",
    "a[href*='/jobdetail/']",
]

# ── Clés typiques dans les réponses JSON d'ATS ────────────────────────────────
# Couvre : Workday, SmartRecruiters, Greenhouse, Lever, Taleo, SAP SF,
#          Phenom People, iCIMS, Algolia, portails custom

JOB_LIST_KEYS   = [
    # Standards
    "jobs", "postings", "items", "results", "content", "data",
    "jobPostings", "vacancies", "positions", "offers", "hits",
    # ATS spécifiques
    "jobList", "jobOffers", "openPositions", "requisitions",
    "opportunities", "listings", "records", "rows", "nodes",
    "edges", "collection", "list", "elements", "documents",
    # Phenom People / SAP SF
    "requisitionList", "jobRequisitions", "postingList",
    # iCIMS
    "openings", "jobs_list",
]
JOB_TITLE_KEYS  = [
    "title", "jobTitle", "name", "position", "label", "headline",
    # ATS spécifiques
    "jobName", "positionTitle", "requisitionTitle", "displayTitle",
    "jobTitleName", "roleName", "opportunityTitle", "job_title",
    "posting_title", "externalJobTitle",
]
JOB_URL_KEYS    = [
    "url", "link", "absoluteUrl", "absolute_url", "externalPath",
    "applyUrl", "jobUrl", "detailUrl", "slug", "path",
    "job_url", "apply_url", "hostedUrl", "canonicalUrl",
]
JOB_LOC_KEYS    = [
    "location", "city", "locationName", "locationsText", "place",
    "primaryLocation", "jobLocation", "officeLocation", "workLocation",
    "locationText", "locations",
]


def _dismiss_cookie_consent(page) -> str:
    """
    Tente d'accepter le cookie consent via un sélecteur CSS combiné (1 seul appel),
    puis fallback sur les patterns texte Playwright si rien trouvé.
    Retourne le selector qui a fonctionné, ou ''.
    """
    # Sélecteurs CSS stricts → 1 seul wait_for_selector combiné (rapide)
    CSS_SELECTORS = [s for s in COOKIE_SELECTORS if not s.startswith("button:has-text")]
    combined = ", ".join(CSS_SELECTORS)
    try:
        page.wait_for_selector(combined, timeout=1500)
        el = page.query_selector(combined)
        if el:
            el.click()
            page.wait_for_timeout(600)
            return combined
    except Exception:
        pass
    # Fallback : sélecteurs texte (has-text) — 300ms chacun
    for sel in [s for s in COOKIE_SELECTORS if s.startswith("button:has-text")]:
        try:
            page.click(sel, timeout=300)
            page.wait_for_timeout(600)
            return sel
        except Exception:
            continue
    return ""


def _navigate(page, url: str) -> str:
    """
    Navigation avec fallback progressif.
    Retourne 'networkidle' | 'load' | 'domcontentloaded' | 'error'.
    """
    timeouts = {"networkidle": 8000, "load": 15000, "domcontentloaded": 15000}
    for wait_until in ("networkidle", "load", "domcontentloaded"):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeouts[wait_until])
            return wait_until
        except Exception:
            continue
    return "error"


def _wait_for_jobs_dom(page, wait_for: str | None, job_pattern: str | None = None) -> str:
    """
    Attend que des job links apparaissent dans le DOM.
    Stratégie : wait_for config > sélecteur dérivé de job_pattern > JOB_LINK_PATTERNS.
    Retourne le premier sélecteur qui matche, ou ''.
    """
    # Priorité au wait_for explicite de la config
    if wait_for:
        try:
            page.wait_for_selector(wait_for, timeout=6000)
            if page.query_selector_all(wait_for):
                return wait_for
        except Exception:
            pass

    # Dériver un sélecteur depuis job_pattern si défini et non-wildcard
    derived: list[str] = []
    if job_pattern and job_pattern != "*":
        derived = [f"a[href*='{job_pattern}']"]

    # Sélecteur combiné : job_pattern dérivé + patterns génériques
    candidates = derived + JOB_LINK_PATTERNS
    combined = ", ".join(dict.fromkeys(candidates))  # déduplique, préserve l'ordre
    try:
        page.wait_for_selector(combined, timeout=6000)
        for sel in candidates:
            if page.query_selector(sel):
                return sel
    except Exception:
        pass
    return ""


def _extract_location(d: dict) -> str:
    """Extrait la localisation depuis un dict job API."""
    for k in JOB_LOC_KEYS:
        v = d.get(k)
        if not v:
            continue
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for sub in ("name", "city", "label"):
                sv = v.get(sub, "")
                if sv:
                    return sv
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                return first.get("name") or first.get("city") or ""
    return ""


def _find_job_list_in_body(body, max_depth: int = 3) -> list | None:
    """
    Cherche récursivement une liste de dicts ressemblant à des jobs.
    Priorité aux clés connues, puis exploration en profondeur.
    """
    if isinstance(body, list):
        if body and isinstance(body[0], dict):
            return body
        return None
    if not isinstance(body, dict) or max_depth == 0:
        return None
    # 1. Clés connues en priorité
    for k in JOB_LIST_KEYS:
        v = body.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    # 2. Exploration récursive des valeurs dict/list
    for v in body.values():
        if isinstance(v, (dict, list)):
            result = _find_job_list_in_body(v, max_depth - 1)
            if result and len(result) >= 1:
                return result
    return None


def _log_unrecognized_api(api_url: str, body) -> None:
    """Loggue la structure d'une API non reconnue — aide au diagnostic et à l'extension des clés."""
    short = api_url.split("?")[0][-70:]
    if isinstance(body, list):
        sample = body[0] if body else {}
        keys = list(sample.keys())[:10] if isinstance(sample, dict) else [type(sample).__name__]
        print(f"       • LIST[{len(body)}] {short}")
        print(f"         item_keys={keys}")
    elif isinstance(body, dict):
        top = list(body.keys())[:10]
        lists = {k: len(v) for k, v in body.items() if isinstance(v, list) and v}
        nested = {k: list(v.keys())[:6] for k, v in body.items() if isinstance(v, dict)}
        print(f"       • DICT {short}")
        print(f"         top_keys={top}  lists={lists}")
        if nested:
            print(f"         nested={nested}")


def _parse_api_jobs(body, company_name: str, validate_mode: bool = False) -> list[dict]:
    """
    Auto-détecte la structure d'une réponse API et extrait les jobs.
    validate_mode=True : désactive le filtre is_relevant_title (health-check, Agent 3).
    Retourne une liste de dicts jobs (sans score, bucket sera calculé par l'appelant).
    """
    from job_scrapper import is_relevant_title, get_location_bucket

    jobs = []
    seen = set()

    job_list = _find_job_list_in_body(body)

    if not job_list:
        return []

    for item in job_list:
        if not isinstance(item, dict):
            continue

        # Données peuvent être dans item directement ou dans item["data"]
        d = item.get("data") if isinstance(item.get("data"), dict) else item

        # Titre
        title = ""
        for k in JOB_TITLE_KEYS:
            v = d.get(k) or item.get(k)
            if v and isinstance(v, str) and v.strip():
                title = v.strip()
                break

        if not title or (not validate_mode and not is_relevant_title(title)):
            continue

        # URL
        url = ""
        for k in JOB_URL_KEYS:
            v = d.get(k) or item.get(k)
            if v and isinstance(v, str) and v.strip():
                url = v.strip()
                break

        # ID (Algolia : objectID à la racine)
        job_id = str(item.get("objectID") or d.get("id") or d.get("jobId") or "")

        # Si URL relative → à compléter par l'appelant
        if not url and job_id:
            slug = d.get("slug") or d.get("urlSlug") or ""
            if not slug:
                slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")
            url = f"/job/{slug}/{job_id}" if slug else f"/job/{job_id}"

        location = _extract_location(d) or _extract_location(item)

        dedup = job_id or url
        if dedup and dedup not in seen:
            seen.add(dedup)
            jobs.append({
                "title": title,
                "company": company_name,
                "location": location,
                "bucket": get_location_bucket(location),
                "description": "",
                "url": url,
                "date": (d.get("publicationDate") or d.get("date") or
                         item.get("updated_at") or "")[:10],
                "source": "API (auto-detected)",
                "score": 0,
            })

    return jobs


def smart_scrape_site(site: dict, pw_page, headers: dict = None,
                      validate_mode: bool = False) -> tuple[list[dict], str]:
    """
    Scrape un site avec toutes les stratégies disponibles.

    Retourne : (jobs, strategy_used)
    strategy_used : description de ce qui a fonctionné (pour le log)

    Stratégies dans l'ordre :
      1. Playwright + DOM (avec cookie consent + wait)
      2. Playwright + API intercept (si DOM = 0)
      3. requests fallback (si Playwright pas dispo ou erreur réseau)

    validate_mode=True : désactive is_relevant_title (health-check / Agent 3).
    """
    from job_scrapper import parse_jobs_from_html, is_relevant_title, get_location_bucket

    if pw_page is None:
        jobs = _requests_fallback(site, headers)
        return jobs, "requests-fallback (no browser)"

    base_url = site["pages"][0]
    base = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    company = site["name"]
    all_jobs = []
    seen_urls = set()

    # Filtres sur URLs à exclure des APIs (bruit)
    API_NOISE = ("_next", "piwik", "analytics", "gtm", "linkedin", "facebook",
                 "google", "doubleclick", "hotjar", "segment", "sentry")

    for page_url in site["pages"]:
        intercepted_apis: list[tuple[str, dict | list]] = []

        def on_response(response):
            url = response.url
            if any(n in url for n in API_NOISE):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = response.json()
                if isinstance(body, (dict, list)):
                    intercepted_apis.append((url, body))
            except Exception:
                pass

        pw_page.on("response", on_response)

        try:
            # ── Étape 1 : navigation ──────────────────────────────────────────
            nav_strategy = _navigate(pw_page, page_url)
            if nav_strategy == "error":
                pw_page.remove_listener("response", on_response)
                jobs = _requests_fallback_url(page_url, site, headers, validate_mode=validate_mode)
                for j in jobs:
                    dedup = j["url"].split("?")[0].rstrip("/")
                    if dedup not in seen_urls:
                        seen_urls.add(dedup)
                        all_jobs.append(j)
                continue

            # ── Étape 2 : cookie consent ──────────────────────────────────────
            consent_sel = _dismiss_cookie_consent(pw_page)

            # ── Étape 3 : scroll pour lazy-loading ───────────────────────────
            for _ in range(3):
                pw_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                pw_page.wait_for_timeout(600)

            # ── Étape 4 : attente DOM jobs ────────────────────────────────────
            found_sel = _wait_for_jobs_dom(pw_page, site.get("wait_for"), site.get("job_pattern"))

            # ── Étape 5 : parse DOM ───────────────────────────────────────────
            dom_jobs = parse_jobs_from_html(pw_page.content(), site, validate_mode=validate_mode)
            for j in dom_jobs:
                j["source"] = "Playwright"

            if dom_jobs:
                strategy = f"Playwright DOM (nav={nav_strategy}, consent={bool(consent_sel)}, sel={found_sel})"
                for j in dom_jobs:
                    dedup = j["url"].split("?")[0].rstrip("/")
                    if dedup not in seen_urls:
                        seen_urls.add(dedup)
                        all_jobs.append(j)
                pw_page.remove_listener("response", on_response)
                continue

            # ── Étape 6 : DOM vide → analyse APIs interceptées ────────────────
            api_jobs = []
            for api_url, body in intercepted_apis:
                candidate_jobs = _parse_api_jobs(body, company, validate_mode=validate_mode)
                for j in candidate_jobs:
                    if j["url"].startswith("/"):
                        j["url"] = base + j["url"]
                    api_jobs.append(j)

            if api_jobs:
                strategy = f"API auto-detected ({len(intercepted_apis)} APIs interceptées, consent={bool(consent_sel)})"
                for j in api_jobs:
                    dedup = j["url"].split("?")[0].rstrip("/")
                    if dedup not in seen_urls:
                        seen_urls.add(dedup)
                        all_jobs.append(j)
                pw_page.remove_listener("response", on_response)
                continue

            # ── Étape 7 : dernier recours requests ───────────────────────────
            # Debug : loggue les structures d'APIs non reconnues pour diagnostic
            if intercepted_apis:
                print(f"     ↳ DOM=0, {len(intercepted_apis)} API(s) interceptée(s) — structures non reconnues :")
                for api_url, body in intercepted_apis:
                    _log_unrecognized_api(api_url, body)
            else:
                print(f"     ↳ DOM=0, aucune API JSON interceptée → requests fallback")
            jobs = _requests_fallback_url(page_url, site, headers, validate_mode=validate_mode)
            for j in jobs:
                dedup = j["url"].split("?")[0].rstrip("/")
                if dedup not in seen_urls:
                    seen_urls.add(dedup)
                    all_jobs.append(j)

        except Exception as e:
            print(f"     ↳ smart_scrape erreur ({str(e)[:80]})")
        finally:
            try:
                pw_page.remove_listener("response", on_response)
            except Exception:
                pass

        time.sleep(0.8)

    strategy = "smart_scrape"
    return all_jobs, strategy


def _requests_fallback(site: dict, headers: dict = None, validate_mode: bool = False) -> list[dict]:
    jobs = []
    for url in site["pages"]:
        jobs.extend(_requests_fallback_url(url, site, headers, validate_mode=validate_mode))
        time.sleep(0.5)
    return jobs


def _requests_fallback_url(url: str, site: dict, headers: dict = None,
                            validate_mode: bool = False) -> list[dict]:
    from job_scrapper import parse_jobs_from_html
    _h = headers or {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,*/*",
    }
    try:
        r = requests.get(url, headers=_h, timeout=15, verify=False)
        if r.status_code == 200:
            jobs = parse_jobs_from_html(r.text, site, validate_mode=validate_mode)
            for j in jobs:
                j["source"] = "requests"
            return jobs
        else:
            print(f"     ↳ requests HTTP {r.status_code}")
    except Exception as e:
        print(f"     ↳ requests erreur ({str(e)[:60]})")
    return []
