from pathlib import Path
import ast
source=Path(__file__).with_name('app.py').read_text()
ast.parse(source)
for marker in ('/tsd', 'tsd_requests', 'BEGIN IMMEDIATE', 'ScanWedge', 'executed_at'):
    assert marker in source, marker
print('v12 static checks OK')
