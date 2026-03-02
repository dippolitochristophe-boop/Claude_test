"""
Job Scraper v11

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

SEARCH_QUERIES = [
    "power trader", "energy trader", "front office power",
    "portfolio manager energy", "head of trading", "market risk power",
    "risk officer power", "origination energy", "renewables trader",
    "BESS trading", "PPA structuring", "intraday trading",
    "algorithmic trading energy", "asset optimizer power",
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
                 "switzerland", "swiss", "olten", "bern", "berne", "basel", "zug", "baar"]
BUCKET_EUR    = ["paris", "amsterdam", "brussels", "rotterdam", "oslo", "stockholm",
                 "hamburg", "düsseldorf", "madrid", "milan", "frankfurt", "netherlands",
                 "france", "germany", "norway", "sweden", "spain", "italy", "belgium",
                 "luxembourg", "denmark", "finland", "austria", "prague", "warsaw", "vienna"]

BUCKET_ORDER = ["🇬🇧 London", "🇨🇭 Switzerland", "🇪🇺 Other Europe", "Unknown", "🌍 Rest of World"]


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
        "job_pattern": "/en/",
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
]

# ── APIs JSON (Workday + SmartRecruiters) ─────────────────────────────────────

WORKDAY_COMPANIES = [
    {"name": "Trafigura", "tenant": "trafigura", "site": "TrafiguraCareerSite", "wd": "wd3"},
    {"name": "Gunvor",    "tenant": "gunvor",    "site": "Gunvor_Careers",      "wd": "wd3"},
    {"name": "Shell",     "tenant": "shell",     "site": "ShellCareers",        "wd": "wd3"},
    {"name": "BP",        "tenant": "bpplc",     "site": "BP",                  "wd": "wd5"},
    {"name": "Equinor",   "tenant": "equinor",   "site": "EQNR",                "wd": "wd3"},
    {"name": "Orsted",    "tenant": "orsted",    "site": "OrstedCareers",       "wd": "wd3"},
    {"name": "EDF Trading","tenant": "edftrading","site": "EDFTrading",           "wd": "wd3"},  # wd1 non confirmé → wd3 par défaut
    {"name": "Centrica",  "tenant": "centrica",  "site": "Centrica",            "wd": "wd3"},
]

SMARTRECRUITERS_COMPANIES = [
    {"name": "Vattenfall", "sr_id": "Vattenfall"},
    {"name": "RWE",        "sr_id": "RWE"},
    {"name": "Uniper",     "sr_id": "Uniper"},
    {"name": "Vitol",      "sr_id": "Vitol"},
    {"name": "ENGIE",      "sr_id": "ENGIE"},
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

        # Localisation : cherche dans le parent, "Unknown" si non trouvé
        location = ""
        parent = a.find_parent(["li", "article", "div", "section"])
        if parent:
            for s in parent.find_all(string=True):
                s = s.strip()
                if any(loc in s.lower() for loc in ["olten", "zurich", "zürich", "geneva",
                       "london", "lausanne", "bern", "basel", "oslo", "paris", "amsterdam",
                       "rotterdam", "brussels", "stockholm", "hamburg", "madrid", "milan",
                       "houston", "singapore", "dubai", "new york", "tokyo"]):
                    location = s
                    break

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
                "source": "Playwright" if PLAYWRIGHT_AVAILABLE else "requests",
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
                pw_page.goto(url, wait_until="networkidle", timeout=25000)
                pw_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                pw_page.wait_for_timeout(1500)
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


def scrape_all_sites(sites: list) -> dict:
    """
    Un seul browser Playwright pour tous les sites HTML.
    Retourne dict {company_name: [jobs]}.
    """
    results = {}

    if PLAYWRIGHT_AVAILABLE:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=HEADERS["User-Agent"])
                pw_page = context.new_page()

                for site in sites:
                    print(f"🏢 {site['name']}...", end=" ", flush=True)
                    jobs = scrape_site(site, pw_page=pw_page)
                    src = set(j["source"] for j in jobs) if jobs else set()
                    print(f"✅ {len(jobs)} [{', '.join(src)}]" if jobs else "⚠️  0")
                    results[site["name"]] = jobs
                    time.sleep(0.5)

                browser.close()
            return results
        except Exception as e:
            print(f"🚨 Browser crash global ({str(e)[:80]}) → fallback requests pour tous")

    # Fallback total : requests sans browser
    for site in sites:
        print(f"🏢 {site['name']}...", end=" ", flush=True)
        jobs = scrape_site(site, pw_page=None)
        print(f"✅ {len(jobs)} [requests]" if jobs else "⚠️  0")
        results[site["name"]] = jobs
        time.sleep(1)

    return results


# _fallback_requests remplacé par _get_jobs_requests


# ── Workday API ───────────────────────────────────────────────────────────────

def scrape_workday(company: dict) -> list[dict]:
    jobs = []
    seen = set()
    for query in SEARCH_QUERIES:
        url = (f"https://{company['tenant']}.{company['wd']}.myworkdayjobs.com"
               f"/wday/cxs/{company['tenant']}/{company['site']}/jobs")
        try:
            r = requests.post(url,
                json={"limit": 20, "offset": 0, "searchText": query},
                headers={**HEADERS, "Content-Type": "application/json"},
                timeout=12, verify=False)
            if r.status_code == 200:
                for job in r.json().get("jobPostings", []):
                    title = job.get("title", "").strip()
                    location = job.get("locationsText", "")
                    if title and title not in seen and is_relevant_title(title):
                        seen.add(title)
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
        except Exception:
            pass
        time.sleep(0.3)
    return jobs


# ── SmartRecruiters API ───────────────────────────────────────────────────────

def scrape_smartrecruiters(company: dict) -> list[dict]:
    jobs = []
    seen = set()
    for q in ["power trader", "energy trader", "trading power", "risk power", "origination energy"]:
        try:
            r = requests.get(
                f"https://api.smartrecruiters.com/v1/companies/{company['sr_id']}/postings",
                params={"q": q, "limit": 20},
                headers=HEADERS, timeout=12, verify=False)
            if r.status_code == 200:
                for job in r.json().get("content", []):
                    title = job.get("name", "").strip()
                    loc = job.get("location", {})
                    location = f"{loc.get('city', '')} {loc.get('country', '')}".strip()
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
        except Exception:
            pass
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

TALEO_LOCATIONS = [
    "london", "paris", "geneva", "amsterdam", "brussels",
    "singapore", "dubai", "houston", "sydney", "new york",
]


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
                    location = ""
                    parent = a.find_parent(["li", "div", "tr", "section"])
                    if parent:
                        for s in parent.find_all(string=True):
                            s = s.strip()
                            if any(loc in s.lower() for loc in TALEO_LOCATIONS):
                                location = s
                                break
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
    print(f"🔍 JOB SCRAPER v11 — {mode}")
    print(f"   Profil : Christophe D'Ippolito | Power focus")
    print(f"   Date   : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 65)

    all_jobs = []
    summary = {}

    # ── 1 browser ouvert pour tout le run ────────────────────────────────────
    playwright_ctx = None
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
            playwright_ctx = None
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
        # Fermeture browser dans tous les cas
        if playwright_ctx:
            try:
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
    out = f"jobs_v11_{ts}.json"
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