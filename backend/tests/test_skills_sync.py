"""Guard against drift between the filesystem skills (.claude/skills) and the
code they document. Skills are dev/agent-side references; if these fail, the
skill docs need updating alongside the code change."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eligibility.evaluate import CHECK_LABELS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# Harness PRD H1 retired these three to `_retired/`: they describe peri-op code
# that is flag-gated for deletion, and a live skill describing dead code sends an
# agent to edit it. The drift guard still applies while the code is still here —
# only the directory moved.
SKILLS_DIR = REPO_ROOT / ".claude" / "skills" / "_retired"

EXPECTED_SKILLS = ["team-eligibility-review", "ehr-extraction", "surgical-risk-triage"]


def test_retired_skills_are_not_active():
    """They must not reappear under .claude/skills/ itself."""
    active = REPO_ROOT / ".claude" / "skills"
    for name in EXPECTED_SKILLS:
        assert not (active / name).exists(), f"{name} is active again — see Harness PRD H1"


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must start with YAML frontmatter"
    fields = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


def test_skills_exist_with_valid_frontmatter():
    for name in EXPECTED_SKILLS:
        skill_md = SKILLS_DIR / name / "SKILL.md"
        assert skill_md.is_file(), f"missing {skill_md}"
        fm = _frontmatter(skill_md.read_text(encoding="utf-8"))
        assert fm.get("name") == name
        desc = fm.get("description", "")
        assert desc and len(desc) <= 1024
        # Skill name constraints: lowercase letters, numbers, hyphens.
        assert re.fullmatch(r"[a-z0-9-]{1,64}", name)


def test_eligibility_rubric_covers_all_checks():
    rubric = (SKILLS_DIR / "team-eligibility-review" / "references" / "rubric.md").read_text(
        encoding="utf-8"
    )
    for key in CHECK_LABELS:
        assert f"`{key}`" in rubric, f"rubric.md missing check {key}"


def test_skill_file_references_still_exist():
    pattern = re.compile(r"`(backend/[A-Za-z0-9_/.]+\.py)`")
    for name in EXPECTED_SKILLS:
        text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        for ref in pattern.findall(text):
            assert (REPO_ROOT / ref).is_file(), f"{name}/SKILL.md references missing file {ref}"
