# ADR 0013 — Lot B4 : images en lecture, champs de scrape en écriture

- Statut : **accepté** (2026-10-26, lot B4)
- Plan source : contrat plexhubTMM `docs/17-contrat-b4-backend.md` (prolonge `docs/16-contrat-b2-backend.md`)
- Portée : `app/models/database.py`, `app/db/migrations.py` (032), `app/services/{metadata_columns,metadata_fields,
  metadata_plan,metadata_views,metadata_service}.py`, `app/models/metadata_schemas.py`, `docs/contracts/metadata-v1.openapi.json`
- ADR recoupés : 0010 (scopes), 0011 (révision, journal), 0012 (verrou étendu)

## D1 — Faits du code
1. `ItemSummary`/`ItemView` n'ont aucun champ image ; `resolved_thumb_url`/`resolved_art_url` (TMDB) existent mais ne
   sortent que par `/api/media/*`. `thumb_url`/`art_url` bruts sont des URL fournisseur (Xtream) ou des chemins Plex relatifs.
2. `METADATA_WRITABLE_COLUMNS` se déduit de `FIELD_COLUMNS` : un champ ajouté là est protégé par `match_locked` pour tous
   les écrivains automatiques (sync, NFO, enrichissement) sans autre changement ; `tests/test_metadata_lock_protection.py`
   le prouve colonne par colonne.
3. La révision est un SHA-256 des champs éditables ; tout champ qui entre dans `columns_to_flat` change toutes les révisions.
4. Aucun module hors API metadata ne connaissait `poster_url`/`fanart_url` comme colonnes (le NFO et le générateur Plex ont
   des attributs du même nom qui alimentent `resolved_*`).

## D2 — Décisions
### D2.1 Colonnes (migration 032), toutes `NULL`, sans `DEFAULT`, sans réécriture de ligne
`poster_url`, `fanart_url` (TEXT) ; `poster_gallery_json`, `fanart_gallery_json`, `available_trailers_json` (TEXT JSON) ;
`directors`, `writers`, `spoken_languages` (TEXT, CSV `", "` comme `genres`) ; `metacritic_rating`/`rt_rating` (REAL),
`metacritic_votes`/`rt_votes` (INTEGER). Patron de la migration 026 (`_column_exists`).
### D2.2 Lecture
`thumbUrl = safe(poster_url ?? resolved_thumb_url)`, `artUrl = safe(fanart_url ?? resolved_art_url)` sur la variante
**canonique**. Les colonnes brutes ne servent jamais de repli. `safe` n'accepte qu'une URL `https` avec hôte, sans
identifiants ni paramètre `token`/`X-Plex-Token`/`api_key`/`apikey`/`password`/`username` (insensible à la casse) ; la valeur
choisie est filtrée, **sans repli sur la source suivante** (une saisie utilisateur piégée ne laisse pas passer un autre lien
silencieusement). Calcul Python sur les lignes déjà chargées : aucune requête de plus. Hors `flat` : ni dans la révision
(piège 27d) ni dans le journal. Exposé dans `ItemSummary` et `ItemView.context` ; `ItemView.fields` rend la valeur stockée.
### D2.3 Écriture
Dix champs ajoutés à `FIELD_COLUMNS` (movie, show ; **pas** episode). URL : validateur `_https_url` existant (X10).
Galeries : ≤ 30, dédoublonnées, ordre conservé. Bandes-annonces : ≤ 20, `{name, url, isPrimary}`, au plus une primaire.
Listes CSV : bornes de `genres`, virgule interdite. Notes : forme `Rating` 0..10, hors `_RATING_FIELDS` (aucun effet sur
`display_rating`). JSON illisible en base ⇒ lu comme `[]`, jamais d'erreur.
### D2.4 Compatibilité
Additif : `SCHEMA_VERSION` inchangé. `REVISION_FORM` passe à 2 (le jeu de champs hashé change) : toutes les révisions
changent une fois ; `x-revision-form` du contrat machine suit.

## D3 — Alternatives écartées
- Tables dédiées pour galeries/bandes-annonces : jamais filtrées ni jointes, coût de migration et de journal inutile.
- Repli sur `thumb_url` brut : fuite possible d'URL à identifiants.
- Enrichissement TMDB remplaçant `poster_url` : contredit le but (une saisie doit tenir) ; retour à TMDB = envoyer `null`.

## D4 — Tests exigés
`tests/metadata_api/test_b4_images_and_scrape_fields.py`, `tests/test_migration_032.py`,
`tests/test_b4_columns_have_no_automatic_writer.py`, extension de `tests/test_metadata_lock_protection.py`,
régénération et test d'export du contrat OpenAPI.

## D5 — Conséquences
Un client B2 existant ignore les nouveaux champs ; ses caches indexés sur la révision se rafraîchissent une fois.
Les NFO/arborescences générés ne lisent pas les nouvelles colonnes (hors périmètre).
