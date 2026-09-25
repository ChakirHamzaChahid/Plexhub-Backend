# CAP-BE-22 — Scraper manuel & « À vérifier »

**Reconstituée le** : 2026-09-26 · **HEAD** : `afe9fe3` · **Sources** : code, gabarits Jinja2, tests, `CLAUDE.md` (§2, §9 piège 21), ADR `docs/architecture/adr/0005-manual-scraper-poster-match.md`, `docs/architecture/flux-cles.md` §5.14, app TV (lecture seule, préfixe `tv:`). Aucun PRD ne couvre cette capacité : `docs/10-prd-*.md` ne traite que les téléchargements. Il n'y a donc pas de tableau d'écart fiche ↔ PRD. Les écarts sont relevés face à l'ADR 0005 (document de conception) et à `CLAUDE.md`.
**Périmètre** : 14 routes HTML de `app/api/admin.py` : groupe `/admin/media/{sid}/{rk}/*` (apply, unlock, clear, rescrape, poster, scrape, candidates, lookup), groupe `/admin/review*` (liste, apply, dismiss), groupe `/admin/scrape-batch*` (lancement, statut, annulation). Services `manual_scrape_service`, `poster_match_service` et `media_identity_writer`, worker `manual_scrape_batch_worker`, colonnes `media.match_locked`/`match_source` (M026), table `scrape_review` (M027). Gabarits `_scrape_panel.html`, `_scrape_candidates.html`, `_scrape_batch_status.html`, `_review_list.html`, `_review_row_result.html`, plus le contrôleur `window.plexhubScrape` de `base.html`. Inventaire des routes : `docs/retro-spec/_genere/inventaire.md`. Hors périmètre : tableau, filtres et compteurs du catalogue `/admin` (CAP-BE-21) ; enrichissement automatique (CAP-BE-06) ; import NFO (CAP-BE-23).

## Résumé

Le scraper manuel sert à l'opérateur quand l'enrichissement automatique s'est trompé d'identité (homonymes, titres Xtream pollués, catalogue en anglais). Un tiroir modal cherche des candidats TMDB, avec repli OMDb. Il compare le poster Xtream à celui de chaque candidat par hachage perceptuel (dHash + pHash) et applique le candidat choisi. L'application **verrouille** la ligne (`match_locked`) ; la synchro, l'enrichissement, l'import NFO et les scripts de cohérence respectent ce verrou. Un lot de scraping en arrière-plan fait deux passes : appariement des ids, puis recherche et moteur de règles pur `decide()`. Il applique ce qui est sûr et range le reste dans la file « À vérifier » (`scrape_review`), que l'opérateur traite sans nouvel appel réseau. L'app TV ne voit que l'effet indirect : les identités corrigées apparaissent dans le catalogue.

## User stories observées

- **US-BE-22.1** (P1) — En tant qu'opérateur, j'ouvre le tiroir de scrape d'un média, je compare son poster à ceux des candidats (badge identique, proche, différent) et j'applique le bon candidat.
  - Parcours : ligne du catalogue → `GET …/scrape` → `GET …/candidates` → `POST …/apply` · Maestro : sans objet (UI navigateur)
- **US-BE-22.2** (P1) — En tant qu'opérateur, je colle un id ou une URL IMDb/TMDB connus au lieu de chercher par titre.
  - Parcours : `POST …/lookup` → une carte candidate → `POST …/apply` · Maestro : sans objet
- **US-BE-22.3** (P1) — En tant qu'opérateur, une identité fixée à la main n'est plus défaite par l'enrichissement, la synchro, l'import NFO ou les scripts, tant que je ne la déverrouille pas.
  - Parcours : apply → verrou ; `POST …/unlock` pour lever · Maestro : sans objet
- **US-BE-22.4** (P2) — En tant qu'opérateur, après un apply, le tiroir se referme et s'ouvre sur la ligne incomplète suivante de la page.
  - Parcours : `HX-Trigger: close-scrape-panel` → `plexhubScrape` → `…/scrape?auto=1` · Maestro : sans objet
- **US-BE-22.5** (P2) — En tant qu'opérateur, je lance un lot de scraping (simulation ou réel), je suis sa progression et je peux l'annuler.
  - Parcours : `POST /admin/scrape-batch` → `GET /admin/scrape-batch/status` (relevé toutes les 2 s) → `POST /admin/scrape-batch/{job_id}/cancel` · Maestro : sans objet
- **US-BE-22.6** (P2) — En tant qu'opérateur, je traite la file « À vérifier » en appliquant un candidat déjà évalué ou en écartant l'item, sans nouvel appel réseau.
  - Parcours : onglet review → `GET /admin/review` → `POST /admin/review/{sid}/{rk}/apply` ou `…/dismiss` · Maestro : sans objet
