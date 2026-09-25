---
name: retro-spec
description: >-
  Méthode de rétro-spécification (spec reconstruite à partir du code existant)
  de PlexHubTV + PlexHub Backend : fonctionnelle par capacité (US, FR-TV-### /
  FR-BE-###, statut, preuves fichier:ligne), puis technique (C4, modules,
  données, contrat d'API, navigation, sécurité, build, dette). À charger par
  /retro-spec, et par /sync-context quand un lot touche une capacité décrite.
---

# retro-spec

<!-- Démarche inspirée de github/spec-kit (MIT) et de l'extension communautaire
     « Brownfield Bootstrap » (MIT) : on décrit ce qui EXISTE, preuves à l'appui,
     avant de décider quoi changer. Aucune de ces deux briques n'est installée. -->

## Principes

1. **Décrire, pas inventer.** Une exigence n'entre dans la rétro-spec que si le
   code (ou un test) la prouve. Ce qui est annoncé ailleurs (PRD, CLAUDE.md,
   README) sans être trouvé dans le code est noté **`absent`** — c'est une
   information précieuse, pas une erreur à masquer.
2. **Chaque exigence a sa preuve** : `chemin:ligne` (ou `chemin:ligne-ligne`)
   vers le code qui la réalise, et le test qui la couvre s'il existe. Preuve
   vers l'autre dépôt : préfixe `backend:` ou `tv:`.
3. **Un seul rédacteur.** Les lecteurs (sous-agents en lecture seule, couple noté
   par la grille `model-effort-routing`) analysent en parallèle et **renvoient**
   un brouillon ; seule la session principale écrit dans `docs/retro-spec/`.
4. **Les faits générés ne s'écrivent pas à la main** : `docs/retro-spec/_genere/`
   (inventaire) et `docs/retro-spec/technique/db/` (tbls) sont régénérés par les
   outils, jamais édités.
5. **Jamais de secret ni de donnée réelle** : on décrit un mécanisme (« le token
   Plex est chiffré par `SecurePreferencesManager` »), jamais une valeur ; la
   base de données n'est documentée que par son schéma reconstruit **vide**.
   ⛔ Rien sous `keystore/`, aucun `*.jks`, aucun `.env`.

## Identifiants

| Objet | Format | Exemple |
|---|---|---|
| Capacité | `CAP-TV-nn` / `CAP-BE-nn` | `CAP-TV-07` Sauvegarde & transfert |
| Exigence fonctionnelle | `FR-TV-###` / `FR-BE-###` (uniques par dépôt, jamais réutilisés) | `FR-TV-041` |
| User story | `US-TV-nn.k` | `US-TV-07.1` |
| Règle métier | `RG-TV-nn.k` | `RG-TV-07.3` |

Statut de chaque FR : **`implémenté`** · **`partiel`** (préciser ce qui manque)
· **`mort`** (code présent mais inatteignable depuis l'UI ou l'API) ·
**`absent`** (annoncé ailleurs, introuvable).

## Phase 0 — faits (outils, sans IA)

```
python tools/retro-spec/inventaire.py      # écrans, routes, VM, DAO, workers, endpoints, parcours Maestro…
python tools/retro-spec/schema-db.py       # schéma vide -> tbls (Docker) -> docs/retro-spec/technique/db/
```

## Phase 1 — fonctionnel

1. **Carte** (fichier **00-carte** de `docs/retro-spec/fonctionnel/`) : capacités, personas,
   parcours de bout en bout TV ↔ Backend. Sources : inventaire (routes, écrans,
   préfixes de textes, endpoints), PRD `docs/10-prd-*.md`, `CLAUDE.md`.
2. **Une fiche par capacité**, modèle `.claude/templates/retro-spec-capacite.md`.
   Pour chaque capacité, un lecteur neuf en lecture seule reçoit : la ligne de
   carte, la liste des écrans/VM/DAO/endpoints concernés (extraite de
   l'inventaire) et le modèle ; il renvoie un brouillon avec preuves. La
   session principale vérifie **au moins 3 preuves au hasard** en ouvrant le
   code, puis écrit la fiche.
3. **Contrôle** : `python tools/retro-spec/check.py` doit sortir en 0.
4. **Pilote obligatoire** avant généralisation : une capacité qui a déjà un PRD
   (Backend : téléchargements, `docs/10-prd-media-download.md`). Mesurer l'écart fiche ↔ PRD : ce que
   la fiche trouve en plus, ce qu'elle rate, ce qui est faux.

## Phase 2 — technique (`docs/retro-spec/technique/`)

**00-c4** (contexte, conteneurs, composants en Mermaid) · `db/` (généré) · **contrat-api** (Backend : le contrat, source
unique ; PlexHubTV : les appels réels en face, par endpoint) · **navigation**
(TV) · **securite** (mécanismes, jamais de valeurs) · **build-release** ·
**dette**. Les flux clés restent dans `docs/architecture/flux-cles.md` : on
y renvoie, on ne les recopie pas.

## Phase 3 — traçabilité

- Chaque parcours `.maestro/*.yaml` est relié aux FR qu'il couvre ; liste des
  FR `P1` sans parcours. L'exécution sur la Mi Box S se fait **sur accord** de
  Chakir (la box est une ressource partagée).
- `/wf-audit-incremental` (§4bis) s'appuie sur ces FR ; `/sync-context` met à
  jour la fiche d'une capacité touchée et relance `check.py`.

## Anti-patterns

- ❌ Une FR paraphrasée depuis un PRD sans preuve dans le code.
- ❌ Recopier des tables d'inventaire dans une fiche (on y renvoie).
- ❌ Documenter une valeur de configuration, un token, une URL privée.
- ❌ Écrire en parallèle depuis plusieurs sous-agents.
