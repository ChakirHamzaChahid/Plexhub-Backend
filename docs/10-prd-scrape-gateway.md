# PRD — Lot B1 : passerelle de scraping `/api/scrape/v1`

**Créé** : 2026-10-18 · **Statut** : analysé (phase 1bis faite ; phase 2bis faite, constats traités : § « Analyse 2bis »)
**Demande d'origine** : « le lot B1 "passerelle de scraping". Le backend expose à un client externe (l'outil
plexhubTMM) des routes JSON de scraping TMDB/OMDb, avec un cache versionné. »
**Entrée** : contrat `plexhubTMM/docs/15-contrat-b1-backend.md` (proposé le 2026-10-18, lu en entier, dépôt
plexhubTMM non modifié). Ce PRD dit ce que le backend livre ; les écarts au contrat sont listés au § « Écarts au
contrat » pour report dans plexhubTMM.
**ADR** : [0008 — scopes des clés d'API](architecture/adr/0008-api-key-scopes.md) ·
[0009 — passerelle de scraping : cache versionné et accès fournisseur](architecture/adr/0009-scrape-gateway-cache.md)
**Tickets** : `docs/31-board.md` § « Lot B1 — passerelle de scraping » (B1-01 → B1-12).

## Problème

plexhubTMM (outil Rust de gestion de métadonnées) doit enrichir films, séries, saisons et épisodes depuis TMDB et
OMDb **sans détenir de clé TMDB ni OMDb**. Aujourd'hui le backend n'expose aucune route JSON de scraping : le
rapprochement titre → fiche n'existe que dans le worker d'enrichissement, et les recherches libres que dans l'admin
HTML (Basic Auth, liées à une ligne `media`). Les détails par `tmdb_id` ne sont pas mis en cache, les saisons ne
sont jamais demandées à TMDB, OMDb ne fournit que la note IMDb, la langue est globale et absente des clés de cache,
et une clé d'API utilisateur ouvre tout `/api/*` hors `/api/admin/*` : il est impossible de donner à un outil
externe une clé « scraping seulement ».

## État des lieux du contrat (§ 2) vérifié contre le code

Vérifié le 2026-10-18 sur `develop`, HEAD **`58a954a`**. Le contrat a été relevé sur `472a74b` ; le seul commit
d'écart ne touche ni le scraping ni l'auth.

| § 2 du contrat | Verdict | Réalité (fichier:ligne au HEAD) |
|---|---|---|
| Cache TMDB `tmdb_scrape_cache`, `make_key`, résultats `matched/ambiguous/nomatch` | **Confirmé**, avec une précision | `scrape_cache_service.py:29-31,61-90`, `database.py:365-386`. **Un `ambiguous` ne stocke aucun candidat** (payload, `tmdb_id`, `imdb_id` = NULL ; seul `confidence` est gardé). `get()` ne rend de données que pour `matched`. |
| `normalize_for_sorting` ne laisse jamais de `\|` | **Confirmé**, avec un piège | `string_normalizer.py:38-63`. Un titre fait **uniquement de ponctuation** se normalise en `""` : la clé `movie\|\|1999` est alors commune à tous ces titres. → FR-007 (400). |
| Le titre est nettoyé par `clean_title` avant `make_key` | **Écart** | `make_key` ne nettoie rien et le worker n'appelle jamais `clean_title` (`enrichment_worker.py:189`) : il utilise `Media.title`, nettoyé une fois au sync (`sync_worker.py:104,232`). Une ligne synchronisée avant un correctif du nettoyeur garde un titre pollué ; le scraper manuel écrit sa clé avec le titre brut de la ligne (`manual_scrape_service.py:876-883`). La parité de clé client ⇄ passerelle tient (même `make_key` sur le même titre) ; la parité client ⇄ worker n'est pas garantie pour ces lignes anciennes. |
| Cache OMDb `omdb_scrape_cache`, `found/not_found` | **Confirmé**, avec un défaut | `database.py:389-407`. `get()` renvoie `None` aussi pour un `not_found` frais : le TTL négatif OMDb **n'a aucun effet** côté worker, et toute panne ou tout 429 OMDb est écrit `not_found` (`omdb_scrape_cache_service.py:52-53,116-119`). |
| TTL 30 j / 3 j, constantes en dur dupliquées | **Confirmé** | `scrape_cache_service.py:25-26`, `omdb_scrape_cache_service.py:29-30`. Expiration calculée à la lecture (`now - fetched_at > ttl`) ; aucune purge. |
| Langue globale `TMDB_LANGUAGE`, hors clé | **Confirmé** | `config.py:203`. La recherche accepte une langue ; les détails utilisent `settings.TMDB_LANGUAGE` en dur (`tmdb_service.py:433-459`). Région du `content_rating` : `_preferred_region()` (`tmdb_service.py:534-540`). |
| Aucun versionnement | **Confirmé** | Un champ de payload renommé lève `TypeError`, attrapé comme un miss silencieux (`scrape_cache_service.py:54-57`). |
| Aucun cache de détails par `tmdb_id` | **Confirmé** | Caches mémoire existants : `_search_cache` (24 h) et `_imdb_find_cache` (7 j), `tmdb_service.py:254-261`. |
| Aucun appel `/tv/{id}/season/{n}` | **Confirmé** | — |
| Films sans `runtime` ni équipe | **Confirmé** | `TMDBEnrichmentData` (23 champs, `tmdb_service.py:91-131`) : `credits` est demandé mais seul `cast` est lu ; pas de titre localisé. Les séries ont `episode_runtime_ms`. |
| Images en URL complètes à taille figée | **Confirmé** | `tmdb_service.py:21-29`. |
| OMDb : seules `imdbRating` et `imdbVotes` sont lues | **Écart (contrat faux)** | 11 champs sont lus (`Title, Year, Runtime, Genre, Director, Actors, Plot, imdbRating, imdbVotes, Type, imdbID`, `omdb_service.py:340-354`). Ce qui manque pour B1 : `Ratings[]` (Rotten Tomatoes, Metacritic) et `Metascore`. |
| Aucune route JSON de scraping ; recherches libres dans l'admin HTML | **Confirmé** | La plage admin est `admin.py:495-589` (le lookup va jusqu'à 589). |
| Aucun single-flight | **Confirmé** | — |
| Clés `phk_…`, SHA-256, révocables, expirables, sans scope | **Confirmé** | `api_key_service.py:26-126`, `database.py:410-437`. `_authenticate` réduit la clé à un booléen (`deps.py:54-61`) : aucune identité n'atteint les routes. |
| Migrations rejouées, dernière 028 | **Confirmé** | `migrations.py:20-65,1316`. |
| `route_audit` refuse une route non gardée | **Confirmé**, avec une précision | La reconnaissance se fait **par le nom de la fonction** de garde (`route_audit.py:57-64,102-112`). Une fabrique `verify_scope("scrape")` produirait une closure non reconnue. |
| Vecteurs d'or `tests/test_enrichment_scraping.py:28-30` | **Confirmé** | 2 vecteurs seulement (`Terminator (VF)`, `Breaking Bad`). |
| Budgets `ENRICHMENT_DAILY_LIMIT` / `OMDB_DAILY_LIMIT` (§ 8 du contrat) | **Écart** | Ce ne sont pas des quotas 24 h : compteurs **en mémoire**, remis à zéro au début de chaque passe du worker (`enrichment_worker.py:609-612`, `enrichment_backfill_worker.py:378`). `tmdb_service` n'applique aucune limite ; seul OMDb a un garde dans le service (`omdb_service.py:220-235`). |
| Reprises TMDB « 1 + 3 tentatives, 429 avec `Retry-After` » (§ 8) | **Confirmé**, avec un risque | `tmdb_service.py:317-366`, timeout httpx 10 s par phase. L'attente sur `Retry-After` **n'est pas plafonnée** ; un `Retry-After` au format date lèverait `ValueError`. → FR-034 (échéance de 25 s). |
| (non cité) Compteurs TMDB/OMDb partagés | **Constat** | Tout appel de la passerelle incrémente `real_request_count` des singletons `tmdb_service`/`omdb_service` (`tmdb_service.py:289-304`, `omdb_service.py:235`), que le worker remet à zéro à chaque passe : la passerelle consomme le budget par passe du worker, et le garde OMDb qu'elle voit dépend de la dernière remise à zéro. Accepté (pas de budget dédié, décision 2026-10-18) ; voir X3. |
| (non cité) Région du `contentRating` | **Constat** | `_parse_movie_certification`/`_parse_tv_certification` (`tmdb_service.py:996,1041`) lisent la région de `settings.TMDB_LANGUAGE` et n'ont aucun paramètre : une requête `en-US` recevrait la certification FR. B1-08 leur ajoute un paramètre `region` optionnel (défaut = comportement actuel). |
| (non cité) Erreurs avalées | **Constat** | `find_by_imdb_id` et `omdb_service.get_by_imdb_id` renvoient `None` sur toute erreur ; `get_movie_details`/`get_tv_details` ne testent pas `is_configured`. La passerelle ne peut pas distinguer « non trouvé » de « panne » avec ces fonctions → ADR 0009 § D2.6. |

