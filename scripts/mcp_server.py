#!/usr/bin/env python3
"""Compatibility wrapper for the Foreman MCP package."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "packages" / "foreman-mcp" / "scripts" / "mcp_server.py"
    runpy.run_path(str(target), run_name="__main__")
