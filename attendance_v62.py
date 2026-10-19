"""Employee attendance: one designated editor, admin-only assignment, audit history."""
import csv
import io
import re
from datetime import date, datetime
from calendar import monthrange

STATUSES={'work':'Работа','day_off':'Выходной','vacation':'Отпуск','sick':'Больничный','absence':'Отсутствие'}
MONTH_RE=re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')

def init_attendance(c):
 c.executescript("""
 CREATE TABLE IF NOT EXISTS attendance_settings_v62(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),editor_id INTEGER REFERENCES users(id),
  changed_at TEXT NOT NULL,changed_by INTEGER REFERENCES users(id));
 CREATE TABLE IF NOT EXISTS attendance_entries_v62(
  id INTEGER PRIMARY KEY,employee_id INTEGER NOT NULL REFERENCES users(id),
  work_date TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('work','day_off','vacation','sick','absence')),
  hours REAL NOT NULL CHECK(hours>=0 AND hours<=24),note TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,updated_by INTEGER NOT NULL REFERENCES users(id),
  UNIQUE(employee_id,work_date));
 CREATE TABLE IF NOT EXISTS attendance_history_v62(
  id INTEGER PRIMARY KEY,employee_id INTEGER NOT NULL REFERENCES users(id),work_date TEXT NOT NULL,
  old_status TEXT,old_hours REAL,old_note TEXT,new_status TEXT NOT NULL,
  new_hours REAL NOT NULL,new_note TEXT NOT NULL,changed_at TEXT NOT NULL,
  changed_by INTEGER NOT NULL REFERENCES users(id));
 CREATE INDEX IF NOT EXISTS idx_attendance_month_v62 ON attendance_entries_v62(work_date,employee_id);
 """)

def validate_month(value):
 if not MONTH_RE.fullmatch(value or ''):raise ValueError('Неверный месяц')
 return value

def validate_entry(work_date,status,hours,note):
 try:date.fromisoformat(work_date)
 except (ValueError,TypeError):raise ValueError('Неверная дата')
 if status not in STATUSES:raise ValueError('Неверный статус')
 try:amount=float(hours)
 except (TypeError,ValueError):raise ValueError('Неверное количество часов')
 import math
 if not math.isfinite(amount) or amount<0 or amount>24:raise ValueError('Часы должны быть от 0 до 24')
 if status!='work' and amount!=0:raise ValueError('Для нерабочего дня часы должны быть равны нулю')
 if len(note)>500:raise ValueError('Примечание слишком длинное')
 return amount

def get_access(c,uid):
 user=c.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone()
 if not user:return False,False
 admin=bool(c.execute("SELECT 1 FROM user_roles_v33 WHERE user_id=? AND role='admin'",(uid,)).fetchone())
 editor=c.execute('SELECT editor_id FROM attendance_settings_v62 WHERE singleton=1').fetchone()
 return admin,bool(editor and editor['editor_id']==uid)

