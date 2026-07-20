# ADR 0003 — Enrichissement double-fournisseur (TMDB + OMDb) & `display_rating` mélangé

- Statut : accepté (sync-specialist, Wave 2 du `/refacto` « dual-provider enrichment »)
- Date : 2026-07-20
- Contexte source : `docs/plans/2026-07-20-omdb-rating-enrichment-design.md` (design Phase 1), companion `docs/plans/2026-07-17-omdb-id-consistency-validator-design.md` (primitives OMDb déjà livrées : client / cache / budget, migration 022)
- Portée : backend PlexHub (`app/workers/enrichment_worker.py`, `app/utils/rating_blend.py`, `app/services/omdb_service.py`) — **aucun changement de schéma** (toutes les colonnes existent déjà)

## Contexte

Jusqu'ici OMDb n'était consulté qu'en **tie-break** d'un match TMDB à faible confiance
(`_omdb_contradicts`, `confidence < 1.0`), et ses `imdb_rating`/`imdb_votes` étaient
**jetés** — ces deux colonnes `media` n'étaient peuplées que par `nfo_import_service`. Par
ailleurs `display_rating` valait la note TMDB brute (`scraped_rating = display_rating =
vote_average`). Deux besoins produit :

1. `imdb_rating`/`imdb_votes` enrichis **systématiquement** via OMDb à chaque enrichissement,
   et OMDb **complète** les champs manqués par TMDB (fill-missing) ; si TMDB échoue totalement
   (`nomatch`), tentative d'un scrape **OMDb-par-titre**.
2. `display_rating` recalculé comme **blend(imdb, tmdb)**, une note IMDb étant récupérée via
   OMDb quand elle manque.

Deux forces structurantes : (a) l'always-fetch OMDb ne doit pas sérialiser la latence réseau
sur un batch de 200 items ; (b) asserter une **nouvelle identité** à partir d'un simple titre
OMDb (souvent renvoyé en anglais) est un risque asymétrique du simple « garder un match ».

## Décision

1. **Une seule requête OMDb par item, dans la phase concurrente `_resolve`** (sous le
   `Semaphore(CONCURRENCY=8)` existant). `FetchResult` gagne `omdb` / `omdb_put` /
   `omdb_identity`. Cette **unique** requête sert **à la fois** le tie-break de contradiction
   (`confidence < 1.0`) **et** l'enrichissement de note — `_omdb_contradicts` est refactoré pour
   accepter le `fr.omdb` **pré-récupéré** (jamais de 2ᵉ appel : exigence 6). Le scénario 3
   (`existing_imdb` sans TMDB) saute toujours TMDB mais récupère désormais OMDb par cet
   imdb_id pour les notes.

