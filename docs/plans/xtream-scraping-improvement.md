# Plan — Améliorer la récupération/scraping des médias Xtream (matching TMDB)

> **But** : faire monter le taux d'auto-match TMDB des films/séries Xtream pour ne plus avoir à
> rescraper à la main (tinyMM + import `.nfo`). On transpose le scrapper **PlexHubTV** (Android),
> qui marche bien, vers le backend Python.
>
> **Spec destinée à être exécutée dans Antigravity / Claude Code.** Chaque section indique le
> fichier exact, le comportement attendu et les tests. À traiter via `/refacto` (owner :
> `sync-specialist`) ou directement.

---

## 1. Diagnostic (prouvé)

### 1.1 Chiffres réels (`data/plexhub.db`, 2026-06-15)
- **Films : 19 % non matchés** (3 963 / 20 475).
- **Séries : 57 % non matchés** (2 561 / 4 465) ← cible prioritaire.
- Épisodes : 100 % « non matchés » mais **normal** — l'enrichissement ne traite que `movie` + `show`
  (`enrichment_worker.py:170,208`). Ne pas compter les épisodes dans le taux d'échec.

### 1.2 Chaîne actuelle
`sync_worker.py:88` nettoie le nom Xtream avec **`parse_title_and_year`** → stocke `media.title`
→ `media_service.py:198` enfile `EnrichmentQueue(title=media.title)` → `enrichment_worker.py:51`
appelle `tmdb_service.search_movie(item.title, item.year)` → `_best_match` exige **confiance ≥ 0,85**
(`tmdb_service.py:323`). Tout repose sur ce nettoyeur + ce scoring.

### 1.3 Causes racines (reproduites en exécutant le code)

**(a) `parse_title_and_year` laisse passer des motifs de pollution** (c'est la fonction utilisée au sync) :

| Titre source | Sortie réelle | Défaut |
|---|---|---|
| `Avatar (FR)` | `('Avatar (FR)', None)` | parenthèse non-année finale **gardée** |
| `Avatar (2009) (FR)` | `('Avatar (2009) (FR)', None)` | **année ratée ET gardée** (regex année ancré en fin : `string_normalizer.py:18`) |
| `VOSTFR - Dune`, `Fr - Le Parrain`, `FRA - …` | inchangé | préfixe non retiré (regex = **exactement 2 lettres MAJ** : `string_normalizer.py:8`) |
| `Spider-Man : No Way Home (2021) MULTI 1080p` | inchangé | année au milieu + `MULTI 1080p` non retirés |
| `Oppenheimer 2023`, `Le.Cygne.Noir.2010.MULTI` | inchangé | année **sans parenthèses** / séparateurs `.` non gérés |

> Note : une meilleure fonction existe déjà (`parse_title_year_and_suffix`, gère `(FR)` et l'année au
> milieu) mais **n'est pas branchée au sync**.

**(b) `normalize_for_sorting` (Python) est un port partiel** : il enlève les articles de tête et les
accents, mais **ne retire pas la ponctuation** (`-`, `:`, `!`, …) et ne fait ni uppercase ni collapse
d'espaces. Le `StringNormalizer.kt` Android, lui, fait `[^\p{L}\p{N}\s] → ""`. Conséquence :
`"Spider-Man : No Way Home"` garde `-`/`:` et fait chuter la similarité.

**(c) `_best_match` (scoring) est plus faible que le matcher Android** (`tmdb_service.py:274-325`) :
- confiance = `title_sim × year_factor` (multiplicatif) avec **year_factor plancher 0,85 même si
  l'année est fausse** (`:307`) → les homonymes d'années différentes peuvent passer.
- ne compare **que** le titre localisé, **pas** `original_title` / `original_name`.
- `fuzz.ratio` strict (pénalise mots en plus / ordre différent).
- **aucune marge** vs 2ᵉ candidat (pas de garde anti-ambiguïté), pas de tiebreak `vote_count`.

**(d) Aucun fallback** : si pas de match ≥ 0,85 à la 1ʳᵉ recherche → `skipped`, puis abandon après
`MAX_ATTEMPTS=3`. D'où la rescrape manuelle.

---

## 2. Modèle de référence PlexHubTV (à transposer)

`domain/.../usecase/ScraperMatcher.kt` (Android) :

```
confidence = 0.7 * titleScore + 0.3 * yearScore
titleScore = max( sim(query, candidate.title), sim(query, candidate.originalTitle) )   # 0..1
sim        = 1 - levenshtein(normA, normB) / max(len)                                  # normalisé
yearScore  = 1.0 (exact) | 0.8 (±1 an) | 0.5 (année inconnue d'un côté) | 0.0 (mismatch)
filtre type (movie vs tv) AVANT scoring

AUTO-MATCH si les TROIS :
  confidence ≥ 0.85
  titleScore ≥ 0.90
  marge ≥ 0.05 sur le 2e candidat (sinon → revue manuelle)
tri : confidence desc, puis voteCount desc
```

`core/common/StringNormalizer.kt` (Android) : articles (FR+EN, dont `des`, `l'`) → accents (NFD +
suppression des marques combinantes) → **`[^\p{L}\p{N}\s] → ""`** → uppercase → trim.

---

## 3. Changements à implémenter

### 3.1 `app/utils/string_normalizer.py`

**(A) Aligner `normalize_for_sorting` sur l'Android** — ajouter la suppression des caractères
spéciaux + collapse d'espaces (garder le `.lower()` côté appelant, ou normaliser en lower ici de
façon cohérente). Ajouter l'article `des`.

