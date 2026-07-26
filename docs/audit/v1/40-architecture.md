# Audit v1 — Phase 4 : Architecture

**Périmètre** : logique métier vs routers, god-files (dette assumée CR-A03/A05), duplications API⇆générateur, conventions de montage des routers (CR-A04), frontières du module `app/dav/`, partage de la file `download_job`, dualité des formules `display_rating`, couplages/imports différés.
**HEAD audité** : `9da9d46` (develop, v1.7.1). Baselines de comparaison : `1879f83` (cartographie pré-remédiation) et `3833349` (fin `/fix-cleanroom`), LOC mesurées via `git show | wc -l`.
Chaque finding indique **[MESURÉ]** (chiffres/greps exécutés) ou **[DÉDUIT]** (jugement par lecture).

---

## Findings

### AUDIT-P4-001 — CR-A04 toujours ouvert : 3 conventions de montage coexistent, le garde-fou structurel promis n'existe pas — une route `/api/*` peut être publiée non authentifiée par accident — **S2** [MESURÉ]

**État à HEAD.** Les 3 patterns cohabitent, désormais **documentés** dans un bloc unique (`main.py:560-657`) :
- **Pattern A** — garde au mount (`dependencies=_guard`) : `accounts/categories/live/media/stream/sync/plex` (`main.py:573-579`) ;
- **Pattern B** — public au niveau router : `health` (`:570`), `tv_auth` (`:582`, `/approve` gardé en interne) ;
- **Pattern C** — bare mount, self-prefixé + garde module-level : `ai`, `api_keys`, `downloads`, `plex_downloads`, `enrichment` (`:643-647`), `dav` (`:657`, hors `/api`).

**Le point critique** : le commentaire `main.py:561-564` acte lui-même le follow-up « *a startup assertion walking `app.routes` to assert every `/api/*` route carries an auth dependency — see CR-A04* » comme **« Tracked follow-up (not done here) »**. Vérifié par grep : **aucun** route-walk n'existe ni dans `app/` ni dans `tests/` (seul hit = ce commentaire). Combiné au fait qu'aucun test de rejet 401 ne couvre `verify_backend_secret` (CR-T02, hors de mon périmètre mais aggravant), le scénario d'échec est concret : un développeur ajoute un router Pattern-C (la convention la plus récente, utilisée par les 5 derniers routers JSON) en **oubliant** le `dependencies=[Depends(...)]` module-level → la route part en prod publique, sans échec de test ni d'assertion au boot. La surface a d'ailleurs grandi : 5 routers Pattern C + 1 DAV à HEAD contre 2 au moment de l'audit clean-room.

**Recommandation.** Implémenter l'assertion de boot promise (~20 lignes : itérer `app.routes`, exiger pour tout chemin `/api/*` hors allow-list explicite (`/api/health`, 3 routes tv_auth) la présence d'une dépendance d'auth connue ; `RuntimeError` sinon). C'est le garde-fou le moins cher de tout l'audit rapporté au risque couvert. **Effort : S.**

---

### AUDIT-P4-002 — God-files (dette assumée CR-A03/A05) : la dette a EMPIRÉ depuis la baseline, et deux nouveaux candidats sont apparus — **S3/dette** [MESURÉ]

Mesures `wc -l` à HEAD vs `1879f83` :

