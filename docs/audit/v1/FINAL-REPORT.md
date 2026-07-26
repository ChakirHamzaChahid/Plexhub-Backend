# Audit v1 — RAPPORT FINAL

> **Audit 360° indépendant `/audit-full`** — PlexHub Backend, branche `develop`, HEAD **`9da9d46`** (release **v1.7.1**), synthèse du 2026-07-26.
> Méthode : 4 auditeurs parallèles (phases 0-8 + DELTA), chaque fait re-prouvé au code `fichier:ligne` ou empiriquement (smoke serveur, curl, EXPLAIN QUERY PLAN, exécution node du hook). Aucune confiance accordée aux audits antérieurs ni à `CLAUDE.md` §9/§10 — et cette défiance s'est avérée justifiée (cf. « Convergence » ci-dessous).
> Détail par phase : `00-cartography.md` · `10-stability.md` · `20-security.md` · `30-perf.md` · `40-architecture.md` · `50-api-contracts.md` · `60-release-observability.md` · `DELTA.md`.

---

## 1. Gates runtime (mesurés pendant l'audit)

| Gate | Résultat |
|---|---|
| `pytest -v` | **1414 passed, 3 skipped**, couverture **77,98 %** (gate `--cov-fail-under=70` atteint), 114 s, 173 warnings. Python 3.12.10 local |
| Boot `uvicorn app.main:app` (DB **fraîche**) | OK, **102 routes**, chaîne de migrations **001→022** rejouée **sans aucun warning duplicate-column**, mode Slave (élection master shimée sous Windows) |
| `GET /api/health` | **200**, `version: 1.7.1` (= `APP_VERSION`, cohérent code ⇆ health ⇆ tag git ⇆ pattern GHCR) |
| `GET /api/media/movies` | **401** sans clé / **200** avec clé — auth fail-closed **confirmée empiriquement** |
| `GET /docs` | **401** (Basic Auth) |
| `GET /dav/` | **503** fail-closed (feature désactivée par défaut) |
| `GET /metrics` | **200 sans auth** (finding AUDIT-P2-001) ; 0 série `plexhub_*` — comportement **attendu** sur instance fraîche (métriques labellisées, aucune série avant le premier événement master ; tranché par le DELTA — ce n'est pas une régression, mais cela fonde AUDIT-P8-001) |

Aucun S1 : le serveur boote, migre, authentifie et répond.

---

## 2. Scorecard

| Axe | Note | Justification (preuves les plus fortes) |
|---|:--:|---|
| **Sécurité** | **B** | Fondations solides et **prouvées** : fail-closed sur toute l'API JSON (smoke 401/200), comparaisons temps-constant partout (`deps.py:44-48,132-142`), clés per-user en digest SHA-256, secrets chiffrés Fernet au repos (`models/database.py:140,577`), confinement F-007 + vetting SSRF sur les chemins récents, **0 injection SQL** (AUDIT-P2-010). Mais le hardening périmétrique reste ouvert : `/metrics` public avec labels `account_id` (P2-001, S2), **zéro rate-limit/anti-brute-force** avec flood DB anonyme via `/tv-auth/start` (P2-004, S2), CSRF admin, et aucune assertion structurelle garantissant qu'une future route `/api/*` sera authentifiée (P4-001, S2). |
| **Stabilité** | **B** | Boot/shutdown propres, migrations rejouables (0 warning vérifié), locks pipeline/validation effectifs (CR-F04 fermé), drain download résilient, tâches de fond sans fuite (§0 de `10-stability.md`). Mais `commit_with_retry` est **structurellement inopérant** en same-session (`PendingRollbackError` non rattrapée, ~30 call-sites — P1-001, S2) : la couche de résilience revendiquée par le piège 8 est en partie du théâtre, atténuée par `busy_timeout=60 s`. Échecs DDL avalés en WARNING (P1-002) et élection master pouvant basculer tout-esclave en silence (P1-003). |
| **État fonctionnel** | **B-** | Les flux vérifiés en profondeur sont sains : download unifié Xtream+Plex (routage/fallback 403 confirmés, P6-002), tv-auth (claim atomique + aliases, P6-003), `/plex/generate` confiné (P6-004). Mais deux cassures réelles : le **handshake `jobId` sync est mort de bout en bout** (status éternellement `unknown`, prouvé par curl — P5-001, S2) et le déblocage du scan Plex via `/dav` repose sur une **séquence manuelle hors-code non vérifiée** (P6-001, S2). S'y ajoute le finding perdu ré-immatriculé **AUDIT-P6-006** : les NFO/posters générés ne sont jamais rafraîchis (`storage.py:112-116`), impact accru depuis le lot OMDb. |
| **Performance** | **B-** | Les gros correctifs sont réels et **mesurés** : CR-P01 O(page) (~31 ms/page), keyset 1,3 ms vs 284 ms d'OFFSET, single-pass confirmé branché. Mais **aucun `ANALYZE`** : le planificateur ignore les 20 index et paie 113-284 ms de scan par requête chaude là où 0,6-25 ms suffisent (×188 prouvé sur le COUNT — P3-001, S2) ; la recherche filtrée reste O(catalogue) par frappe avec dégradation silencieuse si le snapshot échoue (P3-002, S2) ; l'agrégation `DatabaseSource` gèle la boucle ~750 ms de CPU pur, désormais sur le chemin de requête `/dav` (P3-003, S2). Les trois sont peu coûteux à corriger. |
| **Architecture** | **B-** | Les refactos revendiqués sont vérifiés : services extraits (CR-A01/A02), `build_versions` unique (CR-A07 clos sur preuve), file `download_job` partagée bien découpée (P4-006), isolation `dav`⇆générateur réelle. Mais les god-files **croissent au lieu de plafonner** (`sync_worker` +16 % → 1 618, `main.py` +50 % → 661, `media_service` +102 %, nouveau `download_service` 1 352 LOC sans ticket — P4-002), le garde-fou d'auth structurel promis n'existe pas (P4-001), et `display_rating` a **deux écrivains qui s'écrasent** mutuellement (oscillation COALESCE ⇆ blend — P4-005). |
| **API / Contrats** | **C+** | Le camelCase des réponses est tenu (0 propriété snake_case dans les schémas du spec), les motifs 503/422/413 IA sont conformes à la doc (P5-007, vérif exhaustive). Mais le pattern central 202+`jobId`→poll est **cassé silencieusement** (P5-001 + 200 `unknown` sur id inexistant P5-008), un **breaking change a été livré sans coordination** (`/api/media/episodes` → 400 sans `server_id`, P5-004, S2), deux wire-changes sont partis sans détection (P5-005), et la convention des params de requête (100 % snake_case) n'est actée nulle part (P5-003). |
| **Release / Observabilité** | **C** | Versioning v1.7.1 cohérent bout-en-bout (P7-001) et CI réellement verte avec gates (triggers `develop`, cov 70, ruff). Mais 4 S2 sur l'axe : le **compose de référence produit un conteneur inutilisable** (`AI_API_KEY`/`ADMIN_PASSWORD` + 18 clés non injectées → 100 % API en 401 — P7-006), le **détecteur anti-dérive est inerte** (regex `NO MATCH` prouvé sous node — P7-003), l'**alerting par absence est impossible** (0 série avant le 1ᵉʳ événement, aucune métrique de fraîcheur — P8-001) et les sous-systèmes les plus risqués (downloads, DAV, OMDb, sync Plex) ont **zéro métrique** (P8-002). Un pipeline mort peut passer inaperçu 6 h+ × N. |
| **Tests / Qualité** | **B+** | 1414 verts / 77,98 %, les 11 findings tests CR-T01→T11 **tous résolus et vérifiés** (tests 401 par router, vrai lock WAL, orchestration sync testée, deselect CI supprimé). Réserves : le ratchet de couverture n'a jamais été relevé (8 pts de mou entre 70 et 78), `black --check` est un no-op structurel (exclut `app/`+`tests/` — P7-004), Python 3.12 (la version livrée) n'est jamais testé en CI (P7-005), et aucun test trigger→status n'existe — c'est précisément le trou qui a laissé passer P5-001. |

---

## 3. Verdict global

Ce backend est **fondamentalement sain et en amélioration réelle** : l'auth fail-closed est prouvée empiriquement, la suite de tests est massive et verte, le P0 de l'audit clean-room (CR-P01) est réellement fermé, et **aucune des 47 résolutions vérifiées n'a régressé** — la campagne `/fix-cleanroom` a tenu ses promesses, résidus honnêtement déclarés compris. Il n'y a aucun S1, aucune vulnérabilité exploitable pré-auth identifiée, et les features récentes à haut risque (écriture disque, tokens Plex, relay d'URLs à credentials) ont des primitives de sécurité correctes. Mais trois faiblesses réelles empêchent de parler de sérénité : (1) des **cassures fonctionnelles silencieuses** livrées en production — handshake `jobId` mort, breaking change épisodes non coordonné, NFO générés jamais rafraîchis — dont aucune ne produit d'erreur visible ; (2) une **exploitation aveugle** — pas de signal proactif sur les workers, compose de référence inopérant, un master figé indétectable ; (3) une **doc de pilotage devenue trompeuse** (voir ci-dessous), précisément parce que son garde-fou automatique est cassé. Rien d'alarmant, mais le coût du prochain incident sera dominé par ces trois angles morts, pas par le code lui-même.

### Convergence indépendante : `CLAUDE.md` §10 est la principale source de faux diagnostics du repo

Deux agents, travaillant séparément et sans se voir, sont arrivés à la même conclusion : l'agent phase 0 (AUDIT-P0-002 : « ≥10 `CR-*` listés ouverts sont résolus au code », 11 preuves) et l'agent DELTA (tableau §2 : « ~20 findings décrits au présent comme dette vivante sont fermés », 23 items énumérés, dont **CR-S01 — un P1 sécurité présenté comme exploitable alors qu'il est confiné depuis `plex.py:35-73`**). Les deux comptes ne se contredisent pas : ce sont deux échantillonnages du même fossile. **Chiffre réconcilié (union des deux listes, chaque statut tranché par le tableau maître DELTA §1, lui-même prouvé au code)** : **28 findings distincts** sont présentés dans `CLAUDE.md` §10/§9 comme dette vivante alors qu'ils sont **fermés (24 RÉSOLUS)** ou **partiellement fermés (4 RÉSOLUS-PARTIELS : F03, C03, C04, S06)** à HEAD — et c'est un plancher, la section §10 reproduisant en bloc l'état pré-remédiation. La cause racine est identifiée (AUDIT-P7-003) : le détecteur SessionStart ne matche plus le format du bandeau (`NO MATCH` prouvé) et échoue en silence — le dispositif anti-dérive entier est inerte. Conséquence opérationnelle immédiate : tout agent ou développeur qui s'ancre sur §10 re-fixe des non-problèmes, ou — pire — croit CR-S01 exploitable. À l'inverse, le **README clean-room est fiable à ~100 %** (47/47 résolutions exactes, son unique erreur étant dans le sens conservateur) : c'est lui, pas §10, qui doit servir de base au recalage.

---

## 4. DELTA condensé

**Compteurs sur les 56 findings `CR-*` (2026-07-11), vérifiés au code à HEAD :**

| Statut | Nombre | Détail |
|---|:--:|---|
| **RÉSOLU** | **37** | dont le P0 CR-P01, le P1 sécurité CR-S01, et les 11 findings tests |
| **RÉSOLU-PARTIEL** | **10** | résidus tous honnêtement déclarés (A01 stream.py, C04 same-session, P03 ILIKE…) |
| **OUVERT** | **8** | A03/A05 (god-files gardés, **dette en croissance**) · S02 S04 S05 S07 S08 S09 (hardening) |
| **WON'T FIX (justifié)** | **1** | C09 (bloqué par le pin fastapi/instrumentator, justification toujours valide) |
| **RÉGRESSÉ** | **0** | aucune résolution défaite |

**Les écarts déclaratif ⇆ réel qui comptent** : les 28 findings « vendus ouverts, en fait fermés » de `CLAUDE.md` §10/§9 (cf. §3 ci-dessus) ; les pièges §9 décrivant du code disparu (CORS `*` méthodes/headers, `db.commit()` nu, `--deselect` CI) ; le bandeau en retard de 2 commits dont 1 release ; et côté inverse (unique, conservateur) le README clean-room déclarant `_Acc` encore ouvert alors qu'il est résolu. Le board `31-board.md` est fiable sur son périmètre (features download) mais **aucun board ne pilote les 8 `CR-*` restants** — ils vivent éparpillés sans owner.

**Deux items immatriculés par cette synthèse** (identifiés par le DELTA comme sans ID actionnable) :

| Nouvel ID | Sév | Finding | Preuve | Origine |
|---|:--:|---|---|---|
| **AUDIT-P1-001** (déjà posé en phase 1) | S2 | Défaut `commit_with_retry` same-session : le retry est annulé par `PendingRollbackError` (~30 call-sites, une seule vraie tentative) — découvert en Vague D, documenté partout, **jamais immatriculé ni corrigé** ; le test `test_db_retry_real_lock.py:218-258` **fige** le comportement défaillant | `app/utils/db_retry.py:39-45,56-62` | DELTA §3.2 |
| **AUDIT-P6-006** (immatriculé ici) | S3 | **Finding perdu au changement de lignée** (old CR-F15 du 2026-06-15) : `write_file`/`download_image` préservent tout fichier généré existant → **NFO/posters jamais rafraîchis après ré-enrichissement** (nouvelles notes OMDb, ids corrigés par `validate_id_consistency`, `display_rating` blendé — les métadonnées durables évoluent désormais plus souvent que les fichiers générés). Conséquence admise opérationnellement (« l'opérateur doit vider les NFO générés ») mais jamais tracée en finding | `app/plex_generator/storage.py:112-116,119-122` | DELTA §3.1 |

Deux autres pertes de lignée moindres restent consignées au DELTA §3.1 (garde fetch-partiel asymétrique movies/shows vs épisodes ; endpoints pipeline sans master-gate, atténué par le déploiement single-process) — à traiter lors des chantiers V2/V3, pas immatriculées faute d'impact démontré au déploiement actuel.

---

## 5. Top-10 priorisé

Ordre de risque **Sécurité → État fonctionnel → Perf → Architecture**, pondéré par le ratio gain/effort. Arbitrages assumés : AUDIT-P3-001 (perf) monte au rang 2 car c'est le meilleur ratio de tout l'audit (196 ms one-shot pour ×188 sur chaque requête chaude) ; AUDIT-P2-004 (sécurité) descend au rang 5 car l'effort est moyen et une mitigation ingress (WAF Cloudflare) existe à court terme ; P7-003+P0-002 sont fusionnés (cause racine + symptôme, même remédiation).

| # | ID | Sév | Titre | Preuve | Impact concret | Effort | Commande |
|:-:|---|:--:|---|---|---|:--:|---|
| 1 | AUDIT-P2-001 | S2 | `/metrics` public sur la même app que le tunnel (= CR-S02) | `utils/metrics.py:46-51` ; smoke 200 sans clé | Reconnaissance de la carte des routes + fuite des `account_id` à quiconque atteint le tunnel | S | `/fix-cleanroom` (CR-S02) |
| 2 | AUDIT-P3-001 | S2 | Aucun `ANALYZE`/`sqlite_stat1` : le planificateur ignore les 20 index | mesures EQP : COUNT 113,5 ms → **0,6 ms (×188)** après `ANALYZE` (196 ms one-shot) | ~100-280 ms payés sur **chaque** requête de liste/recherche/health-check, linéaire au catalogue | S | `/benchmark`→`/fix-bench-perf` |
| 3 | AUDIT-P5-001 | S2 | `jobId` des 5 triggers sync = handle mort, status éternellement `unknown` | `sync.py:29` vs `sync_worker.py:1100` ; curl 200 `unknown` | Le pattern 202+poll de toute l'API de déclenchement est cassé silencieusement pour tout client (app, scripts, admin) ; aucune issue de pipeline observable (P6-005) | S | `/app-build` |
| 4 | AUDIT-P7-003 + AUDIT-P0-002 | S2 | Détecteur anti-dérive inerte (regex `NO MATCH`, échec avalé) + `CLAUDE.md` §10/§9 : 28 findings faussement « ouverts » | `session-start.js:17` + node ; DELTA §2 | Toute la dérive doc vient de là ; chaque futur agent/dev part sur de faux diagnostics (dont « CR-S01 exploitable ») | trivial | `/sync-context` (+ fix regex du hook) |
| 5 | AUDIT-P2-004 | S2 | Zéro rate-limit : flood DB anonyme via `/tv-auth/start`, brute-force Basic/X-API-Key non throttlé (= CR-S05, aggravé par `/dav`) | `tv_auth.py:60,183-249` ; grep limiter = 0 | DoS applicatif trivial depuis le tunnel public ; force brute praticable (rotation du password = seule révocation `/dav`) | M | `/fix-cleanroom` (CR-S05) |
| 6 | AUDIT-P1-001 | S2 | `commit_with_retry` structurellement inopérant en same-session (`PendingRollbackError`) — ~30 call-sites | `utils/db_retry.py:39-45,56-62` | Sous vraie contention >60 s, crash au 1ᵉʳ échec avec une trace trompeuse ; la résilience du piège 8 est partiellement du théâtre | S-M | `/fix-cleanroom` (résiduel CR-C04) |
| 7 | AUDIT-P7-006 | S2 | `docker-compose.yml` de référence inopérant : `AI_API_KEY`/`ADMIN_PASSWORD` + 18 clés non injectées | diff config⇆compose ; `deps.py:44-48,95-99,120-124` | `docker-compose up` tel que livré → 100 % de l'API en 401 sans issue, admin/docs 503 — fail-closed mais inutilisable et trompeur | S | `/app-build` |
| 8 | AUDIT-P3-003 | S2 | Agrégation `DatabaseSource` sur la boucle d'événements (239+504 ms CPU mesurés), désormais sur le chemin de requête `/dav` | `source.py:132,202` vs `media_service.py:329` | ~750 ms+ de gel de boucle à chaque génération ET rebuild d'arbre DAV — allonge les transactions Plex (cascade `database is locked` documentée) | S | `/fix-bench-perf` |
| 9 | AUDIT-P3-002 | S2 | CR-P01 à moitié : toute requête filtrée (search/genre/year) retombe en O(catalogue) ; échec de rebuild du snapshot = dégradation **silencieuse** | `media_service.py:274-283,368-369` ; `unified_group_service.py:129-133` ; ~230-350 ms/frappe mesurés | La recherche — chemin UX critique — paie des full-scans par frappe ; aucune métrique ne distingue snapshot/live | S (métrique) / M (FTS5) | `/benchmark`→`/fix-bench-perf` |
| 10 | AUDIT-P4-005 | S3 | `display_rating` : deux écrivains, le recompute blend écrase la formule NFO sur toute ligne à note IMDb/TMDB → oscillation selon l'ordre des jobs | `nfo_import_service.py:531-541` vs `rating_blend.py:64-79` | « Note qui change toute seule » visible par l'app, difficile à diagnostiquer ; le module « single source of truth » ne l'est pas | S | `/app-build` |

Hors Top-10 mais à ne pas perdre : **AUDIT-P4-001** (assertion de boot auth manquante — le garde-fou le moins cher rapporté au risque couvert, planifié en V2), **AUDIT-P8-001/002** (chantier observabilité, V3), **AUDIT-P5-004** (vérifier côté app Android que le 400 `server_id` ne casse aucun client legacy — action immédiate triviale : log WARN dédié).

---

## 6. Roadmap en 3 vagues

### V1 — Quick wins (quelques jours) : reprendre la main sur le vrai état + gains gratuits
- **Objectif** : que la doc redevienne fiable, que le déploiement de référence marche, et empocher le ×188.
- **Items** : recalage complet `CLAUDE.md` (bandeau, §2, §3, §4, §5, §9, §10 aligné sur DELTA §5 — AUDIT-P0-001/002/003, P7-002, + board XD-01) ; fix du regex du hook SessionStart + `else` loggant « bandeau non parsable » (P7-003) ; `ANALYZE` one-shot + `PRAGMA optimize` en fin de pipeline (P3-001) ; compléter le bloc `environment:` du compose ou `env_file: .env` (P7-006) ; compléter `.env.example` (P7-008, dont les 3 secrets de sécurité) ; log WARN sur le 400 `server_id` (P5-004) ; en interim sécurité : exclure `/metrics` au niveau ingress.
- **Commandes** : `/sync-context` (doc+hook) · `/fix-bench-perf` (ANALYZE) · `/app-build` (compose/.env.example).
- **Critère de sortie** : le hook matche le bandeau réel et signale la dérive sur test ; `docker-compose up` livre une API utilisable ; COUNT films < 5 ms sur la base réelle ; `CLAUDE.md` §10 == DELTA §5 (8 OUVERTS seulement).

### V2 — Correctifs de fond (1-2 sprints) : réparer les contrats et la résilience réelle
- **Objectif** : plus aucune cassure fonctionnelle silencieuse ; la résilience revendiquée devient effective.
- **Items** : handshake `jobId` bout-en-bout + 404 sur id inconnu + jobs pipeline/enrichment/validation trackés (P5-001, P5-008, P6-005, avec test trigger→status) ; migration des writers vers `run_with_retry` session-fraîche (P1-001/CR-C04 — inverser le test qui fige le défaut) ; `aggregate_movies`/`aggregate_series` off-loop dans `source.py` (P3-003) ; métrique `plexhub_unified_path{snapshot|live}` (P3-002 volet visibilité) ; assertion de boot sur `app.routes` (P4-001 — ~20 lignes) ; convergence `display_rating` sur `rating_blend` (P4-005) ; politique de refresh des NFO/posters générés (P6-006 — au minimum une option `--force-refresh` métadonnées) ; distinguer `BlockingIOError` des autres `OSError` à l'élection master + exposer `is_master` dans `/api/health` (P1-003, P8-005).
- **Commandes** : `/app-build` (backlog `AUDIT-*`) · `/fix-cleanroom` (CR-C04, CR-A04).
- **Critère de sortie** : test trigger→status vert sur les 5 triggers ; `TestCommitWithRetrySameSessionBoundary` réécrit pour asserter le retry réussi ; aucune agrégation >50 ms sur la boucle ; une seule formule `display_rating` (ou oscillation documentée ET testée) ; boot échoue si une route `/api/*` hors allow-list est sans garde.

### V3 — Structurel & hardening (trimestre) : prod-grade sur tunnel public
- **Objectif** : la surface publique tient sans dépendre de la discipline ni du WAF ; les incidents workers deviennent visibles avant les utilisateurs.
- **Items** : rate-limit middleware + cap de sessions `pending` sur `/tv-auth/start` (P2-004/CR-S05) ; CSRF via `Sec-Fetch-Site` sur POST `/admin*` (P2-005/CR-S07) ; auth `/metrics` (P2-001/CR-S02, en dur cette fois) ; chantier observabilité « fraîcheur + files » — gauges `last_success_timestamp` par job, métriques downloads/DAV/OMDb/sync Plex, zéro-init des labels énumérables (P8-001/002/005, F-103) ; retrofit `assert_public_redirect_host` sur images + health-check (P2-008/CR-S08) ; séparation de domaine sur la clé Fernet tv-auth (P2-003/CR-S04) ; cliquet LOC sur les 4 god-files + ticket de découpe `download_service` (P4-002) ; matrice CI 3.12+3.13 (P7-005) ; `USER` non-root + `HEALTHCHECK` image (P7-007) ; cache header/tail intégré au relay DAV (correctif pérenne de P6-001) puis **audit incrémental dédié du périmètre `/dav` sous charge réelle**.
- **Commandes** : `/fix-cleanroom` (S02/S04/S05/S07/S08) · `/app-build` (observabilité, Docker) · `/wf-audit-incremental` (périmètre DAV post-correctifs).
- **Critère de sortie** : brute-force mesurablement throttlé ; une alerte « pipeline mort » se déclenche en test de chaos (master figé) ; scan Plex sans préchauffage manuel ; aucun des 4 fichiers ne dépasse son LOC courant en CI.

---

## 7. Trou de couverture d'audit

Tout le périmètre livré **après le 2026-07-11** (feature « Télécharger » Xtream, « Télécharger Plex » C1→C7, écran unifié, lot OMDb/dual-provider, relay `/dav`) n'avait vu que des reviews par-feature, **jamais d'audit indépendant** avant ce v1.

**Ce que v1 en a couvert** : primitives de sécurité et de secrets (confinement F-007, redirects vettés DL-01, `access_token` chiffré, non-fuite des URLs Xtream — phase 2, tout vérifié tenant) ; câblage fonctionnel du worker partagé + fallback 403 (P6-002, sain) ; contrat et auth du router `/dav` (P6-001, fail-closed confirmé) ; frontières de modules `dav` (P4-007, deux cycles à imports différés) ; coût perf du build d'arbre et du shim Range (P3-003/P3-005/P3-007) ; miroirs JSON et backfill OMDb (P6-002, conformes) ; l'angle mort d'observabilité de tout ce périmètre (P8-002).

**Ce qui reste à approfondir** (personne ne l'a encore fait) : le **relay `/dav` sous charge réelle** — throttle en concurrence, shim Range sur vrais panels, comportement du pool httpx en erreur prolongée, et le correctif pérenne header/tail (v1 a audité le code, pas le comportement sous un vrai scan Plex) ; les cas limites du transfert download (TOCTOU/symlink DL-04/05, actés non-exploitables sur cible Linux mais jamais testés) ; les écrans admin HTMX au-delà du contrat (v1 n'a pas rejoué les parcours navigateur) ; et plus généralement **tout le comportement contre de vrais providers** — l'audit entier a tourné sur smoke DB vide + mesures sur la base locale, jamais contre un panel Xtream ou un PMS vivant. Le `/wf-audit-incremental` DAV planifié en V3 est la suite naturelle.

---

## 8. Prochaines actions (commandes)

1. **`/sync-context`** — immédiat : recalage doc + fix du hook (Top-10 #4, V1).
2. **`/fix-bench-perf`** — `ANALYZE` + off-loop (Top-10 #2/#8, mesures de cette v1 comme baseline).
3. **`/app-build`** — backlog `AUDIT-*` V2 (jobId, compose, display_rating, assertion boot).
4. **`/fix-cleanroom`** — les 8 `CR-*` restants (S02/S04/S05/S07/S08/S09 + god-files A03/A05 via cliquet) + résiduel C04.
5. Pas de `/incident` : aucun S1 avéré.
