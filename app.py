from flask import Flask, render_template, request, redirect, url_for, flash, g
import os
from dotenv import load_dotenv
import pytesseract
from PIL import Image
import io
import re
# For PDF handling (install: pip install pdfplumber)
import pdfplumber
import sqlite3

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a_secret_key_for_dev')
app.config['UPLOAD_FOLDER'] = 'uploads' # Directory to temporarily save uploaded files
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True) # Create folder if it doesn't exist

# If Tesseract is not in your PATH, you might need to specify its path
# pytesseract.pytesseract.tesseract_cmd = r'/path/to/tesseract.exe' # Windows example

DATABASE = 'billsplitter.db'

def init_db():
    with app.app_context(): # Use app context for Flask extensions
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            )
        ''')
        # Updated receipts table to include filename and bill_date
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                payer_id TEXT, -- References users.id
                filename TEXT NOT NULL,
                bill_date TEXT NOT NULL,
                FOREIGN KEY (payer_id) REFERENCES users(id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                price REAL NOT NULL,
                assigned_to TEXT NOT NULL, -- 'eser', 'david', 'shared'
                FOREIGN KEY (receipt_id) REFERENCES receipts(id)
            )
        ''')
        db.commit()

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row # Allows accessing columns by name
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def parse_bill_text(text):
    print("--- STARTING BILL PARSING ---")
    print("Raw OCR Text:")
    print(text)
    print("-----------------------------")

    """
    Parse a whole receipt text and return a list of items.
    Each item is a dict with description, price, and validity.
    """
    items = []
    
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue  # skip empty lines

        # Look for price at the end (e.g., 1,99 A / 2,49 AW / 2,02)
        match = re.search(r"(\d+,\d{2})\s*[A-Z]*$", line)
        if not match:
            continue  # skip non-matching lines like weight calculations

        price_str = match.group(1)
        price = float(price_str.replace(",", "."))

        # description = everything before the price
        description = line[:match.start()].strip()

        items.append({
            "description": description,
            "price": price,
            "is_valid": True
        })
    
    return items
    
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_bill():
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

        # Extract bill date from filename
        date_match = re.search(r'\d{4}-\d{2}-\d{2}', file.filename)
        bill_date = date_match.group(0) if date_match else 'Unknown Date'
        
        extracted_text = ""
        if file_extension in ['png', 'jpg', 'jpeg', 'gif']:
            try:
                img = Image.open(io.BytesIO(file_bytes))
                extracted_text = pytesseract.image_to_string(img)
            except Exception as e:
                flash(f"Error processing image: {e}")
        elif file_extension == 'pdf':
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    for page in pdf.pages:
                        extracted_text += page.extract_text() + "\n--PAGE BREAK--\n"
            except Exception as e:
                flash(f"Error processing PDF: {e}. Make sure the PDF is not an image.")
        else:
            flash('Unsupported file type.')
            return redirect(request.url)

        parsed_items = parse_bill_text(extracted_text)

        # Pass both parsed items and the extracted filename/date to the result template
        return render_template('result.html', parsed_items=parsed_items, filename=file.filename, bill_date=bill_date)
    return redirect(url_for('index'))

@app.route('/save_bill', methods=['POST'])
def save_bill():
    try:
        db = get_db()
        cursor = db.cursor()
        
        # Get the payer's name
        payer_list = request.form.getlist('payer_id')
        if len(payer_list) > 1:
            payer_id = 'both'
        elif len(payer_list) == 1:
            payer_id = payer_list[0]
        else:
            payer_id = None # Handle case where no payer is selected

        # Get the filename and bill date from the form
        filename = request.form.get('filename')
        bill_date = request.form.get('bill_date')

        # Insert the new receipt and get its ID
        cursor.execute('INSERT INTO receipts (payer_id, filename, bill_date) VALUES (?, ?, ?)', (payer_id, filename, bill_date))
        receipt_id = cursor.lastrowid

        # Loop through all the submitted form data to find and save the items
        for key, value in request.form.items():
            if key.startswith('item_description_'):
                index = key.split('_')[-1]
                description = value
                price = request.form.get(f'item_price_{index}')
                assigned_to = request.form.get(f'assigned_to_{index}')
                
                # Check if the item is NOT marked as excluded before saving
                if assigned_to != 'excluded' and description and price:
                    cursor.execute(
                        'INSERT INTO items (receipt_id, description, price, assigned_to) VALUES (?, ?, ?, ?)',
                        (receipt_id, description, float(price), assigned_to)
                    )

        db.commit()
        flash('Bill saved successfully!')
        return redirect(url_for('balances')) # Redirect to the balances page

    except Exception as e:
        db.rollback() # Rollback changes if an error occurred
        flash(f'An error occurred: {e}')
        return redirect(url_for('index'))

@app.route('/remove_bill', methods=['POST'])
def remove_bill():
    try:
        db = get_db()
        cursor = db.cursor()
        receipt_id = request.form.get('receipt_id')

        # Use a transaction for safety
        with db:
            cursor.execute('DELETE FROM items WHERE receipt_id = ?', (receipt_id,))
            cursor.execute('DELETE FROM receipts WHERE id = ?', (receipt_id,))
            
        flash('Bill removed successfully!')
    except Exception as e:
        flash(f'An error occurred while removing the bill: {e}')

    return redirect(url_for('history'))

