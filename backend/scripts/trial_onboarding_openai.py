"""CI-only OpenAI experiment. Outputs cannot be loaded as release case bundles.

One attempt per missing case bounds spend and measures first-pass yield. The
unchanged release validator still requires independent OpenAI/Anthropic review.
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REVISION_INPUTS = Path(__file__).with_name("onboarding_revision_inputs.json")

CALL_USAGE = ContextVar('openai_trial_call_usage', default=None)


class TrialProviderError(RuntimeError):
    def __init__(self, status):
        self.status_code = status if isinstance(status, int) else None
        super().__init__(f'OpenAI request failed (HTTP {self.status_code or "unknown"})')


async def call_openai(*, role, system, messages, purpose, max_tokens, model='gpt-5.6-sol'):
    """Trial-only Responses transport: one request, no SDK logs or shared stores."""
    from asclepius.onboarding_cases import openai_trial_models
    from ai.llm_client import _LLMResult, _openai_input, _openai_output_cap
    from openai import AsyncOpenAI
    if model not in openai_trial_models() or purpose not in (
            'onboarding_case_author', 'onboarding_case_solve', 'onboarding_case_review'):
        raise ValueError('Unexpected OpenAI trial call')
    try:
        async with AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'], max_retries=0, timeout=180) as client:
            response = await client.responses.create(
                model=model, instructions=system, input=_openai_input(messages),
                max_output_tokens=_openai_output_cap(max_tokens, True), store=False)
    except Exception as exc:
        raise TrialProviderError(getattr(exc, 'status_code', None)) from None
    usage = getattr(response, 'usage', None)
    record = {'model': model, 'response_model': getattr(response, 'model', None), 'provider': 'openai',
              'purpose': purpose, 'request_id': getattr(response, 'id', None),
              'input_tokens': getattr(usage, 'input_tokens', None),
              'output_tokens': getattr(usage, 'output_tokens', None)}
    if CALL_USAGE.get() is not None:
        CALL_USAGE.get().append(record)
    if getattr(response, 'status', None) != 'completed' or not getattr(response, 'output_text', ''):
        raise ValueError('OpenAI trial returned incomplete or empty output')
    return _LLMResult(response.output_text, record['input_tokens'], record['output_tokens'], record['request_id']), record


async def check_access():
    from asclepius.onboarding_cases import openai_trial_models
    from openai import AsyncOpenAI
    models = openai_trial_models()
    try:
        async with AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'], max_retries=0, timeout=30) as client:
            for model in models:
                await client.responses.create(model=model, input='Reply OK.', max_output_tokens=64,
                                              store=False)
    except Exception as exc:
        # Provider bodies may contain credentials; expose status only.
        status = getattr(exc, 'status_code', None)
        raise RuntimeError(f'OpenAI trial access unavailable (HTTP {status if isinstance(status, int) else "unknown"}).') from None
    print(json.dumps({'openai_access': True, 'models': models}), flush=True)


def isolated_paths(output: Path):
    """Do not initialize a store until runner-local paths and emptiness pass."""
    from asclepius.onboarding_cases import openai_trial_models
    openai_trial_models()
    if not os.getenv('RUNNER_TEMP') or not os.getenv('ASCLEPIUS_DB_PATH'):
        raise ValueError('Runner temporary directory and isolated storage are required')
    root = Path(os.environ['RUNNER_TEMP']).resolve()
    db = Path(os.environ['ASCLEPIUS_DB_PATH']).resolve()
    output = output.resolve()
    if (root not in db.parents or root not in output.parents or db.exists()
            or (output.exists() and any(output.iterdir()))):
        raise ValueError('Trial requires a new database and empty output under RUNNER_TEMP')
    return output


def revision_inputs():
    """Immutable CI preparation only; these records never authorize publication."""
    import hashlib
    if not REVISION_INPUTS.is_file():
        return {}
    rows = json.loads(REVISION_INPUTS.read_text())["cases"]
    for ident, row in rows.items():
        if row.get("entry") and hashlib.sha256(json.dumps(row["entry"], sort_keys=True).encode()).hexdigest() != row.get("entry_sha256"):
            raise ValueError("Revision input checksum mismatch: " + ident)
        if row.get("resume") and (not row.get("entry") or not row.get("audit_disposition") == "clear"):
            raise ValueError("Only independently audited trial passes can be resumed")
    return rows


def matrix(value: str):
    from scripts.build_onboarding_library import matrix_specialties
    from asclepius import onboarding_cases as bank, onboarding_library as library
    inputs = revision_inputs()
    return [s for s in matrix_specialties(value)
            if any(library.row_for(bank.task_id(s, k)) is None
                   and not inputs.get(bank.task_id(s, k), {}).get("resume")
                   for k in ('practice', 'examination'))]


async def run(specialty: str, output: Path):
    from asclepius import onboarding_cases as bank, onboarding_library as library
    from asclepius.store import get_store
    if specialty not in matrix(specialty):
        print(json.dumps({'specialty': specialty, 'skipped': 'both cases covered by release library or audited trial inputs'}))
        return
    output = isolated_paths(output)
    await check_access()
    store = get_store()
    output.mkdir(parents=True, exist_ok=True)
    inputs = revision_inputs()
    results = []
    previous = [{"question": row["entry"]["question"], "answer_key": row["entry"]["case"]["ground_truth"]}
                for ident, row in inputs.items() if row.get("resume") and
                ident in {bank.task_id(specialty, k) for k in ('practice', 'examination')}]

    def retain(document):
        document = {**document, 'release_eligible': False, 'experiment': 'openai_only_trial'}
        path = output / ('trial-rejected-' + uuid.uuid4().hex + '.json')
        with path.open('x') as stream:
            json.dump(document, stream, indent=2)

    observer = bank.REVIEW_DIAGNOSTICS.set(retain)
    usage = []
    usage_token = CALL_USAGE.set(usage)
    try:
        for kind in ('practice', 'examination'):
            ident = bank.task_id(specialty, kind)
            if library.row_for(ident) is not None:
                results.append({'kind': kind, 'status': 'existing_release_case_skipped'})
                continue
            if inputs.get(ident, {}).get("resume"):
                results.append({'kind': kind, 'status': 'audited_trial_case_skipped'})
                continue
            print(json.dumps({'specialty': specialty, 'kind': kind, 'status': 'started'}), flush=True)
            try:
                entry, report = await asyncio.wait_for(
                    bank.build_case(store, specialty, kind, ident, openai_trial=True,
                                    previous_trial_cases=previous, revision=inputs.get(ident)), timeout=600)
                assert report['method'] == 'openai_only_trial'
                document = {'task_id': ident, 'specialty': specialty, 'kind': kind, 'slot': 1,
                            'entry': entry, 'validation': report, 'release_eligible': False}
                with (output / ('trial-' + ident + '.json')).open('x') as stream:
                    json.dump(document, stream, indent=2)
                previous.append({'question': entry['question'], 'answer_key': entry['case']['ground_truth']})
                result = {'kind': kind, 'status': 'trial_passed', 'release_eligible': False}
            except Exception as exc:
                # Validation errors are clinical/schema diagnostics. SDK errors
                # are not safe to print; record only their type and HTTP status.
                detail = str(exc)[:5000] if isinstance(exc, ValueError) else (
                    type(exc).__name__ + ': HTTP ' + str(getattr(exc, 'status_code', None)))
                result = {'kind': kind, 'status': 'trial_failed', 'error': detail}
                # Abort paid work after a transport/account failure, including a
                # reviewer exception wrapped by the clinical-review aggregator.
                if not isinstance(exc, ValueError) or 'Error: status=' in detail:
                    results.append(result)
                    print(json.dumps(result), flush=True)
                    raise RuntimeError('OpenAI trial stopped after provider/runtime failure') from None
            results.append(result)
            print(json.dumps({'specialty': specialty, **result}), flush=True)
    finally:
        bank.REVIEW_DIAGNOSTICS.reset(observer)
        CALL_USAGE.reset(usage_token)
        with store._conn() as conn:
            for table in ('tasks', 'submissions', 'records', 'onboarding_case_bank'):
                if conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]:
                    raise RuntimeError('Trial unexpectedly wrote clinical inventory')
        summary = {'specialty': specialty, 'release_eligible': False, 'attempts_per_case': 1,
                   'results': results, 'calls': usage}
        (output / 'trial-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    if any(r['status'] == 'trial_failed' for r in results):
        raise RuntimeError('Some cases did not pass the OpenAI-only trial; see retained diagnostics')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--specialty', default='all')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check-access', action='store_true')
    parser.add_argument('--matrix', action='store_true')
    args = parser.parse_args()
    if args.matrix:
        print(json.dumps(matrix(args.specialty)))
    elif args.check_access:
        asyncio.run(check_access())
    elif args.output:
        from asclepius.onboarding_specialties import canonical
        asyncio.run(run(canonical(args.specialty), args.output))
    else:
        parser.error('--output is required')


if __name__ == '__main__':
    # Use the same module instance as build_case's trial transport import, so
    # diagnostic context and per-call accounting remain connected under CLI use.
    from scripts.trial_onboarding_openai import main as canonical_main
    canonical_main()
