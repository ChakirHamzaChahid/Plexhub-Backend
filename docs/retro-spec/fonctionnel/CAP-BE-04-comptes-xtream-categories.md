# CAP-BE-04 — Comptes Xtream & catégories

**Reconstituée le** : 2026-09-25 · recalée le 2026-10-02 (Lot 10) · **HEAD** : `aa3270f` · **Sources** : inventaire, code, tests, app TV (lecture seule, preuves `tv:`). Aucun PRD ne couvre cette capacité (`docs/10-prd-*.md` ne traitent que les téléchargements) : pas de tableau d'écart fiche ↔ PRD ; les écarts sont face à `CLAUDE.md` (§2, §3, §9).
**Périmètre** : `GET/POST /api/accounts`, `PUT/DELETE /api/accounts/{id}`, `POST /api/accounts/{id}/test` (`app/api/accounts.py`, `app/services/account_service.py`) ; `GET/PUT /api/accounts/{id}/categories`, `POST /api/accounts/{id}/categories/refresh` (`app/api/categories.py`, `app/services/category_service.py`) ; auto-provision au boot (`app/main.py`) ; chiffrement du mot de passe (`app/utils/crypto_fields.py`, migration 016) ; tables `xtream_accounts`, `xtream_categories`. Renvoi : `docs/retro-spec/_genere/inventaire.md`.

## Résumé

L'opérateur (ou l'app TV) enregistre des comptes Xtream : le backend vérifie les identifiants auprès du fournisseur, stocke le compte (mot de passe chiffré au repos si une clé existe), lance une première synchronisation, et sait le modifier, le tester ou le supprimer avec toutes ses données. Pour chaque compte, le spectateur choisit les catégories visibles (tout, liste blanche, liste noire) ; le backend répercute ce choix sur la visibilité des films, séries, épisodes et chaînes live, et marque les films des catégories « adultes ». Un compte peut aussi être créé au démarrage depuis les variables d'environnement. Côté TV, seules la lecture des comptes et la sélection des catégories sont branchées ; créer, tester, modifier ou supprimer un compte n'a **aucun écran** dans les deux dépôts. Personas : opérateur, spectateur TV.

## User stories observées

- **US-BE-04.1** (P1) — En tant qu'opérateur, j'ajoute un compte Xtream (libellé, URL, port, identifiants) afin que le backend synchronise son catalogue.
  - Parcours : `POST /api/accounts` → authentification fournisseur → sync en tâche de fond (`app/api/accounts.py:29-60`) · aucun écran client (TV : méthode déclarée, jamais appelée) · Maestro : aucun
- **US-BE-04.2** (P1) — En tant qu'opérateur, je supprime un compte et toutes ses données disparaissent.
  - Parcours : `DELETE /api/accounts/{id}` (`app/api/accounts.py:88-106`) · aucun écran client · Maestro : aucun
- **US-BE-04.3** (P2) — En tant qu'opérateur, je teste la connexion d'un compte existant sans le modifier.
  - Parcours : `POST /api/accounts/{id}/test` (`app/api/accounts.py:109-135`) · aucun écran client · Maestro : aucun
- **US-BE-04.4** (P1) — En tant que spectateur TV, je coche les catégories VOD et séries à afficher afin de filtrer mon catalogue.
  - Parcours : écran de sélection des catégories Xtream → `PUT /api/accounts/{id}/categories` (`tv:app/src/main/java/com/chakir/plexhubtv/feature/xtream/XtreamCategorySelectionViewModel.kt:340-372`) · Maestro : aucun
- **US-BE-04.5** (P2) — En tant que spectateur TV, je rafraîchis la liste des catégories depuis le fournisseur sans perdre mes choix.
  - Parcours : `tv:app/src/main/java/com/chakir/plexhubtv/feature/xtream/XtreamCategorySelectionViewModel.kt:208` → `POST …/categories/refresh` · Maestro : aucun
- **US-BE-04.6** (P2) — En tant qu'opérateur, je déclare un compte dans l'environnement et le backend le crée au démarrage.
  - Parcours : `app/main.py:324-326` → `_auto_provision_xtream_account` · Maestro : sans objet

## Exigences fonctionnelles

### Comptes — lecture et création

