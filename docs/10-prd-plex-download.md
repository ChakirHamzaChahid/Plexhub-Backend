# PRD — Téléchargement physique depuis des serveurs Plex partagés (« Télécharger Plex »)

> Statut : livré (backend, tickets C1→C7) · Cible : backend PlexHub (FastAPI / Python 3.13) · Migration en tête = 018 → **019** pour cette feature · **Additif & rétrocompatible** — n'altère ni `/api/media`, ni `/api/plex`, ni la génération `.strm`, ni l'onglet admin « Télécharger » (Xtream) existant.
> Handoff : miroir produit de `docs/10-prd-media-download.md` (feature « Télécharger », Xtream) — mêmes principes produit (file persistante, worker master-only, confinement d'écriture, jamais de secret exposé), **source de catalogue différente** : au lieu du catalogue Xtream déjà synchronisé (`media`), cette feature parcourt les **serveurs Plex Media Server partagés avec le compte plex.tv de l'opérateur**.

---

## 1. Résumé produit

Un second onglet admin **« Télécharger Plex »** (`/admin/plex-downloads`, HTMX) permet à l'opérateur de parcourir le catalogue **dédupliqué** de tous les serveurs Plex Media Server (PMS) accessibles via son compte plex.tv — qu'il les possède ou qu'ils lui soient **partagés** par un tiers — puis de sélectionner un film (une source), ou une série (source entière ou granularité saison/épisode), et de déclencher le **téléchargement physique** des fichiers vidéo vers le **même** dossier serveur `DOWNLOAD_DIR` que la feature « Télécharger » Xtream. La file d'attente, les états, la progression et le worker de drainage sont **entièrement réutilisés** — seule la **découverte + le catalogue source** sont nouveaux.

Cas d'usage typique : un ami possède un serveur Plex avec des films que l'opérateur n'a pas via son propre fournisseur Xtream ; le token plex.tv de l'opérateur donne accès (lecture catalogue) à ce serveur partagé ; cette feature permet de « rapatrier » ces fichiers en local sans jamais lire/transcoder le flux (juste un GET direct du fichier source, comme pour Xtream).

---

## 2. Personas (consommateurs d'API)

### 2.1 Opérateur admin (primaire)
- **Qui / contexte d'appel** : humain, navigateur, derrière Basic Auth (`verify_admin_basic_auth`) sur `/admin`. Utilise l'onglet « Télécharger Plex », au même titre que « Télécharger » (Xtream) / « Catalogue » / « Importer NFO » / « Clés API ».
- **Objectif** : élargir sa bibliothèque locale avec des titres disponibles uniquement sur un ou plusieurs PMS auxquels il a accès (possédés ou partagés), sans dépendre en continu de la disponibilité de ces serveurs tiers.
- **Frustrations** : la disponibilité d'un serveur partagé n'est pas garantie dans le temps (le partage peut être révoqué, le serveur éteint) ; deux copies du même film sur deux serveurs différents ne doivent pas apparaître comme deux titres distincts ; le token plex.tv est un secret qui ne doit jamais transiter côté client.

### 2.2 Worker de téléchargement (réutilisé, pas de nouveau consommateur système)
- Le worker de drainage de la feature « Télécharger » Xtream (`app/workers/download_worker.py`) traite **indifféremment** les jobs Xtream et Plex — le discriminant est le préfixe de `download_job.server_id` (`xtream_` vs `plex_`) ; l'URL de download est re-dérivée par source au moment du run (`stream_service.build_stream_url` pour Xtream, `plex_download_service.resolve_job_url` pour Plex).

### 2.3 Sync worker catalogue Plex (nouveau, interne)
- **Qui / contexte d'appel** : déclenché manuellement par l'opérateur (bouton « Sync » de l'onglet) ou, optionnellement, par un cron interval configurable (`PLEX_SYNC_INTERVAL_HOURS`, défaut `0` = désactivé). **Master-only** (même élection `fcntl.flock` que le reste du pipeline).
- **Objectif** : découvrir les serveurs (plex.tv resources), probe leur joignabilité, synchroniser leur catalogue (films/séries/épisodes) dans des tables **dédiées**, jamais dans `media`.

> Note : l'app Android PlexHubTV **n'est pas** consommatrice de cette feature (admin-only, même non-goal que la feature « Télécharger » Xtream).

---

## 3. Parcours (flux d'API bout-en-bout)

**Parcours A — Découverte + sync du catalogue.** L'opérateur configure `PLEX_ACCOUNT_TOKEN` (son token plex.tv). Au boot master, une reap best-effort remet à `idle` tout `plex_sync_status` resté `running` (process mort). L'opérateur clique « Sync » (`POST /admin/plex-downloads/sync`) → `plex_sync_service.run_full_sync` : (1) `plex_api_service` liste les ressources plex.tv (serveurs possédés + partagés), (2) probe chaque connexion candidate (`PLEX_PROBE_TIMEOUT`), (3) pour chaque serveur joignable, synchronise son catalogue (films/séries/épisodes) dans `plex_media_item` (mark-and-sweep par `synced_at`), (4) calcule/rafraîchit `unification_id` pour la dédup. État de succès : `plex_server.is_reachable`/`last_synced_at` à jour, `plex_media_item` peuplée.

**Parcours B — Parcourir & filtrer le catalogue dédupliqué.** L'opérateur ouvre `GET /admin/plex-downloads` → liste unifiée films/séries (`GET /admin/plex-downloads/list?type=&search=&page=`), 1 carte par `unification_id` avec `sourceCount` (nombre de serveurs qui possèdent ce titre). Deux exemplaires du même film sur deux PMS différents = **1 seule carte**, `sourceCount=2`.

**Parcours C — Voir les sources & sélectionner.** L'opérateur clique un titre → `GET /admin/plex-downloads/{type}/{unificationId}/versions` → pour un **film** : une source par serveur (résolution, taille, codec) ; pour une **série** : une source par serveur +, si choisi, détail par saison (`.../episodes?season=`) puis par épisode — granularité **série entière (`scope=series_all`) / saisons choisies (`scope=seasons`) / épisodes choisis (`scope=episodes`)**, chacune avec **1 source par unité téléchargée**. Note d'alignement : la feature Xtream sœur a, depuis, elle aussi gagné la sélection par saison (`scope=series_seasons`, `docs/10-prd-media-download.md`) ; la granularité **par épisode** (`scope=episodes`) reste, à ce jour, spécifique à Plex.

**Parcours D — Lancer le téléchargement.** `POST /admin/plex-downloads` (form, scope `movie`/`series_all`/`seasons`/`episodes`) → `plex_download_service.enqueue_plex_selection` calcule la destination sous **le même `DOWNLOAD_DIR`** que Xtream (arborescence par titre), crée 1 job (film) ou N jobs (série), `server_id` préfixé `plex_<clientIdentifier>` pour discriminer la source dans la table `download_job` **partagée**. Le worker existant les prend en charge sans modification — à une exception près : le sidecar `.nfo` best-effort câblé pour les jobs Xtream (`download_worker._write_sidecar_nfo`, lit la table `Media`) ne trouve aucune ligne pour un `server_id` préfixé `plex_` et est silencieusement sauté (aucun NFO écrit pour un téléchargement Plex — cf. §6/PC-202).

**Parcours E — Suivre / échec / reprise / annulation.** **Identiques à la feature Xtream** : mêmes fragments (`_downloads_queue.html`), mêmes états, même reprise `Range`, même annulation — la file est **une seule et même table** `download_job`, peu importe la source.

**Parcours F — Lecture JSON (QA/automation).** `GET /api/admin/plex-downloads/servers` (liste des serveurs connus, sans secret), `GET /api/admin/plex-downloads/catalog?type=&search=&limit=&offset=` (catalogue dédupliqué paginé), `GET /api/admin/plex-downloads/catalog/{type}/{unificationId}` (détail + sources hydratées). Garde `verify_master_key` (secret maître uniquement, jamais une clé par-utilisateur). Miroir strict de `GET /api/admin/downloads` côté Xtream.

---

## 4. Table des capacités

| ID | Nom | Description | Prio | Statut |
|----|-----|-------------|------|--------|
| PC-001 | Découverte serveurs plex.tv | `plex_api_service` liste les resources (possédés+partagés), probe la meilleure connexion | P0 | **Livré** (C1/C2) |
| PC-002 | Tables catalogue dédiées | `plex_server`/`plex_media_item`/`plex_sync_status` (migration 019), isolées de `media` | P0 | **Livré** (C1) |
| PC-003 | Sync catalogue mark-and-sweep | `plex_sync_service.run_full_sync` : discover→probe→catalogue-sync→dédup | P0 | **Livré** (C2/C3) |
| PC-004 | Dédup lecture (`unification_id`) | `plex_catalog_service.list_unified`/`get_group` : `GROUP BY unification_id`, priorité imdb>tmdb>plexsrc | P0 | **Livré** (C4) |
| PC-005 | Enqueue réutilisant la file existante | `plex_download_service.enqueue_plex_selection` écrit dans `download_job` (discriminant `server_id` préfixe `plex_`) | P0 | **Livré** (C5) |
| PC-006 | Worker branché sans duplication | `download_worker` route sur `is_plex_server_id` pour re-dériver l'URL via `plex_download_service.resolve_job_url` | P0 | **Livré** (C5) |
| PC-007 | UI admin `/admin/plex-downloads` | Onglet HTMX miroir, 5 templates, réutilise le panneau file existant | P0 | **Livré** (C6) |
| PC-101 | Miroir JSON lecture | `GET /api/admin/plex-downloads/{servers,catalog,catalog/{type}/{id}}`, `verify_master_key` | P1 | **Livré** (C7, ce PRD) |
| PC-102 | Cron sync optionnel | `PLEX_SYNC_INTERVAL_HOURS>0` + `PLEX_ACCOUNT_TOKEN` non vide → job APScheduler master-only | P1 | **Livré** (C7, ce PRD) |
| PC-201 | Multi-versions par serveur | Aujourd'hui : meilleur `Media` élément retenu par item ; pas de sélection multi-piste par serveur | P2 | Non livré (board) |
| PC-202 | NFO sidecar depuis `plex_media_item` | Écrire un `.nfo` à côté du fichier téléchargé, réutilisant les métadonnées Plex déjà en base | P2 | Non livré (board) |
| PC-203 | Vignettes proxifiées | `thumb_url` est un chemin relatif PMS qui nécessite le token pour être résolu — pas de proxy image aujourd'hui | P2 | Non livré (board) |

---

## 5. User stories (capacités P0/P1)

### PC-001/002/003 — Découverte + catalogue
> **US-PC-001.1** — En tant qu'**opérateur admin**, je veux que le backend découvre tous les serveurs Plex accessibles via mon token afin de ne pas avoir à les déclarer un par un.
> - **Given** `PLEX_ACCOUNT_TOKEN` configuré **When** `POST /admin/plex-downloads/sync` **Then** `plex_server` est peuplée avec les serveurs possédés ET partagés, `is_reachable` reflète le probe réel.
> - **Given** `PLEX_ACCOUNT_TOKEN` vide **When** tout appel de sync **Then** no-op explicite (bandeau « configurer PLEX_ACCOUNT_TOKEN »), aucune tâche lancée, aucune erreur 500.

### PC-004 — Dédup
> **US-PC-004.1** — En tant qu'**opérateur admin**, je veux voir un film disponible sur 2 serveurs comme **une seule** carte afin de choisir la meilleure source sans doublon visuel.
> - **Given** 2 lignes `plex_media_item` de même `unification_id` sur 2 serveurs **When** `GET /admin/plex-downloads/list` **Then** 1 carte, `sourceCount=2`.

### PC-005/006 — Enqueue & worker réutilisés
> **US-PC-005.1** — En tant qu'**opérateur admin**, je veux que le téléchargement Plex passe par la **même** file que Xtream afin de suivre tous mes téléchargements au même endroit.
> - **Given** un enqueue Plex réussi **When** `GET /admin/downloads/queue` (fragment partagé) **Then** le job Plex apparaît aux côtés des jobs Xtream, mêmes états/progression.

### PC-101 — Miroir JSON (ce ticket, C7)
> **US-PC-101.1** — En tant qu'**automation/QA**, je veux lire le catalogue Plex en JSON afin de vérifier la dédup et les sources sans passer par l'UI HTML.
> - **Given** aucune clé **When** `GET /api/admin/plex-downloads/servers|catalog` **Then** **401**.
> - **Given** la clé maître **When** même appel **Then** **200**, jamais de `accessToken`/`baseUri` dans la réponse.
> - **Given** un `unificationId` inconnu **When** `GET /api/admin/plex-downloads/catalog/{type}/{unificationId}` **Then** **404**.

### PC-102 — Cron optionnel (ce ticket, C7)
> **US-PC-102.1** — En tant qu'**opérateur admin**, je veux pouvoir automatiser la re-sync du catalogue Plex sans cliquer « Sync » manuellement afin de garder la liste à jour.
> - **Given** `PLEX_ACCOUNT_TOKEN` et `PLEX_SYNC_INTERVAL_HOURS>0` **When** le master boote **Then** un job APScheduler `plex_catalogue_sync` tourne toutes les `PLEX_SYNC_INTERVAL_HOURS` heures.
> - **Given** `PLEX_SYNC_INTERVAL_HOURS=0` (défaut) **OU** `PLEX_ACCOUNT_TOKEN` vide **Then** **aucun** job n'est enregistré (no-op complet, pas seulement un skip à l'exécution) — la sync reste manuelle via le bouton admin.
> - **Given** un run cron alors qu'une sync manuelle est déjà en cours **When** le tick se déclenche **Then** `run_full_sync` retourne `status="already_running"` sans double-exécution (le claim `plex_sync_status` protège, en plus de `max_instances=1`).

---

## 6. Hors périmètre (non-goals)

- **Aucune insertion dans la table `media`** : le catalogue Plex reste isolé dans `plex_server`/`plex_media_item`/`plex_sync_status`, jamais surfacé par `/api/media`, la génération `.strm`/NFO, ni l'app Android.
- **Pas de lecture/streaming/transcodage** depuis un PMS tiers : uniquement un `GET` direct du fichier source, octets copiés tels quels (même contrat que la feature Xtream).
- **MVP catalogue = meilleur `Media` élément retenu par item** (pas de sélection multi-piste/multi-résolution par serveur) — évolution P2 (PC-201, board).
- **Pas de NFO/poster à côté du fichier téléchargé** au MVP (P2, PC-202) — la feature Xtream sœur a depuis gagné un sidecar `.nfo` best-effort (`download_worker._write_sidecar_nfo`, lit la table `Media`), mais il ne couvre **pas** les jobs Plex (`PlexMediaItem` n'est pas `Media`) ; réutiliser ce mécanisme pour Plex reste à faire (PC-202).
- **Pas de proxy d'image** pour les vignettes (`thumb_url` reste un chemin relatif brut nécessitant le token — non exposé côté client) — P2 (PC-203).
- **Pas d'exposition côté app Android** (mêmes raisons que la feature Xtream : admin-only, opération d'exploitation).
- **Pas de gestion de révocation de partage en temps réel** : un serveur qui devient injoignable est simplement marqué `is_reachable=False` au prochain sync ; les jobs déjà en file échoueront proprement (`failed`, retries épuisés) sans casser le reste de la file.

---

## 7. Décisions figées

1. **Token en env, jamais côté client** : `PLEX_ACCOUNT_TOKEN` est lu depuis l'environnement serveur (`config.py`) — jamais saisi ni affiché dans l'UI admin, jamais renvoyé en JSON.
2. **Catalogue en tables dédiées, isolées de `media`** : `plex_server`/`plex_media_item`/`plex_sync_status` (migration **019**) — aucune migration de `media`, aucun risque de collision avec le pipeline Xtream existant.
3. **Isolation totale de la génération `.strm`/NFO** : la génération de bibliothèque Plex/Jellyfin (`DatabaseSource`, `PlexLibraryGenerator`) n'a **aucune** dépendance sur `plex_media_item` — les deux fonctionnalités partagent le mot « Plex » dans leur nom mais sont des sous-systèmes disjoints (l'une génère une bibliothèque Plex à partir du catalogue Xtream ; l'autre télécharge depuis de vrais serveurs Plex).
4. **Worker/queue réutilisés, pas dupliqués** : `download_job`/`download_worker` sont **partagés** entre Xtream et Plex ; le discriminant est le préfixe de `server_id` (`xtream_` vs `plex_`, `app/utils/server_id.py`) — aucune nouvelle table de file, aucun nouveau worker.
5. **Préfixe `server_id` = `plex_<clientIdentifier>`** : stable même si le serveur est renommé côté Plex (le `clientIdentifier` plex.tv ne change jamais), cohérent avec `build_server_id`/`parse_server_id` côté Xtream.
6. **Dédup priorité imdb > tmdb > `plexsrc://`** : jamais titre+année seul (contrairement à `aggregation_service` côté Xtream qui doit gérer la convergence d'identités splittées) — `plex_media_item.unification_id` est peuplé au sync, la lecture (`plex_catalog_service`) fait un simple `GROUP BY` SQL, pas de convergence en mémoire.
7. **Cron optionnel, désactivé par défaut** : `PLEX_SYNC_INTERVAL_HOURS=0` = sync manuelle uniquement (bouton admin) ; l'opérateur active le cron explicitement si souhaité.

---

## 8. Impact contrat d'API (additif, rétrocompatible)

**Nouveau — HTMX admin (Basic Auth `verify_admin_basic_auth`, préfixe `/admin/plex-downloads`)** — livré C6, inchangé par ce ticket :
| Verbe + chemin | Réponse | Rôle |
|---|---|---|
| `GET /admin/plex-downloads` | 200 HTML | Page onglet + panneau file (partagé) |
| `GET /admin/plex-downloads/list?type=&search=&page=` | 200 fragment | Liste unifiée films/séries Plex |
| `GET /admin/plex-downloads/{type}/{unificationId}/versions` | 200 fragment / 404 | Sources d'un titre |
| `GET /admin/plex-downloads/{type}/{unificationId}/episodes?season=` | 200 fragment | Épisodes d'une saison |
| `POST /admin/plex-downloads` (form) | 200 fragment | Enqueue (réutilise `download_job`) |
| `POST /admin/plex-downloads/sync` | 200 fragment | Déclenche `run_full_sync` |
| `GET /admin/plex-downloads/sync/status` | 200 fragment | État de sync (polling) |

**Nouveau — JSON admin (garde `verify_master_key`, préfixe `/api/admin/plex-downloads`) — ce ticket (C7)** :
| Verbe + chemin | Réponse | Rôle |
|---|---|---|
| `GET /api/admin/plex-downloads/servers` | 200 `PlexServerListResponse` / 401 | Serveurs connus, secret-free |
| `GET /api/admin/plex-downloads/catalog?type=&search=&limit=&offset=` | 200 `PlexCatalogResponse` / 401 | Catalogue dédupliqué paginé |
| `GET /api/admin/plex-downloads/catalog/{type}/{unificationId}` | 200 `PlexUnifiedItemResponse` / 404 / 401 | Détail + sources hydratées |

Schémas Pydantic v2 (camelCase, `to_camel`, `populate_by_name`) déjà posés côté C4 : `PlexServerResponse{serverId,clientIdentifier,name,ownerTitle,owned,isReachable,lastSyncedAt,lastSyncError}`, `PlexSourceResponse{serverId,ratingKey,serverName,resolution,sizeBytes,videoCodec,container}`, `PlexUnifiedItemResponse{unificationId,type,title,year,sourceCount,sources}`, `PlexServerListResponse{items,total}`, `PlexCatalogResponse{items,total}`.

**Nouvelle config (déjà posée C1, référencée ici)** : `PLEX_ACCOUNT_TOKEN` (défaut `""` = feature désactivée), `PLEX_CLIENT_IDENTIFIER` (auto-généré si absent, persisté sur disque), `PLEX_PROBE_TIMEOUT` (défaut `5`s), `PLEX_SYNC_INTERVAL_HOURS` (défaut `0` = cron désactivé, ce ticket câble le job APScheduler correspondant).

**Non modifié** : `/api/media/*`, `/api/plex/*` (génération `.strm`), `/api/admin/downloads*` (Xtream), `/api/ai/*`, auth fail-closed `X-API-Key`, la table `download_job` elle-même (aucune nouvelle colonne).

---

## 9. Sécurité

- **`PlexServer.access_token` et `base_uri`** : le token est un secret **par serveur** (plex.tv en émet un distinct par ressource), chiffré **Fernet au repos** exactement comme `XtreamAccount.password` (`EncryptedString`) — jamais exposé en API/HTML/log. `base_uri` (l'URI de connexion gagnante du probe) ne porte volontairement pas le token et reste affichable pour diagnostic.
- **URL de téléchargement re-dérivée au worker** : comme pour Xtream, aucune URL complète (qui embarquerait le token en query) n'est jamais persistée en DB — `plex_download_service.resolve_job_url` la reconstruit au moment du transfert à partir de `plex_server.access_token`/`base_uri` + `download_job.rating_key`.
- **Confinement d'écriture réutilisé (F-007)** : la destination est calculée serveur-side sous le **même** `DOWNLOAD_DIR`, via le **même** `download_service.resolve_confined` (realpath) — aucun chemin client accepté, aucune nouvelle surface de risque.
- **`GET /api/admin/plex-downloads/*` = secret maître uniquement** (`verify_master_key`), jamais une clé par-utilisateur — même garde que `GET /api/admin/downloads`.
- **`thumb_url`** stocké en base est un chemin **relatif** au PMS (sans token intégré) — jamais résolu en URL absolue côté serveur pour cette feature (pas de proxy image au MVP, cf. §6/PC-203).

---

## 10. Migration

**Migration 019** (idempotente, `CREATE TABLE/INDEX IF NOT EXISTS`, en fin de `run_migrations()`) : tables `plex_server`, `plex_media_item`, `plex_sync_status` — additives, aucune modification de schéma existant. Migration courante du backend après cette feature = **019**.

---

## Handoff

```
NEXT:
- code-reviewer: revue du miroir JSON (app/api/plex_downloads.py) + cron optionnel (app/main.py) — ticket C7
- security-reviewer: confirmer 0 fuite access_token/base_uri sur les 3 nouvelles routes JSON (tests dédiés déjà en place)
- Board : docs/31-board.md — follow-ups P2/P3 ajoutés (re-probe auto, multi-versions/serveur, NFO sidecar,
  vignettes proxifiées, CSRF admin transverse, métriques Prometheus sync Plex)
```
