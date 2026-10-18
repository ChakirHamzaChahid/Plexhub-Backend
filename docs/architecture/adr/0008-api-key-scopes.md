# ADR 0008 — Scopes des clés d'API (`scrape`, `scrape.admin`)

- Statut : **proposé** (2026-10-18, lot B1, spec seulement)
- Date : 2026-10-18
- Plan source : `docs/10-prd-scrape-gateway.md` (FR-001 → FR-006)
- Portée : `app/api/deps.py`, `app/api/route_audit.py`, `app/services/api_key_service.py`, `app/api/api_keys.py`,
  `app/api/admin.py` (écran des clés), `app/models/database.py` (`ApiKey`), migration 030
- ADR recoupés : 0004 (remédiation audit v1 : `route_audit`, auth fail-closed)

## D1 — Faits du code

1. Une clé utilisateur `phk_…` active ouvre tout `/api/*` gardé par `verify_backend_secret` (`main.py`, Pattern A)
   ou `verify_api_key` (`/api/ai`) ; seules les 4 familles `/api/admin/*` la refusent (`verify_master_key`).
2. `_authenticate` (`deps.py:54-61`) réduit la clé résolue à un booléen : aucune route ne sait **quelle** clé
   l'appelle. `request.state` n'est utilisé nulle part.
3. `route_audit` reconnaît un garde par le **nom** de la fonction (`__name__`, `route_audit.py:57-64,102-112`).
4. Des tests appellent les gardes directement avec `request=None` et remplacent `api_key_service.resolve`
   (`tests/test_ai_deps.py`) : les signatures doivent rester compatibles.

## D2 — Décisions

### D2.1 Stockage
Colonne `api_keys.scopes TEXT NOT NULL DEFAULT ''`, valeurs séparées par une espace, triées, sans doublon
(migration **030**, `ADD COLUMN` gardé par `_column_exists`, `server_default=text("''")` dans l'ORM — leçon de la
migration 025 : un `NOT NULL` sans `DEFAULT` SQL casse les `INSERT` bruts). Chaîne vide = clé sans scope.
L'ensemble des scopes connus est fermé : `{"scrape", "scrape.admin"}` (constante unique dans
`api_key_service`). La validation (ensemble fermé, tri, dédoublonnage, rejet de toute valeur inconnue ou
contenant une espace) se fait **dans `create_key`**, pour couvrir à la fois le corps JSON et le formulaire HTML ;
le `Literal` de l'API n'est qu'une première barrière. Une valeur inconnue lue en base est ignorée (et journalisée une fois) — elle ne donne aucun
droit et ne rend pas la clé « sans scope ».

### D2.2 Sémantique
- **Clé maître** : tous les droits, partout (inchangé).
- **Clé sans scope** : comportement actuel inchangé sur toutes les routes existantes, y compris le texte de ses
  erreurs ; **refusée** sur `/api/scrape/v1` (403). Décision de Chakir du 2026-10-18 (moindre privilège).
- **Clé à scopes** (au moins un scope) : confinée aux routes de ses scopes. Sur toute route gardée par
  `verify_backend_secret` ou `verify_api_key` ⇒ 403 `{"detail": {"code": "insufficient_scope", "message": …}}`.
  Sur `/api/admin/*` : inchangé (401 `Master key required`), `scrape.admin` n'y donne rien.
- Les scopes sont indépendants : `scrape.admin` n'inclut pas `scrape`.

### D2.3 Résolution unique
`_authenticate` est remplacé par `_resolve_principal(x_api_key, request) -> Principal | None`, où `Principal` est un
petit objet figé (`is_master: bool`, `key_id: str | None`, `scopes: frozenset[str]`). `_authenticate` reste comme
enveloppe booléenne pour les appelants existants. Le `Principal` résolu est posé sur `request.state.principal`
quand `request` n'est pas `None` (sert au limiteur par clé de la passerelle, ADR 0009 § D2.8). Aucune deuxième
lecture de la table par requête.

### D2.4 Gardes
- `verify_backend_secret` et `verify_api_key` : même code, plus un refus 403 si le principal porte des scopes.
  Leur ordre de contrôles est conservé (`verify_api_key` teste toujours sqlite-vec en premier).
