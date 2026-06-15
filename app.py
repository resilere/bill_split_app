from flask import Flask, render_template, request, redirect, url_for, flash, g, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
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
from pdf2image import convert_from_bytes 
#import sqlite3
import psycopg2
import psycopg2.extras
from urllib.parse import urlparse
import uuid
from datetime import datetime
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
# The canvas environment provides the SECRET_KEY.
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a_secret_key_for_dev')
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

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
            'INSERT INTO users (id, name, email, auth_uid, group_id) VALUES (%s, %s, %s, %s, %s)',
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
        
class DBConnWrapper:
    """Wrap a DB connection so app code can use .cursor(), .commit(), .rollback(), .close()."""
    def __init__(self, conn, is_sqlite=False):
        self._conn = conn
        self._is_sqlite = is_sqlite

    def cursor(self):
        if self._is_sqlite:
            return self._conn.cursor()
        # For psycopg2 return a RealDictCursor so rows are accessible by key like sqlite Row
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


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
    # This regex captures everything up to the price and then ignores any characters after it.
    item_line_pattern = re.compile(r'(.+?)\s*(-?\d+[.,]\d{2})\s*.*?$')

    # Keywords to filter out non-item lines (like totals, taxes, etc.)
    filter_keywords = ['gesamt', 'summe', 'zwischensumme', 'steuer', 'mwst', 'bar', 'bargeld', 'karte', 'zahlung', 'betrag', 'rueckgeld', 'saldo', 'rabatt', 'guthaben']
    
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        
        # Stop parsing if 'summe' is found
        if 'summe' in line.lower():
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

    # 1. Remove noise and improve contrast
    img = cv2.bilateralFilter(img, 9, 75, 75)

    # 2. Adaptive thresholding (binarize text)
    img = cv2.adaptiveThreshold(
        img, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31, 2
    )

    # 3. Optional: deskew (fix tilted receipts)
    coords = np.column_stack(np.where(img > 0))
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    (h, w) = img.shape[:2]
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

    # Get all users involved in the split
    cursor.execute('SELECT id, name FROM users WHERE group_id = %s ORDER BY name', (group_id,))
    users = cursor.fetchall()
    user_ids = [u['id'] for u in users]
    n = len(users)

    # Get all items belonging to this group's receipts
    cursor.execute(
        'SELECT i.price, i.assigned_to, i.receipt_id FROM items i '
        'JOIN receipts r ON r.id = i.receipt_id WHERE r.group_id = %s',
        (group_id,)
    )
    items = cursor.fetchall()
    for item in items:
        item['price'] = float(item['price'])

    # Get who paid for each receipt
    cursor.execute('SELECT id, payer_id FROM receipts WHERE group_id = %s', (group_id,))
    receipt_payers = {row['id']: row['payer_id'] for row in cursor.fetchall()}

    shared_total = sum(item['price'] for item in items if item['assigned_to'] == 'shared')
    shared_share = (shared_total / n) if n else 0.0

    balances = []
    for u in users:
        uid = u['id']
        personal_total = sum(item['price'] for item in items if item['assigned_to'] == uid)

        paid_total = 0.0
        for receipt_id, payer_id in receipt_payers.items():
            receipt_total = sum(
                item['price']
                for item in items
                if item['receipt_id'] == receipt_id and item['assigned_to'] != 'excluded'
            )
            if payer_id == uid:
                paid_total += receipt_total
            elif payer_id == 'both' and n == 2:
                # Legacy 2-person receipts only; new receipts use a single payer id.
                paid_total += receipt_total / 2

        responsibility = personal_total + shared_share
        net = paid_total - responsibility

        balances.append({
            'id': uid,
            'name': u['name'],
            'personal_total': round(personal_total, 2),
            'shared_share': round(shared_share, 2),
            'paid_total': round(paid_total, 2),
            'responsibility': round(responsibility, 2),
            'net': round(net, 2),
        })

    settlements = compute_settlements(balances)

    return {
        'users': balances,
        'shared_total': round(shared_total, 2),
        'settlements': settlements,
    }



@app.route('/')
@login_required
def index():
    """Main route to upload a bill."""
    return render_template('index.html')

