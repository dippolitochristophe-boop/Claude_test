"""
Job Scraper v20

INSTALLATION (une seule fois) :
    pip install playwright requests beautifulsoup4
    $env:NODE_TLS_REJECT_UNAUTHORIZED=0   # PowerShell — proxy corporate
    playwright install chromium

ARCHITECTURE :
- 1 browser Playwright ouvert pour tout le run (performance + stabilité)
- Fallback requests automatique si Playwright échoue sur une page
- Toutes localisations collectées, bucketing en post-traitement
- APIs JSON (Workday, SmartRecruiters) sans browser
"""

import argparse
import os
import requests
import json
import time
from datetime import datetime
import urllib3
from bs4 import BeautifulSoup
from urllib.parse import urlparse

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SEEN_FILE = ".jobs_seen.json"

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("")
    print("=" * 65)
    print("  🚨 PLAYWRIGHT NON INSTALLE — RESULTATS INCOMPLETS 🚨")
    print("  Societes manquantes : TotalEnergies, Mercuria, EDF Trading,")
    print("  Orsted, BP, Hartree, Freepoint, MET Group...")
    print("  Estimation : ~50% des offres ne seront pas trouvees.")
    print("")
    print("  SOLUTION : pip install playwright")
    print("             playwright install chromium")
    print("=" * 65)
    print("")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-GB,en;q=0.9",
}

# ── Mots-clés recherche Workday/SmartRecruiters ───────────────────────────────

# Aligné sur DIRECT_MATCH : les termes cherchés via API couvrent exactement
# ce que is_relevant_title() accepte en hit direct ou via domaine × rôle.
SEARCH_QUERIES = [
    # ── Hit direct (DIRECT_MATCH) ─────────────────────────────────────────────
    "power trader", "energy trader", "power market", "intraday trading",
    "front office", "portfolio manager", "head of trading", "chief risk officer",
    "market risk", "asset optimizer", "trading analyst", "risk officer",
    "algo trading", "algorithmic trading", "ppa manager", "ppa sales",
    "originator",
    # ── Domaine power × rôle (DOMAIN_POWER × ROLE_KEYWORDS) ──────────────────
    "renewables trader", "bess trading", "power risk", "energy origination",
]

# ── Filtre titre ──────────────────────────────────────────────────────────────

DOMAIN_POWER = [
    "power", "electricity", "renewable", "bess", "wind", "solar", "hydro",
    "battery", "intraday", "algo", "algorithmic",
]
DOMAIN_ENERGY_GAS = ["energy", "gas", "ppa", "commodit", "origination"]
DOMAIN_EXCLUDE = [
    "fuel oil", "crude oil", "oil trader", "lng trader", "bunker",
    "nat gas scheduler", "lpg", "middle distillate", "shipping", "site manager", "coal",
]
ROLE_KEYWORDS = [
    "trader", "trading", "portfolio", "origination", "risk officer", "optimizer",
    "structuring", "analyst", "manager", "director", "head of", "coo", "cro",
    "chief", "vp", "front office", "algo",
]
DIRECT_MATCH = [
    "power trader", "energy trader", "power market", "intraday",
    "front office", "portfolio manager", "head of trading", "chief risk",
    "market risk", "originator", "asset optimizer", "trading analyst",
    "risk officer", "algo trading", "algorithmic trading", "ppa manager", "ppa sales",
]
TITLE_NOISE = [
    "cookie", "privacy", "accept", "login", "sign in", "home", "about",
    "contact", "newsletter", "read more", "learn more", "see all", "view all",
    "apply now", "back to", "why work", "life at", "benefits", "culture",
    "copyright", "terms", "legal", "gdpr", "show more", "load more",
]

NON_REGRESSION = [
    "Senior Power Trader", "Senior Power Trader (f/m/d)", "Risk Officer - Power",
    "Head Intraday Algorithmic Trading", "PPA Sales Origination Manager", "Senior Gas Originator",
]


def is_relevant_title(text: str) -> bool:
    t = text.strip()
    if len(t) < 6 or len(t) > 120:
        return False
    tl = t.lower()
    if any(n in tl for n in TITLE_NOISE):
        return False
    if any(ex in tl for ex in DOMAIN_EXCLUDE):
        return False
    if any(kw in tl for kw in DIRECT_MATCH):
        return True
    if any(kw in tl for kw in DOMAIN_POWER) and any(kw in tl for kw in ROLE_KEYWORDS):
        return True
    if any(kw in tl for kw in DOMAIN_ENERGY_GAS):
        strong = ["trader", "trading", "risk officer", "portfolio manager",
                  "originator", "head of trading", "front office", "optimizer"]
        return any(r in tl for r in strong)
    return False


# ── Bucketing ─────────────────────────────────────────────────────────────────

BUCKET_LONDON = ["london", "united kingdom", "uk ", " uk", "england"]
BUCKET_SWISS  = ["geneva", "genève", "geneve", "lausanne", "zurich", "zürich",
                 "switzerland", "swiss", "olten", "bern", "berne", "basel", "zug", "baar",
                 "baden", "aarau", "luzern", "lucerne", "winterthur", "lugano", "nyon"]
BUCKET_EUR    = ["paris", "amsterdam", "brussels", "rotterdam", "oslo", "stockholm",
                 "hamburg", "düsseldorf", "madrid", "milan", "frankfurt", "netherlands",
                 "france", "germany", "norway", "sweden", "spain", "italy", "belgium",
                 "luxembourg", "denmark", "finland", "austria", "prague", "warsaw", "vienna"]

BUCKET_ORDER = ["🇬🇧 London", "🇨🇭 Switzerland", "🇪🇺 Other Europe", "Unknown", "🌍 Rest of World"]

