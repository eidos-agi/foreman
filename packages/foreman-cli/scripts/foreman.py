#!/usr/bin/env python3
"""Local worktree Foreman for multi-agent coding delegation."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import http.server
import json
import os
import signal
import shlex
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STATE_HOME = Path(os.environ.get("FOREMAN_HOME", "~/.foreman")).expanduser()
DB_PATH = STATE_HOME / "foreman.sqlite3"
WORKTREES_HOME = STATE_HOME / "worktrees"
LOGS_HOME = STATE_HOME / "logs"
RUNS_HOME = STATE_HOME / "runs"
DAEMON_PATH = STATE_HOME / "daemon.json"
DAEMON_LOG_PATH = LOGS_HOME / "daemon.log"
MAX_TAIL_BYTES = 2_000_000
ENGINES = ("claude", "codex", "gemini", "aider", "opencode", "gemma4", "smoke")
LOCAL_ENGINE_RESOURCE_GROUPS = {"opencode": "local-agent", "gemma4": "local-agent"}
DEFAULT_TIMEOUT_SEC = 900
DEFAULT_DAEMON_PORT = 53631
DEFAULT_DAEMON_IDLE_TIMEOUT_SEC = 600
DAEMON_START_TIMEOUT_SEC = 5.0
DEFAULT_LOCAL_ENGINE_MAX_RUNNING = 1
DEFAULT_LOCAL_ENGINE_NICE = 10


@dataclass(frozen=True)
class ControlJobSpec:
    repo_path: str
    engine: str
    spec: str
    base_ref: str | None = None
    test_command: str | None = None
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    caller: str = ""
    parent: str = ""
    run_id: str = ""
    contract: str = ""
    allow_dirty: bool = False


def now() -> int:
    return int(time.time())


def run_git(repo: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def init_db() -> None:
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    WORKTREES_HOME.mkdir(parents=True, exist_ok=True)
    LOGS_HOME.mkdir(parents=True, exist_ok=True)
    RUNS_HOME.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY,
                engine TEXT NOT NULL DEFAULT 'claude',
                repo_path TEXT NOT NULL,
                base_ref TEXT NOT NULL,
                branch TEXT NOT NULL,
                worktree_path TEXT NOT NULL,
                spec TEXT NOT NULL,
                test_command TEXT,
                timeout_sec INTEGER NOT NULL DEFAULT 900,
                caller TEXT NOT NULL DEFAULT '',
                parent TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                scratchpad_path TEXT NOT NULL DEFAULT '',
                worker_note_path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                pid INTEGER,
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER,
                exit_code INTEGER,
                log_path TEXT NOT NULL,
                prompt_path TEXT NOT NULL,
                result_path TEXT NOT NULL
            )
            """
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(workers)").fetchall()}
        def refresh_columns() -> set[str]:
            return {row[1] for row in db.execute("PRAGMA table_info(workers)").fetchall()}

        def add_column_once(name: str, ddl: str) -> None:
            nonlocal columns
            if name in columns:
                return
            try:
                db.execute(ddl)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
            columns = refresh_columns()

        add_column_once("engine", "ALTER TABLE workers ADD COLUMN engine TEXT NOT NULL DEFAULT 'claude'")
        add_column_once("timeout_sec", "ALTER TABLE workers ADD COLUMN timeout_sec INTEGER NOT NULL DEFAULT 900")
        add_column_once("caller", "ALTER TABLE workers ADD COLUMN caller TEXT NOT NULL DEFAULT ''")
        add_column_once("parent", "ALTER TABLE workers ADD COLUMN parent TEXT NOT NULL DEFAULT ''")
        add_column_once("run_id", "ALTER TABLE workers ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
        add_column_once("scratchpad_path", "ALTER TABLE workers ADD COLUMN scratchpad_path TEXT NOT NULL DEFAULT ''")
        add_column_once("worker_note_path", "ALTER TABLE workers ADD COLUMN worker_note_path TEXT NOT NULL DEFAULT ''")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS control_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                engine TEXT NOT NULL DEFAULT 'claude',
                base_ref TEXT,
                spec TEXT NOT NULL,
                test_command TEXT,
                timeout_sec INTEGER NOT NULL DEFAULT 900,
                caller TEXT NOT NULL DEFAULT '',
                parent TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                contract TEXT NOT NULL DEFAULT '',
                allow_dirty INTEGER NOT NULL DEFAULT 0,
                worker_id TEXT,
                lease_owner TEXT,
                lease_expires_at INTEGER,
                created_at INTEGER NOT NULL,
                leased_at INTEGER,
                started_at INTEGER,
                finished_at INTEGER,
                error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS control_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL
            )
            """
        )


def db_row(worker_id: str) -> sqlite3.Row:
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown worker_id: {worker_id}")
    return row


def update_worker(worker_id: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    names = sorted(fields)
    sql = ", ".join(f"{name} = ?" for name in names)
    values = [fields[name] for name in names]
    with sqlite3.connect(DB_PATH) as db:
        db.execute(f"UPDATE workers SET {sql} WHERE id = ?", [*values, worker_id])


def append_control_event(job_id: str, event_type: str, message: str = "", payload: dict[str, Any] | None = None) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO control_events (job_id, event_type, message, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, event_type, message, json.dumps(payload or {}, sort_keys=True), now()),
        )


def update_control_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    names = sorted(fields)
    sql = ", ".join(f"{name} = ?" for name in names)
    values = [fields[name] for name in names]
    with sqlite3.connect(DB_PATH) as db:
        db.execute(f"UPDATE control_jobs SET {sql} WHERE id = ?", [*values, job_id])


def control_job_payload(row: sqlite3.Row, include_events: bool = False) -> dict[str, Any]:
    payload = {
        "job_id": row["id"],
        "status": row["status"],
        "repo_path": row["repo_path"],
        "engine": row["engine"],
        "base_ref": row["base_ref"],
        "spec": row["spec"],
        "test_command": row["test_command"],
        "timeout_sec": row["timeout_sec"],
        "caller": row["caller"],
        "parent": row["parent"],
        "run_id": row["run_id"],
        "allow_dirty": bool(row["allow_dirty"]),
        "worker_id": row["worker_id"],
        "lease_owner": row["lease_owner"],
        "lease_expires_at": row["lease_expires_at"],
        "created_at": row["created_at"],
        "leased_at": row["leased_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
    }
    if include_events:
        with sqlite3.connect(DB_PATH) as db:
            db.row_factory = sqlite3.Row
            events = db.execute(
                "SELECT * FROM control_events WHERE job_id = ? ORDER BY id ASC",
                (row["id"],),
            ).fetchall()
        payload["events"] = [
            {
                "id": event["id"],
                "event_type": event["event_type"],
                "message": event["message"],
                "payload": json.loads(event["payload_json"] or "{}"),
                "created_at": event["created_at"],
            }
            for event in events
        ]
    return payload


def control_init(_: argparse.Namespace) -> dict[str, Any]:
    init_db()
    return {
        "status": "ready",
        "state_home": str(STATE_HOME),
        "sqlite_path": str(DB_PATH),
        "note": "This SQLite file is the portable Foreman control-plane state bundle.",
    }


def control_job_spec_from_args(args: argparse.Namespace) -> ControlJobSpec:
    repo = normalize_repo(args.repo)
    return ControlJobSpec(
        repo_path=str(repo),
        engine=args.engine,
        spec=args.spec,
        base_ref=args.base_ref,
        test_command=args.test_command,
        timeout_sec=args.timeout_sec,
        caller=args.caller or caller_label(args),
        parent=args.parent or parent_label(args),
        run_id=args.run_id or "",
        contract=args.contract or "",
        allow_dirty=bool(args.allow_dirty),
    )


def submit_control_job(job_spec: ControlJobSpec) -> dict[str, Any]:
    init_db()
    job_id = "job-" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    run_id = job_spec.run_id or f"run-{job_id}"
    created = now()
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO control_jobs
            (id, status, repo_path, engine, base_ref, spec, test_command, timeout_sec, caller, parent, run_id, contract,
             allow_dirty, created_at)
            VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                job_spec.repo_path,
                job_spec.engine,
                job_spec.base_ref,
                job_spec.spec,
                job_spec.test_command,
                job_spec.timeout_sec,
                job_spec.caller,
                job_spec.parent,
                run_id,
                job_spec.contract,
                1 if job_spec.allow_dirty else 0,
                created,
            ),
        )
    append_control_event(job_id, "submitted", "job submitted to Foreman control plane")
    return {
        "job_id": job_id,
        "status": "pending",
        "repo_path": job_spec.repo_path,
        "engine": job_spec.engine,
        "run_id": run_id,
        "sqlite_path": str(DB_PATH),
        "next": {
            "run_agent_once": f"python3 {Path(__file__).resolve()} agent-run --once --wait",
            "status": f"python3 {Path(__file__).resolve()} control-status {job_id}",
        },
    }


def control_submit(args: argparse.Namespace) -> dict[str, Any]:
    return submit_control_job(control_job_spec_from_args(args))


def control_jobs(args: argparse.Namespace) -> dict[str, Any]:
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM control_jobs ORDER BY created_at DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    return {"jobs": [control_job_payload(row) for row in rows]}


def control_status(args: argparse.Namespace) -> dict[str, Any]:
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM control_jobs WHERE id = ?", (args.job_id,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown job_id: {args.job_id}")
    return {"job": control_job_payload(row, include_events=True)}


def control_job_spec_from_body(body: dict[str, Any]) -> ControlJobSpec:
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    spec = body.get("spec")
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("spec is required and must be a non-empty string")
    repo_input = body.get("repo_path") or body.get("repo")
    repo = normalize_repo(repo_input if isinstance(repo_input, str) else None)
    engine = body.get("engine") or "claude"
    if not isinstance(engine, str) or engine not in ENGINES:
        raise ValueError(f"engine must be one of {list(ENGINES)}")
    timeout_value = body.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
    try:
        timeout_sec = int(timeout_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"timeout_sec must be an integer: {timeout_value!r}") from exc

    def _opt_str(key: str) -> str | None:
        value = body.get(key)
        if value is None:
            return None
        return str(value)

    return ControlJobSpec(
        repo_path=str(repo),
        engine=engine,
        spec=spec,
        base_ref=_opt_str("base_ref"),
        test_command=_opt_str("test_command"),
        timeout_sec=timeout_sec,
        caller=str(body.get("caller") or ""),
        parent=str(body.get("parent") or ""),
        run_id=str(body.get("run_id") or ""),
        contract=str(body.get("contract") or ""),
        allow_dirty=bool(body.get("allow_dirty")),
    )


def control_serve(args: argparse.Namespace) -> dict[str, Any] | None:
    init_db()

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "ForemanControlPlane/0.1"

        def log_message(self, fmt: str, *values: Any) -> None:
            if not args.quiet:
                super().log_message(fmt, *values)

        def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON body: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if path == "/api/health":
                self.send_json(
                    {
                        "ok": True,
                        "state_home": str(STATE_HOME),
                        "sqlite_path": str(DB_PATH),
                        "pid": os.getpid(),
                    }
                )
                return
            if path == "/api/control/jobs":
                limit_raw = query.get("limit", ["20"])[0]
                try:
                    limit = int(limit_raw)
                except ValueError:
                    self.send_json({"error": f"invalid limit: {limit_raw!r}"}, status=400)
                    return
                self.send_json(control_jobs(argparse.Namespace(limit=limit)))
                return
            if path.startswith("/api/control/jobs/"):
                job_id = path[len("/api/control/jobs/"):].strip("/")
                if not job_id or "/" in job_id:
                    self.send_json({"error": "not found"}, status=404)
                    return
                try:
                    self.send_json(control_status(argparse.Namespace(job_id=job_id)))
                except SystemExit as exc:
                    self.send_json({"error": str(exc)}, status=404)
                return
            self.send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/api/control/jobs":
                try:
                    body = self.read_json_body()
                    job_spec = control_job_spec_from_body(body)
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, status=400)
                    return
                self.send_json(submit_control_job(job_spec), status=201)
                return
            self.send_json({"error": "not found"}, status=404)

    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    server.timeout = 1.0
    host, port = server.server_address
    url = f"http://{host}:{port}"

    if args.once_smoke:
        try:
            return {
                "status": "ok",
                "url": url,
                "host": host,
                "port": port,
                "state_home": str(STATE_HOME),
                "sqlite_path": str(DB_PATH),
                "pid": os.getpid(),
            }
        finally:
            server.server_close()

    if not args.quiet:
        print(f"[foreman-control-serve] {url}", flush=True)
        print(f"[foreman-control-serve] state_home={STATE_HOME}", flush=True)
    try:
        while True:
            server.handle_request()
    except KeyboardInterrupt:
        if not args.quiet:
            print("\n[foreman-control-serve] stopped", flush=True)
    finally:
        server.server_close()
    return None


def lease_control_job(agent_id: str, lease_sec: int) -> sqlite3.Row | None:
    init_db()
    deadline = now() + lease_sec
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """
            SELECT * FROM control_jobs
            WHERE status = 'pending'
               OR (status = 'leased' AND COALESCE(lease_expires_at, 0) < ?)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (now(),),
        ).fetchone()
        if row is None:
            db.commit()
            return None
        db.execute(
            """
            UPDATE control_jobs
            SET status = 'leased', lease_owner = ?, lease_expires_at = ?, leased_at = ?
            WHERE id = ?
            """,
            (agent_id, deadline, now(), row["id"]),
        )
        db.commit()
    append_control_event(row["id"], "leased", f"leased to {agent_id}", {"lease_expires_at": deadline})
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM control_jobs WHERE id = ?", (row["id"],)).fetchone()


