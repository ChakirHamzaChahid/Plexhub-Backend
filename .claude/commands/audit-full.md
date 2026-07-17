---
description: Audit complet 360° (lecture seule) — diagnostic exhaustif et indépendant + re-vérification critique des audits antérieurs. Sortie docs/audit/v*/. Délègue à full-auditor.
---

> 🧭 Lis `.claude/WORKFLOWS.md` (routeur + garde-fous + **politique de routage modèle×effort**). Applique à chaque agent lancé le couple **(modèle, effort)** adéquat — doctrine = skill `model-effort-routing`.

Objectif : produire un **audit complet 360°** (diagnostic, AUCUNE modif du code applicatif), indépendant et re-vérifié, du backend sur `develop` à HEAD.

Étapes (Manager) :
1. Lis `CLAUDE.md` (§9 pièges, §10 état réel/dette) + `docs/architecture/ARCHITECTURE.md` (si présent). Survole, pour le DELTA, **les lignées d'audit existantes** : le dernier audit clean-room (`docs/audit/cleanroom-*/`, findings `CR-*`) **ET** la série versionnée `docs/audit/v*/` si elle existe (findings `AUDIT-*`), plus le board `docs/31-board.md` (statut déclaré des findings) — **sans leur faire confiance**.
2. Vérifie le runtime : `pytest -v` (état de la suite), boot `uvicorn app.main:app` + `GET /api/health` 200 + `/metrics` joignable (env `.env` minimal requis). Si le serveur ne boote pas, c'est un finding, pas un bloqueur d'audit.
3. Délègue à l'agent **`full-auditor`** le mandat 360° complet :
   - re-vérifie de façon INDÉPENDANTE les findings antérieurs de **toutes les lignées** — `CR-*` (cleanroom) ET `AUDIT-*` (série versionnée si présente) — en croisant avec le board : un finding déclaré « résolu » l'est-il réellement dans le code à HEAD ? (les audits passés peuvent s'être trompés / avoir manqué des choses),
   - audite l'intégralité du code à HEAD par phases (cartographie → stabilité/sécurité → perf → architecture → API/contrats/features → release/observabilité),
   - écrit le rapport sous `docs/audit/v<N>/` (N = numéro suivant de la série versionnée ; `v1` si aucune série — le clean-room garde sa lignée propre `cleanroom-*`).
4. Présente : la **scorecard**, le **DELTA** couvrant toutes les lignées (`CR-*` + `AUDIT-*` : résolus / encore ouverts / régressés / corrections d'erreurs / faux négatifs), le **Top-10 priorités** et la **roadmap**.

Note : c'est un audit en **lecture seule**. Pour corriger ensuite : `/incident` (bug avéré), `/benchmark`→`/fix-bench-perf` (perf), `/fix-cleanroom` (findings `CR-*`), `/app-build` (backlog `AUDIT-*`).
