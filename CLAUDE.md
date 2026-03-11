# Claude Code — Règles de développement

## Vision : 2 streams

---

## Stream 1 — Scraper Python perso (court terme)

**Objectif** : toi tu as les meilleures offres energy/trading chaque semaine.

- Hardcodé, tourne en local ou cron
- Fixer les boîtes à 0 résultat, élargir la liste, affiner le scoring
- C'est ton outil perso, pas un produit

### Backlog
| Priorité | Tâche | Statut |
|----------|-------|--------|
| 🔴 | Diagnostiquer boîtes à 0 résultat (ENGIE, Glencore, Statkraft...) | en attente |
| 🟡 | Élargir la liste (qu'est-ce qui manque autour de Trafigura Geneva ?) | en attente |
| 🟡 | Affiner le scoring pour ton profil | en attente |
| 🟢 | Cron / automatisation locale | en attente |

### État des configs ATS
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
| ENGIE | HTML (Phenom) | 🔧 URLs search ajoutées | à tester |
| Glencore | Workday | ✅ corrigé — tenant glencore/External/wd3 | fait |
| Statkraft | SmartRecruiters | ✅ corrigé — sr_id statkraft1 | fait |
| InCommodities | HTML | ✅ OK | fait v20 |
| Petroineos | HTML | ✅ OK | fait v20 |
| Orsted | HTML (portail custom) | ✅ corrigé — /vacancies-list/ | fait |
| SEFE M&T | HTML | ❓ ATS inconnu | bloqué |

---

## Stream 2 — Produit commercial (moyen terme)

**Objectif** : un user quelconque entre son profil → reçoit ses offres pertinentes.

### Les 2 agents à construire

#### Agent 1 — Discovery
```
Input  : profil utilisateur (secteur, rôle, localisation, séniorité)
Process: WebSearch → "energy/power trading companies Europe" + variantes
         → extrait noms d'entreprises → déduplique → filtre pertinence
Output : liste de 100+ boîtes correspondant au profil
```

#### Agent 2 — Config Generator
```
Input  : nom d'une entreprise (ex: "Trafigura")
Process: WebSearch + WebFetch → identifie ATS utilisé (Workday/SR/Taleo/HTML)
         → trouve URL carrières + job_pattern
         → génère le bloc de config Python
         → valide (vérifie que l'URL retourne bien des offres)
Output : bloc de config prêt à l'emploi
         ex: {"name": "Trafigura", "tenant": "trafigura", "site": "TrafiguraCareerSite", "wd": "wd3"}
```

### Lien S1 → S2
Les configs hardcodées de S1 = **ground truth** pour valider Agent 2 :
```
Agent 2 input : "Trafigura"
Expected      : {tenant: "trafigura", site: "TrafiguraCareerSite", wd: "wd3"}  ← S1 le sait déjà
```

### Backlog Stream 2
| Priorité | Tâche | Statut |
|----------|-------|--------|
| 🔴 | Prototyper Agent 2 (Config Generator) sur 3 boîtes connues | en attente |
| 🔴 | Prototyper Agent 1 (Discovery) → liste de boîtes energy EU | en attente |
| 🟡 | Chaîner Agent 1 → Agent 2 (pipeline complet) | en attente |
| 🟡 | Paramétrer par profil utilisateur | en attente |
| 🟢 | Infrastructure web + auth + paiement | en attente |

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

---

## Architecture du scraper S1

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
```

## Environnement d'exécution
- **Proxy** : pas de connexion directe à `*.myworkdayjobs.com` — utiliser `WebSearch`
- **Branch** : `claude/review-job-scraper-JxWYi`
- **Résultats de recherche** : `/tmp/ats_results.md`
