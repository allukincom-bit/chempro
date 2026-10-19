"""Scheduled backup job: one-shot invocation by cron/Timeweb scheduler."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from db_maintenance import create_backup, inspect_database


def restore_drill(backup_path):
    """Restore into a disposable isolated database, never into the live database."""
    with tempfile.TemporaryDirectory(prefix='lovetech-restore-') as directory:
        restored = Path(directory) / 'restored.db'
        with sqlite3.connect(f'file:{Path(backup_path).resolve()}?mode=ro', uri=True) as source:
            with sqlite3.connect(restored) as target:
                source.backup(target)
        result = inspect_database(restored)
        if not result['integrity_ok'] or result['foreign_key_violations'] or result['missing_tables']:
            raise RuntimeError('Restore drill validation failed')
        return result


def rotate(directory, keep, protected):
    """Remove only older files created by this tool, never arbitrary .db files."""
    files = sorted(directory.glob('lovetech-auto-*.db'), key=lambda p:p.name, reverse=True)
    deleted = []
    for candidate in files[keep:]:
        if candidate == protected or candidate.is_symlink():
            continue
        candidate.unlink()
        deleted.append(candidate.name)
    return deleted


def run(db, directory, keep=14, clock=None):
    if keep < 1: raise ValueError('keep must be >= 1')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink(): raise ValueError('Backup directory must not be a symlink')
    lock_path = directory / '.lovetech-backup.lock'
    with lock_path.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another backup job is running') from exc
        stamp = (clock or dt.datetime.now(dt.timezone.utc)).strftime('%Y%m%dT%H%M%S%fZ')
        destination = directory / f'lovetech-auto-{stamp}.db'
        report = create_backup(db, destination)
        try:
            restored = restore_drill(destination)
        except Exception:
            # Keep failed backup for investigation; do not rotate on failed drill.
            raise
        removed = rotate(directory, keep, destination)
        result = {'status':'ok', 'backup':report, 'restore_drill':restored,
                  'removed_old_backups':removed, 'created_at_utc':stamp}
        return result


def publish_status(directory, result):
    """Atomic status for the admin dashboard; never expose backup file contents."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    status = directory / 'lovetech-backup-status.json'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                     prefix='.backup-status-', suffix='.tmp', delete=False) as tmp:
        temp_name = tmp.name
        try:
            json.dump(result, tmp, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
    os.replace(temp_name, status)


def run_reported(db, directory, keep=14):
    try:
        result = run(db, directory, keep)
    except Exception as exc:
        failure = {'status':'error','checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                   'error_type':type(exc).__name__}
        publish_status(directory, failure)
        raise
    publish_status(directory, {'status':'ok',
        'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
        'restore_verified':True,
        'backup_name':Path(result['backup']['path']).name if 'destination' in result['backup'] else '',
        'removed_count':len(result['removed_old_backups'])})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='lovetech backup with restore drill and retention')
    parser.add_argument('--db', default=os.getenv('CHEM_DB','/app/data/chem.db'))
    parser.add_argument('--directory', default=os.getenv('LOVETECH_BACKUP_DIR','/app/data/backups'))
    parser.add_argument('--keep', type=int, default=14)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run_reported(args.db,args.directory,args.keep),ensure_ascii=False,indent=2))
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
        print(json.dumps({'status':'error','message':str(error)},ensure_ascii=False),file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