# Constante partagée par parse_jobs_from_html et get_location_bucket (perf : définie une seule fois)
LOC_CITIES = [
    "olten", "zurich", "zürich", "geneva", "genève", "london", "lausanne",
    "bern", "berne", "basel", "zug", "baar", "baden", "aarau", "luzern",
    "lucerne", "winterthur", "oslo", "paris", "amsterdam", "rotterdam",
    "brussels", "stockholm", "hamburg", "madrid", "milan", "frankfurt",
    "düsseldorf", "houston", "singapore", "dubai", "new york", "tokyo",
    "copenhagen", "vienna", "warsaw", "prague", "luxembourg", "edinburgh",
    "manchester", "birmingham", "sydney", "cape town",
    "switzerland", "norway", "germany", "netherlands", "france", "sweden",
    "denmark", "austria", "belgium", "finland", "england", "united kingdom", "uk",
]


def get_location_bucket(location: str) -> str:
    loc = location.lower()
    if not loc or loc in ["", "n/a", "various", "multiple", "remote", "see link"]:
        return "Unknown"
    if any(kw in loc for kw in BUCKET_LONDON): return "🇬🇧 London"
    if any(kw in loc for kw in BUCKET_SWISS):  return "🇨🇭 Switzerland"
    if any(kw in loc for kw in BUCKET_EUR):    return "🇪🇺 Other Europe"
    return "🌍 Rest of World"


# ── Config sociétés HTML ──────────────────────────────────────────────────────
# type: "html" → Playwright first, requests fallback
# job_pattern: substring obligatoire dans href pour qu'un lien soit un job

SITES = [
    # ── Suisse ────────────────────────────────────────────────────────────────
    {
        "name": "Alpiq",
        "type": "html",
        "pages": [
            "https://www.alpiq.com/career/open-jobs",
            "https://www.alpiq.com/career/open-jobs/jobs/job-page-2/f1-%2A/f2-%2A/search",
            "https://www.alpiq.com/career/open-jobs/jobs/job-page-3/f1-%2A/f2-%2A/search",
            "https://www.alpiq.com/career/open-jobs/jobs/job-page-4/f1-%2A/f2-%2A/search",
            "https://www.alpiq.com/career/open-jobs/jobs/job-page-5/f1-%2A/f2-%2A/search",
        ],
        "job_pattern": "/your-application/",
    },
    {
        "name": "Axpo",
        "type": "html",
        "pages": ["https://careers.axpo.com/jobs"],
        "job_pattern": "/jobs/",
    },
    {
        "name": "BKW",
        "type": "html",
        "pages": ["https://karriere.bkw.ch/en"],
        "job_pattern": "/en/job",   # /en/job/ ou /en/jobdetail/ — plus spécifique que /en/
    },
    # ── Londres ───────────────────────────────────────────────────────────────
    # EDF Trading → Workday (voir WORKDAY_COMPANIES)
    {
        "name": "Hartree Partners",
        "type": "html",
        # hartreepartners.com → 403 Cloudflare systématique (requests + WebFetch).
        # ATS non identifié : Greenhouse/Lever/Workday/Ashby/SmartRecruiters → aucun résultat public.
        # ACTION REQUISE : ouvrir dans Chrome DevTools → Network → identifier l'ATS embarqué.
        "pages": ["https://www.hartreepartners.com/about/careers/"],
        "job_pattern": "/careers/",
    },
    # Statkraft → déplacé dans SMARTRECRUITERS_COMPANIES (sr_id: statkraft1)
    # BP → Workday (voir WORKDAY_COMPANIES — doublon supprimé ici)
    # TotalEnergies → Taleo (voir TALEO_SITES)
    # ── Genève / Londres ──────────────────────────────────────────────────────
    {
        "name": "Mercuria",
        "type": "html",
        # www.mercuria.com → 403 bots. mercuria.com (sans www) aussi bloqué via requests/WebFetch.
        # Pattern /job/ correct (confirmé : mercuria.com/job/vacancy-2025-xxx/).
        # Playwright (real browser) est la seule chance de bypass.
        "pages": ["https://mercuria.com/careers/"],
        "job_pattern": "/job/",
    },
    {
        "name": "Freepoint",
        "type": "html",
        "pages": ["https://www.freepointcommodities.com/careers/"],
        "job_pattern": "/careers/",
    },
    {
        "name": "MET Group",
        "type": "html",
        "pages": ["https://met-group.jobs/en/jobs"],
        "job_pattern": "/en/jobs/",
    },
    # ── Anciens SmartRecruiters — portails HTML propres ───────────────────────
    {
        "name": "RWE",
        "type": "html",
        "pages": ["https://www.rwe.com/en/rwe-careers-portal/job-offers/"],
        "job_pattern": "/job-offers/details/",
        "wait_for": "a[href*='/job-offers/details/']",  # SPA : attendre injection DOM
    },
    # Uniper → déplacé vers scrape_uniper() (Next.js custom API /api/filter/query)
    {
        "name": "ENGIE",
        "type": "html",
        # Phenom People platform — homepage ne liste pas les jobs, utiliser search URLs
        # Pattern confirmé : jobs.engie.com/job/{title}/{id}-en_US/
        "pages": [
            "https://jobs.engie.com/search/?q=power+trader",
            "https://jobs.engie.com/search/?q=energy+trader",
            "https://jobs.engie.com/search/?q=trading+risk",
            "https://jobs.engie.com/search/?q=portfolio+manager",
            "https://jobs.engie.com/search/?q=origination",
        ],
        "job_pattern": "/job/",
        "wait_for": "a[href*='/job/']",  # Phenom People SPA : attendre injection DOM
    },
    # ── Commodity traders / bourses d'énergie ─────────────────────────────────
    # Glencore → déplacé dans GREENHOUSE_COMPANIES (Greenhouse EU : glencoreuk + tlgglencorebaar)
    {
        "name": "EEX Group",
        "type": "html",
        "pages": ["https://career.deutsche-boerse.com/eex"],
        "job_pattern": "/eex/job/",  # career.deutsche-boerse.com/eex/job/{loc}/{title}/{id}
        "wait_for": "a[href*='/eex/job/']",  # SPA Deutsche Börse : attendre injection DOM
    },
    {
        "name": "Cargill Trading",
        "type": "html",
        "pages": ["https://careers.cargill.com/en/category/trading-jobs/23251/8144240/1"],
        "job_pattern": "/en/job/",  # /en/job/{location}/{title}/23251/{id}
    },
    {
        "name": "Danske Commodities",
        "type": "html",
        "pages": ["https://careers.danskecommodities.com/Vacancies"],
        "job_pattern": "/Application/",  # /Application/{id} sur le même domaine careers.*
    },
    # ── Portails HTML custom ──────────────────────────────────────────────────
    {
        "name": "Orsted",
        "type": "html",
        # Portail propre — PAS Workday. Pattern confirmé : /en/careers/vacancies-list/{year}/{month}/{id}-{title}
        "pages": ["https://orsted.com/en/careers/vacancies-list"],
        "job_pattern": "/vacancies-list/",
    },
    # ── Pure traders ──────────────────────────────────────────────────────────
    {
        "name": "InCommodities",
        "type": "html",
        "pages": ["https://incommodities.com/join-us"],
        "job_pattern": "*",  # root-level slugs (/algo-trader, /quantitative-developer-…) — filtrage par titre
    },
    {
        "name": "Petroineos Trading",
        "type": "html",
        "pages": ["https://careers.petroineos.com/"],
        "job_pattern": "/postings/",  # Ashby ATS — /postings/{uuid} et /en/postings/{uuid}
    },
]

