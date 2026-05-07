#!/usr/bin/env python3
"""Tiny MCP stdio server for Foreman."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FOREMAN = ROOT / "scripts" / "foreman.py"


TOOLS = [
    {
        "name": "foreman_delegate",
        "description": "Delegate an implementation task to an AI coding engine in an isolated git worktree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string", "description": "Concrete implementation spec with acceptance criteria and scope."},
                "engine": {"type": "string", "enum": ["claude", "codex", "gemini", "aider", "smoke"], "description": "Worker engine. Defaults to claude."},
                "repo_path": {"type": "string", "description": "Repository path. Defaults to MCP server cwd if omitted."},
                "base_ref": {"type": "string", "description": "Base branch/ref for the worktree. Defaults to current branch."},
                "test_command": {"type": "string", "description": "Verification command the engineer should run."},
                "timeout_sec": {"type": "integer", "description": "Maximum worker runtime in seconds. Defaults to 900."},
                "allow_dirty": {"type": "boolean", "description": "Allow delegating from a repo with uncommitted changes. Defaults to false."},
            },
            "required": ["spec"],
        },
    },
    {
        "name": "foreman_list",
        "description": "List recent delegated workers and statuses.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "foreman_tail",
        "description": "Return recent log lines for a worker.",
        "inputSchema": {
            "type": "object",
            "properties": {"worker_id": {"type": "string"}, "lines": {"type": "integer", "default": 100}},
            "required": ["worker_id"],
        },
    },
    {
        "name": "foreman_monitor_hint",
        "description": "Return terminal commands for real-time monitoring of a worker.",
        "inputSchema": {
            "type": "object",
            "properties": {"worker_id": {"type": "string"}},
            "required": ["worker_id"],
        },
    },
    {
        "name": "foreman_collect",
        "description": "Collect worker status, changed files, and git diff for Codex QA.",
        "inputSchema": {
            "type": "object",
            "properties": {"worker_id": {"type": "string"}},
            "required": ["worker_id"],
        },
    },
    {
        "name": "foreman_finalize",
        "description": "Finalize a worker after Codex review: merge, open PR, or discard.",
        "inputSchema": {
            "type": "object",
            "properties": {"worker_id": {"type": "string"}, "action": {"type": "string", "enum": ["merge", "pr", "discard"]}},
            "required": ["worker_id", "action"],
        },
    },
]


def foreman_json(args: list[str]) -> dict[str, Any]:
    cp = subprocess.run([sys.executable, str(FOREMAN), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if cp.returncode != 0:
        return {"error": cp.stderr or cp.stdout, "returncode": cp.returncode}
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError:
        return {"stdout": cp.stdout, "stderr": cp.stderr}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "foreman_delegate":
        cmd = ["delegate"]
        if arguments.get("engine"):
            cmd += ["--engine", arguments["engine"]]
        if arguments.get("repo_path"):
            cmd += ["--repo", arguments["repo_path"]]
        if arguments.get("base_ref"):
            cmd += ["--base-ref", arguments["base_ref"]]
        if arguments.get("test_command"):
            cmd += ["--test-command", arguments["test_command"]]
        if arguments.get("timeout_sec") is not None:
            cmd += ["--timeout-sec", str(arguments["timeout_sec"])]
        if arguments.get("allow_dirty"):
            cmd += ["--allow-dirty"]
        cmd.append(arguments["spec"])
        return foreman_json(cmd)
    if name == "foreman_list":
        return foreman_json(["list"])
    if name == "foreman_tail":
        return foreman_json(["tail", arguments["worker_id"], "--lines", str(arguments.get("lines", 100))])
    if name == "foreman_monitor_hint":
        worker_id = arguments["worker_id"]
        return {
            "worker_id": worker_id,
            "watch_command": f"python3 {FOREMAN} watch {worker_id}",
            "tail_command": f"python3 {FOREMAN} tail {worker_id} --lines 120",
            "note": "watch streams logs in a terminal; MCP clients should use foreman_tail for snapshots.",
        }
    if name == "foreman_collect":
        return foreman_json(["collect", arguments["worker_id"]])
    if name == "foreman_finalize":
        return foreman_json(["finalize", arguments["worker_id"], arguments["action"]])
    return {"error": f"unknown tool: {name}"}


def respond(req: dict[str, Any], result: Any = None, error: Any = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req.get("id")}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        method = req.get("method")
        if method == "initialize":
            respond(
                req,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "foreman", "version": "0.3.1"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            respond(req, {"tools": TOOLS})
        elif method == "tools/call":
            params = req.get("params", {})
            payload = call_tool(params.get("name", ""), params.get("arguments", {}) or {})
            respond(req, {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]})
        else:
            respond(req, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
