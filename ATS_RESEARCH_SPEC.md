# Spec : Méthode de recherche ATS (Config Generator)

## Problème fondamental

Sans méthode structurée, un agent (humain ou LLM) va :
- Trouver une mention indirecte → déclarer "confirmé"
- Confondre Glencore Coal Australia (`gcaa.wd3`) avec Glencore Trading
- Scraper une homepage qui ne liste pas les jobs et conclure "0 résultat"
- Ne jamais valider que la config fonctionne réellement

Ce document est la spec que tout agent (S1 manuel, S2 automatique) doit suivre.

---

## Étape 1 — Identifier l'ATS

### 1a. Google `site:` search (fiabilité : ★★★★★)

```
site:{domain-carrieres} {job-keyword}
```

Exemples :
- `site:jobs.engie.com trader` → URLs du type `jobs.engie.com/job/{title}/{id}-en_US/` → Phenom People
- `site:orsted.com/en/careers trader` → URLs du type `/vacancies-list/{year}/{month}/{id}-{title}` → portail custom
- `site:{company}.wd3.myworkdayjobs.com` → Workday confirmé

**Ce qu'on lit dans les URLs retournées :**

| Pattern URL | ATS |
|---|---|
| `{tenant}.wd{n}.myworkdayjobs.com/{site}` | Workday |
| `careers.smartrecruiters.com/{company}` ou `jobs.smartrecruiters.com/{company}/...` | SmartRecruiters |
| `{domain}/job/{title}/{id}-en_US/` | Phenom People |
| `{domain}/careers/JobDetail/{id}` | Oracle Taleo |
| `{domain}/en/careers/vacancies-list/...` | Portail custom |
| `{domain}/jobs/{id}` | Greenhouse |
| `{domain}/postings/{uuid}` | Ashby |

### 1b. Google direct search (fiabilité : ★★★☆☆)

```
{Company} careers workday OR smartrecruiters myworkdayjobs.com
```

Utiliser uniquement pour trouver des pistes, jamais pour conclure.

### 1c. WebFetch page carrières (fiabilité : ★★☆☆☆)

Souvent bloqué (403). Ne jamais compter dessus comme seule source.

---

## Étape 2 — Trouver les paramètres exacts

### Pour Workday

**Source prioritaire :** URL indexée par Google du type `{tenant}.wd{n}.myworkdayjobs.com/{site}`

- `tenant` = premier segment du domaine
- `wd` = `wd1`, `wd2`, `wd3`, `wd5` — segment entre tenant et `.myworkdayjobs.com`
- `site` = dernier segment de l'URL

**Piège courant :** une entreprise peut avoir plusieurs portails Workday (ex: Glencore a `gcaa.wd3` pour le charbon Australia ET `glencore.wd3` pour le trading). Toujours vérifier que le portail trouvé correspond à la bonne division.

### Pour SmartRecruiters

**Source prioritaire :** URL du type `careers.smartrecruiters.com/{sr_id}` ou `jobs.smartrecruiters.com/{sr_id}/`

Le `sr_id` est exactement ce qui apparaît dans ces URLs. Case-sensitive (ex: `statkraft1` pas `Statkraft1`).

### Pour HTML / Phenom / Custom

- **URL de listing** : trouver la page qui LISTE les jobs (pas la homepage, pas une page "nos métiers")
  - Phenom People : souvent `/search/?q={terme}` ou `/jobs/`
  - Portail custom : souvent `/careers/vacancies-list` ou `/jobs/search`
  - Vérifier en faisant `site:{domain} {job-keyword}` et regarder de quelle page parent viennent les résultats
- **job_pattern** : extraire le substring commun à tous les liens de jobs

---

## Étape 3 — Valider (OBLIGATOIRE)

**Aucune config n'est "confirmée" sans validation.**

### Validation Workday

```python
# Appel API direct — doit retourner HTTP 200 et des jobs
POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
Body: {"limit": 5, "offset": 0, "searchText": "trader"}
```

- HTTP 200 + `jobPostings` non vide → ✅ CONFIRMÉ
- HTTP 404 → tenant ou site incorrect
- HTTP 200 + `jobPostings` vide → endpoint valide mais pas de résultat pour ce terme (tester d'autres termes)
- HTTP 403 → endpoint bloqué (rare)

### Validation SmartRecruiters

```
GET https://api.smartrecruiters.com/v1/companies/{sr_id}/postings?limit=5
```

- HTTP 200 + `content` non vide → ✅ CONFIRMÉ
- HTTP 404 → `sr_id` incorrect

### Validation HTML/Phenom

```
WebFetch {url_listing} + chercher liens contenant {job_pattern}
```

- Liens trouvés → ✅ CONFIRMÉ
- 403 → page anti-bot, nécessite Playwright (noter comme "non validé sans browser")
- 0 lien → url_listing ou job_pattern incorrect

---

## Étape 4 — Niveaux de confiance

| Niveau | Définition | Action |
|---|---|---|
| ✅ CONFIRMÉ | Appel API ou WebFetch réussi retournant des données | Ajouter à la config |
| 🔧 PROBABLE | URL trouvée via `site:` Google mais appel API non testé | Ajouter mais marquer "à valider" |
| ❓ INCONNU | Seulement des mentions indirectes | Ne pas ajouter à la config |
| ❌ INVALIDE | Appel API retourne 404/erreur | Chercher le bon paramètre |

---

## Checklist par entreprise

Pour chaque nouvelle entreprise à ajouter :

```
[ ] 1. site:{domain} {keyword} → identifier ATS + URL pattern
[ ] 2. Trouver les paramètres exacts (tenant/site/sr_id/url)
[ ] 3. Vérifier que c'est la bonne division (trading, pas mining/retail/etc.)
[ ] 4. Appel de validation → HTTP 200 + données
[ ] 5. Documenter : source + niveau de confiance + date de validation
```

---

## Application aux boîtes actuellement bloquées

### ENGIE
- ATS : Phenom People (`jobs.engie.com`) — confirmé via `site:jobs.engie.com trader`
- URL listing : inconnue — la homepage ne liste pas les jobs
- Validation nécessaire : tester `jobs.engie.com/search/?q=trader` avec Playwright
- Confiance actuelle : 🔧 PROBABLE (pattern `/job/` confirmé, URL listing non validée)

### SEFE M&T
- ATS : inconnu
- Action : `site:{domain-sefe} job` pour trouver l'ATS
- Confiance actuelle : ❓ INCONNU

### Glencore
- ATS : Workday — trouvé `glencore.wd3.myworkdayjobs.com/External` via recherche Google
- Validation nécessaire : appel POST API
- Confiance actuelle : 🔧 PROBABLE (URL trouvée indirect, API non testée)

### Orsted
- ATS : portail HTML custom `orsted.com/en/careers/vacancies-list`
- Pattern : `/vacancies-list/` — confirmé via `site:orsted.com/en/careers trader`
- Validation nécessaire : WebFetch ou Playwright
- Confiance actuelle : 🔧 PROBABLE

---

## Ce que cette spec change pour l'agent Config Generator (S2)

L'agent doit suivre exactement ce pipeline, dans l'ordre :

```
1. site:{domain} {keyword}           → détecter ATS + pattern URL
2. Extraire paramètres               → tenant / sr_id / url / job_pattern
3. Appel de validation               → HTTP 200 ?
4. Si non → explorer variantes       → wd1/wd2/wd3, autre sr_id, autre URL
5. Écrire résultat avec niveau de confiance
```

L'agent ne doit JAMAIS écrire un bloc config sans avoir passé l'étape 3.
