# CAP-BE-21 — Console admin du catalogue

**Reconstituée le** : 2026-09-26 · recalée le 2026-10-02 (Lot 10 sous-lot C) · **HEAD** : `aa3270f` · **Sources** : code, gabarits Jinja2, tests, `CLAUDE.md` (§2 `admin`/`templates/admin`, §9 piège 21), ADR `docs/architecture/adr/0005-manual-scraper-poster-match.md`, app TV (lecture seule, preuves `tv:`). Aucun PRD ne couvre cette capacité : `docs/10-prd-*.md` ne traite que les téléchargements. Il n'y a donc pas de tableau d'écart fiche ↔ PRD ; les écarts sont face à `CLAUDE.md` et aux commentaires du code.
**Périmètre** : page `/admin` (`app/templates/admin/index.html`), routes `GET /admin`, `GET /admin/`, `GET /admin/catalogue`, `GET /admin/stats`, `POST /admin/movies/{rating_key}/ids`, `POST /admin/movies/{rating_key}/rescrape` (`app/api/admin.py:125-246` ; les alias `GET /admin/movies` et `GET /admin/movies/stats` ont été retirés au Lot 10), gabarits `base.html`, `index.html`, `_stats.html`, `_media_table.html`, `_media_row.html`, services `manual_scrape_service.list_catalogue`/`catalogue_stats` et `media_service.update_external_ids`/`enqueue_rescrape`, garde Basic Auth du montage et garde CSRF. Inventaire des routes : `docs/retro-spec/_genere/inventaire.md`. Hors périmètre : le tiroir de scrape, le proxy de poster, les boutons Appliquer/Déverrouiller/Effacer de la ligne, la file « À vérifier » et le lot (CAP-BE-22) ; l'import NFO (CAP-BE-23) ; les clés (CAP-BE-02) ; les téléchargements (CAP-BE-18/19/20).

## Résumé

La console `/admin` est une UI HTML rendue côté serveur (Jinja2 + htmx 1.9, Tailwind), réservée à l'opérateur et protégée par une Basic Auth dédiée et une garde CSRF. Elle affiche des compteurs de couverture d'identifiants (films et séries), un bandeau de panne fournisseur, et un tableau paginé du catalogue, filtrable par type, état des identifiants, titre et tri, sans rechargement de page. Chaque ligne permet de saisir un id IMDb ou TMDB : une vraie modification recalcule le regroupement du titre et **verrouille** la ligne contre les écrivains automatiques. La console héberge aussi le scraper manuel, la file « À vérifier » et le lot de scraping (CAP-BE-22). Trois routes héritées d'avant l'ADR 0005 répondent encore mais ne sont plus appelées par aucun gabarit. L'app TV n'utilise pas cette console.

## User stories observées

- **US-BE-21.1** (P1) — En tant qu'opérateur, j'ouvre `/admin` et je vois d'emblée les films aux identifiants incomplets, afin de prioriser le rattrapage.
  - Parcours : `/admin` → tableau filtré `incomplete` · Maestro : sans objet (UI navigateur)
- **US-BE-21.2** (P1) — En tant qu'opérateur, je filtre, cherche, trie et pagine films ou séries sans recharger la page, afin de travailler vite sur un gros catalogue.
  - Parcours : formulaire de filtres → fragment `/admin/catalogue` · Maestro : sans objet
- **US-BE-21.3** (P1) — En tant qu'opérateur, je corrige ou j'efface l'id IMDb ou TMDB d'une ligne, et la correction tient face au prochain sync ou enrichissement.
  - Parcours : ligne → « Sauver » → `POST /admin/movies/{rk}/ids` · Maestro : sans objet
- **US-BE-21.4** (P2) — En tant qu'opérateur, je vois des compteurs par type (total, sans IMDb, sans TMDB, les deux, verrouillés, à vérifier), rafraîchis après chaque action.
  - Parcours : bandeau `_stats.html` ← événement `refresh-stats` · Maestro : sans objet
