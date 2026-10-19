from pathlib import Path
import ast
src=Path(__file__).with_name('app.py').read_text()
ast.parse(src)
for fragment in ("@app.route('/tsd/issue'", "status='issued'", "status='consumed'", "CREATE TABLE IF NOT EXISTS tsd_issues", "Производственное задание", "UPDATE containers SET qty=qty-?"):
 assert fragment in src,fragment
print('v14 static OK')
