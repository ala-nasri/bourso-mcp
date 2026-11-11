from __future__ import annotations

import hashlib
from datetime import date

import pytest

from bourso_mcp.source.errors import AuthFailed, SourceBroken
from bourso_mcp.source.models import StatementRef
from bourso_mcp.sync.service import SyncService
from tests.factories import make_account, make_tx
from tests.fakes import FakeBankSource


def make_ref(id, account_id="cc1", period="2026-10", d="2026-10-31"):
    return StatementRef(
        id=id, account_id=account_id, date=date.fromisoformat(d), period=period, doc_type="statement"
    )


def service(source, repo, settings, now):
    return SyncService(lambda: source, repo, settings, now=lambda: now, sleep=lambda s: None)


def run(svc, scope="all"):
    return svc.run(scope, "cli", lambda method: None)


def three_txs():
    return [
        make_tx(op="2026-09-01", amount=-100, raw="A"),
        make_tx(op="2026-10-15", amount=-200, raw="B"),
        make_tx(op="2026-10-31", amount=-300, raw="C"),
    ]


def test_first_pass_fetches_full_history(repo, settings, now):
    src = FakeBankSource([make_account("cc1")], history={"cc1": three_txs()})
    res = run(service(src, repo, settings, now))
    assert ("iter_transactions", "cc1", None) in src.calls
    assert res.status == "ok"
    assert res.added_transactions == 3
    assert res.accounts[0].ok and res.accounts[0].added_transactions == 3
    row = repo.last_finished_run()
    assert row.status == "ok" and row.added_transactions == 3


def test_incremental_pass_uses_ten_day_overlap(repo, settings, now):
    src = FakeBankSource([make_account("cc1")], history={"cc1": three_txs()})
    svc = service(src, repo, settings, now)
    run(svc)
    src.calls.clear()
    res = run(svc)
    assert ("iter_transactions", "cc1", date(2026, 10, 21)) in src.calls
    assert res.added_transactions == 0
    with repo._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3


def test_pending_becoming_booked_is_not_duplicated(repo, settings, now):
    tx = make_tx(op="2026-11-05", amount=-500, raw="PENDING")
    src = FakeBankSource([make_account("cc1")], pending={"cc1": [tx]})
    svc = service(src, repo, settings, now)
    run(svc)
    with repo._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM transactions WHERE pending = 1").fetchone()[0] == 1
    src.pending = {}
    src.history = {"cc1": [tx]}
    res = run(svc)
    assert res.added_transactions == 1
    with repo._conn() as c:
        rows = c.execute("SELECT pending FROM transactions").fetchall()
    assert [r["pending"] for r in rows] == [0]


def test_statements_downloaded_once_and_named_by_period(repo, settings, now):
    docs = [(make_ref("s1"), b"%PDF one"), (make_ref("s2"), b"%PDF two")]
    src = FakeBankSource([make_account("cc1")], statements={"cc1": docs})
    svc = service(src, repo, settings, now)
    res = run(svc)
    assert res.added_statements == 2
    d = settings.statements_dir / "cc1"
    assert sorted(p.name for p in d.iterdir()) == ["2026-10-2.pdf", "2026-10.pdf"]
    assert (d / "2026-10.pdf").read_bytes() == b"%PDF one"
    with repo._conn() as c:
        row = c.execute("SELECT * FROM statements WHERE id = 's1'").fetchone()
    assert row["file_path"] == "statements/cc1/2026-10.pdf"
    assert row["sha256"] == hashlib.sha256(b"%PDF one").hexdigest()
    src.calls.clear()
    res = run(svc)
    assert res.added_statements == 0
    assert not [c for c in src.calls if c[0] == "download_statement"]


def test_missing_statement_file_is_redownloaded(repo, settings, now):
    src = FakeBankSource([make_account("cc1")], statements={"cc1": [(make_ref("s1"), b"%PDF x")]})
    svc = service(src, repo, settings, now)
    run(svc)
    (settings.statements_dir / "cc1" / "2026-10.pdf").unlink()
    res = run(svc)
    assert res.added_statements == 1
    assert (settings.statements_dir / "cc1" / "2026-10.pdf").read_bytes() == b"%PDF x"


