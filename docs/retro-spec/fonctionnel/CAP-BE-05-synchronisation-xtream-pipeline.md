# CAP-BE-05 — Synchronisation Xtream & pipeline planifié

**Reconstituée le** : 2026-09-25 · **HEAD** : `7338a5c` · **Sources** : inventaire, code, tests, app TV (lecture seule, preuves `tv:`). Aucun PRD ne couvre cette capacité (`docs/10-prd-*.md` ne traitent que les téléchargements) : pas de tableau d'écart fiche ↔ PRD ; les écarts sont face à `CLAUDE.md` (§2, §9) et à `docs/architecture/flux-cles.md` (§5.1 et « Pipeline planifié »).
**Périmètre** : `POST /api/sync/xtream`, `POST /api/sync/xtream/all`, `DELETE /api/sync/cancel/{task_name}`, `POST /api/sync/full-pipeline`, `GET /api/sync/status/{job_id}`, `GET /api/sync/jobs` (`app/api/sync.py`) ; worker `app/workers/sync_worker.py` ; client `app/services/xtream_service.py` ; registre `app/services/job_registry.py` ; fraîcheur `app/utils/job_health.py` ; élection master, planificateur et pipeline dans `app/main.py`. `POST /api/sync/enrichment` et `POST /api/sync/validate-streams` partagent le registre mais relèvent de CAP-BE-06 et CAP-BE-07. Renvoi : `docs/retro-spec/_genere/inventaire.md`.

## Résumé

