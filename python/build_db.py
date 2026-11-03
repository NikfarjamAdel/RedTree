"""Build a database from nothing, then check it, and optionally test it.

  --clean    schema, reference data, staging, views and an empty star schema,
             then the checks. For someone starting with their own property:
             next run setup_property.py with your config, then pipeline.py.
  --example  as --clean, plus the example property
             (config/property.example.json, through setup_property.py), then
             the constraint and scenario tests. This is what CI runs.
  --demo     as --example, plus a year of demo data for the example listing
             (make_demo_data.py): bookings as Airbnb exports loaded through
             load_airbnb.py --from-dir, expenses and loan interest inserted
             directly. The same every time (fixed seed).
All run the analysis queries once, to prove they still work.

  --azure    with --clean only: build a BRAND-NEW Azure SQL database (the
             AZURE_* settings) instead of the local one. Refused unless the
             database has no user tables at all, so it can never overwrite
             the real database; the real one is migrated, never rebuilt.
             No example property or demo data in Azure.

Every build starts from nothing: every user view and table in the database
is dropped first, then the schema is created. So this script:
  - refuses to build into Azure SQL Database, except --clean --azure into
    a database with no tables,
  - needs --yes before rebuilding a local database, which loses its data.

Usage:
    python3 python/build_db.py --clean   --yes
    python3 python/build_db.py --example --yes
    python3 python/build_db.py --demo    --yes
    python3 python/build_db.py --clean --azure --yes
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import make_demo_data
from engine_setup import connect_db
from sql_runner import run_checks, run_sql_file, run_test_file

HERE = Path(__file__).resolve().parent
SQL = HERE.parent / "sql"

# The star tables are created with the rest, so the first load can fill them
BUILD = ("01_schema.sql", "03_seed_reference.sql", "40_stage.sql", "20_views.sql",
         "60_star_schema.sql")
STAR_LOAD = "61_star_load.sql"
CHECKS = ("30_data_quality.sql", "31_load_assertions.sql", "63_star_checks.sql")
TESTS = ("33_pipeline_constraint_tests.sql", "34_ical_change_tests.sql")
QUERIES = ("10_analysis.sql", "62_star_queries.sql")    # run once; results not checked

AZURE_SQL_DATABASE = 5    # SERVERPROPERTY('EngineEdition') for Azure SQL Database


def refuse_azure(db):
    cur = db.cursor()
    cur.execute("SELECT CAST(SERVERPROPERTY('EngineEdition') AS INT), @@SERVERNAME, DB_NAME()")
    edition, server, database = cur.fetchone()
    cur.close()
    print(f"Building {database} on {server}")
    if edition == AZURE_SQL_DATABASE:
        sys.exit("Refusing to build: this is Azure SQL Database, the system of record.")


def refuse_unless_empty(db):
    """--azure builds brand-new databases only: any user table means this
    may be the real database, so nothing is run."""
    cur = db.cursor()
    cur.execute("SELECT DB_NAME(), (SELECT COUNT(*) FROM sys.tables WHERE is_ms_shipped = 0)")
    database, tables = cur.fetchone()
    cur.close()
    if tables:
        sys.exit(f"Refusing to build: {database} already has {tables} tables. "
                 "--azure builds brand-new, empty databases only.")
    print(f"Building {database} in Azure (empty: no tables)")


def drop_everything(db):
    """Drop every user view and table, foreign keys first, so the build
    can't depend on anything left over from an earlier one."""
    cur = db.cursor()
    cur.execute("SELECT QUOTENAME(SCHEMA_NAME(t.schema_id)) + '.' + QUOTENAME(t.name), "
                "QUOTENAME(fk.name) FROM sys.foreign_keys fk "
                "JOIN sys.tables t ON t.object_id = fk.parent_object_id")
    for table, key in cur.fetchall():
        cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT {key}")
    for kind, catalog in (("VIEW", "sys.views"), ("TABLE", "sys.tables")):
        cur.execute(f"SELECT QUOTENAME(SCHEMA_NAME(schema_id)) + '.' + QUOTENAME(name) "
                    f"FROM {catalog} WHERE is_ms_shipped = 0")
        for (name,) in cur.fetchall():
            cur.execute(f"DROP {kind} {name}")
    db.commit()
    cur.close()


def main(args=None):
    args = sys.argv[1:] if args is None else args
    azure = "--azure" in args
    target = "azure" if azure else "local"
    demo = "--demo" in args
    example = demo or "--example" in args
    if not example and "--clean" not in args:
        sys.exit("Choose a mode: --clean, --example or --demo (see the docstring).")
    if azure and example:
        sys.exit("--azure builds --clean only: no example property or demo data in Azure.")
    if "--yes" not in args:
        sys.exit(f"This drops and recreates every table in the {target.upper()} database.\n"
                 "Run again with --yes to continue.")

    db = connect_db(target)
    try:
        if azure:
            refuse_unless_empty(db)
        else:
            refuse_azure(db)
        drop_everything(db)
        for name in BUILD:
            run_sql_file(db, SQL / name)
    finally:
        db.close()

    if example:
        # The example property, through the same script real users run
        sys.stdout.flush()      # keep this script's output in order with the child's
        result = subprocess.run([sys.executable, str(HERE / "setup_property.py"),
                                 "--config", "config/property.example.json"])
        if result.returncode != 0:
            sys.exit("setup_property.py failed")

    if demo:
        db = connect_db("local")
        try:
            make_demo_data.insert_expenses_and_interest(db)
        finally:
            db.close()
        # Bookings through the real pipeline, from exports in a temporary folder
        with tempfile.TemporaryDirectory() as folder:
            names = make_demo_data.write_exports(folder)
            print(f"Wrote {len(names)} demo exports ({names[0]} to {names[-1]})")
            sys.stdout.flush()
            result = subprocess.run([sys.executable, str(HERE / "load_airbnb.py"),
                                     "--from-dir", folder])
        if result.returncode != 0:
            sys.exit("load_airbnb.py failed")

    db = connect_db(target)
    try:
        if not demo:                # load_airbnb.py has just rebuilt it from the demo load
            run_sql_file(db, SQL / STAR_LOAD)
        for name in CHECKS:
            run_checks(db, SQL / name)
        if example:
            for name in TESTS:
                run_test_file(db, SQL / name)
        for name in QUERIES:
            run_sql_file(db, SQL / name)
    finally:
        db.close()

    mode = "demo" if demo else "example" if example else "clean"
    print(f"\nBuild complete ({mode}): every step, check"
          f"{' and test' if example else ''} passed.")
    if not example:
        print(f"Next: python3 python/setup_property.py {'azure ' if azure else ''}"
              "--config <your config>")


if __name__ == "__main__":
    main()