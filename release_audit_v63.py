"""Offline release audit: static route map + read-only SQLite integrity checks.
No Flask import, no application initialization, no production DB mutations.
"""
import argparse,ast,json,sqlite3,sys
from pathlib import Path

CRITICAL={'/attendance','/stock/thresholds','/admin/system','/admin/monitoring','/production/execution','/production/release-readiness','/health/ready'}

def inspect_routes(root):
 routes=[]
 for path in sorted(root.glob('*.py')):
  if path.name.startswith('test_'):continue
  try:tree=ast.parse(path.read_text(encoding='utf-8'),filename=str(path))
  except (SyntaxError,UnicodeError) as exc:
   return {'ok':False,'error':f'{path.name}: {exc}','routes':[]}
  for node in ast.walk(tree):
   if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):continue
   for decorator in node.decorator_list:
    if not isinstance(decorator,ast.Call) or not isinstance(decorator.func,ast.Attribute):continue
    if decorator.func.attr not in ('get','post','route','put','delete'):continue
    if not decorator.args or not isinstance(decorator.args[0],ast.Constant) or not isinstance(decorator.args[0].value,str):continue
    route=decorator.args[0].value
    # decorators on nested register functions use app too
    base=decorator.func.value
    if not isinstance(base,ast.Name) or base.id!='app':continue
    guards=[]
    for dec in node.decorator_list:
     if isinstance(dec,ast.Name):guards.append(dec.id)
     elif isinstance(dec,ast.Call) and isinstance(dec.func,ast.Name):guards.append(dec.func.id)
    routes.append({'path':route,'function':node.name,'file':path.name,'guards':guards})
 paths={r['path'] for r in routes}
 missing=sorted(CRITICAL-paths)
 return {'ok':not missing,'count':len(routes),'missing_critical':missing,'routes':routes}

def inspect_database(path):
 if not path.is_file():return {'ok':False,'error':'database_missing'}
 try:
  uri=path.resolve().as_uri()+'?mode=ro'
  c=sqlite3.connect(uri,uri=True,timeout=3)
  try:
   integrity=[r[0] for r in c.execute('PRAGMA integrity_check').fetchall()]
   foreign_keys=[list(r) for r in c.execute('PRAGMA foreign_key_check').fetchall()]
   tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
   required={'users','lots','recipes','work_orders','attendance_entries_v62','attendance_settings_v62'}
   missing=sorted(required-tables)
   return {'ok':integrity==['ok'] and not foreign_keys and not missing,
           'integrity':integrity,'foreign_key_violations':foreign_keys[:20],
           'foreign_key_violation_count':len(foreign_keys),'missing_tables':missing}
  finally:c.close()
 except (sqlite3.Error,OSError) as exc:return {'ok':False,'error':str(exc)}

def main(argv=None):
 parser=argparse.ArgumentParser(description='lovetech v63 read-only release audit')
 parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent)
 parser.add_argument('--db',type=Path,help='Optional existing SQLite file, opened read-only')
 parser.add_argument('--output',type=Path,help='Write JSON report')
 args=parser.parse_args(argv)
 routes=inspect_routes(args.source)
 report={'version':'v63','route_audit':routes,'database':inspect_database(args.db) if args.db else {'skipped':'no --db'},'ok':routes['ok']}
 if args.db:report['ok']=report['ok'] and report['database']['ok']
 data=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
 if args.output:args.output.write_text(data,encoding='utf-8')
 print(json.dumps({'ok':report['ok'],'route_count':routes.get('count'),'missing_critical':routes.get('missing_critical'), 'database':report['database']},ensure_ascii=False,indent=2))
 return 0 if report['ok'] else 1
if __name__=='__main__':sys.exit(main())
