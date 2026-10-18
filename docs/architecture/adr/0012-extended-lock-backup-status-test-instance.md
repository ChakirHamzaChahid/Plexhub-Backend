# ADR 0012 — Verrou étendu, état de sauvegarde, instance de test

- Statut : **accepté** (2026-10-18, lot B2)
- Plan source : `docs/10-prd-metadata-api.md` (FR-009 → FR-011)
- Portée : `app/services/metadata_columns.py` (nouveau), `app/workers/sync_worker.py`, `app/scripts/{strip_titles_pollution,
  backfill_certifications,backup_db}.py`, `app/services/{trailer_service,nfo_import_service}.py`,
  `app/workers/enrichment_backfill_worker.py`, `app/cli.py`, `app/main.py`, `app/config.py`
- ADR recoupés : 0005 (verrou), 0011, `docs/ops-backup-restore.md`

## D1 — Faits du code (inventaire du 2026-10-18)
1. L'upsert de sync (`sync_worker.py:718-790`) protège sous verrou `tmdb_id`, `unification_id`, `history_group_key`,
   `resolved_*`, `summary`, `genres`, `year` ; il réécrit `title`, `title_sortable`, `duration`, `content_rating`,
   `youtube_trailer`, `display_rating`.
2. Écrivains qui ignorent le verrou : `strip_titles_pollution` (films/séries **et épisodes**, `:322-331`, `:383-387`),
   `backfill_certifications` (`:209-216`), `trailer_service.write_back_youtube_trailer` (`:261-265`), trailers de
   séries du sync (`:1630-1638`), `enrichment_backfill_worker` (`:311-315`), NFO (`:499-553` : seuls 4 ids/images sont
   protégés, et seulement en overwrite ; en fill-missing un id effacé est re-rempli), `cli recompute-unification`,
   `category_service.update_media_adult_flags` (`:526-534`), quatre `delete(Media)` de la synchro.
3. Écrivains qui honorent le verrou : `enrichment_worker`, `validate_id_consistency`, `dedup_resolved_twins`, scraper
   manuel (sauf `source == "manual"`), `rescrape`. Aucun test ne fige l'inventaire.
4. L'état de sauvegarde n'existe qu'en mémoire du master (`job_health`) ; `integrity_check` tourne sur le `.tmp` avant
   `os.replace` ; `AsyncIOScheduler()` sans fuseau (⇒ `tzlocal`).
5. `import fcntl` non gardé (`main.py:279`) : boot impossible sous Windows ; `POST /api/accounts` lance
   `sync_account` indépendamment du lifespan.

## D2 — Décisions

### D2.1 Colonnes protégées
`METADATA_WRITABLE_COLUMNS` (module pur) : `title, title_sortable, original_title, summary, tagline, genres, studio,
country, content_rating, year, premiered, status, duration, imdb_id, tmdb_id, tvdb_id, wikidata_id, imdb_rating,
imdb_votes, tmdb_rating, tmdb_votes, cast, cast_json, youtube_trailer` + dérivés `unification_id, history_group_key,
display_rating`. Source unique de B2 (validation, lecture, révision) et des écrivains.

### D2.2 Écrivains
- Upsert de sync : la boucle `case((match_locked == True, <actuelle>), else_=<expression>)` couvre toute colonne de
  l'ensemble protégé présente dans la ligne (garde `if col in set_`, les clés varient selon le type).
- Sautent les lignes verrouillées : `strip_titles_pollution` (films, séries, épisodes), `backfill_certifications`,
  `write_back_youtube_trailer` et trailers de séries du sync, `enrichment_backfill_worker` (notes), NFO (**toutes**
  colonnes protégées, overwrite **et** fill-missing), `cli recompute-unification`.