- Deux **nouvelles fonctions nommées** : `verify_scrape_scope` (maître ou `scrape`) et `verify_scrape_admin_scope`
  (maître ou `scrape.admin`). Erreurs au format de la passerelle (`invalid_api_key` 401, `insufficient_scope` 403).
- Pas de fabrique `verify_scope("…")` : sa closure ne serait pas reconnue par `route_audit`.
- `_AUTH_DEPENDENCY_NAMES` gagne ces deux noms ; `PUBLIC_API_ALLOWLIST` ne change pas (5 entrées) ; B1-03
  **crée** un test qui fige l'ensemble des gardes reconnus (il n'en existe pas aujourd'hui) et met à jour le texte
  « 4 gardes » de `tests/test_route_auth_assertion.py`.
- Le router de la passerelle n'a **pas** de dépendance au niveau du router : chaque route déclare son garde. Les
  routes `/api/scrape/v1/cache/invalidate|purge` portent **seulement** `verify_scrape_admin_scope` (pas les deux
  gardes empilés : une clé `scrape.admin` seule doit passer). `route_audit` ne distingue pas ces deux gardes : un
  test structurel vérifie le garde attendu route par route.

### D2.5 Gestion
- `POST /api/admin/keys` : champ `scopes: list[Literal["scrape", "scrape.admin"]] = []` (doublon ⇒ dédoublonné,
  valeur inconnue ⇒ 422). `ApiKeyOut` gagne `scopes: list[str]`. Toujours `verify_master_key`.
- Pas de route de modification des scopes d'une clé existante : on révoque et on recrée (une clé est un secret
  remis une seule fois ; changer ses droits en place brouillerait l'audit).
- Admin HTML `/admin/keys` : deux cases à cocher à la création, colonne « Scopes » dans le tableau
  (`keys.html`, `_keys_table.html`, `_key_created.html`, `_keys_ctx`). POST soumis au CSRF `Sec-Fetch-Site`
  existant.

## D3 — Alternatives écartées

- **Table liée `api_key_scopes`** : une jointure à chaque requête authentifiée pour deux valeurs possibles.
- **Scope implicite `api` donné aux clés existantes** (backfill) : plus régulier, mais une migration de données sur
  la table des secrets et un risque de verrouiller une clé existante si le backfill échoue ; la règle « vide =
  comportement actuel » donne le même résultat sans toucher aux lignes.
- **Scopes hiérarchiques** (`scrape.admin` ⊃ `scrape`) : le contrat les veut indépendants.
- **401 pour scope manquant** : la clé est valide ; 403 dit au client que réessayer avec la même clé est inutile.

## D4 — Tests exigés

- Parcours de `app.routes` : une clé `scrape` reçoit 403 sur **chaque** route `/api/*` gardée par
  `verify_backend_secret`/`verify_api_key` (y compris `POST /api/tv-auth/approve`), une clé sans scope 200/2xx
  comme avant sur un échantillon.
- Matrice de la passerelle : maître / sans scope / `scrape` / `scrape.admin` / les deux / révoquée / expirée × route
  de lecture / route d'admin ; clé dont la colonne ne contient que des scopes inconnus ⇒ confinée et sans droit ;
  clé à scopes sur `/api/admin/*` ⇒ 401 `Master key required` (inchangé).
- Création par le formulaire HTML : valeur inconnue ou chaîne « scrape scrape.admin » dans un seul champ rejetée.
- `route_audit` : les deux noms reconnus ; boot qui échoue si une route de la passerelle perd son garde.
- Migration 030 : base fraîche et base ancienne, rejouée deux fois, `INSERT` brut sans la colonne, aucun WARNING.
- `tests/test_ai_deps.py` passe sans modification de ses appels (`request=None`).

## D5 — Conséquences

- Premier usage de `request.state` dans le dépôt : documenté dans `CLAUDE.md` §2 (`deps.py`) et en piège §9.
- Le `POST /api/sync/enrichment` (aujourd'hui ouvert à toute clé utilisateur, constat de l'état des lieux) devient
  inaccessible aux clés à scopes, ce qui est voulu ; il reste ouvert aux clés sans scope (hors périmètre B1).
- Revue `security-reviewer` obligatoire sur les tickets B1-02, B1-03 et B1-04 (déclencheurs `deps.py`,
  `route_audit.py`, `X-API-Key`, POST `/admin*`).
