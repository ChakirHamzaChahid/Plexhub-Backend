# ADR 0006 — Synchro EPG en masse (XMLTV → `epg_entries`), opt-in, master seul

- Statut : **accepté, implémenté** (Lot 10 sous-lot D, FR-BE-222 ; résout FR-BE-554, FR-BE-559, FR-BE-560) — **livré désactivé** (`EPG_SYNC_ENABLED=false`) ; activation en production **conditionnée à une mesure à l'échelle de la base de prod** (voir « Mesure »)
- Date : 2026-10-02 · base HEAD `4910f7b` (`develop`), chaîne de migrations 001→027, **028 ajoutée**
- Plan source : `C:\Users\chakir\.claude\plans\attaque-le-lot-10-hashed-ritchie.md` § D
- Portée : `app/services/epg_sync_service.py` (NOUVEAU), `app/services/xtream_service.py` (`download_xmltv` remplace `get_xmltv`), `app/services/live_service.py` (`ingest_short_epg` en upsert), `app/db/migrations.py` (028), `app/main.py` (job `epg_sync`), `app/config.py` + `.env.example` (`EPG_SYNC_*`), `app/utils/metrics.py` (`epg_sync` dans `_PIPELINE_JOB_NAMES`), `scripts/bench_epg_sync.py` (mesure synthétique). **Aucune route `/api/*` ajoutée ni modifiée** : le TV lit déjà `GET /api/live/channels/{stream_id}/epg` et `GET /api/live/epg` ; la synchro remplit simplement la table qu'elles lisent.
- ADR recoupés : 0004 D4 (`write_with_retry`, session neuve par tentative — obligatoire pour tout écrivain de ce service)

---

## D1 — Faits du code qui contraignent le design (vérifiés à `4910f7b`)

| # | Fait | Preuve | Conséquence |
|---|---|---|---|
| F1 | `sync_worker` n'appelle ni `get_short_epg` ni `get_xmltv` ; il ne mappe que `epg_channel_id` sur les chaînes Live. | `workers/sync_worker.py:392` (mapping), grep des appels | L'EPG n'était jamais rempli par la synchro, contrairement à ce qu'annonçaient `CLAUDE.md` §2 et `flux-cles.md` §5.1. |
| F2 | L'EPG était chargé **à la demande, par chaîne** : cache-miss de `GET /api/live/channels/{stream_id}/epg` → `ingest_short_epg`. `GET /api/live/epg` ne lit que ce qui est déjà en base. | `api/live.py:143-227` | À froid, le « en cours » du TV est vide pour toute chaîne jamais ouverte. |
| F3 | `ingest_short_epg` faisait `db.add` sans upsert. | `services/live_service.py` (avant) | Deux cache-miss concurrents → `IntegrityError` (ou doublons si l'unique manque). |
| F4 | Le modèle déclare l'unique `uix_epg_dedup (server_id, stream_id, start_time)` mais la migration 006 ne crée que 4 index, et `create_all` n'ajoute jamais d'index à une table existante. | `models/database.py` `EpgEntry`, `db/migrations.py` 006 | L'unique PEUT manquer en prod et des doublons PEUVENT exister. Un `ON CONFLICT(server_id, stream_id, start_time)` est **rejeté par SQLite** sans cet index. |
| F5 | `XtreamService.get_xmltv` renvoyait le document ENTIER dans une `str` et n'avait aucun appelant. | `services/xtream_service.py` (avant) | FR-BE-560. Inutilisable tel quel : l'XMLTV d'un gros panel pèse des dizaines à centaines de Mo, le conteneur est plafonné à 2 Go (partagés avec les modèles ONNX), `cpus: 1.0`. |
| F6 | `epg_entries.start_time/end_time` sont en **epoch millisecondes** (short-EPG : `start_timestamp * 1000`) ; les routes live comparent à `int(time.time() * 1000)`. | `live_service.py`, `api/live.py:155,209` | Les dates XMLTV (`YYYYMMDDHHMMSS ±HHMM`) sont converties en ms, pas en secondes. |
| F7 | Le short-EPG Xtream encode parfois titres/descriptions en base64 (`_try_base64_decode`) ; l'XMLTV est du texte XML en clair. | `live_service.py`, piège CR-T01 | Aucun décodage base64 côté XMLTV (un titre « TEST » ressemble à du base64 valide et serait corrompu). |
| F8 | SQLite WAL, **un seul écrivain** ; le pipeline, l'API et les workers écrivent ; `commit_with_retry` ne retente pas réellement. | `CLAUDE.md` §3/§9 piège 8 | Primitive d'écriture = `write_with_retry` ; transactions COURTES. |
| F9 | Les routes live ne filtrent que `is_in_allowed_categories == True` ; `is_adult` est exposé dans le payload, jamais filtré côté serveur. | `api/live.py:39-41`, `LiveChannelResponse` | Règle de visibilité de la synchro = la même, sans en inventer une autre. |
| F10 | Le masquage « panne fournisseur » (`account_outage_service.masked_server_ids`) ne couvre que les lectures `media_service`, pas les routes live. | grep `masked_server_ids` | La synchro l'applique pour ne PAS insister sur un fournisseur confirmé en panne (économie de requêtes), sans changer ce que les routes live servent. |

---

## D2 — Décisions

### D2.1 EPG en masse ET à la demande, sur la même clé
L'EPG en masse devient la source principale (le TV obtient un « en cours » chaud pour toutes les chaînes), le short-EPG à la demande reste le repli (chaîne absente de l'XMLTV, synchro désactivée, compte pas encore passé). Les deux écrivent `epg_entries` par **upsert sur `(server_id, stream_id, start_time)`** : ils ne se dupliquent jamais mutuellement et la dernière écriture gagne.