Le backend recopie en base locale le catalogue de chaque compte Xtream actif : films (VOD), séries, épisodes et chaînes live, filtrés par les catégories choisies. La synchronisation est incrémentale (empreinte des données fournisseur), protège les identifiants corrigés à la main (`match_locked`) et ne supprime jamais rien sur la foi d'une liste vide. Un processus unique, élu « master », enchaîne toutes les `SYNC_INTERVAL_HOURS` heures et une fois au démarrage : synchro → enrichissement → validation des flux → génération de bibliothèque → reconstruction du snapshot unifié → maintenance SQLite. L'opérateur (ou l'app TV) peut déclencher une synchro ou le pipeline complet par API et suivre le job dans un registre en mémoire. Personas : opérateur, planificateur (système), spectateur TV (déclenchement depuis les réglages). **L'EPG n'est pas synchronisé ici** : seule la purge des programmes échus est planifiée.

## User stories observées

- **US-BE-05.1** (P1) — En tant que planificateur, je rafraîchis automatiquement le catalogue de tous les comptes actifs à intervalle régulier, sur un seul processus.
  - Parcours : élection `fcntl.flock` → `scheduled_sync_enrich_generate` (`app/main.py:301-365`) · Maestro : sans objet
- **US-BE-05.2** (P2) — En tant que planificateur, je lance un premier passage au démarrage du master sans attendre le premier intervalle.
  - Parcours : `initial_sync_then_enrich` (`app/main.py:523-542`) · Maestro : sans objet
- **US-BE-05.3** (P1) — En tant qu'opérateur, je déclenche la synchro d'un compte ou de tous les comptes et je récupère un identifiant de job.
  - Parcours : `POST /api/sync/xtream` ou `/xtream/all` (`app/api/sync.py:29-68`) · aucun écran backend · Maestro : aucun
- **US-BE-05.4** (P1) — En tant qu'opérateur, je déclenche le pipeline complet à la demande et je vois dans quelle phase il se trouve.
  - Parcours : `POST /api/sync/full-pipeline` → `GET /api/sync/status/{job_id}` (`app/api/sync.py:129-206`) · Maestro : aucun
- **US-BE-05.5** (P2) — En tant qu'opérateur, j'annule une synchro en cours.
  - Parcours : `DELETE /api/sync/cancel/{task_name}` (`app/api/sync.py:71-80`) · Maestro : aucun
- **US-BE-05.6** (P2) — En tant que spectateur TV, je déclenche depuis les réglages une resynchronisation Xtream côté backend.
  - Parcours : réglages → `SettingsAction.TriggerBackendXtreamSync` (`tv:app/src/main/java/com/chakir/plexhubtv/feature/settings/SettingsViewModel.kt:1193-1218`) → `POST /api/sync/xtream` ou `/xtream/all` · Maestro : aucun
- **US-BE-05.7** (P1) — En tant qu'opérateur, je garde mes corrections d'identité et l'enrichissement déjà acquis quand le fournisseur modifie ou réordonne son catalogue.
  - Parcours : `upsert_media_batch` (`app/workers/sync_worker.py:596-792`) · Maestro : sans objet

## Exigences fonctionnelles

### Déclenchement par API (`app/api/sync.py`)

- **FR-BE-189** — `POST /api/sync/xtream` crée un `jobId` de la forme `sync_<accountId>_<12 hex>`, l'enregistre **avant** de lancer la tâche de fond (pas de course avec un `GET /status` immédiat) et répond 202 `{jobId}`. — *implémenté* — preuve `app/api/sync.py:20-45` · test `tests/test_sync_jobs.py:68-81`
- **FR-BE-190** — `POST /api/sync/xtream/all` synchronise tous les comptes actifs sous un `jobId` `sync_all_…` qui passe `completed` ou `failed` à la fin ; il n'agrège pas l'avancement des comptes (chaque `sync_account` s'auto-attribue son propre job, invisible depuis ce `jobId`). — *implémenté* — preuve `app/api/sync.py:48-68`, `app/workers/sync_worker.py:1190-1194`, `app/workers/sync_worker.py:1771-1783` · test `tests/test_sync_jobs.py:83-94`
- **FR-BE-191** — `DELETE /api/sync/cancel/{task_name}` annule la tâche de fond de ce nom (`sync_<accountId>`, `sync_all`, `full_pipeline`…) et répond 404 si aucune tâche de ce nom ne tourne. — *implémenté* — preuve `app/api/sync.py:71-80`, `app/utils/tasks.py:33-40` · test `tests/test_sync_responses.py:56-67` (chemin succès seulement)
- **FR-BE-192** — Le champ `force` du corps de `POST /api/sync/xtream` modifie le comportement de la synchro. — *mort* (accepté par le schéma, jamais lu par le routeur ni transmis au worker) — preuve `app/models/schemas.py:342-346`, `app/api/sync.py:33-45`, `app/workers/sync_worker.py:1160` · test : aucun
- **FR-BE-193** — `POST /api/sync/full-pipeline` enchaîne synchro de tous les comptes → enrichissement → validation des flux → génération de bibliothèque, en faisant avancer `phase` (`sync` → `enrichment` → `validation` → `generation` → `snapshot`) ; toute exception passe le job `failed` avec son message. — *implémenté* — preuve `app/api/sync.py:129-178` · test `tests/test_sync_jobs.py:124-151`
- **FR-BE-194** — Le pipeline déclenché par API fait le même travail que le pipeline planifié. — *partiel* (manque : la phase finale s'appelle `snapshot` mais n'appelle ni la reconstruction du snapshot `media_group` ni la maintenance `ANALYZE`/`optimize`, que les deux pipelines de `app/main.py` exécutent via `_rebuild_unified_groups`) — preuve `app/api/sync.py:161-171`, `app/main.py:156-179`, `app/main.py:346-349` · test : aucun
- **FR-BE-195** — `GET /api/sync/status/{job_id}` renvoie `status`, `progress`, `phase`, `startedAt`, `finishedAt`, `error` ; un id inconnu, évincé ou perdu au redémarrage donne 404, jamais 200 « unknown ». — *implémenté* — preuve `app/api/sync.py:181-206` · test `tests/test_sync_jobs.py:158-164`
- **FR-BE-196** — `GET /api/sync/jobs` liste les jobs des cinq déclencheurs du routeur, les plus récents d'abord. — *implémenté* — preuve `app/api/sync.py:209-213`, `app/services/job_registry.py:137-142` · test `tests/test_sync_jobs.py:171-183`
- **FR-BE-197** — La liste des jobs expose les mêmes champs que le statut unitaire. — *partiel* (manque : `SyncJobResponse` ne porte que `jobId`, `status`, `progress` ; `phase`, `startedAt`, `finishedAt`, `error` n'y figurent pas) — preuve `app/models/schemas.py:393-406`, `app/models/schemas.py:349` · test `tests/test_sync_responses.py:23-53`
- **FR-BE-198** — Les six routes sont montées sous `/api` avec la garde `verify_backend_secret` (clé `X-API-Key`) et **sans** garde « master seul » : un processus esclave accepte et exécute une synchro ou un pipeline demandé par API. — *implémenté* (comportement constaté) — preuve `app/main.py:643` · test : aucun

### Registre des jobs (`app/services/job_registry.py`)

- **FR-BE-199** — Le registre est en mémoire, propre au processus, partagé par les déclencheurs de `app/api/sync.py` et par le worker, et plafonné à 100 entrées (éviction des plus anciennes). — *implémenté* — preuve `app/services/job_registry.py:43-58` · test `tests/test_sync_jobs.py:190-206`
- **FR-BE-200** — `create_job` crée l'entrée à `processing` avec `started_at` ; `update_job` fusionne les champs et fige `finished_at` une seule fois au passage `completed`/`failed`. — *implémenté* — preuve `app/services/job_registry.py:73-127` · test `tests/test_sync_jobs.py:213-227`
- **FR-BE-201** — `update_job` sur un id jamais créé crée une entrée par défaut (synchro lancée par le planificateur ou `run_all_accounts`) ; `get_job` renvoie `None` sans lever pour un id inconnu. — *implémenté* — preuve `app/services/job_registry.py:100-115`, `app/services/job_registry.py:130-142` · test `tests/test_sync_jobs.py:229-238`
- **FR-BE-202** — Une tâche annulée voit son job passer à un état terminal. — *partiel* (manque : l'annulation lève `CancelledError`, que ni `sync_account` ni les fermetures du routeur n'interceptent — elles ne capturent que `Exception` — ; le job reste `processing` jusqu'à éviction, seule la métrique de durée est enregistrée avec le résultat `error`) — preuve `app/utils/tasks.py:33-40`, `app/workers/sync_worker.py:1757-1766`, `app/api/sync.py:59-65`, `app/api/sync.py:148-175` · test : aucun
- **FR-BE-203** — Le nom de tâche servant à l'annulation n'est pas unique par déclenchement : deux `POST /api/sync/xtream` successifs sur le même compte créent deux tâches `sync_<accountId>`, et l'annulation vise la première trouvée dans un ensemble non ordonné. — *implémenté* (limite constatée) — preuve `app/api/sync.py:42-44`, `app/utils/tasks.py:7`, `app/utils/tasks.py:35-39` · test : aucun

### Client Xtream (`app/services/xtream_service.py`)

- **FR-BE-204** — Le client appelle `player_api.php` avec un client HTTP partagé (timeout 30 s, pool borné) et un User-Agent configurable (`XTREAM_USER_AGENT`). — *implémenté* — preuve `app/services/xtream_service.py:21-32`, `app/services/xtream_service.py:46-47`, `app/config.py:182` · test : aucun
- **FR-BE-205** — Sur délai dépassé, connexion refusée ou protocole rompu, le client retente trois fois (1 s, 2 s, 4 s) puis relève l'exception. — *implémenté* — preuve `app/services/xtream_service.py:11-12`, `app/services/xtream_service.py:61-70`, `app/services/xtream_service.py:88` · test `tests/test_retry_logic.py:14-27` (constantes seulement)
- **FR-BE-206** — Sur 429, 502, 503 ou 504, le client retente en honorant `Retry-After` (secondes, plafonné à 60 s) ; les autres codes HTTP sont relevés immédiatement. — *implémenté* — preuve `app/services/xtream_service.py:71-87` · test : aucun

### Catégories et filtrage

- **FR-BE-207** — Chaque synchro recharge du fournisseur les catégories VOD, séries et live du compte et les enregistre avant tout filtrage. — *implémenté* — preuve `app/workers/sync_worker.py:1097-1157`, `app/workers/sync_worker.py:1231` · test : aucun direct
- **FR-BE-208** — Le mode de filtrage du compte décide des catégories synchronisées : `all` → toutes ; `whitelist` → seulement les autorisées ; `blacklist` → toutes sauf les bloquées ; une catégorie absente de la configuration est exclue en liste blanche et incluse en liste noire. — *implémenté* — preuve `app/workers/sync_worker.py:999-1028` · test : aucun
- **FR-BE-209** — En liste blanche, le worker n'interroge le fournisseur que pour les catégories autorisées, avec une concurrence bornée par `max_connections` du compte. — *implémenté* — preuve `app/workers/sync_worker.py:1031-1094`, `app/workers/sync_worker.py:1246-1254` · test : aucun

### Films (VOD)

- **FR-BE-210** — Une empreinte `dto_hash` sur les champs stables du flux (`name`, `added`, `category_id`, `container_extension` ; icône et note exclues) évite de rappeler `get_vod_info` pour un film inchangé. — *implémenté* — preuve `app/workers/sync_worker.py:540-556`, `app/workers/sync_worker.py:1259-1292` · test `tests/test_sync_worker.py:101-127`
- **FR-BE-211** — Les détails des films nouveaux ou modifiés sont récupérés en parallèle (25 requêtes au plus) et écrits par lots de 100 sous savepoint, un lot en échec étant annulé isolément. — *implémenté* — preuve `app/workers/sync_worker.py:1295-1342` · test `tests/test_sync_worker_orchestration.py:102-146`
- **FR-BE-212** — Les films disparus du fournisseur sont supprimés (globalement ou par catégorie synchronisée), mais seulement si la liste reçue à ce passage est non vide. — *implémenté* — preuve `app/workers/sync_worker.py:1345-1354` · test `tests/test_sync_worker_orchestration.py:192-226` (le cas « liste vide » n'est pas testé)
- **FR-BE-213** — Les films nouveaux ou modifiés sans `tmdb_id` ni `imdb_id` entrent dans la file d'enrichissement. — *implémenté* — preuve `app/workers/sync_worker.py:801-837`, `app/workers/sync_worker.py:1356` · test : aucun direct

### Séries et épisodes

- **FR-BE-214** — Une empreinte `dto_hash` de la fiche série (nom, affiche, résumé, genre, note, catégorie, fond, durée d'épisode, `last_modified`) évite de remapper une série inchangée. — *implémenté* — preuve `app/workers/sync_worker.py:558-564`, `app/workers/sync_worker.py:1379-1426` · test : aucun direct
- **FR-BE-215** — Les épisodes de chaque série listée sont **toujours** relus (`get_series_info`), même si l'empreinte de la série n'a pas changé. — *implémenté* — preuve `app/workers/sync_worker.py:1461-1472` · test `tests/test_sync_worker.py:588-624`
- **FR-BE-216** — Les épisodes disparus d'une série sont supprimés, dans le périmètre strict `(server_id, série)`. — *implémenté* — preuve `app/workers/sync_worker.py:879-933` · test `tests/test_sync_worker.py:465-499`, `tests/test_sync_worker.py:626-681`
- **FR-BE-217** — Un échec de `get_series_info` (exception réseau, ou réponse 200 sans épisodes / sans clé `episodes`) saute le nettoyage des épisodes de cette série pour ce passage. — *implémenté* — preuve `app/workers/sync_worker.py:1474-1541`, `app/workers/sync_worker.py:1579-1598` · test `tests/test_sync_worker.py:683-779`
- **FR-BE-218** — La bande-annonce YouTube de la série (URL ou id brut, normalisé) est écrite après lecture de la fiche, sans jamais être écrasée par une valeur vide. — *implémenté* — preuve `app/workers/sync_worker.py:1486-1493`, `app/workers/sync_worker.py:1570-1607` · test : aucun dans le périmètre
- **FR-BE-219** — Les séries disparues sont supprimées, seulement si la liste reçue est non vide. — *implémenté* — preuve `app/workers/sync_worker.py:1446-1453` · test : aucun direct

### Live et EPG

- **FR-BE-220** — Une empreinte `dto_hash` par chaîne live (nom, logo, id EPG, catégorie, archive, adulte, conteneur, `custom_sid`) limite l'écriture aux chaînes modifiées. — *implémenté* — preuve `app/workers/sync_worker.py:414-421`, `app/workers/sync_worker.py:1637-1670` · test : aucun direct
- **FR-BE-221** — Les chaînes disparues sont supprimées (globalement ou dans les catégories autorisées), seulement si la liste reçue est non vide. — *implémenté* — preuve `app/workers/sync_worker.py:1687-1694` · test : aucun direct
- **FR-BE-222** — La synchro récupère le guide des programmes (EPG) du compte. — *absent* (annoncé par `docs/architecture/flux-cles.md` §5.1 « xtream_service (catégories, VOD, séries+épisodes, Live, EPG) » et par CLAUDE.md §2 « VOD+séries+épisodes+Live+EPG » ; `get_short_epg`/`get_xmltv` existent dans `app/services/xtream_service.py` mais ne sont jamais appelés par `app/workers/sync_worker.py` — l'EPG est chargé à la demande par chaîne, CAP-BE-11)
- **FR-BE-223** — Chaque nuit à 3 h, le master supprime les programmes EPG dont la fin est passée (écriture avec nouvelle tentative, suivie par `track_job("epg_cleanup")`). — *implémenté* — preuve `app/main.py:243-269`, `app/main.py:375-383` · test : aucun

### Écriture et robustesse du worker

- **FR-BE-224** — Quand le fournisseur réordonne sa liste, une ligne encore listée est relogée sur un autre créneau `page_offset` plutôt que supprimée ; seule une ligne prouvée absente de la liste courante est évincée. — *implémenté* — preuve `app/workers/sync_worker.py:596-723` · test `tests/test_sync_worker.py:261-462`
- **FR-BE-225** — Pour une ligne `match_locked`, l'upsert conserve `tmdb_id`, `unification_id`, `history_group_key` (y compris une valeur vidée exprès) et les métadonnées corrigées (`resolved_thumb_url`, `resolved_art_url`, `summary`, `genres`, `year`) ; pour une ligne non verrouillée, un id déjà présent n'est pas écrasé et une clé d'unification fondée sur un id est conservée. — *implémenté* — preuve `app/workers/sync_worker.py:744-792` · test `tests/test_manual_scrape_lock.py:163-256`
- **FR-BE-226** — En fin de synchro d'un compte, une session neuve (`write_with_retry`) recalcule la visibilité par catégorie, marque les films adultes, purge la file d'enrichissement orpheline et met à jour `last_synced_at`, puis le job passe `completed` avec `{total, synced}`. — *implémenté* — preuve `app/workers/sync_worker.py:1720-1755` · test `tests/test_sync_worker_orchestration.py:325-350`
- **FR-BE-227** — Une seule synchro par compte à la fois dans un processus : une seconde demande pendant qu'une première tourne échoue aussitôt (`failed`, message « Sync already running… ») sans toucher la base. — *implémenté* — preuve `app/workers/sync_worker.py:60-67`, `app/workers/sync_worker.py:1175-1188` · test `tests/test_sync_worker_orchestration.py:256-300`
- **FR-BE-228** — Un compte introuvable ou inactif fait échouer le job avec un message explicatif. — *partiel* (manque : le job passe `failed` avec `progress = {}` et aucun `error` ; seul le journal serveur dit pourquoi) — preuve `app/workers/sync_worker.py:1200-1212` · test : aucun
- **FR-BE-229** — La synchro ne s'appuie que sur le drapeau local `is_active` ; le statut fournisseur du compte (`status`, date d'expiration) n'est pas consulté avant de synchroniser. — *implémenté* (comportement constaté) — preuve `app/workers/sync_worker.py:1202-1208` · test : aucun
- **FR-BE-230** — Toute exception non gérée d'une synchro est journalisée et passe le job `failed` avec son message ; la durée est toujours publiée dans `sync_duration_seconds{account_id, result}` (sauf refus pour synchro déjà en cours). — *implémenté* — preuve `app/workers/sync_worker.py:1757-1766` · test `tests/test_sync_worker_orchestration.py:228-249`
- **FR-BE-231** — `run_all_accounts` synchronise les comptes actifs **l'un après l'autre**. — *implémenté* — preuve `app/workers/sync_worker.py:1771-1783` · test `tests/test_sync_worker_orchestration.py:302-323`

### Pipeline planifié et élection master (`app/main.py`)

- **FR-BE-232** — Un seul processus devient master via un verrou `fcntl.flock` non bloquant sur `server_start.lock` ; le résultat réel est publié (`set_master`) ; seul le master crée le planificateur et lance le passage initial, les autres restent passifs. — *implémenté* — preuve `app/main.py:301-318`, `app/main.py:324`, `app/main.py:543-544` · test `tests/test_startup_wiring.py:269-319`
- **FR-BE-233** — Le job `sync_enrich_generate` tourne toutes les `SYNC_INTERVAL_HOURS` heures (défaut 6), `max_instances=1`, `coalesce=True`, tolérance de retard 300 s, et enchaîne synchro → enrichissement → validation → génération → snapshot unifié + maintenance SQLite. — *implémenté* — preuve `app/main.py:330-365`, `app/main.py:156-179`, `app/config.py:110` · test `tests/test_startup_wiring.py:269-289`
- **FR-BE-234** — Au démarrage, le master lance en tâche de fond la même séquence sans attendre le premier intervalle. — *implémenté* — preuve `app/main.py:523-542` · test `tests/test_startup_wiring.py:269-306`
- **FR-BE-235** — Le passage planifié et le passage de démarrage s'excluent (`_PIPELINE_LOCK`) : si l'un tourne, l'autre est sauté avec un avertissement ; pendant le passage, le rebuild différé du scraper manuel est suspendu (`set_pipeline_active`). — *implémenté* — preuve `app/main.py:109-137`, `app/main.py:332-338`, `app/main.py:524-529` · test `tests/test_startup_wiring.py:290-304`, `tests/test_manual_scrape_apply.py:868-885`
- **FR-BE-236** — Deux pipelines ne peuvent jamais tourner en même temps. — *partiel* (manque : `POST /api/sync/full-pipeline` et `POST /api/sync/xtream/all` ne prennent pas `_PIPELINE_LOCK`, et ce verrou comme le verrou par compte sont propres au processus — un esclave peut synchroniser en même temps que le master) — preuve `app/api/sync.py:129-178`, `app/main.py:116`, `app/workers/sync_worker.py:60-67` · test : aucun
- **FR-BE-237** — Le pipeline planifié marque son succès (`mark_job_success("pipeline")`, jauge `plexhub_pipeline_last_success_timestamp_seconds`) seulement si aucune étape n'a levé ; les autres crons passent par `track_job`. — *implémenté* — preuve `app/main.py:349`, `app/main.py:539`, `app/utils/job_health.py:56-78` · test `tests/test_metrics_registry.py:120-125` (déclaration de la série seulement)
- **FR-BE-238** — Le passage de démarrage journalise et absorbe l'échec d'une étape comme le passage planifié. — *partiel* (manque : `initial_sync_then_enrich` n'a pas de `try/except` ; une exception interrompt la séquence et n'est journalisée que par le rappel générique des tâches de fond) — preuve `app/main.py:529-539`, `app/main.py:339-351`, `app/utils/tasks.py:22-30` · test : aucun

### App TV cliente

- **FR-BE-239** — L'app TV déclenche la synchro backend d'un compte ou de tous les comptes et récupère le `jobId`, sans jamais envoyer `force`. — *implémenté* — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:65-68`, `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:115-116`, `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:657-680`
- **FR-BE-240** — L'app TV suit l'avancement du job déclenché (`GET /api/sync/status/{jobId}`). — *mort* (méthode Retrofit `getSyncStatus` déclarée, jamais appelée dans le dépôt TV ; l'écran affiche seulement « Sync triggered (N server(s)) ») — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:70-73`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/settings/SettingsViewModel.kt:1193-1218`

## Règles métier

- **RG-BE-05.1** — Jamais de suppression sur une liste vide : films, séries et chaînes ne sont nettoyés que si la liste du fournisseur reçue à ce passage contient au moins un élément ; les épisodes d'une série seulement si sa fiche a été lue avec succès et non vide. — preuve `app/workers/sync_worker.py:1345`, `app/workers/sync_worker.py:1446`, `app/workers/sync_worker.py:1687`, `app/workers/sync_worker.py:1579-1598`
- **RG-BE-05.2** — Une ligne `match_locked` garde ses identifiants et ses métadonnées corrigées quel que soit le changement côté fournisseur (motif `case`, pas `COALESCE`, pour respecter un id vidé exprès). — preuve `app/workers/sync_worker.py:744-792`
- **RG-BE-05.3** — Un seul passage par compte à la fois, et un seul pipeline planifié/démarrage à la fois, **dans un même processus**. — preuve `app/workers/sync_worker.py:60-67`, `app/main.py:116`
- **RG-BE-05.4** — Le filtrage par catégories s'applique séparément aux films, séries et chaînes live d'un compte. — preuve `app/workers/sync_worker.py:999-1028`, `app/workers/sync_worker.py:1234-1241`
- **RG-BE-05.5** — Seul le master planifie ; l'API de synchro reste ouverte sur tout processus. — preuve `app/main.py:324`, `app/main.py:643`
- **RG-BE-05.6** — Les jobs ne survivent pas à un redémarrage : registre en mémoire, 100 entrées au plus. — preuve `app/services/job_registry.py:43-58`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| Compte inconnu ou inactif | Job `failed`, `progress = {}`, aucun `error` | `app/workers/sync_worker.py:1200-1212` |
| Compte expiré chez le fournisseur mais actif en base | Synchro tentée ; les appels échouent, chaque phase retombe sur une liste vide, rien n'est supprimé | `app/workers/sync_worker.py:1202-1208`, `app/workers/sync_worker.py:1255-1257` |
| Fournisseur injoignable ou lent | 3 nouvelles tentatives (1/2/4 s) puis exception ; la phase concernée tombe à vide sans nettoyage | `app/services/xtream_service.py:61-70`, `app/workers/sync_worker.py:1633-1635` |
| 429 / 502 / 503 / 504 | Nouvelle tentative, `Retry-After` honoré (≤ 60 s) | `app/services/xtream_service.py:71-87` |
| Deux synchros du même compte | La seconde échoue aussitôt (`failed`, « Sync already running… ») | `app/workers/sync_worker.py:1175-1188` |
| Annulation d'une synchro | Tâche annulée, mais job bloqué à `processing` jusqu'à éviction | `app/utils/tasks.py:33-40`, `app/workers/sync_worker.py:1757` |
| Annulation d'un nom inconnu | 404 | `app/api/sync.py:77-79` |
| Statut d'un job inconnu, évincé ou d'avant redémarrage | 404 | `app/api/sync.py:196-198` |
| Pipeline planifié pendant le passage de démarrage (ou l'inverse) | Passage sauté, avertissement journalisé | `app/main.py:332-337`, `app/main.py:524-528` |
| `POST /api/sync/full-pipeline` pendant un pipeline planifié | Aucune exclusion : les deux tournent en parallèle | `app/api/sync.py:129-178` |
| Processus esclave | Ni planificateur ni passage initial ; répond quand même à `/api/sync/*` | `app/main.py:543-544`, `app/main.py:643` |
| Fiche série en « succès mou » (200 sans épisodes) | Traitée comme un échec : aucun épisode supprimé | `app/workers/sync_worker.py:1474-1541` |
| Échec d'une étape du passage de démarrage | Séquence interrompue, erreur journalisée par le rappel générique des tâches | `app/main.py:529-539`, `app/utils/tasks.py:26-30` |

## Dépendances

- Autres capacités : `CAP-BE-04` (comptes, catégories, visibilité, `update_media_adult_flags`), `CAP-BE-06` (enrichissement, étape 2 du pipeline), `CAP-BE-07` (validation, étape 3), `CAP-BE-08` (panne fournisseur : le masquage est décidé par la validation, pas par la synchro), `CAP-BE-16` (génération, étape 4), `CAP-BE-09` (snapshot `media_group` lu par le catalogue), `CAP-BE-11` (EPG à la demande), `CAP-BE-22` (verrou `match_locked`), `CAP-BE-01` (métriques et fraîcheur du pipeline).
- Autre dépôt : appels `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:65` (`triggerSync`), `:115` (`triggerSyncAll`) ; `:70` (`getSyncStatus`, jamais appelé). La « synchro » visible côté TV (`syncMedia`) est une autre chose : elle lit le catalogue déjà synchronisé via `/api/media/*` (`tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:152-209`).
- Données : `media`, `live_channels`, `xtream_categories`, `xtream_accounts` (`last_synced_at`), `enrichment_queue`, `epg_entries` (purge), `media_group`/`media_group_member` (via le pipeline) — voir `docs/retro-spec/technique/db/`.

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| `flux-cles.md` §5.1 et CLAUDE.md §2 annoncent une synchro « Live + EPG » ; le worker n'appelle jamais l'EPG (FR-BE-222) | doc ↔ code | corriger la doc (EPG = à la demande, CAP-BE-11) |
| `flux-cles.md` §5.1 affirme que les épisodes ne sont « JAMAIS differential-cleaned » (CR-F01) et que l'éviction `page_offset` peut supprimer une ligne toujours listée (CR-F02) : les deux sont corrigés (FR-BE-216, FR-BE-224) | doc périmée | recaler §5.1 et `docs/context/etat-reel.md` |
| « Pipeline planifié » de `flux-cles.md` : le passage initial et le passage d'intervalle « ne partagent aucune exclusion » — faux depuis `_PIPELINE_LOCK` (FR-BE-235) ; la séquence décrite omet snapshot unifié et maintenance | doc périmée | recaler la section |
| Références de lignes périmées dans `flux-cles.md` : sync_worker.py:1366 (réel 1771), :920 (réel 1160), :409/435/474/658 (réels 440/466/505/839), :557-576 (réel 596-723), main.py:248-273 (réel 330-365), main.py:311-321 (réel 523-542), main.py:274-307 (réel 366-467) ; CLAUDE.md §9 piège 7 main.py:226-227 (réel 301-313) | doc périmée | `/sync-context` |
| Le pipeline par API ne reconstruit ni le snapshot ni les statistiques SQLite alors que sa phase finale s'appelle `snapshot` (FR-BE-194) | spec ↔ code | aligner sur `_rebuild_unified_groups`, à trancher par Chakir |
| Aucune exclusion entre pipeline par API et pipeline planifié, ni entre processus (FR-BE-236) : double budget TMDB, écritures concurrentes sur l'arbre généré | risque fonctionnel | prendre `_pipeline_guard` dans le routeur ; garde master pour `/api/sync/*`, à trancher par Chakir |
| Une annulation laisse le job `processing` (FR-BE-202) ; noms de tâche ambigus (FR-BE-203) | défaut | intercepter `CancelledError`, nommer la tâche par `jobId` |
| `force` accepté mais ignoré (FR-BE-192) ; `/jobs` plus pauvre que `/status` (FR-BE-197) ; échec « compte inactif » sans message (FR-BE-228) | contrat | retirer ou implémenter `force` ; aligner `SyncJobResponse` |
| Suivi de job déclaré mais jamais appelé côté TV (FR-BE-240) | code mort (TV) | brancher un suivi ou retirer `getSyncStatus` |
| Pas de test : purge EPG, filtrage par catégories, annulation, exclusion API ↔ planifié, cas « liste vide », retry `Retry-After` | test manquant | tickets de test |

## Questions ouvertes

- Après `DELETE /api/sync/cancel/sync_<id>`, le job reste-t-il réellement `processing` et le verrou du compte est-il bien libéré ? — **à vérifier par test**.
- Avec deux déclenchements rapprochés du même compte, quelle tâche `cancel_task_by_name` annule-t-elle ? — **à vérifier par test**.
- `POST /api/sync/full-pipeline` lancé pendant le pipeline planifié : doublement des appels TMDB et conflit sur `.plex_mapping.json` observables ? — **à vérifier par test**.
- Une synchro reçue par un esclave (plusieurs workers uvicorn) entre-t-elle en conflit d'écriture avec celle du master ? — **à vérifier par test**.
- Un compte expiré chez le fournisseur : chaque passage coûte-t-il les 4 tentatives × toutes les catégories avant d'abandonner ? — **à vérifier par test**.
- `force` était-il destiné à ignorer les empreintes `dto_hash` ? Le code ne le dit pas — **à vérifier par test** (ou auprès de Chakir).
