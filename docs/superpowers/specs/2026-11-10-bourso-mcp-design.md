# bourso-mcp : serveur MCP de lecture des comptes BoursoBank

- Date : 2026-11-10
- Statut : design validé en brainstorming, spec en revue
- Périmètre : v1 (exécution locale). Le mode NAS fera l'objet d'une spec séparée.

## 1. Objectif

Un serveur MCP personnel, en **lecture seule**, qui permet à Claude (Desktop ou Code) :

1. de lire les transactions des comptes BoursoBank (ex-Boursorama Banque) avec des filtres ;
2. de lire les extraits (relevés) de compte mensuels au format PDF.

Les données sont récupérées **automatiquement** depuis l'espace client BoursoBank. La seule intervention humaine est la validation de l'authentification forte (2FA), demandée par la banque tous les quelques mois.

### Usages visés (sans priorité entre eux)

- **Analyse conversationnelle** : « combien en restaurants en octobre ? », « quels sont mes prélèvements récurrents ? ».
- **Compta / rapprochement** : récupérer les transactions et relevés d'une période, exporter en CSV pour l'expert-comptable.
- **Recherche ponctuelle** : retrouver une opération par montant, libellé ou date.

### Critères de réussite

- Une question d'analyse sur une période donnée obtient une réponse chiffrée juste, sans que Claude additionne lui-même des lignes.
- Une synchro quotidienne planifiée tient les données à jour sans action de l'utilisateur, hors renouvellement de la 2FA.
- Une panne de la source (site modifié, woob cassé) n'empêche pas d'interroger l'historique déjà synchronisé.
- Aucun identifiant BoursoBank ne transite par Claude.

### Comptes couverts

- Compte courant personnel, avec ses cartes (débit immédiat et débit différé).
- Compte joint.

Hors périmètre : épargne (livrets), bourse (PEA, CTO), assurance-vie.

## 2. Contraintes et faits établis

Recherche effectuée le 2026-11-10.

