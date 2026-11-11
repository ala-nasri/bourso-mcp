from __future__ import annotations

from datetime import datetime

import pytest
from filelock import FileLock

from bourso_mcp.cli import main as cli_main
from bourso_mcp.cli import wiring
from bourso_mcp.source.errors import SourceBroken
from bourso_mcp.store.repository import Repository
from tests.factories import make_account, make_tx
from tests.fakes import FakeBankSource


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOURSO_MCP_HOME", str(tmp_path))
    return tmp_path


def use_source(monkeypatch, source):
    monkeypatch.setattr(wiring, "source_factory", lambda settings: (lambda: source))


def no_input(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("saisie interdite")

    monkeypatch.setattr("builtins.input", fail)
    monkeypatch.setattr("getpass.getpass", fail)


def answer_prompts(monkeypatch, reply):
    """Remplace la saisie du gestionnaire interactif (`input` y est lié à la définition)."""
    real = cli_main.interactive_handler
    monkeypatch.setattr(cli_main, "interactive_handler", lambda: real(prompt=reply))


def finished_run(home, status="ok", error_type=None):
    repo = Repository(home / "bourso.db")
    t = datetime.now().astimezone()
    rid = repo.start_run("cli", "all", t)
    repo.finish_run(rid, status, t, 2, 1, error_type, None)
    return repo


def test_sync_prints_summary_and_exit_code_0(capsys, home, monkeypatch):
    src = FakeBankSource(
        [make_account("cc1")], history={"cc1": [make_tx(raw="A"), make_tx(raw="B", amount=-5)]}
    )
    use_source(monkeypatch, src)
    assert cli_main.main(["sync", "--force"]) == 0
    assert "ok : 2 transactions et 0 relevé ajoutés" in capsys.readouterr().out


def test_sync_non_interactive_with_2fa_exits_2_without_prompting(capsys, home, monkeypatch):
    no_input(monkeypatch)
    use_source(monkeypatch, FakeBankSource([make_account("cc1")], two_factor="sms"))
    assert cli_main.main(["sync", "--non-interactive"]) == 2
    assert "bourso-mcp login" in capsys.readouterr().out
    assert Repository(home / "bourso.db").last_finished_run().trigger == "schedule"


def test_sync_too_soon_exits_3_and_force_bypasses(capsys, home, monkeypatch):
    finished_run(home)
    use_source(monkeypatch, FakeBankSource([make_account("cc1")]))
    assert cli_main.main(["sync"]) == 3
    assert "--force" in capsys.readouterr().out
    assert cli_main.main(["sync", "--force"]) == 0


def test_sync_already_running_exits_3(capsys, home, monkeypatch):
    use_source(monkeypatch, FakeBankSource([make_account("cc1")]))
    with FileLock(home / "sync.lock", timeout=0):
        assert cli_main.main(["sync", "--force"]) == 3
        assert cli_main.main(["sync", "--dry-run"]) == 3
    assert "déjà en cours" in capsys.readouterr().out


def test_sync_dry_run_writes_nothing(capsys, home, monkeypatch):
    src = FakeBankSource([make_account("cc1")], history={"cc1": [make_tx()]})
    use_source(monkeypatch, src)
    assert cli_main.main(["sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "cc1" in out and "1 opérations" in out
    repo = Repository(home / "bourso.db")
    assert repo.last_finished_run() is None
    assert repo.list_accounts() == []


def test_sync_dry_run_source_errors(capsys, home, monkeypatch):
    use_source(monkeypatch, FakeBankSource([], login_error=SourceBroken("page changée")))
    assert cli_main.main(["sync", "--dry-run"]) == 1
    no_input(monkeypatch)
    use_source(monkeypatch, FakeBankSource([], two_factor="sms"))
    assert cli_main.main(["sync", "--dry-run", "--non-interactive"]) == 2


def test_partial_lists_failed_accounts(capsys, home, monkeypatch):
    src = FakeBankSource(
        [make_account("cc1"), make_account("cc2")], account_errors={"cc2": SourceBroken("x")}
    )
    use_source(monkeypatch, src)
    assert cli_main.main(["sync"]) == 1
    out = capsys.readouterr().out
    assert "partial" in out and "cc2 : SourceBroken" in out


def test_login_runs_forced_interactive_sync(capsys, home, monkeypatch):
    finished_run(home)  # dernier succès il y a moins d'une heure
    use_source(monkeypatch, FakeBankSource([make_account("cc1")], two_factor="sms"))
    answer_prompts(monkeypatch, lambda prompt: "123456")
    assert cli_main.main(["login"]) == 0
    assert "ok :" in capsys.readouterr().out


def test_login_never_prompts_for_credentials(capsys, home, monkeypatch):
    def fail(*a, **k):
        raise AssertionError("identifiants demandés")

    monkeypatch.setattr("getpass.getpass", fail)
    prompts = []

    def fake_input(prompt):
        prompts.append(prompt)
        return "123456"

    answer_prompts(monkeypatch, fake_input)
    use_source(monkeypatch, FakeBankSource([make_account("cc1")], two_factor="sms"))
    assert cli_main.main(["login"]) == 0
    assert prompts == ["Code reçu par SMS : "]


def test_status_output(capsys, home):
    finished_run(home, status="partial", error_type="SourceBroken")
    repo = Repository(home / "bourso.db")
    rid = repo.start_run("cli", "all", datetime.now().astimezone())
    repo.set_run_status(rid, "awaiting_2fa", "sms")
    assert cli_main.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "awaiting_2fa, méthode sms" in out
    assert "partial" in out and "SourceBroken" in out
    assert "Dernière réussite : 20" in out


def test_status_empty(capsys, home):
    assert cli_main.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "aucune synchronisation" in out and "Dernier passage : aucun" in out


def test_missing_credentials_message(capsys, home, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("source woob construite")

    monkeypatch.setattr(wiring, "WoobBoursoSource", forbidden)
    no_input(monkeypatch)
    assert cli_main.main(["sync", "--force"]) == 2
    out = capsys.readouterr().out
    assert "cmdkey /generic:bourso-mcp" in out and "Configuration" in out


def test_keyboard_interrupt_exits_130(capsys, home, monkeypatch):
    def boom(prompt):
        raise KeyboardInterrupt

    answer_prompts(monkeypatch, boom)
    use_source(monkeypatch, FakeBankSource([make_account("cc1")], two_factor="sms"))
    assert cli_main.main(["login"]) == 130
    assert Repository(home / "bourso.db").last_finished_run().status == "error"
    assert "Traceback" not in capsys.readouterr().out
