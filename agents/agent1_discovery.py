"""
Agent 1 — Discovery

Trouve les entreprises pertinentes pour un profil utilisateur donné.
Input  : dict profil (role_description, sectors, locations, max_companies)
Output : liste de dicts {name, domain, hq, sector}

Usage direct :
    python agents/agent1_discovery.py
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
from agents.tools import SEARCH_ONLY_TOOLS

# ── Entreprises déjà couvertes par S1 — exclure pour éviter les doublons ───────

EXISTING_S1 = [
    "Trafigura", "Gunvor", "Shell", "BP", "Equinor", "EDF Trading", "Centrica",
    "CCI", "RWE", "Uniper", "ENGIE", "Glencore", "Statkraft", "InCommodities",
    "Petroineos", "Orsted", "SEFE", "Vattenfall", "Vitol", "Alpiq", "Axpo",
    "BKW", "Mercuria", "TotalEnergies", "Macquarie",
]

# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM = """\
You are a company discovery agent. Find companies that hire profiles like the one described.

## Two-phase search strategy

### Phase 1 — Broad discovery (3–4 searches, one per turn)
1. "top [sector] companies [locations]"             → major players
2. "[known company in sector] competitors"           → peer group (pick a company you know in this sector)
3. "list [sector] companies wikipedia"               → curated encyclopedia list
4. "[sector] employers [country]"                    → geographic angle

Replace [sector], [locations], [country] with ACTUAL values from the profile.

### Phase 2 — Gap filling (1–2 searches max)
After Phase 1, if important niches are missing (e.g. prop trading, utilities, smaller firms),
run 1–2 targeted searches to fill gaps.

## Rules
- One tool call per turn maximum.
- Always use max_results=5 in every web_search call.
- Extract ALL company names mentioned in snippets — not just the first result.
- Do not return companies in the exclusion list (if provided).
- Respect the max_companies limit from the user message.
- If a domain is unknown, set "domain": null — never invent one.
- Final answer: valid JSON array only, no prose before or after it.

## Output format
[{"name": "...", "domain": "...", "hq": "...", "sector": "..."}, ...]
- name: official company name
- domain: primary web domain (e.g. "trafigura.com") or null if unknown
- hq: headquarters city + country (e.g. "Geneva, Switzerland") or null
- sector: the specific sector matching this profile
"""


# ── Main function ──────────────────────────────────────────────────────────────

def run_discovery(profile: dict, exclude_list: list = None, progress_cb=None) -> list:
    """
    Retourne une liste de dicts {name, domain, hq, sector}.
    exclude_list : liste de noms à exclure (None = aucune exclusion, mode S2 full)
    """
    max_companies = profile.get("max_companies", 40)
    actual_exclude = exclude_list if exclude_list is not None else []

    exclude_section = (
        f"\n**Already covered — exclude these**:\n{', '.join(actual_exclude)}\n"
        if actual_exclude else ""
    )

    user_msg = f"""\
Find companies hiring profiles like this:

**Role**: {profile.get("role_description", "energy/commodity trader")}
**Sectors**: {", ".join(profile.get("sectors", ["energy trading", "power", "commodities"]))}
**Target locations**: {", ".join(profile.get("locations", ["London", "Geneva", "Amsterdam"]))}
**Max companies to return**: {max_companies}
{exclude_section}
Search the web thoroughly, then return a JSON array of companies.\
"""

    system = SYSTEM

    if progress_cb:
        progress_cb("Agent 1 — Discovery: searching for companies...")

    result_text = run_agent(
        system=system,
        user_message=user_msg,
        tools=SEARCH_ONLY_TOOLS,
        max_turns=6,
        max_tokens=800,
        progress_cb=progress_cb,
    )

    companies = _deduplicate(_extract_json_list(result_text))

    if progress_cb:
        progress_cb(f"Agent 1 done — {len(companies)} companies found")

    # Sauvegarder
    with open(os.path.join(tempfile.gettempdir(), "agent1_companies.json"), "w") as f:
        json.dump(companies, f, indent=2, ensure_ascii=False)

    return companies


# ── Helpers ────────────────────────────────────────────────────────────────────

def _deduplicate(companies: list) -> list:
    """Remove duplicate companies by normalized name (case-insensitive, stripped)."""
    seen = set()
    result = []
    for c in companies:
        key = c.get("name", "").lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(c)
    return result


def _extract_json_list(text: str) -> list:
    """Extrait une liste JSON depuis le texte de réponse de l'agent."""
    # Markdown code block
    match = re.search(r"```(?:json)?\s*(\[[\s\S]+?\])\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # JSON brut dans le texte
    match = re.search(r"\[[\s\S]+\]", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return []


# ── Test direct ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    profile = {
        "role_description": "power/energy trader, 5 years experience, European markets",
        "sectors": ["power trading", "renewable energy", "gas trading"],
        "locations": ["London", "Geneva", "Amsterdam", "Paris"],
        "max_companies": 20,
    }
    companies = run_discovery(profile, exclude_list=EXISTING_S1, progress_cb=print)
    print(f"\n{'─'*40}")
    print(f"Result: {len(companies)} companies")
    for c in companies:
        print(f"  - {c['name']} ({c.get('domain') or 'no domain'}) — {c.get('hq', '?')}")
