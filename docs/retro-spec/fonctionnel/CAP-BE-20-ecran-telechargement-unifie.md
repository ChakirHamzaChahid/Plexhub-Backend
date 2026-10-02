# CAP-BE-20 — Écran de téléchargement unifié

**Reconstituée le** : 2026-09-26 · recalée le 2026-10-02 (Lot 10) · **HEAD** : `aa3270f` · **Sources** : inventaire, code, tests, `docs/architecture/flux-cles.md` §5.10, board `docs/31-board.md` (UDL-01, UDL-02), PRD `docs/10-prd-media-download.md` et `docs/10-prd-plex-download.md` (ne couvrent pas cet écran), dépôt PlexHubTV (recherche négative)
**Périmètre** : UI HTMX `/admin/unified-downloads` (3 décorateurs, 2 fonctions, `app/api/admin_unified_downloads.py`) ; service `unified_download_catalog_service` ; templates `unified_downloads.html` et `_unified_downloads_list.html` ; réutilise sans les modifier la file et les sélecteurs de version de CAP-BE-18 (Xtream) et CAP-BE-19 (Plex). Aucune route `/api/*`, aucune table propre — voir `docs/retro-spec/_genere/inventaire.md`.

## Résumé

Troisième onglet de l'admin, « Téléchargements » : l'opérateur parcourt en un seul écran les catalogues Xtream (`media`) et Plex (`plex_media_item`), fusionnés par `unification_id`. Un titre présent des deux côtés donne une seule carte avec un badge par origine. L'écran ne fait que parcourir : le choix de version et la mise en file sont délégués tels quels aux fragments `/versions` des deux onglets d'origine, qui postent vers leurs propres endpoints et ciblent la file commune `#downloads-queue`. Aucune logique d'enqueue, de worker ou de file n'existe ici. Persona : opérateur (admin). L'app TV ne consomme pas cette capacité.

## User stories observées

- **US-BE-20.1** (P1) — En tant qu'opérateur, je vois un titre présent chez Plex et chez Xtream comme une seule carte afin de ne pas ouvrir deux onglets.
  - Parcours : `/admin/unified-downloads` → cartes avec badges d'origine · Maestro : aucun
- **US-BE-20.2** (P1) — En tant qu'opérateur, je filtre par type, titre et genre sur les deux catalogues à la fois.
  - Parcours : formulaire de filtres → `GET /admin/unified-downloads/list` · Maestro : aucun
- **US-BE-20.3** (P1) — En tant qu'opérateur, j'ouvre une carte et je choisis l'origine et la version à télécharger.
  - Parcours : carte (`<details>`) → sélecteur `/versions` de l'origine → mise en file par l'onglet d'origine · Maestro : aucun
- **US-BE-20.4** (P1) — En tant qu'opérateur, je suis la file commune Plex + Xtream sans recharger la page.
  - Parcours : panneau `#downloads-queue` rafraîchi toutes les 2 s · Maestro : aucun
- **US-BE-20.5** (P2) — En tant qu'opérateur, j'annule ou je relance un job et je purge l'historique terminé depuis le même écran.
  - Parcours : boutons de la file → `POST /admin/downloads/{id}/cancel|retry`, `clear-finished` · Maestro : aucun
- **US-BE-20.6** (P2) — En tant qu'opérateur, je suis prévenu quand la liste est tronquée afin d'affiner ma recherche.
  - Parcours : bandeau ambre + « + » sur le total · Maestro : aucun

## Exigences fonctionnelles

**Accès et pages**

- **FR-BE-1044** — `GET /admin/unified-downloads` (avec ou sans barre finale) rend la page complète : filtres, catalogue et file. — *implémenté* — preuve `app/api/admin_unified_downloads.py:97-117` · test `tests/test_admin_unified_downloads.py:91`
- **FR-BE-1045** — `GET /admin/unified-downloads/list` rend le seul fragment de catalogue, sans la file. — *implémenté* — preuve `app/api/admin_unified_downloads.py:120-140` · test `tests/test_admin_unified_downloads.py:98`
- **FR-BE-1046** — L'accès est protégé par la Basic Auth posée au montage : 401 avec `WWW-Authenticate: Basic` sans identifiants, 503 si `ADMIN_PASSWORD` est vide. — *implémenté* — preuve `app/main.py:695`, `app/api/deps.py:113-145` · test `tests/test_admin_unified_downloads.py:75` et `:81` (401 seulement ; mauvais mot de passe et 503 non testés sur cet onglet)
- **FR-BE-1064** — Un lien « Téléchargements » figure dans la navigation de l'admin. — *implémenté* — preuve `app/templates/admin/base.html:123` · test : aucun

