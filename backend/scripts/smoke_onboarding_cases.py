"""Real-model onboarding smoke. Run only in GitHub Actions with isolated stores.
No physician, CV, patient record, email or production database is used.

Authoring is probabilistic: a real model can miss a machine-checked requirement
and the case lands on ``retry_wait``. Production already tolerates that — the
case-preparation poll re-leases the row once ``RETRY_SECONDS`` expires — so a
single-shot smoke was stricter than the product it certifies. This retries the
same way production does and reports how many attempts each case needed, so
first-pass yield stays visible evidence instead of being hidden by the retry.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ATTEMPTS = max(1, int(os.getenv('SMOKE_ATTEMPTS', '3')))


async def prepare(store, bank, specialty, kind):
    """Drive one case to 'ready', returning its row and the attempts it took."""
    ident = bank.task_id(specialty, kind)
    failures = []
    for n in range(1, ATTEMPTS + 1):
        bank.request_case(store, specialty, kind)
        await asyncio.gather(*list(bank._RUNNING))
        row = bank.row_for(store, ident)
        if row['status'] == 'ready':
            return ident, row, n, failures
        failures.append(row['error_code'])
        print(f'[smoke] {specialty} {kind}: attempt {n}/{ATTEMPTS} -> '
              f'{row["status"]} / {row["error_code"]}', flush=True)
        if n < ATTEMPTS:
            # Wait the retry lease out rather than clearing it, so the path under
            # test is the one a physician's polling UI actually takes.
            await asyncio.sleep(max(0.0, (row['lease_until'] or 0) - time.time()) + 1)
    raise RuntimeError(f'{specialty} {kind}: no case after {ATTEMPTS} attempts: {failures}')


async def run(specialty):
    from asclepius import onboarding_cases as bank
    from asclepius.store import get_store
    from asclepius.onboarding_specialties import canonical
    store = get_store()
    specialty = canonical(specialty)
    yields = {}
    for kind in ('practice', 'examination'):
        ident, row, attempts, failures = await prepare(store, bank, specialty, kind)
        yields[kind] = {'attempts': attempts, 'rejected': failures}
        validation = json.loads(row['validation_json'])
        entry = json.loads(row['entry_json'])
        print(json.dumps({'specialty': specialty, 'kind': kind, 'task_id': ident,
            'question': entry['question'], 'attempts': attempts,
            'rejected_before_ready': failures, 'validation': validation}, indent=2), flush=True)
        assert store.get_task(ident) is None
    with store._conn() as conn:
        assert conn.execute('SELECT count(*) FROM submissions').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM records').fetchone()[0] == 0
    print(json.dumps({'specialty': specialty, 'attempt_budget': ATTEMPTS, 'yield': yields}), flush=True)
    first_pass = [k for k, v in yields.items() if v['attempts'] == 1]
    print(f'Both specialty cases passed real-model evidence review; paid inventory '
          f'untouched. First-pass: {len(first_pass)}/{len(yields)}.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--specialty', required=True)
    args = parser.parse_args()
    if os.getenv('GITHUB_ACTIONS') != 'true' or os.getenv('ASCLEPIUS_LLM_PROVIDER') == 'fake':
        raise SystemExit('This real-model check runs in GitHub Actions only, with the fake off.')
    if not all(os.getenv(key) for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'ASCLEPIUS_DB_PATH')):
        raise SystemExit('Both provider keys and an isolated database path are required.')
    asyncio.run(run(args.specialty))