- **Exceptions assumées** (documentées, testées comme telles) : (a) `update_media_adult_flags` — la classification adulte
  prime, B2 refuse déjà `contentRating` sur un adulte ; (b) `delete(Media)` de la synchro — un titre retiré du
  fournisseur disparaît, l'historique `metadata_edits` reste lisible ; (c) `recompute_display_rating_stmt` — dérivé de
  notes protégées, donc cohérent.
- **Test d'inventaire** : analyse statique de `app/` (`update(Media)`, `delete(Media)`, `insert(Media)`/
  `sqlite_insert(Media)`, SQL brut `UPDATE media`) contre une liste blanche `(fichier, fonction, justification)` ; un
  écrivain non listé fait échouer le test. Plus un test de comportement par écrivain sous verrou (le témoin « non
  protégé » de `test_manual_scrape_lock.py:208-210` passe de `title` à `thumb_url`).
- Conséquence : une ligne verrouillée par le scraper manuel suit désormais le fournisseur **un peu moins** (titre,
  durée, certification, trailer) — voulu (décision 2 de Chakir).

### D2.3 État de sauvegarde
Pas de table (la restauration ferait régresser l'état). `perform_backup` écrit, avant `os.replace`, un sidecar
`plexhub-<stamp>.db.verified` (JSON : `name`, `size`, `mtime_ns`, `integrity:"ok"`) et un fichier d'état
`plexhub-backup-state.json` (dernière tentative : instant, résultat) dans le dossier de sauvegarde ; élagage et
balayage des orphelins étendus aux sidecars. `GET /backup/status` : liste des `plexhub-*.db` (hors `.tmp`), plus récent
par le nom, `stat()`, sidecar concordant ⇒ `integrityCheck:"ok"`, sinon `"unknown"` ; `lastSuccessAt` toujours l'instant
du fichier présent (X6) ; `null` si aucun fichier ; **jamais d'`integrity_check` à la requête**, jamais de chemin ni de
nom de fichier. Sauvegarde antérieure ⇒ `unknown` ; `python -m app.scripts.backup_db --verify-latest` (une fois,
opérateur) crée le sidecar de la plus récente après `integrity_check` en thread. `scheduleHourLocal = BACKUP_HOUR`,
`scheduleTimezone` = nom `tzlocal` (repli `UTC±hh:mm`), calculés sans dépendre du scheduler (donc aussi sur un esclave).

### D2.4 `BACKGROUND_JOBS_ENABLED`
Booléen d'environnement, défaut `true` (production inchangée). `false` ⇒ `fcntl` non importé (import déplacé dans le
`try` de l'élection), `is_master=false`, pas de scheduler, pas de pipeline initial, pas de worker de téléchargement,
pas de reap Plex ; l'auto-provision Xtream reste commandée par ses variables. `POST /api/accounts` lance toujours sa
synchro. Documenté au runbook ; lu par `config.py`, présent dans `.env.example` (invariant testé).

## D3 — Alternatives écartées
- Table `backup_verification` : régresse à la restauration. - `integrity_check` à la requête : bloquant, très long.
- Protéger aussi le marquage adulte : annulerait une classification de sûreté.
- Réglage partiel (couper seulement `sync_enrich_generate`) : `db_backup`/`health_check` agiraient sur `data/`.

## D4 — Tests exigés
Par écrivain : ligne verrouillée avec changement de `content_hash` ⇒ **aucune** colonne protégée modifiée, ligne libre
inchangée de comportement ; NFO fill-missing n'ajoute pas l'id effacé ; test d'inventaire statique ; sauvegarde réussie /
échouée / absente / supprimée après coup / remplacée / après redémarrage / sidecar orphelin / pré-B2 ; `scheduleTimezone` ;
aucun chemin dans la réponse ; lifespan avec `BACKGROUND_JOBS_ENABLED=false` sans faux `fcntl` (`instances == []`).

## D5 — Conséquences
`CLAUDE.md` §9 : piège « tout nouvel écrivain de `media` passe par `metadata_columns` et la liste blanche ».
`docs/ops-backup-restore.md` complété (sidecars). Revue `security-reviewer`.
