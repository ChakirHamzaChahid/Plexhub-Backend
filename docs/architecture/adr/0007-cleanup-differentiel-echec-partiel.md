# ADR 0007 — Le nettoyage différentiel ne touche jamais une catégorie dont le fetch a échoué

Statut : accepté (2026-10-03, Lot 6 E-b, FR-366).

## Contexte
`_fetch_streams_by_categories` (mode whitelist) avalait l'échec de chaque catégorie et renvoyait `[]` pour elle.
`all_*_keys` restait non vide, `differential_cleanup_filtered` / `_live_filtered` supprimait alors TOUS les éléments
de la catégorie en échec (elle figurait dans `synced_cat_ids`). Une panne réseau passagère vidait un pan du catalogue.

## Décision
Option (a) : la fonction renvoie `(streams, failed_category_ids)` ; `_cleanup_category_ids` retire les catégories en
échec du périmètre de nettoyage (VOD, séries, live) et journalise celles sautées (WARNING).
Option (b) (sauter tout le type si un échec) rejetée : une catégorie instable gèlerait le retrait des contenus
disparus de toutes les autres. Un échec TOTAL ne produit aucune clé, donc la garde `if all_*_keys` saute déjà le nettoyage.
Les modes `all` / `blacklist` récupèrent en un seul appel : échec = exception = liste vide = garde existante.

## Conséquences
Contrat interne modifié (tuple). Tests : `tests/test_sync_cleanup_partial_failure.py`.
