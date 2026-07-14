# PRD — Téléchargement physique de médias (« Télécharger »)

> Statut : proposé (CPO) · Cible : backend PlexHub (FastAPI / Python 3.13) · Migration en tête = 017 → **018** pour cette feature · **Additif & rétrocompatible** — n'altère ni `/api/media`, ni `/api/plex`, ni la génération `.strm`, ni les onglets admin existants.
> Handoff attendu : `tech-lead` → `docs/22-impl-spec-backend.md`. Ce PRD reste au niveau **produit/contrat** ; il ne fige pas l'implémentation.

---

## 1. Résumé produit

Un nouvel onglet **« Télécharger »** dans l'UI admin (`/admin`, HTMX) permet à l'opérateur de parcourir tout le catalogue films + séries déjà synchronisé, de **sélectionner une ou plusieurs versions** d'un titre (ou une **série complète**) et de déclencher le **téléchargement physique** des fichiers vidéo (.mkv/.mp4/…) vers un dossier serveur dédié `DOWNLOAD_DIR`, distinct de la bibliothèque `.strm`. Le backend gère une **file de téléchargements** persistante avec états, progression et reprise. C'est la première capacité de PlexHub qui écrit réellement les octets des médias sur disque — jusqu'ici le système était **référence-only** (`.strm` pointant vers des URLs Xtream).

---

## 2. Personas (consommateurs d'API)

### 2.1 Opérateur admin (primaire)
- **Qui / contexte d'appel** : humain, navigateur, derrière Basic Auth (`verify_admin_basic_auth`) sur `/admin`. Utilise l'onglet « Télécharger » (HTMX + Jinja2 + Tailwind CDN), au même titre que « Catalogue » / « Importer NFO » / « Clés API ».
- **Objectif** : constituer une bibliothèque locale de films/séries choisis, hébergée sur son serveur, sans dépendre en continu du fournisseur Xtream.
- **Frustrations** : le catalogue actuel n'est que des références `.strm` ; aucun moyen de « garder » un média localement ; les versions multi-comptes sont opaques ; un téléchargement long ne doit pas dépendre d'un onglet resté ouvert.

### 2.2 Worker de téléchargement interne (consommateur système)
- **Qui / contexte d'appel** : coroutine/worker background qui **ne tourne que sur le processus master** (élection `fcntl.flock`, cohérent avec le pipeline sync/enrich/plex), consomme la file `download_job`, effectue le `GET` streaming httpx vers disque, met à jour l'état/progression via `run_with_retry`.
- **Objectif** : drainer la file de façon bornée (concurrence limitée), résiliente (reprise/retry) et sûre (écriture confinée).
- **Frustrations** : SQLite « database is locked » sur écritures fréquentes de progression ; téléchargements interrompus par un redémarrage ; saturation disque/bande passante.

> Note : l'app Android PlexHubTV **n'est pas** consommatrice de cette feature au MVP (téléchargement = opération d'exploitation, admin-only). Une éventuelle exposition côté app est explicitement hors périmètre (§6).

---

## 3. Parcours (flux d'API bout-en-bout)

**Parcours A — Parcourir & filtrer.** L'opérateur ouvre `GET /admin/downloads` (Basic Auth) → **200** page HTML avec l'onglet « Télécharger » actif. La liste unifiée films + séries est rendue via `GET /admin/downloads/list?type=&search=&page=` (fragment HTMX), qui réutilise `media_service.get_unified_list` (1 carte par titre dédupliqué, `versionCount`). État de succès : l'opérateur voit une liste paginée, cherchable (titre) et filtrable (films / séries), identique en contenu à ce que renvoie `/api/media/{movies,shows}/unified`.

**Parcours B — Voir les versions & sélectionner.** L'opérateur clique un titre → `GET /admin/downloads/{type}/{unificationId}/versions` → **200** fragment listant chaque version (`label`, `serverId`, `ratingKey`, `isBroken`). Pour un **film** : un bouton « Télécharger » par version. Pour une **série** : chaque version = une source de série (compte) ; le choix « **Série complète (toutes saisons)** » télécharge **tous les épisodes de toutes les saisons** de cette source. `unificationId` inconnu → **404**. État de succès : l'opérateur a devant lui les versions exactes (paire `serverId:ratingKey`) qu'il peut piocher.

**Parcours C — Lancer le téléchargement.** L'opérateur soumet `POST /admin/downloads` (form : `type`, `unificationId`, `serverId`, `ratingKey`, `scope=movie|series_all`). Le backend résout l'URL directe via `stream_service.build_stream_url(account, ratingKey)`, calcule le **chemin de destination serveur-side** sous `DOWNLOAD_DIR` (`Movies/<Titre (Année)>/…` ou `Series/<Titre>/Season NN/…`), crée 1 job (film) ou N jobs (série = 1 par épisode existant), tous à l'état `queued`, puis renvoie **200** le fragment « file d'attente » avec les nouveaux items. Si `DOWNLOAD_DIR` non défini → **200** fragment d'erreur explicite (garde de config, analogue au garde `PLEX_LIBRARY_DIR` de l'onglet Import NFO) ; sélection invalide → **422** fragment. État de succès : les jobs sont **persistés** (table `download_job`, migration 018) et le worker master les prend en charge.

