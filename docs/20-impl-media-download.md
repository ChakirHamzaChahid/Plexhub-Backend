# Impl Spec — Téléchargement physique de médias (backend)

> Auteur : tech-lead · Source : `docs/10-prd-media-download.md` (CPO) + `docs/20-architecture.md`/`CLAUDE.md` §2/§3/§5/§9.
> Portée : **feature-scoped**. Complète (ne remplace pas) `docs/22-impl-spec-backend.md`. Migration en tête = **017 → 018**.
> **Additif & rétrocompatible** : n'altère ni `/api/media`, ni `/api/plex`, ni la génération `.strm`, ni les onglets admin existants.
> ADR lié : `docs/architecture/adr/0002-media-download-writes-to-disk.md`.

Cette spec **fige les contrats** (signatures, schéma DB, chemins de routes, forme des templates). Les ICs codent
dessus en parallèle sur des **périmètres de fichiers disjoints** (cf. `docs/31-board.md`). Toute ambiguïté résiduelle
est marquée `# TBD` avec une note ; il n'en reste aucune bloquante à l'ouverture du sprint.

---

## 0. Décisions d'architecture (fermes)

1. **Dossier dédié `DOWNLOAD_DIR`**, distinct de `PLEX_LIBRARY_DIR`. Le catalogue `.strm` est inchangé. C'est la
   **première** capacité qui écrit les octets vidéo sur disque.
2. **Destination 100 % serveur-side.** Le client ne fournit **jamais** de chemin. Le chemin est dérivé du titre/saison/épisode,
   **sanitizé segment par segment**, puis **confiné** sous `DOWNLOAD_DIR` par vérification `realpath` (invariant testé, F-007).
   C'est l'anti-thèse explicite de la dette CR-S01 (`outputDir` verbatim de `POST /api/plex/generate`).
