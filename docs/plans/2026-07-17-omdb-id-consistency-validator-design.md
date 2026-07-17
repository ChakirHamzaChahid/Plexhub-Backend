# OMDb-assisted tmdb_id/imdb_id consistency validator — design

## Problème

Le 2026-07-17 on a trouvé, en production, une classe de bug d'enrichissement : un `media.tmdb_id` et un `media.imdb_id` qui ne correspondent PAS au même titre réel — l'`imdb_id` avait été écrit par erreur avec la valeur d'une autre fiche TMDB, indépendante. Comme `unification_id` (`app/utils/unification.py::calculate_unification_id`) préfère `imdb://…`, la ligne corrompue rejoint alors le groupe unifié d'un tout autre film/série — fusion à tort de deux titres différents dans un seul dossier Plex généré (épisodes/versions mélangés, NFO avec le titre d'un titre et les métadonnées d'un autre).

Impact démontré en prod (corrigé manuellement le 2026-07-17, script ad-hoc contre le conteneur live, pas de code committé pour ce fix ponctuel) :
- **3 séries** : "Fist of the North Star: HOKUTO NO KEN" (2026) fusionnée avec "Ken le survivant" (1984) ; "Shogun - Shōgun" (source Xtream mal datée 1990) qui — cas inverse — avait été *décollée à tort* d'un vrai doublon de "Shōgun" (2024) ; "Zdeňkova akademie"/"Into the Dark" fusionnée avec "In the Dark" (2019).
- **34 lignes films** sur 26 groupes suspects : 20 réassignations (même film, mauvais `tmdb_id`), 13 découplages (films réellement différents fusionnés à tort), 2 groupes laissés volontairement non résolus (signal insuffisant : "Le saut du diable" `imdb://tt14872752`, "Barbare"/"Barbarian" `imdb://tt15791034`).

**Root cause non tracée** : rejouer l'appel TMDB isolément retourne systématiquement la bonne donnée — donc pas un bug de matching TMDB en soi, plus probablement une corruption transitoire côté cache/write-back de `enrichment_worker.py`, jamais reproduite en direct malgré plusieurs tentatives. Ce document propose (1) un détecteur+correcteur réutilisable et (2) un garde-fou défensif qui rend la classe de bug inoffensive, indépendamment de la cause exacte.

