from flask import Flask, render_template, request, redirect, url_for, flash, g, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import requests
import secrets
import string
import os
from dotenv import load_dotenv
import pytesseract
from PIL import Image
import io
import re
import pdfplumber
import psycopg2
import psycopg2.extras
import uuid
import cv2
import numpy as np
import logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
)
# Load environment variables from .env file
load_dotenv()
# Use DATABASE_URL if provided (Render) otherwise local sqlite file (dev)
DATABASE_URL = os.environ.get('DATABASE_URL')  # e.g. postgres://...

app = Flask(__name__)

# SECRET_KEY signs session cookies and CSRF tokens. A hardcoded fallback would
# let anyone forge sessions, so require it in production (where DATABASE_URL is
# set) and fall back to an ephemeral random key only for local dev.
_secret_key = os.environ.get('SECRET_KEY')
if not _secret_key:
    if DATABASE_URL:
        raise RuntimeError("SECRET_KEY environment variable must be set in production.")
    _secret_key = secrets.token_hex(32)  # ephemeral dev key (sessions reset on restart)
app.config['SECRET_KEY'] = _secret_key

# Cap request body size to prevent memory-exhaustion DoS via huge uploads.
# Generous enough for batch uploads of several high-res receipt photos/PDFs.
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB

app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# CSRF protection for all state-changing (POST) form submissions.
csrf = CSRFProtect(app)

# Render terminates TLS in front of gunicorn; without this, url_for(_external=True)
# would generate http:// links in emails instead of https://.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Supabase Auth (GoTrue) - handles signup confirmation emails, password reset
# emails, and credential verification. The anon key is Supabase's public key,
# safe to use here and to embed client-side (see reset_password.html).
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_ANON_KEY = os.environ.get('SUPABASE_ANON_KEY')

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    """Show a friendly message instead of a raw 400 when a CSRF token is
    missing or expired (e.g. the user left a form open too long)."""
    flash("Your session expired. Please try again.")
    return redirect(request.referrer or url_for('index'))


@app.errorhandler(413)
def handle_too_large(e):
    flash("That upload is too large. Please keep files under 32 MB.")
    return redirect(url_for('index'))


class User(UserMixin):
    def __init__(self, id, name, group_id):
        self.id = id
        self.name = name
        self.group_id = group_id

# Legacy shared password, used as a one-time fallback for accounts that
# predate per-user password hashes (see /login).
LEGACY_PASSWORD = os.environ.get("APP_PASSWORD")


def generate_invite_code(cursor, length=8):
    """Generate a random invite code that isn't already used by another group."""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(secrets.choice(alphabet) for _ in range(length))
        cursor.execute('SELECT 1 FROM groups WHERE invite_code = %s', (code,))
        if not cursor.fetchone():
            return code


