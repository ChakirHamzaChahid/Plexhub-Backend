---
description: Snapshot lecture seule de l'état du projet — objectif sprint, board (comptes par Status/sévérité/classe), daily, bugs S1/S2, réf audit, prochaine action.
allowed-tools: Read, Glob, Grep, Bash
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Branche `main`. Docs réels : `docs/30-sprint-plan.md`, `docs/31-board.md`, `docs/80-audit.md`, `docs/daily/<today>.md`, `docs/51-bugs.md` (si présent). Certaines docs peuvent ne PAS exister — ne pas bloquer dessus.

# /app-status — Où en est-on ?

## Étapes
1. **Objectif de sprint** : 1ʳᵉ section de `docs/30-sprint-plan.md` (« Objectif de sprint »). Si absent, le dire.
2. **Résumé du board** (`docs/31-board.md`) — calcule et imprime :
   - Compte de tickets par `Status` (todo / in_progress / review / qa / done / blocked).
   - Répartition **par sévérité** (S1/S2/S3) et **par classe** (Safe / Risky·needs-approval).
   - Liste explicite des lignes `blocked` et de celles avec `cycles=N` (N≥1).
   Astuce calcul (lecture seule) :
   ```bash
   grep -c "| todo |" docs/31-board.md
   grep -oE "cycles=[0-9]+" docs/31-board.md
   grep -nE "\| *blocked *\|" docs/31-board.md
   ```
3. **Rapport du jour** : si `docs/daily/<today>.md` existe, imprime-le.
4. **Bugs** : si `docs/51-bugs.md` existe, montre les `S1`/`S2` ouverts uniquement.
5. **Référence audit** : rappelle la note globale et le P0 depuis `docs/80-audit.md` (dernier audit clean-room).
6. **Prochaine action suggérée** : `/fix-cleanroom` (tout le board) · `/feature <objectif>` (nouvelle fonctionnalité) · `/app-plan <objectif>` (replanifier) · `/audit-cleanroom` (re-juger).

Sois **bref** — c'est un état, pas une narration. N'édite rien (lecture seule).
