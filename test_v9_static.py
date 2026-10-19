import ast, pathlib, unittest
class V9Static(unittest.TestCase):
 def test_routes_and_schema(self):
  text=pathlib.Path(__file__).with_name('app.py').read_text()
  ast.parse(text)
  for marker in ('CREATE TABLE IF NOT EXISTS containers(', 'CREATE TABLE IF NOT EXISTS label_print_jobs(', "@app.route('/containers'", "@app.post('/containers/print')", "@app.get('/containers/print-log')", 'CHEMPRO:CONTAINER:'):
   self.assertIn(marker,text)
if __name__=='__main__':unittest.main()
