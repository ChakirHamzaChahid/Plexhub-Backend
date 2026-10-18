# Runbook — API d'édition des métadonnées `/api/metadata/v1` (lot B2)

Public : l'opérateur du backend (Chakir). Contrat machine : `docs/contracts/metadata-v1.openapi.json`.
Spec : `docs/10-prd-metadata-api.md`, ADR 0010 / 0011 / 0012. Remise à plexhubTMM : `docs/b2-remise-plexhubtmm.md`.

> Les commandes ci-dessous utilisent `$BACKEND_URL` et `$KEY` (variables d'environnement de votre shell) : aucune
> adresse, aucun mot de passe ni aucune clé ne figure dans ce dépôt.

## 0. En bref

- 14 routes sous `/api/metadata/v1`, deux scopes indépendants : `media.read` (lectures **et** simulations) et
  `media.write` (les 3 routes qui valident : `PATCH /items/{s}/{r}`, `POST /items/{s}/{r}/lock`,
  `POST /edits/{id}/revert`).
- Toute écriture réelle **verrouille la ligne** (`match_locked = 1`, `match_source = 'plexhubtmm'`, ou la valeur
  existante si elle était déjà verrouillée) : la synchronisation, l'enrichissement et les scripts ne la modifient
  plus sur les colonnes que l'API écrit.
- Tout est journalisé (`metadata_edits`) avec l'image avant/après de **chaque variante** : on peut annuler.
- Les clés ne sont jamais créées par le code : vous les créez vous-même (§ 1).

## 1. Créer les clés

Deux clés pour plexhubTMM, **une seule fois affichées** à la création.

| Clé | Scopes | Expiration |
|---|---|---|
| lecture | `scrape media.read` | selon votre politique |
| écriture (chargée seulement pour une écriture réelle) | `media.read media.write` | **365 jours** |

Écran d'administration : `/admin/keys` (cases à cocher `scrape`, `scrape.admin`, `media.read`, `media.write`).
Ou, avec la clé maître :

```bash
curl -sS -X POST "$BACKEND_URL/api/admin/keys" -H "X-API-Key: $MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"label": "plexhubTMM écriture", "scopes": ["media.read", "media.write"], "expiresInDays": 365}'
```

Règles à connaître :

- Une clé **à scopes est confinée** : 403 `insufficient_scope` sur tout le reste de `/api/*` (et 401
  `Master key required` sur `/api/admin/*`). Une clé **sans scope** est refusée (403) sur `/api/metadata/v1`.
- La combinaison `scrape media.read` ouvre la passerelle de scraping **et** les lectures/simulations de cette API,
  rien d'autre. `media.write` n'inclut pas `media.read`.
- On ne modifie pas les scopes d'une clé : on la révoque et on en crée une autre.

## 2. Révoquer, renouveler

- Révocation immédiate : `/admin/keys` (bouton Révoquer) ou `DELETE /api/admin/keys/{id}` avec la clé maître. La
  clé révoquée ou expirée reçoit 401 `invalid_api_key`.
- Renouvellement : créer la nouvelle clé, la remettre à plexhubTMM, puis révoquer l'ancienne **avant** l'expiration.
- L'historique garde l'identifiant **interne** de la clé (`keyId`), jamais la clé ni son préfixe ; `master` pour le
  secret maître.

## 3. Lire l'historique d'un élément

Identifier l'élément par `serverId` (compte Xtream, ex. `xtream_xxxxxxxx`) et `ratingKey` (ex. `vod_123.mkv`,
`series_45`, `ep_678.mkv`) — toutes les variantes de catégorie sont un seul élément.

```bash
curl -sS -H "X-API-Key: $KEY" "$BACKEND_URL/api/metadata/v1/items/$SERVER_ID/$RATING_KEY/history?limit=20"
curl -sS -H "X-API-Key: $KEY" "$BACKEND_URL/api/metadata/v1/edits/$EDIT_ID"
```

Du plus récent au plus ancien ; `nextCursor` pour la suite. L'historique reste lisible même si la synchronisation
a supprimé l'élément (un titre retiré du fournisseur disparaît, ses éditions restent). Il n'y a **aucune purge
automatique** : le volume est faible (écritures manuelles).

## 4. Annuler une édition

1. Simuler : `POST /edits/{editId}/revert/preview` (clé `media.read`) — rend ce qui serait restitué, ou un 409.
2. Valider : `POST /edits/{editId}/revert` avec `If-Match: "<révision courante de l'élément>"` et
   `Idempotency-Key: <valeur unique>` (clé `media.write`).

