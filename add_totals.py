import sqlite3

DATABASE = 'billsplitter.db'

def update_receipt_totals():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    # 1. Add total column if it doesn't exist
    cursor.execute("PRAGMA table_info(receipts)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'total' not in columns:
        cursor.execute("ALTER TABLE receipts ADD COLUMN total REAL DEFAULT 0")
        print("Added 'total' column to receipts table.")

    # 2. Fetch all receipts
    cursor.execute("SELECT id FROM receipts")
    receipt_ids = [row[0] for row in cursor.fetchall()]

    for receipt_id in receipt_ids:
        # Fetch all items for this receipt
        cursor.execute("SELECT price FROM items WHERE receipt_id = ?", (receipt_id,))
        items = cursor.fetchall()
        total = sum(float(row[0]) for row in items)  # Ensure float sum

        # Update the receipt with the total
        cursor.execute("UPDATE receipts SET total = ? WHERE id = ?", (total, receipt_id))
        print(f"Receipt {receipt_id} updated with total: {total:.2f} €")

    conn.commit()
    conn.close()
    print("All receipts updated successfully.")

if __name__ == '__main__':
    update_receipt_totals()
