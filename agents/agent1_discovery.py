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

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

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
You are a specialized recruiter with deep expertise in energy, commodities, \
and financial trading sectors in Europe and globally.

Your task: find companies that hire profiles matching the user's description.

## Search strategy (run in this order)

1. "{sector} companies {locations} jobs" → main batch
2. "{role} hiring Europe {sector}" → find companies via active job postings
3. "commodity trading firms {locations} careers" → sweep for trading houses
4. If the profile mentions specific sub-sectors (renewables, gas, LNG, power...): \
add a targeted search

## What to include

- Trading houses: Trafigura, Vitol, Gunvor, Glencore, Mercuria, Freepoint, \
Castleton, Hartree, Marex, Koch Supply & Trading...
- Utilities with active trading desks: E.ON, Enel, Eni, Engie, EDF, RWE, \
Vattenfall, Alpiq, Axpo, BKW, Statkraft, Fortum, CEZ, Verbund...
- Energy majors with trading arms: Shell, BP, TotalEnergies, Equinor, Eni, \
Repsol, OMV...
- Prop trading / market making in energy: DRW, Optiver (energy desk), \
Jane Street (commodities), Flow Traders...
- Commodity merchants relevant to the profile
- Financial institutions with commodity/energy desks IF explicitly relevant

## What to exclude

- Pure retail energy suppliers (no trading desk)
- Consulting, audit, PE, VC firms
- Companies clearly outside the target geography
- Companies already in the EXISTING list

## Output rules

- Maximum {max_companies} companies — quality over quantity
- If domain is unknown: use null (never invent a domain)
- Write intermediate results to /tmp/agent1_companies.json as you discover them
- Final answer: a valid JSON array only, no prose

JSON format:
[
  {"name": "Company Name", "domain": "company.com", "hq": "City", "sector": "power trading"},
  ...
]
"""


# ── Main function ──────────────────────────────────────────────────────────────

def run_discovery(profile: dict, progress_cb=None) -> list:
    """
    Retourne une liste de dicts {name, domain, hq, sector}.
    """
    max_companies = profile.get("max_companies", 40)

    user_msg = f"""\
Find companies hiring profiles like this:

**Role**: {profile.get("role_description", "energy/commodity trader")}
**Sectors**: {", ".join(profile.get("sectors", ["energy trading", "power", "commodities"]))}
**Target locations**: {", ".join(profile.get("locations", ["London", "Geneva", "Amsterdam"]))}
**Max companies to return**: {max_companies}

**Already covered — exclude these**:
{", ".join(EXISTING_S1)}

Search the web thoroughly, then return a JSON array of companies.\
"""

    system = SYSTEM.replace("{max_companies}", str(max_companies))

    if progress_cb:
        progress_cb("Agent 1 — Discovery: searching for companies...")

    result_text = run_agent(
        system=system,
        user_message=user_msg,
        tools=SEARCH_ONLY_TOOLS,
        max_turns=10,
        progress_cb=progress_cb,
    )

    companies = _extract_json_list(result_text)

    if progress_cb:
        progress_cb(f"Agent 1 done — {len(companies)} companies found")

    # Sauvegarder
    with open("/tmp/agent1_companies.json", "w") as f:
        json.dump(companies, f, indent=2, ensure_ascii=False)

    return companies


# ── Helpers ────────────────────────────────────────────────────────────────────

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
    companies = run_discovery(profile, progress_cb=print)
    print(f"\n{'─'*40}")
    print(f"Result: {len(companies)} companies")
    for c in companies:
        print(f"  - {c['name']} ({c.get('domain') or 'no domain'}) — {c.get('hq', '?')}")