**Parcours D — Suivre l'avancement.** Le panneau file s'auto-rafraîchit via `GET /admin/downloads/queue` (fragment HTMX, polling `hx-trigger="every 2s"`) → **200** : chaque job affiche son **état** (`queued`/`running`/`completed`/`failed`/`canceled`) et sa **progression** (`bytesDownloaded`/`bytesTotal`, `percent`, `speedBps`). Pour l'automatisation/QA, le même état est lisible en JSON via `GET /api/admin/downloads` (garde `verify_master_key`). État de succès : la barre de progression avance ; un job terminé passe `completed` et son fichier final (`.part` renommé atomiquement) apparaît sous `DOWNLOAD_DIR`.

**Parcours E — Échec & reprise.** Un transfert interrompu (réseau, 5xx amont) est auto-retenté jusqu'à `DOWNLOAD_MAX_RETRIES` ; épuisé, il passe `failed` avec un message d'erreur borné. L'opérateur relance via `POST /admin/downloads/{jobId}/retry` → **200** fragment, le job repasse `queued`. Si un fichier partiel `.part` existe et que le serveur amont supporte `Range` (les flux Xtream le supportent — cf. validation health-check HEAD→Range GET), la reprise **repart de l'octet déjà téléchargé**, sinon reprise complète. État de succès : le job atteint `completed` sans re-télécharger inutilement.

**Parcours F — Redémarrage du backend.** Au boot du master, tout job resté `running` est remis à `queued` (le transfert de la requête précédente est mort) ; le worker les reprend, `Range` si `.part` présent. État de succès : aucun job « fantôme » `running`, aucune perte de file.

**Parcours G — Annulation.** L'opérateur clique « Annuler » → `POST /admin/downloads/{jobId}/cancel` → **200** fragment ; un job `queued` est retiré de la file, un job `running` est stoppé proprement (le `.part` est laissé pour une reprise éventuelle, jamais promu en fichier final). État de succès : le job affiche `canceled`, la bande passante est libérée.

---

## 4. Table des capacités