```python
def normalize_for_sorting(title: str) -> str:
    # 1) articles de tête (the/a/an/le/la/les/l'/un/une/des)
    # 2) accents : unicodedata NFKD + drop combining
    # 3) NEW: retirer tout sauf lettres/chiffres/espaces  -> re.sub(r"[^\w\s]", " ", s) puis \w sans _;
    #         préférer: re.sub(r"[^0-9a-zA-ZÀ-ɏ\s]", " ", s) AVANT strip accents,
    #         ou après strip accents: re.sub(r"[^0-9a-z\s]", " ", s.lower())
    # 4) NEW: collapse espaces -> re.sub(r"\s+", " ", s).strip()
```

**(B) Nettoyeur de titre durci** — soit corriger `parse_title_and_year`, soit créer
`clean_title(raw) -> (title, year)` qui devient la fonction de référence du sync. Comportement :

1. **Préfixes** (en boucle, casse libre) : `|...|`, `[...]`, et préfixe langue/pays
   `^(VOSTFR|MULTI|TRUEFRENCH|VFF|VFQ|VFI|VFB|VF|VO|FR|EN|US|UK|FRA|NF|SC|AR|...|[A-Z]{2,4})\s*[-|:]\s*`.
   → couvre `Fr - `, `FRA - `, `VOSTFR - `, multiples préfixes empilés.
2. **Séparateurs scène** : si le nom ne contient pas d'espace mais des `.`/`_`, les convertir en
   espaces (`Le.Cygne.Noir.2010` → `Le Cygne Noir 2010`). Ne pas casser les sigles courants.
3. **Année n'importe où** : `(\b(19|20)\d{2}\b)` — parenthésée OU nue ; extraire la **dernière**
   occurrence plausible, retirer du titre.
4. **Tags qualité/langue retirés PARTOUT** (pas seulement en fin), insensibles à la casse :
   `1080p 720p 2160p 4K UHD HD SD HDR HDLIGHT HQ LQ x264 x265 H264 HEVC WEB WEBRIP WEB-DL BLURAY
   BRRIP DVDRIP AC3 DTS MULTI VOSTFR VOST VFF VFQ VFI VFB VF VO TRUEFRENCH SUBFRENCH`.
5. **Crochets / accolades / parenthèses non-année** retirés partout, en boucle.
6. **Nettoyage final** : collapse espaces, retirer tirets/`:`/séparateurs orphelins en bord, trim.
   Si vide → garder le `raw` original (ne pas renvoyer `"Unknown"` qui crée des collisions).

> Conserver `parse_title_year_and_suffix` pour la désambiguïsation de versions (Jellyfin) ; mais le
> **sync doit appeler le nouveau `clean_title`** (remplacer `parse_title_and_year` en
> `sync_worker.py:88,209,322`).

### 3.2 `app/services/tmdb_service.py` — scoring façon ScraperMatcher

Réécrire `_best_match` (`:274-325`) :

- Récupérer le titre **et** l'`original_title` (movie) / `original_name` (tv) de chaque résultat.
  `titleScore = max(sim(query, r_title), sim(query, r_original))`.
- `sim()` : sur titres normalisés via `normalize_for_sorting`. Utiliser `rapidfuzz` :
  `max(fuzz.ratio, fuzz.token_set_ratio) / 100` (ratio = proche Levenshtein ; token_set = robuste
  aux mots en plus / ordre). rapidfuzz est déjà une dépendance.
