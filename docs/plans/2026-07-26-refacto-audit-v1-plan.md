# Plan de migration — `/refacto` remédiation audit v1 (V1 + V2 + hardening V3)

> **Phase 1 du workflow `/refacto`.** Ce document est le plan d'exécution ; il ne
> contient aucun code applicatif. Décisions structurantes :
> `docs/architecture/adr/0004-audit-v1-remediation-contracts.md`.
>
> - **Base** : branche `develop`, HEAD `cea0a3e` (`fix(hooks): ne plus crier « PÉRIMÉ »…`).
> - **Cahier des charges** : `docs/audit/v1/` — FINAL-REPORT §5 (Top-10) et §6 (roadmap).
> - **Périmètre décidé** : roadmap **complète** V1 + V2 + hardening V3.
> - **Déjà faits, à ne pas replanifier** : `AUDIT-P7-003` (regex du hook réparé, `cea0a3e`)
>   et `AUDIT-P0-002` (§10 de `CLAUDE.md` recalée sur le DELTA, `72b0ec8`).
> - **29 étapes**, réparties en **7 vagues**. Une seule migration de schéma dans tout le
>   lot : **023** (additive, non destructive).
> - Convention de commit : dév **directement sur `develop`**, un commit par étape, chaque
>   commit vert et révocable seul par `git revert`.

---

## 0. Table de correspondance findings → étapes

| Finding | Sév | Vague.Étape | Titre court |
|---|:--:|---|---|
| AUDIT-P7-006 | S2 | **S0.1** | compose `env_file` (déploiement de référence inopérant) |
| AUDIT-P7-008 | S3 | **S0.2** | `.env.example` complet (14 clés, dont 3 secrets) |
| AUDIT-P7-005 | S3 | **S0.3** | matrice CI 3.12 + 3.13 |
| AUDIT-P7-004 | S3 | **S0.4** | `black --check` honnête (no-op structurel) |
| AUDIT-P1-001 | S2 | **S1.1** | primitive `write_with_retry` + `commit_with_retry` honnête |
| AUDIT-P4-001 | S2 | **S1.2** | assertion de boot sur `app.routes` |
| AUDIT-P3-001 | S2 | **S1.3** | migration 023 `ANALYZE` + `PRAGMA optimize` de fin de pipeline |
| AUDIT-P8-001 (a) | S2 | **S1.4** | déclaration centrale de toute la surface métrique + zéro-init |
| AUDIT-P5-001 / P5-008 / P6-005 | S2/S3 | **S2.1** | handshake `jobId` bout-en-bout + 404 |
| AUDIT-P3-003 | S2 | **S2.2** | agrégation `DatabaseSource` off-loop |
| AUDIT-P4-005 | S3 | **S2.3** | convergence `display_rating` + backfill |
| AUDIT-P3-002 (volet visibilité) | S2 | **S2.4** | métrique `plexhub_unified_path{snapshot\|live}` |
| AUDIT-P6-006 | S3 | **S2.5** | politique de refresh des NFO/posters générés |
| AUDIT-P5-004 | S2 | **S2.6** | log WARN + compteur sur le 400 `server_id` |
| AUDIT-P1-001 (call-sites) | S2 | **S3.1→S3.4** | migration des writers vers `write_with_retry`, par zone |
| AUDIT-P2-001 / P8-003 / CR-S02 | S2 | **S4.1** | auth sur `/metrics` |
| AUDIT-P2-004 / CR-S05 | S2 | **S4.2** | rate-limit + cap de sessions `pending` |
| AUDIT-P2-005 / CR-S07 | S3 | **S4.3** | CSRF `Sec-Fetch-Site` sur POST `/admin*` |
| AUDIT-P2-008 / CR-S08 | S3 | **S4.4** | vetting SSRF sur images + health-check |
| AUDIT-P2-003 / CR-S04 | S3 | **S4.5** | séparation de domaine de la clé Fernet tv-auth |
| AUDIT-P8-001 (b) / P8-005 | S2/S3 | **S5.1** | gauges `last_success` par job + `isMaster` dans `/api/health` |
| AUDIT-P8-002 / F-103 | S2 | **S5.2→S5.4** | métriques downloads / DAV / OMDb+sync Plex |
| AUDIT-P7-007 | S3 | **S6.1** | `USER` non-root + `HEALTHCHECK` image |
| — (house law) | — | **S6.2** | clôture : `/sync-context`, board, statuts findings |

---

## 1. Cartographie des dépendances — **ce qui se parallélise, ce qui ne se parallélise pas**

### 1.1 Matrice de possession de fichiers

Un fichier ne doit être ouvert que par **une seule** étape à la fois. Le tableau liste
chaque fichier touché par ≥ 1 étape, avec les étapes propriétaires.

| Fichier | Étapes propriétaires | Verdict |
|---|---|---|
| `app/main.py` | S1.2, S1.3, S4.1, S4.2, S4.3, S5.1 | 🔴 **HOTSPOT — sérialisé** |
| `app/utils/metrics.py` | S1.4, S4.1 | 🟠 sérialisé (S1.4 puis S4.1) |
| `app/utils/db_retry.py` | S1.1 | 🟢 exclusif |
| `app/workers/sync_worker.py` | S2.1, S3.1 | 🟠 sérialisé (S2.1 → S3.1) |
| `app/api/sync.py` | S2.1 | 🟢 exclusif |
| `app/services/job_registry.py` *(nouveau)* | S2.1 | 🟢 exclusif |
| `app/models/schemas.py` | S2.1, S5.1 | 🟠 sérialisé (zones différentes, additif) |
| `app/plex_generator/source.py` | S2.2 | 🟢 exclusif |
| `app/plex_generator/storage.py` | S2.5, S4.4 | 🟠 sérialisé (S2.5 → S4.4) |
| `app/plex_generator/generator.py` | S2.5 | 🟢 exclusif |
| `app/api/plex.py` | S2.5 | 🟢 exclusif |
| `app/services/plex_generation_service.py` | S2.5 | 🟢 exclusif |
| `app/services/nfo_import_service.py` | S2.3 | 🟢 exclusif |
| `app/utils/unification.py` | S2.3 | 🟢 exclusif |
| `app/services/media_service.py` | S2.4 | 🟢 exclusif |
| `app/api/media.py` | S2.6 | 🟢 exclusif |
| `app/db/migrations.py` | S1.3 | 🟢 exclusif (**seule migration du lot**) |
| `app/db/maintenance.py` *(nouveau)* | S1.3 | 🟢 exclusif |
| `app/api/route_audit.py` *(nouveau)* | S1.2 | 🟢 exclusif |
| `app/workers/enrichment_worker.py` | S3.1 | 🟢 exclusif |
| `app/workers/enrichment_backfill_worker.py` | S3.1 | 🟢 exclusif |
| `app/workers/health_check_worker.py` | S3.1, S4.4 | 🟠 sérialisé (S3.1 → S4.4) |
| `app/services/{category,account,live}_service.py` | S3.2 | 🟢 exclusif |
| `app/api/{accounts,categories,live,ai}.py` | S3.3 | 🟢 exclusif |
| `app/api/tv_auth.py` | S3.3, S4.2 | 🟠 sérialisé (S3.3 → S4.2) |
| `app/cli.py`, `app/scripts/validate_id_consistency.py` | S3.4 | 🟢 exclusif |
| `app/api/deps.py` | S4.1 | 🟢 exclusif |
| `app/utils/rate_limit.py` *(nouveau)* | S4.2 | 🟢 exclusif |
| `app/api/csrf.py` *(nouveau)* | S4.3 | 🟢 exclusif |
| `app/utils/ssrf.py` *(nouveau)* | S4.4 | 🟢 exclusif |
| `app/services/download_service.py` | S4.4, S5.2 | 🟠 sérialisé (S4.4 → S5.2) |
| `app/utils/payload_crypto.py` | S4.5 | 🟢 exclusif |
| `app/api/health.py` | S5.1 | 🟢 exclusif |
| `app/workers/download_worker.py` | S5.2 | 🟢 exclusif |
| `app/dav/{throttle,relay}.py` | S5.3 | 🟢 exclusif |
| `app/services/{omdb,plex_sync}_service.py` | S5.4 | 🟢 exclusif |
| `app/config.py` | S2.5, S4.1, S4.2, S5.x | 🟡 **append-only** — conflits triviaux |
| `.env.example` | S0.2, S4.1, S4.2, S4.5, S2.5 | 🟡 append-only après S0.2 |
| `docker-compose.yml` | S0.1, S4.1, S6.1 | 🟠 sérialisé |
| `.github/workflows/tests.yml` | S0.3, S0.4 | 🟠 sérialisé |
| `pyproject.toml` | S0.4 | 🟢 exclusif |
| `Dockerfile` | S6.1 | 🟢 exclusif |

### 1.2 Les 3 verrous structurels

**V-1 — `app/main.py` est le goulot du lot.** 6 étapes y touchent (montage de routers,
middlewares, coroutines du pipeline, lifespan) sur un fichier déjà à **661 LOC**
(AUDIT-P4-002, +50 % depuis la baseline). **Mitigation obligatoire, non négociable** :
chaque étape qui touche `main.py` y fait **≤ 10 lignes** (import + câblage) et met toute
sa logique dans un module neuf (`app/db/maintenance.py`, `app/api/route_audit.py`,
`app/utils/rate_limit.py`, `app/api/csrf.py`, `app/utils/job_health.py`). Effet de bord
voulu : le lot ne fait pas grossir `main.py` de plus de ~40 lignes au total, alors qu'il
lui ajoute 6 comportements — c'est aussi une amorce de cliquet sur AUDIT-P4-002.

**V-2 — S1.1 doit précéder toute la vague 3, et la vague 3 doit suivre la vague 2.**
La primitive (`write_with_retry`) doit exister avant qu'on convertisse quoi que ce soit ;
et `sync_worker.py`, `tv_auth.py`, `health_check_worker.py` sont réclamés à la fois par
la conversion (vague 3) et par des étapes de contrat/sécurité (S2.1, S4.2, S4.4). Ordre
imposé : **S1.1 → (S2.1) → S3.1/S3.3 → (S4.2/S4.4)**.

**V-3 — S1.4 désamorce `metrics.py` comme point de conflit.** Toutes les métriques du
lot (unified_path, freshness, downloads, DAV, OMDb, sync Plex, 400 legacy) sont
**déclarées** en une seule étape. Chaque étape ultérieure ne fait qu'**incrémenter**
depuis son propre module. Sans ce découplage, 7 étapes se disputeraient `metrics.py`.

### 1.3 Groupes parallélisables (à lancer en `/app-build` simultané)

```
VAGUE 0   [S0.1] [S0.2] [S0.3] [S0.4]            → 4 en parallèle (fichiers disjoints)
VAGUE 1   [S1.1] [S1.4] puis [S1.2] → [S1.3]     → 2 en parallèle + 2 sérialisés (main.py)
VAGUE 2   [S2.1] [S2.2] [S2.3] [S2.4] [S2.5] [S2.6]   → 6 en parallèle (ZÉRO recouvrement)
VAGUE 3   [S3.1] [S3.2] [S3.3] [S3.4]            → 4 en parallèle (zones disjointes)
VAGUE 4   [S4.4] [S4.5] en parallèle
          [S4.1] → [S4.2] → [S4.3] sérialisés (main.py)
VAGUE 5   [S5.2] [S5.3] [S5.4] en parallèle ; [S5.1] sérialisé (main.py)
VAGUE 6   [S6.1] puis [S6.2]
```