| ID | Nom | Description | Prio | Critères d'acceptation (résumé) |
|----|-----|-------------|------|---------------------------------|
| F-001 | Onglet « Télécharger » + liste unifiée | Nav admin étendue ; `GET /admin/downloads` + fragment `GET /admin/downloads/list` (films+séries, search/filtre/pagination) réutilisant `media_service.get_unified_list` | **P0** | 200 HTML ; onglet actif ; liste dédupliquée cherchable/filtrable ; onglets existants intacts |
| F-002 | Versions d'un titre + sélection | `GET /admin/downloads/{type}/{unificationId}/versions` liste `label`/`serverId`/`ratingKey`/`isBroken` ; option « série complète » pour les shows | **P0** | 200 fragment avec ≥1 version ; 404 si `unificationId` inconnu ; shows exposent l'option toutes-saisons |
| F-003 | Mise en file & téléchargement physique | `POST /admin/downloads` → crée job(s) `queued` ; worker master GET streaming httpx → `DOWNLOAD_DIR` en structure par titre ; écriture `.part`→rename atomique | **P0** | Film = 1 job ; `scope=series_all` = 1 job/épisode ; fichier final sous `DOWNLOAD_DIR` ; catalogue `.strm` inchangé |
| F-004 | Suivi d'états & progression | États `queued/running/completed/failed/canceled` + `bytesDownloaded/bytesTotal/percent` ; `GET /admin/downloads/queue` (polling HTMX) | **P0** | Progression monotone croissante ; `completed` ⇒ fichier présent ; `bytesTotal=null` toléré si pas de Content-Length |
| F-005 | Reprise & retries | Auto-retry transitoire (`DOWNLOAD_MAX_RETRIES`) ; reprise `Range` depuis `.part` ; `POST /admin/downloads/{jobId}/retry` ; jobs `running` → `queued` au boot | **P0** | Retry manuel re-queue ; reprise ne re-télécharge pas l'octet déjà pris quand `Range` supporté ; pas de job `running` fantôme au boot |
| F-006 | Annulation | `POST /admin/downloads/{jobId}/cancel` stoppe `queued`/`running` proprement | **P0** | 200 fragment ; état `canceled` ; `.part` conservé, jamais promu ; bande passante libérée |
| F-007 | Confinement d'écriture + garde config | Chemin destination **calculé serveur-side** (titre/saison/épisode sanitizés) sous `DOWNLOAD_DIR` ; jamais de chemin fourni par le client ; garde si `DOWNLOAD_DIR` vide | **P0** | 0 écriture hors `DOWNLOAD_DIR` (testé) ; `DOWNLOAD_DIR` vide ⇒ fragment d'erreur, aucun job créé |
| F-008 | Persistance & migration 018 | Table `download_job` (+ regroupement batch série) additive/idempotente en fin de chaîne ; écritures via `run_with_retry` | **P0** | Migration idempotente 018 ; boot OK ; jobs survivent au redémarrage ; pas de dette lock request-path |
| F-101 | API JSON statut (lecture) | `GET /api/admin/downloads` + `GET /api/admin/downloads/{jobId}` (Pydantic v2 camelCase), garde `verify_master_key` | P1 | 200 `DownloadJobListResponse`/`DownloadJobResponse` ; 404 job inconnu ; 401 sans clé maître |
| F-102 | Préflight espace disque | Refuse/averti si l'espace libre < `DOWNLOAD_MIN_FREE_DISK_MB` (somme des `Content-Length` connus si dispo) | P1 | Espace insuffisant ⇒ jobs non lancés + message ; jamais de disque plein silencieux |
| F-103 | Métriques Prometheus | `plexhub_downloads_total{result}`, `plexhub_download_bytes_total`, gauge `plexhub_downloads_active` | P1 | Compteurs exposés sur `/metrics` ; incrémentés par transition d'état |
| F-104 | Historique / nettoyage | `POST /admin/downloads/clear-finished` retire `completed/failed/canceled` de la vue | P1 | 200 fragment ; jobs actifs non affectés |
| F-201 | API JSON mutation | `POST /api/admin/downloads` (enqueue) / cancel / retry pour automatisation | P2 | Miroir JSON des actions HTMX, `verify_master_key` |
| F-202 | Préfixe `[XXX]` films adultes | Applique `apply_adult_prefix` au **nom de dossier** des films `is_adult` (cohérence bibliothèque `.strm`) | P2 | Dossier d'un film `is_adult` porte `[XXX] ` ; non-adulte inchangé |
| F-203 | NFO/poster à côté de la vidéo | Écrire `movie.nfo`/`tvshow.nfo` + poster via `nfo_builder` dans `DOWNLOAD_DIR` | P2 | Optionnel, réutilise le générateur existant |

---

## 5. User stories (capacités P0)

### F-001 — Onglet & liste unifiée
> **US-001.1** — En tant qu'**opérateur admin**, je veux ouvrir un onglet « Télécharger » afin de voir tout le catalogue films + séries à télécharger.
> - **Given** Basic Auth admin valide **When** `GET /admin/downloads` **Then** **200** HTML, l'onglet « Télécharger » est dans la nav et marqué actif, sans modifier les onglets « Catalogue » / « Importer NFO » / « Clés API ».
> - **Given** aucune clé/Basic Auth **When** `GET /admin/downloads` **Then** **401** (WWW-Authenticate Basic), aucune donnée rendue.

