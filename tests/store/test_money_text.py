from bourso_mcp.store.money import cents_to_fr, cents_to_str, euros_to_cents
from bourso_mcp.store.text import normalize_text


def test_money_conversions():
    assert cents_to_str(-1250) == "-12.50" and cents_to_str(5) == "0.05"
    assert cents_to_fr(-1250) == "-12,50"
    assert euros_to_cents("12.345") == 1235 and euros_to_cents(12.3) == 1230


def test_normalize_text():
    assert normalize_text("  Café  DE la Gare ") == "cafe de la gare"
