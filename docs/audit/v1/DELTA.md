# DELTA — Re-vérification indépendante des findings d'audit antérieurs

> **Audit v1 (série versionnée `AUDIT-*`) — volet DELTA.**
> HEAD vérifié : `develop` @ **`9da9d46`** (release v1.7.1, 2026-07-25). Méthode : chaque verdict repose sur une
> **preuve code `fichier:ligne` à HEAD** (jamais sur la doc seule), complétée par le smoke-serveur de session
> (`/api/health` 200 v1.7.1, `/api/media/movies` 401 sans clé / 200 avec, `/docs` 401, `/dav/` 503, migrations
> 001→022 rejouées **sans warning duplicate-column**, `/metrics` 200).
> Lignées couvertes : **`CR-*` 2026-07-11** (56 findings — lignée courante), **balayage 2026-06-15** (superseded,
> recherche de findings perdus), **board `docs/31-board.md`**, **`CLAUDE.md` §9/§10**.
> **Lignée `AUDIT-*` (docs/audit/v*)** : **n'existe pas avant ce présent audit v1** — il n'y a donc aucun finding
> `AUDIT-*` antérieur à re-vérifier ; ce document est le premier de la série.

Statuts normalisés : `RÉSOLU` · `RÉSOLU-PARTIEL` · `OUVERT` · `RÉGRESSÉ` · `WON'T FIX (justifié)` ·
`WON'T FIX (justification caduque)` · `NON VÉRIFIABLE`.

---

## 1. Tableau maître — lignée `CR-*` (clean-room 2026-07-11, 56 findings)

Colonne « Déclaré » = statut affiché par le README clean-room (log de remédiation) / le board / `CLAUDE.md` §10.
Quand les trois divergent, la divergence est signalée (⚠️) et détaillée en §2.

### Architecture (CR-A01…A07)

| ID | Sév | Déclaré | Vérifié à HEAD | Preuve (`fichier:ligne`) | Commentaire |
|---|:--:|---|---|---|---|
| CR-A01 | P1 | Partiellement résolu (résidu `stream.py` + pagination `live.py`) | **RÉSOLU-PARTIEL** | Services extraits : `app/services/account_service.py:10`, `app/services/live_service.py:10`, `app/services/plex_generation_service.py`. Résidu intact : `app/api/stream.py:19-29` (parsing `server_id` inline `server_id[7:]` + SELECT brut dans le router, sans même utiliser `utils.server_id.parse_server_id`) ; `app/api/live.py` (219 LOC, listing/pagination inline) | Conforme au déclaré. Le résidu n'a pas grossi (stream.py = 36 LOC). |
| CR-A02 | P1 | Résolu (Vague C2) | **RÉSOLU** | `app/services/plex_generation_service.py` ; `app/api/sync.py:99-103` (importe le service, plus `app.main`) ; `app/cli.py:37` ; `app/api/plex.py:87-89` ; `app/main.py:118-131` (wrapper mince) | Orchestration unifiée, layering inversé corrigé. |
| CR-A03 | P1 | GARDÉ (effort dédié) | **OUVERT — dette AGGRAVÉE** | `wc -l` à HEAD : `app/workers/sync_worker.py` = **1618 LOC** (audit : 1390 → **+228, +16 %**), `app/api/ai.py` = 1225 (~stable), `app/services/nfo_import_service.py` = 888 (stable) | Le « gardé » est honnête, mais le god-file principal a **grossi** depuis l'audit (episodes cleanup, granularité download, file_size…). Le coût du refacto différé augmente. |
| CR-A04 | P2 | Résolu (étiquetage A/B/C) ; centralisation = follow-up | **RÉSOLU-PARTIEL** | Étiquetage présent : `app/main.py:568-580` (Pattern A `_guard`), `:570` (B public), `:623-626` (C self-guardé). **Aucune** assertion centrale sur `app.routes` (grep `app.routes` dans tests/ + main.py = 0) | Résidu inchangé, mais la **surface a grossi** : 5 routers Pattern C désormais (`api_keys`, `downloads`, `plex_downloads`, `enrichment`, `dav`) + `/dav` monté hors `/api` — la garantie « tout `/api/*` est authentifié » reste purement disciplinaire. Atténuation réelle : chaque nouveau router a livré ses tests 401 (cf. CR-T02). |
| CR-A05 | P2 | GARDÉ (effort dédié) | **OUVERT — dette AGGRAVÉE** | `app/main.py` = **661 LOC** (CLAUDE.md §2 déclare encore « 442 LOC ») ; coroutines métier module-level : `_auto_generate_plex_library` `:118` (wrapper mince désormais), `_rebuild_unified_groups` `:134`, `_auto_provision_xtream_account` `:154`, `_cleanup_stale_epg` `:215` | +~220 LOC depuis l'audit (wiring DAV, download worker, jobs Plex, locks pipeline). `_auto_generate` est devenu délégation pure (progrès), le reste demeure. |
| CR-A06 | P2 | Résolu (reach-in) ; job stores = limitation notée | **RÉSOLU** | `app/services/recommendation_service.py:45` (`serialize_vec` public), `app/plex_generator/naming.py:40,90` (`movie_folder`/`series_folder` publics) | La limitation « job stores in-memory process-local » subsiste **et a été répliquée** dans `app/workers/enrichment_backfill_worker.py` (nouveau store 202-jobs en mémoire) — pattern assumé, à garder à l'œil. |
| CR-A07 | debt | Résolu (Vague C1) | **RÉSOLU** | `app/services/aggregation_service.py:82` (`build_versions` unifié, consommé API + générateur) | — |

