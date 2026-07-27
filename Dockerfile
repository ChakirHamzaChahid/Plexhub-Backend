FROM python:3.12-slim
WORKDIR /app
# ffmpeg (Lot A trailers): yt-dlp needs it to mux separate video+audio
# streams into the 720p mp4 the trailer overlay wants — without it, the
# primary format tier in trailer_service.select_format is unreachable and
# every download degrades to a pre-muxed progressive stream (itag 18, 360p,
# too soft for the TV overlay). Installed as root, BEFORE the non-root
# `USER plexhub` switch below (apt requires root); ~100 MB image growth.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
RUN mkdir -p /app/data /app/logs

# AUDIT-P7-007 : utilisateur applicatif non-root (UID/GID 1000 — convention
# Docker standard, valeur du premier utilisateur non-système sur la plupart
# des distributions Linux mono-utilisateur, choisie pour que le `chown`
# opérateur ci-dessous soit prévisible). `--create-home` : fastembed/
# huggingface_hub (cold start, piège §9.1) résolvent leur cache par défaut
# via `Path.home()` quand `AI_EMBED_CACHE_DIR` n'est pas positionné ; sans
# home writable la toute première inférence casserait. Le home vit dans la
# couche conteneur (pas un volume) : le cache y est éphémère aux redémarrages,
# **identique** au comportement root d'avant (HOME=/root, non monté non plus)
# — aucune régression, juste la même reprise nécessaire au cold start.
#
# ⚠️ RUPTURE D'EXPLOITATION (needs-approval, voir
# docs/plans/2026-07-26-refacto-audit-v1-plan.md §5 pt.11 et §7.4,
# docs/32-ops-docker-nonroot.md) : docker-compose.yml monte des volumes HÔTES
# par-dessus /app/data, /app/logs, /app/media, /app/downloads. Le `chown`
# ci-dessous ne couvre que les répertoires DANS L'IMAGE (utile pour un usage
# sans volumes) ; il n'a AUCUN effet sur les volumes montés au run, qui
# gardent l'ownership du répertoire hôte (root sur la plupart des installs
# par défaut). Avant le premier `docker compose up`/`docker run` avec cette
# image :
#     chown -R 1000:1000 ./data ./logs ./media ./downloads
# (ou les chemins visés par PLEX_MEDIA_HOST_PATH/DOWNLOAD_HOST_PATH). Sans ce
# `chown`, le conteneur ne peut pas écrire dans DATA_DIR (SQLite, WAL, le
# verrou `server_start.lock` de l'élection master `fcntl.flock` — piège §9.7)
# ni dans LOG_DIR/DOWNLOAD_DIR : le boot échoue tôt à `init_db`, ou, pire
# (AUDIT-P1-003), une erreur non reconnue comme "locked" sur le lock
# d'élection fait silencieusement basculer l'instance en esclave — plus de
# scheduler, plus de pipeline, sans alerte.
RUN groupadd --gid 1000 plexhub \
    && useradd --uid 1000 --gid plexhub --create-home --shell /usr/sbin/nologin plexhub \
    && chown -R plexhub:plexhub /app/data /app/logs

ENV PYTHONUNBUFFERED=1
ENV APP_PORT=8000
EXPOSE ${APP_PORT}

# AUDIT-P7-007 : HEALTHCHECK au niveau IMAGE. Le seul healthcheck qui existait
# avant ce lot vivait dans docker-compose.yml — absent d'un `docker run` direct
# depuis GHCR. Cible /api/health, seul endpoint `/api/*` public de toute l'API
# (voir CLAUDE.md §3 "Auth" + app/api/route_audit.py, piège §9.10) : aucun
# secret à fournir au HEALTHCHECK. `python` (déjà présent dans l'image, aucune
# dépendance ajoutée) au lieu de `curl`/`wget` (absents de python:3.12-slim) —
# même patron que le healthcheck compose existant. `os.environ.get` lit
# APP_PORT au moment de l'exécution (dans le conteneur en cours, donc la
# valeur réellement configurée), pas la valeur figée au build.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('APP_PORT', '8000') + '/api/health', timeout=5)"]

USER plexhub

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT}"]
