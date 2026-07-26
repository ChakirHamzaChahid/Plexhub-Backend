# ADR 0004 — Contrats structurants de la remédiation `/audit-full` v1

- Statut : **proposé** (tech-lead, Phase 1 du `/refacto` « audit v1 ») — 4 décisions à valider avant démarrage de l'implémentation
- Date : 2026-07-26
- Contexte source : `docs/audit/v1/` (FINAL-REPORT §5 Top-10 + §6 roadmap V1/V2/V3, `10-stability.md`, `20-security.md`, `30-perf.md`, `40-architecture.md`, `50-api-contracts.md`, `60-release-observability.md`, `DELTA.md`)
- Plan d'exécution : `docs/plans/2026-07-26-refacto-audit-v1-plan.md`
- Portée : backend PlexHub, branche `develop`, base HEAD `cea0a3e`. **Une seule migration** dans tout le lot (`023`, additive, non destructive).
- ADR antérieurs recoupés : 0001 (résidus perf unifiés), 0003 (`display_rating` mélangé — **ce présent ADR le corrige sur un point**)

Cet ADR ne couvre **que** les décisions à impact durable du lot. Les corrections
mécaniques (compose `env_file`, `.env.example`, matrice CI, `USER` non-root, `ANALYZE`,
off-load `to_thread`, vetting SSRF réutilisé, métriques additionnelles) n'ont pas besoin
d'ADR : elles appliquent des patterns déjà actés ailleurs dans le repo.

---

## Décision 1 — `display_rating` : une seule formule, `rating_blend`, pour TOUS les écrivains

### Contexte

Deux écrivains coexistent aujourd'hui et se contredisent (AUDIT-P4-005, S3) :

- `nfo_import_service` écrit `calculate_display_rating(scraped, audience, rating)` —
  un **COALESCE best-pick** priorité IMDb (`app/services/nfo_import_service.py:531-541`,
  helper `app/utils/unification.py:40`) ;
- l'enrichissement écrit `blend(imdb, tmdb)` via `app/utils/rating_blend.py`, dont le
  module se déclare pourtant « single source of truth » (`rating_blend.py:1-14`).

Le « by design » acté par `CLAUDE.md` §5.2 (« les lignes NFO-only sans note IMDb/TMDB
gardent l'ancienne formule ») est plus fragile qu'annoncé : `nfo_import_service` **peuple
lui-même** `imdb_rating` et `tmdb_rating` (colonnes M014, via `_FIELD_MAP`), et
`recompute_display_rating_stmt()` tourne **en fin de chaque run d'enrichissement**
(`rating_blend.py:64-81`) sur `type IN (movie, show)` dès qu'**une** des deux notes est
présente. Conséquence mesurable : une ligne importée par NFO avec les deux notes voit son
COALESCE (8.8) remplacée par la moyenne (8.45) au run d'enrichissement suivant, puis
re-remplacée par la COALESCE au prochain import NFO. Dernière-écriture-gagnante, sur une
valeur **visible par l'app Android** (`MediaResponse.displayRating`, et l'index
`ix_media_type_rating` qui pilote le tri).

### Décision

**`app/utils/rating_blend.blend_rating` devient la formule unique de `display_rating`.**

1. `nfo_import_service` cesse d'écrire sa propre formule. Il conserve intégralement son
   rôle de **fournisseur de colonnes durables** (`imdb_rating`, `imdb_votes`,
   `tmdb_rating`, `tmdb_votes`, `scraped_rating`, `audience_rating` — fill-missing
   inchangé) et, quand il (ré)écrit une note, il recalcule `display_rating` via
   `blend_rating(new_imdb, new_tmdb)` sur les valeurs post-écriture.
2. `calculate_display_rating` (`utils/unification.py`) reste en place **uniquement** pour
   `sync_worker` (lignes brutes Xtream, sans note IMDb/TMDB : le blend y renvoie `None`
   et laisserait `display_rating` vide — le COALESCE reste le bon repli pour ce cas
   précis) et est marqué explicitement « repli sync-only, jamais pour une ligne portant
   une note IMDb/TMDB ».
3. **Invariant testable** : pour toute ligne `movie`/`show` portant au moins une note
   `imdb_rating`/`tmdb_rating` > 0, `display_rating == blend_rating(imdb, tmdb)`
   — c'est-à-dire que `recompute_display_rating_stmt()` doit être un **no-op**
   (`rowcount` de lignes réellement changées = 0) juste après un import NFO. C'est le
   test de non-oscillation.