- **US-BE-21.5** (P2) — En tant qu'opérateur, je vois quels comptes Xtream sont en panne et si leur catalogue est masqué côté app, alors que la console, elle, montre tout.
  - Parcours : bandeau de panne dans `_stats.html` · Maestro : sans objet
- **US-BE-21.6** (P1) — En tant qu'opérateur, je suis seul à pouvoir modifier le catalogue : un site tiers ne peut pas poster à ma place.
  - Parcours : Basic Auth + garde `Sec-Fetch-Site` · Maestro : sans objet
- **US-BE-21.7** (P3) — En tant que client hérité d'avant l'ADR 0005, j'appelais `GET /admin/movies` et `GET /admin/movies/stats`.
  - Parcours : appels directs uniquement, aucun gabarit ne les employait ; routes retirées au Lot 10 (`POST /admin/movies/{rk}/ids` et `/rescrape` subsistent) · Maestro : sans objet

Côté app TV : aucun parcours (voir Dépendances).

## Exigences fonctionnelles

### Accès, sécurité et coquille de la page

- **FR-BE-880** — Tout le router `admin` est monté hors `/api`, avec la dépendance `verify_admin_basic_auth` posée au montage, sans garde propre à chaque route. Les identifiants sont comparés en temps constant ; un échec donne 401 avec `WWW-Authenticate: Basic`. — *implémenté* — preuve `app/main.py:680`, `app/api/deps.py:113-145` · test : les tests passent l'auth (`tests/test_admin.py:38`) ; aucun test de rejet propre aux routes de cette capacité (le plus proche : `tests/test_admin_scrape_ui.py:508`, routes du scraper)
- **FR-BE-881** — Si `ADMIN_PASSWORD` est vide, toute la console répond 503 (fail-closed), pas 401. — *implémenté* — preuve `app/api/deps.py:123-127` · test : aucun trouvé dans `tests/test_admin*.py`
- **FR-BE-882** — Tout `POST`/`PUT`/`PATCH`/`DELETE` dont le chemin commence par `/admin` et qui porte `Sec-Fetch-Site: cross-site` est rejeté en 403 (`Cross-site request rejected`). `same-origin`, `same-site` et l'en-tête absent passent ; les `GET` ne sont jamais interceptés. — *implémenté* — preuve `app/api/csrf.py:85-114`, `app/main.py:641-642` · test `tests/test_admin_csrf.py:155-156`, `tests/test_admin_csrf.py:171`, `tests/test_admin_csrf.py:180`, `tests/test_admin_csrf.py:88`, `tests/test_admin_csrf.py:102`
- **FR-BE-883** — `/docs` et `/openapi.json` sont servis derrière la même Basic Auth que la console. — *implémenté* — preuve `app/main.py:706-713` · test : aucun trouvé
- **FR-BE-884** — La barre de navigation de toutes les pages admin mène à Catalogue, Importer NFO, Télécharger, Télécharger Plex, Téléchargements et Clés API. — *implémenté* — preuve `app/templates/admin/base.html:118-125` · test : aucun
- **FR-BE-885** — htmx 1.9.12 et Tailwind sont chargés depuis des CDN publics (`unpkg.com`, `cdn.tailwindcss.com`), non embarqués dans l'image. — *implémenté* — preuve `app/templates/admin/base.html:7-8` · test : aucun
- **FR-BE-886** — Les réponses 409 et 422 sont forcées à s'afficher (htmx 1.x n'affiche pas les 4xx par défaut) ; les autres erreurs, dont 500, restent silencieuses à l'écran. — *implémenté* — preuve `app/templates/admin/base.html:104-110` · test : aucun (comportement navigateur)

### Page et tableau du catalogue

