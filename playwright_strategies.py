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
from agents.log import get_logger

urllib3.disable_warnings()

logger = get_logger("playwright")

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
                     strip_filters: bool = False,
                     pw_page=None) -> dict | list | None:
    """
    Re-fetche une API pour récupérer tous les résultats.
    strip_filters=True : envoie un body minimal (pagination seule).
    Stratégies dans l'ordre :
      0. pw_page.evaluate(fetch) — browser context (cookies + proxy) si pw_page fourni
      1. POST JSON minimal (strip_filters) ou modifié
      2. GET/POST query params
    """
    cap = min(total + 10, 1000)
    h = {k: v for k, v in req_headers.items() if k.lower() != "content-length"}

    # ── Stratégie 0 : browser fetch (pw_page) — contourne proxy/cookies ─────
    if pw_page is not None:
        try:
            # Detect pagination keys + original page size from post_data
            offset_key, limit_key = "From", "To"
            orig_take: int | None = None
            if post_data:
                try:
                    orig = json.loads(post_data)
                    for k, v in orig.items():
                        if k.lower() in {ok.lower() for ok in _OFFSET_KEYS}:
                            offset_key = k
                        if k.lower() in {lk.lower() for lk in _LIMIT_KEYS}:
                            limit_key = k
                            if isinstance(v, int) and v > 0:
                                orig_take = v
                except Exception:
                    pass

            # Forward original headers (includes CSRF tokens, auth, etc.)
            _SKIP_HDRS = {"content-length", "host", "content-type", "transfer-encoding",
                          "connection", "accept-encoding"}
            fwd_headers = {k: v for k, v in req_headers.items()
                           if k.lower() not in _SKIP_HDRS}
            fwd_headers["Content-Type"] = "application/json"

            def _build_body(skip: int, take: int) -> str:
                if strip_filters:
                    return json.dumps({offset_key: skip, limit_key: take})
                if post_data:
                    try:
                        p = json.loads(post_data)
                        for k in list(p.keys()):
                            if k.lower() in {lk.lower() for lk in _LIMIT_KEYS}:
                                p[k] = take
                            elif k.lower() in {ok.lower() for ok in _OFFSET_KEYS}:
                                p[k] = skip
                        return json.dumps(p)
                    except Exception:
                        pass
                return "null"

            def _pw_fetch(skip: int, take: int) -> tuple[dict | list | None, int]:
                """Returns (data, http_status). data=None means error/non-200."""
                body_str = _build_body(skip, take)
                js = f"""
                async () => {{
                    const r = await fetch({json.dumps(url)}, {{
                        method: {json.dumps(method)},
                        headers: {json.dumps(fwd_headers)},
                        body: {body_str if method == "POST" else "undefined"}
                    }});
                    if (!r.ok) return {{"__status": r.status}};
                    return await r.json();
                }}
                """
                d = pw_page.evaluate(js)
                if isinstance(d, dict) and "__status" in d:
                    return None, d["__status"]
                return d, 200

            # Try decreasing page sizes until one works (API may limit max page size)
            sizes = list(dict.fromkeys([cap, min(cap, 100), min(cap, 50), min(cap, 25)]))
            if orig_take and orig_take not in sizes:
                sizes.append(orig_take)

            for try_size in sizes:
                if try_size <= 0:
                    continue
                logger.debug("  [refetch] strat0 take=%d  url=%s", try_size, url.split("?")[0][-60:])
                data, status = _pw_fetch(0, try_size)
                if data is None:
                    logger.debug("  [refetch] strat0 take=%d → HTTP %d", try_size, status)
                    continue

                job_list = _find_job_list_in_body(data)
                if not job_list:
                    logger.debug("  [refetch] strat0 take=%d → no job_list in response", try_size)
                    continue

                if len(job_list) >= total:
                    logger.debug("  [refetch] strat0 OK → all %d jobs in one shot", len(job_list))
                    return data

                # Got partial results → paginate
                logger.debug("  [refetch] strat0 take=%d → %d/%d, paginating...", try_size, len(job_list), total)
                all_items: list = list(job_list)
                skip_val = try_size
                while skip_val < total and len(all_items) < total:
                    page_data, page_status = _pw_fetch(skip_val, try_size)
                    if page_data is None:
                        logger.debug("  [refetch] strat0 pagination skip=%d → HTTP %d, stopping", skip_val, page_status)
                        break
                    page_jobs = _find_job_list_in_body(page_data)
                    if not page_jobs:
                        break
                    all_items.extend(page_jobs)
                    skip_val += try_size
                    logger.debug("  [refetch] strat0 pagination skip=%d → +%d (total=%d)", skip_val, len(page_jobs), len(all_items))

                if all_items:
                    logger.debug("  [refetch] strat0 pagination complete → %d items", len(all_items))
                    return all_items  # list — _find_job_list_in_body handles it
                break  # got 200 but pagination failed, don't try smaller sizes

        except Exception as e:
            logger.debug("  [refetch] strat0 exception: %s", str(e)[:120])

    # ── Stratégie 1 : body minimal pour strip les filtres (POST JSON) ────────
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
            logger.debug("  [refetch] strat1 requests.post  payload=%s", str(payload)[:120])
            r = requests.post(url, json=payload, headers=h, timeout=20)
            logger.debug("  [refetch] strat1 status=%d", r.status_code)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            logger.debug("  [refetch] strat1 exception: %s", str(e)[:100])

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
        logger.debug("  [refetch] strat2 %s %s", method, new_url[-80:])
        r = fn(new_url, headers=h, timeout=20)
        logger.debug("  [refetch] strat2 status=%d", r.status_code)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("  [refetch] strat2 exception: %s", str(e)[:100])

    logger.debug("  [refetch] all strategies failed → None")
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
        logger.debug("  [api-unrecognized] LIST[%d] %s  item_keys=%s", len(body), short, keys)
    elif isinstance(body, dict):
        top = list(body.keys())[:10]
        lists = {k: len(v) for k, v in body.items() if isinstance(v, list) and v}
        nested = {k: list(v.keys())[:6] for k, v in body.items() if isinstance(v, dict)}
        logger.debug("  [api-unrecognized] DICT %s  top_keys=%s  lists=%s", short, top, lists)
        if nested:
            logger.debug("  [api-unrecognized]   nested=%s", nested)
        for k, v in body.items():
            if isinstance(v, list) and len(v) >= 2 and isinstance(v[0], dict):
                logger.debug("  [api-unrecognized]   %s[0]_keys=%s", k, list(v[0].keys())[:12])


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

    logger.debug("  [parse_api] job_list=%d  company=%s  validate_mode=%s",
                 len(job_list), company_name, validate_mode)

    filtered_count = 0
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
            if filtered_count < 10:
                logger.debug("  [parse_api]   FILTERED title=%r  relevant=%s",
                             title, is_relevant_title(title) if title else "no_title")
            filtered_count += 1
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
        logger.warning("LLM pattern discovery failed for %s: %s", company_name, str(e)[:80])
    return None


