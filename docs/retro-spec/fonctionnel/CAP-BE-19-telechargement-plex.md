# CAP-BE-19 — Téléchargement Plex (« Télécharger Plex »)

**Reconstituée le** : 2026-09-26 · recalée le 2026-10-02 (Lot 10) · **HEAD** : `aa3270f` · **Sources** : inventaire, code, tests, PRD `docs/10-prd-plex-download.md`, `docs/architecture/flux-cles.md` §5.9, board `docs/31-board.md`, dépôt PlexHubTV (recherche négative)
**Périmètre** : UI HTMX `/admin/plex-downloads*` (9 routes, `app/api/admin_plex_downloads.py`) + miroir JSON `/api/admin/plex-downloads/*` (3 routes, `app/api/plex_downloads.py`) ; services `plex_api_service`, `plex_sync_service`, `plex_catalog_service`, `plex_download_service` ; worker partagé `download_worker` (branche `is_plex_server_id`) ; tâche planifiée `plex_catalogue_sync` ; tables `plex_server`/`plex_media_item`/`plex_sync_status` (migrations 019 et 021) — voir `docs/retro-spec/_genere/inventaire.md`.

## Résumé

L'opérateur parcourt, dédupliqué par titre, le catalogue de tous les serveurs Plex Media Server accessibles à son compte plex.tv (possédés ou partagés), puis télécharge physiquement films, séries, saisons ou épisodes vers le même `DOWNLOAD_DIR` que la capacité Xtream (CAP-BE-18), dont il réutilise la file `download_job` et le worker. La découverte des serveurs, le test des connexions et la synchronisation du catalogue (mark-and-sweep + dédup par `unification_id`) sont propres à cette capacité et vivent dans des tables dédiées, jamais dans `media`. L'URL de transfert, qui porte le token, n'est jamais stockée : elle est reconstruite au worker. Si le partage interdit le téléchargement (403), le worker bascule sur l'URL de lecture directe. Persona : opérateur (admin). L'app TV ne consomme pas cette capacité.

## User stories observées

- **US-BE-19.1** (P1) — En tant qu'opérateur, je veux que le backend découvre tous mes serveurs Plex (possédés et partagés) afin de ne pas les déclarer un par un.
  - Parcours : `/admin/plex-downloads` → « Actualiser le catalogue » → `POST /admin/plex-downloads/sync` → polling `…/sync/status` · Maestro : aucun
- **US-BE-19.2** (P1) — En tant qu'opérateur, je vois un film présent sur deux serveurs comme une seule carte afin de choisir la meilleure source sans doublon.
  - Parcours : `/admin/plex-downloads` → `GET …/list?type=movie` · Maestro : aucun
- **US-BE-19.3** (P1) — En tant qu'opérateur, je sélectionne un film, une série entière, des saisons ou des épisodes précis afin de contrôler ce qui est rapatrié.
  - Parcours : carte → `…/versions` → (série) `…/episodes?season=` → `POST /admin/plex-downloads` · Maestro : aucun
- **US-BE-19.4** (P1) — En tant qu'opérateur, je suis les téléchargements Plex dans la même file que les téléchargements Xtream.
  - Parcours : mise en file → fragment de file partagé avec `/admin/downloads` · Maestro : aucun
- **US-BE-19.5** (P2) — En tant qu'automate (QA, script), je lis le catalogue Plex en JSON avec la clé maître.
  - Parcours : `GET /api/admin/plex-downloads/{servers,catalog,catalog/{type}/{id}}` · Maestro : aucun
- **US-BE-19.6** (P2) — En tant qu'opérateur, j'active une resynchronisation périodique du catalogue afin de le garder à jour sans clic.
  - Parcours : `PLEX_SYNC_INTERVAL_HOURS>0` → tâche `plex_catalogue_sync` sur le maître · Maestro : aucun

## Exigences fonctionnelles

**Découverte et secrets**

