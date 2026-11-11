from datetime import date

from bourso_mcp.store.queries import (
    Group,
    Totals,
    TransactionFilter,
    build_where,
    search,
    summarize,
)
from tests.factories import make_account, make_tx


def F(**kw):
    return TransactionFilter(**kw)


def amounts(result):
    return [r["amount_cents"] for r in result.items]


def load(repo, now, txs, account="cc1", extra_accounts=()):
    repo.upsert_accounts([make_account(account), *extra_accounts], now)
    repo.upsert_transactions(account, txs, now)
    return repo.db_path


def test_amount_bounds_apply_to_absolute_value(repo, now):
    db = load(
        repo,
        now,
        [
            make_tx(amount=-3000, raw="A"),
            make_tx(amount=-6000, raw="B"),
            make_tx(amount=6000, raw="C"),
            make_tx(amount=-12000, raw="D"),
        ],
    )
    got = amounts(search(db, F(amount_min_cents=5000, amount_max_cents=10000), "amount_asc"))
    assert got == [-6000, 6000]
    got = search(db, F(amount_min_cents=5000, amount_max_cents=10000, direction="debit"))
    assert amounts(got) == [-6000]
    assert amounts(search(db, F(direction="credit"))) == [6000]


def test_text_filter_ignores_accents_and_case(repo, now):
    db = load(repo, now, [make_tx(raw="CAFE DE LA GARE"), make_tx(raw="BOULANGERIE")])
    assert search(db, F(text="café")).totals.count == 1
    assert search(db, F(text="CAFÉ de")).totals.count == 1


def test_text_filter_escapes_like_wildcards(repo, now):
    db = load(repo, now, [make_tx(raw="REMISE 50%"), make_tx(raw="REMISE 500")])
    res = search(db, F(text="50%"))
    assert [r["raw_label"] for r in res.items] == ["REMISE 50%"]
    assert search(db, F(text="_")).totals.count == 0
    assert search(db, F(text="remise 50")).totals.count == 2


def test_card_summary_excluded_by_default(repo, now):
    repo.upsert_accounts([make_account("cc1"), make_account("cb1", "card", parent_id="cc1")], now)
    repo.upsert_transactions(
        "cb1", [make_tx(amount=-2000, raw="X"), make_tx(amount=-3000, raw="Y")], now
    )
    repo.upsert_transactions(
        "cc1", [make_tx(amount=-5000, raw="DEBIT DIFFERE", type="card_summary")], now
    )
    db = repo.db_path
    assert search(db, F()).totals.debits_cents == -5000
    assert search(db, F()).totals.count == 2
    assert search(db, F(types=("card_summary",))).totals.count == 1
    assert search(db, F(types=("card", "card_summary"))).totals.count == 3


def test_date_bounds_are_inclusive_on_operation_and_debit_date(repo, now):
    db = load(
        repo,
        now,
        [
            make_tx(op="2026-09-30", debit="2026-10-05", raw="A"),
            make_tx(op="2026-10-01", debit="2026-10-31", raw="B"),
            make_tx(op="2026-10-31", debit="2026-11-01", raw="C"),
            make_tx(op="2026-11-01", debit="2026-11-30", raw="D"),
        ],
    )
    f = F(date_from=date(2026, 10, 1), date_to=date(2026, 10, 31))
    assert [r["raw_label"] for r in search(db, f, "date_asc").items] == ["B", "C"]
    f = F(date_from=date(2026, 10, 5), date_to=date(2026, 10, 31), date_field="debit")
    assert [r["raw_label"] for r in search(db, f, "date_asc").items] == ["A", "B"]


def test_filter_by_account_ids_category_and_card(repo, now):
    db = load(
        repo,
        now,
        [
            make_tx(raw="A", category="Courses", card="4970 XXXX XXXX 1234"),
            make_tx(raw="B", category="Loisirs", card="4970 XXXX XXXX 9999"),
        ],
        extra_accounts=[make_account("cc2")],
    )
    repo.upsert_transactions("cc2", [make_tx(raw="C", category="Courses")], now)
    assert search(db, F(account_ids=("cc2",))).totals.count == 1
    assert search(db, F(account_ids=("cc1", "cc2"))).totals.count == 3
    assert search(db, F(category="Courses")).totals.count == 2
    assert search(db, F(category="course")).totals.count == 0
    res = search(db, F(card="1234"))
    assert [r["raw_label"] for r in res.items] == ["A"]
    assert search(db, F(card="%")).totals.count == 0


