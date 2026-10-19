"""Central shipping QC check and SQLite trigger defense (v66)."""
from datetime import date

def shipping_eligibility_v66(c, lot_id, today=None):
    today = today or date.today().isoformat()
    lot=c.execute('SELECT id,kind,product_id,status,expires,qty FROM lots WHERE id=?',(lot_id,)).fetchone()
    if not lot or lot['kind']!='finished':return False,'Партия готовой продукции не найдена'
    if lot['status']!='released':return False,'Партия не разрешена ОТК'
    if lot['expires']<today:return False,'Истёк срок годности'
    if lot['qty']<=0:return False,'Нет доступного остатка'
    specs=c.execute("SELECT id,min_value,max_value FROM qc_specs WHERE kind='finished' AND item_id=? AND active=1",(lot['product_id'],)).fetchall()
    if not specs:return False,'Нет действующих норм ОТК'
    for spec in specs:
        result=c.execute('SELECT value,passed FROM qc_results WHERE lot_id=? AND spec_id=? ORDER BY id DESC LIMIT 1',(lot_id,spec['id'])).fetchone()
        if not result or result['passed']!=1:return False,'Нет положительного результата по обязательной норме'
        if ((spec['min_value'] is not None and result['value']<spec['min_value']) or
            (spec['max_value'] is not None and result['value']>spec['max_value'])):
            return False,'Результат ОТК вне действующих границ'
    latest=c.execute('SELECT status FROM qc_samples WHERE lot_id=? ORDER BY id DESC LIMIT 1',(lot_id,)).fetchone()
    if latest and latest['status']!='reviewed':return False,'Последняя проба ОТК не одобрена'
    return True,'Допущена к отгрузке'

def install_shipping_guard_v66(c):
    # SQLite triggers protect direct SQL writes and every application shipping route.
    # A release status alone is insufficient if QC measurements are missing or stale.
    c.executescript("""
    CREATE TRIGGER IF NOT EXISTS v66_shipment_qc_guard BEFORE INSERT ON shipments
    BEGIN
      SELECT CASE WHEN NOT EXISTS(
        SELECT 1 FROM lots l WHERE l.id=NEW.lot_id AND l.kind='finished'
          AND l.status='released' AND l.expires>=date('now')
      ) THEN RAISE(ABORT,'v66: lot is not QC released or is expired') END;
      SELECT CASE WHEN NOT EXISTS(
        SELECT 1 FROM qc_specs s JOIN lots l ON l.product_id=s.item_id
        WHERE l.id=NEW.lot_id AND s.kind='finished' AND s.active=1
      ) THEN RAISE(ABORT,'v66: no active finished QC specs') END;
      SELECT CASE WHEN EXISTS(
        SELECT 1 FROM qc_specs s JOIN lots l ON l.product_id=s.item_id
        WHERE l.id=NEW.lot_id AND s.kind='finished' AND s.active=1 AND NOT EXISTS(
          SELECT 1 FROM qc_results r WHERE r.id=(
            SELECT MAX(rr.id) FROM qc_results rr WHERE rr.lot_id=NEW.lot_id AND rr.spec_id=s.id
          ) AND r.passed=1 AND (s.min_value IS NULL OR r.value>=s.min_value)
            AND (s.max_value IS NULL OR r.value<=s.max_value)
        )
      ) THEN RAISE(ABORT,'v66: missing or failed QC results') END;
      SELECT CASE WHEN EXISTS(
        SELECT 1 FROM qc_samples q WHERE q.id=(
          SELECT MAX(qq.id) FROM qc_samples qq WHERE qq.lot_id=NEW.lot_id
        ) AND q.status<>'reviewed'
      ) THEN RAISE(ABORT,'v66: latest QC sample not approved') END;
    END;
    """)
