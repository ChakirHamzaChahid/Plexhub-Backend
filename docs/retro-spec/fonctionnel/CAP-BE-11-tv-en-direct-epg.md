# CAP-BE-11 — TV en direct & EPG

**Reconstituée le** : 2026-09-25 · **HEAD** : `10aa139` · **Sources** : code, tests, `CLAUDE.md` (§2), `docs/architecture/flux-cles.md` (§5.1), app TV (lecture seule, preuves `tv:`). Aucun PRD ne couvre cette capacité (`docs/10-prd-*.md` ne traitent que les téléchargements) : pas de tableau d'écart fiche ↔ PRD ; les écarts sont face à `CLAUDE.md` et aux docs d'architecture.
**Périmètre** : `GET /api/live/channels` (`app/api/live.py:28`), `GET /api/live/channels/{stream_id}` (`app/api/live.py:86`), `GET /api/live/channels/{stream_id}/stream` (`app/api/live.py:105`, détaillé par CAP-BE-10), `GET /api/live/channels/{stream_id}/epg` (`app/api/live.py:143`), `GET /api/live/epg` (`app/api/live.py:201`) ; service `app/services/live_service.py` (EPG à la demande) ; client `app/services/xtream_service.py` ; visibilité `app/services/category_service.py` ; mappage live de la synchro `app/workers/sync_worker.py:386-421` (synchro détaillée par CAP-BE-05) ; purge `epg_cleanup` (`app/main.py:243-269`, détaillée par CAP-BE-05). Modèles `app/models/database.py:196-266`, schémas `app/models/schemas.py:478-536`, migration 006 `app/db/migrations.py:211-281`. Renvoi : `docs/retro-spec/_genere/inventaire.md` ; tables `live_channels` et `epg_entries` dans `docs/retro-spec/technique/db/`.

## Résumé