# ── APIs JSON (Workday + SmartRecruiters) ─────────────────────────────────────

WORKDAY_COMPANIES = [
    {"name": "Trafigura", "tenant": "trafigura",    "site": "TrafiguraCareerSite", "wd": "wd3"},  # ✅ confirmé
    {"name": "Gunvor",    "tenant": "gunvor",        "site": "Gunvor_Careers",      "wd": "wd3"},  # ✅ confirmé
    {"name": "Shell",     "tenant": "shell",         "site": "ShellCareers",        "wd": "wd3"},  # ✅ confirmé
    {"name": "BP",        "tenant": "bpinternational","site": "bpCareers",           "wd": "wd3"},  # ✅ confirmé (corrigé v15)
    {"name": "Equinor",   "tenant": "equinor",       "site": "EQNR",                "wd": "wd3"},  # ✅ confirmé
    # Orsted → déplacé dans SITES (portail HTML custom orsted.com/en/careers/vacancies-list)
    # Glencore → déplacé dans GREENHOUSE_COMPANIES (portail Greenhouse EU, pas Workday)
    {"name": "EDF Trading","tenant": "edftrading",   "site": "EDFTrading",          "wd": "wd1"},  # ✅ confirmé (corrigé v15)
    {"name": "Centrica",  "tenant": "centrica",      "site": "Centrica",            "wd": "wd3"},  # ✅ confirmé
    {"name": "Castleton Commodities (CCI)", "tenant": "osv-cci", "site": "CCICareers", "wd": "wd1"},  # ✅ confirmé
]

SMARTRECRUITERS_COMPANIES = [
    {"name": "Vattenfall", "sr_id": "Vattenfall"},  # confirmé : careers.smartrecruiters.com/vattenfall
    {"name": "Vitol",      "sr_id": "Vitol"},       # confirmé : jobs.smartrecruiters.com/Vitol/...
    {"name": "Statkraft",  "sr_id": "statkraft1"},  # ✅ confirmé : careers.smartrecruiters.com/statkraft1
    # RWE   → déplacé dans SITES (portail HTML propre, pas SmartRecruiters)
    # Uniper → déplacé dans SITES (portail iCIMS careers.uniper.energy)
    # ENGIE  → dans SITES (portail Phenom People jobs.engie.com)
]

GREENHOUSE_COMPANIES = [
    # API EU : boards-api.eu.greenhouse.io/v1/boards/{board_token}/jobs
    # Retourne tous les jobs → filtrage local par is_relevant_title()
    {"name": "Glencore (London Trading)", "board_token": "glencoreuk",     "region": "eu"},  # ✅ confirmé job-boards.eu.greenhouse.io/glencoreuk
    {"name": "Glencore (Baar HQ)",        "board_token": "tlgglencorebaar", "region": "eu"},  # ✅ confirmé job-boards.eu.greenhouse.io/tlgglencorebaar
]


# ── Parser HTML commun ────────────────────────────────────────────────────────

