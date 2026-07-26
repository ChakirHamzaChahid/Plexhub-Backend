# Audit v1 — Phase 2 : Sécurité

> HEAD `9da9d46` (v1.7.1), branche `develop`. Audit indépendant ; smoke empirique sur serveur local (DB fraîche).

## 1. Matrice d'authentification RÉELLE (prouvée code + smoke)

| Surface | Garde | Preuve | Smoke |
|---|---|---|---|
| `/api/accounts`, `/categories`, `/live`, `/media`, `/stream`, `/sync`, `/plex` | `verify_backend_secret` (maître OU clé per-user active) | `main.py:568,573-579` ; `deps.py:61-70` | `/api/media/movies` sans clé → **401** ; avec clé → 200 |
| `/api/ai/*` (13 endpoints) | `verify_api_key` = même auth + 503 sqlite-vec | `ai.py:52-55`, `deps.py:73-87` | — |
| `/api/admin/keys`, `/api/admin/downloads`, `/api/admin/plex-downloads`, `/api/admin/enrichment` | `verify_master_key` (maître SEUL ; 503 si `AI_API_KEY` vide) | `api_keys.py:20-23`, `downloads.py:33-36`, `plex_downloads.py:46-49`, `enrichment.py:33-36`, `deps.py:90-104` | — |
| `/admin*` (4 routers HTMX), `/docs`, `/openapi.json` | `verify_admin_basic_auth` (503 si `ADMIN_PASSWORD` vide) | `main.py:588-621`, `deps.py:110-142` | `/docs` sans auth → **401** |
| `/dav/*` (OPTIONS/PROPFIND/HEAD/GET) | `verify_dav_basic_auth` (503 si `DAV_ENABLED=false` OU `DAV_PASSWORD` vide) | `dav.py:79`, `deps.py:148-182` | `/dav/` → **503** fail-closed |
| **Publics** | — | `/api/health` (`main.py:570`) ; `/api/tv-auth/start|status|complete` (`main.py:582` ; `/approve` gardé, `tv_auth.py:40,260`) ; **`/metrics`** (`metrics.py:46-51`) | `/api/health` 200 ; `/metrics` **200 sans clé** |

