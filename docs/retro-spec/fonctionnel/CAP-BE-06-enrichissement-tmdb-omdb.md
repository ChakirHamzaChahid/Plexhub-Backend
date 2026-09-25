# CAP-BE-06 — Enrichissement métadonnées TMDB + OMDb

**Reconstituée le** : 2026-09-25 · **HEAD** : `313778e` · **Sources** : code, tests, `CLAUDE.md` (§2, §9 pièges 21 et 22), `docs/architecture/flux-cles.md` (§5.2 et pipeline planifié), ADR `docs/architecture/adr/0003-dual-provider-enrichment-and-blended-display-rating.md`, plan `docs/plans/2026-07-20-omdb-rating-enrichment-design.md`, `.env.example`, app TV (lecture seule, preuves `tv:`). Aucun PRD `docs/10-prd-*.md` ne couvre cette capacité (les deux PRD traitent des téléchargements) : pas de tableau d'écart fiche ↔ PRD ; les écarts sont face à `CLAUDE.md`, `flux-cles.md`, l'ADR 0003 et `.env.example`.
**Périmètre** : `app/workers/enrichment_worker.py` (file et résolution), `app/workers/enrichment_backfill_worker.py` (rattrapage OMDb) ; services `tmdb_service`, `omdb_service`, `scrape_cache_service`, `omdb_scrape_cache_service`, `media_identity_writer` (mode `fill`), `app/utils/rating_blend.py` ; alimentation de la file par `sync_worker.enqueue_for_enrichment` ; endpoints `POST /api/sync/enrichment`, `POST /api/admin/enrichment/omdb-backfill`, `GET /api/admin/enrichment/jobs/{job_id}`, `PATCH /api/media/{rating_key}`, `POST /api/media/{rating_key}/rescrape` ; étape du job planifié `sync_enrich_generate`. Hors périmètre, cités seulement comme interface : scraper manuel et lot de scraping (`CAP-BE-22`), import NFO (`CAP-BE-23`), recommandations IA (`CAP-BE-13`). Renvoi : `docs/retro-spec/_genere/inventaire.md`.

## Résumé

À chaque synchronisation, tout film ou série qui n'a pas à la fois un `tmdb_id` et un `imdb_id` entre dans `enrichment_queue`. `enrichment_worker.run()`, lancé par le pipeline planifié du maître ou à la main, résout chaque entrée : d'abord le cache persistant `tmdb_scrape_cache`, puis une chaîne de recherches TMDB notées (titre 70 %, année 30 %, seuils anti-ambiguïté, départage par le résumé Xtream). Il fait ensuite **un seul** appel OMDb par entrée, qui sert à la fois au contrôle de contradiction, aux notes IMDb et au repli par titre quand TMDB ne trouve rien. Il écrit l'identité, les métadonnées riches et un `display_rating` mélangé (moyenne IMDb/TMDB), sans jamais toucher une ligne verrouillée à la main. Un rattrapage OMDb à la demande (clé maître) complète les notes IMDb des lignes déjà identifiées. Personas : planificateur (système), opérateur. La TV ne déclenche rien : elle reçoit les résultats dans les réponses catalogue.

## User stories observées

- **US-BE-06.1** (P1) — En tant que planificateur, j'identifie et j'enrichis automatiquement chaque film ou série synchronisé, afin que le catalogue servi à la TV porte ids, résumé, affiches et notes.
  - Parcours : sync → `enqueue_for_enrichment` (`app/workers/sync_worker.py:801`) → `enrichment_worker.run()` (`app/main.py:342`) → `UPDATE media` · Maestro : sans objet
- **US-BE-06.2** (P1) — En tant que spectateur TV (indirect), je vois une note qui combine IMDb et TMDB quand les deux existent.
  - Parcours : `_attach_omdb` → `build_rating_values` → `display_rating` ; la TV recalcule le même mélange (`tv:data/src/main/java/com/chakir/plexhubtv/data/mapper/BackendMediaMapper.kt:82`) · Maestro : aucun
- **US-BE-06.3** (P2) — En tant qu'opérateur, je relance l'enrichissement sans attendre la prochaine passe planifiée.
  - Parcours : `POST /api/sync/enrichment` → 202 + `jobId` → `GET /api/sync/status/{jobId}` · Maestro : sans objet
- **US-BE-06.4** (P2) — En tant qu'opérateur, je complète après coup les notes IMDb des titres déjà identifiés.
  - Parcours : `POST /api/admin/enrichment/omdb-backfill` → 202 → `GET /api/admin/enrichment/jobs/{jobId}` · Maestro : sans objet
- **US-BE-06.5** (P2) — En tant qu'opérateur, je remets un titre dans la file, ou je corrige ses ids à la main pour que l'automatisme ne les écrase plus.
  - Parcours : `POST /api/media/{rk}/rescrape?server_id=` ou `PATCH /api/media/{rk}?server_id=` · Maestro : sans objet