**Fusion des catalogues**

- **FR-BE-1047** — Les catalogues Xtream et Plex sont fusionnés par `unification_id` : un titre commun donne une carte unique portant les deux origines. Films et séries. — *implémenté* — preuve `app/services/unified_download_catalog_service.py:59-87`, `app/services/unified_download_catalog_service.py:109-141` · test `tests/test_unified_download_catalog.py:59` et `:108`, `tests/test_admin_unified_downloads.py:98` et `:166`
- **FR-BE-1048** — Sans identifiant partagé (clé `title_…` côté Xtream, `plexsrc://…` côté Plex), il n'y a jamais de fusion : le même titre donne deux cartes. — *implémenté* (par construction des clés) — preuve `app/services/unified_download_catalog_service.py:12-17` · test `tests/test_unified_download_catalog.py:94`
- **FR-BE-1049** — Chaque carte affiche un badge par origine présente (Plex, Xtream) et « N source(s) », somme des versions des deux origines. — *implémenté* — preuve `app/templates/admin/_unified_downloads_list.html:26-34`, `app/services/unified_download_catalog_service.py:128`, `app/services/unified_download_catalog_service.py:140` · test `tests/test_admin_unified_downloads.py:118` (le singulier « 1 source » n'est pas testé)
- **FR-BE-1055** — La liste fusionnée est triée par titre (insensible à la casse) puis par `unification_id`. — *partiel* (manque : le tri ne porte que sur la fenêtre lue, `cap` groupes par source ; sous troncature, un titre hors fenêtre n'apparaît à aucune position) — preuve `app/services/unified_download_catalog_service.py:109-114`, `app/services/unified_download_catalog_service.py:146`, `app/services/unified_download_catalog_service.py:149-150` · test : aucun

**Filtres, pagination, troncature**

- **FR-BE-1050** — Le filtre `type` accepte `movie` et `show` ; toute autre valeur est ramenée à `movie` sans erreur. — *implémenté* — preuve `app/api/admin_unified_downloads.py:108`, `app/api/admin_unified_downloads.py:130` · test `tests/test_admin_unified_downloads.py:166` (`show`) ; valeur inconnue : aucun
- **FR-BE-1051** — La recherche par sous-chaîne de titre s'applique aux deux catalogues. — *implémenté* — preuve `app/services/unified_download_catalog_service.py:109-114`, `app/services/plex_catalog_service.py:203-206` · test : aucun
- **FR-BE-1052** — Le filtre `genre` (sous-chaîne) s'applique aux deux catalogues ; un genre Plex NULL (données d'avant la migration 021) ne correspond jamais. — *implémenté* — preuve `app/services/plex_catalog_service.py:207-210` · test `tests/test_admin_unified_downloads.py:142`, `tests/test_unified_download_catalog.py:126`
- **FR-BE-1053** — La pagination est en mémoire après fusion : `page` ≥ 1 (défaut 1), `page_size` de 6 à 200 (défaut 24), boutons Précédent et Suivant ; un changement de filtre repart de la page 1 (champ caché `page=1`). — *implémenté* — preuve `app/api/admin_unified_downloads.py:72`, `app/api/admin_unified_downloads.py:104-105`, `app/services/unified_download_catalog_service.py:150`, `app/templates/admin/_unified_downloads_list.html:69-87`, `app/templates/admin/unified_downloads.html:46-47` · test : aucun
- **FR-BE-1054** — Chaque source est lue sur `UNIFIED_DOWNLOAD_MERGE_CAP` groupes au plus (défaut 5000) ; au-delà, `truncated` déclenche un bandeau ambre et un « + » sur le total. — *implémenté* — preuve `app/config.py:268`, `app/services/unified_download_catalog_service.py:43`, `app/services/unified_download_catalog_service.py:98`, `app/services/unified_download_catalog_service.py:149`, `app/templates/admin/_unified_downloads_list.html:3-7`, `app/templates/admin/_unified_downloads_list.html:70` · test `tests/test_unified_download_catalog.py:148` (service seulement ; le bandeau HTML n'est pas testé)
- **FR-BE-1056** — Le total du catalogue est rendu via `catalogue_total`, distinct du `total` de la file, pour que le contexte de la file ne l'écrase pas au premier rendu pleine page (UDL-01). — *implémenté* — preuve `app/api/admin_unified_downloads.py:80-91`, `app/api/admin_unified_downloads.py:116`, `app/templates/admin/_unified_downloads_list.html:1`, `app/templates/admin/_unified_downloads_list.html:70` · test : aucun

**Sélection et mise en file (déléguées)**

- **FR-BE-1057** — Chaque carte est un `<details>` portant, pour chaque origine présente seulement, un chargeur HTMX statique qui tire le `/versions` de cette origine à la première ouverture (`toggle once from:closest details`). — *implémenté* — preuve `app/templates/admin/_unified_downloads_list.html:21`, `app/templates/admin/_unified_downloads_list.html:36-58` · test `tests/test_admin_unified_downloads.py:195` (deux chargeurs) et `:219` (chargeur absent pour une origine absente)
- **FR-BE-1058** — Le routeur unifié n'a aucun POST : la mise en file part des formulaires des onglets d'origine, qui ciblent la file partagée. — *implémenté* (par délégation) — preuve `app/api/admin_unified_downloads.py:97-140`, `app/api/admin_downloads.py:379`, `app/api/admin_plex_downloads.py:360` · test : aucun test du clic depuis l'écran unifié
- **FR-BE-1059** — Le worker unique route un job selon le préfixe de `server_id` : Plex (`is_plex_server_id`, URL re-dérivée, repli direct-play) ou Xtream ; un job Plex dont la source n'est plus synchronisée est marqué `failed`. — *implémenté* — preuve `app/workers/download_worker.py:585-594` · test : voir CAP-BE-19

**File partagée**

- **FR-BE-1060** — La file commune est affichée sous `#downloads-queue` et rafraîchie toutes les 2 s via `/admin/downloads/queue` (200 jobs au plus, du plus récent au plus ancien). — *implémenté* — preuve `app/templates/admin/unified_downloads.html:60`, `app/api/admin_downloads.py:440-444`, `app/services/download_service.py:1238-1253` · test `tests/test_download_security.py:250` (401 de `/queue` ; le rendu sur l'écran unifié n'est pas testé)
- **FR-BE-1061** — Depuis l'écran, l'opérateur peut annuler un job `queued` ou `running`, relancer un job `failed` ou `canceled`, et purger en bloc l'historique terminé ; il n'existe pas de suppression unitaire. — *implémenté* (via les routes `/admin/downloads/*`) — preuve `app/templates/admin/_downloads_queue.html:6-10`, `app/templates/admin/_downloads_queue.html:63-70`, `app/api/admin_downloads.py:447-474`, `app/services/download_service.py:1260-1324` · test `tests/test_download_service.py:432`, `:440`, `:476` (service seulement ; aucun test HTTP)

**Sécurité**

- **FR-BE-1062** — Aucun secret n'est exposé : ni `access_token` ni `base_uri` Plex, ni URL Xtream ; la lecture Plex est celle, sans secret, de `plex_catalog_service`. — *implémenté* — preuve `app/api/admin_unified_downloads.py:28-32`, `app/services/unified_download_catalog_service.py:19-23`, `app/services/unified_download_catalog_service.py:132` · test `tests/test_admin_unified_downloads.py:239` (seul un serveur Plex est semé ; aucun job Xtream)
- **FR-BE-1063** — Le client ne fournit aucun chemin de fichier : le routeur n'accepte que `type`, `search`, `genre`, `page` et `page_size`. — *implémenté* — preuve `app/api/admin_unified_downloads.py:99-107` · test : aucun
- **FR-BE-1065** — Les GET de l'onglet ne sont jamais interceptés par le contrôle CSRF `Sec-Fetch-Site`, même avec `cross-site` ; les POST de la file, eux, y restent soumis. — *implémenté* — preuve `app/api/csrf.py:85-114` · test `tests/test_admin_csrf.py:140` (GET seulement ; les POST `/admin/downloads/*` ne sont pas dans les tests CSRF)

## Règles métier

- **RG-BE-20.1** — Les clés id-based `imdb://tt…` et `tmdb://…` sont identiques côté Xtream et côté Plex : la fusion est une égalité de chaîne. — preuve `app/services/unified_download_catalog_service.py:5-10`, `app/services/plex_sync_service.py:102-106`
- **RG-BE-20.2** — Jamais de fusion sur titre + année entre sources : Plex n'a volontairement pas de branche de repli titre + année. — preuve `app/services/plex_sync_service.py:94-100`
- **RG-BE-20.3** — Xtream est fusionné en premier et son titre et son année font foi ; l'année Plex ne comble qu'un manque. — preuve `app/services/unified_download_catalog_service.py:84-87`, `app/services/unified_download_catalog_service.py:118`
- **RG-BE-20.4** — `source_count` est le nombre de versions cumulées des deux origines, pas le nombre d'origines ; `origins` est trié alphabétiquement. — preuve `app/services/unified_download_catalog_service.py:128`, `app/services/unified_download_catalog_service.py:140`, `app/services/unified_download_catalog_service.py:144-145`
- **RG-BE-20.5** — `total` est le nombre de groupes fusionnés dans la fenêtre lue, pas le total réel de la base ; `truncated` vaut vrai dès qu'une source dépasse `cap`. — preuve `app/services/unified_download_catalog_service.py:148-149`
- **RG-BE-20.6** — Les chargeurs de version se déclenchent sur l'événement `toggle` du `<details>`, jamais sur un auto-déclencheur dans du contenu échangé ; le déclencheur des filtres est `change, keyup delay:300ms, submit`, sans `from:` contenant un espace ni `changed` sur un `<form>` (pièges htmx, UDL-02). — preuve `app/templates/admin/_unified_downloads_list.html:12-20`, `app/templates/admin/_unified_downloads_list.html:41`, `app/templates/admin/_unified_downloads_list.html:52`, `app/templates/admin/unified_downloads.html:22`
- **RG-BE-20.7** — Le catalogue Xtream lu par cet écran passe par `media_service.get_unified_list`, donc hérite du masquage des comptes en panne (CAP-BE-08). — preuve `app/services/media_service.py:316-320`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| Aucun résultat | Carte « Aucun titre trouvé. », navigation « 0 résultat(s) · page 1 / 1 » | `app/templates/admin/_unified_downloads_list.html:62-66`, `app/templates/admin/_unified_downloads_list.html:1`, `app/templates/admin/_unified_downloads_list.html:70` |
| `type` inconnu | Ramené à `movie`, sans erreur | `app/api/admin_unified_downloads.py:108`, `app/api/admin_unified_downloads.py:130` |
| `page` < 1, `page_size` < 6 ou > 200 | 422 FastAPI ; `base.html` fait échanger les 422 dans la cible, donc du JSON pourrait s'afficher dans la liste (l'UI ne peut pas produire ces valeurs) | `app/api/admin_unified_downloads.py:104-105`, `app/templates/admin/base.html:106-107` |
| `page` au-delà de la dernière page | Liste vide, libellé « page N / M » non borné | `app/services/unified_download_catalog_service.py:150`, `app/templates/admin/_unified_downloads_list.html:1,70` |
| Fenêtre dépassée sur une source | Bandeau ambre et « + » sur le total | `app/templates/admin/_unified_downloads_list.html:3-7,70` |
| Job inexistant (annuler, relancer) | Le service renvoie `None`, la file est réaffichée sans erreur | `app/services/download_service.py:1265-1267`, `app/services/download_service.py:1290-1292`, `app/api/admin_downloads.py:447-464` |
| Job déjà terminal ou déjà en file | Sans effet (mise à jour conditionnelle) | `app/services/download_service.py:1272`, `app/services/download_service.py:1297` |
| Job Plex dont la source a disparu du catalogue | Marqué `failed` : « source Plex introuvable ou non synchronisée » | `app/workers/download_worker.py:586-589` |
| POST de la file avec `Sec-Fetch-Site: cross-site` | 403 par le middleware CSRF | `app/api/csrf.py:98-113` |
| Sans identifiants admin | 401 avec `WWW-Authenticate: Basic` ; 503 si `ADMIN_PASSWORD` vide | `app/api/deps.py:123-134` |

## Dépendances

- Autres capacités : `CAP-BE-18` (file `download_job`, worker, `_queue_context`, `/admin/downloads/*`, `media_service.get_unified_list`), `CAP-BE-19` (`plex_catalog_service.list_unified`, `/admin/plex-downloads/…/versions`, `plex_download_service`), `CAP-BE-02` (Basic Auth admin, CSRF), `CAP-BE-08` (masquage de panne, RG-BE-20.7).
- Autre dépôt : aucun appelant. Recherche négative dans `tv:` : zéro occurrence de `unified-downloads`, `plex-downloads` ni `admin/downloads` dans le code ; seule mention, `tv:docs/retro-spec/fonctionnel/CAP-TV-14-telechargements-hors-ligne.md:162`, un texte de fiche (aucune dépendance fonctionnelle de la TV avec le module de téléchargement).
- Données : lit `media`, `plex_media_item` (colonne `genres`, migration 021) et `download_job` ; n'écrit rien lui-même. Config : `UNIFIED_DOWNLOAD_MERGE_CAP`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `DOWNLOAD_DIR` (voir `docs/retro-spec/technique/db/`).

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| `docs/architecture/flux-cles.md` §5.10 cite plex_sync_service.py:92 pour l'égalité des clés ; la logique est aux lignes 102-106 (fonction dès la ligne 88) | référence périmée | corriger la référence au prochain `/sync-context` |
| `logger` et `import logging` jamais utilisés dans `app/api/admin_unified_downloads.py:36`, `app/api/admin_unified_downloads.py:49` ; clés `total` et `offset` du contexte (`:87-88`) non lues par les templates de liste | code mort mineur | à trancher par Chakir (nettoyage) |
| `UDL-01` subsiste sur `app/api/admin_downloads.py` et `app/api/admin_plex_downloads.py` : la clé `total` de la file y écrase encore le total du catalogue au premier rendu | dette connue (`docs/31-board.md:110`) | corrigée seulement sur l'onglet unifié ; reprendre sur les deux autres si l'écrasement gêne |
| Aucun test HTTP de la pagination, de la recherche, du tri, de l'état vide, du bandeau de troncature, de `catalogue_total`, du 503, ni des POST de la file depuis cet écran ; le test de non-fuite ne sème pas de job Xtream | test manquant | ajouter au prochain lot touchant l'écran |
| La file n'affiche pas l'origine d'un job : `server_id` est dans `_job_view` (`app/api/admin_downloads.py:98`) mais non rendu par `app/templates/admin/_downloads_queue.html` ; pas de filtre par origine ; pas de suppression unitaire | fonction non annoncée, constat | à trancher par Chakir (besoin réel ?) |

## Questions ouvertes

- Un `unification_id` qui disparaît entre l'affichage de la carte et son ouverture donne un 404 sur `/versions` (`app/api/admin_downloads.py:235-237`, `app/api/admin_plex_downloads.py:248-250`) ; le chargeur `once` reste-t-il figé sur « Chargement… » puisque htmx 1.x n'échange pas les 4xx ? — à vérifier par test.
- Sous troncature, quelles lignes forment la fenêtre de chaque source (récence ou autre) et la première page correspond-elle aux premiers titres du tri global ? Le tri propre de `get_unified_list` n'a pas été relu ici — à vérifier par test.
- Le masquage des comptes en panne (RG-BE-20.7) est-il voulu pour la navigation de téléchargement ? Le flux §5.13 ne le précise pas pour les téléchargements — à trancher par Chakir.
- Les `unification_id` (`imdb://tt…`, `plexsrc://cid/rk`) sont insérés tels quels dans les `hx-get` ; un identifiant avec caractères spéciaux passe-t-il le convertisseur `:path` ? — à vérifier par test.

## Écart fiche ↔ PRD

Aucun PRD ne couvre cette capacité, la carte le note (`PRD : —`).

| Point | PRD | Fiche |
|---|---|---|
| « Liste unifiée » | `docs/10-prd-media-download.md` (F-001) désigne la déduplication Xtream seule de `media_service.get_unified_list`, pas la fusion entre sources | FR-BE-1047 : fusion Xtream + Plex |
| Onglet unique | `docs/10-prd-plex-download.md` (PC-007) prévoit un panneau de file réutilisé et l'onglet Xtream inchangé, sans troisième onglet | FR-BE-1044 à 1061 : troisième onglet, file partagée |
| Sources de spec de fait | — | `docs/architecture/flux-cles.md` §5.10 et `docs/31-board.md` (UDL-01, UDL-02) |