### D2.2 Migration 028 — l'unique devient réel
Dédoublonne `epg_entries` sur `(server_id, stream_id, start_time)` en gardant **le plus grand `id`** (= la dernière insertion ; l'ingestion short-EPG n'a jamais fait qu'ajouter), PUIS `CREATE UNIQUE INDEX IF NOT EXISTS uix_epg_dedup`, dans **une seule transaction** (tout ou rien). Les lignes `stream_id IS NULL` ne sont pas touchées (un UNIQUE SQLite traite les NULL comme distincts). Idempotente et quasi gratuite après le premier boot : si un `uix_epg_dedup` UNIQUE existe déjà, le dédoublonnage est sauté. Un index homonyme non-unique est reconstruit. **Contrairement aux migrations additives (`ADD COLUMN`), un échec n'est pas avalé** (log ERROR + propagation) : sans l'unique, chaque cache-miss et toute la synchro échoueraient à l'exécution.

### D2.3 `ingest_short_epg` = upsert
`INSERT … ON CONFLICT(server_id, stream_id, start_time) DO UPDATE` (+ `RETURNING` pour rendre les lignes persistées, `populate_existing`). Le contrat reste « la session de l'appelant, pas de commit » ; un `start_time` répété dans un même payload est replié (dernier gagnant).

### D2.4 Ingestion : fichier temporaire + `iterparse` en thread + lots courts
- **Pas de `str` géante.** `download_xmltv` streame le corps vers un **fichier temporaire** par blocs de 256 Kio (écriture disque dans un thread), plafond `EPG_SYNC_MAX_BYTES` mesuré sur l'octet **décompressé** (un gzip bomb est coupé par le même compteur ; un `Content-Length` annoncé au-dessus du plafond échoue avant toute lecture). `get_xmltv` est **supprimée** (FR-560), pas conservée comme second chemin gourmand.
- **Même politique réseau que `_get`** : backoff 1/2/4 s sur timeout/connexion/protocole et sur 429/502/503/504 (`Retry-After` respecté, borné à 60 s) ; chaque tentative repart d'un fichier vide et a son propre plafond de durée (`EPG_SYNC_DOWNLOAD_TIMEOUT_SECONDS`). Pas de suivi de redirection (httpx par défaut). Le garde SSRF du dépôt n'est pas appliqué aux requêtes Xtream (cf. `CLAUDE.md` §9-19e : l'hôte est configuré par l'opérateur), la synchro reste cohérente avec elles.
- **Jamais de credential dans un log ni un rapport** : l'URL porte `username`/`password`, et le message d'un `httpx.HTTPStatusError` embarque l'URL complète. Les erreurs sont rapportées par **type + code HTTP** uniquement (`_safe_error`) ; le logger `httpx` reste épinglé WARNING.
- **Le parsing ne tourne jamais sur la boucle** : un générateur synchrone (`iter_programme_batches`) est avancé par `asyncio.to_thread(next, gen, None)`, un lot (≤ `EPG_SYNC_BATCH_SIZE`, 500) à la fois ; les éléments parsés sont libérés au fil de l'eau (`root.clear()`). Mémoire plate en la taille du fichier. Le générateur possède son descripteur de fichier (`with open`), donc un abandon en cours de route le ferme et permet de supprimer le temporaire (Windows compris).
- **Un lot = une transaction d'écriture courte** (`write_with_retry`, session neuve par tentative, ADR 0004 D4) : le verrou d'écriture n'est jamais tenu plus que quelques millisecondes, et un lot qui entre en collision avec un verrou réel est rejoué sans perte (testé avec un vrai verrou WAL).
- **Les lignes identiques ne sont pas réécrites** (`DO UPDATE … WHERE <contenu changé>`) : un re-run ne coûte du WAL que pour les programmes réellement modifiés. Le rapport sépare `inserted` / `updated` / `unchanged`.
- **XML hostile** : un document qui déclare des entités (`<!ENTITY`) est refusé ; la lecture des entités externes n'est de toute façon pas activée par `xml.etree`.

### D2.5 Fenêtre `[now − EPG_SYNC_PAST_HOURS, now + EPG_SYNC_FUTURE_HOURS]` (1 h / 12 h) par **chevauchement**
Un programme est retenu s'il chevauche la fenêtre (`stop > début` ET `start < fin`), pas seulement s'il y commence : un film commencé il y a 2 h et toujours à l'antenne **est** le « en cours » que le TV cherche. L'XMLTV d'un panel couvre souvent plusieurs jours ; seule la fenêtre est écrite (le fichier d'essai de la mesure garde 20 programmes sur 60 par chaîne). Un `stop` absent, une date invalide ou `stop ≤ start` rendent le programme invalide (compté `skipped_invalid`, jamais écrit).