- **US-BE-06.6** (P3) — En tant que planificateur, je ne dépense pas le budget d'appels sur des titres cachés par le filtre de catégories.
  - Parcours : `_media_is_category_visible()` dans la sélection · Maestro : sans objet

## Exigences fonctionnelles

### Alimentation de la file (`enrichment_queue`)

- **FR-BE-337** — À la synchronisation, toute ligne `movie` ou `show` à laquelle il manque `tmdb_id` ou `imdb_id` est insérée dans `enrichment_queue` (statut `pending`, `attempts` 0) avec titre, année, ids déjà connus et résumé Xtream (`existing_summary`), par paquets de 200. Les épisodes et les chaînes n'y entrent jamais. — *implémenté* — preuve `app/workers/sync_worker.py:801-824` · test : aucun sur `enqueue_for_enrichment`
- **FR-BE-338** — Un nouveau passage sur la même `(rating_key, server_id)` ne met à jour que `existing_tmdb_id`, `existing_imdb_id` et `existing_summary` : `status` et `attempts` ne sont pas remis à zéro, donc une entrée `done` ou définitivement `skipped` n'est jamais retentée par une simple synchronisation, même si son titre change. — *implémenté* (comportement constaté) — preuve `app/workers/sync_worker.py:826-837` · test : aucun
- **FR-BE-339** — Les entrées de file dont la ligne `Media` a disparu pour ce compte sont supprimées à chaque synchronisation. — *implémenté* — preuve `app/workers/sync_worker.py:935-954` · test `tests/test_enrichment_scraping.py:584` (au niveau de la requête de sélection)
- **FR-BE-340** — `POST /api/media/{rating_key}/rescrape?server_id=` remet l'entrée à `pending` (ou la crée) et répond 202 `{"status":"queued"}` ; 404 si la ligne `Media` n'existe pas. — *implémenté* — preuve `app/api/media.py:540-554`, `app/services/media_service.py:735-787` · test `tests/test_manual_scrape_lock.py:527`
- **FR-BE-341** — Le rescrape d'une ligne verrouillée (`match_locked`) est refusé en 409, sans rien écrire. — *implémenté* — preuve `app/services/media_service.py:752-753`, `app/api/media.py:550-553` · test `tests/test_manual_scrape_lock.py:558`
- **FR-BE-342** — `PATCH /api/media/{rating_key}?server_id=` ne retient que `imdb_id` et `tmdb_id` du corps, recalcule `unification_id`/`history_group_key` et pose le verrou manuel ; 404 si la ligne est inconnue. Détail du verrou : `CAP-BE-22`. — *implémenté* — preuve `app/api/media.py:518-537`, `app/services/media_service.py:651-733` · test : aucun au niveau HTTP dans ce périmètre

### Sélection à l'exécution

- **FR-BE-343** — `run()` traite d'abord les films (phase 1), puis les séries (phase 2) avec le budget restant, par ordre d'entrée dans la file (`created_at`), en lots de 200 écrits en une transaction chacun. — *implémenté* — preuve `app/workers/enrichment_worker.py:619-716` · test `tests/test_enrichment_scraping.py:480`
- **FR-BE-344** — Sont sélectionnées les entrées `pending` et les entrées `skipped` à moins de 3 tentatives (`MAX_ATTEMPTS`) ; au-delà, l'entrée reste hors sélection pour toujours (seul un rescrape la remet en jeu). `attempts` augmente à chaque passage, succès compris. — *implémenté* — preuve `app/workers/enrichment_worker.py:29`, `app/workers/enrichment_worker.py:625-629`, `app/workers/enrichment_worker.py:590` · test : aucun sur la borne de 3
- **FR-BE-345** — La sélection écarte les entrées dont la ligne `Media` est hors des catégories autorisées (`is_in_allowed_categories`), pour ne pas épuiser le budget sur des titres invisibles dans l'app. — *implémenté* — preuve `app/workers/enrichment_worker.py:32-56`, `app/workers/enrichment_worker.py:631`, `app/workers/enrichment_worker.py:688` · test `tests/test_enrichment_scraping.py:553`
- **FR-BE-346** — La sélection écarte les lignes verrouillées à la main (`match_locked`). — *implémenté* — preuve `app/workers/enrichment_worker.py:59-77`, `app/workers/enrichment_worker.py:632`, `app/workers/enrichment_worker.py:689` · test `tests/test_manual_scrape_lock.py:55`
- **FR-BE-347** — L'`UPDATE media` revérifie `match_locked == False` : une ligne verrouillée entre la sélection et l'écriture n'est pas modifiée (0 ligne touchée). L'entrée de file, elle, passe quand même à `done` ou `skipped`. — *implémenté* — preuve `app/workers/enrichment_worker.py:574-588`, `app/workers/enrichment_worker.py:520` · test `tests/test_manual_scrape_lock.py:122`
- **FR-BE-348** — Aucune exclusion des titres adultes : l'enrichissement automatique traite les lignes `is_adult` comme les autres (l'exclusion de la v1.10.6 ne concerne que le lot de scraping de `CAP-BE-22`). — *implémenté* (comportement constaté, voir Questions ouvertes) — preuve `app/workers/enrichment_worker.py:622-633`

