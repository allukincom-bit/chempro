"""Isolated end-to-end HTTP regression tests for paper production sheets."""
import os, tempfile, unittest, json
from pathlib import Path
from datetime import datetime,date

class SheetLifecycleTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  try:
   import flask,qrcode
  except ImportError as exc:
   raise unittest.SkipTest('Install requirements.txt: '+str(exc))
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='lovetech-v57-');self.addCleanup(self.tmp.cleanup)
  self.db=Path(self.tmp.name)/'isolated.sqlite';os.environ['CHEM_DB']=str(self.db)
  import app as mod
  self.mod=mod;mod.DB=str(self.db);mod.app.config.update(TESTING=True,PROPAGATE_EXCEPTIONS=False,SECRET_KEY='v57-test-only')
  mod.init();self.client=mod.app.test_client()
  with mod.conn() as c:
   c.execute("INSERT INTO users(username,password,role) VALUES('v57_admin','unused','admin')")
   self.uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   c.execute("INSERT INTO products(name,unit) VALUES('V57 product','kg')")
   product=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   c.execute("INSERT INTO materials(name,unit) VALUES('V57 raw','kg')")
   material=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   c.execute("INSERT INTO recipes(product_id,version,status,notes,created_at) VALUES(?,1,'approved','',datetime('now'))",(product,))
   recipe=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   c.execute('INSERT INTO recipe_items(recipe_id,material_id,percent) VALUES(?,?,100)',(recipe,material))
   c.execute("INSERT INTO steps(recipe_id,position,instruction,control_point) VALUES(?,1,'Mix','Temperature')",(recipe,))
   step=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   c.execute("INSERT INTO tech_parameters(step_id,name,unit,min_value,max_value,required,method) VALUES(?,'Temperature','C',20,30,1,'Probe')",(step,))
   self.pid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   c.execute("INSERT INTO work_orders(recipe_id,lot_code,planned_qty,status,created_at) VALUES(?,'V57-WO',100,'approved',datetime('now'))",(recipe,))
   self.oid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
  with self.client.session_transaction() as s:s.update(uid=self.uid,username='v57_admin',role='admin',_csrf='v57-csrf')
  r=self.post('/production/sheets',order_id=str(self.oid))
  self.assertEqual(r.status_code,302)
  self.sid=self.one('SELECT id FROM production_sheets WHERE order_id=?',(self.oid,))
  self.url='/production/sheets/'+str(self.sid)
 def one(self,sql,args=()):
  with self.mod.conn() as c:
   row=c.execute(sql,args).fetchone()
   return row[0] if row else None
 def post(self,url,**data):return self.client.post(url,data={'_csrf':'v57-csrf',**data},follow_redirects=False)
 def reading(self,value='25',comment='',mode='after_shift'):
  return self.post(self.url,action='reading',parameter_id=str(self.pid),value=value,measured_at=datetime.now().strftime('%Y-%m-%dT%H:%M'),mode=mode,comment=comment)
 def paper(self):return self.post(self.url,action='paper',copy_number='A4-001',storage_location='Archive shelf')
 def review(self):return self.post(self.url,action='review')
 def close(self):return self.post(self.url,action='close',paper_checked='on')
 def test_issue_snapshot_and_print(self):
  self.assertEqual(self.one('SELECT status FROM production_sheets WHERE id=?',(self.sid,)),'issued')
  self.assertEqual(self.client.get(self.url).status_code,200)
  self.assertEqual(self.client.get(self.url+'/print').status_code,200)
  snapshot=json.loads(self.one('SELECT snapshot_json FROM production_sheets WHERE id=?',(self.sid,)))
  self.assertTrue(any(p['id']==self.pid for step in snapshot['steps'] for p in step['parameters']))
 def test_measurement_timestamps_and_close(self):
  self.assertEqual(self.reading().status_code,302)
  with self.mod.conn() as c:
   row=c.execute('SELECT measured_at,entered_at,mode,source FROM sheet_readings WHERE sheet_id=?',(self.sid,)).fetchone()
  self.assertTrue(row['measured_at']);self.assertTrue(row['entered_at']);self.assertEqual(row['mode'],'after_shift');self.assertEqual(row['source'],'paper')
  self.paper();self.review();self.close()
  self.assertEqual(self.one('SELECT status FROM production_sheets WHERE id=?',(self.sid,)),'closed')
  self.assertEqual(self.one('SELECT COUNT(*) FROM sheet_paper_register WHERE sheet_id=? AND verified_at IS NOT NULL',(self.sid,)),1)
 def test_required_measurement_blocks_close(self):
  self.paper();self.review();self.close()
  self.assertEqual(self.one('SELECT status FROM production_sheets WHERE id=?',(self.sid,)),'review')
 def test_missing_paper_blocks_review(self):
  self.review();self.assertEqual(self.one('SELECT status FROM production_sheets WHERE id=?',(self.sid,)),'issued')
 def test_deviation_must_be_accepted(self):
  self.reading('35','Above range');self.paper();self.review();self.close()
  self.assertEqual(self.one('SELECT status FROM production_sheets WHERE id=?',(self.sid,)),'review')
  rid=self.one('SELECT id FROM sheet_readings WHERE sheet_id=?',(self.sid,))
  self.post(self.url,action='disposition',reading_id=str(rid),choice='accepted',reason='Technologist authorized')
  self.close();self.assertEqual(self.one('SELECT status FROM production_sheets WHERE id=?',(self.sid,)),'closed')
 def test_rejected_deviation_blocks_close(self):
  self.reading('35','Above range');self.paper();self.review()
  rid=self.one('SELECT id FROM sheet_readings WHERE sheet_id=?',(self.sid,))
  self.post(self.url,action='disposition',reading_id=str(rid),choice='rejected',reason='Do not accept')
  self.close();self.assertEqual(self.one('SELECT status FROM production_sheets WHERE id=?',(self.sid,)),'review')
 def test_closed_sheet_immutable(self):
  self.reading();self.paper();self.review();self.close()
  before=self.one('SELECT COUNT(*) FROM sheet_readings WHERE sheet_id=?',(self.sid,))
  self.reading('26');self.assertEqual(self.one('SELECT COUNT(*) FROM sheet_readings WHERE sheet_id=?',(self.sid,)),before)
 def test_csrf_rejects_write(self):
  response=self.client.post(self.url,data={'action':'paper','copy_number':'X','storage_location':'Y'})
  self.assertEqual(response.status_code,400)
  self.assertEqual(self.one('SELECT COUNT(*) FROM sheet_paper_register WHERE sheet_id=?',(self.sid,)),0)
 def test_viewer_cannot_modify(self):
  with self.client.session_transaction() as s:s['role']='viewer'
  self.assertEqual(self.post(self.url,action='paper',copy_number='X',storage_location='Y').status_code,403)
  self.assertEqual(self.one('SELECT COUNT(*) FROM sheet_paper_register WHERE sheet_id=?',(self.sid,)),0)
 def test_repeated_paper_registration_does_not_duplicate(self):
  self.paper();self.paper();self.assertEqual(self.one('SELECT COUNT(*) FROM sheet_paper_register WHERE sheet_id=?',(self.sid,)),1)

if __name__=='__main__':
 suite=unittest.defaultTestLoader.loadTestsFromTestCase(SheetLifecycleTests)
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