### Conventions (CR-C01…C10)

| ID | Sév | Déclaré | Vérifié à HEAD | Preuve | Commentaire |
|---|:--:|---|---|---|---|
| CR-C01 | P1 | Résolu (Round 1) | **RÉSOLU** | `app/plex_generator/generator.py:210` (`to_thread(self.mapping.load)`), `:232`, `:265` (écritures/prune off-loop) | ⚠️ `CLAUDE.md` §3 (ligne Async I/O) déclare encore l'exception « generate() sur la boucle (dette CR-C01 P1) » — périmé. |
| CR-C02 | P1 | Résolu (Round 1, breaking wire) | **RÉSOLU** | `app/api/categories.py:84` (`response_model=CategoryRefreshResponse`), `:110` | camelCase `vodCount`/`seriesCount` sur le fil. |
| CR-C03 | P2 | Résolu sauf résidu `media.py:353` | **RÉSOLU-PARTIEL** | `app/api/sync.py:20-121` (7 endpoints typés) ; résidus : `app/api/media.py:442` (`return {"status": "queued"}`, rescrape) + `app/api/categories.py:74` (`return {"message": …}`) | Résidu conforme au déclaré (2 dicts nus mineurs), n'a pas grossi. |
| CR-C04 | P2 | Résolu (Vague C1) **mais défaut découvert** (`PendingRollbackError`) | **RÉSOLU-PARTIEL** | `commit_with_retry` câblé request-path : `app/api/tv_auth.py:175,228,294,367,411`, `accounts.py:42,63,79`, `categories.py:105`, `live.py:185`. **Défaut toujours ouvert** : `app/utils/db_retry.py:39-43` n'attrape que `OperationalError` — le test de garde `tests/test_db_retry_real_lock.py:218-258` (`TestCommitWithRetrySameSessionBoundary`) **documente** que sur vrai lock même-session la 2ᵉ tentative lève `PendingRollbackError` non rattrapée | Le correctif « factory `run_with_retry` sur les call-sites » (effort dédié annoncé au README:106 et au bandeau CLAUDE.md) **n'a pas été fait**. Atténué par `busy_timeout=60 s`. Ce défaut n'a **pas d'ID de finding propre** — à immatriculer dans la série v1. |
| CR-C05 | P2 | Résolu (probe `_column_exists`) | **RÉSOLU** | `app/db/migrations.py:55` (helper), utilisé partout (`:112,130,156,186,387`…) ; **confirmé empiriquement** : migrations 001→022 rejouées sur DB fraîche **sans warning duplicate-column** (smoke de session) | — |
| CR-C06 | debt | Résolu (Round 2) | **RÉSOLU** | `pyproject.toml:30` (`--cov-fail-under=70` dans addopts), `:46` (`[tool.ruff]`), `:108` (`[tool.black]`) ; CI job `lint` (`.github/workflows/tests.yml` : ruff + black --check) | — |
| CR-C07 | debt | Résolu (Round 2) | **RÉSOLU** | `grep pydantic-settings requirements.txt` = 0 hit | ⚠️ `CLAUDE.md` §10 (stack runtime) liste **encore** « pydantic-settings≥2.1 (déclarée, non utilisée) » — périmé dans le sens résolu. |
| CR-C08 | debt | Résolu (Round 2) | **RÉSOLU** | `grep sanitize_edition_label\|_EDITION_INVALID app/plex_generator/naming.py` = 0 hit | — |
| CR-C09 | debt | **WON'T FIX** (bloqué par le pin Starlette) | **WON'T FIX (justifié)** | Pin toujours actif et couplé : `requirements.txt:6` (`fastapi>=0.115,<0.116`) + `:16` (instrumentator `<8`, commentaire route-walker) ; constante dépréciée toujours utilisée : `app/api/ai.py:330,365,416,423,463,1139` | La justification **tient toujours** : tant que le couple fastapi/instrumentator est épinglé (outage `a8a0ce7`), `HTTP_422_UNPROCESSABLE_CONTENT` n'existe pas dans la Starlette embarquée. À re-trancher seulement lors d'un bump de stack. |
| CR-C10 | debt | `TempAccount` résolu ; **« `_Acc` de `main.py` still open »** (README:189-191) | **RÉSOLU (mieux que déclaré)** | `app/main.py:160,177` utilise `XtreamCredentials` (`app/services/xtream_credentials.py:15`) ; `grep _Acc app/ --include=*.py` = **0 hit** ; migration-008 remise en ordre numérique (`migrations.py:287`) | ⚠️ Écart déclaratif **dans le sens conservateur** : le README dit la moitié `_Acc` encore ouverte, elle est résolue à HEAD (absorbée par la campagne A01/C10). |

### Data-flows (CR-F01…F11)