> **US-001.2** — En tant qu'**opérateur admin**, je veux chercher et filtrer la liste afin de retrouver vite un titre.
> - **Given** un catalogue synchronisé **When** `GET /admin/downloads/list?type=movie&search=terminator&page=1` **Then** **200** fragment HTML listant les cartes unifiées (1 par titre, `versionCount` visible), filtrées `type=movie` et matchant `search`, paginées.
> - **Given** `type=show` **When** même appel **Then** la liste ne contient que des séries. **Given** aucun résultat **Then** **200** fragment « aucun titre » (jamais 500).

### F-002 — Versions & sélection
> **US-002.1** — En tant qu'**opérateur admin**, je veux voir les versions d'un titre afin de choisir la source/qualité à télécharger.
> - **Given** un `unificationId` de film valide **When** `GET /admin/downloads/movie/{unificationId}/versions` **Then** **200** fragment listant chaque version avec `label`, `serverId`, `ratingKey`, `isBroken`, et un bouton « Télécharger » par version.
> - **Given** un `unificationId` inconnu **When** l'appel **Then** **404**.

> **US-002.2** — En tant qu'**opérateur admin**, je veux une option « série complète » afin de télécharger toutes les saisons d'un coup.
> - **Given** un `unificationId` de série avec des épisodes **When** `GET /admin/downloads/show/{unificationId}/versions` **Then** **200** fragment exposant, par version de série, un choix `scope=series_all` (« toutes saisons »).
> - **Given** une série sans épisode dans la version choisie **Then** l'option reste visible mais l'enqueue produit 0 job + un message « aucun épisode disponible » (jamais 500).

### F-003 — File & téléchargement physique
> **US-003.1** — En tant qu'**opérateur admin**, je veux lancer le téléchargement d'un film afin d'obtenir le fichier vidéo sur mon serveur.
> - **Given** `DOWNLOAD_DIR` défini et un compte actif **When** `POST /admin/downloads` avec `type=movie`, `serverId`, `ratingKey=vod_{id}.mkv`, `scope=movie` **Then** **200** fragment file d'attente contenant **1** job `queued`, et le job est persisté dans `download_job`.
> - **Given** le job traité par le worker **When** le transfert se termine **Then** un fichier vidéo existe sous `DOWNLOAD_DIR/Movies/<Titre (Année)>/…` et le catalogue `.strm` (`PLEX_LIBRARY_DIR`) est **inchangé**.

> **US-003.2** — En tant qu'**opérateur admin**, je veux télécharger une série complète afin d'obtenir tous ses épisodes.
> - **Given** un `unificationId` de série + version choisie **When** `POST /admin/downloads` avec `scope=series_all` **Then** **200** fragment contenant **N** jobs `queued` (1 par épisode existant, `ratingKey=ep_{id}.{ext}`), regroupés sous un même batch.
> - **Then** chaque épisode terminé apparaît sous `DOWNLOAD_DIR/Series/<Titre>/Season NN/…`.

> **US-003.3** — En tant qu'**opérateur admin**, je veux un garde clair si la destination n'est pas configurée afin de ne rien casser.
> - **Given** `DOWNLOAD_DIR` vide **When** `POST /admin/downloads` **Then** **200** fragment d'erreur (« DOWNLOAD_DIR n'est pas défini »), **0** job créé.

### F-004 — États & progression
> **US-004.1** — En tant qu'**opérateur admin**, je veux suivre l'avancement afin de savoir où en est chaque téléchargement.
> - **Given** ≥1 job en file **When** `GET /admin/downloads/queue` **Then** **200** fragment ; chaque item affiche un état ∈ {`queued`,`running`,`completed`,`failed`,`canceled`} et, pour `running`/`completed`, `bytesDownloaded`, `bytesTotal` (ou `null`), `percent`.
> - **Given** un serveur amont sans `Content-Length` **Then** `bytesTotal=null` et `percent=null` sont tolérés (état/octets cumulés toujours affichés, jamais 500).

> **US-004.2** — En tant qu'**opérateur admin**, je veux un état persistant afin de ne pas perdre le suivi si je recharge la page.
> - **Given** un job `running` **When** je recharge `GET /admin/downloads` **Then** l'état et la progression reflètent la valeur persistée en DB (pas un compteur en mémoire de requête).