def _apply_filter_click_seq(pw_page, company: str, seq: list[str]) -> bool:
    """
    Exécute une séquence de clics UI pour appliquer un filtre (ex: Company → S&T).
    Chaque élément de seq peut être une chaîne de sélecteurs séparés par des virgules
    (essayés dans l'ordre — le premier qui fonctionne suffit).
    Retourne True si tous les steps ont réussi.
    """
    for i, selector_group in enumerate(seq):
        candidates = [s.strip() for s in selector_group.split(",")]
        clicked = False
        for sel in candidates:
            try:
                pw_page.click(sel, timeout=2000)
                _wait_stable(pw_page)
                logger.debug("[%s] filter_click step %d: clicked %r", company, i + 1, sel)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            logger.debug("[%s] filter_click step %d: no selector matched → abort (%s)",
                         company, i + 1, selector_group[:60])
            # Dump DOM elements for diagnosis (find the real filter selector)
            try:
                els = pw_page.evaluate("""() => {
                    const nodes = document.querySelectorAll(
                        'button, [role="button"], [class*="filter"], [class*="Filter"], '
                        + '[class*="accordion"], details summary, h3, h4, legend, '
                        + '[data-filter], [data-type]'
                    );
                    return Array.from(nodes).slice(0, 40).map(e => ({
                        tag: e.tagName,
                        text: (e.innerText || '').trim().slice(0, 50),
                        cls: (e.className || '').slice(0, 60),
                        data: Object.fromEntries(
                            Array.from(e.attributes)
                                .filter(a => a.name.startsWith('data-') || a.name === 'role')
                                .map(a => [a.name, (a.value || '').slice(0, 40)])
                        )
                    }));
                }""")
                logger.debug("[%s] filter_click DOM dump (step %d): %s",
                             company, i + 1, json.dumps(els))
            except Exception as dump_err:
                logger.debug("[%s] filter_click DOM dump failed: %s", company, dump_err)
            return False
    return True


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
            logger.debug("[%s] navigate → %s", company, page_url)
            nav_strategy = _navigate(pw_page, page_url)
            logger.debug("[%s] nav_strategy=%s", company, nav_strategy)
            if nav_strategy == "error":
                logger.debug("[%s] nav error → requests fallback for %s", company, page_url)
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
            if consent_sel:
                logger.debug("[%s] cookie consent dismissed: %s", company, consent_sel)

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

            # ── Étape 3.6 : filtre UI (filter_click_seq) ─────────────────────
            # Permet de sélectionner un filtre Company/Division avant de parser.
            # Déclenche un nouvel appel API filtré intercepté dans intercepted_apis.
            # On skip les APIs pré-filtre pour ne garder que les résultats filtrés.
            filter_click_seq = site.get("filter_click_seq")
            filter_start_idx = 0
            if filter_click_seq:
                pre_filter_idx = len(intercepted_apis)
                ok = _apply_filter_click_seq(pw_page, company, filter_click_seq)
                if ok:
                    filter_start_idx = pre_filter_idx
                    logger.debug("[%s] filter applied → skipping %d pre-filter APIs, processing from idx %d",
                                 company, pre_filter_idx, filter_start_idx)
                else:
                    logger.debug("[%s] filter_click_seq failed → processing all APIs (no filter)", company)

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
            logger.debug("[%s] wait_for_jobs_dom → sel=%r  effective_pattern=%r",
                         company, found_sel, effective_pattern)

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
            logger.debug("[%s] DOM parse: raw=%d  after_filter=%d", company, len(dom_jobs_raw), len(dom_jobs))
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
            apis_to_process = intercepted_apis[filter_start_idx:]
            logger.debug("[%s] DOM=0 → analysing %d intercepted API(s)%s", company,
                         len(apis_to_process),
                         f" (skipped {filter_start_idx} pre-filter)" if filter_start_idx else "")
            api_jobs = []
            pagination_total = 0  # total déclaré par l'API quand re-fetch a échoué
            for api_url, method, req_headers, post_data, body in apis_to_process:
                logger.debug("[%s]   API %s %s  body_type=%s", company, method,
                             api_url.split("?")[0][-80:], type(body).__name__)
                # Pagination : si total déclaré > items reçus → re-fetch complet
                effective_body = body
                if isinstance(body, dict):
                    job_list = _find_job_list_in_body(body)
                    if job_list:
                        total, filtered = _total_count_from_body(body)
                        logger.debug("[%s]   job_list=%d  total=%s  filtered=%s",
                                     company, len(job_list), total, filtered)
                        if total and (total > len(job_list) or filtered):
                            logger.debug("[%s]   pagination/filter detected → re-fetch (cap=%d)", company, total)
                            full = _fetch_all_pages(api_url, method, req_headers,
                                                    post_data, total,
                                                    strip_filters=filtered,
                                                    pw_page=pw_page)
                            if full is not None:
                                n_full = len(_find_job_list_in_body(full) or [])
                                tag = "filtre supprimé" if filtered else "pagination"
                                logger.debug("[%s]   re-fetch result: %d jobs (%s)", company, n_full, tag)
                                print(f"     ↳ {tag} ({len(job_list)}/{total}) → re-fetch: {n_full} jobs")
                                effective_body = full
                            else:
                                # Re-fetch bloqué → noter le total pour UI pagination
                                pagination_total = max(pagination_total, total or 0)
                candidate_jobs = _parse_api_jobs(effective_body, company, validate_mode=validate_mode)
                logger.debug("[%s]   candidate_jobs from this API: %d", company, len(candidate_jobs))
                for j in candidate_jobs:
                    if j["url"].startswith("/"):
                        j["url"] = base + j["url"]
                    api_jobs.append(j)

            # ── Étape 6.5 : UI pagination — scroll + load-more si re-fetch bloqué ──
            # Quand l'API déclare plus de jobs que reçus ET le re-fetch est bloqué (403),
            # on laisse le browser faire les appels lui-même via scroll/clic load-more.
            if not api_jobs and pagination_total > 0:
                logger.debug("[%s] UI pagination fallback: total=%d, trying scroll+load-more",
                             company, pagination_total)
                _LOAD_MORE_SELS = [
                    "button:has-text('Load more')",   "button:has-text('Show more')",
                    "button:has-text('Mehr laden')",  "button:has-text('Meer laden')",
                    "button:has-text('Plus de résultats')",
                    ".load-more", ".btn-load-more", "[data-action='load-more']",
                    "[data-testid*='load-more']", "a:has-text('Load more')",
                ]
                max_ui_attempts = min(pagination_total // 9 + 2, 40)
                total_raw_loaded = 0
                for attempt in range(max_ui_attempts):
                    prev_intercept_count = len(intercepted_apis)
                    clicked = False
                    for sel in _LOAD_MORE_SELS:
                        try:
                            pw_page.click(sel, timeout=400)
                            _wait_stable(pw_page)
                            clicked = True
                            logger.debug("[%s] UI pagination attempt %d: clicked %r", company, attempt + 1, sel)
                            break
                        except Exception:
                            continue
                    if not clicked:
                        try:
                            pw_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            pw_page.wait_for_timeout(1200)
                        except Exception:
                            break

                    # Analyser les nouvelles APIs interceptées depuis le dernier passage
                    # Condition d'arrêt : raw_count==0 (plus rien chargé) — pas relevant==0
                    # (évite de stopper trop tôt quand les jobs ne sont pas dans le profil)
                    new_relevant = []
                    new_raw_count = 0
                    for _, _, _, _, body in intercepted_apis[prev_intercept_count:]:
                        job_list = _find_job_list_in_body(body) if isinstance(body, dict) else (body if isinstance(body, list) else [])
                        if job_list:
                            new_raw_count += len(job_list)
                        for j in _parse_api_jobs(body, company, validate_mode=validate_mode):
                            if j["url"].startswith("/"):
                                j["url"] = base + j["url"]
                            new_relevant.append(j)
                    if new_raw_count == 0:
                        logger.debug("[%s] UI pagination attempt %d: no new raw jobs → stop", company, attempt + 1)
                        break
                    total_raw_loaded += new_raw_count
                    api_jobs.extend(new_relevant)
                    logger.debug("[%s] UI pagination attempt %d: raw=%d relevant=%d (total_raw=%d/%d)",
                                 company, attempt + 1, new_raw_count, len(new_relevant),
                                 total_raw_loaded, pagination_total)
                    if total_raw_loaded >= pagination_total:
                        break
                if api_jobs:
                    logger.debug("[%s] UI pagination complete → %d jobs", company, len(api_jobs))

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
            if apis_to_process:
                logger.debug("[%s] DOM=0, %d API(s) — structures non reconnues", company, len(apis_to_process))
                print(f"     ↳ DOM=0, {len(apis_to_process)} API(s) interceptée(s) — structures non reconnues :")
                for api_url, _m, _rh, _pd, body in apis_to_process:
                    _log_unrecognized_api(api_url, body)
            elif effective_pattern:
                logger.debug("[%s] DOM=0, pattern connu (%r) → requests fallback direct", company, effective_pattern)
                print(f"     ↳ DOM=0, pattern connu ({effective_pattern!r}) → requests fallback direct")
            else:
                logger.debug("[%s] DOM=0, no API, no pattern → LLM pattern discovery", company)
                print(f"     ↳ DOM=0, aucune API JSON interceptée → LLM pattern discovery")

            if not effective_pattern:
                discovered = _llm_discover_pattern(page_html, company)
                logger.debug("[%s] LLM pattern discovery → %r", company, discovered)
            else:
                discovered = None
            if discovered:
                _cache_put(company, discovered)
                llm_site = {**site, "job_pattern": discovered}
                llm_jobs_raw = parse_jobs_from_html(page_html, llm_site)
                llm_jobs = llm_jobs_raw if validate_mode else [j for j in llm_jobs_raw if is_relevant_title(j["title"])]
                for j in llm_jobs:
                    j["source"] = "Playwright+LLM"
                logger.debug("[%s] LLM pattern=%r → raw=%d  filtered=%d",
                             company, discovered, len(llm_jobs_raw), len(llm_jobs))
                if not validate_mode and len(llm_jobs_raw) != len(llm_jobs):
                    print(f"     ↳ LLM DOM brut: {len(llm_jobs_raw)} lien(s) → {len(llm_jobs)} après filtre titre")
                    dedup = j["url"].split("?")[0].rstrip("/")
                    if dedup not in seen_urls:
                        seen_urls.add(dedup)
                        all_jobs.append(j)
                if llm_jobs:
                    pw_page.remove_listener("response", on_response)
                    continue
                logger.debug("[%s] LLM pattern=%r but 0 jobs extracted → requests fallback", company, discovered)
                print(f"     ↳ LLM découvert pattern={discovered!r} mais 0 job extrait → requests fallback")
            elif not effective_pattern:
                logger.debug("[%s] LLM found no pattern → requests fallback", company)
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
