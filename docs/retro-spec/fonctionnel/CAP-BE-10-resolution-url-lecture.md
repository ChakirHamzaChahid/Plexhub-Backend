# CAP-BE-10 — Résolution d'URL de lecture

**Reconstituée le** : 2026-09-25 · **HEAD** : `bfcc522` · **Sources** : code, tests, `CLAUDE.md` (§9 piège 17c, §9 piège 20), `docs/architecture/flux-cles.md` (§5.13), `docs/architecture/ARCHITECTURE.md`, `docs/PLEXHUB_BACKEND_INTEGRATION_GUIDE.md`, app TV (lecture seule, preuves `tv:`). Aucun PRD ne couvre cette capacité (`docs/10-prd-*.md` ne traitent que les téléchargements) : pas de tableau d'écart fiche ↔ PRD ; les écarts sont face à `CLAUDE.md`, aux documents d'architecture et au guide d'intégration.
**Périmètre** : `GET /api/stream/{rating_key}?server_id=…` (`app/api/stream.py`), `app/services/stream_service.py` (`parse_rating_key`, `build_stream_url`), les constructeurs d'URL de `app/services/xtream_service.py`, le schéma `StreamResponse`, et la variante live `GET /api/live/channels/{stream_id}/stream` (`app/api/live.py`, résolution d'URL seulement : le reste du live relève de `CAP-BE-11`). Renvoi : `docs/retro-spec/_genere/inventaire.md`. Les tables `xtream_accounts` et `live_channels` sont décrites dans `docs/retro-spec/technique/db/`.

## Résumé