def test_include_pending_false(repo, now):
    db = load(repo, now, [make_tx(raw="POSTED")])
    repo.replace_pending("cc1", [make_tx(raw="PENDING", amount=-500)], now)
    assert search(db, F()).totals.count == 2
    res = search(db, F(include_pending=False))
    assert [r["raw_label"] for r in res.items] == ["POSTED"]


def test_totals_cover_whole_filter_not_page(repo, now):
    txs = [make_tx(op="2026-10-01", raw=f"L{i}", amount=-100 if i % 2 else 300) for i in range(150)]
    db = load(repo, now, txs)
    res = search(db, F(), limit=100)
    assert len(res.items) == 100
    assert res.totals == Totals(count=150, debits_cents=-7500, credits_cents=22500)
    page2 = search(db, F(), limit=100, offset=100)
    assert len(page2.items) == 50
    assert page2.totals == res.totals


def test_empty_result_has_zero_totals(repo, now):
    db = load(repo, now, [make_tx()])
    res = search(db, F(text="introuvable"))
    assert res.items == []
    assert res.totals == Totals(0, 0, 0)


def test_sort_orders(repo, now):
    db = load(
        repo,
        now,
        [
            make_tx(op="2026-10-02", amount=-500, raw="A"),
            make_tx(op="2026-10-01", amount=-500, raw="B"),
            make_tx(op="2026-10-02", amount=700, raw="C"),
            make_tx(op="2026-10-03", amount=-900, raw="D"),
        ],
    )

    def labels(sort):
        return [r["raw_label"] for r in search(db, F(), sort).items]

    assert labels("date_desc") == ["D", "C", "A", "B"]
    assert labels("date_asc") == ["B", "A", "C", "D"]
    assert labels("amount_asc") == ["D", "A", "B", "C"]
    assert labels("amount_desc") == ["C", "B", "A", "D"]


def test_summarize_by_month_and_category(repo, now):
    db = load(
        repo,
        now,
        [
            make_tx(op="2026-09-10", amount=-1000, raw="A", category="Courses"),
            make_tx(op="2026-10-02", amount=-2000, raw="B", category="Courses"),
            make_tx(op="2026-10-05", amount=5000, raw="C", category=None),
            make_tx(op="2026-10-09", amount=-500, raw="D", category="Loisirs"),
        ],
    )
    assert summarize(db, F(), "month") == [
        Group("2026-10", Totals(3, -2500, 5000)),
        Group("2026-09", Totals(1, -1000, 0)),
    ]
    by_cat = summarize(db, F(), "category")
    assert [g.key for g in by_cat] == ["(sans catégorie)", "Courses", "Loisirs"]
    assert by_cat[1].totals == Totals(2, -3000, 0)
    assert [g.key for g in summarize(db, F(), "category", limit=2)] == [
        "(sans catégorie)",
        "Courses",
    ]


def test_summarize_by_label_type_account_and_date_field(repo, now):
    db = load(
        repo,
        now,
        [
            make_tx(op="2026-09-30", debit="2026-10-02", amount=-1000, raw="X", label="Shop"),
            make_tx(op="2026-10-01", amount=-1000, raw="Y", label="Shop"),
            make_tx(op="2026-10-01", amount=-300, raw="Z", label="Autre", type="transfer"),
        ],
        extra_accounts=[make_account("cc2")],
    )
    repo.upsert_transactions("cc2", [make_tx(amount=-100, raw="W", label="Autre")], now)
    assert summarize(db, F(), "label")[0] == Group("Shop", Totals(2, -2000, 0))
    assert {g.key for g in summarize(db, F(), "type")} == {"card", "transfer"}
    assert {g.key: g.totals.count for g in summarize(db, F(), "account")} == {"cc1": 3, "cc2": 1}
    assert [g.key for g in summarize(db, F(date_field="debit"), "month")] == ["2026-10"]
    assert {g.key for g in summarize(db, F(), "month")} == {"2026-09", "2026-10"}


def test_build_where_is_parameterised():
    sql, params = build_where(F(text="a'; DROP TABLE x"))
    assert "DROP" not in sql
    assert "a'; drop table x" in params
    sql, params = build_where(F())
    assert params == ["card_summary"]
