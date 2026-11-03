"""
Fetch emails over IMAP and load them into EMAIL_RAW and RECEIPT.

For each message in the mailbox:
    1. Skip it if its message_id is already in EMAIL_RAW.
    2. Upload its attachments to Azure Blob Storage. Existing blobs are
       not overwritten.
    3. In a single transaction, insert the EMAIL_RAW row and one RECEIPT
       row per attachment, then commit.

Each email's recipient is stored: the address it was sent to, which routes
its receipts to a listing (plus-addressing, e.g. mailbox+redtree@gmail.com).

Attachments are uploaded before the database rows are inserted. If an
insert fails, the message is not recorded in EMAIL_RAW and is processed
again on the next run. Inserting first could leave an EMAIL_RAW row with
no stored attachments, and that message would be skipped on later runs.

The script is idempotent: running it again reports every message as
already stored.

By default only mail received since the last fetch (with a two-day margin)
is scanned. Pass --full to scan the whole mailbox.

Usage:
    python3 python/fetch_emails.py [azure] [--full]

Examples:
    python3 python/fetch_emails.py               # local database, new mail
    python3 python/fetch_emails.py azure         # Azure database, new mail
    python3 python/fetch_emails.py azure --full  # Azure database, all mail
"""
import hashlib
import imaplib
import os
import re
import sys
from datetime import timedelta, timezone
from email import message_from_bytes
from email.policy import default
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import load_dotenv
from engine_setup import connect_db
from mail_common import receipt_files

if "FUNCTIONS_WORKER_RUNTIME" not in os.environ:      # never in Azure: settings come from the app
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

IMAP_HOST = "imap.gmail.com"
IMAP_USER = os.environ["GMAIL_USER"]
IMAP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
BLOB_CONNECTION = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
CONTAINER = os.environ["AZURE_STORAGE_CONTAINER"]
LOCAL_TZ = ZoneInfo("Australia/Adelaide")

MAX_LEN = {"message_id": 250, "sender": 250, "subject": 500, "recipient": 250}
ATT_MAX_LEN = {"original_filename": 260, "content_type": 100}

# Headers that can carry the address an email was delivered to
RECIPIENT_HEADERS = ("Delivered-To", "X-Original-To", "To", "Cc")


def s(x):
    """Convert a header value to str, preserving None so it is stored as NULL."""
    return None if x is None else str(x)


def known_receipt_addresses(cur):
    """The listings' receipt addresses (lower case)."""
    cur.execute("SELECT receipt_address FROM LISTING WHERE receipt_address IS NOT NULL")
    return {row[0] for row in cur.fetchall()}


def recipient_of(msg, receipt_addresses):
    """The address this email was sent to, lower case: a listing's receipt
    address if any header names one, otherwise the first delivery address.
    A forwarded receipt usually shows the address in To; one sent directly
    may show it only in Delivered-To, so all of them are checked."""
    addresses = [address.lower()
                 for header in RECIPIENT_HEADERS
                 for _, address in getaddresses(msg.get_all(header, []))
                 if address]
    for address in addresses:
        if address in receipt_addresses:
            return address
    return addresses[0] if addresses else None


def connect_imap():
    """Open an IMAP connection and select INBOX in read-only mode."""
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    conn.login(IMAP_USER, IMAP_PASSWORD)
    conn.select("INBOX", readonly=True)
    return conn


def safe_logout(conn):
    """Log out of IMAP, ignoring errors from an already-closed connection."""
    try:
        conn.logout()
    except (imaplib.IMAP4.abort, OSError):
        pass