**Qualité des primitives (vérifiée)** : comparaison **temps-constant** partout (`secrets.compare_digest` — maître `deps.py:44-48`, Basic Auth user+pass évalués tous deux avant branchement `deps.py:132-142,172-182`) ; clés per-user stockées en **digest SHA-256**, lookup par digest (pas d'oracle timing), plaintext rendu une seule fois (`api_key_service.py:31-37,58-74`) ; fail-closed par défaut (mot de passe vide ⇒ 503, clé absente ⇒ 401). L'auth « fail-closed sur toute l'API JSON » du bandeau est **confirmée**.

**Réserve structurelle (CR-A04, toujours vraie)** : 3 conventions de montage cohabitent et **aucune assertion au boot** ne garantit que tout `/api/*` porte une garde (follow-up acté en commentaire `main.py:555-564`). Un futur router monté « Pattern A » sans `dependencies=_guard` passerait silencieusement. Croisé avec **CR-T02** (0 test de rejet 401 sur `verify_backend_secret` — grep `tests/` non re-vérifié ici, périmètre agent 5-8).

## 2. Secrets — état vérifié

- **URL Xtream (user/pass embarqués)** : jamais persistée (aucune colonne URL sur `download_job`, `models/database.py`) ; re-dérivée au worker (`download_worker.py:463-470`) ; messages d'exception sans URL (`download_service.py:369`, `relay.py:228-242` avec `from None`) ; **logger httpx épinglé WARNING** avec justification anti-fuite (`main.py:94-104`). ✔
- **`XtreamAccount.password`** et **`PlexServer.access_token`** chiffrés Fernet via `EncryptedString` (`models/database.py:140,577`) ; migration 016 one-shot idempotente. ✔ — mais voir AUDIT-P2-002 (fail-open).
- **Clés API** : digest-only en DB ✔. **TMDB/OMDb** : tronquées à 4 chars dans les logs (`config.py:231,236`) ; `omdb_service` documente la non-fuite via `str(exc)` httpx. ✔
- **`base_uri` Plex** : en clair en DB (pas un secret) mais exclu des réponses API/HTML — conforme.

## Findings

### AUDIT-P2-001 — `/metrics` non authentifié sur la même app que le tunnel public — **S2** (= CR-S02, toujours ouvert)
- **Preuve** : `setup_instrumentator` expose `/metrics` sans aucune dépendance (`app/utils/metrics.py:46-51`, monté `main.py:660-661`) ; smoke : **200 sans clé**. Les métriques métier portent des labels `account_id` (`metrics.py:14-37`) et l'instrumentator révèle la carte des routes + volumes par status.
- **Impact** : reconnaissance de la surface API + fuite d'identifiants de comptes (labels) à quiconque atteint le tunnel. Pas de credentials exposés.
- **Effort** : faible (dépendance Basic Auth ou filtre ingress ; attention à ne pas casser le scrape Prometheus).

### AUDIT-P2-002 — Chiffrement au repos **fail-open** + clé dérivée du secret bearer — **S3** (résiduel CR-S03/CR-S04, en partie by-design documenté)
- **Preuve** : `get_xtream_fernet` → si ni `XTREAM_ENCRYPTION_KEY` ni `AI_API_KEY` : **plaintext au repos** avec un seul warning (`app/utils/crypto_fields.py:81-109`) ; déchiffrement raté (clé rotée) → retour du **ciphertext** sans erreur au caller (`:139-158`) → un `build_stream_url` construirait des URLs avec un mot de passe illisible (panne fonctionnelle silencieuse différée). Clé par défaut **dérivée d'`AI_API_KEY`** (avec tag de séparation de domaine ✔) : la rotation du secret d'API **brique** les mots de passe stockés.
- **Jugement de gravité honnête** : le cas « plaintext » exige les DEUX secrets absents — déploiement où l'API n'a de toute façon plus de secret maître ; le vrai risque opérationnel est la **rotation d'`AI_API_KEY` sans re-chiffrement** (perte de credentials différée et silencieuse). S3, pas S2.
- **Effort** : faible (documenter la procédure de rotation + log ERROR au boot si des lignes ne se déchiffrent pas — un `SELECT` de sonde suffit).

### AUDIT-P2-003 — Clé Fernet tv-auth = `SHA-256(AI_API_KEY)` **sans séparation de domaine** — **S3** (= CR-S04, toujours ouvert)
- **Preuve** : `payload_crypto.get_fernet` dérive directement `sha256(AI_API_KEY)` (`app/utils/payload_crypto.py:42-46`) — contrairement à `crypto_fields` qui, lui, ajoute `_KEY_DERIVATION_CONTEXT` (`crypto_fields.py:68-71`) précisément pour ne pas dupliquer cette clé. Le secret bearer sert donc de KEK telle quelle pour les payloads d'appairage (qui contiennent des tokens Plex, §5.6).
- **Impact** : réutilisation de clé (bearer ⇆ chiffrement) ; toute fuite du header `X-API-Key` déchiffre les payloads au repos ; asymétrie incohérente entre les deux modules de crypto maison.
- **Effort** : faible mais **migration** : changer la dérivation invalide les payloads existants (TTL 900 s → fenêtre de casse minuscule, acceptable).

### AUDIT-P2-004 — Aucun rate-limit / anti-brute-force / anti-flood sur les surfaces publiques — **S2** (= CR-S05, toujours ouvert, aggravé par `/dav`)
- **Preuves** :
  1. `POST /api/tv-auth/start` **non authentifié** crée une ligne DB par appel (`tv_auth.py:183-249`) ; la purge ne vise que les sessions expirées > 1 h (`_CLEANUP_GRACE_MS`, `tv_auth.py:60`) → **insertion non bornée** par un client anonyme du tunnel (flood DB + write-lock SQLite).
  2. Basic Auth `/admin` + `/docs` + `/dav` : brute-force non throttlé, pas de lockout ; pour `/dav`, la revue F2/piège 18b acte que la **rotation du mot de passe est le seul mécanisme de révocation** et qu'**aucun code n'empêche** l'exposition via le tunnel.
  3. `X-API-Key` : essais illimités (la comparaison constant-time protège du timing, pas du volume).
- **Impact** : sur le déploiement cible (tunnel Cloudflare public), (1) est un DoS applicatif trivial ; (2)/(3) rendent la force brute praticable hors mitigation ingress.
- **Effort** : moyen (limiteur en middleware — par IP `cf-connecting-ip` — + cap de sessions `pending` par IP/global sur `/start` ; ou déléguer au WAF Cloudflare et le **documenter comme prérequis**, comme fait pour `/dav`).

### AUDIT-P2-005 — CSRF sur toute l'UI `/admin` (state-changing sous Basic Auth) — **S3** (= CR-S07/DL-03/UDL, toujours ouvert)
- **Preuve** : grep `csrf` dans `app/` = **0 occurrence** ; les 4 routers admin acceptent des POST de mutation (enqueue/cancel/retry downloads, sync Plex, refresh catégories) protégés uniquement par Basic Auth — que le navigateur **rejoue automatiquement** sur toute requête cross-site vers l'origin. Pas de vérif `Origin`/`Sec-Fetch-Site`, pas de token.
- **Impact** : un opérateur authentifié visitant une page piégée peut déclencher enqueue/cancel/sync à son insu. Mutations non destructives de données (pas de delete de catalogue) → S3, cohérent avec le board ; passe S2 si `/admin` est publié sur le tunnel.
- **Effort** : faible (vérifier `Sec-Fetch-Site: same-origin` dans un middleware sur POST `/admin*` — compatible HTMX, zéro template à toucher).

### AUDIT-P2-006 — CORS : origins par défaut `*` — **S3** (CR-S06 résiduel, partiellement durci)
- **Preuve** : `CORS_ORIGINS` défaut `*` (`config.py:86-88`) ; désormais **méthodes/headers explicites** (dont `X-API-Key`) + warning au boot (`main.py:535-550`, observé dans le smoke). `allow_credentials` absent (False par défaut) ✔.
- **Impact** : avec `*`, tout site web peut adresser l'API **avec** le header `X-API-Key` s'il détient une clé (exfiltrée par ailleurs) — le CORS ne protège plus le titulaire d'une clé volée utilisée en drive-by. Atténué : sans clé, tout est 401.
- **Effort** : trivial en prod (`CORS_ORIGINS` explicite dans `.env`) — c'est un défaut de valeur par défaut, pas de code.

### AUDIT-P2-007 — Clés per-user **non scopées** : accès aux URLs de flux à credentials — **S3** (hardening, croise CR-S01-résolu)
- **Preuve** : `verify_backend_secret` met le maître et toute clé per-user au même niveau pour tout le Pattern A (`deps.py:51-58`) ; `GET /api/stream/{rating_key}` renvoie l'URL Xtream complète **user/pass inclus** (`stream.py:32-36`, champ `url` du `StreamResponse`) ; `POST /api/plex/generate` (désormais confiné ✔) et `POST /api/sync/*` restent déclenchables par une clé per-user.
- **Impact** : une clé « par utilisateur » compromise = exfiltration des credentials provider de **tous** les comptes (via stream/rating_keys du catalogue) + déclenchement d'opérations lourdes. By-design pour l'app Android (elle doit lire l'URL pour jouer), mais le modèle « clé basse-confiance » suggéré par `/api/admin/keys` n'existe pas réellement.
- **Effort** : moyen (scopes par clé — `playback` vs `admin-ops` — ou au minimum documenter que toute clé émise = confiance totale catalogue).

### AUDIT-P2-008 — SSRF résiduel : `follow_redirects=True` non vetté sur images + validation de flux — **S3** (= CR-S08, toujours ouvert)
- **Preuve** : le vetting `assert_public_redirect_host` (résolution DNS → rejet loopback/RFC1918/link-local/metadata, `download_service.py:286-330`) est appliqué aux **downloads** (`:403-421`) et au **relay DAV** (`relay.py:270-283`) ✔ — mais PAS aux : téléchargements de posters/fanarts (`plex_generator/storage.py:55`, `follow_redirects=True`, URLs fournies par le provider/TMDB), ni au health-check HEAD/Range-GET (`health_check_worker.py:183,213`, URLs dérivées du `base_url` compte).
- **Impact** : post-auth uniquement (opérateur configure les comptes ; provider malveillant/compromis requis). Un provider peut faire sonder des adresses internes par le backend (réponses non exfiltrées vers lui, sauf effets de bord). Caveat DNS-rebinding déjà acté dans le code (`download_service.py:295-299`).
- **Effort** : faible (réutiliser `assert_public_redirect_host` sur ces deux clients, comme le relay l'a fait).

### AUDIT-P2-009 — `_client_ip` fait confiance à des headers spoofables pour l'audit-trail — **dette**
- **Preuve** : `cf-connecting-ip` puis `x-forwarded-for` lus tels quels (`deps.py:34-41`), stockés dans `api_keys.last_used_ip`. Hors tunnel Cloudflare (accès direct au port), un client forge son IP d'audit.
- **Impact** : pollution d'audit-trail uniquement (aucune décision d'auth basée dessus). Effort : trivial (ne lire `cf-connecting-ip` que si le pair est Cloudflare, sinon `request.client.host`).

### AUDIT-P2-010 — Injection SQL : **RAS** (vérifié)
- Grep des `text(f"…")`/`.format(` : 3 hits, tous sains — `PRAGMA table_info` sur noms de tables codés en dur (`migrations.py:69`), `PRAGMA busy_timeout` sur constante (`nfo_import_service.py:80`), placeholders **bindés** générés pour le IN vec0 (`recommendation_service.py:78`). Tout le reste passe par SQLAlchemy paramétré. ✔

## Résolutions sécurité VÉRIFIÉES à HEAD (cross-ref pour le DELTA — ne pas re-fixer)

- **CR-S01 → RÉSOLU** : `outputDir` confiné sous `PLEX_LIBRARY_DIR` par `resolve()` + appartenance `parents` (`app/api/plex.py:35-73`) ; rejet si `PLEX_LIBRARY_DIR` non configuré.
- **F-007 → tient** : `resolve_confined` = realpath sous `DOWNLOAD_DIR`, chemin 100 % dérivé serveur (`download_service.py:162-200,228-240`) ; sanitize Windows-reserved/NTFS en défense (`:150-159`).
- **DL-01 → tient** : `follow_redirects=False` + vetting IP publique par hop, budget `DOWNLOAD_MAX_REDIRECTS`, messages sans URL (`download_service.py:403-423` ; miroir DAV `relay.py:263-283`).
- **CR-S06 → partiellement résolu** : méthodes/headers explicites + warning (`main.py:535-550`) ; origins `*` par défaut subsiste (AUDIT-P2-006).
- **Docs/OpenAPI** : retirés des URLs publiques par défaut et re-servis sous Basic Auth (`main.py:527-532,614-621`) — smoke 401 sans auth. `/redoc` 404.

## Récapitulatif

| ID | Sévérité | Titre | Preuve |
|---|---|---|---|
| AUDIT-P2-001 | **S2** | `/metrics` public (labels `account_id` + carte des routes) — CR-S02 | `app/utils/metrics.py:46-51` ; smoke 200 |
| AUDIT-P2-002 | S3 | Chiffrement au repos fail-open + rotation `AI_API_KEY` = credentials briqués en silence — CR-S03 rés. | `app/utils/crypto_fields.py:81-109,139-158` |
| AUDIT-P2-003 | S3 | Clé Fernet tv-auth = SHA-256(AI_API_KEY) sans séparation de domaine — CR-S04 | `app/utils/payload_crypto.py:42-46` |
| AUDIT-P2-004 | **S2** | Zéro rate-limit : flood DB via `/tv-auth/start` anonyme + brute-force Basic/X-API-Key — CR-S05 | `app/api/tv_auth.py:60,183-249` ; `deps.py` |
| AUDIT-P2-005 | S3 | CSRF `/admin` (0 token, Basic Auth rejouée) — CR-S07/DL-03 | grep `csrf` = 0 ; routers admin |
| AUDIT-P2-006 | S3 | CORS origins défaut `*` (headers désormais explicites) — CR-S06 rés. | `app/config.py:86-88`, `main.py:541-550` |
| AUDIT-P2-007 | S3 | Clés per-user non scopées ⇒ exfiltration URLs à credentials via `/api/stream` | `app/api/stream.py:32-36`, `deps.py:51-58` |
| AUDIT-P2-008 | S3 | SSRF non vetté sur images (`storage.py:55`) + health-check (`:183,213`) — CR-S08 | `plex_generator/storage.py:55`, `health_check_worker.py:183,213` |
| AUDIT-P2-009 | dette | IP client spoofable dans l'audit-trail des clés | `app/api/deps.py:34-41` |
| AUDIT-P2-010 | RAS | Injection SQL : aucun vecteur trouvé | grep vérifié |
