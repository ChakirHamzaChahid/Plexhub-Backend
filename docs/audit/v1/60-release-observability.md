# Audit v1 — Phases 7-8 : Release & Observabilité

> **Audit 360° indépendant** — branche `develop`, HEAD `9da9d46` (release v1.7.1), 2026-07-26.
> Preuves : lecture `fichier:ligne`, `git show`/`git tag`, probes `curl` sur le serveur de smoke (`/metrics`, `/api/health`), exécution du regex du hook SessionStart sous node. Statut des `CR-*` antérieurs = rapport DELTA (autre agent) ; cités ici uniquement en recoupement.

---

## Phase 7 — Release (CI, Docker, versioning, process)

### AUDIT-P7-001 — Chaîne de versioning v1.7.1 : cohérente de bout en bout — **info (vérifié sain)**

- `app/main.py:37` : `APP_VERSION = "1.7.1"`.
- `GET /api/health` (smoke) : `{"version":"1.7.1"}` — version live via `request.app.version`, pas de valeur codée en dur.
- Tag `git v1.7.1` → commit `b734a28` (« Merge develop into main for v1.7.1 »), présent sur `main` ; `main` contient bien `9da9d46` + `38aeb5a`. Tags `v1.7.0`/`v1.7.1` existants.
- `docker.yml:41-45` : `type=semver,pattern={{version}}` + `{{major}}.{{minor}}` + sha → un push du tag `v1.7.1` produit les images GHCR `1.7.1`/`1.7` (+ `latest` via le flavor par défaut de `metadata-action` sur tag semver). Registre non vérifiable hors-ligne — pattern conforme à la convention documentée.

### AUDIT-P7-002 — Dérive CLAUDE.md à HEAD : la règle anti-dérive a été violée deux fois sur les 2 derniers commits — **S3 (process)**

**Preuve** :
- `38aeb5a` (fix DAV prewarm) a mis à jour `CLAUDE.md` §5.11 + piège 18g **dans le même commit** (diff vérifié) — mais **pas le bandeau** : « À JOUR AU : 2026-07-23 (HEAD develop `1ac00d3`, release v1.7.0) » est resté tel quel alors que la règle exige « bandeau (date + HEAD) + section concernée ». Respect **partiel**.
- `9da9d46` (bump `APP_VERSION` 1.7.0→1.7.1) touche `app/main.py` **seul** — `CLAUDE.md` §2/§4 déclarent toujours `APP_VERSION = "1.7.0"` (`CLAUDE.md` lignes 23 et 58 vs `app/main.py:37`). Respect **nul** (et pas de `/sync-context` compensatoire : le dernier est `4d60122`, antérieur).

**Impact** : le bandeau — l'ancre de confiance de tout le système documentaire — pointe un HEAD faux et une version fausse. Tout agent/développeur qui s'y fie part avec 2 commits d'angle mort (dont un correctif d'exploitation critique DAV). **Effort** : `/sync-context` = minutes. La cause systémique est P7-003.

### AUDIT-P7-003 — Le détecteur de dérive SessionStart est **silencieusement inerte** : son regex ne matche plus le format du bandeau — **S2 (process)**

