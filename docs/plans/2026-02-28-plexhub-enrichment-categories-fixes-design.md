# PlexHub Backend — Enrichment & Category Filtering Improvements

**Date:** 2026-02-28
**Status:** Approved

## Contexte

PlexHub Backend synchronise les catalogues Xtream IPTV et les enrichit avec les métadonnées TMDB. Plusieurs problèmes et limitations ont été identifiés :

**Problèmes à corriger :**
1. ❌ Enrichissement incomplet : ne se déclenche pas quand TMDB ID ou IMDB ID manque individuellement
2. ❌ Relation série-épisodes : l'API `/episodes?parent_rating_key=series_XXX` retourne 0 résultats
3. ❌ Format IMDB ID : garantir le préfixe "tt" dans `unification_id`

**Nouvelles fonctionnalités :**
4. ✅ Filtrage de catégories par compte (whitelist/blacklist)
5. ✅ Endpoint pour lister et gérer les catégories Xtream
6. ✅ Skip des catégories non désirées pendant le sync (ne pas polluer la DB)
7. ✅ Conservation avec marquage des médias hors catégories autorisées

---

## Architecture Choisie : Approche Robuste avec Table Dédiée

### Pourquoi cette approche ?

- ✅ Catalogue potentiellement énorme (144K+ médias)
- ✅ Cache des noms de catégories → meilleure UX Android
- ✅ Évite les appels répétés à l'API Xtream
- ✅ Architecture propre et maintenable

---

## 1. Modifications du Schéma de Base de Données

### Nouvelle table `xtream_categories`

Stocke les catégories disponibles et leur configuration de filtrage par compte.

```sql
CREATE TABLE xtream_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    category_id TEXT NOT NULL,
    category_type TEXT NOT NULL,  -- "vod" ou "series"
    category_name TEXT NOT NULL,
    is_allowed BOOLEAN NOT NULL DEFAULT TRUE,
    last_fetched_at BIGINT NOT NULL,

    UNIQUE(account_id, category_id, category_type),
    FOREIGN KEY(account_id) REFERENCES xtream_accounts(id) ON DELETE CASCADE
);

CREATE INDEX idx_categories_account ON xtream_categories(account_id, is_allowed);
CREATE INDEX idx_categories_type ON xtream_categories(category_type, is_allowed);
```

**Colonnes :**
- `account_id` : Lien vers le compte Xtream (clé étrangère)
- `category_id` : ID de la catégorie Xtream (ex: "1", "44", "444")
- `category_type` : Type de contenu (`"vod"` ou `"series"`)
- `category_name` : Nom lisible pour l'UI (ex: "Action", "Horror", "Documentaires")
- `is_allowed` :
  - `TRUE` = catégorie autorisée (whitelist)
  - `FALSE` = catégorie bloquée (blacklist)
- `last_fetched_at` : Timestamp du dernier fetch depuis Xtream

---

### Modification table `xtream_accounts`

**Ajouter 1 colonne :**
```sql
ALTER TABLE xtream_accounts ADD COLUMN category_filter_mode TEXT NOT NULL DEFAULT 'all';
```

**Valeurs possibles :**
- `"all"` : Synchroniser tout (mode par défaut, pas de filtrage)
- `"whitelist"` : Synchroniser UNIQUEMENT les catégories avec `is_allowed = TRUE`
- `"blacklist"` : Synchroniser tout SAUF les catégories avec `is_allowed = FALSE`

---

### Modification table `media`

**Ajouter 1 colonne :**
```sql
ALTER TABLE media ADD COLUMN is_in_allowed_categories BOOLEAN NOT NULL DEFAULT TRUE;
```

**Comportement :**
- `TRUE` : Le média fait partie des catégories autorisées → visible dans l'API
- `FALSE` : Le média est hors catégories autorisées → conservé mais masqué

**Index :**
```sql
CREATE INDEX idx_media_category_visibility ON media(is_in_allowed_categories, type, added_at);
```

---

### Modification table `enrichment_queue`