| ID | Sév | Déclaré | Vérifié à HEAD | Preuve | Commentaire |
|---|:--:|---|---|---|---|
| CR-F01 | P1 | Résolu (Vague A) | **RÉSOLU** | `app/workers/sync_worker.py:814` (`differential_cleanup_episodes`, scopé show+serveur), câblé `:1464` ; garde soft-failure empty-200 `:1451-1453` (`if success and rows:`) + rationale `:1436-1450` | ⚠️ `CLAUDE.md` §5.1/§10 déclarent encore « les épisodes ne sont JAMAIS differential-cleaned (CR-F01) » — périmé. |
| CR-F02 | P1 | Résolu (Round 1, relocation) | **RÉSOLU** | `app/workers/sync_worker.py:554-622` : collision de slot ⇒ **relocation** per-partition `MAX(page_offset)+1`, DELETE seulement si le rating_key occupant est réellement délisté (`:580-587`, `to_relocate` `:622`) | La ligne inchangée survit avec son enrichissement. ⚠️ CLAUDE.md §10 la liste encore ouverte. |
| CR-F03 | P1 | Résolu (Vague A) ; résidu persistance | **RÉSOLU-PARTIEL** | `app/services/tmdb_service.py:119-123` (`real_request_count`), `:183-188` (incrément **par tentative HTTP réelle**, retries compris) | Résidu déclaré et confirmé : budget **in-process**, remis à zéro par run, non persisté cross-process/cross-restart (board OMDB-02 le re-note). Même sémantique reprise par `omdb_service`. |
| CR-F04 | P1 | Résolu (Round 1) | **RÉSOLU** | `app/main.py:115` (`_PIPELINE_LOCK`), skip-if-locked boot `:299-305`, intervalle `:470-475` | Exclusion mutuelle boot ⇆ intervalle effective (process-local — suffisant en déploiement single-process, cf. balayage 2026-06-15 §3). ⚠️ CLAUDE.md §5/§10 déclarent encore le recouvrement possible. |
| CR-F05 | P1 | Résolu (Vague B) | **RÉSOLU** | `app/services/media_service.py:426-462` : `get_unified_group` charge un pool de candidats convergeables (`_load_convergence_candidates` `:462`) et repasse par le même `aggregate_movies`/`_converge` que la liste | Les jumelles `imdb://`/`tmdb://`/`title_…` sont désormais rapportées. ⚠️ CLAUDE.md §5.4/§10 disent encore « sous-rapporte ». |
| CR-F06 | P2 | Résolu (Vague A) | **RÉSOLU** | `app/api/tv_auth.py:306-318` : `deviceCode` (camelCase, prioritaire) + `device_code` legacy | ⚠️ CLAUDE.md §5.6 décrit encore l'incohérence snake_case comme ouverte. |
| CR-F07 | P2 | Résolu (Vague A) | **RÉSOLU** | `app/api/tv_auth.py:350-370` : claim atomique `UPDATE … WHERE payload_delivered IS FALSE` + `rowcount == 1` | Livraison one-shot désormais atomique sous double poll concurrent. |
| CR-F08 | P2 | Résolu (Vague A) | **RÉSOLU** | `app/workers/health_check_worker.py:601-610` : breaker **roulant** (éval. à chaque check, `min_sample=10`, seuil 0.90) + fix `expunge_all` `:587-594` | Fini l'évaluation unique à exactement 50 checks. |
| CR-F09 | P2 | Résolu (Vague A) | **RÉSOLU** | `app/services/aggregation_service.py:138` (`_key_rank`), `:206` et `:244-246` (`min(..., key=_key_rank)` — vainqueur déterministe, indépendant de l'ordre de requête) | — |
| CR-F10 | debt | Résolu (Vague C1) ; re-pick one-shot acté | **RÉSOLU** | `app/plex_generator/source.py:79` (filtre `is_broken` **post**-agrégation, gaté `STREAM_FILTER_BROKEN`), `:110,195` (commentaires CR-F10 aux points où le filtre a été retiré) | Le résidu « 1ʳᵉ génération = renommage one-shot possible » était transitoire (générations passées depuis). |
| CR-F11 | P2 | Résolu (Vague A) | **RÉSOLU** | `app/workers/sync_worker.py:1426-1433` : fetch épisodes itère `all_series_dtos` (toutes séries actives) par batch de 50, découplé du hash du show (`_compute_series_dto_hash` `:527-528` ne sert plus de gate au fetch d'épisodes) | Coût documenté : +1 `get_series_info`/série/sync. |

### Sécurité (CR-S01…S09)

| ID | Sév | Déclaré | Vérifié à HEAD | Preuve | Commentaire |
|---|:--:|---|---|---|---|
| CR-S01 | P1 | README : **résolu** (Round 1) · **CLAUDE.md §10 : encore OUVERT** (« outputDir client verbatim ») | **RÉSOLU** | `app/api/plex.py:35-73` (`_resolve_confined_output_dir`) : base = `PLEX_LIBRARY_DIR` seul ; chemin client accepté uniquement si `resolve()` ∈ base ou descendants (`Path.parents`, pas de prefix-string naïf) ; 400 si base non configurée `:58-64` | ⚠️ **Écart déclaratif majeur** : `CLAUDE.md` §10 et §9 piège 17d présentent encore CR-S01 comme dette P1 vivante — c'est faux à HEAD. Le follow-up defense-in-depth (`verify_master_key` sur `/plex/generate`) reste non fait (endpoint sous `_guard` = toute clé par-utilisateur, `main.py:579`) — acté « GARDÉ » au bandeau. |
| CR-S02 | P2 | Ouvert | **OUVERT** | `app/main.py:659-661` + `app/utils/metrics.py:51` : `/metrics` exposé sans aucune garde | Confirmé au smoke (200 sans auth). Note d'observation : **0 série `plexhub_*`** sur instance fraîche — attendu (les 5 métriques métier sont toutes **labellisées**, `metrics.py:17,24,30,36,42` → aucune série avant le 1ᵉʳ usage), mais un dashboard/alerting sur instance neuve ne voit rien. |
| CR-S03 | P2 | Résolu (Round 2, « fail-open documenté ») | **RÉSOLU-PARTIEL** (risque résiduel documenté, pas déplacé en douce) | `app/models/database.py:140` (`password = EncryptedString()`), `:577` (`PlexServer.access_token` idem) ; `app/utils/crypto_fields.py:89-101` (clé dédiée `XTREAM_ENCRYPTION_KEY` sinon dérivée `AI_API_KEY` avec domain-separation `:71`) ; **fail-OPEN plaintext** si les deux absents `:102-109,134-136` ; migration 016 chiffre l'existant | Arbitrage tranché : le fail-open est **explicite, loggé et borné** — et en pratique `AI_API_KEY` est requis pour que l'API fonctionne, donc la clé dérivée existe presque toujours. Risque **nouveau** documenté : rotation d'`AI_API_KEY` sans re-chiffrement ⇒ créds indéchiffrables (retour ciphertext, `:151-158`). Verdict : résolution réelle avec réserve opérationnelle, pas un faux « résolu ». |
| CR-S04 | P2 | Ouvert | **OUVERT** | `app/utils/payload_crypto.py:36-46` : clé Fernet tv-auth = `TV_AUTH_ENCRYPTION_KEY` sinon **dérivée SHA-256 d'`AI_API_KEY`** (réutilisation du secret bearer, sans domain-separation contrairement à `crypto_fields.py:71`) | Conforme au déclaré. Incohérence interne : le module Xtream a la domain-separation, le module tv-auth non. |
| CR-S05 | P2 | Ouvert | **OUVERT — surface AGGRAVÉE** | `grep -ri "rate.limit\|slowapi\|limiter" app/` = 0 hit | Aucun rate-limit/anti-brute-force nulle part. Depuis l'audit, la surface a grossi : Basic Auth `/admin` (3 onglets download), **`/dav` Basic Auth sans lockout** (acté §9 piège 18b : « rotation du password = seul mécanisme de révocation »). |
| CR-S06 | P2 | Résolu Round 1 (« CORS explicit methods/headers + warning ») | **RÉSOLU-PARTIEL** | `app/main.py:548-549` : `allow_methods`/`allow_headers` **explicites** (fini `*`) ; `:535-538` warning si origine `*` ; défaut origine toujours `*` (`app/config.py:86-87`) | Le cœur du finding (wildcard origins par défaut) subsiste par design (warning + doc). ⚠️ `CLAUDE.md` §9 piège 10 (« méthodes/headers `*`, main.py:385-386 ») est périmé. |
| CR-S07 | P2 | Ouvert (dette transverse, DL-03/DL-PLEX-05/XD-03) | **OUVERT — surface AGGRAVÉE** | `grep -ri csrf app/` = 0 hit | Aucun token CSRF sur `/admin` ; la surface de POST admin a **triplé** depuis l'audit (`/admin/downloads`, `/admin/plex-downloads`, `/admin/unified-downloads` — enqueue/cancel/retry/sync). Le board trace honnêtement le report (effort transverse unique). Atténuation : Basic Auth + pas de cookies de session. |
| CR-S08 | P2 | Ouvert | **OUVERT** (mais surfaces nouvelles durcies) | Toujours vrai : `app/plex_generator/storage.py:55` (`follow_redirects=True` images), `app/workers/health_check_worker.py:183,213` (HEAD/GET validation) ; `base_url` compte = URL arbitraire post-auth. **Contraste** : les chemins récents vettent leurs redirects (`app/services/download_service.py:403-404` `follow_redirects=False` + vetting par hop, `app/dav/relay.py:107` idem + `assert_public_redirect_host`) | Le finding d'origine est intact ; la maison sait faire (la primitive de vetting existe dans `download_service`) mais ne l'a pas rétrofittée sur images/health-check. |
| CR-S09 | debt | Ouvert | **OUVERT** | `app/utils/request_context.py:22` : `X-Request-ID` client repris **verbatim, non borné** (`request.headers.get(...) or uuid4().hex[:12]` — la troncature ne s'applique qu'au généré) | L'écho d'exceptions upstream n'a pas de remédiation tracée non plus. Conforme au déclaré. |

### Performance (CR-P01…P08)

| ID | Sév | Déclaré | Vérifié à HEAD | Preuve | Commentaire |
|---|:--:|---|---|---|---|
| CR-P01 | **P0** | Résolu (`/refacto` `c3024e3`, §10 CLAUDE.md à jour sur ce point) | **RÉSOLU** | `app/services/media_service.py:269-283` (browse non-filtré → snapshot, O(page)), `:337+` (`_unified_list_from_snapshot`), fallback live si snapshot vide/requête filtrée `:274-294` ; builder `app/services/unified_group_service.py` ; rebuild fin de pipeline `app/main.py:134-152` ; migration 017 | Résidus documentés et confirmés : requêtes **filtrées** = chemin live + cache TTL 45 s (`media_service.py:67-69`) ; staleness ≤ TTL. Verdict : le P0 est réellement fermé. |
| CR-P02 | P1 | Résolu (migration 015) | **RÉSOLU** | `app/db/migrations.py:43` (`_migration_015_add_missing_media_indexes` dans la chaîne) ; preuve test `tests/test_media_indexes_migration.py` | — |
| CR-P03 | P1 | Résolu (COUNT) ; résidu ILIKE | **RÉSOLU-PARTIEL** | COUNT étroit : `app/services/media_service.py:191-194` (`select(func.count()).select_from(Media)`). Résidu intact : ILIKE leading-wildcard `:166,169` (liste brute) et `:291,294` (unified filtré) | Conforme au déclaré (FTS/trigram jamais entrepris). Le résidu n'a pas grossi. |
| CR-P04 | P2 | Résolu (`b1f5ed6`) | **RÉSOLU** | `app/api/media.py:51-67` (`_page_meta` → `next_cursor`), `:126-147` (param `cursor` + `encode_media_cursor`) — additif, offset conservé | — |
| CR-P05 | P2 | Résolu (validation) ; générateur by-design | **RÉSOLU-PARTIEL** | `app/workers/health_check_worker.py:637` (`yield_per=1000`, streaming par compte, `:552-583`) ; `app/plex_generator/source.py` matérialise toujours tout le catalogue (groupement whole-set, by-design documenté `:65`) | Conforme au déclaré. |
| CR-P06 | P2 | Résolu (Vague A) | **RÉSOLU** | `app/workers/health_check_worker.py:329-361` : ancre `rowid` aléatoire + scan avant/arrière indexé — plus d'`ORDER BY random()` | ⚠️ Le board (`31-board.md`, XD-01) décrit encore l'échantillon comme « `ORDER BY random()` CR-P06 » — description périmée du code. |
| CR-P07 | P2 | Résolu (`ba6689e`) | **RÉSOLU** | `app/api/media.py:29` (`_single_pass_json`), utilisé `:145` et sur les endpoints liste ; `response_model=` conservé pour l'OpenAPI | — |
| CR-P08 | debt | Résolu (Vague B) ; skew > 2000 accepté | **RÉSOLU-PARTIEL** | `app/services/recommendation_service.py:214-234` (`KNN_OVERFETCH_CEILINGS = (200, 2000)`, escalade ≤ 2 requêtes, cap dur documenté) | Résidu accepté et documenté dans le code même. |

### Tests (CR-T01…T11)

| ID | Sév | Déclaré | Vérifié à HEAD | Preuve | Commentaire |
|---|:--:|---|---|---|---|
| CR-T01 | P1 | Résolu (Round 1) | **RÉSOLU** | Rejet `U+FFFD` : `app/services/live_service.py:44-47` (code déplacé de `live.py` par CR-A01) ; `grep deselect .github/workflows/tests.yml` = **0 hit** (le test tourne) | ⚠️ `CLAUDE.md` §4 mentionne encore « `--deselect` sur un seul test base64 (tests.yml:33) » et §10 liste CR-T01 ouvert — les deux sont périmés. |
| CR-T02 | P1† | Résolu (Round 1) | **RÉSOLU** | `tests/test_auth_guard.py:35-123` : 401 sans clé + 401 mauvaise clé + non-401 clé maître sur **6 routers gardés** (`accounts`/`categories`/`media`/`live`/`stream`/`sync`) + `POST /api/plex/generate` (`:89-111`) + `/api/health` public (`:119-123`). Les routers Pattern C **postérieurs** portent leurs propres tests de rejet : `tests/test_enrichment_backfill.py:125-138`, `tests/test_dav_router.py`, `tests/test_plex_downloads_json.py`, `tests/test_admin_downloads.py` (tous contiennent des asserts 401) | Couverture = **1 endpoint représentatif par router** (design assumé du filet : détecter un drop de `dependencies=_guard` au mount), pas une couverture par-endpoint. Réponse à la question posée : oui, le fichier existe et couvre réellement tous les routers gardés du Pattern A + le net s'est étendu aux nouveaux routers via leurs suites de feature. |
| CR-T03 | P1 | Résolu (Vague D) | **RÉSOLU** | `tests/test_sync_worker_orchestration.py` (vrai `sync_account`/`run_all_accounts`) | — |
| CR-T04 | P1 | Résolu (Vague D) | **RÉSOLU** | `tests/test_startup_wiring.py` (lifespan réel, faux `fcntl`, élection master/slave, `_PIPELINE_LOCK`) | Résidu déclaré (bodies des crons) = ressort de CR-A05, cohérent. |
| CR-T05 | P2 | Résolu (Vague A) | **RÉSOLU** | `tests/test_health_check_worker.py` + `tests/test_health_check_concurrency.py` (breaker, `_check_one`, sampling) | — |
| CR-T06 | P2 | Résolu (Vague D) | **RÉSOLU** | `tests/test_api_key_service.py` (mint/digest/resolve/revoke/expiry) | — |
| CR-T07 | P2 | Résolu (Vague D) | **RÉSOLU** | `tests/test_router_http_coverage.py` (stream/media/live/api_keys/accounts) | — |
| CR-T08 | P2 | Résolu (Vague D) — **a révélé un défaut non corrigé** | **RÉSOLU** (le finding fixture) ; **le défaut révélé reste OUVERT** | `tests/test_db_retry_real_lock.py:123` (vrai lock WAL fichier), `:218-258` (`TestCommitWithRetrySameSessionBoundary` : `pytest.raises(PendingRollbackError)` — le test **fige le comportement défaillant**, il ne le corrige pas) ; `app/utils/db_retry.py:39-43` n'attrape toujours que `OperationalError` | Statut exact à HEAD : la fidélité de fixture (l'objet de CR-T08) est résolue ; le défaut `commit_with_retry` même-session découvert par elle est **toujours présent**, sans ID de finding, promis à un « effort dédié » jamais réalisé. Voir CR-C04 et §3. |
| CR-T09 | debt | Résolu (Vague D) | **RÉSOLU** | `pyproject.toml:30` (`--cov-fail-under=70`) ; couverture réelle dernier run release = 77.97 % | Le ratchet n'a jamais été relevé : 8 points de mou entre le plancher (70) et le mesuré (78) — une régression de couverture de 7 pts passerait verte. |
| CR-T10 | debt | Résolu (Round 2) | **RÉSOLU** | `.github/workflows/tests.yml` : `push`/`pull_request` sur `[main, develop]` + job `lint` (ruff + black --check) | — |
| CR-T11 | debt | Résolu (Round 2) | **RÉSOLU** | `grep -c pytest.mark.asyncio tests/test_embedding_worker.py` = 0 | La moitié « constante 422 dépréciée en prod » est portée par CR-C09 (WON'T FIX justifié). |

---

## 2. Écarts déclaratif ⇆ réel

Aucun finding « vendu résolu » ne s'est révélé ouvert dans le code — **le log de remédiation du README clean-room
est exact à 100 % sur les 47 résolutions vérifiées** (résidus partiels compris, tous honnêtement déclarés).
Les écarts sont presque tous dans **l'autre sens** (déclaré ouvert / périmé, en réalité résolu) et se concentrent
dans `CLAUDE.md` :

| # | Source du statut faux | Affirmation | Réalité à HEAD | Sens |
|---|---|---|---|---|
| 1 | `CLAUDE.md` §10 (+ §9 piège 17d) | CR-S01 « écriture FS arbitraire post-auth — outputDir client verbatim » listé comme dette P1 **ouverte** | **Résolu** depuis Round 1 : confinement `plex.py:35-73` | vendu ouvert, en fait résolu |
| 2 | `CLAUDE.md` §10 (section « Dette ouverte » entière) | CR-F01, F02, F03, F04, F05, F06, F07, F08, F09, F11, C01, C02, C03, C04, T01…T08 décrits au présent comme dette vivante | Tous résolus ou partiels (cf. §1) — la section reproduit l'état **pré-remédiation** de l'audit et **contredit le bandeau du même fichier** (qui annonce « ~47 findings résolus ») | vendu ouvert, en fait résolu |
| 3 | `CLAUDE.md` §4 | « CI avec `--deselect` sur un seul test base64 (tests.yml:33) » | Deselect retiré ; le test tourne (CR-T01) | périmé |
| 4 | `CLAUDE.md` §9 piège 10 | « méthodes/headers `*` (main.py:385-386) » | Explicites depuis Round 1 (`main.py:548-549`) | périmé |
| 5 | `CLAUDE.md` §9 piège 8 / §3 | « writers request-path utilisent encore `db.commit()` nu (dette CR-C04) » | `commit_with_retry` câblé partout sur le chemin requête (tv_auth/accounts/categories/live) | périmé |
| 6 | `CLAUDE.md` §10 stack | « pydantic-settings≥2.1 (déclarée, non utilisée) » | Retirée de `requirements.txt` (CR-C07) | périmé |
| 7 | `CLAUDE.md` §2 | « main.py = 442 LOC » ; §2 db/ « chaîne 001→019 » ; §3 « chaîne 001→017 » | 661 LOC ; chaîne réelle 001→**022** (cohérente seulement dans §9 piège 6 / §10) | incohérences internes |
| 8 | `CLAUDE.md` bandeau | « À JOUR AU 2026-07-23, HEAD `1ac00d3`, v1.7.0 » | HEAD réel `9da9d46`, v1.7.1 (+ `38aeb5a`* fix prewarm DAV) — le bandeau se déclare lui-même autorité alors qu'il est en retard de 2 commits dont 1 release | périmé |
| 9 | README clean-room `:189-191` | CR-C10 : « `main.py`'s `_Acc` half still open » | Résolu à HEAD (`XtreamCredentials`, `main.py:160,177` ; 0 occurrence `_Acc`) | vendu ouvert, en fait résolu (seul écart du README — sens conservateur) |
| 10 | Board `31-board.md` (XD-01) | « échantillon `ORDER BY random()` CR-P06 » | Échantillonnage par ancre `rowid` depuis Vague A (`health_check_worker.py:329-361`) | description périmée |

\* commit fix DAV entre v1.7.0 et v1.7.1.

**Lecture d'ensemble** : la vérité opérationnelle vit dans le README clean-room (fiable) ; `CLAUDE.md` — pourtant
« autorité de vérité » auto-proclamée — porte une section §10 massivement contredite par son propre bandeau. Tout
agent qui lit §10 sans lire le log de remédiation re-signalera ~20 findings déjà fermés (et inversement, croira
CR-S01 exploitable).

---

## 3. Faux négatifs & trous de couverture

### 3.1 Findings perdus au changement de lignée (2026-06-15 → 2026-07-11)

L'ancienne lignée (78 findings) a été « supersedée » sans table de correspondance. La plupart des items ont un
équivalent `CR-*` 2026-07-11 ou ont été résolus en route. **Trois exceptions notables**, jamais reportées dans la
lignée courante et toujours vraies à HEAD :

| Ancien ID | Finding | État à HEAD | Preuve |
|---|---|---|---|
| **CR-F15 (2026-06-15)** | `write_file`/`download_image` **préservent tout fichier existant** → un NFO/poster généré n'est **jamais rafraîchi** après ré-enrichissement (nouvelles notes, ids corrigés, `display_rating` blendé…) | **Toujours vrai — jamais immatriculé en `CR-*`** | `app/plex_generator/storage.py:112-116` (`if full.exists(): return  # Preserve existing file`), `:119-122` (idem images). La conséquence est même **admise opérationnellement** ailleurs : le script `validate_id_consistency` et le board OMDB-01 notent « l'opérateur doit vider les NFO/posters générés correspondants, `LocalStorage` ne réécrit jamais un fichier existant ». Impact accru depuis le lot OMDb (les métadonnées durables évoluent désormais **plus souvent** que le fichier généré). |
| **CR-F02 (2026-06-15)** | `differential_cleanup` peut supprimer en masse sur un **fetch partiel** (listing tronqué non-vide) | **Partiellement couvert, jamais reporté** | La variante **épisodes** est gardée (`sync_worker.py:1451-1453`, `if success and rows:`) ; la variante **movie/show** ne l'est que par un garde non-vide `if all_vod_keys:` (`sync_worker.py:1249`) — un listing partiel *non vide* (page provider manquante sans erreur) délisterait encore les items absents. Cas plus étroit que l'original (le fetch listing lève normalement sur erreur), mais l'asymétrie épisodes-vs-films n'a jamais été tracée. |
| **CR-A01 (2026-06-15)** | Endpoints pipeline on-demand (`/api/sync/*`, `/plex/generate`) exécutables sur **n'importe quel worker** sans master-gate → double-run en multi-process | **Jamais reporté ; atténué par le déploiement, pas par le code** | `_PIPELINE_LOCK` (`main.py:115`) et les locks par compte (`sync_worker`) sont **process-local** ; aucune garde `is_master` sur les endpoints. Atténuation réelle : Dockerfile single-process + §9 piège 18c interdit `--workers N>1` (pour le throttle DAV). Le risque redevient réel si quelqu'un scale les workers. |

Non retenus (dropped à raison ou obsolètes) : old CR-S09 (creds dans URLs `.strm` — inhérent au design, re-documenté
au lot DAV), old CR-S10 (MD5 pour ids — non-secret), old CR-A09 (imports in-function — style), old CR-F16→F23
(mineurs, plusieurs absorbés par les fixes tv-auth/breaker), old CR-C04 PRAGMA (résolu — `database.py` applique
`busy_timeout` via `connect_args` sur tout le pool), old CR-C05/A11 version health (résolu, confirmé au smoke :
`/api/health` renvoie 1.7.1 live), old CR-F20 (rebuild re-scan lignes vides — non re-vérifié : **NON VÉRIFIABLE**,
impact mineur).

### 3.2 Défaut découvert mais jamais immatriculé

Le défaut **`commit_with_retry` même-session ↔ `PendingRollbackError`** (découvert pendant la Vague D, documenté
README:106 + bandeau CLAUDE.md, test de garde `tests/test_db_retry_real_lock.py:218-258` qui en **fige** le
comportement défaillant) n'a **aucun ID de finding** et n'apparaît sur aucun board actionnable. À HEAD,
`app/utils/db_retry.py:39-43` n'attrape toujours que `OperationalError` : la résilience lock du chemin requête est
partielle (atténuée par `busy_timeout=60 s`). **À immatriculer dans la série v1** (candidat naturel pour le
FINAL-REPORT).

### 3.3 Périmètre jamais audité en clean-room (livré après le 2026-07-11)

Tout ce qui suit a été livré avec des gates de review par-feature (code-reviewer/security-reviewer, board) mais
**sans audit indépendant** — c'est le périmètre neuf que les phases 0-8 du présent audit v1 doivent couvrir (je le
cartographie, je ne l'audite pas ici) :

| Lot (ordre chronologique) | Surface | Sensibilité |
|---|---|---|
| Feature « Télécharger » Xtream (`c440717`→`9361941` + extensions) | **1ʳᵉ capacité d'écriture disque** (`download_service`/`download_worker`, migration 018), redirects vettés, `.nfo` sidecar, granularité saisons/épisodes, `media.file_size` (migration 020) | Haute (FS + creds Xtream) |
| Feature « Télécharger Plex » C1→C7 | plex.tv token, `plex_server.access_token` chiffré, tables 019, fallback 403, worker partagé | Haute (secrets tiers) |
| Écran unifié + fix HTMX (`8e3ab35`/`e44da7e`) | 3ᵉ onglet admin, fusion cross-catalogue | Moyenne |
| Lot OMDb OMDB-01/02 (`302d125`→`56369d6`) | `omdb_service` (clé API jamais loggée), migration 022, enrichissement dual-provider, `display_rating` blendé, **deux formules `display_rating` coexistantes** (by design), backfill 202-job | Moyenne |
| **Relay WebDAV `/dav`** (`63f8425` + fix prewarm `38aeb5a`) | Nouveau module `app/dav/` complet, Basic Auth dédiée, relay d'octets vers URLs à credentials, throttle process-local, **aucun enforcement code** de l'exclusion tunnel (piège 18b), pas de rate-limit/lockout | **La plus haute** — le plus récent, zéro couverture d'audit |

À noter aussi : la croissance des routers **Pattern C** (5 désormais) et de `sync_worker.py` (+228 LOC) s'est faite
entièrement hors couverture d'audit.

---

## 4. Fiabilité du process de suivi

**Verdict : le suivi est fiable dans son exécution, défaillant dans sa consolidation.**

- **README clean-room (`docs/audit/cleanroom-2026-07-11/README.md`)** — **FIABLE**. 47/47 résolutions déclarées
  vérifiées exactes dans le code, résidus partiels systématiquement avoués (CR-P01 staleness, CR-P03 ILIKE,
  CR-C03 résidu, CR-A01 stream.py, CR-P08 skew, CR-F10 re-pick…), et son unique erreur (`_Acc` « still open »)
  est dans le sens conservateur. C'est **le seul document de pilotage exact** des findings.
- **`CLAUDE.md` §10 / §9** — **NON FIABLE en l'état**. La section « Dette ouverte » est un fossile de l'état
  pré-remédiation : ~20 findings y sont décrits au présent comme vivants alors qu'ils sont fermés (dont un P1
  sécurité, CR-S01), plusieurs pièges §9 décrivent du code disparu (CORS `*`, `db.commit()` nu, `--deselect`),
  et les métriques internes se contredisent (chaîne migrations 017/019/022 selon la section, main.py 442 vs 661).
  Le bandeau anti-dérive (« tout commit met à jour la section concernée ») **n'a pas été appliqué à §10** lors de
  la campagne `/fix-cleanroom` — seul le bandeau a été mis à jour. Risque concret : tout agent/audit s'ancrant
  sur §10 produit des faux positifs en série.
- **Board `docs/31-board.md`** — **FIABLE sur son périmètre, mais son périmètre n'est pas les findings**.
  C'est un board de features download ; les statuts spot-checkés sont exacts (DL-PLEX-03 résolu :
  `download_nfo.py:92` + `download_worker.py:44,226` ; UDL-01 résiduel confirmé ; CSRF honnêtement reporté
  DL-03/DL-PLEX-05/XD-03), avec une description périmée (XD-01 cite un `ORDER BY random()` disparu). **Aucun
  board ne pilote les 56 `CR-*`** : les follow-ups sécurité (S02/S04/S05/S07/S08), le défaut
  `PendingRollbackError` et les god-files vivent éparpillés entre README:108, bandeau CLAUDE.md et mémoire
  d'équipe — sans owner ni échéance.
