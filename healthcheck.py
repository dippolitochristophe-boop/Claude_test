"""
Health Check — S1 configs
Vérifie que chaque config retourne ≥ 1 offre, sans filtre métier.
Utilise smart_scrape_site (même code path que le scraper) pour les sites HTML.

Usage : python healthcheck.py [--company NomSociété]
"""

import argparse
import pathlib
import requests
import urllib3

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from job_scrapper import (
    SITES, WORKDAY_COMPANIES, SMARTRECRUITERS_COMPANIES,
    GREENHOUSE_COMPANIES, TALEO_SITES, HEADERS,
)
from playwright_strategies import smart_scrape_site

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 15
OUT_FILE = pathlib.Path(__file__).parent / "healthcheck.md"


# ── CLI args ───────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--company", default=None, help="Filtrer sur une seule société")
args, _ = parser.parse_known_args()

results = []


def check(name, ats, count, error=None, strategy=""):
    if error:
        status, detail = "❌", error
    elif count == 0:
        status, detail = "❌", "0 jobs (scraping OK mais rien trouvé)"
    else:
        status, detail = "✅", f"{count} jobs"
    strat_str = f"  [{strategy}]" if strategy else ""
    results.append((status, name, ats, detail))
    print(f"{status}  {name:<40} {ats:<16} {detail}{strat_str}")


def _filter(lst, key="name"):
    if not args.company:
        return lst
    return [x for x in lst if args.company.lower() in x.get(key, "").lower()]


# ── Workday ────────────────────────────────────────────────────────────────────
print("\n── Workday ──")
for c in _filter(WORKDAY_COMPANIES):
    url = (f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com"
           f"/wday/cxs/{c['tenant']}/{c['site']}/jobs")
    try:
        r = requests.post(url, json={"limit": 1, "offset": 0},
                          headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            check(c["name"], "Workday", 0, f"HTTP {r.status_code}")
        else:
            data = r.json()
            total = data.get("total", len(data.get("jobPostings", [])))
            check(c["name"], "Workday", total)
    except Exception as e:
        check(c["name"], "Workday", 0, str(e)[:60])

# ── SmartRecruiters ────────────────────────────────────────────────────────────
print("\n── SmartRecruiters ──")
for c in _filter(SMARTRECRUITERS_COMPANIES):
    url = f"https://api.smartrecruiters.com/v1/companies/{c['sr_id']}/postings?limit=1"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            check(c["name"], "SmartRec", 0, f"HTTP {r.status_code}")
        else:
            data = r.json()
            total = data.get("totalFound", len(data.get("content", [])))
            check(c["name"], "SmartRec", total)
    except Exception as e:
        check(c["name"], "SmartRec", 0, str(e)[:60])

# ── Greenhouse ─────────────────────────────────────────────────────────────────
print("\n── Greenhouse ──")
for c in _filter(GREENHOUSE_COMPANIES):
    url = f"https://boards-api.{c['region']}.greenhouse.io/v1/boards/{c['board_token']}/jobs"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            check(c["name"], "Greenhouse", 0, f"HTTP {r.status_code}")
        else:
            data = r.json()
            check(c["name"], "Greenhouse", len(data.get("jobs", [])))
    except Exception as e:
        check(c["name"], "Greenhouse", 0, str(e)[:60])

# ── Taleo ──────────────────────────────────────────────────────────────────────
print("\n── Taleo ──")
from bs4 import BeautifulSoup
for c in _filter(TALEO_SITES):
    url = f"{c['base']}/en_US/careers/SearchJobs/trader?listFilterMode=1&jobRecordsPerPage=5"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            check(c["name"], "Taleo", 0, f"HTTP {r.status_code}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        links = [a for a in soup.find_all("a", href=True)
                 if "/careers/jobdetails/" in a["href"] or "requisitionId" in a["href"]]
        check(c["name"], "Taleo", len(links))
    except Exception as e:
        check(c["name"], "Taleo", 0, str(e)[:60])

# ── HTML / SITES — via smart_scrape_site (validate_mode=True) ─────────────────
# validate_mode=True : même code path que le scraper, mais sans filtre is_relevant_title
# → distingue "scraping cassé" (0 jobs bruts) vs "0 jobs pertinents" (filtre titre)
label = "(Playwright)" if PLAYWRIGHT_AVAILABLE else "(requests only)"
print(f"\n── HTML {label} — validate_mode ──")

filtered_sites = _filter(SITES)

if PLAYWRIGHT_AVAILABLE:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_extra_http_headers(HEADERS)
        for s in filtered_sites:
            try:
                jobs_raw, strategy = smart_scrape_site(s, page, validate_mode=True)
                check(s["name"], "HTML/PW", len(jobs_raw), strategy=strategy)
            except Exception as e:
                check(s["name"], "HTML/PW", 0, str(e)[:60])
        browser.close()
else:
    for s in filtered_sites:
        try:
            jobs_raw, strategy = smart_scrape_site(s, None, validate_mode=True)
            check(s["name"], "HTML", len(jobs_raw), strategy=strategy)
        except Exception as e:
            check(s["name"], "HTML", 0, str(e)[:60])

# ── Résumé ─────────────────────────────────────────────────────────────────────
ok  = sum(1 for r in results if r[0] == "✅")
ko  = sum(1 for r in results if r[0] == "❌")
print(f"\n── Résumé : {ok} OK / {ko} BROKEN / {len(results)} total ──\n")
if ko:
    print("Sociétés BROKEN — relancer avec agent_validator.py pour diagnostic :")
    for status, name, ats, detail in results:
        if status == "❌":
            print(f"  • {name} ({ats}) : {detail}")

# ── Export markdown ────────────────────────────────────────────────────────────
md = ["# Health Check — S1 configs\n",
      "| Statut | Entreprise | ATS | Détail |",
      "|--------|-----------|-----|--------|"]
for status, name, ats, detail in results:
    md.append(f"| {status} | {name} | {ats} | {detail} |")
md.append(f"\n**{ok} OK / {ko} BROKEN / {len(results)} total**")
OUT_FILE.write_text("\n".join(md), encoding="utf-8")
print(f"Résultats écrits dans {OUT_FILE}")
