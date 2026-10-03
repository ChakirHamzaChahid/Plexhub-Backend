# PRD — <Nom de la feature>

<!-- Modèle adapté de github/spec-kit (MIT) — templates/spec-template.md.
     Copier vers docs/10-prd-<feature>.md. Procédures associées : skill
     `spec-quality` (Clarifier en phase 1bis, Analyser en phase 2bis).
     Écrire le QUOI et le POURQUOI ; le COMMENT va dans l'archi/ADR. -->

**Créé** : AAAA-MM-JJ · **Statut** : brouillon | clarifié | analysé | GATE OK
**Demande d'origine** : « <objectif tel que donné à /feature> »

## Problème

<Qui souffre de quoi aujourd'hui, en 3-5 lignes. Chiffre ou constat à l'appui si possible.>

## User stories (par priorité)

<!-- Chaque story est TESTABLE SEULE : si on ne livrait qu'elle, elle apporterait déjà quelque chose. -->

### US1 — <titre court> (P1)

<Le parcours en langage courant.>

**Pourquoi P1** : <valeur, raison de la priorité>
**Test indépendant** : <comment on la vérifie seule — action + résultat observable>

**Scénarios d'acceptation**
- **US1-AC1** — **Given** <état initial>, **When** <action>, **Then** <résultat attendu>
- **US1-AC2** — **Given** …, **When** …, **Then** …

### US2 — <titre court> (P2)

…

## Cas limites

- Que se passe-t-il quand <condition limite> ?
- Comment le système gère <erreur / panne / vide> ?

## Exigences fonctionnelles

<!-- Une capacité vérifiable par ligne. Ambiguïté ouverte : [NEEDS CLARIFICATION: …]
     (3 au maximum ; au-delà, valeur par défaut raisonnable écrite en Hypothèses). -->

- **FR-001** : Le système DOIT <capacité précise>.
- **FR-002** : L'utilisateur DOIT pouvoir <interaction>.
- **FR-003** : Le système DOIT <comportement> [NEEDS CLARIFICATION: <ce qui manque>].

### Entités clés *(si la feature touche des données)*

- **<Entité>** : <ce qu'elle représente, attributs clés, relations — sans implémentation>

## Contraintes du dépôt

<!-- Cocher / compléter ce qui s'applique ; « n/a » sinon. -->
- Contrat d'API (routes, codes, schémas Pydantic, compat client TV) : …
- Migration SQLite (idempotente, en fin de `run_migrations()`) : aucune | additive | Risky
- Concurrence d'écriture (`db_retry`) / workers : …
- API externes (quotas, pannes) : …
- Secrets, `X-API-Key`, SSRF : …

## Non-goals

- <ce que cette version ne fait PAS, et pourquoi>

## Critères de succès

<!-- Mesurables, sans technologie. -->
- **SC-001** : <métrique chiffrée, ex. « l'écran s'affiche en moins de 800 ms sur Mi Box S »>
- **SC-002** : …

## Hypothèses

- <valeur par défaut retenue faute de précision, et pourquoi>

## Décisions et options écartées

| Décision | Retenue | Écartée(s) et raison |
|---|---|---|
| … | … | … |

## Clarifications

<!-- Rempli par la phase 1bis (Clarifier) et par les arbitrages du GATE. -->

### Session AAAA-MM-JJ

- Q : <question> → R : <réponse retenue>
