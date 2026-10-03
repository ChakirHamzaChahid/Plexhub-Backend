# CAP-BE-09 — Catalogue VOD & séries

**Reconstituée le** : 2026-09-25 · recalée le 2026-10-02 (Lot 10) · **HEAD** : `aa3270f` · **Sources** : code, tests, `CLAUDE.md` (§2, §9 pièges 15, 20, 22), `docs/architecture/flux-cles.md` (§5.4, §5.13), `docs/retro-spec/fonctionnel/00-carte.md`, fiche `CAP-BE-08`, app TV (lecture seule, preuves `tv:`). Aucun PRD ne couvre cette capacité (`docs/10-prd-media-download.md` et `docs/10-prd-plex-download.md` ne traitent que les téléchargements) : pas de tableau d'écart fiche ↔ PRD ; les écarts sont relevés face à `CLAUDE.md` et à `docs/architecture/flux-cles.md`.
**Périmètre** : les 8 routes `GET /api/media/*` en lecture (`app/api/media.py`), `app/services/media_service.py`, `app/services/aggregation_service.py`, `app/services/unified_group_service.py`, schémas de `app/models/schemas.py`, garde `verify_backend_secret`. Renvoi : `docs/retro-spec/_genere/inventaire.md`. Hors périmètre (cités en dépendance) : `PATCH /api/media/{rk}` et `rescrape` (`CAP-BE-06`), routes trailer (`CAP-BE-12`), `/api/stream` (`CAP-BE-10`), détail du masquage de panne (`CAP-BE-08`), génération de bibliothèque (`CAP-BE-16`).

## Résumé

Le backend expose à l'app TV le catalogue films/séries agrégé de tous les comptes Xtream. Deux familles de routes : les **brutes** (une ligne `media` par entrée : `movies`, `shows`, `episodes`, `{rating_key}`, plus `movies/stats`) et les **unifiées** (un titre = une carte + `versions[]`, dédupliqué par `unification_id` : `movies/unified`, `shows/unified`, `episodes/unified`). La dédup vit dans `aggregation_service` et sert aussi au générateur de bibliothèque. Le parcours non filtré des listes unifiées lit un snapshot précalculé (`media_group`) ; les requêtes filtrées agrègent en mémoire avec un cache de 45 s. Les listes masquent les comptes en panne ; le détail par clé et `movies/stats`, non. Persona : spectateur TV. **La TV ne consomme que les routes brutes, `{rating_key}` et `episodes/unified`** : `movies/unified` et `shows/unified` n'ont aucun appelant TV. Aucun parcours Maestro ne cible spécifiquement le backend.

## User stories observées

- **US-BE-09.1** (P1) — En tant que spectateur TV, je retrouve le catalogue films et séries de tous mes comptes Xtream afin de le parcourir dans l'app.
  - Parcours : synchronisation TV → `GET /api/media/movies` puis `shows` par pages de 500 (`tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:181-186`) · Maestro : `tv:.maestro/legacy/11_browse_tvshows.yaml` (non spécifique au backend)
- **US-BE-09.2** (P1) — En tant que spectateur TV, je vois les épisodes d'une série sans mélange avec une série homonyme d'un autre compte.
  - Parcours : `GET /api/media/episodes?parent_rating_key=&server_id=` (`app/api/media.py:205-263`) · Maestro : `tv:.maestro/legacy/11_browse_tvshows.yaml`
- **US-BE-09.3** (P2) — En tant que spectateur multi-comptes, je vois toutes les versions d'un épisode afin de choisir celle qui marche.
  - Parcours : sélecteur de source → `GET /api/media/episodes/unified?unification_id=` en repli (`tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:417-425`) · Maestro : `tv:.maestro/legacy/13_multi_server_source.yaml`
- **US-BE-09.4** (P2) — En tant que spectateur TV, je relis un titre précis par sa clé afin d'en compléter la fiche.
  - Parcours : `GET /api/media/{rating_key}?server_id=` (`app/api/media.py:506-515`) · Maestro : aucun
- **US-BE-09.5** (P3) — En tant que client tiers ou opérateur, j'obtiens un catalogue déjà dédupliqué (films, séries) sans le reconstruire.
  - Parcours : `GET /api/media/movies/unified`, `shows/unified` (`app/api/media.py:266-391`) · Maestro : aucun — aucun appelant TV
