"""Spike de faisabilité woob / boursorama (script jetable, supprimé à la tâche 10).

ATTENTION : ce script se connecte à la VRAIE banque.

- Les identifiants viennent uniquement de l'entrée générique « bourso-mcp » du Gestionnaire
  d'identification Windows (lecture seule : rien n'y est créé, modifié ni supprimé). Ils ne sont
  jamais affichés, journalisés ni écrits (par précaution, toute occurrence de leur valeur dans la
  sortie est remplacée par ***).
- Il charge woob depuis vendor/woob, ouvre une session, gère la 2FA de façon interactive (les
  codes se saisissent au clavier), lit les comptes, les opérations des derniers jours et les
  relevés, puis télécharge le dernier relevé. Aucun virement, aucun bénéficiaire.
- Tout ce qu'il écrit est sous %TEMP%\\bourso-spike : le stockage woob (EN CLAIR : cookies de
  session) et le dernier relevé. À supprimer après le spike.

La sortie contient des données personnelles (libellés de comptes et de relevés). Les lignes
repérées « (a) » à « (k) » répondent aux questions de la tâche 2 ; le bloc final RÉCAPITULATIF
les regroupe SANS libellé ni montant : c'est lui qu'on peut partager.

Usage :
    uv run python scripts/spike_woob.py
    uv run python scripts/spike_woob.py --answer code    (reprise d'une 2FA SMS laissée en attente)
"""

# Script de diagnostic : afficher la classe et le message de n'importe quelle exception est le but.
# ruff: noqa: BLE001
from __future__ import annotations

import argparse
import collections
import datetime as dt
import getpass
import importlib.metadata as importlib_metadata
import inspect
import itertools
import json
import logging
import os
import re
import sys
import tempfile
import traceback
import unicodedata
from pathlib import Path
from typing import Any

import keyring
import woob
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from pypdf import PdfReader
from woob.capabilities.bank.base import AccountType, TransactionType
from woob.capabilities.base import empty
from woob.capabilities.bill import DocumentTypes
from woob.core import WoobBase
from woob.exceptions import (
    BrowserIncorrectPassword,
    BrowserInteraction,
    BrowserQuestion,
    DecoupledValidation,
)
from woob.tools.config.yamlconfig import YamlConfig
from woob.tools.storage import IStorage, StandardStorage

SERVICE = "bourso-mcp"
MODULE_NAME = "boursorama"
ROOT = Path(__file__).resolve().parents[1]
MODULES_DIR = ROOT / "vendor" / "woob" / "modules"
WORK_DIR = Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "bourso-spike"
STORAGE_PATH = WORK_DIR / "storage"
STATEMENT_PATH = WORK_DIR / "dernier-releve.pdf"

# Champs de configuration woob qui portent la réponse à une 2FA (AUTHENTICATION_METHODS).
ANSWER_FIELDS = ("code", "email_code", "resume")
STOP_AFTER_OLDER = 3  # arrêt de la lecture après 3 opérations anciennes d'affilée
MAX_2FA_ROUNDS = 5  # SMS puis e-mail = 2 tours ; marge pour un nouvel essai
# Types de comptes dont on lit l'historique (le reste est hors périmètre : épargne, bourse...).
HISTORY_TYPES = {AccountType.CHECKING, AccountType.JOINT, AccountType.CARD, AccountType.UNKNOWN}
FR_MONTHS = "janvier fevrier mars avril mai juin juillet aout septembre octobre novembre decembre"

WARNING = (
    "ATTENTION : cette sortie contient des données personnelles (libellés, dates) : masquez-les "
    "avant de la partager, ou ne partagez que le bloc RÉCAPITULATIF final."
)

_SECRETS: list[str] = []
FACTS: list[tuple[str, str]] = []
INTERACTIONS: list[str] = []  # méthode de chaque interaction 2FA rencontrée
FAILURES: list[str] = []
IMPORT_ERRORS: list[str] = []


class Abandon(Exception):
    """L'utilisateur a laissé une réponse 2FA vide : on s'arrête proprement."""


# --------------------------------------------------------------------------------------------
# Sortie
# --------------------------------------------------------------------------------------------


def scrub(text: str) -> str:
    """Remplace par *** toute occurrence d'un identifiant ou d'un mot de passe (précaution)."""
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    # Clavier virtuel de boursorama : un KeyError('<sha256>') désigne un chiffre du mot de passe.
    return re.sub(r"\b[0-9a-fA-F]{64}\b", "<sha256>", text)


def out(text: str = "") -> None:
    print(scrub(text), flush=True)


def section(title: str) -> None:
    out(f"\n=== {title} ===")


def note(tag: str, text: str) -> None:
    """Ligne qui répond à la question `tag` ; reprise sans libellé dans le récapitulatif."""
    FACTS.append((tag, text))
    out(f"  ({tag}) {text}")


