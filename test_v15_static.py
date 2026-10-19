import ast
from pathlib import Path
s=Path(__file__).with_name('app.py').read_text()
ast.parse(s)
for x in ("@app.route('/tsd/return'",'CREATE TABLE IF NOT EXISTS tsd_returns','BEGIN IMMEDIATE',"status='issued'",'issued_existing'):
 assert x in s,x
print('v15 static checks OK')
