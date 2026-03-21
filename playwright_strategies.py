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

import json
import re
import sys
import time
import requests
import urllib3
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

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


def _wait_stable(page, timeout: int = 4000) -> None:
    """
    Attend que la page soit stable après une action pouvant déclencher une navigation
    (consent click, scroll, clic SPA). Fallback load → domcontentloaded si networkidle timeout.
    """
    for state in ("networkidle", "load"):
        try:
            page.wait_for_load_state(state, timeout=timeout)
            return
        except Exception:
            continue


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
            _wait_stable(page)  # consent peut déclencher une navigation SPA
            return combined
    except Exception:
        pass
    # Fallback : sélecteurs texte (has-text) — 300ms chacun
    for sel in [s for s in COOKIE_SELECTORS if s.startswith("button:has-text")]:
        try:
            page.click(sel, timeout=300)
            _wait_stable(page)  # consent peut déclencher une navigation SPA
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


def _ci_get_from(d_lower: dict, keys: list[str]) -> str:
    """Lookup case-insensitive dans un dict pré-lowercased. Retourne la première valeur string non vide."""
    for k in keys:
        v = d_lower.get(k.lower())
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _heuristic_title(d: dict) -> str:
    """Fallback titre : premier champ string 10-150 chars avec espace, sans http ni slash initial."""
    for v in d.values():
        if isinstance(v, str):
            s = v.strip()
            if 10 <= len(s) <= 150 and " " in s and not s.startswith(("http", "/")):
                return s
    return ""


def _heuristic_url(d: dict) -> str:
    """Fallback URL : premier champ string commençant par http ou /path."""
    for v in d.values():
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("http") or (s.startswith("/") and len(s) > 3):
                return s
    return ""


def _extract_location(d: dict) -> str:
    """Extrait la localisation depuis un dict job API — lookup case-insensitive."""
    d_lower = {k.lower(): v for k, v in d.items()}
    for k in JOB_LOC_KEYS:
        v = d_lower.get(k.lower())
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


_TOTAL_KEYS      = ["total", "totalcount", "count", "nbhits", "totalresults",
                    "totalentries", "numberofresults"]
_TOTAL_NOFILT    = ["totalcountnocondition", "totalnofilter", "grandtotal",
                    "totalwithoutfilter", "unfilteredtotal"]
_LIMIT_KEYS      = ["to", "limit", "size", "pagesize", "per_page", "hitsperpage",
                    "take", "rows"]
_OFFSET_KEYS     = ["from", "offset", "skip", "start"]


def _total_count_from_body(body: dict) -> tuple[int | None, bool]:
    """
    Retourne (total, filtered) où :
      - total   : nombre d'offres à récupérer
      - filtered: True si le total "sans conditions" > TotalCount courant
                  → signal qu'un filtre invisible est appliqué côté page
    Priorité à la clé "no-condition" (total réel sans filtres).
    """
    b = {k.lower(): v for k, v in body.items()}

    # Total "sans filtres" (ex : TotalCountNoCondition chez RWE)
    total_nofilt = None
    for k in _TOTAL_NOFILT:
        v = b.get(k)
        if isinstance(v, int) and v > 0:
            total_nofilt = v
            break

    # Total "filtré" (valeur courante)
    total_filt = None
    for k in _TOTAL_KEYS:
        v = b.get(k)
        if isinstance(v, int) and v > 0:
            total_filt = v
            break

    if total_nofilt and total_filt and total_nofilt > total_filt:
        return total_nofilt, True   # filtre détecté, vrai total = nofilt
    if total_nofilt:
        return total_nofilt, False
    if total_filt:
        return total_filt, False
    return None, False