- **Lignée 2026-06-15** — supersedée **sans table de correspondance**, ce qui a coûté au moins un vrai finding
  (old CR-F15, §3.1). Leçon de process : tout changement de schéma d'IDs doit tracer le devenir de chaque item.

---

## 5. Compteurs de synthèse (56 findings `CR-*` 2026-07-11)

| Statut vérifié à HEAD | Nombre | IDs |
|---|:--:|---|
| **RÉSOLU** | **37** | A02 A06 A07 · C01 C02 C05 C06 C07 C08 C10 · F01 F02 F04 F05 F06 F07 F08 F09 F10 F11 · S01 · P01 P02 P04 P06 P07 · T01 T02 T03 T04 T05 T06 T07 T08 T09 T10 T11 |
| **RÉSOLU-PARTIEL** | **10** | A01 A04 · C03 C04 · F03 · S03 S06 · P03 P05 P08 |
| **OUVERT** | **8** | A03 A05 (gardés volontairement, **dette en croissance**) · S02 S04 S05 S07 S08 S09 |
| **WON'T FIX (justifié)** | **1** | C09 (pin Starlette toujours en vigueur) |
| **RÉGRESSÉ** | **0** | — (aucune résolution vérifiée n'a été défaite) |

Points saillants pour le FINAL-REPORT :
1. **Le P0 (CR-P01) est réellement fermé** ; aucune résolution n'a régressé.
2. **Toute la dette ouverte restante est de la sécurité-hardening (6×S) ou des god-files gardés (2×A)** — et les
   deux familles se sont **aggravées en surface** depuis l'audit (CSRF ×3 onglets, pas de rate-limit sur `/dav`,
   `sync_worker` +16 %).
3. **Un défaut connu sans ID** (`commit_with_retry`/`PendingRollbackError`, §3.2) et **un finding perdu**
   (stale-NFO `storage.py:112-116`, §3.1) doivent être immatriculés `AUDIT-*` dans v1.
4. **`CLAUDE.md` §10 doit être resynchronisé** sur le log de remédiation — c'est aujourd'hui la principale source
   de faux diagnostics du repo (§2, §4).
