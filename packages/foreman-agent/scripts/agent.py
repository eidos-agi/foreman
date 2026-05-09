#!/usr/bin/env python3
"""Foreman worker-agent entrypoint.

Thin wrapper that delegates to the Foreman CLI ``agent-run`` subcommand. All
business logic (control-plane leasing, worktree dispatch, lifecycle) lives in
``packages/foreman-cli/scripts/foreman.py``; this script only forwards
arguments so a host can run ``packages/foreman-agent/scripts/agent.py`` as the
canonical worker-agent entrypoint without learning the CLI package layout.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def _cli_target() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "foreman-cli"
        / "scripts"
        / "foreman.py"
    )


def main() -> None:
    target = _cli_target()
    sys.argv = [str(target), "agent-run", *sys.argv[1:]]
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
