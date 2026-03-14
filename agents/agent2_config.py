"""
Agent 2 — Config Generator

Pour une entreprise donnée, identifie son ATS et génère la config scraper exacte.
Suit rigoureusement la méthode définie dans ATS_RESEARCH_SPEC.md.

Input  : company name + domain (optionnel)
Output : dict {name, ats_type, config, confidence, notes}

Niveaux de confiance :
  confirmed — appel API/HTTP réussi, données reçues
  probable  — URL trouvée via Google mais API non testée (403 ou timeout)
  unknown   — seulement des mentions indirectes
  invalid   — 404 ou erreur confirmée après tentatives

Usage direct :
    python agents/agent2_config.py "Trafigura" "trafigura.com"
    python agents/agent2_config.py "Alpiq"
"""

import json
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.loop import run_agent
from agents.tools import TOOLS

# ── System prompt ──────────────────────────────────────────────────────────────
# C'est ici que tout se joue. Le prompt doit être prescriptif, non ambigu,
# et forcer l'agent à valider avant de conclure.

SYSTEM = """\
You are an expert in reverse-engineering corporate Applicant Tracking Systems (ATS).
Your mission: find the EXACT configuration to scrape job postings from a company's careers portal.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANDATORY 4-STEP METHOD — follow in strict order
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## STEP 1 — Identify the ATS via site: search

If the careers domain is unknown: first run web_search("{company} careers jobs apply site")
to find the careers URL, then proceed.

Run: web_search("site:{careers-domain} analyst OR trader OR engineer")
(Use a generic job keyword — not company-specific)

Read the URLs returned and match against this table:

| URL pattern                                              | ATS             |
|----------------------------------------------------------|-----------------|
| {tenant}.wd{n}.myworkdayjobs.com/{site}                  | Workday         |
| careers.smartrecruiters.com/{sr_id}                      | SmartRecruiters |
| jobs.smartrecruiters.com/{sr_id}/...                     | SmartRecruiters |
| boards.greenhouse.io/{token}                             | Greenhouse (US) |
| boards-api.eu.greenhouse.io/v1/boards/{token}            | Greenhouse (EU) |
| /careers/JobDetail/ or "taleo" in URL                    | Taleo           |
| {domain}/job/{title}/{id}-en_US/                         | Phenom People   |
| lever.co/{company} or jobs.lever.co/{company}            | Lever           |
| {domain}/en/careers/vacancies or /jobs/ (custom)         | HTML direct     |

If multiple URLs are returned, prefer the ones pointing directly to job listings.

## STEP 2 — Extract exact parameters from the indexed URL

**Workday** — from URL `{tenant}.wd{n}.myworkdayjobs.com/{site}/job/...`
  - `tenant` = first subdomain segment (before .wd)
  - `wd`     = the segment between tenant and .myworkdayjobs.com (wd1, wd3, wd5...)
  - `site`   = first path segment after the domain (NOT the job title slug)
  Example: "trafigura.wd3.myworkdayjobs.com/TrafiguraCareerSite/job/Geneva/Trader"
    → tenant=trafigura, wd=wd3, site=TrafiguraCareerSite

**SmartRecruiters** — from URL `careers.smartrecruiters.com/{sr_id}/...`
  - `sr_id` = exactly as it appears in the URL — CASE SENSITIVE
  Example: "careers.smartrecruiters.com/statkraft1/..." → sr_id=statkraft1

**Greenhouse** — from URL `boards.greenhouse.io/{board_token}/...`
  - `board_token` = path segment after /boards/ (not after /jobs/)
  - `region` = "eu" if URL is boards-api.eu.greenhouse.io, else "us"
  Example: "boards.greenhouse.io/glencore/jobs/123" → board_token=glencore, region=us

**HTML / Phenom / Custom**:
  - Find the page that LISTS jobs (not homepage, not "our values", not a specific job)
  - Phenom People: typically /search/?q= or /jobs/
  - Custom portals: /careers/vacancies-list, /en/jobs, /open-positions
  - `job_pattern` = common substring in ALL job listing links
    Example: all jobs are at /careers/job/123-title → job_pattern="/careers/job/"

## STEP 3 — MANDATORY VALIDATION (required before outputting confidence=confirmed)

**Workday**: POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
  Body: {"limit": 5, "offset": 0, "searchText": "analyst"}
  → HTTP 200 + non-empty "jobPostings" array = CONFIRMED ✅
  → HTTP 404 = wrong tenant or site name → go to Step 4
  → HTTP 200 + empty "jobPostings" = endpoint valid, retry with searchText=""
  → HTTP 403 = anti-bot, note as confidence=probable

**SmartRecruiters**: GET https://api.smartrecruiters.com/v1/companies/{sr_id}/postings?limit=5
  → HTTP 200 + non-empty "content" array = CONFIRMED ✅
  → HTTP 404 = wrong sr_id → go to Step 4

**Greenhouse**: GET https://boards-api.eu.greenhouse.io/v1/boards/{board_token}/jobs
  or           GET https://boards.greenhouse.io/v1/boards/{board_token}/jobs
  → HTTP 200 + non-empty "jobs" array = CONFIRMED ✅

**HTML/Phenom**: fetch the listing URL, check for links containing the job_pattern
  → Links found = CONFIRMED ✅
  → 403 = anti-bot (Playwright needed) → confidence=probable, note it

## STEP 4 — If Step 3 fails, try variants before giving up

**Workday**:
  - Try wd1, wd2, wd3, wd5 in order
  - Try alternate site names: "Careers", "External", "{Company}Careers", \
"{Company}CareerSite", "{Company}Jobs"
  - Search: web_search("site:{tenant}.myworkdayjobs.com") to find the exact site name

**SmartRecruiters**:
  - Try {Company}1, {company} (lowercase), {COMPANY}, {Company}-{Country}

**Greenhouse**:
  - Try region="us" if "eu" failed

**HTML**:
  - Try /search/?q=analyst, /careers/search, /vacancies/, /en/careers, /jobs/search

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. NEVER output confidence=confirmed without a successful Step 3 call
2. If company has multiple portals (e.g. Glencore coal + Glencore trading):
   always pick the General/External/Trading portal, NOT mining/coal/retail
3. If domain is null: start with a web_search to find it, don't skip
4. Max 8 turns — be efficient, don't over-search

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT — JSON object only, no prose after
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

```json
{
  "name": "Company",
  "ats_type": "workday|smartrecruiters|greenhouse|taleo|html|lever|unknown",
  "config": { ... },
  "confidence": "confirmed|probable|unknown|invalid",
  "notes": "How found, what was validated, any caveats"
}
```

Config shapes per ATS:
- Workday:         {"name": "X", "tenant": "x", "site": "XCareerSite", "wd": "wd3"}
- SmartRecruiters: {"name": "X", "sr_id": "CompanyId"}
- Greenhouse:      {"name": "X", "board_token": "token", "region": "eu"}
- HTML/Phenom:     {"name": "X", "type": "html", "pages": ["https://..."], \
"job_pattern": "/jobs/"}
- Taleo:           {"name": "X", "base": "https://companyname.taleo.net"}
- unknown:         {"name": "X"}
"""


