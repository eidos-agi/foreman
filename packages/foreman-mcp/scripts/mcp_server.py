#!/usr/bin/env python3
"""Tiny MCP stdio server for Foreman."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
FOREMAN = REPO_ROOT / "scripts" / "foreman.py"
DEFAULT_MCP_TIMEOUT_SEC = float(os.environ.get("FOREMAN_MCP_TIMEOUT_SEC", "30"))
DELEGATE_MCP_TIMEOUT_SEC = float(os.environ.get("FOREMAN_MCP_DELEGATE_TIMEOUT_SEC", "45"))


TOOLS = [
    {
        "name": "foreman_delegate",
        "description": "Delegate an implementation task to an AI coding engine in an isolated git worktree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string", "description": "Concrete implementation spec with acceptance criteria and scope."},
                "engine": {
                    "type": "string",
                    "enum": ["claude", "codex", "gemini", "aider", "opencode", "gemma4", "smoke"],
                    "description": "Worker engine. Defaults to claude. Local engines opencode/gemma4 are throttled by Foreman.",
                },
                "repo_path": {"type": "string", "description": "Repository path. Defaults to MCP server cwd if omitted."},
                "base_ref": {"type": "string", "description": "Base branch/ref for the worktree. Defaults to current branch."},
                "test_command": {"type": "string", "description": "Verification command the engineer should run."},
                "timeout_sec": {"type": "integer", "description": "Maximum worker runtime in seconds. Defaults to 900."},
                "allow_dirty": {"type": "boolean", "description": "Allow delegating from a repo with uncommitted changes. Defaults to false."},
                "caller": {"type": "string", "description": "Optional label for the AI/app/person requesting this worker."},
                "parent": {"type": "string", "description": "Optional parent conversation/session/task identifier."},
                "run_id": {"type": "string", "description": "Optional shared run id. Workers with the same run id share a contract scratchpad."},
                "contract": {"type": "string", "description": "Shared contract text to create or append for this run."},
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
        "name": "foreman_control_submit",
        "description": "Submit a structured job into the portable SQLite-backed Foreman control plane without running it immediately.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string"},
                "engine": {"type": "string", "enum": ["claude", "codex", "gemini", "aider", "opencode", "gemma4", "smoke"]},
                "repo_path": {"type": "string"},
                "base_ref": {"type": "string"},
                "test_command": {"type": "string"},
                "timeout_sec": {"type": "integer"},
                "allow_dirty": {"type": "boolean"},
                "caller": {"type": "string"},
                "parent": {"type": "string"},
                "run_id": {"type": "string"},
                "contract": {"type": "string"},
            },
            "required": ["spec"],
        },
    },
    {
        "name": "foreman_control_jobs",
        "description": "List recent jobs in the SQLite-backed Foreman control plane.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}},
    },
    {
        "name": "foreman_control_status",
        "description": "Return a control-plane job with its event trail.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "foreman_agent_run_once",
        "description": "Lease and run at most one pending control-plane job on this machine.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "wait": {"type": "boolean", "default": False},
                "lease_sec": {"type": "integer", "default": 300},
            },
        },
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
        "name": "foreman_status",
        "description": "Return cheap durable status and paths for a worker without reading logs or diffs.",
        "inputSchema": {
            "type": "object",
            "properties": {"worker_id": {"type": "string"}},
            "required": ["worker_id"],
        },
    },
    {
        "name": "foreman_monitor_hint",
        "description": "Return terminal commands and a live local web URL for monitoring a worker; starts the Foreman daemon if needed.",
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
    {
        "name": "foreman_note",
        "description": "Append an operator note to a worker timeline without stopping the worker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "message": {"type": "string"},
                "actor": {"type": "string", "default": "mcp"},
            },
            "required": ["worker_id", "message"],
        },
    },
    {
        "name": "foreman_interrupt",
        "description": "Hard-stop a running worker, preserve logs/worktree, and mark it interrupted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "reason": {"type": "string"},
                "actor": {"type": "string", "default": "mcp"},
            },
            "required": ["worker_id", "reason"],
        },
    },
]


def foreman_json(args: list[str], timeout_sec: float = DEFAULT_MCP_TIMEOUT_SEC) -> dict[str, Any]:
    try:
        cp = subprocess.run(
            [sys.executable, str(FOREMAN), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "error": f"foreman command timed out after {timeout_sec:g}s",
            "timed_out": True,
            "command": args,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "hint": "Use foreman_list or foreman_status with a known worker_id; workers continue from durable state after launch.",
        }
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
        caller = arguments.get("caller") or os.environ.get("FOREMAN_CALLER") or "mcp:unknown-client"
        parent = arguments.get("parent") or os.environ.get("FOREMAN_PARENT")
        cmd += ["--caller", caller]
        if parent:
            cmd += ["--parent", parent]
        if arguments.get("run_id"):
            cmd += ["--run-id", arguments["run_id"]]
        if arguments.get("contract"):
            cmd += ["--contract", arguments["contract"]]
        cmd.append(arguments["spec"])
        return foreman_json(cmd, timeout_sec=DELEGATE_MCP_TIMEOUT_SEC)
    if name == "foreman_list":
        return foreman_json(["list"])
    if name == "foreman_control_submit":
        cmd = ["control-submit"]
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
        if arguments.get("caller"):
            cmd += ["--caller", arguments["caller"]]
        if arguments.get("parent"):
            cmd += ["--parent", arguments["parent"]]
        if arguments.get("run_id"):
            cmd += ["--run-id", arguments["run_id"]]
        if arguments.get("contract"):
            cmd += ["--contract", arguments["contract"]]
        cmd.append(arguments["spec"])
        return foreman_json(cmd)
    if name == "foreman_control_jobs":
        return foreman_json(["control-jobs", "--limit", str(arguments.get("limit", 20))])
    if name == "foreman_control_status":
        return foreman_json(["control-status", arguments["job_id"]])
    if name == "foreman_agent_run_once":
        cmd = ["agent-run", "--once"]
        if arguments.get("agent_id"):
            cmd += ["--agent-id", arguments["agent_id"]]
        if arguments.get("wait"):
            cmd += ["--wait"]
        if arguments.get("lease_sec") is not None:
            cmd += ["--lease-sec", str(arguments["lease_sec"])]
        return foreman_json(cmd, timeout_sec=DELEGATE_MCP_TIMEOUT_SEC)
    if name == "foreman_tail":
        return foreman_json(["tail", arguments["worker_id"], "--lines", str(arguments.get("lines", 100))])
    if name == "foreman_status":
        return foreman_json(["status", arguments["worker_id"]])
    if name == "foreman_monitor_hint":
        return foreman_json(["monitor-hint", arguments["worker_id"]])
    if name == "foreman_collect":
        return foreman_json(["collect", arguments["worker_id"]])
    if name == "foreman_finalize":
        return foreman_json(["finalize", arguments["worker_id"], arguments["action"]])
    if name == "foreman_note":
        cmd = ["note", arguments["worker_id"], arguments["message"], "--actor", arguments.get("actor", "mcp")]
        return foreman_json(cmd)
    if name == "foreman_interrupt":
        cmd = ["interrupt", arguments["worker_id"], arguments["reason"], "--actor", arguments.get("actor", "mcp")]
        return foreman_json(cmd)
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
