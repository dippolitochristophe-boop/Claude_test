"""
Cache des résultats de scraping — TTL par société.

Évite de re-scraper les mêmes entreprises à chaque run.
Fichier : <project_root>/.jobs_cache.json
TTL par défaut : 6h
"""

import json
import os
import datetime

import threading

from agents.log import get_logger

logger = get_logger("jobs_cache")

_CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", ".jobs_cache.json")
DEFAULT_TTL_HOURS = 6
_lock = threading.Lock()


def _load() -> dict:
    if os.path.exists(_CACHE_FILE):
        with open(_CACHE_FILE, encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            return json.loads(content)
    return {}


def _save(cache: dict) -> None:
    with open(_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def get(company_name: str, ttl_hours: int = DEFAULT_TTL_HOURS) -> list[dict] | None:
    """Retourne les jobs en cache si frais, None sinon."""
    with _lock:
        cache = _load()
    key = company_name.lower().strip()
    entry = cache.get(key)
    if not entry:
        return None
    scraped_at = datetime.datetime.fromisoformat(entry["scraped_at"])
    age = datetime.datetime.now() - scraped_at
    if age.total_seconds() > ttl_hours * 3600:
        logger.debug("[cache] EXPIRED  %s  age=%.1fh", company_name, age.total_seconds() / 3600)
        return None
    logger.debug("[cache] HIT  %s  →  %d jobs  age=%.1fh",
                 company_name, len(entry["jobs"]), age.total_seconds() / 3600)
    return entry["jobs"]


def put(company_name: str, jobs: list[dict]) -> None:
    """Met en cache les jobs d'une société."""
    with _lock:
        cache = _load()
        key = company_name.lower().strip()
        cache[key] = {
            "jobs": jobs,
            "scraped_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "count": len(jobs),
        }
        _save(cache)
    logger.debug("[cache] SET  %s  →  %d jobs", company_name, len(jobs))


def invalidate(company_name: str) -> None:
    """Force re-scrape au prochain run."""
    with _lock:
        cache = _load()
        key = company_name.lower().strip()
        if key in cache:
            del cache[key]
            _save(cache)
    logger.debug("[cache] INVALIDATED  %s", company_name)
