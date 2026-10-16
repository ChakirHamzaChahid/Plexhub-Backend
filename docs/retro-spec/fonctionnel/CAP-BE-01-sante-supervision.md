# CAP-BE-01 — Santé & supervision

**Reconstituée le** : 2026-09-25 · recalée le 2026-10-02 (Lot 10) · **HEAD** : `aa3270f` · **Sources** : inventaire, code, tests, app TV (lecture seule). Aucun PRD ne couvre cette capacité (`docs/10-prd-*.md` ne traitent que les téléchargements) : pas de tableau d'écart fiche ↔ PRD ; les écarts sont face à `CLAUDE.md` et à l'ADR 0004.
**Périmètre** : `GET /api/health` (public), `GET /metrics` (Basic Auth dédiée), `GET /docs` + `GET /openapi.json` (Basic Auth admin), métriques métier `plexhub_*`, suivi de fraîcheur des jobs planifiés (`app/utils/job_health.py`), healthchecks Docker, contexte de requête (`request_id`) et garde-fous de logs. Renvoi : `docs/retro-spec/_genere/inventaire.md` (ne voit que `/api/health`, ligne 92, marqué `public ?` — faux positif de l'outil, cf. écart E8).

## Résumé

Le backend expose trois surfaces de supervision. Une sonde publique `/api/health` (compteurs de catalogue, version live, drapeau `isMaster`, date du dernier pipeline réussi) sert aux healthchecks Docker et à l'écran « Backend health » de l'app TV. Une exposition Prometheus `/metrics`, fermée par défaut et protégée par une Basic Auth séparée de l'admin, publie 18 métriques métier plus les métriques HTTP de l'instrumentator. Enfin `/docs` et `/openapi.json` ne sont servis qu'à l'opérateur authentifié. Personas : supervision (Docker, Prometheus), opérateur/développeur, et — indirectement — le spectateur TV.

## User stories observées

- **US-BE-01.1** (P1) — En tant que supervision, je sonde `/api/health` sans clé afin de savoir si le backend répond (healthcheck Docker, uptime).
  - Parcours : `GET /api/health` · Maestro : aucun
- **US-BE-01.2** (P1) — En tant qu'opérateur, mon Prometheus scrape `/metrics` avec `basic_auth` ; sans identifiants configurés, l'endpoint reste fermé (503).
  - Parcours : `GET /metrics` · Maestro : aucun
- **US-BE-01.3** (P1) — En tant qu'opérateur, je détecte un master gelé ou un pipeline périmé via `plexhub_pipeline_last_success_timestamp_seconds{job}`, `plexhub_is_master` et `lastPipelineSuccessAt`.
  - Parcours : `/metrics` + `/api/health` · Maestro : aucun
- **US-BE-01.4** (P2) — En tant que développeur/opérateur, j'ouvre `/docs` et `/openapi.json` avec les identifiants admin, sans exposer le schéma publiquement.
  - Parcours : `GET /docs` → `GET /openapi.json` · Maestro : aucun
- **US-BE-01.5** (P1) — En tant que spectateur TV, je teste ou ajoute un backend, puis « Backend health » m'affiche version, comptes, médias, enrichis, cassés et dernière synchro.
  - Parcours : Paramètres → Sources → tuile « Backend health » (`tv:app/src/main/java/com/chakir/plexhubtv/feature/settings/categories/SourcesSettingsScreen.kt:201-204`) · Maestro : aucun

## Exigences fonctionnelles

### Réponse de `/api/health`

- **FR-BE-037** — `GET /api/health` est public : le routeur est monté sans garde et le couple (`GET`, `/api/health`) figure dans l'allow-list de l'assertion de boot (désormais indexée par (méthode, chemin)). — *implémenté* — preuve `app/main.py:662`, `app/api/route_audit.py:66-99` · test `tests/test_auth_guard.py:119-123`, `tests/test_route_auth_assertion.py:166-175`
- **FR-BE-038** — Le champ `status` vaut toujours `"ok"` : le littéral est en dur, aucun état dégradé n'existe. — *partiel* (manque : représentation d'un état dégradé) — preuve `app/api/health.py:31` · test `tests/test_api_health.py:29` (cas nominal seulement)
- **FR-BE-039** — `version` reflète la version live de l'application (`APP_VERSION` passé à `FastAPI(version=…)`), pas un littéral. — *implémenté* — preuve `app/api/health.py:34`, `app/main.py:39` · test `tests/test_api_health.py:54-63`
- **FR-BE-040** — `accounts` compte tous les comptes Xtream, actifs ou non. — *implémenté* — preuve `app/api/health.py:19-22`, `app/api/health.py:35` · test `tests/test_api_health.py:30`, `tests/test_api_health.py:50`
- **FR-BE-041** — `totalMedia`, `enrichedMedia` (médias avec `tmdb_id`) et `brokenStreams` (`is_broken`) sont trois `COUNT` sur `media`. — *implémenté* — preuve `app/services/media_service.py:779-798`, `app/api/health.py:36-38` · test `tests/test_api_health.py:31` (`totalMedia` = 0 seulement ; aucun test de `enrichedMedia`/`brokenStreams`)
- **FR-BE-042** — `lastSyncAt` vaut le maximum de `last_synced_at` sur tous les comptes, en epoch ms, ou `null`. — *implémenté* — preuve `app/api/health.py:25-28`, `app/api/health.py:39` · test `tests/test_api_health.py:32`, `tests/test_api_health.py:51`
- **FR-BE-043** — `isMaster` publie tel quel le résultat de l'élection `fcntl.flock` ; il vaut `false` par défaut (aucun lifespan, Windows nu, erreur de verrou). — *implémenté* — preuve `app/main.py:305-322`, `app/utils/job_health.py:81-97`, `app/api/health.py:42` · test `tests/test_health_freshness.py:101-115`, `tests/test_health_freshness.py:122-150` (pas de test du câblage `lifespan → set_master`)
- **FR-BE-044** — `lastPipelineSuccessAt` vaut la date (epoch ms) du dernier passage complet du pipeline dans ce process, ou `null`. — *implémenté* — preuve `app/api/health.py:43`, `app/utils/job_health.py:73-78`, `app/main.py:353`, `app/main.py:566` · test `tests/test_health_freshness.py:122-150` (aucun test du câblage `mark_job_success("pipeline")` dans `main.py`)
- **FR-BE-045** — La réponse est sérialisée en camelCase. — *implémenté* — preuve `app/models/schemas.py:540` · test `tests/test_api_health.py:28-32`
- **FR-BE-046** — Si la base est inaccessible, `/api/health` ne dégrade pas : aucune capture d'erreur dans le handler, `get_db` relève l'exception, la réponse est un 500 et le healthcheck Docker passe « unhealthy ». — *partiel* (manque : réponse contrôlée « dégradé ») — preuve `app/api/health.py:15-44`, `app/db/database.py:99-107` · test : aucun

