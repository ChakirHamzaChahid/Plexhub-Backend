# PRD — Lot B2 : API d'édition des métadonnées `/api/metadata/v1`

**Créé** : 2026-10-18 · **Statut** : spécifié (accord de Chakir le 2026-10-18 sur la spec et le plan)
**Demande d'origine** : « lot B2 » — plexhubTMM (outil Rust de gestion de métadonnées) doit **écrire** dans le backend
les métadonnées vérifiées (ids, titres, résumés, genres, notes, casting…), par API uniquement, avec simulation,
contrôle de concurrence, journal avant/après et annulation.
**Entrée** : contrat `plexhubTMM/docs/16-contrat-b2-backend.md` (validé par Chakir le 2026-10-18, lu en entier ; dépôt
plexhubTMM non modifié). Ce PRD dit ce que le backend livre ; les écarts au contrat sont au § « Écarts au contrat »
et repris dans `docs/b2-remise-plexhubtmm.md`.
**ADR** : [0010 — scopes `media.read`/`media.write` et `route_audit` par préfixe](architecture/adr/0010-media-scopes-route-audit.md) ·
[0011 — écriture, révision, journal, annulation](architecture/adr/0011-metadata-write-revision-journal.md) ·
[0012 — verrou étendu, état de sauvegarde, instance de test](architecture/adr/0012-extended-lock-backup-status-test-instance.md)
**Modèle** : lot B1 (`docs/10-prd-scrape-gateway.md`, ADR 0008/0009).

## Problème

La seule écriture de métadonnées par clé d'API est `PATCH /api/media/{rating_key}` (ids IMDb/TMDB seulement, clé
**sans scope**, donc sur-privilégiée), sans simulation, sans révision, sans journal, sans retour arrière. Le verrou
de ligne (`match_locked`) ne protège qu'une partie des colonnes contre la synchronisation, et plusieurs écrivains
automatiques l'ignorent. Il est impossible de donner à un outil externe une clé « édition de métadonnées » à
privilèges minimaux, ni de savoir si une sauvegarde récente et saine existe avant d'écrire.

## Décisions de Chakir (2026-10-18, non rediscutées)

