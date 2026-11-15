# Index des pièges — ai-media-plex

> Généré par `python .claude/tools/pitfalls-index.py` depuis `.claude/skills/pitfalls/references/ai-media-plex.md` : un titre par piège (son segment en gras). Ne pas éditer ici — ajouter le piège dans `references/`, puis relancer le script.

## ai-media-plex — IA vectorielle, Ollama LLM, sous-titres, téléchargements et WebDAV (11)

Texte complet : `.claude/skills/pitfalls/references/ai-media-plex.md`

- [#1] : **Cold start IA ~30 s** au 1ᵉʳ `/rank` : fastembed télécharge + charge les poids ONNX (cold start documenté `embedding_service.py:11`)
- [#2] : **Motifs 503 IA** (contractuels) : **1 garde au niveau du router `/api/ai`** (`verify_api_key`, `deps.py:76-80`) — `AI vector storage unavailable` (sqlite-vec non chargé)
- [#3] : **Épisodes/personnes non supportés par l'IA vectorielle** : ranking au niveau `movie`/`tv` uniquement (`Literal["movie","tv"]`, `ai.py:87,104`)
- [#4] : **Cap 20 fetches TMDB frais par `/rank`** : `HYDRATE_CAP = 20`, timeout 10 s/tâche (`recommendation_service.py:31-32`)
- [#5] : **Le rebuild ne tourne JAMAIS au boot** : uniquement via `POST /api/ai/embed/rebuild` (`embedding_worker.py:3,132`)
- [#12] : **503 LLM Ollama distinct du 503 fastembed** : sur `/describe`/`/chat` (non-stream)/`/assistant`/`/blurb`/`/subtitles`, toute exception Ollama → `_ollama_503` (`ai.py:855`), message `"LLM unavailable — Ollama unreachable or model not loaded"`
- [#13] : **Dépendance réseau + cold start Ollama** : `ollama_service` parle à `OLLAMA_URL` (défaut `http://khoj-ollama:11434`, hôte Docker — **injoignable en dev local hors compose**)
- [#14] : **SSE chat = pas de 503 propre** : en mode `stream=True`, le flux est déjà ouvert
- [#16] : **Sous-titres : limites dures** : `/subtitles/translate` rejette `>SUBTITLE_MAX_BYTES` (défaut 2 Mo) ou `>SUBTITLE_MAX_CUES` (défaut 3000) en **413**
- [#17] : **Feature « Télécharger » (téléchargement physique)** : (a) **worker master-only** — `download_worker.run_drain_loop` ne tourne que sous `if is_master:` (`main.py`, `fcntl` POSIX, piège 7)
- [#18] : **WebDAV `/dav` (feature-gated, no-op par défaut — §5.11)** : (a) **fail-closed 503** tant que `DAV_ENABLED=true` ET `DAV_PASSWORD` non vide ne sont pas réunis tous les deux
