from flask import Flask, render_template, request, redirect, url_for, flash, g, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
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
#DATABASE = 'billsplitter.db'  # local sqlite fallback
app = Flask(__name__)
# The canvas environment provides the SECRET_KEY.
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a_secret_key_for_dev')
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

class User(UserMixin):
    def __init__(self, id):
        self.id = id
# Read credentials from environment variables
USERS = {
    os.environ.get("APP_USERNAME"): os.environ.get("APP_PASSWORD")
}

@login_manager.user_loader
def load_user(user_id):
    if user_id in USERS:
        return User(user_id)
    return None
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if username in USERS and USERS[username] == password:
            user = User(username)
            login_user(user)
            return redirect(url_for("history"))  # or wherever you want after login
        else:
            flash("Invalid username or password")
    return render_template("login.html")
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        # Handle user creation
        pass
    return render_template('register.html')


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
            cur = conn.cursor()
            cur.execute(pg_users)
            cur.execute(pg_receipts)
            cur.execute(pg_items)
            # default users (use ON CONFLICT DO NOTHING)
            cur.execute("INSERT INTO users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", ('eser', 'Eser'))
            cur.execute("INSERT INTO users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", ('david', 'David'))
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

def calculate_balances_detailed():
    db = get_db()
    cursor = get_cursor()

    # Get all items
    cursor.execute('SELECT price, assigned_to, receipt_id FROM items')
    items = cursor.fetchall()
    # Get who paid for each receipt
    cursor.execute('SELECT id, payer_id FROM receipts')
    receipt_rows = cursor.fetchall()
    receipt_payers = {row['id']: row['payer_id'] for row in receipt_rows}

    # Totals for each person
    eser_total_personal = sum(item['price'] for item in items if item['assigned_to'] == 'eser')
    david_total_personal = sum(item['price'] for item in items if item['assigned_to'] == 'david')
    shared_total = sum(item['price'] for item in items if item['assigned_to'] == 'shared')

    # Total actually paid by each
    eser_paid_total = 0
    david_paid_total = 0
    for receipt_id, payer_id in receipt_payers.items():
        receipt_total = sum(
            item['price'] 
            for item in items 
            if item['receipt_id'] == receipt_id and item['assigned_to'] != 'excluded'
        )
        if payer_id == 'eser':
            eser_paid_total += receipt_total
        elif payer_id == 'david':
            david_paid_total += receipt_total
        elif payer_id == 'both':
            eser_paid_total += receipt_total / 2
            david_paid_total += receipt_total / 2

    # Responsibility for each person
    eser_responsibility = eser_total_personal + (shared_total / 2)
    david_responsibility = david_total_personal + (shared_total / 2)

    eser_net = eser_paid_total - eser_responsibility
    david_net = david_paid_total - david_responsibility

    # Only one "owed amount"
    if eser_net > david_net:  # David owes Eser
        amount = eser_net
        balance = {'who_owes': 'david', 'to_whom': 'eser', 'amount': amount}
    elif david_net > eser_net:  # Eser owes David
        amount = david_net
        balance = {'who_owes': 'eser', 'to_whom': 'david', 'amount': amount}
    else:
        balance = {'who_owes': 'Nobody', 'to_whom': 'Nobody', 'amount': 0}


    return {
    'eser_total_personal': eser_total_personal,
    'david_total_personal': david_total_personal,
    'shared_total': shared_total,
    'eser_paid_total': eser_paid_total,
    'david_paid_total': david_paid_total,
    'eser_responsibility': eser_responsibility,
    'david_responsibility': david_responsibility,
    # Only one net balance difference is needed to avoid double counting
    'who_owes': balance['who_owes'],
    'to_whom': balance['to_whom'],
    'amount': balance['amount']
}



@app.route('/')
@login_required
def index():
    """Main route to upload a bill."""
    return render_template('index.html')

