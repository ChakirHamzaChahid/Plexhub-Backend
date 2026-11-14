# 🧭 Routeur de workflows PlexHub Backend (injecté au SessionStart — version courte)

Le détail (garde-fous complets, DoD, routage modèle × effort) est dans `.claude/WORKFLOWS.md` : **lis-le quand tu lances un workflow**, pas avant. Tâche triviale (1 fichier, question) → réponds directement. Intention ambiguë → UNE clarification puis route.

| Intention (mots-clés) | Commande |
|---|---|
| lot suivant d'un plan existant, « reprends le plan », « continue » | `/lot` (une session par lot) |
| nouvelle fonctionnalité, « implémente », « ajoute », « nouvel endpoint » | `/feature` (2 sessions ; chemin mineur si éligible) |
| refonte, refacto, extrait, migre, découpe | `/refacto` |
| bug, 500, régression, « ça marche plus » | `/incident` |
| audit diff / du lot / avant release | `/wf-audit-incremental` |
| audit complet, 360°, état du code | `/audit-full` |
| benchmark, perf, latence API, endpoint lent | `/benchmark` puis `/fix-bench-perf` |
| recale CLAUDE.md, doc périmée | `/sync-context` (léger) · `/refresh-context` (complet) |
| release, publie, tag, image Docker | `/release` (seule voie de publication) |
| audit clean-room / remédiation | `/audit-cleanroom` · `/fix-cleanroom` |
| status, board | `python .claude/tools/lot-state.py` (ou `/app-status`) |

## Garde-fous essentiels (le détail fait foi dans WORKFLOWS.md)
- Tout sur `develop`, **un seul rédacteur à la fois**, pas de worktree. Le parallèle est réservé aux lecteurs (revues, exploration).
- DoD : `python .claude/tools/dod-gate.py run` (lot : ruff + pytest + boot `/api/health`) / `run --fast` (ticket : ruff + pytest `--no-cov`) ; un hook `Stop` refuse de clore si du code a changé sans vert.
- Risky (migration SQLite, refacto large, release `develop`→`main`, purge de données) = approbation de Chakir. Secrets `.env` : **jamais**.
- Routage : ligne `ROUTAGE … model=… effort=…` avant chaque sous-agent, vérifiée par `python .claude/tools/routage.py check` ; Opus seulement si la grille de `model-effort-routing` le donne (agent qui écrit : C2 = 2 ou C3 = 2 ; lecteur : sécurité et synthèse d'audit seulement) ; jamais Fable pour un sous-agent ; `model:` toujours explicite.

## Économie de tokens (NORMATIF)
Mesure : 96 % des tokens = relecture du contexte à chaque tour. Le coût = taille du contexte × nombre de tours.
1. **Un ticket = un agent neuf** (sauf ticket minuscule : `model-effort-routing` §3). La chaîne reste séquentielle, mais chaque ticket part dans une nouvelle instance avec une fiche de passage (ticket, spec, `git log`/diff du ticket précédent) — jamais la suite de la conversation précédente.
2. **Sorties bornées.** Tests/lint via `dod-gate.py` (journal en `.claude/.cache/dod-gate/`, extrait des erreurs si rouge). Autres commandes filtrées (`| tail -n 60`, `grep`) ; jamais un log complet. Fichier > 400 lignes : `Grep` puis lecture par plages (offset/limit), jamais en entier.
3. **Rapport final d'un sous-agent ≤ 30 lignes** (résultat, fichiers, commits, réserves). Il entre tel quel dans le contexte de la session principale.
4. **Une session principale par lot.** Lot terminé → nouvelle session (`/clear`), reprise par le plan + `python .claude/tools/lot-state.py`.
5. **Une revue par lot**, sur le diff consolidé (`code-reviewer` medium). Jumeau `-high` seulement en tentative 2 ou sur déclencheur (migrations/SQLite lock, concurrence, auth/SSRF/secrets, `/dav`, `/api/scrape/v1`, `/api/metadata/v1`).
6. **Recherche ciblée = `Grep`/`Read` directs.** `Explore` (model `haiku`) seulement pour un balayage large de plusieurs modules.
7. **Bandeau `CLAUDE.md`** : recalé seulement quand `session-start.js` / `claude-doctor` le demande (modules, migrations, pièges) — pas de commit « recalage » par lot.
8. **Cache** : modèle et MCP choisis au démarrage (en changer réécrit tout le cache) ; `/compact` **avant** une pause, jamais après ; `/rewind` pour revenir en arrière.