**Le point le plus rentable pour toi** : la **vague 2** est intégralement parallélisable
(6 ICs, zéro collision de fichiers) et la **vague 3** aussi (4 ICs). Ce sont les deux
seuls moments où le pod peut tourner à pleine largeur.

⚠️ **Deux couplages fonctionnels sans conflit de fichier**, à signaler aux ICs :
- S2.2 (`source.py`) et S2.5 (`generator.py`/`storage.py`) modifient tous deux le
  comportement de génération. Fichiers disjoints, mais la QA de génération doit rejouer
  **après les deux** (`tests/test_plex_generator.py` + un dry-run CLI).
- S4.4 (SSRF sur images) touche `storage.py` après S2.5 (refresh policy) : le second
  arrivé rebase.

---

## 2. Découpage en vagues et étapes

Format de chaque étape : **Finding · Fichiers · Contrat · Tests · Pièges §9 · DoD · Rollback**.
DoD minimal commun à **toutes** les étapes (house law + `.claude/skills/house-conventions`) :
`pytest -v` vert (1414+ passed, gate `--cov-fail-under=70`) · `ruff check` vert ·
boot `uvicorn app.main:app` OK · `GET /api/health` **200** · OpenAPI régénérée si l'API
change · `CLAUDE.md` (bandeau + section concernée) mis à jour **dans le même commit**.

---

### VAGUE 0 — Socle d'exploitation (zéro code applicatif)

Objectif : que le déploiement de référence marche et que la CI mesure la bonne chose,
**avant** de toucher au code. Aucune de ces étapes ne peut casser le runtime.

#### S0.1 — `docker-compose.yml` : injecter la configuration (AUDIT-P7-006, S2)

- **Fichiers** : `docker-compose.yml` (+ `docs/` note d'exploitation).
- **Problème** : le bloc `environment:` n'injecte pas `AI_API_KEY`, `ADMIN_USERNAME`/
  `ADMIN_PASSWORD`, `TV_AUTH_ENCRYPTION_KEY`, `XTREAM_ENCRYPTION_KEY`, `CORS_ORIGINS`,
  `OLLAMA_*`, `PLEX_ACCOUNT_TOKEN`, `TMDB_LANGUAGE`, `BACKUP_*`, `DATA_DIR`/`LOG_DIR`,
  `AI_EMBED_*`, `ADULT_*`, `XTREAM_USER_AGENT` (+ ~18 clés). Le `.env` hôte n'est ni copié
  dans l'image (`Dockerfile` ne copie que `app/`) ni monté → `load_dotenv` ne trouve rien
  dans le conteneur. `docker compose up` livré ⇒ **100 % de l'API en 401**, admin/docs 503.
- **Décision** : ajouter **`env_file: .env`** (une ligne, couvre toute la surface présente
  et future) **et conserver** le bloc `environment:` existant pour les clés à **valeur
  fixe conteneur** (`PLEX_LIBRARY_DIR=/app/media`, `DOWNLOAD_DIR=/app/downloads`,
  `APP_PORT`, `TZ`) — `environment:` gagne sur `env_file:`, donc les chemins conteneur
  restent verrouillés même si l'opérateur met un `DOWNLOAD_DIR` hôte dans son `.env`.
  Documenter cette précédence en commentaire (c'est le piège d'exploitation de ce fix).
- **Contrat** : aucun contrat API. Comportement de déploiement uniquement.
- **Tests** : pas de test pytest possible ; **DoD manuelle** = `docker compose config`
  affiche les clés, et un `docker compose up` sur un `.env` minimal (`AI_API_KEY`,
  `ADMIN_PASSWORD`) donne `GET /api/health` 200 **et** `GET /api/media/movies` 200 avec
  la clé. À défaut de Docker sur la machine de dev : `docker compose config` suffit comme
  gate, le smoke complet est une action opérateur.
- **Pièges §9** : aucun code touché. Attention à ne **jamais** commiter un `.env` réel.
- **Rollback** : `git revert`, une ligne.

#### S0.2 — `.env.example` complet (AUDIT-P7-008, S3)

- **Fichiers** : `.env.example`.
- **Manquantes** (vérifié au diff `config.py` ⇆ fichier) : `ADMIN_USERNAME`,
  `ADMIN_PASSWORD`, `PLEX_LIBRARY_DIR` (documenté en commentaire mais non listé),
  `DAV_ACCOUNT_IDS`, `PLEX_CLIENT_IDENTIFIER`, `ADULT_CONTENT_RATING`,
  `ADULT_CATEGORY_KEYWORDS`, `ADULT_CATEGORY_IDS`, `XTREAM_USER_AGENT`. Les 3 secrets de
  sécurité (`TV_AUTH_ENCRYPTION_KEY`, `XTREAM_ENCRYPTION_KEY`, `ADMIN_PASSWORD`) sont
  prioritaires : **leur absence active silencieusement des replis** (dérivation de clé
  depuis `AI_API_KEY` → CR-S04 ; plaintext au repos → CR-S03 ; admin 503).
- **Contrat** : aucun. Documentation.
- **Pièges §9** : jamais de secret réel (le fichier est suivi par git).
- **DoD** : un script de garde `tests/test_env_example_complete.py` — nouveau test qui
  parse les `os.getenv(` de `app/config.py` et assert que chaque clé apparaît dans
  `.env.example` (commentée ou non), avec une allow-list explicite pour les clés
  compose-only. **C'est le test qui empêche la récidive** ; sans lui l'étape se re-dégrade
  au prochain ajout de config.
- **Rollback** : trivial.

#### S0.3 — matrice CI Python 3.12 + 3.13 (AUDIT-P7-005, S3)

- **Fichiers** : `.github/workflows/tests.yml`.
- **Problème** : CI = 3.13 seul (`tests.yml:20,46`) ; l'image livrée est
  `python:3.12-slim` ; l'outillage cible `py312`. **La version livrée n'est jamais testée.**
- **Action** : `strategy.matrix.python-version: ["3.12", "3.13"]` sur le job `pytest`
  (le job `lint` reste sur une seule version, ruff est déterministe).
- **Placement en vague 0 assumé** : si une divergence 3.12/3.13 existe, on la découvre
  **avant** d'empiler 25 étapes dessus. Risque faible (le dev local tourne 3.12.10 avec
  1414 tests verts).
- **DoD** : les deux jobs verts sur `develop`.
- **Rollback** : trivial. Si 3.12 est rouge pour une raison hors périmètre, isoler le
  correctif en une étape dédiée plutôt que de retirer la matrice.

#### S0.4 — `black --check` honnête (AUDIT-P7-004, S3)

- **Fichiers** : `pyproject.toml` et/ou `.github/workflows/tests.yml`.
- **Problème** : `[tool.black] extend-exclude` exclut `^/app/` et `^/tests/` → le gate
  « format check » de la CI **ne vérifie aucun code applicatif**. Un lecteur du YAML croit
  à un gate qui n'existe pas.
- **Décision recommandée** : **retirer l'étape `black --check` de la CI** et laisser
  `ruff check` (déjà réel) comme unique gate de style, en documentant en une ligne dans
  `pyproject.toml` pourquoi black n'est pas appliqué (reformat de masse non souhaité —
  décision CR-C06 déjà prise). Alternative (plus coûteuse) : `ruff format --check` sur un
  périmètre restreint aux fichiers **nouveaux** du lot.
  → **Point d'arbitrage n° 5** (§6.2).
- **Contrat** : aucun.
- **DoD** : la CI ne ment plus sur ce qu'elle vérifie.

---

### VAGUE 1 — Primitives partagées et garde-fous

