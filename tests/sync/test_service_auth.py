from __future__ import annotations

import logging

import pytest

from bourso_mcp.source.errors import (
    AuthFailed,
    AuthMethodUnsupported,
    NetworkError,
    SourceBroken,
    TwoFactorExpired,
)
from bourso_mcp.sync.service import RETRY_DELAYS, SCHEDULE_LOGIN_RETRY_DELAYS, SyncService
from tests.factories import make_account, make_tx
from tests.fakes import FakeBankSource


def build(source, repo, settings, now, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return SyncService(lambda: source, repo, settings, now=lambda: now, sleep=sleeps.append)


def one_account(**kwargs):
    return FakeBankSource([make_account("cc1")], **kwargs)


def test_sms_two_factor_flow(repo, settings, now):
    src = one_account(two_factor="sms")
    seen = []

    def handler(method):
        run = repo.current_run()
        seen.append((method, run.status, run.twofa_method))
        return "123456"

    res = build(src, repo, settings, now).run("all", "cli", handler)
    assert seen == [("sms", "awaiting_2fa", "sms")]
    assert ("submit_2fa", "123456") in src.calls
    assert res.status == "ok"
    assert repo.last_finished_run().twofa_method is None


def test_app_validation_passes_none(repo, settings, now):
    src = one_account(two_factor="app")
    statuses = []
    original = src.submit_2fa

    def spy(code):
        statuses.append(repo.current_run().status)
        original(code)

    src.submit_2fa = spy
    res = build(src, repo, settings, now).run("all", "cli", lambda m: None)
    assert ("submit_2fa", None) in src.calls
    assert statuses == ["awaiting_2fa"]
    assert res.status == "ok"


def test_chained_sms_then_email_succeeds(repo, settings, now):
    src = one_account(two_factor="sms", then_methods=["email"])
    seen = []

    def handler(method):
        seen.append((method, repo.current_run().twofa_method))
        return "123456"

    res = build(src, repo, settings, now).run("all", "cli", handler)
    assert seen == [("sms", "sms"), ("email", "email")]
    assert res.status == "ok"


def test_chain_is_limited_to_three_steps(repo, settings, now):
    src = one_account(two_factor="sms", then_methods=["email", "app", "sms"])
    seen = []

    def handler(method):
        seen.append(method)
        return "123456" if method != "app" else None

    res = build(src, repo, settings, now).run("all", "cli", handler)
    assert seen == ["sms", "email", "app"]
    assert res.status == "auth_required"


def test_two_factor_expired_gives_auth_required(repo, settings, now):
    src = one_account(two_factor="sms")

    def handler(method):
        raise TwoFactorExpired("délai dépassé")

    res = build(src, repo, settings, now).run("all", "cli", handler)
    assert res.status == "auth_required"
    assert repo.last_finished_run().status == "auth_required"


def test_wrong_code_gives_auth_required(repo, settings, now):
    src = one_account(two_factor="sms")
    res = build(src, repo, settings, now).run("all", "cli", lambda m: "000000")
    assert res.status == "auth_required"
    assert src.calls.count(("submit_2fa", "000000")) == 1


def test_auth_failed_on_submit_gives_auth_required_without_retry(repo, settings, now):
    src = one_account(two_factor="sms", submit_error=AuthFailed("BrowserIncorrectPassword"))
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "cli", lambda m: "000000")
    assert res.status == "auth_required"
    assert "2FA" in res.error_message
    assert [c for c in src.calls if c[0] == "submit_2fa"] == [("submit_2fa", "000000")]
    assert sleeps == []


def test_network_error_on_submit_is_not_retried(repo, settings, now):
    src = one_account(two_factor="sms", submit_error=NetworkError("coupure"))
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "cli", lambda m: "123456")
    assert res.status == "error"
    assert [c for c in src.calls if c[0] == "submit_2fa"] == [("submit_2fa", "123456")]
    assert sleeps == []


def test_auth_failed_is_not_retried(repo, settings, now):
    src = one_account(login_error=AuthFailed("identifiants refusés"))
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "cli", lambda m: None)
    assert res.status == "auth_failed"
    assert src.calls.count(("login",)) == 1
    assert sleeps == []


def test_source_factory_auth_error_gives_auth_failed(repo, settings, now):
    sleeps = []

    def factory():
        raise AuthFailed("entrée bourso-mcp absente")

    svc = SyncService(factory, repo, settings, now=lambda: now, sleep=sleeps.append)
    res = svc.run("all", "cli", lambda m: None)
    assert res.status == "auth_failed"
    assert "entrée bourso-mcp absente" in res.error_message
    assert sleeps == []


def test_auth_method_unsupported_gives_error(repo, settings, now):
    src = one_account(login_error=AuthMethodUnsupported("QR code non géré"))
    res = build(src, repo, settings, now).run("all", "cli", lambda m: None)
    assert res.status == "error"
    assert res.error_type == "AuthMethodUnsupported"


def test_network_error_retried_then_succeeds(repo, settings, now):
    src = one_account(network_failures=2)
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "cli", lambda m: None)
    assert sleeps == [2, 8]
    assert res.status == "ok"


