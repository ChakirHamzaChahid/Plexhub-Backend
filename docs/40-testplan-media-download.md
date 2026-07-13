# Plan de test — Téléchargement physique de médias (« Télécharger »)

> Ticket : **PH-DL-07** (phase **plan** uniquement — le code n'existe pas encore : PH-DL-01/03/04/05/06 sont `todo`).
> Sources : `docs/10-prd-media-download.md` (PRD, Given/When/Then §5), `docs/20-impl-media-download.md` (spec, signatures
> figées §5/§6/§7), `docs/31-board.md` (tickets/DAG), ADR `docs/architecture/adr/0002-media-download-writes-to-disk.md`.
> Ce document est **le plan**. Les fichiers `tests/test_download_*.py` seront écrits/exécutés lors du rappel qa-engineer
> post-code (après merge PH-DL-03/04/05 au minimum), en reprenant les `Test ID` ci-dessous 1:1. Aucun test exécutable
> n'est livré dans cette passe.

---

## 1. Périmètre

**Dans cette passe (plan écrit maintenant)** : tous les critères d'acceptation P0 du PRD §5 (US-001.1 → US-008.1,
capacités F-001..F-008) + F-101 (API JSON lecture, P1 mais dans le sprint courant per board). Le confinement de chemin
(F-007) est **bloquant** — c'est l'invariant sécurité qui donne son sens à toute la feature (analogue direct à CR-S01,
déjà connu comme dette résolue côté `/api/plex/generate`, à ne pas reproduire ici).

**Planifié mais stub (exécution différée)** : F-102 (préflight disque), F-103 (métriques Prometheus), F-104
(clear-finished), F-201 (mutation JSON P2), F-202 (préfixe `[XXX]`), F-203 (NFO/poster). Le board les place en
« stretch » (item 10) ou hors du sprint P0 ; la spec ne fige aucune signature pour F-102/F-103 (`# TBD` implicite) →
cas de test non détaillés avant qu'un ticket ne fige leur contrat. F-104 a une signature figée (`clear_finished`,
spec §5.5) donc un cas est déjà écrit (DL-100) même si son exécution est différée.

**Hors périmètre (non-goals PRD §6, non testés)** : transcodage, lecture/streaming depuis `DOWNLOAD_DIR`, téléchargement
de sous-titres, torrent/P2P, purge/rétention automatique, exposition app Android, planification/cron, sélection
épisode-par-épisode fine.

---

## 2. Environnements

- **Runtime** : Python 3.13 (CI) ; le worker (`asyncio`, `httpx.AsyncClient`, `aiosqlite`) n'introduit aucune nouvelle
  dépendance pip (spec §0/ADR) — pas de contrainte d'environnement supplémentaire.
- **DB** : SQLite WAL éphémère.
  - Cas unitaires/service/route : `tests/conftest.py` (`db_engine`/`db_session`/`db_factory`, `:memory:`).
  - Cas « lock réel / `run_with_retry` sous contention » (§4, DL-140) : **DB fichier** `tmp_path`-backed avec un vrai
    writer concurrent (`sqlite3` sync + `BEGIN IMMEDIATE`), pattern déjà établi par `tests/test_db_retry_real_lock.py`
    — `:memory:` ne matérialise pas réellement le WAL/`busy_timeout` (CR-T08, documenté dans ce fichier existant).
- **`DOWNLOAD_DIR`** : toujours un `tmp_path` monkeypatché sur `settings.DOWNLOAD_DIR` — **jamais** le filesystem réel.
  Pour les tests de non-régression `.strm`, `PLEX_LIBRARY_DIR` est monkeypatché sur un **second** `tmp_path` distinct
  (les deux arbres doivent rester étanches l'un de l'autre).
- **HTTP externe** : **`respx`** exclusivement (`xtream_mock`/nouveau fixture dédié, §5) — jamais d'appel réseau réel.
  Premier usage **streaming + `Range`-aware** de `respx` dans ce repo (les mocks existants sont surtout JSON statique) :
  signalé comme risque de testabilité (§6.4).
- **Auth** : `verify_admin_basic_auth` (HTMX `/admin/downloads`, pattern `tests/test_admin.py`) et `verify_master_key`
  (JSON `/api/admin/downloads`, secret maître **seul** — pas une clé par-utilisateur, cf. `deps.py:88-98`).
- **Worker master-only** : pas de vrai `fcntl` sous Windows dev — réutiliser la technique déjà éprouvée par
  `tests/test_startup_wiring.py` (faux module `fcntl` injecté dans `sys.modules`, faux `AsyncIOScheduler`,
  `create_background_task` espionné) plutôt que de booter un vrai `uvicorn`.
- **Conteneur IA / cold-start ~30 s** : **non applicable** à cette feature (aucun appel embeddings/LLM sur le chemin
  téléchargement) — mentionné pour mémoire, explicitement écarté ici, contrairement aux features `/api/ai`.
