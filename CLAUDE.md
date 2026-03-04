# Claude Code — Règles de développement

## Projet
Job scraper Python (`job_scrapper.py`) qui agrège les offres d'emploi energy/trading
depuis plusieurs ATS : Workday, SmartRecruiters, Taleo, et scraping HTML direct.

---

## Règles impératives pour tout lancement d'agent

### 1. Toujours borner avec `max_turns`
```python
Agent(
    max_turns=8,   # jamais plus de 10 pour une recherche, 15 pour une implémentation
    ...
)
```

### 2. Toujours écrire les résultats dans un fichier dès qu'ils sont trouvés
Chaque agent doit écrire dans un fichier `/tmp/<task_name>.md` **immédiatement** après
chaque découverte, pas à la fin. Inclure cette instruction dans le prompt :

```
Écris le résultat dans /tmp/<task>.md dès que tu l'as trouvé.
N'attends pas la fin pour écrire — écris au fur et à mesure.
```

### 3. Paralléliser : un agent = une tâche atomique
- Max 2-3 entreprises par agent (pas toutes en même temps)
- Lancer les agents indépendants en parallèle (`run_in_background=True`)
- Ne pas lancer plus de 5 agents en parallèle

### 4. Format de fichier de résultats intermédiaires
Utiliser ce format dans `/tmp/<task>.md` :
```markdown
# <Task name>
## <Company/Item>
- statut: ✅ OK / ❌ WRONG / ❓ INCONNU
- valeur actuelle: ...
- valeur correcte: ...
- source: (URL ou méthode de vérification)
```

### 5. Template de prompt agent
```
[Description de la tâche]

Pour chaque [item], écris IMMÉDIATEMENT le résultat dans /tmp/<task>.md :
## [item]
- résultat: ...

Puis passe au suivant.
Max X turns.
```

---

## Architecture du scraper

### ATS supportés
| Variable | Type | Méthode |
|----------|------|---------|
| `SITES` | HTML direct | Playwright + requests fallback |
| `WORKDAY_COMPANIES` | Workday API | POST JSON `/wday/cxs/` |
| `SMARTRECRUITERS_COMPANIES` | SmartRecruiters API | GET `/v1/companies/{sr_id}/postings` |
| `TALEO_SITES` | Taleo HTML | requests + BeautifulSoup |

### Ajouter une entreprise HTML
```python
{
    "name": "CompanyName",
    "type": "html",
    "pages": ["https://careers.company.com/jobs"],
    "job_pattern": "/jobs/",   # substring dans les hrefs d'offres individuelles
}
```

### Ajouter une entreprise Workday
```python
{"name": "Company", "tenant": "companyid", "site": "CompanyCareers", "wd": "wd3"}
# Vérifier l'URL réelle : https://{tenant}.{wd}.myworkdayjobs.com/{site}
```

### Ajouter une entreprise SmartRecruiters
```python
{"name": "Company", "sr_id": "CompanyId"}
# Vérifier : https://jobs.smartrecruiters.com/{sr_id}/ doit retourner des offres
```

---

## Environnement d'exécution
- **Proxy** : les connexions directes à `*.myworkdayjobs.com` et aux API externes
  sont souvent bloquées dans cet environnement. Utiliser `WebSearch` pour vérifier
  les configs ATS plutôt que des requêtes HTTP directes.
- **Branch** : toujours développer sur `claude/review-job-scraper-JxWYi`
- **Résultats de recherche** : sauvegarder dans `/tmp/ats_results.md`
