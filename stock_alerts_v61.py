"""Per-item minimum stock thresholds and targeted in-app notifications."""
import math
from flask import request,session,redirect,abort


def init_stock_alerts(c):
 c.executescript("""
 CREATE TABLE IF NOT EXISTS stock_thresholds_v61(
  id INTEGER PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('raw','finished')),
  item_id INTEGER NOT NULL,minimum REAL NOT NULL CHECK(minimum>=0),
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  UNIQUE(kind,item_id));
 CREATE TABLE IF NOT EXISTS stock_threshold_recipients_v61(
  threshold_id INTEGER NOT NULL REFERENCES stock_thresholds_v61(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY(threshold_id,user_id));
 CREATE TABLE IF NOT EXISTS stock_alert_events_v61(
  id INTEGER PRIMARY KEY,threshold_id INTEGER NOT NULL REFERENCES stock_thresholds_v61(id) ON DELETE CASCADE,
  opened_at TEXT NOT NULL,closed_at TEXT,
  qty_at_open REAL NOT NULL);
 CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_alert_open_v61
  ON stock_alert_events_v61(threshold_id) WHERE closed_at IS NULL;
 """)

def stock_balances(c):
 from stock_planning_v65 import availability
 return {key:values['available'] for key,values in availability(c).items()}

def refresh_stock_alerts(c,now):
 """Update transitions atomically; one open event per threshold, no notification spam."""
 balances=stock_balances(c)
 rows=c.execute('SELECT id,kind,item_id,minimum,enabled FROM stock_thresholds_v61').fetchall()
 for r in rows:
  qty=balances.get((r['kind'],r['item_id']),0.0)
  low=bool(r['enabled']) and qty<r['minimum']
  if low:
   c.execute('INSERT OR IGNORE INTO stock_alert_events_v61(threshold_id,opened_at,qty_at_open) VALUES(?,?,?)',
    (r['id'],now,qty))
  else:
   c.execute('UPDATE stock_alert_events_v61 SET closed_at=? WHERE threshold_id=? AND closed_at IS NULL',(now,r['id']))

def stock_notification_items(c,user_id):
 from datetime import datetime
 now=datetime.now().isoformat(timespec='seconds')
 # This function is called from the notifications GET/POST flow; commit on the
 # surrounding connection context so state transitions persist.
 refresh_stock_alerts(c,now)
 rows=c.execute('''SELECT e.id,e.opened_at,t.kind,t.minimum,
 CASE WHEN t.kind='raw' THEN m.name ELSE p.name END name,
 CASE WHEN t.kind='raw' THEN m.unit ELSE p.unit END unit,
 t.item_id AS item_id
 FROM stock_alert_events_v61 e
 JOIN stock_thresholds_v61 t ON t.id=e.threshold_id
 JOIN stock_threshold_recipients_v61 r ON r.threshold_id=t.id AND r.user_id=?
 LEFT JOIN materials m ON t.kind='raw' AND m.id=t.item_id
 LEFT JOIN products p ON t.kind='finished' AND p.id=t.item_id
 WHERE e.closed_at IS NULL AND t.enabled=1 ORDER BY e.id DESC''',(user_id,)).fetchall()
 balances=stock_balances(c)
 return [dict(key='stock-low:'+str(r['id']),kind='Склад',
  title='Низкий остаток: '+(r['name'] or 'Позиция удалена'),
  subtitle=('Сырьё' if r['kind']=='raw' else 'Готовая продукция')+
  ': доступно %.3f %s · минимум %.3f %s'%(balances.get((r['kind'],r['item_id']),0.),r['unit'],r['minimum'],r['unit']),
  url='/stock/thresholds',stamp=r['opened_at']) for r in rows]