## User stories (par priorité)

### US1 — Clé « scraping seulement » (P1)

Chakir crée pour plexhubTMM une clé dédiée, expirable, qui n'ouvre que la passerelle de scraping. Une fuite de
cette clé ne donne accès ni aux médias, ni aux comptes Xtream, ni au flux, ni à l'appairage TV.

**Pourquoi P1** : sans scope, la seule clé possible ouvre tout `/api/*` ; c'est le prérequis de sécurité de tout le
lot.
**Test indépendant** : créer une clé `scrape` ; elle reçoit 403 sur `GET /api/media/movies`, et 200 sur une route de
test montée en fixture derrière `verify_scrape_scope` (la passerelle elle-même arrive avec B1-11).

**Scénarios d'acceptation**
- **US1-AC1** — **Given** la clé maître, **When** `POST /api/admin/keys` avec `{"label": "plexhubTMM", "expiresInDays": 365, "scopes": ["scrape"]}`, **Then** 201 et la réponse porte `scopes: ["scrape"]` et la clé en clair une seule fois.
- **US1-AC2** — **Given** une clé portant `scrape`, **When** elle appelle une route existante de `/api/*` (média, comptes, flux, sync, IA, `POST /api/tv-auth/approve`), **Then** 403 avec `detail.code = "insufficient_scope"`.
- **US1-AC3** — **Given** une clé **sans** scope (toutes les clés actuelles), **When** elle appelle `/api/scrape/v1/*`, **Then** 403 `insufficient_scope` ; **And** sur les routes existantes son comportement est inchangé.
- **US1-AC4** — **Given** une clé `scrape` seule, **When** `POST /api/scrape/v1/cache/purge`, **Then** 403 `insufficient_scope` ; avec une clé `scrape.admin` ou la clé maître, **Then** 200.
- **US1-AC5** — **Given** l'admin HTML `/admin/keys`, **When** Chakir coche `scrape` à la création, **Then** la clé est créée avec ce scope et la colonne « Scopes » du tableau l'affiche.
- **US1-AC6** — **Given** une clé `scrape` révoquée ou expirée, **When** elle appelle la passerelle, **Then** 401 `invalid_api_key`.
- **US1-AC7** — **Given** une clé à scopes, **When** elle appelle `/api/admin/*`, **Then** 401 `Master key required`, comme aujourd'hui.

### US2 — Rapprocher un titre d'une fiche TMDB (P1)

plexhubTMM envoie (type, titre nettoyé, année, langue) et reçoit `matched` avec les détails, `ambiguous` avec les
candidats, ou `nomatch` ; la réponse dit d'où elle vient (`hit`/`miss`) et quand elle expire.

**Pourquoi P1** : c'est le premier appel de tout enrichissement côté client (écart E1).
**Test indépendant** : `GET /api/scrape/v1/match?type=movie&title=matrix&year=1999` → `matched`, `cacheKey`
`1|fr-FR|movie|matrix|1999`, `cache: "miss"` ; même appel → `cache: "hit"`, TMDB non rappelé.

