"""Fetch each listing's Airbnb calendar feed and keep a snapshot.

For each listing with a calendar feed (LISTING.ical_url_setting):
1. Download the .ics feed; the URL is read from the setting it names.
2. Check it really is a calendar (Airbnb can return an error page with
   status 200, which must not be stored as a snapshot).
3. Upload it to the ical container as listing<id>/<UTC timestamp>.ics.
4. Insert an ICAL_SNAPSHOT row with the same timestamp and a SHA-256 hash.

The feed list comes from the database, and is also saved to the ical
container as feeds.json. If the database is unavailable, the saved list is
used, so today's snapshot is still taken. The file is uploaded before its
row is inserted, so a failed insert loses nothing: index_ical.py indexes
the file later. Every run creates a new snapshot on purpose: the history
of the calendar is the point.

Usage:
    python3 python/fetch_ical.py [azure]
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import requests
from azure.storage.blob import BlobClient, ContainerClient, ContentSettings
from engine_setup import connect_db
from extraction_common import BLOB_CONNECTION  # also loads .env, outside Azure

ICAL_CONTAINER = os.environ["AZURE_ICAL_CONTAINER"]
FEEDS_BLOB = "feeds.json"      # top level of the ical container; refreshed every run


def load_feeds(target):
    """[(listing_id, url_setting)] for every listing with a calendar feed.

    Read from LISTING and saved to blob storage. If the database can't be
    reached, the last saved list is used instead, so the snapshot is still
    taken; that only fails if no list has ever been saved."""
    container = ContainerClient.from_connection_string(BLOB_CONNECTION, ICAL_CONTAINER)
    try:
        db = connect_db(target)
        try:
            cur = db.cursor()
            cur.execute("SELECT listing_id, ical_url_setting FROM LISTING "
                        "WHERE ical_url_setting IS NOT NULL ORDER BY listing_id")
            feeds = [(listing_id, setting) for listing_id, setting in cur.fetchall()]
        finally:
            db.close()
        container.upload_blob(FEEDS_BLOB, json.dumps(feeds), overwrite=True)
        return feeds
    except Exception as e:
        print(f"Could not read the feed list from the database or save it to the "
              f"ical container ({type(e).__name__}: {e}); using the saved list")
        saved = container.download_blob(FEEDS_BLOB).readall()
        return [tuple(feed) for feed in json.loads(saved)]


def fetch_feed(url):
    """Return the feed's text. Raise if the request fails or the
    content isn't a calendar."""
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    response.encoding = "utf-8"
    text = response.text.lstrip("\ufeff")
    if not text.startswith("BEGIN:VCALENDAR"):
        raise ValueError(f"Content is not a calendar: {text[:100]}")
    return text


def upload_snapshot(listing_id, fetched_at, content):
    """Upload the feed as listing<id>/<timestamp>.ics; return its URL."""
    name = f"listing{listing_id}/{fetched_at:%Y-%m-%dT%H%M%SZ}.ics"
    blob = BlobClient.from_connection_string(BLOB_CONNECTION, ICAL_CONTAINER, name)
    blob.upload_blob(content.encode("utf-8"), overwrite=False,
                     content_settings=ContentSettings(content_type="text/calendar"))
    return blob.url


def insert_snapshot(db, cur, listing_id, fetched_at, blob_url, content_hash):
    """One ICAL_SNAPSHOT row, committed immediately."""
    cur.execute(
        "INSERT INTO ICAL_SNAPSHOT (listing_id, fetched_at, blob_url, content_hash) "
        "VALUES (?, ?, ?, ?)",
        listing_id,
        fetched_at,
        blob_url,
        content_hash,
    )
    db.commit()


def stable_hash(content):
    """SHA-256 of the feed without DTSTAMP lines. Airbnb's DTSTAMP is
    when the feed was generated, not a booking change."""
    stable = "\n".join(line for line in content.splitlines()
                       if not line.startswith("DTSTAMP"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def main():
    target = "azure" if "azure" in sys.argv[1:] else "local"
    snapshots = []
    failures = 0

    feeds = load_feeds(target)
    if not feeds:
        print("No listing has a calendar feed (LISTING.ical_url_setting)")
        return

    # Phase 1: fetch and upload. Needs only Airbnb and blob storage.
    for listing_id, url_setting in feeds:
        try:
            url = os.environ.get(url_setting)
            if not url:
                raise KeyError(f"setting {url_setting} is not set")
            fetched_at = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
            content = fetch_feed(url)
            content_hash = stable_hash(content)
            blob_url = upload_snapshot(listing_id, fetched_at, content)
            snapshots.append((listing_id, fetched_at, blob_url, content_hash))
            print(f"listing {listing_id}: saved {fetched_at:%Y-%m-%dT%H%M%SZ}  "
                  f"{len(content.encode('utf-8'))} bytes  {content_hash[:12]}")
        except Exception as e:
            failures += 1
            print(f"listing {listing_id}: FAILED {type(e).__name__}: {e}")

    # Phase 2: index in the database. If this fails, the snapshots are
    # still safe in blob storage, and index_ical.py indexes them later.
    if snapshots:
        try:
            db = connect_db(target)
        except Exception as e:
            print(f"DATABASE UNAVAILABLE ({type(e).__name__}): "
                  f"{len(snapshots)} snapshot(s) saved but not indexed")
            sys.exit(1)
        cur = db.cursor()
        try:
            for snapshot in snapshots:
                try:
                    insert_snapshot(db, cur, *snapshot)
                except Exception as e:
                    db.rollback()
                    failures += 1
                    print(f"listing {snapshot[0]}: INDEX FAILED {type(e).__name__}: {e}")
        finally:
            db.close()

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()