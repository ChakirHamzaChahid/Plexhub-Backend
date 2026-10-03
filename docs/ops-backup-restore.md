# Ops — sauvegarde et restauration de la base SQLite

> Lot 10 sous-lot A (FR-BE-874/875/876). Code : `app/scripts/backup_db.py`,
> `app/scripts/restore_db.py`. Déploiement Docker/Linux (service compose
> `backend`, base `/app/data/plexhub.db`, sauvegardes `/app/data/backups`).

## 1. Ce que fait la sauvegarde

Cron quotidien (`BACKUP_HOUR`, master seul) ou à la main :

```bash
docker compose exec backend python -m app.scripts.backup_db            # rétention par défaut
docker compose exec backend python -m app.scripts.backup_db --retention-days 14
```

Garanties :

- **Atomique** : la copie est écrite dans `plexhub-<AAAAMMJJ-HHMMSS-µs>.db.tmp`
  puis renommée en `.db` seulement une fois vérifiée. Un `plexhub-*.db` n'est
  donc jamais partiel. Un `.tmp` laissé par un arrêt brutal est balayé au
  backup suivant (au-delà d'1 h).
- **Vérifiée** : `PRAGMA integrity_check` sur la copie. Si le résultat n'est
  pas `ok`, la copie est supprimée et le backup échoue (l'exception remonte à
  APScheduler qui la journalise ; jauge `db_backup` non rafraîchie) et **aucun ancien backup n'est élagué**.
- **Autonome** : la copie est un fichier unique (pas de `-wal`/`-shm`).
- Les anciens noms (`plexhub-AAAAMMJJ-HHMMSS.db`) restent reconnus par
  l'élagage.

Alerte à surveiller : une erreur `integrity_check failed` dans les logs ou une jauge `db_backup`
périmée sur `/metrics`.

## 2. Restaurer — procédure opérateur

À faire **application arrêtée**. Le script refuse de tourner si l'app tient le
verrou master (`data/server_start.lock`).

```bash
# 1. Choisir le backup
ls -lh data/backups/                 # plexhub-AAAAMMJJ-HHMMSS-µs.db

# 2. Arrêter l'application
docker compose stop backend

# 3. Dry-run (par défaut, ne modifie rien) : vérifie la source et décrit l'opération
docker compose run --rm --no-deps backend \
  python -m app.scripts.restore_db /app/data/backups/plexhub-20261002-040000-123456.db

# 4. Restaurer pour de bon
docker compose run --rm --no-deps backend \
  python -m app.scripts.restore_db /app/data/backups/plexhub-20261002-040000-123456.db --apply

# 5. Redémarrer et contrôler
docker compose up -d backend
curl -s http://localhost:8000/api/health
```

Ce que fait `--apply` (dans cet ordre) :

1. refuse si le verrou master est tenu (code de sortie 1) ;
2. `integrity_check` de la source — une source corrompue est refusée ;
3. **conserve la base courante** sous `data/plexhub.pre-restore-<horodatage>.db`
   (préfixe `plexhub.` et non `plexhub-` : l'élagage automatique ne la supprime
   jamais ; même une base courante corrompue est conservée, par copie brute) ;
4. copie la source vers `data/plexhub.db.restore-tmp`, vérifiée ;
5. **supprime `plexhub.db-wal` et `plexhub.db-shm`** : un WAL périmé à côté d'un
   `.db` restauré serait rejoué dessus et corromprait la lecture ;
6. remplace atomiquement `plexhub.db` (`os.replace`).

Code de sortie `0` = succès (ou dry-run propre), `1` = refus/échec avec message
sur stderr. En cas d'échec avant l'étape 6, `plexhub.db` n'a pas été remplacée.

## 3. Après la restauration

- Vérifier `/api/health` puis quelques écrans de l'app TV.
- La base restaurée est repassée en WAL au démarrage de l'app (comportement
  normal).
- Les NFO/posters déjà générés ne sont pas réécrits (cf. CLAUDE.md §9 piège 21g)
  si la restauration remonte l'identité des médias dans le temps.
- Une fois certain du résultat, supprimer à la main les
  `plexhub.pre-restore-*.db` devenus inutiles (volumineux : taille de la base).

## 4. Fichiers à connaître

| Fichier | Rôle | Élagué automatiquement ? |
|---|---|---|
| `data/backups/plexhub-*.db` | backups vérifiés | oui (rétention) |
| `data/backups/plexhub-*.db.tmp` | backup en cours / orphelin | balayé > 1 h |
| `data/plexhub.pre-restore-*.db` | base courante avant restauration | **non** |
| `data/plexhub.preidfix-*.db`, `plexhub.prededup-*.db` | copies des scripts correctifs `--apply` | **non** |
| `data/plexhub.db-wal` / `-shm` | journal WAL de la base vivante | supprimés par la restauration |

## 5. Hors périmètre (encore ouvert)

Liste/déclenchement depuis l'admin (FR-BE-877), copie hors hôte et contrôle
d'espace disque (878), `VACUUM`/`wal_checkpoint` programmés (879). Les backups
vivent dans le même volume que la base : un backup hors hôte reste à la charge
de l'opérateur.
