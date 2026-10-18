# ADR 0011 — Écriture de métadonnées : révision, simulation, journal, annulation

- Statut : **accepté** (2026-10-18, lot B2)
- Plan source : `docs/10-prd-metadata-api.md` (FR-003 → FR-008, FR-012)
- Portée : `app/api/metadata_v1.py` (nouveau), `app/services/metadata_*` (nouveaux), `app/models/database.py`
  (`MetadataEdit`), `app/db/migrations.py` (031), `app/models/metadata_schemas.py` (nouveau)
- ADR recoupés : 0004 (retry d'écriture, piège 8), 0005 (verrou `match_locked`, no-op), 0010, 0012

## D1 — Faits du code
1. `media_service.update_external_ids` (`:641-723`) est le modèle : `UPDATE` de toutes les variantes `(rating_key,
   server_id)`, recalcul `unification_id`/`history_group_key`, verrou, `schedule_rebuild` après coup.
2. `write_with_retry(work, session_factory=…)` ouvre une session **neuve** par tentative ; `BEGIN IMMEDIATE` n'est
   utilisé nulle part dans le dépôt.
3. PK de `media` : `(rating_key, server_id, filter, sort_order)` ; aucune ligne `season` ; épisodes :
   `unification_id=""`, `history_group_key=rating_key+server_id` (`sync_worker.py:372-373`).
4. Formats : genres/studio/country/cast en CSV `", "` ; `cast_json` JSON ; `duration` en ms ; `premiered` ISO.

## D2 — Décisions

### D2.1 Révision
Empreinte calculée : SHA-256 de la forme canonique JSON `{v:1, variants:[{filter, sortOrder, fields, derived, lock}]}`
(variantes triées par `(filter, sort_order)` en octets, notes arrondies à 1e-6, clés triées), tronquée à 16 octets,
base64url sans padding (22 caractères), `ETag: "<revision>"`. Exclus : `updated_at`, `context`. La propriété « change
quel que soit l'écrivain » est obtenue sans colonne ni modification des autres écrivains. Incrémenter `v` ne se fait
que volontairement (changement du jeu de champs).

### D2.2 Moteur pur et simulation
`plan_patch(variants, body, flags) -> Plan` (sans I/O) : validation (400 `invalid_field`), normalisation (`trim`,
CSV, `cast_json`+`cast`, ms), dérivés **par variante** (`unification_id`/`history_group_key` via
`calculate_unification_id`/`calculate_history_group_key` pour film/série, figés pour épisode ; `title_sortable` ;
`display_rating` via `blend_display_rating_case`), regroupement, no-op, révision après. `preview` = lectures +
`plan_patch` + comptages `membersBefore/After` (lectures) : **aucune transaction d'écriture**. `PATCH` appelle le même
`plan_patch`. Ainsi « preview et PATCH rendent le même `after`/dérivés/`revisionAfter` » est vrai par construction.
`membersBefore/After` = éléments `(server_id, rating_key)` **autres** partageant la valeur ; `""` ⇒ 0.

### D2.3 Transaction d'écriture
Session neuve (`write_with_retry`), ordre : prise du verrou d'écriture (`BEGIN IMMEDIATE`, spike B2-01) → **rejeu
idempotent** (`(key_id, idempotency_key)` + `body_sha256` du corps canonique chemin compris ⇒ réponse enregistrée,
`replayed:true`, même si la révision a changé ; autre corps ⇒ 409) → `If-Match` (412 avec `currentRevision`) → plan →
409 `regroup_not_allowed` → no-op (200 `noop:true`, rien écrit) → `UPDATE` de toutes les variantes (`updated_at =
now_ms()`, verrou : `match_locked=1`, `match_source='plexhubtmm'` **seulement** si non verrouillée) → insertion
`metadata_edits` avec `response_json` préparé **avant** `COMMIT` → `COMMIT`. Puis, hors transaction,
`unified_group_service.schedule_rebuild(type)`. Aucune 5xx possible après `COMMIT` (seule la sérialisation suit).

### D2.4 Échéance et 503
`METADATA_REQUEST_TIMEOUT_SECONDS` (15, borné 5..28). `PRAGMA busy_timeout` de la connexion d'écriture aligné sur
l'échéance restante pour que SQLite abandonne lui-même (un appel aiosqlite bloqué n'est pas annulable proprement) ;
contrôle d'échéance entre les étapes avant `COMMIT` ; `COMMIT` jamais interrompu ; dépassement ⇒ rollback, 503
`database_busy` + `Retry-After`. **Risque R1** : validé par un spike (B2-01) **sous vrai verrou WAL** ; repli : `BEGIN`
ordinaire + `UPDATE` sentinelle de prise de verrou.

### D2.5 Journal `metadata_edits` (migration 031)
Colonnes du contrat § 7 + `item_type`. `edit_id` = ULID monotone (48 bits ms + aléa, ordre croissant garanti dans le
process). Index `(server_id, rating_key, at_ms)` ; unique `(key_id, idempotency_key)`. `before_json` conserve **par
variante** champs, verrou et dérivés (restitution exacte de variantes divergentes) ; `after_json` idem ; `response_json`
= réponse complète rejouable. Append-only sauf `reverted_by`. Jamais de clé : `key_id` = `id` interne (`master` pour le
maître). Pas de purge automatique.

