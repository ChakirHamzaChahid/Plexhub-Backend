# CR — Security

**Dimension score: 38 / 100**

Rationale: cryptographic primitives and the tv-auth flow are competently built, but the entire catalog/mutation/admin surface ships unauthenticated with attacker-controlled filesystem write, no rate limiting, wildcard CORS, plaintext credential storage, and internal-error leakage — a single network-reachable adversary can wipe data, DoS the server, and write files anywhere on disk.

**Threat model.** All probes below were run live against `uvicorn` (HEAD `3c8beef`, harness with a no-op lifespan because `import fcntl` at `app/main.py:196` aborts startup on the non-POSIX host). Reachability: anyone who can reach the HTTP port. Only `/api/ai/*` and `POST /api/tv-auth/approve` enforce `X-API-Key`; everything else — `accounts` (create/update/delete), `sync/*`, `plex/generate`, `categories`, `media`, `stream`, `live`, `/admin` — is fully open. The most dangerous reachable assets are: stored Xtream credentials (plaintext at rest, exfiltrable URL-side), the on-disk filesystem (write-anywhere via `plex/generate`), and the background-worker pool (unbounded trigger = DoS).

---

## CR-S01 — Catalog/mutation/admin API ships with no authentication (broken access control)

- **Severity: P0**
- **Risk:** CVSS ~9.1 (AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H) — network, no privileges, full integrity/availability loss.
- **Evidence:**
  - Auth is wired only to the AI router (`app/api/ai.py:40` module-level `dependencies=[Depends(verify_api_key)]`) and to one tv-auth route (`app/api/tv_auth.py:270`). No other router declares a dependency.
  - `app/main.py:369-380` mounts `accounts`, `categories`, `live`, `media`, `stream`, `sync`, `plex`, `health`, and `admin` with no auth dependency.
  - Mutating handlers with no auth: `POST/PUT/DELETE /api/accounts` (`app/api/accounts.py:36,97,124`), all of `app/api/sync.py` (`:11,23,33,42,51,60`), `POST /api/plex/generate` (`app/api/plex.py:34`), `PUT /api/accounts/{id}/categories` (`app/api/categories.py:43`).
  - **Live proof:** `POST /api/sync/xtream/all` → `202 {"jobId":"sync_all_..."}`; `POST /api/sync/full-pipeline` → `202`; `GET /admin` → `200`; `POST /api/plex/generate` → `200`; all with no header.
- **Attack scenario:** An unauthenticated caller deletes every account and all derived media (`DELETE /api/accounts/{id}` cascades deletes across `Media`, `EnrichmentQueue`, `XtreamCategory`, `LiveChannel`, `EpgEntry` — `accounts.py:140-155`), or registers a malicious account, or repeatedly triggers full pipelines.
- **Root cause:** Auth was scoped to AI/pairing only; the rest of the API was never gated. CLAUDE.md §3 documents this as intentional, but it is a shipping-state vulnerability regardless of intent.
- **Fix:** Apply a global auth dependency (FastAPI `app`-level `dependencies=[...]` or per-router) requiring `X-API-Key` on every state-changing and data-exposing route; keep only `/api/health` and `/api/tv-auth/start|status|complete` (device-flow, deviceCode-bearer) open. Add an allowlist for read endpoints if the Android client cannot send the key.

---

## CR-S02 — `POST /api/plex/generate` is an unauthenticated arbitrary-filesystem-write primitive

- **Severity: P0**
- **Risk:** CVSS ~8.6 (AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:H) — write-anywhere with no path containment.
- **Evidence:**
  - `app/api/plex.py:38` `output_dir = req.output_dir or settings.PLEX_LIBRARY_DIR` — the caller-supplied `outputDir` is taken verbatim, `Path(output_dir)` (`plex.py:44`), passed to `LocalStorage(account_output)` (`plex.py:74,80`).
  - `app/plex_generator/storage.py:97-98` `_resolve` is `self.base_dir / rel_path` with **no containment / `is_relative_to` check**; `_atomic_write_bytes` does `target.parent.mkdir(parents=True, exist_ok=True)` (`storage.py:17`) on the resolved absolute path.
  - **Live proof:** `POST /api/plex/generate {"accountId":"nonexistent123","outputDir":"c:/tmp/plexhub-pwn","dryRun":false}` → `200`, and the directory `c:/tmp/plexhub-pwn/nonexistent123/` was created on disk by the server.