4. **Backfill immédiat** (décidé par l'utilisateur) : après convergence, un recalcul SQL
   sur tout le catalogue via `recompute_display_rating_stmt()` — exécuté par le chemin
   **déjà existant** `POST /api/admin/enrichment/omdb-backfill` avec
   `recomputeDisplayRating=true` et une Phase A vide, donc **zéro nouveau code de
   backfill**. À lancer master idle, après un backup DB.

### Conséquences

- **Rupture de valeur assumée** : les lignes NFO portant les deux notes changent de
  `displayRating` (best-pick → moyenne) et peuvent changer d'ordre dans un tri par note.
  Pas un changement de schéma, pas un changement de type : l'app Android n'a rien à
  adapter. À signaler en note de migration.
- L'oscillation disparaît : plus de « note qui change toute seule » à diagnostiquer.
- `rating_blend` devient réellement ce que son docstring prétend. ADR 0003 §D-BLEND est
  **étendu** (il n'avait tranché que pour l'enrichissement), pas contredit.
- Risque résiduel accepté : une ligne NFO-only sans aucune note garde `COALESCE(scraped,
  audience, rating)` par `sync_worker` — c'est le seul cas où le blend n'a rien à dire.

---

## Décision 2 — Contrat des jobs de déclenchement : registre partagé, `jobId` réel, 404 sur inconnu

### Contexte

Le pattern documenté « travaux longs → `202 Accepted` + `jobId` à poller »
(`.claude/knowledge/api-conventions.md`) est **cassé de bout en bout** sur les 5 triggers
de `app/api/sync.py` (AUDIT-P5-001, S2) :

- le router fabrique `f"sync_{account_id}_{id(task)}"` (identité mémoire de la task,
  `sync.py:29`) tandis que le worker enregistre sous `f"sync_{account_id}_{now_ms()}"`
  (`sync_worker.py:1100`) — deux clés différentes ;
- les 4 autres triggers (`/xtream/all`, `/enrichment`, `/validate-streams`,
  `/full-pipeline`) fabriquent un id qui n'est **jamais** enregistré nulle part : seul
  `sync_account` alimente `_sync_jobs` ;
- `GET /api/sync/status/{job_id}` renvoie **200 `{"status":"unknown"}`** pour un id
  inexistant (`sync.py:111-118`, AUDIT-P5-008) : un client ne peut pas distinguer
  « en cours », « terminé » et « id bidon ».

Prouvé empiriquement par l'audit (`POST /api/sync/xtream/all` → `jobId` → `GET
/status/…` → `200 unknown` ; `GET /api/sync/jobs` → `{"jobs":[]}`).

Corollaire : `POST /api/sync/full-pipeline` n'a **aucun moyen contractuel** d'exposer son
issue (AUDIT-P6-005) — succès, échec et durée ne vivent que dans `logs/plexhub.log`.

### Décision

1. **Un registre de jobs unique** extrait de `sync_worker` vers un module dédié
   `app/services/job_registry.py` (in-memory, borné, éviction FIFO — même sémantique
   qu'aujourd'hui, cf. AUDIT-P1-008 : la persistance des jobs reste hors périmètre et
   assumée). Consommé par les 5 triggers **et** par le worker.
2. **Le `jobId` est créé par l'appelant (le router) et passé au travail de fond**, jamais
   dérivé après coup. Le worker met à jour l'entrée par cette clé. `sync_account`
   conserve sa capacité à créer sa propre entrée quand il est appelé hors HTTP
   (scheduler, `run_all_accounts`) — un paramètre `job_id: str | None = None` suffit.
3. **Les 4 autres travaux sont trackés** au même titre : `sync_all`, `enrichment`,
   `validation`, `pipeline`. Le pipeline enregistre en plus la **phase courante** (`sync`
   → `enrichment` → `validation` → `generation` → `snapshot`), ce qui résout P6-005 sans
   endpoint supplémentaire.
4. **`GET /api/sync/status/{job_id}` renvoie 404 sur id inconnu.** Justification :
   l'ancien 200 `unknown` est indistinguable d'un état réel, et **aucun client ne peut en
   dépendre** puisque le handshake est mort pour 100 % des ids depuis toujours. Le
   contrat propre est posé maintenant, sans période de dépréciation.
5. **Le champ `status` conserve son vocabulaire actuel** (`processing`/`completed`/
   `failed`) pour ne rien casser côté sérialisation ; les informations nouvelles
   (`phase`, `startedAt`, `finishedAt`, `error`) sont **additives** et camelCase.
6. **Test de bout en bout obligatoire** : trigger → `GET /status/{jobId}` retourné →
   assertion d'un état connu, sur les 5 triggers. C'est précisément le test dont
   l'absence a laissé passer le bug.

### Conséquences

- OpenAPI change : `SyncStatusResponse` gagne des champs, `/status/{job_id}` gagne une
  réponse 404. **Changement observable ⇒ `needs-approval`.**
- Impact app Android : **nul en pratique** (le handshake ne fonctionne pas aujourd'hui),
  mais un client qui traitait « 200 » comme « l'id existe » verra un 404. Documenté.
- Les jobs restent **process-local** : un `GET /status` depuis un worker non-master, ou
  après redémarrage, répond 404. C'est cohérent avec le nouveau contrat (404 = « je ne
  connais pas cet id ») et reste la dette assumée AUDIT-P1-008.

---

## Décision 3 — `/metrics` : authentifié par défaut, avec un opt-out explicite

### Contexte

`/metrics` est exposé sans aucune dépendance (`app/utils/metrics.py:46-51`, monté
`main.py:660-661`), **200 sans clé confirmé empiriquement** (AUDIT-P2-001 / AUDIT-P8-003
/ CR-S02, S2). Les métriques métier portent des labels `account_id`
(`metrics.py:14-43`) et l'instrumentator publie la carte complète des routes et les
volumes par statut. Le déploiement cible est un tunnel Cloudflare public. Aggravant : le
chantier d'observabilité de ce même lot (AUDIT-P8-002) **augmente** ce qui fuit.

C'est la seule surface `/…` de l'app qui n'est ni fail-closed, ni derrière Basic Auth.

### Décision

1. **Garde Basic Auth dédiée** `verify_metrics_basic_auth` dans `app/api/deps.py`, avec
   ses propres secrets `METRICS_USERNAME` / `METRICS_PASSWORD` — **pas** `X-API-Key**
   (Prometheus sait faire `basic_auth:` nativement, pas un header custom : même
   raisonnement que `/dav` pour rclone, `deps.py:148`), **pas** les identifiants `ADMIN_*`
   (séparation de domaine : un scraper compromis ne doit pas ouvrir l'UI admin).
2. **Comparaison temps-constant** sur user ET password, les deux évalués avant tout
   branchement — copie conforme de `verify_admin_basic_auth` (`deps.py:110-142`).
3. **Escape hatch explicite `METRICS_PUBLIC=true`** : quand il est posé, `/metrics` reste
   ouvert et le boot logge un `WARNING` nommant la dette. Sans lui, `METRICS_PASSWORD`
   vide ⇒ **503 fail-closed**, même convention qu'`ADMIN_PASSWORD` et `DAV_PASSWORD`.
   L'escape hatch existe pour qu'un opérateur puisse dérouler la mise à jour **avant**
   d'avoir reconfiguré son scraper, pas pour rester ouvert.

### Conséquences

- **Rupture d'exploitation assumée et décidée** : tout scraper Prometheus existant
  tombe en 401 à la montée de version. Procédure opérateur obligatoire dans les notes de
  migration (générer les secrets → `basic_auth` côté `prometheus.yml` → déployer), avec
  `METRICS_PUBLIC=true` comme filet temporaire.
- `excluded_handlers=["/metrics"]` reste : l'endpoint ne s'auto-mesure pas.
- Ne dispense pas de l'exclusion `/metrics` au niveau ingress (défense en profondeur,
  déjà recommandée en interim V1 par le FINAL-REPORT §6).

---

## Décision 4 — Retry DB : `run_with_retry` à **session fraîche par tentative** est LA primitive ; `commit_with_retry` échoue honnêtement et est déprécié

### Contexte

`commit_with_retry(db)` re-invoque `db.commit` **sur la même session**
(`app/utils/db_retry.py:56-62`). En SQLAlchemy 2.x, un `commit()` qui lève invalide la
transaction : la 2ᵉ tentative lève `PendingRollbackError`, que la branche
`except OperationalError` de `run_with_retry` (`db_retry.py:43-45`) **n'attrape pas** →
re-raise immédiat. Le retry n'exécute donc **jamais** de 2ᵉ tentative utile sur les
~36 call-sites répartis dans 13 modules. Le comportement est **déterministe** et
aujourd'hui **figé par un test** qui l'assert comme correct
(`tests/test_db_retry_real_lock.py:218-258`,
`TestCommitWithRetrySameSessionBoundary`).

Deux corollaires trompeurs : (a) la trace remontée (`PendingRollbackError`) est
**différente de l'erreur d'origine** (`OperationalError: database is locked`) — le
diagnostic est brouillé ; (b) le piège §9.8 de `CLAUDE.md` revendique une résilience qui
n'existe pas.

**Le correctif naïf est un piège** : ajouter `await db.rollback()` avant chaque nouvelle
tentative ne marche pas et est **pire que le bug**. SQLAlchemy **expulse** les objets
`pending` (ajoutés par `db.add()` dans la transaction) lors d'un rollback : la tentative
suivante commiterait **zéro ligne**, sans erreur. On échangerait un crash bruyant contre
une perte d'écriture silencieuse. Cette option est explicitement rejetée.

### Décision

1. **La primitive de résilience est la session fraîche par tentative.** Elle existe déjà
   et fonctionne (`run_with_retry` + factory de coroutine, employée correctement par
   `download_worker`, `plex_sync_service`, `_cleanup_stale_epg` `main.py:215-240`). On
   l'expose sous une forme directement consommable :

   ```python
   async def write_with_retry(
       work: Callable[[AsyncSession], Awaitable[T]],
       *, session_factory=None, delays=DEFAULT_DELAYS, op: str = "write",
   ) -> T:
       """Ouvre une session NEUVE par tentative, exécute `work(session)`,
       commit à l'intérieur. Toute tentative repart d'un état propre : les
       objets `pending` sont reconstruits par `work`, jamais expulsés."""
   ```

   Contrat d'usage : `work` doit être **rejouable** (idempotent ou reconstruisant ses
   objets) et ne doit **rien capturer** d'une session extérieure.

