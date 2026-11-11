from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime

import pytest

from bourso_mcp.server.tools import TOOL_NAMES, ToolInputError, parse_filter
from bourso_mcp.source.errors import AuthFailed
from tests.factories import make_account
from tests.fakes import FakeBankSource
from tests.pdf_fixture import text_pdf
from tests.store.test_statements import add as add_statement

NO_SYNC = {"last_success_at": None, "last_status": None, "auth_required": False}


def wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_tool_names_are_the_nine_tools():
    assert len(TOOL_NAMES) == 9 and len(set(TOOL_NAMES)) == 9


def test_read_tools_on_empty_store(tools):
    assert tools.list_accounts() == {"accounts": [], "sync": NO_SYNC}
    out = tools.search_transactions()
    assert out["total_count"] == 0 and out["items"] == []
    assert out["sync"] == NO_SYNC


def test_list_accounts_returns_balances_as_strings(tools, seeded):
    accounts = tools.list_accounts()["accounts"]
    assert [a["id"] for a in accounts] == ["cc1", "cc2"]
    assert accounts[0]["balance"] == "1000.00"
    assert accounts[1]["ownership"] == "co_owner"
    assert "iban" not in accounts[0]
    json.dumps(accounts)


def test_search_returns_amounts_as_strings_and_whole_filter_totals(tools, seeded):
    out = tools.search_transactions(account_ids=["cc1"], limit=1, sort="date_asc")
    assert [i["amount"] for i in out["items"]] == ["-12.50"]
    assert out["items"][0]["label"] == "CB CAFE" and out["items"][0]["type"] == "card"
    # totaux sur tout le filtre (card_summary exclu par défaut), pas sur la page
    assert out["total_count"] == 3
    assert out["total_debits"] == "-42.50"
    assert out["total_credits"] == "2000.00"
    assert out["net"] == "1957.50"
    assert out["limit"] == 1 and out["offset"] == 0
    json.dumps(out)


def test_card_summary_only_when_requested(tools, seeded):
    out = tools.search_transactions(types=["card_summary"])
    assert out["total_count"] == 1 and out["items"][0]["amount"] == "-99.99"


def test_amount_filters_are_in_euros(tools, seeded):
    out = tools.search_transactions(amount_min=20, amount_max="30.00", direction="debit")
    assert [i["amount"] for i in out["items"]] == ["-30.00"]


def test_text_and_dates_filter(tools, seeded):
    out = tools.search_transactions(text="café", date_from="2026-10-01", date_to="2026-10-01")
    assert out["total_count"] == 1


@pytest.mark.parametrize(
    "kwargs, param",
    [
        ({"date_from": "2026-13-01"}, "date_from"),
        ({"date_to": "20261001"}, "date_to"),
        ({"date_from": "2026-10-05", "date_to": "2026-10-01"}, "date_from"),
        ({"amount_min": 100, "amount_max": 50}, "amount_min"),
        ({"amount_min": -1}, "amount_min"),
        ({"amount_max": "abc"}, "amount_max"),
        ({"sort": "random"}, "sort"),
        ({"types": ["salaire"]}, "types"),
        ({"direction": "both"}, "direction"),
        ({"date_field": "value"}, "date_field"),
        ({"offset": -1}, "offset"),
    ],
)
def test_invalid_inputs_raise_clear_errors(tools, kwargs, param):
    with pytest.raises(ToolInputError, match=param):
        tools.search_transactions(**kwargs)


def test_invalid_input_lists_allowed_values(tools):
    with pytest.raises(ToolInputError, match="date_desc.*date_asc.*amount_asc.*amount_desc"):
        tools.search_transactions(sort="random")


def test_parse_filter_defaults_and_conversions():
    f = parse_filter(
        account_ids=["cc1"],
        date_from="2026-10-01",
        amount_min="12.50",
        types=["card"],
        direction="debit",
        date_field="debit",
    )
    assert f.account_ids == ("cc1",) and f.date_from == date(2026, 10, 1)
    assert f.amount_min_cents == 1250 and f.types == ("card",) and f.date_field == "debit"
    assert parse_filter().types is None and parse_filter(types=[]).types is None
    assert parse_filter().include_pending is True


def test_summarize_groups_with_totals(tools, seeded):
    out = tools.summarize_transactions(group_by="month")
    assert out["group_by"] == "month"
    by_key = {g["key"]: g for g in out["groups"]}
    assert by_key["2026-10"] == {
        "key": "2026-10",
        "count": 3,
        "total_debits": "-42.50",
        "total_credits": "2000.00",
        "net": "1957.50",
    }
    assert out["groups"][0]["key"] == "2026-10"  # |net| décroissant
    assert out["sync"] == NO_SYNC
    json.dumps(out)


def test_summarize_rejects_unknown_group_by(tools):
    with pytest.raises(ToolInputError, match="group_by"):
        tools.summarize_transactions(group_by="week")


def test_export_writes_csv_and_returns_path_and_rows(tools, seeded, settings):
    out = tools.export_transactions(account_ids=["cc1"])
    assert out["rows"] == 3
    exported = list(settings.exports_dir.glob("transactions-*.csv"))
    assert len(exported) == 1 and str(exported[0]) == out["path"]
    first_line = exported[0].read_text(encoding="utf-8-sig").splitlines()[0]
    assert first_line.startswith("compte;")
    assert out["sync"] == NO_SYNC