- **Attack scenario:** With any synced account (or after `CR-S01` lets the attacker create one), the attacker sets `outputDir` to a sensitive location and the generator writes attacker-influenced `.strm`/`.nfo`/`.jpg` content (titles flow from Xtream into filenames and `.strm` body = stream URL). At minimum it is a remote `mkdir`/disk-fill DoS; combined with controllable content it can clobber files in writable system paths.
- **Root cause:** No authentication (CR-S01) plus no allowlist/containment on the destination directory; `output_dir` should never be client-controlled.
- **Fix:** Authenticate the endpoint; drop `output_dir` from the request body entirely (use server-side `PLEX_LIBRARY_DIR`), or validate it against a configured allowlist root and assert `resolved.is_relative_to(allowed_root)` before any write.

---

## CR-S03 — Unauthenticated triggers of background pipelines = trivial resource-exhaustion DoS

- **Severity: P1**
- **Risk:** CVSS ~7.5 (AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H).
- **Evidence:**
  - `app/api/sync.py` exposes `POST /xtream`, `/xtream/all`, `/enrichment`, `/validate-streams`, `/full-pipeline`, each calling `create_background_task(...)` with no auth, no concurrency cap, no rate limit (`sync.py:16,28,47,56,78`).
  - `create_background_task` holds strong refs but does not bound concurrent task count (`app/utils/tasks.py`).
  - No rate-limiting middleware exists anywhere (grep for `slowapi|ratelimit|throttle` across `app/` returns only TMDB external-429 handling and metrics).
  - **Live proof:** repeated `POST /api/sync/full-pipeline` each returned `202` and spawned a fresh pipeline task.
- **Attack scenario:** An attacker fires hundreds of `/full-pipeline` and `/sync/xtream/all` requests; each launches sync→enrichment→validation→Plex-gen, saturating the httpx pools, TMDB quota, CPU, and the SQLite writer, plus the embedding cold-start path — denial of service for legitimate clients.
- **Root cause:** No auth, no rate limiting, no job de-duplication/quota on manual triggers.
- **Fix:** Authenticate these routes (CR-S01); add a global rate limiter; de-duplicate by job name (refuse if a `sync_all`/`full_pipeline` task is already running).

---

## CR-S04 — Xtream account passwords stored in plaintext at rest

- **Severity: P1**
- **Risk:** CVSS ~6.5 (local/at-rest confidentiality of third-party IPTV credentials).
- **Evidence:**
  - `app/models/database.py:117` `password = Column(Text, nullable=False)` — no encryption.
  - Written verbatim in `app/api/accounts.py:72` (`password=body.password`) and `app/main.py:161` (env auto-provision).
  - Contrast: the much-lower-value tv-auth payload **is** encrypted at rest with Fernet (`app/api/tv_auth.py:301`), so the asymmetry is clearly an oversight, not a deliberate trade-off.
- **Attack scenario:** Anyone with read access to `plexhub.db` (the unencrypted SQLite file, its WAL, or any of the daily `.backup` snapshots in `BACKUP_DIR`) recovers every IPTV username/password in cleartext. The DB backup job copies these plaintext secrets to a second location daily (`app/scripts/backup_db.py`).
- **Root cause:** No application-level encryption for the `password` column; the existing Fernet helper (`utils/payload_crypto.py`) was not reused.
- **Fix:** Encrypt `XtreamAccount.password` with the existing Fernet helper (or a dedicated key), decrypt only when building stream/API URLs; ensure backups inherit the encryption. Restrict DB/backup file permissions.

---

## CR-S05 — Internal exception strings leaked to clients (information disclosure)

- **Severity: P2**
- **Risk:** CVSS ~5.3 (low-confidentiality leak of internals; aids further attacks).
- **Evidence:**
  - `app/api/accounts.py:64` `HTTPException(400, f"Authentication failed: {e}")` and `:184` `f"Connection test failed: {e}"`.
  - `app/api/categories.py:40,69,72,155` `HTTPException(..., detail=str(e))`.
  - **Live proof:** `POST /api/accounts` with a bad port returned `{"detail":"Authentication failed: Invalid port: '9999:9999'"}` — raw internal parsing error echoed to the client.
- **Attack scenario:** An attacker probes these endpoints to map internal logic, library/parsing behavior, and (on DB/network errors) potentially file paths or connection strings embedded in exception text, aiding targeted exploitation.
- **Root cause:** Exception objects are interpolated directly into the HTTP `detail`. Combined with no auth, the leak is reachable by anyone.
- **Fix:** Return a generic client-facing message (e.g. "Xtream authentication failed"); log the full `e` server-side with the `request_id`. Add a global exception handler so unhandled errors never surface stack/internal detail.

