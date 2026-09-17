"""Real-model onboarding smoke. Run only in GitHub Actions with isolated stores.
No physician, CV, patient record, email or production database is used.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run(specialty):
    from asclepius import onboarding_cases as bank
    from asclepius.store import get_store
    from asclepius.onboarding_specialties import canonical
    store = get_store()
    specialty = canonical(specialty)
    for kind in ('practice', 'examination'):
        ident = bank.task_id(specialty, kind)
        bank.request_case(store, specialty, kind)
        await asyncio.gather(*list(bank._RUNNING))
        row = bank.row_for(store, ident)
        if row['status'] != 'ready':
            raise RuntimeError(f'{specialty} {kind}: {row["status"]} / {row["error_code"]}')
        validation = json.loads(row['validation_json'])
        entry = json.loads(row['entry_json'])
        print(json.dumps({'specialty': specialty, 'kind': kind, 'task_id': ident,
            'question': entry['question'], 'validation': validation}, indent=2), flush=True)
        assert store.get_task(ident) is None
    with store._conn() as conn:
        assert conn.execute('SELECT count(*) FROM submissions').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM records').fetchone()[0] == 0
    print('Both specialty cases passed real-model evidence review; paid inventory untouched.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--specialty', required=True)
    args = parser.parse_args()
    if os.getenv('GITHUB_ACTIONS') != 'true' or os.getenv('ASCLEPIUS_LLM_PROVIDER') == 'fake':
        raise SystemExit('This real-model check runs in GitHub Actions only, with the fake off.')
    if not all(os.getenv(key) for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'ASCLEPIUS_DB_PATH')):
        raise SystemExit('Both provider keys and an isolated database path are required.')
    asyncio.run(run(args.specialty))
