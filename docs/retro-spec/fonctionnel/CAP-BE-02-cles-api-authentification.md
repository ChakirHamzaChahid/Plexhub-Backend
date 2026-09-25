# CAP-BE-02 — Clés API & authentification

**Reconstituée le** : 2026-09-25 · **HEAD** : `7a9d81b` · **Sources** : inventaire, code, tests, app TV (lecture seule, preuves `tv:`). Aucun PRD ne couvre cette capacité (`docs/10-prd-*.md` ne traitent que les téléchargements) : pas de tableau d'écart fiche ↔ PRD ; les écarts sont relevés face à `CLAUDE.md` §2, §3 et §9 (pièges 2, 10, 12, 19).
**Périmètre** : les gardes de `app/api/deps.py` (`verify_backend_secret`, `verify_api_key`, `verify_master_key`, `verify_admin_basic_auth`, `verify_dav_basic_auth`, `verify_metrics_basic_auth`), la gestion des clés par utilisateur (`app/services/api_key_service.py`, `app/api/api_keys.py` → `/api/admin/keys*`, pages `/admin/keys*` de `app/api/admin.py`), la table `api_keys`, l'assertion de boot (`app/api/route_audit.py`), le filtre CSRF de l'admin (`app/api/csrf.py`) et les montages de `app/main.py`. Renvoi : `docs/retro-spec/_genere/inventaire.md` (marque `/api/admin/keys*` `public ?` : faux positif de l'outil, la garde est posée au niveau du router, `app/api/api_keys.py:20-24`). Les gardes des capacités voisines (`/api/tv-auth/approve`, `/dav`, `/metrics`) ne sont décrites ici que pour leur mécanisme d'authentification.

## Résumé

Tout `/api/*` exige l'en-tête `X-API-Key`, sauf `/api/health` et trois endpoints d'appairage TV. La clé acceptée est soit le secret maître (variable d'environnement, comparé en temps constant), soit une clé par utilisateur émise par l'opérateur : un jeton `phk_…` montré une seule fois, dont seul le condensat SHA-256 est stocké, révocable immédiatement et éventuellement expirant. Seul le secret maître gère les clés et lit les miroirs JSON d'administration. L'UI HTML `/admin`, `/docs` et `/openapi.json` sont derrière une Basic Auth dédiée ; `/dav` et `/metrics` ont chacun leur propre Basic Auth. L'application refuse de démarrer si une route `/api/*` est publiée sans garde. Côté TV, un intercepteur OkHttp attache la clé aux seuls hôtes backend connus. Personas : opérateur, app TV (cliente), mainteneur.

## User stories observées

- **US-BE-02.1** (P1) — En tant qu'opérateur, je veux que toute requête `/api/*` sans clé valide soit refusée afin que le catalogue et les flux ne soient pas exposés sur le tunnel public.
  - Parcours : `_guard` au montage + gardes au niveau des routers (`app/main.py:633-644`, `app/main.py:708-712`) · Maestro : aucun
- **US-BE-02.2** (P1) — En tant qu'opérateur, je crée une clé par appareil ou par personne, je la copie une seule fois, et je la révoque sans toucher aux autres.
  - Parcours : `/admin/keys` → formulaire → encart « clé créée » → tableau → « Révoquer » (`app/api/admin.py:902-955`) ou `/api/admin/keys*` (`app/api/api_keys.py:68-105`) · Maestro : aucun
- **US-BE-02.3** (P1) — En tant qu'opérateur, je veux qu'une clé utilisateur ne puisse ni créer ni révoquer d'autres clés.
  - Parcours : `verify_master_key` (`app/api/deps.py:93-107`) · Maestro : aucun
- **US-BE-02.4** (P2) — En tant qu'opérateur, j'administre le backend depuis un navigateur sans y saisir `X-API-Key`, et Prometheus/rclone ont chacun leurs identifiants.
  - Parcours : Basic Auth `/admin`, `/docs`, `/dav`, `/metrics` (`app/api/deps.py:113-234`) · Maestro : aucun
- **US-BE-02.5** (P2) — En tant que mainteneur, je veux qu'un oubli de garde sur une nouvelle route `/api/*` casse le démarrage plutôt que d'ouvrir la route en silence.
  - Parcours : `assert_api_routes_authenticated` (`app/main.py:727-728`) · Maestro : aucun
- **US-BE-02.6** (P1) — En tant qu'app TV, j'attache automatiquement ma clé aux requêtes du backend, et jamais à un serveur tiers.
  - Parcours : `BackendApiKeyInterceptor` (`tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiKeyInterceptor.kt:44-60`) · Maestro : aucun

## Exigences fonctionnelles

### Garde de l'API JSON (`verify_backend_secret`)

- **FR-BE-101** — Le système exige `X-API-Key` sur les routers `accounts`, `categories`, `live`, `media`, `stream`, `sync` et `plex`, par une dépendance posée au montage. — *implémenté* — preuve `app/main.py:633`, `app/main.py:638-644`, `app/api/deps.py:64-73` · test `tests/test_auth_guard.py:66-113`
- **FR-BE-102** — Une requête est authentifiée si la clé est le secret maître (comparaison `secrets.compare_digest`) **ou** une clé utilisateur active ; le secret maître ne coûte aucune lecture en base. — *implémenté* — preuve `app/api/deps.py:47-61` · test `tests/test_ai_deps.py:99`, `tests/test_router_http_coverage.py:494-498`
- **FR-BE-103** — En-tête absent, clé fausse, ou secret maître vide sans clé utilisateur correspondante : 401 « Invalid API key » (jamais 503). — *implémenté* — preuve `app/api/deps.py:54-73` · test `tests/test_auth_guard.py:66-77`, `tests/test_ai_deps.py:34`
- **FR-BE-104** — `GET /api/health` reste public (aucune dépendance d'authentification). — *implémenté* — preuve `app/main.py:635`, `app/api/route_audit.py:76` · test `tests/test_auth_guard.py:119-123`
- **FR-BE-105** — Une erreur de base pendant la résolution d'une clé utilisateur est journalisée en `warning` et traitée comme une clé invalide (401, fail-closed). — *implémenté* — preuve `app/services/api_key_service.py:109-130` · test : aucun sur le chemin d'exception
- **FR-BE-106** — L'IP enregistrée comme « dernière utilisation » est lue dans `CF-Connecting-IP`, sinon `X-Forwarded-For` (premier élément), sinon l'adresse du socket. — *implémenté* — preuve `app/api/deps.py:37-44`, `app/api/deps.py:60` · test `tests/test_api_key_service.py:216` (IP passée directement au service, la résolution des en-têtes n'est pas testée)

### Garde du router IA (`verify_api_key`)

- **FR-BE-107** — Le router `/api/ai` applique la même authentification que FR-BE-102/103, à tous ses endpoints, y compris ceux qui n'utilisent que le LLM. — *implémenté* — preuve `app/api/ai.py:52-56`, `app/api/deps.py:76-90` · test `tests/test_ai_deps.py:55-81`
- **FR-BE-108** — Quand sqlite-vec n'est pas chargé, `/api/ai` répond 503 « AI vector storage unavailable » **avant** tout contrôle de clé : un appelant sans clé reçoit 503, pas 401. — *implémenté* — preuve `app/api/deps.py:81-90` · test `tests/test_ai_deps.py:46` (avec une clé correcte seulement ; le cas sans clé n'est pas testé)

### Cycle de vie d'une clé utilisateur (`api_key_service`)

- **FR-BE-109** — Une clé est un jeton `phk_` + 32 octets aléatoires ; seul son condensat SHA-256 est stocké (colonne unique indexée), jamais le clair. — *implémenté* — preuve `app/services/api_key_service.py:26-37`, `app/services/api_key_service.py:61-66`, `app/models/database.py:427-441` · test `tests/test_api_key_service.py:43`, `tests/test_api_key_service.py:130`
- **FR-BE-110** — Le clair n'est renvoyé qu'une fois, à la création ; la liste et le tableau admin n'exposent que les 10 premiers caractères (`key_prefix`). — *implémenté* — preuve `app/services/api_key_service.py:27`, `app/services/api_key_service.py:58-74`, `app/api/api_keys.py:50-51`, `app/api/api_keys.py:87-88` · test `tests/test_api_key_service.py:73`, `tests/test_router_http_coverage.py:488-492`
- **FR-BE-111** — Une clé est active si `revoked_at` est nul et si `expires_at` est nul ou strictement dans le futur ; le statut exposé vaut `active`, `revoked` ou `expired`, la révocation l'emportant. — *implémenté* — preuve `app/services/api_key_service.py:40-55` · test `tests/test_api_key_service.py:179-210`, `tests/test_api_key_service.py:301`
- **FR-BE-112** — La révocation est idempotente (l'horodatage d'origine est conservé) et prend effet à la requête suivante : aucune mise en cache de l'authentification. — *implémenté* — preuve `app/services/api_key_service.py:86-96`, `app/services/api_key_service.py:110-114` · test `tests/test_api_key_service.py:145`, `tests/test_api_key_service.py:162`, `tests/test_router_http_coverage.py:507-511`
- **FR-BE-113** — Chaque authentification par clé utilisateur met à jour `last_used_at` et `last_used_ip` au plus une fois par minute et par clé, dans une session à part ; un échec de cette écriture n'invalide pas l'authentification. — *implémenté* — preuve `app/services/api_key_service.py:28`, `app/services/api_key_service.py:115-127` · test `tests/test_api_key_service.py:216`, `tests/test_api_key_service.py:239`
- **FR-BE-114** — Un label réduit à des espaces est enregistré sous « unnamed ». — *implémenté* — preuve `app/services/api_key_service.py:66` · test `tests/test_api_key_service.py:86`
- **FR-BE-115** — Le service offre une lecture d'une clé par identifiant (`get_key`). — *mort* (aucun appelant dans `app/` ; seuls les tests l'utilisent) — preuve `app/services/api_key_service.py:82-83` · test `tests/test_api_key_service.py:324-328`

### API JSON de gestion (`/api/admin/keys`)

- **FR-BE-116** — `GET`, `POST /api/admin/keys`, `POST /api/admin/keys/{id}/revoke` et `DELETE /api/admin/keys/{id}` (révocation douce, la ligne est conservée) répondent en camelCase ; la création répond 201. — *implémenté* — preuve `app/api/api_keys.py:20-24`, `app/api/api_keys.py:68-105`, `app/main.py:709` · test `tests/test_router_http_coverage.py:476-518`
- **FR-BE-117** — La création exige un label de 1 à 120 caractères et accepte une expiration de 1 à 3650 jours (422 hors bornes). — *implémenté* — preuve `app/api/api_keys.py:29-33`, `app/api/api_keys.py:81-83` · test `tests/test_router_http_coverage.py:546-553` (bornes non testées)
- **FR-BE-118** — La révocation d'un identifiant inconnu répond 404 « API key not found ». — *implémenté* — preuve `app/api/api_keys.py:93-95`, `app/api/api_keys.py:102-104` · test `tests/test_router_http_coverage.py:520-524`
- **FR-BE-119** — La liste expose, pour chaque clé, le préfixe, le statut, les dates et la dernière IP d'utilisation, triés de la plus récente à la plus ancienne. — *implémenté* — preuve `app/api/api_keys.py:36-65`, `app/services/api_key_service.py:77-79` · test `tests/test_api_key_service.py:312`

### UI admin des clés (`/admin/keys`)

- **FR-BE-120** — La page `/admin/keys` liste les clés, rafraîchit le tableau par `/admin/keys/table`, crée une clé par formulaire (label vide → 422 avec message) et affiche le clair une fois, puis déclenche `refresh-keys`. — *implémenté* — preuve `app/api/admin.py:883-945` · test `tests/test_admin_csrf.py:57-72` (création et tableau, sous l'angle CSRF seulement)
- **FR-BE-121** — La durée d'expiration saisie dans l'UI est validée comme dans l'API JSON. — *partiel* (manque : une valeur non numérique, nulle ou négative est ignorée en silence et produit une clé **sans expiration** ; aucune borne haute) — preuve `app/api/admin.py:931-938` · test : aucun
- **FR-BE-122** — La révocation depuis l'UI re-rend le tableau ; un identifiant inconnu est ignoré sans message. — *implémenté* — preuve `app/api/admin.py:948-955` · test : aucun

### Garde maître (`verify_master_key`)

- **FR-BE-123** — La gestion des clés n'accepte que le secret maître : une clé utilisateur active reçoit 401 « Master key required ». — *implémenté* — preuve `app/api/deps.py:93-107`, `app/api/api_keys.py:23` · test `tests/test_router_http_coverage.py:526-544`
- **FR-BE-124** — Secret maître non configuré : `verify_master_key` répond 503 « Backend secret not configured », alors que les deux autres gardes `X-API-Key` répondent 401 dans le même cas. — *implémenté* — preuve `app/api/deps.py:98-102` · test : aucun
- **FR-BE-125** — La même garde maître protège les miroirs JSON `/api/admin/downloads`, `/api/admin/plex-downloads` et `/api/admin/enrichment`. — *implémenté* — preuve `app/api/downloads.py:34-36`, `app/api/plex_downloads.py:47-49`, `app/api/enrichment.py:34-36`, `app/main.py:710-712` · test `tests/test_download_security.py:270`, `tests/test_plex_downloads_json.py:79`

### Basic Auth navigateur, WebDAV et métriques

- **FR-BE-126** — `/admin*`, `/docs` et `/openapi.json` exigent une Basic Auth (identifiant et mot de passe comparés en temps constant, tous deux évalués avant la décision) ; échec : 401 avec `WWW-Authenticate: Basic`. — *implémenté* — preuve `app/api/deps.py:113-145`, `app/main.py:653-668`, `app/main.py:679-686` · test : aucun test dédié (`tests/test_admin_csrf.py` ne s'en sert qu'avec des identifiants valides)
- **FR-BE-127** — Mot de passe admin vide : 503 « Admin UI not configured » (fail-closed). — *implémenté* — preuve `app/api/deps.py:123-127`, `app/config.py:43-45` · test : aucun
- **FR-BE-128** — La documentation interactive n'est pas publiée aux URL par défaut ; elle est ré-exposée derrière la Basic Auth admin. — *implémenté* — preuve `app/main.py:587-591`, `app/main.py:679-686` · test : aucun
- **FR-BE-129** — `/dav` exige une Basic Auth dédiée, distincte de `X-API-Key` et de l'admin ; mot de passe vide : 503 « DAV not configured » avant tout contrôle d'identifiants. — *implémenté* — preuve `app/api/dav.py:79`, `app/api/deps.py:151-185`, `app/config.py:265-266` · test : aucun test dédié à la garde
- **FR-BE-130** — `DAV_ENABLED=false` n'est contrôlé qu'**après** l'authentification, dans le handler : il faut des identifiants valides pour recevoir 503 « WebDAV disabled ». — *implémenté* — preuve `app/api/dav.py:288-289` · test : aucun
- **FR-BE-131** — `/metrics` exige une Basic Auth dédiée ; mot de passe vide : 503 ; `METRICS_PUBLIC=true` supprime tout contrôle et journalise un avertissement au démarrage. — *implémenté* — preuve `app/api/deps.py:191-234`, `app/main.py:734`, `app/config.py:62`, `app/config.py:484-489` · test `tests/test_metrics_auth.py:41-71`, `tests/test_metrics_auth.py:100`, `tests/test_metrics_auth.py:139`

### Assertion de boot et CSRF

- **FR-BE-132** — Au démarrage, l'application lève une `RuntimeError` si une route `/api/*` hors liste publique ne porte, à aucune profondeur de dépendance, l'une des quatre gardes reconnues. — *implémenté* — preuve `app/api/route_audit.py:57-64`, `app/api/route_audit.py:90-136`, `app/main.py:727-728` · test `tests/test_route_auth_assertion.py:96-101`
- **FR-BE-133** — La liste publique compte exactement quatre chemins : `/api/health`, `/api/tv-auth/start`, `/api/tv-auth/status`, `/api/tv-auth/complete`. — *implémenté* — preuve `app/api/route_audit.py:72-87` · test `tests/test_route_auth_assertion.py:164-173`
- **FR-BE-134** — Une requête `POST`/`PUT`/`PATCH`/`DELETE` sous `/admin*` portant `Sec-Fetch-Site: cross-site` reçoit 403 et un avertissement journalisé ; les `GET` et l'absence d'en-tête passent ; `/api/admin/*` n'est pas concerné. — *implémenté* — preuve `app/api/csrf.py:85-87`, `app/api/csrf.py:98-113` · test `tests/test_admin_csrf.py:54-146`, `tests/test_admin_csrf.py:216`

### Client TV

- **FR-BE-135** — L'app TV ajoute `X-API-Key` aux seules requêtes dont l'hôte est un backend PlexHub connu, sans écraser un en-tête déjà présent, et n'envoie rien si la clé est vide. — *implémenté* — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiKeyInterceptor.kt:44-60` · test `tv:core/network/src/test/java/com/chakir/plexhubtv/core/network/backend/BackendApiKeyInterceptorTest.kt`
- **FR-BE-136** — La clé est stockée dans des préférences chiffrées (AES-256-GCM) sous une entrée unique partagée par les appels backend et IA. — *implémenté* — preuve `tv:core/datastore/src/main/java/com/chakir/plexhubtv/core/datastore/SecurePreferencesManager.kt:52`, `tv:core/datastore/src/main/java/com/chakir/plexhubtv/core/datastore/SecurePreferencesManager.kt:105-113`
- **FR-BE-137** — L'app TV affiche une alerte dans la barre du haut quand un backend est configuré sans clé, avec renvoi vers les réglages. — *implémenté* — preuve `tv:app/src/main/java/com/chakir/plexhubtv/feature/backendkey/BackendKeyChipViewModel.kt:34-51`
- **FR-BE-138** — L'app TV réagit à une clé refusée (401/403) en cours d'usage. — *partiel* (manque : la réponse est seulement convertie en erreur `Unauthorized` générique ; aucune invite de ressaisie, l'alerte FR-BE-137 ne détecte que l'absence de clé, pas une clé révoquée) — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/util/SafeApiCall.kt:136`

## Règles métier

- **RG-BE-02.1** — Aucune notion de portée ni de rôle par clé : une clé utilisateur ouvre toute l'API JSON, l'IA et `/api/tv-auth/approve` ; seules la gestion des clés et les miroirs d'administration lui sont fermés. — preuve `app/models/database.py:429-437`, `app/api/deps.py:64-107`, `app/api/tv_auth.py:40`
- **RG-BE-02.2** — Le secret maître n'est pas une ligne de `api_keys` : il ne se révoque pas, n'expire pas et n'a pas de trace d'utilisation ; seul un changement de la variable d'environnement le fait tourner. — preuve `app/services/api_key_service.py:8-9`, `app/api/deps.py:47-51`
- **RG-BE-02.3** — Quatre familles de secrets cloisonnées : `X-API-Key` (maître + utilisateurs), admin, DAV, métriques ; chaque garde ne lit que son couple. — preuve `app/api/deps.py:47-234`
- **RG-BE-02.4** — Tous les mots de passe Basic Auth vides ferment la route (503) ; en revanche un secret maître vide ne ferme pas l'API JSON (401 à tous, clés utilisateur toujours acceptées). — preuve `app/api/deps.py:54-61`, `app/api/deps.py:123-127`, `app/api/deps.py:163-167`, `app/api/deps.py:212-216`
- **RG-BE-02.5** — Aucun limiteur ni verrouillage sur les échecs d'authentification (`X-API-Key` et Basic Auth) : le seul limiteur existant porte sur `POST /api/tv-auth/start` ; la protection contre la force brute est déléguée à l'ingress. — preuve `app/config.py:71-95`, `CLAUDE.md` §9 piège 19(d)
- **RG-BE-02.6** — La table `api_keys` n'a pas de migration : elle est créée par `create_all` au démarrage, sur base neuve comme existante. — preuve `app/db/database.py:128`, `app/models/database.py:411-441` (aucune occurrence de `api_keys` dans `app/db/migrations.py`)
- **RG-BE-02.7** — L'IP de dernière utilisation vient d'en-têtes posés par le client hors tunnel : elle est indicative, pas probante. — preuve `app/api/deps.py:41-43`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| `/api/*` gardé, clé absente ou fausse | 401 « Invalid API key » | `app/api/deps.py:69-73` |
| Secret maître vide | API JSON et IA : 401 ; gestion des clés et miroirs : 503 | `app/api/deps.py:54-61`, `app/api/deps.py:98-102` |
| `/api/ai`, sqlite-vec absent, sans clé | 503 (et non 401) | `app/api/deps.py:81-85` |
| Clé utilisateur sur `/api/admin/keys` | 401 « Master key required » | `app/api/deps.py:103-107` |
| Clé révoquée ou expirée à l'instant exact | 401 dès la requête suivante | `app/services/api_key_service.py:40-46` |
| Base verrouillée pendant la résolution | lecture : 401 (fail-closed) ; mise à jour de `last_used` : ignorée | `app/services/api_key_service.py:125-130` |
| Révocation JSON d'un id inconnu / UI | 404 / tableau re-rendu sans message | `app/api/api_keys.py:94-95`, `app/api/admin.py:952` |
| Durée d'expiration invalide dans l'UI | clé créée sans expiration | `app/api/admin.py:932-938` |
| `/admin`, `/dav`, `/metrics` sans identifiants | 401 + `WWW-Authenticate: Basic` | `app/api/deps.py:128-134`, `app/api/deps.py:168-174`, `app/api/deps.py:217-223` |
| Mot de passe admin, DAV ou métriques vide | 503 | `app/api/deps.py:123-127`, `app/api/deps.py:163-167`, `app/api/deps.py:212-216` |
| `/dav` identifiants valides, `DAV_ENABLED=false` | 503 « WebDAV disabled » | `app/api/dav.py:288-289` |
| Mutation `/admin*` cross-site | 403 | `app/api/csrf.py:98-113` |
| Route `/api/*` non gardée ajoutée | démarrage refusé | `app/api/route_audit.py:124-136` |
| TV sans clé, backend configuré | alerte barre du haut ; requêtes envoyées sans en-tête → 401 | `tv:app/src/main/java/com/chakir/plexhubtv/feature/backendkey/BackendKeyChipViewModel.kt:48-51` |

## Dépendances

- Autres capacités : toutes les capacités `/api/*` en dépendent ; `CAP-BE-03` (`approve` utilise `verify_backend_secret` sous l'alias `verify_pairing_api_key`), `CAP-BE-01` (garde `/metrics`, liste publique partagée avec `/api/health`), `CAP-BE-17` (garde `/dav`), `CAP-BE-18`/`CAP-BE-19`/`CAP-BE-06` (miroirs sous garde maître), `CAP-BE-21` (UI admin sous Basic Auth).
- Autre dépôt : clients `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiKeyInterceptor.kt`, `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/ai/AiAuthInterceptor.kt` ; l'app TV ne gère pas les clés, elle n'en porte qu'une.
- Données : table `api_keys` (`docs/retro-spec/technique/db/`).
- Configuration : `AI_API_KEY`, `ADMIN_*`, `DAV_*`, `METRICS_*` (mécanisme seulement, aucune valeur documentée ici).

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| `CLAUDE.md` §9 piège 12 affirme qu'une clé absente ou invalide donne 401 « avant tout » sur `/api/ai` ; le code contrôle sqlite-vec d'abord et répond 503 à un appelant anonyme (FR-BE-108) | spec ↔ code | À trancher par Chakir : inverser l'ordre des contrôles (401 d'abord, n'expose plus l'état interne) ou corriger le piège 12 |
| `CLAUDE.md` §3 « Auth » annonce « 0 test de rejet sur ce garde (CR-T02) » ; `tests/test_auth_guard.py` couvre 401 sans clé et clé fausse sur 7 endpoints | doc périmée | Retirer la mention au prochain `/sync-context` |
| `CLAUDE.md` §9 piège 10 dit encore `/metrics` non authentifié (CR-S02) ; le piège 19(b) et le code (FR-BE-131) disent l'inverse | doc périmée (contradiction interne) | Supprimer la phrase du piège 10 |
| Numéros de ligne de `CLAUDE.md` §2/§3 périmés : deps.py lignes 59/71/88/108/148 (réel `app/api/deps.py:64`, 76, 93, 113, 151), montage `api_keys` main.py ligne 438 (réel `app/main.py:709`) | doc périmée | `/sync-context` |
| Asymétrie non documentée : secret maître vide → 401 sur l'API, 503 sur la gestion des clés (FR-BE-124) | spec ↔ code | Documenter dans §3, ou aligner |
| L'UI admin transforme une expiration invalide en clé éternelle, sans borne haute, alors que l'API JSON refuse (FR-BE-121) | comportement risqué | Réutiliser la validation `ApiKeyCreate` dans le formulaire |
| Aucune portée par clé : une clé confiée à un appareil peut approuver un appairage TV, lancer une synchro ou générer la bibliothèque (RG-BE-02.1) | sécurité (mécanisme) | À trancher par Chakir : portées minimales (lecture / appairage / admin) ou accepter |
| Aucun journal des échecs d'authentification (`X-API-Key` et Basic Auth) ; seul le refus CSRF est journalisé | observabilité | Compteur Prometheus ou log `warning` échantillonné des 401 |
| `get_key` n'a aucun appelant (FR-BE-115) | code mort | Supprimer ou utiliser dans la révocation |
| `create_key` et `revoke_key` écrivent par `commit()` direct, hors `write_with_retry` (§9 piège 8) | dette | À vérifier sous vrai verrou (question ouverte) |
| Aucun test de `verify_admin_basic_auth`, `verify_dav_basic_auth`, `verify_master_key` à secret vide, ni de sqlite-vec absent sans clé ; `tests/test_metrics_auth.py` sert de modèle | test manquant | Ajouter l'équivalent pour `/admin` et `/dav` en priorité |
| Côté TV, une clé révoquée n'est signalée par aucune invite (FR-BE-138) | spec ↔ code (TV) | Voir la fiche TV de l'intégration backend |

## Questions ouvertes

- `/api/ai/*` appelé sans clé quand sqlite-vec n'est pas chargé : 503 confirmé en HTTP réel, et est-ce voulu ? — à vérifier par test : cas `_VEC_LOADED=False` + en-tête absent dans `tests/test_ai_deps.py`.
- `POST /api/admin/keys` quand le secret maître est vide : 503 ou 401 en HTTP réel ? — à vérifier par test.
- `/admin` et `/docs` avec mot de passe admin vide, puis identifiants faux : 503 puis 401 avec `WWW-Authenticate` ? — à vérifier par test (modèle `tests/test_metrics_auth.py`).
- `/dav` avec `DAV_ENABLED=false` : un client sans identifiants reçoit-il 401 (et non 503), révélant que la route existe ? — à vérifier par test.
- La création ou la révocation d'une clé pendant une fenêtre d'écriture longue (validation de flux) échoue-t-elle en 500 faute de retry ? — à vérifier par test sous vrai verrou WAL (modèle `tests/test_db_retry_real_lock.py`).
- Avec la vraie base, une lecture de `api_keys` peut-elle lever pendant un verrou d'écriture (WAL), et donc refuser à tort une clé utilisateur valide (FR-BE-105) ? — à vérifier par test sous vrai verrou.
- L'app TV, après révocation de sa clé, montre-t-elle un message exploitable à l'écran ou seulement des listes vides ? — à vérifier par test sur l'app (clé révoquée, navigation accueil).
