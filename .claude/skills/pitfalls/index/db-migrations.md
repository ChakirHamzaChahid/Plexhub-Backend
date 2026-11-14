# Index des pièges — db-migrations

> Généré par `python .claude/tools/pitfalls-index.py` depuis `.claude/skills/pitfalls/references/db-migrations.md` : un titre par piège (son segment en gras). Ne pas éditer ici — ajouter le piège dans `references/`, puis relancer le script.

## db-migrations — DB, SQLite WAL, migrations et sauvegardes (4)

Texte complet : `.claude/skills/pitfalls/references/db-migrations.md`

- [#6] : **Migrations idempotentes** : tout DDL en `IF NOT EXISTS` / `ADD COLUMN` gardé
- [#7] : **`fcntl.flock` = POSIX** : l'élection master-worker (`main.py:226-227`) ne fonctionne que sous Linux/Docker (pas Windows natif)
- [#8] : **SQLite « database is locked »** : passer par `commit_with_retry` / `run_with_retry` (`db_retry.py:21`, délais 0.1/0.5/1.0 s au-dessus du **`busy_timeout=60000` (60 s)** + `connect_args timeout=60`, `database.py:15,90`)
- [#23] : **Sauvegarde / restauration SQLite (Lot 10-A) — trois règles à ne pas défaire.** (a) **Ne JAMAIS laisser un `-wal`/`-shm` à côté d'un `plexhub.db` restauré** : SQLite rejoue le WAL périmé sur le fichier remplacé et corrompt la lecture — `restore_db` les supprime AVANT le `os.replace` (et après avoir conservé …
