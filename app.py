import os, sqlite3, secrets
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, request, redirect, url_for, session, flash, render_template_string, abort
from werkzeug.security import generate_password_hash, check_password_hash

app=Flask(__name__)
app.secret_key=os.environ.get('SECRET_KEY',secrets.token_hex(32))
DB=os.environ.get('CHEM_DB','/app/data/chem.db')
@app.before_request
def csrf_guard():
 if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
  token = session.get('_csrf')
  supplied = request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
  if not token or not supplied or not secrets.compare_digest(token, supplied):
   abort(400, description='CSRF validation failed')

@app.context_processor
def csrf_context():
 if '_csrf' not in session:
  session['_csrf'] = secrets.token_urlsafe(32)
 return {'csrf_token': session['_csrf']}

@app.get('/health')
def health():
 return {'status': 'ok'}

ROLES={'admin':'Администратор','technologist':'Технолог','warehouse':'Кладовщик','operator':'Оператор','quality':'ОТК','viewer':'Просмотр'}

def conn():
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c

def init():
 with conn() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS materials(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,unit TEXT NOT NULL DEFAULT 'кг',shelf_days INTEGER NOT NULL DEFAULT 365);
  CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,unit TEXT NOT NULL DEFAULT 'кг',shelf_days INTEGER NOT NULL DEFAULT 365);
  CREATE TABLE IF NOT EXISTS recipes(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id),version INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'draft',notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL, UNIQUE(product_id,version));
  CREATE TABLE IF NOT EXISTS recipe_items(id INTEGER PRIMARY KEY,recipe_id INTEGER NOT NULL REFERENCES recipes(id),material_id INTEGER NOT NULL REFERENCES materials(id),percent REAL NOT NULL CHECK(percent>0 AND percent<=100),UNIQUE(recipe_id,material_id));
  CREATE TABLE IF NOT EXISTS steps(id INTEGER PRIMARY KEY,recipe_id INTEGER NOT NULL REFERENCES recipes(id),position INTEGER NOT NULL,instruction TEXT NOT NULL,control_point TEXT NOT NULL DEFAULT '');
  CREATE TABLE IF NOT EXISTS lots(id INTEGER PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('raw','finished')),material_id INTEGER REFERENCES materials(id),product_id INTEGER REFERENCES products(id),lot_code TEXT UNIQUE NOT NULL,qty REAL NOT NULL CHECK(qty>=0),manufactured TEXT NOT NULL,expires TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'quarantine',supplier TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL, CHECK((kind='raw' AND material_id IS NOT NULL AND product_id IS NULL) OR (kind='finished' AND product_id IS NOT NULL AND material_id IS NULL)));
  CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY,recipe_id INTEGER NOT NULL REFERENCES recipes(id),lot_id INTEGER UNIQUE NOT NULL REFERENCES lots(id),planned_qty REAL NOT NULL,actual_qty REAL NOT NULL,created_at TEXT NOT NULL,operator_id INTEGER NOT NULL REFERENCES users(id));
  CREATE TABLE IF NOT EXISTS consumptions(id INTEGER PRIMARY KEY,batch_id INTEGER NOT NULL REFERENCES batches(id),raw_lot_id INTEGER NOT NULL REFERENCES lots(id),qty REAL NOT NULL CHECK(qty>0));
  CREATE TABLE IF NOT EXISTS movements(id INTEGER PRIMARY KEY,lot_id INTEGER NOT NULL REFERENCES lots(id),delta REAL NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id));
  CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor_id INTEGER,action TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS shipments(id INTEGER PRIMARY KEY,lot_id INTEGER NOT NULL REFERENCES lots(id),customer TEXT NOT NULL,qty REAL NOT NULL CHECK(qty>0),created_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id));''')
  if not c.execute('SELECT 1 FROM users LIMIT 1').fetchone():
   pw=os.environ.get('ADMIN_PASSWORD')
   if not pw: pw=secrets.token_urlsafe(12);print('\n*** INITIAL ADMIN PASSWORD:',pw,'***\n',flush=True)
   c.execute('INSERT INTO users(username,password,role) VALUES(?,?,?)',('admin',generate_password_hash(pw),'admin'))

def now():return datetime.now().isoformat(timespec='seconds')
def audit(c,action,details):c.execute('INSERT INTO audit(actor_id,action,details,created_at) VALUES(?,?,?,?)',(session.get('uid'),action,details,now()))
def login_required(fn):
 @wraps(fn)
 def w(*a,**kw):
  if not session.get('uid'):return redirect(url_for('login'))
  return fn(*a,**kw)
 return w
def allowed(*roles):
 def deco(fn):
  @wraps(fn)
  @login_required
  def w(*a,**kw):
   if session.get('role') not in roles:abort(403)
   return fn(*a,**kw)
  return w
 return deco

def page(title,body,**ctx):
 base='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{{title}} · ХимПро</title><style>
 :root{font-family:system-ui,Arial;background:#f3f6fa;color:#17263b}body{margin:0}header{background:#12283f;color:white;padding:16px 24px;display:flex;justify-content:space-between;gap:15px;flex-wrap:wrap}nav{background:white;padding:12px 24px;display:flex;gap:15px;flex-wrap:wrap;border-bottom:1px solid #dce3ed}a{color:#11628e;text-decoration:none}nav a{font-weight:600}main{max-width:1200px;margin:25px auto;padding:0 16px}h1{font-size:26px}h2{font-size:20px}.card{background:white;border:1px solid #dfe6ee;border-radius:12px;padding:20px;margin:15px 0;overflow:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #e6ebf0;vertical-align:top}th{background:#f6f8fb}input,select,textarea{padding:10px;border:1px solid #bbc8d5;border-radius:7px;max-width:100%;box-sizing:border-box}input[type=number]{width:120px}label{display:inline-flex;flex-direction:column;gap:5px;margin:6px 12px 6px 0}button,.btn{background:#11628e;color:white;padding:10px 15px;border:0;border-radius:7px;cursor:pointer;display:inline-block}.danger{color:#a12727}.msg{background:#fff1c8;padding:12px;border-radius:7px;margin:10px 0}.muted{color:#627286;font-size:13px}.pill{padding:3px 8px;border-radius:12px;background:#e8edf3;white-space:nowrap}form{margin:8px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}.metric{font-size:30px;font-weight:700}pre{white-space:pre-wrap}</style>
 <header><b>⚗ ХимПро · производственный учёт</b><span>{% if session.uid %}{{session.username}} · {{roles[session.role]}} · <a style="color:white" href="{{url_for('logout')}}">Выход</a>{% endif %}</span></header>
 {% if session.uid %}<nav><a href="/">Обзор</a><a href="/catalog">Номенклатура</a><a href="/recipes">Рецептуры и техкарты</a><a href="/lots">Партии и склад</a><a href="/production">Производство</a><a href="/trace">Прослеживаемость</a><a href="/shipments">Отгрузки</a>{% if session.role=='admin' %}<a href="/users">Пользователи</a><a href="/audit">Журнал</a>{% endif %}</nav>{% endif %}
 <main><h1>{{title}}</h1>{% for m in get_flashed_messages() %}<div class="msg">{{m}}</div>{% endfor %}'''+body+'''</main></html>'''
 return render_template_string(base.replace('<form ', '<form ').replace('<form method="post">', '<form method="post"><input type="hidden" name="_csrf" value="{{csrf_token}}">'),title=title,roles=ROLES,today=date.today().isoformat(),**ctx)