class _ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        if record.exc_info:  # pas de trace brute : elle ne passerait pas par scrub
            record.msg += " | " + scrub(describe_exception(record.exc_info[1]))
            record.exc_info = record.exc_text = None
        record.args = ()
        return True


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")  # jamais d'exception d'encodage à l'affichage
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("  [woob %(name)s] %(levelname)s: %(message)s"))
    handler.addFilter(_ScrubFilter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)


def describe_exception(exc: BaseException) -> str:
    cls = type(exc)
    prefix = "" if cls.__module__ == "builtins" else f"{cls.__module__}."
    text = f"{prefix}{cls.__qualname__}: {exc}"
    extras = [
        f"{attr}={getattr(exc, attr)!r}"
        for attr in ("bad_fields", "action_type", "url")
        if getattr(exc, attr, None) not in (None, "", [], ())
    ]
    return text + (f" [{', '.join(extras)}]" if extras else "")


def report_failure(what: str, exc: BaseException, trace: bool) -> None:
    """À appeler dans un bloc `except` : affiche classe et message, note l'échec."""
    out(f"  ÉCHEC ({what}) : {describe_exception(exc)}")
    if isinstance(exc, BrowserIncorrectPassword):
        out("  ATTENTION : ne relancez pas le script avant d'avoir vérifié l'entrée `cmdkey`")
        out("  (cmdkey /list:bourso-mcp) : chaque lancement est une tentative de connexion et")
        out("  compte pour le verrouillage du compte BoursoBank.")
    FAILURES.append(f"{what} : {type(exc).__name__}")
    if isinstance(exc, ImportError) or isinstance(exc.__cause__, ImportError):
        IMPORT_ERRORS.append(f"{what} : {exc}")
    if trace:
        out(traceback.format_exc())
        return
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        out(
            f"    à {Path(frames[-1].filename).name}:{frames[-1].lineno} (--trace : trace complète)"
        )


def oui(flag: bool) -> str:
    return "oui" if flag else "non"


# --------------------------------------------------------------------------------------------
# Formatage : formes et longueurs, jamais de valeur d'identifiant
# --------------------------------------------------------------------------------------------


def filled(value: Any) -> bool:
    return not empty(value) and value != ""


def kind(value: Any) -> str:
    """Forme d'un identifiant, sans sa valeur : « chiffres(11) », « hex(32) »..."""
    if not filled(value):
        return "-"
    text = str(value)
    if re.fullmatch(r"[0-9a-f]{32}", text):
        return "hex(32)"
    if re.fullmatch(r"[0-9]+", text):
        return f"chiffres({len(text)})"
    if re.fullmatch(r"[0-9*]+", text):
        return f"chiffres+*({len(text)})"
    if re.fullmatch(r"[0-9A-Za-z]+", text):
        return f"alphanum({len(text)})"
    return f"texte({len(text)})"


def shape(value: Any) -> str:
    """Masque une valeur : chiffres -> 9, lettres -> a, le reste est conservé."""
    if not filled(value):
        return "-"
    return re.sub(r"[A-Za-z]", "a", re.sub(r"[0-9]", "9", str(value)))[:30]


def enum_name(enum_cls: Any, value: Any) -> str:
    """Nom d'une constante des Enum maison de woob (valeurs entières)."""
    if empty(value):
        return "-"
    for name, member in enum_cls._items:
        if member == value:
            return str(name)
    return f"?{value!r}"


def as_date(value: Any) -> dt.date | None:
    if empty(value) or not isinstance(value, dt.date):
        return None
    return dt.date(value.year, value.month, value.day)


def fmt_date(value: Any) -> str:
    day = as_date(value)
    return day.isoformat() if day else "-"


def same(a: Any, b: Any) -> bool:
    """Égalité de deux identifiants, « * » valant joker (numéros de carte masqués)."""
    if not filled(a) or not filled(b):
        return False
    a, b = str(a), str(b)
    return len(a) == len(b) and all(x == y or "*" in (x, y) for x, y in zip(a, b, strict=True))


def counts(counter: collections.Counter[str]) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(counter.items())) or "aucun"


# --------------------------------------------------------------------------------------------
# (j) Stockage : le stockage standard de woob, enveloppé pour observer ce que le module y fait
# --------------------------------------------------------------------------------------------


