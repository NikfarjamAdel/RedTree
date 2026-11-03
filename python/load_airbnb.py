"""Load new Airbnb exports from blob storage (or a local folder), then bring
bookings, cleaning and the star schema up to date. Run daily by pipeline.py; safe to run any
time, and does useful work even when no new export has been uploaded.

Exports are uploaded (monthly) to the top level of the blob container
AZURE_EXPORTS_CONTAINER, with the same columns in both:
  airbnb_YYYY-MM-DD.csv           earnings: payments that have happened
  airbnb_upcoming_YYYY-MM-DD.csv  upcoming: bookings not yet paid
A file is new if its name isn't in LOAD_LOG. New files are staged together
and transformed (50), and only then recorded in LOAD_LOG, so a failed load
is retried on the next run. Upcoming rows create bookings and stay nights
but never payouts: their date is an expected payout, not money received.

Every run, new files or not, in this order:
  51 manual corrections            53 calendar changes -> corrections, cancellations
  54 corrections applied, stay nights synced, completed history
  52 imputed cleaning (after the dates are final)
  checks 30 and 31; star rebuild 61; star checks 63 (immediately after the rebuild)

Each check is a query that returns no rows when the data is right; any rows
are printed and the run fails. Each SQL file is one transaction; files are
split on GO lines.

Usage: python3 python/load_airbnb.py [azure] [--from-dir <folder>]
  --from-dir  read exports from a local folder instead of blob storage
              (named the same way; build_db.py --demo uses it)
"""
import io
import os
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from engine_setup import REPO_ROOT, connect_db

SQL_DIR = REPO_ROOT / "sql"

# Exact patterns, so an upcoming file is never mistaken for an earnings file
PATTERNS = {
    "paid":     re.compile(r"^airbnb_(\d{4}-\d{2}-\d{2})\.csv$"),
    "upcoming": re.compile(r"^airbnb_upcoming_(\d{4}-\d{2}-\d{2})\.csv$"),
}

COLUMNS = {
    'Date': 'stg_date',
    'Arriving by date': 'stg_arrived_by_date',
    'Type': 'stg_type',
    'Confirmation Code': 'stg_confirmation_code',
    'Booking date': 'stg_booking_date',
    'Start date': 'stg_start_date',
    'End date': 'stg_end_date',
    'Nights': 'stg_nights',
    'Guest': 'stg_guest_name',
    'Listing': 'stg_listing',
    'Details': 'stg_details',
    'Reference code': 'stg_reference_code',
    'Currency': 'stg_currency',
    'Amount': 'stg_amount',
    'Paid out': 'stg_payout',
    'Service fee': 'stg_service_fee',
    'Fast pay fee': 'stg_fast_pay_fee',
    'Cleaning fee': 'stg_cleaning_fee',
    'Gross earnings': 'stg_gross_earning',
    'Airbnb remitted tax': 'stg_airbnb_remitted_tax',
    'Earnings year': 'stg_earning_year',
}

# Run every time, in this order
ALWAYS = ("51_corrections.sql", "53_apply_ical_changes.sql",
          "54_sync_bookings.sql", "52_impute_cleaning.sql")


def split_batches(sql):
    return [b.strip() for b in re.split(r"(?im)^\s*GO\s*$", sql) if b.strip()]


def exports_container():
    # Imported here: they read Azure settings, which --from-dir doesn't need
    from azure.storage.blob import ContainerClient
    from extraction_common import BLOB_CONNECTION
    return ContainerClient.from_connection_string(
        BLOB_CONNECTION, os.environ["AZURE_EXPORTS_CONTAINER"])


def new_exports(db, names):
    """(name, kind, export_date) for each export not yet in LOAD_LOG, oldest first."""
    cur = db.cursor()
    try:
        cur.execute("SELECT file_name FROM LOAD_LOG")
        loaded = {row[0] for row in cur.fetchall()}
    finally:
        cur.close()

    found = []
    for name in names:
        for kind, pattern in PATTERNS.items():
            m = pattern.match(name)
            if m and name not in loaded:
                found.append((name, kind, date.fromisoformat(m.group(1))))
    return sorted(found, key=lambda f: (f[2], f[1]))


