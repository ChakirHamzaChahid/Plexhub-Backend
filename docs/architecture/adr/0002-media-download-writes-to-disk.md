# ADR 0002 — Téléchargement physique de médias : file persistante + worker master-only + confinement d'écriture

- Statut : accepté (tech-lead)
- Date : 2026-07-14
- Contexte source : `docs/10-prd-media-download.md`, spec `docs/20-impl-media-download.md`
- Portée : backend PlexHub (FastAPI / SQLite WAL)

## Contexte

Jusqu'ici PlexHub est **référence-only** : la bibliothèque générée est un arbre de `.strm` pointant vers des URLs
Xtream. La feature « Télécharger » introduit la **première** écriture réelle des octets vidéo sur disque. Trois forces
structurantes :

1. **Téléchargements longs** — indépendants du cycle requête HTMX, doivent survivre à un redémarrage, être repris et bornés
   en bande passante.
2. **État partagé multi-process** — plusieurs workers uvicorn servent les routes admin, mais l'app élit **un master**
   (`fcntl.flock`) qui seul porte le pipeline planifié. Il faut décider où vit le drain de la file.
3. **Surface sécurité neuve** — écrire des fichiers d'après un titre issu du catalogue (potentiellement hostile) et
   manipuler des URLs Xtream qui **portent les credentials dans le path**. La dette CR-S01 (`outputDir` client verbatim de
   `POST /api/plex/generate`) est le contre-exemple à ne pas reproduire.

## Décision

1. **File persistante en DB (migration 018).** `download_job` (1 ligne = 1 fichier) + `download_batch` (regroupe une série).
   L'état (`queued/running/completed/failed/canceled`) et la progression (`bytes_done`/`bytes_total`) sont **persistés** →
   survivent au redémarrage, lisibles sans compteur mémoire. Table additive, idempotente, en fin de chaîne (comme 017).

2. **Worker de drain master-only, piloté par la DB.** `run_drain_loop` ne démarre que dans le `if is_master:` du lifespan.
   Les routes admin (enqueue/cancel/retry) tournent sur **n'importe quel** worker et n'écrivent que des lignes DB ; le master
   les draine par **poll** (`DOWNLOAD_POLL_INTERVAL`). On n'utilise **pas** d'event mémoire : il ne traverserait pas les
   process. Concurrence bornée par `DOWNLOAD_CONCURRENCY` (défaut 1). Au boot, `reap_orphans` remet tout `running` → `queued`.

3. **Concurrence/annulation via transitions SQL conditionnelles.** Claim = `UPDATE … WHERE id AND state='queued'` ;
   progress = `UPDATE … bytes_* WHERE id` (ne touche jamais `state`) ; cancel = `UPDATE … state='canceled' WHERE state IN
   ('queued','running')` ; terminal = `UPDATE … WHERE id AND state='running'`. Le `cancel_check` du worker **relit `state`**
   depuis la DB → l'annulation traverse les process ; un cancel concurrent gagne toujours (le terminal affecte 0 ligne).

4. **Destination 100 % serveur-side, confinée par `realpath`.** Aucun chemin client. `compute_dest_path` sanitize chaque
   segment (NFC, retrait séparateurs/contrôles/`..`, cap longueur) ; `resolve_confined` **prouve** que le chemin absolu résolu
   reste sous `DOWNLOAD_DIR` (`os.path.realpath` + `base in parents`), sinon `PathConfinementError`. Invariant « 0 fichier hors
   `DOWNLOAD_DIR` » **testé**.

5. **Écriture `.part` + rename atomique + reprise `Range`.** On écrit dans `<dest>.part`, on promeut par `os.replace` (atomique,
   même FS) **au succès seulement**. Un `.part` présent + amont supportant `Range` → reprise depuis l'octet déjà pris ; sinon
   restart complet. Un cancel/échec conserve le `.part` (jamais promu).

6. **`run_with_retry` sur tous les writers** (request-path **et** worker) — on n'ajoute pas la dette CR-C04.

7. **Credentials jamais exposés.** L'URL Xtream est **re-dérivée** au worker via `build_stream_url` et **jamais** persistée,
   loggée ni renvoyée. `_safe_error` borne les messages d'échec. Les logs ne citent que `job_id`/`dest`.

## Alternatives écartées

- **Drain sur chaque worker** — double-run des transferts, contention disque/bande passante, pas d'autorité unique. Écarté au
  profit du master-only cohérent avec le pipeline existant.
- **Event mémoire de réveil (au lieu du poll)** — ne traverse pas les process (enqueue sur slave, drain sur master). Le poll DB
  est le seul canal fiable. Le coût (une requête indexée `WHERE state='queued'` toutes les 2 s) est négligeable.
- **Chemin de destination fourni par le client (comme `POST /api/plex/generate`)** — c'est exactement CR-S01. Écarté :
  destination dérivée + confinée serveur-side.
- **Fichier direct sans `.part`** — un transfert interrompu laisserait un fichier partiel indistinguable d'un fichier complet.
  Le `.part` + rename atomique rend l'état sur disque non ambigu.

## Conséquences

- **+** File résiliente (survit au reboot), reprise `Range`, annulation propre cross-process, invariant d'écriture prouvé,
  aucune fuite de creds, additif (0 régression sur `/api/media`, `/api/plex`, la génération `.strm`).
- **−** Le master est le seul draineur : si le master tombe, la file avance à la prochaine élection (acceptable — pas de SLA
  temps-réel). Débit borné par `DOWNLOAD_CONCURRENCY` (choix de sûreté, configurable).
- **Dette assumée MVP** : progression = débit **moyen** (pas instantané) ; pas de purge/rétention auto ; pas de préflight disque
  dur (P1) ; `main.py` (god-file A05) gagne 2 lignes de wiring worker — borné, seul éditeur = lot wiring.

## Références

- Spec : `docs/20-impl-media-download.md`
- PRD : `docs/10-prd-media-download.md`
- Board : `docs/31-board.md`
- Code cité : `app/main.py:229-234` (élection master), `app/services/stream_service.py::build_stream_url`,
  `app/utils/db_retry.py::run_with_retry`, `app/db/migrations.py:707` (patron 017), `app/api/deps.py::verify_master_key`.
</content>