2. **`commit_with_retry` devient honnête, pas retryant.** Il attrape
   `PendingRollbackError`, fait le `rollback()` d'hygiène, et **re-lève l'`OperationalError`
   d'origine** (`raise original from None` — jamais l'exception de garde de session), avec
   un `logger.warning` nommant l'`op`. Il reste en place comme drop-in `await db.commit()`
   protégé du seul cas où le `busy_timeout=60 s` suffit — ce qu'il fait déjà en réalité —
   et son docstring le dit explicitement. **Il n'est pas supprimé** : le remplacer partout
   d'un coup serait un big-bang sur 13 modules chauds.

3. **Migration par zone, pas d'un bloc**, avec une priorité par exposition réelle à la
   contention (les workers concurrents d'abord, les one-shots CLI/scripts en dernier ou
   jamais). Chaque zone = un commit indépendant, chacun accompagné d'un test de **vrai
   lock WAL** (le harnais existe déjà dans `tests/test_db_retry_real_lock.py`).

4. **Le test qui fige le défaut est inversé** :
   `TestCommitWithRetrySameSessionBoundary` devient
   `TestCommitWithRetryFailsHonestly` — il assert que l'exception remontée est bien
   l'`OperationalError` « database is locked » (pas `PendingRollbackError`) ; un test
   frère assert que `write_with_retry` **survit** au même lock réel.

