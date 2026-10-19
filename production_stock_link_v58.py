"""Isolated cross-module regression: paper sheet + CT48 + accounting + finished lot."""
import json, unittest, uuid
from datetime import datetime
from tsd_flow_v56 import CT48FlowTests

class ProductionStockLinkTests(CT48FlowTests):
    """Inherited CT48 checks plus cross-module assertions, all on disposable SQLite."""
    def prepare(self):
        oid=self.start()
        self.assertEqual(self.register().status_code,302)
        self.assertEqual(self.pick(oid).status_code,302)
        self.assertEqual(self.issue(oid).status_code,302)
        self.sheet(oid)
        sid=self.val('SELECT id FROM production_sheets WHERE order_id=?',(oid,))
        return oid,sid

    def finish(self,oid):
        return self.post('/production',order_id=str(oid),recipe_id=str(self.rid),planned_qty='60',actual_qty='57',lot_code='V56-FG')

    def test_sheet_load_and_issue_reconcile(self):
        oid,sid=self.prepare()
        self.load(oid,qty='60')
        with self.mod.conn() as c:
            report=self.mod.v24_reconcile_order(c,oid)
        self.assertEqual(report['issue_count'],0)
        row=report['rows'][0]
        self.assertEqual((row['norm'],row['issued'],row['actual']),(60,60,60))
        self.assertEqual(self.val('SELECT COUNT(*) FROM material_loads_v23 WHERE sheet_id=?',(sid,)),1)
        self.assertEqual(self.val('SELECT qty FROM lots WHERE id=?',(self.lid,)),100)

    def test_partial_load_report_detects_difference_without_mutation(self):
        oid,_=self.prepare();self.load(oid,qty='50')
        with self.mod.conn() as c:report=self.mod.v24_reconcile_order(c,oid)
        self.assertGreater(report['issue_count'],0)
        self.assertEqual(report['rows'][0]['delta_norm'],-10)
        self.assertEqual(self.val('SELECT qty FROM lots WHERE id=?',(self.lid,)),100)
        self.assertEqual(self.val('SELECT COUNT(*) FROM consumptions'),0)

    def test_finished_lot_consumes_once_and_preserves_loading_history(self):
        oid,sid=self.prepare();self.load(oid,qty='60')
        self.assertEqual(self.finish(oid).status_code,302)
        self.assertEqual(self.val('SELECT status FROM work_orders WHERE id=?',(oid,)),'completed')
        self.assertEqual(self.val('SELECT qty FROM lots WHERE id=?',(self.lid,)),40)
        self.assertEqual(self.val("SELECT qty FROM lots WHERE lot_code='V56-FG'"),57)
        self.assertEqual(self.val('SELECT COUNT(*) FROM consumptions'),1)
        self.assertEqual(self.val('SELECT COUNT(*) FROM movements'),2)
        self.assertEqual(self.val('SELECT COUNT(*) FROM material_loads_v23 WHERE sheet_id=?',(sid,)),1)
        self.assertEqual(self.val("SELECT COUNT(*) FROM tsd_picks WHERE status='consumed'"),1)
        self.assertEqual(self.val('SELECT qty FROM containers WHERE lot_id=?',(self.lid,)),40)

    def test_repeated_finish_does_not_double_consume(self):
        oid,_=self.prepare();self.load(oid,qty='60')
        self.finish(oid);self.finish(oid)
        self.assertEqual(self.val('SELECT COUNT(*) FROM batches'),1)
        self.assertEqual(self.val('SELECT COUNT(*) FROM consumptions'),1)
        self.assertEqual(self.val('SELECT qty FROM lots WHERE id=?',(self.lid,)),40)

    def test_staged_container_blocks_release_atomically(self):
        oid=self.start();self.register();self.pick(oid);self.sheet(oid)
        self.finish(oid)
        self.assertEqual(self.val('SELECT COUNT(*) FROM batches'),0)
        self.assertEqual(self.val('SELECT COUNT(*) FROM consumptions'),0)
        self.assertEqual(self.val('SELECT qty FROM lots WHERE id=?',(self.lid,)),100)

    def test_wrong_container_does_not_add_load(self):
        oid,_=self.prepare()
        self.load(oid,qty='60',code='NONEXISTENT')
        self.assertEqual(self.val('SELECT COUNT(*) FROM material_loads_v23'),0)
        self.assertEqual(self.val('SELECT qty FROM lots WHERE id=?',(self.lid,)),100)

    def test_duplicate_loading_key_does_not_change_balance(self):
        oid,_=self.prepare();key=uuid.uuid4().hex
        self.load(oid,qty='30',key=key);self.load(oid,qty='30',key=key)
        self.assertEqual(self.val('SELECT COUNT(*) FROM material_loads_v23'),1)
        self.assertEqual(self.val('SELECT SUM(qty) FROM material_loads_v23'),30)
        self.assertEqual(self.val('SELECT qty FROM lots WHERE id=?',(self.lid,)),100)

    def test_production_csrf_cannot_change_stock(self):
        oid,_=self.prepare();self.load(oid,qty='60')
        response=self.client.post('/production',data={'order_id':oid,'recipe_id':self.rid,'planned_qty':60,'actual_qty':57,'lot_code':'V56-FG'})
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.val('SELECT COUNT(*) FROM batches'),0)
        self.assertEqual(self.val('SELECT qty FROM lots WHERE id=?',(self.lid,)),100)

def main():
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(ProductionStockLinkTests)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and not result.skipped else 1

if __name__=='__main__':raise SystemExit(main())
