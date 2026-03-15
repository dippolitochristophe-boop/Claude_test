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
You are a specialized recruiter for energy/commodities/trading sectors in Europe.
Task: find companies hiring profiles like the one described.

## Search strategy (in order)
1. "{sector} trading companies {locations} jobs"
2. "{role} hiring Europe {sector}"
3. "commodity trading firms {locations} careers"
4. Add a targeted search for any specific sub-sector mentioned (renewables, gas, LNG...)

## Include
- Trading houses (Vitol, Gunvor, Glencore, Mercuria, Hartree, Marex, Freepoint, Koch...)
- Utilities with trading desks (E.ON, Enel, RWE, Vattenfall, Alpiq, Axpo, Statkraft, Fortum...)
- Energy majors (Shell, BP, TotalEnergies, Equinor, Eni, Repsol, OMV...)
- Prop/market-making in energy (DRW, Optiver, Flow Traders, Jane Street commodities...)
- Commodity merchants matching the profile sector

## Exclude
- Pure retail energy (no trading desk)
- Consulting, audit, PE/VC
- Outside target geography
- Companies already in the EXISTING list

## Rules
- Max {max_companies} companies — quality over quantity
- Unknown domain → null (never invent)
- Final answer: valid JSON array only, no prose

Format: [{"name": "...", "domain": "...", "hq": "...", "sector": "..."}, ...]
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

    system = SYSTEM.replace("{max_companies}", str(max_companies))

    if progress_cb:
        progress_cb("Agent 1 — Discovery: searching for companies...")

    result_text = run_agent(
        system=system,
        user_message=user_msg,
        tools=SEARCH_ONLY_TOOLS,
        max_turns=10,
        max_tokens=1500,
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
