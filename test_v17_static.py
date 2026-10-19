from pathlib import Path
import ast
s=Path(__file__).with_name('app.py').read_text()
ast.parse(s)
for term in ('CREATE TABLE IF NOT EXISTS tech_parameters','CREATE TABLE IF NOT EXISTS production_sheets','CREATE TABLE IF NOT EXISTS sheet_readings',"@app.route('/tech-cards'","@app.route('/production/sheets'","@app.get('/tsd/production')",'measured_at','entered_at','snapshot_json'):
 assert term in s,term
print('v17 static checks OK')
