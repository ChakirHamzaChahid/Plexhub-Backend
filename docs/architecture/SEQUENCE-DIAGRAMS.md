# PlexHub Backend — Diagrammes de séquence par fonctionnalité

> Cartographie des flux bout-en-bout du backend FastAPI (HEAD `40cc8e9`).
> Source : `app/main.py`, `app/api/*`, `app/workers/*`, `app/services/*`, `app/plex_generator/*`.
> **Docs liées** : `docs/architecture/ARCHITECTURE.md` §6 (description textuelle des mêmes flux + flowchart global) et `CLAUDE.md` §5 (flux clés, autorité de vérité). Ce fichier en est la **vue dynamique** (séquences d'échanges).
>
> ⚠️ Delta vs ARCHITECTURE.md/CLAUDE.md (cartographiés à HEAD `1da2ab9`) : la fonctionnalité **#7 LLM génératif (Ollama `/describe` `/chat`)** est plus récente et n'y figure pas encore — elle est documentée ici (vérifiée dans `app/api/ai.py:493-540`).

Le backend a **8 fonctionnalités** :

| # | Fonctionnalité | Déclencheur | Réf code |
|---|---|---|---|
| 0 | Boot + élection master/worker + pipeline planifié | Démarrage app | `main.py:198,241-320` |
| 1 | Sync Xtream (VOD/séries/épisodes/Live/EPG) | Pipeline / `POST /api/sync` | `workers/sync_worker.py` |
| 2 | Enrichissement TMDB | Pipeline (après sync) | `workers/enrichment_worker.py` |
| 3 | Validation de flux (santé streams) | Pipeline + cron 2h | `workers/health_check_worker.py` |
| 4 | Génération bibliothèque Plex (NFO/arbo) | Pipeline / `POST /api/plex/generate` / CLI | `plex_generator/*` |
| 5 | Recommandations IA (embeddings + cosinus) | `POST /api/ai/rank[-multi]` | `api/ai.py`, `services/recommendation_service.py` |
| 6 | Re-embedding (rebuild vecteurs) | `POST /api/ai/embed/rebuild` | `workers/embedding_worker.py` |
| 7 | LLM génératif (descriptions / chat) | `POST /api/ai/describe`, `/chat` | `api/ai.py`, `services/ollama_service.py` |
| 8 | Appairage TV (device-flow) | `POST /api/tv-auth/*` | `api/tv_auth.py` |

---

## 0. Boot, élection master/worker & pipeline planifié

Plusieurs workers uvicorn démarrent ; un **seul** (le master, élu par `fcntl.flock` POSIX) lance le scheduler. Les autres sont passifs.

```mermaid
sequenceDiagram
    autonumber
    participant U as uvicorn (N workers)
    participant L as lifespan (main.py)
    participant FS as flock(server_start.lock)
    participant DB as init_db (SQLite WAL + sqlite-vec)
    participant SCH as APScheduler
    participant PIPE as Pipeline sync→enrich→validate→plex

    U->>L: startup (par worker)
    L->>DB: init_db() (PRAGMA WAL, migrations 001→009)
    L->>FS: tente fcntl.flock(LOCK_EX|LOCK_NB)
    alt lock acquis → MASTER
        FS-->>L: OK
        L->>SCH: add_job(pipeline, interval=SYNC_INTERVAL_HOURS, max_instances=1)
        L->>SCH: add_job(health_check.run, cron hour=2)
        L->>SCH: add_job(cleanup_stale_epg, cron hour=3)
        L->>SCH: add_job(backup_db, cron hour=BACKUP_HOUR) [si BACKUP_ENABLED]
        L->>SCH: scheduler.start()
        L->>PIPE: initial_sync_then_enrich() (background, non bloquant)
        Note over SCH,PIPE: À chaque intervalle :<br/>sync → enrichment → validation → génération Plex (en série)
    else lock pris → SLAVE
        FS-->>L: OSError (déjà tenu)
        L-->>U: mode passif (sert l'API, pas de scheduler)
    end
    L-->>U: yield (app prête)
    Note over U,L: shutdown → cancel_all_background_tasks() + release flock
```

---

## 1. Sync Xtream

Mirroir incrémental d'un panel Xtream. Lock **par compte**, upsert par `dto_hash`/`content_hash`, nettoyage différentiel.

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Scheduler / POST /api/sync
    participant RA as run_all_accounts()
    participant SA as sync_account(id)
    participant LK as _get_account_lock(id)
    participant XS as xtream_service (player_api.php)
    participant MAP as map_*_to_media/channel
    participant DBW as upsert_*_batch + commit_with_retry
    participant EQ as enqueue_for_enrichment
    participant CLN as differential_cleanup*

    SCH->>RA: lancer sync de tous les comptes actifs
    loop pour chaque compte actif
        RA->>SA: sync_account(account_id)
        SA->>LK: acquire (1 sync par compte à la fois)
        SA->>XS: get_categories() (VOD, séries, Live)
        SA->>XS: get_vod_streams / get_series / get_live_streams / get_epg
        XS-->>SA: DTOs bruts
        loop par catégorie / par lot
            SA->>MAP: DTO → row (calcule dto_hash, content_hash)
            MAP-->>SA: rows Media / LiveChannel / EpgEntry
            SA->>DBW: upsert_media_batch / upsert_live_channels_batch
            Note over DBW: skip si dto_hash inchangé (incrémental)
            DBW-->>SA: insérés / mis à jour
            SA->>EQ: enqueue_for_enrichment(nouveaux movies/séries)
        end
        SA->>CLN: differential_cleanup* (supprime ce qui a disparu du panel)
        SA->>LK: release
    end
    RA-->>SCH: rapport par compte (job en mémoire, get_sync_job)
```

---

## 2. Enrichissement TMDB

Vide la queue d'enrichissement. Phase 1 films, puis Phase 2 séries. Borné par `ENRICHMENT_DAILY_LIMIT` (appels API réels), concurrence 8, 3 tentatives max.

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Scheduler (après sync)
    participant EW as enrichment_worker.run()
    participant Q as EnrichmentQueue (DB)
    participant TS as tmdb_service (singleton + cache TTL)
    participant API as TMDB API
    participant DB as Media (DB)
    participant M as metrics (plexhub_tmdb_requests_total)

    SCH->>EW: run()
    EW->>Q: charge items en attente (movies puis séries)
    loop concurrence=8, jusqu'à ENRICHMENT_DAILY_LIMIT
        EW->>TS: search + details(append_to_response=credits,external_ids)
        alt cache TTL hit
            TS-->>EW: métadonnées (sans appel réseau)
        else cache miss
            TS->>API: GET (TMDB_LANGUAGE=fr-FR)
            API-->>TS: JSON (ids, genres, casting, imdb_id…)
            TS->>M: incrémente compteur TMDB
            TS-->>EW: métadonnées
        end
        EW->>DB: update Media (tmdb_id, imdb_id, genres, overview, poster…)
        alt échec
            Note over EW,Q: retry (MAX_ATTEMPTS=3) sinon marque échoué
        end
    end
    EW-->>SCH: fin (queue drainée ou limite atteinte)
```

---

## 3. Validation de flux (santé des streams)

HEAD puis Range GET (magic bytes). Marque cassé après seuil d'échecs ou échec définitif. Circuit breaker par compte à 90 % d'échecs.

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Pipeline / cron 2h
    participant HC as health_check_worker
    participant DB as Media (streams à vérifier)
    participant H as httpx (HEAD)
    participant G as httpx (Range GET)
    participant CB as Circuit breaker (par compte)
    participant MET as plexhub_streams_alive_ratio

    SCH->>HC: run_pipeline_validation() / run()
    HC->>DB: sélectionne streams (re-check > RECHECK_HOURS)
    loop concurrence=STREAM_VALIDATION_CONCURRENCY (20)
        HC->>H: HEAD url
        alt 404/403 ou Content-Type d'erreur
            H-->>HC: échec DÉFINITIF
            HC->>DB: marque cassé immédiatement
        else HEAD ok
            HC->>G: Range GET (premiers octets)
            G-->>HC: octets
            alt magic bytes valides
                HC->>DB: marque vivant (reset compteur échecs)
            else vide / magic-fail
                HC->>DB: incrémente compteur d'échecs
                Note over HC,DB: cassé après STREAM_BROKEN_THRESHOLD (3)
            end
        end
        HC->>CB: maj taux d'échec du compte
        alt taux ≥ 90 %
            CB-->>HC: OUVRE le circuit (stoppe ce compte — panel down)
        end
    end
    HC->>MET: publie ratio streams vivants
    HC-->>SCH: terminé
```

---

## 4. Génération bibliothèque Plex (NFO + arborescence + images)

Pour chaque compte : lit la DB via `DatabaseSource`, génère NFO + arbo + `.strm` + images. Idempotent (created/updated/deleted/unchanged). Images via pool de 8 threads.

```mermaid
sequenceDiagram
    autonumber
    participant T as Pipeline / POST /api/plex/generate / CLI
    participant SRC as DatabaseSource(account_id)
    participant GEN as PlexLibraryGenerator
    participant NFO as nfo_builder + naming
    participant MAP as MappingStore (JSON)
    participant ST as LocalStorage (écritures atomiques)
    participant POOL as _image_pool (8 threads)
    participant FS as Système de fichiers output/{account_id}

    T->>SRC: charge films/séries/épisodes enrichis (DB)
    SRC-->>GEN: PlexMovie / PlexSeries / PlexEpisode (Pydantic)
    loop par item
        GEN->>MAP: déjà généré ? (diff état précédent)
        alt nouveau / modifié
            GEN->>NFO: construit chemin + XML NFO
            GEN->>ST: écrit .nfo + .strm (atomique)
            GEN->>POOL: télécharge poster/fanart (async via threads)
            POOL->>FS: écrit images
            GEN->>MAP: enregistre l'état (hash)
        else inchangé
            Note over GEN: skip (unchanged)
        end
    end
    GEN->>ST: supprime ce qui n'existe plus (deleted)
    GEN-->>T: GenerationReport (created/updated/deleted/unchanged/errors/duration)
```

---

## 5. Recommandations IA — `POST /api/ai/rank` (et `/rank-multi`)

Ranking par similarité cosinus sur embeddings 384-dim (fastembed + sqlite-vec). Auth `X-API-Key`. 3 motifs de 503 contractuels. Cap 20 hydratations TMDB fraîches par appel.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client (Android)
    participant DEP as verify_api_key (X-API-Key)
    participant R as rank() (ai.py)
    participant RES as _resolve_refs (imdb→tmdb)
    participant TS as tmdb_service.find_by_imdb_id
    participant RS as recommendation_service
    participant VEC as load_cached_vectors (ai_tmdb_cache / vec0)
    participant HY as hydrate_misses (cap 20, timeout 10s)
    participant ES as embedding_service (fastembed ONNX)
    participant RANK as cosine_rank

    C->>DEP: POST /api/ai/rank {ref, candidates, mediaType, limit}
    alt AI_API_KEY absent → 503 "AI service not configured"
        DEP-->>C: 503
    else sqlite-vec non chargé → 503 "AI vector storage unavailable"
        DEP-->>C: 503
    else OK
        DEP->>R: payload validé
        R->>RES: résout ref + candidats
        RES->>TS: imdb→tmdb (ignore épisodes/personnes)
        RES-->>R: tmdb_ids (+ resolutionFailed)
        R->>VEC: vecteurs en cache pour {ref + candidats}
        VEC-->>R: hits + liste des manquants
        R->>HY: hydrate manquants (≤20)
        HY->>ES: embed(textes TMDB frais)
        Note over ES: cold start ~30s au 1er appel (puis singleton)
        alt fastembed KO → EmbeddingUnavailableError
            HY-->>R: lève
            R-->>C: 503 "AI model unavailable"
        else
            ES-->>HY: vecteurs L2-normalisés (surplus/timeouts → dropped)
            HY-->>R: vecteurs hydratés + stats
            R->>RANK: cosine(ref_vec, cand_vecs, limit)
            RANK-->>R: top-N (tmdb_id, score)
            R-->>C: 200 {ranked, cacheHits, cacheMisses, cacheMissesDropped, resolutionFailed}
        end
    end
    Note over C: rank-multi = même flux, ref = centroïde pondéré (1.0,0.9,…min 0.1)
```

---

## 6. Re-embedding asynchrone — `POST /api/ai/embed/rebuild`

Job en mémoire (202 + jobId). **Jamais au boot.** Idempotent : scanne `embedded_at IS NULL`, curseur `tmdb_id`, DELETE-puis-INSERT sur la table virtuelle `vec0`.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client (admin)
    participant API as ai.py /embed/rebuild
    participant W as embedding_worker
    participant J as Jobs (mémoire, JOBS_CAP=100)
    participant DB as ai_tmdb_cache / ai_embeddings (vec0)
    participant ES as embedding_service

    C->>API: POST /api/ai/embed/rebuild
    API->>W: enqueue_rebuild()
    W->>J: crée job (status=running)
    API-->>C: 202 {jobId}
    par tâche de fond
        loop pages de PAGE_SIZE=50, curseur tmdb_id
            W->>DB: SELECT où embedded_at IS NULL AND tmdb_id > :cursor
            W->>ES: embed(textes)
            ES-->>W: vecteurs 384-dim
            W->>DB: DELETE puis INSERT vec0 (UPSERT interdit sur table virtuelle)
            W->>J: maj progression
        end
        W->>J: status=done
    end
    C->>API: GET /api/ai/embed/jobs/{id}
    API->>J: lit job
    J-->>C: progression / résultat
    Note over C,API: GET /api/ai/embed/status → diagnostics (compte embeddings)
```

---

## 7. LLM génératif — `POST /api/ai/describe` & `/chat` (Ollama)

Génère des présentations enthousiastes ou un chat libre via Ollama (gemma4). Streaming SSE possible. 503 si Ollama injoignable.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant DEP as verify_api_key (X-API-Key)
    participant API as ai.py describe()/chat()
    participant OS as ollama_service
    participant OL as Ollama (gemma4)

    C->>DEP: POST /api/ai/describe {title, genres, overview, language}
    DEP->>API: payload validé
    API->>API: construit prompt (FR/EN, 2-3 phrases, sans spoiler)
    API->>OS: generate(prompt)
    OS->>OL: inférence
    alt Ollama OK
        OL-->>OS: texte
        OS-->>API: recommandation
        API-->>C: 200 {recommendation, model}
    else Ollama injoignable / modèle non chargé
        OS-->>API: exception
        API-->>C: 503 "LLM unavailable"
    end

    Note over C,OL: /chat stream=true → StreamingResponse SSE :<br/>data: <chunk> … data: [DONE] (ou [ERROR])
```

---

## 8. Appairage TV — device-flow (`/api/tv-auth/*`)

Flux RFC 8628-like. La TV démarre (non authentifiée), un client authentifié approuve avec payload chiffré Fernet, la TV poll puis complète (one-shot, scrub). TTL 900 s.

```mermaid
sequenceDiagram
    autonumber
    participant TV as TV (PlexHubTV, non authentifiée)
    participant APP as App/console authentifiée (X-API-Key)
    participant S as tv_auth.py
    participant CR as payload_crypto (Fernet)
    participant DB as tv_auth_sessions (TTL 900s)

    TV->>S: POST /api/tv-auth/start
    S->>DB: crée session (deviceCode, userCode, status=pending, expiry)
    S-->>TV: 201 {deviceCode, userCode}
    Note over TV: affiche userCode à l'écran

    APP->>S: POST /api/tv-auth/approve {userCode, payload} (X-API-Key)
    S->>S: verify_pairing_api_key + _expire_if_needed
    S->>CR: chiffre le payload (config serveur/credentials)
    S->>DB: attache payload chiffré, status=approved
    S-->>APP: 200 OK

    loop poll
        TV->>S: GET /api/tv-auth/status?deviceCode=…
        alt expirée
            S->>DB: marque expired
            S-->>TV: expired
        else pending
            S-->>TV: pending
        else approved
            S->>CR: déchiffre payload
            S->>DB: marque payload livré (UNE seule fois)
            S-->>TV: 200 {payload}
        end
    end

    TV->>S: POST /api/tv-auth/complete {deviceCode}
    S->>DB: scrub session (one-shot)
    S-->>TV: 200 OK (TV configurée)
```

---

### Notes transverses (s'appliquent à tous les flux)

- **Écritures DB** : SQLite WAL + `commit_with_retry`/`run_with_retry` (retry « database is locked »).
- **Appels bloquants** (`sqlite3.backup`, inférence ONNX) → `asyncio.to_thread` ; images Plex → `ThreadPoolExecutor`.
- **Auth** : seuls le router `/api/ai` et `POST /api/tv-auth/approve` exigent `X-API-Key`. Les routers catalogue (`accounts`/`media`/`live`/`stream`/`sync`/`plex`/`categories`/`admin`) ne sont **pas** authentifiés (dette ouverte, cf. §10).
- **Observabilité** : métriques `plexhub_*` sur `/metrics`, `request_id` injecté par middleware.
