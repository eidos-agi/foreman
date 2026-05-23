from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_mcp_server_lists_expected_tools() -> None:
    payload = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            "",
        ]
    )

    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "mcp_server.py")],
        input=payload,
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=10,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    tools = responses[-1]["result"]["tools"]
    names = {tool["name"] for tool in tools}

    assert "foreman_delegate" in names
    assert "foreman_collect" in names
    assert "foreman_control_submit" in names
    assert "foreman_tail" in names
    assert "foreman_status" in names
    assert "foreman_note" in names
    assert "foreman_interrupt" in names
    assert len(tools) >= 10

    delegate_tool = next(tool for tool in tools if tool["name"] == "foreman_delegate")
    engine_schema = delegate_tool["inputSchema"]["properties"]["engine"]
    assert "opencode" in engine_schema["enum"]
    assert "gemma4" in engine_schema["enum"]
