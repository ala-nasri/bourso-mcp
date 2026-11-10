# bourso-mcp

Serveur MCP personnel, en **lecture seule**, qui donne à Claude (Desktop ou Code) accès aux comptes BoursoBank : transactions et extraits (relevés) mensuels en PDF.

Ce README est le point d'entrée unique du projet : objectif, architecture, prérequis, commandes. Il est mis à jour dans chaque tâche qui change l'architecture, les prérequis, les commandes ou le lancement.

## Objectif

Claude peut, grâce à ce serveur :

1. lire les transactions des comptes BoursoBank (ex-Boursorama Banque), avec des filtres ;
2. lire les extraits de compte mensuels au format PDF.

Les données sont récupérées **automatiquement** depuis l'espace client BoursoBank, avec la bibliothèque [woob](https://gitlab.com/woob/woob). La seule intervention humaine est la validation de l'authentification forte (2FA), que la banque redemande tous les quelques mois.

**Usages visés**, sans priorité entre eux :

| Usage | Exemple |
|---|---|
| Analyse conversationnelle | « combien en restaurants en octobre ? », « quels sont mes prélèvements récurrents ? » |
| Compta et rapprochement | transactions et relevés d'une période, export CSV pour l'expert-comptable |
| Recherche ponctuelle | retrouver une opération par montant, libellé ou date |

**Comptes couverts** : le compte courant personnel avec ses cartes (débit immédiat et débit différé), et le compte joint. Hors périmètre : épargne (livrets), bourse (PEA, CTO) et assurance-vie.

**Garanties visées** :

- une question d'analyse sur une période obtient une réponse chiffrée juste, sans que Claude additionne lui-même des lignes (les totaux sont calculés par le serveur) ;
- une synchro quotidienne planifiée tient les données à jour, hors renouvellement de la 2FA ;
- une panne de la source (site modifié, woob cassé) n'empêche pas d'interroger l'historique déjà synchronisé ;
- aucun identifiant BoursoBank ne transite par Claude, et l'application ne les demande ni ne les stocke : ils sont lus, sans jamais être écrits, dans le Gestionnaire d'identification Windows, où l'utilisateur les a mis lui-même.

## Architecture

Principe : **« stockage d'abord »**. Une synchro alimente une base SQLite et un dossier de PDF ; les outils de lecture MCP n'interrogent que ce stockage local. Cette section décrit l'architecture cible ; les modules sont ajoutés tâche après tâche.

### Modules et règle de dépendance

Un seul paquet Python (3.12, géré avec `uv`), dans `src/bourso_mcp/` :

| Module | Rôle |
|---|---|
| `config.py` | variables d'environnement non secrètes, chemins (`BOURSO_MCP_HOME`) |
| `credentials.py` | lecture seule de l'entrée `bourso-mcp` du Gestionnaire d'identification Windows |
| `store/` | base SQLite et dossier PDF ; ne dépend ni de woob ni de MCP |
| `source/` | interface `BankSource`, `WoobBoursoSource`, modèle de domaine |
| `sync/` | `SyncService` : orchestration, verrou, journal des passages |
| `server/` | serveur MCP : outils de lecture, de synchro et d'authentification |
| `cli/` | commande `bourso-mcp` : `serve`, `sync`, `login`, `status` |

Règle de dépendance :

- `store` et `source` ne se connaissent pas ;
- `sync` dépend des deux ;
- `server` et `cli` dépendent de `sync` et de `store` ;
- seul `source/woob_*` importe woob.

Le dossier de données `BOURSO_MCP_HOME` vaut `%LOCALAPPDATA%\bourso-mcp\` par défaut. Il contient `bourso.db`, `statements/`, `exports/`, `woob/` (état de session woob, chiffré avec DPAPI) et `sync.lock`.

### Flux

```
BoursoBank ──> woob ──> WoobBoursoSource ──> SyncService ──> SQLite + PDF ──> outils MCP ──> Claude
                                                  ^
                              bourso-mcp sync (tâche planifiée) / sync_now (outil MCP)
