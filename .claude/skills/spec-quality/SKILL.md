---
name: spec-quality
description: >-
  Qualité d'une spec /feature avant le code : Clarifier (≤ 5 questions à choix
  avec option recommandée, tracées dans le PRD), Analyser (relecture en
  lecture seule PRD ↔ board ↔ conventions, 6 passes, sévérités, matrice
  FR → tickets), checklist de qualité des exigences, passe de couverture FR
  pour les audits. À charger en phases 1bis et 2bis de /feature et par
  /wf-audit-incremental.
---

# spec-quality

<!-- Adapté de github/spec-kit (MIT) — templates/commands/clarify.md,
     analyze.md, checklist.md et templates/spec-template.md. Ramené au flux
     /feature du dépôt : pas de dossier specs/, pas de branche par feature
     (tout sur develop), PRD dans docs/10-prd-<feature>.md, board dans
     docs/31-board.md. -->

Trois procédures, chacune courte et bornée. Aucune ne s'applique au **chemin
mineur** de `/feature`.

## Identifiants (communs aux trois)

- User stories : `US1`, `US2`… avec une priorité `P1`/`P2`/`P3` ; chacune est
  **testable seule** (si on ne livre qu'elle, elle apporte déjà quelque chose).
- Scénarios d'acceptation : `US1-AC1`… au format **Given / When / Then**.
- Exigences fonctionnelles : `FR-001`… (une capacité vérifiable par ligne).
- Critères de succès : `SC-001`… mesurables, sans technologie.
- Ambiguïté ouverte : `[NEEDS CLARIFICATION: …]` dans le texte même, **3 au
  maximum** au moment de la rédaction ; au-delà, on choisit une valeur par
  défaut raisonnable et on l'écrit en « Hypothèses ».

Modèle : `.claude/templates/prd-template.md`.

## 1. Clarifier (phase 1bis de /feature)

Par la **session principale**, juste après la rédaction du PRD, avant l'archi.

1. Scanner le PRD sur les **9 catégories** (Clair / Partiel / Manquant) :
   périmètre fonctionnel · modèle de données · parcours et états
   (erreur/vide/chargement) · qualités non fonctionnelles (perf, fiabilité,
   observabilité, sécurité) · intégrations et modes de panne · cas limites ·
   contraintes et compromis · terminologie · signaux de complétude
   (critères testables) — **plus les catégories du dépôt** :
   - PlexHubTV : navigation D-pad / focus / overscan · impact Room
     (schéma, migration, `MediaDao`) · multi-serveurs et agrégation ·
     profils / contrôle parental / contenu adulte · OTA et flavors
     `play`/`sideload` · pièges de la skill `pitfalls` concernés.
   - Backend : contrat d'API (codes, schémas Pydantic, compat client TV) ·
     migration SQLite idempotente · concurrence d'écriture (`db_retry`) ·
     workers / scraping / quotas des API externes · secrets et `X-API-Key`.
2. Ne garder que les trous dont la réponse **change** l'archi, le modèle de
   données, le découpage, les tests ou l'UX. Pas de question de style, pas de
   question d'implémentation.
3. **5 questions au maximum**, les plus fortes en (impact × incertitude),
   posées **une par une** avec l'outil AskUserQuestion : 2 à 4 options
   exclusives, **la recommandée en premier** avec « (Recommandé) » et une
   phrase de raison.
4. Après **chaque** réponse, écrire tout de suite dans le PRD :
   - `## Clarifications` → `### Session AAAA-MM-JJ` → `- Q : … → R : …`
   - et la conséquence dans la section concernée (FR, cas limite, SC,
     non-goal) ; remplacer le `[NEEDS CLARIFICATION]` correspondant.
     Une affirmation contredite est **remplacée**, pas doublée.
5. Fin : afficher le tableau de couverture des catégories (Résolu / Différé /
   Clair / Ouvert). Rien d'ouvert à fort impact → phase 2. Sinon, le dire.

Si aucune catégorie à fort impact n'est Partielle ou Manquante : écrire
« Clarifier : aucune ambiguïté critique » et passer à la phase 2, sans
question.

## 2. Analyser (phase 2bis de /feature)

Avant le GATE de la phase 2, sur le PRD + les lignes board + l'ADR éventuel.
Un **sous-agent neuf en lecture seule** (il ne modifie aucun fichier ; il
renvoie un rapport), couple noté par la grille `model-effort-routing`.
Quand la grille donne C2 = 2 ou C3 = 2, la **même** invocation porte aussi le
challenge d'architecture (pas deux relectures).

Six passes, **50 constats au maximum** (au-delà : résumé du reste) :

| Passe | Cherche |
|---|---|
| A. Doublons | deux FR qui disent la même chose |
| B. Ambiguïtés | adjectifs non mesurés (« rapide », « fluide », « robuste »), `[NEEDS CLARIFICATION]` restants, TODO |
| C. Sous-spécification | FR sans objet ou sans résultat vérifiable, story sans scénario, ticket citant un fichier/composant absent du PRD ou de l'archi |
| D. Loi de la maison | conflit avec `CLAUDE.md` (conventions, DoD) ou avec la skill `pitfalls` (PlexHubTV) / les pièges de `CLAUDE.md` (Backend) |
| E. Couverture | FR sans ticket, ticket sans FR ni story, SC qui exige du travail (perf, sécurité, fiabilité) sans ticket |
| F. Incohérences | même notion nommée deux fois, entité de l'archi absente du PRD, ordre de tickets contraire aux dépendances |

Sévérités :
- **CRITICAL** : conflit avec la loi de la maison ou un piège connu ; FR
  cœur sans aucun ticket ; artefact manquant (pas de scénario, pas de board).
- **HIGH** : FR en conflit ou dupliqué, attribut sécurité/perf ambigu,
  critère d'acceptation non testable.
- **MEDIUM** : dérive de vocabulaire, exigence non fonctionnelle sans
  ticket, cas limite sous-spécifié.
- **LOW** : formulation.

Rapport (dans la réponse, pas dans un fichier) :

```
## Analyse de spec — <feature>
| ID | Passe | Sévérité | Où | Constat | Recommandation |
|----|-------|----------|----|---------|----------------|
| E1 | Couverture | CRITICAL | prd FR-004 | aucun ticket | ajouter au ticket X-2 ou différer en non-goal |

Matrice FR → tickets
| FR | Tickets | Note |
|----|---------|------|

Métriques : FR n · tickets n · couverture % · ambiguïtés n · CRITICAL n
```

Effet sur le GATE : **un CRITICAL bloque** le GATE ; un HIGH est corrigé ou
accepté explicitement par Chakir au GATE (la décision est écrite dans le
PRD, section Clarifications) ; MEDIUM/LOW sont listés. La session
principale corrige elle-même le PRD/board ; l'analyste ne corrige rien.

## 3. Checklist de qualité des exigences (seulement si C3 ≥ 1)

Ce sont des « tests unitaires du texte » : on vérifie que les exigences sont
écrites correctement, **pas** que le code marche. 15 items au maximum,
ajoutés au rapport d'analyse :

```
- CHK001 [Gap] Le comportement quand le serveur est injoignable est-il spécifié ? [PRD §Cas limites]
- CHK002 [Ambiguïté] « chargement rapide » est-il chiffré ? [PRD SC-002]
- CHK003 [Conflit] FR-003 et le non-goal n°2 sont-ils compatibles ? [PRD FR-003]
```

Étiquettes : `[Gap]`, `[Ambiguïté]`, `[Conflit]`, `[Hypothèse]`,
`[Couverture]`. Interdit : « vérifier que le bouton marche » (c'est un test
de code, il va dans les critères d'acceptation).

## 4. Passe de couverture FR (audits)

Pour `/wf-audit-incremental` : pour chaque PRD `docs/10-prd-*.md` dont une
ligne board est `done` dans la fenêtre auditée, relever chaque `FR-###`
(ou, pour un PRD antérieur à ce modèle, chaque critère d'acceptation) et
chercher la preuve : fichier de code **et** test qui le couvrent.

```
| PRD | FR | Code (fichier:ligne) | Test | Statut |
|-----|----|----------------------|------|--------|
| backup-export-import | FR-003 | BackupRepositoryImpl.kt:241 | MediaOverlayBackupTest | couvert |
```

Statuts : `couvert` · `code sans test` (MEDIUM) · `absent` alors que le
ticket est `done` (HIGH — la ligne board ment) · `différé` (non-goal écrit).
