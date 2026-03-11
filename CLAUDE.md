# Claude Code — Règles de développement

## Vision : 2 streams en co-construction

```
Stream 1 (perso, court terme)     Stream 2 (produit, moyen terme)
──────────────────────────────    ────────────────────────────────
Python scraper local              Site web + agents IA
→ meilleures offres chaque        → user entre son profil
  semaine pour TOI                → reçoit ses offres
→ hardcodé, cron, local           → auto-discovery, paramétrable
                                  → infra web + paiement
         ↓ alimente ↑
Chaque boîte fixée en S1 = cas de test pour valider que l'agent S2
la redécouvre tout seul correctement
```

---

## Stream 1 — Python perso

**Objectif** : avoir les meilleures offres energy/trading chaque semaine.

### Backlog Stream 1
| Priorité | Tâche | Statut |
|----------|-------|--------|
| 🔴 | Fixer les boîtes à 0 résultat (ENGIE, Glencore, Statkraft...) | en attente |
| 🟡 | Élargir la liste (qu'est-ce qui manque autour de Trafigura Geneva ?) | en attente |
| 🟡 | Affiner le scoring pour le profil utilisateur | en attente |
| 🟢 | Cron / automatisation locale | en attente |

### État des configs ATS (vérifiées)
| Entreprise | ATS | Statut | Action code |
|------------|-----|--------|-------------|
| Trafigura | Workday | ✅ OK | — |
| Gunvor | Workday | ✅ OK | — |
| Shell | Workday | ✅ OK | — |
| BP | Workday | ✅ corrigé v15 | fait |
| Equinor | Workday | ✅ OK | — |
| EDF Trading | Workday | ✅ corrigé v15 | fait |
| Centrica | Workday | ✅ OK | — |
| CCI | Workday | ✅ OK (wd1, osv-cci) | fait v20 |
| RWE | HTML | ✅ OK | — |
| Uniper | HTML | ✅ OK | — |
| ENGIE | HTML | ❌ 0 résultat | à diagnostiquer |
| Glencore | HTML | ❌ 0 résultat | à diagnostiquer |
| Statkraft | HTML | ❌ 0 résultat | à diagnostiquer |
| InCommodities | HTML | ✅ OK | fait v20 |
| Petroineos | HTML | ✅ OK (/postings/) | fait v20 |
| Orsted | Workday | ❓ non confirmé | bloquer |
| SEFE M&T | HTML | ❓ ATS inconnu | bloquer |

---

## Stream 2 — Produit commercial

**Objectif** : un user entre son profil → reçoit ses offres pertinentes.

### Vision produit
- **Agent Discovery** : trouve automatiquement le portail carrières d'une entreprise + identifie l'ATS
- **Paramétrable** : profil utilisateur (secteur, rôle, localisation, séniorité) — pas hardcodé
- **Infrastructure** : site web, auth, paiement
- **Fiabilité industrielle** : monitoring, fallbacks, alertes

### Backlog Stream 2
| Priorité | Tâche | Statut |
|----------|-------|--------|
| 🔴 | Prototyper Agent Discovery (donner une entreprise → trouve l'ATS et la config) | en attente |
| 🟡 | Valider avec les boîtes déjà connues de S1 (cas de test) | en attente |
| 🟡 | Définir le modèle de données profil utilisateur | en attente |
| 🟢 | Infra web (stack à choisir) | en attente |
| 🟢 | Système de paiement | en attente |

### Lien S1 → S2
Chaque entreprise fixée dans S1 devient un **cas de test** :
```
input: "Trafigura"
expected output: {type: "workday", tenant: "trafigura", site: "TrafiguraCareerSite", wd: "wd3"}
```

---

## Règles opérationnelles pour les agents

### 1. Toujours borner avec `max_turns`
```python
Agent(max_turns=8)   # recherche: max 10 / implémentation: max 15
```

### 2. Écrire les résultats au fil de l'eau
```
Écris le résultat dans /tmp/<task>.md dès que tu l'as trouvé.
N'attends pas la fin — écris au fur et à mesure.
```

### 3. Paralléliser (un agent = une tâche atomique)
- Max 2-3 entreprises par agent
- Max 5 agents en parallèle (`run_in_background=True`)

### 4. Format résultats intermédiaires
```markdown
## <Company>
- statut: ✅ OK / ❌ WRONG / ❓ INCONNU
- valeur actuelle: ...
- valeur correcte: ...
- source: ...
```

### 5. Jamais d'implémentation sans vérification
- Stream 1 : on ne code que les ✅
- Stream 2 : chaque feature validée par un cas de test S1

---

## Architecture du scraper (Stream 1)

### ATS supportés
| Variable | Type | Méthode |
|----------|------|---------|
| `SITES` | HTML direct | Playwright + requests fallback |
| `WORKDAY_COMPANIES` | Workday API | POST JSON `/wday/cxs/` |
| `SMARTRECRUITERS_COMPANIES` | SmartRecruiters API | GET `/v1/companies/{sr_id}/postings` |
| `TALEO_SITES` | Taleo HTML | requests + BeautifulSoup |

### Ajouter une entreprise HTML
```python
{"name": "CompanyName", "type": "html", "pages": ["https://..."], "job_pattern": "/jobs/"}
```

### Ajouter une entreprise Workday
```python
{"name": "Company", "tenant": "id", "site": "SiteName", "wd": "wd3"}
# URL réelle : https://{tenant}.{wd}.myworkdayjobs.com/{site}
```

### Ajouter une entreprise SmartRecruiters
```python
{"name": "Company", "sr_id": "CompanyId"}
# Vérifier : https://jobs.smartrecruiters.com/{sr_id}/
```

---

## Environnement d'exécution
- **Proxy** : pas de connexion directe à `*.myworkdayjobs.com` — utiliser `WebSearch`
- **Branch** : `claude/review-job-scraper-JxWYi`
- **Résultats de recherche** : `/tmp/ats_results.md`
