from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from bourso_mcp.server.tools import TOOL_NAMES

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_documents_every_tool():
    for name in TOOL_NAMES:
        assert f"`{name}`" in README, name


def test_readme_documents_every_cli_command():
    for text in (
        "bourso-mcp login",
        "bourso-mcp sync",
        "bourso-mcp status",
        "bourso-mcp serve",
        "--scope",
        "--force",
        "--non-interactive",
        "--dry-run",
    ):
        assert text in README, text


def test_readme_mentions_pinned_woob_commit():
    sha = subprocess.run(
        ["git", "-C", str(ROOT / "vendor" / "woob"), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert sha in README


def test_readme_documents_configuration():
    for text in ("BOURSO_MCP_HOME", "cmdkey /generic:bourso-mcp", "cmdkey /list:bourso-mcp"):
        assert text in README, text


def test_readme_documents_server_launch():
    assert "Lancement du MCP" in README
    assert "claude mcp add bourso -- uv --directory <chemin-du-dépôt> run bourso-mcp serve" in README
    assert "Modifier la configuration" in README


def test_readme_config_snippets_carry_no_secret():
    blocks = re.findall(r"```json\n(.*?)```", README, flags=re.DOTALL)
    assert blocks
    for block in blocks:
        assert "PASSWORD" not in block.upper() and "LOGIN" not in block.upper()
    desktop = [b for b in blocks if "mcpServers" in b]
    assert desktop
    config = json.loads(desktop[0])
    assert "env" not in config["mcpServers"]["bourso"]
