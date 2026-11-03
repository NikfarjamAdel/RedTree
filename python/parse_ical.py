"""Parse stored iCal snapshots into ICAL_EVENT rows.

For each ICAL_SNAPSHOT not yet parsed (parsed_at IS NULL), oldest first:
download its .ics from blob storage, store one row per event, and set
parsed_at, all in one transaction. Safe to rerun: parsed snapshots are
skipped, and a failed one is retried on the next run.

Only what later steps need is stored: dates, kind, UID and confirmation
code. The full file, including guest phone digits, stays in blob storage.

Usage: python3 python/parse_ical.py [azure]
"""
import os
import re
import sys
from datetime import datetime

from azure.storage.blob import BlobClient
from engine_setup import connect_db
from extraction_common import BLOB_CONNECTION, blob_name_from_url  # also loads .env
from icalendar import Calendar

ICAL_CONTAINER = os.environ["AZURE_ICAL_CONTAINER"]

# The confirmation code inside the reservation link in DESCRIPTION
CODE_PATTERN = re.compile(r"/reservations/details/([A-Z0-9]+)")


def as_date(value):
    """iCal dates can be dates or date-times; ICAL_EVENT stores dates."""
    return value.date() if isinstance(value, datetime) else value


def events_in(ics_bytes):
    """Each VEVENT in the file, as a dict ready for ICAL_EVENT."""
    calendar = Calendar.from_ical(ics_bytes)
    for event in calendar.walk("VEVENT"):
        summary = str(event.get("SUMMARY", "")).strip()
        match = CODE_PATTERN.search(str(event.get("DESCRIPTION", "")))
        reserved = summary.lower() == "reserved"
        yield {
            "uid": str(event.get("UID")),
            "kind": "reserved" if reserved else "blocked",
            "start_date": as_date(event.get("DTSTART").dt),
            "end_date": as_date(event.get("DTEND").dt),
            "summary": summary[:200],
            "confirmation_code": match.group(1) if (reserved and match) else None,
        }


def download(blob_url):
    blob = BlobClient.from_connection_string(
        BLOB_CONNECTION, ICAL_CONTAINER, blob_name_from_url(blob_url))
    return blob.download_blob().readall()


def main():
    target = "azure" if "azure" in sys.argv[1:] else "local"
    db = connect_db(target)
    cur = db.cursor()
    try:
        cur.execute("SELECT snapshot_id, blob_url FROM ICAL_SNAPSHOT "
                    "WHERE parsed_at IS NULL ORDER BY fetched_at")
        todo = cur.fetchall()
        print(f"{len(todo)} snapshots to parse")

        for snapshot_id, blob_url in todo:
            try:
                events = list(events_in(download(blob_url)))
                for e in events:
                    cur.execute(
                        "INSERT INTO ICAL_EVENT (snapshot_id, uid, event_kind, start_date, "
                        "end_date, summary, confirmation_code) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        snapshot_id, e["uid"], e["kind"], e["start_date"],
                        e["end_date"], e["summary"], e["confirmation_code"])
                cur.execute("UPDATE ICAL_SNAPSHOT SET parsed_at = SYSDATETIME() "
                            "WHERE snapshot_id = ?", snapshot_id)
                db.commit()                      # events and parsed_at together
                reserved = sum(e["kind"] == "reserved" for e in events)
                print(f"snapshot {snapshot_id}: {len(events)} events ({reserved} reserved)")
            except Exception as e:
                db.rollback()
                print(f"snapshot {snapshot_id}: FAILED {type(e).__name__}: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    main()