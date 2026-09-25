# CAP-BE-24 — Sauvegarde & maintenance de la base

**Reconstituée le** : 2026-09-26 · **HEAD** : `9b7d76a` · **Sources** : code, tests, `CLAUDE.md` (§3 « DB », §9 pièges 8 et 11), `docs/architecture/ARCHITECTURE.md`, `docs/context/etat-reel.md`, `.env.example`, `docker-compose.yml`, app TV (lecture seule, preuves `tv:`). Aucun PRD ne couvre cette capacité : `docs/10-prd-*.md` ne traite que les téléchargements. Il n'y a donc pas de tableau d'écart fiche ↔ PRD ; les écarts sont face aux docstrings du code et aux documents d'architecture.
**Périmètre** : le script `app/scripts/backup_db.py`, la tâche planifiée `db_backup` (`app/main.py`), le module `app/db/maintenance.py`, la migration 023 (`ANALYZE` unique), l'appel de maintenance en fin de pipeline (`_rebuild_unified_groups`), la config `BACKUP_*` (`app/config.py`), et les deux sauvegardes de sécurité des scripts correctifs `--apply`. Aucune route, aucun écran, aucune table propre : voir `docs/retro-spec/_genere/inventaire.md` pour l'inventaire général. Hors périmètre : le pipeline lui-même (CAP-BE-05), le gauge de fraîcheur et `/api/health` (CAP-BE-01).

## Résumé

Le backend sauvegarde sa base SQLite une fois par nuit, sans s'arrêter, et purge les copies de plus de sept jours par défaut. La copie passe par l'API `backup` native de SQLite, exécutée dans un thread. La tâche n'existe que sur le processus master. Une commande CLI permet de lancer la même sauvegarde à la main. Deux scripts correctifs prennent leur propre copie avant `--apply`. La base reçoit aussi une maintenance des statistiques du planificateur SQLite : un `ANALYZE` à la migration 023 (une seule fois par base), puis `ANALYZE` et `PRAGMA optimize` à la fin de chaque passe de pipeline. La capacité n'a aucune surface API, aucune UI admin et aucun consommateur TV. Il n'existe ni restauration, ni vérification d'intégrité d'un backup, ni alerte : ce sont les principaux trous.

## User stories observées

- **US-BE-24.1** (P1) — En tant qu'opérateur, je veux qu'un snapshot de la base soit pris chaque nuit sans arrêter l'app, afin de revenir en arrière après une corruption ou une mutation de masse.
  - Parcours : cron `db_backup` → fichier dans le dossier de sauvegarde · Maestro : sans objet
- **US-BE-24.2** (P2) — En tant qu'opérateur, je règle l'heure, le dossier et la rétention par variables d'environnement, ou je désactive la sauvegarde.
  - Parcours : `.env` → `BACKUP_*` · Maestro : sans objet
- **US-BE-24.3** (P2) — En tant qu'opérateur, je lance une sauvegarde à la demande avant une opération risquée.
  - Parcours : `python -m app.scripts.backup_db [--retention-days N]` · Maestro : sans objet
- **US-BE-24.4** (P2) — En tant qu'opérateur, je veux que les vieux backups soient purgés sans jamais toucher à d'autres fichiers.
  - Parcours : fin de `run_backup` · Maestro : sans objet
- **US-BE-24.5** (P2) — En tant que supervision, je veux savoir quand la dernière sauvegarde a réussi, afin d'alerter si elle devient périmée.
  - Parcours : `/metrics` (gauge `db_backup`) · Maestro : sans objet
- **US-BE-24.6** (P2) — En tant qu'opérateur, je veux que le planificateur SQLite ait des statistiques à jour sans intervention.
  - Parcours : migration 023 puis fin de pipeline · Maestro : sans objet
- **US-BE-24.7** (P3) — En tant qu'opérateur d'un script correctif `--apply`, je veux une copie automatique avant la correction de masse.
  - Parcours : `validate_id_consistency --apply`, `dedup_resolved_twins --apply` · Maestro : sans objet
