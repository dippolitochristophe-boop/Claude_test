"""
Gestion des profils utilisateur.

Un profil pilote l'intégralité du scraper :
  - keywords_include → requêtes API + filtre de pertinence des titres + scoring
  - keywords_exclude → exclusions (fuel oil, coal, intern...)
  - locations        → buckets géographiques affichés
  - sectors + role_description → guidage d'Agent 1 (Discovery)

Usage :
    from profiles import load_profile, save_profile

    profile = load_profile()          # charge profile.json ou retourne le défaut
    save_profile(profile)             # sauvegarde dans profile.json
"""

import json
import os
from datetime import datetime

PROFILE_FILE = "profile.json"

# ── Profil par défaut ──────────────────────────────────────────────────────────
# Correspond exactement au comportement hardcodé actuel de S1.
# Si aucun profile.json n'existe, le scraper se comporte comme avant.

DEFAULT_PROFILE = {
    "name": "Default",
    "role_description": "energy/power trader, European markets",
    "sectors": ["energy trading", "power", "renewables", "gas", "commodities"],
    "locations": ["London", "Geneva", "Amsterdam", "Paris", "Frankfurt"],
    "seniority": "senior",

    # Mots-clés inclus : remplacent SEARCH_QUERIES, DIRECT_MATCH, DOMAIN_POWER × ROLE_KEYWORDS
    "keywords_include": [
        # Titres exacts (ex-DIRECT_MATCH)
        "power trader", "energy trader", "power market", "intraday trading",
        "front office", "portfolio manager", "head of trading", "chief risk officer",
        "market risk", "asset optimizer", "trading analyst", "risk officer",
        "algo trading", "algorithmic trading", "ppa manager", "ppa sales",
        "originator",
        # Combos domaine × rôle (ex-DOMAIN_POWER × ROLE_KEYWORDS)
        "renewables trader", "bess trading", "power risk", "energy origination",
    ],

    # Mots-clés exclus : remplacent DOMAIN_EXCLUDE
    "keywords_exclude": [
        "fuel oil", "crude oil", "oil trader", "lng trader", "bunker",
        "nat gas scheduler", "lpg", "middle distillate", "shipping", "site manager", "coal",
    ],

    # Options scraper
    "max_companies": 40,
    "updated_at": None,
}


# ── API publique ───────────────────────────────────────────────────────────────

def load_profile(path: str = PROFILE_FILE) -> dict:
    """
    Charge le profil depuis `path`.
    Si le fichier n'existe pas ou est invalide, retourne DEFAULT_PROFILE.
    Les clés manquantes sont complétées par les valeurs du défaut.
    """
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            # Merger avec le défaut pour les clés manquantes
            profile = {**DEFAULT_PROFILE, **saved}
            return profile
        except (json.JSONDecodeError, IOError):
            pass
    return dict(DEFAULT_PROFILE)


def save_profile(profile: dict, path: str = PROFILE_FILE) -> None:
    """Sauvegarde le profil dans `path` avec timestamp."""
    profile = dict(profile)
    profile["updated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


def profile_display_name(profile: dict) -> str:
    """Retourne un label court pour affichage (CLI, rapport HTML)."""
    name = profile.get("name", "")
    desc = profile.get("role_description", "")
    if name and name != "Default":
        return f"{name} — {desc}" if desc else name
    return desc or "Custom profile"