**Scénarios d'acceptation**
- **US2-AC1** — **Given** un titre absent du cache, **When** `GET /match`, **Then** l'algorithme de score et les seuils du worker décident `matched`/`ambiguous`/`nomatch`, le résultat est stocké sous la clé `match`, et la réponse porte l'enveloppe complète (`schemaVersion`, `cacheKey`, `language`, `fetchedAt`, `expiresAt`, `cache`, `result`).
- **US2-AC2** — **Given** un `matched`, **Then** `details` porte les mêmes données que `GET /movie/{id}` (ou `/tv/{id}`) **And** au premier calcul ces détails sont stockés sous leur clé `movie`/`show` avec les mêmes `fetchedAt`/`expiresAt` que l'entrée `match`.
- **US2-AC3** — **Given** un `ambiguous`, **Then** `candidates` (≤ 10, score décroissant, puis `tmdbId` croissant) est stocké avec l'entrée et rendu à l'identique au `hit` suivant.
- **US2-AC6** — **Given** une entrée `match` `matched` fraîche dont l'entrée `movie|{id}` a été invalidée ou a expiré, **When** `GET /match`, **Then** les détails sont redemandés à TMDB dans le même vol, restockés, et la réponse porte `cache: "miss"` (FR-018).
- **US2-AC4** — **Given** `language=en-US`, **Then** la recherche **et** les détails sont demandés en `en-US`, et l'entrée est distincte de celle en `fr-FR`.
- **US2-AC5** — **Given** un titre dont la forme normalisée est vide (ponctuation seule), **When** `GET /match`, **Then** 400 `invalid_parameter`.

### US3 — Détails de film, de série, de saison avec ses épisodes (P1)

À partir d'un `tmdb_id`, plexhubTMM récupère la fiche complète : durée, équipe, images en chemins bruts, notes
TMDB + IMDb + Rotten Tomatoes + Metacritic ; pour une saison, tous ses épisodes en un appel.

**Pourquoi P1** : écarts E3, E4, E6, E13 ; sans eux le client ne peut pas écrire de NFO complet.
**Test indépendant** : `GET /api/scrape/v1/tv/1396/season/1` → `found`, 7 épisodes en ordre croissant, un seul appel
TMDB.

**Scénarios d'acceptation**
- **US3-AC1** — **Given** un `tmdbId` de film connu, **When** `GET /movie/{tmdbId}`, **Then** `details` suit `MovieDetails` (contrat § 6.1) : `runtimeMinutes`, `crew` filtré, images en chemins bruts + `imageBaseUrl`, `ratings` dans l'ordre `tmdb, imdb, metacritic, rottenTomatoes`, `omdbStatus`.
- **US3-AC2** — **Given** un `tmdbId` inconnu de TMDB (404 fournisseur), **Then** 200 `result: "not_found"`, `details: null`, mis en cache avec le TTL négatif.
- **US3-AC3** — **Given** une série, **When** `GET /tv/{tmdbId}`, **Then** `ShowDetails` (§ 6.2) dont `seasons` (spéciaux compris, ordre croissant).
- **US3-AC4** — **Given** une saison, **When** `GET /tv/{tmdbId}/season/{n}`, **Then** **un seul** appel TMDB et `SeasonDetails` (§ 6.3) avec ≤ 500 épisodes en `episodeNumber` croissant ; saison inexistante ⇒ `not_found`.
- **US3-AC5** — **Given** OMDb non configuré, budget OMDb épuisé ou OMDb en panne, **Then** la réponse reste 200, les notes OMDb sont omises, `omdbStatus` vaut `not_configured`/`budget_exhausted`/`error`, et pour `budget_exhausted`/`error` `expiresAt = min(expiration TMDB, fetchedAt + TTL négatif)`.
- **US3-AC6** — **Given** `refresh=true`, **Then** l'entrée TMDB de la clé est refaite ; les notes OMDb ne sont redemandées que si leur propre entrée a expiré ; si le fournisseur échoue, l'erreur est rendue et l'ancienne entrée reste intacte.

### US4 — Retrouver une fiche depuis un id IMDb ou TVDB (P2)

**Pourquoi P2** : utile pour les NFO qui portent déjà un id externe (écart E5), mais le client peut rapprocher par
titre sans cela.
**Test indépendant** : `GET /api/scrape/v1/find?imdbId=tt0133093` → `matches: [{"mediaType": "movie", "tmdbId": 603}]`.

**Scénarios d'acceptation**
- **US4-AC1** — **Given** exactement un de `imdbId`/`tvdbId`, **Then** `matches` (≤ 5 ; film, série ou épisode avec `showTmdbId`/`seasonNumber`/`episodeNumber`), `language: "-"`, clé `1|-|find|imdb|tt0133093`, stockée.
- **US4-AC2** — **Given** aucun ou deux ids, **Then** 400 `invalid_parameter`.
- **US4-AC3** — **Given** un id sans correspondance, **Then** 200 `not_found` mis en cache (TTL négatif).

### US5 — Chercher des candidats (P2)

**Pourquoi P2** : sert au choix manuel côté client (écart E2) ; non persisté.
**Scénarios d'acceptation**
- **US5-AC1** — **Given** `GET /search?type=show&query=breaking+bad`, **Then** ≤ 10 `candidates` triés, `result: found|not_found`, `cacheKey: null`, `expiresAt = fetchedAt`, `cache: "miss"`, rien en base.
- **US5-AC2** — **Given** une requête sans résultat TMDB, **Then** 200 `result: "not_found"`, `candidates: []`.
- **US5-AC3** — **Given** `year` et `language=en-US`, **Then** l'appel TMDB porte l'année et `language=en-US`.

### US6 — Observer et maîtriser le cache (P2)

**Pourquoi P2** : sans stats ni purge, la table ne fait que grossir (écart E11) ; l'invalidation sert à corriger une
fiche TMDB modifiée.
**Scénarios d'acceptation**
- **US6-AC1** — **Given** une clé `scrape`, **When** `GET /cache/stats`, **Then** une ligne par (famille, `keyVersion`) triée, compteurs `positive/ambiguous/negative/expired` (`entries = positive + ambiguous + negative` ; `expired` est un sous-ensemble de `entries`, compté en plus), bornes `oldestFetchedAt/newestFetchedAt`, `ttl`, et `sinceStart` (`hits`, `misses`, `coalesced`, `upstreamErrors`, en mémoire).
- **US6-AC2** — **Given** `POST /cache/invalidate` avec `keys` (1 à 500) dont une mal formée, **Then** 400 et **rien** n'est supprimé ; toutes valides ⇒ `{"deleted": n}`, idempotent.
- **US6-AC3** — **Given** `{"family": "season", "tmdbId": 1396}`, **Then** toutes les saisons de la série, toutes langues, sont supprimées.
- **US6-AC4** — **Given** `POST /cache/purge` (`limit` défaut 10 000), **Then** sont supprimées au plus `limit` entrées expirées ou d'une `key_version`/`schema_version` non courante ; réponse `{"deleted", "remaining"}`. Aucune purge automatique.

### US7 — Protéger les fournisseurs (P1, transverse)

