"""Offline smoke checks (do not replace HTTP integration tests)."""
import ast, pathlib, sqlite3, tempfile
root=pathlib.Path(__file__).parent
source=(root/'app.py').read_text()
ast.parse(source)
for name in ('suppliers','purchase_orders','purchase_receipts'):
    assert 'CREATE TABLE IF NOT EXISTS '+name in source
assert "@app.route('/procurement'" in source
assert "'quarantine'" in source
assert 'BEGIN IMMEDIATE' in source
print('Static checks OK')
