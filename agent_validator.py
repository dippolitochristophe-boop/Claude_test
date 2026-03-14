"""
Agent 3 — Validator (Health Check intelligent)

Pour chaque config (ou une société donnée) :
  1. Scrape sans filtre métier (validate_mode=True) → compte les jobs bruts
  2. Si 0 résultat → appelle Claude pour diagnostiquer la cause probable
     en lui donnant les structures d'APIs interceptées et le log de stratégie

Sorties :
  ✅ OK      → N jobs bruts trouvés (strategy)
  ⚠️  FILTRE → jobs bruts trouvés mais 0 après is_relevant_title (profil inadapté)
  ❌ BROKEN  → 0 jobs bruts + diagnostic Claude

Usage :
  python agent_validator.py                    # toutes les configs S1
  python agent_validator.py --company RWE      # une seule société
  python agent_validator.py --company RWE --verbose

ANTHROPIC_API_KEY doit être défini en variable d'environnement.
"""

import argparse
import os
import sys
import requests

# Fix Unicode output on Windows (cp1252 → utf-8)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
import urllib3
from io import StringIO
from contextlib import redirect_stdout

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

from job_scrapper import (
    SITES, WORKDAY_COMPANIES, SMARTRECRUITERS_COMPANIES,
    GREENHOUSE_COMPANIES, TALEO_SITES, HEADERS,
    scrape_site,
)
from playwright_strategies import smart_scrape_site

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 15
MODEL   = "claude-haiku-4-5-20251001"   # rapide + économique pour le diagnostic

# ── CLI ────────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--company", default=None)
parser.add_argument("--verbose", action="store_true")
args, _ = parser.parse_known_args()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _filter(lst, key="name"):
    if not args.company:
        return lst
    return [x for x in lst if args.company.lower() in x.get(key, "").lower()]


def _claude_diagnose(company: str, config: dict, scrape_log: str, jobs_raw_count: int) -> str:
    """
    Appelle Claude pour expliquer pourquoi le scraping retourne 0 résultats.
    Retourne une courte explication + suggestion de fix.
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    log_trimmed = scrape_log[-800:] if len(scrape_log) > 800 else scrape_log
    prompt = f"""Tu es expert en web scraping de portails de recrutement (ATS).

Contexte : le scraper tente de collecter les offres d'emploi de **{company}**.
Config utilisée :
```json
{config}
```

Log de scraping (validate_mode=True, sans filtre métier) :
```
{log_trimmed}
```

Résultat : **{jobs_raw_count} job(s) brut(s) trouvé(s)**.

{"Le scraping échoue complètement (0 résultat même sans filtre métier)." if jobs_raw_count == 0 else f"Le scraping trouve {jobs_raw_count} jobs bruts mais 0 après le filtre de pertinence métier."}