- `yearScore` : **1.0 / 0.8 / 0.5 / 0.0** (remplacer le plancher 0.85 actuel par **0.0** sur
  mismatch — c'est le garde-fou anti-homonyme).
- `confidence = 0.7*titleScore + 0.3*yearScore`.
- Trier par `confidence` desc puis `vote_count` desc.
- **Auto-match** (3 conditions) : `confidence ≥ 0.85` ET `titleScore ≥ 0.90` ET
  `marge(top, 2e) ≥ 0.05`. Sinon renvoyer `None` (ou un statut « ambigu » pour revue).
- Exposer les **constantes** en tête de module (`AUTO_MATCH_THRESHOLD=0.85`,
  `MIN_TITLE_SCORE=0.90`, `MIN_MARGIN=0.05`, `TITLE_WEIGHT=0.7`, `YEAR_WEIGHT=0.3`).

### 3.3 `app/workers/enrichment_worker.py` — stratégie de fallback

Dans `_fetch_movie_data` / `_fetch_series_data` (scénario 4, `:50-55` et `:86-91`), si pas
d'auto-match :
1. **Retry sans année** (l'année source est parfois fausse/absente).
2. **Retry en `language=en-US`** (beaucoup de VOD ont un titre FR qui matche mieux sur l'original).
   → ajouter un paramètre `language` optionnel à `search_movie`/`search_tv`.
3. **`/search/multi`** en dernier recours (titre seul).
4. Si toujours rien : enregistrer le **meilleur score obtenu** (même < seuil) dans
   `tmdb_match_confidence` et passer en `skipped` (utile pour une future revue manuelle / UI admin),
   au lieu de tout perdre.

> Garder `CONCURRENCY=8` et `ENRICHMENT_DAILY_LIMIT` ; les fallbacks consomment plus d'appels TMDB
> mais seulement sur les items non matchés. Compter ces appels comme aujourd'hui.

### 3.4 `app/utils/metrics.py` — mesurer le taux de match

Ajouter un compteur Prometheus :
```python
tmdb_match_total = Counter("plexhub_tmdb_match_total", "TMDB enrichment outcomes",
                           ["media_type", "result"])  # result = matched | nomatch | ambiguous
```
Incrémenter dans `_apply_enrichment_results`. Permet de suivre le gain avant/après sur `/metrics`.

---

## 4. Corpus de tests (pytest) — cas réels prouvés

Créer `tests/test_title_cleaning.py` et étendre `tests/test_tmdb_service_mocked.py`.

**`clean_title` — doit produire un titre propre + année :**

| Entrée | Attendu (title, year) |
|---|---|
| `Avatar (FR)` | `("Avatar", None)` |
| `Avatar (2009) (FR)` | `("Avatar", 2009)` |
| `VOSTFR - Dune` | `("Dune", None)` |
| `Fr - Le Parrain` | `("Le Parrain", None)` |
| `FRA - Le Parrain (1972)` | `("Le Parrain", 1972)` |
| `Spider-Man : No Way Home (2021) MULTI 1080p` | `("Spider-Man : No Way Home", 2021)` |
| `Le.Cygne.Noir.2010.MULTI.1080p` | `("Le Cygne Noir", 2010)` |
| `Oppenheimer 2023` | `("Oppenheimer", 2023)` |
| `John Wick [4K] [MULTI]` | `("John Wick", None)` |
| `\|VM\| Tulsa King  (2022)` | `("Tulsa King", 2022)` |
| `Black Widow (2021) [FHD MULTi-SUBAR]` | `("Black Widow", 2021)` |
| `Skarb narodow-Ksiega tajemnic (2007) [PL]` | `("Skarb narodow-Ksiega tajemnic", 2007)` |

**`_best_match` (mock respx) :**
- titre exact + année exacte → auto-match (conf 1.0).
- titre exact + **mauvaise** année (ex. Hairspray 1988 vs 2007) → **pas** d'auto-match (yearScore 0).
- titre via `original_title` (query FR, candidat anglais) → match.
- deux homonymes (même titre, années inconnues, scores proches) → **pas** d'auto-match (marge < 0.05).
- `token_set_ratio` : `"No Way Home Spider-Man"` matche `"Spider-Man: No Way Home"`.

Extraire d'autres cas réels :
```sql
SELECT type,title,year FROM media WHERE (tmdb_id IS NULL OR tmdb_id='') AND type IN ('movie','show');
```

---

## 5. Ordre d'implémentation & DoD

1. `string_normalizer.py` : aligner `normalize_for_sorting` + `clean_title` durci + tests unitaires
   (table §4) **verts**.
2. Brancher le sync sur `clean_title` (`sync_worker.py:88,209,322`).
3. `tmdb_service.py` : scoring pondéré + `original_title` + marge + `vote_count` + tests respx.
4. `enrichment_worker.py` : fallback (sans année → en-US → multi) + record best score.
5. `metrics.py` : compteur `plexhub_tmdb_match_total`.
6. **Re-enrichir** : repasser les `EnrichmentQueue.status='skipped'` (et/ou `Media` sans `tmdb_id`)
   en `pending` pour rejouer avec le nouveau moteur (penser au cap quotidien).

**DoD** : `pytest -v` vert · serveur boote (`uvicorn app.main:app`) · `GET /api/health` 200 ·
migrations idempotentes inchangées · mesurer le taux via `plexhub_tmdb_match_total` avant/après sur
un échantillon. Mettre à jour le bandeau de `CLAUDE.md` si §5.2 (enrichissement) change
(`/sync-context`).

## 6. Hors périmètre (validé avec toi)
Pas de re-enrichissement automatique sur churn ni d'endpoint admin de re-scrape ciblé dans ce lot
(étape 3 du plan global) — à faire plus tard si besoin.
```

> Référence source : `PlexHubTV/domain/.../usecase/ScraperMatcher.kt` et
> `PlexHubTV/core/common/.../StringNormalizer.kt`.

---

## 7. Addendum (demandé en plus du plan — IMPLÉMENTÉ)

### 7.1 Cache de scrape PERSISTANT (ne plus rappeler TMDB pour un même film/série)
Les caches `tmdb_service` (`_search_cache` 24h, `_imdb_find_cache` 7j) sont **en mémoire** → perdus
au redémarrage, et les détails n'étaient pas cachés. Ajout d'un cache **persistant SQLite** :
- Table `tmdb_scrape_cache` (migration **010**) : `cache_key` PK =
  `f"{media_type}|{normalize_for_sorting(titre)}|{year or ''}"`, `result` (matched/ambiguous/nomatch),
  `tmdb_id`/`imdb_id`/`confidence`, `payload` (JSON de `TMDBEnrichmentData`), `fetched_at`.
- `app/services/scrape_cache_service.py` : `make_key` / `get` (TTL **30 j** match · **3 j** négatif) /
  `put`. Le worker (scénario 4) **consulte le cache AVANT TMDB** ; sur miss il résout puis `put`.
  → même titre à travers comptes/qualités **et** après redémarrage = **0 appel TMDB**.
- Idempotent ; aucun impact sur le cap quotidien (les hits ne consomment rien).

### 7.2 Critère de désambiguïsation par le RÉSUMÉ Xtream
Quand titre+année laissent un **doute** (top-2 dans la marge `MIN_MARGIN`), on départage avec le
**résumé fourni par Xtream** (`media.summary`) comparé à l'`overview` de chaque candidat TMDB :
- `_best_match` calcule `summary_sim = token_set_ratio(résumé_xtream, overview_candidat)` pour les
  candidats proches ; si un candidat dépasse `SUMMARY_MIN_SIM=0.30` **et** distance le 2ᵉ de
  `SUMMARY_TIEBREAK_MARGIN=0.10` → auto-match ce candidat. Sinon → `ambiguous` (pas de faux match).
- Le résumé Xtream est porté jusqu'au matcher via `EnrichmentQueue.existing_summary` (migration **010**,
  peuplé à l'enqueue au sync et au rescrape), passé en `search_movie/search_tv(..., summary=…)`.

### 7.3 État livré
Migration courante = **010**. Fichiers : `string_normalizer.py` (`clean_title` + `normalize_for_sorting`
durci), `sync_worker.py` (branché sur `clean_title`), `tmdb_service.py` (scoring pondéré + original_title
+ marge + vote_count + tie-break résumé + `language`/`search_multi`), `enrichment_worker.py` (cache +
fallback + record best score), `scrape_cache_service.py`, `metrics.py` (`plexhub_tmdb_match_total`),
migration 010 + modèles. Tests : `test_title_cleaning.py`, `test_tmdb_service_mocked.py` (scoring +
tie-break), `test_enrichment_scraping.py` (cache + fallback). **Reste à faire (op runtime)** : repasser
les `EnrichmentQueue.status='skipped'` / `Media` sans `tmdb_id` en `pending` pour rejouer (étape §5.6).
