from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_foreman_help_lists_core_commands() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "foreman.py"), "--help"],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO,
    )

    assert "delegate" in result.stdout
    assert "collect" in result.stdout
    assert "control-submit" in result.stdout


def test_package_source_foreman_lists_core_commands() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO / "packages" / "foreman-cli" / "scripts" / "foreman.py"), "--help"],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO,
    )

    assert "delegate" in result.stdout
    assert "agent-run" in result.stdout
    assert "control-submit" in result.stdout