def delegate_command_for_control_job(row: sqlite3.Row) -> list[str]:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "delegate",
        "--repo",
        row["repo_path"],
        "--engine",
        row["engine"],
        "--timeout-sec",
        str(row["timeout_sec"]),
        "--caller",
        row["caller"] or "foreman-agent",
        "--parent",
        row["parent"] or f"control:{row['id']}",
        "--run-id",
        row["run_id"] or f"run-{row['id']}",
    ]
    if row["base_ref"]:
        cmd.extend(["--base-ref", row["base_ref"]])
    if row["test_command"]:
        cmd.extend(["--test-command", row["test_command"]])
    if row["contract"]:
        cmd.extend(["--contract", row["contract"]])
    if row["allow_dirty"]:
        cmd.append("--allow-dirty")
    cmd.append(row["spec"])
    return cmd


def control_status_from_worker_status(status: str) -> str:
    return "done" if status in {"done", "merged", "pr_opened"} else "failed"


def run_control_job(row: sqlite3.Row, wait: bool, poll_interval_sec: float) -> dict[str, Any]:
    job_id = row["id"]
    update_control_job(job_id, status="starting", started_at=now())
    append_control_event(job_id, "starting", "agent is delegating job to local Foreman runtime")
    cmd = delegate_command_for_control_job(row)
    cp = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if cp.returncode != 0:
        update_control_job(job_id, status="failed", finished_at=now(), error=cp.stderr.strip() or cp.stdout.strip())
        append_control_event(job_id, "failed", "delegate command failed", {"stderr": cp.stderr, "stdout": cp.stdout})
        return {"job_id": job_id, "status": "failed", "stderr": cp.stderr, "stdout": cp.stdout}
    try:
        delegate_payload = json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        update_control_job(job_id, status="failed", finished_at=now(), error=f"delegate returned non-json: {exc}")
        append_control_event(job_id, "failed", "delegate returned non-json", {"stdout": cp.stdout})
        return {"job_id": job_id, "status": "failed", "stdout": cp.stdout}
    worker_id = delegate_payload["worker_id"]
    update_control_job(job_id, status="running", worker_id=worker_id)
    append_control_event(job_id, "worker_started", "worker started", delegate_payload)
    if not wait:
        return {"job_id": job_id, "status": "running", "worker": delegate_payload}

    while True:
        worker_row = db_row(worker_id)
        status = worker_status(worker_row)
        if status != "running":
            final_status = control_status_from_worker_status(status)
            update_control_job(job_id, status=final_status, finished_at=now())
            append_control_event(job_id, "worker_finished", f"worker finished with status {status}", worker_payload(db_row(worker_id)))
            return {"job_id": job_id, "status": final_status, "worker": worker_payload(db_row(worker_id))}
        time.sleep(poll_interval_sec)


def agent_run(args: argparse.Namespace) -> dict[str, Any]:
    agent_id = args.agent_id or f"{os.uname().nodename}:{os.getpid()}"
    results = []
    while True:
        job = lease_control_job(agent_id, args.lease_sec)
        if job is None:
            if args.once:
                return {"agent_id": agent_id, "status": "idle", "job": None, "results": results}
            time.sleep(args.idle_sleep_sec)
            continue
        result = run_control_job(job, wait=args.wait, poll_interval_sec=args.poll_interval_sec)
        result["agent_id"] = agent_id
        results.append(result)
        if args.once:
            return result
        time.sleep(args.idle_sleep_sec)