def test_failure_on_one_account_gives_partial(repo, settings, now):
    src = FakeBankSource(
        [make_account("cc1"), make_account("cb1", type="card", parent_id="cc1")],
        history={"cc1": three_txs()},
        account_errors={"cb1": SourceBroken("x")},
    )
    res = run(service(src, repo, settings, now))
    assert res.status == "partial"
    by_id = {a.account_id: a for a in res.accounts}
    assert by_id["cc1"].ok and by_id["cc1"].added_transactions == 3
    assert not by_id["cb1"].ok and by_id["cb1"].error_type == "SourceBroken"
    assert res.error_type == "SourceBroken"
    assert "cb1" in res.error_message and "SourceBroken" in res.error_message
    assert repo.last_success_at() is not None


def test_all_accounts_failing_gives_error(repo, settings, now):
    src = FakeBankSource([make_account("cc1")], account_errors={"cc1": SourceBroken("x")})
    res = run(service(src, repo, settings, now))
    assert res.status == "error"


def test_login_failure_ends_run_in_error(repo, settings, now):
    src = FakeBankSource([make_account("cc1")], login_error=AuthFailed("no"))
    res = run(service(src, repo, settings, now))
    assert res.status == "error" and res.error_type == "AuthFailed"
    assert repo.current_run() is None
    assert repo.last_finished_run().error_type == "AuthFailed"


def test_factory_failure_ends_run_in_error(repo, settings, now):
    def boom():
        raise AuthFailed("absent")

    res = SyncService(boom, repo, settings, now=lambda: now).run("all", "cli", lambda m: None)
    assert res.status == "error" and res.error_type == "AuthFailed"


def test_unexpected_exception_does_not_leave_run_running(repo, settings, now):
    class Bug(FakeBankSource):
        def list_accounts(self):
            raise ValueError("bug")

    svc = service(Bug([make_account("cc1")]), repo, settings, now)
    with pytest.raises(ValueError):
        run(svc)
    assert repo.current_run() is None
    assert repo.last_finished_run().error_type == "ValueError"


def test_scope_limits_work(repo, settings, now):
    src = FakeBankSource([make_account("cc1")])
    run(service(src, repo, settings, now), "transactions")
    names = {c[0] for c in src.calls}
    assert "iter_transactions" in names and "list_statements" not in names
    src.calls.clear()
    run(service(src, repo, settings, now), "statements")
    names = {c[0] for c in src.calls}
    assert "list_statements" in names
    assert "iter_transactions" not in names and "iter_pending" not in names


def test_balances_updated(repo, settings, now):
    src = FakeBankSource([make_account("cc1", balance_cents=4200)])
    run(service(src, repo, settings, now))
    row = repo.list_accounts()[0]
    assert row["balance_cents"] == 4200
    assert row["balance_at"] == now.isoformat()


def test_dry_run_writes_nothing(repo, settings, now):
    src = FakeBankSource(
        [make_account("cc1")],
        history={"cc1": three_txs()},
        pending={"cc1": [make_tx(op="2026-11-05")]},
        statements={"cc1": [(make_ref("s1"), b"%PDF x")]},
    )
    lines = service(src, repo, settings, now).dry_run(lambda m: None)
    assert len(lines) == 1
    line = lines[0]
    assert (line.account_id, line.label, line.type) == ("cc1", "Compte cc1", "checking")
    assert (line.transactions, line.pending, line.statements) == (3, 1, 1)
    assert not [c for c in src.calls if c[0] == "download_statement"]
    assert repo.list_accounts() == []
    assert repo.last_finished_run() is None and repo.current_run() is None
    with repo._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0
    assert not list(settings.statements_dir.rglob("*.pdf"))
