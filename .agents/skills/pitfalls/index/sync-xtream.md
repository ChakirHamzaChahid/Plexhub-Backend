# Index des pièges — sync-xtream

> Généré par `python .claude/tools/pitfalls-index.py` depuis `.claude/skills/pitfalls/references/sync-xtream.md` : un titre par piège (son segment en gras). Ne pas éditer ici — ajouter le piège dans `references/`, puis relancer le script.

## sync-xtream — Synchro Xtream, EPG, pannes fournisseur et tagging (4)

Texte complet : `.claude/skills/pitfalls/references/sync-xtream.md`

- [#15] : **Tagging adulte = films seulement
- [#20] : **Panne fournisseur (§5.13) — ne JAMAIS « simplifier » le masquage en réutilisant `is_active` ou `is_broken`.** `is_active` est un interrupteur global lu par `plex_generator/source.py`, `sync_worker`, `/dav` et `download_service`
- [#22] : **Durée d'épisode (v1.10.8 / v1.10.9) — une durée RAPPORTÉE par une source est toujours respectée**, même sous 5 min : un panel qui sert un fichier TRONQUÉ le décrit correctement (MAO S01E01 : 125 000 ms exacts, vérifié à `ffprobe`)
- [#25] : **Synchro EPG en masse / EPG (Lot 10-D, ADR 0006) — six règles à ne pas défaire.** (a) **Toute écriture d'`epg_entries` est un upsert sur `(server_id, stream_id, start_time)`** — jamais un `db.add` : l'unique `uix_epg_dedup` n'existait PAS en prod avant la migration 028 (la 006 ne le créait pas et `create_all` …
