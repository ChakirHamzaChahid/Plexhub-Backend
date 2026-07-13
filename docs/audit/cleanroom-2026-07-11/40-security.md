# Clean-room Security Audit — PlexHub Backend

**Dimension:** Security only · **Date:** 2026-07-11 · **HEAD basis:** `develop` (verified against live code, not house docs)

**Verdict:** 3.5/5

The authentication core is in good shape and **fail-closed across the whole JSON API** — this is the single most important fact and it is confirmed from the code (not assumed from `CLAUDE.md`, whose §10 "Auth incomplete / catalogue-sync-plex-admin OUVERTS" claim is **stale and wrong** at this HEAD). Every JSON router is mounted with `Depends(verify_backend_secret)`, the AI router carries a module-level `verify_api_key`, key-management is master-only, `/admin` + `/docs` + `/openapi.json` are behind fail-closed HTTP Basic Auth, and secret/password comparisons use `secrets.compare_digest`. Per-user keys are stored as SHA-256 digests (never plaintext), shown once. No P0 (no auth bypass, no injection, no unauthenticated destructive/write endpoint) was found.

What keeps this off a 4.5 is one genuine **P1 post-auth arbitrary-filesystem-write** (`POST /api/plex/generate` accepts an unconfined client `outputDir`) plus a cluster of P2 hardening gaps: an **unauthenticated `/metrics`** on a "public tunnel", **plaintext Xtream passwords + unencrypted DB backups at rest**, **the TV-pairing at-rest key derived from the API bearer secret** (key reuse), **zero rate-limiting / brute-force protection anywhere**, **admin CSRF**, and **post-auth SSRF** via user-supplied Xtream/image URLs.

Finding count: **1 × P1**, **7 × P2**, **2 × debt**.

---

### CR-S01 — Authenticated arbitrary filesystem write / path traversal via `POST /api/plex/generate` (P1)

**Where:** `app/api/plex.py:35-58` → `app/plex_generator/storage.py:99-196`. Router mounted with the shared guard at `app/main.py:405` (`dependencies=_guard`).

**What:** The request body field `output_dir` (camel `outputDir`) is taken verbatim and turned into a filesystem root with **no confinement, allow-listing, or `resolve()`+prefix check**:

```python
# app/api/plex.py
output_dir = req.output_dir or settings.PLEX_LIBRARY_DIR   # :39
output = Path(output_dir)                                   # :45
storage = DryRunStorage() if req.dry_run else LocalStorage(output)  # :56
```

`LocalStorage._resolve` is a bare join (`self.base_dir / rel_path`, `storage.py:105-106`), so `base_dir` is fully attacker-controlled. Running `generate()` then, under that root:
- `mkdir` `Films/` and `Series/` and title sub-dirs (`storage.py:22`, `140`);
- writes `.strm` files whose content is the **Xtream stream URL including `username`/`password`** (`storage.py:108-110`, url from `build_stream_url`, `stream_service.py:44`), plus `.nfo` XML and downloaded `.jpg` images;
- `prune_orphan_dirs` performs `shutil.rmtree(title_dir, ignore_errors=True)` on generator-owned title folders under `<outputDir>/Films|Series` (`storage.py:170-196`).

The endpoint is authenticated, but the guard (`verify_backend_secret`, `deps.py:59`) accepts **any active per-user key** from the `api_keys` table — i.e. a low-trust "catalogue consumer" credential minted for the Android app, not just the master secret. A read-only catalogue key should not grant server-side filesystem write.

**Impact:** A holder of any valid API key can (a) create attacker-named `.strm`/`.nfo`/`.jpg` files at arbitrary writable paths (network shares, web-served dirs, `~/.config`, cron drop dirs on the container/host), (b) **exfiltrate other accounts' Xtream credentials to disk** by writing populated `.strm` files to a readable location, and (c) delete generator-owned directory trees under the chosen root. Also a trivial disk-fill DoS.

**Exploit scenario:** With a per-user key, `POST /api/plex/generate {"outputDir":"/var/www/html","dryRun":false}`. The server writes `Films/<title>/<title>.strm` (containing `http://panel/movie/USER/PASS/123.ts`) under the web root; the attacker then fetches `/Films/.../*.strm` over HTTP to harvest every synced account's IPTV credentials. On Windows/Docker mounts the same works against any writable share the process can reach.

