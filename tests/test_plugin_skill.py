from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_skill_documents_foreman_tool_discovery_guard() -> None:
    skill = (REPO / "skills" / "delegate-with-foreman" / "SKILL.md").read_text(encoding="utf-8")

    assert "Tool Discovery Guard" in skill
    assert "foreman_status foreman_tail foreman_collect foreman_finalize" in skill
    assert "partial tool-index exposure" in skill
