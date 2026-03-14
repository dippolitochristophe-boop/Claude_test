"""
Pipeline — Orchestrateur Agent 1 → Agent 2 → Agent 3

Flux complet :
  1. Agent 1 découvre les entreprises pertinentes pour le profil
  2. Agent 2 génère les configs ATS (2 en parallèle max)
  3. Agent 3 valide chaque config
  4. Lance le scraper S1 avec les configs validées + les configs hardcodées

Usage :
    python agents/pipeline.py                     # profil de test
    python agents/pipeline.py --profile-file profile.json
    python agents/pipeline.py --agent2-only Axpo  # tester Agent 2 seul
    python agents/pipeline.py --validate-s1       # valider toutes les configs S1 existantes
"""

import argparse
import json
import os
import sys
import time
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.agent1_discovery import run_discovery
from agents.agent2_config import generate_config
from agents.agent3_validator import validate


# ── Orchestrateur principal ────────────────────────────────────────────────────

def run_pipeline(profile: dict, progress_cb=None) -> dict:
    """
    Exécute le pipeline complet Agent 1 → 2 → 3.
    Retourne {validated_configs, broken, stats}.
    """
    log = progress_cb or print
    start = time.time()

    log("═" * 60)
    log("🚀 Pipeline démarré")
    log("═" * 60)

    # ── ÉTAPE 1 : Discovery ──────────────────────────────────────────────────
    log("\n📍 ÉTAPE 1 — Découverte des entreprises")
    companies = run_discovery(profile, progress_cb=log)

    if not companies:
        log("❌ Agent 1 n'a trouvé aucune entreprise — pipeline arrêté")
        return {"validated_configs": [], "broken": [], "stats": {"total": 0}}

    log(f"\n→ {len(companies)} entreprises à configurer")

    # ── ÉTAPE 2+3 : Config + Validation ─────────────────────────────────────
    log("\n📍 ÉTAPE 2+3 — Configuration et validation")

    validated_configs = []
    broken = []

    # Traitement par batch de 2 (respecte les rate limits Anthropic)
    batch_size = 2
    batches = [companies[i:i+batch_size] for i in range(0, len(companies), batch_size)]

    for batch_idx, batch in enumerate(batches):
        log(f"\n  Batch {batch_idx + 1}/{len(batches)} — {[c['name'] for c in batch]}")

        if len(batch) == 1:
            # Pas de parallélisme pour les batches d'une seule entreprise
            results = [_process_company(batch[0], log)]
        else:
            # 2 en parallèle
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {
                    executor.submit(_process_company, company, log): company
                    for company in batch
                }
                results = []
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as e:
                        company = futures[future]
                        log(f"  ❌ {company['name']} — unexpected error: {e}")
                        broken.append({"company": company, "error": str(e)})

        for r in results:
            if r is None:
                continue
            if r.get("validation_status") == "ok":
                validated_configs.append(r["config"])
                log(f"  ✅ {r['name']} ajouté ({r['raw_count']} offres)")
            elif r.get("validation_status") == "filter":
                # Config valide mais 0 offre pertinente pour CE profil
                # On l'ajoute quand même — peut servir pour d'autres profils
                validated_configs.append(r["config"])
                log(f"  ⚠️  {r['name']} — config OK, 0 offre pertinente pour ce profil")
            else:
                broken.append(r)
                log(f"  ❌ {r['name']} — {r.get('diagnosis', r.get('validation_status', 'broken'))}")

        # Pause entre batches pour éviter les rate limits
        if batch_idx < len(batches) - 1:
            time.sleep(1)

    # ── Résumé ───────────────────────────────────────────────────────────────
    elapsed = round(time.time() - start, 1)
    stats = {
        "total": len(companies),
        "validated": len(validated_configs),
        "broken": len(broken),
        "elapsed_s": elapsed,
    }

    log("\n" + "═" * 60)
    log(f"✅ Pipeline terminé en {elapsed}s")
    log(f"   {stats['validated']} configs validées | {stats['broken']} échouées | {stats['total']} total")
    log("═" * 60)

    output = {"validated_configs": validated_configs, "broken": broken, "stats": stats}
    with open("/tmp/pipeline_result.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    return output


def _process_company(company: dict, log) -> dict:
    """
    Traite une seule entreprise : Agent 2 (config) + Agent 3 (validation).
    Retourne un dict de résultat normalisé.
    """
    name = company["name"]
    log(f"\n  [{name}]")

    # Agent 2 — Config Generator
    agent2_result = generate_config(
        name,
        company.get("domain"),
        progress_cb=log,
    )

    # Skip si config invalide
    if agent2_result.get("confidence") == "invalid":
        return {
            "name": name,
            "validation_status": "invalid",
            "config": None,
            "diagnosis": agent2_result.get("notes", "invalid config"),
        }

    # Agent 3 — Validator
    agent3_result = validate(agent2_result, progress_cb=log)

    return {
        "name": name,
        "ats_type": agent2_result.get("ats_type"),
        "config": agent2_result.get("config"),
        "confidence": agent2_result.get("confidence"),
        "validation_status": agent3_result["status"],
        "raw_count": agent3_result["raw_count"],
        "sample_job": agent3_result.get("sample_job"),
        "diagnosis": agent3_result.get("diagnosis"),
    }


# ── Mode validate-s1 : healthcheck sur les configs S1 existantes ───────────────

def validate_s1_configs(progress_cb=None) -> dict:
    """
    Lance Agent 3 sur toutes les configs hardcodées de S1.
    Équivalent d'un healthcheck intelligent avec diagnostic Claude.
    """
    from job_scrapper import (
        WORKDAY_COMPANIES, SMARTRECRUITERS_COMPANIES,
        GREENHOUSE_COMPANIES, TALEO_SITES,
    )
    log = progress_cb or print

    log("═" * 60)
    log("🔍 Healthcheck S1 — validation de toutes les configs existantes")
    log("═" * 60)

    all_configs = (
        [{"name": c["name"], "ats_type": "workday",         "config": c} for c in WORKDAY_COMPANIES] +
        [{"name": c["name"], "ats_type": "smartrecruiters", "config": c} for c in SMARTRECRUITERS_COMPANIES] +
        [{"name": c["name"], "ats_type": "greenhouse",      "config": c} for c in GREENHOUSE_COMPANIES] +
        [{"name": c["name"], "ats_type": "taleo",           "config": c} for c in TALEO_SITES]
    )

    results = []
    for cfg in all_configs:
        r = validate(cfg, progress_cb=log)
        results.append(r)

    ok     = sum(1 for r in results if r["status"] == "ok")
    filt   = sum(1 for r in results if r["status"] == "filter")
    broken = sum(1 for r in results if r["status"] == "broken")

    log(f"\n{'═'*60}")
    log(f"  ✅ {ok} OK  |  ⚠️  {filt} FILTER  |  ❌ {broken} BROKEN  |  {len(results)} total")
    log(f"{'═'*60}")

    with open("/tmp/s1_healthcheck.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    return {"results": results, "ok": ok, "filter": filt, "broken": broken}


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job scraper pipeline")
    parser.add_argument("--profile-file", default=None, help="JSON profile file")
    parser.add_argument("--agent2-only", default=None, metavar="COMPANY",
                        help="Run Agent 2 only on a specific company")
    parser.add_argument("--validate-s1", action="store_true",
                        help="Validate all existing S1 configs (healthcheck)")
    args = parser.parse_args()

    if args.validate_s1:
        validate_s1_configs()

    elif args.agent2_only:
        result = generate_config(args.agent2_only, progress_cb=print)
        print("\n=== RESULT ===")
        print(json.dumps(result, indent=2))

        # Lancer Agent 3 directement si config trouvée
        if result.get("confidence") not in ("unknown", "invalid"):
            print("\n=== VALIDATION ===")
            v = validate(result, progress_cb=print)
            print(json.dumps(v, indent=2))

    else:
        # Pipeline complet
        if args.profile_file and os.path.exists(args.profile_file):
            with open(args.profile_file) as f:
                profile = json.load(f)
        else:
            # Profil de test par défaut
            profile = {
                "role_description": "energy/power trader, 5 years experience, European markets",
                "sectors": ["energy trading", "power", "renewables", "gas"],
                "locations": ["London", "Geneva", "Amsterdam", "Paris"],
                "max_companies": 10,  # Petit pour le test initial
            }

        result = run_pipeline(profile)
        print(f"\nValidated configs: {len(result['validated_configs'])}")
        print(f"Results saved to: /tmp/pipeline_result.json")