2. **D-BLEND — `display_rating` devient un mélange, plus `vote_average`.** `blend_rating(imdb,
   tmdb)` : les deux présents → `(imdb+tmdb)/2` ; un seul → celui-là ; aucun → inchangé (une
   valeur `<= 0`/`NULL` compte comme absente, miroir Android `blendRating`). En base,
   `display_rating` est écrit via `blend_display_rating_case(COALESCE(Media.imdb_rating,
   :new_imdb), COALESCE(Media.tmdb_rating, :new_tmdb), Media.display_rating)` — **calculé
   depuis les colonnes persistées post-écriture**, donc **reproductible en SQL**. `scraped_rating`
   reste = `vote_average` TMDB brut (enregistrement durable). `imdb_rating`/`imdb_votes` et
   `tmdb_rating`/`tmdb_votes` sont écrits en **COALESCE fill-missing** (jamais d'écrasement
   d'une valeur NFO plus riche).

3. **D-IDENTITY — politique d'écriture d'identité OMDb-par-titre asymétrique.** Sur un
   `nomatch` TMDB **frais** (`from_cache is False` uniquement — un nomatch déjà en cache est
   déjà négativement caché côté TMDB, aucun appel OMDb-titre), `search_by_title` est classé :
   - **STRONG** (identité autorisée : `imdb_id` + `unification_id` + `history_group_key` +
     métadonnées + notes, tout en fill-missing) ssi **année EXACTE** (0 tolérance, année
     requise) **ET** `sim >= 0.90` **ET** type OMDb concordant (movie↔movie, series↔show).
   - **Weak** (`0.60 <= sim < 0.90`, ou année non exacte, ou année absente) : métadonnées +
     notes fill-missing, **aucune identité**.
   - **Discard** (`sim < 0.60`) : rien écrit (traité comme OMDb-nomatch).
   Asymétrie **délibérée** vs `_omdb_contradicts` (qui downgrade seulement si année-gap > 1
   **ET** sim < 0.55) : garder un match ne demande que l'absence de contradiction ; **asserter
   une identité neuve** depuis un titre nu demande une barre bien plus haute (année-exacte +
   sim élevé + type), car OMDb renvoie fréquemment le titre anglais — un titre seul n'est
   jamais concluant. Erre vers metadata-only (sûr, self-healing) plutôt que vers une fausse
   identité (qui mal-grouperait un titre dans le dossier Plex d'un autre).

4. **Budget OMDb par-item + fail-open intégral.** Chaque requête est gardée par
   `omdb_service.get_request_count() >= settings.OMDB_DAILY_LIMIT` ; budget épuisé /
   non-configuré / not_found / exception → OMDb ignoré, **le résultat TMDB est conservé** (une
   absence de signal ne dégrade jamais un match normal). `run()` réinitialise le compteur OMDb
   à côté de celui de TMDB.

5. **Dédup unique et autoritaire de l'écriture cache OMDb.** L'ancien couple in-loop
   get/call/put + `omdb_batch_cache` du tie-break disparaît (la requête est désormais unique par
   item dans `_resolve`). L'écriture `omdb_scrape_cache` est dédupliquée par `imdb_id` via un
   **seul** set `omdb_put_keys` dans la phase apply — deux items d'un même batch partageant un
   imdb_id produisent **un seul** INSERT (jamais d'`UNIQUE constraint failed:
   omdb_scrape_cache.imdb_id`, qui bloquait autrefois l'enrichissement en permanence).

6. **Recompute de fin de run (SQL-only, défensif).** Après les deux phases,
   `recompute_display_rating_stmt()` + commit (via `commit_with_retry`) soigne les
   `display_rating` clobberés par un flip de `content_hash` (le blend est recalculable depuis
   les colonnes durables `imdb_rating`/`tmdb_rating`), **avant** la génération et
   `unified_group_service.rebuild_all`. Enveloppé `try/except` : un échec ici ne fait jamais
   planter le run.

## Alternatives écartées

- **Requête OMDb dans la phase apply (sérielle), comme l'ancien tie-break** — sérialiserait la
  latence réseau de l'always-fetch sur les 200 items d'un batch. Écarté au profit de la phase
  concurrente `_resolve`.
- **`display_rating = vote_average` conservé, note IMDb dans une colonne séparée seulement** —
  ne répond pas au besoin produit (note affichée = mélange). Écarté.
- **`?s=` (search list) pour OMDb-par-titre** — double les appels (liste + détail par hit).
  `?t=` est un seul appel et laisse OMDb choisir le meilleur ; OMDb-titre étant un fallback
  long-shot (TMDB, multilingue, a déjà échoué), le seul appel le moins cher gagne.
- **Nouvelle table de cache négatif pour les titre-miss** — inutile : OMDb-par-titre ne tourne
  que sur un `nomatch` TMDB **frais**, donc le titre-miss est déjà négativement caché côté
  `tmdb_scrape_cache` (TTL négatif 3 j) et court-circuite `_resolve` aux runs suivants. Aucun
  état neuf.
- **Année ±1 pour STRONG (comme le tie-break)** — trop permissif pour une écriture d'identité.
  Année-exacte retenue (hard gate).

## Conséquences

- **+** Notes IMDb systématiquement enrichies ; `display_rating` = mélange reproductible en
  SQL et self-healing (recalculable depuis les colonnes durables) ; OMDb-titre récupère des
  items que TMDB rate, sans jamais fausser une identité ; une seule requête OMDb par item ;
  dédup cache préservée ; **aucune migration** (contrat Android `MediaResponse` inchangé — mêmes
  formes, seules les valeurs de `display_rating` diffèrent).
- **−** Le chemin d'enrichissement dépend désormais d'un **second fournisseur externe (OMDb)**
  avec son budget par-item ; `display_rating` ne vaut plus `vote_average`/`tmdb_rating` (un
  mainteneur futur qui compare les chiffres doit connaître le blend). Budget OMDb in-process
  (résiduel CR-F03) : un backfill non-master concurrent au pipeline peut dépasser le quota
  agrégé — mais les écritures restent idempotentes (fill-missing + upsert par PK), seul le
  budget est gaspillé, jamais la donnée corrompue.
- **Gotcha dossier Plex** : une identité STRONG issue d'un `nomatch`→`imdb://` change
  l'`unification_id` ; `LocalStorage` ne réécrit jamais un NFO/poster existant. Risque faible
  (l'item passe d'un dossier `title_` vers un **nouveau** dossier `imdb://` sans NFO
  pré-existant, l'ancien étant orphan-pruné), mais signalé.

## Références

- Design : `docs/plans/2026-07-20-omdb-rating-enrichment-design.md` (§Locked decisions,
  §Thresholds, §Contracts C3, §Risks — corps de décision de cette ADR)
- Primitives Wave 1 : `app/utils/rating_blend.py` (`blend_rating` /
  `blend_display_rating_case` / `recompute_display_rating_stmt`), `app/services/omdb_service.py`
  (`OMDbData.imdb_id`, `search_by_title`)
- Code cité : `app/workers/enrichment_worker.py` (`_resolve` / `_attach_omdb` /
  `_fetch_omdb_by_id` / `_classify_omdb_title` / `_omdb_contradicts` /
  `_apply_enrichment_results` / `run`), `app/utils/unification.py::calculate_unification_id`,
  `app/services/omdb_scrape_cache_service.py`
- Tests : `tests/test_enrichment_scraping.py` (fetch `_resolve`), `tests/test_enrichment_guard.py`
  (apply : tie-break, notes, blend, identité, dédup cache), `tests/test_rating_blend.py` (parité
  SQL↔fn)