def append_worker_log(worker_id: str, line: str) -> None:
    row = db_row(worker_id)
    path = Path(row["log_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")


def add_worker_note(worker_id: str, message: str, actor: str = "operator") -> dict[str, Any]:
    message = " ".join(str(message or "").split())
    if not message:
        raise SystemExit("note cannot be empty")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    append_worker_log(worker_id, f"[operator] {ts} {actor}: {message}")
    return {"worker_id": worker_id, "action": "note", "actor": actor, "message": message}


def interrupt_worker(worker_id: str, reason: str = "", actor: str = "operator") -> dict[str, Any]:
    row = db_row(worker_id)
    status = worker_status(row)
    reason = " ".join(str(reason or "").split()) or "No reason provided."
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    append_worker_log(worker_id, f"[operator] {ts} {actor}: HARD STOP requested. Reason: {reason}")
    if status != "running":
        return {"worker_id": worker_id, "action": "interrupt", "status": status, "stopped": False, "reason": reason}
    pid = row["pid"]
    stopped = False
    if pid:
        try:
            os.killpg(int(pid), signal.SIGTERM)
            stopped = True
        except ProcessLookupError:
            stopped = False
        except PermissionError as exc:
            raise SystemExit(f"cannot interrupt worker {worker_id}: {exc}") from exc
    finished = now()
    result = {
        "worker_id": worker_id,
        "started_at": row["started_at"],
        "finished_at": finished,
        "exit_code": 130,
        "timed_out": False,
        "interrupted": True,
        "interrupt_reason": reason,
        "interrupt_actor": actor,
    }
    Path(row["result_path"]).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    update_worker(worker_id, status="interrupted", finished_at=finished, exit_code=130)
    append_worker_log(worker_id, f"[operator] {ts} {actor}: worker marked interrupted; worktree preserved.")
    return {"worker_id": worker_id, "action": "interrupt", "status": "interrupted", "stopped": stopped, "reason": reason}


def normalize_repo(repo_path: str | None) -> Path:
    repo = Path(repo_path or os.getcwd()).expanduser().resolve()
    cp = run_git(repo, ["rev-parse", "--show-toplevel"], check=False)
    if cp.returncode != 0:
        return repo
    top = cp.stdout.strip()
    return Path(top).resolve()


def current_branch(repo: Path) -> str:
    cp = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    if cp.returncode != 0:
        return "HEAD"
    branch = cp.stdout.strip()
    return "HEAD" if branch == "HEAD" else branch


def preflight(repo: Path, base_ref: str, engine: str, allow_dirty: bool) -> None:
    failures = []
    if not repo.exists():
        failures.append(f"repo does not exist: {repo}")
    if run_git(repo, ["rev-parse", "--is-inside-work-tree"], check=False).stdout.strip() != "true":
        failures.append(f"not a git repository: {repo}")
    if run_git(repo, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"], check=False).returncode != 0:
        failures.append(f"base ref is not a commit: {base_ref}")
    if not allow_dirty:
        dirty = run_git(repo, ["status", "--porcelain"], check=False).stdout.strip()
        if dirty:
            failures.append("repo has uncommitted changes; commit/stash them or pass --allow-dirty")
    try:
        argv = engine_command(engine, "preflight")
    except SystemExit as exc:
        failures.append(str(exc))
    else:
        exe = argv[0]
        if Path(exe).is_absolute():
            if not Path(exe).exists():
                failures.append(f"engine executable not found: {exe}")
        elif shutil.which(exe) is None:
            failures.append(f"engine executable not on PATH: {exe}")
    if failures:
        raise SystemExit("foreman preflight failed:\n- " + "\n- ".join(failures))


def env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def engine_resource_group(engine: str) -> str | None:
    return LOCAL_ENGINE_RESOURCE_GROUPS.get(engine)


def engine_resource_limit(engine: str) -> int:
    group = engine_resource_group(engine)
    if not group:
        return 0
    engine_specific = os.environ.get(f"FOREMAN_ENGINE_{engine.upper()}_MAX_RUNNING")
    if engine_specific is not None:
        try:
            return max(1, int(engine_specific))
        except ValueError:
            return DEFAULT_LOCAL_ENGINE_MAX_RUNNING
    return env_int("FOREMAN_LOCAL_ENGINE_MAX_RUNNING", DEFAULT_LOCAL_ENGINE_MAX_RUNNING, minimum=1, maximum=8)


@contextlib.contextmanager
def engine_resource_slot(engine: str):
    """Serialize heavy local engines so delegated workers do not swamp this Mac."""
    group = engine_resource_group(engine)
    if not group:
        yield None
        return

    limit = engine_resource_limit(engine)
    lock_dir = STATE_HOME / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handles = []
    slot_handle = None
    slot_name = ""
    try:
        while slot_handle is None:
            for idx in range(limit):
                handle = (lock_dir / f"{group}-{idx}.lock").open("w", encoding="utf-8")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handles.append(handle)
                    continue
                slot_handle = handle
                slot_name = f"{group}-{idx + 1}/{limit}"
                break
            if slot_handle is None:
                for handle in handles:
                    handle.close()
                handles = []
                print(f"[foreman] waiting for local engine slot: {group} max_running={limit}", flush=True)
                time.sleep(5)
        print(f"[foreman] acquired local engine slot: {slot_name}", flush=True)
        yield slot_name
    finally:
        for handle in handles:
            handle.close()
        if slot_handle is not None:
            fcntl.flock(slot_handle, fcntl.LOCK_UN)
            slot_handle.close()
            print(f"[foreman] released local engine slot: {slot_name}", flush=True)


def engine_env(engine: str) -> dict[str, str]:
    env = os.environ.copy()
    if engine_resource_group(engine):
        env.setdefault("OMP_NUM_THREADS", os.environ.get("FOREMAN_LOCAL_ENGINE_THREADS", "2"))
        env.setdefault("OPENBLAS_NUM_THREADS", env["OMP_NUM_THREADS"])
        env.setdefault("MKL_NUM_THREADS", env["OMP_NUM_THREADS"])
        env.setdefault("VECLIB_MAXIMUM_THREADS", env["OMP_NUM_THREADS"])
        env.setdefault("NUMEXPR_NUM_THREADS", env["OMP_NUM_THREADS"])
        env.setdefault("OLLAMA_NUM_PARALLEL", "1")
        env.setdefault("OLLAMA_MAX_LOADED_MODELS", "1")
    return env


def low_impact_command(engine: str, argv: list[str]) -> list[str]:
    if not engine_resource_group(engine):
        return argv
    nice_value = env_int(f"FOREMAN_ENGINE_{engine.upper()}_NICE", env_int("FOREMAN_LOCAL_ENGINE_NICE", DEFAULT_LOCAL_ENGINE_NICE))
    if nice_value <= 0 or shutil.which("nice") is None:
        return argv
    return ["nice", "-n", str(nice_value), *argv]


def safe_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:120] or "run"


def ensure_run_contract(run_id: str, contract_text: str = "") -> Path:
    run_dir = RUNS_HOME / safe_id(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    contract_path = run_dir / "contract.md"
    placeholder = "No shared contract text was provided for this run yet."
    lock_path = run_dir / ".contract.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not contract_path.exists():
            body = contract_text.strip() or placeholder
            contract_path.write_text(
                "# Foreman Run Contract\n\n"
                f"- run_id: {run_id}\n"
                f"- created_at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "## Shared Contract\n\n"
                f"{body}\n\n"
                "## Contract Rules\n\n"
                "- This file is shared context for workers in the same run.\n"
                "- Workers should treat it as the public interface contract.\n"
                "- Workers should write uncertainties to their private notes and final report.\n"
                "- The merger agent owns final reconciliation.\n",
                encoding="utf-8",
            )
        elif contract_text.strip():
            existing = contract_path.read_text(encoding="utf-8")
            if placeholder in existing:
                existing = existing.replace(placeholder, contract_text.strip(), 1)
                contract_path.write_text(existing, encoding="utf-8")
            elif contract_text.strip() not in existing:
                with contract_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        "\n## Contract Addendum\n\n"
                        f"- added_at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                        f"{contract_text.strip()}\n"
                    )
        fcntl.flock(lock, fcntl.LOCK_UN)
    return contract_path


def install_worker_coordination_files(worktree: Path, worker_id: str, contract_path: Path) -> tuple[Path, Path]:
    coord_dir = worktree / ".foreman"
    coord_dir.mkdir(parents=True, exist_ok=True)
    worker_contract = coord_dir / "contract.md"
    worker_notes = coord_dir / "worker-notes.md"
    worker_contract.write_text(contract_path.read_text(encoding="utf-8"), encoding="utf-8")
    worker_notes.write_text(
        "# Worker Notes\n\n"
        f"- worker_id: {worker_id}\n"
        f"- created_at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "Append private notes here if the shared contract is incomplete, ambiguous, or wrong.\n",
        encoding="utf-8",
    )
    return worker_contract, worker_notes


def make_prompt(
    spec: str,
    test_command: str | None,
    contract_text: str = "",
    worker_contract_path: str = "",
    worker_note_path: str = "",
) -> str:
    test_block = test_command or "No explicit test command was provided. Run the smallest relevant verification and report what you ran."
    contract_block = ""
    if contract_text:
        contract_block = textwrap.dedent(
            f"""

            Shared contract for this Foreman run:
            {contract_text.strip()}

            Local coordination files:
            - Shared contract copy: {worker_contract_path or "not copied"}
            - Your private worker notes: {worker_note_path or "not copied"}

            Treat the shared contract as the current public interface between workers. Do not invent cross-worker APIs
            that contradict it. If you discover a contract issue, write it in your private worker notes and report it
            in your final answer; the merger agent owns reconciliation.
            """
        ).rstrip()
    return textwrap.dedent(
        f"""
        You are an implementation engineer working for Foreman.

        Stay inside this git worktree. Do not edit files outside the repository. Do not commit, push, open PRs,
        or change remotes. Make the requested file changes, run verification, and return a concise final report.

        Task spec:
        {spec.strip()}
        {contract_block}

        Acceptance and reporting contract:
        - Keep changes scoped to the task.
        - Run this verification before declaring done: {test_block}
        - If blocked, leave the worktree in the best useful state and explain the blocker.
        - End with: changed files, verification output, and open questions.
        """
    ).strip()


def delegate(args: argparse.Namespace) -> dict[str, Any]:
    init_db()
    repo = normalize_repo(args.repo)
    base_ref = args.base_ref or current_branch(repo)
    engine = args.engine
    caller = caller_label(args)
    parent = parent_label(args)
    preflight(repo, base_ref, engine, args.allow_dirty)
    worker_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    repo_name = repo.name
    branch = f"foreman/{worker_id}"
    worktree = (WORKTREES_HOME / repo_name / worker_id).resolve()
    log_path = (LOGS_HOME / f"{worker_id}.log").resolve()
    prompt_path = (LOGS_HOME / f"{worker_id}.prompt.md").resolve()
    result_path = (LOGS_HOME / f"{worker_id}.result.json").resolve()
    run_id = str(getattr(args, "run_id", None) or f"run-{worker_id}")
    contract_path = ensure_run_contract(run_id, getattr(args, "contract", None) or "")

    worktree.parent.mkdir(parents=True, exist_ok=True)

    try:
        run_git(repo, ["worktree", "add", "-b", branch, str(worktree), base_ref])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"git worktree add failed:\n{exc.stderr}") from exc

    (worktree / ".foreman-worker-id").write_text(worker_id + "\n", encoding="utf-8")
    worker_contract, worker_notes = install_worker_coordination_files(worktree, worker_id, contract_path)
    prompt_path.write_text(
        make_prompt(
            args.spec,
            args.test_command,
            contract_text=contract_path.read_text(encoding="utf-8"),
            worker_contract_path=str(worker_contract),
            worker_note_path=str(worker_notes),
        ),
        encoding="utf-8",
    )

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO workers
            (id, engine, repo_path, base_ref, branch, worktree_path, spec, test_command, timeout_sec, caller, parent,
             run_id, scratchpad_path, worker_note_path, status, pid, created_at, started_at, finished_at, exit_code,
             log_path, prompt_path, result_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', NULL, ?, ?, NULL, NULL, ?, ?, ?)
            """,
            (
                worker_id,
                engine,
                str(repo),
                base_ref,
                branch,
                str(worktree),
                args.spec,
                args.test_command,
                args.timeout_sec,
                caller,
                parent,
                run_id,
                str(contract_path),
                str(worker_notes),
                now(),
                now(),
                str(log_path),
                str(prompt_path),
                str(result_path),
            ),
        )

    runner = [sys.executable, str(Path(__file__).resolve()), "_run-worker", worker_id]
    log_file = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        runner,
        cwd=str(worktree),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    update_worker(worker_id, pid=proc.pid)
    return {
        "worker_id": worker_id,
        "engine": engine,
        "status": "running",
        "reattach": {
            "status_command": f"python3 {Path(__file__).resolve()} status {worker_id}",
            "tail_command": f"python3 {Path(__file__).resolve()} tail {worker_id} --lines 100",
            "collect_command": f"python3 {Path(__file__).resolve()} collect {worker_id}",
        },
        "repo_path": str(repo),
        "base_ref": base_ref,
        "branch": branch,
        "caller": caller,
        "parent": parent,
        "run_id": run_id,
        "scratchpad_path": str(contract_path),
        "worker_note_path": str(worker_notes),
        "worktree_path": str(worktree),
        "log_path": str(log_path),
        "timeout_sec": args.timeout_sec,
    }


def caller_label(args: argparse.Namespace) -> str:
    explicit = getattr(args, "caller", None) or os.environ.get("FOREMAN_CALLER")
    if explicit:
        return str(explicit)
    return "cli"


def parent_label(args: argparse.Namespace) -> str:
    explicit = getattr(args, "parent", None) or os.environ.get("FOREMAN_PARENT")
    if explicit:
        return str(explicit)
    return ""


def worker_status(row: sqlite3.Row) -> str:
    if row["status"] in {"discarded", "merged", "pr_opened", "interrupted"}:
        return row["status"]
    result_path = Path(row["result_path"])
    if result_path.exists():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            status = "interrupted" if payload.get("interrupted") else ("done" if payload.get("exit_code") == 0 else "failed")
            if row["status"] == "running":
                update_worker(row["id"], status=status, finished_at=payload.get("finished_at"), exit_code=payload.get("exit_code"))
            return status
        except Exception:
            return row["status"]
    if row["status"] != "running":
        return row["status"]
    pid = row["pid"]
    if not pid:
        return "running"
    try:
        os.kill(pid, 0)
        return "running"
    except ProcessLookupError:
        update_worker(row["id"], status="failed", finished_at=now(), exit_code=255)
        return "failed"


def list_workers(_: argparse.Namespace) -> dict[str, Any]:
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM workers ORDER BY created_at DESC LIMIT 50").fetchall()
    workers = []
    for row in rows:
        workers.append(
            {
                "worker_id": row["id"],
                "display_name": worker_display_name(row),
                "engine": row["engine"],
                "status": worker_status(row),
                "repo_path": row["repo_path"],
                "branch": row["branch"],
                "caller": row["caller"],
                "parent": row["parent"],
                "run_id": row["run_id"],
                "scratchpad_path": row["scratchpad_path"],
                "worker_note_path": row["worker_note_path"],
                "worktree_path": row["worktree_path"],
                "timeout_sec": row["timeout_sec"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            }
        )
    return {"workers": workers}


def recent_workers(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM workers ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [worker_payload(row) for row in rows]


def worker_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = {
        "worker_id": row["id"],
        "display_name": worker_display_name(row),
        "engine": row["engine"],
        "status": worker_status(row),
        "repo_path": row["repo_path"],
        "base_ref": row["base_ref"],
        "branch": row["branch"],
        "caller": row["caller"],
        "parent": row["parent"],
        "run_id": row["run_id"],
        "scratchpad_path": row["scratchpad_path"],
        "worker_note_path": row["worker_note_path"],
        "worktree_path": row["worktree_path"],
        "timeout_sec": row["timeout_sec"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "exit_code": row["exit_code"],
        "log_path": row["log_path"],
    }
    result_path = Path(row["result_path"])
    if result_path.exists():
        try:
            payload["result"] = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload["result"] = {"error": "result file could not be read", "path": str(result_path)}
    return payload


def worker_status_payload(args: argparse.Namespace) -> dict[str, Any]:
    row = db_row(args.worker_id)
    worker_status(row)
    return worker_payload(db_row(args.worker_id))


def worker_display_name(row: sqlite3.Row) -> str:
    repo_name = Path(row["repo_path"]).name or "repo"
    spec = str(row["spec"] or "")
    title = first_meaningful_spec_line(spec)
    if title:
        return f"{repo_name}: {title}"
    return f"{repo_name}: {row['engine']} worker"


def first_meaningful_spec_line(spec: str) -> str:
    for raw_line in spec.splitlines():
        line = raw_line.strip(" \t-*#")
        if not line:
            continue
        lowered = line.lower()
        if lowered in {"goal:", "acceptance criteria:", "files in scope:", "files out of scope:", "verification command:", "notes:"}:
            continue
        for prefix in ("goal:", "task:", "title:", "request:", "build:", "fix:"):
            if lowered.startswith(prefix):
                line = line[len(prefix) :].strip()
                break
        line = " ".join(line.split())
        if line:
            return line[:86] + ("..." if len(line) > 86 else "")
    return ""


def tail(args: argparse.Namespace) -> dict[str, Any]:
    row = db_row(args.worker_id)
    path = Path(row["log_path"])
    if not path.exists():
        return {"worker_id": args.worker_id, "status": worker_status(row), "tail": ""}
    data = path.read_bytes()[-MAX_TAIL_BYTES:]
    lines = data.decode("utf-8", errors="replace").splitlines()
    return {"worker_id": args.worker_id, "status": worker_status(row), "tail": "\n".join(lines[-args.lines :])}


def read_log(worker_id: str, offset: int = 0, max_bytes: int = MAX_TAIL_BYTES) -> dict[str, Any]:
    row = db_row(worker_id)
    path = Path(row["log_path"])
    if not path.exists():
        return {"worker_id": worker_id, "status": worker_status(row), "offset": 0, "text": ""}
    size = path.stat().st_size
    offset = max(0, min(offset, size))
    if size - offset > max_bytes:
        offset = max(0, size - max_bytes)
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(max_bytes)
        new_offset = handle.tell()
    return {
        "worker_id": worker_id,
        "status": worker_status(row),
        "offset": new_offset,
        "size": size,
        "text": chunk.decode("utf-8", errors="replace"),
    }


def log_metrics(worker_id: str) -> dict[str, Any]:
    row = db_row(worker_id)
    path = Path(row["log_path"])
    metrics: dict[str, Any] = {
        "worker_id": worker_id,
        "status": worker_status(row),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_tokens": 0,
        "cost_usd": None,
        "model": None,
        "session_id": None,
        "duration_ms": None,
        "updated_at": now(),
    }
    if not path.exists():
        return metrics
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("session_id"):
            metrics["session_id"] = obj.get("session_id")
        if obj.get("type") == "result":
            usage = obj.get("usage") or {}
            metrics["input_tokens"] = int(usage.get("input_tokens") or metrics["input_tokens"] or 0)
            metrics["output_tokens"] = int(usage.get("output_tokens") or metrics["output_tokens"] or 0)
            metrics["cache_read_input_tokens"] = int(usage.get("cache_read_input_tokens") or metrics["cache_read_input_tokens"] or 0)
            metrics["cache_creation_input_tokens"] = int(usage.get("cache_creation_input_tokens") or metrics["cache_creation_input_tokens"] or 0)
            metrics["cost_usd"] = obj.get("total_cost_usd")
            metrics["duration_ms"] = obj.get("duration_ms")
            model_usage = obj.get("modelUsage") or {}
            if model_usage:
                metrics["model"] = next(iter(model_usage))
            continue
        usage = ((obj.get("event") or {}).get("message") or obj.get("message") or {}).get("usage")
        if not usage:
            usage = (obj.get("event") or {}).get("usage")
        if usage:
            metrics["input_tokens"] = max(int(usage.get("input_tokens") or 0), int(metrics["input_tokens"] or 0))
            metrics["output_tokens"] = max(int(usage.get("output_tokens") or 0), int(metrics["output_tokens"] or 0))
            metrics["cache_read_input_tokens"] = max(int(usage.get("cache_read_input_tokens") or 0), int(metrics["cache_read_input_tokens"] or 0))
            metrics["cache_creation_input_tokens"] = max(int(usage.get("cache_creation_input_tokens") or 0), int(metrics["cache_creation_input_tokens"] or 0))
            if ((obj.get("event") or {}).get("message") or obj.get("message") or {}).get("model"):
                metrics["model"] = ((obj.get("event") or {}).get("message") or obj.get("message") or {}).get("model")
    metrics["total_tokens"] = (
        int(metrics["input_tokens"] or 0)
        + int(metrics["output_tokens"] or 0)
        + int(metrics["cache_read_input_tokens"] or 0)
        + int(metrics["cache_creation_input_tokens"] or 0)
    )
    return metrics


def load_daemon_state() -> dict[str, Any] | None:
    if not DAEMON_PATH.exists():
        return None
    try:
        return json.loads(DAEMON_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def daemon_worker_url(state: dict[str, Any], worker_id: str | None = None) -> str:
    url = str(state["url"])
    if worker_id:
        return url + "?" + urllib.parse.urlencode({"worker": worker_id})
    return url


def get_json_url(url: str, timeout: float = 0.5) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def daemon_health(state: dict[str, Any] | None, timeout: float = 0.5) -> dict[str, Any] | None:
    if not state or not state.get("url"):
        return None
    health = get_json_url(str(state["url"]).rstrip("/") + "/api/health", timeout=timeout)
    if health and health.get("ok"):
        return health
    return None


def daemon_state_payload(host: str, port: int, idle_timeout_sec: int, started_at: int) -> dict[str, Any]:
    display_host = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    script = Path(__file__).resolve()
    return {
        "pid": os.getpid(),
        "host": display_host,
        "port": port,
        "url": f"http://{display_host}:{port}/",
        "started_at": started_at,
        "idle_timeout_sec": idle_timeout_sec,
        "state_path": str(DAEMON_PATH),
        "log_path": str(DAEMON_LOG_PATH),
        "script_path": str(script),
        "script_mtime_ns": script.stat().st_mtime_ns,
    }


def write_daemon_state(state: dict[str, Any]) -> None:
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    DAEMON_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def remove_daemon_state(pid: int) -> None:
    state = load_daemon_state()
    if state and state.get("pid") == pid:
        DAEMON_PATH.unlink(missing_ok=True)


def daemon_code_is_current(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    script = Path(__file__).resolve()
    return state.get("script_path") == str(script) and state.get("script_mtime_ns") == script.stat().st_mtime_ns


def stop_daemon(state: dict[str, Any] | None) -> None:
    if not state or not state.get("pid"):
        return
    try:
        os.kill(int(state["pid"]), 15)
    except (ProcessLookupError, PermissionError, ValueError):
        pass


def ensure_daemon(args: argparse.Namespace) -> dict[str, Any]:
    init_db()
    idle_timeout_sec = int(getattr(args, "idle_timeout_sec", DEFAULT_DAEMON_IDLE_TIMEOUT_SEC))
    host = getattr(args, "host", "127.0.0.1")
    port = int(getattr(args, "port", DEFAULT_DAEMON_PORT))
    existing = load_daemon_state()
    health = daemon_health(existing)
    if existing and health and daemon_code_is_current(existing):
        return {
            "status": "running",
            "started": False,
            "daemon": {**existing, "health": health},
            "url": existing["url"],
        }

    if existing and health and not daemon_code_is_current(existing):
        stop_daemon(existing)
        time.sleep(0.2)
    DAEMON_PATH.unlink(missing_ok=True)
    LOGS_HOME.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "daemon",
        "--host",
        host,
        "--port",
        str(port),
        "--idle-timeout-sec",
        str(idle_timeout_sec),
        "--quiet",
    ]
    log_file = open(DAEMON_LOG_PATH, "ab", buffering=0)
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.time() + DAEMON_START_TIMEOUT_SEC
    last_state: dict[str, Any] | None = None
    while time.time() < deadline:
        last_state = load_daemon_state()
        health = daemon_health(last_state)
        if last_state and health:
            return {
                "status": "running",
                "started": True,
                "daemon": {**last_state, "health": health},
                "url": last_state["url"],
            }
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    return {
        "status": "failed",
        "started": True,
        "pid": proc.pid,
        "returncode": proc.poll(),
        "last_state": last_state,
        "log_path": str(DAEMON_LOG_PATH),
        "error": "daemon did not become healthy before timeout",
    }


def watch(args: argparse.Namespace) -> None:
    row = db_row(args.worker_id)
    log_path = Path(row["log_path"])
    print(f"[foreman-watch] worker_id={args.worker_id}")
    print(f"[foreman-watch] engine={row['engine']}")
    print(f"[foreman-watch] log_path={log_path}")
    print(f"[foreman-watch] worktree_path={row['worktree_path']}")
    print(f"[foreman-watch] interval_sec={args.interval_sec}")
    print("[foreman-watch] streaming log; Ctrl-C stops watching, not the worker")
    offset = 0
    last_status = None
    try:
        while True:
            row = db_row(args.worker_id)
            status = worker_status(row)
            if status != last_status:
                print(f"[foreman-watch] status={status}", flush=True)
                last_status = status
            if log_path.exists():
                with log_path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    offset = handle.tell()
                if chunk:
                    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                    if not chunk.endswith(b"\n"):
                        sys.stdout.write("\n")
                    sys.stdout.flush()
            if status not in {"running"}:
                break
            time.sleep(args.interval_sec)
    except KeyboardInterrupt:
        print("\n[foreman-watch] stopped watching; worker is unchanged", flush=True)


def monitor_hint(args: argparse.Namespace) -> dict[str, Any]:
    worker_id = args.worker_id
    script = Path(__file__).resolve()
    daemon = ensure_daemon(
        argparse.Namespace(
            host="127.0.0.1",
            idle_timeout_sec=DEFAULT_DAEMON_IDLE_TIMEOUT_SEC,
        )
    )
    web_url = daemon_worker_url(daemon["daemon"], worker_id) if daemon.get("daemon") else None
    return {
        "worker_id": worker_id,
        "watch_command": f"python3 {script} watch {worker_id}",
        "tail_command": f"python3 {script} tail {worker_id} --lines 120",
        "web_command": f"python3 {script} web {worker_id} --open",
        "web_url": web_url,
        "daemon": daemon,
        "note": "watch streams logs in a terminal; web attaches to the local Foreman daemon, starting it if needed. The daemon idles out when unused; MCP clients should use foreman_tail for snapshots.",
    }


def run_web(args: argparse.Namespace) -> dict[str, Any]:
    daemon = ensure_daemon(args)
    if daemon.get("status") != "running":
        return daemon
    url = daemon_worker_url(daemon["daemon"], args.worker_id)
    if args.open:
        webbrowser.open(url)
    return {
        "url": url,
        "daemon": daemon,
        "note": "Attached to the local Foreman daemon; workers keep running if this browser page closes.",
    }


def run_daemon(args: argparse.Namespace) -> None:
    init_db()
    started_at = now()
    last_activity_at = time.time()

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "ForemanDaemon/0.1"

        def log_message(self, fmt: str, *values: Any) -> None:
            if not args.quiet:
                super().log_message(fmt, *values)

        def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_html(self, body: str, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSON body: {exc}") from exc
            if not isinstance(payload, dict):
                raise SystemExit("JSON body must be an object")
            return payload

        def do_GET(self) -> None:
            self.server.last_activity_at = time.time()  # type: ignore[attr-defined]
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if path == "/":
                self.send_html(render_monitor_page(None))
                return
            if path == "/api/health":
                self.send_json(
                    {
                        "ok": True,
                        "pid": os.getpid(),
                        "started_at": self.server.started_at,  # type: ignore[attr-defined]
                        "last_activity_at": int(self.server.last_activity_at),  # type: ignore[attr-defined]
                        "idle_timeout_sec": self.server.idle_timeout_sec,  # type: ignore[attr-defined]
                        "script_mtime_ns": Path(__file__).resolve().stat().st_mtime_ns,
                    }
                )
                return
            if path == "/api/workers":
                self.send_json({"workers": recent_workers()})
                return
            if path.startswith("/api/workers/"):
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    try:
                        self.send_json({"worker": worker_payload(db_row(parts[2]))})
                    except SystemExit as exc:
                        self.send_json({"error": str(exc)}, status=404)
                    return
                if len(parts) == 4 and parts[3] == "log":
                    try:
                        offset = int(query.get("offset", ["0"])[0])
                    except ValueError:
                        offset = 0
                    try:
                        self.send_json(read_log(parts[2], offset=offset))
                    except SystemExit as exc:
                        self.send_json({"error": str(exc)}, status=404)
                    return
                if len(parts) == 4 and parts[3] == "metrics":
                    try:
                        self.send_json({"metrics": log_metrics(parts[2])})
                    except SystemExit as exc:
                        self.send_json({"error": str(exc)}, status=404)
                    return
            self.send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:
            self.server.last_activity_at = time.time()  # type: ignore[attr-defined]
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path.startswith("/api/workers/"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] in {"note", "interrupt"}:
                    try:
                        payload = self.read_json_body()
                        actor = str(payload.get("actor") or "operator")
                        if parts[3] == "note":
                            result = add_worker_note(parts[2], str(payload.get("message") or ""), actor=actor)
                        else:
                            result = interrupt_worker(parts[2], str(payload.get("reason") or payload.get("message") or ""), actor=actor)
                        self.send_json(result)
                    except SystemExit as exc:
                        self.send_json({"error": str(exc)}, status=400)
                    return
            self.send_json({"error": "not found"}, status=404)

    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    server.timeout = 1.0
    server.started_at = started_at  # type: ignore[attr-defined]
    server.last_activity_at = last_activity_at  # type: ignore[attr-defined]
    server.idle_timeout_sec = int(args.idle_timeout_sec)  # type: ignore[attr-defined]
    host, port = server.server_address
    state = daemon_state_payload(host, port, int(args.idle_timeout_sec), started_at)
    write_daemon_state(state)
    print(f"[foreman-daemon] {state['url']}", flush=True)
    print(f"[foreman-daemon] idle_timeout_sec={args.idle_timeout_sec}", flush=True)
    try:
        while True:
            server.handle_request()
            idle_for = time.time() - float(server.last_activity_at)  # type: ignore[attr-defined]
            if int(args.idle_timeout_sec) > 0 and idle_for >= int(args.idle_timeout_sec):
                print(f"[foreman-daemon] idle shutdown after {idle_for:.1f}s", flush=True)
                break
    except KeyboardInterrupt:
        print("\n[foreman-daemon] stopped", flush=True)
    finally:
        server.server_close()
        remove_daemon_state(os.getpid())


def render_monitor_page(initial_worker_id: str | None) -> str:
    selected = json.dumps(initial_worker_id or "")
    page_script_mtime_ns = Path(__file__).resolve().stat().st_mtime_ns
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Foreman Monitor</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0d0f12; --panel:#15191f; --panel2:#101318; --panel3:#0a0d11; --text:#f1f4f8; --muted:#9aa6b2; --line:#2a313a; --line2:#20262e; --ok:#57d68d; --run:#7db7ff; --fail:#ff7373; --warn:#ffd166; --accent:#a7f3d0; --accent2:#7db7ff; --glow:rgba(167,243,208,.14); --brand-bg:radial-gradient(circle at top left, rgba(167,243,208,.12), transparent 28rem); }}
    body[data-engine="claude"] {{ --bg:#14110f; --panel:#1d1814; --panel2:#15110e; --panel3:#0f0c0a; --text:#f7efe7; --muted:#b9a89a; --line:#3b3028; --line2:#2a211b; --accent:#d89a63; --accent2:#f0d0ad; --run:#d89a63; --glow:rgba(216,154,99,.20); --brand-bg:radial-gradient(circle at top left, rgba(216,154,99,.22), transparent 30rem), radial-gradient(circle at 70% -10%, rgba(240,208,173,.08), transparent 26rem); }}
    body[data-engine="gemini"] {{ --bg:#090f1e; --panel:#111a31; --panel2:#0d1427; --panel3:#070b16; --text:#eef4ff; --muted:#9fb2dc; --line:#26365f; --line2:#1a294b; --accent:#8ab4ff; --accent2:#c58cff; --run:#8ab4ff; --glow:rgba(138,180,255,.18); --brand-bg:radial-gradient(circle at top left, rgba(138,180,255,.22), transparent 30rem), radial-gradient(circle at 80% 0%, rgba(197,140,255,.16), transparent 24rem); }}
    body[data-engine="codex"] {{ --bg:#071313; --panel:#0d1e1f; --panel2:#091719; --panel3:#050d0e; --text:#effdfb; --muted:#92b7b6; --line:#1f3d40; --line2:#173033; --accent:#5eead4; --accent2:#7db7ff; --run:#5eead4; --glow:rgba(94,234,212,.16); --brand-bg:radial-gradient(circle at top left, rgba(94,234,212,.18), transparent 30rem), radial-gradient(circle at 75% 0%, rgba(125,183,255,.10), transparent 24rem); }}
    body[data-engine="aider"] {{ --bg:#101016; --panel:#181923; --panel2:#12131b; --panel3:#0b0c11; --text:#f4f4fb; --muted:#aaaac3; --line:#303247; --line2:#252638; --accent:#c4b5fd; --accent2:#f0abfc; --run:#c4b5fd; --glow:rgba(196,181,253,.18); --brand-bg:radial-gradient(circle at top left, rgba(196,181,253,.20), transparent 30rem), radial-gradient(circle at 80% 0%, rgba(240,171,252,.10), transparent 24rem); }}
    body[data-engine="smoke"] {{ --bg:#12100a; --panel:#1f1a0d; --panel2:#171307; --panel3:#0e0b04; --text:#fff8e8; --muted:#c8b98f; --line:#3b3218; --line2:#2c250f; --accent:#ffd166; --accent2:#f59e0b; --run:#ffd166; --glow:rgba(255,209,102,.16); --brand-bg:radial-gradient(circle at top left, rgba(255,209,102,.18), transparent 30rem), radial-gradient(circle at 80% 0%, rgba(245,158,11,.10), transparent 24rem); }}
    * {{ box-sizing: border-box; }}
    html, body {{ height:100%; overflow:hidden; }}
    body {{ margin:0; font:14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--brand-bg), var(--bg); color:var(--text); display:flex; flex-direction:column; transition:background .25s ease, color .25s ease; }}
    header {{ flex:0 0 auto; padding:18px 22px; border-bottom:1px solid var(--line); display:flex; gap:16px; align-items:center; justify-content:space-between; background:color-mix(in srgb, var(--panel) 84%, transparent); backdrop-filter:blur(10px); box-shadow:0 10px 40px var(--glow); z-index:2; }}
    h1 {{ margin:0; font-size:18px; letter-spacing:.02em; }}
    .sub {{ color:var(--muted); font-size:12px; }}
    .head-left {{ display:flex; flex-direction:column; gap:2px; }}
    .head-right {{ display:flex; align-items:center; gap:12px; color:var(--muted); font-size:12px; }}
    .engine-chip {{ display:inline-flex; align-items:center; gap:6px; padding:4px 9px; border:1px solid color-mix(in srgb, var(--accent) 45%, var(--line)); color:var(--accent2); background:color-mix(in srgb, var(--accent) 10%, transparent); border-radius:999px; font:11px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; text-transform:uppercase; letter-spacing:.05em; }}
    .live-pill {{ display:inline-flex; align-items:center; gap:7px; padding:4px 10px; border:1px solid color-mix(in srgb, var(--run) 35%, var(--line)); border-radius:999px; color:var(--run); background:color-mix(in srgb, var(--run) 8%, transparent); font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .running-pill {{ color:var(--accent); border-color:color-mix(in srgb, var(--accent) 35%, var(--line)); background:color-mix(in srgb, var(--accent) 8%, transparent); transition:transform .18s ease, border-color .18s ease, background .18s ease, box-shadow .18s ease; }}
    .running-pill.is-running {{ color:var(--bg); border-color:var(--accent); background:var(--accent); box-shadow:0 0 0 0 color-mix(in srgb, var(--accent) 55%, transparent), 0 0 26px color-mix(in srgb, var(--accent) 35%, transparent); animation:runningPulse 1.35s ease-in-out infinite; font-weight:800; }}
    .live-dot {{ width:7px; height:7px; border-radius:50%; background:var(--run); box-shadow:0 0 0 0 color-mix(in srgb, var(--run) 55%, transparent); animation:pulse 1.4s infinite; }}
    main {{ flex:1 1 auto; min-height:0; display:grid; grid-template-columns: 360px minmax(0, 1fr); }}
    aside {{ min-height:0; border-right:1px solid var(--line); background:var(--panel2); overflow:auto; }}
    .worker {{ width:100%; text-align:left; border:0; border-bottom:1px solid var(--line2); border-left:3px solid transparent; background:transparent; color:var(--text); padding:13px 14px 13px 11px; cursor:pointer; display:block; transition:background .15s ease, border-color .15s ease; }}
    .worker:hover {{ background:color-mix(in srgb, var(--accent) 8%, var(--panel2)); }}
    .worker.active {{ background:linear-gradient(90deg, color-mix(in srgb, var(--accent) 15%, var(--panel2)), var(--panel2)); border-left-color:var(--accent); }}
    .worker.new-worker {{ animation:newWorker 4s ease-out; }}
    .wtitle {{ display:block; margin-top:7px; font-weight:650; font-size:13px; line-height:1.25; }}
    .wid {{ display:block; margin-top:4px; color:var(--muted); font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:11px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
    .meta {{ margin-top:5px; color:var(--muted); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
    .status {{ display:inline-block; padding:1px 7px; border-radius:999px; font-size:11px; margin-right:7px; border:1px solid var(--line); }}
    .status-running {{ color:var(--run); border-color:color-mix(in srgb, var(--run) 50%, var(--line)); }}
    .status-done, .status-merged {{ color:var(--ok); border-color:color-mix(in srgb, var(--ok) 50%, var(--line)); }}
    .status-failed, .status-interrupted {{ color:var(--fail); border-color:color-mix(in srgb, var(--fail) 50%, var(--line)); }}
    .status-discarded {{ color:var(--warn); border-color:color-mix(in srgb, var(--warn) 50%, var(--line)); }}
    section {{ min-width:0; min-height:0; display:flex; flex-direction:column; overflow:hidden; }}
    .details {{ flex:0 0 auto; padding:14px 18px; border-bottom:1px solid var(--line); background:linear-gradient(135deg, color-mix(in srgb, var(--accent) 7%, var(--panel)), var(--panel)); box-shadow:0 16px 45px color-mix(in srgb, var(--glow) 65%, transparent); z-index:1; }}
    .details h2 {{ margin:0 0 8px; font-size:16px; }}
    .metrics {{ display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:8px; margin:12px 0 10px; }}
    .metric {{ border:1px solid color-mix(in srgb, var(--accent) 18%, var(--line)); background:color-mix(in srgb, var(--accent) 5%, var(--panel3)); padding:8px 10px; min-width:0; }}
    .metric-label {{ color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.05em; }}
    .metric-value {{ margin-top:3px; color:var(--text); font:13px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
    .token-chart {{ margin:0 0 10px; border:1px solid color-mix(in srgb, var(--accent) 18%, var(--line)); background:linear-gradient(180deg, color-mix(in srgb, var(--accent) 7%, var(--panel3)), var(--panel3)); padding:9px 10px 8px; }}
    .chart-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin-bottom:4px; color:var(--muted); font-size:11px; }}
    .chart-title {{ color:var(--text); font-weight:650; }}
    .chart-eta {{ color:var(--accent2); text-align:right; }}
    .chart-svg {{ display:block; width:100%; height:86px; overflow:visible; }}
    .chart-grid {{ stroke:color-mix(in srgb, var(--line) 70%, transparent); stroke-width:1; }}
    .chart-line {{ fill:none; stroke:var(--accent); stroke-width:2.5; vector-effect:non-scaling-stroke; filter:drop-shadow(0 0 6px var(--glow)); }}
    .chart-area {{ fill:color-mix(in srgb, var(--accent) 13%, transparent); }}
    .chart-dot {{ fill:var(--accent2); stroke:var(--panel3); stroke-width:2; animation:tokenFlash 1s ease-in-out infinite; }}
    .chart-empty {{ fill:var(--muted); font:12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .grid {{ display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:6px 18px; color:var(--muted); font-size:12px; }}
    .grid code {{ color:var(--text); overflow-wrap:anywhere; }}
    .log-shell {{ display:flex; flex:1 1 auto; min-height:0; flex-direction:column; background:var(--panel3); overflow:hidden; }}
    .intervention {{ flex:0 0 auto; display:grid; grid-template-columns:minmax(0,1fr) auto auto; gap:8px; padding:10px 14px; border-bottom:1px solid var(--line2); background:color-mix(in srgb, var(--accent) 5%, var(--panel3)); }}
    .intervention input {{ width:100%; min-width:0; border:1px solid var(--line); background:var(--panel3); color:var(--text); padding:8px 10px; font:12px/1.3 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; outline:none; }}
    .intervention input:focus {{ border-color:var(--accent); box-shadow:0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent); }}
    .intervention button {{ border:1px solid color-mix(in srgb, var(--accent) 35%, var(--line)); background:color-mix(in srgb, var(--accent) 10%, var(--panel)); color:var(--text); padding:8px 11px; font:12px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; cursor:pointer; }}
    .intervention button:hover {{ background:color-mix(in srgb, var(--accent) 18%, var(--panel)); }}
    .intervention button.danger {{ border-color:color-mix(in srgb, var(--fail) 45%, var(--line)); color:#ffd1d1; background:color-mix(in srgb, var(--fail) 10%, var(--panel)); }}
    .intervention button:disabled, .intervention input:disabled {{ opacity:.48; cursor:not-allowed; }}
    .log-toolbar {{ display:flex; justify-content:space-between; align-items:center; gap:12px; padding:9px 14px; border-bottom:1px solid var(--line2); color:var(--muted); font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:11px; }}
    .poll-error {{ color:var(--fail); }}
    .log-toolbar strong {{ color:var(--text); font-weight:600; }}
    .log-view {{ margin:0; padding:12px 0 24px; flex:1 1 auto; min-height:0; overflow:auto; background:linear-gradient(180deg, color-mix(in srgb, var(--accent) 5%, var(--panel3)) 0%, var(--panel3) 100%); color:#dce5ee; font:12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; tab-size:2; }}
    .log-entry {{ display:grid; grid-template-columns:76px 112px minmax(0,1fr); gap:10px; padding:4px 18px; border-left:3px solid transparent; white-space:pre-wrap; overflow-wrap:anywhere; }}
    .log-entry.fresh {{ animation:fresh 1.8s ease-out; }}
    .log-ts {{ color:#6d7b8a; user-select:none; }}
    .log-kind {{ color:#9aa6b2; user-select:none; }}
    .log-msg {{ min-width:0; }}
    .log-foreman {{ border-left-color:#7db7ff; }}
    .log-claude {{ border-left-color:var(--accent); }}
    .log-tool {{ border-left-color:var(--accent2); }}
    .log-result {{ border-left-color:#c4b5fd; }}
    .log-warning {{ border-left-color:#ff7373; color:#ffd1d1; }}
    .log-operator {{ border-left-color:var(--fail); color:#ffe2e2; background:rgba(255,115,115,.06); }}
    .log-final {{ margin-top:12px; padding-top:12px; border-top:1px solid var(--line); background:rgba(87,214,141,.045); border-left-color:#57d68d; }}
    .log-break {{ margin:12px 0 8px; padding:8px 18px; border-top:1px solid var(--line); color:var(--accent); font-weight:700; letter-spacing:.04em; text-transform:uppercase; background:color-mix(in srgb, var(--accent) 6%, transparent); }}
    .empty {{ padding:24px; color:var(--muted); }}
    @keyframes pulse {{ 70% {{ box-shadow:0 0 0 8px transparent; }} 100% {{ box-shadow:0 0 0 0 transparent; }} }}
    @keyframes runningPulse {{ 0%,100% {{ transform:translateY(0) scale(1); box-shadow:0 0 0 0 color-mix(in srgb, var(--accent) 55%, transparent), 0 0 26px color-mix(in srgb, var(--accent) 35%, transparent); }} 50% {{ transform:translateY(-1px) scale(1.035); box-shadow:0 0 0 7px transparent, 0 0 34px color-mix(in srgb, var(--accent) 50%, transparent); }} }}
    @keyframes fresh {{ 0% {{ background:rgba(125,183,255,.22); color:#fff; }} 100% {{ background:transparent; color:#dce5ee; }} }}
    @keyframes newWorker {{ 0% {{ background:rgba(167,243,208,.24); border-left-color:var(--accent); }} 100% {{ background:transparent; }} }}
    @keyframes tokenFlash {{ 0%,100% {{ r:4; opacity:1; }} 50% {{ r:7; opacity:.48; }} }}
    @media (prefers-reduced-motion: reduce) {{ .running-pill.is-running, .live-dot, .chart-dot {{ animation:none; }} }}
    @media (max-width: 850px) {{ main {{ grid-template-columns:1fr; grid-template-rows:minmax(160px, 36vh) minmax(0, 1fr); }} aside {{ border-right:0; border-bottom:1px solid var(--line); }} .grid {{ grid-template-columns:1fr; }} .metrics {{ grid-template-columns:repeat(2, minmax(0,1fr)); }} }}
  </style>
</head>
<body>
  <header>
    <div class="head-left"><h1>Foreman Monitor</h1><div class="sub">Live worker list and log stream. Closing this page does not stop workers.</div></div>
    <div class="head-right"><span class="engine-chip" id="engine-chip">engine</span><span class="live-pill running-pill" id="running-count" aria-live="polite">0 running</span><span class="live-pill"><span class="live-dot"></span><span id="live-label">polling</span></span><span id="clock"></span></div>
  </header>
  <main>
    <aside id="workers"><div class="empty">Loading workers...</div></aside>
    <section>
      <div class="details" id="details"><h2>No worker selected</h2><div class="sub">Pick a worker from the left.</div></div>
      <div class="intervention">
        <input id="intervention-text" placeholder="Operator note or stop reason..." />
        <button id="note-button" type="button">Add note</button>
        <button id="stop-button" type="button" class="danger">Stop worker</button>
      </div>
      <div class="log-shell">
        <div class="log-toolbar"><strong>worker log</strong><span><span id="byte-count">0</span> bytes read · last poll <span id="last-poll">never</span></span></div>
        <div class="log-view" id="log"></div>
      </div>
    </section>
  </main>
  <script>
    const urlParams = new URLSearchParams(location.search);
    let selected = urlParams.get('worker') || {selected};
    let offset = 0;
    let followLog = true;
    let workersInitialized = false;
    let knownWorkerIds = new Set();
    let newlySeenWorkerIds = new Set();
    const workersEl = document.getElementById('workers');
    const detailsEl = document.getElementById('details');
    const logEl = document.getElementById('log');
    const clockEl = document.getElementById('clock');
    const liveLabelEl = document.getElementById('live-label');
    const runningCountEl = document.getElementById('running-count');
    const engineChipEl = document.getElementById('engine-chip');
    const byteCountEl = document.getElementById('byte-count');
    const lastPollEl = document.getElementById('last-poll');
    const interventionTextEl = document.getElementById('intervention-text');
    const noteButtonEl = document.getElementById('note-button');
    const stopButtonEl = document.getElementById('stop-button');
    let lastMetrics = null;
    let selectedStatus = '';
    const tokenHistory = new Map();
    const pageScriptMtimeNs = {page_script_mtime_ns};

    function cls(status) {{ return 'status status-' + String(status || '').replace(/_/g, '-'); }}
    function fmt(ts) {{ return ts ? new Date(ts * 1000).toLocaleString() : ''; }}
    function esc(s) {{ return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
    function fmtNum(n) {{ return Number(n || 0).toLocaleString(); }}
    function fmtCost(n) {{ return n === null || n === undefined ? '...' : '$' + Number(n).toFixed(4); }}
    function fmtMs(ms) {{
      if (!ms) return '...';
      const sec = Math.round(Number(ms) / 1000);
      const min = Math.floor(sec / 60);
      const rem = sec % 60;
      return min ? `${{min}}m ${{rem}}s` : `${{rem}}s`;
    }}
    function fmtDurationSec(sec) {{
      sec = Math.max(0, Math.round(Number(sec || 0)));
      const min = Math.floor(sec / 60);
      const rem = sec % 60;
      return min ? `${{min}}m ${{rem}}s` : `${{rem}}s`;
    }}
    function etaText(worker, metrics) {{
      const status = String(worker?.status || '');
      if (metrics?.duration_ms) return `done in ${{fmtMs(metrics.duration_ms)}}`;
      if (status && status !== 'running') return status;
      if (!worker?.started_at) return 'ETA unknown';
      const elapsed = Date.now() / 1000 - Number(worker.started_at);
      const remaining = Number(worker.timeout_sec || 0) - elapsed;
      const timeoutText = worker.timeout_sec ? `timeout in ${{fmtDurationSec(remaining)}}` : 'no timeout';
      return `completion unknown · ${{timeoutText}}`;
    }}
    function setEngineTheme(engine) {{
      const clean = String(engine || '').toLowerCase();
      document.body.dataset.engine = clean || 'default';
      engineChipEl.textContent = clean || 'engine';
    }}
    function basename(path) {{ return String(path || '').split('/').filter(Boolean).pop() || String(path || ''); }}
    function recordTokenSample(worker, metrics) {{
      if (!worker || !metrics) return;
      const key = worker.worker_id;
      const total = Number(metrics.total_tokens || 0);
      const nowMs = Date.now();
      let samples = tokenHistory.get(key);
      if (!samples) {{
        samples = [];
        const startedMs = worker.started_at ? Number(worker.started_at) * 1000 : nowMs;
        samples.push({{ t: startedMs, y: 0 }});
        tokenHistory.set(key, samples);
      }}
      const last = samples[samples.length - 1];
      const t = worker.finished_at && worker.status !== 'running' ? Number(worker.finished_at) * 1000 : nowMs;
      if (!last || last.y !== total || Math.abs(t - last.t) >= 950) {{
        samples.push({{ t, y: total }});
      }} else {{
        last.t = t;
        last.y = total;
      }}
      if (samples.length > 160) samples.splice(1, samples.length - 160);
    }}
    function tokenChartHtml(worker, metrics) {{
      const samples = tokenHistory.get(worker?.worker_id) || [];
      const width = 720;
      const height = 86;
      const padL = 44;
      const padR = 14;
      const padT = 10;
      const padB = 22;
      const plotW = width - padL - padR;
      const plotH = height - padT - padB;
      const maxY = Math.max(1, ...samples.map(p => Number(p.y || 0)));
      const minT = Math.min(...samples.map(p => p.t), Date.now());
      const maxT = Math.max(...samples.map(p => p.t), minT + 1000);
      const x = p => padL + ((p.t - minT) / Math.max(1, maxT - minT)) * plotW;
      const y = p => padT + (1 - (Number(p.y || 0) / maxY)) * plotH;
      const points = samples.map(p => `${{x(p).toFixed(1)}},${{y(p).toFixed(1)}}`).join(' ');
      const area = samples.length > 1 ? `${{padL}},${{height - padB}} ${{points}} ${{padL + plotW}},${{height - padB}}` : '';
      const latest = samples[samples.length - 1] || {{ t: Date.now(), y: 0 }};
      const latestX = x(latest).toFixed(1);
      const latestY = y(latest).toFixed(1);
      const empty = samples.length < 2 ? `<text class="chart-empty" x="${{padL + 8}}" y="${{padT + 28}}">waiting for live token samples</text>` : '';
      return `<div class="token-chart">
        <div class="chart-head"><span class="chart-title">tokens over time</span><span class="chart-eta">${{esc(etaText(worker, metrics))}}</span></div>
        <svg class="chart-svg" viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none" role="img" aria-label="tokens over time">
          <line class="chart-grid" x1="${{padL}}" y1="${{padT}}" x2="${{padL + plotW}}" y2="${{padT}}"></line>
          <line class="chart-grid" x1="${{padL}}" y1="${{height - padB}}" x2="${{padL + plotW}}" y2="${{height - padB}}"></line>
          <text class="chart-empty" x="2" y="${{padT + 4}}">${{esc(fmtNum(maxY))}}</text>
          <text class="chart-empty" x="2" y="${{height - padB + 4}}">0</text>
          ${{area ? `<polygon class="chart-area" points="${{area}}"></polygon>` : ''}}
          ${{points ? `<polyline class="chart-line" points="${{points}}"></polyline>` : ''}}
          <circle class="chart-dot" cx="${{latestX}}" cy="${{latestY}}" r="4"></circle>
          ${{empty}}
        </svg>
      </div>`;
    }}
    function tsLabel(ts) {{
      const date = ts ? new Date(ts) : new Date();
      return date.toLocaleTimeString([], {{ hour: '2-digit', minute: '2-digit', second: '2-digit' }});
    }}
    function makeEntry(message, kind='log', klass='', ts=null) {{
      return {{ message: String(message || '').trimEnd(), kind, klass, ts: tsLabel(ts) }};
    }}
    function makeBreak(message, ts=null) {{
      return {{ message: String(message || '').trimEnd(), kind: 'break', klass: 'log-break', ts: tsLabel(ts) }};
    }}
    function cleanJsonEvent(obj) {{
      const ts = obj.timestamp || obj.event?.timestamp || null;
      if (obj.type === 'stream_event') {{
        const event = obj.event || {{}};
        if (event.type === 'content_block_delta') {{
          const delta = event.delta || {{}};
          if (delta.type === 'text_delta') return delta.text ? [makeEntry(delta.text, 'assistant', 'log-claude', ts)] : [];
          if (delta.type === 'input_json_delta') return [];
        }}
        if (event.type === 'content_block_start') {{
          const block = event.content_block || {{}};
          if (block.type === 'tool_use') return [makeBreak(`Tool: ${{block.name || 'tool'}}`, ts)];
          if (block.type === 'text') return [];
        }}
        if (event.type === 'content_block_stop') return [];
        if (event.type === 'message_start') {{
          const model = event.message?.model || 'claude';
          return [makeBreak(`Claude message started (${{model}})`, ts)];
        }}
        if (event.type === 'message_delta') {{
          const reason = event.delta?.stop_reason;
          return reason ? [makeEntry(`stop: ${{reason}}`, 'claude', 'log-claude', ts)] : [];
        }}
        if (event.type === 'message_stop') return [makeEntry('message complete', 'claude', 'log-claude', ts)];
        return [];
      }}
      if (obj.type === 'assistant') {{
        const parts = obj.message?.content || [];
        return parts.flatMap(part => {{
          if (part.type === 'text') return part.text ? [makeEntry(part.text, 'assistant', 'log-claude', ts)] : [];
          if (part.type === 'tool_use') {{
            const target = part.input?.file_path || part.input?.path || '';
            return [makeEntry(`${{part.name || 'tool'}} ${{basename(target)}}`, 'tool', 'log-tool', ts)];
          }}
          return [];
        }});
      }}
      if (obj.type === 'user') {{
        const result = obj.message?.content?.[0];
        if (result?.type === 'tool_result') {{
          const text = String(result.content || '').split('\\n')[0];
          return [makeEntry(text, 'result', 'log-result', ts)];
        }}
        return [];
      }}
      if (obj.type === 'system') {{
        if (obj.subtype === 'status') return [makeEntry(obj.status, 'claude', 'log-claude', ts)];
        if (obj.subtype === 'hook_started') return [makeEntry(obj.hook_name || obj.hook_event || 'started', 'hook', 'log-tool', ts)];
        if (obj.subtype === 'hook_response') return [makeEntry(`${{obj.hook_name || obj.hook_event || 'response'}} ${{obj.outcome || ''}}`, 'hook', 'log-tool', ts)];
        if (obj.subtype === 'init') return [makeEntry(`session ${{obj.session_id || ''}}`, 'session', 'log-claude', ts)];
        return [];
      }}
      if (obj.type === 'result') {{
        return [
          makeBreak('Final result', ts),
          makeEntry(obj.result || JSON.stringify(obj), 'final', 'log-final', ts),
        ];
      }}
      return [];
    }}
    function parseLogText(text) {{
      const entries = [];
      for (const line of String(text || '').split(/(?<=\\n)/)) {{
        const trimmed = line.trim();
        if (!trimmed) continue;
        if (trimmed.startsWith('{{') && trimmed.endsWith('}}')) {{
          try {{
            entries.push(...cleanJsonEvent(JSON.parse(trimmed)).filter(e => e.message));
            continue;
          }} catch (err) {{
          }}
        }}
        let kind = 'log';
        let klass = '';
        if (trimmed.startsWith('[foreman]')) {{ kind = 'foreman'; klass = 'log-foreman'; }}
        else if (trimmed.startsWith('[operator]')) {{ kind = 'operator'; klass = 'log-operator'; }}
        else if (trimmed.startsWith('Warning:')) {{ kind = 'warning'; klass = 'log-warning'; }}
        entries.push(makeEntry(trimmed, kind, klass));
      }}
      return entries;
    }}
    function scrollLogToBottom() {{
      logEl.scrollTop = logEl.scrollHeight;
    }}
    function appendLogText(text) {{
      const entries = parseLogText(text);
      if (!entries.length) return;
      for (const entry of entries) {{
        if (entry.kind === 'break') {{
          const div = document.createElement('div');
          div.className = `${{entry.klass}} fresh`;
          div.textContent = `${{entry.ts}}  ${{entry.message}}`;
          logEl.appendChild(div);
          continue;
        }}
        const row = document.createElement('div');
        row.className = `log-entry fresh ${{entry.klass || ''}}`;
        row.innerHTML = `<span class="log-ts">${{esc(entry.ts)}}</span><span class="log-kind">${{esc(entry.kind)}}</span><span class="log-msg"></span>`;
        row.querySelector('.log-msg').textContent = entry.message;
        logEl.appendChild(row);
      }}
      if (followLog) {{
        requestAnimationFrame(scrollLogToBottom);
        setTimeout(scrollLogToBottom, 0);
        setTimeout(scrollLogToBottom, 120);
      }}
    }}
    function updateUrl(workerId, replace=false) {{
      const url = new URL(window.location.href);
      if (workerId) url.searchParams.set('worker', workerId);
      else url.searchParams.delete('worker');
      const fn = replace ? history.replaceState : history.pushState;
      fn.call(history, {{ worker: workerId }}, '', url);
    }}
    function selectWorker(workerId, replace=false) {{
      if (!workerId || workerId === selected) return;
      selected = workerId;
      offset = 0;
      followLog = true;
      logEl.textContent = '';
      byteCountEl.textContent = '0';
      liveLabelEl.textContent = 'switching';
      lastMetrics = null;
      updateUrl(workerId, replace);
      refreshWorkers();
      refreshSelected();
    }}

    async function refreshWorkers() {{
      clockEl.textContent = new Date().toLocaleTimeString();
      let data;
      try {{
        const res = await fetch('/api/workers', {{ cache: 'no-store' }});
        data = await res.json();
      }} catch (err) {{
        liveLabelEl.textContent = 'poll error';
        liveLabelEl.className = 'poll-error';
        return;
      }}
      liveLabelEl.className = '';
      const runningCount = data.workers.filter(w => w.status === 'running').length;
      runningCountEl.textContent = runningCount > 0 ? `${{runningCount}} RUNNING` : '0 running';
      runningCountEl.title = `${{runningCount}} worker${{runningCount === 1 ? '' : 's'}} still running`;
      runningCountEl.classList.toggle('is-running', runningCount > 0);
      const incomingIds = new Set(data.workers.map(w => w.worker_id));
      newlySeenWorkerIds = new Set();
      if (workersInitialized) {{
        for (const id of incomingIds) {{
          if (!knownWorkerIds.has(id)) newlySeenWorkerIds.add(id);
        }}
        if (newlySeenWorkerIds.size) {{
          liveLabelEl.textContent = `${{newlySeenWorkerIds.size}} new worker${{newlySeenWorkerIds.size === 1 ? '' : 's'}}`;
        }}
      }}
      workersInitialized = true;
      knownWorkerIds = incomingIds;
      if (!selected && data.workers.length) {{
        selected = data.workers[0].worker_id;
        updateUrl(selected, true);
      }}
      workersEl.innerHTML = data.workers.map(w => `
        <button class="worker ${{w.worker_id === selected ? 'active' : ''}} ${{newlySeenWorkerIds.has(w.worker_id) ? 'new-worker' : ''}}" data-id="${{esc(w.worker_id)}}" title="${{esc(w.worker_id)}}">
          <div><span class="${{cls(w.status)}}">${{esc(w.status)}}</span><span class="wtitle">${{esc(w.display_name || w.worker_id)}}</span><span class="wid">${{esc(w.worker_id)}}</span></div>
          <div class="meta">${{esc(w.engine)}} · ${{esc(w.caller || 'unknown caller')}} · ${{esc(basename(w.repo_path))}}</div>
        </button>`).join('') || '<div class="empty">No workers yet.</div>';
      workersEl.querySelectorAll('.worker').forEach(btn => btn.onclick = () => {{
        selectWorker(btn.dataset.id);
      }});
    }}

    async function refreshSelected() {{
      if (!selected) return;
      let workerData;
      try {{
        const workerRes = await fetch(`/api/workers/${{encodeURIComponent(selected)}}`, {{ cache: 'no-store' }});
        workerData = await workerRes.json();
        const metricsRes = await fetch(`/api/workers/${{encodeURIComponent(selected)}}/metrics`, {{ cache: 'no-store' }});
        const metricsData = await metricsRes.json();
        lastMetrics = metricsData.metrics || lastMetrics;
      }} catch (err) {{
        liveLabelEl.textContent = 'poll error';
        liveLabelEl.className = 'poll-error';
        return;
      }}
      const w = workerData.worker;
      if (w) {{
        selectedStatus = w.status || '';
        stopButtonEl.disabled = selectedStatus !== 'running';
        noteButtonEl.disabled = false;
        interventionTextEl.disabled = false;
        setEngineTheme(w.engine);
        recordTokenSample(w, lastMetrics);
        let metricsHtml = '<div class="metrics"><div class="metric"><div class="metric-label">tokens</div><div class="metric-value">...</div></div><div class="metric"><div class="metric-label">out</div><div class="metric-value">...</div></div><div class="metric"><div class="metric-label">cache read</div><div class="metric-value">...</div></div><div class="metric"><div class="metric-label">cache write</div><div class="metric-value">...</div></div><div class="metric"><div class="metric-label">cost</div><div class="metric-value">...</div></div><div class="metric"><div class="metric-label">duration</div><div class="metric-value">...</div></div></div>';
        if (lastMetrics) {{
          metricsHtml = `<div class="metrics">
            <div class="metric"><div class="metric-label">tokens</div><div class="metric-value">${{fmtNum(lastMetrics.total_tokens)}}</div></div>
            <div class="metric"><div class="metric-label">out</div><div class="metric-value">${{fmtNum(lastMetrics.output_tokens)}}</div></div>
            <div class="metric"><div class="metric-label">cache read</div><div class="metric-value">${{fmtNum(lastMetrics.cache_read_input_tokens)}}</div></div>
            <div class="metric"><div class="metric-label">cache write</div><div class="metric-value">${{fmtNum(lastMetrics.cache_creation_input_tokens)}}</div></div>
            <div class="metric"><div class="metric-label">cost</div><div class="metric-value">${{fmtCost(lastMetrics.cost_usd)}}</div></div>
            <div class="metric"><div class="metric-label">duration</div><div class="metric-value">${{fmtMs(lastMetrics.duration_ms)}}</div></div>
          </div>`;
        }}
        detailsEl.innerHTML = `<h2><span class="${{cls(w.status)}}">${{esc(w.status)}}</span> ${{esc(w.display_name || w.worker_id)}}</h2>
          ${{metricsHtml}}
          ${{tokenChartHtml(w, lastMetrics)}}
          <div class="grid">
            <div>engine <code>${{esc(w.engine)}}</code></div>
            <div>caller <code>${{esc(w.caller || 'unknown')}}</code></div>
            <div>parent <code>${{esc(w.parent || 'not recorded')}}</code></div>
            <div>run <code>${{esc(w.run_id || 'not recorded')}}</code></div>
            <div>contract <code>${{esc(w.scratchpad_path || 'not recorded')}}</code></div>
            <div>worker notes <code>${{esc(w.worker_note_path || 'not recorded')}}</code></div>
            <div>created <code>${{esc(fmt(w.created_at))}}</code></div>
            <div>worker id <code>${{esc(w.worker_id)}}</code></div>
            <div>model <code>${{esc(lastMetrics?.model || '')}}</code></div>
            <div>session <code>${{esc(lastMetrics?.session_id || '')}}</code></div>
            <div>repo <code>${{esc(w.repo_path)}}</code></div>
            <div>branch <code>${{esc(w.branch)}}</code></div>
            <div>worktree <code>${{esc(w.worktree_path)}}</code></div>
            <div>log <code>${{esc(w.log_path)}}</code></div>
          </div>`;
      }}
      let logData;
      try {{
        const logRes = await fetch(`/api/workers/${{encodeURIComponent(selected)}}/log?offset=${{offset}}`, {{ cache: 'no-store' }});
        logData = await logRes.json();
      }} catch (err) {{
        liveLabelEl.textContent = 'poll error';
        liveLabelEl.className = 'poll-error';
        return;
      }}
      liveLabelEl.className = '';
      lastPollEl.textContent = new Date().toLocaleTimeString();
      liveLabelEl.textContent = logData.status === 'running' ? 'live' : logData.status || 'polling';
      if (typeof logData.offset === 'number') {{
        offset = logData.offset;
        byteCountEl.textContent = String(offset);
      }}
      if (logData.text) {{
        appendLogText(logData.text);
      }}
    }}

    function interventionPayload() {{
      return {{
        actor: 'codex-ui',
        message: interventionTextEl.value.trim(),
      }};
    }}
    async function postIntervention(kind) {{
      if (!selected) return;
      const payload = interventionPayload();
      if (!payload.message) {{
        interventionTextEl.focus();
        return;
      }}
      if (kind === 'interrupt' && !confirm('Stop this worker now? The worktree and logs will be preserved.')) {{
        return;
      }}
      if (kind === 'interrupt') payload.reason = payload.message;
      noteButtonEl.disabled = true;
      stopButtonEl.disabled = true;
      try {{
        const res = await fetch(`/api/workers/${{encodeURIComponent(selected)}}/${{kind === 'interrupt' ? 'interrupt' : 'note'}}`, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'intervention failed');
        interventionTextEl.value = '';
        await refreshSelected();
        await refreshWorkers();
      }} catch (err) {{
        liveLabelEl.textContent = String(err.message || err);
        liveLabelEl.className = 'poll-error';
      }} finally {{
        noteButtonEl.disabled = false;
        stopButtonEl.disabled = selectedStatus !== 'running';
      }}
    }}
    noteButtonEl.addEventListener('click', () => postIntervention('note'));
    stopButtonEl.addEventListener('click', () => postIntervention('interrupt'));
    interventionTextEl.addEventListener('keydown', (event) => {{
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {{
        postIntervention('note');
      }}
    }});

    async function refreshHealth() {{
      try {{
        const res = await fetch('/api/health', {{ cache: 'no-store' }});
        const health = await res.json();
        if (health?.script_mtime_ns && health.script_mtime_ns !== pageScriptMtimeNs) {{
          location.reload();
        }}
      }} catch (err) {{
        // refreshWorkers/refreshSelected already surface polling trouble.
      }}
    }}

    logEl.addEventListener('scroll', () => {{
      followLog = (logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight) < 40;
    }});
    window.addEventListener('popstate', () => {{
      const params = new URLSearchParams(location.search);
      selectWorker(params.get('worker') || '', true);
    }});
    refreshWorkers().then(refreshSelected);
    refreshHealth();
    setInterval(refreshWorkers, 2500);
    setInterval(refreshSelected, 1000);
    setInterval(refreshHealth, 5000);
  </script>
</body>
</html>"""


def collect(args: argparse.Namespace) -> dict[str, Any]:
    row = db_row(args.worker_id)
    repo = Path(row["repo_path"])
    worktree = Path(row["worktree_path"])
    status = worker_status(row)
    diff_cp = run_git(worktree, ["diff", "HEAD", "--binary"], check=False)
    files_cp = run_git(worktree, ["status", "--porcelain"], check=False)
    report = None
    result_path = Path(row["result_path"])
    if result_path.exists():
        try:
            report = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {"error": "result json could not be parsed"}
    return {
        "worker_id": args.worker_id,
        "engine": row["engine"],
        "status": status,
        "repo_path": str(repo),
        "base_ref": row["base_ref"],
        "branch": row["branch"],
        "worktree_path": str(worktree),
        "timeout_sec": row["timeout_sec"],
        "files_changed": changed_files_from_status(files_cp.stdout),
        "diff": diff_cp.stdout,
        "diff_stderr": diff_cp.stderr,
        "worker_result": report,
    }


def changed_files_from_status(status_output: str) -> list[str]:
    files: list[str] = []
    for line in status_output.splitlines():
        if not line:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path and path != ".foreman-worker-id" and not path.startswith(".foreman/"):
            files.append(path)
    return files


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    row = db_row(args.worker_id)
    action = args.action
    repo = Path(row["repo_path"])
    worktree = Path(row["worktree_path"])
    status = worker_status(row)
    if action == "discard":
        run_git(repo, ["worktree", "remove", "--force", str(worktree)], check=False)
        run_git(repo, ["branch", "-D", row["branch"]], check=False)
        update_worker(args.worker_id, status="discarded", finished_at=now())
        return {"worker_id": args.worker_id, "action": action, "status": "discarded"}
    if action == "pr":
        commit_worker_changes(worktree, args.worker_id)
        push = run_git(worktree, ["push", "-u", "origin", row["branch"]], check=False)
        pr = subprocess.run(
            ["gh", "pr", "create", "--repo", remote_repo_slug(repo), "--base", row["base_ref"], "--head", row["branch"], "--title", f"Foreman worker {args.worker_id}", "--body", row["spec"]],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        update_worker(args.worker_id, status="pr_opened" if pr.returncode == 0 else status)
        return {"worker_id": args.worker_id, "action": action, "push_stdout": push.stdout, "push_stderr": push.stderr, "pr_stdout": pr.stdout, "pr_stderr": pr.stderr, "returncode": pr.returncode}
    if action == "merge":
        commit_worker_changes(worktree, args.worker_id)
        merge = run_git(repo, ["merge", "--no-ff", row["branch"]], check=False)
        if merge.returncode == 0:
            update_worker(args.worker_id, status="merged", finished_at=now())
        return {"worker_id": args.worker_id, "action": action, "returncode": merge.returncode, "stdout": merge.stdout, "stderr": merge.stderr}
    raise SystemExit(f"unknown action: {action}")


def note_worker(args: argparse.Namespace) -> dict[str, Any]:
    return add_worker_note(args.worker_id, args.message, actor=args.actor)


def stop_worker(args: argparse.Namespace) -> dict[str, Any]:
    return interrupt_worker(args.worker_id, args.reason, actor=args.actor)


def commit_worker_changes(worktree: Path, worker_id: str) -> None:
    (worktree / ".foreman-worker-id").unlink(missing_ok=True)
    shutil.rmtree(worktree / ".foreman", ignore_errors=True)
    status = run_git(worktree, ["status", "--porcelain"], check=False).stdout.strip()
    if not status:
        return
    run_git(worktree, ["add", "-A"])
    run_git(worktree, ["commit", "-m", f"Foreman worker {worker_id}"])


def remote_repo_slug(repo: Path) -> str:
    remote = run_git(repo, ["remote", "get-url", "origin"]).stdout.strip()
    if remote.startswith("git@github.com:"):
        return remote.removeprefix("git@github.com:").removesuffix(".git")
    if remote.startswith("https://github.com/"):
        return remote.removeprefix("https://github.com/").removesuffix(".git")
    raise SystemExit(f"cannot infer GitHub repo slug from origin: {remote}")


def run_worker(args: argparse.Namespace) -> None:
    row = db_row(args.worker_id)
    prompt = Path(row["prompt_path"]).read_text(encoding="utf-8")
    started = now()
    argv = engine_command(row["engine"], prompt)
    run_argv = low_impact_command(row["engine"], argv)
    print(f"[foreman] worker_id={args.worker_id}", flush=True)
    print(f"[foreman] engine={row['engine']}", flush=True)
    print(f"[foreman] cwd={row['worktree_path']}", flush=True)
    print(f"[foreman] command={display_command(run_argv)}", flush=True)
    print(f"[foreman] timeout_sec={row['timeout_sec']}", flush=True)
    if os.environ.get("FOREMAN_LOG_INPUT", "1") != "0":
        print("[foreman] input_prompt_begin", flush=True)
        print(prompt, flush=True)
        print("[foreman] input_prompt_end", flush=True)
    try:
        with engine_resource_slot(row["engine"]):
            proc = subprocess.run(
                run_argv,
                cwd=row["worktree_path"],
                text=True,
                timeout=row["timeout_sec"],
                env=engine_env(row["engine"]),
            )
            exit_code = proc.returncode
            timed_out = False
    except subprocess.TimeoutExpired:
        print(f"[foreman] worker timed out after {row['timeout_sec']} seconds", flush=True)
        exit_code = 124
        timed_out = True
    except FileNotFoundError as exc:
        print(f"[foreman] engineer command not found: {argv[0]}", flush=True)
        print(str(exc), flush=True)
        exit_code = 127
        timed_out = False
    finished = now()
    result = {"worker_id": args.worker_id, "started_at": started, "finished_at": finished, "exit_code": exit_code, "timed_out": timed_out}
    Path(row["result_path"]).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    update_worker(args.worker_id, status="done" if exit_code == 0 else "failed", finished_at=finished, exit_code=exit_code)


def engine_command(engine: str, prompt: str) -> list[str]:
    override = os.environ.get(f"FOREMAN_ENGINE_{engine.upper()}_CMD")
    if override:
        parts = shlex.split(override)
        if "{prompt}" in parts:
            return [prompt if part == "{prompt}" else part for part in parts]
        return [*parts, "-p", prompt]
    if engine == "claude":
        return [
            "claude",
            "-p",
            prompt,
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--include-hook-events",
        ]
    if engine == "codex":
        return ["codex", "exec", "--sandbox", "workspace-write", prompt]
    if engine == "gemini":
        return ["gemini", "--skip-trust", "--approval-mode", "yolo", "-p", prompt]
    if engine == "aider":
        return ["aider", "--yes-always", "--message", prompt]
    if engine == "opencode":
        return ["opencode", "run", "--format", "json", prompt]
    if engine == "gemma4":
        model = os.environ.get("FOREMAN_GEMMA4_MODEL", "ollama/gemma3n:e4b")
        return ["opencode", "run", "--model", model, "--format", "json", prompt]
    if engine == "smoke":
        return [sys.executable, str(Path(__file__).resolve().parent / "smoke_engineer.py"), "-p", prompt]
    raise SystemExit(f"unknown engine: {engine}")


def display_command(argv: list[str]) -> str:
    if "-p" in argv:
        idx = argv.index("-p")
        return " ".join(shlex.quote(part) for part in argv[: idx + 1]) + " <prompt>"
    if "--message" in argv:
        idx = argv.index("--message")
        return " ".join(shlex.quote(part) for part in argv[: idx + 1]) + " <prompt>"
    if "opencode" in argv and "run" in argv and argv[-1] != "run":
        return " ".join(shlex.quote(part) for part in argv[:-1]) + " <prompt>"
    return " ".join(shlex.quote(part) for part in argv)


def add_job_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo")
    parser.add_argument("--engine", choices=ENGINES, default="claude")
    parser.add_argument("--base-ref")
    parser.add_argument("--test-command")
    parser.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--caller", default=None, help="Label for the AI/app/person that requested this worker.")
    parser.add_argument("--parent", default=None, help="Optional parent conversation/session/task identifier.")
    parser.add_argument("--run-id", default=None, help="Optional shared run id. Workers with the same run id share a contract scratchpad.")
    parser.add_argument("--contract", default=None, help="Shared contract text to create or append for this run.")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("spec")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Foreman local delegation runner")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("delegate")
    add_job_arguments(p)
    p.set_defaults(func=delegate)

    p = sub.add_parser("control-init")
    p.set_defaults(func=control_init)

    p = sub.add_parser("control-submit")
    add_job_arguments(p)
    p.set_defaults(func=control_submit)

    p = sub.add_parser("control-jobs")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=control_jobs)

    p = sub.add_parser("control-status")
    p.add_argument("job_id")
    p.set_defaults(func=control_status)

    p = sub.add_parser("control-serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=53640, help="port to bind; pass 0 to pick a free port")
    p.add_argument("--quiet", action="store_true", help="suppress HTTP access logs")
    p.add_argument(
        "--once-smoke",
        action="store_true",
        help="bind, print a JSON status payload, and exit (used for smoke tests)",
    )
    p.set_defaults(func=control_serve)

    p = sub.add_parser("agent-run")
    p.add_argument("--agent-id", default=None)
    p.add_argument("--once", action="store_true", help="lease and run at most one pending control-plane job")
    p.add_argument("--wait", action="store_true", help="wait for the delegated worker to finish before returning")
    p.add_argument("--lease-sec", type=int, default=300)
    p.add_argument("--poll-interval-sec", type=float, default=1.0)
    p.add_argument("--idle-sleep-sec", type=float, default=5.0)
    p.set_defaults(func=agent_run)

    p = sub.add_parser("list")
    p.set_defaults(func=list_workers)

    p = sub.add_parser("tail")
    p.add_argument("worker_id")
    p.add_argument("--lines", type=int, default=100)
    p.set_defaults(func=tail)

    p = sub.add_parser("status")
    p.add_argument("worker_id")
    p.set_defaults(func=worker_status_payload)

    p = sub.add_parser("watch")
    p.add_argument("worker_id")
    p.add_argument("--interval-sec", type=float, default=1.0)
    p.set_defaults(func=watch)

    p = sub.add_parser("monitor-hint")
    p.add_argument("worker_id")
    p.set_defaults(func=monitor_hint)

    p = sub.add_parser("ensure-daemon")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_DAEMON_PORT, help="port to bind when starting; pass 0 to pick a free port")
    p.add_argument("--idle-timeout-sec", type=int, default=DEFAULT_DAEMON_IDLE_TIMEOUT_SEC)
    p.set_defaults(func=ensure_daemon)

    p = sub.add_parser("daemon")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_DAEMON_PORT, help="port to bind; pass 0 to pick a free port")
    p.add_argument("--idle-timeout-sec", type=int, default=DEFAULT_DAEMON_IDLE_TIMEOUT_SEC)
    p.add_argument("--quiet", action="store_true", help="suppress HTTP access logs")
    p.set_defaults(func=run_daemon)

    p = sub.add_parser("web")
    p.add_argument("worker_id", nargs="?")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_DAEMON_PORT, help="port to bind when starting the daemon; pass 0 to pick a free port")
    p.add_argument("--idle-timeout-sec", type=int, default=DEFAULT_DAEMON_IDLE_TIMEOUT_SEC)
    p.add_argument("--open", action="store_true", help="open the monitor in the default browser")
    p.set_defaults(func=run_web)

    p = sub.add_parser("collect")
    p.add_argument("worker_id")
    p.set_defaults(func=collect)

    p = sub.add_parser("finalize")
    p.add_argument("worker_id")
    p.add_argument("action", choices=["merge", "pr", "discard"])
    p.set_defaults(func=finalize)

    p = sub.add_parser("note")
    p.add_argument("worker_id")
    p.add_argument("message")
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=note_worker)

    p = sub.add_parser("interrupt")
    p.add_argument("worker_id")
    p.add_argument("reason")
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=stop_worker)

    p = sub.add_parser("_run-worker")
    p.add_argument("worker_id")
    p.set_defaults(func=run_worker)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = args.func(args)
    if result is not None:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
