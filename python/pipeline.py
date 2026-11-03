"""The daily run: every step of the pipeline, in order, against one database.

Each step is an existing script, run as its own process with the same
arguments (e.g. 'azure'). Every step is idempotent, so a failed step is
simply retried on the next run, and later steps still run: they do useful
work on their own. If any step fails, an alert email lists the failed steps
with the end of their output, and the run exits non-zero.

Order matters at the end: load_airbnb runs last because it rebuilds the
star schema, which must include today's new expenses and calendar changes.
Review (review_receipts.py) is not part of the run: it needs a person.

Usage:
    python3 python/pipeline.py [azure]
    python3 python/pipeline.py --test-alert      # send a test alert email
"""
import os
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
if "FUNCTIONS_WORKER_RUNTIME" not in os.environ:     # never in Azure: settings come from the app
    load_dotenv(HERE.parent / ".env")

STEPS = (
    ("calendar", "fetch_ical.py"),
    ("calendar", "index_ical.py"),
    ("calendar", "parse_ical.py"),
    ("receipts", "fetch_emails.py"),
    ("receipts", "extract_receipts.py"),
    ("receipts", "validate_receipts.py"),
    ("receipts", "categorise_receipts.py"),
    ("receipts", "create_expenses.py"),
    ("bookings", "load_airbnb.py"),        # last: rebuilds the star schema
)

TAIL_LINES = 15          # lines of a failed step's output included in the alert


def describe_target(args, log=print):
    """Log where this run will write: database host and storage account,
    read from the environment, before any step runs."""
    azure = "azure" in args
    host = os.environ.get("AZURE_MSSQL_HOST" if azure else "MSSQL_HOST", "?")
    database = os.environ.get("AZURE_MSSQL_DB" if azure else "MSSQL_DB", "?")
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    account = next((part.split("=", 1)[1] for part in conn.split(";")
                    if part.startswith("AccountName=")), "?")
    log(f"Target:  {'azure' if azure else 'local'}")
    log(f"Database: {database} on {host}")
    log(f"Storage:  {account}")


def run(args, log=print):
    """Run every step; return [(script, last lines of output)] for those
    that failed. Each step's output is captured and passed to log.

    Each step runs as its own Python process. In Azure Functions the
    installed packages live outside Python's default search path, and only
    the function's own process is told about them, so the same search path
    (sys.path) is passed on to every step through PYTHONPATH.
    """
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    failures = []
    for group, script in STEPS:
        log(f"=== {group}: {script}")
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(HERE / script), *args],
                                capture_output=True, text=True, env=env)
        lines = (result.stdout + result.stderr).splitlines()
        for line in lines:
            log(f"    {line}")
        seconds = time.monotonic() - started
        if result.returncode == 0:
            log(f"=== {script}: ok ({seconds:.0f}s)")
        else:
            log(f"=== {script}: FAILED, exit {result.returncode} ({seconds:.0f}s)")
            failures.append((script, lines[-TAIL_LINES:]))
    return failures


def send_alert(subject, body, log=print):
    """Email ALERT_EMAIL through the receipts Gmail account. Never raises:
    a failed alert is logged, and must not hide the pipeline's own result."""
    sender = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("ALERT_EMAIL")
    if not (sender and password and to):
        log("Alert NOT sent: GMAIL_USER, GMAIL_APP_PASSWORD or ALERT_EMAIL is missing")
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to
    message.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, password)
            smtp.send_message(message)
        log(f"Alert sent to {to}")
    except Exception as e:
        log(f"Alert NOT sent: {type(e).__name__}: {e}")


def failure_report(failures, minutes):
    """The body of the alert email."""
    parts = [f"{len(failures)} of {len(STEPS)} steps failed (the run took {minutes:.1f} min).", ""]
    for script, tail in failures:
        parts += [f"--- {script}: last lines of output", *tail, ""]
    parts.append("Every step is idempotent: failed steps are retried on the next daily run.")
    return "\n".join(parts)


def run_and_report(args, log=print):
    """The whole run: target, every step, and an alert email if anything
    failed. Returns the failures."""
    started = time.monotonic()
    describe_target(args, log)
    failures = run(args, log)
    minutes = (time.monotonic() - started) / 60
    if failures:
        names = ", ".join(script for script, _ in failures)
        log(f"Pipeline finished in {minutes:.1f} min with failures: {names}")
        send_alert(f"RedTree pipeline: {len(failures)} step(s) failed",
                   failure_report(failures, minutes), log)
    else:
        log(f"Pipeline finished in {minutes:.1f} min: all {len(STEPS)} steps ok")
    return failures


def main():
    args = sys.argv[1:]
    if "--test-alert" in args:
        send_alert("RedTree pipeline: test alert",
                   "If you can read this, failure alerts work.")
        return
    failures = run_and_report(args)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