def parse_jobs_from_html(html: str, site: dict) -> list[dict]:
    """Parse le HTML (qu'il vienne de Playwright ou requests) — logique identique."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()

    # Base URL pour résoudre les liens relatifs
    base_url = site["pages"][0]
    base = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # job_pattern vide ou "*" = accepte tous les liens (pour sites comme EDF Trading)
        if site.get("job_pattern") and site["job_pattern"] != "*":
            if site["job_pattern"] not in href:
                continue

        # Titre : lien lui-même, sinon heading dans le parent
        text = a.get_text(strip=True)
        if not is_relevant_title(text):
            parent = a.find_parent(["li", "article", "div", "section"])
            if parent:
                heading = parent.find(["h2", "h3", "h4"])
                text = heading.get_text(strip=True) if heading else parent.get_text(separator=" ", strip=True)[:100]

        if not is_relevant_title(text):
            continue

        # Localisation : cherche dans le parent, préfère la chaîne la plus courte
        location = ""
        parent = a.find_parent(["li", "article", "div", "section"])
        if parent:
            candidates = []
            for s in parent.find_all(string=True):
                s = s.strip()
                if 2 < len(s) <= 80 and any(loc in s.lower() for loc in LOC_CITIES):
                    candidates.append(s)
            if candidates:
                location = min(candidates, key=len)  # la plus courte = la plus précise

        if href.startswith("/"):
            href = base + href
        elif not href.startswith("http"):
            continue

        title = text[:100]
        # Déduplication par URL (évite les doublons quand le même card est parsé 2x)
        dedup_key = href.split("?")[0].rstrip("/")
        if dedup_key not in seen:
            seen.add(dedup_key)
            jobs.append({
                "title": title,
                "company": site["name"],
                "location": location,
                "bucket": get_location_bucket(location),
                "description": "",
                "url": href,
                "date": "",
                "source": "html",  # surchargé par l'appelant ("Playwright" ou "requests")
                "score": 0,
            })
    return jobs


# ── Scraper universel : Playwright → requests fallback ────────────────────────

def scrape_site(site: dict, pw_page=None) -> list[dict]:
    """
    Scrape un site HTML.
    pw_page : page Playwright déjà ouverte (browser unique pour tout le run).
    Si pw_page=None → fallback requests directement.
    """
    all_jobs = []
    seen = set()

    for url in site["pages"]:
        jobs = []

        # ── Playwright (page passée depuis le browser global) ──────────────
        if pw_page is not None:
            try:
                try:
                    pw_page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception:
                    # networkidle peut ne jamais se déclencher sur les SPAs
                    pw_page.goto(url, wait_until="load", timeout=30000)
                # Scroll progressif pour déclencher le lazy-loading
                for _ in range(3):
                    pw_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    pw_page.wait_for_timeout(700)
                # SPAs (iCIMS, Phenom, etc.) : attendre que les liens jobs apparaissent dans le DOM
                wait_for = site.get("wait_for")
                if wait_for:
                    try:
                        pw_page.wait_for_selector(wait_for, timeout=10000)
                    except Exception:
                        pass  # timeout = 0 offres ou ATS non responsive, on parse quand même
                else:
                    pw_page.wait_for_timeout(1000)
                jobs = parse_jobs_from_html(pw_page.content(), site)
                for j in jobs:
                    j["source"] = "Playwright"
                if not jobs:
                    print(f"     ↳ Playwright OK — 0 lien '{site['job_pattern']}' trouvé → rendu SPA? job_pattern à revoir?")
            except Exception as e:
                print(f"     ⚠️  Playwright fail → requests ({str(e)[:70]})")
                jobs = _get_jobs_requests(url, site)

        # ── Fallback requests ──────────────────────────────────────────────
        else:
            jobs = _get_jobs_requests(url, site)

        for j in jobs:
            dedup = j["url"].split("?")[0].rstrip("/")
            if dedup not in seen:
                seen.add(dedup)
                all_jobs.append(j)

        time.sleep(0.8)

    return all_jobs


def _get_jobs_requests(url: str, site: dict) -> list[dict]:
    """Fallback requests+BS4 pour une URL."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        if r.status_code == 200:
            jobs = parse_jobs_from_html(r.text, site)
            for j in jobs:
                j["source"] = "requests"
            if not jobs:
                print(f"     ↳ requests OK (HTTP 200) — 0 lien '{site['job_pattern']}' trouvé → vérifier job_pattern ou rendu JS")
            return jobs
        else:
            print(f"     ↳ requests HTTP {r.status_code} — config ATS à vérifier")
    except requests.exceptions.ConnectionError:
        print(f"     ↳ requests DNS/connexion fail — URL invalide ou proxy bloquant")
    except requests.exceptions.Timeout:
        print(f"     ↳ requests timeout (>15s) — site lent ou accès bloqué")
    except Exception as e:
        print(f"     ↳ requests erreur ({str(e)[:70]})")
    return []


# ── Workday API ───────────────────────────────────────────────────────────────

def scrape_workday(company: dict) -> list[dict]:
    jobs = []
    seen = set()
    any_200 = False  # au moins une requête réussie → endpoint valide
    base_url = f"https://{company['tenant']}.{company['wd']}.myworkdayjobs.com"
    api_url = f"{base_url}/wday/cxs/{company['tenant']}/{company['site']}/jobs"
    for query in SEARCH_QUERIES:
        try:
            r = requests.post(api_url,
                json={"limit": 20, "offset": 0, "searchText": query},
                headers={**HEADERS, "Content-Type": "application/json"},
                timeout=12, verify=False)
            if r.status_code == 200:
                any_200 = True
                for job in r.json().get("jobPostings", []):
                    title = job.get("title", "").strip()
                    location = job.get("locationsText", "")
                    ext_path = job.get("externalPath", "")
                    dedup_key = ext_path or title  # URL en priorité, titre en fallback
                    if title and dedup_key not in seen and is_relevant_title(title):
                        seen.add(dedup_key)
                        jobs.append({
                            "title": title,
                            "company": company["name"],
                            "location": location,
                            "bucket": get_location_bucket(location),
                            "description": "",
                            "url": base_url + ext_path,
                            "date": job.get("postedOn", ""),
                            "source": "Workday",
                            "score": 0,
                        })
            elif r.status_code in (404, 403):
                # Endpoint invalide (tenant/site incorrect) — inutile de continuer
                print(f"   ↳ HTTP {r.status_code} — tenant/site '{company['site']}' invalide")
                break
        except Exception as e:
            print(f"   ↳ erreur réseau [{query[:20]}] : {str(e)[:50]}")
            continue  # erreur transitoire — on tente les queries suivantes
        time.sleep(0.3)
    return jobs