- **US-BE-09.6** (P3) — En tant qu'opérateur, je connais le nombre de films sans `imdb_id` / `tmdb_id`.
  - Parcours : `GET /api/media/movies/stats` (`app/api/media.py:164-169`) · Maestro : sans objet

## Exigences fonctionnelles

### Routes brutes (`movies`, `movies/stats`, `shows`, `episodes`)

- **FR-BE-470** — `GET /api/media/movies` liste les films : `limit` (défaut 500, 1-5000), `offset`, `sort`, filtres `server_id`, `search` et `genre` (sous-chaîne `ILIKE`), `year` (égalité), `missing_imdb` / `missing_tmdb` (OU si les deux), `cursor`. — *implémenté* — preuve `app/api/media.py:128-161`, `app/services/media_service.py:140-192` · test `tests/test_media_serialization_singlepass.py:185-199`
- **FR-BE-471** — `GET /api/media/shows` : mêmes paramètres, sans `missing_imdb` / `missing_tmdb`. — *implémenté* — preuve `app/api/media.py:172-202` · test : aucun test HTTP dédié
- **FR-BE-472** — `GET /api/media/movies/stats` renvoie `{total, missingImdb, missingTmdb}` sur **tous** les films, sans filtre de catégorie ni de panne. — *implémenté* — preuve `app/api/media.py:164-169`, `app/services/media_service.py:627-639` · test : aucun
- **FR-BE-473** — `GET /api/media/episodes` exige `parent_rating_key` et `server_id` ; `server_id` absent ou vide donne 400 avec un message explicite, un `WARNING` (User-Agent tronqué) et l'incrément du compteur `media_episodes_missing_server_id_total`. — *implémenté* — preuve `app/api/media.py:227-249` · test `tests/test_router_http_coverage.py:246-261`, `tests/test_media_episodes_missing_server_id_metric.py:78-132`
- **FR-BE-474** — Un `parent_rating_key` préfixé `series_` filtre sur `grandparent_rating_key`, sinon sur `parent_rating_key`. — *implémenté* — preuve `app/services/media_service.py:193-199` · test `tests/test_router_http_coverage.py:263-281`
- **FR-BE-475** — `episodes` n'expose pas `sort` : l'ordre est toujours `added_desc`. — *implémenté* — preuve `app/api/media.py:258-259`, `app/services/media_service.py:146` · test : aucun
- **FR-BE-476** — Tri : `added_desc` (défaut, et toute valeur inconnue), `added_asc`, `title_asc`, `title_desc`, `rating_desc`, `year_desc` ; les tris de récence départagent par la clé primaire complète pour une pagination stable. — *implémenté* — preuve `app/services/media_service.py:217-233` · test : aucun test par valeur de tri
- **FR-BE-477** — Pagination par curseur (keyset) optionnelle sur les trois routes : avec `sort=added_desc|added_asc`, `cursor` remplace `offset` ; avec un autre tri, `cursor` est ignoré. — *implémenté* — preuve `app/services/media_service.py:235-245` · test `tests/test_media_keyset_pagination.py:88-131`
- **FR-BE-478** — `nextCursor` est émis sur un tri de récence dès que la page est pleine (même sans curseur entrant), sinon `null` ; `hasMore` garde toujours la formule `offset + limit < total`. — *implémenté* — preuve `app/api/media.py:63-79` · test `tests/test_media_keyset_pagination.py:104-110`
- **FR-BE-479** — Un curseur illisible donne 400 « Invalid cursor: … ». — *implémenté* — preuve `app/api/media.py:155-156`, `app/api/media.py:256-257` · test `tests/test_media_keyset_pagination.py:113-119`

### Routes unifiées (`movies/unified`, `shows/unified`, `episodes/unified`)

