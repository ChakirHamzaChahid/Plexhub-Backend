# CAP-BE-18 — Téléchargement physique Xtream

**Reconstituée le** : 2026-09-25 · recalée le 2026-10-02 (Lot 10) · **HEAD** : `aa3270f` · **Sources** : inventaire, code, tests, PRD `docs/10-prd-media-download.md` (pilote)
**Périmètre** : UI HTMX `/admin/downloads*` (11 routes) + miroir JSON `/api/admin/downloads*` (2 routes), `download_service`, `download_nfo`, `download_worker` (maître seul), tables `download_job`/`download_batch` (migration 018) — voir `docs/retro-spec/_genere/inventaire.md`.

## Résumé

L'opérateur choisit, dans l'onglet admin « Télécharger », un film ou une série du catalogue unifié, puis une source Xtream (ou une source par saison/épisode), et le backend télécharge physiquement les fichiers vidéo sous `DOWNLOAD_DIR`. Une file persistante, drainée par un worker qui ne tourne que dans le process maître, gère la progression, la reprise `Range`, les relances, l'annulation et un `.nfo` à côté de la vidéo. L'URL Xtream (qui contient les identifiants) n'est jamais stockée : elle est reconstruite au moment du transfert. Persona : opérateur (admin). L'app TV ne consomme pas cette capacité.

## User stories observées

- **US-BE-18.1** (P1) — En tant qu'opérateur, je parcours le catalogue unifié films/séries dans l'onglet « Télécharger » afin de choisir un titre.
  - Parcours : `/admin/downloads` → recherche/filtre → fragment versions · Maestro : aucun
- **US-BE-18.2** (P1) — En tant qu'opérateur, je vois les versions d'un titre (source, taille estimée) et, pour une série, un sélecteur par saison puis par épisode, afin de choisir quoi télécharger et depuis quel compte.
  - Parcours : liste → `…/versions` → `…/episodes?season=` · Maestro : aucun
- **US-BE-18.3** (P1) — En tant qu'opérateur, je lance le téléchargement d'un film, d'une série entière, de saisons ou d'épisodes choisis afin d'obtenir les fichiers sur mon serveur.
  - Parcours : fragment versions → `POST /admin/downloads` → panneau de file · Maestro : aucun
- **US-BE-18.4** (P1) — En tant qu'opérateur, je suis la progression, j'annule, je relance ou je nettoie l'historique afin de piloter la file.
  - Parcours : panneau `…/queue` (polling) → cancel / retry / clear-finished · Maestro : aucun
- **US-BE-18.5** (P2) — En tant qu'automate (QA, script), je lis l'état de la file en JSON avec la clé maître afin de superviser sans l'UI.
  - Parcours : `GET /api/admin/downloads[/{job_id}]` · Maestro : aucun

## Exigences fonctionnelles

