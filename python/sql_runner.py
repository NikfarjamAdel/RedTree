"""Running SQL files against an open connection.

SQL files are split on GO lines, like SQL Server's own tools do. Three
kinds of file:
  run_sql_file    statements; one transaction per file
  run_checks      each batch is a query that returns no rows when the
                  data is right (30, 31, 63)
  run_test_file   a test script whose LAST result set lists failures only,
                  so an empty last result set means every test passed (33, 34)
"""
import re


def split_batches(sql):
    return [b.strip() for b in re.split(r"(?im)^\s*GO\s*$", sql) if b.strip()]


def run_sql_file(db, path, log=print):
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
    log(f"Ran {path.name} ({len(batches)} batches)")


def run_checks(db, path, log=print):
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
            log(f"CHECK {num} FAILED: {count} rows")
            for row in sample:
                log(f"   {row}")
        raise RuntimeError(f"{len(failures)} checks failed in {path.name}")
    log(f"All {len(checks)} checks passed in {path.name}")


def run_test_file(db, path, log=print):
    """Run a test script. Its last result set must list failures only:
    empty means every test passed."""
    cur = db.cursor()
    last_rows = None
    try:
        for batch in split_batches(path.read_text()):
            cur.execute(batch)
            while True:
                if cur.description is not None:      # this result set returns rows
                    last_rows = cur.fetchall()
                if not cur.nextset():
                    break
        db.commit()
    finally:
        cur.close()
    if last_rows:
        for row in last_rows:
            log(f"   FAILED: {tuple(row)}")
        raise RuntimeError(f"{len(last_rows)} tests failed in {path.name}")
    log(f"All tests passed in {path.name}")