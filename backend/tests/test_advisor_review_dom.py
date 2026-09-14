"""Run the actual admin module: advisor proposal, decisions and legacy repair."""
import json
import shutil
import subprocess

import pytest

from tests.test_decision_screen_dossier import _HARNESS, _DOM_SHIM, _MODULE


def _run(action, *, legacy=False, advisor=True):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    kind = "advisor" if advisor else None
    row = {"id": "u1", "user_id": "u1", "full_name": "Test Applicant", "name": "Test Applicant",
           "email": "applicant@example.org", "account_kind": kind, "proposed_tier": "reviewer",
           "proposed_tier_word": "Reviewer", "allowed_tiers": ["reviewer"],
           "tier_words": {"reviewer": "Reviewer", "labeler": "Labeler"}, "blockers": []}
    responses = {
        "/admin/physicians": {"physicians": [], "counts": {"all": 0},
                              "unfiled_physicians": [row] if legacy else [],
                              "unfiled_count": int(legacy)},
        "/verify/queue?status=pending": {"queue": [] if legacy else [row], "total": 0 if legacy else 1},
        "/admin/signups": {"signups": []},
        "/verify/queue/u1": row,
        "/verify/queue/u1/approve": {"ok": True, "welcome_email_queued": True},
        "/verify/queue/u1/reject": {"ok": True},
        "/admin/physicians/restore?email=applicant%40example.org": {"ok": True},
    }
    harness = _HARNESS.replace("api: function (p) {", "api: function (p, opts) {\n    WRITES.push({path:p, options:opts});")
    script = harness % {
        "shim": json.dumps(str(_DOM_SHIM.resolve())), "module": json.dumps(str(_MODULE)),
        "responses": json.dumps(responses), "body": """
const WRITES = [];
window.AdminPhysiciansSection.render(body, ctx);
later(function () {
  const queueText = textOf(body);
  const row = find(body, e => e.tagName === 'TR' && textOf(e).includes('Test Applicant'))[0];
  if (row) row.dispatch('click');
  later(function () {
    const detailText = textOf(body);
    const buttons = find(body, e => e.tagName === 'BUTTON');
    const labels = buttons.map(textOf);
    const note = find(body, e => e.tagName === 'TEXTAREA')[0];
    if (note) note.value = 'Owner decision';
    const button = buttons.find(e => textOf(e) === ACTION);
    if (!button) throw new Error('Missing action: ' + ACTION + '; ' + labels.join(','));
    if (button.onclick) button.onclick();
    else button.dispatch('click');
    later(function () {
      console.log(JSON.stringify({queueText, detailText, labels,
        writes: WRITES.filter(c => c.options && c.options.method === 'POST')}));
    }, 8);
  }, 8);
}, 8);
""".replace("ACTION", json.dumps(action)),
    }
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_advisor_queue_proposes_reviewer_and_approval_does_not_train_model():
    result = _run("Approve as Reviewer")
    assert "Waiting on your decision (1)" in result["queueText"]
    assert "Reviewer" in result["queueText"]
    assert "Not required" in result["queueText"]
    assert "Proposed: Reviewer" in result["detailText"]
    assert "Approve as Labeler" not in result["labels"]
    assert "Reject" in result["labels"]
    assert result["writes"] == [{"path": "/verify/queue/u1/approve",
                                  "options": {"method": "POST", "body": {"tier": "reviewer", "note": "Owner decision"}}}]


def test_advisor_rejection_records_the_owners_note():
    result = _run("Reject")
    assert result["writes"] == [{"path": "/verify/queue/u1/reject",
                                  "options": {"method": "POST", "body": {"note": "Owner decision"}}}]


@pytest.mark.parametrize("advisor,tier", [(True, "Reviewer"), (False, "labeler")])
def test_legacy_banner_approval_uses_the_applicants_kind(advisor, tier):
    result = _run("Approve as " + tier, legacy=True, advisor=advisor)
    assert len(result["writes"]) == 1
    write = result["writes"][0]
    assert write["path"] == "/admin/physicians/restore?email=applicant%40example.org"
    assert write["options"]["body"]["tier"] == tier.lower()
    assert write["options"]["body"]["approve_verification"] is True