---

## CR-S06 — Wildcard CORS reflected for any origin

- **Severity: P2** (P1 in any browser-fronted deployment)
- **Risk:** CVSS ~5.4. Mitigated to "read-only" today because credentials are header-based, not cookies, and `allow_credentials` is unset (so `*` cannot be combined with cookies) — but every unauthenticated endpoint is browser-readable cross-origin.
- **Evidence:**
  - `app/config.py:55-57` `CORS_ORIGINS` defaults to `["*"]`; `app/main.py:358-363` `allow_origins=settings.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"]`.
  - **Live proof:** preflight `OPTIONS /api/accounts` with `Origin: https://evil.example` → `200` + `access-control-allow-origin: *` + `access-control-allow-methods: DELETE,GET,...,POST,PUT`; a plain `GET /api/health` with attacker Origin also returned `access-control-allow-origin: *`.
- **Attack scenario:** Any malicious website a victim visits can issue cross-origin `fetch()` to the (unauthenticated) catalog/account/sync APIs and read responses, harvesting account metadata (label, base_url, username, server_url, expiration) and triggering syncs/deletes if the user's browser can reach the backend (e.g. LAN-hosted server, DNS-rebinding).
- **Root cause:** Default `*` with wildcard methods/headers; CLAUDE.md §9 flags "explicit in prod" but the shipped default is wide-open.
- **Fix:** Set an explicit origin allowlist (no `*`), restrict `allow_methods`/`allow_headers` to what the Android/web client needs, and never combine `*` with `allow_credentials=True` if cookies are introduced.

---

## CR-S07 — `/metrics` exposed unauthenticated

- **Severity: P2**
- **Risk:** CVSS ~4.3 (info disclosure / recon).
- **Evidence:** `app/utils/metrics.py` instruments via `prometheus-fastapi-instrumentator`; `app/main.py:387-388` mounts it. **Live proof:** `GET /metrics` → `200` with full Python/GC/process metrics and per-endpoint HTTP latency/volume.
- **Attack scenario:** An attacker reads request volumes, endpoint latencies, process uptime/PID-class data, and business gauges (`plexhub_streams_alive_ratio`, `plexhub_enrichment_queue_size`), enabling reconnaissance and capacity/timing inference.
- **Root cause:** Metrics endpoint has no auth and no network restriction.
- **Fix:** Bind `/metrics` to localhost / an internal interface, or require auth, or scrape via a sidecar on a non-public port.

---

## CR-S08 — No brute-force protection on tv-auth `approve` userCode

- **Severity: P2**
- **Risk:** CVSS ~4.0. Strongly mitigated by `X-API-Key` on approve (`tv_auth.py:270`) and decent userCode entropy, but no per-code attempt limit exists.
- **Evidence:**
  - `approve` looks up by normalized `user_code` (`tv_auth.py:286-290`) with no attempt counter, lockout, or rate limit; userCode is 8 chars over a 30-symbol alphabet (`tv_auth.py:55-56`) ≈ 30^8 ≈ 6.6e11 space, TTL 900 s.
  - Because approve already requires the shared secret, an attacker who can call approve is already trusted — so the realistic risk is low; flagged for defense-in-depth.
- **Attack scenario:** A compromised/leaked `AI_API_KEY` plus brute force could hijack a pending pairing session within its 15-min window; without the key it is not reachable.
- **Root cause:** No attempt accounting on pairing-code validation.
- **Fix:** Add a per-deviceCode/per-userCode attempt counter that expires the session after N failed approve attempts; add global rate limiting.

---

## CR-S09 — Stream credentials embedded in URLs returned over (possibly) plain HTTP

- **Severity: dette / P2**
- **Risk:** CVSS ~4.8 (confidentiality of IPTV creds in transit/logs downstream).
- **Evidence:**
  - `app/services/xtream_service.py:179-184,186-191,255-260` build `…/movie/{username}/{password}/{id}.ext` etc.; returned in `StreamResponse.url` by `app/api/stream.py:36` and `app/api/live.py:141-146` to the unauthenticated client.
  - Positive: these URLs are **not** logged at INFO/WARNING (grep confirms only `stream_service.py:70` logs a type, `storage.py:119` logs image URLs — not stream URLs). No HTTPS enforcement in the app; `GZipMiddleware` (`main.py:364`) is enabled.
