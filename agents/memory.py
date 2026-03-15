"""
Mémoire persistante pour Agent 2 — apprend au fil des runs.

Chaque config validée (pipeline.py) écrit ici :
  - la winning_query DDG qui a trouvé l'URL ATS
  - l'URL exacte avec tenant/wd/site encodés
  - le config dict final

Au prochain run, Agent 2 reçoit ces exemples dans son SYSTEM prompt :
il sait ce qui marche pour CE secteur, pas besoin de partir de zéro.
"""

import json
import os
import datetime

MEMORY_FILE = os.path.join(os.path.dirname(__file__), "memory.json")

# Max exemples injectés dans le prompt (garder sous ~350 tokens)
MAX_SUCCESSES_PER_ATS = 5
MAX_FAILURES = 4


def load() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"successes": [], "failures": []}


def _save(m: dict):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)


def add_success(company: str, ats_type: str, winning_query: str,
                url_found: str, config: dict, raw_count: int):
    """Appelé par pipeline.py après validation Agent 3 ok ou filter."""
    m = load()
    m["successes"] = [s for s in m["successes"] if s["company"] != company]
    m["successes"].append({
        "company": company,
        "ats_type": ats_type,
        "winning_query": winning_query,
        "url_found": url_found,
        "config": config,
        "raw_count": raw_count,
        "date": datetime.date.today().isoformat(),
    })
    _save(m)


def add_failure(company: str, tried_queries: list, reason: str):
    """Appelé par pipeline.py après broken ou unknown."""
    m = load()
    m["failures"] = [f for f in m["failures"] if f["company"] != company]
    m["failures"].append({
        "company": company,
        "tried_queries": tried_queries,
        "reason": reason,
        "date": datetime.date.today().isoformat(),
    })
    _save(m)


def build_prompt_section() -> str:
    """
    Retourne un bloc texte (~200-350 tokens) injecté dans le SYSTEM d'Agent 2.
    Montre les patterns qui ont marché + les failures connues.
    """
    m = load()
    successes = m.get("successes", [])
    failures  = m.get("failures", [])

    if not successes and not failures:
        return ""

    lines = ["\n## MEMORY — patterns from past validated runs\n"]

    # Grouper par ATS type
    by_ats: dict[str, list] = {}
    for s in successes:
        by_ats.setdefault(s["ats_type"], []).append(s)

    for ats_type, items in by_ats.items():
        lines.append(f"### {ats_type.upper()}")
        for it in items[:MAX_SUCCESSES_PER_ATS]:
            q  = it.get("winning_query", "")
            url = it.get("url_found", "")
            cfg = it.get("config", {})
            lines.append(f'- query: `{q}`')
            if url:
                lines.append(f'  url: `{url}`')
            lines.append(f'  config: {json.dumps(cfg, ensure_ascii=False)}')
        lines.append("")

    if failures:
        lines.append("### KNOWN FAILURES — don't waste turns on these")
        for f in failures[:MAX_FAILURES]:
            tried = ", ".join(f'`{q}`' for q in f.get("tried_queries", []) if q)
            reason = f.get("reason", "")
            tried_str = f" (tried: {tried})" if tried else ""
            lines.append(f'- {f["company"]}: {reason}{tried_str}')

    return "\n".join(lines)
