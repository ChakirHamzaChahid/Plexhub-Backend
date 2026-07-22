# Ops — WebDAV virtuel backend + montage rclone pour Plex

> Ticket : DAV-1 (`app/dav/vfs.py`, `tree_builder.py`, `propfind.py`, `throttle.py`, `relay.py`) + DAV-2 (`app/api/dav.py`
> + câblage). Plan de référence : `~/.claude/plans/les-strm-gener-par-swirling-haven.md`.
> Portée : **feature-scoped, additive**. N'altère ni la génération `.strm` (Jellyfin), ni `/api/plex`, ni `/api/media`.

## 0. Pourquoi

Le backend génère une bibliothèque `.strm`/`.nfo` (`app/plex_generator/`) que **Jellyfin** lit très bien. **Plex ignore
totalement les `.strm` au scan** — c'est une limite de la plateforme, pas quelque chose de contournable côté contenu.

Le contournement : un **serveur WebDAV en lecture seule intégré au backend** (`/dav`, self-guardé Basic Auth) qui expose
une arborescence *virtuelle* où chaque film/épisode apparaît comme un vrai `.mkv`/`.mp4` (même hiérarchie que les `.strm`,
extension réelle en plus). À la lecture, le backend **relaie les octets** depuis l'URL Xtream (avec support HTTP Range).
Plex monte ce WebDAV via **`rclone mount`** — sur le **même hôte Linux** que le backend — et voit des fichiers normaux :
scan, indexation, lecture fonctionnent comme sur n'importe quelle bibliothèque locale.

`/dav` reste **local** (rclone tourne sur le même hôte) — **ne jamais** l'exposer via le tunnel Cloudflare.

⚠️ **Exigence de déploiement (revue sécurité F2)** : contrairement à `/api/*` (protégé par `X-API-Key`, `app/api/deps.
verify_backend_secret`), `/dav` n'est PAS enforced local-only par le code — c'est la **même** app FastAPI, sur le
**même** port publié (`${APP_PORT:-8000}`) que le reste de l'API. Rien dans le code n'empêche techniquement un tunnel
mal configuré de router `/dav*` vers l'extérieur. Trois points à vérifier **avant** toute activation `DAV_ENABLED=true`
en environnement avec tunnel Cloudflare (ou tout autre reverse-proxy public) :

1. **Exclure explicitement `/dav*`** au niveau du tunnel/reverse-proxy — une règle d'ingress dédiée qui refuse ce
   préfixe, vérifiée avant d'activer la feature (« ne pas exposer » ci-dessus doit être une **règle appliquée**, pas
   seulement une intention documentée).
2. **`DAV_PASSWORD` fort** — Basic Auth est la SEULE frontière de ce endpoint (pas de rate-limiting, pas de lockout
   après échecs répétés) : un mot de passe **aléatoire, ≥24 caractères** (`openssl rand -base64 24` par exemple),
   jamais un mot de passe réutilisé ou mémorisable.
3. **La rotation de `DAV_PASSWORD` est l'unique point de révocation** — il n'y a pas de notion de session/jeton à
   courte durée de vie ici (contrairement à `X-Plex-Token`) ; en cas de doute sur une fuite, changer `DAV_PASSWORD`
   (+ redémarrer le backend + reconfigurer le remote rclone, § 2) est le SEUL moyen de couper l'accès.

## 1. Activation

Toutes les variables sont documentées dans `.env.example` (section « WebDAV virtuel pour Plex »). Le strict minimum
pour activer la feature :

```
DAV_ENABLED=true
DAV_PASSWORD=<mot de passe fort>
```

`/dav` répond **503** tant que l'une des deux conditions n'est pas remplie (fail-closed) — `DAV_ENABLED=false` par
défaut, `DAV_PASSWORD` vide par défaut.

⚠️ **Phase 1 (sous-ensemble de test) — garder `DAV_MOVIE_LIMIT`/`DAV_SERIES_LIMIT` BAS au démarrage** (défauts `25`/`5`) :
Plex **ffprobe chaque fichier** au scan, et les comptes Xtream sont souvent limités à **1-3 connexions simultanées** —
un scan complet sur ~30k films dès le premier jour saturerait le(s) compte(s) Xtream et déclencherait une tempête de
503 côté rclone. Voir § Rollout par paliers.

## 2. Remote rclone

Sur l'hôte Linux (le même que le backend) :

```bash
rclone config create plexdav webdav \
  url=http://127.0.0.1:8000/dav \
  vendor=other \
  user=<DAV_USERNAME> \
  pass="$(rclone obscure '<DAV_PASSWORD>')"
```

