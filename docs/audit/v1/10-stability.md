# Audit v1 — Phase 1 : Stabilité & modes de panne

> HEAD `9da9d46` (v1.7.1), branche `develop`. Lecture indépendante — chaque affirmation re-prouvée au code.
> Contexte runtime : serveur smoke booté (102 routes, DB fraîche 001→022 sans warning), logs `plexhub.log` avec `request_id`.

## 0. Points VÉRIFIÉS SAINS (non-findings, consignés pour le scorecard)

- **Boucle de drain download résiliente** : tick enveloppé `try/except Exception` (les `OperationalError` non-lock ne tuent plus la coroutine), `CancelledError` propagé pour le shutdown (`app/workers/download_worker.py:116-154`) ; `reap_orphans` au boot (`:68-83`) ; back-off transitoire hors sémaphore (`:432-538`) ; `except Exception` défensif autour du transfert pour ne jamais laisser un job `running` fantôme (`:522-526`).
- **Tâches de fond sans fuite** : jeu de réfs fortes + `add_done_callback` qui **logge toute exception** (jamais avalée silencieusement) (`app/utils/tasks.py:7-30`) ; `cancel_all_background_tasks` borné 10 s au shutdown (`:43-62`).
- **Shutdown lifespan complet** : cancel des tâches → release flock + unlink → `scheduler.shutdown(wait=False)` → fermeture `xtream_service`/`tmdb_service`/`health_check_worker`/**`dav_relay.close_client()`** (no-op si jamais servi, double-close safe) → arrêt du ThreadPool images (`app/main.py:493-520`, `app/dav/relay.py:111-125`).
- **CR-F04 fermé** : `_PIPELINE_LOCK` partagé entre le run initial de boot et le job d'intervalle, skip loggé si occupé (`app/main.py:115,297-305,469-475`).
- **Validation cron ⇆ pipeline sérialisées** : `_VALIDATION_LOCK` module-level, le cron skip proprement (`app/workers/health_check_worker.py:285-311`).
- **Sync par compte auto-contenu** : lock asyncio par compte + skip non-bloquant, `try/except` global par compte avec job record `failed` + métrique `sync_duration_seconds` en `finally` (`app/workers/sync_worker.py:1090-1591`) ; les fetches Xtream par section dégradent en liste vide sans avorter le sync (`:1154-1164`).
- **Backfill OMDb** : garde mono-run process-local posée synchroniquement (pas de fenêtre d'await), store de jobs borné `JOBS_CAP=100` avec éviction FIFO, sessions courtes fetch/write séparées (`app/workers/enrichment_backfill_worker.py:54-96,140-170`).
- **`plex_sync_service`** : claim `idle→running` conditionnel + `reap_sync_status` au boot master, tout writer en `run_with_retry` session fraîche (`app/services/plex_sync_service.py:126-161`).
- **Rejouabilité migrations** : prouvée sur DB fraîche (0 warning) ; 008 sur connexion dédiée sqlite-vec (`app/db/migrations.py:34-35`) ; probe `_column_exists` (`:55-70`).
- **`_cleanup_stale_epg`** : le DELETE entier (open→delete→commit) est sous `run_with_retry` avec session fraîche par tentative — leçon du crash nocturne intégrée (`app/main.py:215-240`).
- **Heal `display_rating`** et gauges post-run enveloppés défensivement (`app/workers/enrichment_worker.py:708-732`).

## Findings

### AUDIT-P1-001 — `commit_with_retry` : le retry same-session est structurellement inopérant (`PendingRollbackError`) — **S2**
- **Preuve** : `run_with_retry` n'attrape que `OperationalError` dont le message contient « database is locked » (`app/utils/db_retry.py:24-26,43-45`). `commit_with_retry` re-invoque `db.commit` **sur la même session** (`db_retry.py:56-62`). Or en SQLAlchemy 2.x, un `commit()` qui lève laisse la transaction invalidée : le 2ᵉ `db.commit()` lève `PendingRollbackError` (pas une `OperationalError` « locked ») → re-raise immédiat par la branche `raise` de `:44-45`. **Le retry n'exécute donc jamais une 2ᵉ tentative utile** : tous les ~30 call-sites `commit_with_retry` (sync/enrichment/health_check workers, `tv_auth`, `accounts`, `categories`, `live`, `ai`, `cli`) n'ont en pratique qu'une seule vraie tentative, celle que `busy_timeout=60s` protège déjà.
- **Confirmation du défaut documenté** : CLAUDE.md l'admet (« résilience partielle ») ; le correctif proposé (généraliser `run_with_retry` + session fraîche, comme `download_worker`/`plex_sync_service` le font déjà) est le bon.
- **Impact concret** : sous vraie contention >60 s (validation de flux + sync + génération simultanés — scénario réel cité en commentaire `database.py:13-19`), le writer request-path ou worker crashe au 1ᵉʳ échec avec une trace `PendingRollbackError` **différente de l'erreur d'origine** (diagnostic brouillé). Probabilité basse (busy_timeout 60 s + `_PIPELINE_LOCK` + `_VALIDATION_LOCK` réduisent la fenêtre), mais la couche de résilience revendiquée par piège 8 est en partie du théâtre.
- **Effort** : faible — dans `commit_with_retry`, attraper aussi `PendingRollbackError`, faire `await db.rollback()` avant chaque retry (sémantique : le retry rejoue le flush des objets encore attachés — à valider), OU (mieux, conforme au plan maison) migrer les writers vers `run_with_retry` à session fraîche. Croise **CR-C04**.

### AUDIT-P1-002 — Les migrations avalent les échecs DDL réels en WARNING et continuent la chaîne — **S3**
- **Preuve** : chaque migration enveloppe son DDL dans `try/except Exception: logger.warning("… may already exist: %s")` (ex. `app/db/migrations.py:78-104,115-122,133-140` ; motif répété sur la chaîne). Depuis le fix CR-C05 (`_column_exists`), le cas légitime « déjà présent » ne passe plus par l'except — **tout ce qui atterrit encore dans ces except est un échec réel** (disque plein, DB corrompue, verrou, SQL invalide) reclassé en warning, et `run_migrations` termine par « All migrations completed successfully » (`:52`).
- **Impact** : divergence de schéma silencieuse ; les workers échoueront plus tard avec des erreurs « no such column » éloignées de la cause. Le filet est partiellement compensé par `create_all` qui crée les tables ORM, mais pas pour les migrations data (016 chiffrement, 017 snapshot).
- **Effort** : moyen (distinguer « duplicate column/table exists » → debug, tout le reste → raise ; la proba d'un vrai échec est faible mais le masquage est total).

### AUDIT-P1-003 — Élection master : `except OSError` confond « lock tenu » et « lock file inaccessible » → cluster entièrement esclave, silencieusement — **S3**
- **Preuve** : `open(lock_file, "w")` + `flock(...LOCK_NB)` sous un seul `except OSError` → `is_master = False` (`app/main.py:274-285`). Un `PermissionError`/`EROFS` sur `DATA_DIR` (montage read-only, mauvais droits Docker) est un `OSError` : **tous** les process se déclarent Slave, aucun scheduler/sync/enrichment/génération/download-drain ne tourne, et le seul signal est la ligne INFO « Slave — Passive mode » (`main.py:489`).
- **Impact** : panne totale du pipeline sans aucune erreur loggée — détectable seulement en remarquant l'absence de logs de sync. (Le smoke Windows tourne précisément dans ce mode, ce qui prouve la discrétion du symptôme.)
- **Effort** : faible — distinguer `BlockingIOError`/`EAGAIN` (lock tenu, cas normal) des autres `OSError` (logger en ERROR), et/ou exposer `is_master` dans `/api/health`.

### AUDIT-P1-004 — Pool DB worker dédié adopté par UN seul worker sur six — **S3**
- **Preuve** : `worker_engine`/`worker_session_factory` créés précisément pour isoler les workers longs du pool API (root-cause fix d'un incident 500 `QueuePool limit reached`, commentaire `app/db/database.py:27-43`) — mais seul `health_check_worker` l'importe (`health_check_worker.py:9,401,547`). `sync_worker` (`:1107,1597`), `enrichment_worker` (`:628,715,727`), `enrichment_backfill_worker`, `unified_group_service`, `download_worker` (via le `session_factory` passé par `main.py:431,462` = `async_session_factory`) restent sur le **pool API**.
- **Impact** : un sync multi-comptes long + enrichment tiennent des connexions du pool API (20+30 : marge confortable, l'incident d'origine venait d'un pool 5+10) — risque résiduel faible mais l'intention d'archi n'est appliquée qu'à 1/6 des consommateurs, et l'incident peut revenir si les pools sont retaillés à la baisse.
- **Effort** : faible (basculer les imports/paramètres vers `worker_session_factory`).

### AUDIT-P1-005 — `enrichment_worker.run()` : une seule session ouverte sur toute la durée du run, sans try par batch — **S3**
- **Preuve** : `async with async_session_factory() as db:` englobe les Phases 1 ET 2 complètes (`app/workers/enrichment_worker.py:628-701`) ; aucune capture d'exception par batch — une exception d'`_apply_enrichment_results`/commit avorte tout le restant du run (l'except du pipeline `main.py:316-317` saute alors validation + génération + rebuild du tick).
- **Impact** : un seul item pathologique (erreur d'écriture non-lock) coûte le tick de pipeline entier ; la session multi-heure sur le pool API aggrave AUDIT-P1-004. Les commits par batch limitent la perte de données (déjà commité = conservé).
- **Effort** : moyen (try par batch + session par batch — même patron que `enrichment_backfill_worker` qui fait déjà les deux correctement).

### AUDIT-P1-006 — `get_db` commit implicite sur CHAQUE requête, sans retry — **dette**
- **Preuve** : la dépendance commit après `yield` pour toute requête, y compris les GET purs, en `db.commit()` nu (`app/db/database.py:99-107`).
- **Impact** : (a) un handler d'écriture qui s'appuierait sur ce commit implicite (au lieu d'un `commit_with_retry` explicite) 500 sur lock — résiduel CR-C04 ; (b) coût marginal du commit no-op sur les lectures. Les writers request-path actuels commitent explicitement avant (grep §00-cartography), donc l'exposition réelle est marginale.
- **Effort** : faible (commit conditionnel `if session.dirty/new/deleted`, ou retry).

### AUDIT-P1-007 — `api_key_service.resolve` : panne DB ⇒ 401 silencieux pour toutes les clés par-utilisateur — **S3**
- **Preuve** : tout `Exception` (lock, DB corrompue) → `logger.warning` + `return None` (`app/services/api_key_service.py:128-130`) → `verify_backend_secret` répond **401 « Invalid API key »**.
- **Impact** : fail-closed (bon pour la sécu) mais mode de panne trompeur : pendant un incident DB, les clients per-user voient une erreur d'authentification (ils vont « corriger » leur clé) au lieu d'un 503 ; le secret maître, comparé en mémoire, continue de marcher → diagnostic asymétrique. Le bump `last_used`, lui, est correctement best-effort (`:115-126`).
- **Effort** : faible (distinguer erreur infra → 503 de clé inconnue → 401), à arbitrer contre la simplicité fail-closed.

### AUDIT-P1-008 — États de jobs uniquement en mémoire process (sync, embedding, backfill) — **dette** (rappel)
- **Preuve** : `_sync_jobs` (`sync_worker.py:1609-1617`), `_jobs` backfill (`enrichment_backfill_worker.py:62`), jobs embedding (`embedding_worker`, `JOBS_CAP=100`).
- **Impact** : un `GET .../jobs/{id}` après restart ou depuis un worker non-master répond 404/inconnu alors que le job a tourné (documenté « CR-A06 caveat » dans le code). Bornés (pas de fuite mémoire). Acté by-design au MVP ; consigné pour le scorecard.

### AUDIT-P1-009 — Windows : `import fcntl` dans le lifespan = boot impossible hors POSIX — **dette (actée house-law)**
- **Preuve** : `app/main.py:246` (`import fcntl` en tête de lifespan) ; piège 7 l'acte, la cible est Docker/Linux ; le smoke a nécessité un shim. Aucun garde `sys.platform`. Consigné (pas un finding actionnable tant que la cible reste Linux ; un fallback « slave forcé » de 3 lignes éliminerait le shim de dev).

## Récapitulatif

| ID | Sévérité | Titre | Preuve |
|---|---|---|---|
| AUDIT-P1-001 | **S2** | `commit_with_retry` same-session : retry annulé par `PendingRollbackError` (défaut documenté, confirmé) | `app/utils/db_retry.py:43-45,56-62` |
| AUDIT-P1-002 | S3 | Échecs DDL réels avalés en WARNING, chaîne continue | `app/db/migrations.py:78-104` (motif répété) |
| AUDIT-P1-003 | S3 | Élection master : OSError non-lock ⇒ cluster tout-esclave silencieux | `app/main.py:274-285,489` |
| AUDIT-P1-004 | S3 | Pool worker dédié utilisé par 1 worker sur 6 | `app/db/database.py:27-58`, `health_check_worker.py:9` |
| AUDIT-P1-005 | S3 | `enrichment_worker.run()` : session unique multi-heure, pas de try par batch | `app/workers/enrichment_worker.py:628-701` |
| AUDIT-P1-006 | dette | `get_db` commit implicite sans retry (résiduel CR-C04) | `app/db/database.py:99-107` |
| AUDIT-P1-007 | S3 | Panne DB ⇒ 401 (pas 503) pour les clés per-user | `app/services/api_key_service.py:128-130` |
| AUDIT-P1-008 | dette | Job stores en mémoire process (borné) | `sync_worker.py:1609`, `enrichment_backfill_worker.py:62` |
| AUDIT-P1-009 | dette | `fcntl` POSIX-only au boot (acté) | `app/main.py:246` |
