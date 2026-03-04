"""
Job Scraper v18

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

import requests
import json
import time
from datetime import datetime
import urllib3
from bs4 import BeautifulSoup
from urllib.parse import urlparse

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("")
    print("=" * 65)
    print("  🚨 PLAYWRIGHT NON INSTALLE — RESULTATS INCOMPLETS 🚨")
    print("  Societes manquantes : TotalEnergies, Mercuria, EDF Trading,")
    print("  Statkraft, BP, Hartree, Freepoint, MET Group...")
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
        "pages": ["https://www.hartreepartners.com/about/careers/"],
        "job_pattern": "/careers/",
    },
    {
        "name": "Statkraft",
        "type": "html",
        "pages": ["https://www.statkraft.com/careers/open-positions/"],
        "job_pattern": "/careers/jobs/",
    },
    # BP → Workday (voir WORKDAY_COMPANIES — doublon supprimé ici)
    # TotalEnergies → Taleo (voir TALEO_SITES)
    # ── Genève / Londres ──────────────────────────────────────────────────────
    {
        "name": "Mercuria",
        "type": "html",
        "pages": ["https://www.mercuria.com/careers/"],
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
    },
    {
        "name": "Uniper",
        "type": "html",
        "pages": ["https://careers.uniper.energy/en"],
        "job_pattern": "/job/",  # iCIMS — pattern typique /job/Job-Title/JOBID/
    },
    {
        "name": "ENGIE",
        "type": "html",
        "pages": ["https://jobs.engie.com/"],
        "job_pattern": "/job/",  # confirmé : jobs.engie.com/job/{title}/{id}-en_US/
    },
    # ── Commodity traders / bourses d'énergie ─────────────────────────────────
    {
        "name": "Glencore",
        "type": "html",
        "pages": ["https://www.glencore.com/careers/jobs"],
        "job_pattern": "/careers/jobs/",  # /careers/jobs/{UUID-or-JR-ID}
    },
    {
        "name": "EEX Group",
        "type": "html",
        "pages": ["https://career.deutsche-boerse.com/eex"],
        "job_pattern": "/eex/job/",  # career.deutsche-boerse.com/eex/job/{loc}/{title}/{id}
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
]

# ── APIs JSON (Workday + SmartRecruiters) ─────────────────────────────────────

WORKDAY_COMPANIES = [
    {"name": "Trafigura", "tenant": "trafigura",    "site": "TrafiguraCareerSite", "wd": "wd3"},  # ✅ confirmé
    {"name": "Gunvor",    "tenant": "gunvor",        "site": "Gunvor_Careers",      "wd": "wd3"},  # ✅ confirmé
    {"name": "Shell",     "tenant": "shell",         "site": "ShellCareers",        "wd": "wd3"},  # ✅ confirmé
    {"name": "BP",        "tenant": "bpinternational","site": "bpCareers",           "wd": "wd3"},  # ✅ confirmé (corrigé v15)
    {"name": "Equinor",   "tenant": "equinor",       "site": "EQNR",                "wd": "wd3"},  # ✅ confirmé
    {"name": "Orsted",    "tenant": "orsted",        "site": "OrstedCareers",       "wd": "wd3"},  # ❓ non confirmé — portail Workday non indexé
    {"name": "EDF Trading","tenant": "edftrading",   "site": "EDFTrading",          "wd": "wd1"},  # ✅ confirmé (corrigé v15)
    {"name": "Centrica",  "tenant": "centrica",      "site": "Centrica",            "wd": "wd3"},  # ✅ confirmé
]

SMARTRECRUITERS_COMPANIES = [
    {"name": "Vattenfall", "sr_id": "Vattenfall"},  # confirmé : careers.smartrecruiters.com/vattenfall
    {"name": "Vitol",      "sr_id": "Vitol"},       # confirmé : jobs.smartrecruiters.com/Vitol/...
    # RWE   → déplacé dans SITES (portail HTML propre, pas SmartRecruiters)
    # Uniper → déplacé dans SITES (portail iCIMS careers.uniper.energy)
    # ENGIE  → déplacé dans SITES (portail jobs.engie.com)
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
                pw_page.wait_for_timeout(1000)
                jobs = parse_jobs_from_html(pw_page.content(), site)
                for j in jobs:
                    j["source"] = "Playwright"
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
            return jobs
    except Exception:
        pass
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if PLAYWRIGHT_AVAILABLE:
        mode = "✅ Playwright + fallback requests — FULL COVERAGE"
    else:
        mode = "🚨 requests ONLY — RESULTATS INCOMPLETS (pip install playwright)"
    print("=" * 65)
    print(f"🔍 JOB SCRAPER v18 — {mode}")
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
        for co in WORKDAY_COMPANIES:
            print(f"🏢 {co['name']}...", end=" ", flush=True)
            jobs = scrape_workday(co)
            print(f"✅ {len(jobs)}" if jobs else "⚠️  0")
            all_jobs.extend(jobs); summary[co["name"]] = len(jobs); time.sleep(1)

        print("\n── SmartRecruiters API ──────────────────────────────────────")
        for co in SMARTRECRUITERS_COMPANIES:
            print(f"🏢 {co['name']}...", end=" ", flush=True)
            jobs = scrape_smartrecruiters(co)
            print(f"✅ {len(jobs)}" if jobs else "⚠️  0")
            all_jobs.extend(jobs); summary[co["name"]] = len(jobs); time.sleep(1)

        print("\n── Oracle Taleo (TotalEnergies, Macquarie) ──────────────────")
        for co in TALEO_SITES:
            print(f"🏢 {co['name']}...", end=" ", flush=True)
            jobs = scrape_taleo(co)
            print(f"✅ {len(jobs)}" if jobs else "⚠️  0")
            all_jobs.extend(jobs); summary[co["name"]] = len(jobs)

        # ── Sites HTML — même browser pour tous ───────────────────────────────
        print("\n── Sites HTML ───────────────────────────────────────────────")
        for site in SITES:
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

    # ── Non-régression ────────────────────────────────────────────────────────
    all_titles = [j["title"] for j in all_jobs]
    print("\n── ✅ Non-régression ────────────────────────────────────────")
    for ref in NON_REGRESSION:
        found = any(ref.lower() in t.lower() for t in all_titles)
        print(f"   {'✅' if found else '⚠️  MANQUANT'} — {ref}")

    # ── Affichage par bucket ──────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print(f"📊 {len(all_jobs)} offres pertinentes (toutes localisations)")
    print("=" * 65)

    for bucket in BUCKET_ORDER:
        bucket_jobs = sorted([j for j in all_jobs if j["bucket"] == bucket],
                             key=lambda x: x["score"], reverse=True)
        if not bucket_jobs:
            continue
        print(f"\n{'=' * 65}")
        print(f"  {bucket} — {len(bucket_jobs)} offre(s)")
        print(f"{'=' * 65}")
        for i, job in enumerate(bucket_jobs, 1):
            print(f"  #{i:02d} [{job['score']}⭐] {job['title']}")
            print(f"       🏢 {job['company']} | 📍 {job['location']} | {job['source']}")
            print(f"       🔗 {job['url'][:85]}")
            if job.get("date"):
                print(f"       📅 {job['date']}")
            print()

    # ── Export ────────────────────────────────────────────────────────────────
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    out = f"jobs_v18_{ts}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(
            sorted(all_jobs, key=lambda x: (BUCKET_ORDER.index(x["bucket"]), -x["score"])),
            f, ensure_ascii=False, indent=2)
    print(f"\n✅ Export : {out}")

    print("\n📋 RÉCAP PAR SOCIÉTÉ")
    for co, cnt in sorted(summary.items(), key=lambda x: -x[1]):
        if cnt > 0:
            print(f"   {co:35s} {cnt:3d} offre(s)")


if __name__ == "__main__":
    main()