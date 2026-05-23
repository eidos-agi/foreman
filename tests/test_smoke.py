from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_foreman_smoke_test_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "smoke_test.py")],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
    )

    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert "MCP tools list" in payload["checks"]
