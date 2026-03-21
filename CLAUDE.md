# Claude Code — Règles de développement

## Règles impératives (à respecter à chaque modification)

### 1. Toujours maintenir `requirements.txt`
Quand tu ajoutes un `import` d'une lib tierce dans n'importe quel fichier Python,
tu DOIS ajouter la dépendance dans `requirements.txt` dans le même commit.
Libs tierces actuelles : `requests`, `urllib3`, `beautifulsoup4`, `flask`, `playwright`, `anthropic`, `duckduckgo-search`, `tenacity`, `pydantic`

### 2. Chemins fichiers — toujours cross-platform
Ne jamais hardcoder `/tmp/`. Utiliser `os.path.join(tempfile.gettempdir(), ...)`.
Import `tempfile` requis.

### 3. Coût tokens — limites à ne pas dépasser
| Paramètre | Max autorisé | Raison |
|-----------|-------------|--------|
| `max_turns` | 6 | Agent 1 uniquement (LLM Discovery). Agent 2/3 = Python pur, 0 turn LLM. |
| `max_tokens` | 800 | Suffisant pour JSON + raisonnement court. Défaut dans `loop.py` DOIT être 800 (pas 1024). |
| `web_search` max_results | 5 | 10 résultats = 2× les tokens pour rien |
| `web_search` result | 2000 chars | Tronquer dans `tools.py` — suffisant pour snippets |
| `web_fetch` result | 4000 chars | Plus long nécessaire pour détecter les patterns ATS dans le HTML |

### 4. Économie de tokens dans les prompts agents — règles impératives

**Principe : prompt complet ≠ prompt verbeux.** Un prompt doit être précis et prescriptif, pas long.

#### Stop au premier hit
Tout agent qui fait des recherches séquentielles DOIT avoir cette règle explicite dans son SYSTEM prompt :
```
AS SOON AS one step returns a hit → STOP. Output result immediately.
Every extra search costs money.
```
Sans ça, le LLM continue à chercher même après avoir trouvé.

#### Pas de searches génériques entre les steps
Interdire explicitement les recherches hors-protocole :
```
STRICTLY execute steps a→e in order. NO generic searches. NO deviations.
```
Sans ça, le LLM insère des searches "de confirmation" inutiles.

#### Ordre des searches : du plus probable au moins probable
- Workday en premier (60%+ des grandes boîtes energy/trading)
- SmartRecruiters, Greenhouse ensuite
- Lever, Ashby en dernier (rares dans ce secteur)
→ La majorité des boîtes sont trouvées en 1-2 turns, pas 5.

#### max_tokens dans le SYSTEM prompt
Préciser dans le SYSTEM : "Output JSON only, no prose" + "Be concise".
Le LLM a tendance à sur-expliquer si on ne le contraint pas, ce qui consomme des tokens inutiles dans le contexte cumulé.

#### Ne jamais demander au LLM ce que Python peut faire
Si une opération est déterministe (construire une URL, normaliser un nom, déduplication), la faire en Python post-processing — pas dans un turn LLM supplémentaire.
Exemple : LinkedIn URL fallback → Python, pas agent.

### 5. HTTP retry — règle impérative

Toute fonction qui appelle `requests.get/post` directement DOIT utiliser le décorateur tenacity défini dans `agents/agent3_validator.py` (`_HTTP_RETRY`).

Pattern canonique :
```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

_HTTP_RETRY = dict(
    retry=retry_if_exception_type((requests.exceptions.ConnectionError,
                                   requests.exceptions.Timeout,
                                   requests.exceptions.ChunkedEncodingError)),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    reraise=True,
)

@retry(**_HTTP_RETRY)
def _my_http_call(...):
    ...
```

**Ne jamais retenter sur `HTTPError`** (4xx/5xx) — une 404 = config incorrecte, pas une erreur réseau.
Exception : `_validate_html` (boucle multi-URL avec `except: pass` intentionnel) et `_validate_greenhouse` (logique EU→US 404 fallback — utilise le helper `_gh_get` à la place).

### 6. Logging — règle impérative

#### Log exhaustif `run.log` — règle systématique pour tout projet