@app.route('/upload', methods=['GET','POST'])
@login_required
def upload_bill():
    """Handles file uploads and performs OCR, then sends to bill details page."""
    if 'bill_image' not in request.files:
        flash('No file part')
        logging.warning("Upload attempt without file part")
        return redirect(request.url)
    file = request.files['bill_image']
    if file.filename == '':
        flash('No selected file')
        logging.warning("No selected file")
        return redirect(request.url)
    if file:
        file_bytes = file.read()
        file_extension = file.filename.split('.')[-1].lower()
        unique_filename = f"{uuid.uuid4()}"
        image_path_for_display = None
        extracted_text = ""
         # Extract bill date from filename
        date_match = re.search(r'\d{4}-\d{2}-\d{2}', file.filename)
        bill_date = date_match.group(0) if date_match else 'Unknown Date'
        logging.info(f"Received upload: {file.filename} ({len(file_bytes)/1024:.1f} KB)")
        custom_config = r'--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz€.,:-/'
        try:
            if file_extension in ['png', 'jpg', 'jpeg', 'gif']:
                logging.info("Processing image upload...")
                img = Image.open(io.BytesIO(file_bytes))
                #img = img.convert('RGB')
                #img.thumbnail((2000, 2000))
                processed_img = preprocess_image(img)
                image_path_for_display = f"{unique_filename}.png"
                processed_img.save(os.path.join(app.config['UPLOAD_FOLDER'], image_path_for_display))
                logging.info("Running OCR on image...")
                extracted_text = pytesseract.image_to_string(processed_img, config=custom_config)
                
                logging.info("OCR complete for image.")

            elif file_extension == 'pdf':
                logging.info("Processing PDF upload with pdfplumber...")
                try:
                    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                        for i, page in enumerate(pdf.pages):
                            page_text = page.extract_text()
                            if page_text:
                                extracted_text += page_text + "\n--PAGE BREAK--\n"
                        
                        # Save first page as image preview
                        first_page = pdf.pages[0]
                        image_path_for_display = f"{unique_filename}.png"
                        first_page.to_image(resolution=150).save(os.path.join(app.config['UPLOAD_FOLDER'], image_path_for_display))
                        logging.info(f"PDF processed, extracted text from {len(pdf.pages)} pages.")
                except Exception as e:
                    flash(f"Error processing PDF: {e}.")
                    logging.exception("PDF processing error")
                    return redirect(request.url)

            else:
                flash('Unsupported file type.')
                logging.error(f"Unsupported file type: {file_extension}")
                return redirect(request.url)

        except Exception as e:
            flash(f"Error processing PDF: {e}. Make sure the PDF is not an image.")   
            logging.exception("Error during file processing")
            return redirect(request.url)

        parsed_items = parse_bill_text(extracted_text)

        # Calculate a total sum to display on the result page
        total_sum = sum(item['price'] for item in parsed_items)

        # Store the image path in a session or pass it hidden if needed for later, 
        # but for now we just pass it to the result template.
        return render_template('bill_details.html',
                               parsed_items=parsed_items, 
                               filename=file.filename,
                               total_sum=total_sum,
                               bill_date=bill_date,
                               image_path=url_for('uploaded_file', filename=image_path_for_display))
    return redirect(url_for('index'))

@app.route('/uploads/<filename>')
@login_required
def uploaded_file(filename):
    """Securely serves uploaded files from the UPLOAD_FOLDER."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/save_details', methods=['POST'])
@login_required
def save_details():
    """Saves the assigned bill items to the database and redirects to the confirmation page."""
    try:
        db = get_db()
        cursor = get_cursor()

        payer_id = request.form['payer_id']
        filename = request.form.get('filename')  # Make sure your form passes this
        raw_date = request.form.get('bill_date')  # Also passed from form
        # If it's invalid, set it to None so Postgres accepts it as a NULL value
        if not raw_date or raw_date == "Unknown Date":
            bill_date = None 
        else:
            bill_date = raw_date
        # Insert the new receipt with RETURNING id to get receipt_id
        cursor.execute(
            'INSERT INTO receipts (payer_id, filename, bill_date, group_id) VALUES (%s, %s, %s, %s) RETURNING id',
            (payer_id, filename, bill_date, current_user.group_id)
        )
        receipt_id = cursor.fetchone()['id']

        total = 0.0

        # Loop through ALL form items
        for key, value in request.form.items():
            # Check for BOTH standard parsed items AND manual items
            is_parsed = key.startswith('assigned_to_')
            is_manual = key.startswith('manual_assigned_to_')

            if is_parsed or is_manual:
                assigned_to = value
                index_str = key.split('_')[-1]

                if assigned_to != 'excluded':
                    # Determine prefix based on item type
                    prefix = "manual_" if is_manual else ""
                    
                    description = request.form.get(f'{prefix}item_description_{index_str}')
                    # Note: Your HTML uses "manual_description_", but parsed uses "item_description_"
                    # To be safe, let's handle the specific manual naming:
                    if is_manual:
                        description = request.form.get(f'manual_description_{index_str}')
                        price_str = request.form.get(f'manual_price_{index_str}')
                    else:
                        description = request.form.get(f'item_description_{index_str}')
                        price_str = request.form.get(f'item_price_{index_str}')

                    if description and price_str:
                        price = float(price_str)
                        total += price
                        cursor.execute(
                            'INSERT INTO items (receipt_id, description, price, assigned_to) VALUES (%s, %s, %s, %s)',
                            (receipt_id, description, price, assigned_to)
                        )

        cursor.execute('UPDATE receipts SET total = %s WHERE id = %s', (total, receipt_id))
        db.commit()
        flash('Bill saved successfully!')
        return redirect(url_for('balances'))

    except Exception as e:
        if db: db.rollback()
        logging.error(f"Error in save_details: {e}")
        flash(f'An error occurred: {e}')
        return redirect(url_for('index'))

@app.route('/balances')
@login_required
def balances():
    """Displays the final calculated balances."""
    balances_data = calculate_balances_detailed(current_user.group_id)
    return render_template('balances.html', balance=balances_data)

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

    bills_history = []

    for receipt in receipts:
        # Get items for this receipt
        cursor.execute(
            'SELECT description, price, assigned_to FROM items WHERE receipt_id = %s',
            (receipt['id'],)
        )
        items = [{'description': row['description'], 'assigned_to': row['assigned_to'], 'price': float(row['price'])}
                 for row in cursor.fetchall()]
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
