"""Read-only preflight and isolated restore verification before Timeweb deployments."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from db_maintenance import inspect_database

CRITICAL_ROUTES = ('/health/ready', '/admin/monitoring', '/quality/shipping-control',
                   '/stock/planning', '/attendance/advanced', '/production/release-readiness')

def check_backup(path):
    """Restore a SQLite backup to a temporary DB and verify its content."""
    backup = Path(path)
    if not backup.is_file() or backup.is_symlink():
        raise FileNotFoundError('Backup missing or symlink: ' + str(backup))
    with tempfile.TemporaryDirectory(prefix='lovetech-v67-drill-') as tmp:
        restored = Path(tmp) / 'restored.sqlite'
        with sqlite3.connect(f'file:{backup.resolve()}?mode=ro', uri=True) as source:
            with sqlite3.connect(restored) as dest:
                source.backup(dest)
        result = inspect_database(restored)
    digest = hashlib.file_digest(backup.open('rb'), 'sha256').hexdigest()
    return {'backup_file': str(backup), 'backup_bytes': backup.stat().st_size,
            'sha256': digest, 'restore': result,
            'ok': result['integrity_ok'] and not result['foreign_key_violations'] and not result['missing_tables']}

def check_app_routes():
    """Import only with explicit opt-in; do not invoke init() or use production HTTP."""
    from app import app
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    missing = sorted(set(CRITICAL_ROUTES) - routes)
    return {'ok': not missing, 'missing': missing, 'total': len(routes)}

def preflight(db_path, backup_path=None, check_routes=False, max_backup_hours=36, now=None):
    report = {'version': 67, 'checked_at_utc': (now or dt.datetime.now(dt.timezone.utc)).isoformat(),
              'checks': {}, 'errors': []}
    try:
        db = inspect_database(db_path)
        report['checks']['database'] = db
        if not db['integrity_ok'] or db['foreign_key_violations'] or db['missing_tables']:
            report['errors'].append('database_invalid')
    except (OSError, sqlite3.Error, ValueError) as exc:
        report['checks']['database'] = {'ok': False, 'error': str(exc)}
        report['errors'].append('database_unavailable')
    if backup_path is not None:
        try:
            backup = check_backup(backup_path)
            age = ((now or dt.datetime.now(dt.timezone.utc)).timestamp() - Path(backup_path).stat().st_mtime) / 3600
            backup['age_hours'] = round(age, 2)
            if not backup['ok']:
                report['errors'].append('backup_restore_failed')
            if age > max_backup_hours or age < -0.1:
                report['errors'].append('backup_stale_or_future')
            report['checks']['backup'] = backup
        except (OSError, sqlite3.Error, ValueError) as exc:
            report['checks']['backup'] = {'ok': False, 'error': str(exc)}
            report['errors'].append('backup_unavailable')
    else:
        report['errors'].append('backup_not_specified')
    if check_routes:
        try:
            route_report = check_app_routes()
            report['checks']['routes'] = route_report
            if not route_report['ok']:
                report['errors'].append('routes_missing')
        except (ImportError, RuntimeError, OSError) as exc:
            report['checks']['routes'] = {'ok': False, 'error': str(exc)}
            report['errors'].append('routes_unavailable')
    report['ready'] = not report['errors']
    return report

def main(argv=None):
    p = argparse.ArgumentParser(description='lovetech v67 deployment gate (read-only)')
    p.add_argument('--db', default=os.environ.get('CHEM_DB','/app/data/chem.db'))
    p.add_argument('--backup', required=True, help='Existing, verified SQLite backup file')
    p.add_argument('--max-backup-hours', type=float, default=36)
    p.add_argument('--routes', action='store_true', help='Import Flask app; needs installed dependencies')
    p.add_argument('--output', help='Optional JSON report path')
    args = p.parse_args(argv)
    if args.max_backup_hours <= 0:
        p.error('--max-backup-hours must be positive')
    report = preflight(args.db, args.backup, args.routes, args.max_backup_hours)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent, delete=False) as f:
            f.write(payload + '\n')
            temp = f.name
        os.replace(temp, target)
    print(payload)
    return 0 if report['ready'] else 2

if __name__ == '__main__':
    raise SystemExit(main())
