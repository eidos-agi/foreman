#!/usr/bin/env python3
"""Deterministic fake engineer for Foreman smoke tests."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--prompt", required=True)
    parser.parse_args()

    canopy = Path("src/treemark/canopy.py")
    init_file = Path("src/treemark/__init__.py")
    test_file = Path("tests/test_canopy.py")

    if not canopy.exists() or not init_file.exists() or not test_file.exists():
        heartbeats = int(os.environ.get("FOREMAN_SMOKE_HEARTBEATS", "3"))
        interval_sec = float(os.environ.get("FOREMAN_SMOKE_INTERVAL_SEC", "0.5"))
        for idx in range(1, heartbeats + 1):
            print(f"smoke_engineer heartbeat {idx}/{heartbeats}", flush=True)
            time.sleep(interval_sec)
        Path("foreman-smoke-output.txt").write_text("generic foreman smoke worker completed\n", encoding="utf-8")
        print("smoke_engineer wrote foreman-smoke-output.txt", flush=True)
        return

    canopy.write_text(
        canopy.read_text(encoding="utf-8")
        + '\n\n\ndef canopy_label(species: str, height_ft: int) -> str:\n    """Return a display label used by Foreman smoke tests."""\n    return summarize_tree(species, height_ft).upper()\n',
        encoding="utf-8",
    )
    init_file.write_text(
        "from .canopy import canopy_label, summarize_tree\n\n__all__ = [\"canopy_label\", \"summarize_tree\"]\n",
        encoding="utf-8",
    )
    test_file.write_text(
        test_file.read_text(encoding="utf-8").replace(
            "from treemark import summarize_tree",
            "from treemark import canopy_label, summarize_tree",
        ).replace(
            "    def test_summarize_tree_rejects_non_positive_height(self) -> None:\n",
            "    def test_canopy_label(self) -> None:\n        self.assertEqual(canopy_label(\"Live oak\", 42), \"LIVE OAK: 42 FT\")\n\n    def test_summarize_tree_rejects_non_positive_height(self) -> None:\n",
        ),
        encoding="utf-8",
    )
    print("smoke_engineer changed canopy_label and tests")


if __name__ == "__main__":
    main()