**Preuve (empirique)** : `.claude/hooks/session-start.js:17` attend `(HEAD <hash>)` :
```
/À JOUR AU\s*:\s*([0-9-]+)\s*\(HEAD\s*[`']?([0-9a-f]{7,40})[`']?\)/i
```
Le bandeau réel est `(HEAD develop \`1ac00d3\`, release **v1.7.0**)` — le mot `develop` et le suffixe `, release …` cassent le match. Exécution node sur le `CLAUDE.md` courant : **`NO MATCH`** → le hook n'émet **ni** l'alerte « PÉRIMÉ » **ni** le « ✅ à jour » (échec avalé par le `catch` silencieux `session-start.js:44`). Le filet de sécurité que le process revendique (« Le détecteur SessionStart signale la dérive ») **ne fonctionne pas**, ce qui explique que la dérive P7-002 soit passée inaperçue.

Le second filet, `.git/hooks/post-commit` (installé, vérifié), est **purement consultatif** (echo + `exit 0`, `.claude/hooks/post-commit:8-11`) : il a forcément imprimé son avertissement sur `9da9d46` (commit `app/` sans `CLAUDE.md`) et a été ignoré.

**Impact** : l'ensemble du dispositif anti-dérive est aujourd'hui du théâtre de process : aucun des deux hooks ne peut bloquer ni même signaler de façon fiable. **Effort** : trivial — assouplir le regex (`\(HEAD[^)]*?([0-9a-f]{7,40})`) + un `else` loggant « bandeau non parsable » au lieu du silence ; optionnel : rendre le post-commit bloquant sur les commits `release:`.

### AUDIT-P7-004 — CI : triggers et gates réels vérifiés — globalement sains ; le gate black est cosmétique — **S3**

**Vérifié sain** :
- `tests.yml:3-7` : push **et** PR sur `main` **et** `develop` (l'ancien trou « develop non gardé », CR-T10, est fermé).
- Gate couverture : `pytest -v` en CI hérite de `addopts = "--cov=app --cov-report=term-missing --cov-fail-under=70"` (`pyproject.toml`, section `[tool.pytest.ini_options]`) — le gate s'applique bien en CI même si absent du YAML.
- **Plus aucun `--deselect`** dans `tests.yml` (l'ancien deselect du test EPG base64 a disparu ; le bug sous-jacent est corrigé — rejet de U+FFFD, `app/services/live_service.py:44-47` ; statut CR-T01 au DELTA).
- Job lint séparé : `ruff check .` réel.

**Réserves** :
- `black --check .` (`tests.yml:32-34`) est un **no-op structurel** : `[tool.black] extend-exclude` exclut `^/app/` et `^/tests/` (`pyproject.toml`, fin de fichier) — le gate « format check » ne vérifie donc aucun code applicatif. C'est documenté et assumé (CR-C06 follow-up), mais un lecteur du YAML croit à un gate qui n'existe pas.
- `mypy` est installé mais volontairement hors CI (`[tool.mypy]` commentaire) — cohérent avec la doc, aucun gate typé.
- `filterwarnings` ignore `PytestUnhandledThreadExceptionWarning` globalement (`pyproject.toml`) — dette assumée (CR-T10 résiduel).

### AUDIT-P7-005 — Matrice de runtimes : la version Python **livrée** (3.12) n'est jamais testée en CI — **S3**

**Preuve** : CI = Python **3.13** unique (`tests.yml:20,46`) ; image = `python:3.12-slim` (`Dockerfile:1`) ; outillage ciblé 3.12 (`[tool.ruff] target-version = "py312"`, `[tool.black] target-version = ["py312"]`, `[tool.mypy] python_version = "3.12"`) ; dev local = 3.12.10. La seule combinaison jamais exercée par la CI est celle qui ne tourne nulle part en prod, et inversement. Risque concret : une API 3.13-only (ou un changement de comportement 3.13, p. ex. sémantique asyncio/GC) passe verte en CI et casse dans l'image ; ou une deprecation 3.13 masque un warning qui frappera à l'upgrade. **Effort** : faible — `strategy.matrix.python-version: ["3.12", "3.13"]` sur le job pytest.

### AUDIT-P7-006 — `docker-compose.yml` livré = déploiement qui ne peut pas fonctionner : `AI_API_KEY`/`ADMIN_PASSWORD` (et 18 autres clés) ne sont pas injectées dans le conteneur — **S2 (release)**

**Preuve** : diff exhaustif clés `config.py` ⇆ bloc `environment:` du compose. Non injectées (extrait) : **`AI_API_KEY`**, **`ADMIN_USERNAME`/`ADMIN_PASSWORD`**, `TV_AUTH_ENCRYPTION_KEY`, `XTREAM_ENCRYPTION_KEY`, `CORS_ORIGINS`, `OLLAMA_URL`/`OLLAMA_MODEL`, `PLEX_ACCOUNT_TOKEN`/`PLEX_CLIENT_IDENTIFIER`, `TMDB_LANGUAGE`, `BACKUP_*`, `DATA_DIR`/`LOG_DIR`, `AI_EMBED_*`, `ADULT_*`, `XTREAM_USER_AGENT`. Le `.env` hôte n'est utilisé par compose que pour la **substitution** des variables listées — il n'est ni copié dans l'image (`Dockerfile` ne copie que `app/`) ni monté en volume → `load_dotenv` dans le conteneur ne trouve rien.

**Conséquence d'un `docker-compose up` tel que livré** :
- `AI_API_KEY` vide → `_is_master` toujours faux (`deps.py:44-48`) ; aucune clé par-utilisateur ne peut être créée (`verify_master_key` → 503 « Backend secret not configured », `deps.py:95-99`) → **100 % de l'API JSON répond 401, sans issue possible**.
- `ADMIN_PASSWORD` vide → UI `/admin` + `/docs` en 503 (`deps.py:120-124`).
- `TV_AUTH_ENCRYPTION_KEY` absent → repli sur la dérivation depuis `AI_API_KEY`… lui-même vide (recoupe CR-S04).

La posture est fail-closed (aucune exposition — c'est le bon côté), mais le compose de référence produit un conteneur inutilisable, et l'incohérence est trompeuse : `TMDB_API_KEY`/`OMDB_API_KEY`/`XTREAM_*` **sont** forwardées, ce qui suggère que la liste se voulait complète. **Effort** : faible — ajouter les clés manquantes au bloc `environment:` (ou un `env_file: .env` explicite) + smoke de boot compose dans la CI docker.

### AUDIT-P7-007 — Dockerfile : process root, pas de `HEALTHCHECK` image, `.dockerignore` à vérifier — **S3 (hardening)**

**Preuve** : `Dockerfile` (11 lignes) — aucun `USER` (uvicorn tourne **root** dans le conteneur ; les volumes montés `./data`, `./logs`, media, downloads sont donc écrits root sur l'hôte), aucun `HEALTHCHECK` dans l'image (le healthcheck n'existe qu'au niveau compose — un run hors-compose, p. ex. depuis GHCR en `docker run`, n'en a pas), CMD via `sh -c` (nécessaire pour `${APP_PORT}` — acceptable, `exec` présent donc les signaux passent). Pas de pin de digest de base ni de build multi-stage (image slim, surface faible — acceptable pour ce projet). **Effort** : faible (`USER` non-root + `HEALTHCHECK` image).

### AUDIT-P7-008 — `.env.example` incomplet : 14 clés lues par `config.py` non documentées, dont 3 secrets de sécurité — **S3**

**Preuve** (diff `os.getenv` de `config.py` ⇆ `.env.example`) : manquent `ADMIN_USERNAME`/`ADMIN_PASSWORD` (sans eux : admin/docs 503 — comportement voulu mais indocumenté), `TV_AUTH_ENCRYPTION_KEY` (son absence active silencieusement la dérivation de clé Fernet depuis `AI_API_KEY` — réutilisation de clé, recoupe **CR-S04** : l'opérateur ne peut pas configurer ce qu'il ignore), `XTREAM_ENCRYPTION_KEY` (même logique pour le chiffrement au repos des mots de passe Xtream, M016), `PLEX_LIBRARY_DIR`, `DOWNLOAD_DIR` (les deux features majeures sont no-op sans eux), `PLEX_ACCOUNT_TOKEN`/`PLEX_CLIENT_IDENTIFIER`, `DAV_ACCOUNT_IDS`, `AI_EMBED_MODEL`/`AI_EMBED_CACHE_DIR`, `ADULT_CONTENT_RATING`/`ADULT_CATEGORY_IDS`, `XTREAM_USER_AGENT`. (Sens inverse OK : les clés « en trop » de `.env.example` sont lues via les helpers `_env_int`/`_env_float` `config.py:14,24` ou sont compose-only `*_HOST_PATH`/`APP_PORT`/`TZ` — pas de variable morte détectée.) **Effort** : trivial.

---

## Phase 8 — Observabilité

### AUDIT-P8-001 — Les 5 métriques métier `plexhub_*` n'exposent **aucune série** tant qu'aucun événement n'a eu lieu : l'alerting par absence est impossible — **S2**

**Preuve (empirique, smoke)** : `GET /metrics` expose les lignes `# HELP`/`# TYPE` des 5 métriques (elles **sont** enregistrées au registry à l'import, `app/utils/metrics.py:14-43`) mais **zéro échantillon** — toutes sont labellisées (`account_id`, `kind`×`result`, `media_type`×`result`, `status`) et `prometheus_client` ne crée un child qu'au premier `.labels(...)`.

**Impact concret pour l'alerting** :
- `rate(plexhub_tmdb_requests_total[5m])` sur une instance qui n'a jamais enrichi = **no data**, pas 0 → une alerte « le sync ne tourne plus » basée sur ces séries ne se déclenche jamais sur le cas le plus grave (le pipeline n'a **jamais** démarré : master election ratée, scheduler mort au boot, worker slave scrappé par erreur).
- Impossible de distinguer « instance saine mais quiète », « slave » (les métriques métier ne sont incrémentées que par les workers master-only) et « instrumentation cassée ».
- Aucune métrique de **fraîcheur** (`last_success_timestamp` de pipeline/sync/backup) n'existe : le seul signal de vie du pipeline est le log.

**Recommandation** : (1) gauge non-labellisée `plexhub_pipeline_last_success_timestamp_seconds` (+ une par job planifié critique) — c'est la seule vraie parade ; (2) règles d'alerte en `absent()` documentées à défaut ; (3) éventuellement pré-initialiser les labels énumérables (`result`, `media_type`, `status`). **Effort** : faible.

### AUDIT-P8-002 — Angles morts d'instrumentation confirmés : downloads (Xtream+Plex), sync Plex, relay DAV et OMDb n'ont **zéro** métrique — **S2 (recoupe F-103)**

**Preuve** : grep exhaustif des consommateurs de `app/utils/metrics.py` — seulement 4 : `sync_worker.py:1092` (durée sync), `enrichment_worker.py:385,725` (match + queue), `health_check_worker.py:771` (alive ratio), `tmdb_service.py:176` (requêtes TMDB). **Aucun** import dans `download_worker.py`, `download_service.py`, `plex_sync_service.py`, `plex_download_service.py`, `omdb_service.py`, `app/dav/*`, `enrichment_backfill_worker.py`.

**Impact** : les sous-systèmes les plus récents et les plus risqués — les **seuls qui écrivent des octets sur disque** (downloads) et les **seuls qui relaient des flux upstream en continu** (DAV) — sont invisibles de Prometheus : pas de profondeur de file `download_job` (queued/running/failed), pas de compteur d'échecs/retries de transfert, pas d'octets téléchargés, pas de saturation du throttle DAV (503 émis, temps d'attente des permits), pas de compteur d'appels/budget OMDb (`OMDB_DAILY_LIMIT` consommé = invisible), pas de résultat du sync Plex. En incident (disque plein, provider qui 403, cascade 503 DAV pendant un scan), le diagnostic repose à 100 % sur `logs/plexhub.log`. Recoupe le follow-up **F-103** (board) — connu, toujours non livré, et le périmètre a grossi depuis (DAV, OMDb). **Effort** : moyen (une passe d'instrumentation par sous-système ; commencer par download queue depth + DAV throttle, les deux plus actionnables).

### AUDIT-P8-003 — `/metrics` toujours public — **S3 (recoupe CR-S02, re-vérifié à HEAD)**

**Preuve** : `curl` sans auth → 200 ; `setup_instrumentator` (`metrics.py:46-51`) expose sans dépendance d'auth ; aucune garde au mount (`main.py:660`). Les labels `account_id` (sync duration, alive ratio) exposent les identifiants de comptes Xtream à quiconque atteint le port. Statut détaillé au DELTA ; constat re-confirmé ici car il conditionne les recommandations P8-001/002 (ajouter des métriques enrichit aussi ce qui fuit — protéger `/metrics` d'abord, ou au niveau ingress).

### AUDIT-P8-004 — Logs : conventions vérifiées saines (request_id, httpx épinglé WARNING, rotation) — **info**

- Format vérifié sur `logs/plexhub.log` : `2026-07-23 20:38:14,991 [-] [plexhub] INFO: …` — le slot `[-]` est le `request_id` (contextvar + filtre), peuplé sur les requêtes HTTP.
- **Épinglage `httpx` WARNING tient** : `main.py:104` `logging.getLogger("httpx").setLevel(logging.WARNING)`, avec le commentaire de garde (`main.py:100-103`) expliquant le risque de fuite d'URLs à credentials (relay DAV, downloads) si quelqu'un repasse le root à INFO — le pin est indépendant du niveau root, conforme à la revue F3.
- `SafeRotatingFileHandler` (avale `PermissionError` Windows) présent ; fichier DEBUG / console INFO.
- Réserve mineure : le `request_id` client n'est pas borné en taille (CR-S09, statut au DELTA).

### AUDIT-P8-005 — Jobs planifiés : inventaire vérifié ; aucun signal de santé du scheduler n'est exposé — **S3**

**Inventaire réel (master seul, `main.py:292-415`)** : `sync_enrich_generate` (interval `SYNC_INTERVAL_HOURS`, mutex `_PIPELINE_LOCK` partagé avec le run initial — le commentaire `main.py:108-110` acte le fix CR-F04, statut au DELTA), `health_check` (cron 2 h), `epg_cleanup` (3 h), `subtitle_cache_cleanup` (3 h), `db_backup` (cron `BACKUP_HOUR`, si `BACKUP_ENABLED`), `plex_catalogue_sync` (interval, **enregistré seulement si** `PLEX_ACCOUNT_TOKEN` et `PLEX_SYNC_INTERVAL_HOURS>0`). Drain download master-only après `scheduler.start()` (`main.py:417-421`). Shutdown propre (`main.py:501-503`).

**Le trou** : si le master **meurt**, `restart: unless-stopped` relance le conteneur (single-process : le nouveau process reprend le flock) — OK. Mais si le master **se fige** (boucle affamée par la génération sur l'event-loop — dette CR-C01 —, deadlock, task pendue), rien ne le détecte : le healthcheck compose ne teste que `GET /api/health` (qui répondra tant que la boucle respire un peu), aucune métrique « dernier run réussi par job » n'existe (cf. P8-001), et `/api/health` ne porte aucun champ scheduler/master. Un pipeline planifié mort peut passer inaperçu **6 h+ × N** jusqu'à ce qu'un humain remarque un catalogue périmé. **Effort** : faible — exposer `is_master` + timestamps de dernier succès par job dans `/api/health` (ou les gauges de P8-001).

### AUDIT-P8-006 — Capacité de diagnostic incident : verdict global — **S3 (synthèse)**

Avec l'état actuel : un incident **HTTP** (5xx, latence) est bien couvert (instrumentator + request_id + logs DEBUG). Un incident **pipeline/worker** (sync bloqué, downloads en échec, budget OMDb épuisé, throttle DAV saturé, scheduler figé) n'a **aucun signal proactif** — découverte par symptôme utilisateur, diagnostic par lecture de log uniquement, sur un fichier local au conteneur (pas de shipping ; `json-file` 10 Mo×5 côté Docker). C'est acceptable pour un déploiement mono-hôte auto-hébergé, mais les trois findings P8-001/002/005 forment un même chantier « fraîcheur + files » qui devrait précéder toute nouvelle feature d'écriture. **Effort** cumulé : moyen, fortement mutualisable.

---

## Récapitulatif Phases 7-8

| ID | Sévérité | Titre | Preuve clé |
|---|---|---|---|
| AUDIT-P7-001 | info | Versioning v1.7.1 cohérent (code ⇆ health ⇆ tag ⇆ pattern GHCR) | `main.py:37`, curl health, `git tag`/`b734a28`, `docker.yml:41-45` |
| AUDIT-P7-002 | S3 | Dérive CLAUDE.md : bandeau/`§4` faux à HEAD (règle violée sur `38aeb5a` partiel + `9da9d46` total) | `git show 38aeb5a`/`9da9d46`, CLAUDE.md l.23/58 |
| AUDIT-P7-003 | **S2** | Détecteur SessionStart inerte (regex ≠ format bandeau, échec silencieux) ; post-commit consultatif ignoré | `session-start.js:17` + node `NO MATCH` ; `post-commit:8-11` |
| AUDIT-P7-004 | S3 | CI : triggers/gates sains, mais `black --check` = no-op (exclut `app/`+`tests/`) | `tests.yml:3-7,32-34`, `pyproject.toml` addopts + extend-exclude |
| AUDIT-P7-005 | S3 | Python 3.12 (prod) jamais testé en CI (3.13 seul) | `tests.yml:20,46` vs `Dockerfile:1` |
| AUDIT-P7-006 | **S2** | Compose livré inopérant : `AI_API_KEY`/`ADMIN_PASSWORD`/+18 clés non injectées → 100 % API en 401, admin 503 | diff config⇆compose ; `deps.py:44-48,95-99,120-124` |
| AUDIT-P7-007 | S3 | Dockerfile : root, pas de HEALTHCHECK image | `Dockerfile` (pas de `USER`) |
| AUDIT-P7-008 | S3 | `.env.example` : 14 clés non documentées dont `TV_AUTH_ENCRYPTION_KEY`/`XTREAM_ENCRYPTION_KEY`/`ADMIN_PASSWORD` | diff config⇆`.env.example` (recoupe CR-S04) |
| AUDIT-P8-001 | **S2** | Métriques métier labellisées sans zéro-init ni fraîcheur → alerting par absence impossible, pipeline mort invisible | curl `/metrics` (HELP sans samples), `metrics.py:14-43` |
| AUDIT-P8-002 | **S2** | Zéro métrique sur downloads/sync Plex/DAV/OMDb (F-103, périmètre élargi) | grep consommateurs `metrics` (4 seulement) |
| AUDIT-P8-003 | S3 | `/metrics` public (labels `account_id`) — re-confirmé à HEAD | curl 200 sans auth ; `metrics.py:46-51` (recoupe CR-S02) |
| AUDIT-P8-004 | info | Logs sains : request_id, httpx épinglé WARNING, rotation | `main.py:100-104`, `logs/plexhub.log` |
| AUDIT-P8-005 | S3 | Aucun signal de santé scheduler/master (freeze indétectable par le healthcheck) | `main.py:292-421`, healthcheck compose |
| AUDIT-P8-006 | S3 | Synthèse : incidents worker/pipeline sans signal proactif, diagnostic 100 % logs locaux | P8-001/002/005 |
