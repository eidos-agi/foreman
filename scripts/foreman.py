#!/usr/bin/env python3
"""Local worktree Foreman for multi-agent coding delegation."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any


STATE_HOME = Path(os.environ.get("FOREMAN_HOME", "~/.foreman")).expanduser()
DB_PATH = STATE_HOME / "foreman.sqlite3"
WORKTREES_HOME = STATE_HOME / "worktrees"
LOGS_HOME = STATE_HOME / "logs"
MAX_TAIL_BYTES = 2_000_000
ENGINES = ("claude", "codex", "gemini", "aider", "smoke")
DEFAULT_TIMEOUT_SEC = 900


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
        if "engine" not in columns:
            db.execute("ALTER TABLE workers ADD COLUMN engine TEXT NOT NULL DEFAULT 'claude'")
        if "timeout_sec" not in columns:
            db.execute("ALTER TABLE workers ADD COLUMN timeout_sec INTEGER NOT NULL DEFAULT 900")


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


def make_prompt(spec: str, test_command: str | None) -> str:
    test_block = test_command or "No explicit test command was provided. Run the smallest relevant verification and report what you ran."
    return textwrap.dedent(
        f"""
        You are an implementation engineer working for Foreman.

        Stay inside this git worktree. Do not edit files outside the repository. Do not commit, push, open PRs,
        or change remotes. Make the requested file changes, run verification, and return a concise final report.

        Task spec:
        {spec.strip()}

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
    preflight(repo, base_ref, engine, args.allow_dirty)
    worker_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    repo_name = repo.name
    branch = f"foreman/{worker_id}"
    worktree = (WORKTREES_HOME / repo_name / worker_id).resolve()
    log_path = (LOGS_HOME / f"{worker_id}.log").resolve()
    prompt_path = (LOGS_HOME / f"{worker_id}.prompt.md").resolve()
    result_path = (LOGS_HOME / f"{worker_id}.result.json").resolve()

    worktree.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(make_prompt(args.spec, args.test_command), encoding="utf-8")

    try:
        run_git(repo, ["worktree", "add", "-b", branch, str(worktree), base_ref])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"git worktree add failed:\n{exc.stderr}") from exc

    (worktree / ".foreman-worker-id").write_text(worker_id + "\n", encoding="utf-8")

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO workers
            (id, engine, repo_path, base_ref, branch, worktree_path, spec, test_command, timeout_sec, status, pid,
             created_at, started_at, finished_at, exit_code, log_path, prompt_path, result_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', NULL, ?, ?, NULL, NULL, ?, ?, ?)
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
        "repo_path": str(repo),
        "base_ref": base_ref,
        "branch": branch,
        "worktree_path": str(worktree),
        "log_path": str(log_path),
        "timeout_sec": args.timeout_sec,
    }


def worker_status(row: sqlite3.Row) -> str:
    if row["status"] in {"discarded", "merged", "pr_opened"}:
        return row["status"]
    result_path = Path(row["result_path"])
    if result_path.exists():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            status = "done" if payload.get("exit_code") == 0 else "failed"
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
                "engine": row["engine"],
                "status": worker_status(row),
                "repo_path": row["repo_path"],
                "branch": row["branch"],
                "worktree_path": row["worktree_path"],
                "timeout_sec": row["timeout_sec"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            }
        )
    return {"workers": workers}


def tail(args: argparse.Namespace) -> dict[str, Any]:
    row = db_row(args.worker_id)
    path = Path(row["log_path"])
    if not path.exists():
        return {"worker_id": args.worker_id, "status": worker_status(row), "tail": ""}
    data = path.read_bytes()[-MAX_TAIL_BYTES:]
    lines = data.decode("utf-8", errors="replace").splitlines()
    return {"worker_id": args.worker_id, "status": worker_status(row), "tail": "\n".join(lines[-args.lines :])}


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


def collect(args: argparse.Namespace) -> dict[str, Any]:
    row = db_row(args.worker_id)
    repo = Path(row["repo_path"])
    worktree = Path(row["worktree_path"])
    status = worker_status(row)
    diff_cp = run_git(worktree, ["diff", "HEAD", "--binary"], check=False)
    files_cp = run_git(worktree, ["diff", "--name-only", "HEAD"], check=False)
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
        "files_changed": [line for line in files_cp.stdout.splitlines() if line],
        "diff": diff_cp.stdout,
        "diff_stderr": diff_cp.stderr,
        "worker_result": report,
    }


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


def commit_worker_changes(worktree: Path, worker_id: str) -> None:
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
    print(f"[foreman] worker_id={args.worker_id}", flush=True)
    print(f"[foreman] engine={row['engine']}", flush=True)
    print(f"[foreman] cwd={row['worktree_path']}", flush=True)
    print(f"[foreman] command={display_command(argv)}", flush=True)
    print(f"[foreman] timeout_sec={row['timeout_sec']}", flush=True)
    try:
        proc = subprocess.run(argv, cwd=row["worktree_path"], text=True, timeout=row["timeout_sec"])
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
        return [*shlex.split(override), "-p", prompt]
    if engine == "claude":
        return ["claude", "-p", prompt]
    if engine == "codex":
        return ["codex", "exec", "--sandbox", "workspace-write", prompt]
    if engine == "gemini":
        return ["gemini", "--skip-trust", "--approval-mode", "yolo", "-p", prompt]
    if engine == "aider":
        return ["aider", "--yes-always", "--message", prompt]
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
    return " ".join(shlex.quote(part) for part in argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Foreman local delegation runner")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("delegate")
    p.add_argument("--repo")
    p.add_argument("--engine", choices=ENGINES, default="claude")
    p.add_argument("--base-ref")
    p.add_argument("--test-command")
    p.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC)
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("spec")
    p.set_defaults(func=delegate)

    p = sub.add_parser("list")
    p.set_defaults(func=list_workers)

    p = sub.add_parser("tail")
    p.add_argument("worker_id")
    p.add_argument("--lines", type=int, default=100)
    p.set_defaults(func=tail)

    p = sub.add_parser("watch")
    p.add_argument("worker_id")
    p.add_argument("--interval-sec", type=float, default=1.0)
    p.set_defaults(func=watch)

    p = sub.add_parser("collect")
    p.add_argument("worker_id")
    p.set_defaults(func=collect)

    p = sub.add_parser("finalize")
    p.add_argument("worker_id")
    p.add_argument("action", choices=["merge", "pr", "discard"])
    p.set_defaults(func=finalize)

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
