"""Conversions entre centimes entiers et montants en euros."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def cents_to_str(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    q, r = divmod(abs(cents), 100)
    return f"{sign}{q}.{r:02d}"


def cents_to_fr(cents: int) -> str:
    return cents_to_str(cents).replace(".", ",")


def euros_to_cents(value: float | str | Decimal) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
