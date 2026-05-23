#!/usr/bin/env python3
"""Package-local control-plane wrapper.

This script is the executable surface for the foreman-control-plane package.
It exposes the control-plane subcommands (`init`, `submit`, `jobs`, `status`)
and delegates to the Foreman CLI's existing `control-*` subcommands so the
control plane is not a second implementation of the same logic.

The CLI runtime under packages/foreman-cli owns all business logic for
SQLite schema, lease handling, and event recording. This wrapper exists so
that the control plane can be packaged, deployed, and invoked independently
(for example on a Railway service) without inlining a duplicate state model.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_FOREMAN = REPO_ROOT / "packages" / "foreman-cli" / "scripts" / "foreman.py"

SUBCOMMANDS = {
    "init": "control-init",
    "submit": "control-submit",
    "jobs": "control-jobs",
    "status": "control-status",
}


def usage() -> str:
    names = ", ".join(sorted(SUBCOMMANDS))
    return (
        "usage: control.py <subcommand> [args...]\n"
        f"  subcommands: {names}\n"
        "  delegates to the Foreman CLI control-* commands without duplicating logic.\n"
    )


def resolve_cli() -> Path:
    override = os.environ.get("FOREMAN_CLI")
    if override:
        path = Path(override).expanduser().resolve()
        if path.is_file():
            return path
    if CLI_FOREMAN.is_file():
        return CLI_FOREMAN
    raise SystemExit(
        f"foreman CLI not found at {CLI_FOREMAN}. Set FOREMAN_CLI to override."
    )


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        sys.stdout.write(usage())
        return 0

    sub = argv[0]
    rest = argv[1:]

    if sub in SUBCOMMANDS:
        forwarded = SUBCOMMANDS[sub]
    elif sub.startswith("control-") and sub in SUBCOMMANDS.values():
        forwarded = sub
    else:
        sys.stderr.write(f"unknown subcommand: {sub}\n")
        sys.stderr.write(usage())
        return 2

    cli = resolve_cli()
    cmd = [sys.executable, str(cli), forwarded, *rest]
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
