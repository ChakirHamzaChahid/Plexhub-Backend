# Lot B1 — note de remise à plexhubTMM

**Date** : 2026-10-18 · **De** : PlexHub Backend, branche `develop` · **Pour** : le dépôt plexhubTMM (non modifié par
cette note ; c'est à plexhubTMM de reporter ce qui suit dans `docs/15-contrat-b1-backend.md` et dans son code).
**Contrat d'entrée** : `plexhubTMM/docs/15-contrat-b1-backend.md` (proposé le 2026-10-18). Le backend a **livré ce contrat
avec 33 écarts**, tous listés plus bas ; aucun ne change le chemin, le nom ou le sens d'une route, mais plusieurs
changent un comportement que le client doit connaître.

## 1. Ce qui est livré

La passerelle de scraping `/api/scrape/v1` : 9 routes JSON qui servent les fiches TMDB/OMDb à plexhubTMM **sans que
l'outil détienne de clé TMDB ou OMDb**, derrière un cache versionné côté backend.

| Méthode et chemin | Rôle | Scope |
|---|---|---|
| `GET /api/scrape/v1/match` | rapprochement (type, titre nettoyé, année, langue) → `matched` + détails, `ambiguous` + candidats, ou `nomatch` | `scrape` |
| `GET /api/scrape/v1/search` | candidats d'une recherche libre (non persisté) | `scrape` |
| `GET /api/scrape/v1/movie/{tmdbId}` | fiche film + notes IMDb / Rotten Tomatoes / Metacritic | `scrape` |
| `GET /api/scrape/v1/tv/{tmdbId}` | fiche série + notes | `scrape` |
| `GET /api/scrape/v1/tv/{tmdbId}/season/{seasonNumber}` | une saison et tous ses épisodes (un seul appel TMDB) | `scrape` |
| `GET /api/scrape/v1/find` | `imdbId` ou `tvdbId` → fiches TMDB | `scrape` |
| `GET /api/scrape/v1/cache/stats` | état du cache et compteurs depuis le démarrage | `scrape` |
| `POST /api/scrape/v1/cache/invalidate` | supprimer des entrées (clés ou famille + `tmdbId`) | `scrape.admin` |
| `POST /api/scrape/v1/cache/purge` | supprimer les entrées expirées ou d'une version non courante | `scrape.admin` |

Aussi livrés : les **scopes de clés d'API** (`scrape`, `scrape.admin`, indépendants ; une clé à scopes est confinée à ces
routes, 403 `insufficient_scope` partout ailleurs), la table `scrape_cache` (`key_version = 1`, `schemaVersion = 1`),
les migrations 029 et 030, la limite de 300 requêtes/minute par clé, l'échéance de 25 s par requête, la coalescence des
appels concurrents identiques et la limite de 64 Kio sur les corps.

## 2. Où est le contrat machine

`docs/contracts/scrape-v1.openapi.json` (dans le dépôt du backend) : les 9 chemins et **tous** les schémas qu'ils
référencent, en OpenAPI 3.1, sans hôte ni valeur de configuration. Le fichier est **généré** par
`tests/test_scrape_v1_openapi_export.py` à partir de l'application et le test échoue s'il diverge : il ne peut donc pas
dériver du code. Les versions propres du contrat y figurent (`x-key-version`, `x-schema-version`). C'est ce fichier que
`/schema-sync` de plexhubTMM doit lire. Les noms de composants sont ceux de l'application (voir X26 : `ScrapeSearchResponse`
et `InvalidateBody`).

## 3. Comment obtenir la clé

La clé n'est **pas** dans ce dépôt ni dans cette note. Chakir la crée dans l'écran d'administration du backend
(`/admin/keys`, cases à cocher `scrape` / `scrape.admin`) ou par `POST /api/admin/keys` avec la clé maître, la copie une
seule fois, et la remet à plexhubTMM par un canal privé. Marche à suivre complète (scopes recommandés, expiration,
révocation) : `docs/33-ops-scrape-gateway.md` § 1. Points qui concernent le client :

- Une clé **sans scope** (toutes les clés antérieures) est refusée par la passerelle (X7) : il faut une clé neuve portant `scrape`.
- La clé s'envoie dans l'en-tête `X-API-Key`. Révoquée ou expirée : 401 `invalid_api_key`. Scope manquant : 403
  `insufficient_scope`.