def scrape_workday_broad(company: dict) -> list[dict]:
    """
    Requête Workday sans searchText — 1 seul appel, limit=100, filtre local.
    Certains tenants bloquent (réponse vide ou 403).
    Usage : test/diagnostic uniquement — appelez manuellement si besoin.
    """
    url = (f"https://{company['tenant']}.{company['wd']}.myworkdayjobs.com"
           f"/wday/cxs/{company['tenant']}/{company['site']}/jobs")
    jobs = []
    try:
        r = requests.post(url,
            json={"limit": 100, "offset": 0},
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=12, verify=False)
        if r.status_code == 200:
            for job in r.json().get("jobPostings", []):
                title = job.get("title", "").strip()
                location = job.get("locationsText", "")
                if title and is_relevant_title(title):
                    jobs.append({
                        "title": title,
                        "company": company["name"],
                        "location": location,
                        "bucket": get_location_bucket(location),
                        "description": "",
                        "url": f"https://{company['tenant']}.{company['wd']}.myworkdayjobs.com"
                               + job.get("externalPath", ""),
                        "date": job.get("postedOn", ""),
                        "source": "Workday",
                        "score": 0,
                    })
        else:
            print(f"     ↳ broad HTTP {r.status_code} — tenant bloque les requêtes sans searchText")
    except Exception as e:
        print(f"     ↳ broad erreur : {str(e)[:60]}")
    return jobs


# ── SmartRecruiters API ───────────────────────────────────────────────────────

def scrape_smartrecruiters(company: dict) -> list[dict]:
    jobs = []
    seen = set()
    endpoint_ok = None  # None=inconnu, True=OK, False=invalide
    for q in SEARCH_QUERIES:
        try:
            r = requests.get(
                f"https://api.smartrecruiters.com/v1/companies/{company['sr_id']}/postings",
                params={"q": q, "limit": 20},
                headers=HEADERS, timeout=12, verify=False)
            if r.status_code == 200:
                endpoint_ok = True
                for job in r.json().get("content", []):
                    title = job.get("name", "").strip()
                    loc = job.get("location", {})
                    city = loc.get("city", "")
                    country = loc.get("country", "")
                    location = f"{city} {country}".strip() if (city or country) else ""
                    job_id = job.get("id", "")
                    if title and title not in seen and is_relevant_title(title):
                        seen.add(title)
                        jobs.append({
                            "title": title,
                            "company": company["name"],
                            "location": location,
                            "bucket": get_location_bucket(location),
                            "description": "",
                            "url": f"https://jobs.smartrecruiters.com/{company['sr_id']}/{job_id}",
                            "date": job.get("releasedDate", ""),
                            "source": "SmartRecruiters",
                            "score": 0,
                        })
            elif r.status_code == 404 and endpoint_ok is None:
                print(f"   ↳ HTTP 404 — company id '{company['sr_id']}' invalide sur SmartRecruiters")
                endpoint_ok = False
                break
        except Exception as e:
            print(f"   ↳ erreur réseau [{q[:20]}] : {str(e)[:50]}")
            continue  # erreur transitoire — on tente les queries suivantes
        time.sleep(0.3)
    return jobs


# ── Greenhouse API ───────────────────────────────────────────────────────────

def scrape_greenhouse(company: dict) -> list[dict]:
    """Scraper Greenhouse — GET /v1/boards/{board_token}/jobs (retourne tout, filtre local)."""
    jobs = []
    seen = set()
    board_token = company["board_token"]
    region = company.get("region", "us")
    if region == "eu":
        api_url = f"https://boards-api.eu.greenhouse.io/v1/boards/{board_token}/jobs"
    else:
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    try:
        r = requests.get(api_url, headers=HEADERS, timeout=12, verify=False)
        if r.status_code == 200:
            for job in r.json().get("jobs", []):
                title = job.get("title", "").strip()
                location = job.get("location", {}).get("name", "")
                url = job.get("absolute_url", "")
                job_id = str(job.get("id", ""))
                if title and job_id not in seen and is_relevant_title(title):
                    seen.add(job_id)
                    jobs.append({
                        "title": title,
                        "company": company["name"],
                        "location": location,
                        "bucket": get_location_bucket(location),
                        "description": "",
                        "url": url,
                        "date": job.get("updated_at", "")[:10] if job.get("updated_at") else "",
                        "source": "Greenhouse",
                        "score": 0,
                    })
        elif r.status_code == 404:
            print(f"   ↳ HTTP 404 — board_token '{board_token}' invalide sur Greenhouse")
    except Exception as e:
        print(f"   ↳ erreur réseau : {str(e)[:50]}")
    return jobs


# ── Uniper (Next.js custom API) ───────────────────────────────────────────────

def scrape_uniper() -> list[dict]:
    """Scraper Uniper — POST /api/filter/query, pagination par page."""
    jobs = []
    seen = set()
    base = "https://careers.uniper.energy"
    api_url = f"{base}/api/filter/query"
    page = 0
    while True:
        payload = {"searchQuery": "", "filter": {}, "subclient": "uniper", "locale": "en", "page": page}
        try:
            r = requests.post(api_url, json=payload, headers={**HEADERS, "Content-Type": "application/json"},
                              timeout=15, verify=False)
            if r.status_code != 200:
                print(f"   ↳ HTTP {r.status_code} page {page}")
                break
            body = r.json()
        except Exception as e:
            print(f"   ↳ erreur réseau : {str(e)[:60]}")
            break

        for job in body.get("jobs", []):
            d = job.get("data", {})
            title = (d.get("title") or d.get("jobTitle") or d.get("name") or "").strip()
            # objectID est à la racine (Algolia standard), pas dans data
            job_id = str(job.get("objectID") or d.get("id") or d.get("jobId") or "")
            # URL : champ direct, ou reconstruction slug+id
            url = d.get("url") or d.get("link") or d.get("absoluteUrl") or ""
            if not url and job_id:
                slug = d.get("slug") or d.get("urlSlug") or d.get("externalUrl") or ""
                if not slug and title:
                    import re as _re
                    slug = _re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")
                url = f"{base}/en/job/{slug}/{job_id}" if slug else f"{base}/en/job/{job_id}"
            location = (d.get("location") or d.get("city") or d.get("locationName") or "").strip()
            if isinstance(location, dict):
                location = location.get("name") or location.get("city") or ""
            job_id_key = job_id or url
            if title and job_id_key not in seen and is_relevant_title(title):
                seen.add(job_id_key)
                jobs.append({
                    "title": title,
                    "company": "Uniper",
                    "location": location,
                    "bucket": get_location_bucket(location),
                    "description": "",
                    "url": url,
                    "date": (d.get("publicationDate") or d.get("date") or "")[:10],
                    "source": "Uniper API",
                    "score": 0,
                })

        next_page = body.get("nextPage")
        if next_page is None or next_page == page:
            break
        page = next_page

    return jobs


