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
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Fix Unicode output on Windows (cp1252 → utf-8)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from agents.loop import run_agent
from agents.tools import TOOLS, web_search
from agents.memory import build_prompt_section, add_success, add_failure

# ── System prompt ──────────────────────────────────────────────────────────────
# C'est ici que tout se joue. Le prompt doit être prescriptif, non ambigu,
# et forcer l'agent à valider avant de conclure.

SYSTEM = """\
You are an expert in reverse-engineering corporate Applicant Tracking Systems (ATS).
Mission: find the EXACT config to scrape job postings from a company's careers portal.

## CRITICAL RULES
- ONE tool call per turn. STRICTLY execute steps a→e in alphabetical order. NO other searches.
- Stop at first hit and go to STEP 2. Never add extra searches between steps.

## STEP 1 — Find exact ATS URL (execute a→e in strict order, one per turn)

a) web_search("{company} site:myworkdayjobs.com")
   → Hit: extract tenant/wd/site DIRECTLY from URL — NEVER guess
     e.g. "trafigura.wd3.myworkdayjobs.com/TrafiguraCareerSite"
          → tenant=trafigura  wd=wd3  site=TrafiguraCareerSite → STEP 2

b) web_search("{company} site:jobs.smartrecruiters.com")
   → Hit: sr_id = path segment after domain (CASE SENSITIVE) → STEP 2

c) web_search("{company} site:boards.greenhouse.io")
   → Hit: board_token = slug after /boards/ → STEP 2

d) web_search("{company} site:jobs.lever.co")
   → Hit: lever_id = slug after / → STEP 2

e) web_search("{company} site:ashbyhq.com")
   → Hit: slug → STEP 2

If a–e all return 0 results:
   web_fetch("https://careers.{company}.com") or "https://{company}.com/careers"
   → Check "ATS URLS FOUND:" line in response — tool pre-scans raw HTML for ATS patterns
   → "JSON-LD JobPostings found:" line = structured data for Google indexing

If web_fetch also finds nothing:
   → Return ats_type="unknown" — Python will handle the LinkedIn fallback automatically

## STEP 2 — Extract exact parameters

Workday: tenant=first subdomain, wd=wd1/wd3/wd5, site=first path segment (from real URL only)
SmartRecruiters: sr_id=exactly as in URL (CASE SENSITIVE)
Greenhouse:
  URL boards.greenhouse.io/{token}         → region=us
  URL job-boards.eu.greenhouse.io/{token}  → region=eu
  board_token = exact slug from URL
HTML/Phenom/Lever: pages=[listing URL], job_pattern=common substring in ≥3 job links

## STEP 3 — MANDATORY VALIDATION (required for confidence=confirmed)

Workday: POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
  Body: {"limit":5,"offset":0,"searchText":""}   ← EMPTY STRING, not "analyst"
  200 + non-empty jobPostings = ✅ | 404 = wrong params → Step 4 | 403 = probable

SmartRecruiters: GET https://api.smartrecruiters.com/v1/companies/{sr_id}/postings?limit=5
  200 + non-empty content = ✅ | 404 = wrong sr_id → Step 4

Greenhouse EU: GET https://boards-api.eu.greenhouse.io/v1/boards/{token}/jobs
Greenhouse US: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs
  Try EU first. 404 or empty → try US. 200 + non-empty jobs = ✅

HTML: web_fetch({listing_url}) → count <a href> links containing job_pattern
  ≥3 matching links = ✅ | <3 → refine pattern (Step 4) | 403 = probable

Lever: GET https://api.lever.co/v0/postings/{lever_id}?mode=json
  200 + non-empty array = ✅

## STEP 4 — Variants if Step 3 fails

Workday 404:
  → wd wrong: try wd1 then wd3 (covers 90% of companies)
  → site name wrong: re-run Step 1a — the real URL is indexed by Google
  → tenant wrong: try {company}-group, {company}europe, abbreviated name

SmartRecruiters 404: try {Company}1, {company}, {Company}-{Country}

HTML <3 matches: try /jobs/, /careers/, /vacancies/, /en/careers/, /postings/

## Critical rules
1. NEVER confidence=confirmed without successful Step 3
2. Multiple portals → pick General/External/Trading (not mining/retail/IT)
3. Null domain → run Step 1 searches directly with company name
4. Max 6 turns — be efficient: exhaust all site: searches (a–f) before web_fetch

## Output: JSON object only, no prose

{"name":"Company","ats_type":"workday|smartrecruiters|greenhouse|taleo|html|lever|linkedin|unknown","config":{...},"confidence":"confirmed|probable|unknown|invalid","winning_query":"the site: search that found the URL","notes":"..."}

Config shapes:
- Workday: {"name":"X","tenant":"x","site":"XCareerSite","wd":"wd3"}
- SmartRecruiters: {"name":"X","sr_id":"CompanyId"}
- Greenhouse: {"name":"X","board_token":"token","region":"eu"}
- HTML: {"name":"X","type":"html","pages":["https://..."],"job_pattern":"/jobs/"}
- Taleo: {"name":"X","base":"https://x.taleo.net"}
- Lever: {"name":"X","lever_id":"company-slug"}
- LinkedIn: {"name":"X","linkedin_url":"https://www.linkedin.com/company/{slug}/jobs/"}
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
        system=SYSTEM + build_prompt_section(),
        user_message=user_msg,
        tools=TOOLS,
        max_turns=7,
        max_tokens=800,
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

    # Fallback Python : si ATS toujours inconnu → construire URL LinkedIn depuis le nom
    # Plus fiable que de demander au LLM (évite de brûler un turn supplémentaire)
    if result.get("ats_type") == "unknown":
        # Chercher le slug exact de la page entreprise LinkedIn
        search_result = web_search(f"site:linkedin.com/company {company_name}", max_results=3)
        slug_match = re.search(r'linkedin\.com/company/([\w-]+)', search_result)
        if slug_match:
            slug = slug_match.group(1)
            linkedin_url = f"https://www.linkedin.com/company/{slug}/jobs/"
        else:
            keywords = company_name.replace(" ", "+")
            linkedin_url = f"https://www.linkedin.com/jobs/search/?keywords={keywords}"
        result["ats_type"] = "linkedin"
        result["config"] = {"name": company_name, "linkedin_url": linkedin_url}
        result["confidence"] = "probable"
        result["notes"] = f"No public ATS found — {linkedin_url}"

    # Sauvegarder par entreprise
    safe_name = re.sub(r"[^a-z0-9]", "_", company_name.lower())
    path = os.path.join(tempfile.gettempdir(), f"agent2_{safe_name}.json")
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