- **Boot / santé** : `uvicorn app.main:app` (smoke opérationnel, hors pytest) + `GET /api/health` **200** via `api_client`
  (chaque test qui construit `app.main.app` via la fixture existante l'exerce implicitement) ; DL-084 le rend explicite.

---

## 3. Cas de test (mappés PRD §5, un tableau par user story)

Convention : **Type** = `unit` (service/worker/schémas, sans FastAPI) ou `intégration` (route HTTP via `api_client`,
migration sur engine réel). `Story` référence l'US du PRD. Tous les cas asynchrones suivent `pytest-asyncio` mode auto
(`async def test_*`, pas de décorateur).

### F-001 — Onglet & liste unifiée (US-001.1, US-001.2)

| Test ID | Story | Préconditions / Fixtures | Étapes | Résultat attendu | Type |
|---|---|---|---|---|---|
| DL-001 | US-001.1 | `_admin_creds` (Basic Auth valide, pattern `test_admin.py`) | `GET /admin/downloads` | 200 HTML ; nav contient « Télécharger » marqué actif ; liens « Catalogue »/« Importer NFO »/« Clés API » toujours présents | intégration |
| DL-002 | US-001.1 | aucune credential | `GET /admin/downloads` sans Basic Auth | 401, header `WWW-Authenticate: Basic`, aucun fragment de données dans le corps | intégration |
| DL-003 | US-001.2 | DB seedée avec ≥2 titres (film+série) via `media_service`-compatible rows | `GET /admin/downloads/list?type=movie&search=terminator&page=1` | 200 fragment ; cartes = sortie de `media_service.get_unified_list(type=movie, search=terminator)` (parité comparée directement) ; `versionCount` visible | intégration |
| DL-004 | US-001.2 | idem DL-003 | `GET .../list?type=show` | seules des séries dans le fragment | intégration |
| DL-005 | US-001.2 | DB vide ou `search` sans match | `GET .../list?search=zzzznomatch` | 200 fragment « aucun titre », jamais 500 | intégration |
| DL-006 | US-001.2 | ≥3 titres seedés, `page_size` réduit | `GET .../list?page=2&page_size=1` | tranche correcte, pas de doublon/`IndexError` | intégration |
| DL-007 | (non-rég., édition `base.html`) | `_admin_creds` | `GET /admin/movies`, `GET /admin/import-nfo` (ou équivalent), `GET /admin/keys` | chacun reste 200, marqueurs de page inchangés — l'ajout du lien nav ne casse pas les autres onglets | intégration |

### F-002 — Versions & sélection (US-002.1, US-002.2)

| Test ID | Story | Préconditions / Fixtures | Étapes | Résultat attendu | Type |
|---|---|---|---|---|---|
| DL-010 | US-002.1 | film seedé avec `unification_id` connu, ≥1 version (`server_id`/`rating_key`) | `GET /admin/downloads/movie/{unificationId}/versions` | 200 fragment listant `label`/`serverId`/`ratingKey`/`isBroken` + 1 bouton « Télécharger » par version | intégration |
| DL-011 | US-002.1 | aucun titre avec cet id | `GET .../movie/does-not-exist/versions` | 404 | intégration |
| DL-012 | US-002.2 | série seedée avec épisodes, ≥1 version | `GET /admin/downloads/show/{unificationId}/versions` | 200 fragment, chaque version expose une option `scope=series_all` (« série complète ») | intégration |
| DL-013 | US-002.2 | série seedée, version choisie SANS épisode dans `media` | `GET .../show/{unificationId}/versions` puis l'enqueue associé (cf. DL-024) | option visible malgré tout ; l'enqueue (pas le fragment versions) produit 0 job + message, jamais 500 | intégration |
| DL-014 | Q5 (PRD §10) | version `isBroken=true` | `GET .../versions` | version affichée avec un marqueur d'avertissement mais bouton « Télécharger » **toujours cliquable** (autorisé mais averti) | intégration |

### F-003 — File & téléchargement physique (US-003.1, US-003.2, US-003.3)

| Test ID | Story | Préconditions / Fixtures | Étapes | Résultat attendu | Type |
|---|---|---|---|---|---|
| DL-020 | US-003.1 | `DOWNLOAD_DIR` défini, compte Xtream actif, ligne `Media` film seedée | `enqueue_selection(db, media_type='movie', unification_id, server_id, rating_key='vod_1.mkv', scope='movie')` | 1 `DownloadJob(state='queued', batch_id=None)` persisté ; `dest_path` **relatif** (`Movies/…`, jamais de préfixe `DOWNLOAD_DIR` ni de `/` absolu) | unit |
| DL-021 | US-003.1 (dédup) | job non-terminal déjà existant pour `(server_id, rating_key)` | 2ᵉ `enqueue_selection` identique | renvoie le **même** job (pas de doublon en DB) | unit |
| DL-022 | spec §3.2 (pas de contrainte unique) | job `completed` existant pour le même `(server_id, rating_key)` | `enqueue_selection` identique | un **nouveau** job `queued` est créé (le film peut être re-téléchargé) | unit |
| DL-023 | US-003.2 | série seedée, N épisodes sur ≥2 saisons, version choisie | `enqueue_selection(scope='series_all')` | N `DownloadJob` créés (1/épisode), 1 `DownloadBatch(total_jobs=N)`, `season`/`episode` alignés sur les épisodes source | unit |
| DL-024 | US-002.2 (0 épisode) | série seedée, version choisie sans épisode | `enqueue_selection(scope='series_all')` | `EnqueueResult(jobs=[], error='aucun épisode disponible')`, **0** ligne `download_job`/`download_batch` créée, jamais d'exception | unit |
| DL-025 | US-003.1 | idem DL-020, via HTTP | `POST /admin/downloads` (Form `type=movie,...,scope=movie`) | 200 fragment file d'attente avec le nouveau job `queued` ; 1 ligne `download_job` en DB | intégration |
| DL-026 | US-003.2 | idem DL-023, via HTTP | `POST /admin/downloads` (Form `scope=series_all`) | 200 fragment avec N jobs `queued` regroupés sous le même `batchId` | intégration |
| DL-027 | US-003.1 (bout-en-bout) | job `queued` réel + `xtream_stream_mock` (respx, corps vidéo factice avec `Content-Length`) + `PLEX_LIBRARY_DIR` seedé séparément avec un `.strm` témoin | drain worker (`_run_job` ou `run_drain_loop` une itération) | fichier final présent sous `DOWNLOAD_DIR/Movies/<Titre> (<Année>)/…` ; le `.strm` témoin sous `PLEX_LIBRARY_DIR` est **byte-identique** avant/après (checksum) | intégration |
| DL-028 | US-003.3 | `settings.DOWNLOAD_DIR=""` | `POST /admin/downloads` (Form valide) | 200 fragment d'erreur explicite (« DOWNLOAD_DIR n'est pas défini »), **0** ligne `download_job` créée | intégration |
| DL-029 | spec §5.2 (`compute_dest_path`) | — | `compute_dest_path(media_type='movie', title='Terminator', year=1984, ext='mkv')` / idem `episode` avec `season=1, episode=2` | `"Movies/Terminator (1984)/Terminator (1984).mkv"` ; `"Series/<Titre>/Season 01/<Titre> - S01E02.<ext>"` (zéro-paddé 2 chiffres) | unit |
| DL-029b | spec §5.2 (ext fallback) | `rating_key` sans extension reconnaissable | `compute_dest_path(..., ext=None)` (ou dérivation depuis un `rating_key` sans suffixe) | `ext` retombe sur `'ts'` | unit |