# ── Oracle Taleo (TotalEnergies, Macquarie Group) ────────────────────────────
# Même structure ATS Oracle Taleo : SearchJobs/{query} + JobDetail/{id}

TALEO_SITES = [
    {"name": "TotalEnergies",  "base": "https://jobs.totalenergies.com"},
    {"name": "Macquarie Group","base": "https://recruitment.macquarie.com"},
]

TALEO_QUERIES = [
    "power trader", "energy trader", "gas trader",
    "market risk power", "origination", "head of trading",
    "portfolio manager", "intraday", "PPA",
]

def _taleo_extract_location(a_tag) -> str:
    """Extrait la localisation d'un item Taleo — multiple stratégies."""
    parent = a_tag.find_parent(["li", "tr", "div", "section"])
    if not parent:
        return ""

    # Stratégie 1 : classe Taleo standard (listSrchResultLocation, jobLocation, etc.)
    for cls_kw in ["location", "Location", "loc"]:
        el = parent.find(class_=lambda c: c and cls_kw in c)
        if el:
            txt = el.get_text(strip=True)
            if txt and len(txt) < 100:
                return txt

    # Stratégie 2 : <td> / <span> sibling contenant une virgule (format "City, Country")
    for tag in parent.find_all(["td", "span", "p", "div"], recursive=False):
        txt = tag.get_text(strip=True)
        if "," in txt and 3 < len(txt) < 80 and tag != a_tag.parent:
            return txt

    # Stratégie 3 : texte court avec virgule dans tous les descendants
    for s in parent.find_all(string=True):
        s = s.strip()
        if "," in s and 3 < len(s) < 60 and s != a_tag.get_text(strip=True):
            return s

    return ""


def scrape_taleo(company: dict) -> list[dict]:
    """Scraper Oracle Taleo générique (TotalEnergies, Macquarie, etc.)."""
    base = company["base"]
    jobs = []
    seen = set()
    for q in TALEO_QUERIES:
        url = f"{base}/en_US/careers/SearchJobs/{q.replace(' ', '%20')}?listFilterMode=1&jobRecordsPerPage=20"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/careers/JobDetail/" not in href:
                    continue
                title = a.get_text(strip=True)
                if not title or not is_relevant_title(title):
                    continue
                if href.startswith("/"):
                    href = base + href
                dedup = href.split("?")[0].rstrip("/")
                if dedup not in seen:
                    seen.add(dedup)
                    location = _taleo_extract_location(a)
                    jobs.append({
                        "title": title,
                        "company": company["name"],
                        "location": location,
                        "bucket": get_location_bucket(location),
                        "description": "",
                        "url": href,
                        "date": "",
                        "source": "Taleo",
                        "score": 0,
                    })
        except Exception:
            pass
        time.sleep(0.3)
    return jobs


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_job(job: dict) -> int:
    text = (job["title"] + " " + job.get("description", "")).lower()
    tier1 = ["power trader", "energy trader", "front office", "head of trading",
             "chief risk", "coo", "cro", "portfolio manager", "origination",
             "market risk", "risk officer", "intraday", "algo trading", "ppa"]
    tier2 = ["trader", "trading", "risk", "power", "renewable", "bess",
             "hydro", "commodit", "structuring", "optimizer", "energy", "gas"]
    tier3 = ["senior", "head", "director", "vp", "managing director", "lead", "svp"]
    score = 0
    for kw in tier1:
        if kw in text: score += 4
    for kw in tier2:
        if kw in text: score += 2
    for kw in tier3:
        if kw in text: score += 1
    return score


# ── CLI args ──────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Job Scraper v20 — Power/Energy trading positions")
    p.add_argument("--new-only", action="store_true",
                   help="Affiche uniquement les offres nouvelles depuis le dernier run")
    p.add_argument("--no-html", action="store_true",
                   help="Désactive la génération du rapport HTML")
    p.add_argument("--company", nargs="+", metavar="NOM",
                   help="Scrape uniquement ces sociétés (ex: --company Shell BP Axpo)")
    p.add_argument("--bucket", nargs="+", metavar="ZONE",
                   help="Filtre les résultats par zone géo (ex: --bucket London Switzerland)")
    return p.parse_args()



def filter_companies(lst: list, names) -> list:
    """Retourne lst filtré sur les noms demandés (insensible à la casse). None → tout."""
    if not names:
        return lst
    names_lower = [n.lower() for n in names]
    return [co for co in lst if co["name"].lower() in names_lower]


# ── Delta mode ────────────────────────────────────────────────────────────────

def load_seen() -> tuple[set, str]:
    """Charge les URLs déjà vues et la date du dernier run."""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("urls", [])), data.get("last_run", "")
    return set(), ""


def save_seen(jobs: list):
    """Sauvegarde toutes les URLs du run courant comme 'déjà vues'."""
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "last_run": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "urls": [j["url"] for j in jobs],
        }, f, ensure_ascii=False, indent=2)


# ── HTML report ───────────────────────────────────────────────────────────────

