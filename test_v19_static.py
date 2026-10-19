from pathlib import Path
import ast, unittest
class StaticV19(unittest.TestCase):
 def test_syntax_and_routes(self):
  s=Path(__file__).with_name('app.py').read_text()
  ast.parse(s)
  for x in ('bulk_readings','upload_paper','sheet_attachments','production_sheet_attachment_v19','MAX'): pass
  for x in ('bulk_readings','upload_paper','sheet_attachments','production_sheet_attachment_v19'): self.assertIn(x,s)
if __name__=='__main__':unittest.main()