**Ajouter 2 colonnes pour tracking des IDs existants :**
```sql
ALTER TABLE enrichment_queue ADD COLUMN existing_tmdb_id TEXT;
ALTER TABLE enrichment_queue ADD COLUMN existing_imdb_id TEXT;
```

**Usage :** Permet de savoir quels IDs sont déjà présents et d'optimiser les appels API.

---

## 2. Nouveaux Endpoints API

### `GET /api/accounts/{account_id}/categories`

Récupère les catégories disponibles pour un compte.

**Paramètres query :**
- `type` (optionnel) : `"vod"` ou `"series"` pour filtrer par type
- `refresh` (optionnel) : `true` pour forcer le fetch depuis Xtream

**Réponse 200 :**
```json
{
  "vod": [
    {
      "categoryId": "1",
      "categoryName": "Action",
      "categoryType": "vod",
      "isAllowed": true,
      "lastFetchedAt": 1772240521981
    },
    {
      "categoryId": "44",
      "categoryName": "Horror",
      "categoryType": "vod",
      "isAllowed": false,
      "lastFetchedAt": 1772240521981
    }
  ],
  "series": [
    {
      "categoryId": "2",
      "categoryName": "Drama",
      "categoryType": "series",
      "isAllowed": true,
      "lastFetchedAt": 1772240521981
    }
  ],
  "filterMode": "whitelist"
}
```

**Comportement :**
1. Charge les catégories depuis la table `xtream_categories`
2. Si `refresh=true` ou si cache vide/expiré (> 24h) :
   - Appelle `get_vod_categories()` et `get_series_categories()` de l'API Xtream
   - Upsert dans `xtream_categories` (conserve les préférences `is_allowed`)

---

### `PUT /api/accounts/{account_id}/categories`

Met à jour la configuration de filtrage des catégories.

**Request body :**
```json
{
  "filterMode": "whitelist",
  "categories": [
    {
      "categoryId": "1",
      "categoryType": "vod",
      "isAllowed": true
    },
    {
      "categoryId": "44",
      "categoryType": "vod",
      "isAllowed": false
    },
    {
      "categoryId": "2",
      "categoryType": "series",
      "isAllowed": true
    }
  ]
}
```

**Réponse 200 :** Configuration mise à jour avec succès