# ── Main function ──────────────────────────────────────────────────────────────

def generate_config(company_name: str, domain: str = None, progress_cb=None) -> dict:
    """
    Génère la config ATS pour une entreprise.
    Retourne le dict de résultat (name, ats_type, config, confidence, notes).
    """
    domain_info = (
        f"Known careers domain: {domain}"
        if domain
        else "Careers domain: unknown — find it first with web_search"
    )

    user_msg = f"""\
Generate the scraper config for: **{company_name}**
{domain_info}

Follow the 4-step method exactly. Validate before concluding.
End your response with the JSON object only.\
"""

    if progress_cb:
        progress_cb(f"Agent 2 — {company_name}: identifying ATS...")

    result_text = run_agent(
        system=SYSTEM,
        user_message=user_msg,
        tools=TOOLS,
        max_turns=8,
        progress_cb=progress_cb,
    )

    result = _extract_json_object(result_text)
    if not result:
        result = {
            "name": company_name,
            "ats_type": "unknown",
            "config": {"name": company_name},
            "confidence": "unknown",
            "notes": f"Failed to parse agent output. Raw: {result_text[:300]}",
        }

    # Sauvegarder par entreprise
    safe_name = re.sub(r"[^a-z0-9]", "_", company_name.lower())
    path = f"/tmp/agent2_{safe_name}.json"
    with open(path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    if progress_cb:
        icons = {"confirmed": "✅", "probable": "🔧", "unknown": "❓", "invalid": "❌"}
        icon = icons.get(result.get("confidence", "unknown"), "❓")
        progress_cb(
            f"  {icon} {company_name} → {result.get('ats_type', '?')} "
            f"({result.get('confidence', '?')})"
        )

    return result


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_json_object(text: str) -> dict:
    """Extrait un objet JSON depuis la réponse de l'agent."""
    # Markdown code block
    match = re.search(r"```(?:json)?\s*(\{[\s\S]+?\})\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # JSON brut (chercher le dernier bloc JSON — plus fiable que le premier)
    matches = list(re.finditer(r"\{[\s\S]+\}", text))
    for m in reversed(matches):
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            continue

    return None


# ── Test direct (ground truth) ─────────────────────────────────────────────────

GROUND_TRUTH = {
    "Trafigura":   {"ats_type": "workday", "tenant": "trafigura", "site": "TrafiguraCareerSite", "wd": "wd3"},
    "Gunvor":      {"ats_type": "workday", "tenant": "gunvor", "wd": "wd3"},
    "Shell":       {"ats_type": "workday", "tenant": "shell"},
    "Statkraft":   {"ats_type": "smartrecruiters", "sr_id": "statkraft1"},
    "Glencore":    {"ats_type": "greenhouse", "board_token": "glencore", "region": "eu"},
}

if __name__ == "__main__":
    company = sys.argv[1] if len(sys.argv) > 1 else "Trafigura"
    domain = sys.argv[2] if len(sys.argv) > 2 else None

    result = generate_config(company, domain, progress_cb=print)

    print("\n=== RESULT ===")
    print(json.dumps(result, indent=2))

    # Comparer avec la ground truth si disponible
    if company in GROUND_TRUTH:
        gt = GROUND_TRUTH[company]
        print("\n=== GROUND TRUTH CHECK ===")
        print(f"Expected ATS: {gt['ats_type']}")
        print(f"Got ATS:      {result.get('ats_type')}")
        match = result.get("ats_type") == gt["ats_type"]
        print(f"Match: {'✅' if match else '❌'}")