```

### Interface `BankSource`

Contrat indépendant de woob. Un faux en mémoire (`FakeBankSource`) l'implémente pour les tests.

| Méthode | Rôle |
|---|---|
| `login()` | ouvre la session ; lève `TwoFactorRequired(method)` si la banque demande la 2FA (`sms`, `email` ou `app`) |
| `submit_2fa(code)` | transmet le code SMS ou e-mail, ou `None` pour attendre la validation dans l'appli |
| `list_accounts()` | liste des comptes |
| `iter_transactions(account_id, since)` | opérations comptabilisées, depuis `since` (`None` : tout) |
| `iter_pending(account_id)` | opérations à venir |
| `list_statements(account_id)` | références des relevés disponibles |
| `download_statement(ref)` | contenu d'un relevé, en octets |

Erreurs levées par la source : `TwoFactorRequired`, `AuthFailed`, `AuthMethodUnsupported`, `SourceBroken`, `NetworkError`.

`WoobBoursoSource` enveloppe le backend woob `boursorama`, utilisé comme bibliothèque. Elle n'appelle que des méthodes de lecture (comptes, historique, opérations à venir, documents) : les virements et l'ajout de bénéficiaire ne sont jamais appelés.

### Modèle de données

Base `BOURSO_MCP_HOME/bourso.db`. Les montants sont stockés en **centimes entiers**.

**`accounts`**

| Colonne | Contenu |
|---|---|
| `id` | identifiant woob du compte (clé primaire) |
| `label` | libellé |
| `type` | `checking` ou `card` |
| `ownership` | `owner`, `co_owner` ou `attorney` (le compte joint est un cas de `co_owner`) |
| `parent_id` | compte courant de rattachement d'une carte à débit différé |
| `iban` | IBAN, si connu |
| `currency` | `EUR` |
| `balance_cents`, `balance_at` | dernier solde connu et son horodatage ISO 8601 |

**`transactions`**

| Colonne | Contenu |
|---|---|
| `id`, `account_id` | clé primaire et compte |
| `fingerprint` | empreinte, unique par compte (voir règles de synchro) |
| `operation_date` | date de l'opération (`rdate` woob) |
| `debit_date` | date de débit (`date` woob) |
| `value_date` | date de valeur, si connue |
| `raw_label`, `label` | libellé brut et libellé nettoyé par woob |
| `search_text` | `raw_label` et `label`, en minuscules et sans accents |
| `amount_cents` | montant signé : négatif pour un débit |
| `type` | `card`, `card_summary`, `transfer`, `order` (prélèvement), `check`, `deposit`, `withdrawal`, `bank_fee`, `other` |
| `category` | catégorie BoursoBank, si woob la fournit |
| `card` | numéro de carte masqué |
| `pending` | 1 pour une opération à venir |
| `synced_at` | horodatage de la synchro |

`card_summary` est le débit mensuel global d'une carte à débit différé, vu sur le compte courant. Index : `(account_id, operation_date)`, `(account_id, fingerprint)` unique, `(amount_cents)`.

**`statements`**

| Colonne | Contenu |
|---|---|
| `id`, `account_id` | identifiant woob du document et compte |
| `period` | `AAAA-MM` |
| `date` | date du document |
| `doc_type` | `statement`, ou `other` (documents de frais par exemple) |
| `file_path` | chemin relatif à `BOURSO_MCP_HOME` |
| `sha256`, `downloaded_at` | somme de contrôle et date de téléchargement |

Les PDF sont rangés sous `BOURSO_MCP_HOME/statements/<account_id>/<AAAA-MM>.pdf`, avec un suffixe `-2`, `-3`, etc. si plusieurs documents portent sur la même période.

**`sync_runs`** : journal des passages de synchro.

| Colonne | Contenu |
|---|---|
| `id`, `started_at`, `finished_at` | identité et durée |
| `trigger` | `mcp` (`sync_now`), `cli` (commande lancée à la main) ou `schedule` (`--non-interactive`) |
| `scope` | périmètre demandé |
| `status` | `running`, `awaiting_2fa`, `ok`, `partial`, `auth_required`, `auth_failed`, `error` |
| `twofa_method` | méthode 2FA attendue, sinon `NULL` |
| `added_transactions`, `added_statements` | compteurs |
| `error_type`, `error_message` | erreur éventuelle, sans donnée bancaire |

### Règles de synchro

| Sujet | Règle |
|---|---|
| Empreinte | `sha256(account_id \| operation_date \| amount_cents \| raw_label \| rang)` ; le rang (0, 1, 2…) distingue les opérations identiques du même jour sur le même compte, dans l'ordre de la source |
| Opérations comptabilisées | reprise à `max(debit_date) − 10 jours` pour le compte (recouvrement de 10 jours), puis insertion ou mise à jour par empreinte ; premier passage : tout ce que la source fournit |
| Opérations à venir | à chaque synchro, les lignes `pending = 1` du compte sont supprimées puis rechargées : pas de doublon quand une opération devient comptabilisée |
| Relevés | on ne télécharge que les documents dont l'`id` est absent de `statements` ou dont le fichier a disparu ; premier passage : tout (environ 5 ans) |
| Isolation par compte | l'échec d'un compte n'arrête pas les autres ; le passage est alors `partial` |
| Soldes | mis à jour à chaque passage réussi d'un compte |
| Verrou | `sync.lock` empêche deux synchros simultanées ; une demande pendant une synchro en cours renvoie l'état de celle-ci |
| Passages interrompus | un passage resté `running` ou `awaiting_2fa` est marqué `error` (« interrompu ») à la prochaine prise du verrou |
| Limitation de fréquence | refus si la dernière synchro réussie (statut `ok` ou `partial`) date de moins d'une heure, sauf `force` |
| Nouvelles tentatives | après un `NetworkError` : 3 tentatives, avec des attentes de 2 s, 8 s puis 30 s |
| Échec d'authentification | `AuthFailed` : aucun nouvel essai automatique, pour ne pas faire bloquer le compte |

Authentification forte : un passage suit `running` → `awaiting_2fa(method)` → `running` → `ok`, `partial` ou `error`.

| Méthode | Comportement |
|---|---|
| `sms`, `email` | attente d'un code (outil `submit_2fa_code` ou terminal), abandon au bout de 600 s sans code (`auth_required`) |
| `app` | attente de la validation dans l'appli BoursoBank, jusqu'à 600 s |
| QR code | non géré : `AuthMethodUnsupported`, statut `error`, message qui invite à choisir une autre méthode côté BoursoBank |

En ligne de commande, `bourso-mcp sync` tourne au premier plan ; en mode planifié (`--non-interactive`), une 2FA demandée termine le passage en `auth_required` sans attendre. Dans le serveur MCP, la synchro tourne dans un thread de fond et `sync_now` rend la main immédiatement.

### Outils MCP et filtres

SDK officiel `mcp` (2.x), transport stdio. Les réponses sont en JSON. Les montants sont rendus en **chaînes décimales en euros avec un point** (`"-12.50"`), calculées à partir des centimes ; dans l'export CSV, la virgule décimale (`-12,50`). Chaque réponse d'un outil de lecture contient un bloc `sync` :

```json
"sync": { "last_success_at": "2026-11-10T07:00:12+01:00", "last_status": "ok", "auth_required": false }
```

**Filtres communs** : tous optionnels, combinés en ET.

| Filtre | Type | Défaut |
|---|---|---|
| `account_ids` | liste de chaînes | tous les comptes |
| `date_from`, `date_to` | date ISO, bornes incluses | aucune borne |
| `date_field` | `operation` ou `debit` | `operation` |
| `amount_min`, `amount_max` | nombre positif en euros, comparé à la valeur absolue du montant | aucune borne |
| `direction` | `debit` ou `credit` | les deux |
| `text` | recherche dans `search_text`, sans casse ni accents | aucun |
| `types` | liste de types de transaction | tous **sauf `card_summary`** (évite de compter deux fois les achats d'une carte à débit différé) ; il suffit de le demander explicitement pour l'inclure |
| `category` | chaîne exacte | aucune |
| `card` | fin du numéro masqué | aucune |
| `include_pending` | booléen | `true` |

**Outils de lecture** : ils n'interrogent que le store local et n'ont aucun effet de bord.

| Outil | Paramètres | Réponse |
|---|---|---|
| `list_accounts` | aucun | comptes avec type, titularité, parent, dernier solde et sa date |
| `search_transactions` | filtres, `sort` (`date_desc` par défaut, `date_asc`, `amount_asc`, `amount_desc`), `limit` (100 par défaut, 500 au maximum), `offset` | `items`, `total_count`, `total_debits`, `total_credits`, `net` : totaux sur tout le filtre, pas seulement sur la page |
| `summarize_transactions` | filtres, `group_by` (`month`, `category`, `label`, `type`, `account`), `limit` (50 groupes par défaut) | groupes avec `count`, `total_debits`, `total_credits` et `net`, triés par valeur absolue de `net` décroissante |
| `export_transactions` | filtres | écrit `BOURSO_MCP_HOME/exports/transactions-<horodatage>.csv` (séparateur `;`, UTF-8 avec BOM, virgule décimale, pour Excel en français) ; renvoie le chemin et le nombre de lignes |
| `list_statements` | `account_ids`, `period_from`, `period_to` (`AAAA-MM`) | relevés avec id, compte, période, date, type et chemin |
| `read_statement` | `statement_id` | texte extrait du PDF (`pypdf`), période, compte, chemin local du fichier |

**Outils de synchro et d'authentification** : les seuls qui contactent BoursoBank.

| Outil | Paramètres | Réponse |
|---|---|---|
| `sync_now` | `scope` (`all` par défaut, `transactions`, `statements`), `force` (`false` par défaut) | `started: true`, ou refus motivé (synchro déjà en cours, ou dernière synchro réussie il y a moins d'une heure) |
| `sync_status` | aucun | passage en cours ou dernier passage (statut, étape, méthode 2FA attendue, compteurs, erreur) et date du dernier succès |
| `submit_2fa_code` | `code` | `accepted: true`, ou `accepted: false` avec la raison (aucun passage n'attend de code) |

## Prérequis

- **Git**, avec la prise en charge des sous-modules.
- **uv ≥ 0.13** (testé avec 0.13.7) : `winget install --id=astral-sh.uv -e`.
- **Python 3.12** : `uv python install 3.12`.
- **Claude Desktop** ou **Claude Code**.
- Un **compte BoursoBank** dont la 2FA se fait par SMS, e-mail ou appli. Le QR code n'est pas géré.
- L'**entrée d'identifiants `bourso-mcp`** dans le Gestionnaire d'identification Windows, créée par l'utilisateur avec la commande suivante, qui demande le mot de passe en saisie masquée :

  ```
  cmdkey /generic:bourso-mcp /user:<identifiant> /pass
  ```

  L'application ne fait que lire cette entrée : elle ne la crée, ne la modifie ni ne la supprime, et ne demande jamais d'identifiant. Le détail et les variables d'environnement facultatives seront décrits dans la section Configuration, ajoutée avec la lecture des identifiants.

## Installation

woob est un sous-module git (`vendor/woob`) : le clonage doit le récupérer.

```
git clone --recurse-submodules <url-du-dépôt> <chemin-du-dépôt>
cd <chemin-du-dépôt>
uv sync
```

Si le dépôt a été cloné sans `--recurse-submodules`, lancer `git submodule update --init` avant `uv sync`.

## Commandes

Toutes les commandes passent par `uv`, depuis `<chemin-du-dépôt>`.

| Commande | Rôle |
|---|---|
| `uv run pytest -v` | lance la suite de tests (aucun appel réseau) |
| `uv run ruff check .` | vérifie le style du code |

Les commandes du serveur (`serve`, `login`, `sync`, `status`) et le lancement dans Claude sont ajoutés à ce README avec les tâches qui les créent.

## Notes d'intégration woob

La dernière version de woob sur PyPI (3.7, octobre 2024) est plus ancienne que les correctifs du module `boursorama`, disponibles seulement sur la branche `master` du dépôt. Le cœur de woob et le module `boursorama` sont donc épinglés **sur un même commit**, dans le sous-module `vendor/woob`, installé en mode éditable par `uv`.

| | |
|---|---|
| Dépôt | `https://gitlab.com/woob/woob.git` |
| Commit épinglé | `70ac33dc5b2b399b498415e009121cbe9d426692` |
| Date du commit | 2026-11-09 |
| Inclut | le commit du 2026-10-31 sur `modules/boursorama` (« Try using the configured identity »), postérieur à la correction de la connexion du 2026-08-06 |