def val(form,key,default=None):return form.get(key,default)
def number(form,key):
 v=float(form[key]);
 if not (0<v<1e12):raise ValueError('Количество должно быть положительным')
 return v

def fail(e):flash('Ошибка: '+str(e));return redirect(request.referrer or '/')

@app.route('/login',methods=['GET','POST'])
def login():
 if request.method=='POST':
  with conn() as c:u=c.execute('SELECT * FROM users WHERE username=?',(request.form.get('username'),)).fetchone()
  if u and check_password_hash(u['password'],request.form.get('password','')):
   session.clear();session.update(uid=u['id'],username=u['username'],role=u['role']);return redirect('/')
  flash('Неверный логин или пароль')
 return page('Вход','<div class="card"><form method="post"><label>Логин<input name="username" required></label><label>Пароль<input type="password" name="password" required></label><button>Войти</button></form></div>')
@app.route('/logout')
def logout():session.clear();return redirect('/login')
@app.route('/')
@login_required
def home():
 with conn() as c:
  counts={k:c.execute('SELECT count(*) FROM '+v).fetchone()[0] for k,v in {'raw':'materials','products':'products','lots':'lots','batches':'batches'}.items()}
  exp=c.execute("SELECT lot_code,expires,qty FROM lots WHERE qty>0 AND expires<=date('now','+30 day') ORDER BY expires LIMIT 20").fetchall()
 return page('Панель управления','''<div class="grid">{% for key,label in [('raw','Виды сырья'),('products','Продукты'),('lots','Партии'),('batches','Выпуски')] %}<div class="card"><div class="muted">{{label}}</div><div class="metric">{{counts[key]}}</div></div>{% endfor %}</div><div class="card"><h2>Истекающие в течение 30 дней и просроченные партии</h2><table><tr><th>Партия</th><th>Годен до</th><th>Остаток</th></tr>{% for x in exp %}<tr><td><a href="/trace?lot={{x.lot_code}}">{{x.lot_code}}</a></td><td>{{x.expires}}</td><td>{{x.qty}}</td></tr>{% endfor %}</table></div>''',counts=counts,exp=exp)
