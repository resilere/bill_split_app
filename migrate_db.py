import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import quote
import os

# -------------------------------
# CONFIGURATION
# -------------------------------

# Path to your SQLite DB
SQLITE_PATH = "billsplitter.db"

# Supabase Postgres connection
PG_USER = "postgres.isoasuiofaytylemyvvb"
PG_PASSWORD = os.environ.get("DB_PASSWORD", "[password]")
PG_HOST = "aws-1-eu-west-1.pooler.supabase.com"  # replace with your Supabase pooler host
PG_PORT = "5432"
PG_DB = "postgres"

# URL-encode password to handle special characters
encoded_password = quote(PG_PASSWORD)
PG_CONN_STRING = f"postgresql://{PG_USER}:{encoded_password}@{PG_HOST}:{PG_PORT}/{PG_DB}"

# -------------------------------
# CONNECT TO DATABASES
# -------------------------------

# Connect to SQLite
sqlite_conn = sqlite3.connect(SQLITE_PATH)
sqlite_cur = sqlite_conn.cursor()

# Connect to Supabase Postgres
pg_conn = psycopg2.connect(PG_CONN_STRING, sslmode="require")
pg_cur = pg_conn.cursor(cursor_factory=RealDictCursor)

# -------------------------------
# MIGRATION FUNCTIONS
# -------------------------------

def migrate_table(table_name, columns, conflict_key=None):
    col_str = ", ".join(columns)
    sqlite_cur.execute(f"SELECT {col_str} FROM {table_name}")
    rows = sqlite_cur.fetchall()
    
    if not rows:
        print(f"No rows found in {table_name}, skipping.")
        return

    print(f"Migrating {len(rows)} rows from {table_name}...")
    for row in rows:
        row = list(row)
        # Convert empty strings in bill_date (or any other DATE/TIMESTAMP columns) to None
        for i, col in enumerate(columns):
            if col in ["bill_date", "upload_date"] and row[i] == "":
                row[i] = None
        row = tuple(row)

        if conflict_key:
            on_conflict = f"ON CONFLICT ({conflict_key}) DO NOTHING"
        else:
            on_conflict = ""
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f"INSERT INTO {table_name} ({col_str}) VALUES ({placeholders}) {on_conflict}"
        pg_cur.execute(sql, row)


# -------------------------------
# MIGRATE TABLES
# -------------------------------

# 1. users table
migrate_table("users", ["id", "name"], conflict_key="id")

# 2. receipts table

migrate_table("receipts", ["id", "upload_date", "payer_id", "filename", "bill_date", "total"], conflict_key="id")

# 3. items table
migrate_table("items", ["id", "receipt_id", "description", "price", "assigned_to"], conflict_key="id")

# -------------------------------
# COMMIT AND CLOSE
# -------------------------------

pg_conn.commit()
pg_cur.close()
pg_conn.close()
sqlite_cur.close()
sqlite_conn.close()

print("Migration completed successfully!")