@app.route('/balances')
def balances():
    balances_data = calculate_balances()
    return render_template('balances.html', balances=balances_data)

@app.route('/history')
def history():
    bills_history = get_bill_history()
    return render_template('history.html', bills_history=bills_history)

@app.route('/bill_details/<int:receipt_id>')
def bill_details(receipt_id):
    db = get_db()
    cursor = db.cursor()
    
    # Get the receipt details
    receipt = cursor.execute('SELECT * FROM receipts WHERE id = ?', (receipt_id,)).fetchone()
    
    # Get the items for this receipt
    items = cursor.execute('SELECT * FROM items WHERE receipt_id = ?', (receipt_id,)).fetchall()

    if not receipt:
        flash("Bill not found.")
        return redirect(url_for('history'))

    return render_template('bill_details.html', receipt=receipt, items=items)

def calculate_balances():
    db = get_db()
    cursor = db.cursor()

    # Get all items
    items = cursor.execute('SELECT price, assigned_to, receipt_id FROM items').fetchall()

    # Get who paid for each receipt
    receipt_payers = {}
    for row in cursor.execute('SELECT id, payer_id FROM receipts').fetchall():
        receipt_payers[row['id']] = row['payer_id']

    eser_total_personal = 0
    david_total_personal = 0
    shared_total = 0

    # Calculate total for each category regardless of who paid
    for item in items:
        if item['assigned_to'] == 'eser':
            eser_total_personal += item['price']
        elif item['assigned_to'] == 'david':
            david_total_personal += item['price']
        elif item['assigned_to'] == 'shared':
            shared_total += item['price']

    # Calculate actual money exchanged/spent by each person
    eser_paid_total = 0
    david_paid_total = 0

    # Sum up how much each person paid across all receipts
    for receipt_id, payer_id in receipt_payers.items():
        # Only sum up items for this receipt that are NOT excluded
        receipt_items = cursor.execute('SELECT price, assigned_to FROM items WHERE receipt_id = ? AND assigned_to != ?', (receipt_id, 'excluded')).fetchall()
        receipt_total = sum(item['price'] for item in receipt_items)
        
        if payer_id == 'eser':
            eser_paid_total += receipt_total
        elif payer_id == 'david':
            david_paid_total += receipt_total
        elif payer_id == 'both':
            eser_paid_total += receipt_total / 2
            david_paid_total += receipt_total / 2

    # Now, calculate who owes whom
    # Each person is responsible for their personal items + half of shared items
    eser_responsibility = eser_total_personal + (shared_total / 2)
    david_responsibility = david_total_personal + (shared_total / 2)

    # How much more did someone pay than their responsibility
    eser_net_balance = eser_paid_total - eser_responsibility
    david_net_balance = david_paid_total - david_responsibility

    # Who owes whom
    if eser_net_balance > david_net_balance: # Eser paid more than David, so David owes Eser
        amount_owed = eser_net_balance - david_net_balance
        final_message = "David owes Eser"
    elif david_net_balance > eser_net_balance: # David paid more than Eser, so Eser owes David
        amount_owed = david_net_balance - eser_net_balance
        final_message = "Eser owes David"
    else:
        amount_owed = 0
        final_message = "The bill is perfectly split!"

    # New dictionary to hold all relevant data for the template
    return {
        'eser_owes': 0 if final_message == "David owes Eser" else amount_owed,
        'david_owes': 0 if final_message == "Eser owes David" else amount_owed,
        'eser_total_personal': eser_total_personal,
        'david_total_personal': david_total_personal,
        'shared_total': shared_total,
        'final_message': final_message
    }
    
def get_bill_history():
    db = get_db()
    cursor = db.cursor()

    # Fetch all receipts
    receipts = cursor.execute('SELECT id, filename, bill_date FROM receipts ORDER BY bill_date DESC').fetchall()
    
    bills_history = []
    
    for receipt in receipts:
        receipt_id = receipt['id']
        # Fetch all items for this specific receipt
        items = cursor.execute('SELECT price, assigned_to FROM items WHERE receipt_id = ?', (receipt_id,)).fetchall()
        
        eser_total = 0
        david_total = 0
        shared_total = 0
        
        for item in items:
            if item['assigned_to'] == 'eser':
                eser_total += item['price']
            elif item['assigned_to'] == 'david':
                david_total += item['price']
            elif item['assigned_to'] == 'shared':
                shared_total += item['price']
                
        bills_history.append({
            'id': receipt['id'],
            'filename': receipt['filename'],
            'date': receipt['bill_date'],
            'eser_total': eser_total,
            'david_total': david_total,
            'shared_total': shared_total
        })
        
    return bills_history

# Call init_db() when the app starts
with app.app_context():
     init_db()

if __name__ == '__main__':
    app.run(debug=True) # debug=True is good for development, set to False for production
