"""Isolated Flask role and critical-route smoke tests; NEVER uses production CHEM_DB."""
import argparse, json, os, sqlite3, tempfile
from pathlib import Path

ROLE_ROUTES = {
    '/admin/system': {'admin'},
    '/admin/monitoring': {'admin'},
    '/economics/costs': {'admin','economist','technologist'},
    '/production/execution': {'admin','technologist','operator','quality','warehouse','viewer'},
    '/recipes': {'admin','technologist','warehouse','economist','operator','quality','viewer'},
}
SMOKE_ROUTES = ('/health','/health/ready','/login','/','/recipes','/work-orders',
                '/production/sheets','/production/execution','/notifications',
                '/admin/monitoring','/admin/system')
ROLES=('admin','technologist','warehouse','economist','operator','quality','viewer')

def isolated_app(db_path):
    # Import app only AFTER selecting a disposable database. app.DB is set at import time.
    os.environ['CHEM_DB']=str(db_path)
    from app import app,init
    app.config.update(TESTING=True,PROPAGATE_EXCEPTIONS=False)
    init()
    return app

def create_users(db_path):
    from werkzeug.security import generate_password_hash
    with sqlite3.connect(db_path) as c:
        for role in ROLES:
            username='v54_'+role
            c.execute('INSERT INTO users(username,password,role) VALUES(?,?,?)',
                      (username,generate_password_hash('unused-isolated-v54'),role))
            uid=c.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone()[0]
            c.execute('INSERT OR IGNORE INTO user_roles_v33(user_id,role) VALUES(?,?)',(uid,role))
    with sqlite3.connect(db_path) as c:
        return {role:c.execute('SELECT id FROM users WHERE username=?',('v54_'+role,)).fetchone()[0] for role in ROLES}

def run_checks():
    results=[]
    with tempfile.TemporaryDirectory(prefix='lovetech-v54-') as folder:
        db=Path(folder)/'isolated.db'
        app=isolated_app(db)
        users=create_users(db)
        client=app.test_client()
        def record(name,ok,details=None):
            results.append({'name':name,'ok':bool(ok),'details':details})
        def get(path):
            try:return client.get(path,follow_redirects=False).status_code
            except Exception as e:return 'exception:'+type(e).__name__
        for path in SMOKE_ROUTES:
            with client.session_transaction() as s:s.clear()
            code=get(path)
            expected=200 if path in ('/health','/health/ready','/login') else 302
            record('anonymous GET '+path,code==expected,code)
        for role,uid in users.items():
            with client.session_transaction() as s:
                s.clear();s.update(uid=uid,username='v54_'+role,role=role)
            for path,permitted in ROLE_ROUTES.items():
                code=get(path)
                # An allowed GET must render; forbidden must return 403.
                expected=200 if role in permitted else 403
                record(role+' GET '+path,code==expected,code)
        # Unauthorized mutation must be rejected before reaching a handler.
        for role in ('operator','viewer'):
            with client.session_transaction() as s:
                s.clear();s.update(uid=users[role],username='v54_'+role,role=role)
            r=client.post('/work-orders',data={'action':'create'},follow_redirects=False)
            record(role+' POST without CSRF',r.status_code==400,r.status_code)
        with client.session_transaction() as s:s.clear()
        r=client.post('/work-orders',data={'action':'create'},follow_redirects=False)
        record('anonymous POST without CSRF',r.status_code==400,r.status_code)
        # This test only GETs authenticated pages and sends invalid-CSRF POSTs.
        # No production data is ever read or modified.
    return {'ok':all(x['ok'] for x in results),'passed':sum(x['ok'] for x in results),
            'total':len(results),'checks':results}

def main(argv=None):
    parser=argparse.ArgumentParser(description='lovetech v54 isolated HTTP role checks')
    parser.add_argument('--output',help='Optional JSON report file')
    args=parser.parse_args(argv)
    try:report=run_checks()
    except ImportError as e:
        report={'ok':False,'error':'dependencies_missing','detail':str(e)}
    except Exception as e:
        report={'ok':False,'error':type(e).__name__,'detail':str(e)}
    payload=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:Path(args.output).write_text(payload+'\n',encoding='utf-8')
    print(payload)
    return 0 if report['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
