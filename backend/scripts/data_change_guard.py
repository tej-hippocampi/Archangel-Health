#!/usr/bin/env python3
"""Reject newly introduced destructive SQL in application code.

A deliberately narrow static gate, complemented by migration inventories and
failure tests. Dynamic SQL, ORM cascades and infrastructure require review.
"""
import argparse
import re
import subprocess
import sys

PROTECTED = r'(?:tasks|submissions|records|earnings|assignments|exports|users|health_systems|signed_agreements|sealed_ground_truth|study_assets|ingest_\w+|hs_\w+|media_\w+|community_\w+|\w+_outbox)'
DANGEROUS = re.compile(r'(?:DELETE\s+FROM|DROP\s+TABLE(?:\s+IF\s+EXISTS)?|TRUNCATE(?:\s+TABLE)?|INSERT\s+OR\s+REPLACE\s+INTO|REPLACE\s+INTO)\s+["`\[]?' + PROTECTED + r'\b', re.I)


def violations(text):
    return [m.group(0) for m in DANGEROUS.finditer(text)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='origin/main')
    args = parser.parse_args()
    result = subprocess.run(['git', 'diff', '--no-ext-diff', '--unified=0', args.base, '--', 'backend'], capture_output=True, text=True, check=True)
    path, additions, errors = '', [], []
    def check():
        if path.endswith(('.py', '.sql')) and '/tests/' not in path:
            errors.extend(f'{path}: {hit}' for hit in violations('\n'.join(additions)))
    for line in result.stdout.splitlines():
        if line.startswith('+++ b/'):
            check()
            path, additions = line[6:], []
        elif line.startswith('+') and not line.startswith('+++'):
            content = line[1:]
            if not content.lstrip().startswith('#'):
                additions.append(content)
    check()
    if errors:
        print('Data preservation gate failed. Use append-only revisions; do not erase existing evidence.\n' + '\n'.join(errors), file=sys.stderr)
        return 2
    print('No newly added destructive SQL matched. Inventory and restore evidence are still required.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
