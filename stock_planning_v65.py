"""v65: FEFO-safe availability and explicit purchase suggestions (no automatic ordering)."""
from datetime import date
from flask import request, redirect, abort, session


def availability(c, today=None):
    today=today or date.today().isoformat()
    data={}
    rows=c.execute("""SELECT l.id,l.kind,CASE WHEN l.kind='raw' THEN l.material_id ELSE l.product_id END item_id,
       l.qty,l.expires,l.status,COALESCE(rv.reserved,0) reserved
       FROM lots l LEFT JOIN (SELECT lot_id,SUM(qty) reserved FROM reservations GROUP BY lot_id) rv ON rv.lot_id=l.id""")
    for r in rows:
        key=(r['kind'],r['item_id'])
        x=data.setdefault(key,{'physical':0.,'released':0.,'reserved':0.,'expired':0.,'available':0.})
        x['physical']+=r['qty']
        if r['status']!='released':continue
        if r['expires']<today:
            x['expired']+=r['qty'];continue
        x['released']+=r['qty']
        # Reservations cannot create negative available stock.
        held=min(max(0.,r['reserved']),max(0.,r['qty']))
        x['reserved']+=held
        x['available']+=max(0.,r['qty']-held)
    return data


def purchase_gap(minimum, available, outstanding=0.):
    return round(max(0.,float(minimum)-max(0.,float(available))-max(0.,float(outstanding))),6)


def open_supply(c,material_id):
    # Ordered quantities are informational; no receipt is presumed.
    requested=c.execute("SELECT COALESCE(SUM(qty),0) FROM purchase_requests WHERE material_id=? AND status='open'",(material_id,)).fetchone()[0]
    ordered=c.execute("SELECT COALESCE(SUM(MAX(0,qty-received_qty)),0) FROM purchase_orders WHERE material_id=? AND status IN ('ordered','partial')",(material_id,)).fetchone()[0]
    return float(requested or 0)+float(ordered or 0)


def register_v65(app,conn,page,allowed,audit,now):
    @app.route('/stock/planning',methods=['GET','POST'])
    @allowed('warehouse','economist')
    def stock_planning_v65():
        if request.method=='POST':
            if request.form.get('action')!='suggest':abort(400)
            try:tid=int(request.form.get('threshold_id',''))
            except (TypeError,ValueError):abort(400)
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                t=c.execute("SELECT * FROM stock_thresholds_v61 WHERE id=? AND kind='raw' AND enabled=1",(tid,)).fetchone()
                if not t:abort(404)
                a=availability(c).get(('raw',t['item_id']),{'available':0.})['available']
                outstanding=open_supply(c,t['item_id'])
                qty=purchase_gap(t['minimum'],a,outstanding)
                if qty<=0:abort(409,description='Потребность уже покрыта остатком или открытыми заявками')
                # Double-submit is prevented by BEGIN IMMEDIATE and recalculation.
                c.execute("INSERT INTO purchase_requests(material_id,qty,needed_by,status,note,created_at,created_by) VALUES(?,?,?,'open',?,?,?)",
                  (t['item_id'],qty,__import__('datetime').date.today().isoformat(),
                   'Предложение lovetech v65: порог №'+str(tid)+'; требуется подтверждение закупки',now(),session['uid']))
                audit(c,'stock.v65.purchase_suggestion',str(tid)+':'+str(qty))
            return redirect('/stock/planning')
        with conn() as c:
            balances=availability(c)
            thresholds=[dict(x) for x in c.execute("""SELECT t.*,CASE WHEN t.kind='raw' THEN m.name ELSE p.name END name,
             CASE WHEN t.kind='raw' THEN m.unit ELSE p.unit END unit
             FROM stock_thresholds_v61 t LEFT JOIN materials m ON t.kind='raw' AND m.id=t.item_id
             LEFT JOIN products p ON t.kind='finished' AND p.id=t.item_id WHERE t.enabled=1 ORDER BY t.kind,name""")]
            for t in thresholds:
                x=balances.get((t['kind'],t['item_id']),{})
                t.update({k:x.get(k,0.) for k in ('physical','released','reserved','expired','available')})
                t['outstanding']=open_supply(c,t['item_id']) if t['kind']=='raw' else 0.
                t['gap']=purchase_gap(t['minimum'],t['available'],t['outstanding'])
                t['low']=t['available']<t['minimum']
        return page('Потребность в сырье и минимальные остатки',"""
        <div class='card'><h2>Доступные остатки и предложения на закупку</h2>
        <p class='muted'>Доступно = разрешённые ОТК непросроченные партии минус резервы по каждой партии.
        Карантин и просроченные партии исключены. Заказы и открытые заявки учитываются только при расчёте предложения,
        не прибавляются к складскому остатку. Действие создаёт заявку, но не заказ поставщику.</p>
        <p><a href='/stock/thresholds'>Настроить пороги и получателей оповещений →</a></p>
        <table><thead><tr><th>Позиция</th><th>Физически</th><th>Разрешено и годно</th><th>Резерв</th><th>Доступно</th><th>Минимум</th><th>В заявках/заказах</th><th>Предложение</th></tr></thead><tbody>
        {% for t in thresholds %}<tr><td>{{'Сырьё' if t.kind=='raw' else 'ГП'}} · {{t.name}}{% if t.low %} ⚠{% endif %}</td>
        <td>{{'%.3f'|format(t.physical)}}</td><td>{{'%.3f'|format(t.released)}}</td><td>{{'%.3f'|format(t.reserved)}}</td>
        <td><b>{{'%.3f'|format(t.available)}}</b> {{t.unit}}</td><td>{{'%.3f'|format(t.minimum)}}</td>
        <td>{{'%.3f'|format(t.outstanding)}}</td><td>{% if t.kind=='raw' and t.gap>0 %}
        <form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='suggest'>
        <input type='hidden' name='threshold_id' value='{{t.id}}'><button>Создать заявку на {{'%.3f'|format(t.gap)}} {{t.unit}}</button></form>
        {% elif t.kind=='finished' %}—{% else %}Покрыто{% endif %}</td></tr>
        {% else %}<tr><td colspan='8'>Сначала настройте пороги.</td></tr>{% endfor %}</tbody></table></div>
        """,thresholds=thresholds)