def register_stock_alerts(app,conn,page,allowed,audit,now):
 @app.route('/stock/thresholds',methods=['GET','POST'])
 @allowed('warehouse','economist')
 def stock_thresholds_v61():
  if request.method=='POST':
   if not (session.get('uid')):abort(403)
   action=request.form.get('action')
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    if action=='save':
     kind=request.form.get('kind')
     if kind not in ('raw','finished'):abort(400)
     try:
      item_id=int(request.form.get('item_id',''))
      minimum=float(request.form.get('minimum',''))
     except (ValueError,TypeError):abort(400)
     if item_id<=0 or not math.isfinite(minimum) or minimum<0:abort(400)
     table='materials' if kind=='raw' else 'products'
     if not c.execute('SELECT 1 FROM '+table+' WHERE id=?',(item_id,)).fetchone():abort(400)
     try: recipients={int(x) for x in request.form.getlist('recipients')}
     except ValueError:abort(400)
     if not recipients:abort(400,description='Выберите хотя бы одного сотрудника')
     placeholders=','.join('?' for _ in recipients)
     valid={r['id'] for r in c.execute('SELECT id FROM users WHERE id IN ('+placeholders+')',tuple(recipients))}
     if valid!=recipients:abort(400)
     c.execute('''INSERT INTO stock_thresholds_v61(kind,item_id,minimum,enabled,created_at,updated_at)
      VALUES(?,?,?,1,?,?) ON CONFLICT(kind,item_id) DO UPDATE SET
      minimum=excluded.minimum,enabled=1,updated_at=excluded.updated_at''',
      (kind,item_id,minimum,now(),now()))
     tid=c.execute('SELECT id FROM stock_thresholds_v61 WHERE kind=? AND item_id=?',(kind,item_id)).fetchone()['id']
     c.execute('DELETE FROM stock_threshold_recipients_v61 WHERE threshold_id=?',(tid,))
     c.executemany('INSERT INTO stock_threshold_recipients_v61(threshold_id,user_id) VALUES(?,?)',[(tid,x) for x in sorted(recipients)])
     audit(c,'stock.threshold.save',kind+':'+str(item_id))
    elif action=='disable':
     try:tid=int(request.form.get('threshold_id',''))
     except ValueError:abort(400)
     if not c.execute('SELECT 1 FROM stock_thresholds_v61 WHERE id=?',(tid,)).fetchone():abort(404)
     c.execute('UPDATE stock_thresholds_v61 SET enabled=0,updated_at=? WHERE id=?',(now(),tid))
     audit(c,'stock.threshold.disable',str(tid))
    else:abort(400)
    refresh_stock_alerts(c,now())
   return redirect('/stock/thresholds')
  with conn() as c:
   refresh_stock_alerts(c,now())
   materials=[dict(x) for x in c.execute('SELECT id,name,unit FROM materials ORDER BY name')]
   products=[dict(x) for x in c.execute('SELECT id,name,unit FROM products ORDER BY name')]
   users=[dict(x) for x in c.execute('SELECT id,username FROM users ORDER BY username')]
   balances=stock_balances(c)
   rows=[dict(x) for x in c.execute('''SELECT t.*,CASE WHEN t.kind='raw' THEN m.name ELSE p.name END name,
    CASE WHEN t.kind='raw' THEN m.unit ELSE p.unit END unit,
    (SELECT COUNT(*) FROM stock_alert_events_v61 e WHERE e.threshold_id=t.id AND e.closed_at IS NULL) active
    FROM stock_thresholds_v61 t LEFT JOIN materials m ON t.kind='raw' AND m.id=t.item_id
    LEFT JOIN products p ON t.kind='finished' AND p.id=t.item_id ORDER BY t.kind,name''')]
   for r in rows:
    r['qty']=balances.get((r['kind'],r['item_id']),0.0)
    r['recipients']=', '.join(x['username'] for x in c.execute('''SELECT u.username FROM users u
     JOIN stock_threshold_recipients_v61 sr ON sr.user_id=u.id WHERE sr.threshold_id=? ORDER BY u.username''',(r['id'],)))
  return page('Минимальные остатки',"""
  <div class='card'><h2>Минимальные остатки и оповещения</h2>
  <p class='muted'>Учитываются разрешённые ОТК непросроченные партии за вычетом резервов. <a href="/stock/planning">Анализ потребности и заявки →</a>
  При снижении доступного количества ниже минимума выбранные сотрудники увидят задачу в разделе «Мои задачи и уведомления».
  Проверка выполняется при открытии страниц, без фоновых push-уведомлений.</p>
  <form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'>
  <label>Тип<select name='kind' id='kind_v61' onchange='document.getElementById("raw_v61").disabled=this.value!=="raw";document.getElementById("finished_v61").disabled=this.value!=="finished"'>
   <option value='raw'>Сырьё</option><option value='finished'>Готовая продукция</option></select></label>
  <label>Позиция<select name='item_id' id='raw_v61'>{% for m in materials %}<option value='{{m.id}}'>{{m.name}} ({{m.unit}})</option>{% endfor %}</select>
   <select name='item_id' id='finished_v61' disabled>{% for p in products %}<option value='{{p.id}}'>{{p.name}} ({{p.unit}})</option>{% endfor %}</select></label>
  <label>Минимум<input type='number' name='minimum' min='0' step='any' required></label>
  <fieldset><legend>Кому оповещать</legend>{% for u in users %}<label style='display:inline-flex;align-items:center'><input type='checkbox' name='recipients' value='{{u.id}}'> {{u.username}}</label>{% endfor %}</fieldset>
  <button name='action' value='save'>Сохранить порог</button></form>
  <p class='muted'>Повторное сохранение позиции заменяет минимум и список получателей. Для снятия контроля используйте «Отключить».</p></div>
  <div class='card'><h2>Установленные пороги</h2><table><thead><tr><th>Позиция</th><th>Остаток</th><th>Минимум</th><th>Оповещения</th><th>Получатели</th><th>Действие</th></tr></thead><tbody>
  {% for r in rows %}<tr><td>{{'Сырьё' if r.kind=='raw' else 'ГП'}} · {{r.name}}</td>
  <td>{{'%.3f'|format(r.qty)}} {{r.unit}}</td><td>{{'%.3f'|format(r.minimum)}} {{r.unit}}</td>
  <td>{{'Отключено' if not r.enabled else ('Ниже минимума' if r.active else 'Норма')}}</td>
  <td>{{r.recipients}}</td><td>{% if r.enabled %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'>
  <input type='hidden' name='threshold_id' value='{{r.id}}'><button name='action' value='disable'>Отключить</button></form>{% endif %}</td></tr>
  {% else %}<tr><td colspan='6'>Пороги ещё не заданы</td></tr>{% endfor %}</tbody></table></div>
  """,materials=materials,products=products,users=users,rows=rows)