`url` pointe sur le port `${APP_PORT:-8000}` déjà publié par `docker-compose.yml` (ou le port `uvicorn` en bare-metal) —
aucun port/volume supplémentaire n'est nécessaire, rclone atteint `/dav` en HTTP local. `vendor=other` (pas de
quirk WebDAV serveur particulier ici — c'est un serveur maison minimal, cf. § 5).

## 3. Montage — unité systemd

Créer `/etc/systemd/system/plexhub-dav.service` :

```ini
[Unit]
Description=Montage WebDAV virtuel PlexHub (rclone) pour Plex
After=network-online.target plexhub-backend.service
Wants=network-online.target
Requires=plexhub-backend.service

[Service]
Type=notify
ExecStartPre=/bin/mkdir -p /mnt/plexhub-dav
ExecStart=/usr/bin/rclone mount plexdav: /mnt/plexhub-dav \
  --read-only \
  --allow-other \
  --dir-cache-time 720h \
  --poll-interval 0 \
  --attr-timeout 60m \
  --vfs-cache-mode full \
  --vfs-cache-max-size 20G \
  --vfs-read-chunk-size 8M \
  --vfs-read-chunk-size-limit 64M \
  --buffer-size 16M \
  --uid <plex_uid> \
  --gid <plex_gid> \
  --rc \
  --rc-addr 127.0.0.1:5572 \
  --rc-no-auth
ExecStop=/bin/fusermount -uz /mnt/plexhub-dav
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Remplacer `<plex_uid>`/`<plex_gid>` par les ids réels du compte système que Plex utilise pour lire les fichiers
(`id plex` si Plex tourne en tant qu'utilisateur `plex` ; `id -u`/`id -g` du conteneur Plex si Plex est lui-même en
Docker rootless).

**Prérequis** : ajouter `user_allow_other` (sans commentaire `#`) dans `/etc/fuse.conf` — sinon `--allow-other` échoue
au montage (Plex, tournant sous un autre utilisateur/conteneur que celui qui a lancé `rclone mount`, ne pourrait pas
lire à travers le montage FUSE).

```bash
sudo bash -c 'echo "user_allow_other" >> /etc/fuse.conf'
sudo systemctl daemon-reload
sudo systemctl enable --now plexhub-dav.service
```

**Pourquoi ces flags** :
- `--read-only` : le montage n'a jamais vocation à écrire quoi que ce soit (le backend lui-même n'accepte que
  OPTIONS/PROPFIND/HEAD/GET — toute tentative d'écriture WebDAV échoue déjà côté serveur en 405).
- `--dir-cache-time 720h` + `--poll-interval 0` : l'arbre ne change que via un rebuild explicite (invalidation posée
  par `plex_generation_service` après chaque génération réussie) — inutile de repoller le serveur en continu ;
  rafraîchir manuellement via `rclone rc` (§ 4) après un rebuild.
- `--vfs-cache-mode full` + `--vfs-cache-max-size 20G` : Plex fait des seeks arbitraires pendant l'analyse/la lecture ;
  le cache local absorbe les relectures sans re-solliciter le compte Xtream (qui a un cap de connexions serré).
- `--vfs-read-chunk-size 8M` (`--vfs-read-chunk-size-limit 64M`) + `--buffer-size 16M` : lectures par blocs de taille
  raisonnable côté rclone → moins de requêtes HTTP Range vers le relay, meilleure utilisation du shim de Range
  (`DAV_RANGE_SHIM`, voir `app/dav/relay.py`) quand le panel Xtream l'ignore.
- `--rc --rc-addr 127.0.0.1:5572 --rc-no-auth` : ouvre l'API de contrôle rclone en local uniquement (jamais exposée)
  pour piloter le rafraîchissement du cache VFS après un rebuild d'arbre (§ 4).

### Si Plex tourne lui-même en Docker

Bind le montage FUSE **en lecture seule** dans le conteneur Plex, et s'assurer que le montage démarre **avant** le
conteneur Plex (sinon Plex voit un dossier vide au démarrage) :

```yaml
services:
  plex:
    volumes:
      - /mnt/plexhub-dav:/dav:ro
    depends_on:
      - plexhub-dav-mount   # ou un healthcheck équivalent côté hôte
```

Un montage FUSE côté hôte n'est PAS automatiquement visible dans un conteneur créé avant lui — démarrer
`plexhub-dav.service` (via systemd, hors compose) avant `docker compose up plex`, pas l'inverse.

## 4. Rafraîchir après un rebuild d'arbre