- **FR-BE-988** — Le système découvre, via les ressources plex.tv, tous les serveurs (possédés et partagés) avec leur token par serveur et leurs connexions candidates. — *implémenté* — preuve `app/services/plex_api_service.py:280-311` · test `tests/test_plex_api_service.py:66`
- **FR-BE-989** — Le token (compte ou serveur) part uniquement en en-tête `X-Plex-Token`, jamais en query ; le client ne logue aucun en-tête. — *implémenté* — preuve `app/services/plex_api_service.py:224-229` · test `tests/test_plex_api_service.py:293`
- **FR-BE-990** — `PlexServer.access_token` est chiffré au repos (`EncryptedString`, même convention que le mot de passe Xtream). — *implémenté* — preuve `app/models/database.py:602` · test : aucun test dédié au chiffrement (seule la non-fuite en sortie est testée, FR-BE-1015 et FR-BE-1036)
- **FR-BE-991** — L'identifiant client Plex est stable : valeur d'environnement, sinon lu ou généré puis persisté sous `DATA_DIR`, avec un identifiant transitoire si le dossier n'est pas inscriptible. — *implémenté* — preuve `app/config.py:442-459`

**Test des connexions**

- **FR-BE-992** — L'ordre de test des connexions dépend de la propriété : serveur possédé = local → public → relais ; partagé = public → local → relais. — *implémenté* — preuve `app/services/plex_sync_service.py:206-213` · test `tests/test_plex_sync.py:187`
- **FR-BE-993** — Le test d'une connexion interroge les sections de bibliothèque sous `PLEX_PROBE_TIMEOUT`, ne lève jamais et ne réussit que sur 200 + JSON `MediaContainer`. — *implémenté* — preuve `app/services/plex_api_service.py:313-334` · test `tests/test_plex_api_service.py:142`
- **FR-BE-994** — `base_uri` et `is_reachable` sont persistés par serveur (upsert par identifiant client), indépendamment du résultat de la synchronisation du catalogue. — *implémenté* — preuve `app/services/plex_sync_service.py:384-419` · test `tests/test_plex_sync.py:355`

**Synchronisation du catalogue**