@app.route('/upload', methods=['GET','POST'])
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
def save_details():
    """Saves the assigned bill items to the database and redirects to the confirmation page."""
    try:
        db = get_db()
        cursor = get_cursor()

        payer_id = request.form['payer_id']
        filename = request.form.get('filename')  # Make sure your form passes this
        bill_date = request.form.get('bill_date')  # Also passed from form

         # Insert the new receipt with RETURNING id to get receipt_id
        cursor.execute(
            'INSERT INTO receipts (payer_id, filename, bill_date) VALUES (%s, %s, %s) RETURNING id',
            (payer_id, filename, bill_date)
        )
        receipt_id = cursor.fetchone()['id']

        total = 0.0

        # Loop through all the submitted form data for items
        for key, value in request.form.items():
            if key.startswith('assigned_to_'):
                index_str = key.split('_')[-1]
                assigned_to = value

                # Only save items that are not excluded
                if assigned_to != 'excluded':
                    description = request.form.get(f'item_description_{index_str}')
                    price_str = request.form.get(f'item_price_{index_str}')

                    if description and price_str:
                        price = float(price_str)
                        total += price
                        cursor.execute(
                            'INSERT INTO items (receipt_id, description, price, assigned_to) VALUES (%s, %s, %s, %s)',
                            (receipt_id, description, price, assigned_to)
                        )

        # Update the total in the receipts table
        cursor.execute('UPDATE receipts SET total = %s WHERE id = %s', (total, receipt_id))

        db.commit()
        flash('Bill saved successfully!')
        return redirect(url_for('balances'))

    except Exception as e:
        db.rollback()
        flash(f'An error occurred: {e}')
        print(f"Error saving receipt: {e}")
        return redirect(url_for('index'))


    except Exception as e:
        db.rollback()
        flash(f'An error occurred: {e}')
        print(f'Error saving details: {e}')
        return redirect(url_for('index'))

@app.route('/balances')
@login_required
def balances():
    """Displays the final calculated balances."""
    balances_data = calculate_balances_detailed()
    return render_template('balances.html', balance=balances_data)

def get_bill_history():
    db = get_db()
    cursor = get_cursor()

    cursor.execute('SELECT id, upload_date, payer_id, filename, bill_date, total FROM receipts ORDER BY bill_date DESC')
    receipts = cursor.fetchall()
    
    bills_history = []
    
    for receipt in receipts:
        receipt_id = receipt['id']
        cursor.execute(
            'SELECT description, price, assigned_to FROM items WHERE receipt_id = %s', 
            (receipt['id'],)
        )
        items = [{'description': row['description'], 'assigned_to': row['assigned_to'], 'price': float(row['price'])} 
                 for row in cursor.fetchall()]
        eser_total = sum(item['price'] for item in items if item['assigned_to'] == 'eser')
        david_total = sum(item['price'] for item in items if item['assigned_to'] == 'david')
        shared_total = sum(item['price'] for item in items if item['assigned_to'] == 'shared')
        
        bills_history.append({
            'id': receipt['id'],
            'filename': receipt['filename'],
            'date': receipt['bill_date'],
            'payer': receipt['payer_id'],
            'items': items,
            'eser_total': round(eser_total, 2),
            'david_total': round(david_total, 2),
            'shared_total': round(shared_total, 2),
            'total': float(receipt['total'])
        })
        
    return bills_history


@app.route('/history')
@login_required
def history():
    receipts = get_bill_history()
    return render_template("history.html", receipts=receipts)
@app.route('/manual_payment', methods=['GET', 'POST'])


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
            
            cursor.execute(
                'INSERT INTO receipts (payer_id, filename, bill_date) VALUES (%s, %s, %s) RETURNING id',
                (payer, f"Manual_{description}", payment_date_str)
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
def remove_receipt():
    try:
        receipt_id = request.form.get('receipt_id')
        db = get_db()
        cursor = get_cursor()

        # Delete receipt and items
        cursor.execute('DELETE FROM items WHERE receipt_id = %s', (receipt_id,))
        cursor.execute('DELETE FROM receipts WHERE id = %s', (receipt_id,))
        db.commit()

        flash(f'Receipt #{receipt_id} removed successfully!')
    except Exception as e:
        db.rollback()
        flash(f'Error removing receipt: {e}')

    return redirect(url_for('history'))
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
