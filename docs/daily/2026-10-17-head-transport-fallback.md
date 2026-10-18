# Validation des flux : repli après erreur de transport HEAD

Date : 2026-10-17. Base locale : develop, HEAD 472a74b. Correctif sans migration ni changement de version applicative.

## Comportement

`_check_one` intercepte `httpx.TransportError` sur HEAD uniquement, puis conserve la classification normale du GET Range `bytes=0-8191`. Les HEAD 403/404 restent définitifs. La taille est récupérée depuis le total Content-Range. Un GET 416 après HEAD sans réponse reste un échec, faute de preuve HEAD positive.

Chaque repli incrémente `head_transport_fallback` dans le récapitulatif du run et le compteur Prometheus `plexhub_head_transport_fallback_total`. Un ContextVar transporte le dictionnaire du run dans ses tâches ; les verdicts GET restent inchangés. Le sémaphore du compte couvre HEAD et GET.

## Vérifications

- Avant correction : 10 nouveaux tests en échec ; les GET ne sont jamais appelés après ReadError.
- Suite complète : 2363 passed, 4 skipped, couverture 84,11 %, 324,67 s.
- Après ajout du cas GET 416 : ensemble ciblé final 70 passed, 10,64 s ; ruff check et git diff --check verts.
- Deux tests intégrés cron/pipeline : compte simulé masqué à 58 strikes, 12 HEAD coupés et 12 GET 206 ; compteur remis à 0 par le service existant, outage_since NULL et médias visibles.
- Image candidate Docker/Linux : conteneur healthy, health 200, isMaster true. Serveur TCP local fermant HEAD : 12/12 contrôles sains, 12 HEAD + 12 GET Range, concurrence 1, taille 2000000000.

## Déploiement et preuve réelle

Cible autorisée : chakir@192.168.0.175 ; service Compose plexhub-backend, conteneur plexhub-backend-xtream. Les empreintes des deux fichiers de l’image initiale correspondaient exactement à HEAD local avant modification.

Image locale : plexhub-backend:head-fallback-20261017, ID 42bdf89f8a00509245551938b20137370c3faa31ff272e11105ccdfb7886bf5c. Construite depuis l’image de production af8d20983c107a792dd175bc5b91a8b4edcfe9b9461cebd469e9dd78d1589f12, avec seulement health_check_worker.py et metrics.py remplacés. Aucune publication GHCR ni tag de release.

Configuration persistante : /home/chakir/docker-media-stack/docker-compose.override.yml, uniquement le service concerné, image locale et pull_policy never. Watchtower désactivé pour ce service afin de conserver le correctif jusqu’à une prochaine release officielle. À cette release, retirer cette surcharge après avoir vérifié que l’image officielle inclut le correctif.

Arrêt propre, checkpoint WAL hors conteneur (0,0,0), recréation du seul backend ; conteneur healthy. Aucun téléchargement actif au préalable. Le conteneur isolé de smoke a été supprimé.

Validation réelle ciblée via le worker cron : sélection temporaire limitée à 12 films autorisés du compte 8fb2c0f3 dans un processus séparé, client réseau réel, sémaphore normal ; aucune modification du code déployé pour cette sélection. Résultat : 12 checked, 0 broken, reasons {head_transport_fallback: 12, get_ct_video: 12}. État DB après le run : outage_strikes=0, outage_since=NULL. Pas de remise à zéro SQL manuelle.

GET /api/media/movies?serverId=xtream_8fb2c0f3&limit=1 : 200, total 35254, un film retourné.

POST /api/sync/validate-streams : 202, jobId validation_f0d8c9a75f28. Le run global reste en cours : 252845 candidats ; 162012 sur 8fb2c0f3, concurrence 1. Les premières lignes confirment que le fournisseur 05fd75e9 continue de déclencher son disjoncteur sur ses erreurs propres (2/50 strikes), puis que la validation passe à 8fb2c0f3. Ce run global n’est pas présenté comme terminé.

## Conventions

Les packs de house-conventions vivent sous .claude/knowledge dans ce checkout ; le chemin .Codex/knowledge indiqué dans la compétence est absent. Les faits actuels du code et de pyproject.toml prévalent sur les anciennes indications des packs.