- **FR-BE-995** — `run_full_sync` enchaîne découverte → test → catalogue par serveur → pont tmdb↔imdb → balayage, avec une session neuve par écriture (aucune transaction ouverte pendant un appel réseau). — *implémenté* — preuve `app/services/plex_sync_service.py:611-695` · test `tests/test_plex_sync.py:110`
- **FR-BE-996** — Le verrou de synchronisation est un `UPDATE … WHERE state='idle'` conditionnel : deux appels concurrents ne peuvent pas gagner tous les deux. — *implémenté* — preuve `app/services/plex_sync_service.py:122-137` · test `tests/test_plex_sync.py:281-286`
- **FR-BE-997** — Une synchronisation lancée pendant qu'une autre tourne renvoie `already_running` sans rien faire. — *implémenté* — preuve `app/services/plex_sync_service.py:630-632` · test `tests/test_plex_sync.py:300-304`
- **FR-BE-998** — Le catalogue est synchronisé en mark-and-sweep par serveur : chaque item vu reçoit l'horodatage du run, les lignes restées en arrière sont supprimées. — *implémenté* — preuve `app/services/plex_sync_service.py:591-605` · test `tests/test_plex_sync.py:250`
- **FR-BE-999** — L'`unification_id` est calculé au sync avec la priorité `imdb` > `tmdb` > clé par source `plexsrc://…`, sans jamais de repli titre+année. — *implémenté* — preuve `app/services/plex_sync_service.py:88-107` · test `tests/test_plex_unification.py:22`
- **FR-BE-1000** — Une passe post-sync convertit l'`unification_id` d'un item connu seulement par tmdb en `imdb://…` quand un autre item du même `tmdb_id` porte un `imdb_id`. — *implémenté* — preuve `app/services/plex_sync_service.py:528-585` · test `tests/test_plex_unification.py:73`
- **FR-BE-1001** — Les genres Plex sont capturés au sync dans `plex_media_item.genres` (liste séparée par virgules). — *implémenté* — preuve `app/services/plex_api_service.py:137-153` · test `tests/test_migration_021.py`
- **FR-BE-1002** — Au démarrage du maître, un statut resté `running` est remis à `idle`, sous condition de `PLEX_ACCOUNT_TOKEN` et indépendamment de `DOWNLOAD_DIR`. — *implémenté* — preuve `app/main.py:539-547`, `app/services/plex_sync_service.py:154-172` · test du service `tests/test_plex_sync.py:293-298` (le branchement dans `main.py` n'est pas testé)
- **FR-BE-1003** — La tâche planifiée `plex_catalogue_sync` (intervalle, `max_instances=1`, maître seul) n'est enregistrée que si `PLEX_ACCOUNT_TOKEN` est renseigné ET `PLEX_SYNC_INTERVAL_HOURS>0`. Sinon, aucune tâche n'est créée. — *implémenté* — preuve `app/main.py:474-494`, `app/config.py:277` · test : seul le wrapper `track_job` est couvert (`tests/test_health_freshness.py:80-85`)
- **FR-BE-1004** — `POST /admin/plex-downloads/sync` lance la synchronisation en tâche de fond et réaffiche aussitôt le panneau de statut. — *implémenté* — preuve `app/api/admin_plex_downloads.py:422-451` · test `tests/test_admin_plex_downloads.py:482`
- **FR-BE-1005** — `GET /admin/plex-downloads/sync/status` renvoie l'état courant et la liste des serveurs (polling HTMX). — *implémenté* — preuve `app/api/admin_plex_downloads.py:454-462`
- **FR-BE-1006** — Chaque sortie de synchronisation incrémente `plexhub_plex_sync_total{result}` (`ok`, `disabled`, `already_running`, `error`). — *implémenté* — preuve `app/services/plex_sync_service.py:626-694`, `app/utils/metrics.py:260-267` · test `tests/test_metrics_registry.py:155-158`
- **FR-BE-1007** — Sans `PLEX_ACCOUNT_TOKEN`, la synchronisation est un no-op explicite (`disabled`) et l'UI affiche un bandeau de configuration manquante. — *implémenté* — preuve `app/services/plex_sync_service.py:626-628`, `app/templates/admin/plex_downloads.html:17-22` · test `tests/test_plex_sync.py:339`
- **FR-BE-1008** — Le message d'erreur de synchronisation est borné à 200 caractères. Il n'est jamais tiré du texte brut d'une exception httpx, qui peut contenir une URL. — *implémenté* — preuve `app/services/plex_sync_service.py:192-204` · test `tests/test_plex_sync.py:310`

**Catalogue unifié (lecture)**

- **FR-BE-1009** — La liste est paginée, dédupliquée en SQL par `GROUP BY unification_id` et triée par ajout le plus récent. — *implémenté* — preuve `app/services/plex_catalog_service.py:160-266` · test `tests/test_plex_catalog.py:93`
- **FR-BE-1010** — Un filtre `search` restreint la liste par titre, jokers échappés. — *implémenté* — preuve `app/services/plex_catalog_service.py:203-206` · test `tests/test_plex_catalog.py:121`
- **FR-BE-1011** — Un filtre `genre` restreint la liste. Une ligne synchronisée avant la migration 021 (`genres` nul) ne correspond jamais au filtre. — *implémenté* — preuve `app/services/plex_catalog_service.py:207-210` · test `tests/test_plex_catalog.py:137`
- **FR-BE-1012** — Le détail d'un groupe renvoie une source par ligne, triée par résolution puis taille décroissantes, ou rien si l'`unification_id` est inconnu. — *implémenté* — preuve `app/services/plex_catalog_service.py:269-312` · test `tests/test_plex_catalog.py:228`
- **FR-BE-1013** — Pour une série, le système agrège par saison et par serveur le nombre d'épisodes, la résolution maximale et la taille totale. — *implémenté* — preuve `app/services/plex_catalog_service.py:315-378` · test `tests/test_plex_catalog.py:288`
- **FR-BE-1014** — Pour une saison, le système liste chaque épisode avec une source par serveur qui le possède (chacune avec son `rating_key`). — *implémenté* — preuve `app/services/plex_catalog_service.py:381-449` · test `tests/test_plex_catalog.py:348`
- **FR-BE-1015** — La liste des serveurs n'expose jamais `access_token` ni `base_uri`. — *implémenté* — preuve `app/services/plex_catalog_service.py:474-493` · test `tests/test_plex_catalog.py:402`
- **FR-BE-1016** — Le statut de synchronisation vaut `idle` par défaut tant que la ligne singleton n'existe pas. — *implémenté* — preuve `app/services/plex_catalog_service.py:452-471`

**Mise en file**

- **FR-BE-1017** — La portée `movie` crée un seul job, avec une destination calculée côté serveur. — *implémenté* — preuve `app/services/plex_download_service.py:179-222` · test `tests/test_plex_download_enqueue.py:84`
- **FR-BE-1018** — La portée `series_all` crée un job par épisode. Une série sans épisode donne une erreur explicite. — *implémenté* — preuve `app/services/plex_download_service.py:225-254` · test `tests/test_plex_download_enqueue.py:184`
- **FR-BE-1019** — La portée `seasons` met en file les épisodes des saisons cochées (une source par saison), sans doublon dans la sélection. — *implémenté* — preuve `app/services/plex_download_service.py:257-307` · test `tests/test_plex_download_enqueue.py:255`
- **FR-BE-1020** — La portée `episodes` met en file exactement les épisodes cochés, chacun avec sa source. — *implémenté* — preuve `app/services/plex_download_service.py:310-355` · test `tests/test_plex_download_enqueue.py:297`
- **FR-BE-1021** — Un couple `(server_id, rating_key)` qui a déjà un job non terminé est réutilisé au lieu d'être dupliqué, quelle que soit la portée. — *implémenté* — preuve `app/services/plex_download_service.py:149-152`, `app/services/download_service.py:599-615`
- **FR-BE-1022** — La destination est confinée sous `DOWNLOAD_DIR` par le même mécanisme que Xtream ; aucun chemin fourni par le client n'est accepté. — *implémenté* — preuve `app/services/plex_download_service.py:92-96`, `app/services/download_service.py:287`
- **FR-BE-1023** — Sans `DOWNLOAD_DIR`, la mise en file renvoie une erreur explicite avant toute écriture. — *implémenté* — preuve `app/services/plex_download_service.py:385-386` · test `tests/test_plex_download_enqueue.py:72`

**Transfert (worker partagé)**

- **FR-BE-1024** — L'URL de transfert n'est jamais persistée : le worker la reconstruit au moment du run, dans une session neuve, à partir du serveur et du `rating_key` du job. — *implémenté* — preuve `app/services/plex_download_service.py:414-465` · test `tests/test_plex_download_worker.py:329`
- **FR-BE-1025** — La résolution renvoie une liste ordonnée : d'abord l'URL de téléchargement (`?download=1`), puis l'URL de lecture directe. Aucune n'est loguée. — *implémenté* — preuve `app/services/plex_download_service.py:414-465`
- **FR-BE-1026** — Le worker de CAP-BE-18 aiguille un job vers la résolution Plex d'après le préfixe `plex_` de `server_id`, sans dupliquer transfert, progression ni relances. — *implémenté* — preuve `app/workers/download_worker.py:581-604`, `app/utils/server_id.py:37`
- **FR-BE-1027** — Un 403 reçu avant le premier octet fait basculer le transfert sur l'URL de repli suivante, sans consommer le budget de relances. Côté Xtream, sans repli, un 403 reste un échec définitif. — *implémenté* — preuve `app/services/download_service.py:481-497`
- **FR-BE-1028** — Un `.nfo` best-effort est écrit à côté d'un téléchargement Plex terminé (titre, année, identifiants, genres, durée), sans `<thumb>` car la vignette exigerait le token. — *implémenté* — preuve `app/services/download_nfo.py:92-143`, `app/workers/download_worker.py:333-335` · test `tests/test_download_nfo.py:91`
- **FR-BE-1029** — Choisir, pour un même item, entre plusieurs versions ou pistes d'un serveur (PRD PC-201). — *absent* (annoncé dans `docs/10-prd-plex-download.md:63` ; un seul élément `Media[]` retenu par item, `app/services/plex_api_service.py:165`)
- **FR-BE-1030** — Proxy d'image pour les vignettes Plex (PRD PC-203). — *absent* (annoncé dans `docs/10-prd-plex-download.md:65` ; aucun endpoint trouvé)

**Miroir JSON**

- **FR-BE-1031** — `GET /api/admin/plex-downloads/servers` liste les serveurs connus sans aucun secret. — *implémenté* — preuve `app/api/plex_downloads.py:53-75` · test `tests/test_plex_downloads_json.py:143`
- **FR-BE-1032** — `GET /api/admin/plex-downloads/catalog` renvoie le catalogue dédupliqué et paginé ; `sources` est vide en liste. — *implémenté* — preuve `app/api/plex_downloads.py:78-110` · test `tests/test_plex_downloads_json.py:193`
- **FR-BE-1033** — `GET /api/admin/plex-downloads/catalog/{type}/{unificationId}` renvoie le détail avec ses sources. Il répond 404 si le type ou l'identifiant est inconnu. Les `://` de l'identifiant passent grâce au convertisseur `:path`. — *implémenté* — preuve `app/api/plex_downloads.py:113-154` · test `tests/test_plex_downloads_json.py:316`
- **FR-BE-1034** — Les trois routes JSON exigent le secret maître : une clé par utilisateur valide est refusée. — *implémenté* — preuve `app/api/plex_downloads.py:46-50` · test `tests/test_plex_downloads_json.py:82-137`

**Accès et cas limites**

- **FR-BE-1035** — Toutes les routes `/admin/plex-downloads*` exigent la Basic Auth admin, posée au montage. — *implémenté* — preuve `app/main.py:690` · test `tests/test_admin_plex_downloads.py:96-131`
- **FR-BE-1036** — Aucune vue HTML ne laisse fuir `access_token` ni `base_uri`, même avec ces valeurs en base. — *implémenté* — preuve `app/api/admin_plex_downloads.py:23-33` · test `tests/test_admin_plex_downloads.py:513-547`
- **FR-BE-1037** — Les POST `/admin/plex-downloads*` sont soumis au contrôle CSRF `Sec-Fetch-Site` commun à `/admin*` : `cross-site` donne 403. — *implémenté* — preuve `app/api/csrf.py:85-114` · test `tests/test_admin_csrf.py:132`
- **FR-BE-1038** — Un `type` hors `movie|show` (versions, épisodes, détail JSON) ou un identifiant inconnu donne 404, jamais 500. — *implémenté* — preuve `app/api/admin_plex_downloads.py:245-250`, `app/api/admin_plex_downloads.py:286-287`
- **FR-BE-1039** — Une sélection invalide (portée inattendue, source manquante, rien de coché) produit un message dans le fragment de file, jamais une 500. — *implémenté* — preuve `app/api/admin_plex_downloads.py:377-410` · test `tests/test_plex_download_enqueue.py:352`
- **FR-BE-1040** — Les tables `plex_server`, `plex_media_item` et `plex_sync_status` sont créées de façon idempotente (migration 019). — *implémenté* — preuve `app/db/migrations.py:862-957` · test `tests/test_migration_019.py:53`
- **FR-BE-1041** — `plex_media_item.genres` est ajoutée nullable (migration 021), sans backfill. — *implémenté* — preuve `app/db/migrations.py:998-1027` · test `tests/test_migration_021.py`
- **FR-BE-1042** — Le catalogue Plex reste isolé de `media` : il n'alimente ni `/api/media`, ni la génération `.strm`/NFO, ni l'app TV (aucune référence dans PlexHubTV). — *implémenté* — preuve `app/models/database.py:591-593` · `tv:` recherche négative (`plex-downloads`, `plex_downloads`, `PlexDownload` : 0 fichier `.kt`)
- **FR-BE-1043** — Le wrapper singulier `resolve_job_url` (premier élément de `resolve_job_urls`) n'avait plus aucun appelant applicatif ; seuls des tests l'appelaient. — *retiré* (Lot 10 : la fonction est supprimée ; les tests visent désormais `resolve_job_urls`, et le PRD plex-download a été corrigé) — preuve de l'absence `app/services/plex_download_service.py:414-465` (seule la forme plurielle existe), `app/workers/download_worker.py:586` · test `tests/test_plex_download_worker.py:329-393`

## Règles métier

- **RG-BE-19.1** — Priorité de dédup `imdb` > `tmdb` > clé par source, sans repli titre+année : deux homonymes sur deux serveurs ne fusionnent jamais. — preuve `app/services/plex_sync_service.py:88-107`
- **RG-BE-19.2** — `server_id` d'une source Plex = `plex_<clientIdentifier>`, stable au renommage, sans collision avec `xtream_<id>`. — preuve `app/utils/server_id.py:18-38`
- **RG-BE-19.3** — La synchronisation périodique est désactivée par défaut (`PLEX_SYNC_INTERVAL_HOURS=0`) : synchronisation manuelle seulement. — preuve `app/config.py:277`, `app/main.py:474`
- **RG-BE-19.4** — Pont tmdb→imdb : ordre déterministe `(server_id, rating_key)` si plusieurs `imdb_id` candidats. — preuve `app/services/plex_sync_service.py:528-585`
- **RG-BE-19.5** — File et worker sont communs à Xtream et Plex ; le seul discriminant est le préfixe de `server_id`. — preuve `app/workers/download_worker.py:581-604`
- **RG-BE-19.6** — Un seul élément `Media[]` retenu par item : le meilleur par hauteur puis débit. — preuve `app/services/plex_api_service.py:165-198`
- **RG-BE-19.7** — Confinement d'écriture identique à Xtream : même `DOWNLOAD_DIR`, même `resolve_confined`. — preuve `app/services/plex_download_service.py:92-96`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| `PLEX_ACCOUNT_TOKEN` vide | synchronisation `disabled`, bandeau UI, pas de tâche planifiée | `app/services/plex_sync_service.py:626-628`, `app/main.py:474` |
| Synchronisation déjà en cours | `already_running`, aucun travail | `app/services/plex_sync_service.py:630-632` |
| Process mort avec statut `running` | remis à `idle` au démarrage du maître | `app/main.py:539-547` |
| Serveur injoignable au test | `is_reachable=False` persisté, catalogue précédent conservé | `app/services/plex_sync_service.py:646-647` |
| `type` inconnu / `unificationId` inconnu | 404 | `app/api/admin_plex_downloads.py:245-250`, `app/api/plex_downloads.py:129-134` |
| Sélection invalide ou vide | message dans le fragment de file | `app/api/admin_plex_downloads.py:377-410` |
| Item Plex absent à la mise en file | erreur « Média Plex introuvable » / « Série Plex introuvable » | `app/services/plex_download_service.py:185-187`, `app/services/plex_download_service.py:232-234` |
| Job non terminé déjà présent | réutilisé | `app/services/plex_download_service.py:149-152` |
| Partage « Allow Downloads » désactivé (403) | bascule sur la lecture directe, relances non consommées | `app/services/download_service.py:481-495` |
| Source Plex disparue au moment du run | job en échec « source Plex introuvable ou non synchronisée », sans relance | `app/workers/download_worker.py:585-589` |
| `DOWNLOAD_DIR` vide | mise en file refusée | `app/services/plex_download_service.py:385-386` |
| JSON sans clé maître | 401 | `app/api/plex_downloads.py:46-50` |
| UI sans Basic Auth | 401 | `app/main.py:690` |
| POST cross-site | 403 | `app/api/csrf.py:98-114` |

## Dépendances

- Autres capacités : `CAP-BE-18` (table `download_job`/`download_batch`, worker, `compute_dest_path`/`resolve_confined`/`download_to_disk`/`find_non_terminal_job` réutilisés), `CAP-BE-20` (l'écran unifié fusionne `plex_catalog_service.list_unified` avec le catalogue Xtream et délègue la mise en file à ce routeur, `app/main.py:691-695`), `CAP-BE-02` (`verify_master_key`, Basic Auth admin, CSRF), `CAP-BE-01` (métrique `plexhub_plex_sync_total`, fraîcheur `track_job`). Aucune dépendance vers `CAP-BE-16` (la génération de bibliothèque ne lit jamais `plex_media_item`).
- Autre dépôt : aucun appel de PlexHubTV vers `/admin/plex-downloads` ni `/api/admin/plex-downloads`.
- Données : `plex_server`, `plex_media_item`, `plex_sync_status` (019, `genres` en 021) et `download_job` partagée — voir `docs/retro-spec/technique/db/`.

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| Le PRD classe PC-202 (NFO sidecar Plex) « Non livré » (lignes 64 et 103). Or le code l'écrit (FR-BE-1028) et le board le marque résolu (DL-PLEX-03). | spec ↔ code (PRD périmé) | mettre à jour le PRD — à trancher par Chakir |
| Le PRD PC-006 (ligne 59) et CLAUDE.md §2 décrivaient le worker comme appelant resolve_job_url (singulier). Le worker appelait en réalité `resolve_job_urls`, et le singulier n'avait plus d'appelant (FR-BE-1043). Wrapper supprimé et PRD corrigé au Lot 10 (`docs/10-prd-plex-download.md`). | spec ↔ code · code mort (résolu) | aucune |
| Le board (DL-PLEX-06) indique « aucun `plexhub_plex_sync_*` », alors que la métrique existe et est testée (FR-BE-1006). | board périmé | corriger le board |
| Le PRD (ligne 162) annonce la « migration courante = 019 », alors que la chaîne va désormais jusqu'à 028. | PRD daté | aucune (document historique) |
| L'enregistrement conditionnel de la tâche `plex_catalogue_sync` et le reset de démarrage dans `main.py` ne sont pas testés (FR-BE-1002, FR-BE-1003). | test manquant | test de lifespan : présence ou absence de la tâche selon la configuration |
| Aucun test ne vérifie que `access_token` est réellement chiffré en base (FR-BE-990). | test manquant | test de lecture brute de la colonne |

## Questions ouvertes

- Si un serveur change d'adresse entre deux synchronisations, `base_uri` est périmée et il n'y a pas de nouveau test de connexion au moment du téléchargement. Le job échoue-t-il en transitoire (relances) ou en définitif ? — **à vérifier par test**.
- Un serveur qui tombe entre la mise en file et le run garde une URL reconstruite a priori valide ; le classement de l'échec réseau (transitoire ou définitif) sur ce chemin Plex n'est couvert par aucun test — **à vérifier par test**.
- Un compte plex.tv dont l'accès à un serveur partagé est retiré : le mark-and-sweep supprime-t-il les items de ce serveur, et que deviennent ses jobs `queued` déjà en file ? — **à vérifier par test**.
- Deux titres identiques sans guid commun restent deux cartes distinctes (RG-BE-19.1), ce qui est voulu. Aucun test d'UI ne vérifie le rendu de ce cas — **à vérifier par test**.

## Écart fiche ↔ PRD

PRD : `docs/10-prd-plex-download.md` (exigences PC-001→PC-203, lignes 54-65 ; non-goals lignes 102-104).

| Exigence PRD | Ligne PRD | FR | Verdict |
|---|---|---|---|
| PC-001 Découverte serveurs plex.tv | 54 | FR-BE-988, FR-BE-989, FR-BE-992, FR-BE-993 | couvert |
| PC-002 Tables dédiées (019) | 55 | FR-BE-1040, FR-BE-990, FR-BE-1042 | couvert |
| PC-003 Sync mark-and-sweep | 56 | FR-BE-995→FR-BE-998, FR-BE-1002, FR-BE-1004 | couvert |
| PC-004 Dédup lecture `unification_id` | 57 | FR-BE-999, FR-BE-1009, FR-BE-1012 | couvert |
| PC-005 Mise en file dans la file existante | 58 | FR-BE-1017→FR-BE-1023 | couvert |
| PC-006 Worker branché sans duplication | 59 | FR-BE-1026, FR-BE-1043 | couvert, mais le PRD nomme le wrapper singulier, désormais **mort** |
| PC-007 UI `/admin/plex-downloads` | 60 | FR-BE-1004, FR-BE-1005, FR-BE-1035, FR-BE-1036 | couvert |
| PC-101 Miroir JSON | 61 | FR-BE-1031→FR-BE-1034 | couvert |
| PC-102 Synchronisation périodique optionnelle | 62 | FR-BE-1003 | couvert (enregistrement non testé) |
| PC-201 Multi-versions par serveur (P2) | 63 | FR-BE-1029 | **absent** (conforme au PRD) |
| PC-202 NFO sidecar (P2) | 64, 103 | FR-BE-1028 | **contredit** : livré, le PRD dit « non livré » |
| PC-203 Vignettes proxifiées (P2) | 65, 104 | FR-BE-1030 | **absent** (conforme au PRD) |

**Trouvé en plus du PRD** : bascule 403 vers la lecture directe (FR-BE-1025, FR-BE-1027) ; pont tmdb→imdb (FR-BE-1000) ; ordre de test selon la propriété du serveur (FR-BE-992) ; filtre `genre` et migration 021 (FR-BE-1011, FR-BE-1041) ; métrique `plexhub_plex_sync_total` (FR-BE-1006) ; mise en file par saisons ou épisodes avec une source chacun (FR-BE-1019, FR-BE-1020) ; contrôle CSRF (FR-BE-1037) ; identifiant client persistant (FR-BE-991).
**Raté par la fiche** : aucune exigence du PRD n'est restée sans correspondance.