Le backend invalide son cache TTL de l'arbre DAV (`app/dav/vfs.py::DavTreeCache`) automatiquement à la fin de chaque
génération de bibliothèque réussie (`plex_generation_service.generate_plex_library_auto`, gaté `DAV_ENABLED`) — la
PROCHAINE requête `/dav` reconstruit l'arbre. Mais rclone, côté VFS, garde son **propre** cache de listing
(`--dir-cache-time 720h`) : pour que Plex voie les nouveaux items sans attendre 30 jours, forcer un refresh rclone
après chaque rebuild de bibliothèque :

```bash
rclone rc vfs/refresh recursive=true --rc-addr 127.0.0.1:5572
```

(À automatiser en cron/hook après le pipeline de sync si le rythme de mise à jour du catalogue le justifie — hors
scope de ce ticket.)

## 5. Réglages Plex — CRITIQUES

Chaque feature d'analyse Plex évitée = une lecture complète de fichier en moins par item scanné, sur un flux qui
consomme une connexion Xtream limitée. Sur les deux bibliothèques de test (Films / Séries), **désactiver** :

- **Vignettes d'aperçu vidéo** (« Video preview thumbnails ») — génère des captures à intervalles réguliers sur toute
  la durée du fichier = lecture quasi complète.
- **Détection intro/générique** (Intro/Credits detection).
- **Analyse sonore / loudness** (Audio analysis / Loudness).
- **Vignettes de chapitres** (Chapter thumbnails).
- **« Extensive/deep media analysis »** — se contenter de l'analyse standard (codecs/résolution via ffprobe, déjà
  incontournable pour indexer le fichier).
- **Scan automatique / partial-scan sur changement** — `inotify` ne traverse **pas** un montage FUSE (rclone ne
  génère aucun événement de changement de fichier) : le scan automatique n'a de toute façon aucun effet ici → passer
  en **scans manuels/planifiés** (bouton « Scan Library Files » ou une tâche planifiée à heure creuse).

Agents Plex par défaut (Movie/TV) : les noms générés (`Title (Year)/Title (Year).ext`, `Season NN/Title SxxEyy.ext`)
matchent sans NFO grâce au nommage standard — pas besoin d'agent custom.

## 6. Rollout par paliers

1. **Phase 0 — arbre + PROPFIND sans octets.** Vérifier le listing seul avant tout relais d'octets (§ 7, étape 1).
2. **Phase 1 — le livrable : caps bas (25 films / 5 séries).** Créer les 2 bibliothèques Plex de test sur le mount,
   scan complet, lecture + seek d'un film et d'un épisode. Vérifier dans les logs backend (`plexhub.dav` /
   `plexhub.api.dav`) l'absence de tempête de 503 et le respect de la limite de connexions upstream par compte
   (`DAV_UPSTREAM_PER_ACCOUNT`, clampée par `XtreamAccount.max_connections`).
3. **Phase 2 — élargissement progressif.** Monter `DAV_MOVIE_LIMIT`/`DAV_SERIES_LIMIT` par paliers (25 → 250 → 2500 →
   …), avec un **scan Plex manuel entre chaque palier** — jamais tout le catalogue d'un coup. L'ordre de sélection du
   sous-ensemble est **déterministe** (tri titre/année/source_id) : les items déjà exposés gardent leurs chemins d'un
   palier à l'autre, seuls des items supplémentaires apparaissent. Options disponibles à ce stade : HEAD paresseux
   (`DAV_REQUIRE_KNOWN_SIZE=false`), multi-versions (`DAV_SINGLE_VERSION=false`), posters/fanart servis depuis les
   images déjà générées sous `PLEX_LIBRARY_DIR`.

## 7. Vérification

### 7.1 Local, sans Plex (avant tout montage)

```bash
# Listing (PROPFIND) — doit lister Films/ et Series/ avec les items du sous-ensemble configuré.
rclone lsl plexdav:

# Lecture complète (GET) d'un fichier — doit streamer les vrais octets vidéo.
rclone cat plexdav:Films/<Titre>/<Titre>.mkv | head -c 1M > /tmp/sample.bin
file /tmp/sample.bin   # doit reconnaître un conteneur vidéo, pas du JSON/HTML d'erreur

# HTTP Range direct (sans passer par rclone) — doit répondre 206 Partial Content.
curl -i -u "<DAV_USERNAME>:<DAV_PASSWORD>" \
  -H "Range: bytes=0-1023" \
  "http://127.0.0.1:8000/dav/Films/<Titre>/<Titre>.mkv"
```

### 7.2 Device Plex

