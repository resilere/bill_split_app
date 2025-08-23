from flask import Flask, render_template, request, redirect, url_for, flash, g
import os
from dotenv import load_dotenv
import pytesseract
from PIL import Image
import io
import re
from pdf2image import convert_from_bytes # You'll need poppler-utils on your system for this
import sqlite3

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
# Change for production!
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a_secret_key_for_dev')
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# If Tesseract is not in your PATH, you might need to specify its path
# pytesseract.pytesseract.tesseract_cmd = r'/path/to/tesseract.exe'

DATABASE = 'billsplitter.db'

def init_db():
    """Initializes the SQLite database with necessary tables."""
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                payer_id TEXT,
                FOREIGN KEY (payer_id) REFERENCES users(id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                price REAL NOT NULL,
                assigned_to TEXT NOT NULL,
                FOREIGN KEY (receipt_id) REFERENCES receipts(id)
            )
        ''')
        # Insert default users if they don't exist
        try:
            cursor.execute("INSERT OR IGNORE INTO users (id, name) VALUES (?, ?)", ('eser', 'Eser'))
            cursor.execute("INSERT OR IGNORE INTO users (id, name) VALUES (?, ?)", ('david', 'David'))
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            print("Users 'Eser' and 'David' already exist.")

def get_db():
    """Establishes a new database connection or returns the existing one."""
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    """Closes the database connection at the end of the request."""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def parse_bill_text(text):
    """
    Parses bill text to extract items and their prices.
    This version is more flexible and handles both comma and period decimal separators,
    as well as tax codes that include an asterisk, like '*B', or a single letter like 'A'.
    """
    items = []
    # This regex is now more flexible. It captures everything up to the price
    # and then uses `.*?` to ignore any characters after the price.
    item_line_pattern = re.compile(r'(.+?)\s*(-?\d+[.,]\d{2})\s*.*?$')

    # Keywords to filter out non-item lines (like totals, taxes, etc.)
    # These are in German for your German receipts.
    filter_keywords = ['gesamt', 'summe', 'zwischensumme', 'steuer', 'mwst', 'bar', 'bargeld', 'karte', 'zahlung', 'betrag', 'rueckgeld', 'saldo', 'rabatt', 'guthaben']
    
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        
        # New logic: Stop parsing if 'summe' is found
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
                items.append({'description': description, 'price': price})
            except ValueError:
                continue
    return items

def calculate_balances():
    """
    Calculates the final balances between Eser and David based on all saved receipts.
    """
    db = get_db()
    cursor = db.cursor()

    # Get all items from the database
    items = cursor.execute('SELECT price, assigned_to, receipt_id FROM items').fetchall()
    
    # Get who paid for each receipt
    receipt_payers = {}
    for row in cursor.execute('SELECT id, payer_id FROM receipts').fetchall():
        receipt_payers[row['id']] = row['payer_id']

    eser_total_personal = 0
    david_total_personal = 0
    shared_total = 0

    # Sum up individual and shared item costs
    for item in items:
        if item['assigned_to'] == 'eser':
            eser_total_personal += item['price']
        elif item['assigned_to'] == 'david':
            david_total_personal += item['price']
        elif item['assigned_to'] == 'shared':
            shared_total += item['price']
    
    eser_paid_total = 0
    david_paid_total = 0
    
    # Sum up how much each person paid across all receipts
    for receipt_id, payer_id in receipt_payers.items():
        receipt_total = sum(item['price'] for item in items if item['receipt_id'] == receipt_id)
        if payer_id == 'eser':
            eser_paid_total += receipt_total
        elif payer_id == 'david':
            david_paid_total += receipt_total

    # Calculate net responsibilities
    eser_responsibility = eser_total_personal + (shared_total / 2)
    david_responsibility = david_total_personal + (shared_total / 2)

    # Determine the final balance
    eser_balance = eser_paid_total - eser_responsibility
    david_balance = david_paid_total - david_responsibility

    # Return who owes whom
    if eser_balance > 0:
        return {'who_owes': 'David', 'amount': abs(eser_balance), 'to_whom': 'Eser'}
    elif david_balance > 0:
        return {'who_owes': 'Eser', 'amount': abs(david_balance), 'to_whom': 'David'}
    else:
        return {'who_owes': 'Nobody', 'amount': 0, 'to_whom': 'Nobody'}

@app.route('/')
def index():
    """Main route to upload a bill."""
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_bill():
    """Handles file uploads and performs OCR, then sends to bill details page."""
    if 'bill_image' not in request.files:
        flash('No file part')
        return redirect(request.url)
    file = request.files['bill_image']
    if file.filename == '':
        flash('No selected file')
        return redirect(request.url)
    if file:
        file_bytes = file.read()
        file_extension = file.filename.split('.')[-1].lower()
        
        extracted_text = ""
        if file_extension in ['png', 'jpg', 'jpeg', 'gif']:
            try:
                img = Image.open(io.BytesIO(file_bytes))
                extracted_text = pytesseract.image_to_string(img)
            except Exception as e:
                flash(f"Error processing image: {e}")
                print(f"Error processing image: {e}")
        elif file_extension == 'pdf':
            try:
                images = convert_from_bytes(file_bytes)
                for i, img in enumerate(images):
                    extracted_text += pytesseract.image_to_string(img) + "\n--PAGE BREAK--\n"
            except Exception as e:
                flash(f"Error processing PDF: {e}. Make sure poppler-utils is installed.")
                print(f"Error processing PDF: {e}. Make sure poppler-utils is installed.")
        else:
            flash('Unsupported file type.')
            print('Unsupported file type.')
            return redirect(request.url)

        # --- DEBUGGING PRINTS ---
        print("\n--- Tesseract Extracted Text ---")
        print(extracted_text)
        print("------------------------------\n")

        parsed_items = parse_bill_text(extracted_text)

        print("\n--- Parsed Items ---")
        print(parsed_items)
        print("--------------------\n")
        
        # Calculate a total sum to display on the result page
        total_sum = sum(item['price'] for item in parsed_items)

        # Render the bill details page with the parsed items
        return render_template('bill_details.html', 
                               parsed_items=parsed_items, 
                               filename=file.filename,
                               total_sum=total_sum)
    return redirect(url_for('index'))

@app.route('/save_details', methods=['POST'])
def save_details():
    """Saves the assigned bill items to the database and redirects to the confirmation page."""
    try:
        db = get_db()
        cursor = db.cursor()

        payer_id = request.form['payer_id']

        # Insert the new receipt and get the ID
        cursor.execute('INSERT INTO receipts (payer_id) VALUES (?)', (payer_id,))
        receipt_id = cursor.lastrowid

        # Loop through all the submitted form data for items
        for key, value in request.form.items():
            if key.startswith('assigned_to_'):
                # Extract the index from the key (e.g., 'assigned_to_0' -> '0')
                index_str = key.split('_')[-1]
                
                # Check the value of the radio button. If it's not 'excluded', save the item.
                if value != 'excluded':
                    description = request.form.get(f'item_description_{index_str}')
                    price = request.form.get(f'item_price_{index_str}')
                    assigned_to = value

                    if description and price and assigned_to:
                        cursor.execute(
                            'INSERT INTO items (receipt_id, description, price, assigned_to) VALUES (?, ?, ?, ?)',
                            (receipt_id, description, float(price), assigned_to)
                        )

        db.commit()
        flash('Bill saved successfully!')
        # Redirect to the confirmation page
        return redirect(url_for('confirm'))

    except Exception as e:
        db.rollback()
        flash(f'An error occurred: {e}')
        return redirect(url_for('index'))

@app.route('/confirm')
def confirm():
    """Displays a simple confirmation message."""
    return render_template('confirm.html')

@app.route('/balances')
def balances():
    """Displays the final calculated balances."""
    balance_result = calculate_balances()
    return render_template('balances.html', balance=balance_result)

@app.route('/history')
def history():
    """Displays a list of all past receipts."""
    db = get_db()
    cursor = db.cursor()
    # Fetch all receipts and their items
    receipts_db = cursor.execute('SELECT * FROM receipts ORDER BY upload_date DESC').fetchall()
    
    # Create a list of dictionaries to pass to the template
    all_receipts = []
    for receipt in receipts_db:
        items = cursor.execute('SELECT * FROM items WHERE receipt_id = ?', (receipt['id'],)).fetchall()
        receipt_dict = {
            'id': receipt['id'],
            'payer': receipt['payer_id'],
            'date': receipt['upload_date'],
            'items': items
        }
        all_receipts.append(receipt_dict)

    return render_template('history.html', receipts=all_receipts)


# Call init_db() when the app starts
with app.app_context():
     init_db()

if __name__ == '__main__':
    app.run(debug=True)