@app.route('/catalog',methods=['GET','POST'])
@login_required
def catalog():
 if request.method=='POST':
  if session['role'] not in ('admin','technologist'):abort(403)
  try:
   typ=request.form['kind'];table='materials' if typ=='raw' else 'products'
   with conn() as c:
    c.execute(f'INSERT INTO {table}(name,unit,shelf_days) VALUES(?,?,?)',(request.form['name'].strip(),request.form['unit'],int(request.form['shelf_days'])))
    audit(c,'catalog.create',typ+': '+request.form['name'])
   flash('Добавлено')
  except Exception as e:return fail(e)
  return redirect('/catalog')
 with conn() as c:raw=c.execute('SELECT * FROM materials ORDER BY name').fetchall();products=c.execute('SELECT * FROM products ORDER BY name').fetchall()
 return page('Номенклатура','''{% if session.role in ['admin','technologist'] %}<div class="card"><h2>Добавить позицию</h2><form method="post"><label>Тип<select name="kind"><option value="raw">Сырьё</option><option value="finished">Готовая продукция</option></select></label><label>Наименование<input name="name" required></label><label>Единица<select name="unit"><option>кг</option><option>л</option><option>шт</option></select></label><label>Срок годности, дней<input name="shelf_days" type="number" min="1" value="365" required></label><button>Добавить</button></form></div>{% endif %}{% for label,rows in [('Сырьё',raw),('Готовая продукция',products)] %}<div class="card"><h2>{{label}}</h2><table><tr><th>ID</th><th>Название</th><th>Ед.</th><th>Срок, дней</th></tr>{% for x in rows %}<tr><td>{{x.id}}</td><td>{{x.name}}</td><td>{{x.unit}}</td><td>{{x.shelf_days}}</td></tr>{% endfor %}</table></div>{% endfor %}''',raw=raw,products=products)
