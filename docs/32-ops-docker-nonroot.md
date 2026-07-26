# Ops — conteneur non-root (AUDIT-P7-007 / S6.1)

> Réf : `docs/plans/2026-07-26-refacto-audit-v1-plan.md` §2 VAGUE 6 S6.1,
> §5 point 11 (`needs-approval`), §7.4. Aucun contrat API ne change ; c'est
> une rupture d'**exploitation** potentielle sur des déploiements existants.

## Ce qui change

Depuis cette image, `uvicorn` ne tourne plus **root** dans le conteneur mais
sous un utilisateur applicatif dédié **`plexhub`, UID:GID `1000:1000`**
(`Dockerfile`). Un `HEALTHCHECK` est désormais embarqué **dans l'image**
(avant : uniquement dans `docker-compose.yml`, donc absent d'un `docker run`
direct depuis GHCR), il interroge `GET /api/health` — le seul endpoint
`/api/*` public de toute l'API (aucun secret nécessaire, cf. CLAUDE.md §3
« Auth »).

## Pourquoi c'est une rupture potentielle

`docker-compose.yml` monte 4 répertoires **hôtes** par-dessus des chemins
que l'image utilisait jusqu'ici en root :

| Volume compose | Chemin conteneur | Contenu |
|---|---|---|
| `./data` (ou `DATA_DIR`) | `/app/data` | base SQLite (`.db`/`-wal`/`-shm`), verrou d'élection master `server_start.lock` (`fcntl.flock`), cache mapping Plex |
| `./logs` (ou `LOG_DIR`) | `/app/logs` | logs applicatifs (`SafeRotatingFileHandler`) |
| `${PLEX_MEDIA_HOST_PATH:-./media}` | `/app/media` (`PLEX_LIBRARY_DIR`) | bibliothèque `.strm`/NFO générée |
| `${DOWNLOAD_HOST_PATH:-./downloads}` | `/app/downloads` (`DOWNLOAD_DIR`) | fichiers physiques téléchargés (feature « Télécharger ») |

Sur une installation existante, ces répertoires hôtes appartiennent
généralement à `root` (ils ont été créés par le conteneur root d'origine).
Un conteneur qui bascule en UID `1000` **ne peut plus y écrire** tant que
l'opérateur n'a pas ajusté les permissions — c'est une régression sourde :
pas de message d'erreur explicite en façade, juste un boot qui échoue ou,
pire, une dégradation silencieuse (voir ci-dessous).

## Procédure obligatoire avant mise à jour

**Une seule fois**, avant le premier `docker compose up` (ou `docker run`)
avec cette image, sur l'hôte :

```bash
chown -R 1000:1000 ./data ./logs ./media ./downloads
```

Adapter les deux derniers chemins si `PLEX_MEDIA_HOST_PATH` /
`DOWNLOAD_HOST_PATH` pointent ailleurs. Si le service tourne déjà, l'arrêter
avant le `chown` pour éviter tout écrivain concurrent pendant le changement
de propriétaire.

Vérification post-chown :

```bash
stat -c '%u:%g %n' ./data ./logs ./media ./downloads
# attendu : 1000:1000 sur les 4
```

## Ce qui se passe si le `chown` est oublié

- **Cas le plus probable — boot qui échoue proprement** : `init_db` ne peut
  pas créer/ouvrir le fichier SQLite dans `/app/data` → l'exception remonte,
  le conteneur redémarre en boucle (`restart: unless-stopped`) — visible dans
  `docker logs`/`docker compose ps`.
- **Cas plus insidieux (AUDIT-P1-003)** : si l'écriture du verrou
  `server_start.lock` (élection master-worker, `fcntl.flock`, piège §9.7)
  échoue avec un `OSError` qui n'est **pas** reconnu comme « lock déjà
  tenu », le code actuel traite ça comme « un autre process est déjà
  master » et bascule **silencieusement en esclave** : aucun scheduler,
  aucun pipeline sync/enrichment/génération ne tourne, sans log d'erreur
  visible ni alerte. Le conteneur répond `200` sur `/api/health` (il boote,
  il sert l'API existante) mais **plus aucune donnée ne se rafraîchit**.
  → Après toute mise à jour vers cette image, vérifier que le pipeline
  planifié progresse (logs `plexhub` — recherche des lignes de sync/
  enrichment) plutôt que de se fier au seul `HEALTHCHECK` vert.
- Le worker de téléchargement (feature « Télécharger ») ne peut pas écrire
  ses fichiers `.part` dans `DOWNLOAD_DIR` → jobs en échec permanent, visibles
  dans l'onglet admin `/admin/downloads`.

## Rollback

Le `chown` n'est pas destructif (il ne modifie aucun contenu, seulement les
métadonnées de propriétaire) et n'a pas besoin d'être annulé. Pour revenir à
l'image précédente (root), aucune action sur les volumes n'est requise — un
conteneur root peut toujours écrire dans des répertoires appartenant à
`1000:1000`.

## Note pour un run hors-compose (`docker run` direct, ex. depuis GHCR)

Le `HEALTHCHECK` est désormais actif même sans `docker-compose.yml`. Les
mêmes volumes doivent être montés en écriture pour l'UID `1000` :

```bash
docker run -d \
  -v /path/to/data:/app/data \
  -v /path/to/logs:/app/logs \
  -v /path/to/media:/app/media \
  -v /path/to/downloads:/app/downloads \
  --env-file .env \
  -e PLEX_LIBRARY_DIR=/app/media -e DOWNLOAD_DIR=/app/downloads \
  -p 8000:8000 \
  ghcr.io/<org>/plexhub-backend:<tag>
```

en s'assurant au préalable que `/path/to/{data,logs,media,downloads}`
appartiennent à `1000:1000` sur l'hôte.
