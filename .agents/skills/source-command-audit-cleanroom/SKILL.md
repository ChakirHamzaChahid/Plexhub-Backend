---
name: "source-command-audit-cleanroom"
description: "Audit « clean-room » (table rase, SANS historique) — diagnostic 360° indépendant jugé sur le code + serveur lancé, schéma d'ID neuf CR-*. Sortie docs/audit/cleanroom-<date>/. Délègue à cleanroom-auditor."
---

# source-command-audit-cleanroom

Use this skill when the user asks to run the migrated source command `audit-cleanroom`.

## Command Template

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Dév **directement sur `develop`** (lecture seule ici ; `main` = release only). Lis `.Codex/WORKFLOWS.md` + `AGENTS.md` §2/§3/§5/§9. Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

# /audit-cleanroom — diagnostic indépendant à neuf

Objectif : un audit complet et INDÉPENDANT, non pollué par les audits précédents, pour repartir d'une
photo fiable. **Lecture seule** du code ; sortie dans un dossier neuf isolé.

Étapes (Manager) :
1. Lis le code + `AGENTS.md` §1–3/§9 pour le modèle mental. **NE consulte PAS** `docs/audit/**` ni les anciens rapports — c'est tout l'intérêt.
2. Boot **benchmark** pour les mesures : `uvicorn app.main:app` démarre, `GET /api/health` 200, `pytest -v` vert, latence des endpoints chauds (`curl -w`, cold-start IA noté à part).
3. Délègue à l'agent **`cleanroom-auditor`** le mandat complet :
   modèle mental d'abord → audit à neuf par **dimension** (archi/§2, conventions/§3, flux/§5, sécurité — auth `X-API-Key`/secrets/Fernet/CORS, perf/latence, tests/couverture, dette/§10) → findings **`CR-*`** → écrit `docs/audit/cleanroom-<date>/`.
   (Fan-out par dimension autorisé ; chaque sous-agent garde l'**interdiction de lire l'historique** et reste en lecture seule.)
4. Présente : la **scorecard par dimension**, le **Top-10 priorités**, la **roadmap**, et signale que la
   cartographie produite servira de base à la mise à jour de `docs/architecture/ARCHITECTURE.md` (étape suivante, séparée).

Note : audit en **lecture seule**. La MAJ de `ARCHITECTURE.md`/`AGENTS.md` se fait APRÈS, à partir de la
cartographie produite (via `/refresh-context` ou une passe dédiée).
