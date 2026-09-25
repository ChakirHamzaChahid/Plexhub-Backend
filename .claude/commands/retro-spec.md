---
description: Rétro-spécification (spec reconstruite depuis le code) — faits générés, puis fonctionnel par capacité, puis technique. Lecteurs en parallèle, un seul rédacteur, preuves fichier:ligne vérifiées par check.py.
argument-hint: "faits | carte | capacite <CAP-id ou nom> | technique <volet> | check"
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> Lis d'abord la skill **`retro-spec`** (méthode, identifiants, principes) et `.claude/WORKFLOWS.md` (rédacteur unique, routage `model-effort-routing`, garde-fous). ⛔ Rien sous `keystore/`, aucun `.env`, aucune valeur secrète, aucune donnée réelle.

# /retro-spec — $ARGUMENTS

## `faits` — régénérer la base factuelle (aucun sous-agent)

```
python tools/retro-spec/inventaire.py
python tools/retro-spec/schema-db.py          # tbls via Docker local ; sinon --remote (ci-dessous) ou --no-tbls
```
Pas de Docker local : `--remote chakir@192.168.0.175` (Docker du homelab). Seule la base **vide** y part, le dossier distant est effacé ensuite. Authentification par clé SSH de préférence, sinon mot de passe dans la variable `RETRO_SPEC_SSH_PASSWORD` le temps de la commande — **jamais** écrit dans un fichier ni dans le dépôt.

Puis relis `docs/retro-spec/_genere/inventaire.md` et signale ce qui a changé depuis la dernière fois (`git diff --stat docs/retro-spec/_genere`).

## `carte` — carte des capacités

La session principale lit l'inventaire, les PRD et `CLAUDE.md`, puis écrit la carte **00-carte.md** dans `docs/retro-spec/fonctionnel/` :
tableau `CAP-id | capacité | persona | écrans/routes | endpoints | PRD existant | parcours Maestro`, et les parcours de bout en bout TV ↔ Backend en Mermaid. Pas de FR à ce stade.

## `capacite <id>` — fiche d'une capacité

1. Extrais de l'inventaire le périmètre de la capacité (écrans, VM, DAO, workers, endpoints, parcours Maestro).
2. `ROUTAGE` puis **un** lecteur neuf en lecture seule (en général `Explore` en `sonnet` pour une capacité de plusieurs modules — `model:` explicite, le hook l'exige) : il reçoit le périmètre + le modèle `.claude/templates/retro-spec-capacite.md` et **renvoie** un brouillon avec preuves ; il n'écrit rien.
3. La session principale **ouvre au moins 3 preuves** au hasard dans le code ; une preuve fausse = on reprend le brouillon, pas de fiche approximative.
4. Écris `docs/retro-spec/fonctionnel/CAP-XX-nn-<slug>.md`, puis `python tools/retro-spec/check.py` (doit sortir en 0).
5. Si un PRD existe : ajoute en fin de fiche le **tableau d'écart fiche ↔ PRD** (trouvé en plus · raté · contredit).

## `technique <volet>` — volets techniques

Volets : `c4`, `contrat-api`, `navigation`, `securite`, `build-release`, `dette`. Même mécanique : lecteurs en lecture seule, rédaction par la session principale, preuves vérifiées. `db` est **généré** (commandes de `faits`).

## `check`

```
python tools/retro-spec/check.py
python tools/retro-spec/inventaire.py --check
```

## Sortie de fin de commande

Fichiers écrits · exigences créées (par statut) · écarts relevés · résultat de `check.py` · coût (`python .claude/tools/cost-tracker.py report --weeks 1`).
Commit sur `develop` avec chemins explicites ; pas de push sans accord.
