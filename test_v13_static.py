from pathlib import Path
import ast,unittest
class TestV13(unittest.TestCase):
 def test_syntax_routes_and_guards(self):
  s=Path(__file__).with_name('app.py').read_text()
  ast.parse(s)
  for x in ('/tsd/receive','/tsd/pick','CREATE TABLE IF NOT EXISTS tsd_picks','BEGIN IMMEDIATE','parse_location_scan','request_key','picked_lot','picked_ct'):
   self.assertIn(x,s)
if __name__=='__main__':unittest.main()
