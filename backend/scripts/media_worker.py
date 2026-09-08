"""Run separately from the web service: python -m scripts.media_worker.

Use --migrate once before enabling the API. No migrations alter clinical data.
"""
import argparse
import logging
import time

import realm
from asclepius.media_store import enabled, get_store
from asclepius.media_storage import get_storage
from asclepius.media_service import tick, reap_orphans


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--reap-orphans", action="store_true")
    args = parser.parse_args()
    if args.migrate:
        get_store().migrate()
        return
    if not enabled():
        raise SystemExit("Enable media only after configuration and migration.")
    if args.reap_orphans:
        for scope in realm.active_realms():
            with realm.scoped(scope):
                print(scope, reap_orphans(get_store(), get_storage()))
        return
    while True:
        for scope in realm.active_realms():
            with realm.scoped(scope):
                try:
                    tick(get_store(), get_storage())
                except Exception:
                    logging.exception("Media work failed; durable lease will retry")
        time.sleep(2)


if __name__ == "__main__":
    main()
