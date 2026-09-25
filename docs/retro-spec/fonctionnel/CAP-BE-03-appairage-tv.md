# CAP-BE-03 — Appairage TV (device-flow)

**Reconstituée le** : 2026-09-25 · **HEAD** : `08c2fae` · **Sources** : inventaire, code, tests, app TV (lecture seule, preuves `tv:`). Aucun PRD ne couvre cette capacité (`docs/10-prd-*.md` ne traitent que les téléchargements) : pas de tableau d'écart fiche ↔ PRD ; les écarts sont face à `CLAUDE.md` §9 (piège 19), `docs/architecture/flux-cles.md` §5.6 et au guide utilisateur de l'app TV.
**Périmètre** : `POST /api/tv-auth/start`, `POST /api/tv-auth/approve`, `GET /api/tv-auth/status`, `POST /api/tv-auth/complete` (`app/api/tv_auth.py`), chiffrement du payload (`app/utils/payload_crypto.py`), limiteurs de `start` (`app/utils/rate_limit.py`), table `tv_auth_sessions` (migration 009), allow-list de boot (`app/api/route_audit.py`). Renvoi : `docs/retro-spec/_genere/inventaire.md` (marque `/approve` `public ?` : faux positif de l'outil, la garde est posée sur la route, `app/api/tv_auth.py:386`). Une fiche côté TV traite le même flux depuis l'autre bout : `CAP-TV-04-appairage-tv-backend.md` dans le dépôt PlexHubTV.

## Résumé

Une TV neuve n'a pas de clé : elle demande une session au backend, affiche un code court (et un QR), puis interroge le backend jusqu'à ce qu'un appareil déjà configuré valide ce code et lui confie une configuration (jeton Plex, URL du backend, clé API). Le backend chiffre cette configuration au repos, ne la livre qu'une fois à la TV, puis la détruit à l'acquittement. Trois des quatre endpoints sont publics, le quatrième (`approve`) exige une clé API. Le seul point d'entrée que la TV n'appelle pas est `approve` ; **aucun client de `approve` n'existe dans les deux dépôts** (voir FR-BE-075). Personas : spectateur TV, appareil d'appairage.

## User stories observées

- **US-BE-03.1** (P1) — En tant que spectateur TV, je demande un code d'appairage afin de configurer ma TV sans tout saisir à la télécommande.
  - Parcours : Réglages → Intégrations → « Pair this TV » → écran `PairingScreen` (`tv:app/src/main/java/com/chakir/plexhubtv/feature/settings/categories/IntegrationsSettingsScreen.kt:54-62`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/pairing/PairingViewModel.kt:55-72`) · Maestro : aucun
- **US-BE-03.2** (P1) — En tant que spectateur TV, je vois le code, l'adresse et un QR, et l'écran attend l'approbation avec un compte à rebours.
  - Parcours : `PairingUiState.AwaitingApproval` (`tv:app/src/main/java/com/chakir/plexhubtv/feature/pairing/PairingViewModel.kt:80-132`) · Maestro : aucun
- **US-BE-03.3** (P1) — En tant qu'appareil d'appairage, je saisis le code affiché sur la TV avec ma clé API afin de lui transmettre sa configuration.
  - Parcours : `POST /api/tv-auth/approve` (`app/api/tv_auth.py:382-444`) · aucun client dans les deux dépôts · Maestro : aucun
- **US-BE-03.4** (P1) — En tant que spectateur TV, ma TV reçoit et applique la configuration, puis confirme, sans que le secret reste stocké côté serveur.
  - Parcours : `handleApproved` → `complete` (`tv:app/src/main/java/com/chakir/plexhubtv/feature/pairing/PairingViewModel.kt:134-162`) · Maestro : aucun
- **US-BE-03.5** (P2) — En tant qu'opérateur, je borne les créations de sessions anonymes pour qu'un flot d'appels à `start` ne fasse pas grossir la base ni saturer l'écriture SQLite.
  - Parcours : limiteurs par IP + plafond global (`app/api/tv_auth.py:243-328`) · Maestro : aucun

## Exigences fonctionnelles

### Ouverture de session (`start`)

- **FR-BE-065** — `POST /api/tv-auth/start` est public et renvoie 201 avec `deviceCode`, `userCode`, `verificationUri`, `expiresIn` et `interval`, sérialisés en camelCase. — *implémenté* — preuve `app/api/tv_auth.py:222-227`, `app/api/tv_auth.py:369-375`, `app/main.py:647`, `app/api/route_audit.py:83` · test `tests/test_tv_auth.py:138-148`
- **FR-BE-066** — Le corps est optionnel ; `deviceName` est borné à 120 caractères (422 au-delà). — *implémenté* — preuve `app/api/tv_auth.py:88-91`, `app/api/tv_auth.py:228-232`, `app/api/tv_auth.py:269` · test : aucun sur la borne
- **FR-BE-067** — Le `deviceCode` est un jeton opaque de 32 octets aléatoires (`token_urlsafe`), unique en base. — *implémenté* — preuve `app/api/tv_auth.py:268`, `app/models/database.py:282`, `app/models/database.py:294` · test `tests/test_tv_auth.py:144` (longueur seulement)
- **FR-BE-068** — Le `userCode` compte 8 caractères tirés d'un alphabet de 31 symboles sans `0`/`O`/`1`/`I`/`L`, et s'affiche sous la forme `ABCD-EFGH`. — *implémenté* — preuve `app/api/tv_auth.py:58-59`, `app/api/tv_auth.py:145-152` · test `tests/test_tv_auth.py:145`
- **FR-BE-069** — En cas de collision de `userCode` (index unique), le backend retire un nouveau code, jusqu'à 5 tentatives, puis répond 503 « Could not allocate a pairing code ». — *implémenté* — preuve `app/api/tv_auth.py:330-348`, `app/api/tv_auth.py:359-363`, `app/models/database.py:295` · test : aucun
- **FR-BE-070** — Si aucune clé de chiffrement n'est résolue, `start` répond 503 « TV pairing not configured » avant tout limiteur et toute écriture. — *implémenté* — preuve `app/api/tv_auth.py:160-165`, `app/api/tv_auth.py:241` · test `tests/test_tv_auth.py:582-601`
- **FR-BE-071** — `start` applique deux limiteurs par IP à fenêtre glissante (5 requêtes / 60 s, et 20 requêtes / durée du TTL, par défaut) ; un dépassement renvoie 429 avec `Retry-After`. — *implémenté* — preuve `app/api/tv_auth.py:243-263`, `app/utils/rate_limit.py:140`, `app/utils/rate_limit.py:210`, `app/config.py:80-90` · test `tests/test_rate_limit.py:216` (le second limiteur n'est jamais testé seul, seulement désactivé)
- **FR-BE-072** — `start` refuse (429, `Retry-After` fixe de 30 s) quand le nombre de sessions au statut littéral `pending` atteint le plafond global (500 par défaut, désactivable à 0). — *implémenté* — preuve `app/api/tv_auth.py:309-328`, `app/api/tv_auth.py:354-358`, `app/utils/rate_limit.py:226`, `app/config.py:95` · test `tests/test_rate_limit.py:248`, `tests/test_rate_limit.py:269`, `tests/test_rate_limit.py:97`, `tests/test_rate_limit.py:105`
- **FR-BE-073** — Chaque `start` supprime, dans la même transaction que l'insertion, les sessions expirées depuis plus d'une heure. — *implémenté* — preuve `app/api/tv_auth.py:61`, `app/api/tv_auth.py:305-307` · test : aucun
- **FR-BE-074** — `start` fournit une `verificationUri` construite depuis l'URL de base de la requête (`<base>api/tv-auth/approve`). — *implémenté* — preuve `app/api/tv_auth.py:372` · test `tests/test_tv_auth.py:148`
- **FR-BE-075** — Un client (page web ou appli) sert la `verificationUri` à l'humain : ouvrir l'adresse affichée, ou scanner le QR (`<uri>?userCode=<code>`), permet de saisir ou de valider le code. — *absent* (annoncé par l'écran TV et par `tv:docs/40-user-guide-fr.md:322-323` ; l'URI cible une route **POST uniquement**, il n'existe aucune route GET ni template d'appairage, et le paramètre `userCode` du QR n'est lu nulle part côté backend) — preuve de l'absence `app/api/tv_auth.py:372`, `app/api/tv_auth.py:382-387`, `app/api/tv_auth.py:451` · contenu du QR côté TV `tv:app/src/main/java/com/chakir/plexhubtv/feature/pairing/PairingViewModel.kt:90`

### Approbation (`approve`)

- **FR-BE-076** — `approve` exige l'en-tête `X-API-Key` : le secret maître (comparaison en temps constant) **ou** toute clé utilisateur active ; sinon 401. — *implémenté* — preuve `app/api/tv_auth.py:382-387`, `app/api/deps.py:47-73` · test `tests/test_tv_auth.py:547-557` (absence et fausse clé seulement ; aucun test tv-auth de la clé utilisateur)
- **FR-BE-077** — `approve` refuse (422) un `payload` vide et un `userCode` hors de 4 à 16 caractères. — *implémenté* — preuve `app/api/tv_auth.py:104-108`, `app/api/tv_auth.py:399-400` · test `tests/test_tv_auth.py:560-567`
- **FR-BE-078** — Le `userCode` saisi est normalisé (majuscules, séparateurs retirés) : `abcd-efgh`, `ABCD EFGH` et `ABCDEFGH` désignent la même session. — *implémenté* — preuve `app/api/tv_auth.py:155-157`, `app/api/tv_auth.py:402` · test `tests/test_tv_auth.py:570-579`
- **FR-BE-079** — `approve` répond 404 pour un code inconnu, 410 pour une session expirée et 409 pour une session déjà `approved` ou `completed`. — *implémenté* — preuve `app/api/tv_auth.py:403-415` · test `tests/test_tv_auth.py:528-544`, `tests/test_tv_auth.py:350-359`, `tests/test_tv_auth.py:427-433`
- **FR-BE-080** — `approve` chiffre le payload (Fernet), le stocke, passe la session à `approved` et horodate l'approbation. — *implémenté* — preuve `app/api/tv_auth.py:425-441` · test `tests/test_tv_auth.py:608-632`
- **FR-BE-081** — Le contenu du payload est validé (clés attendues, borne de taille). — *partiel* (manque : le payload est un `dict` libre ; seule la non-vacuité est contrôlée, alors que la TV n'en lit que `plexToken`, `backendUrl` et `apiKey` et ignore le reste en silence) — preuve `app/api/tv_auth.py:108`, `app/api/tv_auth.py:399-400` · côté TV `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/TvPairingRepositoryImpl.kt:130-137` · test : aucun

### Sondage (`status`)

- **FR-BE-082** — `GET /api/tv-auth/status` est public ; le code se passe en `deviceCode` (préféré) ou en `device_code` (ancien nom, toléré) ; l'absence ou une longueur hors de 16 à 128 caractères donne 422. — *implémenté* — preuve `app/api/tv_auth.py:451-470` · test `tests/test_tv_auth.py:217-249`
- **FR-BE-083** — `status` répond 404 pour un code inconnu ; une session expirée répond **200** `expired` (pas 410) ; `expiresIn` est renseigné tant que la session vit et vaut `null` une fois `completed`. — *implémenté* — preuve `app/api/tv_auth.py:472-482`, `app/api/tv_auth.py:547-550` · test `tests/test_tv_auth.py:151-189` (`expiresIn` vérifié à la ligne 161), `tests/test_tv_auth.py:337-347`, `tests/test_tv_auth.py:528-544`
- **FR-BE-084** — Le payload déchiffré est livré **une seule fois**, au premier sondage qui suit l'approbation ; l'attribution passe par un `UPDATE` conditionnel (`payload_delivered IS FALSE`) dont le `rowcount` désigne l'unique gagnant en cas de sondages concurrents. — *implémenté* — preuve `app/api/tv_auth.py:484-543` · test `tests/test_tv_auth.py:192-210`, `tests/test_tv_auth.py:251-330`
- **FR-BE-085** — Si le payload d'une session `approved` ne peut être déchiffré (clé changée ou absente), `status` répond 503 « Pairing payload unavailable » **sans** marquer la livraison ; un blob manquant sur une session `approved` donne 500. — *implémenté* — preuve `app/api/tv_auth.py:485-495` · test : aucun au niveau HTTP (la couche crypto seule est testée : `tests/test_payload_crypto.py:99-113`)
- **FR-BE-086** — `status` n'est jamais limité : aucun limiteur n'est posé sur le sondage, même après épuisement du budget de `start`. — *implémenté* — preuve `app/api/tv_auth.py:451-463`, `app/utils/rate_limit.py:6-14` · test `tests/test_rate_limit.py:304-326`

### Clôture (`complete`)

- **FR-BE-087** — `complete` est public et fait passer une session `approved` à `completed`, horodate et efface le payload chiffré ; réponse 200. — *implémenté* — preuve `app/api/tv_auth.py:557-601` · test `tests/test_tv_auth.py:151-189`, `tests/test_tv_auth.py:635-658`
- **FR-BE-088** — `complete` répond 404 (code inconnu), 410 (expirée), 409 (déjà `completed`) et 409 (pas encore `approved`). — *implémenté* — preuve `app/api/tv_auth.py:567-575` · test `tests/test_tv_auth.py:401-424`, `tests/test_tv_auth.py:528-544`
- **FR-BE-089** — `complete` atteste que la TV a bien reçu le payload (« TV acknowledges », docstring du module). — *partiel* (manque : aucun contrôle de `payload_delivered` ; un `complete` valide sur une session `approved` non livrée détruit le payload sans qu'il ait été remis, et le porteur du seul `deviceCode` peut le déclencher) — preuve `app/api/tv_auth.py:16-17`, `app/api/tv_auth.py:572-596` · test : aucun

### Cycle de vie, chiffrement et exploitation

- **FR-BE-090** — Une session expire à `created_at + TV_AUTH_TTL_SECONDS` (900 s par défaut), de façon **paresseuse** : la bascule en `expired` et l'effacement du payload se font à l'appel de `approve`, `status` ou `complete`. — *implémenté* — preuve `app/api/tv_auth.py:181-215`, `app/api/tv_auth.py:265-266`, `app/config.py:69` · test `tests/test_tv_auth.py:337-394`
- **FR-BE-091** — Une session `completed` n'expire jamais et `expired` est terminal (idempotent). — *implémenté* — preuve `app/api/tv_auth.py:199-200` · test : aucun dédié
- **FR-BE-092** — Le payload est chiffré par Fernet ; la clé vient de `TV_AUTH_ENCRYPTION_KEY`, sinon d'une dérivation de `AI_API_KEY` à contexte dédié ; une clé explicite invalide n'entraîne **pas** de repli sur la dérivation ; sans clé, la fonctionnalité est fermée (503). — *implémenté* — preuve `app/utils/payload_crypto.py:64-100`, `app/config.py:68` · test `tests/test_payload_crypto.py:34-90`, `tests/test_payload_crypto.py:116-124`
- **FR-BE-093** — Le clair n'est jamais stocké : le blob chiffré est effacé à `complete` et à l'expiration ; il n'est **pas** effacé à la livraison. — *implémenté* — preuve `app/api/tv_auth.py:210`, `app/api/tv_auth.py:593`, `app/api/tv_auth.py:528-536` · test `tests/test_tv_auth.py:608-658`, `tests/test_tv_auth.py:362-394`
- **FR-BE-094** — Les journaux de `tv_auth` ne contiennent ni `deviceCode`, ni `userCode`, ni payload, ni clé : seuls l'identifiant de session, le nom d'appareil et les événements de cycle de vie y figurent. — *implémenté* — preuve `app/api/tv_auth.py:365-368`, `app/api/tv_auth.py:443`, `app/api/tv_auth.py:491`, `app/api/tv_auth.py:540`, `app/api/tv_auth.py:600` · test `tests/test_payload_crypto.py:127-137` (matériau de clé seulement)
- **FR-BE-095** — Les écritures de `start`, `approve`, `complete` et de l'expiration passent par `write_with_retry` (session neuve par tentative) et survivent à un vrai verrou SQLite ; l'attribution de livraison de `status` reste volontairement sur `commit_with_retry`. — *implémenté* — preuve `app/api/tv_auth.py:214`, `app/api/tv_auth.py:351`, `app/api/tv_auth.py:441`, `app/api/tv_auth.py:598`, `app/api/tv_auth.py:506-536` · test `tests/test_tv_auth.py:441-521`, `tests/test_tv_auth.py:773-848`
- **FR-BE-096** — Les sessions vivent dans la table `tv_auth_sessions`, créée par la migration 009 avec deux index uniques (`device_code`, `user_code`) et deux index simples (`expires_at`, `status`). — *implémenté* — preuve `app/db/migrations.py:347-383`, `app/models/database.py:279-298` · test `tests/test_tv_auth.py:665-697`
- **FR-BE-097** — Seuls `start`, `status` et `complete` sont publics ; `approve` reste gardé, et l'application refuse de démarrer si une autre route `/api/*` est publiée sans garde. — *implémenté* — preuve `app/api/route_audit.py:72-87` · test `tests/test_route_auth_assertion.py:164-173`, `tests/test_route_auth_assertion.py:185-190`, `tests/test_route_auth_assertion.py:192`
- **FR-BE-098** — Les colonnes `created_at`, `approved_at`, `completed_at` et `device_name` servent l'audit ou l'affichage. — *mort* (écrites à chaque session, jamais relues par une API, une page admin ou une métrique ; `device_name` n'est que journalisé depuis la variable locale à la création) — preuve `app/models/database.py:287-291`, `app/api/tv_auth.py:338-339`, `app/api/tv_auth.py:436`, `app/api/tv_auth.py:592`

### Contrat avec l'app TV

- **FR-BE-099** — L'app TV consomme `start`, `status` et `complete` avec des DTO camelCase alignés sur les réponses du backend ; elle sonde avec l'ancien nom `device_code`, que le backend tolère. — *implémenté* — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/tvauth/TvAuthApiService.kt:27-44`, `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/tvauth/dto/TvAuthDtos.kt:17-58` · test backend `tests/test_tv_auth.py:229-238` · test TV `tv:app/src/test/java/com/chakir/plexhubtv/feature/pairing/PairingViewModelTest.kt:92-131`
- **FR-BE-100** — Le backend impose l'intervalle de sondage (erreur de type `slow_down`, comme le laisse entendre « RFC 8628-like »). — *absent* (seul `interval` = 5 s est **renvoyé** à titre indicatif ; ni refus ni ralentissement, volontairement : `/status` ne doit jamais être limité) — preuve de l'absence `app/api/tv_auth.py:60`, `app/api/tv_auth.py:374`, `app/api/tv_auth.py:451-463`

## Règles métier

- **RG-BE-03.1** — Machine d'états d'une session : `pending` → `approved` → `completed` ; `pending` ou `approved` → `expired` dès que l'heure dépasse `expires_at` (borne incluse : à `expires_at` exactement, la session vit encore). — preuve `app/api/tv_auth.py:70-73`, `app/api/tv_auth.py:199-202`
- **RG-BE-03.2** — Le payload est livré à la TV au **sondage**, pas à l'acquittement : la livraison est marquée avant l'envoi de la réponse. Si la réponse est perdue, les sondages suivants renvoient `approved` sans payload et la TV doit relancer un appairage. — preuve `app/api/tv_auth.py:528-543`, `tv:app/src/main/java/com/chakir/plexhubtv/feature/pairing/PairingViewModel.kt:138-143`
- **RG-BE-03.3** — Le plafond global est le seul garde qui tienne sous usurpation d'IP (`CF-Connecting-IP` et `X-Forwarded-For` sont posés par l'appelant hors tunnel) ; les limiteurs par IP sont une courtoisie. Le comptage porte sur le statut littéral `pending` : des sessions expirées jamais relues comptent jusqu'à leur purge à `expires_at` + 1 h. — preuve `app/api/tv_auth.py:309-323`, `app/utils/rate_limit.py:210-223`, `CLAUDE.md` §9 piège 19(d)
- **RG-BE-03.4** — Le limiteur court enregistre son passage avant l'évaluation du second : un refus du second laisse le passage du premier compté. Un appel refusé n'est pas enregistré dans le limiteur qui le refuse. — preuve `app/api/tv_auth.py:243-263`, `app/utils/rate_limit.py:140`
- **RG-BE-03.5** — Un payload posé par `approve` sur une session déjà livrée est impossible (409 dès que le statut n'est plus `pending`) ; en revanche `approve`, `complete` et l'expiration écrivent par `UPDATE` aveugle sur l'identifiant, sans condition de statut : deux appels concurrents peuvent réussir tous deux (dernier écrivain gagnant). — preuve `app/api/tv_auth.py:206-214`, `app/api/tv_auth.py:429-441`, `app/api/tv_auth.py:586-598`
- **RG-BE-03.6** — Côté TV, le sondage attend un intervalle avant le premier appel, double le délai à chaque échec (plafond 60 s, remise à la base au premier succès), n'interroge plus au-delà du TTL local et traite 404 et 410 du sondage comme « expiré ». — preuve `tv:app/src/main/java/com/chakir/plexhubtv/feature/pairing/PairingViewModel.kt:80-132`, `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/TvPairingRepositoryImpl.kt:72-75` · test `tv:app/src/test/java/com/chakir/plexhubtv/feature/pairing/PairingViewModelTest.kt:224-271`
- **RG-BE-03.7** — Côté TV, une configuration reçue est applicable si elle porte un jeton Plex, ou une URL de backend **et** une clé API ; l'URL et la clé sont enregistrées avant que le jeton Plex ne soit testé, donc un jeton refusé laisse la configuration backend déjà écrite. — preuve `tv:domain/src/main/java/com/chakir/plexhubtv/domain/model/TvPairing.kt:46-47`, `tv:domain/src/main/java/com/chakir/plexhubtv/domain/usecase/pairing/ApplyTvPairingPayloadUseCase.kt:36-63`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| Clé de chiffrement absente | `start` et `approve` : 503 ; `status` sur session `approved` non livrée : 503 ; `status` sur `pending`/`completed` continue de répondre | `app/api/tv_auth.py:160-165`, `app/api/tv_auth.py:488-495` |
| `approve` sans clé API et sans clé de chiffrement | 401 (la garde passe avant le contrôle de configuration) | `tests/test_tv_auth.py:582-601` |
| Code inconnu | 404 partout (`status`, `complete`, `approve`) ; un `deviceCode` de longueur invalide donne 422 et non 404 | `app/api/tv_auth.py:407`, `app/api/tv_auth.py:476`, `app/api/tv_auth.py:567` |
| Session expirée | `status` : 200 `expired` ; `approve`/`complete` : 410 | `app/api/tv_auth.py:479-480`, `app/api/tv_auth.py:410`, `app/api/tv_auth.py:570` |
| Double `approve` ou `approve` d'une session `completed` | 409 avec le statut dans le message | `app/api/tv_auth.py:412-415` |
| `complete` avant `approve` / deux fois | 409 / 409 | `app/api/tv_auth.py:572-575` |
| `status` après `complete` | 200 `completed`, `payload` et `expiresIn` nuls | `app/api/tv_auth.py:547-550` |
| Deux sondages simultanés après approbation | un seul reçoit le payload, l'autre reçoit `approved` sans payload | `app/api/tv_auth.py:538-550` |
| 5 collisions de `userCode` | 503 « Could not allocate a pairing code, retry » | `app/api/tv_auth.py:359-363` |
| Verrou SQLite persistant | une `OperationalError` non gérée après les tentatives de retry (500 par défaut, non testé) | `app/api/tv_auth.py:351`, `app/utils/db_retry.py:28` |
| TV : `start` en 429 ou 503 | un seul message « serveur injoignable », `Retry-After` ignoré, relance manuelle | `tv:app/src/main/java/com/chakir/plexhubtv/feature/pairing/PairingViewModel.kt:65-69` |
| TV : sondage en 429/503/500 | traité comme une panne réseau (backoff) ; un 503 « payload unavailable » fait boucler jusqu'au TTL puis afficher « expiré » | `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/TvPairingRepositoryImpl.kt:72-83` |
| TV : `complete` en échec | journalisé « non fatal », l'écran passe quand même à « terminé » | `tv:app/src/main/java/com/chakir/plexhubtv/feature/pairing/PairingViewModel.kt:149-155` |
| TV : URL du backend non saisie | écran « non configuré », aucun appel réseau | `tv:app/src/main/java/com/chakir/plexhubtv/feature/pairing/PairingViewModel.kt:56-59`, `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/TvPairingRepositoryImpl.kt:44` |

## Dépendances

- Autres capacités : `CAP-BE-02` (clés API : la garde de `approve` accepte les clés utilisateur), `CAP-BE-01` (allow-list de boot partagée avec `/api/health`), `CAP-BE-24` (migration 009 et maintenance de la base).
- Autre dépôt : endpoints `backend:app/api/tv_auth.py:222`, `backend:app/api/tv_auth.py:382`, `backend:app/api/tv_auth.py:451`, `backend:app/api/tv_auth.py:557` ; appels `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/tvauth/TvAuthApiService.kt:27-44` (`approve` non appelé par la TV) ; fiche jumelle `CAP-TV-04`.
- Données : table `tv_auth_sessions` (`docs/retro-spec/technique/db/tv_auth_sessions.md`).
- Configuration : clés `TV_AUTH_*` et `AI_API_KEY` (mécanisme seulement ; aucune valeur documentée ici).

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| L'écran TV, son QR et le guide utilisateur annoncent un appairage « depuis un téléphone / ordinateur », mais aucune page ni appli n'appelle `approve` ; le QR ouvre une route POST-only (FR-BE-075) | spec ↔ code (absent) | À trancher par Chakir : page d'approbation servie par le backend, appli mobile, ou retrait de la promesse |
| `flux-cles.md` §5.6 et `docs/architecture/ARCHITECTURE.md:277` décrivent encore CR-F06 (`device_code` snake_case) et CR-F07 (livraison non atomique) comme **ouverts** ; les deux sont corrigés (`app/api/tv_auth.py:456-461`, `app/api/tv_auth.py:528-543`) ; `docs/audit/v1/DELTA.md:59-60` le dit déjà | doc périmée | Mettre à jour §5.6 au prochain `/sync-context` (aussi la référence de TTL, `app/config.py:69`, que la section cite à une ligne périmée) |
| La docstring du module dit « approve requires the backend shared secret » ; le code accepte aussi toute clé utilisateur active (FR-BE-076) | spec ↔ code | Décision produit : restreindre à la clé maîtresse, ou corriger la docstring |
| `complete` ne contrôle pas `payload_delivered` (FR-BE-089) ; la livraison est marquée avant l'envoi de la réponse (RG-BE-03.2) | comportement risqué | À trancher : exiger la livraison avant `complete`, prévoir une re-livraison bornée |
| Le blob chiffré reste en base entre la livraison et `complete`/l'expiration, jusqu'à `expires_at` + 1 h au plus (FR-BE-093) | sécurité (mécanisme) | À trancher : effacer le blob à la livraison |
| Purge des sessions expirées uniquement dans `start` ; sans appel à `start`, rien n'est supprimé, et 500 sessions `pending` jamais relues bloquent `start` jusqu'à TTL + 1 h (RG-BE-03.3) | dette | À trancher : compter les sessions non expirées, ou purge planifiée |
| `device_name` (texte libre du client) est journalisé sans nettoyage (`app/api/tv_auth.py:366`) | sécurité (injection de lignes de log) | Nettoyer ou borner les caractères |
| Aucun test sur : collision de `userCode` (FR-BE-069), purge à 1 h (FR-BE-073), 503 « payload unavailable » en HTTP (FR-BE-085), éviction mémoire des limiteurs (`app/utils/rate_limit.py:95`), approbations concurrentes (RG-BE-03.5) | test manquant | Ajouter les cas en priorité FR-BE-069 et FR-BE-085 |
| Côté TV, une URL de backend doit déjà être saisie pour lancer l'appairage, alors que le payload livre justement `backendUrl` et `apiKey` ; seul le jeton Plex est appairable de bout en bout sur une TV neuve | spec ↔ code | À trancher (voir `CAP-TV-04`) |
| Aucun parcours Maestro pour l'appairage (le testTag `pairing_cta` existe, inutilisé) ; aucun test du dépôt TV sur `TvPairingRepositoryImpl` ni sur le contrat JSON réel du backend | test manquant | Ajouter un test de contrat DTO ↔ réponses backend |

## Questions ouvertes

- Quel client réalise réellement `approve` (script `curl`, appli mobile, page hors dépôts) ? Scanner le QR mène-t-il à une réponse exploitable ? — à vérifier par test : `GET /api/tv-auth/approve?userCode=…` sur une instance de test.
- Le `deviceCode` circule en query string sur `GET /status` : figure-t-il en clair dans le journal d'accès d'uvicorn (aucun filtre ni `--no-access-log` relevé dans le code) ? — à vérifier par test : lancer l'image, sonder, lire la sortie du conteneur.
- `verificationUri` dépend de `request.base_url` : derrière le tunnel ou un reverse-proxy, le schéma et l'hôte renvoyés sont-ils ceux que la TV et le téléphone peuvent joindre ? — à vérifier par test : `start` appelé via l'ingress réel.
- Une `OperationalError` persistante sous verrou SQLite sur `start`, `approve` ou `complete` remonte-t-elle bien en 500 brut, sans corps exploitable par la TV ? — à vérifier par test (modèle : `tests/test_tv_auth.py:773-848`, avec un verrou tenu au-delà des retries).
- Une TV dont la réponse de sondage est perdue après le marquage de livraison n'a aucun moyen de récupérer le payload : est-ce le comportement voulu ou faut-il une re-livraison bornée ? — décision produit, code muet.
- Le mobile qui approuve envoie-t-il d'autres clés que `plexToken`, `backendUrl` et `apiKey` (comptes Xtream par exemple) ? Le backend les stocke et les livre, la TV les ignore. — à vérifier par test, une fois le client d'approbation identifié.
