"""lovetech v50: safe operational diagnostics (no request payloads or credentials)."""
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

LOG = logging.getLogger('lovetech.monitor')

def ensure_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS operational_errors_v50 (
        id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
        endpoint TEXT NOT NULL, error_type TEXT NOT NULL, http_status INTEGER NOT NULL,
        actor_id INTEGER)""")
    connection.execute('CREATE INDEX IF NOT EXISTS idx_errors_v50_date ON operational_errors_v50(occurred_at DESC)')

def readiness(db_path):
    """No sensitive schema/data returned; read-only probe of the database."""
    try:
        uri = 'file:' + __import__('urllib.parse', fromlist=['quote']).quote(str(db_path), safe='/') + '?mode=ro'
        with sqlite3.connect(uri, uri=True, timeout=2) as c:
            c.execute('PRAGMA query_only=ON')
            c.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
        return True
    except (sqlite3.Error, OSError, ValueError):
        return False

def save_error(connect, endpoint, kind, status, actor, request_id):
    # Log only route template, exception class, HTTP status and actor id; never args, cookies, form or SQL.
    try:
        with connect() as c:
            ensure_schema(c)
            c.execute('INSERT INTO operational_errors_v50(occurred_at,request_id,endpoint,error_type,http_status,actor_id) VALUES(?,?,?,?,?,?)',
                      (datetime.now(timezone.utc).isoformat(timespec='seconds'),request_id,endpoint,kind,status,actor))
        return True
    except (sqlite3.Error, OSError):
        LOG.exception('Unable to persist operational error; request_id=%s',request_id)
        return False

def register(app, connect, db_path, page, allowed):
    from flask import request, session, jsonify
    from werkzeug.exceptions import HTTPException
    @app.get('/health/ready')
    def v50_ready():
        healthy=readiness(db_path)
        response=jsonify({'status':'ready' if healthy else 'unavailable'})
        response.status_code=200 if healthy else 503
        response.headers['Cache-Control']='no-store'
        return response

    @app.errorhandler(Exception)
    def v50_error(error):
        if isinstance(error, HTTPException):
            if error.code < 500:
                return error
            status=error.code
        else:
            status=500
        rid=uuid.uuid4().hex[:16]
        endpoint=request.url_rule.rule if request.url_rule else 'unmatched'
        actor=session.get('uid')
        save_error(connect,endpoint,type(error).__name__,status,actor,rid)
        LOG.error('Request failure request_id=%s endpoint=%s kind=%s status=%s',rid,endpoint,type(error).__name__,status)
        response=jsonify({'error':'Внутренняя ошибка сервера','request_id':rid})
        response.status_code=status
        response.headers['Cache-Control']='no-store'
        return response

    @app.get('/admin/monitoring')
    @allowed('admin')
    def v50_monitoring():
        with connect() as c:
            ensure_schema(c)
            records=[dict(r) for r in c.execute('SELECT occurred_at,request_id,endpoint,error_type,http_status,actor_id FROM operational_errors_v50 ORDER BY id DESC LIMIT 100')]
            total=c.execute('SELECT COUNT(*) FROM operational_errors_v50').fetchone()[0]
        return page('Мониторинг lovetech', """
        <div class='card'><h2>Состояние приложения</h2><p>База данных: <strong>{{'Доступна' if healthy else 'Недоступна'}}</strong></p>
        <p>Записей об ошибках: <b>{{total}}</b>. Показаны последние 100.</p>
        <p class='muted'>Журнал не сохраняет содержимое запросов, пароли, токены или SQL. Доступен только администратору.</p></div>
        <div class='card'><h2>Журнал серверных ошибок</h2><table><thead><tr><th>Время UTC</th><th>ID запроса</th><th>Маршрут</th><th>Тип</th><th>HTTP</th><th>Сотрудник ID</th></tr></thead>
        <tbody>{% for r in records %}<tr><td>{{r.occurred_at}}</td><td>{{r.request_id}}</td><td>{{r.endpoint}}</td><td>{{r.error_type}}</td><td>{{r.http_status}}</td><td>{{r.actor_id or '—'}}</td></tr>
        {% else %}<tr><td colspan='6'>Ошибок пока нет</td></tr>{% endfor %}</tbody></table></div>
        """,records=records,total=total,healthy=readiness(db_path))
