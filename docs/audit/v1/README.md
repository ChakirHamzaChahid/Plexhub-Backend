# Audit v1 — Index (`docs/audit/v1/`)

> **Audit 360° indépendant `/audit-full`** — première itération de la série versionnée `AUDIT-*`.
> **Cible** : PlexHub Backend, branche `develop`, HEAD **`9da9d46`** (release **v1.7.1**). **Date** : 2026-07-26.
> **Méthode** : 4 auditeurs parallèles (cartographie+stabilité+sécurité · perf+architecture · API/contrats+features · release/observabilité+DELTA) + synthèse. Phases 0-8 couvertes, lecture seule sur le code, chaque fait re-prouvé `fichier:ligne` ou empiriquement. Aucune confiance accordée aux audits antérieurs ni à `CLAUDE.md` §9/§10 (défiance validée : cf. DELTA §2 et FINAL-REPORT §3).

## Gates runtime

- `pytest -v` : **1414 passed, 3 skipped**, couverture **77,98 %** (gate 70 atteint), 114 s. Python 3.12.10 local.
- Boot `uvicorn app.main:app` sur DB fraîche : OK, **102 routes**, migrations **001→022** rejouées **sans warning duplicate-column**, mode Slave (élection shimée Windows).
- `GET /api/health` → 200 `version: 1.7.1` (cohérent) · `/api/media/movies` → 401 sans clé / 200 avec · `/docs` → 401 · `/dav/` → 503 fail-closed · `/metrics` → 200 sans auth (0 série `plexhub_*` sur instance fraîche — attendu, métriques labellisées).

## Fichiers

| Fichier | Contenu |
|---|---|
| [`00-cartography.md`](00-cartography.md) | Phase 0 — modèle mental prouvé (28 106 LOC, 21 routers, chaîne 001→022, double engine DB non documenté) + écarts doc⇆code (3 findings). |
| [`10-stability.md`](10-stability.md) | Phase 1 — modes de panne : `commit_with_retry` inopérant same-session (S2), DDL avalés, élection master silencieuse, pools ; + 10 points vérifiés sains. |
| [`20-security.md`](20-security.md) | Phase 2 — matrice d'auth complète prouvée (fail-closed confirmé), secrets vérifiés ; `/metrics` public et zéro rate-limit (2 S2), CSRF/CORS/SSRF résiduels ; 0 injection SQL. |
| [`30-perf.md`](30-perf.md) | Phase 3 — mesures sur la vraie base (102 721 lignes `media`) : `ANALYZE` manquant (×188 prouvé), fallback filtré O(catalogue), agrégation sur la boucle (750 ms CPU) ; CR-P01/P04/P07 confirmés efficaces. |
| [`40-architecture.md`](40-architecture.md) | Phase 4 — assertion d'auth au boot promise et absente (S2), god-files en croissance (+16 %/+50 %/+102 %), oscillation `display_rating`, cycles `dav` à imports différés ; CR-A06/A07 clos sur preuve. |
| [`50-api-contracts.md`](50-api-contracts.md) | Phases 5-6 — `jobId` sync mort de bout en bout (S2, prouvé curl), breaking `server_id` livré (S2), DAV = pansement ops (S2) ; motifs 503 IA, tv-auth et download unifié vérifiés conformes. |
| [`60-release-observability.md`](60-release-observability.md) | Phases 7-8 — compose de référence inopérant (S2), hook anti-dérive inerte (S2, `NO MATCH` prouvé), alerting par absence impossible + zéro métrique downloads/DAV/OMDb (2 S2) ; versioning v1.7.1 cohérent, CI saine. |
| [`DELTA.md`](DELTA.md) | Re-vérification indépendante des 56 `CR-*` + balayage lignée 2026-06-15 + board : statuts prouvés au code, écarts déclaratif⇆réel, findings perdus, fiabilité des sources de suivi. |
| [`FINAL-REPORT.md`](FINAL-REPORT.md) | **Synthèse** : scorecard 8 axes, verdict, DELTA condensé, **[Top-10 priorisé](FINAL-REPORT.md#5-top-10-priorisé)**, roadmap V1/V2/V3, trou de couverture d'audit. |

## Compteurs de findings `AUDIT-*`

**67 IDs au total** : 66 dans les fichiers de phase + 1 immatriculé au FINAL-REPORT (**AUDIT-P6-006**, finding perdu old CR-F15 — NFO générés jamais rafraîchis). Recomptés depuis les fichiers :

| Sévérité | Total | Détail |
|---|:--:|---|
| **S1** | **0** | — |
| **S2** | **15** | P0-002 · P1-001 · P2-001, P2-004 · P3-001, P3-002, P3-003 · P4-001 · P5-001, P5-004 · P6-001 · P7-003, P7-006 · P8-001, P8-002 |
| **S3** | **37** | 36 en phases + AUDIT-P6-006 (FINAL-REPORT) |
| **dette** | **6** | P1-006, P1-008, P1-009 · P2-009 · P3-009 · P4-008 |
| **info / clos / RAS** | **9** | vérifications positives consignées (P2-010, P4-004, P4-006, P5-007, P6-002/003/004, P7-001, P8-004) |

NB : AUDIT-P4-002 (« S3/dette » dans son fichier) est compté S3.

| Phase | Findings | S2 | S3 | dette | info/clos |
|---|:--:|:--:|:--:|:--:|:--:|
| 0 — Cartographie | 3 | 1 | 2 | — | — |
| 1 — Stabilité | 9 | 1 | 5 | 3 | — |
| 2 — Sécurité | 10 | 2 | 6 | 1 | 1 |
| 3 — Performance | 9 | 3 | 5 | 1 | — |
| 4 — Architecture | 8 | 1 | 4 | 1 | 2 |
| 5 — API/Contrats | 8 | 2 | 5 | — | 1 |
| 6 — Features | 5 (+1) | 1 | 1 (+1) | — | 3 |
| 7 — Release | 8 | 2 | 5 | — | 1 |
| 8 — Observabilité | 6 | 2 | 3 | — | 1 |
| **Total** | **66 (+1)** | **15** | **36 (+1)** | **6** | **9** |

## Compteurs DELTA (56 findings `CR-*` 2026-07-11)

**37 RÉSOLUS · 10 RÉSOLUS-PARTIELS · 8 OUVERTS · 1 WON'T FIX justifié · 0 RÉGRESSÉ.**
Le README clean-room s'avère fiable à ~100 % (47/47 résolutions exactes) ; c'est `CLAUDE.md` §10 et les pièges §9 qui ont dérivé — **28 findings** y sont présentés comme dette vivante alors qu'ils sont fermés ou partiellement fermés à HEAD (chiffre réconcilié entre AUDIT-P0-002 et DELTA §2, cause racine = hook anti-dérive inerte AUDIT-P7-003). Détail : [`DELTA.md`](DELTA.md) §2/§5 et [`FINAL-REPORT.md`](FINAL-REPORT.md) §3-4.

## Par où commencer

→ **[FINAL-REPORT.md — Top-10 priorisé](FINAL-REPORT.md#5-top-10-priorisé)** puis la roadmap V1 (quick wins : `/sync-context`, `ANALYZE`, regex du hook, compose).