def test_list_statements_rejects_bad_period(tools):
    with pytest.raises(ToolInputError, match="period_from"):
        tools.list_statements(period_from="2026/10")
    with pytest.raises(ToolInputError, match="period_to"):
        tools.list_statements(period_to="2026-13")
    with pytest.raises(ToolInputError, match="period_from"):
        tools.list_statements(period_from="2026-10", period_to="2026-09")


def test_list_statements_and_read_statement(tools, seeded, repo, settings, now):
    add_statement(repo, settings, now, "2026-09", text_pdf("Releve septembre"))
    add_statement(repo, settings, now, "2026-10", text_pdf("Releve octobre"))
    out = tools.list_statements(period_from="2026-10")
    assert [s["period"] for s in out["statements"]] == ["2026-10"]
    s = out["statements"][0]
    assert s["id"] == "cc1-2026-10" and s["account_id"] == "cc1" and s["doc_type"] == "statement"
    assert s["path"].endswith("2026-10.pdf")
    read = tools.read_statement("cc1-2026-10")
    assert "Releve octobre" in read["text"]
    assert read["period"] == "2026-10" and read["account_id"] == "cc1"
    assert read["pages"] == 1 and read["has_text"] is True
    json.dumps(read)


def test_read_statement_missing_file_suggests_sync(tools, seeded, repo, settings, now):
    add_statement(repo, settings, now, "2026-10", text_pdf("x"))
    (settings.statements_dir / "cc1" / "2026-10.pdf").unlink()
    with pytest.raises(ToolInputError, match=r'sync_now\(scope="statements"\)'):
        tools.read_statement("cc1-2026-10")


def test_read_statement_unknown_id_names_parameter(tools):
    with pytest.raises(ToolInputError, match="statement_id"):
        tools.read_statement("nope")


def test_read_statement_unreadable_pdf(tools, seeded, repo, settings, now):
    add_statement(repo, settings, now, "2026-10", b"pas un pdf")
    with pytest.raises(ToolInputError, match="illisible.*statements"):
        tools.read_statement("cc1-2026-10")


def test_limit_is_clamped_to_500(tools):
    assert tools.search_transactions(limit=1000)["limit"] == 500
    assert tools.search_transactions(limit=0)["limit"] == 1


def finish(repo, status):
    when = datetime.now(UTC)
    run_id = repo.start_run("cli", "all", when)
    repo.finish_run(run_id, status, when, 0, 0, None, None)
    return run_id


def test_sync_block_reports_auth_required(tools, repo):
    finish(repo, "ok")
    block = tools.list_accounts()["sync"]
    assert block["last_status"] == "ok" and block["auth_required"] is False
    assert block["last_success_at"] is not None
    finish(repo, "auth_required")
    block = tools.list_accounts()["sync"]
    assert block["last_status"] == "auth_required" and block["auth_required"] is True
    assert block["last_success_at"] is not None


def test_sync_block_auth_failed_and_awaiting_2fa(tools, repo):
    finish(repo, "auth_failed")
    assert tools.search_transactions()["sync"]["auth_required"] is True
    finish(repo, "ok")
    assert tools.search_transactions()["sync"]["auth_required"] is False
    run_id = repo.start_run("mcp", "all", datetime.now(UTC))
    repo.set_run_status(run_id, "awaiting_2fa", "sms")
    assert tools.search_transactions()["sync"]["auth_required"] is True


def test_sync_now_reports_started_and_too_soon(make_tools, repo):
    tools = make_tools(FakeBankSource([make_account("cc1")]))
    out = tools.sync_now()
    assert out["started"] is True and "message" in out
    assert wait_until(lambda: repo.last_success_at() is not None)
    again = tools.sync_now()
    assert again["started"] is False and again["reason"] == "too_soon"
    assert again["last_success_at"] is not None and "force=true" in again["message"]
    json.dumps(again)
    assert tools.sync_now(force=True)["started"] is True


def test_sync_now_reports_current_run_when_already_running(make_tools):
    tools = make_tools(FakeBankSource([make_account("cc1")], two_factor="sms"))
    assert tools.sync_now()["started"] is True
    assert wait_until(lambda: (tools.sync_status()["current"] or {}).get("status") == "awaiting_2fa")
    out = tools.sync_now(force=True)
    assert out["started"] is False and out["reason"] == "already_running"
    assert out["current"]["status"] == "awaiting_2fa" and out["current"]["twofa_method"] == "sms"
    assert tools.submit_2fa_code("123456")["accepted"] is True
    assert wait_until(lambda: tools.sync_status()["current"] is None)
    assert tools.sync_status()["last"]["status"] == "ok"


def test_sync_now_validates_scope(tools):
    with pytest.raises(ToolInputError, match="scope"):
        tools.sync_now(scope="everything")


def test_sync_status_without_runs(tools):
    out = tools.sync_status()
    assert out["current"] is None and out["last"] is None and out["last_success_at"] is None
    assert out["sync"] == NO_SYNC


def test_sync_status_reports_auth_failure(make_tools):
    tools = make_tools(FakeBankSource([make_account("cc1")], login_error=AuthFailed("refusé")))
    tools.sync_now()
    assert wait_until(lambda: tools.sync_status()["last"] is not None)
    assert tools.sync_status()["last"]["status"] == "auth_failed"
    assert tools.sync_status()["sync"]["auth_required"] is True


def test_submit_2fa_code_without_pending_run(tools):
    out = tools.submit_2fa_code("123456")
    assert out["accepted"] is False
    assert "sync_status" in out["reason"] and "SMS" in out["reason"]
    assert "123456" not in json.dumps(out)