- **Absent, à arbitrer** : restaurer un backup par script ou UI, vérifier l'intégrité d'une copie, lister ou déclencher un backup depuis l'admin.

Côté app TV : aucun parcours (voir Dépendances).

## Exigences fonctionnelles

### Sauvegarde planifiée

- **FR-BE-855** — Une tâche cron `db_backup` est enregistrée seulement si `BACKUP_ENABLED` est vrai, et seulement dans le bloc réservé au master (verrou `fcntl.flock`). Un processus esclave n'enregistre aucune tâche. — *implémenté* — preuve `app/main.py:324`, `app/main.py:420`, `app/main.py:427-435` · test : aucun (`tests/test_startup_wiring.py:378-386` déclare ce câblage non couvert)
- **FR-BE-856** — La tâche tourne une fois par jour à l'heure `BACKUP_HOUR`, avec une seule instance à la fois, les exécutions manquées fusionnées et une tolérance de retard d'une heure. Il n'y a aucun rattrapage au démarrage. — *implémenté* — preuve `app/main.py:430-434` · test : aucun
- **FR-BE-857** — La copie s'exécute dans un thread (`asyncio.to_thread`), pour ne pas bloquer la boucle d'événements. — *implémenté* — preuve `app/main.py:423-425` · test : aucun
- **FR-BE-858** — La sauvegarde est un snapshot en ligne de la base, via l'API native `sqlite3.Connection.backup`, sans arrêter l'application. La source est `DB_PATH` (`DATA_DIR/plexhub.db`). — *implémenté* — preuve `app/scripts/backup_db.py:31-42`, `app/config.py:396` · test `tests/test_backup_db.py:33-39` (copie cohérente), `tests/test_backup_db.py:41-54` (le « writer concurrent » commite avant la copie, en mode journal par défaut : il ne prouve pas la tenue sous écritures WAL actives)
- **FR-BE-859** — Le fichier s'appelle `plexhub-YYYYMMDD-HHMMSS.db`, avec un horodatage en UTC. La granularité est la seconde : deux sauvegardes de la même seconde écrasent le même fichier. — *implémenté* — preuve `app/scripts/backup_db.py:21-28` · test : indirect, `tests/test_backup_db.py:58-63` reconstruit le même format à la main
- **FR-BE-860** — Le dossier cible (`BACKUP_DIR`) est créé à la demande au moment de la copie, pas au démarrage. — *implémenté* — preuve `app/scripts/backup_db.py:33`, `app/config.py:153` · test `tests/test_backup_db.py:36-39`
- **FR-BE-861** — Si la base source n'existe pas, `run_backup` lève `FileNotFoundError` avant toute copie et sans purge. — *implémenté* — preuve `app/scripts/backup_db.py:72-73` · test : aucun
- **FR-BE-862** — La configuration passe par quatre variables : `BACKUP_ENABLED` (défaut vrai), `BACKUP_DIR` (défaut `./data/backups`), `BACKUP_RETENTION_DAYS` (défaut 7), `BACKUP_HOUR` (défaut 4). Les deux entiers retombent sur leur défaut avec un avertissement s'ils ne sont pas numériques. `BACKUP_ENABLED` n'est vrai que pour `true`, `1` ou `yes` : toute autre valeur (par exemple `on`) désactive la sauvegarde sans avertissement. Compose injecte ces variables par `env_file`. — *implémenté* — preuve `app/config.py:12-19`, `app/config.py:151-155`, `.env.example:169-173`, `docker-compose.yml:40` · test : `tests/test_utilities.py:63-85` couvre `_safe_int` en général, rien de propre à `BACKUP_*`

### Rétention et purge

