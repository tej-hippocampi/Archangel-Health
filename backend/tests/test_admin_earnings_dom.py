"""The admin console's Money section, and the reorganized IA.

Source assertions over the shell and the new module: the console's four
sections exist, every legacy tab id still routes somewhere, and the Money
section carries the ledger's mark-paid flow and the referral book.
"""

from __future__ import annotations

from pathlib import Path

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
# PRD-F moved the console shell into its own bundle on its own page. The IA it
# asserts is unchanged; only the file that holds it moved.
_PORTAL_JS = _FRONTEND / "admin_shell.js"
_MONEY_JS = _FRONTEND / "admin_earnings.js"
_ADMIN_HTML = _FRONTEND / "admin.html"



def _strip_js_comments(source: str) -> str:
    """The module talks ABOUT the rules it follows, in comments explaining why.

    A grep that cannot tell prose from code is a test that gets deleted the
    first time somebody documents a rule — "a hardcoded $75 would misreport
    every reviewer" must not read as a hardcoded $75.
    """
    out, i, n = [], 0, len(source)
    while i < n:
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end == -1 else end
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


def test_the_console_has_four_sections_and_the_old_ids_still_route():
    src = _PORTAL_JS.read_text(encoding="utf-8")
    # Admin Launch §1.1 renames the LABELS; the state keys 'work' and 'money'
    # are read in ~12 places and must not move. PRD-F added Community and
    # Referrals beside them and renamed nothing.
    for pair in ("['physicians', 'Physicians']", "['work', 'Tasks']",
                 "['money', 'Money and Metrics']", "['data', 'Data']"):
        assert pair in src, pair
    aliases = src.split("ADMIN_TAB_ALIASES = {")[1].split("};")[0]
    # Every retired top-level id lands on a live section instead of a blank.
    for legacy in ("tasks:", "qa:", "metrics:", "ingestion:", "health:",
                   "export:", "buyers:", "exports:"):
        assert legacy in aliases, legacy


def test_the_money_module_is_registered_and_mounted():
    assert "admin_earnings.js" in _ADMIN_HTML.read_text(encoding="utf-8")
    src = _PORTAL_JS.read_text(encoding="utf-8")
    assert "window.AdminEarningsSection" in src
    assert "renderAdminMoneySection" in src


def test_the_ledger_view_pays_through_an_idempotent_batch():
    money = _MONEY_JS.read_text(encoding="utf-8")
    assert "/admin/earnings" in money
    # §4.5 routes through /admin/earnings/pay, which is thin over
    # asc_payments.mark_paid — the same idempotent path, not a parallel one.
    assert "/admin/earnings/pay" in money
    assert "payout_batch_id" in money
    assert "idempotency key" in money
    # Only approved rows are sent for payment.
    assert "r.status === 'approved'" in money


def test_the_money_screen_is_two_levels_and_the_status_chips_are_gone():
    """§4.1/§4.2: physicians first, then that physician's cases.

    The chip strip filtered a company-wide ledger by ledger state, which is a
    developer's view of the table rather than the question an operator has
    ("what do we owe this doctor?").
    """
    money = _MONEY_JS.read_text(encoding="utf-8")
    assert "selectedUser" in money
    assert "'', 'accrued', 'approved', 'paid', 'void'" not in money, (
        "the status chip strip is back"
    )
    assert "?user_id=" in money, "level 2 does not scope the ledger to one physician"


def test_no_rate_is_hardcoded_anywhere_on_the_money_screen():
    """§4.2: render amount_cents from the row.

    Reviewers are paid per SESSION (tr_session_cents), not per case, so a
    hardcoded task rate would silently misreport every reviewer on the screen.
    """
    money = _strip_js_comments(_MONEY_JS.read_text(encoding="utf-8"))
    assert "amount_cents" in money
    for literal in ("7500", "$75", "75.00"):
        assert literal not in money, f"a rate literal ({literal}) is on the screen"


def test_a_void_requires_a_typed_reason_and_never_a_native_confirm():
    money = _strip_js_comments(_MONEY_JS.read_text(encoding="utf-8"))
    # No native confirm(): it cannot capture a reason, and a void with no reason
    # cannot be audited or appealed.
    assert "window.confirm" not in money
    assert "confirm(" not in money.replace("voidConfirm(", "")
    assert "reason: reason" in money
    # The server's recomputed total, never a local subtraction.
    assert "res.totals" in money or "(res.totals || {})" in money


def test_an_unknown_duration_renders_a_placeholder_and_never_zero_minutes():
    """§4.3: a zero meaning "unknown" is how an operator voids honest work."""
    money = _MONEY_JS.read_text(encoding="utf-8")
    assert "function duration(" in money
    assert "if (!s) return '-';" in money


def test_the_referral_book_names_flags_and_structure():
    money = _MONEY_JS.read_text(encoding="utf-8")
    assert "/admin/referrals" in money
    assert "fraud_flag" in money
    assert "payout_structure" in money


def test_no_innerhtml_in_the_money_module():
    """Comment-stripped: the header comment states the rule by name."""
    assert "innerHTML" not in _strip_js_comments(_MONEY_JS.read_text(encoding="utf-8"))