**Monter de version** :

1. `git -C vendor/woob fetch`
2. `git -C vendor/woob checkout <sha>` : le nouveau commit doit contenir celui du 2026-10-31 sur `modules/boursorama`
3. `uv sync`
4. `uv run pytest -v`
5. vérification manuelle contre BoursoBank (`bourso-mcp sync --dry-run`) : la procédure détaillée sera décrite dans la section Vérification manuelle
6. versionner le nouveau commit du sous-module, et reporter son SHA et sa date dans le tableau ci-dessus

### Résultats du spike woob (tâche 2)

Constats faits par `scripts/spike_woob.py` contre la vraie banque (connexion, liste des comptes et téléchargement d'un relevé réussis), puis lecture du code de woob. Ils pilotent `WoobBoursoSource` (tâche 10).

- **(a) Chargement des modules.** `WoobBase(modules_path=<vendor/woob/modules>)`, puis `load_backend("boursorama", "boursorama", params, storage=<stockage>)`. Le stockage se passe à `load_backend`, pas à `WoobBase`. Classe du module : `BoursoramaModule`. Les paramètres contiennent `login`, `password` et `request_information`.
- **(b) 2FA interactive.** Le paramètre `request_information` doit valoir une valeur non nulle (par exemple `{}`) dans la configuration **avant** la création du navigateur : `TwoFactorBrowser.is_interactive` est lu à la construction. Sinon, `NeedInteractiveFor2FA`. Exceptions levées par le module (lecture du code) : SMS, `SentOTPQuestion` avec le champ `code` ; e-mail, `SentOTPQuestion` avec le champ `email_code` ; appli, `AppValidation`. Reprise : `backend.config["code"]` ou `["email_code"]` reçoit `.set(<code>)`, ou `backend.config["resume"].set(True)` pour l'appli, puis on rappelle la méthode (champs de `AUTHENTICATION_METHODS` : `code`, `email_code`, `resume`). Un SMS peut être suivi d'un e-mail. Un code faux lève `BrowserIncorrectPassword`. L'attente de la validation dans l'appli est de 10 minutes au plus.
- **(c) Méthode 2FA observée.** Premier lancement sans état sauvegardé : aucune 2FA demandée, connexion directe. Le chemin 2FA n'a donc **pas** été observé en conditions réelles : il reste à vérifier à la tâche 13.
- **(d) Compte joint.** Il est visible, avec `ownership` = `co-owner` (`AccountOwnership.CO_OWNER`, soit `co_owner` côté domaine). Des comptes d'autres types (non `CHECKING`) sont aussi renvoyés ; `to_account` les ignore (seuls `CHECKING` et `CARD` sont retenus). Tous les comptes `CHECKING` sont synchronisés.
- **(e) Cartes et types de transaction.** Sur la fenêtre observée (30 jours, aucune opération à venir), aucun compte de type `CARD` avec `parent` n'est apparu et aucune ligne `TYPE_CARD_SUMMARY` non plus. Le spike ne distingue pas « pas de carte à débit différé » de « le module n'expose pas les cartes comme comptes » : la gestion des comptes `CARD` (`parent`, `card_summary`) n'est donc **pas vérifiée en conditions réelles**. Types de transaction observés : seulement `TYPE_CARD` et `TYPE_UNKNOWN`. Avec la correspondance de la tâche 10, les opérations hors carte tombent donc en `other` : **limite connue**, les virements et prélèvements ne sont pas distingués.
- **(f) Documents.** `iter_subscription()` renvoie une subscription par compte : `subscription.id` == `account.id` (aussi == `account.number` et `subscription._account_key` == `account._webid`). `Document.id` = `<subscription.id>_<suffixe>`. `Document.type` == `"statement"` (`DocumentTypes.STATEMENT`), `format` = `pdf`. Les documents arrivent par dates décroissantes. `date` tombe en fin de mois (dernier jour, ou quelques jours avant) ; le mois cité dans le libellé, quand il y en a un (un document sur vingt n'en a pas), est celui de `date`, donc `period` ne doit pas être tiré du libellé ; l'en-tête du PDF couvre les 30 jours environ qui finissent à `date`. **Règle de `period` : `period` = mois de `Document.date` (AAAA-MM).** `pypdf` extrait bien le texte du relevé.
- **(g) Champs des opérations.** `rdate`, `vdate`, `bdate` et `category` sont renseignés. `card` est vide, même sur les opérations carte.
- **(h) Ordre.** `iter_history` renvoie du plus récent au plus ancien.
- **(i) Dépendances.** Aucune dépendance supplémentaire à celles de woob et du projet.
- **(j) Stockage.** Interface `IStorage` : `load(what, name, default)`, `save(what, name)`, `get(what, name, *path, default=)`, `set(what, name, *path, value)`, `delete(what, name, *path)`. `BackendStorage` préfixe tout par `('backends', <nom>)`. Le stockage standard (`StandardStorage`) est un fichier YAML en clair. Appels observés pour un lancement (ordre non établi) : `load`, `get` et `set` de `browser_state`, `save`. Clés déclarées par le navigateur (`__states__`) : `recipient_form`, `transfer_form`, `form_state`, `user_hash`, `otp_number`, `otp_form_token`, `twofa_config`, `otp_headers`, `twofa_count` ; woob y ajoute `url`, `cookies` (et `twofa_logged_date` après une 2FA). `browser_state` est entièrement sérialisable en JSON : `cookies` et `url` sont des `str`, `twofa_count` un `int`, le reste `None`. L'état contient les cookies de session : il doit être chiffré (DPAPI).
- **(k) Identifiants.** `keyring.get_credential("bourso-mcp", None)` lit l'entrée créée avec `cmdkey` via `WinVaultKeyring` : identifiant et mot de passe sont lus. L'identifiant est composé de chiffres (format exigé par woob). Le risque MSIX de Claude Desktop (accès au Gestionnaire d'identification) reste à vérifier à la tâche 13.