- **FR-BE-863** — La purge supprime les fichiers dont l'horodatage lu dans le nom est strictement antérieur à `maintenant − rétention`. Elle ne regarde jamais la date de modification du fichier. — *implémenté* — preuve `app/scripts/backup_db.py:52`, `app/scripts/backup_db.py:59-66` · test `tests/test_backup_db.py:65-72`
- **FR-BE-864** — Une rétention ≤ 0 (variable d'environnement ou CLI) désactive la purge : tout est conservé. Ce comportement n'est pas documenté dans `.env.example`. — *implémenté* — preuve `app/scripts/backup_db.py:50-51` · test `tests/test_backup_db.py:74-78` (valeur 0 seulement ; négatif non testé)
- **FR-BE-865** — La purge ne touche que les fichiers réguliers dont le nom commence par `plexhub-`, finit par `.db` et porte un horodatage valide. Tout le reste est ignoré. Un fichier déposé à la main sous ce format exact serait donc supprimé une fois trop vieux. — *implémenté* — preuve `app/scripts/backup_db.py:56-63` · test `tests/test_backup_db.py:80-87` (`random.txt` et `plexhub-bogus.db` préservés)
- **FR-BE-866** — La purge ne s'exécute qu'après une copie réussie : si la copie lève, les anciens backups sont conservés. Un dossier de sauvegarde absent donne une liste vide. — *implémenté* — preuve `app/scripts/backup_db.py:54-55`, `app/scripts/backup_db.py:75-82` · test : le test de bout en bout (`tests/test_backup_db.py:90-101`) n'asserte pas la purge

### Lancement manuel et sauvegardes de sécurité

- **FR-BE-867** — L'opérateur lance la même sauvegarde en ligne de commande, avec l'option `--retention-days N` pour surcharger la rétention. La commande enchaîne copie et purge. — *implémenté* — preuve `app/scripts/backup_db.py:70-101` · test `tests/test_backup_db.py:90-101` (`run_backup` seulement ; `main()` et l'argument ne sont pas testés)
- **FR-BE-868** — Les scripts `validate_id_consistency --apply` et `dedup_resolved_twins --apply` prennent une copie de la base avant d'écrire, dans le dossier de la base : `plexhub.preidfix-<stamp>.db` et `plexhub.prededup-<stamp>.db`, avec l'heure locale. Ces fichiers portent le préfixe `plexhub.` et non `plexhub-` : la purge FR-BE-865 ne les voit jamais, ils s'accumulent. — *implémenté* — preuve `app/scripts/validate_id_consistency.py:636-646`, `app/scripts/validate_id_consistency.py:687-692`, `app/scripts/dedup_resolved_twins.py:60-69` · test : aucun test des fonctions `_backup`

### Supervision de la sauvegarde

- **FR-BE-869** — La réussite de la tâche `db_backup` est enregistrée dans un gauge de fraîcheur, lisible sur `/metrics` ; une exception laisse le gauge inchangé. La fraîcheur n'apparaît pas dans `/api/health`, qui ne rapporte que le job `pipeline`. — *partiel* (manque : exposition dans `/api/health`, et test du câblage `track_job` sur ce job) — preuve `app/main.py:428`, `app/utils/job_health.py:100-125`, `app/utils/metrics.py:155-162`, `app/api/health.py:43` · test `tests/test_health_freshness.py:52-55`, `tests/test_metrics_registry.py:120-124` (isolés du câblage)

### Maintenance des statistiques SQLite

- **FR-BE-870** — La migration 023 exécute un `ANALYZE` unique, uniquement si `sqlite_stat1` est vide ou absente. Si la table contient déjà une ligne, la migration ne fait rien : le boot ne repaie pas le coût d'un `ANALYZE` complet à chaque redémarrage. — *implémenté* — preuve `app/db/migrations.py:58`, `app/db/migrations.py:1079-1088`, `app/db/migrations.py:1113-1130` · test `tests/test_migration_023.py:80`, `tests/test_migration_023.py:87`, `tests/test_migration_023.py:112`, `tests/test_migration_023.py:145`, `tests/test_migration_023.py:173`, `tests/test_migration_023.py:182`
- **FR-BE-871** — À la fin de chaque passe de pipeline, `_rebuild_unified_groups` appelle `run_sqlite_maintenance`, qui exécute un `ANALYZE` complet puis `PRAGMA optimize`. L'appel a lieu pendant que le verrou de pipeline est encore tenu, à la fois dans le pipeline planifié et dans le pipeline initial du démarrage. — *implémenté* — preuve `app/main.py:156-179`, `app/main.py:338-348`, `app/main.py:524-538`, `app/db/maintenance.py:69-85` · test `tests/test_migration_023.py:319` (chemin heureux de la fonction ; le câblage `_rebuild_unified_groups` → maintenance n'est pas assertionné)
- **FR-BE-872** — La maintenance n'est jamais fatale : toute exception de `ANALYZE` ou de `PRAGMA optimize` est journalisée en avertissement et avalée, sans interrompre le pipeline. — *implémenté* — preuve `app/db/maintenance.py:61-66`, `app/db/maintenance.py:80-85` · test `tests/test_migration_023.py:269` (vrai verrou d'écriture), `tests/test_migration_023.py:298` (idem, sans assertion sur les logs)
- **FR-BE-873** — La maintenance suit toute exécution du pipeline. — *partiel* (manque : le pipeline déclenché par `POST /api/sync/full-pipeline` enchaîne sync, enrichissement, validation et génération, puis marque la phase « snapshot » sans reconstruire le snapshot ni appeler la maintenance) — preuve `app/api/sync.py:157-172` · test : aucun

