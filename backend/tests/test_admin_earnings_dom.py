"""The admin console's Money section, and the reorganized IA.

Source assertions over the shell and the new module: the console's four
sections exist, every legacy tab id still routes somewhere, and the Money
section carries the ledger's mark-paid flow and the referral book.
"""

from __future__ import annotations

from pathlib import Path

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_PORTAL_JS = _FRONTEND / "asclepius.js"
_MONEY_JS = _FRONTEND / "admin_earnings.js"
_INDEX = _FRONTEND / "index.html"


def test_the_console_has_four_sections_and_the_old_ids_still_route():
    src = _PORTAL_JS.read_text(encoding="utf-8")
    for pair in ("['physicians', 'Physicians']", "['work', 'Work']",
                 "['money', 'Money']", "['data', 'Data']"):
        assert pair in src, pair
    aliases = src.split("ADMIN_TAB_ALIASES = {")[1].split("};")[0]
    # Every retired top-level id lands on a live section instead of a blank.
    for legacy in ("tasks:", "qa:", "metrics:", "ingestion:", "health:",
                   "export:", "buyers:", "exports:"):
        assert legacy in aliases, legacy


def test_the_money_module_is_registered_and_mounted():
    assert "admin_earnings.js" in _INDEX.read_text(encoding="utf-8")
    src = _PORTAL_JS.read_text(encoding="utf-8")
    assert "window.AdminEarningsSection" in src
    assert "renderAdminMoneySection" in src


def test_the_ledger_view_pays_through_an_idempotent_batch():
    money = _MONEY_JS.read_text(encoding="utf-8")
    assert "/admin/earnings" in money
    assert "mark-paid" in money
    assert "payout_batch_id" in money
    assert "idempotency key" in money
    # Only approved rows are selectable for payment.
    assert "r.status === 'approved'" in money


def test_the_referral_book_names_flags_and_structure():
    money = _MONEY_JS.read_text(encoding="utf-8")
    assert "/admin/referrals" in money
    assert "fraud_flag" in money
    assert "payout_structure" in money


def test_no_innerhtml_in_the_money_module():
    """Comment-stripped: the header comment states the rule by name."""
    source = _MONEY_JS.read_text(encoding="utf-8")
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
    assert "innerHTML" not in "".join(out)
