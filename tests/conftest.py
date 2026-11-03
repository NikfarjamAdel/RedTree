"""Shared test setup: placeholder settings, so modules that read settings
when imported can be imported without a .env file (e.g. in CI); real values,
when present, are left alone. And the local database, for tests that run SQL."""
import os
import socket

import pytest
from dotenv import load_dotenv
from engine_setup import REPO_ROOT, connect_db

for name in ("AZURE_DOCINTEL_ENDPOINT", "AZURE_DOCINTEL_KEY",
             "AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_CONTAINER",
             "AZURE_EXTRACTION_CONTAINER", "AZURE_ICAL_CONTAINER",
             "AZURE_EXPORTS_CONTAINER"):
    os.environ.setdefault(name, "test")

# The local database, for tests that run real SQL. Each test gets a
# connection that is rolled back afterwards; skipped when the database
# isn't reachable (e.g. Docker is stopped), so the pure tests run anywhere.


def local_database_reachable():
    load_dotenv(REPO_ROOT / ".env")
    host, port = os.environ.get("MSSQL_HOST"), os.environ.get("MSSQL_PORT")
    if not (host and port):
        return False
    try:
        socket.create_connection((host, int(port)), timeout=1).close()
        return True
    except OSError:
        return False


@pytest.fixture
def local_db():
    if not local_database_reachable():
        pytest.skip("local database not reachable")
    db = connect_db("local")
    try:
        yield db
    finally:
        db.rollback()
        db.close()
