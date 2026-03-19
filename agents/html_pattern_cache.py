"""
html_pattern_cache.py — Cache persistant des job_pattern découverts par LLM.

Principe :
  - Run 1 (nouveau site) : LLM Haiku analyse le HTML → découvre job_pattern
  - Le pattern est sauvegardé ici (tempdir/html_patterns.json)
  - Run 2+ : Python lit le cache → 0 token LLM

Le cache est invalidé implicitement : si le pattern sauvegardé retourne 0 jobs,
smart_scrape_site() retombe sur le LLM qui re-discover et met à jour le cache.
"""

import datetime
import json
import os
import tempfile

from agents.log import get_logger

logger = get_logger("html_pattern_cache")

CACHE_FILE = os.path.join(tempfile.gettempdir(), "html_patterns.json")


def _load() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def get(site_name: str) -> str | None:
    """Retourne le job_pattern mis en cache pour ce site, ou None."""
    return _load().get(site_name.lower(), {}).get("job_pattern")


def put(site_name: str, job_pattern: str) -> None:
    """Sauvegarde le job_pattern découvert par LLM pour ce site."""
    cache = _load()
    cache[site_name.lower()] = {
        "job_pattern": job_pattern,
        "date": datetime.date.today().isoformat(),
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    logger.debug("html_pattern_cache: saved job_pattern=%r for %s", job_pattern, site_name)