**Méthode de triage validée en prod (à industrialiser)** : pour un groupe suspect (`tmdb_id` divergent entre membres d'un même `unification_id`), le signal primaire fiable est **interne et indépendant de la langue** — recharger via TMDB (`get_movie_details`/`get_tv_details`) l'`imdb_id` réel de chaque `tmdb_id` du groupe, et comparer à l'`imdb_id` du groupe. Un membre dont le `tmdb_id` propre ne pointe PAS vers le bon `imdb_id` est suspect ; OMDb (interrogé par `imdb_id`, Titre/Année/Durée faisant autorité) ne sert alors que de **signal de repli** pour trancher merge-vs-split sur CE membre — attention : OMDb renvoie souvent le titre anglais même pour un contenu francophone (ex. "En plein cœur" (1998) vs OMDb "In All Innocence"), donc une similarité de titre faible n'est pas concluante seule ; combiner avec la durée (`Media.duration`, en ms) et ne conclure "films différents" que si titre ET durée divergent nettement.

## Composants à livrer

### 1. `app/services/omdb_service.py` (nouveau)

Miroir architectural de `app/services/tmdb_service.py` :
- Client `httpx.AsyncClient` singleton (mêmes paramètres de pool), retry/backoff sur `httpx.TimeoutException/ConnectError/RemoteProtocolError` + 429 (`Retry-After`) + 5xx uniquement (502/503/504), échelle `(1, 2, 4)` s comme `tmdb_service._request` (lignes 174-220 de ce fichier).
- `is_configured` (`bool(settings.OMDB_API_KEY)`), gate sur chaque méthode publique — retour `None`/vide plutôt qu'exception si non configuré.
- Méthode principale : `async def get_by_imdb_id(imdb_id: str) -> OMDbData | None` — usage = *validation*, on a toujours déjà un `imdb_id` candidat en main, jamais de recherche par titre dans ce flux.
- `get_request_count()`/`reset_request_count()` — même pattern de budgétisation que TMDB (incrémenté à CHAQUE tentative HTTP réelle, retries inclus).
- Dataclass `OMDbData` : `title: str`, `year: str`, `runtime_minutes: int | None`, `genre: str | None`, `director: str | None`, `actors: str | None`, `plot: str | None`, `imdb_rating: float | None`, `imdb_votes: int | None`, `type: str` (`movie`/`series`).

Endpoint : `GET http://www.omdbapi.com/?i=<imdb_id>&apikey=<key>` — `Response: "False"` (avec `Error`) = pas trouvé, ne pas lever d'exception, retourner `None`.

### 2. Config (`app/config.py`)

```python
OMDB_API_KEY: str = os.getenv("OMDB_API_KEY", "")
OMDB_DAILY_LIMIT: int = _safe_int("OMDB_DAILY_LIMIT", 20000)  # plan payant utilisateur : 100k/j, on garde une marge
```
+ log de statut dans `Settings.__init__` (miroir des lignes 126-129 pour `TMDB_API_KEY`, préfixe masqué).

### 3. Cache — nouvelle table `omdb_scrape_cache`

**Ne pas réutiliser `tmdb_scrape_cache`** (schéma différent, clé `title|year` inadaptée ici). Nouvelle classe `OmdbScrapeCache` dans `app/models/database.py`, miroir de `TmdbScrapeCache` (lignes 338-359) mais :
```python
class OmdbScrapeCache(Base):
    __tablename__ = "omdb_scrape_cache"
    imdb_id = Column(Text, primary_key=True)   # clé directe, pas de make_key(title, year)
    result = Column(Text, nullable=False)       # 'found' | 'not_found'
    payload = Column(Text)                       # JSON de OMDbData si found
    fetched_at = Column(BigInteger, nullable=False)
    __table_args__ = (Index("ix_omdb_scrape_cache_fetched_at", "fetched_at"),)
```
+ migration `_migration_0XX_omdb_scrape_cache` dans `app/db/migrations.py`, même forme que `_migration_010_scrape_cache` (lignes 369-413 : `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`, `try/except` qui logue un warning plutôt que de lever). TTL suggéré : 30 jours (les données OMDb changent rarement).

Service `app/services/omdb_scrape_cache_service.py` : `get(db, imdb_id, now_ms)`/`put(db, imdb_id, result, data, now_ms)`, miroir de `scrape_cache_service.py`.

### 4. `app/scripts/validate_id_consistency.py` (nouveau, réutilisable)

Mirror `app/scripts/backfill_certifications.py` (async, `argparse`, `--dry-run` par défaut, `_CONCURRENCY = 8`, `_COMMIT_BATCH`, sleep anti-429 entre lots) + `app/scripts/dedup_resolved_twins.py` (backup DB via l'API `.backup()` avant tout `--apply`). Invocation : `python -m app.scripts.validate_id_consistency --dry-run [--media-type movie|show|all] [--limit N]`.

Algorithme (celui validé en production le 2026-07-17, généralisé séries+films) :
1. Détecter les groupes suspects : `media_group`/`media_group_member` où les membres d'un même `group_key` ont des `tmdb_id` divergents (requête déjà utilisée en ad-hoc, cf. session du 2026-07-17 — triviale, pas de call réseau).
2. Pour chaque `tmdb_id` distinct du groupe : `tmdb_service.get_{movie,tv}_details(tmdb_id)` → son `imdb_id` réel. Comparer à l'`imdb_id`/`group_key` du groupe → `own_tmdb_imdb_ok: bool | None` (None si 404/tmdb_id mort).
3. Pour les membres où `own_tmdb_imdb_ok` n'est pas `True` : `omdb_service.get_by_imdb_id(group_imdb_id)` (une fois par groupe, caché) → comparer Titre (normalisé : NFKD, minuscule, tags qualité `FHD/VOST/...` retirés, `difflib.SequenceMatcher`) et Durée (`Media.duration` en ms vs `Runtime` OMDb, tolérance ±5 min) au membre.
4. Classification par membre : `CONSISTENT` (rien à faire) / `SAME_CONTENT_MISLABELED` (réassigner au `tmdb_id`/`imdb_id` correct du groupe — chercher un membre `CONSISTENT` du même groupe comme source de vérité) / `DIFFERENT_CONTENT` (découpler vers l'identité propre du membre, via son propre `tmdb_id` déjà connu — jamais de nouvelle recherche TMDB par titre dans ce flux) / `UNCERTAIN` (ni preuve suffisante ni contradictoire — ne rien faire, logguer pour revue humaine).
5. Rapport dry-run imprimé/JSON. `--apply` : `UPDATE media SET tmdb_id=.., imdb_id=.., unification_id=.., history_group_key=.., + champs riches NFO` en écrasement **inconditionnel** (ces valeurs sont fausses, pas absentes — pas de `COALESCE`) pour `SAME_CONTENT_MISLABELED`/`DIFFERENT_CONTENT` uniquement, puis `unified_group_service.rebuild(db, media_type)` + commit.
6. Après `--apply` : lister les dossiers Plex dont `unification_id` a changé, pour que l'opérateur sache lesquels vider avant la prochaine génération (cf. gotcha §6).

Invocable en ponctuel dès maintenant ; câblage en périodique (nouveau stage après `enrichment_worker.run()` dans `scheduled_sync_enrich_generate()`, `app/main.py`) laissé en option future, pas nécessaire au premier jet.

### 5. Garde-fou anti-récidive dans `app/workers/enrichment_worker.py`

Dans `_apply_enrichment_results` (lignes ~175-186), avant d'écrire `tmdb_id`/`imdb_id`/`unification_id` pour un résultat `matched` : revérifier que `enrichment_data.tmdb_id` confirme bien `enrichment_data.imdb_id` (quasi gratuit — cette paire vient déjà d'un seul appel `get_details`, donc en pratique cette vérification est TOUJOURS vraie SAUF si la corruption se produit APRÈS ce point, ce qui borne la recherche de la vraie cause si elle se reproduit). Si incohérence détectée : downgrade en `"ambiguous"` (ne pas écrire les ids, logguer un warning avec le `rating_key`) plutôt que committer une paire corrompue silencieusement.

Optionnel (coût OMDb) : pour les matches à confiance `< 1.0` (palier exact des 3 cas confirmés en séries), appeler `omdb_service.get_by_imdb_id` en tie-break avant de committer — si le titre/année OMDb contredit fortement `item.title`/`item.year`, downgrader en `"ambiguous"` plutôt que `"matched"`.

### 6. Colonnes `Media.imdb_rating`/`Media.imdb_votes` — déjà en base, jamais peuplées par l'enrichissement

`app/models/database.py` lignes 90-91 — actuellement seul `nfo_import_service` les peuple. `omdb_service`/`validate_id_consistency.py` peuvent les peupler aussi (via `COALESCE`, ne jamais écraser une valeur NFO plus riche) — bénéfice accessoire de la validation.

## Gotcha à connaître avant d'implémenter (trouvé en prod le 2026-07-17)

`app/plex_generator/storage.py::LocalStorage.write_file`/`download_image`/`submit_image_download` ne réécrivent JAMAIS un fichier déjà présent (`tvshow.nfo`/`movie.nfo`/`poster.jpg`/`fanart.jpg`) — commentaire volontaire "Preserve existing file, e.g. enriched by Tiny Media Manager". Donc après tout `--apply` qui change un `unification_id`, il faut supprimer manuellement ces fichiers pour le(s) dossier(s) concerné(s) AVANT la prochaine génération, sinon les anciennes données restent figées indéfiniment. **Piège additionnel** : le dossier régénéré n'est pas forcément celui qu'on a vidé — le nom canonique vient du membre `best` du groupe (`aggregation_service`), qui peut différer du titre qu'on avait en tête. Si après régénération le nfo attendu manque toujours au même endroit, interroger `DatabaseSource().get_movies()`/`get_series()` en direct pour savoir où le contenu a réellement atterri avant de conclure à un bug. `validate_id_consistency.py --apply` devrait donc, dans un futur incrément, automatiser aussi cette étape (supprimer + regénérer + vérifier par requête plutôt que par chemin deviné) plutôt que de laisser ça à un opérateur humain.

## Tests

- `tests/test_omdb_service.py` — mirror `tests/test_tmdb_service_mocked.py` : fixture `configured_omdb` (monkeypatch `settings.OMDB_API_KEY`), nouvelle fixture `omdb_mock` dans `conftest.py` (respx, `base_url="https://www.omdbapi.com"` ou l'URL choisie). Couvrir : match, `Response=False`, 429 + `Retry-After`, 5xx retry, comptage de requêtes à travers les retries, non-configuré → court-circuit.
- `tests/test_omdb_scrape_cache.py` — mirror la partie cache de `tests/test_enrichment_scraping.py` (roundtrip get/put, expiration).
- `tests/test_validate_id_consistency.py` — seed de rows connues bonnes/mauvaises dans `db_session`, assert détection + comportement dry-run vs `--apply`, dans le style "fake service double" (`_Fake()`/`_Boom()`) déjà utilisé par `tests/test_enrichment_scraping.py`, pas `unittest.mock.AsyncMock`.
- Test de régression pour le garde-fou §5 dans `enrichment_worker.py` : un `_Fake()` tmdb_service qui retourne délibérément un `tmdb_id`/`imdb_id` incohérents, assert que le résultat est downgradé en `ambiguous` et que rien n'est écrit.

## Hors scope de ce document

- L'état git du repo au moment de l'écriture de ce doc (`main` local vs `origin/main`/`origin/develop`) évolue vite (plusieurs agents y travaillent en parallèle) — se resynchroniser (`git fetch --all` puis vérifier `git log --oneline -10` sur `develop`) avant de brancher ce travail, ne pas se fier à un état constaté dans une session précédente.
- Câblage en périodique dans le pipeline planifié (`app/main.py::scheduled_sync_enrich_generate`) — à décider séparément une fois le script validé en ponctuel.