3. **Worker master-only.** Le drain de la file ne tourne que sur le processus **master** (élection `fcntl.flock`, `main.py:229-234`),
   comme le pipeline sync/enrich/plex. Les routes admin (enqueue/cancel/retry) tournent sur **n'importe quel** worker uvicorn ;
   elles n'écrivent que des **lignes DB** — le master les draine. → **communication master↔slaves via l'état SQLite partagé**,
   donc le drain est un **poll DB** (pas d'event mémoire cross-process).
4. **Version choisie par l'opérateur** (`serverId:ratingKey`). Série « complète » = tous les épisodes de la source choisie
   (`scope=series_all`). Pas de sélection saison/épisode fine au MVP.
5. **Reprise `Range` via `.part`.** Écriture dans `<dest>.part`, **rename atomique** (`os.replace`) vers le fichier final au
   succès seulement. Un `.part` survit à un cancel/échec pour reprise.
6. **`run_with_retry` sur TOUT writer** (request-path enqueue/cancel/retry **et** worker progress/terminal) — on n'ajoute pas la
   dette CR-C04.
7. **Jamais l'URL Xtream en clair** dans les logs, les erreurs, la DB ou l'API (elle contient `user/password` dans le path).
   L'URL est **re-dérivée** à chaque exécution depuis le compte stocké — **jamais persistée**.

---

## 1. Layout des fichiers (périmètres disjoints)

Nouveaux fichiers (aucune réécriture d'un fichier chaud ; `media_service.py` **n'est pas touché**) :

| Fichier | Rôle | Lot / owner |
|---|---|---|
| `app/models/database.py` (append) | Entités ORM `DownloadJob`, `DownloadBatch` | **db-migration-specialist** |
| `app/db/migrations.py` (append) | `_migration_018_create_download_tables` (fin de chaîne) | **db-migration-specialist** |
| `app/models/schemas.py` (append) | `DownloadJobResponse`, `DownloadJobListResponse`, `DownloadEnqueueRequest` | **db-migration-specialist** |
| `app/config.py` (append) | `DOWNLOAD_*` | **backend-developer (lot service)** |
| `app/services/download_service.py` (new) | logique métier : enqueue, compute_dest_path, primitive `download_to_disk`, list/cancel/retry | **backend-developer (lot service)** |
| `app/workers/download_worker.py` (new) | drain master-only, reprise boot, progression, auto-retry | **backend-developer (lot service)** |
| `app/api/admin_downloads.py` (new) | router HTMX `/admin/downloads` (Basic Auth) | **backend-developer (lot routes)** |
| `app/api/downloads.py` (new) | router JSON `/api/admin/downloads` (P1 lecture, `verify_master_key`) | **backend-developer (lot routes)** |
| `app/templates/admin/downloads.html` + `_downloads_*.html` (new) | page + fragments | **backend-developer (lot routes)** |
| `app/templates/admin/base.html` (edit 1 ligne) | lien de nav « Télécharger » | **backend-developer (lot routes)** |
| `app/main.py` (edit) | mount des 2 routers + start worker (master) | **backend-developer (lot wiring)** — **seul** éditeur de `main.py` |
| `tests/test_download_*.py` (new) | tests unitaires + intégration | **qa-engineer** |

Règle de non-collision : `main.py` a **un seul** éditeur (lot wiring, en dernier). `schemas.py`/`models/database.py`/`migrations.py`
sont édités **uniquement** par le lot racine (db-migration-specialist), en **append**.

---

## 2. Config (`app/config.py`, lues via `os.getenv`/`_safe_int`)

À ajouter dans `class Settings` (mêmes conventions que l'existant) :

```python
# Physical media download (feature "Télécharger") — separate from PLEX_LIBRARY_DIR.
DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "")           # "" = feature disabled (config guard)
DOWNLOAD_CONCURRENCY: int = _safe_int("DOWNLOAD_CONCURRENCY", 1)
DOWNLOAD_CHUNK_BYTES: int = _safe_int("DOWNLOAD_CHUNK_BYTES", 1_048_576)      # 1 MiB
DOWNLOAD_MAX_RETRIES: int = _safe_int("DOWNLOAD_MAX_RETRIES", 3)             # transient auto-retries
DOWNLOAD_MIN_FREE_DISK_MB: int = _safe_int("DOWNLOAD_MIN_FREE_DISK_MB", 2048)  # préflight (P1)
DOWNLOAD_POLL_INTERVAL: int = _safe_int("DOWNLOAD_POLL_INTERVAL", 2)         # worker drain poll (s)
DOWNLOAD_CONNECT_TIMEOUT: int = _safe_int("DOWNLOAD_CONNECT_TIMEOUT", 30)    # httpx connect (s)
DOWNLOAD_READ_TIMEOUT: int = _safe_int("DOWNLOAD_READ_TIMEOUT", 120)         # httpx read/chunk (s)
```

`DOWNLOAD_DIR` **non défini** = feature désactivée : l'enqueue renvoie un fragment/erreur explicite et crée **0 job** (garde
analogue à `PLEX_LIBRARY_DIR` dans `admin.py::admin_import_nfo_run`). Le worker log `download disabled` et no-op si vide.

---

## 3. Schéma DB — migration 018 (owner : `db-migration-specialist`, **Risky / needs-approval**)

Deux tables **additives**, `CREATE TABLE/INDEX IF NOT EXISTS`, ajoutées **en fin** de `run_migrations()` (après 017). Aucune
DDL destructive. Une DB fraîche les a déjà via `Base.metadata.create_all` → la migration est un no-op silencieux là, et une DB
upgradée les obtient ici (même invariant que 017, `migrations.py:707`).

### 3.1 `download_batch` (regroupe une série complète — 1 batch = N jobs)

| Colonne | Type SQLite | Notes |
|---|---|---|
| `id` | TEXT PK | uuid4 hex |
| `media_type` | TEXT NOT NULL | `movie` \| `show` (type de la sélection) |
| `unification_id` | TEXT | back-nav vers le titre |
| `title` | TEXT NOT NULL | titre nettoyé (affichage) |
| `server_id` | TEXT NOT NULL | source choisie |
| `rating_key` | TEXT NOT NULL | `vod_*` (film) / `series_*` ou show rk (série) |
| `scope` | TEXT NOT NULL | `movie` \| `series_all` |
| `total_jobs` | INTEGER NOT NULL DEFAULT 0 | nb de jobs créés |
| `created_at` | BIGINT NOT NULL | epoch ms |

> Un film crée aussi un `download_batch` (total_jobs=1) pour uniformiser l'affichage groupé — ou `batch_id=NULL` accepté
> (le film est alors un job isolé). **Décision figée : film ⇒ `batch_id=NULL`** (pas de batch pour 1 job) ; série ⇒ 1 batch.

### 3.2 `download_job`

| Colonne | Type SQLite | Notes |
|---|---|---|
| `id` | TEXT PK | uuid4 hex |
| `batch_id` | TEXT NULL | → `download_batch.id` (pas de FK dure ; NULL pour un film) |
| `server_id` | TEXT NOT NULL | compte source (`xtream_<id>`) |
| `rating_key` | TEXT NOT NULL | `vod_{id}.{ext}` (film) / `ep_{id}.{ext}` (épisode) |
| `media_type` | TEXT NOT NULL | `movie` \| `episode` (maille job = fichier) |
| `unification_id` | TEXT NULL | pour regroupement/affichage |
| `title` | TEXT NOT NULL | titre nettoyé |
| `season` | INTEGER NULL | épisode seulement |
| `episode` | INTEGER NULL | épisode seulement |
| `dest_path` | TEXT NOT NULL | **relatif** à `DOWNLOAD_DIR` (jamais absolu, jamais client) |
| `state` | TEXT NOT NULL DEFAULT `'queued'` | `queued`\|`running`\|`completed`\|`failed`\|`canceled` |
| `bytes_total` | BIGINT NULL | NULL si pas de `Content-Length` amont |
| `bytes_done` | BIGINT NOT NULL DEFAULT 0 | octets écrits (progression persistée) |
| `error` | TEXT NULL | message **borné**, jamais l'URL |
| `attempts` | INTEGER NOT NULL DEFAULT 0 | auto-retries transitoires consommés |
| `created_at` | BIGINT NOT NULL | epoch ms |
| `updated_at` | BIGINT NOT NULL | epoch ms (bump à chaque transition/progress) |
| `started_at` | BIGINT NULL | 1ʳᵉ transition→running |
| `finished_at` | BIGINT NULL | completed/failed/canceled |

Index :

```
CREATE INDEX IF NOT EXISTS ix_download_job_state    ON download_job(state);
CREATE INDEX IF NOT EXISTS ix_download_job_batch    ON download_job(batch_id);
CREATE INDEX IF NOT EXISTS ix_download_job_created  ON download_job(created_at);
CREATE INDEX IF NOT EXISTS ix_download_job_item     ON download_job(server_id, rating_key);  -- dédup enqueue
```

`ix_download_job_state` sert le drain (`WHERE state='queued'`) et la reprise boot (`WHERE state='running'`).
`ix_download_job_item` sert la dédup idempotente d'enqueue. **Pas** de contrainte unique sur `(server_id, rating_key)` : un
job `completed` doit pouvoir être re-enfilé plus tard.

### 3.3 Entités ORM (`app/models/database.py`, append, style Composite-PK/BigInteger existant)

`DownloadJob` et `DownloadBatch` en miroir exact des tables ci-dessus, avec `__table_args__` portant les 4 index (mêmes noms/colonnes
que la migration → convergence create_all ⇆ migration, cf. la house-law CR-C05/CR-P02).

---

## 4. Schémas Pydantic v2 (`app/models/schemas.py`, append, camelCase `to_camel`)

```python
class DownloadJobResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    job_id: str = Field(alias="id")          # expose "jobId"
    batch_id: Optional[str] = None
    type: str                                # movie|episode
    unification_id: Optional[str] = None
    title: str
    season: Optional[int] = None
    episode: Optional[int] = None
    server_id: str
    rating_key: str
    state: str
    bytes_done: int = 0                       # -> bytesDownloaded ? voir note
    bytes_total: Optional[int] = None
    percent: Optional[float] = None           # calculé (voir builder)
    speed_bps: Optional[float] = None         # calculé (voir builder)
    dest_path: str
    error: Optional[str] = None
    retries: int = Field(alias="attempts")    # expose "retries"
    created_at: int
    updated_at: int
    started_at: Optional[int] = None
    finished_at: Optional[int] = None

class DownloadJobListResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    items: list[DownloadJobResponse]
    total: int

class DownloadEnqueueRequest(BaseModel):     # JSON P2 seulement (HTMX passe par Form)
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    type: str                                 # movie|show
    unification_id: str
    server_id: str
    rating_key: str
    scope: str                                # movie|series_all
```

Contrat de nommage figé (aligné PRD §8) : `DownloadJobResponse` sérialise
`{ jobId, batchId?, type, unificationId?, title, season?, episode?, serverId, ratingKey, state, bytesDownloaded,
bytesTotal?, percent?, speedBps?, destPath, error?, retries, createdAt, updatedAt, startedAt?, finishedAt? }`.

> **Note `bytesDownloaded`** : le PRD expose `bytesDownloaded`. `to_camel(bytes_done)` = `bytesDone`. **Décision figée** : le champ
> Python s'appelle `bytes_downloaded` (⇒ alias camel `bytesDownloaded`) et lit la colonne ORM `bytes_done` via
> `Field(alias=...)` **non** — préférer un **builder explicite** `to_download_response(job)` (fonction dans `schemas.py` ou
> `download_service.py`) qui mappe colonne→schéma et **calcule** `percent`/`speedBps`. `from_attributes` n'est donc **pas** utilisé
> pour ces champs dérivés. Le builder :
> - `percent = round(bytes_done / bytes_total * 100, 1)` si `bytes_total` else `None` ;
> - `speed_bps = bytes_done / max(1, (updated_at - started_at)/1000)` si `state=='running'` et `started_at` else `None`
>   (débit **moyen** ; aucune colonne de vitesse persistée).

Le builder est le **seul** point qui produit un `DownloadJobResponse` (HTMX rend directement l'ORM + un helper Jinja pour
percent ; JSON passe par le builder). Voir §6.4.

---

## 5. `download_service.py` — signatures figées (async, sans dépendance FastAPI)

Couche **service** : logique métier pure + accès DB via session passée en argument (routes) ou `async_session_factory` (worker).
Aucun `HTTPException` ici (les routes mappent).

### 5.1 Exceptions

```python
class DownloadDisabledError(RuntimeError): ...        # DOWNLOAD_DIR non défini
class PathConfinementError(ValueError): ...           # destination hors DOWNLOAD_DIR
class DownloadCanceled(Exception): ...                # cancel coopératif
class DownloadPermanentError(Exception): ...          # 404/403/CT invalide → failed direct
class DownloadTransientError(Exception): ...          # réseau/5xx → auto-retry
```

### 5.2 Résolution de chemin (confinement — F-007, cœur sécu)

```python
def _sanitize_segment(name: str, *, fallback: str) -> str:
    """Un segment de chemin sûr : NFC, retire séparateurs/contrôles/'..', trim '. ', cap 180 chars."""

def compute_dest_path(*, media_type: str, title: str, year: int | None,
                      season: int | None, episode: int | None,
                      ext: str, is_adult: bool = False) -> str:
    """Chemin de destination RELATIF à DOWNLOAD_DIR (jamais absolu).
    Movies:  Movies/<[XXX] ?><Titre (Année)>/<[XXX] ?><Titre (Année)>.<ext>
    Series:  Series/<Titre>/Season NN/<Titre> - SxxEyy.<ext>
    Chaque segment passe par _sanitize_segment. `ext` = ext parsée du rating_key sinon 'ts'.
    `is_adult` applique apply_adult_prefix au dossier + fichier film (P2 — helper prêt, défaut non appliqué au MVP)."""

def resolve_confined(rel_path: str) -> Path:
    """Retourne le chemin ABSOLU confiné, ou lève PathConfinementError.
    base = Path(DOWNLOAD_DIR).resolve(strict=False)
    resolved = Path(os.path.realpath(base / rel_path))
    if resolved != base and base not in resolved.parents: raise PathConfinementError
    return resolved
    (DOWNLOAD_DIR vide -> DownloadDisabledError)"""
```

`compute_dest_path` **sanitize** (défense) ; `resolve_confined` **prouve** le confinement (invariant testé). Les deux sont
appelés : compute au moment de l'enqueue (stocke `dest_path` relatif), resolve au moment de l'écriture (worker).

### 5.3 Primitive de transfert

```python
@dataclass
class DownloadResult:
    bytes_downloaded: int
    bytes_total: int | None
    already_present: bool   # dest final déjà là (skip-if-exists, Q1)
    resumed: bool           # repris via Range depuis un .part

async def download_to_disk(
    url: str,
    dest: Path,                          # chemin ABSOLU confiné (sortie de resolve_confined)
    *,
    on_progress: Callable[[int, int | None], Awaitable[None]] | None = None,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
    chunk_bytes: int = settings.DOWNLOAD_CHUNK_BYTES,
) -> DownloadResult:
    """GET streaming httpx vers `<dest>.part`, promotion atomique au succès.

    - Skip-if-exists : si `dest` existe (taille>0) et pas de `.part` -> return already_present=True.
    - Reprise : si `<dest>.part` existe (taille n>0) -> requête `Range: bytes=n-`.
        * 206 -> append ('ab'), bytes_total = n + Content-Range total ;
        * 200 (Range ignoré) -> truncate .part, restart 'wb' ;
        * 416 -> .part déjà complet -> promotion.
    - UA = settings.XTREAM_USER_AGENT ; follow_redirects=True (dérivé serveur, pas d'input client -> pas de nouvelle
      surface SSRF vs stream-validation existante).
    - timeouts httpx : connect=DOWNLOAD_CONNECT_TIMEOUT, read=DOWNLOAD_READ_TIMEOUT.
    - Boucle: async for chunk in resp.aiter_bytes(chunk_bytes): f.write(chunk); bytes_done += len(chunk);
        appelle on_progress(bytes_done, bytes_total) (throttlé côté worker) ; à intervalle régulier
        await cancel_check() -> si True: close, LAISSE le .part, raise DownloadCanceled.
    - Fin OK: f.flush(); await asyncio.to_thread(os.replace, part, dest)  (rename ATOMIQUE même FS).
    - mkdir parents + os.replace + stat espace disque -> asyncio.to_thread (I/O syscalls hors boucle).
    - Erreurs: 404/403/Content-Type non-média -> DownloadPermanentError ; Timeout/ConnectError/5xx ->
      DownloadTransientError. AUCUN message ne contient `url` (creds)."""
```

> **Invariant creds** : `download_to_disk` reçoit `url` déjà construite ; elle ne la **log** ni ne la **remonte** dans aucune
> exception. Les logs mentionnent `job_id`/`dest`, jamais l'URL. (house-law §9 pt secrets + PRD §9.)

### 5.4 Enqueue (résout sélection → 1..N jobs)

```python
@dataclass
class EnqueueResult:
    jobs: list[DownloadJob]
    batch_id: str | None
    error: str | None          # message utilisateur (fragment) ; jobs=[] si error

async def enqueue_selection(
    db: AsyncSession, *,
    media_type: str,           # movie|show
    unification_id: str,
    server_id: str,
    rating_key: str,
    scope: str,                # movie|series_all
) -> EnqueueResult:
    """1. DOWNLOAD_DIR vide -> EnqueueResult(jobs=[], error='DOWNLOAD_DIR n'est pas défini').
    2. Résout le compte via parse_server_id(server_id) -> XtreamAccount actif ; absent -> error.
    3. scope=movie:
        - charge la ligne Media (server_id, rating_key) -> title/year/is_adult/ext ;
        - compute_dest_path(...) ; dédup : si un job non-terminal existe déjà pour (server_id, rating_key) -> le renvoie ;
        - crée 1 DownloadJob(state='queued', batch_id=None).
    4. scope=series_all:
        - charge le show Media (server_id, rating_key=show rk) -> title ;
        - énumère les épisodes: SELECT Media WHERE type='episode' AND server_id=? AND grandparent_rating_key=? ;
          (requête locale au service — media_service.py N'EST PAS modifié) ;
        - 0 épisode -> EnqueueResult(jobs=[], error='aucun épisode disponible') (jamais 500) ;
        - crée 1 download_batch + N DownloadJob (state='queued', batch_id=batch, season=parent_index, episode=index).
    5. Tous les writes via run_with_retry(db.commit) (CR-C04). Retourne les jobs créés/existants."""
```

L'URL directe n'est **pas** construite ici (elle l'est au worker, non persistée). L'enqueue ne stocke que `server_id`/`rating_key`/`dest_path`.

### 5.5 Lecture / mutation (request-path, via `run_with_retry`)

```python
async def list_jobs(db, *, states: list[str] | None = None,
                    limit: int = 200, offset: int = 0) -> tuple[list[DownloadJob], int]: ...
async def get_job(db, job_id: str) -> DownloadJob | None: ...

async def cancel_job(db, job_id: str) -> DownloadJob | None:
    """queued -> canceled (immédiat). running -> canceled (le worker abandonne au prochain cancel_check,
    laisse le .part). Terminaux -> no-op. UPDATE conditionnel WHERE id AND state IN ('queued','running'),
    commit via run_with_retry."""

async def retry_job(db, job_id: str) -> DownloadJob | None:
    """failed|canceled -> queued : reset error=None, updated_at=now (attempts CONSERVÉ pour ne pas boucler
    infiniment ? -> décision: retry MANUEL remet attempts=0). Le .part est conservé -> reprise Range au run.
    run/queued/completed -> no-op. commit via run_with_retry."""

async def clear_finished(db) -> int:
    """DELETE download_job WHERE state IN ('completed','failed','canceled'). Renvoie le nb supprimé.
    (F-104 P1). Les batches orphelins peuvent être nettoyés ou laissés (no-op sûr)."""
```

> **Décision retry vs attempts** : `attempts` compte les **auto-retries transitoires** consommés par le worker. Un **retry
> manuel** (`retry_job`) **remet `attempts=0`** — l'opérateur demande explicitement un nouveau cycle complet.

---

## 6. `download_worker.py` — drain master-only

Coroutine longue durée démarrée **uniquement sur le master**, dans le `if is_master:` du lifespan (§8). Bornée par
`DOWNLOAD_CONCURRENCY`. Ne bloque jamais la boucle. Toutes les écritures via `run_with_retry`.

### 6.1 Signatures

```python
async def reap_orphans(session_factory) -> int:
    """Boot (master) : UPDATE download_job SET state='queued', updated_at=now WHERE state='running'.
    Le transfert de l'instance précédente est mort -> pas de 'running' fantôme (F-005/F-006 boot). Renvoie le nb repris."""

async def run_drain_loop(session_factory) -> None:
    """Boucle master-only :
      - au démarrage: await reap_orphans(...) ;
      - Semaphore(DOWNLOAD_CONCURRENCY) ; set de tâches en vol ;
      - toutes DOWNLOAD_POLL_INTERVAL s : si des slots libres, SELECT les prochains 'queued'
        (ORDER BY created_at) jusqu'à combler les slots, et pour chacun create_background_task(_run_job(...)) ;
      - s'arrête proprement à l'annulation (shutdown lifespan)."""

async def _run_job(session_factory, job_id: str, sem: asyncio.Semaphore) -> None:
    """1. async with sem:
       2. CLAIM atomique : UPDATE ... SET state='running', started_at=COALESCE(started_at,now), updated_at=now
          WHERE id=:id AND state='queued' (run_with_retry). rowcount==0 -> déjà pris/annulé -> return.
       3. Recharge le job ; résout compte -> url = build_stream_url(account, rating_key) (NON persistée) ;
          dest = resolve_confined(job.dest_path).
       4. on_progress = _persist_progress (throttlé >=1s) ; cancel_check = _is_canceled (relit state en DB).
       5. result = await download_service.download_to_disk(url, dest, on_progress=..., cancel_check=...).
          - succès/already_present -> UPDATE state='completed', finished_at, bytes_* WHERE id AND state='running'.
          - DownloadCanceled -> no-op terminal (cancel a déjà écrit 'canceled') ; .part conservé.
          - DownloadPermanentError -> UPDATE state='failed', error=_safe_error(...), finished_at WHERE id AND state='running'.
          - DownloadTransientError -> attempts+1 ; si attempts <= DOWNLOAD_MAX_RETRIES :
                await asyncio.sleep(min(2**attempts, 30)) ; UPDATE state='queued' (re-drainé) ;
              sinon UPDATE state='failed', error=_safe_error(...).
       Toutes les transitions terminales sont CONDITIONNELLES `WHERE id AND state='running'` -> un cancel concurrent gagne."""
```

### 6.2 Modèle de concurrence / annulation (cross-process, via DB)

- **Claim** : `UPDATE ... WHERE id AND state='queued'` → un seul draineur (master) ; `rowcount` confirme.
- **Progress** : `UPDATE ... SET bytes_done, bytes_total, updated_at WHERE id` — **ne touche pas `state`** (ne peut pas
  écraser un `canceled`).
- **Cancel** (route, potentiellement sur un slave) : `UPDATE ... SET state='canceled' WHERE state IN('queued','running')`.
  Le worker le détecte via `cancel_check` = *relire `state`* : si `!= 'running'` → `DownloadCanceled`, `.part` laissé intact.
- **Terminal** : `... WHERE id AND state='running'` → si le cancel a déjà mis `canceled`, le `completed`/`failed` affecte 0
  ligne et le cancel est honoré.

Ce modèle **traverse les process** (tout passe par SQLite) sans event mémoire — cohérent avec le master-only.

### 6.3 `_safe_error(exc)`

Mappe une exception → message court (`"upstream 404"`, `"network timeout"`, `"disk full"`, `"canceled"`). **Jamais** `str(exc)`
s'il peut contenir l'URL. Cap ~200 chars. C'est ce qui est stocké dans `download_job.error` et rendu à l'API.

### 6.4 Sérialisation

Le builder `to_download_response(job)` (§4) est utilisé par le router JSON. Le HTMX rend l'ORM directement + un filtre Jinja
`percent`/`speed` (ou un petit helper contextuel) — pas de duplication de la logique de calcul (extraire un helper pur
`compute_percent(job)`/`compute_speed_bps(job)` réutilisé des deux côtés).

---

## 7. Contrats de routes

### 7.1 HTMX admin — `app/api/admin_downloads.py` (prefix `/admin/downloads`, Basic Auth au mount)

Même style que `admin.py` : `Jinja2Templates`, fragments HTML, `Depends(get_db)`. **Aucune** logique métier (délègue à
`download_service`).

| Verbe + chemin | Réponse | Rôle | Délègue à |
|---|---|---|---|
| `GET /admin/downloads` | 200 HTML | page onglet + panneau file | `media_service.get_unified_list` (lecture existante) |
| `GET /admin/downloads/list?type=&search=&page=&page_size=` | 200 fragment | liste unifiée films/séries | `media_service.get_unified_list` |
| `GET /admin/downloads/{type}/{unification_id}/versions` | 200 fragment / 404 | versions d'un titre | `media_service.get_unified_group` (movie) / `get_unified_episodes` (show) |
| `POST /admin/downloads` (Form: `type,unification_id,server_id,rating_key,scope`) | 200 fragment / 422 | enqueue | `download_service.enqueue_selection` |
| `GET /admin/downloads/queue` | 200 fragment | file + progression (polling `hx-trigger="every 2s"`) | `download_service.list_jobs` |
| `POST /admin/downloads/{job_id}/cancel` | 200 fragment | annuler | `download_service.cancel_job` |
| `POST /admin/downloads/{job_id}/retry` | 200 fragment | relancer | `download_service.retry_job` |
| `POST /admin/downloads/clear-finished` | 200 fragment | nettoyer l'historique (P1) | `download_service.clear_finished` |

Gardes/erreurs :
- `DOWNLOAD_DIR` vide sur `POST /admin/downloads` → **200** fragment d'erreur, **0** job (US-003.3). Pas de 5xx.
- Sélection invalide (compte/version introuvable) → **200** fragment avec message (l'HTMX préfère un fragment lisible ; le
  status 422 reste possible sur validation Pydantic de forme). `unification_id` inconnu sur `/versions` → **404**.
- La page réutilise **exactement** `get_unified_list`/`get_unified_group`/`get_unified_episodes` (lecture, non modifiées) →
  contenu identique à `/api/media/{movies,shows}/unified`.

Les **versions** rendues affichent `label`, `serverId`, `ratingKey`, `isBroken` (issus de `aggregation_service.build_versions`).
Film : 1 bouton « Télécharger » (`scope=movie`) par version. Série : par version, un bouton « Série complète (toutes saisons) »
(`scope=series_all`).

### 7.2 JSON admin — `app/api/downloads.py` (prefix `/api/admin/downloads`, `verify_master_key` module-level)

Même montage que `api_keys.py` (Pattern C : self-prefix + `dependencies=[Depends(verify_master_key)]` sur l'`APIRouter`).

| Verbe + chemin | Réponse | Prio |
|---|---|---|
| `GET /api/admin/downloads?state=&limit=&offset=` | 200 `DownloadJobListResponse` / 401 | **P1** |
| `GET /api/admin/downloads/{job_id}` | 200 `DownloadJobResponse` / 404 / 401 | **P1** |
| `POST /api/admin/downloads` (`DownloadEnqueueRequest`) | 202 `DownloadJobResponse[]` / 422 / 401 | P2 |
| `POST /api/admin/downloads/{job_id}/{cancel,retry}` | 200 `DownloadJobResponse` / 404 / 401 | P2 |

MVP = **P1 lecture** (miroir QA/automatisation). P2 mutation = même délégation que le HTMX. Réponses **Pydantic v2** typées
(jamais de dict nu). `job_id` inconnu → 404 ; sans clé maître → 401.

### 7.3 Wiring `main.py` (lot wiring, **seul** éditeur)

```python
from app.api import admin_downloads, downloads  # noqa
# Basic Auth comme /admin :
app.include_router(admin_downloads.router, dependencies=[Depends(verify_admin_basic_auth)])
# Pattern C (self-guarded verify_master_key) comme api_keys :
app.include_router(downloads.router)
```

Et dans le `if is_master:` du lifespan (après `scheduler.start()` / le lancement du sync initial) :

```python
from app.workers import download_worker
from app.db.database import async_session_factory
create_background_task(download_worker.run_drain_loop(async_session_factory), name="download_worker")
```

`create_background_task` est déjà annulé proprement au shutdown (`cancel_all_background_tasks`, `main.py:366-367`) → pas de
nouvelle plomberie de cycle de vie.

---

## 8. Templates (Jinja2, à créer sous `app/templates/admin/`)

- `downloads.html` (extends `base.html`) : barre recherche/filtre (`type=movie|show`, `search`), zone liste
  (`hx-get="/admin/downloads/list"`), panneau file (`hx-get="/admin/downloads/queue" hx-trigger="load, every 2s"`).
- `_downloads_list.html` : cartes unifiées (1 par titre, `versionCount`), chaque carte `hx-get=".../versions"` vers un tiroir.
- `_downloads_versions.html` : lignes de versions (`label`/`serverId`/`ratingKey`/`isBroken`) + formulaires POST enqueue.
- `_downloads_queue.html` : table des jobs (état, barre `percent`, `bytesDownloaded/bytesTotal`, `speedBps`) + boutons
  cancel/retry/clear-finished. `bytesTotal=null`/`percent=null` tolérés (afficher octets cumulés, jamais 500).

`base.html` (edit 1 ligne, lot routes) — ajouter dans `<nav>` :

```html
<a href="/admin/downloads" class="text-slate-300 hover:text-white">Télécharger</a>
```

---

## 9. Conformité aux pièges (house-law §9) — checklist figée

| Piège | Application dans cette feature |
|---|---|
| Master-only (`fcntl.flock` POSIX, §9.7) | `run_drain_loop` démarré **uniquement** dans `if is_master:` ; enqueue possible sur tout worker (écrit DB) |
| `run_with_retry` (§9.8, CR-C04) | **tous** les writers : enqueue, cancel, retry, clear, claim, progress, transitions terminales, reap boot |
| `asyncio.to_thread` (§9.11) | `os.replace` (rename atomique), `mkdir parents`, `stat` espace disque, `os.path.realpath` si besoin — jamais sur la boucle. Les writes de chunk (buffered, page cache) restent inline comme l'I/O acceptable existant |
| Migration idempotente en fin de chaîne (§9.6) | 018 = `CREATE TABLE/INDEX IF NOT EXISTS`, après 017, no-op sur DB fraîche |
| Secrets jamais loggés (§9.10) | URL Xtream (creds dans le path) **jamais** loggée/persistée/renvoyée ; `_safe_error` borne les messages ; logs = `job_id`/`dest` |
| SafeRotatingFileHandler / gardes maison | non touchés |
| DDL destructif = needs-approval | 018 purement additive → pas de destructif ; ticket **Risky** quand même (schéma) |

---

## 10. Stratégie de tests (pytest, owner qa-engineer)

`tests/test_download_*.py`, **pytest-asyncio mode auto** (`async def test_*`), HTTP amont **mocké via respx** (jamais de réseau
réel). DB SQLite éphémère (`conftest.py`). Couvrir service + validation (unitaire) + endpoint (intégration `httpx.AsyncClient`/
`TestClient`). Tout nouveau comportement = un test.

Cas figés (mappés PRD §5) :
1. **Confinement (F-007, invariant sécu)** : `compute_dest_path` + `resolve_confined` avec titres hostiles (`../`, séparateurs,
   unicode, noms réservés) → **0** chemin résolu hors `DOWNLOAD_DIR` ; `PathConfinementError` levée. **Test bloquant.**
2. **Enqueue film** : 1 job `queued` persisté, `dest_path` relatif attendu ; dédup (2ᵉ enqueue même item → même job).
3. **Enqueue série** : N jobs = N épisodes, 1 batch ; série sans épisode → 0 job + message (pas de 500).
4. **Garde `DOWNLOAD_DIR` vide** : enqueue → 0 job + fragment/erreur.
5. **`download_to_disk` nominal** (respx) : `.part` → `os.replace` → fichier final présent ; `on_progress` monotone ;
   `bytesTotal=None` toléré (pas de `Content-Length`).
6. **Reprise `Range`** (respx 206 vs 200) : `.part` partiel + serveur `Range` → repart de `n` ; serveur sans `Range` → restart.
7. **Skip-if-exists** : dest présent → `already_present`, pas de re-download.
8. **Cancel** : `queued`→`canceled` (jamais démarré) ; `running`→`canceled` via `cancel_check`, `.part` conservé, jamais promu.
9. **Auto-retry** : erreur transitoire (respx 5xx) → `attempts` monte jusqu'à `DOWNLOAD_MAX_RETRIES` puis `failed`, `error`
   **ne contient pas l'URL**.
10. **Reap boot** : `running` pré-existant → `queued` après `reap_orphans` (pas de fantôme).
11. **Routes HTMX** : `GET /admin/downloads` 200 ; sans Basic Auth → 401. `GET /queue` rend les états. `POST cancel/retry` 200.
12. **Route JSON P1** : `GET /api/admin/downloads` 200 `DownloadJobListResponse` ; sans clé maître → 401 ; job inconnu → 404.
13. **Non-régression** : suite existante verte, `/api/health` 200, `/api/media/*` inchangés (additif).

---

## 11. Walkthrough — F-003 « enqueue film → worker → disque » (bout-en-bout)

**(a) DB / entité** — `download_job` (§3.2) + `DownloadJob` ORM. Un job = un fichier. Colonnes d'état/progression persistées.

**(b) service — enqueue** — `enqueue_selection(db, media_type='movie', unification_id, server_id, rating_key='vod_435071.mkv',
scope='movie')` : garde `DOWNLOAD_DIR`, résout `XtreamAccount` via `parse_server_id`, charge la ligne `Media`, calcule
`dest_path = compute_dest_path(media_type='movie', title=..., year=..., ext='mkv')` → `Movies/Terminator (1984)/Terminator (1984).mkv`,
dédup contre un job non-terminal existant, crée 1 `DownloadJob(state='queued')`, `commit` via `run_with_retry`. **Aucune URL construite.**

**(c) route — HTMX** — `POST /admin/downloads` (Basic Auth) → parse le Form → `enqueue_selection` → rend `_downloads_queue.html`
avec le nouveau job `queued`. `DOWNLOAD_DIR` vide → fragment d'erreur, 0 job.

**(d) worker — master-only** — `run_drain_loop` (poll 2s) voit le `queued`, `_run_job` **claim** (`state='running'`), résout
`url = build_stream_url(account, 'vod_435071.mkv')` (contient user/pass, **jamais** loggée), `dest = resolve_confined(dest_path)`
(prouve le confinement), puis `download_to_disk(url, dest, on_progress=_persist_progress, cancel_check=_is_canceled)` :
`.part` streaming, `on_progress` bump `bytes_done`/`bytes_total`/`updated_at` (throttlé, `run_with_retry`), `os.replace` atomique
au succès → `UPDATE state='completed' WHERE id AND state='running'`.

**(e) suivi** — `GET /admin/downloads/queue` (polling) rend `state`/`percent`/`speedBps` depuis la DB ; `GET /api/admin/downloads`
(JSON, `verify_master_key`) renvoie `DownloadJobListResponse` pour QA. Le fichier final apparaît sous
`DOWNLOAD_DIR/Movies/Terminator (1984)/…` ; le catalogue `.strm` (`PLEX_LIBRARY_DIR`) est **inchangé**.

---

## 12. `# TBD` résolus (aucun bloquant)

- **`bytesDownloaded` vs `bytesDone`** → résolu : builder explicite `to_download_response` + champ `bytes_downloaded` (alias
  camel), pas de `from_attributes` sur les dérivés (§4).
- **Batch pour un film** → résolu : film ⇒ `batch_id=NULL` ; série ⇒ 1 `download_batch` (§3.1).
- **`attempts` au retry manuel** → résolu : `retry_job` remet `attempts=0` (§5.5).
- **`speedBps`** → résolu : débit **moyen** calculé, aucune colonne persistée (§4/§6.4).
- **`ext` du fichier** → résolu : ext parsée du `rating_key`, sinon `'ts'` (cohérent avec `build_stream_url`) (§5.2).
- **Préfixe `[XXX]` dossiers adultes (P2)** → helper prêt dans `compute_dest_path(is_adult=)`, **non appliqué au MVP**.

---

## Handoff

```
NEXT:
- tech-manager: docs/20-impl-media-download.md + docs/31-board.md prêts ; safe de spawner le pod.
  Spine série: PH-DL-01 (migration+ORM+schemas) -> PH-DL-03 (service+config) -> PH-DL-04 (worker) -> PH-DL-06 (wiring).
  Parallèle: PH-DL-05 (routes+templates) dès PH-DL-01 (contrats service figés ici) ; PH-DL-07 (tests) plan dès maintenant.
- db-migration-specialist: PH-DL-01 (Risky/needs-approval : migration 018 additive).
- qa-engineer: plan de test sur les Given/When/Then §5 du PRD (confinement, reprise Range, cancel, garde config).
- security-reviewer: focus PH-DL-03/04/05 (écriture FS confinée + creds Xtream dans les URLs).
```
</content>
</invoke>