Objectif : poser une seule fois les briques que les vagues 2-5 consomment, pour supprimer
les conflits en aval. **Aucune de ces étapes ne change un contrat public** (sauf S1.2 qui
peut faire échouer le boot — c'est son but).

#### S1.1 — `write_with_retry` (session fraîche) + `commit_with_retry` honnête (AUDIT-P1-001, S2)

- **Décision** : ADR 0004 §Décision 4. **Aucun call-site n'est touché ici.**
- **Fichiers** : `app/utils/db_retry.py`, `tests/test_db_retry_real_lock.py`,
  `tests/test_db_retry.py`.
- **Contenu** :
  1. Nouvelle primitive `write_with_retry(work, *, session_factory=None, delays, op)` —
     ouvre une session **neuve par tentative**, exécute `work(session)`, commit dedans.
  2. `commit_with_retry` attrape `PendingRollbackError`, `rollback()` d'hygiène, et
     **re-lève l'`OperationalError` d'origine** (`raise original from None`) + `warning`.
     Docstring réécrite : « ne retry PAS en pratique ; utiliser `write_with_retry` ».
  3. `run_with_retry` inchangé (il est correct — c'est son usage same-session qui ne l'est pas).
- ⚠️ **Piège à ne surtout pas commettre** (documenté dans l'ADR) : ajouter
  `await db.rollback()` **avant un retry same-session** est *pire* que le bug — SQLAlchemy
  **expulse** les objets `pending` au rollback, la tentative suivante commiterait zéro
  ligne **sans erreur** (perte d'écriture silencieuse). Cette option est interdite.
- **Tests (à écrire AVANT — zone à couverture trompeuse)** :
  - **Inverser** `TestCommitWithRetrySameSessionBoundary`
    (`tests/test_db_retry_real_lock.py:218-258`) → `TestCommitWithRetryFailsHonestly` :
    assert que l'exception remontée est `OperationalError` « database is locked », **pas**
    `PendingRollbackError`. Le docstring de la classe (qui fige aujourd'hui le défaut
    comme « CURRENT, observed behaviour ») est réécrit pour pointer l'ADR 0004.
  - Nouveau : `write_with_retry` **survit** à un vrai lock WAL tenu 0,35 s (réutiliser le
    harnais `_hold_write_lock`/`_init_wal_db` existant) et écrit bien la ligne.
  - Nouveau : `write_with_retry` compte **N tentatives réelles** (compteur dans `work`).
  - Nouveau : `work` non-lock (`IntegrityError`) remonte immédiatement, sans retry.
- **Pièges §9** : **8** (`db_retry`/WAL — c'est le cœur du sujet) ; **11** (aucun appel
  bloquant introduit).
- **DoD** : les 4 tests ci-dessus verts ; `grep -c commit_with_retry app/` inchangé
  (36) — **aucun call-site converti dans cette étape**.
- **Rollback** : `git revert` isolé, aucune dépendance amont.

#### S1.2 — Assertion de boot sur `app.routes` (AUDIT-P4-001, S2)

- **Fichiers** : `app/api/route_audit.py` *(nouveau)*, `app/main.py` (**≤ 5 lignes** :
  import + appel après le dernier `include_router`), `tests/test_route_auth_assertion.py`
  *(nouveau)*.
- **Contenu** : parcourir `app.routes`, exiger pour tout chemin commençant par `/api/`
  qu'au moins une dépendance connue soit présente dans la chaîne
  (`verify_backend_secret`, `verify_api_key`, `verify_master_key`,
  `verify_admin_basic_auth`), **`RuntimeError` sinon**. Allow-list **explicite et
  courte**, écrite en dur avec justification par entrée :
  `/api/health`, `/api/tv-auth/start`, `/api/tv-auth/status`, `/api/tv-auth/complete`.
  `/dav` et `/admin` sont hors périmètre (pas de préfixe `/api`) ; `/metrics` aussi
  (traité par S4.1).
- **Pourquoi en vague 1 alors que le FINAL-REPORT le classe V2** : c'est un **garde-fou**,
  pas une feature. Le mettre tôt fait que les 25 étapes suivantes bénéficient du filet
  (S4.1 en particulier ajoute une surface). Coût ~20 lignes, gain immédiat.
- **Contrat** : aucun contrat HTTP. **Contrat de boot** : le serveur refuse de démarrer si
  une route `/api/*` n'est pas gardée. C'est le comportement voulu (fail-closed structurel).
- **Tests (AVANT)** :
  - le boot réel passe l'assertion (test d'intégration sur l'app importée) ;
  - une app jouet avec une route `/api/oops` sans garde ⇒ `RuntimeError` avec le chemin
    fautif dans le message ;
  - l'allow-list est **exactement** les 4 chemins publics documentés (test de liste figée,
    pour qu'un élargissement soit un choix explicite et non un glissement).
- **Pièges §9** : **10** (le fail-closed de toute l'API est justement ce qu'on verrouille).
  Attention : ne pas casser le montage `/dav` (pas de `/api`, §2 conventions de montage).
- **DoD** : boot OK ; les 3 tests verts ; `main.py` +≤ 5 LOC.
- **Rollback** : revert du module + de l'appel. Aucun effet de bord.

#### S1.3 — Migration 023 `ANALYZE` + `PRAGMA optimize` de fin de pipeline (AUDIT-P3-001, S2)

- **Gain mesuré par l'audit** : `COUNT(*)` films autorisés **113,5 ms → 0,6 ms (×188)** ;
  page films **117 → 24 ms** ; recherche `LIKE` **116 → 18 ms** ; coût one-shot **196 ms**.
  Meilleur ratio gain/effort de tout l'audit. Cause : `sqlite_stat1` **absent** ⇒ le
  planificateur choisit `ix_media_category_visible` (booléen matchant 90,5 % des lignes)
  et ignore les 20 index créés par la migration 015.
- **Fichiers** : `app/db/maintenance.py` *(nouveau)*, `app/db/migrations.py`
  (`_migration_023_analyze`, **en fin** de `run_migrations()`), `app/main.py`
  (**≤ 4 lignes** : appel après `_rebuild_unified_groups()` dans le pipeline planifié),
  `tests/test_migration_023.py` *(nouveau)*.
- **Contenu** :
  1. `app/db/maintenance.py::run_sqlite_maintenance(engine)` — exécute `ANALYZE` puis
     `PRAGMA optimize`, **jamais fatal** (try/except → `warning`), et **entièrement via
     l'engine async** (aucun appel sqlite3 synchrone : rien à envoyer dans `to_thread`,
     mais si un `sqlite3` direct est utilisé, `asyncio.to_thread` est **obligatoire**,
     piège 11).
  2. Migration 023 = un `ANALYZE` one-shot au boot. **Idempotente par nature** (ANALYZE
     est rejouable à l'infini), **non destructive** (n'écrit que `sqlite_stat1`), ajoutée
     **en fin de chaîne** (piège 6). Chaîne : 001→**023**.
  3. Appel de fin de pipeline pour que les stats suivent la croissance du catalogue.
- **Contrat** : aucun. Perf pure, sortie des requêtes **identique**.
- **Tests (AVANT — zone non couverte)** :
  - `sqlite_master` contient `sqlite_stat1` après `run_migrations()` sur DB fraîche ;
  - `run_migrations()` rejouée deux fois de suite = aucun warning, aucune erreur
    (aligner sur `tests/test_migrations_no_duplicate_warning.py`) ;
  - `run_sqlite_maintenance` sur une DB en lecture seule / verrouillée **ne lève pas**.
- **Pièges §9** : **6** (migration idempotente en fin de chaîne — **c'est la seule
  migration du lot**) ; **8** (`ANALYZE` prend le verrou d'écriture ~196 ms sur 189 Mo :
  l'appel de fin de pipeline se fait **après** `rebuild_all`, quand `_PIPELINE_LOCK` est
  encore tenu, donc hors contention avec le reste du pipeline) ; **11**.
- **needs-approval** : oui — écriture DDL/données au boot sur la base de prod (189 Mo).
  Non destructive, mais c'est une écriture au démarrage : décision opérateur.
- **DoD** : boot sur une copie de la vraie base ; `EXPLAIN QUERY PLAN` du COUNT films
  montre `ix_media_stream_validation` (covering) et non `ix_media_category_visible`.
- **Rollback** : revert. Pour annuler les stats côté base : `DELETE FROM sqlite_stat1;`
  (documenté en note de migration — non requis, les stats sont inoffensives).

#### S1.4 — Déclaration centrale de la surface métrique + zéro-init (AUDIT-P8-001 volet a, S2)

- **Fichiers** : `app/utils/metrics.py`, `tests/test_metrics_registry.py` *(nouveau)*.
- **Rôle stratégique** : cette étape **existe pour désamorcer un conflit** (§1.2 V-3).
  Elle déclare *toutes* les métriques que les vagues 2-5 vont incrémenter, et **ne branche
  rien**. Chaque étape ultérieure importe et incrémente depuis son propre module.
- **Contenu** :
  1. **Zéro-init des labels énumérables** des 5 métriques existantes : appeler
     `.labels(...)` au chargement pour toutes les combinaisons **fermées**
     (`tmdb_requests_total{kind,result}`, `tmdb_match_total{media_type,result}`,
     `enrichment_queue_size{status}`). Les labels **ouverts** (`account_id`) ne peuvent
     pas être zéro-initialisés — c'est justement pourquoi les gauges de fraîcheur de S5.1
     sont **non labellisées**. Effet : `rate(...[5m])` renvoie `0` et non `no data`, donc
     l'alerting par absence redevient possible.
  2. Déclaration (sans consommateur) de : `plexhub_unified_path_total{path}` (S2.4),
     `plexhub_media_episodes_missing_server_id_total` (S2.6),
     `plexhub_pipeline_last_success_timestamp_seconds{job}` + `plexhub_is_master` (S5.1),
     `plexhub_download_jobs{state}` / `plexhub_download_bytes_total` /
     `plexhub_download_failures_total{reason}` (S5.2),
     `plexhub_dav_throttle_rejections_total` / `plexhub_dav_permit_wait_seconds` /
     `plexhub_dav_relayed_bytes_total` (S5.3),
     `plexhub_omdb_requests_total{result}` / `plexhub_plex_sync_total{result}` (S5.4).
- **Contrat** : `/metrics` gagne des séries à zéro. Additif — **aucun** consommateur ne
  casse. (L'auth arrive en S4.1, pas ici.)
- **Tests (AVANT)** : un test qui scrape le registry et assert que chaque métrique
  énumérable expose ses N séries à 0 **avant tout événement** — c'est la régression
  exacte pointée par AUDIT-P8-001.
- **Pièges §9** : **10** (ne rien ajouter qui fuite plus que `account_id` ne fuite déjà ;
  aucun label ne doit porter un titre de média, une URL, un `rating_key` ou un secret).
- **DoD** : `curl /metrics` sur une instance fraîche renvoie des `0`, pas seulement des
  `# HELP`.
- **Rollback** : revert ; aucune dépendance (les consommateurs n'existent pas encore).

---

### VAGUE 2 — Contrats et correctifs de fond (**6 étapes, 100 % parallèles**)

Vérifié : recouvrement de fichiers **nul** entre S2.1…S2.6. C'est la fenêtre de
parallélisation maximale du lot.

#### S2.1 — Handshake `jobId` bout-en-bout + 404 (AUDIT-P5-001 S2, P5-008 S3, P6-005 S3)

- **Décision** : ADR 0004 §Décision 2.
- **Fichiers** : `app/services/job_registry.py` *(nouveau)*, `app/api/sync.py`,
  `app/workers/sync_worker.py` (extraction du tracker `_sync_jobs`/`_record_sync_job`
  `:30-51` + paramètre `job_id` optionnel sur `sync_account` `:1090-1101`),
  `app/models/schemas.py` (`SyncStatusResponse` additive), `tests/test_sync_jobs.py`
  *(nouveau)*.
- **Contenu** :
  1. Registre partagé (in-memory borné, éviction FIFO — sémantique actuelle conservée).
  2. Le **router** crée le `jobId` et le passe au travail de fond ; le worker met à jour
     par cette clé. Les 5 triggers (`/xtream`, `/xtream/all`, `/enrichment`,
     `/validate-streams`, `/full-pipeline`) enregistrent.
  3. `/full-pipeline` enregistre sa **phase** (`sync|enrichment|validation|generation|
     snapshot`) → résout P6-005 sans nouvel endpoint.
  4. `GET /status/{job_id}` → **404** sur id inconnu.
- **Contrat** :
  - **MODIFIÉ** : `GET /api/sync/status/{job_id}` — 200 `unknown` → **404** sur inconnu.
    Champs additifs `phase`, `startedAt`, `finishedAt`, `error` (camelCase). `status`
    garde son vocabulaire actuel.
  - **PRÉSERVÉ** : forme `{"jobId": "…"}` des 5 triggers, code **202**,
    `GET /api/sync/jobs` (schéma `SyncJobListResponse` inchangé, `jobId` camelCase déjà
    livré cf. AUDIT-P5-005).
  - **Impact Android** : nul en pratique (le handshake est mort pour 100 % des ids
    depuis toujours — c'est la justification explicite de l'utilisateur pour poser un
    contrat propre sans dépréciation). À tracer au board pour vérification côté
    `PlexHubTV`.
  - **Impact OpenAPI** : `SyncStatusResponse` + réponse 404 documentée.
- **Tests (AVANT — le trou exact qui a laissé passer le bug)** :
  - **Test de bout en bout trigger → status** sur les **5** triggers : POST → récupérer
    `jobId` de la réponse → `GET /status/{jobId}` → statut **connu** (pas `unknown`,
    pas 404). Workers mockés pour ne rien exécuter réellement.
  - `GET /status/inexistant` → **404**.
  - `GET /api/sync/jobs` liste bien un job enregistré par un trigger non-`sync_account`.
  - Éviction FIFO : au-delà du cap, l'entrée la plus ancienne disparaît (et son `GET`
    devient 404, cohérent).
- **Pièges §9** : **7** (les jobs restent process-local : un `GET` depuis un worker
  non-master répond 404 — comportement voulu et documenté, dette AUDIT-P1-008 assumée) ;
  **17a** (le déclenchement marche sur tout worker, seul le master draine — ne pas
  introduire de master-gate sur ces endpoints par inadvertance).
- **needs-approval** : oui (changement observable : 404).
- **DoD** : les 5 tests trigger→status verts ; `/openapi.json` régénérée ; note board pour
  la vérification `PlexHubTV`.
- **Rollback** : revert. Le registre étant additif, le revert ramène l'ancien
  comportement sans état résiduel.

#### S2.2 — Agrégation `DatabaseSource` off-loop (AUDIT-P3-003, S2)

- **Mesuré** : `aggregate_movies` = **239 ms de CPU pur** (12 331 lignes → 10 638 groupes) ;
  `aggregate_series` = **504 ms** (2 873 shows + 77 781 épisodes). Soit **~750 ms de gel de
  boucle** à chaque génération **et** à chaque rebuild d'arbre DAV — ce dernier étant sur
  le **chemin de requête** `/dav` (premier hit après TTL 60 min ou invalidation).
- **Fichiers** : `app/plex_generator/source.py` (`get_movies` ~`:132`, `get_series`
  ~`:202`), `tests/test_plex_generator.py` (ou `tests/test_plex_source_offloop.py`).
- **Contenu** : envelopper `aggregate_movies(rows)` et `aggregate_series(shows, episodes)`
  dans `await asyncio.to_thread(...)` — **exactement** le patron déjà appliqué par
  `media_service.py:329,406,556` et `unified_group_service.py:50` sur les **mêmes**
  fonctions. Les fonctions sont pures sur des lignes déjà chargées : aucun accès DB dans
  le thread (invariant à tester). Étendre si possible aux boucles de construction
  `PlexMovie`/`PlexSeries` + `_build_versions` (`source.py:133,207`) — mais **seulement
  si** elles n'accèdent pas à la session (à vérifier : `_build_versions` reçoit `accounts`
  déjà chargés).
- **Contrat** : **strictement inchangé**. La sortie doit être **octet-identique** — c'est
  le critère d'acceptation.
- **Tests (AVANT)** :
  - test de **parité** : sortie de `get_movies()`/`get_series()` identique avant/après
    (fixture de catalogue multi-comptes, comparaison structurelle) ;
  - test que la boucle **respire** : mesurer que `get_movies()` n'occupe pas la boucle
    plus de N ms d'affilée (patron : lancer une tâche témoin qui s'incrémente toutes les
    10 ms et vérifier qu'elle progresse pendant l'appel).
- **Pièges §9** : **11** (c'est *le* piège de cette étape — `asyncio.to_thread`
  obligatoire pour tout CPU/I-O bloquant) ; **8** (ne **jamais** faire passer une
  `AsyncSession` dans le thread : les objets ORM sont déjà matérialisés, mais un accès
  lazy déclencherait une I/O DB hors boucle → interdiction explicite à tester) ; **18d/g**
  (l'arbre DAV consomme ce chemin : la génération ne doit pas se dégrader).
- **DoD** : parité prouvée ; test de respiration vert ; dry-run CLI `python -m app.cli
  generate --all --dry-run` produit le même rapport.
- **Rollback** : revert, une poignée de lignes.

#### S2.3 — Convergence `display_rating` + backfill (AUDIT-P4-005, S3)

- **Décision** : ADR 0004 §Décision 1.
- **Fichiers** : `app/services/nfo_import_service.py` (~`:531-541`),
  `app/utils/unification.py` (`calculate_display_rating` `:40` — conservé, docstring
  restreinte à l'usage sync-only), `tests/test_nfo_import.py`,
  `tests/test_rating_blend.py`.
- **Contenu** : `nfo_import_service` recalcule `display_rating` via
  `rating_blend.blend_rating(new_imdb, new_tmdb)` sur les valeurs post-écriture, au lieu
  de `calculate_display_rating(scraped, audience, rating)`. `sync_worker` garde le
  COALESCE (lignes brutes sans note IMDb/TMDB : le blend y renvoie `None`).
- **Contrat** :
  - Pas de changement de schéma ni de type. **Changement de VALEUR** visible par l'app
    (`MediaResponse.displayRating`) et par le tri (`ix_media_type_rating`) sur les lignes
    NFO portant les deux notes : best-pick IMDb (ex. 8.8) → moyenne (ex. 8.45).
  - `scraped_rating` / `audience_rating` / `imdb_rating` / `tmdb_rating` **inchangés**.
- **Tests (AVANT)** :
  - **test de non-oscillation** (le test-clé) : import NFO d'une ligne à deux notes, puis
    exécution de `recompute_display_rating_stmt()` → **aucune ligne réellement modifiée**.
    Aujourd'hui ce test est rouge ; il devient le garde-fou de l'invariant.
  - grille de parité `blend_rating` ⇆ écriture NFO (réutiliser la grille existante de
    `tests/test_rating_blend.py`) ;
  - une ligne NFO **sans** note IMDb/TMDB garde son `display_rating` (pas de régression
    sur le cas COALESCE légitime).
- **Backfill** (décidé, dans ce lot, **après** que S2.3 soit mergée) : `POST
  /api/admin/enrichment/omdb-backfill` avec `recomputeDisplayRating=true` et une Phase A
  vide — **zéro nouveau code**. Prérequis opérateur : backup DB, master idle.
- **Pièges §9** : **8** (le backfill est un `UPDATE` de masse sur ~102 k lignes sous WAL :
  ne **pas** le lancer pendant le pipeline — le `_PIPELINE_LOCK` ne le couvre pas, c'est
  une contrainte opérateur) ; **6** (aucune migration : les colonnes existent) ; **15**
  (ne rien changer au tagging adulte au passage).
- **needs-approval** : oui, **deux fois** — (a) le changement de formule (valeur visible
  par l'app), (b) le backfill de masse.
- **DoD** : test de non-oscillation vert ; `CLAUDE.md` §5.2 réécrite (la phrase « deux
  formules coexistent — by design » devient fausse) ; ADR 0003 annoté d'un renvoi vers 0004.
- **Rollback** : revert du code = retour à deux formules. **Le backfill n'est pas
  révocable** (valeurs écrasées) — d'où l'exigence de backup préalable. Le recalcul inverse
  serait possible depuis `scraped_rating`/`audience_rating` qui, eux, sont intacts.

#### S2.4 — Métrique `plexhub_unified_path{snapshot|live}` (AUDIT-P3-002 volet visibilité, S2)

- **Problème** : si `unified_group_service.rebuild_all` échoue pour un type (exception
  catchée et seulement loggée, `unified_group_service.py:129-133`) ou si le pipeline n'a
  pas tourné, `_unified_list_from_snapshot` renvoie `None` (`media_service.py:368-369`) et
  **chaque** requête de browse repaie un full-load (**205,9 ms** DB + **239 ms** CPU
  mesurés) jusqu'au pipeline suivant — **6 h**. Aucune métrique ne distingue les deux
  chemins : la dégradation est totalement silencieuse.
- **Fichiers** : `app/services/media_service.py` uniquement (métrique déjà déclarée en
  S1.4), `tests/test_media_unified_path_metric.py` *(nouveau)*.
- **Contenu** : incrémenter `plexhub_unified_path_total{path="snapshot"|"live"}` aux deux
  points de sortie de `get_unified_list` (`media_service.py:274-283`). Distinguer si
  possible `live_filtered` (requête search/genre/year — normal) de `live_fallback`
  (snapshot vide/absent — **anormal**) : c'est le second qui doit déclencher une alerte.
- **Contrat** : aucun changement d'API. Visibilité pure.
- **Tests (AVANT)** : snapshot peuplé + requête non filtrée ⇒ `path="snapshot"` ;
  snapshot vide ⇒ `path="live_fallback"` ; requête avec `search` ⇒ `path="live_filtered"`.
- **Pièges §9** : **10** (aucun label porteur de donnée utilisateur : `path` est un
  énuméré fermé de 3 valeurs, zéro-initialisé en S1.4).
- **DoD** : les 3 séries visibles sur `/metrics` après un scénario de test.
- **Rollback** : trivial.

#### S2.5 — Politique de refresh des NFO/posters générés (AUDIT-P6-006, S3)

- **Problème** : `write_file` et `download_image` **retournent immédiatement si le fichier
  existe** (`app/plex_generator/storage.py:112-116,119-122`, commentaire « Preserve
  existing file (e.g. enriched by Tiny Media Manager) »). Conséquence : **les NFO et
  posters générés ne sont JAMAIS rafraîchis** — alors que les métadonnées durables
  évoluent désormais souvent (notes OMDb du lot dual-provider, ids corrigés par
  `validate_id_consistency`, `display_rating` blendé, et **maintenant le backfill de
  S2.3**). Finding perdu au changement de lignée (ex-CR-F15), ré-immatriculé par l'audit
  v1. Conséquence admise opérationnellement (« l'opérateur doit vider les NFO générés »)
  mais jamais tracée.
- **Fichiers** : `app/plex_generator/storage.py`, `app/plex_generator/generator.py`,
  `app/api/plex.py` (paramètre additif), `app/config.py` (knob), `app/cli.py` (flag),
  `app/services/plex_generation_service.py`, `tests/test_plex_generator.py`.
- **Contenu proposé** (à arbitrer, §6.2 point 3) :
  1. `LocalStorage.write_file(..., force: bool = False)` — `force=True` réécrit
     **atomiquement**, sinon comportement actuel (préserver).
  2. Le générateur ne demande `force=True` que pour les NFO, **jamais** pour les images par
     défaut (bande passante + les posters TMM sont l'usage principal de la préservation).
  3. Déclencheur explicite : `PLEX_FORCE_REFRESH_METADATA` (défaut `false`) +
     `forceRefreshMetadata` (body additif de `POST /api/plex/generate`) +
     `--force-refresh` sur la CLI. **Jamais activé automatiquement.**
- **Contrat** :
  - **MODIFIÉ, opt-in** : `POST /api/plex/generate` gagne un champ **additif optionnel**
    (`forceRefreshMetadata`, défaut `false`) → OpenAPI change, aucun client existant ne
    casse.
  - **Comportement disque** : quand le flag est actif, des fichiers générés existants sont
    **écrasés**. C'est la seule opération quasi-destructive du lot.
- **Tests (AVANT — zone peu couverte)** :
  - `force=False` (défaut) : un `.nfo` existant est **préservé** — non-régression du
    contrat TMM ;
  - `force=True` : le `.nfo` est réécrit, l'écriture reste **atomique** (`_atomic_write_text`) ;
  - `force=True` **ne touche pas** les images (sauf si le sous-flag image est demandé) ;
  - le rapport `SyncReport` compte correctement `updated` vs `unchanged` dans les deux modes.
- **Pièges §9** : **11** (les écritures de génération sont déjà offloadées `generator.py:232`
  — ne pas réintroduire d'I/O sur la boucle) ; **15** (le préfixe `[XXX] ` du `<title>`
  NFO doit être re-posé à l'identique lors d'une réécriture — un refresh qui perd le
  préfixe serait une régression silencieuse : **test dédié**) ; **17d/F-007** (aucun
  chemin d'écriture ne vient du client : `outputDir` reste confiné `plex.py:35-73`).
- **needs-approval** : oui (écrasement de fichiers générés sur le disque de l'opérateur).
- **DoD** : les 4 tests verts ; runbook mis à jour ; `CLAUDE.md` §5.4 (b) complétée.
- **Rollback** : revert. Les fichiers déjà écrasés ne reviennent pas — d'où le défaut
  `false` et l'opt-in explicite.

#### S2.6 — Log WARN + compteur sur le 400 `server_id` (AUDIT-P5-004, S2)

- **Problème** : `GET /api/media/episodes` renvoie **400** si `server_id` est absent
  (`app/api/media.py:192-222`). Le correctif est **justifié** (collision cross-comptes des
  `parent_rating_key`, bug prod MAO/Treadstone) mais a été livré **sans capacité de
  détection** : impossible de savoir si une version legacy de `PlexHubTV` tape encore
  l'endpoint sans `server_id`.
- **Fichiers** : `app/api/media.py` (2-3 lignes dans le garde existant),
  `tests/test_media_episodes_guard.py` *(nouveau ou extension)*.
- **Contenu** : sur le chemin du 400, un `logger.warning` dédié (avec le
  `parent_rating_key` demandé et l'User-Agent — **jamais** de clé ni de credential) +
  incrément de `plexhub_media_episodes_missing_server_id_total` (déclarée en S1.4).
- **Contrat** : **inchangé** (toujours 400, même `detail`). Pure télémétrie.
- **Tests (AVANT)** : le 400 est conservé mot pour mot ; le compteur s'incrémente ; aucun
  secret dans le message loggé.
- **Pièges §9** : **10** (rien de sensible dans le log ni dans le label).
- **DoD** : après déploiement, une semaine d'observation du compteur ⇒ décision
  d'exploitation (garder le 400 dur, ou ajouter un repli). À tracer au board.
- **Rollback** : trivial.

---

### VAGUE 3 — Migration des writers vers `write_with_retry` (**4 étapes parallèles**)

Prérequis : **S1.1 mergée** ; **S2.1 mergée** (elle touche `sync_worker.py`).
Chaque étape convertit une zone, avec un test de **vrai lock WAL** par zone.
Patron de conversion (ADR 0004 §Décision 4) :

```
# avant
db.add(obj); await commit_with_retry(db)
# après
async def _work(session):
    session.add(build_obj())     # reconstruit à chaque tentative — JAMAIS capturé
    return ...
await write_with_retry(_work, op="<module>.<action>")
```

**Règle de non-conversion** : un site qui écrit sous une session **déjà partagée avec une
lecture dont dépend l'écriture** (lecture-modification-écriture) ne se convertit pas
mécaniquement — il faut déplacer la lecture **dans** le `work`. Un site pour lequel c'est
trop invasif reste sur `commit_with_retry` (désormais honnête) **avec un commentaire
justifiant explicitement le choix**. Aucun site n'est laissé sans décision écrite.

#### S3.1 — Zone workers (~23 sites)

- **Fichiers** : `app/workers/sync_worker.py` (9), `app/workers/enrichment_backfill_worker.py`
  (6 — déjà partiellement en sessions courtes, vérifier avant de toucher),
  `app/workers/health_check_worker.py` (4), `app/workers/enrichment_worker.py` (4).
- **Priorité maximale** : ce sont les writers réellement concurrents (le scénario de
  contention cité par `database.py:13-19` est *validation + sync + génération simultanés*).
- **Bonus opportuniste, à ne PAS faire ici** : AUDIT-P1-004 (`worker_session_factory`
  adopté par 1 worker sur 6) et AUDIT-P1-005 (session unique multi-heure de
  `enrichment_worker.run()`) sont **hors périmètre décidé** — les signaler au board, ne
  pas les glisser dans cette étape.
- **Tests (AVANT)** : un test de vrai lock WAL par worker converti (harnais
  `tests/test_db_retry_real_lock.py`), prouvant que l'écriture **aboutit** malgré un
  verrou tenu 0,35 s.
- **Pièges §9** : **8** (cœur du sujet) ; **7** (les workers master-only le restent) ;
  **17a** (`download_worker` n'est pas concerné — il utilise déjà le bon patron).

#### S3.2 — Zone services (~7 sites)

- **Fichiers** : `app/services/category_service.py` (4), `app/services/account_service.py` (2),
  `app/services/live_service.py` (1).
- **Attention** : `category_service.update_media_adult_flags` (`:410`) est appelée depuis
  `sync_worker` (`:1330`) et doit rester **idempotente et rétroactive** (piège 15).
- **Tests (AVANT)** : lock réel + **idempotence** du flag adulte après retry (un `work`
  rejoué ne doit pas doubler d'effet).
- **Pièges §9** : **8**, **15**.

#### S3.3 — Zone routers request-path (~17 sites)

- **Fichiers** : `app/api/tv_auth.py` (6), `app/api/accounts.py` (4), `app/api/ai.py` (3),
  `app/api/live.py` (2), `app/api/categories.py` (2).
- **Difficulté spécifique** : ces handlers reçoivent une session par `Depends(get_db)`,
  qui **commit implicitement après le `yield`** (`app/db/database.py:99-107`, dette
  AUDIT-P1-006). Une conversion en session fraîche laisse le commit implicite de `get_db`
  s'exécuter sur une session vide — inoffensif mais à vérifier explicitement.
- **Cas délicat `tv_auth.start`** : la boucle de 5 tentatives sur collision `user_code`
  (`tv_auth.py:216-234`) mélange déjà retry applicatif et `IntegrityError`. Le `work`
  converti doit **régénérer un `user_code`** à chaque tentative — sinon un retry sur lock
  rejouerait le même code et transformerait un lock en `IntegrityError`. **Test dédié
  obligatoire.**
- **Contrat** : aucun changement HTTP (mêmes codes, mêmes corps).
- **Tests (AVANT)** : lock réel par router + non-régression des codes (`tv_auth` 201/404/
  409/410/503 — cf. AUDIT-P6-003 qui les a vérifiés sains).
- **Pièges §9** : **8** ; **2** (ne pas altérer les motifs 503 de `/api/ai`) ;
  **10** (fail-closed inchangé).

#### S3.4 — Zone CLI / scripts (~5 sites) — **ou décision de non-conversion**

- **Fichiers** : `app/cli.py` (3), `app/scripts/validate_id_consistency.py` (2).
- **Analyse** : ces chemins sont **mono-process, one-shot, lancés par un opérateur**. La
  contention y est possible (le serveur tourne peut-être) mais l'échec est immédiatement
  visible et rejouable à la main.
- **Décision par défaut recommandée** : **ne pas convertir**, ajouter un commentaire
  renvoyant à l'ADR 0004 expliquant pourquoi (`busy_timeout=60 s` + rejouabilité manuelle
  suffisent). Cela ferme le sujet par écrit plutôt que de laisser 5 sites orphelins.
  → **Point d'arbitrage n° 4** (§6.2).
- **DoD si non-conversion** : `grep commit_with_retry app/` ne remonte plus que des sites
  **commentés-justifiés** ; test de garde qui l'assert (grep-based, patron déjà utilisé
  dans le repo pour interdire `==` sur les secrets, cf. `deps.py` docstring).

---

### VAGUE 4 — Hardening sécurité

⚠️ **Trois de ces cinq étapes cassent quelque chose en exploitation.** Les ruptures sont
**décidées et assumées** par l'utilisateur ; le devoir du plan est de les rendre
**procédurales** (§7 notes de migration), pas de les éviter.

#### S4.1 — Auth sur `/metrics` (AUDIT-P2-001 / P8-003 / CR-S02, S2)

- **Décision** : ADR 0004 §Décision 3.
- **Fichiers** : `app/api/deps.py` (`verify_metrics_basic_auth`), `app/utils/metrics.py`
  (`setup_instrumentator` accepte une dépendance), `app/main.py` (**≤ 3 lignes**),
  `app/config.py` (`METRICS_USERNAME`/`METRICS_PASSWORD`/`METRICS_PUBLIC`),
  `docker-compose.yml`, `.env.example`, `tests/test_metrics_auth.py` *(nouveau)*.
- **Contenu** : Basic Auth **dédiée** (Prometheus sait faire `basic_auth:`, pas de header
  custom — même raisonnement que `/dav` pour rclone), comparaison temps-constant
  user **et** password (copie conforme de `verify_admin_basic_auth` `deps.py:110-142`),
  **503 fail-closed** si password vide, **sauf** `METRICS_PUBLIC=true` (escape hatch
  explicite + `WARNING` au boot).
- **Contrat** : **`/metrics` passe de 200 public à 401 sans identifiants.** Rupture
  d'exploitation assumée (§7.1).
- **Tests (AVANT)** : `/metrics` sans auth → 401 ; avec les bons identifiants → 200 et
  contenu Prometheus ; `METRICS_PASSWORD` vide → 503 ; `METRICS_PUBLIC=true` → 200 + log
  de warning ; comparaison temps-constant (test grep interdisant `==`, patron maison).
- **Pièges §9** : **10** (c'est la dernière surface non fail-closed) ; ne pas retirer
  `excluded_handlers=["/metrics"]`.
- **needs-approval** : **oui** (rupture observable).
- **DoD** : `curl /metrics` → 401 ; procédure opérateur écrite (§7.1).

#### S4.2 — Rate-limit + cap de sessions `pending` (AUDIT-P2-004 / CR-S05, S2)

- **Problème** : `POST /api/tv-auth/start` **non authentifié** insère une ligne par appel
  (`tv_auth.py:183-249`) ; la purge opportuniste ne vise que les sessions expirées depuis
  > 1 h (`_CLEANUP_GRACE_MS`) ⇒ **insertion non bornée** par un anonyme du tunnel (flood
  DB + write-lock SQLite). Basic Auth `/admin`, `/docs`, `/dav` et `X-API-Key` : aucun
  throttle, aucun lockout.
- **Fichiers** : `app/utils/rate_limit.py` *(nouveau)*, `app/main.py` (**≤ 5 lignes** de
  câblage middleware), `app/api/tv_auth.py` (cap `pending`), `app/config.py`,
  `tests/test_rate_limit.py` *(nouveau)*.
- **Contenu** :
  1. Middleware limiteur **in-process** (le déploiement est mono-process : le Dockerfile
     lance un seul uvicorn — cf. piège 18c), fenêtre glissante par IP
     (`cf-connecting-ip` puis `x-forwarded-for` puis `request.client.host`, même
     résolution que `deps._client_ip:34-41`), avec des budgets **différenciés** :
     surfaces non authentifiées (`/api/tv-auth/start`) strictes, surfaces authentifiées
     larges, `/api/health` et `/dav` **exemptés** (rclone fait beaucoup de requêtes
     légitimes ; le throttle DAV existe déjà par compte).
  2. **Cap de sessions `pending`** dans `tv_auth.start` : global **et** par IP ; au-delà →
     **429**. C'est la vraie parade au flood DB (le limiteur seul ne borne pas la table).
- **Contrat** : **nouveau code 429** sur `/api/tv-auth/start` (et potentiellement ailleurs).
  L'app Android doit le tolérer — à tracer au board `PlexHubTV`. `Retry-After` renseigné.
- **Tests (AVANT)** : N+1 appels à `/start` depuis la même IP → 429 + `Retry-After` ;
  cap `pending` atteint → 429 même depuis une IP neuve ; `/api/health` et `/dav` jamais
  limités ; une IP différente n'est pas pénalisée ; le compteur se réinitialise.
- **Pièges §9** : **8** (le cap réduit précisément la pression write-lock) ; **10** ;
  **18b** (`/dav` n'a toujours ni lockout ni rate-limit **par design** : l'exclusion
  ingress reste le prérequis — ne pas laisser croire que ce fix couvre `/dav`).
- **needs-approval** : **oui** (nouveau code d'erreur observable).
- **Rollback** : revert du câblage middleware ⇒ retour au comportement actuel.

#### S4.3 — CSRF `Sec-Fetch-Site` sur POST `/admin*` (AUDIT-P2-005 / CR-S07, S3)

- **Problème** : `grep csrf app/` = **0**. Les 4 routers admin acceptent des POST de
  mutation (enqueue/cancel/retry downloads, sync Plex, refresh catégories) protégés
  uniquement par Basic Auth — que le navigateur **rejoue automatiquement** en cross-site.
- **Fichiers** : `app/api/csrf.py` *(nouveau — middleware ou dépendance)*, `app/main.py`
  (**≤ 4 lignes**), `tests/test_admin_csrf.py` *(nouveau)*.
- **Contenu** : sur toute méthode non-sûre (`POST`/`PUT`/`DELETE`) dont le chemin commence
  par `/admin`, rejeter (**403**) si `Sec-Fetch-Site` vaut `cross-site`. **Accepter**
  `same-origin`, `same-site`, `none` **et l'absence du header** — c'est la nuance qui
  évite de casser `curl`/scripts d'automation (aucun navigateur moderne n'omet le header ;
  seuls les clients non-navigateurs le font, et ceux-là ne sont pas la menace CSRF).
  Zéro template à toucher, compatible HTMX.
- **Contrat** : **MODIFIÉ** — les POST `/admin*` initiés cross-site échouent en 403. Les
  parcours navigateur légitimes et l'automation en ligne de commande sont inchangés.
- **Tests (AVANT)** : `Sec-Fetch-Site: cross-site` → 403 ; `same-origin` → passe ;
  header **absent** → passe ; `GET /admin/*` jamais bloqué ; les 4 routers admin couverts
  (`admin`, `admin_downloads`, `admin_plex_downloads`, `admin_unified_downloads`).
- **Pièges §9** : **10** ; ne pas toucher aux formulaires HTMX (les bugs HTMX documentés
  en §5.10 montrent la fragilité de cette UI — **aucune modification de template**).
- **needs-approval** : oui (changement observable, faible impact).

#### S4.4 — Vetting SSRF sur images + health-check (AUDIT-P2-008 / CR-S08, S3)

- **Problème** : `assert_public_redirect_host` (résolution DNS → rejet loopback/RFC1918/
  link-local/metadata, `download_service.py:286-330`) est appliqué aux downloads
  (`:403-421`) et au relay DAV (`relay.py:270-283`) ✔ — mais **pas** aux téléchargements
  de posters/fanarts (`plex_generator/storage.py:55`, `follow_redirects=True`, URLs
  fournies par le provider/TMDB) ni au health-check HEAD/Range-GET
  (`health_check_worker.py:183,213`, URLs dérivées du `base_url` du compte).
- **Fichiers** : `app/utils/ssrf.py` *(nouveau — extraction de l'helper depuis
  `download_service`, ré-export pour compatibilité)*, `app/services/download_service.py`
  (délègue), `app/dav/relay.py` (import), `app/plex_generator/storage.py`,
  `app/workers/health_check_worker.py`, `tests/test_ssrf_vetting.py`.
- **Pourquoi l'extraction** : `app/dav/` importe déjà `download_service` **uniquement**
  pour cette garde (AUDIT-P4-007 : cycle `services ⇄ dav` tenu par import différé).
  Déplacer la garde dans `utils/` **casse ce cycle** au passage — gain d'architecture
  gratuit. `download_service.assert_public_redirect_host` reste exporté (alias) pour ne
  rien casser.
- **Attention `storage.py`** : le client image est **synchrone** et vit dans un
  `ThreadPoolExecutor` (`storage.py:36-60`). Le vetting doit donc être **synchrone** dans
  ce contexte (résolution DNS bloquante — acceptable **dans le thread pool**, jamais sur
  la boucle : piège 11).
- **Contrat** : une URL d'image ou de flux résolvant vers une adresse privée est
  désormais **refusée** (image non téléchargée → `warning`, comportement déjà toléré par
  `download_image` qui retourne `False` ; flux marqué en échec). Comportement observable
  seulement pour un provider malveillant ou un déploiement à panel LAN → **à vérifier** :
  un opérateur avec un panel Xtream sur son propre LAN verrait ses flux rejetés.
  → **Point d'arbitrage n° 2** (§6.2) : prévoir une allow-list
  (`SSRF_ALLOW_PRIVATE_HOSTS`) pour ce cas légitime.
- **Tests (AVANT)** : URL vers `127.0.0.1`/`10.x`/`169.254.169.254` rejetée sur les deux
  nouveaux chemins ; URL publique acceptée ; le caveat DNS-rebinding déjà acté
  (`download_service.py:295-299`) reste documenté ; allow-list respectée si retenue.
- **Pièges §9** : **11** (résolution DNS bloquante ⇒ thread pool pour `storage`,
  `to_thread`/client async pour le worker) ; **17c** (les URLs Xtream restent
  non loggées — le message d'erreur du vetting ne doit **jamais** contenir l'URL, cf.
  `download_service.py:369` `raise … from None`).

#### S4.5 — Séparation de domaine de la clé Fernet tv-auth (AUDIT-P2-003 / CR-S04, S3)

- **Problème** : `payload_crypto.get_fernet` dérive `sha256(AI_API_KEY)` **directement**
  (`app/utils/payload_crypto.py:42-46`), alors que `crypto_fields` ajoute, lui, un
  `_KEY_DERIVATION_CONTEXT` (`crypto_fields.py:68-71`) **précisément pour ne pas
  dupliquer cette clé**. Le secret bearer sert donc de KEK telle quelle pour les payloads
  d'appairage (qui contiennent des tokens Plex).
- **Fichiers** : `app/utils/payload_crypto.py`, `.env.example`,
  `tests/test_payload_crypto.py`.
- **Contenu** : introduire `_KEY_DERIVATION_CONTEXT = b"plexhub.tv_auth_payload.v1:"`,
  strictement symétrique de `crypto_fields`. `TV_AUTH_ENCRYPTION_KEY` explicite reste
  prioritaire et **inchangée** (les déploiements qui l'ont configurée ne subissent
  **rien**).
- **Contrat** : **les sessions d'appairage en cours deviennent indéchiffrables** pour les
  déploiements en dérivation implicite. Fenêtre de casse = `TV_AUTH_TTL_SECONDS`
  (**900 s** par défaut) ; le code renvoie déjà **503** proprement sur payload
  indéchiffrable (`tv_auth.py:344-348`), donc pas de 500. Rupture assumée par
  l'utilisateur.
- **Tests (AVANT)** : la clé dérivée est **différente** de celle de `crypto_fields` pour
  le même `AI_API_KEY` (test de non-collision — c'est l'invariant du fix) ;
  `TV_AUTH_ENCRYPTION_KEY` explicite l'emporte toujours ; un token de l'ancienne
  dérivation produit un **503**, pas un 500.
- **Pièges §9** : **10** (aucun secret loggé, y compris en cas d'échec de déchiffrement).
- **needs-approval** : **oui** (invalidation des appairages en vol).

---

### VAGUE 5 — Observabilité (**3 étapes parallèles + 1 sérialisée**)

Toutes les métriques sont **déjà déclarées** (S1.4) : ces étapes ne font que brancher.

#### S5.1 — Fraîcheur des jobs + `isMaster` (AUDIT-P8-001 volet b, P8-005)

- **Problème** : aucun signal de vie du pipeline autre que le log. Un master **figé**
  (boucle affamée, deadlock, task pendue) est indétectable : le healthcheck compose ne
  teste que `GET /api/health`, qui répondra tant que la boucle respire un peu. Un pipeline
  planifié mort peut passer inaperçu **6 h × N**. Aggravant AUDIT-P1-003 : un `OSError`
  non-lock à l'élection master (`main.py:274-285`) met **tout** le cluster en esclave, avec
  pour seul signal une ligne INFO « Slave — Passive mode ».
- **Fichiers** : `app/utils/job_health.py` *(nouveau — helper `mark_job_success(name)`)*,
  `app/main.py` (**≤ 8 lignes** : marquage à la fin de chaque job planifié + gauge
  `is_master`), `app/api/health.py`, `app/models/schemas.py` (`HealthResponse` additif),
  `tests/test_health_freshness.py`.
- **Contenu** :
  1. Gauge **non labellisée par instance** `plexhub_pipeline_last_success_timestamp_seconds{job}`
     (`job` = énuméré fermé : `pipeline`, `health_check`, `epg_cleanup`,
     `subtitle_cache_cleanup`, `db_backup`, `plex_catalogue_sync`) — zéro-initialisée en
     S1.4 pour que `absent()`/`time() - gauge` fonctionnent dès le boot.
  2. Gauge `plexhub_is_master` (0/1).
  3. `GET /api/health` gagne **`isMaster`** et **`lastPipelineSuccessAt`** (camelCase,
     additifs).
  4. Optionnel dans la même étape : distinguer `BlockingIOError` (lock tenu = normal) des
     autres `OSError` (logger en **ERROR**) à l'élection master — 3 lignes, ferme
     AUDIT-P1-003 qui est le mode de panne que ces gauges sont censées révéler.
- **Contrat** : `HealthResponse` **additif** ⇒ aucun client ne casse (Android tolère les
  champs inconnus). OpenAPI change.
- **Tests (AVANT)** : la gauge est à 0 avant tout run et porte un timestamp après ;
  `/api/health` expose `isMaster` ; un job qui **échoue** ne met **pas** à jour sa gauge
  (c'est tout le point).
- **Pièges §9** : **7** (`fcntl` POSIX : `isMaster` sera `false` sur un dev Windows shimé —
  ne pas en faire une condition de test) ; **10**.

#### S5.2 — Métriques downloads (AUDIT-P8-002 / F-103)

- **Fichiers** : `app/workers/download_worker.py`, `app/services/download_service.py`,
  `tests/test_download_metrics.py`.
- **Contenu** : profondeur de file par état (`queued`/`running`/`failed` — gauge
  rafraîchie à chaque tick de drain), octets transférés, échecs par raison
  (`http_403`/`disk_full`/`timeout`/`confinement`). **Le sous-système qui écrit des octets
  sur disque est aujourd'hui totalement invisible.**
- **Pièges §9** : **17c** — **aucun label ne doit contenir une URL, un `rating_key`, un
  nom de fichier ou un `server_id` complet**. Les URLs Xtream portent des credentials ;
  `/metrics` est scrapé et (jusqu'à S4.1) public. Le label `reason` est un énuméré fermé.
  **17a/b** (worker master-only, no-op si `DOWNLOAD_DIR` vide : les métriques doivent
  rester à 0, pas disparaître).

#### S5.3 — Métriques relay DAV (AUDIT-P8-002)

- **Fichiers** : `app/dav/throttle.py`, `app/dav/relay.py`, `tests/test_dav_metrics.py`.
- **Contenu** : rejets 503 du throttle, temps d'attente d'un permit (histogram), octets
  relayés. Ce sont les deux signaux qui permettraient de diagnostiquer la cascade
  `database is locked` du scan Plex (piège 18g) sans lire les logs.
- **Pièges §9** : **18c** (les sémaphores sont process-local — la métrique l'est aussi,
  cohérent avec le mono-process) ; **18d** (le drain du shim Range tient le permit : c'est
  exactement ce que l'histogram d'attente doit rendre visible) ; **18f** (ne **jamais**
  logger ni labelliser l'URL upstream).

#### S5.4 — Métriques OMDb + sync Plex (AUDIT-P8-002)

- **Fichiers** : `app/services/omdb_service.py`, `app/services/plex_sync_service.py`,
  `tests/test_omdb_metrics.py`.
- **Contenu** : `plexhub_omdb_requests_total{result}` (le budget `OMDB_DAILY_LIMIT`
  consommé est aujourd'hui **invisible**, alors qu'il est fail-open : son épuisement
  dégrade silencieusement l'enrichissement) ; `plexhub_plex_sync_total{result}`.
- **Pièges §9** : **10** (la clé OMDb ne doit jamais approcher un label ni un message —
  le service documente déjà la non-fuite via `str(exc)` httpx : ne pas la défaire).

---

### VAGUE 6 — Image & clôture

#### S6.1 — `USER` non-root + `HEALTHCHECK` image (AUDIT-P7-007, S3)

- **Fichiers** : `Dockerfile`, `docker-compose.yml` (commentaire), `docs/` (note d'exploitation).
- **Contenu** : créer un utilisateur applicatif, `chown` des répertoires créés dans
  l'image (`/app/data`, `/app/logs`), `USER` avant le `CMD`, et un `HEALTHCHECK` **dans
  l'image** (aujourd'hui il n'existe qu'au niveau compose : un `docker run` depuis GHCR
  n'en a aucun).
- **Contrat** : aucun contrat API. **Rupture d'exploitation réelle** : les volumes hôtes
  déjà montés (`./data`, `./logs`, media, downloads) appartiennent à `root` — le conteneur
  non-root ne pourra plus y écrire tant que l'opérateur n'a pas fait le `chown`.
- **Placement en dernier** : rien ne dépend de cette étape, et c'est celle dont le mode de
  panne est le plus « ça marchait avant le redémarrage ».
- **Tests** : pas de pytest. DoD = build local + `docker run` + `GET /api/health` 200 +
  écriture effective dans `/app/data`.
- **needs-approval** : oui (ownership des volumes hôtes).
- **Rollback** : revert du `Dockerfile` + rebuild.

#### S6.2 — Clôture documentaire

- **Fichiers** : `CLAUDE.md` (bandeau + §2/§3/§4/§5/§9/§10), `docs/31-board.md`,
  `docs/audit/v1/` (colonne statut par finding), ADR 0004 passé de « proposé » à
  « accepté ».
- **Contenu** : `/sync-context` complet ; marquer les findings traités ; **créer au board
  les tickets des findings identifiés mais hors périmètre** (voir §8) ; vérifier que le
  hook SessionStart réparé (`cea0a3e`) matche bien le nouveau bandeau.
- **DoD** : le hook émet « ✅ à jour » ; `CLAUDE.md` §10 ne liste plus que des dettes
  réellement ouvertes.

---

## 3. Contrats — ce qui ne bouge pas, ce qui bouge

### 3.1 Contrats STABLES (aucune étape n'a le droit d'y toucher)

| Contrat | Pourquoi | Étapes à risque |
|---|---|---|
| `MediaResponse` / `UnifiedMediaResponse` / `UnifiedEpisodeResponse` / `MediaListResponse` — forme + camelCase + `next_cursor` | consommés par `PlexHubTV` | S2.4, S2.6 |
| Préfixe `[XXX] ` (API **et** dossiers/`.strm`/`<title>` NFO) | piège 15 ; régression invisible | **S2.5** (réécriture NFO) |
| `calculate_unification_id` : format `imdb://` / `tmdb://` / `title_…` | contrat app Android (§5.4) ; clé de fusion Xtream⇆Plex (§5.10) | S2.3 |
| Les 3 `detail` des 503 IA (`AI vector storage unavailable`, `AI model unavailable`, LLM) | contractuels §9.2 | S3.3 |
| `X-API-Key` fail-closed ; publics = `/api/health` + tv-auth start/status/complete | posture de sécurité prouvée empiriquement | **S1.2** (l'allow-list les grave) |
| tv-auth : 201/404/409/410/503, aliases `deviceCode`+`device_code`, livraison one-shot atomique | vérifié sain AUDIT-P6-003 | S3.3, S4.2, S4.5 |
| `GET /api/stream/{rating_key}` champ `url` | lecture Android | — |
| `download_job` : états + namespace `xtream_`/`plex_` (`utils/server_id`) | file partagée, routage worker | S5.2 |
| `/dav` : OPTIONS/PROPFIND/HEAD/GET seulement, Basic Auth dédiée, 503 fail-closed | piège 18 | S5.3, S4.2 (exemption) |
| Arborescence `.strm` / naming des versions | Jellyfin + parité de chemins avec l'arbre DAV | S2.2, S2.5 |
| Sortie de `aggregate_movies`/`aggregate_series` | consommée par API **et** générateur | **S2.2** (parité octet-identique) |

### 3.2 Contrats qui BOUGENT

| Étape | Contrat | Avant → Après | Adaptateur | OpenAPI | Impact `PlexHubTV` |
|---|---|---|---|---|---|
| S2.1 | `GET /api/sync/status/{job_id}` | 200 `unknown` → **404** sur inconnu ; champs additifs | aucun (handshake mort aujourd'hui) | oui | **nul en pratique** — à vérifier au board |
| S2.1 | valeur du `jobId` des 5 triggers | id fantôme → id réel ; **forme inchangée** | aucun | non | nul |
| S2.3 | `displayRating` (valeur) | COALESCE → blend sur lignes NFO bi-notées | aucun (même type) | non | **note affichée + ordre de tri changent** |
| S2.5 | `POST /api/plex/generate` | champ **additif** `forceRefreshMetadata` (défaut `false`) | rétro-compatible | oui | nul |
| S4.1 | `GET /metrics` | 200 public → **401** | `METRICS_PUBLIC=true` (transitoire) | non | nul (hors app) |
| S4.2 | `/api/tv-auth/start` (et surfaces limitées) | **nouveau 429** + `Retry-After` | aucun | oui | **l'app doit tolérer 429** |
| S4.3 | `POST /admin*` | **403** si `Sec-Fetch-Site: cross-site` | header absent ⇒ accepté (curl OK) | non | nul |
| S4.5 | payloads tv-auth chiffrés | dérivation de clé changée ⇒ **sessions en vol invalidées** | `TV_AUTH_ENCRYPTION_KEY` explicite = zéro impact | non | ré-appairage (fenêtre ≤ 15 min) |
| S5.1 | `GET /api/health` | champs **additifs** `isMaster`, `lastPipelineSuccessAt` | rétro-compatible | oui | nul |
| S1.2 | boot | démarre toujours → **`RuntimeError`** si une route `/api/*` est non gardée | — | non | nul |

---

## 4. Pièges §9 touchés — table de contrôle

| Piège | Étapes concernées | Ce qui doit être vérifié |
|---|---|---|
| **6** — migrations idempotentes en fin de chaîne | **S1.3** | 023 = seule migration du lot ; `ANALYZE` rejouable ; ajoutée **après** `_migration_022` ; rejeu sans warning |
| **7** — `fcntl` POSIX, master-only | S2.1, S3.1, S5.1 | jobs process-local (404 assumé hors master) ; les workers master-only le restent ; `isMaster=false` sur dev Windows shimé |
| **8** — `db_retry` / WAL / `busy_timeout` | **S1.1, S3.1-S3.4**, S1.3, S2.3, S4.2 | jamais de `rollback()`+retry same-session (perte silencieuse) ; `ANALYZE` hors contention ; backfill hors pipeline ; le cap `pending` réduit la pression write-lock |
| **10** — CORS / secrets / fail-closed | S1.2, S1.4, S2.6, S4.1-S4.5, S5.x | aucun label ni log porteur de secret, d'URL ou d'identifiant utilisateur ; fail-closed préservé et **verrouillé** par S1.2 |
| **11** — `asyncio.to_thread` | **S2.2**, S1.3, S2.5, S4.4 | agrégation off-loop ; DNS du vetting SSRF dans le thread pool image ; écritures de génération toujours offloadées |
| **2 / 12 / 14** — motifs 503 IA & LLM | S3.3 | `ai.py` converti sans altérer un seul `detail` ni l'ordre des gardes |
| **15** — tagging adulte | S2.5, S3.2 | `[XXX] ` re-posé à l'identique lors d'une réécriture NFO ; `update_media_adult_flags` reste idempotente sous retry |
| **17c / F-007** — URLs Xtream & confinement | S4.4, S5.2, S2.5 | URL jamais persistée/loggée/labellisée ; `resolve_confined` et le confinement `outputDir` intacts |
| **18b-g** — WebDAV | S4.2, S5.3, S2.2 | `/dav` exempté du rate-limit (le throttle par compte est sa parade) ; process-local assumé ; URL upstream jamais loggée ; le rebuild d'arbre bénéficie de S2.2 |
| **1 / 5 / 13** — cold start IA, rebuild jamais au boot | *(aucune)* | aucune étape ne touche le chemin embeddings/Ollama — à re-vérifier en revue si S3.3 approche `ai.py` |
| **9** — `SafeRotatingFileHandler` | *(aucune)* | ne pas le retirer en touchant `main.py` |

---

## 5. Points `needs-approval` (à valider avant l'étape, pas après)

| # | Étape | Nature | Réversible ? |
|:-:|---|---|---|
| 1 | **S1.3** | Migration **023** : écriture DDL/données (`sqlite_stat1`) au boot sur la base de prod (189 Mo, ~196 ms) | oui (`DELETE FROM sqlite_stat1`) |
| 2 | **S2.1** | Contrat observable : `GET /api/sync/status/{id}` → 404 sur inconnu | oui (revert) |
| 3 | **S2.3 (a)** | Comportement observable : `displayRating` change de formule (valeur + tri) | oui (revert code) |
| 4 | **S2.3 (b)** | **Mutation de masse** : recalcul SQL sur ~102 k lignes | **non** — backup DB obligatoire |
| 5 | **S2.5** | Écrasement de fichiers générés sur le disque de l'opérateur (opt-in) | **non** pour les fichiers écrasés |
| 6 | **S4.1** | Rupture d'exploitation : `/metrics` → 401 (casse le scraper Prometheus) | oui (`METRICS_PUBLIC=true`) |
| 7 | **S4.2** | Nouveau code d'erreur **429** sur une surface consommée par l'app | oui (revert) |
| 8 | **S4.3** | **403** sur POST `/admin*` cross-site | oui (revert) |
| 9 | **S4.4** | Refus des hôtes privés : casse un panel Xtream **sur LAN** | oui (allow-list) |
| 10 | **S4.5** | Invalidation des sessions d'appairage en vol | **non** (ré-appairage utilisateur) |
| 11 | **S6.1** | Conteneur non-root : les volumes hôtes `root` deviennent non écrivables | oui (`chown`) |

Aucune migration **destructive**, aucun `DROP`, aucun wipe : le lot n'en contient pas.

---

## 6. Ordre d'exécution recommandé

### 6.1 Justification (risque × dépendance × gain)

1. **VAGUE 0 en premier** — zéro code applicatif, zéro risque runtime, et deux bénéfices
   immédiats : le déploiement de référence redevient utilisable (S0.1, S2 de l'audit) et
   la CI se met à tester la version **réellement livrée** (S0.3) **avant** qu'on empile
   25 étapes dessus. Si une divergence 3.12/3.13 existe, on la paie ici, pas au 20ᵉ commit.

2. **VAGUE 1 ensuite, dans l'ordre S1.1 ∥ S1.4, puis S1.2, puis S1.3** — ce sont des
   **enablers**, pas des features : la primitive de retry doit exister avant qu'on
   convertisse, la surface métrique avant qu'on branche, l'assertion de boot avant qu'on
   ajoute des routes/gardes. S1.1 et S1.4 sont file-disjoints → en parallèle. S1.2 puis
   S1.3 sérialisés (tous deux touchent `main.py`), S1.2 d'abord car il protège S1.3 et
   toute la suite. S1.3 est classé n° 2 du Top-10 (×188 mesuré) mais **passe après S1.2**
   parce que 196 ms one-shot peuvent attendre 20 minutes, pas l'inverse.

3. **VAGUE 2 en pleine largeur (6 ICs)** — c'est la fenêtre de parallélisation maximale,
   et elle contient 4 des 10 items du Top-10 (P5-001 n° 3, P3-003 n° 8, P3-002 n° 9,
   P4-005 n° 10). Zéro recouvrement de fichiers vérifié. **Lancer les 6 ensemble.**

4. **VAGUE 3 après la vague 2** — la conversion `db_retry` touche `sync_worker.py` (que
   S2.1 modifie), `tv_auth.py` et `health_check_worker.py` (que la vague 4 modifiera). La
   placer au milieu la coince entre ses deux voisins et évite deux rebases pénibles. C'est
   **le chantier le plus conflictuel du lot** : il mérite une fenêtre exclusive.

5. **VAGUE 4 après** — le hardening ajoute des gardes ; le faire **après** S1.2 signifie
   que l'assertion de boot valide déjà la nouvelle surface. S4.1 avant S4.2/S4.3 car il
   touche `metrics.py` (que la vague 5 va lire) et parce que protéger `/metrics` **avant**
   d'y ajouter des séries (vague 5) est l'ordre correct — l'audit le dit explicitement
   (AUDIT-P8-003 : « ajouter des métriques enrichit aussi ce qui fuit — protéger
   `/metrics` d'abord »). S4.4 et S4.5 en parallèle sur le côté.

6. **VAGUE 5 après la 4** — pour la raison ci-dessus. Trois étapes parallèles (downloads,
   DAV, OMDb/Plex), S5.1 sérialisée sur `main.py`.

7. **VAGUE 6 en dernier** — S6.1 (non-root) n'est requise par rien et a le mode de panne
   le plus opérationnel ; S6.2 clôt la doc une fois que tout est stabilisé.

### 6.2 Ce sur quoi j'attends ton arbitrage **avant** que l'implémentation démarre

1. **`/metrics` — escape hatch ou pas ?** L'ADR propose `METRICS_PUBLIC=true` pour que
   l'opérateur puisse déployer avant de reconfigurer Prometheus. C'est un adoucissement de
   ton « en dur cette fois ». **Je le garde, ou j'impose le fail-closed sec ?**

2. **SSRF (S4.4) — panel Xtream sur LAN.** Le vetting refusera un `base_url` en RFC1918.
   Si un opérateur a son panel/serveur d'images sur le réseau local, le health-check et
   les posters cassent. Je propose une allow-list `SSRF_ALLOW_PRIVATE_HOSTS` (vide par
   défaut). **Confirmes-tu que c'est un cas à supporter ?**

3. **Refresh NFO/posters (S2.5) — jusqu'où ?** Ma proposition : opt-in, NFO seulement,
   images jamais (préserver TMM). Alternatives : (a) refresh NFO **par défaut** avec
   marqueur de provenance pour ne jamais écraser un fichier édité à la main ;
   (b) inclure les images derrière un second flag. **Quelle option ?**

4. **`db_retry` (S3.4) — CLI/scripts.** Je recommande de **ne pas convertir** les 5 sites
   `cli.py`/`validate_id_consistency.py` (mono-process, rejouable à la main) et de le
   documenter. **D'accord, ou conversion intégrale des ~36 sites ?**

5. **`black` (S0.4).** Je recommande de **retirer** le gate de la CI (il ne vérifie rien)
   plutôt que d'activer black sur `app/`+`tests/` — ce dernier ferait un reformat de masse
   qui entrerait en conflit avec les 28 autres étapes. **Confirmes-tu le retrait ?**

6. **Rate-limit (S4.2) — portée.** Limiteur **global** (toutes surfaces, budgets
   différenciés) ou **ciblé** `/api/tv-auth/start` + cap `pending` uniquement, en
   déléguant le brute-force Basic/`X-API-Key` au WAF Cloudflare (déjà la doctrine actée
   pour `/dav`) ? Le ciblé est nettement moins risqué pour l'app.

---

## 7. Notes de migration opérateur (ruptures assumées)

### 7.1 `/metrics` devient authentifié (S4.1)

**Avant** de déployer la version contenant S4.1 :
1. Générer un couple `METRICS_USERNAME` / `METRICS_PASSWORD` (mot de passe long, aléatoire).
2. Les ajouter au `.env` hôte (S0.1 les fait suivre automatiquement dans le conteneur).
3. Ajouter à la job Prometheus concernée :
   ```yaml
   basic_auth:
     username: <METRICS_USERNAME>
     password: <METRICS_PASSWORD>
   ```
4. Déployer, puis vérifier `up{job="plexhub"} == 1`.

**Filet** : si le scraper ne peut pas être reconfiguré immédiatement, poser
`METRICS_PUBLIC=true` (un `WARNING` sera loggé au boot) et le retirer plus tard.
**Recommandation indépendante** : exclure `/metrics*` au niveau ingress/tunnel — l'auth
applicative et l'exclusion ingress sont complémentaires, pas alternatives.

### 7.2 Clé Fernet tv-auth (S4.5)

- **Si `TV_AUTH_ENCRYPTION_KEY` est déjà configurée** : **aucun impact**, rien à faire.
- **Sinon** (dérivation implicite depuis `AI_API_KEY`) : **les appairages TV en cours
  échouent** après déploiement, avec un **503** propre côté TV. Fenêtre = `TV_AUTH_TTL_SECONDS`
  (900 s par défaut). Procédure : déployer à une heure creuse ; l'utilisateur relance
  l'appairage. **Recommandé à cette occasion** : poser une vraie
  `TV_AUTH_ENCRYPTION_KEY` explicite (générée par
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
  pour ne plus jamais dépendre de la dérivation — et rendre la rotation d'`AI_API_KEY`
  sans effet sur l'appairage.

### 7.3 Backfill `display_rating` (S2.3)

Ordre **impératif** :
1. S2.3 mergée et déployée (sinon le prochain import NFO ré-écraserait le backfill).
2. **Backup DB** (`app/scripts/backup_db.py` ou le cron `BACKUP_HOUR`) — le backfill n'est
   pas révocable.
3. Pipeline **à l'arrêt** (aucun sync/enrichment en cours) — c'est un `UPDATE` de masse
   sous WAL sur ~102 k lignes.
4. `POST /api/admin/enrichment/omdb-backfill` (`verify_master_key`) avec
   `recomputeDisplayRating=true` et une Phase A vide → poller `GET .../jobs/{jobId}`.
5. Contrôle : les notes affichées par l'app changent sur les titres bi-notés (attendu).

### 7.4 Conteneur non-root (S6.1)

Avant le premier `docker compose up` de la nouvelle image :
`chown -R <uid>:<gid>` sur `./data`, `./logs`, le dossier média et le dossier downloads —
sinon le conteneur ne peut plus écrire et le boot échoue à `init_db`. L'`uid` sera indiqué
dans le `Dockerfile` et rappelé dans la note de release.

---

## 8. Hors périmètre (identifié, non planifié — à créer au board en S6.2)

Findings de l'audit v1 **délibérément** hors de ce lot, à ne pas glisser dans une étape :

- **AUDIT-P4-002** — god-files en croissance (`sync_worker` 1 618, `main.py` 661,
  `media_service` 691, `download_service` 1 352 sans ticket). Le lot les **contient**
  (règle V-1 : ≤ 10 lignes dans `main.py` par étape) mais ne les découpe pas. Créer :
  ticket de découpe `download_service` + cliquet LOC en CI.
- **AUDIT-P1-002** (échecs DDL avalés en WARNING), **P1-004** (`worker_session_factory`
  utilisé par 1 worker sur 6), **P1-005** (session unique multi-heure de
  `enrichment_worker.run()`), **P1-007** (panne DB ⇒ 401 au lieu de 503).
- **AUDIT-P3-004** (~4 465 `get_series_info`/sync non clampés par `max_connections`),
  **P3-005** (560 Mo RSS pour tenir le catalogue vs limite 2 Go), **P3-006** (FTS5),
  **P3-007** (drain post-fenêtre du shim Range), **P3-008** (index `(type, year)`).
- **AUDIT-P4-003** (SQL inline `live.py`, parse manuel `server_id[7:]` `stream.py:20-23`),
  **P4-007** (cycles `vfs ↔ tree_builder` — **partiellement** amélioré par S4.4 qui casse
  le cycle `services ⇄ dav`), **P4-008** (PK 4-tuple `media` — direction d'archi).
- **AUDIT-P5-002** (3 endpoints hors OpenAPI), **P5-003** (convention params snake_case à
  acter), **P5-005** (wire-changes livrés à vérifier côté consommateurs),
  **P5-006** (503 sqlite-vec évalué avant l'auth sur `/api/ai`).
- **AUDIT-P6-001** — le correctif **pérenne** du scan Plex (cache header/tail intégré au
  relay). Le lot n'y touche pas ; le `/wf-audit-incremental` DAV sous charge réelle reste
  la suite naturelle (FINAL-REPORT §6 V3 et §7).
- **AUDIT-P2-002** (chiffrement fail-open + rotation `AI_API_KEY`), **P2-006** (CORS `*`
  par défaut — c'est une valeur de `.env`, traitée documentairement par S0.2),
  **P2-007** (clés per-user non scopées), **P2-009** (IP client spoofable),
  **CR-S09** (echo d'exceptions upstream, request-id client non borné).
- **CR-A03 / CR-A05** — les 2 derniers `CR-*` structurels ouverts, volontairement gardés.

---

## 9. Récapitulatif

| Vague | Étapes | Parallélisme | Prérequis |
|---|:--:|---|---|
| 0 — Socle d'exploitation | 4 | **4 en parallèle** | — |
| 1 — Primitives & garde-fous | 4 | 2 ∥ + 2 sérialisées (`main.py`) | V0 |
| 2 — Contrats & fond | 6 | **6 en parallèle** | S1.1, S1.4 |
| 3 — Migration `db_retry` | 4 | **4 en parallèle** | S1.1, S2.1 |
| 4 — Hardening sécurité | 5 | 2 ∥ + 3 sérialisées (`main.py`) | S1.2, S3.1, S3.3 |
| 5 — Observabilité | 4 | 3 ∥ + 1 sérialisée (`main.py`) | S1.4, S4.1 |
| 6 — Image & clôture | 2 | sérialisées | tout |
| **Total** | **29** | | |