### F-004 — États & progression (US-004.1, US-004.2)

| Test ID | Story | Préconditions / Fixtures | Étapes | Résultat attendu | Type |
|---|---|---|---|---|---|
| DL-040 | US-004.1 | jobs seedés dans chacun des 5 états | `GET /admin/downloads/queue` | 200 fragment ; chaque item affiche un état ∈ `{queued,running,completed,failed,canceled}` ; `running`/`completed` affichent `bytesDownloaded`/`bytesTotal`/`percent` | intégration |
| DL-041 | US-004.1 (Content-Length absent) | job avec `bytes_total=None` | builder `to_download_response`/`compute_percent` | `percent=None` (pas de `ZeroDivisionError`/`None`-crash), rendu fragment sans 500 | unit |
| DL-042 | spec §4 (calcul percent) | `bytes_total=1000, bytes_done=250` | `compute_percent(job)` | `25.0` (arrondi 1 décimale) | unit |
| DL-043 | spec §4 (calcul speedBps) | `state='running'`, `started_at` défini, horloge contrôlée (`monkeypatch` du epoch ms) | `compute_speed_bps(job)` pour `state='running'` puis `state='queued'` | valeur moyenne `bytes_done / max(1, Δt_s)` si `running` ; `None` sinon (jamais de division par zéro même à `Δt=0`) | unit |
| DL-044 | US-004.2 (persistance, pas de compteur mémoire) | job `running` en DB avec `bytes_done=X` | `GET /admin/downloads` (requête A) → mutation directe `bytes_done=Y` en DB → `GET /admin/downloads` (requête B, nouvelle session HTTP) | la requête B reflète **Y**, pas X ni une valeur en mémoire de la requête A | intégration |
| DL-045 | US-004.1 (amont sans Content-Length) | job `running` avec `bytes_total=None` alimenté par un vrai run `download_to_disk` sans header | `GET /admin/downloads/queue` | `bytesTotal=null`/`percent=null` tolérés, octets cumulés (`bytesDownloaded`) toujours affichés, jamais 500 | intégration |

### F-005 — Reprise & retries (US-005.1, US-005.2)

