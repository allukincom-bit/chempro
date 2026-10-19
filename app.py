import os, sqlite3, secrets, base64, io, uuid, json, math, hashlib
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, request, redirect, url_for, session, flash, render_template_string, abort, send_file
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode
from urllib.parse import quote
from qrcode.image.svg import SvgPathImage

app=Flask(__name__)
app.secret_key=os.environ.get('SECRET_KEY',secrets.token_hex(32))
DB=os.environ.get('CHEM_DB','/app/data/chem.db')
@app.before_request
def csrf_guard():
 if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
  token = session.get('_csrf')
  supplied = request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
  if not token or not supplied or not secrets.compare_digest(token, supplied):
   abort(400, description='CSRF validation failed')

@app.context_processor
def csrf_context():
 if '_csrf' not in session:
  session['_csrf'] = secrets.token_urlsafe(32)
 return {'csrf_token': session['_csrf']}

@app.get('/health')
def health():
 return {'status': 'ok'}

ROLES={'admin':'Администратор','technologist':'Технолог','warehouse':'Кладовщик','economist':'Экономист','operator':'Оператор','quality':'ОТК','viewer':'Просмотр'}

def conn():
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c

def init():
 with conn() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS materials(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,unit TEXT NOT NULL DEFAULT 'кг',shelf_days INTEGER NOT NULL DEFAULT 365);
  CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,unit TEXT NOT NULL DEFAULT 'кг',shelf_days INTEGER NOT NULL DEFAULT 365);
  CREATE TABLE IF NOT EXISTS recipes(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id),version INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'draft',notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL, UNIQUE(product_id,version));
  CREATE TABLE IF NOT EXISTS recipe_items(id INTEGER PRIMARY KEY,recipe_id INTEGER NOT NULL REFERENCES recipes(id),material_id INTEGER NOT NULL REFERENCES materials(id),percent REAL NOT NULL CHECK(percent>0 AND percent<=100),UNIQUE(recipe_id,material_id));
  CREATE TABLE IF NOT EXISTS steps(id INTEGER PRIMARY KEY,recipe_id INTEGER NOT NULL REFERENCES recipes(id),position INTEGER NOT NULL,instruction TEXT NOT NULL,control_point TEXT NOT NULL DEFAULT '');
  CREATE TABLE IF NOT EXISTS lots(id INTEGER PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('raw','finished')),material_id INTEGER REFERENCES materials(id),product_id INTEGER REFERENCES products(id),lot_code TEXT UNIQUE NOT NULL,qty REAL NOT NULL CHECK(qty>=0),manufactured TEXT NOT NULL,expires TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'quarantine',supplier TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL, CHECK((kind='raw' AND material_id IS NOT NULL AND product_id IS NULL) OR (kind='finished' AND product_id IS NOT NULL AND material_id IS NULL)));
  CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY,recipe_id INTEGER NOT NULL REFERENCES recipes(id),lot_id INTEGER UNIQUE NOT NULL REFERENCES lots(id),planned_qty REAL NOT NULL,actual_qty REAL NOT NULL,created_at TEXT NOT NULL,operator_id INTEGER NOT NULL REFERENCES users(id));
  CREATE TABLE IF NOT EXISTS consumptions(id INTEGER PRIMARY KEY,batch_id INTEGER NOT NULL REFERENCES batches(id),raw_lot_id INTEGER NOT NULL REFERENCES lots(id),qty REAL NOT NULL CHECK(qty>0));
  CREATE TABLE IF NOT EXISTS movements(id INTEGER PRIMARY KEY,lot_id INTEGER NOT NULL REFERENCES lots(id),delta REAL NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id));
  CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor_id INTEGER,action TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS shipments(id INTEGER PRIMARY KEY,lot_id INTEGER NOT NULL REFERENCES lots(id),customer TEXT NOT NULL,qty REAL NOT NULL CHECK(qty>0),created_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id));''')
  c.executescript("""CREATE TABLE IF NOT EXISTS qc_specs(id INTEGER PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('raw','finished')),item_id INTEGER NOT NULL,parameter TEXT NOT NULL,unit TEXT NOT NULL DEFAULT '',min_value REAL,max_value REAL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,UNIQUE(kind,item_id,parameter),CHECK(min_value IS NOT NULL OR max_value IS NOT NULL),CHECK(min_value IS NULL OR max_value IS NULL OR min_value<=max_value));
  CREATE TABLE IF NOT EXISTS qc_results(id INTEGER PRIMARY KEY,lot_id INTEGER NOT NULL REFERENCES lots(id),spec_id INTEGER NOT NULL REFERENCES qc_specs(id),value REAL NOT NULL,passed INTEGER NOT NULL CHECK(passed IN (0,1)),recorded_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),comment TEXT NOT NULL DEFAULT '');
  CREATE INDEX IF NOT EXISTS idx_qc_results_lot_spec ON qc_results(lot_id,spec_id,id);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS qc_samples(
    id INTEGER PRIMARY KEY, sample_code TEXT NOT NULL UNIQUE,
    lot_id INTEGER NOT NULL REFERENCES lots(id), sampled_at TEXT NOT NULL,
    registered_at TEXT NOT NULL, registered_by INTEGER NOT NULL REFERENCES users(id),
    sampled_by TEXT NOT NULL, location TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','reviewed','rejected')),
    reviewed_at TEXT, reviewed_by INTEGER REFERENCES users(id), review_note TEXT NOT NULL DEFAULT '');
  CREATE INDEX IF NOT EXISTS idx_qc_samples_lot ON qc_samples(lot_id,id);
  CREATE TABLE IF NOT EXISTS qc_sample_results(
    id INTEGER PRIMARY KEY, sample_id INTEGER NOT NULL REFERENCES qc_samples(id),
    spec_id INTEGER NOT NULL REFERENCES qc_specs(id), value REAL NOT NULL,
    passed INTEGER NOT NULL CHECK(passed IN (0,1)),
    recorded_at TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
    note TEXT NOT NULL DEFAULT '', UNIQUE(sample_id,spec_id));""")

  c.executescript("""CREATE TABLE IF NOT EXISTS quality_passports(
    id INTEGER PRIMARY KEY, passport_no TEXT NOT NULL UNIQUE,
    lot_id INTEGER NOT NULL REFERENCES lots(id), sample_id INTEGER NOT NULL REFERENCES qc_samples(id),
    issued_at TEXT NOT NULL, issued_by INTEGER NOT NULL REFERENCES users(id),
    snapshot_json TEXT NOT NULL, UNIQUE(lot_id,sample_id));
    CREATE INDEX IF NOT EXISTS idx_quality_passports_lot ON quality_passports(lot_id,id);""")

  c.executescript("""CREATE TABLE IF NOT EXISTS packing_shipments_v28(
    id INTEGER PRIMARY KEY, request_token TEXT NOT NULL UNIQUE,
    lot_id INTEGER NOT NULL REFERENCES lots(id), customer TEXT NOT NULL,
    qty REAL NOT NULL CHECK(qty>0), shipment_id INTEGER NOT NULL UNIQUE REFERENCES shipments(id),
    created_at TEXT NOT NULL, actor_id INTEGER NOT NULL REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS packing_shipment_items_v28(
    shipment_id INTEGER NOT NULL REFERENCES packing_shipments_v28(id),
    container_id INTEGER NOT NULL REFERENCES containers(id),
    qty REAL NOT NULL CHECK(qty>0), PRIMARY KEY(shipment_id,container_id));
    CREATE INDEX IF NOT EXISTS idx_packing_shipments_lot_v28 ON packing_shipments_v28(lot_id,id);""")

  c.executescript("""CREATE TABLE IF NOT EXISTS dispatch_tasks_v31(
    id INTEGER PRIMARY KEY, customer TEXT NOT NULL, vehicle TEXT NOT NULL DEFAULT '', driver TEXT NOT NULL DEFAULT '',
    due_at TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'new'
    CHECK(status IN ('new','assigned','shipped','completed','cancelled')),
    created_at TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id),
    assigned_at TEXT, assigned_by INTEGER REFERENCES users(id),
    shipped_at TEXT, shipment_id INTEGER UNIQUE REFERENCES packing_shipments_v28(id),
    completed_at TEXT, completed_by INTEGER REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS dispatch_task_lines_v31(
    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES dispatch_tasks_v31(id),
    lot_id INTEGER NOT NULL REFERENCES lots(id), qty REAL NOT NULL CHECK(qty>0),
    UNIQUE(task_id,lot_id));
    CREATE TABLE IF NOT EXISTS dispatch_photos_v31(
    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES dispatch_tasks_v31(id),
    mime TEXT NOT NULL, photo BLOB NOT NULL, sha256 TEXT NOT NULL,
    caption TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id));
    CREATE INDEX IF NOT EXISTS idx_dispatch_photos_task_v31 ON dispatch_photos_v31(task_id,id);""")

  c.executescript("""CREATE TABLE IF NOT EXISTS dispatch_scans_v32(
    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES dispatch_tasks_v31(id),
    line_id INTEGER NOT NULL REFERENCES dispatch_task_lines_v31(id),
    shipment_id INTEGER NOT NULL REFERENCES packing_shipments_v28(id),
    container_id INTEGER NOT NULL REFERENCES containers(id),
    qty REAL NOT NULL CHECK(qty>0), created_at TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    UNIQUE(task_id,container_id), UNIQUE(shipment_id,container_id));
    CREATE INDEX IF NOT EXISTS idx_dispatch_scans_v32_task ON dispatch_scans_v32(task_id,line_id);
    CREATE TABLE IF NOT EXISTS dispatch_receipts_v32(
    task_id INTEGER PRIMARY KEY REFERENCES dispatch_tasks_v31(id),
    closed_at TEXT NOT NULL, closed_by INTEGER NOT NULL REFERENCES users(id),
    snapshot_json TEXT NOT NULL);""")

  c.executescript("""CREATE TABLE IF NOT EXISTS dispatch_reviews_v39(
    task_id INTEGER PRIMARY KEY REFERENCES dispatch_tasks_v31(id),
    status TEXT NOT NULL CHECK(status IN ('pending','changes_requested','approved')),
    submitted_at TEXT NOT NULL, submitted_by INTEGER NOT NULL REFERENCES users(id),
    reviewed_at TEXT, reviewed_by INTEGER REFERENCES users(id),
    comment TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS dispatch_review_events_v39(
    id INTEGER PRIMARY KEY,task_id INTEGER NOT NULL REFERENCES dispatch_tasks_v31(id),
    action TEXT NOT NULL,actor_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,comment TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_dispatch_review_events_task_v39 ON dispatch_review_events_v39(task_id,id);""")

  c.executescript("""CREATE TABLE IF NOT EXISTS customer_returns_v29(
    id INTEGER PRIMARY KEY, request_token TEXT NOT NULL UNIQUE,
    shipment_id INTEGER NOT NULL REFERENCES packing_shipments_v28(id),
    container_id INTEGER NOT NULL REFERENCES containers(id),
    lot_id INTEGER NOT NULL REFERENCES lots(id), qty REAL NOT NULL CHECK(qty>0),
    reason TEXT NOT NULL, customer TEXT NOT NULL, registered_at TEXT NOT NULL,
    registered_by INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'quarantine' CHECK(status IN ('quarantine','released','rejected')),
    decision_note TEXT NOT NULL DEFAULT '', decided_at TEXT,
    decided_by INTEGER REFERENCES users(id), UNIQUE(container_id));
    CREATE INDEX IF NOT EXISTS idx_customer_returns_v29_status ON customer_returns_v29(status,id);""")

  c.executescript("""CREATE TABLE IF NOT EXISTS return_disposal_acts_v30(
    id INTEGER PRIMARY KEY, act_no TEXT NOT NULL UNIQUE,
    return_id INTEGER NOT NULL UNIQUE REFERENCES customer_returns_v29(id),
    container_id INTEGER NOT NULL REFERENCES containers(id),
    lot_id INTEGER NOT NULL REFERENCES lots(id), qty REAL NOT NULL CHECK(qty>0),
    method TEXT NOT NULL, contractor TEXT NOT NULL DEFAULT '',
    document_ref TEXT NOT NULL DEFAULT '', note TEXT NOT NULL,
    created_at TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id));
    CREATE INDEX IF NOT EXISTS idx_return_disposal_lot_v30 ON return_disposal_acts_v30(lot_id,id);""")

  c.executescript("""CREATE TABLE IF NOT EXISTS finished_packing_jobs(
    id INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL REFERENCES lots(id),
    request_token TEXT NOT NULL UNIQUE, pack_type TEXT NOT NULL,
    container_count INTEGER NOT NULL CHECK(container_count BETWEEN 1 AND 100),
    unit_qty REAL NOT NULL CHECK(unit_qty>0), total_qty REAL NOT NULL CHECK(total_qty>0),
    code_prefix TEXT NOT NULL, created_at TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS finished_packing_items(
    job_id INTEGER NOT NULL REFERENCES finished_packing_jobs(id),
    container_id INTEGER NOT NULL UNIQUE REFERENCES containers(id),
    PRIMARY KEY(job_id,container_id));
    CREATE INDEX IF NOT EXISTS idx_finished_packing_lot ON finished_packing_jobs(lot_id,id);""")

  c.executescript("""CREATE TABLE IF NOT EXISTS work_orders(
    id INTEGER PRIMARY KEY,recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    planned_qty REAL NOT NULL CHECK(planned_qty>0),lot_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved','completed','cancelled')),
    due_date TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),approved_by INTEGER REFERENCES users(id),
    batch_id INTEGER UNIQUE REFERENCES batches(id));
    CREATE INDEX IF NOT EXISTS idx_work_orders_status_due ON work_orders(status,due_date);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS reservations(
   id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES work_orders(id),
   lot_id INTEGER NOT NULL REFERENCES lots(id),qty REAL NOT NULL CHECK(qty>0),
   created_at TEXT NOT NULL, UNIQUE(order_id,lot_id));
   CREATE INDEX IF NOT EXISTS idx_reservations_lot ON reservations(lot_id);
   CREATE TABLE IF NOT EXISTS purchase_requests(
   id INTEGER PRIMARY KEY,material_id INTEGER NOT NULL REFERENCES materials(id),
   qty REAL NOT NULL CHECK(qty>0),needed_by TEXT NOT NULL,
   status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','ordered','closed','cancelled')),
   note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
   created_by INTEGER NOT NULL REFERENCES users(id));""")
  c.executescript("""CREATE TABLE IF NOT EXISTS suppliers(
   id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,contact TEXT NOT NULL DEFAULT '',
   email TEXT NOT NULL DEFAULT '',phone TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1);
   CREATE TABLE IF NOT EXISTS purchase_orders(
   id INTEGER PRIMARY KEY,request_id INTEGER REFERENCES purchase_requests(id),
   supplier_id INTEGER NOT NULL REFERENCES suppliers(id),material_id INTEGER NOT NULL REFERENCES materials(id),
   qty REAL NOT NULL CHECK(qty>0),received_qty REAL NOT NULL DEFAULT 0 CHECK(received_qty>=0),
   due_date TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'ordered' CHECK(status IN ('ordered','partial','received','cancelled')),
   created_at TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id),note TEXT NOT NULL DEFAULT '');
   CREATE TABLE IF NOT EXISTS purchase_receipts(
   id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES purchase_orders(id),
   lot_id INTEGER NOT NULL UNIQUE REFERENCES lots(id),qty REAL NOT NULL CHECK(qty>0),
   received_at TEXT NOT NULL,received_by INTEGER NOT NULL REFERENCES users(id));
   CREATE INDEX IF NOT EXISTS idx_purchase_orders_supplier ON purchase_orders(supplier_id,status);
   CREATE INDEX IF NOT EXISTS idx_purchase_receipts_order ON purchase_receipts(order_id);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS containers(
    id INTEGER PRIMARY KEY,lot_id INTEGER NOT NULL REFERENCES lots(id),
    code TEXT NOT NULL UNIQUE,qty REAL NOT NULL CHECK(qty>0),
    created_at TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id),
    note TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)));
    CREATE INDEX IF NOT EXISTS idx_containers_lot ON containers(lot_id,active);
    CREATE TABLE IF NOT EXISTS label_print_jobs(
    id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),
    format TEXT NOT NULL,mode TEXT NOT NULL CHECK(mode IN ('lot','container','batch')),
    label_count INTEGER NOT NULL CHECK(label_count>0),details TEXT NOT NULL);
    """)
  c.executescript("""CREATE TABLE IF NOT EXISTS container_operations(
    id INTEGER PRIMARY KEY,container_id INTEGER NOT NULL REFERENCES containers(id),
    operation TEXT NOT NULL CHECK(operation IN ('transfer','count','consume')),
    before_qty REAL NOT NULL,after_qty REAL NOT NULL,
    from_location TEXT NOT NULL,to_location TEXT NOT NULL,
    created_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),
    note TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS idx_container_operations_container ON container_operations(container_id,id);
    CREATE TABLE IF NOT EXISTS container_locations(
    container_id INTEGER PRIMARY KEY REFERENCES containers(id),location TEXT NOT NULL DEFAULT 'Основной склад');
    """)
  c.executescript("""CREATE TABLE IF NOT EXISTS tsd_requests(
    token TEXT PRIMARY KEY,container_id INTEGER NOT NULL REFERENCES containers(id),
    operation TEXT NOT NULL CHECK(operation IN ('transfer','consume')),
    created_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),
    executed_at TEXT,details TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS idx_tsd_requests_user ON tsd_requests(user_id,created_at);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS tsd_picks(
   id INTEGER PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
   order_id INTEGER NOT NULL REFERENCES work_orders(id),
   container_id INTEGER NOT NULL REFERENCES containers(id),
   qty REAL NOT NULL CHECK(qty>0), location TEXT NOT NULL,
   created_at TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
   status TEXT NOT NULL DEFAULT 'staged' CHECK(status IN ('staged','cancelled','issued','consumed')));
   CREATE INDEX IF NOT EXISTS idx_tsd_picks_order ON tsd_picks(order_id,status);
   CREATE INDEX IF NOT EXISTS idx_tsd_picks_container ON tsd_picks(container_id,status);
  """)
  # v14: SQLite CHECK migration for existing v13 databases.
  pick_sql=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tsd_picks'").fetchone()[0]
  if "'issued'" not in pick_sql:
   c.execute('ALTER TABLE tsd_picks RENAME TO tsd_picks_v13')
   c.executescript("""CREATE TABLE tsd_picks(
    id INTEGER PRIMARY KEY,request_key TEXT NOT NULL UNIQUE,
    order_id INTEGER NOT NULL REFERENCES work_orders(id),
    container_id INTEGER NOT NULL REFERENCES containers(id),
    qty REAL NOT NULL CHECK(qty>0),location TEXT NOT NULL,
    created_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'staged' CHECK(status IN ('staged','cancelled','issued','consumed')));
    INSERT INTO tsd_picks SELECT * FROM tsd_picks_v13;
    DROP TABLE tsd_picks_v13;
    CREATE INDEX IF NOT EXISTS idx_tsd_picks_order ON tsd_picks(order_id,status);
    CREATE INDEX IF NOT EXISTS idx_tsd_picks_container ON tsd_picks(container_id,status);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS tsd_issues(
   request_key TEXT PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES work_orders(id),
   created_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id));""")
  c.executescript("""CREATE TABLE IF NOT EXISTS tsd_returns(
    id INTEGER PRIMARY KEY,request_key TEXT NOT NULL UNIQUE,
    pick_id INTEGER NOT NULL REFERENCES tsd_picks(id),
    order_id INTEGER NOT NULL REFERENCES work_orders(id),
    container_id INTEGER NOT NULL REFERENCES containers(id),
    qty REAL NOT NULL CHECK(qty>0),created_at TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),note TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS idx_tsd_returns_order ON tsd_returns(order_id,id);""")
  # v11: migrate the v10 operation constraint without losing recorded operations.
  op_sql=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='container_operations'").fetchone()[0]
  if "'consume'" not in op_sql:
   c.execute('ALTER TABLE container_operations RENAME TO container_operations_v10')
   c.executescript("""CREATE TABLE container_operations(
    id INTEGER PRIMARY KEY,container_id INTEGER NOT NULL REFERENCES containers(id),
    operation TEXT NOT NULL CHECK(operation IN ('transfer','count','consume')),
    before_qty REAL NOT NULL,after_qty REAL NOT NULL,
    from_location TEXT NOT NULL,to_location TEXT NOT NULL,
    created_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),
    note TEXT NOT NULL DEFAULT '');
    INSERT INTO container_operations SELECT * FROM container_operations_v10;
    DROP TABLE container_operations_v10;
    CREATE INDEX IF NOT EXISTS idx_container_operations_container ON container_operations(container_id,id);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS tech_parameters(
    id INTEGER PRIMARY KEY,step_id INTEGER NOT NULL REFERENCES steps(id),name TEXT NOT NULL,unit TEXT NOT NULL DEFAULT '',
    min_value REAL,max_value REAL,required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
    method TEXT NOT NULL DEFAULT '',CHECK(min_value IS NULL OR max_value IS NULL OR min_value<=max_value));
    CREATE TABLE IF NOT EXISTS production_sheets(
    id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL UNIQUE REFERENCES work_orders(id),
    snapshot_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'issued' CHECK(status IN ('issued','review','closed')),
    issued_at TEXT NOT NULL,issued_by INTEGER NOT NULL REFERENCES users(id),reviewed_at TEXT,reviewed_by INTEGER REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS sheet_readings(
    id INTEGER PRIMARY KEY,sheet_id INTEGER NOT NULL REFERENCES production_sheets(id),
    parameter_id INTEGER NOT NULL,step_id INTEGER NOT NULL,value REAL,measured_at TEXT NOT NULL,
    entered_at TEXT NOT NULL,entered_by INTEGER NOT NULL REFERENCES users(id),mode TEXT NOT NULL CHECK(mode IN ('live','after_shift')),
    source TEXT NOT NULL,comment TEXT NOT NULL DEFAULT '',out_of_range INTEGER NOT NULL CHECK(out_of_range IN (0,1)),
    disposition TEXT NOT NULL DEFAULT 'pending' CHECK(disposition IN ('pending','accepted','rejected')),
    disposition_by INTEGER REFERENCES users(id),disposition_at TEXT,disposition_reason TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS idx_sheet_readings_sheet ON sheet_readings(sheet_id,parameter_id,id);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS sheet_paper_register(
    sheet_id INTEGER PRIMARY KEY REFERENCES production_sheets(id),
    copy_number TEXT NOT NULL,received_at TEXT NOT NULL,received_by INTEGER NOT NULL REFERENCES users(id),
    storage_location TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',
    verified_at TEXT,verified_by INTEGER REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS sheet_corrections(
    id INTEGER PRIMARY KEY,sheet_id INTEGER NOT NULL REFERENCES production_sheets(id),
    old_reading_id INTEGER NOT NULL UNIQUE REFERENCES sheet_readings(id),
    new_reading_id INTEGER NOT NULL UNIQUE REFERENCES sheet_readings(id),
    reason TEXT NOT NULL,created_at TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id));
    CREATE INDEX IF NOT EXISTS idx_sheet_corrections_sheet ON sheet_corrections(sheet_id);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS sheet_attachments(
    id INTEGER PRIMARY KEY,sheet_id INTEGER NOT NULL REFERENCES production_sheets(id),
    filename TEXT NOT NULL,mime TEXT NOT NULL,sha256 TEXT NOT NULL,
    payload BLOB NOT NULL,uploaded_at TEXT NOT NULL,
    uploaded_by INTEGER NOT NULL REFERENCES users(id));
    CREATE INDEX IF NOT EXISTS idx_sheet_attachments_sheet ON sheet_attachments(sheet_id,id);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS shift_assignments(
    id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL UNIQUE REFERENCES work_orders(id),
    shift_date TEXT NOT NULL,shift_name TEXT NOT NULL DEFAULT 'Дневная',
    operator_id INTEGER NOT NULL REFERENCES users(id),
    planned_start TEXT NOT NULL,planned_end TEXT NOT NULL,
    actual_start TEXT,actual_end TEXT,
    note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id),
    CHECK(planned_end>planned_start),CHECK(actual_end IS NULL OR actual_start IS NOT NULL),
    CHECK(actual_end IS NULL OR actual_end>=actual_start));
    CREATE INDEX IF NOT EXISTS idx_shift_assignments_day ON shift_assignments(shift_date,planned_start);
    CREATE TABLE IF NOT EXISTS shift_events(
    id INTEGER PRIMARY KEY,assignment_id INTEGER NOT NULL REFERENCES shift_assignments(id),
    event TEXT NOT NULL CHECK(event IN ('plan','start','finish','edit')),
    occurred_at TEXT NOT NULL,actor_id INTEGER NOT NULL REFERENCES users(id),details TEXT NOT NULL DEFAULT '');""")
  c.executescript("""CREATE TABLE IF NOT EXISTS material_loads_v23(
    id INTEGER PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
    order_id INTEGER NOT NULL REFERENCES work_orders(id),
    sheet_id INTEGER NOT NULL REFERENCES production_sheets(id),
    container_id INTEGER NOT NULL REFERENCES containers(id),
    material_id INTEGER NOT NULL REFERENCES materials(id),
    qty REAL NOT NULL CHECK(qty>0),norm_qty REAL NOT NULL CHECK(norm_qty>0),
    tolerance_percent REAL NOT NULL CHECK(tolerance_percent>=0),
    measured_at TEXT NOT NULL,entered_at TEXT NOT NULL,
    entered_by INTEGER NOT NULL REFERENCES users(id),
    entry_mode TEXT NOT NULL CHECK(entry_mode IN ('live','after_shift')),
    note TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS idx_material_loads_v23_order ON material_loads_v23(order_id,material_id);
    CREATE INDEX IF NOT EXISTS idx_material_loads_v23_container ON material_loads_v23(container_id);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS shift_stage_plans(
    id INTEGER PRIMARY KEY,assignment_id INTEGER NOT NULL REFERENCES shift_assignments(id),
    step_position INTEGER NOT NULL,step_instruction TEXT NOT NULL,
    planned_start TEXT NOT NULL,planned_end TEXT NOT NULL,
    actual_start TEXT,actual_end TEXT,notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id),
    UNIQUE(assignment_id,step_position),CHECK(planned_end>planned_start),
    CHECK(actual_end IS NULL OR actual_start IS NOT NULL));
    CREATE INDEX IF NOT EXISTS idx_shift_stage_assignment ON shift_stage_plans(assignment_id,step_position);
    CREATE TABLE IF NOT EXISTS shift_stage_events(
    id INTEGER PRIMARY KEY,stage_id INTEGER NOT NULL REFERENCES shift_stage_plans(id),
    event TEXT NOT NULL CHECK(event IN ('planned','started','finished')),
    happened_at TEXT NOT NULL,actor_id INTEGER NOT NULL REFERENCES users(id),
    note TEXT NOT NULL DEFAULT '');""")
  c.executescript("""CREATE TABLE IF NOT EXISTS permission_overrides_v35(
    subject_type TEXT NOT NULL CHECK(subject_type IN ('role','user')),
    subject_id TEXT NOT NULL, permission TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    updated_at TEXT NOT NULL, updated_by INTEGER REFERENCES users(id),
    PRIMARY KEY(subject_type,subject_id,permission));
    CREATE INDEX IF NOT EXISTS idx_permission_subject_v35 ON permission_overrides_v35(subject_type,subject_id);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS inventory_sessions_v45(id INTEGER PRIMARY KEY,title TEXT NOT NULL,location TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','review','closed')),created_at TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id),submitted_at TEXT,closed_at TEXT,closed_by INTEGER REFERENCES users(id),note TEXT NOT NULL DEFAULT '');CREATE TABLE IF NOT EXISTS inventory_counts_v45(id INTEGER PRIMARY KEY,session_id INTEGER NOT NULL REFERENCES inventory_sessions_v45(id),container_id INTEGER NOT NULL REFERENCES containers(id),expected_qty REAL NOT NULL,actual_qty REAL NOT NULL CHECK(actual_qty>=0),location TEXT NOT NULL,counted_at TEXT NOT NULL,counted_by INTEGER NOT NULL REFERENCES users(id),note TEXT NOT NULL DEFAULT '',UNIQUE(session_id,container_id));CREATE INDEX IF NOT EXISTS idx_inventory_counts_v45_session ON inventory_counts_v45(session_id);""")
  c.execute('''CREATE TABLE IF NOT EXISTS user_menu_v34(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,path TEXT NOT NULL,visible INTEGER NOT NULL CHECK(visible IN (0,1)),PRIMARY KEY(user_id,path))''')
  c.execute('''CREATE TABLE IF NOT EXISTS user_roles_v33(
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL, PRIMARY KEY(user_id,role))''')
  c.executescript("""CREATE TABLE IF NOT EXISTS notification_reads_v46(
   user_id INTEGER NOT NULL REFERENCES users(id),event_key TEXT NOT NULL,
   read_at TEXT NOT NULL,PRIMARY KEY(user_id,event_key));
   CREATE INDEX IF NOT EXISTS idx_notification_reads_v46_user ON notification_reads_v46(user_id);""")
  c.executescript("""CREATE TABLE IF NOT EXISTS cost_prices_v47(
   material_id INTEGER PRIMARY KEY REFERENCES materials(id),price_per_unit REAL NOT NULL CHECK(price_per_unit>=0),
   currency TEXT NOT NULL DEFAULT 'RUB',updated_at TEXT NOT NULL,updated_by INTEGER NOT NULL REFERENCES users(id));
   CREATE TABLE IF NOT EXISTS batch_costs_v47(
   batch_id INTEGER PRIMARY KEY REFERENCES batches(id),packaging_cost REAL NOT NULL DEFAULT 0 CHECK(packaging_cost>=0),
   overhead_cost REAL NOT NULL DEFAULT 0 CHECK(overhead_cost>=0),note TEXT NOT NULL DEFAULT '',
   snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id));""")
  # Existing users keep their original role; repeated init is idempotent.
  c.execute('INSERT OR IGNORE INTO user_roles_v33(user_id,role) SELECT id,role FROM users')
  if not c.execute('SELECT 1 FROM users LIMIT 1').fetchone():
   pw=os.environ.get('ADMIN_PASSWORD')
   if not pw: pw=secrets.token_urlsafe(12);print('\n*** INITIAL ADMIN PASSWORD:',pw,'***\n',flush=True)
   c.execute('INSERT INTO users(username,password,role) VALUES(?,?,?)',('admin',generate_password_hash(pw),'admin'))

  from stock_alerts_v61 import init_stock_alerts
  init_stock_alerts(c)
  from attendance_v62 import init_attendance
  init_attendance(c)
  from attendance_v64 import init_attendance_v64
  init_attendance_v64(c)

def now():return datetime.now().isoformat(timespec='seconds')
def audit(c,action,details):c.execute('INSERT INTO audit(actor_id,action,details,created_at) VALUES(?,?,?,?)',(session.get('uid'),action,details,now()))
def login_required(fn):
 @wraps(fn)
 def w(*a,**kw):
  if not session.get('uid'):return redirect(url_for('login'))
  return fn(*a,**kw)
 return w
def current_roles():
 uid=session.get('uid')
 if not uid:return frozenset()
 with conn() as c:
  rows=c.execute('SELECT role FROM user_roles_v33 WHERE user_id=?',(uid,)).fetchall()
  if not rows:
   user=c.execute('SELECT role FROM users WHERE id=?',(uid,)).fetchone()
   return frozenset([user['role']]) if user else frozenset()
 return frozenset(r['role'] for r in rows if r['role'] in ROLES)

def has_role(*roles):
 mine=current_roles()
 return 'admin' in mine or bool(mine.intersection(roles))

def is_only_operator():
 mine=current_roles()
 return 'operator' in mine and not bool(mine-{'operator'})

def allowed(*roles):
 def deco(fn):
  @wraps(fn)
  @login_required
  def w(*a,**kw):
   if not has_role(*roles):abort(403)
   return fn(*a,**kw)
  return w
 return deco

MENU_V34 = [
 ('Администрирование','/admin/system','Состояние системы',('admin',)),
 ('Основное','/','Обзор',()),
 ('Основное','/notifications','Мои задачи и уведомления',()),
 ('Склад','/stock/thresholds','Минимальные остатки и оповещения',('admin','warehouse','economist')),
 ('Склад','/stock/planning','Потребность и предложения на закупку',('admin','warehouse','economist')),
 ('Экономист','/economics/costs','Себестоимость партий',('economist','admin','technologist')),
 ('Производство','/production/execution','Контроль выполнения',('admin','technologist','operator','quality','warehouse','viewer')),
 ('Справочники','/catalog','Номенклатура',('technologist','warehouse','quality','economist')),
 ('Технолог','/recipes','Рецептуры и техкарты',('technologist',)),
 ('Технолог','/tech-cards','Параметры ТК',('technologist',)),
 ('Технолог','/production/dashboard','Пульт технолога',('technologist',)),
 ('Технолог','/production/shift-plan','План смены',('technologist','operator')),
 ('Технолог','/production/stages','Этапы смены',('technologist','operator')),
 ('Технолог','/production/sheets','Производственные листы',('technologist','operator')),
 ('Технолог','/work-orders','Производственные задания',('technologist','operator')),
 ('Технолог','/production','Производство',('technologist','operator')),
 ('Технолог','/production/load-reconciliation','Сверка загрузок',('technologist','quality')),
 ('Склад','/inventory/v45','Инвентаризация CT48',('warehouse','technologist','quality','viewer')),
 ('ТСД','/tsd','ТСД CT48',('warehouse','operator')),
 ('ТСД','/tsd/stages','ТСД · Этапы',('operator','technologist')),
 ('ТСД','/tsd/production','ТСД · Техкарты',('operator','technologist')),
 ('ТСД','/tsd/shift-plan','ТСД · Моя смена',('operator','technologist')),
 ('ТСД','/tsd/receive','ТСД · Приёмка тары',('warehouse',)),
 ('ТСД','/tsd/pick','ТСД · Подбор задания',('warehouse','operator')),
 ('ТСД','/tsd/load','ТСД · Загрузка сырья',('operator','technologist')),
 ('ТСД','/tsd/return','ТСД · Возврат',('warehouse','operator')),
 ('ТСД','/scan','Сканирование ТСД / QR',('warehouse','operator')),
 ('ТСД','/tsd/packing','ТСД · Фасовка',('warehouse','operator')),
 ('ТСД','/tsd/ship','ТСД · Отгрузка ГП',('warehouse',)),
 ('ТСД','/tsd/customer-return','ТСД · Возврат клиента',('warehouse',)),
 ('Склад','/lots','Партии и склад',('warehouse','quality','technologist')),
 ('Склад','/labels','Этикетки',('warehouse',)),
 ('Склад','/containers','Тара и печать',('warehouse',)),
 ('Склад','/packing','Фасовка ГП',('warehouse','operator')),
 ('Склад','/container-ops','Операции с тарой',('warehouse',)),
 ('Склад','/stock-alerts','Контроль сроков',('warehouse','quality')),
 ('Склад','/reconciliation','Сверка остатков и расхода',('warehouse','technologist')),
 ('ОТК','/quality','Лаборатория ОТК',('quality',)),
 ('ОТК','/quality/samples','Пробы ОТК',('quality',)),
 ('ОТК','/quality/passports','Паспорта качества',('quality',)),
 ('ОТК','/quality/customer-returns','ОТК · Возвраты',('quality',)),
 ('ОТК','/quality/disposal-acts','Акты брака и утилизации',('quality',)),
 ('Экономист','/planning','План и потребность',('economist','technologist')),
 ('Экономист','/supply','Резервы и снабжение',('economist','warehouse')),
 ('Экономист','/procurement','Закупки и поставщики',('economist',)),
 ('Экономист','/dispatch/tasks','Задания на отгрузку',('economist','warehouse')),
 ('Экономист','/dispatch/v32','Отгрузки · несколько партий',('economist','warehouse')),
 ('Экономист','/shipments','Отгрузки',('economist','warehouse')),
 ('Общее','/trace','Прослеживаемость',('technologist','warehouse','quality','economist','viewer')),
 ('Сотрудники','/attendance','Учёт рабочего времени',()),
 ('Администрирование','/users','Пользователи',('admin',)),
 ('Администрирование','/admin/permissions','Матрица прав',('admin',)),
 ('Администрирование','/audit','Журнал',('admin',)),
]

def menu_v34():
 roles=current_roles()
 if not roles:return []
 with conn() as c:
  prefs=c.execute('SELECT path,visible FROM user_menu_v34 WHERE user_id=?',(session['uid'],)).fetchall()
 overrides={r['path']:bool(r['visible']) for r in prefs}
 return [(group,path,label) for group,path,label,allowed_roles in MENU_V34
         if (path!='/attendance' or _attendance_menu_access(session['uid']))
         and ('admin' in roles or not allowed_roles or roles.intersection(allowed_roles))
         and overrides.get(path,True)]

def page(title,body,**ctx):
 base='''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}} · lovetech</title>
 <style>
 :root{font-family:Inter,system-ui,-apple-system,Arial,sans-serif;color:#1c293d;background:#f5f6f8;--red:#cf001e;--line:#e1e5eb;--muted:#67758a}*{box-sizing:border-box}body{margin:0}a{color:#c90020;text-decoration:none}a:hover{text-decoration:underline}
 .shell{display:flex;min-height:100vh}.sidebar{width:252px;flex:0 0 252px;background:#f9fafb;border-right:1px solid var(--line);position:sticky;top:0;height:100vh;overflow-y:auto}.brand{display:flex;align-items:center;gap:12px;padding:23px 20px;border-bottom:1px solid var(--line)}.brand-icon{font-size:31px;color:var(--red)}.brand strong{font-size:27px;letter-spacing:-1px;color:#19243a}.brand strong em{font-style:normal;color:var(--red)}.brand small{display:block;font-size:11px;color:#687488}.sidebar-nav{padding:15px 10px}.sidebar-nav a{display:flex;gap:10px;align-items:center;padding:10px 13px;border-radius:7px;color:#26344a;font-size:14px;font-weight:550}.sidebar-nav a:hover{background:#eceef2;text-decoration:none}.sidebar-nav a.active{background:var(--red);color:white}.sidebar-nav .group{font-size:10px;text-transform:uppercase;color:#8490a1;padding:17px 12px 5px;letter-spacing:1px}.sidebar-nav .navicon{width:20px;text-align:center;font-size:16px}.workspace{flex:1;min-width:0}.topbar{height:70px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:16px;padding:0 26px}.topbar .search{border:1px solid var(--line);border-radius:7px;background:#fafbfc;padding:10px 14px;max-width:440px;flex:1}.userbar{font-size:13px;color:#4a596d}.userbar strong{color:#17243b}.content{max-width:1600px;margin:0 auto;padding:24px 25px 45px}.page-heading{font-size:26px;letter-spacing:-.5px;margin:2px 0 20px;color:#19263c}.card{background:white;border:1px solid var(--line);border-radius:9px;padding:19px;margin:14px 0;overflow:auto;box-shadow:0 2px 7px #17243b06}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:10px 9px;border-bottom:1px solid #e8ebf0;vertical-align:top}th{background:#f6f7f9;color:#526079;font-weight:650}tr:hover td{background:#fbfcfd}h1{font-size:26px}h2{font-size:19px}h3{font-size:16px}input,select,textarea{padding:10px;border:1px solid #cdd4de;border-radius:7px;max-width:100%;background:white}input[type=number]{width:120px}label{display:inline-flex;flex-direction:column;gap:5px;margin:6px 12px 6px 0}button,.btn{background:var(--red);color:white;padding:10px 15px;border:0;border-radius:7px;cursor:pointer;display:inline-block}button:hover,.btn:hover{background:#a9001a;color:white;text-decoration:none}.danger{color:#a12727}.msg{background:#fff2d9;border-left:3px solid #e9a23b;padding:12px;border-radius:6px;margin:10px 0}.muted{color:var(--muted);font-size:13px}.pill{padding:4px 9px;border-radius:12px;background:#edf0f4;white-space:nowrap}form{margin:8px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}.metric{font-size:30px;font-weight:750}pre{white-space:pre-wrap}.dash-links{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px}.dash-link{background:#fff;border:1px solid var(--line);border-radius:9px;padding:17px 12px;min-height:103px;color:#1b293f}.dash-link:hover{border-color:#e39ba5;background:#fff9fa;text-decoration:none}.dash-link .ico{font-size:24px;display:block;margin-bottom:9px}.dash-link b{font-size:14px}.dash-link small{display:block;color:#68758a;margin-top:6px;font-size:11px}.dash-link.featured{background:#fff1f3;border-color:#f5d5d9;color:#c90020}.dash-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.dash-kpis .card{margin:0}.dash-kpis .metric{margin-top:12px}.dash-panels{display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-top:15px}.dash-panels .card{margin:0}.dash-bottom{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:15px;margin-top:15px}.dash-bottom .card{margin:0}.panel-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:14px}.panel-head h2{margin:0;font-size:16px}.status{padding:4px 8px;border-radius:6px;background:#f1f2f5;color:#526078;white-space:nowrap}.status.done{background:#e2f6e9;color:#167445}.status.active{background:#fff1dc;color:#9e5900}.bar{height:8px;border-radius:8px;background:#e9ecf0;overflow:hidden;min-width:60px}.bar span{display:block;background:var(--red);height:100%}.dash-note{border-left:3px solid var(--red);padding:8px 10px;background:#fafbfc;margin:8px 0;font-size:12px}.mini-bars{display:flex;align-items:flex-end;height:125px;gap:10px;border-bottom:1px solid #dfe4ea;padding:0 5px}.mini-bars div{flex:1;background:linear-gradient(#f1546b,#cf001e);border-radius:4px 4px 0 0}.donut{width:160px;height:160px;border-radius:50%;background:conic-gradient(#e4002b 0 45%,#ff6c39 45% 70%,#ffbd28 70% 85%,#9894c9 85% 95%,#cdd2db 95%);display:grid;place-items:center;margin:8px auto}.donut-inner{background:white;width:105px;height:105px;border-radius:50%;display:grid;place-items:center;text-align:center;font-weight:750;font-size:16px}.donut-inner small{font-size:10px;color:#68758a;font-weight:400}.hamburger{display:none}.logout{margin-left:14px}.no-data{color:#8791a0;text-align:center;padding:25px 8px}.dash-intro{color:#6b7890;margin-top:-12px;margin-bottom:20px}
 @media(max-width:1150px){.dash-links{grid-template-columns:repeat(3,1fr)}.dash-bottom{grid-template-columns:1fr 1fr}.dash-bottom .card:first-child{grid-column:span 2}}@media(max-width:800px){.sidebar{position:fixed;z-index:30;transform:translateX(-100%);transition:transform .2s;box-shadow:0 10px 40px #0002}.sidebar.open{transform:none}.hamburger{display:inline-block;background:transparent;color:var(--red);font-size:24px;padding:4px 9px}.topbar{padding:0 12px;height:62px}.topbar .search{display:none}.content{padding:18px 13px}.dash-kpis{grid-template-columns:repeat(2,1fr)}.dash-panels,.dash-bottom{grid-template-columns:1fr}.dash-bottom .card:first-child{grid-column:auto}.dash-links{grid-template-columns:repeat(2,1fr)}.userbar{font-size:11px}}@media(max-width:440px){.dash-kpis{grid-template-columns:1fr 1fr}.dash-kpis .metric{font-size:23px}.dash-link{min-height:95px;padding:12px 9px}.page-heading{font-size:22px}}
 /* v38: shared work-area design, accessible focus, responsive CT48 controls */
 :root{--surface:#fff;--canvas:#f5f6f8;--ink:#19263c;--accent:#c90020;--soft-red:#fff2f4}
 .card h2:first-child,.card h3:first-child{margin-top:0;color:var(--ink)}
 .card>form{display:flex;flex-wrap:wrap;align-items:end;gap:10px 12px}
 .card>form label{margin:0;min-width:150px;flex:0 1 auto}
 .card>form label input,.card>form label select,.card>form label textarea{width:100%}
 input,select,textarea,button,.btn{font:inherit;line-height:1.4}
 input:focus-visible,select:focus-visible,textarea:focus-visible,button:focus-visible,a:focus-visible{outline:3px solid #e98d9b;outline-offset:2px}
 input[type=checkbox],input[type=radio]{accent-color:var(--accent)}
 .card table{min-width:550px}.card{scrollbar-color:#cdd4de transparent}
 .card table tr:last-child td{border-bottom:0}
 .card a:not(.btn){text-underline-offset:3px}
 .sidebar-nav a.active{font-weight:700}.sidebar-nav a[hidden],.sidebar-nav .group[hidden]{display:none!important}
 .topbar .search{display:flex;align-items:center;gap:8px;max-width:440px;padding:0 10px}
 .topbar .search input{border:0;background:transparent;width:100%;padding:11px 4px;outline:none;font-size:13px}
 .topbar .search input:focus-visible{outline:2px solid #e98d9b;outline-offset:-2px}
 .search-submit{padding:7px 12px;font-size:12px}.search-empty{display:none;color:#8c3b49;font-size:12px;padding:8px 12px}
 .search-empty.visible{display:block}
 .sidebar-overlay{display:none}
 .ct48{max-width:600px!important;margin:0 auto!important}
 .ct48 input,.ct48 select,.ct48 textarea,.ct48 button{font-size:18px!important;min-height:54px!important;max-width:100%;border-radius:9px!important}
 .ct48 input,.ct48 select,.ct48 textarea{background:#fff!important;border:1px solid #cbd3de!important}
 .ct48 input:focus,.ct48 select:focus{border-color:var(--red)!important;box-shadow:0 0 0 3px #cf001e1a}
 .ct48 button{font-weight:700;touch-action:manipulation}
 .ct48 .card{padding:16px}
 @media(max-width:800px){.sidebar-overlay.open{display:block;position:fixed;inset:0;background:#0f1a2a77;z-index:29}.topbar .search{display:flex;min-width:0;flex:1}.topbar .search input{font-size:12px}.userbar{max-width:38%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.logout{margin-left:6px}.card{padding:14px}.card>form{display:block}.card>form label{display:flex;margin:9px 0}.card>form button{width:100%;min-height:46px}.ct48{width:100%}}
 @media(max-width:480px){.topbar .search{display:none}.userbar{max-width:75%}.ct48 input,.ct48 select,.ct48 button{width:100%!important}}

 @media print{.sidebar,.topbar,.hamburger{display:none!important}.content{padding:0}.card{box-shadow:none}.workspace{width:100%}}
 </style></head><body><div class="shell">
 {% if session.uid %}<aside class="sidebar" id="sidebar"><div class="brand"><span class="brand-icon">⚗</span><div><strong>love<em>tech</em></strong><small>Производство и склад</small></div></div><nav class="sidebar-nav" id="sidebar-nav" aria-label="Навигация"><a href="/" class="{% if request.path=='/' %}active{% endif %}"><span class="navicon">⌂</span>Главная</a>{% set ns=namespace(group='') %}{% for group,path,label in menu_items %}{% if ns.group!=group %}<div class="group">{{group}}</div>{% set ns.group=group %}{% endif %}<a href="{{path}}" class="{% if request.path==path %}active{% endif %}"><span class="navicon">{% if group=='Склад' %}▣{% elif group=='ТСД' %}▯{% elif group=='ОТК' %}◈{% elif group=='Технолог' %}⚙{% elif group=='Экономист' %}▤{% else %}◉{% endif %}</span>{{label}}</a>{% endfor %}<a href="/profile/menu">⚙ Моё меню</a></nav></aside>{% endif %}
 <div class="sidebar-overlay" id="sidebar-overlay" aria-hidden="true"></div><div class="workspace"><div class="topbar">{% if session.uid %}<button class="hamburger" type="button" aria-label="Открыть меню" onclick="window.lovetechToggleMenu()">☰</button>{% else %}<b>⚗ lovetech</b>{% endif %}<form class="search" method="get" action="/search" role="search"><span aria-hidden="true">⌕</span><input id="nav-search" name="q" type="search" placeholder="Поиск партии, продукции, клиента, QR..." autocomplete="off" aria-label="Глобальный поиск"><button type="submit" class="search-submit" aria-label="Искать">Найти</button></form><span class="userbar">{% if session.uid %}<strong>{{session.username}}</strong> · {{role_labels}} <a class="logout" href="{{url_for('logout')}}">Выход</a>{% endif %}</span></div><main class="content">{% if not is_home %}<h1 class="page-heading">{{title}}</h1>{% endif %}{% for m in get_flashed_messages() %}<div class="msg">{{m}}</div>{% endfor %}'''+body+'''</main></div></div><script>
 (function(){
  const menu=document.getElementById('sidebar'),overlay=document.getElementById('sidebar-overlay'),search=document.getElementById('nav-search');
  window.lovetechToggleMenu=function(force){if(!menu)return;const opened=typeof force==='boolean'?force:!menu.classList.contains('open');menu.classList.toggle('open',opened);if(overlay)overlay.classList.toggle('open',opened)};
  if(overlay)overlay.addEventListener('click',()=>window.lovetechToggleMenu(false));
  document.addEventListener('keydown',e=>{if(e.key==='Escape')window.lovetechToggleMenu(false)});

 })();
 </script></body></html>'''
 return render_template_string(base.replace('<form method="post">','<form method="post"><input type="hidden" name="_csrf" value="{{csrf_token}}">'),title=title,roles=ROLES,role_labels=', '.join(ROLES[r] for r in ROLES if r in current_roles()),has_role=has_role,menu_items=menu_v34(),today=date.today().isoformat(),is_home=request.path=='/',request=request,**ctx)

def val(form,key,default=None):return form.get(key,default)
def number(form,key):
 v=float(form[key]);
 if not (0<v<1e12):raise ValueError('Количество должно быть положительным')
 return v

def fail(e):flash('Ошибка: '+str(e));return redirect(request.referrer or '/')

@app.route('/login',methods=['GET','POST'])
def login():
 if request.method=='POST':
  with conn() as c:u=c.execute('SELECT * FROM users WHERE username=?',(request.form.get('username'),)).fetchone()
  if u and check_password_hash(u['password'],request.form.get('password','')):
   session.clear();session.update(uid=u['id'],username=u['username'],role=u['role']);return redirect('/')
  flash('Неверный логин или пароль')
 return page('Вход','<div class="card"><form method="post"><label>Логин<input name="username" required></label><label>Пароль<input type="password" name="password" required></label><button>Войти</button></form></div>')
@app.route('/logout')
def logout():session.clear();return redirect('/login')
@app.route('/')
@login_required
def home():
 with conn() as c:
  kpis={
   'finished':c.execute("SELECT COALESCE(SUM(qty),0) FROM lots WHERE kind='finished' AND status='released'").fetchone()[0],
   'raw':c.execute("SELECT COALESCE(SUM(qty),0) FROM lots WHERE kind='raw' AND status='released'").fetchone()[0],
   'dispatch':c.execute("SELECT COUNT(*) FROM dispatch_tasks_v31 WHERE status NOT IN ('completed','cancelled')").fetchone()[0],
   'samples':c.execute("SELECT COUNT(*) FROM qc_samples WHERE status='open'").fetchone()[0]}
  tasks=c.execute("SELECT id,customer,vehicle,status,created_at FROM dispatch_tasks_v31 ORDER BY id DESC LIMIT 6").fetchall()
  orders=c.execute("SELECT w.id,w.lot_code,w.status,w.planned_qty,p.name AS product FROM work_orders w JOIN recipes r ON r.id=w.recipe_id JOIN products p ON p.id=r.product_id ORDER BY w.id DESC LIMIT 6").fetchall()
  exp=c.execute("SELECT lot_code,expires,qty FROM lots WHERE qty>0 AND expires<=date('now','+30 day') ORDER BY expires LIMIT 4").fetchall()
  shipment_days=c.execute("SELECT substr(created_at,1,10) AS day,ROUND(SUM(qty),1) AS qty FROM shipments WHERE created_at>=date('now','-6 days') GROUP BY substr(created_at,1,10) ORDER BY day").fetchall()
  finished_count=c.execute("SELECT COUNT(*) FROM lots WHERE kind='finished' AND qty>0").fetchone()[0]
  raw_count=c.execute("SELECT COUNT(*) FROM lots WHERE kind='raw' AND qty>0").fetchone()[0]
 visible={path for _,path,_ in menu_v34()}
 shortcuts=[('▣','Склад','Остатки, приёмка, перемещения','/lots'),('⚙','Производство','Задания, рецептуры, выпуск','/work-orders'),('▰','Отгрузка','Задания экономиста, фотоотчёт','/dispatch/v32'),('▤','Закупки','Заявки и поставщики','/procurement'),('◈','ОТК','Пробы и паспорта качества','/quality/samples'),('▯','Urovo CT48','Сканирование QR','/tsd')]
 shortcuts=[x for x in shortcuts if x[3] in visible]
 return page('Главная','''<h1 class="page-heading">Добро пожаловать в <span style="color:var(--red)">lovetech!</span></h1><p class="dash-intro">Единая система учёта производства, склада, отгрузки и контроля качества</p>
 <div class="dash-links">{% for ico,name,desc,path in shortcuts %}<a class="dash-link {% if name=='Отгрузка' %}featured{% endif %}" href="{{path}}"><span class="ico">{{ico}}</span><b>{{name}} →</b><small>{{desc}}</small></a>{% endfor %}</div>
 <div class="dash-kpis" style="margin-top:16px"><div class="card"><b>▣ Остатки готовой продукции</b><div class="metric">{{'%.1f'|format(kpis.finished)}} <small style="font-size:13px">кг*</small></div><div class="muted">{{finished_count}} партий с остатком</div></div><div class="card"><b>◈ Сырьё и материалы</b><div class="metric">{{'%.1f'|format(kpis.raw)}} <small style="font-size:13px">кг*</small></div><div class="muted">{{raw_count}} партий с остатком</div></div><div class="card"><b>▰ Задания на отгрузку</b><div class="metric">{{kpis.dispatch}}</div><div class="muted">Не завершены и не отменены</div></div><div class="card"><b>⚗ Пробы ОТК</b><div class="metric">{{kpis.samples}}</div><div class="muted">Ожидают рассмотрения</div></div></div>
 <div class="dash-panels"><section class="card"><div class="panel-head"><h2>▰ Задания на отгрузку</h2>{% if '/dispatch/v32' in visible %}<a href="/dispatch/v32">Все задания →</a>{% endif %}</div><table><tr><th>№</th><th>Клиент</th><th>Автомобиль</th><th>Статус</th></tr>{% for t in tasks %}<tr><td><a href="/dispatch/v32/{{t.id}}">{{t.id}}</a></td><td>{{t.customer}}</td><td>{{t.vehicle or '—'}}</td><td><span class="status {% if t.status in ('completed','shipped') %}done{% elif t.status in ('assigned','new') %}active{% endif %}">{{{'new':'Новое','assigned':'В работе','shipped':'Отгружено','completed':'Завершено','cancelled':'Отменено'}.get(t.status,t.status)}}</span></td></tr>{% else %}<tr><td colspan="4" class="no-data">Заданий пока нет</td></tr>{% endfor %}</table></section>
 <section class="card"><div class="panel-head"><h2>⚙ Производственные задания</h2>{% if '/work-orders' in visible %}<a href="/work-orders">Все задания →</a>{% endif %}</div><table><tr><th>№</th><th>Продукция</th><th>Партия</th><th>Статус</th><th>План</th></tr>{% for o in orders %}<tr><td>{{o.id}}</td><td>{{o.product}}</td><td>{{o.lot_code}}</td><td><span class="status {% if o.status=='completed' %}done{% elif o.status=='approved' %}active{% endif %}">{{{'draft':'Черновик','approved':'В работе','completed':'Завершено','cancelled':'Отменено'}.get(o.status,o.status)}}</span></td><td>{{o.planned_qty}}</td></tr>{% else %}<tr><td colspan="5" class="no-data">Заданий пока нет</td></tr>{% endfor %}</table></section></div>
 <div class="dash-bottom"><section class="card"><div class="panel-head"><h2>◉ Уведомления по срокам</h2>{% if '/lots' in visible %}<a href="/lots">Все партии →</a>{% endif %}</div>{% for e in exp %}<div class="dash-note"><b>{{e.lot_code}}</b> — годен до {{e.expires}}, остаток {{e.qty}}</div>{% else %}<p class="muted">Нет партий с истекающим сроком годности в ближайшие 30 дней.</p>{% endfor %}</section><section class="card"><div class="panel-head"><h2>Отгрузки за 7 дней</h2></div><div class="mini-bars">{% set peak=namespace(v=1) %}{% for d in shipment_days %}{% if d.qty>peak.v %}{% set peak.v=d.qty %}{% endif %}{% endfor %}{% for d in shipment_days %}<div title="{{d.day}}: {{d.qty}}" style="height:{{(d.qty/peak.v*100)|round|int}}%"></div>{% else %}<p class="muted">Нет отгрузок за период</p>{% endfor %}</div><p class="muted">Фактическая масса отгрузок по дням</p></section><section class="card"><div class="panel-head"><h2>Остатки по категориям</h2></div><div class="donut"><div class="donut-inner">{{'%.0f'|format(kpis.finished)}}<small>Готовая продукция*</small></div></div><p class="muted" style="text-align:center">Готовая продукция: {{finished_count}} партий</p></section></div><p class="muted">* Сумма остатков корректна для партий, учитываемых в килограммах. Если в справочнике используются разные единицы измерения, необходимо настроить пересчёт перед использованием итоговых значений.</p>''',kpis=kpis,tasks=tasks,orders=orders,exp=exp,shipment_days=shipment_days,finished_count=finished_count,raw_count=raw_count,shortcuts=shortcuts,visible=visible)
@app.route('/catalog',methods=['GET','POST'])
@login_required
def catalog():
 if request.method=='POST':
  if not has_role('admin','technologist'):abort(403)
  try:
   typ=request.form['kind'];table='materials' if typ=='raw' else 'products'
   with conn() as c:
    c.execute(f'INSERT INTO {table}(name,unit,shelf_days) VALUES(?,?,?)',(request.form['name'].strip(),request.form['unit'],int(request.form['shelf_days'])))
    audit(c,'catalog.create',typ+': '+request.form['name'])
   flash('Добавлено')
  except Exception as e:return fail(e)
  return redirect('/catalog')
 with conn() as c:raw=c.execute('SELECT * FROM materials ORDER BY name').fetchall();products=c.execute('SELECT * FROM products ORDER BY name').fetchall()
 return page('Номенклатура','''{% if has_role('admin','technologist') %}<div class="card"><h2>Добавить позицию</h2><form method="post"><label>Тип<select name="kind"><option value="raw">Сырьё</option><option value="finished">Готовая продукция</option></select></label><label>Наименование<input name="name" required></label><label>Единица<select name="unit"><option>кг</option><option>л</option><option>шт</option></select></label><label>Срок годности, дней<input name="shelf_days" type="number" min="1" value="365" required></label><button>Добавить</button></form></div>{% endif %}{% for label,rows in [('Сырьё',raw),('Готовая продукция',products)] %}<div class="card"><h2>{{label}}</h2><table><tr><th>ID</th><th>Название</th><th>Ед.</th><th>Срок, дней</th></tr>{% for x in rows %}<tr><td>{{x.id}}</td><td>{{x.name}}</td><td>{{x.unit}}</td><td>{{x.shelf_days}}</td></tr>{% endfor %}</table></div>{% endfor %}''',raw=raw,products=products)
@app.route('/recipes',methods=['GET','POST'])
@login_required
def recipes():
 if request.method=='POST':
  if not has_role('admin','technologist'):abort(403)
  try:
   pid=int(request.form['product_id'])
   with conn() as c:
    version=c.execute('SELECT COALESCE(MAX(version),0)+1 FROM recipes WHERE product_id=?',(pid,)).fetchone()[0]
    cur=c.execute('INSERT INTO recipes(product_id,version,notes,created_at) VALUES(?,?,?,?)',(pid,version,request.form.get('notes',''),now()))
    audit(c,'recipe.create',str(cur.lastrowid))
   return redirect('/recipe/'+str(cur.lastrowid))
  except Exception as e:return fail(e)
 with conn() as c:
  products=c.execute('SELECT * FROM products ORDER BY name').fetchall()
  rows=c.execute('SELECT r.*,p.name FROM recipes r JOIN products p ON p.id=r.product_id ORDER BY r.id DESC').fetchall()
 return page('Рецептуры и технологические карты','''{% if has_role('admin','technologist') %}<div class="card"><h2>Новая версия рецептуры</h2><form method="post"><label>Продукт<select name="product_id">{% for p in products %}<option value="{{p.id}}">{{p.name}}</option>{% endfor %}</select></label><label>Примечания<input name="notes"></label><button>Создать черновик</button></form></div>{% endif %}<div class="card"><table><tr><th>Продукт</th><th>Версия</th><th>Статус</th><th>Открыть</th></tr>{% for r in rows %}<tr><td>{{r.name}}</td><td>{{r.version}}</td><td>{{r.status}}</td><td><a href="/recipe/{{r.id}}">Техкарта →</a> · <a href="/recipe/{{r.id}}/print" target="_blank">Печать A4</a></td></tr>{% endfor %}</table></div>''',products=products,rows=rows)
@app.route('/recipe/<int:rid>',methods=['GET','POST'])
@login_required
def recipe(rid):
 if request.method=='POST':
  if not has_role('admin','technologist'):abort(403)
  try:
   with conn() as c:
    r=c.execute('SELECT * FROM recipes WHERE id=?',(rid,)).fetchone()
    if not r or r['status']!='draft':raise ValueError('Редактировать можно только черновик')
    action=request.form['action']
    if action=='item':c.execute('INSERT INTO recipe_items(recipe_id,material_id,percent) VALUES(?,?,?)',(rid,int(request.form['material_id']),number(request.form,'percent')))
    elif action=='step':c.execute('INSERT INTO steps(recipe_id,position,instruction,control_point) VALUES(?,?,?,?)',(rid,int(request.form['position']),request.form['instruction'],request.form.get('control_point','')))
    elif action=='approve':
     total=c.execute('SELECT COALESCE(SUM(percent),0) FROM recipe_items WHERE recipe_id=?',(rid,)).fetchone()[0]
     steps=c.execute('SELECT count(*) FROM steps WHERE recipe_id=?',(rid,)).fetchone()[0]
     if abs(total-100)>0.0001 or steps==0:raise ValueError('Для утверждения сумма компонентов должна быть 100%, необходим хотя бы один шаг')
     c.execute("UPDATE recipes SET status='approved' WHERE id=?",(rid,))
    else:raise ValueError('Неизвестная операция')
    audit(c,'recipe.'+action,str(rid))
  except Exception as e:return fail(e)
  return redirect('/recipe/'+str(rid))
 with conn() as c:
  r=c.execute('SELECT r.*,p.name FROM recipes r JOIN products p ON p.id=r.product_id WHERE r.id=?',(rid,)).fetchone()
  if not r:abort(404)
  items=c.execute('SELECT i.*,m.name,m.unit FROM recipe_items i JOIN materials m ON m.id=i.material_id WHERE recipe_id=?',(rid,)).fetchall()
  steps=c.execute('SELECT * FROM steps WHERE recipe_id=? ORDER BY position,id',(rid,)).fetchall()
  materials=c.execute('SELECT * FROM materials ORDER BY name').fetchall()
 return page('Техкарта: '+r['name']+' / v'+str(r['version']),'''<div class="card"><p><b>Статус:</b> {{r.status}} · <b>Примечания:</b> {{r.notes}} · <a href="/recipe/{{r.id}}/print" target="_blank">Распечатать технологическую карту A4</a></p><h2>Состав</h2><table><tr><th>Сырьё</th><th>Доля, %</th></tr>{% for x in items %}<tr><td>{{x.name}}</td><td>{{x.percent}}</td></tr>{% endfor %}</table><p><b>Итого: {{total}}%</b></p><h2>Технологические операции</h2><table><tr><th>№</th><th>Операция</th><th>Контрольная точка</th></tr>{% for x in steps %}<tr><td>{{x.position}}</td><td>{{x.instruction}}</td><td>{{x.control_point}}</td></tr>{% endfor %}</table></div>{% if r.status=='draft' and has_role('admin','technologist') %}<div class="card"><h2>Редактирование черновика</h2><form method="post"><input type="hidden" name="action" value="item"><label>Сырьё<select name="material_id">{% for m in materials %}<option value="{{m.id}}">{{m.name}}</option>{% endfor %}</select></label><label>Доля, %<input type="number" name="percent" min="0.000001" max="100" step="any" required></label><button>Добавить компонент</button></form><form method="post"><input type="hidden" name="action" value="step"><label>№<input name="position" type="number" min="1" required></label><label>Операция<input name="instruction" required></label><label>Контрольная точка<input name="control_point"></label><button>Добавить операцию</button></form><form method="post"><input type="hidden" name="action" value="approve"><button>Утвердить рецептуру</button></form></div>{% endif %}''',r=r,items=items,steps=steps,materials=materials,total=round(sum(x['percent'] for x in items),6))
@app.route('/lots',methods=['GET','POST'])
@login_required
def lots():
 if request.method=='POST':
  if not has_role('admin','warehouse','quality'):abort(403)
  try:
   action=request.form['action']
   with conn() as c:
    if action=='receive':
     mid=int(request.form['material_id']);qty=number(request.form,'qty');m=c.execute('SELECT * FROM materials WHERE id=?',(mid,)).fetchone()
     manufactured=request.form['manufactured'];exp=(date.fromisoformat(manufactured)+timedelta(days=m['shelf_days'])).isoformat()
     cur=c.execute("INSERT INTO lots(kind,material_id,lot_code,qty,manufactured,expires,status,supplier,created_at) VALUES('raw',?,?,?,?,?,'quarantine',?,?)",(mid,request.form['lot_code'],qty,manufactured,exp,request.form.get('supplier',''),now()))
     c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(cur.lastrowid,qty,'Приход сырья',now(),session['uid']))
    elif action=='status':
     if not has_role('admin','quality'):abort(403)
     lid=int(request.form['lot_id']);status=request.form['status']
     if status not in ('released','blocked','quarantine'):raise ValueError('Неверный статус')
     if status=='released':
      current=c.execute('SELECT qty,expires FROM lots WHERE id=?',(lid,)).fetchone()
      if not current:raise ValueError('Партия не найдена')
      if current['expires'] < date.today().isoformat():raise ValueError('Нельзя разрешить просроченную партию')
      ok,reason=qc_release_check(c,lid)
      if not ok:raise ValueError(reason)
     c.execute('UPDATE lots SET status=? WHERE id=?',(status,lid))
    else:raise ValueError('Неизвестная операция')
    audit(c,'lot.'+action,str(dict(request.form)))
   flash('Сохранено')
  except Exception as e:return fail(e)
  return redirect('/lots')
 with conn() as c:
  mats=c.execute('SELECT * FROM materials ORDER BY name').fetchall()
  rows=c.execute('SELECT l.*,COALESCE(m.name,p.name) name FROM lots l LEFT JOIN materials m ON m.id=l.material_id LEFT JOIN products p ON p.id=l.product_id ORDER BY l.id DESC').fetchall()
 return page('Партии и склад','''{% if has_role('admin','warehouse','quality') %}<div class="card"><h2>Приёмка сырья</h2><form method="post"><input type="hidden" name="action" value="receive"><label>Сырьё<select name="material_id">{% for m in mats %}<option value="{{m.id}}">{{m.name}}</option>{% endfor %}</select></label><label>Код партии<input name="lot_code" required></label><label>Количество<input name="qty" type="number" step="any" min="0.000001" required></label><label>Дата изготовления<input name="manufactured" type="date" value="{{today}}" required></label><label>Поставщик<input name="supplier"></label><button>Принять на карантин</button></form></div>{% endif %}<div class="card"><h2>Остатки по партиям</h2><table><tr><th>Партия</th><th>Тип / позиция</th><th>Остаток</th><th>Годен до</th><th>Статус</th><th>Действия ОТК</th></tr>{% for l in rows %}<tr><td><a href="/trace?lot={{l.lot_code}}">{{l.lot_code}}</a> · <a href="/labels/{{l.id}}">Этикетка</a></td><td>{{'Сырьё' if l.kind=='raw' else 'ГП'}} · {{l.name}}</td><td>{{'%.3f'|format(l.qty)}}</td><td>{{l.expires}}{% if l.expires<today %} <span class="danger">Просрочено</span>{% endif %}</td><td><span class="pill">{{l.status}}</span></td><td>{% if has_role('admin','quality') %}<form method="post"><input type="hidden" name="action" value="status"><input type="hidden" name="lot_id" value="{{l.id}}"><select name="status"><option value="released">Разрешить</option><option value="quarantine">Карантин</option><option value="blocked">Блокировать</option></select><button>OK</button></form>{% endif %}</td></tr>{% endfor %}</table></div>''',mats=mats,rows=rows)
def qc_release_check(c,lot_id):
 # Check latest results against current active limits; no automatic release.
 lot=c.execute('SELECT * FROM lots WHERE id=?',(lot_id,)).fetchone()
 if not lot:return False,'Партия не найдена'
 item_id=lot['material_id'] if lot['kind']=='raw' else lot['product_id']
 specs=c.execute('SELECT * FROM qc_specs WHERE kind=? AND item_id=? AND active=1 ORDER BY id',(lot['kind'],item_id)).fetchall()
 if not specs:return False,'Для этой номенклатуры нет действующих норм ОТК'
 for spec in specs:
  r=c.execute('SELECT * FROM qc_results WHERE lot_id=? AND spec_id=? ORDER BY id DESC LIMIT 1',(lot_id,spec['id'])).fetchone()
  if not r:return False,'Нет результата испытания: '+spec['parameter']
  v=r['value']
  if (spec['min_value'] is not None and v<spec['min_value']) or (spec['max_value'] is not None and v>spec['max_value']):
   return False,'Показатель вне нормы: '+spec['parameter']
 return True,'Все обязательные показатели в норме'

@app.route('/quality',methods=['GET','POST'])
@login_required
def quality():
 if request.method=='POST':
  if not has_role('admin','quality'):abort(403)
  try:
   action=request.form.get('action')
   with conn() as c:
    if action=='spec':
     kind=request.form['kind'];item_id=int(request.form['item_id']);parameter=request.form['parameter'].strip()
     if kind not in ('raw','finished') or not parameter or len(parameter)>120:raise ValueError('Некорректная норма')
     table='materials' if kind=='raw' else 'products'
     if not c.execute('SELECT 1 FROM '+table+' WHERE id=?',(item_id,)).fetchone():raise ValueError('Номенклатура не найдена')
     def bound(k):
      t=request.form.get(k,'').strip()
      if not t:return None
      import math
      v=float(t)
      if not math.isfinite(v):raise ValueError('Значение должно быть конечным числом')
      return v
     lo,hi=bound('min_value'),bound('max_value')
     if lo is None and hi is None:raise ValueError('Укажите хотя бы одну границу')
     if lo is not None and hi is not None and lo>hi:raise ValueError('Минимум больше максимума')
     c.execute('INSERT INTO qc_specs(kind,item_id,parameter,unit,min_value,max_value,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(kind,item_id,parameter) DO UPDATE SET unit=excluded.unit,min_value=excluded.min_value,max_value=excluded.max_value,active=1',(kind,item_id,parameter,request.form.get('unit','').strip()[:32],lo,hi,now()))
     c.execute("UPDATE lots SET status='quarantine' WHERE status='released' AND kind=? AND "+('material_id' if kind=='raw' else 'product_id')+"=?",(kind,item_id))
     audit(c,'qc.spec',f'{kind}:{item_id} {parameter} [{lo};{hi}]')
    elif action=='result':
     import math
     lot_id=int(request.form['lot_id']);spec_id=int(request.form['spec_id']);v=float(request.form['value'])
     if not math.isfinite(v):raise ValueError('Результат должен быть конечным числом')
     lot=c.execute('SELECT * FROM lots WHERE id=?',(lot_id,)).fetchone()
     spec=c.execute('SELECT * FROM qc_specs WHERE id=? AND active=1',(spec_id,)).fetchone()
     if not lot or not spec or spec['kind']!=lot['kind'] or spec['item_id']!=(lot['material_id'] if lot['kind']=='raw' else lot['product_id']):raise ValueError('Норма не соответствует партии')
     passed=int((spec['min_value'] is None or v>=spec['min_value']) and (spec['max_value'] is None or v<=spec['max_value']))
     c.execute('INSERT INTO qc_results(lot_id,spec_id,value,passed,recorded_at,user_id,comment) VALUES(?,?,?,?,?,?,?)',(lot_id,spec_id,v,passed,now(),session['uid'],request.form.get('comment','').strip()[:500]))
     # A new failing result invalidates a previous release.
     if not passed:c.execute("UPDATE lots SET status='quarantine' WHERE id=? AND status='released'",(lot_id,))
     audit(c,'qc.result',f'lot={lot_id} spec={spec_id} value={v} passed={passed}')
    else:raise ValueError('Неизвестная операция')
   flash('Данные ОТК сохранены')
  except Exception as e:return fail(e)
  return redirect('/quality')
 with conn() as c:
  materials=c.execute('SELECT id,name FROM materials ORDER BY name').fetchall()
  products=c.execute('SELECT id,name FROM products ORDER BY name').fetchall()
  specs=c.execute('SELECT * FROM qc_specs WHERE active=1 ORDER BY kind,item_id,parameter').fetchall()
  lots=c.execute('SELECT id,lot_code,kind,material_id,product_id,status FROM lots ORDER BY id DESC LIMIT 300').fetchall()
  results=c.execute('SELECT r.*,l.lot_code,s.parameter,s.unit,u.username FROM qc_results r JOIN lots l ON l.id=r.lot_id JOIN qc_specs s ON s.id=r.spec_id JOIN users u ON u.id=r.user_id ORDER BY r.id DESC LIMIT 100').fetchall()
  checks={l['id']:qc_release_check(c,l['id']) for l in lots}
 return page('Лаборатория ОТК','''<div class="card"><h2>Состояние партий</h2><table><tr><th>Партия</th><th>Статус</th><th>Готовность ОТК</th></tr>{% for l in lots %}<tr><td><a href="/trace?lot={{l.lot_code}}">{{l.lot_code}}</a> · <a href="/labels/{{l.id}}">Этикетка</a></td><td>{{l.status}}</td><td>{% if checks[l.id][0] %}✓ Все показатели в норме{% else %}<span class="danger">{{checks[l.id][1]}}</span>{% endif %}</td></tr>{% endfor %}</table><p class="muted">Решение о выпуске партии принимается вручную в разделе «Партии и склад».</p></div>
 {% if has_role('admin','quality') %}<div class="card"><h2>Новая или обновлённая норма</h2><p class="muted">При изменении нормы предыдущие измерения повторно сравниваются с действующими границами. Для производственного применения необходим регламент утверждения норм.</p><form method="post"><input type="hidden" name="action" value="spec"><label>Тип<select name="kind"><option value="raw">Сырьё</option><option value="finished">Готовая продукция</option></select></label><label>ID номенклатуры<input name="item_id" type="number" min="1" required></label><label>Показатель<input name="parameter" required maxlength="120"></label><label>Единица<input name="unit" maxlength="32"></label><label>Минимум<input name="min_value" type="number" step="any"></label><label>Максимум<input name="max_value" type="number" step="any"></label><button>Сохранить норму</button></form><p class="muted">Сырьё: {% for x in materials %}{{x.id}} — {{x.name}}; {% endfor %}</p><p class="muted">Продукты: {% for x in products %}{{x.id}} — {{x.name}}; {% endfor %}</p></div>
 <div class="card"><h2>Зарегистрировать измерение</h2><form method="post"><input type="hidden" name="action" value="result"><label>Партия<select name="lot_id">{% for l in lots %}<option value="{{l.id}}">{{l.lot_code}}</option>{% endfor %}</select></label><label>Норма<select name="spec_id">{% for s in specs %}<option value="{{s.id}}">№{{s.id}} · {{'сырьё' if s.kind=='raw' else 'ГП'}} {{s.item_id}} · {{s.parameter}}</option>{% endfor %}</select></label><label>Значение<input name="value" type="number" step="any" required></label><label>Комментарий<input name="comment" maxlength="500"></label><button>Записать результат</button></form></div>{% endif %}
 <div class="card"><h2>Действующие нормы</h2><table><tr><th>ID</th><th>Тип / ID позиции</th><th>Показатель</th><th>Диапазон</th></tr>{% for s in specs %}<tr><td>{{s.id}}</td><td>{{s.kind}} / {{s.item_id}}</td><td>{{s.parameter}}</td><td>{{s.min_value if s.min_value is not none else '—'}} … {{s.max_value if s.max_value is not none else '—'}} {{s.unit}}</td></tr>{% endfor %}</table></div>
 <div class="card"><h2>Журнал лабораторных результатов</h2><table><tr><th>Время</th><th>Партия</th><th>Показатель</th><th>Результат</th><th>Итог</th><th>Сотрудник</th></tr>{% for r in results %}<tr><td>{{r.recorded_at}}</td><td>{{r.lot_code}}</td><td>{{r.parameter}}</td><td>{{r.value}} {{r.unit}}</td><td>{{'Норма' if r.passed else 'ВНЕ НОРМЫ'}}</td><td>{{r.username}}</td></tr>{% endfor %}</table></div>''',materials=materials,products=products,specs=specs,lots=lots,results=results,checks=checks)


@app.route('/quality/samples',methods=['GET','POST'])
@login_required
def quality_samples():
 if request.method=='POST':
  if not has_role('admin','quality'):abort(403)
  try:
   action=request.form.get('action','')
   with conn() as c:
    if action=='create':
     lot_id=int(request.form['lot_id'])
     if not c.execute('SELECT 1 FROM lots WHERE id=?',(lot_id,)).fetchone():raise ValueError('Партия не найдена')
     code=request.form.get('sample_code','').strip()
     who=request.form.get('sampled_by','').strip()
     taken=request.form.get('sampled_at','').strip()
     if not code or len(code)>80 or not who or len(who)>120:raise ValueError('Укажите код пробы и отборщика')
     try:datetime.fromisoformat(taken)
     except ValueError:raise ValueError('Укажите корректную дату и время отбора')
     if datetime.fromisoformat(taken)>datetime.now()+timedelta(minutes=5):raise ValueError('Дата отбора в будущем')
     c.execute('INSERT INTO qc_samples(sample_code,lot_id,sampled_at,registered_at,registered_by,sampled_by,location) VALUES(?,?,?,?,?,?,?)',(code,lot_id,taken,now(),session['uid'],who,request.form.get('location','').strip()[:120]))
     audit(c,'qc.sample.create',f'{code} lot={lot_id}')
    elif action=='measure':
     sample_id=int(request.form['sample_id']);spec_id=int(request.form['spec_id'])
     sample=c.execute('SELECT s.*,l.kind,l.material_id,l.product_id FROM qc_samples s JOIN lots l ON l.id=s.lot_id WHERE s.id=?',(sample_id,)).fetchone()
     spec=c.execute('SELECT * FROM qc_specs WHERE id=? AND active=1',(spec_id,)).fetchone()
     if not sample or sample['status']!='open':raise ValueError('Проба не открыта')
     if not spec or spec['kind']!=sample['kind'] or spec['item_id']!=(sample['material_id'] if sample['kind']=='raw' else sample['product_id']):raise ValueError('Норма не соответствует пробе')
     value=float(request.form['value'])
     if not math.isfinite(value):raise ValueError('Некорректное измерение')
     passed=int((spec['min_value'] is None or value>=spec['min_value']) and (spec['max_value'] is None or value<=spec['max_value']))
     if c.execute('SELECT 1 FROM qc_sample_results WHERE sample_id=? AND spec_id=?',(sample_id,spec_id)).fetchone():raise ValueError('Измерение уже зарегистрировано; исправление требует новой пробы')
     c.execute('INSERT INTO qc_sample_results(sample_id,spec_id,value,passed,recorded_at,user_id,note) VALUES(?,?,?,?,?,?,?)',(sample_id,spec_id,value,passed,now(),session['uid'],request.form.get('note','').strip()[:500]))
     if not passed:c.execute("UPDATE lots SET status='quarantine' WHERE id=? AND status='released'",(sample['lot_id'],))
     audit(c,'qc.sample.measure',f'sample={sample_id} spec={spec_id} value={value} passed={passed}')
    elif action=='review':
     sample_id=int(request.form['sample_id'])
     sample=c.execute('SELECT s.*,l.kind,l.material_id,l.product_id FROM qc_samples s JOIN lots l ON l.id=s.lot_id WHERE s.id=?',(sample_id,)).fetchone()
     if not sample or sample['status']!='open':raise ValueError('Проба уже рассмотрена')
     item=sample['material_id'] if sample['kind']=='raw' else sample['product_id']
     specs=c.execute('SELECT * FROM qc_specs WHERE active=1 AND kind=? AND item_id=?',(sample['kind'],item)).fetchall()
     if not specs:raise ValueError('Нет действующих норм ОТК')
     readings=c.execute('SELECT * FROM qc_sample_results WHERE sample_id=?',(sample_id,)).fetchall()
     by_spec={r['spec_id']:r for r in readings}
     if any(sp['id'] not in by_spec for sp in specs):raise ValueError('Не все обязательные показатели измерены')
     if any(not by_spec[sp['id']]['passed'] for sp in specs):raise ValueError('Есть показатели вне нормы: пробу нельзя одобрить')
     # Copy approved results into existing QC history, preserving established manual release.
     for sp in specs:
      r=by_spec[sp['id']]
      c.execute('INSERT INTO qc_results(lot_id,spec_id,value,passed,recorded_at,user_id,comment) VALUES(?,?,?,?,?,?,?)',(sample['lot_id'],sp['id'],r['value'],1,now(),session['uid'],'Проба '+sample['sample_code']+'; измерено '+r['recorded_at']))
     c.execute("UPDATE qc_samples SET status='reviewed',reviewed_at=?,reviewed_by=?,review_note=? WHERE id=? AND status='open'",(now(),session['uid'],request.form.get('note','').strip()[:500],sample_id))
     audit(c,'qc.sample.review',f'sample={sample_id}; lot={sample["lot_id"]}')
    elif action=='reject':
     sample_id=int(request.form['sample_id'])
     reason=request.form.get('note','').strip()
     if not reason:raise ValueError('Укажите причину отклонения пробы')
     sample=c.execute("SELECT * FROM qc_samples WHERE id=? AND status='open'",(sample_id,)).fetchone()
     if not sample:raise ValueError('Проба уже рассмотрена')
     c.execute("UPDATE qc_samples SET status='rejected',reviewed_at=?,reviewed_by=?,review_note=? WHERE id=?",(now(),session['uid'],reason[:500],sample_id))
     c.execute("UPDATE lots SET status='quarantine' WHERE id=? AND status='released'",(sample['lot_id'],))
     audit(c,'qc.sample.reject',f'sample={sample_id}; {reason[:150]}')
    else:raise ValueError('Неизвестная операция')
   flash('Данные пробы сохранены')
  except (ValueError,sqlite3.IntegrityError) as e:
   flash('Ошибка: '+str(e))
  return redirect('/quality/samples')
 with conn() as c:
  lots=c.execute('SELECT id,lot_code,kind,material_id,product_id FROM lots ORDER BY id DESC LIMIT 500').fetchall()
  specs=c.execute('SELECT * FROM qc_specs WHERE active=1 ORDER BY kind,item_id,parameter').fetchall()
  samples=c.execute('SELECT s.*,l.lot_code,u.username AS registered_user FROM qc_samples s JOIN lots l ON l.id=s.lot_id JOIN users u ON u.id=s.registered_by ORDER BY s.id DESC LIMIT 200').fetchall()
  readings=c.execute('SELECT r.*,s.parameter,s.unit,q.sample_code FROM qc_sample_results r JOIN qc_specs s ON s.id=r.spec_id JOIN qc_samples q ON q.id=r.sample_id ORDER BY r.id DESC LIMIT 300').fetchall()
 return page('Пробы ОТК','''<div class="card"><h2>Регистрация лабораторных проб</h2><p>Пробы связаны с партией. Время отбора вводится с бумажной записи; время регистрации фиксируется сервером.</p>
 {% if has_role('admin','quality') %}<form method="post"><input type="hidden" name="action" value="create"><label>Партия<select name="lot_id">{% for l in lots %}<option value="{{l.id}}">{{l.lot_code}}</option>{% endfor %}</select></label><label>Код пробы<input name="sample_code" required maxlength="80"></label><label>Дата и время отбора<input name="sampled_at" type="datetime-local" required></label><label>Кто отобрал<input name="sampled_by" required maxlength="120"></label><label>Место отбора<input name="location" maxlength="120"></label><button>Зарегистрировать пробу</button></form>{% endif %}</div>
 <div class="card"><h2>Пробы</h2><table><tr><th>Проба / партия</th><th>Отбор</th><th>Статус</th><th>Рассмотрение</th></tr>{% for s in samples %}<tr><td>{{s.sample_code}} / {{s.lot_code}}</td><td>{{s.sampled_at}} · {{s.sampled_by}}</td><td>{{s.status}}</td><td>{{s.review_note}}</td></tr>{% endfor %}</table></div>
 {% if has_role('admin','quality') %}<div class="card"><h2>Результат испытания</h2><form method="post"><input type="hidden" name="action" value="measure"><label>Открытая проба<select name="sample_id">{% for s in samples if s.status=='open' %}<option value="{{s.id}}">{{s.sample_code}} · {{s.lot_code}}</option>{% endfor %}</select></label><label>Норма<select name="spec_id">{% for s in specs %}<option value="{{s.id}}">№{{s.id}} · {{s.kind}}/{{s.item_id}} · {{s.parameter}} ({{s.unit}})</option>{% endfor %}</select></label><label>Измерено<input name="value" type="number" step="any" required></label><label>Примечание<input name="note" maxlength="500"></label><button>Сохранить измерение</button></form><p class="muted">Выберите норму той же номенклатуры, что и проба. Повторное измерение оформляйте новой пробой.</p></div>
 <div class="card"><h2>Рассмотрение пробы</h2><form method="post"><label>Проба<select name="sample_id">{% for s in samples if s.status=='open' %}<option value="{{s.id}}">{{s.sample_code}}</option>{% endfor %}</select></label><label>Комментарий / причина отклонения<input name="note" maxlength="500"></label><button name="action" value="review">Подтвердить все показатели</button><button name="action" value="reject">Отклонить пробу</button></form><p class="muted">Подтверждение переносит результаты в действующий журнал ОТК, но НЕ выпускает партию из карантина автоматически.</p></div>{% endif %}
 <div class="card"><h2>Журнал измерений проб</h2><table><tr><th>Проба</th><th>Параметр</th><th>Значение</th><th>Оценка</th><th>Зарегистрировано</th></tr>{% for r in readings %}<tr><td>{{r.sample_code}}</td><td>{{r.parameter}}</td><td>{{r.value}} {{r.unit}}</td><td>{{'Норма' if r.passed else 'ВНЕ НОРМЫ'}}</td><td>{{r.recorded_at}}</td></tr>{% endfor %}</table></div>''',lots=lots,specs=specs,samples=samples,readings=readings)


def passport_eligible(c,lot_id):
 """Return immutable passport source data or raise a user-facing validation error."""
 lot=c.execute("""SELECT l.*,p.name AS product_name,p.unit AS product_unit
   FROM lots l JOIN products p ON p.id=l.product_id
   WHERE l.id=? AND l.kind='finished'""",(lot_id,)).fetchone()
 if not lot:raise ValueError('Партия готовой продукции не найдена')
 if lot['status']!='released':raise ValueError('Партия не разрешена ОТК')
 if lot['expires']<date.today().isoformat():raise ValueError('Срок годности партии истёк')
 specs=c.execute("SELECT * FROM qc_specs WHERE kind='finished' AND item_id=? AND active=1 ORDER BY id",(lot['product_id'],)).fetchall()
 if not specs:raise ValueError('Для продукции не настроены действующие нормы ОТК')
 sample=c.execute("""SELECT s.*,u.username AS reviewer FROM qc_samples s
   JOIN users u ON u.id=s.reviewed_by WHERE s.lot_id=? AND s.status='reviewed'
   ORDER BY s.id DESC LIMIT 1""",(lot_id,)).fetchone()
 if not sample:raise ValueError('Нет подтверждённой пробы ОТК')
 readings=c.execute('SELECT * FROM qc_sample_results WHERE sample_id=?',(sample['id'],)).fetchall()
 by_id={r['spec_id']:r for r in readings}
 results=[]
 for spec in specs:
  r=by_id.get(spec['id'])
  if r is None or not r['passed']:raise ValueError('Не все показатели последней подтверждённой пробы соответствуют нормам')
  value=r['value']
  if ((spec['min_value'] is not None and value<spec['min_value']) or
      (spec['max_value'] is not None and value>spec['max_value'])):
   raise ValueError('Нормы ОТК изменились после испытаний; требуется новая проба')
  results.append({'parameter':spec['parameter'],'unit':spec['unit'],
     'min':spec['min_value'],'max':spec['max_value'],'value':value,
     'recorded_at':r['recorded_at']})
 return {'lot_code':lot['lot_code'],'product':lot['product_name'],'unit':lot['product_unit'],
   'manufactured':lot['manufactured'],'expires':lot['expires'],
   'sample_code':sample['sample_code'],'sampled_at':sample['sampled_at'],
   'reviewed_at':sample['reviewed_at'],'reviewer':sample['reviewer'],
   'results':results,'decision':'Соответствует установленным показателям ОТК'}

@app.route('/quality/passports',methods=['GET','POST'])
@login_required
def quality_passports():
 if request.method=='POST':
  if not has_role('admin','quality'):abort(403)
  try:
   lot_id=int(request.form['lot_id'])
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    snapshot=passport_eligible(c,lot_id)
    sample=c.execute("SELECT id FROM qc_samples WHERE lot_id=? AND status='reviewed' ORDER BY id DESC LIMIT 1",(lot_id,)).fetchone()
    previous=c.execute('SELECT id FROM quality_passports WHERE lot_id=? AND sample_id=?',(lot_id,sample['id'])).fetchone()
    if previous:raise ValueError('Паспорт по этой пробе уже выпущен; используйте повторную печать')
    # Number is allocated inside the same SQLite write transaction.
    next_id=c.execute("SELECT COALESCE(MAX(id),0)+1 FROM quality_passports").fetchone()[0]
    passport_no='ПК-'+str(next_id).zfill(6)
    c.execute('INSERT INTO quality_passports(passport_no,lot_id,sample_id,issued_at,issued_by,snapshot_json) VALUES(?,?,?,?,?,?)',
       (passport_no,lot_id,sample['id'],now(),session['uid'],json.dumps(snapshot,ensure_ascii=False)))
    audit(c,'qc.passport.issue',f'{passport_no} lot={lot_id} sample={sample["id"]}')
   flash('Паспорт качества выпущен: '+passport_no)
  except (ValueError,sqlite3.IntegrityError) as e:flash('Ошибка: '+str(e))
  return redirect('/quality/passports')
 with conn() as c:
  lots=c.execute("""SELECT l.id,l.lot_code,p.name,l.status,l.expires FROM lots l
     JOIN products p ON p.id=l.product_id WHERE l.kind='finished' ORDER BY l.id DESC LIMIT 250""").fetchall()
  passports=c.execute("""SELECT q.id,q.passport_no,q.issued_at,l.lot_code,l.status AS current_status,
    u.username AS issuer FROM quality_passports q JOIN lots l ON l.id=q.lot_id
    JOIN users u ON u.id=q.issued_by ORDER BY q.id DESC LIMIT 250""").fetchall()
 return page('Паспорта качества','''<div class="card"><h2>Выпуск паспорта качества</h2>
 <p>Паспорт создаётся только для разрешённой и не просроченной партии готовой продукции с подтверждённой пробой ОТК. Результаты фиксируются в неизменяемом снимке на момент выпуска.</p>
 {% if has_role('admin','quality') %}<form method="post"><label>Партия<select name="lot_id" required>{% for l in lots %}<option value="{{l.id}}">{{l.lot_code}} · {{l.name}} · {{l.status}}</option>{% endfor %}</select></label><button>Выпустить паспорт</button></form>{% endif %}</div>
 <div class="card"><h2>Реестр паспортов</h2><table><tr><th>Номер</th><th>Партия</th><th>Дата выпуска</th><th>Ответственный</th><th>Текущий статус партии</th><th>Документ</th></tr>
 {% for p in passports %}<tr><td>{{p.passport_no}}</td><td>{{p.lot_code}}</td><td>{{p.issued_at}}</td><td>{{p.issuer}}</td><td>{{p.current_status}}</td><td><a href="/quality/passports/{{p.id}}/print" target="_blank" rel="noopener">Открыть / печать / PDF</a></td></tr>{% endfor %}</table></div>''',lots=lots,passports=passports)

@app.get('/quality/passports/<int:passport_id>/print')
@login_required
def quality_passport_print(passport_id):
 with conn() as c:
  row=c.execute("""SELECT q.*,u.username AS issuer,l.status AS current_status,l.expires AS current_expires
    FROM quality_passports q JOIN users u ON u.id=q.issued_by
    JOIN lots l ON l.id=q.lot_id WHERE q.id=?""",(passport_id,)).fetchone()
 if not row:abort(404)
 data=json.loads(row['snapshot_json'])
 # Historic reprints remain historically accurate and prominently show current status.
 warning=row['current_status']!='released' or row['current_expires']<date.today().isoformat()
 return render_template_string('''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{row.passport_no}} · lovetech</title>
 <style>@page{size:A4;margin:17mm}body{font:14px/1.5 Arial,sans-serif;color:#182739;max-width:190mm;margin:20px auto}h1{font-size:22px;margin:0}h2{font-size:17px}table{border-collapse:collapse;width:100%;margin:18px 0}td,th{border:1px solid #8c9bab;padding:8px;text-align:left}th{background:#edf2f7}.top{display:flex;justify-content:space-between;gap:16px}.notice{border:2px solid #a12727;color:#a12727;padding:10px;margin:16px 0}.muted{color:#526273}.actions{margin:14px 0}button{padding:10px 18px;cursor:pointer}@media print{.actions{display:none}body{margin:0;max-width:none}}</style>
 <div class="actions"><button onclick="window.print()">Печать / Сохранить в PDF</button> <a href="/quality/passports">К реестру</a></div>
 <div class="top"><div><b>lovetech</b><h1>ПАСПОРТ КАЧЕСТВА</h1><div>{{row.passport_no}}</div></div><div><b>Дата выпуска:</b> {{row.issued_at}}<div class="muted">Повторная печать архивного документа</div></div></div>
 {% if warning %}<div class="notice"><b>ВНИМАНИЕ:</b> текущий статус партии — {{row.current_status}}; годна до {{row.current_expires}}. Этот архивный паспорт НЕ подтверждает текущую пригодность партии.</div>{% endif %}
 <table><tr><th>Продукция</th><td>{{d.product}}</td></tr><tr><th>Партия</th><td>{{d.lot_code}}</td></tr><tr><th>Изготовлено</th><td>{{d.manufactured}}</td></tr><tr><th>Годен до</th><td>{{d.expires}}</td></tr><tr><th>Проба</th><td>{{d.sample_code}} · отбор {{d.sampled_at}}</td></tr></table>
 <h2>Результаты испытаний</h2><table><tr><th>Показатель</th><th>Допустимый диапазон</th><th>Фактическое значение</th></tr>
 {% for r in d.results %}<tr><td>{{r.parameter}}</td><td>{{'от '+(r.min|string) if r.min is not none else '—'}} / {{'до '+(r.max|string) if r.max is not none else '—'}} {{r.unit}}</td><td>{{r.value}} {{r.unit}}</td></tr>{% endfor %}</table>
 <p><b>Заключение:</b> {{d.decision}}</p><p><b>Проверка ОТК:</b> {{d.reviewer}} · {{d.reviewed_at}}</p>
 <p><b>Оформил в системе:</b> {{row.issuer}}</p><p class="muted">Документ сформирован по подтверждённой пробе на момент выпуска. Подпись уполномоченного лица на бумажном экземпляре: __________________</p>
 </html>''',row=row,d=data,warning=warning)

@app.get('/stock-alerts')
@login_required
def stock_alerts():
 # Read-only inventory risk register
 days_text=request.args.get('days','30')
 try:
  days=int(days_text)
  if not 0 <= days <= 365:raise ValueError()
 except ValueError:
  abort(400,description='Период должен быть от 0 до 365 дней')
 today=date.today()
 cutoff=(today+timedelta(days=days)).isoformat()
 with conn() as c:
  rows=c.execute('''SELECT l.id,l.lot_code,l.kind,l.qty,l.expires,l.status,
                    COALESCE(m.name,p.name) AS name,
                    COALESCE(m.unit,p.unit) AS unit
                    FROM lots l LEFT JOIN materials m ON m.id=l.material_id
                    LEFT JOIN products p ON p.id=l.product_id
                    WHERE l.qty>0 AND l.expires<=?
                    ORDER BY l.expires,l.lot_code''',(cutoff,)).fetchall()
  totals=c.execute('''SELECT l.kind,COALESCE(m.name,p.name) AS name,
                      COALESCE(m.unit,p.unit) AS unit,
                      SUM(l.qty) AS total_qty,
                      SUM(CASE WHEN l.status='released' AND l.expires>=? THEN l.qty ELSE 0 END) AS available_qty
                      FROM lots l LEFT JOIN materials m ON m.id=l.material_id
                      LEFT JOIN products p ON p.id=l.product_id
                      WHERE l.qty>0 GROUP BY l.kind,l.material_id,l.product_id
                      ORDER BY l.kind,name''',(today.isoformat(),)).fetchall()
 return page('Контроль сроков и остатков','''<div class="card"><p>Отчёт показывает партии с положительным остатком, срок которых уже истёк или истечёт в выбранный период. Доступный остаток — только разрешённые ОТК и не просроченные партии.</p><form method="get"><label>Горизонт контроля, дней<input type="number" name="days" min="0" max="365" value="{{days}}" required></label><button>Обновить</button></form></div>
 <div class="card"><h2>Партии под контролем: {{rows|length}}</h2><table><tr><th>Партия</th><th>Номенклатура</th><th>Остаток</th><th>Годен до</th><th>Риск</th><th>Статус ОТК</th></tr>{% for l in rows %}<tr><td><a href="/trace?lot={{l.lot_code|urlencode}}">{{l.lot_code}}</a></td><td>{{l.name}} · {{'Сырьё' if l.kind=='raw' else 'ГП'}}</td><td>{{'%.3f'|format(l.qty)}} {{l.unit}}</td><td>{{l.expires}}</td><td>{% if l.expires<today %}<span class="danger">Просрочена</span>{% elif l.expires==today %}<span class="danger">Истекает сегодня</span>{% else %}Скоро истекает{% endif %}</td><td>{{l.status}}</td></tr>{% else %}<tr><td colspan="6">Партий под контролем нет</td></tr>{% endfor %}</table></div>
 <div class="card"><h2>Сводка остатков</h2><table><tr><th>Тип</th><th>Номенклатура</th><th>Общий остаток</th><th>Доступно к использованию</th></tr>{% for x in totals %}<tr><td>{{'Сырьё' if x.kind=='raw' else 'ГП'}}</td><td>{{x.name}}</td><td>{{'%.3f'|format(x.total_qty)}} {{x.unit}}</td><td>{{'%.3f'|format(x.available_qty)}} {{x.unit}}</td></tr>{% else %}<tr><td colspan="4">Остатков нет</td></tr>{% endfor %}</table></div>''',days=days,rows=rows,totals=totals)

@app.route('/work-orders',methods=['GET','POST'])
@login_required
def work_orders():
 if request.method=='POST':
  action=request.form.get('action','')
  try:
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    if action=='create':
     if not has_role('admin','technologist'):abort(403)
     rid=int(request.form['recipe_id']);qty=number(request.form,'planned_qty')
     code=request.form.get('lot_code','').strip();due=request.form.get('due_date','').strip()
     if not code or len(code)>100:raise ValueError('Укажите номер партии (до 100 символов)')
     if date.fromisoformat(due)<date.today():raise ValueError('Дата выпуска не может быть в прошлом')
     if not c.execute("SELECT 1 FROM recipes WHERE id=? AND status='approved'",(rid,)).fetchone():raise ValueError('Рецептура не утверждена')
     cur=c.execute('INSERT INTO work_orders(recipe_id,planned_qty,lot_code,due_date,notes,created_at,created_by) VALUES(?,?,?,?,?,?,?)',(rid,qty,code,due,request.form.get('notes','').strip()[:1000],now(),session['uid']))
     audit(c,'work_order.create','order='+str(cur.lastrowid))
     flash('Производственное задание создано как черновик')
    elif action in ('reserve','unreserve'):
     if not has_role('admin','technologist','warehouse'):abort(403)
     oid=int(request.form['order_id'])
     order=c.execute("SELECT * FROM work_orders WHERE id=? AND status='approved'",(oid,)).fetchone()
     if not order:raise ValueError('Резервировать можно только утверждённое задание')
     if c.execute("SELECT 1 FROM tsd_picks WHERE order_id=? AND status IN ('staged','issued') LIMIT 1",(oid,)).fetchone():raise ValueError('Есть подбор/выдача тары: сначала завершите задание или отмените подбор')
     if action=='unreserve':
      c.execute('DELETE FROM reservations WHERE order_id=?',(oid,))
     else:
      if c.execute('SELECT 1 FROM reservations WHERE order_id=?',(oid,)).fetchone():raise ValueError('Сначала снимите прежний резерв')
      items=c.execute('SELECT material_id,percent FROM recipe_items WHERE recipe_id=?',(order['recipe_id'],)).fetchall()
      if not items:raise ValueError('В рецептуре нет компонентов')
      for item in items:
       needed=round(order['planned_qty']*item['percent']/100,8)
       lots=c.execute("""SELECT l.id,l.qty,l.expires,COALESCE(SUM(rv.qty),0) reserved
        FROM lots l LEFT JOIN reservations rv ON rv.lot_id=l.id
        WHERE l.kind='raw' AND l.material_id=? AND l.status='released' AND l.expires>=?
        GROUP BY l.id ORDER BY l.expires,l.id""",(item['material_id'],order['due_date'])).fetchall()
       for lot in lots:
        take=round(min(needed,max(0,lot['qty']-lot['reserved'])),8)
        if take>0:
         c.execute('INSERT INTO reservations(order_id,lot_id,qty,created_at) VALUES(?,?,?,?)',(oid,lot['id'],take,now()))
         needed=round(needed-take,8)
        if needed<=0.000001:break
       if needed>0.000001:raise ValueError('Недостаточно свободного сырья ID '+str(item['material_id']))
     audit(c,'work_order.'+action,'order='+str(oid))
     flash('Резервы обновлены')
    elif action in ('approve','cancel'):
     if not has_role('admin','technologist'):abort(403)
     oid=int(request.form['order_id'])
     row=c.execute('SELECT * FROM work_orders WHERE id=?',(oid,)).fetchone()
     if not row:raise ValueError('Задание не найдено')
     if action=='approve':
      if row['status']!='draft':raise ValueError('Можно утверждать только черновик')
      if not c.execute("SELECT 1 FROM recipes WHERE id=? AND status='approved'",(row['recipe_id'],)).fetchone():raise ValueError('Рецептура больше не утверждена')
      c.execute("UPDATE work_orders SET status='approved',approved_by=? WHERE id=?",(session['uid'],oid))
     else:
      if row['status'] not in ('draft','approved'):raise ValueError('Отменить можно только невыполненное задание')
      c.execute("DELETE FROM reservations WHERE order_id=?",(oid,))
      c.execute("UPDATE work_orders SET status='cancelled' WHERE id=?",(oid,))
     audit(c,'work_order.'+action,'order='+str(oid))
     flash('Статус задания обновлён')
    else:raise ValueError('Неизвестная операция')
  except Exception as e:return fail(e)
  return redirect('/work-orders')
 with conn() as c:
  recipes=c.execute("SELECT r.id,r.version,p.name FROM recipes r JOIN products p ON p.id=r.product_id WHERE r.status='approved' ORDER BY p.name,r.version DESC").fetchall()
  orders=c.execute("""SELECT o.*,p.name,r.version,b.actual_qty FROM work_orders o
   JOIN recipes r ON r.id=o.recipe_id JOIN products p ON p.id=r.product_id
   LEFT JOIN batches b ON b.id=o.batch_id ORDER BY o.id DESC LIMIT 200""").fetchall()
 return page('Производственные задания',"""<div class="card"><p>Планирование выпуска: создание задания, утверждение технологом, выполнение оператором. Резервирование утверждённых заданий по FEFO; при выполнении резерв списывается, при отмене освобождается.</p></div>
 {% if has_role('admin','technologist') %}<div class="card"><h2>Новое задание</h2><form method="post"><input type="hidden" name="action" value="create"><label>Рецептура<select name="recipe_id" required>{% for r in recipes %}<option value="{{r.id}}">{{r.name}} · v{{r.version}}</option>{% endfor %}</select></label><label>Плановое количество<input type="number" name="planned_qty" step="any" min="0.000001" required></label><label>Номер будущей партии<input name="lot_code" maxlength="100" required></label><label>Дата выпуска<input type="date" name="due_date" value="{{today}}" required></label><label>Примечание<input name="notes" maxlength="1000"></label><button>Создать черновик</button></form></div>{% endif %}
 <div class="card"><h2>Задания</h2><table><tr><th>№</th><th>Продукт</th><th>Партия</th><th>План</th><th>Срок</th><th>Статус</th><th>Действия</th></tr>{% for o in orders %}<tr><td>{{o.id}}</td><td>{{o.name}} · v{{o.version}}</td><td>{{o.lot_code}}</td><td>{{o.planned_qty}}</td><td>{{o.due_date}}</td><td>{{{'draft':'Черновик','approved':'Утверждено','completed':'Выполнено','cancelled':'Отменено'}[o.status]}}</td><td>{% if o.status=='draft' and has_role('admin','technologist') %}<form method="post"><input type="hidden" name="action" value="approve"><input type="hidden" name="order_id" value="{{o.id}}"><button>Утвердить</button></form>{% endif %}{% if o.status in ['draft','approved'] and has_role('admin','technologist') %}<form method="post"><input type="hidden" name="action" value="cancel"><input type="hidden" name="order_id" value="{{o.id}}"><button>Отменить</button></form>{% endif %}{% if o.status=='approved' and has_role('admin','technologist','warehouse') %}<form method="post"><input type="hidden" name="action" value="reserve"><input type="hidden" name="order_id" value="{{o.id}}"><button>Резервировать</button></form><form method="post"><input type="hidden" name="action" value="unreserve"><input type="hidden" name="order_id" value="{{o.id}}"><button>Снять резерв</button></form>{% endif %}{% if o.status=='approved' and has_role('admin','operator') %}<a href="/production?order_id={{o.id}}">Выполнить</a>{% endif %}{% if o.batch_id %}<a href="/trace?lot={{o.lot_code|urlencode}}">Прослеживаемость</a>{% endif %}</td></tr>{% else %}<tr><td colspan="7">Заданий пока нет</td></tr>{% endfor %}</table></div>""",recipes=recipes,orders=orders)

@app.get('/planning')
@login_required
def planning():
 """Read-only material requirements planning with free stock net of reservations."""
 horizon=request.args.get('days','30')
 try: days=int(horizon)
 except (TypeError,ValueError):days=30
 days=max(1,min(days,365))
 today=date.today().isoformat()
 cutoff=(date.today()+timedelta(days=days)).isoformat()
 with conn() as c:
  orders=c.execute("""SELECT o.id,o.due_date,o.planned_qty,o.lot_code,p.name AS product_name,r.version
   FROM work_orders o JOIN recipes r ON r.id=o.recipe_id
   JOIN products p ON p.id=r.product_id
   WHERE o.status='approved' AND o.due_date<=?
   ORDER BY o.due_date,o.id""",(cutoff,)).fetchall()
  needs=c.execute("""SELECT m.id AS material_id,m.name,m.unit,
   SUM(o.planned_qty*i.percent/100.0) AS required_qty
   FROM work_orders o JOIN recipe_items i ON i.recipe_id=o.recipe_id
   JOIN materials m ON m.id=i.material_id
   WHERE o.status='approved' AND o.due_date<=?
   GROUP BY m.id,m.name,m.unit ORDER BY m.name""",(cutoff,)).fetchall()
  available=c.execute("""SELECT l.material_id,SUM(l.qty-COALESCE(rv.reserved,0)) AS available_qty
   FROM lots l LEFT JOIN (SELECT lot_id,SUM(qty) reserved FROM reservations GROUP BY lot_id) rv ON rv.lot_id=l.id
   WHERE l.kind='raw' AND l.status='released' AND l.expires>=? AND l.qty>0
   GROUP BY l.material_id""",(today,)).fetchall()
  stock={r['material_id']:r['available_qty'] for r in available}
  lines=[]
  for item in needs:
   required=round(item['required_qty'],4)
   free=round(stock.get(item['material_id'],0),4)
   lines.append(dict(name=item['name'],unit=item['unit'],required=required,
     available=free,shortage=round(max(0,required-free),4)))
  # Advisory shortage: reservations are excluded from stock but included in total demand.
  late=sum(1 for o in orders if o['due_date']<today)
 return page('План производства и потребность в сырье',"""
 <div class="card"><p>План по утверждённым невыполненным заданиям. Потребность рассчитана по процентам рецептуры. Остатки учитываются только по разрешённым ОТК и не просроченным партиям на сегодня.</p>
 <p class="muted">Это предварительный MRP-отчёт: свободный остаток исключает действующие резервы. Потребность включает все утверждённые задания, включая зарезервированные; дефицит может быть завышен для таких заданий. Будущие поставки не учитываются. Перед выпуском остатки проверяются повторно.</p>
 <form method="get"><label>Горизонт планирования, дней (1–365)<input type="number" min="1" max="365" name="days" value="{{days}}" required></label><button>Пересчитать</button></form>
 <p>Заданий: <strong>{{orders|length}}</strong> · Просроченных по плану: <strong>{{late}}</strong></p></div>
 <div class="card"><h2>Потребность в сырье</h2><table><tr><th>Сырьё</th><th>Потребность</th><th>Годный разрешённый остаток</th><th>Дефицит</th></tr>
 {% for r in lines %}<tr><td>{{r.name}}</td><td>{{'%.3f'|format(r.required)}} {{r.unit}}</td><td>{{'%.3f'|format(r.available)}} {{r.unit}}</td><td>{% if r.shortage>0 %}<span class="danger">{{'%.3f'|format(r.shortage)}} {{r.unit}}</span>{% else %}0{% endif %}</td></tr>
 {% else %}<tr><td colspan="4">В выбранном периоде потребности нет</td></tr>{% endfor %}</table></div>
 <div class="card"><h2>Календарный список заданий</h2><table><tr><th>Дата</th><th>Задание</th><th>Продукт</th><th>Партия</th><th>План</th></tr>
 {% for o in orders %}<tr><td>{% if o.due_date<today %}<span class="danger">{{o.due_date}}</span>{% else %}{{o.due_date}}{% endif %}</td><td>#{{o.id}}</td><td>{{o.product_name}} · v{{o.version}}</td><td>{{o.lot_code}}</td><td>{{'%.3f'|format(o.planned_qty)}}</td></tr>
 {% else %}<tr><td colspan="5">Утверждённых заданий нет</td></tr>{% endfor %}</table></div>
 """,days=days,orders=orders,lines=lines,late=late)

@app.route('/supply',methods=['GET','POST'])
@login_required
def supply():
 if request.method=='POST':
  if not has_role('admin','technologist','warehouse'):abort(403)
  try:
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    action=request.form.get('action')
    if action=='create':
     material_id=int(request.form['material_id']);qty=number(request.form,'qty')
     needed_by=date.fromisoformat(request.form['needed_by']).isoformat()
     if needed_by<date.today().isoformat():raise ValueError('Дата потребности уже прошла')
     if not c.execute('SELECT 1 FROM materials WHERE id=?',(material_id,)).fetchone():raise ValueError('Сырьё не найдено')
     cur=c.execute('INSERT INTO purchase_requests(material_id,qty,needed_by,note,created_at,created_by) VALUES(?,?,?,?,?,?)',
      (material_id,qty,needed_by,request.form.get('note','').strip()[:500],now(),session['uid']))
     audit(c,'purchase_request.create','request='+str(cur.lastrowid))
    elif action=='status':
     rid=int(request.form['request_id']);status=request.form['status']
     if status not in ('open','ordered','closed','cancelled'):raise ValueError('Неверный статус')
     if c.execute('UPDATE purchase_requests SET status=? WHERE id=?',(status,rid)).rowcount!=1:raise ValueError('Заявка не найдена')
     audit(c,'purchase_request.status','request='+str(rid)+'; status='+status)
    else:raise ValueError('Неизвестная операция')
   flash('Заявка сохранена')
  except Exception as e:return fail(e)
  return redirect('/supply')
 with conn() as c:
  materials=c.execute('SELECT id,name,unit FROM materials ORDER BY name').fetchall()
  requests=c.execute('SELECT pr.*,m.name,m.unit FROM purchase_requests pr JOIN materials m ON m.id=pr.material_id ORDER BY pr.id DESC LIMIT 200').fetchall()
  reservations=c.execute("""SELECT rv.order_id,l.lot_code,m.name,m.unit,rv.qty,o.due_date
   FROM reservations rv JOIN lots l ON l.id=rv.lot_id JOIN materials m ON m.id=l.material_id
   JOIN work_orders o ON o.id=rv.order_id ORDER BY o.due_date,rv.order_id,l.expires""").fetchall()
 return page('Резервы и снабжение',"""
 <div class="card"><h2>Резервы сырья по партиям</h2><p>Резервы создаются в разделе «Производственные задания». Зарезервированное сырьё исключается из свободного остатка при выпуске.</p>
 <table><tr><th>Задание</th><th>Сырьё</th><th>Партия</th><th>Количество</th><th>Дата выпуска</th></tr>
 {% for r in reservations %}<tr><td>#{{r.order_id}}</td><td>{{r.name}}</td><td>{{r.lot_code}}</td><td>{{'%.3f'|format(r.qty)}} {{r.unit}}</td><td>{{r.due_date}}</td></tr>{% else %}<tr><td colspan="5">Резервов нет</td></tr>{% endfor %}</table></div>
 {% if has_role('admin','technologist','warehouse') %}<div class="card"><h2>Новая заявка на закупку</h2><form method="post"><input type="hidden" name="action" value="create">
 <label>Сырьё<select name="material_id" required>{% for m in materials %}<option value="{{m.id}}">{{m.name}} ({{m.unit}})</option>{% endfor %}</select></label>
 <label>Количество<input type="number" name="qty" min="0.000001" step="any" required></label><label>Требуется к дате<input type="date" name="needed_by" value="{{today}}" required></label>
 <label>Комментарий<input name="note" maxlength="500"></label><button>Создать заявку</button></form></div>{% endif %}
 <div class="card"><h2>Заявки на закупку</h2><table><tr><th>№</th><th>Сырьё</th><th>Количество</th><th>К дате</th><th>Статус</th><th>Комментарий</th><th>Действие</th></tr>
 {% for r in requests %}<tr><td>#{{r.id}}</td><td>{{r.name}}</td><td>{{'%.3f'|format(r.qty)}} {{r.unit}}</td><td>{{r.needed_by}}</td><td>{{r.status}}</td><td>{{r.note}}</td><td>{% if has_role('admin','technologist','warehouse') %}<form method="post"><input type="hidden" name="action" value="status"><input type="hidden" name="request_id" value="{{r.id}}"><select name="status"><option value="open">Открыта</option><option value="ordered">Заказано</option><option value="closed">Закрыта</option><option value="cancelled">Отменена</option></select><button>Сохранить</button></form>{% endif %}</td></tr>
 {% else %}<tr><td colspan="7">Заявок нет</td></tr>{% endfor %}</table></div>""",materials=materials,requests=requests,reservations=reservations)

@app.route('/procurement',methods=['GET','POST'])
@login_required
def procurement():
 if request.method=='POST':
  if not has_role('admin','warehouse','technologist'):abort(403)
  try:
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    action=request.form.get('action')
    if action=='supplier':
     if not has_role('admin'):abort(403)
     name=request.form.get('name','').strip()[:150]
     if not name:raise ValueError('Укажите поставщика')
     cur=c.execute('INSERT INTO suppliers(name,contact,email,phone) VALUES(?,?,?,?)',
      (name,request.form.get('contact','').strip()[:150],request.form.get('email','').strip()[:150],request.form.get('phone','').strip()[:80]))
     audit(c,'supplier.create',f'supplier={cur.lastrowid}')
    elif action=='order':
     if not has_role('admin','technologist'):abort(403)
     supplier_id=int(request.form['supplier_id']);material_id=int(request.form['material_id']);qty=number(request.form,'qty')
     due=date.fromisoformat(request.form['due_date']).isoformat()
     if not c.execute('SELECT 1 FROM suppliers WHERE id=? AND active=1',(supplier_id,)).fetchone():raise ValueError('Поставщик не найден')
     if not c.execute('SELECT 1 FROM materials WHERE id=?',(material_id,)).fetchone():raise ValueError('Сырьё не найдено')
     rid=int(request.form['request_id']) if request.form.get('request_id') else None
     if rid:
      pr=c.execute('SELECT material_id,status FROM purchase_requests WHERE id=?',(rid,)).fetchone()
      if not pr or pr['material_id']!=material_id or pr['status'] not in ('open','ordered'):raise ValueError('Заявка не соответствует сырью или закрыта')
     cur=c.execute('INSERT INTO purchase_orders(request_id,supplier_id,material_id,qty,due_date,created_at,created_by,note) VALUES(?,?,?,?,?,?,?,?)',
      (rid,supplier_id,material_id,qty,due,now(),session['uid'],request.form.get('note','').strip()[:500]))
     if rid:c.execute("UPDATE purchase_requests SET status='ordered' WHERE id=?",(rid,))
     audit(c,'purchase_order.create',f'order={cur.lastrowid}; request={rid}')
    elif action=='receive':
     if not has_role('admin','warehouse'):abort(403)
     order_id=int(request.form['order_id']);qty=number(request.form,'qty')
     order=c.execute("""SELECT po.*,m.shelf_days,s.name supplier_name FROM purchase_orders po
      JOIN materials m ON m.id=po.material_id JOIN suppliers s ON s.id=po.supplier_id WHERE po.id=?""",(order_id,)).fetchone()
     if not order or order['status'] not in ('ordered','partial'):raise ValueError('Заказ недоступен для приёмки')
     if qty>order['qty']-order['received_qty']+1e-8:raise ValueError('Приход превышает остаток заказа')
     manufactured=date.fromisoformat(request.form['manufactured'])
     expires=(manufactured+timedelta(days=order['shelf_days'])).isoformat()
     code=request.form.get('lot_code','').strip()[:120]
     if not code:raise ValueError('Укажите номер партии')
     cur=c.execute("INSERT INTO lots(kind,material_id,lot_code,qty,manufactured,expires,status,supplier,created_at) VALUES('raw',?,?,?,?,?,'quarantine',?,?)",
      (order['material_id'],code,qty,manufactured.isoformat(),expires,order['supplier_name'],now()))
     lot_id=cur.lastrowid
     c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',
      (lot_id,qty,f'Приход по заказу #{order_id}',now(),session['uid']))
     c.execute('INSERT INTO purchase_receipts(order_id,lot_id,qty,received_at,received_by) VALUES(?,?,?,?,?)',
      (order_id,lot_id,qty,now(),session['uid']))
     new_total=order['received_qty']+qty
     status='received' if new_total>=order['qty']-1e-8 else 'partial'
     c.execute('UPDATE purchase_orders SET received_qty=?,status=? WHERE id=?',(new_total,status,order_id))
     if status=='received' and order['request_id']:
      # Close the request only when all linked orders have been fully received.
      remaining=c.execute("SELECT COUNT(*) n FROM purchase_orders WHERE request_id=? AND status NOT IN ('received','cancelled')",(order['request_id'],)).fetchone()['n']
      if not remaining:c.execute("UPDATE purchase_requests SET status='closed' WHERE id=?",(order['request_id'],))
     audit(c,'purchase_receipt.create',f'order={order_id}; lot={lot_id}; qty={qty}')
    elif action=='cancel':
     if not has_role('admin','technologist'):abort(403)
     oid=int(request.form['order_id'])
     if c.execute("UPDATE purchase_orders SET status='cancelled' WHERE id=? AND status='ordered' AND received_qty=0",(oid,)).rowcount!=1:
      raise ValueError('Отменить можно только заказ без приходов')
     audit(c,'purchase_order.cancel',f'order={oid}')
    else:raise ValueError('Неизвестная операция')
   flash('Операция сохранена')
  except Exception as e:return fail(e)
  return redirect('/procurement')
 with conn() as c:
  suppliers=c.execute('SELECT * FROM suppliers WHERE active=1 ORDER BY name').fetchall()
  materials=c.execute('SELECT id,name,unit FROM materials ORDER BY name').fetchall()
  requests=c.execute("SELECT pr.id,m.name,m.id material_id,pr.qty,pr.needed_by FROM purchase_requests pr JOIN materials m ON m.id=pr.material_id WHERE pr.status IN ('open','ordered') ORDER BY pr.id DESC").fetchall()
  orders=c.execute("""SELECT po.*,s.name supplier_name,m.name material_name,m.unit FROM purchase_orders po
   JOIN suppliers s ON s.id=po.supplier_id JOIN materials m ON m.id=po.material_id ORDER BY po.id DESC LIMIT 200""").fetchall()
  receipts=c.execute("""SELECT r.*,l.lot_code,po.id order_number FROM purchase_receipts r
   JOIN lots l ON l.id=r.lot_id JOIN purchase_orders po ON po.id=r.order_id ORDER BY r.id DESC LIMIT 100""").fetchall()
 return page('Закупки и поставщики',"""
 {% if has_role('admin') %}<div class="card"><h2>Добавить поставщика</h2><form method="post"><input type="hidden" name="action" value="supplier">
 <label>Наименование<input name="name" maxlength="150" required></label><label>Контакт<input name="contact"></label><label>Email<input name="email" type="email"></label><label>Телефон<input name="phone"></label><button>Добавить</button></form></div>{% endif %}
 {% if has_role('admin','technologist') %}<div class="card"><h2>Заказ поставщику</h2><form method="post"><input type="hidden" name="action" value="order">
 <label>Поставщик<select name="supplier_id" required>{% for s in suppliers %}<option value="{{s.id}}">{{s.name}}</option>{% endfor %}</select></label>
 <label>Сырьё<select name="material_id" required>{% for m in materials %}<option value="{{m.id}}">{{m.name}} ({{m.unit}})</option>{% endfor %}</select></label>
 <label>Количество<input name="qty" type="number" step="any" min="0.000001" required></label>
 <label>Ожидается<input name="due_date" type="date" value="{{today}}" required></label>
 <label>Заявка (необязательно)<select name="request_id"><option value="">Без заявки</option>{% for r in requests %}<option value="{{r.id}}">#{{r.id}} — {{r.name}}, {{r.qty}}</option>{% endfor %}</select></label>
 <label>Примечание<input name="note" maxlength="500"></label><button>Создать заказ</button></form><p class="muted">Если выбираете заявку, сырьё должно совпадать с сырьём в заявке.</p></div>{% endif %}
 <div class="card"><h2>Заказы поставщикам</h2><table><tr><th>№</th><th>Поставщик</th><th>Сырьё</th><th>Заказано</th><th>Получено</th><th>Дата</th><th>Статус</th><th>Действие</th></tr>
 {% for o in orders %}<tr><td>#{{o.id}}</td><td>{{o.supplier_name}}</td><td>{{o.material_name}}</td><td>{{'%.3f'|format(o.qty)}} {{o.unit}}</td><td>{{'%.3f'|format(o.received_qty)}}</td><td>{{o.due_date}}</td><td>{{o.status}}</td><td>
 {% if has_role('admin','warehouse') and o.status in ['ordered','partial'] %}<form method="post"><input type="hidden" name="action" value="receive"><input type="hidden" name="order_id" value="{{o.id}}"><label>Партия<input name="lot_code" required></label><label>Количество<input type="number" name="qty" step="any" min="0.000001" max="{{o.qty-o.received_qty}}" required></label><label>Изготовлено<input type="date" name="manufactured" value="{{today}}" required></label><button>Принять на карантин</button></form>{% endif %}
 {% if has_role('admin','technologist') and o.status=='ordered' and o.received_qty==0 %}<form method="post"><input type="hidden" name="action" value="cancel"><input type="hidden" name="order_id" value="{{o.id}}"><button>Отменить</button></form>{% endif %}</td></tr>{% else %}<tr><td colspan="8">Заказов нет</td></tr>{% endfor %}</table></div>
 <div class="card"><h2>История приёмок</h2><table><tr><th>Заказ</th><th>Партия</th><th>Количество</th><th>Дата</th></tr>{% for r in receipts %}<tr><td>#{{r.order_number}}</td><td><a href="/trace?lot={{r.lot_code|urlencode}}">{{r.lot_code}}</a></td><td>{{'%.3f'|format(r.qty)}}</td><td>{{r.received_at}}</td></tr>{% else %}<tr><td colspan="4">Приёмок нет</td></tr>{% endfor %}</table></div>
 """,suppliers=suppliers,materials=materials,requests=requests,orders=orders,receipts=receipts)


# v8: printable warehouse labels. Labels are internal inventory identification,
# not a substitute for statutory chemical hazard labelling (GHS/CLP).
LABEL_WARNING_DAYS = 30

def label_state(lot, today=None, warning_days=LABEL_WARNING_DAYS):
    today = today or date.today()
    expiry = date.fromisoformat(lot['expires'])
    if expiry < today:
        return ('expired', 'ПРОСРОЧЕНО', 'Использование и отгрузка запрещены')
    if lot['status'] == 'blocked':
        return ('blocked', 'ЗАБЛОКИРОВАНО', 'Использование и отгрузка запрещены')
    if lot['status'] != 'released':
        return ('quarantine', 'КАРАНТИН', 'Использование запрещено до разрешения ОТК')
    days = (expiry - today).days
    if days <= warning_days:
        return ('warning', 'ИСТЕКАЕТ СРОК', f'Осталось {days} дн. · ОТК: разрешено')
    return ('released', 'РАЗРЕШЕНО', 'ОТК: разрешено к использованию')


def label_qr_svg(lot_code):
    # PUBLIC_BASE_URL must be set by the administrator to a trusted HTTPS origin.
    # Without it, the QR contains a stable machine-readable lot identifier.
    public_base = os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/')
    if public_base and not public_base.startswith('https://'):
        public_base = ''
    payload = (public_base + '/trace?lot=' + quote(lot_code, safe='')) if public_base else 'CHEMPRO:LOT:' + lot_code
    qr = qrcode.QRCode(box_size=4, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(payload)
    qr.make(fit=True)
    buffer = io.BytesIO()
    qr.make_image(image_factory=SvgPathImage).save(buffer)
    return 'data:image/svg+xml;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


@app.get('/labels')
@login_required
def labels_index():
    with conn() as c:
        rows = c.execute('''SELECT l.*,COALESCE(m.name,p.name) AS name,
                     COALESCE(m.unit,p.unit) AS unit
                     FROM lots l LEFT JOIN materials m ON m.id=l.material_id
                     LEFT JOIN products p ON p.id=l.product_id
                     ORDER BY l.id DESC LIMIT 250''').fetchall()
    return page('Этикетки партий', '''<div class="card"><p>Печать внутренних этикеток сырья и готовой продукции. Статус вычисляется по данным партии на момент печати.</p><p class="muted">Внимание: внутренние этикетки не заменяют обязательную маркировку химической опасности.</p><table><tr><th>Партия</th><th>Наименование</th><th>Тип</th><th>Статус ОТК</th><th>Печать</th></tr>{% for l in rows %}<tr><td>{{l.lot_code}}</td><td>{{l.name}}</td><td>{{'Сырьё' if l.kind=='raw' else 'Продукция'}}</td><td>{{l.status}}</td><td><a href="/labels/{{l.id}}">Открыть макет</a></td></tr>{% endfor %}</table></div>''', rows=rows)


@app.get('/labels/<int:lot_id>')
@login_required
def label_print(lot_id):
    fmt = request.args.get('size', '100x70')
    if fmt not in ('100x70', '100x150'):
        abort(400, description='Неподдерживаемый формат этикетки')
    with conn() as c:
        lot = c.execute('''SELECT l.*, COALESCE(m.name,p.name) AS name,
                      COALESCE(m.unit,p.unit) AS unit
                      FROM lots l LEFT JOIN materials m ON m.id=l.material_id
                      LEFT JOIN products p ON p.id=l.product_id
                      WHERE l.id=?''', (lot_id,)).fetchone()
    if not lot:
        abort(404)
    state, status, note = label_state(lot)
    qr = label_qr_svg(lot['lot_code'])
    width, height = map(int, fmt.split('x'))
    template = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Этикетка {{lot.lot_code}} · lovetech</title>
    <style>
    @page { size: {{width}}mm {{height}}mm; margin: 0; }
    * {box-sizing:border-box} body{margin:0;background:#e7edf4;font-family:Arial,sans-serif;color:#15283d}
    .toolbar{padding:14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:#fff}
    .toolbar a,.toolbar button{padding:8px 12px;background:#1c5379;color:#fff;border:0;border-radius:5px;text-decoration:none;cursor:pointer;font-size:14px}
    .sheet{width:{{width}}mm;height:{{height}}mm;margin:15px auto;background:#fff;box-shadow:0 2px 12px #0002;overflow:hidden;border:1px solid #ddd;display:flex;flex-direction:column}
    .head{background:var(--accent);color:var(--head);padding:3mm 4mm;display:flex;justify-content:space-between;align-items:center;font-size:11pt;font-weight:800}
    .state{font-size:{{'15' if height==70 else '20'}}pt;font-weight:900;letter-spacing:.3px;padding:2mm 4mm 1mm}
    .name{font-size:{{'11' if height==70 else '16'}}pt;font-weight:750;padding:0 4mm 2mm;overflow-wrap:anywhere}
    .body{display:flex;gap:2mm;padding:0 4mm;flex:1;min-height:0}
    .data{flex:1;min-width:0;font-size:{{'8.5' if height==70 else '11'}}pt;line-height:1.35}
    .field{display:flex;gap:1mm;justify-content:space-between;border-bottom:1px solid #e6eaf0;padding:0.7mm 0}
    .field span{color:#536171}.field strong{text-align:right;overflow-wrap:anywhere}
    .qr{width:{{'23' if height==70 else '36'}}mm;flex:none;text-align:center;align-self:center;font-size:7pt}
    .qr img{width:100%;display:block;image-rendering:pixelated}
    .foot{background:var(--pale);padding:2.5mm 4mm;font-size:{{'9' if height==70 else '12'}}pt;font-weight:800;min-height:12mm;display:flex;align-items:center}
    .subnote{font-size:7pt;font-weight:normal;margin-top:1mm}
    .sheet[data-state="quarantine"]{--accent:#c62828;--head:#fff;--pale:#ffe3e3}
    .sheet[data-state="released"]{--accent:#117a43;--head:#fff;--pale:#e0f4e8}
    .sheet[data-state="warning"]{--accent:#ffc928;--head:#222;--pale:#fff2c4}
    .sheet[data-state="expired"],.sheet[data-state="blocked"]{--accent:#39434d;--head:#fff;--pale:#e0e5ea}
    @media print {body{background:#fff}.toolbar{display:none}.sheet{margin:0;border:0;box-shadow:none;break-after:page}}
    </style></head><body><div class="toolbar"><a href="/labels">← Все этикетки</a><a href="?size=100x70">100 × 70 мм</a><a href="?size=100x150">100 × 150 мм</a><button onclick="window.print()">Печать / сохранить PDF</button></div>
    <article class="sheet" data-state="{{state}}"><div class="head"><span>lovetech · {{'СЫРЬЁ' if lot.kind=='raw' else 'ПРОДУКЦИЯ'}}</span><span>№ {{lot.id}}</span></div>
    <div class="state">{{status}}</div><div class="name">{{lot.name}}</div>
    <div class="body"><div class="data"><div class="field"><span>Партия</span><strong>{{lot.lot_code}}</strong></div><div class="field"><span>Количество</span><strong>{{'%.3f'|format(lot.qty)}} {{lot.unit}}</strong></div><div class="field"><span>{{'Поступление / изготовление' if lot.kind=='raw' else 'Изготовлено'}}</span><strong>{{lot.manufactured}}</strong></div><div class="field"><span>Годен до</span><strong>{{lot.expires}}</strong></div><div class="field"><span>ОТК</span><strong>{{lot.status}}</strong></div>{% if lot.kind=='raw' %}<div class="field"><span>Поставщик</span><strong>{{lot.supplier or '—'}}</strong></div>{% endif %}<div class="field"><span>Печать</span><strong>{{printed}}</strong></div></div><div class="qr"><img src="{{qr}}" alt="QR-код партии"><b>QR · партия</b></div></div>
    <div class="foot"><div>{{note}}<div class="subnote">Внутренняя складская этикетка. Не заменяет маркировку опасности (СГС/GHS).</div></div></div></article></body></html>'''
    return render_template_string(template, lot=lot, state=state, status=status, note=note,
                                  qr=qr, width=width, height=height, printed=now())



# v9: individually identifiable packages, printable sheets and a preparation log.
# Quantities here describe package allocation, not independent warehouse stock.
def container_qr_svg(code):
    qr=qrcode.QRCode(box_size=4,border=2,error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data('CHEMPRO:CONTAINER:'+code);qr.make(fit=True)
    buf=io.BytesIO();qr.make_image(image_factory=SvgPathImage).save(buf)
    return 'data:image/svg+xml;base64,'+base64.b64encode(buf.getvalue()).decode('ascii')

@app.route('/containers',methods=['GET','POST'])
@login_required
def containers_view():
    if request.method=='POST':
        if not has_role('admin','warehouse'):abort(403)
        try:
            lid=int(request.form['lot_id']);code=request.form['code'].strip()
            qty=number(request.form,'qty');note=request.form.get('note','').strip()[:300]
            if not code or len(code)>100:raise ValueError('Код тары: от 1 до 100 символов')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                lot=c.execute('SELECT id,qty FROM lots WHERE id=?',(lid,)).fetchone()
                if not lot:raise ValueError('Партия не найдена')
                allocated=c.execute('SELECT COALESCE(SUM(qty),0) FROM containers WHERE lot_id=? AND active=1',(lid,)).fetchone()[0]
                if qty+allocated>lot['qty']+0.000001:raise ValueError('Количество в таре превышает текущий остаток партии')
                c.execute('INSERT INTO containers(lot_id,code,qty,created_at,created_by,note) VALUES(?,?,?,?,?,?)',(lid,code,qty,now(),session['uid'],note))
                audit(c,'container.create',f'lot={lid}; code={code}; qty={qty}')
            flash('Единица тары зарегистрирована')
        except Exception as e:return fail(e)
        return redirect('/containers')
    with conn() as c:
        lots=c.execute("SELECT l.id,l.lot_code,l.qty,COALESCE(m.unit,p.unit) unit FROM lots l LEFT JOIN materials m ON m.id=l.material_id LEFT JOIN products p ON p.id=l.product_id ORDER BY l.id DESC LIMIT 250").fetchall()
        rows=c.execute("SELECT ct.*,l.lot_code,l.status,l.expires FROM containers ct JOIN lots l ON l.id=ct.lot_id ORDER BY ct.id DESC LIMIT 250").fetchall()
    return page('Учёт тары и массовая печать','''<div class="card"><p>Каждая единица тары имеет уникальный код и QR. Учёт тары информационный: при расходе или отгрузке партии её количество автоматически не распределяется по контейнерам.</p><p class="muted">Для складских операций используйте остаток партии. После списаний сверяйте содержимое тары вручную.</p>{% if has_role('admin','warehouse') %}<form method="post"><label>Партия<select name="lot_id">{% for l in lots %}<option value="{{l.id}}">{{l.lot_code}} · остаток {{l.qty}} {{l.unit}}</option>{% endfor %}</select></label><label>Код тары<input name="code" maxlength="100" required></label><label>Количество<input type="number" name="qty" step="any" min="0.000001" required></label><label>Примечание<input name="note" maxlength="300"></label><button>Зарегистрировать тару</button></form>{% endif %}</div><div class="card"><h2>Единицы тары</h2><table><tr><th>Код</th><th>Партия</th><th>Количество</th><th>Статус партии</th><th>Печать</th></tr>{% for x in rows %}<tr><td>{{x.code}}</td><td>{{x.lot_code}}</td><td>{{x.qty}}</td><td>{{x.status}} · до {{x.expires}}</td><td><a href="/containers/{{x.id}}/label">Этикетка</a></td></tr>{% endfor %}</table></div><div class="card"><h2>Массовая печать</h2><form action="/containers/print" method="post"><label>Партия<select name="lot_id">{% for l in lots %}<option value="{{l.id}}">{{l.lot_code}}</option>{% endfor %}</select></label><label>Формат<select name="size"><option>100x70</option><option>100x150</option></select></label><button>Подготовить лист этикеток</button></form><p><a href="/containers/print-log">Журнал подготовки печати →</a></p></div>''',lots=lots,rows=rows)

@app.get('/containers/<int:container_id>/label')
@login_required
def container_label(container_id):
    fmt=request.args.get('size','100x70')
    if fmt not in ('100x70','100x150'):abort(400)
    with conn() as c:
        item=c.execute('''SELECT ct.*,l.lot_code,l.kind,l.expires,l.status,l.manufactured,
         COALESCE(m.name,p.name) name,COALESCE(m.unit,p.unit) unit
         FROM containers ct JOIN lots l ON l.id=ct.lot_id
         LEFT JOIN materials m ON m.id=l.material_id LEFT JOIN products p ON p.id=l.product_id
         WHERE ct.id=?''',(container_id,)).fetchone()
    if not item:abort(404)
    return render_container_sheet([dict(item)],fmt)

def render_container_sheet(items,fmt):
    if fmt not in ('100x70','100x150'):abort(400)
    w,h=map(int,fmt.split('x'))
    labels=[]
    for item in items:
        state,status,note=label_state(item)
        labels.append(dict(item=item,state=state,status=status,note=note,qr=container_qr_svg(item['code'])))
    template='''<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>lovetech · этикетки тары</title><style>
    @page{size:{{w}}mm {{h}}mm;margin:0}*{box-sizing:border-box}body{font-family:Arial,sans-serif;margin:0;background:#e9edf2;color:#17273b}
    .toolbar{background:white;padding:12px;display:flex;gap:12px}.toolbar a,.toolbar button{background:#17628a;color:white;border:0;border-radius:5px;padding:9px 12px;text-decoration:none;cursor:pointer}
    .sheet{width:{{w}}mm;height:{{h}}mm;background:white;margin:12px auto;display:flex;flex-direction:column;border:1px solid #aaa;overflow:hidden;break-after:page}
    .sheet[data-state="released"]{--c:#147844;--p:#e3f5e9}.sheet[data-state="warning"]{--c:#b38200;--p:#fff2c8}.sheet[data-state="quarantine"]{--c:#b82626;--p:#ffe5e5}.sheet[data-state="blocked"],.sheet[data-state="expired"]{--c:#39434d;--p:#e3e7ea}
    .top{background:var(--c);color:white;font-weight:bold;padding:3mm 4mm}.status{font-weight:bold;color:var(--c);font-size:{{'15' if h==70 else '22'}}pt;padding:2mm 4mm}.name{font-weight:bold;padding:0 4mm 2mm;overflow-wrap:anywhere}
    .content{display:flex;gap:2mm;padding:0 4mm;flex:1;min-height:0}.fields{flex:1;min-width:0;font-size:{{'9' if h==70 else '12'}}pt}.fields p{margin:1mm 0;overflow-wrap:anywhere}.qr{width:{{'25' if h==70 else '40'}}mm;align-self:center;text-align:center;font-size:8pt}.qr img{width:100%}
    .foot{background:var(--p);padding:2mm 4mm;font-weight:bold;font-size:9pt}.small{font-size:7pt;font-weight:normal}
    @media print{body{background:white}.toolbar{display:none}.sheet{border:0;margin:0;box-shadow:none}}
    </style></head><body><div class="toolbar"><a href="/containers">← Тара</a><button onclick="window.print()">Печать / PDF</button></div>
    {% for l in labels %}<article class="sheet" data-state="{{l.state}}"><div class="top">lovetech · {{'СЫРЬЁ' if l.item.kind=='raw' else 'ПРОДУКЦИЯ'}} · ЕДИНИЦА ТАРЫ</div><div class="status">{{l.status}}</div><div class="name">{{l.item.name}}</div><div class="content"><div class="fields"><p><b>Код тары:</b> {{l.item.code}}</p><p><b>Партия:</b> {{l.item.lot_code}}</p><p><b>Количество:</b> {{l.item.qty}} {{l.item.unit}}</p><p><b>Годен до:</b> {{l.item.expires}}</p><p><b>ОТК:</b> {{l.item.status}}</p></div><div class="qr"><img src="{{l.qr}}" alt="QR"><b>QR тары</b></div></div><div class="foot">{{l.note}}<div class="small">Внутренняя этикетка. Не заменяет маркировку СГС/GHS. Данные на момент формирования.</div></div></article>{% endfor %}</body></html>'''
    return render_template_string(template,labels=labels,w=w,h=h)

@app.post('/containers/print')
@allowed('admin','warehouse','quality')
def containers_batch_print():
    fmt=request.form.get('size','100x70')
    if fmt not in ('100x70','100x150'):abort(400)
    try:lid=int(request.form['lot_id'])
    except (KeyError,ValueError):abort(400)
    with conn() as c:
        items=c.execute('''SELECT ct.*,l.lot_code,l.kind,l.expires,l.status,l.manufactured,
         COALESCE(m.name,p.name) name,COALESCE(m.unit,p.unit) unit
         FROM containers ct JOIN lots l ON l.id=ct.lot_id
         LEFT JOIN materials m ON m.id=l.material_id LEFT JOIN products p ON p.id=l.product_id
         WHERE ct.lot_id=? AND ct.active=1 ORDER BY ct.id LIMIT 100''',(lid,)).fetchall()
        if not items:abort(404,description='У партии нет зарегистрированной тары')
        c.execute('INSERT INTO label_print_jobs(created_at,user_id,format,mode,label_count,details) VALUES(?,?,?,?,?,?)',(now(),session['uid'],fmt,'batch',len(items),f'lot_id={lid}; container_ids='+','.join(str(i['id']) for i in items)))
        audit(c,'labels.batch_prepared',f'lot_id={lid}; count={len(items)}; format={fmt}')
    return render_container_sheet([dict(x) for x in items],fmt)

@app.get('/containers/print-log')
@login_required
def containers_print_log():
    with conn() as c:
        rows=c.execute('SELECT j.*,u.username FROM label_print_jobs j JOIN users u ON u.id=j.user_id ORDER BY j.id DESC LIMIT 300').fetchall()
    return page('Журнал подготовки этикеток','''<div class="card"><p>Записывается факт формирования массового печатного листа, а не подтверждение физической печати на принтере.</p><table><tr><th>Дата</th><th>Пользователь</th><th>Формат</th><th>Этикеток</th><th>Состав</th></tr>{% for x in rows %}<tr><td>{{x.created_at}}</td><td>{{x.username}}</td><td>{{x.format}}</td><td>{{x.label_count}}</td><td>{{x.details}}</td></tr>{% endfor %}</table></div>''',rows=rows)

# v10: container transfers and stocktaking with an immutable operation journal.
# IMPORTANT: production and shipment transactions still use lot-level balances;
# this module intentionally does not claim end-to-end package-level accounting.
@app.route('/container-ops',methods=['GET','POST'])
@login_required
def container_operations_view():
    if request.method=='POST':
        if not has_role('admin','warehouse'):abort(403)
        try:
            cid=int(request.form['container_id'])
            action=request.form.get('operation','')
            note=request.form.get('note','').strip()[:300]
            if action not in ('transfer','count'):raise ValueError('Неизвестная операция')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                ct=c.execute('SELECT ct.*,l.qty AS lot_qty,l.lot_code FROM containers ct JOIN lots l ON l.id=ct.lot_id WHERE ct.id=? AND ct.active=1',(cid,)).fetchone()
                if not ct:raise ValueError('Активная тара не найдена')
                loc=c.execute('SELECT location FROM container_locations WHERE container_id=?',(cid,)).fetchone()
                old_location=loc['location'] if loc else 'Основной склад'
                before=float(ct['qty'])
                if action=='transfer':
                    new_location=request.form.get('location','').strip()
                    if not new_location or len(new_location)>100:raise ValueError('Укажите место хранения (до 100 символов)')
                    if new_location==old_location:raise ValueError('Место хранения не изменилось')
                    after=before
                    c.execute('INSERT INTO container_locations(container_id,location) VALUES(?,?) ON CONFLICT(container_id) DO UPDATE SET location=excluded.location',(cid,new_location))
                else:
                    after=number(request.form,'qty')
                    if after<=0:raise ValueError('Для пустой тары используйте отдельную процедуру закрытия')
                    if abs(after-before)<0.000001:raise ValueError('Количество не изменилось')
                    # Reconciliation only; do not silently mutate lot-level stock.
                    allocated=c.execute('SELECT COALESCE(SUM(qty),0) FROM containers WHERE lot_id=? AND active=1',(ct['lot_id'],)).fetchone()[0]
                    if allocated-before+after>ct['lot_qty']+0.000001:raise ValueError('Количество по таре превысит остаток партии')
                    new_location=old_location
                    c.execute('UPDATE containers SET qty=? WHERE id=?',(after,cid))
                c.execute('INSERT INTO container_operations(container_id,operation,before_qty,after_qty,from_location,to_location,created_at,user_id,note) VALUES(?,?,?,?,?,?,?,?,?)',(cid,action,before,after,old_location,new_location,now(),session['uid'],note))
                audit(c,'container.'+action,f'container={cid}; from={old_location}; to={new_location}; before={before}; after={after}')
            flash('Операция зарегистрирована')
        except Exception as e:return fail(e)
        return redirect('/container-ops')
    with conn() as c:
        items=c.execute("""SELECT ct.id,ct.code,ct.qty,l.lot_code,l.qty AS lot_qty,
            COALESCE(cl.location,'Основной склад') AS location
            FROM containers ct JOIN lots l ON l.id=ct.lot_id
            LEFT JOIN container_locations cl ON cl.container_id=ct.id
            WHERE ct.active=1 ORDER BY ct.id DESC LIMIT 300""").fetchall()
        history=c.execute("""SELECT o.*,ct.code,u.username FROM container_operations o
            JOIN containers ct ON ct.id=o.container_id JOIN users u ON u.id=o.user_id
            ORDER BY o.id DESC LIMIT 100""").fetchall()
    return page('Операции с тарой',"""<div class="card"><p>Перемещения и инвентаризационные корректировки по индивидуальному коду тары. Операции регистрируются в журнале. <strong>Расход производства и отгрузка по-прежнему списываются на уровне партии</strong> — перед использованием необходимо сверять остатки.</p>
    {% if has_role('admin','warehouse') %}<form method="post"><label>Тара<select name="container_id">{% for x in items %}<option value="{{x.id}}">{{x.code}} — {{x.lot_code}} ({{x.qty}})</option>{% endfor %}</select></label><label>Операция<select name="operation"><option value="transfer">Перемещение</option><option value="count">Инвентаризация</option></select></label><label>Новое место хранения (для перемещения)<input name="location" maxlength="100" placeholder="Склад / зона / стеллаж"></label><label>Фактическое количество (для инвентаризации)<input type="number" name="qty" min="0.000001" step="any"></label><label>Примечание<input name="note" maxlength="300"></label><button>Записать операцию</button></form>{% endif %}</div>
    <div class="card"><h2>Остатки по таре</h2><table><tr><th>Код</th><th>Партия</th><th>Количество</th><th>Место</th><th>Этикетка</th></tr>{% for x in items %}<tr><td>{{x.code}}</td><td>{{x.lot_code}}</td><td>{{x.qty}}</td><td>{{x.location}}</td><td><a href="/containers/{{x.id}}/label">Печать</a></td></tr>{% endfor %}</table></div>
    <div class="card"><h2>История операций</h2><table><tr><th>Время</th><th>Код тары</th><th>Действие</th><th>Было → стало</th><th>Откуда → куда</th><th>Сотрудник</th><th>Комментарий</th></tr>{% for x in history %}<tr><td>{{x.created_at}}</td><td>{{x.code}}</td><td>{{x.operation}}</td><td>{{x.before_qty}} → {{x.after_qty}}</td><td>{{x.from_location}} → {{x.to_location}}</td><td>{{x.username}}</td><td>{{x.note}}</td></tr>{% endfor %}</table></div>""",items=items,history=history)

# v11: keyboard-wedge / TSD scan and optional browser camera scan.
# Both methods enter the same textual CHEMPRO:CONTAINER:<code> payload.
def parse_container_scan(raw):
    raw=(raw or '').strip()
    prefix='CHEMPRO:CONTAINER:'
    if raw.upper().startswith(prefix):raw=raw[len(prefix):]
    if not raw or len(raw)>100 or any(ord(ch)<32 for ch in raw):
        raise ValueError('Некорректный код тары')
    return raw

@app.route('/scan',methods=['GET','POST'])
@login_required
def scan_container():
    if request.method=='POST':
        if not has_role('admin','warehouse'):abort(403)
        try:
            code=parse_container_scan(request.form.get('scan_code'))
            action=request.form.get('operation','lookup')
            if action not in ('lookup','transfer','consume'):raise ValueError('Неизвестная операция')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                ct=c.execute("""SELECT ct.*,l.lot_code,l.kind,l.status,l.expires,l.qty AS lot_qty
                    FROM containers ct JOIN lots l ON l.id=ct.lot_id WHERE ct.code=?""",(code,)).fetchone()
                if not ct:raise ValueError('Тара не найдена: '+code)
                if action!='lookup' and not ct['active']:raise ValueError('Тара неактивна')
                loc=c.execute('SELECT location FROM container_locations WHERE container_id=?',(ct['id'],)).fetchone()
                oldloc=loc['location'] if loc else 'Основной склад'
                if action=='transfer':
                    newloc=request.form.get('location','').strip()
                    if not newloc or len(newloc)>100 or newloc==oldloc:raise ValueError('Укажите другое место хранения (до 100 символов)')
                    c.execute('INSERT INTO container_locations(container_id,location) VALUES(?,?) ON CONFLICT(container_id) DO UPDATE SET location=excluded.location',(ct['id'],newloc))
                    c.execute('INSERT INTO container_operations(container_id,operation,before_qty,after_qty,from_location,to_location,created_at,user_id,note) VALUES(?,?,?,?,?,?,?,?,?)',(ct['id'],'transfer',ct['qty'],ct['qty'],oldloc,newloc,now(),session['uid'],'ТСД/сканирование'))
                    audit(c,'scan.transfer',f'container={ct["id"]}; from={oldloc}; to={newloc}')
                    flash('Тара перемещена: '+code)
                elif action=='consume':
                    qty=number(request.form,'qty')
                    if ct['kind']!='raw':raise ValueError('Расход через этот экран допускается только для сырья')
                    if ct['status']!='released' or ct['expires']<date.today().isoformat():raise ValueError('Партия не разрешена ОТК или просрочена')
                    if qty>ct['qty']+0.0000001:raise ValueError('Недостаточно сырья в таре')
                    reserved=c.execute('SELECT COALESCE(SUM(qty),0) FROM reservations WHERE lot_id=?',(ct['lot_id'],)).fetchone()[0]
                    if ct['lot_qty']-reserved+0.0000001<qty:raise ValueError('Остаток партии зарезервирован под производственное задание')
                    u=c.execute('UPDATE containers SET qty=qty-?,active=CASE WHEN qty-?<=0.0000001 THEN 0 ELSE 1 END WHERE id=? AND active=1 AND qty>=?',(qty,qty,ct['id'],qty))
                    if u.rowcount!=1:raise ValueError('Остаток тары изменился')
                    u=c.execute('UPDATE lots SET qty=qty-? WHERE id=? AND qty>=?',(qty,ct['lot_id'],qty))
                    if u.rowcount!=1:raise ValueError('Недостаточно остатка партии')
                    c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(ct['lot_id'],-qty,'Расход из тары '+code+' (вне производственного задания)',now(),session['uid']))
                    c.execute('INSERT INTO container_operations(container_id,operation,before_qty,after_qty,from_location,to_location,created_at,user_id,note) VALUES(?,?,?,?,?,?,?,?,?)',(ct['id'],'consume',ct['qty'],max(0,ct['qty']-qty),oldloc,oldloc,now(),session['uid'],'Вне производственного задания'))
                    audit(c,'scan.consume',f'container={ct["id"]}; qty={qty}; lot={ct["lot_id"]}')
                    flash('Расход проведён по таре и партии: '+code)
                else:
                    flash('Найдена тара '+code+'; партия '+ct['lot_code']+'; остаток '+str(ct['qty']))
        except Exception as e:return fail(e)
        return redirect('/scan')
    with conn() as c:
        rows=c.execute("""SELECT ct.code,ct.qty,ct.active,l.lot_code,l.status,l.expires,
            COALESCE(cl.location,'Основной склад') location
            FROM containers ct JOIN lots l ON l.id=ct.lot_id
            LEFT JOIN container_locations cl ON cl.container_id=ct.id
            ORDER BY ct.id DESC LIMIT 100""").fetchall()
        hist=c.execute("""SELECT o.created_at,o.operation,ct.code,o.before_qty,o.after_qty,u.username
            FROM container_operations o JOIN containers ct ON ct.id=o.container_id
            JOIN users u ON u.id=o.user_id ORDER BY o.id DESC LIMIT 50""").fetchall()
    return page('Сканирование ТСД и QR',"""<div class="card"><h2>Сканирование тары</h2>
    <p>Поддерживаются ТСД и USB/Bluetooth-сканеры в режиме HID-клавиатуры: сканер вводит код в поле, суффикс Enter запускает операцию. Камера доступна при поддержке BarcodeDetector и HTTPS.</p>
    {% if has_role('admin','warehouse') %}<form method="post" id="scan-form">
    <label>QR / код тары<input id="scan-code" name="scan_code" autofocus autocomplete="off" maxlength="120" placeholder="CHEMPRO:CONTAINER:..." required></label>
    <label>Операция<select name="operation" id="scan-action"><option value="lookup">Найти тару</option><option value="transfer">Переместить</option><option value="consume">Частичный расход сырья (вне задания)</option></select></label>
    <label>Место назначения (для перемещения)<input name="location" maxlength="100"></label>
    <label>Количество (для расхода)<input name="qty" type="number" min="0.000001" step="any"></label>
    <button type="submit">Выполнить</button> <button type="button" id="camera-start">Сканировать камерой</button> <button type="button" id="camera-stop">Остановить камеру</button>
    <video id="camera-preview" autoplay playsinline muted style="display:none;max-width:100%;width:360px"></video><p id="camera-message" aria-live="polite"></p>
    </form>{% else %}<p>Операции доступны кладовщику и администратору.</p>{% endif %}
    <p class="muted">При частичном расходе одновременно уменьшаются остатки тары и партии. Это отдельное складское списание, не списание по рецептуре или производственному заданию. Для зарегистрированной тары обычное списание партии в производстве/отгрузке временно блокируется.</p></div>
    <div class="card"><h2>Тара</h2><table><tr><th>Код</th><th>Партия</th><th>Остаток</th><th>Статус</th><th>Место</th></tr>{% for x in rows %}<tr><td>{{x.code}}</td><td>{{x.lot_code}}</td><td>{{x.qty}}</td><td>{{'Активна' if x.active else 'Пустая' }} / {{x.status}}</td><td>{{x.location}}</td></tr>{% endfor %}</table></div>
    <div class="card"><h2>Последние операции</h2><table><tr><th>Дата</th><th>Тара</th><th>Операция</th><th>Было → стало</th><th>Сотрудник</th></tr>{% for x in hist %}<tr><td>{{x.created_at}}</td><td>{{x.code}}</td><td>{{x.operation}}</td><td>{{x.before_qty}} → {{x.after_qty}}</td><td>{{x.username}}</td></tr>{% endfor %}</table></div>
    <script>
    (()=>{const form=document.getElementById('scan-form');if(!form)return;
      const input=document.getElementById('scan-code'),video=document.getElementById('camera-preview'),msg=document.getElementById('camera-message');
      let stream=null,active=false;
      input.focus();
      // HID scanners often append Enter; native form submission handles that.
      document.getElementById('camera-start').addEventListener('click',async()=>{
        if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia){msg.textContent='Для камеры требуется HTTPS и разрешение браузера';return}
        if(!('BarcodeDetector' in window)){msg.textContent='Камера этого браузера не поддерживает BarcodeDetector. Используйте ТСД или ручной ввод.';return}
        try{const detector=new BarcodeDetector({formats:['qr_code']});stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'},audio:false});video.srcObject=stream;video.style.display='block';active=true;
          const loop=async()=>{if(!active)return;try{const codes=await detector.detect(video);if(codes.length){input.value=codes[0].rawValue;stop();form.requestSubmit();return}}catch(e){}if(active)requestAnimationFrame(loop)};requestAnimationFrame(loop)
        }catch(e){msg.textContent='Не удалось открыть камеру: '+e.message;stop()}
      });
      function stop(){active=false;if(stream){stream.getTracks().forEach(t=>t.stop());stream=null}video.style.display='none'}
      document.getElementById('camera-stop').addEventListener('click',stop);
      window.addEventListener('pagehide',stop);
    })();
    </script>""",rows=rows,hist=hist)


# v12: Urovo CT48 browser HID scanner. Scanning only looks up a container;
# a separate explicit POST confirms an operation. Tokens are single-use in SQLite.
@app.route('/tsd',methods=['GET','POST'])
@allowed('admin','warehouse')
def tsd_workplace():
    found=None
    error=None
    if request.method=='POST':
        try:
            action=request.form.get('action')
            if action=='lookup':
                code=parse_container_scan(request.form.get('scan_code'))
                return redirect(url_for('tsd_workplace',code=code))
            if action!='confirm':raise ValueError('Неизвестное действие')
            token=request.form.get('token','')
            if not token:raise ValueError('Нет идентификатора операции')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                req=c.execute('SELECT * FROM tsd_requests WHERE token=? AND user_id=?',(token,session['uid'])).fetchone()
                if not req:raise ValueError('Операция не найдена')
                if req['executed_at']:raise ValueError('Эта операция уже проведена. Повторное списание запрещено')
                ct=c.execute("""SELECT ct.*,l.kind,l.status,l.expires,l.qty AS lot_qty,l.lot_code
                    FROM containers ct JOIN lots l ON l.id=ct.lot_id WHERE ct.id=?""",(req['container_id'],)).fetchone()
                if not ct or not ct['active']:raise ValueError('Тара неактивна')
                loc=c.execute('SELECT location FROM container_locations WHERE container_id=?',(ct['id'],)).fetchone()
                oldloc=loc['location'] if loc else 'Основной склад'
                if req['operation']=='transfer':
                    newloc=request.form.get('location','').strip()
                    if not newloc or len(newloc)>100 or newloc==oldloc:raise ValueError('Укажите новое место хранения (до 100 символов)')
                    c.execute('INSERT INTO container_locations(container_id,location) VALUES(?,?) ON CONFLICT(container_id) DO UPDATE SET location=excluded.location',(ct['id'],newloc))
                    c.execute('INSERT INTO container_operations(container_id,operation,before_qty,after_qty,from_location,to_location,created_at,user_id,note) VALUES(?,?,?,?,?,?,?,?,?)',(ct['id'],'transfer',ct['qty'],ct['qty'],oldloc,newloc,now(),session['uid'],'ТСД CT48'))
                    details='Новое место: '+newloc
                elif req['operation']=='consume':
                    qty=number(request.form,'qty')
                    if ct['kind']!='raw':raise ValueError('Расход через ТСД допускается только для сырья')
                    if ct['status']!='released' or ct['expires']<date.today().isoformat():raise ValueError('ОТК не разрешил партию или истёк срок годности')
                    if qty>ct['qty']+1e-8:raise ValueError('Недостаточно сырья в таре')
                    picked=c.execute("SELECT COALESCE(SUM(qty),0) FROM tsd_picks WHERE container_id=? AND status IN ('staged','issued')",(ct['id'],)).fetchone()[0]
                    if qty>ct['qty']-picked+1e-8:raise ValueError('Количество зарезервировано подбором по заданию')
                    reserved=c.execute('SELECT COALESCE(SUM(qty),0) FROM reservations WHERE lot_id=?',(ct['lot_id'],)).fetchone()[0]
                    if qty>ct['lot_qty']-reserved+1e-8:raise ValueError('Недостаточно свободного остатка партии')
                    updated=c.execute('UPDATE containers SET qty=qty-?,active=CASE WHEN qty-?<=0.00000001 THEN 0 ELSE 1 END WHERE id=? AND active=1 AND qty>=?',(qty,qty,ct['id'],qty))
                    if updated.rowcount!=1:raise ValueError('Остаток тары изменился')
                    updated=c.execute('UPDATE lots SET qty=qty-? WHERE id=? AND qty>=?',(qty,ct['lot_id'],qty))
                    if updated.rowcount!=1:raise ValueError('Остаток партии изменился')
                    c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(ct['lot_id'],-qty,'ТСД: расход из тары '+ct['code']+' (вне задания)',now(),session['uid']))
                    c.execute('INSERT INTO container_operations(container_id,operation,before_qty,after_qty,from_location,to_location,created_at,user_id,note) VALUES(?,?,?,?,?,?,?,?,?)',(ct['id'],'consume',ct['qty'],max(0,ct['qty']-qty),oldloc,oldloc,now(),session['uid'],'ТСД CT48, вне задания'))
                    details='Расход: '+str(qty)
                else:raise ValueError('Недопустимая операция')
                c.execute('UPDATE tsd_requests SET executed_at=?,details=? WHERE token=? AND executed_at IS NULL',(now(),details,token))
                audit(c,'tsd.'+req['operation'],f'container={ct["id"]}; {details}; token={token}')
            flash('Операция успешно проведена: '+details)
            return redirect(url_for('tsd_workplace',code=ct['code']))
        except (ValueError,sqlite3.Error) as e:
            error=str(e)
    code=(request.args.get('code') or '').strip()
    if code and not error:
        try:
            code=parse_container_scan(code)
            with conn() as c:
                found=c.execute("""SELECT ct.*,l.kind,l.status,l.expires,l.qty AS lot_qty,l.lot_code,
                    COALESCE(cl.location,'Основной склад') AS location
                    FROM containers ct JOIN lots l ON l.id=ct.lot_id
                    LEFT JOIN container_locations cl ON cl.container_id=ct.id WHERE ct.code=?""",(code,)).fetchone()
                if not found:error='Тара не найдена: '+code
        except ValueError as e:error=str(e)
    transfer_token=consume_token=None
    if found and found['active'] and found['qty']>0:
        with conn() as c:
            transfer_token=uuid.uuid4().hex
            consume_token=uuid.uuid4().hex
            c.executemany('INSERT INTO tsd_requests(token,container_id,operation,created_at,user_id) VALUES(?,?,?,?,?)',[(transfer_token,found['id'],'transfer',now(),session['uid']),(consume_token,found['id'],'consume',now(),session['uid'])])
    return page('ТСД · Urovo CT48',"""
    <style>
    .tsd{max-width:480px;margin:auto}.tsd .card{padding:14px;margin:10px 0;overflow:visible}
    .tsd input,.tsd button,.tsd select{font-size:18px;min-height:50px;width:100%;box-sizing:border-box}
    .tsd label{display:flex;margin:8px 0}.tsd button{font-weight:700;margin-top:8px}
    .tsd .status{font-size:18px;font-weight:700}.tsd .ok{color:#176c3d}.tsd .bad{color:#a12727}
    .tsd .value{font-size:25px;font-weight:700}.tsd .small{font-size:13px;color:#58677b}
    </style>
    <div class="tsd"><div class="card"><h2>Сканируйте тару</h2>
    <p class="small">Urovo CT48: ScanWedge → клавиатурный ввод (HID) → суффикс Enter. Для поиска расход не проводится.</p>
    <form method="post" id="tsd-scan"><input type="hidden" name="_csrf" value="{{csrf_token}}"><input type="hidden" name="action" value="lookup">
    <label>QR / код тары<input id="tsd-code" name="scan_code" autocomplete="off" autocapitalize="off" autofocus placeholder="Нажмите кнопку сканера" required maxlength="120"></label>
    <button type="submit">Найти тару</button></form></div>
    {% if error %}<div class="card bad" role="alert">{{error}}</div>{% endif %}
    {% if found %}<div class="card"><h2>Тара {{found.code}}</h2>
    <p>Партия: <b>{{found.lot_code}}</b></p><p>Место: <b>{{found.location}}</b></p>
    <p>Остаток: <span class="value">{{found.qty}}</span></p>
    <p class="status {{'ok' if found.status=='released' and found.expires>=today else 'bad'}}">ОТК: {{found.status}} · годен до {{found.expires}}</p>
    {% if not found.active %}<p class="bad">Тара неактивна, операции запрещены.</p>{% else %}
    <form method="post" class="tsd-operation"><input type="hidden" name="_csrf" value="{{csrf_token}}"><input type="hidden" name="action" value="confirm"><input type="hidden" name="token" value="{{transfer_token}}">
    <label>Переместить в<input name="location" maxlength="100" placeholder="Стеллаж / зона" required></label>
    <button type="submit">Подтвердить перемещение</button></form>
    {% if found.kind=='raw' and found.status=='released' and found.expires>=today %}
    <form method="post" class="tsd-operation"><input type="hidden" name="_csrf" value="{{csrf_token}}"><input type="hidden" name="action" value="confirm"><input type="hidden" name="token" value="{{consume_token}}">
    <label>Расход сырья, количество<input type="number" name="qty" min="0.000001" step="any" max="{{found.qty}}" required></label>
    <button type="submit" style="background:#a04d1b">Подтвердить расход</button></form>
    <p class="small">Расход вне производственного задания. Уменьшает остатки тары и партии.</p>
    {% endif %}{% endif %}</div>{% endif %}
    <p class="small">Онлайн-режим: при отсутствии сети операция не проводится. Повторная отправка подтверждения блокируется на сервере.</p></div>
    <script>(()=>{
      const field=document.getElementById('tsd-code'),form=document.getElementById('tsd-scan');
      if(!field||!form)return;
      let pending=false;
      form.addEventListener('submit',e=>{if(pending){e.preventDefault();return}pending=true;setTimeout(()=>pending=false,3000)});
      document.querySelectorAll('.tsd-operation').forEach(f=>f.addEventListener('submit',e=>{
        if(f.dataset.sent==='1'){e.preventDefault();return}
        if(!window.confirm('Подтвердить складскую операцию?')){e.preventDefault();return}
        f.dataset.sent='1';f.querySelector('button[type=submit]').disabled=true;
      }));
      field.focus();
    })();</script>
    """,found=found,error=error,transfer_token=transfer_token,consume_token=consume_token)

@app.route('/production',methods=['GET','POST'])
@login_required
def production():
 if request.method=='POST':
  if not has_role('admin','operator'):abort(403)
  try:
   order_id=int(request.form.get('order_id') or 0)
   rid=int(request.form['recipe_id']);planned=number(request.form,'planned_qty');actual=number(request.form,'actual_qty');code=request.form['lot_code'].strip()
   if not code or len(code)>100:raise ValueError('Неверный номер партии')
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    if order_id:
     order=c.execute("SELECT * FROM work_orders WHERE id=? AND status='approved'",(order_id,)).fetchone()
     if not order:raise ValueError('Задание не найдено или не утверждено')
     if order['recipe_id']!=rid or abs(order['planned_qty']-planned)>0.000001 or order['lot_code']!=code:raise ValueError('Параметры выпуска не совпадают с заданием')
    if order_id and c.execute("SELECT 1 FROM tsd_picks WHERE order_id=? AND status='staged' LIMIT 1",(order_id,)).fetchone():raise ValueError('Есть подобранная, но ещё не выданная тара')
    if order_id and os.environ.get('LOVETECH_V59_ENFORCE_RELEASE','0')=='1':
     # v60: explicitly selected post-close sequence avoids requiring closure before release.
     if os.environ.get('LOVETECH_V60_RELEASE_SEQUENCE','legacy')=='post_close':
      readiness=v60_sequence_status(c,order_id)
     else:
      readiness=v59_release_readiness(c,order_id)
     if not readiness['ready']:raise ValueError('Выпуск заблокирован контролем: '+'; '.join(readiness['blockers'][:5]))
    r=c.execute("SELECT r.*,p.shelf_days FROM recipes r JOIN products p ON p.id=r.product_id WHERE r.id=? AND r.status='approved'",(rid,)).fetchone()
    if not r:raise ValueError('Рецептура не утверждена')
    items=c.execute('SELECT * FROM recipe_items WHERE recipe_id=?',(rid,)).fetchall()
    allocations=[]
    reserved=c.execute('SELECT lot_id,qty FROM reservations WHERE order_id=? ORDER BY lot_id',(order_id,)).fetchall() if order_id else []
    if reserved:
     for item in items:
      needed=round(planned*item['percent']/100,8)
      matching=c.execute("""SELECT rv.lot_id,rv.qty,l.qty AS stock,l.material_id,l.status,l.expires
       FROM reservations rv JOIN lots l ON l.id=rv.lot_id WHERE rv.order_id=? AND l.material_id=?
       ORDER BY l.expires,l.id""",(order_id,item['material_id'])).fetchall()
      for lot in matching:
       if lot['status']!='released' or lot['expires']<date.today().isoformat() or lot['stock']+0.000001<lot['qty']:
        raise ValueError('Зарезервированная партия недоступна или просрочена')
       allocations.append((lot['lot_id'],lot['qty']))
       needed=round(needed-lot['qty'],8)
      if abs(needed)>0.000001:raise ValueError('Резерв не соответствует плану задания')
    else:
     for item in items:
      needed=round(planned*item['percent']/100,8)
      raw=c.execute("""SELECT l.*,COALESCE(SUM(rv.qty),0) AS reserved
       FROM lots l LEFT JOIN reservations rv ON rv.lot_id=l.id
       WHERE l.kind='raw' AND l.material_id=? AND l.status='released' AND l.expires>=? AND l.qty>0
       GROUP BY l.id ORDER BY l.expires,l.id""",(item['material_id'],date.today().isoformat())).fetchall()
      for lot in raw:
       take=round(min(needed,max(0,lot['qty']-lot['reserved'])),8)
       if take>0:allocations.append((lot['id'],take));needed=round(needed-take,8)
       if needed<=0.000001:break
      if needed>0.000001:raise ValueError('Недостаточно свободного годного сырья ID '+str(item['material_id']))
    if order_id:
     issued_rows=c.execute("SELECT ct.lot_id,SUM(p.qty) AS qty FROM tsd_picks p JOIN containers ct ON ct.id=p.container_id WHERE p.order_id=? AND p.status='issued' GROUP BY ct.lot_id",(order_id,)).fetchall()
     issued_map={x['lot_id']:x['qty'] for x in issued_rows}
     for lid,qty in allocations:
      if lid in issued_map and abs(issued_map[lid]-qty)>0.000001:raise ValueError('Выданное количество не соответствует резерву после возврата')
     if any(lid not in {a[0] for a in allocations} for lid in issued_map):raise ValueError('Выдана тара вне задания')
    made=date.today();exp=made+timedelta(days=r['shelf_days'])
    cur=c.execute("INSERT INTO lots(kind,product_id,lot_code,qty,manufactured,expires,status,created_at) VALUES('finished',?,?,?,?,?,'quarantine',?)",(r['product_id'],code,actual,made.isoformat(),exp.isoformat(),now()))
    bid=c.execute('INSERT INTO batches(recipe_id,lot_id,planned_qty,actual_qty,created_at,operator_id) VALUES(?,?,?,?,?,?)',(rid,cur.lastrowid,planned,actual,now(),session['uid'])).lastrowid
    for lid,qty in allocations:
     registered=c.execute('SELECT COUNT(*) FROM containers WHERE lot_id=? AND active=1 AND qty>0',(lid,)).fetchone()[0]
     related=c.execute('SELECT p.*,ct.qty AS container_qty,ct.active,ct.code FROM tsd_picks p JOIN containers ct ON ct.id=p.container_id WHERE ct.lot_id=? AND p.order_id=? AND p.status=?',(lid,order_id,'issued')).fetchall() if order_id else []
     if registered or related:
      if not order_id:raise ValueError('Для тары требуется производственное задание и выдача через ТСД')
      if abs(sum(x['qty'] for x in related)-qty)>0.000001:raise ValueError('Выданное сырьё не соответствует резерву партии '+str(lid))
      for x in related:
       if not x['active'] or x['container_qty']+0.000001<x['qty']:raise ValueError('Остаток тары изменился')
       changed=c.execute('UPDATE containers SET qty=qty-?,active=CASE WHEN qty-?<=0.00000001 THEN 0 ELSE 1 END WHERE id=? AND active=1 AND qty>=?',(x['qty'],x['qty'],x['container_id'],x['qty']))
       if changed.rowcount!=1:raise ValueError('Ошибка списания тары')
       location=c.execute('SELECT location FROM container_locations WHERE container_id=?',(x['container_id'],)).fetchone()
       loc=location['location'] if location else 'Основной склад'
       c.execute('INSERT INTO container_operations(container_id,operation,before_qty,after_qty,from_location,to_location,created_at,user_id,note) VALUES(?,?,?,?,?,?,?,?,?)',(x['container_id'],'consume',x['container_qty'],max(0,x['container_qty']-x['qty']),loc,loc,now(),session['uid'],'Производственное задание '+str(order_id)))
      c.execute("UPDATE tsd_picks SET status='consumed' WHERE order_id=? AND container_id IN (SELECT id FROM containers WHERE lot_id=?) AND status='issued'",(order_id,lid))
     updated=c.execute('UPDATE lots SET qty=qty-? WHERE id=? AND qty>=?',(qty,lid,qty))
     if updated.rowcount!=1:raise ValueError('Остаток изменился, повторите операцию')
     c.execute('INSERT INTO consumptions(batch_id,raw_lot_id,qty) VALUES(?,?,?)',(bid,lid,qty))
     c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(lid,-qty,'Расход в производство #'+str(bid),now(),session['uid']))
    c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(cur.lastrowid,actual,'Выпуск #'+str(bid),now(),session['uid']))
    if order_id:
     c.execute('DELETE FROM reservations WHERE order_id=?',(order_id,))
     changed=c.execute("UPDATE work_orders SET status='completed',batch_id=? WHERE id=? AND status='approved'",(bid,order_id))
     if changed.rowcount!=1:raise ValueError('Статус задания изменился')
    audit(c,'production.create','batch='+str(bid)+'; lot='+code+'; order='+str(order_id or '-'))
   flash('Партия выпущена на карантин. Номер производственной партии: '+str(bid))
  except Exception as e:return fail(e)
  return redirect('/production')
 with conn() as c:
  recipes=c.execute("SELECT r.id,r.version,p.name FROM recipes r JOIN products p ON p.id=r.product_id WHERE r.status='approved' ORDER BY p.name,r.version DESC").fetchall()
  chosen=None
  if request.args.get('order_id','').isdigit():
   chosen=c.execute("SELECT * FROM work_orders WHERE id=? AND status='approved'",(int(request.args['order_id']),)).fetchone()
  batches=c.execute('SELECT b.*,l.lot_code,p.name,r.version FROM batches b JOIN lots l ON l.id=b.lot_id JOIN recipes r ON r.id=b.recipe_id JOIN products p ON p.id=r.product_id ORDER BY b.id DESC').fetchall()
 return page('Производство','''{% if has_role('admin','operator') %}<div class="card"><h2>Зарегистрировать выпуск</h2><p class="muted">Автоматическое списание сырья по FEFO. Плановый расход по рецептуре, фактический выход задаётся отдельно. Операции атомарны.</p><form method="post">{% if chosen %}<input type="hidden" name="order_id" value="{{chosen.id}}"><p>Выполнение задания №{{chosen.id}}. План и партия зафиксированы.</p>{% endif %}<label>Утверждённая рецептура<select name="recipe_id">{% for r in recipes %}<option value="{{r.id}}" {% if chosen and chosen.recipe_id==r.id %}selected{% endif %}>{{r.name}} · v{{r.version}}</option>{% endfor %}</select></label><label>План, кг/ед.<input name="planned_qty" type="number" min="0.000001" step="any" value="{{chosen.planned_qty if chosen else ''}}" required></label><label>Факт, кг/ед.<input name="actual_qty" type="number" min="0.000001" step="any" required></label><label>Номер партии ГП<input name="lot_code" value="{{chosen.lot_code if chosen else ''}}" required></label><button>Провести производство</button></form></div>{% endif %}<div class="card"><h2>Журнал выпусков</h2><table><tr><th>ID</th><th>Дата</th><th>Продукт</th><th>Партия</th><th>План</th><th>Факт</th></tr>{% for b in batches %}<tr><td>{{b.id}}</td><td>{{b.created_at}}</td><td>{{b.name}} · v{{b.version}}</td><td><a href="/trace?lot={{b.lot_code}}">{{b.lot_code}}</a></td><td>{{b.planned_qty}}</td><td>{{b.actual_qty}}</td></tr>{% endfor %}</table></div>''',recipes=recipes,batches=batches,chosen=chosen)
@app.route('/trace')
@login_required
def trace():
 code=request.args.get('lot','').strip();lot=None;parents=[];children=[];ship=[]
 if code:
  with conn() as c:
   lot=c.execute('SELECT * FROM lots WHERE lot_code=?',(code,)).fetchone()
   if lot:
    if lot['kind']=='finished':
     parents=c.execute('SELECT l.lot_code,c.qty,m.name FROM batches b JOIN consumptions c ON c.batch_id=b.id JOIN lots l ON l.id=c.raw_lot_id JOIN materials m ON m.id=l.material_id WHERE b.lot_id=?',(lot['id'],)).fetchall()
     ship=c.execute('SELECT * FROM shipments WHERE lot_id=?',(lot['id'],)).fetchall()
    else:
     children=c.execute('SELECT l.lot_code,c.qty,p.name FROM consumptions c JOIN batches b ON b.id=c.batch_id JOIN lots l ON l.id=b.lot_id JOIN products p ON p.id=l.product_id WHERE c.raw_lot_id=?',(lot['id'],)).fetchall()
 return page('Двусторонняя прослеживаемость','''<div class="card"><form method="get"><label>Номер партии<input name="lot" value="{{code}}" required></label><button>Найти связи</button></form></div>{% if code and not lot %}<div class="msg">Партия не найдена</div>{% endif %}{% if lot %}<div class="card"><h2>{{lot.lot_code}} · {{'сырьё' if lot.kind=='raw' else 'готовая продукция'}}</h2><p>Остаток: {{lot.qty}} · Статус: {{lot.status}} · Годен до: {{lot.expires}}</p>{% if lot.kind=='finished' %}<h2>Из каких партий сырья произведена</h2><table><tr><th>Партия сырья</th><th>Наименование</th><th>Расход</th></tr>{% for x in parents %}<tr><td><a href="/trace?lot={{x.lot_code}}">{{x.lot_code}}</a></td><td>{{x.name}}</td><td>{{x.qty}}</td></tr>{% endfor %}</table><h2>Кому отгружено</h2><table><tr><th>Клиент</th><th>Количество</th><th>Дата</th></tr>{% for x in ship %}<tr><td>{{x.customer}}</td><td>{{x.qty}}</td><td>{{x.created_at}}</td></tr>{% endfor %}</table>{% else %}<h2>В какие партии ГП вошло сырьё</h2><table><tr><th>Партия ГП</th><th>Продукт</th><th>Расход сырья</th></tr>{% for x in children %}<tr><td><a href="/trace?lot={{x.lot_code}}">{{x.lot_code}}</a></td><td>{{x.name}}</td><td>{{x.qty}}</td></tr>{% endfor %}</table>{% endif %}</div>{% endif %}''',code=code,lot=lot,parents=parents,children=children,ship=ship)
@app.route('/shipments',methods=['GET','POST'])
@login_required
def shipments():
 if request.method=='POST':
  if not has_role('admin','warehouse'):abort(403)
  try:
   lid=int(request.form['lot_id']);qty=number(request.form,'qty');customer=request.form['customer'].strip()
   if not customer:raise ValueError('Укажите клиента')
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    if c.execute('SELECT 1 FROM containers WHERE lot_id=? AND qty>0',(lid,)).fetchone():raise ValueError('Для партии зарегистрирована тара. Отгрузка по партии заблокирована; требуется операция по таре')
    require_finished_shipping_v66(c,lid)
    updated=c.execute("UPDATE lots SET qty=qty-? WHERE id=? AND kind='finished' AND status='released' AND expires>=? AND qty>=?",(qty,lid,date.today().isoformat(),qty))
    if updated.rowcount!=1:raise ValueError('Недостаточно разрешённой годной продукции')
    c.execute('INSERT INTO shipments(lot_id,customer,qty,created_at,user_id) VALUES(?,?,?,?,?)',(lid,customer,qty,now(),session['uid']))
    c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(lid,-qty,'Отгрузка '+customer,now(),session['uid']))
    audit(c,'shipment.create',str(lid)+' '+customer+' '+str(qty))
   flash('Отгрузка проведена')
  except Exception as e:return fail(e)
  return redirect('/shipments')
 with conn() as c:
  lots=c.execute("SELECT l.*,p.name FROM lots l JOIN products p ON p.id=l.product_id WHERE l.kind='finished' AND l.status='released' AND l.qty>0 AND l.expires>=?",(date.today().isoformat(),)).fetchall()
  rows=c.execute('SELECT s.*,l.lot_code FROM shipments s JOIN lots l ON l.id=s.lot_id ORDER BY s.id DESC').fetchall()
 return page('Отгрузки','''{% if has_role('admin','warehouse') %}<div class="card"><h2>Новая отгрузка</h2><form method="post"><label>Партия<select name="lot_id">{% for l in lots %}<option value="{{l.id}}">{{l.lot_code}} · {{l.name}} ({{l.qty}})</option>{% endfor %}</select></label><label>Клиент<input name="customer" required></label><label>Количество<input name="qty" type="number" min="0.000001" step="any" required></label><button>Отгрузить</button></form></div>{% endif %}<div class="card"><table><tr><th>Дата</th><th>Партия</th><th>Клиент</th><th>Количество</th></tr>{% for x in rows %}<tr><td>{{x.created_at}}</td><td><a href="/trace?lot={{x.lot_code}}">{{x.lot_code}}</a></td><td>{{x.customer}}</td><td>{{x.qty}}</td></tr>{% endfor %}</table></div>''',lots=lots,rows=rows)
@app.route('/users',methods=['GET','POST'])
@allowed('admin')
def users():
 if request.method=='POST':
  try:
   selected=set(request.form.getlist('roles'))
   if not selected or not selected.issubset(ROLES):raise ValueError('Выберите хотя бы одну допустимую роль')
   if 'admin' in selected and len(selected)>1:raise ValueError('Роль администратора назначается отдельно')
   role=next(r for r in ROLES if r in selected)
   action=request.form.get('action','create')
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    if action=='create':
     password=request.form.get('password','')
     if len(password)<10:raise ValueError('Пароль не менее 10 символов')
     username=request.form.get('username','').strip()
     if not username:raise ValueError('Укажите логин')
     cur=c.execute('INSERT INTO users(username,password,role) VALUES(?,?,?)',(username,generate_password_hash(password),role))
     uid=cur.lastrowid
    elif action=='update':
     uid=int(request.form['user_id'])
     target=c.execute('SELECT id,username FROM users WHERE id=?',(uid,)).fetchone()
     if not target:raise ValueError('Пользователь не найден')
     if uid==session['uid'] and 'admin' not in selected:raise ValueError('Нельзя снять с себя роль администратора')
     # Keep at least one admin across the application.
     was_admin=c.execute("SELECT 1 FROM user_roles_v33 WHERE user_id=? AND role='admin'",(uid,)).fetchone()
     if was_admin and 'admin' not in selected:
      other=c.execute("SELECT 1 FROM user_roles_v33 WHERE role='admin' AND user_id<>? LIMIT 1",(uid,)).fetchone()
      if not other:raise ValueError('Нельзя снять роль с последнего администратора')
     c.execute('UPDATE users SET role=? WHERE id=?',(role,uid))
     c.execute('DELETE FROM user_roles_v33 WHERE user_id=?',(uid,))
    else:raise ValueError('Неизвестная операция')
    c.executemany('INSERT OR IGNORE INTO user_roles_v33(user_id,role) VALUES(?,?)',[(uid,r) for r in selected])
    audit(c,'user.roles.'+action,'user_id='+str(uid)+' roles='+','.join(sorted(selected)))
   flash('Роли сотрудника сохранены')
  except Exception as e:return fail(e)
  return redirect('/users')
 with conn() as c:
  rows=c.execute('SELECT id,username,role FROM users ORDER BY id').fetchall()
  role_rows=c.execute('SELECT user_id,role FROM user_roles_v33').fetchall()
 mapping={u['id']:[] for u in rows}
 for x in role_rows:mapping.setdefault(x['user_id'],[]).append(x['role'])
 return page('Пользователи и совмещение ролей','''
 <div class="card"><h2>Добавить сотрудника</h2><form method="post">
 <input type="hidden" name="action" value="create"><label>Логин<input name="username" required></label>
 <label>Пароль<input type="password" name="password" minlength="10" required></label>
 <fieldset><legend>Роли (можно выбрать несколько)</legend>{% for k,v in roles.items() %}
 <label style="display:inline-flex;flex-direction:row"><input type="checkbox" name="roles" value="{{k}}"> {{v}}</label>{% endfor %}</fieldset>
 <button>Создать сотрудника</button></form></div>
 <div class="card"><h2>Назначенные роли</h2><p class="muted">Изменения действуют с нового запроса, даже если сотрудник уже вошёл в систему.</p>
 {% for u in rows %}<form method="post" style="border-bottom:1px solid #ddd;padding:12px 0">
 <input type="hidden" name="action" value="update"><input type="hidden" name="user_id" value="{{u.id}}">
 <b>{{u.username}}</b><div>{% for k,v in roles.items() %}<label style="display:inline-flex;flex-direction:row">
 <input type="checkbox" name="roles" value="{{k}}" {% if k in mapping[u.id] %}checked{% endif %}> {{v}}</label>{% endfor %}</div>
 <button>Сохранить роли</button></form>{% endfor %}</div>
 ''',rows=rows,mapping=mapping)
@app.route('/audit')
@allowed('admin')
def audit_view():
 with conn() as c:rows=c.execute('SELECT a.*,u.username FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 300').fetchall()
 return page('Журнал действий','''<div class="card"><table><tr><th>Время</th><th>Пользователь</th><th>Действие</th><th>Детали</th></tr>{% for x in rows %}<tr><td>{{x.created_at}}</td><td>{{x.username}}</td><td>{{x.action}}</td><td>{{x.details}}</td></tr>{% endfor %}</table></div>''',rows=rows)

# v27: Packing is an allocation of finished stock to identifiable containers.
# It MUST NOT add to lots.qty or issue a stock movement. The unallocated balance
# is computed from active container balances and may be reduced by legacy routes.
def packing_available(c,lot_id):
    lot=c.execute("SELECT qty FROM lots WHERE id=? AND kind='finished'",(lot_id,)).fetchone()
    if not lot:raise ValueError('Партия готовой продукции не найдена')
    allocated=c.execute('SELECT COALESCE(SUM(qty),0) FROM containers WHERE lot_id=? AND active=1',(lot_id,)).fetchone()[0]
    return float(lot['qty'])-float(allocated)

def packing_register(c,lot_id,pack_type,count,unit_qty,prefix,token,actor):
    # Caller owns a BEGIN IMMEDIATE transaction; duplicate requests return the old job.
    old=c.execute('SELECT id FROM finished_packing_jobs WHERE request_token=?',(token,)).fetchone()
    if old:return old['id'],False
    if pack_type not in ('Канистра','Бочка','IBC','Другая тара'):raise ValueError('Неизвестный вид тары')
    if not 1<=count<=100 or not math.isfinite(unit_qty) or unit_qty<=0:raise ValueError('Неверное количество тары или масса')
    if not prefix or len(prefix)>45 or not all(ch.isalnum() or ch in '-_' for ch in prefix):raise ValueError('Префикс: 1–45 символов, буквы, цифры, - или _')
    lot=c.execute("SELECT id,lot_code,qty,status,expires FROM lots WHERE id=? AND kind='finished'",(lot_id,)).fetchone()
    if not lot:raise ValueError('Партия готовой продукции не найдена')
    if lot['status']!='released' or lot['expires']<date.today().isoformat():raise ValueError('Фасовка разрешена только для годной партии со статусом ОТК «разрешено»')
    total=count*unit_qty
    if not math.isfinite(total) or total>packing_available(c,lot_id)+0.0000001:raise ValueError('Недостаточно нераспределённого остатка готовой продукции')
    # Codes include a random job segment and ordinal; they never depend on reused lot codes.
    segment=uuid.uuid4().hex[:12].upper()
    codes=[f'{prefix}-{segment}-{i:03d}' for i in range(1,count+1)]
    job=c.execute('INSERT INTO finished_packing_jobs(lot_id,request_token,pack_type,container_count,unit_qty,total_qty,code_prefix,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?)',
        (lot_id,token,pack_type,count,unit_qty,total,prefix,now(),actor)).lastrowid
    for code in codes:
        cid=c.execute('INSERT INTO containers(lot_id,code,qty,created_at,created_by,note) VALUES(?,?,?,?,?,?)',
          (lot_id,code,unit_qty,now(),actor,'Фасовка ГП; '+pack_type+'; задание '+str(job))).lastrowid
        c.execute('INSERT INTO finished_packing_items(job_id,container_id) VALUES(?,?)',(job,cid))
    return job,True

@app.route('/packing',methods=['GET','POST'])
@login_required
def packing_view():
    if request.method=='POST':
        if not has_role('admin','warehouse','technologist'):abort(403)
        try:
            lot_id=int(request.form['lot_id']);count=int(request.form['count'])
            unit_qty=number(request.form,'unit_qty')
            pack_type=request.form.get('pack_type','');prefix=request.form.get('prefix','').strip()
            token=request.form.get('request_token','')
            if not token or len(token)>100:raise ValueError('Не указан идентификатор операции')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                job,created=packing_register(c,lot_id,pack_type,count,unit_qty,prefix,token,session['uid'])
                if created:audit(c,'packing.create',f'job={job}; lot={lot_id}; count={count}; total={count*unit_qty}')
            flash('Фасовка зарегистрирована' if created else 'Повторный запрос: возвращено ранее созданное задание')
            return redirect('/packing/'+str(job))
        except (ValueError,KeyError,sqlite3.IntegrityError) as e:return fail(e)
    with conn() as c:
        lots=c.execute("""SELECT l.id,l.lot_code,l.qty,l.status,l.expires,p.name,p.unit,
            (SELECT COALESCE(SUM(ct.qty),0) FROM containers ct WHERE ct.lot_id=l.id AND ct.active=1) allocated,
            (SELECT COUNT(*) FROM quality_passports qp WHERE qp.lot_id=l.id) passports
            FROM lots l JOIN products p ON p.id=l.product_id WHERE l.kind='finished' ORDER BY l.id DESC LIMIT 200""").fetchall()
        jobs=c.execute("""SELECT j.*,l.lot_code,u.username FROM finished_packing_jobs j
            JOIN lots l ON l.id=j.lot_id JOIN users u ON u.id=j.created_by ORDER BY j.id DESC LIMIT 100""").fetchall()
    return page('Фасовка готовой продукции',"""<div class="card"><h2>Регистрация фасовки</h2><p>Фасовка распределяет существующий остаток готовой партии по единицам тары, <b>не увеличивая остаток партии</b>. Перед началом убедитесь, что партия разрешена ОТК.</p>
    {% if has_role('admin','warehouse','technologist') %}<form method="post"><input type="hidden" name="request_token" value="{{token}}">
    <label>Готовая партия<select name="lot_id" required>{% for l in lots %}<option value="{{l.id}}">{{l.lot_code}} · {{l.name}} · свободно {{'%.3f'|format(l.qty-l.allocated)}} {{l.unit}} · ОТК {{l.status}} · паспортов {{l.passports}}</option>{% endfor %}</select></label>
    <label>Вид тары<select name="pack_type"><option>Канистра</option><option>Бочка</option><option>IBC</option><option>Другая тара</option></select></label>
    <label>Количество единиц<input type="number" name="count" min="1" max="100" value="1" required></label>
    <label>Количество продукта в одной единице (в единицах номенклатуры)<input type="number" name="unit_qty" min="0.000001" step="any" required></label>
    <label>Префикс QR-кода тары<input name="prefix" value="GP" maxlength="45" required></label>
    <button>Зарегистрировать фасовку</button></form>{% endif %}</div>
    <div class="card"><h2>Последние задания фасовки</h2><table><tr><th>№</th><th>Партия</th><th>Тара</th><th>Количество</th><th>Дата</th><th>Оператор</th></tr>{% for j in jobs %}<tr><td><a href="/packing/{{j.id}}">{{j.id}}</a></td><td>{{j.lot_code}}</td><td>{{j.pack_type}}</td><td>{{j.container_count}} × {{j.unit_qty}}</td><td>{{j.created_at}}</td><td>{{j.username}}</td></tr>{% endfor %}</table></div>""",lots=lots,jobs=jobs,token=uuid.uuid4().hex)

@app.get('/packing/<int:job_id>')
@login_required
def packing_job_view(job_id):
    with conn() as c:
        job=c.execute("""SELECT j.*,l.lot_code,l.status,l.expires,p.name,p.unit
            FROM finished_packing_jobs j JOIN lots l ON l.id=j.lot_id
            JOIN products p ON p.id=l.product_id WHERE j.id=?""",(job_id,)).fetchone()
        if not job:abort(404)
        items=c.execute("""SELECT ct.id,ct.code,ct.qty FROM finished_packing_items pi
            JOIN containers ct ON ct.id=pi.container_id WHERE pi.job_id=? ORDER BY ct.id""",(job_id,)).fetchall()
        passport=c.execute('SELECT id,passport_no FROM quality_passports WHERE lot_id=? ORDER BY id DESC LIMIT 1',(job['lot_id'],)).fetchone()
    return page('Фасовка №'+str(job_id),"""<div class="card"><h2>Фасовка №{{job.id}} · {{job.lot_code}}</h2><p>{{job.name}} · {{job.pack_type}} · {{job.container_count}} × {{job.unit_qty}} {{job.unit}}</p>
    <p>ОТК: {{job.status}} · годен до {{job.expires}}. {% if passport %}<a href="/quality/passports/{{passport.id}}/print">Паспорт качества {{passport.passport_no}}</a>{% else %}<b>Паспорт качества не выпущен</b>{% endif %}</p>
    <form method="post" action="/packing/{{job.id}}/print"><label>Формат<select name="size"><option>100x70</option><option>100x150</option></select></label><button>Печать всех этикеток</button></form>
    <table><tr><th>Код</th><th>Количество</th><th>Этикетка</th></tr>{% for x in items %}<tr><td>{{x.code}}</td><td>{{x.qty}}</td><td><a href="/containers/{{x.id}}/label">Печать</a></td></tr>{% endfor %}</table></div>""",job=job,items=items,passport=passport)

@app.post('/packing/<int:job_id>/print')
@allowed('admin','warehouse','technologist','quality')
def packing_job_print(job_id):
    fmt=request.form.get('size','100x70')
    if fmt not in ('100x70','100x150'):abort(400)
    with conn() as c:
        items=c.execute("""SELECT ct.*,l.lot_code,l.kind,l.expires,l.status,l.manufactured,
            p.name,p.unit FROM finished_packing_items pi
            JOIN containers ct ON ct.id=pi.container_id
            JOIN lots l ON l.id=ct.lot_id JOIN products p ON p.id=l.product_id
            WHERE pi.job_id=? ORDER BY ct.id LIMIT 100""",(job_id,)).fetchall()
        if not items:abort(404)
        c.execute('INSERT INTO label_print_jobs(created_at,user_id,format,mode,label_count,details) VALUES(?,?,?,?,?,?)',
            (now(),session['uid'],fmt,'batch',len(items),'packing_job='+str(job_id)))
        audit(c,'packing.labels_prepared',f'job={job_id}; count={len(items)}')
    return render_container_sheet([dict(x) for x in items],fmt)

@app.get('/tsd/packing')
@login_required
def tsd_packing_view():
    with conn() as c:
        jobs=c.execute("""SELECT j.id,j.pack_type,j.container_count,j.created_at,l.lot_code
            FROM finished_packing_jobs j JOIN lots l ON l.id=j.lot_id ORDER BY j.id DESC LIMIT 60""").fetchall()
    return page('ТСД · Фасовка',"""<div class="card"><h2>Urovo CT48 · Фасовка</h2><p>Аппаратный сканер в режиме ScanWedge / Keystroke, суффикс Enter. Сканирование ищет зарегистрированную тару и не создаёт расход.</p>
    <form method="get" action="/tsd/packing/scan"><label>QR тары<input name="code" id="packing-scan" autofocus autocomplete="off" placeholder="CHEMPRO:CONTAINER:..."></label><button>Найти тару</button></form>
    <h3>Последние фасовки</h3>{% for j in jobs %}<p><a href="/packing/{{j.id}}">№{{j.id}} · {{j.lot_code}} · {{j.pack_type}} × {{j.container_count}}</a></p>{% endfor %}</div>
    <script>const x=document.getElementById('packing-scan');if(x)x.focus();</script>""",jobs=jobs)

@app.get('/tsd/packing/scan')
@login_required
def tsd_packing_scan():
    try:code=parse_container_scan(request.args.get('code',''))
    except ValueError as e:abort(400,description=str(e))
    with conn() as c:
        row=c.execute("""SELECT ct.*,l.lot_code,l.kind,l.status,l.expires,p.name,p.unit,
          pi.job_id FROM containers ct JOIN lots l ON l.id=ct.lot_id
          JOIN products p ON p.id=l.product_id LEFT JOIN finished_packing_items pi ON pi.container_id=ct.id
          WHERE ct.code=? AND l.kind='finished' """,(code,)).fetchone()
        if not row:abort(404,description='Тара готовой продукции не найдена')
        passport=c.execute('SELECT passport_no FROM quality_passports WHERE lot_id=? ORDER BY id DESC LIMIT 1',(row['lot_id'],)).fetchone()
    return page('ТСД · Проверка тары',"""<div class="card"><h2>Тара {{x.code}}</h2><p><b>{{x.name}}</b> · партия {{x.lot_code}}</p><p>Количество: {{x.qty}} {{x.unit}} · ОТК: {{x.status}} · годен до {{x.expires}}</p><p>Статус тары: {{'активна' if x.active else 'неактивна'}}. Паспорт качества: {{passport.passport_no if passport else 'не выпущен'}}.</p><p>{% if x.job_id %}<a href="/packing/{{x.job_id}}">Задание фасовки №{{x.job_id}}</a>{% endif %}</p><p><a href="/tsd/packing">← Следующее сканирование</a></p></div>""",x=row,passport=passport)


# Direct-run bootstrap moved below all route registrations (v30).


# v13: CT48 ScanWedge keyboard input with Enter suffix. This is a browser-only
# workflow; receiving registers packaging for an EXISTING lot, not a new PO receipt.
def parse_location_scan(raw):
    value=(raw or '').strip()
    prefix='CHEMPRO:LOCATION:'
    if value.upper().startswith(prefix):value=value[len(prefix):]
    if not value or len(value)>100 or any(ord(ch)<32 for ch in value):
        raise ValueError('Некорректный код места хранения')
    return value

@app.route('/tsd/receive',methods=['GET','POST'])
@allowed('admin','warehouse')
def tsd_receive():
    if request.method=='POST':
        try:
            lot_code=(request.form.get('lot_code') or '').strip()
            code=parse_container_scan(request.form.get('container_code'))
            location=parse_location_scan(request.form.get('location'))
            qty=number(request.form,'qty')
            if not lot_code:raise ValueError('Укажите партию')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                lot=c.execute('SELECT * FROM lots WHERE lot_code=?',(lot_code,)).fetchone()
                if not lot or lot['kind']!='raw':raise ValueError('Не найдена партия сырья. Сначала оформите поступление через Закупки')
                allocated=c.execute('SELECT COALESCE(SUM(qty),0) FROM containers WHERE lot_id=? AND active=1',(lot['id'],)).fetchone()[0]
                if allocated+qty>lot['qty']+0.00000001:raise ValueError('Количество в таре превышает остаток партии')
                cur=c.execute('INSERT INTO containers(lot_id,code,qty,created_at,created_by,note) VALUES(?,?,?,?,?,?)',(lot['id'],code,qty,now(),session['uid'],'ТСД: регистрация тары'))
                c.execute('INSERT INTO container_locations(container_id,location) VALUES(?,?)',(cur.lastrowid,location))
                audit(c,'tsd.receive',f'container={cur.lastrowid}; lot={lot_code}; qty={qty}; location={location}')
            flash('Тара зарегистрирована. Партия остаётся в прежнем статусе ОТК.')
            return redirect(url_for('tsd_workplace',code=code))
        except (ValueError,sqlite3.Error) as e:flash('Ошибка: '+str(e))
    return page('ТСД · Регистрация тары',"""
    <style>.ct48{max-width:480px;margin:auto}.ct48 input,.ct48 button{font-size:18px;min-height:52px;width:100%}.ct48 label{display:block}</style>
    <div class="card ct48"><h2>Приёмка тары · Urovo CT48</h2>
    <p>Сначала оформите поступление партии через «Закупки». Здесь регистрируется тара без повторного увеличения складского остатка.</p>
    <form method="post"><input type="hidden" name="_csrf" value="{{csrf_token}}">
    <label>Номер существующей партии<input name="lot_code" required maxlength="100" autocomplete="off"></label>
    <label>QR тары<input name="container_code" required maxlength="120" autocomplete="off" placeholder="CHEMPRO:CONTAINER:..."></label>
    <label>QR места хранения<input name="location" required maxlength="120" autocomplete="off" placeholder="CHEMPRO:LOCATION:..."></label>
    <label>Количество<input type="number" name="qty" step="any" min="0.000001" required></label>
    <button>Зарегистрировать тару</button></form></div>
    """)

@app.route('/tsd/pick',methods=['GET','POST'])
@allowed('admin','warehouse')
def tsd_pick():
    if request.method=='POST':
        try:
            action=request.form.get('action','pick')
            key=request.form.get('request_key','')
            if not key or len(key)>80:raise ValueError('Не указан идентификатор операции')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                existing=c.execute('SELECT id FROM tsd_picks WHERE request_key=?',(key,)).fetchone()
                if existing:raise ValueError('Повторное проведение запрещено: операция уже записана')
                if action!='pick':raise ValueError('Неизвестная операция')
                oid=int(request.form['order_id'])
                code=parse_container_scan(request.form.get('container_code'))
                location=parse_location_scan(request.form.get('location'))
                qty=number(request.form,'qty')
                order=c.execute("SELECT * FROM work_orders WHERE id=? AND status='approved'",(oid,)).fetchone()
                if not order:raise ValueError('Задание не утверждено')
                if c.execute("SELECT 1 FROM tsd_picks WHERE order_id=? AND status='issued'",(oid,)).fetchone():raise ValueError('Сырьё уже выдано в производство')
                ct=c.execute("SELECT ct.*,l.kind,l.status,l.expires,l.qty AS lot_qty,l.material_id, COALESCE(cl.location,'Основной склад') AS location FROM containers ct JOIN lots l ON l.id=ct.lot_id LEFT JOIN container_locations cl ON cl.container_id=ct.id WHERE ct.code=?",(code,)).fetchone()
                if not ct or not ct['active'] or ct['kind']!='raw':raise ValueError('Активная тара сырья не найдена')
                if ct['status']!='released' or ct['expires']<date.today().isoformat():raise ValueError('Партия не разрешена ОТК или просрочена')
                if location!=ct['location']:raise ValueError('Сканированное место не совпадает с местом тары')
                res=c.execute('SELECT qty FROM reservations WHERE order_id=? AND lot_id=?',(oid,ct['lot_id'])).fetchone()
                if not res:raise ValueError('Для задания нет резерва этой партии')
                picked_lot=c.execute("SELECT COALESCE(SUM(qty),0) FROM tsd_picks WHERE order_id=? AND status IN ('staged','issued') AND container_id IN (SELECT id FROM containers WHERE lot_id=?)",(oid,ct['lot_id'])).fetchone()[0]
                picked_ct=c.execute("SELECT COALESCE(SUM(qty),0) FROM tsd_picks WHERE container_id=? AND status IN ('staged','issued')",(ct['id'],)).fetchone()[0]
                if qty>res['qty']-picked_lot+1e-8:raise ValueError('Количество превышает неотобранный резерв задания')
                if qty>ct['qty']-picked_ct+1e-8:raise ValueError('Количество превышает свободный остаток тары')
                c.execute('INSERT INTO tsd_picks(request_key,order_id,container_id,qty,location,created_at,user_id) VALUES(?,?,?,?,?,?,?)',(key,oid,ct['id'],qty,location,now(),session['uid']))
                audit(c,'tsd.pick',f'order={oid}; container={ct["id"]}; qty={qty}; location={location}')
            flash('Подбор записан. Остатки не списаны: это резервирование конкретной тары.')
            return redirect(url_for('tsd_pick'))
        except (ValueError,sqlite3.Error,KeyError) as e:flash('Ошибка: '+str(e))
    with conn() as c:
        orders=c.execute("SELECT id,lot_code FROM work_orders WHERE status='approved' ORDER BY id DESC LIMIT 100").fetchall()
        picks=c.execute("SELECT p.*,ct.code,o.lot_code,u.username FROM tsd_picks p JOIN containers ct ON ct.id=p.container_id JOIN work_orders o ON o.id=p.order_id JOIN users u ON u.id=p.user_id ORDER BY p.id DESC LIMIT 50").fetchall()
    return page('ТСД · Подбор по заданию',"""
    <style>.ct48{max-width:480px;margin:auto}.ct48 input,.ct48 button,.ct48 select{font-size:18px;min-height:52px;width:100%}.ct48 label{display:block}</style>
    <div class="card ct48"><h2>Подбор сырья по заданию</h2>
    <p>Сканируйте QR тары и QR места хранения. Требуется утверждённое задание и резерв партии. Подбор не списывает сырьё.</p>
    <form method="post" id="pick-form"><input type="hidden" name="_csrf" value="{{csrf_token}}"><input type="hidden" name="request_key" value="{{request_key}}">
    <label>Производственное задание<select name="order_id" required>{% for o in orders %}<option value="{{o.id}}">№{{o.id}} · {{o.lot_code}}</option>{% endfor %}</select></label>
    <label>QR тары<input name="container_code" required maxlength="120" autocomplete="off" placeholder="CHEMPRO:CONTAINER:..."></label>
    <label>QR места<input name="location" required maxlength="120" autocomplete="off" placeholder="CHEMPRO:LOCATION:..."></label>
    <label>Количество<input type="number" name="qty" min="0.000001" step="any" required></label>
    <button type="submit">Подтвердить подбор</button></form>
    <p class="muted">Списание по производственному заданию пока не подключено: подобранная тара остаётся на складе в учётных остатках.</p></div>
    <div class="card"><h2>Последние подборы</h2><table><tr><th>Задание</th><th>Тара</th><th>Количество</th><th>Место</th><th>Сотрудник</th></tr>
    {% for x in picks %}<tr><td>{{x.lot_code}}</td><td>{{x.code}}</td><td>{{x.qty}}</td><td>{{x.location}}</td><td>{{x.username}}</td></tr>{% endfor %}</table></div>
    <script>document.getElementById('pick-form').addEventListener('submit',function(e){if(this.dataset.sent){e.preventDefault();return}if(!confirm('Подтвердить подбор?')){e.preventDefault();return}this.dataset.sent='1';this.querySelector('button').disabled=true})</script>
    """,orders=orders,picks=picks,request_key=uuid.uuid4().hex)


@app.route('/tsd/return',methods=['GET','POST'])
@allowed('admin','warehouse')
def tsd_return():
    """Return issued material to warehouse BEFORE production consumption.

    No stock adjustment: v14 issue never decremented physical inventory.
    The returned quantity is removed from the issued allocation, so it can
    be picked again to satisfy the unchanged reservation.
    """
    if request.method=='POST':
        try:
            key=request.form.get('request_key','').strip()
            if len(key)!=32 or any(ch not in '0123456789abcdef' for ch in key):raise ValueError('Неверный ключ операции')
            pick_id=int(request.form['pick_id'])
            qty=number(request.form,'qty')
            note=request.form.get('note','').strip()[:300]
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                if c.execute('SELECT 1 FROM tsd_returns WHERE request_key=?',(key,)).fetchone():raise ValueError('Повторная операция возврата')
                pick=c.execute("""SELECT p.*,o.status AS order_status,ct.code AS container_code
                   FROM tsd_picks p JOIN work_orders o ON o.id=p.order_id
                   JOIN containers ct ON ct.id=p.container_id
                   WHERE p.id=? AND p.status='issued'""",(pick_id,)).fetchone()
                if not pick or pick['order_status']!='approved':raise ValueError('Выдача не найдена или задание уже завершено')
                if qty>pick['qty']+1e-8:raise ValueError('Возврат превышает выданное количество')
                remaining=round(pick['qty']-qty,8)
                if remaining<=1e-8:
                    c.execute("UPDATE tsd_picks SET status='cancelled' WHERE id=? AND status='issued'",(pick_id,))
                else:
                    c.execute("UPDATE tsd_picks SET qty=? WHERE id=? AND status='issued'",(remaining,pick_id))
                c.execute('INSERT INTO tsd_returns(request_key,pick_id,order_id,container_id,qty,created_at,user_id,note) VALUES(?,?,?,?,?,?,?,?)',
                          (key,pick_id,pick['order_id'],pick['container_id'],qty,now(),session['uid'],note))
                audit(c,'tsd.return',f"order={pick['order_id']}; container={pick['container_code']}; qty={qty}; remaining={remaining}")
            flash('Возврат зарегистрирован. Остатки не менялись: выдача ещё не была списанием. Для выпуска повторно подберите недостающее количество.')
            return redirect(url_for('tsd_return'))
        except (ValueError,KeyError,sqlite3.Error) as e:flash('Ошибка: '+str(e))
    with conn() as c:
        issued=c.execute("""SELECT p.id,p.order_id,p.qty,ct.code,o.lot_code
          FROM tsd_picks p JOIN containers ct ON ct.id=p.container_id
          JOIN work_orders o ON o.id=p.order_id
          WHERE p.status='issued' AND o.status='approved' ORDER BY p.id DESC""").fetchall()
        history=c.execute("""SELECT r.*,ct.code,u.username FROM tsd_returns r
          JOIN containers ct ON ct.id=r.container_id JOIN users u ON u.id=r.user_id
          ORDER BY r.id DESC LIMIT 100""").fetchall()
    return page('ТСД · Возврат из производства',"""
    <style>.ct48{max-width:480px;margin:auto}.ct48 input,.ct48 select,.ct48 button{min-height:50px;font-size:18px;width:100%}</style>
    <div class='card ct48'><h2>Возврат до выпуска</h2>
    <p>Возврат уменьшает выданное по заданию количество, но не складской остаток. После возврата необходимо повторно подобрать недостающее сырьё.</p>
    <form method='post' id='return-form'>
      <input type='hidden' name='_csrf' value='{{csrf_token}}'>
      <input type='hidden' name='request_key' value='{{key}}'>
      <label>Выдача<select name='pick_id' required>{% for x in issued %}<option value='{{x.id}}'>№{{x.order_id}} · {{x.code}} · {{x.qty}}</option>{% endfor %}</select></label>
      <label>Количество возврата<input type='number' name='qty' min='0.00000001' step='any' required></label>
      <label>Причина<input name='note' maxlength='300' placeholder='Причина возврата'></label>
      <button {% if not issued %}disabled{% endif %}>Подтвердить возврат</button>
    </form><p><a href='/tsd/pick'>Подбор</a> · <a href='/tsd/issue'>Выдача</a></p></div>
    <div class='card'><h2>Журнал возвратов</h2><table><tr><th>Время</th><th>Задание</th><th>Тара</th><th>Количество</th><th>Сотрудник</th><th>Причина</th></tr>
    {% for x in history %}<tr><td>{{x.created_at}}</td><td>{{x.order_id}}</td><td>{{x.code}}</td><td>{{x.qty}}</td><td>{{x.username}}</td><td>{{x.note}}</td></tr>{% else %}<tr><td colspan='6'>Возвратов нет</td></tr>{% endfor %}</table></div>
    <script>document.getElementById('return-form').addEventListener('submit',function(e){if(this.dataset.sent){e.preventDefault();return}if(!confirm('Подтвердить возврат?')){e.preventDefault();return}this.dataset.sent='1';this.querySelector('button').disabled=true})</script>
    """,issued=issued,history=history,key=uuid.uuid4().hex)

@app.route('/tsd/issue',methods=['GET','POST'])
@allowed('admin','warehouse')
def tsd_issue():
    if request.method=='POST':
        try:
            oid=int(request.form['order_id']);key=request.form.get('request_key','')
            if len(key)!=32 or not all(x in '0123456789abcdef' for x in key):raise ValueError('Неверный ключ операции')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                if c.execute('SELECT 1 FROM tsd_issues WHERE request_key=?',(key,)).fetchone():raise ValueError('Выдача уже проведена')
                order=c.execute("SELECT * FROM work_orders WHERE id=? AND status='approved'",(oid,)).fetchone()
                if not order:raise ValueError('Задание не утверждено')
                picks=c.execute("SELECT p.*,ct.qty AS container_qty,ct.active,l.status AS qc,l.expires,l.qty AS lot_qty,ct.lot_id FROM tsd_picks p JOIN containers ct ON ct.id=p.container_id JOIN lots l ON l.id=ct.lot_id WHERE p.order_id=? AND p.status='staged'",(oid,)).fetchall()
                if not picks:raise ValueError('Нет подобранной тары для выдачи')
                reservations=c.execute('SELECT lot_id,qty FROM reservations WHERE order_id=?',(oid,)).fetchall()
                if not reservations:raise ValueError('Отсутствует резерв сырья')
                issued_existing=c.execute("SELECT ct.lot_id,SUM(p.qty) AS qty FROM tsd_picks p JOIN containers ct ON ct.id=p.container_id WHERE p.order_id=? AND p.status='issued' GROUP BY ct.lot_id",(oid,)).fetchall()
                already={x['lot_id']:x['qty'] for x in issued_existing}
                for r in reservations:
                    selected=[x for x in picks if x['lot_id']==r['lot_id']]
                    if abs(sum(x['qty'] for x in selected)+already.get(r['lot_id'],0)-r['qty'])>0.000001:raise ValueError('Сумма ранее выданного и нового подбора не соответствует резерву партии '+str(r['lot_id']))
                if any(x['lot_id'] not in {r['lot_id'] for r in reservations} for x in picks):raise ValueError('Подбор вне резервов')
                for x in picks:
                    if not x['active'] or x['qc']!='released' or x['expires']<date.today().isoformat():raise ValueError('Тара недоступна по ОТК или сроку годности')
                    if x['qty']>x['container_qty']+1e-8:raise ValueError('Недостаточно остатка в таре')
                c.execute("UPDATE tsd_picks SET status='issued' WHERE order_id=? AND status='staged'",(oid,))
                c.execute('INSERT INTO tsd_issues(request_key,order_id,created_at,user_id) VALUES(?,?,?,?)',(key,oid,now(),session['uid']))
                audit(c,'tsd.issue',f'order={oid}; containers={len(picks)}')
            flash('Сырьё выдано в производство. Остатки спишутся атомарно при выпуске партии.')
            return redirect(url_for('tsd_issue'))
        except (ValueError,sqlite3.Error,KeyError) as e:flash('Ошибка: '+str(e))
    with conn() as c:
        orders=c.execute("SELECT o.id,o.lot_code,COUNT(p.id) AS pieces,SUM(p.qty) AS qty FROM work_orders o JOIN tsd_picks p ON p.order_id=o.id AND p.status='staged' WHERE o.status='approved' GROUP BY o.id ORDER BY o.id DESC").fetchall()
        issued=c.execute("SELECT o.id,o.lot_code,COUNT(p.id) AS pieces FROM work_orders o JOIN tsd_picks p ON p.order_id=o.id AND p.status='issued' GROUP BY o.id ORDER BY o.id DESC LIMIT 20").fetchall()
    return page('ТСД · Выдача в производство',"""
    <style>.ct48{max-width:480px;margin:auto}.ct48 button,.ct48 select{min-height:52px;font-size:18px;width:100%}</style>
    <div class='card ct48'><h2>Выдача подобранного сырья</h2><p>Подбор должен полностью совпадать с резервами задания. Выдача не уменьшает остатки: списание происходит при проведении выпуска.</p>
    <form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='request_key' value='{{key}}'>
    <label>Задание<select name='order_id' required>{% for o in orders %}<option value='{{o.id}}'>№{{o.id}} · {{o.lot_code}} · {{o.pieces}} мест · {{o.qty}}</option>{% endfor %}</select></label>
    <button {% if not orders %}disabled{% endif %} onclick="return confirm('Подтвердить выдачу в производство?')">Подтвердить выдачу</button></form>
    <p><a href='/tsd/pick'>Подбор сырья</a> · <a href='/tsd/return'>Возврат</a> · <a href='/production'>Производство</a></p></div>
    <div class='card'><h2>Выдано, ожидает выпуска</h2>{% for o in issued %}<p>Задание №{{o.id}} · {{o.lot_code}} · {{o.pieces}} мест</p>{% else %}<p>Нет ожидающих выдач</p>{% endfor %}</div>
    """,orders=orders,issued=issued,key=uuid.uuid4().hex)

@app.route('/reconciliation')
@login_required
def reconciliation():
 """Read-only operational reconciliation; no automatic stock corrections."""
 if not has_role('admin','warehouse','technologist','operator'):abort(403)
 with conn() as c:
  stock=c.execute("""SELECT l.id,l.lot_code,l.qty AS lot_qty,m.name AS material,m.unit,
   COALESCE((SELECT SUM(ct.qty) FROM containers ct WHERE ct.lot_id=l.id AND ct.active=1),0) AS container_qty,
   (SELECT COUNT(*) FROM containers ct WHERE ct.lot_id=l.id) AS container_count,
   (SELECT COUNT(*) FROM containers ct WHERE ct.lot_id=l.id AND ct.active=0 AND ct.qty>0.000001) AS inactive_positive
   FROM lots l JOIN materials m ON m.id=l.material_id WHERE l.kind='raw' ORDER BY l.id DESC""").fetchall()
  stock_rows=[]
  for x in stock:
   d=dict(x)
   d['difference']=round(d['lot_qty']-d['container_qty'],6)
   d['alert']=bool(d['inactive_positive'] or (d['container_count'] and abs(d['difference'])>0.000001))
   stock_rows.append(d)
  batches=c.execute("""SELECT b.id,b.planned_qty,b.actual_qty,l.lot_code,p.name AS product,r.id AS recipe_id
   FROM batches b JOIN lots l ON l.id=b.lot_id JOIN recipes r ON r.id=b.recipe_id
   JOIN products p ON p.id=r.product_id ORDER BY b.id DESC LIMIT 100""").fetchall()
  production_rows=[]
  for b in batches:
   recipe=c.execute('SELECT material_id,percent FROM recipe_items WHERE recipe_id=?',(b['recipe_id'],)).fetchall()
   consumed=c.execute("""SELECT l.material_id,SUM(x.qty) AS qty FROM consumptions x
    JOIN lots l ON l.id=x.raw_lot_id WHERE x.batch_id=? GROUP BY l.material_id""",(b['id'],)).fetchall()
   actual={x['material_id']:x['qty'] for x in consumed}
   for r in recipe:
    material=c.execute('SELECT name,unit FROM materials WHERE id=?',(r['material_id'],)).fetchone()
    planned=round(b['planned_qty']*r['percent']/100,6)
    used=round(actual.pop(r['material_id'],0),6)
    production_rows.append(dict(batch_id=b['id'],lot_code=b['lot_code'],product=b['product'],material=material['name'],unit=material['unit'],norm=planned,recorded=used,difference=round(used-planned,6)))
   for material_id,used in actual.items():
    material=c.execute('SELECT name,unit FROM materials WHERE id=?',(material_id,)).fetchone()
    production_rows.append(dict(batch_id=b['id'],lot_code=b['lot_code'],product=b['product'],material=material['name'] if material else str(material_id),unit=material['unit'] if material else '',norm=0,recorded=used,difference=round(used,6)))
 return page('Сверка склада и расхода',"""
 <div class='card'><h2>Сверка остатков партии и тары</h2>
 <p>Разница = остаток партии − сумма остатков активной тары. Для партий без зарегистрированной тары сравнение неприменимо. Данные доступны только для чтения.</p>
 <table><tr><th>Партия</th><th>Материал</th><th>Партия</th><th>Тара</th><th>Разница</th><th>Состояние</th></tr>
 {% for x in stock_rows %}<tr><td>{{x.lot_code}}</td><td>{{x.material}}</td><td>{{'%.3f'|format(x.lot_qty)}} {{x.unit}}</td><td>{% if x.container_count %}{{'%.3f'|format(x.container_qty)}} {{x.unit}}{% else %}—{% endif %}</td><td>{% if x.container_count %}{{'%.3f'|format(x.difference)}}{% else %}—{% endif %}</td><td>{% if x.inactive_positive %}Ошибка: неактивная тара с остатком{% elif x.alert %}<strong>Требуется сверка</strong>{% elif x.container_count %}Совпадает{% else %}Тара не учтена{% endif %}</td></tr>{% else %}<tr><td colspan='6'>Партий нет</td></tr>{% endfor %}</table></div>
 <div class='card'><h2>Нормативный и учтённый расход</h2><p>Норматив = плановый выпуск × доля компонента в рецептуре. Учтённый расход берётся из документов списания, а не из измерений весов. Отклонение = учтённый − норматив.</p>
 <table><tr><th>Выпуск</th><th>Материал</th><th>Норматив</th><th>Учтено</th><th>Отклонение</th></tr>
 {% for x in production_rows %}<tr><td>№{{x.batch_id}} · {{x.lot_code}}</td><td>{{x.material}}</td><td>{{'%.3f'|format(x.norm)}} {{x.unit}}</td><td>{{'%.3f'|format(x.recorded)}} {{x.unit}}</td><td>{% if x.difference|abs > 0.000001 %}<strong>{{'%+.3f'|format(x.difference)}}</strong>{% else %}0{% endif %}</td></tr>{% else %}<tr><td colspan='5'>Выпусков нет</td></tr>{% endfor %}</table></div>
 <div class='card'><p><strong>Важно:</strong> отчёт не проводит корректировки, не подтверждает фактическое взвешивание и не заменяет инвентаризацию. Разницу нельзя автоматически считать недостачей: партия может быть не полностью распределена по таре.</p></div>
 """,stock_rows=stock_rows,production_rows=production_rows)



# v17: detailed process parameters and immutable issued production sheets.
def sheet_snapshot(c,order):
 recipe=c.execute('SELECT r.*,p.name AS product FROM recipes r JOIN products p ON p.id=r.product_id WHERE r.id=?',(order['recipe_id'],)).fetchone()
 if not recipe or recipe['status']!='approved':raise ValueError('Рецептура не утверждена')
 steps=[]
 for st in c.execute('SELECT * FROM steps WHERE recipe_id=? ORDER BY position,id',(recipe['id'],)).fetchall():
  params=[dict(x) for x in c.execute('SELECT * FROM tech_parameters WHERE step_id=? ORDER BY id',(st['id'],)).fetchall()]
  steps.append({'id':st['id'],'position':st['position'],'instruction':st['instruction'],'control_point':st['control_point'],'parameters':params})
 items=[]
 for x in c.execute('SELECT m.id AS material_id,m.name,m.unit,i.percent FROM recipe_items i JOIN materials m ON m.id=i.material_id WHERE i.recipe_id=? ORDER BY i.id',(recipe['id'],)).fetchall():
  items.append({'material_id':x['material_id'],'name':x['name'],'unit':x['unit'],'percent':x['percent'],'planned':round(order['planned_qty']*x['percent']/100,6)})
 return {'order_id':order['id'],'lot_code':order['lot_code'],'product':recipe['product'],'recipe_id':recipe['id'],'recipe_version':recipe['version'],'planned_qty':order['planned_qty'],'steps':steps,'items':items}

def sheet_data(c,sheet_id):
 sheet=c.execute('SELECT * FROM production_sheets WHERE id=?',(sheet_id,)).fetchone()
 if not sheet:abort(404)
 snapshot=json.loads(sheet['snapshot_json'])
 readings=[dict(x) for x in c.execute('SELECT r.*,u.username FROM sheet_readings r JOIN users u ON u.id=r.entered_by WHERE sheet_id=? ORDER BY r.id',(sheet_id,)).fetchall()]
 return sheet,snapshot,readings

@app.route('/tech-cards',methods=['GET','POST'])
@login_required
def tech_cards_v17():
 if request.method=='POST':
  if not has_role('admin','technologist'):abort(403)
  try:
   sid=int(request.form['step_id']);name=request.form['name'].strip()[:100]
   if not name:raise ValueError('Укажите название параметра')
   lo=request.form.get('min_value','').strip();hi=request.form.get('max_value','').strip()
   low=float(lo.replace(',','.')) if lo else None;high=float(hi.replace(',','.')) if hi else None
   if (low is not None and not math.isfinite(low)) or (high is not None and not math.isfinite(high)):raise ValueError('Некорректный допуск')
   if low is not None and high is not None and low>high:raise ValueError('Минимум больше максимума')
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    st=c.execute('SELECT s.*,r.status FROM steps s JOIN recipes r ON r.id=s.recipe_id WHERE s.id=?',(sid,)).fetchone()
    if not st or st['status']!='draft':raise ValueError('Параметры можно добавлять только в черновик рецептуры')
    c.execute('INSERT INTO tech_parameters(step_id,name,unit,min_value,max_value,required,method) VALUES(?,?,?,?,?,?,?)',(sid,name,request.form.get('unit','').strip()[:30],low,high,1 if request.form.get('required') else 0,request.form.get('method','').strip()[:200]))
    audit(c,'tech_parameter.create','step='+str(sid)+' '+name)
   flash('Контрольный параметр добавлен')
  except (ValueError,sqlite3.Error) as e:return fail(e)
  return redirect('/tech-cards')
 with conn() as c:
  rows=c.execute('SELECT s.id AS step_id,s.position,s.instruction,r.id AS recipe_id,r.version,r.status,p.name AS product FROM steps s JOIN recipes r ON r.id=s.recipe_id JOIN products p ON p.id=r.product_id ORDER BY r.id DESC,s.position,s.id').fetchall()
  params=c.execute('SELECT * FROM tech_parameters ORDER BY step_id,id').fetchall()
 return page('Контрольные параметры ТК',"""
 <div class='card'><p>Параметры привязаны к шагам рецептуры. После утверждения рецептуры редактирование запрещено; для изменений создайте новую редакцию рецептуры.</p>
 {% for x in rows %}<h3>{{x.product}} · редакция {{x.version}} · этап {{x.position}}: {{x.instruction}} ({{x.status}})</h3>
 <table><tr><th>Параметр</th><th>Допуск</th><th>Метод</th><th>Обязательный</th></tr>{% for p in params if p.step_id==x.step_id %}<tr><td>{{p.name}} ({{p.unit}})</td><td>{{p.min_value if p.min_value is not none else '—'}} – {{p.max_value if p.max_value is not none else '—'}}</td><td>{{p.method}}</td><td>{{'Да' if p.required else 'Нет'}}</td></tr>{% endfor %}</table>
 {% if x.status=='draft' and has_role('admin','technologist') %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='step_id' value='{{x.step_id}}'><label>Параметр<input name='name' required placeholder='Температура, pH, плотность…'></label><label>Единица<input name='unit' placeholder='°C'></label><label>Минимум<input name='min_value' inputmode='decimal'></label><label>Максимум<input name='max_value' inputmode='decimal'></label><label>Метод измерения<input name='method'></label><label><input type='checkbox' name='required' checked> Обязательный</label><button>Добавить параметр</button></form>{% endif %}
 {% else %}<p>Этапы пока не созданы.</p>{% endfor %}</div>""",rows=rows,params=params)


# v20: read-only production technologist dashboard. No stock mutations.
def sheet_dashboard_rows(c):
 sheets=c.execute("""SELECT s.id,s.order_id,s.status,s.issued_at,s.snapshot_json,
    w.lot_code,w.due_date,u.username AS issued_by_name,
    p.copy_number,p.received_at AS paper_received,
    (SELECT COUNT(*) FROM sheet_attachments a WHERE a.sheet_id=s.id) AS attachments
    FROM production_sheets s JOIN work_orders w ON w.id=s.order_id
    LEFT JOIN users u ON u.id=s.issued_by
    LEFT JOIN sheet_paper_register p ON p.sheet_id=s.id
    ORDER BY s.id DESC""").fetchall()
 result=[]
 for sheet in sheets:
  snap=json.loads(sheet['snapshot_json'])
  required={int(p['id']) for step in snap.get('steps',[]) for p in step.get('parameters',[]) if p.get('required')}
  all_parameters={int(p['id']) for step in snap.get('steps',[]) for p in step.get('parameters',[])}
  readings=c.execute('SELECT id,parameter_id,out_of_range,disposition FROM sheet_readings WHERE sheet_id=? ORDER BY id',(sheet['id'],)).fetchall()
  latest={r['parameter_id']:r for r in readings}
  missing=len(required-set(latest))
  # A corrected reading is superseded; only current readings count for dashboard attention.
  pending=sum(1 for r in latest.values() if r['out_of_range'] and r['disposition']!='accepted')
  row=dict(sheet)
  row.pop('snapshot_json')
  row.update(product=snap.get('product','—'),recipe_version=snap.get('recipe_version','—'),
             required_total=len(required),required_done=len(required)-missing,
             missing=missing,pending=pending,entered=len(all_parameters & set(latest)),
             attention=bool(missing or pending or not sheet['paper_received']),
             ready=bool(not missing and not pending and sheet['paper_received']))
  result.append(row)
 return result

@app.get('/production/dashboard')
@login_required
def production_dashboard_v20():
 if not has_role('admin','technologist','quality','viewer'):abort(403)
 status=request.args.get('status','all')
 if status not in ('all','issued','review','closed'):status='all'
 focus=request.args.get('focus','all')
 if focus not in ('all','attention','ready','missing','deviations'):focus='all'
 search=request.args.get('q','').strip()[:80].casefold()
 with conn() as c: rows=sheet_dashboard_rows(c)
 summary={'total':len(rows),'issued':sum(r['status']=='issued' for r in rows),
          'review':sum(r['status']=='review' for r in rows),
          'closed':sum(r['status']=='closed' for r in rows),
          'attention':sum(r['attention'] and r['status']!='closed' for r in rows),
          'deviations':sum(r['pending']>0 and r['status']!='closed' for r in rows)}
 if status!='all':rows=[r for r in rows if r['status']==status]
 if focus=='attention':rows=[r for r in rows if r['attention'] and r['status']!='closed']
 elif focus=='ready':rows=[r for r in rows if r['ready'] and r['status']!='closed']
 elif focus=='missing':rows=[r for r in rows if r['missing'] and r['status']!='closed']
 elif focus=='deviations':rows=[r for r in rows if r['pending'] and r['status']!='closed']
 if search:rows=[r for r in rows if search in str(r['id']).casefold() or search in str(r['lot_code']).casefold() or search in str(r['product']).casefold()]
 return page('Пульт технолога · производственные листы',"""
 <div class='card'><h2>Сводка по производственным листам</h2>
 <p>Всего: <b>{{summary.total}}</b> · Выданы: <b>{{summary.issued}}</b> · На проверке: <b>{{summary.review}}</b> · Закрыты: <b>{{summary.closed}}</b></p>
 <p>Требуют внимания: <b>{{summary.attention}}</b> · С нерассмотренными отклонениями: <b>{{summary.deviations}}</b></p>
 <p class='muted'>Отчёт только для просмотра. Подтверждение измерений, отклонений и закрытие выполняются в самом производственном листе.</p></div>
 <div class='card'><form method='get' action='/production/dashboard'>
 <label>Поиск по номеру листа, партии или продукции<input name='q' value='{{request.args.get("q", "")[:80]}}' maxlength='80'></label>
 <label>Статус<select name='status'><option value='all' {% if status=='all' %}selected{% endif %}>Все</option><option value='issued' {% if status=='issued' %}selected{% endif %}>Выданные</option><option value='review' {% if status=='review' %}selected{% endif %}>На проверке</option><option value='closed' {% if status=='closed' %}selected{% endif %}>Закрытые</option></select></label>
 <label>Контроль<select name='focus'><option value='all' {% if focus=='all' %}selected{% endif %}>Все</option><option value='attention' {% if focus=='attention' %}selected{% endif %}>Требуют внимания</option><option value='missing' {% if focus=='missing' %}selected{% endif %}>Незаполненные точки</option><option value='deviations' {% if focus=='deviations' %}selected{% endif %}>Отклонения</option><option value='ready' {% if focus=='ready' %}selected{% endif %}>Готовы к проверке</option></select></label>
 <button>Показать</button> <a href='/production/dashboard'>Сбросить фильтры</a></form></div>
 <div class='card'><h2>Производственные листы ({{rows|length}})</h2><div style='overflow-x:auto'><table><tr><th>Лист</th><th>Продукция / партия</th><th>Статус</th><th>Обязательные точки</th><th>Отклонения</th><th>Бумага / сканы</th><th>Действие</th></tr>
 {% for r in rows %}<tr><td>ПЛ-{{r.id}}<br><small>{{r.issued_at}}</small></td><td>{{r.product}}<br><small>{{r.lot_code}}</small></td><td>{{'Выдан' if r.status=='issued' else 'На проверке' if r.status=='review' else 'Закрыт'}}</td>
 <td>{{r.required_done}} / {{r.required_total}}{% if r.missing %}<br><strong>Не заполнено: {{r.missing}}</strong>{% endif %}</td>
 <td>{% if r.pending %}<strong>Нужно рассмотреть: {{r.pending}}</strong>{% else %}Нет нерассмотренных{% endif %}</td>
 <td>{% if r.paper_received %}Оригинал №{{r.copy_number}}{% else %}<strong>Не зарегистрирован</strong>{% endif %}<br>Сканов: {{r.attachments}}</td>
 <td><a href='/production/sheets/{{r.id}}'>Открыть</a> · <a href='/production/sheets/{{r.id}}/print' target='_blank'>Печать</a></td></tr>
 {% else %}<tr><td colspan='7'>По выбранным условиям листов нет</td></tr>{% endfor %}</table></div></div>
 """,rows=rows,summary=summary,status=status,focus=focus)

@app.route('/production/sheets',methods=['GET','POST'])
@login_required
def production_sheets_v17():
 if request.method=='POST':
  if not has_role('admin','technologist'):abort(403)
  try:
   oid=int(request.form['order_id'])
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    order=c.execute("SELECT * FROM work_orders WHERE id=? AND status='approved'",(oid,)).fetchone()
    if not order:raise ValueError('Можно выдать лист только для утверждённого задания')
    snap=sheet_snapshot(c,order)
    cur=c.execute('INSERT INTO production_sheets(order_id,snapshot_json,issued_at,issued_by) VALUES(?,?,?,?)',(oid,json.dumps(snap,ensure_ascii=False),now(),session['uid']))
    audit(c,'production_sheet.issue','sheet='+str(cur.lastrowid)+' order='+str(oid))
   return redirect('/production/sheets/'+str(cur.lastrowid))
  except (ValueError,sqlite3.Error) as e:return fail(e)
 with conn() as c:
  orders=c.execute("SELECT w.id,w.lot_code FROM work_orders w LEFT JOIN production_sheets s ON s.order_id=w.id WHERE w.status='approved' AND s.id IS NULL ORDER BY w.id DESC").fetchall()
  rows=c.execute('SELECT s.id,s.order_id,s.status,s.issued_at,w.lot_code FROM production_sheets s JOIN work_orders w ON w.id=s.order_id ORDER BY s.id DESC').fetchall()
 return page('Производственные листы',"""
 {% if has_role('admin','technologist') %}<div class='card'><h2>Выдать производственный лист</h2><form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><label>Утверждённое задание<select name='order_id' required>{% for o in orders %}<option value='{{o.id}}'>№{{o.id}} · {{o.lot_code}}</option>{% endfor %}</select></label><button {% if not orders %}disabled{% endif %}>Выдать лист</button></form><p class='muted'>При выдаче создаётся неизменяемый снимок состава, операций и допусков. Один лист на одно задание.</p></div>{% endif %}
 <div class='card'><table><tr><th>Лист</th><th>Задание</th><th>Статус</th><th>Выдан</th><th>Открыть</th></tr>{% for s in rows %}<tr><td>ПЛ-{{s.id}}</td><td>{{s.lot_code}}</td><td>{{s.status}}</td><td>{{s.issued_at}}</td><td><a href='/production/sheets/{{s.id}}'>Заполнить</a> · <a href='/production/sheets/{{s.id}}/print'>Печать</a></td></tr>{% endfor %}</table></div>""",orders=orders,rows=rows)

@app.route('/production/sheets/<int:sheet_id>',methods=['GET','POST'])
@login_required
def production_sheet_v17(sheet_id):
 if request.method=='POST':
  if not has_role('admin','technologist','operator','quality'):abort(403)
  try:
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    sheet,snap,_=sheet_data(c,sheet_id)
    if sheet['status']=='closed':raise ValueError('Закрытый лист нельзя изменять')
    action=request.form.get('action','reading')
    if action=='bulk_readings':
     if sheet['status']!='issued':raise ValueError('Пакетный ввод доступен только до передачи на проверку')
     mode=request.form.get('mode','after_shift')
     if mode not in ('live','after_shift'):raise ValueError('Некорректный режим')
     params={p['id']:p for st in snap['steps'] for p in st['parameters']}
     prepared=[]
     for pid,p in params.items():
      raw=request.form.get('value_'+str(pid),'').strip()
      if not raw:continue
      value=float(raw.replace(',','.'))
      if not math.isfinite(value):raise ValueError('Некорректное значение '+p['name'])
      measured_dt=datetime.fromisoformat(request.form.get('measured_'+str(pid),'').strip())
      if measured_dt>datetime.now()+timedelta(minutes=5):raise ValueError('Время измерения в будущем: '+p['name'])
      out=(p['min_value'] is not None and value<p['min_value']) or (p['max_value'] is not None and value>p['max_value'])
      comment=request.form.get('comment_'+str(pid),'').strip()[:1000]
      if out and not comment:raise ValueError('Нужно пояснение отклонения: '+p['name'])
      prepared.append((sheet_id,pid,p['step_id'],value,measured_dt.isoformat(timespec='minutes'),now(),session['uid'],mode,'paper' if mode=='after_shift' else 'direct',comment,int(out)))
     if not prepared:raise ValueError('Нет значений для сохранения')
     c.executemany('INSERT INTO sheet_readings(sheet_id,parameter_id,step_id,value,measured_at,entered_at,entered_by,mode,source,comment,out_of_range) VALUES(?,?,?,?,?,?,?,?,?,?,?)',prepared)
     audit(c,'production_sheet.bulk_readings','sheet='+str(sheet_id)+' count='+str(len(prepared)))
    elif action=='upload_paper':
     if not has_role('admin','technologist','quality'):abort(403)
     if not c.execute('SELECT 1 FROM sheet_paper_register WHERE sheet_id=?',(sheet_id,)).fetchone():raise ValueError('Сначала зарегистрируйте бумажный оригинал')
     f=request.files.get('document')
     if not f or not f.filename:raise ValueError('Выберите файл')
     data=f.stream.read(5*1024*1024+1)
     if len(data)>5*1024*1024:raise ValueError('Файл больше 5 МБ')
     if data.startswith(b'%PDF-'):mime,ext='application/pdf','.pdf'
     elif data.startswith(b'\x89PNG\r\n\x1a\n'):mime,ext='image/png','.png'
     elif data.startswith(b'\xff\xd8\xff'):mime,ext='image/jpeg','.jpg'
     else:raise ValueError('Разрешены PDF, PNG и JPEG')
     if not data:raise ValueError('Пустой файл')
     count=c.execute('SELECT COUNT(*) FROM sheet_attachments WHERE sheet_id=?',(sheet_id,)).fetchone()[0]
     if count>=10:raise ValueError('Не более 10 файлов на лист')
     name='PL-'+str(sheet_id)+'-scan-'+str(count+1)+ext
     c.execute('INSERT INTO sheet_attachments(sheet_id,filename,mime,sha256,payload,uploaded_at,uploaded_by) VALUES(?,?,?,?,?,?,?)',(sheet_id,name,mime,hashlib.sha256(data).hexdigest(),data,now(),session['uid']))
     audit(c,'production_sheet.paper_upload','sheet='+str(sheet_id)+' filename='+name)
    elif action=='reading':
     pid=int(request.form['parameter_id']);parameter=next((p for st in snap['steps'] for p in st['parameters'] if p['id']==pid),None)
     if not parameter:raise ValueError('Параметр отсутствует в выданной редакции ТК')
     raw=request.form['value'].strip().replace(',','.');value=float(raw)
     if not math.isfinite(value):raise ValueError('Некорректное значение')
     measured=request.form['measured_at'].strip()
     measured_dt=datetime.fromisoformat(measured)
     if measured_dt>datetime.now()+timedelta(minutes=5):raise ValueError('Время измерения не может быть в будущем')
     mode=request.form.get('mode','after_shift')
     if mode not in ('live','after_shift'):raise ValueError('Некорректный режим')
     out=(parameter['min_value'] is not None and value<parameter['min_value']) or (parameter['max_value'] is not None and value>parameter['max_value'])
     comment=request.form.get('comment','').strip()[:1000]
     if out and not comment:raise ValueError('При отклонении обязательно пояснение')
     c.execute('INSERT INTO sheet_readings(sheet_id,parameter_id,step_id,value,measured_at,entered_at,entered_by,mode,source,comment,out_of_range) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(sheet_id,pid,parameter['step_id'],value,measured_dt.isoformat(timespec='minutes'),now(),session['uid'],mode,'paper' if mode=='after_shift' else 'direct',comment,int(out)))
     audit(c,'production_sheet.reading','sheet='+str(sheet_id)+' param='+str(pid)+' deviation='+str(int(out)))
    elif action=='paper':
     if not has_role('admin','technologist','quality'):abort(403)
     number=request.form.get('copy_number','').strip()[:100]
     location=request.form.get('storage_location','').strip()[:200]
     if not number or not location:raise ValueError('Укажите номер бумажного экземпляра и место хранения')
     if c.execute('SELECT 1 FROM sheet_paper_register WHERE sheet_id=?',(sheet_id,)).fetchone():raise ValueError('Бумажный оригинал уже зарегистрирован')
     c.execute('INSERT INTO sheet_paper_register(sheet_id,copy_number,received_at,received_by,storage_location,notes) VALUES(?,?,?,?,?,?)',(sheet_id,number,now(),session['uid'],location,request.form.get('notes','').strip()[:500]))
     audit(c,'production_sheet.paper_received','sheet='+str(sheet_id)+' copy='+number)
    elif action=='correct':
     old_id=int(request.form['old_reading_id'])
     reason=request.form.get('reason','').strip()[:1000]
     if not reason:raise ValueError('Укажите причину исправления')
     old=c.execute('SELECT * FROM sheet_readings WHERE id=? AND sheet_id=?',(old_id,sheet_id)).fetchone()
     if not old:raise ValueError('Исходное измерение не найдено')
     if c.execute('SELECT 1 FROM sheet_corrections WHERE old_reading_id=?',(old_id,)).fetchone():raise ValueError('Это измерение уже исправлено')
     if c.execute('SELECT 1 FROM sheet_readings WHERE sheet_id=? AND parameter_id=? AND id>?',(sheet_id,old['parameter_id'],old_id)).fetchone():raise ValueError('Исправлять можно только последнее измерение параметра')
     parameter=next((p for st in snap['steps'] for p in st['parameters'] if p['id']==old['parameter_id']),None)
     if not parameter:raise ValueError('Параметр отсутствует в снимке карты')
     value=float(request.form['value'].strip().replace(',','.'))
     if not math.isfinite(value):raise ValueError('Некорректное значение')
     measured_dt=datetime.fromisoformat(request.form['measured_at'].strip())
     if measured_dt>datetime.now()+timedelta(minutes=5):raise ValueError('Время измерения в будущем')
     out=(parameter['min_value'] is not None and value<parameter['min_value']) or (parameter['max_value'] is not None and value>parameter['max_value'])
     cur=c.execute('INSERT INTO sheet_readings(sheet_id,parameter_id,step_id,value,measured_at,entered_at,entered_by,mode,source,comment,out_of_range) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(sheet_id,old['parameter_id'],old['step_id'],value,measured_dt.isoformat(timespec='minutes'),now(),session['uid'],old['mode'],old['source'],reason,int(out)))
     c.execute('INSERT INTO sheet_corrections(sheet_id,old_reading_id,new_reading_id,reason,created_at,created_by) VALUES(?,?,?,?,?,?)',(sheet_id,old_id,cur.lastrowid,reason,now(),session['uid']))
     audit(c,'production_sheet.correct','sheet='+str(sheet_id)+' old='+str(old_id)+' new='+str(cur.lastrowid))
    elif action=='review':
     if not has_role('admin','technologist','quality'):abort(403)
     if not c.execute('SELECT 1 FROM sheet_paper_register WHERE sheet_id=?',(sheet_id,)).fetchone():raise ValueError('Сначала зарегистрируйте бумажный оригинал')
     c.execute("UPDATE production_sheets SET status='review' WHERE id=?",(sheet_id,))
     audit(c,'production_sheet.review','sheet='+str(sheet_id))
    elif action=='disposition':
     if not has_role('admin','technologist','quality'):abort(403)
     rid=int(request.form['reading_id']);choice=request.form['choice'];reason=request.form.get('reason','').strip()
     if choice not in ('accepted','rejected') or not reason:raise ValueError('Укажите решение и причину')
     r=c.execute("SELECT id FROM sheet_readings WHERE id=? AND sheet_id=? AND out_of_range=1 AND disposition='pending'",(rid,sheet_id)).fetchone()
     if not r:raise ValueError('Отклонение не найдено или уже рассмотрено')
     c.execute('UPDATE sheet_readings SET disposition=?,disposition_by=?,disposition_at=?,disposition_reason=? WHERE id=?',(choice,session['uid'],now(),reason[:1000],rid))
     audit(c,'production_sheet.deviation','sheet='+str(sheet_id)+' reading='+str(rid)+' '+choice)
    elif action=='close':
     if not has_role('admin','technologist'):abort(403)
     # In post-close mode, first record the physical release into quarantine.
     if os.environ.get('LOVETECH_V60_RELEASE_SEQUENCE','legacy')=='post_close':
      order_state=c.execute('SELECT status,batch_id FROM work_orders WHERE id=?',(sheet['order_id'],)).fetchone()
      if not order_state or order_state['status']!='completed' or not order_state['batch_id']:
       raise ValueError('Сначала зарегистрируйте выпуск партии на карантин, затем закройте производственный лист')
     latest=c.execute('SELECT parameter_id,MAX(id) AS id FROM sheet_readings WHERE sheet_id=? GROUP BY parameter_id',(sheet_id,)).fetchall()
     ids={x['parameter_id']:x['id'] for x in latest}
     required={p['id'] for st in snap['steps'] for p in st['parameters'] if p['required']}
     if not required.issubset(ids):raise ValueError('Не все обязательные параметры заполнены')
     paper=c.execute('SELECT * FROM sheet_paper_register WHERE sheet_id=?',(sheet_id,)).fetchone()
     if not paper:raise ValueError('Зарегистрируйте бумажный оригинал')
     if sheet['status']!='review':raise ValueError('Сначала переведите лист на проверку')
     if not request.form.get('paper_checked'):raise ValueError('Подтвердите сверку с бумажным оригиналом')
     if c.execute("SELECT 1 FROM sheet_readings r WHERE r.sheet_id=? AND r.out_of_range=1 AND r.disposition!='accepted' AND NOT EXISTS(SELECT 1 FROM sheet_corrections x WHERE x.old_reading_id=r.id) LIMIT 1",(sheet_id,)).fetchone():raise ValueError('Есть неразрешённые отклонения')
     c.execute('UPDATE sheet_paper_register SET verified_at=?,verified_by=? WHERE sheet_id=?',(now(),session['uid'],sheet_id))
     c.execute("UPDATE production_sheets SET status='closed',reviewed_at=?,reviewed_by=? WHERE id=?",(now(),session['uid'],sheet_id))
     audit(c,'production_sheet.close','sheet='+str(sheet_id))
    else:raise ValueError('Неизвестное действие')
   flash('Операция сохранена')
  except (ValueError,KeyError,sqlite3.Error) as e:return fail(e)
  return redirect('/production/sheets/'+str(sheet_id))
 with conn() as c:
  sheet,snap,readings=sheet_data(c,sheet_id)
  paper=c.execute('SELECT * FROM sheet_paper_register WHERE sheet_id=?',(sheet_id,)).fetchone()
  corrections=c.execute('SELECT * FROM sheet_corrections WHERE sheet_id=? ORDER BY id DESC',(sheet_id,)).fetchall()
  attachments=c.execute('SELECT id,filename,sha256,uploaded_at FROM sheet_attachments WHERE sheet_id=? ORDER BY id',(sheet_id,)).fetchall()
 param_list=[p for st in snap['steps'] for p in st['parameters']]
 names={p['id']:p['name'] for p in param_list}
 latest_ids={r['parameter_id']:r['id'] for r in readings}
 complete=sum(1 for p in param_list if p['id'] in latest_ids and p['required'])
 required=sum(1 for p in param_list if p['required'])
 return page('Производственный лист ПЛ-'+str(sheet_id),"""
 <div class='card'><p><b>{{snap.product}}</b> · задание №{{snap.order_id}} · партия {{snap.lot_code}} · план {{snap.planned_qty}} · ТК {{snap.recipe_id}} ред. {{snap.recipe_version}}</p><p>Статус: <b>{{sheet.status}}</b> · <a href='/production/sheets/{{sheet.id}}/print' target='_blank'>Распечатать A4</a></p></div>
 {% if sheet.status!='closed' and has_role('admin','technologist','operator','quality') %}<div class='card'><h2>Внести фактическое измерение</h2><p class='muted'>При заполнении после смены укажите время с бумажной карты. Время внесения сервер сохраняет автоматически.</p><form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='reading'><label>Контрольная точка<select name='parameter_id'>{% for st in snap.steps %}{% for p in st.parameters %}<option value='{{p.id}}'>Этап {{st.position}} · {{p.name}} ({{p.unit}}), {{p.min_value if p.min_value is not none else '−∞'}}…{{p.max_value if p.max_value is not none else '+∞'}}</option>{% endfor %}{% endfor %}</select></label><label>Фактическое значение<input name='value' inputmode='decimal' required></label><label>Время измерения<input type='datetime-local' name='measured_at' required></label><label>Режим<select name='mode'><option value='after_shift'>После смены по бумаге</option><option value='live'>Во время производства</option></select></label><label>Примечание / причина отклонения<input name='comment'></label><button>Сохранить измерение</button></form></div>{% endif %}
 <div class='card'><h2>Заполнение карты после смены</h2><p>Обязательные точки: {{complete}} / {{required}}. Пустые поля не сохраняются. Повторный ввод создаёт новую запись в истории; для исправлений используйте отдельную форму ниже.</p>
 {% if sheet.status=='issued' and has_role('admin','technologist','operator','quality') %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='bulk_readings'><label>Режим<select name='mode'><option value='after_shift'>После смены, по бумаге</option><option value='live'>Во время производства</option></select></label>
 {% for st in snap.steps %}<h3>Этап {{st.position}} — {{st.name if st.name is defined else 'Операция'}}</h3>{% for p in st.parameters %}<fieldset><legend>{{p.name}} ({{p.unit}}) {% if p.required %}*{% endif %}</legend><small>Допуск: {{p.min_value if p.min_value is not none else '−∞'}} … {{p.max_value if p.max_value is not none else '+∞'}} · {% if p.id in latest_ids %}Есть запись{% else %}Не заполнено{% endif %}</small><label>Факт<input name='value_{{p.id}}' inputmode='decimal'></label><label>Время измерения<input name='measured_{{p.id}}' type='datetime-local'></label><label>Примечание / причина отклонения<input name='comment_{{p.id}}'></label></fieldset>{% endfor %}{% endfor %}<button>Сохранить заполненные значения</button></form>{% endif %}</div>
 <div class='card'><h2>Записи измерений (история без удаления)</h2><table><tr><th>Параметр</th><th>Факт</th><th>Измерено</th><th>Внесено</th><th>Исполнитель</th><th>Результат</th></tr>{% for r in readings %}<tr><td>{{names.get(r.parameter_id,r.parameter_id)}}</td><td>{{r.value}}</td><td>{{r.measured_at}}</td><td>{{r.entered_at}}</td><td>{{r.username}} · {{r.mode}}</td><td>{% if r.out_of_range %}<strong>Отклонение: {{r.disposition}}</strong><p>{{r.comment}}</p>{% if r.disposition=='pending' and sheet.status!='closed' and has_role('admin','technologist','quality') %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='disposition'><input type='hidden' name='reading_id' value='{{r.id}}'><select name='choice'><option value='accepted'>Принять с обоснованием</option><option value='rejected'>Отклонить</option></select><input name='reason' placeholder='Обоснование' required><button>Решение</button></form>{% else %}<p>{{r.disposition_reason}}</p>{% endif %}{% else %}В допуске{% endif %}</td></tr>{% else %}<tr><td colspan='6'>Измерений пока нет</td></tr>{% endfor %}</table></div>
 <div class='card'><h2>Бумажный оригинал</h2>
 {% if paper %}<p>Экземпляр: <b>{{paper.copy_number}}</b> · место хранения: {{paper.storage_location}} · получен: {{paper.received_at}}</p><p>{{paper.notes}}</p>
 {% elif sheet.status!='closed' and has_role('admin','technologist','quality') %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='paper'><label>Номер бумажного экземпляра<input name='copy_number' required></label><label>Место хранения оригинала<input name='storage_location' required></label><label>Примечание<input name='notes'></label><button>Зарегистрировать оригинал</button></form>
 {% else %}<p>Оригинал не зарегистрирован</p>{% endif %}</div>
 {% if sheet.status!='closed' and has_role('admin','technologist','operator','quality') %}<div class='card'><h2>Исправить последнее измерение</h2><p>Старая запись сохраняется. Исправление требует указания причины и создаёт новую запись.</p><form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='correct'><label>Запись<select name='old_reading_id'>{% for r in readings %}<option value='{{r.id}}'>№{{r.id}} · {{names.get(r.parameter_id,r.parameter_id)}} · {{r.value}}</option>{% endfor %}</select></label><label>Новое значение<input name='value' required inputmode='decimal'></label><label>Фактическое время измерения<input name='measured_at' type='datetime-local' required></label><label>Причина исправления<input name='reason' required></label><button>Сохранить исправление</button></form></div>{% endif %}
 <div class='card'><h2>История исправлений</h2>{% for x in corrections %}<p>Запись №{{x.old_reading_id}} → №{{x.new_reading_id}} · {{x.created_at}} · {{x.reason}}</p>{% else %}<p>Исправлений нет</p>{% endfor %}</div>
 <div class='card'><h2>Скан бумажного оригинала</h2><p>PDF, JPG или PNG до 5 МБ. Не более 10 файлов. Файлы хранятся в базе данных и доступны только после входа.</p>{% for a in attachments %}<p><a href='/production/sheets/{{sheet.id}}/attachments/{{a.id}}'>{{a.filename}}</a> · {{a.uploaded_at}} · SHA256 {{a.sha256[:12]}}…</p>{% else %}<p>Файлы не приложены</p>{% endfor %}
 {% if paper and sheet.status!='closed' and has_role('admin','technologist','quality') %}<form method='post' enctype='multipart/form-data'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='upload_paper'><input type='file' name='document' accept='.pdf,.jpg,.jpeg,.png' required><button>Приложить скан</button></form>{% endif %}</div>
 {% if sheet.status=='issued' and paper and has_role('admin','technologist','quality') %}<div class='card'><form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='review'><button>Передать на проверку</button></form></div>{% endif %}
 {% if sheet.status!='closed' and has_role('admin','technologist') %}<div class='card'><h2>Закрытие листа</h2><form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='close'><label><input type='checkbox' name='paper_checked' required> Бумажный оригинал сверен с электронными записями</label><button>Проверить и закрыть</button></form></div>{% endif %}
 <div class='card'><p><b>Важно:</b> закрытие этого листа не проводит списание сырья и не закрывает производственное задание. Для исправления значения создаётся новая запись, исходная остаётся в журнале.</p></div>""",sheet=sheet,snap=snap,readings=readings,names=names,paper=paper,corrections=corrections,attachments=attachments,latest_ids=latest_ids,complete=complete,required=required)

@app.get('/production/sheets/<int:sheet_id>/attachments/<int:attachment_id>')
@login_required
def production_sheet_attachment_v19(sheet_id,attachment_id):
 with conn() as c:
  r=c.execute('SELECT filename,mime,payload FROM sheet_attachments WHERE id=? AND sheet_id=?',(attachment_id,sheet_id)).fetchone()
 if not r:abort(404)
 response=send_file(io.BytesIO(r['payload']),mimetype=r['mime'],as_attachment=True,download_name=r['filename'])
 response.headers['Cache-Control']='private, no-store'
 response.headers['X-Content-Type-Options']='nosniff'
 return response

@app.get('/production/sheets/<int:sheet_id>/print')
@login_required
def production_sheet_print_v17(sheet_id):
 # v41: blank, paper-first production form; immutable snapshot of the issued sheet.
 if not has_role('admin','technologist','operator','quality'):
  abort(403)
 with conn() as c:
  sheet,snap,readings=sheet_data(c,sheet_id)
 qr_buf=io.BytesIO()
 qrcode.make('CHEMPRO:SHEET:'+str(sheet_id),image_factory=SvgPathImage).save(qr_buf)
 qr_data=base64.b64encode(qr_buf.getvalue()).decode('ascii')
 template_path=os.path.join(os.path.dirname(__file__),'production_sheet_print_v41.html')
 with open(template_path,encoding='utf-8') as fh:
  template=fh.read()
 response=app.make_response(render_template_string(template,sheet=sheet,snap=snap,qr_data=qr_data))
 response.headers['Cache-Control']='private, no-store'
 response.headers['X-Content-Type-Options']='nosniff'
 return response

@app.get('/tsd/production')
@login_required
def tsd_production_v17():
 with conn() as c:rows=c.execute("SELECT s.id,s.status,w.lot_code FROM production_sheets s JOIN work_orders w ON w.id=s.order_id WHERE s.status!='closed' ORDER BY s.id DESC LIMIT 50").fetchall()
 return page('ТСД CT48 · Производственные листы',"""<div class='card'><p>Сканер ScanWedge работает как клавиатура. Считайте код задания вида <code>CHEMPRO:SHEET:17</code> или введите номер вручную.</p><label>Код листа<input id='scan' autocomplete='off' autofocus placeholder='CHEMPRO:SHEET:17'></label><button type='button' onclick='goSheet()'>Открыть</button><p id='error' class='danger'></p></div><div class='card'><h2>Активные листы</h2>{% for s in rows %}<p><a href='/production/sheets/{{s.id}}'>ПЛ-{{s.id}} · {{s.lot_code}} · {{s.status}}</a></p>{% endfor %}</div><script>function goSheet(){let v=document.getElementById('scan').value.trim();let m=v.match(/^(?:CHEMPRO:SHEET:)?([0-9]+)$/i);if(m){location.href='/production/sheets/'+encodeURIComponent(m[1]);}else{document.getElementById('error').textContent='Неверный код листа';}}document.getElementById('scan').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();goSheet();}});</script>""",rows=rows)


# v21: shift planning and execution tracking. No automatic inventory postings.
def shift_plan_rows(c,day):
 rows=c.execute("""SELECT a.*,w.lot_code,w.status AS order_status,w.planned_qty,
  u.username AS operator_name,p.name AS product_name,
  s.id AS sheet_id,s.status AS sheet_status
  FROM shift_assignments a JOIN work_orders w ON w.id=a.order_id
  JOIN recipes r ON r.id=w.recipe_id JOIN products p ON p.id=r.product_id
  JOIN users u ON u.id=a.operator_id
  LEFT JOIN production_sheets s ON s.order_id=w.id
  WHERE a.shift_date=? ORDER BY a.planned_start,a.id""",(day,)).fetchall()
 current=datetime.now()
 result=[]
 for r in rows:
  x=dict(r)
  try: end=datetime.fromisoformat(x['planned_end'])
  except ValueError:end=current
  x['delayed']=bool(x['actual_end'] is None and current>end)
  x['state']='Завершено' if x['actual_end'] else 'В работе' if x['actual_start'] else 'Запланировано'
  result.append(x)
 return result

@app.route('/production/shift-plan',methods=['GET','POST'])
@login_required
def shift_plan_v21():
 if not has_role('admin','technologist','quality','operator','viewer'):abort(403)
 if request.method=='POST':
  if not has_role('admin','technologist'):abort(403)
  action=request.form.get('action','')
  try:
   with conn() as c:
    if action=='assign':
     oid=int(request.form['order_id']);uid=int(request.form['operator_id'])
     shift_day=date.fromisoformat(request.form['shift_date']).isoformat()
     shift_name=request.form.get('shift_name','Дневная').strip()[:40] or 'Дневная'
     start=datetime.fromisoformat(request.form['planned_start']);end=datetime.fromisoformat(request.form['planned_end'])
     if start.date().isoformat()!=shift_day or end<=start or (end-start)>timedelta(hours=24):raise ValueError('Проверьте дату и плановый интервал (не более 24 часов)')
     order=c.execute("SELECT status FROM work_orders WHERE id=?",(oid,)).fetchone()
     operator=c.execute("SELECT role FROM users WHERE id=?",(uid,)).fetchone()
     if not order or order['status']!='approved':raise ValueError('Можно планировать только утверждённое задание')
     if not operator or not c.execute("SELECT 1 FROM user_roles_v33 WHERE user_id=? AND role IN ('operator','admin','technologist')",(operator['id'],)).fetchone():raise ValueError('Выберите действующего оператора')
     cur=c.execute("""INSERT INTO shift_assignments(order_id,shift_date,shift_name,operator_id,planned_start,planned_end,created_at,created_by)
       VALUES(?,?,?,?,?,?,?,?)""",(oid,shift_day,shift_name,uid,start.isoformat(timespec='minutes'),end.isoformat(timespec='minutes'),now(),session['uid']))
     c.execute('INSERT INTO shift_events(assignment_id,event,occurred_at,actor_id,details) VALUES(?,?,?,?,?)',(cur.lastrowid,'plan',now(),session['uid'],'Планирование задания'))
     audit(c,'shift.assign','order='+str(oid)+' operator='+str(uid))
    elif action in ('start','finish'):
     aid=int(request.form['assignment_id'])
     r=c.execute('SELECT a.*,w.status AS order_status FROM shift_assignments a JOIN work_orders w ON w.id=a.order_id WHERE a.id=?',(aid,)).fetchone()
     if not r:abort(404)
     if r['order_status'] in ('cancelled','draft'):raise ValueError('Задание неактивно')
     if action=='start':
      if r['actual_start'] or r['actual_end']:raise ValueError('Начало уже отмечено')
      c.execute('UPDATE shift_assignments SET actual_start=? WHERE id=?',(now(),aid))
     else:
      if not r['actual_start'] or r['actual_end']:raise ValueError('Сначала отметьте начало, повторное завершение запрещено')
      c.execute('UPDATE shift_assignments SET actual_end=? WHERE id=?',(now(),aid))
     c.execute('INSERT INTO shift_events(assignment_id,event,occurred_at,actor_id,details) VALUES(?,?,?,?,?)',(aid,action,now(),session['uid'],'Отметка технолога'))
     audit(c,'shift.'+action,'assignment='+str(aid))
    else:abort(400)
   flash('Операция сохранена')
  except (ValueError,sqlite3.IntegrityError,KeyError) as e:flash('Не удалось сохранить: '+str(e))
  return redirect('/production/shift-plan?date='+request.form.get('shift_date',date.today().isoformat()))
 day=request.args.get('date',date.today().isoformat())
 try:day=date.fromisoformat(day).isoformat()
 except ValueError:day=date.today().isoformat()
 with conn() as c:
  rows=shift_plan_rows(c,day)
  orders=c.execute("""SELECT w.id,w.lot_code,p.name AS product FROM work_orders w
   JOIN recipes r ON r.id=w.recipe_id JOIN products p ON p.id=r.product_id
   LEFT JOIN shift_assignments a ON a.order_id=w.id
   WHERE w.status='approved' AND a.id IS NULL ORDER BY w.due_date,w.id LIMIT 200""").fetchall()
  operators=c.execute("SELECT id,username FROM users WHERE role IN ('operator','technologist','admin') ORDER BY username").fetchall()
  counts={'planned':sum(not r['actual_start'] for r in rows),'running':sum(bool(r['actual_start'] and not r['actual_end']) for r in rows),'finished':sum(bool(r['actual_end']) for r in rows),'late':sum(r['delayed'] for r in rows)}
 return page('План смены · технолог',"""
 <div class='card'><form method='get' action='/production/shift-plan'><label>Дата смены<input type='date' name='date' value='{{day}}' required></label><button>Показать</button></form>
 <p>Запланировано: <b>{{counts.planned}}</b> · В работе: <b>{{counts.running}}</b> · Завершено: <b>{{counts.finished}}</b> · Просрочено по плану: <b>{{counts.late}}</b></p>
 <p class='muted'>Фактическое начало/окончание отмечается технологом по времени сохранения на сервере. Эти отметки не заменяют измерения в ТК и не проводят складские операции.</p></div>
 {% if has_role('admin','technologist') %}<div class='card'><h2>Назначить задание на смену</h2><form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='assign'>
 <label>Задание<select name='order_id' required>{% for o in orders %}<option value='{{o.id}}'>№{{o.id}} · {{o.lot_code}} · {{o.product}}</option>{% endfor %}</select></label>
 <label>Оператор<select name='operator_id' required>{% for u in operators %}<option value='{{u.id}}'>{{u.username}}</option>{% endfor %}</select></label>
 <label>Дата<input type='date' name='shift_date' value='{{day}}' required></label><label>Смена<input name='shift_name' value='Дневная' maxlength='40'></label>
 <label>План начала<input type='datetime-local' name='planned_start' required></label><label>План окончания<input type='datetime-local' name='planned_end' required></label>
 <button {% if not orders or not operators %}disabled{% endif %}>Назначить</button></form></div>{% endif %}
 <div class='card'><h2>Задания на {{day}}</h2><div style='overflow-x:auto'><table><tr><th>Задание / продукция</th><th>Оператор</th><th>План</th><th>Факт</th><th>Состояние</th><th>Действия</th></tr>
 {% for r in rows %}<tr><td>№{{r.order_id}} · {{r.lot_code}}<br>{{r.product_name}} · {{r.planned_qty}}<br>{% if r.sheet_id %}<a href='/production/sheets/{{r.sheet_id}}'>ПЛ-{{r.sheet_id}}</a>{% endif %}</td>
 <td>{{r.operator_name}}<br>{{r.shift_name}}</td><td>{{r.planned_start}}<br>{{r.planned_end}}</td>
 <td>Начало: {{r.actual_start or '—'}}<br>Окончание: {{r.actual_end or '—'}}</td>
 <td>{{r.state}}{% if r.delayed %}<br><strong class='danger'>Задержка относительно плана</strong>{% endif %}</td><td>
 {% if has_role('admin','technologist') %}{% if not r.actual_start %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='shift_date' value='{{day}}'><input type='hidden' name='assignment_id' value='{{r.id}}'><button name='action' value='start'>Начало</button></form>
 {% elif not r.actual_end %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='shift_date' value='{{day}}'><input type='hidden' name='assignment_id' value='{{r.id}}'><button name='action' value='finish'>Окончание</button></form>{% endif %}{% endif %}</td></tr>
 {% else %}<tr><td colspan='6'>На эту дату задания не назначены</td></tr>{% endfor %}</table></div></div>
 """,day=day,rows=rows,counts=counts,orders=orders,operators=operators)

@app.get('/tsd/shift-plan')
@login_required
def tsd_shift_plan_v21():
 if not has_role('admin','technologist','operator'):abort(403)
 day=date.today().isoformat()
 with conn() as c:rows=shift_plan_rows(c,day)
 if is_only_operator():rows=[r for r in rows if r['operator_id']==session['uid']]
 return page('ТСД CT48 · Моя смена',"""<div class='card'><h2>Задания на {{day}}</h2><p>Откройте производственный лист, чтобы внести измерения после смены.</p>
 {% for r in rows %}<div class='card'><b>№{{r.order_id}} · {{r.product_name}}</b><p>Партия: {{r.lot_code}} · {{r.state}}</p><p>План: {{r.planned_start}} — {{r.planned_end}}</p>
 {% if r.sheet_id %}<a class='btn' href='/production/sheets/{{r.sheet_id}}'>Открыть ПЛ-{{r.sheet_id}}</a>{% else %}<p>Производственный лист пока не выпущен технологом.</p>{% endif %}</div>
 {% else %}<p>На сегодня назначений нет.</p>{% endfor %}</div>""",day=day,rows=rows)


# v22: detailed stage schedule. Actual timestamps are server-side event timestamps.
def v22_stage_rows(c, assignment_id):
 rows=c.execute("SELECT * FROM shift_stage_plans WHERE assignment_id=? ORDER BY step_position",(assignment_id,)).fetchall()
 current=datetime.now()
 result=[]
 for row in rows:
  item=dict(row)
  item['state']='Завершён' if item['actual_end'] else 'В работе' if item['actual_start'] else 'Ожидает'
  item['late']=not item['actual_end'] and current>datetime.fromisoformat(item['planned_end'])
  result.append(item)
 return result

@app.route('/production/stages',methods=['GET','POST'])
@login_required
def production_stages_v22():
 if not has_role('admin','technologist','operator','quality','viewer'):abort(403)
 assignment_id=request.values.get('assignment_id','')
 if request.method=='POST':
  if not has_role('admin','technologist'):abort(403)
  try:
   aid=int(assignment_id)
   with conn() as c:
    a=c.execute('SELECT * FROM shift_assignments WHERE id=?',(aid,)).fetchone()
    if not a:abort(404)
    if a['actual_end']:raise ValueError('Смена уже завершена')
    sheet=c.execute('SELECT snapshot_json FROM production_sheets WHERE order_id=?',(a['order_id'],)).fetchone()
    if not sheet:raise ValueError('Сначала выпустите производственный лист')
    steps=json.loads(sheet['snapshot_json']).get('steps',[])
    if not steps:raise ValueError('В технологической карте нет этапов')
    if c.execute('SELECT 1 FROM shift_stage_plans WHERE assignment_id=?',(aid,)).fetchone():raise ValueError('План этапов уже создан')
    start=datetime.fromisoformat(a['planned_start']);end=datetime.fromisoformat(a['planned_end'])
    span=(end-start)/len(steps)
    for i,step in enumerate(steps):
     position=int(step['position'])
     st=(start+span*i).isoformat(timespec='minutes');en=(end if i==len(steps)-1 else start+span*(i+1)).isoformat(timespec='minutes')
     cur=c.execute('INSERT INTO shift_stage_plans(assignment_id,step_position,step_instruction,planned_start,planned_end,created_at,created_by) VALUES(?,?,?,?,?,?,?)',(aid,position,str(step['instruction'])[:1000],st,en,now(),session['uid']))
     c.execute('INSERT INTO shift_stage_events(stage_id,event,happened_at,actor_id,note) VALUES(?,?,?,?,?)',(cur.lastrowid,'planned',now(),session['uid'],'Равномерное предварительное распределение времени'))
    audit(c,'stages.plan','assignment='+str(aid))
   flash('Черновой план этапов создан. Время распределено равномерно и требует проверки технологом.')
  except (ValueError,KeyError,sqlite3.IntegrityError) as e:flash('Ошибка: '+str(e))
  return redirect('/production/stages?assignment_id='+str(assignment_id))
 with conn() as c:
  assignments=c.execute('SELECT a.id,a.shift_date,a.order_id,w.lot_code,u.username FROM shift_assignments a JOIN work_orders w ON w.id=a.order_id JOIN users u ON u.id=a.operator_id ORDER BY a.shift_date DESC,a.id DESC LIMIT 150').fetchall()
  selected=None;stages=[]
  if assignment_id:
   try:aid=int(assignment_id)
   except ValueError:abort(400)
   selected=c.execute('SELECT * FROM shift_assignments WHERE id=?',(aid,)).fetchone()
   if not selected:abort(404)
   stages=v22_stage_rows(c,aid)
 return page('Планирование этапов ТК',"""<div class='card'><h2>Этапы производственной смены</h2><p>Выберите назначение. Время первоначально распределяется равномерно — технолог должен проверить его по реальной ТК. Данные не заменяют нормативы и измерения.</p><form method='get'><label>Назначение<select name='assignment_id' required><option value=''>Выберите задание</option>{% for a in assignments %}<option value='{{a.id}}' {% if selected and selected.id==a.id %}selected{% endif %}>{{a.shift_date}} · ПЗ-{{a.order_id}} · {{a.lot_code}} · {{a.username}}</option>{% endfor %}</select></label><button>Показать</button></form></div>
 {% if selected %}<div class='card'><h2>Назначение №{{selected.id}}</h2>{% if not stages and has_role('admin','technologist') %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='assignment_id' value='{{selected.id}}'><button>Создать предварительный план по этапам ТК</button></form>{% endif %}
 <table><tr><th>Этап</th><th>План</th><th>Факт</th><th>Состояние</th></tr>{% for st in stages %}<tr><td>{{st.step_position}}. {{st.step_instruction}}</td><td>{{st.planned_start}} — {{st.planned_end}}</td><td>{{st.actual_start or '—'}} — {{st.actual_end or '—'}}</td><td>{{st.state}}{% if st.late %} · задержка{% endif %}</td></tr>{% else %}<tr><td colspan='4'>Этапы ещё не запланированы</td></tr>{% endfor %}</table><p><a href='/tsd/stages?assignment_id={{selected.id}}'>Открыть на ТСД</a></p></div>{% endif %}""",assignments=assignments,selected=selected,stages=stages)

@app.route('/tsd/stages',methods=['GET','POST'])
@login_required
def tsd_stages_v22():
 if not has_role('admin','technologist','operator'):abort(403)
 if request.method=='POST':
  try:
   stage_id=int(request.form['stage_id']);action=request.form.get('action')
   if action not in ('start','finish'):abort(400)
   with conn() as c:
    stage=c.execute('SELECT p.*,a.operator_id,a.actual_end AS assignment_end FROM shift_stage_plans p JOIN shift_assignments a ON a.id=p.assignment_id WHERE p.id=?',(stage_id,)).fetchone()
    if not stage:abort(404)
    if is_only_operator() and stage['operator_id']!=session['uid']:abort(403)
    if stage['assignment_end']:raise ValueError('Смена завершена')
    if action=='start':
     if stage['actual_start'] or stage['actual_end']:raise ValueError('Этап уже начат')
     updated=c.execute('UPDATE shift_stage_plans SET actual_start=? WHERE id=? AND actual_start IS NULL AND actual_end IS NULL',(now(),stage_id)).rowcount
    else:
     if not stage['actual_start'] or stage['actual_end']:raise ValueError('Этап ещё не начат или уже завершён')
     updated=c.execute('UPDATE shift_stage_plans SET actual_end=? WHERE id=? AND actual_start IS NOT NULL AND actual_end IS NULL',(now(),stage_id)).rowcount
    if updated!=1:raise ValueError('Повторное проведение запрещено')
    c.execute('INSERT INTO shift_stage_events(stage_id,event,happened_at,actor_id,note) VALUES(?,?,?,?,?)',(stage_id,'started' if action=='start' else 'finished',now(),session['uid'],'Отметка этапа на ТСД/ПК'))
    audit(c,'stage.'+action,'stage='+str(stage_id))
   flash('Время этапа зафиксировано')
  except (ValueError,KeyError,sqlite3.IntegrityError) as e:flash('Ошибка: '+str(e))
  return redirect('/tsd/stages?assignment_id='+request.form.get('assignment_id',''))
 assignment_id=request.args.get('assignment_id','')
 with conn() as c:
  sql='SELECT a.id,a.order_id,w.lot_code,a.shift_date FROM shift_assignments a JOIN work_orders w ON w.id=a.order_id WHERE 1=1'
  args=[]
  if is_only_operator():sql+=' AND a.operator_id=?';args.append(session['uid'])
  assignments=c.execute(sql+' ORDER BY a.shift_date DESC,a.id DESC LIMIT 100',args).fetchall()
  selected=None;stages=[]
  if assignment_id:
   try:aid=int(assignment_id)
   except ValueError:abort(400)
   selected=c.execute('SELECT * FROM shift_assignments WHERE id=?',(aid,)).fetchone()
   if not selected:abort(404)
   if is_only_operator() and selected['operator_id']!=session['uid']:abort(403)
   stages=v22_stage_rows(c,aid)
 return page('ТСД CT48 · Этапы смены',"""<div class='card'><h2>Мои технологические этапы</h2><form method='get'><label>Задание<select name='assignment_id' required><option value=''>Выберите задание</option>{% for a in assignments %}<option value='{{a.id}}' {% if selected and selected.id==a.id %}selected{% endif %}>{{a.shift_date}} · ПЗ-{{a.order_id}} · {{a.lot_code}}</option>{% endfor %}</select></label><button>Открыть</button></form></div>
 {% if selected %}{% for st in stages %}<div class='card'><h3>{{st.step_position}}. {{st.step_instruction}}</h3><p>План: {{st.planned_start}} — {{st.planned_end}}</p><p><b>{{st.state}}</b>{% if st.late %} · задержка{% endif %}</p><p>Факт: {{st.actual_start or '—'}} — {{st.actual_end or '—'}}</p>{% if not selected.actual_end %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='assignment_id' value='{{selected.id}}'><input type='hidden' name='stage_id' value='{{st.id}}'>{% if not st.actual_start %}<button name='action' value='start'>Начать этап</button>{% elif not st.actual_end %}<button name='action' value='finish'>Завершить этап</button>{% endif %}</form>{% endif %}</div>{% else %}<div class='card'>Технолог ещё не составил план этапов.</div>{% endfor %}{% endif %}<div class='card'><p>Отметки времени не заменяют ввод температуры, pH, плотности и других параметров. Для измерений используйте производственный лист.</p></div>""",assignments=assignments,selected=selected,stages=stages)


# v23: factual loading journal. No stock mutation; production posting remains sole stock write.
def v23_parse_measured(value):
 try: result=datetime.fromisoformat(value)
 except (ValueError,TypeError):raise ValueError('Укажите корректные дату и время взвешивания')
 if result>datetime.now()+timedelta(minutes=5):raise ValueError('Время взвешивания в будущем')
 return result.isoformat(timespec='minutes')

@app.route('/tsd/load',methods=['GET','POST'])
@allowed('admin','operator','technologist')
def tsd_load_v23():
 if request.method=='POST':
  try:
   key=request.form.get('request_key','')
   if len(key)!=32 or any(ch not in '0123456789abcdef' for ch in key):raise ValueError('Некорректный идентификатор операции')
   order_id=int(request.form['order_id'])
   code=parse_container_scan(request.form.get('container_code',''))
   material_id=int(request.form['material_id'])
   qty=number(request.form,'qty')
   measured=v23_parse_measured(request.form.get('measured_at',''))
   mode=request.form.get('entry_mode','')
   if mode not in ('live','after_shift'):raise ValueError('Укажите режим записи')
   note=request.form.get('note','').strip()[:1000]
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    if c.execute('SELECT 1 FROM material_loads_v23 WHERE request_key=?',(key,)).fetchone():raise ValueError('Эта запись уже сохранена')
    order=c.execute("SELECT * FROM work_orders WHERE id=? AND status='approved'",(order_id,)).fetchone()
    if not order:raise ValueError('Нет утверждённого задания')
    sheet=c.execute("SELECT * FROM production_sheets WHERE order_id=? AND status!='closed'",(order_id,)).fetchone()
    if not sheet:raise ValueError('Для задания нужен открытый производственный лист')
    snap=json.loads(sheet['snapshot_json'])
    # Immutable sheet snapshot is authoritative for planned composition, including legacy sheets.
    recipe_items=c.execute('SELECT material_id,percent FROM recipe_items WHERE recipe_id=?',(snap['recipe_id'],)).fetchall()
    recipe_item=next((r for r in recipe_items if r['material_id']==material_id),None)
    if not recipe_item:raise ValueError('Материал отсутствует в рецептуре')
    snap_item=next((x for x in snap['items'] if x.get('material_id')==material_id),None)
    if snap_item is None:
     # For pre-v23 sheets resolve by unique material name, from the frozen snapshot.
     material=c.execute('SELECT name FROM materials WHERE id=?',(material_id,)).fetchone()
     snap_item=next((x for x in snap['items'] if x['name']==material['name']),None)
    if not snap_item:raise ValueError('Материал отсутствует в выданной карте')
    norm=float(snap_item['planned'])
    if not math.isfinite(norm) or norm<=0:raise ValueError('Некорректный норматив')
    ct=c.execute("SELECT ct.*,l.material_id,l.status AS qc,l.expires,l.kind FROM containers ct JOIN lots l ON l.id=ct.lot_id WHERE ct.code=?",(code,)).fetchone()
    if not ct or not ct['active'] or ct['kind']!='raw':raise ValueError('Активная тара сырья не найдена')
    if ct['material_id']!=material_id:raise ValueError('ВНИМАНИЕ: в таре другой компонент!')
    if ct['qc']!='released' or ct['expires']<date.today().isoformat():raise ValueError('Сырьё не разрешено ОТК или просрочено')
    picked=c.execute("SELECT COALESCE(SUM(qty),0) FROM tsd_picks WHERE order_id=? AND container_id=? AND status='issued'",(order_id,ct['id'])).fetchone()[0]
    if picked<=0:raise ValueError('Тара не выдана по этому заданию')
    loaded_ct=c.execute('SELECT COALESCE(SUM(qty),0) FROM material_loads_v23 WHERE order_id=? AND container_id=?',(order_id,ct['id'])).fetchone()[0]
    if qty>picked-loaded_ct+1e-8:raise ValueError('Превышено выданное количество из этой тары')
    loaded_mat=c.execute('SELECT COALESCE(SUM(qty),0) FROM material_loads_v23 WHERE order_id=? AND material_id=?',(order_id,material_id)).fetchone()[0]
    tolerance=0.0  # Strict by default: no undocumented process tolerance.
    if loaded_mat+qty>norm+1e-8:raise ValueError('Загрузка превысит норматив ТК. Требуется отдельное согласование технолога')
    c.execute("INSERT INTO material_loads_v23(request_key,order_id,sheet_id,container_id,material_id,qty,norm_qty,tolerance_percent,measured_at,entered_at,entered_by,entry_mode,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(key,order_id,sheet['id'],ct['id'],material_id,qty,norm,tolerance,measured,now(),session['uid'],mode,note))
    audit(c,'production.material_load',f'order={order_id}; container={ct["id"]}; material={material_id}; qty={qty}; mode={mode}')
   flash('Фактическая загрузка записана. Складские остатки не списаны повторно.')
   return redirect(url_for('tsd_load_v23',order_id=order_id))
  except (ValueError,KeyError,sqlite3.Error) as e:flash('Ошибка: '+str(e))
 with conn() as c:
  orders=c.execute("SELECT w.id,w.lot_code,w.recipe_id,w.planned_qty FROM work_orders w JOIN production_sheets s ON s.order_id=w.id WHERE w.status='approved' AND s.status!='closed' ORDER BY w.id DESC LIMIT 100").fetchall()
  oid=request.args.get('order_id',type=int)
  if oid not in [x['id'] for x in orders]:oid=orders[0]['id'] if orders else None
  materials=c.execute("SELECT DISTINCT m.id,m.name,m.unit FROM recipe_items ri JOIN materials m ON m.id=ri.material_id JOIN work_orders w ON w.recipe_id=ri.recipe_id WHERE w.id=? ORDER BY m.name",(oid,)).fetchall() if oid else []
  history=c.execute("SELECT ml.*,ct.code,m.name AS material,u.username FROM material_loads_v23 ml JOIN containers ct ON ct.id=ml.container_id JOIN materials m ON m.id=ml.material_id JOIN users u ON u.id=ml.entered_by WHERE ml.order_id=? ORDER BY ml.id DESC LIMIT 50",(oid,)).fetchall() if oid else []
  norms={}
  if oid:
   sheet=c.execute('SELECT snapshot_json FROM production_sheets WHERE order_id=?',(oid,)).fetchone()
   snap=json.loads(sheet['snapshot_json'])
   norms={x['name']:x['planned'] for x in snap['items']}
   totals=c.execute("SELECT material_id,SUM(qty) AS qty FROM material_loads_v23 WHERE order_id=? GROUP BY material_id",(oid,)).fetchall()
   totals={x['material_id']:x['qty'] for x in totals}
  else:totals={}
 return page('ТСД · Фактическая загрузка сырья',"""
 <style>.ct48{max-width:520px;margin:auto}.ct48 input,.ct48 select,.ct48 button,.ct48 textarea{font-size:18px;min-height:50px;width:100%}.ct48 label{display:block}</style>
 <div class='card ct48'><h2>Загрузка сырья по ТК</h2>
 <p>Сканер Urovo CT48: ScanWedge → клавиатурный ввод → Enter. Сканирование только выбирает тару; запись выполняется после подтверждения.</p>
 <form method='get'><label>Производственное задание<select name='order_id' onchange='this.form.submit()'>{% for o in orders %}<option value='{{o.id}}' {% if o.id==oid %}selected{% endif %}>№{{o.id}} · {{o.lot_code}}</option>{% endfor %}</select></label></form>
 {% if oid %}<form method='post' id='load-form'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='request_key' value='{{key}}'><input type='hidden' name='order_id' value='{{oid}}'>
 <label>Компонент из ТК<select name='material_id' required>{% for m in materials %}<option value='{{m.id}}'>{{m.name}} · {{m.unit}} · норма {{norms.get(m.name,'?')}} · учтено {{totals.get(m.id,0)}}</option>{% endfor %}</select></label>
 <label>QR тары<input id='load-qr' name='container_code' autocomplete='off' maxlength='120' required placeholder='CHEMPRO:CONTAINER:...'></label>
 <label>Фактический вес (единица материала)<input type='number' name='qty' min='0.000001' step='any' required></label>
 <label>Время фактического взвешивания<input type='datetime-local' name='measured_at' value='{{measured_default}}' required></label>
 <label>Способ записи<select name='entry_mode'><option value='live'>Во время производства</option><option value='after_shift'>После смены с бумажной ТК</option></select></label>
 <label>Комментарий<textarea name='note' maxlength='1000' rows='2'></textarea></label>
 <button type='submit'>Подтвердить загрузку</button></form>
 <p class='muted'>Строгий контроль: материал должен совпадать с рецептурой, тара — быть выданной на задание, партия — разрешённой ОТК. Превышение норматива блокируется. Записи не списывают остатки.</p>
 {% else %}<p>Нет открытых листов утверждённых заданий.</p>{% endif %}</div>
 <div class='card'><h2>Журнал фактической загрузки</h2><table><tr><th>Материал</th><th>Тара</th><th>Вес</th><th>Измерено</th><th>Внесено</th><th>Оператор</th></tr>{% for x in history %}<tr><td>{{x.material}}</td><td>{{x.code}}</td><td>{{x.qty}}</td><td>{{x.measured_at}}</td><td>{{x.entered_at}}</td><td>{{x.username}}</td></tr>{% else %}<tr><td colspan='6'>Записей пока нет</td></tr>{% endfor %}</table></div>
 <script>(function(){const f=document.getElementById('load-form');if(!f)return;const q=document.getElementById('load-qr');q.focus();q.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();f.querySelector('[name=qty]').focus()}});f.addEventListener('submit',e=>{if(f.dataset.sent){e.preventDefault();return}if(!confirm('Записать фактическую загрузку?')){e.preventDefault();return}f.dataset.sent='1';f.querySelector('button').disabled=true})})();</script>
 """,orders=orders,oid=oid,materials=materials,norms=norms,totals=totals,history=history,key=uuid.uuid4().hex,measured_default=datetime.now().strftime('%Y-%m-%dT%H:%M'))


# v24: read-only reconciliation; never posts stock or changes QC statuses.
def v24_reconcile_order(c, order_id):
 order=c.execute("SELECT id,lot_code,status,recipe_id,planned_qty FROM work_orders WHERE id=?",(order_id,)).fetchone()
 if not order:raise ValueError('Производственное задание не найдено')
 sheet=c.execute('SELECT id,snapshot_json,status FROM production_sheets WHERE order_id=? ORDER BY id DESC LIMIT 1',(order_id,)).fetchone()
 if not sheet:raise ValueError('Производственный лист не выпущен')
 snapshot=json.loads(sheet['snapshot_json'])
 materials=c.execute('SELECT id,name,unit FROM materials').fetchall()
 by_name={}
 for m in materials:by_name.setdefault(m['name'],[]).append(m)
 loaded={r['material_id']:float(r['qty']) for r in c.execute('SELECT material_id,SUM(qty) qty FROM material_loads_v23 WHERE order_id=? GROUP BY material_id',(order_id,))}
 # 'issued' and 'consumed' are historical issue records; 'staged' is NOT issued.
 issued={r['material_id']:float(r['qty']) for r in c.execute("""SELECT l.material_id,SUM(p.qty) qty FROM tsd_picks p JOIN containers ct ON ct.id=p.container_id JOIN lots l ON l.id=ct.lot_id WHERE p.order_id=? AND p.status IN ('issued','consumed') AND l.kind='raw' GROUP BY l.material_id""",(order_id,))}
 rows=[]; seen=set()
 for item in snapshot.get('items',[]):
  mid=item.get('material_id')
  if mid is None:
   matches=by_name.get(item.get('name'),[])
   mid=matches[0]['id'] if len(matches)==1 else None
  name=item.get('name','Неизвестный материал')
  try:norm=float(item['planned'])
  except (KeyError,TypeError,ValueError):norm=0
  actual=loaded.get(mid,0) if mid is not None else 0
  issued_qty=issued.get(mid,0) if mid is not None else 0
  seen.add(mid)
  issues=[]
  if mid is None:issues.append('Материал не сопоставлен со справочником')
  if norm<=0:issues.append('Неверный норматив')
  if actual<=0:issues.append('Нет записей фактической загрузки')
  if actual>norm+1e-7:issues.append('Загрузка выше норматива')
  if actual>issued_qty+1e-7:issues.append('Загружено больше выданного')
  if issued_qty>actual+1e-7:issues.append('Выдано больше зарегистрированной загрузки')
  if norm>actual+1e-7:issues.append('Загрузка ниже норматива')
  rows.append(dict(name=name,unit=next((m['unit'] for m in materials if m['id']==mid),''),norm=norm,issued=issued_qty,actual=actual,delta_norm=round(actual-norm,6),delta_issued=round(actual-issued_qty,6),issues=issues))
 for mid in sorted((set(loaded)|set(issued))-seen):
  m=next((x for x in materials if x['id']==mid),None)
  rows.append(dict(name=m['name'] if m else f'Материал #{mid}',unit=m['unit'] if m else '',norm=0,issued=issued.get(mid,0),actual=loaded.get(mid,0),delta_norm=loaded.get(mid,0),delta_issued=round(loaded.get(mid,0)-issued.get(mid,0),6),issues=['Материал отсутствует в снимке ТК']))
 return dict(order=order,sheet=sheet,rows=rows,issue_count=sum(len(r['issues']) for r in rows))

@app.get('/production/load-reconciliation')
@allowed('admin','technologist','quality','warehouse','viewer')
def v24_load_reconciliation():
 with conn() as c:
  orders=c.execute("SELECT w.id,w.lot_code,w.status FROM work_orders w JOIN production_sheets s ON s.order_id=w.id ORDER BY w.id DESC LIMIT 200").fetchall()
  oid=request.args.get('order_id',type=int)
  if oid is None and orders:oid=orders[0]['id']
  report=v24_reconcile_order(c,oid) if oid is not None else None
 return page('Сверка фактической загрузки',"""
 <div class='card'><h2>Сверка сырья: ТК / выдано / загружено</h2>
 <p class='muted'>Отчёт только для просмотра. Сверка не корректирует складские остатки и не заменяет подтверждение фактического расхода.</p>
 <form method='get'><label>Производственное задание<select name='order_id' required>{% for o in orders %}<option value='{{o.id}}' {% if oid==o.id %}selected{% endif %}>ПЗ-{{o.id}} · {{o.lot_code}} · {{o.status}}</option>{% endfor %}</select></label><button>Показать</button></form>
 {% if report %}<h3>ПЗ-{{report.order.id}} · {{report.order.lot_code}}</h3>
 <p>Статус задания: <b>{{report.order.status}}</b> · Замечаний: <b>{{report.issue_count}}</b></p>
 <table><thead><tr><th>Компонент</th><th>Норма ТК</th><th>Выдано</th><th>Загружено</th><th>Δ к норме</th><th>Δ к выдаче</th><th>Проверить</th></tr></thead><tbody>
 {% for r in report.rows %}<tr><td>{{r.name}}</td><td>{{'%.3f'|format(r.norm)}} {{r.unit}}</td><td>{{'%.3f'|format(r.issued)}}</td><td>{{'%.3f'|format(r.actual)}}</td><td>{{'%+.3f'|format(r.delta_norm)}}</td><td>{{'%+.3f'|format(r.delta_issued)}}</td><td>{% if r.issues %}{% for issue in r.issues %}<div class='danger'>{{issue}}</div>{% endfor %}{% else %}Совпадает{% endif %}</td></tr>{% endfor %}
 </tbody></table><p><a href='{{url_for("tsd_load_v23",order_id=oid)}}'>Открыть журнал загрузки</a> · <a href='/production/dashboard'>Пульт технолога</a></p>
 {% else %}<p>Производственных листов пока нет.</p>{% endif %}</div>
 <div class='card'><h3>Как читать отчёт</h3><p>«Выдано» — сумма записей подбора со статусами issued/consumed. Возвраты уменьшают выдачу через изменение исходных записей. «Загружено» — журнал v23. Данные могут расходиться, если складская выдача или выпуск проведены через старые маршруты. Не считайте расхождение доказанной недостачей.</p></div>
 """,orders=orders,oid=oid,report=report)


# v28: whole-container shipping with atomic lot/container deduction.
def v28_ship_containers(c,codes,customer,token,actor):
    # Must run within BEGIN IMMEDIATE. Duplicate token never applies stock twice.
    existing=c.execute('SELECT id FROM packing_shipments_v28 WHERE request_token=?',(token,)).fetchone()
    if existing:return existing['id'],False
    if not customer or len(customer)>200:raise ValueError('Укажите клиента (до 200 символов)')
    if not token or len(token)>100:raise ValueError('Неверный ключ операции')
    if not codes or len(codes)>100 or len(set(codes))!=len(codes):raise ValueError('Укажите от 1 до 100 разных QR-кодов')
    rows=[]
    for code in codes:
        r=c.execute("""SELECT ct.id,ct.code,ct.qty,ct.active,ct.lot_id,l.kind,l.status,l.expires,l.qty AS lot_qty,
          pi.job_id FROM containers ct JOIN lots l ON l.id=ct.lot_id
          LEFT JOIN finished_packing_items pi ON pi.container_id=ct.id WHERE ct.code=?""",(code,)).fetchone()
        if not r or r['kind']!='finished' or r['job_id'] is None:raise ValueError('QR не принадлежит зарегистрированной фасовке: '+code)
        if not r['active'] or r['qty']<=0:raise ValueError('Тара неактивна или пуста: '+code)
        if r['status']!='released' or r['expires']<date.today().isoformat():raise ValueError('Партия не разрешена ОТК или просрочена: '+code)
        rows.append(r)
    lot_id=rows[0]['lot_id']
    if any(r['lot_id']!=lot_id for r in rows):raise ValueError('В одной отгрузке допускается только одна партия')
    total=sum(float(r['qty']) for r in rows)
    if not math.isfinite(total) or total<=0:raise ValueError('Некорректное количество')
    require_finished_shipping_v66(c,lot_id)
    # Reject inconsistent inventory instead of silently hiding it.
    sum_packed=c.execute('SELECT COALESCE(SUM(qty),0) FROM containers WHERE lot_id=? AND active=1',(lot_id,)).fetchone()[0]
    if sum_packed>float(rows[0]['lot_qty'])+1e-6:raise ValueError('Нарушена сверка партии и тары; обратитесь к кладовщику')
    updated=c.execute("UPDATE lots SET qty=qty-? WHERE id=? AND kind='finished' AND status='released' AND expires>=? AND qty>=?",(total,lot_id,date.today().isoformat(),total))
    if updated.rowcount!=1:raise ValueError('Недостаточно остатка партии')
    for r in rows:
        cur=c.execute('UPDATE containers SET qty=0,active=0 WHERE id=? AND active=1 AND qty=?',(r['id'],r['qty']))
        if cur.rowcount!=1:raise ValueError('Остаток тары изменился во время отгрузки')
    sid=c.execute('INSERT INTO shipments(lot_id,customer,qty,created_at,user_id) VALUES(?,?,?,?,?)',(lot_id,customer,total,now(),actor)).lastrowid
    c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(lot_id,-total,'Отгрузка QR '+str(sid)+' '+customer,now(),actor))
    shipment_id=c.execute('INSERT INTO packing_shipments_v28(request_token,lot_id,customer,qty,shipment_id,created_at,actor_id) VALUES(?,?,?,?,?,?,?)',(token,lot_id,customer,total,sid,now(),actor)).lastrowid
    for r in rows:c.execute('INSERT INTO packing_shipment_items_v28(shipment_id,container_id,qty) VALUES(?,?,?)',(shipment_id,r['id'],r['qty']))
    return shipment_id,True

@app.route('/tsd/ship',methods=['GET','POST'])
@allowed('admin','warehouse')
def v28_tsd_ship():
    if request.method=='POST':
        try:
            customer=request.form.get('customer','').strip()
            token=request.form.get('request_token','').strip()
            raw=request.form.get('codes','')
            codes=[parse_container_scan(x) for x in raw.splitlines() if x.strip()]
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                shipment_id,created=v28_ship_containers(c,codes,customer,token,session['uid'])
                if created:audit(c,'packing.shipment',f'id={shipment_id}; customer={customer}; count={len(codes)}')
            flash('Отгрузка проведена' if created else 'Повторный запрос: отгрузка уже проведена')
            return redirect('/tsd/ship/'+str(shipment_id))
        except Exception as e:return fail(e)
    with conn() as c:
        recent=c.execute("""SELECT ps.id,ps.customer,ps.qty,ps.created_at,l.lot_code FROM packing_shipments_v28 ps JOIN lots l ON l.id=ps.lot_id ORDER BY ps.id DESC LIMIT 30""").fetchall()
    return page('ТСД · Отгрузка готовой продукции',"""<div class='card'><h2>Urovo CT48 · Отгрузка по QR</h2><p>Сканер ScanWedge / Keystroke + Enter. Каждая строка — одна целая единица тары. Отгрузка одной партии за операцию. Частичная отгрузка тары не поддерживается.</p>
    <form method='post'><input type='hidden' name='request_token' value='{{token}}'><label>Клиент<input name='customer' required maxlength='200'></label>
    <label>Коды тары (по одному на строку)<textarea id='codes' name='codes' rows='8' required placeholder='CHEMPRO:CONTAINER:...'></textarea></label>
    <button>Провести отгрузку</button></form><p class='muted'>Операция атомарно уменьшает остатки тары и партии и регистрирует складское движение. Проверьте список перед проведением.</p></div>
    <div class='card'><h3>Последние отгрузки</h3><table><tr><th>№</th><th>Дата</th><th>Партия</th><th>Клиент</th><th>Количество</th></tr>{% for x in recent %}<tr><td><a href='/tsd/ship/{{x.id}}'>{{x.id}}</a></td><td>{{x.created_at}}</td><td>{{x.lot_code}}</td><td>{{x.customer}}</td><td>{{x.qty}}</td></tr>{% endfor %}</table></div>
    <script>const a=document.getElementById('codes');if(a)a.focus();</script>""",recent=recent,token=uuid.uuid4().hex)

@app.get('/tsd/ship/<int:shipment_id>')
@allowed('admin','warehouse','technologist','quality','viewer')
def v28_shipment_detail(shipment_id):
    with conn() as c:
        head=c.execute("""SELECT ps.*,l.lot_code,p.name,p.unit,u.username FROM packing_shipments_v28 ps
          JOIN lots l ON l.id=ps.lot_id JOIN products p ON p.id=l.product_id
          JOIN users u ON u.id=ps.actor_id WHERE ps.id=?""",(shipment_id,)).fetchone()
        if not head:abort(404)
        items=c.execute("""SELECT ct.code,psi.qty FROM packing_shipment_items_v28 psi JOIN containers ct ON ct.id=psi.container_id WHERE psi.shipment_id=? ORDER BY ct.code""",(shipment_id,)).fetchall()
    return page('Отгрузка №'+str(shipment_id),"""<div class='card'><h2>Отгрузка №{{x.id}}</h2><p>Клиент: {{x.customer}} · {{x.created_at}} · {{x.username}}</p><p>{{x.name}} · партия {{x.lot_code}} · <b>{{x.qty}} {{x.unit}}</b></p><table><tr><th>QR-код тары</th><th>Отгружено</th></tr>{% for y in items %}<tr><td>{{y.code}}</td><td>{{y.qty}}</td></tr>{% endfor %}</table><p><a href='/tsd/ship'>← Новая отгрузка</a></p></div>""",x=head,items=items)



# v29: customer returns are physically quarantined without increasing saleable stock.
def v29_register_return(c,raw_code,token,reason,actor):
    if not token or len(token)>100:raise ValueError('Некорректный ключ операции')
    old=c.execute('SELECT id FROM customer_returns_v29 WHERE request_token=?',(token,)).fetchone()
    if old:return old['id'],False
    code=parse_container_scan(raw_code)
    if not reason.strip() or len(reason)>500:raise ValueError('Укажите причину возврата (до 500 символов)')
    r=c.execute("""SELECT ct.id AS container_id,ct.qty,ct.active,psi.qty AS shipped_qty,
      ps.id AS shipment_id,ps.lot_id,ps.customer
      FROM containers ct JOIN packing_shipment_items_v28 psi ON psi.container_id=ct.id
      JOIN packing_shipments_v28 ps ON ps.id=psi.shipment_id
      WHERE ct.code=?""",(code,)).fetchone()
    if not r:raise ValueError('Код отсутствует в истории QR-отгрузок')
    if r['active'] or float(r['qty'])!=0:raise ValueError('Тара не имеет статуса полностью отгруженной')
    if c.execute('SELECT id FROM customer_returns_v29 WHERE container_id=?',(r['container_id'],)).fetchone():
        raise ValueError('По этой таре уже зарегистрирован возврат')
    rid=c.execute("""INSERT INTO customer_returns_v29(request_token,shipment_id,container_id,lot_id,qty,reason,customer,registered_at,registered_by)
      VALUES(?,?,?,?,?,?,?,?,?)""",(token,r['shipment_id'],r['container_id'],r['lot_id'],r['shipped_qty'],reason.strip(),r['customer'],now(),actor)).lastrowid
    return rid,True

@app.route('/tsd/customer-return',methods=['GET','POST'])
@allowed('admin','warehouse')
def v29_tsd_customer_return():
    if request.method=='POST':
        try:
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                rid,created=v29_register_return(c,request.form.get('code',''),request.form.get('request_token',''),request.form.get('reason',''),session['uid'])
                if created:audit(c,'customer_return.register',f'return={rid}')
            flash('Возврат зарегистрирован в карантине' if created else 'Повторная отправка: возврат уже зарегистрирован')
            return redirect('/quality/customer-returns/'+str(rid))
        except (ValueError,sqlite3.IntegrityError) as e:return fail(e)
    return page('ТСД · Возврат от клиента',"""<div class='card'><h2>Возврат от клиента · Urovo CT48</h2>
      <p>Сканируйте QR ранее отгруженной тары (ScanWedge / Keystroke + Enter). Возврат помещается в карантин и <b>не увеличивает доступный остаток</b>.</p>
      <form method='post'><input type='hidden' name='request_token' value='{{token}}'>
      <label>QR-код тары<input id='return_code' name='code' required autofocus></label>
      <label>Причина возврата<textarea name='reason' maxlength='500' required></textarea></label>
      <button>Принять в карантин</button></form></div>
      <script>const x=document.getElementById('return_code');if(x)x.focus();</script>""",token=uuid.uuid4().hex)

@app.get('/quality/customer-returns')
@allowed('admin','quality','technologist','warehouse','viewer')
def v29_customer_returns():
    with conn() as c:
        rows=c.execute("""SELECT r.id,r.status,r.qty,r.registered_at,r.customer,ct.code,l.lot_code
          FROM customer_returns_v29 r JOIN containers ct ON ct.id=r.container_id
          JOIN lots l ON l.id=r.lot_id ORDER BY r.id DESC LIMIT 200""").fetchall()
    return page('ОТК · Возвраты клиентов',"""<div class='card'><h2>Возвраты готовой продукции</h2>
      <p>До решения ОТК возвращённая тара не включается в доступный остаток партии.</p>
      <table><tr><th>№</th><th>QR</th><th>Партия</th><th>Клиент</th><th>Количество</th><th>Статус</th></tr>
      {% for r in rows %}<tr><td><a href='/quality/customer-returns/{{r.id}}'>{{r.id}}</a></td><td>{{r.code}}</td><td>{{r.lot_code}}</td><td>{{r.customer}}</td><td>{{r.qty}}</td><td>{{r.status}}</td></tr>{% endfor %}</table></div>""",rows=rows)

@app.route('/quality/customer-returns/<int:rid>',methods=['GET','POST'])
@allowed('admin','quality','technologist','warehouse','viewer')
def v29_customer_return_detail(rid):
    if request.method=='POST':
        if not has_role('admin','quality'):abort(403)
        try:
            decision=request.form.get('decision','')
            note=request.form.get('note','').strip()
            if decision not in ('released','rejected') or not note or len(note)>500:raise ValueError('Нужно указать решение и основание (до 500 символов)')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                r=c.execute('SELECT * FROM customer_returns_v29 WHERE id=?',(rid,)).fetchone()
                if not r:abort(404)
                if r['status']!='quarantine':raise ValueError('Решение по возврату уже принято')
                if decision=='released':
                    lot=c.execute('SELECT kind,status,expires FROM lots WHERE id=?',(r['lot_id'],)).fetchone()
                    if not lot or lot['kind']!='finished' or lot['status']!='released' or lot['expires']<date.today().isoformat():
                        raise ValueError('Исходная партия не разрешена ОТК или просрочена. Возврат остаётся в карантине')
                    ct=c.execute('UPDATE containers SET qty=?,active=1 WHERE id=? AND qty=0 AND active=0',(r['qty'],r['container_id']))
                    if ct.rowcount!=1:raise ValueError('Состояние тары изменилось, операция остановлена')
                    c.execute('UPDATE lots SET qty=qty+? WHERE id=?',(r['qty'],r['lot_id']))
                    c.execute('INSERT INTO movements(lot_id,delta,reason,created_at,user_id) VALUES(?,?,?,?,?)',(r['lot_id'],r['qty'],'Возврат клиента ОТК #'+str(rid),now(),session['uid']))
                c.execute('UPDATE customer_returns_v29 SET status=?,decision_note=?,decided_at=?,decided_by=? WHERE id=?',(decision,note,now(),session['uid'],rid))
                audit(c,'customer_return.decision',f'return={rid}; decision={decision}')
            flash('Решение ОТК сохранено')
            return redirect('/quality/customer-returns/'+str(rid))
        except (ValueError,sqlite3.IntegrityError) as e:return fail(e)
    with conn() as c:
        r=c.execute("""SELECT r.*,ct.code,l.lot_code,l.status AS lot_status,l.expires,u.username AS registrar
          FROM customer_returns_v29 r JOIN containers ct ON ct.id=r.container_id
          JOIN lots l ON l.id=r.lot_id JOIN users u ON u.id=r.registered_by WHERE r.id=?""",(rid,)).fetchone()
    if not r:abort(404)
    return page('Возврат клиента №'+str(rid),"""<div class='card'><h2>Возврат №{{r.id}} · {{r.status}}</h2>
      <p>QR: {{r.code}} · Партия: {{r.lot_code}} · Клиент: {{r.customer}}</p>
      <p>Количество: {{r.qty}} · Исходная отгрузка: <a href='/tsd/ship/{{r.shipment_id}}'>№{{r.shipment_id}}</a></p>
      <p>Принято: {{r.registered_at}} ({{r.registrar}}) · Причина: {{r.reason}}</p>
      <p>Исходная партия: {{r.lot_status}} · Годен до: {{r.expires}}</p>
      {% if r.status=='quarantine' %}<p><b>Карантин.</b> Требуется физическая изоляция и проверка ОТК; форма не заменяет лабораторные испытания.</p>
      {% if has_role('admin','quality') %}<form method='post'>
      <label>Решение<select name='decision'><option value='rejected'>Забраковать / не возвращать в продажу</option><option value='released'>Разрешить к повторному обороту</option></select></label>
      <label>Обоснование и ссылка на проверку ОТК<textarea name='note' maxlength='500' required></textarea></label>
      <button>Зафиксировать решение</button></form>{% endif %}
      {% else %}<p>Решение: {{r.decision_note}} · {{r.decided_at}}</p>{% endif %}
      <p><a href='/quality/customer-returns'>← Журнал возвратов</a></p></div>""",r=r)


# v30: documentary write-off of rejected customer returns. Rejected returned goods
# were never restored to saleable inventory; creating an act must NOT subtract stock.
@app.route('/quality/disposal-acts',methods=['GET','POST'])
@allowed('admin','quality','technologist','viewer')
def v30_disposal_acts():
    if request.method=='POST':
        if not has_role('admin','quality'):abort(403)
        try:
            rid=int(request.form.get('return_id','0'))
            method=request.form.get('method','').strip()
            contractor=request.form.get('contractor','').strip()
            doc=request.form.get('document_ref','').strip()
            note=request.form.get('note','').strip()
            if method not in ('утилизация','уничтожение','передача подрядчику'):
                raise ValueError('Выберите способ обращения с браком')
            if not note or len(note)>1000 or len(contractor)>200 or len(doc)>200:
                raise ValueError('Требуется обоснование (до 1000 символов); проверьте длину полей')
            if method=='передача подрядчику' and (not contractor or not doc):
                raise ValueError('Для передачи подрядчику укажите организацию и документ')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                r=c.execute('SELECT * FROM customer_returns_v29 WHERE id=?',(rid,)).fetchone()
                if not r or r['status']!='rejected':
                    raise ValueError('Акт оформляется только на возврат, забракованный ОТК')
                if c.execute('SELECT id FROM return_disposal_acts_v30 WHERE return_id=?',(rid,)).fetchone():
                    raise ValueError('Акт по этому возврату уже создан')
                ct=c.execute('SELECT qty,active FROM containers WHERE id=?',(r['container_id'],)).fetchone()
                if not ct or float(ct['qty'])!=0 or ct['active']:
                    raise ValueError('Остаток или статус тары изменён: акт не создан')
                act_no='АБ-'+uuid.uuid4().hex[:12].upper()
                aid=c.execute("""INSERT INTO return_disposal_acts_v30
                  (act_no,return_id,container_id,lot_id,qty,method,contractor,document_ref,note,created_at,created_by)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(act_no,rid,r['container_id'],r['lot_id'],r['qty'],method,contractor,doc,note,now(),session['uid'])).lastrowid
                audit(c,'return_disposal.create',f'act={aid}; return={rid}; method={method}')
            flash('Акт брака зарегистрирован; складской остаток не изменён')
            return redirect('/quality/disposal-acts/'+str(aid))
        except (ValueError,sqlite3.IntegrityError) as e:return fail(e)
    with conn() as c:
        acts=c.execute("""SELECT a.id,a.act_no,a.created_at,a.method,a.qty,l.lot_code,ct.code
          FROM return_disposal_acts_v30 a JOIN lots l ON l.id=a.lot_id
          JOIN containers ct ON ct.id=a.container_id ORDER BY a.id DESC LIMIT 200""").fetchall()
        pending=c.execute("""SELECT r.id,r.qty,ct.code,l.lot_code FROM customer_returns_v29 r
          JOIN containers ct ON ct.id=r.container_id JOIN lots l ON l.id=r.lot_id
          LEFT JOIN return_disposal_acts_v30 a ON a.return_id=r.id
          WHERE r.status='rejected' AND a.id IS NULL ORDER BY r.id DESC""").fetchall()
    return page('ОТК · Акты брака',"""<div class='card'><h2>Акты брака и утилизации</h2>
      <p>Только забракованные ОТК возвраты. Акт фиксирует решение и способ обращения с продукцией; физическое уничтожение подтверждается отдельными документами. Остаток партии повторно не списывается.</p>
      {% if has_role('admin','quality') %}<form method='post'><label>Забракованный возврат<select name='return_id' required>
      {% for r in pending %}<option value='{{r.id}}'>Возврат №{{r.id}} · {{r.code}} · {{r.lot_code}} · {{r.qty}}</option>{% endfor %}
      </select></label><label>Способ<select name='method'><option>утилизация</option><option>уничтожение</option><option>передача подрядчику</option></select></label>
      <label>Подрядчик<input name='contractor' maxlength='200'></label><label>Номер внешнего документа<input name='document_ref' maxlength='200'></label>
      <label>Обоснование и описание операции<textarea name='note' required maxlength='1000'></textarea></label>
      <button {% if not pending %}disabled{% endif %}>Оформить акт</button></form>{% endif %}</div>
      <div class='card'><h3>Журнал актов</h3><table><tr><th>Номер</th><th>Дата</th><th>Партия / QR</th><th>Количество</th><th>Способ</th></tr>
      {% for a in acts %}<tr><td><a href='/quality/disposal-acts/{{a.id}}'>{{a.act_no}}</a></td><td>{{a.created_at}}</td><td>{{a.lot_code}} / {{a.code}}</td><td>{{a.qty}}</td><td>{{a.method}}</td></tr>{% endfor %}</table></div>""",acts=acts,pending=pending)

@app.get('/quality/disposal-acts/<int:aid>')
@allowed('admin','quality','technologist','viewer')
def v30_disposal_act_detail(aid):
    with conn() as c:
        a=c.execute("""SELECT a.*,l.lot_code,p.name AS product_name,ct.code,u.username,
          r.reason AS return_reason,r.decision_note AS qc_note FROM return_disposal_acts_v30 a
          JOIN lots l ON l.id=a.lot_id JOIN products p ON p.id=l.product_id
          JOIN containers ct ON ct.id=a.container_id JOIN users u ON u.id=a.created_by
          JOIN customer_returns_v29 r ON r.id=a.return_id WHERE a.id=?""",(aid,)).fetchone()
    if not a:abort(404)
    return page('Акт брака '+a['act_no'],"""<style>@media print{nav,.no-print{display:none!important}.card{border:0!important}}</style>
      <div class='card'><h2>АКТ БРАКА / УТИЛИЗАЦИИ № {{a.act_no}}</h2>
      <p>Дата оформления: {{a.created_at}} · Ответственный: {{a.username}}</p>
      <p>Продукция: {{a.product_name}} · Партия: {{a.lot_code}} · QR: {{a.code}}</p>
      <p>Возврат № {{a.return_id}} · Количество: <b>{{a.qty}}</b></p>
      <p>Причина возврата: {{a.return_reason}} · Решение ОТК: {{a.qc_note}}</p>
      <p>Способ: <b>{{a.method}}</b> · Подрядчик: {{a.contractor or '—'}}</p>
      <p>Внешний документ: {{a.document_ref or '—'}}</p><p>Основание: {{a.note}}</p>
      <p>Ответственный за передачу/утилизацию: ___________________</p>
      <p>Подпись: ___________________ &nbsp; Дата: ______________</p>
      <p class='no-print'><button onclick='window.print()'>Печать A4 / сохранить PDF</button> <a href='/quality/disposal-acts'>К журналу</a></p>
      <p><small>Регистрация акта в системе не подтверждает фактическое уничтожение продукции.</small></p></div>""",a=a)




# v31: Economist dispatch orders, warehouse QR loading and truck photo evidence.
def dispatch_v31_task(c,tid):
    task=c.execute("""SELECT t.*,u.username AS creator FROM dispatch_tasks_v31 t
       JOIN users u ON u.id=t.created_by WHERE t.id=?""",(tid,)).fetchone()
    if not task:abort(404)
    return task

@app.route('/dispatch/tasks',methods=['GET','POST'])
@allowed('admin','economist','warehouse','viewer')
def dispatch_v31_tasks():
    if request.method=='POST':
        if not has_role('admin','economist'):abort(403)
        try:
            customer=request.form.get('customer','').strip();vehicle=request.form.get('vehicle','').strip()
            driver=request.form.get('driver','').strip();due=request.form.get('due_at','').strip()
            note=request.form.get('note','').strip();lot_id=int(request.form['lot_id']);qty=number(request.form,'qty')
            if not customer or len(customer)>200 or len(vehicle)>100 or len(driver)>100 or len(note)>1000 or len(due)>40:raise ValueError('Проверьте реквизиты задания')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                lot=c.execute("SELECT id,qty FROM lots WHERE id=? AND kind='finished' AND status='released' AND expires>=?",(lot_id,date.today().isoformat())).fetchone()
                if not lot or float(lot['qty'])+1e-7<qty:raise ValueError('Партия недоступна или недостаточно остатка')
                tid=c.execute("INSERT INTO dispatch_tasks_v31(customer,vehicle,driver,due_at,note,created_at,created_by) VALUES(?,?,?,?,?,?,?)",(customer,vehicle,driver,due,note,now(),session['uid'])).lastrowid
                c.execute('INSERT INTO dispatch_task_lines_v31(task_id,lot_id,qty) VALUES(?,?,?)',(tid,lot_id,qty))
                audit(c,'dispatch.task.created',f'task={tid}; lot={lot_id}; qty={qty}')
            return redirect('/dispatch/tasks/'+str(tid))
        except Exception as e:return fail(e)
    with conn() as c:
        tasks=c.execute("SELECT t.*,l.lot_code,tl.qty FROM dispatch_tasks_v31 t JOIN dispatch_task_lines_v31 tl ON tl.task_id=t.id JOIN lots l ON l.id=tl.lot_id ORDER BY t.id DESC LIMIT 100").fetchall()
        lots=c.execute("""SELECT l.id,l.lot_code,l.qty,p.name FROM lots l JOIN products p ON p.id=l.product_id
          WHERE l.kind='finished' AND l.status='released' AND l.expires>=? AND l.qty>0 ORDER BY l.id DESC""",(date.today().isoformat(),)).fetchall()
    return page('Задания на отгрузку',"""<div class='card'><h2>Задания экономиста → кладовщику</h2>
    {% if has_role('admin','economist') %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'>
    <label>Покупатель<input name='customer' required maxlength='200'></label><label>Автомобиль / госномер<input name='vehicle' maxlength='100'></label>
    <label>Водитель<input name='driver' maxlength='100'></label><label>Дата/время подачи<input type='datetime-local' name='due_at'></label>
    <label>Готовая партия<select name='lot_id' required>{% for l in lots %}<option value='{{l.id}}'>{{l.name}} · {{l.lot_code}} · остаток {{l.qty}}</option>{% endfor %}</select></label>
    <label>Количество, в единицах продукции<input type='number' name='qty' step='any' min='0.001' required></label>
    <label>Указания кладовщику<textarea name='note' maxlength='1000'></textarea></label><button>Создать задание</button></form>
    <p class='muted'>Одно задание — одна партия. Количество должно совпасть с суммой отсканированных целых единиц тары.</p>{% endif %}</div>
    <div class='card'><h3>Журнал</h3><table><tr><th>№</th><th>Покупатель</th><th>Партия</th><th>Количество</th><th>Машина</th><th>Статус</th></tr>
    {% for t in tasks %}<tr><td><a href='/dispatch/tasks/{{t.id}}'>№{{t.id}}</a></td><td>{{t.customer}}</td><td>{{t.lot_code}}</td><td>{{t.qty}}</td><td>{{t.vehicle}}</td><td>{{t.status}}{% if t.status=='completed' %} · <a href='/dispatch/v39/{{t.id}}'>Фотоотчёт</a>{% endif %}</td></tr>{% endfor %}</table></div>""",tasks=tasks,lots=lots)

@app.route('/dispatch/tasks/<int:tid>',methods=['GET','POST'])
@allowed('admin','economist','warehouse','viewer')
def dispatch_v31_detail(tid):
    if request.method=='POST':
        if not has_role('admin','warehouse'):abort(403)
        try:
            action=request.form.get('action','')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE');t=dispatch_v31_task(c,tid)
                if action=='assign':
                    if t['status']!='new':raise ValueError('Задание уже принято')
                    c.execute("UPDATE dispatch_tasks_v31 SET status='assigned',assigned_at=?,assigned_by=? WHERE id=?",(now(),session['uid'],tid))
                elif action=='ship':
                    if t['status']!='assigned':raise ValueError('Сначала примите задание')
                    if c.execute('SELECT COUNT(*) FROM dispatch_task_lines_v31 WHERE task_id=?',(tid,)).fetchone()[0]!=1:raise ValueError('Многопозиционное задание отгружайте через /dispatch/v32')
                    if c.execute('SELECT 1 FROM dispatch_scans_v32 WHERE task_id=?',(tid,)).fetchone():raise ValueError('Частичная отгрузка уже проведена через v32')
                    line=c.execute('SELECT * FROM dispatch_task_lines_v31 WHERE task_id=?',(tid,)).fetchone()
                    codes=[parse_container_scan(x) for x in request.form.get('codes','').splitlines() if x.strip()]
                    if not codes or len(codes)>100 or len(set(codes))!=len(codes):raise ValueError('Укажите 1–100 уникальных QR')
                    placeholders=','.join('?' for _ in codes)
                    rows=c.execute('SELECT code,lot_id,qty FROM containers WHERE code IN ('+placeholders+')',codes).fetchall()
                    if len(rows)!=len(codes) or any(r['lot_id']!=line['lot_id'] for r in rows):raise ValueError('QR не соответствует партии задания')
                    total=sum(float(r['qty']) for r in rows)
                    if abs(total-float(line['qty']))>1e-6:raise ValueError(f'Задано {line["qty"]}, отсканировано {total}; отгрузка заблокирована')
                    shipment_id,created=v28_ship_containers(c,codes,t['customer'],'dispatch-v31-'+str(tid),session['uid'])
                    if not created:raise ValueError('Отгрузка уже зарегистрирована')
                    c.execute("UPDATE dispatch_tasks_v31 SET status='shipped',shipment_id=?,shipped_at=? WHERE id=?",(shipment_id,now(),tid))
                elif action=='complete':
                    if t['status']!='shipped':raise ValueError('Сначала проведите отгрузку')
                    count=c.execute('SELECT COUNT(*) FROM dispatch_photos_v31 WHERE task_id=?',(tid,)).fetchone()[0]
                    if count<1:raise ValueError('Для завершения нужен хотя бы один фотоснимок загруженной машины')
                    c.execute("UPDATE dispatch_tasks_v31 SET status='completed',completed_at=?,completed_by=? WHERE id=?",(now(),session['uid'],tid))
                else:raise ValueError('Неизвестное действие')
                audit(c,'dispatch.task.'+action,f'task={tid}')
            flash('Действие выполнено');return redirect('/dispatch/tasks/'+str(tid))
        except Exception as e:return fail(e)
    with conn() as c:
        t=dispatch_v31_task(c,tid)
        line=c.execute("SELECT tl.*,l.lot_code,p.name,p.unit FROM dispatch_task_lines_v31 tl JOIN lots l ON l.id=tl.lot_id JOIN products p ON p.id=l.product_id WHERE tl.task_id=?",(tid,)).fetchone()
        photos=c.execute('SELECT id,caption,created_at FROM dispatch_photos_v31 WHERE task_id=? ORDER BY id',(tid,)).fetchall()
    return page('Задание на отгрузку №'+str(tid),"""<div class='card'><p><b>Статус:</b> {{t.status}} · <b>Покупатель:</b> {{t.customer}}</p>
    <p><b>Продукция:</b> {{line.name}} · <b>Партия:</b> {{line.lot_code}} · <b>Задано:</b> {{line.qty}} {{line.unit}}</p>
    <p><b>Автомобиль:</b> {{t.vehicle}} · <b>Водитель:</b> {{t.driver}} · <b>Подача:</b> {{t.due_at}}</p>
    <p><b>Указания:</b> {{t.note}} · <b>Экономист:</b> {{t.creator}}</p>
    {% if t.shipment_id %}<p><a href='/tsd/ship/{{t.shipment_id}}'>Проведённая отгрузка №{{t.shipment_id}}</a></p>{% endif %}
    {% if has_role('admin','warehouse') and t.status=='new' %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='assign'><button>Принять в работу</button></form>{% endif %}
    {% if has_role('admin','warehouse') and t.status=='assigned' %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='ship'>
    <label>QR тары (CT48 ScanWedge + Enter, один код на строку)<textarea name='codes' rows='9' id='dispatch_codes' required autofocus></textarea></label>
    <p class='muted'>Сверка количества обязательна. Проведение атомарно уменьшает остатки партии и тары.</p><button>Подтвердить отгрузку</button></form>{% endif %}
    </div><div class='card'><h3>Фотоотчёт загруженной машины</h3>
    {% if has_role('admin','warehouse') and t.status in ('assigned','shipped','completed') %}<form method='post' action='/dispatch/tasks/{{t.id}}/photos' enctype='multipart/form-data'>
    <input type='hidden' name='_csrf' value='{{csrf_token}}'><label>Фото JPEG, PNG или WebP (до 8 МБ)<input type='file' name='photo' accept='image/jpeg,image/png,image/webp' capture='environment' required></label>
    <label>Комментарий<input name='caption' maxlength='300' placeholder='Вид загруженного кузова, пломба...'></label><button>Добавить фото</button></form>{% endif %}
    <div class='grid'>{% for ph in photos %}<div><a href='/dispatch/photos/{{ph.id}}' target='_blank'><img src='/dispatch/photos/{{ph.id}}' loading='lazy' style='max-width:100%;max-height:260px'></a><p>{{ph.caption}} · {{ph.created_at}}</p></div>{% endfor %}</div>
    {% if has_role('admin','warehouse') and t.status=='shipped' %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='complete'><button>Завершить с фотоотчётом</button></form>{% endif %}</div>
    <p><a href='/dispatch/tasks'>← Все задания</a></p>""",t=t,line=line,photos=photos)

@app.post('/dispatch/tasks/<int:tid>/photos')
@allowed('admin','warehouse')
def dispatch_v31_photo_upload(tid):
    try:
        f=request.files.get('photo')
        if not f:raise ValueError('Выберите фотографию')
        blob=f.stream.read(8*1024*1024+1)
        if not blob or len(blob)>8*1024*1024:raise ValueError('Фото должно быть не больше 8 МБ')
        if blob.startswith(b'\xff\xd8\xff'):mime='image/jpeg'
        elif blob.startswith(b'\x89PNG\r\n\x1a\n'):mime='image/png'
        elif blob[:4]==b'RIFF' and blob[8:12]==b'WEBP':mime='image/webp'
        else:raise ValueError('Поддерживаются только JPEG, PNG и WebP')
        caption=request.form.get('caption','').strip()
        if len(caption)>300:raise ValueError('Комментарий слишком длинный')
        with conn() as c:
            c.execute('BEGIN IMMEDIATE');t=dispatch_v31_task(c,tid)
            if t['status'] not in ('assigned','shipped','completed'):raise ValueError('Нельзя приложить фото к этому заданию')
            if c.execute('SELECT 1 FROM dispatch_receipts_v32 WHERE task_id=?',(tid,)).fetchone():raise ValueError('Отчёт закрыт, фото добавлять нельзя')
            if c.execute('SELECT COUNT(*) FROM dispatch_photos_v31 WHERE task_id=?',(tid,)).fetchone()[0]>=20:raise ValueError('Не более 20 фото на задание')
            pid=c.execute('INSERT INTO dispatch_photos_v31(task_id,mime,photo,sha256,caption,created_at,created_by) VALUES(?,?,?,?,?,?,?)',(tid,mime,blob,hashlib.sha256(blob).hexdigest(),caption,now(),session['uid'])).lastrowid
            audit(c,'dispatch.photo.added',f'task={tid}; photo={pid}')
        return redirect('/dispatch/tasks/'+str(tid))
    except Exception as e:return fail(e)

@app.get('/dispatch/photos/<int:pid>')
@allowed('admin','economist','warehouse','viewer')
def dispatch_v31_photo(pid):
    with conn() as c:
        p=c.execute('SELECT mime,photo FROM dispatch_photos_v31 WHERE id=?',(pid,)).fetchone()
    if not p:abort(404)
    response=send_file(io.BytesIO(p['photo']),mimetype=p['mime'],as_attachment=False,download_name='truck-photo')
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Cache-Control']='private, no-store'
    response.headers['Content-Security-Policy']="default-src 'none'; sandbox"
    return response


# v32: multi-line dispatch orders, incremental QR shipments, immutable printed receipt.
def v32_lines(c,tid):
    return c.execute("""SELECT dl.id,dl.lot_id,dl.qty,l.lot_code,p.name,p.unit,
      COALESCE((SELECT SUM(ds.qty) FROM dispatch_scans_v32 ds WHERE ds.line_id=dl.id),0) AS shipped
      FROM dispatch_task_lines_v31 dl JOIN lots l ON l.id=dl.lot_id
      JOIN products p ON p.id=l.product_id WHERE dl.task_id=? ORDER BY dl.id""",(tid,)).fetchall()

def v32_eligible(c,tid):
    t=dispatch_v31_task(c,tid)
    if c.execute('SELECT 1 FROM dispatch_receipts_v32 WHERE task_id=?',(tid,)).fetchone():
        raise ValueError('Задание закрыто, операции запрещены')
    if t['status'] not in ('new','assigned','shipped'):
        raise ValueError('Задание недоступно для отгрузки')
    if t['shipment_id'] is not None:
        raise ValueError('Задание ранее отгружено через v31; повторное списание запрещено')
    return t

@app.route('/dispatch/v32',methods=['GET','POST'])
@allowed('admin','economist','warehouse','viewer')
def dispatch_v32_index():
    if request.method=='POST':
        if not has_role('admin','economist'):abort(403)
        try:
            customer=request.form.get('customer','').strip()
            vehicle=request.form.get('vehicle','').strip();driver=request.form.get('driver','').strip()
            due=request.form.get('due_at','').strip();note=request.form.get('note','').strip()
            raw=request.form.get('lines','').strip().splitlines()
            if not customer or len(customer)>200 or len(vehicle)>100 or len(driver)>100 or len(due)>40 or len(note)>1000:
                raise ValueError('Проверьте реквизиты')
            if not raw or len(raw)>30:raise ValueError('Требуется от 1 до 30 строк')
            parsed=[];seen=set()
            for row in raw:
                a=row.split(';')
                if len(a)!=2:raise ValueError('Формат строки: ID партии;количество')
                lid=int(a[0].strip());q=float(a[1].strip().replace(',','.'))
                if lid<=0 or not math.isfinite(q) or q<=0 or q>1e12 or lid in seen:
                    raise ValueError('Неверная партия, количество или повтор строки')
                seen.add(lid);parsed.append((lid,q))
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                for lid,q in parsed:
                    lot=c.execute("SELECT qty FROM lots WHERE id=? AND kind='finished' AND status='released' AND expires>=?",(lid,date.today().isoformat())).fetchone()
                    if not lot or float(lot['qty'])+1e-7<q:raise ValueError('Недоступная партия или недостаточный остаток: '+str(lid))
                tid=c.execute('INSERT INTO dispatch_tasks_v31(customer,vehicle,driver,due_at,note,created_at,created_by) VALUES(?,?,?,?,?,?,?)',
                    (customer,vehicle,driver,due,note,now(),session['uid'])).lastrowid
                c.executemany('INSERT INTO dispatch_task_lines_v31(task_id,lot_id,qty) VALUES(?,?,?)',[(tid,lid,q) for lid,q in parsed])
                audit(c,'dispatch.v32.created',f'task={tid}; lines={len(parsed)}')
            return redirect('/dispatch/v32/'+str(tid))
        except Exception as e:return fail(e)
    with conn() as c:
        tasks=c.execute('SELECT id,customer,vehicle,status,created_at FROM dispatch_tasks_v31 ORDER BY id DESC LIMIT 100').fetchall()
        lots=c.execute("""SELECT l.id,l.lot_code,l.qty,p.name,p.unit FROM lots l JOIN products p ON p.id=l.product_id
          WHERE l.kind='finished' AND l.status='released' AND l.expires>=? AND l.qty>0 ORDER BY l.id DESC LIMIT 100""",(date.today().isoformat(),)).fetchall()
    return page('Многопозиционные отгрузки',"""<div class='card'><h2>Задания экономиста: несколько партий</h2>
    {% if has_role('admin','economist') %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'>
    <label>Клиент<input name='customer' required maxlength='200'></label><label>Автомобиль<input name='vehicle' maxlength='100'></label>
    <label>Водитель<input name='driver' maxlength='100'></label><label>Дата подачи<input name='due_at' type='datetime-local'></label>
    <label>Строки: ID партии; количество (каждая с новой строки)<textarea name='lines' rows='5' required placeholder='12;250&#10;15;500'></textarea></label>
    <label>Указания<textarea name='note' maxlength='1000'></textarea></label><button>Создать задание</button></form>
    <p>Доступные партии:</p><table><tr><th>ID</th><th>Продукция</th><th>Партия</th><th>Остаток</th></tr>
    {% for l in lots %}<tr><td>{{l.id}}</td><td>{{l.name}}</td><td>{{l.lot_code}}</td><td>{{l.qty}} {{l.unit}}</td></tr>{% endfor %}</table>{% endif %}</div>
    <div class='card'><h3>Задания</h3><table><tr><th>№</th><th>Клиент</th><th>Машина</th><th>Статус</th></tr>
    {% for t in tasks %}<tr><td><a href='/dispatch/v32/{{t.id}}'>{{t.id}}</a></td><td>{{t.customer}}</td><td>{{t.vehicle}}</td><td>{{t.status}}</td></tr>{% endfor %}</table></div>""",tasks=tasks,lots=lots)

@app.route('/dispatch/v32/<int:tid>',methods=['GET','POST'])
@allowed('admin','economist','warehouse','viewer')
def dispatch_v32_detail(tid):
    if request.method=='POST':
        if not has_role('admin','warehouse'):abort(403)
        try:
            action=request.form.get('action','')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE');t=v32_eligible(c,tid)
                if action=='assign':
                    if t['status']!='new':raise ValueError('Задание уже принято')
                    c.execute("UPDATE dispatch_tasks_v31 SET status='assigned',assigned_at=?,assigned_by=? WHERE id=?",(now(),session['uid'],tid))
                elif action=='ship':
                    if t['status']!='assigned':raise ValueError('Сначала примите задание')
                    line_id=int(request.form.get('line_id','0'))
                    line=next((r for r in v32_lines(c,tid) if r['id']==line_id),None)
                    if line is None:raise ValueError('Строка не относится к заданию')
                    codes=[parse_container_scan(x) for x in request.form.get('codes','').splitlines() if x.strip()]
                    if not codes or len(codes)>100 or len(codes)!=len(set(codes)):
                        raise ValueError('От 1 до 100 уникальных QR')
                    placeholders=','.join('?' for _ in codes)
                    rows=c.execute('SELECT id,code,lot_id,qty FROM containers WHERE code IN ('+placeholders+')',codes).fetchall()
                    if len(rows)!=len(codes) or any(r['lot_id']!=line['lot_id'] for r in rows):
                        raise ValueError('Тара не соответствует выбранной партии')
                    qty=sum(float(r['qty']) for r in rows)
                    if qty<=0 or qty>float(line['qty'])-float(line['shipped'])+1e-6:
                        raise ValueError('Количество превышает остаток задания')
                    if c.execute('SELECT 1 FROM dispatch_scans_v32 WHERE task_id=? AND container_id IN ('+placeholders+')',(tid,*[r['id'] for r in rows])).fetchone():
                        raise ValueError('QR уже проведён по заданию')
                    token=request.form.get('request_token','').strip()
                    if not token or len(token)>70:raise ValueError('Неверный ключ операции')
                    shipment_id,created=v28_ship_containers(c,codes,t['customer'],'dispatch-v32-'+str(tid)+'-'+token,session['uid'])
                    if not created:raise ValueError('Эта операция уже выполнена')
                    c.executemany('INSERT INTO dispatch_scans_v32(task_id,line_id,shipment_id,container_id,qty,created_at,created_by) VALUES(?,?,?,?,?,?,?)',
                      [(tid,line_id,shipment_id,r['id'],r['qty'],now(),session['uid']) for r in rows])
                elif action=='complete':
                    lines=v32_lines(c,tid)
                    if not lines or any(abs(float(r['qty'])-float(r['shipped']))>1e-6 for r in lines):
                        raise ValueError('Не все позиции отгружены; закрытие запрещено')
                    photos=c.execute('SELECT id,caption,sha256,created_at FROM dispatch_photos_v31 WHERE task_id=? ORDER BY id',(tid,)).fetchall()
                    if not photos:raise ValueError('Добавьте хотя бы одно фото машины')
                    data=dict(task_id=tid,customer=t['customer'],vehicle=t['vehicle'],driver=t['driver'],
                        lines=[dict(x) for x in lines],photos=[dict(x) for x in photos],closed_at=now())
                    c.execute('INSERT INTO dispatch_receipts_v32(task_id,closed_at,closed_by,snapshot_json) VALUES(?,?,?,?)',
                        (tid,now(),session['uid'],json.dumps(data,ensure_ascii=False)))
                    c.execute("UPDATE dispatch_tasks_v31 SET status='completed',completed_at=?,completed_by=? WHERE id=?",(now(),session['uid'],tid))
                else:raise ValueError('Неизвестная операция')
                audit(c,'dispatch.v32.'+action,f'task={tid}')
            flash('Операция выполнена');return redirect('/dispatch/v32/'+str(tid))
        except Exception as e:return fail(e)
    with conn() as c:
        t=dispatch_v31_task(c,tid);lines=v32_lines(c,tid)
        photos=c.execute('SELECT id,caption,created_at FROM dispatch_photos_v31 WHERE task_id=? ORDER BY id',(tid,)).fetchall()
        closed=c.execute('SELECT task_id FROM dispatch_receipts_v32 WHERE task_id=?',(tid,)).fetchone()
        legacy=t['shipment_id'] is not None
    return page('Задание №'+str(tid),"""<div class='card'><h2>Отгрузка №{{t.id}} · {{t.status}}</h2>
    <p><b>Клиент:</b> {{t.customer}} · <b>Машина:</b> {{t.vehicle}} · <b>Водитель:</b> {{t.driver}}</p><p>{{t.note}}</p>
    <table><tr><th>Продукция / партия</th><th>Задано</th><th>Отгружено</th><th>Осталось</th></tr>
    {% for l in lines %}<tr><td>{{l.name}} · {{l.lot_code}}</td><td>{{l.qty}} {{l.unit}}</td><td>{{l.shipped}}</td><td>{{'%.3f'|format(l.qty-l.shipped)}}</td></tr>{% endfor %}</table>
    {% if legacy %}<p class='danger'>Отгрузка уже проведена в v31; повторные операции через v32 запрещены.</p>{% endif %}
    {% if has_role('admin','warehouse') and not closed and not legacy %}
    {% if t.status=='new' %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='assign'><button>Принять задание</button></form>{% endif %}
    {% if t.status=='assigned' %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='ship'>
    <input type='hidden' name='request_token' value='{{token}}'><label>Позиция<select name='line_id'>{% for l in lines if l.qty-l.shipped>0.000001 %}<option value='{{l.id}}'>{{l.name}} / {{l.lot_code}} · осталось {{'%.3f'|format(l.qty-l.shipped)}}</option>{% endfor %}</select></label>
    <label>QR тары — ScanWedge + Enter<textarea name='codes' rows='7' required autofocus></textarea></label><button>Отгрузить отсканированную тару</button></form>{% endif %}
    {% if t.status=='assigned' %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='complete'><button>Закрыть задание после полной загрузки и фото</button></form>{% endif %}{% endif %}
    {% if closed %}<p><a href='/dispatch/v32/{{t.id}}/report' target='_blank'>Печатный отчёт A4 с фото</a> · <a href='/dispatch/v39/{{t.id}}'>Проверка фотоотчёта экономистом</a></p>{% endif %}</div>
    <div class='card'><h3>Фотоотчёт</h3>{% if has_role('admin','warehouse') and not closed and t.status=='assigned' %}
    <form method='post' action='/dispatch/tasks/{{t.id}}/photos' enctype='multipart/form-data'><input type='hidden' name='_csrf' value='{{csrf_token}}'>
    <input type='file' name='photo' accept='image/jpeg,image/png,image/webp' capture='environment' required><input name='caption' maxlength='300' placeholder='Кузов, крепление, пломба'><button>Добавить фото</button></form>{% endif %}
    {% for ph in photos %}<a href='/dispatch/photos/{{ph.id}}' target='_blank'><img src='/dispatch/photos/{{ph.id}}' style='max-width:220px;max-height:180px' loading='lazy'></a><p>{{ph.caption}} · {{ph.created_at}}</p>{% endfor %}</div>""",t=t,lines=lines,photos=photos,closed=closed,legacy=legacy,token=uuid.uuid4().hex)

@app.get('/dispatch/v32/<int:tid>/report')
@allowed('admin','economist','warehouse','viewer')
def dispatch_v32_report(tid):
    with conn() as c:
        receipt=c.execute('SELECT snapshot_json FROM dispatch_receipts_v32 WHERE task_id=?',(tid,)).fetchone()
        if not receipt:abort(404)
        snapshot=json.loads(receipt['snapshot_json'])
    return page('Акт отгрузки №'+str(tid),"""<style>@media print{nav,button{display:none!important}body{font-size:11pt}img{break-inside:avoid}}</style>
    <div class='card'><h2>Отчёт об отгрузке №{{r.task_id}}</h2><p>Клиент: {{r.customer}} · Автомобиль: {{r.vehicle}} · Водитель: {{r.driver}}</p>
    <p>Закрыто: {{r.closed_at}}</p><table><tr><th>Продукция</th><th>Партия</th><th>Задано</th><th>Отгружено</th></tr>
    {% for l in r.lines %}<tr><td>{{l.name}}</td><td>{{l.lot_code}}</td><td>{{l.qty}} {{l.unit}}</td><td>{{l.shipped}}</td></tr>{% endfor %}</table>
    <h3>Фото загруженной машины</h3>{% for ph in r.photos %}<div><img src='/dispatch/photos/{{ph.id}}' style='max-width:90%;max-height:340px'><p>{{ph.caption}} · {{ph.created_at}}</p></div>{% endfor %}
    <p>Кладовщик _____________________ &nbsp; Водитель _____________________</p><button onclick='window.print()'>Печать / сохранить PDF</button></div>""",r=snapshot)



@app.route('/profile/menu',methods=['GET','POST'])
@login_required
def profile_menu_v34():
 roles=current_roles()
 available=[(group,path,label) for group,path,label,allowed_roles in MENU_V34
            if 'admin' in roles or not allowed_roles or roles.intersection(allowed_roles)]
 if request.method=='POST':
  selected=set(request.form.getlist('visible'))
  allowed_paths={path for _,path,_ in available}
  if not selected.issubset(allowed_paths):abort(400)
  with conn() as c:
   c.execute('BEGIN IMMEDIATE')
   c.execute('DELETE FROM user_menu_v34 WHERE user_id=?',(session['uid'],))
   c.executemany('INSERT INTO user_menu_v34(user_id,path,visible) VALUES(?,?,?)',
                 [(session['uid'],path,int(path in selected)) for path in allowed_paths])
   audit(c,'user.menu.update','visible='+str(len(selected))+' available='+str(len(allowed_paths)))
  flash('Настройки меню сохранены')
  return redirect('/profile/menu')
 visible={path for _,path,_ in menu_v34()}
 return page('Настройка моего меню','''<div class="card"><p>Отметьте разделы, которые хотите видеть в верхнем меню. Доступ к разделам определяется назначенными ролями; скрытие ссылки не изменяет права.</p>
 <form method="post">{% for group,path,label in entries %}<label style="display:flex;flex-direction:row;align-items:center;gap:8px"><input type="checkbox" name="visible" value="{{path}}" {% if path in visible %}checked{% endif %}> <span>{{group}} · {{label}}</span></label>{% endfor %}
 <button>Сохранить моё меню</button></form></div>''',entries=available,visible=visible)

# v36: Warehouse, production, procurement and CT48 operation permissions.
# Defaults preserve the original role checks; explicit denies restrict them.
PERMISSIONS_V36 = {
 'warehouse.view':'Склад: просмотр партий и тары',
 'warehouse.edit':'Склад: приёмка и изменение остатков',
 'warehouse.pack':'Склад: фасовка готовой продукции',
 'warehouse.ship':'Склад: проведение отгрузки',
 'warehouse.reconcile':'Склад: сверка остатков',
 'tsd.view':'ТСД: просмотр рабочего места',
 'tsd.receive':'ТСД: приёмка',
 'tsd.pick':'ТСД: подбор сырья',
 'tsd.issue':'ТСД: выдача сырья',
 'tsd.return':'ТСД: возврат сырья',
 'tsd.load':'ТСД: регистрация загрузки сырья',
 'tsd.ship':'ТСД: отгрузка готовой продукции',
 'tsd.customer_return':'ТСД: возврат от клиента',
 'production.view':'Производство: просмотр заданий и листов',
 'production.plan':'Производство: планирование и изменение заданий',
 'production.record':'Производство: ввод измерений и этапов',
 'production.release':'Производство: выпуск продукции',
 'procurement.view':'Снабжение: просмотр закупок и резервов',
 'procurement.edit':'Снабжение: создание и изменение закупок и резервов',
 'quality.returns':'ОТК: решения по возвратам',
 'quality.disposal':'ОТК: оформление актов утилизации',
}
# Each tuple: permission for read requests, permission for write requests.
# GET on a mixed read/write endpoint never implies permission to POST.
V36_ENDPOINTS = {
 'lots':('warehouse.view','warehouse.edit'),
 'containers_view':('warehouse.view','warehouse.edit'),
 'container_operations_view':('warehouse.view','warehouse.edit'),
 'packing_view':('warehouse.view','warehouse.pack'),
 'packing_job_view':('warehouse.view',None),
 'packing_job_print':('warehouse.view',None),
 'containers_batch_print':('warehouse.view',None),
 'containers_print_log':('warehouse.view',None),
 'container_label':('warehouse.view',None),
 'reconciliation':('warehouse.reconcile',None),
 'shipments':('warehouse.view','warehouse.ship'),
 'tsd_workplace':('tsd.view','tsd.pick'),
 'scan_container':('tsd.view','tsd.pick'),
 'tsd_receive':('tsd.view','tsd.receive'),
 'tsd_pick':('tsd.view','tsd.pick'),
 'tsd_issue':('tsd.view','tsd.issue'),
 'tsd_return':('tsd.view','tsd.return'),
 'tsd_load_v23':('tsd.view','tsd.load'),
 'tsd_packing_view':('tsd.view',None),
 'tsd_packing_scan':('tsd.view','warehouse.pack'),
 'v28_tsd_ship':('tsd.view','tsd.ship'),
 'v28_shipment_detail':('tsd.view',None),
 'v29_tsd_customer_return':('tsd.view','tsd.customer_return'),
 'v29_customer_returns':('quality.view',None),
 'v29_customer_return_detail':('quality.view','quality.returns'),
 'v30_disposal_acts':('quality.view','quality.disposal'),
 'v30_disposal_act_detail':('quality.view',None),
 'work_orders':('production.view','production.plan'),
 'production':('production.view','production.release'),
 'production_dashboard_v20':('production.view',None),
 'production_sheets_v17':('production.view','production.plan'),
 'production_sheet_v17':('production.view','production.record'),
 'production_sheet_attachment_v19':('production.view',None),
 'production_sheet_print_v17':('production.view',None),
 'tsd_production_v17':('production.view',None),
 'shift_plan_v21':('production.view','production.plan'),
 'tsd_shift_plan_v21':('production.view',None),
 'production_stages_v22':('production.view','production.plan'),
 'tsd_stages_v22':('production.view','production.record'),
 'v24_load_reconciliation':('production.view',None),
 'supply':('procurement.view','procurement.edit'),
 'procurement':('procurement.view','procurement.edit'),
}
# Existing v35 permission names remain available in the same admin editor.

# v35: Additional deny-only permission layer. Original @allowed guards remain mandatory.
# Role permissions can be denied, and individual users can be denied or restored
# to their role's permission. An explicit user allow never grants a missing role.
PERMISSIONS_V35 = {
 'dispatch.view': 'Отгрузка: просмотр заданий',
 'dispatch.create': 'Отгрузка: создание заданий',
 'dispatch.execute': 'Отгрузка: выполнение / сканирование',
 'dispatch.photo': 'Отгрузка: загрузка фотоотчёта',
 'dispatch.close': 'Отгрузка: закрытие задания',
 'quality.view': 'ОТК: просмотр проб и паспортов',
 'quality.measure': 'ОТК: ввод результатов испытаний',
 'quality.approve': 'ОТК: подтверждение проб',
 'quality.passport': 'ОТК: выпуск паспорта качества',
 'technology.view': 'Технология: просмотр техкарт',
 'technology.edit': 'Технология: изменение рецептур',
 **PERMISSIONS_V36,
}

# Permission mapping uses actual Flask endpoint names, not untrusted paths.
V35_ENDPOINTS = {
 'dispatch_v31_tasks': ('dispatch.view','dispatch.create'),
 'dispatch_v32_index': ('dispatch.view','dispatch.create'),
 'dispatch_v31_detail': ('dispatch.view','dispatch.execute'),
 'dispatch_v32_detail': ('dispatch.view','dispatch.execute'),
 'dispatch_v31_photo_upload': ('dispatch.view','dispatch.photo'),
 'dispatch_v31_photo': ('dispatch.view',None),
 'dispatch_v32_report': ('dispatch.view',None),
 'quality_samples': ('quality.view',None),
 'quality_passports': ('quality.view','quality.passport'),
 'quality_passport_print': ('quality.view',None),
 'recipes': ('technology.view','technology.edit'),
}

V35_QUALITY_ACTIONS = {'create':'quality.measure','measure':'quality.measure',
 'review':'quality.approve','reject':'quality.approve'}
V35_DISPATCH_CLOSE = {'close','complete','finish'}

def v35_permission_for_request():
 endpoint=request.endpoint
 if endpoint not in V35_ENDPOINTS and endpoint not in V36_ENDPOINTS:return None
 get_perm,post_perm=V35_ENDPOINTS.get(endpoint,V36_ENDPOINTS.get(endpoint))
 if request.method not in ('POST','PUT','PATCH','DELETE'):return get_perm
 if endpoint=='quality_samples':
  return V35_QUALITY_ACTIONS.get(request.form.get('action',''), 'quality.approve')
 if endpoint in ('dispatch_v31_detail','dispatch_v32_detail') and request.form.get('action','') in V35_DISPATCH_CLOSE:
  return 'dispatch.close'
 if endpoint=='work_orders' and request.form.get('action') in ('complete','release'):
  return 'production.release'
 return post_perm or get_perm

def v35_permission_allowed(uid,roles,permission,c=None):
 if permission not in PERMISSIONS_V35:return False
 if 'admin' in roles:return True
 own=c is None
 if own:c=conn()
 try:
  # Every role with the requested capability must be enabled (least privilege
  # for combined roles); user deny always wins. User allow can restore role
  # default, but cannot bypass an explicit role deny.
  placeholders=','.join('?' for _ in roles)
  if not roles:return False
  records=c.execute('SELECT subject_type,subject_id,enabled FROM permission_overrides_v35 '
   'WHERE permission=? AND ((subject_type=\'user\' AND subject_id=?) OR '
   '(subject_type=\'role\' AND subject_id IN ('+placeholders+')))',
   (permission,str(uid),*sorted(roles))).fetchall()
  for record in records:
   if not record['enabled']:return False
  return True
 finally:
  if own:c.close()

@app.before_request
def v35_permission_guard():
 permission=v35_permission_for_request()
 if permission is None:return
 if not session.get('uid'):return  # existing login_required handles anonymous users
 if not v35_permission_allowed(session['uid'],current_roles(),permission):abort(403)

@app.route('/admin/permissions',methods=['GET','POST'])
@allowed('admin')
def permissions_v35():
 if request.method=='POST':
  try:
   subject_type=request.form.get('subject_type','')
   subject_id=request.form.get('subject_id','')
   permission=request.form.get('permission','')
   state=request.form.get('state','')
   if subject_type not in ('role','user') or permission not in PERMISSIONS_V35 or state not in ('inherit','allow','deny'):
    abort(400)
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    if subject_type=='role':
     if subject_id not in ROLES or subject_id=='admin':abort(400)
    else:
     if not subject_id.isdecimal() or not c.execute('SELECT 1 FROM users WHERE id=?',(int(subject_id),)).fetchone():abort(400)
    if state=='inherit':
     c.execute('DELETE FROM permission_overrides_v35 WHERE subject_type=? AND subject_id=? AND permission=?',
       (subject_type,subject_id,permission))
    else:
     c.execute('INSERT INTO permission_overrides_v35(subject_type,subject_id,permission,enabled,updated_at,updated_by) '
       'VALUES(?,?,?,?,?,?) ON CONFLICT(subject_type,subject_id,permission) DO UPDATE SET '
       'enabled=excluded.enabled,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
       (subject_type,subject_id,permission,int(state=='allow'),now(),session['uid']))
    audit(c,'permission.update',f'{subject_type}:{subject_id} {permission}={state}')
   flash('Настройка права сохранена')
  except Exception as exc:
   if isinstance(exc,ValueError):return fail(exc)
   raise
  return redirect('/admin/permissions')
 with conn() as c:
  users=c.execute('SELECT id,username FROM users ORDER BY username').fetchall()
  overrides=c.execute('SELECT * FROM permission_overrides_v35 ORDER BY subject_type,subject_id,permission').fetchall()
 return page('Матрица прав доступа','''<div class="card">
 <p>Ограничения к существующим ролям: склад, ТСД, производство, снабжение, ОТК и отгрузка. Запрет имеет приоритет при совмещении ролей. Разрешение не обходит проверку роли на сервере.</p>
 <form method="post"><input type="hidden" name="_csrf" value="{{csrf_token}}">
 <label>Тип назначения<select name="subject_type"><option value="role">Роль</option><option value="user">Сотрудник</option></select></label>
 <label>Роль / ID сотрудника<input name="subject_id" required placeholder="economist или 5"></label>
 <label>Действие<select name="permission">{% for k,v in permissions.items() %}<option value="{{k}}">{{v}}</option>{% endfor %}</select></label>
 <label>Правило<select name="state"><option value="deny">Запретить</option><option value="inherit">По умолчанию</option><option value="allow">Разрешить (не выше роли)</option></select></label>
 <button>Сохранить</button></form>
 <p class="muted">Роли: {% for k,v in roles.items() %}{{k}} — {{v}}; {% endfor %}</p>
 <p class="muted">Сотрудники: {% for u in users %}{{u.id}} — {{u.username}}; {% endfor %}</p></div>
 <div class="card"><h2>Настроенные исключения</h2><table><tr><th>Объект</th><th>Право</th><th>Режим</th><th>Изменено</th></tr>
 {% for x in overrides %}<tr><td>{{x.subject_type}}: {{x.subject_id}}</td><td>{{permissions[x.permission]}}</td>
 <td>{{'Разрешено' if x.enabled else 'Запрещено'}}</td><td>{{x.updated_at}}</td></tr>{% endfor %}</table></div>''',
 permissions=PERMISSIONS_V35,users=users,overrides=overrides)


# v40 — read-only global search. Never search across entities unavailable to the caller.
SEARCH_V40 = (
 ('Продукция','/catalog','catalog', "SELECT id,name AS title,unit AS subtitle FROM products WHERE name LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?"),
 ('Сырьё','/catalog','catalog', "SELECT id,name AS title,unit AS subtitle FROM materials WHERE name LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?"),
 ('Партии','/lots','lots', "SELECT l.id,l.lot_code AS title,COALESCE(p.name,m.name,'') AS subtitle FROM lots l LEFT JOIN products p ON p.id=l.product_id LEFT JOIN materials m ON m.id=l.material_id WHERE l.lot_code LIKE ? ESCAPE '\\' OR COALESCE(p.name,m.name,'') LIKE ? ESCAPE '\\' ORDER BY l.id DESC LIMIT ?"),
 ('Производственные задания','/work-orders','work_orders', "SELECT w.id,w.lot_code AS title,p.name AS subtitle FROM work_orders w JOIN recipes r ON r.id=w.recipe_id JOIN products p ON p.id=r.product_id WHERE w.lot_code LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\' ORDER BY w.id DESC LIMIT ?"),
 ('Задания на отгрузку','/dispatch/v32','dispatch', "SELECT d.id,d.customer AS title,COALESCE(d.vehicle,'') AS subtitle FROM dispatch_tasks_v31 d WHERE d.customer LIKE ? ESCAPE '\\' OR d.vehicle LIKE ? ESCAPE '\\' OR CAST(d.id AS TEXT) LIKE ? ESCAPE '\\' ORDER BY d.id DESC LIMIT ?"),
 ('Тара / QR','/containers','containers', "SELECT c.id,c.code AS title,l.lot_code AS subtitle FROM containers c JOIN lots l ON l.id=c.lot_id WHERE c.code LIKE ? ESCAPE '\\' OR l.lot_code LIKE ? ESCAPE '\\' ORDER BY c.id DESC LIMIT ?"),
 ('Отгрузки','/shipments','shipments', "SELECT s.id,s.customer AS title,l.lot_code AS subtitle FROM shipments s JOIN lots l ON l.id=s.lot_id WHERE s.customer LIKE ? ESCAPE '\\' OR l.lot_code LIKE ? ESCAPE '\\' ORDER BY s.id DESC LIMIT ?"),
)

# Independent explicit mapping for search authorization, including hidden-menu items.
SEARCH_ACCESS_V40 = {
 'catalog': ('technologist','warehouse','quality','economist'),
 'lots': ('warehouse','quality','technologist'),
 'work_orders': ('technologist','operator'),
 'dispatch': ('economist','warehouse'),
 'containers': ('warehouse',),
 'shipments': ('economist','warehouse'),
}
SEARCH_PERMISSION_V40 = {
 'lots':'warehouse.view', 'work_orders':'production.view',
 'dispatch':'dispatch.view', 'containers':'warehouse.view',
 'shipments':'warehouse.view',
}

def search_allowed_v40(key,roles,uid):
 if 'admin' in roles:return True
 if not roles.intersection(SEARCH_ACCESS_V40[key]):return False
 permission=SEARCH_PERMISSION_V40.get(key)
 return not permission or v35_permission_allowed(uid,roles,permission)

def search_like_v40(q):
 return '%' + q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'

@app.get('/search')
@login_required
def global_search_v40():
 q=request.args.get('q','').strip()[:100]
 roles=current_roles()
 groups=[]
 if len(q)>=2:
  pattern=search_like_v40(q)
  with conn() as c:
   for label,path,key,sql in SEARCH_V40:
    if not search_allowed_v40(key,roles,session['uid']):continue
    # Each SQL query is fixed, with bound parameters and a per-category cap.
    count=sql.count('LIKE ?')
    rows=c.execute(sql,(*([pattern]*count),12)).fetchall()
    if rows:groups.append((label,path,rows))
 return page('Глобальный поиск','''<div class="card"><h2>Поиск по lovetech</h2>
 <form action="/search" method="get" class="global-search-form"><label>Название, партия, QR, клиент или номер задания<input name="q" value="{{q}}" maxlength="100" minlength="2" required autofocus placeholder="Например: ГП-2026-014"></label><button>Искать</button></form>
 <p class="muted">Результаты ограничены вашими ролями и запретами в матрице прав. Не менее двух символов, до 12 результатов на категорию.</p></div>
 {% if q|length>=2 %}{% for label,path,rows in groups %}<section class="card"><h2>{{label}} <span class="muted">({{rows|length}})</span></h2><table><tr><th>Совпадение</th><th>Дополнительно</th><th>Открыть раздел</th></tr>
 {% for r in rows %}<tr><td><b>{{r.title}}</b></td><td>{{r.subtitle}}</td><td><a href="{{path}}">Перейти →</a></td></tr>{% endfor %}</table></section>
 {% else %}<div class="card"><p>Совпадений в доступных вам разделах не найдено.</p></div>{% endfor %}
 {% endif %}''',q=q,groups=groups)



# v39: Separate, auditable economist sign-off for the photo report.
# Shipment and stock movement are deliberately NOT repeated or reversed by this workflow.
@app.route('/dispatch/v39/<int:tid>',methods=['GET','POST'])
@allowed('admin','economist','warehouse','viewer')
def dispatch_v39_review(tid):
    if request.method=='POST':
        action=request.form.get('action','')
        if action not in ('submit','approve','request_changes'):abort(400)
        if action=='submit' and not has_role('admin','warehouse'):abort(403)
        if action in ('approve','request_changes') and not has_role('admin','economist'):abort(403)
        comment=request.form.get('comment','').strip()
        if len(comment)>1500:return fail(ValueError('Комментарий слишком длинный'))
        if action=='request_changes' and len(comment)<5:return fail(ValueError('Укажите причину возврата на исправление (не менее 5 символов)'))
        try:
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                task=c.execute('SELECT id,status,completed_by FROM dispatch_tasks_v31 WHERE id=?',(tid,)).fetchone()
                receipt=c.execute('SELECT task_id FROM dispatch_receipts_v32 WHERE task_id=?',(tid,)).fetchone()
                if not task or task['status']!='completed' or not receipt:
                    raise ValueError('Сначала закройте многопозиционную отгрузку v32')
                review=c.execute('SELECT * FROM dispatch_reviews_v39 WHERE task_id=?',(tid,)).fetchone()
                if action=='submit':
                    if review and review['status']!='changes_requested':raise ValueError('Отчёт уже направлен на проверку или утверждён')
                    photos=c.execute('SELECT id,created_at FROM dispatch_photos_v31 WHERE task_id=? ORDER BY id',(tid,)).fetchall()
                    if not photos:raise ValueError('Необходимо хотя бы одно фото')
                    if review and not c.execute('SELECT 1 FROM dispatch_photos_v31 WHERE task_id=? AND created_at>?',(tid,review['reviewed_at'])).fetchone():
                        raise ValueError('Добавьте новое фото после замечаний экономиста')
                    revision=review['revision']+1 if review else 1
                    c.execute('INSERT INTO dispatch_reviews_v39(task_id,status,submitted_at,submitted_by,reviewed_at,reviewed_by,comment,revision) VALUES(?,?,?,?,NULL,NULL,?,?) ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,submitted_at=excluded.submitted_at,submitted_by=excluded.submitted_by,reviewed_at=NULL,reviewed_by=NULL,comment=excluded.comment,revision=excluded.revision',(tid,'pending',now(),session['uid'],comment,revision))
                else:
                    if not review or review['status']!='pending':raise ValueError('Нет отчёта, ожидающего решения')
                    if session['uid']==review['submitted_by'] or session['uid']==task['completed_by']:
                        raise ValueError('Нельзя проверять собственный фотоотчёт или собственное закрытие отгрузки')
                    revision=review['revision']
                    status='approved' if action=='approve' else 'changes_requested'
                    c.execute('UPDATE dispatch_reviews_v39 SET status=?,reviewed_at=?,reviewed_by=?,comment=? WHERE task_id=?',(status,now(),session['uid'],comment,tid))
                c.execute('INSERT INTO dispatch_review_events_v39(task_id,action,actor_id,created_at,comment,revision) VALUES(?,?,?,?,?,?)',(tid,action,session['uid'],now(),comment,revision))
                audit(c,'dispatch.v39.'+action,'task='+str(tid)+'; revision='+str(revision))
            flash('Решение сохранено');return redirect('/dispatch/v39/'+str(tid))
        except Exception as e:return fail(e)
    with conn() as c:
        task=c.execute('SELECT * FROM dispatch_tasks_v31 WHERE id=?',(tid,)).fetchone()
        if not task:abort(404)
        receipt=c.execute('SELECT task_id FROM dispatch_receipts_v32 WHERE task_id=?',(tid,)).fetchone()
        review=c.execute('SELECT * FROM dispatch_reviews_v39 WHERE task_id=?',(tid,)).fetchone()
        photos=c.execute('SELECT id,caption,created_at FROM dispatch_photos_v31 WHERE task_id=? ORDER BY id',(tid,)).fetchall()
        events=c.execute('SELECT e.*,u.username FROM dispatch_review_events_v39 e JOIN users u ON u.id=e.actor_id WHERE e.task_id=? ORDER BY e.id DESC',(tid,)).fetchall()
    return page('Проверка фотоотчёта №'+str(tid),"""
    <div class='card'><h2>Фотоотчёт по машине №{{t.id}}</h2>
    <p><b>Клиент:</b> {{t.customer}} · <b>Автомобиль:</b> {{t.vehicle}} · <b>Водитель:</b> {{t.driver}}</p>
    <p><b>Отгрузка:</b> {{t.status}} · <b>Проверка:</b> {{r.status if r else 'не направлен'}}{% if r %} · версия {{r.revision}}{% endif %}</p>
    {% if not receipt %}<p>Для передачи на проверку сначала завершите задание v32.</p>{% endif %}
    <p><a href='/dispatch/v32/{{t.id}}'>← Задание</a> · <a href='/dispatch/v32/{{t.id}}/report'>Печатный отчёт</a></p></div>
    <div class='card'><h3>Фото загруженной машины</h3><div class='grid'>
    {% for ph in photos %}<div><a href='/dispatch/photos/{{ph.id}}' target='_blank' rel='noopener'><img src='/dispatch/photos/{{ph.id}}' loading='lazy' style='width:100%;max-height:270px;object-fit:contain'></a><p>{{ph.caption}} · {{ph.created_at}}</p></div>{% endfor %}
    </div>{% if not photos %}<p>Фото пока нет.</p>{% endif %}</div>
    {% if receipt and has_role('admin','warehouse') and (not r or r.status=='changes_requested') %}
    <div class='card'><h3>Передать фотоотчёт экономисту</h3>
    {% if r and r.status=='changes_requested' %}<p class='danger'>Замечание: {{r.comment}}. Добавьте новое фото через <a href='/dispatch/v32/{{t.id}}'>карточку задания</a> перед повторной отправкой.</p>{% endif %}
    <form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='submit'>
    <label>Комментарий кладовщика<textarea name='comment' maxlength='1500'></textarea></label><button>Отправить на проверку</button></form></div>{% endif %}
    {% if r and r.status=='pending' and has_role('admin','economist') and user_id!=r.submitted_by and user_id!=t.completed_by %}
    <div class='card'><h3>Решение экономиста</h3><form method='post'>
    <input type='hidden' name='_csrf' value='{{csrf_token}}'><label>Комментарий / причина замечаний<textarea name='comment' maxlength='1500'></textarea></label>
    <button name='action' value='approve'>Подтвердить фотоотчёт</button>
    <button name='action' value='request_changes' style='background:#b91c1c'>Вернуть на исправление</button></form></div>{% endif %}
    <div class='card'><h3>История проверки</h3><table><tr><th>Когда</th><th>Сотрудник</th><th>Действие</th><th>Версия</th><th>Комментарий</th></tr>
    {% for e in events %}<tr><td>{{e.created_at}}</td><td>{{e.username}}</td><td>{{e.action}}</td><td>{{e.revision}}</td><td>{{e.comment}}</td></tr>{% endfor %}</table></div>
    """,t=task,r=review,receipt=receipt,photos=photos,events=events,user_id=session['uid'])



# v43: portrait A4 printout of a recipe revision; read-only, no schema changes.
@app.get('/recipe/<int:rid>/print')
@login_required
def recipe_print_v43(rid):
 if not has_role('technologist','operator','quality','viewer'):
  abort(403)
 with conn() as c:
  recipe=c.execute('SELECT r.*,p.name AS product,p.unit AS product_unit FROM recipes r JOIN products p ON p.id=r.product_id WHERE r.id=?',(rid,)).fetchone()
  if recipe is None:abort(404)
  items=[dict(x) for x in c.execute('SELECT i.percent,m.name,m.unit FROM recipe_items i JOIN materials m ON m.id=i.material_id WHERE i.recipe_id=? ORDER BY i.id',(rid,)).fetchall()]
  steps=[]
  for st in c.execute('SELECT id,position,instruction,control_point FROM steps WHERE recipe_id=? ORDER BY position,id',(rid,)).fetchall():
   row=dict(st)
   row['parameters']=[dict(x) for x in c.execute('SELECT name,unit,min_value,max_value,required,method FROM tech_parameters WHERE step_id=? ORDER BY id',(st['id'],)).fetchall()]
   steps.append(row)
 # Print-only mass illustration. Percentages are the approved formula; no batch quantity is implied.
 total=sum(x['percent'] for x in items)
 from pathlib import Path
 template=Path(__file__).with_name('recipe_print_v43.html').read_text(encoding='utf-8')
 response=app.make_response(render_template_string(template,recipe=dict(recipe),items=items,steps=steps,total=total,printed_at=now()))
 response.headers['Cache-Control']='private, no-store'
 response.headers['X-Content-Type-Options']='nosniff'
 return response



# v44: production execution control — read-only overview; no automatic stock movements.
def v44_execution_status(c, sheet_id):
 sheet=c.execute("SELECT * FROM production_sheets WHERE id=?",(sheet_id,)).fetchone()
 if sheet is None:raise ValueError('Производственный лист не найден')
 snap=json.loads(sheet['snapshot_json'])
 required={p['id']:p for st in snap.get('steps',[]) for p in st.get('parameters',[]) if p.get('required')}
 latest={r['parameter_id']:r for r in c.execute('SELECT r.* FROM sheet_readings r JOIN (SELECT parameter_id,MAX(id) id FROM sheet_readings WHERE sheet_id=? GROUP BY parameter_id) z ON z.id=r.id',(sheet_id,)).fetchall()}
 missing=[p['name'] for pid,p in required.items() if pid not in latest]
 unresolved=c.execute("SELECT COUNT(*) FROM sheet_readings r WHERE r.sheet_id=? AND r.out_of_range=1 AND r.disposition!='accepted' AND NOT EXISTS (SELECT 1 FROM sheet_corrections x WHERE x.old_reading_id=r.id)",(sheet_id,)).fetchone()[0]
 paper=c.execute('SELECT copy_number,verified_at FROM sheet_paper_register WHERE sheet_id=?',(sheet_id,)).fetchone()
 attachments=c.execute('SELECT COUNT(*) FROM sheet_attachments WHERE sheet_id=?',(sheet_id,)).fetchone()[0]
 reconciliation=v24_reconcile_order(c,sheet['order_id'])
 issues=[]
 if missing:issues.append('Не заполнены обязательные измерения: '+', '.join(missing[:8]))
 if unresolved:issues.append('Не рассмотрены отклонения: '+str(unresolved))
 if paper is None:issues.append('Не зарегистрирован бумажный оригинал')
 if sheet['status']=='issued':issues.append('Лист ещё не передан на проверку')
 if reconciliation['issue_count']:issues.append('Расхождения по загрузке сырья: '+str(reconciliation['issue_count'])+' (информационно)')
 return dict(sheet=sheet,snap=snap,required=len(required),measured=len(required)-len(missing),missing=missing,unresolved=unresolved,paper=paper,attachments=attachments,reconciliation=reconciliation,issues=issues)

@app.get('/production/execution')
@allowed('admin','technologist','operator','quality','warehouse','viewer')
def v44_execution_overview():
 with conn() as c:
  rows=c.execute("SELECT s.id,s.status,w.lot_code,w.id order_id,s.issued_at FROM production_sheets s JOIN work_orders w ON w.id=s.order_id ORDER BY s.id DESC LIMIT 150").fetchall()
  summaries=[]
  for r in rows:
   data=v44_execution_status(c,r['id'])
   summaries.append(dict(id=r['id'],order_id=r['order_id'],lot_code=r['lot_code'],status=r['status'],measured=data['measured'],required=data['required'],unresolved=data['unresolved'],load_issues=data['reconciliation']['issue_count'],paper=bool(data['paper'])))
 return page('Контроль производства',"""
 <div class='card'><h2>Контроль выполнения производственных заданий</h2><p class='muted'>Сводка по бумажным листам, фактическим измерениям и загрузке сырья. Сверка загрузки информационная и не изменяет остатки.</p>
 <table><tr><th>Лист / партия</th><th>Статус</th><th>Измерения</th><th>Отклонения</th><th>Сырьё</th><th>Бумага</th><th>Открыть</th></tr>
 {% for r in rows %}<tr><td>Лист №{{r.id}} · {{r.lot_code}}</td><td>{{r.status}}</td><td>{{r.measured}} / {{r.required}}</td><td>{{r.unresolved}}</td><td>{{r.load_issues}} замеч.</td><td>{{'Есть' if r.paper else 'Нет'}}</td><td><a href='/production/execution/{{r.id}}'>Контроль</a></td></tr>{% else %}<tr><td colspan='7'>Производственных листов пока нет</td></tr>{% endfor %}</table></div>
 """,rows=summaries)

@app.get('/production/execution/<int:sheet_id>')
@allowed('admin','technologist','operator','quality','warehouse','viewer')
def v44_execution_detail(sheet_id):
 with conn() as c:
  try:data=v44_execution_status(c,sheet_id)
  except ValueError:abort(404)
 return page('Контроль партии',"""
 <div class='card'><h2>Контроль партии {{d.snap.lot_code}}</h2><p>Лист №{{d.sheet.id}} · ПЗ-{{d.sheet.order_id}} · {{d.snap.product}} · Статус: <b>{{d.sheet.status}}</b></p>
 <p>Обязательные измерения: <b>{{d.measured}} / {{d.required}}</b> · Неразрешённые отклонения: <b>{{d.unresolved}}</b> · Бумажный оригинал: <b>{{'зарегистрирован' if d.paper else 'отсутствует'}}</b> · Файлов: {{d.attachments}}</p>
 <p><a href='/production/sheets/{{d.sheet.id}}'>Открыть производственный лист</a> · <a href='/production/sheets/{{d.sheet.id}}/print' target='_blank'>Печать A4</a> · <a href='/production/load-reconciliation?order_id={{d.sheet.order_id}}'>Подробная сверка загрузки</a></p></div>
 <div class='card'><h3>Контрольные замечания</h3>{% for x in d.issues %}<p>⚠ {{x}}</p>{% else %}<p>По проверяемым признакам замечаний нет.</p>{% endfor %}<p class='muted'>Это обзор, а не разрешение ОТК или автоматическое закрытие производственного задания. Расхождения загрузки могут требовать документального объяснения.</p></div>
 <div class='card'><h3>Норма / выдано / фактически загружено</h3><table><tr><th>Сырьё</th><th>Норма</th><th>Выдано</th><th>Загружено</th><th>Разница</th></tr>{% for r in d.reconciliation.rows %}<tr><td>{{r.name}}</td><td>{{r.norm}} {{r.unit}}</td><td>{{r.issued}}</td><td>{{r.actual}}</td><td>{{r.delta_norm}}</td></tr>{% endfor %}</table></div>
 """,d=data)
# v45: CT48 physical container inventory; observation-only (no stock writes).
def v45_inventory_rows(c,sid):
 return c.execute("""SELECT ct.code,l.lot_code,ct.qty AS current_qty,
  ic.expected_qty,ic.actual_qty,ic.actual_qty-ic.expected_qty AS difference,
  ic.location,ic.counted_at,ic.note,u.username
  FROM inventory_counts_v45 ic JOIN containers ct ON ct.id=ic.container_id
  JOIN lots l ON l.id=ct.lot_id JOIN users u ON u.id=ic.counted_by
  WHERE ic.session_id=? ORDER BY ct.code""",(sid,)).fetchall()

def v45_inventory_get(c,sid):
 row=c.execute('SELECT * FROM inventory_sessions_v45 WHERE id=?',(sid,)).fetchone()
 if row is None:abort(404)
 return row

@app.route('/inventory/v45',methods=['GET','POST'])
@allowed('admin','warehouse','technologist','quality','viewer')
def v45_inventory_list():
 if request.method=='POST':
  if not has_role('admin','warehouse'):abort(403)
  title=request.form.get('title','').strip()[:120]
  location=request.form.get('location','').strip()[:120]
  if not title:return fail(ValueError('Укажите название инвентаризации'))
  with conn() as c:
   c.execute('BEGIN IMMEDIATE')
   cur=c.execute('INSERT INTO inventory_sessions_v45(title,location,created_at,created_by) VALUES(?,?,?,?)',(title,location,now(),session['uid']))
   sid=cur.lastrowid
   audit(c,'inventory.v45.create',f'id={sid}')
  return redirect(f'/inventory/v45/{sid}')
 with conn() as c:
  rows=c.execute('''SELECT s.*,u.username,(SELECT COUNT(*) FROM inventory_counts_v45 x WHERE x.session_id=s.id) scanned,
   (SELECT COUNT(*) FROM inventory_counts_v45 x WHERE x.session_id=s.id AND ABS(x.actual_qty-x.expected_qty)>0.000001) mismatched
   FROM inventory_sessions_v45 s JOIN users u ON u.id=s.created_by ORDER BY s.id DESC LIMIT 150''').fetchall()
 return page('Инвентаризация CT48',"""
 <div class='card'><h2>Инвентаризация тары</h2><p class='muted'>Сканирование QR фиксирует фактическое количество. Остатки партий и тары не корректируются автоматически. Неотсканированная тара не считается отсутствующей без отдельной проверки.</p>
 {% if has_role('admin','warehouse') %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><label>Название<input name='title' maxlength='120' required placeholder='Инвентаризация склада'></label><label>Зона / место<input name='location' maxlength='120' placeholder='Основной склад'></label><button>Создать инвентаризацию</button></form>{% endif %}</div>
 <div class='card'><h2>Сессии</h2><table><tr><th>№</th><th>Название</th><th>Место</th><th>Статус</th><th>Просканировано</th><th>Расхождения</th><th>Открыть</th></tr>{% for r in rows %}<tr><td>{{r.id}}</td><td>{{r.title}}</td><td>{{r.location}}</td><td>{{r.status}}</td><td>{{r.scanned}}</td><td>{{r.mismatched}}</td><td><a href='/inventory/v45/{{r.id}}'>Открыть</a></td></tr>{% else %}<tr><td colspan='7'>Нет инвентаризаций</td></tr>{% endfor %}</table></div>""",rows=rows)

@app.route('/inventory/v45/<int:sid>',methods=['GET','POST'])
@allowed('admin','warehouse','technologist','quality','viewer')
def v45_inventory_detail(sid):
 if request.method=='POST':
  if not has_role('admin','warehouse'):abort(403)
  action=request.form.get('action','scan')
  try:
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    inv=v45_inventory_get(c,sid)
    if action=='scan':
     if inv['status']!='open':raise ValueError('Сессия уже отправлена на проверку')
     code=parse_container_scan(request.form.get('code',''))
     container=c.execute('''SELECT ct.*,COALESCE(cl.location,'Основной склад') AS location FROM containers ct
       LEFT JOIN container_locations cl ON cl.container_id=ct.id WHERE ct.code=?''',(code,)).fetchone()
     if not container:raise ValueError('QR-код тары не найден')
     if not container['active']:raise ValueError('Тара неактивна')
     location=container['location']
     if inv['location'] and location.casefold()!=inv['location'].casefold():
      raise ValueError('Тара относится к другой зоне: '+location)
     raw=request.form.get('actual_qty','').strip().replace(',','.')
     qty=float(raw)
     if not math.isfinite(qty) or qty<0 or qty>1e12:raise ValueError('Фактическое количество должно быть от 0 до 1 000 000 000 000')
     note=request.form.get('note','').strip()[:300]
     # A repeated scan updates the same container instead of duplicating it.
     existing=c.execute('SELECT id FROM inventory_counts_v45 WHERE session_id=? AND container_id=?',(sid,container['id'])).fetchone()
     if existing:
      c.execute('UPDATE inventory_counts_v45 SET actual_qty=?,location=?,counted_at=?,counted_by=?,note=? WHERE id=?',(qty,location,now(),session['uid'],note,existing['id']))
     else:
      c.execute('''INSERT INTO inventory_counts_v45(session_id,container_id,expected_qty,actual_qty,location,counted_at,counted_by,note)
       VALUES(?,?,?,?,?,?,?,?)''',(sid,container['id'],container['qty'],qty,location,now(),session['uid'],note))
     audit(c,'inventory.v45.count',f'session={sid};container={container["id"]};actual={qty}')
    elif action=='submit':
     if inv['status']!='open':raise ValueError('Неверный статус')
     if not c.execute('SELECT 1 FROM inventory_counts_v45 WHERE session_id=?',(sid,)).fetchone():raise ValueError('Нет просканированной тары')
     c.execute("UPDATE inventory_sessions_v45 SET status='review',submitted_at=? WHERE id=?",(now(),sid))
     audit(c,'inventory.v45.submit',f'session={sid}')
    elif action=='reopen':
     if not has_role('admin'):abort(403)
     if inv['status']!='review':raise ValueError('Неверный статус')
     c.execute("UPDATE inventory_sessions_v45 SET status='open',submitted_at=NULL WHERE id=?",(sid,))
     audit(c,'inventory.v45.reopen',f'session={sid}')
    elif action=='close':
     if not has_role('admin'):abort(403)
     if inv['status']!='review':raise ValueError('Сначала передайте ведомость на проверку')
     c.execute("UPDATE inventory_sessions_v45 SET status='closed',closed_at=?,closed_by=? WHERE id=?",(now(),session['uid'],sid))
     audit(c,'inventory.v45.close',f'session={sid};without_stock_adjustment')
    else:raise ValueError('Неизвестное действие')
  except ValueError as e:return fail(e)
  return redirect(f'/inventory/v45/{sid}')
 with conn() as c:
  inv=v45_inventory_get(c,sid)
  rows=v45_inventory_rows(c,sid)
 return page('Инвентаризация №'+str(sid),"""
 <div class='card'><h2>Инвентаризация №{{inv.id}} · {{inv.title}}</h2><p>Зона: {{inv.location or 'Все зоны'}} · Статус: <b>{{inv.status}}</b> · Создана: {{inv.created_at}}</p>
 <p><a href='/inventory/v45'>← Все инвентаризации</a> · <a href='/inventory/v45/{{inv.id}}/print' target='_blank'>Печатная ведомость A4</a></p>
 <p class='muted'>Сверка носит информационный характер: подтверждение не меняет остатки. При проверке учитывайте тару, которая не была просканирована.</p></div>
 {% if inv.status=='open' and has_role('admin','warehouse') %}<div class='card'><h3>Сканирование на Urovo CT48</h3><form method='post' id='inventoryScanForm'>
 <input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='scan'>
 <label>QR-код тары<input id='v45scan' name='code' autocomplete='off' autofocus required placeholder='CHEMPRO:CONTAINER:...' onkeydown="if(event.key==='Enter'){event.preventDefault();this.form.elements['actual_qty'].focus()}"></label>
 <label>Фактически, кг/ед. <input type='number' name='actual_qty' min='0' step='any' required></label>
 <label>Комментарий<input name='note' maxlength='300'></label><button>Сохранить подсчёт</button></form>
 <p class='muted'>ScanWedge: ввод как с клавиатуры + Enter. При сканировании код вводится в поле QR; количество укажите перед сохранением.</p></div>{% endif %}
 <div class='card'><h3>Фактически подсчитанная тара ({{rows|length}})</h3><table><tr><th>QR</th><th>Партия</th><th>Учёт на момент первого сканирования</th><th>Факт</th><th>Разница</th><th>Место</th><th>Кто / когда</th><th>Примечание</th></tr>
 {% for r in rows %}<tr><td>{{r.code}}</td><td>{{r.lot_code}}</td><td>{{r.expected_qty}}</td><td>{{r.actual_qty}}</td><td>{{'%.3f'|format(r.difference)}}</td><td>{{r.location}}</td><td>{{r.username}} · {{r.counted_at}}</td><td>{{r.note}}</td></tr>{% else %}<tr><td colspan='8'>Нет сканирований</td></tr>{% endfor %}</table></div>
 {% if inv.status=='open' and has_role('admin','warehouse') %}<div class='card'><form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><button name='action' value='submit'>Передать ведомость на проверку</button></form></div>{% endif %}
 {% if inv.status=='review' and has_role('admin') %}<div class='card'><form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><button name='action' value='reopen'>Вернуть на подсчёт</button><button name='action' value='close'>Закрыть ведомость без корректировки остатков</button></form></div>{% endif %}
 """,inv=inv,rows=rows)

@app.get('/inventory/v45/<int:sid>/print')
@allowed('admin','warehouse','technologist','quality','viewer')
def v45_inventory_print(sid):
 with conn() as c:
  inv=v45_inventory_get(c,sid)
  rows=v45_inventory_rows(c,sid)
 return render_template_string("""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>lovetech · Инвентаризация №{{inv.id}}</title>
 <style>@page{size:A4 portrait;margin:12mm 10mm 13mm}body{font:11px Arial,sans-serif;color:#263044}h1{font-size:19px}h2{font-size:13px;color:#bf1830}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{border:1px solid #cdd3db;padding:6px 4px;word-wrap:break-word}th{background:#f1f2f4}thead{display:table-header-group}tr{break-inside:avoid}.muted{color:#687589}.brand{color:#c90020;font-weight:bold}button{padding:8px}@media print{button{display:none}}</style></head><body>
 <button onclick="window.print()">Печать A4</button><p class="brand">lovetech</p><h1>Инвентаризационная ведомость №{{inv.id}}</h1>
 <p>Название: {{inv.title}} · Зона: {{inv.location or 'Все зоны'}} · Статус: {{inv.status}} · Дата: {{inv.created_at}}</p>
 <table><thead><tr><th>QR-код</th><th>Партия</th><th>Учёт</th><th>Факт</th><th>Разница</th><th>Место</th></tr></thead><tbody>{% for r in rows %}<tr><td>{{r.code}}</td><td>{{r.lot_code}}</td><td>{{r.expected_qty}}</td><td>{{r.actual_qty}}</td><td>{{'%.3f'|format(r.difference)}}</td><td>{{r.location}}</td></tr>{% endfor %}</tbody></table>
 <p class="muted">Учётные количества зафиксированы при первом сканировании. Неотсканированная тара не включена в ведомость. Закрытие ведомости не меняет остатки.</p>
 <p>Подсчёт: _____________________ &nbsp; Проверил: _____________________ &nbsp; Дата: ______________</p></body></html>""",inv=inv,rows=rows)


# v47: costing. Values are explicitly entered by the economist, not inferred purchase prices.
def cost_v47_calculate(items, planned_qty, actual_qty, packaging, overhead):
 from decimal import Decimal, ROUND_HALF_UP
 D=lambda x:Decimal(str(x))
 if D(planned_qty)<=0 or D(actual_qty)<=0:raise ValueError('Количество должно быть положительным')
 if any(not D(x).is_finite() or D(x)<0 for x in (planned_qty,actual_qty,packaging,overhead)):raise ValueError('Некорректные количества или затраты')
 planned=D('0');actual=D('0');complete=bool(items);rows=[]
 for item in items:
  qty=D(item['qty']);norm=D(item['norm_qty']);price=item['price']
  if price is None:complete=False
  line={'material':item['material'],'unit':item['unit'],'qty':str(qty),'norm_qty':str(norm),'price':str(price) if price is not None else None}
  if price is not None:
   line['planned_cost']=str(norm*D(price));line['actual_cost']=str(qty*D(price))
   planned+=norm*D(price);actual+=qty*D(price)
  rows.append(line)
 result={'lines':rows,'complete':complete,'planned_material_cost':str(planned) if complete else None,
  'actual_material_cost':str(actual) if complete else None,'packaging_cost':str(D(packaging)),
  'overhead_cost':str(D(overhead)),'planned_qty':str(D(planned_qty)),'actual_qty':str(D(actual_qty))}
 if complete:
  total=actual+D(packaging)+D(overhead)
  result.update(total_cost=str(total),cost_per_unit=str((total/D(actual_qty)).quantize(D('0.01'),rounding=ROUND_HALF_UP)),
   material_deviation=str(actual-planned))
 return result

def cost_v47_rows(c,batch):
 recipe=c.execute('SELECT material_id,percent FROM recipe_items WHERE recipe_id=?',(batch['recipe_id'],)).fetchall()
 norms={r['material_id']:float(batch['planned_qty'])*float(r['percent'])/100 for r in recipe}
 actual=c.execute('''SELECT l.material_id,SUM(x.qty) qty FROM consumptions x
 JOIN lots l ON l.id=x.raw_lot_id WHERE x.batch_id=? GROUP BY l.material_id''',(batch['id'],)).fetchall()
 used={r['material_id']:float(r['qty']) for r in actual}
 ids=set(norms)|set(used)
 out=[]
 for mid in sorted(ids):
  r=c.execute('''SELECT m.name,m.unit,p.price_per_unit FROM materials m LEFT JOIN cost_prices_v47 p
   ON p.material_id=m.id WHERE m.id=?''',(mid,)).fetchone()
  if r:out.append(dict(material=r['name'],unit=r['unit'],qty=used.get(mid,0),norm_qty=norms.get(mid,0),price=r['price_per_unit']))
 return out

@app.route('/economics/costs',methods=['GET','POST'])
@allowed('admin','economist','technologist')
def costs_v47():
 if request.method=='POST':
  if not has_role('admin','economist'):abort(403)
  try:
   from decimal import Decimal,InvalidOperation
   action=request.form.get('action')
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    if action=='price':
     mid=int(request.form['material_id']);price=Decimal(request.form['price'])
     if not price.is_finite() or price<0 or price>1000000000:raise ValueError('Некорректная цена')
     if not c.execute('SELECT 1 FROM materials WHERE id=?',(mid,)).fetchone():raise ValueError('Сырьё не найдено')
     c.execute('''INSERT INTO cost_prices_v47(material_id,price_per_unit,currency,updated_at,updated_by)
      VALUES(?,?,?,?,?) ON CONFLICT(material_id) DO UPDATE SET price_per_unit=excluded.price_per_unit,
      updated_at=excluded.updated_at,updated_by=excluded.updated_by''',(mid,float(price),'RUB',now(),session['uid']))
     audit(c,'cost.v47.price',f'material={mid}; price={price}')
    elif action=='snapshot':
     bid=int(request.form['batch_id']);batch=c.execute('SELECT * FROM batches WHERE id=?',(bid,)).fetchone()
     if not batch:raise ValueError('Партия выпуска не найдена')
     if c.execute('SELECT 1 FROM batch_costs_v47 WHERE batch_id=?',(bid,)).fetchone():raise ValueError('Расчёт уже зафиксирован')
     pack=Decimal(request.form.get('packaging_cost','0'));over=Decimal(request.form.get('overhead_cost','0'))
     if any(not x.is_finite() or x<0 or x>1000000000 for x in (pack,over)):raise ValueError('Некорректные затраты')
     result=cost_v47_calculate(cost_v47_rows(c,batch),batch['planned_qty'],batch['actual_qty'],pack,over)
     if not result['complete']:raise ValueError('Не указаны цены на всё использованное сырьё')
     result['currency']='RUB';result['batch_id']=bid
     c.execute('''INSERT INTO batch_costs_v47(batch_id,packaging_cost,overhead_cost,note,snapshot_json,created_at,created_by)
      VALUES(?,?,?,?,?,?,?)''',(bid,float(pack),float(over),request.form.get('note','')[:1000],json.dumps(result,ensure_ascii=False),now(),session['uid']))
     audit(c,'cost.v47.snapshot',f'batch={bid}; total={result["total_cost"]}')
    else:raise ValueError('Неизвестная операция')
   flash('Сохранено')
  except (ValueError,KeyError,ArithmeticError) as e:return fail(e)
  return redirect('/economics/costs')
 with conn() as c:
  materials=c.execute('''SELECT m.id,m.name,m.unit,p.price_per_unit FROM materials m
   LEFT JOIN cost_prices_v47 p ON p.material_id=m.id ORDER BY m.name''').fetchall()
  batches=c.execute('''SELECT b.*,l.lot_code,p.name product,s.batch_id fixed
   FROM batches b JOIN lots l ON l.id=b.lot_id JOIN recipes r ON r.id=b.recipe_id
   JOIN products p ON p.id=r.product_id LEFT JOIN batch_costs_v47 s ON s.batch_id=b.id
   ORDER BY b.id DESC LIMIT 100''').fetchall()
 return page('Себестоимость партий',"""
 <div class='card'><h2>Цены сырья</h2><p class='muted'>Вручную заданная учётная цена за единицу номенклатуры (RUB). Не является фактической закупочной ценой конкретной партии.</p>
 <table><tr><th>Сырьё</th><th>Цена за единицу</th><th>Действие</th></tr>{% for m in materials %}
 <tr><td>{{m.name}} ({{m.unit}})</td><td>{{m.price_per_unit if m.price_per_unit is not none else 'Не указана'}}</td>
 <td>{% if has_role('admin','economist') %}<form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='price'><input type='hidden' name='material_id' value='{{m.id}}'><input type='number' name='price' min='0' max='1000000000' step='0.0001' required placeholder='Цена'><button>Сохранить</button></form>{% endif %}</td></tr>{% endfor %}</table></div>
 <div class='card'><h2>Выпущенные партии</h2><p class='muted'>Расчёт использует реальные списания из журнала consumptions и нормативы действующей рецептуры. Для исторических партий рецептура могла измениться; проверьте нормы до фиксации.</p>
 <table><tr><th>Партия</th><th>Продукция</th><th>Выпуск</th><th>Расчёт</th></tr>{% for b in batches %}<tr><td>{{b.lot_code}}</td><td>{{b.product}}</td><td>{{b.actual_qty}}</td><td><a href='/economics/costs/{{b.id}}'>{{'Открыть зафиксированный' if b.fixed else 'Рассчитать'}}</a></td></tr>{% endfor %}</table></div>
 """,materials=materials,batches=batches)

@app.route('/economics/costs/<int:batch_id>',methods=['GET'])
@allowed('admin','economist','technologist')
def cost_detail_v47(batch_id):
 with conn() as c:
  batch=c.execute('''SELECT b.*,l.lot_code,p.name product FROM batches b JOIN lots l ON l.id=b.lot_id
   JOIN recipes r ON r.id=b.recipe_id JOIN products p ON p.id=r.product_id WHERE b.id=?''',(batch_id,)).fetchone()
  if not batch:abort(404)
  saved=c.execute('SELECT * FROM batch_costs_v47 WHERE batch_id=?',(batch_id,)).fetchone()
  report=json.loads(saved['snapshot_json']) if saved else cost_v47_calculate(cost_v47_rows(c,batch),batch['planned_qty'],batch['actual_qty'],0,0)
 return page('Себестоимость '+batch['lot_code'],"""
 <div class='card'><h2>{{batch.product}} · {{batch.lot_code}}</h2><p>{{'Зафиксированный расчёт' if saved else 'Предварительный расчёт'}} · Выпуск {{report.actual_qty}} · Валюта RUB</p>
 {% if not report.complete %}<p class='msg'>Не все цены указаны. Итоговая себестоимость не рассчитана.</p>{% endif %}
 <table><tr><th>Сырьё</th><th>Норма</th><th>Списано</th><th>Цена</th><th>План, RUB</th><th>Факт, RUB</th></tr>
 {% for r in report.lines %}<tr><td>{{r.material}}</td><td>{{r.norm_qty}} {{r.unit}}</td><td>{{r.qty}} {{r.unit}}</td><td>{{r.price if r.price is not none else '—'}}</td><td>{{r.planned_cost if r.price is not none else '—'}}</td><td>{{r.actual_cost if r.price is not none else '—'}}</td></tr>{% endfor %}</table>
 <p>Упаковка: {{report.packaging_cost}} · Прочие затраты: {{report.overhead_cost}}</p>
 {% if report.complete %}<h3>Итого: {{report.total_cost}} RUB · {{report.cost_per_unit}} RUB / ед. выпуска</h3><p>Отклонение стоимости сырья от нормы: {{report.material_deviation}} RUB</p>{% endif %}
 {% if saved %}<p class='muted'>Зафиксировано {{saved.created_at}}. Расчёт не меняется при обновлении цен.</p>
 {% elif has_role('admin','economist') %}<form method='post' action='/economics/costs'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='action' value='snapshot'><input type='hidden' name='batch_id' value='{{batch.id}}'>
 <label>Упаковка, RUB<input type='number' name='packaging_cost' min='0' step='0.01' value='0' required></label><label>Прочие затраты, RUB<input type='number' name='overhead_cost' min='0' step='0.01' value='0' required></label>
 <label>Примечание<input name='note' maxlength='1000'></label><button {% if not report.complete %}disabled{% endif %}>Зафиксировать расчёт</button></form>{% endif %}
 </div><p><a href='/economics/costs'>← К списку</a></p>
 """,batch=batch,report=report,saved=saved)

if __name__=='__main__':
 init()
 app.run(host='0.0.0.0',port=int(os.environ.get('PORT',8080)))


# v46: role-aware, read-only task inbox built from live records.
# A read marker is per user and per event version; it never changes document status.
def notification_items_v46(c,roles,limit=120):
 items=[]
 def add(kind,key,title,subtitle,url,stamp):
  items.append({'kind':kind,'key':key,'title':title,'subtitle':subtitle,
                'url':url,'stamp':stamp or ''})
 if 'admin' in roles or 'operator' in roles or 'technologist' in roles:
  for r in c.execute("SELECT id,lot_code,due_date,status FROM work_orders WHERE status='approved' ORDER BY id DESC LIMIT 40"):
   add('Производство','wo:'+str(r['id'])+':'+r['status'],'Производственное задание №'+str(r['id']),r['lot_code']+' · срок '+r['due_date'],'/work-orders',r['due_date'])
 if 'admin' in roles or 'technologist' in roles:
  for r in c.execute("SELECT id,order_id,issued_at FROM production_sheets WHERE status='review' ORDER BY id DESC LIMIT 40"):
   add('Технолог','sheet:'+str(r['id'])+':review','Лист №'+str(r['id'])+' ожидает проверки','Задание №'+str(r['order_id']),'/production/sheets/'+str(r['id']),r['issued_at'])
 if 'admin' in roles or 'warehouse' in roles:
  for r in c.execute("SELECT id,customer,vehicle,created_at,status FROM dispatch_tasks_v31 WHERE status IN ('new','assigned') ORDER BY id DESC LIMIT 40"):
   add('Склад','dispatch:'+str(r['id'])+':'+r['status'],'Задание на отгрузку №'+str(r['id']),r['customer']+' · '+r['vehicle'],'/dispatch/tasks/'+str(r['id']),r['created_at'])
 if 'admin' in roles or 'quality' in roles:
  for r in c.execute("SELECT id,sample_code,registered_at FROM qc_samples WHERE status='open' ORDER BY id DESC LIMIT 40"):
   add('ОТК','sample:'+str(r['id'])+':open','Проба '+r['sample_code']+' ожидает проверки','Лаборатория ОТК','/quality/samples',r['registered_at'])
  for r in c.execute("SELECT id,registered_at FROM customer_returns_v29 WHERE status='quarantine' ORDER BY id DESC LIMIT 30"):
   add('ОТК','return:'+str(r['id'])+':quarantine','Возврат №'+str(r['id'])+' в карантине','Требуется решение ОТК','/quality/customer-returns/'+str(r['id']),r['registered_at'])
 if 'admin' in roles or 'economist' in roles:
  for r in c.execute("SELECT task_id,revision,submitted_at FROM dispatch_reviews_v39 WHERE status='pending' ORDER BY submitted_at DESC LIMIT 40"):
   add('Экономист','photo:'+str(r['task_id'])+':'+str(r['revision']),'Проверить фотоотчёт №'+str(r['task_id']),'Версия '+str(r['revision']),'/dispatch/v39/'+str(r['task_id']),r['submitted_at'])
 if 'admin' in roles or 'warehouse' in roles:
  for r in c.execute("SELECT task_id,revision,reviewed_at FROM dispatch_reviews_v39 WHERE status='changes_requested' ORDER BY reviewed_at DESC LIMIT 40"):
   add('Склад','photo-fix:'+str(r['task_id'])+':'+str(r['revision']),'Исправить фотоотчёт №'+str(r['task_id']),'Возвращён экономистом','/dispatch/v39/'+str(r['task_id']),r['reviewed_at'])
 from stock_alerts_v61 import stock_notification_items
 items.extend(stock_notification_items(c,session['uid']))
 return sorted(items,key=lambda x:x['stamp'],reverse=True)[:limit]

@app.route('/notifications',methods=['GET','POST'])
@login_required
def notifications_v46():
 if request.method=='POST':
  action=request.form.get('action')
  if action not in ('read','unread'):abort(400)
  key=request.form.get('key','')
  if not key or len(key)>180:abort(400)
  with conn() as c:
   c.execute('BEGIN IMMEDIATE')
   current={item['key'] for item in notification_items_v46(c,current_roles())}
   if key not in current:abort(404)
   if action=='read':
    c.execute('INSERT INTO notification_reads_v46(user_id,event_key,read_at) VALUES(?,?,?) ON CONFLICT(user_id,event_key) DO UPDATE SET read_at=excluded.read_at',(session['uid'],key,now()))
   else:
    c.execute('DELETE FROM notification_reads_v46 WHERE user_id=? AND event_key=?',(session['uid'],key))
   audit(c,'notifications.v46.'+action,key)
  return redirect('/notifications')
 with conn() as c:
  items=notification_items_v46(c,current_roles())
  read={r['event_key'] for r in c.execute('SELECT event_key FROM notification_reads_v46 WHERE user_id=?',(session['uid'],))}
 for item in items:item['read']=item['key'] in read
 unread=sum(not item['read'] for item in items)
 return page('Мои задачи и уведомления',"""
 <div class='card'><h2>Центр задач и уведомлений</h2>
 <p>Непрочитанных: <b>{{unread}}</b> · Всего активных: <b>{{items|length}}</b></p>
 <p class='muted'>Задачи формируются из текущих документов и доступны согласно вашим ролям. Отметка «Прочитано» не завершает задачу и не изменяет её статус.</p></div>
 <div class='card'><table><thead><tr><th>Статус</th><th>Раздел</th><th>Задача</th><th>Действие</th></tr></thead><tbody>
 {% for n in items %}<tr><td>{% if n.read %}Прочитано{% else %}<b>Новое</b>{% endif %}</td>
 <td>{{n.kind}}</td><td><a href='{{n.url}}'><b>{{n.title}}</b></a><div class='muted'>{{n.subtitle}}</div></td>
 <td><form method='post'><input type='hidden' name='_csrf' value='{{csrf_token}}'><input type='hidden' name='key' value='{{n.key}}'>
 <button name='action' value='{{"unread" if n.read else "read"}}'>{{"Не прочитано" if n.read else "Прочитано"}}</button></form></td></tr>
 {% else %}<tr><td colspan='4'>Сейчас нет задач для ваших ролей.</td></tr>{% endfor %}
 </tbody></table></div>""",items=items,unread=unread)

# v50: register monitoring after all existing routes.
from observability_v50 import register as register_monitoring_v50
register_monitoring_v50(app, conn, DB, page, allowed)

# v51: administrator dashboard and in-app operational alerts.
from operations_v51 import register as register_operations_v51
from observability_v50 import readiness as readiness_v50
register_operations_v51(app,conn,DB,page,allowed,readiness_v50)


# v59: read-only release readiness, evaluated from live DB inside the production transaction.
def v59_release_readiness(c, order_id):
 order=c.execute('SELECT id,lot_code,status,planned_qty FROM work_orders WHERE id=?',(order_id,)).fetchone()
 if order is None:raise ValueError('Производственное задание не найдено')
 blockers=[]; warnings=[]; checks=[]
 def check(label,ok,reason,blocking=True):
  checks.append({'label':label,'ok':bool(ok),'reason':reason,'blocking':blocking})
  if not ok:(blockers if blocking else warnings).append(reason)
 check('Статус задания',order['status']=='approved','Задание должно быть утверждено и ещё не выполнено')
 sheet=c.execute('SELECT id,status FROM production_sheets WHERE order_id=? ORDER BY id DESC LIMIT 1',(order_id,)).fetchone()
 check('Производственный лист',sheet is not None,'Для задания не выдан производственный лист')
 if sheet:
  detail=v44_execution_status(c,sheet['id'])
  check('Обязательные измерения',not detail['missing'],'Не заполнены обязательные измерения: '+', '.join(detail['missing'][:8]))
  check('Отклонения',detail['unresolved']==0,'Есть неразрешённые отклонения: '+str(detail['unresolved']))
  check('Бумажный оригинал',bool(detail['paper']),'Не зарегистрирован бумажный оригинал')
  check('Проверка технологом',sheet['status']=='closed','Производственный лист не закрыт технологом')
  # Reconciliation is advisory: legacy production may not have CT48 issue records.
  recon=detail['reconciliation']
  check('Сверка загрузки',recon['issue_count']==0,'Есть замечания по загрузке: '+str(recon['issue_count']),False)
 staged=c.execute("SELECT COUNT(*) FROM tsd_picks WHERE order_id=? AND status='staged'",(order_id,)).fetchone()[0]
 check('Подбор тары',staged==0,'Осталась подобранная, но не выданная тара')
 return {'order':dict(order),'sheet_id':sheet['id'] if sheet else None,'ready':not blockers,'blockers':blockers,'warnings':warnings,'checks':checks}

@app.get('/production/release-readiness')
@allowed('admin','technologist','operator','quality','warehouse','viewer')
def v59_readiness_list():
 with conn() as c:
  orders=c.execute("SELECT id,lot_code,status FROM work_orders ORDER BY id DESC LIMIT 100").fetchall()
  rows=[]
  for order in orders:
   data=v59_release_readiness(c,order['id'])
   rows.append({'id':order['id'],'lot_code':order['lot_code'],'status':order['status'],'ready':data['ready'],'blockers':len(data['blockers']),'warnings':len(data['warnings'])})
 return page('Готовность партий к выпуску',"""
 <div class='card'><h2>Готовность партий к выпуску</h2><p class='muted'>Контроль технологических измерений, отклонений, бумажного листа и загрузки. Статус готовности рассчитывается при открытии страницы. Расхождения загрузки показаны как предупреждения.</p>
 <table><tr><th>Задание</th><th>Партия</th><th>Статус</th><th>Готовность</th><th>Замечания</th><th></th></tr>{% for r in rows %}<tr><td>№{{r.id}}</td><td>{{r.lot_code}}</td><td>{{r.status}}</td><td>{{'Готово' if r.ready else 'Не готово'}}</td><td>{{r.blockers}} блокир. / {{r.warnings}} предупр.</td><td><a href='/production/release-readiness/{{r.id}}'>Проверить</a></td></tr>{% else %}<tr><td colspan='6'>Нет заданий</td></tr>{% endfor %}</table></div>
 """,rows=rows)

@app.get('/production/release-readiness/<int:order_id>')
@allowed('admin','technologist','operator','quality','warehouse','viewer')
def v59_readiness_detail(order_id):
 with conn() as c:
  try:d=v59_release_readiness(c,order_id)
  except ValueError:abort(404)
 return page('Готовность партии',"""
 <div class='card'><h2>Задание №{{d.order.id}} · {{d.order.lot_code}}</h2><h3>{{'По обязательным проверкам готово' if d.ready else 'Не готово к выпуску'}}</h3><p>Статус: {{d.order.status}}</p>
 {% if d.sheet_id %}<p><a href='/production/sheets/{{d.sheet_id}}'>Производственный лист</a> · <a href='/production/execution/{{d.sheet_id}}'>Контроль выполнения</a></p>{% endif %}
 <table><tr><th>Проверка</th><th>Результат</th><th>Характер</th></tr>{% for x in d.checks %}<tr><td>{{x.label}}</td><td>{{'OK' if x.ok else x.reason}}</td><td>{{'Блокирующий' if x.blocking else 'Предупреждение'}}</td></tr>{% endfor %}</table>
 <p class='muted'>По умолчанию это диагностическая проверка. Блокировка выпуска включается отдельно параметром LOVETECH_V59_ENFORCE_RELEASE=1. Не является решением ОТК о разрешении реализации продукции.</p></div>
 """,d=d)


# v60: explicit alternative sequence: review -> release into quarantine -> close paper sheet -> independent QC.
def v60_sequence_status(c, order_id):
    """Read-only gate. Evaluated inside the caller's SQLite transaction."""
    base=v59_release_readiness(c,order_id)
    order=base['order']
    checks=[]
    blockers=[]
    warnings=list(base['warnings'])
    for check in base['checks']:
        item=dict(check)
        if item['label']=='Проверка технологом':
            sheet=c.execute('SELECT status FROM production_sheets WHERE id=?',(base['sheet_id'],)).fetchone() if base['sheet_id'] else None
            item['ok']=bool(sheet and sheet['status'] in ('review','closed'))
            item['reason']='Производственный лист должен быть передан на проверку технологу'
        checks.append(item)
        if not item['ok'] and item['blocking']:
            blockers.append(item['reason'])
    return dict(order=order,sheet_id=base['sheet_id'],ready=not blockers,
                blockers=blockers,warnings=warnings,checks=checks)

@app.get('/production/release-sequence/<int:order_id>')
@allowed('admin','technologist','operator','quality','warehouse','viewer')
def v60_release_sequence_detail(order_id):
    with conn() as c:
        try:
            data=v60_sequence_status(c,order_id)
            order=c.execute('SELECT status,batch_id FROM work_orders WHERE id=?',(order_id,)).fetchone()
            sheet=c.execute('SELECT id,status FROM production_sheets WHERE order_id=? ORDER BY id DESC LIMIT 1',(order_id,)).fetchone()
        except ValueError:abort(404)
    mode=os.environ.get('LOVETECH_V60_RELEASE_SEQUENCE','legacy')
    enforced=os.environ.get('LOVETECH_V59_ENFORCE_RELEASE','0')=='1'
    return page('Порядок завершения производства',"""
    <div class='card'><h2>Партия {{d.order.lot_code}} · задание №{{d.order.id}}</h2>
    <p>Режим: <b>{{mode}}</b>; блокировка выпуска: <b>{{'включена' if enforced else 'выключена'}}</b></p>
    <ol><li>Заполнить обязательные измерения, рассмотреть отклонения и зарегистрировать бумажный оригинал.</li>
    <li>Передать лист на проверку (статус review).</li>
    <li>Зарегистрировать выпуск готовой продукции на карантин (задание completed).</li>
    <li>Технолог сверяет бумажный оригинал и закрывает производственный лист.</li>
    <li>ОТК отдельно принимает решение о разрешении готовой продукции.</li></ol>
    <p><b>{{'Предварительные проверки пройдены' if d.ready else 'Есть незавершённые проверки'}}</b></p>
    <table><tr><th>Проверка</th><th>Результат</th></tr>{% for x in d.checks %}<tr><td>{{x.label}}</td><td>{{'OK' if x.ok else x.reason}}</td></tr>{% endfor %}</table>
    <p>Задание: {{order.status}}; лист: {{sheet.status if sheet else 'не выдан'}}.</p>
    <p><a href='/production/release-readiness/{{d.order.id}}'>Строгая проверка v59</a> · <a href='/production/sheets/{{sheet.id}}' {% if not sheet %}style='display:none'{% endif %}>Производственный лист</a></p>
    <p class='muted'>При режиме legacy действуют прежние правила. Это не разрешение ОТК на реализацию.</p></div>
    """,d=data,order=dict(order),sheet=dict(sheet) if sheet else None,mode=mode,enforced=enforced)

# v61: stock minimum thresholds and recipient-targeted in-app alerts.
from stock_alerts_v61 import register_stock_alerts
register_stock_alerts(app,conn,page,allowed,audit,now)

# v62: separate attendance access; only one admin-appointed account can edit.
from attendance_v62 import register_attendance, get_access
def _attendance_menu_access(uid):
 with conn() as c:
  admin,editor=get_access(c,uid)
  return admin or editor
register_attendance(app,conn,page,audit,now)

# v64: advanced monthly timesheets, bulk schedules and period locking.
from attendance_v64 import register_attendance_v64
register_attendance_v64(app,conn,page,audit,now)

# v65: stock planning and explicit purchase suggestions.
from stock_planning_v65 import register_v65
register_v65(app,conn,page,allowed,audit,now)


# v66: one QC gate for all shipment paths, with an SQLite safety net.
from qc_shipping_v66 import install_shipping_guard_v66, shipping_eligibility_v66

def require_finished_shipping_v66(c, lot_id):
    ok, reason = shipping_eligibility_v66(c, lot_id)
    if not ok:
        raise ValueError('Отгрузка запрещена ОТК: ' + reason)

_previous_init_v65 = init
def init():
    _previous_init_v65()
    with conn() as c:
        install_shipping_guard_v66(c)

@app.get('/quality/shipping-control')
@allowed('admin','quality','warehouse','economist','viewer')
def quality_shipping_control_v66():
    with conn() as c:
        lots = c.execute("SELECT l.id,l.lot_code,l.status,l.qty,l.expires,p.name FROM lots l JOIN products p ON p.id=l.product_id WHERE l.kind='finished' ORDER BY l.id DESC LIMIT 250").fetchall()
        rows = [dict(lot=dict(r), check=shipping_eligibility_v66(c,r['id'])) for r in lots]
    return page('ОТК · Допуск к отгрузке',"""<div class='card'><h2>Контроль допуска готовой продукции к отгрузке</h2>
    <p>Отгрузка разрешена только при статусе released, действующем сроке годности, положительном остатке и действительных результатах ОТК по всем активным нормам. Новая отклонённая или открытая проба блокирует отгрузку до рассмотрения.</p>
    <table><tr><th>Партия</th><th>Продукция</th><th>Остаток</th><th>Статус</th><th>Допуск</th></tr>
    {% for r in rows %}<tr><td>{{r.lot.lot_code}}</td><td>{{r.lot.name}}</td><td>{{r.lot.qty}}</td><td>{{r.lot.status}}</td><td>{% if r.check[0] %}Разрешено{% else %}<span class='danger'>{{r.check[1]}}</span>{% endif %}</td></tr>{% endfor %}</table></div>""",rows=rows)
