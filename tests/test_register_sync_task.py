"""Tâche planifiée : les réglages promis par le README sont bien dans le script."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "scripts" / "register-sync-task.ps1").read_text(encoding="utf-8-sig")


def test_task_settings_documented_in_readme_are_registered():
    for text in (
        "-StartWhenAvailable",
        "-AllowStartIfOnBatteries",
        "-DontStopIfGoingOnBatteries",
        "-LogonType Interactive",
        "-RunLevel Limited",
        "--non-interactive",
    ):
        assert text in SCRIPT, text


def test_trigger_time_stays_local():
    # New-ScheduledTaskTrigger écrit l'heure en UTC : le script la réécrit sans décalage,
    # sinon la tâche se décale d'une heure au passage à l'heure d'été.
    assert "$trigger.StartBoundary = $start.ToString(\"yyyy-MM-dd'T'HH:mm:ss\"" in SCRIPT


def test_at_parameter_is_validated():
    assert "[ValidatePattern('^([01]?\\d|2[0-3]):[0-5]\\d$')]" in SCRIPT


def test_next_run_date_is_formatted_day_first():
    # "$next" interpolé s'écrirait mois/jour (culture invariante).
    assert "prochaine exécution le $next" not in SCRIPT
    assert '$next.ToString("dd/MM/yyyy' in SCRIPT
