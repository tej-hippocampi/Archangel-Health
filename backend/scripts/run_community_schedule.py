"""Drive idempotent community endpoints; fail visibly on missing setup/errors."""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the internal bearer credential elsewhere.


ROUTES = {
    "news": ["run-digest?kind=news&scheduled=true"],
    "routine": ["run-morning", "run-newsletter",
                "run-digest?kind=papers&scheduled=true", "run-spotlight", "run-webinars"],
}


def run(task: str, *, environ=None, opener=None) -> int:
    env = os.environ if environ is None else environ
    base = env.get("MORNING_BASE_URL", "").strip().rstrip("/")
    secret = env.get("INTERNAL_TOOL_SECRET", "").strip()
    if not base or not secret:
        print("::error::MORNING_BASE_URL and INTERNAL_TOOL_SECRET must both be configured.")
        return 1
    url = urlsplit(base)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path or url.query or url.fragment):
        print("::error::MORNING_BASE_URL must be an HTTPS origin without a path or credentials.")
        return 1
    opener = opener or urllib.request.build_opener(NoRedirect())
    failed = False
    for route in ROUTES[task]:
        request = urllib.request.Request(base + "/internal/community/" + route,
                                         data=b"", method="POST")
        request.add_unredirected_header("Authorization", "Bearer " + secret)
        try:
            with opener.open(request, timeout=900) as response:
                result = json.load(response)
            if not isinstance(result, dict) or not any(
                    k in result for k in ("ok", "ran", "cohorts", "created")):
                raise ValueError("unexpected response shape")
            if task == "news" and result.get("ok") is not False:
                if (result.get("ok") is not True or type(result.get("posted")) is not int
                        or result["posted"] < 0):
                    raise ValueError("news response is missing its publication count")
            # HTTP 200 also carries application failures. Backoff means today's
            # digest still has no post; preserve that signal on subsequent ticks.
            bad = (result.get("ok") is False or bool(result.get("failed"))
                   or result.get("outcome") == "backing_off"
                   or (task == "news" and result.get("posted") == 0
                       and result.get("outcome") not in ("not_due", "already_running")))
            summary = {key: result[key] for key in
                       ("ok", "kind", "outcome", "posted", "reason", "sent") if key in result}
            print(route + ": " + json.dumps(summary))
            if bad:
                print("::error::" + route + " did not complete successfully; inspect the community run ledger.")
                failed = True
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            # Log type/status only, not response bodies, addresses or secrets.
            status = getattr(exc, "code", None)
            print(f"::error::{route} request failed ({type(exc).__name__}, HTTP {status}).")
            failed = True
    return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=ROUTES)
    raise SystemExit(run(parser.parse_args().task))
