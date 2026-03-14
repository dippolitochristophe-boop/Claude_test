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

# Fix Unicode output on Windows (cp1252 → utf-8)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from agents.loop import run_agent
from agents.tools import TOOLS

# ── System prompt ──────────────────────────────────────────────────────────────
# C'est ici que tout se joue. Le prompt doit être prescriptif, non ambigu,
# et forcer l'agent à valider avant de conclure.

SYSTEM = """\
You are an expert in reverse-engineering corporate Applicant Tracking Systems (ATS).
Mission: find the EXACT config to scrape job postings from a company's careers portal.

## STEP 1 — Identify ATS via site: search

If careers domain unknown: web_search("{company} careers jobs apply") first.
Then: web_search("site:{careers-domain} analyst OR trader OR engineer")

Match URLs to ATS:
- {tenant}.wd{n}.myworkdayjobs.com/{site} → Workday
- careers.smartrecruiters.com/{sr_id} or jobs.smartrecruiters.com/{sr_id} → SmartRecruiters
- boards.greenhouse.io/{token} → Greenhouse (us) / boards-api.eu.greenhouse.io → Greenhouse (eu)
- /careers/JobDetail/ or "taleo" in URL → Taleo
- {domain}/job/{title}/{id}-en_US/ → Phenom People
- lever.co/{company} or jobs.lever.co/{company} → Lever
- other → HTML direct

## STEP 2 — Extract exact parameters

Workday: tenant=first subdomain, wd=wd1/wd3/wd5, site=first path segment
  e.g. trafigura.wd3.myworkdayjobs.com/TrafiguraCareerSite → tenant=trafigura, wd=wd3, site=TrafiguraCareerSite
SmartRecruiters: sr_id=exactly as in URL (CASE SENSITIVE)
Greenhouse: board_token=segment after /boards/, region=eu or us
HTML/Phenom: pages=[listing URL], job_pattern=common substring in all job links

## STEP 3 — MANDATORY VALIDATION (required for confidence=confirmed)

Workday: POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs {"limit":5,"offset":0,"searchText":"analyst"}
  200 + non-empty jobPostings = ✅ | 404 = wrong params → Step 4 | 403 = probable
SmartRecruiters: GET https://api.smartrecruiters.com/v1/companies/{sr_id}/postings?limit=5
  200 + non-empty content = ✅ | 404 = wrong sr_id → Step 4
Greenhouse: GET https://boards-api.eu.greenhouse.io/v1/boards/{token}/jobs (try us if eu fails)
  200 + non-empty jobs = ✅
HTML/Phenom: fetch listing URL, check links contain job_pattern → ✅ | 403 = probable

## STEP 4 — Variants if Step 3 fails

Workday: try wd1/wd2/wd3/wd5, try site names: Careers/External/{Company}Careers/{Company}CareerSite
SmartRecruiters: try {Company}1, {company}, {COMPANY}, {Company}-{Country}
HTML: try /search/?q=analyst, /vacancies/, /en/careers, /jobs/search

## Critical rules
1. NEVER confidence=confirmed without successful Step 3
2. Multiple portals → pick General/External/Trading (not mining/retail/IT)
3. Null domain → web_search first
4. Max 8 turns — be efficient

## Output: JSON object only, no prose

{"name":"Company","ats_type":"workday|smartrecruiters|greenhouse|taleo|html|lever|unknown","config":{...},"confidence":"confirmed|probable|unknown|invalid","notes":"..."}

Config shapes:
- Workday: {"name":"X","tenant":"x","site":"XCareerSite","wd":"wd3"}
- SmartRecruiters: {"name":"X","sr_id":"CompanyId"}
- Greenhouse: {"name":"X","board_token":"token","region":"eu"}
- HTML: {"name":"X","type":"html","pages":["https://..."],"job_pattern":"/jobs/"}
- Taleo: {"name":"X","base":"https://x.taleo.net"}
- unknown: {"name":"X"}
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
        max_tokens=700,
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