Chaque projet doit produire un fichier `run.log` à la racine du projet, écrasé à chaque run.
C'est le fichier à copier-coller directement à Claude pour le diagnostic — **pas besoin de décrire le problème, le log dit tout**.

**Dans le script principal** (`main()` ou équivalent) — appeler `init_run_log()` en tout premier :
```python
from agents.log import get_logger, init_run_log
log_path = init_run_log()   # écrase run.log, démarre propre
print(f"📋 Log : {log_path}")
```

**Dans chaque module** :
```python
from agents.log import get_logger
logger = get_logger("nom_du_module")
```

**Niveau de détail exigé dans `run.log`** (DEBUG) :
- Toutes les requêtes HTTP (URL, méthode, statut, nb résultats)
- Toutes les décisions de parsing (quel sélecteur a matché, combien d'items avant/après filtre)
- Toutes les valeurs intermédiaires importantes (tenant, sr_id, board_token, total/cap pagination)
- Toutes les erreurs et fallbacks (avec URL + message d'erreur complet)
- Résumé final (nb jobs par société, stratégie utilisée)

**Console** : INFO uniquement — messages humains propres, pas de spam DEBUG.
**Fichier** : DEBUG tout — `<project_root>/run.log`, écrasé à chaque run (pas de versioning).

Ne jamais utiliser `print()` dans `agents/` pour des erreurs ou warnings silencieux.
Utiliser `logger.debug()` pour les détails de parsing, `logger.warning()` pour les échecs.

### 7. Validation schema sur les outputs LLM

Tout output LLM parsé en JSON DOIT passer par un modèle Pydantic avant utilisation.
Modèle existant : `CompanyResult` dans `agents/agent1_discovery.py`.

Principe :
- Items invalides = filtrés + loggés (`logger.debug`), jamais raising
- `model_dump()` pour convertir en dict standard avant de passer au reste du pipeline
- Validator `mode="before"` pour normaliser les strings vides/null du LLM

### 8. Type hints — obligatoires sur tout nouveau code (Python 3.10)

Toute nouvelle fonction et tout fichier modifié DOIT avoir des annotations de type.
Syntaxe Python 3.10 — pas besoin de `from __future__ import annotations` :

```python
# ✅ Python 3.10+
def validate(config: dict | None) -> str | None: ...
def find_companies(names: list[str]) -> list[dict]: ...

# ❌ Style pré-3.10 — ne pas utiliser
from typing import Optional, List, Dict
def validate(config: Optional[Dict]) -> Optional[str]: ...
```

- `X | Y` à la place de `Union[X, Y]`
- `X | None` à la place de `Optional[X]`
- `list[str]` / `dict[str, int]` à la place de `List[str]` / `Dict[str, int]` (minuscules)
- Ne pas annoter rétrospectivement tout le code existant — uniquement le code que tu touches.

### 9. SSL — jamais `verify=False`

Interdire `requests.get(..., verify=False)`, `requests.post(..., verify=False)` et `urllib3.disable_warnings()`.
Ces patterns désactivent la validation des certificats TLS → attaque MITM possible.

Si un endpoint retourne une erreur SSL :
- ❌ `verify=False` — masque le problème silencieusement
- ✅ Lever une exception explicite avec l'URL et le message d'erreur → problème visible dans les logs

### 10. Pas de dead code

Interdire :
- `import X` si X n'est pas utilisé dans le fichier
- Fonctions définies mais jamais appelées
- Classes ou constantes définies mais non référencées

Règle : si tu ajoutes un import ou une fonction, elle doit être utilisée dans le même commit.
Si une fonction devient inutile après refactor → la supprimer dans le même commit.

### 11. Tests inline obligatoires avant tout commit

**Toute nouvelle fonction dans `playwright_strategies.py` ou `job_scrapper.py` DOIT être testée via Bash avant le commit.**

Protocole obligatoire :
1. Écrire les tests inline (`python -c "..."`) avec des fixtures tirées des vrais logs (corps API interceptés, HTML réels)
2. Exécuter via `Bash` — les tests doivent passer avant de stager le fichier
3. Seulement si ✅ → `git add` + `git commit`

Ce qui est testable sans Playwright (à tester systématiquement) :
- Fonctions pures : `_total_count_from_body`, `_find_job_list_in_body`, `_ci_get_from`, `_heuristic_title`, `_heuristic_url`, `_extract_location`, `_parse_api_jobs`, `parse_jobs_from_html`
- Fonctions HTTP : mocker `requests` avec `unittest.mock.patch`

Ce qui n'est pas testable ici (accepté sans test inline) :
- Fonctions Playwright (`smart_scrape_site`, `_navigate`, etc.) → validées par l'utilisateur dans PyCharm

**Exemples de bugs qui auraient été attrapés avec des tests :**
- `TotalCount: 9 == len(Results): 9` → `9 > 9 = False` → pagination jamais déclenchée
- `"Results"` non trouvé par lookup case-sensitive alors que `"results"` était dans `JOB_LIST_KEYS`

---

## Architecture Python-first — principe fondamental

**L'agent LLM est le dernier recours, pas le premier réflexe.**

### Hiérarchie d'exécution (du moins cher au plus cher)

| Niveau | Coût tokens | Quand l'utiliser |
|--------|-------------|-----------------|
| 1. Cache mémoire (`memory.get_success()`) | 0 | Toujours en premier |
| 2. Python déterministe (regex, HTTP, parsing) | 0 | Si la réponse est structurée |
| 3. LLM output contraint (JSON only, prompt court) | maîtrisé | Extraction NLP simple |
| 4. LLM libre (raisonnement, diagnostic) | élevé | Uniquement si 1–3 impossibles |

### Règle : ne jamais utiliser un LLM pour ce que Python peut faire

| Tâche | ❌ Mauvais | ✅ Bon |
|-------|-----------|--------|
| Extraire tenant/wd/site depuis une URL | LLM turn | `re.search(pattern, url)` |
| Dédupliquer une liste de boîtes | LLM turn | `set()` + normalize |
| Construire une URL ATS | LLM turn | f-string Python |
| Tester si une API retourne ≥1 résultat | LLM turn | `requests.get()` + `len()` |
| Fallback URL connu | LLM turn | constante Python |

### Quand utiliser un LLM

✅ **Oui** :
- Extraire des noms de sociétés depuis des snippets web non structurés (Agent 1)
- Diagnostiquer pourquoi une config a échoué en 2 phrases (Agent 3 — Haiku)
- Comprendre le HTML d'une page inconnue pour en extraire le `job_pattern`

❌ **Non** :
- URL pattern matching → regex Python
- Validation HTTP endpoint → `requests` direct
- Déduplication de listes → Python `set`
- Construction de configs ATS → Python dict depuis paramètres extraits par regex

### Pattern canonique : orchestrateur Python + agent LLM en fallback

```python
# ✅ BON — Python orchestre, LLM uniquement si Python échoue
for pattern in ATS_PATTERNS:
    result = web_search(pattern.query)         # HTTP, pas LLM
    m = re.search(pattern.url_re, result)      # Python, pas LLM
    if m:
        return build_config(m)                 # Python, pas LLM

# LLM en dernier recours seulement
return run_agent(system=SYSTEM, ...)

# ❌ MAUVAIS — LLM orchestre tout
return run_agent(system="Search these 5 ATS in order a→e...")
```

### Mémoire persistante — vérifier en premier, toujours

```python
# Étape 0 — AVANT tout traitement
cached = memory.get_success(company_name)
if cached:
    return cached  # 0 token, résultat immédiat
```

Coût réel Agent 2 sans cache : ~12–23k tokens/boîte (Haiku, peu fiable).
Coût avec cache mémoire : 0 token pour les boîtes connues.
Coût avec Python-first : ~0 token même pour les nouvelles boîtes (5 HTTP calls, 0 LLM).

---

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
| Glencore | Greenhouse EU | ✅ corrigé — glencoreuk + tlgglencorebaar | fait v21 |
| Statkraft | SmartRecruiters | ✅ corrigé — sr_id statkraft1 | fait |
| InCommodities | HTML | ✅ OK | fait v20 |
| Petroineos | HTML | ✅ OK | fait v20 |
| Orsted | HTML (portail custom) | ✅ corrigé — /vacancies-list/ | fait |
| SEFE M&T | SAP SuccessFactors (probable) | ❓ à confirmer — careers.sefe.eu → 403 proxy | vérifier manuellement |

---

## Stream 2 — Produit commercial (moyen terme)

**Objectif** : un user quelconque entre son profil → reçoit ses offres pertinentes.

### Les 3 agents à construire

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
Output : bloc de config candidat
         ex: {"name": "Trafigura", "tenant": "trafigura", "site": "TrafiguraCareerSite", "wd": "wd3"}
```

#### Agent 3 — Validator (Health Check)
```
Input  : config (générée par Agent 2, ou config existante S1)
Process: hit l'endpoint sans aucun filtre métier
         → "donne-moi n'importe quelle offre"
         → vérifie qu'on reçoit ≥ 1 résultat
Output : ✅ OK (N offres trouvées) / ❌ BROKEN (0 résultats + raison probable)
```

**Double usage de l'Agent 3 :**
- **Pipeline S2** : certifie les configs générées par Agent 2 avant de les ajouter au pool
- **Cron S1** : health-check périodique sur les configs existantes → détecte les régressions

**Pipeline complet S2 :**
```
Agent 1 → liste de boîtes
    → pour chaque boîte : Agent 2 → config candidate
        → Agent 3 → ✅ ajouter au pool / ❌ rejeter + logger
```

### Lien S1 → S2
Les configs hardcodées de S1 = **ground truth** pour valider Agent 2 :
```
Agent 2 input : "Trafigura"
Expected      : {tenant: "trafigura", site: "TrafiguraCareerSite", wd: "wd3"}  ← S1 le sait déjà
```

Agent 3 sur S1 = solution directe au 🔴 "Diagnostiquer boîtes à 0 résultat" :
```
ENGIE    → ❌ 0 résultats → raison : URL search incorrecte
Statkraft → ✅ 3 offres   → OK
```

### Backlog Stream 2
| Priorité | Tâche | Statut |
|----------|-------|--------|
| 🔴 | Prototyper Agent 2 (Config Generator) sur 3 boîtes connues | en attente |
| 🔴 | Prototyper Agent 1 (Discovery) → liste de boîtes energy EU | en attente |
| 🔴 | Prototyper Agent 3 (Validator) → health-check sur configs S1 existantes | en attente |
| 🟡 | Chaîner Agent 1 → Agent 2 → Agent 3 (pipeline complet) | en attente |
| 🟡 | Paramétrer par profil utilisateur | en attente |
| 🟢 | Infrastructure web + auth + paiement | en attente |

---

## Règles opérationnelles pour les agents LLM

Ces règles s'appliquent uniquement aux agents LLM (`run_agent()`).
Agent 2 et Agent 3 sont Python-first — ces règles ne les concernent pas.

### 1. Borner avec `max_turns`
| Agent | max_turns | Raison |
|-------|-----------|--------|
| Agent 1 Discovery | 6 | 4 searches Phase 1 + 2 gap filling Phase 2 |
| LLM diagnosis (Agent 3) | 1 | Réponse courte, 1 turn suffisant |
| Tout autre agent LLM | 6 | Limite absolue — voir table tokens ci-dessus |

### 2. Écrire les résultats au fil de l'eau
Écrire dans `os.path.join(tempfile.gettempdir(), "<task>.json")` dès qu'un résultat est disponible.
Ne jamais attendre la fin.

### 3. Ne pas paralléliser les agents LLM
Max 1 agent LLM actif à la fois.
Raison : rate limit 50k tokens/min Anthropic — 2 agents simultanés = hit garanti.
Les agents Python (Agent 2, Agent 3) peuvent tourner séquentiellement sans limite.

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
| `GREENHOUSE_COMPANIES` | Greenhouse API | GET `/v1/boards/{board_token}/jobs` |
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

### Ajouter une entreprise Greenhouse
```python
{"name": "Company", "board_token": "companytoken", "region": "eu"}  # region: "eu" ou "us"
# API : boards-api.{region}.greenhouse.io/v1/boards/{board_token}/jobs
```

## Environnement d'exécution
- **Proxy** : pas de connexion directe à `*.myworkdayjobs.com` — utiliser `WebSearch`
- **Branch** : `claude/review-job-scraper-JxWYi`
- **Résultats de recherche** : `os.path.join(tempfile.gettempdir(), "ats_results.md")`
