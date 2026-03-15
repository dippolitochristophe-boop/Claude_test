"""
Agent 3 — Validator

Valide qu'une config générée par Agent 2 retourne ≥1 offre réelle.
Réutilise directement les fonctions HTTP de job_scrapper.py — pas de LLM pour la validation.
Claude (Haiku) est appelé uniquement pour le diagnostic si raw_count == 0.

Statuts de sortie :
  ok     — config valide, N offres trouvées (dont ≥1 pertinente pour le profil)
  filter — config valide (raw_count > 0) mais 0 offre pertinente pour ce profil
  broken — 0 offre brute → config cassée
  skip   — ATS inconnu ou unsupported → pas de validation possible

Usage direct :
    python agents/agent3_validator.py               # teste toutes les configs S1
    python agents/agent3_validator.py Trafigura     # teste une config S1 connue
"""

import json
import os
import sys
import requests
import urllib3

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Fix Unicode output on Windows (cp1252 → utf-8)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from job_scrapper import HEADERS, is_relevant_title, configure as configure_scraper

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

TIMEOUT = 15
MODEL = "claude-haiku-4-5-20251001"


# ── Fonction principale ────────────────────────────────────────────────────────

def validate(agent2_result: dict, profile: dict = None, progress_cb=None) -> dict:
    """
    Valide une config issue d'Agent 2.

    Input  : dict {name, ats_type, config, confidence, notes}
             profile : profil utilisateur pour le filtre de pertinence (optionnel)
    Output : dict {name, status, raw_count, filtered_count, sample_job, diagnosis}
    """
    # Configurer le scraper avec le profil pour que is_relevant_title() soit correct
    if profile:
        configure_scraper(profile)

    name     = agent2_result.get("name", "Unknown")
    ats_type = agent2_result.get("ats_type", "unknown")
    config   = agent2_result.get("config", {})

    if ats_type == "linkedin":
        url = (config or {}).get("linkedin_url", f"https://www.linkedin.com/company/{name.lower().replace(' ', '-')}/jobs/")
        if progress_cb:
            progress_cb(f"  🔗 {name} — LinkedIn Easy Apply only: {url}")
        return _result(name, "linkedin", 0, 0, None, url)

    if ats_type == "unknown" or not config:
        return _result(name, "skip", 0, 0, None, "ATS unknown — validation skipped")

    if progress_cb:
        progress_cb(f"Agent 3 — Validating {name} ({ats_type})...")

    try:
        if ats_type == "workday":
            raw_jobs = _validate_workday(config)
        elif ats_type == "smartrecruiters":
            raw_jobs = _validate_smartrecruiters(config)
        elif ats_type == "greenhouse":
            raw_jobs = _validate_greenhouse(config)
        elif ats_type == "html":
            raw_jobs = _validate_html(config)
        elif ats_type == "taleo":
            raw_jobs = _validate_taleo(config)
        elif ats_type == "lever":
            raw_jobs = _validate_lever(config)
        else:
            return _result(name, "skip", 0, 0, None, f"ATS '{ats_type}' not supported yet")

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        diagnosis = _diagnose(name, config, error_msg) if ANTHROPIC_AVAILABLE else error_msg
        return _result(name, "broken", 0, 0, None, diagnosis)

    raw_count = len(raw_jobs)

    if raw_count == 0:
        diagnosis = _diagnose(name, config, "0 jobs returned") if ANTHROPIC_AVAILABLE else "0 jobs — check config"
        return _result(name, "broken", 0, 0, None, diagnosis)

    # Appliquer le filtre métier : titre + département si disponible
    # Le département (ex: "Finance & Accounting") peut exclure des faux positifs
    EXCLUDE_DEPARTMENTS = {"finance", "accounting", "legal", "it", "hr", "communications", "marketing"}
    def _is_relevant(j: dict) -> bool:
        dept = j.get("department", "").lower()
        if dept and any(x in dept for x in EXCLUDE_DEPARTMENTS):
            return False
        return is_relevant_title(j.get("title", ""))

    filtered = [j for j in raw_jobs if _is_relevant(j)]
    filtered_count = len(filtered)
    sample = filtered[0]["title"] if filtered else raw_jobs[0].get("title", "")

    status = "ok" if filtered_count > 0 else "filter"

    if progress_cb:
        icon = "✅" if status == "ok" else "⚠️"
        progress_cb(f"  {icon} {name}: {raw_count} raw / {filtered_count} filtered")

    return _result(name, status, raw_count, filtered_count, sample, None)


# ── Validateurs par ATS ────────────────────────────────────────────────────────

def _validate_workday(config: dict) -> list:
    """Appel API Workday — retourne les jobs bruts."""
    url = (
        f"https://{config['tenant']}.{config['wd']}.myworkdayjobs.com"
        f"/wday/cxs/{config['tenant']}/{config['site']}/jobs"
    )
    r = requests.post(
        url,
        json={"limit": 20, "offset": 0, "searchText": ""},
        headers=HEADERS, timeout=TIMEOUT, verify=False,
    )
    r.raise_for_status()
    data = r.json()
    return [
        {"title": j.get("title", ""), "url": j.get("externalPath", "")}
        for j in data.get("jobPostings", [])
    ]


