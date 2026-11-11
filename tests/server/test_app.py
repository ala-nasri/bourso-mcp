from __future__ import annotations

import asyncio
import json
import logging

from mcp.client import Client

from bourso_mcp.server.app import build_server
from bourso_mcp.server.tools import TOOL_NAMES


def run_client(server, calls):
    """Appelle des outils via le client MCP en mémoire ; `calls` : liste de (nom, arguments)."""

    async def go():
        async with Client(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            results = [await client.call_tool(name, args) for name, args in calls]
            return names, results

    return asyncio.run(go())


def payload(result):
    return json.loads(result.content[0].text)


def test_app_lists_all_tools(tools):
    names, _ = run_client(build_server(tools), [])
    assert names == set(TOOL_NAMES)


def test_tool_descriptions_state_the_rules(tools):
    async def go():
        async with Client(build_server(tools)) as client:
            return {t.name: t.description for t in (await client.list_tools()).tools}

    descriptions = asyncio.run(go())
    search = descriptions["search_transactions"]
    assert "lecture seule" in search.lower() and "card_summary" in search and "sync" in search
    assert "tout le filtre" in search
    assert "BoursoBank" in descriptions["sync_now"]


def test_app_search_roundtrip(tools, seeded):
    _, (result,) = run_client(
        build_server(tools), [("search_transactions", {"account_ids": ["cc1"], "limit": 2})]
    )
    assert result.is_error is False
    data = payload(result)
    assert data["total_count"] == 3 and len(data["items"]) == 2
    assert data["sync"]["last_status"] is None


def test_app_turns_input_errors_into_tool_errors(tools):
    _, (result,) = run_client(build_server(tools), [("search_transactions", {"sort": "random"})])
    assert result.is_error is True
    assert "sort" in result.content[0].text and "date_desc" in result.content[0].text


def test_app_statement_error_message_reaches_the_client(tools):
    _, (result,) = run_client(build_server(tools), [("read_statement", {"statement_id": "nope"})])
    assert result.is_error is True and "statement_id" in result.content[0].text


def test_stdout_stays_clean_and_code_is_not_echoed(tools, seeded, capsys):
    run_client(
        build_server(tools),
        [("search_transactions", {}), ("sync_status", {}), ("submit_2fa_code", {"code": "424242"})],
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "424242" not in captured.err


def test_submit_code_is_never_logged(tools, caplog):
    caplog.set_level(logging.DEBUG)
    run_client(build_server(tools), [("submit_2fa_code", {"code": "424242"})])
    assert "424242" not in caplog.text