def _fetch_all_pages(url: str, method: str, req_headers: dict,
                     post_data: str | None, total: int,
                     strip_filters: bool = False) -> dict | list | None:
    """
    Re-fetche une API pour récupérer tous les résultats.
    strip_filters=True : envoie un body minimal (pagination seule) pour
      ignorer les filtres implicites de la page (ex: pays, catégorie).
    Stratégies dans l'ordre :
      0. POST minimal {From:0, To:cap} si strip_filters et POST détecté
      1. POST JSON   — modifie limit/offset dans le body existant
      2. GET/POST    — modifie les query params
    """
    cap = min(total + 10, 1000)
    h = {k: v for k, v in req_headers.items() if k.lower() != "content-length"}

    # ── Stratégie 0 : body minimal pour strip les filtres (POST JSON) ────────
    if strip_filters and (method == "POST" or post_data):
        # Tente de déduire les noms de clés From/To depuis le body existant
        offset_key, limit_key = "From", "To"
        if post_data:
            try:
                orig = json.loads(post_data)
                for k in orig:
                    if k.lower() in {ok.lower() for ok in _OFFSET_KEYS}:
                        offset_key = k
                    if k.lower() in {lk.lower() for lk in _LIMIT_KEYS}:
                        limit_key = k
            except Exception:
                pass
        try:
            minimal = {offset_key: 0, limit_key: cap}
            r = requests.post(url, json=minimal, headers=h, timeout=20)
            if r.status_code == 200:
                data = r.json()
                if _find_job_list_in_body(data):
                    return data
        except Exception:
            pass

    # ── Stratégie 1 : POST JSON — modifie le body existant ──────────────────
    if method == "POST" and post_data:
        try:
            payload = json.loads(post_data)
            changed = False
            for k in list(payload.keys()):
                if k.lower() in {lk.lower() for lk in _LIMIT_KEYS}:
                    payload[k] = cap
                    changed = True
                elif k.lower() in {ok.lower() for ok in _OFFSET_KEYS}:
                    payload[k] = 0
                    changed = True
            if not changed:
                payload["limit"] = cap
            r = requests.post(url, json=payload, headers=h, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass

    # ── Stratégie 2 : query params (GET ou POST sans JSON body) ─────────────
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        changed = False
        for k in list(params.keys()):
            if k.lower() in {lk.lower() for lk in _LIMIT_KEYS}:
                params[k] = [str(cap)]
                changed = True
            elif k.lower() in {ok.lower() for ok in _OFFSET_KEYS}:
                params[k] = ["0"]
                changed = True
        if not changed:
            params["limit"] = [str(cap)]
        new_url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
        fn = requests.post if method == "POST" else requests.get
        r = fn(new_url, headers=h, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass

    return None


def _find_job_list_in_body(body, max_depth: int = 3) -> list | None:
    """
    Cherche récursivement une liste de dicts ressemblant à des jobs.
    Lookup case-insensitive sur JOB_LIST_KEYS, puis exploration en profondeur.
    """
    if isinstance(body, list):
        if body and isinstance(body[0], dict):
            return body
        return None
    if not isinstance(body, dict) or max_depth == 0:
        return None
    # 1. Clés connues — lookup case-insensitive
    body_lower = {k.lower(): v for k, v in body.items()}
    for k in JOB_LIST_KEYS:
        v = body_lower.get(k.lower())
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
        # Clés des items dans les listes ≥2 dicts → aide à identifier les champs titre/url
        for k, v in body.items():
            if isinstance(v, list) and len(v) >= 2 and isinstance(v[0], dict):
                print(f"         {k}[0]_keys={list(v[0].keys())[:12]}")


def _parse_api_jobs(body, company_name: str, validate_mode: bool = False) -> list[dict]:
    """
    Auto-détecte la structure d'une réponse API et extrait les jobs.
    Tous les lookups sont case-insensitive. Heuristiques titre/url en fallback.
    validate_mode=True : désactive le filtre is_relevant_title (health-check, Agent 3).
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

        # Vue case-insensitive (calculée une fois par item)
        d_ci   = {k.lower(): v for k, v in d.items()}
        item_ci = {k.lower(): v for k, v in item.items()}

        # Titre — clés connues CI, puis heuristique
        title = (_ci_get_from(d_ci, JOB_TITLE_KEYS)
                 or _ci_get_from(item_ci, JOB_TITLE_KEYS)
                 or _heuristic_title(d))

        if not title or (not validate_mode and not is_relevant_title(title)):
            continue

        # URL — clés connues CI, puis heuristique
        url = (_ci_get_from(d_ci, JOB_URL_KEYS)
               or _ci_get_from(item_ci, JOB_URL_KEYS)
               or _heuristic_url(d))

        # ID — case-insensitive
        job_id = str(
            item_ci.get("objectid") or d_ci.get("id") or
            d_ci.get("jobid") or d_ci.get("referencenumber") or ""
        )

        # Fallback URL depuis slug + id
        if not url and job_id:
            slug = d_ci.get("slug") or d_ci.get("urlslug") or ""
            if not slug:
                slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")
            url = f"/job/{slug}/{job_id}" if slug else f"/job/{job_id}"

        location = _extract_location(d) or _extract_location(item)

        date = (d_ci.get("publicationdate") or d_ci.get("date") or
                item_ci.get("updated_at") or "")[:10]

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
                "date": date,
                "source": "API (auto-detected)",
                "score": 0,
            })

    return jobs


def _llm_discover_pattern(html: str, company_name: str) -> str | None:
    """
    Haiku analyse le HTML et retourne le job_pattern (substring href commun
    à tous les liens d'offre). Appel one-shot, max_tokens=150.
    Résultat persisté dans html_pattern_cache → 0 token aux runs suivants.
    """
    from agents.loop import run_agent

    SYSTEM = (
        "You analyze HTML from job listing pages. "
        "Find the URL substring shared by all job listing anchor tags (the job_pattern). "
        'Output JSON only: {"job_pattern": "/jobs/"} '
        'If no job links visible: {"job_pattern": null}. '
        "No prose."
    )
    prompt = f"Find job_pattern in this HTML from {company_name}:\n{html[:4000]}"
    try:
        result = run_agent(
            system=SYSTEM,
            user_message=prompt,
            tools=[],
            max_turns=1,
            max_tokens=150,
        )
        m = re.search(r'\{[^}]+\}', result)
        if m:
            data = json.loads(m.group())
            pattern = data.get("job_pattern")
            if pattern and isinstance(pattern, str):
                return pattern
    except Exception as e:
        print(f"     ↳ LLM pattern discovery failed: {str(e)[:80]}")
    return None


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

    company = site["name"]
    all_jobs = []
    seen_urls = set()

    # Filtres sur URLs à exclure des APIs (bruit)
    API_NOISE = ("_next", "piwik", "analytics", "gtm", "linkedin", "facebook",
                 "google", "doubleclick", "hotjar", "segment", "sentry")

    for page_url in site["pages"]:
        # (url, method, req_headers, post_data, response_body)
        intercepted_apis: list[tuple[str, str, dict, str | None, dict | list]] = []

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
                    req = response.request
                    intercepted_apis.append((
                        url,
                        req.method,
                        dict(req.headers),
                        req.post_data,
                        body,
                    ))
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
                try:
                    pw_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    pw_page.wait_for_timeout(400)
                except Exception:
                    # Page navigated during scroll — laisser se stabiliser
                    break
            _wait_stable(pw_page)

            # ── Étape 3.5a : recalcul base depuis URL réelle ──────────────────
            # pw_page.url peut différer de page_url après redirect SPA/consent
            actual_url = pw_page.url
            base = f"{urlparse(actual_url).scheme}://{urlparse(actual_url).netloc}"

            # ── Étape 3.5 : job_pattern effectif (cache → config) ─────────────
            # Cache check : 0 token. Surcharge job_pattern si LLM l'a déjà découvert.
            from agents.html_pattern_cache import get as _cache_get, put as _cache_put
            cached_pattern = _cache_get(company)
            effective_pattern = cached_pattern or site.get("job_pattern")
            effective_site = (
                {**site, "job_pattern": effective_pattern}
                if effective_pattern != site.get("job_pattern")
                else site
            )

            # ── Étape 4 : attente DOM jobs ────────────────────────────────────
            found_sel = _wait_for_jobs_dom(pw_page, site.get("wait_for"), effective_pattern)

            # ── Étape 5 : parse DOM ───────────────────────────────────────────
            try:
                page_html = pw_page.content()
            except Exception:
                _wait_stable(pw_page)
                page_html = pw_page.content()
            dom_jobs_raw = parse_jobs_from_html(page_html, effective_site)
            dom_jobs = dom_jobs_raw if validate_mode else [j for j in dom_jobs_raw if is_relevant_title(j["title"])]
            for j in dom_jobs:
                j["source"] = "Playwright"
            if not validate_mode and len(dom_jobs_raw) != len(dom_jobs):
                print(f"     ↳ DOM brut: {len(dom_jobs_raw)} lien(s) → {len(dom_jobs)} après filtre titre")

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
            for api_url, method, req_headers, post_data, body in intercepted_apis:
                # Pagination : si total déclaré > items reçus → re-fetch complet
                effective_body = body
                if isinstance(body, dict):
                    job_list = _find_job_list_in_body(body)
                    if job_list:
                        total, filtered = _total_count_from_body(body)
                        if total and (total > len(job_list) or filtered):
                            full = _fetch_all_pages(api_url, method, req_headers,
                                                    post_data, total,
                                                    strip_filters=filtered)
                            if full is not None:
                                n_full = len(_find_job_list_in_body(full) or [])
                                tag = "filtre supprimé" if filtered else "pagination"
                                print(f"     ↳ {tag} ({len(job_list)}/{total}) → re-fetch: {n_full} jobs")
                                effective_body = full
                candidate_jobs = _parse_api_jobs(effective_body, company, validate_mode=validate_mode)
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

            # ── Étape 7 : LLM pattern discovery ──────────────────────────────
            # Fires uniquement si DOM=0, API=0, et aucun pattern connu (cache ou config).
            # Si effective_pattern déjà connu → skip LLM, requests fallback direct.
            if intercepted_apis:
                print(f"     ↳ DOM=0, {len(intercepted_apis)} API(s) interceptée(s) — structures non reconnues :")
                for api_url, _m, _rh, _pd, body in intercepted_apis:
                    _log_unrecognized_api(api_url, body)
            elif effective_pattern:
                print(f"     ↳ DOM=0, pattern connu ({effective_pattern!r}) → requests fallback direct")
            else:
                print(f"     ↳ DOM=0, aucune API JSON interceptée → LLM pattern discovery")

            if not effective_pattern:
                discovered = _llm_discover_pattern(page_html, company)
            else:
                discovered = None
            if discovered:
                _cache_put(company, discovered)
                llm_site = {**site, "job_pattern": discovered}
                llm_jobs_raw = parse_jobs_from_html(page_html, llm_site)
                llm_jobs = llm_jobs_raw if validate_mode else [j for j in llm_jobs_raw if is_relevant_title(j["title"])]
                for j in llm_jobs:
                    j["source"] = "Playwright+LLM"
                if not validate_mode and len(llm_jobs_raw) != len(llm_jobs):
                    print(f"     ↳ LLM DOM brut: {len(llm_jobs_raw)} lien(s) → {len(llm_jobs)} après filtre titre")
                    dedup = j["url"].split("?")[0].rstrip("/")
                    if dedup not in seen_urls:
                        seen_urls.add(dedup)
                        all_jobs.append(j)
                if llm_jobs:
                    pw_page.remove_listener("response", on_response)
                    continue
                print(f"     ↳ LLM découvert pattern={discovered!r} mais 0 job extrait → requests fallback")
            elif not effective_pattern:
                print(f"     ↳ LLM n'a pas trouvé de pattern → requests fallback")

            # ── Étape 8 : dernier recours requests ───────────────────────────
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
    from job_scrapper import parse_jobs_from_html, is_relevant_title
    _h = headers or {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,*/*",
    }
    try:
        r = requests.get(url, headers=_h, timeout=15, verify=False)
        if r.status_code == 200:
            raw = parse_jobs_from_html(r.text, site)
            jobs = raw if validate_mode else [j for j in raw if is_relevant_title(j["title"])]
            for j in jobs:
                j["source"] = "requests"
            if not validate_mode and len(raw) != len(jobs):
                print(f"     ↳ requests brut: {len(raw)} lien(s) → {len(jobs)} après filtre titre")
            return jobs
        else:
            print(f"     ↳ requests HTTP {r.status_code}")
    except Exception as e:
        print(f"     ↳ requests erreur ({str(e)[:60]})")
    return []