@app.route('/recipes',methods=['GET','POST'])
@login_required
def recipes():
 if request.method=='POST':
  if session['role'] not in ('admin','technologist'):abort(403)
  try:
   pid=int(request.form['product_id'])
   with conn() as c:
    version=c.execute('SELECT COALESCE(MAX(version),0)+1 FROM recipes WHERE product_id=?',(pid,)).fetchone()[0]
    cur=c.execute('INSERT INTO recipes(product_id,version,notes,created_at) VALUES(?,?,?,?)',(pid,version,request.form.get('notes',''),now()))
    audit(c,'recipe.create',str(cur.lastrowid))
   return redirect('/recipe/'+str(cur.lastrowid))
  except Exception as e:return fail(e)
 with conn() as c:
  products=c.execute('SELECT * FROM products ORDER BY name').fetchall()
  rows=c.execute('SELECT r.*,p.name FROM recipes r JOIN products p ON p.id=r.product_id ORDER BY r.id DESC').fetchall()
 return page('Рецептуры и технологические карты','''{% if session.role in ['admin','technologist'] %}<div class="card"><h2>Новая версия рецептуры</h2><form method="post"><label>Продукт<select name="product_id">{% for p in products %}<option value="{{p.id}}">{{p.name}}</option>{% endfor %}</select></label><label>Примечания<input name="notes"></label><button>Создать черновик</button></form></div>{% endif %}<div class="card"><table><tr><th>Продукт</th><th>Версия</th><th>Статус</th><th>Открыть</th></tr>{% for r in rows %}<tr><td>{{r.name}}</td><td>{{r.version}}</td><td>{{r.status}}</td><td><a href="/recipe/{{r.id}}">Техкарта →</a></td></tr>{% endfor %}</table></div>''',products=products,rows=rows)
@app.route('/recipe/<int:rid>',methods=['GET','POST'])
@login_required
def recipe(rid):
 if request.method=='POST':
  if session['role'] not in ('admin','technologist'):abort(403)
  try:
   with conn() as c:
    r=c.execute('SELECT * FROM recipes WHERE id=?',(rid,)).fetchone()
    if not r or r['status']!='draft':raise ValueError('Редактировать можно только черновик')
    action=request.form['action']
    if action=='item':c.execute('INSERT INTO recipe_items(recipe_id,material_id,percent) VALUES(?,?,?)',(rid,int(request.form['material_id']),number(request.form,'percent')))
    elif action=='step':c.execute('INSERT INTO steps(recipe_id,position,instruction,control_point) VALUES(?,?,?,?)',(rid,int(request.form['position']),request.form['instruction'],request.form.get('control_point','')))
    elif action=='approve':
     total=c.execute('SELECT COALESCE(SUM(percent),0) FROM recipe_items WHERE recipe_id=?',(rid,)).fetchone()[0]
     steps=c.execute('SELECT count(*) FROM steps WHERE recipe_id=?',(rid,)).fetchone()[0]
     if abs(total-100)>0.0001 or steps==0:raise ValueError('Для утверждения сумма компонентов должна быть 100%, необходим хотя бы один шаг')
     c.execute("UPDATE recipes SET status='approved' WHERE id=?",(rid,))
    else:raise ValueError('Неизвестная операция')
    audit(c,'recipe.'+action,str(rid))
  except Exception as e:return fail(e)
  return redirect('/recipe/'+str(rid))
 with conn() as c:
  r=c.execute('SELECT r.*,p.name FROM recipes r JOIN products p ON p.id=r.product_id WHERE r.id=?',(rid,)).fetchone()
  if not r:abort(404)
  items=c.execute('SELECT i.*,m.name,m.unit FROM recipe_items i JOIN materials m ON m.id=i.material_id WHERE recipe_id=?',(rid,)).fetchall()
  steps=c.execute('SELECT * FROM steps WHERE recipe_id=? ORDER BY position,id',(rid,)).fetchall()
  materials=c.execute('SELECT * FROM materials ORDER BY name').fetchall()
 return page('Техкарта: '+r['name']+' / v'+str(r['version']),'''<div class="card"><p><b>Статус:</b> {{r.status}} · <b>Примечания:</b> {{r.notes}}</p><h2>Состав</h2><table><tr><th>Сырьё</th><th>Доля, %</th></tr>{% for x in items %}<tr><td>{{x.name}}</td><td>{{x.percent}}</td></tr>{% endfor %}</table><p><b>Итого: {{total}}%</b></p><h2>Технологические операции</h2><table><tr><th>№</th><th>Операция</th><th>Контрольная точка</th></tr>{% for x in steps %}<tr><td>{{x.position}}</td><td>{{x.instruction}}</td><td>{{x.control_point}}</td></tr>{% endfor %}</table></div>{% if r.status=='draft' and session.role in ['admin','technologist'] %}<div class="card"><h2>Редактирование черновика</h2><form method="post"><input type="hidden" name="action" value="item"><label>Сырьё<select name="material_id">{% for m in materials %}<option value="{{m.id}}">{{m.name}}</option>{% endfor %}</select></label><label>Доля, %<input type="number" name="percent" min="0.000001" max="100" step="any" required></label><button>Добавить компонент</button></form><form method="post"><input type="hidden" name="action" value="step"><label>№<input name="position" type="number" min="1" required></label><label>Операция<input name="instruction" required></label><label>Контрольная точка<input name="control_point"></label><button>Добавить операцию</button></form><form method="post"><input type="hidden" name="action" value="approve"><button>Утвердить рецептуру</button></form></div>{% endif %}''',r=r,items=items,steps=steps,materials=materials,total=round(sum(x['percent'] for x in items),6))