En 3-4 lignes maximum, donne :
1. La cause la plus probable (ex: URL obsolète, job_pattern incorrect, cookie consent non géré, API non reconnue avec clés {'{...}'}...)
2. Une suggestion concrète de fix (ex: changer job_pattern en "/X/", ajouter clé "Y" dans JOB_LIST_KEYS, ou vérifier manuellement l'URL)

Sois direct et technique. Pas de blabla."""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _validate_html_site(s: dict, pw_page=None) -> dict:
    """
    Valide un site HTML.
    Retourne {raw: int, raw_titles: list, filtered: int, strategy: str, log: str}.
    """
    from job_scrapper import is_relevant_title
    buf = StringIO()
    with redirect_stdout(buf):
        jobs_raw, strategy = smart_scrape_site(s, pw_page, validate_mode=True)
    jobs_filtered = [j for j in jobs_raw if is_relevant_title(j["title"])]

    return {
        "raw":        len(jobs_raw),
        "raw_titles": [j["title"] for j in jobs_raw],
        "filtered":   len(jobs_filtered),
        "strategy":   strategy,
        "log":        buf.getvalue(),
    }


def _validate_api(name: str, count_fn) -> dict:
    """Valide un ATS API (Workday, SmartRecruiters, Greenhouse, Taleo)."""
    try:
        count = count_fn()
        return {"raw": count, "filtered": None, "strategy": "API direct", "log": ""}
    except Exception as e:
        return {"raw": 0, "filtered": None, "strategy": "API direct",
                "log": f"Exception: {e}"}


# ── Validation par ATS ─────────────────────────────────────────────────────────

all_results = []   # (status, name, ats, detail, config, result_dict)


def report(name, ats, config, result):
    raw      = result["raw"]
    filtered = result["filtered"]
    strategy = result["strategy"]
    log      = result["log"]

    if raw == 0:
        status = "❌"
        detail = "0 jobs bruts — scraping cassé"
        # Diagnostic Claude si API key disponible
        if ANTHROPIC_AVAILABLE and os.environ.get("ANTHROPIC_API_KEY"):
            diag = _claude_diagnose(name, config, log, 0)
            detail += f"\n      🔍 {diag}"
        elif not ANTHROPIC_AVAILABLE:
            detail += "\n      (pip install anthropic pour diagnostic automatique)"
        else:
            detail += "\n      (set ANTHROPIC_API_KEY pour diagnostic automatique)"
    elif filtered is not None and filtered == 0:
        status = "⚠️ "
        detail = f"{raw} jobs bruts, 0 pertinents — profil ou filtre à ajuster"
        raw_titles = result.get("raw_titles", [])
        if raw_titles:
            for t in raw_titles[:5]:
                detail += f"\n      • {t}"
            if len(raw_titles) > 5:
                detail += f"\n      … +{len(raw_titles) - 5} autres"
        if ANTHROPIC_AVAILABLE and os.environ.get("ANTHROPIC_API_KEY"):
            diag = _claude_diagnose(name, config, log, raw)
            detail += f"\n      🔍 {diag}"
    else:
        status = "✅"
        count_str = f"{raw} jobs bruts" if filtered is None else f"{raw} bruts / {filtered} pertinents"
        detail = f"{count_str}  [{strategy}]"

    all_results.append((status, name, ats, detail))
    icon = status.strip()
    print(f"\n{icon}  {name}  ({ats})")
    print(f"   {detail.replace(chr(10), chr(10) + '   ')}")
    if args.verbose and log:
        print(f"   LOG:\n{log}")


# ── Workday ────────────────────────────────────────────────────────────────────
print("\n══ Workday ══")
for c in _filter(WORKDAY_COMPANIES):
    url = (f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com"
           f"/wday/cxs/{c['tenant']}/{c['site']}/jobs")
    def _wd_count(url=url):
        r = requests.post(url, json={"limit": 1, "offset": 0},
                          headers=HEADERS, timeout=TIMEOUT, verify=False)
        r.raise_for_status()
        d = r.json()
        return d.get("total", len(d.get("jobPostings", [])))
    result = _validate_api(c["name"], _wd_count)
    report(c["name"], "Workday", c, result)

# ── SmartRecruiters ────────────────────────────────────────────────────────────
print("\n══ SmartRecruiters ══")
for c in _filter(SMARTRECRUITERS_COMPANIES):
    sr_url = f"https://api.smartrecruiters.com/v1/companies/{c['sr_id']}/postings?limit=1"
    def _sr_count(u=sr_url):
        r = requests.get(u, headers=HEADERS, timeout=TIMEOUT, verify=False)
        r.raise_for_status()
        d = r.json()
        return d.get("totalFound", len(d.get("content", [])))
    result = _validate_api(c["name"], _sr_count)
    report(c["name"], "SmartRec", c, result)

# ── Greenhouse ─────────────────────────────────────────────────────────────────
print("\n══ Greenhouse ══")
for c in _filter(GREENHOUSE_COMPANIES):
    gh_url = f"https://boards-api.{c['region']}.greenhouse.io/v1/boards/{c['board_token']}/jobs"
    def _gh_count(u=gh_url):
        r = requests.get(u, headers=HEADERS, timeout=TIMEOUT, verify=False)
        r.raise_for_status()
        return len(r.json().get("jobs", []))
    result = _validate_api(c["name"], _gh_count)
    report(c["name"], "Greenhouse", c, result)

# ── HTML / SITES ───────────────────────────────────────────────────────────────
filtered_sites = _filter(SITES)
if filtered_sites:
    print(f"\n══ HTML {'(Playwright)' if PLAYWRIGHT_AVAILABLE else '(requests)'} ══")
    if PLAYWRIGHT_AVAILABLE:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers(HEADERS)
            for s in filtered_sites:
                result = _validate_html_site(s, page)
                report(s["name"], "HTML/PW", s, result)
            browser.close()
    else:
        for s in filtered_sites:
            result = _validate_html_site(s, None)
            report(s["name"], "HTML", s, result)

# ── Résumé ─────────────────────────────────────────────────────────────────────
ok      = sum(1 for r in all_results if r[0].strip() == "✅")
warn    = sum(1 for r in all_results if r[0].strip() == "⚠️")
broken  = sum(1 for r in all_results if r[0].strip() == "❌")
total   = len(all_results)

print(f"\n{'═'*60}")
print(f"  {ok} OK  |  {warn} FILTRE  |  {broken} BROKEN  |  {total} total")
print(f"{'═'*60}\n")