- **US-BE-22.7** (P3) — En tant qu'opérateur, j'efface les ids d'une ligne ou je la remets en file d'enrichissement.
  - Parcours : `POST …/clear` · `POST …/rescrape` · Maestro : sans objet

Côté app TV : aucun parcours (voir Dépendances).

## Exigences fonctionnelles

### Tiroir, proxy de poster et recherche

- **FR-BE-921** — `GET /admin/media/{sid}/{rk}/poster?which=xtream|current` sert l'image via `poster_match_service`. L'URL est lue en base, jamais fournie par le client. — *implémenté* — preuve `app/api/admin.py:492-513` · test `tests/test_admin_scrape_ui.py:217`, `tests/test_admin_scrape_ui.py:260`
- **FR-BE-922** — Toute défaillance du proxy (pas d'URL, hôte bloqué, format refusé) donne 404 sans révéler l'URL ni l'hôte. — *implémenté* — preuve `app/api/admin.py:510-517` · test `tests/test_admin_scrape_ui.py:283`, `tests/test_admin_scrape_ui.py:290`, `tests/test_admin_scrape_ui.py:301`
- **FR-BE-923** — Le proxy renvoie `Cache-Control: private, max-age=86400`, `Content-Encoding: identity` et `X-Content-Type-Options: nosniff`. — *implémenté* — preuve `app/api/admin.py:521-530` · test : aucun sur les en-têtes
- **FR-BE-924** — `GET …/scrape` rend le tiroir modal : deux posters et un formulaire prérempli (titre et année nettoyés). `?auto=1` lance la recherche au chargement ; c'est réservé à l'enchaînement automatique. — *implémenté* — preuve `app/api/admin.py:534-562` · test `tests/test_admin_scrape_ui.py:322`, `tests/test_admin_scrape_ui.py:364`
- **FR-BE-925** — `GET …/candidates` lance la recherche réseau (titre, année, type, fournisseur). — *implémenté* — preuve `app/api/admin.py:566-597` · test `tests/test_admin_scrape_ui.py:435`
- **FR-BE-926** — `POST …/lookup` accepte un id ou une URL saisis ; entrée illisible → 422. — *implémenté* — preuve `app/api/admin.py:600-629` · test `tests/test_admin_scrape_ui.py:486`, `tests/test_admin_scrape_ui.py:506`
- **FR-BE-927** — Le contrôleur `window.plexhubScrape` ferme la modale et enchaîne sur la ligne suivante marquée `data-incomplete="1"` (une ligne verrouillée compte comme réglée). La ligne est résolue par `getElementById`, jamais `querySelector`, car un `rating_key` peut finir par `.mkv`. — *implémenté* — preuve `app/templates/admin/base.html:23-69`, `app/templates/admin/base.html:79-81` · test `tests/test_admin_scrape_ui.py:386`, `tests/test_admin_scrape_ui.py:411`
- **FR-BE-928** — `search_candidates` élargit la recherche TMDB par paliers (avec année, sans année, puis `en-US`) et déduplique par `tmdb_id` en gardant la meilleure confiance. — *implémenté* — preuve `app/services/manual_scrape_service.py:270`, `app/services/manual_scrape_service.py:325-357` · test `tests/test_manual_scrape_search.py:142`, `tests/test_manual_scrape_search.py:170`
- **FR-BE-929** — Repli OMDb (`?s=`) si `provider="omdb"`, ou si `provider="auto"` n'a donné aucun candidat TMDB. `provider="tmdb"` n'appelle jamais OMDb. — *implémenté* — preuve `app/services/manual_scrape_service.py:384-409` · test `tests/test_manual_scrape_search.py:211`, `tests/test_manual_scrape_search.py:238`, `tests/test_manual_scrape_search.py:257`
- **FR-BE-930** — Seuls les `SCRAPE_INTERACTIVE_POSTER_CANDIDATES` meilleurs candidats au score texte ont leur poster comparé. — *implémenté* — preuve `app/services/manual_scrape_service.py:313-316`, `app/services/manual_scrape_service.py:411-437` · test `tests/test_manual_scrape_search.py:299`
- **FR-BE-931** — Le score combiné pondère le texte et l'image quand une preuve image existe. Sinon le score texte reste tel quel : l'absence de preuve ne classe jamais sous une preuve contraire. — *implémenté* — preuve `app/services/manual_scrape_service.py:77`, `app/services/manual_scrape_service.py:442-454` · test `tests/test_manual_scrape_search.py:329`
- **FR-BE-932** — Au plus un candidat est marqué `recommended`, et seulement s'il est `text_safe` ou si son poster est `identical`. — *implémenté* — preuve `app/services/manual_scrape_service.py:456-462` · test `tests/test_manual_scrape_search.py:299`
- **FR-BE-933** — `lookup()` résout `tt…`, un entier TMDB ou une URL IMDb/TMDB en une carte unique non recommandée. Repli OMDb seul si TMDB ne résout rien. — *implémenté* — preuve `app/services/manual_scrape_service.py:483-503`, `app/services/manual_scrape_service.py:552-596` · test `tests/test_manual_scrape_apply.py:704`, `tests/test_manual_scrape_apply.py:767`

### Comparaison d'images

- **FR-BE-934** — dHash (9×8) et pHash (DCT 32×32) sont calculés hors boucle (`asyncio.to_thread`), après rognage des bandes noires. — *implémenté* — preuve `app/services/poster_match_service.py:214-309` · test `tests/test_poster_match_service.py:92`, `tests/test_poster_match_service.py:102`
- **FR-BE-935** — Badge `identical`, `close`, `different` ou `unknown` selon les seuils de distance (RG-BE-22.3). — *implémenté* — preuve `app/services/poster_match_service.py:316-323` · test `tests/test_poster_match_service.py:115`, `tests/test_poster_match_service.py:132`
- **FR-BE-936** — Détection du poster générique (image quasi uniforme, ou même pHash vu sur plusieurs URL Xtream). Un poster générique plafonne le badge à `close`. — *implémenté* — preuve `app/services/poster_match_service.py:326-340`, `app/services/poster_match_service.py:591-593` · test `tests/test_poster_match_service.py:127`, `tests/test_manual_scrape_decide.py:116`
- **FR-BE-937** — Raccourci sans téléchargement quand le poster Xtream est déjà une image `image.tmdb.org` de même nom de fichier que celle du candidat. — *implémenté* — preuve `app/services/poster_match_service.py:548-559` · test `tests/test_poster_match_service.py:445`, `tests/test_poster_match_service.py:460`
- **FR-BE-938** — Client HTTP dédié, jamais celui de `tmdb_service`/`omdb_service` (qui injectent la clé API en paramètre). Garde SSRF appliquée à chaque requête et à chaque redirection. — *implémenté* — preuve `app/services/poster_match_service.py:346-361` · test `tests/test_poster_match_service.py:245`, `tests/test_poster_match_service.py:254`, `tests/test_poster_match_service.py:484`
- **FR-BE-939** — Le SVG (type scriptable) est refusé et Pillow n'accepte que JPEG, PNG, WEBP et GIF. — *implémenté* — preuve `app/services/poster_match_service.py:122-125`, `app/services/poster_match_service.py:419-426` · test `tests/test_poster_match_service.py:313`
- **FR-BE-940** — Taille en octets et en pixels plafonnée avant le décodage complet ; les bombes de décompression sont rejetées. — *implémenté* — preuve `app/services/poster_match_service.py:277-297`, `app/services/poster_match_service.py:428-432` · test `tests/test_poster_match_service.py:225`, `tests/test_poster_match_service.py:292`
- **FR-BE-941** — Chaque téléchargement d'image a un délai total (`asyncio.wait_for`), en plus des délais httpx par opération. — *implémenté* — preuve `app/services/poster_match_service.py:444-474` · test `tests/test_poster_match_service.py:421`
- **FR-BE-942** — Concurrence bornée par trois sémaphores : un global, un par hôte hors CDN, un pour le hachage. — *implémenté* — preuve `app/services/poster_match_service.py:375-394` · test `tests/test_poster_match_service.py:188`
- **FR-BE-943** — Les hashes de poster sont mis en cache par URL : 6 h en cas de succès, 15 min en cas d'échec. — *implémenté* — preuve `app/services/poster_match_service.py:97-98`, `app/services/poster_match_service.py:477-502` · test `tests/test_poster_match_service.py:524`

### Application d'identité

- **FR-BE-944** — `apply_candidate` enchaîne trois phases séparées : lecture en session courte, réseau sans accès à la base, écriture par `write_with_retry` sans réseau. — *implémenté* — preuve `app/services/manual_scrape_service.py:646-664`, `app/services/manual_scrape_service.py:670-753`, `app/services/manual_scrape_service.py:855-945` · test `tests/test_manual_scrape_apply.py:977`
- **FR-BE-945** — `POST /admin/media/{sid}/{rk}/apply` exige un `tmdb_id` numérique ou un `imdb_id` (sinon 422). Si un autre item porte déjà l'id, c'est un conflit 409, sauf case « forcer ». — *implémenté* — preuve `app/api/admin.py:299-339`, `app/services/manual_scrape_service.py:756-793` · test `tests/test_admin_manual_scrape.py:72`, `tests/test_admin_manual_scrape.py:91`, `tests/test_manual_scrape_apply.py:328`
- **FR-BE-946** — Le mode d'écriture (`fill` ou `replace`) se déduit du changement effectif d'un id existant. — *implémenté* — preuve `app/services/manual_scrape_service.py:794-798` · test `tests/test_manual_scrape_apply.py:115`, `tests/test_manual_scrape_apply.py:235`
- **FR-BE-947** — Avec `propagate`, l'apply complète les jumelles de même clé-titre, sans aucun id et non verrouillées. Il se désactive quand la clé-titre est dégénérée. — *implémenté* — preuve `app/services/manual_scrape_service.py:626-637`, `app/services/manual_scrape_service.py:913-938` · test `tests/test_manual_scrape_apply.py:405`, `tests/test_manual_scrape_apply.py:462`
- **FR-BE-948** — L'apply pose toujours `match_locked=True` et `match_source` (manuel ou lot). — *implémenté* — preuve `app/services/manual_scrape_service.py:850-853` · test `tests/test_manual_scrape_apply.py:546`
- **FR-BE-949** — Un écrivain non manuel (lot) n'écrase jamais une ligne déjà verrouillée (`skipped_locked`) ; un apply manuel peut, lui, reverrouiller. — *implémenté* — preuve `app/services/manual_scrape_service.py:864-872`, `app/services/manual_scrape_service.py:946-955` · test `tests/test_manual_scrape_apply.py:520`
- **FR-BE-950** — Dans la même transaction, l'apply met à jour trois choses : il écrase le cache de scrape du titre, passe l'entrée `enrichment_queue` à `done` et résout la revue `pending`. — *implémenté* — preuve `app/services/manual_scrape_service.py:876-911` · test `tests/test_manual_scrape_apply.py:153`, `tests/test_manual_scrape_batch.py:676`
- **FR-BE-951** — Si la base est verrouillée (« database is locked ») pendant l'apply, l'opérateur reçoit un 409 lisible, jamais un 500 ; les autres erreurs de base remontent. — *implémenté* — preuve `app/api/admin.py:341-376` · test `tests/test_admin_manual_scrape.py:117`, `tests/test_admin_manual_scrape.py:155`
- **FR-BE-952** — `media_identity_writer` mode `fill` reproduit le comportement historique de l'`enrichment_worker`. En mode `replace`, les colonnes propres au fournisseur (`PROVIDER_ONLY_COLS`) sont affectées ou remises à NULL, et les images réassignées. — *implémenté* — preuve `app/services/media_identity_writer.py:104-173`, `app/services/media_identity_writer.py:175-218` · test `tests/test_media_identity_writer.py:71`, `tests/test_manual_scrape_apply.py:296`
- **FR-BE-953** — Notes : `fill` complète seulement les champs vides (COALESCE) ; `replace` affecte directement et recalcule la note mixte. `0.0` signifie « pas de note », car la colonne est NOT NULL. — *implémenté* — preuve `app/services/media_identity_writer.py:298-325` · test `tests/test_manual_scrape_apply.py:266`
- **FR-BE-954** — Une correction d'identité se propage au catalogue : le rebuild du snapshot est différé et regroupé par `schedule_rebuild`. — *partiel* (manque : les NFO et posters déjà générés ne sont pas réécrits, limite D12 de l'ADR ; l'opérateur doit vider les fichiers à la main) — preuve `app/services/unified_group_service.py:286`, `docs/architecture/adr/0005-manual-scraper-poster-match.md:660` · test : aucun sur la bibliothèque générée

### Verrou et actions de ligne

- **FR-BE-955** — `POST …/unlock` efface `match_locked` et `match_source` sans toucher aux ids. — *implémenté* — preuve `app/api/admin.py:421`, `app/services/manual_scrape_service.py:973-986` · test `tests/test_admin_manual_scrape.py:194`, `tests/test_manual_scrape_apply.py:623`
- **FR-BE-956** — `POST …/clear` vide les ids et recalcule l'unification à partir du titre ; le reverrouillage est en option. — *implémenté* — preuve `app/api/admin.py:440`, `app/services/manual_scrape_service.py:989-1025` · test `tests/test_admin_manual_scrape.py:221`, `tests/test_manual_scrape_apply.py:644`
- **FR-BE-957** — `POST …/rescrape` remet la ligne en file d'enrichissement. Sur une ligne verrouillée, il affiche l'état « verrouillé » et ne met rien en file. — *implémenté* — preuve `app/api/admin.py:462`, `app/services/media_service.py:735-754` · test `tests/test_admin_manual_scrape.py:240`, `tests/test_admin_manual_scrape.py:256`
- **FR-BE-958** — L'enrichissement exclut les lignes verrouillées de sa sélection, puis revérifie le verrou dans l'UPDATE. — *implémenté* — preuve `app/workers/enrichment_worker.py:59-77` · test `tests/test_manual_scrape_lock.py:72`, `tests/test_manual_scrape_lock.py:93`
- **FR-BE-959** — La synchro conserve les colonnes d'identité d'une ligne verrouillée avec un `CASE`, pas une `COALESCE`, qui ne distinguerait pas « jamais rempli » de « vidé exprès ». — *implémenté* — preuve `app/workers/sync_worker.py:754`, `app/workers/sync_worker.py:789` · test `tests/test_manual_scrape_lock.py:163`, `tests/test_manual_scrape_lock.py:212`
- **FR-BE-960** — Sur une ligne verrouillée, l'import NFO ne fait que compléter les ids et images, même avec `overwrite`. — *implémenté* — preuve `app/services/nfo_import_service.py:503-517` · test `tests/test_manual_scrape_lock.py:309`, `tests/test_manual_scrape_lock.py:334`
- **FR-BE-961** — `validate_id_consistency` et `dedup_resolved_twins` sautent les lignes verrouillées en `--apply`. — *implémenté* — preuve `app/scripts/validate_id_consistency.py:442`, `app/scripts/dedup_resolved_twins.py:172` · test `tests/test_manual_scrape_lock.py:416`, `tests/test_manual_scrape_lock.py:452`
- **FR-BE-962** — `PATCH /api/media/{rk}` et `…/rescrape` respectent le verrou (409 ou `locked`) ; un patch vide `{}` ne verrouille pas. — *implémenté* — preuve `app/services/media_service.py:651-733` · test `tests/test_manual_scrape_lock.py:508`, `tests/test_manual_scrape_lock.py:608`

### File « À vérifier »

- **FR-BE-963** — `decide()` est un moteur de règles pur, sans I/O. L'ordre d'évaluation est fixe : règle A, poster générique, règle B, raison de mise en revue. — *implémenté* — preuve `app/services/manual_scrape_service.py:1296-1375` · test `tests/test_manual_scrape_decide.py:82`
- **FR-BE-964** — Règle A : un même candidat `text_safe` avec un poster `identical` est appliqué. — *implémenté* — preuve `app/services/manual_scrape_service.py:1330-1337` · test `tests/test_manual_scrape_decide.py:89`
- **FR-BE-965** — Règle B (poster seul), active si `SCRAPE_BATCH_POSTER_ONLY_AUTO` : un unique poster `identical`, un titre assez proche et une année à ±1 an. Plusieurs posters identiques → `ambiguous_posters`. — *implémenté* — preuve `app/services/manual_scrape_service.py:1348-1357` · test `tests/test_manual_scrape_decide.py:124`, `tests/test_manual_scrape_decide.py:141`, `tests/test_manual_scrape_decide.py:152`
- **FR-BE-966** — Un poster Xtream générique empêche toute application sur preuve d'image (`xtream_poster_generic`). — *implémenté* — preuve `app/services/manual_scrape_service.py:1339-1342` · test `tests/test_manual_scrape_decide.py:103`
- **FR-BE-967** — Chaque mise en revue porte une raison distincte, montrée à l'opérateur (par exemple `identical_poster_weak_text` et `no_identical_poster`). — *implémenté* — preuve `app/services/manual_scrape_service.py:1277-1284`, `app/services/manual_scrape_service.py:1359-1375` · test `tests/test_manual_scrape_decide.py:60`, `tests/test_manual_scrape_decide.py:181`
- **FR-BE-968** — `upsert_review` fige les 5 meilleurs candidats en JSON et rouvre en `pending` une revue `resolved` ou `dismissed` revue par le lot. — *implémenté* — preuve `app/services/manual_scrape_service.py:1444-1494` · test `tests/test_manual_scrape_batch.py:396`, `tests/test_manual_scrape_batch.py:788`
- **FR-BE-969** — `GET /admin/review` pagine les revues `pending` et masque celles dont le média a disparu (`EXISTS`). — *implémenté* — preuve `app/api/admin.py:657`, `app/services/manual_scrape_service.py:1398-1425` · test `tests/test_manual_scrape_batch.py:629`
- **FR-BE-970** — Un `candidates_json` malformé donne une liste vide, jamais une exception. — *implémenté* — preuve `app/services/manual_scrape_service.py:1428-1441` · test : aucun
- **FR-BE-971** — `POST /admin/review/{sid}/{rk}/apply` applique le candidat stocké côté serveur à l'index choisi, jamais un id fourni par le client. Index hors bornes → 422 ; revue inconnue → 404. — *implémenté* — preuve `app/api/admin.py:672-703` · test `tests/test_admin_scrape_batch_ui.py:109`, `tests/test_admin_scrape_batch_ui.py:133`
- **FR-BE-972** — `POST …/dismiss` écarte l'item ; les lots suivants sautent les revues `pending` et `dismissed`. — *implémenté* — preuve `app/api/admin.py:730`, `app/services/manual_scrape_service.py:1518-1524`, `app/workers/manual_scrape_batch_worker.py:204-216` · test `tests/test_manual_scrape_batch.py:651`
- **FR-BE-973** — `resolve_review()` marque une revue `resolved` hors du chemin `apply_candidate`. — *mort* (aucun appelant dans `app/` ; l'apply résout déjà la revue dans sa transaction) — preuve `app/services/manual_scrape_service.py:1527-1533` · test `tests/test_manual_scrape_batch.py:788`

### Lot de scraping

- **FR-BE-974** — Les jobs sont stockés en mémoire dans le processus, 20 au plus, les plus anciens évincés en premier. — *implémenté* — preuve `app/workers/manual_scrape_batch_worker.py:70`, `app/workers/manual_scrape_batch_worker.py:116-119` · test `tests/test_manual_scrape_batch.py:614`
- **FR-BE-975** — `POST /admin/scrape-batch` (type, `dry_run`, `max_items`) refuse un second lot concurrent (409, même fragment). — *implémenté* — preuve `app/api/admin.py:749-770`, `app/workers/manual_scrape_batch_worker.py:162-187` · test `tests/test_manual_scrape_batch.py:595`, `tests/test_admin_scrape_batch_ui.py:201`
- **FR-BE-976** — `GET /admin/scrape-batch/status` rend la progression, relevée toutes les 2 s tant que le job est `queued` ou `running`. — *implémenté* — preuve `app/api/admin.py:776`, `app/templates/admin/_scrape_batch_status.html:3-9` · test `tests/test_admin_scrape_batch_ui.py:218`
- **FR-BE-977** — `POST /admin/scrape-batch/{job_id}/cancel` annule sans interrompre : l'arrêt est vérifié avant chaque item, et l'item en cours va au bout. — *implémenté* — preuve `app/api/admin.py:787`, `app/workers/manual_scrape_batch_worker.py:106-113`, `app/workers/manual_scrape_batch_worker.py:494-499` · test `tests/test_manual_scrape_batch.py:514`, `tests/test_admin_scrape_batch_ui.py:244`
- **FR-BE-978** — Passe 1 (appariement) : pour un item qui porte exactement un id, elle résout l'autre, sans recherche texte ni poster. — *implémenté* — preuve `app/workers/manual_scrape_batch_worker.py:340-394` · test `tests/test_manual_scrape_batch.py:192`, `tests/test_manual_scrape_batch.py:212`
- **FR-BE-979** — Si TMDB n'a pas d'`imdb_id`, repli OMDb en passe 1, accepté seulement aux critères STRONG (année exacte, titre très proche, type cohérent ; v1.10.7). — *implémenté* — preuve `app/workers/manual_scrape_batch_worker.py:289-337` · test `tests/test_manual_scrape_batch.py:252`, `tests/test_manual_scrape_batch.py:283`, `tests/test_manual_scrape_batch.py:306`
- **FR-BE-980** — Passe 2 : pour les items sans aucun id, elle appelle `search_candidates` puis `decide()` ; si rien n'est applicable, l'item part en revue. — *implémenté* — preuve `app/workers/manual_scrape_batch_worker.py:397-446` · test `tests/test_manual_scrape_batch.py:349`, `tests/test_manual_scrape_batch.py:368`
- **FR-BE-981** — La sélection du lot exclut les médias `is_adult`, les lignes verrouillées et celles qui ont déjà une revue ouverte ou écartée (v1.10.6). — *implémenté* — preuve `app/workers/manual_scrape_batch_worker.py:204-241` · test `tests/test_manual_scrape_batch.py:426`, `tests/test_manual_scrape_batch.py:446`, `tests/test_manual_scrape_batch.py:461`
- **FR-BE-982** — Budget d'appels TMDB compté par une tally de contexte (`count_requests()`), pas par le compteur global que l'enrichissement remet à zéro. Épuisement → arrêt propre (`budgetExhausted`). — *implémenté* — preuve `app/workers/manual_scrape_batch_worker.py:498-502` · test `tests/test_manual_scrape_batch.py:481`, `tests/test_manual_scrape_batch.py:812`
- **FR-BE-983** — `max_items` plafonne le nombre d'items parcourus. — *implémenté* — preuve `app/config.py:319-321` · test `tests/test_manual_scrape_batch.py:500`
- **FR-BE-984** — En simulation (`dry_run`), rien n'est écrit (ni `media`, ni `scrape_review`, ni cache) ; seul l'histogramme des distances pHash est rempli, pour calibrer la règle B. — *implémenté* — preuve `app/workers/manual_scrape_batch_worker.py:271-282`, `app/workers/manual_scrape_batch_worker.py:383-385` · test `tests/test_manual_scrape_batch.py:545`
- **FR-BE-985** — Un seul rebuild de snapshot par type touché, en fin de lot. — *implémenté* — preuve `app/workers/manual_scrape_batch_worker.py:599-609` · test `tests/test_manual_scrape_apply.py:203`
- **FR-BE-986** — Une erreur TMDB/OMDb sur un item compte comme erreur d'item ; le lot continue. — *implémenté* — preuve `app/workers/manual_scrape_batch_worker.py:512-523` · test : aucun dédié trouvé
- **FR-BE-987** — Supprimer un compte Xtream purge ses revues. — *implémenté* — preuve `app/services/account_service.py:159` · test `tests/test_manual_scrape_batch.py:725`

## Règles métier

- **RG-BE-22.1** — Règle A : `text_safe` et poster `identical` sur le **même** candidat → apply. — preuve `app/services/manual_scrape_service.py:1330-1337`
- **RG-BE-22.2** — Règle B : désactivée par défaut (`SCRAPE_BATCH_POSTER_ONLY_AUTO=false`) ; quand elle est active, il faut un seul poster `identical`, un score titre ≥ `RULE_B_MIN_TITLE_SCORE` et une année à ±1 an. — preuve `app/services/manual_scrape_service.py:78`, `app/services/manual_scrape_service.py:1348-1357`
- **RG-BE-22.3** — `identical` si la distance pHash ≤ `POSTER_PHASH_IDENTICAL` **et** la distance dHash ≤ `POSTER_DHASH_IDENTICAL` ; `close` si la distance pHash ≤ `POSTER_PHASH_CLOSE` ; sinon `different` ; `unknown` si un hash manque. — preuve `app/services/poster_match_service.py:316-323`
- **RG-BE-22.4** — Un poster générique n'est jamais `identical` et bloque les règles A et B. — preuve `app/services/poster_match_service.py:591-593`, `app/services/manual_scrape_service.py:1339-1342`
- **RG-BE-22.5** — Score combiné = poids texte × confiance + poids image × score image, seulement si le badge n'est pas `unknown`. — preuve `app/services/manual_scrape_service.py:77`, `app/services/manual_scrape_service.py:442-454`
- **RG-BE-22.6** — L'UPDATE d'un écrivain de lot porte lui-même la condition `match_locked == False`. — preuve `app/services/manual_scrape_service.py:864-872`
- **RG-BE-22.7** — Budgets du lot réglables par `SCRAPE_BATCH_TMDB_LIMIT`, `SCRAPE_BATCH_MAX_ITEMS` et `SCRAPE_BATCH_CONCURRENCY`. — preuve `app/config.py:319-321`
- **RG-BE-22.8** — Le rebuild du snapshot après un apply interactif est retardé (`SCRAPE_REBUILD_DEBOUNCE_SECONDS`, défaut 60 s) et regroupé par type. — preuve `app/config.py:304`, `app/services/unified_group_service.py:286`
- **RG-BE-22.9** — Imbrication de `count_requests()` (TMDB et OMDb) : un appel est compté dans le bloc courant **et** dans tous les blocs qui l'englobent. Sans ce cumul, le budget du lot ne verrait pas les appels de `search_candidates`. — preuve `app/services/tmdb_service.py:153-197`, `app/services/omdb_service.py:121-141`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| Apply sans id, ou `tmdb_id` non numérique | 422 avec message | `app/api/admin.py:313-339` |
| Id déjà porté par un autre item, sans « forcer » | 409, candidats réaffichés avec la case « forcer » | `app/api/admin.py:401-406` |
| Candidat introuvable chez le fournisseur | 422 `provider_not_found` | `app/api/admin.py:407-412` |
| Ligne verrouillée entre-temps (écriture de lot) | `skipped_locked` → 409 | `app/api/admin.py:413-418` |
| Base verrouillée pendant l'apply | 409 lisible, jamais 500 | `app/api/admin.py:341-376` |
| Ligne inconnue | 404 | `app/api/admin.py:688-689` |
| Lot déjà en cours | 409 sur `POST /admin/scrape-batch` | `app/api/admin.py:761-770` |
| Annulation pendant un lot | L'item en cours se termine, puis arrêt | `app/workers/manual_scrape_batch_worker.py:494-499` |
| Budget TMDB épuisé | Arrêt propre, `budgetExhausted` | `app/workers/manual_scrape_batch_worker.py:498-502` |
| TMDB en panne en recherche interactive | Liste vide, pas de 500 | `app/services/manual_scrape_service.py:205-230` |
| Réponse poster `image/svg+xml` | Refusée, jamais servie | `app/services/poster_match_service.py:419-426` |
| Index de candidat hors bornes (file « À vérifier ») | 422 | `app/api/admin.py:692-693` |
| Compte Xtream supprimé | Revues du compte purgées | `app/services/account_service.py:159` |
| Redémarrage pendant un lot | État du lot perdu (store en mémoire) | `app/workers/manual_scrape_batch_worker.py:70` |

## Dépendances

- Autres capacités : `CAP-BE-21` (console : tableau, filtres `locked`/`batch`/`review`, compteurs, saisie d'ids), `CAP-BE-06` (enrichissement : partage `media_identity_writer`, `tmdb_service` et `omdb_service`, respecte le verrou), `CAP-BE-05` (la synchro respecte le verrou), `CAP-BE-23` (l'import NFO respecte le verrou), `CAP-BE-09` (snapshot `media_group` reconstruit après correction), `CAP-BE-16` (fichiers générés non réécrits), `CAP-BE-04` (suppression de compte), `CAP-BE-12` (le mode `replace` réécrit la bande-annonce).
- Autre dépôt : aucun appel `tv:` au scraper ni aux routes `/admin` (recherche vide dans `PlexHubTV`) ; `match_locked`/`match_source` ne figurent pas dans `app/models/schemas.py`. L'app ne voit que les identités corrigées.
- Données : `media.match_locked`/`match_source` (M026), `scrape_review` (M027, modèle `app/models/database.py:706`) ; schéma : `docs/retro-spec/technique/db/`.

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| CLAUDE.md §2 annonce « +16 routes du scraper manuel » ; il y en a 14 nouvelles dans `app/api/admin.py` (le compte de 16 inclut sans doute les alias ou routes conservées). | spec ↔ code (doc) | Corriger CLAUDE.md au prochain `/sync-context` |
| L'ADR 0005 (D11 et tableau de config) donne 5.0 comme défaut de `SCRAPE_REBUILD_DEBOUNCE_SECONDS` ; le code vaut 60.0. | spec ↔ code | Mettre à jour l'ADR |
| L'ADR spécifiait un `count_requests()` qui ne cumulait pas les blocs imbriqués ; le code livré les cumule, et c'est ce cumul qui fait fonctionner le budget du lot (piège 21f). | spec ↔ code (le code a raison) | Mettre à jour l'ADR |
| Numéros de ligne périmés dans CLAUDE.md piège 21 : admin.py:482 pour `nosniff` (réel `app/api/admin.py:529`), base.html:36 et base.html:23 pour les auditeurs (réels `app/templates/admin/base.html:104` et `app/templates/admin/base.html:79`). | doc périmée | `/sync-context` |
| flux-cles.md §5.14 cite des lignes décalées (apply :282, proxy :445, tiroir :487, decide :1208…). | doc périmée | `/sync-context` sur §5.14 |
| Les NFO et posters déjà générés ne sont pas réécrits après une correction (ADR D12). | limite fonctionnelle | À trancher par Chakir |
| Déverrouillage uniquement unitaire, alors qu'un lot peut verrouiller des centaines de lignes. | asymétrie fonctionnelle | À trancher par Chakir |
| `resolve_review()` exporté et testé, sans appelant. | code mort | Supprimer ou brancher |
| En-têtes du proxy (`nosniff`, `Cache-Control`) et JSON de revue malformé : aucun test dédié. | test manquant | Ajouter deux tests |

## Questions ouvertes

- Le store de jobs est propre au processus : avec plusieurs workers `uvicorn`, `is_running()` laisserait passer deux lots concurrents. Le déploiement garantit-il un seul processus ? — à vérifier par test
- Une erreur fournisseur sur un item de lot (FR-BE-986) est-elle bien comptée sans arrêter le lot, et l'item est-il ignoré plutôt que mis en revue ? — à vérifier par test
- Faut-il exposer `match_locked`/`match_source` dans `/api/media`, pour que l'app TV distingue une identité fixée à la main ? — à trancher par Chakir
- `SCRAPE_BATCH_POSTER_ONLY_AUTO` a-t-il été calibré en production (histogramme d'une simulation) ? Le code ne le dit pas. — à trancher par Chakir
