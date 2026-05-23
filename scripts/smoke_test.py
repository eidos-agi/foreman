#!/usr/bin/env python3
"""End-to-end smoke test for Foreman local control-plane plumbing."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run(args: list[str], *, env: dict[str, str], timeout: int = 30, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        args,
        cwd=REPO,
        env=env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if cp.returncode != 0:
        raise AssertionError(
            "command failed\n"
            f"cmd: {' '.join(args)}\n"
            f"returncode: {cp.returncode}\n"
            f"stdout:\n{cp.stdout}\n"
            f"stderr:\n{cp.stderr}"
        )
    return cp


def run_json(args: list[str], *, env: dict[str, str], timeout: int = 30) -> dict[str, Any]:
    cp = run(args, env=env, timeout=timeout)
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"expected JSON from {' '.join(args)}; got:\n{cp.stdout}") from exc


def http_json(url: str, *, data: dict[str, Any] | None = None, expected_status: int = 200) -> dict[str, Any]:
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = json.loads(exc.read().decode("utf-8"))
    if status != expected_status:
        raise AssertionError(f"expected HTTP {expected_status} from {url}, got {status}: {payload}")
    return payload


def wait_for_removed(path: Path, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not path.exists():
            return True
        time.sleep(0.1)
    return not path.exists()


def main() -> int:
    temp_home = Path(tempfile.mkdtemp(prefix="foreman-smoke."))
    env = os.environ.copy()
    env["FOREMAN_HOME"] = str(temp_home)

    checks: list[str] = []

    try:
        compile_targets = [
            "scripts/foreman.py",
            "scripts/mcp_server.py",
            "scripts/smoke_engineer.py",
            "scripts/sleep_engineer.py",
            "packages/foreman-cli/scripts/foreman.py",
            "packages/foreman-cli/scripts/smoke_engineer.py",
            "packages/foreman-cli/scripts/sleep_engineer.py",
            "packages/foreman-mcp/scripts/mcp_server.py",
            "packages/foreman-control-plane/scripts/control.py",
            "packages/foreman-agent/scripts/agent.py",
        ]
        run([PYTHON, "-m", "py_compile", *compile_targets], env=env)
        checks.append("py_compile")

        init_payload = run_json([PYTHON, "packages/foreman-control-plane/scripts/control.py", "init"], env=env)
        assert init_payload["status"] == "ready"
        checks.append("control init wrapper")

        smoke_payload = run_json(
            [
                PYTHON,
                "packages/foreman-control-plane/scripts/control.py",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--once-smoke",
            ],
            env=env,
        )
        assert smoke_payload["status"] == "ok"
        checks.append("control serve once-smoke")

        hint_payload = run_json(
            [
                PYTHON,
                "packages/foreman-control-plane/scripts/control.py",
                "hint",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
            ],
            env=env,
        )
        control_pid = int(hint_payload["control_plane"]["pid"])
        control_url = hint_payload["url"]
        assert http_json(f"{control_url}/api/health")["ok"] is True
        assert http_json(f"{control_url}/api/control/jobs?limit=1")["jobs"] == []
        submitted = http_json(
            f"{control_url}/api/control/jobs",
            data={
                "repo": str(REPO),
                "engine": "smoke",
                "spec": "HTTP API smoke job",
                "test_command": "true",
                "allow_dirty": True,
                "caller": "foreman-smoke-test",
                "parent": "scripts/smoke_test.py",
                "run_id": "foreman-smoke-http",
            },
            expected_status=201,
        )
        job_id = submitted["job_id"]
        assert http_json(f"{control_url}/api/control/jobs/{job_id}")["job"]["job_id"] == job_id
        assert "error" in http_json(f"{control_url}/api/control/jobs", data={"engine": "smoke"}, expected_status=400)
        checks.append("control HTTP API")

        os.kill(control_pid, signal.SIGTERM)
        assert wait_for_removed(temp_home / "control-plane.json"), "control-plane state file was not removed after SIGTERM"
        checks.append("control service cleanup")

        job = run_json(
            [
                PYTHON,
                "packages/foreman-control-plane/scripts/control.py",
                "submit",
                "--repo",
                str(REPO),
                "--engine",
                "smoke",
                "--allow-dirty",
                "--timeout-sec",
                "20",
                "--test-command",
                "true",
                "--caller",
                "foreman-smoke-test",
                "--parent",
                "scripts/smoke_test.py",
                "--run-id",
                "foreman-smoke-agent",
                "No-edit smoke job through package control and agent wrappers.",
            ],
            env=env,
        )
        assert job["status"] == "pending"
        agent = run_json(
            [
                PYTHON,
                "packages/foreman-agent/scripts/agent.py",
                "--once",
                "--wait",
                "--agent-id",
                "smoke-local-agent",
                "--poll-interval-sec",
                "0.1",
            ],
            env=env,
            timeout=60,
        )
        assert agent["status"] == "done"
        worker_id = agent["worker"]["worker_id"]
        assert run_json([PYTHON, "scripts/foreman.py", "status", worker_id], env=env)["status"] == "done"
        checks.append("agent leases and runs smoke worker")

        monitor = run_json([PYTHON, "scripts/foreman.py", "monitor-hint", worker_id], env=env)
        monitor_url = monitor["daemon"]["url"].rstrip("/")
        assert http_json(f"{monitor_url}/api/health")["ok"] is True
        assert http_json(f"{monitor_url}/api/workers")["workers"]
        checks.append("worker monitor daemon")

        mcp_input = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                "",
            ]
        )
        mcp = run([PYTHON, "scripts/mcp_server.py"], env=env, input_text=mcp_input)
        assert "foreman_control_hint" in mcp.stdout
        assert "foreman_agent_run_once" in mcp.stdout
        checks.append("MCP tools list")

        print(json.dumps({"status": "ok", "checks": checks, "foreman_home": str(temp_home)}, indent=2))
        return 0
    finally:
        for state_file in (temp_home / "daemon.json", temp_home / "control-plane.json"):
            if not state_file.exists():
                continue
            try:
                pid = int(json.loads(state_file.read_text(encoding="utf-8")).get("pid") or 0)
                if pid:
                    os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        shutil.rmtree(temp_home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