- Prévoir un renouvellement avant l'expiration (créer la nouvelle clé, basculer, révoquer l'ancienne).

## 4. Ce que plexhubTMM doit reporter ou faire

1. Lire `docs/contracts/scrape-v1.openapi.json` comme source des types (au lieu du § 6 du contrat provisoire).
2. Reporter le tableau X1 → X33 ci-dessous dans `15-contrat-b1-backend.md` (dernière colonne = action côté plexhubTMM).
3. Gérer tous les codes d'erreur : 400 `invalid_parameter` (dont `language` hors liste, X32), 401 `invalid_api_key`, 403
   `insufficient_scope`, 413 `invalid_parameter` (corps > 64 Kio, X33), 422 (validation FastAPI), 429 `rate_limited`
   (respecter `Retry-After`), 502 `upstream_error`, 503 `provider_not_configured` / `upstream_unavailable`, 504
   `upstream_timeout`. Corps : `{"detail": {"code", "message", "retryAfterSeconds"?}}`.
4. Ne demander que `fr-FR` ou `en-US` (X32), n'envoyer `refresh` qu'à `match`/`movie`/`tv`/`season` (X30), lire
   `expiresAt` tel quel (X17, X31) et ne pas compter sur `ENRICHMENT_DAILY_LIMIT`/`OMDB_DAILY_LIMIT` (X3).
5. Prévoir la purge du cache : le backend ne purge jamais seul (X5) ; l'opérateur ou plexhubTMM appelle `POST /cache/purge`
   (clé `scrape.admin`).

## 5. Tableau des écarts X1 → X33

