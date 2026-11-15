# Plan d'évolution fonctionnelle — PlexHub Backend (v1.17+)

> **Baseline :** `develop` @ `b8f2726` (`APP_VERSION = "1.16.0"`, déployé sur homelab `192.168.0.175`, migration courante **033**)
> **Règle d'exécution :** un lot par session via `/lot docs/plans/2026-11-15-functional-roadmap-plan.md`, un rédacteur à la fois, découpage par couche (≤ 2 paquets/ticket).

---

## Table des lots

| Lot | Thème | Priorité | Capacités couvertes | Statut |
|---|---|---|---|---|
| **Lot 7** | Intégrité catalogue & Cycle de vie NFO/Rétention | P1-A | Nettoyage épisodes orphelins, règle unique `display_rating`, invalidation ciblée NFO, purge rétention | ✅ Fait |
| **Lot 8** | Notifications proactives (Webhook / Discord / Notifiarr) | P1-B | Alertes panne fournisseur, fin batch téléchargement, échec backup, circuit-breaker | ✅ Fait |
| **Lot 9** | Centre de contrôle Admin (`/admin`) | P1-C | Gestion comptes Xtream, éditeur catégories, pilotage/suivi jobs, audit `metadata_edits` & `scrape_cache`, fusion onglets download | ✅ Fait |
| **Lot 10** | IA « Playable-first » & Nouveautés catalogue | P2-A | Filtrage disponibilité réelle sur `/api/ai/search` & `/rank`, filtres année/genre/note, endpoint `/api/media/recent` | ⏳ À faire |
| **Lot 11** | Pipeline Téléchargements avancé (Sous-titres auto & Plex HLS) | P2-B | Sidecar `.fr.srt` auto via `subtitle_service`, fallback HLS + remux `ffmpeg` sur partages Plex transcode-only | ⏳ À faire |
| **Lot 12** | Relais WebDAV `/dav` sans friction (Pré-cache & Observabilité) | P2-C | Cache header+tail intégré au relais `/dav`, métriques Prometheus DAV/downloads | ⏳ À faire |

---

## Lot 7 — Intégrité catalogue & Cycle de vie NFO/Rétention (P1-A)

