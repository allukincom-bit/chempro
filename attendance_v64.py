"""lovetech v64: advanced time sheet, restricted to the single v62 designated editor."""
from datetime import date,timedelta
from calendar import monthrange
from math import isfinite
from attendance_v62 import get_access,validate_month,STATUSES

SCHEDULES={'five_two':'5/2 (пн–пт)','two_two':'2/2 (от даты начала цикла)'}

def init_attendance_v64(c):
 c.executescript('''
 CREATE TABLE IF NOT EXISTS attendance_extra_v64(
 employee_id INTEGER NOT NULL REFERENCES users(id),work_date TEXT NOT NULL,
 night_hours REAL NOT NULL DEFAULT 0 CHECK(night_hours>=0 AND night_hours<=24),
 overtime_hours REAL NOT NULL DEFAULT 0 CHECK(overtime_hours>=0 AND overtime_hours<=24),
 PRIMARY KEY(employee_id,work_date));
 CREATE TABLE IF NOT EXISTS attendance_month_locks_v64(
 month TEXT PRIMARY KEY,locked_at TEXT NOT NULL,locked_by INTEGER NOT NULL REFERENCES users(id));
 CREATE TABLE IF NOT EXISTS attendance_schedules_v64(
 employee_id INTEGER PRIMARY KEY REFERENCES users(id),kind TEXT NOT NULL CHECK(kind IN ('five_two','two_two')),
 anchor_date TEXT NOT NULL,shift_hours REAL NOT NULL CHECK(shift_hours>0 AND shift_hours<=24),
 updated_at TEXT NOT NULL,updated_by INTEGER NOT NULL REFERENCES users(id));
 ''')

def parse_hours(v,maximum=24):
 try:n=float(v)
 except (TypeError,ValueError):raise ValueError('Неверное количество часов')
 if not isfinite(n) or n<0 or n>maximum:raise ValueError('Часы вне диапазона')
 return n

def is_work_day(day,kind,anchor):
 if kind=='five_two':return day.weekday()<5
 if kind=='two_two':return ((day-date.fromisoformat(anchor)).days%4)<2
 raise ValueError('Неизвестный график')

def plan_month(month,kind,anchor,hours):
 validate_month(month)
 if kind not in SCHEDULES:raise ValueError('Неизвестный график')
 date.fromisoformat(anchor)
 hours=parse_hours(hours)
 if hours==0:raise ValueError('Нулевая смена')
 y,m=map(int,month.split('-'))
 return [(date(y,m,d).isoformat(),'work' if is_work_day(date(y,m,d),kind,anchor) else 'day_off',hours if is_work_day(date(y,m,d),kind,anchor) else 0) for d in range(1,monthrange(y,m)[1]+1)]

def write_entry(c,uid,employee_id,day,status,hours,note,now,audit):
 old=c.execute('SELECT status,hours,note FROM attendance_entries_v62 WHERE employee_id=? AND work_date=?',(employee_id,day)).fetchone()
 if old is not None and (old['status'],old['hours'],old['note'])==(status,hours,note):return False
 c.execute('''INSERT INTO attendance_entries_v62(employee_id,work_date,status,hours,note,updated_at,updated_by)
 VALUES(?,?,?,?,?,?,?) ON CONFLICT(employee_id,work_date) DO UPDATE SET status=excluded.status,hours=excluded.hours,note=excluded.note,updated_at=excluded.updated_at,updated_by=excluded.updated_by''',(employee_id,day,status,hours,note,now(),uid))
 c.execute('''INSERT INTO attendance_history_v62(employee_id,work_date,old_status,old_hours,old_note,new_status,new_hours,new_note,changed_at,changed_by)
 VALUES(?,?,?,?,?,?,?,?,?,?)''',(employee_id,day,old['status'] if old else None,old['hours'] if old else None,old['note'] if old else None,status,hours,note,now(),uid))
 audit(c,'attendance.v64.bulk','employee_id=%s date=%s'%(employee_id,day))
 return True