**Comportement :**
1. Met à jour `category_filter_mode` dans `xtream_accounts`
2. Met à jour `is_allowed` pour chaque catégorie dans `xtream_categories`
3. Appelle `_update_media_category_visibility(account_id)` pour marquer les médias existants
4. **Ne déclenche PAS de sync automatique** (l'utilisateur doit le faire manuellement via `/api/sync/xtream`)

---

### `POST /api/accounts/{account_id}/categories/refresh`

Force la récupération des catégories depuis le serveur Xtream.

**Réponse 202 :** Job de refresh lancé en arrière-plan

**Comportement :**
1. Appelle `get_vod_categories()` et `get_series_categories()`
2. Upsert dans `xtream_categories` avec `last_fetched_at = now()`
3. **Conserve les préférences `is_allowed` existantes**

---

## 3. Modifications des Endpoints Existants

### `GET /api/media/movies` et `GET /api/media/shows`

**Nouveau paramètre query :**
- `include_filtered` (optionnel, défaut `false`) :
  - `false` : Retourne uniquement les médias avec `is_in_allowed_categories = TRUE`
  - `true` : Retourne tous les médias (y compris ceux hors catégories autorisées)

**Changement dans `media_service.get_media_list()` :**
```python
async def get_media_list(
    self,
    db: AsyncSession,
    media_type: str,
    include_filtered: bool = False,  # NOUVEAU
    # ... autres paramètres
):
    query = select(Media).where(Media.type == media_type)

    # Filtrer par visibilité des catégories
    if not include_filtered:
        query = query.where(Media.is_in_allowed_categories == True)

    # ... reste du code
```

---

### `GET /api/media/episodes`

**Corrections et améliorations :**

**Nouveau paramètre query :**
- `series_rating_key` (optionnel) : Pour récupérer tous les épisodes d'une série

**Comportement intelligent :**
- Si `parent_rating_key` commence par `"series_"` → filtre par `grandparent_rating_key`
- Si `parent_rating_key` commence par `"season_"` → filtre par `parent_rating_key`
- Si `series_rating_key` fourni → filtre par `grandparent_rating_key`

**Exemples d'utilisation :**
```http
# Tous les épisodes de la série "Dope Thief"
GET /api/media/episodes?series_rating_key=series_6336

# OU (détection automatique du préfixe "series_")
GET /api/media/episodes?parent_rating_key=series_6336

# Épisodes de la saison 1 uniquement
GET /api/media/episodes?parent_rating_key=season_6336_1
```

**Code du service :**
```python
async def get_media_list(
    self,
    db: AsyncSession,
    media_type: str,
    parent_rating_key: Optional[str] = None,
    series_rating_key: Optional[str] = None,  # NOUVEAU
    # ... autres paramètres
):
    query = select(Media).where(Media.type == media_type)

    # Support série OU saison
    if series_rating_key:
        query = query.where(Media.grandparent_rating_key == series_rating_key)
    elif parent_rating_key:
        if parent_rating_key.startswith("series_"):
            # Détection auto : c'est une série, pas une saison
            query = query.where(Media.grandparent_rating_key == parent_rating_key)
        else:
            # C'est une saison
            query = query.where(Media.parent_rating_key == parent_rating_key)

    # ... reste du code
```

---

## 4. Modifications du Sync Worker

### Nouvelle fonction de chargement de configuration

```python
async def _load_category_config(db, account_id: str) -> dict:
    """Charge la configuration de filtrage des catégories depuis la DB."""

    # Récupérer le mode de filtrage
    result = await db.execute(
        select(XtreamAccount.category_filter_mode)
        .where(XtreamAccount.id == account_id)
    )
    mode = result.scalar() or "all"

    # Récupérer les catégories autorisées/bloquées
    result = await db.execute(
        select(XtreamCategory)
        .where(
            XtreamCategory.account_id == account_id,
            XtreamCategory.is_allowed == True
        )
    )
    allowed_categories = result.scalars().all()

    result = await db.execute(
        select(XtreamCategory)
        .where(
            XtreamCategory.account_id == account_id,
            XtreamCategory.is_allowed == False
        )
    )
    blocked_categories = result.scalars().all()

    # Construire les sets d'IDs par type
    vod_allowed = {c.category_id for c in allowed_categories if c.category_type == "vod"}
    vod_blocked = {c.category_id for c in blocked_categories if c.category_type == "vod"}
    series_allowed = {c.category_id for c in allowed_categories if c.category_type == "series"}
    series_blocked = {c.category_id for c in blocked_categories if c.category_type == "series"}

    return {
        "mode": mode,
        "vod_allowed": vod_allowed,
        "vod_blocked": vod_blocked,
        "series_allowed": series_allowed,
        "series_blocked": series_blocked,
    }
```

---

### Nouvelle fonction de vérification de catégorie

```python
def _should_sync_category(category_id: str, category_type: str, config: dict) -> bool:
    """Détermine si une catégorie doit être synchronisée."""

    if config["mode"] == "all":
        return True

    elif config["mode"] == "whitelist":
        allowed_set = config[f"{category_type}_allowed"]
        return category_id in allowed_set

    elif config["mode"] == "blacklist":
        blocked_set = config[f"{category_type}_blocked"]
        return category_id not in blocked_set

    # Défaut : sync
    return True
```

---

### Modification du flux de synchronisation VOD

**Avant :**
```python
vod_streams = await xtream_service.get_vod_streams(account)
for dto in vod_streams:
    media_row = map_vod_to_media(dto, account_id, index)
    await upsert_media_batch(db, [media_row])
```

**Après :**
```python
vod_streams = await xtream_service.get_vod_streams(account)
category_config = await _load_category_config(db, account_id)

synced_vod = []
for dto in vod_streams:
    category_id = str(dto.get("category_id", ""))

    # SKIP si catégorie non autorisée
    if not _should_sync_category(category_id, "vod", category_config):
        logger.debug(f"Skipping VOD {dto.get('name')} (category {category_id} not allowed)")
        continue

    # Mapper et marquer comme visible
    media_row = map_vod_to_media(dto, account_id, index)
    media_row["is_in_allowed_categories"] = True
    synced_vod.append(media_row)

# Upsert en batch
await upsert_media_batch(db, synced_vod)
```

**Même logique pour les séries.**

---

### Nouvelle fonction de marquage des médias existants

```python
async def _update_media_category_visibility(db, account_id: str):
    """
    Met à jour is_in_allowed_categories pour tous les médias d'un compte
    suite à un changement de configuration de catégories.
    """

    config = await _load_category_config(db, account_id)
    server_id = f"xtream_{account_id}"

    if config["mode"] == "all":
        # Tout est autorisé, marquer tous les médias comme visibles
        await db.execute(
            update(Media)
            .where(Media.server_id == server_id)
            .values(is_in_allowed_categories=True)
        )
        return

    # Mode whitelist
    if config["mode"] == "whitelist":
        # Marquer comme visibles uniquement les médias des catégories autorisées

        # VOD
        await db.execute(
            update(Media)
            .where(
                Media.server_id == server_id,
                Media.type == "movie",
                Media.filter.in_(config["vod_allowed"])
            )
            .values(is_in_allowed_categories=True)
        )
        await db.execute(
            update(Media)
            .where(
                Media.server_id == server_id,
                Media.type == "movie",
                Media.filter.notin_(config["vod_allowed"])
            )
            .values(is_in_allowed_categories=False)
        )

        # Series (même logique)
        await db.execute(
            update(Media)
            .where(
                Media.server_id == server_id,
                Media.type.in_(["show", "episode"]),
                Media.filter.in_(config["series_allowed"])
            )
            .values(is_in_allowed_categories=True)
        )
        await db.execute(
            update(Media)
            .where(
                Media.server_id == server_id,
                Media.type.in_(["show", "episode"]),
                Media.filter.notin_(config["series_allowed"])
            )
            .values(is_in_allowed_categories=False)
        )

    # Mode blacklist (logique inverse)
    elif config["mode"] == "blacklist":
        # Marquer comme non visibles uniquement les médias des catégories bloquées

        # VOD
        await db.execute(
            update(Media)
            .where(
                Media.server_id == server_id,
                Media.type == "movie",
                Media.filter.in_(config["vod_blocked"])
            )
            .values(is_in_allowed_categories=False)
        )
        await db.execute(
            update(Media)
            .where(
                Media.server_id == server_id,
                Media.type == "movie",
                Media.filter.notin_(config["vod_blocked"])
            )
            .values(is_in_allowed_categories=True)
        )

        # Series (même logique)
        await db.execute(
            update(Media)
            .where(
                Media.server_id == server_id,
                Media.type.in_(["show", "episode"]),
                Media.filter.in_(config["series_blocked"])
            )
            .values(is_in_allowed_categories=False)
        )
        await db.execute(
            update(Media)
            .where(
                Media.server_id == server_id,
                Media.type.in_(["show", "episode"]),
                Media.filter.notin_(config["series_blocked"])
            )
            .values(is_in_allowed_categories=True)
        )
```

**Appelée automatiquement après `PUT /api/accounts/{account_id}/categories`.**

---

### Modification du nettoyage différentiel

**Comportement actuel :**
- Compare tous les `rating_key` de l'API vs la base
- Supprime les médias absents

**Nouveau comportement :**
- Compare UNIQUEMENT les médias des catégories autorisées
- Les médias hors catégories sont conservés avec `is_in_allowed_categories = FALSE`

```python
async def differential_cleanup(
    db, server_id: str, category_config: dict, api_rating_keys: set[str],
):
    """
    Supprime les médias qui ne sont plus dans l'API Xtream
    MAIS UNIQUEMENT pour les catégories actuellement synchronisées.
    """

    # Ne pas toucher aux médias hors catégories autorisées
    # Ils sont déjà marqués is_in_allowed_categories = FALSE

    # Récupérer les rating_keys existants des catégories actuellement synced
    result = await db.execute(
        select(Media.rating_key, Media.filter).where(
            Media.server_id == server_id,
            Media.is_in_allowed_categories == True,  # Uniquement les visibles
        )
    )
    existing_items = {row[0]: row[1] for row in result}

    # Identifier les médias à supprimer (présents en DB mais absents de l'API)
    stale_keys = set(existing_items.keys()) - api_rating_keys

    if stale_keys:
        await db.execute(
            delete(Media).where(
                Media.rating_key.in_(stale_keys),
                Media.server_id == server_id,
            )
        )
        logger.info(f"Removed {len(stale_keys)} stale items from {server_id}")
```

---

## 5. Modifications de l'Enrichment Worker

### Problème actuel

Le worker enrichit UNIQUEMENT si `tmdb_id` ET `imdb_id` sont tous les deux absents.

**Nouveau comportement :**
- Enrichir si `tmdb_id` est absent **OU** si `imdb_id` est absent
- Objectif : maximiser la complétude des métadonnées

---

### Modification de l'insertion dans `enrichment_queue`

**Dans `sync_worker.enqueue_for_enrichment()` :**

```python
async def enqueue_for_enrichment(db, rows: list[dict]):
    """
    Insère les médias dans la queue d'enrichissement
    si AU MOINS UN des deux IDs (TMDB ou IMDB) manque.
    """
    for row in rows:
        if row["type"] not in ("movie", "show"):
            continue

        has_tmdb = bool(row.get("tmdb_id"))
        has_imdb = bool(row.get("imdb_id"))

        # Skip si les deux IDs sont présents
        if has_tmdb and has_imdb:
            continue

        # Au moins un ID manque → enqueue
        stmt = sqlite_upsert(EnrichmentQueue).values(
            rating_key=row["rating_key"],
            server_id=row["server_id"],
            media_type=row["type"],
            title=row["title"],
            year=row.get("year"),
            existing_tmdb_id=row.get("tmdb_id"),  # NOUVEAU
            existing_imdb_id=row.get("imdb_id"),  # NOUVEAU
            status="pending",
            attempts=0,
            created_at=now_ms(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["rating_key", "server_id"],
            set_={
                "status": "pending",
                "existing_tmdb_id": row.get("tmdb_id"),
                "existing_imdb_id": row.get("imdb_id"),
            },
        )
        await db.execute(stmt)
```

---

### Modification de `_enrich_vod_item()`

**Scénarios gérés :**

1. **Aucun ID** : get_vod_info() → TMDB search → external_ids
2. **TMDB présent, IMDB absent** : external_ids(tmdb_id) directement (1 appel API seulement)
3. **IMDB présent, TMDB absent** : get_vod_info() → TMDB search (skip external_ids)
4. **Les deux présents** : skip (status = "skipped")

```python
async def _enrich_vod_item(db, item, account):
    """
    Enrichit un film VOD avec focus sur la complétude TMDB + IMDB.
    Optimise les appels API en fonction des IDs déjà présents.
    """
    used = 0
    tmdb_id = item.existing_tmdb_id
    imdb_id = item.existing_imdb_id
    confidence = None

    # Scénario 4 : Les deux IDs présents, skip
    if tmdb_id and imdb_id:
        item.status = "skipped"
        item.processed_at = now_ms()
        return 0

    # Scénario 2 : TMDB présent, IMDB absent
    if tmdb_id and not imdb_id:
        try:
            ext_ids = await tmdb_service.get_movie_external_ids(int(tmdb_id))
            imdb_id = ext_ids.get("imdb_id")
            used += 1
            confidence = 1.0  # TMDB ID était connu
            logger.info(f"Enriched {item.rating_key}: got IMDB from existing TMDB")
        except Exception as e:
            logger.debug(f"Failed to get external_ids for tmdb_id {tmdb_id}: {e}")

    # Scénario 1 & 3 : TMDB manque
    elif not tmdb_id:
        # Step 1: Tenter get_vod_info (gratuit, peut contenir tmdb_id)
        try:
            vod_id_str = item.rating_key.split("_")[1].split(".")[0]
            vod_id = int(vod_id_str)
            vod_info = await xtream_service.get_vod_info(account, vod_id)
            info = vod_info.get("info") or {}
            raw_tmdb = info.get("tmdb_id")

            if raw_tmdb and str(raw_tmdb).strip():
                tmdb_id = str(int(raw_tmdb))

                # Récupérer IMDB si on ne l'a pas déjà
                if not imdb_id:
                    ext_ids = await tmdb_service.get_movie_external_ids(int(tmdb_id))
                    imdb_id = ext_ids.get("imdb_id")
                    used += 1

                confidence = 1.0
                logger.info(f"Enriched {item.rating_key}: got TMDB from Xtream")
        except Exception as e:
            logger.debug(f"Xtream vod_info failed for {item.rating_key}: {e}")

        # Step 2: Fallback TMDB search si toujours pas de tmdb_id
        if not tmdb_id and tmdb_service.is_configured:
            try:
                match = await tmdb_service.search_movie(item.title, item.year)
                if match and match.confidence >= 0.85:
                    tmdb_id = str(match.tmdb_id)

                    # Récupérer IMDB si on ne l'a pas déjà
                    if not imdb_id:
                        ext_ids = await tmdb_service.get_movie_external_ids(int(tmdb_id))
                        imdb_id = ext_ids.get("imdb_id")
                        used += 2  # search + external_ids
                    else:
                        used += 1  # search seulement

                    confidence = match.confidence
                    logger.info(f"Enriched {item.rating_key}: got TMDB from search")
            except Exception as e:
                logger.debug(f"TMDB search failed for {item.title}: {e}")

    # Update media si on a récupéré au moins un ID
    if tmdb_id or imdb_id:
        # Calculer unification_id (priorité IMDB)
        new_unification = calculate_unification_id(
            item.title, item.year, imdb_id, tmdb_id
        )

        await db.execute(
            update(Media)
            .where(
                Media.rating_key == item.rating_key,
                Media.server_id == item.server_id,
            )
            .values(
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                unification_id=new_unification,
                history_group_key=new_unification,
                tmdb_match_confidence=confidence,
            )
        )
        item.status = "done"
    else:
        item.status = "skipped"

    item.attempts += 1
    item.processed_at = now_ms()
    return used
```

**Même logique pour `_enrich_series_item()` avec `search_tv()` et `get_tv_external_ids()`.**

---

### Garantie du préfixe "tt" pour IMDB ID

**Dans `tmdb_service.py` :**

```python
async def get_movie_external_ids(self, tmdb_id: int) -> dict:
    """Récupère les IDs externes depuis TMDB."""
    client = await self._get_client()
    resp = await client.get(f"{self.BASE_URL}/movie/{tmdb_id}/external_ids")
    resp.raise_for_status()
    data = resp.json()

    # Garantir le préfixe "tt" pour IMDB ID
    imdb_id = data.get("imdb_id")
    if imdb_id and not imdb_id.startswith("tt"):
        imdb_id = f"tt{imdb_id}"

    return {
        "imdb_id": imdb_id,
        "tvdb_id": data.get("tvdb_id"),
        "facebook_id": data.get("facebook_id"),
        "instagram_id": data.get("instagram_id"),
        "twitter_id": data.get("twitter_id"),
    }

async def get_tv_external_ids(self, tmdb_id: int) -> dict:
    """Récupère les IDs externes pour séries TV."""
    client = await self._get_client()
    resp = await client.get(f"{self.BASE_URL}/tv/{tmdb_id}/external_ids")
    resp.raise_for_status()
    data = resp.json()

    # Garantir le préfixe "tt" pour IMDB ID
    imdb_id = data.get("imdb_id")
    if imdb_id and not imdb_id.startswith("tt"):
        imdb_id = f"tt{imdb_id}"

    return {
        "imdb_id": imdb_id,
        "tvdb_id": data.get("tvdb_id"),
        "freebase_id": data.get("freebase_id"),
        "tvrage_id": data.get("tvrage_id"),
    }
```

---

### Correction de `calculate_unification_id()`

**Dans `app/utils/unification.py` :**

```python
def calculate_unification_id(
    title: str,
    year: int | None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
) -> str:
    """
    Calcule l'ID d'unification pour agréger les médias cross-serveur.
    Priorité: imdb > tmdb > title_year.
    """

    # Priorité 1 : IMDB ID
    if imdb_id:
        # Garantir le préfixe "tt"
        if not imdb_id.startswith("tt"):
            imdb_id = f"tt{imdb_id}"
        return f"imdb://{imdb_id}"

    # Priorité 2 : TMDB ID
    if tmdb_id:
        return f"tmdb://{tmdb_id}"

    # Fallback : normalized title + year
    if title == "Unknown":
        return ""

    normalized = normalize_for_sorting(title).lower()
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = re.sub(r"[^a-z0-9_]", "", normalized)

    return f"title_{normalized}_{year}" if year else f"title_{normalized}"
```

---

## 6. Résumé des Changements

### Database

| Table | Action | Description |
|-------|--------|-------------|
| `xtream_categories` | CREATE | Nouvelle table pour gérer les catégories par compte |
| `xtream_accounts` | ALTER | Ajouter `category_filter_mode` (all/whitelist/blacklist) |
| `media` | ALTER | Ajouter `is_in_allowed_categories` (visibilité) |
| `enrichment_queue` | ALTER | Ajouter `existing_tmdb_id` et `existing_imdb_id` |

### API Endpoints

| Endpoint | Type | Description |
|----------|------|-------------|
| `GET /api/accounts/{id}/categories` | NOUVEAU | Lister les catégories disponibles |
| `PUT /api/accounts/{id}/categories` | NOUVEAU | Configurer le filtrage des catégories |
| `POST /api/accounts/{id}/categories/refresh` | NOUVEAU | Forcer le refresh des catégories depuis Xtream |
| `GET /api/media/movies` | MODIFIÉ | Ajouter `include_filtered` param |
| `GET /api/media/shows` | MODIFIÉ | Ajouter `include_filtered` param |
| `GET /api/media/episodes` | MODIFIÉ | Ajouter `series_rating_key` param + détection auto |

### Workers

| Worker | Modification | Description |
|--------|--------------|-------------|
| `sync_worker` | MODIFIÉ | Filtrage des catégories + skip pendant le sync |
| `sync_worker` | NOUVEAU | `_update_media_category_visibility()` pour marquage |
| `enrichment_worker` | MODIFIÉ | Enrichir si TMDB OU IMDB manque (pas les deux requis) |
| `enrichment_worker` | MODIFIÉ | Optimisation des appels API selon IDs existants |

### Services

| Service | Modification | Description |
|---------|--------------|-------------|
| `tmdb_service` | MODIFIÉ | Garantir préfixe "tt" dans `get_*_external_ids()` |
| `media_service` | MODIFIÉ | Support `series_rating_key` + détection auto "series_" |

### Utilities

| Utility | Modification | Description |
|---------|--------------|-------------|
| `unification.py` | MODIFIÉ | Garantir préfixe "tt" dans `calculate_unification_id()` |

---

## 7. Flux d'Utilisation Complet

### Scénario 1 : Configuration initiale des catégories

```
1. Android → GET /api/accounts/{id}/categories?refresh=true
   Backend → Fetch categories depuis Xtream API
   Backend → Insert/update dans xtream_categories (toutes is_allowed = TRUE par défaut)
   Backend → Retourne la liste complète

2. Android affiche checkboxes pour chaque catégorie

3. User décoche "Horror" et "Adult"

4. Android → PUT /api/accounts/{id}/categories
   Body: {
     "filterMode": "blacklist",
     "categories": [
       {"categoryId": "44", "categoryType": "vod", "isAllowed": false},
       {"categoryId": "18", "categoryType": "vod", "isAllowed": false}
     ]
   }
   Backend → Update xtream_accounts.category_filter_mode = "blacklist"
   Backend → Update is_allowed dans xtream_categories
   Backend → Appelle _update_media_category_visibility()
   Backend → Marque les médias "Horror" et "Adult" avec is_in_allowed_categories = FALSE

5. Android → POST /api/sync/xtream
   Body: {"accountId": "05fd75e9"}
   Backend → Sync uniquement les catégories autorisées
   Backend → Skip les films/séries "Horror" et "Adult" (ne les insère pas en DB)
```

---

### Scénario 2 : Enrichissement d'une série sans IDs

```
Série "Dope Thief" en DB :
- tmdb_id = null
- imdb_id = null
- title = "Dope Thief"
- year = null

1. Sync Worker → enqueue_for_enrichment()
   Insertion dans enrichment_queue:
   - existing_tmdb_id = null
   - existing_imdb_id = null
   - status = "pending"

2. Enrichment Worker → _enrich_series_item()
   a. Check : existing_tmdb_id AND existing_imdb_id → Non, on continue
   b. TMDB search_tv("Dope Thief", null)
   c. Match trouvé : tmdb_id = 246145, confidence = 0.92
   d. get_tv_external_ids(246145)
   e. Récupère imdb_id = "tt29623890"
   f. calculate_unification_id("Dope Thief", null, "tt29623890", "246145")
      → Retourne "imdb://tt29623890"
   g. UPDATE media SET
      tmdb_id = "246145",
      imdb_id = "tt29623890",
      unification_id = "imdb://tt29623890",
      history_group_key = "imdb://tt29623890",
      tmdb_match_confidence = 0.92
```

---

### Scénario 3 : Récupération des épisodes d'une série

```
Android → GET /api/media/episodes?parent_rating_key=series_6336&server_id=xtream_05fd75e9

Backend → media_service.get_media_list()
1. Détecte que "series_6336" commence par "series_"
2. Filtre avec WHERE grandparent_rating_key = "series_6336"
3. Retourne tous les épisodes de toutes les saisons

Réponse :
{
  "items": [
    {
      "ratingKey": "ep_12345.mkv",
      "type": "episode",
      "title": "Episode 1",
      "index": 1,
      "parentRatingKey": "season_6336_1",
      "parentIndex": 1,
      "grandparentRatingKey": "series_6336",
      "grandparentTitle": "Dope Thief",
      ...
    },
    ...
  ],
  "total": 12,
  "hasMore": false
}
```

---

## 8. Ordre d'Implémentation Recommandé

1. **Database migrations** (créer table + colonnes)
2. **Category management API** (GET/PUT/POST categories)
3. **Sync worker filtering** (filtrage pendant sync + marquage)
4. **Media service improvements** (fix episodes query)
5. **Enrichment worker fixes** (logique TMDB/IMDB + préfixe "tt")
6. **Testing & validation**

---

## 9. Points d'Attention pour l'Implémentation

### Performance

- Les requêtes `_update_media_category_visibility()` peuvent être coûteuses sur de gros catalogues
- Solution : Exécuter en arrière-plan (asyncio.create_task) après `PUT /categories`
- Index `idx_media_category_visibility` critique pour les performances

### Rétrocompatibilité

- L'endpoint `/api/media/episodes?parent_rating_key=series_XXX` fonctionnera grâce à la détection auto
- Les apps Android existantes n'ont pas besoin de changement immédiat

### Edge Cases

- Catégorie supprimée par le fournisseur IPTV → conservée en DB mais marquée obsolète
- Changement fréquent de filtres → pas de suppression, juste marquage (conservation)
- TMDB/IMDB IDs invalides → skip gracefully, logger l'erreur

### Limites TMDB API

- 50 requêtes/seconde max (géré par `asyncio.sleep(0.03)`)
- Quota journalier configuré via `ENRICHMENT_DAILY_LIMIT`
- Optimisation : skip les appels si un ID existe déjà

---

**Design approuvé le 2026-02-28**
