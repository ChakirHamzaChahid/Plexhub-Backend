# CAP-XX-nn — <Nom de la capacité>

<!-- Modèle de fiche de rétro-spécification (skill `retro-spec`). Copier vers
     docs/retro-spec/fonctionnel/CAP-XX-nn-<slug>.md. On décrit ce qui EXISTE :
     chaque exigence porte sa preuve `chemin:ligne` ; ce qui est annoncé ailleurs
     mais introuvable est marqué `absent`. Aucune valeur secrète. -->

**Reconstituée le** : AAAA-MM-JJ · **HEAD** : `<sha>` · **Sources** : inventaire, code, tests, PRD éventuel
**Périmètre** : écrans / routes / endpoints / workers concernés (renvoyer à `docs/retro-spec/_genere/inventaire.md`)

## Résumé

<Ce que la capacité permet à l'utilisateur, en 3-5 lignes, et pour qui.>

## User stories observées

- **US-XX-nn.1** (P1) — En tant que <persona>, je <action> afin de <bénéfice>.
  - Parcours : <écran → écran → …> · Maestro : `<flow>.yaml` ou « aucun »

## Exigences fonctionnelles

<!-- Statut : implémenté · partiel · mort · absent. Une preuve au moins, sauf `absent`. -->

- **FR-XX-###** — Le système <capacité précise>. — *implémenté* — preuve `chemin:ligne` · test `chemin:ligne`
- **FR-XX-###** — … — *partiel* (manque : …) — preuve `chemin:ligne`
- **FR-XX-###** — … — *absent* (annoncé dans `docs/10-prd-….md`, introuvable dans le code)

## Règles métier

- **RG-XX-nn.1** — <règle, seuil, priorité, cas de fusion…> — preuve `chemin:ligne`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| <réseau coupé, liste vide, serveur injoignable…> | <…> | `chemin:ligne` |

## Dépendances

- Autres capacités : `CAP-…`
- Autre dépôt : endpoints `backend:…` / appels `tv:…`
- Données : tables (renvoyer à `docs/retro-spec/technique/db/`)

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| <PRD dit X, le code fait Y> | spec ↔ code · code mort · test manquant | <ticket, ou « à trancher par Chakir »> |

## Questions ouvertes

- <ce que le code ne permet pas de trancher>