### Résolution TMDB

- **FR-BE-349** — Quatre cas selon les ids déjà connus : les deux → jamais en file ; `tmdb_id` seul → lecture directe des détails TMDB (confiance 1.0) ; `imdb_id` seul → aucune recherche TMDB, seules les notes OMDb sont cherchées ; aucun → cache puis recherche. — *implémenté* — preuve `app/workers/enrichment_worker.py:164-204`, `app/workers/sync_worker.py:810-811` · test : aucun par cas
- **FR-BE-350** — Cache persistant `tmdb_scrape_cache`, clé `type|titre normalisé|année`, valable 30 jours pour une correspondance et 3 jours pour un échec ; il évite de rappeler TMDB pour le même titre d'un compte à l'autre et après redémarrage. Les écritures sont dédupliquées par clé dans un lot. — *implémenté* — preuve `app/services/scrape_cache_service.py:25-26`, `app/workers/enrichment_worker.py:189-193`, `app/workers/enrichment_worker.py:489-494` · test `tests/test_enrichment_scraping.py:33`
- **FR-BE-351** — Sur un défaut de cache, la chaîne de recherche est : titre + année → sans année → `en-US` → `/search/multi` ; elle s'arrête à la première correspondance automatique, sinon garde la meilleure note. — *implémenté* — preuve `app/workers/enrichment_worker.py:132-161` · test : aucun sur l'ordre
- **FR-BE-352** — Note de correspondance = 0,7 × similarité de titre + 0,3 × score d'année (rapidfuzz sur titre localisé et titre original). — *implémenté* — preuve `app/services/tmdb_service.py:49-50`, `app/services/tmdb_service.py:690-743` · test `tests/test_tmdb_score_verdict_split.py:55`
- **FR-BE-353** — Correspondance automatique si note ≥ 0,85 ET titre ≥ 0,90 ET écart ≥ 0,05 avec le deuxième candidat ; si l'écart est trop faible, le résumé Xtream départage (similarité ≥ 0,30 et avance ≥ 0,10). — *implémenté* — preuve `app/services/tmdb_service.py:51-57`, `app/services/tmdb_service.py:745-784` · test `tests/test_tmdb_score_verdict_split.py:64`
- **FR-BE-354** — Un résultat TMDB au `tmdb_id` non positif ou à l'`imdb_id` hors forme `tt\d+` est déclassé en `ambiguous` : rien n'est écrit. — *implémenté* — preuve `app/workers/enrichment_worker.py:343-355`, `app/workers/enrichment_worker.py:463-472` · test `tests/test_enrichment_guard.py:97`
- **FR-BE-355** — Un 404 TMDB (id périmé fourni par le fournisseur) donne `not_found` avec un log `INFO` sans trace ; toute autre erreur donne `skipped` avec un log `WARNING`. Les deux restent retentables. — *implémenté* — preuve `app/workers/enrichment_worker.py:309-322`, `app/workers/enrichment_worker.py:552-553` · test : aucun
- **FR-BE-356** — Une correspondance TMDB sans `imdb_id` est écrite (`tmdb_id` seul) et l'entrée passe à `done` : l'enrichissement automatique ne cherche pas l'`imdb_id` manquant chez OMDb. Ce repli (v1.10.7) n'existe que dans le lot de scraping de `CAP-BE-22`. — *partiel* (manque : repli OMDb de l'`imdb_id` dans le pipeline automatique) — preuve `app/workers/enrichment_worker.py:269-281`, `app/workers/enrichment_worker.py:511-520`
- **FR-BE-357** — Les titres sont nettoyés à la synchronisation, avant d'entrer en file (préfixes pays, balises de qualité dont `FHD` depuis la v1.10.7). — *implémenté* — preuve `app/utils/string_normalizer.py:69-73`, `app/workers/sync_worker.py:112` · test : aucun dans ce périmètre

### OMDb : un seul appel, trois usages

- **FR-BE-358** — Un seul appel OMDb par entrée, sous le même sémaphore que TMDB (8 en parallèle) : il sert au contrôle de contradiction ET aux notes, jamais un second appel. — *implémenté* — preuve `app/workers/enrichment_worker.py:28`, `app/workers/enrichment_worker.py:258-277`, `app/workers/enrichment_worker.py:301-332` · test `tests/test_enrichment_scraping.py:435`
- **FR-BE-359** — Avec un `imdb_id` en main, OMDb est lu par id, cache `omdb_scrape_cache` d'abord (30 jours trouvé, 3 jours non trouvé), puis réseau sous garde de budget ; l'écriture en cache est faite par l'appelant et dédupliquée par `imdb_id` dans un lot. — *implémenté* — preuve `app/services/omdb_scrape_cache_service.py:29-30`, `app/services/omdb_scrape_cache_service.py:85-119`, `app/workers/enrichment_worker.py:496-503` · test `tests/test_enrichment_guard.py:441`
- **FR-BE-360** — Repli par titre OMDb uniquement après un échec TMDB **frais** (un échec lu en cache ne relance pas OMDb). Classement : `discard` sous 0,60 de similarité (rien écrit) ; `strong` si année exacte ET similarité ≥ 0,90 ET type concordant (identité écrite) ; `weak` sinon (métadonnées et notes, jamais d'identité). — *implémenté* — preuve `app/workers/enrichment_worker.py:96-97`, `app/workers/enrichment_worker.py:239-255`, `app/workers/enrichment_worker.py:279-298` · test `tests/test_enrichment_scraping.py:363`
- **FR-BE-361** — Contrôle de contradiction : une correspondance TMDB de confiance < 1,0 est déclassée en `ambiguous` seulement si OMDb donne un écart d'année > 1 ET une similarité de titre < 0,55 ; tout cas non concluant garde la correspondance. — *implémenté* — preuve `app/workers/enrichment_worker.py:87-88`, `app/workers/enrichment_worker.py:367-403`, `app/workers/enrichment_worker.py:473-480` · test `tests/test_enrichment_guard.py:132`
- **FR-BE-362** — Identité `strong` venue d'OMDb : écrit `imdb_id`, `unification_id`, `history_group_key` et les métadonnées OMDb en complément ; l'entrée passe à `done`. — *implémenté* — preuve `app/workers/enrichment_worker.py:522-536` · test `tests/test_enrichment_guard.py:396`
- **FR-BE-363** — Cas `imdb_id` seul : l'entrée passe à `done` si OMDb a répondu, sinon `skipped` (3 essais au plus). — *implémenté* — preuve `app/workers/enrichment_worker.py:547-553` · test : aucun
- **FR-BE-364** — OMDb est en échec ouvert : clé absente, budget épuisé, id inconnu ou exception → le résultat TMDB est gardé tel quel. — *implémenté* — preuve `app/workers/enrichment_worker.py:282-286`, `app/workers/enrichment_worker.py:324-331` · test `tests/test_enrichment_scraping.py:299`
- **FR-BE-365** — La clé OMDb ne paraît jamais dans les journaux : seuls le type d'exception ou le statut HTTP sont écrits, jamais le texte de l'exception (qui contient l'URL avec la clé). — *implémenté* — preuve `app/services/omdb_service.py:18`, `app/services/omdb_service.py:331`, `app/services/omdb_service.py:403` · test : aucun sur le contenu des logs

### Écriture en base

- **FR-BE-366** — Correspondance TMDB (mode `fill` de l'écrivain partagé) : identité et `tmdb_match_confidence` toujours écrits ; `summary`, `genres`, affiche, fond, `scraped_rating`, `year`, `cast` écrasés dès que TMDB fournit une valeur ; `content_rating`, `original_title`, `tagline`, `premiered`, `status`, `studio`, `country`, `tvdb_id`, `wikidata_id`, `tmdb_rating`, `tmdb_votes`, `cast_json`, `youtube_trailer` en complément seulement (`COALESCE`). — *implémenté* — preuve `app/services/media_identity_writer.py:123-173`, `app/workers/enrichment_worker.py:517-519` · test `tests/test_media_identity_writer.py:71`
- **FR-BE-367** — Les notes IMDb (`imdb_rating`, `imdb_votes`) sont écrites en complément : elles n'écrasent jamais une valeur déjà posée, par exemple par l'import NFO. — *implémenté* — preuve `app/services/media_identity_writer.py:298-316` · test `tests/test_enrichment_guard.py:266`
- **FR-BE-368** — `display_rating` = moyenne d'IMDb et TMDB si les deux sont présents, sinon la seule présente, sinon inchangé ; une note ≤ 0 compte comme absente. Calcul identique en Python et en SQL. — *implémenté* — preuve `app/utils/rating_blend.py:26-61` · test `tests/test_rating_blend.py:43`
- **FR-BE-369** — Fin de `run()` : une passe SQL recalcule `display_rating` de toutes les lignes `movie`/`show` qui ont au moins une note, pour réparer une note écrasée par la synchronisation ; son échec est journalisé et n'arrête pas le pipeline. — *implémenté* — preuve `app/workers/enrichment_worker.py:723-748`, `app/utils/rating_blend.py:64-81` · test `tests/test_enrichment_scraping.py:480`
- **FR-BE-370** — Durée d'une série : l'`episode_run_time` TMDB n'est écrit sur la ligne **série** que si sa durée est absente ou sous 5 minutes. Cette valeur sert ensuite de plancher aux épisodes unifiés, et seulement quand aucune source de l'épisode ne rapporte de durée. — *implémenté* — preuve `app/services/media_identity_writer.py:83-101`, `app/services/aggregation_service.py:49`, `app/services/aggregation_service.py:78-81`, `app/api/media.py:418-420` · test `tests/test_duration_consolidation.py:39`
- **FR-BE-371** — La bande-annonce YouTube vient de la réponse de détails TMDB déjà obtenue, sans appel de plus. — *implémenté* — preuve `app/services/tmdb_service.py:433-459`, `app/services/media_identity_writer.py:166` · test : aucun côté enrichissement
- **FR-BE-372** — Après chaque passe, les jauges `enrichment_queue_size{status}` reprennent le compte par statut de la file. — *implémenté* — preuve `app/workers/enrichment_worker.py:750-759` · test : aucun

### Budget et fiabilité réseau

- **FR-BE-373** — Le budget TMDB compte les requêtes HTTP réelles, nouvelles tentatives comprises ; une phase s'arrête entre deux lots quand `ENRICHMENT_DAILY_LIMIT` (50 000 par défaut) est atteint, et les entrées non traitées restent en file. — *implémenté* — preuve `app/services/tmdb_service.py:326-334`, `app/workers/enrichment_worker.py:642-644`, `app/config.py:111` · test : aucun sur l'arrêt
- **FR-BE-374** — Les deux budgets (`ENRICHMENT_DAILY_LIMIT`, `OMDB_DAILY_LIMIT` 95 000 par défaut) sont des compteurs du processus remis à zéro au début de chaque `run()` : ce ne sont pas des quotas sur 24 heures. — *partiel* (manque : quota quotidien persistant ; résidu CR-F03 assumé dans le code) — preuve `app/services/tmdb_service.py:295-304`, `app/workers/enrichment_worker.py:609-612`, `app/config.py:38` · test : aucun
- **FR-BE-375** — TMDB et OMDb : jusqu'à 3 nouvelles tentatives (1 s, 2 s, 4 s), en-tête `Retry-After` suivi sur un 429, nouvelle tentative sur 502/503/504, les autres 4xx remontés tout de suite. — *implémenté* — preuve `app/services/tmdb_service.py:317-366`, `app/services/omdb_service.py:237-290` · test : aucun dans ce périmètre
- **FR-BE-376** — Sans `TMDB_API_KEY`, aucune requête TMDB n'est faite (entrées `skipped`) ; le démarrage le signale en `WARNING`. — *implémenté* — preuve `app/services/tmdb_service.py:285-287`, `app/workers/enrichment_worker.py:185-186`, `app/config.py:417-420` · test : aucun
- **FR-BE-377** — La clé TMDB ne doit pas paraître dans les journaux. Or une erreur HTTP TMDB autre que 404 est journalisée avec le texte de l'exception et sa trace, qui contiennent l'URL de la requête, clé comprise (la clé est un paramètre de requête ajouté par le client). — *partiel* (manque : journaliser le type et le statut seulement, comme `omdb_service`) — preuve `app/services/tmdb_service.py:272`, `app/workers/enrichment_worker.py:318`, `app/workers/enrichment_worker.py:321` · test : aucun
- **FR-BE-378** — La passe tient une seule session du **pool de l'API** (`async_session_factory`) de bout en bout, et ses commits de lot ne retentent pas (lecture-modification-écriture sur objets attachés, choix documenté). — *partiel* (manque : pool dédié aux workers, comme le prévoit `worker_engine`) — preuve `app/workers/enrichment_worker.py:12`, `app/workers/enrichment_worker.py:619`, `app/workers/enrichment_worker.py:656-672` · test : aucun

### Déclenchement

- **FR-BE-379** — Le job planifié `sync_enrich_generate` (maître seulement, toutes les `SYNC_INTERVAL_HOURS`, une instance à la fois, rattrapage fusionné) enchaîne synchronisation → enrichissement → validation → génération → instantané ; un premier passage est lancé au démarrage du maître. Chacun saute son tour si `_PIPELINE_LOCK` est déjà tenu. — *implémenté* — preuve `app/main.py:330-365`, `app/main.py:523-532` · test : aucun dans ce périmètre (détail : `CAP-BE-05`)
- **FR-BE-380** — `POST /api/sync/enrichment` (clé `X-API-Key`) lance `run()` en tâche de fond et répond 202 + `jobId`, suivi par le registre de jobs en mémoire. — *implémenté* — preuve `app/api/sync.py:83-103` · test : aucun dans ce périmètre
- **FR-BE-381** — Le déclenchement manuel ne prend pas `_PIPELINE_LOCK` et ne vérifie pas qu'une passe tourne déjà : deux `run()` peuvent se chevaucher, et chacun remet à zéro les compteurs partagés de budget. — *partiel* (manque : exclusion mutuelle avec le pipeline) — preuve `app/api/sync.py:87-103`, `app/workers/enrichment_worker.py:609-612` · test : aucun
- **FR-BE-382** — En cas d'échec, le job manuel garde `str(exc)` dans son état, renvoyé par l'API de suivi. — *implémenté* (comportement constaté) — preuve `app/api/sync.py:98-100` · test : aucun

### Rattrapage OMDb (backfill)

- **FR-BE-383** — `POST /api/admin/enrichment/omdb-backfill` (secret maître seul) lance un rattrapage et répond 202 avec le job neuf ; paramètres `mediaType` (`movie`/`show`/`all`), `recomputeDisplayRating` (vrai par défaut), `limit` (≥ 1). — *implémenté* — preuve `app/api/enrichment.py:33-47`, `app/api/enrichment.py:69-101` · test `tests/test_enrichment_backfill.py:165`
- **FR-BE-384** — Un second rattrapage pendant qu'un premier tourne dans le même processus est refusé en 409. — *implémenté* — preuve `app/api/enrichment.py:85-89`, `app/workers/enrichment_backfill_worker.py:79-83` · test `tests/test_enrichment_backfill.py:240`
- **FR-BE-385** — Phase A : parcours par pages (clé `server_id`, `rating_key`) des lignes qui ont un `imdb_id` sans `imdb_rating`, lecture OMDb cache d'abord sous budget, écriture des notes en complément et du `display_rating` mélangé ; une erreur sur une ligne est comptée sans arrêter le job. — *implémenté* — preuve `app/workers/enrichment_backfill_worker.py:141-345` · test `tests/test_enrichment_backfill.py:273`
- **FR-BE-386** — Phase B (optionnelle) : la même passe SQL de recalcul de `display_rating` que FR-BE-369, sans réseau. — *implémenté* — preuve `app/workers/enrichment_backfill_worker.py:384-399` · test `tests/test_enrichment_backfill.py:421`
- **FR-BE-387** — `GET /api/admin/enrichment/jobs/{jobId}` renvoie statut et compteurs (`scanned`, `omdbFetched`, `imdbFilled`, `displayRecomputed`, `errors`, `lastError`) ; 404 si inconnu. — *implémenté* — preuve `app/api/enrichment.py:50-62`, `app/api/enrichment.py:104-113` · test `tests/test_enrichment_backfill.py:211`
- **FR-BE-388** — Les jobs de rattrapage vivent en mémoire du processus (100 au plus, les plus anciens évincés) : perdus au redémarrage, invisibles d'un autre processus ; le rattrapage tourne sur n'importe quel processus, pas seulement le maître. — *implémenté* — preuve `app/workers/enrichment_backfill_worker.py:63`, `app/workers/enrichment_backfill_worker.py:94-101` · test : aucun
- **FR-BE-389** — Si un verrou SQLite force à rejouer une page, ses compteurs peuvent être comptés deux fois ; les écritures, elles, restent justes (idempotentes). — *partiel* (manque : compteurs exacts sous nouvelle tentative) — preuve `app/workers/enrichment_backfill_worker.py:255-268` · test : aucun

### Côté app TV

- **FR-BE-390** — Le DTO média de la TV reçoit `imdbId`, `tmdbId`, `imdbRating`, `tmdbRating`, `displayRating`, résumé, genres, `castJson`, `youtubeTrailer`, produits par cet enrichissement (ou par l'import NFO). — *implémenté* — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendDto.kt:49-86`
- **FR-BE-391** — La TV recalcule le mélange à partir d'`imdbRating` et `tmdbRating`, et ne reprend le `displayRating` du backend que si ce calcul ne donne rien. — *implémenté* — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/mapper/BackendMediaMapper.kt:82`, `tv:data/src/main/java/com/chakir/plexhubtv/data/mapper/BackendMediaMapper.kt:124`
- **FR-BE-392** — La TV propose de relancer l'enrichissement ou de corriger un id. — *absent* : aucune référence à `rescrape`, au `PATCH` média ni aux routes d'enrichissement dans le code réseau de la TV — preuve négative `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:18-63`

## Règles métier

- **RG-BE-06.1** — Une ligne entre en file si elle est `movie` ou `show` et qu'il lui manque `tmdb_id` ou `imdb_id`. — preuve `app/workers/sync_worker.py:806-811`
- **RG-BE-06.2** — Une ligne verrouillée n'est jamais modifiée par l'enrichissement automatique, ni à la sélection ni à l'écriture. — preuve `app/workers/enrichment_worker.py:59-77`, `app/workers/enrichment_worker.py:585`
- **RG-BE-06.3** — Correspondance TMDB automatique : note ≥ 0,85, titre ≥ 0,90, écart ≥ 0,05 (ou départage par le résumé). — preuve `app/services/tmdb_service.py:51-57`
- **RG-BE-06.4** — Identité tirée d'un titre OMDb : année exacte, similarité ≥ 0,90, type concordant ; seuil plus haut que celui du contrôle de contradiction, car OMDb renvoie souvent le titre anglais. — preuve `app/workers/enrichment_worker.py:90-97`, `app/workers/enrichment_worker.py:253`
- **RG-BE-06.5** — Déclassement par OMDb : écart d'année > 1 ET similarité < 0,55, les deux ensemble. — preuve `app/workers/enrichment_worker.py:82-88`
- **RG-BE-06.6** — `display_rating` = mélange IMDb/TMDB ; une note ≤ 0 compte comme absente ; aucune note → inchangé. Formule distincte de celle de l'import NFO (`CAP-BE-23`). — preuve `app/utils/rating_blend.py:26-39`
- **RG-BE-06.7** — OMDb ne dégrade jamais un résultat TMDB : toute défaillance est en échec ouvert. — preuve `app/workers/enrichment_worker.py:324-331`
- **RG-BE-06.8** — Le budget compte les requêtes HTTP réelles, par passe. — preuve `app/services/tmdb_service.py:326-334`, `app/services/tmdb_service.py:295-304`
- **RG-BE-06.9** — Une durée rapportée par une source n'est jamais remplacée au niveau épisode ; la durée TMDB ne remplit que la ligne série absente ou sous 5 minutes. — preuve `app/services/media_identity_writer.py:97-101`, `app/services/aggregation_service.py:78-81`
- **RG-BE-06.10** — Une entrée est retentée au plus 3 fois ; une entrée `done` ne revient que par un rescrape. — preuve `app/workers/enrichment_worker.py:29`, `app/workers/sync_worker.py:829-836`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| `TMDB_API_KEY` vide | Aucune requête ; entrées `skipped` puis bloquées après 3 passes | `app/workers/enrichment_worker.py:185-186`, `app/workers/enrichment_worker.py:625-629` |
| `OMDB_API_KEY` vide | Pas de notes IMDb, pas de repli par titre ; les cas `imdb_id` seul restent `skipped` | `app/workers/enrichment_worker.py:282-283`, `app/workers/enrichment_worker.py:550-553` |
| Budget TMDB atteint | Arrêt entre deux lots ; le reste attend la passe suivante | `app/workers/enrichment_worker.py:642-644`, `app/workers/enrichment_worker.py:698-700` |
| Budget OMDb atteint | Repli par titre sauté, lecture par id refusée ; résultat TMDB gardé | `app/workers/enrichment_worker.py:284-285`, `app/services/omdb_scrape_cache_service.py:85-119` |
| 404 TMDB sur un id périmé | `not_found`, log `INFO`, retentable | `app/workers/enrichment_worker.py:315-317` |
| Autre erreur HTTP TMDB | `skipped`, log `WARNING` avec texte et trace de l'exception | `app/workers/enrichment_worker.py:318-322` |
| 429 TMDB ou OMDb | `Retry-After` suivi, 3 nouvelles tentatives au plus | `app/services/tmdb_service.py:338-345` |
| Ids TMDB mal formés | Déclassé `ambiguous`, rien écrit | `app/workers/enrichment_worker.py:463-472` |
| TMDB trouve le titre mais sans `imdb_id` | `tmdb_id` écrit, entrée `done`, `imdb_id` jamais cherché ailleurs | `app/workers/enrichment_worker.py:511-520` |
| Verrou posé entre sélection et écriture | `UPDATE` sans effet, entrée tout de même close | `app/workers/enrichment_worker.py:580-590` |
| Même `imdb_id` ou même clé de cache deux fois dans un lot | Une seule écriture en cache | `app/workers/enrichment_worker.py:489-503` |
| Note écrasée par la synchronisation | Réparée en fin de passe | `app/workers/enrichment_worker.py:734-748` |
| Rescrape d'une ligne verrouillée | 409 | `app/api/media.py:550-553` |
| Rattrapage déjà en cours | 409 | `app/api/enrichment.py:85-89` |
| Enrichissement manuel pendant le pipeline planifié | Deux passes en parallèle, compteurs de budget remis à zéro par la seconde | `app/api/sync.py:87-103`, `app/workers/enrichment_worker.py:609-612` |
| Redémarrage pendant un rattrapage | Job perdu | `app/workers/enrichment_backfill_worker.py:63` |

## Dépendances

- Autres capacités : `CAP-BE-05` (alimente la file, porte le pipeline planifié), `CAP-BE-22` (verrou `match_locked`, écrivain d'identité partagé, lot de scraping qui porte le repli OMDb v1.10.7 et l'exclusion adulte v1.10.6), `CAP-BE-23` (mêmes colonnes, autre formule de note), `CAP-BE-13` (utilise les `tmdb_id` résolus), `CAP-BE-09` (sert les champs enrichis), `CAP-BE-12` (bande-annonce), `CAP-BE-01` (métriques et registre de jobs), `CAP-BE-02` (gardes `verify_backend_secret` et `verify_master_key`).
- Autre dépôt : aucun appel TV vers les endpoints de cette capacité ; champs lus dans `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendDto.kt:49-86`.
- Données : `media`, `enrichment_queue`, `tmdb_scrape_cache` (migration 010), `omdb_scrape_cache` (migration 022) — renvoi `docs/retro-spec/technique/db/`.

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| `CLAUDE.md` piège 21 (c) interdit de journaliser le texte d'une exception httpx TMDB (l'URL, donc la clé, y est) ; `enrichment_worker` le fait pour toute erreur autre que 404, trace comprise (FR-BE-377) | code ↔ règle (sécurité) | journaliser type et statut seulement ; à traiter en priorité |
| `CLAUDE.md` §2 décrit l'enrichissement comme un worker ; le pool dédié `worker_engine` existe pour que les workers ne prennent pas les connexions de l'API, mais `enrichment_worker` et le rattrapage utilisent `async_session_factory` (FR-BE-378) | code ↔ doc | passer au pool des workers, à trancher par Chakir (lié à AUDIT-P1-005) |
| Le bandeau de `CLAUDE.md` (v1.10.7) annonce « repli OMDb quand TMDB n'a pas d'`imdb_id` » sans préciser qu'il vit dans le seul lot de scraping ; l'enrichissement automatique ne l'a pas (FR-BE-356) | doc imprécise / manque fonctionnel | préciser le bandeau, ou porter le repli dans `enrichment_worker` |
| Le déclenchement manuel ne partage pas le verrou du pipeline (FR-BE-381) | code manquant | prendre `_PIPELINE_LOCK` ou refuser en 409 |
| Les noms `ENRICHMENT_DAILY_LIMIT` / `OMDB_DAILY_LIMIT` et `CLAUDE.md` §2 (« borné ENRICHMENT_DAILY_LIMIT ») suggèrent un quota quotidien ; ce sont des budgets par passe, et le pipeline tourne toutes les 6 heures par défaut (FR-BE-374) | spec ↔ code (nommage) | documenter « par passe » ou persister un compteur |
| `.env.example` et le log de démarrage présentent OMDb comme validateur de cohérence des ids (« imdb-id consistency validator will be disabled ») sans ses rôles de notes et de repli par titre | doc et message périmés | mettre à jour le commentaire et le message |
| `flux-cles.md`, section pipeline planifié, cite des lignes de `main.py` qui ne correspondent plus (main.py:248-273, main.py:311-321) ; les fonctions sont à `app/main.py:330` et `app/main.py:523` | doc ↔ code (lignes périmées) | corriger à la prochaine `/sync-context` |
| `CLAUDE.md` §2 affiche encore `APP_VERSION = "1.10.0"` dans la puce `main.py`, alors que le code porte 1.10.9 (§4 le dit) | doc ↔ doc | corriger à la prochaine `/sync-context` |
| Tests manquants : `enqueue_for_enrichment` (cas d'ids partiels, statut non remis à zéro), borne des 3 tentatives, ordre de la chaîne de recherche, arrêt sur budget, contenu des logs (clés TMDB et OMDb), chevauchement manuel/planifié, `youtube_trailer` écrit par l'enrichissement, cas `imdb_id` seul sans réponse OMDb | test manquant | lot de tests dédié |

## Questions ouvertes

- L'exception `httpx.HTTPStatusError` levée par `tmdb_service` affiche-t-elle bien l'URL avec la clé dans le log de `app/workers/enrichment_worker.py:318` ? La lecture du code (client à paramètre `api_key`, rappel en tête de `app/services/omdb_service.py:18`) l'indique — à vérifier par test.
- Deux `run()` simultanés (manuel + planifié) traitent-ils deux fois les mêmes entrées, et le budget est-il dépassé ? Déduit de la lecture, non testé — à vérifier par test.
- Une entrée `done` avec `tmdb_id` seul (TMDB sans `imdb_id`) n'est jamais retentée par l'automatisme : ces lignes ne comptent-elles que sur le lot de scraping de `CAP-BE-22` pour obtenir leur `imdb_id` ? — à trancher par Chakir.
- L'absence d'exclusion des titres adultes dans l'enrichissement automatique est-elle voulue, alors que le lot de scraping les exclut depuis la v1.10.6 (FR-BE-348) ? — à trancher par Chakir.
- Une entrée `done` dont le titre change à la synchronisation (FR-BE-338) garde-t-elle une identité fausse jusqu'à un rescrape ? — à vérifier par test.
- La passe tient une session du pool API pendant des heures (FR-BE-378) : les requêtes de l'API manquent-elles de connexions pendant l'enrichissement ? — à vérifier par test (charge concurrente).
- Le recalcul SQL de `display_rating` (FR-BE-369) remplace-t-il la note calculée par l'import NFO sur une ligne NFO qui a aussi une note IMDb ou TMDB ? Le filtre ne regarde que ces deux colonnes (`app/utils/rating_blend.py:72-75`) — à vérifier par test.