- **Attack scenario:** Credentials-in-URL is intrinsic to Xtream, but returning them to an unauthenticated endpoint over HTTP exposes them to any on-path observer and to client-side logs/proxies/referrers. Combined with CR-S01 anyone can fetch them.
- **Root cause:** Xtream protocol design + unauthenticated stream endpoints + no transport hardening.
- **Fix:** Authenticate stream endpoints (CR-S01); deploy strictly behind TLS; consider a short-lived signed proxy URL instead of handing raw Xtream credential URLs to clients.

---

## CR-S10 — MD5 used for account identifiers (weak primitive, low security impact)

- **Severity: dette**
- **Risk:** Negligible-direct (identifier, not a secret/auth control), flagged because MD5 should not appear in a security review's clean bill.
- **Evidence:** `app/api/accounts.py:25-27` `hashlib.md5(...).hexdigest()[:8]`; same in `app/main.py:128-130`.
- **Impact:** Account IDs are predictable/collidable (8 hex chars, MD5). Not used as a secret, so impact is low, but truncated MD5 collisions could cause account-ID clashes/enumeration.
- **Fix:** Use `hashlib.sha256` (or `blake2b`) truncated, or a random UUID; reserve MD5 for non-security hashing only.

---

## Dependency / transport notes (best-effort, `requirements.txt`)

- Pins are floor-only (`>=`), so the shipped versions are non-reproducible; a fresh install could pull newer (or, with no upper bound on `fastapi`/`sqlalchemy`/`httpx`, breaking) releases. `cryptography>=42,<46` is adequately recent for Fernet. No obviously CVE-laden pin observed, but the lack of a lockfile (`requirements.txt` floors + no hashes) is a supply-chain weakness — **dette**.
- No security headers set anywhere (no HSTS / X-Content-Type-Options / X-Frame-Options); the `/admin` HTML UI (HTMX) has no CSP and no auth — clickjacking/XSS-amplification surface if exposed. **dette / P2.**

---

## What's solid

- **Constant-time API-key comparison** done correctly with `secrets.compare_digest` on `bytes` in both `app/api/deps.py:42-45` and `app/api/tv_auth.py:80-83`; live probes returned identical `401 "Invalid API key"` for missing and wrong keys. (`compare_digest` safely handles differing lengths.)
- **Fernet payload crypto is sound:** the AI_API_KEY-derived key is a proper 32-byte urlsafe base64 (`base64.urlsafe_b64encode(sha256(key).digest())`, `payload_crypto.py:43-44`) — correct length and format for `Fernet`; explicit `TV_AUTH_ENCRYPTION_KEY` preferred; missing key → clean `None` → 503. Settings read at call time so no import-time key capture.
- **tv-auth device-flow is well-built:** `deviceCode` = `secrets.token_urlsafe(32)` (unguessable, `tv_auth.py:219`); `userCode` from `secrets.choice` over an unambiguous alphabet (`:154-155`); payload delivered exactly once (`payload_delivered` flag, `:332-344`), scrubbed on complete and on expiry (`:187,383`); TTL enforced lazily (`:177-189`). `approve` correctly auth-gated (live 401).
- **Secrets hygiene at the repo/log boundary:** `.env` is gitignored (only `.env.example` tracked — verified via `git ls-files`); TMDB key truncated in logs (`config.py:76`); the boot summary is sanitized (`main.py:210-221`); `AccountResponse` deliberately omits `password` (`schemas.py:169-189`) — passwords are never returned in API responses. Stream URLs are not logged.
- **SQL injection: not found.** All queries use SQLAlchemy ORM/Core with bound parameters; the only `ilike` filters escape `\ % _` before use (`media_service.py:47,50-51`). No raw `text()` built from user input.
- **XML/NFO injection: mitigated** — NFO built via `xml.etree.ElementTree` `SubElement(...).text = value`, which auto-escapes (`plex_generator/nfo_builder.py`). Path components from titles are stripped of `\ / : * ? " < > |` and trailing dots (`naming.py:5-19`), so traversal-via-title is effectively neutralized (a bare `..` title collapses to `"Unknown"`).
- **SSRF surface is bounded:** server-side fetches (xtream, health-check, image download) target URLs derived from stored account `base_url`, not free-form per-request user input; httpx clients use explicit timeouts and bounded connection pools (`xtream_service.py:21-28`, `health_check_worker.py:27-33`, `storage.py:48-51`).
