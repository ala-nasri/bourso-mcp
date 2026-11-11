import csv

from bourso_mcp.store.export import CSV_HEADER, export_csv
from bourso_mcp.store.queries import TransactionFilter
from tests.factories import make_account, make_tx


def F(**kw):
    return TransactionFilter(**kw)


def load(repo, now, txs):
    repo.upsert_accounts([make_account("cc1")], now)
    repo.upsert_transactions("cc1", txs, now)
    return repo.db_path


def test_export_csv_format(repo, settings, now):
    db = load(
        repo,
        now,
        [
            make_tx(op="2026-10-02", amount=150000, raw="VIR SALAIRE", type="transfer"),
            make_tx(
                op="2026-10-01",
                amount=-1250,
                raw="CB CAFE",
                label="Café",
                category="Restaurants",
                card="1234",
            ),
        ],
    )
    path, n = export_csv(db, F(), settings.exports_dir, now)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and n == 2
    lines = raw.decode("utf-8-sig").splitlines()
    assert lines[0] == CSV_HEADER and ";-12,50;" in lines[1]
    assert lines[1] == "cc1;2026-10-01;2026-10-01;Café;CB CAFE;-12,50;card;Restaurants;1234;non"
    assert lines[2] == "cc1;2026-10-02;2026-10-02;VIR SALAIRE;VIR SALAIRE;1500,00;transfer;;;non"
    assert path.parent == settings.exports_dir
    assert path.name == "transactions-20261110-070000.csv"


def test_export_quotes_special_characters_and_flags_pending(repo, settings, now):
    db = load(repo, now, [make_tx(raw='ACHAT "X"; Y', amount=-100)])
    repo.replace_pending("cc1", [make_tx(op="2026-10-05", raw="EN ATTENTE", amount=-200)], now)
    path, n = export_csv(db, F(), settings.exports_dir, now)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    assert n == 2 and len(rows) == 3
    assert rows[1][3] == 'ACHAT "X"; Y' and rows[1][9] == "non"
    assert rows[2][3] == "EN ATTENTE" and rows[2][9] == "oui"


def test_export_respects_filter(repo, settings, now):
    db = load(
        repo,
        now,
        [make_tx(op="2026-10-01", amount=-100), make_tx(op="2026-10-02", amount=-200)],
    )
    path, n = export_csv(db, F(direction="credit"), settings.exports_dir, now)
    assert n == 0
    assert path.read_bytes().decode("utf-8-sig").splitlines() == [CSV_HEADER]
    _, n = export_csv(db, F(amount_min_cents=150), settings.exports_dir, now)
    assert n == 1