- **FR-BE-480** — `GET /api/media/movies/unified` renvoie un titre par groupe `{unificationId, title, year, …, versions[], versionCount}` ; `limit` défaut 200 (1-2000), `offset`, filtres `search` / `genre` / `year`. — *implémenté* — preuve `app/api/media.py:266-330` · test `tests/test_media_serialization_singlepass.py:216-226`, `tests/test_plex_dedup.py:398-419`
- **FR-BE-481** — `GET /api/media/shows/unified` : même contrat pour les séries, sans `isAdult` ni préfixe adulte. — *implémenté* — preuve `app/api/media.py:333-391` · test : aucun test HTTP direct (service couvert par `tests/test_unified_by_id.py:131-220`)
- **FR-BE-482** — Avec `unification_id`, `movies/unified` et `shows/unified` ne renvoient que ce groupe (`total=1`) ; inconnu donne 200 `{items: [], total: 0}`, jamais 404. — *implémenté* — preuve `app/api/media.py:284-304`, `app/api/media.py:351-368` · test `tests/test_unified_by_id.py:53-108`
- **FR-BE-483** — La recherche par `unification_id` recharge aussi les jumelles candidates (même `imdb_id` / `tmdb_id`, ou même année) et les fait reconverger, pour ne pas sous-compter les versions d'un titre à identité scindée. — *implémenté* — preuve `app/services/media_service.py:456-573` · test `tests/test_unified_by_id.py:230-313`
- **FR-BE-484** — `GET /api/media/episodes/unified?unification_id=` regroupe les épisodes de toutes les séries membres par `(saison, épisode)`, triés, chaque créneau portant `versions[]` ; aucune série trouvée donne 404 « No show found for that unificationId ». Pas de pagination (`total` = nombre de créneaux). — *implémenté* — preuve `app/api/media.py:394-429`, `app/services/media_service.py:575-610` · test `tests/test_media_serialization_singlepass.py:229-239`
- **FR-BE-485** — Sans filtre, la liste unifiée pagine le snapshot `media_group` / `media_group_member` en SQL (`ORDER BY sort_added_at DESC, group_key`) puis réagrège les seuls membres de la page ; `total` = nombre de groupes du snapshot. — *implémenté* — preuve `app/services/media_service.py:305-314`, `app/services/media_service.py:372-454` · test `tests/test_unified_group_snapshot.py:133-256`
- **FR-BE-486** — Snapshot vide pour le type : repli sur l'agrégation en mémoire, tracé `live_fallback` dans `plexhub_unified_path_total`. — *implémenté* — preuve `app/services/media_service.py:305-314`, `app/services/media_service.py:398-404` · test `tests/test_unified_group_snapshot.py:171-180`, `tests/test_media_unified_path_metric.py:101-117`
- **FR-BE-487** — Toute requête filtrée (`search`, `genre`, `year`) passe par l'agrégation en mémoire, tracée `live_filtered`. — *implémenté* — preuve `app/services/media_service.py:305-314`, `app/services/media_service.py:313-314` · test `tests/test_unified_group_snapshot.py:197-221`, `tests/test_media_unified_path_metric.py:84-100`
- **FR-BE-488** — L'agrégation en mémoire met en cache la liste triée des groupes (TTL 45 s), clé = moteur, type, filtres (`search`, `genre`, `year`), nombre de lignes, `max(updated_at)` et ensemble des comptes masqués ; les pages suivantes du même filtre ne font que découper. — *implémenté* — preuve `app/services/media_service.py:331-370`, `app/services/media_service.py:44-79` · test : aucun test dédié du cache
- **FR-BE-489** — L'agrégation (tri + regroupement) tourne hors de la boucle d'événements (`asyncio.to_thread`), pour la liste, le snapshot et les épisodes. — *implémenté* — preuve `app/services/media_service.py:364`, `app/services/media_service.py:448`, `app/services/media_service.py:608` · test : aucun
- **FR-BE-490** — Le snapshot est reconstruit en fin de pipeline et, avec anti-rebond par type, après une correction d'identité manuelle. — *implémenté* — preuve `app/services/unified_group_service.py:142-293`, `app/services/media_service.py:713-722` · test : couvert côté `CAP-BE-06`
- **FR-BE-491** — Un même fichier (`server_id`, `rating_key`) présent dans deux catégories autorisées donne deux lignes `media` et donc deux entrées dans `versions[]` (même `serverId`/`ratingKey`, libellé suffixé `#2`) ; `versionCount` compte des lignes, pas des sources distinctes. — *implémenté* (comportement constaté) — preuve `app/services/unified_group_service.py:87-96`, `app/services/aggregation_service.py:109-117` · test `tests/test_unified_group_snapshot.py:257-290`
- **FR-BE-492** — Une variante de catégorie non autorisée n'est jamais réintégrée dans `versions[]`, y compris à la réhydratation du snapshot. — *implémenté* — preuve `app/services/media_service.py:426-446` · test `tests/test_unified_group_snapshot.py:293-319`