| Test ID | Story | Préconditions / Fixtures | Étapes | Résultat attendu | Type |
|---|---|---|---|---|---|
| DL-050 | spec §5.3 (nominal) | `xtream_stream_mock` GET 200 + `Content-Length` | `download_to_disk(url, dest, on_progress=spy)` | `.part` écrit puis `os.replace` vers `dest` ; `on_progress` appelé avec `bytes_done` **strictement croissant** ; `DownloadResult(already_present=False, resumed=False)` | unit |
| DL-051 | spec §5.3 (sans Content-Length) | mock sans header `Content-Length`, corps chunké | `download_to_disk(...)` | `bytes_total=None` tout du long, transfert complet malgré tout | unit |
| DL-052 | US-005.1 (reprise Range) | `.part` pré-existant de taille `n>0` ; mock répond **206** + `Content-Range` sur `Range: bytes=n-` | `download_to_disk(...)` | requête sortante porte `Range: bytes=n-` (assertion sur la requête capturée respx) ; `.part` **appendé** (pas tronqué) ; `DownloadResult(resumed=True)` | unit |
| DL-053 | US-005.1 (amont ignore Range) | `.part` pré-existant ; mock répond **200** complet (Range ignoré) | `download_to_disk(...)` | `.part` tronqué et redémarré (`'wb'`), transfert quand même `completed`, `resumed=False` | unit |
| DL-054 | spec §5.3 (416) | `.part` déjà complet ; mock répond **416** | `download_to_disk(...)` | promotion directe `.part`→`dest` sans requête de données supplémentaire | unit |
| DL-055 | Q1 PRD (skip-if-exists) | `dest` déjà présent (taille>0), pas de `.part` | `download_to_disk(...)` | `DownloadResult(already_present=True)` ; **0** requête GET émise (assert route respx non appelée) | unit |
| DL-056 | US-005.1 (retry manuel + reprise bout-en-bout) | job `failed` avec `.part` partiel, amont supportant `Range` | `retry_job(db, job_id)` puis 1 itération worker | `state='queued'`→`running`→`completed` sans re-télécharger l'octet déjà pris (assertion combinée avec DL-052 : header `Range` envoyé au run repris) | intégration |
| DL-057 | US-005.2 (auto-retry) | mock renvoie 502/timeout `k` fois puis succès, `DOWNLOAD_MAX_RETRIES=3` | drain worker sur plusieurs itérations | `attempts` incrémente à chaque échec transitoire ; `state` recycle `running→queued→running…` ; au-delà de `DOWNLOAD_MAX_RETRIES` → `failed` ; **`error` ne contient jamais l'URL/les credentials** (assertion croisée avec DL-110) | unit |
| DL-058 | US-005.2 (reap boot) | job pré-seedé `state='running'` (+ un `queued`, un `completed` témoins) | `reap_orphans(session_factory)` | le `running` repasse `queued`, `updated_at` bumpé ; les autres états **inchangés** | unit |
| DL-059 | US-005.2 (ordre boot) | spy sur `reap_orphans` et sur le claim `queued` | `run_drain_loop` démarré | `reap_orphans` est appelé **avant** toute tentative de claim `queued` (aucun `running` fantôme au moment où le drain commence à prendre des jobs) | unit |
| DL-05A | spec §5.1 (erreur permanente) | mock répond 404/403 ou `Content-Type` non-média | drain worker | `DownloadPermanentError` → `state='failed'` **directement**, sans consommer de cycle de retry transitoire (contraste avec DL-057) | unit |

### F-006 — Annulation (US-006.1)

| Test ID | Story | Préconditions / Fixtures | Étapes | Résultat attendu | Type |
|---|---|---|---|---|---|
| DL-060 | US-006.1 (queued) | job `state='queued'` | `cancel_job(db, job_id)` puis tentative de claim (`UPDATE...WHERE state='queued'`) | job → `canceled` immédiatement ; le claim ultérieur affecte **0** ligne (jamais démarré) | unit |
| DL-061 | US-006.1 (running) | job `state='running'` mi-transfert (mock avec délai contrôlable), `.part` partiel déjà écrit | `cancel_job(db, job_id)` pendant le transfert ; `cancel_check` du worker relit l'état | `download_to_disk` lève `DownloadCanceled` ; `.part` **conservé** (jamais renommé/supprimé) ; job reste `canceled` (le worker n'écrase pas vers `failed`/`completed`, `WHERE state='running'` affecte 0 ligne) | unit |
| DL-062 | US-006.1 | job `queued` ou `running` seedé | `POST /admin/downloads/{jobId}/cancel` | 200 fragment, `state='canceled'` en DB | intégration |
| DL-063 | spec §5.5 (no-op terminal) | job déjà `completed`/`failed`/`canceled` | `cancel_job(db, job_id)` | no-op, état inchangé | unit |
| DL-064 | spec §6.2 (course cancel/completed) | job `running` ; le worker écrit `state='completed'` (terminal `WHERE...state='running'`) **juste avant** que `cancel_job` s'exécute | `cancel_job(db, job_id)` après la transition terminale | `UPDATE...WHERE state IN ('queued','running')` affecte 0 ligne → job reste `completed` (le cancel perd proprement, aucune corruption d'état) | unit |

### F-007 — Confinement & sécurité chemin (US-007.1) — **BLOQUANT**