**Scénarios d'acceptation**
- **US7-AC1** — **Given** N requêtes concurrentes de même clé canonique et même `refresh`, **Then** un seul appel fournisseur ; `coalesced` augmente de N − 1.
- **US7-AC2** — **Given** plus de `SCRAPE_RATE_LIMIT_PER_MINUTE` requêtes en une minute pour une même clé, **Then** 429 `rate_limited` avec `Retry-After`.
- **US7-AC3** — **Given** TMDB répond 429 après ses reprises, ou avec un `Retry-After` qui dépasse le temps restant avant l'échéance, **Then** 429 `rate_limited` avec `Retry-After` (jamais 504 dans ce cas) ; TMDB injoignable ⇒ 503 `upstream_unavailable` ; réponse invalide ou exception imprévue ⇒ 502 `upstream_error` ; échéance dépassée ⇒ 504 `upstream_timeout`.
- **US7-AC4** — **Given** `TMDB_API_KEY` absente, **Then** 503 `provider_not_configured` sur toute route TMDB.

## Cas limites

- Titre normalisé vide → 400 (US2-AC5). Année absente → `year` vide dans la clé (`movie|matrix|`), comme le worker.
- Le client qui coupe sa requête pendant un appel fournisseur ne doit pas annuler l'appel partagé : les autres requêtes coalescées l'attendent toujours, et le résultat est quand même mis en cache.
- `refresh=true` concurrent d'un appel sans `refresh` sur la même clé : deux vols distincts (le `refresh` fait partie de la clé de vol), le dernier écrit gagne.
- `ambiguous` rendu par `/match` : TTL négatif, mais **non** compté comme négatif dans les stats (colonne `ambiguous`).
- Payload d'une entrée illisible (JSON corrompu, `schema_version` ancienne) → traité comme absent, refetch, journal WARNING sans le contenu.
- Écriture en base qui échoue après un appel fournisseur réussi → la réponse est quand même rendue (`cache: "miss"`), l'échec est journalisé, rien n'est rendu de faux.
- `Retry-After` TMDB qui pousserait au-delà de l'échéance → 504 `upstream_timeout` à l'échéance (25 s).
- Saison 0 (spéciaux) : valide. `seasonNumber` 1000 ou `tmdbId` 0 → 422 FastAPI (bornes `Path`).
- Invalidation par famille de toutes les langues d'un film absent du cache → `{"deleted": 0}`.

## Exigences fonctionnelles

### Clés et scopes
- **FR-001** : La table `api_keys` DOIT porter les scopes d'une clé (colonne `scopes`, migration 030) ; une clé existante n'a aucun scope.
- **FR-002** : `POST /api/admin/keys` DOIT accepter `scopes` (liste, valeurs `scrape`, `scrape.admin`, sans doublon ; toute autre valeur ⇒ 422) et `GET` DOIT les rendre (`scopes: []` pour une clé sans scope). La validation contre l'ensemble fermé (tri, dédoublonnage, rejet de toute valeur inconnue ou contenant une espace) DOIT vivre dans `api_key_service.create_key`, pour couvrir aussi le chemin HTML.
- **FR-003** : Une clé utilisateur **dont la colonne `scopes` n'est pas vide** (y compris si elle ne contient que des valeurs inconnues, qui ne donnent aucun droit) DOIT être refusée (403 `insufficient_scope`) par les gardes `verify_backend_secret` et `verify_api_key`. Une clé sans scope garde exactement son comportement actuel sur ces routes (y compris le format de ses erreurs).
- **FR-004** : Les routes de lecture de la passerelle DOIVENT exiger la clé maître ou une clé portant `scrape` ; invalidation et purge la clé maître ou une clé portant `scrape.admin`. Les scopes sont indépendants. Clé absente, inconnue, révoquée ou expirée ⇒ 401 `invalid_api_key`.
- **FR-005** : Les deux nouveaux gardes DOIVENT être reconnus par `route_audit` (nommés `verify_scrape_scope` et `verify_scrape_admin_scope`) ; l'allow-list publique reste à 5 entrées.
- **FR-006** : L'admin HTML `/admin/keys` DOIT proposer les scopes à la création (cases à cocher) et les afficher dans le tableau.

### Clés de cache et langue
- **FR-007** : La clé canonique DOIT être le triplet `(key_version, language, key)` du contrat § 4.2, forme texte `"{key_version}|{language}|{key}"`, `key_version = 1`. La clé `match` DOIT être exactement `make_key(type, title, year)` actuel, sur le titre reçu, sans renettoyage. Un titre dont `normalize_for_sorting` donne `""` ⇒ 400 `invalid_parameter`.
- **FR-008** : Le paramètre `language` (`^[a-z]{2}-[A-Z]{2}$`, défaut `TMDB_LANGUAGE`, validé à la main : hors motif ⇒ **400** `invalid_parameter`, comme le contrat § 4.3, et non 422) DOIT être transmis à tous les appels TMDB de la requête et faire partie de la clé ; les familles `omdb` et `find` utilisent `-`. Images demandées avec `include_image_language={langue courte},en,null`. Pas de repli de langue.
- **FR-009** : Une forme texte de clé DOIT être analysable sans ambiguïté selon la règle du contrat § 4.2 (familles par premier segment ; `movie`/`show` : 5 segments = `match`, 4 = détails) ; toute autre forme est mal formée.

