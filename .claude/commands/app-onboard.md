---
description: Adopte un service backend EXISTANT — détecte la stack, rétro-ingénierie de l'architecture as-built, génère les docs baseline + CLAUDE.md avant d'auditer ou d'étendre
argument-hint: [chemin du service, optionnel — défaut = répertoire courant]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** ⚠️ Pour CE repo, l'équivalent natif complet est **`/refresh-context`** (a0-cartographer, seul autorisé à éditer `CLAUDE.md`). `/app-onboard` sert pour adopter un **autre** service existant.

# /app-onboard — Intégrer un service existant dans l'équipe

Chemin du service cible (optionnel, défaut = répertoire courant) : $ARGUMENTS

À utiliser quand le projet a déjà du code. Produit la même baseline que le flux greenfield, mais rétro-ingénierée depuis ce qui existe réellement — pour que `/app-audit` et `/app-build` aient une référence.

## Étapes

1. **Invoque le skill `brownfield-onboarding`** et suis-le. Lance d'abord sa détection : scanne la cible pour les marqueurs Python/backend — `requirements.txt`/`pyproject.toml`, `app/main.py` ou équivalent ASGI/WSGI, dossier de migrations, `Dockerfile`/`docker-compose.yml`, CI. Imprime un bloc court « ce que j'ai trouvé » (framework, versions, layout des modules, présence de CLAUDE.md / CI / observabilité). Si le répertoire ne contient pas de service, stoppe et suggère `/app-init` (greenfield).

2. **Invoque `house-conventions`** pour cadrer la rétro-ingénierie contre les standards maison (packs `python-conventions`, `api-conventions`, `stack-defaults`, `git-workflow`, `observability`).

3. **Spawn en parallèle** (un seul message), chacun lisant le code et écrivant un instantané *as-built* — décrire ce qui existe, marquer les suppositions `(inferred)`, ne rien changer au code :
   - `cto` + `tech-lead` → `docs/20-architecture.md` (stack réelle depuis les fichiers de deps, layering, persistance/migrations, tâches de fond/scheduler, auth, CI, déploiement) et `docs/22-impl-spec-backend.md`.
   - `cpo` → `docs/10-prd.md` : inventaire des capacités dérivé des routers/endpoints et des workers. Ne demande à l'utilisateur que l'intention produit illisible dans le code.
   - `devops-engineer` → `docs/23-git-strategy.md` : modèle de branches / CI / packaging actuels vs cible House KB, en signalant les problèmes d'hygiène de secrets.

4. **Génère `CLAUDE.md`** à la racine du projet s'il manque (ou propose une MAJ d'un CLAUDE.md maigre) : stack réelle, commandes build/run/test, noms canoniques trouvés dans le code, seedé du House KB. Ne jamais écraser un `CLAUDE.md` substantiel — proposer un diff à la place.

5. **Résumé.** Imprime les docs produits, la stack détectée, et la suite suggérée : `/app-audit` pour noter le service contre le House KB et construire le plan de remédiation.

## Sécurité

- Lecture seule sur le code source pendant l'onboarding — cette étape prend une photo, elle ne rénove pas.
- Ne pas inventer d'intention produit ; poser une question ciblée ou marquer `(inferred)`.
- Jamais d'écrasement d'un `CLAUDE.md`/doc d'architecture existant ; proposer les changements pour review.
