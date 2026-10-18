# Ops — passerelle de scraping `/api/scrape/v1` (Lot B1)

> Lot B1 (2026-10-18). Public : l'opérateur du backend. Contexte : `docs/10-prd-scrape-gateway.md` (PRD), ADR
> `docs/architecture/adr/0008-api-key-scopes.md` (scopes) et `0009-scrape-gateway-cache.md` (cache), flux
> `docs/architecture/flux-cles.md` §5.15, contrat machine `docs/contracts/scrape-v1.openapi.json`, note de remise à
> plexhubTMM `docs/b1-remise-plexhubtmm.md`.
> **Ce document ne contient aucune valeur réelle** : `<CLE_MAITRE>`, `<CLE_SCRAPE>`, `<CLE_SCRAPE_ADMIN>`,
> `<HOTE_BACKEND>` et `<ID_CLE>` sont des emplacements à remplacer par vos valeurs, qui ne doivent jamais être
> recopiées dans un dépôt, un ticket ou un journal.

## 0. Ce que c'est

Neuf routes JSON (`/api/scrape/v1/*`) qui servent à l'outil externe plexhubTMM les fiches TMDB/OMDb (rapprochement
titre → fiche, film, série, saison avec épisodes, recherche par id IMDb/TVDB, candidats), **sans que plexhubTMM détienne
la moindre clé TMDB ou OMDb** : seul le backend les a. Les réponses sont gardées dans la table `scrape_cache`
(versionnée, par langue). Rien ne touche à la table `media` ni aux caches de l'enrichissement planifié.

Pré-requis côté backend : `TMDB_API_KEY` renseignée (sinon toute route de données répond 503
`provider_not_configured`) ; `OMDB_API_KEY` facultative (sans elle, les notes IMDb/Rotten Tomatoes/Metacritic sont
omises et `omdbStatus` vaut `not_configured`, la réponse reste 200).

## 1. Créer la clé de plexhubTMM

Une clé « scraping seulement » : elle n'ouvre **que** `/api/scrape/v1`. Sur tout le reste de l'API, une clé à scopes
reçoit 403 `insufficient_scope` ; sur `/api/admin/*`, 401 (réservé à la clé maître). Une fuite de cette clé ne donne
donc accès ni aux médias, ni aux comptes Xtream, ni au flux, ni à l'appairage TV.

### 1.1 Scopes recommandés

| Scope | Ouvre | À donner à |
|---|---|---|
| `scrape` | les 7 routes de lecture : `match`, `search`, `movie`, `tv`, `tv/.../season/...`, `find`, `cache/stats` | la clé courante de plexhubTMM |
| `scrape.admin` | `POST cache/invalidate` et `POST cache/purge` **uniquement** | une clé d'exploitation, distincte |

Les deux scopes sont **indépendants** : `scrape.admin` n'inclut pas `scrape` (une clé `scrape.admin` seule ne peut pas
lire `cache/stats`). Une clé **sans aucun scope** (toutes les clés créées avant ce lot) est **refusée** sur la
passerelle (403) : il faut créer une clé neuve, on ne modifie pas les scopes d'une clé existante.

