"""Index .ics snapshots in blob storage that ICAL_SNAPSHOT doesn't have yet.

The files are the record; this builds the index from them. It covers
snapshots saved while the database was unavailable, and snapshots made
by runs against the other database. Safe to rerun.

Usage:
    python3 python/index_ical.py [azure]
"""
import re
import sys
from datetime import datetime

from azure.storage.blob import ContainerClient
from engine_setup import connect_db
from extraction_common import BLOB_CONNECTION
from fetch_ical import FEEDS_BLOB, ICAL_CONTAINER, insert_snapshot, stable_hash

# listing1/2025-04-01T053000Z.ics
NAME = re.compile(r"^listing(\d+)/(\d{4}-\d{2}-\d{2}T\d{6})Z\.ics$")


def main():
    target = "azure" if "azure" in sys.argv[1:] else "local"
    db = connect_db(target)
    cur = db.cursor()

    cur.execute("SELECT listing_id, fetched_at FROM ICAL_SNAPSHOT")
    known = {(row[0], row[1]) for row in cur.fetchall()}

    container = ContainerClient.from_connection_string(BLOB_CONNECTION, ICAL_CONTAINER)
    added = already = 0
    try:
        for blob in container.list_blobs():
            if blob.name == FEEDS_BLOB:          # the saved feed list, not a snapshot
                continue
            m = NAME.match(blob.name)
            if not m:
                print(f"skipped, unexpected name: {blob.name}")
                continue
            listing_id = int(m.group(1))
            fetched_at = datetime.strptime(m.group(2), "%Y-%m-%dT%H%M%S")
            if (listing_id, fetched_at) in known:
                already += 1
                continue

            client = container.get_blob_client(blob.name)
            content = client.download_blob().readall().decode("utf-8")
            insert_snapshot(db, cur, listing_id, fetched_at,
                            client.url, stable_hash(content))
            added += 1
            print(f"indexed {blob.name}")
    finally:
        db.close()

    print(f"\n{added} indexed, {already} already indexed")


if __name__ == "__main__":
    main()