def read_export(data):
    """The file's bytes as text, with columns renamed to staging names. The
    upcoming export lacks a few columns (e.g. 'Paid out'); those stay NULL."""
    df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
    return df.rename(columns=COLUMNS)


def load_to_staging(db, frames):
    """Empty staging and load every new file, in one transaction."""
    cur = db.cursor()
    try:
        cur.execute("TRUNCATE TABLE STG_AIRBNB_EARNINGS")
        for name, kind, _, df in frames:
            df = df.assign(stg_source_file=name, stg_file_kind=kind)
            columns = ", ".join(df.columns)
            params = ", ".join("?" * len(df.columns))
            cur.executemany(
                f"INSERT INTO STG_AIRBNB_EARNINGS ({columns}) VALUES ({params})",
                df.values.tolist())
            print(f"Staged {len(df)} {kind} rows from {name}")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()


def record_loads(db, frames):
    """Record the files in LOAD_LOG, after their transform has succeeded."""
    cur = db.cursor()
    try:
        for name, kind, export_date, df in frames:
            cur.execute("INSERT INTO LOAD_LOG (file_name, file_kind, export_date, row_count) "
                        "VALUES (?, ?, ?, ?)", name, kind, export_date, len(df))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()


def run_sql_file(db, path):
    batches = split_batches(path.read_text())
    cur = db.cursor()
    try:
        for batch in batches:
            cur.execute(batch)
            # an error in a later statement of the batch only surfaces
            # when its result is reached
            while cur.nextset():
                pass
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
    print(f"Ran {path.name} ({len(batches)} batches)")


def run_checks(db, path):
    checks = split_batches(path.read_text())
    failures = []
    cur = db.cursor()
    try:
        for i, check in enumerate(checks, start=1):
            rows = cur.execute(check).fetchall()
            if rows:
                failures.append((i, len(rows), rows[:5]))
    finally:
        cur.close()
    if failures:
        for num, count, sample in failures:
            print(f"CHECK {num} FAILED: {count} rows")
            for row in sample:
                print(f"   {row}")
        raise RuntimeError(f"{len(failures)} checks failed in {path.name}")
    print(f"All {len(checks)} checks passed in {path.name}")


def main():
    args = sys.argv[1:]
    target = "azure" if "azure" in args else "local"
    folder = Path(args[args.index("--from-dir") + 1]) if "--from-dir" in args else None
    db = connect_db(target)
    print(f"Target: {target} -> {os.environ[('AZURE_' if target == 'azure' else '') + 'MSSQL_HOST']}")
    try:
        if folder:
            print(f"Exports from folder {folder}")
            names = [p.name for p in folder.iterdir() if p.is_file()]
            read = lambda name: (folder / name).read_bytes()
        else:
            container = exports_container()
            names = [blob.name for blob in container.list_blobs()]
            read = lambda name: container.download_blob(name).readall()
        new = new_exports(db, names)
        if new:
            frames = [(name, kind, export_date, read_export(read(name)))
                      for name, kind, export_date in new]
            load_to_staging(db, frames)
            run_sql_file(db, SQL_DIR / "50_transform.sql")
            record_loads(db, frames)
        else:
            print("No new exports")

        for name in ALWAYS:
            run_sql_file(db, SQL_DIR / name)
        run_checks(db, SQL_DIR / "30_data_quality.sql")
        run_checks(db, SQL_DIR / "31_load_assertions.sql")

        run_sql_file(db, SQL_DIR / "61_star_load.sql")
        run_checks(db, SQL_DIR / "63_star_checks.sql")
    finally:
        db.close()


if __name__ == "__main__":
    main()