**Fix direction:** Ignore the client `output_dir` entirely and always generate under `settings.PLEX_LIBRARY_DIR` (server config), OR confine it: `resolve()` the candidate, require it to be a child of an operator-configured allow-list root, reject on mismatch (`Path.is_relative_to`). Additionally, gate this endpoint behind `verify_master_key` (it is an operator/admin action, not a per-user one).

---

### CR-S02 — `/metrics` is unauthenticated on the public tunnel (P2)

**Where:** `app/utils/metrics.py:46-51` — `Instrumentator(...).instrument(app).expose(app, endpoint="/metrics")`; no dependency attached. Mounted at `app/main.py:442`.

**What:** `/metrics` is a bare route with no `verify_backend_secret`/Basic-Auth guard, unlike the rest of the surface. `app/main.py:374-379` explicitly frames the deployment as a "public tunnel" and disables default docs for that reason — yet Prometheus scrape output is wide open. It exposes per-endpoint request counts/latencies/status classes (path enumeration of the whole API) plus business labels including `account_id` (`metrics.py:17,36` — 8-char account identifiers), `enrichment_queue_size` by status, and `streams_alive_ratio` per account.

**Impact:** Unauthenticated operational reconnaissance: attacker learns which endpoints exist and are exercised, request volumes/error rates, account identifiers, and catalogue/health internals — a map for further attack and a privacy leak of internal identifiers.

**Exploit scenario:** `curl https://<public-tunnel>/metrics` returns the full Prometheus exposition with account IDs and endpoint inventory, no credentials needed.

**Fix direction:** Put `/metrics` behind Basic Auth (`verify_admin_basic_auth`) or the backend secret, or restrict it to the internal network / scrape sidecar and never route it through the public tunnel. Avoid high-cardinality PII-ish labels (`account_id`) on publicly reachable metrics.

---

### CR-S03 — Xtream account passwords stored in plaintext at rest (and in unencrypted backups) (P2) — **RÉSOLU (2026-07-11)**

> **Statut : résolu.** `XtreamAccount.password` est désormais mappé via un `TypeDecorator` SQLAlchemy transparent (`app/utils/crypto_fields.py::EncryptedString`) qui chiffre en Fernet à l'écriture et déchiffre à la lecture — **aucun site d'appel modifié** (ORM `session.add`/`select`, **et** le bulk Core `update(XtreamAccount).values(password=...)` utilisé par `PUT /api/accounts/{id}`, vérifiés tous deux transparents). Résolution de clé : `settings.XTREAM_ENCRYPTION_KEY` (dédiée, recommandée prod) → sinon dérivée de `AI_API_KEY` (tag de séparation de domaine distinct de la dérivation tv-auth de `payload_crypto.py`, donc pas la même clé) → sinon **fail-open documenté** (texte clair, comme avant, pour ne jamais bloquer la création de compte/sync — un warning est loggé). **Migration 016** (`app/db/migrations.py::_migration_016_encrypt_xtream_passwords`) chiffre en place les lignes `xtream_accounts.password` pré-existantes en clair, idempotente (skip si déjà un token Fernet `gAAAAA…`, skip si vide, no-op si aucune clé configurée). Aucun accès SQL brut (`text()`) à `xtream_accounts.password` trouvé ailleurs que dans cette migration elle-même (grep confirmé sur `sync_worker.py`, `categories.py`, `main.py`, `live.py`, `deps.py`, `xtream_service.py`, `stream_service.py`, `accounts.py` — tous en accès ORM). Preuve : `tests/test_xtream_cred_encryption.py` (5 tests, verts) — colonne chiffrée en base brute, lecture ORM en clair, fail-open sans clé, migration 016 chiffre + idempotente, valeur déjà chiffrée non re-chiffrée. Boot vérifié (`tests/test_api_health.py` vert + dry-run `init_db()` réel sur fichier SQLite : migrations 001→016 OK, deuxième `run_migrations()` idempotente sans erreur). **Résiduel documenté** : si ni `XTREAM_ENCRYPTION_KEY` ni `AI_API_KEY` n'est configuré, les mots de passe restent en clair (fail-open volontaire, cf. docstring `crypto_fields.py`) ; une rotation de `AI_API_KEY` sans clé dédiée rend les lignes déjà chiffrées indéchiffrables (documenté `.env.example` + docstrings) — une clé `XTREAM_ENCRYPTION_KEY` dédiée reste recommandée en prod.

**Where (avant fix) :** `app/models/database.py:136` (`password = Column(Text, nullable=False)`); written plaintext at `app/api/accounts.py:72`, `app/main.py:163`; snapshotted by the online-backup cron (`app/main.py:309-324` → `app/scripts/backup_db`) to `BACKUP_DIR` with no encryption.

