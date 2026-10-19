from pathlib import Path
import ast
s=Path(__file__).with_name('app.py').read_text()
ast.parse(s)
for token in ("@app.route('/scan'",'parse_container_scan','BarcodeDetector','getUserMedia',"'consume'",'CHEMPRO:CONTAINER:',"scan.consume"):
 assert token in s,token
print('v11 static checks OK')