def _validate_smartrecruiters(config: dict) -> list:
    url = f"https://api.smartrecruiters.com/v1/companies/{config['sr_id']}/postings?limit=20"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
    r.raise_for_status()
    data = r.json()
    return [
        {"title": j.get("name", ""), "url": j.get("ref", "")}
        for j in data.get("content", [])
    ]


def _validate_greenhouse(config: dict) -> list:
    # ?content=true → includes full job description HTML + departments + offices
    region = config.get("region", "us")
    if region == "eu":
        url = f"https://boards-api.eu.greenhouse.io/v1/boards/{config['board_token']}/jobs?content=true"
    else:
        url = f"https://boards-api.greenhouse.io/v1/boards/{config['board_token']}/jobs?content=true"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
    if r.status_code == 404 and region == "eu":
        # Fallback US si EU 404
        url = f"https://boards-api.greenhouse.io/v1/boards/{config['board_token']}/jobs?content=true"
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
    r.raise_for_status()
    data = r.json()
    return [
        {
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "department": (j.get("departments") or [{}])[0].get("name", ""),
            "location": (j.get("location") or {}).get("name", ""),
        }
        for j in data.get("jobs", [])
    ]


def _validate_html(config: dict) -> list:
    """Validation HTML via requests (fallback sans Playwright)."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    pages = config.get("pages", [])
    pattern = config.get("job_pattern", "/job")
    jobs = []

    for page_url in pages[:2]:
        try:
            r = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT, verify=False)
            soup = BeautifulSoup(r.text, "html.parser")
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if pattern in href:
                    full_url = urljoin(page_url, href)
                    title = link.get_text(strip=True) or href
                    if title and 5 < len(title) < 120:
                        jobs.append({"title": title, "url": full_url})
        except Exception:
            pass

    # Dédupliquer par URL
    seen = set()
    unique = []
    for j in jobs:
        if j["url"] not in seen:
            seen.add(j["url"])
            unique.append(j)
    return unique


def _validate_taleo(config: dict) -> list:
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    base = config.get("base", "")
    url = f"{base}/en_US/careers/SearchJobs/"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
    soup = BeautifulSoup(r.text, "html.parser")
    return [
        {"title": link.get_text(strip=True), "url": urljoin(url, link["href"])}
        for link in soup.find_all("a", href=True)
        if "JobDetail" in link["href"]
    ]


def _validate_lever(config: dict) -> list:
    """Lever ATS — API publique simple."""
    company = config.get("lever_id") or config.get("name", "").lower().replace(" ", "")
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
    r.raise_for_status()
    data = r.json()
    return [
        {"title": j.get("text", ""), "url": j.get("hostedUrl", "")}
        for j in (data if isinstance(data, list) else [])
    ]


# ── Diagnostic Claude ──────────────────────────────────────────────────────────

def _diagnose(name: str, config: dict, error: str) -> str:
    """Appelle Claude Haiku pour diagnostiquer l'échec (court, technique)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return error

    client = anthropic.Anthropic()
    prompt = f"""\
ATS scraping config FAILED for {name}.
Config: {json.dumps(config)}
Error: {error}

Answer in exactly 2 short plain-text sentences (no markdown, no headers):
sentence 1 = most likely cause (tenant/site/wd/sr_id/board_token/region), sentence 2 = concrete fix.\
"""
    msg = client.messages.create(
        model=MODEL,
        max_tokens=180,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _result(name, status, raw, filtered, sample, diagnosis):
    return {
        "name": name,
        "status": status,        # ok | broken | filter | skip
        "raw_count": raw,
        "filtered_count": filtered,
        "sample_job": sample,
        "diagnosis": diagnosis,
    }


# ── Test direct sur les configs S1 ────────────────────────────────────────────

if __name__ == "__main__":
    from job_scrapper import WORKDAY_COMPANIES, SMARTRECRUITERS_COMPANIES, GREENHOUSE_COMPANIES

    target = sys.argv[1].lower() if len(sys.argv) > 1 else None

    test_configs = []
    for c in WORKDAY_COMPANIES:
        test_configs.append({"name": c["name"], "ats_type": "workday", "config": c})
    for c in SMARTRECRUITERS_COMPANIES:
        test_configs.append({"name": c["name"], "ats_type": "smartrecruiters", "config": c})
    for c in GREENHOUSE_COMPANIES:
        test_configs.append({"name": c["name"], "ats_type": "greenhouse", "config": c})

    if target:
        test_configs = [c for c in test_configs if target in c["name"].lower()]

    results = []
    for cfg in test_configs:
        r = validate(cfg, progress_cb=print)
        results.append(r)

    print(f"\n{'═'*50}")
    ok      = sum(1 for r in results if r["status"] == "ok")
    filt    = sum(1 for r in results if r["status"] == "filter")
    broken  = sum(1 for r in results if r["status"] == "broken")
    print(f"  ✅ {ok} OK  |  ⚠️  {filt} FILTER  |  ❌ {broken} BROKEN  |  {len(results)} total")
    print(f"{'═'*50}")