**What:** IPTV provider credentials are persisted verbatim in SQLite and copied into `.backup` snapshots. They are correctly **omitted from `AccountResponse`** (`schemas.py:282-303` has no `password`) and not logged — good — but any read of the DB file or a backup yields every provider password in clear. They also propagate into stream URLs returned to authenticated clients and into `.strm` files on disk (see CR-S01).

**Impact:** DB/backup exposure (stolen volume, misplaced backup, container image with data dir, path-traversal read) discloses all upstream IPTV credentials in cleartext.

**Exploit scenario:** An attacker who obtains a nightly `.backup` from `BACKUP_DIR` (or the `plexhub.db` file) runs `SELECT username,password FROM xtream_accounts` and reuses/ resells the accounts.

**Fix direction:** Encrypt the `password` column at rest with a dedicated key (e.g. Fernet with a key that is NOT the API bearer secret — see CR-S04), or at minimum encrypt the backup artifacts and lock down `BACKUP_DIR` permissions. Document the residual exposure for self-hosted operators.

---

### CR-S04 — TV-pairing at-rest encryption key derived from the API bearer secret (key reuse) (P2)

**Where:** `app/utils/payload_crypto.py:34-47`. When `TV_AUTH_ENCRYPTION_KEY` is unset (the documented default path, `config.py:35`), the Fernet key is `urlsafe_b64encode(sha256(AI_API_KEY))`.

**What:** `AI_API_KEY` is simultaneously (a) the master **bearer token** sent as `X-API-Key` on every request and embedded in the Android app, and (b) the seed for the "encryption at rest" of pairing payloads (which carry Plex tokens / config). The confidentiality of the at-rest blob therefore reduces to the secrecy of a widely-distributed shared secret. Anyone who knows `AI_API_KEY` (any app user, anyone who observes one request if TLS is stripped/mis-terminated, an operator with the env) can decrypt any captured `tv_auth_sessions.payload_encrypted`.

**Impact:** The "encrypted at rest" guarantee for Plex tokens is only as strong as a bearer secret that is deliberately spread to every client — weak defense-in-depth; a DB leak (CR-S03) plus knowledge of the master key yields the plaintext Plex payloads.

**Exploit scenario:** Attacker with a copy of the DB and the app's configured `AI_API_KEY` derives the Fernet key and decrypts historical `payload_encrypted` rows that were not yet scrubbed (approved-but-not-completed sessions).

**Fix direction:** Require a dedicated, independently-generated `TV_AUTH_ENCRYPTION_KEY` (fail-closed 503 if absent, as it already does for a totally missing key) and stop deriving crypto material from the auth bearer secret. Rotate keys independently of the API secret.

---

### CR-S05 — No rate limiting / brute-force protection anywhere (P2)

**Where:** No limiter middleware exists (grep for `limiter|slowapi|RateLimit` → none). Auth guards `deps.py:59-140` have no attempt-throttling/lockout. Unauthenticated `POST /api/tv-auth/start` (`tv_auth.py:178-241`) creates a DB row per call.

**What:** (a) `X-API-Key` (master + per-user) and `/admin` Basic-Auth accept unlimited guesses. Constant-time compare closes the timing channel but not online guessing — if an operator picks a weak `AI_API_KEY`/`ADMIN_PASSWORD` (both are free-form env values, `config.py:23,29`), they are brute-forceable over the public tunnel. (b) `tv-auth/start` is unauthenticated and each call does a DELETE-scan + INSERT with up-to-5 retries; opportunistic cleanup only removes sessions expired > 1h ago (`tv_auth.py:59,197-199`), so a flood inflates the DB and churns the SQLite writer with no ceiling.

**Impact:** Online credential brute-force; unauthenticated DB-bloat / writer-contention DoS.

**Exploit scenario:** A script hammers `POST /api/tv-auth/start` thousands of times per second, growing `tv_auth_sessions` and competing for the single SQLite write lock, degrading sync/enrichment writes; separately, a dictionary attack against `X-API-Key` proceeds unthrottled.

**Fix direction:** Add IP-based rate limiting (reverse-proxy or `slowapi`) on unauthenticated endpoints and on auth failures; add exponential backoff / temporary lockout on repeated 401s; cap pending `tv-auth` sessions per IP and shorten the cleanup grace.

---

### CR-S06 — CORS default `*` origin + `*` methods/headers (P2)