def test_network_error_exhausted(repo, settings, now):
    src = one_account(network_failures=9)
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "cli", lambda m: None)
    assert sleeps == [2, 8, 30] == list(RETRY_DELAYS)
    assert res.status == "error"
    assert res.error_type == "NetworkError"


def test_scheduled_login_waits_longer_for_network(repo, settings, now):
    # Tâche planifiée lancée au réveil : le réseau revient après plusieurs minutes.
    src = one_account(network_failures=5)
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "schedule", lambda m: None)
    assert sleeps == [2, 8, 30, 60, 120]
    assert res.status == "ok"


def test_scheduled_login_network_error_exhausted(repo, settings, now):
    src = one_account(network_failures=99)
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "schedule", lambda m: None)
    assert sleeps == list(SCHEDULE_LOGIN_RETRY_DELAYS)
    assert res.status == "error"
    assert res.error_type == "NetworkError"


def test_scheduled_run_keeps_short_retries_after_login(repo, settings, now):
    # Seule la connexion attend le réseau plus longtemps ; une fois connecté, une panne
    # de la banque ne doit pas tenir le verrou pendant des minutes à chaque appel.
    src = one_account(list_accounts_network_failures=9)
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "schedule", lambda m: None)
    assert sleeps == list(RETRY_DELAYS)
    assert res.error_type == "NetworkError"


def test_account_call_retried_on_network_error(repo, settings, now):
    src = one_account(history={"cc1": [make_tx(op="2026-10-15", amount=-200, raw="B")]})
    original = src.iter_transactions
    state = {"n": 0}

    def flaky(account_id, since):
        state["n"] += 1
        if state["n"] <= 2:
            raise NetworkError("coupure")
        yield from original(account_id, since)

    src.iter_transactions = flaky
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "cli", lambda m: None)
    assert sleeps == [2, 8]
    assert res.status == "ok" and res.added_transactions == 1


def test_account_network_exhausted_fails_only_that_account(repo, settings, now):
    src = FakeBankSource(
        [make_account("cc1"), make_account("cc2")],
        account_errors={"cc1": NetworkError("coupure")},
    )
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "cli", lambda m: None)
    assert sleeps == [2, 8, 30]
    assert res.status == "partial"


def test_other_errors_are_not_retried_on_account(repo, settings, now):
    src = FakeBankSource([make_account("cc1")], account_errors={"cc1": SourceBroken("changé")})
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "cli", lambda m: None)
    assert sleeps == []
    assert res.status == "error"


def test_info_logs_contain_no_bank_data(caplog, repo, settings, now):
    tx = make_tx(op="2026-10-15", amount=-1250, raw="CARTE SECRET MARCHAND")
    src = FakeBankSource(
        [make_account("cc1")], history={"cc1": [tx]}, two_factor="sms", network_failures=1
    )
    with caplog.at_level(logging.INFO, logger="bourso_mcp.sync"):
        build(src, repo, settings, now).run("all", "cli", lambda m: "123456")
    assert caplog.records
    text = caplog.text
    for forbidden in ("SECRET", "MARCHAND", "12.50", "12,50", "FR76", "123456", "Compte cc1"):
        assert forbidden not in text
    assert all(r.name == "bourso_mcp.sync" for r in caplog.records)


def test_dry_run_two_factor_writes_nothing(repo, settings, now):
    src = one_account(two_factor="sms", network_failures=1)
    sleeps = []
    seen = []

    def handler(method):
        seen.append(repo.current_run())
        return "123456"

    lines = build(src, repo, settings, now, sleeps).dry_run(handler)
    assert [line.account_id for line in lines] == ["cc1"]
    assert sleeps == [2]
    assert seen == [None]
    assert repo.last_finished_run() is None and repo.current_run() is None
    with repo._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_dry_run_auth_failed_propagates(repo, settings, now):
    src = one_account(login_error=AuthFailed("refusé"))
    with pytest.raises(AuthFailed):
        build(src, repo, settings, now).dry_run(lambda m: None)


def test_list_accounts_network_error_is_retried(repo, settings, now):
    src = one_account(list_accounts_network_failures=2)
    sleeps = []
    res = build(src, repo, settings, now, sleeps).run("all", "cli", lambda m: None)
    assert sleeps == [2, 8]
    assert res.status == "ok"


def test_dry_run_list_accounts_network_error_is_retried(repo, settings, now):
    src = one_account(list_accounts_network_failures=1)
    sleeps = []
    lines = build(src, repo, settings, now, sleeps).dry_run(lambda m: None)
    assert sleeps == [2] and len(lines) == 1


def test_two_factor_expired_raised_by_source_gives_auth_required(repo, settings, now):
    src = one_account(two_factor="app", submit_error=TwoFactorExpired("validation expirée"))
    res = build(src, repo, settings, now).run("all", "cli", lambda m: None)
    assert res.status == "auth_required"
    assert res.error_type == "TwoFactorExpired"


def test_keyboard_interrupt_closes_run_and_frees_lock(repo, settings, now):
    from filelock import FileLock

    src = one_account(two_factor="sms")

    def handler(method):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        build(src, repo, settings, now).run("all", "cli", handler)
    assert repo.current_run() is None
    last = repo.last_finished_run()
    assert last.status == "error" and last.error_type == "KeyboardInterrupt"
    with FileLock(settings.lock_path, timeout=0):
        pass