Recommandation : **deux clés**. `plexhubTMM` avec `scrape` (usage courant, limitée à la lecture) et
`plexhubTMM-admin` avec `scrape.admin` (purge et invalidation, utilisée par vous ou par une tâche d'exploitation).
Si plexhubTMM doit invalider elle-même, créer une seule clé avec les deux scopes. La clé maître fait tout, mais ne la
confiez pas à un outil externe.

### 1.2 Expiration

Donner une expiration : `expiresInDays` (1 à 3650) ; 365 est un bon défaut. Une clé expirée répond 401
`invalid_api_key`, comme une clé révoquée. Noter la date dans votre agenda : renouveler = créer une nouvelle clé,
la remettre à plexhubTMM, puis révoquer l'ancienne.

### 1.3 Créer — par l'écran d'administration HTML

1. Ouvrir `http://<HOTE_BACKEND>/admin/keys` (authentification Basic `ADMIN_USERNAME` / `ADMIN_PASSWORD`).
2. Saisir le libellé (`plexhubTMM`), l'expiration, cocher `scrape` (et/ou `scrape.admin`), valider.
3. La clé en clair (`phk_…`) n'apparaît **qu'une fois**, dans l'écran qui suit la création : la copier tout de suite
   dans le gestionnaire de secrets de plexhubTMM. Le tableau n'affiche ensuite que le préfixe et la colonne « Scopes ».

### 1.4 Créer — par l'API avec la clé maître

```sh
curl -sS -X POST "http://<HOTE_BACKEND>/api/admin/keys" \
  -H "X-API-Key: <CLE_MAITRE>" -H "Content-Type: application/json" \
  -d '{"label": "plexhubTMM", "expiresInDays": 365, "scopes": ["scrape"]}'
```

Réponse 201 : `id`, `label`, `keyPrefix`, `status`, `scopes`, `expiresAt` et `key` (la clé en clair, **une seule
fois**). Une valeur de scope inconnue donne 422. `GET /api/admin/keys` (même en-tête) liste les clés, scopes compris,
sans jamais rendre une clé en clair.

### 1.5 Révoquer

```sh
curl -sS -X POST "http://<HOTE_BACKEND>/api/admin/keys/<ID_CLE>/revoke" -H "X-API-Key: <CLE_MAITRE>"
```

(ou le bouton de révocation de `/admin/keys`). Effet immédiat : la clé répond 401 `invalid_api_key`. En cas de doute sur
une fuite, révoquer d'abord, créer ensuite.

## 2. Réglages `SCRAPE_*`

Tous facultatifs (valeurs par défaut sûres), documentés dans `.env.example`. Les valeurs hors bornes sont ramenées à la
borne la plus proche, jamais refusées.

| Variable | Défaut | Bornes | Effet |
|---|---|---|---|
| `SCRAPE_TTL_SUCCESS_DAYS` | 30 | 1..365 | durée de vie d'une entrée trouvée (`matched`/`found`) |
| `SCRAPE_TTL_NEGATIVE_DAYS` | 3 | 1..30 | durée de vie d'une entrée négative (`ambiguous`/`nomatch`/`not_found`) |
| `SCRAPE_RATE_LIMIT_PER_MINUTE` | 300 | `<= 0` désactive | requêtes par minute et par clé ; au-delà 429 `rate_limited` + `Retry-After` |
| `SCRAPE_REQUEST_TIMEOUT_SECONDS` | 25 | 5..28 | échéance d'une requête ; au-delà 504 `upstream_timeout` |
| `SCRAPE_ALLOWED_LANGUAGES` | `fr-FR,en-US` | balises `xx-XX` | langues acceptées (`TMDB_LANGUAGE` toujours acceptée en plus) ; hors liste 400 |

Ces TTL ne valent que pour la table `scrape_cache` : le cache de l'enrichissement planifié garde les siens. Ajouter une
langue = ajouter une balise à `SCRAPE_ALLOWED_LANGUAGES` ; chaque langue distincte a ses propres lignes de cache.

⚠️ **Sur le homelab, le `.env` n'est pas injecté dans le conteneur** : le `docker-compose.yml` du service n'a pas
de directive `env_file`, seules les variables listées sous `environment:` atteignent l'application. Pour changer un
réglage, **l'ajouter sous `environment:` du service** (par exemple `SCRAPE_RATE_LIMIT_PER_MINUTE: "300"`), puis
recréer le conteneur (`docker compose up -d`). Vérifier ensuite que la valeur est effectivement vue par le conteneur
(`docker exec <CONTENEUR> env`, filtre sur `SCRAPE_`). Éditer le `.env` seul ne change **rien** là-bas. (Le
`docker-compose.yml` du dépôt, lui, a un `env_file` : ce piège est propre au compose du homelab.)

La passerelle est **process-local** (coalescence des appels, limiteur par clé, compteurs `sinceStart` en mémoire d'un
seul process) : ne jamais lancer `uvicorn --workers N` avec N > 1.

## 3. Entretien du cache : purge périodique et statistiques

### 3.1 Pourquoi purger à la main

Il n'y a **aucune purge automatique** (décision du lot). Une entrée expirée n'est jamais servie, mais elle reste en
base tant qu'on ne la supprime pas ; la table grossit donc avec le nombre de titres, d'années et de langues demandés.
`POST /api/scrape/v1/cache/purge` supprime au plus `limit` entrées **expirées ou d'une version de clé/schéma non
courante** (jamais une entrée encore valide) et rend `{"deleted": n, "remaining": m}` — `remaining` est ce qu'il
reste de purgeable.

### 3.2 Cadence conseillée

Une fois par semaine suffit en usage normal (TTL de 30 jours). Clé requise : `scrape.admin` ou la clé maître.

```sh
curl -sS -X POST "http://<HOTE_BACKEND>/api/scrape/v1/cache/purge" \
  -H "X-API-Key: <CLE_SCRAPE_ADMIN>" -H "Content-Type: application/json" \
  -d '{"limit": 10000}'
```

`limit` : 1 à 100 000 (défaut 10 000 si le corps est absent). Si `remaining` est supérieur à 0, relancer jusqu'à ce
qu'il atteigne 0. À planifier par une tâche d'exploitation (cron du homelab) ou à la demande de plexhubTMM ; si
l'appel est automatisé, la clé de l'automate ne doit porter **que** `scrape.admin`.

La purge ne borne pas une croissance due à la **variété** des demandes (beaucoup de titres ou d'années distincts) :
surveiller la taille (§ 3.3) et, si elle dérive, réduire `SCRAPE_TTL_SUCCESS_DAYS`.

### 3.3 Statistiques

```sh
curl -sS "http://<HOTE_BACKEND>/api/scrape/v1/cache/stats" -H "X-API-Key: <CLE_SCRAPE>"
```

Une ligne par (`family`, `keyVersion`) : `entries` (= `positive` + `ambiguous` + `negative`), `expired` (sous-ensemble
de `entries`, ce que la prochaine purge retirera), `oldestFetchedAt` / `newestFetchedAt` (millisecondes depuis
l'epoch), plus `ttl` (jours) et `sinceStart` (`hits`, `misses`, `coalesced`, `upstreamErrors` **depuis le démarrage du
process**, remis à zéro à chaque redémarrage). Un `expired` qui grossit sans purge, ou un `upstreamErrors` qui monte,
sont les deux signaux à regarder. La métrique Prometheus `plexhub_scrape_gateway_requests_total{route,outcome}`
(sur `/metrics`, Basic Auth `METRICS_*`) compte les requêtes par route et par issue (`hit`, `miss`, `coalesced`,
`error`).

## 4. Invalidation

Quand une fiche TMDB a été corrigée en amont ou qu'un résultat est faux, supprimer l'entrée plutôt que d'attendre
l'expiration (clé `scrape.admin` ou maître) :

```sh
# Toutes les langues d'un film (ou "show", ou "season" = toutes les saisons de la série)
curl -sS -X POST "http://<HOTE_BACKEND>/api/scrape/v1/cache/invalidate" \
  -H "X-API-Key: <CLE_SCRAPE_ADMIN>" -H "Content-Type: application/json" \
  -d '{"family": "movie", "tmdbId": 603}'

# Ou des clés canoniques précises (1 à 500, forme "{keyVersion}|{langue}|{clé}")
curl -sS -X POST "http://<HOTE_BACKEND>/api/scrape/v1/cache/invalidate" \
  -H "X-API-Key: <CLE_SCRAPE_ADMIN>" -H "Content-Type: application/json" \
  -d '{"keys": ["1|fr-FR|movie|603"]}'
```

L'opération est **tout ou rien** : une seule clé mal formée donne 400 et **rien** n'est supprimé. Elle est idempotente
(`{"deleted": 0}` si l'entrée n'existe pas). Invalider `movie|{id}` corrige aussi la réponse suivante de `/match`, qui
relit ses détails. Pour forcer le rafraîchissement d'une seule fiche sans rien supprimer, le client peut appeler la
route avec `refresh=true` (action explicite, à ne pas automatiser).

## 5. Vérifications

1. **Santé** : `GET http://<HOTE_BACKEND>/api/health` → 200 (route publique), avec la version attendue ; le conteneur
   est `healthy`.
2. **Migrations** : au premier démarrage après mise à jour, les migrations 029 (table `scrape_cache`) et 030
   (colonne `api_keys.scopes`) s'appliquent seules ; elles sont additives et rejouables. Aucun WARNING de colonne dupliquée.
3. **Garde** : `GET /api/scrape/v1/cache/stats` **sans** clé → 401 ; avec une clé `scrape` ou la clé maître → 200 ;
   avec une clé sans scope → 403 `insufficient_scope`.
4. **Confinement** : avec la clé `scrape`, `GET /api/media/movies` → 403 `insufficient_scope`.
5. **Première lecture** : un `GET /api/scrape/v1/match?type=movie&title=matrix&year=1999` répond `cache: "miss"`, le
   même appel ensuite `cache: "hit"`. Le contrat machine (formes exactes) est dans
   `docs/contracts/scrape-v1.openapi.json`.
6. **Statistiques** : `cache/stats` montre des `entries` et des `hits` qui augmentent.

## 6. Dépannage

| Symptôme | Cause probable | Action |
|---|---|---|
| 401 `invalid_api_key` | clé absente, inconnue, révoquée, expirée — ou base momentanément indisponible (le backend ne distingue pas) | vérifier l'en-tête `X-API-Key`, l'état dans `/admin/keys`, créer une nouvelle clé |
| 403 `insufficient_scope` | clé sans le scope de la route (ou clé sans scope du tout) | créer une clé avec le bon scope ; on ne modifie pas une clé existante |
| 400 `invalid_parameter` sur `language` | langue hors `SCRAPE_ALLOWED_LANGUAGES` | l'ajouter à la liste (et à `environment:` sur le homelab) si c'est voulu |
| 413 `invalid_parameter` | corps de plus de 64 Kio | le client doit envoyer moins de clés par invalidation |
| 429 `rate_limited` | plus de `SCRAPE_RATE_LIMIT_PER_MINUTE` requêtes par minute pour cette clé, ou 429 TMDB | respecter `Retry-After` ; relever la limite si besoin |
| 503 `provider_not_configured` | `TMDB_API_KEY` absente, ou rejetée par TMDB | vérifier la clé TMDB du conteneur (`environment:` sur le homelab) |
| 503 `upstream_unavailable` | TMDB injoignable | réessayer (`Retry-After: 30`) |
| 504 `upstream_timeout` | TMDB trop lent pour l'échéance | réessayer ; le résultat peut déjà être en cache à l'essai suivant |
| `omdbStatus: "budget_exhausted"` | budget OMDb du process épuisé (partagé avec l'enrichissement planifié) | normal, la réponse reste 200 ; les notes reviendront à la remise à zéro du budget |

Ne jamais copier une clé (`phk_…`, TMDB, OMDb) ni un en-tête `X-API-Key` dans un ticket ou un journal.