**Where:** `app/main.py:382-387` (`allow_origins=settings.CORS_ORIGINS`, `allow_methods=["*"]`, `allow_headers=["*"]`); default `CORS_ORIGINS=["*"]` at `config.py:61-63`.

**What:** Wildcard CORS. The saving grace is that auth is header-based (`X-API-Key`) and `allow_credentials` is **not** enabled (defaults False), so a cross-origin site cannot read authenticated responses (it has no key and the browser won't auto-attach the custom header) — the practical exposure is therefore limited. It remains a hardening gap: it advertises the API to any origin, allows unauthenticated cross-origin reads of the public endpoints (`/api/health`, `tv-auth/*`, `/metrics`), and would become dangerous the moment cookie/credential auth or `allow_credentials=True` is ever added.

**Impact:** Defense-in-depth gap; latent foot-gun if the auth model changes; broad browser reachability of public endpoints.

**Fix direction:** Set an explicit `CORS_ORIGINS` allow-list in production (the app's real origins), and constrain `allow_methods`/`allow_headers` to what the client actually uses. Keep `allow_credentials=False`.

---

### CR-S07 — CSRF on the `/admin` UI (Basic-Auth + form POST, no token) (P2)

**Where:** `app/api/admin.py` state-changing routes: `POST /admin/keys` (`:346`), `POST /admin/keys/{id}/revoke` (`:378`), `POST /admin/movies/{rk}/ids` (`:161`), `POST /admin/movies/{rk}/rescrape` (`:209`), `POST /admin/import-nfo` (`:245`). Guarded only by HTTP Basic Auth (`main.py:412`).

**What:** These POSTs accept `application/x-www-form-urlencoded` (a CORS "simple" content type) and rely solely on browser-cached Basic-Auth credentials — there is no CSRF token and Basic Auth carries no SameSite protection. A logged-in admin who visits a malicious page can have a cross-site auto-submitting form fire these actions with their cached credentials attached.

**Impact:** Cross-site forgery of admin actions: mint/revoke API keys, rewrite media external IDs, trigger NFO imports — without the attacker knowing the admin password.

**Exploit scenario:** Admin authenticated to `/admin` in their browser opens attacker-controlled page containing `<form action="https://host/admin/keys" method=POST><input name=label value=pwn>...</form>` that auto-submits; a new API key is minted (the plaintext is returned in the response the victim's browser renders, but even blind, revocation/ID-tampering succeed).

**Fix direction:** Add a per-session CSRF token (or a double-submit cookie) to admin forms, and/or move admin behind a first-party session cookie with `SameSite=Strict` instead of Basic Auth; require re-auth for key creation.

---

### CR-S08 — Post-auth SSRF via user-supplied Xtream URLs and image download URLs (P2)

**Where:** Xtream: `app/services/xtream_service.py:38-63` builds the request URL from account `base_url`/`port` supplied at account creation (`accounts.py:37-64`) and used by sync + stream validation (`health_check_worker.py:136`). Images: `app/plex_generator/storage.py:49-56` uses an httpx client with `follow_redirects=True` on `image_url` values sourced from TMDB/DB/NFO.

**What:** Account creation (authenticated) lets a caller point `base_url` at any host/port; the server then issues requests to it — a classic SSRF into the internal network / cloud metadata endpoints. Blind for the Xtream JSON path (response is parsed as Xtream data, and the Xtream client does not follow redirects), but reachability alone (connect/timing/port-scan oracle) is meaningful. The image-download path additionally **follows redirects**, widening the SSRF for URLs influenced via NFO import / DB fields.

**Impact:** Server-side requests to `169.254.169.254`, `localhost`, and RFC1918 hosts from within the deployment; internal port scanning; potential metadata reachability. Requires a valid API key, and connecting to arbitrary panels is partly the product's purpose, so severity is bounded.

**Exploit scenario:** `POST /api/accounts {"baseUrl":"http://169.254.169.254","port":80,...}` then observe timing/error to probe the metadata service; or seed an NFO with an `image_url` that 302-redirects to an internal host and let the generator fetch it.

**Fix direction:** Validate/deny link-local, loopback, and private IP ranges (resolve then check) for account `base_url` and for image downloads; disable redirects on the image client or re-validate the redirect target; consider an egress allow-list.

---

### CR-S09 — Verbose error/echo surfaces: upstream exception text, client-controlled request-id, public health counts (debt)

**Where:** `app/api/accounts.py:64,184` (`f"Authentication failed: {e}"`, `f"Connection test failed: {e}"` returned to client); `app/utils/request_context.py:22,28` (client `X-Request-ID` echoed back and injected into every log line, unbounded, no charset limit); `app/api/health.py:29-39` (public endpoint returns version + account/media/broken-stream counts).

**What:** Small information-disclosure / log-hygiene gaps. Raw upstream exception strings can leak internal URLs/library details to the (authenticated) caller. The unbounded client-supplied request-id is written into log records (`[%(request_id)s]`) — CRLF log-forging is blocked by ASGI header parsing, but oversized/bracket-laden ids degrade log readability and bloat. Public `/health` discloses catalogue size and version to anyone.

**Impact:** Minor recon aid and log-quality erosion; none individually exploitable.

**Fix direction:** Return generic client-facing messages and log the detail server-side; cap/sanitize the accepted `X-Request-ID` length/charset (or always mint server-side); consider trimming `/health` to `status`/`version` for unauthenticated callers.

---

## What's healthy (verified from code)

- **Fail-closed on the entire JSON API.** All of `accounts`/`categories`/`live`/`media`/`stream`/`sync`/`plex` are mounted with `dependencies=_guard` = `[Depends(verify_backend_secret)]` (`main.py:396-405`); missing/invalid key → 401 (`deps.py:59-68`). The house-doc claim that these are "OUVERTS" is **stale**. (Empirically re-confirmed this session: `/api/media/movies` with no key → 401.)
- **AI router** carries a module-level `verify_api_key` (`ai.py:52-56`) = same auth + sqlite-vec 503 gate (`deps.py:71-85`).
- **Key management** (`/api/admin/keys`) is master-only via `verify_master_key` (`api_keys.py:20-24`, `deps.py:88-102`) — a per-user key cannot mint/revoke other keys. Correct privilege separation.
- **Constant-time secret comparison everywhere it matters:** master secret (`_is_master`, `deps.py:42-46`), master-key guard (`deps.py:98`), and **both** admin username+password evaluated before branching to avoid a username-timing oracle (`deps.py:130-139`). No `==` on any raw secret.
- **Per-user keys store only a SHA-256 digest** (`api_key_service.py:31-32,58-74`); plaintext (`phk_` + `secrets.token_urlsafe(32)`, ~256-bit) is returned exactly once (`api_keys.py:50-52,84-88`). Lookup is by hashed digest, so the SQL `==` is acceptable (timing on a hash prefix does not recover the pre-image token). Revocation/expiry enforced in `is_active` (`api_key_service.py:40-46`).
- **`AI_API_KEY` empty ⇒ fail-closed:** `_is_master` returns False on empty master (`deps.py:44`), so with no per-user keys the whole API returns 401; `verify_master_key` returns 503 (`deps.py:93-97`). Bricked, not bypassed.
- **`ADMIN_PASSWORD` empty ⇒ fail-closed 503** for `/admin`, `/docs`, `/openapi.json` (`deps.py:118-122`); default password is empty, so docs/admin are locked by default.
- **Docs surface hardened:** default `docs_url`/`redoc_url`/`openapi_url` are `None` (`main.py:377-379`); `/docs` and `/openapi.json` re-exposed only behind the same Basic Auth (`main.py:423-430`). Not publicly advertised.
- **TV pairing capability model is sound:** `deviceCode` = `secrets.token_urlsafe(32)` (`tv_auth.py:201`), unguessable and not logged; `/approve` requires the backend secret (`tv_auth.py:248-252`); payload is Fernet-encrypted at rest, delivered exactly once (`payload_delivered` flag, `tv_auth.py:314-331`) and scrubbed on complete/expire (`tv_auth.py:169,365`).
- **No SQL injection:** all queries use SQLAlchemy Core/ORM with bound params; the only dynamic SQL builds `:idN` **placeholder names** (not values) with a params dict (`ai.py:210-217`), and `text()` PRAGMA uses an internal constant (`nfo_import_service.py:80`). LIKE wildcards escaped in `live.py:60-63`.
- **Secrets not logged:** TMDB key truncated to 4 chars (`config.py:118`); boot summary is explicitly sanitized (`main.py:212-223`); no password/stream-URL logging found. `.env` is gitignored (only `.env.example` tracked).
- **`admin/import-nfo` uses the server-side `settings.PLEX_LIBRARY_DIR`, not a client path** (`admin.py:256,279`) — no traversal there (contrast CR-S01, which is the plex-generate path).
