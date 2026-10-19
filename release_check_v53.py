"""Read-only release gate for lovetech v53. Does not import Flask or run init()."""
import argparse
import ast
import json
import sqlite3
from datetime import datetime,timezone
from pathlib import Path
from diagnostics_v52 import inspect_db, REQUIRED_TABLES
from operations_v51 import backup_status

# Only exact, known route patterns; never synthesize URLs with real production IDs.
PROTECTED = {
 '/users': {'admin'},
 '/admin/system': {'admin'},
 '/admin/monitoring': {'admin'},
 '/production/execution': {'admin','technologist','operator','quality','warehouse','viewer'},
}
CRITICAL = ('/health','/health/ready','/login','/recipes','/work-orders',
            '/production','/production/execution','/inventory/v45',
            '/notifications','/economics/costs','/admin/system','/admin/monitoring')

def _literal_route(decorator):
 if not isinstance(decorator,ast.Call) or not decorator.args:return None
 f=decorator.func
 if not isinstance(f,ast.Attribute) or not isinstance(f.value,ast.Name) or f.value.id!='app' or f.attr not in ('get','route'):return None
 try:return ast.literal_eval(decorator.args[0])
 except (ValueError,TypeError):return None

def inspect_source(source):
 """Check route declarations and the security decorator on sensitive endpoints."""
 try:tree=ast.parse(Path(source).read_text(encoding='utf-8'))
 except (SyntaxError,OSError,UnicodeError) as e:return {'ok':False,'error':type(e).__name__}
 routes={}; duplicates=[]
 trees=[tree]
 for module in ('observability_v50.py','operations_v51.py'):
  try:trees.append(ast.parse(Path(source).with_name(module).read_text(encoding='utf-8')))
  except (OSError,SyntaxError,UnicodeError):pass
 for node in (n for t in trees for n in ast.walk(t) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))):
  declared=[_literal_route(d) for d in node.decorator_list]
  for route in (r for r in declared if r):
   if route in routes:duplicates.append(route)
   routes[route]=node
 missing=[r for r in CRITICAL if r not in routes]
 permissions={}
 for route,roles in PROTECTED.items():
  node=routes.get(route)
  if not node:
   permissions[route]='missing';continue
  decorators=[d for d in node.decorator_list if isinstance(d,ast.Call) and isinstance(d.func,ast.Name) and d.func.id=='allowed']
  if not decorators:
   permissions[route]='missing_allowed';continue
  try:actual={ast.literal_eval(a) for a in decorators[0].args}
  except (ValueError,TypeError):actual=set()
  permissions[route]='ok' if actual==roles else 'role_mismatch'
 return {'ok':not missing and all(v=='ok' for v in permissions.values()),
         'missing_routes':missing,'permissions':permissions,
         'duplicate_route_patterns':sorted(set(duplicates)),
         'checked_routes':len(CRITICAL)}

def inspect_production_data(path):
 """Non-mutating checks for basic production referential consistency."""
 p=Path(path)
 if not p.is_file():return {'ok':False,'error':'database_missing'}
 try:
  from urllib.parse import quote
  uri='file:'+quote(str(p.resolve()),safe='/')+'?mode=ro'
  with sqlite3.connect(uri,uri=True,timeout=3) as c:
   c.execute('PRAGMA query_only=ON')
   tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
   needed={'work_orders','production_sheets','batches','recipes','lots'}
   if not needed.issubset(tables):return {'ok':False,'missing_tables':sorted(needed-tables)}
   checks={
    'orphan_production_sheets':"SELECT COUNT(*) FROM production_sheets s LEFT JOIN work_orders w ON w.id=s.order_id WHERE w.id IS NULL",
    'orphan_batches_recipe':"SELECT COUNT(*) FROM batches b LEFT JOIN recipes r ON r.id=b.recipe_id WHERE r.id IS NULL",
    'orphan_batches_lot':"SELECT COUNT(*) FROM batches b LEFT JOIN lots l ON l.id=b.lot_id WHERE l.id IS NULL",
   }
   result={key:c.execute(sql).fetchone()[0] for key,sql in checks.items()}
   return {'ok':all(v==0 for v in result.values()),'counts':result}
 except (sqlite3.Error,OSError,ValueError) as exc:return {'ok':False,'error':type(exc).__name__}

def build_report(db,source,backups=None,max_backup_hours=36):
 checks={'database':inspect_db(db),
         'source_and_permissions':inspect_source(source),
         'production_data':inspect_production_data(db)}
 if backups is not None:
  b=backup_status(backups,max_age_hours=max_backup_hours)
  checks['backup']={'ok':b['state']=='ok',**b}
 passed=all(v.get('ok',False) for v in checks.values())
 return {'version':'v53','generated_at_utc':datetime.now(timezone.utc).isoformat(),
         'ready':passed,'checks':checks,
         'note':'Read-only pre-deployment checks. Not a substitute for role-based HTTP tests or restore drills.'}

def main(argv=None):
 p=argparse.ArgumentParser(description='lovetech v53 read-only release readiness report')
 p.add_argument('--db',required=True)
 p.add_argument('--source',default=str(Path(__file__).with_name('app.py')))
 p.add_argument('--backups',help='Directory containing lovetech-backup-status.json; omit to skip backup check')
 p.add_argument('--max-backup-hours',type=int,default=36)
 p.add_argument('--output',help='Optional JSON report file (must not be the SQLite database)')
 a=p.parse_args(argv)
 if a.max_backup_hours<=0:p.error('--max-backup-hours must be positive')
 report=build_report(a.db,a.source,a.backups,a.max_backup_hours)
 data=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
 if a.output:
  target=Path(a.output).resolve()
  if target==Path(a.db).resolve():p.error('Report cannot overwrite the database')
  target.write_text(data,encoding='utf-8')
 print(data,end='')
 return 0 if report['ready'] else 1

if __name__=='__main__':raise SystemExit(main())
