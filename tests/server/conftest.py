from __future__ import annotations

import pytest

from bourso_mcp.cli.wiring import build_service
from bourso_mcp.server.tools import Tools
from bourso_mcp.sync.runner import BackgroundSync
from tests.factories import make_account, make_tx
from tests.fakes import FakeBankSource


@pytest.fixture
def make_tools(settings, repo):
    """Fabrique des outils ; la source n'est qu'un faux de la banque."""

    def build(source=None):
        source = source or FakeBankSource([make_account("cc1")])
        service = build_service(settings, factory=lambda: source)
        return Tools(settings, repo, BackgroundSync(service, repo))

    return build


@pytest.fixture
def tools(make_tools):
    return make_tools()


@pytest.fixture
def seeded(repo, now):
    repo.upsert_accounts([make_account("cc1"), make_account("cc2", ownership="co_owner")], now)
    repo.upsert_transactions(
        "cc1",
        [
            make_tx(op="2026-10-01", amount=-1250, raw="CB CAFE", category="Restaurants"),
            make_tx(op="2026-10-02", amount=-3000, raw="CB COURSES"),
            make_tx(op="2026-10-03", amount=200000, raw="VIR SALAIRE", type="transfer"),
            make_tx(op="2026-10-04", amount=-9999, raw="DEBIT CARTE", type="card_summary"),
        ],
        now,
    )
    repo.upsert_transactions("cc2", [make_tx(op="2026-09-15", amount=-500, raw="CB TABAC")], now)
