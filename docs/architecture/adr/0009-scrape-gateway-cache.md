# ADR 0009 — Passerelle de scraping : cache versionné et accès fournisseur

- Statut : **proposé** (2026-10-18, lot B1, spec seulement)
- Date : 2026-10-18
- Plan source : `docs/10-prd-scrape-gateway.md` (FR-007 → FR-036) ; contrat d'entrée
  `plexhubTMM/docs/15-contrat-b1-backend.md`
- Portée : nouveau router `app/api/scrape_v1.py`, nouveaux services `scrape_keys`, `scrape_gateway_cache`,
  `scrape_mappers`, `scrape_gateway_service`, schémas `app/models/scrape_schemas.py`, ajouts **additifs** à
  `tmdb_service` et `omdb_service`, migration 029, réglages `SCRAPE_*`
- ADR recoupés : 0003 (enrichissement double fournisseur), 0005 (scraper manuel : `_score_results`/`_verdict`,
  `count_requests`), 0008 (scopes)

## D1 — Faits du code qui contraignent la conception

1. `tmdb_scrape_cache` (worker) n'a ni langue, ni version, ni candidats pour `ambiguous` ; `omdb_scrape_cache`
   ignore son TTL négatif et écrit `not_found` sur une panne. Garde-fou du lot : **ni l'un ni l'autre ne change**.
2. `find_by_imdb_id` et `omdb_service.get_by_imdb_id` avalent toute erreur (`None`) ; `get_movie_details` /
   `get_tv_details` ne testent pas `is_configured` et figent la langue à `TMDB_LANGUAGE`. Ils sont utilisés par le
   worker : leur comportement ne doit pas bouger.
3. `tmdb_service._request` : 4 tentatives, attente sur `Retry-After` non plafonnée, exceptions httpx brutes dont
   le message contient l'URL avec `api_key`.
4. Les budgets `ENRICHMENT_DAILY_LIMIT`/`OMDB_DAILY_LIMIT` sont des compteurs en mémoire remis à zéro par chaque
   passe du worker ; seul OMDb a un garde dans le service.
5. Le dépôt n'a aucun single-flight par clé ; le motif le plus proche est le dict de tâches de
   `unified_group_service` (`_pending_rebuild_tasks`).
6. Un seul process uvicorn (pièges 18c et 7) : l'état en mémoire est l'état du service.

## D2 — Décisions

### D2.1 Table `scrape_cache` (migration 029)

```sql
CREATE TABLE IF NOT EXISTS scrape_cache (
    family          TEXT    NOT NULL,
    key_version     INTEGER NOT NULL,
    language        TEXT    NOT NULL,
    cache_key       TEXT    NOT NULL,
    result          TEXT    NOT NULL,
    schema_version  INTEGER NOT NULL,
    payload         TEXT,
    fetched_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    PRIMARY KEY (family, key_version, language, cache_key)
);
CREATE INDEX IF NOT EXISTS ix_scrape_cache_expires_at ON scrape_cache (expires_at);
```

Modèle ORM `ScrapeCacheEntry` au DDL identique (types `Text`/`Integer`, pas de `BigInteger` pour rester
octet-pour-octet avec la migration ; PK composite ; `Index("ix_scrape_cache_expires_at", "expires_at")`). Aucune
colonne n'a de défaut : toute écriture passe par le code de la passerelle, qui fournit toutes les valeurs (écart
X18 au contrat, qui suggérait des défauts SQL). Le nom `scrape_cache` est celui du contrat ; à ne pas confondre avec
le module **existant** `scrape_cache_service` (cache TMDB du worker, table `tmdb_scrape_cache`) — le module de la
passerelle s'appelle `scrape_gateway_cache` pour cette raison.
Test de convergence sur le modèle de `tests/test_migration_027.py` (base fraîche par `create_all`, base ancienne
sans la table, migration rejouée, `PRAGMA table_info` et `PRAGMA index_list` comparés).
Une seule table pour toutes les familles (contrat § 11 Q1) : purge et stats en une requête, schéma identique au
cache local de plexhubTMM.

### D2.2 Clés canoniques — module pur `app/services/scrape_keys.py`

