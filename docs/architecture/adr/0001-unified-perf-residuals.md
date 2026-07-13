# ADR 0001 — Résiduels perf des endpoints media (CR-P07 / CR-P04 / CR-P01)

- **Statut :** Accepté (périmètre « les 3 » validé par l'utilisateur — vaut approbation `needs-approval` pour la migration de schéma de l'étape 3).
- **Date :** 2026-07-12
- **Contexte issu de :** `docs/audit/cleanroom-2026-07-11/50-perf.md`
- **Périmètre code :** `app/api/media.py`, `app/services/media_service.py` (+ étape 3 : `app/models/database.py`, `app/db/migrations.py`, un builder de service, un hook workers).
- **Commande :** `/refacto` (refonte sûre multi-étapes, pas de big-bang).

## Décision

Traiter les 3 résiduels perf en **3 étapes isolées, chacune verte et committée avant la suivante**, de la plus sûre à la plus risquée. Les contrats publics (routes, schémas Pydantic camelCase) restent **inchangés** aux étapes 1 et 3 ; l'étape 2 est **purement additive**.

---

### Étape 1 — CR-P07 : sérialisation single-pass (sûr, sortie identique)

**Problème.** Les endpoints liste construisent `XxxResponse.model_validate(...)` **puis** FastAPI re-valide + re-sérialise contre `response_model` → deux passes Pydantic par requête (jusqu'à 5000 lignes × ~60 champs).

**Décision.** Construire le modèle une seule fois et renvoyer un `JSONResponse(content=model.model_dump(mode="json", by_alias=True))`. Quand la valeur de retour est un `Response`, FastAPI **saute sa passe de sérialisation** (plus de re-validation). On **garde `response_model=`** sur le décorateur → l'OpenAPI reste typé (ne régresse pas CR-C03).

**Invariance.** Défauts identiques à FastAPI (`by_alias=True`, pas d'`exclude_*`) + `JSONResponse` = même classe de réponse par défaut (aucun `default_response_class` custom) → JSON **octet-pour-octet identique**. Champs des modèles = scalaires/listes JSON-natifs (aucun `datetime`). Garde : tests de caractérisation comparant la sortie.

**Périmètre.** Les 6 endpoints liste (`/movies`, `/shows`, `/episodes`, `/movies/unified`, `/shows/unified`, `/episodes/unified`). Les endpoints item unique restent inchangés (coût négligeable).

---

### Étape 2 — CR-P04 : pagination keyset OPTIONNELLE (additif, non-cassant)

**Problème.** `OFFSET n` sur SQLite parcourt puis jette les `n` premières lignes (coût O(offset)) sur les listes brutes.

**Décision.** Ajouter un paramètre **optionnel** `cursor` aux listes brutes (`/movies`, `/shows`, `/episodes`). Quand il est fourni ET que `sort ∈ {added_desc, added_asc}`, l'OFFSET est remplacé par un seek `WHERE (added_at, <PK 4 cols>) </> :cursor`. `Media` n'a **pas** d'id auto-incrément (PK = `(rating_key, server_id, filter, sort_order)`) et `added_at` n'est pas unique → l'ordre total (et donc le curseur) porte `added_at` **+ la PK composite complète** comme tie-break déterministe (row-value comparison SQLite). `offset` reste pleinement fonctionnel (défaut) et **`has_more` garde exactement la formule `(offset+limit) < total`** (inchangé pour tous). Le champ réponse **`next_cursor` est émis sur tout tri récence dès qu'une page est pleine** (`len==limit`), **même sans curseur entrant** — un client keyset démarre donc sans curseur et suit `next_cursor` jusqu'à `null`. Additif : les clients offset l'ignorent. Les tris non-récence (`title_*`, `rating_desc`, `year_desc`) ignorent le curseur (retombent sur OFFSET, `next_cursor=null`).

**Contrainte.** `/unified` est **hors périmètre keyset** : il pagine un slice mémoire déjà agrégé, pas un OFFSET SQL. Les tris non-mono-colonne (`rating_desc`, `year_desc`, `title_*` — clés non uniques) ignorent le curseur et retombent sur OFFSET (documenté). Bénéfice réel effectif **quand l'app Android adopte `cursor`** ; côté backend c'est prêt et non-cassant d'ici là.

---

### Étape 3 — CR-P01 : table dénormalisée `media_group` (vague isolée, migration `needs-approval`)

**Problème.** `get_unified_list` charge **tout** le catalogue autorisé, agrège (`aggregate_movies` + `_converge`) et trie à chaque appel ; `limit/offset` s'appliquent **après**. Atténué en Vague B par un cache TTL (45 s) mais le premier chargement par fenêtre reste O(catalogue).

**Contrainte structurelle (vérifiée `aggregation_service.py:165-268`).** Le `group_key` **convergé** ne peut PAS se calculer ligne-à-ligne : Pass A (`_merge_by_shared_ids`, union-find sur ids partagés) et Pass B (`_absorb_title_groups`) sont des opérations **sur l'ensemble complet**. La table doit donc être **construite par un passage d'agrégation complet** (le même code), pas incrémentalement.

**Décision.**
1. **Nouvelle table `media_group`** (migration **017**, idempotente `CREATE TABLE IF NOT EXISTS`, en fin de `run_migrations()`), une ligne par groupe convergé :
   - clés : `media_type`, `group_key`, `best_server_id`, `best_rating_key` ;
   - carte dénormalisée depuis la *best row* (titre canonique, année, summary, genres, thumb/art, imdb/tmdb, rating, cast, is_adult, + 13 colonnes NFO) ;
   - tri : `sort_added_at` (= `best.added_at`, clé de tri actuelle) ;
   - `version_count` ;
   - aides-filtre « any-member » : `search_blob` (titres membres concaténés, lower), `genres_blob`, `years_csv` — pour **préserver exactement** la sémantique actuelle (un groupe apparaît si *n'importe quel* membre matche search/genre/year, filtré **avant** groupement aujourd'hui).
   - Un `build_stamp` (ms) par ligne pour tracer la fraîcheur.
2. **Table de mapping `media_group_member`** (`media_type`, `group_key`, `server_id`, `rating_key`) pour reconstruire `versions[]` de la page hydratée (join borné à la page).
3. **Builder `unified_group_service.rebuild(db, media_type)`** : streame le catalogue, exécute `aggregate_movies`/`aggregate_series` (déjà offloadé `asyncio.to_thread`), remplace le contenu de `media_group`/`media_group_member` en une transaction (idempotent, retry via `run_with_retry`). Bornage mémoire par `media_type`.
4. **Hook** : appelé en fin de pipeline planifié (`scheduled_sync_enrich_generate`, après enrichissement — même endroit que la génération Plex qui agrège déjà) + après un sync manuel. **Jamais au boot bloquant.**
5. **Réécriture `get_unified_list`** : si `media_group` est peuplée → **pagination côté SQL** (filtres `search`/`genre`/`year` sur les blobs, `ORDER BY sort_added_at DESC`, `LIMIT/OFFSET`), puis hydratation **de la page seule** (chargement des membres des groupes de la page pour `versions[]`). **Fallback** sur le chemin live actuel (aggregate + cache TTL) **si la table est vide** (DB fraîche avant 1er build) → correction garantie au démarrage à froid.

**Compromis de fraîcheur.** La liste `/unified` reflète le **dernier build** (fin de pipeline), au lieu du live+cache 45 s. Cohérent avec la bibliothèque Plex générée (qui agrège au même moment). Le variant `?unification_id=` (`get_unified_group`) **reste live** (déjà indexé/efficace) — inchangé.

**Contrat public.** Réponse `UnifiedMediaListResponse` **inchangée** (mêmes champs camelCase, mêmes cartes). Seule la **source** des groupes change (précalculée vs live) → couvert par tests de parité live-vs-table.

## Pièges §9 touchés

- **Migrations idempotentes** en fin de chaîne (017) — `CREATE TABLE/INDEX IF NOT EXISTS`, rejouables.
- **`asyncio.to_thread`** pour l'agrégation CPU du builder (déjà le cas).
- **`run_with_retry`/WAL** pour l'écriture du builder (writer concurrent du sync).
- **Master-worker** : le build tourne dans le pipeline planifié (master seul), pas au boot de chaque worker.

## Conséquences

- **+** Endpoints browse `/unified` : lecture O(page) au lieu de O(catalogue) ; mémoire bornée.
- **+** Listes brutes : pagination profonde O(page) via curseur (quand l'app l'adopte).
- **+** Moins de CPU de sérialisation (single-pass) sur toutes les listes.
- **−** Une table + un builder à maintenir ; fraîcheur `/unified` alignée sur le pipeline (documentée).
- **−** ~2 nouvelles tables (additives, aucune donnée existante touchée).

## Validation (DoD par étape)

`pytest -v` vert · `GET /api/health` 200 · migration idempotente (double run) · `ruff check` vert · OpenAPI cohérent · parité de sortie prouvée par test.
