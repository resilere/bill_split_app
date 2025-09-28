# read_db.py
import sqlite3

DB_PATH = "billsplitter.db"

def print_table(cursor, table_name):
    print(f"\n=== {table_name.upper()} ===")
    rows = cursor.execute(f"SELECT * FROM {table_name}").fetchall()
    col_names = [description[0] for description in cursor.description]

    if not rows:
        print("(empty)")
        return

    # pretty print rows
    for row in rows:
        row_dict = dict(zip(col_names, row))
        print(row_dict)

def main():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # find all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [t[0] for t in cursor.fetchall()]

    if not tables:
        print("No tables found in database.")
        return

    for table in tables:
        print_table(cursor, table)

    conn.close()

if __name__ == "__main__":
    main()
