"""Read-only migration and route diagnostics for lovetech v52."""
import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

REQUIRED_TABLES = (
    'users','materials','products','recipes','recipe_items','steps','lots',
    'batches','consumptions','movements','audit','shipments',
    'work_orders','production_sheets','containers',
    'notification_reads_v46','operational_errors_v50','admin_alerts_v51',
)
SAFE_ROUTES = ('/health','/health/ready','/login','/','/recipes',
               '/production/execution','/notifications','/admin/monitoring','/admin/system')

def inspect_db(path, required=REQUIRED_TABLES):
    """Never create or modify the target database."""
    target=Path(path)
    if not target.is_file():
        return {'ok':False,'error':'database_missing','missing_tables':list(required)}
    uri='file:'+quote(str(target.resolve()),safe='/')+'?mode=ro'
    try:
        with sqlite3.connect(uri,uri=True,timeout=3) as c:
            c.execute('PRAGMA query_only=ON')
            integrity=[row[0] for row in c.execute('PRAGMA integrity_check').fetchall()]
            fk=[list(row) for row in c.execute('PRAGMA foreign_key_check').fetchmany(20)]
            tables={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing=sorted(set(required)-tables)
            return {'ok':integrity==['ok'] and not fk and not missing,
                    'integrity':integrity[:5], 'foreign_key_violations':fk,
                    'missing_tables':missing,'checked_tables':len(required)}
    except (sqlite3.Error,OSError,ValueError) as exc:
        return {'ok':False,'error':type(exc).__name__,'missing_tables':[]}

def inspect_routes(app, routes=SAFE_ROUTES):
    """Inspect Flask routing map without sending requests or changing data."""
    rules={rule.rule:rule.methods for rule in app.url_map.iter_rules()}
    missing=[path for path in routes if path not in rules or 'GET' not in rules[path]]
    return {'ok':not missing,'missing_routes':missing,'checked_routes':len(routes)}

def smoke_http(app, routes=SAFE_ROUTES):
    """GET-only smoke test, using isolated test-client session and no POST actions."""
    results={}
    with app.test_client() as client:
        # Public routes should work without a login.
        for path in ('/health','/health/ready','/login'):
            if path in routes:
                r=client.get(path,follow_redirects=False)
                results[path]=r.status_code
        # No credentials are used: create only an ephemeral test-client session
        # for an existing admin account, without writing to the database.
        from app import conn
        with conn() as c:
            user=c.execute("SELECT id,username,role FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if user:
            with client.session_transaction() as s:
                s['uid']=user['id'];s['username']=user['username'];s['role']=user['role']
            for path in routes:
                if path not in results:
                    results[path]=client.get(path,follow_redirects=False).status_code
        else:
            for path in routes:
                if path not in results:results[path]='skipped:no_admin'
    bad={p:code for p,code in results.items() if isinstance(code,int) and code>=500}
    return {'ok':not bad and results.get('/health/ready')==200,
            'results':results,'server_errors':bad}

def main(argv=None):
    parser=argparse.ArgumentParser(description='lovetech v52 safe deployment diagnostics')
    parser.add_argument('--db',required=True,help='Existing SQLite file')
    parser.add_argument('--routes',action='store_true',help='Check Flask route registration')
    parser.add_argument('--http',action='store_true',help='Run GET-only Flask smoke test (requires installed dependencies)')
    args=parser.parse_args(argv)
    result={'database':inspect_db(args.db)}
    if args.routes or args.http:
        import os
        os.environ['CHEM_DB']=str(Path(args.db).resolve())
        from app import app
        result['routes']=inspect_routes(app)
        if args.http:result['http']=smoke_http(app)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if all(item['ok'] for item in result.values()) else 1

if __name__=='__main__':raise SystemExit(main())