- **FR-BE-139** — `GET /api/accounts` renvoie tous les comptes (actifs ou non), en camelCase, sans champ mot de passe, avec `outageStrikes`/`outageSince`. — *implémenté* — preuve `app/api/accounts.py:23-26`, `app/models/schemas.py:303-328` · test : aucun au niveau HTTP
- **FR-BE-140** — L'identifiant d'un compte est déterministe : 8 premiers caractères du MD5 de `base_url + username`. — *implémenté* — preuve `app/services/account_service.py:47-49`, `app/services/account_service.py:59` · test `tests/test_account_service.py:75`
- **FR-BE-141** — `POST /api/accounts` refuse (409 « Account already exists ») un compte dont l'identifiant existe déjà. — *implémenté* — preuve `app/services/account_service.py:61-65`, `app/api/accounts.py:35-36` · test `tests/test_account_service.py:75-85`
- **FR-BE-142** — La création authentifie d'abord contre le fournisseur ; tout échec (identifiants refusés, fournisseur injoignable, réponse invalide) donne 400 « Authentication failed: … » avec le message de l'exception. — *implémenté* — preuve `app/services/account_service.py:67-78`, `app/api/accounts.py:37-38` · test `tests/test_account_service.py:87-110`
- **FR-BE-143** — Le compte créé reprend du fournisseur `status`, `expiration_date` (secondes × 1000), `max_connections`, `allowed_formats`, `server_url` et `https_port`, et naît `is_active = true`. — *implémenté* — preuve `app/services/account_service.py:80-99` · test `tests/test_account_service.py:33-73`
- **FR-BE-144** — Après le commit de création, une synchronisation du compte est lancée en tâche de fond ; la réponse 201 n'attend pas sa fin. — *implémenté* — preuve `app/api/accounts.py:54-60` · test : aucun
- **FR-BE-145** — Le commit de création reste sur `commit_with_retry` (pas de vraie nouvelle tentative) pour ne pas ré-authentifier le fournisseur à chaque verrou. — *implémenté* — preuve `app/api/accounts.py:40-54` · test : aucun

### Comptes — mise à jour, suppression, test

- **FR-BE-146** — `PUT /api/accounts/{id}` applique une mise à jour partielle (`label`, `baseUrl`, `port`, `username`, `password`, `isActive`) et renvoie le compte relu ; 404 si inconnu. — *implémenté* — preuve `app/models/schemas.py:292-300`, `app/services/account_service.py:105-132`, `app/api/accounts.py:63-85` · test `tests/test_account_service.py:112-132`, `tests/test_accounts_retry.py:61`
- **FR-BE-147** — La mise à jour ne revérifie ni les identifiants auprès du fournisseur, ni l'unicité, et ne recalcule pas l'identifiant : un compte peut changer d'URL ou d'utilisateur en gardant l'identifiant de l'ancien couple. — *implémenté* (comportement constaté, voir RG-BE-04.2) — preuve `app/services/account_service.py:120-126` · test : aucun
- **FR-BE-148** — La mise à jour et la suppression passent par `write_with_retry` (session neuve par tentative) et survivent à un vrai verrou SQLite. — *implémenté* — preuve `app/api/accounts.py:76-82`, `app/api/accounts.py:99-104` · test `tests/test_accounts_retry.py:165`, `tests/test_accounts_retry.py:190`
- **FR-BE-149** — `DELETE /api/accounts/{id}` supprime en cascade les `media`, la file d'enrichissement, les catégories, les revues du scraper manuel, les chaînes live et l'EPG du compte, puis le compte ; 204, ou 404 si inconnu. — *implémenté* — preuve `app/services/account_service.py:135-162`, `app/api/accounts.py:88-106` · test `tests/test_account_service.py:134-176`
- **FR-BE-150** — La suppression nettoie les autres tables liées au compte (snapshot `media_group`, téléchargements, `ai_*`). — *partiel* (manque : seules six tables sont purgées ; le snapshot unifié n'est reconstruit qu'au pipeline suivant et les `download_job` ne sont pas touchés) — preuve `app/services/account_service.py:147-162` · test : aucun
- **FR-BE-151** — `POST /api/accounts/{id}/test` ré-authentifie avec les identifiants stockés et renvoie `status`, `expirationDate`, `maxConnections`, `allowedFormats`, sans rien écrire ; 404 si inconnu, 400 si l'authentification échoue ou si la réponse fournisseur est mal formée. — *implémenté* — preuve `app/services/account_service.py:165-187`, `app/api/accounts.py:109-135` · test `tests/test_account_service.py:181-210` (service ; la branche « réponse mal formée » n'est pas testée)
- **FR-BE-152** — Le mot de passe n'apparaît dans aucune réponse de compte. — *partiel* (manque : les schémas de réponse n'ont pas de champ mot de passe, mais le texte d'erreur 400 de la création et du test recopie `str(e)` ; pour une erreur HTTP du fournisseur, ce texte contient l'URL appelée, dont les paramètres `username`/`password`, voir Questions ouvertes) — preuve `app/models/schemas.py:303-328`, `app/api/accounts.py:38`, `app/api/accounts.py:118`, `app/services/xtream_service.py:59-69` · test : aucun

