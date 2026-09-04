"""There are two doors on the landing page, and both are labelled.

A physician walking the site to APPLY found one auth control in the header,
"Sign in", and beside it "Request products", which is the data-buyer door. So
the choice a new contributor actually faced was between a door that needs an
account they do not have and a door meant for someone else. They guessed.

The fix is not clever: put "Sign up" next to "Sign in". What IS worth pinning is
the second half, which is invisible and easy to lose in a later refactor. Sign
up must go through ``goToJoin``, not through a plain ``href="/join"``, because
``goToJoin`` carries any ``?ref=`` on the current URL into the signup. A bare
anchor drops it, and dropping it means a physician who arrived on a colleague's
referral link is silently not credited to them, which is money.
"""

from __future__ import annotations

import re
from pathlib import Path

_ARCH = Path(__file__).resolve().parents[2] / "landing" / "src" / "app" / "components" / "arch"
_SHELL = (_ARCH / "ArchShell.tsx").read_text(encoding="utf-8")
_MENU = (_ARCH / "MenuPanel.tsx").read_text(encoding="utf-8")

_COMMENT = re.compile(r"\{/\*.*?\*/\}", re.DOTALL)


def _code(src: str) -> str:
    """JSX comments stripped, so a mention in prose cannot satisfy a test."""
    return _COMMENT.sub("", src)


def test_the_header_offers_sign_in_and_sign_up_together():
    code = _code(_SHELL)
    assert ">\n                    Sign in\n" in code or ">Sign in<" in code
    assert ">\n                    Sign up\n" in code or ">Sign up<" in code


def test_sign_up_preserves_the_referral_code():
    """The invisible half. ``goToJoin`` reads ?ref= and passes it to /join."""
    code = _code(_SHELL)
    m = re.search(r"onClick=\{(\w+)\}>\s*\n?\s*Sign up", code)
    assert m, "Sign up is not wired to a named handler"
    assert m.group(1) == "goToJoin", (
        f"Sign up calls {m.group(1)}, not goToJoin. A plain href to /join drops "
        "?ref= and a referred physician stops being credited to the colleague "
        "who sent them."
    )
    assert 'URLSearchParams(window.location.search).get("ref")' in code, (
        "goToJoin stopped reading ?ref="
    )


def test_the_mobile_menu_offers_both_doors_too():
    code = _code(_MENU)
    assert ">Sign in<" in code and ">Sign up<" in code
    assert "onSignUp: () => void;" in code, "onSignUp is not on the prop type"


def test_the_menu_sign_up_is_wired_from_the_shell():
    code = _code(_SHELL)
    assert "onSignUp={" in code, "MenuPanel is rendered without onSignUp"
    # Whatever it does, it must end up in the same place the header does.
    tail = code[code.index("onSignUp={"):][:220]
    assert "goToJoin" in tail, tail