def iter_messages(criteria):
    """Yield (message number, raw bytes) for each message matching criteria.

    Manages its own IMAP connection. If the connection drops, it reconnects
    and retries the fetch, up to three attempts per message.
    """
    conn = connect_imap()
    try:
        _, data = conn.search(None, criteria)
        ids = data[0].split()
        print(f"{len(ids)} messages\n")
        for i, num in enumerate(ids, start=1):
            print(f"[{i}/{len(ids)}] ", end="", flush=True)
            for attempt in range(1, 4):
                try:
                    _, raw = conn.fetch(num, "(BODY.PEEK[])")
                    break
                except (imaplib.IMAP4.abort, OSError) as e:
                    if attempt == 3:
                        raise            # Give up after three failed attempts
                    print(f"\n  connection dropped ({e}), reconnecting...")
                    safe_logout(conn)
                    conn = connect_imap()
            yield num.decode(), raw[0][1]
    finally:
        safe_logout(conn)


def to_local(date_header):
    """Convert a Date header to a naive datetime in LOCAL_TZ.

    Dates without a timezone are treated as UTC.

    Raises:
        ValueError: If the header is missing.
    """
    if date_header is None:
        raise ValueError("no Date header")
    dt = parsedate_to_datetime(date_header)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).replace(tzinfo=None)


def parse_message(raw, receipt_addresses):
    """Parse a raw email into a dict of the fields stored in the database.

    If the message has no Message-ID header, a SHA-256 hash of the raw
    bytes is used instead.
    """
    msg = message_from_bytes(raw, policy=default)
    parsed = {}
    parsed["message_id"] = (str(msg["Message-ID"]) if msg["Message-ID"]
                            else f"sha256:{hashlib.sha256(raw).hexdigest()}")
    parsed["sender"] = msg["From"]
    parsed["recipient"] = recipient_of(msg, receipt_addresses)
    parsed["subject"] = msg["Subject"]
    parsed["received_at"] = to_local(msg["Date"])
    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    parsed["body_text"] = text_part.get_content() if text_part else None
    parsed["body_html"] = html_part.get_content() if html_part else None
    parsed["attachments"] = [
        {"original_filename": f, "content_type": t, "data": d}
        for f, t, d in receipt_files(msg)
    ]
    return parsed


def blob_name(message_id, index, filename):
    """Build a deterministic blob name for an attachment.

    The name combines a sanitised, truncated message ID with a short hash
    of the full ID, so the same attachment always maps to the same blob.
    """
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:12]
    readable = re.sub(r"[^A-Za-z0-9_.-]", "_",
                      message_id.strip().strip("<>"))[:60]
    safe_filename = re.sub(r"[^A-Za-z0-9_.-]", "_", filename or "attachment")
    return f"{readable}_{digest}/{index:02d}_{safe_filename}"


def upload(container, name, data, content_type):
    """Upload a file to blob storage without overwriting an existing blob.

    Returns:
        A tuple (result, url), where result is 'uploaded' or 'skipped'.
        The URL is returned in both cases, since an existing blob may still
        need to be referenced by a new RECEIPT row.
    """
    blob = container.get_blob_client(name)
    try:
        blob.upload_blob(data, overwrite=False,
                         content_settings=ContentSettings(content_type=content_type))
        return 'uploaded', blob.url
    except ResourceExistsError:
        return 'skipped', blob.url


def existing_message_ids(cur):
    """Return the set of message_id values already stored in EMAIL_RAW."""
    cur.execute("SELECT message_id FROM EMAIL_RAW")
    return {row[0] for row in cur.fetchall()}


def search_criteria(cur, full_scan):
    """Return the IMAP search criteria for this run.

    Searches from two days before the most recent fetched_at in EMAIL_RAW.
    Falls back to ALL when the table is empty or full_scan is True.
    """
    cur.execute("SELECT MAX(fetched_at) FROM EMAIL_RAW")
    last = cur.fetchone()[0]
    if last is None or full_scan:
        print("Full scan")
        return "ALL"
    since = last - timedelta(days=2)
    print(f"Fetching since {since:%d-%b-%Y}")
    return f'(SINCE "{since:%d-%b-%Y}")'