| Sujet | Fait | Source |
|---|---|---|
| API publique | BoursoBank n'a pas d'API publique pour les particuliers. | — |
| woob, module `boursorama` | Maintenu (commits jusqu'au 2026-10-31), cible `https://clients.boursobank.com`. Capacités : comptes, historique, opérations à venir, liste et téléchargement des documents (relevés), profil. | gitlab.com/woob/woob, `modules/boursorama` |
| woob, authentification | Clavier virtuel géré. 2FA par code SMS, code e-mail ou validation dans l'appli (attente jusqu'à 600 s). Le QR code n'est pas géré. L'état 2FA est conservé 90 jours. En mode non interactif, woob lève `NeedInteractiveFor2FA`. | idem |
| woob, fiabilité | Connexion cassée du 2026-04-30 au 2026-08-06 (issue #840, corrigée par la MR !1091). Les cartes à débit différé sont en cours de correction (MR !1033 encore ouverte le 2026-11-09). | idem |
| woob, distribution | Dernière version PyPI : 3.7 (2024-10-29). Les correctifs du module ne sont disponibles que sur git `master`. Python ≥ 3.11. Licence LGPL-3.0-or-later. | idem |
| BoursoBank, relevés | Disponibles en ligne 5 ans. Le relevé mensuel paraît au 7ᵉ jour ouvré du mois suivant, et seulement si le mois contient au moins une opération. | Aide BoursoBank |
| BoursoBank, authentification forte | Redemandée périodiquement : tous les 180 jours selon l'aide en ligne, tous les 90 jours ouvrés selon les conditions générales du 15/04/2026. Elle est aussi exigée pour consulter les opérations de plus de 90 jours. | Aide BoursoBank, CG BoursoBank |
| Open banking (DSP2) | GoCardless Bank Account Data n'accepte plus de nouveaux comptes depuis juillet 2025 (confiance moyenne). Enable Banking reste accessible gratuitement en usage personnel, avec environ 90 jours d'historique et un consentement d'environ 90 jours. Aucun agrégateur DSP2 ne fournit les relevés PDF. | enablebanking.com/docs/api/linked-accounts |
| CGU du site | « tout moyen de récupération automatique » des *Informations* du site est interdit (§2.1), de même que la surcharge du site (§8). L'application de cette clause aux données personnelles de l'espace client n'est pas claire : le risque réaliste est un blocage du compte. **Risque accepté par l'utilisateur.** | boursorama.com/aide/mentions-legales-et-cgu |

## 3. Décision d'architecture

**Retenu : tout en Python, avec woob comme librairie, et une architecture « stockage d'abord ».**

- Une **synchro** alimente une base SQLite et un dossier de PDF.
- Les **outils de lecture MCP n'interrogent que ce stockage local**.

| Alternative écartée | Raison |
|---|---|
| Hybride Enable Banking (transactions) + woob (relevés) | Deux authentifications à maintenir et un historique DSP2 limité à environ 90 jours. woob reste de toute façon indispensable pour les PDF. Reste possible plus tard grâce à l'interface `BankSource`. |
| Serveur MCP Java/Spring AI + worker Python woob | Deux runtimes à installer et à faire tourner ensemble, pour le seul bénéfice de rester dans la stack Java. |
| Tout Java, scraper maison (Playwright) | Réécrire et maintenir seul ce que woob fait déjà (clavier virtuel, 2FA, pages), avec la plus faible résistance aux changements du site. |
| Réutiliser `boursocli` (Go) | S'appuie sur les cookies de Chrome et ne sait pas se reconnecter seul : ne répond pas à l'exigence d'automatisation. |

## 4. Architecture

### 4.1 Modules

Un seul paquet Python (Python 3.12, géré avec `uv`) :

```
bourso-mcp/
  pyproject.toml
  README.md
  src/bourso_mcp/
    config.py   Chemins (BOURSO_MCP_HOME), réglages, SecretProvider
    store/      Base SQLite + dossier PDF. Ne dépend ni de woob ni de MCP.
    source/     Interface BankSource, WoobBoursoSource, modèle de domaine
    sync/       SyncService : orchestration, verrou, journal des passages
    server/     Serveur MCP : outils de lecture, de synchro et d'authentification
    cli/        bourso-mcp serve | sync | login | status
  tests/
  docs/superpowers/specs/
```

Règle de dépendance :

- `store` et `source` ne se connaissent pas.
- `sync` dépend des deux.
- `server` et `cli` dépendent de `sync` et `store`.
- Seul `source/woob_*` importe woob.

### 4.2 Flux

```
BoursoBank ──> woob ──> WoobBoursoSource ──> SyncService ──> SQLite + PDF ──> outils MCP ──> Claude
                                                  ^
                              bourso-mcp sync (tâche planifiée) / sync_now (outil MCP)
```

### 4.3 Interface `BankSource`

Contrat indépendant de woob, à partir duquel on écrit aussi un faux en mémoire pour les tests :

- `login()` : ouvre la session. Lève `TwoFactorRequired(method)` si la banque demande la 2FA (`method` ∈ `sms`, `email`, `app`).
- `submit_2fa(code: str | None)` : transmet le code (SMS ou e-mail), ou `None` pour attendre la validation dans l'appli.
- `list_accounts() -> list[Account]`
- `iter_transactions(account_id, since: date | None) -> Iterator[Transaction]` : opérations comptabilisées.
- `iter_pending(account_id) -> Iterator[Transaction]` : opérations à venir.
- `list_statements(account_id) -> list[StatementRef]`
- `download_statement(ref) -> bytes`

Erreurs levées par la source : `TwoFactorRequired`, `AuthFailed`, `AuthMethodUnsupported`, `SourceBroken`, `NetworkError`.

### 4.4 `WoobBoursoSource`

- Enveloppe le backend woob `boursorama`, utilisé comme librairie Python et non en ligne de commande.
- **Cœur woob et module `boursorama` épinglés sur un même commit** du dépôt `gitlab.com/woob/woob`. Il doit être postérieur au 2026-08-06, date de la correction de la connexion. Mettre à jour woob revient à changer ce commit.
- Elle n'appelle **que** des méthodes de lecture : comptes, historique, opérations à venir, documents, téléchargement de document. Les capacités de virement et d'ajout de bénéficiaire ne sont jamais appelées.
- Elle traduit les objets woob (`Account`, `Transaction`, `Document`) vers le modèle de domaine, et les exceptions woob vers les erreurs de la section 4.3.
- L'état de session woob (cookies, état 2FA) est conservé dans `BOURSO_MCP_HOME/woob/`.

## 5. Modèle de données

Base `BOURSO_MCP_HOME/bourso.db`. `BOURSO_MCP_HOME` vaut `%LOCALAPPDATA%\bourso-mcp\` par défaut.

### `accounts`

| Colonne | Type | Note |
|---|---|---|
| `id` | TEXT PK | identifiant woob du compte |
| `label` | TEXT | |
| `type` | TEXT | `checking` ou `card` |
| `ownership` | TEXT | `owner`, `co_owner` ou `attorney` (le compte joint en est un cas) |
| `parent_id` | TEXT NULL | compte courant de rattachement d'une carte à débit différé |
| `iban` | TEXT NULL | |
| `currency` | TEXT | `EUR` |
| `balance_cents` | INTEGER NULL | dernier solde connu |
| `balance_at` | TEXT NULL | horodatage ISO 8601 |

### `transactions`

| Colonne | Type | Note |
|---|---|---|
| `id` | INTEGER PK | |
| `account_id` | TEXT FK | |
| `fingerprint` | TEXT | unique par compte (voir 6.1) |
| `operation_date` | TEXT | date de l'opération (`rdate` woob), ISO |
| `debit_date` | TEXT | date de débit (`date` woob), ISO |
| `value_date` | TEXT NULL | date de valeur |
| `raw_label` | TEXT | libellé brut |
| `label` | TEXT | libellé nettoyé par woob |
| `search_text` | TEXT | `raw_label` + `label`, en minuscules et sans accents |
| `amount_cents` | INTEGER | signé : négatif pour un débit |
| `type` | TEXT | `card`, `card_summary` (débit mensuel global d'une carte à débit différé sur le compte courant), `transfer`, `order` (prélèvement), `check`, `deposit`, `withdrawal`, `bank_fee`, `other` |
| `category` | TEXT NULL | catégorie BoursoBank si woob la fournit |
| `card` | TEXT NULL | numéro de carte masqué |
| `pending` | INTEGER | 1 pour une opération à venir |
| `synced_at` | TEXT | |

Index : `(account_id, operation_date)`, `(account_id, fingerprint)` unique, `(amount_cents)`.

### `statements`

| Colonne | Type | Note |
|---|---|---|
| `id` | TEXT PK | identifiant woob du document |
| `account_id` | TEXT FK | |
| `period` | TEXT | `AAAA-MM` |
| `date` | TEXT | date du document |
| `doc_type` | TEXT | `statement`, ou `other` pour les documents de frais par exemple |
| `file_path` | TEXT | relatif à `BOURSO_MCP_HOME` |
| `sha256` | TEXT | |
| `downloaded_at` | TEXT | |

Les PDF sont rangés sous `BOURSO_MCP_HOME/statements/<account_id>/<AAAA-MM>.pdf`. Si plusieurs documents portent sur la même période, un suffixe `-2`, `-3`, etc. est ajouté.

### `sync_runs`

`id`, `started_at`, `finished_at`, `trigger` (`mcp` pour un `sync_now`, `cli` pour une commande lancée à la main, `schedule` pour une commande lancée avec `--non-interactive`), `scope`, `status` (`running`, `awaiting_2fa`, `ok`, `partial`, `auth_required`, `auth_failed`, `error`), `twofa_method` (méthode 2FA attendue, sinon NULL), `added_transactions`, `added_statements`, `error_type`, `error_message`.

## 6. Synchro

### 6.1 Règles

- **Empreinte** : `sha256(account_id | operation_date | amount_cents | raw_label | rang)`. Le rang (0, 1, 2…) distingue les opérations identiques du même jour sur le même compte, dans l'ordre où la source les renvoie.
- **Opérations comptabilisées** :
  - la synchro repart de `max(debit_date) − 10 jours` pour le compte (recouvrement), puis insère ou met à jour par empreinte. On utilise la date de débit parce que c'est celle qui ordonne l'historique woob ;
  - premier passage : `since = None`, on récupère tout ce que la source fournit.
- **Opérations à venir** : à chaque synchro, toutes les lignes `pending = 1` du compte sont supprimées puis rechargées. Une opération qui passe de « à venir » à « comptabilisée » ne crée pas de doublon.
- **Relevés** : à chaque synchro, on liste les documents de chaque compte et on ne télécharge que ceux dont l'`id` est absent de `statements`, ou dont le fichier a disparu du disque. Premier passage : tout ce qui est disponible, soit environ 5 ans.
- **Isolation par compte** : un échec sur un compte n'arrête pas les autres. Le passage est alors marqué `partial`, avec le détail dans `error_message`.
- **Soldes** : mis à jour à chaque passage réussi d'un compte.

### 6.2 Exécution

- **Verrou** : un fichier verrou dans `BOURSO_MCP_HOME` empêche deux synchros simultanées, par exemple la tâche planifiée et un `sync_now`. Une demande de synchro pendant qu'une autre tourne renvoie l'état de celle en cours.
- **Passages interrompus** : si le processus s'arrête pendant une synchro (fermeture de Claude Desktop par exemple), le passage resté `running` ou `awaiting_2fa` est marqué `error` (« interrompu ») à la prochaine prise du verrou.
- **Limitation de fréquence** : une synchro est refusée si la dernière synchro réussie date de moins d'une heure, sauf avec `force=true`. Une synchro **réussie** est une synchro au statut `ok` ou `partial`. C'est la même définition que pour `last_success_at`.
- **Dans le serveur MCP** : la synchro tourne dans un thread de fond. `sync_now` rend la main immédiatement.
- **En ligne de commande** : `bourso-mcp sync` tourne au premier plan. Si la 2FA est demandée en mode planifié (non interactif), le passage se termine en `auth_required` sans attendre.

### 6.3 Authentification forte

Machine à états d'un passage : `running` → `awaiting_2fa(method)` → `running` → `ok`, `partial` ou `error`.

- `sms` / `email` : le passage attend un code transmis par `submit_2fa_code` (outil MCP) ou saisi dans le terminal (`bourso-mcp login` ou `bourso-mcp sync` en interactif). Le passage abandonne au bout de 10 minutes sans code (`auth_required`).
- `app` : le passage attend la validation dans l'appli BoursoBank, jusqu'à 10 minutes.
- QR code : `AuthMethodUnsupported`, statut `error`, avec un message qui invite à choisir une autre méthode côté BoursoBank.

## 7. Outils MCP

SDK officiel `mcp` (v2.x), transport stdio en v1. Les réponses sont en JSON. Les montants sont rendus en **chaînes décimales en euros** (`"-12.50"`), calculées à partir des centimes, pour éviter les erreurs d'arrondi des nombres à virgule flottante.

Chaque réponse des outils de lecture contient un bloc `sync` :

```json
"sync": { "last_success_at": "2026-11-10T07:00:12+01:00", "last_status": "ok", "auth_required": false }
```

### 7.1 Filtres communs

Tous optionnels, combinés en ET :

| Filtre | Type | Défaut |
|---|---|---|
| `account_ids` | liste de chaînes | tous les comptes |
| `date_from`, `date_to` | date ISO, bornes incluses | — |
| `date_field` | `operation` ou `debit` | `operation` |
| `amount_min`, `amount_max` | nombre positif en euros, comparé à la **valeur absolue** du montant (le sens se filtre avec `direction`) | — |
| `direction` | `debit` ou `credit` | les deux |
| `text` | chaîne, recherchée dans `search_text` (sans casse ni accents) | — |
| `types` | liste de types (section 5) | tous **sauf `card_summary`**, pour ne pas compter deux fois les achats d'une carte à débit différé, déjà présents un par un sur le compte carte. On peut l'inclure en le demandant explicitement. |
| `category` | chaîne exacte | — |
| `card` | chaîne (fin du numéro masqué) | — |
| `include_pending` | booléen | `true` |

### 7.2 Outils de lecture

Ils n'interrogent que le store local.

| Outil | Paramètres | Réponse |
|---|---|---|
| `list_accounts` | — | comptes avec type, titularité, parent, dernier solde et sa date |
| `search_transactions` | filtres + `sort` (`date_desc` par défaut, `date_asc`, `amount_asc`, `amount_desc`), `limit` (100 par défaut, 500 au maximum), `offset` | `items`, `total_count`, `total_debits`, `total_credits`, `net` (sur **tout** le filtre, pas seulement la page) |
| `summarize_transactions` | filtres + `group_by` (`month`, `category`, `label`, `type`, `account`) + `limit` (50 groupes par défaut) | groupes avec `count`, `total_debits`, `total_credits` et `net`, triés par valeur absolue de `net` décroissante |
| `export_transactions` | filtres | écrit `BOURSO_MCP_HOME/exports/transactions-<horodatage>.csv` (séparateur `;`, UTF-8 avec BOM, montants à virgule décimale, pour Excel en français) et renvoie le chemin et le nombre de lignes |
| `list_statements` | `account_ids`, `period_from`, `period_to` (`AAAA-MM`) | relevés avec id, compte, période, date, type et chemin |
| `read_statement` | `statement_id` | texte extrait du PDF (`pypdf`), période, compte, chemin local du fichier |

### 7.3 Outils de synchro et d'authentification

Les seuls qui contactent BoursoBank.

| Outil | Paramètres | Réponse |
|---|---|---|
| `sync_now` | `scope` (`all` par défaut, `transactions`, `statements`), `force` (`false` par défaut) | `started: true`, ou refus motivé (synchro déjà en cours, ou dernière synchro réussie il y a moins d'une heure). L'id et l'avancement du passage se lisent avec `sync_status`. |
| `sync_status` | — | passage en cours ou dernier passage (statut, étape, méthode 2FA attendue, compteurs, erreur), date du dernier succès |
| `submit_2fa_code` | `code` | `accepted: true`, ou `accepted: false` avec la raison (aucun passage n'attend de code) |

## 8. Secrets et sécurité

- **Identifiants BoursoBank** :
  - saisis une seule fois avec `bourso-mcp login` (saisie masquée dans le terminal) ;
  - stockés dans le Gestionnaire d'identification Windows via `keyring`, derrière une interface `SecretProvider` ;
  - jamais exposés par un outil MCP ni présents dans une réponse.
- **Codes 2FA** : usage unique et durée de vie courte. Ils peuvent passer par Claude ou par le terminal.
- **État de session woob** : dans `BOURSO_MCP_HOME/woob/`, jamais journalisé.
- **Lecture seule** : garantie par `WoobBoursoSource` (section 4.4) et vérifiée par un test.
- **Données au repos** : non chiffrées, par choix de l'utilisateur. Leur protection repose sur la session Windows et le chiffrement du disque.
- **Journaux** :
  - au niveau INFO, pas de montant, de libellé ni d'IBAN ; seulement des compteurs et des statuts ;
  - jamais d'identifiants ni de codes, quel que soit le niveau.
- **Charge sur le site** : la limitation de fréquence (section 6.2) et une synchro planifiée par jour suffisent.
- **Libellés = données non fiables** : les libellés (virements reçus par exemple) peuvent contenir du texte qui s'adresse à un LLM. Ils sont rendus comme champs JSON. Les outils de lecture n'ont aucun effet de bord. Seuls les outils de synchro agissent, et ils sont limités par le verrou et la fréquence.

## 9. Gestion des erreurs

| Erreur | Statut du passage | Comportement |
|---|---|---|
| `TwoFactorRequired` | `awaiting_2fa`, puis `auth_required` si personne ne répond | voir section 6.3 |
| `AuthFailed` | `auth_failed` | **aucun nouvel essai automatique**, pour ne pas faire bloquer le compte ; message : relancer `bourso-mcp login` |
| `AuthMethodUnsupported` | `error` | message explicatif |
| `SourceBroken` | `error` (ou `partial` si un seul compte est touché) | message : le site a probablement changé, mettre à jour le commit woob épinglé |
| `NetworkError` | après 3 tentatives (attente de 2 s, 8 s puis 30 s) : `error` | |
| Erreur inattendue | `error` | type et message enregistrés, sans données bancaires |

Les outils de lecture fonctionnent quel que soit l'état de la source.

## 10. Ligne de commande

| Commande | Rôle |
|---|---|
| `bourso-mcp serve` | démarre le serveur MCP (stdio) |
| `bourso-mcp login` | saisie des identifiants et ouverture de session interactive avec 2FA |
| `bourso-mcp sync [--scope all\|transactions\|statements] [--force] [--non-interactive] [--dry-run]` | synchro. `--non-interactive` pour la tâche planifiée. `--dry-run` récupère les données et affiche les volumes par compte sans rien écrire. |
| `bourso-mcp status` | état du dernier passage et date du dernier succès |

## 11. Tests

pytest et ruff. La suite automatique ne fait aucun appel réseau.

- **store** :
  - chaque filtre de la section 7.1, la pagination et les totaux calculés sur le filtre complet ;
  - `summarize` pour chaque `group_by` ;
  - l'export CSV : format, BOM et virgule décimale.
- **sync**, contre un `FakeBankSource` en mémoire :
  - premier passage puis passage incrémental avec recouvrement ;
  - deux opérations identiques le même jour ;
  - une opération à venir qui devient comptabilisée ;
  - les relevés déjà téléchargés ;
  - la 2FA par SMS et par l'appli, l'expiration du délai de 2FA ;
  - `AuthFailed` sans nouvel essai, `SourceBroken`, l'échec d'un seul compte (`partial`) ;
  - le verrou et la limitation de fréquence.
- **server** : outils appelés via le client MCP en mémoire du SDK, sur un store préchargé. On vérifie le schéma des réponses, les montants en chaînes et le bloc `sync`.
- **WoobBoursoSource** :
  - conversion d'objets woob construits à la main vers le domaine ;
  - traduction des exceptions ;
  - vérification de la lecture seule : aucune méthode de virement ou d'ajout de bénéficiaire n'est appelée.
- **Contre le vrai BoursoBank** : aucun test automatique. Une procédure manuelle, décrite dans le README, s'appuie sur `bourso-mcp sync --dry-run` et sert à chaque montée de version de woob.

## 12. Plan de livraison

0. **Validation de faisabilité** (avant tout le reste) : installer woob au commit épinglé, charger le backend `boursorama` comme librairie, se connecter avec la 2FA, lister les comptes et un relevé. **L'utilisateur exécute cette étape lui-même**, puisqu'elle nécessite ses identifiants. Elle confirme aussi la méthode de chargement du module woob épinglé, ainsi que la remontée du compte joint et des cartes à débit différé. Si elle échoue, on revoit la décision d'architecture avant d'écrire le reste.
1. Les étapes suivantes seront détaillées dans le plan d'implémentation : store, sync, outils, ligne de commande, README.

Livrables de la v1 :

- le dépôt `D:\work\repos\bourso-mcp` ;
- un `README.md` en français, **point d'entrée unique du projet**. Il contient :
  - l'objectif ;
  - toute l'architecture : modules, règle de dépendance, flux, modèle de données, outils MCP et leurs filtres ;
  - les outils à installer avant de commencer, avec leurs versions ;
  - toutes les commandes à lancer : installation, tests, login, synchro, statut ;
  - le lancement du serveur MCP et sa configuration dans Claude Desktop/Code ;
  - la tâche planifiée Windows ;
  - la procédure de vérification manuelle.

  Le README est mis à jour **dans chaque tâche** qui modifie l'architecture, les prérequis, les commandes ou le lancement, et non en fin de projet ;
- la configuration MCP : `uv --directory D:\work\repos\bourso-mcp run bourso-mcp serve` ;
- une tâche quotidienne du Planificateur de tâches Windows : `bourso-mcp sync --non-interactive`.

## 13. Hors périmètre v1

- Mode NAS : transport Streamable HTTP avec jeton obligatoire (le serveur refusera de démarrer sans), Dockerfile, secrets Docker, synchro planifiée en conteneur. Spec séparée.
- Source Enable Banking (DSP2).
- Épargne, bourse, assurance-vie.
- Catégorisation maison, analyse ligne à ligne des PDF, autres formats d'export que CSV.
- Ressources et prompts MCP.
- Toute opération en écriture vers la banque.

## 14. Risques

| Risque | Impact | Parade |
|---|---|---|
| woob cassé par un changement du site (déjà arrivé : environ 3 mois en 2026) | données plus fraîches | architecture « stockage d'abord », statut `error` explicite, montée du commit épinglé dès qu'un correctif sort |
| Cartes à débit différé mal remontées par woob | opérations de carte manquantes | validation à l'étape 0, synchro `partial` visible, suivi de la MR !1033 |
| Blocage du compte (CGU, trop d'échecs de connexion) | perte d'accès en ligne | aucun nouvel essai sur `AuthFailed`, limitation de fréquence, une synchro par jour |
| 2FA en QR code imposée par la banque | synchro impossible | `AuthMethodUnsupported` explicite, choix d'une autre méthode côté BoursoBank |
| Fuite des données locales | confidentialité | données dans le profil Windows de l'utilisateur, aucun secret dans les journaux, identifiants dans le Gestionnaire d'identification Windows |
