from datetime import date

import pytest

from bourso_mcp.source.models import StatementRef
from bourso_mcp.store.statements import (
    StatementFileMissing,
    StatementNotFound,
    StatementUnreadable,
    list_statements,
    read_statement,
)
from tests.factories import make_account
from tests.pdf_fixture import blank_pdf, text_pdf


def add(repo, settings, now, period, pdf, account="cc1", sid=None):
    rel = f"statements/{account}/{period}.pdf"
    target = settings.home / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pdf)
    ref = StatementRef(
        id=sid or f"{account}-{period}",
        account_id=account,
        date=date.fromisoformat(f"{period}-28"),
        period=period,
        doc_type="statement",
    )
    repo.save_statement(ref, rel, "0" * 64, now)
    return ref


@pytest.fixture
def accounts(repo, now):
    repo.upsert_accounts([make_account("cc1"), make_account("cc2")], now)


def test_list_statements_filters_by_period(repo, settings, now, accounts):
    for p in ("2026-08", "2026-09", "2026-10"):
        add(repo, settings, now, p, blank_pdf())
    rows = list_statements(repo.db_path, None, "2026-09", None)
    assert [r["period"] for r in rows] == ["2026-10", "2026-09"]
    rows = list_statements(repo.db_path, None, None, "2026-09")
    assert [r["period"] for r in rows] == ["2026-09", "2026-08"]
    rows = list_statements(repo.db_path, None, "2026-09", "2026-09")
    assert [r["period"] for r in rows] == ["2026-09"]


def test_list_statements_filters_by_account(repo, settings, now, accounts):
    add(repo, settings, now, "2026-10", blank_pdf(), account="cc1")
    add(repo, settings, now, "2026-10", blank_pdf(), account="cc2")
    assert len(list_statements(repo.db_path, None, None, None)) == 2
    rows = list_statements(repo.db_path, ("cc2",), None, None)
    assert [r["account_id"] for r in rows] == ["cc2"]
    assert list_statements(repo.db_path, (), None, None) == []


def test_read_statement_extracts_text(repo, settings, now, accounts):
    ref = add(repo, settings, now, "2026-10", text_pdf(r"RELEVE OCTOBRE (1) \ fin"))
    st = read_statement(repo.db_path, settings.home, ref.id)
    assert r"RELEVE OCTOBRE (1) \ fin" in st.text
    assert st.has_text is True and st.pages == 1
    assert st.path == settings.home / "statements/cc1/2026-10.pdf"
    assert st.row["period"] == "2026-10" and st.row["account_id"] == "cc1"


def test_read_statement_without_text_layer(repo, settings, now, accounts):
    ref = add(repo, settings, now, "2026-10", blank_pdf())
    st = read_statement(repo.db_path, settings.home, ref.id)
    assert st.has_text is False and st.text == "" and st.pages == 1


def test_read_statement_missing_file(repo, settings, now, accounts):
    ref = add(repo, settings, now, "2026-10", blank_pdf())
    (settings.home / "statements/cc1/2026-10.pdf").unlink()
    with pytest.raises(StatementFileMissing):
        read_statement(repo.db_path, settings.home, ref.id)


def test_read_statement_unknown_id(repo, settings):
    with pytest.raises(StatementNotFound):
        read_statement(repo.db_path, settings.home, "nope")


def _set_file_path(repo, statement_id, value):
    import sqlite3

    conn = sqlite3.connect(repo.db_path)
    with conn:
        conn.execute("UPDATE statements SET file_path = ? WHERE id = ?", (value, statement_id))
    conn.close()


def test_read_statement_rejects_parent_traversal(repo, settings, now, accounts):
    ref = add(repo, settings, now, "2026-10", blank_pdf())
    (settings.home.parent / "x.pdf").write_bytes(blank_pdf())
    _set_file_path(repo, ref.id, "../x.pdf")
    with pytest.raises(StatementFileMissing):
        read_statement(repo.db_path, settings.home, ref.id)


def test_read_statement_rejects_absolute_path(repo, settings, now, accounts, tmp_path_factory):
    ref = add(repo, settings, now, "2026-10", blank_pdf())
    outside = tmp_path_factory.mktemp("outside") / "y.pdf"
    outside.write_bytes(blank_pdf())
    _set_file_path(repo, ref.id, str(outside))
    with pytest.raises(StatementFileMissing):
        read_statement(repo.db_path, settings.home, ref.id)


@pytest.mark.parametrize("content", [b"", b"ceci n'est pas un pdf"])
def test_read_statement_unreadable_pdf(repo, settings, now, accounts, content):
    ref = add(repo, settings, now, "2026-10", content)
    with pytest.raises(StatementUnreadable):
        read_statement(repo.db_path, settings.home, ref.id)
