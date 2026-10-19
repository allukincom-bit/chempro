"""v51: admin-facing health status and in-app alerts, no external notification service."""
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta


def backup_status(directory, max_age_hours=36, now=None):
    path=Path(directory)/'lovetech-backup-status.json'
    try:
        if path.is_symlink():raise ValueError('symlink not allowed')
        data=json.loads(path.read_text(encoding='utf-8'))
        stamp=datetime.fromisoformat(data['checked_at_utc'].replace('Z','+00:00'))
        if stamp.tzinfo is None:raise ValueError('timezone missing')
        age=(now or datetime.now(timezone.utc))-stamp
        if age < timedelta(minutes=-5):return {'state':'warning','message':'Время резервного копирования находится в будущем'}
        if data.get('status')!='ok' or not data.get('restore_verified'):
            return {'state':'error','message':'Последнее резервное копирование завершилось с ошибкой'}
        if age>timedelta(hours=max_age_hours):
            return {'state':'warning','message':'Последняя проверенная копия устарела'}
        return {'state':'ok','message':'Копия создана и проверена восстановлением','checked_at':stamp.isoformat()}
    except FileNotFoundError:
        return {'state':'warning','message':'Отчёт о резервном копировании отсутствует'}
    except (ValueError,KeyError,TypeError,OSError,json.JSONDecodeError):
        return {'state':'error','message':'Не удалось прочитать отчёт о резервном копировании'}


def ensure_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS admin_alerts_v51 (
      id INTEGER PRIMARY KEY, alert_key TEXT NOT NULL UNIQUE, severity TEXT NOT NULL,
      title TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
      acknowledged_at TEXT, acknowledged_by INTEGER)""")


def reconcile(c, issues, stamp):
    """Active issues are deduplicated; reappearing issues reopen after acknowledgement."""
    ensure_schema(c)
    for key,severity,title in issues:
        row=c.execute('SELECT id,acknowledged_at FROM admin_alerts_v51 WHERE alert_key=?',(key,)).fetchone()
        if row is None:
            c.execute('INSERT INTO admin_alerts_v51(alert_key,severity,title,first_seen,last_seen) VALUES(?,?,?,?,?)',
                      (key,severity,title,stamp,stamp))
        else:
            c.execute('UPDATE admin_alerts_v51 SET severity=?,title=?,last_seen=? WHERE id=?',
                      (severity,title,stamp,row[0]))
    # resolved alerts are retained as history, but not listed as active
    return {item[0] for item in issues}


def register(app,connect,db_path,page,allowed,readiness):
    from flask import request, session, redirect, abort
    @app.route('/admin/system',methods=['GET','POST'])
    @allowed('admin')
    def system_v51():
        if request.method=='POST':
            try: alert_id=int(request.form.get('alert_id',''))
            except ValueError:abort(400)
            with connect() as c:
                ensure_schema(c)
                result=c.execute('UPDATE admin_alerts_v51 SET acknowledged_at=?,acknowledged_by=? WHERE id=?',
                                 (datetime.now(timezone.utc).isoformat(),session['uid'],alert_id))
                if result.rowcount!=1:abort(404)
            return redirect('/admin/system')
        ready=readiness(db_path)
        directory=os.environ.get('LOVETECH_BACKUP_DIR','/app/data/backups')
        backup=backup_status(directory)
        issues=[]
        if not ready:issues.append(('database','error','База SQLite недоступна'))
        if backup['state']!='ok':issues.append(('backup',backup['state'],backup['message']))
        stamp=datetime.now(timezone.utc).isoformat()
        with connect() as c:
            active=reconcile(c,issues,stamp)
            records=[dict(r) for r in c.execute('SELECT * FROM admin_alerts_v51 ORDER BY id DESC LIMIT 100')]
            errors=c.execute('SELECT COUNT(*) FROM operational_errors_v50').fetchone()[0] if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operational_errors_v50'").fetchone() else 0
        for r in records:r['active']=r['alert_key'] in active
        return page('Состояние системы lovetech',"""
        <div class='card'><h2>Состояние системы</h2>
        <p>SQLite: <strong>{{'Доступна' if ready else 'Недоступна'}}</strong></p>
        <p>Резервные копии: <strong>{{backup.message}}</strong></p>
        <p>Зарегистрировано ошибок сервера: {{errors}} · <a href='/admin/monitoring'>Журнал ошибок</a></p>
        <p class='muted'>Предупреждения появляются при открытии страницы. Подтверждение означает ознакомление, а не устранение причины. Внешняя отправка уведомлений не настроена.</p></div>
        <div class='card'><h2>Предупреждения администратора</h2>
        <table><tr><th>Состояние</th><th>Сообщение</th><th>Первое появление UTC</th><th>Действие</th></tr>
        {% for r in records if r.active %}<tr><td>{{r.severity}}</td><td>{{r.title}}</td><td>{{r.first_seen}}</td><td>
        {% if r.acknowledged_at %}Ознакомлен {{r.acknowledged_at}}{% else %}
        <form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'>
        <input type='hidden' name='alert_id' value='{{r.id}}'><button>Ознакомлен</button></form>{% endif %}</td></tr>
        {% else %}<tr><td colspan='4'>Активных предупреждений нет</td></tr>{% endfor %}</table></div>
        """,ready=ready,backup=backup,records=records,errors=errors)
