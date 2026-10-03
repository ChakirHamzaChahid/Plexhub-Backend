# Rétro-spécification — PlexHub Backend

Spec **reconstruite à partir du code existant** : ce que le produit fait réellement,
preuves `fichier:ligne` à l'appui. Méthode : skill `retro-spec`, commande `/retro-spec`.

| Dossier | Contenu | Écrit par |
|---|---|---|
| `_genere/` | inventaire factuel (écrans, routes, endpoints, tables…) | `tools/retro-spec/inventaire.py` — ne pas éditer |
| `fonctionnel/` | carte des capacités + une fiche par capacité (US, FR-XX-###, règles, écarts) | session principale |
| `technique/` | C4, contrat d'API, navigation, sécurité, build, dette | session principale |
| `technique/db/` | schéma de données (base reconstruite vide) | `tools/retro-spec/schema-db.py` (tbls) — ne pas éditer |
Contrôle anti-dérive : `python tools/retro-spec/check.py` (preuves qui pointent dans le vide,
exigences en double ou sans preuve). Lancé par `/retro-spec check` et `/sync-context`.
