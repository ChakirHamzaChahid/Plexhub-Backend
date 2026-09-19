# ADR 0005 — Scraper manuel (TMDB + IMDb/OMDb) + score poster, verrou `match_locked`, lot + file « À vérifier »

- Statut : **proposé** (tech-lead, Phase 1 « Architecte » du `/refacto` scraper manuel) — gate : validation avant tout code
- Date : 2026-09-19 · base HEAD `4e5b04f` (`develop`), chaîne de migrations 001→025
- Plan source : `C:\Users\chakir\.claude\plans\yes-enumerated-marble.md` (validé)
- Portée : `app/services/{tmdb_service,omdb_service,omdb_scrape_cache_service,unified_group_service,media_service,account_service}.py`, **nouveaux** `app/services/{media_identity_writer,poster_match_service,manual_scrape_service}.py` + `app/workers/manual_scrape_batch_worker.py`, `app/workers/{enrichment_worker,enrichment_backfill_worker,sync_worker}.py`, `app/services/nfo_import_service.py`, `app/scripts/{validate_id_consistency,dedup_resolved_twins}.py`, `app/api/{admin,media}.py`, `app/templates/admin/*`. **2 migrations additives** (026, 027). **Aucune nouvelle route `/api/*`** (piège 19a).
- ADR recoupés : 0003 (blend `display_rating` — réutilisé tel quel), 0004 D4 (`write_with_retry`, session neuve par tentative — obligatoire ici)

**Règle d'interprétation :** tout ce qui est en bloc de code ci-dessous est un **contrat figé**
(nom, signature, sémantique). Un IC qui a besoin d'en dévier ouvre une question au tech-lead ;
il n'improvise pas. Les `fichier:ligne` sont vérifiés à `4e5b04f`.

---

## D1 — Faits du code qui contraignent le design (vérifiés)

