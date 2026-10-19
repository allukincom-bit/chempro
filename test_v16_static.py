from pathlib import Path
import ast
s=Path(__file__).with_name('app.py').read_text(encoding='utf-8')
ast.parse(s)
for token in ("@app.route('/reconciliation')", "stock_rows", "production_rows", "container_count", "consumptions"):
 assert token in s, token
print('v16 static checks OK')