### Accès à `/metrics`, `/docs`, `/openapi.json`

- **FR-BE-047** — Avec des identifiants valides, `/metrics` renvoie 200 au format d'exposition Prometheus. — *implémenté* — preuve `app/api/deps.py:191-234`, `app/utils/metrics.py:280-288`, `app/main.py:760-761` · test `tests/test_metrics_auth.py:52-56`
- **FR-BE-048** — Sans identifiants, ou avec des identifiants faux, `/metrics` renvoie 401 avec `WWW-Authenticate: Basic`. — *implémenté* — preuve `app/api/deps.py:217-223`, `app/api/deps.py:233-234` · test `tests/test_metrics_auth.py:41-49` (mauvais nom d'utilisateur seul : aucun test)
- **FR-BE-049** — Si `METRICS_PASSWORD` est vide (et `METRICS_PUBLIC` faux), `/metrics` renvoie 503 « Metrics endpoint not configured » avant toute vérification d'identifiants. — *implémenté* — preuve `app/api/deps.py:212-216` · test `tests/test_metrics_auth.py:59-62`
- **FR-BE-050** — `METRICS_PUBLIC=true` neutralise le garde : `/metrics` répond 200 sans aucun contrôle, même avec un mot de passe vide. — *implémenté* — preuve `app/api/deps.py:210-211` · test `tests/test_metrics_auth.py:65-71`
- **FR-BE-051** — Un WARNING est émis au démarrage quand `METRICS_PUBLIC` est actif, sinon quand `METRICS_PASSWORD` est vide (les deux ne se cumulent pas). — *implémenté* — preuve `app/config.py:528-536` · test `tests/test_metrics_auth.py:100-136` (voir question Q8 sur la destination réelle du log)
- **FR-BE-052** — Les deux champs d'identifiants sont comparés en temps constant (`secrets.compare_digest`), tous deux évalués avant de brancher. — *implémenté* — preuve `app/api/deps.py:224-233` · test `tests/test_metrics_auth.py:139-149` (inspection du source, pas de mesure de timing)
- **FR-BE-053** — `/docs` et `/openapi.json` sont servis derrière la Basic Auth admin ; les URLs par défaut de FastAPI sont désactivées et `/redoc` n'existe pas. — *implémenté* — preuve `app/main.py:616-618`, `app/main.py:706-713` · test : aucun test HTTP (`tests/test_ai_openapi.py:17-20` appelle `app.openapi()` sans passer par HTTP)

### Métriques

- **FR-BE-054** — Le système instrumente automatiquement les requêtes HTTP (familles standard de `prometheus-fastapi-instrumentator`, statuts groupés) en excluant `/metrics` lui-même. — *implémenté* — preuve `app/utils/metrics.py:270-288` · test : aucun dédié (`tests/test_metrics_auth.py:56` vérifie seulement la présence de `plexhub_`)
- **FR-BE-055** — Dix-huit métriques métier `plexhub_*` sont déclarées dans un module unique et alimentées par les capacités concernées (sync, TMDB/OMDb, validation, panne fournisseur, unifié, téléchargements, DAV, Plex). — *implémenté* — preuve `app/utils/metrics.py:53-267` (18 déclarations comptées), alimentation `app/workers/sync_worker.py:1756`, `app/workers/download_worker.py:192`, `app/dav/relay.py:222` · test `tests/test_metrics_registry.py:112-158` (existence) ; câblage testé pour les téléchargements, OMDb et le chemin unifié (`tests/test_download_metrics.py`, `tests/test_omdb_metrics.py`, `tests/test_media_unified_path_metric.py`), non testé pour les autres (Q6)
- **FR-BE-056** — Les séries à cardinalité fermée sont initialisées à 0 au boot ; les séries à étiquette ouverte (`account_id`) ne le sont volontairement pas. — *implémenté* — preuve `app/utils/metrics.py:101-113`, `app/utils/metrics.py:155-165`, `app/utils/metrics.py:212-227` · test `tests/test_metrics_registry.py:42-104`
- **FR-BE-057** — `plexhub_is_master` est une jauge 0/1 sans étiquette, écrite par `set_master`. — *implémenté* — preuve `app/utils/metrics.py:170-173`, `app/utils/job_health.py:90` · test `tests/test_health_freshness.py:101-115`

### Fraîcheur des jobs planifiés

- **FR-BE-058** — Un job planifié marque son succès (`mark_job_success`) seulement s'il retourne sans exception ; l'exception est propagée telle quelle et l'horodatage n'avance pas. — *implémenté* — preuve `app/utils/job_health.py:56-70`, `app/utils/job_health.py:100-125` · test `tests/test_health_freshness.py:37-98`
- **FR-BE-059** — Six jobs sont suivis par la jauge `plexhub_pipeline_last_success_timestamp_seconds{job}` : `pipeline`, `health_check`, `epg_cleanup`, `subtitle_cache_cleanup`, `db_backup`, `plex_catalogue_sync` ; `trailer_cache_purge` est décoré mais absent de la liste initialisée à 0. — *partiel* (manque : `trailer_cache_purge` sans série initiale ; aucun garde contre l'oubli) — preuve `app/main.py:371`, `app/main.py:380`, `app/main.py:395`, `app/main.py:415`, `app/main.py:432`, `app/main.py:487`, `app/utils/metrics.py:155-163` · test : aucun du câblage `add_job` ↔ `track_job` (`tests/test_startup_wiring.py:281-282` ne vérifie que deux ids de job)

### Docker et cas limites de supervision

- **FR-BE-060** — L'image et le compose portent un healthcheck sur `/api/health` (30 s / 10 s / 3 essais / démarrage 60 s), en `python` (pas de `curl` dans l'image), port lu à l'exécution ; il est « healthy » dès qu'une réponse 2xx arrive, sans regarder `isMaster` ni la fraîcheur. — *implémenté* — preuve `Dockerfile:62-63`, `docker-compose.yml:67-72` · test : aucun
- **FR-BE-061** — Chaque requête reçoit un `request_id` (celui du client, sinon un identifiant généré), renvoyé en `X-Request-ID` et injecté dans les logs. — *implémenté* — preuve `app/utils/request_context.py:18-37`, `app/main.py:639` · test `tests/test_api_health.py:66-70` (écho de l'en-tête seulement ; génération et filtre de log non testés)
- **FR-BE-062** — Le logger `httpx` est épinglé à WARNING pour ne pas écrire les URLs Xtream (avec identifiants) dans les logs. — *implémenté* — preuve `app/main.py:99-109` · test : aucun
- **FR-BE-063** — La rotation des fichiers de log avale les `PermissionError` (verrou tenu par un autre process, cas Windows). — *implémenté* — preuve `app/main.py:47-60`, `app/main.py:76-81` · test : aucun
- **FR-BE-064** — Le démarrage échoue (`RuntimeError`) si une route `/api/*` hors allow-list (couples (méthode, chemin), `app/api/route_audit.py:115-118`) n'a aucun des gardes d'authentification reconnus ; `/metrics`, `/docs` et `/openapi.json` (hors préfixe `/api`) échappent à cette marche. — *implémenté* (cas limite de sécurité, listé ici car il borne l'exposition publique) — preuve `app/api/route_audit.py:121-154`, `app/main.py:754-755` · test `tests/test_route_auth_assertion.py:32-218`

## Règles métier

- **RG-BE-01.1** — Priorité d'accès à `/metrics` : `METRICS_PUBLIC` > mot de passe vide (503) > identifiants absents (401) > identifiants faux (401). — preuve `app/api/deps.py:210-234`
- **RG-BE-01.2** — Les identifiants de `/metrics` forment un couple distinct de `ADMIN_*` (séparation de domaine : un scraper compromis n'ouvre pas l'UI admin). — preuve `app/api/deps.py:197-203`
- **RG-BE-01.3** — Le code ne définit aucun seuil de « périmé » : il publie des horodatages (`lastPipelineSuccessAt` en **millisecondes**, jauge Prometheus en **secondes**) ; la décision d'alerte (`time() - jauge`) est déléguée à l'opérateur. — preuve `app/utils/job_health.py:44-48`, `app/utils/job_health.py:68-70`, `app/utils/metrics.py:145-149`
- **RG-BE-01.4** — « Jamais réussi dans ce process » s'exprime `null` dans `/api/health` mais `0` dans la jauge (deux représentations du même état). — preuve `app/utils/job_health.py:78`, `app/utils/metrics.py:164-165`
- **RG-BE-01.5** — Un process esclave n'exécute aucun job : sa fraîcheur reste vide et `isMaster=false` — signal voulu, pas une panne. — preuve `app/utils/job_health.py:14-18`, `app/utils/job_health.py:50-53`
- **RG-BE-01.6** — Un job qui retourne sans lever est compté « réussi », même s'il a été ignoré ou a échoué en interne (voir E3). — preuve `app/utils/job_health.py:116-121`
- **RG-BE-01.7** — Les états (fraîcheur, `isMaster`, registre Prometheus) sont process-locaux, sans mode multiprocess ; le Dockerfile lance un seul process uvicorn. — preuve `app/utils/job_health.py:14-18`, `Dockerfile:67`
- **RG-BE-01.8** — Aucune étiquette de métrique ne porte de secret, d'URL ni de titre ; les étiquettes ouvertes ne sont pas pré-créées. — preuve `app/utils/metrics.py:39-41`, `app/utils/metrics.py:59`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| `/api/health`, base indisponible | 500 non contrôlé ; Docker « unhealthy » ; pas de statut dégradé | `app/api/health.py:15-44`, `app/db/database.py:99-107` |
| `/api/health`, process esclave ou sans lifespan | `isMaster=false`, `lastPipelineSuccessAt=null` | `app/utils/job_health.py:53`, `app/utils/job_health.py:78` |
| `/metrics`, `METRICS_PASSWORD` vide | 503 « Metrics endpoint not configured » | `app/api/deps.py:212-216` |
| `/metrics`, `METRICS_PUBLIC=true` | 200 sans identifiants, WARNING au boot | `app/api/deps.py:210-211`, `app/config.py:528-534` |
| `/metrics`, identifiants absents ou faux | 401 + `WWW-Authenticate: Basic` | `app/api/deps.py:217-223` |
| `/docs` ou `/openapi.json`, `ADMIN_PASSWORD` vide | 503 | `app/api/deps.py:123-127` |
| `/docs` ou `/openapi.json`, identifiants absents ou faux | 401 | `app/api/deps.py:128-145` |
| `/redoc` | non servi (`redoc_url=None`) | `app/main.py:617` |
| Job qui lève une exception | horodatage non avancé, exception propagée | `app/utils/job_health.py:118-121` |
| Job qui retourne tôt sans lever | compté « réussi » (fausse fraîcheur) | `app/workers/health_check_worker.py:339-344`, `app/utils/job_health.py:119-120` |
| Pipeline planifié qui échoue | exception loguée, pas de marque de succès | `app/main.py:343-355` |
| Pipeline planifié déjà en cours | tick ignoré avec un WARNING, pas de marque | `app/main.py:336-341` |
| Verrou `fcntl` : `OSError` | `is_master=False` (esclave silencieux) | `app/main.py:312-322` |
| Rotation de log bloquée | `PermissionError` avalée, l'écriture continue | `app/main.py:55-60` |
| Route `/api/*` sans garde | démarrage refusé | `app/api/route_audit.py:142-154` |

## Dépendances

- Autres capacités : lit les compteurs de `CAP-BE-09` (catalogue) et `CAP-BE-04` (comptes) ; ses jauges sont alimentées par `CAP-BE-05` (sync), `CAP-BE-06` (enrichissement), `CAP-BE-07`/`CAP-BE-08` (validation, panne fournisseur), `CAP-BE-17` (DAV), `CAP-BE-18`/`CAP-BE-19` (téléchargements), `CAP-BE-09` (chemin unifié). `CAP-BE-02` fournit les gardes Basic Auth (`deps.py`).
- Autre dépôt : `GET api/health` appelé par la TV (`tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:12-13`). DTO lu : `status`, `version`, `accounts`, `totalMedia`, `enrichedMedia`, `brokenStreams` (obligatoires), `lastSyncAt` (optionnel) — `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendDto.kt:96-104`. `isMaster` et `lastPipelineSuccessAt` sont ignorés (`ignoreUnknownKeys`). L'ajout d'un serveur exige `status == "ok"` (`tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:88`). Aucun appel TV à `/metrics`, `/docs` ni `/openapi.json`.
- Données : tables `media` et `xtream_accounts` (lecture) — voir `docs/retro-spec/technique/db/`.
- Bibliothèque : `prometheus-fastapi-instrumentator` < 8 et `fastapi` < 0.116, liés (`requirements.txt`, `CLAUDE.md` §10).

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| E1 — `CLAUDE.md` §9 piège 10 dit encore « `/metrics` non authentifié (CR-S02) », contredit par §9 piège 19b et par le code | spec ↔ code (doc) | corriger le piège 10 via `/sync-context` |
| E2 — `CLAUDE.md` annonce « 5 métriques métier » ; 18 sont déclarées ; ses renvois `app/main.py:230-231` (flock), `app/main.py:31-36`, `app/main.py:108` sont périmés (code en `app/main.py:305-310`, `43-56`, `105`) ; « 0 test de rejet » sur le garde d'auth est contredit par `tests/test_auth_guard.py:119` et `tests/test_metrics_auth.py` | spec ↔ code (doc) | recaler via `/sync-context` |
| E3 — Fausse fraîcheur : `track_job` compte comme succès un retour anticipé (`STREAM_VALIDATION_ENABLED=false`, verrou déjà pris) ; `run_full_sync` renvoie un statut `error`/`disabled`/`already_running` sans lever ; `sync_account` avale ses exceptions et `run_all_accounts` continue, donc le pipeline peut être marqué réussi même si tous les syncs de compte échouent | code (comportement) | à trancher par Chakir (Q5) |
| E4 — `trailer_cache_purge` absent de `_PIPELINE_JOB_NAMES`, et rien ne détecte l'oubli (`tests/test_metrics_registry.py:120-125` ne le voit pas) | test manquant / code | ajouter le nom à la liste + un test de cohérence liste ↔ `track_job` |
| E5 — `plexhub_enrichment_queue_size` ne met à jour que les statuts renvoyés par le `GROUP BY` : un statut tombé à 0 ligne garde son ancienne valeur (`app/workers/enrichment_worker.py:758-759`) | code (jauge périmée) | remettre à 0 les statuts absents, comme pour `download_jobs` |
| E6 — `tests/test_health_freshness.py:152-176` ne vérifie que l'état initial ; il n'exerce pas le pipeline de `main.py` | test tautologique | test qui pilote réellement `scheduled_sync_enrich_generate` |
| E7 — Commentaire `app/main.py:638` : `RequestIdMiddleware` « ajouté en dernier donc enveloppe les autres », or `AdminCsrfMiddleware` (`app/main.py:641-642`) et l'instrumentator (`app/main.py:761`) sont ajoutés après | commentaire inexact | corriger le commentaire, vérifier Q7 |
| E8 — L'inventaire généré marque `/api/health` « public ? » et ne voit ni `/metrics`, `/docs`, `/openapi.json` | outil d'inventaire | corriger `tools/retro-spec/inventaire.py` (déjà noté dans `00-carte.md`) |
| E9 — Trous de test : `/docs`/`/openapi.json`/`/redoc` en HTTP, healthchecks Docker et compose, épinglage `httpx`, `SafeRotatingFileHandler`, `enrichedMedia`/`brokenStreams`, câblage `lifespan → set_master` et `add_job ↔ track_job` | test manquant | tests ciblés (priorité : `/docs` protégé, FR-BE-053) |
| E10 — Couplage TV : la TV exige `status == "ok"` et déclare `enrichedMedia`/`brokenStreams` obligatoires ; ajouter un état « dégradé » à `status` ou retirer ces champs casserait l'ajout de serveur | contrat | à traiter avant d'implémenter FR-BE-038/046 |

## Questions ouvertes

- **Q1** — `/api/health` fait 4 `COUNT` par appel : reste-t-il sous les 10 s du healthcheck pendant une écriture longue (`busy_timeout` 60 s) ? — **à vérifier par test**.
- **Q2** — Le coût de ces `COUNT` sur un catalogue de plusieurs dizaines de milliers de lignes, toutes les 30 s, est-il négligeable (index sur `is_broken` et `tmdb_id` non vérifiés) ? — **à vérifier par test**.
- **Q3** — Avec `uvicorn --workers N` (déconseillé), chaque worker exposerait son propre `/metrics` sans mode multiprocess : quel comportement côté scrape ? — **à vérifier par test**.
- **Q4** — `/docs` charge-t-il Swagger UI depuis un CDN externe (comportement attendu de `get_swagger_ui_html`, `app/main.py:708`) ? Le code ne le tranche pas sans relire la bibliothèque — **à vérifier par test**.
- **Q5** — Un job ignoré ou terminé en erreur interne doit-il compter comme « succès » pour l'alerte de fraîcheur (E3) ? Décision produit non tranchée par le code ; le pipeline complet avec échec de tous les comptes doit être exécuté pour confirmer — **à vérifier par test**.
- **Q6** — Le câblage des métriques `sync_duration_seconds`, `streams_alive_ratio`, `account_outage_strikes`, `enrichment_queue_size`, `tmdb_*`, `plex_sync_total` et `dav_*` est-il couvert par un test ? Aucune recherche exhaustive n'a été faite — **à vérifier par test**.
- **Q7** — Les 403 CSRF de `/admin*` portent-ils un `X-Request-ID` (E7) ? — **à vérifier par test**.
- **Q8** — `Settings()` est instancié à l'import de `app.config`, avant l'attachement des handlers du logger `plexhub` : les WARNING de `app/config.py:528-536` atteignent-ils `plexhub.log` ou seulement stderr ? — **à vérifier par test**.
- **Q9** — Un `X-Request-ID` fourni par le client est repris sans contrôle de longueur ni de format (`app/utils/request_context.py:22-28`) : quel impact sur les logs ? — **à vérifier par test**.