### D2.6 Mapping de chaînes
`channel=` XMLTV → `LiveChannel.epg_channel_id` (comparaison **trim + casefold**) → `stream_id`. Un id XMLTV inconnu est ignoré (`skipped_unmapped`). **Plusieurs `stream_id` pour un même `epg_channel_id`** (variantes HD/SD/secours d'une même chaîne) : le programme est écrit pour **chacun** — chaque variante existe dans l'app et le TV interroge par `stream_id` ; ce multiplicateur compte dans `EPG_SYNC_MAX_PROGRAMMES`. Chaque ligne garde l'orthographe d'`epg_channel_id` de SA chaîne.

### D2.7 Quels comptes, quelles chaînes
Comptes `is_active` ET non masqués par `account_outage_service.masked_server_ids` (un fournisseur en panne confirmée n'est pas sollicité). Chaînes : `is_in_allowed_categories == True` **seulement** — la règle exacte de `GET /api/live/channels` ; `is_adult` n'est pas filtré côté serveur par les routes live, donc pas ici non plus. Un compte sans chaîne mappable n'est pas téléchargé du tout.

### D2.8 Élagage incrémental des programmes terminés, par plage de `stream_id`
Après un compte synchronisé avec succès, les lignes du serveur dont `end_time < now − EPG_SYNC_PAST_HOURS` sont supprimées. Sans cela, chaque programme entre dans la table ~12 h avant son passage et y reste jusqu'à la purge de 03:00 (24 h plus tard : mesuré 210 000 → 580 000 lignes, fichier ×1,8), puis `_cleanup_stale_epg` les supprime en **une seule** transaction qui tient le verrou d'écriture pendant tout le DELETE. La purge nocturne **reste en place** comme filet de sécurité (comptes que la synchro ne visite plus, lignes short-EPG, synchro désactivée). Best-effort : un échec d'élagage ne fait jamais échouer un compte.
**Le découpage compte** (mesuré, D4) : les programmes terminés sont le plus vieux de *chaque* chaîne, donc éparpillés dans les 7 index ordonnés par chaîne. Un DELETE découpé **par âge** a fait gonfler le WAL à ~255 Mo pour 63 000 lignes (le WAL journalise des pages entières) ; découpé par **plage contiguë de 250 `stream_id` par transaction**, le même travail laisse un WAL de 7 Mo. Chaque transaction reste courte.

### D2.9 Planification : opt-in, master seul, fraîcheur honnête
Job APScheduler `interval` (`EPG_SYNC_INTERVAL_HOURS`, 6), `max_instances=1`, `coalesce=True`, `misfire_grace_time=3600`, `track_job("epg_sync")`, enregistré **dans le bloc master** et **seulement si `EPG_SYNC_ENABLED=true`** (convention DAV/backups : tant que l'opérateur ne l'active pas, le job n'existe même pas). `epg_sync` entre dans `_PIPELINE_JOB_NAMES` (jauge de fraîcheur initialisée à 0). `run_epg_sync_job` **lève** si au moins un compte a été tenté et qu'aucun n'a réussi : sinon `track_job` estamperait « frais » un run où tout a échoué. Un échec partiel reste un succès (les comptes en échec sont dans le rapport et le log).
Master seul parce que (a) le planificateur n'existe que sur le master (`fcntl.flock`, piège 7) et (b) N workers téléchargeraient N fois le même XMLTV et se disputeraient l'unique écrivain SQLite pour des lignes identiques.

### D2.10 Hors pipeline
La synchro n'est **pas** une étape du pipeline `sync → enrichissement → validation → génération` : elle n'a pas besoin de son verrou (`_PIPELINE_LOCK`), ne doit pas rallonger un pipeline déjà long, et son échec ne doit pas l'interrompre. Elle lit simplement les `live_channels` que le dernier sync a posés.

---

## D3 — Alternatives écartées

| Alternative | Pourquoi non |
|---|---|
| **Préchauffer le short-EPG chaîne par chaîne** (appeler `get_short_epg` pour chaque `stream_id`) | Une requête fournisseur PAR chaîne (5 000+ par compte), à chaque cycle : coût en latence, quotas/limiteurs de débit des panels, risque de bannissement du compte — l'XMLTV est LA réponse prévue pour l'EPG en masse (1 requête). |
| Garder `get_xmltv` (corps entier en `str`) | Pic mémoire de la taille du document × 2 (décodage) dans un conteneur à 2 Go partagés avec ONNX. |
| Une transaction par compte | Verrou d'écriture tenu des secondes ; le pipeline et l'API écrivent en même temps ; `database is locked` en cascade. |
| Parser sur la boucle d'événements | Viole le piège §9-11 ; bloque l'API pendant plusieurs secondes de CPU sur `cpus: 1.0`. |
| `xml.sax` / parseur maison | `iterparse` + `clear()` suffit, plus court, mêmes garanties mémoire. |
| Stocker tout l'XMLTV (plusieurs jours) | ×N lignes pour un besoin « en cours + à suivre » ; le coût BDD est proportionnel à la fenêtre. |
| Insérer la synchro EPG dans `sync_account` | Couple l'EPG au verrou et à la durée du pipeline ; un XMLTV en échec ferait échouer un sync de catalogue sain. |
| Déduire l'état « adulte » et masquer côté EPG | Inventerait une règle que les routes live n'ont pas ; le drapeau voyage déjà dans la chaîne. |

---

## D4 — Mesure synthétique (`scripts/bench_epg_sync.py`)

> ⚠️ **Ce que ces chiffres sont — et ne sont pas.** Données **synthétiques** générées localement, sur la machine du développeur (Windows, Python 3.12, non contrainte à `cpus: 1.0` / 2 Go), sonde de latence **dans le même process et la même boucle d'événements** que la synchro. La base de prod (~1,9 Go) et de vrais XMLTV fournisseur n'étaient **pas disponibles** : **la mesure à l'échelle de la PROD reste à faire avant d'activer `EPG_SYNC_ENABLED` en production.** Ces chiffres donnent des ordres de grandeur et la *forme* de la contention.

Commande : `.venv/Scripts/python.exe scripts/bench_epg_sync.py --accounts 2 --channels 5000 --programmes 20 --extra 40 --cycles 4` (puis `--no-prune`, puis `--tracemalloc`). Base jetable en WAL dans un dossier temporaire, moteurs configurés comme la prod (`timeout=60`, pool API 20+30, pool worker 2+2), `ANALYZE` après le run à froid (la prod l'exécute : migration 023 + `PRAGMA optimize` en fin de pipeline). Jeu de données : 2 comptes × 5 000 chaînes ; XMLTV de **65,6 Mo par compte** = **305 000 programmes** (105 000 chevauchent la fenêtre, 200 000 sont des jours passés à écarter) ; grille de créneaux fixe (un re-run redonne les mêmes programmes).

| Mesure | Résultat |
|---|---|
| Run complet **à froid** (2 comptes, tout est insertion) | **35,0 s** (≈ 17,5 s/compte : téléchargement simulé = copie locale + parsing + 105 000 upserts) |
| Lignes écrites dans `epg_entries` | **210 000** (105 000 par compte) ; 200 000 programmes/compte écartés par la fenêtre |
| Re-run immédiat (**tout inchangé**) | **30,4 s**, **0 écriture** (210 000 `unchanged`) — le coût résiduel est le parsing |
| Tick de régime (+6 h, 4 fois) | **33–35 s** ; 90 000 insérées, 120 000 inchangées, 90 000 élaguées ; **table stable à 210 000 lignes** |
| Même 24 h **sans** élagage | table 210 000 → **580 000** lignes ; fichier BDD 116 → **210 Mo** |
| Taille de la table | base 3,2 Mo → **85,4 Mo** après le run à froid (≈ **0,4 Ko/ligne** index compris, 7 index) ; en régime : fichier 116,5 Mo + WAL 16,7 Mo |
| **Pic mémoire** | RSS du process : **+4 à +6 Mo** au plus (échantillonnage `psutil` toutes les 50 ms) ; tas Python (`tracemalloc`) : **pic 6,8 Mo** pour tout le run à froid — plat en la taille du fichier |
| Latence de `GET /api/live/epg` (partie BDD, `live.get_epg_now`) | table vide, au repos : p50 **1,4** / p95 2,1 ms. **Pendant** le run à froid : p50 80 / p95 **150** / max 585 ms. Pendant un run « tout inchangé » (aucune écriture, CPU = parsing) : p50 **203** / p95 234 ms. Pendant les ticks de régime : p95 **20–23 ms** (105 ms au 1ᵉʳ tick). Au repos sur table pleine : p50 55 / p95 57 ms **sans statistiques**, p50 39 / p95 63 ms après `ANALYZE` |
| Latence d'une écriture concurrente (UPDATE d'une ligne via `write_with_retry`) pendant la synchro | p50 **11–22 ms** / p95 **63–68 ms** / max 108 ms ; **aucune erreur** (`probe_errors: []`), aucun `database is locked` remonté |

Ce que les chiffres montrent :

1. **La mémoire n'est pas le risque** (pic < 7 Mo de tas Python sur un XMLTV de 65 Mo) — c'était le but du flux disque + `iterparse` + `clear()`.
2. **Le verrou d'écriture n'est pas le risque non plus** : un écrivain concurrent obtient le verrou en quelques dizaines de ms (lots de 500 lignes) ; testé aussi sous un **vrai** verrou WAL retenu 350 ms (`tests/test_epg_sync_real_lock.py`).
3. **Le vrai coût est le CPU de parsing partagé avec la boucle d'événements.** Le thread de parsing et la boucle se disputent le GIL : pendant les 15–17 s/compte de parsing, une lecture de l'API est ~2 à 4× plus lente (p50 39–55 ms → 80–200 ms ici). Sur `cpus: 1.0` ce sera plus marqué et plus long. Levier prévu : `EPG_SYNC_BATCH_PAUSE_MS` (pause entre deux lots, 0 par défaut) — à n'ajuster qu'après la mesure de prod.
4. **L'élagage est nécessaire, et *comment* on élague compte.** Un DELETE découpé **par âge** (« les 2 000 plus vieilles lignes stales ») a fait gonfler le WAL à **~255 Mo** pour une table de 63 000 lignes (sur le jeu 2 × 5 000 chaînes, un essai antérieur a mesuré un WAL d'environ 1,2 Go) : les programmes terminés sont le plus vieux de **chaque** chaîne, donc éparpillés dans tous les index ordonnés par `stream_id`, et le WAL journalise des pages entières. Découpé par **plage contiguë de `stream_id`** (250 par transaction), le même travail laisse un WAL de **7 Mo** (16,7 Mo sur le jeu 2 × 5 000) sans changer les lignes supprimées. Le DELETE unique de la purge nocturne `_cleanup_stale_epg` a le même défaut à plus grande échelle, d'où le besoin que la synchro garde la table à la taille de la fenêtre pour qu'il soit presque vide (non modifié ici : hors périmètre).
5. `GET /api/live/epg` sur table pleine coûte ~40–55 ms par appel (tri + `COUNT` sur ~100 000 lignes/serveur) : acceptable, et le plan s'améliore avec les statistiques (le planificateur passe de `ix_epg_server` à `ix_epg_time`) ; un index composite `(server_id, start_time, end_time)` ramène le `COUNT` à ~1 ms (essai sur copie, **non ajouté** : à décider sur la mesure de prod).

---

## D5 — Conséquences et prérequis d'activation

- **Avant `EPG_SYNC_ENABLED=true` en production** : rejouer `scripts/bench_epg_sync.py` n'est PAS suffisant — la base de prod fait ~1,9 Go, les XMLTV réels des fournisseurs ont leurs propres tailles et défauts, et le conteneur est limité à 1 CPU / 2 Go. Il faut mesurer sur une **copie de la base de prod** avec de vrais comptes : durée d'un run, taille d'`epg_entries` après, latence de `/api/media/*` et de `/api/live/epg` pendant le run (le verrou d'écriture est la contrainte), pic mémoire du conteneur.
- La table `epg_entries` passe de « quelques centaines de lignes » à `≈ chaînes × programmes dans la fenêtre` par compte ; ses 7 index grossissent d'autant (voir D4).
- `GET /api/live/epg` lit désormais des dizaines de milliers de lignes par serveur : son plan dépend des statistiques du planificateur (`ANALYZE` de la migration 023 + `PRAGMA optimize` en fin de pipeline). Les chiffres de D4 donnent les deux cas.
- Réglages : `EPG_SYNC_ENABLED` (false), `EPG_SYNC_INTERVAL_HOURS` (6), `EPG_SYNC_PAST_HOURS` (1), `EPG_SYNC_FUTURE_HOURS` (12), `EPG_SYNC_MAX_BYTES` (256 Mio), `EPG_SYNC_MAX_PROGRAMMES` (500 000 lignes/compte), `EPG_SYNC_BATCH_SIZE` (500), `EPG_SYNC_BATCH_PAUSE_MS` (0), `EPG_SYNC_DOWNLOAD_TIMEOUT_SECONDS` (600). Tous dans `.env.example` (invariant testé par `tests/test_env_example_invariant.py`).
- Non couvert : l'XMLTV compressé en gzip *dans le fichier* (`.xml.gz` servi tel quel) ; seule la compression de transport (`Content-Encoding`) est gérée.