### D2.6 Annulation et verrou
`revert` : édition `patch`/`lock` non annulée (sinon 409 `fields:[]`). Pour **chaque** variante, chaque champ modifié
doit encore valoir la valeur « après » (sinon 409 `revert_conflict` + `fields`, aucune annulation partielle). Verrou
restitué seulement si le verrou courant vaut `lockAfter` **et** aucune édition plus récente non annulée sur l'élément
(sinon 409 `fields:["lock"]`). Valeurs « avant » par variante ; dérivés `unification_id`/`history_group_key`/
`title_sortable` **et `display_rating` restitués depuis l'enregistrement** (précision D2.8 : écart X17 — une
restitution enregistrée est plus fidèle qu'un recalcul quand `display_rating` ne valait pas le mélange) ; contrôle de regroupement ; `reverted_by` posé dans la même
transaction ; une annulation est une édition (`kind:"revert"`, `reverts`) annulable. `lock` : `locked:true` ⇒ verrou
`plexhubtmm` si libre ; `locked:false` ⇒ `match_locked=0, match_source=NULL` ; même état ⇒ no-op.

### D2.7 Lecture
`cast` lu depuis `cast_json` si valide, sinon noms du CSV (rôles `null`). `countries` tel quel. `context.
durationImplausible = duration < IMPLAUSIBLE_DURATION_MS`. `variants.items` ≤ 50, canonique (plus petit
`(filter, sort_order)` en octets) en tête. `lookup` ≤ 50 + `truncated`, `items` paginé par curseur opaque keyset sur
`(server_id, rating_key)`, `episodes` ≤ 2 000. Lignes cachées et adultes comprises.

### D2.9 Durcissements issus des relectures indépendantes (2026-10-18)
- **Plafond de concurrence** : `MAX_CONCURRENT_WRITERS = 3` écrivains au plus attendent / tiennent le verrou d'écriture
  (sémaphore, une connexion du pool API chacun) ; les autres font la queue sans connexion et rendent 503 `database_busy`
  à l'échéance. Une salve de PATCH ne peut plus vider le pool qui sert le reste de l'API.
- **`busy_timeout` restauré sur annulation** (nettoyage protégé par `asyncio.shield`).
- **Caractères** : contrôle, substituts isolés et points de code non attribués / non-caractères sont refusés (400
  `invalid_field`) ; les caractères de format (ex. la liaison de largeur nulle persane) restent permis. `reason` suit les
  mêmes règles et sa borne (200) est un 400, pas un 422 (le 422 de Pydantic recopie l'entrée et ne peut pas être rendu si elle
  contient un substitut isolé — 500 après le `COMMIT`).
- **Identifiants** : regex ASCII + `fullmatch` (chiffres Unicode et saut de ligne final refusés) ; `votes` ≤ 2^31−1.
- **Curseurs** : `at_ms` borné ; une imbrication profonde ou un entier démesuré donne 400.
- **Annulation et regroupement** : la restitution du `unification_id` enregistré n'est valable que si titre, année et ids
  du résultat sont ceux d'avant l'édition ; sinon il est recalculé depuis le résultat (une édition de titre postérieure ne
  laisse plus un groupe périmé sur une ligne verrouillée). Le 409 de regroupement expose la première variante qui
  regroupe réellement (jamais « avant == après »).

### D2.8 Précisions retenues à l'implémentation (2026-10-18)
- **Règle du verrou à l'annulation** : elle ne s'applique que si l'édition annulée a *changé* le verrou
  (`lockBefore ≠ lockAfter`) ; une édition faite sur une ligne déjà verrouillée n'a rien à restituer côté verrou et
  n'est donc pas bloquée par une édition plus récente (les conflits de champs, eux, restent contrôlés).
- **Règle de compensation** : parmi les « éditions plus récentes non annulées », on ne compte ni une édition déjà
  annulée, ni une annulation dont la cible est elle-même plus récente que l'édition visée (la paire s'annule). Sans
  cela, annuler `E2` empêcherait ensuite d'annuler `E1` (la pile ne se déferait jamais).
- **`display_rating` à l'annulation** : restitué depuis l'enregistrement, pas recalculé (X17).
- **Variante disparue** : une variante de l'édition qui n'existe plus est ignorée ; si aucune n'existe encore,
  409 `revert_conflict` avec `fields: ["variants"]`.
- **`update` d'une variante sans changement de champ** : `updated_at` et le verrou sont quand même posés sur
  *toutes* les variantes d'une écriture réelle.
- **Sélection des lignes** : l'API voit tout (catégories masquées, adultes, masque de panne fournisseur compris) ; ce
  n'est pas une lecture « app ».
- **Corps d'idempotence** : empreinte de `{méthode chemin, fields tels qu'envoyés, allowRegroup, reason}` — les
  valeurs par défaut explicites ou absentes donnent la même empreinte.

## D3 — Alternatives écartées
- Révision stockée (compteur) : exige que tous les écrivains l'incrémentent.
- Preview par transaction annulée : prend le verrou d'écriture pour rien et diverge si le chemin d'écriture change.
- Recalculer les dérivés à l'annulation : peut regrouper des historiques par accident.

## D4 — Tests exigés
Ceux du contrat § 9 (écriture, simulation ≡ patch, concurrence/idempotence, regroupement, annulation, atomicité sur N
variantes, épisodes « Pilot »), plus **sous vrai verrou WAL** : échéance ⇒ 503 sans écriture, deux écritures
concurrentes ⇒ 412 pour la seconde, rejeu concurrent d'une même clé ⇒ une seule édition.

## D5 — Conséquences
Premier `BEGIN IMMEDIATE` du dépôt (documenté, piège ajouté à `CLAUDE.md` §9). Revue `security-reviewer`
(écriture par clé d'API, `X-API-Key`).