1. API dédiée `/api/metadata/v1`, deux scopes indépendants `media.read` / `media.write` (mécanique de l'ADR 0008).
   `scrape media.read` (clé de lecture de plexhubTMM) est admise. `PATCH /api/media/{rating_key}` inchangé.
2. Verrou étendu : toute écriture réelle pose `match_locked = 1` ; la protection du verrou couvre **toutes** les
   colonnes que B2 peut écrire. Déverrouillage par l'API sous `media.write`, journalisé.
3. Jeu de champs « prudent + titre » (contrat § 6.2/§ 6.3). Jamais écrits : images, colonnes calculées, état de
   lecture, hash, clés. Épisode : `unification_id` (`""`) et `history_group_key` (`rating_key+server_id`) figés.
4. Regroupement autorisé via le garde `allowRegroup`, avec `unificationId` avant/après et indicateur `regroup`.
5. `match_source = 'plexhubtmm'`, sauf si la ligne était déjà verrouillée (valeur existante gardée).
6. Réglage d'environnement coupant synchronisation planifiée et tâches de fond d'une instance de test, sans effet par
   défaut.
7. Instance jetable de plexhubTMM : `git archive`, environnement vidé, base synthétique, `127.0.0.1`, peuplée par le
   backend (faux fournisseur Xtream + `POST /api/accounts`).

## User stories

- **US1 (P1) — Clé « lecture » et clé « écriture ».** Chakir crée une clé `scrape media.read` et une clé
  `media.read media.write` (365 jours). Chaque clé n'ouvre que ses routes ; une clé sans scope est refusée.
- **US2 (P1) — Retrouver et lire.** `lookup` par id externe ou `unificationId`, `items` (éléments sans id),
  `items/{s}/{r}` (champs, dérivés, verrou, révision, variantes), `episodes`, `fields`.
- **US3 (P1) — Simuler puis écrire.** `preview` rend exactement ce que `PATCH` écrira (champs, dérivés, regroupement,
  révision après) sans rien écrire ; `PATCH` exige `If-Match` et `Idempotency-Key`, est atomique sur toutes les
  variantes et journalisé.
- **US4 (P1) — Revenir en arrière.** `history`, `edits/{id}`, `revert[/preview]`, `lock[/preview]` : l'annulation
  restitue champs, variantes, dérivés et verrou.
- **US5 (P1) — Ne pas perdre le travail.** Une ligne verrouillée ne change plus sur aucune colonne que B2 écrit, quel
  que soit l'écrivain automatique (synchronisation, trailers, certifications, notes OMDb, NFO, renommage de titres).
- **US6 (P2) — Sauvegarde saine.** `GET /backup/status` dit si une sauvegarde présente, récente et vérifiée existe.
- **US7 (P2) — Instance de test.** `BACKGROUND_JOBS_ENABLED=false` permet de lancer une instance jetable sans tâche
  de fond (y compris sous Windows).

## Exigences fonctionnelles

- **FR-001** Scopes `media.read`/`media.write` dans `KNOWN_SCOPES`, `ApiKeyCreate`, formulaire `/admin/keys` ;
  gardes nommés `verify_media_read_scope`/`verify_media_write_scope` ; `route_audit` les reconnaît **sous
  `/api/metadata/v1/` seulement** et refuse le démarrage dans le cas contraire (et inversement pour les gardes `scrape`).
- **FR-002** 14 routes (contrat § 5.1), garde par route puis limiteur de débit ; routeur sans garde global.
- **FR-003** Révision = empreinte calculée ; change quel que soit l'écrivain ; `ETag` sur les routes du contrat.
- **FR-004** `If-Match` (428/412) et `Idempotency-Key` (428, rejeu `replayed`, 409 `idempotency_key_reused`) sur les
  routes qui valident ; aucune 5xx après `COMMIT`.
- **FR-005** Simulation et écriture partagent `plan_patch` (fonction pure) ; la simulation n'écrit rien.
- **FR-006** Écriture atomique sur toutes les variantes + dérivés + journal ; no-op sans verrou ni audit ni révision.
- **FR-007** Regroupement : `regroup` partout, 409 `regroup_not_allowed` sans `allowRegroup`.
- **FR-008** Journal `metadata_edits` (migration 031) ; annulation avec contrôle de conflit et règle du verrou.
- **FR-009** `METADATA_WRITABLE_COLUMNS` unique ; upsert de synchronisation et autres écrivains sautent les lignes
  verrouillées ; test d'inventaire statique des écrivains de `media`.
- **FR-010** `GET /backup/status` calculé à la requête depuis les fichiers (sidecars `.verified`), jamais de chemin.
- **FR-011** `BACKGROUND_JOBS_ENABLED` (défaut `true`).
- **FR-012** Limites : corps 256 Kio, débit 300/90 par minute par clé, échéance 15 s (5..28) → 503 `database_busy`.
- **FR-013** Contrat machine `docs/contracts/metadata-v1.openapi.json` généré et vérifié par test ; version 1.13.0 ;
  runbook `docs/34-ops-metadata-api.md` ; note de remise `docs/b2-remise-plexhubtmm.md`.

## Non-goals

Images ; ligne `season` ; colonnes réalisateurs/scénaristes/Rotten Tomatoes/Metacritic ; écriture par titre ;
modification des scopes d'une clé ; purge de `metadata_edits` ; écriture groupée ; **déploiement homelab** (lot
séparé, accord explicite de Chakir) ; création de clés de production.

## Contraintes du dépôt

SQLite mono-écrivain : tout writer critique est testé sous **vrai verrou WAL** (piège 8) ; jamais de `rollback()` +
retry sur la même session ; migrations idempotentes en fin de chaîne, défauts au niveau SQL (leçons 025/027) ; aucune
clé, URL fournisseur ni corps complet dans les journaux INFO ; état process-local (limiteurs) ⇒ pas de `--workers N>1`.