| Fichier | `1879f83` | HEAD `9da9d46` | Δ | Note |
|---|---|---|---|---|
| `app/workers/sync_worker.py` | 1 390 | **1 618** | **+16 %** | `sync_account` = **~504 lignes** (`:1090-1594`, vs 446 à l'audit CR) — la croissance date de la remédiation elle-même (déjà 1 618 à `3833349`) |
| `app/main.py` | 442 | **661** | **+50 %** | lifespan a absorbé download worker, cron Plex, DAV, backup… (CR-A05 « gardé » mais en croissance active) |
| `app/api/ai.py` | 1 228 | 1 225 | stable | plus gros handlers : `/subtitles/translate` ~128 l., `/search` ~123 l., `/blurb` ~111 l. |
| `app/services/nfo_import_service.py` | 888 | 888 | stable | — |
| `app/services/media_service.py` | 342 | **691** | **+102 %** | fixes CR-P01/P04/F05 empilés dans le même module |
| `app/services/download_service.py` | — | **1 352** | **nouveau** | enqueue + confinement chemin + SSRF + transfert + préflight disque + sanitization NTFS dans un seul module |

**Jugement.** La décision « GARDÉS (effort dédié) » de CLAUDE.md était défendable au moment où elle a été prise ; les chiffres montrent que **la trajectoire est à la hausse, pas au plateau** : `sync_worker` et `main.py` sont les deux fichiers où chaque feature récente a ajouté du code, et `download_service.py` (1 352 LOC, 4 responsabilités distinctes) est né **après** la décision et n'est couvert par aucun ticket de dette. Coût de non-action concret : `sync_account` à 504 lignes est le plus gros bloc non testé du repo (CR-T03), chaque fix sync (F01/F02/F11) a dû s'y insérer par chirurgie ; `main.py` mélange logging, lifespan, élection master, 4 crons et le montage — toute feature « scheduled » y retouche.
**Recommandation.** Ne pas re-litiguer CR-A03/A05 en bloc, mais (a) créer le ticket manquant pour `download_service.py` (découpe naturelle : `path/confinement` vs `transfer` vs `queue`) ; (b) fixer un **cliquet** : aucun des 4 fichiers ne dépasse son LOC actuel (gate lint simple), les prochains ajouts sync/lifespan vont dans des modules dédiés. **Effort : S (cliquet) / L (découpes).**

---

### AUDIT-P4-003 — CR-A01/A02 : majoritairement résolus (vérifié), avec deux résidus localisés dans `live.py` et `stream.py` — **S3** [MESURÉ]

**Vérifié résolu** :
- `account_service`, `live_service`, `plex_generation_service`, `xtream_credentials` existent tous (`ls app/services/`) ; `accounts.py` (108 LOC) délègue bien (`accounts.py:34,56,72,87`), `categories.py` (122 LOC) importe `category_service`.
- L'orchestration Plex-gen est **unifiée** dans `plex_generation_service` (module docstring `plex_generation_service.py:1-35` liste les 4 call-sites) ; `sync.py:99` porte le commentaire confirmant la suppression du reach-in `from app.main import _auto_generate_plex_library`. Grep : **aucun** `import app.main` hors de `main.py` lui-même.

**Résidus confirmés** :
1. `live.py::list_channels` construit encore son SQL dans le router (`select`, `ilike`, narrow-count, tri, pagination — `live.py:39-61+`), alors que `live_service` n'expose que `ingest_short_epg`/`_try_base64_decode`. Le service porte le nom mais pas la logique de liste.
2. `stream.py` re-parse `server_id` à la main (`server_id.startswith("xtream_")` puis `server_id[7:]`, `stream.py:20-23`) au lieu de `utils.server_id.parse_server_id` — duplication du format `xtream_<id>` qui vit partout ailleurs dans l'util dédié (et qui a justement gagné un frère `plex_<id>` : toute évolution du namespace doit maintenant penser à ce site orphelin). SQL inline accessoire (36 LOC au total, bénin).

**Effort : S** (les deux résidus tiennent en une PR).

---

### AUDIT-P4-004 — Duplications API⇆générateur (CR-A07) et reach-ins (CR-A06) : résolus — vérifié — **clos** [MESURÉ]

- `build_versions` a **une seule** implémentation (`aggregation_service.py:82`) consommée par les trois frontières : API (`api/media.py:85`, wrappé `_build_versions` :70 qui n'est plus qu'un adaptateur de labels), générateur (`source.py:89`), UI download (`admin_downloads.py:241`). Les `_build_versions` locaux restants sont des adaptateurs (filtrage broken + résolution d'URL côté générateur), pas des duplications de la logique tri/label/dédup.
- Reach-ins privés : grep `_serialize_vec` / `_movie_folder` → **0 occurrence** cross-module ; le générateur expose désormais des alias publics `resolve_movie_names`/`resolve_series_names` consommés par `dav/tree_builder.py` (import propre, pas de underscore).

Rien à signaler — je le consigne pour que la lignée CR-A06/A07 puisse être fermée sur preuve.

---

### AUDIT-P4-005 — Deux formules `display_rating` : le « by design » est plus fragile qu'annoncé — le recompute de fin d'enrichissement ÉCRASE la formule NFO sur toute ligne portant une note IMDb/TMDB — **S3** [DÉDUIT]

Les deux écrivains coexistent comme documenté : `nfo_import_service` écrit `calculate_display_rating(scraped, audience, rating)` (COALESCE best-pick, `nfo_import_service.py:531-541`) ; l'enrichissement écrit `blend(imdb, tmdb)` via `rating_blend` (« single source of truth », `rating_blend.py:1-14`).

**Le point que la doc sous-estime** : le « by design » de CLAUDE.md affirme que « les lignes NFO-only sans note IMDb/TMDB gardent l'ancienne formule ». C'est vrai — mais `nfo_import_service` **peuple lui-même** `imdb_rating`/`tmdb_rating` (colonnes M014, via `_FIELD_MAP`). Or `recompute_display_rating_stmt()` tourne **en fin de chaque run d'enrichissement** (défensif, §5.2) et re-blende **toute** ligne movie/show ayant ≥ 1 de ces notes (`rating_blend.py:64-79`). Conséquence : pour une ligne enrichie par NFO avec `imdb_rating` ET `tmdb_rating`, la valeur COALESCE (best-pick priorité IMDb, ex. 8.8) écrite à l'import est **remplacée par la moyenne** (ex. (8.8+8.1)/2 = 8.45) au run d'enrichissement suivant — puis re-remplacée par la COALESCE si l'opérateur réimporte des NFO. Deux écrivains, dernière-écriture-gagnante, valeur visible par l'app qui **oscille** entre deux définitions selon l'ordre des jobs. Pas de corruption, pas d'incident — mais c'est exactement le profil d'une future « note qui change toute seule » difficile à diagnostiquer, et le module qui se déclare « single source of truth » ne l'est factuellement pas.

**Recommandation.** Faire converger `nfo_import_service` vers `rating_blend.blend_rating` (une seule formule, l'import NFO garde son rôle de *fournisseur de colonnes durables*, plus de recompute concurrent), ou à défaut documenter l'oscillation dans CLAUDE.md §5.2 comme comportement connu. **Effort : S.**

---

### AUDIT-P4-006 — File `download_job` partagée Xtream/Plex routée par `is_plex_server_id` : design sain — **clos (avec une réserve mineure)** [MESURÉ]

Vérifié : le discriminant est consommé en exactement **2 points** du worker (`download_worker.py:224` pour le sidecar NFO, `:452-467` pour la résolution d'URL), tout le reste (états, retry, confinement F-007, drain, métriques d'orphelins) est réellement mutualisé — pas de duplication de worker ni de table, et les préfixes `xtream_`/`plex_` sont fabriqués/parsés uniquement via `utils/server_id`. C'est le bon découpage : la variance (comment dériver l'URL, quel catalogue lire) est isolée dans `plex_download_service`, l'invariant (transférer des octets sous confinement) est unique.
**Réserve** : le namespace est stringly-typed — la garantie « jamais de collision » repose sur la discipline des builders. `stream.py` (cf. P4-003.2) est la preuve qu'un site peut parser le préfixe à la main. Le jour où une 3ᵉ source arrive, centraliser un `enum`/match exhaustif dans `utils/server_id` évitera le « if/else oublié ». **Effort : S, non urgent.**

---

### AUDIT-P4-007 — Frontières `app/dav/` : isolation correcte, mais deux cycles de modules ne tiennent que par imports différés — **S3** [MESURÉ]

Cartographie des dépendances (grep imports) :
- **Sortantes** (saines, unidirectionnelles) : `dav → config`, `dav → plex_generator` (source/generator-alias/naming/models), `dav → services.download_service` (réutilisation de `assert_public_redirect_host` — bon réemploi de la garde SSRF plutôt qu'une copie), `dav → services.stream_service`.
- **Entrantes** : `api/dav.py` (router), `main.py` (mount + `close_client` au lifespan), et **`services/plex_generation_service.py:129`** qui importe `app.dav.vfs` **en différé, gaté `DAV_ENABLED`**, pour invalider `dav_tree_cache` post-génération.

Deux cycles potentiels sont cassés uniquement au call-time :
1. `vfs ↔ tree_builder` — vrai cycle structurel, documenté et assumé (`vfs.py:126-131` : `tree_builder` importe `DavEntry/DavTree` depuis `vfs`, `vfs.get()` importe `build_dav_tree` en différé). Fonctionne, mais le cycle disparaîtrait en déplaçant les dataclasses `DavEntry/DavTree` dans un `dav/model.py` sans dépendance.
2. `services → dav` (invalidation) vs `dav → services` (SSRF) — cycle **au niveau paquet**, invisible tant que l'import reste différé. Un futur import module-scope de `plex_generation_service` depuis le paquet dav (ou l'inverse) le matérialiserait en `ImportError` circulaire au boot. L'inversion propre serait un hook d'invalidation enregistré par `dav` (callback/registre côté `plex_generation_service`), le service ne connaissant plus `dav`.

Pour le reste, l'isolation revendiquée est réelle : `plex_media_item` n'est jamais lu par `DatabaseSource`/le générateur (vérifié par les imports), et `dav` ne touche ni `media_service` ni les routers. Les imports différés du reste du repo (`sync.py`, `accounts.py`, `ai.py`, `cli.py` — listés par grep) sont du lazy-loading de workers lourds, pas des ruptures de cycle — pattern cohérent, pas de finding. **Effort : S (dataclasses `dav/model.py`) / M (inversion du hook).**

---

### AUDIT-P4-008 — Dette structurelle de fond : la PK 4-tuple de `media` (`rating_key, server_id, filter, sort_order`) + la mécanique `page_offset` irriguent la complexité de tout l'étage supérieur — **dette** [DÉDUIT]

Constat transversal plutôt que finding local : une même vidéo physique peut exister en N lignes (une par catégorie/`filter`), ce qui force — preuves dans le code à HEAD — (a) l'éviction par slot Phase-1 de l'upsert et son arithmétique `page_offset` (`sync_worker.py:653-692`, racine de CR-F02, toujours ouverte) ; (b) la dédup des pointeurs membres du snapshot (`unified_group_service.py:64-87`, long commentaire d'excuse) ; (c) la ré-application du prédicat catégories à l'hydratation snapshot pour ne pas ré-inflater la jumelle non autorisée (`media_service.py:391-404`) ; (d) un curseur keyset à 5 colonnes (`media_service.py:93-98`). Quatre sites, quatre commentaires-fleuves expliquant la même bizarrerie de modèle. Aucune action court-terme raisonnable (migration lourde), mais toute refonte future du sync devrait viser `media_item` (1 ligne physique) + `media_category` (N:M) — à consigner comme direction, pour arrêter d'empiler des contournements. **Effort : L (hors sprint, direction d'archi).**

---

## Tableau récapitulatif

| ID | Sévérité | Titre | Preuve principale | Statut |
|---|---|---|---|---|
| AUDIT-P4-001 | **S2** | CR-A04 : 3 conventions de montage, assertion de boot promise **non implémentée** — route non-auth possible par accident | `main.py:560-657` (aveu `:561-564`) ; grep route-walk = 0 | MESURÉ |
| AUDIT-P4-002 | S3/dette | God-files : `sync_worker` +16 % (1 618), `main.py` +50 % (661), `media_service` +102 % (691), **nouveau** `download_service` 1 352 LOC | `wc -l` vs `git show 1879f83` | MESURÉ |
| AUDIT-P4-003 | S3 | Résidus CR-A01 : SQL inline `live.py:39-61` ; parse manuel `server_id[7:]` `stream.py:20-23` | greps + lecture | MESURÉ |
| AUDIT-P4-004 | clos | CR-A06/A07 vérifiés résolus (`build_versions` unique, 0 reach-in privé) | `aggregation_service.py:82` + greps | MESURÉ |
| AUDIT-P4-005 | S3 | `display_rating` : le recompute blend écrase la formule NFO sur toute ligne à note IMDb/TMDB → oscillation deux-écrivains | `nfo_import_service.py:531-541` vs `rating_blend.py:64-79` | DÉDUIT |
| AUDIT-P4-006 | clos (réserve S) | File `download_job` partagée : design sain, 2 points de dispatch ; namespace stringly-typed à durcir | `download_worker.py:224,452-467` | MESURÉ |
| AUDIT-P4-007 | S3 | `app/dav/` : 2 cycles tenus par imports différés (`vfs↔tree_builder` ; `services⇄dav` via invalidation) | `vfs.py:126-131`, `plex_generation_service.py:119-131` | MESURÉ |
| AUDIT-P4-008 | dette | PK 4-tuple `media` + `page_offset` : racine commune de 4 contournements documentés dans le code | `sync_worker.py:653-692`, `unified_group_service.py:64-87`, `media_service.py:391-404,93-98` | DÉDUIT |
