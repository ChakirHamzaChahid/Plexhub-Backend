# Audit v1 — Phase 3 : Performance

**Périmètre** : chemins chauds (listes unifiées, recherche, sync/enrichment, génération, DAV relay, IA), travail bloquant sur la boucle d'événements, index/plans de requête.
**Base de mesure** : `data/plexhub.db` (LECTURE SEULE) — **102 721 lignes `media`** (20 475 films / 4 465 shows / 77 781 épisodes), 189 Mo, 1 compte Xtream actif, snapshot `media_group` peuplé (13 355 groupes / 15 204 membres). Les expérimentations d'écriture (`ANALYZE`) ont été faites sur une **copie scratchpad**, jamais sur la base du repo. Le serveur smoke (DB vide) n'a **pas** servi aux mesures de latence.
**HEAD audité** : `9da9d46` (develop, v1.7.1).
**Méthode** : `EXPLAIN QUERY PLAN` + chronométrage direct SQLite, chronométrage de l'agrégation Python (`aggregate_movies`/`aggregate_series`) sur la volumétrie réelle, lecture de code `fichier:ligne`. Chaque finding indique **[MESURÉ]** ou **[DÉDUIT]**.

---

## Findings

### AUDIT-P3-001 — Aucun `ANALYZE` / `sqlite_stat1` : le planificateur SQLite choisit un index quasi non-sélectif sur TOUTES les requêtes chaudes — **S2** [MESURÉ]

**Preuve.** `SELECT name FROM sqlite_master WHERE name LIKE 'sqlite_stat%'` sur la vraie base → **vide**. Aucun `ANALYZE` ni `PRAGMA optimize` dans `app/db/database.py` ni `app/db/migrations.py` (grep). Conséquence : sans statistiques, le planificateur choisit `ix_media_category_visible` (colonne booléenne `is_in_allowed_categories`, qui matche **92 985 / 102 721 lignes = 90,5 %**) pour quasiment toutes les requêtes de liste, ignorant les index composés pertinents (`ix_media_type_added`, `ix_media_stream_validation`) pourtant présents.

**Mesures (vraie base, sans stats)** :

| Requête | Plan | Durée |
|---|---|---|
| `COUNT(*)` films autorisés | `SEARCH ... ix_media_category_visible` | **113,5 ms** |
| Page films offset 0 (tri récence) | idem + `TEMP B-TREE FOR ORDER BY` | **117,2 ms** |
| Recherche `LIKE '%matrix%'` films | idem | **116,3 ms** |
| Échantillon health-check (rowid anchor) | idem | **113,8 ms** |
| Page profonde OFFSET 15000 | idem + TEMP B-TREE | **284,4 ms** |

**Mesures après `ANALYZE` (copie scratchpad, `ANALYZE` = 196 ms one-shot)** :

| Requête | Plan | Durée | Gain |
|---|---|---|---|
| `COUNT(*)` films autorisés | `COVERING INDEX ix_media_stream_validation` | **0,6 ms** | **×188** |
| Page films offset 0 | `ix_media_stream_validation` | **23,9 ms** | ×5 |
| Recherche `LIKE '%matrix%'` | idem | **18,2 ms** | ×6 |
| Full-load films (fallback live) | idem | 112,9 ms (vs 205,9) | ×1,8 |

**Impact.** Toute l'API de listes brutes (`/api/media/movies|shows|episodes`), la recherche, le fingerprint du cache unifié et l'échantillonnage health-check paient ~100-120 ms de scan là où 1-25 ms suffisent — **sur chaque requête**, et le coût croît linéairement avec le catalogue (multi-comptes). C'est le finding perf au meilleur ratio gain/effort de tout l'audit : la migration 015 (CR-P02) a bien créé les index (vérifié : les 20 index ORM de `models/database.py:109-128` existent tous sur la vraie base via `PRAGMA index_list(media)`), mais **sans statistiques, SQLite ne les utilise pas**.

