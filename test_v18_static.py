from pathlib import Path
import ast, unittest
S=Path(__file__).with_name('app.py').read_text()
class TestV18(unittest.TestCase):
 def test_syntax(self):ast.parse(S)
 def test_features(self):
  for x in ('sheet_paper_register','sheet_corrections',"action=='correct'","action=='paper'","action=='review'",'production_sheet.correct'):
   self.assertIn(x,S)
 def test_csrf_in_sheet_forms(self):
  section=S.split("@app.route('/tech-cards'")[1].split("@app.get('/production/sheets/<int:sheet_id>/print')")[0]
  self.assertNotIn("<form method='post'><input type='hidden' name='action'",section)
if __name__=='__main__':unittest.main()