class SpyStorage(StandardStorage):
    """StandardStorage (YAML en clair) qui note chaque appel : méthode, chemin, type de valeur."""

    def __init__(self, path: str) -> None:
        self.calls: collections.Counter[str] = collections.Counter()
        super().__init__(path)

    def _log(self, method: str, path: tuple[Any, ...], value: Any = None) -> None:
        text = f"{method} " + "/".join(str(p) for p in path)
        if value is not None:
            text += f" [valeur : {type(value).__name__}]"
        self.calls[text] += 1

    def load(self, what: str, name: str, default: Any = None) -> None:
        self._log("load", (what, name))
        return super().load(what, name, {} if default is None else default)

    def save(self, what: str, name: str) -> None:
        self._log("save", (what, name))
        return super().save(what, name)

    def set(self, what: str, name: str, *args: Any) -> None:
        self._log("set", (what, name, *args[:-1]), args[-1])
        return super().set(what, name, *args)

    def delete(self, what: str, name: str, *args: Any) -> None:
        self._log("delete", (what, name, *args))
        return super().delete(what, name, *args)

    def get(self, what: str, name: str, *args: Any, **kwargs: Any) -> Any:
        self._log("get", (what, name, *args))
        return super().get(what, name, *args, **kwargs)

    def saved_state(self) -> dict[str, Any]:
        """Lecture directe (sans appel noté) de l'état du navigateur du module."""
        try:
            state = self.config.values["backends"][MODULE_NAME]["browser_state"]
        except (KeyError, TypeError):
            return {}
        return state if isinstance(state, dict) else {}


def short_signature(function: Any) -> str:
    parts = []
    for param in list(inspect.signature(function).parameters.values())[1:]:
        prefix = {param.VAR_POSITIONAL: "*", param.VAR_KEYWORD: "**"}.get(param.kind, "")
        parts.append(prefix + param.name + ("=..." if param.default is not param.empty else ""))
    return f"{function.__name__}({', '.join(parts)})"


def describe_value(value: Any) -> str:
    if isinstance(value, str):
        return f"str({len(value)} car.)"
    if isinstance(value, dict):
        inner = ", ".join(f"{k}:{type(v).__name__}" for k, v in sorted(value.items(), key=str))
        return "dict{" + inner + "}"
    if isinstance(value, list | tuple):
        return f"{type(value).__name__}[{len(value)}]"
    return type(value).__name__