def register_attendance_v64(app,conn,page,audit,now):
 from flask import request,session,redirect,abort,Response
 from markupsafe import escape
 @app.before_request
 def protect_locked_v62():
  # v62 edit endpoint remains available but cannot bypass month lock.
  if request.path!='/attendance' or request.method!='POST' or request.form.get('action')!='save':return
  month=request.form.get('work_date','')[:7]
  if not month:return
  with conn() as c:
   if c.execute('SELECT 1 FROM attendance_month_locks_v64 WHERE month=?',(month,)).fetchone():abort(409)

 @app.route('/attendance/advanced',methods=['GET','POST'])
 def attendance_advanced_v64():
  uid=session.get('uid')
  if not uid:return redirect('/login')
  month=request.values.get('month',date.today().strftime('%Y-%m'))
  try:validate_month(month)
  except ValueError:abort(400)
  with conn() as c:
   admin,editor=get_access(c,uid)
  if not (admin or editor):abort(403)
  if request.method=='POST':
   action=request.form.get('action','')
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    admin,editor=get_access(c,uid)
    if action in ('bulk','schedule','extra','lock','unlock') and not editor:abort(403)
    locked=bool(c.execute('SELECT 1 FROM attendance_month_locks_v64 WHERE month=?',(month,)).fetchone())
    if locked and action not in ('unlock',):abort(409)
    if action=='lock':
     c.execute('INSERT INTO attendance_month_locks_v64 VALUES(?,?,?)',(month,now(),uid))
     audit(c,'attendance.v64.lock','month='+month)
    elif action=='unlock':
     c.execute('DELETE FROM attendance_month_locks_v64 WHERE month=?',(month,))
     audit(c,'attendance.v64.unlock','month='+month)
    elif action in ('bulk','schedule','extra'):
     try:employee_id=int(request.form.get('employee_id',''))
     except ValueError:abort(400)
     if not c.execute('SELECT 1 FROM users WHERE id=?',(employee_id,)).fetchone():abort(400)
     if action=='schedule':
      kind=request.form.get('kind','');anchor=request.form.get('anchor','')
      try:
       hours=parse_hours(request.form.get('shift_hours'))
       if hours<=0 or kind not in SCHEDULES:raise ValueError()
       date.fromisoformat(anchor)
      except (ValueError,TypeError):abort(400)
      c.execute('''INSERT INTO attendance_schedules_v64 VALUES(?,?,?,?,?,?) ON CONFLICT(employee_id) DO UPDATE SET kind=excluded.kind,anchor_date=excluded.anchor_date,shift_hours=excluded.shift_hours,updated_at=excluded.updated_at,updated_by=excluded.updated_by''',(employee_id,kind,anchor,hours,now(),uid))
      audit(c,'attendance.v64.schedule','employee_id='+str(employee_id))
     elif action=='bulk':
      sch=c.execute('SELECT * FROM attendance_schedules_v64 WHERE employee_id=?',(employee_id,)).fetchone()
      if not sch:abort(400)
      try:plan=plan_month(month,sch['kind'],sch['anchor_date'],sch['shift_hours'])
      except ValueError:abort(400)
      # Never overwrite manually entered days, holidays, absences or previous bulk entries.
      for day,status,hours in plan:
       if not c.execute('SELECT 1 FROM attendance_entries_v62 WHERE employee_id=? AND work_date=?',(employee_id,day)).fetchone():
        write_entry(c,uid,employee_id,day,status,hours,'Автозаполнение по графику',now,audit)
     else:
      day=request.form.get('work_date','')
      try:
       date.fromisoformat(day)
       if not day.startswith(month+'-'):raise ValueError()
       night=parse_hours(request.form.get('night_hours'))
       overtime=parse_hours(request.form.get('overtime_hours'))
      except (ValueError,TypeError):abort(400)
      entry=c.execute('SELECT status,hours FROM attendance_entries_v62 WHERE employee_id=? AND work_date=?',(employee_id,day)).fetchone()
      if not entry or entry['status']!='work' or night>entry['hours']:abort(400)
      c.execute('''INSERT INTO attendance_extra_v64 VALUES(?,?,?,?) ON CONFLICT(employee_id,work_date) DO UPDATE SET night_hours=excluded.night_hours,overtime_hours=excluded.overtime_hours''',(employee_id,day,night,overtime))
      audit(c,'attendance.v64.extra','employee_id=%s date=%s night=%s overtime=%s'%(employee_id,day,night,overtime))
    else:abort(400)
   return redirect('/attendance/advanced?month='+month)
  with conn() as c:
   employees=[dict(r) for r in c.execute('SELECT id,username FROM users ORDER BY username')]
   locked=bool(c.execute('SELECT 1 FROM attendance_month_locks_v64 WHERE month=?',(month,)).fetchone())
   summary=[dict(r) for r in c.execute('''SELECT u.username,u.id,COUNT(a.id) days,
    COALESCE(SUM(CASE WHEN a.status='work' THEN a.hours ELSE 0 END),0) hours,
    COALESCE(SUM(e.night_hours),0) night,COALESCE(SUM(e.overtime_hours),0) overtime
    FROM users u LEFT JOIN attendance_entries_v62 a ON a.employee_id=u.id AND a.work_date>=? AND a.work_date<?
    LEFT JOIN attendance_extra_v64 e ON e.employee_id=a.employee_id AND e.work_date=a.work_date
    GROUP BY u.id ORDER BY u.username''',(month+'-01',next_month(month)))]
   schedules={r['employee_id']:dict(r) for r in c.execute('SELECT * FROM attendance_schedules_v64')}
  if request.args.get('print')=='1':
   from flask import render_template_string
   html='''<!doctype html><html lang="ru"><meta charset="utf-8"><title>lovetech — Табель {{month}}</title>
   <style>@page{size:A4 portrait;margin:12mm}body{font:12px Arial;color:#222}h1{font-size:18px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #aaa;padding:7px;text-align:left}th{background:#eee}.controls{margin-bottom:15px}@media print{.controls{display:none}} </style>
   <div class="controls"><button onclick="window.print()">Печать / PDF</button></div><h1>lovetech · Табель за {{month}}</h1><p>Статус: {{'Закрыт' if locked else 'Открыт'}}</p>
   <table><thead><tr><th>Сотрудник</th><th>Дней</th><th>Часов</th><th>Ночных</th><th>Сверхурочных</th></tr></thead><tbody>
   {%for r in summary%}<tr><td>{{r.username}}</td><td>{{r.days}}</td><td>{{'%.2f'|format(r.hours)}}</td><td>{{'%.2f'|format(r.night)}}</td><td>{{'%.2f'|format(r.overtime)}}</td></tr>{%endfor%}</tbody></table><p>Ответственный: ____________________ Подпись: __________ Дата: __________</p></html>'''
   resp=Response(render_template_string(html,month=month,locked=locked,summary=summary),mimetype='text/html')
   resp.headers['Cache-Control']='private, no-store'
   return resp
  return page('Расширенный табель', '''
  <div class="card"><h2>Расширенный табель · {{month}}</h2><p>Статус: <b>{{'Закрыт' if locked else 'Открыт'}}</b>. Изменять данные может только назначенный табельщик.</p>
  <form method="get"><input type="month" name="month" value="{{month}}"><button>Открыть</button></form>
  <p><a href="/attendance?month={{month}}">Обычный табель</a> · <a href="/attendance/advanced?month={{month}}&print=1" target="_blank">Печать A4 / PDF</a></p></div>
  {%if editor%}<div class="card"><h3>{{'Открыть месяц' if locked else 'Закрыть месяц'}}</h3><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token}}"><input type="hidden" name="month" value="{{month}}"><input type="hidden" name="action" value="{{'unlock' if locked else 'lock'}}"><button>{{'Открыть для исправлений' if locked else 'Закрыть месяц'}}</button></form></div>{%endif%}
  {%if editor and not locked%}
  <div class="card"><h3>График сотрудника</h3><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token}}"><input type="hidden" name="month" value="{{month}}"><input type="hidden" name="action" value="schedule">
  <select name="employee_id">{%for e in employees%}<option value="{{e.id}}">{{e.username}}</option>{%endfor%}</select><select name="kind"><option value="five_two">5/2</option><option value="two_two">2/2</option></select>
  <label>Начало цикла <input type="date" name="anchor" value="{{month}}-01" required></label><label>Часов за смену <input type="number" name="shift_hours" min="0.25" max="24" step="0.25" value="8" required></label><button>Сохранить график</button></form></div>
  <div class="card"><h3>Заполнить месяц по графику</h3><p>Только пустые даты. Существующие записи, отпуска и больничные не перезаписываются. Праздники автоматически не определяются.</p>
  <form method="post"><input type="hidden" name="_csrf" value="{{csrf_token}}"><input type="hidden" name="month" value="{{month}}"><input type="hidden" name="action" value="bulk"><select name="employee_id">{%for e in employees%}<option value="{{e.id}}">{{e.username}}</option>{%endfor%}</select><button>Заполнить</button></form></div>
  <div class="card"><h3>Ночные и сверхурочные часы</h3><p>Ночные часы входят в обычные часы, сверхурочные учитываются отдельно. Расчёт оплаты не производится.</p>
  <form method="post"><input type="hidden" name="_csrf" value="{{csrf_token}}"><input type="hidden" name="month" value="{{month}}"><input type="hidden" name="action" value="extra">
  <select name="employee_id">{%for e in employees%}<option value="{{e.id}}">{{e.username}}</option>{%endfor%}</select><input type="date" name="work_date" value="{{month}}-01" required><label>Ночные <input type="number" name="night_hours" min="0" max="24" step="0.25" value="0" required></label><label>Сверхурочные <input type="number" name="overtime_hours" min="0" max="24" step="0.25" value="0" required></label><button>Сохранить</button></form></div>
  {%endif%}
  <div class="card"><h3>Сводка</h3><table><tr><th>Сотрудник</th><th>Дней</th><th>Часов</th><th>Ночных</th><th>Сверхурочных</th><th>График</th></tr>
  {%for r in summary%}<tr><td>{{r.username}}</td><td>{{r.days}}</td><td>{{'%.2f'|format(r.hours)}}</td><td>{{'%.2f'|format(r.night)}}</td><td>{{'%.2f'|format(r.overtime)}}</td><td>{{schedules[r.id].kind if r.id in schedules else '—'}}</td></tr>{%endfor%}</table></div>
  ''',month=month,locked=locked,editor=editor,employees=employees,summary=summary,schedules=schedules)

def next_month(month):
 y,m=map(int,month.split('-'))
 return f'{y+1}-01-01' if m==12 else f'{y}-{m+1:02d}-01'
