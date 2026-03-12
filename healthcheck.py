"""
Health Check — S1 configs
Vérifie que chaque config retourne ≥ 1 offre, sans filtre métier.
Usage : python healthcheck.py
"""

import requests
import json
import urllib3
from bs4 import BeautifulSoup

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
print("\n── HTML ──")
for s in SITES:
    url = s["pages"][0]
    pattern = s.get("job_pattern", "")
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            check(s["name"], "HTML", 0, f"HTTP {r.status_code}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        if pattern and pattern != "*":
            links = [a for a in soup.find_all("a", href=True) if pattern in a["href"]]
        else:
            links = soup.find_all("a", href=True)
        check(s["name"], "HTML", len(links))
    except Exception as e:
        check(s["name"], "HTML", 0, str(e)[:60])

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

with open("/tmp/healthcheck.md", "w") as f:
    f.write("\n".join(md_lines))
print("Résultats écrits dans /tmp/healthcheck.md")