### Dédup et convergence (`aggregation_service`)

- **FR-BE-493** — Clé de groupe = `unification_id` (`imdb://` > `tmdb://` > `title_<titre>_<année>`), sinon clé par ligne `server_id:rating_key` (jamais de fusion abusive). — *implémenté* — preuve `app/services/aggregation_service.py:20-24`, `app/utils/unification.py:5-28` · test `tests/test_plex_dedup.py:398-419`
- **FR-BE-494** — La ligne représentative (`best_row`) est élue sur : enrichie, a un poster, note, a une année, longueur du titre ; départage `(server_id, rating_key)`. Ni `is_broken` ni la durée n'entrent dans l'élection. — *implémenté* — preuve `app/services/aggregation_service.py:31-43` · test `tests/test_plex_dedup.py:302-355`
- **FR-BE-495** — Passe A : fusion des groupes qui partagent un `imdb_id` ou `tmdb_id` (réparation du partage `imdb://` / `tmdb://`). — *implémenté* — preuve `app/services/aggregation_service.py:203-248` · test `tests/test_plex_convergence.py:62-92`
- **FR-BE-496** — Passe B : absorption d'un groupe `title_…` dans le groupe à identifiant de même titre et même année, gardée contre les titres dégénérés (`title__<année>`), avec choix déterministe entre candidats. — *implémenté* — preuve `app/services/aggregation_service.py:251-297` · test `tests/test_plex_convergence.py:95-148`
- **FR-BE-497** — Les deux passes s'appliquent aux films et aux séries avant la construction des groupes, de la même façon pour l'API et pour le générateur de bibliothèque. — *implémenté* — preuve `app/services/aggregation_service.py:291-338` · test `tests/test_plex_convergence.py:149-163`
- **FR-BE-498** — Le titre affiché est nettoyé (qualificatif VF/HD/VOSTFR et année incrustée retirés) ; l'année vient de la colonne, sinon du titre. — *implémenté* — preuve `app/services/aggregation_service.py:84-92` · test `tests/test_plex_dedup.py:41-53`
- **FR-BE-499** — Libellé de version = qualificatif du titre + libellé du compte (« VF · Compte 1 »), sinon l'un des deux, sinon « v ». — *implémenté* — preuve `app/services/aggregation_service.py:95-106` · test `tests/test_plex_dedup.py:451-475`
- **FR-BE-500** — Les membres sont triés par `(server_id, rating_key)` avant libellé, puis toute collision de libellé reçoit `#n` ; séquence partagée par l'API et le générateur. — *implémenté* — preuve `app/services/aggregation_service.py:120-151`, `app/api/media.py:82-98` · test `tests/test_plex_dedup.py:477-543`
- **FR-BE-501** — Une entrée de `versions[]` porte `serverId`, `ratingKey`, `title`, `label`, `isBroken` ; la catégorie (`filter`) n'y figure pas. — *implémenté* — preuve `app/api/media.py:92-98` · test `tests/test_media_serialization_singlepass.py:216-226`
- **FR-BE-502** — Les épisodes sont rattachés à leur série par `(server_id, grandparent_rating_key)` puis regroupés par `(parent_index, index)` sur toutes les séries du groupe. — *implémenté* — preuve `app/services/aggregation_service.py:309-338` · test `tests/test_plex_convergence.py:150-163`
- **FR-BE-503** — Un épisode sans numéro de saison ou d'épisode est écarté sans trace des créneaux. — *implémenté* — preuve `app/services/aggregation_service.py:329-331` · test : aucun
- **FR-BE-504** — Durée d'un créneau d'épisode = la plus longue durée positive rapportée par ses membres, jamais celle de `best_row`. — *implémenté* — preuve `app/services/aggregation_service.py:52-81`, `app/api/media.py:413-420` · test `tests/test_duration_consolidation.py:28-40`
- **FR-BE-505** — Le plancher ne s'applique que si aucun membre ne rapporte de durée ; une durée courte réellement rapportée est conservée. Le plancher est la durée de la ligne série élue (`group.best.duration`), que l'enrichissement remplace par l'`episode_run_time` TMDB seulement si elle est absente ou inférieure à 5 min. — *implémenté* — preuve `app/api/media.py:418-420`, `app/services/media_identity_writer.py:83-101` · test `tests/test_duration_consolidation.py:53-64`

