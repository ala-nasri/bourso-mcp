# bourso-mcp : plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** un serveur MCP local, en lecture seule, qui synchronise les transactions et les relevés PDF des comptes BoursoBank (compte courant, compte joint, cartes) dans un stockage local et les expose à Claude avec des filtres.

**Architecture :**
- La librairie woob (module `boursorama`, épinglée en sous-module git) est la seule source de données. Elle est enveloppée dans `WoobBoursoSource`, derrière l'interface `BankSource`.
- `SyncService` alimente une base SQLite et un dossier de PDF.
- Les outils de lecture MCP n'interrogent que ce stockage. Seuls les outils `sync_*` et `submit_2fa_code` contactent la banque, via une synchro en tâche de fond.

**Tech Stack :** Python 3.12, uv, woob (git, commit épinglé), SDK `mcp` 2.x (stdio), sqlite3 (bibliothèque standard), keyring, pypdf, filelock, pytest, ruff.

**Spec :** `docs/superpowers/specs/2026-11-10-bourso-mcp-design.md`. Lis-la avant chaque tâche : les sections citées (§) y renvoient.

## Global Constraints

- `requires-python = ">=3.12,<3.13"`. Toutes les commandes passent par `uv run …`.
- Cœur woob et module `boursorama` sur **le même commit**, dans le sous-module `vendor/woob`. Ce commit doit inclure celui du 2026-10-31 sur `modules/boursorama` (« Try using the configured identity »). Son SHA est inscrit dans le README.
- **Lecture seule** : aucun appel aux méthodes woob de virement ou de bénéficiaire (§4.4, §8).
- **Montants** :
  - stockés en **centimes entiers** ;
  - rendus par les outils MCP en chaînes décimales en euros, avec un point (`"-12.50"`) ;
  - en CSV, avec une virgule décimale (`-12,50`).
- **Dossier de données** : `BOURSO_MCP_HOME`, par défaut `%LOCALAPPDATA%\bourso-mcp\`. Il contient `bourso.db`, `statements/`, `exports/`, `woob/` et `sync.lock`.
- **Valeurs fixées par la spec** :
  - recouvrement de la synchro : 10 jours, calculé sur `debit_date` ;
  - limitation de fréquence : 1 h, sauf `force` ; une synchro est **réussie** si son statut est `ok` ou `partial` ;
  - délai d'attente de la 2FA : 600 s ;
  - nouvelles tentatives après `NetworkError` : 3, avec des attentes de 2 s, 8 s puis 30 s ;
  - `limit` de `search_transactions` : 100 par défaut, 500 au maximum ;
  - `limit` de `summarize_transactions` : 50 par défaut ;
  - CSV : séparateur `;`, encodage `utf-8-sig`.
- Le filtre `types` exclut `card_summary` par défaut (§7.1).
- **Journaux** : sur stderr uniquement, jamais sur stdout (c'est le canal stdio du MCP). Au niveau INFO, aucun montant, libellé ni IBAN ; à aucun niveau, d'identifiants ni de codes 2FA.
- **README** (règle générale de l'utilisateur) :
  - c'est le point d'entrée unique, en français : architecture complète, prérequis avec versions, toutes les commandes, lancement du MCP ;
  - toute tâche qui modifie l'architecture, les prérequis, les commandes ou le lancement met le README à jour **dans le même commit** ;
  - `tests/test_readme.py` (tâche 12) en vérifie la cohérence.
- **Commits** : messages courts en français, dans le style du dépôt. Chaque commit porte la ligne de fin `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (via un second `-m`).
- **L'exécutant ne saisit jamais d'identifiants BoursoBank.** Les étapes contre la vraie banque (tâches 2 et 13) sont faites par l'utilisateur.

## Review Focus

1. **Carte à débit différé comptée deux fois** : un total sur tous les comptes ne doit pas additionner les achats de la carte et la ligne `card_summary` du compte courant. Test : tâche 5, `test_card_summary_excluded_by_default`.
2. **Recherche texte avec accents, casse ou `%`/`_`** : `text="café"` doit trouver `CAFE`, et `50%` ne doit pas se comporter comme un joker. Tests : tâche 5, `test_text_filter_ignores_accents_and_case` et `test_text_filter_escapes_like_wildcards`.
3. **Premier lancement, store vide** : les outils de lecture doivent répondre « vide », avec `last_success_at: null`, sans planter. Test : tâche 12, `test_read_tools_on_empty_store`.
4. **Entrées invalides venant de Claude** (date malformée, bornes inversées, `group_by` inconnu, `limit` trop grand) : il faut un message d'erreur clair en français, ou une valeur bornée, jamais une trace d'exception. Tests : tâche 12, `test_invalid_inputs_raise_clear_errors` et `test_limit_is_clamped_to_500`.
5. **PDF de relevé supprimé du disque ou sans couche texte** : `read_statement` doit donner une erreur explicite ou `has_text: false`, et la synchro suivante doit retélécharger le fichier. Tests : tâche 6, `test_read_statement_missing_file` et `test_read_statement_without_text_layer` ; tâche 7, `test_missing_statement_file_is_redownloaded`.

---

### Task 1 : socle du projet, woob épinglé, README initial

**Files :**
- Create : `pyproject.toml`, `.python-version`, `.gitignore`, `src/bourso_mcp/__init__.py`, `README.md`, `tests/test_vendor_woob.py`
- Create (sous-module) : `vendor/woob`, `.gitmodules`

**Interfaces :**
- Produces : le paquet `bourso_mcp` installable, la commande `uv run pytest`, woob importable depuis `vendor/woob`.

- [ ] **Step 1 : ajouter woob en sous-module et l'épingler**

```bash
git submodule add https://gitlab.com/woob/woob.git vendor/woob
git -C vendor/woob log -1 --format="%H %cs %s" origin/master
git -C vendor/woob checkout <SHA affiché>
git -C vendor/woob log --oneline --since=2026-10-30 -- modules/boursorama
```

Résultat attendu : la dernière commande liste « Try using the configured identity ». Note le SHA, il sera reporté dans le README.

- [ ] **Step 2 : écrire `pyproject.toml`, `.python-version` (`3.12`) et `.gitignore`**

```toml
[project]
name = "bourso-mcp"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = ["woob", "mcp>=2.3,<3", "keyring>=25", "pypdf>=5", "filelock>=3.16"]

[project.scripts]
bourso-mcp = "bourso_mcp.cli.main:main"

[tool.uv.sources]
woob = { path = "vendor/woob", editable = true }

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/bourso_mcp"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.gitignore` contient `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `*.pdf` et `.idea/`. Les PDF de test sont générés à la volée (tâche 6) et ne sont pas versionnés.

- [ ] **Step 3 : écrire le test qui échoue, dans `tests/test_vendor_woob.py`**

```python
VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "woob"

def test_woob_is_importable_from_vendor():
    import woob
    assert Path(woob.__file__).resolve().is_relative_to(VENDOR)

def test_boursorama_module_is_vendored():
    assert (VENDOR / "modules" / "boursorama" / "module.py").is_file()