### Cache
- **FR-010** : Une table unique `scrape_cache` (migration 029, DDL identique à l'ORM) DOIT stocker toutes les familles (`match`, `movie`, `show`, `season`, `omdb`, `find`) avec les colonnes du contrat § 7, PK `(family, key_version, language, cache_key)`, index sur `expires_at`.
- **FR-011** : `expires_at` DOIT être calculé à l'écriture (`fetched_at + TTL`) : TTL succès pour `matched`/`found`, TTL négatif pour `ambiguous`/`nomatch`/`not_found`. Une entrée expirée, d'une autre `key_version` ou d'une autre `schema_version` n'est jamais servie.
- **FR-012** : Les TTL DOIVENT venir de `SCRAPE_TTL_SUCCESS_DAYS` (défaut 30, borné 1..365) et `SCRAPE_TTL_NEGATIVE_DAYS` (défaut 3, borné 1..30), lus au démarrage. Ils ne s'appliquent **qu'à** `scrape_cache` : les constantes de `scrape_cache_service` et `omdb_scrape_cache_service` restent inchangées.
- **FR-013** : Les notes OMDb DOIVENT être stockées dans la famille `omdb` de `scrape_cache` (payload : notes IMDb, Rotten Tomatoes, Metacritic) ; `omdb_scrape_cache` n'est ni lu ni écrit par la passerelle.
- **FR-014** : Tout écrit en base de la passerelle DOIT passer par `write_with_retry` (session neuve par tentative), dans une transaction courte, **après** l'appel fournisseur (jamais de transaction ouverte pendant un appel réseau).

### Routes
- **FR-015** : Le router `/api/scrape/v1` (auto-préfixé, monté nu, **sans dépendance au niveau du router** : le garde est déclaré **par route**, `verify_scrape_scope` sur les 7 routes de lecture, `verify_scrape_admin_scope` seul sur `invalidate` et `purge`) DOIT exposer les 9 routes du contrat § 5.1, avec les paramètres et bornes du contrat (§ 5.2 à § 5.10).
- **FR-015b** : Validation des entrées — **422** (natif FastAPI) pour le type et les bornes déclarées en `Query`/`Path`/corps : `type` hors `movie|show`, `title`/`query` hors 1..300, `year` hors 1850..2200, `tmdbId` hors 1..999 999 999, `seasonNumber` hors 0..999, `keys` hors 1..500 éléments, `limit` hors 1..100 000. **400** `invalid_parameter` pour les contrôles sémantiques : `language` hors motif, titre normalisé vide, `/find` avec zéro ou deux ids, `imdbId` ou `tvdbId` hors motif, corps d'invalidation avec `keys` **et** `family`, ou aucun des deux, ou une famille autre que `movie|show|season`, toute clé canonique mal formée.
- **FR-016** : Toute réponse 200 de `/match`, `/search`, `/movie`, `/tv`, `/tv/.../season/...`, `/find` DOIT porter l'enveloppe du contrat § 4.4 en camelCase ; `cache` vaut `hit` ou `miss`.
- **FR-017** : `/match` DOIT réutiliser le score et les seuils du worker (`tmdb_service._score_results`/`_verdict` : 0.85 / 0.90 / 0.05), recherche avec année puis sans, dans la langue demandée, **sans** départage par résumé, garde OMDb, repli `en-US` ni `/search/multi` (X13). Le verdict se prend dans l'ordre actuel de `_score_results` ; la liste `candidates` rendue est **ensuite** re-triée (score décroissant, puis `tmdbId` croissant).
- **FR-018** : L'entrée `match` d'un `matched` DOIT stocker seulement `{confidence, tmdbId}` ; les détails vivent dans l'entrée `movie|{id}`/`show|{id}`. Au premier calcul, les deux entrées sont écrites dans la même transaction avec les mêmes `fetchedAt`/`expiresAt` (tout ou rien : si l'appel de détails échoue, rien n'est stocké et l'erreur est rendue). Au `hit` de `match`, les détails sont lus dans leur entrée si elle est fraîche ; sinon ils sont redemandés dans le même vol et restockés, et la réponse porte `cache: "miss"`. L'enveloppe de `/match` porte alors `fetchedAt` = celui de l'entrée `match` et `expiresAt = min(expiration match, expiration détails)` (X17). Une invalidation de `movie|{id}` corrige donc aussi `/match`.
- **FR-019** : Pour `ambiguous`, les candidats (≤ 10) DOIVENT être stockés dans le payload et rendus au `hit`.
- **FR-020** : `/search` NE DOIT RIEN persister (`cacheKey: null`, `expiresAt = fetchedAt`).
- **FR-021** : `/movie/{tmdbId}` et `/tv/{tmdbId}` DOIVENT faire un appel TMDB unique (`append_to_response=credits,external_ids,release_dates|content_ratings,videos,images`), et `/tv/{id}/season/{n}` un appel unique (`append_to_response=external_ids,images` ; l'équipe et les invités des épisodes sont déjà dans la réponse de saison). Un 404 fournisseur ⇒ `not_found` mis en cache. Le `contentRating` utilise la région dérivée de `language` (paramètre `region` ajouté aux fonctions de certification existantes, défaut inchangé pour le worker).
- **FR-022** : `/find` DOIT accepter exactement un de `imdbId` (`^tt\d{7,10}$`) / `tvdbId` (1 à 12 chiffres, normalisé en entier sans zéros de tête avant de former la clé), sinon 400, et rendre ≤ 5 `matches` (films, séries, épisodes ; les résultats de saison et de personne sont ignorés) sans titre ni année.
- **FR-023** : Les réponses composites (détails + OMDb) DOIVENT suivre la règle `fetchedAt`/`expiresAt` du contrat § 4.4. Le payload TMDB est stocké **sans** les notes OMDb ; la composition se fait à la lecture.
- **FR-024** : `refresh=true` DOIT ignorer l'entrée existante de la clé demandée (et, pour `/match`, les détails embarqués) ; l'entrée `omdb` n'est refaite que si elle a expiré ; un échec fournisseur pendant un `refresh` laisse l'ancienne entrée intacte.

### Schémas de sortie
- **FR-025** : Les objets `MovieDetails`, `ShowDetails`, `SeasonDetails`, `EpisodeDetails`, `Rating`, `CastMember`, `CrewMember`, `Candidate`, `Images` DOIVENT suivre le contrat § 6 (champs, sources, bornes de listes, ordres, `null` pour absent, jamais de chaîne vide), en modèles Pydantic v2 camelCase présents dans l'OpenAPI.
- **FR-026** : Les ids externes DOIVENT être validés avec les règles de l'import NFO (`imdb` `^tt\d{7,10}$`, `tvdb` 1 à 12 chiffres, `wikidata` `^Q\d+$`) ; invalide ⇒ `null`.
- **FR-027** : Les notes DOIVENT être normalisées selon le contrat § 6.4 (TMDB omise si `vote_count = 0` ; Rotten Tomatoes `"87%"` → 87/100 ; Metacritic `"74/100"` ou `Metascore` → 74/100) ; ordre `tmdb, imdb, metacritic, rottenTomatoes`.
- **FR-028** : Toute réponse DOIT rester sous 4 Mio (test sur une saison de 500 épisodes aux listes pleines) et toute liste avoir un ordre déterministe ; les listes d'images sont triées explicitement par rang de langue (langue demandée, `en`, sans langue) puis dans l'ordre TMDB (tri stable).
- **FR-028b** : Les corps de `stats`, `invalidate`, `purge` et le corps d'erreur DOIVENT être des modèles Pydantic camelCase présents dans l'OpenAPI (pas de dict nu, §3).

### Gestion du cache
- **FR-029** : `GET /cache/stats` DOIT rendre le corps du contrat § 5.8 (sans enveloppe).
- **FR-030** : `POST /cache/invalidate` DOIT accepter `keys` (1 à 500) **ou** `{family: movie|show|season, tmdbId}` ; tout ou rien ; idempotent.
- **FR-031** : `POST /cache/purge` DOIT supprimer par lots bornés (`limit`, défaut 10 000, borné 1..100 000) les entrées expirées ou d'une version non courante ; aucune purge automatique.

### Concurrence, erreurs, observabilité
- **FR-032** : Des requêtes concurrentes de même clé canonique et même `refresh` DOIVENT partager un seul appel fournisseur (single-flight process-local) ; la déconnexion d'un client n'annule pas l'appel partagé.
- **FR-033** : Une limite par clé d'API (`SCRAPE_RATE_LIMIT_PER_MINUTE`, défaut 300, `<= 0` désactive) DOIT rendre 429 `rate_limited` avec `Retry-After`. Elle s'applique aux 9 routes, par une dépendance de route placée **après** le garde, et compte toute requête authentifiée (`hit`, `miss`, erreurs 400/422 comprises).
- **FR-034** : Une requête de la passerelle DOIT se terminer en moins de `SCRAPE_REQUEST_TIMEOUT_SECONDS` (défaut 25, borné 5..28) ; au-delà ⇒ 504 `upstream_timeout`. Un `Retry-After` TMDB plus long que le temps restant DOIT produire tout de suite 429 `rate_limited` (pas d'attente, pas de 504).
- **FR-035** : Les erreurs de la passerelle DOIVENT avoir le corps `{"detail": {"code", "message", "retryAfterSeconds"?}}` et les codes du contrat § 4.6 ; 422 et 404 restent ceux de FastAPI. Toute exception imprévue d'un appel fournisseur ⇒ 502 `upstream_error`, message générique. Aucune erreur ni aucun journal ne contient de clé (TMDB, OMDb, X-API-Key), d'URL fournisseur, de chemin serveur ni de trace — y compris les journaux de la tâche partagée du single-flight, qui ne laisse jamais sortir d'exception httpx brute.
- **FR-036** : Des métriques Prometheus à labels fermés DOIVENT compter les requêtes de la passerelle par route et par issue (`hit`, `miss`, `coalesced`, `error`).

### Entités clés
- **Entrée de cache** (`scrape_cache`) : une réponse fournisseur normalisée, identifiée par famille, version de clé, langue et clé ; porte son résultat, la version du format de son payload, ses instants de fetch et d'expiration.
- **Scope de clé** : droit nommé (`scrape`, `scrape.admin`) porté par une clé utilisateur ; une clé à scopes est confinée à ses routes.

## Contraintes du dépôt

- **Contrat d'API** : 9 routes **additives** ; aucune route existante ne change de chemin, de paramètre ni de corps de réponse. Seul changement observable sur l'existant : une clé **à scopes** reçoit 403 sur les routes existantes (une telle clé n'existe pas avant B1). Application TV non concernée.
- **Migrations** : 029 (`scrape_cache`, nouvelle table) et 030 (`api_keys.scopes`, `ADD COLUMN` avec `DEFAULT ''` au niveau SQL, gardée par `_column_exists`) — additives, idempotentes, en fin de `run_migrations()`. Risky (schéma) ⇒ `needs-approval` au moment du développement.
- **Concurrence d'écriture** : `write_with_retry` ; transactions courtes ; testé sous vrai verrou WAL (`tests/test_db_retry_real_lock.py` comme modèle).
- **API externes** : TMDB (aucune limite côté backend, reprises 1 + 3), OMDb (garde global du service) ; les appels de la passerelle comptent dans les compteurs par passe du worker (TMDB **et** OMDb) et le garde OMDb vu par la passerelle n'est pas un quota fiable ; pas de budget dédié (décision 2026-10-18).
- **Croissance de la table** : sans purge automatique, une clé `scrape` peut faire grossir `scrape_cache` (langues × titres × années, jusqu'à 300 requêtes/min) ; le runbook de B1-12 prescrit une purge périodique par l'opérateur ou par plexhubTMM.
- **Panne de la base pendant l'authentification** : `api_key_service.resolve` avale toute exception ⇒ 401 `invalid_api_key`, que le client ne réessaie pas. Comportement existant, documenté, non modifié en B1.
- **Secrets / X-API-Key / SSRF** : auth touchée ⇒ `security-reviewer` obligatoire ; clients httpx TMDB/OMDb existants (aucun nouveau client sortant, aucune URL venue du client) ; jamais `str(exc)` d'une exception httpx dans un log ou une réponse (piège 21c/25c).
- **Process unique** : single-flight, limiteur par clé et compteurs `sinceStart` sont process-local, comme le reste (`uvicorn` sans `--workers`, piège 18c).

## Non-goals

- Écrire des métadonnées dans `media` (lot M3 de plexhubTMM, autre API).
- Modifier `tmdb_scrape_cache`, `omdb_scrape_cache`, le worker d'enrichissement, le scraper manuel ou leurs TTL.
- Budgets quotidiens TMDB/OMDb dédiés à la passerelle (décision 2026-10-18).
- Purge automatique (décision 2026-10-18).
- Appels TMDB par épisode (ids IMDb d'épisode), repli de langue, traductions, titres alternatifs, mots-clés, mandataire d'images, recherche OMDb par titre exposée.
- Pagination ; limiteur partagé entre plusieurs process.

## Critères de succès

- **SC-001** : Au deuxième appel identique, 100 % des routes persistées répondent `hit` sans appel fournisseur (vérifié par un client TMDB simulé qui compte ses appels).
- **SC-002** : 20 requêtes concurrentes identiques ⇒ 1 appel fournisseur.
- **SC-003** *(indicatif, non bloquant)* : un `hit` répond en moins de 50 ms en p95 sur la machine de dev, mesuré par `curl -w` au checkpoint du lot (B1-12) et noté dans le rapport ; pas de test automatique (instable en CI).
- **SC-004** : Une clé `scrape` reçoit 403 sur 100 % des routes `/api/*` existantes gardées par `verify_backend_secret`/`verify_api_key` (test qui parcourt `app.routes`).
- **SC-005** : `pytest -v` vert, `ruff check` vert, boot `uvicorn` + `GET /api/health` 200, migrations 029/030 rejouées deux fois sans erreur ni WARNING.

## Hypothèses

- Seuils de rapprochement réutilisés tels quels (contrat § 11 Q5 : oui).
- `key_version = 1` et `schemaVersion = 1` au lancement.
- La région du `contentRating` est dérivée de `language` par la règle actuelle de `_preferred_region` (dernier segment, en majuscules), passée explicitement aux fonctions de certification.
- Une seule instance uvicorn (règle du dépôt) : un single-flight et un limiteur en mémoire suffisent.
- TMDB `/find` avec `external_source=tvdb_id` couvre la recherche par id TVDB.

## Décisions et options écartées

| Décision | Retenue | Écartée(s) et raison |
|---|---|---|
| Stockage du cache (§ 11 Q1) | Table unique `scrape_cache` | Une table par famille : 6 DDL à garder alignés sur l'ORM, purge et stats en 6 requêtes, et le cache local de plexhubTMM a déjà une table unique. |
| Stockage des scopes (§ 11 Q2) | Colonne `api_keys.scopes TEXT NOT NULL DEFAULT ''`, valeurs séparées par des espaces ; API en liste | Table liée : une jointure à chaque requête authentifiée pour deux valeurs possibles. |
| Notes OMDb (§ 11 Q3) | Famille `omdb` de `scrape_cache` | Étendre `omdb_scrape_cache` : table du worker (garde-fou du lot), son `not_found` n'a pas d'effet et une panne y est écrite `not_found` (voir état des lieux). |
| Purge (§ 11 Q4) | Route seulement | Tâche planifiée ou purge au démarrage (décision 2026-10-18). |
| Seuils (§ 11 Q5) | Réutilisés tels quels, via `_score_results`/`_verdict` | Seuils propres à la passerelle : divergence de verdict avec le worker. |
| Clés sans scope sur la passerelle | Refusées (403) | Acceptées : contraire au moindre privilège. |
| Budgets | Aucun budget dédié ; limite par clé + garde OMDb existant | Réutiliser `ENRICHMENT_DAILY_LIMIT` : ce n'est pas un quota 24 h. |
| TTL configurables | Pour `scrape_cache` seulement | Remplacer les constantes des caches existants (contrat § 7) : contraire au garde-fou « worker et `tmdb_scrape_cache` tels quels ». |
| Gardes | Deux fonctions nommées | Fabrique `verify_scope(...)` : closure non reconnue par `route_audit` (reconnaissance par nom). |
| Accès fournisseur | Nouvelles méthodes qui lèvent des erreurs typées, à côté des existantes | Modifier `find_by_imdb_id`/`get_by_imdb_id` : changerait le comportement du worker. |

## Écarts au contrat (à reporter dans plexhubTMM)

| # | Sujet | Contrat (provisoire) | Retenu par B1 | Effet côté client |
|---|---|---|---|---|
| X1 | État des lieux § 2, ligne OMDb | « seules `imdbRating` et `imdbVotes` sont lues » | 11 champs sont lus ; manquent `Ratings[]` et `Metascore` | Aucun (correction documentaire). |
| X2 | État des lieux § 2, `clean_title` | le titre du worker passe par `clean_title` avant `make_key` | le worker ne le fait pas lui-même ; il utilise `Media.title`, nettoyé au sync | La parité de clé est garantie client ⇄ passerelle ; **pas** client ⇄ worker pour des lignes anciennes. |
| X3 | Budgets (§ 8) | `ENRICHMENT_DAILY_LIMIT`/`OMDB_DAILY_LIMIT` s'appliquent à la passerelle | aucun budget dédié ; ces réglages ne protègent pas la passerelle (compteurs par passe du worker, que la passerelle incrémente aussi, TMDB comme OMDb). 429 `rate_limited` vient de la limite par clé ou d'un 429 TMDB. Un budget OMDb épuisé (garde global du service, ou réponse OMDb « Request limit reached! ») ⇒ `omdbStatus: "budget_exhausted"`, jamais 429 | Gérer 429 sans supposer de quota quotidien ; `budget_exhausted` peut apparaître et disparaître au rythme des passes du worker. |
| X4 | TTL configurables (§ 7) | remplacent les constantes des caches existants | ne valent que pour `scrape_cache` ; les caches existants gardent leurs constantes | Aucun (le client lit `expiresAt`). |
| X5 | Purge (§ 5.10) | purge automatique possible au démarrage ou par tâche | **route seulement** | Le client (ou Chakir) déclenche `POST /cache/purge`. |
| X6 | Purge, borne de `limit` | non bornée | `limit` 1..100 000 (422 hors bornes) | Ne pas envoyer plus de 100 000. |
| X7 | Clés sans scope (§ 4.1) | « gardent leur comportement actuel » (muet sur la passerelle) | **refusées** sur `/api/scrape/v1` (403 `insufficient_scope`) | La clé de plexhubTMM doit porter `scrape` (et `scrape.admin` pour invalider/purger). |
| X8 | Clés à scopes sur les routes existantes | 403 `insufficient_scope` | identique, corps `{"detail": {"code": "insufficient_scope", "message"}}` ; les erreurs des clés **sans** scope gardent leur `detail` texte | Aucun. |
| X9 | Migrations (§ 7) | 029 `_migration_029_scrape_gateway_cache` | **029** `_migration_029_scrape_cache` (table) + **030** `_migration_030_api_key_scopes` (colonne) | Aucun. |
| X10 | Colonne de scopes (§ 4.1) | « colonne `scopes` ou table liée » | `api_keys.scopes TEXT NOT NULL DEFAULT ''` (séparateur espace) ; API : `scopes: list[str]` | Aucun. |
| X11 | Nom de la dépendance de garde (§ 4.1) | `verify_scope("scrape")` | `verify_scrape_scope` et `verify_scrape_admin_scope` | Aucun. |
| X12 | Titre normalisé vide (§ 5.2) | non traité | 400 `invalid_parameter` | Ne pas appeler `/match` pour un titre fait de ponctuation seule. |
| X13 | Algorithme de `/match` (§ 5.2) | « même algorithme que le worker » | même score et mêmes seuils, recherche avec année puis sans ; **sans** départage par résumé, garde OMDb, repli `en-US` ni `/search/multi` du worker | Un titre `matched` par le worker peut sortir `ambiguous` ou `nomatch` de la passerelle. |
| X14 | Clé TMDB rejetée par TMDB (401) | non traité | 503 `provider_not_configured` (pas de reprise) | Traiter comme `provider_not_configured`. |
| X15 | Variables d'environnement | `SCRAPE_TTL_SUCCESS_DAYS`, `SCRAPE_TTL_NEGATIVE_DAYS` | gardées, plus `SCRAPE_RATE_LIMIT_PER_MINUTE` (défaut 300) et `SCRAPE_REQUEST_TIMEOUT_SECONDS` (défaut 25, borné 5..28) | Aucun. |
| X16 | Stockage OMDb (§ 7) | au choix | famille `omdb` de `scrape_cache` ; l'appel OMDb de la passerelle est indépendant du cache du worker | Aucun. |
| X17 | Entrée `match` d'un `matched` (§ 5.2, § 4.4) | détails « embarqués », stockés aussi sous leur clé avec les mêmes dates | l'entrée `match` ne stocke que `{confidence, tmdbId}` ; les détails viennent de l'entrée `movie`/`show` (redemandés si absente ou expirée, `cache: "miss"`). `expiresAt` de `/match` = `min(expiration match, expiration détails)` | Recopier `expiresAt` tel quel (pas de calcul) ; une invalidation de `movie\|{id}` change aussi la réponse de `/match`. |
| X18 | Défauts SQL de `scrape_cache` (§ 7) | défauts au niveau SQL (leçon de la migration 027) | **aucune** colonne n'a de défaut : toutes les valeurs sont fournies par la passerelle à chaque écriture | Aucun (le cache local de plexhubTMM peut garder ses défauts). |
| X19 | `tvdbId` de `/find` (§ 5.7) | chaîne de 1 à 12 chiffres | normalisé en entier (zéros de tête retirés) avant de former la clé : `007` et `7` donnent `1\|-\|find\|tvdb\|7` | Normaliser de la même façon dans la clé locale. |
| X20 | `/find`, résultats rendus (§ 5.7) | films, séries, épisodes | identique ; les résultats de saison et de personne de TMDB sont ignorés | Aucun. |
| X22 | Doublons du casting (§ 6.5) | « même personne, même `job` » | casting : dédoublonné sur (`tmdbPersonId`, `character`) — un acteur n'a pas de `job` ; deux rôles distincts sont gardés. Équipe : (`tmdbPersonId`, `job`) comme le contrat | Aucun. |
| X23 | Bande-annonce (§ 6.1) | « même règle de choix qu'aujourd'hui » | règle actuelle reprise telle quelle : français puis anglais, **quelle que soit** la `language` demandée (les images, elles, suivent la langue) | Une requête `en-US` peut recevoir une bande-annonce française. |
| X24 | `matches` de `/find` (§ 5.7) | `showTmdbId`/`seasonNumber`/`episodeNumber` « en plus » pour un épisode | toujours présents, `null` pour un film ou une série (règle « absent ⇒ `null` » du § 6) | Lire ces champs comme nullables. |
| X25 | Filtrage des listes (§ 6.3, § 6.6) | non précisé | candidat sans titre ni titre original écarté ; un seul épisode par numéro (le premier gagne) ; saisons d'une série plafonnées à 1 000 | Aucun en pratique. |
| X21 | `Retry-After` TMDB long | non traité | si le `Retry-After` dépasse le temps restant avant l'échéance, 429 `rate_limited` immédiat (jamais 504) | Aucun : c'est le comportement attendu par § 4.6. |

Restent tels quels : préfixe `/api/scrape/v1`, chemins et paramètres des 9 routes, scopes `scrape`/`scrape.admin`,
table `scrape_cache` et ses colonnes, formes de clés, `key_version = 1`, `schemaVersion = 1`, enveloppe, codes
d'erreur, schémas § 6.

## Clarifications

### Session 2026-10-18

- Q : Budget de la passerelle, puisque `ENRICHMENT_DAILY_LIMIT`/`OMDB_DAILY_LIMIT` ne sont pas des quotas 24 h ? → R : **pas de budget dédié** ; la limite par clé et le garde OMDb global protègent les fournisseurs (FR-033, X3).
- Q : Purge automatique du nouveau cache ? → R : **route seulement** (FR-031, X5).
- Q : Une clé sans scope peut-elle appeler `/api/scrape/v1` ? → R : **non** (FR-004, X7).
- Q : Scopes dans l'admin HTML dans B1 ? → R : **oui**, cases à cocher et colonne (FR-006).
- Questions § 11 du contrat tranchées par la session (techniques) : Q1 table unique, Q2 colonne texte, Q3 famille `omdb`, Q5 seuils réutilisés (§ « Décisions et options écartées »).

## Analyse 2bis

Faite le 2026-10-18 par un `tech-lead` neuf en lecture seule (skill `spec-quality` §2 + §3), sur ce PRD, les ADR
0008/0009 et les tickets B1. **0 CRITICAL**, 4 HIGH, 20 MEDIUM, 18 LOW (43 constats). Tous les HIGH et MEDIUM sont
corrigés dans ce PRD, les ADR et le board ; aucun n'est laissé à l'arbitrage de Chakir.

| HIGH | Correction |
|---|---|
| `language` invalide : 400 ou 422 ? | FR-008 et FR-015b : 400 `invalid_parameter` (validation à la main) ; liste exhaustive 400 / 422. |
| Payload `match` d'un `matched` et invalidation | FR-018, US2-AC6, X17 : `match` ne stocke que `{confidence, tmdbId}`, les détails vivent sous leur clé. |
| Fuite de clé par la tâche partagée du single-flight (`create_background_task` journalise l'exception avec `exc_info`) | ADR 0009 § D2.7 : la coroutine du vol traduit **toute** exception en `ProviderError` sans URL ; test avec vol orphelin. FR-035. |
| `contentRating` par région de `language` impossible avec les fonctions actuelles | B1-08 : paramètre `region` optionnel sur `_parse_movie_certification`/`_parse_tv_certification`, défaut inchangé. FR-021. |

Principaux MEDIUM traités : `Retry-After` long ⇒ 429 (FR-034, X21) ; erreurs OMDb typées (`Request limit reached!`
⇒ `budget_exhausted`, clé invalide ⇒ `not_configured`, ADR 0009 § D2.6) ; toute exception imprévue ⇒ 502 ;
limiteur localisé (FR-033) ; modèles Pydantic des corps d'administration (FR-028b) ; validation des scopes dans le
service (FR-002) ; re-tri des candidats après verdict (FR-017) ; garde **par route** (FR-015) ; compteurs partagés
(X3) ; FR-008 tracé au board ; B1-01 dépend de B1-05 ; saison sans `credits` ; X13 complété.