### Filtrage et masquage

- **FR-BE-506** — Les listes brutes et unifiées ne renvoient que les lignes des catégories autorisées (`is_in_allowed_categories`), sans exception : le levier `include_filtered` a été retiré au Lot 10. — *implémenté* — preuve `app/services/media_service.py:200`, `app/services/media_service.py:317-321` · test `tests/test_unified_group_snapshot.py:293-319`
- **FR-BE-507** — Les leviers `include_filtered=True` et `apply_outage_mask=False` de `get_media_list` n'avaient aucun appelant dans `app/` ; le commentaire qui disait que l'UI admin passe `False` était faux. — *retiré* (Lot 10 : les deux paramètres sont supprimés, le filtre de catégories et le masque de panne sont inconditionnels) — preuve de l'absence `app/services/media_service.py:140-155` (signature), `app/services/media_service.py:200-205` · test : aucun
- **FR-BE-508** — `include_broken=False` de `get_unified_list` n'était jamais passé par une route (le code le disait « API-unreachable »). — *retiré* (Lot 10, voir FR-BE-285 : le filtrer masquerait le seul exemplaire d'un titre en cas de faux positif et divergerait du générateur `.strm`, qui filtre déjà après regroupement ; le comportement « tout servir » est conservé) — preuve de l'absence `app/services/media_service.py:258-268` (signature), `app/services/media_service.py:305-329` · test : aucun
- **FR-BE-509** — Les listes brutes, unifiées (en mémoire et snapshot), la recherche par `unification_id` et `episodes/unified` masquent les comptes en panne confirmée. — *implémenté* — preuve `app/services/media_service.py:127-135`, `app/services/media_service.py:200-205`, `app/services/media_service.py:439-445`, `app/services/media_service.py:482-490`, `app/services/media_service.py:583-590` · test `tests/test_account_outage.py:346-413`
- **FR-BE-510** — `GET /api/media/{rating_key}` et `movies/stats` ne filtrent ni la catégorie ni la panne : un titre masqué reste lisible par sa clé et compté. — *implémenté* (comportement constaté) — preuve `app/services/media_service.py:612-639` · test : aucun
- **FR-BE-511** — Aucune route `/api/media/*` n'exclut les flux cassés : une ligne `is_broken` reste listée, signalée par `isBroken` ; `STREAM_FILTER_BROKEN` ne concerne que la génération. — *implémenté* (comportement constaté) — preuve `app/services/media_service.py:173-205`, `app/plex_generator/source.py:80` · test : aucun sur `/api/media`
- **FR-BE-512** — `search` et `genre` échappent `\`, `%`, `_` avant un `ILIKE`. — *implémenté* — preuve `app/services/media_service.py:177-182`, `app/services/media_service.py:322-327` · test : aucun
- **FR-BE-513** — Le filtre `year` des listes unifiées porte sur la colonne `Media.year`, pas sur l'année affichée (qui peut venir du titre) : un titre sans colonne `year` n'est pas trouvé par ce filtre alors que sa carte affiche une année. — *partiel* (manque : filtrer sur l'année canonique) — preuve `app/services/media_service.py:328-329`, `app/services/aggregation_service.py:84-92` · test : aucun

### Sérialisation

- **FR-BE-514** — Les listes sont sérialisées une seule fois (`JSONResponse` prêt), `response_model` n'est gardé que pour OpenAPI ; `{rating_key}` et `movies/stats` passent par la sérialisation normale. — *implémenté* — preuve `app/api/media.py:41-57`, `app/api/media.py:515` · test `tests/test_media_serialization_singlepass.py:64-123`
- **FR-BE-515** — Réponses en camelCase, champs vides à `null`. — *implémenté* — preuve `app/models/schemas.py:27-31` · test `tests/test_media_serialization_singlepass.py:109-123`
- **FR-BE-516** — Le préfixe `[XXX] ` est ajouté au titre à la sérialisation, sans double préfixe, jamais écrit en base : automatiquement pour `MediaResponse`, à la main dans `movies/unified`. — *implémenté* — preuve `app/models/schemas.py:13-20`, `app/models/schemas.py:115-118`, `app/api/media.py:290-299` · test `tests/test_adult_classification.py:167-174`, `tests/test_adult_classification.py:218-235`
- **FR-BE-517** — Seuls les films peuvent être marqués adultes. — *implémenté* — preuve `app/services/category_service.py:441-522` · test `tests/test_adult_classification.py:109-165`
- **FR-BE-518** — Les cartes unifiées portent les métadonnées NFO de la ligne élue (titre original, slogan, date de sortie, studio, pays, `tvdbId`, `wikidataId`, notes et votes IMDb/TMDB, casting JSON, `youtubeTrailer`). — *implémenté* — preuve `app/api/media.py:105-125` · test : aucun test dédié
- **FR-BE-519** — `tmdbId` des cartes unifiées est une chaîne, `null` si vide. — *implémenté* — preuve `app/api/media.py:101-102` · test : aucun

### Sécurité

- **FR-BE-520** — Toutes les routes sont montées avec `verify_backend_secret` : 401 sans clé valide. — *implémenté* — preuve `app/main.py:660`, `app/main.py:668`, `app/api/deps.py:64-73` · test `tests/test_auth_guard.py:66-86`
- **FR-BE-521** — L'app refuse de démarrer si une route `/api/*` est publiée sans garde connue. — *implémenté* — preuve `app/api/route_audit.py:121-143` · test `tests/test_route_auth_assertion.py:32-61`
- **FR-BE-522** — `GET /api/media/{rating_key}` exige `server_id` (422 sinon) et cherche le couple `(rating_key, server_id)` ; clé inconnue donne 404 « Media not found ». — *implémenté* — preuve `app/api/media.py:506-515`, `app/services/media_service.py:612-625` · test : aucun test HTTP 200/404

### Côté app TV (lecture seule)

- **FR-BE-523** — La TV appelle `movies`, `shows`, `episodes`, `{ratingKey}` et `episodes/unified` ; `movies/unified`, `shows/unified` et `movies/stats` n'ont aucun appelant TV. — *implémenté* (constat) — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:18-63` · test : sans objet
- **FR-BE-524** — La TV synchronise tout le catalogue brut par pages de 500 (`offset += 500` jusqu'à `hasMore=false`), films, séries puis épisodes par série, avec un délai de 30 s par page. — *implémenté* — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:181-186`, `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:268-275` · test `tv:data/src/test/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImplTest.kt:108`
- **FR-BE-525** — La TV pagine uniquement par `offset` : elle n'envoie jamais `cursor` et son DTO de liste ne déclare pas `nextCursor`. — *implémenté* (constat : le curseur backend n'a pas de consommateur TV) — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendDto.kt:12-16`, `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:18-41` · test : aucun
- **FR-BE-526** — `getEpisodes` rend `server_id` obligatoire côté TV (paramètre non nullable). — *implémenté* — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:31-41` · test `tv:data/src/test/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImplTest.kt:199-241`
- **FR-BE-527** — Le sélecteur de source d'épisode lit d'abord la base locale et n'appelle `episodes/unified` qu'en repli, seulement pour un `unificationId` en `imdb://` ou `tmdb://`. — *implémenté* — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:417-512` · test `tv:data/src/test/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImplTest.kt:304-407`
- **FR-BE-528** — `getMovies` envoie `sort=added_desc`, `getShows` n'envoie pas de tri (défaut serveur identique). — *implémenté* — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:18-29` · test : aucun
- **FR-BE-529** — `getMediaDetail` relit une ligne par `(ratingKey, server_id)` ; la TV construit ses cartes dédupliquées depuis sa base locale, pas depuis les routes unifiées. — *implémenté* — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:570-593` · test `tv:data/src/test/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImplTest.kt:657-698`
- **FR-BE-530** — Le DTO TV reprend les champs camelCase de `MediaResponse`, dont `unificationId` et `isBroken`. — *implémenté* — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendDto.kt:18-70` · test : aucun test du mapping cité

## Règles métier

- **RG-BE-09.1** — Priorité d'identité : `imdb://` > `tmdb://` > `title_<titre>_<année>` ; sans identité, la ligne reste seule. — preuve `app/utils/unification.py:5-28`, `app/services/aggregation_service.py:20-24`
- **RG-BE-09.2** — `(rating_key, server_id)` est la seule clé d'une ligne : un `rating_key` n'est unique que par compte, d'où `server_id` obligatoire sur `episodes` et `{rating_key}`. — preuve `app/api/media.py:227-249`, `app/api/media.py:509`
- **RG-BE-09.3** — Une ligne cassée peut être la ligne élue et fournir poster et résumé du groupe. — preuve `app/services/aggregation_service.py:31-43`, `tests/test_plex_dedup.py:302-355`
- **RG-BE-09.4** — Une durée rapportée est toujours respectée (maximum du créneau) ; le plancher ne comble qu'une absence (`CLAUDE.md` §9 piège 22). — preuve `app/services/aggregation_service.py:78-81`
- **RG-BE-09.5** — Le préfixe adulte n'est jamais persisté ; seul le marquage `is_adult` (films) l'est. — preuve `app/models/schemas.py:13-20`, `app/services/category_service.py:441-522`
- **RG-BE-09.6** — L'ensemble des comptes masqués fait partie de la clé de cache (sans lui, une page en cache resservirait un compte tombé en panne). — preuve `app/services/media_service.py:347-354`
- **RG-BE-09.7** — Le snapshot ne connaît pas la panne : le masquage est appliqué à la réhydratation, un groupe entièrement masqué disparaît de la page et `total` sur-compte. — preuve `app/services/media_service.py:434-454`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| `episodes` sans `server_id` ou vide | 400 + `WARNING` + compteur | `app/api/media.py:231-249` |
| `{rating_key}` sans `server_id` | 422 | `app/api/media.py:509` |
| `{rating_key}` inconnu | 404 « Media not found » | `app/api/media.py:513-514` |
| `episodes/unified` sans série pour l'`unification_id` | 404 « No show found for that unificationId » | `app/api/media.py:401-403` |
| `movies/unified?unification_id=` inconnu | 200, liste vide, `total=0` | `app/api/media.py:284-287` |
| Curseur illisible | 400 « Invalid cursor: … » | `app/api/media.py:155-156` |
| `cursor` avec `sort=title_asc` | ignoré, pagination par `offset`, `nextCursor=null` | `app/services/media_service.py:237-245`, `app/api/media.py:77` |
| Valeur de `sort` inconnue | tri `added_desc`, mais `nextCursor=null` | `app/services/media_service.py:232-233`, `app/api/media.py:77` |
| Snapshot vide pour le type | agrégation en mémoire (`live_fallback`) | `app/services/media_service.py:403-404` |
| `offset` au-delà du snapshot | page vide, `total` inchangé | `app/services/media_service.py:412-413` |
| Snapshot périmé (membres supprimés) | page vide ou plus courte que prévu, `total` sur-compte | `app/services/media_service.py:423-424`, `app/services/media_service.py:450-454` |
| Compte en panne masquée | absent des listes ; lisible par `{rating_key}`, compté par `movies/stats` | `app/services/media_service.py:200-205`, `app/services/media_service.py:612-639` |
| Épisode sans saison ou numéro | absent de `episodes/unified` | `app/services/aggregation_service.py:329-331` |
| Aucun membre d'un créneau ne donne de durée | durée de la ligne série élue, sinon `null` | `app/services/aggregation_service.py:78-81`, `app/api/media.py:418-420` |
| Sans `X-API-Key` ou clé invalide | 401 | `app/api/deps.py:64-73` |

## Dépendances

- Autres capacités : `CAP-BE-05` (synchronisation, alimente `media`), `CAP-BE-06` (enrichissement : identités, `episode_run_time`, reconstruction du snapshot après correction manuelle), `CAP-BE-07` (validation, source d'`is_broken`), `CAP-BE-08` (masquage de panne consommé ici), `CAP-BE-04` (catégories autorisées, libellés de compte), `CAP-BE-10` (consomme `rating_key` / `server_id`), `CAP-BE-12` (`youtubeTrailer` exposé ici), `CAP-BE-16` (second consommateur d'`aggregation_service`), `CAP-BE-02` (garde d'authentification), `CAP-BE-20` (le catalogue unifié de téléchargement réutilise `get_unified_list` / `get_unified_group`).
- Autre dépôt : appels `tv:` de FR-BE-523 à FR-BE-530.
- Données : `media` (lecture), `media_group` / `media_group_member` (lecture ; écrites par `unified_group_service`), `xtream_accounts` (libellés, état de panne). Schéma : `docs/retro-spec/technique/db/`.

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| docs/architecture/flux-cles.md §5.4 (a) décrit `get_unified_list` comme chargeant tout le catalogue à chaque appel, pagination sans effet, avec la référence media_service.py:143-147 (qui pointe aujourd'hui `get_media_list`) ; le snapshot `media_group`, le cache et le passage hors boucle ne sont pas mentionnés | doc périmée | réécrire §5.4 (a) : snapshot, repli, cache, `to_thread` |
| Même paragraphe : le variant `?unification_id=` (référence media_service.py:149) « sous-rapporte les jumelles » (CR-F05) ; le code recharge désormais les candidates et le teste (FR-BE-483) | doc périmée | marquer CR-F05 comme corrigé |
| Même paragraphe : présente les routes unifiées comme destinées à « l'app PlexHubTV » ; la TV n'appelle que `episodes/unified` (FR-BE-523) | doc ↔ code client | préciser les consommateurs réels |
| Commentaire « The admin UI passes False » — aucun appelant ne passait `apply_outage_mask` ; idem `include_filtered` (FR-BE-507) et `include_broken=False` (FR-BE-508) | code mort (résolu au Lot 10 : paramètres et commentaire retirés) | aucune |
| `{rating_key}` et `movies/stats` ignorent panne et catégories (FR-BE-510), alors que `CLAUDE.md` §9 piège 20 (a) demande que toute lecture app passe par `_outage_clause`, sans exception listée | spec ↔ code | lister ces exceptions dans le piège 20 ou les aligner : à trancher par Chakir (déjà signalé pour le détail dans `CAP-BE-08`) |
| `versions[]` peut contenir deux fois le même fichier (deux catégories autorisées), indiscernables côté client (FR-BE-491, FR-BE-501) | contrat ambigu | dédupliquer par `(serverId, ratingKey)` ou exposer la catégorie : à trancher par Chakir |
| Filtre `year` des listes unifiées sur la colonne brute, pas sur l'année affichée (FR-BE-513) | incohérence filtre ↔ affichage | à trancher par Chakir |
| Le bandeau de `CLAUDE.md` (v1.10.8) parle d'un « plancher TMDB `episode_run_time` » ; le plancher réel est la durée de la ligne série élue, qui peut être une valeur du panel ≥ 5 min (FR-BE-505) | doc imprécise | préciser la formulation |
| Aucun test HTTP de `{rating_key}` (200/404), `movies/stats`, `shows` seul, `shows/unified` ; aucun test de l'échappement `ILIKE`, des tris, du cache 45 s, de l'épisode sans numéro | test manquant | ajouter des cas paramétrés |
| Le curseur keyset (CR-P04) n'a aucun consommateur TV (FR-BE-525) | fonction sans client | le garder pour un futur client ou le documenter comme optionnel |

## Questions ouvertes

- `episodes/unified` ne filtre les épisodes ni par catégorie autorisée ni par panne : seul le filtre sur les séries membres s'applique (`app/services/media_service.py:596-602`). Un épisode d'une catégorie non autorisée peut-il apparaître dans un créneau ? À vérifier par test.
- Quand une page du snapshot contient un groupe entièrement masqué, la page est plus courte que `limit` et `hasMore` reste vrai : un client qui s'arrête sur une page courte perd-il des titres ? À vérifier par test.
- Le cache de 45 s (clé = `max(updated_at)`) laisse voir une ancienne valeur d'`is_broken` ou d'enrichissement pendant une fenêtre ; les écritures qui ne touchent pas `updated_at` sont-elles fréquentes en pratique ? À vérifier par test.
- La TV traite-t-elle deux entrées `versions[]` de même clé comme deux sources distinctes dans le sélecteur ? À vérifier par test côté TV.
- `movies/stats` sans filtre de catégorie ni de panne : indicateur brut voulu pour l'opérateur, ou oubli ? Le code ne tranche pas.
- `movies/unified` et `shows/unified` ont-ils un consommateur externe (scripts, autre client) qui justifie de les garder au contrat ? Le code ne tranche pas.
