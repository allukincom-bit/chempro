import os, tempfile, unittest
from datetime import date, timedelta

class LabelTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory()
  os.environ['CHEM_DB']=self.tmp.name+'/t.db'
  os.environ['SECRET_KEY']='x'*40
  os.environ['ADMIN_PASSWORD']='p'*16
  import app
  self.a=app
  app.DB=os.environ['CHEM_DB']
  app.init()
  self.client=app.app.test_client()
  with self.client.session_transaction() as s:
   s['uid']=1;s['username']='admin';s['role']='admin'
  with app.conn() as c:
   c.execute("INSERT INTO materials(name,unit,shelf_days) VALUES('Тестовое сырьё','кг',365)")
   c.execute("INSERT INTO lots(kind,material_id,lot_code,qty,manufactured,expires,status,supplier,created_at) VALUES('raw',1,'TEST-001',10,?,?, 'released','Тест',?)",(date.today().isoformat(),(date.today()+timedelta(days=100)).isoformat(),app.now()))
 def tearDown(self):self.tmp.cleanup()
 def test_index(self):self.assertEqual(self.client.get('/labels').status_code,200)
 def test_print_and_qr(self):
  r=self.client.get('/labels/1')
  self.assertEqual(r.status_code,200)
  self.assertIn(b'data:image/svg+xml;base64,',r.data)
  self.assertIn(b'TEST-001',r.data)
 def test_invalid_size(self):self.assertEqual(self.client.get('/labels/1?size=42x42').status_code,400)
 def test_missing_lot(self):self.assertEqual(self.client.get('/labels/999').status_code,404)
 def test_expired_priority(self):
  with self.a.conn() as c:c.execute("UPDATE lots SET expires='2020-01-01' WHERE id=1")
  self.assertEqual(self.a.label_state(dict(expires='2020-01-01',status='released'))[0],'expired')
  self.assertIn('ПРОСРОЧЕНО',self.client.get('/labels/1').get_data(as_text=True))
 def test_quarantine_not_warning(self):
  self.assertEqual(self.a.label_state(dict(expires=(date.today()+timedelta(days=2)).isoformat(),status='quarantine'))[0],'quarantine')
 def test_no_auth(self):
  c=self.a.app.test_client()
  self.assertEqual(c.get('/labels/1').status_code,302)

if __name__=='__main__':unittest.main()
