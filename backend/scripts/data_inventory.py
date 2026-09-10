#!/usr/bin/env python3
"""Read-only preservation check for every SQLite table and selected file trees.

Snapshot before a migration; diff afterward with writers paused. Added columns
and rows are allowed. Missing rows/files or changed existing values fail closed.
Use --allow-change table.column only for reviewed, intentional transformations.
Run separately for every live database (including team, community and media).
This detects change; it is not a backup or a proof of production durability.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import sqlite3
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = BACKEND.parent / 'docs' / 'asclepius'
TABLES = ('tasks', 'submissions', 'records', 'earnings', 'uploads', 'assignments', 'exports')


def _db_path():
    sys.path.insert(0, str(BACKEND))
    import realm
    return realm.live_asclepius_db()


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _digest(value):
    if isinstance(value, bytes):
        value = {'bytes': value.hex()}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def snapshot(db=None, blob_roots=None):
    path = pathlib.Path(db or _db_path()).resolve()
    if not path.is_file():
        raise ValueError(f'no database at {path}; refusing an empty baseline')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        conn.execute('BEGIN')
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('database integrity check failed')
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if not tables:
            raise ValueError('database has no application tables')
        out = {}
        for table in tables:
            columns = list(conn.execute(f'PRAGMA table_info({_quote(table)})'))
            names = [r[1] for r in columns]
            pk = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
            selection = '*' if pk else 'rowid, *'
            fields = {}
            for row in conn.execute(f'SELECT {selection} FROM {_quote(table)}'):
                values = dict(zip(names, row if pk else row[1:]))
                key = json.dumps([values[c] for c in pk], default=str) if len(pk) > 1 else str(values[pk[0]]) if pk else str(row[0])
                if key in fields:
                    raise ValueError(f'{table}: non-unique row identity')
                fields[key] = {c: _digest(v) for c, v in values.items()}
            label = 'uploads' if table == 'ingest_uploads' and 'uploads' not in tables else table
            out[label] = {'table': table, 'id_columns': pk or ['rowid'],
                          'count': len(fields), 'ids': sorted(fields), 'fields': fields}
    finally:
        conn.close()
    blobs = {}
    for label, directory in (blob_roots or {}).items():
        base = pathlib.Path(directory).resolve()
        if not base.is_dir():
            raise ValueError(f'blob root {label} is missing')
        files = {}
        for file in sorted(base.rglob('*')):
            if file.is_symlink():
                raise ValueError(f'blob root {label} contains a symlink; resolve its scope explicitly')
            if file.is_file():
                digest = hashlib.sha256()
                with file.open('rb') as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(chunk)
                files[str(file.relative_to(base))] = {'sha256': digest.hexdigest(), 'bytes': file.stat().st_size}
        blobs[label] = {'root': str(base), 'files': files}
    return {'version': 2, 'db': str(path), 'taken_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'tables': out, 'blobs': blobs}


def compare(before, now, allowed=()):
    if not before.get('tables'):
        return ['empty baseline is not preservation evidence']
    problems = []
    allowed = set(allowed)
    for name, previous in before['tables'].items():
        current = now['tables'].get(name)
        if current is None:
            problems.append(f'{name}: table disappeared')
            continue
        missing = set(previous.get('ids') or []) - set(current.get('ids') or [])
        if missing:
            problems.append(f'{name}: {len(missing)} missing ids: {sorted(missing)[:10]}')
        if previous.get('ids') is None and current['count'] < previous['count']:
            problems.append(f'{name}: row count decreased')
        for identity, values in previous.get('fields', {}).items():
            if identity not in current.get('fields', {}):
                continue
            for column, digest in values.items():
                updated = current['fields'][identity]
                # A column disappearing cannot be authorized as a value change.
                if column not in updated or (digest != updated[column] and f"{previous['table']}.{column}" not in allowed):
                    problems.append(f'{name}: existing content changed in {column} (row {identity})')
    for label, previous in before.get('blobs', {}).items():
        current = now.get('blobs', {}).get(label, {}).get('files', {})
        for name, value in previous['files'].items():
            if current.get(name) != value:
                problems.append(f'{label}: file missing or changed: {name}')
    return problems


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--snapshot', action='store_true')
    mode.add_argument('--diff', metavar='BEFORE.json')
    parser.add_argument('--db')
    parser.add_argument('--output')
    parser.add_argument('--blob-root', action='append', default=[], metavar='NAME=PATH')
    parser.add_argument('--allow-change', action='append', default=[], metavar='TABLE.COLUMN')
    args = parser.parse_args(argv[1:])
    try:
        before = json.loads(pathlib.Path(args.diff).read_text()) if args.diff else None
        roots = dict(x.split('=', 1) for x in args.blob_root)
        if before and not roots:
            roots = {k: v['root'] for k, v in before.get('blobs', {}).items()}
        now = snapshot(args.db, roots)
        if args.snapshot:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            output = pathlib.Path(args.output) if args.output else OUT_DIR / f"INVENTORY_{dt.datetime.now().strftime('%Y-%m-%d_%H%M%S_%f')}.json"
            fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as target:
                json.dump(now, target, indent=2)
                target.write('\n')
                target.flush()
                os.fsync(target.fileno())
            print(f"wrote {output}: {len(now['tables'])} tables, {sum(t['count'] for t in now['tables'].values())} rows")
            return 0
        problems = compare(before, now, args.allow_change)
        if problems:
            print('PRESERVATION CHECK FAILED:\n' + '\n'.join(problems[:50]), file=sys.stderr)
            return 2
        if before.get('version') != 2:
            print('Legacy baseline: IDs only; content and blobs were not measured.', file=sys.stderr)
        print('no ids lost; all baselined content and files preserved')
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f'PRESERVATION CHECK FAILED: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
