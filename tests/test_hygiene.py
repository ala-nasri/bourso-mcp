"""Hygiène des fichiers versionnés : ni chemin personnel absolu, ni IBAN."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DRIVE_PATH = re.compile(r"\b[A-Za-z]:\[A-Za-z]")
IBAN = re.compile(r"\bFR\d{2}(?:\s?\d{4}){5}")


def _tracked_files() -> list[Path]:
    # `git ls-files` ne liste que les fichiers du dépôt : le contenu du sous-module
    # vendor/woob n'en fait pas partie (il apparaît comme un dossier, ignoré ici).
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    paths = [ROOT / name.decode("utf-8") for name in out.split(b"\0") if name]
    files = [path for path in paths if path.is_file()]
    assert files, "git ls-files ne renvoie aucun fichier : le test ne vérifierait rien"
    return files


def _offending_files(pattern: re.Pattern[str]) -> list[str]:
    offenders = []
    for path in _tracked_files():
        text = path.read_bytes().decode("utf-8", errors="ignore")
        if pattern.search(text):
            offenders.append(path.relative_to(ROOT).as_posix())
    return offenders


def test_no_absolute_drive_paths_in_tracked_files():
    assert _offending_files(DRIVE_PATH) == []


def test_no_iban_in_tracked_files():
    assert _offending_files(IBAN) == []
