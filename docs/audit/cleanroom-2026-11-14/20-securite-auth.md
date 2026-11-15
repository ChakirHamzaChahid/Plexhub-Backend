# Dimension 2 — Sécurité & Authentification

> **Audit Clean-Room** · Commit `6cb09fa` · `APP_VERSION = "1.15.0"` · 2026-11-14

## 1. Synthèse de la posture de sécurité

Le backend présente une architecture de sécurité globalement mature et défensive :
- **Authentification fail-closed systématique :** `app/api/route_audit.py` inspecte au boot chaque route `/api/*` et fait échouer le démarrage si une route hors allowlist (`PUBLIC_API_ALLOWLIST`, 5 routes) est dépourvue d'un garde reconnu (`deps.py`).
- **Isolation stricte des scopes :** Les clés scopées (`scrape`, `scrape.admin`, `media.read`, `media.write`) sont rejetées (`403 insufficient_scope`) sur l'API JSON générale et réciproquement.
- **Comparaison temps constant :** `secrets.compare_digest` sur le secret maître et les Basic Auth (`/admin`, `/dav`, `/metrics`, tous fail-closed `503` si mot de passe vide).
- **Durcissement médias & parsing :** `poster_match_service.py` rejette les SVG, vérifie `DecompressionBombError` / `POSTER_MAX_PIXELS` et lit en streaming brut `aiter_raw()` ; `epg_sync_service.py` bloque `<!ENTITY` et UTF-16/32 ; `resolve_confined` bloque les traversées de chemin et symlinks ; requêtes SQL `text()` paramétrées (aucune SQLi détectée) ; exécution conteneur non-root (`UID/GID 1000`).

Points d'attention majeurs : **fuite de credentials Xtream** dans HTTP 400 et logs applicatifs (`CR-S01`, `CR-S02`) et **absence de garde SSRF** sur `xtream_service` et l'URL initiale de `download_to_disk` (`CR-S03`, `CR-S04`).

---

## 2. Tableau récapitulatif des findings (`CR-S*`)

| ID | Sévérité | Domaine | Intitulé | Fichiers & Lignes |
|---|---|---|---|---|
| **CR-S01** | **HIGH** | Fuite de Secrets | Fuite d'identifiants Xtream dans la réponse HTTP 400 (`POST /api/accounts/{id}/test`) | `app/services/account_service.py:185`, `app/api/accounts.py:118` |
| **CR-S02** | **HIGH** | Fuite de Secrets | Inscription en clair des identifiants Xtream dans les logs (`str(e)` httpx) | `app/services/xtream_service.py:74`, `app/workers/sync_worker.py:1310, 1433, 1688, 1814`, `app/main.py:223` |
| **CR-S03** | **HIGH** | SSRF | Absence de contrôle SSRF dans `xtream_service` et à la création de compte | `app/services/xtream_service.py:28-36`, `app/api/accounts.py:50-118` |
| **CR-S04** | **MEDIUM** | SSRF | URL initiale non vérifiée anti-SSRF dans `download_service.download_to_disk` | `app/services/download_service.py:452-470` |
| **CR-S05** | **MEDIUM** | Crypto / Secrets | Chiffrement au repos des mots de passe Xtream fail-OPEN si aucune clé configurée | `app/utils/crypto_fields.py:37-42, 88-100, 116-121` |
| **CR-S06** | **MEDIUM** | Déni de Service | Absence de limitation de taille du body sur l'API générale (`body_limit` limité à scrape/metadata) | `app/api/body_limit.py:39-43, 71-75`, `app/main.py:658-661`, `Dockerfile:67` |
| **CR-S07** | **LOW** | Rate-Limiting | Absence de rate-limit sur `POST /tv-auth/approve`, `GET /tv-auth/status`, `POST /tv-auth/complete` | `app/api/tv_auth.py:482, 547, 653` |
| **CR-S08** | **LOW** | Rate-Limiting | Spoofing d'IP possible via `CF-Connecting-IP` / `X-Forwarded-For` | `app/utils/rate_limit.py:220-223`, `docker-compose.yml:5-6` |
| **CR-S09** | **LOW** | CORS | Origine CORS par défaut permissive (`CORS_ORIGINS=*`) | `app/config.py:236-237`, `app/main.py:634-649` |
| **CR-S10** | **LOW** | Auth / Brute-force | Absence de verrouillage / backoff applicatif sur Basic Auth (`/admin`, `/dav`, `/metrics`) | `app/api/deps.py:239-360`, `app/api/dav.py:79` |
| **CR-S11** | **LOW** | Route Audit | Périmètre de `route_audit.py` restreint aux routes préfixées `/api/*` | `app/api/route_audit.py:180-181` |

---

## 3. Fiches détaillées des findings

