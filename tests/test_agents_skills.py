"""Validate the predefined .claude/ agents and skills stay well-formed.

These files make the repo turnkey for an AI coding agent: on clone, Claude Code
auto-discovers the agents (`.claude/agents/*.md`) and skills
(`.claude/skills/*/SKILL.md`). This test guards their frontmatter so a typo
doesn't silently make one undiscoverable. No YAML dependency — the frontmatter
is a simple `key: value` block.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO / ".claude" / "agents"
SKILL_DIR = REPO / ".claude" / "skills"
CODEX_AGENT_DIR = REPO / ".codex" / "agents"
SHARED_SKILL_DIR = REPO / ".agents" / "skills"

EXPECTED_AGENTS = {"coordinator", "explorer", "reviewer", "county-onboarder"}
EXPECTED_SKILLS = {"onboard-locale", "appraisal-check", "add-county"}


def _parse_frontmatter(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.S)
    assert m, f"{path} has no --- frontmatter block"
    fm, body = m.group(1), m.group(2)
    meta = dict(re.findall(r"^([A-Za-z_]+):\s*(.+)$", fm, re.M))
    return meta, body


def test_agents_directory_exists():
    assert AGENT_DIR.is_dir(), "missing .claude/agents"
    assert SKILL_DIR.is_dir(), "missing .claude/skills"
    assert CODEX_AGENT_DIR.is_dir(), "missing .codex/agents"
    assert SHARED_SKILL_DIR.is_dir(), "missing .agents/skills"


def test_expected_agents_present():
    found = {p.stem for p in AGENT_DIR.glob("*.md")}
    assert EXPECTED_AGENTS <= found, f"missing agents: {EXPECTED_AGENTS - found}"


def test_expected_skills_present():
    found = {p.parent.name for p in SKILL_DIR.glob("*/SKILL.md")}
    assert EXPECTED_SKILLS <= found, f"missing skills: {EXPECTED_SKILLS - found}"


def test_agent_frontmatter_wellformed():
    for path in AGENT_DIR.glob("*.md"):
        meta, body = _parse_frontmatter(path)
        assert meta.get("name") == path.stem, f"{path}: name must match filename"
        assert len(meta.get("description", "")) >= 40, f"{path}: description too thin"
        assert len(body.strip()) >= 200, f"{path}: body too thin"


def test_skill_frontmatter_wellformed():
    for path in SKILL_DIR.glob("*/SKILL.md"):
        meta, body = _parse_frontmatter(path)
        assert meta.get("name") == path.parent.name, \
            f"{path}: skill name must match its directory"
        assert len(meta.get("description", "")) >= 40, f"{path}: description too thin"
        assert len(body.strip()) >= 200, f"{path}: body too thin"


def test_agents_and_skills_referenced_by_coordinator():
    """The coordinator should point at the other agents/skills so they're used."""
    body = (AGENT_DIR / "coordinator.md").read_text(encoding="utf-8")
    for name in ("explorer", "reviewer", "onboard-locale", "appraisal-check",
                 "add-county"):
        assert name in body, f"coordinator doesn't reference {name}"


def test_codex_agents_are_well_formed():
    found = {p.stem for p in CODEX_AGENT_DIR.glob("*.toml")}
    assert EXPECTED_AGENTS <= found, f"missing Codex agents: {EXPECTED_AGENTS - found}"
    for path in CODEX_AGENT_DIR.glob("*.toml"):
        text = path.read_text(encoding="utf-8")
        assert f'name = "{path.stem}"' in text
        assert "description =" in text
        assert "developer_instructions =" in text


def test_shared_skills_are_well_formed():
    found = {p.parent.name for p in SHARED_SKILL_DIR.glob("*/SKILL.md")}
    assert EXPECTED_SKILLS <= found, f"missing shared skills: {EXPECTED_SKILLS - found}"
    for path in SHARED_SKILL_DIR.glob("*/SKILL.md"):
        meta, body = _parse_frontmatter(path)
        assert meta.get("name") == path.parent.name
        assert len(meta.get("description", "")) >= 40
        assert len(body.strip()) >= 200


def test_agents_doc_uses_real_codex_paths():
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert ".codex/agents/" in text
    assert ".agents/skills/" in text
    assert ".Codex/" not in text