### F-005 — Reprise & retries
> **US-005.1** — En tant qu'**opérateur admin**, je veux relancer un téléchargement échoué afin de ne pas tout recommencer.
> - **Given** un job `failed` avec `.part` partiel et amont supportant `Range` **When** `POST /admin/downloads/{jobId}/retry` **Then** **200** fragment, job `queued`, et la reprise repart de `bytesDownloaded` (requête `Range: bytes=<n>-`), sans re-télécharger l'octet déjà pris.
> - **Given** amont sans `Range` **Then** reprise complète (le `.part` est réécrit), le job atteint tout de même `completed`.

> **US-005.2** — En tant que **worker de téléchargement**, je veux auto-retenter les erreurs transitoires afin d'atteindre `completed` sans intervention.
> - **Given** une coupure réseau/5xx transitoire **When** le transfert échoue **Then** le worker retente jusqu'à `DOWNLOAD_MAX_RETRIES` avant de passer `failed`.
> - **Given** un redémarrage backend avec des jobs `running` **When** le master reboot **Then** ces jobs repassent `queued` et sont repris (aucun `running` fantôme).

### F-006 — Annulation
> **US-006.1** — En tant qu'**opérateur admin**, je veux annuler un téléchargement afin de libérer la bande passante/disque.
> - **Given** un job `queued` **When** `POST /admin/downloads/{jobId}/cancel` **Then** **200** fragment, état `canceled`, le job ne démarre jamais.
> - **Given** un job `running` **When** l'annulation **Then** le transfert s'arrête proprement, l'état passe `canceled`, le `.part` est conservé (jamais promu en fichier final).

### F-007 — Confinement & sécurité chemin
> **US-007.1** — En tant que **worker de téléchargement**, je veux que la destination soit calculée serveur-side afin d'empêcher toute écriture hors `DOWNLOAD_DIR`.
> - **Given** un titre au nom hostile (`../`, séparateurs, unicode) **When** le chemin est résolu **Then** il est sanitizé et **confiné** sous `DOWNLOAD_DIR` (résolution `realpath` vérifiée), et aucun champ de chemin n'est accepté du client (contraste avec la dette `outputDir` verbatim de `POST /api/plex/generate`).
> - **Then** un test prouve que 0 fichier n'est écrit hors de `DOWNLOAD_DIR`.

### F-008 — Persistance & migration
> **US-008.1** — En tant que **db-migration-specialist**, je veux une table `download_job` additive afin de persister la file sans casser le schéma.
> - **Given** une DB en migration 017 **When** `run_migrations()` s'exécute **Then** la migration **018** crée `download_job` (+ regroupement batch) en `CREATE TABLE IF NOT EXISTS`, idempotente, en fin de chaîne, et le boot `uvicorn app.main:app` reste OK avec `/api/health` **200**.
> - **Then** les écritures de progression fréquentes passent par `run_with_retry` (pas de `db.commit()` nu request-path).

---

## 6. Hors périmètre (non-goals)