## Écarts au contrat (X1 → X16 à la spec ; liste **finale** X1 → X33 dans `docs/b2-remise-plexhubtmm.md`)

| # | Contrat | Retenu | Action plexhubTMM |
|---|---|---|---|
| X1 | preview = transaction annulée | plan pur partagé (`plan_patch`), aucune transaction d'écriture | aucune |
| X2 | clé à scopes : 403 sur le reste de `/api/*` | 403 sur les routes gardées par `verify_backend_secret`/`verify_api_key` ; **401 `Master key required`** sur `/api/admin/*` (ADR 0008) | ne pas supposer 403 sur `/api/admin/*` |
| X3 | colonnes protégées = § 6.2 | + `display_rating` (sinon la note mélangée posée par B2 est réécrite par la synchro) | aucune |
| X4 | écrivains du § 2 | + épisodes de `strip_titles_pollution`, `cli recompute-unification`, NFO en fill-missing (re-remplit un id effacé) ; **exceptions assumées** : marquage adulte (`content_rating`), `delete(Media)` de la synchro, recalcul de `display_rating` | prévoir ces trois cas hors verrou |
| X5 | réglage « coupe la synchro » | `BACKGROUND_JOBS_ENABLED=false` supprime aussi `import fcntl` (boot Windows) | l'utiliser |
| X6 | `lastSuccessAt` = sauvegarde vérifiée | rempli aussi pour une sauvegarde non vérifiée, avec `integrityCheck: unknown` | refuser si `integrityCheck ≠ ok` (déjà prévu) |
| X7 | `lastAttempt*` | lus dans un fichier d'état durable ; `null` tant qu'aucune tentative depuis B2 | tolérer `null` |
| X8 | `scheduleTimezone` IANA ou décalage | nom `tzlocal`, repli `UTC±hh:mm` | interpréter `scheduleHourLocal` dans ce fuseau |
| X9 | corps 256 Kio | limite par préfixe (passerelle B1 reste à 64 Kio) | aucune |
| X10 | casting `thumb`/`profile` URL https | ≤ 2 048 caractères | borner côté client |
| X11 | `context` | + `durationImplausible` (additif) | ignorer ou traiter la durée comme absente |
| X12 | `beforeVariants[]` | + `derived` par variante (additif) | ignorer |
| X13 | `cast` lu | repli sur les noms du CSV si `cast_json` absent ⇒ une relecture peut différer d'un `cast_json` envoyé | comparer sur noms+rôles |
| X14 | `countries` ISO ou noms | tel quel (la base mélange déjà les deux) | ne pas normaliser |
| X15 | table `metadata_edits` | + colonne `item_type` ; migration 031 | aucune |
| X16 | champs par type | un champ non écrivable pour le type ⇒ 400 `invalid_field` (ex. `status` hors `show`) | respecter `GET /fields` |

## Réponses aux questions ouvertes du contrat (§ 11)

1. Révision **calculée** (empreinte). 2. `plexhubtmm` (décidé). 3. État de sauvegarde par sidecars + fichier d'état,
`unknown` pour une sauvegarde antérieure. 4. Inventaire : voir X4 et ADR 0012 ; helper = `metadata_columns.py` + test
statique. 5. Sous-ensemble Xtream : voir le runbook ; réglage `BACKGROUND_JOBS_ENABLED`. 6. Bornes conservées, URL de
casting ≤ 2 048. 7. Durée implausible exposée (`context.durationImplausible`). 8. `countries` tel quel.

## Critères de succès

Suite complète verte (gate 70 %), `ruff` vert, migrations 001→031 rejouables, contrat machine à jour, aucun écrivain
de `media` hors liste blanche, `scrape media.read` ouvre la passerelle et les lectures B2 seulement.