- **FR-BE-001** — Le système affiche le catalogue unifié films/séries (page + fragment filtrable par type et recherche) en réutilisant `media_service.get_unified_list`. — *implémenté* — preuve `app/api/admin_downloads.py:156-204` · test `tests/test_admin_downloads.py`
- **FR-BE-002** — Toutes les routes `/admin/downloads*` exigent la Basic Auth admin, posée au montage du routeur. — *implémenté* — preuve `app/main.py:685` · test `tests/test_download_security.py:240-254`
- **FR-BE-003** — Le système liste les versions d'un titre (`server_id`, `rating_key`, libellé, `is_broken`) et, pour un film, la taille estimée par source. — *implémenté* — preuve `app/api/admin_downloads.py:220-285` · test `tests/test_admin_downloads.py:145-206`
- **FR-BE-004** — Un `unification_id` inconnu ou un `type` hors `movie|show` donne 404. — *implémenté* — preuve `app/api/admin_downloads.py:232-237` · test `tests/test_admin_downloads.py:206-227`
- **FR-BE-005** — Pour une série, le système propose un sélecteur par saison avec sources multi-comptes et taille agrégée. — *implémenté* — preuve `app/services/download_service.py:727-786`, `app/api/admin_downloads.py:260-271` · test `tests/test_admin_downloads.py:228-266`
- **FR-BE-006** — Pour une saison donnée, le système propose un sélecteur par épisode avec la taille de chaque épisode. — *implémenté* — preuve `app/api/admin_downloads.py:288-317`, `app/services/download_service.py:789-824` · test `tests/test_admin_downloads.py:307-334`
- **FR-BE-007** — La mise en file accepte quatre portées : film (1 job), série entière `series_all` (1 job par épisode + 1 lot), saisons choisies avec une source par saison (`seasons`), épisodes choisis avec une source par épisode (`episodes`) ; une même action peut couvrir plusieurs comptes. — *implémenté* — preuve `app/api/admin_downloads.py:400-437`, `app/services/download_service.py:827-1233` · test `tests/test_admin_downloads.py:355-580`
- **FR-BE-008** — L'ancienne portée `series_seasons` (cases à cocher, une seule source) restait dans `enqueue_selection` alors que le routeur la refusait : aucun chemin UI ni API ne l'atteignait. — *retiré* (Lot 10 : la portée, son paramètre `seasons`, sa branche et le schéma `DownloadEnqueueRequest` sont supprimés ; l'interface propose `seasons`, avec une source par saison) — preuve de l'absence `app/services/download_service.py:827-838` (signature sans `seasons`), `app/api/admin_downloads.py:392-397` (docstring : portée retirée) · test : aucun (le routeur refuse déjà toute portée hors `movie`/`series_all`/`seasons`/`episodes`, `app/api/admin_downloads.py:400-402`)
- **FR-BE-009** — La destination est toujours calculée côté serveur ; le formulaire n'accepte aucun chemin (`type`, `unification_id`, `scope`, `source`, `season_pick`, `episode_pick` seulement). — *implémenté* — preuve `app/services/download_service.py:222-259`, `app/api/admin_downloads.py:377-390` · test `tests/test_download_security.py:277-292`
- **FR-BE-010** — Si `DOWNLOAD_DIR` est vide, la mise en file renvoie une erreur explicite sans créer de job, et le worker ne démarre pas. — *implémenté* — preuve `app/services/download_service.py:851-852`, `app/workers/download_worker.py:200-202`, `app/main.py:507-510` · test `tests/test_download_service.py:162-176`
- **FR-BE-011** — Le chemin final est prouvé confiné sous `DOWNLOAD_DIR` par résolution `realpath` au moment de l'écriture, y compris contre un lien symbolique interne pointant à l'extérieur. — *implémenté* — preuve `app/services/download_service.py:287-299` · test `tests/test_download_confinement.py:208-219`
- **FR-BE-012** — Chaque segment de chemin est assaini : séparateurs et `..` neutralisés, caractères interdits NTFS remappés, caractères de contrôle retirés, longueur bornée, noms réservés Windows suffixés. — *implémenté* — preuve `app/services/download_service.py:152-219` · test `tests/test_download_confinement.py:62-118`
- **FR-BE-013** — L'URL Xtream n'est jamais persistée, loggée ni renvoyée : aucune colonne URL, re-dérivation au worker par `build_stream_url`. — *implémenté* — preuve `app/workers/download_worker.py:600`, `app/models/database.py:543-575` · test `tests/test_download_security.py:162`
- **FR-BE-014** — Le transfert ne suit pas les redirections automatiquement : chaque saut (borné par `DOWNLOAD_MAX_REDIRECTS`) n'est suivi que si l'hôte résout vers une IP publique, sans jamais écrire l'URL dans le message d'erreur. — *implémenté* — preuve `app/services/download_service.py:345-379`, `app/services/download_service.py:452-472` · test `tests/test_download_security.py:328-353`
- **FR-BE-015** — Le transfert écrit dans `<dest>.part` puis promeut par `os.replace` ; sur annulation ou échec, le `.part` est conservé et jamais promu. — *implémenté* — preuve `app/services/download_service.py:476`, `app/services/download_service.py:560` · test `tests/test_download_transfer.py:33-56`
- **FR-BE-016** — La reprise envoie `Range: bytes=<n>-` depuis le `.part` ; un 206 au `Content-Range` incohérent ou un 200 redémarrent de zéro ; un 416 promeut le `.part`. — *implémenté* — preuve `app/services/download_service.py:474-531` · test `tests/test_download_transfer.py:93-218`
- **FR-BE-017** — Si le fichier final existe et qu'aucun `.part` n'est présent, le job se termine sans re-télécharger. — *implémenté* — preuve `app/services/download_service.py:433-437` · test `tests/test_download_transfer.py:219-237`
- **FR-BE-018** — Les réponses amont sont classées : 403/404 et contenu d'erreur (HTML/JSON) = échec définitif ; 5xx/429 = échec transitoire relancé. — *implémenté* — preuve `app/services/download_service.py:496-507` · test `tests/test_download_transfer.py:238-268`
- **FR-BE-019** — Un job `queued`/`running` peut être annulé ; l'écriture est conditionnelle à l'état et le transfert en cours la découvre par une vérification coopérative (~1/s). — *implémenté* — preuve `app/services/download_service.py:1260-1282`, `app/workers/download_worker.py:400-412` · test `tests/test_download_service.py:431-458`
- **FR-BE-020** — Un job `failed`/`canceled` peut être relancé : retour à `queued`, compteur d'essais et erreur remis à zéro, `.part` conservé pour la reprise. — *implémenté* — preuve `app/services/download_service.py:1285-1307` · test `tests/test_download_service.py:465-497`
- **FR-BE-021** — « Vider les terminés » supprime les jobs `completed`/`failed`/`canceled` sans toucher aux jobs actifs. — *implémenté* — preuve `app/services/download_service.py:1310-1324`, `app/api/admin_downloads.py:467-474` · test `tests/test_download_service.py:504-527`
- **FR-BE-022** — Les échecs transitoires sont relancés jusqu'à `DOWNLOAD_MAX_RETRIES` avec attente exponentielle plafonnée à 30 s ; le créneau de concurrence est rendu avant l'attente et le job reste visible `queued`. — *implémenté* — preuve `app/workers/download_worker.py:479-563` · test `tests/test_download_worker.py:247-344`
- **FR-BE-023** — Au démarrage du maître, les jobs restés `running` (process mort) repassent `queued` avant le drain. — *implémenté* (sans test dédié) — preuve `app/workers/download_worker.py:125-140`, `app/workers/download_worker.py:204`
- **FR-BE-024** — Le worker ne tourne que dans le process maître, lancé en tâche de fond sans bloquer le démarrage. — *implémenté* — preuve `app/main.py:498-524` · test `tests/test_download_worker.py` (drain isolé, pas le câblage `lifespan`)
- **FR-BE-025** — Avant chaque transfert, le job échoue définitivement (sans consommer de relance) si l'espace libre sous `DOWNLOAD_DIR` est sous `DOWNLOAD_MIN_FREE_DISK_MB` (0 = désactivé). — *implémenté* — preuve `app/services/download_service.py:262-284`, `app/workers/download_worker.py:642-648` · test `tests/test_download_service.py:296-334`
- **FR-BE-026** — La concurrence est bornée par `DOWNLOAD_CONCURRENCY` (sémaphore). — *implémenté* — preuve `app/workers/download_worker.py:214-215` · test `tests/test_download_worker.py:272-315`
- **FR-BE-027** — Une mise en file qui retrouve un job non terminal pour le même `(server_id, rating_key)` le réutilise ; un job `completed` peut être remis en file. — *implémenté* — preuve `app/services/download_service.py:599-615` · test `tests/test_download_service.py:106-143`
- **FR-BE-028** — Un compte introuvable ou inactif bloque la mise en file (erreur, aucun job) ; s'il devient inactif avant exécution, le job passe `failed`. — *implémenté* — preuve `app/services/download_service.py:864-868`, `app/workers/download_worker.py:596-599` · test `tests/test_download_service.py:186`
- **FR-BE-029** — Après un téléchargement réussi, un `.nfo` (Jellyfin/Kodi) est écrit à côté de la vidéo, en best-effort ; aucune image n'est posée sur disque (le poster n'est qu'une URL dans le NFO). — *partiel* (manque : fichier poster annoncé par F-203) — preuve `app/services/download_nfo.py:50-90`, `app/workers/download_worker.py:323-359` · test `tests/test_download_nfo.py:32-120`
- **FR-BE-030** — Un miroir JSON en lecture seule (`GET /api/admin/downloads`, `GET /api/admin/downloads/{job_id}`) est gardé par la clé maître seule. — *implémenté* — preuve `app/api/downloads.py:33-61` · test `tests/test_download_security.py:278-325`
- **FR-BE-031** — Ni le fragment HTMX de la file ni le JSON n'exposent d'URL ou d'identifiant. — *implémenté* — preuve `app/api/admin_downloads.py:80-112`, `app/models/schemas.py:562-606` · test `tests/test_download_security.py:186-224`
- **FR-BE-032** — La progression (`bytes_done`/`bytes_total`) est persistée au plus ~1 fois/s sans jamais toucher à l'état (une annulation concurrente n'est pas écrasée). — *implémenté* — preuve `app/workers/download_worker.py:380-397` · test : aucun dédié (le throttle voisin d'annulation est testé en `tests/test_download_worker.py:200-246`)
- **FR-BE-033** — Les tables `download_job`/`download_batch` sont créées de façon additive et idempotente (migration 018), `batch_id` sans clé étrangère dure. — *implémenté* — preuve `app/db/migrations.py:808-859`, `app/models/database.py:511-575`
- **FR-BE-034** — Trois métriques Prometheus : jauge `plexhub_download_jobs{state}` recalculée périodiquement, compteur `plexhub_download_bytes_total`, compteur `plexhub_download_failures_total{reason}` à valeurs fermées. — *implémenté* (noms différents du PRD) — preuve `app/utils/metrics.py:175-227`, `app/services/download_service.py:93-147` · test `tests/test_download_metrics.py`
- **FR-BE-035** — Le préfixe `[XXX] ` du dossier d'un film adulte existait dans `compute_dest_path` mais aucun appelant ne passait `is_adult=True` (choix documenté « non appliqué au MVP »). — *retiré* (Lot 10 : le paramètre `is_adult` et l'import de `apply_adult_prefix` sont supprimés de `compute_dest_path` ; le titre n'est jamais préfixé dans les chemins de téléchargement) — preuve de l'absence `app/services/download_service.py:222-259`, docstring `app/services/download_service.py:239-240` · test : aucun
- **FR-BE-036** — API JSON de mutation (mise en file, annulation, relance par `X-API-Key`) annoncée par le PRD (F-201). — *absent* (annoncé dans `docs/10-prd-media-download.md`, introuvable dans le code ; le schéma orphelin `DownloadEnqueueRequest`, sans route, a été retiré au Lot 10) — preuve de l'absence : plus aucune occurrence de `DownloadEnqueueRequest` dans `app/`, `app/models/schemas.py:612-620`

## Règles métier

- **RG-BE-18.1** — Un film n'a jamais de lot (`batch_id` nul) ; toute mise en file de série crée exactement un `DownloadBatch`. — preuve `app/services/download_service.py:888-913`, `app/services/download_service.py:937-948`
- **RG-BE-18.2** — Le `total_jobs` d'un lot ne compte que les jobs neufs rattachés à ce lot ; un job dédupliqué garde son lot d'origine. — preuve `app/services/download_service.py:991-993`
- **RG-BE-18.3** — Une saison ou un numéro d'épisode absent vaut `0` pour le nommage et le tri. — preuve `app/services/download_service.py:252-255`
- **RG-BE-18.4** — Toute écriture terminale du worker est conditionnée à `state='running'` : une annulation concurrente l'emporte toujours. — preuve `app/workers/download_worker.py:427-463`
- **RG-BE-18.5** — L'assainissement à la mise en file n'est qu'une défense en profondeur ; seule `resolve_confined` au worker prouve le confinement. — preuve `app/services/download_service.py:237-239`
- **RG-BE-18.6** — Le repli 403 vers une URL de lecture directe n'existe que pour les jobs Plex ; un job Xtream n'a jamais d'URL de repli. — preuve `app/workers/download_worker.py:581-594`
- **RG-BE-18.7** — Une version marquée cassée (`is_broken`) reste téléchargeable : aucun garde ne la bloque. — preuve `app/api/admin_downloads.py:248`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| `DOWNLOAD_DIR` vide (mise en file) | Erreur « DOWNLOAD_DIR n'est pas défini », aucun job | `app/services/download_service.py:851-852` |
| `DOWNLOAD_DIR` vide (démarrage) | Worker non lancé, simple log | `app/main.py:507-510` |
| Compte introuvable/inactif | Mise en file refusée ; au worker, job `failed` | `app/workers/download_worker.py:596-599` |
| Chemin résolu hors `DOWNLOAD_DIR` | Job `failed` « chemin de destination invalide », métrique `confinement` | `app/workers/download_worker.py:605-616` |
| Disque presque plein | Job `failed` sans relance, métrique `disk_full` | `app/services/download_service.py:281-284` |
| 403/404 amont | Échec définitif `failed` | `app/services/download_service.py:496-497` |
| 5xx/429 amont | Relance avec attente, puis `failed` si budget épuisé | `app/workers/download_worker.py:479-563` |
| Redirection vers IP privée / métadonnées cloud | Rejetée avant tout fetch, échec définitif | `app/services/download_service.py:345-379` |
| Reprise avec `Content-Range` incohérent | Redémarrage à zéro (pas de trou ni doublon) | `app/services/download_service.py:509-525` |
| Fichier final déjà présent | `completed` sans transfert | `app/services/download_service.py:433-437` |
| Annulation pendant le transfert | `.part` gardé, jamais promu, état `canceled` | `app/services/download_service.py:565-566` |
| Redémarrage avec job `running` | Remis `queued` au boot du maître | `app/workers/download_worker.py:125-140` |
| Sélection saisons/épisodes vide | Erreur, aucun job | `app/services/download_service.py:1134-1135` |
| Un épisode choisi est inconnu | Ignoré ; erreur seulement si tous les choix échouent | `app/services/download_service.py:1208-1219` |
| Compte en panne fournisseur (§5.13) | Aucun traitement dédié trouvé : seul `is_active` est testé | `app/services/download_service.py:618-628` |

## Dépendances

- Autres capacités : `CAP-BE-09` (catalogue unifié), `CAP-BE-10` (`build_stream_url`), `CAP-BE-04` (comptes Xtream), `CAP-BE-16` (`nfo_builder` pour le sidecar), `CAP-BE-19` (même worker, branche Plex), `CAP-BE-20` (écran unifié qui réutilise cette file), `CAP-BE-17` (garde SSRF partagée).
- Autre dépôt : aucun appel de PlexHubTV vers `/admin/downloads` ni `/api/admin/downloads`. Le module TV `feature/downloads` est un téléchargement local à l'appareil, sans lien réseau avec cette capacité (`tv:app/src/main/java/com/chakir/plexhubtv/feature/downloads/DownloadsViewModel.kt`).
- Données : `download_job`, `download_batch` (lecture de `media`, `xtream_accounts`) — voir `docs/retro-spec/technique/db/`.

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| Portée `series_seasons` inatteignable (FR-BE-008) | code mort (résolu au Lot 10 : retirée) | aucune |
| Préfixe `[XXX]` jamais appliqué (FR-BE-035) | code mort (P2 assumé) | câbler F-202 ou retirer le paramètre |
| `DownloadEnqueueRequest` sans route (FR-BE-036) | code mort (schéma retiré au Lot 10) / spec ↔ code (API JSON toujours absente) | idem F-201 |
| `reap_orphans` sans test dédié (FR-BE-023) | test manquant | test sous vrai verrou WAL (§9 piège 8) |
| Câblage `lifespan` maître-seul non testé (FR-BE-024) | test manquant | test d'intégration du démarrage |
| Migration 018 sans test dédié (FR-BE-033) | test manquant | test de rejouabilité |

## Questions ouvertes

- Un compte en panne fournisseur (`outage_since` posé, `is_active` inchangé) laisse-t-il ses jobs tourner et échouer en boucle jusqu'à `DOWNLOAD_MAX_RETRIES` ? Le code ne consulte pas `account_outage_service` — **à vérifier par test**.
- Le PRD F-102 demande de comparer l'espace libre à la somme des `Content-Length` connus ; le code compare à un seuil fixe, juste avant chaque job. Un lot de série plus gros que l'espace libre mais sous le seuil remplit-il le disque job après job ? — **à vérifier par test**.
- Avec `DOWNLOAD_CONCURRENCY > 1` et un compte `max_connections=1`, le fournisseur refuse-t-il la seconde connexion (403/5xx) ? Aucun throttle par compte n'existe côté downloads, contrairement à `/dav` — **à vérifier par test**.
- Le PRD (§10 Q1) évoque un futur `overwrite=true` : aucune trace dans le code ; abandon ou report ?

## Écart fiche ↔ PRD (pilote)

PRD : `docs/10-prd-media-download.md` (exigences F-001→F-203, lignes 52-66 ; non-goals §6, lignes 136-147).

| PRD | Ligne PRD | Fiche | Verdict |
|---|---|---|---|
| F-001 Onglet + liste unifiée | 52 | FR-BE-001, FR-BE-002 | couvert |
| F-002 Versions + option série complète | 53 | FR-BE-003, FR-BE-004, FR-BE-007 | couvert |
| F-003 Mise en file + `.part`→rename | 54 | FR-BE-007, FR-BE-009, FR-BE-015 | couvert |
| F-004 États & progression, polling | 55 | FR-BE-019, FR-BE-032 | couvert |
| F-005 Reprise, relances, reprise au boot | 56 | FR-BE-016, FR-BE-020, FR-BE-022, FR-BE-023 | couvert (reprise au boot non testée) |
| F-006 Annulation | 57 | FR-BE-019 | couvert |
| F-007 Confinement + garde config | 58 | FR-BE-009→FR-BE-012 | couvert |
| F-008 Migration 018 additive | 59 | FR-BE-033 | couvert (pas de test dédié) |
| F-101 JSON lecture, clé maître | 60 | FR-BE-030, FR-BE-031 | couvert |
| F-102 Préflight disque | 61 | FR-BE-025 | **partiel** : seuil fixe, pas de somme des `Content-Length`, contrôle au job et non à la mise en file |
| F-103 Métriques `plexhub_downloads_total{result}`, `plexhub_downloads_active` | 62 | FR-BE-034 | **contredit** : noms et modèle différents (jauge par état + échecs par raison) |
| F-104 Vider les terminés | 63 | FR-BE-021 | couvert |
| F-201 JSON mutation (P2) | 64 | FR-BE-036 | **absent** |
| F-202 Préfixe `[XXX]` (P2) | 65 | FR-BE-035 | **mort** (câblé, jamais appelé) |
| F-203 NFO + poster (P2) | 66 | FR-BE-029 | **partiel** : NFO livré, pas d'image |
| Non-goal « pas de choix saison/épisode au MVP » | 146 | FR-BE-005, FR-BE-006, FR-BE-007 | **contredit** : livré depuis (évolution « X2 »), PRD non mis à jour |

**Trouvé en plus du PRD** : garde SSRF par saut de redirection (FR-BE-014) ; remappage NTFS des noms (FR-BE-012, plus le script `app/scripts/rename_download_illegal_chars.py`) ; classification fine des échecs (FR-BE-018, FR-BE-034) ; déduplication des jobs (FR-BE-027) ; skip-if-exists (FR-BE-017, tranché en §10 Q1 du PRD) ; relance sans bloquer la concurrence (FR-BE-022) ; sélection multi-comptes dans une même action (FR-BE-007).
**Raté par la fiche** : rien d'identifié du PRD resté sans correspondance.
**Faux dans le PRD aujourd'hui** : noms des métriques (F-103) et non-goal de granularité (§6).