- **FR-BE-887** — `GET /admin` et `GET /admin/` rendent la page complète : bandeau de stats, formulaire de filtres, cible du tiroir de scrape, tableau, bloc du lot et section « À vérifier ». Par défaut : films, filtre `incomplete`, tri `added_desc`, page 1, 50 lignes. — *implémenté* — preuve `app/api/admin.py:125-146`, `app/templates/admin/index.html:4-142` · test `tests/test_admin.py:38`, `tests/test_admin_scrape_ui.py:69`
- **FR-BE-888** — Le paramètre `type` accepte `movie`, `show`, `series` et `tv` (sans casse) ; toute autre valeur retombe sur `movie`, sans 422. — *implémenté* — preuve `app/api/admin.py:78-81` · test `tests/test_admin_scrape_ui.py:85`
- **FR-BE-889** — Le paramètre `ids` accepte huit filtres : `all`, `missing_imdb`, `missing_tmdb`, `missing_both`, `incomplete` (l'un des deux manque), `locked` (`match_locked`), `batch` (`match_source` `batch_auto` ou `batch_pair`), `review` (revue en attente) ; une valeur inconnue retombe sur `incomplete`. Un id vide (`""`) compte comme absent. — *implémenté* — preuve `app/api/admin.py:72-85`, `app/services/manual_scrape_service.py:1051-1052`, `app/services/manual_scrape_service.py:1075-1091` · test `tests/test_admin_scrape_ui.py:98`, `tests/test_admin_scrape_ui.py:141`
- **FR-BE-890** — La recherche filtre le titre par `ILIKE %terme%`, en échappant `\`, `%` et `_`. — *implémenté* — preuve `app/services/manual_scrape_service.py:1101-1103` · test `tests/test_admin_scrape_ui.py:155`
- **FR-BE-891** — Cinq tris : date d'ajout décroissante ou croissante, titre A→Z ou Z→A, année décroissante ; une valeur inconnue retombe sur la date d'ajout décroissante. — *implémenté* — preuve `app/services/manual_scrape_service.py:1043-1049`, `app/services/manual_scrape_service.py:1135`, `app/templates/admin/index.html:65-72` · test : aucun pour le repli
- **FR-BE-892** — Pagination : `page ≥ 1`, `page_size` entre 10 et 500 (défaut 50), rejet 422 par FastAPI hors bornes ; boutons Précédent et Suivant, total et « page n / N » sous le tableau. — *implémenté* — preuve `app/api/admin.py:133-134`, `app/api/admin.py:156-157`, `app/templates/admin/_media_table.html:1`, `app/templates/admin/_media_table.html:28-51` · test : aucun sur les bornes
- **FR-BE-893** — Les filtres s'appliquent sans rechargement (déclencheurs `change`, `keyup` avec délai de 300 ms, `submit`) et remplacent seulement `#media-table`. L'URL n'est pas mise à jour (`hx-push-url="false"`). — *implémenté* — preuve `app/templates/admin/index.html:15-22` · test : aucun (comportement navigateur)
- **FR-BE-894** — `GET /admin/catalogue` renvoie le seul fragment du tableau, avec les mêmes paramètres que la page ; le formulaire de filtres et la pagination l'appellent. — *implémenté* — preuve `app/api/admin.py:149-164`, `app/templates/admin/_media_table.html:32-47` · test `tests/test_admin_scrape_ui.py:85`, `tests/test_admin_scrape_ui.py:98`, `tests/test_admin_scrape_ui.py:155`
- **FR-BE-895** — Le tableau ne montre que les médias en catégorie autorisée (`is_in_allowed_categories`) et **n'applique pas** le masquage de panne fournisseur : c'est une vue de diagnostic complète. — *implémenté* — preuve `app/services/manual_scrape_service.py:1094-1100` · test : aucun dédié
- **FR-BE-896** — Le tableau est dédoublonné à une ligne par `(server_id, rating_key)`, alors que `media` porte une ligne par catégorie ; le total compte les items distincts. — *implémenté* — preuve `app/services/manual_scrape_service.py:1120-1143` · test `tests/test_admin_scrape_ui.py:128`
- **FR-BE-897** — Chaque ligne montre : vignette (proxy de poster, CAP-BE-22), titre, 120 premiers caractères du résumé, pastilles IMDb/TMDB, source du match, confiance TMDB en %, année, `server_id`, liens externes IMDb et TMDB (chemin `tv` pour une série), et l'état verrouillé ou non. — *implémenté* — preuve `app/templates/admin/_media_row.html:13-40`, `app/templates/admin/_media_row.html:83-98`, `app/templates/admin/_media_row.html:115-124` · test : aucun dédié
- **FR-BE-898** — Un résultat vide affiche « Aucun résultat. ». — *implémenté* — preuve `app/templates/admin/_media_table.html:21-22` · test : aucun
- **FR-BE-899** — Le paramètre `tab` (`catalogue` ou `review`) était lu et posé dans le contexte de `GET /admin`, mais aucun gabarit ne le lisait : il n'avait aucun effet. — *retiré* (Lot 10 : paramètre et clé de contexte supprimés ; Pydantic/FastAPI ignorent un `?tab=` résiduel) — preuve de l'absence `app/api/admin.py:125-146` (signature de `admin_index` sans `tab`) · test : aucun
- **FR-BE-900** — `list_catalogue` calculait `review_keys` (items de la page en revue) par une requête supplémentaire à chaque page, transmis au gabarit, qui ne l'utilisait pas. — *retiré* (Lot 10 : champ `CataloguePage.review_keys`, requête et clé de contexte supprimés) — preuve de l'absence `app/services/manual_scrape_service.py:179-185` (`CataloguePage` sans `review_keys`), `app/services/manual_scrape_service.py:1144-1149`, `app/api/admin.py:88-112` · test : aucun
- **FR-BE-901** — Au rechargement de `/admin`, le dernier lot de scraping est réaffiché (suivi et bouton Annuler), pour ne pas afficher « aucun lot » pendant qu'un lot tourne. — *implémenté* — preuve `app/api/admin.py:142-145` · test : couvert côté CAP-BE-22

### Compteurs et bandeau de panne

- **FR-BE-902** — Le bandeau affiche, pour les films puis les séries : total, sans IMDb, sans TMDB, les deux, verrouillés, à vérifier, et le pourcentage d'items ayant les deux ids. Les cinq premiers compteurs sortent d'un seul passage SQL sur les items distincts en catégorie autorisée ; « à vérifier » compte les revues en attente dont le média existe encore en catégorie autorisée. — *implémenté* — preuve `app/services/manual_scrape_service.py:1168-1251`, `app/templates/admin/_stats.html:1-48` · test `tests/test_admin_scrape_ui.py:541`
- **FR-BE-903** — Le bandeau se recharge seul via `GET /admin/stats` à chaque événement `refresh-stats` ; l'alias `GET /admin/movies/stats` n'existe plus (FR-BE-919). — *implémenté* — preuve `app/templates/admin/_stats.html:41-43`, `app/api/admin.py:167-171` · test `tests/test_admin_scrape_ui.py:168-176`
- **FR-BE-904** — Le contexte des stats portait trois clés « rétro-compatibles » (`total_movies`, `missing_imdb_count`, `missing_tmdb_count`) qu'aucun gabarit ni aucun test ne lisait. — *retiré* (Lot 10 : les trois clés sont supprimées de `_stats_ctx`) — preuve de l'absence `app/api/admin.py:115-122` · test : aucun
- **FR-BE-905** — Le bandeau liste chaque compte en panne (libellé, id, nombre de runs en échec, date de début) et dit si son catalogue est masqué côté app (rouge) ou encore visible (orange). — *implémenté* — preuve `app/api/admin.py:121`, `app/templates/admin/_stats.html:49-66` · test : aucun dans le périmètre (service couvert par CAP-BE-08)

### Édition des identifiants

- **FR-BE-906** — Chaque ligne porte deux petits formulaires séparés, un pour IMDb et un pour TMDB, qui postent un seul champ plus le `server_id` caché vers `POST /admin/movies/{rating_key}/ids`. Le navigateur pré-valide le format (`pattern`). — *implémenté* — preuve `app/templates/admin/_media_row.html:42-81` · test `tests/test_admin_csrf.py:155`
- **FR-BE-907** — `server_id` est un champ obligatoire (422 s'il manque) : la ligne ciblée est `(rating_key, server_id)`, sans ambiguïté entre comptes malgré l'URL sans `server_id`. — *implémenté* — preuve `app/api/admin.py:174-182` · test : aucun sur le champ manquant
- **FR-BE-908** — Validation : espaces retirés, chaîne vide = effacement, IMDb conforme à `^tt\d{7,10}$` (préfixe `tt` en minuscules), TMDB conforme à `^\d{1,9}$`. Un format invalide renvoie 422 avec la ligne réaffichée et le message d'erreur, sans écriture. — *implémenté* — preuve `app/models/schemas.py:8-9`, `app/models/schemas.py:234-260`, `app/api/admin.py:191-202` · test : aucun test HTTP du 422 sur cette route
- **FR-BE-909** — Une ligne inconnue donne 404, en cas d'erreur de validation comme de succès. — *implémenté* — preuve `app/api/admin.py:194-196`, `app/api/admin.py:211-212` · test `tests/test_admin_csrf.py:180`
- **FR-BE-910** — Un envoi qui ne change rien (même valeur, ou effacement d'un id déjà vide) n'écrit rien et ne verrouille pas. La route affiche pourtant « ✓ enregistré » et émet `refresh-stats`, comme pour un vrai changement. — *implémenté* — preuve `app/services/media_service.py:656-680`, `app/api/admin.py:208-219` · test `tests/test_manual_scrape_lock.py:608` (patch vide seulement ; le renvoi de la même valeur n'est pas testé)
- **FR-BE-911** — Un vrai changement recalcule `unification_id` et `history_group_key` (priorité IMDb puis TMDB), pose `match_locked = vrai`, `match_source = "manual"` et `updated_at`. — *implémenté* — preuve `app/services/media_service.py:682-704` · test `tests/test_manual_scrape_lock.py:589`, `tests/test_manual_scrape_lock.py:627`, `tests/test_manual_scrape_lock.py:643`
- **FR-BE-912** — Le changement s'applique à toutes les lignes-catégories du même `(rating_key, server_id)`. — *implémenté* — preuve `app/services/media_service.py:705-709` · test `tests/test_manual_scrape_lock.py:665`
- **FR-BE-913** — Effacer un id existant est un vrai changement : la ligne est verrouillée elle aussi, et l'enrichissement ne la remplira plus tant qu'elle n'est pas déverrouillée (bouton de CAP-BE-22). — *implémenté* — preuve `app/models/schemas.py:242-243`, `app/services/media_service.py:676-704` · test : aucun
- **FR-BE-914** — Après un vrai changement, une reconstruction du snapshot unifié est planifiée (anti-rebond) pour le type du média, sans attendre le prochain pipeline. — *implémenté* — preuve `app/services/media_service.py:713-722` · test : aucun dans le périmètre
- **FR-BE-915** — En cas de succès, la ligne est réaffichée sur fond vert avec « ✓ enregistré », et l'en-tête `HX-Trigger: refresh-stats` rafraîchit les compteurs. — *implémenté* — preuve `app/api/admin.py:214-219`, `app/templates/admin/_media_row.html:12`, `app/templates/admin/_media_row.html:110` · test : en-tête non vérifié par un test

### Re-scrape et routes héritées

- **FR-BE-916** — La mise en file de re-scrape refuse d'écrire si la ligne est verrouillée (`locked`), renvoie `not_found` si le média n'existe pas, sinon remet à `pending` la ligne `enrichment_queue` existante (tentatives à 0, erreur et date de traitement effacées, instantané des ids et du résumé) ou en crée une. — *implémenté* — preuve `app/services/media_service.py:725-777` · test `tests/test_manual_scrape_lock.py:508`, `tests/test_manual_scrape_lock.py:520`, `tests/test_manual_scrape_lock.py:530`
- **FR-BE-917** — `POST /admin/movies/{rating_key}/rescrape` traduit ces issues : 404, ligne « 🔒 verrouillé — déverrouillage requis » sans rafraîchissement, ou ligne « en file d'attente » avec `refresh-stats`. Aucun gabarit ne l'appelle : le bouton Re-scrape de la ligne poste vers `/admin/media/{server_id}/{rating_key}/rescrape` (CAP-BE-22). — *mort* — preuve `app/api/admin.py:222-246`, `app/templates/admin/_media_row.html:99-107` · test `tests/test_manual_scrape_lock.py:571`, `tests/test_admin_csrf.py:156`
- **FR-BE-918** — `GET /admin/movies` (alias d'avant l'ADR 0005) traduisait `missing_imdb`/`missing_tmdb` en filtre `ids` (les deux → `incomplete`, aucun → `all`), films seulement. Aucun gabarit ne l'appelait. — *retiré* (Lot 10 : route supprimée ; les tests passent par `GET /admin/catalogue?ids=…`) — preuve de l'absence `app/api/admin.py:149-171` (le fragment du catalogue est `GET /admin/catalogue`) · test `tests/test_admin.py:26-35`, `tests/test_admin.py:61-68` (visent désormais `/admin/catalogue`)
- **FR-BE-919** — `GET /admin/movies/stats` restait servi, mais seul `/admin/stats` était appelé par le gabarit. — *retiré* (Lot 10 : l'alias est supprimé, `GET /admin/stats` est la seule route) — preuve de l'absence `app/api/admin.py:167-171`, `app/templates/admin/_stats.html:41` · test `tests/test_admin_scrape_ui.py:168-176`

### Annoncé, introuvable

- **FR-BE-920** — `index.html` présente un onglet `catalogue|review`. — *absent* (annoncé par `CLAUDE.md` §2, puce `templates/admin/` ; `app/templates/admin/index.html` n'a aucun onglet : le tableau et la section « À vérifier » sont affichés ensemble ; le paramètre `tab` inerte a été retiré, FR-BE-899)

## Règles métier

- **RG-BE-21.1** — Une saisie manuelle d'identifiant qui change réellement la valeur verrouille la ligne. À partir de là, sync, enrichissement, import NFO et scripts de cohérence ne réécrivent plus ses ids. Le déverrouillage n'existe pas dans cette capacité (bouton « Déverrouiller » de CAP-BE-22). — preuve `app/services/media_service.py:699-704`, `app/templates/admin/_media_row.html:134-142`
- **RG-BE-21.2** — « Pas d'id » a deux écritures en base (`NULL` et `""`) qui sont traitées comme équivalentes, pour les filtres comme pour la détection d'un vrai changement. — preuve `app/services/manual_scrape_service.py:1051-1052`, `app/services/media_service.py:676-679`
- **RG-BE-21.3** — Le regroupement suit l'id le plus fort : IMDb d'abord, puis TMDB, puis titre et année. — preuve `app/services/media_service.py:687-694` · test `tests/test_manual_scrape_lock.py:643`
- **RG-BE-21.4** — La console voit tout ce que la panne fournisseur masque à l'app, mais reste limitée aux catégories autorisées, comme l'app. — preuve `app/services/manual_scrape_service.py:1094-1100`
- **RG-BE-21.5** — Les compteurs se font sur les items distincts. Pour « sans IMDb » et « sans TMDB », un item compte dès qu'une de ses lignes-catégories manque l'id ; pour « les deux » et « verrouillés », dès qu'une ligne remplit la condition. — preuve `app/services/manual_scrape_service.py:1181-1198` · test `tests/test_admin_scrape_ui.py:541`
- **RG-BE-21.6** — Une ligne verrouillée n'est jamais remise en file d'enrichissement. — preuve `app/services/media_service.py:742-743`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| `ADMIN_PASSWORD` vide | 503 sur toute la console | `app/api/deps.py:123-127` |
| Identifiants absents ou faux | 401 + `WWW-Authenticate: Basic` | `app/api/deps.py:128-145` |
| POST avec `Sec-Fetch-Site: cross-site` | 403 | `app/api/csrf.py:98-114` |
| `type`, `ids` ou `sort` inconnus | Repli silencieux sur la valeur par défaut, 200 | `app/api/admin.py:78-85`, `app/services/manual_scrape_service.py:1135` |
| `page < 1` ou `page_size` hors 10..500 | 422 FastAPI | `app/api/admin.py:133-134` |
| Rechargement de la page après filtrage | Les filtres reviennent aux défauts (URL jamais mise à jour) | `app/templates/admin/index.html:22` |
| Aucun résultat | « Aucun résultat. » | `app/templates/admin/_media_table.html:22` |
| Id au mauvais format | 422, ligne rouge avec le message, rien d'écrit | `app/api/admin.py:191-202` |
| `server_id` absent du formulaire | 422 FastAPI | `app/api/admin.py:178` |
| Ligne inconnue | 404 | `app/api/admin.py:194-196`, `app/api/admin.py:211-212` |
| Renvoi de la valeur déjà stockée | Rien d'écrit, pas de verrou, mais « ✓ enregistré » et compteurs rafraîchis | `app/services/media_service.py:676-680`, `app/api/admin.py:214-219` |
| Effacement d'un id existant | Id mis à `NULL`, ligne verrouillée | `app/models/schemas.py:242-243`, `app/services/media_service.py:702-703` |
| Re-scrape (route héritée) d'une ligne verrouillée | 200, « verrouillé — déverrouillage requis », pas de mise en file | `app/api/admin.py:233-240` |
| Réponse 500 d'une action | Rien ne change à l'écran (seuls 409/422 sont affichés) | `app/templates/admin/base.html:98-110` |
| CDN htmx ou Tailwind injoignable | Page sans style ni interactions (non testé) | `app/templates/admin/base.html:7-8` |
| Compte en panne | Ses médias restent listés ; bandeau rouge ou orange | `app/services/manual_scrape_service.py:1095-1096`, `app/templates/admin/_stats.html:52-62` |

## Dépendances

- Autres capacités : `CAP-BE-02` (Basic Auth admin, garde CSRF, page des clés) ; `CAP-BE-22` (tiroir de scrape, proxy de poster, boutons Appliquer, Déverrouiller, Effacer et Re-scrape de la ligne, section « À vérifier », lot) ; `CAP-BE-08` (`list_outages` pour le bandeau) ; `CAP-BE-06` (le worker d'enrichissement consomme `enrichment_queue` et honore le verrou) ; `CAP-BE-09` (reconstruction du snapshot unifié, lu par les endpoints `/api/media/*/unified`) ; `CAP-BE-23`, `CAP-BE-18`, `CAP-BE-19`, `CAP-BE-20` (liens de la barre de navigation).
- Autre dépôt : l'app TV n'appelle jamais `/admin*`. Son interface backend ne déclare que des chemins `api/…` (`tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:11`, `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:17`). Elle voit l'effet des corrections d'ids par le catalogue unifié.
- Données : `media` (lecture, et écriture des ids, du verrou, de `unification_id`), `enrichment_queue`, `scrape_review` (lecture), `xtream_accounts` (bandeau de panne), `media_group`/`media_group_member` (reconstruction planifiée). Schéma : `docs/retro-spec/technique/db/`.

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| `CLAUDE.md` §2 place le montage du router `admin` en main.py:412 ; il est en `app/main.py:680` (main.py:412 est aujourd'hui une tâche planifiée sans rapport). | spec ↔ code (référence périmée) | Corriger à la prochaine passe `/sync-context`. |
| `CLAUDE.md` §9 piège 21e cite base.html:36 pour l'opt-in `htmx:beforeSwap` 409/422 et base.html:23 pour l'auditeur `close-scrape-panel` ; ils sont en `app/templates/admin/base.html:104-110` et `app/templates/admin/base.html:79`. | spec ↔ code (références périmées) | Idem. |
| `CLAUDE.md` §2 annonce l'onglet `catalogue|review` dans `index.html` ; aucun onglet n'existe (FR-BE-920). Le paramètre `tab` inerte (FR-BE-899) est retiré. | spec ↔ code | Corriger la doc, ou faire l'onglet : à trancher par Chakir. |
| Le commentaire de `update_external_ids` dit que le formulaire de ligne renvoie les deux ids à chaque « Sauver » (`app/services/media_service.py:666-671`) ; la ligne a deux formulaires séparés d'un seul champ chacun (`app/templates/admin/_media_row.html:42-81`). | commentaire ↔ code | Mettre le commentaire à jour ; la garde de non-changement reste utile. |
| Le commentaire des stats disait que l'alias `/admin/movies/stats` et ses tests lisaient `total_movies`, `missing_imdb_count`, `missing_tmdb_count` ; personne ne les lisait. | commentaire ↔ code, code mort (résolu au Lot 10 : clés et commentaire supprimés, `app/api/admin.py:115-122`) | aucune |
| Le commentaire de la route de re-scrape héritée dit que le déverrouillage « n'est pas encore disponible » (`app/api/admin.py:234-235`) ; il existe (`app/templates/admin/_media_row.html:134-142`). Et `index.html` dit encore que le tiroir est vidé par un swap hors bande (`app/templates/admin/index.html:81-82`), démenti par le commentaire suivant. | commentaires périmés | Nettoyer. |
| Trois routes héritées (`GET /admin/movies`, `GET /admin/movies/stats`, `POST /admin/movies/{rk}/rescrape`) n'étaient appelées par aucun gabarit : les deux `GET` sont retirées au Lot 10 ; `POST /admin/movies/{rk}/rescrape` (`app/api/admin.py:222-246`) subsiste, testée, et double la route de CAP-BE-22. | code mort (partiellement résolu) | À trancher par Chakir : supprimer le POST restant, ou documenter un client externe. |
| La saisie d'ids passe encore par une URL `/admin/movies/…` alors qu'elle sert aussi aux séries. L'en-tête de page dit « PlexHub — Admin films » (`app/templates/admin/base.html:116-117`). | nommage ↔ périmètre | Renommer à l'occasion (sans urgence). |
| Un renvoi sans changement affiche « ✓ enregistré » (FR-BE-910) : l'opérateur ne distingue pas une sauvegarde réelle d'un no-op. | UX trompeuse | Afficher un état « inchangé ». |
| Les compteurs `/admin` comptent les items distincts ; `app/api/media.py:166` s'appuie sur `count_movies_missing_external`, qui compte les lignes-catégories (`app/services/media_service.py:627-639`). Les deux chiffres « sans IMDb » divergent. | incohérence entre surfaces | À trancher : aligner l'API sur `catalogue_stats`. |
| Pas de test HTTP pour le 422 d'id invalide, le 503 sans mot de passe admin, le repli de tri, les bornes de pagination, l'en-tête `refresh-stats` ou le renvoi de la même valeur. | test manquant | Ajouter aux tests admin. |

## Questions ouvertes

- **Ligne-catégorie affichée et triée** — `list_catalogue` sélectionne `Media` groupé par `(server_id, rating_key)` sans agrégat (`app/services/manual_scrape_service.py:1136-1143`) : SQLite renvoie une ligne-catégorie arbitraire, dont les ids, le verrou et `added_at` servent à l'affichage et au tri. Or `catalogue_stats` documente que ces lignes peuvent diverger (`app/services/manual_scrape_service.py:1181-1187`). Une ligne listée par un filtre peut-elle afficher des valeurs qui contredisent ce filtre ? — à vérifier par test.
- **`get_media_by_key` sans `ORDER BY`** (`app/services/media_service.py:619-625`) : même question pour la ligne renvoyée après une sauvegarde. — à vérifier par test.
- **Pagination** : le bouton inclut le formulaire, qui porte un `page=1` caché (`app/templates/admin/index.html:75`), et ajoute `hx-vals` avec la page voulue (`app/templates/admin/_media_table.html:35-36`). Quelle valeur gagne quand les deux sont envoyées ? — à vérifier par test (navigateur).
- **Console hors ligne** : sans accès aux CDN, l'UI est-elle encore utilisable ? — à vérifier par test.
- **Coût du `COUNT` distinct avec recherche `ILIKE`** sur le catalogue de production, à chaque frappe (délai de 300 ms) : aucun chiffre dans le dépôt (seul `catalogue_stats` est mesuré). — à vérifier par test.
- **Routes héritées** : un client externe (script, favori) dépendait-il de `GET /admin/movies*` (retirées au Lot 10) ? Le code ne le dit pas ; `POST /admin/movies/{rk}/ids` et `/rescrape` subsistent. — à trancher par Chakir.