def generate_html_report(jobs: list, new_urls: set = None) -> str:
    """Génère un rapport HTML autonome avec liens cliquables. Retourne le nom du fichier."""
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"rapport_{ts}.html"

    new_count_str = f" · <b>{len(new_urls)} nouvelles</b>" if new_urls is not None else ""

    buckets_html = ""
    for bucket in BUCKET_ORDER:
        bucket_jobs = sorted(
            [j for j in jobs if j["bucket"] == bucket],
            key=lambda x: x["score"], reverse=True,
        )
        if not bucket_jobs:
            continue
        jobs_html = ""
        for job in bucket_jobs:
            is_new = new_urls is not None and job["url"] in new_urls
            new_badge = '<span class="new-badge">NEW</span>' if is_new else ""
            filled = min(job["score"] // 2, 5)
            stars = "★" * filled + "☆" * (5 - filled)
            date_html = f'📅 {job["date"]} &nbsp;·&nbsp; ' if job.get("date") else ""
            title_safe = job["title"].replace("&", "&amp;").replace("<", "&lt;")
            company_safe = job["company"].replace("&", "&amp;")
            location_safe = (job["location"] or "?").replace("&", "&amp;")
            jobs_html += f"""
        <div class="job">
          <div class="job-title">
            <a href="{job['url']}" target="_blank" rel="noopener">{title_safe}</a>{new_badge}
          </div>
          <div class="job-meta">
            🏢 {company_safe} &nbsp;·&nbsp; 📍 {location_safe} &nbsp;·&nbsp;
            {date_html}<span class="score">{stars}</span> {job['score']}pts &nbsp;·&nbsp;
            <span class="ats">{job['source']}</span>
          </div>
        </div>"""
        buckets_html += f"""
    <div class="bucket">
      <h2>{bucket} — {len(bucket_jobs)} offre(s)</h2>
      {jobs_html}
    </div>"""

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Job Scraper — {date_str}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #f0f2f5; padding: 24px; color: #1a1a2e; }}
    .header {{ background: #1a1a2e; color: white; padding: 20px 24px; border-radius: 10px; margin-bottom: 20px; }}
    .header h1 {{ font-size: 1.3em; font-weight: 700; }}
    .header p {{ color: #a0aec0; font-size: 0.9em; margin-top: 6px; }}
    .bucket {{ background: white; border-radius: 10px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow: hidden; }}
    .bucket h2 {{ padding: 14px 20px; font-size: 1em; background: #fafafa; border-bottom: 1px solid #eee; color: #374151; }}
    .job {{ padding: 12px 20px; border-bottom: 1px solid #f3f4f6; }}
    .job:last-child {{ border-bottom: none; }}
    .job-title a {{ font-weight: 600; color: #2563eb; text-decoration: none; font-size: 0.95em; }}
    .job-title a:hover {{ text-decoration: underline; }}
    .job-meta {{ color: #6b7280; font-size: 0.82em; margin-top: 4px; }}
    .score {{ color: #f59e0b; letter-spacing: 1px; }}
    .new-badge {{ background: #dcfce7; color: #15803d; font-size: 0.68em; font-weight: 700;
                  padding: 2px 7px; border-radius: 10px; margin-left: 8px; vertical-align: middle; }}
    .ats {{ background: #eff6ff; color: #3b82f6; font-size: 0.75em; padding: 1px 6px; border-radius: 4px; }}
  </style>
</head>
<body>
  <div class="header">
    <h1>🔍 Job Scraper v20 — Christophe D'Ippolito</h1>
    <p>{len(jobs)} offres{new_count_str} · {date_str}</p>
  </div>
  {buckets_html}
</body>
</html>"""

    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    return filename


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    # ── Filtre sociétés ───────────────────────────────────────────────────────
    sites          = filter_companies(SITES,                     args.company)
    workday_cos    = filter_companies(WORKDAY_COMPANIES,         args.company)
    sr_cos         = filter_companies(SMARTRECRUITERS_COMPANIES, args.company)
    taleo_cos      = filter_companies(TALEO_SITES,               args.company)
    greenhouse_cos = filter_companies(GREENHOUSE_COMPANIES,      args.company)
    run_uniper     = not args.company or any(c.lower() in ("uniper",) for c in args.company)

    if args.company:
        found = len(sites) + len(workday_cos) + len(sr_cos) + len(taleo_cos) + len(greenhouse_cos) + (1 if run_uniper else 0)
        print(f"🔎 Filtre --company : {', '.join(args.company)} → {found} société(s) retenue(s)")

    if PLAYWRIGHT_AVAILABLE:
        mode = "✅ Playwright + fallback requests — FULL COVERAGE"
    else:
        mode = "🚨 requests ONLY — RESULTATS INCOMPLETS (pip install playwright)"
    print("=" * 65)
    print(f"🔍 JOB SCRAPER v20 — {mode}")
    print(f"   Profil : Christophe D'Ippolito | Power focus")
    print(f"   Date   : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 65)

    all_jobs = []
    summary = {}

    # ── 1 browser ouvert pour tout le run ────────────────────────────────────
    playwright_ctx = None
    browser = None
    pw_page = None
    if PLAYWRIGHT_AVAILABLE:
        try:
            playwright_ctx = sync_playwright().start()
            browser = playwright_ctx.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            pw_page = context.new_page()
            print("🌐 Browser Chromium ouvert\n")
        except Exception as e:
            print(f"🚨 Browser fail ({str(e)[:80]}) → requests fallback pour tous\n")
            # Nettoyage explicite pour éviter les processus orphelins
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                if playwright_ctx:
                    playwright_ctx.stop()
            except Exception:
                pass
            playwright_ctx = None
            browser = None
            pw_page = None

    try:
        # ── APIs JSON (pas de browser nécessaire) ─────────────────────────────
        print("── Workday API ──────────────────────────────────────────────")
        for co in workday_cos:
            print(f"🏢 {co['name']}...", end=" ", flush=True)
            jobs = scrape_workday(co)
            print(f"✅ {len(jobs)}" if jobs else "⚠️  0")
            all_jobs.extend(jobs); summary[co["name"]] = len(jobs); time.sleep(1)

        print("\n── SmartRecruiters API ──────────────────────────────────────")
        for co in sr_cos:
            print(f"🏢 {co['name']}...", end=" ", flush=True)
            jobs = scrape_smartrecruiters(co)
            print(f"✅ {len(jobs)}" if jobs else "⚠️  0")
            all_jobs.extend(jobs); summary[co["name"]] = len(jobs); time.sleep(1)

        if run_uniper:
            print("\n── Uniper API ───────────────────────────────────────────────")
            print("🏢 Uniper...", end=" ", flush=True)
            jobs = scrape_uniper()
            print(f"✅ {len(jobs)}" if jobs else "⚠️  0")
            all_jobs.extend(jobs); summary["Uniper"] = len(jobs)

        print("\n── Greenhouse API ───────────────────────────────────────────")
        for co in greenhouse_cos:
            print(f"🏢 {co['name']}...", end=" ", flush=True)
            jobs = scrape_greenhouse(co)
            print(f"✅ {len(jobs)}" if jobs else "⚠️  0")
            all_jobs.extend(jobs); summary[co["name"]] = len(jobs); time.sleep(0.5)

        print("\n── Oracle Taleo (TotalEnergies, Macquarie) ──────────────────")
        for co in taleo_cos:
            print(f"🏢 {co['name']}...", end=" ", flush=True)
            jobs = scrape_taleo(co)
            print(f"✅ {len(jobs)}" if jobs else "⚠️  0")
            all_jobs.extend(jobs); summary[co["name"]] = len(jobs)

        # ── Sites HTML — même browser pour tous ───────────────────────────────
        print("\n── Sites HTML ───────────────────────────────────────────────")
        for site in sites:
            print(f"🏢 {site['name']}...", end=" ", flush=True)
            jobs = scrape_site(site, pw_page=pw_page)
            src = set(j["source"] for j in jobs) if jobs else set()
            print(f"✅ {len(jobs)} [{', '.join(src)}]" if jobs else "⚠️  0")
            all_jobs.extend(jobs); summary[site["name"]] = len(jobs); time.sleep(0.5)

    finally:
        # Fermeture browser dans tous les cas (évite les processus orphelins)
        if playwright_ctx:
            try:
                if browser:
                    browser.close()
                playwright_ctx.stop()
                print("\n🌐 Browser fermé")
            except Exception:
                pass

    # ── Score + tri ───────────────────────────────────────────────────────────
    for job in all_jobs:
        job["score"] = score_job(job)

    # ── Delta mode ────────────────────────────────────────────────────────────
    new_urls = set()
    last_run_date = ""
    if args.new_only:
        seen_urls, last_run_date = load_seen()
        new_urls = {j["url"] for j in all_jobs} - seen_urls
        save_seen(all_jobs)

    # ── Filtre bucket ─────────────────────────────────────────────────────────
    display_jobs = all_jobs
    if args.bucket:
        bl = [b.lower() for b in args.bucket]
        display_jobs = [j for j in display_jobs if any(b in j["bucket"].lower() for b in bl)]

    # ── Filtre new-only sur display ───────────────────────────────────────────
    if args.new_only:
        display_jobs = [j for j in display_jobs if j["url"] in new_urls]

    # ── Non-régression ────────────────────────────────────────────────────────
    all_titles = [j["title"] for j in all_jobs]
    print("\n── ✅ Non-régression ────────────────────────────────────────")
    for ref in NON_REGRESSION:
        found = any(ref.lower() in t.lower() for t in all_titles)
        print(f"   {'✅' if found else '⚠️  MANQUANT'} — {ref}")

    # ── Affichage par bucket ──────────────────────────────────────────────────
    print("\n" + "=" * 65)
    if args.new_only:
        since = f" depuis {last_run_date}" if last_run_date else ""
        print(f"🆕 {len(display_jobs)} nouvelles offres{since} (sur {len(all_jobs)} total)")
    else:
        print(f"📊 {len(display_jobs)} offres pertinentes (toutes localisations)")
    print("=" * 65)

    for bucket in BUCKET_ORDER:
        bucket_jobs = sorted([j for j in display_jobs if j["bucket"] == bucket],
                             key=lambda x: x["score"], reverse=True)
        if not bucket_jobs:
            continue
        print(f"\n{'=' * 65}")
        print(f"  {bucket} — {len(bucket_jobs)} offre(s)")
        print(f"{'=' * 65}")
        for i, job in enumerate(bucket_jobs, 1):
            is_new = job["url"] in new_urls
            new_tag = " 🆕" if is_new else ""
            print(f"  #{i:02d} [{job['score']}⭐]{new_tag} {job['title']}")
            print(f"       🏢 {job['company']} | 📍 {job['location']} | {job['source']}")
            print(f"       🔗 {job['url']}")
            if job.get("date"):
                print(f"       📅 {job['date']}")
            print()

    # ── Export JSON ───────────────────────────────────────────────────────────
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    out = f"jobs_v20_{ts}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(
            sorted(display_jobs, key=lambda x: (BUCKET_ORDER.index(x["bucket"]), -x["score"])),
            f, ensure_ascii=False, indent=2)
    print(f"\n✅ Export JSON : {out}")

    # ── Rapport HTML ──────────────────────────────────────────────────────────
    if not args.no_html:
        html_file = generate_html_report(display_jobs, new_urls if args.new_only else None)
        print(f"🌐 Rapport HTML : {html_file}")

    print("\n📋 RÉCAP PAR SOCIÉTÉ")
    for co, cnt in sorted(summary.items(), key=lambda x: -x[1]):
        if cnt > 0:
            print(f"   ✅ {co:33s} {cnt:3d} offre(s)")

    zeros = [co for co, cnt in summary.items() if cnt == 0]
    if zeros:
        print(f"\n🔧 {len(zeros)} société(s) à investiguer (0 offre) :")
        for co in sorted(zeros):
            print(f"   ⚠️  {co}")


if __name__ == "__main__":
    main()