from datetime import date, timedelta

from bourso_mcp.source.models import StatementRef
from bourso_mcp.store.schema import connect
from tests.factories import make_account, make_tx


def _query(repo, sql):
    conn = connect(repo.db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _count(repo, where="1=1"):
    return _query(repo, f"SELECT COUNT(*) AS n FROM transactions WHERE {where}")[0]["n"]


def _setup(repo, now):
    repo.upsert_accounts([make_account()], now)


def test_upsert_is_idempotent(repo, now):
    _setup(repo, now)
    txs = [make_tx(), make_tx(), make_tx(op="2026-10-02")]
    assert repo.upsert_transactions("cc1", txs, now) == 3
    assert repo.upsert_transactions("cc1", txs, now) == 0
    assert _count(repo) == 3


def test_upsert_updates_mutable_fields(repo, now):
    _setup(repo, now)
    repo.upsert_transactions("cc1", [make_tx(label="Ancien")], now)
    assert repo.upsert_transactions("cc1", [make_tx(label="Nouveau Café")], now) == 0
    assert _count(repo) == 1
    row = _query(repo, "SELECT label, search_text FROM transactions")[0]
    assert row["label"] == "Nouveau Café"
    assert row["search_text"] == "cb cafe nouveau cafe"


def test_replace_pending_replaces_only_pending(repo, now):
    _setup(repo, now)
    repo.upsert_transactions("cc1", [make_tx(), make_tx(op="2026-10-02")], now)
    repo.replace_pending("cc1", [make_tx(op="2026-11-01"), make_tx(op="2026-11-02")], now)
    assert _count(repo) == 4
    repo.replace_pending("cc1", [make_tx(op="2026-11-03")], now)
    assert _count(repo) == 3
    assert _count(repo, "pending = 1") == 1


def test_max_debit_date_ignores_pending(repo, now):
    _setup(repo, now)
    assert repo.max_debit_date("cc1") is None
    repo.upsert_transactions("cc1", [make_tx(op="2026-10-01", debit="2026-10-05")], now)
    repo.replace_pending("cc1", [make_tx(op="2026-11-01")], now)
    assert repo.max_debit_date("cc1") == date(2026, 10, 5)


def test_statement_needs_download_when_file_missing(repo, settings, now):
    _setup(repo, now)
    ref = StatementRef("s1", "cc1", date(2026, 10, 31), "2026-10", "statement")
    assert repo.statement_needs_download("s1", settings.home) is True
    rel = "statements/cc1/2026-10.pdf"
    f = settings.home / rel
    f.parent.mkdir(parents=True)
    f.write_bytes(b"%PDF")
    repo.save_statement(ref, rel, "abc", now)
    assert repo.statement_needs_download("s1", settings.home) is False
    f.unlink()
    assert repo.statement_needs_download("s1", settings.home) is True


def test_last_success_at_counts_ok_and_partial(repo, now):
    assert repo.last_success_at() is None
    t1, t2, t3 = now, now + timedelta(hours=1), now + timedelta(hours=2)
    for status, t in (("ok", t1), ("partial", t2), ("error", t3)):
        rid = repo.start_run("cli", "all", t)
        repo.finish_run(rid, status, t, 0, 0, None, None)
    assert repo.last_success_at() == t2
    assert repo.last_finished_run().status == "error"


def test_mark_interrupted_runs(repo, now):
    repo.start_run("mcp", "all", now)
    r2 = repo.start_run("mcp", "all", now)
    repo.set_run_status(r2, "awaiting_2fa", "sms")
    assert repo.current_run().id == r2
    assert repo.mark_interrupted_runs(now) == 2
    assert repo.current_run() is None
    last = repo.last_finished_run()
    assert last.status == "error" and last.error_message == "interrompu"
    assert last.finished_at == now