@app.route('/lots',methods=['GET','POST'])
@login_required
def lots():
 if request.method=='POST':
  if session['role'] not in ('admin','warehouse','quality'):abort(403)
  try:
   action=request.form['action']
   with conn() as c:
    if action=='receive':
     mid=int(request.form['material_id']);qty=number(request.form,'qty');m=c.execute('SELECT * FROM materials WHERE id=?',(mid,)).fetchone()
     manufactured=request.form['manufactured'];exp=(date.fromisoformat(manufactured)+timedelta(days=m['shelf_days'])).isoformat()
     cur=c.execute("INSERT INTO lots(kind,material_id,lot_code,qty,manufactured,expires,status,supplier,created_at) VALUES('raw',?,?,?,?,?,'quarantine',?,?)",(mid,request.form['lot_code'],qty,manufactured,exp,request.form.get('supplier',''),now()))
     c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(cur.lastrowid,qty,'Приход сырья',now(),session['uid']))
    elif action=='status':
     if session['role'] not in ('admin','quality'):abort(403)
     lid=int(request.form['lot_id']);status=request.form['status']
     if status not in ('released','blocked','quarantine'):raise ValueError('Неверный статус')
     c.execute('UPDATE lots SET status=? WHERE id=?',(status,lid))
    else:raise ValueError('Неизвестная операция')
    audit(c,'lot.'+action,str(dict(request.form)))
   flash('Сохранено')
  except Exception as e:return fail(e)
  return redirect('/lots')
 with conn() as c:
  mats=c.execute('SELECT * FROM materials ORDER BY name').fetchall()
  rows=c.execute('SELECT l.*,COALESCE(m.name,p.name) name FROM lots l LEFT JOIN materials m ON m.id=l.material_id LEFT JOIN products p ON p.id=l.product_id ORDER BY l.id DESC').fetchall()
 return page('Партии и склад','''{% if session.role in ['admin','warehouse','quality'] %}<div class="card"><h2>Приёмка сырья</h2><form method="post"><input type="hidden" name="action" value="receive"><label>Сырьё<select name="material_id">{% for m in mats %}<option value="{{m.id}}">{{m.name}}</option>{% endfor %}</select></label><label>Код партии<input name="lot_code" required></label><label>Количество<input name="qty" type="number" step="any" min="0.000001" required></label><label>Дата изготовления<input name="manufactured" type="date" value="{{today}}" required></label><label>Поставщик<input name="supplier"></label><button>Принять на карантин</button></form></div>{% endif %}<div class="card"><h2>Остатки по партиям</h2><table><tr><th>Партия</th><th>Тип / позиция</th><th>Остаток</th><th>Годен до</th><th>Статус</th><th>Действия ОТК</th></tr>{% for l in rows %}<tr><td><a href="/trace?lot={{l.lot_code}}">{{l.lot_code}}</a></td><td>{{'Сырьё' if l.kind=='raw' else 'ГП'}} · {{l.name}}</td><td>{{'%.3f'|format(l.qty)}}</td><td>{{l.expires}}{% if l.expires<today %} <span class="danger">Просрочено</span>{% endif %}</td><td><span class="pill">{{l.status}}</span></td><td>{% if session.role in ['admin','quality'] %}<form method="post"><input type="hidden" name="action" value="status"><input type="hidden" name="lot_id" value="{{l.id}}"><select name="status"><option value="released">Разрешить</option><option value="quarantine">Карантин</option><option value="blocked">Блокировать</option></select><button>OK</button></form>{% endif %}</td></tr>{% endfor %}</table></div>''',mats=mats,rows=rows)
