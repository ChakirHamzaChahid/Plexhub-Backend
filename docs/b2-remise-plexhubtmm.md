# Lot B2 — note de remise à plexhubTMM

**Date** : 2026-10-18 · **De** : PlexHub Backend, branche `develop` · **Pour** : le dépôt plexhubTMM (non modifié par
cette note ; c'est à plexhubTMM de reporter ce qui suit dans `docs/16-contrat-b2-backend.md` et dans son code, par la
procédure de son ADR 0012).
**Contrat d'entrée** : `plexhubTMM/docs/16-contrat-b2-backend.md` (validé par Chakir le 2026-10-18). Le backend a
**livré ce contrat avec 33 écarts**, tous listés plus bas ; aucun ne change le chemin ni le sens d'une des 14 routes,
mais plusieurs changent un comportement que le client doit connaître. **Rien n'est déployé** : tant que Chakir n'a pas
décidé d'un lot de déploiement séparé, l'API n'existe que sur `develop` (version `1.13.0`).

## 1. Ce qui est livré

L'API `/api/metadata/v1` : 14 routes JSON pour lire, simuler, écrire, journaliser et annuler les métadonnées de la
table `media`, sans jamais écrire dans `plexhub.db` autrement que par le code du backend.

| Méthode et chemin | Rôle | Scope |
|---|---|---|
| `GET /lookup` | éléments portant exactement un id (`imdbId`/`tmdbId`/`tvdbId`) ou un `unificationId` | `media.read` |
| `GET /items` | éléments à compléter (`missingIds`), pagination par curseur | `media.read` |
| `GET /items/{serverId}/{ratingKey}` | vue complète : champs, dérivés, verrou, révision (`ETag`), variantes | `media.read` |
| `GET /items/{serverId}/{ratingKey}/episodes` | épisodes d'une série | `media.read` |
| `POST /items/{serverId}/{ratingKey}/preview` | simulation d'une écriture | `media.read` |
| `PATCH /items/{serverId}/{ratingKey}` | écriture partielle atomique sur toutes les variantes | **`media.write`** |
| `GET /items/{serverId}/{ratingKey}/history` | historique du plus récent au plus ancien | `media.read` |
| `GET /edits/{editId}` | une édition | `media.read` |
| `POST /edits/{editId}/revert/preview` | simulation d'une annulation | `media.read` |
| `POST /edits/{editId}/revert` | annulation | **`media.write`** |
| `POST /items/{serverId}/{ratingKey}/lock/preview` | simulation du verrou | `media.read` |
| `POST /items/{serverId}/{ratingKey}/lock` | poser / lever le verrou | **`media.write`** |
| `GET /backup/status` | état de la dernière sauvegarde | `media.read` |
| `GET /fields` | champs écrivables par type et bornes | `media.read` |

Aussi livrés : les scopes de clés `media.read` / `media.write` (indépendants ; la combinaison `scrape media.read` est
admise), la table `metadata_edits` (migration **031**), le **verrou étendu** (toutes les colonnes que l'API écrit sont
protégées contre la synchronisation et les autres écrivains automatiques, § 4 X4), les limites (corps 256 Kio, 300
requêtes/min par clé dont 90 écritures/min, échéance 15 s) et le réglage d'instance de test
`BACKGROUND_JOBS_ENABLED=false`. Runbook d'exploitation : `docs/34-ops-metadata-api.md`.

## 2. Où est le contrat machine

`docs/contracts/metadata-v1.openapi.json` (dans le dépôt du backend) : les 14 chemins et **tous** les schémas qu'ils
référencent, en OpenAPI 3.1, sans hôte ni valeur de configuration. Le fichier est **généré** par
`tests/metadata_api/test_openapi_export.py` à partir de l'application et le test échoue s'il diverge. Versions propres :
`x-schema-version: 1` (formes de réponse) et `x-revision-form: 1` (forme de l'empreinte de révision — la révision reste
une chaîne opaque, ne jamais la calculer côté client). C'est ce fichier que `/schema-sync` de plexhubTMM doit lire.
Noms de composants : voir X27.

## 3. Comment obtenir les clés

Les clés ne sont **pas** dans ce dépôt ni dans cette note. Chakir les crée dans `/admin/keys` ou par
`POST /api/admin/keys` (clé maître), les copie une seule fois et les remet à plexhubTMM par un canal privé :

