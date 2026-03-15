"""
Pipeline — Orchestrateur Agent 1 → Agent 2 → Agent 3

Usage :
    python agents/pipeline.py                                        # pipeline complet
    python agents/pipeline.py --profile-file profile.json           # profil custom
    python agents/pipeline.py --companies "Trafigura,Shell,Gunvor"  # bypass Agent 1
"""

import argparse
import json
import os
import sys
import tempfile
import time
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.agent1_discovery import run_discovery
from agents.agent2_config import generate_config
from agents.agent3_validator import validate
from agents import memory
from profiles import load_profile, save_profile


# ── Orchestrateur principal ────────────────────────────────────────────────────

def run_pipeline(profile: dict, target_companies: list = None, progress_cb=None) -> dict:
    """
    Exécute le pipeline complet Agent 1 → 2 → 3.
    target_companies : liste de noms → bypass Agent 1
    Retourne {validated_configs, broken, stats}.
    """
    log = progress_cb or print
    start = time.time()

    log("═" * 60)
    log("🚀 Pipeline démarré")
    log("═" * 60)

    # ── ÉTAPE 1 : Discovery (skippée si target_companies fourni) ─────────────
    if target_companies:
        companies = [{"name": n.strip(), "domain": None} for n in target_companies]
        log(f"\n📍 Mode ciblé — {len(companies)} entreprises : {[c['name'] for c in companies]}")
    else:
        log("\n📍 ÉTAPE 1 — Découverte des entreprises")
        companies = run_discovery(profile, exclude_list=None, progress_cb=log)

    if not companies:
        log("❌ Agent 1 n'a trouvé aucune entreprise — pipeline arrêté")
        return {"validated_configs": [], "broken": [], "stats": {"total": 0}}

    log(f"\n→ {len(companies)} entreprises à configurer")

    # ── ÉTAPE 2+3 : Config + Validation ─────────────────────────────────────
    log("\n📍 ÉTAPE 2+3 — Configuration et validation")

    validated_configs = []
    broken = []
    linkedin_only = []

    # Séquentiel — évite les rate limits (50k tokens/min avec 2 agents parallèles)
    for idx, company in enumerate(companies):
        log(f"\n  [{idx + 1}/{len(companies)}] {company['name']}")
        try:
            r = _process_company(company, profile, log)
        except Exception as e:
            log(f"  ❌ {company['name']} — unexpected error: {e}")
            broken.append({"company": company, "error": str(e)})
            continue

        if r is None:
            continue
        if r.get("validation_status") == "ok":
            validated_configs.append(r["config"])
            log(f"  ✅ {r['name']} ajouté ({r['raw_count']} offres)")
        elif r.get("validation_status") == "filter":
            validated_configs.append(r["config"])
            log(f"  ⚠️  {r['name']} — config OK, 0 offre pertinente pour ce profil")
        elif r.get("validation_status") == "linkedin":
            linkedin_only.append(r)
        else:
            broken.append(r)
            log(f"  ❌ {r['name']} — {r.get('diagnosis', r.get('validation_status', 'broken'))}")

        # Pause pour respecter le rate limit tokens/min
        if idx < len(companies) - 1:
            time.sleep(3)

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
    if linkedin_only:
        log(f"\n🔗 Vérifier manuellement sur LinkedIn ({len(linkedin_only)}) :")
        for r in linkedin_only:
            url = r.get("diagnosis", "")
            log(f"   • {r['name']} → {url}")
    log("═" * 60)

    stats["linkedin_only"] = len(linkedin_only)
    output = {"validated_configs": validated_configs, "broken": broken, "linkedin_only": linkedin_only, "stats": stats}
    with open(os.path.join(tempfile.gettempdir(), "pipeline_result.json"), "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    return output


def _process_company(company: dict, profile: dict, log) -> dict:
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

    # Agent 3 — Validator (avec profil pour le filtre de pertinence)
    agent3_result = validate(agent2_result, profile=profile, progress_cb=log)

    status = agent3_result["status"]

    # Écrire dans la mémoire pour les prochains runs
    if status in ("ok", "filter"):
        memory.add_success(
            company=name,
            ats_type=agent2_result.get("ats_type", "unknown"),
            winning_query=agent2_result.get("winning_query", ""),
            url_found=agent2_result.get("notes", ""),
            config=agent2_result.get("config"),
            raw_count=agent3_result.get("raw_count"),
        )
    else:
        memory.add_failure(
            company=name,
            tried_queries=[agent2_result.get("winning_query", "")],
            reason=agent3_result.get("diagnosis") or agent2_result.get("notes", ""),
        )

    return {
        "name": name,
        "ats_type": agent2_result.get("ats_type"),
        "config": agent2_result.get("config"),
        "confidence": agent2_result.get("confidence"),
        "validation_status": status,
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

    with open(os.path.join(tempfile.gettempdir(), "s1_healthcheck.json"), "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    return {"results": results, "ok": ok, "filter": filt, "broken": broken}


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="S2 pipeline — Agent 1 → Agent 2 → Agent 3")
    parser.add_argument("--profile-file", default=None, help="JSON profile file")
    parser.add_argument("--companies", default=None, metavar="\"Co1,Co2,Co3\"",
                        help="Bypass Agent 1 — tester directement ces entreprises")
    args = parser.parse_args()

    profile = load_profile(args.profile_file) if args.profile_file else load_profile()
    target = [c.strip() for c in args.companies.split(",")] if args.companies else None

    result = run_pipeline(profile, target_companies=target)
    print(f"\nValidated configs: {len(result['validated_configs'])}")
    print(f"Results saved to: {os.path.join(tempfile.gettempdir(), 'pipeline_result.json')}")