@app.route('/production',methods=['GET','POST'])
@login_required
def production():
 if request.method=='POST':
  if session['role'] not in ('admin','operator'):abort(403)
  try:
   rid=int(request.form['recipe_id']);planned=number(request.form,'planned_qty');actual=number(request.form,'actual_qty');code=request.form['lot_code'].strip()
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    r=c.execute("SELECT r.*,p.shelf_days FROM recipes r JOIN products p ON p.id=r.product_id WHERE r.id=? AND r.status='approved'",(rid,)).fetchone()
    if not r:raise ValueError('Рецептура не утверждена')
    items=c.execute('SELECT * FROM recipe_items WHERE recipe_id=?',(rid,)).fetchall()
    allocations=[]
    for item in items:
     needed=round(planned*item['percent']/100,8)
     raw=c.execute("SELECT * FROM lots WHERE kind='raw' AND material_id=? AND status='released' AND expires>=? AND qty>0 ORDER BY expires,id",(item['material_id'],date.today().isoformat())).fetchall()
     for lot in raw:
      take=min(needed,lot['qty'])
      if take>0:allocations.append((lot['id'],take));needed=round(needed-take,8)
      if needed<=0:break
     if needed>0.000001:raise ValueError('Недостаточно разрешённого годного сырья ID '+str(item['material_id']))
    made=date.today();exp=made+timedelta(days=r['shelf_days'])
    cur=c.execute("INSERT INTO lots(kind,product_id,lot_code,qty,manufactured,expires,status,created_at) VALUES('finished',?,?,?,?,?,'quarantine',?)",(r['product_id'],code,actual,made.isoformat(),exp.isoformat(),now()))
    bid=c.execute('INSERT INTO batches(recipe_id,lot_id,planned_qty,actual_qty,created_at,operator_id) VALUES(?,?,?,?,?,?)',(rid,cur.lastrowid,planned,actual,now(),session['uid'])).lastrowid
    for lid,qty in allocations:
     updated=c.execute('UPDATE lots SET qty=qty-? WHERE id=? AND qty>=?',(qty,lid,qty))
     if updated.rowcount!=1:raise ValueError('Остаток изменился, повторите операцию')
     c.execute('INSERT INTO consumptions(batch_id,raw_lot_id,qty) VALUES(?,?,?)',(bid,lid,qty))
     c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(lid,-qty,'Расход в производство #'+str(bid),now(),session['uid']))
    c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(cur.lastrowid,actual,'Выпуск #'+str(bid),now(),session['uid']))
    audit(c,'production.create','batch='+str(bid)+'; lot='+code)
   flash('Партия выпущена на карантин. Номер производственной партии: '+str(bid))
  except Exception as e:return fail(e)
  return redirect('/production')
 with conn() as c:
  recipes=c.execute("SELECT r.id,r.version,p.name FROM recipes r JOIN products p ON p.id=r.product_id WHERE r.status='approved' ORDER BY p.name,r.version DESC").fetchall()
  batches=c.execute('SELECT b.*,l.lot_code,p.name,r.version FROM batches b JOIN lots l ON l.id=b.lot_id JOIN recipes r ON r.id=b.recipe_id JOIN products p ON p.id=r.product_id ORDER BY b.id DESC').fetchall()
 return page('Производство','''{% if session.role in ['admin','operator'] %}<div class="card"><h2>Зарегистрировать выпуск</h2><p class="muted">Автоматическое списание сырья по FEFO. Плановый расход по рецептуре, фактический выход задаётся отдельно. Операции атомарны.</p><form method="post"><label>Утверждённая рецептура<select name="recipe_id">{% for r in recipes %}<option value="{{r.id}}">{{r.name}} · v{{r.version}}</option>{% endfor %}</select></label><label>План, кг/ед.<input name="planned_qty" type="number" min="0.000001" step="any" required></label><label>Факт, кг/ед.<input name="actual_qty" type="number" min="0.000001" step="any" required></label><label>Номер партии ГП<input name="lot_code" required></label><button>Провести производство</button></form></div>{% endif %}<div class="card"><h2>Журнал выпусков</h2><table><tr><th>ID</th><th>Дата</th><th>Продукт</th><th>Партия</th><th>План</th><th>Факт</th></tr>{% for b in batches %}<tr><td>{{b.id}}</td><td>{{b.created_at}}</td><td>{{b.name}} · v{{b.version}}</td><td><a href="/trace?lot={{b.lot_code}}">{{b.lot_code}}</a></td><td>{{b.planned_qty}}</td><td>{{b.actual_qty}}</td></tr>{% endfor %}</table></div>''',recipes=recipes,batches=batches)
