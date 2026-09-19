"""Small CI-only Anthropic availability probe before expensive case authorship.

This checks account/model access, not clinical quality or remaining balance.
It sends only a fixed greeting and never receives physician or patient data.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ReviewerUnavailable(RuntimeError):
    pass


async def check_reviewer():
    from ai.model_config import api_model_id, fake_llm_enabled, resolve, resolve_provider
    if os.getenv("GITHUB_ACTIONS") != "true" or fake_llm_enabled():
        raise ReviewerUnavailable("Real reviewer availability checks run in GitHub Actions only, with the fake off.")
    if not all(os.getenv(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")):
        raise ReviewerUnavailable("Both provider API keys must be configured in GitHub Secrets.")
    model = resolve("asclepius_case_judge")["model"]
    if resolve_provider(model) != "anthropic":
        raise ReviewerUnavailable("An independent Anthropic reviewer is required.")
    from anthropic import AsyncAnthropic
    try:
        async with AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=0, timeout=30) as client:
            await client.messages.create(model=api_model_id(model), max_tokens=64,
                                         messages=[{"role": "user", "content": "Reply OK."}])
    except Exception as exc:
        # Never print or retain raw SDK bodies; some failures echo credentials.
        status = getattr(exc, "status_code", None)
        credit = status == 400 and "credit balance is too low" in str(exc).lower()
        message = ("Anthropic API credits are unavailable; restore billing before retrying."
                   if credit else f"Anthropic reviewer unavailable (HTTP {status if isinstance(status, int) else 'unknown'}).")
        raise ReviewerUnavailable(message) from None


if __name__ == "__main__":
    try:
        asyncio.run(check_reviewer())
    except ReviewerUnavailable as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    print("Anthropic reviewer access confirmed; clinical review is still required for every case.")