```

- [ ] **Step 4 : installer avec `uv sync`, puis lancer `uv run pytest -v`**

Résultat attendu : 2 tests passés. Il n'y a pas d'étape « échec » ici : c'est du socle, et `uv run` installerait de toute façon les dépendances avant de lancer les tests. Si `uv sync` échoue sur une dépendance de woob, note-la dans le README (section Prérequis) et corrige.

- [ ] **Step 5 : écrire `README.md`, avec ces sections et dans cet ordre**

1. **Objectif** (§1).
2. **Architecture** :
   - modules et règle de dépendance (§4.1) ;
   - flux (§4.2) ;
   - interface `BankSource` (§4.3) ;
   - modèle de données (§5) ;
   - règles de synchro (§6) ;
   - outils MCP et filtres (§7), présentés en tableaux et non recopiés tels quels.
3. **Prérequis** :
   - Git, uv ≥ 0.5 (`winget install --id=astral-sh.uv -e`), Python 3.12 (`uv python install 3.12`) ;
   - Claude Desktop ou Claude Code ;
   - un compte BoursoBank avec la 2FA par SMS, e-mail ou appli (pas de QR code).
4. **Installation** : `git clone --recurse-submodules …`, puis `uv sync`.
5. **Commandes** : `uv run pytest -v`, `uv run ruff check .`.
6. **Notes d'intégration woob** :
   - le SHA épinglé et sa date ;
   - la procédure de montée de version : `git -C vendor/woob fetch`, `checkout <sha>`, `uv sync`, tests, puis vérification manuelle (tâche 13).

Les sections Lancement, Synchro planifiée, Vérification manuelle et Dépannage seront ajoutées par les tâches qui les rendent réelles.

- [ ] **Step 6 : commit**

```bash
git add .
git commit -m "ajout du socle du projet et de woob épinglé" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2 : validation de faisabilité woob (spike, exécutée par l'utilisateur), POINT D'ARRÊT

**Files :**
- Create : `scripts/spike_woob.py` (jetable, supprimé à la tâche 10)
- Modify : `README.md` (section « Notes d'intégration woob »)

**Interfaces :**
- Produces : les réponses notées dans le README, qui pilotent la tâche 10 :
  - (a) la classe et les arguments woob pour charger les modules depuis `vendor/woob/modules` ;
  - (b) le paramètre du backend qui active la 2FA interactive, les exceptions levées pour chaque méthode (SMS, e-mail, appli) et la façon de reprendre après le code ou la validation ;
  - (c) la méthode 2FA observée ;
  - (d) si le compte joint est visible, et avec quelle valeur d'`ownership` ;
  - (e) si les cartes à débit différé sont des comptes avec `parent`, et si des lignes `TYPE_CARD_SUMMARY` apparaissent sur le compte courant ;
  - (f) comment un document est rattaché à un compte (subscription ↔ compte), sa valeur de `type` et le sens de `date` (date de fin de période ou date d'émission), ce qui fixe la règle de `period` ;
  - (g) si `rdate`, `vdate`, `category` et `card` sont renseignés ;
  - (h) l'ordre de `iter_history` (récent → ancien attendu) ;
  - (i) les dépendances supplémentaires nécessaires.

- [ ] **Step 1 : écrire `scripts/spike_woob.py`**

Comportement :
- Demande l'identifiant avec `input` et le mot de passe avec `getpass`. Rien n'est stocké ni journalisé.
- Charge woob avec `WoobBase(modules_path=<vendor/woob/modules>)`. Si cette signature n'existe pas dans la version épinglée, trouve l'équivalent dans `vendor/woob/woob/core/` et utilise-le.
- Stocke l'état du backend dans `%TEMP%\bourso-spike\storage` avec le stockage standard de woob, pour que l'état 2FA survive entre deux lancements.
- Active la 2FA interactive avec le paramètre que lit `TwoFactorBrowser` (voir `vendor/woob/woob/browser/browsers.py`).
- Boucle sur `iter_accounts()` :
  - sur une question 2FA, demande chaque champ et l'injecte dans la configuration du backend avant de réessayer ;
  - sur une validation dans l'appli, affiche le message et reprend.
- Pour chaque compte, affiche : type, ownership, parent, libellé, nombre d'opérations sur 30 jours, nombre d'opérations à venir, liste des types de transaction rencontrés, puis les champs `date`, `rdate`, `vdate`, `category` et `card` d'une opération.
- Liste les subscriptions et les documents (id, date, libellé, type, format), télécharge le dernier relevé dans `%TEMP%\bourso-spike\`, et affiche sa taille et si pypdf en extrait du texte.

- [ ] **Step 2 : vérifier la syntaxe avec `uv run ruff check scripts/spike_woob.py`**

Résultat attendu : aucune erreur.

- [ ] **Step 3 : POINT D'ARRÊT, l'utilisateur lance le script**

L'exécutant s'arrête et demande à l'utilisateur de lancer `uv run python scripts/spike_woob.py`, puis de lui rapporter la sortie **sans données personnelles** : montants, libellés et IBAN masqués.

- [ ] **Step 4 : consigner les réponses (a) à (i) dans le README**

Seulement des faits structurels. La règle de `period` y est écrite explicitement, par exemple : « period = mois de `date` » ou « period = mois précédant `date` ».

**Barrière :**
- Si la connexion, la liste des comptes ou le téléchargement d'un relevé échoue, **arrête le plan** et reviens à la décision d'architecture (§12, étape 0).
- Si une dépendance manque, ajoute-la dans `pyproject.toml` et dans la section Prérequis.

- [ ] **Step 5 : commit**

```bash
git add scripts/spike_woob.py README.md pyproject.toml uv.lock
git commit -m "validation de faisabilité woob boursorama" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3 : configuration, modèle de domaine, erreurs, interface `BankSource`

**Files :**
- Create : `src/bourso_mcp/config.py`, `src/bourso_mcp/source/__init__.py`, `src/bourso_mcp/source/models.py`, `src/bourso_mcp/source/errors.py`, `src/bourso_mcp/source/base.py`
- Test : `tests/test_config.py`
- Modify : `README.md` (sous-section « Configuration » : `BOURSO_MCP_HOME` et l'arborescence du dossier)

**Interfaces :**
- Produces :

```python
# config.py
@dataclass(frozen=True)
class Settings:
    home: Path
    # propriétés : db_path = home/"bourso.db", statements_dir = home/"statements",
    # exports_dir = home/"exports", woob_dir = home/"woob", lock_path = home/"sync.lock"
def load_settings(env: Mapping[str, str] = os.environ) -> Settings  # crée les dossiers

# source/models.py
AccountType = Literal["checking", "card"]
Ownership = Literal["owner", "co_owner", "attorney"]
TxType = Literal["card", "card_summary", "transfer", "order", "check", "deposit",
                 "withdrawal", "bank_fee", "other"]
TwoFactorMethod = Literal["sms", "email", "app"]
@dataclass(frozen=True)
class Account: id: str; label: str; type: AccountType; ownership: Ownership;
               parent_id: str | None; iban: str | None; currency: str; balance_cents: int | None
@dataclass(frozen=True)
class Transaction: operation_date: date; debit_date: date; value_date: date | None;
                   raw_label: str; label: str; amount_cents: int; type: TxType;
                   category: str | None; card: str | None
@dataclass(frozen=True)
class StatementRef: id: str; account_id: str; date: date; period: str;  # "AAAA-MM"
                    doc_type: Literal["statement", "other"];
                    handle: object = field(default=None, compare=False, repr=False)

# source/errors.py
class SourceError(Exception)
class TwoFactorRequired(SourceError):  # attribut .method: TwoFactorMethod
    def __init__(self, method: TwoFactorMethod)
class TwoFactorExpired(SourceError)
class AuthFailed(SourceError)
class AuthMethodUnsupported(SourceError)
class SourceBroken(SourceError)
class NetworkError(SourceError)

# source/base.py : Protocol BankSource, signatures exactes de la spec §4.3
```

- [ ] **Step 1 : écrire les tests qui échouent, dans `tests/test_config.py`**

```python
def test_default_home_is_localappdata(tmp_path):
    s = load_settings({"LOCALAPPDATA": str(tmp_path)})
    assert s.home == tmp_path / "bourso-mcp"
    assert s.db_path == tmp_path / "bourso-mcp" / "bourso.db"

def test_env_overrides_home(tmp_path):
    s = load_settings({"BOURSO_MCP_HOME": str(tmp_path / "x"), "LOCALAPPDATA": "ignored"})
    assert s.home == tmp_path / "x"

def test_load_settings_creates_directories(tmp_path):
    s = load_settings({"BOURSO_MCP_HOME": str(tmp_path)})
    assert s.statements_dir.is_dir() and s.exports_dir.is_dir() and s.woob_dir.is_dir()
```

- [ ] **Step 2 : lancer `uv run pytest tests/test_config.py -v`**

Résultat attendu : ÉCHEC (`bourso_mcp.config` introuvable).

- [ ] **Step 3 : implémenter les fichiers listés dans le bloc Interfaces**

- [ ] **Step 4 : lancer `uv run pytest -v`**

Résultat attendu : tous les tests passent.

- [ ] **Step 5 : mettre à jour le README (Configuration), puis commit**

```bash
git add src tests README.md
git commit -m "ajout de la configuration et du modèle de domaine" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4 : store, avec schéma, écritures et empreintes

**Files :**
- Create : `src/bourso_mcp/store/__init__.py`, `store/schema.py`, `store/text.py`, `store/money.py`, `store/fingerprint.py`, `store/repository.py`
- Test : `tests/conftest.py`, `tests/factories.py`, `tests/store/test_money_text.py`, `tests/store/test_fingerprint.py`, `tests/store/test_repository.py`

**Interfaces :**
- Consumes : `Settings`, `Account`, `Transaction`, `StatementRef` (tâche 3).
- Produces :

```python
# schema.py
def connect(db_path: Path) -> sqlite3.Connection  # row_factory=Row, foreign_keys=ON
def init_db(db_path: Path) -> None  # WAL, user_version=1, tables de la spec §5 (avec twofa_method)
# text.py
def normalize_text(s: str) -> str     # NFKD, sans diacritiques, casefold, espaces fusionnés, strip
# money.py
def cents_to_str(cents: int) -> str   # -1250 -> "-12.50"
def cents_to_fr(cents: int) -> str    # -1250 -> "-12,50"
def euros_to_cents(value: int | float | str | Decimal) -> int  # arrondi au plus proche (demi vers le haut), via Decimal(str(value))
# fingerprint.py
def compute_fingerprints(scope_key: str, txs: Sequence[Transaction]) -> list[str]
# repository.py
Trigger = Literal["mcp", "cli", "schedule"]
Scope = Literal["all", "transactions", "statements"]
RunStatus = Literal["running", "awaiting_2fa", "ok", "partial", "auth_required", "auth_failed", "error"]
@dataclass(frozen=True)
class RunRow: id: int; started_at: datetime; finished_at: datetime | None; trigger: Trigger;
              scope: Scope; status: RunStatus; twofa_method: str | None;
              added_transactions: int; added_statements: int;
              error_type: str | None; error_message: str | None
class Repository:
    def __init__(self, db_path: Path)  # appelle init_db ; une connexion par appel (sûr entre threads)
    db_path: Path
    def upsert_accounts(self, accounts: Sequence[Account], balance_at: datetime) -> None
    def list_accounts(self) -> list[sqlite3.Row]
    def upsert_transactions(self, account_id: str, txs: Sequence[Transaction], synced_at: datetime) -> int  # nb insérées
    def replace_pending(self, account_id: str, txs: Sequence[Transaction], synced_at: datetime) -> None
    def max_debit_date(self, account_id: str) -> date | None  # opérations comptabilisées seulement
    def statement_needs_download(self, statement_id: str, home: Path) -> bool
    def save_statement(self, ref: StatementRef, rel_path: str, sha256: str, downloaded_at: datetime) -> None
    def start_run(self, trigger: Trigger, scope: Scope, started_at: datetime) -> int
    def set_run_status(self, run_id: int, status: RunStatus, twofa_method: str | None = None) -> None
    def finish_run(self, run_id: int, status: RunStatus, finished_at: datetime, added_transactions: int,
                   added_statements: int, error_type: str | None, error_message: str | None) -> None
    def current_run(self) -> RunRow | None       # statut running ou awaiting_2fa
    def last_finished_run(self) -> RunRow | None
    def last_success_at(self) -> datetime | None  # finished_at du dernier passage ok ou partial
    def mark_interrupted_runs(self, at: datetime) -> int  # running/awaiting_2fa -> error « interrompu »
```

**Algorithme d'empreinte** (non déterminé par les tests, il est donc fixé ici) :
- on parcourt `txs` dans l'ordre et on calcule `base = f"{scope_key}|{op.isoformat()}|{amount_cents}|{raw_label}"` ;
- `rang` = nombre d'occurrences précédentes de la même `base` dans le lot ;
- empreinte = `sha256(f"{base}|{rang}")` en hexadécimal ;
- `scope_key` vaut l'`account_id` pour les opérations comptabilisées, et `f"{account_id}|pending"` pour les opérations à venir, pour éviter qu'elles entrent en collision avec les comptabilisées.

`upsert_transactions` insère ou met à jour sur `(account_id, fingerprint)` les champs `label`, `value_date`, `category`, `card`, `type` et `synced_at`, avec `pending = 0`. `search_text` vaut `normalize_text(raw_label + " " + label)`.

- [ ] **Step 1 : écrire les helpers de test**

`tests/factories.py` :
- `make_tx(op="2026-10-01", amount=-1250, raw="CB CAFE", label=None, type="card", debit=None, category=None, card=None, value=None) -> Transaction`. Les dates sont données en chaînes ISO, `debit` vaut `op` par défaut et `label` vaut `raw` par défaut.
- `make_account(id="cc1", type="checking", ownership="owner", parent_id=None, balance_cents=100000) -> Account`.

`tests/conftest.py` : fixtures `settings` (`load_settings({"BOURSO_MCP_HOME": str(tmp_path)})`), `repo` (`Repository(settings.db_path)`) et `now` (`datetime(2026, 11, 10, 7, 0, tzinfo=timezone(timedelta(hours=1)))`).

- [ ] **Step 2 : écrire les tests qui échouent**

```python
# test_money_text.py
def test_money_conversions():
    assert cents_to_str(-1250) == "-12.50" and cents_to_str(5) == "0.05"
    assert cents_to_fr(-1250) == "-12,50"
    assert euros_to_cents("12.345") == 1235 and euros_to_cents(12.3) == 1230

def test_normalize_text():
    assert normalize_text("  Café  DE la Gare ") == "cafe de la gare"

# test_fingerprint.py
def test_fingerprints_rank_identical_transactions():
    a, b = make_tx(), make_tx()
    fps = compute_fingerprints("cc1", [a, b])
    assert fps[0] != fps[1] and compute_fingerprints("cc1", [a, b]) == fps

def test_fingerprint_depends_on_scope_key():
    assert compute_fingerprints("cc1", [make_tx()]) != compute_fingerprints("cc1|pending", [make_tx()])

# test_repository.py
def test_upsert_is_idempotent(repo, now): ...       # 3 insérées puis 0 ; 3 lignes en base
def test_upsert_updates_mutable_fields(repo, now): ...  # nouveau label => mise à jour, pas de doublon
def test_replace_pending_replaces_only_pending(repo, now): ...  # 2 comptabilisées + 2 à venir -> remplacement par 1 à venir => 3 lignes
def test_max_debit_date_ignores_pending(repo, now): ...
def test_statement_needs_download_when_file_missing(repo, settings, now): ...  # enregistré + fichier présent => False ; fichier supprimé => True
def test_last_success_at_counts_ok_and_partial(repo, now): ...  # ok à T1, partial à T2, error à T3 => T2
def test_mark_interrupted_runs(repo, now): ...  # running -> error, error_message == "interrompu"
```

- [ ] **Step 3 : lancer `uv run pytest tests/store -v`**

Résultat attendu : ÉCHEC (modules introuvables).

- [ ] **Step 4 : implémenter les fichiers du bloc Interfaces**

Les index sont ceux de la spec §5.

- [ ] **Step 5 : lancer `uv run pytest -v`**

Résultat attendu : tous les tests passent.

- [ ] **Step 6 : commit**

```bash
git add src tests
git commit -m "ajout du store sqlite et des empreintes de transactions" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5 : store, avec recherche et agrégats

**Files :**
- Create : `src/bourso_mcp/store/queries.py`
- Test : `tests/store/test_queries.py`

**Interfaces :**
- Consumes : `Repository`, `connect`, `normalize_text` (tâche 4), `TxType` (tâche 3).
- Produces :

```python
@dataclass(frozen=True)
class TransactionFilter:
    account_ids: tuple[str, ...] | None = None
    date_from: date | None = None
    date_to: date | None = None
    date_field: Literal["operation", "debit"] = "operation"
    amount_min_cents: int | None = None   # comparé à abs(amount_cents)
    amount_max_cents: int | None = None
    direction: Literal["debit", "credit"] | None = None
    text: str | None = None
    types: tuple[TxType, ...] | None = None   # None => tous sauf "card_summary"
    category: str | None = None
    card: str | None = None                   # fin du numéro masqué
    include_pending: bool = True
Sort = Literal["date_desc", "date_asc", "amount_asc", "amount_desc"]
GroupBy = Literal["month", "category", "label", "type", "account"]
@dataclass(frozen=True)
class Totals: count: int; debits_cents: int; credits_cents: int   # débits <= 0 ; net = débits + crédits
@dataclass(frozen=True)
class Group: key: str; totals: Totals
@dataclass(frozen=True)
class SearchResult: items: list[sqlite3.Row]; totals: Totals
def build_where(f: TransactionFilter) -> tuple[str, list[object]]   # partagé avec l'export (tâche 6)
def search(db_path: Path, f: TransactionFilter, sort: Sort = "date_desc", limit: int = 100, offset: int = 0) -> SearchResult
def summarize(db_path: Path, f: TransactionFilter, group_by: GroupBy, limit: int = 50) -> list[Group]
```

Règles :
- **Texte** : `search_text LIKE '%' || ? || '%' ESCAPE '\'`, avec l'aiguille passée par `normalize_text` et les caractères `\`, `%` et `_` échappés.
- **Tri** : on départage les égalités par `id`.
- **Clés de groupe** :
  - `month` : `substr(<date_field>, 1, 7)` ;
  - `category` : `coalesce(category, '(sans catégorie)')` ;
  - `label` : `label` ;
  - `account` : `account_id`.
- Les groupes sont triés par `abs(net)` décroissant, puis par clé.

- [ ] **Step 1 : écrire les tests qui échouent, dans `tests/store/test_queries.py`**

Chaque test précharge le store avec `make_account` et `make_tx`.

```python
def test_amount_bounds_apply_to_absolute_value(...):
    # montants -30, -60, +60, -120 € ; min 50 €, max 100 €
    assert amounts(search(db, F(amount_min_cents=5000, amount_max_cents=10000))) == [-6000, 6000]
    assert amounts(search(db, F(amount_min_cents=5000, amount_max_cents=10000, direction="debit"))) == [-6000]

def test_text_filter_ignores_accents_and_case(...):   # "CAFE DE LA GARE" trouvé avec text="café"
def test_text_filter_escapes_like_wildcards(...):     # "REMISE 50%" et "REMISE 500" : "50%" -> seulement le premier ; "_" -> aucun
def test_card_summary_excluded_by_default(...):
    # carte cb1 (parent cc1) : -20 € et -30 € en "card" ; cc1 : -50 € en "card_summary"
    assert search(db, F()).totals.debits_cents == -5000
    assert search(db, F(types=("card_summary",))).totals.count == 1
def test_date_bounds_are_inclusive_on_operation_and_debit_date(...)
def test_filter_by_account_ids_category_and_card(...)  # card="1234" trouve "4970 XXXX XXXX 1234"
def test_include_pending_false(...)
def test_totals_cover_whole_filter_not_page(...):      # 150 lignes, limit 100 => 100 items, totals.count == 150
def test_sort_orders(...)                              # 4 tris, départage par id
def test_summarize_by_month_and_category(...):         # clés, comptes, totaux attendus et ordre par abs(net)
```

- [ ] **Step 2 : lancer `uv run pytest tests/store/test_queries.py -v`**

Résultat attendu : ÉCHEC.

- [ ] **Step 3 : implémenter `store/queries.py`**

- [ ] **Step 4 : lancer `uv run pytest -v`**

Résultat attendu : tous les tests passent.

- [ ] **Step 5 : commit**

```bash
git add src tests
git commit -m "ajout de la recherche et des agrégats de transactions" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6 : store, avec export CSV et lecture des relevés

**Files :**
- Create : `src/bourso_mcp/store/export.py`, `src/bourso_mcp/store/statements.py`
- Test : `tests/pdf_fixture.py`, `tests/store/test_export.py`, `tests/store/test_statements.py`

**Interfaces :**
- Consumes : `TransactionFilter`, `build_where` (tâche 5), `cents_to_fr`, `Repository.save_statement` (tâche 4).
- Produces :

```python
# export.py
CSV_HEADER = "compte;date_operation;date_debit;libelle;libelle_brut;montant;type;categorie;carte;a_venir"
def export_csv(db_path: Path, f: TransactionFilter, exports_dir: Path, now: datetime) -> tuple[Path, int]
# fichier transactions-AAAAMMJJ-HHMMSS.csv, utf-8-sig, ";", tri date_asc, montant en cents_to_fr, a_venir "oui"/"non"
# statements.py
class StatementNotFound(LookupError)
class StatementFileMissing(FileNotFoundError)
@dataclass(frozen=True)
class StatementText: row: sqlite3.Row; path: Path; pages: int; text: str; has_text: bool
def list_statements(db_path: Path, account_ids: tuple[str, ...] | None, period_from: str | None, period_to: str | None) -> list[sqlite3.Row]
def read_statement(db_path: Path, home: Path, statement_id: str) -> StatementText   # pypdf, pages jointes par "\n"
```

- [ ] **Step 1 : écrire `tests/pdf_fixture.py`**

- `text_pdf(text: str) -> bytes` construit un PDF minimal d'une page, avec la police Helvetica, un flux `BT /F1 12 Tf 72 720 Td (<text>) Tj ET` et des offsets xref calculés.
- `blank_pdf() -> bytes` utilise `pypdf.PdfWriter().add_blank_page(595, 842)`.

- [ ] **Step 2 : écrire les tests qui échouent**

```python
def test_export_csv_format(...):
    path, n = export_csv(db, F(), settings.exports_dir, now)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and n == 2
    lines = raw.decode("utf-8-sig").splitlines()
    assert lines[0] == CSV_HEADER and ";-12,50;" in lines[1]
    assert path.name == "transactions-20261110-070000.csv"
def test_export_respects_filter(...)
def test_list_statements_filters_by_period(...)        # 2026-08..2026-10, period_from="2026-09" => 2 relevés
def test_read_statement_extracts_text(...)             # text_pdf("RELEVE OCTOBRE") => "RELEVE OCTOBRE" in text, has_text True
def test_read_statement_without_text_layer(...)        # blank_pdf => has_text False, text == ""
def test_read_statement_missing_file(...)              # fichier supprimé => StatementFileMissing
def test_read_statement_unknown_id(...)                # => StatementNotFound
```

- [ ] **Step 3 : lancer `uv run pytest tests/store -v`**

Résultat attendu : ÉCHEC sur les nouveaux tests.

- [ ] **Step 4 : implémenter `export.py` et `statements.py`**

- [ ] **Step 5 : lancer `uv run pytest -v`**

Résultat attendu : tous les tests passent.

- [ ] **Step 6 : commit**

```bash
git add src tests
git commit -m "ajout de l'export csv et de la lecture des relevés" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7 : `SyncService`, un passage complet sans 2FA

**Files :**
- Create : `src/bourso_mcp/sync/__init__.py`, `src/bourso_mcp/sync/service.py`
- Test : `tests/fakes.py`, `tests/sync/test_service_pass.py`

**Interfaces :**
- Consumes : `BankSource` et les erreurs (tâche 3), `Repository` (tâche 4), `Settings`.
- Produces :

```python
TwoFactorHandler = Callable[[TwoFactorMethod], str | None]
OVERLAP = timedelta(days=10)
@dataclass(frozen=True)
class AccountOutcome: account_id: str; ok: bool; added_transactions: int; added_statements: int;
                      error_type: str | None
@dataclass(frozen=True)
class SyncRunResult: run_id: int; status: RunStatus; added_transactions: int; added_statements: int;
                     accounts: list[AccountOutcome]; error_type: str | None; error_message: str | None
@dataclass(frozen=True)
class DryRunLine: account_id: str; label: str; type: str; transactions: int; pending: int; statements: int
class SyncService:
    def __init__(self, source_factory: Callable[[], BankSource], repo: Repository, settings: Settings,
                 *, now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 sleep: Callable[[float], None] = time.sleep)
    def run(self, scope: Scope, trigger: Trigger, two_factor: TwoFactorHandler, *, force: bool = False) -> SyncRunResult
    def dry_run(self, two_factor: TwoFactorHandler) -> list[DryRunLine]   # aucune écriture
```

`tests/fakes.py` :
- `FakeBankSource(accounts, history={}, pending={}, statements={}, two_factor=None, expected_code="123456", login_error=None, account_errors={}, network_failures=0)` respecte le Protocol `BankSource`.
- Les `N` premiers appels à `login` lèvent `NetworkError`.
- `iter_transactions` filtre sur `debit_date >= since`.
- `account_errors[id]` est levée par `iter_transactions` pour ce compte.
- Chaque appel est enregistré dans `self.calls`, par exemple `("iter_transactions", "cc1", date(2026, 10, 21))`.

Déroulé de `run` dans cette tâche :
1. `start_run`, puis `login`.
2. `upsert_accounts`.
3. Pour chaque compte :
   - si le scope inclut les transactions : `since = max_debit_date - OVERLAP` (`None` au premier passage), puis `upsert_transactions` et `replace_pending` ;
   - si le scope inclut les relevés : `list_statements`, puis pour chaque relevé où `statement_needs_download` est vrai, `download_statement`, écriture du fichier `statements/<account_id>/<period>.pdf` (suffixe `-2`, `-3`… si le nom est pris), sha256 et `save_statement`.
4. Une `SourceError` sur un compte rend ce compte `ok=False`. Le statut final vaut `partial` si au moins un compte a réussi, `error` sinon.
5. `finish_run`.

La 2FA, les nouvelles tentatives, le verrou et la limitation de fréquence viennent à la tâche 8.

- [ ] **Step 1 : écrire `tests/fakes.py` et les tests qui échouent**

```python
def test_first_pass_fetches_full_history(...):        # appel ("iter_transactions","cc1",None) ; status "ok" ; added_transactions == 3
def test_incremental_pass_uses_ten_day_overlap(...):  # max debit 2026-10-31 => since 2026-10-21 ; 0 doublon
def test_pending_becoming_booked_is_not_duplicated(...)
def test_statements_downloaded_once_and_named_by_period(...):  # 2 docs 2026-10 => 2026-10.pdf et 2026-10-2.pdf ; 2e passage => 0 téléchargement
def test_missing_statement_file_is_redownloaded(...)
def test_failure_on_one_account_gives_partial(...):   # account_errors={"cb1": SourceBroken()} => "partial", cc1 ok
def test_scope_limits_work(...):                       # "transactions" => aucun list_statements ; "statements" => aucun iter_transactions
def test_balances_updated(...)
def test_dry_run_writes_nothing(...):                  # lignes par compte ; aucune ligne en base ni dans sync_runs
```

- [ ] **Step 2 : lancer `uv run pytest tests/sync -v`**

Résultat attendu : ÉCHEC.

- [ ] **Step 3 : implémenter `sync/service.py`**

- [ ] **Step 4 : lancer `uv run pytest -v`**

Résultat attendu : tous les tests passent.

- [ ] **Step 5 : commit**

```bash
git add src tests
git commit -m "ajout du service de synchronisation" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8 : synchro, avec 2FA, erreurs, nouvelles tentatives, verrou et fréquence

**Files :**
- Modify : `src/bourso_mcp/sync/service.py`
- Create : `src/bourso_mcp/sync/twofactor.py`
- Test : `tests/sync/test_service_auth.py`, `tests/sync/test_service_guards.py`, `tests/sync/test_twofactor.py`

**Interfaces :**
- Consumes : la tâche 7.
- Produces :

```python
# service.py (ajouts)
RATE_LIMIT = timedelta(hours=1)
RETRY_DELAYS = (2, 8, 30)
class SyncAlreadyRunning(Exception): run: RunRow | None
class SyncTooSoon(Exception): last_success_at: datetime
def SyncService.check_can_start(self, force: bool) -> None   # lève SyncTooSoon ou SyncAlreadyRunning ; ne crée pas de source
# twofactor.py
def interactive_handler(prompt: Callable[[str], str] = input, out: Callable[[str], None] = print) -> TwoFactorHandler
def non_interactive_handler(method: TwoFactorMethod) -> NoReturn   # lève TwoFactorExpired
```

Comportement (spec §6.2, §6.3, §9) :
- **Ordre** :
  1. `check_can_start(force)` ;
  2. prise de `FileLock(settings.lock_path, timeout=0)` (si déjà pris : `SyncAlreadyRunning(repo.current_run())`) ;
  3. `mark_interrupted_runs` ;
  4. `start_run`.
- **2FA** :
  - `login` lève `TwoFactorRequired(m)` : on passe à `set_run_status(awaiting_2fa, m)`, puis `code = two_factor(m)`, puis `submit_2fa(code)`, puis `set_run_status(running)`. Le statut reste donc `awaiting_2fa` pendant que la source attend la validation dans l'appli ;
  - `TwoFactorExpired`, ou un nouveau `TwoFactorRequired` après `submit_2fa`, donnent `auth_required`.
- `AuthFailed` donne `auth_failed`, avec **un seul** appel à `login`. `AuthMethodUnsupported` donne `error`.
- **`NetworkError`** :
  - on retente `login` et chaque appel de compte avec `sleep(2)`, `sleep(8)`, `sleep(30)` ;
  - une fois les délais épuisés : `error` si c'est la connexion, compte en échec si c'est un compte ;
  - **jamais** de nouvelle tentative sur `submit_2fa`.
- **Interactif** : `interactive_handler` demande `"Code reçu par SMS : "` ou `"Code reçu par e-mail : "` et retire les espaces. Pour `app`, il affiche `"Valide la connexion dans l'appli BoursoBank…"` et renvoie `None`.
- **Journaux** : logger `bourso_mcp.sync`. Au niveau INFO, uniquement des statuts et des compteurs.

- [ ] **Step 1 : écrire les tests qui échouent**

```python
def test_sms_two_factor_flow(...):   # le handler voit current_run().status == "awaiting_2fa" et twofa_method == "sms" ; ("submit_2fa","123456") ; "ok"
def test_app_validation_passes_none(...)       # ("submit_2fa", None) ; pendant submit_2fa, current_run().status == "awaiting_2fa"
def test_two_factor_expired_gives_auth_required(...)
def test_wrong_code_gives_auth_required(...)
def test_auth_failed_is_not_retried(...):            # nb d'appels login == 1 ; "auth_failed"
def test_auth_method_unsupported_gives_error(...)
def test_network_error_retried_then_succeeds(...):   # network_failures=2 => sleeps == [2, 8] ; "ok"
def test_network_error_exhausted(...):               # network_failures=9 => sleeps == [2, 8, 30] ; "error"
def test_second_concurrent_sync_raises_already_running(...)   # FileLock tenu par le test
def test_rate_limit_and_force(...):                  # succès il y a 30 min => SyncTooSoon ; force=True => passage lancé
def test_interrupted_run_marked_error_on_next_start(...)
def test_info_logs_contain_no_bank_data(caplog, ...): # ni libellé ni "12.50" ni IBAN au niveau INFO
def test_interactive_handler_prompts_and_strips_spaces():  # prompt renvoie " 123 456 " => "123456"
def test_interactive_handler_app_returns_none_and_prints_instruction()
def test_non_interactive_handler_raises()
```

- [ ] **Step 2 : lancer `uv run pytest tests/sync -v`**

Résultat attendu : ÉCHEC sur les nouveaux tests.

- [ ] **Step 3 : implémenter les ajouts**

- [ ] **Step 4 : lancer `uv run pytest -v`**

Résultat attendu : tous les tests passent.

- [ ] **Step 5 : commit**

```bash
git add src tests
git commit -m "gestion de la 2fa, des erreurs et des garde-fous de synchro" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9 : synchro en tâche de fond (`BackgroundSync`)

**Files :**
- Create : `src/bourso_mcp/sync/runner.py`
- Test : `tests/sync/test_runner.py`

**Interfaces :**
- Consumes : `SyncService.run` et `check_can_start`, `SyncTooSoon`, `SyncAlreadyRunning` (tâche 8), `Repository` (tâche 4).
- Produces :

```python
@dataclass(frozen=True)
class StartResult: started: bool; reason: Literal["started", "already_running", "too_soon"];
                   last_success_at: datetime | None
class BackgroundSync:
    def __init__(self, service: SyncService, repo: Repository, *, code_timeout: float = 600.0)
    def start(self, scope: Scope, force: bool = False) -> StartResult  # trigger "mcp", thread démon
    def submit_code(self, code: str) -> bool   # True si un passage attend un code sms/email ; espaces retirés
    def status(self) -> dict    # {"current": dict|None, "last": dict|None, "last_success_at": str|None} (ISO)
    def wait(self, timeout: float) -> SyncRunResult | None   # utilitaire de test
```

Le handler interne :
- pour `app`, renvoie `None` (la source attend elle-même la validation) ;
- pour `sms` ou `email`, attend sur une `queue.Queue` alimentée par `submit_code`, avec `get(timeout=code_timeout)`. Si la file reste vide, il lève `TwoFactorExpired`.

- [ ] **Step 1 : écrire les tests qui échouent**

Les tests utilisent `FakeBankSource` et un helper `wait_until(pred, timeout=2)`.

```python
def test_start_returns_immediately_and_completes(...)     # started True ; wait(2).status == "ok"
def test_sms_code_submitted_via_submit_code(...)          # status()["current"]["status"] == "awaiting_2fa" ; submit_code("123 456") is True ; "ok"
def test_submit_code_without_waiting_run_returns_false(...)
def test_code_timeout_gives_auth_required(...)            # code_timeout=0.1
def test_start_while_running_reports_already_running(...)
def test_start_too_soon_reports_too_soon(...)             # reason "too_soon", last_success_at renseigné
```

- [ ] **Step 2 : lancer `uv run pytest tests/sync/test_runner.py -v`**

Résultat attendu : ÉCHEC.

- [ ] **Step 3 : implémenter `sync/runner.py`**

- [ ] **Step 4 : lancer `uv run pytest -v`, puis 3 fois de suite `uv run pytest tests/sync/test_runner.py -q`**

Résultat attendu : tous les tests passent, et les 3 lancements affichent `6 passed`. Les tests de threads ne doivent pas être instables.

- [ ] **Step 5 : commit**

```bash
git add src tests
git commit -m "ajout de la synchro en tâche de fond" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10 : identifiants et `WoobBoursoSource`

**Files :**
- Create : `src/bourso_mcp/credentials.py`, `src/bourso_mcp/source/woob_mapping.py`, `src/bourso_mcp/source/woob_source.py`
- Test : `tests/test_credentials.py`, `tests/source/test_woob_mapping.py`, `tests/source/test_woob_source.py`
- Delete : `scripts/spike_woob.py`
- Modify : `README.md` (table de correspondance woob → domaine dans les notes d'intégration ; stockage des identifiants ; retrait de la mention du spike)

**Interfaces :**
- Consumes : les notes (a) à (i) du README (tâche 2), le modèle et les erreurs (tâche 3).
- Produces :

```python
# credentials.py
@dataclass(frozen=True)
class Credentials: login: str; password: str = field(repr=False)
class CredentialStore(Protocol):
    def get(self) -> Credentials | None: ...
    def set(self, c: Credentials) -> None: ...
class KeyringCredentialStore:   # service keyring "bourso-mcp" ; clé "login" -> identifiant ; clé <identifiant> -> mot de passe
# woob_mapping.py
def to_account(a: "woob Account") -> Account | None    # None hors TYPE_CHECKING / TYPE_CARD
def to_transaction(t: "woob Transaction") -> Transaction
def to_statement_ref(account_id: str, d: "woob Document") -> StatementRef
def translate_error(e: Exception) -> SourceError | None   # None : exception non woob, à relancer
# woob_source.py
VENDOR_WOOB = Path(__file__).resolve().parents[3] / "vendor" / "woob"
ALLOWED_BACKEND_CALLS = frozenset({"iter_accounts", "iter_history", "iter_coming",
                                   "iter_subscription", "iter_documents", "download_document"})
class WoobBoursoSource:   # respecte BankSource
    def __init__(self, credentials: Credentials, woob_dir: Path,
                 modules_path: Path = VENDOR_WOOB / "modules", backend_factory: Callable[..., Any] | None = None)
```

Correspondances (valeurs fixées) :
- **Montant** : `Decimal` vers centimes, arrondi `ROUND_HALF_UP`.
- **Dates** : `operation_date = rdate` si non vide, sinon `date` ; `debit_date = date` ; `value_date = vdate` ou `None`. « Vide » au sens du helper `empty()` de woob.
- **Libellés** : `raw_label = raw or label` ; `label = label or raw`.
- **Types de transaction** :
  - `TYPE_CARD`, `TYPE_DEFERRED_CARD` → `card` ;
  - `TYPE_CARD_SUMMARY` → `card_summary` ;
  - `TYPE_TRANSFER`, `TYPE_INSTANT` (s'il existe dans la version épinglée) → `transfer` ;
  - `TYPE_ORDER` → `order` ;
  - `TYPE_CHECK` → `check` ;
  - `TYPE_DEPOSIT`, `TYPE_CASH_DEPOSIT` → `deposit` ;
  - `TYPE_WITHDRAWAL` → `withdrawal` ;
  - `TYPE_BANK` → `bank_fee` ;
  - tous les autres → `other`.
- **Comptes** :
  - `TYPE_CHECKING` → `checking`, `TYPE_CARD` → `card` ;
  - `AccountOwnership` OWNER, CO_OWNER, ATTORNEY → `owner`, `co_owner`, `attorney` ; si vide : `owner` ;
  - `parent_id = a.parent.id` si un parent existe ; devise par défaut `EUR`.
- **Documents** : `doc_type = "statement"` si le type est celui relevé à la tâche 2 (f), sinon `other`. `period` suit la règle notée en (f).
- **Erreurs** (classes de `vendor/woob/woob/exceptions.py`) :
  - `BrowserIncorrectPassword` → `AuthFailed` ;
  - `BrowserQuestion` → `TwoFactorRequired`, avec `sms` pour le champ `code` et `email` pour `email_code` ;
  - `AppValidation` → `TwoFactorRequired("app")` ;
  - `AppValidationExpired`, `AppValidationCancelled`, `NeedInteractiveFor2FA` → `TwoFactorExpired` ;
  - `AuthMethodNotImplemented` → `AuthMethodUnsupported` ;
  - `BrowserUnavailable`, ainsi que les erreurs de connexion et de délai de `requests` → `NetworkError` ;
  - `ActionNeeded` et toute autre exception levée par du code woob → `SourceBroken`, avec le message woob.

Session :
- elle suit les notes (a), (b) et (f) ;
- tout appel au backend passe par `_call(name, *args)`, qui vérifie `name in ALLOWED_BACKEND_CALLS` ;
- `iter_transactions` s'arrête dès que `debit_date < since`, l'historique woob allant du plus récent au plus ancien (note h).

- [ ] **Step 1 : écrire les tests qui échouent**

Les objets woob sont construits à la main (`woob.capabilities.bank`, `woob.capabilities.bill`). Le backend factice enregistre les appels.

```python
def test_to_account_checking_joint():           # CO_OWNER => "co_owner" ; balance Decimal("1234.56") => 123456
def test_to_account_ignores_savings():          # TYPE_SAVINGS => None
def test_to_account_card_has_parent():
def test_to_transaction_dates_and_amount():     # rdate vide => operation_date == date ; Decimal("-12.505") => -1251
@pytest.mark.parametrize(...) def test_type_mapping(woob_type, expected)
def test_to_statement_ref_period():             # règle notée en (f)
@pytest.mark.parametrize(...) def test_translate_error(exc, expected_type)
def test_credentials_repr_hides_password():     # "secret" not in repr(...)
def test_keyring_store_roundtrip():             # backend keyring en mémoire (keyring.set_keyring)
def test_iter_transactions_stops_before_since():
def test_source_only_calls_allowed_backend_methods():   # flux complet : noms appelés ⊆ ALLOWED_BACKEND_CALLS
def test_source_module_never_references_write_methods():
    src = Path(woob_source.__file__).read_text(encoding="utf-8").lower()
    assert not any(w in src for w in ("transfer", "recipient"))
```

- [ ] **Step 2 : lancer `uv run pytest tests/source tests/test_credentials.py -v`**

Résultat attendu : ÉCHEC.

- [ ] **Step 3 : implémenter les trois modules**

Ensuite, supprime `scripts/spike_woob.py` et mets à jour le README.

- [ ] **Step 4 : lancer `uv run pytest -v` et `uv run ruff check .`**

Résultat attendu : tous les tests passent et ruff ne signale rien.

- [ ] **Step 5 : commit**

```bash
git add -A
git commit -m "ajout de la source woob boursorama et du stockage des identifiants" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11 : ligne de commande (`login`, `sync`, `status`)

**Files :**
- Create : `src/bourso_mcp/cli/__init__.py`, `src/bourso_mcp/cli/main.py`, `src/bourso_mcp/cli/wiring.py`
- Test : `tests/cli/test_cli.py`
- Modify : `README.md` (section Commandes complète : chaque commande, ses options et ses codes de sortie)

**Interfaces :**
- Consumes : `SyncService` et `interactive_handler`/`non_interactive_handler` (tâches 7 et 8), `WoobBoursoSource` et `KeyringCredentialStore` (tâche 10), `load_settings` (tâche 3).
- Produces :

```python
# wiring.py
class MissingCredentials(Exception)
def credential_store() -> CredentialStore                      # KeyringCredentialStore()
def source_factory(settings: Settings, store: CredentialStore) -> Callable[[], BankSource]  # MissingCredentials si store.get() is None
def build_service(settings: Settings, factory: Callable[[], BankSource] | None = None) -> SyncService
# main.py
def main(argv: list[str] | None = None) -> int
# codes de sortie : 0 ok ; 1 partial ou error ; 2 auth_required, auth_failed ou identifiants absents ; 3 refus (trop tôt, déjà en cours)
```

Commandes :
- **`login`** :
  - demande `"Identifiant BoursoBank : "`, en redemandant tant que la saisie n'est pas uniquement composée de chiffres, puis le mot de passe avec `getpass("Mot de passe : ")` ;
  - enregistre les identifiants avec `store.set` ;
  - lance un passage `run("all", "cli", interactive_handler(), force=True)`, qui ouvre la session avec la 2FA et fait la première synchro ;
  - affiche le résumé.
- **`sync`** : options `--scope {all,transactions,statements}` (par défaut `all`), `--force`, `--non-interactive` (trigger `schedule`, handler non interactif) et `--dry-run` (tableau par compte, sans écriture).
  - Résumé en cas de succès : `"ok : 12 transactions et 1 relevé ajoutés"`.
  - En cas de `partial`, une ligne par compte en échec.
- **`status`** : affiche le passage en cours, le dernier passage et la dernière réussite.
- **Journaux** : `logging.basicConfig(level=INFO, stream=sys.stderr)`.
- **Identifiants absents** : `MissingCredentials` affiche `"Aucun identifiant enregistré : lance d'abord « bourso-mcp login »."` et sort avec le code 2.

- [ ] **Step 1 : écrire les tests qui échouent**

`BOURSO_MCP_HOME` pointe sur `tmp_path`. `wiring.source_factory` et `wiring.credential_store` sont remplacés par monkeypatch (`FakeBankSource`, keyring en mémoire).

```python
def test_sync_prints_summary_and_exit_code_0(capsys, ...)
def test_sync_non_interactive_with_2fa_exits_2_without_prompting(...)   # input monkeypatché pour échouer s'il est appelé
def test_sync_too_soon_exits_3_and_force_bypasses(...)
def test_sync_dry_run_writes_nothing(...)
def test_login_stores_credentials_and_syncs(...)
def test_login_reprompts_non_digit_identifier(...)
def test_status_output(...)
def test_missing_credentials_message(...)
```

- [ ] **Step 2 : lancer `uv run pytest tests/cli -v`**

Résultat attendu : ÉCHEC.

- [ ] **Step 3 : implémenter `wiring.py` et `main.py`, puis mettre à jour le README**

- [ ] **Step 4 : lancer `uv run pytest -v`, puis `uv run bourso-mcp --help`**

Résultat attendu : tous les tests passent, et l'aide liste `login`, `sync` et `status`.

- [ ] **Step 5 : commit**

```bash
git add src tests README.md
git commit -m "ajout de la ligne de commande login, sync, status" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12 : serveur MCP (outils, application stdio, `serve`)

**Files :**
- Create : `src/bourso_mcp/server/__init__.py`, `src/bourso_mcp/server/tools.py`, `src/bourso_mcp/server/app.py`
- Modify : `src/bourso_mcp/cli/main.py` (sous-commande `serve`), `README.md` (outils MCP avec leurs paramètres définitifs ; section « Lancement du MCP »)
- Test : `tests/server/test_tools.py`, `tests/server/test_app.py`, `tests/test_readme.py`

**Interfaces :**
- Consumes :
  - `search`, `summarize`, `TransactionFilter` (tâche 5) ;
  - `export_csv`, `list_statements`, `read_statement` et leurs erreurs (tâche 6) ;
  - `BackgroundSync` (tâche 9), `Repository`, `cents_to_str`, `euros_to_cents` (tâche 4) ;
  - `wiring.build_service` (tâche 11).
- Produces :

```python
# tools.py
TOOL_NAMES = ("list_accounts", "search_transactions", "summarize_transactions", "export_transactions",
              "list_statements", "read_statement", "sync_now", "sync_status", "submit_2fa_code")
class ToolInputError(ValueError)
def parse_filter(*, account_ids=None, date_from=None, date_to=None, date_field="operation", amount_min=None,
                 amount_max=None, direction=None, text=None, types=None, category=None, card=None,
                 include_pending=True) -> TransactionFilter
class Tools:
    def __init__(self, settings: Settings, repo: Repository, runner: BackgroundSync)
    # une méthode par nom de TOOL_NAMES, paramètres de la spec §7 ; renvoie un dict sérialisable en JSON
# app.py
def build_server(tools: Tools)  # serveur de haut niveau du SDK `mcp` 2.x ; un outil enregistré par nom de TOOL_NAMES
def serve_stdio(settings: Settings) -> None
```

Règles :
- **Bloc `sync`** : chaque outil de lecture ajoute `"sync": {"last_success_at": iso|None, "last_status": str|None, "auth_required": bool}`. `auth_required` est vrai si le passage en cours est `awaiting_2fa`, ou si le dernier passage terminé est `auth_required` ou `auth_failed`.
- **Montants** : rendus avec `cents_to_str`, dans `amount`, `total_debits`, `total_credits` et `net`.
- **Réponse de `search_transactions`** : `items`, `total_count`, `total_debits`, `total_credits`, `net`, `limit`, `offset` et `sync`.
- **Validation** : `ToolInputError` avec un message en français qui nomme le paramètre fautif et les valeurs autorisées. Elle porte sur :
  - les dates (`AAAA-MM-JJ`) et les périodes (`AAAA-MM`) ;
  - les bornes de date et de montant inversées, et les montants négatifs ;
  - les valeurs inconnues de `types`, `sort`, `group_by`, `scope`, `direction` et `date_field`.
- **Bornage** : `limit` est ramené dans [1, 500] et `offset` doit être ≥ 0.
- **Erreurs de relevé** : `StatementFileMissing` devient une `ToolInputError` qui invite à lancer `sync_now(scope="statements")`.
- **Descriptions des outils** (en français, dans `app.py`) : elles rappellent la lecture seule, que les totaux portent sur le filtre entier, l'exclusion par défaut de `card_summary`, et qu'il faut regarder le bloc `sync` pour juger de la fraîcheur.
- **Enregistrement des outils** : utilise l'API haut niveau du SDK `mcp` 2.x (déclaration par décorateur, schéma déduit des annotations). La recherche du 2026-11-10 indique `from mcp.server import MCPServer` ; vérifie-le dans le README du SDK installé.
- **`serve`** : journaux sur stderr uniquement, puis `serve_stdio(load_settings())`.

- [ ] **Step 1 : écrire les tests qui échouent**

```python
def test_read_tools_on_empty_store(tools):
    assert tools.list_accounts() == {"accounts": [], "sync": {"last_success_at": None, "last_status": None, "auth_required": False}}
    assert tools.search_transactions()["total_count"] == 0
def test_search_returns_amounts_as_strings_and_whole_filter_totals(...)   # "amount": "-12.50" ; totaux sur tout le filtre
@pytest.mark.parametrize("kwargs, param", [({"date_from": "2026-13-01"}, "date_from"),
    ({"amount_min": 100, "amount_max": 50}, "amount_min"), ({"sort": "random"}, "sort"),
    ({"types": ["salaire"]}, "types")])
def test_invalid_inputs_raise_clear_errors(tools, kwargs, param)   # pytest.raises(ToolInputError, match=param)
def test_summarize_rejects_unknown_group_by(tools)                 # group_by="week"
def test_list_statements_rejects_bad_period(tools)                 # period_from="2026/10"
def test_limit_is_clamped_to_500(tools)                            # limit=1000 => réponse "limit" == 500
def test_sync_block_reports_auth_required(...)
def test_sync_now_reports_started_and_too_soon(...)
def test_submit_2fa_code_without_pending_run(...)                  # {"accepted": False, ...}
def test_read_statement_missing_file_suggests_sync(...)
# test_app.py, via le client MCP en mémoire du SDK (pas de sous-processus)
def test_app_lists_all_tools():                    # noms == set(TOOL_NAMES)
def test_app_search_roundtrip():                   # appel search_transactions => JSON avec "total_count"
# test_readme.py
def test_readme_documents_every_tool():            # chaque nom de TOOL_NAMES présent dans README.md
def test_readme_documents_every_cli_command():     # "bourso-mcp login|sync|status|serve", "--scope", "--force", "--non-interactive", "--dry-run"
def test_readme_mentions_pinned_woob_commit():     # git -C vendor/woob rev-parse HEAD présent dans README.md
```

- [ ] **Step 2 : lancer `uv run pytest tests/server tests/test_readme.py -v`**

Résultat attendu : ÉCHEC.

- [ ] **Step 3 : implémenter `tools.py`, `app.py` et `serve`**

- [ ] **Step 4 : mettre à jour le README**

La section « Lancement du MCP » contient :
- la configuration Claude Desktop, dans `%APPDATA%\Claude\claude_desktop_config.json` :

  ```json
  {"mcpServers": {"bourso": {"command": "uv", "args": ["--directory", "D:\\work\\repos\\bourso-mcp", "run", "bourso-mcp", "serve"]}}}
  ```

- la commande Claude Code :

  ```bash
  claude mcp add bourso -- uv --directory D:\work\repos\bourso-mcp run bourso-mcp serve
  ```

- un rappel : relancer Claude Desktop après modification.

- [ ] **Step 5 : lancer `uv run pytest -v` et `uv run ruff check .`**

Résultat attendu : tous les tests passent et ruff ne signale rien.

- [ ] **Step 6 : commit**

```bash
git add src tests README.md
git commit -m "ajout du serveur mcp et de ses outils" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13 : synchro planifiée, vérification de bout en bout, README final

**Files :**
- Create : `scripts/register-sync-task.ps1`
- Modify : `README.md` (sections « Synchro planifiée », « Vérification manuelle », « Dépannage »)

**Interfaces :**
- Consumes : `bourso-mcp sync --non-interactive` (tâche 11), `serve` (tâche 12).

- [ ] **Step 1 : écrire `scripts/register-sync-task.ps1`**

- Paramètres : `-At "07:30"` (par défaut) et `-Unregister`.
- Il enregistre la tâche `bourso-mcp sync`, quotidienne, avec `Register-ScheduledTask` :
  - action `(Get-Command uv).Source` avec les arguments `--directory "<racine du dépôt>" run bourso-mcp sync --non-interactive` ;
  - `New-ScheduledTaskSettingsSet -StartWhenAvailable`.
- Elle s'exécute sous l'utilisateur courant, seulement quand il est connecté, pour garder l'accès au Gestionnaire d'identification Windows.
- `-Unregister` supprime la tâche.

- [ ] **Step 2 : vérifier la syntaxe**

```powershell
powershell -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw scripts/register-sync-task.ps1)); 'ok'"
```

Résultat attendu : `ok`.

- [ ] **Step 3 : compléter le README**

- **Synchro planifiée** : enregistrer la tâche, la lancer à la main (`Start-ScheduledTask -TaskName "bourso-mcp sync"`), la supprimer.
- **Dépannage** : un tableau des statuts (`auth_required`, `auth_failed`, `error` lié à `SourceBroken`, `partial`), avec leur cause et l'action à faire, et les codes de sortie de la CLI.
- **Vérification manuelle** : les étapes 4 à 8 ci-dessous, à refaire à chaque montée de version de woob.

- [ ] **Step 4 : lancer `uv run pytest -v` et `uv run ruff check .`, puis commit**

```bash
git add scripts README.md
git commit -m "ajout de la synchro planifiée et de la procédure de vérification" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 5 : POINT D'ARRÊT, l'utilisateur fait la vérification de bout en bout**

L'exécutant s'arrête et transmet la procédure à l'utilisateur :
1. `uv run bourso-mcp login` : identifiants, 2FA, premier passage `ok` ou `partial` détaillé.
2. `uv run bourso-mcp sync --dry-run` : volumes plausibles par compte, y compris le compte joint et la carte.
3. `uv run bourso-mcp status`.
4. Claude Desktop configuré, puis trois questions à poser :
   - « liste mes comptes » ;
   - « combien ai-je dépensé par carte en octobre 2026 ? », à comparer avec le site ;
   - « lis mon relevé de septembre 2026 », à comparer avec le PDF.
5. `scripts/register-sync-task.ps1`, puis `Start-ScheduledTask -TaskName "bourso-mcp sync"`, puis `uv run bourso-mcp status`.

Les écarts constatés deviennent des corrections, chacune avec son test, avant de clore le plan.
