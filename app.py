from flask import Flask, render_template, request, jsonify, send_file
import sqlite3, json, csv, io, os
import urllib.request, urllib.error
from datetime import datetime, date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('SMARTSHOP_DATA_DIR', str(BASE_DIR / 'instance'))).resolve()
DB_PATH = DATA_DIR / 'smartbilling.db'
BUNDLED_DB = BASE_DIR / 'instance' / 'smartbilling.db'

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False
    
STORE_NAME = 'SHRI JI ENTERPRISES'
ADDRESS = 'तीर्थ मंदिर, सराय तरीन (संभल) – 244303'
CONTACTS = '9927892667 | 9758673114'
DISCOUNT_RATE = 0.20

PRODUCTS = [
    ('p','Poshak','Thakur Ji Poshak'), ('m','Mala','Shringar'), ('mu','Mukut','Shringar'),
    ('b','Bansuri','Shringar'), ('j','Jhula','Shringar'), ('sh','Singhasan','Shringar'),
    ('dm','Decorative Mala','Shringar'), ('kp','Krishna Poshak (Children)','Thakur Ji Poshak'),
    ('kd','Kade (कड़े)','Accessories'), ('n','Necklace','Accessories'), ('ne','Nekar','Accessories'),
    ('h','Hair','Accessories'), ('dp','Devi Poshak','Thakur Ji Poshak'), ('k','Kurti','Ladies Wear'),
    ('pl','Plazo','Ladies Wear'), ('f','Fall','Ladies Wear'), ('a','Astar','Ladies Wear'), ('g','Gown','Ladies Wear')
]


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH != BUNDLED_DB and not DB_PATH.exists() and BUNDLED_DB.exists():
        import shutil
        shutil.copy2(BUNDLED_DB, DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def table_columns(conn, table):
    return {r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def add_column_if_missing(conn, table, column, definition):
    if column not in table_columns(conn, table):
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def init_db():
    conn = get_db()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        address TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shortcut TEXT UNIQUE NOT NULL,
        barcode TEXT UNIQUE,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        price REAL NOT NULL DEFAULT 0,
        stock INTEGER NOT NULL DEFAULT 0,
        min_stock INTEGER NOT NULL DEFAULT 1,
        expiry_date TEXT,
        location_id INTEGER,
        created_at TEXT NOT NULL,
        FOREIGN KEY(location_id) REFERENCES locations(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS bills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_no TEXT UNIQUE NOT NULL,
        customer_name TEXT,
        customer_mobile TEXT,
        subtotal REAL NOT NULL,
        discount REAL NOT NULL,
        final_amount REAL NOT NULL,
        payment TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS bill_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_id INTEGER NOT NULL,
        product_name TEXT NOT NULL,
        qty INTEGER NOT NULL,
        price REAL NOT NULL,
        line_total REAL NOT NULL,
        FOREIGN KEY(bill_id) REFERENCES bills(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER,
        product_name TEXT NOT NULL,
        change_qty INTEGER NOT NULL,
        movement_type TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL
    );
    ''')

    # Migration for older database created by the previous version.
    add_column_if_missing(conn, 'products', 'expiry_date', 'TEXT')
    add_column_if_missing(conn, 'products', 'location_id', 'INTEGER')
    add_column_if_missing(conn, 'products', 'barcode', 'TEXT')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode) WHERE barcode IS NOT NULL AND barcode <> \'\'')

    now = datetime.now().isoformat(timespec='seconds')
    # Ensure a default shop location exists.
    conn.execute('INSERT OR IGNORE INTO locations(name,address,created_at) VALUES(?,?,?)',
                 ('Main Shop', ADDRESS, now))
    main_location = conn.execute('SELECT id FROM locations WHERE name=?', ('Main Shop',)).fetchone()['id']

    for key, name, category in PRODUCTS:
        conn.execute('''
            INSERT OR IGNORE INTO products(shortcut,name,category,price,stock,min_stock,expiry_date,location_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
        ''', (key, name, category, 0, 0, 1, None, main_location, now))
    # Preserve existing stock while initializing per-location stock tracking.
    rows = conn.execute('SELECT id,location_id,stock FROM products WHERE location_id IS NOT NULL').fetchall()
    for r in rows:
        conn.execute('INSERT OR IGNORE INTO location_stock(product_id,location_id,qty) VALUES(?,?,?)',(r['id'],r['location_id'],r['stock']))
    conn.commit()
    conn.close()


def next_bill_no(conn):
    row = conn.execute('SELECT bill_no FROM bills ORDER BY id DESC LIMIT 1').fetchone()
    if not row:
        return 'SJ-1001'
    try:
        return f"SJ-{int(row['bill_no'].split('-')[-1]) + 1}"
    except Exception:
        return 'SJ-1001'


def json_rows(rows):
    return [dict(r) for r in rows]


def alerts_rows(conn):
    today = date.today()
    soon = today + timedelta(days=7)
    rows = conn.execute('''
        SELECT p.*, l.name AS location_name
        FROM products p
        LEFT JOIN locations l ON l.id=p.location_id
        ORDER BY p.name
    ''').fetchall()
    alerts = []
    for p in rows:
        stock = int(p['stock'] or 0)
        min_stock = int(p['min_stock'] or 1)
        if stock <= min_stock:
            alerts.append({
                'type': 'low_stock', 'priority': 'High' if stock == 0 else 'Medium',
                'title': f"{p['name']} is low on stock",
                'message': f"Only {stock} left (minimum {min_stock}).",
                'product_id': p['id'], 'product_name': p['name'], 'location_name': p['location_name'] or '-'
            })
        if p['expiry_date']:
            try:
                exp = date.fromisoformat(p['expiry_date'])
                if exp < today:
                    alerts.append({
                        'type': 'expired', 'priority': 'High',
                        'title': f"{p['name']} has expired",
                        'message': f"Expiry date: {p['expiry_date']}.",
                        'product_id': p['id'], 'product_name': p['name'], 'location_name': p['location_name'] or '-'
                    })
                elif exp <= soon:
                    alerts.append({
                        'type': 'expiry', 'priority': 'High' if exp <= today + timedelta(days=2) else 'Medium',
                        'title': f"{p['name']} expires soon",
                        'message': f"Expiry date: {p['expiry_date']}.",
                        'product_id': p['id'], 'product_name': p['name'], 'location_name': p['location_name'] or '-'
                    })
            except ValueError:
                pass
    order = {'High': 0, 'Medium': 1, 'Low': 2}
    alerts.sort(key=lambda a: (order.get(a['priority'], 9), a['type'], a['product_name'].lower()))
    return alerts


def ai_context():
    """Build a read-only business snapshot for the AI assistant."""
    conn = get_db()
    summary = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM products) AS products,
            (SELECT COALESCE(SUM(stock),0) FROM products) AS stock_units,
            (SELECT COUNT(*) FROM products WHERE stock <= min_stock) AS low_stock,
            (SELECT COUNT(*) FROM bills WHERE date(created_at)=date('now','localtime')) AS today_bills,
            (SELECT COALESCE(SUM(final_amount),0) FROM bills WHERE date(created_at)=date('now','localtime')) AS today_sales,
            (SELECT COALESCE(SUM(final_amount),0) FROM bills WHERE strftime('%Y-%m',created_at,'localtime')=strftime('%Y-%m','now','localtime')) AS month_sales,
            (SELECT COUNT(*) FROM bills WHERE date(created_at)>=date('now','-6 days','localtime')) AS week_bills,
            (SELECT COALESCE(SUM(final_amount),0) FROM bills WHERE date(created_at)>=date('now','-6 days','localtime')) AS week_sales
    """).fetchone()
    credit = conn.execute("SELECT COUNT(DISTINCT customer_id) customers, COALESCE(SUM(CASE WHEN entry_type='SALE_DUE' THEN amount WHEN entry_type='PAYMENT' THEN -amount ELSE 0 END),0) outstanding FROM customer_ledger").fetchone()
    products = conn.execute("""
        SELECT p.name,p.category,p.price,p.stock,p.min_stock,p.expiry_date,l.name AS location
        FROM products p LEFT JOIN locations l ON l.id=p.location_id ORDER BY p.name
    """).fetchall()
    top = conn.execute("""
        SELECT bi.product_name AS name, SUM(bi.qty) AS qty, ROUND(SUM(bi.line_total),2) AS revenue
        FROM bill_items bi JOIN bills b ON b.id=bi.bill_id
        GROUP BY bi.product_name ORDER BY qty DESC LIMIT 10
    """).fetchall()
    category_sales = conn.execute("""
        SELECT COALESCE(p.category,'Unknown') AS category,
               SUM(bi.qty) AS qty, ROUND(SUM(bi.line_total),2) AS revenue
        FROM bill_items bi
        JOIN bills b ON b.id=bi.bill_id
        LEFT JOIN products p ON lower(p.name)=lower(bi.product_name)
        GROUP BY COALESCE(p.category,'Unknown') ORDER BY revenue DESC
    """).fetchall()
    daily_sales = conn.execute("""
        SELECT date(created_at,'localtime') AS day, COUNT(*) AS bills,
               ROUND(COALESCE(SUM(final_amount),0),2) AS sales
        FROM bills
        WHERE date(created_at)>=date('now','-6 days','localtime')
        GROUP BY day ORDER BY day
    """).fetchall()
    recent = conn.execute("""
        SELECT bill_no, customer_name, final_amount, payment, created_at
        FROM bills ORDER BY id DESC LIMIT 8
    """).fetchall()
    conn.close()
    alert_conn = get_db()
    alerts = alerts_rows(alert_conn)
    alert_conn.close()
    return {
        'summary': {**dict(summary), **dict(credit)},
        'products': json_rows(products),
        'top_selling_products': json_rows(top),
        'category_sales': json_rows(category_sales),
        'daily_sales': json_rows(daily_sales),
        'recent_bills': json_rows(recent),
        'alerts': alerts
    }


def fallback_ai_answer(question, ctx):
    """Read-only business assistant that works even without an external AI key."""
    q = question.lower().strip()
    summary = ctx['summary']
    products = ctx['products']
    alerts = ctx['alerts']
    top = ctx['top_selling_products']
    categories = ctx['category_sales']
    daily = ctx['daily_sales']

    low = [p for p in products if int(p['stock'] or 0) <= int(p['min_stock'] or 1)]
    exp = [a for a in alerts if a['type'] in ('expiry','expired')]

    if any(k in q for k in ['low stock', 'low-stock', 'shortage', 'running out', 'restock', 're-stock']):
        if not low:
            return 'There are currently no products at or below their minimum stock level.'
        return 'Products to review for restocking:\n' + '\n'.join(
            f"• {p['name']}: {p['stock']} left (minimum {p['min_stock']})" for p in low)

    if any(k in q for k in ['expire', 'expiry', 'expiring']):
        if not exp:
            return 'No expiry-related alerts are currently recorded.'
        return 'Expiry alerts:\n' + '\n'.join(f"• {a['product_name']}: {a['message']}" for a in exp)

    if any(k in q for k in ['today sale', 'today sales', 'sold today', 'sales today']):
        return f"Today's sales are ₹{float(summary['today_sales'] or 0):,.2f} across {summary['today_bills']} bill(s)."

    if any(k in q for k in ['this week', 'weekly', 'week sale', 'week sales', 'last 7 days']):
        return f"The last 7 recorded days show ₹{float(summary['week_sales'] or 0):,.2f} in sales across {summary['week_bills']} bill(s)."

    if any(k in q for k in ['month sale', 'month sales', 'monthly sale', 'monthly sales']):
        return f"This month's recorded sales are ₹{float(summary['month_sales'] or 0):,.2f}."

    if any(k in q for k in ['category', 'categories', 'which section', 'which type']) and categories:
        return 'Sales by category:\n' + '\n'.join(
            f"• {c['category']}: {c['qty']} units, ₹{float(c['revenue'] or 0):,.2f} revenue" for c in categories)

    if any(k in q for k in ['top product', 'best selling', 'best-selling', 'most sold', 'fast moving', 'fast-moving']):
        if not top:
            return 'There are no recorded sales yet, so I cannot identify a best-selling product.'
        return 'Top recorded products by quantity sold:\n' + '\n'.join(
            f"• {p['name']}: {p['qty']} units (₹{float(p['revenue'] or 0):,.2f})" for p in top)

    if any(k in q for k in ['udhaar', 'outstanding', 'credit', 'customer due', 'customer dues']):
        return f"Customer credit snapshot: {summary.get('customers',0)} customer(s) recorded and ₹{float(summary.get('outstanding',0) or 0):,.2f} total outstanding."

    if any(k in q for k in ['inventory summary', 'inventory', 'stock summary', 'how many product', 'total product', 'stock units']):
        return (f"Inventory snapshot: {summary['products']} catalog products and "
                f"{summary['stock_units']} total units in stock. {summary['low_stock']} product(s) "
                f"are at or below minimum stock.")

    if any(k in q for k in ['business', 'suggest', 'suggestion', 'advice', 'improve', 'what should', 'strategy', 'analysis', 'performance']):
        advice = []
        if low:
            advice.append(f"Review {len(low)} low-stock product(s) before the next restock cycle.")
        if exp:
            advice.append(f"Check {len(exp)} expiry-related alert(s) and handle those products promptly.")
        if top:
            advice.append(f"Use your top-selling items as a signal when planning future stock purchases.")
        if categories:
            advice.append(f"Your highest recorded category by revenue is {categories[0]['category']} (₹{float(categories[0]['revenue'] or 0):,.2f}).")
        if daily:
            values=[float(d['sales'] or 0) for d in daily]
            if len(values) >= 2 and values[-1] > values[0]:
                advice.append('Recorded sales on the latest day are higher than the first day in the 7-day snapshot.')
            elif len(values) >= 2 and values[-1] < values[0]:
                advice.append('Recorded sales on the latest day are lower than the first day in the 7-day snapshot.')
        if not advice:
            advice.append('More completed bills will give the assistant more sales history for business analysis.')
        return 'Business snapshot:\n' + '\n'.join(f'• {x}' for x in advice)

    if any(k in q for k in ['alert', 'alerts']):
        return f"There are {len(alerts)} current alert(s): {sum(1 for a in alerts if a['type']=='low_stock')} stock alert(s) and {len(exp)} expiry alert(s)."

    return (f"I can help with sales, inventory and business analysis. Current snapshot: "
            f"₹{float(summary['today_sales'] or 0):,.2f} sales today, "
            f"₹{float(summary['month_sales'] or 0):,.2f} this month, "
            f"{summary['stock_units']} units in stock and {summary['low_stock']} low-stock item(s).\n\n"
            "Try: 'Give me a business analysis', 'Which category sells most?', "
            "'What should I restock?', 'How were sales this week?', or 'Show top-selling products'.")


def call_external_ai(question, ctx):
    """Optional OpenAI-compatible assistant. No API key is stored in the project."""
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if not api_key:
        return None

    model = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini').strip()
    instructions = (
        'You are the SmartShop business assistant for SHRI JI ENTERPRISES. '
        'Answer using only the supplied shop data. Do not invent sales, stock, prices, or products. '
        'If the data is insufficient, say so. Keep answers concise and useful for a shopkeeper. '
        'You may summarize inventory, sales, low stock, expiry alerts, and business patterns. '
        'Do not change any database data; this endpoint is read-only.'
    )
    payload = json.dumps({
        'model': model,
        'instructions': instructions,
        'input': f"SHOP DATA:\n{json.dumps(ctx, ensure_ascii=False)}\n\nUSER QUESTION:\n{question}"
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.openai.com/v1/responses',
        data=payload,
        headers={'Content-Type':'application/json', 'Authorization':f'Bearer {api_key}'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data=json.loads(response.read().decode('utf-8'))
        text=data.get('output_text')
        if text:
            return text.strip()
        parts=[]
        for item in data.get('output',[]):
            for content in item.get('content',[]):
                if content.get('type') in ('output_text','text') and content.get('text'):
                    parts.append(content['text'])
        return '\n'.join(parts).strip() or None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None


@app.route('/')
def dashboard():
    return render_template('dashboard.html', store=STORE_NAME, address=ADDRESS, contacts=CONTACTS)

@app.route('/billing')
def billing():
    return render_template('billing.html', store=STORE_NAME, address=ADDRESS, contacts=CONTACTS)

@app.route('/inventory')
def inventory():
    return render_template('inventory.html', store=STORE_NAME, address=ADDRESS)

@app.route('/products')
def products_redirect():
    return inventory()

@app.route('/sales')
def sales():
    return render_template('sales.html', store=STORE_NAME, address=ADDRESS)

@app.route('/alerts')
def alerts():
    return render_template('alerts.html', store=STORE_NAME, address=ADDRESS)

@app.route('/locations')
def locations():
    return render_template('locations.html', store=STORE_NAME, address=ADDRESS)

@app.route('/reports')
def reports():
    return render_template('reports.html', store=STORE_NAME, address=ADDRESS)

@app.route('/settings')
def settings():
    return render_template('settings.html', store=STORE_NAME, address=ADDRESS, contacts=CONTACTS)


@app.route('/ai')
def ai_assistant():
    return render_template('ai_assistant.html', store=STORE_NAME, address=ADDRESS, contacts=CONTACTS)


@app.post('/api/ai/chat')
def api_ai_chat():
    data=request.get_json(force=True) or {}
    question=str(data.get('message','')).strip()
    if not question:
        return jsonify({'error':'Please enter a question.'}),400
    ctx=ai_context()
    external=call_external_ai(question, ctx)
    answer=external or fallback_ai_answer(question, ctx)
    return jsonify({'answer':answer, 'mode':'ai' if external else 'smart-fallback'})


@app.get('/api/summary')
def api_summary():
    conn = get_db()
    today = conn.execute('''SELECT COUNT(*) bills, COALESCE(SUM(final_amount),0) sales,
                            COALESCE(SUM(discount),0) discount FROM bills
                            WHERE date(created_at)=date('now','localtime')''').fetchone()
    month = conn.execute('''SELECT COUNT(*) bills, COALESCE(SUM(final_amount),0) sales
                            FROM bills WHERE strftime('%Y-%m',created_at,'localtime')=
                            strftime('%Y-%m','now','localtime')''').fetchone()
    products = conn.execute('SELECT COUNT(*) c FROM products').fetchone()['c']
    stock_units = conn.execute('SELECT COALESCE(SUM(stock),0) s FROM products').fetchone()['s']
    low = conn.execute('SELECT COUNT(*) c FROM products WHERE stock <= min_stock').fetchone()['c']
    locations_count = conn.execute('SELECT COUNT(*) c FROM locations').fetchone()['c']
    alert_count = len(alerts_rows(conn))
    top_products = conn.execute('''SELECT bi.product_name name, SUM(bi.qty) qty
                                   FROM bill_items bi JOIN bills b ON b.id=bi.bill_id
                                   WHERE date(b.created_at)=date('now','localtime')
                                   GROUP BY bi.product_name ORDER BY qty DESC LIMIT 5''').fetchall()
    # Chart data: last 7 calendar days, including zero-sales days.
    sales_trend = []
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        row = conn.execute('''SELECT COUNT(*) bills, COALESCE(SUM(final_amount),0) sales
                             FROM bills WHERE date(created_at)=?''', (d,)).fetchone()
        sales_trend.append({'date': d, 'bills': row['bills'], 'sales': row['sales']})
    payment_mix = conn.execute('''SELECT payment, COUNT(*) count, COALESCE(SUM(final_amount),0) amount
                                  FROM bills GROUP BY payment ORDER BY amount DESC''').fetchall()
    recent = conn.execute('''SELECT id,bill_no,customer_name,final_amount,payment,created_at
                             FROM bills ORDER BY id DESC LIMIT 6''').fetchall()
    conn.close()
    return jsonify({
        'today_bills': today['bills'], 'today_sales': today['sales'], 'today_discount': today['discount'],
        'month_bills': month['bills'], 'month_sales': month['sales'], 'products': products,
        'stock_units': stock_units, 'low_stock': low, 'alerts': alert_count, 'locations': locations_count,
        'top_products': json_rows(top_products), 'recent_bills': json_rows(recent),
        'sales_trend': sales_trend, 'payment_mix': json_rows(payment_mix)
    })


@app.get('/api/alerts')
def api_alerts():
    conn = get_db(); data = alerts_rows(conn); conn.close()
    return jsonify(data)


@app.get('/api/products')
def api_products():
    q = request.args.get('q', '').strip().lower()
    barcode = request.args.get('barcode', '').strip()
    location_id = request.args.get('location_id', '').strip()
    conn = get_db()
    sql = '''SELECT p.*, l.name AS location_name FROM products p LEFT JOIN locations l ON l.id=p.location_id'''
    params = []
    where = []
    if q:
        where.append('(lower(p.name) LIKE ? OR lower(p.shortcut) LIKE ? OR lower(p.category) LIKE ? OR lower(COALESCE(p.barcode,\'\')) LIKE ?)')
        params += [f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%']
    if barcode:
        where.append('p.barcode=?'); params.append(barcode)
    if location_id:
        where.append('p.location_id=?'); params.append(int(location_id))
    if where: sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY p.name'
    rows = conn.execute(sql, params).fetchall(); conn.close()
    return jsonify(json_rows(rows))


@app.post('/api/products')
def api_add_product():
    data = request.get_json(force=True) or {}
    name = str(data.get('name','')).strip(); shortcut = str(data.get('shortcut','')).strip().lower()
    barcode = str(data.get('barcode','')).strip() or None
    category = str(data.get('category','')).strip() or 'General'
    try:
        price = max(0, float(data.get('price') or 0)); stock = max(0, int(data.get('stock') or 0)); min_stock = max(1, int(data.get('min_stock') or 1))
    except (TypeError, ValueError):
        return jsonify({'error':'Price, stock and minimum stock must be valid numbers.'}), 400
    expiry_date = data.get('expiry_date') or None
    location_id = data.get('location_id')
    if not name or not shortcut: return jsonify({'error':'Name and shortcut are required.'}), 400
    conn = get_db()
    try:
        conn.execute('''INSERT INTO products(shortcut,barcode,name,category,price,stock,min_stock,expiry_date,location_id,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)''', (shortcut,barcode,name,category,price,stock,min_stock,expiry_date,location_id or None,datetime.now().isoformat(timespec='seconds')))
        pid = conn.execute('SELECT last_insert_rowid() id').fetchone()['id']
        if stock:
            conn.execute('INSERT INTO stock_movements(product_id,product_name,change_qty,movement_type,note,created_at) VALUES(?,?,?,?,?,?)',
                         (pid,name,stock,'Opening Stock','Initial stock',datetime.now().isoformat(timespec='seconds')))
        conn.commit()
        row = conn.execute('SELECT p.*,l.name location_name FROM products p LEFT JOIN locations l ON l.id=p.location_id WHERE p.id=?',(pid,)).fetchone()
        return jsonify(dict(row))
    except sqlite3.IntegrityError:
        return jsonify({'error':'Shortcut already exists.'}), 409
    finally: conn.close()


@app.put('/api/products/<int:pid>')
def api_edit_product(pid):
    data = request.get_json(force=True) or {}
    conn = get_db(); old = conn.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
    if not old: conn.close(); return jsonify({'error':'Product not found.'}), 404
    try:
        values = {
            'name': str(data.get('name', old['name'])).strip(),
            'shortcut': str(data.get('shortcut', old['shortcut'])).strip().lower(),
            'barcode': str(data.get('barcode', old['barcode'] if 'barcode' in old.keys() else '')).strip() or None,
            'category': str(data.get('category', old['category'])).strip() or old['category'],
            'price': max(0,float(data.get('price',old['price']) or 0)),
            'stock': max(0,int(data.get('stock',old['stock']) or 0)),
            'min_stock': max(1,int(data.get('min_stock',old['min_stock']) or 1)),
            'expiry_date': data.get('expiry_date') if data.get('expiry_date') != '' else old['expiry_date'],
            'location_id': data.get('location_id') if data.get('location_id') not in ('',None) else old['location_id']
        }
        conn.execute('''UPDATE products SET shortcut=?,barcode=?,name=?,category=?,price=?,stock=?,min_stock=?,expiry_date=?,location_id=? WHERE id=?''',
                     (values['shortcut'],values['barcode'],values['name'],values['category'],values['price'],values['stock'],values['min_stock'],values['expiry_date'],values['location_id'],pid))
        diff = values['stock'] - old['stock']
        if diff:
            conn.execute('INSERT INTO stock_movements(product_id,product_name,change_qty,movement_type,note,created_at) VALUES(?,?,?,?,?,?)',
                         (pid,values['name'],diff,'Manual Adjustment','Edited from inventory',datetime.now().isoformat(timespec='seconds')))
        conn.commit(); row=conn.execute('SELECT p.*,l.name location_name FROM products p LEFT JOIN locations l ON l.id=p.location_id WHERE p.id=?',(pid,)).fetchone(); return jsonify(dict(row))
    except sqlite3.IntegrityError:
        return jsonify({'error':'Shortcut already exists.'}), 409
    except (TypeError, ValueError):
        return jsonify({'error':'Invalid product values.'}), 400
    finally: conn.close()


@app.delete('/api/products/<int:pid>')
def api_delete_product(pid):
    conn=get_db(); conn.execute('DELETE FROM products WHERE id=?',(pid,)); conn.commit(); conn.close(); return jsonify({'ok':True})


@app.post('/api/products/<int:pid>/stock')
def api_adjust_stock(pid):
    data=request.get_json(force=True) or {}
    try: change=int(data.get('change_qty'))
    except (TypeError,ValueError): return jsonify({'error':'Stock change must be a whole number.'}),400
    conn=get_db(); p=conn.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
    if not p: conn.close(); return jsonify({'error':'Product not found.'}),404
    new_stock=int(p['stock'])+change
    if new_stock<0: conn.close(); return jsonify({'error':'Stock cannot go below zero.'}),400
    movement_type='Stock In' if change>0 else 'Stock Out'
    note=str(data.get('note','')).strip() or ('Manual stock increase' if change>0 else 'Manual stock decrease')
    conn.execute('UPDATE products SET stock=? WHERE id=?',(new_stock,pid))
    conn.execute('INSERT INTO stock_movements(product_id,product_name,change_qty,movement_type,note,created_at) VALUES(?,?,?,?,?,?)',
                 (pid,p['name'],change,movement_type,note,datetime.now().isoformat(timespec='seconds')))
    conn.commit(); row=conn.execute('SELECT p.*,l.name location_name FROM products p LEFT JOIN locations l ON l.id=p.location_id WHERE p.id=?',(pid,)).fetchone(); conn.close(); return jsonify(dict(row))


@app.get('/api/stock-movements')
def api_stock_movements():
    conn=get_db(); rows=conn.execute('SELECT * FROM stock_movements ORDER BY id DESC LIMIT 250').fetchall(); conn.close(); return jsonify(json_rows(rows))


@app.get('/api/bill-next')
def api_bill_next():
    conn=get_db(); no=next_bill_no(conn); conn.close(); return jsonify({'bill_no':no})


@app.post('/api/bills')
def api_create_bill():
    data=request.get_json(force=True) or {}; items=data.get('items') or []
    if not items: return jsonify({'error':'At least one item is required.'}),400
    customer_name=str(data.get('customer_name','')).strip(); customer_mobile=str(data.get('customer_mobile','')).strip(); payment=str(data.get('payment','Cash')).strip() or 'Cash'
    if payment.lower() in ('udhaar','credit') and not (customer_name or customer_mobile): return jsonify({'error':'Udhaar ke liye customer name ya mobile required hai.'}),400
    try:
        discount_rate=float(data.get('discount_rate', DISCOUNT_RATE * 100))
    except (TypeError, ValueError):
        discount_rate=DISCOUNT_RATE * 100
    discount_rate=max(0.0, min(100.0, discount_rate))
    conn=get_db()
    try:
        normalized=[]
        subtotal=0.0
        for raw in items:
            name=str(raw.get('name','')).strip(); qty=int(raw.get('qty',1)); price=float(raw.get('price',0))
            if not name or qty<1 or price<=0: raise ValueError('Invalid item, quantity or price.')
            # Prefer known product by name; allow custom line item as before.
            p=conn.execute('SELECT id,name,stock FROM products WHERE lower(name)=lower(?)',(name,)).fetchone()
            if p and qty>int(p['stock']): raise ValueError(f"Not enough stock for {p['name']}. Available: {p['stock']}.")
            line=round(qty*price,2); subtotal+=line
            normalized.append({'name':name,'qty':qty,'price':price,'product_id':p['id'] if p else None})
        discount=round(subtotal*(discount_rate/100.0),2); final_amount=round(subtotal-discount,2); created_at=datetime.now().isoformat(timespec='seconds'); bill_no=next_bill_no(conn)
        customer_id=None
        if customer_name or customer_mobile:
            existing=conn.execute('SELECT id FROM customers WHERE lower(name)=lower(?) AND COALESCE(mobile,"")=COALESCE(?,"")',(customer_name,customer_mobile)).fetchone()
            if existing: customer_id=existing['id']
            else:
                curc=conn.execute('INSERT INTO customers(name,mobile,created_at) VALUES(?,?,?)',(customer_name or 'Walk-in Customer',customer_mobile or None,created_at)); customer_id=curc.lastrowid
        cur=conn.execute('''INSERT INTO bills(bill_no,customer_name,customer_mobile,subtotal,discount,final_amount,payment,created_at)
                           VALUES(?,?,?,?,?,?,?,?)''',(bill_no,customer_name,customer_mobile,subtotal,discount,final_amount,payment,created_at))
        bill_id=cur.lastrowid
        if customer_id and payment.lower() in ('udhaar','credit'):
            conn.execute('INSERT INTO customer_ledger(customer_id,bill_id,entry_type,amount,note,created_at) VALUES(?,?,?,?,?,?)',(customer_id,bill_id,'SALE_DUE',final_amount,f'Bill {bill_no}',created_at))
        elif customer_id:
            conn.execute('INSERT INTO customer_ledger(customer_id,bill_id,entry_type,amount,note,created_at) VALUES(?,?,?,?,?,?)',(customer_id,bill_id,'SALE_PAID',0,f'Bill {bill_no} — paid',created_at))
        for x in normalized:
            conn.execute('INSERT INTO bill_items(bill_id,product_name,qty,price,line_total) VALUES(?,?,?,?,?)',(bill_id,x['name'],x['qty'],x['price'],round(x['qty']*x['price'],2)))
            if x['product_id']:
                conn.execute('UPDATE products SET stock=stock-? WHERE id=?',(x['qty'],x['product_id']))
                conn.execute('INSERT INTO stock_movements(product_id,product_name,change_qty,movement_type,note,created_at) VALUES(?,?,?,?,?,?)',
                             (x['product_id'],x['name'],-x['qty'],'Sale',f'Bill {bill_no}',created_at))
        conn.commit()
        return jsonify({'bill_no':bill_no,'created_at':created_at,'customer_name':customer_name,'customer_mobile':customer_mobile,'payment':payment,
                        'items':normalized,'subtotal':subtotal,'discount':discount,'discount_rate':discount_rate,'final_amount':final_amount})
    except Exception as e:
        conn.rollback(); return jsonify({'error':str(e)}),400
    finally: conn.close()


@app.get('/api/customers')
def api_customers():
    q=request.args.get('q','').strip().lower(); conn=get_db()
    where='WHERE lower(c.name) LIKE ? OR lower(COALESCE(c.mobile,"")) LIKE ?' if q else ''
    params=[f'%{q}%',f'%{q}%'] if q else []
    rows=conn.execute(f'''SELECT c.id,c.name,c.mobile,COALESCE(SUM(CASE WHEN cl.entry_type='SALE_DUE' THEN cl.amount WHEN cl.entry_type='PAYMENT' THEN -cl.amount ELSE 0 END),0) outstanding,COUNT(DISTINCT cl.bill_id) bills FROM customers c LEFT JOIN customer_ledger cl ON cl.customer_id=c.id {where} GROUP BY c.id ORDER BY c.name''',params).fetchall(); conn.close(); return jsonify(json_rows(rows))

@app.post('/api/customers/<int:cid>/payment')
def api_customer_payment(cid):
    data=request.get_json(force=True) or {}
    try: amount=float(data.get('amount',0))
    except (TypeError,ValueError): return jsonify({'error':'Payment amount must be valid.'}),400
    if amount<=0:return jsonify({'error':'Payment amount must be greater than zero.'}),400
    conn=get_db(); c=conn.execute('SELECT id FROM customers WHERE id=?',(cid,)).fetchone()
    if not c: conn.close(); return jsonify({'error':'Customer not found.'}),404
    outstanding=conn.execute("SELECT COALESCE(SUM(CASE WHEN entry_type='SALE_DUE' THEN amount WHEN entry_type='PAYMENT' THEN -amount ELSE 0 END),0) v FROM customer_ledger WHERE customer_id=?",(cid,)).fetchone()['v']
    amount=min(amount,max(0,float(outstanding)))
    conn.execute('INSERT INTO customer_ledger(customer_id,entry_type,amount,note,created_at) VALUES(?,?,?,?,?)',(cid,'PAYMENT',amount,str(data.get('note','Payment received')).strip(),datetime.now().isoformat(timespec='seconds'))); conn.commit(); conn.close(); return jsonify({'ok':True,'paid':amount})

@app.get('/api/customers/<int:cid>/ledger')
def api_customer_ledger(cid):
    conn=get_db(); rows=conn.execute('SELECT * FROM customer_ledger WHERE customer_id=? ORDER BY id DESC',(cid,)).fetchall(); conn.close(); return jsonify(json_rows(rows))

@app.post('/api/stock-transfer')
def api_stock_transfer():
    data=request.get_json(force=True) or {}
    try: pid=int(data.get('product_id')); from_id=int(data.get('from_location_id')); to_id=int(data.get('to_location_id')); qty=int(data.get('qty'))
    except (TypeError,ValueError): return jsonify({'error':'Product, locations and quantity are required.'}),400
    if from_id==to_id or qty<=0:return jsonify({'error':'Choose two different locations and a positive quantity.'}),400
    conn=get_db(); p=conn.execute('SELECT id,name,stock FROM products WHERE id=?',(pid,)).fetchone()
    if not p: conn.close(); return jsonify({'error':'Product not found.'}),404
    src=conn.execute('SELECT qty FROM location_stock WHERE product_id=? AND location_id=?',(pid,from_id)).fetchone(); src_qty=int(src['qty']) if src else 0
    if qty>src_qty:return jsonify({'error':f'Only {src_qty} units available at the source location.'}),400
    conn.execute('INSERT OR IGNORE INTO location_stock(product_id,location_id,qty) VALUES(?,?,0)',(pid,to_id))
    conn.execute('UPDATE location_stock SET qty=qty-? WHERE product_id=? AND location_id=?',(qty,pid,from_id)); conn.execute('UPDATE location_stock SET qty=qty+? WHERE product_id=? AND location_id=?',(qty,pid,to_id))
    note=str(data.get('note','')).strip() or f'Transfer {from_id} → {to_id}'
    conn.execute('INSERT INTO stock_movements(product_id,product_name,change_qty,movement_type,note,created_at) VALUES(?,?,?,?,?,?)',(pid,p['name'],0,'Transfer',note,datetime.now().isoformat(timespec='seconds'))); conn.commit(); conn.close(); return jsonify({'ok':True})


@app.get('/api/bills')
def api_bills():
    q=request.args.get('q','').strip().lower(); conn=get_db()
    sql='''SELECT b.*,COUNT(bi.id) item_count FROM bills b LEFT JOIN bill_items bi ON bi.bill_id=b.id'''; params=[]
    if q:
        sql += " WHERE lower(b.bill_no) LIKE ? OR lower(COALESCE(b.customer_name,'')) LIKE ? OR lower(COALESCE(b.customer_mobile,'')) LIKE ?"
        params=[f'%{q}%',f'%{q}%',f'%{q}%']
    sql += ' GROUP BY b.id ORDER BY b.id DESC LIMIT 1000'; rows=conn.execute(sql,params).fetchall(); conn.close(); return jsonify(json_rows(rows))


@app.get('/api/bills/<int:bid>')
def api_bill_detail(bid):
    conn=get_db(); b=conn.execute('SELECT * FROM bills WHERE id=?',(bid,)).fetchone()
    if not b: conn.close(); return jsonify({'error':'Bill not found.'}),404
    items=conn.execute('SELECT * FROM bill_items WHERE bill_id=? ORDER BY id',(bid,)).fetchall(); conn.close(); d=dict(b); d['items']=json_rows(items); return jsonify(d)


@app.delete('/api/bills/<int:bid>')
def api_delete_bill(bid):
    conn=get_db();
    conn.execute('DELETE FROM customer_ledger WHERE bill_id=?',(bid,))
    # Do not attempt stock reversal automatically because bills may be deleted after stock moved.
    conn.execute('DELETE FROM bills WHERE id=?',(bid,)); conn.commit(); conn.close(); return jsonify({'ok':True})

@app.delete('/api/bills')
def api_clear_bills():
    conn=get_db(); conn.execute('DELETE FROM customer_ledger'); conn.execute('DELETE FROM bills'); conn.commit(); conn.close(); return jsonify({'ok':True})


@app.get('/api/category-sales')
def api_category_sales():
    conn=get_db(); rows=conn.execute('''SELECT COALESCE(p.category,bi.product_name) category,SUM(bi.qty) qty,ROUND(SUM(bi.line_total),2) revenue
        FROM bill_items bi JOIN bills b ON b.id=bi.bill_id LEFT JOIN products p ON lower(p.name)=lower(bi.product_name)
        GROUP BY COALESCE(p.category,bi.product_name) ORDER BY qty DESC''').fetchall(); conn.close(); return jsonify(json_rows(rows))


@app.get('/api/report')
def api_report():
    period=request.args.get('period','month'); conn=get_db()
    if period=='today': where="date(created_at)=date('now','localtime')"
    elif period=='month': where="strftime('%Y-%m',created_at,'localtime')=strftime('%Y-%m','now','localtime')"
    elif period=='year': where="strftime('%Y',created_at,'localtime')=strftime('%Y','now','localtime')"
    else: where='1=1'
    summary=conn.execute(f'''SELECT COUNT(*) bills,COALESCE(SUM(subtotal),0) subtotal,COALESCE(SUM(discount),0) discount,COALESCE(SUM(final_amount),0) final FROM bills WHERE {where}''').fetchone()
    daily=conn.execute(f'''SELECT date(created_at,'localtime') day,COUNT(*) bills,COALESCE(SUM(final_amount),0) sales FROM bills WHERE {where} GROUP BY day ORDER BY day''').fetchall()
    conn.close(); return jsonify({'summary':dict(summary),'daily':json_rows(daily)})


@app.get('/api/locations')
def api_locations():
    conn=get_db(); rows=conn.execute('''SELECT l.*,COUNT(p.id) product_count,COALESCE(SUM(p.stock),0) stock_units
                              FROM locations l LEFT JOIN products p ON p.location_id=l.id GROUP BY l.id ORDER BY l.name''').fetchall(); conn.close(); return jsonify(json_rows(rows))

@app.post('/api/locations')
def api_add_location():
    data=request.get_json(force=True) or {}; name=str(data.get('name','')).strip(); address=str(data.get('address','')).strip()
    if not name:return jsonify({'error':'Location name is required.'}),400
    conn=get_db()
    try:
        conn.execute('INSERT INTO locations(name,address,created_at) VALUES(?,?,?)',(name,address,datetime.now().isoformat(timespec='seconds'))); conn.commit()
        row=conn.execute('SELECT * FROM locations WHERE name=?',(name,)).fetchone(); return jsonify(dict(row))
    except sqlite3.IntegrityError:return jsonify({'error':'Location already exists.'}),409
    finally:conn.close()

@app.delete('/api/locations/<int:lid>')
def api_delete_location(lid):
    conn=get_db(); count=conn.execute('SELECT COUNT(*) c FROM products WHERE location_id=?',(lid,)).fetchone()['c']
    if count:
        conn.close(); return jsonify({'error':'Move products to another location before deleting this location.'}),400
    conn.execute('DELETE FROM locations WHERE id=?',(lid,));conn.commit();conn.close();return jsonify({'ok':True})


@app.get('/api/backup')
def api_backup():
    conn=get_db(); payload={'version':2,'app':STORE_NAME+' Smart Billing','exported_at':datetime.now().isoformat(timespec='seconds'),
        'locations':json_rows(conn.execute('SELECT * FROM locations').fetchall()),
        'products':json_rows(conn.execute('SELECT * FROM products').fetchall()),
        'bills':json_rows(conn.execute('SELECT * FROM bills').fetchall()),
        'bill_items':json_rows(conn.execute('SELECT * FROM bill_items').fetchall()),
        'stock_movements':json_rows(conn.execute('SELECT * FROM stock_movements').fetchall()),
        'customers':json_rows(conn.execute('SELECT * FROM customers').fetchall()),
        'customer_ledger':json_rows(conn.execute('SELECT * FROM customer_ledger').fetchall()),
        'location_stock':json_rows(conn.execute('SELECT * FROM location_stock').fetchall())}
    conn.close(); mem=io.BytesIO(json.dumps(payload,ensure_ascii=False,indent=2).encode('utf-8'))
    return send_file(mem,as_attachment=True,download_name='SHRI_JI_COLLECTION_SmartBilling_Backup.json',mimetype='application/json')


@app.post('/api/restore')
def api_restore():
    if 'file' not in request.files:return jsonify({'error':'Backup file missing.'}),400
    try:
        payload=json.load(request.files['file']); conn=get_db(); conn.execute('DELETE FROM stock_movements');conn.execute('DELETE FROM customer_ledger');conn.execute('DELETE FROM customers');conn.execute('DELETE FROM location_stock');conn.execute('DELETE FROM bill_items');conn.execute('DELETE FROM bills');conn.execute('DELETE FROM products');conn.execute('DELETE FROM locations')
        for l in payload.get('locations',[]):conn.execute('INSERT INTO locations(id,name,address,created_at) VALUES(?,?,?,?)',(l['id'],l['name'],l.get('address'),l['created_at']))
        for p in payload.get('products',[]):conn.execute('''INSERT INTO products(id,shortcut,name,category,price,stock,min_stock,expiry_date,location_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',(p['id'],p['shortcut'],p['name'],p['category'],p['price'],p['stock'],p['min_stock'],p.get('expiry_date'),p.get('location_id'),p['created_at']))
        for b in payload.get('bills',[]):conn.execute('''INSERT INTO bills(id,bill_no,customer_name,customer_mobile,subtotal,discount,final_amount,payment,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',(b['id'],b['bill_no'],b.get('customer_name'),b.get('customer_mobile'),b['subtotal'],b['discount'],b['final_amount'],b['payment'],b['created_at']))
        for bi in payload.get('bill_items',[]):conn.execute('''INSERT INTO bill_items(id,bill_id,product_name,qty,price,line_total) VALUES(?,?,?,?,?,?)''',(bi['id'],bi['bill_id'],bi['product_name'],bi['qty'],bi['price'],bi['line_total']))
        for sm in payload.get('stock_movements',[]):conn.execute('''INSERT INTO stock_movements(id,product_id,product_name,change_qty,movement_type,note,created_at) VALUES(?,?,?,?,?,?,?)''',(sm['id'],sm.get('product_id'),sm['product_name'],sm['change_qty'],sm['movement_type'],sm.get('note'),sm['created_at']))
        for c in payload.get('customers',[]):conn.execute('INSERT INTO customers(id,name,mobile,created_at) VALUES(?,?,?,?)',(c['id'],c['name'],c.get('mobile'),c['created_at']))
        for cl in payload.get('customer_ledger',[]):conn.execute('INSERT INTO customer_ledger(id,customer_id,bill_id,entry_type,amount,note,created_at) VALUES(?,?,?,?,?,?,?)',(cl['id'],cl['customer_id'],cl.get('bill_id'),cl['entry_type'],cl['amount'],cl.get('note'),cl['created_at']))
        for ls in payload.get('location_stock',[]):conn.execute('INSERT INTO location_stock(product_id,location_id,qty) VALUES(?,?,?)',(ls['product_id'],ls['location_id'],ls['qty']))
        conn.commit();conn.close();return jsonify({'ok':True})
    except Exception as e:return jsonify({'error':str(e)}),400


@app.get('/api/export/csv')
def export_csv():
    conn=get_db(); rows=conn.execute('''SELECT b.bill_no,b.created_at,b.customer_name,b.customer_mobile,bi.product_name,bi.qty,bi.price,bi.line_total,b.payment,b.subtotal,b.discount,b.final_amount FROM bills b JOIN bill_items bi ON bi.bill_id=b.id ORDER BY b.id DESC''').fetchall(); conn.close()
    buf=io.StringIO();w=csv.writer(buf);w.writerow(['Bill No','Date','Customer','Mobile','Item','Qty','Price','Line Total','Payment','Subtotal','Discount','Final Amount'])
    for r in rows:w.writerow([r[k] for k in r.keys()])
    return send_file(io.BytesIO(buf.getvalue().encode('utf-8-sig')),as_attachment=True,download_name='SHRI_JI_COLLECTION_sales.csv',mimetype='text/csv')


if __name__=='__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
   