### Fonctions annoncées ou attendues, introuvables

- **FR-BE-874** — Le système restaure une sauvegarde (script, endpoint ou UI). — *absent* (aucun code de restauration dans `app/` ; seuls deux documents d'architecture anciens décrivent une copie manuelle, voir Écarts)
- **FR-BE-875** — Le système vérifie l'intégrité d'un backup après écriture (`integrity_check` ou `quick_check`). — *absent* (aucune occurrence dans `app/`)
- **FR-BE-876** — L'écriture d'un backup est atomique (fichier temporaire puis renommage), pour ne jamais laisser un `plexhub-*.db` partiel. — *absent* (`app/scripts/backup_db.py:35-42` écrit directement sous le nom final, sans nettoyage en cas d'échec)
- **FR-BE-877** — L'opérateur liste ou déclenche une sauvegarde depuis l'admin. — *absent* (aucune route ni aucun gabarit ne mentionne la sauvegarde)
- **FR-BE-878** — Une alerte, un contrôle d'espace disque, ou une copie hors de l'hôte accompagne la sauvegarde. — *absent* (le dossier par défaut vit dans le volume de la base, `docker-compose.yml:62`)
- **FR-BE-879** — Le système exécute `VACUUM` ou un `wal_checkpoint` explicite. — *absent* (le mode WAL est posé à `app/db/database.py:122`, sans checkpoint ni compactage programmé)

## Règles métier

- **RG-BE-24.1** — Seul le master sauvegarde. L'élection est un `fcntl.flock` sur `DATA_DIR/server_start.lock` ; l'image lance un seul processus `uvicorn`. — preuve `app/main.py:301-318`, `app/main.py:324`, `Dockerfile:67`
- **RG-BE-24.2** — La rétention est exprimée en jours, pas en nombre de copies : avec un cron quotidien, sept jours donnent sept à huit fichiers. — preuve `app/scripts/backup_db.py:50-66`
- **RG-BE-24.3** — L'ordre est toujours copie, journal, puis purge. Un échec de copie ne purge rien. — preuve `app/scripts/backup_db.py:75-82`
- **RG-BE-24.4** — La maintenance passe par le moteur SQLAlchemy asynchrone (aiosqlite), donc sans `to_thread` supplémentaire, et prend le verrou d'écriture comme tout autre écrivain (WAL, `busy_timeout` 60 s). — preuve `app/db/maintenance.py:27-37`, `app/main.py:170-179`
- **RG-BE-24.5** — Le mot de passe Xtream est chiffré (Fernet) en base ; un backup le contient donc chiffré. — preuve `app/utils/crypto_fields.py:3`, `app/utils/crypto_fields.py:17`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| `BACKUP_ENABLED` faux, ou valeur non reconnue (`on`) | Aucune tâche enregistrée, sans avertissement | `app/config.py:152`, `app/main.py:420` |
| Processus esclave | Aucune tâche enregistrée | `app/main.py:324` |
| Base source absente | `FileNotFoundError`, aucune copie, aucune purge | `app/scripts/backup_db.py:72-73` |
| Dossier de sauvegarde non inscriptible (uid 1000, volume mal `chown`) | Aucun `try/except` : l'exception sort de `run_backup`, le gauge n'est pas marqué, aucun contrôle au démarrage | `app/scripts/backup_db.py:33-38`, `app/utils/job_health.py:118-121` |
| Échec en cours de copie (disque plein) | Un fichier `plexhub-<ts>.db` partiel peut rester et ressemble à un backup valide ; il sera purgé comme les autres | `app/scripts/backup_db.py:35-42` |
| Collision de nom (même seconde) | Le second écrase le premier | `app/scripts/backup_db.py:23`, `app/scripts/backup_db.py:28` |
| Rétention 0 ou négative | Aucune purge | `app/scripts/backup_db.py:50-51` |
| `BACKUP_HOUR` ou `BACKUP_RETENTION_DAYS` non entier | Défaut (4, 7) et avertissement | `app/config.py:12-19` |
| `BACKUP_HOUR` hors 0-23 | Aucune validation dans le dépôt | `app/main.py:430` |
| Fichier `-wal`, `-shm`, `.txt`, préfixe autre, horodatage invalide | Jamais supprimé | `app/scripts/backup_db.py:57-63` |
| `plexhub.preidfix-*.db`, `plexhub.prededup-*.db` | Jamais purgés | `app/scripts/validate_id_consistency.py:639`, `app/scripts/dedup_resolved_twins.py:62` |
| `ANALYZE` ou `PRAGMA optimize` en échec (verrou, disque) | Avertissement, pipeline non impacté | `app/db/maintenance.py:65-66`, `app/db/maintenance.py:84-85` |
| Migration 023 sur base neuve (vide) | `ANALYZE` exécuté (instantané) | `app/db/migrations.py:1100-1103` |
| Pipeline lancé par `POST /api/sync/full-pipeline` | Aucune maintenance | `app/api/sync.py:157-172` |

## Dépendances

- Autres capacités : `CAP-BE-01` (gauge `db_backup`, `/metrics`, `/api/health`) ; `CAP-BE-05` (verrou de pipeline, `_rebuild_unified_groups`, déclenchement API du pipeline) ; `CAP-BE-04` (mot de passe Xtream chiffré, présent dans chaque copie) ; les scripts correctifs d'identité (`validate_id_consistency`, `dedup_resolved_twins`).
- Autre dépôt : aucun appel TV. La seule route backend que l'app appelle près de ce sujet est `GET api/health` (`tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:12`), qui ne rapporte pas la sauvegarde. Les homonymes de l'app (`tv:app/src/main/java/com/chakir/plexhubtv/work/BackupExportWorker.kt:34`, export de la base Room locale, et un `ANALYZE` Room local dans `tv:core/database/src/main/java/com/chakir/plexhubtv/core/database/DatabaseModule.kt:106`) sont sans lien avec la capacité.
- Données : la sauvegarde copie la base entière ; la maintenance n'écrit que `sqlite_stat1` (et `sqlite_stat4` éventuel). Aucune table métier propre (`docs/retro-spec/technique/db/`).
- Infra : `docker-compose.yml:62` (seul volume, `./data:/app/data`, aucun volume dédié aux sauvegardes) ; `docs/32-ops-docker-nonroot.md` (`chown` 1000 des volumes).

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| La docstring de `run_sqlite_maintenance` dit que `PRAGMA optimize` est assez léger pour l'appeler à chaque passe, « unlike a full `ANALYZE` re-run » (`app/db/maintenance.py:21-25`), et la migration 023 écrit que les stats se rafraîchissent par le seul `PRAGMA optimize`, « incremental by design » (`app/db/migrations.py:1095-1098`). Le code appelle pourtant un `ANALYZE` complet à chaque passe (`app/db/maintenance.py:79`). Le même docstring de migration chiffre un `ANALYZE` complet à 14 minutes sur 1,9 Go à froid (`app/db/migrations.py:1084-1088`). | spec ↔ code, risque de performance | À trancher par Chakir : ne garder que `PRAGMA optimize`, ou borner l'`ANALYZE`. À mesurer avant de conclure (voir Questions ouvertes). |
| `docs/architecture/ARCHITECTURE.md:238` cite main.py:309-324 pour le cron de sauvegarde ; le bloc est en `app/main.py:420-435`. | spec ↔ code (référence périmée) | Mettre à jour la référence à la prochaine passe `/sync-context`. |
| `docs/context/etat-reel.md:25` affirme « aucun ANALYZE/sqlite_stat1 n'a jamais tourné » (constat d'audit) sans mention de sa résolution par la migration 023 et `app/db/maintenance.py`. | spec ↔ code (constat périmé) | Ajouter la mention de résolution. |
| Les documents d'architecture anciens (`docs/architecture/architecture-2026-05-13.md:648` et `docs/architecture/architecture-2026-05-14.md:648`) décrivent la restauration comme un `cp` manuel après arrêt du service. Ils ne disent rien des fichiers `-wal` et `-shm` qui coexistent avec la base. | spec ↔ code, procédure absente | Rédiger un runbook de restauration (arrêt, suppression de `-wal`/`-shm`, copie, redémarrage). |
| Les copies `plexhub.preidfix-*` et `plexhub.prededup-*` sont hors de la rétention (préfixe `plexhub.` au lieu de `plexhub-`). | code (accumulation sans purge) | À trancher : ramener ces copies sous la purge, ou les documenter comme manuelles. |
| `POST /api/sync/full-pipeline` n'exécute ni le snapshot ni la maintenance, alors que la phase finale s'appelle « snapshot ». | code (chemin incomplet) | À trancher : oubli ou choix. |
| Le test « writer concurrent » ne teste pas de vrai writer WAL actif ; le câblage de la tâche `db_backup`, la purge de bout en bout et le CLI n'ont aucun test. | test manquant | Ajouter un test sous vrai verrou WAL (modèle : `tests/test_db_retry_real_lock.py`). |

## Questions ouvertes

- **`ANALYZE` complet à chaque passe de pipeline : coût réel** — le code ne tranche pas si c'est intentionnel ni combien de temps il tient le verrou d'écriture sur la base de production. — à vérifier par test (mesure sur une copie de la base, cache froid et cache chaud).
- **`BACKUP_HOUR` hors 0-23** — le comportement dépend d'APScheduler ; le dépôt ne valide rien. Une `ValueError` au démarrage est probable. — à vérifier par test.
- **Fuseau du cron** — `AsyncIOScheduler()` est construit sans fuseau (`app/main.py:353`), alors que les noms de fichiers sont en UTC (`app/scripts/backup_db.py:27`). L'heure réelle du cron par rapport à `BACKUP_HOUR` n'est pas tranchée par le code. — à vérifier par test.
- **Sauvegardes sur le même volume que la base** — le défaut est `./data/backups`, donc même disque. Voulu ? Une copie complète pèse la taille de la base (1,9 Go cité par la migration 023, soit environ 13 à 15 Go pour sept jours), sans contrôle d'espace libre. — décision de l'opérateur.
- **Rattrapage** — si le conteneur est arrêté plus d'une heure autour de `BACKUP_HOUR`, la sauvegarde du jour est perdue. Le comportement exact d'APScheduler (jobstore en mémoire) n'est pas prouvé par le dépôt. — à vérifier par test.
- **Sauvegarde sous fortes écritures WAL** — la copie n'est testée que sans writer actif ; le comportement sous charge (temps, pages recopiées) n'est pas établi. — à vérifier par test.
- **`.env.example:173`** — `BACKUP_HOUR=4` porte un commentaire en ligne ; l'interprétation par `env_file` de Compose n'est pas prouvée. En cas de mauvaise lecture, `_safe_int` retombe sur 4. — à vérifier par test.
- **`PRAGMA analysis_limit`** — l'app TV borne son `ANALYZE` local (`tv:core/database/src/main/java/com/chakir/plexhubtv/core/database/DatabaseModule.kt:81`) ; le backend n'utilise pas de borne. Est-ce transposable côté serveur ? — à vérifier par test.