Source : `docs/10-prd-scrape-gateway.md` § « Écarts au contrat » (copie fidèle des quatre premières colonnes ; la dernière
est l'action pour plexhubTMM). « Contrat » = ce que disait `15-contrat-b1-backend.md` ; « Retenu par B1 » = ce que le
backend a livré.

| # | Sujet | Contrat (provisoire) | Retenu par B1 | Effet côté client | À reporter dans plexhubTMM |
|---|---|---|---|---|---|
| X1 | État des lieux § 2, ligne OMDb | « seules `imdbRating` et `imdbVotes` sont lues » | 11 champs sont lus ; manquent `Ratings[]` et `Metascore` | Aucun (correction documentaire). | Corriger la phrase de l'état des lieux (§ 2) : OMDb lit déjà 11 champs. Aucun code. |
| X2 | État des lieux § 2, `clean_title` | le titre du worker passe par `clean_title` avant `make_key` | le worker ne le fait pas lui-même ; il utilise `Media.title`, nettoyé au sync | La parité de clé est garantie client ⇄ passerelle ; **pas** client ⇄ worker pour des lignes anciennes. | Ne pas supposer que la clé locale égale celle du worker pour d'anciennes lignes `media` ; la parité tient seulement client ⇄ passerelle. |
| X3 | Budgets (§ 8) | `ENRICHMENT_DAILY_LIMIT`/`OMDB_DAILY_LIMIT` s'appliquent à la passerelle | aucun budget dédié ; ces réglages ne protègent pas la passerelle (compteurs par passe du worker, que la passerelle incrémente aussi, TMDB comme OMDb). 429 `rate_limited` vient de la limite par clé ou d'un 429 TMDB. Un budget OMDb épuisé (garde global du service, ou réponse OMDb « Request limit reached! ») ⇒ `omdbStatus: "budget_exhausted"`, jamais 429 | Gérer 429 sans supposer de quota quotidien ; `budget_exhausted` peut apparaître et disparaître au rythme des passes du worker. | Retirer du § 8 l'hypothèse « `ENRICHMENT_DAILY_LIMIT`/`OMDB_DAILY_LIMIT` protègent la passerelle ». Gérer 429 sans supposer de quota quotidien ; accepter que `budget_exhausted` apparaisse et disparaisse. |
| X4 | TTL configurables (§ 7) | remplacent les constantes des caches existants | ne valent que pour `scrape_cache` ; les caches existants gardent leurs constantes | Aucun (le client lit `expiresAt`). | Rien à faire ; lire `expiresAt` dans l'enveloppe. |
| X5 | Purge (§ 5.10) | purge automatique possible au démarrage ou par tâche | **route seulement** ; elle retire les entrées expirées et celles d'une `key_version` **ou d'une `schema_version`** non courante | Le client (ou Chakir) déclenche `POST /cache/purge`. | Prévoir l'appel de `POST /cache/purge` (manuel ou planifié) ; le backend ne purge jamais seul. |
| X6 | Purge, borne de `limit` | non bornée | `limit` 1..100 000 (422 hors bornes) | Ne pas envoyer plus de 100 000. | Ne jamais envoyer `limit` > 100 000 (422 sinon). |
| X7 | Clés sans scope (§ 4.1) | « gardent leur comportement actuel » (muet sur la passerelle) | **refusées** sur `/api/scrape/v1` (403 `insufficient_scope`) | La clé de plexhubTMM doit porter `scrape` (et `scrape.admin` pour invalider/purger). | Utiliser une clé portant `scrape` (et `scrape.admin` pour invalider/purger) ; une clé sans scope reçoit 403. |
| X8 | Clés à scopes sur les routes existantes | 403 `insufficient_scope` | identique, corps `{"detail": {"code": "insufficient_scope", "message"}}` ; les erreurs des clés **sans** scope gardent leur `detail` texte | Aucun. | Rien à faire. |
| X9 | Migrations (§ 7) | 029 `_migration_029_scrape_gateway_cache` | **029** `_migration_029_scrape_cache` (table) + **030** `_migration_030_api_key_scopes` (colonne) | Aucun. | Rien à faire (numérotation des migrations côté backend). |
| X10 | Colonne de scopes (§ 4.1) | « colonne `scopes` ou table liée » | `api_keys.scopes TEXT NOT NULL DEFAULT ''` (séparateur espace) ; API : `scopes: list[str]` | Aucun. | Rien à faire (le client ne voit que `scopes: [...]` à la création de la clé). |
| X11 | Nom de la dépendance de garde (§ 4.1) | `verify_scope("scrape")` | `verify_scrape_scope` et `verify_scrape_admin_scope` | Aucun. | Rien à faire (noms internes des gardes). |
| X12 | Titre normalisé vide (§ 5.2) | non traité | 400 `invalid_parameter` | Ne pas appeler `/match` pour un titre fait de ponctuation seule. | Gérer le 400 `invalid_parameter` ; ne pas appeler `/match` pour un titre fait de ponctuation seule. |
| X13 | Algorithme de `/match` (§ 5.2) | « même algorithme que le worker » | même score et mêmes seuils, recherche avec année puis sans ; **sans** départage par résumé, garde OMDb, repli `en-US` ni `/search/multi` du worker | Un titre `matched` par le worker peut sortir `ambiguous` ou `nomatch` de la passerelle. | Accepter qu'un titre `matched` par le worker sorte `ambiguous`/`nomatch` de la passerelle ; ne pas comparer les verdicts. |
| X14 | Clé TMDB rejetée par TMDB (401 ou 403) | non traité | 503 `provider_not_configured` (pas de reprise) | Traiter comme `provider_not_configured`. | Traiter ce 503 comme `provider_not_configured` (pas de reprise automatique). |
| X15 | Variables d'environnement | `SCRAPE_TTL_SUCCESS_DAYS`, `SCRAPE_TTL_NEGATIVE_DAYS` | gardées, plus `SCRAPE_RATE_LIMIT_PER_MINUTE` (défaut 300) et `SCRAPE_REQUEST_TIMEOUT_SECONDS` (défaut 25, borné 5..28), plus `SCRAPE_ALLOWED_LANGUAGES` (défaut `fr-FR,en-US`, X32) | Aucun. | Documenter les 5 réglages côté exploitation ; aucun impact sur le client. |
| X16 | Stockage OMDb (§ 7) | au choix | famille `omdb` de `scrape_cache` ; l'appel OMDb de la passerelle est indépendant du cache du worker | Aucun. | Rien à faire. |
| X17 | Entrée `match` d'un `matched` (§ 5.2, § 4.4) | détails « embarqués », stockés aussi sous leur clé avec les mêmes dates | l'entrée `match` ne stocke que `{confidence, tmdbId}` ; les détails viennent de l'entrée `movie`/`show` (redemandés si absente ou expirée, `cache: "miss"`). `expiresAt` de `/match` = `min(expiration match, expiration détails)` | Recopier `expiresAt` tel quel (pas de calcul) ; une invalidation de `movie\|{id}` change aussi la réponse de `/match`. Le `cache` d'une réponse composite (détails + OMDb) décrit l'entrée TMDB : `"hit"` même si les notes OMDb ont été demandées pendant la requête (ADR 0009 D2.10). | Recopier `expiresAt` tel quel (aucun calcul) ; `cache` décrit l'entrée TMDB ; une invalidation de `movie|{id}` change aussi `/match`. |
| X18 | Défauts SQL de `scrape_cache` (§ 7) | défauts au niveau SQL (leçon de la migration 027) | **aucune** colonne n'a de défaut : toutes les valeurs sont fournies par la passerelle à chaque écriture | Aucun (le cache local de plexhubTMM peut garder ses défauts). | Rien à faire (le cache local peut garder ses défauts SQL). |
| X19 | `tvdbId` de `/find` (§ 5.7) | chaîne de 1 à 12 chiffres | normalisé en entier (zéros de tête retirés) avant de former la clé : `007` et `7` donnent `1\|-\|find\|tvdb\|7` | Normaliser de la même façon dans la clé locale. | Normaliser `tvdbId` en entier (sans zéros de tête) dans la clé locale `find|tvdb|…`. |
| X20 | `/find`, résultats rendus (§ 5.7) | films, séries, épisodes | identique ; les résultats de saison et de personne de TMDB sont ignorés | Aucun. | Rien à faire. |
| X21 | `Retry-After` TMDB long | non traité | si le `Retry-After` dépasse le temps restant avant l'échéance, 429 `rate_limited` immédiat (jamais 504) | Aucun : c'est le comportement attendu par § 4.6. | Rien à faire (429 immédiat attendu, cf. § 4.6). |
| X22 | Doublons du casting (§ 6.5) | « même personne, même `job` » | casting : dédoublonné sur (`tmdbPersonId`, `character`) — un acteur n'a pas de `job` ; deux rôles distincts sont gardés. Équipe : (`tmdbPersonId`, `job`) comme le contrat | Aucun. | Rien à faire (dédoublonnage du casting sur personne + rôle). |
| X23 | Bande-annonce (§ 6.1) | « même règle de choix qu'aujourd'hui » | règle actuelle reprise telle quelle : français puis anglais, **quelle que soit** la `language` demandée (les images, elles, suivent la langue) | Une requête `en-US` peut recevoir une bande-annonce française. | Accepter qu'une requête `en-US` reçoive une bande-annonce française. |
| X24 | `matches` de `/find` (§ 5.7) | `showTmdbId`/`seasonNumber`/`episodeNumber` « en plus » pour un épisode | toujours présents, `null` pour un film ou une série (règle « absent ⇒ `null` » du § 6) | Lire ces champs comme nullables. | Lire `showTmdbId`/`seasonNumber`/`episodeNumber` comme nullables (toujours présents). |
| X25 | Filtrage des listes (§ 6.3, § 6.6) | non précisé | candidat sans titre ni titre original écarté ; un seul épisode par numéro (le premier gagne) ; saisons d'une série plafonnées à 1 000 | Aucun en pratique. | Rien à faire en pratique. |
| X26 | Noms OpenAPI (contrat machine M2-02) | non fixés | réponse de `/search` = `ScrapeSearchResponse` (le nom `SearchResponse` existe déjà pour `/api/ai` : FastAPI aurait renommé les deux composants) ; corps de `/cache/invalidate` = `InvalidateBody` (enveloppe de `InvalidateRequest`) | JSON échangé inchangé ; seul `/schema-sync` voit ces noms. | Utiliser `ScrapeSearchResponse` et `InvalidateBody` dans `/schema-sync` ; le JSON échangé est inchangé. |
| X27 | `/match` : 404 TMDB sur les détails d'un `matched` | non traité | 502 `upstream_error`. Rien n'est mis en cache au premier calcul ; si l'entrée `match` est déjà fraîche et que les détails répondent 404, `movie\|{id}` (ou `show\|{id}`) est mis en cache `not_found` (l'entrée `match` reste) | Réessayer plus tard (borné). | Gérer le 502 `upstream_error` de `/match` quand les détails d'un `matched` répondent 404 ; réessayer plus tard (borné). |
| X28 | `/match` : `confidence` d'un `nomatch` | « toujours » présent | toujours `null` pour un `nomatch` (hit ou miss : l'entrée négative n'a pas de payload) | Lire `confidence` comme nullable. | Lire `confidence` comme nullable (toujours `null` pour un `nomatch`). |
| X29 | 503 `upstream_unavailable` | `retryAfterSeconds` dans l'exemple | toujours `retryAfterSeconds: 30` + en-tête `Retry-After: 30` | Aucun. | Rien à faire (`retryAfterSeconds: 30` et en-tête `Retry-After: 30`). |
| X30 | `refresh` (§ 4.4) | décrit en général | accepté par `match`, `movie`, `tv`, `season` seulement (pas `find` ni `search`, conforme § 5.3 et § 5.7) | Ne pas envoyer `refresh` à `/find`. | Ne pas envoyer `refresh` à `/find` ni à `/search`. |
| X31 | `expiresAt` d'une réponse composite (§ 4.4) | formule littérale `min(expiration TMDB, fetchedAt + TTL négatif)` quand OMDb échoue ; `min(expiration match, expiration détails)` pour `/match` | la valeur obtenue est bornée à l'instant de la réponse : `expiresAt = max(maintenant, valeur)`, jamais dans le passé (un `hit` sur une entrée TMDB de plus de 3 jours dont la partie OMDb est en `error`/`budget_exhausted` donnait sinon une échéance déjà écoulée) | Traiter `expiresAt <= maintenant` comme « à redemander » : pour ces réponses, c'est la valeur « maintenant » du serveur. | Traiter `expiresAt <= maintenant` comme « à redemander » ; pour ces réponses c'est l'instant serveur. |
| X32 | `language` (§ 4.3) | tout motif `^[a-z]{2}-[A-Z]{2}$` | limitée à `SCRAPE_ALLOWED_LANGUAGES` (défaut `fr-FR,en-US`, balises `xx-XX` séparées par des virgules ; éléments invalides ignorés avec un WARNING au démarrage, liste vide ⇒ défaut) ; `TMDB_LANGUAGE` est toujours accepté (valeur par défaut quand le paramètre est absent). Hors liste ⇒ 400 `invalid_parameter` sur `/match`, `/search`, `/movie`, `/tv`, `/tv/.../season/...` | Ne demander que `fr-FR` ou `en-US` (ou une langue ajoutée par l'opérateur). Chaque langue distincte crée ses propres lignes de cache : la liste fermée borne la croissance de la table. | Ne demander que `fr-FR` ou `en-US` (ou une langue ajoutée par l'opérateur) ; sinon 400. |
| X33 | Taille du corps des requêtes | non traité | tout corps de plus de 64 Kio (65 536 octets) sur `/api/scrape/v1/` ⇒ 413 `{"detail": {"code": "invalid_parameter", "message": "Request body too large"}}` : `Content-Length` trop grand refusé sans lire le corps, corps `chunked` compté au fil de l'eau et coupé au-delà. Les autres routes ne sont pas touchées | Un corps d'invalidation (≤ 500 clés de ≤ 1 024 caractères, 422 au-delà) tient largement ; ne rien envoyer de plus gros. | Ne jamais envoyer un corps de plus de 64 Kio (413 sinon) ; ≤ 500 clés de ≤ 1 024 caractères par invalidation. |
## 6. Ce que cette note ne couvre pas

- Le déploiement sur le homelab (lot séparé, sur accord de Chakir) : tant qu'il n'a pas eu lieu, la passerelle n'existe
  que sur `develop`.
- L'écriture de métadonnées dans la table `media` (lot M3 de plexhubTMM, autre API).
- Les budgets quotidiens TMDB/OMDb dédiés à la passerelle, la purge automatique, la recherche OMDb par titre, les
  appels TMDB par épisode et le repli de langue : hors lot, décisions du 2026-10-18.
- Les constats ouverts côté backend (budget partagé avec le worker, deux caches TMDB, état en mémoire d'un process) :
  `docs/context/etat-reel.md`, section « Constats ouverts du lot B1 ».