### `CR-S01` — Fuite d'identifiants Xtream dans la réponse HTTP 400 (`POST /api/accounts/{id}/test`)
- **Sévérité :** `HIGH`
- **Preuves :** `app/services/account_service.py:182-185` encapsule `str(e)` dans `AccountAuthenticationError(str(e))`, puis `app/api/accounts.py:117-118` renvoie `HTTPException(400, f"Connection test failed: {e}")`.
- **Mécanisme :** `str(httpx.HTTPStatusError)` embarque l'URL complète incluant `?username=...&password=...`.
- **Remédiation :** Ne jamais renvoyer `str(e)` d'une exception `httpx` au client ; exposer uniquement `f"HTTP {e.response.status_code}"` ou `type(e).__name__`.

### `CR-S02` — Inscription en clair des identifiants Xtream dans les logs
- **Sévérité :** `HIGH`
- **Preuves :** `app/services/xtream_service.py:74` (`logger.warning(... ({e}) ...)`), `app/workers/sync_worker.py:1310, 1433, 1688, 1814`, `app/main.py:223`.
- **Mécanisme :** Bien que le logger `httpx` soit épinglé à `WARNING` (`app/main.py:111`), les loggers applicatifs interpolent `{e}` directement.
- **Remédiation :** Adopter le pattern de `xtream_service.download_xmltv` (`xtream_service.py:273-276, 298`) : logger uniquement `type(e).__name__` et le code HTTP.

### `CR-S03` — Absence de contrôle SSRF dans `xtream_service`
- **Sévérité :** `HIGH`
- **Preuves :** `app/services/xtream_service.py:26-36` instancie `httpx.AsyncClient` sans `event_hooks={"request": [ssrf.vet_request]}` ; `app/api/accounts.py:50-118` ne valide pas `base_url` via `assert_public_host`.
- **Impact :** Un client authentifié peut faire cibler le réseau interne (`127.0.0.1`, `169.254.169.254`, services LAN) via `/accounts` et `/test`.
- **Remédiation :** Ajouter `event_hooks={"request": [ssrf.vet_request]}` dans `xtream_service._get_client()` et valider `base_url` à l'enregistrement/mise à jour (avec opt-in explicite si IPTV local autorisé en homelab).

### `CR-S04` — URL initiale non vérifiée anti-SSRF dans `download_service.download_to_disk`
- **Sévérité :** `MEDIUM`
- **Preuves :** `app/services/download_service.py:452-470` vérifie `_assert_public_redirect_host` uniquement sur les réponses `301/302/303/307/308`, pas sur l'URL initiale avant `client.stream("GET", url)`.
- **Remédiation :** Appeler `await _assert_public_redirect_host(httpx.URL(url).host)` avant la boucle `while True`.

### `CR-S05` — Chiffrement au repos des mots de passe Xtream fail-OPEN
- **Sévérité :** `MEDIUM`
- **Preuves :** `app/utils/crypto_fields.py:37-42, 88-100, 116-121` écrit et lit en clair si ni `XTREAM_ENCRYPTION_KEY` ni `AI_API_KEY` n'est définie (contrairement à `payload_crypto.py` qui est fail-closed).
- **Remédiation :** Exiger une clé de chiffrement configurée (fail-closed) lors de l'écriture/lecture de comptes Xtream.

### `CR-S06` — Absence de limitation de taille du body sur l'API générale
- **Sévérité :** `MEDIUM`
- **Preuves :** `app/api/body_limit.py:39-43, 71-75` ne couvre que `/api/scrape/v1/` (64 KiB) et `/api/metadata/v1/` (256 KiB) ; `Dockerfile:67` lance `uvicorn` sans `--limit-max-body-size`.
- **Remédiation :** Ajouter `--limit-max-body-size` dans `Dockerfile` et une limite par défaut raisonnable sur les routes mutantes.

### `CR-S07` à `CR-S11` — Durcissement complémentaire (`LOW`)
- **`CR-S07`** (`app/api/tv_auth.py:482, 547, 653`) : ajouter un `SlidingWindowLimiter` sur `POST /tv-auth/approve`, `GET /status` et `POST /complete`.
- **`CR-S08`** (`app/utils/rate_limit.py:220-223`, `docker-compose.yml:5-6`) : restreindre la lecture de `CF-Connecting-IP`/`X-Forwarded-For` aux proxies de confiance (`forwarded_allow_ips`) ou binder `127.0.0.1:8000` si derrière tunnel/reverse proxy.
- **`CR-S09`** (`app/config.py:236-237`, `app/main.py:634-649`) : remplacer la valeur par défaut `CORS_ORIGINS=*` par une liste vide par défaut en production.
- **`CR-S10`** (`app/api/deps.py:239-360`, `app/api/dav.py:79`) : ajouter un rate-limit/backoff applicatif sur les échecs Basic Auth (`/admin`, `/dav`, `/metrics`).
- **`CR-S11`** (`app/api/route_audit.py:180-181`) : étendre la table d'audit de `route_audit.py` aux routes hors `/api/` (`/admin*`, `/dav*`, `/metrics`).