def supabase_auth(method, path, **kwargs):
    """Call Supabase's GoTrue REST API. Returns (status_code, json_body).
    Returns a synthetic 503 if Supabase isn't configured or unreachable, so
    callers can always treat the result as (status, dict)."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return 503, {"error_description": "Auth service not configured."}
    headers = {
        'apikey': SUPABASE_ANON_KEY,
        'Authorization': f'Bearer {SUPABASE_ANON_KEY}',
        'Content-Type': 'application/json',
    }
    headers.update(kwargs.pop('headers', {}))
    try:
        resp = requests.request(method, f'{SUPABASE_URL}/auth/v1{path}', headers=headers, timeout=10, **kwargs)
        return resp.status_code, resp.json()
    except Exception as e:
        logging.error(f"Supabase auth request failed: {e}")
        return 503, {"error_description": "Auth service unavailable."}


@login_manager.user_loader
def load_user(user_id):
    cursor = get_cursor()
    cursor.execute('SELECT id, name, group_id FROM users WHERE id = %s', (user_id,))
    row = cursor.fetchone()
    return User(row['id'], row['name'], row['group_id']) if row else None

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        db = get_db()
        cursor = get_cursor()
        cursor.execute('SELECT id, name, email, auth_uid, password_hash, group_id FROM users WHERE id = %s', (username,))
        row = cursor.fetchone()

        if row and row['auth_uid'] and row['email']:
            status, data = supabase_auth('POST', '/token?grant_type=password',
                                          json={'email': row['email'], 'password': password})
            if status == 200:
                login_user(User(row['id'], row['name'], row['group_id']))
                return redirect(url_for("history"))
            err = (data.get('error_description') or data.get('msg') or '').lower()
            if 'confirm' in err:
                flash("Please confirm your email before logging in. "
                      "Use 'Resend confirmation email' below if you need a new link.")
            else:
                flash("Invalid username or password")
        elif row and row['password_hash'] is None and row['auth_uid'] is None and LEGACY_PASSWORD and password == LEGACY_PASSWORD:
            new_hash = generate_password_hash(password)
            cursor.execute('UPDATE users SET password_hash = %s WHERE id = %s', (new_hash, row['id']))
            db.commit()
            login_user(User(row['id'], row['name'], row['group_id']))
            return redirect(url_for("history"))
        elif row and row['password_hash'] and check_password_hash(row['password_hash'], password):
            login_user(User(row['id'], row['name'], row['group_id']))
            return redirect(url_for("history"))
        else:
            flash("Invalid username or password")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if not username or not password or not email:
            flash("Username, email, and password are required.")
            return render_template("register.html")
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            flash("Please enter a valid email address.")
            return render_template("register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.")
            return render_template("register.html")
        if password != confirm_password:
            flash("Passwords do not match.")
            return render_template("register.html")

        user_id = re.sub(r'[^a-z0-9]+', '_', username.lower()).strip('_')
        if not user_id:
            flash("Invalid username.")
            return render_template("register.html")

        db = get_db()
        cursor = get_cursor()
        cursor.execute('SELECT id FROM users WHERE id = %s', (user_id,))
        if cursor.fetchone():
            flash("Username already taken.")
            return render_template("register.html")

        cursor.execute('SELECT id FROM users WHERE email = %s', (email,))
        if cursor.fetchone():
            flash("An account with that email already exists.")
            return render_template("register.html")

        group_code = request.form.get("group_code", "").strip().upper()
        group_id = None
        if group_code:
            cursor.execute('SELECT id FROM groups WHERE invite_code = %s', (group_code,))
            group_row = cursor.fetchone()
            if not group_row:
                flash("That household invite code wasn't found.")
                return render_template("register.html")
            group_id = group_row['id']

        status, data = supabase_auth('POST', f"/signup?redirect_to={url_for('login', _external=True)}",
                                      json={'email': email, 'password': password})
        if status >= 400:
            flash(data.get('msg') or data.get('error_description') or "Could not create account.")
            return render_template("register.html")

        # /signup returns either {"user": {...}, "session": ...} (autoconfirm)
        # or the user object directly (confirmation required)
        user = data.get('user', data)
        if user.get('identities') == []:
            flash("An account with that email already exists. Try logging in or resetting your password.")
            return render_template("register.html")

        if group_id is None:
            invite_code = generate_invite_code(cursor)
            cursor.execute(
                'INSERT INTO groups (name, invite_code) VALUES (%s, %s) RETURNING id',
                (f"{username}'s household", invite_code)
            )
            group_id = cursor.fetchone()['id']

        cursor.execute(
            'INSERT INTO users (id, name, email, auth_uid, group_id, joined_at) VALUES (%s, %s, %s, %s, %s, NOW())',
            (user_id, username, email, user.get('id'), group_id)
        )
        db.commit()
        flash("Account created! Check your email for a confirmation link before logging in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/resend_verification", methods=["GET", "POST"])
def resend_verification():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        supabase_auth('POST', '/resend', json={'type': 'signup', 'email': email})
        flash("If that email is registered and not yet confirmed, a new confirmation link has been sent.", "success")
        return redirect(url_for("login"))
    return render_template("resend_verification.html")


@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        supabase_auth('POST', f"/recover?redirect_to={url_for('reset_password', _external=True)}",
                       json={'email': email})
        flash("If that email is registered, a password reset link has been sent.", "success")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.route("/reset_password")
def reset_password():
    return render_template("reset_password.html",
                            supabase_url=SUPABASE_URL, supabase_anon_key=SUPABASE_ANON_KEY)


# If Tesseract is not in your PATH, you might need to specify its path
# pytesseract.pytesseract.tesseract_cmd = r'/path/to/tesseract.exe'

#DATABASE = 'billsplitter.db'

def init_db():
    """Initializes the DB with tables. Supports Postgres (via DATABASE_URL) or local SQLite fallback."""
    # Postgres-compatible statements
    pg_users = """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
    """
    pg_users_alter = "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;"
    pg_users_alter_email = "ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT UNIQUE;"
    pg_users_alter_verified = "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT TRUE;"
    pg_users_alter_auth_uid = "ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_uid UUID UNIQUE;"
    pg_groups = """
        CREATE TABLE IF NOT EXISTS groups (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            invite_code TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """
    pg_users_alter_group = "ALTER TABLE users ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES groups(id);"
    pg_users_alter_joined_at = "ALTER TABLE users ADD COLUMN IF NOT EXISTS joined_at TIMESTAMP DEFAULT '2000-01-01';"
    pg_receipts_alter_group = "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES groups(id);"
    pg_receipts = """
        CREATE TABLE IF NOT EXISTS receipts (
            id SERIAL PRIMARY KEY,
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            payer_id TEXT,
            filename TEXT,
            bill_date DATE,
            total NUMERIC DEFAULT 0,
            image_path TEXT,
            FOREIGN KEY (payer_id) REFERENCES users(id)
        );
    """
    pg_items = """
        CREATE TABLE IF NOT EXISTS items (
            id SERIAL PRIMARY KEY,
            receipt_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            price NUMERIC NOT NULL,
            assigned_to TEXT NOT NULL,
            FOREIGN KEY (receipt_id) REFERENCES receipts(id)
        );
    """


    with app.app_context():
        if DATABASE_URL:
            # Use psycopg2 to create tables in Postgres
            conn = psycopg2.connect(DATABASE_URL, sslmode='require')
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(pg_groups)
            cur.execute(pg_users)
            cur.execute(pg_users_alter)
            cur.execute(pg_users_alter_email)
            cur.execute(pg_users_alter_verified)
            cur.execute(pg_users_alter_auth_uid)
            cur.execute(pg_users_alter_group)
            cur.execute(pg_users_alter_joined_at)
            cur.execute(pg_receipts)
            cur.execute(pg_receipts_alter_group)
            cur.execute(pg_items)
            # default users (use ON CONFLICT DO NOTHING)
            cur.execute("INSERT INTO users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", ('eser', 'Eser'))
            cur.execute("INSERT INTO users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", ('david', 'David'))

            # One-time migration: put any users/receipts without a group into a
            # shared "Household" group (covers the original eser/david data).
            cur.execute("SELECT COUNT(*) AS c FROM groups")
            if cur.fetchone()['c'] == 0:
                invite_code = generate_invite_code(cur)
                cur.execute(
                    "INSERT INTO groups (name, invite_code) VALUES (%s, %s) RETURNING id",
                    ("Household", invite_code)
                )
                default_group_id = cur.fetchone()['id']
                cur.execute("UPDATE users SET group_id = %s WHERE group_id IS NULL", (default_group_id,))
                cur.execute("UPDATE receipts SET group_id = %s WHERE group_id IS NULL", (default_group_id,))

            conn.commit()
            cur.close()
            conn.close()


def get_db():
    if 'db' not in g:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        g.db = conn
    return g.db
def get_cursor():
    db = get_db()
    return db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


@app.teardown_appcontext
def close_db(e=None):
    """Close DB connection at request end."""
    db = g.pop('db', None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


@app.context_processor
def inject_all_users():
    """Make the list of the current user's household members available to every template."""
    try:
        if not current_user.is_authenticated:
            return {'all_users': []}
        cursor = get_cursor()
        cursor.execute('SELECT id, name FROM users WHERE group_id = %s ORDER BY name', (current_user.group_id,))
        return {'all_users': cursor.fetchall()}
    except Exception:
        return {'all_users': []}


def parse_bill_text(text):
    """
    Parses bill text to extract items and their prices.
    This version is more flexible and handles both comma and period decimal separators.
    """
    items = []
    # This regex captures everything up to the price (allowing an optional
    # thousands separator and currency symbol) and ignores any trailing characters.
    item_line_pattern = re.compile(r'(.+?)\s*€?\s*(-?\d{1,3}(?:[.,]\d{3})*[.,]\d{2})\s*€?\s*.*?$')

    # Keywords to filter out non-item lines (like totals, taxes, etc.)
    filter_keywords = [
        'gesamt', 'summe', 'zwischensumme', 'steuer', 'mwst', 'ust',
        'bar', 'bargeld', 'karte', 'ec-cash', 'zahlung', 'betrag',
        'rueckgeld', 'rückgeld', 'saldo', 'rabatt', 'guthaben',
        'total', 'subtotal', 'tax', 'vat', 'cash', 'card', 'change', 'balance', 'discount', 'tip', 'trinkgeld',
        '/kg', '€/kg', 'stk',
    ]

    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue

        # Stop parsing once the totals section starts
        if 'summe' in line.lower() or 'total' in line.lower():
            break

        # Check if the line contains a filter keyword
        is_filtered_line = False
        for keyword in filter_keywords:
            if keyword in line.lower():
                is_filtered_line = True
                break
        if is_filtered_line:
            continue
        
        # Now, try to match the item pattern
        match = item_line_pattern.search(line)
        if match:
            description = match.group(1).strip()
            # Replace comma with period for float conversion
            price_str = match.group(2).replace(',', '.')
            try:
                price = float(price_str)
                items.append({'description': description, 'price': price, 'is_valid': True})
            except ValueError:
                continue
    return items

def preprocess_image(pil_image):
    """Enhances receipt image for better OCR accuracy."""
    # Convert PIL to OpenCV
    img = np.array(pil_image.convert('L'))  # grayscale

    # 1. Downscale large photos. OCR time and the deskew step below both
    # scale with pixel count, and phone photos are often far larger than
    # Tesseract needs.
    max_dim = 1800
    h, w = img.shape[:2]
    if max(h, w) > max_dim:
        scale = max_dim / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    # 2. Remove noise and improve contrast
    img = cv2.bilateralFilter(img, 9, 75, 75)

    # 3. Adaptive thresholding (binarize text)
    img = cv2.adaptiveThreshold(
        img, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31, 2
    )

    # 4. Deskew (fix tilted receipts). Estimate the angle from the text
    # pixels themselves (fewer points than the background, so this stays
    # fast), and only rotate for a genuine tilt - small angle "corrections"
    # just blur already-straight text.
    coords = cv2.findNonZero(255 - img)
    if coords is not None:
        angle = cv2.minAreaRect(coords)[-1]
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
        if 0.5 < abs(angle) < 15:
            h, w = img.shape[:2]
            M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
            img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    # Back to PIL
    return Image.fromarray(img)

def compute_settlements(balances):
    """Greedy debt simplification: repeatedly match the largest debtor with
    the largest creditor until all balances are settled."""
    creditors = sorted((b.copy() for b in balances if b['net'] > 0.005), key=lambda x: -x['net'])
    debtors = sorted((b.copy() for b in balances if b['net'] < -0.005), key=lambda x: x['net'])

    settlements = []
    i, j = 0, 0
    while i < len(debtors) and j < len(creditors):
        debtor = debtors[i]
        creditor = creditors[j]
        amount = min(-debtor['net'], creditor['net'])
        if amount > 0.005:
            settlements.append({
                'from': debtor['id'],
                'from_name': debtor['name'],
                'to': creditor['id'],
                'to_name': creditor['name'],
                'amount': round(amount, 2),
            })
        debtor['net'] += amount
        creditor['net'] -= amount
        if abs(debtor['net']) < 0.005:
            i += 1
        if abs(creditor['net']) < 0.005:
            j += 1
    return settlements


def calculate_balances_detailed(group_id):
    cursor = get_cursor()

    # Users with their join dates (founding members default to '2000-01-01')
    cursor.execute(
        'SELECT id, name, joined_at FROM users WHERE group_id = %s ORDER BY name',
        (group_id,)
    )
    users = cursor.fetchall()

    # All receipts with their effective date for membership cutoff
    cursor.execute(
        'SELECT id, payer_id, COALESCE(bill_date, upload_date) AS receipt_date '
        'FROM receipts WHERE group_id = %s',
        (group_id,)
    )
    receipts = cursor.fetchall()

    # All items for this group
    cursor.execute(
        'SELECT i.price, i.assigned_to, i.receipt_id FROM items i '
        'JOIN receipts r ON r.id = i.receipt_id WHERE r.group_id = %s',
        (group_id,)
    )
    items = cursor.fetchall()
    for item in items:
        item['price'] = float(item['price'])

    # Accumulate per-user totals, computing N per receipt based on who had joined by then.
    acc = {u['id']: {'personal': 0.0, 'shared_owed': 0.0, 'paid': 0.0} for u in users}
    total_shared = 0.0

    for receipt in receipts:
        rid = receipt['id']
        rdate = receipt['receipt_date']
        payer_id = receipt['payer_id']
        receipt_items = [i for i in items if i['receipt_id'] == rid]

        # Members who had joined by this receipt's date
        active_ids = [
            u['id'] for u in users
            if u['joined_at'] is None or u['joined_at'] <= rdate
        ]
        n = len(active_ids) or 1

        shared_in_receipt = sum(i['price'] for i in receipt_items if i['assigned_to'] == 'shared')
        total_shared += shared_in_receipt
        per_person_shared = shared_in_receipt / n

        for uid in active_ids:
            acc[uid]['shared_owed'] += per_person_shared

        for item in receipt_items:
            if item['assigned_to'] in acc:
                acc[item['assigned_to']]['personal'] += item['price']

        receipt_total = sum(i['price'] for i in receipt_items if i['assigned_to'] != 'excluded')
        if payer_id in acc:
            acc[payer_id]['paid'] += receipt_total
        elif payer_id == 'both' and n == 2:
            for uid in active_ids:
                acc[uid]['paid'] += receipt_total / 2

    balances = []
    for u in users:
        uid = u['id']
        d = acc[uid]
        responsibility = d['personal'] + d['shared_owed']
        net = d['paid'] - responsibility
        balances.append({
            'id': uid,
            'name': u['name'],
            'personal_total': round(d['personal'], 2),
            'shared_share': round(d['shared_owed'], 2),
            'paid_total': round(d['paid'], 2),
            'responsibility': round(responsibility, 2),
            'net': round(net, 2),
        })

    settlements = compute_settlements(balances)
    return {
        'users': balances,
        'shared_total': round(total_shared, 2),
        'settlements': settlements,
    }



@app.route('/')
@login_required
def index():
    """Main route to upload a bill."""
    return render_template('index.html')

OCR_CONFIG = r'--oem 3 --psm 4 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyzÄÖÜäöüß€%.,:-/ '


def _process_one_file(file):
    """OCR-process a single uploaded file. Returns a result dict or None on error."""
    file_bytes = file.read()
    ext = file.filename.rsplit('.', 1)[-1].lower()
    unique_filename = str(uuid.uuid4())
    extracted_text = ""
    image_path_for_display = None

    date_match = re.search(r'\d{4}-\d{2}-\d{2}', file.filename)
    bill_date = date_match.group(0) if date_match else 'Unknown Date'
    logging.info(f"Processing upload: {file.filename} ({len(file_bytes)/1024:.1f} KB)")

    try:
        if ext in ('png', 'jpg', 'jpeg', 'gif'):
            img = Image.open(io.BytesIO(file_bytes))
            processed_img = preprocess_image(img)
            image_path_for_display = f"{unique_filename}.png"
            processed_img.save(os.path.join(app.config['UPLOAD_FOLDER'], image_path_for_display))
            logging.info("Running OCR on image...")
            extracted_text = pytesseract.image_to_string(processed_img, lang='deu+eng', config=OCR_CONFIG)
            logging.info("OCR complete.")
        elif ext == 'pdf':
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        extracted_text += page_text + "\n--PAGE BREAK--\n"
                image_path_for_display = f"{unique_filename}.png"
                pdf.pages[0].to_image(resolution=150).save(
                    os.path.join(app.config['UPLOAD_FOLDER'], image_path_for_display)
                )
                logging.info(f"PDF processed ({len(pdf.pages)} pages).")
        else:
            flash(f'Unsupported file type: {ext}')
            return None
    except Exception as e:
        flash(f"Error processing {file.filename}: {e}")
        logging.exception(f"Error processing {file.filename}")
        return None

    parsed_items = parse_bill_text(extracted_text)
    return {
        'filename': file.filename,
        'bill_date': bill_date,
        'parsed_items': parsed_items,
        'total_sum': round(sum(i['price'] for i in parsed_items), 2),
        'image_path': url_for('uploaded_file', filename=image_path_for_display) if image_path_for_display else None,
    }


def get_assignment_memory(group_id):
    """Return {normalized_description: assigned_to} learned from this group's
    past item assignments. For each distinct description, pick the assignment
    used most often (ties broken by the most recent). Only current members and
    the 'shared' marker are eligible, so suggestions for removed users don't
    leak in."""
    cursor = get_cursor()
    cursor.execute('SELECT id FROM users WHERE group_id = %s', (group_id,))
    valid = {row['id'] for row in cursor.fetchall()}
    valid.add('shared')

    cursor.execute(
        'SELECT lower(btrim(i.description)) AS norm, i.assigned_to, '
        'COUNT(*) AS cnt, MAX(i.id) AS recent '
        'FROM items i JOIN receipts r ON r.id = i.receipt_id '
        'WHERE r.group_id = %s '
        'GROUP BY norm, i.assigned_to',
        (group_id,)
    )
    best = {}  # norm -> ((cnt, recent), assigned_to)
    for row in cursor.fetchall():
        if row['assigned_to'] not in valid or not row['norm']:
            continue
        rank = (row['cnt'], row['recent'])
        if row['norm'] not in best or rank > best[row['norm']][0]:
            best[row['norm']] = (rank, row['assigned_to'])
    return {norm: val[1] for norm, val in best.items()}


def _apply_assignment_memory(receipts, group_id):
    """Pre-fill each parsed item's suggested assignment from past history."""
    memory = get_assignment_memory(group_id)
    for receipt in receipts:
        for item in receipt['parsed_items']:
            norm = (item.get('description') or '').strip().lower()
            remembered = memory.get(norm)
            item['suggested'] = remembered if remembered else 'shared'
            item['from_memory'] = remembered is not None


@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload_bill():
    """Accepts one or more files, OCRs them all, and forwards to the assignment page."""
    files = [f for f in request.files.getlist('bill_image') if f.filename]
    if not files:
        flash('No files selected.')
        return redirect(request.url)

    receipts = [r for r in (_process_one_file(f) for f in files) if r is not None]
    if not receipts:
        return redirect(url_for('index'))

    _apply_assignment_memory(receipts, current_user.group_id)
    return render_template('bill_details.html', receipts=receipts)

@app.route('/uploads/<filename>')
@login_required
def uploaded_file(filename):
    """Securely serves uploaded files from the UPLOAD_FOLDER."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/save_details', methods=['POST'])
@login_required
def save_details():
    """Saves one or more assigned receipts and their items to the database."""
    db = None
    try:
        db = get_db()
        cursor = get_cursor()
        receipt_count = int(request.form.get('receipt_count', 1))

        # Valid assignment targets are only members of the current household,
        # plus the special 'shared'/'excluded' markers. This stops a tampered
        # form from crediting or assigning items to users in other groups.
        cursor.execute('SELECT id FROM users WHERE group_id = %s', (current_user.group_id,))
        valid_user_ids = {row['id'] for row in cursor.fetchall()}

        for ri in range(receipt_count):
            pfx = f'r{ri}_'
            payer_id = request.form[f'{pfx}payer_id']
            filename = request.form.get(f'{pfx}filename')
            raw_date = request.form.get(f'{pfx}bill_date')
            bill_date = None if (not raw_date or raw_date == 'Unknown Date') else raw_date

            if payer_id not in valid_user_ids:
                db.rollback()
                flash('Invalid payer selected.')
                return redirect(url_for('index'))

            cursor.execute(
                'INSERT INTO receipts (payer_id, filename, bill_date, group_id) VALUES (%s, %s, %s, %s) RETURNING id',
                (payer_id, filename, bill_date, current_user.group_id)
            )
            receipt_id = cursor.fetchone()['id']
            total = 0.0

            for key, value in request.form.items():
                if not key.startswith(pfx):
                    continue
                sub = key[len(pfx):]
                is_parsed = sub.startswith('assigned_to_')
                is_manual = sub.startswith('manual_assigned_to_')
                if not (is_parsed or is_manual):
                    continue
                assigned_to = value
                idx = sub.split('_')[-1]
                if assigned_to == 'excluded':
                    continue
                if assigned_to != 'shared' and assigned_to not in valid_user_ids:
                    db.rollback()
                    flash('Invalid item assignment.')
                    return redirect(url_for('index'))
                if is_manual:
                    desc = request.form.get(f'{pfx}manual_description_{idx}')
                    price_str = request.form.get(f'{pfx}manual_price_{idx}')
                else:
                    desc = request.form.get(f'{pfx}item_description_{idx}')
                    price_str = request.form.get(f'{pfx}item_price_{idx}')
                if desc and price_str:
                    price = float(price_str)
                    total += price
                    cursor.execute(
                        'INSERT INTO items (receipt_id, description, price, assigned_to) VALUES (%s, %s, %s, %s)',
                        (receipt_id, desc, price, assigned_to)
                    )

            cursor.execute('UPDATE receipts SET total = %s WHERE id = %s', (total, receipt_id))

        db.commit()
        msg = f'{receipt_count} bills saved!' if receipt_count > 1 else 'Bill saved!'
        flash(msg)
        return redirect(url_for('balances'))

    except Exception as e:
        if db:
            db.rollback()
        logging.error(f"Error in save_details: {e}")
        flash(f'An error occurred: {e}')
        return redirect(url_for('index'))

@app.route('/balances')
@login_required
def balances():
    """Displays the final calculated balances."""
    balances_data = calculate_balances_detailed(current_user.group_id)
    return render_template('balances.html', balance=balances_data)


@app.route('/settle', methods=['POST'])
@login_required
def settle():
    """Record a repayment for a suggested settlement. Booked as a receipt paid
    by the debtor with the amount assigned to the creditor, which exactly
    cancels the outstanding balance between them."""
    db = get_db()
    cursor = get_cursor()
    try:
        from_id = request.form.get('from_id')
        to_id = request.form.get('to_id')
        amount = float(request.form.get('amount', 0))

        cursor.execute('SELECT id FROM users WHERE group_id = %s', (current_user.group_id,))
        valid_user_ids = {row['id'] for row in cursor.fetchall()}
        if from_id not in valid_user_ids or to_id not in valid_user_ids or from_id == to_id:
            flash('Invalid settlement.')
            return redirect(url_for('balances'))
        if amount <= 0:
            flash('Invalid settlement amount.')
            return redirect(url_for('balances'))

        cursor.execute(
            'INSERT INTO receipts (payer_id, filename, bill_date, total, group_id) '
            'VALUES (%s, %s, CURRENT_DATE, %s, %s) RETURNING id',
            (from_id, 'Settlement', amount, current_user.group_id)
        )
        receipt_id = cursor.fetchone()['id']
        cursor.execute(
            'INSERT INTO items (receipt_id, description, price, assigned_to) VALUES (%s, %s, %s, %s)',
            (receipt_id, 'Settlement payment', amount, to_id)
        )
        db.commit()
        flash('Settlement recorded.')
    except Exception as e:
        db.rollback()
        flash(f'Error recording settlement: {e}')
    return redirect(url_for('balances'))

def get_bill_history(sort_by='upload_date', group_id=None):
    cursor = get_cursor()

    cursor.execute('SELECT id, name FROM users WHERE group_id = %s ORDER BY name', (group_id,))
    user_ids = [u['id'] for u in cursor.fetchall()]

    # Define the mapping of sort keys to SQL columns
    # We use a whitelist approach here to prevent SQL injection
    sort_options = {
        'upload_date': 'upload_date DESC',
        'bill_date': 'bill_date DESC',
        'total': 'total DESC'
    }
    order_clause = sort_options.get(sort_by, 'upload_date DESC')
    # Use the dynamic order clause
    query = f'SELECT id, upload_date, payer_id, filename, bill_date, total FROM receipts WHERE group_id = %s ORDER BY {order_clause}'
    cursor.execute(query, (group_id,))
    receipts = cursor.fetchall()

    # Fetch all items for these receipts in a single query (avoids N+1),
    # then group them by receipt_id in memory.
    receipt_ids = [r['id'] for r in receipts]
    items_by_receipt = {rid: [] for rid in receipt_ids}
    if receipt_ids:
        cursor.execute(
            'SELECT receipt_id, description, price, assigned_to FROM items WHERE receipt_id = ANY(%s)',
            (receipt_ids,)
        )
        for row in cursor.fetchall():
            items_by_receipt[row['receipt_id']].append(
                {'description': row['description'], 'assigned_to': row['assigned_to'], 'price': float(row['price'])}
            )

    bills_history = []

    for receipt in receipts:
        items = items_by_receipt[receipt['id']]
        totals_by_user = {uid: round(sum(item['price'] for item in items if item['assigned_to'] == uid), 2)
                           for uid in user_ids}
        shared_total = sum(item['price'] for item in items if item['assigned_to'] == 'shared')
        calculated_total = sum(totals_by_user.values()) + shared_total
        bills_history.append({
            'id': receipt['id'],
            'upload_date': receipt['upload_date'].strftime('%Y-%m-%d %H:%M') if receipt['upload_date'] else "N/A",
            'filename': receipt['filename'],
            'date': receipt['bill_date'] if receipt['bill_date'] else "Unknown",
            'payer': receipt['payer_id'],
            'items': items,
            'totals_by_user': totals_by_user,
            'shared_total': round(shared_total, 2),
            'total': round(calculated_total, 2)
        })

    return bills_history


@app.route('/history')
@login_required
def history():
    # Capture the sort preference from the URL query string (?sort_by=...)
    sort_by = request.args.get('sort_by', 'upload_date')
    
    # Pass the preference to the data fetcher
    receipts = get_bill_history(sort_by=sort_by, group_id=current_user.group_id)
    
    return render_template("history.html", receipts=receipts, current_sort=sort_by)


@app.route('/update_receipt_date', methods=['POST'])
@login_required
def update_receipt_date():
    db = None
    try:
        receipt_id = request.form.get('receipt_id')
        new_date = request.form.get('bill_date')

        db = get_db()
        cursor = get_cursor()

        # Update the bill_date for the specific receipt
        cursor.execute(
            'UPDATE receipts SET bill_date = %s WHERE id = %s AND group_id = %s',
            (new_date, receipt_id, current_user.group_id)
        )
        db.commit()
        flash(f'Date updated for Receipt #{receipt_id}!')
    except Exception as e:
        if db:
            db.rollback()
        flash(f'Error updating date: {e}')

    return redirect(url_for('history'))

@app.route('/add_missing_item', methods=['POST'])
@login_required
def add_missing_item():
    try:
        receipt_id = request.form.get('receipt_id')
        description = request.form.get('description')
        price = float(request.form.get('price'))
        assigned_to = request.form.get('assigned_to')

        db = get_db()
        cursor = get_cursor()

        # Make sure this receipt belongs to the current user's household
        cursor.execute('SELECT id FROM receipts WHERE id = %s AND group_id = %s', (receipt_id, current_user.group_id))
        if not cursor.fetchone():
            flash('Receipt not found.')
            return redirect(url_for('history'))

        # Assignment must be 'shared' or a member of this household
        cursor.execute('SELECT id FROM users WHERE group_id = %s', (current_user.group_id,))
        valid_user_ids = {row['id'] for row in cursor.fetchall()}
        if assigned_to != 'shared' and assigned_to not in valid_user_ids:
            flash('Invalid item assignment.')
            return redirect(url_for('history'))

        # Insert the new item into the database
        cursor.execute(
            'INSERT INTO items (receipt_id, description, price, assigned_to) VALUES (%s, %s, %s, %s)',
            (receipt_id, description, price, assigned_to)
        )
        db.commit()
        flash('Item added successfully!')
    except Exception as e:
        flash(f'Error adding item: {e}')
    
    return redirect(url_for('history'))

@app.route('/manual_payment', methods=['GET', 'POST'])
@login_required
def manual_payment():
    if request.method == 'POST':
        db = get_db()
        cursor = get_cursor()
        try:
            payer = request.form['payer']
            payee = request.form['payee']
            amount = float(request.form['amount'])
            description = request.form.get('description', 'Manual settlement')
            payment_date_str = request.form.get('payment_date')  # YYYY-MM-DD

            # Convert empty date to None for Postgres
            bill_date = payment_date_str if payment_date_str else None

            # Payer and payee must belong to this household ('shared' allowed for payee)
            cursor.execute('SELECT id FROM users WHERE group_id = %s', (current_user.group_id,))
            valid_user_ids = {row['id'] for row in cursor.fetchall()}
            if payer not in valid_user_ids or (payee != 'shared' and payee not in valid_user_ids):
                flash('Invalid payer or payee.')
                return redirect(url_for('manual_payment'))

            # Insert receipt; let Postgres auto-assign id
            cursor.execute(
                'INSERT INTO receipts (payer_id, filename, bill_date, total, group_id) VALUES (%s, %s, %s, %s, %s) RETURNING id',
                (payer, f"Manual_{description}", bill_date, amount, current_user.group_id)
            )
            receipt_id = cursor.fetchone()['id']

            # Insert corresponding item
            cursor.execute(
                'INSERT INTO items (receipt_id, description, price, assigned_to) VALUES (%s, %s, %s, %s)',
                (receipt_id, description, amount, payee)
            )

            db.commit()
            flash('Manual payment recorded successfully!')
            return redirect(url_for('history'))

        except Exception as e:
            db.rollback()
            flash(f"Error recording manual payment: {e}")
            return redirect(url_for('manual_payment'))

    return render_template('manual_payment.html')

@app.route('/remove_receipt', methods=['POST'])
@login_required
def remove_receipt():
    try:
        receipt_id = request.form.get('receipt_id')
        db = get_db()
        cursor = get_cursor()

        # Make sure this receipt belongs to the current user's household
        cursor.execute('SELECT id FROM receipts WHERE id = %s AND group_id = %s', (receipt_id, current_user.group_id))
        if not cursor.fetchone():
            flash('Receipt not found.')
            return redirect(url_for('history'))

        # Delete receipt and items
        cursor.execute('DELETE FROM items WHERE receipt_id = %s', (receipt_id,))
        cursor.execute('DELETE FROM receipts WHERE id = %s', (receipt_id,))
        db.commit()

        flash(f'Receipt #{receipt_id} removed successfully!')
    except Exception as e:
        db.rollback()
        flash(f'Error removing receipt: {e}')

    return redirect(url_for('history'))

@app.route('/group')
@login_required
def group_page():
    cursor = get_cursor()
    cursor.execute('SELECT id, name, invite_code FROM groups WHERE id = %s', (current_user.group_id,))
    grp = cursor.fetchone()
    cursor.execute('SELECT id, name FROM users WHERE group_id = %s ORDER BY name', (current_user.group_id,))
    members = cursor.fetchall()
    return render_template('group.html', group=grp, members=members)


@app.route('/group/remove_member', methods=['POST'])
@login_required
def remove_member():
    user_id = request.form.get('user_id', '').strip()
    if not user_id:
        flash('No user specified.')
        return redirect(url_for('group_page'))
    if user_id == current_user.id:
        flash("You can't remove yourself from the household.")
        return redirect(url_for('group_page'))

    db = get_db()
    cursor = get_cursor()

    # Confirm the target user actually belongs to this group
    cursor.execute(
        'SELECT id FROM users WHERE id = %s AND group_id = %s',
        (user_id, current_user.group_id)
    )
    if not cursor.fetchone():
        flash('Member not found in your household.')
        return redirect(url_for('group_page'))

    # Block removal if they paid for any receipts (would leave orphaned payer data)
    cursor.execute(
        'SELECT COUNT(*) AS c FROM receipts WHERE payer_id = %s AND group_id = %s',
        (user_id, current_user.group_id)
    )
    paid_count = cursor.fetchone()['c']
    if paid_count > 0:
        flash(f"Can't remove — this member paid for {paid_count} receipt(s). "
              f"Reassign those in History first.")
        return redirect(url_for('group_page'))

    # Reassign their personally-assigned items to shared, then detach from group
    cursor.execute(
        'UPDATE items SET assigned_to = %s '
        'WHERE assigned_to = %s AND receipt_id IN '
        '(SELECT id FROM receipts WHERE group_id = %s)',
        ('shared', user_id, current_user.group_id)
    )
    cursor.execute('UPDATE users SET group_id = NULL WHERE id = %s', (user_id,))
    db.commit()
    flash('Member removed and their items reassigned to shared.')
    return redirect(url_for('group_page'))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for("login"))


# Call init_db() when the app starts
with app.app_context():
     init_db()

if __name__ == '__main__':
    app.run(debug=True)
