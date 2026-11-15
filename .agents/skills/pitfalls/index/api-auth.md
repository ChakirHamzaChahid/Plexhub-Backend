# Index des pièges — api-auth

> Généré par `python .claude/tools/pitfalls-index.py` depuis `.claude/skills/pitfalls/references/api-auth.md` : un titre par piège (son segment en gras). Ne pas éditer ici — ajouter le piège dans `references/`, puis relancer le script.

## api-auth — API, authentification, sécurité, route_audit et metadata v1 (4)

Texte complet : `.claude/skills/pitfalls/references/api-auth.md`

- [#10] : **CORS** : `CORS_ORIGINS` **défaut `*`** (avec WARNING au boot) — **expliciter en prod**
- [#19] : **Garde-fous ajoutés par le lot `/refacto` audit v1 — ne pas les défaire par mégarde** :
- [#24] : **GET public sur le même chemin qu'un POST gardé (Lot 10-B, `GET`/`POST /api/tv-auth/approve`) — l'audit de routes DOIT raisonner par (méthode, chemin).** (a) `PUBLIC_API_ALLOWLIST` est un ensemble de `(METHOD, path)` et `route_audit._is_allow_listed` n'ignore une route que si **toutes** ses méthodes y …
- [#27] : **API d'édition des métadonnées `/api/metadata/v1` (Lot B2, §5.16) — sept règles à ne pas défaire.**