1. Créer 2 bibliothèques Plex de test (« Films (DAV test) », « Séries (DAV test) ») pointant sur
   `/mnt/plexhub-dav/Films` et `/mnt/plexhub-dav/Series`.
2. Appliquer les réglages § 5 sur ces deux bibliothèques AVANT le premier scan.
3. Lancer un scan complet — surveiller les logs backend (`docker compose logs -f backend | grep dav`) : pas de
   rafale de `503`, le nombre de connexions upstream simultanées par compte ne dépasse jamais
   `DAV_UPSTREAM_PER_ACCOUNT`/`max_connections`.
4. Lire un film et un épisode jusqu'au bout d'un seek (avance rapide) — vérifier l'absence de coupure/buffering
   anormal.

## 8. Risques actés

- **Dérive de taille DB vs upstream** : si le provider ré-encode un fichier après coup, la taille en base
  (`Media.file_size`, posée par `health_check_worker`) diverge de la taille réelle — un `GET` loggue un warning
  (`DAV GET size mismatch for …`, jamais l'URL) mais continue de streamer ; auto-corrigé au prochain passage du
  health worker + rebuild d'arbre.
- **Contention scan vs visionnage** sur un compte `max_connections=1` : inhérent au compte lui-même ; un client qui
  patiente au-delà de `DAV_QUEUE_TIMEOUT_SECONDS` reçoit un `503 + Retry-After: 10` que rclone réessaie tout seul.
  Option phase 2 : clamper à `max_connections - 1` pour réserver une connexion à la lecture live.
- **Permit tenu pendant le shim de Range** (`app/dav/relay.py::_shim_ranged_body`, gaté `DAV_RANGE_SHIM`) : quand le
  panel Xtream ignore le header `Range` et répond `200` avec le fichier complet, le shim **draine tout l'upstream**
  (lit et jette les octets avant `start`, continue après `end` plutôt que de couper la connexion) pour re-découper la
  fenêtre demandée — et le permit de throttle (`app/dav/throttle.py`) reste tenu pendant TOUTE cette durée (relâché
  seulement au `finally` de `app/api/dav.py::_get_response.body()`, à la fin de l'itération). Sur un compte
  `max_connections=1`, un seek Plex (avance rapide) sur un tel provider **bloque toute autre lecture** le temps du
  drain complet du fichier — pas seulement le temps de la fenêtre demandée. Correctness avant bande passante upstream,
  comme documenté dans `relay.py`, mais à garder en tête pour le dimensionnement des comptes exposés via `/dav`.
- **Parité de nommage DAV vs `.strm` à la frontière du cap** : `app/dav/tree_builder.py::build_dav_tree` applique la
  désambiguïsation de noms du générateur `.strm` (`resolve_movie_names`/`resolve_series_names`) **APRÈS** filtrage
  ET cap (`DAV_MOVIE_LIMIT`/`DAV_SERIES_LIMIT`) — volontairement, pour que les chemins DAV soient identiques à ce que
  produirait le générateur `.strm` pour ce MÊME sous-ensemble. Conséquence : pour un homonyme `(titre, année)` dont le
  jumeau tombe hors du sous-ensemble exposé par `/dav` (cap bas, ou catégorie/compte exclu), la résolution de nom peut
  différer entre les deux surfaces — le nom reste « nu » côté DAV (pas de suffixe de désambiguïsation, puisqu'aucun
  autre item ne collisionne dans le sous-ensemble réellement construit) alors que le `.strm` Jellyfin (qui voit tout
  le catalogue) l'aurait désambiguïsé. **Inoffensif** : Plex matche ses agents par `Title (Year)` (le nom nu reste
  correct pour CET item) et il n'y a jamais de collision de chemin À L'INTÉRIEUR de l'arbre DAV lui-même (le
  sous-ensemble effectivement construit est toujours désambiguïsé en interne). C'est uniquement la garantie « chemin
  byte-identique entre les deux surfaces » qui ne tient pas à la frontière du cap — acté, pas un bug.
- **Churn du sous-ensemble** : un item qui devient `broken`/sort des catégories autorisées fait entrer le suivant
  dans la fenêtre du cap — acceptable en phase de test ; si gênant, épingler la sélection dans un fichier persisté
  (miroir de `.plex_mapping.json`), non implémenté ici.
- **`uvicorn --workers N > 1`** : les sémaphores de `app/dav/throttle.py` sont **process-local** — passer à
  plusieurs workers multiplierait le cap effectif par N. Le Dockerfile de ce repo lance un seul process ; ne PAS
  passer `--workers` sans revoir ce point.
