# Board — Feature « Télécharger » (téléchargement physique de médias)

> Spec : `docs/20-impl-media-download.md` · PRD : `docs/10-prd-media-download.md` · ADR : `docs/architecture/adr/0002-media-download-writes-to-disk.md`
> But du sprint : livrer les P0 F-001→F-008 (onglet admin + file persistante + worker master-only + confinement) sans régression.

## Definition of Done (tout ticket)
- Code sur `develop` · `pytest -v` **vert** · boot `uvicorn app.main:app` OK · `GET /api/health` **200**
- Migrations **idempotentes** (re-run sans casse) · **OpenAPI à jour** (schémas Pydantic v2, pas de dict nu)
- `run_with_retry` sur tout writer · aucun secret Xtream loggé · code-reviewer approuvé (+ security-reviewer si FS/creds)
- QA a exercé les critères d'acceptation du PRD §5

## Légende
- **Safe/Risky** : Risky = migration de schéma (needs-approval) **ou** surface sécurité (écriture FS + creds dans URLs).
- **Depends on** : arête dure (hard) = bloque le démarrage ; (soft) = contrat figé dans la spec, dev en parallèle, intégration au merge.

## Tickets

| PH-NNN | F-NNN | Titre | Owner | Status | Depends on | Safe/Risky | Estimate | Périmètre fichiers (DISJOINT) | Acceptance (PRD) |
|---|---|---|---|---|---|---|---|---|---|
| **PH-DL-01** | F-008 | Racine : migration 018 + entités ORM + schémas Pydantic | `db-migration-specialist` | **Done** | — | **Risky** (schéma, needs-approval) | M | `app/db/migrations.py` (append 018), `app/models/database.py` (append `DownloadJob`,`DownloadBatch`), `app/models/schemas.py` (append `Download*`) | US-008.1 : 018 idempotente en fin de chaîne, boot OK, `/api/health` 200 — **validé** : `_migration_018_create_download_tables` rejouée 2× (fresh DB via `create_all` + DB "upgradée" simulée) sans erreur ; `init_db()` (create_all+PRAGMA+migrations) rejoué 2× sur la DB réelle OK ; `app.main:app`/`openapi()` s'importent (60 routes, aucune régression) ; `pytest -v` = 735 passed (cov 73.03 %) ; `ruff check` vert sur les 3 fichiers. Note d'écart : le builder `to_download_response`/`compute_percent`/`compute_speed_bps` (§4/§6.4 de la spec) est **volontairement laissé à `download_service.py`** (PH-DL-03), pas ajouté ici dans `schemas.py` — la spec l'autorise dans l'un ou l'autre fichier, et `schemas.py` a un éditeur unique (cette ticket) par la règle de non-collision du board : un builder que PH-DL-03/04/05 doivent pouvoir étendre doit vivre dans un fichier qu'ils possèdent. |
| **PH-DL-03** | F-003/F-005/F-007 | `download_service` : enqueue, `compute_dest_path`+confinement, `download_to_disk`, list/cancel/retry + config `DOWNLOAD_*` | `backend-developer` (lot service) | todo | PH-DL-01 (hard) | **Risky** (FS + creds) | L | `app/services/download_service.py` (new), `app/config.py` (append) | US-003.1/3.3, US-005.1, US-007.1 : jobs persistés, `.part`→rename, 0 écriture hors `DOWNLOAD_DIR` |
| **PH-DL-04** | F-003/F-004/F-005/F-006 | `download_worker` : drain master-only, reap boot, progression, auto-retry, cancel coopératif | `backend-developer` (lot service) | todo | PH-DL-03 (hard), PH-DL-01 (hard) | **Risky** (FS + creds) | L | `app/workers/download_worker.py` (new) | US-004.1/4.2, US-005.2, US-006.1 : progression persistée, pas de `running` fantôme au boot, `failed` sans URL |
| **PH-DL-05** | F-001/F-002/F-004/F-006/F-101 | Routes+templates : HTMX `/admin/downloads` + JSON `/api/admin/downloads` (P1 lecture) + nav | `backend-developer` (lot routes) | todo | PH-DL-01 (hard), PH-DL-03 (**soft** — signatures figées §5) | **Risky** (surface admin FS) | M | `app/api/admin_downloads.py` (new), `app/api/downloads.py` (new), `app/templates/admin/downloads.html`+`_downloads_*.html` (new), `app/templates/admin/base.html` (edit nav) | US-001.1/1.2, US-002.1/2.2, F-004 queue, F-101 : 200/401/404 attendus, liste unifiée = `/api/media/*/unified` |
| **PH-DL-06** | F-003/F-008 | Wiring : mount des 2 routers + start `run_drain_loop` (master) | `backend-developer` (lot wiring) | todo | PH-DL-04 (hard), PH-DL-05 (hard) | Safe (2 lignes, mais touche lifespan) | S | `app/main.py` (edit — **seul** éditeur) | Worker démarre master-only ; routers montés ; boot OK |
| **PH-DL-07** | F-001..F-008/F-101 | Plan + exécution tests pytest (respx, SQLite éphémère) | `qa-engineer` | todo | spec (plan) ; exécution après PH-DL-03/04/05 | Safe | M | `tests/test_download_*.py` (new) | Cas §10 spec : confinement (bloquant), reprise Range, cancel, garde config, auto-retry, reap, routes 401/404 |
| **PH-DL-R1** | — | Review continue (contrat + conventions) | `code-reviewer` | continu | par ticket | Safe | — | (diff review) | Spec + conventions Python/FastAPI |
| **PH-DL-R2** | — | Review sécurité (FS confiné + creds Xtream) | `security-reviewer` | continu | PH-DL-03/04/05 | Safe | — | (diff review) | 0 fuite creds, confinement prouvé, pas de chemin client |