| # | Fait | Preuve | Conséquence contractuelle |
|---|---|---|---|
| F1 | La PK de `media` a **4 colonnes** `(rating_key, server_id, filter, sort_order)` : un même item existe en N lignes (une par catégorie). | `models/database.py:18-21` | Toute écriture cible `(rating_key, server_id)` (toutes variantes) ; toute lecture « un item » = `GROUP BY server_id, rating_key` ; `scrape_review` se raccroche à `media` par **`EXISTS`**, jamais `INNER JOIN` (doublons). |
| F2 | `_get_client()` TMDB injecte `api_key` en query-param sur **chaque** requête. | `tmdb_service.py:135-146` (`:139`) | Le client image est un `httpx.AsyncClient` **dédié, sans `params`**. Interdit d'utiliser `tmdb_service`/`omdb_service` pour télécharger une image. |
| F3 | La branche « match TMDB » du worker n'est **pas** du fill-missing pur : identité + `summary/genres/resolved_thumb_url/resolved_art_url/scraped_rating/year/cast` sont **écrasés si fournis** ; seules 13 colonnes « riches » sont en `COALESCE`. | `enrichment_worker.py:498-554` | Le mode `fill` de `media_identity_writer` reproduit **exactement** ça (et non un fill-missing idéal). |
| F4 | Le worker n'écrit **jamais** `updated_at`. | `enrichment_worker.py:620-628` | `build_*_values` ne l'émet pas ; c'est `manual_scrape_service` qui l'ajoute (clé de cache `get_unified_list` = `COUNT+MAX(updated_at)`, `media_service.py:346`). |
| F5 | `run()` remet à zéro les compteurs globaux TMDB/OMDb. | `enrichment_worker.py:649,652` | Budget du lot = compteur `ContextVar` propre, jamais `reset_request_count()`. |
| F6 | Scénario 3 : imdb seul → **jamais** de tmdb. | `enrichment_worker.py:158-161` | Passe 1 « appariement » du lot. |
| F7 | `_fetch_omdb_by_id` est dupliqué ligne à ligne (worker + backfill) et **diffère le `put`** au batch (anti `UNIQUE` sous `no_autoflush`). Les tests patchent `backfill._fetch_omdb_by_id` et `backfill.omdb_service`. | `enrichment_worker.py:185-205`, `enrichment_backfill_worker.py:117-141`, `tests/test_worker_write_retry_real_lock.py`, `tests/test_enrichment_backfill.py:275` | `get_or_fetch` renvoie `(data, pending_put)` et prend `client` + `session_factory` injectés ; les 2 wrappers privés **gardent nom et signature**. |
| F8 | htmx **1.9.12** ne swappe pas les réponses 4xx (le 422 existant de `admin.py:192` n'est déjà jamais affiché). | `templates/admin/base.html:8` | W4 ajoute un handler `htmx:beforeSwap` qui autorise le swap sur 409/422. |
| F9 | Le trigger `from:#filters select` (sélecteur avec espace) est le piège htmx connu (§5.10). | `templates/admin/index.html:13` | Remplacé par `change, keyup delay:300ms, submit`. |
| F10 | Pillow 10.4.0 est déjà installé **transitivement** (fastembed 0.8 : `pillow>=10.3,<13`, `>=11` sous py3.13). | `pip show fastembed` | Épinglage explicite `pillow>=10.3,<13` dans `requirements.txt` (W3), aucun conflit. |

---

## D2 — `tmdb_service` (W0 : découpage neutre + compteur ; W3 : extras)

```python
# app/services/tmdb_service.py — module level
POSTER_W185_BASE = "https://image.tmdb.org/t/p/w185"
_IMAGE_LANGUAGE_FILTER = "fr,en,null"

@dataclass
class RequestTally:
    count: int = 0                      # vraies tentatives HTTP (chaque retry compte)

_request_tally: ContextVar[RequestTally | None] = ContextVar("tmdb_request_tally", default=None)

@contextmanager
def count_requests() -> Iterator[RequestTally]:
    """Compte les appels TMDB réels faits dans ce contexte ET dans les tâches
    asyncio créées depuis ce contexte (copie de contexte => même objet partagé).
    Non agrégeant en imbrication : le plus interne gagne. N'affecte JAMAIS
    `real_request_count`."""

def title_similarity(query: str, candidate: str) -> float: ...   # = _title_sim(normalize_for_sorting(query), candidate)
def year_score(query_year: int | None, candidate_year: int | None) -> float: ...  # = _year_score

@dataclass(frozen=True)
class ScoredCandidate:
    tmdb_id: int
    kind: Literal["movie", "tv"]
    title: str
    original_title: str | None
    year: int | None
    overview: str | None
    poster_path: str | None            # chemin TMDB brut ("/abc.jpg")
    vote_count: int
    title_score: float
    confidence: float                  # 0.7·title + 0.3·year (inchangé)

@dataclass
class CandidateSearch:
    verdict: TMDBSearchOutcome         # IDENTIQUE à ce que _best_match renverrait pour (results, title, year, summary)
    candidates: list[ScoredCandidate]  # tri confidence desc puis vote_count desc, ≤ 10

@dataclass(frozen=True)
class MatchExtras:
    tmdb_id: int
    kind: Literal["movie", "tv"]
    imdb_id: str | None                # external_ids.imdb_id, préfixe "tt" garanti
    title: str
    original_title: str | None
    year: int | None
    overview: str | None
    poster_urls: list[str]             # w185 ; poster principal d'abord, puis images.posters (ordre TMDB), dédoublonné, ≤ POSTER_MAX_VARIANTS
```

```python
class TMDBService:
    def _score_results(self, results: list[dict], title: str, year: int | None, *,
                       title_key: str, orig_key: str, date_key: str,
                       ) -> list[tuple[TMDBMatch, dict]]:
        """Corps actuel de _best_match lignes 580-600 : results[:10], tri (confidence, vote_count) desc.
        Renvoie (match, dict résultat brut) — le brut porte overview/poster_path/original_*."""

    def _verdict(self, scored: list[tuple[TMDBMatch, dict]], summary: str | None) -> TMDBSearchOutcome:
        """Corps actuel lignes 601-626 (seuils, marge, tie-break résumé sur raw.get('overview') or '')."""

    def _best_match(...):  # signature INCHANGÉE
        # if not results: return TMDBSearchOutcome("nomatch")
        # return self._verdict(self._score_results(...), summary)

    async def search_candidates(self, kind: Literal["movie", "tv"], title: str, year: int | None,
                                *, language: str | None = None, summary: str | None = None,
                                ) -> CandidateSearch:
        """UN seul appel /search/movie|tv (mêmes params que search_movie/search_tv). Pas de
        _search_cache (recherche manuelle = fraîche). Non configuré -> CandidateSearch(nomatch, [])."""

    async def get_match_extras(self, tmdb_id: int, kind: Literal["movie", "tv"]) -> MatchExtras | None:
        """UN appel /{kind}/{id}?append_to_response=external_ids,images
        &include_image_language=fr,en,null&language=TMDB_LANGUAGE. 404/erreur -> None.
        Log = type d'exception + statut HTTP UNIQUEMENT (str(exc) embarque l'URL avec api_key)."""
```

- Le tally s'incrémente **à côté** de `self.real_request_count += 1` (`tmdb_service.py:198`), dans la même boucle (donc chaque retry compte).
- **Même mécanisme dans `omdb_service`** (`count_requests()` + `RequestTally`, incrément à `omdb_service.py:196`) — ajout par rapport au plan, pour que le job de lot rapporte ses appels OMDb sans lire un compteur global que l'enrichissement remet à zéro.

## D3 — `omdb_service.search_list` (W4) + `omdb_scrape_cache_service.get_or_fetch` (W0)

```python
@dataclass(frozen=True)
class OMDbHit:
    imdb_id: str
    title: str
    year: int | None          # 4 premiers chiffres de "Year" ("2015–2019" -> 2015)
    type: str                 # "movie" | "series" | "episode" (passé tel quel)
    poster_url: str | None    # "N/A" -> None

async def search_list(self, query: str, year: int | None, media_type: str) -> list[OMDbHit]:
    """GET /?s=<query>[&y=][&type=movie|series] — page 1 seulement (≤10). media_type 'movie'|'show'.
    Mêmes garanties que search_by_title : no-op si non configuré / query vide, garde
    _budget_exhausted(), [] sur toute erreur, 1 incrément plexhub_omdb_requests_total{result}
    par appel, clé jamais loggée (type/statut seulement). Response:"False" -> [] + result=not_found."""
```

```python
# app/services/omdb_scrape_cache_service.py
async def get_or_fetch(
    imdb_id: str, *,
    session_factory: Callable[[], AsyncSession],
    client: OMDbService,
) -> tuple[OMDbData | None, tuple[str, str] | None]:
    """Corps EXACT de enrichment_worker._fetch_omdb_by_id : (None, None) si id vide / non configuré /
    client.get_request_count() >= OMDB_DAILY_LIMIT ; lecture cache sur session courte ; sinon
    client.get_by_imdb_id(). Renvoie (data, pending_put) où pending_put=(imdb_id, 'found'|'not_found')
    sur appel frais, None sur hit cache/skip. N'ÉCRIT JAMAIS : l'appelant persiste via put() dans
    SA transaction (dédup batch — sinon UNIQUE omdb_scrape_cache.imdb_id)."""
```

Wrappers conservés (W0, noms/signatures inchangés, résolution des globals **à l'appel** pour que les monkeypatch de tests restent effectifs) :
`enrichment_worker._fetch_omdb_by_id(imdb_id)` → `get_or_fetch(imdb_id, session_factory=async_session_factory, client=omdb_service)` ;
`enrichment_backfill_worker._fetch_omdb_by_id(imdb_id, session_factory)` → idem avec `client=omdb_service` du module backfill.

## D4 — `media_identity_writer` (nouveau, W0)

```python
# app/services/media_identity_writer.py — fonctions PURES : renvoient un dict pour update(Media).values(**d)
WriteMode = Literal["fill", "replace"]

XTREAM_ORIGIN_COLS = ("summary", "genres", "cast", "year", "content_rating")
PROVIDER_ONLY_COLS = ("original_title", "tagline", "premiered", "status", "studio", "country",
                      "tvdb_id", "wikidata_id", "tmdb_rating", "tmdb_votes", "cast_json",
                      "youtube_trailer", "scraped_rating")

def build_identity_values(
    data: TMDBEnrichmentData, *,
    confidence: float | None,
    omdb: OMDbData | None,
    mode: WriteMode = "fill",
    is_adult: bool = False,
) -> dict[str, Any]: ...

def build_rating_values(
    *, omdb_imdb_rating: float | None, omdb_imdb_votes: int | None,
    tmdb_rating: float | None, mode: WriteMode = "fill",
) -> dict[str, Any]: ...
```

**Identité (les deux modes)** : `tmdb_id=str(data.tmdb_id)`, `imdb_id=data.imdb_id`,
`unification_id = history_group_key = "imdb://tt…" si imdb sinon "tmdb://<id>"`, `tmdb_match_confidence=confidence`.
Jamais `updated_at`, `match_locked`, `match_source`, `title` (voir D12).

**`mode="fill"`** = octet-pour-octet `enrichment_worker.py:504-554` + `:593-618` :
- écrasé **si fourni** (truthy) : `summary←overview`, `genres`, `resolved_thumb_url←poster_url`, `resolved_art_url←backdrop_url`, `scraped_rating←vote_average`, `year`, `cast` ;
- `COALESCE(col, new)` si `new is not None` : `content_rating, original_title, tagline, premiered, status, studio, country, tvdb_id, wikidata_id, tmdb_rating, tmdb_votes, cast_json, youtube_trailer` ;
- `omdb` et `is_adult` **ignorés** (le `COALESCE` protège déjà `"XXX"`) ;
- `build_rating_values(fill)` : `imdb_rating/imdb_votes = COALESCE(col, new)` si new non-None ; `display_rating = blend_display_rating_case(COALESCE-ou-col imdb, COALESCE-ou-col tmdb, Media.display_rating)` — émis par l'appelant seulement si TMDB **ou** OMDb présent (condition actuelle `:607`).

**`mode="replace"`** (correction d'une mauvaise identité — le film faux ne doit rien laisser) :
- `XTREAM_ORIGIN_COLS` : nouvelle valeur TMDB, sinon repli OMDb (`plot`/`genre`/`actors`/année) , sinon **inchangée** (jamais NULL : ces colonnes viennent aussi de Xtream) ;
- `content_rating` : `settings.ADULT_CONTENT_RATING` si `is_adult`, sinon cert TMDB, sinon inchangée ;
- `resolved_thumb_url` : poster TMDB sinon **`Media.thumb_url`** ; `resolved_art_url` : backdrop sinon **`Media.art_url`** ;
- `PROVIDER_ONLY_COLS` : nouvelle valeur **ou `NULL`** (affectation directe, pas de `COALESCE`) ;
- `build_rating_values(replace)` : `imdb_rating/imdb_votes` = valeur OMDb ou `NULL` ; `display_rating = blend_rating(imdb, tmdb)` calculé en Python, sinon expression `Media.display_rating` (inchangé).

Le worker (W0) : `update_values.update(build_identity_values(data, confidence=fr.confidence, omdb=None, mode="fill"))` dans la branche `if enrichment_data:` et `build_rating_values(...)` pour le bloc notes ; branches OMDb-strong/weak (`:557-588`) **inchangées**.

## D5 — `poster_match_service` (nouveau, W3)

```python
# app/services/poster_match_service.py
Badge = Literal["identical", "close", "different", "unknown"]

class PosterFetchError(Exception): ...   # message sans URL ni hôte

@dataclass(frozen=True)
class PosterHash:
    dhash: int          # 64 bits, gradient horizontal sur 9x8 niveaux de gris
    phash: int          # 64 bits, DCT 2D numpy 32x32 -> coin 8x8 (DC exclu du médian), seuil = médiane
    is_generic: bool    # écart-type 32x32 < POSTER_GENERIC_STDDEV, ou phash vu >= POSTER_GENERIC_MIN_REPEAT fois (URLs distinctes)

@dataclass(frozen=True)
class FetchedImage:
    content: bytes
    content_type: str   # toujours "image/*"

@dataclass(frozen=True)
class PosterComparison:
    badge: Badge
    phash_distance: int | None
    dhash_distance: int | None
    image_score: float | None        # max(0, 1 - phash_distance/32) ; None si unknown
    best_poster_url: str | None      # variante candidate la plus proche
    variants_compared: int
    xtream_generic: bool
    shortcut: bool                   # True = match par nom de fichier image.tmdb.org, sans téléchargement

def compute_hashes(image_bytes: bytes) -> PosterHash:
    """SYNC, CPU pur — n'appeler QUE via asyncio.to_thread. Pillow : Image.open (lazy) ->
    rejet si width*height > POSTER_MAX_PIXELS AVANT load() (+ capture DecompressionBombError/Warning,
    on NE modifie PAS Image.MAX_IMAGE_PIXELS global) -> ImageOps.exif_transpose -> L ->
    rognage letterbox (lignes/colonnes de bord quasi uniformes) -> hashes. PosterFetchError si indécodable."""

def hamming(a: int, b: int) -> int: ...          # (a ^ b).bit_count()

def badge_for(phash_d: int | None, dhash_d: int | None) -> Badge:
    # identical : phash_d <= POSTER_PHASH_IDENTICAL and dhash_d <= POSTER_DHASH_IDENTICAL
    # close     : phash_d <= POSTER_PHASH_CLOSE ; different sinon ; unknown si None

async def fetch_image(url: str) -> FetchedImage:          # PosterFetchError sur tout échec
async def hash_url(url: str) -> PosterHash | None:        # cache TTL positif 6 h / négatif 15 min, clé = URL
async def compare_posters(xtream_url: str | None, candidate_poster_urls: Sequence[str]) -> PosterComparison:
    """1) xtream_url vide/échec -> unknown. 2) raccourci : si xtream_url est sur image.tmdb.org et son
    basename == basename d'une variante -> identical, distances 0, shortcut=True. 3) sinon hash Xtream,
    puis variantes dans l'ordre (≤ POSTER_MAX_VARIANTS), distance = MIN, arrêt anticipé dès identical.
    xtream_generic=True -> badge plafonné à 'close' (un placeholder n'est jamais 'identical')."""

async def close() -> None:                                  # appelé au shutdown (main.py, à côté de :545-550)
```

Client : `httpx.AsyncClient(follow_redirects=True, max_redirects=5, timeout=POSTER_FETCH_TIMEOUT,
event_hooks={"request": [ssrf.vet_request]})` (`utils/ssrf.py:189`, modèle `plex_generator/storage.py:64`) — **aucun `params`**.
Réponse : `stream()` ; rejet si `Content-Type` ne commence pas par `image/` ou si > `POSTER_MAX_BYTES` (compté pendant la lecture, pas seulement `Content-Length`).
Sémaphores : global `POSTER_CONCURRENCY` ; par hôte `POSTER_PER_HOST_CONCURRENCY` pour tout hôte **hors** `{"image.tmdb.org", "m.media-amazon.com"}`.
Logs : jamais d'URL ni d'hôte Xtream — `type(exc).__name__` + `sha1(url)[:8]`.

## D6 — `manual_scrape_service` (nouveau, W2 → W5)

```python
# app/services/manual_scrape_service.py
MediaKind = Literal["movie", "show"]                 # vocabulaire Media.type ; kind TMDB dérivé ("show" -> "tv")
Provider = Literal["auto", "tmdb", "omdb"]
MatchSource = Literal["manual", "batch_auto", "batch_pair"]
IdFilter = Literal["all", "missing_imdb", "missing_tmdb", "missing_both", "incomplete",
                   "locked", "batch", "review"]      # incomplete = au moins un id manquant (défaut UI)
TEXT_WEIGHT, IMAGE_WEIGHT = 0.6, 0.4
RULE_B_MIN_TITLE_SCORE = 0.6

@dataclass(frozen=True)
class ScrapeQuery:
    media_type: MediaKind
    title: str
    year: int | None
    provider: Provider = "auto"
    language: str | None = None          # None -> settings.TMDB_LANGUAGE

@dataclass
class Candidate:
    provider: Literal["tmdb", "omdb"]
    tmdb_id: int | None
    imdb_id: str | None
    media_type: MediaKind
    title: str
    original_title: str | None
    year: int | None
    overview: str | None
    poster_url: str | None               # w185 TMDB ou Poster OMDb — chargé tel quel par le navigateur
    title_score: float
    text_confidence: float               # 0.7·title + 0.3·year (mêmes helpers D2)
    poster: PosterComparison | None      # None = hors top-N comparé
    combined_score: float                # 0.6·text + 0.4·image_score ; = text si poster None/unknown
    text_safe: bool                      # verdict TMDB 'matched' ET verdict.match.tmdb_id == tmdb_id
    recommended: bool                    # au plus 1 : le 1er du tri SI text_safe ou badge identical
    def to_json(self) -> dict[str, Any]: ...   # clés figées, cf. D8

@dataclass
class SearchResult:
    query: ScrapeQuery
    candidates: list[Candidate]          # tri combined desc, text_confidence desc, ≤ 10
    text_verdict: Literal["matched", "ambiguous", "nomatch"]
    xtream_poster_available: bool
    xtream_poster_generic: bool
    tmdb_calls: int                      # via count_requests()
    omdb_calls: int

@dataclass
class ApplyOutcome:
    status: Literal["applied", "conflict", "not_found", "provider_not_found", "skipped_locked"]
    server_id: str
    rating_key: str
    media_type: MediaKind
    mode: WriteMode | None
    old_tmdb_id: str | None
    old_imdb_id: str | None
    new_tmdb_id: str | None
    new_imdb_id: str | None
    propagated: int                      # nb d'items (server_id, rating_key) distincts propagés
    conflicts: list[tuple[str, str, str | None, str | None]]   # (server_id, rating_key, tmdb_id, imdb_id), ≤ 10

@dataclass(frozen=True)
class Decision:
    action: Literal["apply", "review"]
    rule: Literal["A", "B"] | None
    candidate: Candidate | None
    reason: Literal["text_safe_and_identical", "poster_only_identical", "no_candidates",
                    "no_xtream_poster", "xtream_poster_generic", "ambiguous_posters",
                    "no_identical_poster", "text_safe_no_poster_match"]

@dataclass
class CataloguePage:
    items: list[Media]                   # 1 ligne par (server_id, rating_key) (GROUP BY, F1)
    total: int
    offset: int
    review_keys: set[tuple[str, str]]    # items ayant une review 'pending'

@dataclass(frozen=True)
class TypeStats:
    total: int; missing_imdb: int; missing_tmdb: int; with_both: int; locked: int; review_pending: int
```

```python
async def search_candidates(query: ScrapeQuery, *, xtream_poster_url: str | None,
                            summary: str | None = None, poster_top_n: int | None = None) -> SearchResult
async def lookup(raw_id: str, *, media_type: MediaKind, xtream_poster_url: str | None) -> SearchResult
async def apply_candidate(*, server_id: str, rating_key: str, media_type: MediaKind,
                          tmdb_id: int | None, imdb_id: str | None,
                          source: MatchSource = "manual", confidence: float | None = None,
                          force: bool = False, propagate: bool = False, schedule_rebuild: bool = True,
                          session_factory: Callable[[], AsyncSession]) -> ApplyOutcome
async def unlock(server_id: str, rating_key: str, *, session_factory) -> bool
async def clear_ids(server_id: str, rating_key: str, *, lock: bool = False, session_factory) -> bool
async def load_row(server_id: str, rating_key: str, *, session_factory) -> Media | None
async def list_catalogue(db: AsyncSession, *, media_type: MediaKind, id_filter: IdFilter = "incomplete",
                         search: str | None = None, sort: str = "added_desc",
                         page: int = 1, page_size: int = 50) -> CataloguePage
async def catalogue_stats(db: AsyncSession) -> dict[MediaKind, TypeStats]
async def list_reviews(db: AsyncSession, *, media_type: MediaKind | None, page: int, page_size: int
                       ) -> tuple[list[ScrapeReview], int]
async def upsert_review(*, server_id, rating_key, media_type, title, year, reason: str,
                        candidates: list[Candidate], session_factory) -> None
async def dismiss_review(server_id: str, rating_key: str, *, session_factory) -> bool
def decide(result: SearchResult, *, poster_only_auto: bool) -> Decision      # PURE, aucun I/O
```

**`search_candidates`** — (1) `provider != "omdb"` : `tmdb_service.search_candidates` avec l'année ; si verdict ≠ matched et année fournie → sans année ; si toujours ≠ matched → `language="en-US"` (même chaîne que `_search_with_fallback`, **sans** `/search/multi`). Fusion dédoublonnée par `tmdb_id` (garde la meilleure `confidence`) ; `text_verdict` = premier `matched` de la chaîne, sinon `ambiguous` si l'un l'est, sinon `nomatch`. (2) OMDb `search_list` si `provider == "omdb"` (TMDB sauté) ou si `provider == "auto"` et 0 candidat TMDB ; `provider == "tmdb"` → jamais OMDb. (3) `get_match_extras` + `compare_posters` sur les `poster_top_n` premiers par `text_confidence` (défaut `SCRAPE_INTERACTIVE_POSTER_CANDIDATES`=5 ; lot = `SCRAPE_BATCH_POSTER_CANDIDATES`=3) ; les extras remplissent `imdb_id`. Candidats OMDb : une seule variante = leur `poster_url`. (4) scores, tri, `recommended`.

**`lookup`** — accepte `tt\d+`, un entier TMDB, une URL `imdb.com/title/(tt\d+)` ou `themoviedb.org/(movie|tv)/(\d+)`. imdb → `find_by_imdb_id` → `get_match_extras` ; sinon OMDb `get_or_fetch` (carte OMDb seule). Non parsable → `ValueError` (route → 422). 1 candidat, `recommended=False`.

**`apply_candidate`** — trois phases **strictement séparées** (piège 8) :
1. *Lecture* (session courte) : ligne cible (`not_found` si absente), `is_adult`, ids actuels, clé titre propagation `calculate_unification_id(title, year)`.
2. *Réseau* (hors transaction) : `tmdb_id` fourni → `get_movie_details|get_tv_details` (404 → `provider_not_found`) ; sinon `imdb_id` → `find_by_imdb_id` puis détails ; si toujours pas de TMDB → identité **imdb seule** via OMDb `get_or_fetch` (rien d'OMDb non plus → `provider_not_found`). Si l'opérateur a fourni un imdb ≠ `details.imdb_id` → conflit. OMDb `get_or_fetch(imdb final)` pour notes/replis. **Contrôle de conflit** (lecture, même `type`, hors cible) : `tmdb_id = T AND imdb_id NOT IN ('', I)` ou `imdb_id = I AND tmdb_id NOT IN ('', T)`. Conflit et `force=False` → `conflict`, **zéro écriture**. `force=True` écrit sur la cible seulement, jamais sur les autres lignes.
3. *Écriture* : `write_with_retry(work, session_factory=session_factory, op="manual_scrape.apply")`, `work` rejouable, **aucun `await` réseau dedans** :
   - `mode = "replace" if (old_tmdb and old_tmdb != new_tmdb) or (old_imdb and old_imdb != new_imdb) else "fill"` ;
   - `UPDATE media … WHERE rating_key=:rk AND server_id=:sid` (+ `AND match_locked = 0` si `source != "manual"` → rowcount 0 = `skipped_locked`) avec `build_identity_values ∪ build_rating_values ∪ {match_locked: True, match_source: source, updated_at: now_ms(), tmdb_match_confidence: 1.0 si manual/batch_pair sinon confidence}` ;
   - `scrape_cache.put(make_key(type, title, year), type, "matched", conf, details, ts)` (écrase le cache titre) ;
   - `UPDATE enrichment_queue SET status='done', processed_at=:ts` ; `UPDATE scrape_review SET status='resolved', resolved_at=:ts WHERE status='pending'` ;
   - `omdb_scrape_cache.put` si `pending_put` ;
   - `propagate=True` : mêmes valeurs (mode `fill`, même `match_source`) sur les items `type=:t AND unification_id=:title_key AND match_locked=0 AND COALESCE(imdb_id,'')='' AND COALESCE(tmdb_id,'')=''` hors cible ;
   - `commit`.
4. Puis `unified_group_service.schedule_rebuild(media_type, session_factory=session_factory)` si `schedule_rebuild`.

**`unlock`** : `match_locked=0, match_source=NULL, updated_at` ; ids conservés. **`clear_ids`** : `imdb_id=tmdb_id=NULL`, `unification_id/history_group_key` recalculés titre (`calculate_unification_id` + `calculate_history_group_key`), `tmdb_match_confidence=NULL`, `match_locked=lock`, `match_source='manual' if lock else NULL`, `updated_at` ; puis `schedule_rebuild`. **`load_row`** ouvre une **session neuve** : une route admin qui ré-affiche la ligne après écriture ne passe jamais par sa session `get_db` (snapshot WAL figé si elle a déjà lu).

**`list_catalogue`** : `type`, `is_in_allowed_categories`, `GROUP BY server_id, rating_key`, **sans masque outage** (piège 20), `id_filter` : `locked`=`match_locked=1` ; `batch`=`match_source IN ('batch_auto','batch_pair')` ; `review`=`EXISTS scrape_review pending`. **`catalogue_stats`** : sur items distincts, catégories autorisées (≠ anciens compteurs `count_movies_missing_external` qui comptaient les lignes-variantes — non réutilisés).

**`decide()`** (ordre d'évaluation figé) :
1. aucun candidat → review `no_candidates` ; 2. `xtream_poster_available=False` → review `no_xtream_poster` (ou `text_safe_no_poster_match` si un candidat est `text_safe`) ;
3. **A** : ∃ c `text_safe` et `c.poster.badge == "identical"` → apply A ;
4. `xtream_poster_generic` → review `xtream_poster_generic` ;
5. **B** (si `poster_only_auto`) : exactement **un** candidat `identical` parmi les comparés, `c.title_score ≥ 0.6`, année compatible (`query.year` ou `c.year` absent, ou |Δ| ≤ 1) → apply B ; plusieurs `identical` → review `ambiguous_posters` ;
6. sinon review (`text_safe_no_poster_match` si un `text_safe` existe, sinon `no_identical_poster`).

## D7 — Verrou : migration 026 (W1, `needs-approval` : additive, non destructive)

```python
async def _migration_026_add_media_match_lock(engine: AsyncEngine) -> None:
    columns = (("match_locked", "INTEGER NOT NULL DEFAULT 0"),
               ("match_source", "TEXT"))            # 'manual' | 'batch_auto' | 'batch_pair' | NULL
    # boucle identique à 025 : _column_exists("media", name) -> skip ; sinon
    # ALTER TABLE media ADD COLUMN {name} {ddl} dans try/except (course init_db multi-process)
```
Appel ajouté **après** `_migration_025_add_account_outage_tracking(engine)` (`migrations.py:53`). **Pas d'index** sur `match_locked` (booléen peu sélectif : même piège que `ix_media_category_visible`, AUDIT-P3-001 ; les `NOT EXISTS` corrèlent sur le préfixe de PK).

ORM (`class Media`, après `is_adult` `models/database.py:111`) :
```python
match_locked = Column(Boolean, nullable=False, default=False, server_default=text("0"))
match_source = Column(Text)
```

**Points d'application du verrou (vérifiés à HEAD)** :

| # | Où | Changement |
|---|---|---|
| L1 | `enrichment_worker.py:662-675` (sélection Phase 1 films) | `~_media_is_locked()` — `EXISTS(select Media.rating_key WHERE rk,sid corrélés AND match_locked = 1)`, même idiome que `_media_is_category_visible` `:31-55` |
| L2 | `enrichment_worker.py:719-731` (sélection Phase 2 séries) | idem |
| L3 | `enrichment_worker.py:620-628` (UPDATE `media`) | `WHERE … AND Media.match_locked == False` (course : verrou posé entre sélection et écriture) |
| L4 | `sync_worker.py:733-763` (upsert, `set_` avant `on_conflict_do_update`) | pour `resolved_thumb_url, resolved_art_url, summary, genres, year` : `set_[c] = case((Media.match_locked == True, getattr(Media, c)), else_=stmt.excluded[c])` (si `c in set_`). `display_rating` non protégé : guéri par `recompute_display_rating_stmt()` en fin d'enrichissement (comportement actuel des lignes non verrouillées) |
| L5 | `nfo_import_service.py:500-512` (`_compute_updates`) | si `row.match_locked` : `imdb_id, tmdb_id, resolved_thumb_url, resolved_art_url` forcés en fill-missing même avec `overwrite=True` ; autres colonnes inchangées |
| L6 | `validate_id_consistency.py:406-442` (`_apply_fix`) + `:561-565` (appel) | `WHERE … AND match_locked = 0` + la boucle saute les lignes verrouillées et les compte (`report.skipped_locked`) |
| L7 | `dedup_resolved_twins.py:165-170` (UPDATE brut) | `AND match_locked = 0` |
| L8 | `media_service.py:680-728` (`enqueue_rescrape`) | retour `Literal["queued", "not_found", "locked"]` (au lieu de `bool`) ; ligne verrouillée → `"locked"`, rien écrit |
| L9 | `api/media.py:533-543` (`POST /api/media/{rk}/rescrape`) | `locked` → **409** `detail="Media match is locked"` (à signaler côté Android) ; `admin.py:213-229` → ligne rendue avec message « verrouillé » + bouton Déverrouiller |
| L10 | `media_service.py:650-678` (`update_external_ids`) — **correctif du bug** | `fields` non vide : même UPDATE ajoute `unification_id`/`history_group_key` recalculés (`calculate_unification_id(title, year, imdb, tmdb)` + `calculate_history_group_key`), `match_locked=True`, `match_source='manual'`, `updated_at` ; puis `schedule_rebuild(type, session_factory=async_session_factory)`. `fields` vide → no-op **sans** verrou (`tests/test_router_http_coverage.py` inchangé). Sert `PATCH /api/media/{rk}` (`api/media.py:511`) et `POST /admin/movies/{rk}/ids` (`admin.py:165`) |
| — | `enrichment_backfill_worker` | **inchangé** : ne remplit que des notes vides (voulu) |
| — | `category_service.update_media_adult_flags` | inchangé : force `XXX`, compatible (le mode `replace` le conserve) |

**Déviations constatées à W1 (revue `code-reviewer` post-`752118b`, corrigées dans le commit suivant sauf mention contraire) :**
- **BLOCKING, corrigé** — L4 tel que livré ne protégeait QUE `resolved_thumb_url/resolved_art_url/summary/genres/year`. `tmdb_id` (toujours en `COALESCE(Media.tmdb_id, excluded.tmdb_id)`) et `unification_id`/`history_group_key` (toujours dérivés de `keep_uni = unification_id LIKE '%://%'`) ignoraient `match_locked` : un opérateur qui **efface** un `tmdb_id` faux sur une ligne verrouillée (`tmdb_id=NULL`, `unification_id` retombé titre-based) se le voyait **réinjecté par le prochain `content_hash` changé** côté provider — la COALESCE ne distingue pas « jamais rempli » de « vidé exprès ». Fix : les trois colonnes sont désormais chacune enveloppées `case((Media.match_locked == True, <valeur actuelle de la colonne>), else_=<expression pré-existante inchangée pour les lignes non verrouillées>)` — une ligne verrouillée garde sa valeur courante **telle quelle**, y compris `NULL`. Test de non-régression associé : une ligne NON verrouillée continue de prendre l'id/unification du provider normalement.
- **Non-blocking, corrigé** — `enrichment_worker._media_is_locked()` renommé **`_media_is_unlocked()`** : la fonction renvoie un `NOT EXISTS` (elle **affirme** « pas verrouillé »), l'ancien nom se lisait à l'envers à ses deux points d'appel (`.where(..., _media_is_locked())` donnait l'impression de sélectionner les lignes verrouillées).
- **Non-blocking, corrigé** — tests ajoutés : `update_external_ids` sur une ligne portant plusieurs variantes `(filter, sort_order)` (la PK composite F1 — l'UPDATE doit toucher **toutes** les variantes, pas la première trouvée) ; et le cas où l'`imdb_id` est **déjà présent** avant un patch qui ne touche que `tmdb_id` (la priorité imdb>tmdb de `calculate_unification_id` doit survivre — l'ancien test `test_setting_tmdb_only_prefers_imdb_priority_in_unification` était mal nommé : il ne couvrait que le cas SANS imdb en jeu, renommé `test_setting_tmdb_only_without_imdb_uses_tmdb_based_unification`).
- **Noté pour les vagues suivantes (pas un défaut de W1)** : le bouton « Déverrouiller » (route `unlock`) est différé à **W4** (D6/D10 l'assignent à `manual_scrape_service`/l'UI, pas encore câblés) — W1 se contente d'afficher l'état verrouillé sur le fragment `_movie_row.html`. Le câblage de `unified_group_service.schedule_rebuild(...)` dans `update_external_ids` (L10) reste **W2** (D9 n'est pas encore implémenté à ce point du rollout) — noté en commentaire dans `media_service.py`. L'import NFO (L5) peut légitimement changer `year` sur une ligne verrouillée (`year` n'est PAS dans `_LOCKED_FILL_ONLY_COLS` — seuls ids+images le sont, par contrat D7) : **comportement intentionnel**, la lock protège l'identité/poster, pas les métadonnées descriptives que l'opérateur n'a pas édité lui-même.

## D8 — Lot + review : migration 027 (W5)

```sql
CREATE TABLE IF NOT EXISTS scrape_review (
    rating_key       TEXT    NOT NULL,
    server_id        TEXT    NOT NULL,
    media_type       TEXT    NOT NULL,
    title            TEXT,
    year             INTEGER,
    reason           TEXT    NOT NULL,
    candidates_json  TEXT    NOT NULL DEFAULT '[]',
    best_confidence  REAL,
    best_image_score REAL,
    status           TEXT    NOT NULL DEFAULT 'pending',   -- pending | resolved | dismissed
    created_at       INTEGER NOT NULL,
    resolved_at      INTEGER,
    PRIMARY KEY (rating_key, server_id)
);
CREATE INDEX IF NOT EXISTS ix_scrape_review_status_type ON scrape_review(status, media_type);
```
(PK composite = l'UNIQUE du plan.) ORM `ScrapeReview` byte-aligné (convention 017-022) :
```python
class ScrapeReview(Base):
    __tablename__ = "scrape_review"
    rating_key = Column(Text, primary_key=True)
    server_id = Column(Text, primary_key=True)
    media_type = Column(Text, nullable=False)
    title = Column(Text)
    year = Column(Integer)
    reason = Column(Text, nullable=False)
    candidates_json = Column(Text, nullable=False, default="[]", server_default=text("'[]'"))
    best_confidence = Column(Float)
    best_image_score = Column(Float)
    status = Column(Text, nullable=False, default="pending", server_default=text("'pending'"))
    created_at = Column(BigInteger, nullable=False)
    resolved_at = Column(BigInteger)
    __table_args__ = (Index("ix_scrape_review_status_type", "status", "media_type"),)
```
`candidates_json` = liste (≤ 5) de `Candidate.to_json()`, clés figées : `provider, tmdb_id, imdb_id, media_type, title, original_title, year, overview, poster_url, title_score, text_confidence, combined_score, text_safe, recommended, badge, phash_distance, dhash_distance, image_score`.
`upsert_review` : `INSERT … ON CONFLICT(rating_key, server_id) DO UPDATE` (repasse `pending`). Orphelins : masqués par `EXISTS media` (F1) ; purgés dans `account_service.delete_account_cascade` (`account_service.py:134-157`, `delete(ScrapeReview).where(server_id == …)`).

```python
# app/workers/manual_scrape_batch_worker.py — modèle enrichment_backfill_worker (store mémoire, process-local)
JOBS_CAP = 20
class BatchAlreadyRunningError(Exception): ...

def is_running() -> bool
def start(*, media_type: MediaKind | Literal["all"], dry_run: bool, max_items: int | None = None) -> str
    # lève BatchAlreadyRunningError ; pose le garde synchrone ; create_background_task(run(...)) ; jobId "scrape_batch_<now_ms>"
def get(job_id: str) -> dict[str, Any] | None
def get_latest() -> dict[str, Any] | None
def cancel(job_id: str) -> bool          # coopératif : cancelRequested=True, vérifié entre deux items
async def run(job_id: str, *, session_factory, media_type, dry_run: bool, max_items: int | None) -> None
```
État du job (camelCase, comme le backfill) : `jobId, status (queued|running|completed|failed|canceled), dryRun, mediaType, phase (pairing|scraping|rebuild|done), scanned, paired, autoAppliedA, autoAppliedB, queuedForReview, skippedLocked, errors, lastError, tmdbCalls, omdbCalls, tmdbBudget, budgetExhausted, distanceHistogram (buckets "0-3","4-6","7-12","13-20","21+","unknown" du meilleur pHash par item), cancelRequested, startedAt, finishedAt`.

Déroulé : tout le corps sous `with tmdb_service.count_requests() as t, omdb_service.count_requests() as o` ; **aucun** `reset_request_count()`. Sélection keyset sur `(server_id, rating_key)` distincts : `type`, catégories autorisées, `match_locked=0`, pas les deux ids, pas de review `pending|dismissed`, ≤ `min(max_items, SCRAPE_BATCH_MAX_ITEMS)`. **Passe 1** (un seul id) : imdb seul → `find_by_imdb_id` ; tmdb seul → détails → imdb ; succès → `apply_candidate(source="batch_pair", schedule_rebuild=False)` ; échec → review (`no_tmdb_for_imdb` / `no_imdb_for_tmdb`). **Passe 2** (aucun id) : `search_candidates(poster_top_n=SCRAPE_BATCH_POSTER_CANDIDATES)` → `decide(poster_only_auto=SCRAPE_BATCH_POSTER_ONLY_AUTO)` → `apply_candidate(source="batch_auto", confidence=c.combined_score, schedule_rebuild=False)` ou `upsert_review`. Concurrence `SCRAPE_BATCH_CONCURRENCY` ; arrêt dès `t.count >= SCRAPE_BATCH_TMDB_LIMIT` (`budgetExhausted=True`, dépassement ≤ items en vol). OMDb fail-open. **dry-run = zéro écriture** (ni media, ni review, ni caches), seulement l'état du job + histogramme. Fin : `schedule_rebuild(mt, delay=0)` par type touché puis `await flush_scheduled()`.

## D9 — `unified_group_service` (W2)

```python
REBUILD_DEBOUNCE_SECONDS = settings.SCRAPE_REBUILD_DEBOUNCE_SECONDS   # 5.0

def schedule_rebuild(media_type: str, *, session_factory: Callable[[], AsyncSession],
                     delay: float | None = None) -> None
    """Debounce trailing-edge par type : annule la tâche en ATTENTE du même type et en recrée une
    (create_background_task, nom 'unified-rebuild-<type>') qui dort `delay` puis reconstruit ce type
    (même _attempt que rebuild_all : session neuve + run_with_retry). Une reconstruction DÉJÀ en
    cours n'est jamais annulée : un nouvel appel en programme une autre après. Type hors
    GROUP_MEDIA_TYPES -> no-op. Échec -> logger.error, jamais levé (le live path est le repli)."""

async def flush_scheduled() -> None
    """Exécute immédiatement les reconstructions en attente et attend celles en cours (fin de lot, tests)."""
```
`rebuild_all` (`unified_group_service.py:103-134`) est refactoré pour partager `_rebuild_one(media_type, session_factory)` ; comportement inchangé. Arrêt : les tâches en attente sont annulées par `cancel_all_background_tasks` (snapshot guéri au prochain pipeline, `main.py:153`).

## D10 — Routes admin (UI, `app/api/admin.py`, montage Basic Auth existant `main.py:628`, CSRF POST existant)

`{sid}` = `server_id`, `{rk}` = `rating_key`. Toute réponse d'écriture ré-affiche via `load_row` (session neuve).

| Méthode | Chemin | Paramètres | Réponse | Vague |
|---|---|---|---|---|
| GET | `/admin` | `type=movie\|show`, `ids: IdFilter`, `search`, `sort`, `page`, `page_size`, `tab=catalogue\|review` | `index.html` | W4 |
| GET | `/admin/catalogue` | idem sans `tab` | `_media_table.html` | W4 |
| GET | `/admin/movies` | **alias** : `missing_imdb`/`missing_tmdb` → `ids` (`missing_imdb`, `missing_tmdb`, les deux → `incomplete`, aucun → `all`), `type=movie` | `_media_table.html` | W4 |
| GET | `/admin/stats` · `/admin/movies/stats` (alias) | — | `_stats.html` (`TypeStats` × 2 + bandeau outage) | W4 |
| GET | `/admin/media/{sid}/{rk}/poster` | `which=xtream\|current` | octets image (`FetchedImage`), `Cache-Control: private, max-age=86400`, `Content-Encoding: identity` ; 404 si pas d'URL/échec. URL lue en DB (`thumb_url`/`resolved_thumb_url`), **jamais** fournie par le client | W3 |
| POST | `/admin/media/{sid}/{rk}/apply` | form `tmdb_id`, `imdb_id`, `type`, `force`, `propagate` | 200 : `_media_row.html` (outerHTML `#row-…`) + `<div id="scrape-panel" hx-swap-oob="true"></div>` + `HX-Trigger: refresh-stats, refresh-review` · 409 conflit / 422 `provider_not_found` : fragment d'erreur du panneau (bouton « Forcer ») · 404 | W2 (row) / W4 (OOB) |
| POST | `/admin/media/{sid}/{rk}/unlock` | — | `_media_row.html` + `HX-Trigger: refresh-stats` | W2 |
| POST | `/admin/media/{sid}/{rk}/clear` | form `lock` | idem | W2 |
| POST | `/admin/media/{sid}/{rk}/rescrape` | — | `_media_row.html` (message « verrouillé » si `locked`) + `HX-Trigger: refresh-stats` | W2 |
| POST | `/admin/movies/{rk}/ids` · `/admin/movies/{rk}/rescrape` | form `server_id` (+ ids) | **conservés** (compat), branchés sur L10 / L8 | W1 |
| GET | `/admin/media/{sid}/{rk}/scrape` | — | `_scrape_panel.html` (posters Xtream/actuel via proxy, formulaire de recherche prérempli, saisie d'id) | W4 |
| GET | `/admin/media/{sid}/{rk}/candidates` | `title`, `year`, `type`, `provider` | `_scrape_candidates.html` (réseau ; `hx-indicator`) | W4 |
| POST | `/admin/media/{sid}/{rk}/lookup` | form `raw_id`, `type` | `_scrape_candidates.html` (1 carte) · 422 id illisible | W4 |
| GET | `/admin/review` | `type`, `page`, `page_size` | `_review_list.html` (candidats lus dans `candidates_json`, **0 réseau**) | W5 |
| POST | `/admin/review/{sid}/{rk}/apply` | form `candidate_index` | ligne review vidée + `HX-Trigger: refresh-stats, refresh-review` ; 409/422 comme `apply` | W5 |
| POST | `/admin/review/{sid}/{rk}/dismiss` | — | idem | W5 |
| POST | `/admin/scrape-batch` | form `type`, `dry_run`, `max_items` | 202 `_scrape_batch_status.html` · **409** si un lot tourne (même fragment + message) | W5 |
| GET | `/admin/scrape-batch/status` | `job_id` (optionnel → dernier) | `_scrape_batch_status.html` ; `hx-trigger="every 2s"` rendu **seulement** si `status ∈ {queued, running}` | W5 |
| POST | `/admin/scrape-batch/{job_id}/cancel` | — | `_scrape_batch_status.html` | W5 |

Templates : `_movie_row.html` → **renommé `_media_row.html`** (W2, id DOM `row-{sid}-{rk}`), `_movies_table.html` → `_media_table.html` (W4). `base.html` (W4) : handler `htmx:beforeSwap` autorisant le swap des statuts 409/422. `index.html` : trigger `change, keyup delay:300ms, submit` (F9).

## D11 — Configuration (`config.py`, `_safe_int`/`_safe_float`, W3/W5 ; `.env.example` en W6)

| Clé | Défaut | Rôle |
|---|---|---|
| `SCRAPE_BATCH_TMDB_LIMIT` | `3000` | appels TMDB réels max par lot (tally `ContextVar`) |
| `SCRAPE_BATCH_MAX_ITEMS` | `2000` | items max par lot |
| `SCRAPE_BATCH_CONCURRENCY` | `4` | items traités en parallèle |
| `SCRAPE_BATCH_POSTER_ONLY_AUTO` | `false` | active la règle B — **reste `false` tant que le dry-run de calibration n'a pas été lu** |
| `SCRAPE_INTERACTIVE_POSTER_CANDIDATES` | `5` | candidats dont on compare les posters (UI) |
| `SCRAPE_BATCH_POSTER_CANDIDATES` | `3` | idem en lot |
| `SCRAPE_REBUILD_DEBOUNCE_SECONDS` | `5.0` | debounce `schedule_rebuild` |
| `POSTER_PHASH_IDENTICAL` | `6` | seuil `identical` (avec dHash) |
| `POSTER_DHASH_IDENTICAL` | `10` | seuil `identical` |
| `POSTER_PHASH_CLOSE` | `12` | seuil `close` |
| `POSTER_MAX_VARIANTS` | `10` | variantes TMDB comparées par candidat |
| `POSTER_MAX_BYTES` | `5242880` | 5 Mo par image |
| `POSTER_MAX_PIXELS` | `25000000` | garde bombe de décompression |
| `POSTER_FETCH_TIMEOUT` | `8.0` | secondes |
| `POSTER_CONCURRENCY` | `8` | sémaphore global |
| `POSTER_PER_HOST_CONCURRENCY` | `2` | par hôte non-CDN |
| `POSTER_GENERIC_STDDEV` | `8.0` | écart-type sous lequel une image est un aplat |
| `POSTER_GENERIC_MIN_REPEAT` | `5` | nb d'URLs Xtream distinctes au même pHash → placeholder |

`requirements.txt` (W3) : `pillow>=10.3,<13` (F10). Pas de `imagehash`.

## D12 — Décisions annexes

- **`media.title` n'est jamais réécrit** par le scraper : il appartient au fournisseur (la synchro le réécrit à chaque `content_hash`), alimente les clés `title_…` et le préfixe `[XXX]`. Le bon titre arrive via `original_title` ; le titre affiché reste celui de Xtream. Évolution possible hors lot (colonne `title_override`).
- Changer l'unification détache l'historique Android indexé par `history_group_key` — même effet que l'enrichissement, accepté.
- Les NFO/posters déjà générés ne sont pas réécrits (`LocalStorage`, AUDIT-P6-006) : limite documentée en W6.
- Rebinding DNS sur le client image = risque résiduel accepté (piège 19e) ; `follow_redirects=True` + hook conservés.

## D13 — Vagues et Definition of Done

Chaque vague : commit(s) verts sur `develop` via `git commit --only`, `pytest -v` complet + `ruff check` verts, gate `code-reviewer` (≤ 2 cycles). Toute écriture nouvelle passe par `write_with_retry` (session neuve), jamais de réseau dedans.

| Vague | Owner | Contenu | DoD |
|---|---|---|---|
| **W0** refactors neutres | `sync-specialist` | D2 (`_score_results`/`_verdict`, `search_candidates`, `count_requests`, `title_similarity`/`year_score`, tally OMDb), D3 `get_or_fetch` + 2 wrappers, D4 `media_identity_writer` branché en `fill` | **Aucun fichier `tests/` existant modifié** ; `test_tmdb_service_mocked`, `test_enrichment_*`, `test_trailer_enrichment_fillmissing`, `test_worker_write_retry_real_lock`, `test_omdb_*` verts ; nouveaux tests : `_best_match` ≡ `_verdict(_score_results)` sur une table de cas, `build_identity_values(fill)` == dict pré-refacto (fixture golden), tally compte les retries et les tâches `gather`, `get_or_fetch` ne fait aucune écriture |
| **W1** verrou | `db-migration-specialist` (026 + ORM) puis `sync-specialist` (L1-L10) | D7 | 026 rejouée 2× (DB neuve + DB existante) ; `INSERT` brut sans les colonnes OK ; ligne verrouillée ignorée par L1-L3 ; sync préserve les 5 colonnes quand `content_hash` change (L4) ; NFO `overwrite=True` ne touche pas ids/images verrouillés (L5) ; L6/L7 sautent ; `/api/media/{rk}/rescrape` → 409 ; PATCH recalcule unification + verrouille ; PATCH `{}` inchangé |
| **W2** application | `backend-developer` | D6 `apply_candidate`/`lookup` (sans poster)/`unlock`/`clear_ids`/`load_row`, D9, routes POST W2 de D10, `_media_row.html` | tests : application complète (mode fill et replace, `XXX` conservé), conflit sans/avec `force`, propagation, snapshot reconstruit après debounce (`flush_scheduled`), `updated_at` bump ; **test sous vrai verrou WAL** (modèle `tests/test_worker_write_retry_real_lock.py`) ; aucun appel respx pendant la phase d'écriture |
| **W3** image | `backend-developer` | D5, `get_match_extras`, proxy poster, Pillow | images synthétiques : même poster redimensionné/recompressé/letterboxé → `identical`, poster différent → `different` ; SSRF IP privée bloquée (y compris via redirection) ; > 5 Mo et bombe de pixels rejetés ; content-type non image rejeté ; **respx : aucune requête vers un hôte image ne porte `api_key`/`apikey`** ; aucune URL Xtream dans `caplog` ; hash exécuté via `to_thread` |
| **W4** UI + OMDb `?s=` | `backend-developer` | `search_list`, `search_candidates` complet, routes GET/lookup/panneau, templates, alias, `beforeSwap`, trigger F9 | fragments HTML rendus ; OOB + `HX-Trigger` présents ; CSRF cross-site → 403 sur **chaque** nouveau POST (extension `test_admin_csrf.py`) ; alias `/admin/movies` et `/admin/movies/stats` ; `test_admin.py` existant vert |
| **W5** lot + review | `backend-developer` (+ `db-migration-specialist` pour 027) | D8, `decide()`, routes W5 | 027 rejouée 2× ; `decide()` table de cas couvrant chaque `reason` ; lot bout-en-bout respx (A, B off/on, review, pairing) ; 409 si lot en cours ; annulation ; **dry-run n'écrit rien** (compte des lignes avant/après sur toutes les tables) ; écriture sous vrai verrou ; orphelins purgés à la suppression de compte |
| **W6** docs | `/sync-context` | bandeau `CLAUDE.md`, §2 (4 nouveaux modules), §5.14 (flux scraper), §9 piège 21 (verrou ≠ `is_active` ; client image ≠ client TMDB), chaîne 001→027, `.env.example` (`SCRAPE_*`, `POSTER_*`) | détecteur SessionStart sans dérive ; `.env.example` couvre 100 % des nouveaux `os.getenv` |

## Conséquences

- L'opérateur peut fixer une identité en une action, et elle **tient** : ni l'enrichissement, ni la synchro, ni l'import NFO, ni les deux scripts ne la défont.
- Un seul rédacteur d'identité (`media_identity_writer`) pour le worker, le scraper et le lot : pas de troisième formule.
- Coût : 2 migrations additives, 1 dépendance explicite (Pillow, déjà présente), ~4 modules neufs ; `main.py` ne gagne qu'un `close()` (règle ≤10 lignes).
- Rupture de contrat mineure côté app : `POST /api/media/{rk}/rescrape` peut renvoyer **409** — à coordonner avec PlexHubTV.