L'annulation restitue, pour **chaque variante**, les champs modifiés, les dérivés (`unificationId`,
`historyGroupKey`, `titleSortable`, `displayRating` — restitués, pas recalculés) et l'état du verrou. Elle est
elle-même une édition (`kind: revert`) et peut être annulée pour rétablir l'édition d'origine.

409 `revert_conflict` (rien n'est écrit) :

| `fields` | Sens |
|---|---|
| `["summary", …]` | ces champs ont changé depuis l'édition (synchro, enrichissement, autre édition) |
| `["lock"]` | le verrou a changé depuis, ou une édition plus récente (non compensée) existe |
| `["variants"]` | aucune variante de l'édition n'existe plus |
| `[]` | l'édition est déjà annulée |

Pour défaire plusieurs éditions : de la plus récente à la plus ancienne (annuler `E2` puis `E1`).
409 `regroup_not_allowed` : l'annulation regrouperait l'élément ; renvoyer `{"allowRegroup": true}` après avoir
lu `unificationIdBefore/After` et `membersBefore/After`.

## 5. Verrouiller / déverrouiller

```bash
curl -sS -X POST "$BACKEND_URL/api/metadata/v1/items/$SERVER_ID/$RATING_KEY/lock" \
  -H "X-API-Key: $WRITE_KEY" -H "If-Match: \"$REVISION\"" -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" -d '{"locked": false}'
```

- `locked: false` ⇒ `match_locked = 0`, `match_source = NULL` (comme le déverrouillage de `/admin`) : la ligne
  redevient éligible à l'enrichissement et à la synchronisation. Journalisé (`kind: lock`).
- `locked: true` sur une ligne libre ⇒ `plexhubtmm`. Même état ⇒ no-op (200, `noop: true`, rien d'écrit).
- Une simulation est disponible : `POST …/lock/preview` (clé `media.read`).

**Exceptions au verrou (assumées, ADR 0012)** — ce qui peut encore changer une ligne verrouillée :
le marquage adulte (`content_rating = XXX`), la suppression d'un titre retiré du fournisseur, le recalcul de
`display_rating` à partir de notes déjà protégées, et le scraper manuel `/admin` (décision de l'opérateur).

## 6. État de la sauvegarde

`GET /api/metadata/v1/backup/status` (clé `media.read`) — calculé **à chaque appel** depuis le dossier de
sauvegarde, sans ouvrir la base ni relancer `integrity_check` :

| Champ | Sens |
|---|---|
| `lastSuccessAt` / `ageSeconds` | plus récente sauvegarde **présente** (nom du fichier, UTC) ; `null` si aucune |
| `integrityCheck` | `ok` si un fichier `.verified` concorde (nom, taille, date), `unknown` sinon, `not_run` si aucune sauvegarde |
| `lastAttemptAt` / `lastAttemptOutcome` | dernière tentative (fichier d'état), `null` avant la première après B2 |
| `scheduleHourLocal` / `scheduleTimezone` | `BACKUP_HOUR` et le fuseau du planificateur (variable `TZ`) |

Un client écrit seulement si `enabled`, `integrityCheck == "ok"` et `ageSeconds <= 26 h`.

**Après le déploiement de B2**, la sauvegarde existante n'a pas de fichier `.verified` : `integrityCheck` reste
`unknown` (donc plexhubTMM refuse d'écrire) jusqu'à la prochaine sauvegarde quotidienne — ou, tout de suite :

```bash
python -m app.scripts.backup_db --verify-latest      # contrôle d'intégrité réel, puis écrit le .verified
```

Rien de tout cela n'est dans la base : une restauration (`restore_db`) ne fait pas régresser l'état. Voir aussi
`docs/ops-backup-restore.md`.

## 7. Réglages

| Variable | Défaut | Rôle |
|---|---|---|
| `METADATA_RATE_LIMIT_PER_MINUTE` | 300 | requêtes/minute/clé, toutes routes ; `<= 0` désactive |
| `METADATA_WRITE_RATE_LIMIT_PER_MINUTE` | 90 | idem pour PATCH, lock, revert |
| `METADATA_REQUEST_TIMEOUT_SECONDS` | 15 | échéance **avant** le `COMMIT`, bornée 5..28 → 503 `database_busy` |
| `BACKGROUND_JOBS_ENABLED` | `true` | `false` : instance de test sans tâche de fond (§ 8) |

Limite de corps : 256 Kio sous `/api/metadata/v1/` (413 au-delà). Sur le homelab, un réglage doit être ajouté sous
`environment:` du compose (le `.env` n'y est pas injecté). État process-local (limiteurs) : ne jamais lancer
`uvicorn --workers N>1`.

## 8. Instance de test jetable (plexhubTMM)

`BACKGROUND_JOBS_ENABLED=false` : l'instance démarre **sans** élection `fcntl` (donc aussi sous Windows), sans
planificateur (ni synchro planifiée, ni sauvegarde, ni contrôle de santé), sans pipeline initial, sans worker de
téléchargement ni reprise Plex ; `/api/health` renvoie `isMaster: false`. `POST /api/accounts` lance quand même
la synchronisation du compte créé.

Environnement vidé recommandé : `DATA_DIR`, `LOG_DIR`, `BACKUP_DIR`, la clé maître de test ; **aucune** variable
`XTREAM_*` (elles déclencheraient l'auto-provision). Lancer depuis un `git archive` dans un dossier temporaire sans
`.env` parent (`load_dotenv()` remonte les dossiers).

Faux fournisseur Xtream — sous-ensemble appelé par la synchronisation (`GET {base}:{port}/player_api.php`) :

| Appel | Utilité | Réponse minimale |
|---|---|---|
| sans `action` | création du compte | `{"user_info": {"status": "Active", "exp_date": "2000000000", "max_connections": "2", "allowed_output_formats": ["m3u8","ts"]}, "server_info": {"url": "127.0.0.1", "https_port": "443"}}` (un objet, sinon 400) |
| `get_vod_categories` / `get_series_categories` / `get_live_categories` | catégories | `[{"category_id": "10", "category_name": "Films"}]`, `[…]`, `[]` |
| `get_vod_streams` | films | `[{"stream_id": 1001, "name": "Test Movie (2020)", "category_id": "10", "container_extension": "mp4"}]` |
| `get_vod_info&vod_id=` | détail film (tolérant : `{}` accepté) | `{"info": {"tmdb_id": "603", "duration_secs": 5400}, "movie_data": {"stream_id": 1001}}` |
| `get_series` | séries | `[{"series_id": 2001, "name": "Test Show (2019)", "category_id": "20"}]` |
| `get_series_info&series_id=` | épisodes | `{"info": {}, "episodes": {"1": [{"id": "30001", "episode_num": 1, "title": "Pilot", "container_extension": "mkv"}, {"id": "30002", "episode_num": 2, "title": "Second", "container_extension": "mkv"}]}}` |
| `get_live_streams` | chaînes | `[]` |

Indispensables : `stream_id`, `series_id`, `id` d'épisode ; tout le reste est optionnel. Clés produites :
`vod_<stream_id>.<ext>` (film), `series_<series_id>`, `ep_<id>.<ext>` (épisode ; saison = clé de `episodes`),
`server_id = xtream_<md5(baseUrl+username)[:8]>`. `127.0.0.1` est accepté, aucun réglage SSRF n'est nécessaire.
Ne lancer qu'**une** synchronisation (celle de la création) et attendre sa fin via `GET /api/sync/jobs` ou le
contenu des routes de lecture.

## 9. Diagnostic

| Symptôme | Cause probable | Action |
|---|---|---|
| 401 `invalid_api_key` | clé absente, inconnue, révoquée ou expirée | recréer la clé (§ 1) |
| 403 `insufficient_scope` | scope manquant, clé sans scope, ou clé à scopes sur une route hors périmètre | vérifier les scopes de la clé |
| 412 `revision_mismatch` | l'élément a changé depuis la lecture | relire (`currentRevision`), replanifier |
| 428 `precondition_required` | `If-Match` ou `Idempotency-Key` absent (champ `header`) | les ajouter |
| 409 `regroup_not_allowed` | l'écriture changerait le regroupement (`unificationId`) | relire l'avertissement puis `allowRegroup: true` |
| 409 `idempotency_key_reused` | même clé d'idempotence, autre corps | nouvelle valeur |
| 400 `invalid_field` | valeur refusée (champ `field`) | corriger ; `GET /fields` donne champs et bornes |
| 429 `rate_limited` | débit dépassé | attendre `Retry-After` |
| 503 `database_busy` | verrou SQLite plus long que l'échéance, rien d'écrit | réessayer avec la même `Idempotency-Key` |
| plusieurs minutes de 503 | synchro/validation qui tient le verrou d'écriture | voir `docs/ops-backup-restore.md`, journaux `plexhub.db.immediate` |

Journaux : ligne INFO `metadata api: route=… outcome=… N ms` (jamais de titre, de clé ni de corps).