@app.route('/trace')
@login_required
def trace():
 code=request.args.get('lot','').strip();lot=None;parents=[];children=[];ship=[]
 if code:
  with conn() as c:
   lot=c.execute('SELECT * FROM lots WHERE lot_code=?',(code,)).fetchone()
   if lot:
    if lot['kind']=='finished':
     parents=c.execute('SELECT l.lot_code,c.qty,m.name FROM batches b JOIN consumptions c ON c.batch_id=b.id JOIN lots l ON l.id=c.raw_lot_id JOIN materials m ON m.id=l.material_id WHERE b.lot_id=?',(lot['id'],)).fetchall()
     ship=c.execute('SELECT * FROM shipments WHERE lot_id=?',(lot['id'],)).fetchall()
    else:
     children=c.execute('SELECT l.lot_code,c.qty,p.name FROM consumptions c JOIN batches b ON b.id=c.batch_id JOIN lots l ON l.id=b.lot_id JOIN products p ON p.id=l.product_id WHERE c.raw_lot_id=?',(lot['id'],)).fetchall()
 return page('Двусторонняя прослеживаемость','''<div class="card"><form method="get"><label>Номер партии<input name="lot" value="{{code}}" required></label><button>Найти связи</button></form></div>{% if code and not lot %}<div class="msg">Партия не найдена</div>{% endif %}{% if lot %}<div class="card"><h2>{{lot.lot_code}} · {{'сырьё' if lot.kind=='raw' else 'готовая продукция'}}</h2><p>Остаток: {{lot.qty}} · Статус: {{lot.status}} · Годен до: {{lot.expires}}</p>{% if lot.kind=='finished' %}<h2>Из каких партий сырья произведена</h2><table><tr><th>Партия сырья</th><th>Наименование</th><th>Расход</th></tr>{% for x in parents %}<tr><td><a href="/trace?lot={{x.lot_code}}">{{x.lot_code}}</a></td><td>{{x.name}}</td><td>{{x.qty}}</td></tr>{% endfor %}</table><h2>Кому отгружено</h2><table><tr><th>Клиент</th><th>Количество</th><th>Дата</th></tr>{% for x in ship %}<tr><td>{{x.customer}}</td><td>{{x.qty}}</td><td>{{x.created_at}}</td></tr>{% endfor %}</table>{% else %}<h2>В какие партии ГП вошло сырьё</h2><table><tr><th>Партия ГП</th><th>Продукт</th><th>Расход сырья</th></tr>{% for x in children %}<tr><td><a href="/trace?lot={{x.lot_code}}">{{x.lot_code}}</a></td><td>{{x.name}}</td><td>{{x.qty}}</td></tr>{% endfor %}</table>{% endif %}</div>{% endif %}''',code=code,lot=lot,parents=parents,children=children,ship=ship)