- **Aucune modification du catalogue `.strm` existant** : la bibliothèque `PLEX_LIBRARY_DIR` (références Xtream) et sa génération restent intactes ; `DOWNLOAD_DIR` est une bibliothèque **distincte** de fichiers physiques.
- **Pas de transcodage / ré-encodage / remux** : les octets sont copiés tels quels depuis l'amont Xtream.
- **Pas de lecture in-app ni de streaming depuis `DOWNLOAD_DIR`** : le backend écrit les fichiers, l'app PlexHubTV n'en consomme pas au MVP.
- **Pas de téléchargement de sous-titres** (le service de traduction SRT/VTT reste séparé) ni de pistes externes.
- **Pas de torrent / seeding / P2P** : uniquement `GET` HTTP direct sur l'URL Xtream.
- **Pas de purge automatique / rétention** des fichiers téléchargés au MVP : l'espace disque est géré par l'opérateur (nettoyage manuel possible via l'OS).
- **Pas d'exposition côté app Android** (endpoints `/api/media` inchangés ; pas de nouvel endpoint app pour lancer/lister des téléchargements).
- **Pas de planification/cron de téléchargement** : déclenchement manuel par l'opérateur uniquement.
- **Pas de sélection épisode-par-épisode au MVP** : la maille série est « source entière = toutes saisons » (`scope=series_all`) ; le choix saison/épisode fin est une évolution ultérieure.
- **Écriture NFO/poster à côté de la vidéo** = P2 (pas MVP) ; **préfixe `[XXX]`** sur les dossiers adultes = P2 (défaut décidé : oui, mais non bloquant).

---

## 7. États d'un téléchargement (modèle produit)

```
queued ──▶ running ──▶ completed
   │          │
   │          ├──▶ failed      (retries épuisés)  ──(retry)──▶ queued
   └──────────┴──▶ canceled    (action opérateur)  ──(retry)──▶ queued
```

- **queued** : job créé, en attente du worker (borné par `DOWNLOAD_CONCURRENCY`, défaut 1).
- **running** : transfert en cours ; met à jour `bytesDownloaded`/`bytesTotal`/`percent`/`speedBps`.
- **completed** : `.part` renommé atomiquement vers le fichier final ; fichier présent sous `DOWNLOAD_DIR`. (Variante idempotente : fichier déjà présent ⇒ `completed` marqué « déjà présent », sans re-télécharger — voir Q1.)
- **failed** : erreur définitive (retries épuisés, 404/403 amont, disque plein) + message borné (jamais l'URL Xtream en clair, qui contient les credentials).
- **canceled** : arrêt volontaire ; `.part` conservé pour reprise éventuelle.

---

## 8. Impact contrat d'API (additif, rétrocompatible)

**Nouveau — HTMX admin (Basic Auth `verify_admin_basic_auth`, préfixe `/admin/downloads`)** :
| Verbe + chemin | Réponse | Rôle |
|---|---|---|
| `GET /admin/downloads` | 200 HTML | Page onglet + panneau file |
| `GET /admin/downloads/list?type=&search=&page=&page_size=` | 200 fragment | Liste unifiée films/séries |
| `GET /admin/downloads/{type}/{unificationId}/versions` | 200 fragment / 404 | Versions d'un titre |
| `POST /admin/downloads` (form) | 200 fragment / 422 | Enqueue film ou série complète |
| `GET /admin/downloads/queue` | 200 fragment | File + progression (polling) |
| `POST /admin/downloads/{jobId}/cancel` | 200 fragment | Annuler |
| `POST /admin/downloads/{jobId}/retry` | 200 fragment | Relancer |
| `POST /admin/downloads/clear-finished` | 200 fragment | Nettoyer l'historique (P1) |

**Nouveau — JSON admin (garde `verify_master_key`, préfixe `/api/admin/downloads`)** — P1 lecture / P2 mutation :
| Verbe + chemin | Réponse | Prio |
|---|---|---|
| `GET /api/admin/downloads` | 200 `DownloadJobListResponse` / 401 | P1 |
| `GET /api/admin/downloads/{jobId}` | 200 `DownloadJobResponse` / 404 / 401 | P1 |
| `POST /api/admin/downloads` | 202 `DownloadJobResponse[]` / 422 / 401 | P2 |
| `POST /api/admin/downloads/{jobId}/{cancel,retry}` | 200 `DownloadJobResponse` / 404 / 401 | P2 |

Schémas Pydantic v2 (camelCase, `to_camel`, `populate_by_name`) — forme cible : `DownloadJobResponse { jobId, type, title, season?, episode?, serverId, ratingKey, unificationId, state, bytesDownloaded, bytesTotal?, percent?, speedBps?, destPath, error?, retries, batchId?, createdAt, updatedAt }`.

**Nouvelle config** (`config.py`, lues via `os.getenv`) : `DOWNLOAD_DIR` (défaut `""` = feature désactivée), `DOWNLOAD_CONCURRENCY` (défaut `1`), `DOWNLOAD_CHUNK_BYTES` (défaut `1048576`), `DOWNLOAD_MAX_RETRIES` (défaut `3`), `DOWNLOAD_MIN_FREE_DISK_MB` (défaut `2048`), timeouts httpx dédiés.

**Non modifié** : `/api/media/*`, `/api/plex/*`, génération `.strm`/NFO existante, `/api/ai/*`, onglets admin existants, auth fail-closed `X-API-Key`.

---

## 9. Métriques de succès & risques produit

**Succès** :
- ≥ 95 % des jobs lancés atteignent `completed` sans retry **manuel** (auto-retry inclus).
- Débit d'un job ≥ 80 % d'un `GET` direct de référence (`curl`) sur la même source (le backend n'est pas le goulot).
- **0** fichier écrit hors `DOWNLOAD_DIR` (invariant sécurité, testé).
- Lancer un téléchargement depuis la liste ≤ 3 clics (titre → version → Télécharger).
- **0** régression sur les endpoints/onglets existants (suite `pytest` verte, additif).

**Risques & mitigations** :
- **Espace disque** → préflight `DOWNLOAD_MIN_FREE_DISK_MB` (F-102), `.part` + rename atomique, échec `failed` explicite si plein.
- **Bande passante** → `DOWNLOAD_CONCURRENCY` (défaut 1), worker master-only (pas de multiplication multi-process).
- **Téléchargements longs** → job background persistant, indépendant du cycle requête HTMX ; reprise `Range` ; survit au redémarrage.
- **Sécurité chemin d'écriture** → destination serveur-side sanitizée + confinée (`realpath` sous `DOWNLOAD_DIR`) ; aucun chemin client accepté (ne pas reproduire la dette `outputDir` verbatim de `/api/plex/generate`, CR-S01).
- **Fuite de credentials** → l'URL Xtream (creds en query string) n'est **jamais** loggée en clair ni renvoyée ; les fichiers téléchargés sont de la vidéo pure (contrairement aux `.strm` qui embarquent les creds).
- **SQLite lock** → écritures de progression fréquentes via `run_with_retry` (ne pas ajouter de dette CR-C04).

---

## 10. Questions ouvertes (avec défaut non bloquant)

1. **Écrasement si fichier déjà présent ?** → Défaut : **skip-if-exists** (idempotent), état `completed` marqué « déjà présent ». Un `overwrite=true` explicite peut être ajouté plus tard.
2. **Écrire NFO/poster à côté de la vidéo ?** → Défaut MVP : **vidéo seule** ; NFO/poster = P2 (F-203, réutilise `nfo_builder`).
3. **Préfixe `[XXX]` sur les dossiers de films adultes ?** → Défaut : **oui** (cohérence avec la bibliothèque `.strm`), mais non bloquant → P2 (F-202).
4. **Concurrence par défaut ?** → **1** (sûr pour la bande passante) ; configurable via `DOWNLOAD_CONCURRENCY`.
5. **Télécharger une version `isBroken` ?** → Défaut : **autorisé mais averti** (l'opérateur décide) ; le job peut échouer et passera `failed`.
6. **Granularité série (saison/épisode fin) ?** → Hors MVP ; maille = source entière (`series_all`). À rouvrir si demande.
7. **Purge/rétention automatique ?** → Défaut : **non** (géré par l'opérateur). À rouvrir si le disque devient un point de douleur.

---

## 11. Séquencement & estimation (pour tech-manager)

| Ordre | Item | Points |
|---|---|---|
| 1 | F-008 Table `download_job` + migration 018 + `download_service` (états, enqueue) | M |
| 2 | F-003 Worker master-only : GET streaming httpx → `.part`→rename, structure `DOWNLOAD_DIR` | L |
| 3 | F-007 Confinement/sanitize chemin + garde `DOWNLOAD_DIR` | S |
| 4 | F-001 Onglet + liste unifiée (réutilise `media_service`) | S |
| 5 | F-002 Fragment versions + option série complète (expansion épisodes) | M |
| 6 | F-004 Suivi états/progression + `GET /admin/downloads/queue` polling | M |
| 7 | F-005 Reprise `Range` + auto-retry + reprise au boot | L |
| 8 | F-006 Annulation (queued + running) | S |
| 9 | F-101 API JSON lecture + `DownloadJob*` schémas + tests | S |
| 10 | F-102 préflight disque · F-103 métriques · F-104 clear-finished | S |
| — | P2 (F-201/202/203) | XS–S chacun, hors MVP |

---

## Handoff

```
NEXT:
- tech-lead: une fois l'architecture CTO posée, utiliser ce PRD pour écrire docs/22-impl-spec-backend.md
  (frontières: config DOWNLOAD_DIR, service download_service, worker master-only, migration 018,
   router admin /admin/downloads + JSON /api/admin/downloads, confinement chemin, run_with_retry)
- tech-manager: séquencer §11 en sprint ; paralléliser db-migration-specialist (F-008) et
  plex-generator-specialist/backend-developer (F-003/F-002)
- qa-engineer: écrire le plan de test sur les Given/When/Then §5 (états, confinement chemin, reprise Range, garde config)
```
