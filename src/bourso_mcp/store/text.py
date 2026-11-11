"""Normalisation de texte pour la recherche."""

from __future__ import annotations

import unicodedata


def normalize_text(s: str) -> str:
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())
