# Observability — PlexHub Backend

## Logging structuré
- Logger applicatif **`plexhub`** (sous-loggers `plexhub.<module>`). Console = INFO, fichier = DEBUG (`logs/plexhub.log`, rotation 10 Mo × 5 via `SafeRotatingFileHandler`). Libs tierces = WARNING.
- **`request_id`** injecté par `RequestIdMiddleware` + `RequestIdLogFilter` (`utils/request_context.py`) ; `-` hors requête HTTP. Format : `%(asctime)s [%(request_id)s] [%(name)s] %(levelname)s: %(message)s`.
- **Règle d'or** : aucun secret/token/clé/PII en clair dans les logs. Ne pas logguer headers d'auth ni bodies susceptibles d'en contenir. Le payload tv-auth est chiffré (Fernet).

## Métriques (Prometheus)
- Exposées sur **`/metrics`** via `prometheus-fastapi-instrumentator` (`utils/metrics.setup_instrumentator`). Métriques HTTP par requête (latence, codes) automatiques.
- Pour une nouvelle métrique métier : compteur/histogramme `prometheus_client` nommé `plexhub_<domaine>_<mesure>` ; documenter l'unité. Pas de label à cardinalité non bornée (pas d'ID utilisateur/tmdb en label).

## Diagnostic IA
- `GET /api/ai/embed/status` = snapshot (counts, modèle chargé, RSS). Premier `/rank` = cold start ~30 s (cf. `CLAUDE.md` §9). Les 3 motifs de 503 sont les premiers à vérifier en incident IA.

## Santé & exploitation
- `GET /api/health` = sonde de liveness (smoke test de tout workflow).
- Pipeline planifié loggé étape par étape (sync → enrich → validation → génération Plex). Le boot logge un résumé sanitisé (version, nb routes, config sans secrets).
- En incident : `logs/plexhub.log` (filtrer par `request_id`) + `/metrics` + repro `curl` = source primaire. Aucune hypothèse non vérifiée.
