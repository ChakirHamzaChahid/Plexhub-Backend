# ADR 0010 — Scopes `media.read` / `media.write` et `route_audit` par préfixe

- Statut : **accepté** (2026-10-18, lot B2)
- Plan source : `docs/10-prd-metadata-api.md` (FR-001, FR-002, FR-012)
- Portée : `app/api/deps.py`, `app/api/route_audit.py`, `app/services/api_key_service.py`, `app/api/api_keys.py`,
  `app/templates/admin/keys.html`, `app/api/body_limit.py`, `app/utils/rate_limit.py`, `app/main.py`
- ADR recoupés : 0008 (scopes B1 — cette ADR le **prolonge sans le modifier**), 0004 (audit de routes)

## D1 — Faits du code

1. `Principal`/`_resolve_principal`/`_require_scope` (`deps.py`) gèrent déjà plusieurs scopes ; une clé à scopes est
   « confinée » (403 `insufficient_scope` sur `verify_backend_secret`/`verify_api_key`, 401 sur `/api/admin/*`).
2. `scrape media.read` n'est bloquée que par la liste fermée `KNOWN_SCOPES` et le `Literal` de `ApiKeyCreate`.
3. `route_audit` ne reconnaît les gardes de scope que sous le préfixe unique `/api/scrape/v1/`.
4. `body_limit` a un préfixe et une limite uniques ; `main.py` n'enregistre qu'**une** instance (le test d'ordre des
   middlewares fige la liste exacte). `enforce_scrape_rate_limit` est propre à la passerelle.

## D2 — Décisions

### D2.1 Scopes
`KNOWN_SCOPES = {"scrape", "scrape.admin", "media.read", "media.write"}`. `Literal` de `ApiKeyCreate` aligné (un test
l'impose). Indépendants, non hiérarchiques : `media.write` n'inclut pas `media.read`. Validation unique dans
`create_key` (inchangée). Le formulaire `/admin/keys` suit par sa boucle Jinja ; le texte explicatif est complété.
Pas de modification de scopes d'une clé existante (révoquer et recréer).

### D2.2 Gardes
Deux fonctions **nommées** `verify_media_read_scope` / `verify_media_write_scope` (via `_require_scope`), pas de
fabrique. Maître : tout. Clé sans scope : refusée (403) sur `/api/metadata/v1`. `scrape media.read` : passerelle B1 +
routes de lecture B2 ; refusée sur les routes qui valident (403 `insufficient_scope`), sur `/api/media/*` (403) et sur
`/api/admin/*` (401 `Master key required`).

### D2.3 `route_audit`
`_SCOPE_GUARD_PREFIXES = {verify_scrape_scope: "/api/scrape/v1/", verify_scrape_admin_scope: "/api/scrape/v1/",
verify_media_read_scope: "/api/metadata/v1/", verify_media_write_scope: "/api/metadata/v1/"}` ; `_is_guarded` exige
que le chemin commence par le préfixe **du garde porté** (un garde `scrape` sous `/api/metadata/v1/` ou l'inverse ne
compte pas ⇒ boot refusé). `_SCOPE_GUARDS` et `_AUTH_DEPENDENCY_NAMES` en sont dérivés. `PUBLIC_API_ALLOWLIST`
inchangée. Test structurel **route par route** : une route d'écriture ne porte jamais le garde de lecture
(`media.write` seul, pas les deux empilés : la clé d'écriture porte les deux scopes mais le garde exact est testé).

### D2.4 Limites par préfixe
`body_limit` devient une table `{préfixe: octets}` (`/api/scrape/v1/` 65 536 ; `/api/metadata/v1/` 262 144) dans une
**seule** instance de middleware (comportement identique : 413 sans lire un `Content-Length` trop grand, corps
`chunked` compté). Deux `SlidingWindowLimiter` : général (`METADATA_RATE_LIMIT_PER_MINUTE`, 300) et écriture
(`METADATA_WRITE_RATE_LIMIT_PER_MINUTE`, 90), compte par clé (`key_id` ou `master`), dépendance déclarée **après** le
garde, 429 `rate_limited` + `Retry-After` avant tout traitement. Remise à zéro dans `tests/conftest.py`.

### D2.5 Précision (revue de sécurité, 2026-10-18)
L'indépendance `media.write` / `media.read` vaut pour les **routes** : une clé `media.write` seule n'atteint aucune lecture ni
simulation. Mais le résultat d'une écriture (`before`, `after`, `variants`) et le `currentRevision` d'un 412 rendent le contenu
de l'élément visé — des métadonnées non secrètes. Choix : le documenter (écart X31) plutôt que d'appauvrir ces réponses, dont le
client a besoin ; la clé d'écriture de plexhubTMM porte de toute façon les deux scopes.

## D3 — Alternatives écartées
- Garde unique `verify_scope(...)` paramétrique : closure non reconnue par identité (ADR 0008 D2.4).
- Second `add_middleware(body_limit)` : casse l'ordre figé et double l'enveloppe ASGI.
- Table de préfixes par route dans `route_audit` : le préfixe porté par le **garde** suffit et est plus strict.

## D4 — Tests exigés
Matrice maître / sans scope / `scrape` / `media.read` / `media.write` / `scrape media.read` / `media.read media.write` /
révoquée / expirée × chaque route (lecture, validation) ; `scrape media.read` refusée sur `/api/media/*` ; clé `media.*`
sans `scrape` refusée sur `/api/scrape/v1` ; boot refusé si un garde `media.*` est posé hors préfixe ou un garde `scrape`
sous `/api/metadata/v1/` ; ensemble des gardes reconnus figé (8) ; `Literal` = `KNOWN_SCOPES` ; création par formulaire ;
limites de corps par préfixe ; limiteurs séparés.

## D5 — Conséquences
Revue `security-reviewer` obligatoire (déclencheurs `deps.py`, `route_audit.py`, `X-API-Key`). `CLAUDE.md` §2/§9
(piège 26b) mis à jour : le préfixe n'est plus unique, il suit le garde.