Pour lancer une lecture, l'app TV demande au backend l'URL directe d'un titre du fournisseur Xtream. Le backend ne lit pas le catalogue : il retrouve le compte à partir de `server_id`, décode le `rating_key` (film, épisode, chaîne live) et assemble `{base}/{movie|series|live}/{user}/{pass}/{id}.{ext}` avec les identifiants du compte. La réponse contient donc **l'identifiant et le mot de passe Xtream en clair**, sans signature ni expiration. L'endpoint ne regarde ni l'état du compte, ni le masquage de panne, ni l'état du média : un titre supprimé ou un fournisseur mort obtient quand même une URL, et l'échec n'apparaît qu'à la lecture. Persona : spectateur TV (via l'app). **Aucun parcours Maestro ne cible cette capacité** : `tv:.maestro/06_play_movie.yaml` lance une lecture sans être propre au backend.

## User stories observées

- **US-BE-10.1** (P1) — En tant que spectateur TV, je lance un film ou un épisode d'un fournisseur Xtream afin de le regarder sans que l'app connaisse les identifiants du fournisseur.
  - Parcours : détail média → lecteur → `GET /api/stream/{rating_key}?server_id=xtream_<compte>` (`app/api/stream.py:13`) · Maestro : aucun
- **US-BE-10.2** (P1) — En tant que spectateur TV, je lance une chaîne live afin de la regarder.
  - Parcours : liste des chaînes → `GET /api/live/channels/{stream_id}/stream` (`app/api/live.py:105`) · Maestro : aucun
- **US-BE-10.3** (P2) — En tant que spectateur TV qui choisit une version précise (compte différent) d'un même titre, je lis la version choisie afin de ne pas tomber sur un autre fournisseur.
  - Parcours : sélecteur de version → `server_id` du compte de la version (`tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:404-408`) · Maestro : aucun
- **US-BE-10.4** (P2) — En tant qu'opérateur, je m'appuie sur le même constructeur d'URL pour la bibliothèque générée, le relais DAV, la validation de flux et les téléchargements, afin qu'une seule règle définisse ce qu'est l'URL d'un titre.
  - Parcours : voir Dépendances · Maestro : sans objet

## Exigences fonctionnelles

### Endpoint VOD / épisode (`/api/stream`)

- **FR-BE-393** — Le système expose `GET /api/stream/{rating_key}?server_id=…` et répond `{url, expiresAt}` (alias camelCase). — *implémenté* — preuve `app/api/stream.py:13-36`, `app/models/schemas.py:273-277` · test `tests/test_router_http_coverage.py:93-110`
- **FR-BE-394** — Le paramètre `server_id` est obligatoire : absent, FastAPI répond 422. — *implémenté* — preuve `app/api/stream.py:16` · test : aucun
- **FR-BE-395** — Un `server_id` qui ne commence pas par `xtream_` (y compris vide ou `plex_…`) est rejeté en 400 « Invalid server_id format » ; le reste après `xtream_` devient l'identifiant de compte. — *implémenté* — preuve `app/api/stream.py:20-23` · test `tests/test_router_http_coverage.py:112-119`
- **FR-BE-396** — Un identifiant de compte inconnu (y compris `xtream_` seul, identifiant vide) donne 404 « Account not found ». — *implémenté* — preuve `app/api/stream.py:25-30` · test `tests/test_router_http_coverage.py:121-127`
- **FR-BE-397** — L'URL est construite à partir du seul compte et du `rating_key` : la table `media` n'est pas lue. Un identifiant absent du catalogue obtient une réponse 200 avec une URL. — *implémenté* (comportement constaté) — preuve `app/api/stream.py:25-36` (seule la table `XtreamAccount` est interrogée) · test : aucun
- **FR-BE-398** — L'endpoint ne consulte ni `is_active`, ni `is_broken`, ni le masquage de panne fournisseur : un compte désactivé ou en panne reçoit une URL normale. C'est voulu pour le masquage (« non masqué volontairement ») ; le relais DAV, lui, filtre `is_active`. — *implémenté* (comportement constaté) — preuve `app/api/stream.py:25-36`, `app/api/dav.py:140-146`, `docs/architecture/flux-cles.md:46` · test : aucun
- **FR-BE-399** — Un `rating_key` dont le type n'est pas constructible (`series_…`, `season_…`, préfixe inconnu) donne 400 « Cannot build stream URL for: {rating_key} » et un `WARNING` qui nomme le type, pas l'URL. — *implémenté* — preuve `app/services/stream_service.py:36-41`, `app/services/stream_service.py:69-71`, `app/api/stream.py:32-34` · test `tests/test_router_http_coverage.py:129-138` (clé inconnue seulement ; aucun test `series_` / `season_`)
- **FR-BE-400** — Le décodage du `rating_key` se fait par préfixe : `vod_`→film, `ep_`→épisode, `live_`→live, `series_`→série, `season_`→saison, sinon inconnu ; l'extension est ce qui suit le dernier point. — *implémenté* — preuve `app/services/stream_service.py:9-41` · test : aucun test direct de `parse_rating_key` (couvert seulement par les tests HTTP de FR-BE-393)
- **FR-BE-401** — Sans extension (ou extension vide), l'URL prend `ts`. — *implémenté* — preuve `app/services/stream_service.py:49`, `app/services/stream_service.py:56`, `app/services/stream_service.py:63` · test : aucun
- **FR-BE-402** — Les URLs suivent `{base}movie/{user}/{pass}/{id}.{ext}` (film), `{base}series/…` (épisode) et `{base}live/…` (live). — *implémenté* — preuve `app/services/xtream_service.py:182-194`, `app/services/xtream_service.py:258-263` · test `tests/test_router_http_coverage.py:101`, `tests/test_router_http_coverage.py:110` (le `/live/` de `/api/stream` n'est pas testé)
- **FR-BE-403** — Le port est omis pour `http`+80 et `https`+443, sinon ajouté (`https` + 80 donne `https://hôte:80/`) ; le schéma est celui de `base_url`, un `/` final est retiré. — *implémenté* — preuve `app/services/xtream_service.py:38-44` · test : aucun test dédié
- **FR-BE-404** — Un `rating_key` de film ou de live à identifiant non numérique (`vod_abc.mp4`, `vod_`, `vod_12.5.mp4`, `live_abc`) devrait donner un 400 comme les autres clés non constructibles. Le code fait `int(parsed["id"])` sans garde : l'exception `ValueError` sort de l'endpoint et aucun gestionnaire d'exception global n'existe (grep `exception_handler` dans `app/` : aucune occurrence), d'où un 500 attendu. — *partiel* (manque : conversion protégée ; le 500 est déduit de la lecture, non observé) — preuve `app/services/stream_service.py:53`, `app/services/stream_service.py:67`, `app/api/stream.py:32` · test : aucun
- **FR-BE-405** — Pour un épisode, l'identifiant reste une chaîne non validée : `ep_abc.mkv` donne une URL `…/series/u/p/abc.mkv` en 200. — *implémenté* (comportement constaté) — preuve `app/services/stream_service.py:55-61` · test : aucun
- **FR-BE-406** — Identifiant et mot de passe sont insérés tels quels dans le chemin, sans encodage : un caractère réservé (`@`, `/`, `:`, `?`, `#`) produit une URL invalide ou ambiguë. — *implémenté* (comportement constaté) — preuve `app/services/xtream_service.py:187`, `app/services/xtream_service.py:194`, `app/services/xtream_service.py:263` · test : aucun

### Sécurité et traçabilité

- **FR-BE-407** — Les deux routeurs (`stream`, `live`) sont montés avec `verify_backend_secret` : sans clé valide, 401, en fail-closed ; clé maître ou clé par utilisateur active, comparaison en temps constant pour la clé maître. — *implémenté* — preuve `app/main.py:633`, `app/main.py:640`, `app/main.py:642`, `app/api/deps.py:47-73` · test `tests/test_auth_guard.py:66-86` (le chemin `/api/stream/no-such-rating-key` figure dans la liste des chemins gardés, `tests/test_auth_guard.py:44`)
- **FR-BE-408** — Au démarrage, une assertion refuse de lancer l'app si une route `/api/*` est publiée sans garde connue : `/api/stream` ne peut pas devenir publique par oubli. — *implémenté* — preuve `app/main.py:727-728`, `app/api/route_audit.py:103-125` · test `tests/test_route_auth_assertion.py:1`
- **FR-BE-409** — L'URL n'est jamais journalisée par ces endpoints : le seul log est le `WARNING` de FR-BE-399 (type de clé). — *implémenté* — preuve `app/services/stream_service.py:70`, `app/api/stream.py:13-36` (aucun appel de log), `app/api/live.py:105-140` (idem) · test : aucun
- **FR-BE-410** — Le contrat prévoit une échéance `expiresAt`, mais aucun endpoint ne la renseigne (toujours `null`) et la TV ne la lit pas : l'URL est valable tant que le compte l'est. — *mort* (champ déclaré, jamais alimenté ni lu) — preuve `app/models/schemas.py:277`, `app/api/stream.py:36`, `app/api/live.py:140`, `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendDto.kt:90-93` · test : aucun

### Variante live

- **FR-BE-411** — `GET /api/live/channels/{stream_id}/stream?server_id=…` : `stream_id` entier (sinon 422), `server_id` décodé par `parse_server_id` (400 si invalide), 404 si le compte est inconnu, 404 si la chaîne n'existe pas pour ce `server_id`. — *implémenté* — preuve `app/api/live.py:105-132`, `app/utils/server_id.py:6-10` · test `tests/test_router_http_coverage.py:332-359`
- **FR-BE-412** — L'extension live est `container_extension` de la chaîne lue en base, sinon `ts` : contrairement au VOD, le live consulte donc la table `live_channels`. — *implémenté* — preuve `app/api/live.py:124-139` · test `tests/test_router_http_coverage.py:338` (cas `ts` uniquement)
- **FR-BE-413** — Un `rating_key` `live_<id>` passé à `/api/stream` est accepté et donne `…/live/u/p/<id>.ts`, sans consulter `container_extension`. La TV n'utilise pas ce chemin. — *implémenté* (comportement constaté, peu utilisé) — preuve `app/services/stream_service.py:62-68`, `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:135-139` · test : aucun

### Constructeur partagé

- **FR-BE-414** — `build_stream_url` est la règle unique de construction d'URL pour la bibliothèque générée, le relais DAV, la validation de flux et le worker de téléchargement Xtream (l'arbre DAV et le service de téléchargement n'utilisent que `parse_rating_key`). — *implémenté* — preuve `app/plex_generator/source.py:85`, `app/api/dav.py:170`, `app/workers/health_check_worker.py:190`, `app/workers/download_worker.py:600`, `app/dav/tree_builder.py:87`, `app/services/download_service.py:596` · test `tests/test_plex_download_worker.py:181-205` (branche Xtream du worker)

### Côté app TV (lecture seule)

- **FR-BE-415** — La TV appelle `api/stream/{ratingKey}` avec `server_id` en query et lit `BackendStreamResponse(url, expiresAt)`. — *implémenté* — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:49-53`, `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendDto.kt:90-93` · test `tv:data/src/test/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImplTest.kt:147-166`
- **FR-BE-416** — Le `server_id` envoyé est le compte source, résolu dans l'ordre : version choisie, compte encodé dans le `rating_key` préfixé, lecture de la base locale ; sans résultat, l'appel n'est pas fait. Le `rating_key` est dépréfixé avant l'appel. — *implémenté* — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:404-411` · test `tv:data/src/test/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImplTest.kt:168-185`
- **FR-BE-417** — Pour un titre non Plex, la TV essaie d'abord la construction locale d'URL (`XtreamUrlBuilder`), puis le backend (`BackendUrlBuilder`) ; il n'existe aucun repli si le backend échoue. — *implémenté* — preuve `tv:app/src/main/java/com/chakir/plexhubtv/feature/player/url/DirectStreamUrlBuilder.kt:32-38`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/player/url/BackendUrlBuilder.kt:17-23` · test : aucun test de `BackendUrlBuilder`
- **FR-BE-418** — Toute erreur de résolution (400, 404, 401, 5xx, réseau) se réduit à `null` puis à un message générique (« Failed to get stream URL » en VOD, « Failed to resolve stream » en live) ; le 400 n'est pas distingué (`AppError.Unknown`) et les 5xx / 429 sont rejoués jusqu'à 3 fois. — *partiel* (manque : message propre à « titre non lisible ») — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/util/SafeApiCall.kt:134-140`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/player/PlayerControlViewModel.kt:659-661`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/livetv/LiveTvViewModel.kt:287-288` · test : aucun
- **FR-BE-419** — La TV résout les chaînes live par `getLiveStream` depuis deux écrans (télévision en direct, IPTV) ; aucune résolution n'est mise en cache, chaque lecture redemande l'URL. — *implémenté* — preuve `tv:app/src/main/java/com/chakir/plexhubtv/feature/livetv/LiveTvViewModel.kt:260`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/iptv/IptvViewModel.kt:631` · test : aucun test de `getLiveStream` côté TV
- **FR-BE-420** — En build de debug, la TV journalise l'URL résolue entière (identifiants compris) ; le build release n'accepte que les niveaux INFO et au-dessus. — *implémenté* (comportement constaté) — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/BackendRepositoryImpl.kt:412`, `tv:app/src/main/java/com/chakir/plexhubtv/PlexHubApplication.kt:161-167` · test : aucun

## Règles métier

- **RG-BE-10.1** — `server_id` = `xtream_<id_compte>` où l'identifiant de compte est `MD5(baseUrl+username)[:8]`. — preuve `app/api/stream.py:23`, `app/models/database.py:148`
- **RG-BE-10.2** — Le mot de passe est chiffré au repos et déchiffré à la lecture par l'ORM ; la réponse le contient en clair. Le secret sort donc du backend vers tout client détenteur d'une clé valide, y compris une clé par utilisateur (non cloisonnée par périmètre). — preuve `app/models/database.py:153`, `app/api/stream.py:32-36`
- **RG-BE-10.3** — `(rating_key, server_id)` est la seule clé identifiante : le `rating_key` n'est unique que dans le contexte d'un compte (le guide d'intégration le rappelle). — preuve `docs/PLEXHUB_BACKEND_INTEGRATION_GUIDE.md:1377`
- **RG-BE-10.4** — Valider qu'un titre est lisible n'est pas le rôle de cet endpoint : cela relève de la validation de flux (`CAP-BE-07`) et du masquage de panne (`CAP-BE-08`), qui filtrent les listes du catalogue, pas la résolution d'URL. — preuve `docs/architecture/flux-cles.md:46`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| `server_id` absent | 422 | `app/api/stream.py:16` |
| `server_id` vide, `plex_…` ou sans préfixe `xtream_` | 400 « Invalid server_id format » | `app/api/stream.py:20-21` |
| `server_id` = `xtream_` seul | 404 « Account not found » | `app/api/stream.py:23-30` |
| Compte `is_active=False` ou en panne masquée | 200 avec URL | `app/api/stream.py:25-36` |
| Titre absent du catalogue ou marqué cassé | 200 avec URL ; l'échec n'apparaît qu'à la lecture | `app/api/stream.py:25-36` |
| `vod_435071.mp4` | `…/movie/u/p/435071.mp4` | `tests/test_router_http_coverage.py:101` |
| `vod_435071` ou `vod_435071.` | extension `ts` | `app/services/stream_service.py:49` |
| `vod_abc.mp4`, `vod_`, `vod_12.5.mp4`, `live_abc` | `ValueError` non rattrapée ; 500 attendu (déduit) | `app/services/stream_service.py:53`, `app/services/stream_service.py:67` |
| `vod_-5.mp4` | accepté : `…/movie/u/p/-5.mp4` | `app/services/stream_service.py:53` |
| `ep_abc.mkv` | accepté : `…/series/u/p/abc.mkv` | `app/services/stream_service.py:57-61` |
| `series_6581`, `season_3`, clé quelconque | 400 « Cannot build stream URL for: … » | `app/services/stream_service.py:69-71`, `app/api/stream.py:34` |
| `rating_key` contenant `/` | non capturé par le paramètre de chemin (non testé) | déduit du routage FastAPI, à vérifier par test |
| Sans `X-API-Key` ou clé fausse | 401 | `app/api/deps.py:64-73`, `tests/test_auth_guard.py:66-77` |
| Chaîne live inconnue pour ce `server_id` | 404 « Channel not found » | `app/api/live.py:131-132` |

## Dépendances

- Autres capacités : `CAP-BE-04` (comptes et identifiants Xtream), `CAP-BE-09` (catalogue qui fournit les `rating_key` et `server_id`), `CAP-BE-07` (validation de flux, qui utilise `build_stream_url` pour sonder), `CAP-BE-08` (masquage de panne : ne s'applique pas ici), `CAP-BE-11` (chaînes live et EPG), `CAP-BE-16` (`.strm` générés), `CAP-BE-17` (relais DAV), `CAP-BE-18` (téléchargement, même constructeur), `CAP-BE-02` (garde d'authentification).
- Consommateurs de `build_stream_url` / `parse_rating_key` : `app/plex_generator/source.py:85`, `app/api/dav.py:170`, `app/workers/health_check_worker.py:190`, `app/workers/download_worker.py:600`, `app/dav/tree_builder.py:87`, `app/services/download_service.py:596`. Le live appelle `xtream_service.build_live_url` directement (`app/api/live.py:135`) ; la synchronisation Xtream n'utilise pas ce constructeur.
- Autre dépôt : appels `tv:` de FR-BE-415 à FR-BE-420.
- Données : `xtream_accounts` (lecture, y compris le mot de passe chiffré), `live_channels` (live seulement).

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| Identifiant non numérique (`vod_abc`, `live_abc`) : `ValueError` non rattrapée, 500 attendu au lieu de 400 (FR-BE-404) | code ↔ contrat du guide (400 pour clé non constructible) | test de confirmation puis garde `try/except` : à trancher par Chakir |
| `/api/stream` ignore `is_active` et le masquage de panne, alors que `/dav` filtre `is_active` (FR-BE-398) | comportement asymétrique entre surfaces | à trancher par Chakir : le choix « non masqué » est documenté, l'absence de contrôle `is_active` ne l'est pas |
| `CLAUDE.md` §9 piège 17c pose que l'URL Xtream « ne doit JAMAIS être renvoyée en API/HTML » dans le contexte des téléchargements, alors que `/api/stream` la renvoie volontairement avec identifiants | spec ↔ code (portée de la règle ambiguë) | préciser dans `CLAUDE.md` que la règle vise les téléchargements ; à trancher par Chakir |
| `expiresAt` déclaré au contrat, jamais renseigné ni lu (FR-BE-410) | code mort | supprimer le champ ou introduire une URL signée : à trancher par Chakir |
| Identifiants non encodés dans l'URL (FR-BE-406) | robustesse | encoder avec `urllib.parse.quote` si un identifiant réel contient un caractère réservé : à trancher par Chakir |
| Parsing de `server_id` recopié dans `app/api/stream.py:20-23` au lieu d'appeler `parse_server_id` (déjà utilisé par `app/api/live.py:112`) | duplication | remplacer par `parse_server_id` (signalé dans `docs/audit/cleanroom-2026-07-11/10-architecture.md:25`) |
| `docs/PLEXHUB_BACKEND_INTEGRATION_GUIDE.md:672` et `docs/PLEXHUB_BACKEND_INTEGRATION_GUIDE.md:897` montrent `http://example.com:80/…`, alors que le code omet `:80` (FR-BE-403) | doc périmée | corriger les exemples |
| `docs/architecture/architecture-2026-05-14.md:153` et `docs/architecture/architecture-2026-05-14.md:163` (idem `docs/architecture/architecture-2026-05-13.md:151`) indiquent l'authentification « aucune » pour `/api/live` et `/api/stream` ; `docs/architecture/ARCHITECTURE.md:156` dit correctement `verify_backend_secret` | doc périmée | marquer les deux notes datées comme historiques |
| Le commentaire d'en-tête de tests/test_auth_guard.py cite app/main.py:396-405 et deps.py:59 ; le montage est à `app/main.py:633-644` et la garde à `app/api/deps.py:64` | référence périmée | mettre à jour le commentaire |
| Aucun test unitaire de `parse_rating_key` / `build_stream_url` ; non couverts : `series_`/`season_`, extension absente, id non numérique, port non défaut, `live_` via `/api/stream`, compte inactif | test manquant | ajouter une table de cas paramétrée |
| La TV journalise l'URL entière en debug et, dans `tv:app/src/main/java/com/chakir/plexhubtv/feature/player/PlayerControlViewModel.kt:216`, une URL rejetée en `WARNING` (`Timber.w`) ; les 80 premiers caractères d'une URL Xtream contiennent souvent les identifiants (`tv:app/src/main/java/com/chakir/plexhubtv/feature/player/controller/PlayerController.kt:527`) | fuite de secret possible dans les logs TV | vérifier ce que le build release envoie à Crashlytics ; à trancher côté TV |
| La TV ne distingue pas le 400 (titre non lisible) des autres échecs (FR-BE-418) | comportement client | message dédié : côté TV |

## Questions ouvertes

- `int("abc")` produit-il bien un 500 sur `/api/stream` faute de gestionnaire global ? À vérifier par test avec un client qui ne relève pas les exceptions serveur.
- Un `%2F` dans `rating_key` est-il transmis, rejeté ou réécrit par le reverse-proxy amont ? À vérifier par test.
- L'exposition du mot de passe à toute clé par utilisateur est-elle un risque accepté, ou une URL signée à durée limitée est-elle envisagée puisque `expiresAt` existe déjà au contrat ? Le code ne tranche pas (audit sécurité : `docs/audit/v1/20-security.md:97`).
- Faut-il que `/api/stream` refuse un compte `is_active=False` comme le fait `/dav` ? Le code ne dit pas si l'asymétrie est voulue : à vérifier par test de comportement attendu, puis décision de Chakir.
- Que devient une lecture sur un compte en panne masquée mais toujours résolue par `/api/stream` (la TV voit ses listes vidées, mais une version déjà ouverte reste lisible) ? À vérifier par test de bout en bout.