- clé de **lecture** : scopes `scrape media.read` ;
- clé d'**écriture** : scopes `media.read media.write`, expiration **365 jours** (`expiresInDays: 365`).

Une clé **sans scope** est refusée (403) sur `/api/metadata/v1` ; une clé à scopes est confinée à ses routes (403
`insufficient_scope` ailleurs). Marche à suivre complète (révocation, renouvellement avant expiration) : runbook § 1–2.

## 4. Tableau des écarts X1 → X33

« Contrat » = ce que disait `16-contrat-b2-backend.md` ; « Retenu par B2 » = ce que le backend a livré.

| # | Sujet | Contrat (provisoire) | Retenu par B2 | Effet côté client | À reporter dans plexhubTMM |
|---|---|---|---|---|---|
| X1 | Simulation (§ 5.5) | « transaction annulée ou `SAVEPOINT` relâché » | un **plan pur** (`plan_patch`) partagé par la simulation et l'écriture ; la simulation n'ouvre aucune transaction d'écriture | Aucun : mêmes champs, dérivés, regroupement et `revisionAfter` ; la simulation ne prend jamais le verrou d'écriture | Rien à faire. |
| X2 | Clé à scopes ailleurs (§ 4.1) | « toute clé à scopes reçoit 403 sur le reste de `/api/*` » | 403 `insufficient_scope` sur les routes gardées par `verify_backend_secret`/`verify_api_key` ; **401 `Master key required` sur `/api/admin/*`** (comportement ADR 0008, inchangé) | Ne pas attendre un 403 sur `/api/admin/*` | Corriger la phrase du § 4.1. |
| X3 | Colonnes protégées (§ 8.2) | colonnes du § 6.2 (+ `title_sortable`, `cast`) | + **`display_rating`**, `unification_id`, `history_group_key` : sinon la synchronisation réécrit `display_rating` d'une ligne verrouillée avec la note du fournisseur et efface la note mélangée posée par B2 | Un `display_rating` écrit par B2 survit | Ajouter `display_rating` à la liste des colonnes protégées. |
| X4 | Inventaire des écrivains (§ 2, § 8.2) | 7 écrivains cités | **38 sites d'écriture** (26 couples fichier/fonction/opération) inventoriés (test statique `tests/test_media_writers_inventory.py`). Protégés en plus : épisodes de `strip_titles_pollution`, `cli recompute-unification`, import NFO **y compris en fill-missing** (qui re-remplissait un id effacé). **3 exceptions assumées** (ADR 0012) : le marquage adulte (`content_rating = XXX`), la suppression d'un titre retiré du fournisseur, le recalcul de `display_rating` | Une ligne verrouillée peut encore changer pour ces trois causes | Prévoir ces trois cas hors verrou dans l'analyse de risque et dans l'écran d'erreur. |
| X5 | Réglage d'instance de test (§ 11 Q5) | « réglage à nommer, coupe la synchro planifiée » | **`BACKGROUND_JOBS_ENABLED=false`** (défaut `true`). Coupe aussi l'élection `fcntl` : sans cela l'instance **ne démarre pas sous Windows** (`import fcntl`) ; coupe scheduler, pipeline initial, worker de téléchargement, reprise Plex ; `isMaster: false` | L'instance jetable démarre sous Windows | Poser ce réglage dans l'environnement vidé de l'instance jetable (runbook § 8). |
| X6 | `lastSuccessAt` (§ 5.12) | sauvegarde « complète et vérifiée encore présente » | l'instant de la plus récente sauvegarde **présente**, vérifiée ou non ; `integrityCheck` vaut `ok` seulement si un fichier `.verified` concorde (nom, taille, date), `unknown` sinon, `not_run` si aucune sauvegarde | Une sauvegarde antérieure à B2 donne `lastSuccessAt` renseigné + `unknown` | Refuser d'écrire si `integrityCheck != ok` (déjà prévu) ; message utile : « lancer `--verify-latest` ou attendre la sauvegarde de 04 h ». |
| X7 | `lastAttemptAt` / `lastAttemptOutcome` | derniers essais | lus dans un fichier d'état durable du dossier de sauvegarde ; `null` tant qu'aucune sauvegarde n'a tourné depuis B2 | Valeurs nulles possibles après déploiement | Tolérer `null`. |
| X8 | `scheduleTimezone` | nom IANA ou décalage | nom `tzlocal` (le fuseau d'APScheduler, `TZ`), repli `UTC±hh:mm` ; calculé sans le planificateur (donc aussi sur un esclave ou une instance de test) | `scheduleHourLocal` s'interprète dans ce fuseau | Interpréter `BACKUP_HOUR` dans ce fuseau. |
| X9 | Corps (§ 4.6) | 256 Kio | 256 Kio sous `/api/metadata/v1/` ; la passerelle B1 reste à 64 Kio (table par préfixe dans un seul middleware) | Aucun | Rien à faire. |
| X10 | Casting `thumb`/`profile` (§ 6.2) | URL https | URL https de **2 048 caractères au plus** | 400 `invalid_field` (`cast`) au-delà | Borner à 2 048. |
| X11 | `context` (Q7) | — | **`durationImplausible`** (additif) : vrai si `duration` < 5 min (`IMPLAUSIBLE_DURATION_MS`) ; la valeur reste écrivable | Un champ de plus | Traiter la durée comme absente quand il vaut vrai. |
| X12 | `beforeVariants[]` (§ 6.7) | `{filter, sortOrder, fields, lock}` | + **`derived`** de chaque variante (additif) | Un champ de plus | Ignorer ou afficher. |
| X13 | Lecture de `cast` | `cast_json` | `cast_json` s'il est valide, **sinon les noms du CSV `cast`** (rôles `null`) ; une relecture peut donc différer d'un `cast_json` envoyé quand seul le CSV existait | Un envoi identique aux noms du CSV est un no-op | Comparer le casting sur noms + rôles. |
| X14 | `countries` (Q8) | ISO ou noms | stockés **tels quels**, sans normalisation ; la base mélange déjà noms TMDB et codes ISO | Aucun | Ne pas normaliser à la lecture. |
| X15 | Table du journal (§ 7) | colonnes listées | `metadata_edits` : colonnes du contrat + **`item_type`** ; migration **031**, défauts SQL sur les colonnes JSON | Aucun | Rien à faire. |
| X16 | Champ non écrivable pour le type (§ 6.2) | 400 `invalid_field` | 400 `invalid_field` (ex. `status` sur un film, `genres` sur un épisode) ; une clé **inconnue partout** (`thumbUrl`, `unificationId`…) est le **422 natif** (`extra="forbid"`) | Deux codes distincts | Mapper les deux. |
| X17 | Annulation : `display_rating` (§ 5.10) | « recalculé » | **restitué depuis l'enregistrement**, comme les autres dérivés (plus fidèle quand `display_rating` ne valait pas le mélange IMDb/TMDB) | Aucun en pratique | Rien à faire. |
| X18 | Annulation : règle du verrou (§ 5.10) | refus si une édition plus récente non annulée existe | la règle ne s'applique que si l'édition annulée **a changé le verrou** ; et **règle de compensation** : une édition annulée, ou une annulation d'une édition plus récente, ne compte pas — sinon on ne pourrait jamais défaire `E2` puis `E1` | Annuler de la plus récente à la plus ancienne fonctionne | Documenter l'ordre « dernier posé, premier annulé ». |
| X19 | Bornes et types (§ 4.7) | 400 pour les bornes, 422 pour les types | une valeur hors borne d'un champ connu est **400 `invalid_field`** (nom du champ) ; un mauvais **type** (`"7"` pour une note, `1` pour un booléen) est le **422** FastAPI ; `reason` (0..200 caractères, sans caractère de contrôle ni point de code invalide) hors bornes est un **400 `invalid_field`** (`field: "reason"`) | Les corps sont **stricts** (aucune coercition) | Envoyer des types exacts. |
| X20 | `If-Match` (§ 4.4) | forme forte | `"rev"` ou `rev` nu accepté ; `W/…`, `*`, valeur mal formée → **400 `invalid_parameter`** ; absent → 428 | Aucun | Envoyer `"<revision>"`. |
| X21 | Idempotence (§ 4.5) | rejeu rend la réponse enregistrée | rendue **avant** le contrôle `If-Match` (même si la révision a changé), `replayed: true` ; la clé est unique par couple (clé d'API, `Idempotency-Key`) ; une requête refusée (4xx/503) n'est pas enregistrée ; un **no-op n'est pas enregistré** (son rejeu est réévalué) | Un 503 se rejoue avec la même clé | Réutiliser la même `Idempotency-Key` après un 503/réseau. |
| X22 | `GET /items` (§ 5.2 bis) | `limit` 1..500, `cursor` opaque | identique ; aucun filtre de visibilité (cachés, adultes, masque de panne fournisseur **inclus** : `context.isHidden`, `isAdult` exposés) | Voit tout | Filtrer côté client si besoin. |
| X23 | `history` d'un élément inconnu (§ 5.8) | 404 si inconnu **et** sans édition | identique ; un élément supprimé par la synchronisation garde son historique lisible | Aucun | Rien à faire. |
| X24 | `lookup` : `type` (§ 5.2) | `movie` ou `show` | identique ; `episode` ou autre → **422** natif | Aucun | Rien à faire. |
| X25 | Variante disparue à l'annulation | non traité | une variante de l'édition qui n'existe plus est ignorée ; si aucune n'existe plus → 409 `revert_conflict` avec `fields: ["variants"]` | Un cas de plus dans `fields` | Gérer `variants` comme valeur de `fields`. |
| X26 | Écriture réelle : variantes inchangées | non traité | **toutes** les variantes reçoivent `updated_at` et le verrou, même celles dont aucun champ ne change | Aucun | Rien à faire. |
| X27 | Noms OpenAPI (contrat machine) | non fixés | modèles d'erreur `MetadataErrorResponse` / `MetadataErrorDetail` (les noms `ErrorResponse`/`ErrorDetail` existent pour la passerelle B1 : FastAPI aurait préfixé **les deux** et cassé `scrape-v1.openapi.json`) ; le JSON échangé est identique à celui de B1 | `/schema-sync` voit ces noms | Utiliser ces noms dans `/schema-sync`. |
| X28 | Migration (§ 7) | proposée 031 | `_migration_031_metadata_edits` ; version `1.13.0` | Aucun | Rien à faire. |
| X29 | Variantes exposées (§ 6.1) | « ≤ 50 » | **toutes** les variantes sont rendues, sans plafond : l'atomicité, la révision et l'annulation portent sur toutes ; un élément en a en pratique quelques-unes (une par catégorie) | Aucun | Ne pas supposer 50 au plus. |
| X30 | Annulation d'une édition inconnue | non traité | 404 `edit_not_found` est rendu **avant** le contrôle des en-têtes (428) : l'élément visé se déduit de l'édition | Un 404 peut précéder un 428 | Rien à faire. |
| X31 | Indépendance `media.read` / `media.write` (§ 4.1) | `media.write` n'inclut pas `media.read` | vrai **pour les routes** ; mais le résultat d'une écriture (`before`, `after`, `variants`) et le `currentRevision` d'un 412 rendent le contenu de l'élément visé : une clé `media.write` seule peut donc en lire l'état par ce biais (métadonnées non secrètes). Documenté, non restreint | Aucun pour plexhubTMM (sa clé d'écriture porte les deux scopes) | Garder la clé d'écriture à `media.read media.write`. |
| X32 | `PATCH /api/media/{rating_key}` historique (§ 1) | « inchangé » | inchangé — donc une clé **sans scope** peut encore écraser `imdb_id`/`tmdb_id` d'une ligne verrouillée par B2 (et poser `match_source = manual`), **sans ligne de journal** ; un `revert` du verrou donne alors `revert_conflict` | La piste d'audit B2 ne couvre pas cette route | Ne pas utiliser cette route ; une relecture avant écriture détecte le changement (révision). |
| X33 | Journal (§ 7) | sans purge | sans purge **et sans plafond** : une écriture pèse jusqu'à ~100 Ko (3 blobs JSON dont la réponse rejouable) ; 90 écritures/min maximum par clé. Surveiller la taille de la table `metadata_edits` | Croissance du fichier SQLite et des sauvegardes si une clé boucle | Respecter 1 écriture/s ; ne pas rejouer en boucle. |

## 5. Ce que plexhubTMM doit reporter ou faire

1. Lire `docs/contracts/metadata-v1.openapi.json` comme source des types.
2. Reporter X1 → X33 dans `16-contrat-b2-backend.md` (dernière colonne = action) et régénérer
   `schemas/plexhub-backend/contract-b2.openapi.json` + fixtures + tests rouges, par l'ADR 0012 de plexhubTMM.
3. Gérer tous les codes : 400 `invalid_parameter` / `invalid_field` (champ `field`), 401 `invalid_api_key`, 403
   `insufficient_scope`, 404 `item_not_found` / `edit_not_found`, 409 `regroup_not_allowed` / `revert_conflict` /
   `idempotency_key_reused`, 412 `revision_mismatch` (`currentRevision`), 413 `invalid_parameter`, 422 (FastAPI), 428
   `precondition_required` (champ `header`), 429 `rate_limited` (respecter `Retry-After`), 503 `database_busy` (rien
   d'écrit ; réessayer avec la même `Idempotency-Key`). Corps : `{"detail": {"code", "message", …}}`.
4. Avant toute écriture réelle : `GET /backup/status` (`enabled`, `integrityCheck == "ok"`, `ageSeconds <= 26 h`).
   Après le déploiement de B2, la sauvegarde existante est `unknown` : Chakir lance
   `python -m app.scripts.backup_db --verify-latest` une fois, ou attend la sauvegarde quotidienne.
5. Instance jetable : lancer le backend depuis un `git archive`, environnement vidé (`DATA_DIR`, `LOG_DIR`,
   `BACKUP_*`, clé maître de test, **`BACKGROUND_JOBS_ENABLED=false`**), sans variable `XTREAM_*`, puis
   `POST /api/accounts` vers un faux fournisseur Xtream sur `127.0.0.1` ; sous-ensemble Xtream minimal et exemple de
   réponses : runbook § 8.
6. Respecter les limites : 1 écriture/s (le serveur tolère 90/min), corps ≤ 256 Kio, réponses ≤ 4 Mio.

## 6. Réponses aux 8 questions du contrat (§ 11)

1. **Révision** : calculée — SHA-256 des champs éditables, dérivés exposés et verrou de **toutes** les variantes
   (forme canonique JSON), 22 caractères base64url. Elle change quel que soit l'écrivain, sans colonne ni modification
   des autres écrivains.
2. **`match_source`** : `plexhubtmm`, valeur existante gardée si la ligne était déjà verrouillée (décidé).
3. **État de sauvegarde** : un fichier `<nom>.db.verified` écrit avant le renommage final (nom, taille, `mtime_ns`,
   résultat) + un fichier d'état de la dernière tentative, tous deux dans le dossier de sauvegarde ; l'état est calculé
   à chaque appel depuis les fichiers présents. Pas de table (une restauration ferait régresser l'état), pas
   d'`integrity_check` à la requête. Sauvegarde antérieure à B2 : `integrityCheck: unknown`.
4. **Inventaire des écrivains** : le relevé du contrat était exact sur 7/7 mais incomplet — voir X3, X4. Un helper commun
   existe (`app/services/metadata_columns.py`) et un test statique fige les 38 sites.
5. **Instance jetable** : sous-ensemble Xtream et réglage — voir X5 et le runbook § 8.
6. **Bornes** : conservées ; ajout d'une borne d'URL de casting (X10) ; le corps de 256 Kio suffit largement.
7. **Durée implausible** : exposée (`context.durationImplausible`, X11) ; la valeur reste écrivable.
8. **`countries`** : tel quel, sans normalisation (X14).

## 7. Ce que cette note ne couvre pas

- Le **déploiement sur le homelab** (lot séparé, sur accord explicite de Chakir) : le service suit le tag flottant
  `:main`, les réglages se déclarent sous `environment:` du compose, la migration 031 s'applique au démarrage.
- La **création des clés de production** (Chakir les crée lui-même).
- Les images, la ligne `season`, les colonnes réalisateurs/scénaristes/Rotten Tomatoes/Metacritic, l'écriture par titre,
  la modification des scopes d'une clé, la purge de `metadata_edits`, l'écriture groupée : hors lot.
- Les constats ouverts côté backend : `docs/context/etat-reel.md`, section « Constats ouverts du lot B2 ».