## DAG (arêtes de dépendance)

```
PH-DL-01 (migration+ORM+schemas)  ── hard ──▶ PH-DL-03 (service+config) ── hard ──▶ PH-DL-04 (worker) ── hard ──┐
      │                                                                                                          ├──▶ PH-DL-06 (wiring main.py)
      └── hard ──▶ PH-DL-05 (routes+templates) ◀── soft (contrats §5 figés) ── PH-DL-03 ────────── hard ─────────┘

PH-DL-07 (tests) : plan dès la spec ; exécution après 03/04/05.
Reviews (R1 continu, R2 sur 03/04/05).
```

## Ce qui tourne EN PARALLÈLE vs EN SÉRIE

**EN SÉRIE (spine série)** :
1. `PH-DL-01` (racine — migration/ORM/schemas fige les entités et le contrat DB). **Doit finir en premier** : Risky/needs-approval.
2. puis `PH-DL-03` (service+config) → `PH-DL-04` (worker) : le worker consomme les primitives du service (dépendance dure, même track/owner).
3. puis `PH-DL-06` (wiring `main.py`) : intégration finale, **seul** éditeur de `main.py` (évite tout conflit sur le god-file).

**EN PARALLÈLE (dès PH-DL-01 mergé)** :
- `PH-DL-05` (routes+templates, 2ᵉ dev) démarre **en parallèle** de `PH-DL-03/04` : fichiers **disjoints** (`api/` + `templates/`
  vs `services/` + `workers/`) et signatures de `download_service` **figées §5 de la spec** → dev contre l'interface, intégration au merge.
- `PH-DL-07` (qa) : **plan de test** écrit dès la spec (parallèle total) ; **exécution** après que 03/04/05 atterrissent.
- `PH-DL-R1`/`R2` : review en continu au fil des merges.

**Contrainte de non-collision** : `main.py`, `schemas.py`, `models/database.py`, `migrations.py` ont chacun **un seul** éditeur
(lot wiring pour `main.py` ; lot racine pour les 3 autres) → aucun merge-conflict entre tracks parallèles.

## Lancement parallèle (handoff tech-manager)

```
SPRINT « Télécharger » — But : P0 F-001→F-008 + F-101 lecture, additif, 0 régression
Lancement immédiat :
- db-migration-specialist  ← PH-DL-01 (migration 018 + ORM + schemas)        [P0, Risky/needs-approval]  ← RACINE
Dès PH-DL-01 mergé (parallèle) :
- backend-developer (A, lot service)  ← PH-DL-03 → PH-DL-04                    [P0, Risky]
- backend-developer (B, lot routes)   ← PH-DL-05 (contrats service figés §5)   [P0, Risky]
Puis (série) :
- backend-developer (wiring)          ← PH-DL-06 (après PH-DL-04 + PH-DL-05)    [P0]
Continu :
- qa-engineer      ← PH-DL-07 (plan dès maintenant, exécution après le code)   [P0]
- code-reviewer    ← R1 (tous)     · security-reviewer ← R2 (PH-DL-03/04/05)
Stretch (si capacité) : F-102 préflight disque · F-103 métriques · F-104 clear-finished · F-201/202/203 (P2)
DoD : pytest -v vert · boot uvicorn app.main:app · /api/health 200 · migrations idempotentes · OpenAPI à jour
```
</content>