def register_attendance(app,conn,page,audit,now):
 from flask import request, session, redirect, abort, Response
 @app.route('/attendance',methods=['GET','POST'])
 def attendance_v62():
  uid=session.get('uid')
  if not uid:return redirect('/login')
  with conn() as c:
   is_admin,is_editor=get_access(c,uid)
  if not (is_admin or is_editor):abort(403)
  month=request.values.get('month',date.today().strftime('%Y-%m'))
  try:validate_month(month)
  except ValueError:abort(400)
  if request.method=='POST':
   action=request.form.get('action')
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    is_admin,is_editor=get_access(c,uid)
    if action=='assign':
     if not is_admin:abort(403)
     try:editor_id=int(request.form.get('editor_id',''))
     except ValueError:abort(400)
     if not c.execute('SELECT 1 FROM users WHERE id=?',(editor_id,)).fetchone():abort(400)
     c.execute("""INSERT INTO attendance_settings_v62(singleton,editor_id,changed_at,changed_by)
      VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET editor_id=excluded.editor_id,
      changed_at=excluded.changed_at,changed_by=excluded.changed_by""",(editor_id,now(),uid))
     audit(c,'attendance.assign','editor_id='+str(editor_id))
    elif action=='save':
     if not is_editor:abort(403)
     try:employee_id=int(request.form.get('employee_id',''))
     except ValueError:abort(400)
     if not c.execute('SELECT 1 FROM users WHERE id=?',(employee_id,)).fetchone():abort(400)
     day=request.form.get('work_date','')
     status=request.form.get('status','')
     note=request.form.get('note','').strip()
     try:
      amount=validate_entry(day,status,request.form.get('hours'),note)
      if not day.startswith(month+'-'):raise ValueError('Дата вне выбранного месяца')
     except ValueError:abort(400)
     old=c.execute('SELECT status,hours,note FROM attendance_entries_v62 WHERE employee_id=? AND work_date=?',(employee_id,day)).fetchone()
     if old is None or (old['status'],old['hours'],old['note'])!=(status,amount,note):
      c.execute("""INSERT INTO attendance_entries_v62(employee_id,work_date,status,hours,note,updated_at,updated_by)
       VALUES(?,?,?,?,?,?,?) ON CONFLICT(employee_id,work_date) DO UPDATE SET
       status=excluded.status,hours=excluded.hours,note=excluded.note,updated_at=excluded.updated_at,
       updated_by=excluded.updated_by""",(employee_id,day,status,amount,note,now(),uid))
      c.execute("""INSERT INTO attendance_history_v62(employee_id,work_date,old_status,old_hours,old_note,
       new_status,new_hours,new_note,changed_at,changed_by) VALUES(?,?,?,?,?,?,?,?,?,?)""",
       (employee_id,day,old['status'] if old else None,old['hours'] if old else None,
        old['note'] if old else None,status,amount,note,now(),uid))
      audit(c,'attendance.save','employee_id=%s date=%s'%(employee_id,day))
    else:abort(400)
   return redirect('/attendance?month='+month)
  with conn() as c:
   employees=[dict(x) for x in c.execute('SELECT id,username FROM users ORDER BY username')]
   setting=c.execute('SELECT editor_id FROM attendance_settings_v62 WHERE singleton=1').fetchone()
   editor_id=setting['editor_id'] if setting else None
   rows=[dict(x) for x in c.execute("""SELECT a.*,u.username FROM attendance_entries_v62 a
    JOIN users u ON u.id=a.employee_id WHERE a.work_date>=? AND a.work_date<?
    ORDER BY a.work_date,u.username""",(month+'-01',_next_month(month)))]
   summary=[dict(x) for x in c.execute("""SELECT u.id,u.username,
    COUNT(a.id) AS days_recorded,COALESCE(SUM(CASE WHEN a.status='work' THEN a.hours ELSE 0 END),0) AS hours,
    COALESCE(SUM(CASE WHEN a.status='work' THEN 1 ELSE 0 END),0) AS work_days
    FROM users u LEFT JOIN attendance_entries_v62 a ON a.employee_id=u.id
    AND a.work_date>=? AND a.work_date<? GROUP BY u.id ORDER BY u.username""",
    (month+'-01',_next_month(month)))]
  if request.args.get('format')=='csv':
   out=io.StringIO();writer=csv.writer(out)
   writer.writerow(['Дата','Сотрудник','Статус','Часы','Примечание','Внёс','Изменено'])
   names={e['id']:e['username'] for e in employees}
   for r in rows:
    # Guard against CSV formula injection in user-controlled fields.
    def safe(v):
     v=str(v or '')
     return "'"+v if v.lstrip().startswith(('=','+','-','@')) else v
    writer.writerow([r['work_date'],safe(r['username']),STATUSES[r['status']],r['hours'],safe(r['note']),safe(names.get(r['updated_by'],'')),r['updated_at']])
   return Response('\ufeff'+out.getvalue(),mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="lovetech-attendance-'+month+'.csv"','Cache-Control':'private, no-store'})
  return page('Учёт рабочего времени',"""
  <div class='card'><h2>Табель рабочего времени · {{month}}</h2>
  <p class='muted'>Доступ только назначенному табельщику и администратору. Вносить и исправлять записи может только назначенный сотрудник. Все изменения сохраняются в истории.</p>
  <form method='get'><label>Месяц<input type='month' name='month' value='{{month}}' required></label><button>Показать</button>
  <a class='btn' href='/attendance?month={{month}}&format=csv'>Выгрузить CSV</a></form>
  <p><b>Ответственный за табель:</b> {{names.get(editor_id,'Не назначен')}}</p></div>
  {% if is_admin %}<div class='card'><h2>Назначение ответственного</h2><form method='post'>
  <input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='month' value='{{month}}'>
  <input type='hidden' name='action' value='assign'><label>Единственный сотрудник с правом заполнения
  <select name='editor_id' required>{% for e in employees %}<option value='{{e.id}}' {% if e.id==editor_id %}selected{% endif %}>{{e.username}}</option>{% endfor %}</select></label>
  <button>Назначить</button></form></div>{% endif %}
  {% if is_editor %}<div class='card'><h2>Заполнить день</h2><form method='post'>
  <input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='month' value='{{month}}'><input type='hidden' name='action' value='save'>
  <label>Сотрудник<select name='employee_id' required>{% for e in employees %}<option value='{{e.id}}'>{{e.username}}</option>{% endfor %}</select></label>
  <label>Дата<input type='date' name='work_date' value='{{month}}-01' min='{{month}}-01' max='{{last_day}}' required></label>
  <label>Статус<select name='status' required>{% for key,name in statuses.items() %}<option value='{{key}}'>{{name}}</option>{% endfor %}</select></label>
  <label>Часов<input type='number' name='hours' min='0' max='24' step='0.25' value='8' required></label>
  <label>Примечание<input name='note' maxlength='500' placeholder='Смена, причина отсутствия…'></label>
  <button>Сохранить запись</button></form><p class='muted'>Повторное сохранение той же даты исправляет запись с сохранением истории. Для выходного, отпуска и отсутствия укажите 0 часов.</p></div>{% endif %}
  <div class='card'><h2>Итоги по сотрудникам</h2><table><tr><th>Сотрудник</th><th>Заполнено дней</th><th>Рабочих дней</th><th>Часов</th></tr>
  {% for s in summary %}<tr><td>{{s.username}}</td><td>{{s.days_recorded}}</td><td>{{s.work_days}}</td><td>{{'%.2f'|format(s.hours)}}</td></tr>{% endfor %}</table></div>
  <div class='card'><h2>Записи за месяц</h2><table><tr><th>Дата</th><th>Сотрудник</th><th>Статус</th><th>Часы</th><th>Примечание</th><th>Изменено</th></tr>
  {% for r in rows %}<tr><td>{{r.work_date}}</td><td>{{r.username}}</td><td>{{statuses[r.status]}}</td><td>{{r.hours}}</td><td>{{r.note}}</td><td>{{r.updated_at}}</td></tr>
  {% else %}<tr><td colspan='6'>Нет записей за выбранный месяц</td></tr>{% endfor %}</table></div>
  """,month=month,last_day=month+'-'+str(monthrange(int(month[:4]),int(month[5:]))[1]).zfill(2),
  employees=employees,names={e['id']:e['username'] for e in employees},editor_id=editor_id,
  is_admin=is_admin,is_editor=is_editor,rows=rows,summary=summary,statuses=STATUSES)

def _next_month(month):
 y,m=map(int,month.split('-'))
 return '%04d-%02d-01'%(y+1,1) if m==12 else '%04d-%02d-01'%(y,m+1)
