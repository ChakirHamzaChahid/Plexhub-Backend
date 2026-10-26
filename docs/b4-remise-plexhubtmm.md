# Lot B4 — remise à plexhubTMM

Version **1.15.0**, migration **032**, non déployé (déploiement homelab = lot séparé, accord de Chakir).
Contrat : `plexhubTMM/docs/17-contrat-b4-backend.md`. ADR : `docs/architecture/adr/0013-b4-images-and-scrape-fields.md`.
Contrat machine régénéré : `docs/contracts/metadata-v1.openapi.json` (à copier dans `schemas/plexhub-backend/`, `/schema-sync`).

## 1. Livré
- `ItemSummary.thumbUrl` / `artUrl` (aussi `ItemView.context`) : `poster_url` > `resolved_thumb_url` > `null` ; `fanart_url` >
  `resolved_art_url` > `null` ; filtre `https`, sans identifiants, sans paramètre d'authentification, sinon `null`.
- `EditableFields` (movie, show) : `posterUrl`, `fanartUrl`, `posterGallery`, `fanartGallery`, `directors`, `writers`,
  `spokenLanguages`, `availableTrailers` + `ratings.metacritic`, `ratings.rottenTomatoes`. `GET /fields` les liste (pas pour `episode`).
- Migration 032 (12 colonnes nullables). `match_locked` les protège comme les champs B2 ; aucun écrivain automatique ne les connaît.

## 2. Réponses aux questions ouvertes
| Q | Décision |
|---|---|
| Q1 | La saisie gagne toujours. Aucun enrichissement ne touche `poster_url`/`fanart_url` ; pour revenir à TMDB : envoyer `null`. |
| Q2 | Non : pas d'`alternativeThumbUrls`. |
| Q3 | JSON dans colonnes texte. |
| Q4 | Non pour les épisodes (jeu épisode inchangé) — X38. |
| Q5 | 0..10 confirmé, même forme `Rating` — X39. |
| Q6 | Pas de plafond ajouté : page ≤ 500, deux URL courtes par ligne, GZip actif ; aucune requête de plus. |

## 3. Écarts (numérotés à la suite de X37 ; X34–X37 sont alloués côté plexhubTMM, à confirmer)
| # | Sujet | Contrat | Retenu par B4 | Effet côté client |
|---|---|---|---|---|
| X38 | `posterUrl` d'un épisode | Q4 | Refusé (400 `invalid_field`, absent de `GET /fields`) ; `thumbUrl` de l'épisode reste calculé par repli sur `resolved_thumb_url` | Garder la vignette d'épisode en local |
| X39 | Échelle Metacritic / Rotten Tomatoes | Q5 | 0..10 ; la passerelle `/api/scrape/v1` les rend en 0..100 : le client divise par 10 | Convertir avant écriture |
| X40 | Listes `directors`, `writers`, `spokenLanguages` | CSV, « sans virgule » | Un nom contenant une virgule est refusé (400) | Nettoyer en amont |
| X41 | Révision | — | `REVISION_FORM` 2 : toutes les révisions changent une fois | Invalider les caches de révision |
| X42 | Filtrage de `thumbUrl` | § 3 | Appliqué à la valeur **choisie** : une `poster_url` non sûre donne `null`, sans repli sur `resolved_thumb_url` | `null` = inconnu |
| X43 | Élément verrouillé | § 6 test 3 « refus » | Comme B2 : l'API écrit sur un élément verrouillé (et le laisse verrouillé) ; le verrou protège contre les écrivains **automatiques** | Aucun refus à attendre |
| X44 | Lecture tolérante | — | JSON de galerie/bande-annonce illisible ou entrée invalide en base ⇒ ignoré (`[]`), jamais d'erreur | — |
| X45 | `availableTrailers` et `youtubeTrailer` | — | Indépendants ; `youtubeTrailer` inchangé | Ne pas les synchroniser entre eux |
| X46 | NFO / générateur Plex | — | N'utilisent pas ces colonnes (leurs attributs `poster_url` alimentent `resolved_thumb_url`) | — |
| X47 | `SCHEMA_VERSION` | § 5 | Inchangé (additif) | — |
| X48 | `ItemView.fields` | § 3 | Valeur stockée, sans repli ; `thumbUrl`/`artUrl` calculés uniquement dans `context` | — |

## 4. Non tenu / à noter
- Test indicatif § 6.6 « performance de `GET /items` sur 126 000 lignes » : non mesuré sur la vraie base ; le calcul est en
  Python sur la page déjà chargée (≤ 500 lignes), requêtes inchangées. Un test borne la taille par ligne.
- Le numéro X38 est provisoire tant que plexhubTMM n'a pas confirmé la numérotation.