def json_ok(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def describe_saved_state(storage: SpyStorage) -> None:
    state = storage.saved_state()
    if not state:
        note("c", "état de session sauvegardé au démarrage : absent (1er lancement ou état effacé)")
        return
    note(
        "c",
        f"état de session sauvegardé au démarrage : présent ({len(state)} clés ; "
        f"cookies : {oui(bool(state.get('cookies')))} ; "
        f"twofa_logged_date : {state.get('twofa_logged_date', '-')} ; "
        f"expire : {state.get('expire', '-')})",
    )


def report_storage(app: WoobBase, storage: SpyStorage) -> None:
    section("(j) STOCKAGE WOOB")
    module = app.modules_loader.loaded.get(MODULE_NAME)
    browser_class = getattr(getattr(module, "klass", None), "BROWSER", None)
    names = ("load", "save", "get", "set", "delete")
    signatures = " ; ".join(short_signature(getattr(IStorage, n)) for n in names)
    note("j", f"interface IStorage : {signatures}")
    note("j", "côté backend : BackendStorage(name, storage) préfixe tout par ('backends', <nom>)")
    note("j", "implémentation standard : StandardStorage(path) -> YamlConfig, YAML en clair")
    calls = " | ".join(f"{text} x{n}" for text, n in sorted(storage.calls.items()))
    note("j", f"appels observés : {calls}")
    declared = ", ".join(getattr(browser_class, "__states__", ()))
    note("j", f"clés déclarées par le navigateur du module (__states__) : {declared}")
    state = storage.saved_state()
    if not state:
        note("j", "browser_state : aucune clé écrite pendant ce lancement")
    for key, value in sorted(state.items()):
        note("j", f"browser_state[{key}] : {describe_value(value)} ; JSON : {oui(json_ok(value))}")
    if state:
        note("j", f"browser_state entier sérialisable en JSON : {oui(json_ok(state))}")
    try:
        reloaded = YamlConfig(str(STORAGE_PATH))
        reloaded.load()
    except Exception as exc:
        note(
            "j", f"relecture du fichier avec le chargeur YAML sûr : NON ({describe_exception(exc)})"
        )
    else:
        identical = reloaded.values == storage.config.values
        note(
            "j",
            f"relecture du fichier avec le chargeur YAML sûr : oui ; identique : {oui(identical)}",
        )


# --------------------------------------------------------------------------------------------
# (k) Identifiants : lecture seule du Gestionnaire d'identification
# --------------------------------------------------------------------------------------------


def read_credentials() -> tuple[str, str] | None:
    """Renvoie (identifiant, mot de passe), ou None (message affiché) si l'entrée est inutilisable."""
    section("(k) IDENTIFIANTS (Gestionnaire d'identification, lecture seule)")
    try:
        backend_name = type(keyring.get_keyring()).__name__
        credential = keyring.get_credential(SERVICE, None)
    except Exception as exc:
        out(f"  Lecture impossible : {describe_exception(exc)}")
        return None
    login = getattr(credential, "username", None) or ""
    password = getattr(credential, "password", None) or ""
    note(
        "k",
        f"keyring.get_credential('{SERVICE}', None) via {backend_name} : "
        f"identifiant lu : {oui(bool(login))}, mot de passe lu : {oui(bool(password))}",
    )
    if not login or not password:
        out("  Entrée absente ou incomplète. Créez-la vous-même (cmdkey demande le mot de passe) :")
        out(f"    cmdkey /generic:{SERVICE} /user:<identifiant> /pass")
        return None
    _SECRETS.extend(s for s in (login, password) if len(s) >= 6)
    note("k", f"identifiant conforme au format woob (chiffres seulement) : {oui(login.isdigit())}")
    return login, password


# --------------------------------------------------------------------------------------------
# (b)(c) 2FA interactive
# --------------------------------------------------------------------------------------------


def record_interaction(exc: BrowserInteraction) -> None:
    lineage = " < ".join(
        c.__name__ for c in type(exc).__mro__ if c not in (object, BaseException, Exception)
    )
    medium = getattr(exc, "medium_type", None)
    if isinstance(exc, BrowserQuestion):
        fields = ", ".join(field.id for field in exc.fields)
        message = getattr(exc, "message", "") or "; ".join(str(f.label) for f in exc.fields)
        resume = f"config[<champ>].set(<code saisi>) puis nouvel appel ; champs : {fields}"
    else:
        message = getattr(exc, "message", "")
        resume = "config['resume'].set(True) puis nouvel appel (woob sonde la validation, 10 min)"
    INTERACTIONS.append(
        {"sms": "sms", "email": "email", "mobile_app": "app"}.get(medium, str(medium))
    )
    note(
        "b",
        f"interaction {len(INTERACTIONS)} : {lineage} ; medium_type={medium} ; "
        f"message={message!r} ; reprise : {resume}",
    )


def call_with_2fa(backend: Any, function: Any) -> Any:
    """Appelle `function` ; sur une 2FA, demande la réponse, l'injecte et recommence."""
    for _ in range(MAX_2FA_ROUNDS):
        try:
            return function()
        except BrowserQuestion as exc:
            record_interaction(exc)
            for field in exc.fields:
                answer = getpass.getpass(f"  {field.label or field.id} (saisie masquée) : ").strip()
                if not answer:
                    raise Abandon(f"réponse vide pour « {field.id} »") from exc
                backend.config[field.id].set(answer)
        except DecoupledValidation as exc:
            record_interaction(exc)
            out(f"  {exc.message}")
            out("  Validez dans l'application BoursoBank (attente jusqu'à 10 minutes)...")
            backend.config["resume"].set(True)
    raise RuntimeError(f"plus de {MAX_2FA_ROUNDS} interactions 2FA consécutives")


def preset_answers(backend: Any, fields: list[str]) -> None:
    """--answer : champ renseigné avant la première tentative (reprise d'une 2FA en attente)."""
    for name in fields:
        if name == "resume":
            backend.config["resume"].set(True)
        else:
            answer = getpass.getpass(f"  {name} (saisie masquée) : ").strip()
            if not answer:
                raise Abandon(f"réponse vide pour « {name} »")
            backend.config[name].set(answer)
        out(f"  champ « {name} » renseigné avant la première tentative")


# --------------------------------------------------------------------------------------------
# (d)(e)(g)(h) Comptes et opérations
# --------------------------------------------------------------------------------------------


def list_accounts(backend: Any) -> list[Any]:
    section("COMPTES : connexion, 2FA éventuelle, liste")
    connected = False
    try:
        accounts = call_with_2fa(backend, lambda: list(backend.iter_accounts()))
        connected = True
    finally:
        browser = getattr(backend, "_browser", None)
        methods = getattr(browser, "AUTHENTICATION_METHODS", {})
        note(
            "b",
            f"navigateur : is_interactive={getattr(browser, 'is_interactive', '?')} ; "
            f"champs de reprise (AUTHENTICATION_METHODS) : {', '.join(methods)}",
        )
        seen = ", ".join(sorted(set(INTERACTIONS))) or "aucune"
        issue = "connexion réussie" if connected else "connexion NON aboutie"
        note(
            "c",
            f"méthode(s) 2FA observée(s) : {seen} ; interactions : {len(INTERACTIONS)} ; {issue}",
        )
    out(f"  {len(accounts)} compte(s) reçu(s)")
    return accounts


def history_window(backend: Any, account: Any, since: dt.date, max_ops: int) -> dict[str, Any]:
    """Lit iter_history jusqu'à `since` ; arrêt dès que l'ordre décroissant est établi."""
    iterator = backend.iter_history(account)
    kept: list[Any] = []
    seen = down = up = older_run = 0
    prev = first = last = None
    truncated = False
    try:
        while True:
            if seen >= max_ops:  # test avant de lire : pas de requête de détail en trop
                truncated = True
                break
            tr = next(iterator, None)
            if tr is None:
                break
            seen += 1
            day = as_date(tr.date)
            if day is None:
                kept.append(tr)
                continue
            first = first or day
            last = day
            if prev is not None and day < prev:
                down += 1
            elif prev is not None and day > prev:
                up += 1
            prev = day
            if day >= since:
                kept.append(tr)
                older_run = 0
            else:
                older_run += 1
                if older_run >= STOP_AFTER_OLDER:
                    break  # trop d'opérations anciennes d'affilée, quel que soit l'ordre
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()
    if down and not up:
        order = "décroissant (récent -> ancien)"
    elif up and not down:
        order = "croissant (ancien -> récent)"
    elif up and down:
        order = "mixte"
    else:
        order = "indéterminé (moins de 2 dates distinctes)"
    return {
        "kept": kept,
        "seen": seen,
        "order": order,
        "first": first,
        "last": last,
        "truncated": truncated,
    }


def sample_line(tr: Any) -> str:
    sign = "?" if empty(tr.amount) else ("+" if tr.amount >= 0 else "-")
    category = repr(tr.category[:25]) if filled(tr.category) else "-"
    return (
        f"type={enum_name(TransactionType, tr.type)} date={fmt_date(tr.date)} "
        f"rdate={fmt_date(tr.rdate)} vdate={fmt_date(tr.vdate)} bdate={fmt_date(tr.bdate)} "
        f"category={category} card={shape(tr.card)} coming={tr.coming} sens={sign}"
    )


def read_account(
    backend: Any, account: Any, alias: str, since: dt.date, args: argparse.Namespace
) -> collections.Counter[str]:
    window = history_window(backend, account, since, args.max_ops)
    kept = window["kept"]
    coming = list(itertools.islice(backend.iter_coming(account), args.max_ops))
    types = collections.Counter(enum_name(TransactionType, tr.type) for tr in kept)
    coming_types = collections.Counter(enum_name(TransactionType, tr.type) for tr in coming)
    ratios = " ".join(
        f"{name} {sum(filled(getattr(tr, name)) for tr in kept)}/{len(kept)}"
        for name in ("rdate", "vdate", "bdate", "category", "card")
    )
    cut = " (TRONQUÉ au plafond --max-ops)" if window["truncated"] else ""
    span = f"{fmt_date(window['first'])} -> {fmt_date(window['last'])}"
    out(
        f"    opérations {args.days} j : {len(kept)} (lues : {window['seen']}{cut}) ; lues de {span}"
    )
    out(f"    à venir : {len(coming)} ; types à venir : {counts(coming_types)}")
    out(f"    types ({args.days} j) : {counts(types)}")
    out(f"    champs renseignés : {ratios}")
    if kept:
        out(f"    exemple (la plus récente lue) : {sample_line(kept[0])}")
    summary = next((tr for tr in kept if tr.type == TransactionType.CARD_SUMMARY), None)
    if summary is not None:
        out(f"    exemple CARD_SUMMARY : {sample_line(summary)}")

    truncated = " (TRONQUÉ)" if window["truncated"] else ""
    note("h", f"{alias} : iter_history ordre {window['order']}{truncated}")
    note("g", f"{alias} : champs renseignés sur {len(kept)} opérations : {ratios}")
    note(
        "e",
        f"{alias} : {args.days} j = {len(kept)} ops ; à venir = {len(coming)} ; "
        f"types : {counts(types)} ; types à venir : {counts(coming_types)}",
    )
    return types


def report_accounts(backend: Any, accounts: list[Any], args: argparse.Namespace) -> None:
    section("(d)(e)(g)(h) COMPTES ET OPÉRATIONS")
    alias_of = {acc.id: f"A{n}" for n, acc in enumerate(accounts, 1)}
    since = dt.date.today() - dt.timedelta(days=args.days)
    types_by_alias: dict[str, collections.Counter[str]] = {}
    ownerships, cards, joint_hint = [], [], []
    for n, acc in enumerate(accounts, 1):
        alias = f"A{n}"
        type_name = enum_name(AccountType, acc.type)
        ownership = acc.ownership if filled(acc.ownership) else "-"
        parent = "-" if empty(acc.parent) else alias_of.get(getattr(acc.parent, "id", None), "?")
        out(
            f"\n  {alias}  {type_name}  ownership={ownership}  parent={parent}  « {str(acc.label)[:60]} »"
        )
        out(f"    id={kind(acc.id)} number={kind(acc.number)} iban={oui(filled(acc.iban))}")
        note("d", f"{alias} : type={type_name} ownership={ownership} parent={parent}")
        ownerships.append(f"{alias}={ownership}")
        if acc.type == AccountType.CARD:
            cards.append(f"{alias}->{parent}")
        if re.search(r"joint|commun", str(acc.label), re.IGNORECASE):
            joint_hint.append(alias)
        if acc.type not in HISTORY_TYPES:
            out("    historique non lu (type hors périmètre)")
            continue
        try:
            types_by_alias[alias] = read_account(backend, acc, alias, since, args)
        except Exception as exc:
            report_failure(f"historique {alias}", exc, args.trace)
    hint = ", ".join(joint_hint) or "aucun"
    note("d", f"ownership par compte : {' '.join(ownerships)} ; libellé avec joint/commun : {hint}")
    note("e", f"comptes de type CARD (carte -> parent) : {' '.join(cards) or 'aucun'}")
    summaries = {a: c["CARD_SUMMARY"] for a, c in types_by_alias.items() if c["CARD_SUMMARY"]}
    note("e", f"lignes CARD_SUMMARY dans la fenêtre : {summaries or 'aucune'}")


# --------------------------------------------------------------------------------------------
# (f) Subscriptions, documents, dernier relevé
# --------------------------------------------------------------------------------------------


def label_month_offset(label: str, day: dt.date) -> str:
    """Décalage en mois entre `day` et le mois cité dans le libellé (« +1 » : mois suivant)."""
    months = FR_MONTHS.split()
    text = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode().lower()
    match = re.search(rf"\b({'|'.join(months)})\b(?:\s+(\d{{4}}))?", text)
    if match:
        month, year = months.index(match.group(1)) + 1, match.group(2)
    else:
        match = re.search(r"\b(\d{1,2})[/-](\d{4})\b", text)
        if not match or not 1 <= int(match.group(1)) <= 12:
            return "sans mois"
        month, year = int(match.group(1)), match.group(2)
    if year:
        delta = (day.year * 12 + day.month) - (int(year) * 12 + month)
    else:
        delta = (day.month - month + 6) % 12 - 6
    return f"{delta:+d}"


def link_to_accounts(sub: Any, accounts: list[Any]) -> str:
    key = getattr(sub, "_account_key", None)
    links = []
    for n, acc in enumerate(accounts, 1):
        iban = str(acc.iban).replace(" ", "") if filled(acc.iban) else ""
        checks = {
            "id==id": same(sub.id, acc.id),
            "id==number": same(sub.id, acc.number),
            "id dans iban": bool(filled(sub.id) and iban and str(sub.id) in iban),
            "clé==_webid": same(key, getattr(acc, "_webid", None)),
            "clé==id": same(key, acc.id),
        }
        hits = [name for name, hit in checks.items() if hit]
        if hits:
            links.append(f"A{n}({', '.join(hits)})")
    return " ".join(links) or "aucun lien direct"


def header_date_offsets(text: str, reference: dt.date | None) -> list[int]:
    """Écarts (en jours) entre `reference` et les dates des 40 premières lignes du relevé."""
    if reference is None:
        return []
    header = "\n".join([line for line in text.splitlines() if line.strip()][:40])
    offsets = set()
    for match in re.finditer(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", header):
        try:
            found = dt.date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            continue
        offsets.add((found - reference).days)
    return sorted(offsets)


def mask_line(line: str) -> str:
    line = re.sub(r"\d+[.,]\d{2}\b", "<montant>", line)
    return re.sub(r"\d{6,}", "#", line.strip())[:110]


def check_statement(backend: Any, document: Any, trace: bool) -> None:
    data = backend.download_document(document)
    if not data:
        raise RuntimeError("download_document n'a renvoyé aucun contenu")
    STATEMENT_PATH.write_bytes(data)
    note(
        "f",
        f"téléchargement du dernier relevé : {len(data)} octets ; en-tête %PDF : "
        f"{oui(data[:5] == b'%PDF-')} ; fichier : <TEMP>/bourso-spike/{STATEMENT_PATH.name}",
    )
    try:
        reader = PdfReader(str(STATEMENT_PATH))
        if reader.is_encrypted:
            note(
                "f", f"PDF chiffré ; déchiffrement avec mot de passe vide : {reader.decrypt('')!r}"
            )
        text = reader.pages[0].extract_text() or ""
    except Exception as exc:
        report_failure("pypdf", exc, trace)
        return
    note(
        "f",
        f"pypdf : {len(reader.pages)} page(s) ; texte extrait de la page 1 : {len(text)} "
        f"caractères ({oui(len(text) > 20)})",
    )
    offsets = header_date_offsets(text, as_date(document.date))
    note("f", f"dates de l'en-tête (40 premières lignes), en jours par rapport à date : {offsets}")
    hint = re.compile(r"(?i)p[ée]riode|relev[ée]|arr[êe]t[ée]|[ée]dit[ée]|\bdu\b.*\bau\b")
    for line in [mask_line(ln) for ln in text.splitlines()[:60] if hint.search(ln)][:6]:
        out(f"    en-tête : {line}")  # texte libre : hors récapitulatif


def report_documents(backend: Any, accounts: list[Any], args: argparse.Namespace) -> None:
    section("(f) SUBSCRIPTIONS ET DOCUMENTS")
    subs = call_with_2fa(backend, lambda: list(backend.iter_subscription()))
    out(f"  {len(subs)} subscription(s)")
    statements: list[tuple[str, Any]] = []
    all_types: collections.Counter[str] = collections.Counter()
    for n, sub in enumerate(subs, 1):
        tag = f"S{n}"
        out(
            f"\n  {tag}  id={kind(sub.id)} clé={kind(getattr(sub, '_account_key', None))} "
            f"« {str(sub.label)[:60]} »"
        )
        note("f", f"{tag} : lien avec les comptes : {link_to_accounts(sub, accounts)}")
        try:
            docs = list(backend.iter_documents(sub))
        except Exception as exc:
            report_failure(f"documents {tag}", exc, args.trace)
            continue
        types = collections.Counter(str(d.type) for d in docs)
        formats = collections.Counter(str(d.format) for d in docs)
        dated = [d for d in docs if as_date(d.date)]
        dates = [as_date(d.date) for d in dated]
        day_counts = collections.Counter(day.day for day in dates)
        offsets = collections.Counter(
            label_month_offset(str(d.label), as_date(d.date)) for d in dated
        )
        down = sum(a > b for a, b in itertools.pairwise(dates))
        up = sum(a < b for a, b in itertools.pairwise(dates))
        order = "décroissant" if down and not up else "croissant" if up and not down else "mixte/NA"
        all_types.update(types)
        id_form = sum(str(d.id).startswith(f"{sub.id}_") for d in docs)
        note(
            "f",
            f"{tag} : Document.id de la forme <subscription.id>_<suffixe> : {id_form}/{len(docs)}",
        )
        note(
            "f",
            f"{tag} : {len(docs)} documents ; types : {counts(types)} ; "
            f"formats : {counts(formats)} ; ordre des dates : {order}",
        )
        note(
            "f",
            f"{tag} : jour du mois de date : {dict(sorted(day_counts.items()))} ; "
            f"décalage mois(date) - mois(libellé) : {dict(sorted(offsets.items()))}",
        )
        for doc in docs[:3]:
            doc_id = str(doc.id).replace(str(sub.id), "<sub>")
            out(
                f"    id={doc_id} date={fmt_date(doc.date)} type={doc.type} "
                f"format={doc.format} « {str(doc.label)[:50]} »"
            )
        statements.extend((tag, d) for d in dated if d.type == DocumentTypes.STATEMENT and d.url)
    note(
        "f",
        f"valeurs de Document.type vues : {counts(all_types)} "
        f"(constantes : STATEMENT={DocumentTypes.STATEMENT!r}, RIB={DocumentTypes.RIB!r})",
    )
    if not statements:
        raise RuntimeError("aucun relevé (type statement, daté, avec url) à télécharger")
    tag, latest = max(statements, key=lambda item: as_date(item[1].date))
    out(
        f"\n  dernier relevé : {tag} date={fmt_date(latest.date)} format={latest.format} « {latest.label} »"
    )
    note(
        "f",
        f"dernier relevé : {tag} ; date={fmt_date(latest.date)} ; type={latest.type} ; "
        f"format={latest.format}",
    )
    check_statement(backend, latest, args.trace)


# --------------------------------------------------------------------------------------------
# (i) Dépendances ; récapitulatif ; programme principal
# --------------------------------------------------------------------------------------------


def requirement_closure(roots: list[str]) -> set[str]:
    """Distributions installées atteignables depuis `roots` (marqueurs et « extras » évalués)."""
    seen: set[tuple[str, frozenset[str]]] = set()
    names: set[str] = set()
    todo = [(root, frozenset[str]()) for root in roots]
    while todo:
        raw_name, extras = todo.pop()
        name = canonicalize_name(raw_name)
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        try:
            dist = importlib_metadata.distribution(name)
        except importlib_metadata.PackageNotFoundError:
            continue
        names.add(name)
        for text in dist.requires or []:
            req = Requirement(text)
            if req.marker is None or any(req.marker.evaluate({"extra": e}) for e in extras or {""}):
                todo.append((req.name, frozenset(req.extras)))
    return names


def report_dependencies() -> None:
    section("(i) DÉPENDANCES")
    modules = {name.split(".")[0] for name in sys.modules if not name.startswith("_")}
    mapping = importlib_metadata.packages_distributions()
    imported = {
        canonicalize_name(dist)
        for module in modules - set(sys.stdlib_module_names)
        for dist in mapping.get(module, [])
    }
    outside = sorted(imported - requirement_closure(["woob", "bourso-mcp"]))
    out(f"  distributions tierces importées pendant le run : {', '.join(sorted(imported))}")
    note(
        "i",
        f"importées mais hors dépendances de woob et du projet : {', '.join(outside) or 'aucune'}",
    )
    missing = f" : {'; '.join(IMPORT_ERRORS)}" if IMPORT_ERRORS else ""
    note("i", f"ImportError pendant le run : {oui(bool(IMPORT_ERRORS))}{missing}")


def report_recap() -> None:
    section("RÉCAPITULATIF (sans libellé ni montant : partageable)")
    for tag in "abcdefghijk":
        for fact_tag, text in FACTS:
            if fact_tag == tag:
                out(f"({tag}) {text}")
    if FAILURES:
        out("ÉCHECS : " + " ; ".join(FAILURES))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spike woob / boursorama (lecture seule).")
    parser.add_argument("--days", type=int, default=30, help="fenêtre des opérations (défaut : 30)")
    parser.add_argument("--max-ops", type=int, default=120, help="plafond d'opérations lues/compte")
    parser.add_argument(
        "--answer",
        action="append",
        choices=ANSWER_FIELDS,
        default=[],
        help="renseigne un champ 2FA avant la 1re tentative (reprise d'une 2FA en attente)",
    )
    parser.add_argument(
        "--identity", help="libellé de l'identité à choisir si la banque en propose"
    )
    parser.add_argument(
        "--trace", action="store_true", help="affiche les traces d'erreur complètes"
    )
    return parser.parse_args(argv)


def run_session(
    app: WoobBase, storage: SpyStorage, login: str, password: str, args: argparse.Namespace
) -> None:
    """Charge le module, ouvre la session, lit les comptes puis les documents."""
    section("(a)(b) CHARGEMENT DE WOOB")
    params: dict[str, Any] = {"login": login, "password": password, "request_information": {}}
    if args.identity:
        params["identity"] = args.identity
    backend = app.load_backend(MODULE_NAME, MODULE_NAME, params, storage=storage)
    modules_dir = MODULES_DIR.relative_to(ROOT).as_posix()
    note(
        "a",
        f"WoobBase(modules_path=<{modules_dir}>) puis load_backend('{MODULE_NAME}', "
        f"'{MODULE_NAME}', params, storage=<StandardStorage>) ; woob {woob.__version__} ; "
        f"classe du module : {type(backend).__name__}",
    )
    note(
        "b",
        "2FA interactive : paramètre de config 'request_information' à une valeur non nulle "
        "({} ici) avant la création du navigateur (lu par TwoFactorBrowser.is_interactive)",
    )
    describe_saved_state(storage)
    if args.answer:
        preset_answers(backend, args.answer)

    accounts = list_accounts(backend)
    report_accounts(backend, accounts, args)
    try:
        report_documents(backend, accounts, args)
    except Abandon:
        raise
    except Exception as exc:
        report_failure("documents / relevé", exc, args.trace)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_output()
    out(WARNING)
    if not MODULES_DIR.is_dir():
        out(f"Dossier des modules introuvable : {MODULES_DIR.relative_to(ROOT)} (sous-module ?)")
        return 2
    credentials = read_credentials()
    if credentials is None:
        return 2

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    storage = SpyStorage(str(STORAGE_PATH))
    app = WoobBase(modules_path=str(MODULES_DIR))
    exit_code = 0
    try:
        run_session(app, storage, *credentials, args)
    except Abandon as exc:
        out(f"\n  Abandon : {exc}. État de session sauvegardé ; reprise : --answer <champ>.")
        exit_code = 3
    except KeyboardInterrupt:
        out("\n  Interrompu (Ctrl+C). État de session sauvegardé.")
        exit_code = 130
    except Exception as exc:
        report_failure("session woob", exc, args.trace)
        exit_code = 1
    finally:
        try:
            app.deinit()  # écrit browser_state dans le stockage (Module.dump_state)
        except Exception as exc:
            report_failure("sauvegarde de l'état", exc, args.trace)

    report_storage(app, storage)
    report_dependencies()
    report_recap()
    return exit_code or (1 if FAILURES else 0)


if __name__ == "__main__":
    sys.exit(main())