**Recommandation.** Exécuter `PRAGMA optimize` à la fermeture de chaque connexion long-lived OU un `ANALYZE` en fin de pipeline sync (au même endroit que `unified_group_service.rebuild_all`), + une migration one-shot `ANALYZE`. **Effort : S** (quelques lignes).

---

### AUDIT-P3-002 — CR-P01 « résolu » ne couvre que le browse non-filtré : toute requête search/genre/year retombe en O(catalogue), et un échec de rebuild du snapshot dégrade silencieusement — **S2** [MESURÉ + DÉDUIT]

**Vérifié d'abord (positif).** Le chemin snapshot fonctionne et est bien branché : `media_service.get_unified_list` (`media_service.py:278-283`) route vers `_unified_list_from_snapshot` (`media_service.py:337-412`) quand `search/genre/year` sont None. Coût mesuré d'une page de 60 groupes sur la vraie base : clés **0,2 ms** + membres **0,4 ms** (covering index) + hydratation `SELECT *` de 67 lignes **30,9 ms** → **~31 ms de DB par page**, O(page). CR-P01 est réel pour le browse non-filtré. [MESURÉ]

**Mais** :

1. **Toute requête filtrée (search, genre, year) emprunte le fallback live** (`media_service.py:274-277`, commentaire explicite). Coût par requête en cache froid : fingerprint `COUNT+MAX(updated_at)` avec le même `ILIKE` (**115,9 ms** mesuré sans stats) + full-load des lignes matchées (**116,3 ms**) + agrégation `to_thread`. Une recherche tapée lettre à lettre depuis l'app Android (« m », « ma », « mat »…) = **une clé de cache distincte par frappe** → chaque frappe paie ~230-350 ms de scans complets. Le cache TTL (45 s, 12 entrées, `media_service.py:70-76`) n'aide que sur la répétition exacte du même filtre. [MESURÉ]
2. **Un filtre `genre` populaire** charge et agrège une fraction majeure du catalogue (le `ILIKE '%action%'` sur `genres` peut matcher des milliers de lignes) → full-load + agrégation par combinaison de filtre, chaque entrée du cache pinnant un snapshot O(sous-catalogue) en RAM (cf. AUDIT-P3-005). [DÉDUIT]
3. **Dégradation silencieuse en O(catalogue)** : si `unified_group_service.rebuild_all` échoue pour un type (l'exception est catchée et seulement loggée, `unified_group_service.py:129-133`), OU si le pipeline n'a pas encore tourné, `_unified_list_from_snapshot` renvoie `None` (`media_service.py:368-369`) et **chaque** requête de browse repaie le full-load (**205,9 ms** DB mesuré pour les films sans stats) + agrégation (**239 ms CPU** mesuré, cf. P3-004) jusqu'au prochain pipeline (6 h par défaut). Aucune métrique/alerte ne distingue le chemin snapshot du chemin live. [MESURÉ pour les coûts, DÉDUIT pour le scénario]

**Impact.** La recherche — un des chemins les plus fréquents côté app — reste O(catalogue) par frappe ; la promesse O(page) de CR-P01 ne tient que pour le scroll non-filtré.
**Recommandation.** (a) AUDIT-P3-001 d'abord (divise le coût du fallback par ~6) ; (b) exposer une métrique Prometheus `plexhub_unified_path{snapshot|live}` pour rendre la dégradation visible ; (c) à terme, FTS5 sur `title` pour la recherche (cf. P3-007). **Effort : S (métrique) / M (FTS5).**

---

### AUDIT-P3-003 — L'agrégation de `DatabaseSource` tourne SUR la boucle d'événements : génération Plex et build de l'arbre DAV bloquent la boucle ~0,75 s+ à chaque passe — **S2** [MESURÉ]

CR-C01 a bien été corrigé **côté écritures** : `PlexLibraryGenerator.generate()` offloade `mapping.load` (`generator.py:210`) et toute la séquence write/prune/save (`generator.py:232`) via `asyncio.to_thread` — vérifié. Le backup passe aussi par `to_thread` (`main.py:371`), l'inférence fastembed aussi (`embedding_service.py`), les images par ThreadPool (`storage.py:41`).

**Mais la phase de LECTURE/agrégation n'est pas offloadée** : `DatabaseSource.get_movies()` appelle `aggregate_movies(rows)` **inline** (`source.py:132`) et `get_series()` appelle `aggregate_series(shows, episodes)` inline (`source.py:202`) — contrairement à `media_service` et `unified_group_service` qui passent les mêmes fonctions par `asyncio.to_thread` (`media_service.py:329,406,556` ; `unified_group_service.py:50`).

**Mesuré sur la volumétrie réelle** (copie scratchpad, même machine) :
- `aggregate_movies` sur 12 331 lignes → 10 638 groupes = **239 ms de CPU pur** ;
- `aggregate_series` sur 2 873 shows + 77 781 épisodes → 2 717 groupes = **504 ms de CPU pur** ;
- hydratation ORM : 266 ms (films) + **1 972 ms** (shows+épisodes) — celle-ci s'exécute par morceaux via aiosqlite/greenlets, mais la construction des objets Python se fait aussi côté boucle.

À cela s'ajoutent les boucles `_build_versions` par groupe (`source.py:133,207`) et la construction des `PlexMovie`/`PlexSeries`, elles aussi sur la boucle.

**Impact.** À chaque génération planifiée (toutes les 6 h) **et à chaque rebuild de l'arbre DAV** (`tree_builder.build_dav_tree` consomme `DatabaseSource`, `tree_builder.py:187-190` — déclenché sur le chemin de requête `/dav` au premier hit après TTL 60 min ou invalidation), la boucle d'événements est gelée par tranches totalisant **~750 ms de CPU pur minimum** — toutes les requêtes API en vol (dont les lectures Plex via le relay DAV) subissent cette latence. Le préchauffage VFS obligatoire (runbook §0.1) rend le point d'autant plus sensible : un gel de la boucle pendant qu'un scan Plex lit via `/dav` allonge les transactions côté Plex (la cascade `database is locked` documentée).

**Recommandation.** Envelopper `aggregate_movies`/`aggregate_series` + la construction des modèles dans `asyncio.to_thread` dans `source.py` (les fonctions sont pures sur des lignes déjà chargées — même justification que `media_service.py:24-29`). **Effort : S.**

---

### AUDIT-P3-004 — Coût du fix CR-F11/CR-F01 : `get_series_info` pour CHAQUE série à CHAQUE sync = ~4 465 appels HTTP/run — dette acceptée mais non bornée — **S3** [DÉDUIT, volumétrie mesurée]

`sync_account` fetch désormais les épisodes de **toutes** les séries synchronisées, découplé du `dto_hash` show (`sync_worker.py:1359-1370`, commentaire CR-F11 explicite : aucun signal provider fiable pour « la liste d'épisodes a changé »). Sur la vraie base : **4 465 séries** → ~4 465 appels `get_series_info` par run (toutes les `SYNC_INTERVAL_HOURS=6`), concurrence `Semaphore(25)` (`sync_worker.py:1202`), batchs de 50 (`sync_worker.py:1425`). À 300 ms/appel → **~55 s minimum** de phase épisodes par compte et par run ; à 1 s/appel (panel lent) → ~3 min. S'y ajoutent ~77 781 lignes épisode re-passées dans l'upsert `ON CONFLICT` chaque run — le garde `WHERE content_hash != excluded.content_hash` (`sync_worker.py:731`) évite les écritures inutiles, mais pas les ~780 statements de 100 lignes ni la comparaison par ligne.

**Impact.** (a) charge provider : 4 465 requêtes API/6 h sur des panels qui limitent parfois à 1-3 connexions — le `Semaphore(25)` est bien au-dessus de `max_connections` de la plupart des comptes (les appels `player_api.php` ne comptent généralement pas comme des connexions de stream, mais certains panels agressifs bannissent sur le rythme d'API) ; (b) la durée de sync croît linéairement avec le nombre de séries et de comptes. C'est le prix assumé de la correction des orphelins (CR-F01) — acceptable aujourd'hui, à surveiller.
**Recommandation.** Clamper la concurrence épisode par `account.max_connections` (comme le fait déjà le health-check), et/ou espacer la passe épisodes complète (1 run sur N, les séries `changed` à chaque run). **Effort : S-M.**

---

### AUDIT-P3-005 — Empreinte mémoire des chemins « catalogue entier » : 560 Mo RSS mesurés pour tenir le catalogue, dans un conteneur limité à 2 Go — **S3** [MESURÉ]

Chargement ORM du catalogue complet (12 331 films + 2 873 shows + 77 781 épisodes) : **560 Mo RSS** mesurés (~6 Ko/ligne ORM). Consommateurs simultanés possibles :
- la génération Plex (`DatabaseSource`, CR-P05 « constrained-by-design » documenté `source.py:117-127` — le streaming `yield_per=1000` évite le buffer driver mais la dédup exige l'ensemble complet en RAM) **plus** les objets `PlexMovie`/`PlexSeries` construits par-dessus ;
- le build de l'arbre DAV (même `DatabaseSource`, cap 25/5 appliqué **après** le full-load, `tree_builder.py:165-190`) ;
- le cache TTL unifié : 12 entrées max, chacune pouvant pinner un snapshot de groupes (~74 Mo estimés pour un snapshot films complet, prorata des 560 Mo ; un filtre `genre` large peut en pinner une fraction importante) — le risque est acknowledgé dans le commentaire `media_service.py:66-69` ;
- le rebuild snapshot `unified_group_service.rebuild` (full-load par type, séquentiel).

**Impact.** Un pipeline (génération) concurrent d'un rebuild d'arbre DAV et de quelques recherches filtrées peut approcher la limite 2 Go (`docker-compose.yml`) avec en plus fastembed/ONNX résident. Pas de crash observé/rapporté à ce jour — risque, pas incident.
**Recommandation.** Sérialiser génération et build DAV (déjà partiellement le cas via l'invalidation post-génération), réduire `_UNIFIED_GROUPS_CACHE_MAX_SIZE` ou pinner des projections plutôt que des lignes ORM complètes. **Effort : M.**

---

### AUDIT-P3-006 — Recherche `ILIKE '%term%'` non-sargable (résidu CR-P03) : ~116 ms/scan sans stats, 18 ms avec — vivable aujourd'hui, linéaire demain — **S3** [MESURÉ]

Le leading-wildcard demeure sur `media_service.py:166,291` et `live.py:50`. Mesuré (films, 12 331 lignes autorisées) : **116 ms par scan** sans `ANALYZE`, **18 ms** avec. Une requête de recherche unifiée en paie 2 (fingerprint + load) en cache froid ; une recherche brute en paie 2 (count + page). Le narrow-COUNT de CR-P03 est bien en place (`media_service.py:194-198`, `live.py:53-61`) — c'est le résidu structurel qui reste. À 1 compte c'est acceptable **après AUDIT-P3-001** ; à N comptes le coût est ×N.
**Recommandation.** FTS5 (table virtuelle `title`) si le catalogue dépasse ~300 k lignes ou que la recherche devient un chemin critique UX. **Effort : M.**

---

### AUDIT-P3-007 — Shim Range DAV : draine l'upstream APRÈS la fin de la fenêtre demandée en tenant le permit — le drain post-fenêtre est du gaspillage pur — **S3** [DÉDUIT, code lu]

Le comportement « drain complet en tenant le permit » est une dette **assumée et documentée** (piège 18d, `relay.py:165-175`). Quantification : sur un compte `max_connections=1` (`DAV_UPSTREAM_PER_ACCOUNT` clampé), un seek Plex en fin de fichier sur un film de 4 Go via un panel qui ignore `Range` force le relay à lire ~4 Go upstream ; à 50 Mbps ≈ **10-11 minutes** pendant lesquelles le sémaphore du compte bloque toute autre lecture DAV (et le préchauffage lit justement header **et tail** de chaque fichier — le script `prewarm-dav-cache.sh` déclenche exactement ce cas sur chaque item si le panel ignore Range).

**Nuance actionnable** : `_shim_ranged_body` (`relay.py:176-188`) continue de drainer **après `end`** (« keeps draining after end rather than closing the response early »). Le drain **avant** `start` est inévitable (le panel ne seek pas) ; le drain **après** `end` ne sert qu'à préserver la connexion keep-alive du pool — sur des fichiers de plusieurs Go, fermer la réponse (`resp.aclose()` abandonne la connexion) dès `position > end` coûte une reconnexion TCP (~ms) et économise potentiellement des Go de bande passante et des minutes de permit tenu.
**Recommandation.** `break` dès `chunk_start > end` et laisser `aclose` fermer la connexion. **Effort : S.** (Le drain pré-fenêtre reste la dette assumée du piège 18d.)

---

### AUDIT-P3-008 — Détail unifié (`?unification_id=`, fix CR-F05) : le pool de candidats « même année » charge jusqu'à ~1 150 lignes `SELECT *` par requête de détail — **S3** [MESURÉ]

`get_unified_group._load_convergence_candidates` (`media_service.py:513-522`) charge **toutes** les lignes film de la ou des années des seeds. Volumétrie réelle : année la plus peuplée = 1 151 lignes (2022), 1 088 (2021). Mesuré ~247 ms sans stats (dont le coût du plan dégradé de P3-001) ; estimé ~30-60 ms avec stats + `ix_media_type_added`… mais il n'existe **pas d'index sur `year`** (`PRAGMA index_list(media)` vérifié) → le filtre année reste un scan du type. Coût par ouverture de fiche détail côté app. Correct fonctionnellement (borné, jamais tout le catalogue), sous-optimal.
**Recommandation.** Index `(type, year)` + ne déclencher la passe (b) « même année » que si la passe (a) « ids partagés » n'a rien absorbé. **Effort : S.**

---

### AUDIT-P3-009 — Cold starts IA documentés et non atténués au boot : ~30 s fastembed au 1ᵉʳ `/rank`, chargement gemma4 côté Ollama au 1ᵉʳ appel LLM — **dette** [DÉDUIT]

Conforme à la doc (piège 1, `embedding_service.py:11` ; piège 13). Aucun warmup optionnel au lifespan (choix assumé : le rebuild ne tourne jamais au boot, piège 5). Le premier utilisateur d'une feature IA après chaque redéploiement paie 30 s + timeout potentiel côté app. Pas de régression constatée ; statu quo acceptable si l'app tolère le premier timeout.
**Recommandation (optionnelle).** Hook de warmup gaté par env (`AI_WARMUP_ON_BOOT`) qui embed une chaîne vide en tâche de fond master-only. **Effort : S.**

---

## Vérifications positives (déclaré résolu → confirmé à HEAD)

| Réf | Verdict | Preuve |
|---|---|---|
| CR-P07 (sérialisation single-pass) | **Confirmé branché** | `_single_pass_json` défini `api/media.py:29`, consommé sur les 6 endpoints liste (`:145,186,…`) |
| CR-P04 (curseur keyset) | **Confirmé + mesuré efficace** | `media_service.py:220-226`, `api/media.py:126-147` ; mesuré **1,3 ms** (keyset) vs **284 ms** (OFFSET 15000) — plan `ix_media_type_added (type=? AND added_at<?)` |
| CR-P06 (`ORDER BY random()`) | **Confirmé résolu** | `_sample_stream_candidates` = ancre rowid + range-scan (`health_check_worker.py:326-367`) ; NB : le plan reste dégradé par P3-001 (113 ms mesurés) |
| CR-C01 (écritures génération sur la boucle) | **Résolu côté écritures** | `generator.py:210,232` (`to_thread` sur load/write/prune/save) ; **mais** cf. AUDIT-P3-003 pour la lecture/agrégation |
| CR-P02 (index `media` manquants) | **Confirmé résolu** | migration 015 (`migrations.py:580-644`) ; les 20 index ORM présents sur la vraie base (`PRAGMA index_list`) — **mais** inexploités sans stats (P3-001) |
| CR-P03 (COUNT-over-subquery) | **Résolu pour le COUNT** | narrow `func.count()` `media_service.py:194-198`, `live.py:53-61` ; résidu leading-wildcard = P3-006 |
| `sqlite3.backup` bloquant | **Confirmé offloadé** | `main.py:369-371` (`asyncio.to_thread(_run_backup)`) |
| `DavTreeCache` | **Sain** | TTL 60 min + single-flight (`vfs.py:119-135`), invalidation post-génération (`plex_generation_service.py:119-131`) ; coût du rebuild = P3-003 |

**Observation hors-finding** : la base dev `data/plexhub.db` est antérieure à la migration 020 (`media.file_size` absent — constaté en tentant un `SELECT` ORM sur la copie) ; elle n'a simplement pas été bootée depuis. Sans impact code, mais à savoir pour quiconque mesure dessus.

---

## Tableau récapitulatif

| ID | Sévérité | Titre | Preuve principale | Statut |
|---|---|---|---|---|
| AUDIT-P3-001 | **S2** | Aucun `ANALYZE`/`sqlite_stat1` — planificateur sur index non-sélectif partout (113 ms → 0,6 ms prouvé) | `db/database.py`, `db/migrations.py` (absence) ; mesures EQP | MESURÉ |
| AUDIT-P3-002 | **S2** | CR-P01 : fallback O(catalogue) sur toute requête filtrée + dégradation silencieuse si rebuild snapshot KO | `media_service.py:274-283,368-369` ; `unified_group_service.py:129-133` | MESURÉ + DÉDUIT |
| AUDIT-P3-003 | **S2** | Agrégation `DatabaseSource` sur la boucle (239 + 504 ms CPU mesurés) — génération + arbre DAV | `source.py:132,202` vs `media_service.py:329` | MESURÉ |
| AUDIT-P3-004 | S3 | Fix CR-F11 : ~4 465 `get_series_info`/sync, Semaphore(25) non clampé par `max_connections` | `sync_worker.py:1359-1430` ; volumétrie DB | DÉDUIT (volumétrie mesurée) |
| AUDIT-P3-005 | S3 | 560 Mo RSS mesurés pour tenir le catalogue ; cumul générateur + arbre DAV + cache TTL vs limite 2 Go | `source.py:117-127`, `media_service.py:66-76`, mesure RSS | MESURÉ |
| AUDIT-P3-006 | S3 | Résidu CR-P03 : `ILIKE '%term%'` = 116 ms/scan (18 ms avec stats), linéaire au catalogue | `media_service.py:166,291`, `live.py:50` | MESURÉ |
| AUDIT-P3-007 | S3 | Shim Range DAV : drain post-fenêtre inutile en tenant le permit (fermeture anticipée possible) | `relay.py:165-188` | DÉDUIT |
| AUDIT-P3-008 | S3 | CR-F05 : pool candidats « même année » = jusqu'à ~1 150 `SELECT *`/fiche détail, pas d'index `(type, year)` | `media_service.py:513-522` | MESURÉ |
| AUDIT-P3-009 | dette | Cold starts IA (~30 s fastembed, gemma4) sans warmup optionnel | `embedding_service.py:11` | DÉDUIT |