@app.route('/shipments',methods=['GET','POST'])
@login_required
def shipments():
 if request.method=='POST':
  if session['role'] not in ('admin','warehouse'):abort(403)
  try:
   lid=int(request.form['lot_id']);qty=number(request.form,'qty');customer=request.form['customer'].strip()
   if not customer:raise ValueError('Укажите клиента')
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    updated=c.execute("UPDATE lots SET qty=qty-? WHERE id=? AND kind='finished' AND status='released' AND expires>=? AND qty>=?",(qty,lid,date.today().isoformat(),qty))
    if updated.rowcount!=1:raise ValueError('Недостаточно разрешённой годной продукции')
    c.execute('INSERT INTO shipments(lot_id,customer,qty,created_at,user_id) VALUES(?,?,?,?,?)',(lid,customer,qty,now(),session['uid']))
    c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(lid,-qty,'Отгрузка '+customer,now(),session['uid']))
    audit(c,'shipment.create',str(lid)+' '+customer+' '+str(qty))
   flash('Отгрузка проведена')
  except Exception as e:return fail(e)
  return redirect('/shipments')
 with conn() as c:
  lots=c.execute("SELECT l.*,p.name FROM lots l JOIN products p ON p.id=l.product_id WHERE l.kind='finished' AND l.status='released' AND l.qty>0 AND l.expires>=?",(date.today().isoformat(),)).fetchall()
  rows=c.execute('SELECT s.*,l.lot_code FROM shipments s JOIN lots l ON l.id=s.lot_id ORDER BY s.id DESC').fetchall()
 return page('Отгрузки','''{% if session.role in ['admin','warehouse'] %}<div class="card"><h2>Новая отгрузка</h2><form method="post"><label>Партия<select name="lot_id">{% for l in lots %}<option value="{{l.id}}">{{l.lot_code}} · {{l.name}} ({{l.qty}})</option>{% endfor %}</select></label><label>Клиент<input name="customer" required></label><label>Количество<input name="qty" type="number" min="0.000001" step="any" required></label><button>Отгрузить</button></form></div>{% endif %}<div class="card"><table><tr><th>Дата</th><th>Партия</th><th>Клиент</th><th>Количество</th></tr>{% for x in rows %}<tr><td>{{x.created_at}}</td><td><a href="/trace?lot={{x.lot_code}}">{{x.lot_code}}</a></td><td>{{x.customer}}</td><td>{{x.qty}}</td></tr>{% endfor %}</table></div>''',lots=lots,rows=rows)
@app.route('/users',methods=['GET','POST'])
@allowed('admin')
def users():
 if request.method=='POST':
  try:
   role=request.form['role']
   if role not in ROLES:raise ValueError('Роль не найдена')
   if len(request.form['password'])<10:raise ValueError('Пароль не менее 10 символов')
   with conn() as c:
    c.execute('INSERT INTO users(username,password,role) VALUES(?,?,?)',(request.form['username'],generate_password_hash(request.form['password']),role))
    audit(c,'user.create',request.form['username']+' '+role)
   flash('Пользователь добавлен')
  except Exception as e:return fail(e)
  return redirect('/users')
 with conn() as c:rows=c.execute('SELECT id,username,role FROM users ORDER BY id').fetchall()
 return page('Пользователи и роли','''<div class="card"><form method="post"><label>Логин<input name="username" required></label><label>Пароль<input type="password" name="password" minlength="10" required></label><label>Роль<select name="role">{% for k,v in roles.items() %}<option value="{{k}}">{{v}}</option>{% endfor %}</select></label><button>Добавить</button></form></div><div class="card"><table><tr><th>Логин</th><th>Роль</th></tr>{% for u in rows %}<tr><td>{{u.username}}</td><td>{{roles[u.role]}}</td></tr>{% endfor %}</table></div>''',rows=rows)
@app.route('/audit')
@allowed('admin')
def audit_view():
 with conn() as c:rows=c.execute('SELECT a.*,u.username FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 300').fetchall()
 return page('Журнал действий','''<div class="card"><table><tr><th>Время</th><th>Пользователь</th><th>Действие</th><th>Детали</th></tr>{% for x in rows %}<tr><td>{{x.created_at}}</td><td>{{x.username}}</td><td>{{x.action}}</td><td>{{x.details}}</td></tr>{% endfor %}</table></div>''',rows=rows)
if __name__=='__main__':
 init();app.run(host=os.environ.get('HOST','127.0.0.1'),port=int(os.environ.get('PORT',5000)),debug=False)