def check_lengths(m):
    """Check that message and attachment values fit their database columns.

    Raises:
        ValueError: If any value exceeds its column's maximum length.
    """
    for key, limit in MAX_LEN.items():
        value = m.get(key)
        if value is not None and len(str(value)) > limit:
            raise ValueError(f"{key} too long ({len(str(value))} > {limit}) in {m['message_id']}")
    for index, att in enumerate(m["attachments"]):
        for key, limit in ATT_MAX_LEN.items():
            value = att.get(key)
            if value is not None and len(str(value)) > limit:
                raise ValueError(f"Attachment {index} ({att['original_filename']}) of "
                                 f"{m['message_id']}: {key} too long ({len(value)} > {limit})")


def insert_email(cur, m):
    """Insert an EMAIL_RAW row and return its generated email_id."""
    cur.execute(
        "INSERT INTO EMAIL_RAW (message_id, received_at, sender, recipient, subject, "
        "body_text, body_html) "
        "OUTPUT INSERTED.email_id "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        m["message_id"],
        m["received_at"],
        s(m["sender"]),
        m["recipient"],
        s(m["subject"]),
        m["body_text"],
        m["body_html"],
    )
    return cur.fetchone()[0]


def insert_receipt(cur, email_id, index, att, url):
    """Insert a RECEIPT row for one attachment of an email."""
    cur.execute(
        "INSERT INTO RECEIPT (email_id, attachment_index, blob_url, original_filename, "
        "content_type, file_size_bytes, file_sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        email_id,
        index,
        url,
        att["original_filename"],
        att["content_type"],
        len(att["data"]),
        hashlib.sha256(att["data"]).hexdigest(),
    )


def main():
    args = sys.argv[1:]
    target = "azure" if "azure" in args else "local"
    full_scan = "--full" in args

    db = connect_db(target)
    cur = db.cursor()
    known = existing_message_ids(cur)
    receipt_addresses = known_receipt_addresses(cur)
    print(f"{len(known)} messages already in EMAIL_RAW; "
          f"{len(receipt_addresses)} listing receipt address(es)")
    criteria = search_criteria(cur, full_scan)

    container = BlobServiceClient.from_connection_string(
        BLOB_CONNECTION).get_container_client(CONTAINER)

    n_messages = n_new = n_known = n_uploaded = n_failed = n_skipped = 0
    try:
        for num, raw in iter_messages(criteria):
            n_messages += 1
            try:
                m = parse_message(raw, receipt_addresses)
                if m["message_id"] in known:
                    print(f'stored  {m["received_at"]:%Y-%m-%d %H:%M}  '
                          f'{m["sender"]}  "{m["subject"]}"')
                    n_known += 1
                    continue
                check_lengths(m)
                urls = []
                for index, member in enumerate(m["attachments"]):
                    name = blob_name(m['message_id'], index, member['original_filename'])
                    result, new_url = upload(container, name, member['data'],
                                             member['content_type'])
                    urls.append(new_url)
                    if result == 'uploaded':
                        n_uploaded += 1
                    else:
                        n_skipped += 1
                email_id = insert_email(cur, m)
                for index, member in enumerate(m["attachments"]):
                    insert_receipt(cur, email_id, index, member, urls[index])
                db.commit()
                known.add(m["message_id"])
                n_new += 1

                print(f'{m["received_at"]:%Y-%m-%d %H:%M}  {m["sender"]}  '
                      f'"{m["subject"]}"  to {m["recipient"]}  {len(m["attachments"])} att')

            except Exception as e:
                db.rollback()
                n_failed += 1
                print(f"FAILED message {num}: {type(e).__name__}: {e}")
    finally:
        db.close()

    print(f"\n{n_messages} messages, {n_new} new, {n_known} already stored, "
          f"{n_uploaded} blobs uploaded, {n_skipped} skipped, {n_failed} failed")


if __name__ == "__main__":
    main()