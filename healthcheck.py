"""
Health Check — S1 configs
Vérifie que chaque config retourne ≥ 1 offre, sans filtre métier.
Usage : python healthcheck.py
"""

import requests
import urllib3
import pathlib
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from job_scrapper import (
    SITES,
    WORKDAY_COMPANIES,
    SMARTRECRUITERS_COMPANIES,
    GREENHOUSE_COMPANIES,
    TALEO_SITES,
    HEADERS,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 15
results = []
OUT_FILE = pathlib.Path(__file__).parent / "healthcheck.md"


def check(name, ats, count, error=None):
    if error:
        status = "❌"
        detail = error
    elif count == 0:
        status = "❌"
        detail = "0 jobs"
    else:
        status = "✅"
        detail = f"{count} jobs"
    results.append((status, name, ats, detail))
    print(f"{status}  {name:<35} {ats:<16} {detail}")


def count_links(html: str, pattern: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    if pattern and pattern != "*":
        return len([a for a in soup.find_all("a", href=True) if pattern in a["href"]])
    return len(soup.find_all("a", href=True))


# ── Workday ────────────────────────────────────────────────────────────────────
print("\n── Workday ──")
for c in WORKDAY_COMPANIES:
    url = f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    try:
        r = requests.post(url, json={"limit": 1, "offset": 0}, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            check(c["name"], "Workday", 0, f"HTTP {r.status_code}")
        else:
            data = r.json()
            count = len(data.get("jobPostings", []))
            total = data.get("total", count)
            check(c["name"], "Workday", total)
    except Exception as e:
        check(c["name"], "Workday", 0, str(e)[:60])

# ── SmartRecruiters ────────────────────────────────────────────────────────────
print("\n── SmartRecruiters ──")
for c in SMARTRECRUITERS_COMPANIES:
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
for c in GREENHOUSE_COMPANIES:
    url = f"https://boards-api.{c['region']}.greenhouse.io/v1/boards/{c['board_token']}/jobs"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            check(c["name"], "Greenhouse", 0, f"HTTP {r.status_code}")
        else:
            data = r.json()
            count = len(data.get("jobs", []))
            check(c["name"], "Greenhouse", count)
    except Exception as e:
        check(c["name"], "Greenhouse", 0, str(e)[:60])

# ── HTML (SITES) ───────────────────────────────────────────────────────────────
print(f"\n── HTML {'(Playwright)' if PLAYWRIGHT_AVAILABLE else '(requests only — install Playwright for full check)'} ──")

def _check_html_site(s, pw_page=None):
    url = s["pages"][0]
    pattern = s.get("job_pattern", "")
    try:
        # Playwright : networkidle puis scroll pour JS lazy-loading
        if pw_page is not None:
            try:
                try:
                    pw_page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception:
                    pw_page.goto(url, wait_until="load", timeout=30000)
                for _ in range(3):
                    pw_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    pw_page.wait_for_timeout(700)
                pw_page.wait_for_timeout(1000)
                n = count_links(pw_page.content(), pattern)
                check(s["name"], "HTML/PW", n)
                return
            except Exception as e:
                print(f"     ↳ Playwright fail → requests ({str(e)[:60]})")
        # Fallback requests
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            check(s["name"], "HTML", 0, f"HTTP {r.status_code}")
            return
        check(s["name"], "HTML", count_links(r.text, pattern))
    except Exception as e:
        check(s["name"], "HTML", 0, str(e)[:60])

if PLAYWRIGHT_AVAILABLE:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_extra_http_headers(HEADERS)
        for s in SITES:
            _check_html_site(s, pw_page=page)
        browser.close()
else:
    for s in SITES:
        _check_html_site(s)

# ── Taleo ──────────────────────────────────────────────────────────────────────
print("\n── Taleo ──")
for c in TALEO_SITES:
    # Un seul mot-clé large suffit pour vérifier que le site répond
    url = f"{c['base']}/en_US/careers/SearchJobs/trader?listFilterMode=1&jobRecordsPerPage=5"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            check(c["name"], "Taleo", 0, f"HTTP {r.status_code}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        links = [a for a in soup.find_all("a", href=True) if "/careers/jobdetails/" in a["href"] or "requisitionId" in a["href"]]
        check(c["name"], "Taleo", len(links))
    except Exception as e:
        check(c["name"], "Taleo", 0, str(e)[:60])

# ── Résumé ─────────────────────────────────────────────────────────────────────
ok = sum(1 for r in results if r[0] == "✅")
ko = sum(1 for r in results if r[0] == "❌")
print(f"\n── Résumé : {ok} OK / {ko} BROKEN / {len(results)} total ──\n")

# Écriture markdown
md_lines = ["# Health Check — S1 configs\n", "| Statut | Entreprise | ATS | Détail |", "|--------|-----------|-----|--------|"]
for status, name, ats, detail in results:
    md_lines.append(f"| {status} | {name} | {ats} | {detail} |")
md_lines.append(f"\n**{ok} OK / {ko} BROKEN / {len(results)} total**")

OUT_FILE.write_text("\n".join(md_lines), encoding="utf-8")
print(f"Résultats écrits dans {OUT_FILE}")
