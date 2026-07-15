"""Guard: every LLM ``role=`` the Asclepius code calls MUST be registered in
``ai/model_config.py``.

This is the regression test for the V3 empty-queue outage: the multimodal
pipeline called ``role="asclepius_case_gen"`` / ``asclepius_case_judge`` /
``asclepius_hardness_judge`` (and citation ranking called
``asclepius_cite_rank``), but those roles were never added to the model
registry. ``resolve()`` does a direct ``MODEL_REGISTRY[role]`` lookup, so each
call raised ``KeyError`` — which the generators catch and report as "LLM
unavailable", silently dropping every multimodal case and falling V3 back to a
text queue. The unit tests never caught it because they monkeypatch the LLM
calls, so the real ``resolve()`` for these roles was never exercised.

This test closes that gap: it statically scans the asclepius package for every
``role="asclepius_*"`` literal and asserts it resolves, so a newly-introduced
role that isn't wired into the registry fails CI immediately.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.model_config import MODEL_REGISTRY, resolve  # noqa: E402

_ASC_DIR = Path(__file__).resolve().parent.parent / "asclepius"
_ROLE_RE = re.compile(r"""role\s*=\s*["'](asclepius_[a-z_]+)["']""")


def _referenced_roles() -> set[str]:
    roles: set[str] = set()
    for path in _ASC_DIR.rglob("*.py"):
        roles.update(_ROLE_RE.findall(path.read_text(encoding="utf-8")))
    return roles


def test_every_asclepius_llm_role_is_registered():
    roles = _referenced_roles()
    # Sanity: we actually found the roles (guards against a broken scan silently
    # passing). The pipeline references well over a handful.
    assert len(roles) >= 10, f"role scan looks broken, only found: {sorted(roles)}"
    missing = sorted(r for r in roles if r not in MODEL_REGISTRY)
    assert not missing, (
        "Asclepius calls these LLM roles but they are NOT in ai/model_config.py "
        f"(resolve() would KeyError → the caller reports 'LLM unavailable' and "
        f"silently drops work): {missing}"
    )


def test_every_asclepius_role_resolves_to_a_model():
    for role in sorted(_referenced_roles()):
        cfg = resolve(role)
        assert cfg.get("model"), f"role {role!r} resolves without a model: {cfg}"


def test_multimodal_pipeline_roles_present():
    # The exact roles whose absence caused the V3 outage — assert explicitly so the
    # intent is legible even if the static scan is ever changed.
    for role in ("asclepius_case_gen", "asclepius_case_judge", "asclepius_hardness_judge"):
        assert role in MODEL_REGISTRY, f"multimodal pipeline role missing: {role}"