- `CacheKey(family, key_version, language, key)` figé ; `to_text()` → `"{v}|{lang}|{key}"` ; `parse(text)` applique
  la règle du contrat § 4.2 (famille par premier segment de `key`, `movie`/`show` : 5 segments = `match`, 4 =
  détails ; `tmdbId` entier positif ; `season` : `season|{id}|{n}` ; `omdb|tt…` ; `find|imdb|…`/`find|tvdb|…` ;
  `language` `-` pour `omdb`/`find`, sinon `^[a-z]{2}-[A-Z]{2}$`) et lève `InvalidCacheKey` sinon.
- `match_key(media_type, title, year)` appelle **`scrape_cache_service.make_key`** (import, pas de copie) : toute
  évolution de `normalize_for_sorting` passe donc par ce seul point, et impose `KEY_VERSION += 1`.
- `KEY_VERSION = 1`, `SCHEMA_VERSION = 1` déclarés ici, nulle part ailleurs.
- Vecteurs d'or : les 2 existants + au moins accents, article, chiffres seuls, Unicode pleine chasse (`｜`),
  apostrophe, année absente, et le piège d'analyse `1|fr-FR|movie|603|` (titre « 603 » sans année, 5 segments,
  famille `match`) contre `1|fr-FR|movie|603` (4 segments, détails) — l'analyse fait un `split("|")` **sans**
  filtrer les segments vides ; exportés dans un fichier de test lisible par l'oracle de plexhubTMM
  (`tests/fixtures/scrape_key_vectors.json`, généré par le test, jamais écrit à la main).

### D2.3 Expiration et TTL

`expires_at = fetched_at + TTL(result)` calculé à l'écriture ; succès = `matched`/`found`
(`SCRAPE_TTL_SUCCESS_DAYS`, 30, borné 1..365) ; négatif = `ambiguous`/`nomatch`/`not_found`
(`SCRAPE_TTL_NEGATIVE_DAYS`, 3, borné 1..30). Bornage dans le service (motif `epg_sync_service` : la config lit,
le service borne). Lecture : servie seulement si `expires_at > now`, `key_version == KEY_VERSION` et
`schema_version == SCHEMA_VERSION`. Un payload illisible ⇒ absent + WARNING sans contenu.

### D2.4 Payload

