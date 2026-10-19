"""Isolated end-to-end production tests. Never point this tool at the production database."""
import json, os, sqlite3, tempfile, unittest
from datetime import date, timedelta
from pathlib import Path

class ProductionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import flask, qrcode
        except ImportError as exc:
            raise unittest.SkipTest('Flask/qrcode dependencies not installed: '+str(exc))

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='lovetech-v55-')
        self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'disposable.sqlite'
        os.environ['CHEM_DB']=str(self.db)
        import app as module
        self.module=module
        module.DB=str(self.db)
        module.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False, SECRET_KEY='v55-isolated-testing-secret')
        module.init()
        self.client=module.app.test_client()
        with module.conn() as c:
            c.execute("INSERT INTO users(username,password,role) VALUES('v55_admin','unused','admin')")
            self.uid=c.execute("SELECT id FROM users WHERE username='v55_admin'").fetchone()[0]
            c.execute("INSERT INTO materials(name,unit) VALUES('Тестовое сырьё','кг')")
            self.material=c.execute("SELECT id FROM materials WHERE name='Тестовое сырьё'").fetchone()[0]
            c.execute("INSERT INTO products(name,unit,shelf_days) VALUES('Тестовый продукт','кг',365)")
            self.product=c.execute("SELECT id FROM products WHERE name='Тестовый продукт'").fetchone()[0]
            c.execute("INSERT INTO recipes(product_id,version,status,notes,created_at) VALUES(?,1,'approved','',datetime('now'))",(self.product,))
            self.recipe=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute('INSERT INTO recipe_items(recipe_id,material_id,percent) VALUES(?,?,100)',(self.recipe,self.material))
            for code,qty,days in [('V55-RAW-OLDER',70,40),('V55-RAW-NEWER',80,100)]:
                c.execute("INSERT INTO lots(kind,material_id,lot_code,qty,manufactured,expires,status,created_at) VALUES('raw',?,?,?,?,?,'released',datetime('now'))",(self.material,code,qty,date.today().isoformat(),(date.today()+timedelta(days=days)).isoformat()))
        with self.client.session_transaction() as session:
            session.update(uid=self.uid,username='v55_admin',role='admin',_csrf='v55-csrf')

    def post(self,path,**fields):
        return self.client.post(path,data={'_csrf':'v55-csrf',**fields},follow_redirects=False)

    def scalar(self,sql,args=()):
        with self.module.conn() as c:return c.execute(sql,args).fetchone()[0]

    def order(self,code='V55-FG-1',qty=100):
        r=self.post('/work-orders',action='create',recipe_id=str(self.recipe),planned_qty=str(qty),lot_code=code,due_date=date.today().isoformat())
        self.assertEqual(r.status_code,302)
        with self.module.conn() as c:
            row=c.execute('SELECT id,status FROM work_orders WHERE lot_code=?',(code,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row['status'],'draft')
            return row['id']

    def approve(self,oid):
        self.assertEqual(self.post('/work-orders',action='approve',order_id=str(oid)).status_code,302)
        self.assertEqual(self.scalar('SELECT status FROM work_orders WHERE id=?',(oid,)),'approved')

    def test_fefo_reserve_and_complete_atomic(self):
        oid=self.order();self.approve(oid)
        self.post('/work-orders',action='reserve',order_id=str(oid))
        with self.module.conn() as c:
            allocations=c.execute('SELECT l.lot_code,r.qty FROM reservations r JOIN lots l ON l.id=r.lot_id WHERE r.order_id=? ORDER BY l.expires',(oid,)).fetchall()
        self.assertEqual([(x[0],x[1]) for x in allocations],[('V55-RAW-OLDER',70),('V55-RAW-NEWER',30)])
        self.assertEqual(self.post('/production',order_id=str(oid),recipe_id=str(self.recipe),planned_qty='100',actual_qty='96',lot_code='V55-FG-1').status_code,302)
        self.assertEqual(self.scalar('SELECT status FROM work_orders WHERE id=?',(oid,)),'completed')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reservations WHERE order_id=?',(oid,)),0)
        self.assertEqual(self.scalar("SELECT qty FROM lots WHERE lot_code='V55-RAW-OLDER'"),0)
        self.assertEqual(self.scalar("SELECT qty FROM lots WHERE lot_code='V55-RAW-NEWER'"),50)
        self.assertEqual(self.scalar("SELECT qty FROM lots WHERE lot_code='V55-FG-1'"),96)
        self.assertEqual(self.scalar("SELECT status FROM lots WHERE lot_code='V55-FG-1'"),'quarantine')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM consumptions'),2)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM movements'),3)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM batches'),1)

    def test_repeated_submission_does_not_consume_twice(self):
        oid=self.order();self.approve(oid)
        fields=dict(order_id=str(oid),recipe_id=str(self.recipe),planned_qty='100',actual_qty='98',lot_code='V55-FG-1')
        self.post('/production',**fields)
        self.post('/production',**fields)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM batches'),1)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM consumptions'),2)
        self.assertEqual(self.scalar("SELECT qty FROM lots WHERE lot_code='V55-RAW-NEWER'"),50)

    def test_insufficient_stock_rolls_back(self):
        oid=self.order(qty=200);self.approve(oid)
        self.post('/production',order_id=str(oid),recipe_id=str(self.recipe),planned_qty='200',actual_qty='190',lot_code='V55-FG-1')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM batches'),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM consumptions'),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM movements'),0)
        self.assertEqual(self.scalar("SELECT SUM(qty) FROM lots WHERE kind='raw'"),150)
        self.assertEqual(self.scalar('SELECT status FROM work_orders WHERE id=?',(oid,)),'approved')

    def test_unapproved_order_and_csrf_rejected(self):
        oid=self.order()
        self.post('/production',order_id=str(oid),recipe_id=str(self.recipe),planned_qty='100',actual_qty='99',lot_code='V55-FG-1')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM batches'),0)
        r=self.client.post('/production',data={'recipe_id':self.recipe,'planned_qty':100,'actual_qty':99,'lot_code':'V55-FG-1'})
        self.assertEqual(r.status_code,400)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM batches'),0)

    def test_duplicate_reservation_not_added(self):
        oid=self.order();self.approve(oid)
        self.post('/work-orders',action='reserve',order_id=str(oid))
        self.post('/work-orders',action='reserve',order_id=str(oid))
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reservations WHERE order_id=?',(oid,)),2)
        self.assertEqual(self.scalar('SELECT SUM(qty) FROM reservations WHERE order_id=?',(oid,)),100)

    def test_cancellation_releases_reserve_without_consumption(self):
        oid=self.order();self.approve(oid)
        self.post('/work-orders',action='reserve',order_id=str(oid))
        self.post('/work-orders',action='cancel',order_id=str(oid))
        self.assertEqual(self.scalar('SELECT status FROM work_orders WHERE id=?',(oid,)),'cancelled')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reservations WHERE order_id=?',(oid,)),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM movements'),0)
        self.assertEqual(self.scalar("SELECT SUM(qty) FROM lots WHERE kind='raw'"),150)

def run():
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(ProductionFlowTests)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and not result.skipped else 1

if __name__=='__main__':raise SystemExit(run())
