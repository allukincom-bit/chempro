"""Offline/online-safe SQLite maintenance for lovetech. No Flask dependency."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

REQUIRED_TABLES = ('users', 'materials', 'products', 'recipes', 'lots', 'audit')

def inspect_database(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f'Database not found: {path}')
    # mode=ro prevents accidental creation or modification.
    with sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True, timeout=20) as db:
        integrity = [row[0] for row in db.execute('PRAGMA integrity_check')]
        fk = db.execute('PRAGMA foreign_key_check').fetchall()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = db.execute('PRAGMA user_version').fetchone()[0]
    return {'integrity_ok': integrity == ['ok'], 'integrity_messages': integrity[:20],
            'foreign_key_violations': len(fk), 'missing_tables': sorted(set(REQUIRED_TABLES)-tables),
            'user_version': version, 'table_count': len(tables)}

def create_backup(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise FileNotFoundError(f'Database not found: {source}')
    if source.resolve() == destination.resolve():
        raise ValueError('Backup must not overwrite source')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite an existing backup. Temp file is created in target directory.
    if destination.exists():
        raise FileExistsError(destination)
    tmp = destination.with_name(destination.name + '.partial')
    if tmp.exists():
        raise FileExistsError(tmp)
    try:
        with sqlite3.connect(f'file:{source.resolve()}?mode=ro', uri=True, timeout=30) as original:
            with sqlite3.connect(tmp) as backup:
                original.backup(backup, pages=128, sleep=0.05)
        report = inspect_database(tmp)
        if not report['integrity_ok'] or report['foreign_key_violations'] or report['missing_tables']:
            raise RuntimeError('Backup validation failed: ' + json.dumps(report, ensure_ascii=False))
        os.replace(tmp, destination)
    finally:
        if tmp.exists(): tmp.unlink()
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {'path': str(destination), 'bytes': destination.stat().st_size, 'sha256': digest, **report}

def main(argv=None):
    parser = argparse.ArgumentParser(description='lovetech SQLite backup and preflight')
    parser.add_argument('command', choices=('check', 'backup'))
    parser.add_argument('--db', default=os.environ.get('CHEM_DB','/app/data/chem.db'))
    parser.add_argument('--output', help='Unique destination file for backup command')
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            report = inspect_database(args.db)
        else:
            if not args.output:
                parser.error('--output is required for backup')
            report = create_backup(args.db, args.output)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if not report['integrity_ok'] or report['foreign_key_violations'] or report['missing_tables']:
            return 2
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