Le payload est le corps **hors enveloppe** déjà normalisé (sortie des mappers, D2.9), sérialisé en JSON trié
(`sort_keys`) : un `hit` rend exactement ce qu'un `miss` aurait rendu. Les familles `movie`/`show` stockent les
détails **sans** les notes OMDb ni `omdbStatus` ; la composition (D2.10) se fait à chaque réponse. `nomatch` et
`not_found` : `payload` NULL. `ambiguous` : `{"confidence", "candidates"}`. `matched` (famille `match`) :
`{"confidence", "tmdbId"}` **seulement** — les détails vivent dans l'entrée `movie|{id}`/`show|{id}` ; au `hit` de
`match`, ils sont lus dans cette entrée si elle est fraîche, sinon redemandés **dans le même vol** et restockés
(`cache: "miss"`), et l'enveloppe porte `expiresAt = min(expiration match, expiration détails)` (PRD FR-018, X17). `omdb` : `{"imdb", "rottenTomatoes",
"metacritic"}` déjà normalisés en `Rating`.

### D2.5 Écritures

Toujours **après** l'appel fournisseur, en dehors de toute transaction ouverte, via `write_with_retry` (session
neuve par tentative, piège 8), en `INSERT … ON CONFLICT (family, key_version, language, cache_key) DO UPDATE`.
`/match` `matched` écrit l'entrée `match` **et** l'entrée `movie`/`show` dans la même transaction (tout ou rien).
Un échec d'écriture après un fetch réussi est journalisé et n'empêche pas la réponse (`cache: "miss"`). Les
lectures utilisent l'engine du chemin requête (`get_db`), jamais `worker_engine`.

### D2.6 Accès fournisseur : méthodes strictes additives

Les méthodes existantes ne changent pas. Ajouts :

- `tmdb_service` : `fetch_movie(tmdb_id, *, language)`, `fetch_tv(tmdb_id, *, language)`,
  `fetch_season(tmdb_id, season, *, language)`, `find_external(source, external_id)`,
  `search_scored(kind, title, year, *, language)` (réutilise `_score_results`/`_verdict`, sans résumé ; la liste de
  candidats est re-triée score ↓ puis `tmdbId` ↑ **après** le verdict). Toutes testent `is_configured` et rendent le
  JSON brut (ou un résultat scoré). Elles passent par un nouveau `_request_bounded(path, params, *, deadline)` qui
  reprend la boucle de `_request` (mêmes reprises, même compteur, même métrique) mais **lève tout de suite**
  `rate_limited` quand un `Retry-After` dépasse le temps restant avant `deadline`, et accepte un `Retry-After` non
  entier (date HTTP ⇒ délai par défaut) ; `_request` lui-même ne change pas. `fetch_*` demandent
  `append_to_response` (film : `credits,external_ids,release_dates,videos,images` ; série :
  `credits,external_ids,content_ratings,videos,images` ; saison : `external_ids,images`),
  `include_image_language={court},en,null` et la règle actuelle `include_video_language`. `find_external` ne lit
  que `movie_results`, `tv_results`, `tv_episode_results` (saisons et personnes ignorées).
- Fonctions de certification : `_parse_movie_certification`/`_parse_tv_certification` gagnent un paramètre
  optionnel `region: str | None = None` (défaut = `_preferred_region()`, comportement du worker inchangé, test de
  non-régression) ; la passerelle passe la région dérivée de `language`.
- `omdb_service.fetch_ratings(imdb_id)` : notes IMDb, Rotten Tomatoes (`Ratings[]`), Metacritic (`Ratings[]` puis
  `Metascore`) ; lève des erreurs typées au lieu de rendre `None` : `Response: "False"` avec `Error` « Incorrect IMDb
  ID. », « Movie not found! » ou « Error getting data. » ⇒ `not_found` (mis en cache négatif) ; « Request limit
  reached! » ou `_budget_exhausted()` ⇒ `budget_exhausted` ; « Invalid API key! », « No API key provided. » ou clé
  absente ⇒ `not_configured` ; toute autre réponse ou exception ⇒ `error` (ni l'un ni l'autre mis en cache).
  Incrémente le même compteur et la même métrique que `get_by_imdb_id`.
- Une seule traduction d'erreurs, dans `scrape_gateway_service`, de l'exception httpx vers `ProviderError(kind,
  retry_after)` : `not_configured` (clé absente, ou 401/403 de TMDB), `not_found` (404 ⇒ résultat négatif, pas une
  erreur), `rate_limited` (429), `unavailable` (`ConnectError`, `RemoteProtocolError`, 502/503/504),
  `bad_response` (autre 4xx/5xx, JSON invalide, **et toute autre exception**, ex. `ValueError` ⇒ 502
  `upstream_error`, message générique), `timeout` (`TimeoutException` ou échéance D2.7). `retry_after` vient de
  `exc.response.headers["Retry-After"]` quand c'est un entier, sinon 30 s. Le message rendu et journalisé ne
  contient que le type et le code HTTP — **jamais `str(exc)`** (piège 21c).

### D2.7 Single-flight et échéance

- Vit dans `scrape_gateway_service` (un seul module). Dict process-local `_in_flight: dict[tuple[str, bool],
  asyncio.Task]` indexé par (forme texte de la clé, `refresh`). La consultation et l'insertion se font **sans
  `await` entre les deux** (sinon deux vols). Le premier demandeur crée la tâche via `create_background_task`
  (référence forte) ; les suivants attendent `asyncio.shield(task)` et incrémentent `coalesced`. La tâche se retire
  du dict dans un `add_done_callback` qui compare **l'identité** de la tâche (ne jamais retirer un vol plus récent de
  la même clé). La déconnexion d'un client n'annule pas la tâche ; son résultat est mis en cache.
- **Aucune exception httpx brute ne sort de la tâche** : `create_background_task` journalise l'exception d'une tâche
  avec `exc_info` (`utils/tasks.py:22-30`), et une `HTTPStatusError` embarque l'URL avec `api_key=`/`apikey=`. La
  coroutine du vol attrape donc **toute** exception et la convertit en `ProviderError` (type + code HTTP seulement,
  D2.6) avant de la relever. Test : clé TMDB factice reconnaissable, 5xx dans un vol dont tous les attendeurs sont
  partis, capture des journaux ⇒ la clé n'apparaît nulle part.
- Chaque requête attend sous `asyncio.timeout(SCRAPE_REQUEST_TIMEOUT_SECONDS)` (défaut 25, borné 5..28) ; au-delà ⇒
  504 `upstream_timeout`, la tâche continue et peut encore remplir le cache. Cela borne aussi l'attente non
  plafonnée sur `Retry-After` (D1.3) sans modifier `_request` ; `_request_bounded` (D2.6) rend d'ailleurs 429 avant
  que l'échéance ne tombe quand c'est un `Retry-After` qui la ferait dépasser.
- `/search` n'est pas coalescé (non persisté, coût faible) ; `/match` coalesce sur sa clé `match`.

### D2.8 Limite par clé, pas de budget dédié

`SlidingWindowLimiter` singleton `scrape_gateway_rate_limiter` (fenêtre 60 s, `SCRAPE_RATE_LIMIT_PER_MINUTE`
défaut 300, `<= 0` désactive), clé du limiteur = `key_id` du principal (`"master"` pour la clé maître), lu sur
`request.state.principal` (ADR 0008 § D2.3) ; dépassement ⇒ 429 `rate_limited` + `Retry-After`. C'est une
dépendance de route (`enforce_scrape_rate_limit`) déclarée **après** le garde sur chacune des 9 routes ; elle compte
toute requête authentifiée, `hit` et erreurs 400/422 comprises. Ajouté à la
fixture `_reset_rate_limiters` de `tests/conftest.py`. **Aucun budget quotidien dédié** (décision de Chakir du
2026-10-18) : un budget OMDb épuisé (garde global) ⇒ `omdbStatus: "budget_exhausted"`, jamais une erreur HTTP.

### D2.9 Mappers purs — `app/services/scrape_mappers.py`

Fonctions pures JSON TMDB/OMDb → dicts des schémas du contrat § 6 : chemins d'images bruts + `IMAGE_BASE_URL`
constante, listes bornées et ordonnées (contrat § 4.5, § 6 ; images triées explicitement par rang de langue —
langue demandée, `en`, sans langue — puis ordre TMDB, tri stable), doublons de personnes (même `tmdbPersonId`, même `job`)
retirés, `0` ⇒ `null` pour les durées, chaînes vides ⇒ `null`, ids externes validés avec les expressions de
`nfo_import_service` (importées, pas recopiées), `contentRating` par les fonctions existantes
`_parse_movie_certification`/`_parse_tv_certification` avec la région dérivée de `language`, bande-annonce par la
règle existante. Modèles Pydantic v2 camelCase dans `app/models/scrape_schemas.py` (`extra="forbid"` en sortie,
`response_model_by_alias=True`), qui apparaissent dans `app.openapi()`.

### D2.10 Composition OMDb

Pour `movie`/`show` : si `externalIds.imdb` est absent ⇒ `omdbStatus: "no_imdb_id"` ; sinon lecture de l'entrée
`omdb|{imdb}` (servie si fraîche), sinon `fetch_ratings` coalescé sur sa propre clé. Statuts : `ok`, `not_found`
(mis en cache négatif), `not_configured`, `budget_exhausted`, `error` (ni l'un ni l'autre mis en cache). `expiresAt`
de la réponse = expiration TMDB, ou `min(expiration TMDB, fetchedAt + TTL négatif)` pour `budget_exhausted`/`error`.
`refresh=true` ne refait pas une entrée `omdb` encore fraîche.

### D2.11 Stats, invalidation, purge

- Stats : une requête `GROUP BY family, key_version` avec sommes conditionnelles sur `result` et `expires_at`,
  plus les compteurs mémoire `sinceStart` (`hits`, `misses`, `coalesced`, `upstreamErrors`).
- Invalidation : par `keys` (parse de toutes les clés **avant** toute suppression ; une seule invalide ⇒ 400, rien
  supprimé ; une transaction) ou par famille + `tmdbId` (`movie|{id}`, `show|{id}` ou `season|{id}|%`, toutes
  langues et versions). Les entrées `match` ne sont pas supprimées, mais comme elles ne portent que le `tmdbId`
  (D2.4), la réponse suivante de `/match` relit des détails frais.
- Corps de réponse (`stats`, `invalidate`, `purge`) et corps d'erreur : modèles Pydantic camelCase dans
  `scrape_schemas.py`. Stats : `entries = positive + ambiguous + negative`, `expired` est un sous-ensemble compté
  en plus.
- Purge : `DELETE … WHERE rowid IN (SELECT rowid FROM scrape_cache WHERE expires_at <= :now OR key_version !=
  :kv OR schema_version != :sv LIMIT :limit)` puis `COUNT` du reste purgeable ; une transaction courte ;
  `limit` 1..100 000. Route seulement (décision 2026-10-18).

### D2.12 Erreurs et observabilité

Corps d'erreur `{"detail": {"code", "message", "retryAfterSeconds"?}}` produit par `HTTPException(detail=dict)` —
pas de gestionnaire d'exceptions global, aucune route existante ne change de format. Métrique
`plexhub_scrape_gateway_requests_total{route, outcome}` à labels fermés (`route` ∈ 9 routes, `outcome` ∈ `hit`,
`miss`, `coalesced`, `error`), zéro-initialisée à l'import. Journaux : famille, résultat et durée ; jamais de titre
complet au niveau INFO, jamais de clé.

## D3 — Alternatives écartées

- **Étendre `tmdb_scrape_cache`/`omdb_scrape_cache`** : garde-fou du lot, et leurs défauts (pas de langue, `ambiguous`
  sans candidats, `not_found` inopérant) seraient hérités.
- **Une table par famille** : six DDL à garder alignés, purge et stats en six requêtes.
- **Stocker la réponse TMDB brute** : chaque `hit` referait le mapping, et un changement de mapper changerait des
  réponses sans changer `schema_version`.
- **Modifier `find_by_imdb_id`/`get_by_imdb_id` pour qu'elles lèvent** : changerait le comportement du worker.
- **Verrou `asyncio.Lock` par clé** : laisse l'appel suivant refaire une lecture et ne compte pas les coalescés ;
  le dict de tâches le fait directement.
- **Budget quotidien dédié** : écarté par Chakir (2026-10-18).
- **Gestionnaire d'exceptions global** : changerait le format d'erreur des routes existantes.

## D4 — Tests exigés

Clés (vecteurs d'or, parse/format aller-retour, formes mal formées) ; migration 029 (fraîche, ancienne, rejouée,
DDL = ORM) ; chaque route : `miss` puis `hit`, négatif mis en cache, `refresh`, langue transmise aux appels TMDB
(respx), enveloppe complète, ordre déterministe ; erreurs 400/401/403/422/429/502/503/504 ; single-flight (20
requêtes concurrentes ⇒ 1 appel ; client annulé ⇒ entrée quand même écrite ; vol orphelin en échec ⇒ aucune clé
journalisée) ; `Retry-After` plus long que le temps restant ⇒ 429 immédiat ; `Retry-After` au format date ; chaque
message d'erreur OMDb ⇒ son `omdbStatus` ; région du `contentRating` suit `language` et le worker garde la
sienne ; taille d'une saison de 500 épisodes aux listes pleines < 4 Mio ; garde attendu **par route** sur les 9
routes (test structurel, `route_audit` ne distingue pas les deux gardes) ; métrique zéro-initialisée ; écriture sous **vrai verrou WAL**
(modèle `tests/test_db_retry_real_lock.py`) ; aucune clé dans les journaux ni les corps d'erreur (capture des logs
avec une clé TMDB factice reconnaissable) ; OpenAPI contient les 9 routes et les schémas.

## D5 — Conséquences

- `CLAUDE.md`/`AGENTS.md` : §2 (nouveaux modules), §3 (migrations 001→030), §5 + `flux-cles.md` §5.15 (flux de la
  passerelle), §9 nouveau piège (« la passerelle n'écrit jamais dans les caches du worker ; une clé à scopes est
  confinée ; jamais `str(exc)` »).
- Deux caches TMDB coexistent (worker et passerelle) : un même titre peut être demandé deux fois à TMDB, une fois
  par cache. Accepté (garde-fou du lot) ; une convergence éventuelle est un lot ultérieur.
- `security-reviewer` obligatoire (nouvelles routes `/api/*`, `deps.py`) ; `perf-benchmarker` déclenché par la
  migration 029.