- **Déclencheurs :** `code-reviewer` + `perf-benchmarker` (impact DB / I/O disque).
- **Tickets :**
  - **T7.1 — Nettoyage différentiel des épisodes orphelins (`CR-F01`)**
    - *Périmètre :* `app/workers/sync_worker.py`, `tests/test_sync_cleanup_partial_failure.py`.
    - *Critères :* `sync_account` exécute un `differential_cleanup_episodes` par série synchronisée avec succès (ou par batch de séries sans erreur upstream) afin de supprimer les épisodes retirés côté Xtream sans toucher aux séries en échec partiel ; `unified_group_service` reflète les suppressions.
  - **T7.2 — Unification du calcul `display_rating`**
    - *Périmètre :* `app/services/nfo_import_service.py`, `app/utils/rating_blend.py`, `tests/test_nfo_import_service.py`.
    - *Critères :* `nfo_import_service` utilise `rating_blend.blend_rating(imdb_rating, tmdb_rating)` (avec repli `COALESCE` uniquement si aucune note IMDb/TMDB n'est présente) afin d'éliminer toute oscillation entre import NFO et `enrichment_worker`.
  - **T7.3 — Régénération ciblée NFO/poster après édition manuelle**
    - *Périmètre :* `app/plex_generator/storage.py`, `app/services/plex_generation_service.py`, `app/services/manual_scrape_service.py`, `app/services/metadata_service.py`, tests dédiés.
    - *Critères :* `LocalStorage` supporte un mode `overwrite_keys: set[str]` ciblé ; lorsqu'une identité ou métadonnée est modifiée via `manual_scrape_service.apply_candidate` ou `PATCH /api/metadata/v1/items/...`, seul le dossier `.nfo`/poster concerné est marqué pour régénération (et optionnellement notifié à Plex/Jellyfin via webhook/API si configuré).
  - **T7.4 — Rétention automatique `metadata_edits` et `scrape_cache`**
    - *Périmètre :* `app/config.py`, `app/services/scrape_gateway_cache.py`, `app/services/metadata_service.py`, `app/services/startup_tasks.py` / `app/main.py` (cron nocturne), tests dédiés.
    - *Critères :* ajout des paramètres `SCRAPE_CACHE_MAX_ENTRIES` / purge nocturne des entrées expirées et excédentaires (LRU) + `METADATA_EDITS_RETENTION_DAYS` (défaut 365, `0` = désactivé) dans le cron nocturne `hour=3`.

---

## Lot 8 — Notifications proactives (Webhook / Discord / Notifiarr) (P1-B)

- **Déclencheurs :** `security-reviewer` (SSRF sur URL webhook + rédaction secrets) + `code-reviewer`. **Risky** (appels sortants configurables).
- **Tickets :**
  - **T8.1 — Service `notification_service` générique (Discord / Webhook JSON)**
    - *Périmètre :* `app/services/notification_service.py` (nouveau), `app/config.py`, `.env.example`, `tests/test_notification_service.py`.
    - *Critères :* variables `NOTIFY_WEBHOOK_URL`, `NOTIFY_ENABLED` (`false` par défaut), `NOTIFY_EVENTS` ; envoi async fire-and-forget via `httpx` avec timeout court (`5s`), déduplication anti-spam (cooldown par événement/clé), aucune fuite de secret/token dans les logs ou payloads.
  - **T8.2 — Câblage sur les événements critiques**
    - *Périmètre :* `app/services/account_outage_service.py`, `app/workers/download_worker.py`, `app/scripts/backup_db.py` / `app/services/startup_tasks.py`, tests associés.
    - *Critères :* déclenchement d'une notification structurée sur : (1) franchissement du seuil de panne fournisseur (`outage_strikes >= ACCOUNT_OUTAGE_STRIKES`) et rétablissement (`record_clear`), (2) fin d'un batch de téléchargement (`download_batch` terminé ou en échec), (3) échec de sauvegarde DB ou `integrity_check` KO.

---

## Lot 9 — Centre de contrôle Admin (`/admin`) (P1-C)

- **Déclencheurs :** `security-reviewer` (CSRF, masquage credentials Xtream, auth Basic) + `code-reviewer`.
- **Tickets :**
  - **T9.1 — Gestion des comptes Xtream & éditeur de catégories dans `/admin`**
    - *Périmètre :* `app/api/admin.py`, `app/templates/admin/accounts.html` (nouveau), `app/templates/admin/base.html`, `tests/test_admin_accounts.py`.
    - *Critères :* onglet `/admin/accounts` permettant de lister les comptes (mot de passe jamais renvoyé), tester la connexion, ajouter/activer/désactiver un compte, et éditer visuellement les catégories (`is_allowed`, mode whitelist/blacklist, filtre adulte) via HTMX + protection CSRF existante.
  - **T9.2 — Pilotage & suivi temps réel des jobs et sauvegardes**
    - *Périmètre :* `app/api/admin.py`, `app/templates/admin/_stats.html`, `app/templates/admin/jobs.html` (nouveau), `tests/test_admin_jobs.py`.
    - *Critères :* affichage de l'état des jobs (`job_registry` + `job_health` : `sync`, `epg_sync`, `enrichment`, `health_check`, `plex_generate`, `backup`), boutons de déclenchement manuel (protégés CSRF), et statut de la dernière sauvegarde (`backup_status_service`).
  - **T9.3 — Consolidation des onglets Téléchargements & Vue audit**
    - *Périmètre :* `app/templates/admin/base.html`, `app/api/admin_downloads.py`, `app/api/admin_plex_downloads.py`, `app/api/admin.py`, `app/templates/admin/audit.html` (nouveau).
    - *Critères :* correction de `catalogue_total` sur `admin_downloads` / `admin_plex_downloads` (`UDL-01`), regroupement sous l'entrée principale `Téléchargements` (avec sous-onglets source si besoin), et ajout d'une vue lecture seule des dernières entrées `metadata_edits` + stats `scrape_cache`.

---

## Lot 10 — IA « Playable-first » & Nouveautés catalogue (P2-A)

- **Déclencheurs :** `perf-benchmarker` (requêtes vectorielles + jointure catalogue) + `code-reviewer`.
- **Tickets :**
  - **T10.1 — Filtrage disponibilité réelle & filtres avancés sur `/api/ai/search` et `/rank`**
    - *Périmètre :* `app/services/recommendation_service.py`, `app/api/ai.py`, `app/models/schemas.py`, `tests/test_recommendation_service.py`.
    - *Critères :* ajout du paramètre optionnel `playableOnly: bool = True` (et filtres optionnels `genres`, `yearMin`, `yearMax`, `minRating`) ; croisement avec `media_group` / `media` (catégories autorisées, `is_broken = False`, exclusion des serveurs masqués par `account_outage_service`) ; sur-fetch adaptatif si `media_type` est filtré post-KNN afin de garantir `limit` résultats quand disponibles.
  - **T10.2 — Endpoint `/api/media/recent` (« Récemment ajoutés »)**
    - *Périmètre :* `app/services/media_service.py`, `app/api/media.py`, `app/models/schemas.py`, `tests/test_media_recent.py`.
    - *Critères :* `GET /api/media/recent?type=movie|show&days=7&limit=50` retournant les groupes unifiés apparus récemment (`created_at` / première apparition en sync), triés par date décroissante, respectant `is_adult`, catégories autorisées et `account_outage_service`.

---

## Lot 11 — Pipeline Téléchargements avancé : Sous-titres auto & Plex HLS (P2-B)

- **Déclencheurs :** `security-reviewer` (SSRF / ffmpeg args / path confinement) + `code-reviewer`.
- **Tickets :**
  - **T11.1 — Traduction automatique de sous-titres en post-téléchargement**
    - *Périmètre :* `app/config.py`, `app/workers/download_worker.py`, `app/services/download_service.py`, `app/services/subtitle_service.py`, tests dédiés.
    - *Critères :* option `DOWNLOAD_AUTO_SUBTITLES_LANG` (vide par défaut, ex. `fr`) ; après téléchargement réussi d'un média avec sous-titre externe ou flux SRT extractible (`ffmpeg`), génération best-effort d'un sidecar `.<lang>.srt` via `subtitle_service.translate_subtitles` (sans jamais faire échouer le job vidéo si Ollama est indisponible).
  - **T11.2 — Fallback HLS + remux `ffmpeg` sur partages Plex transcode-only**
    - *Périmètre :* `app/services/plex_download_service.py`, `app/services/download_service.py`, `tests/test_plex_download_hls.py`.
    - *Critères :* si `?download=1` ET le direct-play renvoient `403`, tentative optionnelle (activée via `PLEX_HLS_FALLBACK_ENABLED=false` par défaut) de capture du flux HLS transcodé + remux atomique en `.mp4`/`.mkv` dans le répertoire temporaire du job avant promotion finale.

---

## Lot 12 — Relais WebDAV `/dav` sans friction (Pré-cache & Observabilité) (P2-C)

- **Déclencheurs :** `perf-benchmarker` (mémoire/disque cache DAV, verrouillage permit) + `security-reviewer` (auth/local-only).
- **Tickets :**
  - **T12.1 — Cache header+tail intégré au relais `/dav`**
    - *Périmètre :* `app/dav/relay.py`, `app/dav/vfs.py`, `app/config.py`, `tests/test_dav_relay.py`.
    - *Critères :* mise en cache disque bornée (`DAV_PROBE_CACHE_DIR`, `DAV_PROBE_CACHE_MAX_MB`, opt-in) des premiers 16 Mo (header) et derniers 32 Mo (tail) lors des lectures `Range` ou d'un préchauffage en tâche de fond après `build_dav_tree`, permettant aux scans Plex de répondre localement sans verrouiller SQLite.
  - **T12.2 — Métriques Prometheus dédiées DAV & Téléchargements**
    - *Périmètre :* `app/utils/metrics.py`, `app/dav/relay.py`, `app/workers/download_worker.py`, `tests/test_metrics_dav_downloads.py`.
    - *Critères :* zéro-initialisation et exposition des compteurs/gauges manquants (`AUDIT-P8-002`) : `plexhub_dav_requests_total{method,status}`, `plexhub_dav_bytes_served_total`, `plexhub_download_jobs_total{source,status}`, `plexhub_download_queue_size`.