Le backend expose les chaînes Live IPTV des comptes Xtream, répliquées localement dans `live_channels` par la synchro, et un guide des programmes (EPG). Contrairement au VOD, **l'EPG n'est jamais synchronisé en masse** : il est chargé à la demande, chaîne par chaîne, via le « short EPG » Xtream, puis mis en cache dans `epg_entries` et purgé chaque nuit. L'app TV liste les chaînes (tous comptes confondus), résout l'URL de lecture (CAP-BE-10), affiche le programme en cours de chaque chaîne et le « maintenant / ensuite » d'une chaîne. Seule la visibilité par catégorie (`is_in_allowed_categories`) filtre la liste : ni le drapeau adulte, ni `is_active`, ni le masquage de panne (CAP-BE-08) ne s'appliquent au live. Personas : spectateur TV ; opérateur (via l'API) ; planificateur (purge nocturne). Un seul parcours Maestro touche le live (`tv:.maestro/16_iptv.yaml`, navigation seulement).

## User stories observées

- **US-BE-11.1** (P1) — En tant que spectateur TV, je parcours la liste des chaînes live avec recherche et tri afin de trouver une chaîne à regarder.
  - Parcours : écran IPTV ou Live TV → `GET /api/live/channels` (`app/api/live.py:28`) · Maestro : `tv:.maestro/16_iptv.yaml` (navigation seule, aucune assertion sur le contenu)
- **US-BE-11.2** (P1) — En tant que spectateur TV, je vois ce qui passe maintenant sur chaque chaîne afin de choisir sans zapper.
  - Parcours : écran IPTV → `tv:app/src/main/java/com/chakir/plexhubtv/feature/iptv/IptvViewModel.kt:687-709` → `GET /api/live/epg` (`app/api/live.py:201`) · Maestro : aucun
- **US-BE-11.3** (P2) — En tant que spectateur TV, je consulte le programme en cours et à suivre d'une chaîne afin de planifier mon visionnage.
  - Parcours : grille EPG → `tv:domain/src/main/java/com/chakir/plexhubtv/domain/usecase/GetChannelNowNextUseCase.kt:14-34` → `GET /api/live/channels/{stream_id}/epg` (`app/api/live.py:143`) · Maestro : aucun
- **US-BE-11.4** (P3) — En tant que planificateur, je purge chaque nuit les programmes échus afin que la base ne grossisse pas indéfiniment.
  - Parcours : cron `epg_cleanup` (`app/main.py:375-383`) → `_cleanup_stale_epg` (`app/main.py:243-269`) · Maestro : sans objet (détail : CAP-BE-05, FR-BE-223)
- **US-BE-11.5** (P3) — En tant que client détenant une clé API, je consulte le détail d'une chaîne par son `stream_id`. Aucun parcours TV : l'endpoint n'est jamais appelé par l'app (FR-BE-538).
  - Parcours : `GET /api/live/channels/{stream_id}` (`app/api/live.py:86`) · Maestro : sans objet

## Exigences fonctionnelles

### Liste des chaînes (`GET /api/live/channels`)

- **FR-BE-531** — L'endpoint pagine (`limit` 1-5000, défaut 500 ; `offset` ≥ 0), trie (`name_asc`, `name_desc`, `added_asc`, `added_desc` ; toute autre valeur retombe sur `name_asc`) et filtre par `server_id` et `category_id` exacts et par `search` (sous-chaîne insensible à la casse sur `name`, avec `%`, `_` et `\` échappés). — *implémenté* — preuve `app/api/live.py:28-77` · test `tests/test_live_channels_query.py:54`, `tests/test_live_channels_query.py:70`, `tests/test_live_channels_query.py:85` (tri et `category_id` non testés)
- **FR-BE-532** — Le total est un `COUNT` étroit sur les mêmes filtres que la page, indépendant de `limit`/`offset` ; `hasMore` vaut `offset + limit < total`. — *implémenté* — preuve `app/api/live.py:52-61`, `app/api/live.py:79-83` · test `tests/test_live_channels_query.py:54`
- **FR-BE-533** — `is_in_allowed_categories = vrai` est un filtre systématique : aucun paramètre ne permet de lister les chaînes masquées par catégorie. — *implémenté* — preuve `app/api/live.py:39-41` · test : aucun (les fixtures posent toujours `is_in_allowed_categories=True`, `tests/test_live_channels_query.py:22-28`)
- **FR-BE-534** — Aucun filtre sur `is_adult` : les chaînes adultes visibles par catégorie sont listées comme les autres, sans préfixe `[XXX] ` (le préfixe n'existe que pour les films, `app/models/schemas.py:16-19`, `app/models/schemas.py:117`). — *implémenté* (comportement constaté) — preuve `app/api/live.py:39-50` · test : aucun
- **FR-BE-535** — `is_active` est exposé dans la réponse mais n'est jamais lu par une requête et n'est écrit qu'à `True` par la synchro : la colonne vaut toujours vrai. — *mort* — preuve `app/models/database.py:222`, `app/models/schemas.py:500`, `app/workers/sync_worker.py:407` · test : aucun
- **FR-BE-536** — Le `server_id` de la liste n'est pas validé par `parse_server_id` : une valeur malformée ne donne pas 400, elle filtre sur une valeur qui ne correspond à aucune ligne (liste vide). — *implémenté* (incohérence avec `/stream` et `/epg` par chaîne) — preuve `app/api/live.py:33`, `app/api/live.py:43-44` · test : aucun

### Détail d'une chaîne (`GET /api/live/channels/{stream_id}`)

- **FR-BE-537** — `server_id` est obligatoire (sinon 422) ; la paire `(stream_id, server_id)` doit exister, sinon 404 « Channel not found ». Pas de validation du format de `server_id`, et pas de filtre de visibilité : une chaîne masquée par catégorie reste lisible par son identifiant. — *implémenté* — preuve `app/api/live.py:86-102` · test `tests/test_router_http_coverage.py:315-328`
- **FR-BE-538** — L'app TV n'appelle jamais cet endpoint : la méthode Retrofit `getLiveChannel` est déclarée mais absente de l'interface `BackendRepository` et sans appelant. — *mort* (côté TV ; l'endpoint backend fonctionne) — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:130`, `tv:domain/src/main/java/com/chakir/plexhubtv/domain/repository/BackendRepository.kt:154` (seul `getLiveChannels` y figure) · test : aucun

### URL de lecture live (`GET /api/live/channels/{stream_id}/stream`)

- **FR-BE-539** — Résolution de l'URL live (validation `server_id`, compte, chaîne en base, extension `container_extension` sinon `ts`) : détail dans CAP-BE-10 (FR-BE-411, FR-BE-412). — *implémenté* — preuve `app/api/live.py:105-140` · test `tests/test_router_http_coverage.py:332-359`

### EPG par chaîne (`GET /api/live/channels/{stream_id}/epg`)

- **FR-BE-540** — La route lit d'abord `epg_entries` (`stream_id`, `server_id`, `end_time >= maintenant`, tri par `start_time`) ; si le résultat n'est pas vide, elle le sert sans rappeler le fournisseur, quel que soit l'âge de `fetched_at` (pas de rafraîchissement anticipé). — *implémenté* — preuve `app/api/live.py:154-169` · test `tests/test_router_http_coverage.py:363`, `tests/test_router_http_coverage.py:407-418` (second appel avec fournisseur piégé : pas de re-fetch)
- **FR-BE-541** — Cache vide : `live_service.ingest_short_epg` valide `server_id` (400 « Invalid server_id format ») et le compte (404 « Account not found »), mais **ne vérifie pas que la chaîne existe ou est visible** dans `live_channels`, contrairement à `/stream`. Un `stream_id` hors catalogue local, ou masqué par catégorie, déclenche un appel Xtream réel. — *implémenté* (incohérence constatée) — preuve `app/api/live.py:171-179`, `app/services/live_service.py:73-88` · test `tests/test_router_http_coverage.py:420`, `tests/test_router_http_coverage.py:427` (le cas « chaîne hors catalogue » n'est pas testé)
- **FR-BE-542** — L'appel fournisseur est `get_short_epg(account, stream_id=…)` sans `limit` : le nombre de programmes dépend du défaut du fournisseur. — *implémenté* — preuve `app/services/live_service.py:85`, `app/services/xtream_service.py:227-243` · test : aucun
- **FR-BE-543** — Les programmes reçus sont ajoutés en base (`db.add`, pas d'upsert) ; `start_timestamp`/`stop_timestamp` (secondes) sont convertis en millisecondes ; `epg_channel_id` vaut `epg_id` ou chaîne vide ; titre absent → « Unknown ». — *implémenté* — preuve `app/services/live_service.py:93-132` · test `tests/test_live_service.py:57`
- **FR-BE-544** — Titre et description sont décodés en base64 seulement si le résultat est du texte lisible : refus si le décodage contient un caractère de contrôle (hors `\n\r\t`) ou le caractère de remplacement U+FFFD ; sinon la valeur d'origine est gardée. — *implémenté* — preuve `app/services/live_service.py:35-50` · test `tests/test_live_service.py:106`, `tests/test_utilities.py:20-53`
- **FR-BE-545** — Une exception du fournisseur pendant `get_short_epg` dégrade en liste vide avec un `WARNING` : la route répond 200 `{items: [], total: 0}`, jamais 502. Rien n'est mis en cache, donc chaque appel suivant retente le fournisseur. — *implémenté* — preuve `app/services/live_service.py:84-88`, `app/api/live.py:181` · test `tests/test_live_service.py:38`
- **FR-BE-546** — Cette route n'a ni `limit` ni `offset` : elle renvoie tout ce que le cache ou le fournisseur donne pour la chaîne. — *implémenté* — preuve `app/api/live.py:143-148` · test : aucun
- **FR-BE-547** — Les nouvelles lignes sont validées par `commit_with_retry` (qui ne rattrape que `OperationalError`) sur la même session que l'appel réseau, choix documenté pour ne pas rappeler le fournisseur à chaque tentative. Aucune garde contre un doublon `(server_id, stream_id, start_time)` : sur une base portant l'index unique `uix_epg_dedup`, deux cache-miss concurrents peuvent lever une `IntegrityError` non rattrapée. — *partiel* (manque : garde anti-doublon ; comportement déduit, non observé) — preuve `app/api/live.py:181-193`, `app/models/database.py:265` · test : aucun
- **FR-BE-548** — Les programmes fraîchement récupérés sont renvoyés **sans** le filtre `end_time >= maintenant` appliqué au chemin cache ; un programme sans heure de fin exploitable est stocké avec `end_time = 0`, n'est donc jamais relu par le cache, et relance l'appel fournisseur à la requête suivante. — *implémenté* (comportement constaté) — preuve `app/services/live_service.py:105-108`, `app/api/live.py:159`, `app/api/live.py:195-198` · test : aucun
- **FR-BE-549** — Le code annonce des heures « epoch secondes ou chaînes date-heure », mais seules les valeurs purement numériques sont interprétées : un programme dont `start` est une date-heure texte est ignoré silencieusement. — *partiel* (manque : analyse des dates texte) — preuve `app/services/live_service.py:97-111` · test : aucun

### EPG « maintenant » (`GET /api/live/epg`)

- **FR-BE-550** — Pour un `server_id`, renvoie les programmes couvrant l'instant courant (`start_time <= maintenant <= end_time`), triés par `start_time`, paginés (`limit` 1-5000 défaut 500, `offset`), total par sous-requête `COUNT`. — *implémenté* — preuve `app/api/live.py:201-227` · test `tests/test_router_http_coverage.py:436`, `tests/test_router_http_coverage.py:461`
- **FR-BE-551** — Aucune validation de `server_id` : une valeur malformée ou inconnue donne 200 et une liste vide. — *implémenté* (incohérence avec l'EPG par chaîne) — preuve `app/api/live.py:201-215` · test : aucun
- **FR-BE-552** — La requête ne joint pas `live_channels` : ni visibilité par catégorie ni drapeau adulte. Le programme en cours d'une chaîne masquée reste servi tant qu'il est en cache. Cet endpoint ne sert que ce qui a déjà été chargé à la demande par l'EPG par chaîne. — *implémenté* (comportement constaté) — preuve `app/api/live.py:211-215` · test : aucun

### Modèles et tables

- **FR-BE-553** — `live_channels` a pour clé primaire `(stream_id, server_id)`. `tv_archive`/`tv_archive_duration` (catch-up) sont alimentés par la synchro, mais aucun endpoint ne sert d'archive ni n'accepte un programme passé. — *mort* (colonnes remplies, aucun consommateur backend) — preuve `app/models/database.py:199-216`, `app/workers/sync_worker.py:404-405` · test : aucun
- **FR-BE-554** — Le modèle ORM déclare `ix_live_channels_server_visible`, `ix_live_channels_server_category`, `ix_epg_server_channel`, `ix_epg_stream_time` et l'index unique `uix_epg_dedup`, mais la migration 006 ne les crée pas. `create_all` (lancé avant les migrations) ne les pose que lorsqu'il crée la table : une base dont les tables existaient avant l'ajout de ces index au modèle ne les a pas. — *partiel* (manque : migration pour les bases existantes) — preuve `app/models/database.py:230-266`, `app/db/migrations.py:241-277`, `app/db/database.py:128-131` · test : aucun
- **FR-BE-555** — Les réponses `LiveChannelResponse`, `EpgEntryResponse` et `EpgListResponse` sont en camelCase (`streamId`, `epgChannelId`, `tvArchive`, `startTime`…) via `to_camel`. — *implémenté* — preuve `app/models/schemas.py:480-536` · test `tests/test_live_channels_query.py:54` (`hasMore`)

### Synchronisation, adulte et visibilité

- **FR-BE-556** — La liste des chaînes est synchronisée de façon incrémentale (`dto_hash`) avec nettoyage différentiel : détail dans CAP-BE-05 (FR-BE-220). — *implémenté* — preuve `app/workers/sync_worker.py:414-421`, `app/workers/sync_worker.py:431-435` · test : aucun direct
- **FR-BE-557** — `is_adult` d'une chaîne vient directement du champ fournisseur, converti par `bool(...)`, sans recalcul par mot-clé de catégorie : `update_media_adult_flags` ne traite que les films. Même conversion pour `tv_archive`. — *implémenté* (asymétrie avec le VOD) — preuve `app/workers/sync_worker.py:404`, `app/workers/sync_worker.py:406`, `app/services/category_service.py:441-497` · test : aucun
- **FR-BE-558** — La visibilité `is_in_allowed_categories` des chaînes est recalculée à chaque synchro selon le mode de filtrage du compte (`all`, `whitelist`, `blacklist`), dans une mise à jour distincte de celle du VOD. — *implémenté* — preuve `app/services/category_service.py:293-298`, `app/services/category_service.py:402-420` · test : aucun direct de la branche live
- **FR-BE-559** — La synchro récupère l'EPG. — *absent* (annoncé par `CLAUDE.md` §2 et `docs/architecture/flux-cles.md` §5.1 ; déjà relevé par CAP-BE-05, FR-BE-222 : l'EPG est exclusivement chargé à la demande, FR-BE-540)
- **FR-BE-560** — `xtream_service.get_xmltv` (XMLTV complet du compte) n'est appelé par aucun module de `app/`. — *mort* — preuve `app/services/xtream_service.py:245-256` · test : aucun

### Purge et sécurité

- **FR-BE-561** — Chaque nuit à 3 h, sur le master, les programmes dont `end_time` est passé sont supprimés : détail dans CAP-BE-05 (FR-BE-223). — *implémenté* — preuve `app/main.py:243-269`, `app/main.py:375-383` · test : aucun direct de la suppression
- **FR-BE-562** — Les cinq routes sont montées sous `verify_backend_secret` (fail-closed, `X-API-Key`) et couvertes par l'assertion de boot sur les routes `/api/*`. — *implémenté* — preuve `app/main.py:640`, `app/api/deps.py:64-73` · test `tests/test_auth_guard.py:43`

### Côté app TV (lecture seule)

- **FR-BE-563** — La TV appelle `getLiveChannels` avec `server_id` toujours `null` (agrégation de tous les comptes backend), et transmet `categoryId` et `search`. — *implémenté* — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:786-823` (ligne 801) · test : aucun
- **FR-BE-564** — L'écran IPTV transmet `search`/`categoryId` puis refiltre côté client ; l'écran Live TV n'envoie jamais `search` et applique un masquage local de catégories (réglage TV) distinct de `is_in_allowed_categories`. — *implémenté* — preuve `tv:app/src/main/java/com/chakir/plexhubtv/feature/iptv/IptvViewModel.kt:556-581`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/livetv/LiveTvViewModel.kt:196-208` · test : aucun
- **FR-BE-565** — `tvArchive`, `tvArchiveDuration` et `isAdult` sont mappés jusqu'au modèle de domaine TV, mais aucun écran live ne les lit (pas de catch-up, pas de filtre adulte) ; `customSid` et `isActive` sont abandonnés au mappage. — *mort* (côté TV) — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:804-819` · test : aucun
- **FR-BE-566** — `getLiveStream` est appelé sans cache à chaque lecture ou aperçu : détail dans CAP-BE-10. — *implémenté* — preuve `tv:app/src/main/java/com/chakir/plexhubtv/feature/livetv/LiveTvViewModel.kt:260`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/iptv/IptvViewModel.kt:631`
- **FR-BE-567** — La TV tient un cache EPG local (Room) : pour un compte géré par le backend, alimenté par `getCurrentEpg` (programmes en cours seulement) et complété par `getChannelEpg` quand le cache est froid ; pour un compte Xtream non géré par le backend, XMLTV lu directement chez le fournisseur. — *implémenté* — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/EpgRepositoryImpl.kt:47-71`, `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/EpgRepositoryImpl.kt:184-260` · test : aucun
- **FR-BE-568** — La TV normalise défensivement les horodatages EPG (`× 1000` s'ils ressemblent à des secondes) ; sans effet puisque le backend renvoie des millisecondes. — *implémenté* — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:844-845`, `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:898` · test : aucun
- **FR-BE-569** — Le seul parcours Maestro live vérifie la navigation vers l'écran IPTV et sa stabilité ; ni la liste, ni l'EPG, ni la lecture ne sont vérifiés. — *implémenté* (couverture limitée) — preuve `tv:.maestro/16_iptv.yaml:1-20`

## Règles métier

- **RG-BE-11.1** — Seul `is_in_allowed_categories` filtre la liste des chaînes ; `is_active` (toujours vrai) et `is_adult` ne filtrent jamais. — preuve `app/api/live.py:39-41`
- **RG-BE-11.2** — Le masquage de panne fournisseur (CAP-BE-08) ne s'applique pas au live : aucune route de `app/api/live.py` ne consulte `account_outage_service`. — preuve `app/api/live.py:1-21` (imports)
- **RG-BE-11.3** — L'indicateur adulte d'une chaîne est la donnée fournisseur, jamais recalculée depuis la catégorie (contrairement aux films). — preuve `app/workers/sync_worker.py:406`
- **RG-BE-11.4** — L'EPG est chargé à la demande par chaîne et servi depuis le cache tant qu'au moins un programme n'est pas terminé ; purge nocturne des programmes échus. — preuve `app/api/live.py:154-169`, `app/main.py:243-269`
- **RG-BE-11.5** — `server_id` = `xtream_<id>` : validé par `parse_server_id` sur `/stream` et `/epg` par chaîne seulement, pas sur la liste, le détail ni l'EPG « maintenant ». — preuve `app/api/live.py:112`, `app/services/live_service.py:73-75`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| `server_id` malformé sur la liste | 200, liste vide | `app/api/live.py:43-44` |
| `server_id` absent sur le détail, `/stream`, `/epg` | 422 | `app/api/live.py:89`, `app/api/live.py:146`, `app/api/live.py:203` |
| Chaîne inconnue sur le détail | 404 « Channel not found » | `app/api/live.py:100-101` |
| `server_id` malformé sur l'EPG par chaîne | 400 « Invalid server_id format » | `app/api/live.py:176-177` |
| Compte inconnu sur l'EPG par chaîne | 404 « Account not found » | `app/api/live.py:178-179` |
| `server_id` malformé ou inconnu sur l'EPG « maintenant » | 200, liste vide | `app/api/live.py:211-215` |
| `stream_id` hors catalogue sur l'EPG par chaîne | appel Xtream tenté quand même | `app/services/live_service.py:73-88` |
| Fournisseur injoignable pendant le short EPG | 200 `{items: [], total: 0}`, `WARNING`, rien en cache | `app/services/live_service.py:84-88` |
| Programme sans heure de fin | stocké avec `end_time = 0`, renvoyé une fois, jamais relu du cache | `app/services/live_service.py:105-108` |
| Deux cache-miss concurrents | `IntegrityError` possible si `uix_epg_dedup` existe, doublons sinon (déduit) | `app/api/live.py:181-193` |
| `search` contenant `%`, `_`, `\` | échappés, pas d'injection de motif LIKE | `app/api/live.py:47-50` |
| `limit` hors 1-5000 | 422 | `app/api/live.py:30`, `app/api/live.py:204` |
| Demande d'un programme passé (catch-up) | aucun endpoint | `app/models/database.py:214-216` |

## Dépendances

- Autres capacités : `CAP-BE-02` (garde `X-API-Key`), `CAP-BE-04` (comptes, catégories, visibilité, suppression en cascade des chaînes et de l'EPG), `CAP-BE-05` (synchro live, purge EPG — FR-BE-220 à FR-BE-223), `CAP-BE-08` (masquage de panne, non appliqué au live), `CAP-BE-10` (URL de lecture live, FR-BE-411 et FR-BE-412).
- Autre dépôt : appels `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:119-147` ; consommateurs `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:786-898`, `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/EpgRepositoryImpl.kt:47-71` ; écrans IPTV et Live TV ; Maestro `tv:.maestro/16_iptv.yaml`.
- Données : `live_channels`, `epg_entries`, `xtream_accounts`, `xtream_categories` (voir `docs/retro-spec/technique/db/`).

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| CLAUDE.md §2 et flux-cles §5.1 annoncent une synchro « Live + EPG » ; l'EPG est à la demande (FR-BE-559) | doc ↔ code | déjà relevé par CAP-BE-05 : corriger la doc |
| Index du modèle (`uix_epg_dedup`, `ix_epg_stream_time`, `ix_epg_server_channel`, `ix_live_channels_server_visible`, `ix_live_channels_server_category`) absents des migrations (FR-BE-554) | modèle ↔ migration | migration `CREATE … INDEX IF NOT EXISTS` (dédoublonner avant l'index unique), à trancher par Chakir |
| L'EPG par chaîne ne vérifie pas que la chaîne existe ou est visible, contrairement à `/stream` (FR-BE-541) | incohérence entre routes | aligner sur `/stream`, à trancher par Chakir |
| L'EPG « maintenant » ignore la visibilité par catégorie (FR-BE-552) | fuite d'information mineure | joindre `live_channels.is_in_allowed_categories`, à trancher par Chakir |
| Validation de `server_id` non uniforme entre les 5 routes (FR-BE-536, FR-BE-551) | incohérence de contrat | `parse_server_id` partout |
| Programme sans heure de fin → `end_time = 0` et re-fetch à chaque appel ; programmes échus renvoyés au premier chargement (FR-BE-548) | comportement douteux | filtrer à l'ingestion, à trancher par Chakir |
| Heures en date-heure texte ignorées malgré le commentaire (FR-BE-549) | code ↔ commentaire | analyser ou corriger le commentaire |
| `get_xmltv` jamais appelé (FR-BE-560) ; `is_active` toujours vrai (FR-BE-535) ; `tv_archive*` sans consommateur backend ni écran TV (FR-BE-553, FR-BE-565) | code mort | supprimer ou implémenter le catch-up, à trancher par Chakir |
| `getLiveChannel` déclaré côté TV, jamais appelé (FR-BE-538) | code mort (TV) | retirer côté TV |
| `EpgEntryResponse.epg_channel_id` est `Optional[str]` (`app/models/schemas.py:522`) mais `BackendEpgEntryDto.epgChannelId` est non-nullable côté TV (`tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendDto.kt:245`) ; en pratique jamais `null` (colonne `NOT NULL`, ingestion `or ""`) | contrat ↔ client | resserrer le schéma en `str` |
| Aucun test : mappage live de la synchro, suppression EPG nocturne, cache-miss concurrents, chaîne hors catalogue, tri et `category_id` de la liste | test manquant | tickets de test |

## Questions ouvertes

- `bool(dto.get("is_adult", 0))` et `bool(dto.get("tv_archive", 0))` : si un panel renvoie la chaîne `"0"`, `bool("0")` vaut vrai et toutes les chaînes seraient marquées adultes et « archivables ». Les panels réels renvoient-ils des entiers ou des chaînes ? — à vérifier par test (réponse `get_live_streams` enregistrée, puis `map_live_stream_to_channel`).
- Un `stream_id` inconnu du catalogue local (ou masqué) renvoie-t-il réellement l'EPG du fournisseur via `/channels/{id}/epg` ? — à vérifier par test.
- Deux cache-miss concurrents sur la même chaîne donnent-ils une 500 (`IntegrityError`) sur une base portant `uix_epg_dedup`, ou des doublons sur une base sans l'index ? — à vérifier par test.
- La base de production porte-t-elle `uix_epg_dedup` et les index composites, ou seulement les bases créées par `create_all` ? — à vérifier par test (`PRAGMA index_list('epg_entries')` sur une copie de schéma).
- Un short EPG qui renvoie un programme déjà stocké et échu (non encore purgé) mais de même `start_time` peut-il provoquer une violation d'unicité ? — à vérifier par test.
- `tv_archive` est-il le vestige d'un catch-up abandonné ou une fonctionnalité prévue ? — à vérifier par test (ou à trancher par Chakir).
