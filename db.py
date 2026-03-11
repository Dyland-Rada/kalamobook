import os

DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = DATABASE_URL and DATABASE_URL.startswith("postgres")

if IS_POSTGRES:
    import psycopg2
    from psycopg2.extras import DictCursor
else:
    import sqlite3

def get_connection():
    if IS_POSTGRES:
        # psycopg2 accepts both postgres:// and postgresql://
        return psycopg2.connect(DATABASE_URL, cursor_factory=DictCursor)
    else:
        conn = sqlite3.connect("books.db")
        # Ensure row factory for dictionary-like access if needed
        conn.row_factory = sqlite3.Row
        return conn

def execute_query(cursor, query, params=()):
    """
    Translates SQLite '?' parameters to PostgreSQL '%s' if necessary.
    """
    if IS_POSTGRES:
        query = query.replace("?", "%s")
    
    cursor.execute(query, params)

def get_pandas_engine():
    """
    Returns a SQLAlchemy engine string suitable for pandas read_sql.
    """
    if IS_POSTGRES:
        # Pandas requires postgresql:// instead of postgres:// in sqlalchemy
        return DATABASE_URL.replace("postgres://", "postgresql://")
    else:
        return "sqlite:///books.db"
