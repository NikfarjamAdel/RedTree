
import os
import time
from pathlib import Path
from urllib.parse import quote_plus

import pyodbc
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError

REPO_ROOT = Path(__file__).resolve().parent.parent
def engine_setup(target="local"):
    load_dotenv(REPO_ROOT / ".env")
    prefix = "AZURE_" if target == "azure" else ""

    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={os.environ[prefix + 'MSSQL_HOST']},{os.environ[prefix + 'MSSQL_PORT']};"
        f"DATABASE={os.environ[prefix + 'MSSQL_DB']};"
        f"UID={os.environ[prefix + 'MSSQL_USER']};"
        f"PWD={os.environ[prefix + 'MSSQL_PASSWORD']};"
        f"TrustServerCertificate={'no' if target == 'azure' else 'yes'};"
    )

    engine = create_engine(f"mssql+pyodbc:///?odbc_connect={quote_plus(conn_str)}")
    return engine

def connect_db(target, attempts=None, wait_seconds=30):
    """Return a raw connection, retrying while a paused Azure database wakes.

    The first attempt is what triggers the resume, so it usually fails;
    later attempts succeed once the database is online. Local Docker
    doesn't pause, so it gets a single attempt by default.
    """
    if attempts is None:
        attempts = 5 if target == "azure" else 1

    for attempt in range(1, attempts + 1):
        try:
            db = engine_setup(target).raw_connection()
            cur = db.cursor()
            cur.execute("SELECT DB_NAME()")
            print("Writing to:", cur.fetchone()[0])
            cur.close()
            return db
        except (DBAPIError, pyodbc.Error) as e:
            if attempt == attempts:
                raise
            print(f"Database not ready (attempt {attempt}/{attempts}): "
                  f"{type(e).__name__}: {str(e)[:160]}; retrying in {wait_seconds}s")
            time.sleep(wait_seconds)