### Conséquences

- Le piège §9.8 de `CLAUDE.md` doit être réécrit : « `commit_with_retry` ≠ résilience —
  utiliser `write_with_retry` (session fraîche) pour tout writer réellement exposé ».
- Les call-sites non encore migrés ne régressent pas : ils passent d'un crash à trace
  trompeuse à un crash à trace exacte. Aucun changement de contrat HTTP.
- Coût : ~36 sites à convertir en closures, sur des fichiers chauds
  (`sync_worker.py` 1 618 LOC, `tv_auth.py`, `accounts.py`…). C'est le chantier le plus
  conflictuel du lot — d'où le séquencement dédié dans le plan.
- Effet de bord positif : les closures `work(session)` isolent naturellement les unités
  d'écriture et sont un premier pas vers le découpage de `sync_account` (504 lignes,
  AUDIT-P4-002).

---

## Références

- Audit : `docs/audit/v1/FINAL-REPORT.md` §5 (Top-10), §6 (roadmap V1/V2/V3)
- Plan d'exécution par étapes : `docs/plans/2026-07-26-refacto-audit-v1-plan.md`
- ADR antérieurs : `0001-unified-perf-residuals.md`,
  `0003-dual-provider-enrichment-and-blended-display-rating.md` (étendu par la Décision 1)
- House law : `CLAUDE.md` §3 (conventions), §9 (pièges 6/8/11/17/18)