| Test ID | Story | Préconditions / Fixtures | Étapes | Résultat attendu | Type |
|---|---|---|---|---|---|
| DL-070 | US-007.1 (matrice d'attaque, `compute_dest_path` + `resolve_confined`) | titres paramétrés : `"../../../etc/passwd"`, séparateurs `/`/`\`, `".."` seul, octets de contrôle/NUL, unicode hostile (homoglyphes, RTL override, combinants), titre >180 car., espaces/points de tête/fin, chaîne vide | pour chaque titre : `dest = compute_dest_path(title=titre, ...)` puis `resolve_confined(dest)` | le chemin résolu est **strictement sous** `DOWNLOAD_DIR` (`resolved == base` ou `base in resolved.parents`) ; jamais de `PathConfinementError` sur la sortie déjà sanitizée ; segment capé ≤180 car. | unit (paramétré) |
| DL-071 | spec §5.2 (`resolve_confined` seul, contournement direct) | chemins relatifs forgés à la main **sans passer par** `compute_dest_path` : `"../../etc/passwd"`, `"/etc/passwd"`, `"..\\..\\windows\\system32"` | `resolve_confined(rel_path)` | lève **`PathConfinementError`** dans tous les cas ; **aucun** fichier créé/modifié hors `DOWNLOAD_DIR` | unit (paramétré) |
| DL-072 | défense en profondeur (symlink) | un symlink créé À L'INTÉRIEUR de `DOWNLOAD_DIR` pointant vers un dossier hors `DOWNLOAD_DIR` (skip si `os.symlink` échoue par privilège insuffisant — cf. §6.3) | `resolve_confined("lien/fichier")` | `realpath` résout le symlink puis rejette (`PathConfinementError`) — le confinement n'est pas contournable par lien symbolique | unit |
| DL-073 | US-007.1 (preuve filesystem bout-en-bout) | job enqueue avec titre Xtream hostile réel (ex. `"../../evil"`), `DOWNLOAD_DIR` = `tmp_path/dl`, **canary** placé juste au niveau parent immédiat de `DOWNLOAD_DIR` (`tmp_path/canary.txt`) avant le run | `enqueue_selection(...)` → 1 itération worker complète (respx mock) | canary **intact** (checksum inchangé) ; balayage récursif de `tmp_path` entier confirme **0** fichier créé hors de `DOWNLOAD_DIR` — c'est LE test qui prouve l'invariant produit « 0 fichier écrit hors DOWNLOAD_DIR » | intégration |
| DL-074 | spec §5.2 (garde config) | `settings.DOWNLOAD_DIR=""` | `resolve_confined(any_rel_path)` | lève `DownloadDisabledError` (pas `PathConfinementError`, pas de crash non catégorisé) | unit |
| DL-075 | anti-régression CR-S01 | schémas `DownloadEnqueueRequest` (JSON) + champs `Form` de `POST /admin/downloads` (HTMX) | introspection des champs Pydantic/Form | **aucun** champ `path`/`destPath`/`outputDir` acceptable en entrée — seuls `type`/`unificationId`/`serverId`/`ratingKey`/`scope` — garde statique contre la réintroduction du pattern `outputDir` verbatim | unit |

### F-008 — Persistance & migration (US-008.1)

| Test ID | Story | Préconditions / Fixtures | Étapes | Résultat attendu | Type |
|---|---|---|---|---|---|
| DL-080 | US-008.1 (DB fraîche) | engine `create_all` (pattern `test_media_group_migration.py`) | `_migration_018_create_download_tables(engine)` | no-op propre (tables déjà là via `create_all`) ; `download_job`/`download_batch` + 4 index présents | intégration (migration) |
| DL-081 | US-008.1 (idempotence) | idem DL-080 | migration exécutée **2 fois** sur le même engine | pas d'erreur, schéma identique | intégration (migration) |
| DL-082 | US-008.1 (DB upgradée) | engine construit **sans** `download_job`/`download_batch` (schéma limité à 017) | `_migration_018_create_download_tables(engine)` | tables + `ix_download_job_state`/`_batch`/`_created`/`_item` créées | intégration (migration) |
| DL-083 | spec §9 (chaîne complète) | engine fraîche | `run_migrations(engine)` (001→018 complet) | aucune erreur/`duplicate column` warning (extension de `test_migrations_no_duplicate_warning.py`) | intégration (migration) |
| DL-084 | US-008.1 (boot) | `api_client` (démarre `app.main.app`) | `GET /api/health` | 200 — confirme que l'ajout du schéma 018 ne casse pas le boot applicatif | intégration |
| DL-085 | spec §9 (`run_with_retry` partout) | code source `download_service.py`/`download_worker.py` | scan statique (grep-based test, pattern maison CR-T02/CR-C04) | **aucun** `db.commit()` nu sur le chemin requête/worker — tous les writers (`enqueue_selection`, `cancel_job`, `retry_job`, `clear_finished`, claim, progress, transitions terminales, `reap_orphans`) passent par `run_with_retry`/`commit_with_retry` | unit (statique) |

### F-101 — API JSON lecture (P1, dans le sprint courant)

| Test ID | Story | Préconditions / Fixtures | Étapes | Résultat attendu | Type |
|---|---|---|---|---|---|
| DL-090 | F-101 | jobs seedés, `X-API-Key` = secret maître | `GET /api/admin/downloads` | 200 `DownloadJobListResponse` ; champs **camelCase** exacts (`jobId`,`batchId`,`unificationId`,`serverId`,`ratingKey`,`bytesDownloaded`,`bytesTotal`,`destPath`,`createdAt`,`updatedAt`,`retries`) | intégration |
| DL-091 | F-101 (auth) | aucune clé | `GET /api/admin/downloads` | 401 | intégration |
| DL-092 | F-101 (auth, clé non-maître) | clé par-utilisateur active (table `api_keys`), PAS le secret maître | `GET /api/admin/downloads` | 401 (`verify_master_key` = secret maître **seul**, contrairement à `verify_backend_secret`) | intégration |
| DL-093 | F-101 | job seedé | `GET /api/admin/downloads/{jobId}` | 200 `DownloadJobResponse` | intégration |
| DL-094 | F-101 | id inconnu | `GET /api/admin/downloads/does-not-exist` | 404 | intégration |
| DL-095 | F-101 (filtre) | jobs seedés dans plusieurs états | `GET /api/admin/downloads?state=queued` | seuls les jobs `queued` dans la réponse | intégration |
| DL-096 | spec §4/§6.4 (`to_download_response`) | `DownloadJob` ORM en mémoire (états variés) | appel direct du builder | mapping colonne→schéma correct, `percent`/`speedBps` calculés (délègue à `compute_percent`/`compute_speed_bps`), `retries` = alias de `attempts` | unit |

### F-104 / F-102 / F-103 / F-201 / F-202 / F-203 — reportés (stub de plan seulement)

| Test ID | Story | Note |
|---|---|---|
| DL-100 | F-104 (`clear_finished`, signature figée spec §5.5) | `POST /admin/downloads/clear-finished` → supprime `completed`/`failed`/`canceled` uniquement, jobs actifs intacts. Exécuté **quand F-104 est réellement livré** (item stretch board). |
| DL-101..103 | F-102/F-103 | **Non figés** dans la spec (`# TBD` implicite : aucune signature de préflight/métrique donnée en §5/§6) → cas de test écrits **au moment où un ticket fige leur contrat**, pas avant (éviter de deviner une API). |
| DL-104 | F-201 (mutation JSON P2) | Miroir JSON de DL-025/DL-062/DL-056 (`POST /api/admin/downloads`, `.../{id}/cancel`, `.../{id}/retry`) — mêmes assertions d'état, transposées en JSON + `202`. Différé (P2, hors sprint P0). |
| DL-105 | F-202 (préfixe `[XXX]`) | `compute_dest_path(is_adult=True)` applique `apply_adult_prefix` au dossier + fichier film — helper déjà réutilisable (`schemas.py:16`), test à écrire dès que F-202 est activé au MVP (actuellement « prêt mais non appliqué »). |
| DL-106 | F-203 (NFO/poster) | Différé, réutilise `nfo_builder` existant (déjà testé côté génération Plex) — pas de nouveau risque identifié à ce stade. |

### Sécurité credentials — transverse (F-007 note sécurité + PRD §9 risques)

| Test ID | Préconditions / Fixtures | Étapes | Résultat attendu | Type |
|---|---|---|---|---|
| DL-110 | exception synthétique dont `str()` embarque une fausse URL `http://user:pass@host/...` | `_safe_error(exc)` | le message retourné (stocké dans `download_job.error`) **exclut** la sous-chaîne URL/credentials | unit |
| DL-111 | run complet échoué avec URL factice injectée, `caplog` actif | drain worker jusqu'à `failed` | **aucun** enregistrement de log ne contient la sous-chaîne URL/credentials (seuls `job_id`/`dest` cités, conforme §9 pt secrets) | unit |
| DL-112 | job avec compte Xtream réel (URL construite en interne, jamais persistée) | `GET /admin/downloads/queue` (HTML) et `GET /api/admin/downloads` (JSON) | corps de réponse **ne contient jamais** l'URL Xtream/user/password — seuls `serverId`/`ratingKey` exposés | intégration |
| DL-113 | schéma `DownloadJob` (ORM) | introspection des colonnes de `download_job`/`download_batch` | aucune colonne ne stocke une URL complète (`dest_path` relatif, `server_id`/`rating_key` seulement) — garde statique contre une future régression de schéma | unit (statique) |

### Worker master-only / garde de config — transverse (infra F-003/F-004)

| Test ID | Préconditions / Fixtures | Étapes | Résultat attendu | Type |
|---|---|---|---|---|
| DL-120 | technique `test_startup_wiring.py` (faux `fcntl` `flock_succeeds=True`, faux scheduler, `create_background_task` espionné) | `lifespan()` réel exécuté | `create_background_task(download_worker.run_drain_loop(...), name="download_worker")` appelé **uniquement** côté master | unit |
| DL-121 | idem, `flock_succeeds=False` (slave) | `lifespan()` réel exécuté | le worker de téléchargement **n'est jamais** planifié côté slave | unit |
| DL-122 | `DOWNLOAD_CONCURRENCY=1`, 3 jobs `queued`, hook de progression/gate contrôlé par le test (cf. risque §6.2) | drain de la file | au plus **1** job `running` à tout instant échantillonné (`Semaphore` respecté) ; variante `DOWNLOAD_CONCURRENCY=2` prouve que la borne est configurable | unit |
| DL-123 | aucun worker démarré | `enqueue_selection`/`cancel_job`/`retry_job` appelés directement | les writers request-path fonctionnent **indépendamment** du worker (pas de couplage à `is_master`) — les lignes DB atterrissent correctement même sans drain actif | unit |

### Non-régression — transverse (exigence explicite du mandat)

| Test ID | Étapes | Résultat attendu | Type |
|---|---|---|---|
| DL-130 | Suite `pytest -v` complète post-merge (735+ tests existants + nouveaux `test_download_*.py`) | 100 % verte — aucune régression sur les fichiers existants (nouveaux fichiers additifs uniquement) | intégration (gate CI) |
| DL-131 | Seed une ligne `download_job`/`download_batch`, puis rejouer `GET /api/media/movies`, `/shows`, `/{movies,shows}/unified` (reprend fixtures `test_unified_offload.py`/`test_media_serialization_singlepass.py`) | réponse **byte-identique** à la baseline captée sans la ligne `download_job` (la simple existence/peuplement des nouvelles tables n'altère aucune requête existante) | intégration |
| DL-132 | Génération `.strm` (`POST /api/plex/generate` ou CLI `generate`) avec `DOWNLOAD_DIR` configuré+peuplé vs vide | sortie **identique** dans les deux cas (les deux arbres sont hermétiques l'un à l'autre) | intégration |
| DL-133 | (doublon volontaire de DL-007, formulé comme gate non-régression explicite) `GET /admin/movies`, `/admin/import-nfo`, `/admin/keys` | 200 inchangé après l'édition d'1 ligne de `base.html` | intégration |
| DL-134 | `GET /openapi.json` (Basic Auth) | contient `DownloadJobResponse`/`DownloadJobListResponse` (schémas Pydantic v2, pas de dict nu) ; schémas `/api/ai`/`/api/media` existants toujours présents/inchangés (extension `test_ai_openapi.py`) | intégration |

---

## 4. Checks non-fonctionnels

| Check | Cas | Détail |
|---|---|---|
| **Latence endpoints** | (perf smoke, non gate dur) | `GET /admin/downloads/queue` avec ~200 jobs seedés doit rester rapide (usage des index `ix_download_job_state`/`ix_download_job_created`, pas de full-scan — leçon CR-P02) ; à observer, pas nécessairement un seuil chiffré figé dans cette passe. |
| **Cold-start IA ~30 s** | N/A | Aucun appel `/api/ai`/embeddings/LLM sur le chemin téléchargement — explicitement écarté pour cette feature (contrairement au reste du backend, cf. `CLAUDE.md` §9 pt 1). |
| **Migrations rejouables** | DL-080, DL-081, DL-082, DL-083 | 018 idempotente, `IF NOT EXISTS`, fin de chaîne, aucun warning `duplicate column`. |
| **Boot `uvicorn` + `/api/health` 200** | DL-084 | Confirmé via `api_client` (boot in-process de `app.main.app`) ; smoke Docker réel (2 Go) reste une vérification opérationnelle complémentaire (devops/release-manager), pas re-décrite ici. |
| **Comportement sous lock SQLite (`run_with_retry`)** | **DL-140** (nouveau, NFR) | DB fichier `tmp_path` WAL réelle + writer externe tenant un `BEGIN IMMEDIATE` (pattern `test_db_retry_real_lock.py`) pendant qu'une écriture de progression du worker (`UPDATE download_job SET bytes_done=... WHERE id=...` via `run_with_retry`) est déclenchée → doit **retenter puis réussir**, pas lever `OperationalError`/`PendingRollbackError` brut. |
| **Worker master-only** | DL-120, DL-121 | Cf. §3. |
| **Concurrence bornée** | DL-122 | Cf. §3 — sous réserve du hook d'instrumentation (risque §6.2). |
| **Disque plein (chemin d'erreur, adjacent à F-102 différé)** | **DL-141** (nouveau, NFR) | `os.replace`/écriture de chunk lève `OSError(ENOSPC)` simulé → `DownloadPermanentError`-style `failed`, message d'erreur **sans URL**, pas de fichier final corrompu/partiel promu (le `.part` peut rester, `dest` n'est jamais créé). Le préflight dur (F-102) est différé, mais le worker ne doit **pas** planter silencieusement sur disque plein dès le MVP. |

---

## 5. Fixtures nécessaires (à ajouter dans `tests/conftest.py` ou un module dédié `tests/_download_fixtures.py`, au moment de l'implémentation)

- **`download_dir(tmp_path, monkeypatch)`** — monkeypatch `settings.DOWNLOAD_DIR` sur un sous-dossier `tmp_path`, retourne le `Path`. Symétrique du besoin déjà couvert pour `PLEX_LIBRARY_DIR` ailleurs dans la suite.
- **`xtream_stream_mock`** (respx) — extension du `xtream_mock` existant capable de :
  - servir un corps avec/sans `Content-Length` ;
  - répondre `206`+`Content-Range` ou `200` (ignore Range) ou `416` selon le header `Range` reçu (inspection de la requête via `side_effect` respx, **premier usage streaming+Range-aware** du repo — cf. risque §6.4) ;
  - injecter une séquence d'échecs transitoires (502/timeout) puis un succès, pour les tests d'auto-retry.
- **`seeded_movie` / `seeded_series_with_episodes`** (basé sur `db_session`) — lignes `Media`(+`XtreamAccount`) minimales requises par `enqueue_selection` ; réutiliser un éventuel helper de seed déjà présent dans `test_unified_offload.py`/`test_plex_generator.py` plutôt que d'en dupliquer un nouveau (vérification à faire à l'implémentation).
- **`download_job_factory`** (basé sur `db_session`) — insère un `DownloadJob` directement dans un état donné (pour les tests machine-à-états cancel/retry/reap sans repasser par tout l'enqueue).
- **Réutilisées telles quelles** : `api_client`, `db_engine`/`db_session`/`db_factory`, `tmdb_mock`/`xtream_mock` (`tests/conftest.py`, aucune modification nécessaire) ; pattern `_admin_creds`/`ADMIN_AUTH` (`test_admin.py`) pour Basic Auth ; pattern `API_KEY`/`API_HEADERS` (`test_plex_api_security.py`) pour le secret maître.

---

## 6. Risques de testabilité (signalés maintenant, avant code)

1. **Wiring master-only** — pas un vrai risque : la technique de `tests/test_startup_wiring.py` (faux `fcntl` + faux
   scheduler + `create_background_task` espionné) s'applique directement (DL-120/121). Condition : le
   `backend-developer` doit garder le `create_background_task(download_worker.run_drain_loop(...))` **textuellement**
   dans le même bloc `if is_master:` que le reste (spec §7.3) — sinon le test doit être adapté au cas par cas.
2. **Concurrence réelle bornée (DL-122)** — prouver qu'au plus N jobs sont `running` simultanément nécessite un point
   de synchronisation déterministe (event/gate côté mock, ou hook d'instrumentation sur `download_to_disk`) plutôt
   qu'un test basé sur `asyncio.sleep` (source de flakiness sur CI lente). La spec ne fige pas explicitement un tel
   hook — **signalé au backend-developer** : prévoir un point d'injection testable (le paramètre `on_progress` déjà
   prévu §5.3 peut probablement servir de gate côté test, à confirmer à l'implémentation).
3. **Symlink-escape (DL-072)** — la création de symlinks peut nécessiter des privilèges élevés sous Windows (poste
   dev) ; exécutable sans souci en CI (ubuntu, `tests.yml`). À encadrer par un `skipif`/`try-except OSError → skip`
   plutôt que de bloquer la suite localement.
4. **respx streaming + `Range`-aware (§2, §5)** — premier mock de ce type dans le repo (les mocks existants sont
   surtout JSON statique côté TMDB/Xtream API). Nécessite d'inspecter le header `Range` de la requête sortante via
   `side_effect` et de streamer un corps arbitraire — faisable avec `respx>=0.21` (déjà dans `requirements-dev.txt`)
   mais plus complexe ; à valider tôt (dès PH-DL-03 mergé) pour ne pas découvrir une limitation respx tardivement.
5. **Preuve filesystem « 0 écriture hors `DOWNLOAD_DIR` » (DL-073)** — le canary doit être placé au niveau **parent
   immédiat** de `DOWNLOAD_DIR` (pas seulement à la racine `tmp_path`) pour que le test soit réellement discriminant
   contre une évasion `../` d'un seul niveau ; documenté explicitement dans le cas pour éviter un faux-négatif.
6. **`speedBps` dépendant du temps réel** — un test déterministe doit contrôler l'horloge (`monkeypatch` de
   `now_ms`/`time.time`, ou construire le job avec des timestamps fixes) plutôt que dépendre de la durée réelle
   d'exécution du test (DL-043).
7. **F-102/F-103 non figés** — aucune signature de préflight disque ni de métrique n'est donnée en spec §5/§6 → pas
   de cas de test écrit avant qu'un ticket ne fige leur contrat (reporté explicitement plutôt que deviné, cf. §1/§3
   « stub »).
8. **F-202/F-203 (P2)** — helper `apply_adult_prefix`/`nfo_builder` déjà existants et testés côté génération Plex ;
   pas de risque nouveau identifié, cas différé jusqu'à activation du ticket.

---

## 7. Critères de sortie (release gate P0 de cette feature)

- [ ] **DL-070 → DL-075 (confinement, bloquant)** tous verts, y compris DL-073 (preuve filesystem bout-en-bout). Un
  seul échec ici = release **bloquée** (S1 — house law : aucune release avec S1/S2 ouvert sur une feature P0).
- [ ] Cycle de vie complet vert (DL-020…064), y compris la course cancel/completed (DL-064).
- [ ] Migration 018 idempotente sur DB fraîche **et** DB upgradée (DL-080…083), chaîne 001→018 sans warning.
- [ ] Boot `uvicorn app.main:app` + `GET /api/health` **200** confirmé (DL-084).
- [ ] **0** credential Xtream trouvé : en DB (`download_job.error`/toute colonne), dans les logs capturés, dans les
  réponses HTML/JSON (DL-110…113).
- [ ] Garde `DOWNLOAD_DIR` vide testée (DL-028, DL-074), aucun crash 5xx.
- [ ] Auth : `/admin/downloads` 401 sans Basic Auth (DL-002) ; `/api/admin/downloads` 401 sans clé **et** 401 avec une
  clé par-utilisateur non-maître (DL-091/092).
- [ ] Non-régression : suite existante verte (DL-130) ; `/api/media/*` et génération `.strm` inchangés (DL-131/132).
- [ ] Worker master-only prouvé (DL-120/121) ; concurrence bornée démontrée au moins une fois (DL-122 — si le hook
  d'instrumentation manque à l'implémentation, documenter la limite dans le rapport plutôt que de skip silencieusement).
- [ ] **0** bug S1/S2 ouvert sur cette feature au moment du sign-off (`docs/51-bugs.md`, à ouvrir dès l'exécution).
- **Non bloquant pour ce sprint** (dette documentée acceptable) : F-102/F-103/F-104 (P1 stretch) et F-201/F-202/F-203
  (P2) — s'ils ne sont pas livrés, ils ne bloquent pas le sign-off P0 tant que les non-goals PRD §6 restent respectés.

---

## Handoff

```
NEXT:
- qa-engineer : revenir dès PH-DL-01 + PH-DL-03 mergés pour écrire tests/test_download_*.py (fichiers réels) sur la
  base des Test ID DL-xxx ci-dessus ; exécuter au fil des merges PH-DL-04/05 ; déposer les bugs trouvés dans
  docs/51-bugs.md (sévérité S1..S4, cf. gabarit standard) ; republier une synthèse dans docs/daily/<date>.md.
- backend-developer (lot service, PH-DL-03) : prévoir un point d'injection testable pour DL-122 (concurrence bornée)
  — voir risque §6.2.
- security-reviewer : porter une attention particulière à DL-070..075/110..113 en revue (PH-DL-R2).
```