### Comptes — auto-provision au boot

- **FR-BE-153** — Au démarrage, si l'URL, l'utilisateur et le mot de passe Xtream sont tous fournis par l'environnement, le backend crée le compte correspondant ; cette étape tourne sur chaque processus, pas seulement le master. — *implémenté* — preuve `app/main.py:324-326`, `app/config.py:434` · test : aucun
- **FR-BE-154** — L'auto-provision ne fait rien si l'identifiant dérivé existe déjà, et un échec d'authentification est journalisé sans interrompre le démarrage. — *implémenté* — preuve `app/main.py:195-205`, `app/main.py:216-222` · test : aucun
- **FR-BE-155** — Le compte auto-provisionné porte le libellé fixe « Auto (env) » et n'est **pas** suivi d'une synchronisation immédiate (le pipeline planifié s'en charge). — *implémenté* — preuve `app/main.py:224-244` · test : aucun
- **FR-BE-156** — L'auto-provision partage la dérivation d'identifiant et la construction du compte avec le service. — *partiel* (manque : le calcul MD5 et le remplissage des champs sont recopiés dans `app/main.py` au lieu d'appeler `account_service`, deux copies à garder alignées) — preuve `app/main.py:195-197`, `app/main.py:224-241`, `app/services/account_service.py:47-49`, `app/services/account_service.py:80-99`

### Catégories — lecture et rafraîchissement

- **FR-BE-157** — `GET /api/accounts/{id}/categories` renvoie `{items, filterMode}` ; les items (`categoryId`, `categoryName`, `categoryType`, `isAllowed`, `lastFetchedAt`) sont triés par nom, VOD, séries **et live** mêlés ; le mode vaut `all` par défaut. — *implémenté* — preuve `app/api/categories.py:27-47`, `app/services/category_service.py:34-63`, `app/models/schemas.py:422-434` · test : aucun
- **FR-BE-158** — Pour un compte inconnu, `GET …/categories` répond 200 avec une liste vide et `filterMode = all`, pas 404. — *implémenté* (constaté) — preuve `app/services/category_service.py:49-63` · test : aucun
- **FR-BE-159** — `POST …/categories/refresh` recharge du fournisseur les catégories VOD et séries, les insère ou renomme, et renvoie `{message, vodCount, seriesCount, total}` en camelCase ; 404 si le compte est inconnu. — *implémenté* — preuve `app/api/categories.py:82-126`, `app/services/category_service.py:541-608` · test `tests/test_categories_refresh_camelcase.py:44-84`, `tests/test_category_service_refresh.py:28-61`
- **FR-BE-160** — Le rafraîchissement conserve le `is_allowed` des catégories connues ; une catégorie nouvelle naît autorisée. — *implémenté* — preuve `app/services/category_service.py:99-115` · test `tests/test_category_service_refresh.py:63`
- **FR-BE-161** — Le rafraîchissement couvre aussi les catégories live, supprime les catégories disparues chez le fournisseur et recalcule la visibilité. — *partiel* (manque : ni live, ni suppression, ni recalcul dans `refresh` ; seule la sync complète recharge le live, `app/workers/sync_worker.py:1095-1127`, et elle non plus ne supprime rien) — preuve `app/services/category_service.py:570-608` · test : aucun
- **FR-BE-162** — Une erreur fournisseur pendant `refresh` donne 500 avec le texte de l'exception, et ce texte est journalisé en `ERROR`. — *implémenté* (constaté) — preuve `app/api/categories.py:129-131` · test : aucun

### Catégories — filtrage

- **FR-BE-163** — `PUT /api/accounts/{id}/categories` reçoit `{filterMode, categories[]}` ; `filterMode` hors de `all`/`whitelist`/`blacklist` donne 400 ; toute autre erreur donne 500 ; succès = 200 `{"message": …}` sans `response_model`. — *implémenté* — preuve `app/api/categories.py:50-79`, `app/services/category_service.py:131-132` · test : aucun
- **FR-BE-164** — Les éléments de `categories` sont validés (identifiant, type, booléen). — *partiel* (manque : le champ est typé `list[dict]` ; un élément sans `categoryId` ou `categoryType` est ignoré en silence, et le modèle typé `CategoryUpdate` existe mais n'est utilisé nulle part) — preuve `app/models/schemas.py:437-451`, `app/services/category_service.py:210-219` · test : aucun
- **FR-BE-165** — Les catégories listées reçoivent leur `isAllowed` ; en `whitelist`, toutes les **autres** catégories du compte (tous types, live compris) passent à non autorisées, en `blacklist` à autorisées ; en `all`, seules les valeurs listées changent. — *implémenté* — preuve `app/services/category_service.py:205-239` · test : aucun
- **FR-BE-166** — Un compte inconnu passé à `PUT …/categories` renvoie 200 sans rien modifier. — *implémenté* (constaté, aucun contrôle d'existence ni de `rowcount` bloquant) — preuve `app/services/category_service.py:134-139`, `app/services/category_service.py:173-177` · test : aucun
- **FR-BE-167** — Après chaque mise à jour du filtre, la visibilité des médias du compte est recalculée dans la même requête. — *implémenté* — preuve `app/services/category_service.py:254-255` · test : aucun

### Propagation de la visibilité

- **FR-BE-168** — En mode `all`, tous les médias et toutes les chaînes live du compte deviennent visibles (`is_in_allowed_categories = true`). — *implémenté* — preuve `app/services/category_service.py:287-310` · test : aucun direct
- **FR-BE-169** — En `whitelist`/`blacklist`, films et séries sont visibles si leur catégorie (`media.filter`) est autorisée ; les mises à jour passent par lots de 500 identifiants. — *implémenté* — preuve `app/services/category_service.py:318-369` · test : aucun
- **FR-BE-170** — Les épisodes héritent de la visibilité de leur série (`grandparent_rating_key`) et ne sont jamais filtrés par leur propre catégorie. — *implémenté* — preuve `app/services/category_service.py:371-400` · test : aucun
- **FR-BE-171** — Les chaînes live sont filtrées par leurs catégories `live`. — *implémenté* — preuve `app/services/category_service.py:402-420` · test : aucun
- **FR-BE-172** — Le recalcul de visibilité est rejoué à chaque synchronisation, suivi du marquage adulte. — *implémenté* — preuve `app/workers/sync_worker.py:1713-1720` · test : voir CAP-BE-05

### Marquage adulte

- **FR-BE-173** — Une catégorie est adulte si son nom contient, sans tenir compte de la casse, un mot-clé de `ADULT_CATEGORY_KEYWORDS`, ou si son identifiant figure dans `ADULT_CATEGORY_IDS`. — *implémenté* — preuve `app/services/category_service.py:22-31`, `app/services/category_service.py:482-488`, `app/config.py:241` · test `tests/test_adult_classification.py:74-78`, `tests/test_adult_classification.py:151`
- **FR-BE-174** — Les films (et eux seuls) d'une catégorie VOD adulte sont marqués `is_adult` et leur `content_rating` est forcé à `ADULT_CONTENT_RATING` ; les autres films gardent leur classification. — *implémenté* — preuve `app/services/category_service.py:490-523` · test `tests/test_adult_classification.py:109-121`
- **FR-BE-175** — Le marquage est idempotent et rétroactif : tout est remis à non adulte puis recalculé, et une reclassification efface un marquage périmé. — *implémenté* — preuve `app/services/category_service.py:490-497` · test `tests/test_adult_classification.py:122-150`, `tests/test_category_service_real_lock.py:200`
- **FR-BE-176** — Le marquage s'exécute sur une session neuve par tentative (`write_with_retry`) et survit à un vrai verrou. — *implémenté* — preuve `app/services/category_service.py:528-533` · test `tests/test_category_service_real_lock.py:132-180`
- **FR-BE-177** — Modifier les catégories via l'API met à jour le marquage adulte. — *absent* (le marquage n'est appelé que par la sync, jamais par `PUT …/categories` ni `refresh`) — preuve de l'absence `app/services/category_service.py:254-255`, `app/workers/sync_worker.py:1718-1720`
- **FR-BE-178** — Un film adulte redevenu non adulte retrouve sa classification d'origine. — *partiel* (manque : le `content_rating` forcé n'est pas restauré par le reset, il reste « XXX » jusqu'à ce qu'une autre écriture le remplace) — preuve `app/services/category_service.py:493-497`, `app/services/category_service.py:514-523` · test : aucun sur ce point

### Stockage et sécurité

- **FR-BE-179** — Le mot de passe Xtream est chiffré au repos (Fernet, type `EncryptedString`) avec la clé `XTREAM_ENCRYPTION_KEY`, sinon une clé dérivée d'`AI_API_KEY` avec séparation de domaine. — *implémenté* — preuve `app/models/database.py:153`, `app/utils/crypto_fields.py:81-101`, `app/utils/crypto_fields.py:129-137`, `app/config.py:114` · test `tests/test_xtream_cred_encryption.py:48-72`, `tests/test_xtream_cred_encryption.py:124`
- **FR-BE-180** — Sans clé, le chiffrement est ouvert : le mot de passe est stocké en clair avec un avertissement unique ; une clé invalide ou changée fait relire le texte chiffré tel quel (journalisé), sans erreur. — *implémenté* — preuve `app/utils/crypto_fields.py:37-42`, `app/utils/crypto_fields.py:89-109`, `app/utils/crypto_fields.py:139-158` · test `tests/test_xtream_cred_encryption.py:75` (repli en clair ; la clé changée n'est pas testée)
- **FR-BE-181** — La migration 016 chiffre en place, une seule fois, les mots de passe restés en clair. — *implémenté* — preuve `app/db/migrations.py:662-722` · test `tests/test_xtream_cred_encryption.py:89-121`
- **FR-BE-182** — Les tables `xtream_accounts` et `xtream_categories` sont créées par `create_all` ; `category_filter_mode` vient de la migration 002 (défaut `all`), `outage_strikes`/`outage_since` de la migration 025 ; une catégorie est unique par `(account_id, category_id, category_type)`. — *implémenté* — preuve `app/models/database.py:145-193`, `app/db/migrations.py:120-135`, `app/db/migrations.py:1167-1213`
- **FR-BE-183** — Les huit routes de la capacité exigent `X-API-Key` (secret maître ou clé utilisateur active). — *implémenté* — preuve `app/main.py:665-666`, `app/api/deps.py:47-73` · test : voir CAP-BE-02
- **FR-BE-184** — Une interface d'administration web gère les comptes et catégories. — *absent* (aucune route `/admin` ne crée, modifie ou supprime un compte ou une catégorie ; l'UI admin n'en affiche que les pannes) — preuve de l'absence `app/api/admin.py:115-122`

### Contrat avec l'app TV

- **FR-BE-185** — L'app TV lit la liste des comptes (`GET /api/accounts`) pour importer les comptes du backend dans ses réglages. — *implémenté* — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:15-16`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/settings/SettingsViewModel.kt:942`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/settings/SettingsViewModel.kt:1166`
- **FR-BE-186** — L'app TV lit, rafraîchit et enregistre les catégories d'un compte. — *implémenté* — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:75-89`, `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/HybridCategoryRepository.kt:51`, `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/HybridCategoryRepository.kt:87`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/xtream/XtreamCategorySelectionViewModel.kt:155`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/xtream/XtreamCategorySelectionViewModel.kt:368` · test TV `tv:data/src/test/java/com/chakir/plexhubtv/data/repository/HybridCategoryRepositoryTest.kt:85`
- **FR-BE-187** — L'app TV crée, teste et supprime un compte Xtream du backend. — *mort* (déclaré dans Retrofit et encapsulé dans le repository, mais aucun ViewModel ni écran ne l'appelle ; `updateAccount` n'est même pas encapsulé ; les endpoints backend `POST`/`PUT`/`DELETE /api/accounts` sont **conservés volontairement** au Lot 10 : API publique testée, le code mort est côté TV) — preuve : déclarations Retrofit et méthodes de dépôt retiré au Lot 6 A (TV `d12ec586`, 2026-10-03) — plus de code TV à citer (trace du retrait : `tv:docs/retro-spec/technique/dette.md:282`)
- **FR-BE-188** — L'app TV n'utilise que les modes `all` et `whitelist` : tout coché ou rien coché → `all`, sinon `whitelist` avec les seules catégories VOD et séries. — *implémenté* (constaté, voir RG-BE-04.4) — preuve `tv:app/src/main/java/com/chakir/plexhubtv/feature/xtream/XtreamCategorySelectionViewModel.kt:345-372`

## Règles métier

- **RG-BE-04.1** — Un compte est identifié par le couple `(base_url, username)` au moment de sa création ; le port et le mot de passe n'entrent pas dans l'identifiant (le même utilisateur sur deux ports est un doublon). — preuve `app/services/account_service.py:47-49`
- **RG-BE-04.2** — Après un `PUT` qui change `base_url` ou `username`, l'identifiant ne correspond plus au couple : recréer l'ancien couple donne 409, recréer le nouveau crée un second compte vers le même fournisseur. — preuve `app/services/account_service.py:59-65`, `app/services/account_service.py:120-126`
- **RG-BE-04.3** — `whitelist` : non listé = masqué ; `blacklist` : non listé = visible ; `all` : tout visible, quelles que soient les valeurs `isAllowed`. — preuve `app/services/category_service.py:222-223`, `app/services/category_service.py:287-298`
- **RG-BE-04.4** — Le défaut de masse du mode `whitelist` s'applique à **tous** les types de catégories du compte. Comme la TV n'envoie que VOD et séries, un choix partiel depuis la TV passe toutes les catégories live à non autorisées, donc masque toutes les chaînes live du compte. — preuve `app/services/category_service.py:227-239`, `app/services/category_service.py:402-420`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/xtream/XtreamCategorySelectionViewModel.kt:345`
- **RG-BE-04.5** — Une catégorie apparue chez le fournisseur naît autorisée, y compris en mode `whitelist` : elle devient visible à la sync suivante sans avoir été cochée. — preuve `app/services/category_service.py:104-113`, `app/workers/sync_worker.py:1113-1139`
- **RG-BE-04.6** — Le marquage adulte ne concerne que les films ; le préfixe « [XXX] » n'est jamais stocké dans le titre (il est posé aux sorties, voir CAP-BE-09 et CAP-BE-16). — preuve `app/services/category_service.py:445-458`
- **RG-BE-04.7** — La suppression est définitive et immédiate (pas de corbeille) ; la visibilité par catégorie est un masquage réversible, distinct de `is_active` et de l'état de panne (CAP-BE-08). — preuve `app/services/account_service.py:135-162`, `app/models/schemas.py:324-326`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| `PUT`, `DELETE`, `…/test` ou `…/categories/refresh` sur un compte inconnu | 404 « Account not found » | `app/api/accounts.py:84`, `app/api/accounts.py:106`, `app/api/accounts.py:116`, `app/api/categories.py:125-126` |
| `GET` ou `PUT …/categories` sur un compte inconnu | 200 (liste vide, `all`) / 200 « updated successfully », rien d'écrit | `app/services/category_service.py:49-63`, `app/services/category_service.py:134-139` |
| Création d'un couple déjà enregistré | 409 | `app/api/accounts.py:35-36` |
| Identifiants refusés, ou fournisseur injoignable après 3 tentatives, à la création ou au test | 400 dans les deux cas : impossible de distinguer « identifiants faux » de « fournisseur en panne » | `app/services/xtream_service.py:66-93`, `app/services/account_service.py:77-78`, `app/services/account_service.py:184-185` |
| 429/502/503/504 du fournisseur | nouvelles tentatives (1, 2, 4 s, `Retry-After` respecté jusqu'à 60 s), puis l'erreur remonte | `app/services/xtream_service.py:66-93` |
| Réponse fournisseur mal formée à `…/test` | 400 | `app/api/accounts.py:123-135` |
| `filterMode` invalide | 400 | `app/api/categories.py:75-76` |
| Erreur inattendue sur les catégories (dont panne fournisseur au `refresh`) | 500, texte de l'exception renvoyé et journalisé | `app/api/categories.py:45-47`, `app/api/categories.py:77-79`, `app/api/categories.py:129-131` |
| Verrou SQLite tenu pendant `PUT`/`DELETE` d'un compte | nouvelle tentative sur session neuve | `app/api/accounts.py:82`, `app/api/accounts.py:104` |
| Verrou SQLite tenu pendant création, `refresh` ou `PUT …/categories` | pas de nouvelle tentative ; seul `busy_timeout` protège, puis `OperationalError` (500) | `app/api/accounts.py:54`, `app/api/categories.py:114`, `app/services/category_service.py:248` |
| Auto-provision : compte déjà là / authentification refusée | no-op journalisé / erreur journalisée, démarrage poursuivi | `app/main.py:203-205`, `app/main.py:220-222` |
| Clé de chiffrement absente ou changée à la lecture | le texte chiffré est renvoyé comme mot de passe (la sync et la lecture échoueront côté fournisseur), erreur journalisée | `app/utils/crypto_fields.py:144-158` |

## Dépendances

- Autres capacités : `CAP-BE-02` (garde `X-API-Key`), `CAP-BE-05` (sync lancée à la création ; recharge des catégories live et recalcul visibilité + adulte à chaque sync), `CAP-BE-08` (colonnes de panne exposées par `GET /api/accounts`), `CAP-BE-09` et `CAP-BE-11` (consomment `is_in_allowed_categories` et `is_adult`), `CAP-BE-16` (préfixe adulte dans la bibliothèque générée), `CAP-BE-22` (revues purgées à la suppression), `CAP-BE-24` (migrations 002, 016, 025).
- Autre dépôt : endpoints `backend:app/api/accounts.py:23`, `backend:app/api/categories.py:27` ; appels `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:15-16`, `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/backend/BackendApiService.kt:75-90` (catégories ; CRUD et test retiré au Lot 6 A (TV `d12ec586`, 2026-10-03)).
- Données : `xtream_accounts`, `xtream_categories` ; écrites en cascade : `media`, `enrichment_queue`, `scrape_review`, `live_channels`, `epg_entries` (renvoi `docs/retro-spec/technique/db/`).
- Configuration : clés `XTREAM_*` (dont `XTREAM_ENCRYPTION_KEY`), `AI_API_KEY`, `ADULT_*` (mécanisme seulement, aucune valeur ici).

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| Le texte d'erreur 400 de la création et du test, et le journal `ERROR` du `refresh` et de l'auto-provision, recopient `str(e)` d'une exception httpx ; pour une erreur HTTP, ce texte contient l'URL `player_api.php` avec `username`/`password` en paramètres (FR-BE-152, FR-BE-162). `…/test` renvoie ainsi le mot de passe **stocké** à tout porteur d'une clé utilisateur | sécurité | Remplacer `str(e)` par type d'exception + statut (règle déjà appliquée à TMDB/OMDb, `CLAUDE.md` §9 piège 21c) ; ajouter un test |
| La TV n'envoie que VOD et séries en `whitelist` : un choix partiel masque toutes les chaînes live du compte (RG-BE-04.4) | spec ↔ code (bug probable) | À trancher : limiter le défaut de masse aux types présents dans la requête, ou faire envoyer le live par la TV |
| Une catégorie nouvelle naît autorisée même en `whitelist` (RG-BE-04.5) | comportement risqué (contenu non choisi, possiblement adulte, rendu visible) | À trancher : naître `is_allowed = (mode != whitelist)` |
| `refresh` ignore le live, ne supprime rien et ne recalcule pas la visibilité ; les catégories disparues restent pour toujours (FR-BE-161) | incomplet | Aligner `refresh` sur la recharge de la sync et recalculer |
| Le `content_rating` forcé « XXX » n'est pas restauré quand un film cesse d'être adulte (FR-BE-178) ; les modifications de catégories par l'API n'actualisent pas le marquage (FR-BE-177) | spec ↔ code | Stocker la classification d'origine, ou relancer le marquage depuis `bulk_update_categories` |
| `GET`/`PUT …/categories` répondent 200 sur un compte inconnu, alors que `refresh` répond 404 | incohérence API | Contrôler l'existence du compte dans les trois routes |
| Fournisseur injoignable et identifiants faux donnent le même 400 | sémantique HTTP | Distinguer 502/504 des refus d'authentification |
| `PUT /api/accounts/{id}` : ni ré-authentification, ni contrôle d'unicité, ni recalcul d'identifiant (RG-BE-04.2) | incohérence create ↔ update | À trancher : interdire la modification de `baseUrl`/`username`, ou revalider |
| La suppression laisse le snapshot `media_group` et les `download_job` du compte (FR-BE-150) | incomplet | Reconstruire le snapshot après suppression, décider du sort des téléchargements |
| `CategoryUpdateRequest.categories` est un `list[dict]` ; le modèle `CategoryUpdate` est mort ; `PUT …/categories` n'a pas de `response_model` (dette « dicts bruts » de `CLAUDE.md` §3, confirmée) | dette + code mort | Typer la requête et la réponse |
| `CLAUDE.md` §2 situe `update_media_adult_flags` à category_service.py:410 et §9 piège 15 le filtre `type='movie'` à category_service.py:445 ; lignes réelles `app/services/category_service.py:441` et `app/services/category_service.py:495` ; §2 situe aussi `create_all` à database.py:92 (réel `app/db/database.py:128`) | doc périmée | Corriger au prochain `/sync-context` |
| Commentaire du modèle et docstring d'`upsert_category` : « vod ou series » ; la table contient aussi `live` | commentaire périmé | Corriger `app/models/database.py:184` |
| Auto-provision : logique d'identifiant et de construction du compte dupliquée dans `app/main.py` (FR-BE-156) | dette (`CR-A05`) | Appeler `account_service` |
| Création, test, modification et suppression de compte : aucune interface (ni TV, ni admin web) ; le code TV correspondant est mort (FR-BE-184, FR-BE-187). Les endpoints `POST`/`PUT`/`DELETE /api/accounts` sont conservés volontairement (API publique testée). | code mort (TV) / fonctionnalité sans UI | À trancher par Chakir : écran de gestion, ou retrait du code TV |
| Aucun test HTTP de `GET /api/accounts`, `GET`/`PUT …/categories`, de la propagation de visibilité, de l'auto-provision ; aucun test que le mot de passe n'est jamais renvoyé | test manquant | Priorité : `PUT …/categories` en `whitelist` avec du live, et fuite de mot de passe dans les 400 |
| Aucun parcours Maestro sur les comptes ou les catégories | test manquant | Ajouter un parcours « sélection de catégories » |

## Questions ouvertes

- Le 400 de `POST /api/accounts/{id}/test` contient-il réellement l'URL fournisseur avec le mot de passe quand le fournisseur répond 401/403/404 ? — à vérifier par test (fournisseur simulé `respx` renvoyant 403, lecture du `detail`).
- Le journal de l'auto-provision et du `refresh` contient-il ces mêmes identifiants dans ce cas ? — à vérifier par test (capture des logs `plexhub`).
- Après un choix partiel depuis la TV, les chaînes live du compte disparaissent-elles bien de `/api/live` ? — à vérifier par test (compte avec catégories live, `PUT` whitelist VOD seule, lecture de `/api/live`).
- Un `PUT /api/accounts/{id}` avec `isActive = false` masque-t-il le compte partout (catalogue, génération, DAV, sync) et sans purge ? — à vérifier par test (relève aussi de CAP-BE-05 et CAP-BE-16).
- Un mot de passe changé par `PUT` est-il bien chiffré (le type `EncryptedString` s'applique-t-il à un `UPDATE … VALUES` Core) ? — à vérifier par test (lecture brute de la colonne après `PUT`).
- Deux `PUT …/categories` concurrents pour le même compte laissent-ils un état cohérent entre `category_filter_mode`, `is_allowed` et la visibilité ? — à vérifier par test.
- Que fait l'app TV des champs `outageStrikes`/`outageSince`, absents de son DTO de compte ? — à vérifier par test côté TV (désérialisation d'une réponse réelle).
